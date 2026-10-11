# -*- coding: utf-8 -*-
"""L2 发货通道：SMTP（主）+ 企业微信机器人 / Telegram（备）。

唯一的硬要求：**发货失败必须被看见**。
买家付了钱却拿不到口令是最严重的故障，任何通道失败都要写日志 + 进告警列表，
绝不允许 except 吞掉后假装成功。deliver() 的返回里永远带 errors，
调用方（webhook 路由）据此决定要不要进「已付款未发货」告警队列。
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


PLAN_LABEL = {
    "trial": "内测试用(7天)",
    "monthly": "月付(30天)",
    "quarterly": "季付(90天)",
    "halfyear": "半年付(180天)",
    "yearly": "年付(365天)",
}

INSTALL_CMD = "dsh plugin --profile web add dsh-motor-ai"
CONTACT = "13546196530@163.com"


def build_body(order: Dict[str, Any], passcode: str) -> str:
    """发货正文：口令 + 用法 + 换机说明 + 联系渠道。"""
    plan = order.get("plan", "")
    yuan = round(int(order.get("amount") or 0) / 100.0, 2)
    return (
        "感谢购买 dsh-motor-ai。\n\n"
        f"订单号：{order.get('order_id')}\n"
        f"套餐：{PLAN_LABEL.get(plan, plan)}    金额：¥{yuan}\n"
        f"激活口令：{passcode}\n\n"
        "—— 使用方法 ——\n"
        f"1) 安装插件：{INSTALL_CMD}\n"
        "2) 在插件中执行 activate，粘贴上面的口令即可解锁（首次激活会绑定本机）。\n"
        "3) 到期后自动回落免费档；续费请重新购买，激活时会自动取更晚的到期日。\n\n"
        "—— 换机 / 重装 ——\n"
        "口令一码一机。换机前请先联系解绑，再到新机器激活。\n"
        f"技术支持：{CONTACT}\n\n"
        "（虚拟商品，一经核销不支持退款；换机解绑支持请见上方联系方式。）\n"
    )


def _send_smtp(to_addr: str, subject: str, body: str) -> Tuple[bool, str]:
    host, port = _env("DSH_NOTIFY_SMTP_HOST"), int(_env("DSH_NOTIFY_SMTP_PORT", "465"))
    user, pwd = _env("DSH_NOTIFY_SMTP_USER"), _env("DSH_NOTIFY_SMTP_PASS")
    sender = _env("DSH_NOTIFY_SMTP_FROM", user)
    if not (host and user and pwd and to_addr):
        return False, "SMTP 未配置或缺少收件人"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = to_addr
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
                s.login(user, pwd)
                s.sendmail(sender, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, pwd)
                s.sendmail(sender, [to_addr], msg.as_string())
        return True, ""
    except Exception as e:
        return False, f"SMTP 发送失败: {e}"


def _send_wecom(text: str) -> Tuple[bool, str]:
    hook = _env("DSH_NOTIFY_WECOM_WEBHOOK")
    if not hook or requests is None:
        return False, "企业微信机器人未配置"
    try:
        r = requests.post(
            hook,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=10,
        )
        ok = r.status_code == 200 and "ok" in (r.text or "").lower()
        return ok, "" if ok else f"企业微信返回异常: {r.text[:200]}"
    except Exception as e:
        return False, f"企业微信发送失败: {e}"


def _send_telegram(text: str) -> Tuple[bool, str]:
    token, chat = _env("DSH_NOTIFY_TELEGRAM_TOKEN"), _env("DSH_NOTIFY_TELEGRAM_CHAT_ID")
    if not (token and chat) or requests is None:
        return False, "Telegram 未配置"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text}, timeout=10,
        )
        ok = r.status_code == 200
        return ok, "" if ok else f"Telegram 返回异常: {r.text[:200]}"
    except Exception as e:
        return False, f"Telegram 发送失败: {e}"


def deliver(
    order: Dict[str, Any], passcode: str, contact: Optional[str] = None
) -> Dict[str, Any]:
    """发货：主通道 SMTP，失败则依次尝试备用通道。

    返回 {ok, sent:[通道], errors:[{channel, error}]}。
    ok=True 表示至少一个通道成功；errors 永远保留全部失败明细供告警。
    """
    to_addr = contact or order.get("buyer_contact") or ""
    body = build_body(order, passcode)
    subject = f"dsh-motor-ai 激活口令 {passcode}"
    sent: List[str] = []
    errors: List[Dict[str, str]] = []

    if "@" in to_addr:
        ok, err = _send_smtp(to_addr, subject, body)
        (sent.append("smtp") if ok else errors.append({"channel": "smtp", "error": err}))
    else:
        errors.append({"channel": "smtp", "error": f"无效邮箱地址: {to_addr!r}"})

    if not sent:  # 主通道挂了才走备用，避免重复打扰
        ok, err = _send_wecom(f"[dsh-motor-ai 发货]\n{body}")
        if ok:
            sent.append("wecom")
        else:
            errors.append({"channel": "wecom", "error": err})

    if not sent:
        ok, err = _send_telegram(f"[dsh-motor-ai 发货]\n{body}")
        if ok:
            sent.append("telegram")
        else:
            errors.append({"channel": "telegram", "error": err})

    result = {"ok": bool(sent), "sent": sent, "errors": errors,
              "order_id": order.get("order_id")}
    if not sent:
        # 最严重故障：付了钱发不出去。必须留痕，供 reconcile 的 alert_not_delivered 捕获。
        print(f"[dsh-notify][CRITICAL] 订单 {order.get('order_id')} 发货全部失败: "
              f"{json.dumps(errors, ensure_ascii=False)}")
    return result


def send_renewal_reminder(item: Dict[str, Any]) -> Dict[str, Any]:
    """到期续费提醒（定时任务调用）。"""
    to_addr = item.get("buyer_contact") or ""
    body = (
        "您的 dsh-motor-ai 授权即将到期。\n\n"
        f"订单号：{item.get('order_id')}\n"
        f"套餐：{PLAN_LABEL.get(item.get('plan', ''), item.get('plan', ''))}\n"
        f"到期时间：{item.get('expires_at')}\n"
        + (f"剩余天数：{item['days_left']}\n"
           if item.get("days_left") is not None else "")
        + "\n"
        "续费后重新激活即可，系统会自动取更晚的到期日（不会缩短）。\n"
        f"技术支持：{CONTACT}\n"
    )
    ok, err = _send_smtp(to_addr, "dsh-motor-ai 授权即将到期", body)
    return {"ok": ok, "order_id": item.get("order_id"), "error": err}
