"""
ai_flow_config.py - AI 流程设置集中配置层 (v4.00.013)

对应管理端「AI团队设置」界面 5 个标签页:
    requirement : 需求识别阶段   (RequirementAgent)
    design      : 方案生成阶段   (DesignAgent)
    review      : 方案评价阶段   (评审推荐师 + 收敛四剑客 + 量化评分权重模板)
    evolution   : 数据飞轮与进化 (进化引擎 / 运营分析师)
    resources   : 模型与资源控制 (Ollama / 云端 API / 显存保护)

设计要点:
  1. 单一真源 AI_FLOW_SCHEMA: 每个字段带 label/type/default/范围/选项/生效方式,
     默认配置与合法性校验均由 schema 派生, 避免 UI 与后端口径漂移。
  2. 持久化到 data/ai_flow_settings.json; 版本历史写 data/ai_flow_versions.json,
     每次保存生成 v1/v2/..., 仅保留最近 10 版, 支持一键回滚。
  3. 量化评分权重模板存 SQLite 表 scoring_weight_templates (预置 4 套模板)。
  4. 与既有 workflow_config.py 的重叠项做**单向同步** (ai_flow -> workflow_config),
     只覆盖明确映射的少数键, 绝不整体改写 workflow_config.json。

与 workflow_config.py 的分工:
  workflow_config : 并发/队列/检查点超时/智能体模型映射/LLM 运行时/质量门禁 (底层运行时)
  ai_flow_config  : 面向运营的流程策略参数 (管理端可视化配置)

v1.0  2026-08-20  初始版本 (对应参考方案《管理端"AI流程设置"界面完整方案》)
"""
import os
import json
import copy
import sqlite3
import threading
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# H-3 二期 T3: 云端模型编解码 / 门控
#   is_cloud_model / cloud_gated 来自统一工厂 Core.cloud_llm
#   normalize_agent_model 来自 Core.workflow_config (T2 已定义)
from cloud_llm import is_cloud_model, cloud_gated, list_cloud_models
from workflow_config import normalize_agent_model

logger = logging.getLogger(__name__)

# ─────────────────────────── 路径 ───────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
AI_FLOW_CONFIG_PATH = os.path.join(_DATA_DIR, "ai_flow_settings.json")
AI_FLOW_VERSIONS_PATH = os.path.join(_DATA_DIR, "ai_flow_versions.json")
MAX_VERSIONS = 10

