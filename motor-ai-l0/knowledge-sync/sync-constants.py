# -*- coding: utf-8 -*-
"""
P1 真源同步 —— Python 侧常量 → JS 侧常量的单向漂移检测
=====================================================================
背景（v2 定案①）
    constants 的**唯一真源**在 Python 侧。JS 侧 lib/motor-constants.mjs
    是构建期生成产物，禁止手写。本脚本负责把真源抽出来、算出指纹、
    并与 JS 侧当前值做逐项比对，产出 drift 报告。

为什么用 AST 而不是 import / 行号
    1. 主线（MotorDesign 后端）仍在调整中，import physics_kernel 会带
       入 dotenv / DB 等副作用，甚至因依赖缺失直接失败；
       AST 只做静态解析，零副作用、零依赖。
    2. 行号会随主线编辑漂移（这也是本仓库已踩过的坑：资料稿按行号
       引用真源，主线一动就失效）。本脚本按**符号名**定位，
       行号只作为"定位提示"记录进报告，不参与匹配。

模式
    python sync-constants.py                # 打印 drift 报告（只读）
    python sync-constants.py --emit         # 额外写出 constants.json
    python sync-constants.py --check        # 有 drift 则 exit 1（CI / release 门禁）

只依赖标准库。不 import 任何既有业务模块。
"""
from __future__ import annotations

import argparse
import ast
import os
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 控制台 GBK 解码会炸（本项目历史坑），强制 utf-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent                      # motor-ai-l0/
# 真源解析：L0 物理内核真源在独立程序仓 D:\MotorDesign（不是插件仓自身，也不是早期 worktree 副本）。
#   0.1.4 修正：原先用 `PLUGIN_ROOT.parents[0]` 相对推导 —— 当插件作为独立仓
#   （D:\dsh-motor-ai-l0）时该表达式解析成盘符根 D:\，其下无 Scripts/，导致
#   --check 把所有符号判成 source_missing（假阴性）。现改为显式指真源仓，
#   并用环境变量 MOTOR_AI_REPO_ROOT 支持其它机器的真源位置。
DEFAULT_REPO_ROOT = Path(r"D:\MotorDesign")
REPO_ROOT = Path(os.environ.get("MOTOR_AI_REPO_ROOT", DEFAULT_REPO_ROOT))

PHYSICS_KERNEL = REPO_ROOT / "Scripts" / "physics_kernel.py"
MOTOR_TOOLS = REPO_ROOT / "Scripts" / "motor_tools.py"
JS_CONSTANTS = PLUGIN_ROOT / "lib" / "motor-constants.mjs"
OUT_JSON = HERE / "constants.json"
OUT_MANIFEST = HERE / "constants-manifest.json"

MANIFEST_SCHEMA = "l0-constants-manifest/1"

# ---------------------------------------------------------------- 同步契约
# py_symbol: Python 侧符号名
# js_symbol: JS 侧导出名（None = 仅记录不比对）
# note     : 用途说明
SYNC_CONTRACT: List[Dict[str, Any]] = [
    {
        "py_symbol": "LAMBDA_RANGE",
        "js_symbol": "LAMBDA_RANGE",
        "py_file": "physics_kernel.py",
        "note": "长径比 λ 区间（按极数分档），V04 与矩阵生成共用",
    },
    {
        "py_symbol": "AIR_GAP_MIN",
        "js_symbol": "AIR_GAP_MIN",
        "py_file": "physics_kernel.py",
        "note": "气隙下限 mm，V02 硬规则",
    },
    {
        "py_symbol": "AIR_GAP_MAX",
        "js_symbol": "AIR_GAP_MAX",
        "py_file": "physics_kernel.py",
        "note": "气隙上限 mm（常规档），V02 硬规则",
    },
    {
        "py_symbol": "FRAME_SHIFT_THRESHOLDS",
        "js_symbol": None,
        "py_file": "physics_kernel.py",
        "note": "机座号迁移阈值 —— L0 当前不消费，仅登记观察",
    },
]

