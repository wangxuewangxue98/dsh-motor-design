"""
蒙特卡洛沙盘推演引擎 - 1000 次虚拟推演展示最惨失败情形
纯 numpy 实现, 不依赖外部库, 数据不出机

失败模式:
  - thermal_runaway: 温升超限 (max_temp > 绝缘等级限值)
  - magnetic_saturation: 齿磁密 > 1.9T 或气隙磁密 > 1.2T
  - copper_loss_excess: 铜耗占比过高 (> 总损耗 70%)
  - demagnetization: 高温下磁钢退磁风险 (电流过载 + 高温)
  - torque_ripple: 转矩波动过大
  - efficiency_collapse: 效率跌破目标值 5 个百分点以上
"""
import json
import math
import random
from datetime import datetime
from typing import Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# 失败模式定义
FAILURE_MODES = {
    "thermal_runaway": {
        "label": "热失控",
        "severity": "critical",
        "description": "温升超过绝缘等级限值, 绕组烧毁风险",
    },
    "magnetic_saturation": {
        "label": "磁饱和",
        "severity": "high",
        "description": "齿磁密 > 1.9T 或气隙磁密 > 1.2T, 铁耗激增",
    },
    "copper_loss_excess": {
        "label": "铜耗过高",
        "severity": "medium",
        "description": "铜耗占总损耗 > 70%, 效率受损",
    },
    "demagnetization": {
        "label": "退磁风险",
        "severity": "critical",
        "description": "高温 + 过载电流导致磁钢不可逆退磁",
    },
    "efficiency_collapse": {
        "label": "效率崩塌",
        "severity": "high",
        "description": "效率跌破目标值 5 个百分点以上",
    },
}


def _perturb(value, sigma_frac=0.05, lower=None, upper=None):
    """对数值施加高斯扰动, 截断到合理范围"""
    if value is None or not isinstance(value, (int, float)):
        return value
    if HAS_NUMPY:
        noise = float(np.random.normal(0, abs(value) * sigma_frac))
    else:
        noise = random.gauss(0, abs(value) * sigma_frac)
    v = value + noise
    if lower is not None:
        v = max(lower, v)
    if upper is not None:
        v = min(upper, v)
    return v


def _classify_failure(trial: dict, baseline: dict, specs: dict) -> list:
    """判定一次扰动试验的失败模式"""
    modes = []

    # 绝缘等级限值 (默认 F 级 155C, 温升 100K)
    insulation_class = specs.get("insulation_class", "F")
    temp_limit_k = {"A": 60, "E": 75, "B": 80, "F": 100, "H": 125}.get(insulation_class, 100)

    max_temp = trial.get("max_temp", 0)
    if max_temp and max_temp > temp_limit_k:
        modes.append("thermal_runaway")

    # 磁饱和
    tooth_b = trial.get("stator_tooth_flux_density")
    airgap_b = trial.get("air_gap_flux_density")
    if (tooth_b and tooth_b > 1.9) or (airgap_b and airgap_b > 1.2):
        modes.append("magnetic_saturation")

    # 铜耗占比
    copper = trial.get("copper_loss", 0)
    iron = trial.get("iron_loss", 0)
    total_loss = copper + iron
    if total_loss > 0 and copper / total_loss > 0.70:
        modes.append("copper_loss_excess")

    # 退磁风险: 高温 + 高电密
    current_density = trial.get("current_density", 0)
    if max_temp and max_temp > 120 and current_density and current_density > 10:
        modes.append("demagnetization")

    # 效率崩塌
    eff = trial.get("efficiency", 0)
    eff_target = specs.get("efficiency_target")
    baseline_eff = baseline.get("efficiency", 0)
    ref = eff_target if eff_target else baseline_eff
    if ref and eff < ref - 5:
        modes.append("efficiency_collapse")

    return modes


