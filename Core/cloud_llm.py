"""
cloud_llm.py - 多厂商云端 LLM 统一工厂 (v4.02.06, H-3 二期 T1 地基)

职责 (单一、低风险、不触碰任何 Agent 运行时推理链路):
  1. 维护 12 个云模型 -> 6 厂商路由表 (base_url + API key 环境变量)。
  2. get_cloud_client(model) 返回协议感知客户端 wrapper (_ProtocolAwareClient):
     对外维持 .chat.completions.create 接口, 内部按 endpoint (chat / completions) 派发。
  3. 云端门控: 试用版强制关闭; ALLOW_CLOUD_API=false 时关闭; 缺真实密钥抛 CloudLLMUnavailable。
  4. 工具函数: is_cloud_model / list_cloud_models / encode_cloud / decode_cloud / cloud_gated。

设计约束:
  - 纯后端新模块, 不 import 任何 Agent / orchestrator / config.env (避免循环依赖与导入副作用)。
  - openai 包在 get_cloud_client 内惰性导入, 即使未安装也不影响模块导入与静态校验。
  - 门控逻辑与 config.env.Config.ALLOW_CLOUD_API 语义保持一致 (trial 禁用 + 环境变量开关),
    但不直接 import config.env, 改用等价的环境变量读取, 保证零耦合。
  - 现有 3 个硬编码云端 Agent (report/requirement/review_agent) 将在 T4 统一改走本工厂,
    其余 6 个 Agent 在 T5 接入; 本文件仅为它们提供底层客户端能力。

与 workflow_config (T2) 的分工:
  workflow_config.agent_model_map 用 "cloud:<model>" 前缀编码云端意图,
  其 get_agent_llm_spec() 会 decode_cloud() 取出纯模型名再调用本模块 get_cloud_client()。
  因此前缀编解码常量 (CLOUD_PREFIX / encode_cloud / decode_cloud) 以本模块为权威来源。
"""

import os
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────── 异常 ───────────────────────────
class CloudLLMUnavailable(Exception):
    """云端 LLM 不可用: 被门控禁用 / 缺密钥 / 未知模型 / openai 未安装。

    调用方 (Agent) 应捕获此异常并降级到本地 Ollama, 而非中断工作流。
    """


# ───────────────────── 厂商注册表 (12 模型 -> 6 厂商) ─────────────────────
# 每个厂商: 默认 base_url + 可被环境变量覆盖的 base_url + API key 环境变量。
# 所有端点均为 OpenAI 兼容 (/v1/chat/completions), 可用 openai.OpenAI 直连。
_VENDOR_REGISTRY: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": "deepseek-chat,deepseek-reasoner",
    },
    "dashscope": {  # 阿里云百炼 (Qwen)
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "api_key_env": "DASHSCOPE_API_KEY",
        "models": "qwen-max,qwen-plus",
    },
    "zhipu": {  # 智谱 AI (GLM)
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "base_url_env": "ZHIPU_BASE_URL",
        "api_key_env": "ZHIPU_API_KEY",
        "models": "glm-4-plus,glm-4",
    },
    "moonshot": {  # 月之暗面
        "base_url": "https://api.moonshot.cn/v1",
        "base_url_env": "MOONSHOT_BASE_URL",
        "api_key_env": "MOONSHOT_API_KEY",
        "models": "moonshot-v1-8k",
    },
    "minimax": {  # MiniMax (abab)
        "base_url": "https://api.minimax.chat/v1",
        "base_url_env": "MINIMAX_BASE_URL",
        "api_key_env": "MINIMAX_API_KEY",
        "models": "abab6.5s-chat",
    },
    "openai": {  # OpenAI (GPT-4o 系列) — H-3 二期修复 gpt-4o 目录坑
        "base_url": "https://api.openai.com/v1",
        "base_url_env": "OPENAI_BASE_URL",
        "api_key_env": "OPENAI_API_KEY",
        "models": "gpt-4o,gpt-4o-mini,gpt-4-turbo,gpt-4",
    },
}

# 模型名 -> 厂商名 反向索引 (构建一次)
MODEL_TO_VENDOR: Dict[str, str] = {}
for _vname, _vinfo in _VENDOR_REGISTRY.items():
    for _m in _vinfo["models"].split(","):
        MODEL_TO_VENDOR[_m.strip()] = _vname

