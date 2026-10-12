"""
llm_gateway.py - 系统级 LLM 网关 (v4.02.07, H-3 二期 T1 落地增强)

职责 (单一、系统级、对所有智能体透明):
  1. 统一 LLM 解析: resolve_cloud_model_generic(agent) 按 agent 名读取云端配置
     (ai_flow 持久化 > agent_model_map cloud: 前缀 > CLOUD_MODEL_NAME),
     消除 base_agent._resolve_cloud_model 仅硬编码 requirement 段的天花板。
  2. 统一本地构建: _build_local_ollama(agent, temperature, is_vl, num_ctx, num_predict)
     集中治理 num_ctx/keep_alive/client_kwargs 超时, 并在云端模型名泄漏时回退
     LOCAL_MODEL_NAME, 避免喂给本地 Ollama 致 404。
  3. 统一云端适配器: _GatewayCloudLLM 云端优先, 失败时自动降级本地 Ollama, 对调用方透明;
     记录 went_cloud / fallback_reason 供可观测。
  4. 统一入口:
       - resolve_llm_for_agent(agent, temperature, is_vl, num_ctx, num_predict)
         返回单一 chat 模型 (云端优先/本地兜底), 消除各 agent 各自 try/except 降级
         (设计文档: 降级逻辑重复 4+ 处、风格分裂 3 种)。
       - gw_request(agent, system, user, task_type, temperature, ...)
         高层一步调用, 返回 GatewayResponse(model_used/attempts/fallback_reason/usage),
         并在全 LLM 失败时调用已注册的 rule_engine 兜底处理器。
  5. 规则引擎兜底注册表: register_rule_handler(task_type, fn); 各 agent 可在导入时注册。

设计约束:
  - 纯后端模块, 不 import base_agent (避免循环依赖); base_agent 反向 import 本模块并重导出
    _CloudLLMAdapter / _CloudAIMessage / extract_ollama_usage / _RetryingLLM / _wrap_llm_with_retry。
  - 复用 cloud_llm (get_cloud_client / is_cloud_model / cloud_gated / CLOUD_PREFIX / decode_cloud)
    与 workflow_config / ai_flow_config (均惰性导入, 避免导入副作用与循环依赖)。
  - 所有网络/模型调用惰性发生, 导入本模块零副作用。
"""

import os
import json
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("llm_gateway")

# ─────────────────────────── 云端工厂依赖 ───────────────────────────
from cloud_llm import (  # 纯后端模块, 仅依赖 cloud_llm (openai 惰性导入, 无副作用)
    CloudLLMUnavailable,
    get_cloud_client,
    is_cloud_model,
    cloud_gated,
    CLOUD_PREFIX,
    decode_cloud,
)

# ─────────────────────────── 模型监控埋点 (6.2 面板, Plan B) ───────────────────────────
# 惰性/容错导入: 监控模块不可用绝不影响网关主链路。model_monitor 仅依赖 cost_tracker, 无循环依赖。
try:
    from Core.model_monitor import (
        record_request as _mm_record_request,
        record_degradation as _mm_record_degradation,
        inc_active_requests as _mm_inc_active,
        dec_active_requests as _mm_dec_active,
    )
    _MM_OK = True
except Exception as _mm_err:  # pragma: no cover
    _MM_OK = False
    logger.warning("[模型监控] model_monitor 不可用, 跳过埋点: %s", _mm_err)


# ════════════════════════════════════════════════════════════════════
# 1. 连接重置重试包装 (原 base_agent._RetryingLLM, 迁移至网关集中治理)
# ════════════════════════════════════════════════════════════════════
def _collect_connection_errors():
    """收集所有"连接类"异常类型, 用于重试判定 (Ollama 崩溃/网络抖动)."""
    errs = [ConnectionResetError, ConnectionError, BrokenPipeError]
    _specs = [
        ("requests.exceptions", ["ConnectionError", "ChunkedEncodingError", "Timeout"]),
        ("urllib3.exceptions", ["HTTPError", "ProtocolError", "MaxRetryError", "ReadTimeoutError"]),
        ("httpx", ["TransportError", "ConnectError", "ReadError", "RemoteProtocolError",
                   "ConnectTimeout", "ReadTimeout"]),
    ]
    for _mod, _names in _specs:
        try:
            _m = __import__(_mod, fromlist=["_"])
        except Exception:
            continue
        for _n in _names:
            _e = getattr(_m, _n, None)
            if _e is not None and isinstance(_e, type) and issubclass(_e, Exception):
                errs.append(_e)
    return tuple(dict.fromkeys(errs))


_CONNECTION_ERRORS = _collect_connection_errors()


def _normalize_messages(messages):
    """把 OpenAI 风格 dict 列表 ([{"role","content"}]) 转 LangChain BaseMessage 列表,
    使其可被本地 ChatOllama 消费; 非 dict 列表 (含 BaseMessage / str) 原样返回。
    用途: report/review 等既有智能体以 dict 消息调用网关, 本地路径需与云端适配器对齐,
    使网关路由模式对调用方完全透明 (云端 dict / 本地 dict 统一受理)。
    """
    if not isinstance(messages, list) or not messages:
        return messages
    if not all(isinstance(m, dict) for m in messages):
        return messages
    try:
        from langchain_core.messages import (
            SystemMessage, HumanMessage, AIMessage, ToolMessage)
        _map = {"system": SystemMessage, "user": HumanMessage,
                "assistant": AIMessage, "tool": ToolMessage}
        out = []
        for m in messages:
            _role = (m.get("role") or "user")
            _cls = _map.get(_role, HumanMessage)
            out.append(_cls(content=m.get("content", "")))
        return out
    except Exception:
        return messages


