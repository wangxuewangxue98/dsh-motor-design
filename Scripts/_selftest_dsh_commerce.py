# -*- coding: utf-8 -*-
"""dsh_commerce 离线自测（临时 DB，不碰生产库）。"""
import os
import sys
import tempfile

CORE = r"D:\MotorDesign\Core"
sys.path.insert(0, CORE)

import dsh_registry as reg  # noqa: E402
import dsh_commerce as com  # noqa: E402

DB = os.path.join(tempfile.gettempdir(), "_selftest_commerce.db")
if os.path.exists(DB):
    os.remove(DB)
for suffix in ("-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

ok = lambda c, m: print(("  PASS  " if c else "  FAIL  ") + m)

print("=== 0. 建库 + 登记插件 ===")
reg.init_db(DB)
reg.register_plugin({
    "plugin_id": "l2/flagship",
    "name": "dsh-motor-ai",
    "channel": "l2",
    "tier": "flagship",
    "version": "1.0.0-rc.7",
}, DB)
ok(True, "插件登记")

print("\n=== 1. 金额换算 ===")
ok(com.amount_fen("monthly") == 9900, f"月付 99 元 = {com.amount_fen('monthly')} 分")
ok(com.amount_fen("yearly") == 69900, f"年付 699 元 = {com.amount_fen('yearly')} 分")
ok(com.verify_amount("monthly", 9900), "金额校验 9900 通过")
ok(not com.verify_amount("monthly", 9901), "金额校验 9901 拒绝（防篡改）")

print("\n=== 2. 下单 ===")
o = com.create_order("l2/flagship", "monthly", channel="mock",
                     buyer_contact="buyer@example.com", db_path=DB)
oid = o["order_id"]
ok(o["status"] == "pending", f"status=pending ({o['status']})")
ok(o["amount"] == 9900, f"amount={o['amount']} 分")
ok(o["passcode"] is None, "未付款不泄露口令")
print(f"  order_id = {oid}")

print("\n=== 3. 支付回调 → 自动发码 ===")
r1 = com.mark_paid(oid, channel_trade_no="MOCK-TN-001", raw='{"mock":1}', db_path=DB)
code1 = r1["passcode"]
ok(r1["newly_issued"] is True, "首次回调：发新码")
ok(bool(code1) and code1.startswith("DM-"), f"码格式 = {code1}")
o = com.get_order(oid, DB)
ok(o["status"] == "paid", "订单置为 paid")
ok(o["passcode"] == code1, "订单回填口令")

print("\n=== 4. 幂等：回调重放 ===")
r2 = com.mark_paid(oid, channel_trade_no="MOCK-TN-001", raw='{"mock":1}', db_path=DB)
ok(r2["newly_issued"] is False, "重放：不发新码")
ok(r2["already_paid"] is True, "already_paid=True")
ok(r2["passcode"] == code1, f"回带原码一致（{r2['passcode']}）")
import sqlite3
c = sqlite3.connect(DB)
n = c.execute(
    "SELECT COUNT(*) FROM dsh_plugin_passcodes WHERE order_ref=?", (oid,)
).fetchone()[0]
c.close()
ok(n == 1, f"该订单只产生 1 个口令（实际 {n}）")

print("\n=== 5. 重发（不发新码） ===")
rr = com.resend(oid, DB)
ok(rr["passcode"] == code1, "重发回带同一码")
ok(rr["newly_issued"] is False, "newly_issued=False")

print("\n=== 6. 发货回写 + 对账 ===")
com.mark_delivered(oid, DB)
rc = com.reconcile("l2/flagship", DB)
ok(rc["paid"] == 1, f"paid={rc['paid']}")
ok(rc["revenue_yuan"] == 99.0, f"收入 {rc['revenue_yuan']} 元")
ok(rc["alert_no_passcode"] == [], "无「收钱没码」异常")
ok(rc["alert_not_delivered"] == [], "无「有码没发货」异常")
print(f"  对账: {rc}")

print("\n=== 7. 未发货告警 ===")
o2 = com.create_order("l2/flagship", "quarterly", channel="mock", db_path=DB)
com.mark_paid(o2["order_id"], db_path=DB)  # 故意不调 mark_delivered
rc2 = com.reconcile("l2/flagship", DB)
ok(o2["order_id"] in rc2["alert_not_delivered"], "未发货订单进入告警列表")

print("\n=== 8. 核销（口令真的能用） ===")
v = reg.verify_passcode(code1, "device-abc", DB)
ok(v.get("valid") is True, f"核销通过 plan={v.get('plan')} days_left={v.get('days_left')}")
v2 = reg.verify_passcode(code1, "device-xyz", DB)
ok(v2.get("reason") == "device_mismatch", f"异设备拒绝（{v2.get('reason')}）")

print("\n=== 9. 退款并吊销 ===")
com.mark_refunded(oid, DB)
v3 = reg.verify_passcode(code1, "device-abc", DB)
ok(v3.get("reason") == "revoked", f"退款后口令吊销（{v3.get('reason')}）")
rc3 = com.reconcile("l2/flagship", DB)
ok(rc3["refund_yuan"] == 99.0, f"退款 {rc3['refund_yuan']} 元")
ok(rc3["gross_yuan"] == 298.0, f"毛收入 {rc3['gross_yuan']} 元（99 已退但进过账）")
ok(rc3["net_yuan"] == 199.0, f"净收入 {rc3['net_yuan']} 元（季度档仍在，不得双重扣减）")
ok(rc3["net_yuan"] == rc3["revenue_yuan"], "净收入 == 当前有效收入（口径自洽）")

print("\n=== 10. 到期提醒查询 ===")
# 先让季度码被核销（status: unused → active），否则 expiring_orders 查不到
code2 = com.get_order(o2["order_id"], DB)["passcode"]
ok(bool(code2), f"季度订单有码 {code2}")
vq = reg.verify_passcode(code2, "device-q", DB)
ok(vq.get("valid") is True, f"季度码核销通过（{vq.get('plan')}）")
exp = com.expiring_orders(days=400, plugin_id="l2/flagship", db_path=DB)
ok(len(exp) == 1 and exp[0]["order_id"] == o2["order_id"],
   f"到期提醒命中 1 条（实际 {len(exp)}）")
# 已退款的码不得再进提醒列表
ok(all(e["code"] != code1 for e in exp), "已退款/吊销的码不进提醒列表")

print("\n完成。临时库:", DB)