# 占位密钥特征: 这些被视为"未配置真实密钥"
_PLACEHOLDER_KEYS = {"", "sk-your-key-here", "your-key-here", "none", "null"}

# 云端意图前缀 (workflow_config.agent_model_map 编码用, 本模块为权威来源)
CLOUD_PREFIX = "cloud:"


# ─────────────────────────── 工具函数 ───────────────────────────
def _cloud_alias_index() -> Dict[str, Tuple[str, str]]:
    """构建云端模型别名索引: ``{别名小写: (厂商名, 有效请求模型名)}``。

    覆盖每个启用厂商卡的:
      - ``default_model`` (真实请求模型名, 如 ``Agnes-3.0-Flash-CN``)
      - ``display_models`` 的键 (UI 下拉项, 如 ``agnes-3.0-flash``)
      - ``display_models`` 的值 (真实模型名别名, 如 ``Agnes-3.0-Flash-CN``)
    无论 Agent 存的是 display_models 键还是值, 都能解析到正确厂商与有效模型名。
    有效请求模型名优先取卡片 ``default_model`` (真实 id), 回退到匹配到的 display_models 值。

    v4.02.07 Fix Y 修正: 原实现仅匹配 ``default_model``, 但 Agent 实际存的是
    display_models 键 (小写 ``agnes-3.0-flash``), 而卡片 ``default_model`` 是
    Title-Case 的 ``Agnes-3.0-Flash-CN``, 导致 is_cloud_model 误判本地 -> 404。
    现统一按别名索引 (大小写不敏感) 匹配, 彻底打通"下拉框选 Agnes 真能走云端"。
    惰性导入 cloud_services_config 避免循环依赖; 异常时返回空索引 (保守判本地)。
    """
    idx: Dict[str, Tuple[str, str]] = {}
    try:
        try:
            from Core.cloud_services_config import get_all
        except Exception:
            from cloud_services_config import get_all  # type: ignore
        data = get_all()
        for vname, v in (data.get("vendors") or {}).items():
            if not v.get("enabled", True):
                continue
            dm = (v.get("default_model") or "").strip()
            if dm:
                idx[dm.lower()] = (vname, dm)
            for key, val in (v.get("display_models") or {}).items():
                key = (key or "").strip()
                val = (val or "").strip()
                if key:
                    idx[key.lower()] = (vname, val or dm)
                # B-3 (方案 B): 同时收录 display_models 的值 (显示名, 如 Agnes-3.0-Flash-CN),
                #   使 is_cloud_model("Agnes-3.0-Flash-CN")=True, 避免把显示名误判为本地 Ollama 名。
                if val:
                    idx[val.lower()] = (vname, val or dm)
    except Exception as e:  # pragma: no cover - 配置读取异常时返回空索引
        logger.warning("[cloud_llm] 构建云端别名索引失败: %s", e)
    return idx


def is_cloud_model(model: str) -> bool:
    """判断一个纯模型名是否属于已知云端目录 (注册表 + 自定义卡片别名)。

    v4.02.07 H-3 E方案 / Fix Y-1: 扩展识别范围 —— 除 12 个注册表标准云模型外,
    也识别用户在各厂商卡片配置的自定义云模型名 (default_model 或 display_models
    键/值, 如 DeepSeek 卡 display_models 键 ``agnes-3.0-flash`` 指向
    ``Agnes-3.0-Flash-CN``). 下拉框选中的自定义云模型因此能被路由到云端, 而非被
    误判为本地 Ollama 名导致 404.
    自定义部分惰性读取 cloud_services_config 构建别名索引, 避免循环依赖。
    """
    if not model:
        return False
    if model in MODEL_TO_VENDOR:
        return True
    try:
        return model.lower() in _cloud_alias_index()
    except Exception as e:  # pragma: no cover - 配置读取异常时保守判本地
        logger.warning("[cloud_llm] is_cloud_model 扩展识别失败, 回退仅注册表: %s", e)
        return False


def _reverse_lookup_vendor_by_default_model(model: str) -> Optional[str]:
    """注册表未命中兜底: 在云服务配置别名索引中找匹配 model 的厂商卡。

    v4.02.07 H-3 E方案 / Fix Y-3 (修正): 自定义云模型不在 MODEL_TO_VENDOR 注册表,
    但实为某厂商卡的 default_model 或 display_models 键/值别名. 通过 _cloud_alias_index
    匹配 (大小写不敏感), 反向查到厂商名后 get_cloud_client 即可按该卡的密钥/端点/协议
    路由, 解决"下拉框选 Agnes 真能走云端". 找不到或异常时返回 None.
    """
    if not model:
        return None
    try:
        entry = _cloud_alias_index().get(model.lower())
        return entry[0] if entry else None
    except Exception as e:  # pragma: no cover
        logger.warning("[cloud_llm] 反向查卡失败: %s", e)
        return None


