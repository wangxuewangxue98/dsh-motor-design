# -*- coding: utf-8 -*-
"""
Ollama 显存看门狗 (v5.2)
========================

后台守护线程, 周期轮询 Ollama /api/ps 的 size_vram 总和:
  - 当总和超过阈值 (默认 3.5 GB, T600 4GB 安全边界) 时,
    自动对「非当前流程必需」模型调用 keep_alive=0 卸载,
    防止双 4B 模型叠加 (≈5.4GB) 触发 T600 4GB 显存边界死锁。

设计要点
--------
  - 由 app.py 启动时调用 start_watchdog() 拉起, 全局单例 (仅一个线程);
  - 阈值 / 周期 / 必需模型清单来自 workflow_config.get_llm_runtime().watchdog,
    运行时改配置即时生效 (每轮重新读取);
  - 看门狗本身失败绝不抛出, 仅记录日志, 不影响 Flask 主服务;
  - 卸载动作复用 ollama_helper.unload_non_essential, 与 orchestrator 释放逻辑统一。
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_watchdog_thread = None
_watchdog_running = None  # threading.Event
_watchdog_lock = threading.Lock()


def _load_config() -> dict:
    try:
        from workflow_config import get_llm_runtime
        return get_llm_runtime().get("watchdog", {})
    except Exception as e:
        logger.debug("[watchdog] 读取看门狗配置失败, 用内置默认: %s", e)
        return {
            "enabled": True,
            "interval_seconds": 30,
            "vram_threshold_gb": 3.5,
            "essential_models": [],
        }


def _loop() -> None:
    """看门狗主循环 (在独立守护线程内运行)"""
    global _watchdog_running
    logger.info("[watchdog] 显存看门狗线程启动")
    while _watchdog_running is not None and _watchdog_running.is_set():
        try:
            cfg = _load_config()
            interval = max(5, int(cfg.get("interval_seconds", 30)))
            threshold = float(cfg.get("vram_threshold_gb", 3.5))
            essential = cfg.get("essential_models", []) or []

            from ollama_helper import get_loaded_vram_gb, unload_non_essential
            vram = get_loaded_vram_gb()
            if vram > threshold:
                unloaded = unload_non_essential(essential)
                logger.warning(
                    "[watchdog] size_vram=%.2fGB 超阈值 %.2fGB, 已卸载 %d 个非必需模型 (essential=%s)",
                    vram, threshold, unloaded, essential)
            else:
                logger.debug("[watchdog] size_vram=%.2fGB 正常 (<%.2fGB)", vram, threshold)
        except Exception as e:
            logger.debug("[watchdog] 轮询异常(忽略): %s", e)
        # 轮询间隔 (即使异常也等待, 避免空转打满 CPU)
        for _ in range(max(1, int(interval))):
            if _watchdog_running is None or not _watchdog_running.is_set():
                return
            time.sleep(1)
    logger.info("[watchdog] 显存看门狗线程退出")


def start_watchdog() -> bool:
    """启动看门狗守护线程 (全局单例, 重复调用安全)。

    返回是否成功启动 (已运行/配置禁用也视为 True, 不报错)。
    """
    global _watchdog_thread, _watchdog_running
    with _watchdog_lock:
        # 已在运行 → 直接返回
        if _watchdog_running is not None and _watchdog_running.is_set() \
                and _watchdog_thread is not None and _watchdog_thread.is_alive():
            logger.info("[watchdog] 已在运行, 跳过重复启动")
            return True
        # 读取配置: 禁用则不启动
        cfg = _load_config()
        if not cfg.get("enabled", True):
            logger.info("[watchdog] 配置 disabled, 不启动看门狗")
            return True
        # 新建 Event + 线程
        _watchdog_running = threading.Event()
        _watchdog_running.set()
        _watchdog_thread = threading.Thread(target=_loop, name="ollama-vram-watchdog", daemon=True)
        _watchdog_thread.start()
        logger.info("[watchdog] 已启动 (interval=%ss, threshold=%sGB)",
                    cfg.get("interval_seconds", 30), cfg.get("vram_threshold_gb", 3.5))
        return True


def stop_watchdog() -> None:
    """停止看门狗 (供测试 / 优雅退出使用)"""
    global _watchdog_running
    if _watchdog_running is not None:
        _watchdog_running.clear()
    logger.info("[watchdog] 已请求停止")
