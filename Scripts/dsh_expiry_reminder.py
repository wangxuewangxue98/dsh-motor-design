# -*- coding: utf-8 -*-
"""
dsh-motor-ai 授权到期续费提醒 —— 定时任务脚本
=================================================

用途
----
扫描「即将到期 / 已过期」的已激活授权，按档位给买家发续费提醒邮件，**同一档位只发一次**。

档位（默认）
------------
  stage  7  → 剩余 1 < days_left <= 7 天   「即将到期」
  stage  1  → 剩余 0 < days_left <= 1 天   「明天到期」
  stage  0  → 剩余 -1 < days_left <= 0 天  「今天到期」
  stage -1  → days_left <= -1             「已过期」（只发一次，不重复骚扰）

幂等
----
发送记录落表 ``dsh_reminder_log``（主键 code+stage）：
  * 已成功（ok=1）→ 永久跳过，重启/重跑/多实例都不会重发；
  * 上次失败（ok=0）→ 下次允许重试（邮件临时故障不应永久吞掉提醒）。

用法
----
    # 预览（不真发邮件、不落库）——上线前必跑
    python Scripts/dsh_expiry_reminder.py --dry-run

    # 正式运行
    python Scripts/dsh_expiry_reminder.py

    # 只跑某几个档位 / 指定库 / 指定插件
    python Scripts/dsh_expiry_reminder.py --stages 7,1 --db D:/MotorDesign/data/dsh_plugin_registry.db

退出码
------
    0 = 全部成功（或无需提醒）
    1 = 有发送失败（便于任务计划程序/监控告警捕获）
    2 = 参数或环境错误

Windows 计划任务注册（每天 09:00，管理员 PowerShell 执行一次）
-------------------------------------------------------------
    $py  = "D:\\MotorDesign\\venv\\Scripts\\python.exe"
    $sc  = "D:\\MotorDesign\\Scripts\\dsh_expiry_reminder.py"
    $act = New-ScheduledTaskAction -Execute $py -Argument "`"$sc`" --db D:\\MotorDesign\\data\\dsh_plugin_registry.db" -WorkingDirectory "D:\\MotorDesign"
    $trg = New-ScheduledTaskTrigger -Daily -At 09:00
    Register-ScheduledTask -TaskName "DSH-ExpiryReminder" -Action $act -Trigger $trg -Description "dsh-motor-ai 授权到期续费提醒"

Linux / macOS cron（每天 09:00）
--------------------------------
    0 9 * * * /path/to/venv/bin/python /path/to/Scripts/dsh_expiry_reminder.py >> /var/log/dsh-reminder.log 2>&1

前置：SMTP 需配置环境变量（见 Core/dsh_notify.py）：
    DSH_SMTP_HOST / DSH_SMTP_PORT / DSH_SMTP_USER / DSH_SMTP_PASS / DSH_MAIL_FROM
未配置时发送会失败并计入退出码 1 —— 这正是「提醒链路断了」该被看见的信号，不静默吞。
"""
from __future__ import annotations

import argparse
import os
import sys
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

# 让脚本可直接从仓库任意位置运行
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "Core"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dsh_registry as reg  # noqa: E402
import dsh_commerce as com  # noqa: E402
import dsh_notify as notify  # noqa: E402

DEFAULT_STAGES = (7, 1, 0, -1)


# ---------------------------------------------------------------- 档位判定
def stage_of(days_left: float, stages: List[int]) -> Optional[int]:
    """把剩余天数归入档位；不属于任何档位返回 None。

    档位语义：stage=N 表示「剩余不超过 N 天」这一档。
    同一时刻可能同时满足多个档（如剩 0.5 天既 <=7 也 <=1），
    必须取**满足条件的最小 stage**（最紧急那一档），否则 0.5 天会被
    误判成 7 天档、买家收到的文案与紧迫度不符（实测踩过）。
    """
    if days_left <= -1:
        return -1 if -1 in stages else None
    for s in sorted(s for s in stages if s >= 0):  # 升序 → 命中最小档
        if days_left <= s:
            return s
    return None


def days_left_of(expires_at: str) -> Optional[float]:
    """剩余天数（可为负）。解析失败返回 None（不提醒，避免误判）。"""
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(str(expires_at))
    except Exception:
        return None
    return (exp - datetime.now()).total_seconds() / 86400.0