def list_cloud_models() -> List[str]:
    """返回全部已知云端模型名 (供管理端 UI / 目录合并使用)。"""
    return list(MODEL_TO_VENDOR.keys())


def _resolve_cloud_id(model: str) -> str:
    """把云端模型的任意别名 (id 或 display name) 归一到规范 id
    (display_models 键 / 厂商卡片 default_model)。

    v4.02.07 方案 B: 用于 UI 落库前把 display name 反查为 id,
    保证磁盘 agent_model_map 以 id 为单一真源 (如 "Agnes-3.0-Flash-CN" -> "agnes-3.0-flash")。
    非云端 / 未匹配 -> 原样返回。
    """
    if not model:
        return model
    m = model.lower()
    if m in MODEL_TO_VENDOR:
        return model  # 注册表标准 id, 已是规范 id
    try:
        try:
            from Core.cloud_services_config import get_all
        except Exception:
            from cloud_services_config import get_all  # type: ignore
        data = get_all()
        for vname, v in (data.get("vendors") or {}).items():
            if not v.get("enabled", True):
                continue
            dm = (v.get("default_model") or "").strip()
            for key, val in (v.get("display_models") or {}).items():
                key = (key or "").strip()
                val = (val or "").strip()
                if key and m == key.lower():
                    return key          # 已是规范 id
                if val and m == val.lower():
                    return key          # 显示名 -> 反查规范 id
            if dm and m == dm.lower():
                return dm
    except Exception as e:  # pragma: no cover - 配置读取异常时原样返回
        logger.warning("[cloud_llm] 云端 id 反查失败, 原样返回: %s", e)
    return model


def encode_cloud(model: str) -> str:
    """把纯模型名编码为 agent_model_map 的云端意图串: cloud:<model>。"""
    if model.startswith(CLOUD_PREFIX):
        return model
    return f"{CLOUD_PREFIX}{model}"


def decode_cloud(spec: str) -> str:
    """从 cloud:<model> 或纯模型名中取出纯模型名。"""
    if spec.startswith(CLOUD_PREFIX):
        return spec[len(CLOUD_PREFIX):]
    return spec


def cloud_gated() -> Optional[str]:
    """返回门控原因字符串; 为 None 表示允许调用云端。

    语义对齐 config.env.Config.ALLOW_CLOUD_API:
      - 试用版 (MOTOR_ENV=trial) 强制关闭;
      - ALLOW_CLOUD_API 环境变量为 false 时关闭;
      - 其余 (dev 且未显式关闭) 允许。
    注意: 单个厂商"缺真实密钥"不在此处拦截, 由 get_cloud_client 抛 CloudLLMUnavailable。
    """
    env = os.getenv("MOTOR_ENV", "dev")
    if env == "trial":
        return "trial_env_disabled"
    if os.getenv("ALLOW_CLOUD_API", "true").lower() != "true":
        return "allow_cloud_api_off"
    return None


def _resolve_vendor_credential(vendor: str) -> Optional[Dict[str, Any]]:
    """解析某厂商的 {api_key, base_url, timeout}。

    优先级: 云服务配置 (UI 填写, cloud_services_config) > 环境变量 (.env)。
    云服务未启用 / 无密钥 时返回 None, 调用方据此回退到环境变量。
    本函数惰性导入 cloud_services_config, 避免与 cloud_llm 形成循环依赖。
    """
    try:
        try:
            from Core.cloud_services_config import get_vendor_credential
        except Exception:
            from cloud_services_config import get_vendor_credential  # type: ignore
        cred = get_vendor_credential(vendor)
        if cred and (cred.get("api_key") or "").strip():
            return cred
    except Exception as e:  # pragma: no cover
        logger.warning("[cloud_llm] 读取云服务配置失败, 回退 .env: %s", e)
    # 回退环境变量
    info = _VENDOR_REGISTRY.get(vendor)
    if not info:
        return None
    return {
        "api_key": os.getenv(info["api_key_env"], ""),
        "base_url": os.getenv(info["base_url_env"], info["base_url"]),
        "timeout": None,
        "api": "openai-chat",
        "api_key_env": info["api_key_env"],
        "default_model": "",
    }


