"""
case_motor_agent.py - 案例电机智能体
包含三个智能体:
  1. CaseVerifyAgent   - 参数验算智能体 (正向校核 + 反向推导)
  2. ParamSuggestAgent - 参数补全智能体 (AI智能补全 + 物理推算)
  3. OCRRecognizeAgent - OCR识别智能体 (LLM智能识别铭牌参数)

验算逻辑:
  - 正向校核: 用结构参数(定子外径/铁心长度/极数等)推算性能(功率/效率/温升)
  - 反向推导: 用铭牌参数(功率/电压/转速)反推合理电磁负荷范围
  - 逻辑检查: 效率合理性(0-100%)、功率因数合理性、温升合理性等

参数补全逻辑:
  - 基于系列模板的典型值补全 (置信度 85-95)
  - 基于物理公式的推算补全 (置信度 65-90)
  - 基于经验公式的估算补全 (置信度 55-60)
  - LLM 智能补全: 前三层未覆盖的参数 (置信度 40-50)
  - 四层结果去重: 同一参数只保留置信度最高的

OCR识别逻辑:
  - LLM 智能识别: 从 OCR 原始文本或用户输入文本中提取电机铭牌参数
  - 比 正则提取 更强: 能理解上下文、纠正OCR错误、从型号推断参数
  - 与 正则提取 合并: AI结果覆盖同字段正则值, AI新增字段追加
"""
import sys
import os
import math
import json
import time
import logging
from typing import Any, Dict, List, Optional

sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Agents")

from base_agent import BaseAgent

# v4.01.026: 变量对照智能体需要字段定义 (case_motor_db 在 Core 目录)
try:
    from Core.case_motor_db import MOTOR_PARAM_FIELDS
except Exception:
    try:
        from case_motor_db import MOTOR_PARAM_FIELDS
    except Exception:
        MOTOR_PARAM_FIELDS = []

# v4.00.005: LLM 补全支持
try:
    from dotenv import load_dotenv
    load_dotenv(r"D:\MotorDesign\.env", override=False)
except Exception:
    pass

try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

# v4.01.024: VL 专用访问器 (选"使用默认模型"时回退视觉模型, 而非文本模型)
try:
    from workflow_config import get_vl_model, get_vl_timeout
except Exception:
    try:
        from Core.workflow_config import get_vl_model, get_vl_timeout
    except Exception:
        get_vl_model = lambda: os.getenv("VL_MODEL_NAME", "qwen3-vl:2b-instruct")
        get_vl_timeout = lambda: int(os.getenv("VL_TIMEOUT", "300"))

_LLM_AVAILABLE = False
try:
    from langchain_ollama import ChatOllama
    _LLM_AVAILABLE = True
except Exception:
    pass

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# v4.01.023: VL 图像识别共用 — 字段别名归一 & 无效值过滤
# qwen3-vl:2b 有时自创字段名 (Model/Power/Voltage...) 或把不可见字段填 "未识别",
# 这两个工具让 VLImageAgent 与 _call_vl_ollama 共用同一套清洗口径。
# ════════════════════════════════════════════════════════════════════════════
_VL_FIELD_ALIASES = {
    "model": "motor_type", "type": "motor_type",
    "power": "rated_power", "ratedpower": "rated_power",
    "voltage": "rated_voltage", "ratedvoltage": "rated_voltage",
    "current": "rated_current", "ratedcurrent": "rated_current",
    "speed": "rated_speed", "ratedspeed": "rated_speed",
    "frequency": "rated_freq", "freq": "rated_freq",
    "powerfactor": "power_factor",
    "torque": "starting_torque",
}
_VL_SKIP_VALUES = {
    "", "未识别", "未知", "无", "none", "null", "n/a", "na", "未找到",
    "未提供", "-", "—", "无数据", "unknown", "未识", "未", "暂无",
}


def _vl_normalize_param(name, valid_fields):
    """把模型可能自创的字段名 (Model/Power/Voltage...) 归一为白名单字段名"""
    if name is None:
        return None
    n = str(name).strip()
    if n in valid_fields:
        return n
    low = n.lower().replace(" ", "").replace("_", "")
    alias = _VL_FIELD_ALIASES.get(low)
    if alias and alias in valid_fields:
        return alias
    if low in valid_fields:
        return low
    return None


def _vl_is_skip_value(value):
    """模型对不可见字段常回 '未识别'/'未知'/'无' 等, 这些不应作为有效值写入"""
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in _VL_SKIP_VALUES:
        return True
    return ("未识别" in s) or ("未知" in s) or ("未找到" in s) or ("未提供" in s)


