# -*- coding: utf-8 -*-
"""
eval_ext_baseline.py — 外部线性基线在留出集上的同尺评估

用途：把本机产出的线性基线（如 2.3.1-fixseg）放到与 v3 重训完全相同的
     80/20 分层留出集（同快照行序 + 同 SEGMENT_RULE + seed=20260930）上回放，
     得到与 C(Ridge+GBR) 可直接对比的 B231 指标。

基线 JSON 约定（兼容两种形态）:
  a) 2.3.1 形态: seg.primary.coef_full(6维完整特征序) + feature_mean/std(active 子集)
     + active_features
  b) v2.2.0 形态: seg.primary.coef(6维) + feature_mean/std(全 5 维)
"""
import json
import os
import sys

import numpy as np

from common_real import (load_snapshot, stratified_split, metrics, FEATS)

SEED = 20260930
TEST_RATIO = 0.2
FAM_MAP = {"异步电动机": "induction_segments", "永磁同步电动机": "pmsm_segments"}


def predict_baseline(model_json, X, seg_arr):
    """回放外部线性基线；PMSM 零方差 dropped 维按 coef=0 处理。"""
    out = np.empty(len(X))
    for i in range(len(X)):
        kind, name = seg_arr[i].split(":")
        seg = model_json[FAM_MAP[kind]][name]
        p = seg["primary"]
        if "coef_full" in p:                       # 2.3.1 形态
            coef_full = np.array(p["coef_full"], dtype=float)
            active = p.get("active_features", FEATS)
            # feature_mean/std 按 active 顺序存储；被剔除维 coef_full=0，
            # mu/sd 取占位值（0/1）不影响点积结果
            mu_map = {f: p["feature_mean"][i] for i, f in enumerate(active)}
            sd_map = {f: p["feature_std"][i] for i, f in enumerate(active)}
            mu = np.array([mu_map.get(f, 0.0) for f in FEATS])
            sd = np.array([sd_map.get(f, 1.0) for f in FEATS])
        else:                                       # v2.2.0 形态
            coef_full = np.array(p["coef"], dtype=float)
            mu = np.array(p["feature_mean"], dtype=float)
            sd = np.array(p["feature_std"], dtype=float)
        sd[sd == 0] = 1.0
        zs = (X[i] - mu) / sd
        out[i] = coef_full[0] + float(np.dot(coef_full[1:], zs))
    return out


def main():
    baseline_path, out_path = sys.argv[1], sys.argv[2]
    model = json.load(open(baseline_path, encoding="utf-8"))
    X, y, raw, seg, meta = load_snapshot()
    _, te = stratified_split(seg, TEST_RATIO, SEED)

    yhat = predict_baseline(model, X[te], seg[te])
    m = metrics(y[te], yhat)

    per_seg = {}
    for s in sorted(set(seg[te])):
        idx = np.where(seg[te] == s)[0]
        per_seg[s] = metrics(y[te][idx], yhat[idx])
        per_seg[s]["n_holdout"] = int(len(idx))

    out = {"baseline_version": model.get("version"),
           "holdout_n": int(len(te)),
           "overall": m, "per_segment": per_seg}
    json.dump(out, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"\n===== 外部基线留出评估: {model.get('version')} =====")
    print(f"留出 n={len(te)}  MAE={m['MAE_pp']:.3f}pp  bias={m['bias_pp']:+.3f}pp  "
          f"P95={m['P95_pp']:.2f}pp  >5pp={m['gt5pp_pct']:.1f}%")
    for s, ms in per_seg.items():
        print(f"  {s}: n={ms['n_holdout']} MAE={ms['MAE_pp']:.3f}  "
              f"bias={ms['bias_pp']:+.3f}  >5pp={ms['gt5pp_pct']:.1f}%")


if __name__ == "__main__":
    main()
