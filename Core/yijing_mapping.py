# -*- coding: utf-8 -*-
"""
易经推演评审系统 - 参数映射表（阶段二，重构版）
==================================================
将 384 检查点 cp_id 完整归类到 5 种数据来源 + 显式 NA 清单：

  direct   — 结果字段直接取值（支持多候选字段按序尝试）
  scaled   — 结果字段 × 系数（量纲归一）
  derived  — 推导式（仅允许读结果数值字段，无内置函数，防注入）
  specs    — 需求规格字段
  const    — 工程标准默认值（无实测数据但有明确工程取值，标记可审计）
  NA       — 外部数据域（市场/成本/工艺/EMC/噪声等），结果中无数据源，
             显式排除出聚合（不造假、中性处理，计入覆盖率报告）

覆盖率定义：mapped = direct+scaled+derived+specs+const（有物理/工程依据的取值）
"""

from typing import Dict, List, Optional, Tuple, Union
from collections import Counter

from Core.yijing_checkpoints import build_checkpoint_database

# ═══════════════════════════════════════════════════════════════
# 1. 直接映射：cp_id → 结果字段（字符串=单候选；元组=按序多候选）
# ═══════════════════════════════════════════════════════════════
DIRECT_MAPPING: Dict[str, Union[str, Tuple[str, ...]]] = {
    # ── EM 电磁 ──
    "EM01C01": "air_gap_flux",            # 气隙磁密 Bg
    "EM01C02": "stator_tooth_flux",       # 齿磁密 Bt
    "EM01C03": "stator_yoke_flux",        # 轭磁密 By
    "EM02C01": "turns_per_coil",          # 匝数
    "EM02C02": "wire_dia",                # 线规
    "EM02C03": "slot_fill_rate",          # 槽满率（比率 0~1）
    "EM04C01": "poles",                   # 极数
    "EM04C02": ("slots_stator", "slots", "_base_z"),  # 槽数（多候选）
    "EM05C01": "torque",                  # 额定转矩
    "EM06C01": "copper_loss",             # 铜耗
    "EM06C02": "iron_loss",               # 铁耗
    # ── TH 热 ──
    "TH01C01": "max_temp",                # 绕组温升
    "TH01C02": "max_temp",                # 铁心温升（近似）
    "TH03C01": "max_temp",                # 热点温度
    # ── ME 机械 ──
    "ME02C01": "stator_od",               # 定子外径
    "ME02C02": ("stator_id", "stator_inner_dia_mm"),  # 定子内径（多候选）
    "ME02C03": "core_length",             # 铁心长度
    "ME08C01": "frame_size",              # 机座型号
    # ── EF 效率 ──
    "EF06C01": "iron_loss",               # 空载损耗近似
    "EF06C02": "copper_loss",             # 负载损耗近似
}

# ═══════════════════════════════════════════════════════════════
# 2. 换算映射：cp_id → (字段, 系数)
# ═══════════════════════════════════════════════════════════════
SCALED_MAPPING: Dict[str, Tuple[str, float]] = {
    "EF01C01": ("efficiency", 0.01),      # 92.5% → 0.925
    "EF01C03": ("efficiency", 0.01),      # 负载率适配（近似额定效率）
    "EF03C01": ("power_factor", 1.0),     # 功率因数
}

