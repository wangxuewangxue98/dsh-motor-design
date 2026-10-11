# -*- coding: utf-8 -*-
"""支付渠道适配层：微信支付 v3（Native）+ 支付宝（当面付 precreate）。

设计取舍：
  * 只做「下单」与「回调验签」两件事——其余（发码/发货/对账）在 dsh_commerce。
  * 凭据全部走环境变量，绝不入库、不随包；缺失时 available() 返回 False 并给出明确原因，
    而不是在回调时才炸。
  * 提供 mock 通道：无需任何真实凭据即可把整条 L2 链路跑通（联调与自测用）。
  * cryptography 缺失时整个模块降级为不可用（除了 mock），不会静默放行未验签的回调。

微信 v3 签名串：
    method\nurl_path\ntimestamp\nnonce\nbody\n
支付宝待签名串：
    key=value 按 key 升序用 & 连接（剔除 sign / sign_type），再做 RSA2(SHA256)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

# ---------------------------------------------------------------- 依赖探测
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.x509 import load_pem_x509_certificate

    CRYPTO_OK = True
    CRYPTO_ERR = ""
except Exception as e:  # pragma: no cover
    CRYPTO_OK = False
    CRYPTO_ERR = str(e)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _load_pem(value: str) -> bytes:
    """PEM：既支持直接给内容，也支持给文件路径。"""
    v = (value or "").strip()
    if not v:
        raise ValueError("空的 PEM 配置")
    if v.startswith("-----BEGIN"):
        return v.encode("utf-8")
    with open(v, "rb") as f:
        return f.read()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- 可用性
def available(channel: str) -> Tuple[bool, str]:
    """渠道是否具备运行条件。返回 (ok, reason)。"""
    if channel == "mock":
        return True, "mock 通道（无需凭据，联调用）"
    if channel == "manual":
        return True, "manual 通道（人工确认收款后调用 mark_paid）"
    if not CRYPTO_OK:
        return False, f"缺少 cryptography 依赖：{CRYPTO_ERR}"
    if channel == "wechat":
        need = {
            "DSH_PAY_WECHAT_MCHID": _env("DSH_PAY_WECHAT_MCHID"),
            "DSH_PAY_WECHAT_SERIAL": _env("DSH_PAY_WECHAT_SERIAL"),
            "DSH_PAY_WECHAT_PRIVATE_KEY": _env("DSH_PAY_WECHAT_PRIVATE_KEY"),
            "DSH_PAY_WECHAT_API_V3_KEY": _env("DSH_PAY_WECHAT_API_V3_KEY"),
            "DSH_PAY_WECHAT_PLATFORM_CERT": _env("DSH_PAY_WECHAT_PLATFORM_CERT"),
        }
        miss = [k for k, v in need.items() if not v]
        return (False, f"微信支付缺少配置: {', '.join(miss)}") if miss else (True, "")
    if channel == "alipay":
        need = {
            "DSH_PAY_ALIPAY_APP_ID": _env("DSH_PAY_ALIPAY_APP_ID"),
            "DSH_PAY_ALIPAY_PRIVATE_KEY": _env("DSH_PAY_ALIPAY_PRIVATE_KEY"),
            "DSH_PAY_ALIPAY_PUBLIC_KEY": _env("DSH_PAY_ALIPAY_PUBLIC_KEY"),
        }
        miss = [k for k, v in need.items() if not v]
        return (False, f"支付宝缺少配置: {', '.join(miss)}") if miss else (True, "")
    return False, f"未知支付渠道: {channel}"


# ---------------------------------------------------------------- 微信支付 v3
def _wechat_sign(method: str, url_path: str, ts: str, nonce: str, body: str) -> str:
    """WECHATPAY2-SHA256-RSA2048 签名。"""
    key = serialization.load_pem_private_key(
        _load_pem(_env("DSH_PAY_WECHAT_PRIVATE_KEY")), password=None
    )
    msg = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body}\n".encode("utf-8")
    sig = key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
    return _b64(sig)


def _wechat_auth(method: str, url_path: str, body: str) -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    sign = _wechat_sign(method, url_path, ts, nonce, body)
    return (
        f'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{_env("DSH_PAY_WECHAT_MCHID")}",'
        f'nonce_str="{nonce}",'
        f'signature="{sign}",'
        f'timestamp="{ts}",'
        f'serial_no="{_env("DSH_PAY_WECHAT_SERIAL")}"'
    )


def wechat_native_prepay(order: Dict[str, Any], notify_url: str) -> Dict[str, Any]:
    """微信 Native 下单，返回 {code_url}（用于生成二维码）。

    order: create_order 的返回（含 order_id / amount 分 / plan）
    """
    import requests

    url_path = "/v3/pay/transactions/native"
    body = json.dumps(
        {
            "mchid": _env("DSH_PAY_WECHAT_MCHID"),
            "out_trade_no": order["order_id"],
            "appid": _env("DSH_PAY_WECHAT_APPID"),
            "description": f"dsh-motor-ai {order['plan']}",
            "notify_url": notify_url,
            "amount": {"total": int(order["amount"]), "currency": "CNY"},
        },
        ensure_ascii=False,
    )
    headers = {
        "Authorization": _wechat_auth("POST", url_path, body),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "dsh-motor-ai-commerce",
    }
    r = requests.post(
        f"https://api.mch.weixin.qq.com{url_path}",
        data=body.encode("utf-8"), headers=headers, timeout=15,
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"微信下单失败 HTTP {r.status_code}: {r.text[:300]}"}
    return {"ok": True, "code_url": r.json().get("code_url")}


def _wechat_verify(headers: Dict[str, str], body: str) -> bool:
    """回调验签：用平台证书公钥核对 Wechatpay-Signature。"""
    serial = headers.get("Wechatpay-Serial") or headers.get("wechatpay-serial")
    ts = headers.get("Wechatpay-Timestamp") or headers.get("wechatpay-timestamp")
    nonce = headers.get("Wechatpay-Nonce") or headers.get("wechatpay-nonce")
    sig = headers.get("Wechatpay-Signature") or headers.get("wechatpay-signature")
    if not (serial and ts and nonce and sig):
        return False
    cert = load_pem_x509_certificate(_load_pem(_env("DSH_PAY_WECHAT_PLATFORM_CERT")))
    pub = cert.public_key()
    msg = f"{ts}\n{nonce}\n{body}\n".encode("utf-8")
    try:
        pub.verify(base64.b64decode(sig), msg, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def wechat_parse_webhook(
    headers: Dict[str, str], body: str
) -> Dict[str, Any]:
    """微信回调：验签 → AES-256-GCM 解密 resource → 归一化结果。"""
    if not _wechat_verify(headers, body):
        return {"ok": False, "error": "微信回调验签失败"}
    try:
        payload = json.loads(body)
        res = payload["resource"]
        key = _env("DSH_PAY_WECHAT_API_V3_KEY").encode("utf-8")
        plain = AESGCM(key).decrypt(
            base64.b64decode(res["nonce"]),
            base64.b64decode(res["ciphertext"]),
            (res.get("associated_data") or "").encode("utf-8"),
        )
        data = json.loads(plain.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"微信回调解密失败: {e}"}

    state = data.get("trade_state")
    amt = (data.get("amount") or {}).get("total")
    return {
        "ok": True,
        "paid": state == "SUCCESS",
        "channel_trade_no": data.get("transaction_id") or "",
        "out_trade_no": data.get("out_trade_no") or "",
        "amount_fen": int(amt) if amt is not None else None,
        "raw": body[:4000],
        "trade_state": state,
    }


# ---------------------------------------------------------------- 支付宝
def _alipay_sign(params: Dict[str, Any]) -> str:
    """RSA2：按 key 升序拼 k=v&…，剔除 sign/sign_type。"""
    items = sorted(
        (k, v) for k, v in params.items()
        if k not in ("sign", "sign_type") and v not in (None, "")
    )
    msg = "&".join(f"{k}={v}" for k, v in items).encode("utf-8")
    key = serialization.load_pem_private_key(
        _load_pem(_env("DSH_PAY_ALIPAY_PRIVATE_KEY")), password=None
    )
    return _b64(key.sign(msg, padding.PKCS1v15(), hashes.SHA256()))


def alipay_precreate(order: Dict[str, Any], notify_url: str) -> Dict[str, Any]:
    """支付宝当面付（二维码）下单，返回 {qr_code}。"""
    import requests

    params = {
        "app_id": _env("DSH_PAY_ALIPAY_APP_ID"),
        "method": "alipay.trade.precreate",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": notify_url,
        "biz_content": json.dumps(
            {
                "out_trade_no": order["order_id"],
                "total_amount": f"{int(order['amount']) / 100.0:.2f}",
                "subject": f"dsh-motor-ai {order['plan']}",
            },
            ensure_ascii=False,
        ),
    }
    params["sign"] = _alipay_sign(params)
    r = requests.post(
        "https://openapi.alipay.com/gateway.do", data=params, timeout=15
    )
    try:
        resp = r.json().get("alipay_trade_precreate_response", {})
    except Exception:
        return {"ok": False, "error": f"支付宝响应解析失败: {r.text[:300]}"}
    if resp.get("code") != "10000":
        return {"ok": False, "error": f"支付宝下单失败: {resp.get('sub_msg') or resp}"}
    return {"ok": True, "qr_code": resp.get("qr_code")}


def alipay_verify(form: Dict[str, str]) -> bool:
    """支付宝回调验签（RSA2）。"""
    sign = form.get("sign") or ""
    if not sign:
        return False
    items = sorted(
        (k, v) for k, v in form.items()
        if k not in ("sign", "sign_type") and v not in (None, "")
    )
    msg = "&".join(f"{k}={v}" for k, v in items).encode("utf-8")
    pub = serialization.load_pem_public_key(
        _load_pem(_env("DSH_PAY_ALIPAY_PUBLIC_KEY"))
    )
    try:
        pub.verify(base64.b64decode(sign), msg, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def alipay_parse_webhook(form: Dict[str, str]) -> Dict[str, Any]:
    """支付宝回调：验签 → 归一化。trade_status 为 TRADE_SUCCESS/TRADE_FINISHED 视为已付。"""
    if not alipay_verify(form):
        return {"ok": False, "error": "支付宝回调验签失败"}
    status = form.get("trade_status") or ""
    amt = form.get("total_amount")
    return {
        "ok": True,
        "paid": status in ("TRADE_SUCCESS", "TRADE_FINISHED"),
        "channel_trade_no": form.get("trade_no") or "",
        "out_trade_no": form.get("out_trade_no") or "",
        "amount_fen": int(round(float(amt) * 100)) if amt else None,
        "raw": json.dumps(form, ensure_ascii=False)[:4000],
        "trade_state": status,
    }


# ---------------------------------------------------------------- 统一入口
def create_payment(
    channel: str, order: Dict[str, Any], notify_base: str = ""
) -> Dict[str, Any]:
    """按渠道下单。mock 通道直接返回可用的本地确认地址。"""
    okc, reason = available(channel)
    if not okc:
        return {"ok": False, "error": reason}
    base = (notify_base or _env("DSH_PAY_NOTIFY_BASE", "")).rstrip("/")
    if channel == "mock":
        return {
            "ok": True,
            "channel": "mock",
            "code_url": f"{base}/api/dsh/pay/mock-confirm/{order['order_id']}",
            "note": "mock 通道：访问该地址即视为支付成功（仅联调用）",
        }
    if not base:
        return {"ok": False, "error": "缺少 DSH_PAY_NOTIFY_BASE（回调公网基址）"}
    notify_url = f"{base}/api/dsh/pay/webhook/{channel}"
    if channel == "wechat":
        r = wechat_native_prepay(order, notify_url)
    elif channel == "alipay":
        r = alipay_precreate(order, notify_url)
    else:
        return {"ok": False, "error": f"渠道 {channel} 不支持下单"}
    r["channel"] = channel
    return r


def parse_webhook(
    channel: str, headers: Dict[str, str], body: str, form: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """按渠道解析并验签回调。任何验签失败都返回 ok=False，调用方必须据此拒绝。"""
    okc, reason = available(channel)
    if not okc:
        return {"ok": False, "error": reason}
    if channel == "wechat":
        return wechat_parse_webhook(headers, body)
    if channel == "alipay":
        return alipay_parse_webhook(form or {})
    if channel == "mock":
        try:
            d = json.loads(body or "{}")
        except Exception:
            d = {}
        return {
            "ok": True,
            "paid": True,
            "channel_trade_no": d.get("channel_trade_no") or "MOCK",
            "out_trade_no": d.get("order_id") or "",
            "amount_fen": d.get("amount"),
            "raw": body[:4000],
            "trade_state": "MOCK_SUCCESS",
        }
    return {"ok": False, "error": f"渠道 {channel} 不支持回调"}
