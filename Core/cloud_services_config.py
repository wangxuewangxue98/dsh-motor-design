"""
cloud_services_config.py - 云服务(API)配置持久化 (v4.02.06 H-3 二期 S1)

职责 (单一、低风险):
  1. 持久化各厂商云端 LLM 的 API Key / 自定义端点 / 启用状态 / 默认模型 / 超时。
  2. 提供 CRUD: get_all / get_vendor / upsert / delete。
  3. 提供 get_vendor_credential(vendor) 供 Core.cloud_llm 解析密钥与端点 (UI 配置 > .env)。
  4. 提供 test_connection(vendor, *, api_key=, base_url=, model=) 连通性自检。

单一真源: data/cloud_services.json
优先级 (解析顺序): 本配置 (UI 填写) > 环境变量 (.env) > 模拟模式
  (无真实密钥时 cloud_llm 抛 CloudLLMUnavailable, 由各 Agent 降级本地 Ollama)

安全约定:
  - 明文存储 + 文件 0o600 权限限制 (与现有 .env 一致, 用户已确认明文方案)。
  - 不引入加密以保持简单; 切换密钥需重启 Flask 使 cloud_llm 重新读取 (既有约束)。

依赖约束:
  - 本模块 **不 import cloud_llm** (避免循环依赖): cloud_llm 在运行时惰性 import 本模块。
  - 厂商默认定义在此处自包含拷贝, 与 cloud_llm._VENDOR_REGISTRY 对齐。
"""

import os
import re
import time
import json
import threading
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────── 路径 ───────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
CONFIG_PATH = os.path.join(_DATA_DIR, "cloud_services.json")

# ───────────────────── 6 厂商默认定义 (自包含, 避免循环依赖) ─────────────────────
# v4.02.07 H-3 二期补 B 方案:
#   - 新增 `endpoint` 字段: 实际派发目标 ("chat" 或 "completions")
#   - 默认按 `api` 推导: openai-chat → chat, openai-completions → completions
#   - 用户可在 UI 单独覆盖 (如 Agnes 标注 openai-completions 但实际只走 chat)
#   - 决策权: 派发按 endpoint, 不按 api; api 字段降级为"接口风格标记"
_VENDOR_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "deepseek":  {"label": "DeepSeek",            "default_base_url": "https://api.deepseek.com",
                  "models": ["deepseek-chat", "deepseek-reasoner"], "default_model": "deepseek-chat",
                  "timeout": 60,
                  "api": "openai-chat", "api_key_env": "DEEPSEEK_API_KEY",
                  "endpoint": "chat",
                  "display_models": {
                      "deepseek-chat": "DeepSeek Chat (V3)",
                      "deepseek-reasoner": "DeepSeek Reasoner (R1)",
                  }},
    "dashscope": {"label": "阿里云百炼 (Qwen)",  "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "models": ["qwen-max", "qwen-plus"], "default_model": "qwen-max",
                  "timeout": 60,
                  "api": "openai-chat", "api_key_env": "DASHSCOPE_API_KEY",
                  "endpoint": "chat",
                  "display_models": {
                      "qwen-max": "通义千问 Max",
                      "qwen-plus": "通义千问 Plus",
                  }},
    "zhipu":     {"label": "智谱 AI (GLM)",       "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
                  "models": ["glm-4-plus", "glm-4"], "default_model": "glm-4-plus",
                  "timeout": 60,
                  "api": "openai-chat", "api_key_env": "ZHIPU_API_KEY",
                  "endpoint": "chat",
                  "display_models": {
                      "glm-4-plus": "智谱 GLM-4 Plus",
                      "glm-4":      "智谱 GLM-4",
                  }},
    "moonshot":  {"label": "月之暗面 (Moonshot)", "default_base_url": "https://api.moonshot.cn/v1",
                  "models": ["moonshot-v1-8k"], "default_model": "moonshot-v1-8k",
                  "timeout": 60,
                  "api": "openai-chat", "api_key_env": "MOONSHOT_API_KEY",
                  "endpoint": "chat",
                  "display_models": {
                      "moonshot-v1-8k": "月之暗面 Moonshot 8K",
                  }},
    "minimax":   {"label": "MiniMax (abab)",      "default_base_url": "https://api.minimax.chat/v1",
                  "models": ["abab6.5s-chat"], "default_model": "abab6.5s-chat",
                  "timeout": 60,
                  "api": "openai-chat", "api_key_env": "MINIMAX_API_KEY",
                  "endpoint": "chat",
                  "display_models": {
                      "abab6.5s-chat": "MiniMax abab6.5s",
                  }},
    # v4.02.07 H-3 二期 C方案 B: 补 OpenAI 厂商, 与 cloud_llm._VENDOR_REGISTRY 对齐 (6 厂商)
    #   使「系统设置→云模型卡片」页也能管理 GPT-4o 系列, 与 AI 团队下拉框同源。
    "openai":   {"label": "OpenAI (GPT-4o)",     "default_base_url": "https://api.openai.com/v1",
                 "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4"], "default_model": "gpt-4o",
                 "timeout": 60,
                 "api": "openai-chat", "api_key_env": "OPENAI_API_KEY",
                 "endpoint": "chat",
                 "display_models": {
                     "gpt-4o":      "GPT-4o",
                     "gpt-4o-mini": "GPT-4o Mini",
                     "gpt-4-turbo": "GPT-4 Turbo",
                     "gpt-4":       "GPT-4",
                 }},
}