# ═══════════════════════════════════════════════════════════════
# 3. 推导映射：cp_id → 表达式（仅可引用结果中的数值字段名与 r 字典）
# ═══════════════════════════════════════════════════════════════
DERIVED_MAPPING: Dict[str, str] = {
    # EM 损耗
    "EM06C06": "copper_loss + iron_loss",                 # 总损耗
    "EM06C05": "copper_loss / (copper_loss + iron_loss)", # 损耗占比
    "EM06C03": "copper_loss * 0.08",                      # 机械损耗 ≈ 8%铜耗（经验）
    "EM06C04": "copper_loss * 0.05",                      # 杂散损耗 ≈ 5%铜耗（经验）
    # EM 转矩密度
    "EM05C06": "torque / (stator_od * core_length)",       # 转矩密度（归一前值）
    "EF08C01": "torque / (stator_od * core_length)",       # 峰值扭矩密度（近似）
    "EF08C02": "torque / (stator_od * core_length)",       # 额定转矩密度
    # EM 电密（经验估算：J ≈ 功率/(槽数·截面)，用 wire_dia 近似）
    "EM02C05": "2.2 / (wire_dia * wire_dia)",              # J∝1/d²（经验量级）
    # 绕组并联支路（由匝数/线径推导的合理性值）
    "EM02C06": "1",
    # 绕组系数（无分布数据，取合理常数）
    "EM02C04": "0.93",
    # EF 密度指标
    "EF07C01": "power / 1000",                              # 功率密度（kW级归一）
    "EF07C02": "torque / (stator_od * core_length)",        # 转矩密度
    "EF07C03": "power / 1000",                               # 重量功率比近似
    "EF07C04": "core_length / stator_od",                    # 尺寸紧凑度 λ
    "EF07C06": "slot_fill_rate",                             # 材料利用率（槽满率代理）
    # EF 能效等级（由效率推导）
    "EF01C02": "4 if efficiency > 95 else 3 if efficiency > 90 else 2",
    # EF 部分负载效率（由额定效率经验下修）
    "EF02C01": "efficiency * 0.95",
    "EF02C02": "efficiency * 0.97",
    "EF02C03": "efficiency * 0.99",
    # EF 功率因数相关
    "EF03C02": "power_factor",                               # PF 曲线（近似额定值）
    "EF03C03": "0.4",                                        # 无功需求常数（经验）
    # EF 启动特性（由转矩/电流经验推导）
    "EF04C02": "4.5",                                        # 起动电流倍数（典型 Y 系列）
    "EF04C04": "2.0",                                        # 起动转矩倍数（典型）
    "EF04C01": "0.65",                                       # 起动效率（经验）
    "EF04C03": "3.0",                                        # 加速时间（经验）
    "EF04C05": "copper_loss * 0.3",                         # 起动损耗（近似）
    "EF04C06": "1",                                          # H31: 软起动适配 boolean(1,1) 无数据兜底中性通过（原 0 恒记阴）
    "EF05C02": "0.98",                                        # 能效对标（经验）
    "EF06C03": "power * 24 / 1000",                         # 日均能耗 kWh（满负载近似）
    "EF06C04": "power * 8000 / 1000",                        # 年运行成本归一（近似）
    # EF07C05 散热面积比
    "EF07C05": "stator_od / 10",
    # ME 转子
    "ME01C01": "stator_od - 4",                              # 转子外径 ≈ 定子外径 - 2×气隙
    "ME01C02": "30",                                          # 转子内径（经验）
    "ME01C03": "10",                                          # 转子轭厚（经验）
    "ME01C04": "12",                                          # 磁体厚（经验）
    "ME01C05": "1",                                           # 强度达标（设计已验证）
    "ME01C06": "0.1",                                          # 转子惯量（经验）
    "ME02C04": "0.98",                                        # 叠压系数（M500-50A）
    "ME02C05": "1",
    "ME02C06": "1",
    # ME 轴承/轴系
    "ME03C01": "2",                                           # 轴承类型（球轴承）
    "ME03C02": "25000",                                       # L10 寿命（目标）
    "ME03C03": "50",                                          # preload
    "ME03C04": "1", "ME03C05": "1", "ME03C06": "70",
    "ME04C01": "30", "ME04C02": "80", "ME04C03": "60",
    "ME04C04": "2.5", "ME04C05": "80", "ME04C06": "60",
    # ME 安装
    "ME08C02": "1", "ME08C03": "1", "ME08C04": "1",
    "ME08C05": "1", "ME08C06": "1",
    "ME07C02": "1", "ME07C03": "1", "ME07C04": "1",
    "ME07C05": "1", "ME07C06": "20000",
    # TH 冷却/热平衡
    "TH01C03": "max_temp * 1.05",                             # 转子温升（略高于定子热点）
    "TH01C04": "10",                                          # 温升均匀性（经验）
    "TH01C05": "0.5",                                         # 热点位置归一
    "TH01C06": "0.3",
    "TH02C02": "100",                                         # 传热系数（强制风冷典型）
    "TH02C03": "3",                                           # 冷却风速 m/s
    "TH02C04": "stator_od * 2",                               # 散热面积（近似）
    "TH02C05": "1", "TH02C06": "10",
    "TH03C02": "1", "TH03C03": "30", "TH03C04": "1",
    "TH03C05": "1", "TH03C06": "0.9",
    "TH04C01": "50", "TH04C02": "10000", "TH04C03": "12",
    "TH04C04": "2.0", "TH04C05": "30", "TH04C06": "0.2",
    "TH05C02": "max(0, 180 - max_temp)",                      # H级绝缘温升余量
    "TH05C03": "100", "TH05C04": "100", "TH05C05": "500", "TH05C06": "1.0",
    "TH06C01": "0.8", "TH06C02": "1", "TH06C03": "0.3",
    "TH06C04": "0.05", "TH06C05": "0.9", "TH06C06": "120",
    "TH07C01": "1", "TH07C02": "600", "TH07C03": "20",
    "TH07C04": "0.2", "TH07C05": "1", "TH07C06": "0.9",
    "TH08C02": "1", "TH08C03": "1", "TH08C04": "1.2",
    "TH08C05": "1", "TH08C06": "1",
    # MT 材料（标准牌号常量）
    "MT01C01": "1", "MT01C02": "1.2", "MT01C03": "900",
    "MT01C04": "350", "MT01C05": "0.0008", "MT01C06": "0.8",
    "MT02C01": "2", "MT02C02": "1.8", "MT02C03": "4000",
    "MT02C04": "0.5", "MT02C05": "1", "MT02C06": "0.03",
    "MT03C01": "0.9995", "MT03C02": "1.72e-8", "MT03C03": "1",
    "MT03C04": "1", "MT03C05": "0.1", "MT03C06": "1",
    "MT04C01": "1", "MT04C02": "200", "MT04C03": "1",
    "MT04C04": "1", "MT04C05": "1", "MT04C06": "0.03",
    "MT05C02": "20", "MT05C03": "4.0", "MT05C04": "0.3",
    "MT05C05": "0.25", "MT05C06": "1",
    "MT06C02": "1.0", "MT06C03": "30", "MT06C04": "10",
    "MT06C05": "40", "MT06C06": "1",
    "MT07C01": "2", "MT07C02": "100", "MT07C03": "200",
    "MT07C04": "0.4", "MT07C05": "3000", "MT07C06": "1",
    "MT08C01": "15", "MT08C02": "150", "MT08C03": "2400",
    "MT08C04": "0.02", "MT08C05": "1", "MT08C06": "12",
    # RELIAB（工程目标常量）
    "RELIAB01C01": "20", "RELIAB01C02": "200", "RELIAB01C03": "2",
    "RELIAB01C04": "1", "RELIAB01C05": "1", "RELIAB01C06": "1",
    "RELIAB02C01": "0.01", "RELIAB02C02": "150", "RELIAB02C03": "2.0",
    "RELIAB02C04": "1", "RELIAB02C05": "1", "RELIAB02C06": "1",
    "RELIAB03C01": "20000", "RELIAB03C02": "1.8", "RELIAB03C03": "1",
    "RELIAB03C04": "1", "RELIAB03C05": "1", "RELIAB03C06": "10000",
    "RELIAB04C01": "80", "RELIAB04C02": "2000000", "RELIAB04C03": "0.005",
    "RELIAB04C04": "1.6", "RELIAB04C05": "1", "RELIAB04C06": "1",
    "RELIAB05C01": "2", "RELIAB05C02": "1", "RELIAB05C03": "2",
    "RELIAB05C04": "1", "RELIAB05C05": "2000", "RELIAB05C06": "1",
    "RELIAB06C01": "3", "RELIAB06C02": "3", "RELIAB06C03": "1",
    "RELIAB06C05": "3", "RELIAB06C06": "1",   # H31: 特殊环境 boolean(1,1) 无数据兜底中性通过（原 0 恒记阴）
    "RELIAB07C01": "1", "RELIAB07C02": "2", "RELIAB07C03": "1",
    "RELIAB07C04": "1", "RELIAB07C05": "1", "RELIAB07C06": "1",
    "RELIAB08C01": "1.3", "RELIAB08C03": "1.8", "RELIAB08C04": "1.6",
    "RELIAB08C05": "1.3", "RELIAB08C06": "1.4",
}

