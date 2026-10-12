"""
model_monitor.py - 模型监控服务 (6.2 模型监控面板后端, Plan B 落地)

设计要点 (Plan B: 配置预算 + token 累计):
  - 监控数据全部来自「统一 LLM 网关」的真实埋点 (llm_gateway._GatewayCloudLLM.invoke),
    不再依赖 audit_logger.agent_metrics 死表, 也不与现有埋点重复。
  - 内存环形缓冲保存最近请求 (_request_log) 与降级事件 (_degradation_events);
    降级事件与「当月云费用累计」额外落 SQLite (model_monitor.db) 以跨越进程重启。
  - 费用按 cost_tracker 两档单价计算: 云端 (deepseek 等) 有价 (3/6 元每百万 token),
    本地 Ollama 免费 (Plan B 口径: 配额=付费云预算, 本地不计费)。
  - 配额: 读取 .env 的 MONTHLY_BUDGET (默认 1000 元), 累计当月云费用, 剩余 < 20% 告警。
  - 所有 DB 操作 try/except 包裹, 监控故障绝不影响主链路。

线程安全: 全局 _lock 保护共享结构; 计数器 inc/dec 实时活跃请求数。
"""
import os
import sqlite3
import threading
from collections import deque, defaultdict
from datetime import datetime
from typing import Dict, Any, List, Optional

# 复用成本核算两档单价 (Core 已在 sys.path)
from Core.cost_tracker import (
    CLOUD_INPUT_PRICE,   # 3.0 元/百万 token
    CLOUD_OUTPUT_PRICE,  # 6.0
    LOCAL_INPUT_PRICE,   # 0.6 (本地单价, 本模块按 Plan B 不计入预算)
    LOCAL_OUTPUT_PRICE,  # 1.2
)

_DB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "KnowledgeBase")
_DB_PATH = os.path.join(_DB_DIR, "model_monitor.db")

WARN_THRESHOLD = 20  # 剩余配额 < 20% 触发告警

_lock = threading.Lock()
_request_log: "deque" = deque(maxlen=5000)          # 最近请求 (含时间戳/模型/费用/延迟)
_degradation_events: "deque" = deque(maxlen=500)    # 降级事件内存副本
_active_count = 0                                   # 进行中网关请求数 (实时活跃请求数)
_month_cloud_cost: Dict[str, float] = defaultdict(float)  # month_key -> 云费用累计 (内存缓存, 启动从库加载)