def _endpoint_default_for(api: str) -> str:
    """根据 `api` 字段推导默认 `endpoint`。"""
    api = (api or "").strip().lower()
    if api == "openai-completions":
        return "completions"
    # openai-chat / 其它未知值 → 默认 chat (主流 OpenAI 兼容)
    return "chat"


# ═══════════════ P1 (v4.02.81+): 自定义云厂商 (custom:*) ═══════════════
# 语义: 内置 6 厂商由 _VENDOR_DEFAULTS 固定; 任何 OpenAI 兼容端点 (自建网关 / 中转 /
#   Agnes / 本地 vLLM 等) 都可注册为一张"自定义厂商卡", vendor id 形如 "custom:agnes-cn"。
#   自定义卡与内置卡同构, 继承 endpoint / api_key_env / display_models /
#   api_keys (P0 多 KEY 池) 全部能力; 仅两点不同:
#     (a) 无内置默认端点 → base_url_override 必填 (upsert 强校验);
#     (b) delete = 真删 (内置厂商 delete 仍为"重置为默认", 保持既有行为)。
# 零回归: 不新增任何 custom 卡时, 全部改动等价 no-op (JSON 无该 key, _read 不注入)。
CUSTOM_PREFIX = "custom:"
# id 主体字符集: 字母数字 + . _ - ; 避免 URL 路径段出现空格 / 斜杠 / 二级冒号
_CUSTOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def is_custom_vendor(vendor: str) -> bool:
    """vendor id 是否属于自定义厂商 (custom:*, 大小写不敏感)。"""
    return str(vendor or "").strip().lower().startswith(CUSTOM_PREFIX)


def _custom_id_body(vendor: str) -> str:
    """取自定义厂商 id 的主体部分 (去掉 'custom:' 前缀)。"""
    return str(vendor or "").strip()[len(CUSTOM_PREFIX):]


def validate_custom_vendor(vendor: str) -> Optional[str]:
    """校验自定义厂商 id: 合法返回 None, 非法返回错误说明 (供 upsert 直接回给 UI)。"""
    v = str(vendor or "").strip()
    if not v.lower().startswith(CUSTOM_PREFIX):
        return f"自定义厂商 id 必须以 '{CUSTOM_PREFIX}' 开头"
    body = v[len(CUSTOM_PREFIX):]
    if not body:
        return f"自定义厂商 id 缺少主体 (形如 {CUSTOM_PREFIX}agnes-cn)"
    if not _CUSTOM_ID_RE.match(body):
        return (f"自定义厂商 id 主体只允许字母/数字/./_/- , 且以字母或数字开头, 长度 1-48 (当前: '{body}')")
    if body.lower() in _VENDOR_DEFAULTS:
        return f"自定义厂商 id 主体不可与内置厂商重名 ('{body}')"
    return None