def run_simulation(solution: dict, specs: dict, n_trials: int = 1000,
                   sigma_frac: float = 0.05, seed: Optional[int] = None) -> dict:
    """
    对单个方案做蒙特卡洛扰动模拟

    Args:
        solution: 方案性能数据 (efficiency, max_temp, torque, ...)
        specs: 需求规格 (efficiency_target, insulation_class, ...)
        n_trials: 模拟次数 (默认 1000)
        sigma_frac: 扰动幅度 (默认 5%)
        seed: 随机种子 (可复现)

    Returns:
        {
            "trials": n_trials,
            "failure_rate": float,
            "failure_modes": {mode: count},
            "worst_case": {...},
            "percentiles": {"efficiency": {"p5":..,"p50":..,"p95":..}, ...},
            "survival_rate": float,
            "recommendation": str,
        }
    """
    if seed is not None:
        if HAS_NUMPY:
            np.random.seed(seed)
        else:
            random.seed(seed)

    perturb_fields = [
        ("efficiency", 0.03, 0, 100),
        ("max_temp", 0.08, 0, 200),
        ("torque", 0.06, 0, None),
        ("copper_loss", 0.10, 0, None),
        ("iron_loss", 0.10, 0, None),
        ("stator_tooth_flux_density", 0.05, 0, 2.5),
        ("air_gap_flux_density", 0.05, 0, 1.5),
        ("current_density", 0.08, 0, 20),
    ]

    eff_samples = []
    temp_samples = []
    torque_samples = []
    failure_count = 0
    mode_counts = {m: 0 for m in FAILURE_MODES}
    worst_case = None
    worst_eff = 999

    for _ in range(n_trials):
        trial = {}
        for field, frac, lo, hi in perturb_fields:
            base_val = solution.get(field)
            if base_val is not None:
                trial[field] = _perturb(base_val, frac, lo, hi)
            else:
                trial[field] = base_val

        modes = _classify_failure(trial, solution, specs)
        if modes:
            failure_count += 1
            for m in modes:
                mode_counts[m] = mode_counts.get(m, 0) + 1

        eff = trial.get("efficiency") or 0
        eff_samples.append(eff)
        temp_samples.append(trial.get("max_temp") or 0)
        torque_samples.append(trial.get("torque") or 0)

        if eff < worst_eff:
            worst_eff = eff
            worst_case = dict(trial)
            worst_case["failure_modes"] = modes

    def _pct(samples, p):
        if not samples:
            return None
        s = sorted(samples)
        idx = max(0, min(len(s) - 1, int(len(s) * p / 100)))
        return round(s[idx], 2)

    failure_rate = round(failure_count / max(n_trials, 1) * 100, 1)
    survival_rate = round(100 - failure_rate, 1)

    # 推荐
    if failure_rate < 5:
        verdict = "稳健"
        recommendation = f"1000 次推演失败率仅 {failure_rate}%, 方案鲁棒性强, 可放心推进。"
    elif failure_rate < 20:
        verdict = "可控"
        recommendation = f"失败率 {failure_rate}%, 主要风险: {max(mode_counts, key=mode_counts.get)}. 建议针对性优化后推进。"
    elif failure_rate < 50:
        verdict = "高风险"
        recommendation = f"失败率 {failure_rate}%, 鲁棒性不足。建议重新调整参数或增加安全裕度。"
    else:
        verdict = "极危险"
        recommendation = f"失败率高达 {failure_rate}%, 该方案在扰动下大概率失效。强烈建议放弃或大幅修改。"

    return {
        "trials": n_trials,
        "sigma_frac": sigma_frac,
        "failure_rate": failure_rate,
        "survival_rate": survival_rate,
        "verdict": verdict,
        "failure_modes": {m: c for m, c in mode_counts.items() if c > 0},
        "failure_mode_labels": {m: FAILURE_MODES[m]["label"] for m in mode_counts if c > 0} if False else {m: FAILURE_MODES[m]["label"] for m, c in mode_counts.items() if c > 0},
        "worst_case": worst_case,
        "percentiles": {
            "efficiency": {"p5": _pct(eff_samples, 5), "p50": _pct(eff_samples, 50), "p95": _pct(eff_samples, 95)},
            "max_temp": {"p5": _pct(temp_samples, 5), "p50": _pct(temp_samples, 50), "p95": _pct(temp_samples, 95)},
            "torque": {"p5": _pct(torque_samples, 5), "p50": _pct(torque_samples, 50), "p95": _pct(torque_samples, 95)},
        },
        "recommendation": recommendation,
        "timestamp": datetime.now().isoformat(),
    }


def run_batch(solutions: list, specs: dict, n_trials: int = 1000) -> list:
    """对多个方案批量推演, 返回排序后的结果 (失败率升序)"""
    results = []
    for i, sol in enumerate(solutions):
        sim = run_simulation(sol, specs, n_trials=n_trials, seed=42 + i)
        sim["solution_index"] = i
        results.append(sim)
    results.sort(key=lambda x: x["failure_rate"])
    return results
