"""
cloud_key_pool.py - 云端多 KEY 池 + 自适应限速 (P0 落地, v4.02.81+)

设计铁律 (保守默认关):
  - 本模块是 P0「多 KEY 池下沉 + 多级降级」的地基。新增、不改任何调用方。
  - 仅当开关打开 (B1: gateway_config.key_pool_enabled, env CLOUD_KEY_POOL_ENABLED 可应急覆盖)
    且某厂商卡配置了非空 api_keys 池时, get_cloud_client 才走池取 key + 429 sticky 轮换
    + per-endpoint×key 限速。
  - 未启用 (默认) 时, 生产 LLM 路由 100% 与现状字节等价 (本模块不被调用)。

蓝本 (直接复用, 不重造轮子):
  - RateGuard: 下沉自 Scripts/_cloud_rate_guard.py (AIMD per-slot 限速, slot=host#key[-6:])。
  - KeyPool sticky 轮换: 蓝本来自 Scripts/_run_aiteam_20_test.py::_rotate_key (成功不轮换,
    仅 429 后 advance; 试满整池轮才退避) 与 _verify_sticky_key.py 的三阶段断言。

循环依赖约束:
  - 本模块 **不 import cloud_llm / cloud_services_config 顶层** (仅运行时惰性 import
    cloud_services_config.pool_of), 与 cloud_services_config 相同的边界约定, 避免导入副作用。
  - openai 包在调用方 (cloud_llm) 处惰性导入, 本模块不涉及。
"""

from __future__ import annotations

import os
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("cloud_key_pool")


