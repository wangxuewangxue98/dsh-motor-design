"""
智能体基类 - 所有智能体继承此类
提供审计日志、错误处理、耗时统计等通用能力
"""
import os
import time
import uuid
import logging
from datetime import datetime
from typing import Any, Optional
import sys
sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")
import audit_logger

# v4.02.07 H-3 二期 T1: 系统级 LLM 网关 (Core/llm_gateway.py)
# base_agent 的 LLM 解析/路由/降级统一委托网关; 云端适配器与 token 计量原语由网关提供并由此重导出。
from llm_gateway import (
    resolve_cloud_model_generic,
    resolve_llm_for_agent,
    _build_local_ollama,
    gw_request,
    register_rule_handler,
    GatewayResponse,
)

logger = logging.getLogger("BaseAgent")


# ──────────────────────────────────────────────────────────
# v5.3-B: Ollama 连接重置重试 (防 T600 4GB 显存压力下 worker 崩溃致工作流 abort)
# ──────────────────────────────────────────────────────────
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
    # 去重保序
    return tuple(dict.fromkeys(errs))


_CONNECTION_ERRORS = _collect_connection_errors()


class _RetryingLLM:
    """把 ChatOllama 包一层连接重置重试 (v5.3-B 修复版).

    ⚠ 关键: ChatOllama 是 pydantic v2 模型, 禁止给未声明字段赋值 (setattr 会抛
    ValidationError: object has no field 'invoke'), 因此**绝不能**用 setattr 打补丁。
    这里改用组合代理: 持有一个内部 ChatOllama 实例, 对 invoke/stream/batch/ainvoke
    做重试后委派, 不触碰原 pydantic 实例的任何属性。

    Ollama 在 T600 4GB 显存压力下偶发 worker 崩溃 (TCP RST 10054), 自愈通常只需数秒。
    重试 + 指数退避可让瞬断自愈后自动恢复, 避免单个 Agent 调用即 abort 整个 AI 团队工作流。
    """

    _RETRY_METHODS = ("invoke", "stream", "batch", "ainvoke")

    def __init__(self, llm, max_retries=3, base_backoff=1.5):
        # 用 object.__setattr__ 绕过任何 __setattr__ 限制
        object.__setattr__(self, "_llm", llm)
        object.__setattr__(self, "_max_retries", max_retries)
        object.__setattr__(self, "_base_backoff", base_backoff)
        # 镜像常用只读属性, 供调用方读取 (如 llm.model / llm.keep_alive)
        for _a in ("model", "model_name", "temperature", "keep_alive", "num_ctx", "base_url"):
            if hasattr(llm, _a):
                object.__setattr__(self, _a, getattr(llm, _a))

    def __getattr__(self, name):
        # 仅当本职未定义该属性时, 委派给内部 llm (invoke/stream/... 已显式定义, 不会走到这)
        return getattr(object.__getattribute__(self, "_llm"), name)

    def _retry_call(self, method_name, *args, **kwargs):
        _llm = object.__getattribute__(self, "_llm")
        _max = object.__getattribute__(self, "_max_retries")
        _base = object.__getattribute__(self, "_base_backoff")
        _last = None
        for _attempt in range(_max):
            try:
                return getattr(_llm, method_name)(*args, **kwargs)
            except _CONNECTION_ERRORS as _e:
                _last = _e
                if _attempt < _max - 1:
                    _wait = _base * (2 ** _attempt)
                    logger.warning(
                        "[BaseAgent] LLM 连接重置(尝试 %d/%d), %ss 后重试: %s",
                        _attempt + 1, _max, _wait, _e)
                    time.sleep(_wait)
                    continue
                logger.error("[BaseAgent] LLM 连接重试耗尽(%d): %s", _max, _e)
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
    """从 ChatOllama 返回的 AIMessage 提取真实 token 用量 (input/output)。

    v4.06: 收敛阵营成本计量从启发式粗估升级为真实计量。
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
    # 回退: 基于 prompt/输出文本启发式
    _ti = _estimate_chars(prompt) // 4 if prompt else 0
    _to = _estimate_chars(getattr(resp, "content", "")) // 4 if resp is not None else 0
    return _ti, _to


# ──────────────────────────────────────────────────────────
# v4.02.06 H-3 二期 T5: 云端 LLM 适配器 (OpenAI 兼容 -> LangChain invoke 接口)
# 使收敛阵营 / OCR 等 Agent 的既有 llm.invoke(prompt) / extract_ollama_usage 代码零改动。
# ──────────────────────────────────────────────────────────
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
            from cloud_llm import get_cloud_client
            self._client = get_cloud_client(self.model, timeout=self._timeout)
        return self._client

    @staticmethod
    def _to_openai_messages(prompt_or_messages):
        if isinstance(prompt_or_messages, str):
            return [{"role": "user", "content": prompt_or_messages}]
        out = []
        for m in prompt_or_messages:
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


class BaseAgent:
    """所有智能体的基类"""

    # v5.1: 各智能体默认温度 (未显式传入 _get_llm(temperature=...) 时使用)
    # 子类可在 _get_llm 调用时按自身需求覆盖; 此处仅作兜底默认值
    DEFAULT_LLM_TEMPERATURE = 0.3

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.project_id: Optional[str] = None

    # ──────────────────────────────────────────────────────────
    # v5.1: 统一 LLM 创建 (集中治理 num_ctx / keep_alive)
    # ──────────────────────────────────────────────────────────
    def _get_llm(self, temperature: Optional[float] = None, is_vl: bool = False,
                 num_ctx: Optional[int] = None, num_predict: Optional[int] = None):
        """统一创建本地 ChatOllama 实例 (v4.02.07 委托网关集中治理)。

        实际构建由 Core.llm_gateway._build_local_ollama 完成 (num_ctx/keep_alive/超时/重试
        集中治理, 云端模型名泄漏自动回退 LOCAL_MODEL_NAME 避免 404)。
        temperature: 优先调用方显式值, 否则子类 self.llm_temperature, 再否则 DEFAULT_LLM_TEMPERATURE。
        num_ctx/num_predict: 透传, 供 OCR 等需大上下文的场景覆盖运行时默认。
        """
        if temperature is None:
            temperature = getattr(self, "llm_temperature", self.DEFAULT_LLM_TEMPERATURE)
        return _build_local_ollama(
            self.name, temperature, is_vl=is_vl, num_ctx=num_ctx, num_predict=num_predict)

    # ──────────────────────────────────────────────────────────
    # v4.02.06 H-3 二期 T4: 云端客户端统一接入 (经 Core.cloud_llm 工厂)
    # 供 report/requirement/review 三智能体及 T5 其余智能体复用, 消除各自硬编码 OpenAI 构建。
    # 设计: 模型名经 _resolve_cloud_model 解析 (ai_flow 设置优先, 回退 CLOUD_MODEL_NAME 历史约定);
    #       _get_cloud_client 惰性+按模型缓存地构造云端 OpenAI 兼容客户端; 密钥缺失/被门控/
    #       未知模型时抛 CloudLLMUnavailable, 由调用方捕获后降级本地 Ollama (行为不变)。
    # ──────────────────────────────────────────────────────────
    def _resolve_cloud_model(self) -> str:
        """解析本智能体应使用的云端模型名 (v4.02.07 委托网关通用解析)。

        网关 resolve_cloud_model_generic(agent) 按 agent 名读取 ai_flow 持久化云端控制
        (不再硬编码 requirement 段) > agent_model_map 的 cloud:<model> > CLOUD_MODEL_NAME,
        使所有智能体都能独立配置云端模型, 行为向后兼容。
        """
        return resolve_cloud_model_generic(self.name)

    def _cloud_usable(self) -> bool:
        """云端是否可用: 门控放行 且 模型属于已知云端目录。

        密钥缺失不在此时拦截, 由 _get_cloud_client 抛 CloudLLMUnavailable 供调用方降级本地。
        """
        try:
            from cloud_llm import cloud_gated, is_cloud_model
            return cloud_gated() is None and is_cloud_model(self._resolve_cloud_model())
        except Exception:
            return False

    def _get_cloud_client(self, model=None):
        """惰性 + 按模型缓存地返回云端 OpenAI 兼容客户端 (统一工厂 cloud_llm.get_cloud_client)。

        缺密钥 / 被门控 / 未知模型 -> 抛 CloudLLMUnavailable, 调用方须捕获并降级本地 Ollama。
        """
        if model is None:
            model = self._resolve_cloud_model()
        if not hasattr(self, "_cloud_clients"):
            self._cloud_clients = {}
        if model not in self._cloud_clients:
            from cloud_llm import get_cloud_client
            self._cloud_clients[model] = get_cloud_client(model)
        return self._cloud_clients[model]

    def _is_cloud_configured(self) -> bool:
        """本智能体是否被配置为云端模型 (按 agent_model_map 的 cloud: 前缀严格判定)。

        与 _resolve_cloud_model 不同: 后者在本地配置下会回退到 CLOUD_MODEL_NAME 历史约定
        (一个已知云端模型名), 若直接据此判断会误把"未配云端"当成云端。本方法只看 spec.backend。
        """
        try:
            from workflow_config import get_agent_llm_spec
            spec = get_agent_llm_spec(self.name)
        except Exception:
            return False
        return spec.get("backend") == "cloud"

    def _get_llm_or_cloud(self, temperature: Optional[float] = None, is_vl: bool = False,
                          num_ctx: Optional[int] = None, num_predict: Optional[int] = None):
        """本地/云端统一 LLM 入口 (v4.02.07 委托网关路由模式)。

        经网关 resolve_llm_for_agent 解析: 本智能体配置为云端模型 (cloud: 前缀, 且云端可用)
        -> 返回 _GatewayCloudLLM (云端优先, 失败自动降级本地, 对调用方透明);
        否则 -> 返回本地 ChatOllama。所有智能体经此单一入口, 路由与降级集中在网关,
        不再由各 Agent 各自实现 (消除设计文档所述重复降级逻辑)。
        """
        if temperature is None:
            temperature = getattr(self, "llm_temperature", self.DEFAULT_LLM_TEMPERATURE)
        return resolve_llm_for_agent(
            self.name, temperature=temperature, is_vl=is_vl,
            num_ctx=num_ctx, num_predict=num_predict)

    def _new_project_id(self) -> str:
        self.project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        return self.project_id

    def _log(self, action: str, parameters: Any = None, result: Any = None,
             status: str = "success", duration_ms: int = 0):
        """记录审计日志"""
        audit_logger.log(
            agent_name=self.name,
            action=action,
            parameters=parameters,
            result=result,
            status=status,
            duration_ms=duration_ms,
            project_id=self.project_id
        )

    def run(self, **kwargs) -> dict:
        """执行智能体任务（子类实现）"""
        raise NotImplementedError

    def safe_run(self, **kwargs) -> dict:
        """带错误处理的执行包装"""
        start = time.time()
        try:
            if not self.project_id:
                self._new_project_id()
            result = self.run(**kwargs)
            duration = int((time.time() - start) * 1000)
            self._log(
                action=f"run:{self.name}",
                parameters=kwargs,
                result=result,
                status="success",
                duration_ms=duration
            )
            return result
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            self._log(
                action=f"run:{self.name}",
                parameters=kwargs,
                result=str(e),
                status="error",
                duration_ms=duration
            )
            return {"error": str(e), "agent": self.name}