def _custom_seed(vendor: str, label: str = "") -> Dict[str, Any]:
    """自定义厂商卡的默认骨架 (无内置端点 → base_url_override 必填)。"""
    body = _custom_id_body(vendor)
    return {
        "vendor": vendor,
        "label": (label or "").strip() or f"自定义 ({body})",
        "enabled": False,
        "api_key": "",
        "base_url_override": "",      # 必填: 自定义卡无内置默认端点
        "default_model": "",
        "timeout": 60,
        "api": "openai-chat",
        "api_key_env": "",
        "endpoint": "chat",
        "display_models": {},
        # P0: 多 KEY 池 (自定义卡同样支持, 保守默认关)
        "api_keys": [],
        # UI / 运维据此区分内置卡与自定义卡 (自定义卡可真删)
        "custom": True,
    }

# ─────────── 掩码占位符识别 (v4.02.85 防"掩码当密钥"事故) ───────────
# GET /api/admin/cloud-services 出于安全对密钥脱敏为 "***...XXXX" (掩码 = '*'*(n-4) + 末4位)。
# UI 把掩码回填到输入框; 若某条链路忘了"未改动则不回传"的守卫, 掩码就会被当成真 key
# 发到上游, 表现为 401 «无效的令牌» —— 极易被误判为"密钥失效/账号问题"。
# 本函数作为后端最后一道防线: 识别掩码形态并拒绝, 给出可执行的提示。
#   真 key 形如 sk-xxxxxxxx (不含 '*' 前缀), 故误伤概率为 0。
_MASKED_KEY_RE = re.compile(r"^(?:\*{4,}[^*]{4}|\*{8,})$")


def looks_masked_key(k: str) -> bool:
    """判断字符串是否为 UI 脱敏掩码占位符 (而非真实密钥)。

    形态: 4 个以上 '*' + 恰好 4 个非 '*' 字符 (即 mask 的末4位), 如 '****...****1UCI'。
    """
    s = str(k or "").strip()
    return bool(s) and bool(_MASKED_KEY_RE.match(s))


_lock = threading.RLock()


# ═══════════════════════ 内部读写 ═══════════════════════
def _seed_defaults() -> Dict[str, Dict[str, Any]]:
    """返回全部厂商的默认占位配置 (enabled=False, key 空)。"""
    out: Dict[str, Dict[str, Any]] = {}
    for vname, v in _VENDOR_DEFAULTS.items():
        out[vname] = {
            "vendor": vname,
            "label": v["label"],
            "enabled": False,
            "api_key": "",
            "base_url_override": "",
            "default_model": v["default_model"],
            "timeout": 60,
            "api": v.get("api", "openai-chat"),        # v4.02.07: 协议类型 (openai-chat / openai-completions)
            "api_key_env": v.get("api_key_env", ""),   # v4.02.07: 密钥环境变量名 (留空 = 不指定 .env 回退)
            "endpoint": v.get("endpoint") or _endpoint_default_for(v.get("api", "openai-chat")),  # v4.02.07: 实际派发目标 (chat/completions)
            "display_models": dict(v.get("display_models") or {}),  # v4.02.07 H-3 E方案 B: 真名→显示名映射, UI 友好化
            # P0 (v4.02.81+): 多 KEY 池 (per-端点, 保守默认关)。缺省 [] = 走原单 api_key 路径, 零回归。
            # 每项: {key, endpoint?, weight?, enabled?, label?}; 由 cloud_key_pool 消费。
            "api_keys": [],
        }
    return out


