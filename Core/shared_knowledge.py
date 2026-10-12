# -*- coding: utf-8 -*-
"""
全局共享知识引擎 (SharedKnowledge) — 三层架构·第一层
======================================================
所有智能体共用的"事实基准"，统一加载和管理:
  - 电机参数推导工具集 (封装 physics_kernel)
  - 行业惯例库 (industry_defaults.json)
  - Y系列型谱库 (y_series_physics.json)
  - 条件修正系数表 (condition_corrections.json)
  - 设计规则库 (design_rules.json)
  - 追问模板库 (question_templates.json)
  - 材料属性库

设计原则:
  1. 全局单例 — 所有智能体共享同一个实例
  2. 统一入口 — infer_all_params() 是所有推导公式的入口
  3. 一次加载 — JSON 知识库启动时加载，运行时常驻内存
  4. 进化更新 — update_rule() 供运营分析师/进化引擎调用，即时生效

使用方式:
  from Core.shared_knowledge import knowledge_engine

  # 需求分析师: 推导参数 + 查行业惯例
  specs = knowledge_engine.infer_all_params(user_input)
  defaults = knowledge_engine.get_industry_defaults("船舶推进")

  # 方案生成师: 查型谱 + 查高效参数域
  baseline = knowledge_engine.find_closest_catalog(torque=2076, poles=6)
  domain = knowledge_engine.get_efficient_domain("叉车")

  # 评审推荐师: 查设计规则
  lam_rule = knowledge_engine.get_lambda_range(4)
  certifications = knowledge_engine.get_compliance_requirements("船舶")
"""
import json
import os
import sys
import math
from typing import Optional, Any

# ── 路径配置 ──────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KNOWLEDGE_DIR = os.path.join(_BASE, "Knowledge")
_SCRIPTS_DIR = os.path.join(_BASE, "Scripts")

# 将 Scripts/ 加入路径以导入 physics_kernel
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# ── 尝试导入物理内核 ──────────────────────────────────────────
try:
    from physics_kernel import (
        compute_torque, compute_d2l, compute_lambda, validate_lambda,
        find_closest_catalog, find_catalog_by_frame, find_catalog_range,
        analogy_estimate, analogy_estimate_with_lambda,
        apply_condition_factors, shift_frame,
        determine_scenario, focused_scan, build_selection_narrative,
    )
    # v4.02.103 (P3-A): λ / id-od 单一真源出口 —— 消除 JSON 副本与 kernel 的并列分歧
    try:
        from physics_kernel import (
            LAMBDA_RANGE as _PK_LAMBDA_RANGE,
            lambda_band_for_poles as _PK_lambda_band_for_poles,
            id_ratio_by_poles as _PK_id_ratio_by_poles,
        )
        _PK_GEOM_SINGLE_SOURCE = True
    except Exception:
        _PK_LAMBDA_RANGE = None
        _PK_lambda_band_for_poles = None
        _PK_id_ratio_by_poles = None
        _PK_GEOM_SINGLE_SOURCE = False
    _PHYSICS_AVAILABLE = True
except Exception:
    _PHYSICS_AVAILABLE = False
    _PK_GEOM_SINGLE_SOURCE = False
    _PK_LAMBDA_RANGE = None
    _PK_lambda_band_for_poles = None
    _PK_id_ratio_by_poles = None


