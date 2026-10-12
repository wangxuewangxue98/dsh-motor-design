# -*- coding: utf-8 -*-
"""
Ollama 共享辅助 (v5.1 / v5.2 / v5.3)
=====================================

集中管理 Ollama 模型生命周期的底层 HTTP 调用, 供以下场景复用:
  - v5.1: orchestrator 在「流程结束 / 人工检查点」后显式释放非必需模型
  - v5.2: ollama_watchdog 看门狗轮询 /api/ps 的 size_vram, 超阈值卸载
  - v5.3: OCR 图像识别 (VL 模型) 结束后立即卸载, 与文本模型时间错开

设计原则
--------
  - 全部调用失败静默降级 (返回 False / 空值), 绝不向上抛异常阻断主流程;
  - 仅依赖标准库 urllib + 可选 requests, 不引入新依赖;
  - 模型释放统一走 /api/generate + keep_alive:0 (Ollama 官方卸载语义),
    与 app.py 既有 /api/models/unload 端点保持同一套底层协议。
"""
import json
import logging
import os
import urllib.request as _urllib_req
import urllib.error as _urllib_err

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def ollama_base_url() -> str:
    """Ollama 服务地址 (环境变量可覆盖)"""
    return os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)


def _post_json(url: str, payload: dict, timeout: int = 10):
    """POST JSON 到 Ollama, 返回解析后的 dict; 失败返回 None (静默降级)"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = _urllib_req.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except _urllib_err.HTTPError as e:
        # 4xx/5xx: 读取错误体便于排查, 但整体仍视为失败
        body = ""
        try:
            body = json.loads(e.read()).get("error", "")
        except Exception:
            pass
        logger.debug("[ollama_helper] HTTP %d: %s", e.code, body or e.reason)
        return None
    except Exception as e:
        logger.debug("[ollama_helper] 请求失败 (%s): %s", url, e)
        return None


def get_loaded_models() -> list:
    """返回当前 /api/ps 中已加载模型列表: [{"name", "size_vram", "size", "context_length"}, ...]"""
    try:
        url = f"{ollama_base_url()}/api/ps"
        with _urllib_req.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        return data.get("models", []) or []
    except Exception as e:
        logger.debug("[ollama_helper] /api/ps 读取失败: %s", e)
        return []


def get_loaded_vram_gb() -> float:
    """已加载模型 size_vram 总和 (GB)。用于看门狗阈值判断。"""
    total = 0.0
    for m in get_loaded_models():
        try:
            total += float(m.get("size_vram", 0) or 0)
        except (TypeError, ValueError):
            pass
    return total / (1024 ** 3)


def unload_model(model_name: str = "") -> bool:
    """卸载模型。

    model_name 为空 → 卸载 /api/ps 中所有已加载模型 (常用于流程结束/人工检查点);
    model_name 非空 → 仅卸载指定模型 (常用于 VL 图像识别结束后单独卸载)。

    返回是否成功发出卸载请求 (不保证模型已彻底释放)。
    """
    base = ollama_base_url()
    if model_name:
        ok = _post_json(f"{base}/api/generate",
                        {"model": model_name, "keep_alive": 0}, timeout=10)
        logger.info("[ollama_helper] 卸载模型 %s: %s", model_name, "ok" if ok is not None else "failed")
        return ok is not None
    # 卸载全部
    models = get_loaded_models()
    if not models:
        return True
    success = True
    for m in models:
        name = m.get("name", "")
        if not name:
            continue
        ok = _post_json(f"{base}/api/generate",
                        {"model": name, "keep_alive": 0}, timeout=10)
        if ok is None:
            success = False
        logger.info("[ollama_helper] 卸载模型 %s: %s", name, "ok" if ok is not None else "failed")
    return success


def unload_non_essential(essential_models: list = None) -> int:
    """卸载所有「非必需」模型, 保留 essential_models 清单中的模型。

    返回成功卸载的模型数量。
    """
    essential = {m.split(":")[0] for m in (essential_models or [])}
    essential |= {m for m in (essential_models or [])}
    base = ollama_base_url()
    unloaded = 0
    for m in get_loaded_models():
        name = m.get("name", "")
        if not name:
            continue
        # 前缀匹配 (忽略 tag) 判定是否必需
        if name in essential or name.split(":")[0] in essential:
            continue
        ok = _post_json(f"{base}/api/generate",
                        {"model": name, "keep_alive": 0}, timeout=10)
        if ok is not None:
            unloaded += 1
            logger.info("[ollama_helper] 看门狗卸载非必需模型 %s", name)
    return unloaded


def release_checkpoint_models() -> bool:
    """人工检查点 / 流程结束后释放非必需模型 (v5.1)。

    默认卸载全部已加载模型, 把显存完全交还, 避免模型在人工等待期间空占显存。
    下一次 LLM 调用会按需重载 (keep_alive=5m 仍保证流程内连续阶段不反复重载)。

    静默降级: 任何异常均记录 debug 日志并返回 False, 不影响主流程。
    """
    try:
        return unload_model("")
    except Exception as e:
        logger.debug("[ollama_helper] 检查点释放模型失败(忽略): %s", e)
        return False


def recover_ollama_deadlock() -> bool:
    """死锁应急恢复 (v5.3 沿用已验证流程)。

    当 T600 4GB 上出现 >4B 模型死锁 (HTTP 在线但 /api/generate 超时、VRAM 居高不下) 时,
    按既定顺序恢复:
        taskkill ollama → ollama serve (后台) → curl 预热 → 验证 /api/ps VRAM 回落

    ⚠️ 该操作会中断所有在途推理, 仅应在确认死锁时调用 (看门狗无法自愈时)。
    返回是否成功重启 Ollama 服务。
    """
    try:
        import subprocess
        logger.warning("[ollama_helper] 执行死锁应急恢复: taskkill ollama ...")
        # 1. 终止现有 ollama 进程
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception as e:
            logger.debug("[ollama_helper] taskkill 失败(忽略): %s", e)
        # 2. 重新拉起 ollama serve (后台, 不阻塞)
        subprocess.Popen(["ollama", "serve"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008)  # DETACHED_PROCESS
        # 3. 等待服务就绪 (轮询最多 20s)
        import time
        for _ in range(20):
            if get_loaded_models() is not None:
                # /api/ps 可通说明服务已起 (可能返回空列表)
                try:
                    with _urllib_req.urlopen(f"{ollama_base_url()}/api/ps", timeout=3):
                        logger.info("[ollama_helper] Ollama 服务已恢复")
                        return True
                except Exception:
                    pass
            time.sleep(1)
        logger.warning("[ollama_helper] Ollama 服务恢复超时")
        return False
    except Exception as e:
        logger.warning("[ollama_helper] 死锁恢复失败: %s", e)
        return False
