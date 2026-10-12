# -*- coding: utf-8 -*-
"""DSH 结果回流与健康度统计（M7 / M9, v4.02.75）。

把每次 DSH 生成的结局（成功/失败、失败分类、几何校验是否通过、是否重生成、
耗时/超时/输出字数）记录为 JSONL 并聚合统计，供：
  - GET /api/dsh/stats   : 各失败类别占比、几何校验通过率
  - GET /api/dsh/status  : 健康度摘要（kb_version / prompt_ver / checker_pass_rate / fail_class）

纯本地、无外部依赖；任何写盘异常都静默吞掉，绝不影响主生成流程。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List

_STORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_STORE_PATH = os.path.join(_STORE_DIR, "dsh_feedback.jsonl")
_LOCK = threading.Lock()

# 失败分类枚举（与 _classify_failure 一一对应）
FAIL_CLASSES = (
    "rate_limit",            # 外部 API 限流 (429)
    "timeout",               # 生成超时
    "endpoint_unimplemented",  # HTTP 端点未实现 (404/405/501)
    "no_output",             # CLI 无输出
    "cli_missing",           # 未探测到 dsh CLI 二进制
    "node_missing",          # node 可执行文件未找到
    "unavailable",           # DSH 总开关关闭 / 不可用
    "exception",             # 其它异常
    "unknown",               # 空错误
)


def classify_failure(error: str) -> str:
    """把错误文本归入失败类别（M7）。"""
    s = str(error or "")
    up = s.upper()
    if ("RATE_LIMIT" in up) or ("429" in s) or ("速率限制" in s):
        return "rate_limit"
    if ("超时" in s) or ("timeout" in s.lower()):
        return "timeout"
    if ("端点未实现" in s) or ("404" in s) or ("405" in s) or ("501" in s):
        return "endpoint_unimplemented"
    if "无输出" in s:
        return "no_output"
    if "未探测到 dsh CLI" in s:
        return "cli_missing"
    if "node 可执行文件未找到" in s:
        return "node_missing"
    if ("DSH 不可用" in s) or ("DSH_ENABLED" in s):
        return "unavailable"
    if not s.strip():
        return "unknown"
    return "exception"


def record_outcome(outcome: Dict[str, Any]) -> None:
    """记录一次生成结局（JSONL 追加）。永不抛异常。"""
    try:
        ok = bool(outcome.get("ok"))
        rec = {
            "ts": time.time(),
            "ok": ok,
            "mode": outcome.get("mode", ""),
            "prompt_ver": outcome.get("prompt_ver", ""),
            "fail_class": ("" if ok else (outcome.get("fail_class") or "unknown")),
            "checker_available": bool(outcome.get("checker_available")),
            "checker_ok": bool(outcome.get("checker_ok")),
            "regenerated": bool(outcome.get("regenerated")),
            "elapsed_s": outcome.get("elapsed_s", 0),
            "timeout_s": outcome.get("timeout_s", 0),
            "report_chars": outcome.get("report_chars", 0),
            "kb_version": outcome.get("kb_version", ""),
        }
        with _LOCK:
            os.makedirs(_STORE_DIR, exist_ok=True)
            with open(_STORE_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_all() -> List[dict]:
    recs: List[dict] = []
    try:
        if os.path.isfile(_STORE_PATH):
            with open(_STORE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return recs


def _pct(x: int, y: int) -> float:
    return round(100.0 * x / y, 1) if y else 0.0


def stats(tail: int = 500) -> Dict[str, Any]:
    """聚合最近 ``tail`` 条记录的失败分类占比 / 几何校验通过率（M7/M9）。"""
    recs = _read_all()[-max(1, int(tail)):]
    n = len(recs)
    fails: Dict[str, int] = {}
    ok_n = chk_n = chk_pass = 0
    for r in recs:
        if r.get("ok"):
            ok_n += 1
        else:
            fc = r.get("fail_class") or "unknown"
            fails[fc] = fails.get(fc, 0) + 1
        if r.get("checker_available"):
            chk_n += 1
            if r.get("checker_ok"):
                chk_pass += 1
    total_fail = n - ok_n
    last = recs[-1] if recs else {}
    return {
        "samples": n,
        "ok_count": ok_n,
        "fail_count": total_fail,
        "ok_rate_pct": _pct(ok_n, n),
        "fail_class_counts": fails,
        "fail_class_pct": {k: _pct(v, total_fail) for k, v in fails.items()},
        "checker_samples": chk_n,
        "checker_pass_rate_pct": _pct(chk_pass, chk_n),
        "last_mode": last.get("mode", ""),
        "last_prompt_ver": last.get("prompt_ver", ""),
        "store_path": _STORE_PATH,
    }