DB_PATH = os.getenv("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

_DEFAULT_LOCAL_MODEL = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
_DEFAULT_CLOUD_MODEL = os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")
_LOCAL_4B_MODEL = os.getenv("LOCAL_MODEL_4B", "qwen3:4b")

# 云模型目录 (管理端「模型选择」下拉的云端候选)
# v4.02.07 H-3 二期 C方案 A: 单一真源改为 Core.cloud_llm.list_cloud_models()
#   (即 _VENDOR_REGISTRY 全 6 厂商 12 模型), 与 Agent 运行时 registry 永远一致,
#   新增厂商 / 模型无需再手动同步此处。
# 仍保留环境变量 CLOUD_MODEL_CATALOG 覆盖能力 (逗号分隔), 仅在未设置时回退 registry。
_CLOUD_CATALOG_ENV = os.getenv("CLOUD_MODEL_CATALOG", "")
CLOUD_MODEL_CATALOG = [m.strip() for m in _CLOUD_CATALOG_ENV.split(",") if m.strip()] or list(list_cloud_models())

# 生效方式常量
EFFECT_INSTANT = "instant"      # 即时生效 (运行时读取)
EFFECT_RESTART = "restart"      # 需重启服务 (Ollama / GPU 底层参数)
EFFECT_SCHEDULED = "scheduled"  # 定时生效 (下一执行周期)

SECTIONS = ["requirement", "design", "review", "evolution", "resources"]
SECTION_LABELS = {
    "requirement": "需求识别",
    "design": "方案生成",
    "review": "方案评价",
    "evolution": "数据飞轮",
    "resources": "模型与资源",
}

# ───────────────────── 配置 Schema (单一真源) ─────────────────────
# type: bool / int / float / str / enum / model
# enum 字段的 options 为 [(value, label), ...]
AI_FLOW_SCHEMA: Dict[str, Dict[str, Dict[str, Any]]] = {
    # ─────────── 3.1 需求识别阶段 ───────────
    "requirement": {
        "local_model": {
            "label": "本地模型", "type": "model", "default": _DEFAULT_LOCAL_MODEL,
            "agent": "RequirementAgent",
            "desc": "需求解析使用的主模型 (下拉取 Ollama 已安装列表)",
            "effect": EFFECT_INSTANT,
        },
        "cloud_review_enabled": {
            "label": "云端复核", "type": "bool", "default": False,
            "desc": "是否启用云端大模型二次解析", "effect": EFFECT_INSTANT,
        },
        "cloud_model": {
            "label": "云端模型", "type": "model", "default": _DEFAULT_CLOUD_MODEL,
            "desc": "复核使用的云端模型 (下拉含本地 Ollama + 云端目录; 需先启用云端复核)",
            "effect": EFFECT_INSTANT,
            "depends_on": "cloud_review_enabled",
        },
        "regex_preextract": {
            "label": "正则预提取", "type": "bool", "default": True,
            "desc": "是否启用正则预提取并注入提示词", "effect": EFFECT_INSTANT,
        },
        "forced_questions": {
            "label": "强制追问", "type": "bool", "default": True,
            "desc": "是否启用强制追问规则库", "effect": EFFECT_INSTANT,
        },
        "confidence_threshold": {
            "label": "置信度阈值", "type": "float", "default": 0.7,
            "min": 0.5, "max": 0.95, "step": 0.05,
            "desc": "低于此值触发二次校验或人工确认", "effect": EFFECT_INSTANT,
        },
        "recheck_trigger": {
            "label": "二次校验触发条件", "type": "enum", "default": "both",
            "options": [("missing_core", "核心参数缺失"), ("low_confidence", "置信度低"),
                        ("both", "两者任一")],
            "desc": "什么情况下自动二次识别", "effect": EFFECT_INSTANT,
        },
        "parse_timeout_seconds": {
            "label": "解析超时时间", "type": "int", "default": 60,
            "min": 10, "max": 120, "unit": "秒",
            "desc": "本地模型单次解析最大耗时", "effect": EFFECT_INSTANT,
        },
        "question_timeout_action": {
            "label": "追问超时策略", "type": "enum", "default": "hold_notify",
            "options": [("hold_notify", "挂起并通知"), ("auto_accept", "自动确认"),
                        ("auto_reject", "自动拒绝")],
            "desc": "人工确认超时后的动作", "effect": EFFECT_INSTANT,
        },
        "industry_lib_auto_update": {
            "label": "行业惯例库自动更新", "type": "bool", "default": False,
            "desc": "是否允许系统自动更新行业惯例库", "effect": EFFECT_SCHEDULED,
        },
    },

    # ─────────── 3.2 方案生成阶段 ───────────
    "design": {
        "default_scheme_count": {
            "label": "默认方案数量", "type": "int", "default": 30,
            "min": 5, "max": 100, "unit": "个",
            "desc": "每次任务生成的最大方案数", "effect": EFFECT_INSTANT,
        },
        "param_suggest_model": {
            "label": "参数补全模型", "type": "model", "default": "",
            "agent": "ParamSuggestAgent",
            "desc": "参数补全智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "solver_preference": {
            "label": "求解器选择", "type": "enum", "default": "rmxprt_first",
            "options": [("rmxprt_first", "RMxprt 优先"), ("motorcad", "Motor-CAD"),
                        ("simulate", "模拟模式")],
            "desc": "默认使用的批量求解工具", "effect": EFFECT_INSTANT,
        },
        "autonomous_mode": {
            "label": "自主模式", "type": "enum", "default": "semi",
            "options": [("semi", "半智能"), ("full", "全AI智能")],
            "desc": "半智能=LLM 仅作约束建议, 求解以确定性为主; 全AI智能=LLM 可更深入干预参数与排序, 仍不阻断 13 步流程",
            "effect": EFFECT_INSTANT,
        },
        "case_seed_enabled": {
            "label": "案例种子开关", "type": "bool", "default": True,
            "desc": "是否使用历史案例作为约束种子", "effect": EFFECT_INSTANT,
        },
        "ideation_llm_enabled": {
            "label": "方案构思 LLM", "type": "bool", "default": True,
            "desc": "是否启用 LLM 方案构思节点", "effect": EFFECT_INSTANT,
        },
        "diversity_mutation_ratio": {
            "label": "多样性扰动比例", "type": "int", "default": 10,
            "min": 0, "max": 30, "unit": "%",
            "desc": "参数矩阵中随机变异方案的占比", "effect": EFFECT_INSTANT,
        },
        "partial_redo_enabled": {
            "label": "局部分块重做", "type": "bool", "default": True,
            "desc": "评审反馈后是否只重做生成节点", "effect": EFFECT_INSTANT,
        },
        "d2l_coeff_water": {
            "label": "D²L 修正系数-水冷", "type": "float", "default": 1.3,
            "min": 0.5, "max": 1.5, "step": 0.05,
            "desc": "水冷时扭矩密度修正系数", "effect": EFFECT_INSTANT,
        },
        "d2l_coeff_air": {
            "label": "D²L 修正系数-风冷", "type": "float", "default": 1.0,
            "min": 0.5, "max": 1.5, "step": 0.05,
            "desc": "风冷时扭矩密度修正系数", "effect": EFFECT_INSTANT,
        },
        "frame_size_float_step": {
            "label": "机座号浮动档位", "type": "enum", "default": "1",
            "options": [("0", "0 档"), ("1", "±1 档"), ("2", "±2 档")],
            "desc": "细调层允许的最大机座号浮动", "effect": EFFECT_INSTANT,
        },
        "lambda_min": {
            "label": "长径比下限", "type": "float", "default": 0.8,
            "min": 0.1, "max": 5.0, "step": 0.1,
            "desc": "方案生成校验的长径比下限", "effect": EFFECT_INSTANT,
        },
        "lambda_max": {
            "label": "长径比上限", "type": "float", "default": 2.0,
            "min": 0.2, "max": 8.0, "step": 0.1,
            "desc": "方案生成校验的长径比上限", "effect": EFFECT_INSTANT,
        },
        "slot_fill_min": {
            "label": "槽满率下限", "type": "int", "default": 70,
            "min": 30, "max": 95, "unit": "%",
            "desc": "硬约束槽满率下限", "effect": EFFECT_INSTANT,
        },
        "slot_fill_max": {
            # P0 (v4.02.67): 默认 80→75, 与单一真相源 SLOT_FILL_MAX_DEFAULT(0.75) 对齐,
            #   消除"UI 默认 0.80 同步后把门禁抬到 0.80"的口径漂移。
            "label": "槽满率上限", "type": "int", "default": 75,
            "min": 35, "max": 98, "unit": "%",
            "desc": "硬约束槽满率上限 (同步到质量门禁 slot_fill_max)",
            "effect": EFFECT_INSTANT,
        },
    },

    # ─────────── 3.3 方案评价阶段 ───────────
    "review": {
        "coarse_top_n": {
            "label": "粗筛 Top 数量", "type": "int", "default": 10,
            "min": 5, "max": 30, "unit": "个",
            "desc": "量化评分后进入 AI 辅助评价的方案数", "effect": EFFECT_INSTANT,
        },
        "detail_top_n": {
            "label": "详细评审 Top 数量", "type": "int", "default": 5,
            "min": 3, "max": 10, "unit": "个",
            "desc": "最终进入详细评审的方案数", "effect": EFFECT_INSTANT,
        },
        "ai_review_chunk_size": {
            "label": "AI 辅助评价分块大小", "type": "int", "default": 4,
            "min": 1, "max": 10, "unit": "个/批",
            "desc": "每批评审方案数", "effect": EFFECT_INSTANT,
        },
        "review_model_tier": {
            "label": "评审模型", "type": "enum", "default": "local_3b",
            "options": [("local_3b", "本地 3B (安全)"), ("local_4b", "本地 4B (显存风险)"),
                        ("cloud", "云端")],
            "desc": "评审使用的模型档位; T600 4GB 下 4B 有死锁风险",
            "effect": EFFECT_INSTANT,
        },
        "convergence_mode": {
            "label": "收敛四剑客执行模式", "type": "enum", "default": "parallel",
            "options": [("parallel", "并行"), ("serial", "串行")],
            "desc": "三剑客 + 魔鬼代言人的执行方式", "effect": EFFECT_INSTANT,
        },
        "quick_scan_enabled": {
            "label": "收敛四剑客快速扫描", "type": "bool", "default": True,
            "desc": "是否在 Top10 阶段启用快速扫描", "effect": EFFECT_INSTANT,
        },
        "review_timeout_seconds": {
            "label": "评审超时时间", "type": "int", "default": 90,
            "min": 30, "max": 180, "unit": "秒",
            "desc": "单个评审智能体最大耗时", "effect": EFFECT_INSTANT,
        },
        "review_agent_model": {
            "label": "评审推荐师模型", "type": "model", "default": "",
            "agent": "ReviewAgent",
            "desc": "评审推荐师模型; 空=默认; 可选本地 Ollama 或云端目录 (取代旧评审模型档位)",
            "effect": EFFECT_INSTANT,
        },
        "devils_model": {
            "label": "魔鬼代言人模型", "type": "model", "default": "",
            "agent": "DevilsAdvocate",
            "desc": "魔鬼代言人智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "reality_model": {
            "label": "现实检验师模型", "type": "model", "default": "",
            "agent": "RealityChecker",
            "desc": "现实检验师智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "reverse_model": {
            "label": "逆向评审师模型", "type": "model", "default": "",
            "agent": "ReverseReviewer",
            "desc": "逆向评审师智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "sandbox_model": {
            "label": "沙盘推演师模型", "type": "model", "default": "",
            "agent": "SandboxSimulator",
            "desc": "沙盘推演师智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "active_weight_template": {
            "label": "当前权重模板", "type": "str", "default": "平衡型",
            "desc": "量化评分启用的权重模板名称", "effect": EFFECT_INSTANT,
        },
    },

    # ─────────── 3.4 数据飞轮与进化阶段 ───────────
    "evolution": {
        "flow_flywheel_threshold": {
            "label": "流程级飞轮触发阈值", "type": "int", "default": 80,
            "min": 50, "max": 100, "unit": "分",
            "desc": "单次任务质量评分低于该值触发即时优化", "effect": EFFECT_INSTANT,
        },
        "enterprise_update_cycle": {
            "label": "企业级飞轮更新周期", "type": "enum", "default": "weekly",
            "options": [("daily", "每天"), ("weekly", "每周"), ("monthly", "每月")],
            "desc": "全局知识库与规则库的更新频率", "effect": EFFECT_SCHEDULED,
        },
        "promote_project_to_line": {
            "label": "知识升级验证次数(项目→产品线)", "type": "int", "default": 3,
            "min": 1, "max": 10, "unit": "次",
            "desc": "项目级知识升级到产品线所需验证次数", "effect": EFFECT_INSTANT,
        },
        "promote_line_to_enterprise": {
            "label": "知识升级验证次数(产品线→企业级)", "type": "int", "default": 5,
            "min": 2, "max": 20, "unit": "次",
            "desc": "产品线知识升级到企业级所需验证次数", "effect": EFFECT_INSTANT,
        },
        "auto_update_forced_questions": {
            "label": "自动更新强制追问规则", "type": "bool", "default": True,
            "desc": "是否允许系统自动添加强制追问项", "effect": EFFECT_INSTANT,
        },
        "auto_optimize_prompt": {
            "label": "自动优化提示词", "type": "bool", "default": False,
            "desc": "是否允许系统自动优化提示词模板 (建议人工审核)",
            "effect": EFFECT_INSTANT,
        },
        "operation_report_freq": {
            "label": "运营报告生成频率", "type": "enum", "default": "per_task",
            "options": [("per_task", "每次任务后"), ("daily", "每天"), ("weekly", "每周")],
            "desc": "运营分析报告自动生成频率", "effect": EFFECT_SCHEDULED,
        },
        "feedback_clean_threshold": {
            "label": "反馈清洗阈值", "type": "float", "default": 0.8,
            "min": 0.0, "max": 1.0, "step": 0.05,
            "desc": "用户反馈过滤阈值", "effect": EFFECT_INSTANT,
        },
        "report_agent_model": {
            "label": "运营分析报告模型", "type": "model", "default": "",
            "agent": "ReportAgent",
            "desc": "运营分析报告智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
    },

    # ─────────── 3.5 模型与资源控制 ───────────
    "resources": {
        "ollama_num_parallel": {
            "label": "OLLAMA_NUM_PARALLEL", "type": "int", "default": 1,
            "min": 1, "max": 3,
            "desc": "Ollama 并行请求数", "effect": EFFECT_RESTART,
        },
        "ollama_max_loaded_models": {
            "label": "OLLAMA_MAX_LOADED_MODELS", "type": "int", "default": 1,
            "min": 1, "max": 2,
            "desc": "最大同时加载模型数", "effect": EFFECT_RESTART,
        },
        "model_cache_minutes": {
            # 参考方案默认 -1(常驻); 本实现按后端 v5.1 现状取 5 分钟:
            # T600 4GB 下模型常驻叠加视觉模型易触发显存死锁, 常驻仅建议大显存机器启用
            "label": "模型缓存时间", "type": "int", "default": 5,
            "min": -1, "max": 1440, "unit": "分钟",
            "desc": "Ollama 模型空闲释放时间; -1 = 常驻 (4GB 显存不建议常驻)",
            "effect": EFFECT_INSTANT,
        },
        "cloud_api_timeout": {
            "label": "云端 API 超时", "type": "int", "default": 30,
            "min": 10, "max": 120, "unit": "秒",
            "desc": "云端模型调用超时", "effect": EFFECT_INSTANT,
        },
        "gpu_enabled": {
            "label": "GPU 加速", "type": "bool", "default": True,
            "desc": "是否使用 GPU 推理", "effect": EFFECT_RESTART,
        },
        "vram_protect_percent": {
            "label": "显存保护阈值", "type": "int", "default": 90,
            "min": 70, "max": 95, "unit": "%",
            "desc": "显存占用超过该比例时拒绝新请求 / 触发看门狗卸载",
            "effect": EFFECT_INSTANT,
        },
        "gpu_total_gb": {
            "label": "显卡显存容量", "type": "float", "default": 4.0,
            "min": 1.0, "max": 48.0, "step": 0.5, "unit": "GB",
            "desc": "用于显存保护百分比换算 (T600 = 4GB)", "effect": EFFECT_INSTANT,
        },
        "ocr_text_model": {
            "label": "OCR 文本识别模型", "type": "model", "default": "",
            "agent": "OCRRecognizeAgent",
            "desc": "OCR 文本识别智能体模型; 空=默认; 可选本地 Ollama 或云端目录",
            "effect": EFFECT_INSTANT,
        },
        "ocr_image_model": {
            "label": "OCR 图像识别模型", "type": "model", "default": "qwen3-vl:2b-instruct",
            "agent": "OCRImageRecognizeAgent",
            "desc": "OCR 图像识别智能体 (多模态 VL) 模型; 默认 qwen3-vl:2b; 视觉模型请保持本地",
            "effect": EFFECT_INSTANT,
        },
    },
}

# ───────────────────── 量化评分权重模板 ─────────────────────
WEIGHT_KEYS: List[str] = [
    "efficiency", "temperature_rise", "torque_density", "cost",
    "power_factor", "lambda_reasonability", "manufacturability",
]
WEIGHT_LABELS = {
    "efficiency": "效率",
    "temperature_rise": "温升",
    "torque_density": "转矩密度",
    "cost": "成本估算",
    "power_factor": "功率因数",
    "lambda_reasonability": "长径比合理性",
    "manufacturability": "工艺可行性",
}

BUILTIN_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "平衡型", "is_default": 1, "description": "各指标较均衡, 通用场景",
        "weights": {"efficiency": 25, "temperature_rise": 20, "torque_density": 15,
                    "cost": 15, "power_factor": 10, "lambda_reasonability": 10,
                    "manufacturability": 5},
    },
    {
        "name": "效率优先", "is_default": 0, "description": "以效率为核心指标",
        "weights": {"efficiency": 35, "temperature_rise": 20, "torque_density": 15,
                    "cost": 10, "power_factor": 10, "lambda_reasonability": 5,
                    "manufacturability": 5},
    },
    {
        "name": "成本优先", "is_default": 0, "description": "成本敏感型客户/产品线",
        "weights": {"efficiency": 20, "temperature_rise": 15, "torque_density": 10,
                    "cost": 30, "power_factor": 10, "lambda_reasonability": 5,
                    "manufacturability": 10},
    },
    {
        "name": "温升敏感", "is_default": 0, "description": "散热受限或高防护等级场景",
        "weights": {"efficiency": 20, "temperature_rise": 35, "torque_density": 10,
                    "cost": 10, "power_factor": 10, "lambda_reasonability": 10,
                    "manufacturability": 5},
    },
]

