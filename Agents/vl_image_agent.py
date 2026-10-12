# -*- coding: utf-8 -*-
"""
VLImageAgent — OCR 图像识别智能体 (v4.01.023)
==============================================

职责单一：把**电机铭牌 / 技术手册 / 参数表的图片**直接交给多模态视觉语言模型
(qwen3-vl:2b)，输出 RMxprt 11 大类的结构化参数。

与 OCRRecognizeAgent 的分工
--------------------------
    OCRRecognizeAgent  : 文本通道  图片→Tesseract→文本→正则+LLM→参数
    VLImageAgent (本类): 图像通道  图片───────────→VL 视觉直读→参数

两者共用同一套后处理闭环（型号推断 / 物理校验 / 优先级 / 置信度 / 白名单），
后处理仍复用 OCRRecognizeAgent 的实现，避免知识库出现两份。

为什么要独立成 Agent
--------------------
1. 模型生命周期不同：VL 模型（qwen3-vl:2b，约 2GB）与文本模型分开预检、预热、
   降级，避免一方不可用把另一方拖下水；
2. 图像预处理是 VL 专有环节：超大图会撑爆 2B 模型的视觉 token 预算，
   必须先等比压缩；
3. 可独立扩展多图批量识别（一台电机的铭牌 + 手册页一起喂）。

模型配置
--------
    VL_MODEL_NAME          默认 qwen3-vl:2b-instruct   (T600 4GB 可跑，2B 视觉模型)
    OCRImageRecognizeAgent 管理端可独立配置该智能体模型
    OLLAMA_BASE_URL        默认 http://127.0.0.1:11434
    VL_TIMEOUT             默认 300 (秒)
    VL_MAX_EDGE            默认 1280 (px, 长边上限；超出等比压缩)
"""
import base64
import io
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from Agents.base_agent import BaseAgent

# v4.01.023: 支持在管理端为 OCR 图像识别师独立配置模型
# v4.01.024: 改用 get_vl_model() —— 管理端选"使用默认模型"时回退到视觉模型而非文本模型
try:
    from Core.workflow_config import get_agent_model, get_vl_model, get_vl_timeout, get_llm_runtime
except Exception:
    get_agent_model = None
    get_vl_model = None
    get_vl_timeout = None
    get_llm_runtime = None

logger = logging.getLogger(__name__)

DEFAULT_VL_MODEL = "qwen3-vl:2b-instruct"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_EDGE = 1280
AVAILABILITY_TTL = 60          # 模型可用性缓存 (秒)


