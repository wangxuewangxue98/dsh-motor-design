# -*- coding: utf-8 -*-
"""
D²L 物理内核模块 — 分层类比选型法的物理引擎
================================================
核心原理: 电磁转矩 T = k · B_delta · A · D^2 · L
  在同一电机系列中, B_delta 和 A 近似为常数, 故 T proportional to D^2 * L

本模块提供:
  1. Y 系列型谱查表 (find_closest_catalog)
  2. D²L 类比推算 (analogy_estimate)
  3. 条件修正系数 (apply_condition_factors)
  4. 机座号跃迁判定 (shift_frame)
  5. 聚焦扫描矩阵生成 (focused_scan) — 替代暴力网格扫描
  6. 场景识别 (determine_scenario) — 型谱模式 vs 类比模式
"""
import json
import os
import math
import random
from typing import Optional

# ── 常量 ─────────────────────────────────────────────────────
Y_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "Knowledge", "y_series_physics.json")

# Y系列典型长径比范围 (按极数)
LAMBDA_RANGE = {
    2: (0.6, 1.3),    # 2极: 高速, 转子细长
    4: (0.7, 1.4),    # 4极: 中速
    6: (0.6, 1.2),    # 6极: 低速
    8: (0.5, 1.1),    # 8极: 低速, 大直径短铁心
}

# 机座号跃迁阈值 (基于转矩比)
FRAME_SHIFT_THRESHOLDS = {
    "micro": 0.05,     # <5%: 同机座号微调
    "fine":  0.20,     # 5-20%: 机座号 ±1~2
    "coarse": float('inf'),  # >20%: 机座号 +2 以上
}

# 气隙经验值 (mm)
AIR_GAP_MIN = 0.30
AIR_GAP_MAX = 1.50

# ── 缓存 ─────────────────────────────────────────────────────
_catalog_cache = None


