# -*- coding: utf-8 -*-
"""到期提醒脚本离线自测（临时 DB，不碰生产库；不真发邮件）。"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")

import dsh_registry as reg          # noqa: E402
import dsh_commerce as com          # noqa: E402
import dsh_expiry_reminder as er     # noqa: E402

DB = os.path.join(tempfile.gettempdir(), "_selftest_reminder.db")
for s in ("", "-wal", "-shm"):
    if os.path.exists(DB + s):
        os.remove(DB + s)

ok = lambda c, m: print(("  PASS  " if c else "  FAIL  ") + m)

print("=== 0. 建库 + 造两个已激活授权 ===")
reg.init_db(DB)
reg.register_plugin({"plugin_id": "l2/flagship", "name": "dsh-motor-ai",
                     "channel": "l2", "tier": "flagship",
                     "version": "1.0.0-rc.7"}, DB)
# trial = 7 天 → 落在 7 天档；monthly = 30 天 → 不在任何档
o_trial = com.create_order("l2/flagship", "trial", channel="mock",
                           buyer_contact="trial@example.com", db_path=DB)
r_trial = com.mark_paid(o_trial["order_id"], db_path=DB)
v = reg.verify_passcode(r_trial["passcode"], "dev-trial", DB)
ok(v.get("valid") is True, f"trial 码激活 days_left={v.get('days_left')}")

o_mon = com.create_order("l2/flagship", "monthly", channel="mock",
                         buyer_contact="mon@example.com", db_path=DB)
r_mon = com.mark_paid(o_mon["order_id"], db_path=DB)
reg.verify_passcode(r_mon["passcode"], "dev-mon", DB)
ok(True, "monthly 码激活（30 天，应不进提醒）")

print("\n=== 1. 档位判定 ===")
ok(er.stage_of(6.5, [7, 1, 0, -1]) == 7, "剩 6.5 天 → stage 7")
ok(er.stage_of(0.5, [7, 1, 0, -1]) == 1, "剩 0.5 天 → stage 1")
ok(er.stage_of(0.0, [7, 1, 0, -1]) == 0, "剩 0 天 → stage 0")
ok(er.stage_of(-3.0, [7, 1, 0, -1]) == -1, "已过期 3 天 → stage -1")
ok(er.stage_of(30.0, [7, 1, 0, -1]) is None, "剩 30 天 → 不提醒")
ok(er.stage_of(6.5, [1, 0]) is None, "档位只有 1,0 时剩 6.5 天不命中")

print("\n=== 2. dry-run（不发送、不落库） ===")
res = er.run(DB, [7, 1, 0, -1], None, dry_run=True)
ok(res["scanned"] >= 1, f"扫描到 {res['scanned']} 条（trial 应命中）")
ok(len(res["sent"]) == 1 and res["sent"][0]["stage"] == 7,
   f"预览 1 条 stage=7（实际 {len(res['sent'])}）")
ok(all(r.get("to") == "trial@example.com" for r in res["sent"]), "收件人=trial 买家")
conn = sqlite3.connect(DB)
n = conn.execute("SELECT COUNT(*) FROM dsh_reminder_log").fetchone()[0]
conn.close()
ok(n == 0, f"dry-run 不落库（实际 {n} 条）")

print("\n=== 3. 正式运行：SMTP 未配置 → 必须失败且退出码 1 ===")
res2 = er.run(DB, [7, 1, 0, -1], None, dry_run=False)
ok(len(res2["failed"]) == 1, f"发送失败 1 条（实际 {len(res2['failed'])}）")
conn = sqlite3.connect(DB)
row = conn.execute("SELECT ok, error FROM dsh_reminder_log WHERE stage=7").fetchone()
conn.close()
ok(row is not None and row[0] == 0, f"失败记 ok=0（实际 {row}）")
ok(bool(row and row[1]), f"失败原因已记录（{(row[1] or '')[:60]}）")

print("\n=== 4. 失败允许重试（ok=0 不跳过） ===")
res3 = er.run(DB, [7, 1, 0, -1], None, dry_run=False)
ok(len(res3["failed"]) == 1, "失败后重跑仍会重试（不永久吞掉）")

print("\n=== 5. 成功一次后永久跳过 ===")
conn = sqlite3.connect(DB)
conn.execute("UPDATE dsh_reminder_log SET ok=1, error='' WHERE code=? AND stage=7",
             (r_trial["passcode"],))
conn.commit()
conn.close()
res4 = er.run(DB, [7, 1, 0, -1], None, dry_run=False)
ok(len(res4["sent"]) == 0 and len(res4["failed"]) == 0, "已成功 → 不再重发")
ok(any(s.get("why") == "本档位已发送" for s in res4["skipped"]),
   "命中跳过原因「本档位已发送」")

print("\n=== 6. 命令行入口 dry-run 退出码 ===")
rc = er.main(["--db", DB, "--dry-run"])
ok(rc == 0, f"main(--dry-run) 退出码 0（实际 {rc}）")

print("\n完成。临时库:", DB)