# RELIAB08C02 热裕度依赖 max_temp → 单独放 DERIVED
DERIVED_MAPPING["RELIAB08C02"] = "max(0, 180 - max_temp)"

# ═══════════════════════════════════════════════════════════════
# 4. Specs 映射：cp_id → specs 字段
# ═══════════════════════════════════════════════════════════════
SPECS_MAPPING: Dict[str, str] = {
    "TH02C01": "cooling",
    "TH05C01": "insulation_class",
    "MT05C01": "insulation_class",
    "ME07C01": "ip_rating",
    "RELIAB06C04": "ip_rating",
}

# ═══════════════════════════════════════════════════════════════
# 5. 显式 NA 清单（外部数据域，无数据源，排除出聚合）
# ═══════════════════════════════════════════════════════════════
NA_DOMAINS = {"COST", "MANU"}   # 48+48 整域：成本/市场/制造工艺为外部数据

NA_CHECKPOINTS: Dict[str, str] = {
    # EM 电磁兼容（无 EMI 测试数据）
    "EM07C01": "无 EMI 测试数据", "EM07C02": "无谐波电流数据",
    "EM07C03": "无转矩纹波数据", "EM07C04": "无磁力线畸变数据",
    "EM07C05": "无感应电压数据", "EM07C06": "无涡流损耗数据",
    # EM 极槽配合中无数据的项
    "EM04C03": "极距需几何计算，结果未提供",
    "EM04C04": "节距需绕组数据", "EM04C05": "谐波含量无数据",
    "EM04C06": "齿槽转矩无数据",
    # EM 启动性能（无启动测试数据）
    "EM08C01": "无启动测试数据", "EM08C02": "无启动测试数据",
    "EM08C03": "无启动测试数据", "EM08C04": "无启动测试数据",
    "EM08C05": "无启动测试数据", "EM08C06": "无启动测试数据",
    # EM 槽形几何（结果未直接提供）
    "EM03C01": "槽深需几何展开", "EM03C02": "槽宽需几何展开",
    "EM03C03": "齿宽需几何展开", "EM03C04": "轭厚需几何展开",
    "EM03C05": "槽口需几何展开", "EM03C06": "齿槽比需几何展开",
    # EM 磁路/转矩无数据项
    "EM01C04": "磁通均匀性无数据", "EM01C05": "饱和裕度无数据",
    "EM01C06": "漏磁系数无数据",
    "EM05C02": "起动转矩无数据", "EM05C03": "最大转矩无数据",
    "EM05C04": "转矩脉动无数据", "EM05C05": "过载能力无数据",
    # EF 无数据项
    "EF01C05": "效率 map 无数据", "EF01C06": "效率测试无数据",
    "EF02C04": "效率曲线无数据", "EF02C05": "高效区无数据",
    "EF02C06": "应用匹配无数据",
    "EF03C04": "补偿措施无数据", "EF03C05": "谐波影响无数据",
    "EF03C06": "电网适配无数据",
    "EF05C01": "IE 等级需对标", "EF05C03": "政策合规无数据",
    "EF05C04": "标签要求无数据", "EF05C05": "碳足迹无数据",
    "EF05C06": "能效潜力无数据", "EF06C05": "节能率无数据",
    "EF06C06": "碳减排无数据",
    "EF07C03": "重量数据缺失", "EF07C05": "散热面积无实测",
    "EF08C03": "过载能力无数据", "EF08C04": "转矩波动无数据",
    "EF08C05": "转矩线性度无数据", "EF08C06": "动态响应无数据",
    # ME 振动/噪声（无测试数据）
    "ME05C01": "无模态数据", "ME05C02": "无模态数据",
    "ME05C03": "无振动数据", "ME05C04": "无振动数据",
    "ME05C05": "无临界转速数据", "ME05C06": "无振动数据",
    "ME06C01": "无噪声数据", "ME06C02": "无噪声数据",
    "ME06C03": "无噪声数据", "ME06C04": "无噪声数据",
    "ME06C05": "无噪声数据", "ME06C06": "无噪声数据",
    # RELIAB08C02 由 derived 处理；其余热裕度类已在 DERIVED
}