def _load_catalog() -> list:
    """加载 Y 系列型谱数据库 (带缓存)"""
    global _catalog_cache
    if _catalog_cache is None:
        with open(Y_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _catalog_cache = data.get("catalog", [])
    return _catalog_cache


# ═══════════════════════════════════════════════════════════════
# 1. 基础物理计算
# ═══════════════════════════════════════════════════════════════

def compute_torque(power_kw: float, speed_rpm: float) -> float:
    """转矩: T = 9550 * P / n  (Nm)"""
    if speed_rpm <= 0:
        return 0.0
    return round(9550.0 * power_kw / speed_rpm, 2)


def compute_d2l(d_mm: float, l_mm: float) -> float:
    """D²L = (D/1000)^2 * (L/1000)  (m³)"""
    d_m = d_mm / 1000.0
    l_m = l_mm / 1000.0
    return round(d_m ** 2 * l_m, 6)


def compute_lambda(l_mm: float, d_mm: float) -> float:
    """长径比 lambda = L / D"""
    if d_mm <= 0:
        return 0.0
    return round(l_mm / d_mm, 3)


def validate_lambda(l_mm: float, d_mm: float, poles: int) -> dict:
    """
    验证长径比是否在合理范围内
    返回: {"valid": bool, "lambda": float, "range": tuple, "advice": str}
    """
    lam = compute_lambda(l_mm, d_mm)
    lo, hi = LAMBDA_RANGE.get(poles, (0.5, 1.5))
    valid = lo <= lam <= hi
    advice = ""
    if not valid:
        if lam < lo:
            advice = f"lambda={lam} 偏小, 铁心过短或内径过大, 建议增加铁心长度"
        else:
            advice = f"lambda={lam} 偏大, 铁心过长或内径过小, 建议减小铁心长度或增大内径"
    return {"valid": valid, "lambda": lam, "range": (lo, hi), "advice": advice}


# ═══════════════════════════════════════════════════════════════
# 2. 型谱查表
# ═══════════════════════════════════════════════════════════════

def find_closest_catalog(torque_nm: float, poles: int = None,
                         power_kw: float = None, speed_rpm: float = None) -> dict:
    """
    在 Y 系列型谱中查找与目标转矩最接近的型号

    优先按极数匹配, 然后按转矩差值排序
    返回: 型谱条目 dict (含 _torque_diff, _match_score 等附加字段)
    """
    catalog = _load_catalog()

    # 如果有功率和转速, 先算转矩
    if torque_nm is None and power_kw and speed_rpm:
        torque_nm = compute_torque(power_kw, speed_rpm)

    # 过滤极数
    if poles:
        candidates = [e for e in catalog if e["poles"] == poles]
    else:
        candidates = list(catalog)

    if not candidates:
        return {}

    # 按转矩差值排序
    for e in candidates:
        e["_torque_diff"] = round(abs(e["torque_nm"] - torque_nm), 2)
        e["_torque_ratio"] = round(torque_nm / e["torque_nm"], 4) if e["torque_nm"] else 0

    candidates.sort(key=lambda x: x["_torque_diff"])

    best = dict(candidates[0])  # 拷贝
    best["_target_torque"] = torque_nm
    return best


def find_catalog_by_frame(frame_size: int, poles: int = None) -> list:
    """按机座号查询型谱"""
    catalog = _load_catalog()
    results = [e for e in catalog if e["frame_size"] == frame_size]
    if poles:
        results = [e for e in results if e["poles"] == poles]
    return results


def find_catalog_range(torque_min: float, torque_max: float,
                       poles: int = None) -> list:
    """查询转矩落在 [min, max] 范围内的所有型号"""
    catalog = _load_catalog()
    results = [e for e in catalog
               if torque_min <= e["torque_nm"] <= torque_max]
    if poles:
        results = [e for e in results if e["poles"] == poles]
    results.sort(key=lambda x: x["torque_nm"])
    return results


# ═══════════════════════════════════════════════════════════════
# 3. D²L 类比推算
# ═══════════════════════════════════════════════════════════════

def analogy_estimate(torque_target: float, ref_entry: dict) -> dict:
    """
    基于 D²L 类比推算目标电机的尺寸

    公式: K_V = T_target / T_0
          D' = D_0 * K_V^(1/3)
          L' = L_0 * K_V^(1/3)

    参数:
      torque_target: 目标转矩 (Nm)
      ref_entry: 参考型谱条目 (含 stator_inner_dia_mm, core_length_mm, torque_nm)

    返回: {
        "D_estimated": float, "L_estimated": float,
        "K_V": float, "lambda": float,
        "D2L_estimated": float, "D2L_ref": float,
        "ref_model": str, "scenario": str
    }
    """
    t_0 = ref_entry["torque_nm"]
    d_0 = ref_entry["stator_inner_dia_mm"]
    l_0 = ref_entry["core_length_mm"]

    k_v = torque_target / t_0 if t_0 > 0 else 1.0
    k_cube = k_v ** (1.0 / 3.0)

    d_est = round(d_0 * k_cube, 1)
    l_est = round(l_0 * k_cube, 1)

    d2l_ref = compute_d2l(d_0, l_0)
    d2l_est = compute_d2l(d_est, l_est)
    lam = compute_lambda(l_est, d_est)

    # 判定机座号跃迁层级
    shift = shift_frame(k_v - 1.0 if k_v >= 1 else 1.0 - k_v)

    return {
        "D_estimated": d_est,
        "L_estimated": l_est,
        "K_V": round(k_v, 4),
        "k_cube": round(k_cube, 4),
        "lambda": lam,
        "D2L_estimated": d2l_est,
        "D2L_ref": d2l_ref,
        "ref_model": ref_entry.get("model", "?"),
        "ref_torque": t_0,
        "ref_D": d_0,
        "ref_L": l_0,
        "frame_shift": shift,
    }


def analogy_estimate_with_lambda(torque_target: float, ref_entry: dict,
                                 target_lambda: float = None) -> dict:
    """
    带长径比约束的类比推算

    如果参考型号的 lambda 不理想, 可指定 target_lambda:
      K_V = T_target / T_0 = D'^2 * L' / (D_0^2 * L_0)
      lambda' = L' / D' = target_lambda
      => D' = (D_0^2 * L_0 * K_V / target_lambda)^(1/3)
      => L' = D' * target_lambda
    """
    t_0 = ref_entry["torque_nm"]
    d_0 = ref_entry["stator_inner_dia_mm"]
    l_0 = ref_entry["core_length_mm"]
    d2l_0 = compute_d2l(d_0, l_0)

    k_v = torque_target / t_0 if t_0 > 0 else 1.0

    if target_lambda:
        # D' = (D2L_0 * K_V / lambda)^(1/3)
        d2l_target = d2l_0 * k_v
        d_est = round((d2l_target / target_lambda) ** (1.0 / 3.0) * 1000, 1)
        l_est = round(d_est * target_lambda, 1)
    else:
        # 无约束: 等比例缩放
        k_cube = k_v ** (1.0 / 3.0)
        d_est = round(d_0 * k_cube, 1)
        l_est = round(l_0 * k_cube, 1)

    lam = compute_lambda(l_est, d_est)
    d2l_est = compute_d2l(d_est, l_est)
    shift = shift_frame(abs(k_v - 1.0))

    return {
        "D_estimated": d_est,
        "L_estimated": l_est,
        "K_V": round(k_v, 4),
        "lambda": lam,
        "lambda_target": target_lambda,
        "D2L_estimated": d2l_est,
        "D2L_ref": d2l_0,
        "ref_model": ref_entry.get("model", "?"),
        "ref_torque": t_0,
        "ref_D": d_0,
        "ref_L": l_0,
        "frame_shift": shift,
    }


# ═══════════════════════════════════════════════════════════════
# 4. 条件修正系数
# ═══════════════════════════════════════════════════════════════

def apply_condition_factors(specs: dict) -> dict:
    """
    根据工况条件判定是否可用 Y 系列类比, 并计算修正系数

    三层判定:
      - 直接影响层: 水冷/400Hz/高温(>70°C)/高海拔(>4000m) → 不能用Y系列类比
      - 间接修正层: S3工作制/IP65/过载/海拔2000m/60Hz → 系数修正
      - 微调层: <5% → 忽略

    返回: {
        "usable": bool,          # 是否可用Y系列类比
        "block_reasons": list,   # 不可用的原因
        "correction_factor": float,  # 总修正系数 (乘到转矩上)
        "factors": dict,         # 各项修正明细
        "scenario": str,         # "catalog" 或 "analogy"
    }
    """
    block_reasons = []
    factors = {}
    total_factor = 1.0

    cooling = (specs.get("cooling") or "").lower()
    ambient = specs.get("ambient_temp_c", 40)
    altitude = specs.get("altitude_m", 0)
    frequency = specs.get("frequency_hz", 50)
    duty = (specs.get("duty_cycle") or "").upper()
    protection = (specs.get("protection_class") or "").upper()
    overload = specs.get("overload_pct", 0)

    # ── 直接影响层: 不能用 Y 系列类比 ──
    if "水冷" in cooling or "water" in cooling or "油冷" in cooling or "oil" in cooling:
        if cooling and cooling != "ic411 (自扇冷却)":
            block_reasons.append(f"冷却方式({cooling})非自扇冷却, Y系列类比不适用")
            factors["cooling"] = "blocked"

    if frequency and frequency >= 400:
        block_reasons.append(f"频率({frequency}Hz)过高, Y系列(50Hz)类比不适用")
        factors["frequency"] = "blocked"

    if ambient and ambient > 70:
        block_reasons.append(f"环境温度({ambient}°C)过高, 超出Y系列绝缘等级上限")
        factors["ambient"] = "blocked"

    if altitude and altitude > 4000:
        block_reasons.append(f"海拔({altitude}m)过高, 超出Y系列适用范围")
        factors["altitude"] = "blocked"

    if block_reasons:
        return {
            "usable": False,
            "block_reasons": block_reasons,
            "correction_factor": 1.0,
            "factors": factors,
            "scenario": "analogy",  # 不能用型谱, 但仍可类比推算(需自定义基准)
        }

    # ── 间接修正层: 系数修正 ──
    # S3 工作制: 转矩需折算到 S1 等效
    if duty and duty.startswith("S3"):
        ed = specs.get("ed_pct", 40)
        if ed and ed < 100:
            # S3 等效转矩 = T * sqrt(ED/100)
            f_duty = math.sqrt(ed / 100.0)
            factors["duty_s3"] = round(f_duty, 3)
            total_factor *= f_duty

    # IP65 及以上: 散热变差, 温升修正
    if "IP65" in protection or "IP66" in protection or "IP67" in protection:
        f_prot = 0.95  # 5% 降额
        factors["protection"] = f_prot
        total_factor *= f_prot

    # 过载要求: 需要额外转矩余量
    if overload and overload > 0:
        f_overload = 1.0 + overload / 100.0
        factors["overload"] = round(f_overload, 3)
        total_factor *= f_overload

    # 海拔修正 (1000-4000m)
    if altitude and altitude > 1000:
        # 每 1000m 降额 ~3%
        f_alt = 1.0 - (altitude - 1000) / 1000.0 * 0.03
        f_alt = max(0.88, f_alt)  # 最低 0.88
        factors["altitude"] = round(f_alt, 3)
        total_factor *= f_alt

    # 频率修正 (非 50Hz)
    if frequency and frequency != 50 and frequency < 400:
        # 60Hz: 转速 ×1.2, 转矩近似不变 (同功率下)
        # 但恒转矩负载在 60Hz 下功率上升, 需修正
        f_freq = 50.0 / frequency if frequency > 50 else 1.0
        factors["frequency"] = round(f_freq, 3)
        total_factor *= f_freq

    total_factor = round(total_factor, 4)

    # ── 微调层: <5% 忽略 ──
    if abs(total_factor - 1.0) < 0.05:
        total_factor = 1.0
        factors["_note"] = "修正量<5%, 已忽略"

    return {
        "usable": True,
        "block_reasons": [],
        "correction_factor": total_factor,
        "factors": factors,
        "scenario": "catalog" if abs(total_factor - 1.0) < 0.05 else "analogy",
    }


# ═══════════════════════════════════════════════════════════════
# 5. 机座号跃迁判定
# ═══════════════════════════════════════════════════════════════

def shift_frame(torque_ratio_deviation: float) -> dict:
    """
    根据转矩偏差判定机座号跃迁层级

    参数:
      torque_ratio_deviation: |K_V - 1| 的值, 即转矩偏差比例

    返回: {
        "level": "micro" | "fine" | "coarse",
        "frame_delta": int,      # 建议机座号变化量
        "description": str,
    }
    """
    if torque_ratio_deviation < FRAME_SHIFT_THRESHOLDS["micro"]:
        return {
            "level": "micro",
            "frame_delta": 0,
            "description": f"转矩偏差 {torque_ratio_deviation*100:.1f}% < 5%, "
                           f"同机座号微调 (调整铁心长度/匝数)",
        }
    elif torque_ratio_deviation < FRAME_SHIFT_THRESHOLDS["fine"]:
        # 5-20%: 机座号 ±1~2
        delta = 1 if torque_ratio_deviation < 0.10 else 2
        return {
            "level": "fine",
            "frame_delta": delta,
            "description": f"转矩偏差 {torque_ratio_deviation*100:.1f}% "
                           f"(5%~20%), 机座号 {'+' if delta > 0 else ''}{delta}",
        }
    else:
        # >20%: 机座号 +2 以上
        delta = 2 if torque_ratio_deviation < 0.35 else 3
        return {
            "level": "coarse",
            "frame_delta": delta,
            "description": f"转矩偏差 {torque_ratio_deviation*100:.1f}% "
                           f"> 20%, 机座号 +{delta} (跨系列选型)",
        }


# ═══════════════════════════════════════════════════════════════
# 6. 场景识别
# ═══════════════════════════════════════════════════════════════

def determine_scenario(specs: dict) -> dict:
    """
    场景识别: 判定使用型谱模式还是类比模式

    型谱模式: 标准工况, Y 系列可直接查表
    类比模式: 非标工况, 需要 D²L 推算

    返回: {
        "scenario": "catalog" | "analogy",
        "condition": dict,       # apply_condition_factors 的结果
        "torque_nm": float,      # 目标转矩 (含修正)
        "poles": int,            # 推荐极数
        "reason": str,
    }
    """
    power_kw = specs.get("power_kw") or 10
    speed_rpm = specs.get("speed_rpm") or 1500

    # 极数推算: n = 60*f / p, 同步转速略高于额定
    sync_speed = speed_rpm * 1.03 if speed_rpm < 3000 else speed_rpm * 1.02
    if sync_speed > 2500:
        poles = 2
    elif sync_speed > 1200:
        poles = 4
    elif sync_speed > 800:
        poles = 6
    else:
        poles = 8

    torque = compute_torque(power_kw, speed_rpm)

    condition = apply_condition_factors(specs)
    corrected_torque = round(torque * condition["correction_factor"], 2)

    if not condition["usable"]:
        scenario = "analogy"
        reason = "; ".join(condition["block_reasons"])
    elif condition["scenario"] == "catalog":
        scenario = "catalog"
        reason = "标准工况, 可直接查 Y 系列型谱"
    else:
        scenario = "analogy"
        reason = f"需修正系数 {condition['correction_factor']}, 采用类比推算"

    return {
        "scenario": scenario,
        "condition": condition,
        "torque_nm": torque,
        "corrected_torque_nm": corrected_torque,
        "poles": poles,
        "reason": reason,
    }


# ═══════════════════════════════════════════════════════════════
# 7. 聚焦扫描矩阵 (替代暴力网格扫描)
# ═══════════════════════════════════════════════════════════════

def focused_scan(specs: dict, count: int = 10,
                 scenario_info: dict = None) -> list:
    """
    基于物理内核的聚焦扫描 — 替代暴力网格扫描

    工作流:
      1. 场景识别 → 确定 catalog 或 analogy 模式
      2. catalog 模式: 以最接近型谱型号为中心, 在 ±10% 范围内微调
      3. analogy 模式: 以 D²L 推算尺寸为中心, 在 ±15% 范围内扫描
      4. 每组方案附带物理核验信息

    参数:
      specs: 需求规格 dict
      count: 目标方案数量 (5-20)
      scenario_info: 预计算的场景信息 (可选, 避免重复计算)

    返回: 参数字典列表 (与 generate_param_matrix 兼容, 含物理内核附加字段)
    """
    voltage = specs.get("voltage_v") or 48
    speed = specs.get("speed_rpm") or 1500
    od_limit = specs.get("stator_od_limit") or 450

    # 1. 场景识别
    if scenario_info is None:
        scenario_info = determine_scenario(specs)

    scenario = scenario_info["scenario"]
    poles = scenario_info["poles"]
    target_torque = scenario_info["corrected_torque_nm"]

    # 2. 获取设计基准 (型谱查表 or 类比推算)
    ref_entry = find_closest_catalog(target_torque, poles=poles)

    if scenario == "catalog" and ref_entry:
        # ── 型谱模式: 以型谱型号为中心微调 ──
        base_d = ref_entry["stator_inner_dia_mm"]
        base_l = ref_entry["core_length_mm"]
        base_od = ref_entry["stator_od_mm"]
        ref_model = ref_entry["model"]
        scan_range = 0.10  # ±10%
    else:
        # ── 类比模式: D²L 推算 ──
        if ref_entry:
            est = analogy_estimate(target_torque, ref_entry)
            base_d = est["D_estimated"]
            base_l = est["L_estimated"]
            base_od = base_d / 0.60  # 估算外径
            ref_model = f"类比自{est['ref_model']}"
        else:
            # 无型谱数据: 回退到经验公式
            base_d = 100 + target_torque ** 0.3 * 30
            base_l = base_d * 0.9
            base_od = base_d / 0.60
            ref_model = "经验估算"
        scan_range = 0.15  # ±15%

    # 3. 聚焦扫描 — 在基准尺寸附近生成变体
    designs = []
    n_variants = max(count, 5)

    # 扫描维度: D ±scan_range, L ±scan_range, 极数微调
    d_steps = _linspace(base_d * (1 - scan_range), base_d * (1 + scan_range),
                        max(3, int(n_variants ** 0.5)))
    l_steps = _linspace(base_l * (1 - scan_range), base_l * (1 + scan_range),
                        max(3, int(n_variants ** 0.5)))

    # 极数候选: 主极数 ±1档
    pole_candidates = [poles]
    if poles > 2:
        pole_candidates.append(poles - 2)
    if poles < 8:
        pole_candidates.append(poles + 2)

    # 槽配合
    slot_map = {
        2: [(18, 16), (24, 20), (30, 26)],
        4: [(24, 22), (36, 28), (48, 44)],
        6: [(36, 28), (54, 44), (72, 58)],
        8: [(48, 44), (54, 44), (72, 58)],
        10: [(60, 50), (72, 58), (90, 72)],
    }

    seen_keys = set()
    for d in d_steps:
        for l in l_steps:
            for pole in pole_candidates:
                # 外径估算: D / id_ratio
                id_ratio = 0.55 + (pole - 2) * 0.03
                od = round(d / id_ratio, 0)

                # 限制外径
                if od > od_limit:
                    continue

                # 去重
                key = (round(d, 0), round(l, 0), pole)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                # 气隙
                air_gap = round(max(AIR_GAP_MIN, min(AIR_GAP_MAX,
                    0.30 + (od - 120) * 0.0015 + random.uniform(-0.05, 0.05))), 2)

                # 转子外径
                rotor_od = round(d - 2 * air_gap, 1)

                # 槽配合
                slots = slot_map.get(pole, [(36, 28)])
                slots_s, slots_r = random.choice(slots)

                # 齿宽
                tooth_width = round(3.14159 * d / (2 * slots_s), 2)

                # 轭厚
                slot_depth = round((od - d) / 2 * 0.60, 1)
                yoke_thickness = round((od - d) / 2 - slot_depth, 1)

                # 轴径
                shaft_dia = round(rotor_od * 0.35, 1)

                # 匝数估算
                base_turns = max(6, int(voltage * 0.3 + (450 - od) * 0.02))
                turns = base_turns + random.choice([-2, 0, 2])

                # D²L 物理核验
                d2l = compute_d2l(d, l)
                lam = compute_lambda(l, d)
                lam_check = validate_lambda(l, d, pole)
                est_torque = round(target_torque * d2l / compute_d2l(base_d, base_l), 2) \
                    if compute_d2l(base_d, base_l) > 0 else target_torque

                designs.append({
                    "stator_od": int(od),
                    "stator_id": round(d, 1),
                    "rotor_od": rotor_od,
                    "core_length": round(l, 1),
                    "air_gap": air_gap,
                    "tooth_width": tooth_width,
                    "yoke_thickness": yoke_thickness,
                    "shaft_dia": shaft_dia,
                    "poles": pole,
                    "voltage": voltage,
                    "peak_current": random.choice([50, 80, 100]),
                    "speed": speed,
                    "slots_stator": slots_s,
                    "slots_rotor": slots_r,
                    "turns_per_coil": turns,
                    "parallel_circuits": random.choice([1, 2]),
                    # ── 物理内核附加字段 ──
                    "_physics": {
                        "d_squared_l": d2l,
                        "lambda": lam,
                        "lambda_valid": lam_check["valid"],
                        "lambda_advice": lam_check["advice"],
                        "estimated_torque": est_torque,
                        "ref_model": ref_model,
                        "scenario": scenario,
                        "target_torque": target_torque,
                    },
                })

                if len(designs) >= count:
                    break
            if len(designs) >= count:
                break
        if len(designs) >= count:
            break

    # 如果不足 count, 补充随机变体
    while len(designs) < count:
        d = round(base_d * (1 + random.uniform(-scan_range, scan_range)), 1)
        l = round(base_l * (1 + random.uniform(-scan_range, scan_range)), 1)
        pole = poles
        od = round(d / (0.55 + (pole - 2) * 0.03), 0)
        if od > od_limit:
            od = od_limit
        air_gap = round(max(AIR_GAP_MIN, min(AIR_GAP_MAX,
            0.30 + (od - 120) * 0.0015)), 2)
        rotor_od = round(d - 2 * air_gap, 1)
        slots_s, slots_r = slot_map.get(pole, [(36, 28)])[0]
        d2l = compute_d2l(d, l)
        lam = compute_lambda(l, d)
        designs.append({
            "stator_od": int(od), "stator_id": d, "rotor_od": rotor_od,
            "core_length": l, "air_gap": air_gap,
            "tooth_width": round(3.14159 * d / (2 * slots_s), 2),
            "yoke_thickness": round((od - d) / 2 * 0.40, 1),
            "shaft_dia": round(rotor_od * 0.35, 1),
            "poles": pole, "voltage": voltage,
            "peak_current": random.choice([50, 80, 100]),
            "speed": speed, "slots_stator": slots_s, "slots_rotor": slots_r,
            "turns_per_coil": max(6, int(voltage * 0.3 + (450 - od) * 0.02)),
            "parallel_circuits": 1,
            "_physics": {
                "d_squared_l": d2l, "lambda": lam,
                "lambda_valid": True, "lambda_advice": "",
                "estimated_torque": target_torque,
                "ref_model": ref_model, "scenario": scenario,
                "target_torque": target_torque,
            },
        })

    # 按 D²L 与目标转矩的匹配度排序
    if designs:
        target_d2l = compute_d2l(base_d, base_l)
        if target_d2l > 0:
            designs.sort(key=lambda x: abs(
                x["_physics"]["d_squared_l"] - target_d2l))

    return designs[:count]


# ═══════════════════════════════════════════════════════════════
# 8. 选型路径叙述 (供评审推荐师生成物理逻辑解释)
# ═══════════════════════════════════════════════════════════════

def build_selection_narrative(specs: dict, scenario_info: dict,
                              best_design: dict) -> str:
    """
    生成选型路径叙述文本 (供评审推荐师使用)

    输出: "客户需求 → 转矩计算 → 场景识别 → 型谱查表/类比推算 → 聚焦扫描 → 最优方案"
    """
    parts = []
    power = specs.get("power_kw", "?")
    speed = specs.get("speed_rpm", "?")
    torque = scenario_info.get("torque_nm", "?")
    corrected = scenario_info.get("corrected_torque_nm", "?")
    poles = scenario_info.get("poles", "?")
    scenario = scenario_info.get("scenario", "?")
    condition = scenario_info.get("condition", {})

    parts.append("## 选型路径叙述\n")
    parts.append(f"**1. 需求转转矩**\n")
    parts.append(f"  P={power}kW, n={speed}rpm → T=9550×{power}/{speed}={torque}Nm\n")

    parts.append(f"\n**2. 场景识别**\n")
    parts.append(f"  推荐极数: {poles}极 (同步转速{int(120*50/poles)}rpm)\n")
    parts.append(f"  选型模式: {'型谱查表' if scenario == 'catalog' else '类比推算'}\n")
    if condition.get("correction_factor", 1.0) != 1.0:
        parts.append(f"  修正系数: {condition['correction_factor']} "
                     f"(修正后转矩={corrected}Nm)\n")
        for k, v in condition.get("factors", {}).items():
            parts.append(f"    - {k}: {v}\n")
    if condition.get("block_reasons"):
        parts.append(f"  限制因素: {'; '.join(condition['block_reasons'])}\n")

    # 型谱查表
    ref = find_closest_catalog(corrected, poles=poles)
    if ref:
        parts.append(f"\n**3. Y系列基准型号**\n")
        parts.append(f"  最接近型号: {ref.get('model', '?')}\n")
        parts.append(f"  基准转矩: {ref.get('torque_nm', '?')}Nm\n")
        parts.append(f"  基准尺寸: D={ref.get('stator_inner_dia_mm', '?')}mm, "
                     f"L={ref.get('core_length_mm', '?')}mm\n")
        parts.append(f"  基准 D²L: {ref.get('d_squared_l_m3', '?')}m³\n")

        if scenario == "analogy":
            est = analogy_estimate(corrected, ref)
            parts.append(f"\n**4. D²L类比推算**\n")
            parts.append(f"  K_V = {corrected}/{ref['torque_nm']} = {est['K_V']}\n")
            parts.append(f"  D' = {ref['stator_inner_dia_mm']} × {est['K_V']}^(1/3) "
                         f"= {est['D_estimated']}mm\n")
            parts.append(f"  L' = {ref['core_length_mm']} × {est['K_V']}^(1/3) "
                         f"= {est['L_estimated']}mm\n")
            parts.append(f"  机座号跃迁: {est['frame_shift']['description']}\n")

    # 最优方案
    if best_design:
        phys = best_design.get("_physics", {})
        parts.append(f"\n**5. 最优方案核验**\n")
        parts.append(f"  定子内径 D={best_design.get('stator_id', '?')}mm\n")
        parts.append(f"  铁心长度 L={best_design.get('core_length', '?')}mm\n")
        parts.append(f"  D²L={phys.get('d_squared_l', '?')}m³\n")
        parts.append(f"  长径比 λ={phys.get('lambda', '?')} "
                     f"({'合理' if phys.get('lambda_valid') else '偏'+('小' if phys.get('lambda', 1) < 1 else '大')})\n")
        parts.append(f"  估算转矩: {phys.get('estimated_torque', '?')}Nm\n")
        parts.append(f"  参考型号: {phys.get('ref_model', '?')}\n")

    return "".join(parts)


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _linspace(start: float, stop: float, num: int) -> list:
    """生成等间距数列"""
    if num <= 1:
        return [start]
    step = (stop - start) / (num - 1)
    return [round(start + i * step, 1) for i in range(num)]


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("D²L 物理内核自测")
    print("=" * 60)

    # 1. 基础计算
    print("\n--- 基础计算 ---")
    print(f"  T(15kW, 1460rpm) = {compute_torque(15, 1460)} Nm")
    print(f"  D²L(170mm, 155mm) = {compute_d2l(170, 155)} m³")
    print(f"  lambda(155, 170) = {compute_lambda(155, 170)}")

    # 2. 型谱查表
    print("\n--- 型谱查表 ---")
    target_t = compute_torque(15, 1460)
    ref = find_closest_catalog(target_t, poles=4)
    print(f"  目标转矩 {target_t}Nm → 最近型号: {ref.get('model', '?')} "
          f"(T={ref.get('torque_nm', '?')}Nm, 差={ref.get('_torque_diff', '?')}Nm)")

    # 3. 类比推算
    print("\n--- D²L类比推算 ---")
    est = analogy_estimate(target_t, ref)
    print(f"  K_V = {est['K_V']}")
    print(f"  D' = {est['D_estimated']}mm (基准 {est['ref_D']}mm)")
    print(f"  L' = {est['L_estimated']}mm (基准 {est['ref_L']}mm)")
    print(f"  机座跃迁: {est['frame_shift']['description']}")

    # 4. 条件修正
    print("\n--- 条件修正 ---")
    test_specs = {
        "power_kw": 15, "speed_rpm": 1460, "cooling": "自然风冷",
        "ambient_temp_c": 45, "altitude_m": 1500, "frequency_hz": 50,
    }
    cond = apply_condition_factors(test_specs)
    print(f"  可用: {cond['usable']}, 修正系数: {cond['correction_factor']}")
    print(f"  明细: {cond['factors']}")

    # 水冷测试
    test_specs2 = dict(test_specs, cooling="水冷")
    cond2 = apply_condition_factors(test_specs2)
    print(f"  水冷 → 可用: {cond2['usable']}, 原因: {cond2['block_reasons']}")

    # 5. 场景识别
    print("\n--- 场景识别 ---")
    scenario = determine_scenario(test_specs)
    print(f"  模式: {scenario['scenario']}, 极数: {scenario['poles']}")
    print(f"  转矩: {scenario['torque_nm']}Nm → 修正后: {scenario['corrected_torque_nm']}Nm")
    print(f"  原因: {scenario['reason']}")

    # 6. 聚焦扫描
    print("\n--- 聚焦扫描 (生成5组) ---")
    designs = focused_scan(test_specs, count=5, scenario_info=scenario)
    for i, d in enumerate(designs):
        p = d.get("_physics", {})
        print(f"  [{i+1}] OD={d['stator_od']} ID={d['stator_id']} "
              f"L={d['core_length']} P={d['poles']} "
              f"D²L={p.get('d_squared_l', '?')} λ={p.get('lambda', '?')} "
              f"参考={p.get('ref_model', '?')[:15]}")

    # 7. 选型叙述
    print("\n--- 选型路径叙述 ---")
    if designs:
        narrative = build_selection_narrative(test_specs, scenario, designs[0])
        print(narrative)

    print("\n" + "=" * 60)
    print("自测完成")