# ---------------------------------------------------------------- 幂等记录
def already_sent(conn: sqlite3.Connection, code: str, stage: int) -> bool:
    row = conn.execute(
        "SELECT ok FROM dsh_reminder_log WHERE code=? AND stage=?", (code, stage)
    ).fetchone()
    return row is not None and int(row["ok"] or 0) == 1


def record(conn: sqlite3.Connection, code: str, stage: int, ok: bool,
           channel: str, error: str) -> None:
    conn.execute(
        "INSERT INTO dsh_reminder_log (code, stage, sent_at, ok, channel, error) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(code, stage) DO UPDATE SET sent_at=excluded.sent_at, "
        "ok=excluded.ok, channel=excluded.channel, error=excluded.error",
        (code, stage, datetime.now().isoformat(timespec="seconds"),
         1 if ok else 0, channel, (error or "")[:500]),
    )
    conn.commit()


# ---------------------------------------------------------------- 主流程
def run(db_path: Optional[str], stages: List[int], plugin_id: Optional[str],
        dry_run: bool) -> Dict[str, Any]:
    conn = reg.connect(db_path)
    try:
        horizon = max([s for s in stages if s >= 0] or [0])
        # 已过期档（stage=-1）需要包含过去的时间，horizon 只管未来；
        # expiring_orders 查询条件是 expires_at <= now+horizon，天然已含过期项。
        items = com.expiring_orders(days=horizon, plugin_id=plugin_id, db_path=db_path)

        sent: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        for it in items:
            code = it.get("code") or ""
            if not code:
                continue
            dl = days_left_of(it.get("expires_at"))
            if dl is None:
                skipped.append({"code": code, "why": "到期时间无法解析"})
                continue
            st = stage_of(dl, stages)
            if st is None:
                continue

            payload = dict(it)
            payload["days_left"] = round(dl, 1)

            if already_sent(conn, code, st):
                skipped.append({"code": code, "stage": st, "why": "本档位已发送"})
                continue

            if dry_run:
                sent.append({"code": code, "stage": st, "days_left": payload["days_left"],
                             "to": it.get("buyer_contact"), "dry_run": True})
                continue

            try:
                res = notify.send_renewal_reminder(payload)
            except Exception as e:  # 通知模块异常不得中断整批
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}

            ok = bool(res.get("ok"))
            err = str(res.get("error") or "")
            record(conn, code, st, ok, "smtp", err)
            row = {"code": code, "stage": st, "days_left": payload["days_left"],
                   "to": it.get("buyer_contact"), "ok": ok, "error": err}
            (sent if ok else failed).append(row)
            if not ok:
                print(f"[dsh-reminder][CRITICAL] 提醒发送失败 code={code} "
                      f"stage={st} err={err}", file=sys.stderr)

        return {
            "scanned": len(items),
            "stages": list(stages),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="dsh-motor-ai 授权到期续费提醒")
    ap.add_argument("--db", default=os.environ.get("DSH_REGISTRY_DB"),
                    help="注册表 SQLite 路径（默认取 DSH_REGISTRY_DB 或仓库 data/）")
    ap.add_argument("--stages", default=",".join(str(s) for s in DEFAULT_STAGES),
                    help="提醒档位，逗号分隔，默认 7,1,0,-1")
    ap.add_argument("--plugin-id", default=None, help="只处理指定插件（默认全部）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览不发送、不落库")
    args = ap.parse_args(argv)

    try:
        stages = [int(s) for s in str(args.stages).split(",") if s.strip() != ""]
    except ValueError:
        print("[dsh-reminder] --stages 必须是整数列表", file=sys.stderr)
        return 2
    if not stages:
        print("[dsh-reminder] --stages 为空", file=sys.stderr)
        return 2

    try:
        result = run(args.db, stages, args.plugin_id, args.dry_run)
    except Exception as e:
        print(f"[dsh-reminder] 运行失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    tag = "[DRY-RUN] " if result["dry_run"] else ""
    print(f"{tag}扫描 {result['scanned']} 条 | "
          f"发送 {len(result['sent'])} | 失败 {len(result['failed'])} | "
          f"跳过 {len(result['skipped'])}")
    for r in result["sent"]:
        print(f"  {'→' if r.get('dry_run') else '✓'} code={r['code']} "
              f"stage={r['stage']} 剩{r['days_left']}天 -> {r.get('to') or '(无邮箱)'}")
    for r in result["failed"]:
        print(f"  ✗ code={r['code']} stage={r['stage']} err={r.get('error')}",
              file=sys.stderr)

    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