class _RetryingLLM:
    """把 ChatOllama 包一层连接重置重试 (v5.3-B 修复版).

    ChatOllama 是 pydantic v2 模型, 禁止给未声明字段赋值, 因此**绝不**用 setattr 打补丁。
    改用组合代理: 持有一个内部 ChatOllama 实例, 对 invoke/stream/batch/ainvoke 做重试后委派。

    Ollama 在 T600 4GB 显存压力下偶发 worker 崩溃 (TCP RST 10054), 自愈通常只需数秒。
    重试 + 指数退避可让瞬断自愈后自动恢复, 避免单个 Agent 调用即 abort 整个 AI 团队工作流。
    """

    _RETRY_METHODS = ("invoke", "stream", "batch", "ainvoke")

    def __init__(self, llm, max_retries=3, base_backoff=1.5):
        object.__setattr__(self, "_llm", llm)
        object.__setattr__(self, "_max_retries", max_retries)
        object.__setattr__(self, "_base_backoff", base_backoff)
        for _a in ("model", "model_name", "temperature", "keep_alive", "num_ctx", "base_url"):
            if hasattr(llm, _a):
                object.__setattr__(self, _a, getattr(llm, _a))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_llm"), name)

    def _retry_call(self, method_name, *args, **kwargs):
        _llm = object.__getattribute__(self, "_llm")
        _max = object.__getattribute__(self, "_max_retries")
        _base = object.__getattribute__(self, "_base_backoff")
        _last = None
        # 归一化: 允许以 OpenAI 风格 dict 列表调用本地 Ollama (与云端适配器对齐),
        # 使 report/review 既有 dict 消息零改动接入本地路径 (网关路由对调用方透明)。
        args = tuple(_normalize_messages(a) if isinstance(a, list) else a for a in args)
        for _attempt in range(_max):
            try:
                return getattr(_llm, method_name)(*args, **kwargs)
            except _CONNECTION_ERRORS as _e:
                _last = _e
                if _attempt < _max - 1:
                    _wait = _base * (2 ** _attempt)
                    logger.warning(
                        "[网关-重试] LLM 连接重置(尝试 %d/%d), %ss 后重试: %s",
                        _attempt + 1, _max, _wait, _e)
                    time.sleep(_wait)
                    continue
                logger.error("[网关-重试] LLM 连接重试耗尽(%d): %s", _max, _e)
                raise
            except Exception:
                raise
        raise _last

    def invoke(self, *args, **kwargs):
        return self._retry_call("invoke", *args, **kwargs)

    def stream(self, *args, **kwargs):
        return self._retry_call("stream", *args, **kwargs)

    def batch(self, *args, **kwargs):
        return self._retry_call("batch", *args, **kwargs)

    async def ainvoke(self, *args, **kwargs):
        return self._retry_call("ainvoke", *args, **kwargs)


def _wrap_llm_with_retry(llm, max_retries=3, base_backoff=1.5):
    """给 ChatOllama 实例包一层连接重置重试 (返回代理对象, 不修改原 pydantic 实例)."""
    if llm is None:
        return llm
    return _RetryingLLM(llm, max_retries=max_retries, base_backoff=base_backoff)


# ════════════════════════════════════════════════════════════════════
# 1b. 本地调用埋点包装 (v4.02.99 C)
# ════════════════════════════════════════════════════════════════════
class _MonitoredLLM:
    """本地 LLM 埋点包装: 让本地 Ollama 调用进入 model_monitor.request_log。

    背景 (v6 实测): 本地调用此前完全不可观测 — 埋点只在 _GatewayCloudLLM.invoke 与
    agent.run, 本地 ChatOllama 调用既不入 request_log 也不入 audit.log, 只能靠 httpx
    /api/chat 日志清点。本包装在 invoke/stream/batch/ainvoke 上计时并落埋点
    (is_cloud=False, 按 Plan B 计 cost=0), 使「本地 25 次」这类调用可在 6.2 面板直接统计。

    与 _RetryingLLM 一样采用组合代理 (ChatOllama 是 pydantic v2 模型, 禁止 setattr 打补丁)。
    """

    def __init__(self, llm, agent_name: str, model: str):
        object.__setattr__(self, "_llm", llm)
        object.__setattr__(self, "_agent_name", agent_name)
        object.__setattr__(self, "_model", model)
        for _a in ("model", "model_name", "temperature", "keep_alive", "num_ctx", "base_url"):
            if hasattr(llm, _a):
                object.__setattr__(self, _a, getattr(llm, _a))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_llm"), name)

    def _emit(self, resp, latency_ms: float, success: bool, error=None):
        if not _MM_OK:
            return
        try:
            _ti, _to = (0, 0)
            if success:
                _ti, _to = extract_ollama_usage(resp)
            _mm_record_request(
                agent_name=object.__getattribute__(self, "_agent_name"),
                model=object.__getattribute__(self, "_model"),
                is_cloud=False, success=success,
                tokens_in=_ti, tokens_out=_to,
                latency_ms=latency_ms, error=error)
        except Exception as _mm_e:  # pragma: no cover
            logger.debug("[模型监控] 本地埋点失败: %s", _mm_e)

    def _call(self, method_name, *args, **kwargs):
        _llm = object.__getattribute__(self, "_llm")
        _t0 = time.perf_counter()
        if _MM_OK:
            try:
                _mm_inc_active()
            except Exception:
                pass
        try:
            resp = getattr(_llm, method_name)(*args, **kwargs)
        except Exception as e:
            self._emit(None, (time.perf_counter() - _t0) * 1000.0, False,
                       f"{type(e).__name__}: {e}")
            raise
        finally:
            if _MM_OK:
                try:
                    _mm_dec_active()
                except Exception:
                    pass
        self._emit(resp, (time.perf_counter() - _t0) * 1000.0, True)
        return resp

    def invoke(self, *args, **kwargs):
        return self._call("invoke", *args, **kwargs)

    def stream(self, *args, **kwargs):
        return self._call("stream", *args, **kwargs)

    def batch(self, *args, **kwargs):
        return self._call("batch", *args, **kwargs)

    async def ainvoke(self, *args, **kwargs):
        return self._call("ainvoke", *args, **kwargs)


def _wrap_llm_with_monitor(llm, agent_name: str, model: str):
    """给本地 LLM 实例包一层埋点 (返回代理对象, 不修改原 pydantic 实例)。"""
    if llm is None:
        return llm
    return _MonitoredLLM(llm, agent_name, model)


# ════════════════════════════════════════════════════════════════════
# 2. token 用量提取 (原 base_agent.extract_ollama_usage, 迁移至网关)
# ════════════════════════════════════════════════════════════════════
def _estimate_chars(text) -> int:
    """把任意对象转成字符串后返回字符数 (供 token 启发式回退)."""
    if not text:
        return 0
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return 0
    return len(text)


def extract_ollama_usage(resp, prompt=None):
    """从 ChatOllama / 云端返回的 AIMessage 提取真实 token 用量 (input/output)。

    v4.06: 收敛成本计量从启发式粗估升级为真实计量。
    优先读 AIMessage.usage_metadata (langchain >=0.2.2), 其次读
    response_metadata.token_usage (旧版 ChatOllama); 两者皆无时回退启发式
    (中英文混合约 4 字符/token)。返回 (input_tokens, output_tokens)。
    """
    try:
        if hasattr(resp, "usage_metadata") and isinstance(getattr(resp, "usage_metadata"), dict):
            _um = resp.usage_metadata
            _ti = _um.get("input_tokens") or _um.get("prompt_tokens") or 0
            _to = _um.get("output_tokens") or _um.get("completion_tokens") or 0
            if _ti or _to:
                return int(_ti), int(_to)
        _rm = getattr(resp, "response_metadata", None) or {}
        _tu = _rm.get("token_usage") or _rm.get("usage") or {}
        if isinstance(_tu, dict):
            _ti = _tu.get("prompt_tokens") or _tu.get("input_tokens") or 0
            _to = _tu.get("completion_tokens") or _tu.get("output_tokens") or 0
            if _ti or _to:
                return int(_ti), int(_to)
    except Exception:
        pass
    _ti = _estimate_chars(prompt) // 4 if prompt else 0
    _to = _estimate_chars(getattr(resp, "content", "")) // 4 if resp is not None else 0
    return _ti, _to