def _read() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            vendors = data.get("vendors", {})
            # 补齐: 确保全部已知厂商都在, 且字段齐全
            defaults = _seed_defaults()
            for k, v in defaults.items():
                if k not in vendors:
                    vendors[k] = v
                else:
                    for fk, fv in v.items():
                        vendors[k].setdefault(fk, fv)
            # P1: 自定义厂商卡 (custom:*) 字段补齐 —— 用骨架 setdefault, 不覆盖用户已填值。
            #   仅对文件中已存在的 custom 卡生效; 无 custom 卡时本段为 no-op (零回归)。
            for k in list(vendors.keys()):
                if not is_custom_vendor(k) or not isinstance(vendors.get(k), dict):
                    continue
                for fk, fv in _custom_seed(k).items():
                    vendors[k].setdefault(fk, fv)
                vendors[k].setdefault("custom", True)
            data["vendors"] = vendors
            return data
    except Exception as e:  # pragma: no cover
        logger.warning("[cloud_services_config] 读取失败 (%s), 回退默认值", e)
    return {"vendors": _seed_defaults(), "meta": {}}


def _write(data: Dict[str, Any]) -> None:
    # 目录跟随 CONFIG_PATH (而非模块常量 _DATA_DIR): 测试重定向 CONFIG_PATH 时
    # 不会误建/误写生产 data 目录。
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # v4.02.85: Windows 上 os.replace 可能被 AV 实时扫描/索引器短暂占用而抛
    #   PermissionError (WinError 5)。此前表现为偶发"保存失败", 现改为短重试。
    _last_err = None
    for _attempt in range(5):
        try:
            os.replace(tmp, CONFIG_PATH)
            _last_err = None
            break
        except PermissionError as _e:  # pragma: no cover - 环境相关
            _last_err = _e
            time.sleep(0.12 * (_attempt + 1))
    if _last_err is not None:
        raise _last_err
    # best-effort: 限制为仅 owner 读写 (Windows 下 chmod 行为有限, 不阻断)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ═══════════════════════ 对外 API ═══════════════════════
def list_vendors() -> List[str]:
    """已知厂商名列表 (内置 6 家 + 已注册的自定义 custom:*, 供 UI 下拉 / 校验)。"""
    out = list(_VENDOR_DEFAULTS.keys())
    with _lock:
        data = _read()
    for k in (data.get("vendors") or {}).keys():
        if is_custom_vendor(k) and k not in out:
            out.append(k)
    return out


def model_catalog() -> Dict[str, List[str]]:
    """各厂商可选模型 (供 UI 模型下拉); 含自定义卡 (default_model + display_models 键)。"""
    out: Dict[str, List[str]] = {k: list(v["models"]) for k, v in _VENDOR_DEFAULTS.items()}
    with _lock:
        data = _read()
    for k, v in (data.get("vendors") or {}).items():
        if not is_custom_vendor(k) or not isinstance(v, dict):
            continue
        models: List[str] = []
        dm = (v.get("default_model") or "").strip()
        if dm:
            models.append(dm)
        for key in (v.get("display_models") or {}).keys():
            key = (key or "").strip()
            if key and key not in models:
                models.append(key)
        out[k] = models
    return out


def display_models_for(vendor: str) -> Dict[str, str]:
    """返回 vendor 的真名→显示名映射 (供 UI 友好显示)。

    v4.02.07 H-3 E方案 B: UI 渲染时优先用显示名, 实际请求发真名;
    解决「标签说 legacy (openai-completions), 但实际想要友好名 + chat 路径」
    之类的 UX 落差。
    """
    with _lock:
        data = _read()
    v = data.get("vendors", {}).get(vendor) or {}
    saved = v.get("display_models")
    if isinstance(saved, dict) and saved:
        return {str(k).strip(): str(val).strip() for k, val in saved.items() if str(k).strip() and str(val).strip()}
    # 回退到 _VENDOR_DEFAULTS 默认值
    defaults = _VENDOR_DEFAULTS.get(vendor) or {}
    return {str(k).strip(): str(val).strip() for k, val in (defaults.get("display_models") or {}).items() if str(k).strip() and str(val).strip()}


