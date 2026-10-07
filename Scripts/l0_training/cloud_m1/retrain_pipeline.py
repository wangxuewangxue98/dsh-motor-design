# -*- coding: utf-8 -*-
"""
retrain_pipeline.py — L0 云端重训编排（M2 核心，调度器/CI 单命令入口）
================================================================================
串联：抽取(可选) → 训练(Ridge+GBR v3) → 门禁(G1-G7) → 登记注册表。
门禁不通过则阻断（非零退出），保留上一版 current 指针，不覆盖线上模型。

依赖（本仓库内）:
  Scripts/l0_training/extract_l0_residuals.py          (M0 抽取，--full 全量)
  cloud_m1/02_脚本/train_surrogate_family_real.py      (训练 → 03_M1产物/v3.json)
  cloud_m1/evaluate_gate.py                            (门禁 → 03_M1产物/gate_report.json)
  cloud_m1/model_registry.py                           (登记/回滚)

用法:
  python retrain_pipeline.py [--full] [--version 3.1.x] [--notes S]
  --full  先跑 extract_l0_residuals.py --full 刷新快照（事件/定时触发时用）
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # Scripts/l0_training
EXTRACT = os.path.join(ROOT, "extract_l0_residuals.py")
TRAIN = os.path.join(HERE, "02_脚本", "train_surrogate_family_real.py")
GATE = os.path.join(HERE, "evaluate_gate.py")
REGISTER = os.path.join(HERE, "model_registry.py")
SNAPSHOT = os.environ.get(
    "L0_SNAPSHOT",
    r"D:\MotorDesign\output\l0_full_20260930\l0_residuals_snapshot_full.jsonl",
)
BASELINE = os.environ.get(
    "L0_BASELINE",
    r"D:\MotorDesign\motor-ai-l0\models\l0_surrogate_family.json",
)
EXTRACT_OUT_DIR = os.environ.get(
    "L0_EXTRACT_OUT_DIR",
    r"D:\MotorDesign\output\l0_full_20260930",
)
V3_JSON = os.path.join(HERE, "03_M1产物", "l0_surrogate_family_v3.json")
GATE_JSON = os.path.join(HERE, "03_M1产物", "gate_report.json")
METRICS = os.path.join(HERE, "03_M1产物", "metrics_retrain.json")
# Python 解释器：CI 用 workflow 的 python3，本机默认走隔离 venv（保持原行为）
PY = os.environ.get(
    "L0_PYTHON",
    r"C:\Users\15389\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
)


def run(cmd):
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=os.path.dirname(TRAIN))
    if r.returncode != 0:
        print(f"  ❌ 步骤失败 exit={r.returncode}")
        raise SystemExit(r.returncode)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="先刷新全量快照")
    ap.add_argument("--version", default="3.1.0-fixseg")
    ap.add_argument("--notes", default="云端重训流水线自动产出")
    args = ap.parse_args()
    t0 = time.time()

    if args.full:
        run([PY, EXTRACT, "--full",
             "--out-dir", EXTRACT_OUT_DIR,
             "--out", "l0_residuals_snapshot_full.jsonl"])

    run([PY, TRAIN])                                   # → 03_M1产物/v3.json + metrics
    run([PY, GATE, "--model", V3_JSON, "--snapshot", SNAPSHOT,
         "--baseline", BASELINE, "--out", GATE_JSON])     # → gate_report.json

    # 门禁报告判定
    import json
    gate = json.load(open(GATE_JSON, encoding="utf-8"))
    if not gate["passed"]:
        print(f"\n❌ 门禁 {gate['gates_passed']} 未全过 — 阻断发布，保留上一版 current")
        raise SystemExit(2)

    run([PY, REGISTER, "register", "--version", args.version,
         "--model", V3_JSON, "--metrics", METRICS, "--gate", GATE_JSON,
         "--notes", args.notes])
    print(f"\n✅ 流水线完成 {time.time()-t0:.0f}s | 模型 {args.version} 已登记 | current 指针见 registry")


if __name__ == "__main__":
    main()