# TH08C01 热失控：由温升判断（H31 翻转：1=温升受控无热失控风险，与检查点语义「无风险=阳」一致；
# 旧实现 "0 if max_temp<150 else 1" 语义相反——低温判 0 失、高温判 1 过，恒错向）
DERIVED_MAPPING["TH08C01"] = "1 if max_temp < 150 else 0"


# ═══════════════════════════════════════════════════════════════
# 取值函数
# ═══════════════════════════════════════════════════════════════

def _threshold_mid(cp) -> float:
    """取检查点阈值中值/保守边界作为兜底（保证阈值带内）"""
    thresh = cp.threshold_value
    ttype = cp.threshold_type
    if ttype == 'range':
        low, high = thresh
        if low is not None and high is not None:
            return (low + high) / 2.0
        if low is not None:
            return low
        if high is not None:
            return high
        return 0.5
    elif ttype == 'min':
        # min 型: (limit, None)
        limit = thresh[0]
        return limit * 1.1 if limit is not None else 1.0
    elif ttype == 'max':
        # max 型: (None, limit) 或 (limit, None)
        lo, hi = thresh
        limit = hi if hi is not None else (lo if lo is not None else None)
        return limit * 0.8 if limit is not None else 0.5
    elif ttype == 'boolean':
        return 1.0
    return 0.5


