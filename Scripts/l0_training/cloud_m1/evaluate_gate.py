# -*- coding: utf-8 -*-
"""
evaluate_gate.py — L0 重训门禁（M2 核心，云作业训练后调用）
=============================================================
对一份候选模型 JSON 跑 G1–G7 检测门，输出门禁报告 JSON + 判定。
与 verify_replay_real.py 同逻辑，但路径全部参数化（不依赖固定 PKG 布局），
可直接被 retrain_pipeline.py / CI 调度器调用。

G1 每段留出 MAE(C) ≤ MAE(B′ 线性)
G2 整体留出 MAE(C) ≤ 0.9×MAE(B′)
G3 每段留出 |bias(C)| ≤ 0.5pp
G4 留出 P95(C) ≤ P95(B′)
G5 留出 >5pp 率(C) ≤ 0.75×(B′)
G6 快照分段覆盖 100%（6 段齐）
G7 v3 JSON 纯 numpy 回放 vs sklearn 全量重训 max|Δ| < 1e-6

用法:
  python evaluate_gate.py --model PATH --snapshot PATH --baseline PATH \
                          --out PATH [--seed 20260930]
门禁不通过时退出码非 0（供 CI 阻断发布）。
"""
import argparse
import json
import os
import sys
import time

import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "02_脚本"))
import common_real as cr
import sklearn
# 方案 B：锁 sklearn 版本，保证 G7 复现口径与训练一致。
assert sklearn.__version__ == "1.9.1", \
    f"[环境锁] evaluate_gate 需在 sklearn==1.9.1 运行（G7 复现口径），当前 {sklearn.__version__}。"

FAM_MAP = {"异步电动机": "induction_segments", "永磁同步电动机": "pmsm_segments"}


def predict_model_json(model, X, raw, seg):
    out = np.empty(len(X))
    for i in range(len(X)):
        kind, name = seg[i].split(":")
        fam = model[FAM_MAP[kind]]
        key = name if name in fam else next(k for k in fam if k.startswith(name + "("))
        p = fam[key]["primary"]
        mu = np.array(p["feature_mean"], dtype=float)
        sd = np.array(p["feature_std"], dtype=float)
        sd[sd == 0] = 1.0
        zs = (X[i] - mu) / sd
        out[i] = p["coef"][0] + float(np.dot(np.array(p["coef"][1:]), zs))
        if "gbr" in fam[key]:
            g = fam[key]["gbr"]
            # 纯 numpy 重放 GBR 树（与 verify_replay_real 同口径）
            xz = zs
            for tree in g["trees"]:
                out[i] += g["learning_rate"] * _tree_pred(tree, xz)
    return out


def _tree_pred(tree, x):
    feat = np.asarray(tree["feature"]); thr = np.asarray(tree["threshold"])
    left = np.asarray(tree["left"]); right = np.asarray(tree["right"])
    val = np.asarray(tree["values"])
    node = 0
    while True:
        f = int(feat[node]); t = thr[node]
        if x[f] <= t:
            nxt = int(left[node])
        else:
            nxt = int(right[node])
        if nxt == -1:
            break
        node = nxt
    return float(val[node])


