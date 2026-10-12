# -*- coding: utf-8 -*-
"""DSH 设计 JSON 契约 — 单一真相源（M3, v4.02.75）。

源 v2 方案 M3：消除 12 键契约在 5 处的手工维护
（prompt v1 / prompt v2 / _SCHEMA_MAP / _JSON_CORE_KEYS / _dsh_evaluator.GATE_JSON_CORE_KEYS）。

所有下游（dsh_agent.py 的提示词与映射、_dsh_evaluator.py 的门控）统一从这里导入，
修改契约只改本文件，再由 ``Scripts/_verify_dsh_schema.py`` 做五处同源断言。

v4.02.75 修复：``temperature_rise_k`` 原映射到 ``max_temp_rise``，但主流程质量门禁
实际用 ``max_temp``（orchestrator）/ ``temp_max``（quality_gate），导致 DSH 输出
无法进入门禁。本文件统一为 ``max_temp``。
"""
from __future__ import annotations

# ---- 12 键设计 JSON 契约（DSH 输出侧规范化命名）----
# 10 个数值键（用于打分 / 门控 / 映射）+ 2 个文本键（summary / risks）。
DSH_NUMERIC_KEYS = (
    "stator_od_mm", "stator_id_mm", "core_length_mm", "air_gap_mm",
    "slots_stator", "poles", "efficiency_pct", "power_factor",
    "temperature_rise_k", "torque_nm",
)
DSH_AUX_KEYS = ("summary", "risks")
DSH_DESIGN_KEYS = DSH_NUMERIC_KEYS + DSH_AUX_KEYS  # 共 12 键

# DSH JSON 键 -> MotorAI 结果 schema 键（单一映射，修复 max_temp_rise -> max_temp）
SCHEMA_MAP = {
    "stator_od_mm": "stator_od",
    "stator_id_mm": "stator_id",
    "core_length_mm": "core_length",
    "air_gap_mm": "air_gap",
    "slots_stator": "slots_stator",
    "poles": "poles",
    "efficiency_pct": "efficiency",
    "power_factor": "power_factor",
    "temperature_rise_k": "max_temp",   # 修复：原 max_temp_rise -> max_temp
    "torque_nm": "torque",
    "summary": "summary",
    "risks": "risks",
}

# 抽取器 / 门控共用核心键（= 数值键）
JSON_CORE_KEYS = DSH_NUMERIC_KEYS
GATE_JSON_CORE_KEYS = DSH_NUMERIC_KEYS
JSON_AUX_KEYS = DSH_AUX_KEYS

# 纯文本 `key = value` 兜底的最低核心键命中数
KV_MIN_CORE = 6

# 每个键的中文含义 + 单位（用于提示词自动生成 schema 块，保持五处同源）
_SCHEMA_ANNOTATIONS = {
    "stator_od_mm": ("定子外径", "mm"),
    "stator_id_mm": ("定子内径", "mm"),
    "core_length_mm": ("铁心长", "mm"),
    "air_gap_mm": ("气隙", "mm"),
    "slots_stator": ("定子槽数", "整数"),
    "poles": ("极数", "整数"),
    "efficiency_pct": ("效率", "%"),
    "power_factor": ("功率因数", "0~1"),
    "temperature_rise_k": ("温升", "K"),
    "torque_nm": ("额定转矩", "N·m"),
    "summary": ("一句话方案总结", "字符串"),
    "risks": ("关键风险", "字符串数组"),
}


def json_schema_block() -> str:
    """生成 12 键 JSON schema 的代码块文本（含中文注释），供提示词内联。

    提示词 v1/v2 的【输出格式】段统一调用本函数，确保键集合与 SCHEMA_MAP / 门控
    永远一致（五处同源）。
    """
    lines = ["{"]
    for k in DSH_DESIGN_KEYS:
        name, unit = _SCHEMA_ANNOTATIONS[k]
        lines.append(f'  "{k}": {name}({unit}),')
    lines.append("}")
    return "\n".join(lines)
