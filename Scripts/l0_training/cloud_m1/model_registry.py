# -*- coding: utf-8 -*-
"""
model_registry.py — L0 代理模型注册表（M2 核心：版本化 + 灰度 + 回滚）
================================================================================
本地 JSON 账本，记录每一次重训产出的模型版本、指标、门禁结果、当前指针。
设计原则（安全）：
  - 只读真值湖、不碰 AEDT/COM（重训只算数，方案 §3）；
  - 回滚只改「当前指针」+ 受控 `export --to PATH`，绝不自动覆盖线上插件模型
    （避免误伤真源 D:/dsh-motor-ai-l0 与镜像 D:/MotorDesign/motor-ai-l0）；
  - 版本幂等登记，重复 register 覆盖元数据但保留历史顺序。

CLI:
  register   --version V --model PATH [--metrics PATH] [--gate PATH] [--notes S]
  list
  current
  rollback   --to V            # 仅改 current 指针，并提示需手动 export
  export     --to PATH         # 将 current 指向的模型导出到指定部署槽
  show       --version V
"""
import argparse
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "registry", "l0_registry.json")


def _load():
    if not os.path.exists(REGISTRY):
        return {"schema": "l0_model_registry", "current": None, "versions": {}}
    with open(REGISTRY, encoding="utf-8") as f:
        return json.load(f)


def _save(reg):
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)


def register(version, model, metrics=None, gate=None, notes=""):
    reg = _load()
    model_abs = os.path.abspath(model)
    if not os.path.exists(model_abs):
        raise FileNotFoundError(model_abs)
    entry = {
        "model": model_abs,
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metrics": os.path.abspath(metrics) if metrics else None,
        "gate": os.path.abspath(gate) if gate else None,
        "notes": notes,
        "is_current": False,
    }
    # 门禁摘要
    if gate and os.path.exists(gate):
        try:
            g = json.load(open(gate, encoding="utf-8"))
            entry["gates_passed"] = g.get("gates_passed")
        except Exception:
            pass
    reg["versions"][version] = entry
    if reg["current"] is None:
        reg["current"] = version
    _save(reg)
    print(f"[register] {version} -> {model_abs}")
    print(f"  current 仍为: {reg['current']}（如需切换请 rollback --to {version}）")
    return reg


def list_versions():
    reg = _load()
    print(f"current = {reg['current']}")
    print(f"{'version':18s} {'gates':8s} {'registered_at':22s} notes")
    for v, e in reg["versions"].items():
        mark = " *" if v == reg["current"] else "  "
        print(f"{mark}{v:16s} {str(e.get('gates_passed','-')):8s} "
              f"{e.get('registered_at','-'):22s} {e.get('notes','')}")


def current():
    reg = _load()
    print(reg["current"])


def rollback(version):
    reg = _load()
    if version not in reg["versions"]:
        raise KeyError(f"unknown version: {version}; known={list(reg['versions'])}")
    reg["current"] = version
    _save(reg)
    m = reg["versions"][version]["model"]
    print(f"[rollback] current -> {version}")
    print(f"  模型文件: {m}")
    print(f"  提示: 受控导出用 `python model_registry.py export --to <部署槽路径>`")


def export_to(path):
    reg = _load()
    cur = reg["current"]
    if cur is None:
        raise RuntimeError("current 为空，无法导出；先 register / rollback")
    src = reg["versions"][cur]["model"]
    dst = os.path.abspath(path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[export] {cur} -> {dst}  ({os.path.getsize(src)} bytes)")


def show(version):
    reg = _load()
    print(json.dumps(reg["versions"].get(version, {}), ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register")
    p.add_argument("--version", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--metrics")
    p.add_argument("--gate")
    p.add_argument("--notes", default="")
    sub.add_parser("list").set_defaults(func=lambda a: list_versions())
    sub.add_parser("current").set_defaults(func=lambda a: current())
    p2 = sub.add_parser("rollback")
    p2.add_argument("--to", required=True)
    p3 = sub.add_parser("export")
    p3.add_argument("--to", required=True)
    p4 = sub.add_parser("show")
    p4.add_argument("--version", required=True)
    args = ap.parse_args()
    if args.cmd == "register":
        register(args.version, args.model, args.metrics, args.gate, args.notes)
    elif args.cmd == "list":
        list_versions()
    elif args.cmd == "current":
        current()
    elif args.cmd == "rollback":
        rollback(args.to)
    elif args.cmd == "export":
        export_to(args.to)
    elif args.cmd == "show":
        show(args.version)


if __name__ == "__main__":
    main()
