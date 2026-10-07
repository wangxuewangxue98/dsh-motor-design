# -*- coding: utf-8 -*-
"""
train_surrogate_family_real.py — L0 校正器云端重训（真实数据 M1）
=================================================================
模型 C：每段 Ridge(残差 l1−l0) + GBR(残差修正)。
导出 schema v3.0.0，向后兼容 v2.2.0 客户端公式：
    primary.coef 折算：w0=μ5+c0, w5=σ5+c5, wj=cj (j=1..4)
    （客户端老公式 l1=w0+Σwj·z 仍精确成立，只是吸收了 l0 的 +1 基座）
    gbr 为可选增强字段，旧客户端忽略后自动退化为线性校正（仍优于 v2.2.0）。
产出：
    03_M1产物/l0_surrogate_family_v3.json        （全量 4955 训练，部署用）
    03_M1产物/metrics_retrain.json               （5 折 CV + 80/20 留出指标）
"""
import json, os, time
import numpy as np
from sklearn.model_selection import KFold, StratifiedKFold
from sklearn.linear_model import RidgeCV
from sklearn.ensemble import GradientBoostingRegressor
import sklearn
# 方案 B：锁定 sklearn 版本，使 G7（纯 numpy 回放 vs sklearn 全量重训）可复现通过。
# 云端重训必须在 requirements.txt 锁定的环境运行，否则门禁无法复现。
assert sklearn.__version__ == "1.9.1", \
    f"[环境锁] sklearn 必须锁定 1.9.1 才能保证 G7 可复现通过，当前 {sklearn.__version__}。" \
    f"请在 requirements.txt 指定的环境运行云端重训。"
from common_real import (HERE, PKG, BASELINE, load_snapshot, fit_channel, export_trees,
                         GBR_PARAMS, RIDGE_ALPHAS, metrics, stratified_split,
                         SEGMENT_RULE)

OUT_JSON = os.path.join(PKG, "03_M1产物", "l0_surrogate_family_v3.json")
OUT_METRICS = os.path.join(PKG, "03_M1产物", "metrics_retrain.json")
FAM_MAP = {"异步电动机": "induction_segments", "永磁同步电动机": "pmsm_segments"}


def standardize(X):
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    return (X - mu) / sd, mu, sd


def fold_predict_C(Xs_seg, resid, base, y, tr, te):
    """折内训练 C，返回 te 上的 l1 预测"""
    rg, g = fit_channel(Xs_seg[tr], resid[tr])
    return base[te] + rg.predict(Xs_seg[te]) + g.predict(Xs_seg[te])