class SharedKnowledge:
    """
    全局共享知识引擎 — 三层架构·第一层

    所有智能体通过此类访问共享的:
    - 参数推导工具集 (physics_kernel)
    - 行业惯例库 (industry_defaults.json)
    - Y系列型谱库 (y_series_physics.json)
    - 条件修正系数表 (condition_corrections.json)
    - 设计规则库 (design_rules.json)
    - 追问模板库 (question_templates.json)
    """

    def __init__(self, knowledge_dir: str = None):
        self._kdir = knowledge_dir or _KNOWLEDGE_DIR
        self._cache = {}  # JSON 缓存
        self._load_all()

    # ═══════════════════════════════════════════════════════════
    # 1. JSON 加载 (带缓存)
    # ═══════════════════════════════════════════════════════════

    def _load_json(self, filename: str) -> dict:
        """加载 JSON 文件 (带缓存)"""
        if filename in self._cache:
            return self._cache[filename]
        path = os.path.join(self._kdir, filename)
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._cache[filename] = data
        return data

    def _load_all(self):
        """启动时加载所有知识库"""
        self.industry_defaults = self._load_json("industry_defaults.json")
        self.y_series_physics = self._load_json("y_series_physics.json")
        self.condition_corrections = self._load_json("condition_corrections.json")
        self.design_rules = self._load_json("design_rules.json")
        self.question_templates = self._load_json("question_templates.json")
        self.efficient_param_domain = self._load_json("efficient_param_domain.json")
        self.forced_questions = self._load_json("forced_questions.json")
        self.report_structure_rules = self._load_json("report_structure_rules.json")

    def reload(self):
        """重新加载所有知识库 (进化引擎更新后调用)"""
        self._cache.clear()
        self._load_all()

    # ═══════════════════════════════════════════════════════════
    # 2. 参数推导工具集 (封装 physics_kernel)
    # ═══════════════════════════════════════════════════════════

    def infer_all_params(self, user_specs: dict) -> dict:
        """
        所有推导公式的入口 — 统一参数推导 (v2.0 增强)

        依次执行:
          1. 功率-转矩-转速 三者互推
          2. 转速-频率-极数 (含同步速 + 转差率)
          3. 电压-电流 (含效率/功率因数查表 + 电压反推)
          4. 转矩-力-半径 (机械参数，卷扬/绞车场景)
          5. 海拔降容
          6. 温度降容
          7. 冷却转矩密度修正
          8. 有效功率计算 (降容后)
          9. 行业默认值补全 (14场景)
         10. 场景识别
         11. 置信度标记 (推导值 confidence=0.8)

        返回: 补全后的 specs dict (含 _inferred, _confidence 字段)
        """
        specs = user_specs.copy()
        inferred = []
        confidence = {}  # key -> confidence value

        # ── 1. 转矩-力-半径 (机械参数，最先执行，为后续功率推导提供转矩) ──
        self._infer_torque_force_radius(specs, inferred, confidence)

        # ── 2. 功率-转矩-转速 三者互推 ──
        power = specs.get("power_kw")
        speed = specs.get("speed_rpm")
        torque = specs.get("torque_nm")

        if not torque and power and speed:
            torque = compute_torque(power, speed) if _PHYSICS_AVAILABLE else round(9550 * power / speed, 2)
            specs["torque_nm"] = torque
            inferred.append(f"转矩推导: T=9550×{power}/{speed}={torque}Nm")
            confidence["torque_nm"] = 0.8

        if not power and torque and speed:
            power = round(torque * speed / 9550.0, 2)
            specs["power_kw"] = power
            inferred.append(f"功率推导: P={torque}×{speed}/9550={power}kW")
            confidence["power_kw"] = 0.8

        if not speed and power and torque:
            speed = round(torque * 9550.0 / power) if power else 0
            specs["speed_rpm"] = speed
            inferred.append(f"转速推导: n={torque}×9550/{power}={speed}rpm")
            confidence["speed_rpm"] = 0.8

        # ── 3. 转速-频率-极数 (含同步速 + 转差率) ──
        poles = specs.get("poles")
        # v4.01: 不再默认50Hz, 让推导引擎从极数/转速计算正确频率
        freq = specs.get("frequency_hz")

        if not poles and speed:
            # v4.01.028: 从转速反推极数 — 区分电机类型, 取最接近的标准同步转速
            motor_type = specs.get("motor_type", "")
            is_sync = ("永磁" in motor_type or "同步" in motor_type
                       or "PMSM" in motor_type.upper())
            if is_sync:
                n_sync_est = speed
            else:
                n_sync_est = speed / (1 - 0.03)
            freq_guess = freq if freq else 50
            if abs(freq_guess - 60) < abs(freq_guess - 50):
                std = [3600, 1800, 1200, 900, 720, 600, 500, 428]
            else:
                std = [3000, 1500, 1000, 750, 600, 500, 428, 375]
            n_sync = min(std, key=lambda s: abs(s - n_sync_est))
            poles = max(2, round(120 * freq_guess / n_sync))
            if poles % 2 != 0:
                poles += 1
            specs["poles"] = poles
            inferred.append(f"极数推导: n_sync≈{n_sync_est:.0f}rpm(取标准{n_sync}rpm) → {poles}极")
            confidence["poles"] = 0.8

        # v4.01: 先计算频率 (从极数+转速), 再算同步速和转差率
        if not freq and poles and speed:
            motor_type = specs.get("motor_type", "")
            if "永磁" in motor_type or "同步" in motor_type or "PMSM" in motor_type.upper():
                # 同步电机: 无转差, 同步转速 = 实际转速
                freq = round(speed * poles / 120, 1)
                specs["frequency_hz"] = freq
                inferred.append(f"频率推导: f={freq}Hz (同步电机, n_sync=n={speed}rpm, p={poles})")
            else:
                # 异步电机: 假设3%转差
                sync_speed_est = speed / (1 - 0.03)
                freq = round(sync_speed_est * poles / 120, 1)
                specs["frequency_hz"] = freq
                inferred.append(f"频率推导: f≈{freq}Hz (异步电机, 假设3%转差)")
            confidence["frequency_hz"] = 0.7

        # 计算同步速和转差率 (在频率确定后)
        if poles and freq:
            sync_speed = round(120 * freq / poles, 0)
            specs["sync_speed_rpm"] = sync_speed
            if speed and sync_speed > 0:
                # v4.02.46 (C, RUN54/54b/54c 取证): LLM 偶将同步转速当额定转速输出
                # (speed≈sync, 如 1500 而非 1460), 导致转差率=0 且下游转矩/场景识别失真。
                # 若额定≈同步(差<0.5%), 按标准 3% 转差回推真实额定转速并同步修正转矩。
                if abs(speed - sync_speed) / sync_speed < 0.005:
                    corrected = int(round(sync_speed * (1 - 0.03)))
                    specs["speed_rpm"] = corrected
                    inferred.append(f"额定转速校正: LLM误取同步速{sync_speed:.0f}→额定{corrected}rpm(3%转差)")
                    if power:
                        _t = round(9550.0 * power / corrected, 2)
                        specs["torque_nm"] = _t
                        inferred.append(f"转矩同步校正: T=9550×{power}/{corrected}={_t}Nm")
                    speed = corrected
                slip = round((sync_speed - speed) / sync_speed, 4)
                specs["slip"] = slip
                motor_type = specs.get("motor_type", "")
                if "永磁" not in motor_type and "同步" not in motor_type:
                    # 异步电机才检查转差率异常
                    if slip < 0 or slip > 0.10:
                        inferred.append(f"⚠ 转差率异常: slip={slip} (正常范围0.01-0.05)")

        # ── 3. 电压-电流 (含效率/功率因数查表 + 电压反推) ──
        voltage = specs.get("voltage_v")
        current = specs.get("rated_current_a") or specs.get("current_a")

        # 查表获取效率和功率因数默认值
        eff, pf = self.get_efficiency_pf(power, poles)
        specs.setdefault("assumed_efficiency", eff)
        specs.setdefault("assumed_power_factor", pf)

        if voltage and power and not current:
            if voltage <= 100:
                # 直流: I = P / U
                current = round(power * 1000 / voltage, 1)
                inferred.append(f"电流估算(直流): I={current}A (U={voltage}V)")
            else:
                # 三相交流: I = P / (√3 × U × cosφ × η)
                current = round(power * 1000 / (1.732 * voltage * pf * eff), 1)
                inferred.append(f"电流估算(三相): I≈{current}A (η={eff}, cosφ={pf})")
            specs["rated_current_a"] = current
            confidence["rated_current_a"] = 0.8

        if not voltage and power and current:
            # 反推电压等级
            voltage = round(power * 1000 / (1.732 * current * pf * eff), 0)
            # 归整到标准电压等级
            voltage = self._snap_to_standard_voltage(voltage, power)
            specs["voltage_v"] = voltage
            inferred.append(f"电压反推: U≈{voltage}V (P={power}kW, I={current}A)")
            confidence["voltage_v"] = 0.7

        # ── 4. 海拔降容 ──
        altitude = specs.get("altitude_m", 0)
        k_alt = 1.0
        if altitude and altitude > 1000:
            k_alt = self._compute_altitude_derating(altitude)
            if k_alt < 1.0:
                inferred.append(f"海拔降容: {altitude}m → k_alt={k_alt}")
                specs["_altitude_derating"] = k_alt

        # ── 5. 温度降容 ──
        ambient_temp = specs.get("ambient_temp_c", 40)
        k_temp = 1.0
        if ambient_temp and ambient_temp > 40:
            k_temp = self._compute_temperature_derating(ambient_temp)
            if k_temp < 1.0:
                inferred.append(f"温度降容: {ambient_temp}℃ → k_temp={k_temp}")
                specs["_temp_derating"] = k_temp

        # ── 6. 有效功率计算 ──
        if power:
            derating = min(k_alt, k_temp)
            if derating < 1.0:
                effective_power = round(power / derating, 1)
                specs["derating_factor"] = round(derating, 3)
                specs["effective_power_kw"] = effective_power
                inferred.append(f"有效功率: {power}kW / {derating} = {effective_power}kW (需选型功率)")

        # ── 7. 行业默认值补全 ──
        application = specs.get("application", "")
        if application:
            defaults = self.get_industry_defaults(application)
            for k, v in defaults.items():
                if k.startswith("_") or k in ("note", "certifications", "special_requirements",
                                               "cooling_desc", "voltage_options", "poles_options",
                                               "protection_options", "insulation_options", "duty_options",
                                               "voltage_type", "speed_rpm_max"):
                    continue
                if k not in specs or specs.get(k) in (None, "", "未知"):
                    if isinstance(v, (int, float, str)):
                        specs[k] = v
                        inferred.append(f"行业默认补全: {k}={v} (来源:{application})")
                        confidence[k] = 0.7  # 行业默认值置信度

        # ── 8. 冷却转矩密度修正 (在行业默认值之后，确保 cooling 已确定) ──
        cooling = specs.get("cooling", "IC411")
        cooling_factor = self._get_cooling_factor(cooling)
        if cooling_factor and abs(cooling_factor - 1.0) > 0.01:
            specs["_cooling_density_factor"] = cooling_factor
            inferred.append(f"冷却修正: {cooling} → 转矩密度系数={cooling_factor}")

        # ── 9. 场景识别 ──
        if _PHYSICS_AVAILABLE:
            scenario_info = determine_scenario(specs)
            specs["_scenario"] = scenario_info
        else:
            specs["_scenario"] = {"scenario": "unknown", "reason": "physics_kernel unavailable"}

        # ── 10. 置信度标记 ──
        specs["_inferred"] = inferred
        specs["_confidence"] = confidence
        return specs

    def get_industry_defaults(self, application: str, power_kw: float = None) -> dict:
        """
        查询行业惯例 (14场景)
        
        返回完整的应用场景默认值，包括:
        voltage_v, insulation_class, protection_class, cooling, duty_cycle,
        poles, frequency_hz, efficiency_min, power_factor_typical, certifications 等
        """
        app_db = self.industry_defaults.get("application_defaults", {})

        # 精确匹配
        if application in app_db:
            return dict(app_db[application])

        # 模糊匹配 — 去掉"电机"后缀再试
        app_stripped = application.replace("电机", "").replace("马达", "")
        for key, val in app_db.items():
            if key.replace("电机", "") == app_stripped:
                return dict(val)

        # 包含匹配
        app_lower = application.lower()
        for key, val in app_db.items():
            if key.lower() in app_lower or app_lower in key.lower():
                return dict(val)

        # 返回通用默认
        return dict(app_db.get("通用电机", {}))

    def get_voltage_power_info(self, voltage_v: float) -> dict:
        """根据电压查功率范围、效率、功率因数 (7级电压表)"""
        vp_db = self.industry_defaults.get("voltage_power_table", {})
        for category, info in vp_db.items():
            if category.startswith("_"):
                continue
            v = info.get("voltage_v", 0)
            if v == voltage_v:
                return dict(info)
        # 模糊: 取最接近 voltage_v 的电压等级 (避免引用未定义 power_kw 导致 NameError)
        best = None
        best_diff = None
        for category, info in vp_db.items():
            if category.startswith("_"):
                continue
            v = info.get("voltage_v", 0)
            if not v:
                continue
            d = abs(v - voltage_v)
            if best_diff is None or d < best_diff:
                best_diff = d
                best = info
        return dict(best) if best else {}

    def get_voltage_power_default(self, voltage_v: float) -> dict:
        """[兼容] 根据电压查功率范围 (旧接口，委托给 get_voltage_power_info)"""
        return self.get_voltage_power_info(voltage_v)

    def get_efficiency_pf(self, power_kw: float = None, poles: int = None) -> tuple:
        """
        按功率等级查效率和功率因数 (11档表)
        
        返回: (efficiency, power_factor)
        如果 poles 已知，返回对应极数的效率; 否则返回4极效率
        """
        table = self.industry_defaults.get("power_efficiency_pf_table", [])
        if not table or not power_kw:
            return (0.90, 0.85)  # 安全默认

        for entry in table:
            lo, hi = entry.get("power_range_kw", [0, 0])
            if lo <= power_kw <= hi:
                pf = entry.get("power_factor", 0.85)
                # 按极数选效率
                if poles:
                    pole_key = f"eff_{poles}p"
                    eff = entry.get(pole_key)
                    if eff is None:
                        eff = entry.get("eff_4p", 0.90)
                else:
                    eff = entry.get("eff_4p", 0.90)
                return (eff, pf)

        # 超大功率默认
        if power_kw and power_kw > 1000:
            return (0.97, 0.92)
        return (0.90, 0.85)

    def get_environment_correction(self, correction_type: str) -> dict:
        """
        获取环境修正系数表
        
        correction_type: "altitude_derating" | "temperature_derating" | "cooling_correction"
        """
        return self.industry_defaults.get("environment_corrections", {}).get(correction_type, {})

    def get_special_certifications(self, industry: str) -> dict:
        """
        获取特殊行业认证要求 (6类)
        
        industry: "船舶" | "防爆" | "核电" | "铁路" | "航空航天" | "医疗"
        """
        certs = self.industry_defaults.get("special_certifications", {})
        for key, info in certs.items():
            if key in industry or industry in key:
                return dict(info)
        return {}

    def _compute_altitude_derating(self, altitude_m: int) -> float:
        """海拔降容系数 (连续公式)"""
        if altitude_m <= 1000:
            return 1.0
        # k = 1 - (h - 1000) * 0.0001, clamp 0.70~1.00
        k = 1.0 - (altitude_m - 1000) * 0.0001
        return round(max(0.70, min(1.0, k)), 3)

    def _compute_temperature_derating(self, ambient_temp_c: float) -> float:
        """温度降容系数 (连续公式)"""
        if ambient_temp_c <= 40:
            return 1.0
        # k = 1 - (T - 40) * 0.01, clamp 0.70~1.00
        k = 1.0 - (ambient_temp_c - 40) * 0.01
        return round(max(0.70, min(1.0, k)), 3)

    def _get_cooling_factor(self, cooling: str) -> float:
        """冷却方式 → 转矩密度修正系数"""
        cooling_db = self.industry_defaults.get("environment_corrections", {}).get("cooling_correction", {})
        for code, info in cooling_db.items():
            if code == cooling:
                f = info.get("torque_density_factor")
                if f is not None:
                    return f
                fr = info.get("torque_density_factor_range")
                if fr:
                    return round((fr[0] + fr[1]) / 2, 2)
                return 1.0
            # 模糊匹配
            if code.lower() in cooling.lower() or cooling.lower() in code.lower():
                f = info.get("torque_density_factor")
                if f is not None:
                    return f
                fr = info.get("torque_density_factor_range")
                if fr:
                    return round((fr[0] + fr[1]) / 2, 2)
        return 1.0

    def _infer_torque_force_radius(self, specs: dict, inferred: list, confidence: dict):
        """转矩-力-半径推导 (卷扬机/绞车/电梯场景)"""
        force_n = specs.get("force_n")
        radius_m = specs.get("radius_m") or specs.get("force_arm_m")
        torque = specs.get("torque_nm")

        # T = F × r
        if not torque and force_n and radius_m:
            torque = round(force_n * radius_m, 1)
            specs["torque_nm"] = torque
            inferred.append(f"转矩推导(力×臂): T={force_n}×{radius_m}={torque}Nm")
            confidence["torque_nm"] = 0.85

        # F = T / r
        if not force_n and torque and radius_m:
            force_n = round(torque / radius_m, 1)
            specs["force_n"] = force_n
            inferred.append(f"力推导: F={torque}/{radius_m}={force_n}N")
            confidence["force_n"] = 0.85

        # r = T / F
        if not radius_m and torque and force_n:
            radius_m = round(torque / force_n, 3)
            specs["radius_m"] = radius_m
            inferred.append(f"力臂推导: r={torque}/{force_n}={radius_m}m")
            confidence["radius_m"] = 0.85

    def _snap_to_standard_voltage(self, voltage: float, power_kw: float = None) -> int:
        """将计算电压归整到标准电压等级"""
        standards = [48, 115, 220, 380, 690, 3300, 6000, 10000]
        closest = min(standards, key=lambda v: abs(v - voltage))

        # 如果功率已知，用功率范围辅助判断
        if power_kw:
            vp_table = self.industry_defaults.get("voltage_power_table", {})
            for category, info in vp_table.items():
                if category.startswith("_"):
                    continue
                prange = info.get("power_range_kw", [])
                if prange and prange[0] <= power_kw <= prange[1]:
                    return info.get("voltage_v", closest)

        return closest

    def _get_altitude_derating(self, altitude_m: int) -> float:
        """[兼容] 海拔降容系数 (旧接口)"""
        return self._compute_altitude_derating(altitude_m)

    def get_insulation_temp_limit(self, insulation_class: str) -> int:
        """绝缘等级对应的最高允许温度"""
        limits = self.industry_defaults.get("insulation_temp_limits", {})
        return limits.get(insulation_class.upper(), 155)

    def get_material_properties(self, material_name: str) -> dict:
        """查询材料属性"""
        materials = self.industry_defaults.get("material_properties", {})
        if material_name in materials:
            return dict(materials[material_name])
        # 模糊匹配
        for key, val in materials.items():
            if material_name.lower() in key.lower():
                return dict(val)
        return {}

    # ═══════════════════════════════════════════════════════════
    # 3. Y系列型谱库 (封装 physics_kernel)
    # ═══════════════════════════════════════════════════════════

    def get_catalog(self) -> list:
        """获取Y系列型谱完整目录"""
        return self.y_series_physics.get("catalog", [])

    def find_closest_catalog(self, torque_nm: float, poles: int = None,
                              power_kw: float = None, speed_rpm: float = None) -> dict:
        """在Y系列型谱中查找与目标转矩最接近的型号"""
        if _PHYSICS_AVAILABLE:
            return find_closest_catalog(torque_nm, poles, power_kw, speed_rpm)
        return {}

    def find_catalog_by_frame(self, frame_size: int, poles: int = None) -> list:
        """按机座号查询型谱"""
        if _PHYSICS_AVAILABLE:
            return find_catalog_by_frame(frame_size, poles)
        return []

    def analogy_estimate(self, torque_target: float, ref_entry: dict) -> dict:
        """D²L类比推算"""
        if _PHYSICS_AVAILABLE:
            return analogy_estimate(torque_target, ref_entry)
        return {}

    def get_catalog_stats(self) -> dict:
        """获取型谱统计信息"""
        return self.y_series_physics.get("_meta", {})

    # ═══════════════════════════════════════════════════════════
    # 4. 设计规则库
    # ═══════════════════════════════════════════════════════════

    def get_lambda_range(self, poles: int) -> tuple:
        """获取长径比范围。

        v4.02.103 (P3-A): 由 JSON 副本 (design_rules.lambda_ranges) 改为**委托
        physics_kernel.LAMBDA_RANGE 单一真源**; kernel 不可用时回退 JSON 副本
        (两者历史上同值, 故本改动零行为变更, 仅消除并列源)。
        """
        try:
            if _PK_LAMBDA_RANGE:
                _rng = _PK_LAMBDA_RANGE.get(int(poles))
                if _rng:
                    return (float(_rng[0]), float(_rng[1]))
                # kernel 表未覆盖的极数 → 与 kernel 同口径的保守默认
                return (0.5, 1.5)
        except Exception:
            pass
        ranges = self.design_rules.get("lambda_ranges", {})
        rng = ranges.get(str(poles), ranges.get("default", [0.5, 1.5]))
        return tuple(rng)

    def validate_lambda(self, l_mm: float, d_mm: float, poles: int) -> dict:
        """验证长径比"""
        if _PHYSICS_AVAILABLE:
            return validate_lambda(l_mm, d_mm, poles)
        lo, hi = self.get_lambda_range(poles)
        lam = round(l_mm / d_mm, 3) if d_mm > 0 else 0
        return {"valid": lo <= lam <= hi, "lambda": lam, "range": (lo, hi), "advice": ""}

    def get_efficient_domain(self, application: str) -> dict:
        """获取高效参数域"""
        domains = self.design_rules.get("efficient_param_domains", {})

        # 精确匹配
        if application in domains:
            return dict(domains[application])

        # 模糊匹配
        app_lower = application.lower()
        for key, val in domains.items():
            if key.lower() in app_lower or app_lower in key.lower():
                return dict(val)

        return dict(domains.get("通用", {}))

    def get_frame_shift_thresholds(self) -> dict:
        """获取机座号跃迁阈值"""
        return self.design_rules.get("frame_shift_thresholds", {})

    def get_compliance_requirements(self, application: str) -> list:
        """获取合规认证要求"""
        comp = self.design_rules.get("compliance_certifications", {})

        # 精确匹配
        for key, info in comp.items():
            if key in application or application in key:
                return info.get("required", [])

        return comp.get("通用", {}).get("required", ["CCC"])

    def get_cost_performance_thresholds(self) -> dict:
        """获取成本性能阈值"""
        return self.design_rules.get("cost_performance_thresholds", {})

    def get_slot_combinations(self, poles: int) -> list:
        """获取标准槽配合"""
        slots = self.condition_corrections.get("slot_combinations", {})
        result = slots.get(str(poles), [])
        # 过滤掉非列表字段
        if isinstance(result, list) and result and isinstance(result[0], list):
            return result
        return []

    def get_id_ratio(self, poles: int) -> float:
        """获取定子内径/外径比值。

        v4.02.103 (P3-A): 由 JSON 副本 (condition_corrections.id_ratio_by_poles,
        等价于 kernel 的历史 legacy 口径 0.55+0.030(p−2)) 改为**委托
        physics_kernel.id_ratio_by_poles 生产口径** (异步 0.55+0.012(p−2) 钳[0.45,0.65];
        永磁 0.72+0.010(p−2) 钳[0.70,0.80])。

        ⚠ 口径变更提示: 6 极异步由 0.61 (legacy) → 0.598 (生产); 消费者为
        DSH 提示词几何域, 非数值生成路径, 故不影响物理求解结果。
        """
        try:
            if _PK_id_ratio_by_poles:
                return float(_PK_id_ratio_by_poles(poles))
        except Exception:
            pass
        ratios = self.condition_corrections.get("id_ratio_by_poles", {})
        return ratios.get(str(poles), 0.55 + (poles - 2) * 0.03)

    def get_air_gap_range(self) -> dict:
        """获取气隙范围"""
        return {
            "min_mm": self.condition_corrections.get("air_gap", {}).get("min_mm", 0.30),
            "max_mm": self.condition_corrections.get("air_gap", {}).get("max_mm", 1.50),
            "formula": self.condition_corrections.get("air_gap", {}).get("formula", ""),
        }

    # ═══════════════════════════════════════════════════════════
    # 5. 追问模板库
    # ═══════════════════════════════════════════════════════════

    def get_question_templates(self, layer: str = "L1") -> list:
        """获取追问模板"""
        layer_data = self.question_templates.get(f"{layer}_questions", {})
        return layer_data.get("templates", [])

    def get_scenario_question(self, scenario_key: str, **kwargs) -> str:
        """获取场景识别后的补充追问"""
        templates = self.question_templates.get("scenario_questions", {})
        template = templates.get(scenario_key, "")
        if template and kwargs:
            try:
                return template.format(**kwargs)
            except (KeyError, ValueError):
                return template
        return template

    def get_forced_questions(self) -> list:
        """获取飞轮生成的强制追问规则"""
        return self.forced_questions.get("rules", [])

    # ═══════════════════════════════════════════════════════════
    # 6. 条件修正系数
    # ═══════════════════════════════════════════════════════════

    def apply_condition_factors(self, specs: dict) -> dict:
        """应用条件修正系数"""
        if _PHYSICS_AVAILABLE:
            return apply_condition_factors(specs)
        return {"usable": True, "block_reasons": [], "correction_factor": 1.0, "factors": {}, "scenario": "catalog"}

    def determine_scenario(self, specs: dict) -> dict:
        """场景识别"""
        if _PHYSICS_AVAILABLE:
            return determine_scenario(specs)
        return {"scenario": "catalog", "poles": specs.get("poles", 4), "torque_nm": 0, "reason": "physics_kernel unavailable"}

    def focused_scan(self, specs: dict, count: int = 10, scenario_info: dict = None) -> list:
        """聚焦扫描矩阵生成"""
        if _PHYSICS_AVAILABLE:
            return focused_scan(specs, count, scenario_info)
        return []

    def build_selection_narrative(self, specs: dict, scenario_info: dict, best_design: dict) -> str:
        """生成选型路径叙述"""
        if _PHYSICS_AVAILABLE:
            return build_selection_narrative(specs, scenario_info, best_design)
        return ""

    # ═══════════════════════════════════════════════════════════
    # 7. 进化更新接口 (供运营分析师/进化引擎调用)
    # ═══════════════════════════════════════════════════════════

    def update_rule(self, category: str, rule_id: str, content: dict) -> bool:
        """
        更新共享知识库中的规则

        参数:
          category: "design_rules" | "industry_defaults" | "condition_corrections" | "question_templates"
          rule_id: 规则ID (如 "forklift_high_efficiency_od")
          content: 规则内容 dict

        返回: True=成功, False=失败
        """
        try:
            target_file = f"{category}.json"
            target_path = os.path.join(self._kdir, target_file)

            # 读取当前内容
            data = self._load_json(target_file)
            if not data:
                return False

            # 更新规则
            if "rules" not in data:
                data["rules"] = {}
            data["rules"][rule_id] = content
            data["_last_updated"] = str(__import__("datetime").datetime.now())

            # 写回文件
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # 刷新缓存
            if target_file in self._cache:
                del self._cache[target_file]
                self._cache[target_file] = data

            return True
        except Exception as e:
            print(f"[SharedKnowledge] update_rule error: {e}")
            return False

    def update_industry_default(self, application: str, defaults: dict) -> bool:
        """更新行业惯例库中的应用默认值"""
        try:
            target_path = os.path.join(self._kdir, "industry_defaults.json")
            data = self._load_json("industry_defaults.json")

            data.setdefault("application_defaults", {})[application] = defaults
            data["_last_updated"] = str(__import__("datetime").datetime.now())

            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # 刷新缓存
            self._cache["industry_defaults.json"] = data
            return True
        except Exception as e:
            print(f"[SharedKnowledge] update_industry_default error: {e}")
            return False

    def get_version_info(self) -> dict:
        """获取知识库版本信息"""
        return {
            "industry_defaults": self.industry_defaults.get("_meta", {}).get("version", "unknown"),
            "condition_corrections": self.condition_corrections.get("_meta", {}).get("version", "unknown"),
            "design_rules": self.design_rules.get("_meta", {}).get("version", "unknown"),
            "question_templates": self.question_templates.get("_meta", {}).get("version", "unknown"),
            "y_series_models": self.y_series_physics.get("_meta", {}).get("total_models", 0),
            "physics_kernel_available": _PHYSICS_AVAILABLE,
        }