# L0 私有常量（Python 侧无对应真源，不参与比对）
L0_PRIVATE = [
    "SLOT_MAP", "COOLING_COEFFICIENT", "POLE_EFFICIENCY_FACTOR",
    "EFFICIENCY_BASELINE", "STEINMETZ", "CURRENT_DENSITY_REF",
    "FLUX_TARGET", "STACKING_FACTOR", "HIGH_SPEED_RPM",
    "AIR_GAP_MAX_HIGHSPEED", "SLOT_ASPECT_RANGE",
]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 主线文件偶发 BOM / 非 utf-8，退化为 replace
        return path.read_text(encoding="utf-8", errors="replace")


def extract_py_symbol(source: str, symbol: str) -> Dict[str, Any]:
    """用 AST 按符号名抽取顶层赋值的值（零 import、零副作用）"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {"found": False, "error": f"AST 解析失败: {e}"}

    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue

        for t in targets:
            if isinstance(t, ast.Name) and t.id == symbol:
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError, TypeError):
                    # 二次尝试：处理 float('inf') / float("-inf") 等非字面但属常量的写法
                    try:
                        seg = ast.get_source_segment(source, node.value) or ""
                        # get_source_segment 会连带行内 # 注释返回，literal_eval 无法解析，先剥离
                        seg = re.sub(r"#[^\n]*", "", seg)
                        # ast.literal_eval 不认裸 inf，改用引号哨兵，求值后还原成 float('inf')
                        seg = (seg.replace("float('inf')", "'__POS_INF__'")
                                  .replace('float("inf")', "'__POS_INF__'")
                                  .replace("float('-inf')", "'__NEG_INF__'")
                                  .replace('float("-inf")', "'__NEG_INF__'"))
                        value = _replace_infinity(ast.literal_eval(seg))
                    except (ValueError, SyntaxError, TypeError) as e:
                        return {
                            "found": True,
                            "lineno": getattr(node, "lineno", None),
                            "value": None,
                            "error": f"无法字面求值（非常量表达式）: {e}",
                        }
                return {
                    "found": True,
                    "lineno": getattr(node, "lineno", None),
                    "value": value,
                    "error": None,
                }
    return {"found": False, "error": f"未找到顶层符号 {symbol}"}


def _quote_js_keys(raw: str) -> str:
    """给未加引号的对象键（标识符或数字）加双引号；已加引号的键原样保留。"""
    import re

    def repl(m: "re.Match") -> str:  # noqa: F821
        pre = m.group(1)            # ([{,]\s*)
        if m.group(2) is not None:  # 已是 "key" 或 'key' —— 原样保留
            return m.group(0)
        key = m.group(4) if m.group(4) is not None else m.group(5)
        return f'{pre}"{key}":'

    pat = re.compile(
        r'([{,]\s*)'                                   # 前导 { 或 , 加空白
        r'(?:(["\'])([^"\']*)\2'                       # 已加引号键
        r'|([A-Za-z_$][\w$]*)'                         # 标识符键
        r'|(\d+))'                                     # 数字键
        r'\s*:'
    )
    return pat.sub(repl, raw)


def _replace_infinity(value: Any) -> Any:
    """JS 解析后把无穷大哨兵字符串还原成 float('inf')/-inf，与 Python 侧对齐。"""
    if isinstance(value, dict):
        return {k: _replace_infinity(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_infinity(v) for v in value]
    if value == "__POS_INF__":
        return float("inf")
    if value == "__NEG_INF__":
        return float("-inf")
    return value


def extract_js_symbol(source: str, symbol: str) -> Dict[str, Any]:
    """
    从 JS 源码抽取 `export const X = {...}` / `= 数值` 的值。
    只支持 JSON 可序列化的字面量（对象/数组/数字/字符串）。
    健壮性：先剥离注释再做括号配对扫描，支持数字键、Infinity 哨兵。
    """
    import re
    pattern = re.compile(
        r"export\s+const\s+" + re.escape(symbol) + r"\s*=\s*",
        re.MULTILINE,
    )
    m = pattern.search(source)
    if not m:
        return {"found": False, "error": f"未找到 export const {symbol}"}

    # 取 = 之后整段，先剥离注释让括号扫描不再被注释干扰
    slice_src = source[m.end():]
    slice_src = re.sub(r"/\*.*?\*/", "", slice_src, flags=re.S)
    slice_src = re.sub(r"//[^\n]*", "", slice_src)

    # 括号配对扫描，取到赋值表达式结束（顶层换行即终止）
    depth = 0
    i = 0
    in_str = None
    n = len(slice_src)
    while i < n:
        ch = slice_src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'`":
            in_str = ch
            i += 1
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            if depth == 0:
                break
            depth -= 1
        elif ch == "\n" and depth == 0:
            break
        i += 1

    raw = slice_src[:i].strip().rstrip(";").strip().rstrip(",")

    # Infinity 哨兵（词边界，避免误伤标识符）
    raw = re.sub(r"-\s*Infinity\b", '"__NEG_INF__"', raw)
    raw = re.sub(r"\bInfinity\b", '"__POS_INF__"', raw)

    # JS → JSON：对象 key 加引号、单引号转双引号、去尾随逗号
    try:
        normalized = _quote_js_keys(raw)
        normalized = normalized.replace("'", '"')
        normalized = re.sub(r",(\s*[}\]])", r"\1", normalized)
        value = json.loads(normalized)
        value = _replace_infinity(value)
        return {"found": True, "value": value, "error": None}
    except (ValueError, TypeError) as e:
        return {"found": True, "value": None, "raw": raw[:200],
                "error": f"JS 字面量解析失败: {e}"}


def fingerprint(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _coerce_numeric_key(k: Any) -> Any:
    """JS 解析后数字键会变成字符串（"2"），还原成 int 以便与 Python 整数键对标"""
    if isinstance(k, str):
        try:
            return int(k)
        except ValueError:
            try:
                return float(k)
            except ValueError:
                return k
    return k


def _sanitize_for_json(obj: Any) -> Any:
    """Python json.dumps 会把 inf/nan 写成非标准 JSON 令牌（Infinity/NaN），
    JS 侧 json.loads 无法解析。输出前把非有限浮点替换为 null（标准 JSON）。"""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # nan / ±inf
            return None
    return obj


def js_number_keys_equal(a: Any, b: Any, tol: float = 1e-9) -> bool:
    """数值/嵌套结构比对，浮点给容差；元组≙列表、int键≙str键均可比"""
    if isinstance(a, dict) and isinstance(b, dict):
        ak = {_coerce_numeric_key(k): v for k, v in a.items()}
        bk = {_coerce_numeric_key(k): v for k, v in b.items()}
        if set(ak.keys()) != set(bk.keys()):
            return False
        return all(js_number_keys_equal(ak[k], bk[k], tol) for k in ak)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(js_number_keys_equal(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol
    return a == b


def main() -> int:
    ap = argparse.ArgumentParser(description="P1 常量真源同步与漂移检测")
    ap.add_argument("--emit", action="store_true", help="写出 constants.json")
    ap.add_argument("--check", action="store_true", help="有 drift 则 exit 1")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    py_sources: Dict[str, Optional[str]] = {}
    for key, path in (("physics_kernel.py", PHYSICS_KERNEL),
                      ("motor_tools.py", MOTOR_TOOLS)):
        py_sources[key] = read_text(path) if path.exists() else None

    js_src = read_text(JS_CONSTANTS) if JS_CONSTANTS.exists() else None

    items: List[Dict[str, Any]] = []
    drifts: List[Dict[str, Any]] = []
    missing: List[str] = []

    for c in SYNC_CONTRACT:
        src = py_sources.get(c["py_file"])
        if src is None:
            missing.append(f"{c['py_file']} 不存在")
            items.append({**c, "status": "source_missing"})
            continue

        got = extract_py_symbol(src, c["py_symbol"])
        entry: Dict[str, Any] = {
            "py_symbol": c["py_symbol"],
            "js_symbol": c["js_symbol"],
            "py_file": c["py_file"],
            "py_lineno": got.get("lineno"),
            "py_found": got.get("found", False),
            "py_error": got.get("error"),
            "py_value": got.get("value"),
            "note": c["note"],
        }

        if not got.get("found"):
            missing.append(f"{c['py_symbol']} 未找到")
            entry["status"] = "py_symbol_missing"
            items.append(entry)
            continue

        if got.get("error"):
            entry["status"] = "unevaluable"
            items.append(entry)
            continue

        if c["js_symbol"] and js_src:
            j = extract_js_symbol(js_src, c["js_symbol"])
            entry["js_found"] = j.get("found")
            entry["js_error"] = j.get("error")
            entry["js_value"] = j.get("value")
            if not j.get("found"):
                entry["status"] = "js_symbol_missing"
                drifts.append({**entry, "reason": "JS 侧缺该导出"})
            elif j.get("error"):
                entry["status"] = "js_unparseable"
                drifts.append({**entry, "reason": "JS 侧无法解析"})
            elif js_number_keys_equal(got["value"], j.get("value")):
                entry["status"] = "in_sync"
            else:
                entry["status"] = "drift"
                drifts.append({
                    "py_symbol": c["py_symbol"],
                    "js_symbol": c["js_symbol"],
                    "py_value": got["value"],
                    "js_value": j.get("value"),
                    "py_lineno": got.get("lineno"),
                    "reason": "数值不一致 —— 需重新生成 motor-constants.mjs",
                })
        else:
            entry["status"] = "observed" if not c["js_symbol"] else "js_missing"

        items.append(entry)

    payload_blob = {
        "schema": MANIFEST_SCHEMA,
        "items": items,
        "l0_private": L0_PRIVATE,
    }
    fp = fingerprint(payload_blob)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "fingerprint": fp,
        "generated_at": None,
        "source_files": {
            "physics_kernel.py": str(PHYSICS_KERNEL),
            "motor_tools.py": str(MOTOR_TOOLS),
        },
        "items": items,
        "l0_private": L0_PRIVATE,
        "drift_count": len(drifts),
        "missing": missing,
    }

    if args.emit:
        OUT_JSON.write_text(
            json.dumps(_sanitize_for_json(manifest), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(_sanitize_for_json(manifest), ensure_ascii=False, indent=2))
    else:
        print("")
        print("══════ P1 常量真源同步 · 漂移报告 ══════")
        for it in items:
            mark = {
                "in_sync": "✅", "drift": "⚠️ ", "observed": "👁 ",
                "py_symbol_missing": "❌", "js_symbol_missing": "❌",
                "js_unparseable": "❌", "unevaluable": "❌",
                "source_missing": "❌", "js_missing": "❌",
            }.get(it["status"], "? ")
            loc = f"(py:{it.get('py_lineno')})" if it.get("py_lineno") else ""
            print(f"{mark} {it['py_symbol']:<26} {it['status']:<20} {loc}")
        print("──────────────────────────────────────────")
        print(f"指纹 {fp} | drift {len(drifts)} | 缺失 {len(missing)}")
        if L0_PRIVATE:
            print(f"L0 私有常量（不参与比对）: {', '.join(L0_PRIVATE[:4])}…")
        if drifts:
            print("")
            print("⚠️  检测到漂移 —— JS 侧需重新生成：")
            for d in drifts:
                print(f"   - {d.get('py_symbol')}: {d.get('reason')}")
                if "py_value" in d:
                    print(f"       py={d.get('py_value')}")
                    print(f"       js={d.get('js_value')}")
        if missing:
            print("")
            print("❌ 缺失项：")
            for m in missing:
                print(f"   - {m}")
        print("")

    if args.check and (drifts or missing):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
