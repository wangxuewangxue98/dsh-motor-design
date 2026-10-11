# -*- coding: utf-8 -*-
"""
DSH 插件注册表工具 —— 管理端（DSH 插件管理）后端数据层
=====================================================================
v3 定案①落地：插件注册表载体 = SQLite（取代 v2 的 JSON 文件方案）

设计要点：
  1. 两张表
     - dsh_plugin_registry : 插件台账（1 插件 1 行）
     - dsh_plugin_tokens   : 口令台账（1 口令 1 行，外键 plugin_id）
  2. distribution / release_gate 存 TEXT(JSON)，整块读写，不再用独立表；
     上层做 dict 级合并后整体写回（见 merge_json_field）
  3. **列级 UPDATE 局部更新**（取代 v2 的“三层合并”写法）
     update_plugin_fields() 只更新传入的白名单列，天然不会冲掉其他列
  4. WAL + busy_timeout=3000：读多写少（管理端人工触发）下的并发写兜底
  5. 安装状态**不入本库**：客户端心跳 PUT /api/admin/clients/<id> 附带
     plugin_versions 字段，由 build_install_matrix() 实时 join（v3 定案②）

只依赖标准库（sqlite3/json/os/pathlib/datetime），不 import 既有业务模块，
避免把 Manager/Flask 的 dotenv 侧效应带进来。
"""
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------- 路径
# Core/dsh_registry.py → parents[1] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "dsh_plugin_registry.db"

BUSY_TIMEOUT_MS = 3000

