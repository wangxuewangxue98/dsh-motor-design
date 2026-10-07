# -*- coding: utf-8 -*-
"""
verify_replay_real.py — M1 真实模型回放验证与检测门（G1–G7）
============================================================
G1 每段留出 MAE(C) ≤ MAE(B′ 线性)          逐段不劣化
G2 整体留出 MAE(C) ≤ 0.9×MAE(B′)           ≥10% 改善
G3 每段留出 |bias(C)| ≤ 0.5pp
G4 留出 P95(C) ≤ P95(B′)
G5 留出 >5pp 大误差率(C) ≤ 0.75×(B′)
G6 快照分段覆盖 100%
G7 v3 JSON 纯 numpy 回放 vs sklearn 内存模型 max|Δ| < 1e-6（确定性复训）
"""
import json, os, time
import numpy as np
from sklearn.linear_model import RidgeCV
from sklearn.ensemble import GradientBoostingRegressor
from common_real import (HERE, PKG, load_snapshot, fit_channel, GBR_PARAMS,
                         RIDGE_ALPHAS, metrics, stratified_split,
                         np_predict_model_json, seg_stats)

V3 = os.path.join(PKG, "03_M1产物", "l0_surrogate_family_v3.json")
OUT = os.path.join(PKG, "03_M1产物", "metrics_replay.json")


def main():
    t0 = time.time()
    X, y, raw, seg, meta = load_snapshot()
    resid = y - raw
    tr_all, te_all = stratified_split(seg, 0.2, seed=20260930)

    # ---------- a) 80/20 协议复训（与 metrics_retrain.json 同 seed） ----------
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
        rg = RidgeCV(alphas=RIDGE_ALPHAS).fit(Xs, ri)
        g = GradientBoostingRegressor(**GBR_PARAMS).fit(Xs, ri - rg.predict(Xs))
        pred_B[ite] = raw[te_all][ite] + rg.predict(Xte)
        pred_C[ite] = pred_B[ite] + g.predict(Xte)
        seg_rows[s] = metrics(y[te_all][ite], pred_C[ite])

    mC, mB = metrics(y[te_all], pred_C), metrics(y[te_all], pred_B)
    mA = metrics(y[te_all], raw[te_all])

    # ---------- b) G7：v3 JSON 纯 numpy 回放 vs sklearn 全量重训 ----------
    model_v3 = json.load(open(V3, encoding="utf-8"))
    rng = np.random.default_rng(7)
    sample_idx = rng.choice(len(y), 300, replace=False)
    json_pred = np_predict_model_json(model_v3, X[sample_idx], raw[sample_idx], seg[sample_idx])
    sk_pred = np.empty(len(sample_idx))
    for i, gi in enumerate(sample_idx):
        s = seg[gi]
        idx = np.where(seg == s)[0]
        mu, sd = X[idx].mean(0), X[idx].std(0); sd[sd == 0] = 1.0
        Xs = (X[idx] - mu) / sd
        rg, g = fit_channel(Xs, resid[idx])
        zs = (X[gi] - mu) / sd
        sk_pred[i] = raw[gi] + rg.predict(zs.reshape(1, -1))[0] + g.predict(zs.reshape(1, -1))[0]
    g7_diff = float(np.max(np.abs(json_pred - sk_pred)))

    # ---------- c) 检测门 ----------
    gates = [
        ("G1", "每段留出 MAE(C) ≤ MAE(B′)",
         all(seg_rows[s]["MAE_pp"] <= metrics(y[te_all][hold_seg == s], pred_B[hold_seg == s])["MAE_pp"] + 1e-9
             for s in seg_rows)),
        ("G2", "整体留出 MAE(C) ≤ 0.9×MAE(B′)", mC["MAE_pp"] <= 0.9 * mB["MAE_pp"]),
        ("G3", "每段留出 |bias(C)| ≤ 0.5pp", all(abs(r["bias_pp"]) <= 0.5 for r in seg_rows.values())),
        ("G4", "留出 P95(C) ≤ P95(B′)", mC["P95_pp"] <= mB["P95_pp"]),
        ("G5", "留出 >5pp 率(C) ≤ 0.75×(B′)", mC["gt5pp_pct"] <= 0.75 * mB["gt5pp_pct"]),
        ("G6", "快照分段覆盖 100%", len(seg_stats(seg)) == 6),
        ("G7", "JSON 纯 numpy 回放一致性 < 1e-6", g7_diff < 1e-6),
    ]
    n_pass = sum(p for _, _, p in gates)

    out = {
        "holdout": {"n": int(len(te_all)), "split": "80/20 stratified seed=20260930"},
        "model": model_v3["version"], "base_model": "v2.2.0",
        "holdout_metrics": {"A_raw": mA, "B_linear": mB, "C_retrained": mC},
        "per_segment_C": seg_rows,
        "g7_json_replay_maxdiff": g7_diff,
        "gates": [{"id": g, "desc": d, "pass": bool(p)} for g, d, p in gates],
        "gates_passed": f"{n_pass}/{len(gates)}",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"留出集 n={len(te_all)} | A_raw={mA['MAE_pp']:.3f}  B′={mB['MAE_pp']:.3f}  C={mC['MAE_pp']:.3f}pp")
    for s, r in seg_rows.items():
        mb = metrics(y[te_all][hold_seg == s], pred_B[hold_seg == s])
        print(f"  {s:22s} C_MAE={r['MAE_pp']:.3f} (B′ {mb['MAE_pp']:.3f})  bias={r['bias_pp']:+.3f}  >5pp={r['gt5pp_pct']:.1f}%")
    for g, d, p in gates:
        print(f"  [{'PASS' if p else 'FAIL'}] {g}: {d}")
    print(f"  G7 max|Δ|={g7_diff:.2e}  | 检测门 {n_pass}/{len(gates)} | {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
