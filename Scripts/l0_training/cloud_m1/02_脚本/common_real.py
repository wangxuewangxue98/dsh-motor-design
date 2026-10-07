# -*- coding: utf-8 -*-
"""
common_real.py — L0 真实数据重训流水线共享库
=============================================
数据：M0 残差快照（真实，4955 条，sha256=66ba97c7… 与原清单一致）
基线：v2.2.0 分段线性校正器（真实，sha256=4ea15e4a…）
语义：客户端先得 L0 原始效率(l0_eff)，校正器将其修正到 L1 高保真水平(l1_eff)。
      特征 = [stator_od, stator_id, core_length, poles, l0_eff]（l0_eff 自身入模）。
"""
import json, math, os
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
# 本机落地：SNAPSHOT/BASELINE 指向仓库既有数据，不重复拷贝（方案 §4 脚本放仓库）
# CI / 云环境通过环境变量覆盖（保留 Windows 默认值以兼容本机调用）
SNAPSHOT = os.environ.get(
    "L0_SNAPSHOT",
    r"D:\MotorDesign\output\l0_full_20260930\l0_residuals_snapshot_full.jsonl",
)
BASELINE = os.environ.get(
    "L0_BASELINE",
    r"D:\MotorDesign\motor-ai-l0\models\l0_surrogate_family.json",
)

FEATS = ["stator_od", "stator_id", "core_length", "poles", "l0_eff"]
TARGET = "l1_eff"

# 段边界（继承 v2.2.0 名义分档，medium/large 之间的空隙归 medium，保证 100% 覆盖）
SEGMENT_RULE = {
    "异步电动机": [("small", 0.0, 373.1000), ("medium", 373.1000, 903.8000), ("large", 903.8000, 1e9)],
    "永磁同步电动机": [("small", 0.0, 857.7000), ("medium", 857.7000, 882.6000), ("large", 882.6000, 1e9)],
}


def load_snapshot(path=SNAPSHOT):
    X, y, raw, seg, meta = [], [], [], [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            X.append([r[k] for k in FEATS])
            y.append(r[TARGET])
            raw.append(r["l0_eff"])
            seg.append(assign_segment(r))
            meta.append({"id": r["id"], "ts": r["timestamp"],
                         "motor_type": r["motor_type"], "poles": r["poles"]})
    return (np.array(X, dtype=float), np.array(y, dtype=float),
            np.array(raw, dtype=float), np.array(seg), meta)


def assign_segment(r):
    for name, lo, hi in SEGMENT_RULE[r["motor_type"]]:
        if lo <= r["stator_od"] < hi:
            return f"{r['motor_type']}:{name}"
    raise ValueError(f"unsegmented: {r}")


def seg_stats(seg_arr):
    from collections import Counter
    return dict(Counter(seg_arr))


# ----------------------------------------------------------------------
# 分层划分（motor_type × 段 等比例）
# ----------------------------------------------------------------------
def stratified_split(seg_arr, test_ratio=0.2, seed=20260930):
    rng = np.random.default_rng(seed)
    test_idx = []
    for s in np.unique(seg_arr):
        idx = np.where(seg_arr == s)[0]
        rng.shuffle(idx)
        k = max(1, int(round(len(idx) * test_ratio)))
        test_idx.extend(idx[:k])
    test_idx = np.array(sorted(test_idx))
    mask = np.ones(len(seg_arr), dtype=bool)
    mask[test_idx] = False
    train_idx = np.where(mask)[0]
    return train_idx, test_idx


# ----------------------------------------------------------------------
# 模型 C：Ridge(残差) + GBR(残差修正) —— 训练与导出
# ----------------------------------------------------------------------
RIDGE_ALPHAS = np.logspace(-3, 3, 25)
GBR_PARAMS = dict(n_estimators=400, max_depth=3, learning_rate=0.05,
                  subsample=0.85, min_samples_leaf=10, init="zero", random_state=7)


def fit_channel(Xs, resid):
    from sklearn.linear_model import RidgeCV
    from sklearn.ensemble import GradientBoostingRegressor
    rg = RidgeCV(alphas=RIDGE_ALPHAS).fit(Xs, resid)
    g = GradientBoostingRegressor(**GBR_PARAMS).fit(Xs, resid - rg.predict(Xs))
    return rg, g


def export_trees(gbr):
    trees = []
    for est in gbr.estimators_[:, 0]:
        t = est.tree_
        trees.append({
            "feature": t.feature.tolist(),
            "threshold": [round(float(v), 8) for v in t.threshold],
            "left": t.children_left.tolist(),
            "right": t.children_right.tolist(),
            "values": [round(float(v), 8) for v in t.value[:, 0, 0]],
        })
    return trees


def np_tree_predict(tree, X):
    feat = np.asarray(tree["feature"]); thr = np.asarray(tree["threshold"])
    left = np.asarray(tree["left"]); right = np.asarray(tree["right"])
    val = np.asarray(tree["values"])
    is_leaf = left == -1
    idx = np.zeros(len(X), dtype=int)
    active = np.ones(len(X), dtype=bool)
    while active.any():
        i = idx[active]
        step = np.where(is_leaf[i], i,
                        np.where(X[active, feat[i]] <= thr[i], left[i], right[i]))
        idx[active] = step
        active = ~is_leaf[idx]
    return val[idx]


def np_gbr_predict(seg_json, Xs):
    gbr = seg_json["gbr"]
    out = np.full(len(Xs), gbr.get("init", 0.0), dtype=float)
    lr = gbr["learning_rate"]
    for tree in gbr["trees"]:
        out += lr * np_tree_predict(tree, Xs)
    return out


def np_predict_model_json(model_json, X, raw_l0, seg_arr):
    """纯 numpy 回放：l1_hat = primary 线性(客户端公式) + gbr 修正（若段含 gbr）"""
    X = np.atleast_2d(np.asarray(X, dtype=float))
    out = np.empty(len(X))
    fam_map = {"异步电动机": "induction_segments", "永磁同步电动机": "pmsm_segments"}
    for i in range(len(X)):
        kind, name = seg_arr[i].split(":")
        seg = model_json[fam_map[kind]][name]
        p = seg["primary"]
        mu = np.array(p["feature_mean"]); sd = np.array(p["feature_std"])
        sd[sd == 0] = 1.0
        zs = (X[i] - mu) / sd
        yhat = p["coef"][0] + float(np.dot(np.array(p["coef"][1:]), zs))
        if seg.get("gbr"):
            yhat += float(np_gbr_predict(seg, zs.reshape(1, -1))[0])
        out[i] = yhat
    return out


def metrics(y, yhat):
    e = np.asarray(yhat, dtype=float) - np.asarray(y, dtype=float)
    ss = float(np.sum((y - y.mean()) ** 2))
    return {"MAE_pp": float(np.mean(np.abs(e))),
            "RMSE_pp": float(np.sqrt(np.mean(e ** 2))),
            "R2": float(1 - np.sum(e ** 2) / ss) if ss > 0 else float("nan"),
            "bias_pp": float(np.mean(e)),
            "P95_pp": float(np.percentile(np.abs(e), 95)),
            "gt5pp_pct": float(100 * np.mean(np.abs(e) > 5))}