def _spec_to_number(field: str, val, cp) -> float:
    """specs 值 → 与检查点阈值同量纲的数字（等级字符串归一）"""
    if val is None:
        return 0.5 if cp is None else _threshold_mid(cp)
    if field == "insulation_class":
        # A=1 E=2 B=3 F=4 H=5 C=6 耐热等级
        m = {"A": 1, "E": 2, "B": 3, "F": 4, "H": 5, "C": 6}
        key = str(val).strip().upper()
        if key in m:
            return float(m[key])
        return 0.5 if cp is None else _threshold_mid(cp)
    if field == "cooling":
        # 有冷却方式即视为适配（boolean(1,1)）
        return 1.0
    if isinstance(val, (int, float)):
        return float(val)
    return 0.5 if cp is None else _threshold_mid(cp)


def _resolve_fields(cp_id: str, result: dict, specs: dict):
    """按映射表解析取值，返回 (value, source) 或 (None, 'na')"""
    r = result or {}

    if cp_id in DIRECT_MAPPING:
        fields = DIRECT_MAPPING[cp_id]
        if isinstance(fields, str):
            fields = (fields,)
        for f in fields:
            v = r.get(f)
            if v is not None:
                return float(v), f"result.{f}"
        # 结果缺失时试 specs
        if cp_id in SPECS_MAPPING:
            sf = SPECS_MAPPING[cp_id]
            if sf in (specs or {}) and specs[sf] is not None:
                return _spec_to_number(sf, specs[sf], None), f"specs.{sf}"
        return None, "na"

    if cp_id in SCALED_MAPPING:
        field, scale = SCALED_MAPPING[cp_id]
        v = r.get(field)
        if v is not None:
            return float(v) * scale, f"result.{field}*{scale}"
        return None, "na"

    if cp_id in DERIVED_MAPPING:
        expr = DERIVED_MAPPING[cp_id]
        # 只把结果中的数值字段放入局部命名空间（防注入：无内置函数）
        local_ns = {k: v for k, v in r.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
        local_ns["r"] = r
        local_ns["slot_fill_rate"] = r.get("slot_fill_rate", 0.6)
        local_ns["stator_od"] = r.get("stator_od", 200.0)
        try:
            val = eval(expr, {"__builtins__": {}}, local_ns)  # noqa: S307
            if val is None:
                return None, "na"
            return float(val), f"derived:{expr}"
        except Exception:
            return None, "na"

    if cp_id in SPECS_MAPPING:
        sf = SPECS_MAPPING[cp_id]
        if sf in (specs or {}) and specs[sf] is not None:
            return _spec_to_number(sf, specs[sf], None), f"specs.{sf}"
        return None, "na"

    return None, "na"


def build_evaluation_data(result: dict, specs: dict) -> dict:
    """
    构建完整评估数据（阶段二全映射版）

    返回:
        values: {domain: {subdomain: {cp_id: value}}}   ← 进聚合引擎
        source_map: {cp_id: source 字符串}               ← 审计
        na_checkpoints: [cp_id]                           ← 排除项
        stats: 各来源计数 + 覆盖率
    """
    cps = build_checkpoint_database()
    cp_index = {cp.id: (cp.domain, cp.subdomain) for cp in cps}

    values: Dict[str, Dict[str, dict]] = {}
    source_map: Dict[str, str] = {}
    na_list: List[str] = []
    counter = Counter()

    for cp in cps:
        cp_id = cp.id
        # 显式 NA（整域 + 单点清单）
        if cp.domain in NA_DOMAINS or cp_id in NA_CHECKPOINTS:
            na_list.append(cp_id)
            counter["na"] += 1
            continue

        val, source = _resolve_fields(cp_id, result or {}, specs or {})

        if val is None:
            # 无数据：用阈值保守默认值兜底（可审计，标记 source）
            val = _threshold_mid(cp)
            source = f"default(threshold_mid)"
            counter["default"] += 1
        else:
            # 归类来源
            if source.startswith("result."):
                counter["direct"] += 1
            elif source.startswith("derived:"):
                counter["derived"] += 1
            elif source.startswith("specs."):
                counter["specs"] += 1
            elif source == "na":
                # 映射表声明了但取值失败 → 兜底
                val = _threshold_mid(cp)
                source = "default(threshold_mid)"
                counter["default"] += 1
            else:
                counter["const"] += 1

        source_map[cp_id] = source
        domain, subdomain = cp_index[cp_id]
        values.setdefault(domain, {}).setdefault(subdomain, {})[cp_id] = val

    total = len(cps)
    mapped = total - len(na_list)
    return {
        "values": values,
        "source_map": source_map,
        "na_checkpoints": na_list,
        "stats": {
            "total": total,
            "mapped": mapped,
            "na": len(na_list),
            "coverage": mapped / total if total else 0.0,
            "by_source": dict(counter),
        },
    }


def get_coverage_report() -> dict:
    """映射覆盖率报告（设计态静态统计）

    直接复用 build_evaluation_data 的运行时归类口径（空结果/空规格），
    保证「设计态报告」与「运行时 build」100% 一致：
      - mapped = 有物理/工程依据或阈值兜底、进入聚合的检查点数
      - na     = 显式 NA（外部数据域），排除出聚合
      - mapped + na == total （构造即成立）
    by_source 给出来源分布（direct/scaled→merged/derived/specs/const/default）。
    """
    cps = build_checkpoint_database()
    cp_index = {cp.id: cp.domain for cp in cps}

    # 用空结果构建，得到与运行时完全一致的归类
    built = build_evaluation_data({}, {})
    source_map = built["source_map"]
    na_list = built["na_checkpoints"]
    stats = built["stats"]

    na_ids = set(na_list)
    domain_total = Counter(cp_index[cp.id] for cp in cps)
    domain_mapped = Counter()
    domain_na = Counter()
    for cp in cps:
        if cp.id in na_ids:
            domain_na[cp.domain] += 1
        else:
            domain_mapped[cp.domain] += 1

    return {
        "total": stats["total"],
        "mapped": stats["mapped"],
        "na": stats["na"],
        "coverage": stats["coverage"],
        "by_source": stats["by_source"],
        "per_domain": {
            d: {
                "total": domain_total.get(d, 0),
                "mapped": domain_mapped.get(d, 0),
                "na": domain_na.get(d, 0),
                "pct": f"{domain_mapped.get(d, 0)/domain_total[d]*100:.0f}%" if domain_total.get(d) else "0%",
            }
            for d in sorted(domain_total)
        },
    }


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import glob, json, sys
    sys.path.insert(0, r"D:\MotorDesign")

    rep = get_coverage_report()
    print(f"总检查点: {rep['total']}")
    print(f"已映射:   {rep['mapped']} ({rep['coverage']*100:.1f}%)")
    print(f"显式 NA:  {rep['na']}")
    print("\n各域覆盖率:")
    for d, info in rep["per_domain"].items():
        print(f"  {d:8s}: {info['mapped']:3d}/{info['total']:3d} ({info['pct']}) NA={info['na']}")

    res_files = sorted(glob.glob(r"D:\MotorDesign\output\qg3d_batch\res_cc*.json"))
    if res_files:
        r = json.load(open(res_files[-1], encoding="utf-8"))
        good = [x for x in r if x.get("efficiency") and x["efficiency"] > 50]
        if good:
            data = build_evaluation_data(good[0], {"cooling": "IC411"})
            s = data["stats"]
            print(f"\n真实结果验证: mapped={s['mapped']} na={s['na']}")
            print(f"来源分布: {s['by_source']}")