def run_gate(model_path, snapshot_path, baseline_path, seed=20260930):
    cr.SNAPSHOT = snapshot_path
    X, y, raw, seg, meta = cr.load_snapshot()
    resid = y - raw
    tr_all, te_all = cr.stratified_split(seg, 0.2, seed=seed)
    hold_seg = seg[te_all]
    pred_C = np.zeros(len(te_all)); pred_B = np.zeros(len(te_all))
    seg_rows = {}
    for s in np.unique(hold_seg):
        itr = np.where(seg[tr_all] == s)[0]
        ite = np.where(hold_seg == s)[0]
        Xi, ri = X[tr_all][itr], resid[tr_all][itr]
        bi = raw[tr_all][itr]
        mu, sd = Xi.mean(0), Xi.std(0); sd[sd == 0] = 1.0
        Xs = (Xi - mu) / sd
        Xte = (X[te_all][ite] - mu) / sd
        rg = __import__("sklearn.linear_model", fromlist=["RidgeCV"]).RidgeCV(
            alphas=cr.RIDGE_ALPHAS).fit(Xs, ri)
        g = __import__("sklearn.ensemble", fromlist=["GradientBoostingRegressor"]).GradientBoostingRegressor(
            **cr.GBR_PARAMS).fit(Xs, ri - rg.predict(Xs))
        pred_B[ite] = raw[te_all][ite] + rg.predict(Xte)
        pred_C[ite] = pred_B[ite] + g.predict(Xte)
        seg_rows[s] = cr.metrics(y[te_all][ite], pred_C[ite])

    mC, mB = cr.metrics(y[te_all], pred_C), cr.metrics(y[te_all], pred_B)

    # G7: 纯 numpy 回放 vs 重训
    model_v3 = json.load(open(model_path, encoding="utf-8"))
    rng = np.random.default_rng(7)
    sample_idx = rng.choice(len(y), 300, replace=False)
    json_pred = predict_model_json(model_v3, X[sample_idx], raw[sample_idx], seg[sample_idx])
    sk_pred = np.empty(len(sample_idx))
    for k, gi in enumerate(sample_idx):
        s = seg[gi]; idx = np.where(seg == s)[0]
        mu, sd = X[idx].mean(0), X[idx].std(0); sd[sd == 0] = 1.0
        Xs = (X[idx] - mu) / sd
        rg, g = cr.fit_channel(Xs, resid[idx])
        zs = (X[gi] - mu) / sd
        sk_pred[k] = raw[gi] + rg.predict(zs.reshape(1, -1))[0] + g.predict(zs.reshape(1, -1))[0]
    g7_diff = float(np.max(np.abs(json_pred - sk_pred)))

    gates = [
        ("G1", "每段留出 MAE(C) ≤ MAE(B′)",
         all(seg_rows[s]["MAE_pp"] <= cr.metrics(y[te_all][hold_seg == s],
             pred_B[hold_seg == s])["MAE_pp"] + 1e-9 for s in seg_rows)),
        ("G2", "整体留出 MAE(C) ≤ 0.9×MAE(B′)", mC["MAE_pp"] <= 0.9 * mB["MAE_pp"]),
        ("G3", "每段留出 |bias(C)| ≤ 0.5pp", all(abs(r["bias_pp"]) <= 0.5 for r in seg_rows.values())),
        ("G4", "留出 P95(C) ≤ P95(B′)", mC["P95_pp"] <= mB["P95_pp"]),
        ("G5", "留出 >5pp 率(C) ≤ 0.75×(B′)", mC["gt5pp_pct"] <= 0.75 * mB["gt5pp_pct"]),
        ("G6", "快照分段覆盖 100%", len(cr.seg_stats(seg)) == 6),
        ("G7", "JSON 纯 numpy 回放一致性 < 1e-6", g7_diff < 1e-6),
    ]
    n_pass = sum(1 for _, _, p in gates if p)
    out = {
        "model": model_v3.get("version"), "base_model": "v2.2.0",
        "holdout": {"n": int(len(te_all)), "split": f"80/20 stratified seed={seed}"},
        "holdout_metrics": {"B_linear": mB, "C_retrained": mC},
        "per_segment_C": seg_rows,
        "g7_json_replay_maxdiff": g7_diff,
        "gates": [{"id": g, "desc": d, "pass": bool(p)} for g, d, p in gates],
        "gates_passed": f"{n_pass}/{len(gates)}",
        "passed": n_pass == len(gates),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return out, gates, n_pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20260930)
    args = ap.parse_args()
    out, gates, n_pass = run_gate(args.model, args.snapshot, args.baseline, args.seed)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"留出集 n={out['holdout']['n']} | B′={out['holdout_metrics']['B_linear']['MAE_pp']:.3f} "
          f"C={out['holdout_metrics']['C_retrained']['MAE_pp']:.3f}pp")
    for g, d, p in gates:
        print(f"  [{'PASS' if p else 'FAIL'}] {g}: {d}")
    print(f"  检测门 {n_pass}/{len(gates)} | 结论: {'PASS ✅' if out['passed'] else 'FAIL ❌ 阻断发布'}")
    raise SystemExit(0 if out["passed"] else 1)


if __name__ == "__main__":
    main()