def _vendor_base_url(vendor: str) -> str:
    """回退用: 仅环境变量 / 默认端点 (UI 配置优先路径见 _resolve_vendor_credential)。

    P1: 自定义厂商 (custom:*) 无注册表条目 → 返回空串 (端点只能来自卡片 base_url_override),
    不再 KeyError。
    """
    info = _VENDOR_REGISTRY.get(vendor)
    if not info:
        return ""
    return os.getenv(info["base_url_env"], info["base_url"])


def _vendor_api_key(vendor: str) -> str:
    """回退用: 仅环境变量 (UI 配置优先路径见 _resolve_vendor_credential)。

    P1: 自定义厂商无注册表条目 → 返回空串 (密钥只能来自卡片 api_key / api_key_env)。
    """
    info = _VENDOR_REGISTRY.get(vendor)
    if not info:
        return ""
    return os.getenv(info["api_key_env"], "")


# ───────────────────── 核心: 返回 OpenAI 兼容客户端 ─────────────────────
def get_cloud_client(model: str, *, timeout: float = 60.0,
                     api_key: Optional[str] = None, base_url: Optional[str] = None,
                     model_override: Optional[str] = None):
    """按模型名返回协议感知的云端客户端 wrapper。

    v4.02.07 H-3 二期 #839 / #841: 返回 _ProtocolAwareClient 而非裸 openai.OpenAI。
    该 wrapper 对外维持 `.chat.completions.create(...)` 接口形状 (调用方响应解析零改动),
    内部按 `endpoint` (chat / completions) 派发到对应端点,
    并把 legacy 响应归一化为 chat 形状。有效请求模型名取厂商卡片 default_model
    (而非注册表 model 名), 以解决"用户把 DeepSeek 卡改填 Agnes 端点但 Agent 仍发
    deepseek-chat 被拒"的问题; 派发按 endpoint 而非 api, 使 Agnes 标 legacy 但实际
    只走 chat 的网关也能正确运行 (避开上游 messages 重复注入 bug)。

    参数:
        model     : 纯模型名 (如 "deepseek-chat"), 不含 "cloud:" 前缀 (T2 解码后传入)。
                     仅用于解析厂商与路由; 实际请求模型名取厂商卡片 default_model (回退本参数)。
        timeout   : 请求超时 (秒), 默认 60; 后续 Agent 可透传自身超时策略。
        api_key   : 覆盖密钥 (测试端点用, 未传则按优先级解析)。
        base_url  : 覆盖端点 (测试端点用, 未传则按优先级解析)。
        model_override : 强制实际请求模型名 (P1-2 cloud_alt 备选云模型层专用)。
                     卡片 default_model 覆盖是「Agnes 重指向」的既定行为, 但多级降级
                     换备选模型时会被它拉回主模型 (备选形同虚设), 故此处开一个显式覆盖口;
                     未传 (默认) 时行为与现状完全一致。

    返回: _ProtocolAwareClient 实例 (协议感知, 对外 .chat.completions.create)。

    异常:
        CloudLLMUnavailable: 门控禁用 / 未知模型 / 缺真实密钥 / openai 未安装。
    """
    # 1) 门控
    gate = cloud_gated()
    if gate is not None:
        raise CloudLLMUnavailable(
            f"云端 API 已禁用 (reason={gate}); 请检查 MOTOR_ENV / ALLOW_CLOUD_API 配置。"
        )

    # 2) 模型已知性 (用于解析厂商)
    vendor = MODEL_TO_VENDOR.get(model)
    if vendor is None:
        # H-3 二期 E方案 / Fix Y-3: 注册表未命中时反向查云服务卡片
        #   default_model == model 的厂商 (自定义云模型场景: 用户把 DeepSeek 卡
        #   default_model 改成 agnes-3.0-flash). 找到则按该厂商卡解析密钥/端点.
        vendor = _reverse_lookup_vendor_by_default_model(model)
    if vendor is None:
        raise CloudLLMUnavailable(
            f"未知云端模型 '{model}'; 已知: {list_cloud_models()}"
        )

    # 3) 解析密钥 / 端点 (云服务配置 > .env)
    cred = _resolve_vendor_credential(vendor) or {}
    if api_key is None:
        api_key = (cred.get("api_key") or "").strip()
    if base_url is None:
        base_url = cred.get("base_url") or _vendor_base_url(vendor)
    # 厂商级超时优先 (仅当调用方未显式指定)
    if timeout is None and cred.get("timeout"):
        timeout = cred["timeout"]

    # 占位 / 缺失 -> 不可用
    if not api_key or api_key.strip().lower() in _PLACEHOLDER_KEYS:
        # P1: 自定义厂商 (custom:*) 无注册表条目, 用 .get 兜底避免 KeyError
        _env_name = (_VENDOR_REGISTRY.get(vendor) or {}).get("api_key_env") or "N/A (自定义厂商无内置环境变量)"
        raise CloudLLMUnavailable(
            f"云端模型 '{model}' 缺少真实 API 密钥 "
            f"(云服务配置未启用/未填, 且环境变量 "
            f"{_env_name} 未配置或仍为占位值)。"
        )

    # P1: 端点缺失显式报错 (自定义厂商无内置默认端点, 空 base_url 会让 openai SDK
    #   静默回落官方 api.openai.com 造成"鉴权失败"误判, 此处提前拦截给出可操作提示)
    if not (base_url or "").strip():
        raise CloudLLMUnavailable(
            f"云端模型 '{model}' 缺少端点 (vendor={vendor}); "
            f"请在管理端「API 服务 (云端模型)」该厂商卡填写「端点覆盖」。"
        )

    # 4) 协议 / 实际派发目标 / 有效模型名 (均取自厂商卡片)
    #    派发权由 endpoint 接管 (B 方案, v4.02.07): endpoint="completions" 走
    #    /v1/completions + prompt, 其余 (含 chat) 走 /v1/chat/completions + messages。
    #    api 仅作接口风格标记, 不再决定派发。endpoint 缺失或不合法时按 api 推导兜底。
    protocol = (cred.get("api") or "openai-chat").strip() or "openai-chat"
    endpoint = (cred.get("endpoint") or "").strip().lower()
    if endpoint not in ("chat", "completions"):
        endpoint = "completions" if protocol == "openai-completions" else "chat"
    # P1-2: model_override 优先 (cloud_alt 备选云模型必须真正发备选名, 否则被卡片
    #   default_model 拉回主模型, 换模型重试变成「原地重试主模型」)。
    effective_model = ((model_override or "").strip()
                       or (cred.get("default_model") or "").strip() or model)

    # 5) 惰性导入 openai (避免未安装时阻断模块导入)
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover - 依赖缺失
        raise CloudLLMUnavailable(f"openai 包不可用: {e}")

    # P2-1: 关闭 openai SDK 内部默认的 2 次静默重试, 与池路径 (下方 max_retries=0) 对齐。
    #   否则单 key 路径每一次上层可见失败会被 SDK 放大成最多 3 次真实 HTTP 往返,
    #   在超时 (120s×3) 与限流场景下使实际耗时/配额消耗成倍叠加 (历史坑: 池路径
    #   曾因 SDK 重试把端点桶在 17s 内打出 20+ 请求全 429)。重试策略改由网关层
    #   (_GatewayCloudLLM.invoke 的 P1-1 有限重试) 统一、可控地承担。
    raw = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    if effective_model != model:
        logger.info(
            "[cloud_llm] 实际请求模型名覆盖/派发: 注册表 '%s' -> 卡片有效模型 '%s' "
            "(vendor=%s, api=%s, endpoint=%s)", model, effective_model, vendor, protocol, endpoint
        )

    # P0 多 KEY 池 (保守默认关): 启用时为该 vendor 惰性构建池客户端并注入 wrapper。
    #   未启用 (默认) 时完全跳过本段, 返回单 key 客户端, 与现状字节等价。
    key_pool_obj = None
    pool_clients = []
    key_entries = []
    rate_guard_obj = None
    try:
        # 双路径导入 (与 cloud_services_config 一致): 正常包运行命中 Core.cloud_key_pool;
        #   Core 目录在 sys.path 时裸导入命中。两者指向同一模块, 单例 KeyPool/RATE_GUARD 不分裂。
        try:
            from Core.cloud_key_pool import KeyPool, RATE_GUARD, key_pool_enabled
        except Exception:
            from cloud_key_pool import KeyPool, RATE_GUARD, key_pool_enabled  # type: ignore
        if key_pool_enabled():
            _kp = KeyPool.instance()
            if _kp.is_active(vendor):
                key_pool_obj = _kp
                rate_guard_obj = RATE_GUARD
                _entries = _kp.active_entries(vendor)
                for _e in _entries:
                    _b = (_e.endpoint or (base_url or "")).rstrip("/")
                    if not _b:
                        continue
                    # 关键 (历史坑铁律): max_retries=0 关掉 SDK 内部 2 次重试, 否则池 wrapper
                    #   每次 429 尝试被放大成 3 次真实 HTTP → 直接打爆端点桶 (实测 17s 20+ 请求全 429)。
                    pool_clients.append(OpenAI(
                        api_key=_e.key, base_url=_b, timeout=timeout, max_retries=0))
                    key_entries.append(_e)
                if not pool_clients:
                    key_pool_obj = None  # 池建空 → 回退单 key (不启用池路径)
                else:
                    logger.info(
                        "[cloud_llm][pool] 启用多 KEY 池 vendor=%s (n=%d, model=%s)",
                        vendor, len(pool_clients), effective_model)
    except Exception as _pe:  # pragma: no cover - 池加载异常 → 静默回退单 key (零回归保障)
        logger.warning("[cloud_llm][pool] 池构建失败, 回退单 key 路径: %s", _pe)
        key_pool_obj = None

    return _ProtocolAwareClient(
        raw, effective_model=effective_model, protocol=protocol, endpoint=endpoint,
        key_pool=key_pool_obj, pool_vendor=vendor, pool_clients=pool_clients,
        key_entries=key_entries, rate_guard=rate_guard_obj,
    )


