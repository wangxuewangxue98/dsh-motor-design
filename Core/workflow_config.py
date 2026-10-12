"""
AI 团队工作流集中配置管理 (v4.00.004)

提供运行时可修改的工作流参数:
  - max_concurrent_workflows: 全局最大并发任务数
  - queue_timeout_seconds: 排队等待最长时间 (秒)
  - human_checkpoint_timeouts: 各人工检查点超时覆盖
  - agent_model_map: 各智能体独立模型选择 (空字符串=用默认;
    云端模型以 "cloud:<model>" 前缀编码, 由 get_agent_llm_spec 解析)

所有配置持久化到 data/workflow_config.json, 修改后即时生效无需重启后端。
"""
import os
import json
import copy
import threading
import logging

# H-3 二期 T2: 云端模型编解码能力 (来自统一工厂 Core.cloud_llm)
# 该模块仅依赖 os/typing, 无循环依赖风险, 可安全在顶层导入。
from cloud_llm import (
    CLOUD_PREFIX, decode_cloud, encode_cloud,
    is_cloud_model, get_cloud_client, CloudLLMUnavailable,
    _resolve_cloud_id,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# P0 (v4.02.67): 槽满率工程上限 · 单一真相源 (Single Source of Truth)
# ---------------------------------------------------------------------------
# 背景: 此前槽满率上限散落 4 处且互相漂移 —
#   quality_gate.py:53=0.75 (门禁代码默认)
#   workflow_config.py:128=0.75 (运行时配置默认)
#   ai_flow_config.py:209=80% → 同步为 0.80 (UI 默认, 会把门禁抬到 0.80!)
#   scheme_engine.py:347=0.76 (VPI 工艺参考表)
# 现收口到本模块, 其余消费点一律引用本常量/取值函数, 消除口径分裂。
#   通用散嵌/常规浸漆: 0.75 (工程上限)
#   真空压力浸漆(VPI): 0.76 (工艺背书可略高)
#   H 级绝缘成型绕组: 0.80 (需工艺背书)
# 注: 这是"硬上限"口径; 生成期目标带 (软带) 见 SLOT_FILL_TARGET_BAND。
# ═══════════════════════════════════════════════════════════════════════════
SLOT_FILL_MAX_DEFAULT = 0.75        # 通用工艺槽满率硬上限 (单一真相源)
SLOT_FILL_MAX_BY_PROCESS = {
    "default": SLOT_FILL_MAX_DEFAULT,
    "VPI": 0.76,                    # 真空压力浸漆
    "H级成型": 0.80,                 # H 级绝缘成型绕组 (需工艺背书)
}
# 生成期目标带 (软带): 生成器应使槽满率落在此区间, 越靠上限越危险。
SLOT_FILL_TARGET_BAND = (0.65, SLOT_FILL_MAX_DEFAULT)
# 生成期预检硬剔除线: 预估槽满率超过此值视为几何已饱和, 不送 L1 (省算力)。
SLOT_FILL_PRECHECK_HARD_CAP = 0.80


def get_slot_fill_max(process: str = None) -> float:
    """按工艺解析槽满率硬上限 (单一取值入口)。

    process 命不中时返回通用上限 0.75。process 采用子串匹配
    (如 '真空压力浸漆' 命中 'VPI' 需显式传 'VPI'; 传 'VPI 浸漆' 亦可)。
    """
    if process:
        p = str(process)
        for key, val in SLOT_FILL_MAX_BY_PROCESS.items():
            if key != "default" and key and key in p:
                return float(val)
    return float(SLOT_FILL_MAX_DEFAULT)


def get_slot_fill_target_band(process: str = None) -> tuple:
    """生成期目标带 (软带): (下限, 上限=工艺硬上限)。"""
    return (0.65, get_slot_fill_max(process))


# ─── 路径 ───
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
WORKFLOW_CONFIG_PATH = os.path.join(_DATA_DIR, "workflow_config.json")

# ─── 默认模型 ───
_DEFAULT_MODEL = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

# 多模态视觉模型默认值 (OCRImageRecognizeAgent 专用)
# 注意: 该智能体绝不能回退到文本模型 _DEFAULT_MODEL, 否则图片识别必然失败
DEFAULT_VL_MODEL = os.getenv("VL_MODEL_NAME", "qwen3-vl:2b-instruct")

# VL 识别等待上限 (秒) — v4.01.024: 120 → 300 (5 分钟)
DEFAULT_VL_TIMEOUT = int(os.getenv("VL_TIMEOUT", "300"))

# ─── LLM 运行时治理 (v5.1 集中治理 num_ctx / keep_alive; v5.2 看门狗) ───
# 目标: 消除 8 个 AI 团队 Agent 各自硬编码 ChatOllama 参数造成的口径不一致,
#       并防 T600 4GB 显存边界死锁 (双 4B 模型叠加 ≈5.4GB > 4GB)。
LLM_RUNTIME_DEFAULT = {
    # AI 团队文本模型统一上下文窗口: 4B 模型 KV cache 显存随 num_ctx 缩放, 4096 为稳妥值
    "num_ctx": 4096,
    # 单流程内跨多阶段中等 TTL: 避免每阶段反复重载模型造成抖动 (5 分钟)
    # 流程结束 / 人工检查点后由 orchestrator 显式调用 /api/models/unload 释放
    "keep_alive": "5m",
    # ─── 调用级超时 (v4.06: 对治检测报告#2 主线程 LLM 节点无 request_timeout 硬兜底) ───
    # 单条 LLM HTTP 请求的总超时 (秒, 连接+读取). Ollama worker 死锁时 llm.invoke 永不吐
    # chunk, 此处准时熔断避免整流程挂起. 经 ChatOllama.client_kwargs 注入底层 httpx;
    # 本环境 langchain_ollama 的 ChatOllama 无 request_timeout 字段 (已端到端验证
    # client_kwargs={"timeout":N} 可触发 ReadTimeout 生效). 仅设超时, 不含模型尺寸限制.
    "request_timeout": 180,
    # ─── 点评级超时 (v4.06-P2: 评审 LLM 整步护盾, 与调用级语义分层治理) ───
    # 单条「方案评审」LLM 调用的最大等待 (秒). 评审需逐方案点评 10 个方案, 长推理属正常,
    # 故点评级上限(默认2000s)远大于调用级(180s). 二者语义严格分层、不可混用:
    #   · 调用级 request_timeout: 单条 HTTP 请求的底层读取熔断 (Ollama 吐不出 chunk 即断),
    #     作用于 _get_llm 注入的 httpx.client_kwargs.timeout, 防单请求永久挂起。
    #   · 点评级 point_review_timeout_s: 整个评审步骤的预算上限 (含模型冷启 + 长推理),
    #     作用于 review_agent 的 future.result(timeout=...) 护盾, 防整步冻结。
    # 历史: 300→1000(v4.00.011b 误杀长推理)→2000(v4.00.011c 挂死兜底)→600(v4.02.66 收敛).
    # 实测: 10 方案逐评正常耗时 ~10s, 但 Ollama 过载时单步可拖到 28min 仍"正常吐字";
    #   2000s 兜底过长 → 用户感知"流程卡死". 收口到 600s: 既容忍冷启+长推理突刺,
    #   又能在 Ollama 真正卡死 ~10min 内熔断降级 (跳过 LLM 点评, 返回物理对比数据), 避免整流程冻结.
    # 可用环境变量 OLLAMA_REVIEW_TIMEOUT 临时覆盖, 管理端 workflow_config.json 持久化即时生效。
    "point_review_timeout_s": 600,
    # 视觉模型清单 (OCRImageRecognizeAgent 专用): 用于看门狗识别"非文本必需"模型
    "image_models": [
        "qwen3-vl:2b-instruct",
        "qwen3-vl:4b-instruct-q4_K_M",
    ],
    # 视觉模型较短 TTL: 图像识别为短突发, 结束后尽快释放显存避免与文本模型叠加死锁
    "image_keep_alive": "1m",
    # ─── 看门狗 (v5.2): 周期轮询 /api/ps 的 size_vram, 超阈值卸载非必需模型 ───
    "watchdog": {
        "enabled": True,                       # 看门狗总开关
        "interval_seconds": 30,                # 轮询 /api/ps 周期 (秒)
        "vram_threshold_gb": 3.5,              # size_vram 总和阈值 (GB)
        # 视为"流程必需"绝不自动卸载的模型 (空列表=所有模型均可被看门狗卸载)
        # 硬约束: T600 4GB 仅允许 ≤4B 模型稳定常驻; 双 4B 叠加 ≈5.4GB 视为死锁高风险
        "essential_models": [],
    },
}

# ─── 默认配置 ───
DEFAULT_WORKFLOW_CONFIG = {
    "max_concurrent_workflows": 2,        # 1-10
    "queue_timeout_seconds": 600,          # 60-3600 (10分钟)
    "human_checkpoint_timeouts": {
        # AI 团队工作流中使用的 4 个核心检查点 (可在管理端配置)
        "requirement_confirm": 180,        # 需求确认 3分钟
        "base_param_select": 180,           # 基准参数选定 3分钟
        "design_review": 180,              # 方案确认 3分钟
        "report_review": 600,              # 报告确认 10分钟
        # 案例电机审核 (较长)
        "case_motor_review": 1800,          # 案例入库审核 30分钟
    },
    "agent_model_map": {
        # 7 个使用 LLM 的智能体 + 1 个参数补全智能体 + 1 个 OCR 图像识别智能体 + DesignAgent(方案生成); 空字符串 = 使用默认
        "RequirementAgent": "",
        "ReviewAgent": "",
        "ReportAgent": "",
        "DevilsAdvocate": "",
        "RealityChecker": "",
        "ReverseReviewer": "",
        "SandboxSimulator": "",
        "ParamSuggestAgent": "",
        "OCRRecognizeAgent": "",
        "OCRImageRecognizeAgent": "qwen3-vl:2b-instruct",
        "DesignAgent": "",   # v4.02.39: 正名方案生成师智能体，纳入模型治理
        "IdeationAgent": "",  # v4.02.99 B: 补入模型治理 (原缺失键 → 恒走硬本地)
    },
    "default_model": _DEFAULT_MODEL,       # 全局默认模型 (只读展示)
    # v5.1: LLM 运行时治理 (num_ctx / keep_alive / image_keep_alive / watchdog)
    "llm_runtime": copy.deepcopy(LLM_RUNTIME_DEFAULT),
    # v4.01.038 (P0-2): 决策门质量门禁配置
    "quality_gate": {
        "confidence_threshold": 0.7,   # 评审置信度门禁阈值 (取自 review_feedback.confidence)
        "max_redo": 2,                  # 有限次方案重生上限 (未通过时自动重生, 超限转人工)
        "enforce_solver_available": True,  # P6: 质量门禁重生前校验求解器可用性, 不可用则转人工而非空耗重生
        "hard_constraints": {
            "efficiency_min": 70.0,      # 全局效率下限 (%, 0 = 不校验); 按类型命中时以 by_type 为准
            "temp_max": 120.0,           # 最佳方案最高温升 (°C, F级绝缘); 0 = 不校验
            # P0 (v4.02.67): 引用单一真相源常量 (原硬编码 0.75), 消除口径漂移
            "slot_fill_max": SLOT_FILL_MAX_DEFAULT,   # 最佳方案槽满率上限 (0~1); 0 = 不校验
            "efficiency_min_by_type": {  # 不同电机类型的合理效率下限 (子串匹配 motor_type)
                "永磁同步": 80.0,
                "异步": 75.0,
                "开关磁阻": 60.0,
            },
        },
        "convergence_fatal_keywords": [
            "致命", "严重冲突", "不可接受", "否决", "禁止",
            "不满足关键", "一票否决", "FATAL", "致命缺陷", "无法接受",
        ],
    },
}

# ─── 运行时状态 (线程安全) ───
_lock = threading.Lock()
_runtime_config: dict = {}
_override_timeouts: dict = {}
_agent_model_map: dict = {}
_max_concurrent: int = 2
_queue_timeout: int = 600


def load_config() -> dict:
    """从 JSON 文件加载配置; 文件不存在时用默认值并自动创建"""
    global _runtime_config, _override_timeouts, _agent_model_map, _max_concurrent, _queue_timeout

    # P0 (v4.02.67): 深拷贝默认配置 —— 原 dict() 浅拷贝使 config 与 DEFAULT 共享
    #   quality_gate/hard_constraints 等嵌套 dict, 后续 config[key].update(saved) 会
    #   就地污染 DEFAULT_WORKFLOW_CONFIG (实测: 持久化 0.80 把模块默认槽满率上限也改成 0.80,
    #   即"口径漂移"的隐藏成因)。深拷贝后默认值恒定, 单一真相源方才成立。
    config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    # v5.1: 深拷贝避免与模块级默认共享引用 (防止 merge 时污染默认配置)
    config["llm_runtime"] = copy.deepcopy(LLM_RUNTIME_DEFAULT)

    try:
        if os.path.exists(WORKFLOW_CONFIG_PATH):
            with open(WORKFLOW_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # 深度合并: 保留新增的默认字段
            for key in config:
                if key in saved:
                    if isinstance(config[key], dict):
                        config[key].update(saved[key])
                    else:
                        config[key] = saved[key]
            logger.info("[workflow_config] 已加载 %s", WORKFLOW_CONFIG_PATH)
        else:
            logger.info("[workflow_config] 配置文件不存在, 使用默认值")
    except Exception as e:
        logger.warning("[workflow_config] 加载失败 (%s), 使用默认值", e)

    with _lock:
        _runtime_config = config
        _override_timeouts = dict(config.get("human_checkpoint_timeouts", {}))
        _agent_model_map = dict(config.get("agent_model_map", {}))
        _max_concurrent = int(config.get("max_concurrent_workflows", 2))
        _queue_timeout = int(config.get("queue_timeout_seconds", 600))

    return config


def save_config(config: dict) -> None:
    """保存配置到 JSON 文件并更新运行时状态"""
    global _runtime_config, _override_timeouts, _agent_model_map, _max_concurrent, _queue_timeout

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(WORKFLOW_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    with _lock:
        _runtime_config = dict(config)
        _override_timeouts = dict(config.get("human_checkpoint_timeouts", {}))
        _agent_model_map = dict(config.get("agent_model_map", {}))
        _max_concurrent = int(config.get("max_concurrent_workflows", 2))
        _queue_timeout = int(config.get("queue_timeout_seconds", 600))

    # 同步环境变量 (供 orchestrator 初始化读取)
    os.environ["MOTOR_MAX_CONCURRENT"] = str(_max_concurrent)

    logger.info("[workflow_config] 配置已保存: max_concurrent=%d, queue_timeout=%d, "
                "checkpoint_overrides=%d, agent_map=%d",
                _max_concurrent, _queue_timeout,
                len(_override_timeouts), len(_agent_model_map))


def get_config() -> dict:
    """获取当前运行时配置的深拷贝 (如未加载则自动加载)"""
    import copy
    if not _runtime_config:
        load_config()
    with _lock:
        return copy.deepcopy(_runtime_config)


# ─── 检查点超时访问器 ───

def get_checkpoint_timeout(checkpoint_type: str, default: int = 3600) -> int:
    """获取检查点超时时间: 优先返回覆盖值, 否则返回 default"""
    with _lock:
        if checkpoint_type in _override_timeouts:
            return int(_override_timeouts[checkpoint_type])
    return default


def is_checkpoint_timeout_set(checkpoint_type: str) -> bool:
    """v4.02.110 (C): 判断管理端是否**显式配置**了该检查点超时。

    供 human_checkpoint.create_checkpoint 做优先级裁决: 只有管理端真的配过该 key
    (含 0 等 falsy 值) 时, 其配置值才应压过 orchestrator 硬传的内置默认 (DEFAULT_TIMEOUTS);
    未配置的 key 绝不该落 3600 兜底, 而应保留 orchestrator 传入的内置默认。
    """
    with _lock:
        return checkpoint_type in _override_timeouts


def set_override_timeouts(overrides: dict) -> None:
    """批量设置检查点超时覆盖"""
    with _lock:
        _override_timeouts = dict(overrides)


# ─── 智能体名归一化 (v4.02.99 A) ───
# 背景: 调用点写法不统一 — motor_tools._llm_gw_call 传 "design_agent"(小写下划线),
# 而 agent_model_map 的键是驼峰 "DesignAgent"; dict.get 严格匹配且无归一化,
# 导致同一智能体仅因写法不同被判为"未配置" → 静默落到本地 3B 模型 (v6 实测 25 次本地调用)。
_AGENT_NAME_ALIASES = {
    "design_agent": "DesignAgent",
    "ideation_agent": "IdeationAgent",
    "review_agent": "ReviewAgent",
    "requirement_agent": "RequirementAgent",
    "report_agent": "ReportAgent",
    "param_suggest_agent": "ParamSuggestAgent",
    "case_motor_agent": "CaseMotorAgent",
    "devils_advocate": "DevilsAdvocate",
    "reality_checker": "RealityChecker",
    "reverse_reviewer": "ReverseReviewer",
    "sandbox_simulator": "SandboxSimulator",
    "ocr_recognize_agent": "OCRRecognizeAgent",
    "ocr_image_recognize_agent": "OCRImageRecognizeAgent",
}


def _norm_agent_key(name: str) -> str:
    """归一化智能体名: 去大小写/下划线/连字符/空格 (design_agent ≡ DesignAgent)。"""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _lookup_agent_model(agent_name: str):
    """按智能体名查 agent_model_map: 精确 → 内置别名 → 归一化索引。

    本函数是 agent_model_map 的唯一读取入口 (调用方不要再直接 _agent_model_map.get)。
    找不到返回 None (= 未配置), 以区别于"配置了空字符串"(= 用默认)。
    内部自行持锁, 调用方不得再套 with _lock (Lock 非可重入, 会自死锁)。
    """
    with _lock:
        if agent_name in _agent_model_map:
            return _agent_model_map[agent_name]
        _alias = _AGENT_NAME_ALIASES.get(agent_name)
        if _alias and _alias in _agent_model_map:
            return _agent_model_map[_alias]
        _target = _norm_agent_key(agent_name)
        if _target:
            for _k, _v in _agent_model_map.items():
                if _norm_agent_key(_k) == _target:
                    return _v
    return None


# ─── 智能体模型访问器 ───

def get_agent_model(agent_name: str) -> str:
    """获取智能体使用的模型; 空字符串或不存在时返回默认模型"""
    # v4.02.99 A: 经归一化查找, 兼容 design_agent / DesignAgent 等写法差异
    model = _lookup_agent_model(agent_name) or ""
    if model:
        return model
    return os.getenv("LOCAL_MODEL_NAME", _DEFAULT_MODEL)


def get_vl_model() -> str:
    """获取 OCR 图像识别智能体 (多模态 VL) 使用的模型。

    与 get_agent_model 的区别: 当管理端把该智能体设为"使用默认模型"(空字符串)时,
    这里回退到 DEFAULT_VL_MODEL (视觉模型), 而不是全局文本默认模型,
    避免把 qwen2.5:3b 这类纯文本模型误用于图片识别。
    """
    with _lock:
        model = _agent_model_map.get("OCRImageRecognizeAgent", "")
    return model or DEFAULT_VL_MODEL


def get_vl_timeout() -> int:
    """VL 图像识别等待上限 (秒), 默认 300 (5 分钟)"""
    return DEFAULT_VL_TIMEOUT


# ─── 云端感知智能体 LLM 规格访问器 (H-3 二期 T2) ───
def get_agent_llm_spec(agent_name: str) -> dict:
    """返回智能体 LLM 规格, 供 Agent 选择 ChatOllama 或云端客户端。

    返回结构: {"backend": "local"|"cloud", "model": <纯模型名>}
      - backend="cloud": model 为云端目录模型名 (如 "qwen-max"), 调用方应使用
        Core.cloud_llm.get_cloud_client(model) 取得 OpenAI 兼容客户端。
      - backend="local" : model 为 Ollama 模型名; OCRImageRecognizeAgent 空值回退到
        DEFAULT_VL_MODEL 而非文本默认模型, 与 get_vl_model 行为一致。

    约定: agent_model_map 中云端模型以 "cloud:<model>" 前缀编码 (encode_cloud),
          本地模型为纯 Ollama 名, 空字符串表示使用默认。
    """
    # v4.02.99 A: 经归一化查找 (严格匹配会让 "design_agent" 漏配 → 误判本地)
    raw = _lookup_agent_model(agent_name) or ""
    if raw and raw.startswith(CLOUD_PREFIX):
        return {"backend": "cloud", "model": decode_cloud(raw)}
    if raw:
        # H-3 二期 E方案 / Fix Y-2: 裸名也可能是自定义云模型
        #   (如用户把 DeepSeek 卡 default_model 改成 agnes-3.0-flash,
        #   经 normalize_agent_model 因当时 is_cloud_model=False 未补 cloud: 前缀,
        #   磁盘落库为裸名). 此处直接按 is_cloud_model 判定路由云端,
        #   消化磁盘已有的裸名, 无需重存编码即可走云, 避免被误当本地 Ollama 名发 404.
        if is_cloud_model(raw):
            return {"backend": "cloud", "model": raw}
        return {"backend": "local", "model": raw}
    # 空字符串 -> 默认
    if agent_name == "OCRImageRecognizeAgent":
        return {"backend": "local", "model": get_vl_model()}
    return {"backend": "local", "model": os.getenv("LOCAL_MODEL_NAME", _DEFAULT_MODEL)}


def is_agent_cloud(agent_name: str) -> bool:
    """该智能体当前是否配置为云端模型。

    方案 B: 磁盘 agent_model_map 以 bare id 为单一真源 (不再依赖 cloud: 前缀),
    故按 is_cloud_model 判定; 同时兼容历史 cloud: 前缀写法。
    """
    # v4.02.99 A: 经归一化查找
    raw = _lookup_agent_model(agent_name) or ""
    if not raw:
        return False
    if raw.startswith(CLOUD_PREFIX):
        return True
    return is_cloud_model(raw)


def normalize_agent_model(value: str) -> str:
    """规整智能体模型条目为 bare id (方案 B: 磁盘 agent_model_map 以 id 为单一真源)。

    供 T3 的 sync_to_workflow_config 在写 agent_model_map 时使用, 保证口径一致:
      - 空字符串 -> ""
      - 已是 cloud: 前缀 -> 解开为 bare 再归一
      - 云端模型 (id 或 display name 如 Agnes-3.0-Flash-CN) -> 反查为规范 id (agnes-3.0-flash),
        不加 cloud: 前缀 (get_agent_llm_spec 已按 is_cloud_model 判定云端路由)
      - 本地 Ollama 名 -> 原样返回
    """
    if not value:
        return ""
    if value.startswith(CLOUD_PREFIX):
        value = decode_cloud(value)
    # 云端别名 (id 或 display name) 归一到规范 id
    if is_cloud_model(value):
        return _resolve_cloud_id(value)
    return value


def get_agent_cloud_client(agent_name: str):
    """便捷封装: 若该智能体配置为云端, 返回构造好的云端客户端; 否则返回 None。

    调用方应捕获 CloudLLMUnavailable 并降级到本地 Ollama。
    """
    spec = get_agent_llm_spec(agent_name)
    if spec["backend"] != "cloud":
        return None
    return get_cloud_client(spec["model"])


# ─── LLM 运行时治理访问器 (v5.1) ───
def get_llm_runtime() -> dict:
    """获取 LLM 运行时治理配置 (num_ctx / keep_alive / image_keep_alive / watchdog)。

    返回合并后的深拷贝, 未配置或读取失败时回退到 LLM_RUNTIME_DEFAULT,
    保证即使 workflow_config.json 缺失该段也不影响主流程。
    """
    try:
        cfg = get_config()
        rt = cfg.get("llm_runtime")
        if isinstance(rt, dict):
            merged = copy.deepcopy(LLM_RUNTIME_DEFAULT)
            merged.update({k: v for k, v in rt.items() if k != "watchdog"})
            saved_wd = rt.get("watchdog") or {}
            merged["watchdog"].update(saved_wd)
            return merged
    except Exception as e:
        logger.warning("[workflow_config] llm_runtime 读取失败, 用默认: %s", e)
    return copy.deepcopy(LLM_RUNTIME_DEFAULT)


def get_point_review_timeout() -> int:
    """点评级超时 (评审 LLM 整步护盾), 默认取自 llm_runtime.point_review_timeout_s (2000s)。

    可用环境变量 OLLAMA_REVIEW_TIMEOUT 临时覆盖。语义分层见 LLM_RUNTIME_DEFAULT 注释:
      调用级 request_timeout (单 HTTP 请求熔断) vs 点评级 point_review_timeout_s (整步预算上限)。
    """
    _env = os.getenv("OLLAMA_REVIEW_TIMEOUT")
    if _env:
        try:
            return int(_env)
        except Exception:
            pass
    try:
        return int(get_llm_runtime().get("point_review_timeout_s", 2000))
    except Exception:
        return 2000


# ─── 质量门禁配置访问器 (P0-2) ───
def get_quality_gate_config() -> dict:
    """获取决策门质量门禁配置 (含默认值合并), 供 orchestrator 调用

    返回结构见 quality_gate.DEFAULT_QUALITY_GATE。
    配置持久化在 workflow_config.json 的 quality_gate 段, 运行时修改即时生效。
    """
    try:
        from quality_gate import get_quality_gate_config as _qg
        return _qg(get_config())
    except Exception as e:  # 模块缺失时退回内置默认
        logger.warning("[workflow_config] 质量门禁配置读取失败, 用默认: %s", e)
        try:
            from quality_gate import DEFAULT_QUALITY_GATE
            return dict(DEFAULT_QUALITY_GATE)
        except Exception:
            return {
                "confidence_threshold": 0.7, "max_redo": 2,
                "hard_constraints": {"efficiency_min": 0.0, "temp_max": 0.0, "slot_fill_max": 0.0},
                "convergence_fatal_keywords": ["致命", "严重冲突", "不可接受", "否决", "禁止"],
            }


def set_agent_model_map(model_map: dict) -> None:
    """批量设置智能体模型映射

    v4.01.024 修复: 原实现缺少 global 声明, 赋值只作用于函数局部变量,
    导致运行时调用本函数完全无效 (静默空操作)。
    """
    global _agent_model_map
    with _lock:
        _agent_model_map = dict(model_map)


# ─── 并发/队列访问器 ───

def get_max_concurrent() -> int:
    """获取当前最大并发数"""
    with _lock:
        return _max_concurrent


def get_queue_timeout() -> int:
    """获取排队超时时间"""
    with _lock:
        return _queue_timeout


def set_max_concurrent(n: int) -> None:
    """运行时设置最大并发数 (供 API 直接调用)"""
    global _max_concurrent
    with _lock:
        _max_concurrent = max(1, min(10, int(n)))
        os.environ["MOTOR_MAX_CONCURRENT"] = str(_max_concurrent)


# ─── 初始化: 模块导入时自动加载 ───
load_config()
