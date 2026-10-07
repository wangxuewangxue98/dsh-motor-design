# -*- coding: utf-8 -*-
"""
ab_eval.py — 闭环 A/B 评估骨架（M3 核心，可本地运行）
====================================================
用 RMxprt 真解（l1_eff）作金标，在「同规格候选集」内比较两种排序器
（surrogate v3.1.0 vs formula 原始 l0）的 TopN 效率排序质量：
  - Top1 命中率：预测第 1 是否等于真解第 1
  - Top3 命中率：真解第 1 是否落在预测 Top3
  - 组内 Spearman 相关系数（全序相关）
这是方案生成阶段「surrogate 默认开启 + A/B」能否上线的量化判据。

用法:
  python ab_eval.py --model PATH --snapshot PATH --baseline PATH
候选集构造：按 (motor_type, OD 50mm 桶) 聚合，组内多候选才有排序意义。
"""
import argparse
import json
import os
import sys

import numpy as np
from scipy.stats import spearmanr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "02_脚本"))
import common_real as cr

FAM_MAP = {"异步电动机": "induction_segments", "永磁同步电动机": "pmsm_segments"}


def predict_v3(model, X, raw, seg):
    out = np.empty(len(X))
    for i in range(len(X)):
        kind, name = seg[i].split(":")
        fam = model[FAM_MAP[kind]]
        key = name if name in fam else next(k for k in fam if k.startswith(name + "("))
        p = fam[key]["primary"]
        mu = np.array(p["feature_mean"], float); sd = np.array(p["feature_std"], float)
        sd[sd == 0] = 1.0
        zs = (X[i] - mu) / sd
        out[i] = p["coef"][0] + float(np.dot(np.array(p["coef"][1:]), zs))
        if "gbr" in fam[key]:
            for tree in fam[key]["gbr"]["trees"]:
                out[i] += fam[key]["gbr"]["learning_rate"] * _tree(tree, zs)
    return out


def _tree(tree, x):
    feat = np.asarray(tree["feature"]); thr = np.asarray(tree["threshold"])
    left = np.asarray(tree["left"]); right = np.asarray(tree["right"]); val = np.asarray(tree["values"])
    node = 0
    while True:
        nxt = int(left[node]) if x[int(feat[node])] <= thr[node] else int(right[node])
        if nxt == -1:
            break
        node = nxt
    return float(val[node])


def build_groups(seg, X, bins=50.0):
    """(motor_type, OD 桶) 聚合为候选集；组内样本数≥3 才计入排序评测。"""
    groups = {}
    for i in range(len(seg)):
        kind, name = seg[i].split(":")
        od_bin = int(X[i, 0] // bins)
        key = (kind, name, od_bin)
        groups.setdefault(key, []).append(i)
    return {k: v for k, v in groups.items() if len(v) >= 3}


def eval_ranker(y_true, pred, idxs):
    top1 = top3 = 0
    rhos = []
    for g in idxs.values():
        if len(g) < 3:
            continue
        gt = y_true[g]; pr = pred[g]
        order_pred = np.argsort(-pr)            # 降序（效率越高越好）
        order_true = np.argsort(-gt)
        if order_pred[0] == order_true[0]:
            top1 += 1
        if order_true[0] in order_pred[:3]:
            top3 += 1
        if len(g) >= 4:
            rho, _ = spearmanr(gt, pr)
            if np.isfinite(rho):
                rhos.append(rho)
    n = len(idxs)
    return {"n_groups": n, "top1_hit": top1 / n, "top3_hit": top3 / n,
            "spearman_mean": float(np.mean(rhos)) if rhos else float("nan")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--baseline", required=True)
    args = ap.parse_args()
    cr.SNAPSHOT = args.snapshot
    X, y, raw, seg, _ = cr.load_snapshot()
    groups = build_groups(seg, X)

    model = json.load(open(args.model, encoding="utf-8"))
    pred_v3 = predict_v3(model, X, raw, seg)
    pred_formula = raw.copy()                    # formula 基线 = 原始 l0 效率

    r_v3 = eval_ranker(y, pred_v3, groups)
    r_f = eval_ranker(y, pred_formula, groups)
    print(f"候选集组数(组内≥3): {len(groups)}")
    print(f"{'排序器':12s} {'Top1命中':>9s} {'Top3命中':>9s} {'Spearman':>9s}")
    print(f"{'formula(l0)':12s} {r_f['top1_hit']:9.3f} {r_f['top3_hit']:9.3f} {r_f['spearman_mean']:9.3f}")
    print(f"{'surrogate':12s} {r_v3['top1_hit']:9.3f} {r_v3['top3_hit']:9.3f} {r_v3['spearman_mean']:9.3f}")
    print(f"\n结论: surrogate 相对 formula 的 Top1 提升 "
          f"{100*(r_v3['top1_hit']-r_f['top1_hit']):+.1f}pt / "
          f"Top3 提升 {100*(r_v3['top3_hit']-r_f['top3_hit']):+.1f}pt")


if __name__ == "__main__":
    main()