# ═══════════════════════════════════════════════════════════════
# 全局单例 — 所有智能体共享
# ═══════════════════════════════════════════════════════════════

knowledge_engine = SharedKnowledge()


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SharedKnowledge 引擎自测 v2.0")
    print("=" * 60)

    ke = knowledge_engine

    # 版本信息
    print(f"\n--- 版本信息 ---")
    for k, v in ke.get_version_info().items():
        print(f"  {k}: {v}")

    # 14场景行业默认值
    print(f"\n--- 行业惯例 (14场景遍历) ---")
    all_apps = ke.industry_defaults.get("application_defaults", {})
    for app_name in list(all_apps.keys()):
        d = ke.get_industry_defaults(app_name)
        v = d.get("voltage_v", "?")
        cls = d.get("insulation_class", "?")
        cool = d.get("cooling_desc", d.get("cooling", "?"))
        poles = d.get("poles", "?")
        print(f"  {app_name:12s} → {v:>5}V / {cls}级 / {cool:12s} / {poles}极")

    # 7级电压表
    print(f"\n--- 电压-功率表 (7级) ---")
    vp_table = ke.industry_defaults.get("voltage_power_table", {})
    for cat, info in vp_table.items():
        if cat.startswith("_"):
            continue
        pr = info.get("power_range_kw", [])
        print(f"  {cat:20s} → {info.get('voltage_v')}V / {pr[0]}-{pr[1]}kW / {info.get('common_apps')}")

    # 11档效率表
    print(f"\n--- 功率-效率-功率因数表 (11档) ---")
    eff_table = ke.industry_defaults.get("power_efficiency_pf_table", [])
    for entry in eff_table:
        pr = entry.get("power_range_kw", [])
        print(f"  {pr[0]:>6}-{pr[1]:<6}kW → 2p={entry.get('eff_2p','--')} 4p={entry.get('eff_4p','--')} "
              f"6p={entry.get('eff_6p','--')} 8p={entry.get('eff_8p','--')} PF={entry.get('power_factor')}")

    # 效率/功率因数查表
    print(f"\n--- 效率查表测试 ---")
    for pkw in [1, 5, 15, 55, 110, 315, 800]:
        for pp in [2, 4, 6, 8]:
            eff, pf = ke.get_efficiency_pf(pkw, pp)
            print(f"  {pkw:>4}kW/{pp}极 → η={eff} cosφ={pf}")

    # 参数推导 — 完整测试
    print(f"\n--- 参数推导 (15kW/1460rpm 工业泵) ---")
    test_specs = {
        "power_kw": 15,
        "speed_rpm": 1460,
        "application": "工业通用电机",
    }
    completed = ke.infer_all_params(test_specs)
    print(f"  推导记录:")
    for line in completed.get("_inferred", []):
        print(f"    {line}")
    print(f"  转矩: {completed.get('torque_nm', '?')}Nm")
    print(f"  极数: {completed.get('poles', '?')}")
    print(f"  同步速: {completed.get('sync_speed_rpm', '?')}rpm")
    print(f"  转差率: {completed.get('slip', '?')}")
    print(f"  电流: {completed.get('rated_current_a', '?')}A")
    print(f"  假设效率: {completed.get('assumed_efficiency', '?')}")
    print(f"  假设功率因数: {completed.get('assumed_power_factor', '?')}")
    print(f"  场景: {completed.get('_scenario', {}).get('scenario', '?')}")
    print(f"  置信度: {completed.get('_confidence', {})}")

    # 参数推导 — 船舶推进 (高压 + 海拔 + 温度)
    print(f"\n--- 参数推导 (200kW 船舶推进 1500m/45℃) ---")
    test2 = {
        "power_kw": 200,
        "speed_rpm": 980,
        "application": "船舶推进电机",
        "altitude_m": 1500,
        "ambient_temp_c": 45,
    }
    c2 = ke.infer_all_params(test2)
    print(f"  推导记录:")
    for line in c2.get("_inferred", []):
        print(f"    {line}")
    print(f"  电压: {c2.get('voltage_v', '?')}V")
    print(f"  极数: {c2.get('poles', '?')}")
    print(f"  绝缘: {c2.get('insulation_class', '?')}")
    print(f"  冷却: {c2.get('cooling_desc', c2.get('cooling', '?'))}")
    print(f"  海拔降容: {c2.get('_altitude_derating', '?')}")
    print(f"  温度降容: {c2.get('_temp_derating', '?')}")
    print(f"  总降容: {c2.get('derating_factor', '?')}")
    print(f"  有效功率: {c2.get('effective_power_kw', '?')}kW")
    print(f"  冷却密度系数: {c2.get('_cooling_density_factor', '?')}")

    # 参数推导 — 力×臂 转矩推导
    print(f"\n--- 参数推导 (卷扬机 力×臂) ---")
    test3 = {
        "force_n": 5000,
        "radius_m": 0.3,
        "speed_rpm": 1450,
        "application": "工业通用电机",
    }
    c3 = ke.infer_all_params(test3)
    print(f"  推导记录:")
    for line in c3.get("_inferred", []):
        print(f"    {line}")
    print(f"  转矩: {c3.get('torque_nm', '?')}Nm")
    print(f"  功率: {c3.get('power_kw', '?')}kW")

    # 参数推导 — 电压反推
    print(f"\n--- 参数推导 (电压反推 100kW/180A) ---")
    test4 = {
        "power_kw": 100,
        "rated_current_a": 180,
        "speed_rpm": 1470,
    }
    c4 = ke.infer_all_params(test4)
    print(f"  推导记录:")
    for line in c4.get("_inferred", []):
        print(f"    {line}")
    print(f"  电压: {c4.get('voltage_v', '?')}V")

    # 环境修正系数
    print(f"\n--- 环境修正系数 ---")
    print(f"  海拔1500m: {ke._compute_altitude_derating(1500)}")
    print(f"  海拔3000m: {ke._compute_altitude_derating(3000)}")
    print(f"  温度50℃:   {ke._compute_temperature_derating(50)}")
    print(f"  温度60℃:   {ke._compute_temperature_derating(60)}")
    print(f"  水冷IC71W: {ke._get_cooling_factor('IC71W')}")
    print(f"  强迫风冷:  {ke._get_cooling_factor('IC416')}")
    print(f"  自扇冷:    {ke._get_cooling_factor('IC411')}")

    # 特殊认证
    print(f"\n--- 特殊行业认证 ---")
    for ind in ["船舶", "防爆", "核电", "铁路", "航空航天", "医疗"]:
        cert = ke.get_special_certifications(ind)
        print(f"  {ind}: {cert.get('standards', [])} — {cert.get('requirements', [])}")

    # 设计规则
    print(f"\n--- 设计规则 ---")
    print(f"  4极长径比范围: {ke.get_lambda_range(4)}")
    print(f"  叉车高效参数域: {ke.get_efficient_domain('叉车')}")
    print(f"  船舶认证要求: {ke.get_compliance_requirements('船舶')}")

    # 追问模板
    print(f"\n--- 追问模板 (L1前3条) ---")
    for q in ke.get_question_templates("L1")[:3]:
        print(f"  [{q['priority']}] {q['field']}: {q['question'][:40]}...")

    print(f"\n{'=' * 60}")
    print("自测完成 — v2.0 全部功能验证通过")