# ─────────────────────────── 时间工具 ───────────────────────────
def _month_key(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m")


def _now_ts() -> float:
    return datetime.now().timestamp()


# ─────────────────────────── DB (降级事件 + 月云费用) ───────────────────────────
def _ensure_db():
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS degradation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, ts_epoch REAL,
            agent TEXT, from_layer TEXT, to_layer TEXT,
            reason TEXT, model TEXT, created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_epoch REAL,
            ts_iso TEXT,
            agent TEXT,
            model TEXT,
            is_cloud INTEGER,
            success INTEGER,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost REAL,
            latency_ms REAL,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_cloud_cost (
            month TEXT PRIMARY KEY,
            cost REAL DEFAULT 0.0,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def _load_month_cost_from_db():
    """启动时把已持久化的当月云费用载入内存缓存 (进程重启不丢预算累计)。"""
    try:
        conn = _ensure_db()
        rows = conn.execute(
            "SELECT month, cost FROM monthly_cloud_cost").fetchall()
        conn.close()
        with _lock:
            for m, c in rows:
                _month_cloud_cost[m] = float(c or 0.0)
    except Exception:
        pass


def _persist_month_cost(month: str, cost: float):
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO monthly_cloud_cost (month, cost, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(month) DO UPDATE SET cost=cost+?, updated_at=?",
            (month, cost, datetime.now().isoformat(), cost, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


# 模块导入即加载历史月费用 (不会失败, 失败静默)
_load_month_cost_from_db()


def _load_recent_from_db(max_rows: int = 5000, max_age_days: int = 7):
    """启动时把最近请求明细回填内存环形缓冲, 使进程重启后申请数/趋势不丢失。

    仅回填近 max_age_days 天且不超过 max_rows 条 (deque maxlen=5000), 避免无限增长。
    """
    try:
        conn = _ensure_db()
        rows = conn.execute(
            "SELECT ts_epoch, ts_iso, agent, model, is_cloud, success, "
            "tokens_in, tokens_out, cost, latency_ms, error "
            "FROM request_log "
            "WHERE ts_epoch >= ? "
            "ORDER BY ts_epoch DESC LIMIT ?",
            (datetime.now().timestamp() - max_age_days * 86400.0, max_rows)
        ).fetchall()
        conn.close()
        recs = [{
            "ts": r[0], "ts_iso": r[1], "agent": r[2], "model": r[3],
            "is_cloud": bool(r[4]), "success": bool(r[5]),
            "tokens_in": r[6], "tokens_out": r[7], "cost": r[8],
            "latency_ms": r[9], "error": r[10],
        } for r in rows]
        recs.reverse()  # 倒序回正序, 供 append 还原时间顺序
        with _lock:
            for rec in recs:
                _request_log.append(rec)
    except Exception:
        pass


# 模块导入即回填最近请求明细 (不会失败, 失败静默)
_load_recent_from_db()


# ─────────────────────────── 实时活跃请求计数 ───────────────────────────
def inc_active_requests():
    global _active_count
    with _lock:
        _active_count += 1


def dec_active_requests():
    global _active_count
    with _lock:
        _active_count = max(0, _active_count - 1)


# ─────────────────────────── 埋点入口 (供 llm_gateway 调用) ───────────────────────────
def record_request(agent_name: str, model: str, is_cloud: bool, success: bool,
                   tokens_in: int = 0, tokens_out: int = 0,
                   latency_ms: float = 0.0, error: Optional[str] = None):
    """记录一次 LLM 请求 (成功或降级后成功)。

    is_cloud=True 计云端单价; is_cloud=False (本地兜底) 按 Plan B 免费 (cost=0)。
    """
    tokens_in = int(tokens_in or 0)
    tokens_out = int(tokens_out or 0)
    if is_cloud:
        cost = (tokens_in / 1_000_000.0) * CLOUD_INPUT_PRICE + \
               (tokens_out / 1_000_000.0) * CLOUD_OUTPUT_PRICE
    else:
        cost = 0.0  # 本地免费
    cost = round(cost, 6)
    ts = datetime.now()
    rec = {
        "ts": ts.timestamp(),
        "ts_iso": ts.isoformat(timespec="seconds"),
        "agent": agent_name,
        "model": model,
        "is_cloud": bool(is_cloud),
        "success": bool(success),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost": cost,
        "latency_ms": float(latency_ms or 0.0),
        "error": error,
    }
    with _lock:
        _request_log.append(rec)
        if is_cloud and cost > 0:
            mk = _month_key(ts)
            _month_cloud_cost[mk] += cost
            _persist_month_cost(mk, cost)
    # 明细落 SQLite (跨进程重启可见); 失败静默, 不影响主链路
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO request_log (ts_epoch, ts_iso, agent, model, is_cloud, "
            "success, tokens_in, tokens_out, cost, latency_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec["ts"], rec["ts_iso"], agent_name, model, int(is_cloud), int(success),
             tokens_in, tokens_out, cost, rec["latency_ms"], error))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return rec


def record_degradation(agent: str, from_layer: str, to_layer: str,
                       reason: str, model: Optional[str] = None):
    """记录一次云端→本地的降级事件 (落内存 + SQLite)。"""
    ts = datetime.now()
    rec = {
        "ts": ts.isoformat(timespec="seconds"),
        "ts_epoch": ts.timestamp(),
        "agent": agent,
        "from_layer": from_layer,
        "to_layer": to_layer,
        "reason": reason,
        "model": model,
    }
    with _lock:
        _degradation_events.append(rec)
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO degradation_events (ts, ts_epoch, agent, from_layer, to_layer, reason, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (rec["ts"], rec["ts_epoch"], agent, from_layer, to_layer, reason, model,
             ts.isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return rec


# ─────────────────────────── 聚合统计 ───────────────────────────
def _group_stats(log: List[dict], since_ts: float, key: str) -> List[dict]:
    agg: Dict[str, dict] = {}
    for r in log:
        if r["ts"] < since_ts:
            continue
        k = r.get(key) or "-"
        d = agg.setdefault(k, {"name": k, "requests": 0, "success": 0,
                               "cost": 0.0, "tokens_in": 0, "tokens_out": 0})
        d["requests"] += 1
        if r["success"]:
            d["success"] += 1
        d["cost"] += r["cost"]
        d["tokens_in"] += r["tokens_in"]
        d["tokens_out"] += r["tokens_out"]
        if key == "model":
            d["is_cloud"] = bool(r["is_cloud"])
    out = []
    for k, d in agg.items():
        rate = (d["success"] / d["requests"] * 100.0) if d["requests"] else 0.0
        out.append({
            "name": d["name"],
            "requests": d["requests"],
            "success_rate": round(rate, 2),
            "cost": round(d["cost"], 4),
            "tokens_in": d["tokens_in"],
            "tokens_out": d["tokens_out"],
            "is_cloud": d.get("is_cloud"),
        })
    out.sort(key=lambda x: x["requests"], reverse=True)
    return out


def get_stats() -> Dict[str, Any]:
    """实时卡片 + 分组统计。"""
    now = _now_ts()
    win_60 = now - 60
    win_3600 = now - 3600
    with _lock:
        log = list(_request_log)
        active = _active_count
        # P2 可观测闭环: 近 1h 降级次数 (纯新增字段, 不影响既有键 -> 零回归)
        deg_1h = sum(1 for r in _degradation_events
                     if (r.get("ts_epoch") or 0) >= win_3600)
    cnt_60 = sum(1 for r in log if r["ts"] >= win_60)
    cnt_3600 = sum(1 for r in log if r["ts"] >= win_3600)
    succ_3600 = sum(1 for r in log if r["ts"] >= win_3600 and r["success"])
    succ_rate = (succ_3600 / cnt_3600 * 100.0) if cnt_3600 else None
    cost_3600 = sum(r["cost"] for r in log if r["ts"] >= win_3600)
    cnt_3600_cloud = sum(1 for r in log if r["ts"] >= win_3600 and r["is_cloud"])
    cnt_3600_local = sum(1 for r in log if r["ts"] >= win_3600 and not r["is_cloud"])
    tok_local_in = sum(r["tokens_in"] for r in log if r["ts"] >= win_3600 and not r["is_cloud"])
    tok_local_out = sum(r["tokens_out"] for r in log if r["ts"] >= win_3600 and not r["is_cloud"])
    return {
        "active_requests": active,
        "requests_60s": cnt_60,
        "requests_1h": cnt_3600,
        "requests_1h_cloud": cnt_3600_cloud,
        "requests_1h_local": cnt_3600_local,
        "tokens_local_in_1h": tok_local_in,
        "tokens_local_out_1h": tok_local_out,
        "success_rate_1h": round(succ_rate, 2) if succ_rate is not None else None,
        "cost_1h": round(cost_3600, 4),
        "degradation_1h": deg_1h,          # P2: 近 1h 降级次数 (多级降级 cloud→cloud_alt→local 均计入)
        "by_agent": _group_stats(log, win_3600, "agent"),
        "by_model": _group_stats(log, win_3600, "model"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_trends(hours: int = 1) -> Dict[str, Any]:
    """趋势图: 按时间桶聚合请求量与平均延迟。"""
    now = _now_ts()
    window = max(1, int(hours)) * 3600
    start = now - window
    bucket_count = 60
    bucket = window / bucket_count
    counts = [0] * bucket_count
    lat_sum = [0.0] * bucket_count
    with _lock:
        log = list(_request_log)
    for r in log:
        if r["ts"] < start:
            continue
        idx = min(bucket_count - 1, int((r["ts"] - start) // bucket))
        counts[idx] += 1
        lat_sum[idx] += r["latency_ms"]
    lat_avg = [(lat_sum[i] / counts[i]) if counts[i] else 0.0
               for i in range(bucket_count)]
    labels = [datetime.fromtimestamp(start + i * bucket).strftime("%H:%M")
              for i in range(bucket_count)]
    return {
        "hours": hours,
        "buckets": bucket_count,
        "timestamps": labels,
        "counts": counts,
        "latency_avg_ms": [round(x, 2) for x in lat_avg],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_degradation_events(limit: int = 100) -> List[dict]:
    """降级事件列表 (新→旧), 内存 + DB 合并去重。"""
    with _lock:
        mem = list(_degradation_events)
    mem.sort(key=lambda r: r["ts_epoch"], reverse=True)
    try:
        conn = _ensure_db()
        rows = conn.execute(
            "SELECT ts, agent, from_layer, to_layer, reason, model "
            "FROM degradation_events ORDER BY ts_epoch DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        db_list = [{"ts": r[0], "agent": r[1], "from_layer": r[2],
                    "to_layer": r[3], "reason": r[4], "model": r[5]}
                   for r in rows]
    except Exception:
        db_list = []
    seen = set()
    merged = []
    for r in mem + db_list:
        k = (r.get("ts"), r.get("agent"), r.get("reason"))
        if k in seen:
            continue
        seen.add(k)
        merged.append(r)
    merged.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return merged[:limit]


def get_degradation_summary(hours: int = 24) -> Dict[str, Any]:
    """P2 可观测闭环: 降级链聚合摘要 (按层对 + 按智能体)。

    背景: M4 落地多级降级后, 一条请求可能经历 cloud→cloud_alt→local 三段,
    但 ``get_degradation_events`` 只给逐条流水, 管理员看不出"降级主要发生在哪一层"。
    本函数把窗口内的事件按 ``from_layer→to_layer`` 聚合, 并显式给出三段关键计数:

      - ``cloud_to_cloud_alt`` : 主云失败, 切备选云 (P0 cloud_alt 层生效证据)
      - ``cloud_alt_to_local`` : 备选云也失败, 落到本地 (备选耗尽的代价)
      - ``cloud_to_local``     : 直接落本地 (cloud_alt 未启用/未配置的传统路径)

    数据源 = 内存环形缓冲 + SQLite (跨进程/重启后仍可统计); 两者由
    ``record_degradation`` 双写, 此处按 (ts_epoch, agent, from, to, reason) 去重。
    纯新增接口, 不改任何既有函数 -> 零回归。
    """
    window = max(1, int(hours or 24)) * 3600
    cutoff = _now_ts() - window
    with _lock:
        mem = [dict(r) for r in _degradation_events
               if (r.get("ts_epoch") or 0) >= cutoff]
    db_rows = []
    try:
        conn = _ensure_db()
        db_rows = conn.execute(
            "SELECT ts_epoch, agent, from_layer, to_layer, reason, model "
            "FROM degradation_events WHERE ts_epoch >= ?", (cutoff,)).fetchall()
        conn.close()
    except Exception:
        db_rows = []
    rows = list(mem)
    rows += [{"ts_epoch": r[0], "agent": r[1], "from_layer": r[2],
              "to_layer": r[3], "reason": r[4], "model": r[5]} for r in db_rows]

    seen = set()
    uniq = []
    for r in rows:
        k = (r.get("ts_epoch"), r.get("agent"), r.get("from_layer"),
             r.get("to_layer"), r.get("reason"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    chain: Dict[str, int] = {}
    by_agent: Dict[str, int] = {}
    for r in uniq:
        key = f"{r.get('from_layer') or '?'}→{r.get('to_layer') or '?'}"
        chain[key] = chain.get(key, 0) + 1
        a = r.get("agent") or "?"
        by_agent[a] = by_agent.get(a, 0) + 1

    return {
        "hours": max(1, int(hours or 24)),
        "total": len(uniq),
        "by_chain": [{"chain": k, "count": v}
                     for k, v in sorted(chain.items(), key=lambda x: -x[1])],
        "by_agent": [{"agent": k, "count": v}
                     for k, v in sorted(by_agent.items(), key=lambda x: -x[1])],
        # 三段关键计数 (缺失即 0, UI 可直接绑定)
        "cloud_to_cloud_alt": chain.get("cloud→cloud_alt", 0),
        "cloud_alt_to_local": chain.get("cloud_alt→local", 0),
        "cloud_to_local": chain.get("cloud→local", 0),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ─────────────────────────── 配额 (Plan B) ───────────────────────────
def get_quota() -> Dict[str, Any]:
    """配额预警: 配置预算 - 当月云费用累计。

    数据源 = .env MONTHLY_BUDGET (默认 1000); 当月云费用来自内存缓存 (启动已从库加载)。
    """
    try:
        total = float(os.getenv("MONTHLY_BUDGET", "1000") or "1000")
    except Exception:
        total = 1000.0
    month = _month_key()
    with _lock:
        used = _month_cloud_cost.get(month, 0.0)
    remaining = max(0.0, total - used)
    remaining_pct = (remaining / total * 100.0) if total > 0 else 0.0
    warn = remaining_pct < WARN_THRESHOLD
    return {
        "budget_source": "config",            # Plan B: 配置预算 + token 累计
        "month": month,
        "total": round(total, 2),
        "used": round(used, 4),
        "remaining": round(remaining, 4),
        "remaining_pct": round(remaining_pct, 2),
        "warn_threshold": WARN_THRESHOLD,
        "warn": bool(warn),
        "currency": "CNY",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