def all_custom_default_models() -> List[str]:
    """所有 vendor 当前 default_model 中, 不在注册 catalog 内的部分 (供 catalog 动态化)。

    v4.02.07 H-3 E方案 A: 用户把 DeepSeek 卡 default_model 改成 ``agnes-3.0-flash`` 后,
    AI 团队下拉框应立即包含此项。仅返回「卡片实际配置 ≠ registry 注册」的差集,
    registry 真源由 ``cloud_llm.list_cloud_models()`` 提供。
    """
    registered = set()
    for v in _VENDOR_DEFAULTS.values():
        for m in (v.get("models") or []):
            m = (m or "").strip()
            if m:
                registered.add(m)
    with _lock:
        data = _read()
    out: List[str] = []
    seen = set()
    for v in (data.get("vendors") or {}).values():
        dm = (v.get("default_model") or "").strip()
        if dm and dm not in registered and dm not in seen:
            seen.add(dm)
            out.append(dm)
    return out


def get_all() -> Dict[str, Any]:
    """返回全部厂商配置 + 元信息。

    安全: 本地单用户工具, 明文返回 api_key (用户已确认明文方案),
    同时附带 has_key 标志便于 UI 判断。

    v4.02.07 H-3 E方案 B: 同时附带 ``catalog_with_display`` (per-vendor 真名→显示名),
    让前端可直接消费, 无需对每个 vendor 调 ``display_models_for()``。
    """
    with _lock:
        data = _read()
    vendors = dict(data.get("vendors", {}))
    for v in vendors.values():
        v["has_key"] = bool((v.get("api_key") or "").strip())
    # 聚合所有 vendor 的显示名映射
    catalog_with_display: Dict[str, Dict[str, str]] = {}
    for vn in vendors.keys():
        catalog_with_display[vn] = display_models_for(vn)
    return {"vendors": vendors, "meta": data.get("meta", {}),
            "catalog": model_catalog(),
            "catalog_with_display": catalog_with_display}


def get_vendor(vendor: str) -> Optional[Dict[str, Any]]:
    with _lock:
        data = _read()
    v = data.get("vendors", {}).get(vendor)
    if v:
        v = dict(v)
        v["has_key"] = bool((v.get("api_key") or "").strip())
    return v


def pool_of(vendor: str) -> List[Dict[str, Any]]:
    """P0 (v4.02.81+): 返回某厂商的多 KEY 池原始列表 (数据读取层)。

    每项为 dict: {key, endpoint?, weight?, enabled?, label?}。
    - 旧 JSON 无 api_keys 字段 → 返回 [] (调用方 cloud_key_pool 据此回退单 key)。
    - 返回深拷贝 (每项 dict 拷贝), 防调用方就地改动污染磁盘读取; 过滤策略
      (enabled/weight 展开) 由消费方 cloud_key_pool.KeyPool 负责, 本层只取数。
    - 仅保留 key 非空的项 (空 key 条目无意义, 直接丢弃)。
    """
    with _lock:
        data = _read()
    v = data.get("vendors", {}).get(vendor) or {}
    raw = v.get("api_keys") or []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        k = str(item.get("key") or "").strip()
        if not k:
            continue
        out.append({
            "key": k,
            "endpoint": str(item.get("endpoint") or "").strip(),
            "weight": int(item.get("weight") or 1),
            "enabled": bool(item.get("enabled", True)),
            "label": str(item.get("label") or "").strip(),
        })
    return out