# ════════════════════════════════════════════════════════════════════
# 3. 云端适配器 (原 base_agent._CloudLLMAdapter, 迁移至网关)
# ════════════════════════════════════════════════════════════════════
class _CloudAIMessage:
    """mimic AIMessage 最小子集, 供 extract_ollama_usage 读取真实云端 token 用量。"""

    def __init__(self, content, input_tokens=0, output_tokens=0):
        self.content = content
        self.usage_metadata = {"input_tokens": int(input_tokens or 0),
                               "output_tokens": int(output_tokens or 0)}
        self.response_metadata = {}


class _CloudLLMAdapter:
    """把云端 OpenAI 兼容客户端适配成 ChatModel.invoke 接口。

    - prompt 可为 str 或 LangChain messages 列表 (自动转 OpenAI message dicts)。
    - 客户端在首次 invoke 时惰性构造, 缺密钥/门控/未知模型抛 CloudLLMUnavailable,
      由各 Agent 的 try/except 捕获后降级 (模板报告 / 仅正则结果), 不中断工作流。
    """

    def __init__(self, model, temperature=0.3, timeout=120.0):
        self.model = model
        self.temperature = temperature
        self._timeout = timeout
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            self._client = get_cloud_client(self.model, timeout=self._timeout)
        return self._client

    @staticmethod
    def _to_openai_messages(prompt_or_messages):
        if isinstance(prompt_or_messages, str):
            return [{"role": "user", "content": prompt_or_messages}]
        out = []
        for m in prompt_or_messages:
            # 兼容 OpenAI 风格 dict ({"role":..., "content":...}), 便于既有 report/review 云端消息列表零改动接入
            if isinstance(m, dict):
                _role = (m.get("role") or "user")
                content = m.get("content", "")
                if _role == "system":
                    out.append({"role": "system", "content": content})
                elif _role == "assistant":
                    out.append({"role": "assistant", "content": content})
                else:
                    out.append({"role": "user", "content": content})
                continue
            _type = getattr(m, "type", None)
            content = getattr(m, "content", m)
            if _type == "system":
                out.append({"role": "system", "content": content})
            elif _type == "ai":
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": content})
        return out

    def invoke(self, prompt_or_messages, **kwargs):
        client = self._ensure_client()
        messages = self._to_openai_messages(prompt_or_messages)
        resp = client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature
        )
        content = ""
        if getattr(resp, "choices", None):
            content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        _ti = getattr(usage, "prompt_tokens", 0) if usage else 0
        _to = getattr(usage, "completion_tokens", 0) if usage else 0
        return _CloudAIMessage(content=content, input_tokens=_ti, output_tokens=_to)


# ════════════════════════════════════════════════════════════════════
# 4. 配置加载 (gateway_config.json)
# ════════════════════════════════════════════════════════════════════
_GATEWAY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gateway_config.json")

_DEFAULT_GATEWAY_CONFIG = {
    "version": "1.0",
    "default_fallback_chain": ["cloud", "local"],
    "local_fallback_on_cloud_error": True,
    "agent_overrides": {},
    "rule_engine": {"enabled": True},
    # P0-1: 云端请求超时 (秒)。此前该键仅在默认值里定义、全仓库无任何读取点 ⇒ 死配置,
    #   _GatewayCloudLLM 恒用硬编码 120s; 而云端实测延迟 avg 52s / ReviewAgent avg 84.3s /
    #   峰值 1154s (406 次 >100s) ⇒ 余量仅约 36s, 长报告场景必然撞线降级本地 3B。
    #   现由 get_cloud_timeout() 统一读取并透传 (env CLOUD_REQUEST_TIMEOUT 可临时覆盖)。
    "cloud_timeout": 300,
    # P1-1: 云端失败时的网关层有限重试 (仅超时/429, 其它硬错误直接走 cloud_alt/本地降级)。
    #   cloud_max_retries   : 首次之外的额外重试次数 (0 = 不重试, 与旧行为等价)
    #   cloud_retry_backoff : 每次超时重试前的等待秒数
    "cloud_max_retries": 2,
    "cloud_retry_backoff": 3.0,
    "local_default_num_ctx": 4096,
    # 多级降级 cloud_alt 层参数 (P1-2 已启用)
    #   cloud_alt_model: 备选云模型名 (空=未配置)
    #   cloud_alt_max_attempts: cloud_alt 层内换 key/模型的最大尝试次数
    #   cloud_alt_enabled: 【B1】cloud_alt 层总开关, 由管理端「模型总路由卡片」写入;
    #                      env CLOUD_ALT_ENABLED 若被显式设置则优先 (应急热改, 双向覆盖)
    "cloud_alt_model": "",
    "cloud_alt_max_attempts": 3,
    "cloud_alt_enabled": False,
    # 【B1】多 KEY 池总开关, 由管理端「模型总路由卡片」写入;
    #        env CLOUD_KEY_POOL_ENABLED 若被显式设置则优先 (应急热改, 双向覆盖)。
    #        此前二者只读 env ⇒ 管理端只能看不能改 (GAP3), UI 开关点了不生效的静默黑洞。
    "key_pool_enabled": False,
}

_gateway_config_cache: Optional[Dict[str, Any]] = None