# ───────────────────── 运行时状态 (线程安全) ─────────────────────
_lock = threading.RLock()
_runtime_config: Dict[str, Any] = {}


# ═══════════════════════ 默认值与校验 ═══════════════════════
def default_config() -> Dict[str, Any]:
    """由 schema 派生完整默认配置"""
    cfg: Dict[str, Any] = {}
    for section, fields in AI_FLOW_SCHEMA.items():
        cfg[section] = {key: copy.deepcopy(meta["default"]) for key, meta in fields.items()}
    return cfg


def get_schema() -> Dict[str, Any]:
    """返回可 JSON 序列化的 schema (供管理端渲染/校验提示)"""
    out: Dict[str, Any] = {"sections": [], "weight_keys": WEIGHT_KEYS,
                           "weight_labels": WEIGHT_LABELS}
    for section, fields in AI_FLOW_SCHEMA.items():
        items = []
        for key, meta in fields.items():
            item = {"key": key, "label": meta.get("label", key),
                    "type": meta.get("type", "str"), "default": meta.get("default"),
                    "desc": meta.get("desc", ""), "effect": meta.get("effect", EFFECT_INSTANT)}
            for extra in ("min", "max", "step", "unit", "depends_on", "agent"):
                if extra in meta:
                    item[extra] = meta[extra]
            if meta.get("options"):
                item["options"] = [{"value": v, "label": l} for v, l in meta["options"]]
            items.append(item)
        out["sections"].append({"key": section, "label": SECTION_LABELS.get(section, section),
                                "fields": items})
    return out