class CaseVerifyAgent(BaseAgent):
    """参数验算智能体

    对录入的电机参数进行多维度验算:
    1. 正向校核: 结构参数 → 性能参数 (物理公式推算)
    2. 反向推导: 铭牌参数 → 电磁负荷范围 (经验公式)
    3. 逻辑检查: 数值范围合理性、参数间一致性
    """

    def __init__(self):
        super().__init__(name="CaseVerifyAgent", description="案例电机参数验算智能体")

    def run(self, params: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行验算

        Args:
            params: 电机参数字典 (所有已确认的参数)

        Returns:
            {
                "result": "pass" | "warning" | "fail",
                "verify_type": "forward" | "reverse" | "full",
                "details": [{"item": ..., "expected": ..., "actual": ..., "deviation": ..., "status": ..., "message": ...}],
                "summary": "验算摘要文本",
                "warning_items": "警告项列表文本"
            }
        """
        details = []

        # ---- 1. 逻辑检查 (必做) ----
        details.extend(self._check_logic(params))

        # ---- 2. 正向校核 (结构→性能) ----
        if self._has_structure_params(params):
            details.extend(self._forward_verify(params))

        # ---- 3. 反向推导 (铭牌→电磁负荷) ----
        if self._has_nameplate_params(params):
            details.extend(self._reverse_verify(params))

        # ---- 汇总 ----
        fails = [d for d in details if d["status"] == "fail"]
        warns = [d for d in details if d["status"] == "warning"]
        passes = [d for d in details if d["status"] == "pass"]

        if fails:
            result = "fail"
        elif warns:
            result = "warning"
        else:
            result = "pass"

        summary = self._build_summary(result, len(passes), len(warns), len(fails))
        warning_items = "; ".join(d["message"] for d in warns) if warns else ""

        return {
            "result": result,
            "verify_type": "full",
            "details": details,
            "summary": summary,
            "warning_items": warning_items,
        }

    def _check_logic(self, p: Dict) -> List[Dict]:
        """基本逻辑检查"""
        results = []

        # 效率范围
        eff = p.get("efficiency")
        if eff is not None:
            if eff > 100 or eff < 20:
                results.append({
                    "item": "效率", "expected": "20~100%", "actual": f"{eff}%",
                    "deviation": "", "status": "fail",
                    "message": f"效率 {eff}% 超出合理范围 (20-100%)"
                })
            elif eff > 99 or eff < 40:
                results.append({
                    "item": "效率", "expected": "40~99%", "actual": f"{eff}%",
                    "deviation": "", "status": "warning",
                    "message": f"效率 {eff}% 接近边界值, 请确认"
                })
            else:
                results.append({
                    "item": "效率", "expected": "40~99%", "actual": f"{eff}%",
                    "deviation": "", "status": "pass",
                    "message": f"效率 {eff}% 在合理范围内"
                })

        # 功率因数范围
        pf = p.get("power_factor")
        if pf is not None:
            if pf > 1.0 or pf < 0.1:
                results.append({
                    "item": "功率因数", "expected": "0.1~1.0", "actual": str(pf),
                    "deviation": "", "status": "fail",
                    "message": f"功率因数 {pf} 超出合理范围 (0.1-1.0)"
                })
            elif pf < 0.5 or pf > 0.98:
                results.append({
                    "item": "功率因数", "expected": "0.5~0.98", "actual": str(pf),
                    "deviation": "", "status": "warning",
                    "message": f"功率因数 {pf} 偏{'低' if pf < 0.5 else '高'}, 请确认"
                })

        # 极数与转速一致性
        poles = p.get("poles")
        speed = p.get("rated_speed")
        freq = p.get("rated_freq", 50)
        if poles and speed and freq:
            sync_speed = 120 * freq / poles
            slip = (sync_speed - speed) / sync_speed * 100
            if abs(slip) > 10 or slip < -1:
                results.append({
                    "item": "极数-转速一致性", "expected": f"同步转速 {sync_speed:.0f} r/min",
                    "actual": f"{speed} r/min (滑差 {slip:.1f}%)",
                    "deviation": f"{abs(slip):.1f}%",
                    "status": "fail",
                    "message": f"转速 {speed} 与 {poles}极/{freq}Hz 不匹配 (同步转速 {sync_speed:.0f})"
                })
            elif slip < 0.3 or slip > 7:
                results.append({
                    "item": "极数-转速一致性", "expected": f"滑差 0.3~7%",
                    "actual": f"滑差 {slip:.1f}%",
                    "deviation": f"{abs(slip - 3.5):.1f}%",
                    "status": "warning",
                    "message": f"滑差 {slip:.1f}% 偏{'小' if slip < 0.3 else '大'}, 典型值 1-5%"
                })
            else:
                results.append({
                    "item": "极数-转速一致性", "expected": f"同步转速 {sync_speed:.0f} r/min",
                    "actual": f"{speed} r/min (滑差 {slip:.1f}%)",
                    "deviation": f"{slip:.1f}%",
                    "status": "pass",
                    "message": f"极数-转速匹配, 滑差 {slip:.1f}% 合理"
                })

        # 温升范围
        temp = p.get("temperature_rise")
        if temp is not None:
            if temp > 200 or temp < 10:
                results.append({
                    "item": "温升", "expected": "10~200K", "actual": f"{temp}K",
                    "deviation": "", "status": "fail",
                    "message": f"温升 {temp}K 超出合理范围"
                })
            elif temp > 120:
                results.append({
                    "item": "温升", "expected": "<120K (F级)", "actual": f"{temp}K",
                    "deviation": "", "status": "warning",
                    "message": f"温升 {temp}K 偏高, F级绝缘限值约 125K"
                })

        # 气隙范围 (典型 0.2-2mm)
        gap = p.get("air_gap")
        if gap is not None:
            if gap > 5 or gap < 0.1:
                results.append({
                    "item": "气隙", "expected": "0.2~2mm", "actual": f"{gap}mm",
                    "deviation": "", "status": "warning",
                    "message": f"气隙 {gap}mm 超出常见范围 (0.2-2mm)"
                })

        # 定子外径 > 定子内径
        sod = p.get("stator_od")
        sid = p.get("stator_id")
        if sod and sid and sod <= sid:
            results.append({
                "item": "定子外径-内径", "expected": "外径 > 内径",
                "actual": f"外径 {sod}mm, 内径 {sid}mm",
                "deviation": "", "status": "fail",
                "message": f"定子外径({sod})应大于内径({sid})"
            })

        # 转子外径 < 定子内径
        rod = p.get("rotor_od")
        if sid and rod and rod >= sid:
            results.append({
                "item": "转子外径-定子内径", "expected": "转子外径 < 定子内径",
                "actual": f"转子外径 {rod}mm, 定子内径 {sid}mm",
                "deviation": "", "status": "warning",
                "message": f"转子外径({rod})应小于定子内径({sid}), 气隙={sid-rod:.2f}mm"
            })

        return results

    def _forward_verify(self, p: Dict) -> List[Dict]:
        """正向校核: 用结构参数推算性能, 对比实际值"""
        results = []

        sod = p.get("stator_od")  # mm
        cl = p.get("core_length")  # mm
        poles = p.get("poles")
        freq = p.get("rated_freq", 50)
        voltage = p.get("rated_voltage", 380)

        # ================================================================
        # 经验体积法 k 值查表 (Y系列统计)
        # 公式: P = k * D_od^2 * L * n_sync / 60  (kW, D/m, L/m)
        # k = 60P / (D_od^2 * L * n_sync)
        # 选取顺序: 系列 → 极数 → 扭矩
        # ================================================================
        K_OD_TABLE = {
            # poles → (torque_low, k_low, torque_mid, k_mid, torque_high, k_high)
            # 基于Y系列统计: k = 60P/(D_od²×L×n_sync)
            2: (10,  22, 50,  27, 99999, 33),
            4: (20,  28, 200, 44, 99999, 55),
            6: (30,  35, 200, 47, 99999, 58),
            8: (80,  42, 300, 50, 99999, 60),
            10: (100, 45, 500, 52, 99999, 58),  # 10极(参考)
            12: (150, 48, 600, 52, 99999, 58),  # 12极(参考)
        }

        if sod and cl and poles:
            sync_speed = 120 * freq / poles
            d_meter = sod / 1000
            l_meter = cl / 1000

            # --- 1. 确定扭矩等级 (选取k的关键条件) ---
            actual_power = p.get("rated_power")
            rated_speed = p.get("rated_speed")
            est_torque_n = None
            k_used = None
            k_source = ""

            # 1a. 若有额定功率 → 计算额定扭矩
            if actual_power and rated_speed and rated_speed > 0:
                est_torque_n = 9550 * actual_power / rated_speed
            elif actual_power and sync_speed > 0:
                # 无额定转速 → 按3.5%滑差估算
                est_speed = sync_speed * 0.965
                est_torque_n = 9550 * actual_power / est_speed

            # 1b. 查表选k
            poles_int = int(poles) if poles else 4
            k_entry = K_OD_TABLE.get(poles_int)
            if k_entry:
                t_low, k_low, t_mid, k_mid, t_high, k_high = k_entry
                if est_torque_n is not None:
                    if est_torque_n < t_low:
                        k_used = k_low
                        k_source = f"Y系列{poles_int}P/T<{t_low}Nm"
                    elif est_torque_n < t_mid:
                        k_used = k_mid
                        k_source = f"Y系列{poles_int}P/{t_low}≤T<{t_mid}Nm"
                    else:
                        k_used = k_high
                        k_source = f"Y系列{poles_int}P/T≥{t_mid}Nm"
                else:
                    # 无额定功率 → 取极数中位数
                    k_used = k_mid
                    k_source = f"Y系列{poles_int}P(中位数,无功率信息)"
            else:
                k_used = 40  # 通用默认
                k_source = f"通用默认({poles_int}P未收录)"

            # 1c. 修正系数
            corrections = []
            # 冷却方式修正: 强制风冷+5%, 水冷+10%
            cooling = p.get("cooling_method", "")
            if "水冷" in str(cooling) or "water" in str(cooling).lower():
                k_used *= 1.10
                corrections.append("水冷+10%")
            elif "风冷" in str(cooling) or "fan" in str(cooling).lower():
                k_used *= 1.05
                corrections.append("强制风冷+5%")
            # 防护等级修正: IP54+以上散热差 -5%
            ip_rating = p.get("ip_rating", p.get("protection_class", ""))
            if "IP54" in str(ip_rating) or "IP55" in str(ip_rating) or "IP56" in str(ip_rating):
                k_used *= 0.95
                corrections.append("高防护-5%")

            # --- 2. 计算估算功率 ---
            est_power = k_used * (d_meter ** 2) * l_meter * sync_speed / 60

            corr_str = f" [{'+'.join(corrections)}]" if corrections else ""

            if actual_power:
                deviation = abs(est_power - actual_power) / actual_power * 100
                if deviation > 50:
                    status = "warning"
                    msg = (f"结构推算功率 {est_power:.1f}kW (k={k_used:.0f}, {k_source}{corr_str}), "
                           f"实际 {actual_power}kW, 偏差 {deviation:.0f}%")
                elif deviation > 20:
                    status = "warning"
                    msg = (f"结构推算功率 {est_power:.1f}kW (k={k_used:.0f}, {k_source}{corr_str}), "
                           f"实际 {actual_power}kW, 偏差 {deviation:.0f}%, 需确认")
                else:
                    status = "pass"
                    msg = (f"结构推算功率 {est_power:.1f}kW (k={k_used:.0f}, {k_source}{corr_str}) "
                           f"与实际 {actual_power}kW 吻合 (偏差 {deviation:.0f}%)")
                results.append({
                    "item": "功率-结构一致性 (正向)",
                    "expected": f"~{est_power:.1f}kW",
                    "actual": f"{actual_power}kW",
                    "deviation": f"{deviation:.1f}%",
                    "status": status,
                    "message": msg
                })
            else:
                # 无额定功率 → 仅给估算值
                results.append({
                    "item": "结构推算功率 (估算)",
                    "expected": f"~{est_power:.1f}kW",
                    "actual": "未提供",
                    "deviation": "",
                    "status": "pass",
                    "message": f"结构推算功率约 {est_power:.1f}kW (k={k_used:.0f}, {k_source}{corr_str}, "
                               f"D={sod}mm, L={cl}mm, n={sync_speed:.0f}r/min)"
                })

            # 估算转矩
            if actual_power:
                use_speed = rated_speed if rated_speed else sync_speed * 0.965
                est_torque = actual_power * 1000 / (2 * math.pi * use_speed / 60)
                actual_torque = p.get("max_torque")
                if actual_torque:
                    # 最大转矩通常是额定转矩的 2-3 倍
                    expected_max = est_torque * 2.5
                    dev = abs(actual_torque - expected_max) / expected_max * 100
                    if dev > 50:
                        status = "warning"
                    else:
                        status = "pass"
                    results.append({
                        "item": "最大转矩合理性",
                        "expected": f"~{expected_max:.0f}N.m (2.5x额定)",
                        "actual": f"{actual_torque}N.m",
                        "deviation": f"{dev:.0f}%",
                        "status": status,
                        "message": f"额定转矩约 {est_torque:.0f}N.m, 最大转矩 {actual_torque}N.m ({actual_torque/est_torque:.1f}x)"
                    })

        # 磁密范围检查
        for flux_name, label, low, high in [
            ("stator_tooth_flux", "定子齿磁密", 1.0, 2.0),
            ("stator_yoke_flux", "定子轭磁密", 0.8, 1.8),
            ("air_gap_flux", "气隙磁密", 0.5, 1.2),
        ]:
            val = p.get(flux_name)
            if val is not None:
                if val > high * 1.2 or val < low * 0.5:
                    status = "warning"
                    msg = f"{label} {val}T 超出常见范围 ({low}-{high}T)"
                elif val > high or val < low:
                    status = "warning"
                    msg = f"{label} {val}T 接近边界 ({low}-{high}T)"
                else:
                    status = "pass"
                    msg = f"{label} {val}T 在合理范围内"
                results.append({
                    "item": label,
                    "expected": f"{low}-{high}T",
                    "actual": f"{val}T",
                    "deviation": "",
                    "status": status,
                    "message": msg
                })

        # 电流密度
        cd = p.get("current_density")
        if cd is not None:
            if cd > 10 or cd < 1:
                status = "warning"
                msg = f"电流密度 {cd} A/mm2 超出常见范围 (2-8 A/mm2)"
            elif cd > 8 or cd < 2:
                status = "warning"
                msg = f"电流密度 {cd} A/mm2 偏{'高' if cd > 8 else '低'} (典型 3-6 A/mm2)"
            else:
                status = "pass"
                msg = f"电流密度 {cd} A/mm2 在合理范围内"
            results.append({
                "item": "电流密度",
                "expected": "2-8 A/mm2",
                "actual": f"{cd} A/mm2",
                "deviation": "",
                "status": status,
                "message": msg
            })

        return results

    def _reverse_verify(self, p: Dict) -> List[Dict]:
        """反向推导: 用铭牌参数反推合理电磁负荷范围"""
        results = []

        power = p.get("rated_power")
        voltage = p.get("rated_voltage", 380)
        eff = p.get("efficiency", 90)
        pf = p.get("power_factor", 0.85)
        speed = p.get("rated_speed")
        poles = p.get("poles")

        if power and voltage:
            # 反推额定电流
            input_power = power * 1000 / (eff / 100) if eff else power * 1000 / 0.9
            est_current = input_power / (math.sqrt(3) * voltage * (pf or 0.85))
            actual_current = p.get("rated_current")

            if actual_current:
                dev = abs(est_current - actual_current) / actual_current * 100
                if dev > 30:
                    status = "warning"
                else:
                    status = "pass"
                results.append({
                    "item": "额定电流-功率一致性 (反向)",
                    "expected": f"~{est_current:.1f}A",
                    "actual": f"{actual_current}A",
                    "deviation": f"{dev:.1f}%",
                    "status": status,
                    "message": f"由功率反推电流 {est_current:.1f}A, 实际 {actual_current}A (偏差 {dev:.1f}%)"
                })
            else:
                # 补全建议
                results.append({
                    "item": "额定电流 (反向推算)",
                    "expected": "",
                    "actual": f"{est_current:.1f}A (推算值)",
                    "deviation": "",
                    "status": "pass",
                    "message": f"可补全额定电流: {est_current:.1f}A"
                })

        # 起动电流倍数检查
        sc = p.get("starting_current")
        if sc and actual_current:
            ratio = sc / actual_current
            if ratio > 10 or ratio < 3:
                status = "warning"
                msg = f"起动电流倍数 {ratio:.1f}x 超出常见范围 (4-8x)"
            else:
                status = "pass"
                msg = f"起动电流倍数 {ratio:.1f}x 合理"
            results.append({
                "item": "起动电流倍数",
                "expected": "4-8x",
                "actual": f"{ratio:.1f}x",
                "deviation": "",
                "status": status,
                "message": msg
            })

        # 起动转矩倍数
        st = p.get("starting_torque")
        if st and power and speed:
            rated_torque = power * 1000 / (2 * math.pi * speed / 60)
            ratio = st / rated_torque
            if ratio > 5 or ratio < 0.5:
                status = "warning"
                msg = f"起动转矩倍数 {ratio:.2f}x 超出常见范围 (1-3x)"
            else:
                status = "pass"
                msg = f"起动转矩倍数 {ratio:.2f}x 合理"
            results.append({
                "item": "起动转矩倍数",
                "expected": "1-3x",
                "actual": f"{ratio:.2f}x",
                "deviation": "",
                "status": status,
                "message": msg
            })

        return results

    def _has_structure_params(self, p: Dict) -> bool:
        return any(p.get(k) for k in ["stator_od", "core_length", "slots_stator"])

    def _has_nameplate_params(self, p: Dict) -> bool:
        return any(p.get(k) for k in ["rated_power", "rated_voltage", "rated_speed"])

    def _build_summary(self, result: str, passes: int, warns: int, fails: int) -> str:
        icon = {"pass": "✅", "warning": "⚠️", "fail": "❌"}[result]
        text = {
            "pass": "所有指标偏差均在合理范围内",
            "warning": "部分指标存在偏差, 请确认后提交",
            "fail": "存在严重逻辑错误, 必须返回修改"
        }[result]
        return f"{icon} 验算结果: {result.upper()} — {text} (通过{passes}项 / 警告{warns}项 / 不通过{fails}项)"


class ParamSuggestAgent(BaseAgent):
    """参数补全智能体

    根据已填参数, 智能补全缺失参数:
    1. 系列模板典型值 (效率/功率因数/绝缘等级等)
    2. 物理公式推算 (额定电流/转矩/同步转速等)
    3. 经验公式估算 (定子外径/铁心长度等)
    4. LLM 智能补全 (前三层未覆盖的参数, 如电磁参数/损耗/绕组等)
    """

    def __init__(self):
        super().__init__(name="ParamSuggestAgent", description="案例电机参数补全智能体")
        # v4.00.005: 初始化占位 (模型改为每次调用时动态读取, 支持运行时切换)
        self._llm = None

    def _get_llm(self):
        """每次调用时动态创建 LLM, 经网关路由 (管理端模型配置即时生效 + 云端自动降级)。"""
        if not _LLM_AVAILABLE:
            return None
        return super()._get_llm_or_cloud(temperature=0.1)

    def run(self, params: Dict[str, Any], series_id: str = "", **kwargs) -> Dict[str, Any]:
        """执行参数补全

        Args:
            params: 已填写的参数字典
            series_id: 电机系列ID (用于模板匹配)

        Returns:
            {
                "suggestions": [
                    {"param": "rated_current", "value": 11.2, "unit": "A", "confidence": 85, "reason": "..."},
                    ...
                ],
                "completeness_before": 45.0,
                "completeness_after": 78.5
            }
        """
        suggestions = []

        # ---- 1. 系列模板补全 ----
        suggestions.extend(self._suggest_from_template(params, series_id))

        # ---- 2. 物理公式推算 ----
        suggestions.extend(self._suggest_from_physics(params))

        # ---- 3. 经验估算 ----
        suggestions.extend(self._suggest_from_experience(params))

        # ---- 4. LLM 智能补全 (仅补全前三层未覆盖的参数) ----
        already_suggested = {s["param"] for s in suggestions}
        llm_suggestions = self._suggest_from_llm(params, series_id, already_suggested)
        suggestions.extend(llm_suggestions)

        # 去重 (同一参数只保留置信度最高的)
        seen = {}
        for s in suggestions:
            key = s["param"]
            if key not in seen or s["confidence"] > seen[key]["confidence"]:
                seen[key] = s
        final = list(seen.values())

        return {
            "suggestions": final,
            "count": len(final),
        }

    def _suggest_from_template(self, p: Dict, series_id: str) -> List[Dict]:
        """从系列模板补全"""
        suggestions = []
        sys.path.insert(0, r"D:\MotorDesign\Core")
        try:
            from case_motor_db import SERIES_TEMPLATES
            tpl = SERIES_TEMPLATES.get(series_id, {})
            defaults = tpl.get("default_params", {})
            eff_range = tpl.get("typical_efficiency_range", {})

            for key, val in defaults.items():
                if p.get(key) is None:
                    confidence = 95 if key in ("rated_freq", "phase") else 85
                    suggestions.append({
                        "param": key,
                        "value": val,
                        "unit": self._get_unit(key),
                        "confidence": confidence,
                        "reason": f"系列模板继承 ({tpl.get('name', series_id)})"
                    })

            # 效率范围补全
            poles = str(p.get("poles", ""))
            if poles in eff_range and p.get("efficiency") is None:
                low, high = eff_range[poles]
                mid = (low + high) / 2
                suggestions.append({
                    "param": "efficiency",
                    "value": round(mid, 1),
                    "unit": "%",
                    "confidence": 70,
                    "reason": f"{series_id}系列{poles}极典型效率范围 {low}-{high}%, 取中间值"
                })
        except Exception:
            pass
        return suggestions

    def _suggest_from_physics(self, p: Dict) -> List[Dict]:
        """物理公式推算"""
        suggestions = []

        power = p.get("rated_power")
        voltage = p.get("rated_voltage", 380)
        eff = p.get("efficiency", 90)
        pf = p.get("power_factor", 0.85)
        speed = p.get("rated_speed")
        poles = p.get("poles")
        freq = p.get("rated_freq", 50)

        # 额定电流
        if power and voltage and p.get("rated_current") is None:
            input_power = power * 1000 / (eff / 100) if eff else power * 1000 / 0.9
            est_current = input_power / (math.sqrt(3) * voltage * (pf or 0.85))
            suggestions.append({
                "param": "rated_current",
                "value": round(est_current, 1),
                "unit": "A",
                "confidence": 88,
                "reason": f"P=({power}kW / {eff}% / {pf}) / (√3 × {voltage}V) = {est_current:.1f}A"
            })

        # 同步转速
        if poles and freq and p.get("rated_speed") is None:
            sync_speed = 120 * freq / poles
            suggestions.append({
                "param": "rated_speed",
                "value": round(sync_speed * 0.97),  # 扣除3%滑差
                "unit": "r/min",
                "confidence": 90,
                "reason": f"同步转速 120×{freq}/{poles}={sync_speed:.0f}, 额定约 {sync_speed*0.97:.0f} (滑差3%)"
            })

        # 额定转矩
        if power and speed and p.get("max_torque") is None:
            rated_torque = power * 1000 / (2 * math.pi * speed / 60)
            est_max = rated_torque * 2.5
            suggestions.append({
                "param": "max_torque",
                "value": round(est_max, 0),
                "unit": "N.m",
                "confidence": 72,
                "reason": f"额定转矩 {rated_torque:.0f}N.m × 2.5倍 = {est_max:.0f}N.m"
            })
            # 起动转矩
            if p.get("starting_torque") is None:
                est_start = rated_torque * 1.8
                suggestions.append({
                    "param": "starting_torque",
                    "value": round(est_start, 0),
                    "unit": "N.m",
                    "confidence": 68,
                    "reason": f"额定转矩 {rated_torque:.0f}N.m × 1.8倍 = {est_start:.0f}N.m"
                })

        # 起动电流
        if power and voltage and p.get("starting_current") is None and p.get("rated_current"):
            est_sc = p["rated_current"] * 6.0
            suggestions.append({
                "param": "starting_current",
                "value": round(est_sc, 1),
                "unit": "A",
                "confidence": 65,
                "reason": f"额定电流 × 6.0倍 = {est_sc:.1f}A"
            })

        return suggestions

    def _suggest_from_experience(self, p: Dict) -> List[Dict]:
        """经验估算"""
        suggestions = []

        power = p.get("rated_power")
        poles = p.get("poles")
        sod = p.get("stator_od")

        # 定子外径估算 (基于功率和极数)
        if power and sod is None:
            # 经验公式: D ≈ k * P^(1/3), k 随极数变化
            k_map = {"2": 85, "4": 95, "6": 105, "8": 115}
            k = k_map.get(str(poles), 100)
            est_od = k * (power ** (1/3))
            # 取标准机座号
            standard_od = self._round_to_standard(est_od)
            suggestions.append({
                "param": "stator_od",
                "value": standard_od,
                "unit": "mm",
                "confidence": 60,
                "reason": f"经验公式 D≈{k}×{power}^(1/3)={est_od:.0f}mm, 取标准值 {standard_od}mm"
            })

        # 铁心长度估算
        if power and sod and poles and p.get("core_length") is None:
            freq = p.get("rated_freq", 50)
            sync_speed = 120 * freq / poles
            # L = P * 10^9 / (C * D^2 * n_sync), C≈2.0
            est_l = power * 1000 * 60 / (2.0 * (sod ** 2) * sync_speed) * 1000
            est_l = max(50, round(est_l / 10) * 10)  # 取整到10mm
            suggestions.append({
                "param": "core_length",
                "value": est_l,
                "unit": "mm",
                "confidence": 55,
                "reason": f"体积法 L = P×60 / (2.0×{sod}²×{sync_speed:.0f}) ≈ {est_l}mm"
            })

        # 功率因数估算
        if power and p.get("power_factor") is None:
            # 经验: 小功率低PF, 大功率高PF
            if power < 5:
                est_pf = 0.78
            elif power < 30:
                est_pf = 0.83
            elif power < 100:
                est_pf = 0.87
            else:
                est_pf = 0.89
            suggestions.append({
                "param": "power_factor",
                "value": est_pf,
                "unit": "",
                "confidence": 58,
                "reason": f"功率{power}kW级典型功率因数 ≈ {est_pf}"
            })

        return suggestions

    def _round_to_standard(self, od: float) -> int:
        """取标准机座号外径"""
        standard_ods = [80, 90, 100, 112, 132, 160, 180, 200, 210, 225, 250, 280,
                        315, 350, 400, 450, 500, 560, 630, 740, 850, 990]
        for s in standard_ods:
            if od <= s:
                return s
        return int(od)

    def _get_unit(self, param_name: str) -> str:
        """获取参数单位"""
        units = {
            "rated_power": "kW", "rated_voltage": "V", "rated_current": "A",
            "rated_speed": "r/min", "rated_freq": "Hz", "poles": "",
            "phase": "", "efficiency": "%", "power_factor": "",
            "insulation_class": "", "protection_class": "", "cooling_method": "",
            "stator_od": "mm", "stator_id": "mm", "core_length": "mm",
            "air_gap": "mm", "max_torque": "N.m", "starting_torque": "N.m",
            "starting_current": "A", "temperature_rise": "K",
            # v4.00.009: 扩展字段单位
            "rotor_od": "mm", "shaft_dia": "mm", "tooth_width": "mm",
            "yoke_thickness": "mm", "shaft_length": "mm",
            "stator_hs0": "mm", "stator_hs1": "mm", "stator_hs2": "mm",
            "stator_bs0": "mm", "stator_bs1": "mm", "stator_bs2": "mm",
            "stator_rs": "mm", "stator_skew": "slot", "stator_slot_ins": "mm",
            "rotor_id": "mm", "rotor_hs0": "mm", "rotor_hs1": "mm", "rotor_hs2": "mm",
            "rotor_bs0": "mm", "rotor_bs1": "mm", "rotor_bs2": "mm",
            "wire_dia": "mm", "wire_wrap": "mm",
            "rotor_bar_width": "mm", "rotor_bar_height": "mm",
            "end_ring_width": "mm", "end_ring_height": "mm",
            "wind_loss": "W", "reference_speed": "r/min",
        }
        return units.get(param_name, "")

    # ─── 第 4 层: LLM 智能补全 ───

    def _suggest_from_llm(self, p: Dict, series_id: str,
                          already_suggested: set) -> List[Dict]:
        """LLM 智能补全 — 第 4 层

        利用本地大模型对前三层未覆盖的缺失参数进行推理补全。
        置信度 40-50 (低于规则引擎 55-95), dedup 时自动让位于更可靠的规则结果。
        LLM 仅补全用户未填 + 前三层未覆盖的参数, 避免浪费算力。
        """
        # v4.00.006 + v4.02.06 H-3 二期 T5: 统一本地/云端路由 (按 ai_flow 配置)
        llm = self._get_llm_or_cloud(temperature=0.1)
        if not llm:
            return []

        # 加载参数字段定义 (中文名 + 单位)
        try:
            from case_motor_db import MOTOR_PARAM_FIELDS
        except Exception:
            return []

        # 找出缺失参数 (用户未填 + 前三层未补全)
        missing = []
        for field_name, cn_name, unit, group in MOTOR_PARAM_FIELDS:
            if p.get(field_name) is None and field_name not in already_suggested:
                missing.append({"param": field_name, "name": cn_name, "unit": unit})

        if not missing:
            return []

        # v4.00.012: 限制 LLM 处理的缺失参数数量 (最多 25 个), 避免 prompt 过长导致超时
        _PRIORITY_GROUPS = ["铭牌参数", "额定参数", "结构参数", "槽形参数", "绕组参数", "材料", "其他"]
        if len(missing) > 25:
            # 按分组优先级排序, 取前 25 个
            missing.sort(key=lambda x: _PRIORITY_GROUPS.index(x.get("group", "其他")) if x.get("group", "其他") in _PRIORITY_GROUPS else len(_PRIORITY_GROUPS))
            missing_limited = missing[:25]
            logger.info("[ParamSuggestAgent] 缺失 %d 个参数, LLM 仅处理前 %d 个 (优先级排序)",
                        len(missing), len(missing_limited))
            missing = missing_limited

        # 构建已知参数摘要
        known = {}
        for field_name, cn_name, unit, group in MOTOR_PARAM_FIELDS:
            val = p.get(field_name)
            if val is not None:
                known[field_name] = {"name": cn_name, "value": val, "unit": unit}

        # 构建 prompt
        system_msg = (
            "你是一位电机设计专家。根据用户已知的电机参数, 推断缺失参数的合理值。\n"
            "只返回 JSON 数组, 不要包含任何解释文字或 markdown 标记。\n"
            '数组元素格式: {"param": "参数英文名", "value": 数值或字符串, "reason": "简短推理依据"}\n'
            "value 必须是纯数值(数字)或简单字符串(如 'F', 'IP54'), 不要包含单位。"
        )

        user_msg = json.dumps({
            "series": series_id or "未知",
            "known_params": known,
            "missing_params": missing,
        }, ensure_ascii=False, indent=2)

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            messages = [SystemMessage(content=system_msg), HumanMessage(content=user_msg)]
            # v4.00.012: ChatOllama 已设 timeout=60, 这里加 try/except 防止阻塞
            response = llm.invoke(messages)
            content = response.content.strip()

            # 清理 markdown 包裹
            if content.startswith("```"):
                lines = content.split("\n")
                if lines[-1].strip().startswith("```"):
                    content = "\n".join(lines[1:-1])
                else:
                    content = "\n".join(lines[1:])

            # 解析 JSON
            parsed = self._robust_json_parse(content)
            if not isinstance(parsed, list):
                logger.warning("[ParamSuggestAgent] LLM 返回非数组: %s", content[:200])
                return []

            suggestions = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                param = item.get("param", "")
                value = item.get("value")
                reason = item.get("reason", "LLM 推理补全")

                # 跳过无效项
                if not param or value is None:
                    continue
                # 跳过已被用户填写或前三层已补全的 (双重保险)
                if p.get(param) is not None or param in already_suggested:
                    continue

                confidence = self._llm_confidence(param)
                value = self._clean_llm_value(param, value)

                suggestions.append({
                    "param": param,
                    "value": value,
                    "unit": self._get_unit(param),
                    "confidence": confidence,
                    "reason": f"[AI] {reason}"
                })

            logger.info("[ParamSuggestAgent] LLM 补全 %d 个参数 (请求 %d 个)",
                        len(suggestions), len(missing))
            return suggestions

        except Exception as e:
            logger.warning("[ParamSuggestAgent] LLM 补全失败: %s", e)
            return []

    def _llm_confidence(self, param: str) -> int:
        """LLM 建议的置信度 (40-50, 低于规则引擎 55-95)"""
        # 有物理公式可算的参数 — LLM 只是辅助, 置信度最低
        formula_params = {
            "rated_current", "rated_speed", "max_torque", "starting_torque",
            "starting_current", "stator_od", "core_length", "power_factor",
            "efficiency",
        }
        # 纯经验参数 (规则引擎难覆盖) — LLM 价值更高, 置信度略高
        experience_params = {
            "noise_level", "vibration_level", "temperature_rise",
            "stator_tooth_flux", "stator_yoke_flux", "rotor_yoke_flux",
            "air_gap_flux", "current_density",
            "copper_loss", "iron_loss", "total_loss", "friction_loss", "stray_loss",
            "stator_id", "rotor_od", "air_gap", "shaft_dia",
            "tooth_width", "yoke_thickness",
            "slots_stator", "slots_rotor", "turns_per_coil",
            "wire_dia", "parallel_branches",
        }
        if param in formula_params:
            return 42
        elif param in experience_params:
            return 48
        else:
            return 45

    def _clean_llm_value(self, param: str, value):
        """清洗 LLM 返回的值 — 去除单位字符串, 转换为正确类型"""
        import re as _re

        # 纯数字直接返回
        if isinstance(value, (int, float)):
            # 效率: LLM 可能返回 0.85 表示 85%, 转换为 85
            if param == "efficiency" and isinstance(value, float) and 0 < value < 1:
                return round(value * 100, 1)
            return value

        # 字符串处理
        s = str(value).strip()

        # 字符串类参数 (绝缘等级/防护等级/冷却方式/振动等级) — 保留原文
        string_params = {"insulation_class", "protection_class", "cooling_method", "vibration_level"}
        if param in string_params:
            return s

        # 数值类参数 — 提取数字
        # 匹配: 120mm, 0.85, 68dB, 3.5mm/s, 1.4T, 10A, 50Hz 等
        m = _re.match(r'^[\-]?\d+\.?\d*', s)
        if m:
            num_str = m.group()
            try:
                num = float(num_str)
                if num == int(num):
                    num = int(num)
                # 效率: 0.85 → 85
                if param == "efficiency" and 0 < num < 1:
                    return round(num * 100, 1)
                return num
            except (ValueError, TypeError):
                pass

        # 无法解析, 返回原始字符串
        return s

    def _robust_json_parse(self, text: str):
        """从 LLM 响应文本中提取 JSON 数组"""
        import re as _re
        # 尝试直接解析
        try:
            return json.loads(text)
        except Exception:
            pass
        # 尝试提取第一个 JSON 数组
        match = _re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return None


class OCRRecognizeAgent(BaseAgent):
    """OCR 识别智能体 (v4.00.007 四维优化版)

    利用 LLM 从 OCR 原始文本或用户输入文本中智能提取电机铭牌参数。

    四维优化策略:
      1. 消除不确定性 — 电机系列知识库, 从型号推断标准参数
      2. 几何逻辑一致性校验 — 极数-转速、功率-电流-电压、效率范围交叉验证
      3. 语义标准化 — 单位清洗、格式归一化、OCR纠错
      4. 敏感性分析 — 参数优先级分类 (高/中/低), 指导用户优先确认

    工作流程:
      1. 接收原始文本 (来自 Tesseract OCR 或用户粘贴)
      2. 正则提取 → LLM 智能提取 → 合并去重
      3. 从型号推断缺失参数 (系列知识库)
      4. 物理一致性校验 + 置信度评估
      5. 返回带优先级和校验结果的参数表
    """

    # ── 全字段定义 (11大类, 覆盖RMxprt全部输入参数) ──
    # 1.通用参数 2.定子参数 3.定子槽形参数 4.转子参数 5.转子槽形参数
    # 6.绕组参数 7.转轴参数 8.材料参数 9.性能参数 10.电磁参数 11.损耗参数
    _FIELD_DEFS = [
        # ═══ 1. 通用参数 (General) ═══
        ("motor_type",         "型号",         "",     "如 Y2-132S-4"),
        ("rated_power",        "额定功率",     "kW",   "如 5.5"),
        ("rated_voltage",      "额定电压",     "V",    "如 380"),
        ("rated_speed",        "额定转速",     "r/min","如 1440"),
        ("rated_freq",         "额定频率",     "Hz",   "如 50"),
        ("poles",              "极数",         "",     "如 4"),
        ("phase",              "相数",         "",     "如 3"),
        ("operating_temp",     "工作温度",     "℃",    "如 75"),
        ("load_type",          "负载类型",     "",     "如 Constant Power/恒功率"),
        ("insulation_class",   "绝缘等级",     "",     "如 F"),
        ("protection_class",   "防护等级",     "",     "如 IP55"),
        ("cooling_method",     "冷却方式",     "",     "如 IC411"),
        # ═══ 2. 定子参数 (Stator) ═══
        ("stator_od",          "定子外径",     "mm",   "如 210"),
        ("stator_id",          "定子内径",     "mm",   "如 136"),
        ("core_length",        "定子铁心长度", "mm",   "如 115"),
        ("air_gap",            "气隙长度",     "mm",   "如 0.5"),
        ("slots_stator",       "定子槽数",     "",     "如 36"),
        ("lamination_sectors", "叠片扇形数",   "",     "如 0 (0=整圆冲片)"),
        ("stator_skew",        "定子斜槽",     "slot", "如 1.0 (斜槽数)"),
        ("tooth_width",        "齿宽",         "mm",   "如 5.2"),
        ("yoke_thickness",     "轭厚",         "mm",   "如 15"),
        # ═══ 3. 定子槽形参数 (Stator Slot) ═══
        ("stator_slot_type",   "定子槽形类型", "",     "如 梨形/梯形/圆形"),
        ("stator_hs0",         "定子槽口高Hs0","mm",   "如 1.0"),
        ("stator_hs1",         "定子槽肩高Hs1","mm",   "如 0.8"),
        ("stator_hs2",         "定子槽身高Hs2","mm",   "如 12.5"),
        ("stator_bs0",         "定子槽口宽Bs0","mm",   "如 3.2"),
        ("stator_bs1",         "定子槽肩宽Bs1","mm",   "如 7.5"),
        ("stator_bs2",         "定子槽身宽Bs2","mm",   "如 9.8"),
        ("stator_rs",          "定子槽底半径Rs","mm",  "如 4.9"),
        ("stator_slot_ins",    "定子槽绝缘",   "mm",   "如 0.25"),
        # ═══ 4. 转子参数 (Rotor) ═══
        ("rotor_od",           "转子外径",     "mm",   "如 135.3"),
        ("rotor_id",           "转子内径",     "mm",   "如 48"),
        ("rotor_core_length",  "转子铁心长度", "mm",   "如 115 (通常与定子相同)"),
        ("slots_rotor",        "转子槽数",     "",     "如 32"),
        ("rotor_cast",         "铸铝转子",     "",     "如 是/否 (True/False)"),
        ("rotor_double_cage",  "双笼转子",     "",     "如 是/否"),
        ("rotor_deep_bar",     "深槽转子",     "",     "如 是/否"),
        ("rotor_wound",        "绕线转子",     "",     "如 是/否"),
        # ═══ 5. 转子槽形参数 (Rotor Slot) ═══
        ("rotor_slot_type",    "转子槽形类型", "",     "如 梯形/梨形/刀形"),
        ("rotor_hs0",          "转子槽口高Hs0","mm",   "如 0.5"),
        ("rotor_hs1",          "转子槽肩高Hs1","mm",   "如 1.0"),
        ("rotor_hs2",          "转子槽身高Hs2","mm",   "如 10.0"),
        ("rotor_bs0",          "转子槽口宽Bs0","mm",   "如 2.0"),
        ("rotor_bs1",          "转子槽肩宽Bs1","mm",   "如 5.0"),
        ("rotor_bs2",          "转子槽身宽Bs2","mm",   "如 7.0"),
        # ═══ 6. 绕组参数 (Winding) ═══
        ("winding_type",       "绕组类型",     "",     "如 单层/双层/叠绕/波绕/同心/交叉式"),
        ("winding_layer",      "绕组层数",     "",     "如 1=单层, 2=双层"),
        ("parallel_branches",  "并联支路数",   "",     "如 1"),
        ("conductors_per_slot","每槽导体数",   "",     "如 47"),
        ("turns_per_coil",     "每圈匝数",     "",     "如 15"),
        ("strands",            "并绕根数",     "",     "如 2"),
        ("wire_dia",           "线径",         "mm",   "如 0.93"),
        ("wire_wrap",          "线径包带",     "mm",   "如 0.05"),
        ("winding_pitch",      "绕组节距",     "",     "如 1-9/2-10/19-27 (交叉式)"),
        ("coil_pitch",         "线圈节距",     "",     "如 1-9 或 7"),
        ("connection_type",    "接法",         "",     "如 Y/△/Delta/Y-△"),
        ("rotor_winding_type", "转子绕组类型", "",     "如 铸铝/Cast Aluminum"),
        ("rotor_bar_material", "转子条材料",   "",     "如 铸铝/Aluminum/铜"),
        ("end_ring_material",  "端环材料",     "",     "如 铸铝/Aluminum/铜"),
        ("rotor_bar_width",    "转子条宽",     "mm",   "如 5.0"),
        ("rotor_bar_height",   "转子条高",     "mm",   "如 12.0"),
        ("end_ring_width",     "端环宽",       "mm",   "如 15.0"),
        ("end_ring_height",    "端环高",       "mm",   "如 20.0"),
        # ═══ 7. 转轴参数 (Shaft) ═══
        ("shaft_dia",          "轴径",         "mm",   "如 38"),
        ("shaft_length",       "轴长度",       "mm",   "如 400"),
        ("friction_loss",      "摩擦损耗",     "W",    "如 0"),
        ("wind_loss",          "风磨损耗",     "W",    "如 0"),
        ("reference_speed",    "参考转速",     "r/min","如 1500 (同步转速)"),
        # ═══ 8. 材料参数 (Material) ═══
        ("stator_material",    "定子铁芯材料", "",     "如 DW470_50/50WW350"),
        ("rotor_material",     "转子铁芯材料", "",     "如 DW470_50"),
        ("conductor_material", "导体材料",     "",     "如 Copper/铜"),
        ("shaft_material",     "轴材料",       "",     "如 Steel_1010/45钢/42CrMo"),
        # ═══ 9. 性能参数 (Performance) ═══
        ("rated_current",      "额定电流",     "A",    "如 11.6"),
        ("efficiency",         "效率",         "%",    "如 85 (百分比数值)"),
        ("power_factor",       "功率因数",     "",     "如 0.85"),
        ("starting_torque",    "起动转矩",     "N.m",  "如 60"),
        ("max_torque",         "最大转矩",     "N.m",  "如 150"),
        ("starting_current",   "起动电流",     "A",    "如 70"),
        ("temperature_rise",   "温升",         "K",    "如 75"),
        ("noise_level",        "噪声水平",     "dB",   "如 65"),
        ("vibration_level",    "振动等级",     "",     "如 A/B/C"),
        # ═══ 10. 电磁参数 (Electromagnetic) ═══
        ("stator_tooth_flux",  "定子齿磁密",   "T",    "如 1.5"),
        ("stator_yoke_flux",   "定子轭磁密",   "T",    "如 1.2"),
        ("rotor_yoke_flux",    "转子轭磁密",   "T",    "如 1.0"),
        ("air_gap_flux",       "气隙磁密",     "T",    "如 0.7"),
        ("current_density",    "电流密度",     "A/mm2","如 5.5"),
        # ═══ 11. 损耗参数 (Losses) ═══
        ("copper_loss",        "铜耗",         "W",    "如 300"),
        ("iron_loss",          "铁耗",         "W",    "如 150"),
        ("total_loss",         "总损耗",       "W",    "如 500"),
        ("stray_loss",         "杂散损耗",     "W",    "如 40"),
    ]

    _INT_FIELDS = {"poles", "phase", "rated_voltage", "rated_freq", "rated_speed",
                   "slots_stator", "slots_rotor", "turns_per_coil", "parallel_branches",
                   "conductors_per_slot", "strands", "coil_pitch", "stator_skew",
                   "reference_speed", "lamination_sectors", "winding_layer"}
    _FLOAT_FIELDS = {"rated_power", "rated_current", "efficiency", "power_factor",
                     "operating_temp",
                     "stator_od", "stator_id", "core_length", "rotor_od", "rotor_id",
                     "rotor_core_length", "air_gap",
                     "shaft_dia", "tooth_width", "yoke_thickness", "shaft_length",
                     "stator_hs0", "stator_hs1", "stator_hs2", "stator_bs0", "stator_bs1",
                     "stator_bs2", "stator_rs", "stator_slot_ins",
                     "rotor_hs0", "rotor_hs1", "rotor_hs2",
                     "rotor_bs0", "rotor_bs1", "rotor_bs2",
                     "wire_dia", "wire_wrap", "rotor_bar_width", "rotor_bar_height",
                     "end_ring_width", "end_ring_height",
                     "starting_torque", "max_torque", "starting_current",
                     "noise_level", "temperature_rise",
                     "stator_tooth_flux", "stator_yoke_flux", "rotor_yoke_flux",
                     "air_gap_flux", "current_density",
                     "copper_loss", "iron_loss", "total_loss", "friction_loss",
                     "wind_loss", "stray_loss"}
    _STRING_FIELDS = {"insulation_class", "protection_class", "cooling_method", "motor_type",
                      "load_type",
                      "stator_slot_type", "rotor_slot_type",
                      "rotor_cast", "rotor_double_cage", "rotor_deep_bar", "rotor_wound",
                      "winding_type", "winding_pitch", "connection_type",
                      "rotor_winding_type", "rotor_bar_material", "end_ring_material",
                      "vibration_level", "stator_material", "rotor_material",
                      "conductor_material", "shaft_material"}

    # v4.01.023: VL 图像识别专用字段子集 (铭牌/手册图片上"肉眼可见"的参数)
    # 全量 _FIELD_DEFS 有 90 个, 其中定子/转子槽形几何、材料、损耗、磁密等绝大多数
    # 在图片上不可见, 硬塞进 VL 提示词只会撑大上下文、拉长生成、诱发幻觉。
    # 这里只保留"铭牌必含 + 手册/图纸页可能出现的"字段, 让模型聚焦可见信息;
    # 其余字段由后处理闭环 (型号推断 + 物理校验) 补全, 与文本通道口径一致。
    # 若图片中某字段不可见, 模型按规则 5 直接跳过, 不会输出, 故多列无害。
    _VL_FIELD_SUBSET = [
        # 1. 通用 (铭牌必含)
        "motor_type", "rated_power", "rated_voltage", "rated_current", "rated_speed",
        "rated_freq", "poles", "phase", "operating_temp", "load_type",
        "insulation_class", "protection_class", "cooling_method",
        # 9. 性能 (铭牌/手册)
        "efficiency", "power_factor", "starting_torque", "max_torque",
        "starting_current", "temperature_rise", "noise_level", "vibration_level",
        # 6. 绕组/接法
        "winding_type", "connection_type",
        # 2/4. 定子/转子结构 (手册/图纸页常出现)
        "stator_od", "stator_id", "core_length", "slots_stator",
        "rotor_od", "rotor_id", "rotor_core_length", "slots_rotor",
        "rotor_cast", "rotor_wound", "rotor_double_cage", "rotor_deep_bar",
    ]

    # ── 维度4: 参数优先级 (敏感性分析) ──
    # 高: 对电磁仿真结果影响最大, 必须精确
    # 中: 影响中等, 可取标准值
    # 低: 影响较小, 可估算或默认
    _PRIORITY_MAP = {
        # 1. 通用参数
        "poles":            "high",
        "rated_power":      "high",
        "rated_voltage":    "high",
        "rated_speed":      "high",
        "rated_freq":       "medium",
        "phase":            "low",
        "motor_type":       "medium",
        "operating_temp":   "low",
        "load_type":        "low",
        "insulation_class": "low",
        "protection_class": "low",
        "cooling_method":   "low",
        # 2. 定子参数
        "stator_od":        "high",
        "stator_id":        "high",
        "core_length":      "high",
        "slots_stator":     "high",
        "lamination_sectors":"low",
        "stator_skew":      "medium",
        # 3. 定子槽形参数
        "stator_slot_type": "medium",
        "stator_hs0":       "medium",
        "stator_hs1":       "medium",
        "stator_hs2":       "medium",
        "stator_bs0":       "medium",
        "stator_bs1":       "medium",
        "stator_bs2":       "medium",
        "stator_rs":        "medium",
        "stator_slot_ins":  "low",
        # 4. 转子参数
        "rotor_od":         "high",
        "rotor_id":         "medium",
        "rotor_core_length":"medium",
        "slots_rotor":      "high",
        "rotor_cast":       "medium",
        "rotor_double_cage":"low",
        "rotor_deep_bar":   "low",
        "rotor_wound":      "low",
        # 5. 转子槽形参数
        "rotor_slot_type":  "medium",
        "rotor_hs0":        "medium",
        "rotor_hs1":        "medium",
        "rotor_hs2":        "medium",
        "rotor_bs0":        "medium",
        "rotor_bs1":        "medium",
        "rotor_bs2":        "medium",
        # 6. 绕组参数
        "winding_type":     "high",
        "winding_layer":    "medium",
        "parallel_branches":"high",
        "conductors_per_slot":"high",
        "turns_per_coil":   "high",
        "strands":          "medium",
        "wire_dia":         "high",
        "wire_wrap":        "low",
        "winding_pitch":    "medium",
        "coil_pitch":       "medium",
        "connection_type":  "medium",
        "rotor_winding_type":"medium",
        "rotor_bar_material":"medium",
        "end_ring_material":"medium",
        "rotor_bar_width":  "low",
        "rotor_bar_height": "low",
        "end_ring_width":   "low",
        "end_ring_height":  "low",
        # 7. 转轴参数
        "shaft_dia":        "medium",
        "shaft_length":     "low",
        "friction_loss":    "low",
        "wind_loss":        "low",
        "reference_speed":  "medium",
        # 8. 材料参数
        "stator_material":  "high",
        "rotor_material":   "high",
        "conductor_material":"medium",
        "shaft_material":   "low",
        # 9. 性能参数
        "rated_current":    "medium",
        "efficiency":       "medium",
        "power_factor":     "medium",
        "max_torque":       "medium",
        "starting_torque":  "low",
        "starting_current": "low",
        "temperature_rise": "medium",
        "noise_level":      "low",
        "vibration_level":  "low",
        # 10. 电磁参数
        "stator_tooth_flux":"high",
        "stator_yoke_flux": "high",
        "rotor_yoke_flux":  "medium",
        "air_gap_flux":     "high",
        "current_density":  "medium",
        # 11. 损耗参数
        "copper_loss":      "low",
        "iron_loss":        "low",
        "total_loss":       "low",
        "stray_loss":       "low",
    }

    # ── 维度1: 电机系列知识库 (消除"需查阅手册"的不确定性) ──
    _SERIES_KB = {
        "Y":   {"name": "Y系列",     "insulation": "B", "protection": "IP44", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "Y2":  {"name": "Y2系列",    "insulation": "F", "protection": "IP54", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "Y3":  {"name": "Y3系列",    "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YE2": {"name": "YE2高效",   "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YE3": {"name": "YE3超高效",  "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YE4": {"name": "YE4超超高效", "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YX3": {"name": "YX3高效",   "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YB":  {"name": "YB隔爆",    "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YB2": {"name": "YB2隔爆",   "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YB3": {"name": "YB3隔爆",   "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YBX3":{"name": "YBX3隔爆",  "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YVF": {"name": "YVF变频",   "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YVFP":{"name": "YVFP变频",  "insulation": "F", "protection": "IP55", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YS":  {"name": "YS系列",    "insulation": "F", "protection": "IP54", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
        "YC":  {"name": "YC电磁调速","insulation": "F", "protection": "IP44", "cooling": "IC411", "freq": 50, "phase": 3, "voltage": 380},
    }

    # ── 维度2: 极数与转速物理关系 (50Hz) ──
    # 同步转速 = 120 × f / p, 额定转速 ≈ 同步转速 × (1 - 滑差率2~5%)
    _POLE_SPEED_MAP_50HZ = {
        2: (2900, 3000), 4: (1440, 1500), 6: (960, 1000),
        8: (720, 750), 10: (580, 600), 12: (480, 500),
    }

    # ── 维度2: 效率合理范围 (按功率等级) ──
    # (功率上限kW, IE1下限%, IE2下限%, IE3下限%)
    _EFF_RANGE_BY_POWER = [
        (1.1, 70, 75, 80), (5.5, 75, 82, 86), (11, 80, 87, 90),
        (22, 84, 89, 92), (55, 88, 92, 94), (132, 91, 93, 95), (9999, 92, 94, 96),
    ]

    def __init__(self):
        super().__init__(name="OCRRecognizeAgent", description="OCR智能识别铭牌参数")
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换
        self._llm = None
        # v4.00.008: 结果缓存 (text_hash → result, 30分钟TTL)
        import hashlib
        self._hash_fn = hashlib.md5
        self._result_cache = {}  # {cache_key: (timestamp, result)}
        self._CACHE_TTL = 1800   # 30 minutes
        # v4.00.008: 统计追踪 (供数据飞轮消费)
        self._stats = {
            "total_requests": 0,
            "ai_success": 0,
            "ai_fallback": 0,
            "cache_hits": 0,
            "total_elapsed": 0.0,
            "total_params_extracted": 0,
        }
        # v4.00.008: Ollama 健康状态缓存 (避免每次请求都预检)
        self._ollama_healthy = None
        self._ollama_check_ts = 0
        # v4.01.024: VL 多模态识别独立模型与可用性缓存
        # 模型名改为动态属性 _vl_model_name (见下方 property), 管理端改配置即时生效
        self._vl_available: Optional[bool] = None
        self._vl_check_ts = 0
        self._vl_last_model: str = ""

    @property
    def _vl_model_name(self) -> str:
        """当前 VL 多模态模型名 (动态读取, 管理端切换即时生效)。

        优先级: 环境变量 VL_MODEL_NAME > 管理端 OCRImageRecognizeAgent
                > 默认 qwen3-vl:2b-instruct
        模型名变化时自动作废可用性缓存, 避免沿用旧模型的预检结果。
        """
        env = os.getenv("VL_MODEL_NAME")
        name = env if env else (get_vl_model() or "qwen3-vl:2b-instruct")
        if name != self._vl_last_model:
            self._vl_last_model = name
            self._vl_available = None
            self._vl_check_ts = 0
        return name

    @property
    def _vl_timeout(self) -> int:
        """VL 识别等待上限 (秒), 默认 300 (5 分钟)"""
        env = os.getenv("VL_TIMEOUT")
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        return get_vl_timeout()

    def _get_llm(self):
        """每次调用时动态创建 LLM, 经网关路由 (管理端模型配置即时生效 + 云端自动降级)。
        num_ctx=8192/num_predict=4096 保留全字段识别所需上下文。"""
        if not _LLM_AVAILABLE:
            return None
        return super()._get_llm_or_cloud(temperature=0.1, num_ctx=8192, num_predict=4096)

    def _check_ollama_health(self) -> bool:
        """v4.01.007: Ollama 健康预检 — 仅检查服务是否在线, 不阻塞等待模型加载

        v4.01.007 改进:
          - 移除 15s 阻塞 warmup (T600 上模型加载需要 85s, 15s 必然超时 → 误判为降级)
          - 仅检查 /api/ps 是否响应 (3s timeout)
          - 模型未加载时仍返回 True, 让 LLM 调用自身 (timeout=120s) 处理加载
          - 后台预热线程在 Flask 启动时已开始, 大多数情况模型已就绪
        """
        import time as _time
        now = _time.time()
        if self._ollama_healthy is not None and (now - self._ollama_check_ts) < 30:
            return self._ollama_healthy
        try:
            import requests as _requests
            base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            r = _requests.get(f"{base_url}/api/ps", timeout=3)
            if r.status_code != 200:
                self._ollama_healthy = False
                self._ollama_check_ts = now
                logger.warning("[OCRRecognizeAgent] Ollama /api/ps 返回 %d, 跳过 LLM", r.status_code)
                return False
            # 检查模型是否已加载 (仅用于日志, 不阻塞)
            loaded_models = [m.get("name", "") for m in r.json().get("models", [])]
            target_model = get_agent_model("OCRRecognizeAgent")
            if target_model in loaded_models:
                logger.info("[OCRRecognizeAgent] 模型 %s 已在 VRAM 中, LLM 调用将很快", target_model)
            else:
                logger.info("[OCRRecognizeAgent] 模型 %s 未加载, LLM 调用将包含加载时间 (~85s on T600)", target_model)
            self._ollama_healthy = True
            self._ollama_check_ts = now
            return True
        except Exception as e:
            self._ollama_healthy = False
            self._ollama_check_ts = now
            logger.warning("[OCRRecognizeAgent] Ollama 健康预检失败: %s", e)
            return False

    def _prewarm_model(self):
        """v4.01.007: 后台预热模型 — 在 Flask 启动时调用, 避免首次 OCR 等待 85s 加载"""
        import threading
        def _warm():
            try:
                import requests as _requests
                base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
                target_model = get_agent_model("OCRRecognizeAgent")
                logger.info("[OCRRecognizeAgent] 后台预热模型 %s ...", target_model)
                r = _requests.post(
                    f"{base_url}/api/generate",
                    json={"model": target_model, "prompt": "ok", "stream": False},
                    timeout=120,
                )
                if r.status_code == 200:
                    logger.info("[OCRRecognizeAgent] 模型预热完成, 已加载到 VRAM")
                else:
                    logger.warning("[OCRRecognizeAgent] 模型预热返回 %d", r.status_code)
            except Exception as e:
                logger.warning("[OCRRecognizeAgent] 模型预热失败: %s", e)
        t = threading.Thread(target=_warm, daemon=True)
        t.start()

    def get_stats(self) -> dict:
        """v4.00.008: 获取识别统计 (供数据飞轮/运营分析师消费)"""
        s = self._stats
        total = s["total_requests"]
        return {
            "total_requests": total,
            "ai_success": s["ai_success"],
            "ai_fallback": s["ai_fallback"],
            "cache_hits": s["cache_hits"],
            "avg_elapsed": round(s["total_elapsed"] / total, 2) if total else 0,
            "avg_params": round(s["total_params_extracted"] / total, 1) if total else 0,
            "ai_success_rate": round(s["ai_success"] / total * 100, 1) if total else 0,
            "cache_size": len(self._result_cache),
        }

    def run(self, text: str, regex_params: dict = None) -> dict:
        """从文本中智能提取电机铭牌参数 (v4.00.008 六步优化版)

        v4.00.008 改进:
          - Step 0: Ollama 健康预检 (避免60秒空等)
          - Step 0.5: 结果缓存 (文本hash, 30分钟TTL)
          - 统计追踪 (供数据飞轮消费)

        Args:
            text: OCR 原始文本或用户输入文本
            regex_params: 正则已提取的参数 (可选, 用于合并)

        Returns:
            {
                "params": {field: {value, unit, label, raw_match, source, priority, confidence}},
                "ai_count": int,
                "regex_count": int,
                "inferred_count": int,
                "validation": [{field, status, message}],
                "warnings": [str],
                "elapsed": float,           # v4.00.008: 总耗时秒
                "model_status": str,        # v4.00.008: "healthy"|"unhealthy"|"cached"
            }
        """
        import time as _time
        t_start = _time.time()
        self._stats["total_requests"] += 1

        regex_params = regex_params or {}
        ai_params = {}

        # v4.01.008: 去除缓存 — 每次都重新调用大模型识别
        now = _time.time()

        # ── Step 0: Ollama 健康预检 ──
        ollama_healthy = self._check_ollama_health()
        model_status = "healthy" if ollama_healthy else "unhealthy"

        # Step 1: LLM 智能提取 (本地需 Ollama 健康; 云端模型不依赖 Ollama)
        if ollama_healthy or self._is_cloud_configured():
            llm = self._get_llm_or_cloud(temperature=0.1)
            if llm and text.strip():
                ai_params = self._llm_extract(text, llm)
                if ai_params:
                    self._stats["ai_success"] += 1
                else:
                    self._stats["ai_fallback"] += 1
                    # v4.01.007: LLM 调用完成但返回空 → 可能是超时或解析失败
                    model_status = "no_result"
            else:
                if not llm:
                    logger.info("[OCRRecognizeAgent] LLM 不可用, 仅使用正则结果")
                elif not text.strip():
                    logger.info("[OCRRecognizeAgent] 文本为空, 跳过")
                self._stats["ai_fallback"] += 1
                model_status = "no_llm"
        else:
            logger.info("[OCRRecognizeAgent] Ollama 不健康, 跳过 LLM, 直接走正则")
            self._stats["ai_fallback"] += 1
            model_status = "unhealthy"

        # Step 2: 合并 — 正则为基础, AI覆盖同字段+新增字段
        merged = {}
        for field, info in regex_params.items():
            merged[field] = {
                "value": info.get("value", ""),
                "unit": info.get("unit", ""),
                "label": info.get("label", field),
                "raw_match": info.get("raw_match", ""),
                "source": "regex",
            }
        for field, info in ai_params.items():
            merged[field] = {
                "value": info.get("value", ""),
                "unit": info.get("unit", ""),
                "label": info.get("label", field),
                "raw_match": info.get("raw_match", ""),
                "source": "ai",
            }

        # Step 3: 维度1 — 从型号推断缺失参数 (系列知识库)
        inferred = self._infer_from_model(merged)
        inferred_count = 0
        for field, (value, reason) in inferred.items():
            if field not in merged:
                field_def = next((f for f in self._FIELD_DEFS if f[0] == field), None)
                merged[field] = {
                    "value": value,
                    "unit": field_def[2] if field_def else "",
                    "label": field_def[1] if field_def else field,
                    "raw_match": "",
                    "source": "inferred",
                    "inferred_reason": reason,
                }
                inferred_count += 1
            elif field in merged and merged[field].get("source") == "regex":
                # 正则提取的值可能不完整 (如只提取了数字部分), 用系列标准值补全
                pass

        # Step 4: 维度2 — 物理一致性校验
        validation, corrections = self._validate_consistency(merged)
        for corr in corrections:
            field = corr["field"]
            if field in merged:
                old_val = merged[field]["value"]
                merged[field]["value"] = corr["new_value"]
                merged[field]["correction"] = f"校验修正: {old_val} → {corr['new_value']} ({corr['reason']})"
                logger.info("[OCRRecognizeAgent] 校验修正 %s: %s → %s (%s)",
                            field, old_val, corr["new_value"], corr["reason"])

        # Step 5: 维度4 — 附加优先级和置信度
        for field, info in merged.items():
            info["priority"] = self._PRIORITY_MAP.get(field, "low")
            info["confidence"] = self._calc_confidence(field, info, merged)

        # v4.00.010: 过滤非 PARAM_FIELDS 白名单且非 motor_type 的非标字段
        # 避免"识别成功但填入不到表单"的字段泄漏
        try:
            from Core.case_motor_db import MOTOR_PARAM_FIELDS
            whitelist = {f[0] for f in MOTOR_PARAM_FIELDS} | {"motor_type"}
            dropped = [k for k in list(merged.keys()) if k not in whitelist]
            if dropped:
                logger.info("[OCRRecognizeAgent] 过滤非白名单字段: %s", dropped)
                for k in dropped:
                    merged.pop(k, None)
        except Exception as _e:
            logger.debug("[OCRRecognizeAgent] 白名单过滤跳过: %s", _e)

        # 收集警告
        warnings = [v["message"] for v in validation if v["status"] == "warning"]

        elapsed = round(_time.time() - t_start, 3)

        logger.info("[OCRRecognizeAgent] 提取完成: 正则%d + AI%d + 推断%d = %d参数, %d个警告, %.2fs, model=%s",
                    len(regex_params), len(ai_params), inferred_count,
                    len(merged), len(warnings), elapsed, model_status)

        # ── 统计更新 ──
        self._stats["total_elapsed"] += elapsed
        self._stats["total_params_extracted"] += len(merged)

        result = {
            "params": merged,
            "ai_count": len(ai_params),
            "regex_count": len(regex_params),
            "inferred_count": inferred_count,
            "validation": validation,
            "warnings": warnings,
            "elapsed": elapsed,
            "model_status": model_status,
        }

        # v4.01.008: 去除缓存写入 — 每次都重新识别, 不缓存结果

        return result

    # ══════════════════════════════════════════════════════════════
    # 维度1: 电机系列知识库 — 从型号推断参数
    # ══════════════════════════════════════════════════════════════

    def _infer_from_model(self, merged: dict) -> dict:
        """从电机型号字符串推断缺失参数

        解析型号如 "Y2-132S-4" → 系列=Y2, 机座号=132, 极数=4
        然后用系列知识库补全绝缘/防护/冷却/频率/相数等标准值
        """
        inferred = {}
        motor_type = merged.get("motor_type", {}).get("value", "")
        if not motor_type or not isinstance(motor_type, str):
            return inferred

        import re as _re

        # 解析型号: 前缀(系列, 可含数字如Y2/YE3/YBX3) + 机座号 + 极数
        # 常见格式: Y2-132S-4, YE3-160M-6, YB2-180L-4, Y-250M-8
        m = _re.match(r'^([A-Z]+\d*)\s*[-]?\s*(\d+)\s*([SMLE])?\s*[-]?\s*(\d+)', motor_type, _re.IGNORECASE)
        if not m:
            # 尝试无机座号的格式: Y2-4, YE3-6
            m2 = _re.match(r'^([A-Z]+\d*)\s*[-]?\s*(\d+)$', motor_type, _re.IGNORECASE)
            if m2:
                series = m2.group(1).upper()
                poles = int(m2.group(2))
                # 用系列知识库补全
                kb = self._match_series(series)
                if kb:
                    for field, key, label in [
                        ("insulation_class", "insulation", "绝缘等级"),
                        ("protection_class", "protection", "防护等级"),
                        ("cooling_method", "cooling", "冷却方式"),
                        ("rated_freq", "freq", "额定频率"),
                        ("phase", "phase", "相数"),
                        ("rated_voltage", "voltage", "额定电压"),
                    ]:
                        if field not in merged:
                            inferred[field] = (kb[key], f"{kb['name']}系列标准{label}: {kb[key]}")
                if poles not in merged.get("poles", {}).get("value", 0):
                    inferred["poles"] = (poles, f"从型号{motor_type}末位推断极数={poles}")
            return inferred

        series = m.group(1).upper()
        frame_size = m.group(2)
        frame_code = m.group(3) or ""
        poles_from_model = int(m.group(4))

        # 从型号末位推断极数
        if "poles" not in merged:
            inferred["poles"] = (poles_from_model, f"从型号{motor_type}末位{poles_from_model}推断极数")

        # 从机座号推断额定功率范围 (粗略, Y系列标准)
        power_hint = self._frame_to_power(frame_size, poles_from_model)
        if power_hint and "rated_power" not in merged:
            inferred["rated_power"] = (power_hint, f"Y系列机座号{frame_size}/{poles_from_model}极标准功率: {power_hint}kW")

        # 从极数推断额定转速 (50Hz)
        if "rated_speed" not in merged and poles_from_model in self._POLE_SPEED_MAP_50HZ:
            rated_speed, sync_speed = self._POLE_SPEED_MAP_50HZ[poles_from_model]
            inferred["rated_speed"] = (rated_speed, f"{poles_from_model}极50Hz额定转速≈{rated_speed}rpm (同步{sync_speed})")

        # 用系列知识库补全标准参数
        kb = self._match_series(series)
        if kb:
            for field, key, label in [
                ("insulation_class", "insulation", "绝缘等级"),
                ("protection_class", "protection", "防护等级"),
                ("cooling_method", "cooling", "冷却方式"),
                ("rated_freq", "freq", "额定频率"),
                ("phase", "phase", "相数"),
                ("rated_voltage", "voltage", "额定电压"),
            ]:
                if field not in merged:
                    inferred[field] = (kb[key], f"{kb['name']}系列标准{label}: {kb[key]}")

        logger.info("[OCRRecognizeAgent] 型号推断: %s → 系列=%s, 机座=%s, 极数=%d, 推断%d个参数",
                    motor_type, series, frame_size, poles_from_model, len(inferred))
        return inferred

    def _match_series(self, prefix: str) -> dict:
        """模糊匹配系列知识库"""
        prefix = prefix.upper().strip()
        # 精确匹配
        if prefix in self._SERIES_KB:
            return self._SERIES_KB[prefix]
        # 前缀匹配 (如 "YE3-" → "YE3")
        for key, val in self._SERIES_KB.items():
            if prefix.startswith(key) or key.startswith(prefix):
                return val
        return None

    def _frame_to_power(self, frame: str, poles: int) -> float:
        """Y系列机座号 → 标准额定功率 (kW)

        粗略映射, 实际功率需查手册; 仅用于参数补全提示
        """
        try:
            f = int(frame)
        except (ValueError, TypeError):
            return None
        # Y系列标准机座号-功率对照表 (4极为例)
        # 参考: GB/T 28575
        _frame_power_4p = {
            80: 0.55, 90: 1.1, 100: 2.2, 112: 4.0, 132: 5.5,
            160: 11, 180: 22, 200: 30, 225: 45, 250: 55,
            280: 90, 315: 110, 355: 250,
        }
        _frame_power_2p = {
            80: 0.75, 90: 1.5, 100: 3.0, 112: 5.5, 132: 7.5,
            160: 15, 180: 30, 200: 45, 225: 75, 250: 90,
        }
        _frame_power_6p = {
            90: 0.75, 100: 1.5, 112: 2.2, 132: 3.0, 160: 7.5,
            180: 15, 200: 18.5, 225: 30, 250: 37, 280: 55,
        }
        _frame_power_8p = {
            132: 2.2, 160: 5.5, 180: 11, 200: 15, 225: 22,
            250: 30, 280: 45, 315: 75,
        }
        if poles == 2:
            return _frame_power_2p.get(f)
        elif poles == 4:
            return _frame_power_4p.get(f)
        elif poles == 6:
            return _frame_power_6p.get(f)
        elif poles == 8:
            return _frame_power_8p.get(f)
        return None

    # ══════════════════════════════════════════════════════════════
    # 维度2: 物理一致性校验
    # ══════════════════════════════════════════════════════════════

    def _validate_consistency(self, merged: dict) -> tuple:
        """交叉参数物理一致性校验

        Returns:
            (validation_list, corrections_list)
            validation_list: [{field, status, message}]
            corrections_list: [{field, new_value, reason}]
        """
        validations = []
        corrections = []

        def _get_val(field):
            info = merged.get(field)
            if not info:
                return None
            v = info.get("value")
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        poles = _get_val("poles")
        speed = _get_val("rated_speed")
        power = _get_val("rated_power")
        current = _get_val("rated_current")
        voltage = _get_val("rated_voltage")
        eff = _get_val("efficiency")
        pf = _get_val("power_factor")
        freq = _get_val("rated_freq") or 50

        # ── 校验1: 极数 ↔ 额定转速 ──
        if poles and speed:
            sync_speed = 120 * freq / poles
            slip = (sync_speed - speed) / sync_speed
            if slip < 0 or slip > 0.10:
                validations.append({
                    "field": "rated_speed",
                    "status": "error",
                    "message": f"极数{int(poles)}极{int(freq)}Hz同步转速={int(sync_speed)}rpm, "
                               f"但额定转速={int(speed)}rpm, 滑差率{slip*100:.1f}%异常(正常2-5%)"
                })
            elif slip > 0.07:
                validations.append({
                    "field": "rated_speed",
                    "status": "warning",
                    "message": f"滑差率{slip*100:.1f}%偏高(正常2-5%), 请确认极数和转速"
                })
            else:
                validations.append({
                    "field": "rated_speed",
                    "status": "ok",
                    "message": f"极数{int(poles)}极 → 同步{int(sync_speed)}rpm, 额定{int(speed)}rpm, 滑差{slip*100:.1f}%正常"
                })

        # ── 校验2: 功率 ↔ 电流 ↔ 电压 (P = √3 × U × I × cosφ × η) ──
        if power and current and voltage and eff and pf:
            # 计算理论电流
            eff_dec = eff / 100.0
            i_calc = power * 1000 / (math.sqrt(3) * voltage * pf * eff_dec)
            ratio = current / i_calc if i_calc > 0 else 0
            if ratio < 0.7 or ratio > 1.3:
                validations.append({
                    "field": "rated_current",
                    "status": "warning",
                    "message": f"功率{power}kW/电压{int(voltage)}V/效率{eff}%/cosφ={pf} "
                               f"推算电流≈{i_calc:.1f}A, 实际{current:.1f}A, 偏差{(ratio-1)*100:.0f}%"
                })
            else:
                validations.append({
                    "field": "rated_current",
                    "status": "ok",
                    "message": f"电流校验通过: 推算{i_calc:.1f}A vs 实际{current:.1f}A (偏差{(ratio-1)*100:.0f}%)"
                })

        # ── 校验3: 效率范围合理性 ──
        if power and eff:
            for max_p, ie1, ie2, ie3 in self._EFF_RANGE_BY_POWER:
                if power <= max_p:
                    if eff < ie1 - 5 or eff > 100:
                        validations.append({
                            "field": "efficiency",
                            "status": "error",
                            "message": f"效率{eff}%异常: {power}kW电机IE1下限{ie1}%, 当前值超出合理范围"
                        })
                    elif eff < ie1:
                        validations.append({
                            "field": "efficiency",
                            "status": "warning",
                            "message": f"效率{eff}%偏低: {power}kW电机IE1下限{ie1}%, 请确认是否正确"
                        })
                    else:
                        eff_class = "IE3" if eff >= ie3 else ("IE2" if eff >= ie2 else "IE1")
                        validations.append({
                            "field": "efficiency",
                            "status": "ok",
                            "message": f"效率{eff}%合理 ({eff_class}级, {power}kW范围{ie1}-{ie3}%)"
                        })
                    break

        # ── 校验4: 功率因数范围 ──
        if pf is not None:
            if pf > 1.0:
                # 可能是百分比形式
                if pf > 50:
                    new_pf = round(pf / 100, 2)
                    corrections.append({
                        "field": "power_factor",
                        "new_value": new_pf,
                        "reason": f"功率因数{pf}>1, 疑似百分比, 修正为{new_pf}"
                    })
                else:
                    validations.append({
                        "field": "power_factor",
                        "status": "error",
                        "message": f"功率因数{pf}>1, 物理不可能 (正常0.7-0.95)"
                    })
            elif pf < 0.5 and pf > 0:
                validations.append({
                    "field": "power_factor",
                    "status": "warning",
                    "message": f"功率因数{pf}偏低 (正常0.7-0.95), 请确认"
                })
            else:
                validations.append({
                    "field": "power_factor",
                    "status": "ok",
                    "message": f"功率因数{pf}合理"
                })

        # ── 校验5: 极数合理性 ──
        if poles:
            if poles not in [2, 4, 6, 8, 10, 12]:
                validations.append({
                    "field": "poles",
                    "status": "warning",
                    "message": f"极数{int(poles)}非标准值 (常见: 2/4/6/8/10/12)"
                })

        # ── 校验6: 电压常见值 ──
        if voltage:
            common_voltages = [220, 380, 400, 415, 440, 660, 690, 1140, 3000, 3300, 6000, 6600, 10000]
            if voltage not in common_voltages and voltage < 50:
                # 可能是 kV
                if 0.22 <= voltage <= 10:
                    corrections.append({
                        "field": "rated_voltage",
                        "new_value": int(voltage * 1000),
                        "reason": f"电压{voltage}疑似kV单位, 修正为{int(voltage*1000)}V"
                    })

        return validations, corrections

    # ══════════════════════════════════════════════════════════════
    # 维度4: 置信度计算
    # ══════════════════════════════════════════════════════════════

    def _calc_confidence(self, field: str, info: dict, merged: dict) -> int:
        """计算单个参数的置信度 (0-100)

        置信度因素:
          - source: ai=80, regex=75, inferred=60
          - 校验状态: ok +10, warning -10, error -20
          - 交叉验证一致性: +5~15
        """
        source = info.get("source", "regex")
        base = {"ai": 80, "regex": 75, "inferred": 60, "vl": 82}.get(source, 70)

        # 交叉验证加分
        if field == "rated_speed" and "poles" in merged:
            try:
                p = int(merged["poles"]["value"])
                s = float(info["value"])
                sync = 120 * 50 / p
                slip = (sync - s) / sync
                if 0 < slip < 0.07:
                    base += 15  # 转速与极数一致, 高置信
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        if field == "rated_current" and "rated_power" in merged and "rated_voltage" in merged:
            try:
                p = float(merged["rated_power"]["value"])
                u = float(merged["rated_voltage"]["value"])
                i = float(info["value"])
                i_calc = p * 1000 / (math.sqrt(3) * u * 0.85 * 0.85)
                if 0.7 < i / i_calc < 1.3:
                    base += 10
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        if field == "efficiency" and "rated_power" in merged:
            try:
                pw = float(merged["rated_power"]["value"])
                ev = float(info["value"])
                for max_p, ie1, _, ie3 in self._EFF_RANGE_BY_POWER:
                    if pw <= max_p:
                        if ie1 <= ev <= ie3 + 2:
                            base += 10
                        break
            except (ValueError, TypeError):
                pass

        # 推断参数降低置信度
        if source == "inferred":
            base = min(base, 65)

        return max(30, min(98, base))

    # ══════════════════════════════════════════════════════════════
    # VL 多模态识别 (v4.01.023: qwen3-vl:2b 专用图像识别智能体)
    # ══════════════════════════════════════════════════════════════

    def get_vl_model_name(self) -> str:
        """当前 VL 多模态识别使用的模型名 (默认 qwen3-vl:2b)"""
        return self._vl_model_name

    def check_vl_available(self, force: bool = False) -> bool:
        """
        v4.01.023: 检查 VL 模型是否在 Ollama 本地模型库中存在。
        这是 "VL: 不可用" 状态判定的依据 —— 服务在线但模型没 pull 时,
        应给出明确提示, 而不是调用时报错。
        """
        import time as _time
        now = _time.time()
        if (not force and self._vl_available is not None
                and (now - self._vl_check_ts) < 60):
            return self._vl_available
        try:
            import requests as _requests
            base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            r = _requests.get(f"{base_url}/api/tags", timeout=5)
            if r.status_code != 200:
                self._vl_available = False
                self._vl_check_ts = now
                return False
            local_models = {m.get("name", "") for m in r.json().get("models", [])}
            # Ollama tag 可能带 :latest 或不带, 兼容比较
            target = self._vl_model_name
            self._vl_available = (
                target in local_models
                or target.split(":")[0] in {m.split(":")[0] for m in local_models}
            )
            self._vl_check_ts = now
            logger.info("[OCRRecognizeAgent] VL 模型 %s 可用性: %s",
                        target, self._vl_available)
            return self._vl_available
        except Exception as e:
            logger.warning("[OCRRecognizeAgent] VL 可用性检查失败: %s", e)
            self._vl_available = False
            self._vl_check_ts = now
            return False

    def get_vl_model_info(self) -> dict:
        """返回 VL 模型元信息, 供客户端 /api/case-motor/ocr-vl 状态展示"""
        return {
            "model": self._vl_model_name,
            "available": self.check_vl_available(),
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        }

    def vl_recognize(self, image_bytes: bytes, image_format: str = "JPEG") -> dict:
        """v4.01.023: 多模态 VL 识别 — 直接识别铭牌/手册图片 (qwen3-vl:2b)

        与 run() (文本 OCR + LLM) 互补: 图片 → VL 结构化提取 → 知识库补全
        → 物理校验 → 置信度评估。保留与 run() 相同的后处理闭环, 仅在
        "从文本提取" 这一步替换为 "从图片视觉提取"。

        v4.01.023 起, 实际执行体已抽取为独立的 **VLImageAgent (图像识别智能体)**,
        本方法转为委托调用, 以保持既有 API / 调用方零改动。
        独立成 Agent 的收益: 图像预处理(等比压缩)、模型独立预检与统计、
        多图批量识别 run_batch()。

        模型配置:
            默认 VL_MODEL_NAME=qwen3-vl:2b-instruct (T600 4GB 可跑), 可通过环境变量覆盖。

        Returns: 同 run() 结构 (source 统一为 'vl')
        """
        try:
            from Agents.vl_image_agent import get_vl_image_agent
            agent = get_vl_image_agent(ocr_agent=self)
            result = agent.run(image_bytes=image_bytes, image_format=image_format)
            # 统计口径与文本通道保持一致
            self._stats["total_requests"] += 1
            if result.get("model_status") == "healthy":
                self._stats["ai_success"] += 1
            else:
                self._stats["ai_fallback"] += 1
            return result
        except Exception as e:
            logger.warning("[OCRRecognizeAgent] VLImageAgent 不可用, 回退内置实现: %s", e)
            return self._vl_recognize_legacy(image_bytes, image_format)

    def _vl_recognize_legacy(self, image_bytes: bytes, image_format: str = "JPEG") -> dict:
        """v4.01.023 之前的内置 VL 实现 — 仅在 VLImageAgent 导入失败时兜底"""
        import time as _time
        t_start = _time.time()
        self._stats["total_requests"] += 1

        # v4.01.023: VL 模型可用性检查 (服务在线 + 模型已 pull)
        ollama_healthy = self._check_ollama_health()
        vl_available = self.check_vl_available()
        if not ollama_healthy:
            logger.warning("[OCRRecognizeAgent] Ollama 不健康, VL 识别跳过")
            self._stats["ai_fallback"] += 1
            model_status = "unhealthy"
            vl_params = {}
        elif not vl_available:
            logger.warning("[OCRRecognizeAgent] VL 模型 %s 未在本地安装, 跳过 VL 识别",
                           self._vl_model_name)
            self._stats["ai_fallback"] += 1
            model_status = "model_not_found"
            vl_params = {}
        else:
            vl_params = self._call_vl_ollama(image_bytes, image_format)
            if vl_params:
                self._stats["ai_success"] += 1
                model_status = "healthy"
            else:
                self._stats["ai_fallback"] += 1
                model_status = "no_result"

        # ── 后处理闭环: 与 run() 完全一致 ──
        merged = {}
        for field, info in vl_params.items():
            merged[field] = {
                "value": info.get("value", ""),
                "unit": info.get("unit", ""),
                "label": info.get("label", field),
                "raw_match": info.get("raw_match", ""),
                "source": "vl",
            }

        # 维度1 — 从型号推断缺失参数 (系列知识库)
        inferred = self._infer_from_model(merged)
        inferred_count = 0
        for field, (value, reason) in inferred.items():
            if field not in merged:
                field_def = next((f for f in self._FIELD_DEFS if f[0] == field), None)
                merged[field] = {
                    "value": value,
                    "unit": field_def[2] if field_def else "",
                    "label": field_def[1] if field_def else field,
                    "raw_match": "",
                    "source": "inferred",
                    "inferred_reason": reason,
                }
                inferred_count += 1

        # 维度2 — 物理一致性校验
        validation, corrections = self._validate_consistency(merged)
        for corr in corrections:
            field = corr["field"]
            if field in merged:
                old_val = merged[field]["value"]
                merged[field]["value"] = corr["new_value"]
                merged[field]["correction"] = f"校验修正: {old_val} → {corr['new_value']} ({corr['reason']})"

        # 维度4 — 优先级 + 置信度
        for field, info in merged.items():
            info["priority"] = self._PRIORITY_MAP.get(field, "low")
            info["confidence"] = self._calc_confidence(field, info, merged)

        # 白名单过滤 (与 run() 一致)
        try:
            from Core.case_motor_db import MOTOR_PARAM_FIELDS
            whitelist = {f[0] for f in MOTOR_PARAM_FIELDS} | {"motor_type"}
            dropped = [k for k in list(merged.keys()) if k not in whitelist]
            if dropped:
                logger.info("[OCRRecognizeAgent] VL 过滤非白名单字段: %s", dropped)
                for k in dropped:
                    merged.pop(k, None)
        except Exception as _e:
            logger.debug("[OCRRecognizeAgent] VL 白名单过滤跳过: %s", _e)

        warnings = [v["message"] for v in validation if v["status"] == "warning"]
        elapsed = round(_time.time() - t_start, 3)

        logger.info("[OCRRecognizeAgent] VL 识别完成: VL%d + 推断%d = %d参数, %.2fs, model=%s",
                    len(vl_params), inferred_count, len(merged), elapsed, model_status)

        return {
            "params": merged,
            "vl_count": len(vl_params),
            "regex_count": 0,
            "inferred_count": inferred_count,
            "validation": validation,
            "warnings": warnings,
            "elapsed": elapsed,
            "model_status": model_status,
            # v4.01.023: 把 VL 模型信息透传出去, 方便客户端解释 "VL: 不可用"
            "vl_model": self._vl_model_name,
            "vl_model_available": vl_available if ollama_healthy else False,
        }

    def _call_vl_ollama(self, image_bytes: bytes, image_format: str) -> dict:
        """调用 Ollama 多模态模型 (qwen3-vl:2b) 直接识别图片, 返回结构化参数 dict

        Args:
            image_bytes: 原始图片字节
            image_format: 图片格式 (JPEG/PNG/...), 仅用于日志
        Returns:
            {field: {value, unit, label, raw_match}}  (source 在 vl_recognize 中统一赋值)
        """
        import base64 as _b64
        import requests as _requests
        try:
            base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            vl_model = self._vl_model_name

            img_b64 = _b64.b64encode(image_bytes).decode("utf-8")

            # v4.01.023: 只用铭牌可见子集, 缩短上下文+生成, 防止 2B 视觉模型超时/幻觉
            use_subset = True
            system_prompt, field_list = self._build_vl_prompt(use_subset=use_subset)
            valid_fields = (
                set(self._VL_FIELD_SUBSET) if use_subset
                else {f[0] for f in self._FIELD_DEFS}
            )
            quoted_fields = ", ".join(f'"{n}"' for n in valid_fields)
            user_prompt = (
                "这是一张电机铭牌或技术手册的图片。请识别图片中可见的电机参数, "
                "只输出 JSON, 不要任何解释或 markdown。\n"
                '格式: {"params": [{"param": "字段英文名", "value": 值, "raw_match": "图中原文"}]}\n'
                f"param 必须是下列英文名之一: {quoted_fields}\n"
                "只输出图片中确实可见的参数, 不可见的字段不要输出。\n"
                "特别注意: 若图片含电机型号(如 Y2-200L-4 / YE3-160M-6 / 三相异步电动机), "
                "务必输出 motor_type=该型号; 若含极数(如末尾数字 4/6)或'极'字样, 务必输出 poles。"
            )

            # v4.01.023: qwen3-vl:2b 强制开启 thinking, 且 think 参数放到 options 里无效;
            # 正确做法: 顶层 "think": false + 尾部 assistant 预填 <think>\\n\\n</think>\\n\\n,
            # 二者缺一不可(单独 think:false 会输出空, 单独 prefill 仍会 thinking)。
            payload = {
                "model": vl_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt, "images": [img_b64]},
                    {"role": "assistant", "content": "<think>\n\n</think>\n\n"},
                ],
                "format": "json",
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1, "num_predict": 2048},
            }

            logger.info("[OCRRecognizeAgent] 调用 VL 模型 %s (%s, %dKB, 子集%d字段)...",
                        vl_model, image_format, len(image_bytes) // 1024, len(valid_fields))
            # v4.01.024: qwen3-vl:2b-instruct 单图约 12~50s, 等待上限 300s(5分钟); 可 VL_TIMEOUT 覆盖
            resp = _requests.post(
                f"{base_url}/api/chat", json=payload,
                timeout=self._vl_timeout
            )
            if resp.status_code != 200:
                logger.warning("[OCRRecognizeAgent] VL 模型返回 %d: %s",
                               resp.status_code, resp.text[:300])
                return {}
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            if not content:
                return {}

            # 解析 JSON (兼容 {"params":[...]} 对象 或 顶层 [...], 含 ```json 包裹容错)
            parsed = self._robust_json_parse(content)
            if isinstance(parsed, dict):
                parsed = parsed.get("params", [])
            if not isinstance(parsed, list):
                logger.warning("[OCRRecognizeAgent] VL 返回非数组, content[:200]=%s", content[:200])
                return {}

            results = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                param = item.get("param", "")
                value = item.get("value")
                raw_match = item.get("raw_match", "")
                # v4.01.023: 归一字段名 (Model→motor_type 等) + 过滤 "未识别" 等无效值
                param = _vl_normalize_param(param, valid_fields)
                if not param or param not in valid_fields or _vl_is_skip_value(value):
                    continue
                value = self._convert_value(param, value)
                field_def = next((f for f in self._FIELD_DEFS if f[0] == param), None)
                label = field_def[1] if field_def else param
                unit = field_def[2] if field_def else ""
                results[param] = {
                    "value": value,
                    "unit": unit,
                    "label": label,
                    "raw_match": raw_match or str(value),
                }
            logger.info("[OCRRecognizeAgent] VL 提取到 %d 个参数", len(results))
            return results
        except Exception as e:
            logger.warning("[OCRRecognizeAgent] VL 调用失败: %s", e)
            return {}

    def _build_vl_prompt(self, use_subset: bool = False):
        """构建 VL 识别的 system 提示词 + 字段列表 (复用 _FIELD_DEFS 与提取规则)

        v4.01.023: use_subset=True 时只用 _VL_FIELD_SUBSET (铭牌可见字段),
        大幅缩短上下文与生成长度, 避免 qwen3-vl:2b 在 2B 视觉预算下超时/幻觉。
        """
        if use_subset:
            subset = set(self._VL_FIELD_SUBSET)
            defs = [f for f in self._FIELD_DEFS if f[0] in subset]
        else:
            defs = self._FIELD_DEFS
        field_list = "\n".join(
            f'  - "{name}": {cn} (单位: {unit or "无"}) {hint}'
            for name, cn, unit, hint in defs
        )
        system_prompt = (
            "你是一位电机铭牌识别助手。从图片中识别可见的电机参数, 按 RMxprt 11 大类理解, "
            "只输出 JSON, 不要任何解释或 markdown。\n"
            "【规则】\n"
            "1. 只识别图片中明确可见的参数, 不要凭空编造\n"
            "2. 单位换算: 功率保留 kW 数值(5.5kW→5.5); 效率'85%'或'0.85'→85; "
            "功率因数'0.85'→0.85; 磁密'1.5T'→1.5\n"
            "3. value 必须是纯数值或简单字符串, 不要包含单位\n"
            "4. 布尔类(铸铝转子/双笼/深槽/绕线): 是/True→'是', 否/False→'否'\n"
            "5. 不可见的字段不要输出, 也不要填'未识别'\n"
            "6. raw_match 填图中识别到的原文片段(如'5.5kW')"
        )
        return system_prompt, field_list

    # ══════════════════════════════════════════════════════════════
    # 维度3: LLM 提取 (增强提示词)
    # ══════════════════════════════════════════════════════════════

    def _llm_extract(self, text: str, llm) -> dict:
        """调用 LLM 从文本中提取电机全参数 (v4.01.009 11大类全字段增强版)"""
        field_list = "\n".join(
            f'  - "{name}": {cn} (单位: {unit or "无"}) {hint}'
            for name, cn, unit, hint in self._FIELD_DEFS
        )

        system_msg = (
            "你是一位精通电机电磁设计与 RMxprt 仿真的高级工程师, 同时擅长 OCR 文本识别纠错。\n"
            "从给定的文本中提取电机的全部参数, 按 RMxprt 输入参数分为 11 大类:\n"
            "1.通用参数 2.定子参数 3.定子槽形参数 4.转子参数 5.转子槽形参数\n"
            "6.绕组参数 7.转轴参数 8.材料参数 9.性能参数 10.电磁参数 11.损耗参数\n\n"

            "【提取规则】\n"
            "1. 只提取文本中明确出现的参数值, 不要凭空编造\n"
            "2. 从电机型号可以推断极数: Y2-132S-4 的末位 4 → 4极, YE3-160M-6 → 6极\n"
            "3. 单位换算:\n"
            "   - 功率: '5.5kW' → value=5.5 (保留kW数值, 不要转换为W)\n"
            "   - 电压: '380V' → value=380\n"
            "   - 效率: '85%' 或 '0.85' → value=85 (统一为百分比)\n"
            "   - 功率因数: 'cosφ=0.85' → value=0.85 (保留小数)\n"
            "   - 磁密: '1.5T' → value=1.5\n"
            "   - 温升: '75K' → value=75\n"
            "   - 温度: '75℃' → value=75\n"
            "4. OCR 纠错 (高频错误模式):\n"
            "   - '5.SkW' → 5.5, 'lP55' → IP55, '1C4ll' → IC411\n"
            "   - 常见混淆: O→0, l→1, S→5, B→8, I→1\n"
            "   - 单位粘连: '380V' 可能被OCR为 '3BOV'\n"
            "5. 交叉验证:\n"
            "   - 4极50Hz → 额定转速约1440-1480rpm, 同步转速1500\n"
            "   - 2极50Hz → 约2880-2950rpm, 6极50Hz → 约960-980rpm, 8极50Hz → 约720-740rpm\n"
            "   - 如果功率和电流同时存在, 验证 I ≈ P×1000/(√3×U×cosφ×η)\n"
            "   - 转子外径 ≈ 定子内径 - 2×气隙\n"
            "   - 效率范围: 0.55kW约70%, 5.5kW约85%, 55kW约92%, 250kW约95%\n"
            "6. value 必须是纯数值(数字)或简单字符串, 不要包含单位\n"
            "7. 如果文本中某个参数值不确定或无法识别, 不要输出该参数\n"
            "8. 电机型号可能含有空格或换行: 'Y 2 - 132 S - 4' → motor_type='Y2-132S-4'\n"
            "9. 布尔类参数(铸铝转子/双笼/深槽/绕线): 是/True → '是', 否/False → '否'\n"
            "10. 绕组节距可能是交叉式: '1-9/2-10/19-27' → winding_pitch='1-9/2-10/19-27'\n\n"

            "【RMxprt 参数参考 (Y-132S-4 5.5kW 4极 典型值)】\n"
            "通用: 极数=4, 功率=5.5kW, 电压=380V, 转速=1440rpm, 频率=50Hz, 工作温度=75℃\n"
            "定子: 外径=210mm, 内径=136mm, 铁心长=115mm, 槽数=36, 斜槽=1.0, 槽绝缘=0.25mm\n"
            "定子槽形: 梨形槽, Hs0/Hs1/Hs2, Bs0/Bs1/Bs2, Rs (需从文本提取)\n"
            "转子: 外径≈135.3mm, 内径=48mm, 铁心长=115mm, 槽数=32, 铸铝=是, 双笼=否, 深槽=否, 绕线=否\n"
            "转子槽形: 刀形/梯形, Hs0/Hs1/Hs2, Bs0/Bs1/Bs2 (需从文本提取)\n"
            "绕组: 单层交叉式, 层数=1, 并联支路=1, 每槽导体=47, 并绕=2, 线径=0.93mm, 节距=1-9/2-10/19-27, 接法=△\n"
            "转子绕组: 铸铝, 导条材料=铝, 端环材料=铝\n"
            "转轴: 轴径=38mm, 参考转速=1500rpm(同步)\n"
            "材料: 定子=DW470_50, 转子=DW470_50, 导体=Copper, 轴=Steel_1010\n"
            "性能: 电流≈11.6A, 效率≈85%, 功率因数≈0.85\n"
            "电磁: 齿磁密≈1.5T, 轭磁密≈1.2T, 气隙磁密≈0.7T, 电流密度≈5.5A/mm2\n\n"

            "只返回 JSON 数组, 不要包含任何解释文字或 markdown 标记。\n"
            '数组元素格式: {"param": "参数英文名", "value": 值, "raw_match": "原文中匹配到的片段"}\n\n'
            f"可提取的参数列表({len(self._FIELD_DEFS)}个, 11大类):\n{field_list}"
        )

        user_msg = f"请从以下文本中提取电机全部参数 (11大类):\n\n{text}"

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            messages = [SystemMessage(content=system_msg), HumanMessage(content=user_msg)]
            response = llm.invoke(messages)
            content = response.content.strip()

            # v4.01.009: 详细日志记录 LLM 响应
            logger.info("[OCRRecognizeAgent] LLM 响应长度=%d, 前200字符=%s, 后200字符=%s",
                        len(content), content[:200], content[-200:] if len(content) > 200 else "")

            # 清理 markdown 包裹
            if content.startswith("```"):
                lines = content.split("\n")
                if lines[-1].strip().startswith("```"):
                    content = "\n".join(lines[1:-1])
                else:
                    content = "\n".join(lines[1:])

            # 解析 JSON
            parsed = self._robust_json_parse(content)
            if not isinstance(parsed, list):
                logger.warning("[OCRRecognizeAgent] LLM 返回非数组 (解析失败), content[:300]=%s", content[:300])
                return {}

            # 转换为 params dict
            results = {}
            valid_fields = {f[0] for f in self._FIELD_DEFS}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                param = item.get("param", "")
                value = item.get("value")
                raw_match = item.get("raw_match", "")

                if not param or value is None:
                    continue
                if param not in valid_fields:
                    continue

                # 类型转换 + 值清洗
                value = self._convert_value(param, value)

                # 查找字段定义
                field_def = next((f for f in self._FIELD_DEFS if f[0] == param), None)
                label = field_def[1] if field_def else param
                unit = field_def[2] if field_def else ""

                results[param] = {
                    "value": value,
                    "unit": unit,
                    "label": label,
                    "raw_match": raw_match or str(value),
                }

            logger.info("[OCRRecognizeAgent] LLM 提取到 %d 个参数 (文本长度 %d)",
                        len(results), len(text))
            return results

        except Exception as e:
            logger.warning("[OCRRecognizeAgent] LLM 提取失败: %s", e)
            return {}

    # ══════════════════════════════════════════════════════════════
    # 维度3: 值清洗与类型转换
    # ══════════════════════════════════════════════════════════════

    def _convert_value(self, param: str, value):
        """将 LLM 返回的值转换为正确类型 (v4.00.008: 增强清洗)

        v4.00.008 新增:
          - 中文数字转换: "四" → 4, "两" → 2
          - 千分位去除: "1,440" → 1440, "5,500" → 5500
          - 范围值取中值: "85-86%" → 85.5, "85~86" → 85.5
          - 误差值取标称: "85±0.5" → 85
          - 全角数字转半角: "１４４０" → 1440
        """
        import re as _re

        # 纯数字直接处理
        if isinstance(value, (int, float)):
            if param == "efficiency" and isinstance(value, float) and 0 < value < 1:
                return round(value * 100, 1)
            if param in self._INT_FIELDS:
                return int(value)
            if param in self._FLOAT_FIELDS:
                return float(value)
            return value

        # 字符串处理
        s = str(value).strip()

        # v4.00.008: 全角数字 → 半角
        s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

        # v4.00.008: 中文数字转换
        cn_num_map = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
                      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if len(s) == 1 and s in cn_num_map:
            s = str(cn_num_map[s])
        elif s.startswith("十") and len(s) == 2:
            s = str(10 + cn_num_map.get(s[1], 0))

        # 字符串类参数 — 清洗多余后缀
        if param in self._STRING_FIELDS:
            if param == "insulation_class":
                m = _re.search(r'([A-F])', s)
                return m.group(1) if m else s
            if param == "protection_class":
                m = _re.search(r'(IP\s*\d+)', s, _re.IGNORECASE)
                return m.group(1).replace(" ", "").upper() if m else s
            if param == "cooling_method":
                m = _re.search(r'(IC\s*\d+)', s, _re.IGNORECASE)
                return m.group(1).replace(" ", "").upper() if m else s
            if param == "motor_type":
                return s
            return s

        # v4.00.008: 去千分位逗号 ("1,440" → "1440")
        s_clean = _re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', s)

        # v4.00.008: 范围值取中值 ("85-86" → "85.5", "85~86" → "85.5")
        range_m = _re.match(r'^(\d+\.?\d*)\s*[-~]\s*(\d+\.?\d*)', s_clean)
        if range_m:
            try:
                low, high = float(range_m.group(1)), float(range_m.group(2))
                mid = round((low + high) / 2, 2)
                s_clean = str(mid)
            except (ValueError, TypeError):
                pass

        # v4.00.008: 误差值取标称 ("85±0.5" → "85")
        s_clean = _re.sub(r'\s*[±]\s*\d+\.?\d*', '', s_clean)

        # 数值类参数 — 提取数字
        m = _re.match(r'^[\-]?\d+\.?\d*', s_clean)
        if m:
            num_str = m.group()
            try:
                num = float(num_str)
                if param == "efficiency" and 0 < num < 1:
                    return round(num * 100, 1)
                if param in self._INT_FIELDS:
                    return int(num)
                if num == int(num):
                    return int(num)
                return num
            except (ValueError, TypeError):
                pass

        return s

    def _robust_json_parse(self, text: str):
        """从 LLM 响应文本中提取 JSON 数组 (v4.01.009: 增强截断JSON容错)

        v4.01.009 改进:
          - 处理截断的JSON数组 (自动补全括号)
          - 逐对象提取容错 (当数组整体解析失败时, 逐个提取 {} 对象)
          - 更健壮的 markdown 包裹清理
        """
        import re as _re

        # 0. 清理 markdown 包裹
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # 去掉首行 ``` 和可能的末行 ```
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        # 1. 直接解析
        try:
            result = json.loads(cleaned)
            if isinstance(result, list):
                return result
        except Exception:
            pass

        # 2. 贪婪提取 [...] 块
        match = _re.search(r'\[[\s\S]*\]', cleaned)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass

        # 3. v4.01.009: 处理截断的 JSON 数组 — 从第一个 [ 开始, 逐字符匹配括号深度
        first_bracket = cleaned.find('[')
        if first_bracket >= 0:
            fragment = cleaned[first_bracket:]
            # 尝试补全截断的数组
            depth = 0
            in_string = False
            escape = False
            last_complete_obj_end = -1
            for i, ch in enumerate(fragment):
                if escape:
                    escape = False
                    continue
                if ch == '\\':
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '[':
                    depth += 1
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 1:  # 回到数组层
                        last_complete_obj_end = i
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        # 完整数组, 直接解析
                        try:
                            return json.loads(fragment[:i+1])
                        except Exception:
                            pass
                        break

            # 如果有完整的对象但数组被截断, 截取到最后一个完整对象并补 ]
            if last_complete_obj_end > 0:
                truncated = fragment[:last_complete_obj_end + 1] + ']'
                try:
                    return json.loads(truncated)
                except Exception:
                    pass

        # 4. v4.01.009: 逐对象提取 — 最后的兜底, 用正则逐个提取 JSON 对象
        objects = _re.findall(r'\{[^{}]*\}', cleaned)
        if objects:
            results = []
            for obj_str in objects:
                try:
                    obj = json.loads(obj_str)
                    if isinstance(obj, dict):
                        results.append(obj)
                except Exception:
                    # 尝试修复常见问题: 尾部逗号
                    fixed = _re.sub(r',\s*}', '}', obj_str)
                    fixed = _re.sub(r',\s*]', ']', fixed)
                    try:
                        obj = json.loads(fixed)
                        if isinstance(obj, dict):
                            results.append(obj)
                    except Exception:
                        pass
            if results:
                logger.info("[OCRRecognizeAgent] _robust_json_parse 逐对象提取 %d 个对象", len(results))
                return results

        logger.warning("[OCRRecognizeAgent] _robust_json_parse 全部策略失败, text[:300]=%s", cleaned[:300])
        return None


# ════════════════════════════════════════════════════════════════════════════
# v4.01.026: 新增 — 变量对照校核智能体
# 用途: 批量导入冲突解决时, 由 AI 对照「已录入记录」与「本次导入数据」,
#       判断冲突字段是否合理、建议覆盖还是保留, 供人工校核确认。
# ════════════════════════════════════════════════════════════════════════════
class ParamCompareAgent(BaseAgent):
    """变量对照校核智能体 — 比对两条电机记录, 给出冲突处理建议"""

    def __init__(self):
        super().__init__(name="ParamCompareAgent", description="案例电机变量对照校核智能体")
        self._llm = None

    def _get_llm(self):
        """每次调用时动态创建 LLM, 经网关路由 (管理端模型配置即时生效 + 云端自动降级)。"""
        if not _LLM_AVAILABLE:
            return None
        return super()._get_llm_or_cloud(temperature=0.1)

    def _format_record(self, rec: Dict[str, Any]) -> str:
        """把记录格式化为可读文本 (只列有值的字段)"""
        if not rec:
            return "(空)"
        lines = []
        for f in MOTOR_PARAM_FIELDS:
            fname, cn = f[0], f[1]
            v = rec.get(fname)
            if v is not None and str(v).strip() != "":
                lines.append(f"  - {cn}({fname}): {v}")
        return "\n".join(lines) if lines else "(无参数)"

    def run(self, existing: Dict[str, Any], incoming: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """对照两条记录, 返回建议

        Returns:
            {
                "advice": "覆盖" | "保留" | "部分覆盖",
                "detail": "冲突字段对比与理由说明",
                "diff_fields": ["rated_power", ...],
            }
        """
        from Core.case_motor_db import MOTOR_PARAM_FIELDS as _MPF  # 兜底引用
        try:
            MOTOR_PARAM_FIELDS  # noqa
        except NameError:
            pass

        existing_text = self._format_record(existing)
        incoming_text = self._format_record(incoming)

        # 先本地计算差异字段 (不依赖 LLM, 保证健壮性)
        diff_fields = []
        for f in MOTOR_PARAM_FIELDS:
            fname = f[0]
            ev = existing.get(fname)
            iv = incoming.get(fname)
            if ev is None and iv is None:
                continue
            if _fp_equal_local(ev, iv):
                continue
            if str(ev).strip() == "" and iv is None:
                continue
            if ev is None or str(ev).strip() == "":
                if iv is not None and str(iv).strip() != "":
                    diff_fields.append(fname)
            elif iv is None or str(iv).strip() == "":
                diff_fields.append(fname)
            else:
                diff_fields.append(fname)

        llm = self._get_llm()
        if not llm:
            # LLM 不可用时回退为启发式: 有差异字段就建议人工查看(默认保留)
            return {
                "advice": "保留",
                "detail": f"LLM 不可用, 已检测到 {len(diff_fields)} 个差异字段, 建议人工校核后决定。",
                "diff_fields": diff_fields,
            }

        system_msg = (
            "你是一位电机参数审核专家。现有两条同一系列+型号的电机记录(一条已录入数据库, "
            "一条是本次批量导入的新数据), 它们存在参数冲突。\n"
            "请对比两条记录, 判断哪些差异是合理的(如本次导入是更准确的实测值/更新版本), "
            "哪些差异可疑(如数量级错误、单位混淆、明显笔误)。\n"
            "只输出 JSON, 不要任何解释或 markdown, 格式:\n"
            '{"advice": "覆盖" 或 "保留" 或 "部分覆盖", '
            '"detail": "用中文简要说明冲突字段对比与你的判断理由(不超过120字)", '
            '"diff_fields": ["字段名1", "字段名2"]}'
        )
        human_msg = (
            f"【已录入记录】\n{existing_text}\n\n"
            f"【本次导入数据】\n{incoming_text}\n\n"
            f"已知差异字段: {', '.join(diff_fields) if diff_fields else '无'}\n\n"
            "请给出处理建议 (覆盖=用新数据更新旧记录; 保留=保留旧记录; 部分覆盖=仅更新合理差异字段)。"
        )

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            messages = [SystemMessage(content=system_msg), HumanMessage(content=human_msg)]
            resp = llm.invoke(messages)
            content = resp.content if hasattr(resp, "content") else str(resp)
            # 容错提取 JSON
            import re
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                advice = obj.get("advice", "保留")
                if advice not in ("覆盖", "保留", "部分覆盖"):
                    advice = "保留"
                return {
                    "advice": advice,
                    "detail": obj.get("detail", ""),
                    "diff_fields": obj.get("diff_fields", diff_fields),
                }
            return {
                "advice": "保留",
                "detail": "AI 返回格式异常, 已回退为保留建议, 请人工校核。",
                "diff_fields": diff_fields,
            }
        except Exception as e:
            logger.warning("[ParamCompareAgent] LLM 调用失败: %s", e)
            return {
                "advice": "保留",
                "detail": f"AI 调用异常({e}), 已回退为保留建议。",
                "diff_fields": diff_fields,
            }


def _fp_equal_local(a: Any, b: Any) -> bool:
    """数值/字符串相等判定 (与 case_motor_db._fp_equal 同口径)"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()