# ---------------------------------------------------------------- DDL
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS dsh_plugin_registry (
  plugin_id      TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  channel        TEXT NOT NULL CHECK(channel IN ('l0','l1','l2')),
  tier           TEXT NOT NULL CHECK(tier IN ('free','vip','flagship')),
  version        TEXT NOT NULL,
  locked_version TEXT,
  status         TEXT NOT NULL CHECK(status IN
                   ('draft','published','locked','rolled_back','offline')),
  distribution   TEXT NOT NULL DEFAULT '{}',
  release_gate   TEXT NOT NULL DEFAULT '{}',
  usage_stats    TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dsh_plugin_tokens (
  token_id    TEXT PRIMARY KEY,
  plugin_id   TEXT NOT NULL REFERENCES dsh_plugin_registry(plugin_id),
  type        TEXT NOT NULL CHECK(type IN ('one_time','limited')),
  tier        TEXT NOT NULL,
  issued_at   TEXT NOT NULL,
  expires_at  TEXT,
  redeemed_by TEXT,
  status      TEXT NOT NULL CHECK(status IN ('active','used','revoked'))
);

CREATE INDEX IF NOT EXISTS idx_tokens_plugin
  ON dsh_plugin_tokens(plugin_id, status);

-- 方案 C：npm 公开分发下载量趋势（逐日，采集脚本写入，管理端只读展示）
CREATE TABLE IF NOT EXISTS dsh_plugin_downloads (
  plugin_id    TEXT NOT NULL,
  package      TEXT NOT NULL,
  day          TEXT NOT NULL,
  downloads    INTEGER NOT NULL DEFAULT 0,
  collected_at TEXT NOT NULL,
  PRIMARY KEY (plugin_id, package, day)
);

CREATE INDEX IF NOT EXISTS idx_downloads_plugin_day
  ON dsh_plugin_downloads(plugin_id, day);

-- 设计经验脱敏回流（design_exp，插件 0.2.8 P1 通道）
-- 逐条样本：规格+几何+结论+误差带（已脱敏白名单裁剪），喂 L0 代理重训/真值库/可行性画像。
-- 幂等键 = (plugin_id, rec_id)：rec_id 是插件端对「规格+几何」的 sha1[:12]，
-- 同一设计重报不增长；跨插件同一设计各自保留（复合主键，避免 12hex 全局碰撞误杀）。
-- spec/geo/out 存 TEXT(JSON)，整块读写（与 distribution/release_gate 同风格）。
CREATE TABLE IF NOT EXISTS dsh_plugin_experience (
  rec_id         TEXT NOT NULL,
  plugin_id      TEXT NOT NULL,
  plugin_version TEXT NOT NULL DEFAULT '',
  motor_type     TEXT,
  spec_complete  INTEGER NOT NULL DEFAULT 0,
  spec           TEXT,
  geo            TEXT,
  out            TEXT,
  -- verdict 从 out.verdict 提升到列级（2026-10-10）：画像聚合走列，不必逐行解 JSON。
  -- 空值语义：out 段无 verdict（残缺样本/旧客户端）→ NULL，画像归 "unknown"。
  verdict        TEXT,
  ts             TEXT NOT NULL,
  reported_at    TEXT NOT NULL,
  PRIMARY KEY (plugin_id, rec_id)
);

CREATE INDEX IF NOT EXISTS idx_exp_plugin_ts
  ON dsh_plugin_experience(plugin_id, ts);
CREATE INDEX IF NOT EXISTS idx_exp_type_verdict
  ON dsh_plugin_experience(motor_type, verdict, plugin_id);

-- 第一阶段商业化：付费口令台账（一单一码，设备绑定，激活起算到期日）
CREATE TABLE IF NOT EXISTS dsh_plugin_passcodes (
  code           TEXT PRIMARY KEY,
  plugin_id      TEXT NOT NULL REFERENCES dsh_plugin_registry(plugin_id),
  plan           TEXT NOT NULL CHECK(plan IN ('trial','monthly','quarterly','halfyear','yearly')),
  status         TEXT NOT NULL CHECK(status IN ('unused','active','expired','revoked')),
  price          INTEGER NOT NULL DEFAULT 0,
  issued_at      TEXT NOT NULL,
  activated_at   TEXT,
  expires_at     TEXT,
  device_id      TEXT,
  order_ref      TEXT,
  note           TEXT,
  last_verify_at TEXT,
  verify_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_passcodes_plugin
  ON dsh_plugin_passcodes(plugin_id, status);

-- 每日免费额度（插件 × 设备 × 天 计数，服务端配额防重装重置）
CREATE TABLE IF NOT EXISTS dsh_free_usage (
  plugin_id  TEXT NOT NULL,
  device_id  TEXT NOT NULL,
  day        TEXT NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (plugin_id, device_id, day)
);

-- L2 全自动闭环：订单主表（付款 → 发码 → 发货 → 对账）
-- 设计要点：
--   * amount 单位「分」（PASSCODE_PLANS 的 price 是元），避免浮点误差；
--   * order_id 同时作为 passcodes.order_ref，订单↔口令天然可 JOIN 对账；
--   * 回调可能重复：mark_paid 幂等，已 paid 且已发码的订单绝不二次发码。
CREATE TABLE IF NOT EXISTS dsh_orders (
  order_id         TEXT PRIMARY KEY,
  plugin_id        TEXT NOT NULL,
  plan             TEXT NOT NULL CHECK(plan IN
                     ('trial','monthly','quarterly','halfyear','yearly')),
  amount           INTEGER NOT NULL DEFAULT 0,   -- 分
  currency         TEXT NOT NULL DEFAULT 'CNY',
  channel          TEXT NOT NULL,                -- wechat|alipay|mock|manual
  channel_trade_no TEXT,                         -- 渠道流水号，对账用
  status           TEXT NOT NULL CHECK(status IN
                     ('pending','paid','failed','refunded')),
  buyer_contact    TEXT,                         -- 邮箱/手机（发货目标）
  passcode         TEXT,                         -- 发码后回填
  paid_at          TEXT,
  delivered_at     TEXT,
  created_at       TEXT NOT NULL,
  raw              TEXT                          -- 渠道回调原文，排障用
);

CREATE INDEX IF NOT EXISTS idx_orders_status
  ON dsh_orders(status, paid_at);
CREATE INDEX IF NOT EXISTS idx_orders_trade
  ON dsh_orders(channel, channel_trade_no);

-- 续费提醒发送记录（幂等）：同一口令在同一提醒档位只发一次，
-- 避免定时任务每天重复轰炸买家（重启/重跑/多实例都不重发）。
CREATE TABLE IF NOT EXISTS dsh_reminder_log (
  code      TEXT NOT NULL,
  stage     INTEGER NOT NULL,          -- 7 / 1 / 0 / -1(已过期)
  sent_at   TEXT NOT NULL,
  ok        INTEGER NOT NULL DEFAULT 0,
  channel   TEXT,
  error     TEXT,
  PRIMARY KEY (code, stage)
);
"""

# ---------------------------------------------------------------- 约束常量
VALID_CHANNEL = ("l0", "l1", "l2")
VALID_TIER = ("free", "vip", "flagship")
VALID_STATUS = ("draft", "published", "locked", "rolled_back", "offline")
VALID_TOKEN_TYPE = ("one_time", "limited")
VALID_TOKEN_STATUS = ("active", "used", "revoked")

# 可更新的列白名单（plugin_id / created_at 不可变）
UPDATABLE_PLUGIN_FIELDS = (
    "name", "channel", "tier", "version", "locked_version", "status",
    "distribution", "release_gate", "usage_stats",
)

# 层级 → 付费等级
TIER_OF_CHANNEL = {"l0": "free", "l1": "vip", "l2": "flagship"}

# ---------------------------------------------------------------- 商业口令常量（第一阶段商业化）
# 价格阶梯（商业方案 2.1）：trial 为内测/种子用户免费档
PASSCODE_PLANS = {
    "trial":     {"days": 7,   "price": 0,   "label": "内测试用"},
    "monthly":   {"days": 30,  "price": 99,  "label": "月付"},
    "quarterly": {"days": 90,  "price": 199, "label": "季付"},
    "halfyear":  {"days": 180, "price": 399, "label": "半年付"},
    "yearly":    {"days": 365, "price": 699, "label": "年付"},
}

# 每日免费调用次数（方案 2.2：每日免费 5 次；可被环境变量 DSH_FREE_DAILY_LIMIT 覆盖）
FREE_DAILY_LIMIT_DEFAULT = 5

# 口令字符集：去掉易混淆的 0/O/1/I/L
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class RegistryError(Exception):
    """注册表业务规则异常（等价于上层 400/403）"""


class NotFound(RegistryError):
    pass


# ---------------------------------------------------------------- 连接
def resolve_db_path(db_path: Optional[str] = None) -> Path:
    """优先环境变量 DSH_REGISTRY_DB，其次显式入参，最后默认 <仓库根>/data/"""
    env = os.getenv("DSH_REGISTRY_DB")
    if env:
        return Path(env)
    return Path(db_path) if db_path else DEFAULT_DB_PATH


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """建立连接：WAL + busy_timeout + 外键 + Row 工厂 + 幂等列迁移"""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_migrations(conn)
    return conn


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    """幂等迁移：老库补表/补列（新库由 init_db 的 SCHEMA_SQL 直接建全）。

    注意：老库上 connect() 不会跑 SCHEMA_SQL，因此历史库缺的新表必须在这里补建，
    否则采集脚本/管理端读 dsh_plugin_downloads 会 no such table。
    """
    # ① 补建下载量表（方案 C 新增）
    has_dl = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_plugin_downloads'"
    ).fetchone()
    if not has_dl:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    # ①b 补建设计经验表（design_exp 回流，插件 0.2.8 P1 通道）
    has_exp = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_plugin_experience'"
    ).fetchone()
    if not has_exp:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    else:
        # ①c 补 verdict 列（2026-10-10 提升 out.verdict 到列级）
        # CREATE TABLE IF NOT EXISTS 对已存在的表不生效，老库必须显式 ALTER。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(dsh_plugin_experience)")}
        if "verdict" not in cols:
            conn.execute("ALTER TABLE dsh_plugin_experience ADD COLUMN verdict TEXT")
            conn.commit()
        # 索引含新列，需重建才生效（SQLite 不会自动更新既有索引定义）
        conn.execute("DROP INDEX IF EXISTS idx_exp_type_verdict")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exp_type_verdict "
            "ON dsh_plugin_experience(motor_type, verdict, plugin_id)"
        )
        conn.commit()

    # ①d 补建商业口令 + 每日免费额度表（第一阶段商业化，幂等）
    has_pc = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_plugin_passcodes'"
    ).fetchone()
    if not has_pc:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    # ①e 补建订单主表（L2 全自动闭环，幂等）
    has_ord = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_orders'"
    ).fetchone()
    if not has_ord:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    # ①f 补建续费提醒幂等表（到期提醒定时任务，幂等）
    has_rmd = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_reminder_log'"
    ).fetchone()
    if not has_rmd:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_plugin_registry'"
    ).fetchone()
    if not exists:
        return  # 新库：init_db 随后的 SCHEMA_SQL 会直接建出含 usage_stats 的表
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(dsh_plugin_registry)").fetchall()}
    if "usage_stats" not in cols:
        conn.execute(
            "ALTER TABLE dsh_plugin_registry "
            "ADD COLUMN usage_stats TEXT NOT NULL DEFAULT '{}'"
        )
        conn.commit()


def init_db(db_path: Optional[str] = None) -> Path:
    """建库/建表（幂等，可重复执行）"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    return resolve_db_path(db_path)


# ---------------------------------------------------------------- 工具
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    for col in ("distribution", "release_gate", "usage_stats"):
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except (ValueError, TypeError):
                d[col] = {}
    return d


def _dump(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


# ---------------------------------------------------------------- 插件台账
def register_plugin(payload: Dict[str, Any], db_path: Optional[str] = None) -> Dict[str, Any]:
    """注册插件（status=draft）。channel 与 tier 需按付费等级映射匹配"""
    plugin_id = payload.get("plugin_id")
    channel = payload.get("channel")
    tier = payload.get("tier")
    if not plugin_id:
        raise RegistryError("plugin_id 必填")
    if channel not in VALID_CHANNEL:
        raise RegistryError(f"channel 非法: {channel}")
    if tier not in VALID_TIER:
        raise RegistryError(f"tier 非法: {tier}")
    if TIER_OF_CHANNEL.get(channel) != tier:
        raise RegistryError(f"channel={channel} 与 tier={tier} 不匹配（应为 {TIER_OF_CHANNEL.get(channel)}）")

    conn = connect(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM dsh_plugin_registry WHERE plugin_id = ?", (plugin_id,)
        ).fetchone():
            raise RegistryError(f"plugin_id 已存在: {plugin_id}")
        conn.execute(
            """INSERT INTO dsh_plugin_registry
               (plugin_id,name,channel,tier,version,locked_version,status,
                distribution,release_gate,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                plugin_id,
                payload.get("name") or plugin_id,
                channel,
                tier,
                payload.get("version", "0.0.0"),
                None,
                "draft",
                _dump(payload.get("distribution") or {}),
                _dump(payload.get("release_gate") or {}),
                _now(),
                _now(),
            ),
        )
        conn.commit()
        # 保持 distribution/release_gate 返回为 dict（而非 TEXT）
        row = conn.execute(
            "SELECT * FROM dsh_plugin_registry WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_plugin(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM dsh_plugin_registry WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"插件不存在: {plugin_id}")
        return _row_to_dict(row)
    finally:
        conn.close()


def list_plugins(channel: Optional[str] = None, status: Optional[str] = None,
                 db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM dsh_plugin_registry WHERE 1=1"
    args: List[Any] = []
    if channel:
        sql += " AND channel = ?"
        args.append(channel)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY plugin_id"
    conn = connect(db_path)
    try:
        return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def update_plugin_fields(plugin_id: str, fields: Dict[str, Any],
                         db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    列级局部更新 —— 只 UPDATE 传入的白名单列。

    这是 v2「三层合并」范式的替代品：SQLite 按列更新天然不会改动其他列，
    因此单字段提交不可能冲掉 version / distribution / release_gate 等。
    """
    if not fields:
        raise RegistryError("fields 为空，拒绝空更新")

    unknown = set(fields) - set(UPDATABLE_PLUGIN_FIELDS)
    if unknown:
        raise RegistryError(f"不可更新字段: {sorted(unknown)}")

    if "channel" in fields and fields["channel"] not in VALID_CHANNEL:
        raise RegistryError(f"channel 非法: {fields['channel']}")
    if "tier" in fields and fields["tier"] not in VALID_TIER:
        raise RegistryError(f"tier 非法: {fields['tier']}")
    if "status" in fields and fields["status"] not in VALID_STATUS:
        raise RegistryError(f"status 非法: {fields['status']}")

    # status=published 需先过发布门禁（release_gate.triple_ok）
    if fields.get("status") == "published":
        cur = get_plugin(plugin_id, db_path)
        gate = dict(cur.get("release_gate") or {})
        gate.update(fields.get("release_gate") or {})
        if not gate.get("triple_ok"):
            raise RegistryError(
                f"发布门禁未通过（triple_ok=False），禁止置为 published: {plugin_id}"
            )

    sets = []
    args: List[Any] = []
    for col in UPDATABLE_PLUGIN_FIELDS:
        if col in fields:
            val = fields[col]
            if col in ("distribution", "release_gate", "usage_stats"):
                val = _dump(val)
            sets.append(f"{col} = ?")
            args.append(val)
    sets.append("updated_at = ?")
    args.append(_now())
    args.append(plugin_id)

    conn = connect(db_path)
    try:
        cur = conn.execute(
            f"UPDATE dsh_plugin_registry SET {', '.join(sets)} WHERE plugin_id = ?", args
        )
        if cur.rowcount == 0:
            raise NotFound(f"插件不存在: {plugin_id}")
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_plugin_registry WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def merge_json_field(plugin_id: str, column: str, patch: Dict[str, Any],
                     db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    JSON 列按 key 合并更新（读-改-写）：distribution / release_gate 专用。
    局部提交不会清空同列其他 key。
    """
    if column not in ("distribution", "release_gate"):
        raise RegistryError(f"非 JSON 列: {column}")
    cur = get_plugin(plugin_id, db_path)
    merged = dict(cur.get(column) or {})
    merged.update(patch or {})
    return update_plugin_fields(plugin_id, {column: merged}, db_path)


def set_release_gate(plugin_id: str, triple_ok: bool, summary: str = "",
                     db_path: Optional[str] = None) -> Dict[str, Any]:
    """回写 release.mjs 发布门禁结果"""
    return merge_json_field(
        plugin_id, "release_gate",
        {"triple_ok": bool(triple_ok), "ts": _now(), "summary": (summary or "")[:200]},
        db_path,
    )


# ---------------------------------------------------------------- 使用情况统计
# 插件端（lib/telemetry.mjs）上报的是「按工具聚合的脱敏指标」（调用次数/成败/
# 耗时分布/规模总和/来源版本），**不含**设计参数正文/结果明细/提示词/工作目录/密钥。
# 本库只存这份聚合 payload + 上报元数据（来源版本/上报时间/累计次数），供管理端
# 「DSH 插件管理 · 插件使用情况」卡片展示。多次上报走「按字段累加 + 覆盖时间戳」。
USAGE_STATS_KEYS = (
    "calls_total", "ok_total", "failed_total", "sum_n",
    "elapsed_min_ms", "elapsed_max_ms", "overall_success_rate",
)


def _empty_usage() -> Dict[str, Any]:
    return {
        "reported": False,
        "calls_total": 0, "ok_total": 0, "failed_total": 0, "sum_n": 0,
        "overall_success_rate": 0,
        "elapsed_min_ms": 0, "elapsed_max_ms": 0,
        "by_tool": {},
        "last_report": None,
        "last_ts": None,
        "plugin_version": None,
        "platform": None,
        "report_count": 0,
    }


def get_usage_stats(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """读取某插件的使用情况统计（无上报记录时返回带 reported=False 的空结构）。"""
    cur = get_plugin(plugin_id, db_path)
    stats = dict(cur.get("usage_stats") or {})
    base = _empty_usage()
    base.update({k: v for k, v in stats.items() if k in base or k == "by_tool"})
    base["reported"] = bool(stats.get("reported", False))
    return base


# ---------------------------------------------------------------- npm 下载量趋势（方案 C）
def record_downloads(plugin_id: str, package: str,
                     series: Iterable[Dict[str, Any]],
                     db_path: Optional[str] = None) -> int:
    """写入逐日下载序列（按 plugin_id+package+day UPSERT，幂等可重跑）。

    series: [{"day": "2026-09-21", "downloads": 270}, ...]
    返回落库行数。
    """
    conn = connect(db_path)
    try:
        ts = _now()
        rows = []
        for item in series or []:
            day = str(item.get("day") or "").strip()
            if not day:
                continue
            try:
                n = int(item.get("downloads") or 0)
            except (TypeError, ValueError):
                n = 0
            rows.append((plugin_id, package, day, n, ts))
        if not rows:
            return 0
        conn.executemany(
            "INSERT INTO dsh_plugin_downloads "
            "(plugin_id, package, day, downloads, collected_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(plugin_id, package, day) DO UPDATE SET "
            "downloads=excluded.downloads, collected_at=excluded.collected_at",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def get_download_stats(plugin_id: str, days: int = 30,
                       db_path: Optional[str] = None) -> Dict[str, Any]:
    """读取某插件的 npm 下载量统计（无采集记录时 reported=False，前端显示「暂无数据」）。"""
    try:
        days_n = max(1, min(int(days), 3650))
    except (TypeError, ValueError):
        days_n = 30

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT day, downloads FROM dsh_plugin_downloads "
            "WHERE plugin_id=? ORDER BY day ASC", (plugin_id,)
        ).fetchall()
        pkg_row = conn.execute(
            "SELECT package, MAX(collected_at) AS ts FROM dsh_plugin_downloads "
            "WHERE plugin_id=? GROUP BY package ORDER BY ts DESC LIMIT 1",
            (plugin_id,)).fetchone()
    finally:
        conn.close()

    if not rows:
        return {"reported": False, "package": "", "total": 0, "d7": 0, "d30": 0,
                "series": [], "peak": None, "last_day": None,
                "first_day": None, "last_updated": None}

    series_all = [{"day": r["day"], "downloads": int(r["downloads"] or 0)} for r in rows]
    nonzero = [s for s in series_all if s["downloads"] > 0]
    return {
        "reported": True,
        "package": pkg_row["package"] if pkg_row else "",
        "total": sum(s["downloads"] for s in series_all),
        "d7": sum(s["downloads"] for s in series_all[-7:]),
        "d30": sum(s["downloads"] for s in series_all[-30:]),
        "series": series_all[-days_n:],
        "peak": max(series_all, key=lambda s: s["downloads"]),
        # npm 当天数据不完整（常为 0），「最新有效日」取最后一个非零日
        "last_day": nonzero[-1] if nonzero else series_all[-1],
        "first_day": series_all[0]["day"],
        "last_updated": pkg_row["ts"] if pkg_row else None,
    }


# ---------------------------------------------------------------- 幂等键（P1 修复 2026-10-08）
# 同一批聚合 payload 重试重报不再翻倍：
#   - 插件端显式带 report_id 时直接用作键（为未来版本预留）；
#   - 否则对聚合指标做规范化哈希。刻意排除 reported_at —— 插件每次尝试都会重新
#     生成它，而重试时重读的是同一批 records（offset 未推进），计数/last_ts/by_tool
#     完全一致；排除后重试哈希稳定，正常新批次（last_ts 不同）不会误伤。
# 键环形保留最近 _USAGE_DEDUP_KEEP 条；命中即视为重复上报，直接返回现值。
_USAGE_DEDUP_KEEP = 64


def _usage_report_key(payload: Dict[str, Any]) -> str:
    p = payload or {}
    rid = p.get("report_id")
    if isinstance(rid, str) and rid.strip():
        return "rid:" + rid.strip()[:64]
    canon = json.dumps(
        {
            "plugin": p.get("plugin"),
            "kind": p.get("kind"),
            "calls_total": p.get("calls_total"),
            "ok_total": p.get("ok_total"),
            "failed_total": p.get("failed_total"),
            "sum_n": p.get("sum_n"),
            "by_tool": p.get("by_tool") or {},
            "last_ts": p.get("last_ts"),
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "sha1:" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:24]


def ingest_usage_stats(plugin_id: str, payload: Dict[str, Any],
                       source: str = "endpoint",
                       db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    合并一次聚合上报到 usage_stats。
      - 计数类字段（calls/ok/failed/sum_n）累加；
      - by_tool 按工具名累加 calls/failed/sum_n，耗时取 min/max；
      - 时间/版本/平台类字段取最新一次（last_report / last_ts / plugin_version / platform）；
      - 成功速率由累加后的 ok_total / calls_total 重算；
      - report_count +1，reported=True；
      - 幂等去重（P1）：report_id 或聚合指标哈希命中 recent_report_ids 时直接返回现值
        （附 deduped=True），不累加、不计 report_count。
    payload 结构 = lib/telemetry.mjs::aggregateUsage 的返回。
    """
    p = payload or {}
    # 走已校验的读接口，避免手写 SQL 拼接
    cur_row = get_plugin(plugin_id, db_path)
    acc = _empty_usage()
    acc.update({k: v for k, v in (cur_row.get("usage_stats") or {}).items()
                if k in acc or k == "by_tool"})
    acc["by_tool"] = dict((cur_row.get("usage_stats") or {}).get("by_tool") or {})

    # 幂等去重（P1）：命中最近上报键 => 同一批 payload 重报，直接返回现值不累加
    raw_stats = cur_row.get("usage_stats") or {}
    seen_keys = [k for k in (raw_stats.get("recent_report_ids") or [])
                 if isinstance(k, str)]
    rep_key = _usage_report_key(p)
    if rep_key in seen_keys:
        out = get_usage_stats(plugin_id, db_path)
        out["deduped"] = True
        out["dedup_key"] = rep_key
        return out

    for key in ("calls_total", "ok_total", "failed_total", "sum_n"):
        acc[key] = int(acc.get(key) or 0) + int(p.get(key) or 0)

    # 耗时取并集 min/max（历史极值 vs 本批极值）
    for key, op in (("elapsed_min_ms", min), ("elapsed_max_ms", max)):
        batch_v = p.get(key)
        if isinstance(batch_v, (int, float)):
            hist_v = acc.get(key) or 0
            acc[key] = op(batch_v, hist_v) if hist_v else batch_v

    # by_tool 按工具名累加
    for tool, b in (p.get("by_tool") or {}).items():
        tb = acc["by_tool"].setdefault(tool, {
            "calls": 0, "ok": 0, "failed": 0, "sum_n": 0,
            "avg_elapsed_ms": 0, "success_rate": 0,
        })
        tb["calls"] = int(tb.get("calls", 0)) + int(b.get("calls", 0))
        tb["ok"] = int(tb.get("ok", 0)) + int(b.get("ok", 0))
        tb["failed"] = int(tb.get("failed", 0)) + int(b.get("failed", 0))
        tb["sum_n"] = int(tb.get("sum_n", 0)) + int(b.get("sum_n", 0))
        if isinstance(b.get("avg_elapsed_ms"), (int, float)):
            tb["avg_elapsed_ms"] = round(b["avg_elapsed_ms"], 1)
        if isinstance(b.get("success_rate"), (int, float)):
            tb["success_rate"] = round(b["success_rate"], 1)

    # 时间/版本/平台类：取最新
    if p.get("last_ts"):
        acc["last_ts"] = p["last_ts"]
    if p.get("plugin_version"):
        acc["plugin_version"] = p["plugin_version"]
    if p.get("platform"):
        acc["platform"] = p["platform"]
    acc["last_report"] = p.get("reported_at") or _now()
    acc["report_count"] = int(acc.get("report_count", 0)) + 1
    acc["reported"] = True
    acc["source"] = source
    # 环形保留最近 N 个上报键（新键追加到尾部）
    acc["recent_report_ids"] = (seen_keys + [rep_key])[-_USAGE_DEDUP_KEEP:]
    acc["calls_total"] = acc["calls_total"] or 0
    total = acc["calls_total"] or 0
    acc["overall_success_rate"] = (
        round((acc["ok_total"] / total) * 1000) / 10 if total else 0.0
    )

    row = update_plugin_fields(plugin_id, {"usage_stats": acc}, db_path)
    return get_usage_stats(plugin_id, db_path)


# ---------------------------------------------------------------- 设计经验回流（design_exp，插件 0.2.8 P1）
# 与 usage_stats 的区别：usage 是「聚合统计量」（整块合并），design_exp 是「逐条脱敏样本」
# （规格+几何+结论+误差带），需要逐条入库做 rec_id 幂等去重。
#
# 服务端白名单 = 插件端 lib/experience-upload.mjs DESIGN_EXP_*_FIELDS 的**对称镜像**。
# 服务端**不信任**客户端传入字段，逐段只认白名单键（与 usage 的「列级白名单」同理），
# 红线键（client_id/cwd/路径/账号…）即便混入也在此被丢弃。
EXP_SPEC_WHITELIST = (
    "power_kw", "voltage_v", "speed_rpm", "poles",
    "line_freq_hz", "cooling", "insulation_class", "motor_type",
)
EXP_GEO_WHITELIST = (
    "stator_od", "stator_id", "core_length", "air_gap",
    "slots_stator", "slots_rotor",
)
EXP_OUT_WHITELIST = (
    "efficiency_raw", "temp_rise", "total_loss",
    "feasible", "verdict", "confidence", "confidence_band_pp",
)

# 体量/批量护栏（防御恶意放大）：单批最多入库条数、单条裁剪后 JSON 上限
EXP_MAX_BATCH = 500
_EXP_RECORD_BYTE_CAP = 8192


def _pick_whitelist(seg: Optional[Dict[str, Any]], whitelist: Iterable[str]) -> Dict[str, Any]:
    """只保留白名单键，丢弃一切非白名单/红线键（服务端脱敏裁剪真源）。"""
    if not isinstance(seg, dict):
        return {}
    out = {}
    for k in whitelist:
        if k in seg and seg[k] is not None:
            out[k] = seg[k]
    return out


def _sanitize_design_exp_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    裁剪一条 design_exp record 为入库形态。
    返回 None = 拒收（rec_id 缺失/非法，或 spec_complete 为真却无 spec 段——自相矛盾）。
    """
    rec_id = rec.get("rec_id")
    if not (isinstance(rec_id, str) and len(rec_id) == 12
            and all(c in "0123456789abcdef" for c in rec_id.lower())):
        return None

    spec_complete = bool(rec.get("spec_complete"))
    spec_clean = _pick_whitelist(rec.get("spec"), EXP_SPEC_WHITELIST)
    geo_clean = _pick_whitelist(rec.get("geo"), EXP_GEO_WHITELIST)
    out_clean = _pick_whitelist(rec.get("out"), EXP_OUT_WHITELIST)

    # spec_complete=True 但裁剪后 spec 为空 → 数据自相矛盾，拒收（防残缺规格污染真值库）
    if spec_complete and not spec_clean:
        return None

    # 残缺样本：不存 spec 段（对齐插件端 buildDesignExp 的降级行为），只存 geo+out
    spec_json = (json.dumps(spec_clean, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":"))
                 if spec_clean else None)
    geo_json = json.dumps(geo_clean, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    out_json = json.dumps(out_clean, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))

    body = {"spec": spec_json, "geo": geo_json, "out": out_json}
    # 体量护栏：裁剪后仍超限 → 拒收该条（正常样本 ~400B，远低于 8KB）
    if sum(len(v) for v in body.values() if v) > _EXP_RECORD_BYTE_CAP:
        return None

    # motor_type 从 spec 段取（契约位置），残缺时为空
    motor_type = spec_clean.get("motor_type")
    # verdict 从 out 段提升到列级（画像聚合按列走，不必逐行解 JSON；
    # 无 verdict → None，画像归 "unknown"）。仅取白名单内的 verdict 键，
    # 不信任客户端顶层传入的同名字段。
    verdict = out_clean.get("verdict")
    return {
        "rec_id": rec_id,
        "plugin_version": str(rec.get("plugin_version") or ""),
        "motor_type": motor_type,
        "spec_complete": 1 if spec_clean else 0,
        "spec": spec_json,
        "geo": geo_json,
        "out": out_json,
        "verdict": str(verdict) if verdict is not None else None,
    }


def ingest_experience(plugin_id: str, payload: Dict[str, Any],
                      source: str = "endpoint",
                      db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    逐条入库一批 design_exp 脱敏样本（插件 0.2.8 P1 通道）。
      - 服务端白名单裁剪（不信任客户端字段，红线键丢弃）；
      - rec_id 幂等：(plugin_id, rec_id) 已存在则跳过（计 deduped），不覆盖；
      - 批量护栏：超过 EXP_MAX_BATCH 条截断（truncated=True）；
      - 体量护栏：单条裁剪后超限拒收（rejected）。
    payload 结构 = lib/experience-upload.mjs::reportExperience 的 POST body：
      { plugin, plugin_version, kind, records:[design_exp...], sample_count, ... }
    返回 { reported, inserted, deduped, rejected, truncated, total, last_report }。
    """
    get_plugin(plugin_id, db_path)  # 校验插件存在（NotFound → 上层 404）

    raw_records = (payload or {}).get("records") or []
    if not isinstance(raw_records, list):
        raw_records = []
    truncated = len(raw_records) > EXP_MAX_BATCH
    batch = raw_records[:EXP_MAX_BATCH]

    conn = connect(db_path)
    inserted = deduped = rejected = 0
    ts_now = _now()
    try:
        for rec in batch:
            if not isinstance(rec, dict):
                rejected += 1
                continue
            clean = _sanitize_design_exp_record(rec)
            if clean is None:
                rejected += 1
                continue
            # INSERT OR IGNORE：命中 (plugin_id, rec_id) 主键 → 幂等去重
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO dsh_plugin_experience
                  (rec_id, plugin_id, plugin_version, motor_type,
                   spec_complete, spec, geo, out, verdict, ts, reported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean["rec_id"], plugin_id, clean["plugin_version"],
                    clean["motor_type"], clean["spec_complete"],
                    clean["spec"], clean["geo"], clean["out"],
                    clean.get("verdict"),
                    ts_now, ts_now,
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                deduped += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "reported": True,
        "inserted": inserted,
        "deduped": deduped,
        "rejected": rejected,
        "truncated": truncated,
        "total": len(batch),
        "source": source,
        "last_report": ts_now,
    }


def get_experience_stats(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    可行性画像：按 motor_type × verdict 聚合 + spec 覆盖率 + 版本分布。
    喂管理端「设计经验统计」标签与 L0 重训盲区识别（哪些规格段被反复查、哪段可行率塌）。
    """
    get_plugin(plugin_id, db_path)
    conn = connect(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM dsh_plugin_experience WHERE plugin_id=?",
            (plugin_id,)).fetchone()["n"]
        spec_ok = conn.execute(
            "SELECT COUNT(*) AS n FROM dsh_plugin_experience "
            "WHERE plugin_id=? AND spec_complete=1", (plugin_id,)).fetchone()["n"]

        # motor_type × verdict 矩阵：优先读列级 verdict（0.2.10 起入库时从
        # out.verdict 提升），列为空则回退解 out JSON（兼容补列前的历史行）。
        by_type_verdict: Dict[str, Dict[str, int]] = {}
        rows = conn.execute(
            "SELECT motor_type, verdict, out FROM dsh_plugin_experience WHERE plugin_id=?",
            (plugin_id,)).fetchall()
        for r in rows:
            verdict = r["verdict"]
            if not verdict:
                verdict = "unknown"
                try:
                    verdict = (json.loads(r["out"]) or {}).get("verdict", "unknown")
                except (TypeError, json.JSONDecodeError):
                    pass
            key = r["motor_type"] or "unknown"
            by_type_verdict.setdefault(key, {})
            by_type_verdict[key][str(verdict)] = by_type_verdict[key].get(str(verdict), 0) + 1

        by_version = {}
        for r in conn.execute(
                "SELECT plugin_version, COUNT(*) AS n FROM dsh_plugin_experience "
                "WHERE plugin_id=? GROUP BY plugin_version", (plugin_id,)).fetchall():
            by_version[r["plugin_version"] or ""] = r["n"]

        last_ts = conn.execute(
            "SELECT MAX(ts) AS m FROM dsh_plugin_experience WHERE plugin_id=?",
            (plugin_id,)).fetchone()["m"]
    finally:
        conn.close()

    return {
        "plugin_id": plugin_id,
        "reported": total > 0,
        "total_samples": total,
        "spec_complete_samples": spec_ok,
        "spec_coverage": round(spec_ok / total * 1000) / 10 if total else 0.0,
        "by_type_verdict": by_type_verdict,
        "by_version": by_version,
        "last_sample_ts": last_ts,
    }


def lock_version(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """锁版：locked_version = version，status = locked（此后停发新口令）"""
    cur = get_plugin(plugin_id, db_path)
    return update_plugin_fields(
        plugin_id,
        {"locked_version": cur.get("version"), "status": "locked"},
        db_path,
    )


def rollback_version(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """回滚到 locked_version"""
    cur = get_plugin(plugin_id, db_path)
    target = cur.get("locked_version")
    if not target:
        raise RegistryError(f"无锁版锚点，无法回滚: {plugin_id}")
    return update_plugin_fields(
        plugin_id, {"version": target, "status": "rolled_back"}, db_path
    )


# ---------------------------------------------------------------- 口令台账
def issue_token(plugin_id: str, token_id: str, token_type: str = "one_time",
                tier: Optional[str] = None, expires_at: Optional[str] = None,
                db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    发放口令。规则：
      - tier 与插件 channel 付费等级必须匹配（l0/free 不发 vip 口令）
      - status=locked / offline 停发
    """
    cur = get_plugin(plugin_id, db_path)
    if cur.get("status") in ("locked", "offline"):
        raise RegistryError(f"插件状态 {cur.get('status')}，已停止发放口令: {plugin_id}")

    tier = tier or cur.get("tier")
    if tier != cur.get("tier"):
        raise RegistryError(
            f"口令 tier={tier} 与插件 channel={cur.get('channel')} 不匹配（应为 {cur.get('tier')}）"
        )
    if token_type not in VALID_TOKEN_TYPE:
        raise RegistryError(f"口令类型非法: {token_type}")

    conn = connect(db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM dsh_plugin_tokens WHERE token_id = ?", (token_id,)
        ).fetchone():
            raise RegistryError(f"token_id 已存在: {token_id}")
        conn.execute(
            """INSERT INTO dsh_plugin_tokens
               (token_id,plugin_id,type,tier,issued_at,expires_at,redeemed_by,status)
               VALUES (?,?,?,?,?,?,?,?)""",
            (token_id, plugin_id, token_type, tier, _now(), expires_at, None, "active"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_plugin_tokens WHERE token_id = ?", (token_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def revoke_token(token_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """吊销口令：used 不可吊销"""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM dsh_plugin_tokens WHERE token_id = ?", (token_id,)
        ).fetchone()
        if not row:
            raise NotFound(f"口令不存在: {token_id}")
        if row["status"] == "used":
            raise RegistryError(f"已兑换口令不可吊销: {token_id}")
        conn.execute(
            "UPDATE dsh_plugin_tokens SET status = 'revoked' WHERE token_id = ?",
            (token_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_plugin_tokens WHERE token_id = ?", (token_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_tokens(plugin_id: str, status: Optional[str] = None,
                db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM dsh_plugin_tokens WHERE plugin_id = ?"
    args: List[Any] = [plugin_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- 安装矩阵
def build_install_matrix(clients: Iterable[Dict[str, Any]],
                         db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    实时 join：注册表 × 客户端心跳上报的 plugin_versions。

    :param clients: [{"client_id": "...", "plugin_versions": {"motor-ai-l0": {...}}}, ...]
                    plugin_versions 可缺省/为 None（旧客户端容错，计数记 0）
    :return: [{"plugin_id","name","version","locked_version","status",
               "clients":[{"client_id","installed_version","state","is_latest"}...],
               "installed_count","pending_update_count"}]
    """
    plugins = list_plugins(db_path=db_path)
    matrix = []
    for p in plugins:
        entries = []
        for c in clients or []:
            pv = (c or {}).get("plugin_versions") or {}
            rec = pv.get(p["plugin_id"])
            if not rec:
                continue
            entries.append({
                "client_id": c.get("client_id"),
                "installed_version": rec.get("installed_version"),
                "install_ts": rec.get("install_ts"),
                "state": rec.get("state", "unknown"),
                "is_latest": rec.get("installed_version") == p.get("version"),
            })
        matrix.append({
            "plugin_id": p["plugin_id"],
            "name": p["name"],
            "version": p["version"],
            "locked_version": p.get("locked_version"),
            "status": p["status"],
            "clients": entries,
            "installed_count": len(entries),
            "pending_update_count": sum(
                1 for e in entries if e["state"] == "pending_update" or not e["is_latest"]
            ),
        })
    return matrix


# ---------------------------------------------------------------- 商业口令（第一阶段商业化）
# 语义（按商业方案）：
#   - 一单一码：一次性口令，激活时绑定设备并起算到期日（trial 7天 / 月30 / 季90 / 半年180 / 年365）
#   - 状态机：unused →（verify 激活+绑设备）→ active →（expires_at 到期，惰性判定）→ expired
#             unused/active →（管理端吊销）→ revoked（终态）
#   - 设备绑定：1 口令 1 设备；管理端可解绑（unbind），下次 verify 重新绑定
#   - 每日免费额度：dsh_free_usage 按（插件×设备×天）计数，服务端配额，重装不重置
def _expire_due_passcodes(conn: sqlite3.Connection) -> None:
    """惰性到期：active 且 expires_at < now → expired（幂等）。"""
    conn.execute(
        "UPDATE dsh_plugin_passcodes SET status='expired' "
        "WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?",
        (_now(),),
    )


def _days_left(expires_at: Optional[str]) -> int:
    """剩余整天数（向下取整，到期时刻过后为 0）。解析失败返回 0。"""
    if not expires_at:
        return 0
    try:
        delta = datetime.fromisoformat(expires_at) - datetime.now()
        return max(0, int(delta.total_seconds() // 86400))
    except (ValueError, TypeError):
        return 0


def _gen_passcode(conn: sqlite3.Connection) -> str:
    """生成 DM-XXXX-XXXX 格式口令（去易混淆字符），主键冲突自动重试。"""
    for _ in range(20):
        body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        code = f"DM-{body[:4]}-{body[4:]}"
        if not conn.execute(
            "SELECT 1 FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone():
            return code
    raise RegistryError("口令生成连续冲突，请重试")


def free_daily_limit() -> int:
    """每日免费调用上限（环境变量 DSH_FREE_DAILY_LIMIT 可覆盖，默认 5）。"""
    try:
        return max(0, int(os.getenv("DSH_FREE_DAILY_LIMIT", str(FREE_DAILY_LIMIT_DEFAULT))))
    except ValueError:
        return FREE_DAILY_LIMIT_DEFAULT


def generate_passcodes(plugin_id: str, plan: str, count: int,
                       note: str = "", order_ref: str = "",
                       db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """批量生成一次性商业口令（status=unused）。count ∈ [1, 500]。"""
    if plan not in PASSCODE_PLANS:
        raise RegistryError(f"plan 非法: {plan}（合法: {sorted(PASSCODE_PLANS)}）")
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise RegistryError(f"count 非法: {count}")
    if not 1 <= count <= 500:
        raise RegistryError("count 超出范围（1~500）")
    get_plugin(plugin_id, db_path)  # 不存在 → NotFound

    conn = connect(db_path)
    try:
        rows = []
        for _ in range(count):
            code = _gen_passcode(conn)
            conn.execute(
                "INSERT INTO dsh_plugin_passcodes "
                "(code, plugin_id, plan, status, price, issued_at, note, order_ref) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (code, plugin_id, plan, "unused", PASSCODE_PLANS[plan]["price"],
                 _now(), (note or "")[:120], (order_ref or "")[:120]),
            )
            rows.append({"code": code, "plan": plan, "status": "unused",
                         "price": PASSCODE_PLANS[plan]["price"]})
        conn.commit()
        # 重新取全字段返回
        out = []
        for r in rows:
            row = conn.execute(
                "SELECT * FROM dsh_plugin_passcodes WHERE code = ?", (r["code"],)
            ).fetchone()
            out.append(dict(row))
        return out
    finally:
        conn.close()


def list_passcodes(plugin_id: str, status: Optional[str] = None,
                   db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """口令列表（最新发行在前）；读取时惰性结算到期。"""
    conn = connect(db_path)
    try:
        _expire_due_passcodes(conn)
        conn.commit()
        sql = "SELECT * FROM dsh_plugin_passcodes WHERE plugin_id = ?"
        args: List[Any] = [plugin_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY issued_at DESC, code"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def verify_passcode(code: str, device_id: str,
                    db_path: Optional[str] = None) -> Dict[str, Any]:
    """插件端授权验证（公开接口语义）。

    返回 {valid, reason?, plan, expires_at?, days_left?, activated?}：
      - unused → 激活（绑定设备 + 起算到期日）并返回 valid=True (activated=True)
      - active → 设备一致且未过期 → valid=True；设备不一致 → device_mismatch
      - expired / revoked / not_found / bad_request → valid=False + reason
    """
    code = (code or "").strip().upper()
    device_id = (device_id or "").strip()
    if not code or not device_id:
        return {"valid": False, "reason": "bad_request"}
    conn = connect(db_path)
    try:
        _expire_due_passcodes(conn)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return {"valid": False, "reason": "not_found"}
        plan = row["plan"]
        exp = row["expires_at"]
        if row["status"] == "revoked":
            return {"valid": False, "reason": "revoked", "plan": plan}
        if row["status"] == "expired":
            return {"valid": False, "reason": "expired", "plan": plan,
                    "expires_at": exp, "days_left": 0}
        if row["status"] == "unused":
            days = PASSCODE_PLANS[plan]["days"]
            exp = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE dsh_plugin_passcodes SET status='active', activated_at=?, "
                "expires_at=?, device_id=?, last_verify_at=?, verify_count=verify_count+1 "
                "WHERE code=?",
                (_now(), exp, device_id, _now(), code),
            )
            conn.commit()
            return {"valid": True, "plan": plan, "expires_at": exp,
                    "days_left": days, "activated": True}
        # active
        if row["device_id"] and device_id != row["device_id"]:
            return {"valid": False, "reason": "device_mismatch", "plan": plan,
                    "expires_at": exp, "days_left": _days_left(exp)}
        # 解绑后的重新绑定 + 常规验证心跳
        conn.execute(
            "UPDATE dsh_plugin_passcodes SET last_verify_at=?, "
            "verify_count=verify_count+1, device_id=COALESCE(device_id, ?) WHERE code=?",
            (_now(), device_id, code),
        )
        conn.commit()
        return {"valid": True, "plan": plan, "expires_at": exp,
                "days_left": _days_left(exp)}
    finally:
        conn.close()


def revoke_passcode(code: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """吊销口令（管理端）。unused/active/expired 均可吊销。"""
    code = (code or "").strip().upper()
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            raise NotFound(f"口令不存在: {code}")
        conn.execute(
            "UPDATE dsh_plugin_passcodes SET status='revoked' WHERE code = ?", (code,)
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone())
    finally:
        conn.close()


def unbind_passcode(code: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """解绑设备（管理端）。状态保持 active，下次 verify 以新设备重新绑定。"""
    code = (code or "").strip().upper()
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            raise NotFound(f"口令不存在: {code}")
        conn.execute(
            "UPDATE dsh_plugin_passcodes SET device_id=NULL WHERE code = ?", (code,)
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM dsh_plugin_passcodes WHERE code = ?", (code,)
        ).fetchone())
    finally:
        conn.close()


def passcode_stats(plugin_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """商业化统计：各状态计数 / 核销率 / 7 天内到期 / 已实现收入。"""
    conn = connect(db_path)
    try:
        _expire_due_passcodes(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT status, price, expires_at FROM dsh_plugin_passcodes WHERE plugin_id = ?",
            (plugin_id,),
        ).fetchall()
    finally:
        conn.close()

    by = {"unused": 0, "active": 0, "expired": 0, "revoked": 0}
    expiring_7d = 0
    revenue = 0
    horizon = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    for r in rows:
        st = r["status"]
        by[st] = by.get(st, 0) + 1
        if st in ("active", "expired"):
            revenue += int(r["price"] or 0)
        if st == "active" and r["expires_at"] and r["expires_at"] <= horizon:
            expiring_7d += 1
    total = len(rows)
    used = total - by["unused"]
    return {
        "plugin_id": plugin_id,
        "total": total,
        "unused": by["unused"],
        "active": by["active"],
        "expired": by["expired"],
        "revoked": by["revoked"],
        "expiring_in_7d": expiring_7d,
        # 核销率 = 已激活（含已过期）/ 全部发行
        "redeem_rate": round(used * 100.0 / total, 1) if total else 0.0,
        "revenue_realized": revenue,
        "free_daily_limit": free_daily_limit(),
    }


def free_call(plugin_id: str, device_id: str,
              db_path: Optional[str] = None) -> Dict[str, Any]:
    """每日免费额度检查 + 计数（调用一次计一次）。

    插件在免费模式执行工具前调用：allowed=True 放行（配额内自增），False 拒绝。
    返回 {allowed, used, remaining, limit, day}。
    """
    device_id = (device_id or "").strip()
    if not device_id:
        return {"allowed": False, "reason": "bad_request"}
    limit = free_daily_limit()
    day = datetime.now().strftime("%Y-%m-%d")
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT used FROM dsh_free_usage "
            "WHERE plugin_id=? AND device_id=? AND day=?",
            (plugin_id, device_id, day),
        ).fetchone()
        used = int(row["used"]) if row else 0
        if used >= limit:
            return {"allowed": False, "used": used, "remaining": 0,
                    "limit": limit, "day": day}
        conn.execute(
            "INSERT INTO dsh_free_usage (plugin_id, device_id, day, used, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(plugin_id, device_id, day) DO UPDATE SET "
            "used = used + 1, updated_at = excluded.updated_at",
            (plugin_id, device_id, day, used + 1, _now()),
        )
        conn.commit()
        used += 1
        return {"allowed": True, "used": used,
                "remaining": max(0, limit - used), "limit": limit, "day": day}
    finally:
        conn.close()