# ─────────────────────────── 环境变量开关 (默认全关) ───────────────────────────
def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return float(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default


# ── B1 (v4.02.91): 开关真值解析 — env 显式设置优先 → gateway_config 同名键 → 默认关 ──
#   背景: 此前 key_pool_enabled/cloud_alt_enabled/cloud_alt_max_attempts 只读 env,
#   管理端「模型总路由卡片」写入 gateway_config 后无人读取 ⇒ UI 开关点了不生效 (GAP3),
#   与云降级「死配置」同类。改为 config 优先 + env 应急双向覆盖, 且惰性 import llm_gateway
#   (与 cloud_services_config 相同的循环依赖边界约定, 不在模块顶层 import)。
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _env_explicit_flag(name: str):
    """读取 env 开关; 未设置/空串/非开关值返回 None (交由 gateway_config 决定)。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    v = str(raw).strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return None


def _gw_config_value(key: str, default=None):
    """读取 gateway_config 的键。

    优先【直读 JSON 文件】而非 import llm_gateway.load_gateway_config:
      1) 规避双模块实例 (Core.llm_gateway vs llm_gateway) 的进程内缓存分裂;
      2) 规避 Manager 守护 spawn 后端时【继承旧 env】导致的真值不一致;
      3) gateway_config 是小文件, 读取开销可忽略 (<0.1ms)。
    import 路径仅作兜底。
    """
    try:
        import json as _json
        _p = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "gateway_config.json")
        with open(_p, "r", encoding="utf-8") as _f:
            _d = _json.load(_f)
        if isinstance(_d, dict) and key in _d:
            return _d.get(key, default)
    except Exception:
        pass
    try:
        try:
            from Core.llm_gateway import load_gateway_config
        except Exception:
            from llm_gateway import load_gateway_config  # type: ignore
        return load_gateway_config().get(key, default)
    except Exception:
        return default


def _resolve_switch(cfg_key: str, env_name: str, default: bool = False) -> bool:
    """B1: 开关真值解析 — 【gateway_config 优先】, 无该键时回退 env, 再 default。

    为何 config 优先 (而非 env 优先): 后端由 Manager 守护 spawn, 会【继承 Manager 进程的
    env】(Manager 常驻, 其 env 为启动时快照)。若 env 优先, 管理端 UI 翻转开关会被继承的
    旧 env 静默否决 (实测: PUT key_pool_enabled=false 后运行态仍 True)。故以 config 为真源,
    env 仅作"未配置时的引导默认值"。
    """
    _c = _gw_config_value(cfg_key, None)
    if _c is not None:
        if isinstance(_c, bool):
            return _c
        if isinstance(_c, (int, float)):
            return bool(_c)
        if isinstance(_c, str):
            v = _c.strip().lower()
            if v in _TRUTHY:
                return True
            if v in _FALSY:
                return False
    _e = _env_explicit_flag(env_name)
    if _e is not None:
        return _e
    return default


def key_pool_enabled() -> bool:
    """多 KEY 池是否启用 (B1: gateway_config.key_pool_enabled, env 可应急覆盖; 默认关)。"""
    return _resolve_switch("key_pool_enabled", "CLOUD_KEY_POOL_ENABLED", False)


def cloud_alt_enabled() -> bool:
    """多级降级 cloud_alt 层是否启用 (B1: gateway_config.cloud_alt_enabled, env 应急覆盖; 默认关)。"""
    return _resolve_switch("cloud_alt_enabled", "CLOUD_ALT_ENABLED", False)


def cloud_alt_max_attempts() -> int:
    """cloud_alt 层内换 key/模型的最大尝试次数 (B1: 优先 gateway_config, env 兜底, 默认 3)。

    此前只读 env CLOUD_ALT_MAX_ATTEMPTS ⇒ 管理端同名字段写入 gateway_config 却无人读取,
    属与云降级同类的「死写」。现改为 config 优先, 让管理端字段真正生效。
    """
    _c = _gw_config_value("cloud_alt_max_attempts", None)
    try:
        if _c is not None:
            return int(float(_c))
    except (TypeError, ValueError):
        pass
    _e = os.getenv("CLOUD_ALT_MAX_ATTEMPTS")
    if _e is not None and str(_e).strip():
        try:
            return int(float(_e))
        except (TypeError, ValueError):
            pass
    return 3


# ─────────────────────────── slot / 限速常量 (与 Scripts 蓝本一致) ───────────────────────────
def rate_min_interval() -> float:
    return _env_float("RATE_GUARD_MIN_INTERVAL", 1.5)


def rate_max_interval() -> float:
    return _env_float("RATE_GUARD_MAX_INTERVAL", 60.0)


_RATE_INIT = 2.0      # 起始间隔 (保守, 实测安全区 ~5s/次)
_RATE_UP_STREAK = 5   # 连续 N 次成功才提速
_RATE_DOWN_FACTOR = 1.6
_RATE_UP_FACTOR = 0.8


def host_of(url: str) -> str:
    """把 base_url 归一成端点 host (cn 与 apihub 是两个独立限额桶)。"""
    try:
        h = urlparse(str(url or "")).netloc.strip().lower()
        return h or "default"
    except Exception:  # pragma: no cover
        return "default"


def rate_slot_for(base_url: str, key: str = "") -> str:
    """限速槽位: 按「端点 + key 尾6位」划分 (Q4 选 1 = per-endpoint×key)。

    多 key 并发场景下, 带上 key 后 N 把 key = N 条独立通道, 并发才真能提速;
    而 429 换 key 时 KeyPool 仍切端点 (与 _cloud_keys_default 蓝本一致), 避免同端点空转。
    """
    h = host_of(base_url)
    k = str(key or "").strip()
    return f"{h}#{k[-6:]}" if k else h


# ─────────────────────────── RateGuard (AIMD, 下沉自 Scripts) ───────────────────────────
class RateGuard:
    """按 slot 独立维护最小请求间隔的自适应限速器 (AIMD 拥塞控制)。"""

    def __init__(self, min_interval: float = None, max_interval: float = None,
                 init: float = _RATE_INIT, up_streak: int = _RATE_UP_STREAK):
        self.min = float(min_interval if min_interval is not None else rate_min_interval())
        self.max = float(max_interval if max_interval is not None else rate_max_interval())
        self.up_streak = int(up_streak)
        self._init = float(init)
        self._iv: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._ok: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _touch(self, slot: str):
        if slot not in self._iv:
            self._iv[slot] = self._init
            self._last[slot] = 0.0
            self._ok[slot] = 0

    def acquire(self, slot: str) -> float:
        """占用一个发送名额, 必要时阻塞。返回实际等待秒数。"""
        with self._lock:
            self._touch(slot)
            now = time.time()
            wait = (self._last[slot] + self._iv[slot]) - now
            # 预约: 无论是否需等待, 都把 _last 推到本次发送时刻之后, 使并发调用者依次排队。
            self._last[slot] = now + max(wait, 0.0)
        if wait > 0:
            time.sleep(wait)
        return max(wait, 0.0)

    def on_success(self, slot: str):
        with self._lock:
            self._touch(slot)
            self._ok[slot] += 1
            if self._ok[slot] >= self.up_streak:
                self._ok[slot] = 0
                self._iv[slot] = max(self.min, self._iv[slot] * _RATE_UP_FACTOR)

    def on_rate_limited(self, slot: str) -> float:
        with self._lock:
            self._touch(slot)
            self._ok[slot] = 0
            self._iv[slot] = min(self.max, max(self._iv[slot] * _RATE_DOWN_FACTOR, self.min))
            return self._iv[slot]

    def interval(self, slot: str) -> float:
        with self._lock:
            self._touch(slot)
            return self._iv[slot]


#: 进程内共享单例 —— pipeline 与评价两条链路必须共用同一套 slot, 否则互相打爆端点。
RATE_GUARD = RateGuard()


# ─────────────────────────── KeyEntry / KeyPool ───────────────────────────
class KeyEntry:
    """池元素: 一把 key + 其端点 + 权重/启用/审计标签。"""

    __slots__ = ("key", "endpoint", "weight", "enabled", "label")

    def __init__(self, key: str, endpoint: str = "", weight: int = 1,
                 enabled: bool = True, label: str = ""):
        self.key = (key or "").strip()
        self.endpoint = (endpoint or "").strip()
        self.weight = int(weight) if int(weight or 0) > 0 else 1
        self.enabled = bool(enabled)
        self.label = (label or "").strip()

    def slot(self) -> str:
        return rate_slot_for(self.endpoint or "", self.key)

    def masked(self) -> str:
        k = self.key
        return (k[:6] + "..." + k[-4:]) if len(k) > 10 else "***"

    def __repr__(self):  # pragma: no cover
        return (f"KeyEntry({self.label or self.masked()}@{self.endpoint or '?'}, "
                f"w={self.weight}, en={self.enabled})")


class KeyPool:
    """单厂商的多 KEY 池, sticky 选择 + 429 后 advance。

    语义 (与 Scripts/_verify_sticky_key.py 三阶段断言一致):
      - get_next(advance=False) 沿用【上次成功】的 key (不轮换)。
      - get_next(advance=True)  (仅在 429 之后调用) 切到下一把 (按 weight 展开)。
      - advance 是粘性指针: 成功则粘在当前 key, 429 才漂移。
    池按 vendor 维护 (Q1 选 1 = 挂厂商卡 api_keys)。
    """

    _INSTANCE: Optional["KeyPool"] = None
    _INST_LOCK = threading.Lock()

    def __init__(self):
        # vendor -> (entries, 当前 sticky 下标)
        self._pools: Dict[str, List[KeyEntry]] = {}
        self._sticky: Dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "KeyPool":
        if cls._INSTANCE is None:
            with cls._INST_LOCK:
                if cls._INSTANCE is None:
                    cls._INSTANCE = cls()
        return cls._INSTANCE

    # ── 池加载 (惰性读 cloud_services_config, 避免顶层循环依赖) ──
    def _load_vendor(self, vendor: str) -> List[KeyEntry]:
        try:
            try:
                from Core.cloud_services_config import pool_of
            except Exception:
                from cloud_services_config import pool_of  # type: ignore
            raw = pool_of(vendor) or []
        except Exception as e:  # pragma: no cover
            logger.warning("[cloud_key_pool] 读取 %s 池失败: %s", vendor, e)
            return []
        entries: List[KeyEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            k = str(item.get("key") or "").strip()
            if not k:
                continue
            entries.append(KeyEntry(
                key=k,
                endpoint=str(item.get("endpoint") or "").strip(),
                weight=int(item.get("weight") or 1),
                enabled=bool(item.get("enabled", True)),
                label=str(item.get("label") or "").strip(),
            ))
        return entries

    def active_entries(self, vendor: str) -> List[KeyEntry]:
        """返回某 vendor 当前启用 (enabled=True) 的池条目 (按 weight 展开后顺序保留)。"""
        with self._lock:
            if vendor not in self._pools:
                self._pools[vendor] = self._load_vendor(vendor)
                self._sticky.setdefault(vendor, 0)
            pool = self._pools[vendor]
        # 按 weight 展开 (weight=n → 该 key 在展开序列里出现 n 次), 保留原始顺序
        expanded: List[KeyEntry] = []
        for e in pool:
            if e.enabled and e.key:
                expanded.extend([e] * e.weight)
        return expanded

    def is_active(self, vendor: str) -> bool:
        """池是否可用: 全局开关开 且 该 vendor 有非空启用池。"""
        if not key_pool_enabled():
            return False
        return bool(self.active_entries(vendor))

    def current(self, vendor: str) -> Optional[KeyEntry]:
        """当前 sticky 指向的条目 (未启用/空池返回 None → 调用方回退单 key)。"""
        expanded = self.active_entries(vendor)
        if not expanded:
            return None
        idx = self._sticky.get(vendor, 0) % len(expanded)
        return expanded[idx]

    def advance_on_429(self, vendor: str) -> Optional[KeyEntry]:
        """429 后调用: 粘移到下一把 key (粘性指针 +1)。返回新指向条目。"""
        expanded = self.active_entries(vendor)
        if not expanded:
            return None
        with self._lock:
            self._sticky[vendor] = (self._sticky.get(vendor, 0) + 1) % len(expanded)
        return expanded[self._sticky[vendor]]

    def sticky_index(self, vendor: str) -> int:
        """当前 sticky 指向的展开序列下标 (供 create 循环定位池客户端; 空池返回 0)。

        与 active_entries(vendor) 的顺序严格对齐 (同一份权重展开), 故下标可直接
        索引 [池客户端列表]。线程安全: 读 sticky 不加锁是竞态可接受 (与 Scripts 蓝本一致)。
        """
        expanded = self.active_entries(vendor)
        if not expanded:
            return 0
        return self._sticky.get(vendor, 0) % len(expanded)

    def get_next(self, vendor: str, advance: bool = False) -> Optional[KeyEntry]:
        """粘性取 key: advance=False 沿用上次成功 key; advance=True 才切下一把。"""
        if advance:
            return self.advance_on_429(vendor)
        return self.current(vendor)

    def invalidate(self, vendor: Optional[str] = None):
        """配置变更后清缓存 (vendor=None 清全部), 下次读取重新加载。"""
        with self._lock:
            if vendor is None:
                self._pools.clear()
                self._sticky.clear()
            else:
                self._pools.pop(vendor, None)
                self._sticky.pop(vendor, None)


# 便捷别名 (调用方/单测可直接 import)
_POOL = KeyPool.instance


def pool_for(vendor: str) -> List[KeyEntry]:
    return KeyPool.instance().active_entries(vendor)


__all__ = [
    "key_pool_enabled", "cloud_alt_enabled", "cloud_alt_max_attempts",
    "rate_min_interval", "rate_max_interval",
    "host_of", "rate_slot_for",
    "RateGuard", "RATE_GUARD",
    "KeyEntry", "KeyPool", "pool_for",
]


if __name__ == "__main__":
    import json
    print(json.dumps({
        "key_pool_enabled": key_pool_enabled(),
        "cloud_alt_enabled": cloud_alt_enabled(),
        "cloud_alt_max_attempts": cloud_alt_max_attempts(),
        "host_of_cn": host_of("https://api.agnes-ai.cn/v1"),
        "host_of_hub": host_of("https://apihub.agnes-ai.com/v1"),
        "slot_sample": rate_slot_for("https://apihub.agnes-ai.com/v1", "sk-abcde12345"),
    }, ensure_ascii=False, indent=2))