def _coerce(meta: Dict[str, Any], value: Any) -> Tuple[Any, Optional[str]]:
    """按 schema 类型强转并夹取范围; 返回 (值, 警告信息)"""
    ftype = meta.get("type", "str")
    default = meta.get("default")
    try:
        if ftype == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "开"), None
            return bool(value), None
        if ftype == "int":
            v = int(round(float(value)))
            lo, hi = meta.get("min"), meta.get("max")
            if lo is not None and v < lo:
                return lo, f"{meta.get('label')} 低于下限 {lo}, 已夹取"
            if hi is not None and v > hi:
                return hi, f"{meta.get('label')} 超过上限 {hi}, 已夹取"
            return v, None
        if ftype == "float":
            v = float(value)
            lo, hi = meta.get("min"), meta.get("max")
            if lo is not None and v < lo:
                return lo, f"{meta.get('label')} 低于下限 {lo}, 已夹取"
            if hi is not None and v > hi:
                return hi, f"{meta.get('label')} 超过上限 {hi}, 已夹取"
            return round(v, 4), None
        if ftype == "enum":
            allowed = [v for v, _ in meta.get("options", [])]
            sval = str(value)
            if sval not in allowed:
                return default, f"{meta.get('label')} 取值 {sval} 非法, 已回退默认 {default}"
            return sval, None
        # str / model
        sval = "" if value is None else str(value).strip()
        if not sval:
            return default, None
        return sval, None
    except Exception:
        return default, f"{meta.get('label')} 解析失败, 已回退默认"