def upsert(payload: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新一个厂商配置。

    payload 字段: vendor(必填), label?, enabled?, api_key?, base_url_override?,
                  default_model?, timeout?, api?, api_key_env?
    api_key / base_url_override / api_key_env 显式传空串 = 清空; 不传 = 保留原值。
    """
    vendor = (payload.get("vendor") or "").strip()
    # P1: 自定义厂商 (custom:*) 放开白名单; 内置厂商仍走原校验 (零回归)
    is_custom = is_custom_vendor(vendor)
    if is_custom:
        _err = validate_custom_vendor(vendor)
        if _err:
            return {"ok": False, "error": _err}
    elif vendor not in _VENDOR_DEFAULTS:
        return {"ok": False,
                "error": f"未知厂商 '{vendor}'; 已知内置: {list(_VENDOR_DEFAULTS.keys())}; "
                         f"若为新厂商请以 '{CUSTOM_PREFIX}' 开头 (如 {CUSTOM_PREFIX}agnes-cn)"}
    _warn_masked: List[str] = []
    with _lock:
        data = _read()
        base = _custom_seed(vendor, payload.get("label") or "") if is_custom \
            else _seed_defaults().get(vendor)
        v = dict(data.get("vendors", {}).get(vendor, base))
        v["vendor"] = vendor
        if payload.get("label"):
            v["label"] = payload["label"]
        if "enabled" in payload:
            v["enabled"] = bool(payload["enabled"])
        if "api_key" in payload:
            # v4.02.85: 掩码占位符不得落库。UI 若在"未改动"时误回了掩码,
            #   此处保留原值 (而非写入 "***1UCI"), 避免密钥被静默损坏。
            _incoming_key = payload["api_key"] or ""
            if looks_masked_key(_incoming_key):
                _warn_masked.append("api_key")
                logger.warning("[云服务配置] 厂商 %s 提交的 api_key 是掩码占位符, 已忽略并保留原值", vendor)
            else:
                v["api_key"] = _incoming_key
        if "base_url_override" in payload:
            v["base_url_override"] = payload["base_url_override"] or ""
        if payload.get("default_model"):
            v["default_model"] = payload["default_model"]
        if "timeout" in payload and payload["timeout"] is not None:
            try:
                v["timeout"] = max(10, int(float(payload["timeout"])))
            except Exception:
                pass
        # v4.02.07: 协议类型 + 密钥环境变量名 + 实际派发目标
        if payload.get("api"):
            v["api"] = payload["api"]
            # api 变更时, 若用户没显式给 endpoint, 跟着 api 自动推导
            if "endpoint" not in payload:
                v["endpoint"] = _endpoint_default_for(payload["api"])
        if "api_key_env" in payload:
            v["api_key_env"] = payload["api_key_env"] or ""
        if "endpoint" in payload:
            ep = (payload["endpoint"] or "").strip().lower()
            v["endpoint"] = ep if ep in ("chat", "completions") else _endpoint_default_for(v.get("api", "openai-chat"))
        # v4.02.07 H-3 E方案 B: 真名→显示名映射 (Dict); UI 友好化, 实际请求用真名
        if "display_models" in payload and isinstance(payload["display_models"], dict):
            v["display_models"] = {
                str(k).strip(): str(val).strip()
                for k, val in payload["display_models"].items()
                if str(k).strip() and str(val).strip()
            }
        # P0 (v4.02.81+): 多 KEY 池。显式传 api_keys (含 []) = 覆盖; 不传 = 保留原值。
        #   每项 schema: {key(必填非空), endpoint?, weight?(默认1,正整数), enabled?(默认True), label?}。
        #   与单 api_key 并存: 池非空且 CLOUD_KEY_POOL_ENABLED=1 时优先走池, 否则回退 api_key。
        if "api_keys" in payload:
            _raw_keys = payload.get("api_keys")
            if not isinstance(_raw_keys, list):
                raise ValueError("api_keys 必须是 list (可传 [] 清空池)")
            norm = []
            for _it in _raw_keys:
                if not isinstance(_it, dict):
                    continue
                _k = str(_it.get("key") or "").strip()
                if not _k:
                    continue  # 丢弃无 key 项
                if looks_masked_key(_k):
                    # v4.02.85: 掩码项不得进池 (否则轮询会把掩码当 key 发给上游 → 401)
                    _warn_masked.append(f"api_keys[{_k[-4:]}]")
                    logger.warning("[云服务配置] 厂商 %s 的池项是掩码占位符, 已丢弃", vendor)
                    continue
                try:
                    _w = int(_it.get("weight") or 1)
                except (TypeError, ValueError):
                    _w = 1
                norm.append({
                    "key": _k,
                    "endpoint": str(_it.get("endpoint") or "").strip(),
                    "weight": _w if _w > 0 else 1,
                    "enabled": bool(_it.get("enabled", True)),
                    "label": str(_it.get("label") or "").strip(),
                })
            v["api_keys"] = norm
        # P1: 自定义厂商强校验 —— 无内置默认端点, base_url_override 必填
        if is_custom:
            v["custom"] = True
            if not (v.get("base_url_override") or "").strip():
                return {"ok": False,
                        "error": f"自定义厂商 '{vendor}' 必须填写端点 (base_url_override): "
                                 "自定义卡没有内置默认端点, 留空将无法路由"}
        data["vendors"][vendor] = v
        data.setdefault("meta", {})["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write(data)
    out = {"ok": True, "vendor": vendor, "config": v, "has_key": bool(v["api_key"].strip())}
    if _warn_masked:
        # v4.02.85: 把"忽略了掩码"这件事显式回给调用方 (UI 可提示, 不再静默)
        out["warnings"] = [f"已忽略掩码占位符 ({', '.join(_warn_masked)}); "
                           f"如需更换密钥请在输入框重新输入真实 API Key"]
        out["masked_ignored"] = _warn_masked
    return out


def delete(vendor: str) -> Dict[str, Any]:
    """删除厂商配置 (P1 起按类型区分语义)。

    - 内置厂商 (6 家): **重置为默认** (禁用 + 清空 key), 保持厂商列表完整 —— 与既有行为一致, 零回归。
    - 自定义厂商 (custom:*): **真删** (从 vendors 移除), 避免"删不掉"的幽灵卡越积越多。

    返回 {ok, vendor, removed}; removed=True 表示真删, False 表示重置。
    """
    with _lock:
        data = _read()
        vendors = data.setdefault("vendors", {})
        if is_custom_vendor(vendor):
            vendors.pop(vendor, None)
            _write(data)
            return {"ok": True, "vendor": vendor, "removed": True}
        if vendor in vendors:
            vendors[vendor] = _seed_defaults()[vendor]
            _write(data)
    return {"ok": True, "vendor": vendor, "removed": False}


def get_vendor_credential(vendor: str) -> Optional[Dict[str, Any]]:
    """供 cloud_llm 解析密钥 / 端点 / 协议 / 有效模型名。

    返回 {api_key, base_url, timeout, api, api_key_env, default_model};
    未启用 / 无密钥(含 api_key_env 回退后仍无) 时返回 None (降级到 .env)。

    v4.02.07 增 api / api_key_env / default_model 字段供调用方按协议派发:
      - api          : 协议类型 (openai-chat / openai-completions)。
      - api_key_env : 密钥环境变量名 (.env 回退名); 卡片未填 key 但此变量在 .env 存在时采用之。
      - default_model: 实际请求模型名 (解决 Agnes 重指向场景: 用户把 DeepSeek 卡改填
        Agnes 端点, 但 Agent 仍发 deepseek-chat 被拒 -> 用卡片 default_model 覆盖)。
    """
    with _lock:
        data = _read()
    v = data.get("vendors", {}).get(vendor)
    if not v or not v.get("enabled", False):
        return None
    defaults = _VENDOR_DEFAULTS.get(vendor, {})
    # v4.02.07: api_key_env 回退 — 卡片未填 key 但指定了环境变量且其在 .env 中存在
    api_key_env = (v.get("api_key_env") or "").strip() or defaults.get("api_key_env", "")
    key = (v.get("api_key") or "").strip()
    if not key and api_key_env:
        key = os.getenv(api_key_env, "").strip()
    if not key:
        return None
    base = (v.get("base_url_override") or "").strip() or \
        defaults.get("default_base_url", "")
    # v4.02.07: endpoint 字段 (实际派发目标, 优先卡片覆盖值)
    saved_endpoint = (v.get("endpoint") or "").strip().lower()
    endpoint = saved_endpoint if saved_endpoint in ("chat", "completions") \
        else _endpoint_default_for(v.get("api") or defaults.get("api", "openai-chat"))
    return {
        "api_key": key,
        "base_url": base,
        "timeout": int(v.get("timeout", 60)),
        "api": v.get("api") or defaults.get("api", "openai-chat"),
        "api_key_env": api_key_env,
        "default_model": v.get("default_model") or defaults.get("default_model", ""),
        "endpoint": endpoint,
    }


def test_connection(vendor: str, *, api_key: str = "", base_url: str = "",
                    model: str = "", timeout: int = 30,
                    api: str = "", api_key_env: str = "",
                    endpoint: str = "") -> Dict[str, Any]:
    """连通性自检: 用给定凭据对一个模型发最小请求。

    不依赖已保存配置; 若参数缺失则从已保存配置补齐 (仅当 enabled 且有 key)。
    返回 {ok, model, reply?, error?, api?, endpoint?}

    v4.02.07 增强:
      - 按 `endpoint` 派发 (chat / completions); endpoint 缺失时按 `api` 推导。
      - api_key 留空时按 api_key_env 指定的环境变量名从 .env 读取。
      - v4.02.07 H-3 二期 B: 派发权由 `api` 改为 `endpoint`, 因为 Agnes
        标注 openai-completions 但实际只走 chat 路径 (上游 /v1/completions
        内部 chat.completion() got multiple values for keyword argument 'messages')。
    """
    saved = get_vendor(vendor) or {}
    if not api_key:
        api_key = (saved.get("api_key") or "").strip()
    if not base_url:
        base_url = (saved.get("base_url_override") or "").strip()
    if not model:
        model = saved.get("default_model") or ""
    if not timeout:
        timeout = int(saved.get("timeout", 30) or 30)
    if not api:
        api = saved.get("api") or _VENDOR_DEFAULTS.get(vendor, {}).get("api", "openai-chat")
    if not api_key_env:
        api_key_env = (saved.get("api_key_env") or "").strip() or \
                      _VENDOR_DEFAULTS.get(vendor, {}).get("api_key_env", "")
    # endpoint 优先级: 实参 > saved.endpoint > saved.api 推导
    if not endpoint:
        endpoint = (saved.get("endpoint") or "").strip().lower()
    if endpoint not in ("chat", "completions"):
        endpoint = _endpoint_default_for(api)
    if not model:
        return {"ok": False, "error": "缺少测试模型 (default_model 为空)"}
    # API key 优先级: UI 填写 > 已保存 > 环境变量 (按 api_key_env)
    if not api_key and api_key_env:
        api_key = os.getenv(api_key_env, "").strip()
    if not api_key:
        return {"ok": False, "error": f"缺少 API Key (该厂商未启用或未填写密钥, 且环境变量 {api_key_env or '?'} 未配置)"}
    # v4.02.85: 掩码占位符拦截 —— 必须在联网前判定。
    #   否则上游只会回 401 «无效的令牌», 指向"密钥失效/账号问题", 掩盖真实原因
    #   (前端把脱敏显示值当真 key 发了)。
    if looks_masked_key(api_key):
        logger.warning("[云服务配置] 厂商 %s 的测试请求携带掩码占位符, 已拦截", vendor)
        return {"ok": False, "error": ("检测到提交的是掩码占位符 (形如 '****1UCI'), 不是真实密钥。"
                                       "请在 API Key 输入框重新输入/粘贴真实密钥后再测试; "
                                       "若密钥已在服务端保存, 留空输入框即可由后端回退使用已保存密钥。"),
                "api": api, "endpoint": endpoint, "masked": True}
    try:
        from openai import OpenAI
    except Exception as e:
        return {"ok": False, "error": f"openai 包不可用: {e}"}
    base = base_url or _VENDOR_DEFAULTS.get(vendor, {}).get("default_base_url", "")
    try:
        client = OpenAI(api_key=api_key, base_url=base, timeout=timeout)
        if endpoint == "completions":
            # 老式 /v1/completions 接口 (如部分厂商支持)
            resp = client.completions.create(
                model=model,
                prompt="ping",
                max_tokens=5,
            )
            ok = bool(resp and getattr(resp, "choices", None))
            reply = ""
            if ok:
                reply = (resp.choices[0].text or "")[:80]
            return {"ok": ok, "model": model, "reply": reply, "api": api, "endpoint": endpoint}
        else:
            # 默认: chat completions 接口 (/v1/chat/completions)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            ok = bool(resp and getattr(resp, "choices", None))
            reply = ""
            if ok:
                reply = (resp.choices[0].message.content or "")[:80]
            return {"ok": ok, "model": model, "reply": reply, "api": api, "endpoint": endpoint}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "api": api, "endpoint": endpoint}


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_all())