def load_gateway_config() -> Dict[str, Any]:
    """加载网关配置 (带默认值兜底, 文件缺失/损坏不影响运行)."""
    global _gateway_config_cache
    if _gateway_config_cache is not None:
        return _gateway_config_cache
    cfg = dict(_DEFAULT_GATEWAY_CONFIG)
    try:
        if os.path.exists(_GATEWAY_CONFIG_PATH):
            with open(_GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception as e:
        logger.warning("[网关] 加载 gateway_config.json 失败, 使用默认配置: %s", e)
    _gateway_config_cache = cfg
    return cfg


def get_fallback_chain(agent_name: str) -> List[str]:
    """返回某智能体的降级链 (云端优先/本地兜底), 支持 per-agent override。"""
    cfg = load_gateway_config()
    overrides = cfg.get("agent_overrides") or {}
    if agent_name in overrides:
        chain = overrides[agent_name].get("fallback_chain")
        if isinstance(chain, list) and chain:
            return chain
    return cfg.get("default_fallback_chain") or ["cloud", "local"]


def get_cloud_timeout() -> float:
    """云端请求超时秒数 (P0-1: 让 gateway_config.cloud_timeout 真正生效)。

    此前该键仅在 _DEFAULT_GATEWAY_CONFIG 里定义、全仓库无读取点 ⇒ 改配置不生效,
    _GatewayCloudLLM 恒用硬编码 120s, 而云端实测延迟 avg 52s / 长文本智能体 avg 84s,
    余量仅约 36s ⇒ 长报告场景必然撞线 (APITimeoutError) 降级本地 3B 小模型。

    优先级: env CLOUD_REQUEST_TIMEOUT (应急热改/灰度) > gateway_config.cloud_timeout > 120。
    非法值/非正值一律回退, 保证永不返回 0 或负数 (否则请求会立即超时)。
    """
    try:
        _env = (os.getenv("CLOUD_REQUEST_TIMEOUT") or "").strip()
        if _env:
            _f = float(_env)
            if _f > 0:
                return _f
    except Exception:
        pass
    try:
        _f = float(load_gateway_config().get("cloud_timeout"))
        if _f > 0:
            return _f
    except Exception:
        pass
    return 120.0


def _cloud_retry_settings() -> Tuple[int, float]:
    """云端失败重试策略 (P1-1): 返回 (额外重试次数, 重试前等待秒数)。

    仅对本轮"可自愈"的失败开放重试 (判定见 _GatewayCloudLLM.invoke):
      - 超时 (APITimeoutError / ReadTimeout): 抖动性质, 换个时机大概率成功;
      - 429 限流: 走 KEY 池换 key (见 _rotate_key_on_rate_limit), 不靠睡眠硬等。
    硬错误 (密钥失效 / 未知模型 / 参数错) 不重试, 直接走 cloud_alt → 本地降级,
    避免用无谓延迟掩盖真故障。次数夹紧到 [0, 5], 防止配置写错导致长时间挂死。
    """
    cfg = load_gateway_config()
    try:
        _n = int(cfg.get("cloud_max_retries"))
    except Exception:
        _n = 2
    try:
        _b = float(cfg.get("cloud_retry_backoff"))
    except Exception:
        _b = 3.0
    return max(0, min(_n, 5)), max(0.0, _b)


def _is_timeout_error(e) -> bool:
    """判定异常是否属"超时类" (可安全重试)。

    不 import openai (保持模块惰性), 靠类名 / status_code / 消息特征判定,
    兼容 openai SDK (APITimeoutError) 与底层 httpx (ReadTimeout/ConnectTimeout)。
    """
    _name = type(e).__name__.lower()
    if "timeout" in _name or "timedout" in _name:
        return True
    try:
        if getattr(e, "status_code", None) in (408, 504):
            return True
    except Exception:
        pass
    _s = str(e)
    _m = _s.lower()
    return ("timed out" in _m) or ("timeout" in _m) or ("read timed out" in _m) \
        or ("请求超时" in _s)


def _rate_error_check(e) -> bool:
    """判定异常是否属 429 限流 (复用 cloud_llm._is_rate_error; 模块缺失时按特征兜底)。"""
    try:
        from cloud_llm import _is_rate_error
        return bool(_is_rate_error(e))
    except Exception:
        _s = str(e)
        _m = _s.lower()
        return ("429" in _s) or ("rate limit" in _m) or ("too many requests" in _m) \
            or ("速率限制" in _s)


# ════════════════════════════════════════════════════════════════════
# 5. 统一模型解析 (修复 base_agent L336 仅硬编码 requirement 的天花板)
# ════════════════════════════════════════════════════════════════════
def resolve_cloud_model_generic(agent_name: str) -> str:
    """按 agent 名解析应使用的云端模型名 (v4.02.07 通用化, 不再仅限 requirement)。

    优先级: ai_flow 持久化 (cloud_review_enabled + cloud_model, 按 agent 名读取)
             > agent_model_map 的 cloud:<model> (经 get_agent_llm_spec 解码)
             > 历史约定 CLOUD_MODEL_NAME 环境变量 (默认 deepseek-chat)。
    保证向后兼容性, 且使所有智能体都能经 ai_flow / agent_model_map 独立配置云端模型。
    """
    # 1) ai_flow 持久化云端控制 (按 agent 名读取自身段, 而非硬编码 requirement)
    try:
        from ai_flow_config import get_config as _afc_get_config
        _afc = (_afc_get_config() or {}).get(agent_name, {})
        if _afc.get("cloud_review_enabled"):
            _cm = (_afc.get("cloud_model") or "").strip()
            if _cm:
                return _cm
    except Exception:
        pass
    # 2) agent_model_map 的 cloud:<model>
    try:
        from workflow_config import get_agent_llm_spec
        spec = get_agent_llm_spec(agent_name)
        if spec and spec.get("backend") == "cloud":
            _m = decode_cloud(spec.get("model", ""))
            if _m:
                return _m
    except Exception:
        pass
    # 3) 历史约定环境变量
    return os.getenv("CLOUD_MODEL_NAME", "deepseek-chat")


def cloud_usable_for(model: str) -> bool:
    """云端是否可用: 门控放行 且 模型属于已知云端目录。"""
    try:
        return cloud_gated() is None and is_cloud_model(model)
    except Exception:
        return False


def _cloud_alt_settings():
    """P0 多级降级 cloud_alt 层配置 (P1-2: 已由 env CLOUD_ALT_ENABLED=1 + 备选模型名启用)。

    返回 (enabled, alt_model, max_attempts):
      - enabled      : env CLOUD_ALT_ENABLED=1 且 gateway_config.cloud_alt_model 非空
      - alt_model    : 备选云模型名 (启用时), 未启用为 None
      - max_attempts : cloud_alt 层内最大尝试次数 (默认 3)
    未启用 (env 关或模型名为空) 时返回 (False, None, N), LLM 路由与旧行为字节等价。
    惰性/容错导入 cloud_key_pool (双路径, 与 cloud_llm 一致); 模块缺失/异常 → 保守全关。
    """
    try:
        try:
            from Core.cloud_key_pool import cloud_alt_enabled, cloud_alt_max_attempts
        except Exception:
            from cloud_key_pool import cloud_alt_enabled, cloud_alt_max_attempts  # type: ignore
        _max_attempts = int(cloud_alt_max_attempts() or 3)
        if not cloud_alt_enabled():
            return (False, None, _max_attempts)
        _alt_model = (load_gateway_config().get("cloud_alt_model") or "").strip()
        if not _alt_model:
            return (False, None, _max_attempts)
        return (True, _alt_model, _max_attempts)
    except Exception:  # pragma: no cover - 池模块缺失/异常 → 保守全关
        return (False, None, 3)


def _is_vl_agent(agent_name: str) -> bool:
    """判断是否为视觉(VI)智能体 (OCR 图像识别), 决定本地候选用 VL 模型。"""
    return agent_name in ("OCRRecognizeAgent", "OCRImageRecognizeAgent")


def _resolve_local_model_name_for(agent_name: str, is_vl: bool) -> str:
    """依据智能体返回应使用的本地 Ollama 模型名 (文本 or VL)。

    云端名泄漏时回退 LOCAL_MODEL_NAME, 避免直接喂给本地 Ollama 致 404。
    """
    try:
        from workflow_config import get_agent_model, get_vl_model
        if is_vl:
            m = get_vl_model()
        else:
            m = get_agent_model(agent_name)
    except Exception:
        m = None
    if not m:
        m = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
    if (m or "").startswith(CLOUD_PREFIX) or is_cloud_model(m or ""):
        m = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
    return m


def _resolve_plan(agent_name: str, is_vl: bool = False) -> dict:
    """纯决策层: 依据可配置降级链 + 云端可用性, 返回该智能体应使用的 backend / 模型 / 来源。

    不构造任何 LLM 实例 — 供 resolve_llm_for_agent (构造实例) 与
    describe_agent_effective (可观测回显) 共用, 杜绝两处决策逻辑分裂。
    降级链由 get_fallback_chain 提供 (default_fallback_chain 或 per-agent override),
    支持 ["cloud","local"] / ["local","cloud"] / ["local"] / ["cloud"] 等组合。
    """
    chain = get_fallback_chain(agent_name)
    spec = None
    try:
        from workflow_config import get_agent_llm_spec
        spec = get_agent_llm_spec(agent_name)
    except Exception:
        spec = None
    cloud_model = resolve_cloud_model_generic(agent_name)
    configured_cloud = bool(spec and spec.get("backend") == "cloud")
    cloud_ok = cloud_usable_for(cloud_model)
    # P0 多级降级回显 (不影响路由决策; cloud_alt 在 _GatewayCloudLLM.invoke 运行时才发生)
    _alt_enabled, _alt_model, _alt_attempts = _cloud_alt_settings()
    plan = {
        "agent": agent_name,
        "chain": chain,
        "configured_cloud": configured_cloud,
        "cloud_model": cloud_model,
        "cloud_usable": cloud_ok,
        "cloud_timeout": get_cloud_timeout(),
        "cloud_alt_enabled": _alt_enabled,
        "cloud_alt_model": _alt_model,
        "cloud_alt_max_attempts": _alt_attempts,
        "backend": None,
        "effective_model": None,
        "source": None,
    }
    for level in chain:
        if level == "cloud":
            if configured_cloud and cloud_ok:
                plan["backend"] = "cloud"
                plan["effective_model"] = cloud_model
                plan["source"] = "agent_model_map:cloud"
                break
            # cloud 不可用 (未配置/门控关闭/缺密钥), 按链继续尝试 local
        elif level == "local":
            local_m = _resolve_local_model_name_for(agent_name, is_vl)
            plan["backend"] = "local"
            plan["effective_model"] = local_m
            plan["source"] = ("fallback:local (cloud disabled/unusable)"
                              if (configured_cloud and not cloud_ok) else "local_default")
            break
    if plan["backend"] is None:
        # 链中无可用层级 (如 ["cloud"] 但 cloud 不可用) -> 兜底本地, 保证不中断
        local_m = _resolve_local_model_name_for(agent_name, is_vl)
        plan["backend"] = "local"
        plan["effective_model"] = local_m
        plan["source"] = "fallback:local (chain exhausted)"
    return plan


# ════════════════════════════════════════════════════════════════════
# 6. 统一本地构建 (集中治理 num_ctx/keep_alive/超时 + 云端名剥离)
# ════════════════════════════════════════════════════════════════════
def _build_local_ollama(agent_name: str, temperature: Optional[float] = None,
                        is_vl: bool = False, num_ctx: Optional[int] = None,
                        num_predict: Optional[int] = None, monitor: bool = True):
    """统一创建本地 ChatOllama 实例 (网关集中治理版)。

    monitor=True (默认): 额外包一层埋点 (_MonitoredLLM), 使本地调用进入 model_monitor。
    monitor=False: 由调用方自行埋点 (仅 _GatewayCloudLLM 云端降级本地路径使用,
                   该路径已在 invoke 内记 is_cloud=False 埋点, 避免重复计数)。

    与 base_agent._get_llm 行为等价: 从 workflow_config.get_llm_runtime() 读取
    num_ctx/keep_alive/request_timeout; 云端模型名泄漏时回退 LOCAL_MODEL_NAME 避免 404;
    调用级超时经 client_kwargs 注入底层 httpx; 末端包裹连接重置重试。
    """
    try:
        from langchain_ollama import ChatOllama
    except Exception:
        return None
    try:
        from workflow_config import get_agent_model, get_vl_model, get_llm_runtime
    except Exception:
        get_agent_model = lambda n: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
        get_vl_model = lambda: os.getenv("VL_MODEL_NAME", "qwen3-vl:2b")
        get_llm_runtime = lambda: {}

    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    use_gpu = os.getenv("USE_GPU", "true").lower() == "true"
    rt = get_llm_runtime() or {}

    if is_vl:
        model = get_vl_model()
        keep_alive = rt.get("image_keep_alive", "1m")
    else:
        model = get_agent_model(agent_name)
        # 云端模型名 (cloud: 前缀 / 云端目录模型名) 不可直接喂给本地 Ollama, 否则 404。
        if (model or "").startswith(CLOUD_PREFIX) or is_cloud_model(model or ""):
            model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
        keep_alive = rt.get("keep_alive", "5m")

    if num_ctx is None:
        num_ctx = rt.get("num_ctx", 4096)
    _rt_timeout = float(rt.get("request_timeout", 180))
    _env_to = os.getenv("OLLAMA_REQUEST_TIMEOUT")
    if _env_to:
        try:
            _rt_timeout = float(_env_to)
        except Exception:
            pass

    kwargs = {
        "model": model,
        "temperature": temperature if temperature is not None else 0.3,
        "base_url": base_url,
        "num_ctx": num_ctx,
        "keep_alive": keep_alive,
        "client_kwargs": {"timeout": _rt_timeout},
    }
    if num_predict is not None:
        kwargs["num_predict"] = num_predict
    if not use_gpu:
        kwargs["num_gpu"] = 0
    llm = ChatOllama(**kwargs)
    llm = _wrap_llm_with_retry(llm)
    # v4.02.99 C: 本地调用埋点 (此前本地 25 次调用全不入 request_log, 只能靠 httpx 日志清点)
    if monitor:
        llm = _wrap_llm_with_monitor(llm, agent_name, model)
    return llm


# ════════════════════════════════════════════════════════════════════
# 7. 网关云端适配器: 云端优先 + 失败自动降级本地 (对调用方透明)
# ════════════════════════════════════════════════════════════════════
class _GatewayCloudLLM(_CloudLLMAdapter):
    """云端优先适配器: 云端失败时自动降级本地 Ollama, 对调用方透明。

    成功走云端时 went_cloud=True; 降级时 went_cloud=False 且 fallback_reason 记录原因。
    调用方无需各自写 try/except CloudLLMUnavailable 降级 (消除设计文档所述重复降级逻辑)。
    """

    def __init__(self, agent_name: str, cloud_model: str, temperature: float = 0.3,
                 timeout: Optional[float] = None, num_ctx: Optional[int] = None,
                 num_predict: Optional[int] = None, is_vl: bool = False):
        # P0-1: 不再硬编码 120s。未显式传入时读 gateway_config.cloud_timeout
        #   (get_cloud_timeout 内含 env CLOUD_REQUEST_TIMEOUT 覆盖与安全兜底)。
        if timeout is None:
            timeout = get_cloud_timeout()
        super().__init__(cloud_model, temperature=temperature, timeout=timeout)
        self._agent_name = agent_name
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._is_vl = is_vl
        self.went_cloud = False
        self.fallback_reason = None

    def _rotate_key_on_rate_limit(self) -> bool:
        """P1-1: 单 key 路径遇 429 时, 若该厂商 KEY 池启用则换下一把 key 并重建客户端。

        返回 True = 已换 key (调用方可立即重试); False = 无池可换 (交还上层 cloud_alt/本地降级)。
        池启用时 get_cloud_client 通常已把 429 在 create 内轮换消化; 本方法是"池也轮完仍 429"
        或"池未覆盖该 vendor"时的最后一层换 key 机会, 与 _try_cloud_alt 的换 key 语义一致。
        """
        try:
            try:
                from Core.cloud_key_pool import KeyPool, key_pool_enabled
            except Exception:
                from cloud_key_pool import KeyPool, key_pool_enabled  # type: ignore
            if not key_pool_enabled():
                return False
            try:
                from cloud_llm import (MODEL_TO_VENDOR,
                                       _reverse_lookup_vendor_by_default_model)
                _vendor = (MODEL_TO_VENDOR.get(self.model)
                           or _reverse_lookup_vendor_by_default_model(self.model))
            except Exception:
                _vendor = None
            if not _vendor:
                return False
            _pool = KeyPool.instance()
            if not _pool.is_active(_vendor):
                return False
            _pool.advance_on_429(_vendor)
            # 丢弃缓存的客户端, 使下次 _ensure_client() 以新的 sticky key 重建
            self._client = None
            return True
        except Exception as e:  # pragma: no cover - 换 key 失败不影响降级主链路
            logger.warning("[网关] 429 换 key 失败, 交还上层降级: %s", e)
            return False

    def _try_cloud_alt(self, alt_model: str, max_attempts: int,
                       prompt_or_messages, kwargs) -> Optional[Any]:
        """P0 多级降级: 主云模型失败后, 换【备选云模型】+【轮换 key】重试 ≤max_attempts 次。

        Q2 语义 = 换 key + 换备选模型:
          - 构建备选模型客户端 (get_cloud_client, 池启用时其内部自带 429 轮换);
          - 每次尝试前若该 vendor 池 active 则 advance 一把 key (换 key);
          - 命中则返回 _CloudAIMessage, 全部失败 (限流耗尽 / 硬错误) 返回 None → 交还本地兜底。
        保守默认关: 仅当 _cloud_alt_settings() 返回启用时才由 invoke 调用。
        """
        _pool = None
        _alt_vendor = None
        try:
            try:
                from Core.cloud_key_pool import KeyPool, key_pool_enabled
            except Exception:
                from cloud_key_pool import KeyPool, key_pool_enabled  # type: ignore
            if key_pool_enabled():
                _pool = KeyPool.instance()
        except Exception:
            _pool = None
        try:
            from cloud_llm import (MODEL_TO_VENDOR,
                                   _reverse_lookup_vendor_by_default_model,
                                   _is_rate_error)
            _alt_vendor = (MODEL_TO_VENDOR.get(alt_model)
                          or _reverse_lookup_vendor_by_default_model(alt_model))
        except Exception:
            _alt_vendor = None
            def _is_rate_error(e):  # 兜底: 模块缺失时按特征判
                _m = str(e).lower()
                return ("429" in _m) or ("rate limit" in _m) or ("too many requests" in _m)

        alt_client = None
        try:
            # P1-2: 必须传 model_override, 否则实际请求模型名会被厂商卡片 default_model
            #   覆盖回主模型 (agnes-3.0-flash) —— "换备选模型"退化成"原地重试主模型"。
            alt_client = get_cloud_client(alt_model, timeout=self._timeout,
                                          model_override=alt_model)
        except Exception as e:
            logger.warning("[网关][cloud_alt] 备选云模型 %s 客户端不可用 (缺密钥/门控/未知): %s",
                          alt_model, e)
            return None

        messages = self._to_openai_messages(prompt_or_messages)
        last = None
        for _attempt in range(1, int(max_attempts) + 1):
            # 换 key: 池 active 时每次尝试前推进 sticky (主路径 429 已推进, 这里再换下一把)
            if _pool is not None and _alt_vendor and _pool.is_active(_alt_vendor):
                _pool.advance_on_429(_alt_vendor)
            try:
                resp = alt_client.chat.completions.create(
                    model=alt_model, messages=messages, temperature=self.temperature)
                content = ""
                if getattr(resp, "choices", None):
                    content = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                _ti = getattr(usage, "prompt_tokens", 0) if usage else 0
                _to = getattr(usage, "completion_tokens", 0) if usage else 0
                return _CloudAIMessage(content=content, input_tokens=_ti, output_tokens=_to)
            except Exception as e:
                last = e
                if not _is_rate_error(e):
                    logger.warning("[网关][cloud_alt] 备选云模型 %s 硬错误, 不再重试: %s", alt_model, e)
                    return None
                logger.warning("[网关][cloud_alt] 备选云模型 %s 限流 (第 %d/%d), 换 key 重试: %s",
                               alt_model, _attempt, int(max_attempts), e)
                continue
        if last is not None:
            logger.warning("[网关][cloud_alt] 备选云模型 %s 重试 %d 次仍限流, 交还本地兜底",
                          alt_model, int(max_attempts))
        return None

    def invoke(self, prompt_or_messages, **kwargs):
        _t0 = time.perf_counter()
        if _MM_OK:
            _mm_inc_active()
        try:
            try:
                # ── P1-1: 云端有限重试 (超时→退避重试; 429→KEY 池换 key 重试) ──
                #   目的: 单次抖动 / 瞬时限流不再直接降级 3B 本地模型。
                #   硬错误 (密钥失效/未知模型/参数错) 一律不重试, 立即交还 cloud_alt→本地降级,
                #   避免用无谓延迟掩盖真故障。次数/退避由 gateway_config 控制 (默认 2 次 / 3s)。
                _retries, _backoff = _cloud_retry_settings()
                resp = None
                _last_err = None
                for _attempt in range(0, _retries + 1):
                    try:
                        resp = super().invoke(prompt_or_messages, **kwargs)
                        break
                    except Exception as _e:
                        _last_err = _e
                        if _retries - _attempt <= 0:
                            raise
                        if _is_timeout_error(_e):
                            logger.warning(
                                "[网关] 智能体 %s 云端(%s)超时 (尝试 %d/%d), %.1fs 后重试: %s",
                                self._agent_name, self.model, _attempt + 1, _retries + 1,
                                _backoff, _e)
                            if _backoff > 0:
                                time.sleep(_backoff)
                            continue
                        if _rate_error_check(_e) and self._rotate_key_on_rate_limit():
                            logger.warning(
                                "[网关] 智能体 %s 云端(%s)限流 (尝试 %d/%d), 已换 key 重试",
                                self._agent_name, self.model, _attempt + 1, _retries + 1)
                            continue
                        raise
                if resp is None and _last_err is not None:
                    raise _last_err
                self.went_cloud = True
                _latency = (time.perf_counter() - _t0) * 1000.0
                if _MM_OK:
                    try:
                        _ti, _to = extract_ollama_usage(resp)
                        _mm_record_request(
                            agent_name=self._agent_name, model=self.model,
                            is_cloud=True, success=True,
                            tokens_in=_ti, tokens_out=_to, latency_ms=_latency)
                    except Exception as _mm_e:  # pragma: no cover
                        logger.debug("[模型监控] 云端埋点失败: %s", _mm_e)
                return resp
            except Exception as e:
                _latency = (time.perf_counter() - _t0) * 1000.0
                self.fallback_reason = f"{type(e).__name__}: {e}"
                logger.warning(
                    "[网关] 智能体 %s 云端(%s)调用失败, 自动降级本地 Ollama: %s",
                    self._agent_name, self.model, e)
                if _MM_OK:
                    try:
                        # 先记「云端失败」请求 (计入申请数, 与降级事件对应), 再记降级事件
                        _mm_record_request(
                            agent_name=self._agent_name, model=self.model,
                            is_cloud=True, success=False, latency_ms=_latency,
                            error=self.fallback_reason)
                    except Exception as _mm_e:  # pragma: no cover
                        logger.debug("[模型监控] 云端失败埋点失败: %s", _mm_e)
                # P0 多级降级: cloud_alt 层 (保守默认关; 未启用时完全走下方本地兜底, 字节等价)
                _alt_on, _alt_model, _alt_attempts = _cloud_alt_settings()
                _alt_attempted = bool(_alt_on and _alt_model)
                if _alt_attempted:
                    logger.warning(
                        "[网关][cloud_alt] 智能体 %s 主云模型 %s 失败, 换备选云模型 %s 重试",
                        self._agent_name, self.model, _alt_model)
                    _alt_resp = self._try_cloud_alt(
                        _alt_model, _alt_attempts, prompt_or_messages, kwargs)
                    if _alt_resp is not None:
                        self.went_cloud = True
                        self.fallback_reason = f"cloud→cloud_alt:{_alt_model}"
                        _alt_latency = (time.perf_counter() - _t0) * 1000.0
                        if _MM_OK:
                            try:
                                _mm_record_degradation(
                                    agent=self._agent_name, from_layer="cloud",
                                    to_layer="cloud_alt", reason=self.fallback_reason,
                                    model=_alt_model)
                            except Exception as _mm_e:  # pragma: no cover
                                logger.debug("[模型监控] cloud_alt 降级埋点失败: %s", _mm_e)
                            try:
                                _ti, _to = extract_ollama_usage(_alt_resp)
                                _mm_record_request(
                                    agent_name=self._agent_name, model=_alt_model,
                                    is_cloud=True, success=True,
                                    tokens_in=_ti, tokens_out=_to, latency_ms=_alt_latency)
                            except Exception as _mm_e:  # pragma: no cover
                                logger.debug("[模型监控] cloud_alt 埋点失败: %s", _mm_e)
                        return _alt_resp
                    if _MM_OK:
                        try:
                            _mm_record_degradation(
                                agent=self._agent_name, from_layer="cloud",
                                to_layer="cloud_alt",
                                reason=f"cloud_alt_exhausted:{_alt_model}",
                                model=_alt_model)
                        except Exception as _mm_e:  # pragma: no cover
                            logger.debug("[模型监控] cloud_alt 耗尽埋点失败: %s", _mm_e)
                # 本地兜底 (原逻辑; 降级 from_layer 依是否经过 cloud_alt, 供 P2 可观测闭环)
                if _MM_OK:
                    try:
                        _mm_record_degradation(
                            agent=self._agent_name,
                            from_layer="cloud_alt" if _alt_attempted else "cloud",
                            to_layer="local", reason=self.fallback_reason, model=self.model)
                    except Exception as _mm_e:  # pragma: no cover
                        logger.debug("[模型监控] 降级埋点失败: %s", _mm_e)
                # monitor=False: 本路径下方已自行记本地埋点, 避免与 _MonitoredLLM 重复计数
                local = _build_local_ollama(
                    self._agent_name, self.temperature, is_vl=self._is_vl,
                    num_ctx=self._num_ctx, num_predict=self._num_predict,
                    monitor=False)
                _latency = (time.perf_counter() - _t0) * 1000.0
                try:
                    lresp = local.invoke(prompt_or_messages, **kwargs)
                    if _MM_OK:
                        try:
                            _ti, _to = extract_ollama_usage(lresp)
                            _mm_record_request(
                                agent_name=self._agent_name, model="local-ollama",
                                is_cloud=False, success=True,
                                tokens_in=_ti, tokens_out=_to, latency_ms=_latency)
                        except Exception as _mm_e:  # pragma: no cover
                            logger.debug("[模型监控] 本地埋点失败: %s", _mm_e)
                    return lresp
                except Exception:
                    # 本地兜底也失败: 记一次失败请求 (不计费) 后原样抛出
                    if _MM_OK:
                        try:
                            _mm_record_request(
                                agent_name=self._agent_name, model="local-ollama",
                                is_cloud=False, success=False, latency_ms=_latency,
                                error=self.fallback_reason)
                        except Exception as _mm_e:  # pragma: no cover
                            logger.debug("[模型监控] 本地失败埋点失败: %s", _mm_e)
                    raise
        finally:
            if _MM_OK:
                _mm_dec_active()


# ════════════════════════════════════════════════════════════════════
# 8. 统一入口
# ════════════════════════════════════════════════════════════════════
def resolve_llm_for_agent(agent_name: str, temperature: Optional[float] = None,
                          is_vl: bool = False, num_ctx: Optional[int] = None,
                          num_predict: Optional[int] = None):
    """返回某智能体的统一 chat 模型 (网关路由模式, 消费可配置降级链)。

    - 降级链首层为 cloud 且该智能体配置云端且云端可用 -> _GatewayCloudLLM (云端优先, 失败自动降级本地)。
    - 否则 -> 返回本地 ChatOllama (经 _build_local_ollama, 含重试与超时治理)。
    降级顺序由 gateway_config.default_fallback_chain / agent_overrides 决定, 不再硬编码 cloud-first。
    """
    if temperature is None:
        temperature = 0.3
    plan = _resolve_plan(agent_name, is_vl=is_vl)
    if plan["backend"] == "cloud":
        try:
            return _GatewayCloudLLM(
                agent_name, plan["effective_model"], temperature=temperature,
                timeout=get_cloud_timeout(),  # P0-1: 由 gateway_config.cloud_timeout 驱动
                num_ctx=num_ctx, num_predict=num_predict, is_vl=is_vl)
        except Exception as e:
            logger.warning("[网关] 云端适配器创建失败, 降级本地 Ollama: %s", e)
    return _build_local_ollama(
        agent_name, temperature, is_vl=is_vl, num_ctx=num_ctx, num_predict=num_predict)


# ─── 可观测 / 配置写入辅助 (管理端「当前生效模型」回显 + 降级链 CRUD) ───

def _ollama_ping(timeout: float = 2.0) -> bool:
    """探测本地 Ollama 是否可达 (供监控面板回显本地可用性)。"""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as _r:
            return _r.status == 200
    except Exception:
        return False


def describe_agent_effective(agent_name: str) -> dict:
    """返回某智能体当前生效模型的可观测快照 (供管理端监控面板「当前生效模型」回显)。

    与 resolve_llm_for_agent 共用 _resolve_plan 决策逻辑, 保证回显即真实运行态,
    不另写一套决策导致 UI 与实际路由分裂。VL 智能体按自身类型判定本地模型。
    """
    is_vl = _is_vl_agent(agent_name)
    plan = _resolve_plan(agent_name, is_vl=is_vl)
    plan["ollama_available"] = _ollama_ping()
    return plan


def save_gateway_config(cfg: dict) -> dict:
    """保存网关配置到 data/gateway_config.json (清缓存, 即时生效)。

    校验: default_fallback_chain / agent_overrides.*.fallback_chain 元素仅限
    'cloud'/'local' 且非空。写失败或校验失败抛异常, 调用方应捕获并返回 400。
    """
    global _gateway_config_cache
    if not isinstance(cfg, dict):
        raise ValueError("gateway config 必须是 dict")
    _valid_levels = ("cloud", "local")
    chain = cfg.get("default_fallback_chain")
    if chain is not None:
        if (not isinstance(chain, list) or not chain or
                not all(c in _valid_levels for c in chain)):
            raise ValueError("default_fallback_chain 必须是非空列表, 元素仅限 'cloud'/'local'")
    overrides = cfg.get("agent_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError("agent_overrides 必须是 dict")
        for _a, _o in overrides.items():
            if isinstance(_o, dict) and "fallback_chain" in _o:
                _fc = _o["fallback_chain"]
                if (not isinstance(_fc, list) or not _fc or
                        not all(c in _valid_levels for c in _fc)):
                    raise ValueError(f"agent_overrides[{_a}].fallback_chain 非法")
    # 三层合并: 默认值 ← 磁盘现值 ← 本次提交 (仅已知字段, 避免未知字段污染 schema)。
    # ⚠ 修复既有破坏性行为: 此前只从 _DEFAULT_GATEWAY_CONFIG 起, 未提交的键会被"重置为默认"
    #    (例如仅提交 {"key_pool_enabled": false} 会把 cloud_alt_model 冲成空串 ⇒ 备选云层静默失效,
    #     也会摧毁管理端 S2「仅提交 agent_overrides」的保存)。现改为先继承磁盘现值, 局部更新。
    new = dict(_DEFAULT_GATEWAY_CONFIG)
    try:
        if os.path.exists(_GATEWAY_CONFIG_PATH):
            with open(_GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as _f:
                _cur = json.load(_f)
            if isinstance(_cur, dict):
                for _k in _DEFAULT_GATEWAY_CONFIG.keys():
                    if _k in _cur:
                        new[_k] = _cur[_k]
    except Exception as e:
        logger.warning("[网关] 合并现有 gateway_config 失败, 退回默认值: %s", e)
    for _k in _DEFAULT_GATEWAY_CONFIG.keys():
        if _k in cfg:
            new[_k] = cfg[_k]
    try:
        _dir = os.path.dirname(_GATEWAY_CONFIG_PATH)
        if _dir and not os.path.isdir(_dir):
            os.makedirs(_dir, exist_ok=True)
        with open(_GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
    except Exception as e:
        raise ValueError(f"写入 gateway_config.json 失败: {e}")
    _gateway_config_cache = new
    return new


# ─────────────────────────── 规则引擎兜底注册表 ───────────────────────────
_RULE_HANDLERS: Dict[str, Any] = {}


def register_rule_handler(task_type: str, fn):
    """注册某任务类型的规则引擎兜底处理器 (fn(system, user) -> str)。

    当 gw_request 所有 LLM 尝试均失败时, 若该 task_type 已注册处理器则调用之,
    返回确定性结果 (如正则先验 / 物理补全 / 推断规则), 使工作流不中断。
    """
    _RULE_HANDLERS[task_type] = fn
    logger.info("[网关] 已注册规则引擎处理器: task_type=%s", task_type)


class GatewayResponse:
    """gw_request 的统一返回信封。"""

    def __init__(self, content: str = "", model_used: str = "none",
                 attempts: Optional[List[Tuple[str, Any]]] = None,
                 fallback_reason: Optional[str] = None,
                 usage: Tuple[int, int] = (0, 0), ok: bool = False):
        self.content = content
        self.model_used = model_used
        self.attempts = attempts or []
        self.fallback_reason = fallback_reason
        self.usage = usage
        self.ok = ok

    def __repr__(self):
        return (f"GatewayResponse(ok={self.ok}, model_used={self.model_used!r}, "
                f"attempts={self.attempts!r}, fallback_reason={self.fallback_reason!r}, "
                f"usage={self.usage!r})")


def gw_request(agent: str, system: str, user: str,
               task_type: Optional[str] = None,
               temperature: Optional[float] = None,
               is_vl: bool = False, **kwargs) -> GatewayResponse:
    """高层一步调用: 经网关解析 LLM 并执行 (云端优先→本地兜底→规则引擎)。

    参数:
        agent      : 智能体名 (用于解析其模型配置)
        system     : 系统提示 (可为空)
        user       : 用户提示
        task_type  : 任务类型, 用于匹配规则引擎兜底处理器
        temperature: 温度
        is_vl      : 是否视觉模型
    返回: GatewayResponse (content / model_used / attempts / fallback_reason / usage / ok)
    """
    model = resolve_llm_for_agent(agent, temperature=temperature, is_vl=is_vl)
    attempts: List[Tuple[str, Any]] = []
    try:
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            _have_lc = True
        except Exception:
            _have_lc = False
        if _have_lc and system and user:
            msgs = [SystemMessage(content=system), HumanMessage(content=user)]
        else:
            msgs = (user or system or "")
        resp = model.invoke(msgs)
        used = "cloud" if getattr(model, "went_cloud", False) else "local"
        attempts.append((used, None))
        ti, to = extract_ollama_usage(resp, f"{system}\n\n{user}")
        return GatewayResponse(
            content=getattr(resp, "content", "") or "",
            model_used=getattr(model, "model", "?"),
            attempts=attempts,
            fallback_reason=getattr(model, "fallback_reason", None),
            usage=(ti, to), ok=True)
    except Exception as e:
        attempts.append(("error", f"{type(e).__name__}: {e}"))
        # 全 LLM 失败 → 规则引擎兜底
        handler = _RULE_HANDLERS.get(task_type)
        if handler:
            try:
                content = handler(system, user)
                return GatewayResponse(
                    content=content, model_used="rule_engine",
                    attempts=attempts,
                    fallback_reason="llm_unavailable->rule_engine",
                    usage=(0, 0), ok=True)
            except Exception as he:
                attempts.append(("rule_engine_error", str(he)))
        return GatewayResponse(
            content="", model_used="none", attempts=attempts,
            fallback_reason=str(e), usage=(0, 0), ok=False)


if __name__ == "__main__":
    import pprint
    pprint.pprint({
        "gateway_config": load_gateway_config(),
        "requirement_cloud_model": resolve_cloud_model_generic("RequirementAgent"),
        "review_cloud_model": resolve_cloud_model_generic("ReviewAgent"),
    })