# ───────────────────── 协议感知客户端 (v4.02.07 H-3 二期 #839) ─────────────────────
def _messages_to_prompt(messages) -> str:
    """把 chat messages (list[dict] / list[obj] / str) 拼成 completions 接口所需的 prompt 文本。"""
    if messages is None:
        return ""
    if isinstance(messages, str):
        return messages
    parts = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role")
            content = m.get("content")
        else:
            role = getattr(m, "role", None)
            content = getattr(m, "content", m)
        if role == "system":
            parts.append(f"System: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        elif role == "user":
            parts.append(f"User: {content}")
        else:
            parts.append(str(content))
    return "\n".join(parts)


class _CompletionMessage:
    """归一化后的 message: 暴露 .content (与 chat 响应同形)。"""
    def __init__(self, content):
        self.content = content
        self.role = "assistant"


class _CompletionChoice:
    """归一化后的 choice: 暴露 .message.content (与 chat 响应同形)。"""
    def __init__(self, text):
        self.text = text
        self.message = _CompletionMessage(text)


class _NormalizedCompletion:
    """把 /v1/completions 响应归一化为 chat 形状, 使调用方 .choices[0].message.content / .usage 可用。"""
    def __init__(self, original, choices):
        self.choices = choices
        self.usage = getattr(original, "usage", None)
        self.id = getattr(original, "id", None)
        self.model = getattr(original, "model", None)
        self.created = getattr(original, "created", None)
        self.object = getattr(original, "object", None)


def _normalize_completions(resp):
    """把 /v1/completions 响应归一化为 chat 形状 (.choices[0].message.content / .usage)。"""
    if resp is None:
        return resp
    choices = getattr(resp, "choices", None) or []
    norm_choices = [_CompletionChoice(getattr(c, "text", None) or "") for c in choices]
    return _NormalizedCompletion(resp, norm_choices)


def _is_rate_error(e) -> bool:
    """判定一个异常是否为云端限流 (429)。蓝本: Scripts install_cloud_429_backoff。

    不 import openai (保持模块惰性), 靠 status_code / 类名 / 消息特征判定,
    兼容 openai SDK 各版本 (RateLimitError.status_code=429 等)。
    """
    code = getattr(e, "status_code", None)
    name = type(e).__name__
    msg = str(e).lower()
    return (code == 429) or ("429" in str(e)) or ("ratelimit" in name.lower()) \
        or ("速率限制" in str(e)) or ("rate limit" in msg) or ("too many requests" in msg)


class _CompletionsNamespace:
    """`.chat.completions` 的 create 入口; 按 endpoint 派发到 chat 或 completions 端点。

    v4.02.07 H-3 二期 B (#841): 派发判据由 `client._protocol` (api) 改为 `client._endpoint`。
    因此 api 标 openai-completions 但 endpoint=chat 的网关 (如 Agnes)
    也能正确走 /v1/chat/completions + messages, 不再撞上游 messages 重复注入 bug。
    """
    def __init__(self, client: "_ProtocolAwareClient"):
        self._client = client

    def create(self, *, model=None, messages=None, **kwargs):
        client = self._client
        # P0 多 KEY 池 (保守默认关): 仅当该 client 注入了 key_pool 且池客户端已建 → 走池路径。
        # 未注入池 (绝大多数生产调用) → 走下方原路径, 行为 100% 不变 (字节等价铁律)。
        pool_on = getattr(client, "_key_pool", None) is not None
        if pool_on and getattr(client, "_pool_clients", None):
            return self._create_with_pool(client, messages=messages, **kwargs)
        if client._endpoint == "completions":
            # 老式 /v1/completions 接口 (prompt 入参)
            prompt = _messages_to_prompt(messages)
            raw_resp = client._raw.completions.create(
                model=client._effective_model, prompt=prompt, **kwargs
            )
            return _normalize_completions(raw_resp)
        # 默认 chat 接口 (/v1/chat/completions + messages)
        return client._raw.chat.completions.create(
            model=client._effective_model, messages=messages, **kwargs
        )

    # ── P0 多 KEY 池路径 (仅池启用时进入; 蓝本: Scripts install_cloud_429_backoff) ──
    def _create_with_pool(self, client, *, messages=None, **kwargs):
        """池内 sticky 轮换 + per-endpoint×key AIMD 限速 + 试满整池轮才退避。

        语义 (与 Scripts 蓝本 + _verify_sticky_key 三阶段一致):
          - 粘性: 首次沿用【上次成功】key (advance=False); 仅 429 后才 advance 切下一把。
          - 池里还有没试过的 key → 立即换 key 重试 (不睡); 整池一轮都限流才退避睡眠。
          - 非限流错误 (密钥失效/网络) → 不降速、原样抛出交还网关降级。
        池客户端列表与 key_entries 顺序严格对齐 (由 get_cloud_client 惰性构建)。
        """
        pool = client._key_pool
        vendor = client._pool_vendor
        clients = client._pool_clients
        entries = client._key_entries
        guard = client._rate_guard
        n = len(clients)
        endpoint_dispatch = client._endpoint == "completions"

        max_attempts = 16
        last = None
        for i in range(max_attempts):
            # 粘性选择池客户端: i=0 沿用上次成功 (sticky_index), i>0 是 429 后 advance
            if i == 0:
                idx = pool.sticky_index(vendor) % n
            else:
                pool.advance_on_429(vendor)
                idx = pool.sticky_index(vendor) % n
            raw_cli = clients[idx]

            # per-endpoint×key AIMD 限速 (slot 名 = host#key[-6:], 见 cloud_key_pool.rate_slot_for)
            slot = entries[idx].slot()
            if guard is not None:
                guard.acquire(slot)

            try:
                if endpoint_dispatch:
                    prompt = _messages_to_prompt(messages)
                    ret = raw_cli.completions.create(
                        model=client._effective_model, prompt=prompt, **kwargs)
                    if guard is not None:
                        guard.on_success(slot)
                    return _normalize_completions(ret)
                ret = raw_cli.chat.completions.create(
                    model=client._effective_model, messages=messages, **kwargs)
                if guard is not None:
                    guard.on_success(slot)
                return ret
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_rate_error(e):
                    # 非限流 (密钥失效/网络中断) → 不降速, 交还网关降级
                    if guard is not None:
                        guard.on_success(slot)
                    raise
                if guard is not None:
                    guard.on_rate_limited(slot)
                # 池里还有没试过的 key → 立即换下一把 (不睡); 整池一轮才退避
                if n > 1 and (i + 1) % n != 0:
                    logger.warning(
                        "[cloud_llm][pool] 限流, 立即换 key 重试 [%d/%d] vendor=%s",
                        ((i + 1) % n) + 1, n, vendor)
                    continue
                delay = self._resolve_retry_delay(e, i)
                logger.warning(
                    "[cloud_llm][pool] 限流(第%d/%d) vendor=%s, 退避 %.0fs 重试",
                    i + 1, max_attempts, vendor, delay)
                import time as _t
                _t.sleep(delay)
        logger.error("[cloud_llm][pool] 退避 %d 次仍限流, 抛出让网关降级: vendor=%s",
                     max_attempts, vendor)
        raise last

    @staticmethod
    def _resolve_retry_delay(e, attempt: int, min_delay: float = 30.0,
                             max_delay: float = 200.0) -> float:
        """429 退避: 优先 API 的 Retry-After, 否则退化指数退避 (蓝本: Scripts)。"""
        resp = getattr(e, "response", None)
        headers = getattr(resp, "headers", None) if resp is not None else None
        if headers is not None:
            ra = headers.get("retry-after") or headers.get("Retry-After") \
                or headers.get("x-ratelimit-reset")
            if ra is not None:
                try:
                    secs = float(ra)
                    if secs >= 0:
                        return min(max(secs, min_delay), max_delay)
                except (TypeError, ValueError):
                    pass
                try:
                    import email.utils as _eu
                    import time as _t
                    dt = _eu.parsedate_to_datetime(str(ra))
                    if dt is not None:
                        wait = dt.timestamp() - _t.time()
                        if wait > 0:
                            return min(max(wait, min_delay), max_delay)
                except Exception:
                    pass
        return min(min_delay * (1.6 ** min(attempt, 8)), max_delay)


class _ChatNamespace:
    def __init__(self, client: "_ProtocolAwareClient"):
        self._client = client
        self.completions = _CompletionsNamespace(client)


class _ProtocolAwareClient:
    """协议感知客户端 wrapper。

    对外维持 `.chat.completions.create(...)` 接口形状 (调用方响应解析零改动),
    内部按 `endpoint` 派发到 chat 或 completions 端点, 并把 legacy 响应归一化为 chat 形状。
    effective_model 取自厂商卡片 default_model (而非注册表 model 名), 解决 Agnes 重指向场景。

    v4.02.07 H-3 二期 B (#841): 派发判据由 `api` (_protocol) 改为 `endpoint`,
    使"api 标 legacy 但实际只走 chat"的网关 (Agnes) 自动走正确路径。
    """
    def __init__(self, raw_client, *, effective_model: str, protocol: str = "openai-chat",
                 endpoint: str = "chat",
                 # P0 多 KEY 池 (保守默认关): 以下 5 个默认 None → create 走原单 key 路径, 字节等价
                 key_pool=None, pool_vendor=None, pool_clients=None,
                 key_entries=None, rate_guard=None):
        self._raw = raw_client
        self._effective_model = effective_model
        self._protocol = protocol          # 仅作接口风格标记 (api 字段), 不参与派发
        self._endpoint = endpoint          # 实际派发目标: "chat" / "completions"
        # P0 池属性 (未启用时全 None, 零开销, 与原行为完全一致)
        self._key_pool = key_pool
        self._pool_vendor = pool_vendor
        self._pool_clients = pool_clients or []
        self._key_entries = key_entries or []
        self._rate_guard = rate_guard
        self.chat = _ChatNamespace(self)

    def __getattr__(self, name):
        # 未显式定义的属性 (如 .models / .embeddings / .with_options) 委托给裸客户端
        return getattr(self._raw, name)


# ─────────────────────────── 自检 (模块导入即可用) ───────────────────────────
def self_check() -> Dict[str, object]:
    """返回模块自检信息, 供运维/测试核验路由表完整性。

    v4.02.85: 原来只统计硬编码 _VENDOR_REGISTRY, 自定义厂商 (custom:*) 配了也不可见
    -> 诊断口径与真实配置不一致。现补 configured_* 口径 (含自定义卡)。
    """
    _configured: List[str] = []
    _customs: List[str] = []
    try:
        try:
            from Core.cloud_services_config import list_vendors, is_custom_vendor
        except ImportError:
            from cloud_services_config import list_vendors, is_custom_vendor
        _configured = list_vendors()
        _customs = [k for k in _configured if is_custom_vendor(k)]
    except Exception as e:  # pragma: no cover - 配置层不可用时退回 registry 口径
        logger.debug("[cloud_llm] self_check 读取配置厂商列表失败: %s", e)
    return {
        # 配置口径 (内置 + 已注册自定义): 反映"实际存在多少张卡"
        "vendor_count": len(_configured) or len(_VENDOR_REGISTRY),
        "builtin_vendor_count": len(_VENDOR_REGISTRY),
        "custom_vendors": _customs,
        "cloud_model_count": len(MODEL_TO_VENDOR),
        "cloud_models": list_cloud_models(),
        "gated": cloud_gated(),
        "prefix": CLOUD_PREFIX,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
