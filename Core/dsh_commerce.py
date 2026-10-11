# -*- coding: utf-8 -*-
"""L2 全自动闭环：订单主表与发码/发货/对账逻辑。

承接 Core/dsh_registry.py 的口令能力，补上「收款 → 订单 → 自动发码 → 对账」这段。

核心不变量（写死在代码里，别改）：
  1. 幂等：支付回调可能重复。已 paid 且已发码的订单**绝不二次发码**
     （mark_paid 返回 newly_issued=False 并回带原码）。
  2. 金额单位统一为「分」（PASSCODE_PLANS 的 price 是元），避免浮点误差。
  3. order_id 同时作为 passcodes.order_ref，订单↔口令天然可 JOIN 对账。
  4. 发码与置 paid 在同一写事务内完成，避免「收了钱没码」或「有码没订单」。
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

try:  # Core 被 app.py 加入 sys.path 时的常规形态
    import dsh_registry as reg
except ImportError:  # 直接以脚本/离线自测运行时的兜底
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dsh_registry as reg

# ---------------------------------------------------------------- 常量
VALID_ORDER_STATUS = ("pending", "paid", "failed", "refunded")
VALID_CHANNEL = ("wechat", "alipay", "mock", "manual")
_ORDER_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 同口令，去易混字符


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def amount_fen(plan: str) -> int:
    """套餐价（元）→ 分。非法 plan 抛 RegistryError。"""
    if plan not in reg.PASSCODE_PLANS:
        raise reg.RegistryError(
            f"plan 非法: {plan}（合法: {sorted(reg.PASSCODE_PLANS)}）"
        )
    return int(reg.PASSCODE_PLANS[plan]["price"]) * 100


def verify_amount(plan: str, amount: int) -> bool:
    """回调金额校验：渠道报的实付金额必须等于套餐价（单位分）。"""
    try:
        return int(amount) == amount_fen(plan)
    except (TypeError, ValueError):
        return False


def _gen_order_id(conn: sqlite3.Connection) -> str:
    """ORD-YYYYMMDD-XXXX；主键冲突自动重试。"""
    for _ in range(20):
        body = "".join(secrets.choice(_ORDER_ID_ALPHABET) for _ in range(4))
        oid = f"ORD-{datetime.now().strftime('%Y%m%d')}-{body}"
        if not conn.execute(
            "SELECT 1 FROM dsh_orders WHERE order_id = ?", (oid,)
        ).fetchone():
            return oid
    raise reg.RegistryError("订单号生成连续冲突，请重试")


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def _public(order: Dict[str, Any]) -> Dict[str, Any]:
    """对外返回的脱敏视图：未付款不泄露口令。"""
    if not order:
        return {}
    out = dict(order)
    if out.get("status") != "paid":
        out["passcode"] = None
    out["amount_yuan"] = round(int(out.get("amount") or 0) / 100.0, 2)
    return out


# ---------------------------------------------------------------- 下单
def create_order(
    plugin_id: str,
    plan: str,
    channel: str = "mock",
    buyer_contact: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """创建 pending 订单。返回含 order_id / amount（分）/ plan / pay 所需字段。"""
    if plan not in reg.PASSCODE_PLANS:
        raise reg.RegistryError(
            f"plan 非法: {plan}（合法: {sorted(reg.PASSCODE_PLANS)}）"
        )
    if channel not in VALID_CHANNEL:
        raise reg.RegistryError(
            f"channel 非法: {channel}（合法: {list(VALID_CHANNEL)}）"
        )
    reg.get_plugin(plugin_id, db_path)  # 不存在 → NotFound

    conn = reg.connect(db_path)
    try:
        oid = _gen_order_id(conn)
        conn.execute(
            "INSERT INTO dsh_orders "
            "(order_id, plugin_id, plan, amount, currency, channel, status, "
            " buyer_contact, created_at) "
            "VALUES (?,?,?,?,'CNY',?,'pending',?,?)",
            (oid, plugin_id, plan, amount_fen(plan), channel,
             (buyer_contact or "")[:200], _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_orders WHERE order_id = ?", (oid,)
        ).fetchone()
        return _public(_row(row))
    finally:
        conn.close()


def get_order(
    order_id: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """订单查询（买家自助查码用）。未付款不返回口令。"""
    conn = reg.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM dsh_orders WHERE order_id = ?", ((order_id or "").strip(),)
        ).fetchone()
        if row is None:
            raise reg.NotFound(f"订单不存在: {order_id}")
        return _public(_row(row))
    finally:
        conn.close()


def list_orders(
    plugin_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """订单列表（最新在前）。"""
    sql = "SELECT * FROM dsh_orders"
    where, args = [], []
    if plugin_id:
        where.append("plugin_id = ?")
        args.append(plugin_id)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, order_id DESC LIMIT ?"
    args.append(int(limit))
    conn = reg.connect(db_path)
    try:
        return [_public(_row(r)) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- 支付回调 → 发码
def mark_paid(
    order_id: str,
    channel_trade_no: str = "",
    raw: str = "",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """置为已支付并**原子地**发一个口令。

    幂等：已 paid 的订单直接返回原码，newly_issued=False，不再生成新码
    （支付渠道回调重放是常态，重复发码等于白送一份授权）。
    """
    oid = (order_id or "").strip()
    conn = reg.connect(db_path)
    prev = conn.isolation_level
    try:
        conn.isolation_level = None  # 手动事务，确保「置 paid + 发码」原子
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM dsh_orders WHERE order_id = ?", (oid,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise reg.NotFound(f"订单不存在: {oid}")
        order = _row(row)

        if order["status"] == "paid":
            # 幂等命中：渠道重复回调，直接回带已发的码
            conn.execute("ROLLBACK")
            return {
                "order": _public(order),
                "passcode": order.get("passcode"),
                "newly_issued": False,
                "already_paid": True,
            }
        if order["status"] == "refunded":
            conn.execute("ROLLBACK")
            raise reg.RegistryError("订单已退款，不能再次置为已支付")

        # 发码（复用 registry 的生成器，保证与手工发码同一码制/同一字符集）
        code = reg._gen_passcode(conn)
        conn.execute(
            "INSERT INTO dsh_plugin_passcodes "
            "(code, plugin_id, plan, status, price, issued_at, note, order_ref) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (code, order["plugin_id"], order["plan"], "unused",
             reg.PASSCODE_PLANS[order["plan"]]["price"], _now(),
             "L2 自动发码", oid),
        )
        conn.execute(
            "UPDATE dsh_orders SET status='paid', paid_at=?, channel_trade_no=?, "
            "passcode=?, raw=? WHERE order_id=?",
            (_now(), (channel_trade_no or "")[:120], code, (raw or "")[:4000], oid),
        )
        conn.execute("COMMIT")
        fresh = _row(conn.execute(
            "SELECT * FROM dsh_orders WHERE order_id = ?", (oid,)
        ).fetchone())
        return {
            "order": _public(fresh),
            "passcode": code,
            "newly_issued": True,
            "already_paid": False,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.isolation_level = prev
        conn.close()


def mark_delivered(
    order_id: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """发货成功后回写 delivered_at（发货失败则不调用，便于扫「已付款未发货」）。"""
    conn = reg.connect(db_path)
    try:
        conn.execute(
            "UPDATE dsh_orders SET delivered_at=? WHERE order_id=?",
            (_now(), (order_id or "").strip()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM dsh_orders WHERE order_id = ?", ((order_id or "").strip(),)
        ).fetchone()
        if row is None:
            raise reg.NotFound(f"订单不存在: {order_id}")
        return _public(_row(row))
    finally:
        conn.close()


def resend(
    order_id: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """重发：**不发新码**，只回带已发口令供再次投递（邮件丢件时自助取回）。"""
    order = get_order(order_id, db_path)  # 未付款会脱敏掉 passcode
    if order.get("status") != "paid":
        raise reg.RegistryError("订单未支付，无法重发")
    if not order.get("passcode"):
        raise reg.RegistryError("订单无口令（发码异常），请人工介入")
    return {"order": order, "passcode": order["passcode"], "newly_issued": False}


def mark_refunded(
    order_id: str, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """退款：置 refunded 并吊销对应口令（避免退了钱还在用）。"""
    oid = (order_id or "").strip()
    order = get_order(oid, db_path)
    conn = reg.connect(db_path)
    try:
        conn.execute(
            "UPDATE dsh_orders SET status='refunded' WHERE order_id=?", (oid,)
        )
        conn.commit()
    finally:
        conn.close()
    if order.get("passcode"):
        try:
            reg.revoke_passcode(order["passcode"], db_path)
        except Exception:
            pass  # 吊销失败不阻断退款记账，但订单已是 refunded，核销时会被拒
    return _public(get_order(oid, db_path))


# ---------------------------------------------------------------- 对账 / 续费提醒
def reconcile(
    plugin_id: Optional[str] = None, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """订单↔口令对账：收入、已发码、已核销、异常项（收了钱没码/没发货）。"""
    conn = reg.connect(db_path)
    try:
        sql = "SELECT * FROM dsh_orders"
        args: List[Any] = []
        if plugin_id:
            sql += " WHERE plugin_id = ?"
            args.append(plugin_id)
        orders = [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()

    paid = [o for o in orders if o["status"] == "paid"]
    refunded = [o for o in orders if o["status"] == "refunded"]
    pending = [o for o in orders if o["status"] == "pending"]

    # 口径（勿混）：
    #   gross   = 历史真实收款（paid + refunded），退款不倒扣——钱确实进过账
    #   refund  = 已退款金额
    #   net     = gross - refund，等价于「当前仍有效的收入」= revenue
    # 早期实现用 paid 求和再减 refund，会把已退款订单扣两次（实测净收入 100 ≠ 199）。
    revenue_fen = sum(int(o.get("amount") or 0) for o in paid)
    gross_fen = revenue_fen + sum(int(o.get("amount") or 0) for o in refunded)
    refund_fen = sum(int(o.get("amount") or 0) for o in refunded)
    no_code = [o["order_id"] for o in paid if not o.get("passcode")]
    not_delivered = [
        o["order_id"] for o in paid if o.get("passcode") and not o.get("delivered_at")
    ]

    return {
        "plugin_id": plugin_id,
        "orders_total": len(orders),
        "paid": len(paid),
        "pending": len(pending),
        "refunded": len(refunded),
        "revenue_yuan": round(revenue_fen / 100.0, 2),
        "gross_yuan": round(gross_fen / 100.0, 2),
        "refund_yuan": round(refund_fen / 100.0, 2),
        "net_yuan": round((gross_fen - refund_fen) / 100.0, 2),
        # 异常项：这两类必须告警——都是「收了钱但买家拿不到/没拿到码」
        "alert_no_passcode": no_code,
        "alert_not_delivered": not_delivered,
    }


def expiring_orders(
    days: int = 7, plugin_id: Optional[str] = None, db_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """N 天内到期的已激活订单（用于续费提醒）。靠 order_ref 反查买家联系方式。"""
    horizon = (datetime.now() + timedelta(days=int(days))).isoformat(timespec="seconds")
    sql = (
        "SELECT o.order_id, o.plugin_id, o.plan, o.buyer_contact, o.amount, "
        "       p.code, p.expires_at, p.status AS pass_status "
        "FROM dsh_orders o JOIN dsh_plugin_passcodes p ON p.order_ref = o.order_id "
        "WHERE o.status='paid' AND p.status='active' AND p.expires_at IS NOT NULL "
        "      AND p.expires_at <= ?"
    )
    args: List[Any] = [horizon]
    if plugin_id:
        sql += " AND o.plugin_id = ?"
        args.append(plugin_id)
    sql += " ORDER BY p.expires_at ASC"
    conn = reg.connect(db_path)
    try:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