def main():
    t0 = time.time()
    X, y, raw, seg, meta = load_snapshot()
    n = len(y)
    resid_full = y - raw                      # 相对 raw l0 的残差（C 的学习目标）

    # ---------- 1) 全量 5 折 CV（段内）：C vs B′(线性) vs A(raw) ----------
    cv_pred_C = np.zeros(n)
    cv_pred_B = np.zeros(n)
    cv_metrics = {}
    for s in np.unique(seg):
        idx = np.where(seg == s)[0]
        Xs, _, _ = standardize(X[idx])
        kf = KFold(5, shuffle=True, random_state=42)
        for tr, te in kf.split(Xs):
            cv_pred_B[idx[te]] = raw[idx][te] + RidgeCV(alphas=RIDGE_ALPHAS).fit(
                Xs[tr], resid_full[idx][tr]).predict(Xs[te])
            cv_pred_C[idx[te]] = fold_predict_C(Xs, resid_full[idx], raw[idx], y[idx], tr, te)
        mC, mB = metrics(y[idx], cv_pred_C[idx]), metrics(y[idx], cv_pred_B[idx])
        cv_metrics[s] = {"n": int(len(idx)),
                         "cv_MAE_C": mC["MAE_pp"], "cv_MAE_B": mB["MAE_pp"],
                         "improve_pct": float(100 * (1 - mC["MAE_pp"] / mB["MAE_pp"]))}
        print(f"CV {s:22s} n={len(idx):5d}  B′={mB['MAE_pp']:.3f}pp  C={mC['MAE_pp']:.3f}pp  ({cv_metrics[s]['improve_pct']:+.1f}%)")
    mC_cv, mB_cv, mA_cv = metrics(y, cv_pred_C), metrics(y, cv_pred_B), metrics(y, raw)

    # ---------- 2) 80/20 分层留出 ----------
    tr_all, te_all = stratified_split(seg, 0.2, seed=20260930)
    hold_pred_C = np.zeros(len(te_all))
    hold_pred_B = np.zeros(len(te_all))
    hold_seg = seg[te_all]
    for s in np.unique(hold_seg):
        itr = np.where(seg[tr_all] == s)[0]
        ite = np.where(hold_seg == s)[0]
        Xi, yi, ri, bi = X[tr_all][itr], y[tr_all][itr], resid_full[tr_all][itr], raw[tr_all][itr]
        Xs, _, _ = standardize(Xi)
        Xte = (X[te_all][ite] - Xi.mean(0)) / np.where(Xi.std(0) == 0, 1, Xi.std(0))
        rg = RidgeCV(alphas=RIDGE_ALPHAS).fit(Xs, ri)
        g = GradientBoostingRegressor(**GBR_PARAMS).fit(Xs, ri - rg.predict(Xs))
        hold_pred_B[ite] = raw[te_all][ite] + rg.predict(Xte)
        hold_pred_C[ite] = raw[te_all][ite] + rg.predict(Xte) + g.predict(Xte)
    mC_h, mB_h = metrics(y[te_all], hold_pred_C), metrics(y[te_all], hold_pred_B)
    mA_h, mV_h = metrics(y[te_all], raw[te_all]), metrics(y[te_all], np_predict_v22_hold(X[te_all], seg[te_all], raw[te_all]))

    # ---------- 3) 全量重训 + 导出 v3.0.0 ----------
    family = {"induction_segments": {}, "pmsm_segments": {}}
    for s in np.unique(seg):
        kind, name = s.split(":")
        idx = np.where(seg == s)[0]
        Xs, mu, sd = standardize(X[idx])
        rg, g = fit_channel(Xs, resid_full[idx])
        c = rg.coef_; c0 = float(rg.intercept_)
        mu5, s5 = float(mu[4]), float(sd[4])
        w = [0.0] * 6
        w[0] = mu5 + c0                       # 折算：吸收 l0 基座
        w[1:5] = [float(v) for v in c[:4]]
        w[5] = s5 + float(c[4])
        family[FAM_MAP[kind]][name] = {
            "od_range": [float(X[idx, 0].min()), float(X[idx, 0].max())],
            "n": int(len(idx)),
            "primary": {
                "coef": w,
                "cv_r2": mC_cv["R2"],          # 整体口径参考；段内 CV 见 metrics
                "cv_mae_pp": cv_metrics[s]["cv_MAE_C"],
                "train_n": int(len(idx)),
                "l0_baseline_mae_pp": metrics(y[idx], raw[idx])["MAE_pp"],
                "feature_mean": [float(v) for v in mu],
                "feature_std": [float(v) for v in sd],
                "formula": "l1_hat = coef[0] + sum(coef[j]*(x_j-mean_j)/std_j)  (客户端 v2.2.0 兼容)",
            },
            "gbr": {"init": 0.0, "learning_rate": GBR_PARAMS["learning_rate"],
                    "n_estimators": GBR_PARAMS["n_estimators"], "max_depth": GBR_PARAMS["max_depth"],
                    "trees": export_trees(g),
                    "note": "可选增强：l1_hat += learning_rate*sum(trees(标准化特征))；旧客户端忽略本字段"},
            "error": None,
        }
    model_v3 = {
        "schema": "l0_surrogate_family",
        "version": "3.1.0-fixseg",
        "experimental": True,
        "features": ["stator_od", "stator_id", "core_length", "poles", "l0_eff"],
        "target": "l1_eff",
        "gate": {"type": "confidence", "threshold": 0.4},
        "lambda": 1.0,
        "segment_rule": {k: [[n2, lo, hi] for n2, lo, hi in v] for k, v in SEGMENT_RULE.items()},
        "residual_model": "ridge_plus_gbr (M1 重建)",
        "trained_on": {"snapshot": "l0_residuals_snapshot_full.jsonl (6029, sha256=f89bdd87…)",
                       "generated_at": time.strftime("%Y-%m-%d")},
        "base_model": "v2.2.0 (sha256=4ea15e4a…)",
        "data_sources": {"primary": "designs.db.l0_residuals (fault=0, l1_eff∈[50,99])",
                         "clean_n_async": 3855, "clean_n_pmsm": 1100,
                         "notes": "排除了 fault=1 的 RMxprt 失败样本 (原 6390→3835+?"},
        **family,
        "generated_at": time.strftime("%Y-%m-%d"),
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(model_v3, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------- 4) 指标落盘（G7 纯 numpy 回放一致性由 verify_replay_real.py 全量执行）----------
    metrics_out = {
        "protocol": {"cv": "5-fold 段内 shuffle seed=42", "holdout": "80/20 分层 seed=20260930",
                     "n": n, "gbr_params": GBR_PARAMS},
        "cv": {"A_raw": mA_cv, "B_linear": mB_cv, "C_retrained": mC_cv,
               "per_segment": cv_metrics},
        "holdout_20pct": {"A_raw": mA_h, "v22_original_ref": mV_h,
                          "B_linear_same_protocol": mB_h, "C_retrained": mC_h},
        "json_replay_check": "executed in verify_replay_real.py (G7)",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    json.dump(metrics_out, open(OUT_METRICS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n留出集(20%): raw={mA_h['MAE_pp']:.3f}  v2.2.0原版(参考)={mV_h['MAE_pp']:.3f}  "
          f"B′={mB_h['MAE_pp']:.3f}  C={mC_h['MAE_pp']:.3f}pp ({100*(1-mC_h['MAE_pp']/mB_h['MAE_pp']):+.1f}% vs B′)")
    print(f"模型 -> {OUT_JSON} ({os.path.getsize(OUT_JSON)/1024:.0f} KB)  耗时 {time.time()-t0:.0f}s")


def np_predict_v22_hold(X, seg, raw):
    model = json.load(open(BASELINE, encoding="utf-8"))
    out = np.empty(len(X))
    for i in range(len(X)):
        kind, name = seg[i].split(":")
        fam = model[FAM_MAP[kind]]
        key = name if name in fam else next(k for k in fam if k.startswith(name + "("))
        p = fam[key]["primary"]
        mu = np.array(p["feature_mean"]); sd = np.array(p["feature_std"])
        sd[sd == 0] = 1.0
        out[i] = p["coef"][0] + float(np.dot(np.array(p["coef"][1:]), (X[i] - mu) / sd))
    return out


if __name__ == "__main__":
    main()