def validate_config(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """合并默认值 + 类型强转 + 范围夹取 + 跨字段一致性校验

    返回 (干净配置, 警告列表)。始终返回可用配置, 不抛异常。
    """
    warnings: List[str] = []
    cfg = default_config()
    raw = raw if isinstance(raw, dict) else {}

    for section, fields in AI_FLOW_SCHEMA.items():
        incoming = raw.get(section) or {}
        if not isinstance(incoming, dict):
            warnings.append(f"[{SECTION_LABELS.get(section)}] 数据格式非法, 已整段回退默认")
            continue
        for key, meta in fields.items():
            if key in incoming:
                val, warn = _coerce(meta, incoming[key])
                cfg[section][key] = val
                if warn:
                    warnings.append(warn)
        # 未知字段告警 (不写入, 防止脏数据入库)
        for key in incoming:
            if key not in fields:
                warnings.append(f"[{SECTION_LABELS.get(section)}] 忽略未知字段 {key}")

    # ── 跨字段一致性 ──
    d = cfg["design"]
    if d["lambda_min"] >= d["lambda_max"]:
        warnings.append("长径比下限 ≥ 上限, 已重置为 0.8~2.0")
        d["lambda_min"], d["lambda_max"] = 0.8, 2.0
    if d["slot_fill_min"] >= d["slot_fill_max"]:
        # P0 (v4.02.67): 重置值随单一真相源 (上限 0.75 → 75%), 原 80% 与门禁口径冲突
        warnings.append("槽满率下限 ≥ 上限, 已重置为 70%~75%")
        d["slot_fill_min"], d["slot_fill_max"] = 70, 75

    r = cfg["review"]
    if r["detail_top_n"] > r["coarse_top_n"]:
        warnings.append("详细评审 Top 数量大于粗筛 Top 数量, 已按粗筛值夹取")
        r["detail_top_n"] = min(r["coarse_top_n"], AI_FLOW_SCHEMA["review"]["detail_top_n"]["max"])
    if r["ai_review_chunk_size"] > r["coarse_top_n"]:
        warnings.append("分块大小大于粗筛数量, 已按粗筛值夹取")
        r["ai_review_chunk_size"] = r["coarse_top_n"]

    e = cfg["evolution"]
    if e["promote_line_to_enterprise"] < e["promote_project_to_line"]:
        warnings.append("产品线→企业级验证次数小于项目→产品线, 已提升为同值")
        e["promote_line_to_enterprise"] = e["promote_project_to_line"]

    if cfg["requirement"]["cloud_review_enabled"] and not cfg["requirement"]["cloud_model"]:
        warnings.append("已启用云端复核但未填云端模型, 已回退默认云端模型")
        cfg["requirement"]["cloud_model"] = _DEFAULT_CLOUD_MODEL

    # ── 跨字段: 任一智能体选了云端模型但云端 API 被门控 -> 提示将降级 ──
    gate = cloud_gated()
    if gate is not None:
        for section, fields in AI_FLOW_SCHEMA.items():
            for key, meta in fields.items():
                if meta.get("agent") and meta.get("type") == "model":
                    val = (cfg.get(section, {}).get(key) or "").strip()
                    if val and (val.startswith("cloud:") or is_cloud_model(val)):
                        warnings.append(
                            f"[{SECTION_LABELS.get(section)}] {meta.get('label')} 已选云端模型"
                            f" '{val}', 但云端 API 被禁用 (reason={gate}), 运行时将降级到本地默认"
                        )

    return cfg, warnings


def validate_weights(weights: Dict[str, Any]) -> Tuple[Dict[str, int], List[str]]:
    """校验权重模板: 7 个键齐备、各 0~100、总和 100"""
    warnings: List[str] = []
    clean: Dict[str, int] = {}
    weights = weights if isinstance(weights, dict) else {}
    for key in WEIGHT_KEYS:
        try:
            v = int(round(float(weights.get(key, 0))))
        except Exception:
            v = 0
            warnings.append(f"{WEIGHT_LABELS[key]} 数值非法, 已按 0 处理")
        clean[key] = max(0, min(100, v))
    total = sum(clean.values())
    if total != 100:
        warnings.append(f"权重总和为 {total}%, 必须为 100%")
    return clean, warnings


# ═══════════════════════ 加载 / 保存 / 版本 ═══════════════════════
def load_config() -> Dict[str, Any]:
    """从 JSON 加载配置; 文件缺失时用默认值 (不自动落盘)"""
    global _runtime_config
    raw: Dict[str, Any] = {}
    try:
        if os.path.exists(AI_FLOW_CONFIG_PATH):
            with open(AI_FLOW_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            logger.info("[ai_flow_config] 已加载 %s", AI_FLOW_CONFIG_PATH)
        else:
            logger.info("[ai_flow_config] 配置文件不存在, 使用默认值")
    except Exception as e:
        logger.warning("[ai_flow_config] 加载失败 (%s), 使用默认值", e)

    cfg, warns = validate_config(raw)
    for w in warns:
        logger.warning("[ai_flow_config] %s", w)
    with _lock:
        _runtime_config = cfg
    return copy.deepcopy(cfg)


def get_config() -> Dict[str, Any]:
    """获取当前运行时配置的深拷贝 (未加载则自动加载)"""
    with _lock:
        if not _runtime_config:
            load_config()
        return copy.deepcopy(_runtime_config)


def get_section(section: str) -> Dict[str, Any]:
    """获取单段配置; section 非法时返回空 dict"""
    return get_config().get(section, {})


def diff_config(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    """逐字段比较, 输出变更清单 (供审计日志)"""
    changes: List[Dict[str, Any]] = []
    for section, fields in AI_FLOW_SCHEMA.items():
        o, n = (old or {}).get(section, {}) or {}, (new or {}).get(section, {}) or {}
        for key, meta in fields.items():
            ov, nv = o.get(key), n.get(key)
            if ov != nv:
                changes.append({
                    "section": section,
                    "section_label": SECTION_LABELS.get(section, section),
                    "key": key, "label": meta.get("label", key),
                    "old": ov, "new": nv,
                    "effect": meta.get("effect", EFFECT_INSTANT),
                })
    return changes


def _read_versions() -> Dict[str, Any]:
    try:
        if os.path.exists(AI_FLOW_VERSIONS_PATH):
            with open(AI_FLOW_VERSIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if isinstance(data.get("history"), list):
                return data
    except Exception as e:
        logger.warning("[ai_flow_config] 版本历史读取失败: %s", e)
    return {"current_version": 0, "history": []}


def _write_versions(data: Dict[str, Any]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = AI_FLOW_VERSIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AI_FLOW_VERSIONS_PATH)


def get_meta() -> Dict[str, Any]:
    """当前配置元信息: 版本号 / 最后修改时间 / 修改人 / 待重启标记"""
    data = _read_versions()
    hist = data.get("history", [])
    latest = hist[-1] if hist else {}
    return {
        "version": int(data.get("current_version", 0)),
        "version_label": f"v{int(data.get('current_version', 0))}",
        "last_modified": latest.get("timestamp", ""),
        "operator": latest.get("operator", ""),
        "note": latest.get("note", ""),
        "status": "已生效" if latest else "默认配置",
        "history_count": len(hist),
        "config_path": AI_FLOW_CONFIG_PATH,
    }


def save_config(raw: Dict[str, Any], operator: str = "admin",
                note: str = "", record_version: bool = True) -> Dict[str, Any]:
    """校验并保存配置, 生成新版本号, 返回保存结果

    返回: {config, warnings, changes, version, effects, sync}
    """
    global _runtime_config
    old = get_config()
    cfg, warnings = validate_config(raw)
    changes = diff_config(old, cfg)

    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = AI_FLOW_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AI_FLOW_CONFIG_PATH)

    with _lock:
        _runtime_config = copy.deepcopy(cfg)

    version = int(_read_versions().get("current_version", 0))
    if record_version:
        data = _read_versions()
        version = int(data.get("current_version", 0)) + 1
        data["current_version"] = version
        data.setdefault("history", []).append({
            "version": version,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": operator or "admin",
            "note": note or "",
            "changes": changes,
            "snapshot": copy.deepcopy(cfg),
        })
        # 仅保留最近 MAX_VERSIONS 版
        data["history"] = data["history"][-MAX_VERSIONS:]
        _write_versions(data)

    sync = sync_to_workflow_config(cfg)
    effects = classify_effects(changes)

    logger.info("[ai_flow_config] 配置已保存 v%d, 变更 %d 项, 警告 %d 条",
                version, len(changes), len(warnings))
    return {"config": cfg, "warnings": warnings, "changes": changes,
            "version": version, "effects": effects, "sync": sync}


def reset_config(operator: str = "admin") -> Dict[str, Any]:
    """恢复默认配置 (仍记录版本, 可回滚)"""
    return save_config(default_config(), operator=operator, note="恢复默认配置")


def list_versions() -> List[Dict[str, Any]]:
    """版本历史 (不含快照, 减小传输体积), 倒序返回"""
    data = _read_versions()
    out = []
    for rec in reversed(data.get("history", [])):
        out.append({
            "version": rec.get("version"),
            "timestamp": rec.get("timestamp", ""),
            "operator": rec.get("operator", ""),
            "note": rec.get("note", ""),
            "changes_count": len(rec.get("changes", [])),
            "changes": rec.get("changes", [])[:50],
        })
    return out


def rollback(version: int, operator: str = "admin") -> Dict[str, Any]:
    """回滚到指定版本 (回滚本身作为新版本记录, 保证链路可追溯)"""
    data = _read_versions()
    target = None
    for rec in data.get("history", []):
        if int(rec.get("version", -1)) == int(version):
            target = rec
            break
    if not target:
        raise ValueError(f"版本 v{version} 不存在或已被清理 (仅保留最近 {MAX_VERSIONS} 版)")
    snapshot = target.get("snapshot") or {}
    return save_config(snapshot, operator=operator, note=f"回滚到 v{version}")


def classify_effects(changes: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """按生效方式归类变更, 供管理端提示"重启 Ollama 后生效"等"""
    out: Dict[str, List[str]] = {EFFECT_INSTANT: [], EFFECT_RESTART: [], EFFECT_SCHEDULED: []}
    for c in changes or []:
        out.setdefault(c.get("effect", EFFECT_INSTANT), []).append(c.get("label", c.get("key")))
    return out


# ═══════════════════════ 单向同步到 workflow_config ═══════════════════════
def sync_to_workflow_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把重叠项单向同步进 workflow_config (ai_flow -> workflow_config)

    只覆盖明确映射的少数键, 其余保持原样, 失败不影响主流程。
    映射关系 (由 AI_FLOW_SCHEMA 中带 "agent" 键的 type=model 字段驱动, 全量 10 智能体):
      <section>.<model_field>(agent=X) -> agent_model_map.X (云端模型自动加 cloud: 前缀)
      design.slot_fill_max         -> quality_gate.hard_constraints.slot_fill_max (%/100)
      resources.model_cache_minutes-> llm_runtime.keep_alive ('-1' 或 'Nm')
      resources.vram_protect_*     -> llm_runtime.watchdog.vram_threshold_gb
      review.review_timeout_seconds-> (仅记录, 由评审节点运行时读取)
    注: 旧 review.review_model_tier 档位映射已废弃, 改由 review.review_agent_model 显式选择。
    """
    applied: List[str] = []
    try:
        import workflow_config as wc
    except Exception:
        try:
            from Core import workflow_config as wc  # type: ignore
        except Exception as e:
            logger.warning("[ai_flow_config] workflow_config 不可用, 跳过同步: %s", e)
            return {"ok": False, "applied": [], "error": str(e)}

    try:
        wcfg = wc.get_config()
        amap = dict(wcfg.get("agent_model_map", {}))

        # ── 通用: 所有带 agent 键的 model 字段 -> agent_model_map ──
        for section, fields in AI_FLOW_SCHEMA.items():
            for key, meta in fields.items():
                agent = meta.get("agent")
                if not agent or meta.get("type") != "model":
                    continue
                raw_val = (cfg.get(section, {}).get(key) or "").strip()
                encoded = normalize_agent_model(raw_val)  # 云端模型加 cloud: 前缀
                if amap.get(agent) != encoded:
                    amap[agent] = encoded
                    applied.append(f"agent_model_map.{agent}={encoded or '(默认)'}")

        wcfg["agent_model_map"] = amap

        qg = dict(wcfg.get("quality_gate", {}))
        hc = dict(qg.get("hard_constraints", {}))
        # P0 (v4.02.67): 缺省回退 80→75, 与单一真相源 SLOT_FILL_MAX_DEFAULT 对齐
        sf = float(cfg.get("design", {}).get("slot_fill_max", 75)) / 100.0
        if abs(float(hc.get("slot_fill_max", 0)) - sf) > 1e-6:
            hc["slot_fill_max"] = round(sf, 4)
            applied.append(f"quality_gate.hard_constraints.slot_fill_max={hc['slot_fill_max']}")
        qg["hard_constraints"] = hc
        wcfg["quality_gate"] = qg

        rt = dict(wcfg.get("llm_runtime", {}))
        cache_min = int(cfg.get("resources", {}).get("model_cache_minutes", -1))
        keep_alive = "-1" if cache_min < 0 else f"{cache_min}m"
        if rt.get("keep_alive") != keep_alive:
            rt["keep_alive"] = keep_alive
            applied.append(f"llm_runtime.keep_alive={keep_alive}")
        wd = dict(rt.get("watchdog", {}))
        total = float(cfg.get("resources", {}).get("gpu_total_gb", 4.0))
        pct = float(cfg.get("resources", {}).get("vram_protect_percent", 90))
        thr = round(total * pct / 100.0, 2)
        if abs(float(wd.get("vram_threshold_gb", 0)) - thr) > 1e-6:
            wd["vram_threshold_gb"] = thr
            applied.append(f"llm_runtime.watchdog.vram_threshold_gb={thr}")
        rt["watchdog"] = wd
        wcfg["llm_runtime"] = rt

        if applied:
            wc.save_config(wcfg)
            try:
                wc.set_agent_model_map(amap)
            except Exception:
                pass
        return {"ok": True, "applied": applied}
    except Exception as e:
        logger.warning("[ai_flow_config] 同步 workflow_config 失败: %s", e)
        return {"ok": False, "applied": applied, "error": str(e)}


# ═══════════════════════ 权重模板 (SQLite) ═══════════════════════
def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_scoring_tables() -> None:
    """创建 scoring_weight_templates 表并预置 4 套模板 (幂等)"""
    dirname = os.path.dirname(DB_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scoring_weight_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                weights TEXT NOT NULL,
                description TEXT DEFAULT '',
                is_default INTEGER DEFAULT 0,
                is_builtin INTEGER DEFAULT 0,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_swt_default "
                     "ON scoring_weight_templates(is_default)")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for tpl in BUILTIN_TEMPLATES:
            row = conn.execute("SELECT id FROM scoring_weight_templates WHERE name=?",
                               (tpl["name"],)).fetchone()
            if row:
                continue
            conn.execute(
                "INSERT INTO scoring_weight_templates "
                "(name, weights, description, is_default, is_builtin, created_at, updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (tpl["name"], json.dumps(tpl["weights"], ensure_ascii=False),
                 tpl.get("description", ""), int(tpl.get("is_default", 0)), now, now))
        conn.commit()
    logger.info("[ai_flow_config] scoring_weight_templates 就绪 (%s)", DB_PATH)


def _row_to_template(row) -> Dict[str, Any]:
    try:
        weights = json.loads(row["weights"]) or {}
    except Exception:
        weights = {}
    clean, _ = validate_weights(weights)
    return {
        "id": row["id"], "name": row["name"], "weights": clean,
        "description": row["description"] or "",
        "is_default": bool(row["is_default"]), "is_builtin": bool(row["is_builtin"]),
        "created_at": row["created_at"] or "", "updated_at": row["updated_at"] or "",
        "total": sum(clean.values()),
    }


def list_templates() -> List[Dict[str, Any]]:
    """列出全部权重模板 (默认模板置顶)"""
    init_scoring_tables()
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM scoring_weight_templates "
                            "ORDER BY is_default DESC, is_builtin DESC, id ASC").fetchall()
    return [_row_to_template(r) for r in rows]


def get_template(name: str) -> Optional[Dict[str, Any]]:
    init_scoring_tables()
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM scoring_weight_templates WHERE name=?",
                           (name,)).fetchone()
    return _row_to_template(row) if row else None


def create_template(name: str, weights: Dict[str, Any], description: str = "",
                    set_default: bool = False) -> Dict[str, Any]:
    """新建模板 (含另存为/复制); 权重总和必须为 100%"""
    name = (name or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    clean, warns = validate_weights(weights)
    if sum(clean.values()) != 100:
        raise ValueError(f"权重总和必须为 100%, 当前 {sum(clean.values())}%")
    init_scoring_tables()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_conn() as conn:
        exists = conn.execute("SELECT id FROM scoring_weight_templates WHERE name=?",
                              (name,)).fetchone()
        if exists:
            raise ValueError(f"模板『{name}』已存在")
        conn.execute(
            "INSERT INTO scoring_weight_templates "
            "(name, weights, description, is_default, is_builtin, created_at, updated_at) "
            "VALUES (?,?,?,0,0,?,?)",
            (name, json.dumps(clean, ensure_ascii=False), description, now, now))
        conn.commit()
    if set_default:
        set_default_template(name)
    return {"name": name, "weights": clean, "warnings": warns}


def update_template(name: str, weights: Optional[Dict[str, Any]] = None,
                    new_name: Optional[str] = None,
                    description: Optional[str] = None) -> Dict[str, Any]:
    """更新模板权重 / 重命名 / 改描述 (内置模板允许改权重, 不允许重命名)"""
    init_scoring_tables()
    tpl = get_template(name)
    if not tpl:
        raise ValueError(f"模板『{name}』不存在")
    sets, args = [], []
    warns: List[str] = []
    if weights is not None:
        clean, warns = validate_weights(weights)
        if sum(clean.values()) != 100:
            raise ValueError(f"权重总和必须为 100%, 当前 {sum(clean.values())}%")
        sets.append("weights=?")
        args.append(json.dumps(clean, ensure_ascii=False))
    if description is not None:
        sets.append("description=?")
        args.append(description)
    target_name = name
    if new_name and new_name.strip() and new_name.strip() != name:
        if tpl["is_builtin"]:
            raise ValueError("内置模板不允许重命名")
        if get_template(new_name.strip()):
            raise ValueError(f"模板『{new_name.strip()}』已存在")
        target_name = new_name.strip()
        sets.append("name=?")
        args.append(target_name)
    if not sets:
        return {"name": name, "changed": False}
    sets.append("updated_at=?")
    args.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    args.append(name)
    with _get_conn() as conn:
        conn.execute(f"UPDATE scoring_weight_templates SET {', '.join(sets)} WHERE name=?", args)
        conn.commit()
    # 重命名后若配置里引用了旧名, 同步更新引用
    if target_name != name:
        cfg = get_config()
        if cfg.get("review", {}).get("active_weight_template") == name:
            cfg["review"]["active_weight_template"] = target_name
            save_config(cfg, operator="system", note=f"模板重命名 {name}→{target_name}")
    return {"name": target_name, "changed": True, "warnings": warns}


def delete_template(name: str) -> Dict[str, Any]:
    """删除模板 (内置模板与当前默认模板禁止删除)"""
    init_scoring_tables()
    tpl = get_template(name)
    if not tpl:
        raise ValueError(f"模板『{name}』不存在")
    if tpl["is_builtin"]:
        raise ValueError("内置模板不允许删除")
    if tpl["is_default"]:
        raise ValueError("默认模板不允许删除, 请先设置其它模板为默认")
    with _get_conn() as conn:
        conn.execute("DELETE FROM scoring_weight_templates WHERE name=?", (name,))
        conn.commit()
    cfg = get_config()
    if cfg.get("review", {}).get("active_weight_template") == name:
        fallback = (get_default_template() or {}).get("name", "平衡型")
        cfg["review"]["active_weight_template"] = fallback
        save_config(cfg, operator="system", note=f"模板删除后回退到 {fallback}")
    return {"name": name, "deleted": True}


def set_default_template(name: str) -> Dict[str, Any]:
    init_scoring_tables()
    if not get_template(name):
        raise ValueError(f"模板『{name}』不存在")
    with _get_conn() as conn:
        conn.execute("UPDATE scoring_weight_templates SET is_default=0")
        conn.execute("UPDATE scoring_weight_templates SET is_default=1 WHERE name=?", (name,))
        conn.commit()
    return {"name": name, "is_default": True}


def get_default_template() -> Optional[Dict[str, Any]]:
    for t in list_templates():
        if t["is_default"]:
            return t
    tpls = list_templates()
    return tpls[0] if tpls else None


def get_active_weights() -> Dict[str, int]:
    """量化评分实际使用的权重: 优先 review.active_weight_template, 否则默认模板"""
    name = get_section("review").get("active_weight_template", "")
    tpl = get_template(name) if name else None
    if not tpl:
        tpl = get_default_template()
    if tpl:
        return dict(tpl["weights"])
    return dict(BUILTIN_TEMPLATES[0]["weights"])


# ═══════════════════════ 运行时便捷访问器 ═══════════════════════
def get_requirement_config() -> Dict[str, Any]:
    return get_section("requirement")


def get_design_config() -> Dict[str, Any]:
    return get_section("design")


def get_review_config() -> Dict[str, Any]:
    return get_section("review")


def get_evolution_config() -> Dict[str, Any]:
    return get_section("evolution")


def get_resources_config() -> Dict[str, Any]:
    return get_section("resources")


def export_bundle() -> Dict[str, Any]:
    """导出配置包 (供管理端"导出配置"): 配置 + 元信息 + 模板"""
    try:
        templates = list_templates()
    except Exception as e:
        logger.warning("[ai_flow_config] 导出模板失败: %s", e)
        templates = []
    return {
        "kind": "motorai_ai_flow_settings",
        "schema_version": 1,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": get_meta(),
        "config": get_config(),
        "templates": templates,
    }


def import_bundle(bundle: Dict[str, Any], operator: str = "admin") -> Dict[str, Any]:
    """导入配置包: 校验后保存配置, 并补齐缺失的自定义模板"""
    bundle = bundle if isinstance(bundle, dict) else {}
    cfg_raw = bundle.get("config") if isinstance(bundle.get("config"), dict) else bundle
    result = save_config(cfg_raw, operator=operator, note="导入配置")
    imported, skipped = [], []
    for tpl in bundle.get("templates") or []:
        try:
            name = (tpl.get("name") or "").strip()
            if not name:
                continue
            if get_template(name):
                skipped.append(name)
                continue
            create_template(name, tpl.get("weights") or {}, tpl.get("description", ""))
            imported.append(name)
        except Exception as e:
            skipped.append(f"{tpl.get('name')}({e})")
    result["templates_imported"] = imported
    result["templates_skipped"] = skipped
    return result


# ─── 初始化: 模块导入时加载配置 (不落盘, 不建表) ───
load_config()


# ═══════════════════════ 自测 ═══════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("=" * 68)
    print("ai_flow_config 自测")
    print("=" * 68)

    d = default_config()
    assert set(d.keys()) == set(SECTIONS), d.keys()
    total_fields = sum(len(v) for v in d.values())
    print(f"[1] 默认配置 5 段 / {total_fields} 字段 OK")
    for s in SECTIONS:
        print(f"    - {SECTION_LABELS[s]:<12} {len(d[s])} 项")

    # 范围夹取 + 非法枚举
    bad = copy.deepcopy(d)
    bad["requirement"]["confidence_threshold"] = 9.9
    bad["requirement"]["recheck_trigger"] = "xxx"
    bad["design"]["default_scheme_count"] = 999
    bad["design"]["lambda_min"] = 3.0
    bad["design"]["lambda_max"] = 1.0
    bad["review"]["detail_top_n"] = 10
    bad["review"]["coarse_top_n"] = 5
    bad["zzz"] = {"a": 1}
    clean, warns = validate_config(bad)
    assert clean["requirement"]["confidence_threshold"] == 0.95
    assert clean["requirement"]["recheck_trigger"] == "both"
    assert clean["design"]["default_scheme_count"] == 100
    assert (clean["design"]["lambda_min"], clean["design"]["lambda_max"]) == (0.8, 2.0)
    assert clean["review"]["detail_top_n"] <= clean["review"]["coarse_top_n"]
    print(f"[2] 校验/夹取 OK, 警告 {len(warns)} 条")
    for w in warns[:6]:
        print(f"    ! {w}")

    # 权重模板
    init_scoring_tables()
    tpls = list_templates()
    names = [t["name"] for t in tpls]
    assert len(tpls) >= 4, names
    for t in tpls:
        assert t["total"] == 100, (t["name"], t["total"])
    print(f"[3] 权重模板 {len(tpls)} 套, 总和均 100%: {names}")
    print(f"    默认模板: {(get_default_template() or {}).get('name')}")

    tmp_name = "__自测模板__"
    if get_template(tmp_name):
        delete_template(tmp_name)
    w = {"efficiency": 40, "temperature_rise": 20, "torque_density": 10,
         "cost": 10, "power_factor": 10, "lambda_reasonability": 5,
         "manufacturability": 5}
    create_template(tmp_name, w, "自测用")
    assert get_template(tmp_name)["weights"]["efficiency"] == 40
    try:
        create_template("__非法__", {"efficiency": 10})
        raise AssertionError("总和校验未生效")
    except ValueError as e:
        print(f"[4] 总和非法拦截 OK: {e}")
    update_template(tmp_name, description="自测改描述")
    delete_template(tmp_name)
    assert get_template(tmp_name) is None
    print("[5] 模板 CRUD OK")

    # 保存 / 版本 / 回滚 (改动后还原)
    orig = get_config()
    orig_meta = get_meta()
    r1 = save_config(orig, operator="selftest", note="基线")
    base_ver = r1["version"]
    mod = copy.deepcopy(orig)
    mod["design"]["default_scheme_count"] = 42
    mod["review"]["coarse_top_n"] = 12
    r2 = save_config(mod, operator="selftest", note="自测改动")
    assert get_config()["design"]["default_scheme_count"] == 42
    print(f"[6] 保存 v{r2['version']}, 变更 {len(r2['changes'])} 项, "
          f"同步 workflow_config: {r2['sync'].get('applied')}")
    vs = list_versions()
    print(f"[7] 版本历史 {len(vs)} 条 (最多保留 {MAX_VERSIONS}), 最新 v{vs[0]['version']}")
    rb = rollback(base_ver, operator="selftest")
    assert get_config()["design"]["default_scheme_count"] == orig["design"]["default_scheme_count"]
    print(f"[8] 回滚到 v{base_ver} OK -> 新版本 v{rb['version']}")

    print(f"[9] 生效分类: {classify_effects(r2['changes'])}")
    print(f"[10] 激活权重: {get_active_weights()}")
    b = export_bundle()
    print(f"[11] 导出包: config {len(b['config'])} 段 / 模板 {len(b['templates'])} 套")
    print(f"[12] 元信息: {get_meta()}")
    print(f"     原元信息: v{orig_meta['version']} @ {orig_meta['last_modified'] or '-'}")
    print("=" * 68)
    print("全部自测通过")