class VLImageAgent(BaseAgent):
    """OCR 图像识别智能体：图片 → qwen3-vl:2b-instruct → 结构化电机参数"""

    def __init__(self, ocr_agent=None):
        """
        Args:
            ocr_agent: OCRRecognizeAgent 实例。用于复用字段定义与后处理闭环。
                       不传时惰性创建（便于单独调用本智能体）。
        """
        super().__init__(
            name="VLImageAgent",
            description="多模态图像识别智能体 (qwen3-vl:2b-instruct), 直接从铭牌/手册图片提取电机参数",
        )
        self._ocr_agent = ocr_agent
        self._available: Optional[bool] = None
        self._check_ts: float = 0.0
        self._last_model: str = ""
        self._stats = {
            "total_requests": 0,
            "success": 0,
            "empty": 0,
            "failed": 0,
            "model_missing": 0,
        }

    # ──────────────────────────────────────────────────────────
    # 模型 / 超时 (v4.01.024: 动态读取, 管理端改配置即时生效)
    # ──────────────────────────────────────────────────────────
    @property
    def _model_name(self) -> str:
        """当前 VL 模型名。

        优先级: 环境变量 VL_MODEL_NAME > 管理端 OCRImageRecognizeAgent 配置
                > DEFAULT_VL_MODEL (qwen3-vl:2b-instruct)
        每次访问动态读取, 因此在管理端切换模型后无需重启后端。
        模型名变化时自动作废可用性缓存。
        """
        env = os.getenv("VL_MODEL_NAME")
        if env:
            name = env
        elif get_vl_model:
            name = get_vl_model() or DEFAULT_VL_MODEL
        else:
            name = DEFAULT_VL_MODEL
        if name != self._last_model:
            self._last_model = name
            self._available = None      # 模型换了 → 重新预检
            self._check_ts = 0.0
        return name

    @property
    def _timeout(self) -> int:
        """VL 等待上限 (秒), 默认 300 (5 分钟)"""
        env = os.getenv("VL_TIMEOUT")
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        if get_vl_timeout:
            return get_vl_timeout()
        return DEFAULT_TIMEOUT

    # ──────────────────────────────────────────────────────────
    # 依赖
    # ──────────────────────────────────────────────────────────
    @property
    def ocr_agent(self):
        """惰性获取 OCRRecognizeAgent（后处理闭环 + 字段定义的唯一来源）"""
        if self._ocr_agent is None:
            from Agents.case_motor_agent import OCRRecognizeAgent
            self._ocr_agent = OCRRecognizeAgent()
        return self._ocr_agent

    @property
    def base_url(self) -> str:
        return os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL)

    @property
    def model_name(self) -> str:
        return self._model_name

    # ──────────────────────────────────────────────────────────
    # 模型可用性
    # ──────────────────────────────────────────────────────────
    def check_available(self, force: bool = False) -> bool:
        """
        检查 VL 模型是否已 pull 到本地 Ollama。

        区分两种"不可用"：
          · Ollama 服务不通         → service_down
          · 服务在线但模型没装      → model_not_found（提示 ollama pull）
        """
        now = time.time()
        if not force and self._available is not None and (now - self._check_ts) < AVAILABILITY_TTL:
            return self._available
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code != 200:
                self._available, self._check_ts = False, now
                return False
            local = {m.get("name", "") for m in r.json().get("models", [])}
            target = self._model_name
            # Ollama tag 可能带 :latest，做前缀兼容比较
            self._available = (
                target in local
                or target.split(":")[0] in {m.split(":")[0] for m in local}
            )
            self._check_ts = now
            logger.info("[VLImageAgent] 模型 %s 可用性: %s", target, self._available)
            return self._available
        except Exception as e:
            logger.warning("[VLImageAgent] 可用性检查失败: %s", e)
            self._available, self._check_ts = False, now
            return False

    def get_model_info(self) -> Dict[str, Any]:
        """供 /api/case-motor/ocr-vl 与客户端状态栏展示"""
        available = self.check_available()
        return {
            "agent": self.name,
            "model": self._model_name,
            "available": available,
            "base_url": self.base_url,
            "timeout": self._timeout,
            "max_edge": int(os.getenv("VL_MAX_EDGE", str(DEFAULT_MAX_EDGE))),
            "install_hint": "" if available else f"ollama pull {self._model_name}",
            "stats": dict(self._stats),
        }

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    # ──────────────────────────────────────────────────────────
    # 图像预处理
    # ──────────────────────────────────────────────────────────
    def preprocess_image(self, image_bytes: bytes,
                         image_format: str = "JPEG") -> Tuple[bytes, Dict[str, Any]]:
        """
        等比压缩过大的图片。

        2B 级视觉模型的图像 token 预算很紧：4000px 的手机拍照直接喂进去，
        要么显存爆，要么细节被粗暴下采样反而识别更差。统一压到长边 VL_MAX_EDGE，
        既省显存又能保住铭牌文字的可读性。

        Pillow 不可用时原样返回（不阻断主流程）。
        """
        meta = {"resized": False, "orig_kb": len(image_bytes) // 1024}
        max_edge = int(os.getenv("VL_MAX_EDGE", str(DEFAULT_MAX_EDGE)))
        try:
            from PIL import Image
        except ImportError:
            meta["note"] = "Pillow 未安装, 跳过压缩"
            return image_bytes, meta

        try:
            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size
            meta["orig_size"] = f"{w}x{h}"
            if max(w, h) <= max_edge:
                return image_bytes, meta

            scale = max_edge / float(max(w, h))
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img = img.resize(new_size, Image.LANCZOS)

            buf = io.BytesIO()
            fmt = "JPEG" if (image_format or "").upper() in ("JPEG", "JPG") else "PNG"
            img.save(buf, format=fmt, quality=92)
            out = buf.getvalue()
            meta.update({
                "resized": True,
                "new_size": f"{new_size[0]}x{new_size[1]}",
                "new_kb": len(out) // 1024,
            })
            logger.info("[VLImageAgent] 图片压缩 %s → %s (%dKB → %dKB)",
                        meta["orig_size"], meta["new_size"],
                        meta["orig_kb"], meta["new_kb"])
            return out, meta
        except Exception as e:
            logger.warning("[VLImageAgent] 图片预处理失败(按原图继续): %s", e)
            meta["error"] = str(e)
            return image_bytes, meta

    # ──────────────────────────────────────────────────────────
    # 核心: 视觉直读
    # ──────────────────────────────────────────────────────────
    def extract(self, image_bytes: bytes, image_format: str = "JPEG") -> Dict[str, Dict]:
        """调用 VL 模型，返回 {field: {value, unit, label, raw_match}}（未做后处理）"""
        import requests
        agent = self.ocr_agent
        try:
            img, _meta = self.preprocess_image(image_bytes, image_format)
            img_b64 = base64.b64encode(img).decode("utf-8")

            # v4.01.023: 只用铭牌可见子集, 缩短上下文+生成, 防止 2B 视觉模型超时/幻觉
            use_subset = True
            system_prompt, field_list = agent._build_vl_prompt(use_subset=use_subset)
            valid_fields = (
                set(agent._VL_FIELD_SUBSET) if use_subset
                else {f[0] for f in agent._FIELD_DEFS}
            )
            # 字段名加引号, 强调 param 必须是下列英文名之一 (减少 Model/Power 等自创名)
            quoted_fields = ", ".join(f'"{n}"' for n in valid_fields)
            user_prompt = (
                "这是一张电机铭牌或技术手册的图片。请识别图片中可见的电机参数, "
                "只输出 JSON, 不要任何解释或 markdown。\n"
                '格式: {"params": [{"param": "字段英文名", "value": 值, "raw_match": "图中原文"}]}\n'
                f"param 必须是下列英文名之一: {quoted_fields}\n"
                "只输出图片中确实可见的参数, 不可见的字段不要输出。\n"
                "特别注意: 若图片含电机型号(如 Y2-200L-4 / YE3-160M-6 / 三相异步电动机), "
                "务必输出 motor_type=该型号; 若含极数(如末尾数字 4/6)或'极'字样, 务必输出 poles。"
            )

            # v4.01.023: qwen3-vl:2b 强制开启 thinking, 且 think 放 options 里无效;
            # 正确做法: 顶层 "think": false + 尾部 assistant 预填 <think>\\n\\n</think>\\n\\n,
            # 二者缺一不可。模型从该前缀续写 → thinking 关闭, 12~80s 出结果, 保留图片注入。
            # v5.3: keep_alive 取 image_keep_alive (默认 "1m"), 视觉模型为短突发,
            #        识别结束后尽快释放显存, 与文本模型 (keep_alive=5m) 时间错开,
            #        避免双 4B 模型 (qwen3-vl:4b + qwen3:4b ≈5.4GB) 在 T600 4GB 上叠加死锁。
            _keep_alive = "1m"
            try:
                if get_llm_runtime:
                    _keep_alive = get_llm_runtime().get("image_keep_alive", "1m")
            except Exception:
                pass
            payload = {
                "model": self._model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt, "images": [img_b64]},
                    {"role": "assistant", "content": "<think>\n\n</think>\n\n"},
                ],
                "format": "json",
                "stream": False,
                "think": False,
                "keep_alive": _keep_alive,
                "options": {"temperature": 0.1, "num_predict": 2048},
            }
            timeout = self._timeout
            logger.info("[VLImageAgent] 调用 %s (%s, %dKB, 子集%d字段, timeout=%ds)...",
                        self._model_name, image_format, len(img) // 1024,
                        len(valid_fields), timeout)
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout)
            if resp.status_code != 200:
                logger.warning("[VLImageAgent] 模型返回 %d: %s",
                               resp.status_code, resp.text[:300])
                return {}
            content = resp.json().get("message", {}).get("content", "")
            if not content:
                return {}
            return self._parse_response(content, agent, valid_fields)
        except Exception as e:
            logger.warning("[VLImageAgent] 调用失败: %s", e)
            return {}

    def _parse_response(self, content: str, agent,
                        valid_fields: Optional[set] = None) -> Dict[str, Dict]:
        """解析模型输出（兼容 {"params":[...]} 与顶层数组，含截断容错）"""
        from Agents.case_motor_agent import _vl_normalize_param, _vl_is_skip_value
        parsed = agent._robust_json_parse(content)
        if isinstance(parsed, dict):
            parsed = parsed.get("params", [])
        if not isinstance(parsed, list):
            logger.warning("[VLImageAgent] 返回非数组, content[:200]=%s", content[:200])
            return {}

        if valid_fields is None:
            valid_fields = {f[0] for f in agent._FIELD_DEFS}
        results: Dict[str, Dict] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            param = item.get("param", "")
            value = item.get("value")
            raw_match = item.get("raw_match", "")
            # v4.01.023: 归一字段名 (Model→motor_type 等) + 过滤 "未识别" 等无效值
            param = _vl_normalize_param(param, valid_fields)
            if not param or param not in valid_fields or _vl_is_skip_value(value):
                continue
            value = agent._convert_value(param, value)
            fd = next((f for f in agent._FIELD_DEFS if f[0] == param), None)
            results[param] = {
                "value": value,
                "unit": fd[2] if fd else "",
                "label": fd[1] if fd else param,
                "raw_match": raw_match or str(value),
            }
        logger.info("[VLImageAgent] 提取到 %d 个参数", len(results))
        return results

    # ──────────────────────────────────────────────────────────
    # 对外主入口
    # ──────────────────────────────────────────────────────────
    def run(self, image_bytes: bytes = b"", image_format: str = "JPEG",
            **kwargs) -> Dict[str, Any]:
        """
        识别单张图片，返回与 OCRRecognizeAgent.run() 同构的结果。

        Returns:
            {
              params: {field: {value, unit, label, raw_match, source, priority,
                               confidence, ...}},
              vl_count, regex_count, inferred_count,
              validation, warnings, elapsed,
              model_status: healthy | unhealthy | model_not_found | no_result,
              vl_model, vl_model_available, vl_agent
            }
        """
        t0 = time.time()
        self._stats["total_requests"] += 1
        agent = self.ocr_agent

        # ── 1) 前置健康检查：服务 → 模型 ──
        healthy = agent._check_ollama_health()
        available = self.check_available()
        if not healthy:
            logger.warning("[VLImageAgent] Ollama 服务不可用, 跳过 VL 识别")
            self._stats["failed"] += 1
            model_status, vl_params = "unhealthy", {}
        elif not available:
            logger.warning("[VLImageAgent] 模型 %s 未安装, 跳过 VL 识别", self._model_name)
            self._stats["model_missing"] += 1
            model_status, vl_params = "model_not_found", {}
        else:
            vl_params = self.extract(image_bytes, image_format)
            # v5.3: VL 图像识别为短突发, 识别后立即卸载视觉模型, 与文本模型时间错开,
            #        避免双 4B 模型 (qwen3-vl:4b + qwen3:4b ≈5.4GB) 在 T600 4GB 上叠加死锁。
            #        (extract 的 keep_alive=image_keep_alive 为兜底, 此处为确定性立即释放)
            try:
                from Core.ollama_helper import unload_model
                unload_model(self._model_name)
            except Exception as _e:
                logger.debug("[VLImageAgent] 识别后卸载视觉模型失败(忽略): %s", _e)
            if vl_params:
                self._stats["success"] += 1
                model_status = "healthy"
            else:
                self._stats["empty"] += 1
                model_status = "no_result"

        # ── 2) 后处理闭环（复用 OCRRecognizeAgent，保证与文本通道口径一致）──
        merged = self._postprocess(vl_params, agent)
        inferred_count = sum(1 for v in merged.values() if v.get("source") == "inferred")
        validation, corrections = agent._validate_consistency(merged)
        for corr in corrections:
            f = corr["field"]
            if f in merged:
                old = merged[f]["value"]
                merged[f]["value"] = corr["new_value"]
                merged[f]["correction"] = \
                    f"校验修正: {old} → {corr['new_value']} ({corr['reason']})"
        for field, info in merged.items():
            info["priority"] = agent._PRIORITY_MAP.get(field, "low")
            info["confidence"] = agent._calc_confidence(field, info, merged)
        merged = self._filter_whitelist(merged)

        warnings = [v["message"] for v in validation if v["status"] == "warning"]
        elapsed = round(time.time() - t0, 3)
        logger.info("[VLImageAgent] 识别完成: VL%d + 推断%d = %d参数, %.2fs, status=%s",
                    len(vl_params), inferred_count, len(merged), elapsed, model_status)

        return {
            "params": merged,
            "vl_count": len(vl_params),
            "regex_count": 0,
            "inferred_count": inferred_count,
            "validation": validation,
            "warnings": warnings,
            "elapsed": elapsed,
            "model_status": model_status,
            "vl_model": self._model_name,
            "vl_model_available": available if healthy else False,
            "vl_agent": self.name,
        }

    def run_batch(self, images: List[Tuple[bytes, str]]) -> Dict[str, Any]:
        """
        多图识别并合并（铭牌 + 手册页 + 参数表一起喂）。

        合并策略：同一字段以**置信度更高**者胜出；置信度相同则先到先得。
        这样铭牌上的权威值不会被手册里的通用值覆盖。
        """
        t0 = time.time()
        merged: Dict[str, Dict] = {}
        per_image = []
        for idx, (img, fmt) in enumerate(images):
            r = self.run(image_bytes=img, image_format=fmt)
            per_image.append({
                "index": idx,
                "count": len(r.get("params", {})),
                "model_status": r.get("model_status"),
                "elapsed": r.get("elapsed"),
            })
            for field, info in r.get("params", {}).items():
                old = merged.get(field)
                if old is None or info.get("confidence", 0) > old.get("confidence", 0):
                    info = dict(info)
                    info["from_image"] = idx
                    merged[field] = info
        return {
            "params": merged,
            "image_count": len(images),
            "per_image": per_image,
            "elapsed": round(time.time() - t0, 3),
            "vl_model": self._model_name,
            "vl_agent": self.name,
        }

    # ──────────────────────────────────────────────────────────
    # 后处理
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _postprocess(vl_params: Dict[str, Dict], agent) -> Dict[str, Dict]:
        """打 source 标记 + 型号推断补全"""
        merged: Dict[str, Dict] = {}
        for field, info in vl_params.items():
            merged[field] = {
                "value": info.get("value", ""),
                "unit": info.get("unit", ""),
                "label": info.get("label", field),
                "raw_match": info.get("raw_match", ""),
                "source": "vl",
            }
        for field, (value, reason) in agent._infer_from_model(merged).items():
            if field in merged:
                continue
            fd = next((f for f in agent._FIELD_DEFS if f[0] == field), None)
            merged[field] = {
                "value": value,
                "unit": fd[2] if fd else "",
                "label": fd[1] if fd else field,
                "raw_match": "",
                "source": "inferred",
                "inferred_reason": reason,
            }
        return merged

    @staticmethod
    def _filter_whitelist(merged: Dict[str, Dict]) -> Dict[str, Dict]:
        """只保留数据库字段白名单内的参数，防止脏字段写库"""
        try:
            from Core.case_motor_db import MOTOR_PARAM_FIELDS
            whitelist = {f[0] for f in MOTOR_PARAM_FIELDS} | {"motor_type"}
            dropped = [k for k in merged if k not in whitelist]
            if dropped:
                logger.info("[VLImageAgent] 过滤非白名单字段: %s", dropped)
                for k in dropped:
                    merged.pop(k, None)
        except Exception as e:
            logger.debug("[VLImageAgent] 白名单过滤跳过: %s", e)
        return merged


# ──────────────────────────────────────────────────────────────
# 单例
# ──────────────────────────────────────────────────────────────
_vl_image_agent: Optional[VLImageAgent] = None


def get_vl_image_agent(ocr_agent=None) -> VLImageAgent:
    """全局单例（保留模型可用性缓存与统计，避免每次请求重新探测）"""
    global _vl_image_agent
    if _vl_image_agent is None:
        _vl_image_agent = VLImageAgent(ocr_agent=ocr_agent)
    elif ocr_agent is not None and _vl_image_agent._ocr_agent is None:
        _vl_image_agent._ocr_agent = ocr_agent
    return _vl_image_agent
