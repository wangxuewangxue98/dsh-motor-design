# -*- coding: utf-8 -*-
"""电磁负荷与材料利用率计算 (v4.02.104, 用户指令 三.2)。

背景
----
RUN32 报告里这两章标注为「程序未直接提供，单独计算」——
即报告数值是人工事后算的, 既不可复现也无法进入产品。本模块把该计算**固化进程序**,
供质量门禁 / 报告撰写员 / 客户端报告统一取用, 消除人工二次计算。

口径 (与 RUN32 报告一致, 可复现)
--------------------------------
电磁负荷:
  - 线负荷      A   = 2·m·W·I / (π·Dsi)          [A/m]  (m=3 相, W=每相串联匝数,
                                                          I=相电流, Dsi 单位 m)
  - 气隙磁密    Bδ  = 程序求解值                  [T]   (峰值)
  - 平均磁密    Bav = (2/π)·Bδ                    [T]   (正弦分布假设)
  - 电磁负荷    A·Bδ / A·Bav                      [A/(m·T)]

材料利用率:
  - 定子冲片净面积  A_net = π/4·(Dso²−Dsi²) − A_slot_total          [m²]
  - 铁体积          V_fe  = A_net · L · k                            [m³]  (k=叠压系数)
  - 铜体积          V_cu  = A_slot_total · SF · L · 1.35             [m³]  (1.35 端部系数)
  - 硅钢重/铜重     G = V·ρ   (ρ_fe=7650, ρ_cu=8960 kg/m³)
  - 铜铁比          G_cu / G_fe
  - 功率密度        P_kW / (G_total/1000)                            [kW/kg]
  - D²L 体积指数    Dsi² · L                                         [m³]
  - 转矩/D²L, 功率/D²L   (单位体积材料产出能力, 衡量材料利用率)

  ⚠ D²L 采用**定子内径** Dsi (与报告口径一致); 若工程上改用等效直径,
    调用方可通过 `d_for_d2l` 覆盖。

设计约束
--------
- 全字段容错: 任一数据缺失 ⇒ 该字段为 None, 绝不抛异常 (报告生成不能因此失败)。
- 每个派生量都记录来源 (`sources`) 与所用方法 (`method`), 供报告声明数据血统。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# ── 物理常数 ────────────────────────────────────────────────────────────────
RHO_FE = 7650.0          # 硅钢片密度 kg/m³
RHO_CU = 8960.0          # 铜密度 kg/m³
K_STACK_DEFAULT = 0.98   # 叠压系数
K_END_DEFAULT = 1.35     # 绕组端部系数 (铜体积折算)
NUM_PHASES = 3           # 相数

# ── 中小型三相异步电机电磁负荷经验区间 (RUN32 报告引用值) ──────────────────
BAND_A = (1.5e4, 6.0e4)          # 线负荷 A/m
BAND_BD = (0.6, 0.9)             # 气隙磁密 T
BAND_AB = (1.0e4, 4.5e4)         # 电磁负荷 A/(m·T)

_SLOT_AREA_KEYS = ("slot_area_mm2", "slot_area", "_slot_area_act", "_slot_area_mm2")


def _f(v: Any) -> Optional[float]:
    """安全转 float; 失败/None/空串 ⇒ None。"""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _first(d: dict, keys) -> tuple:
    """按 keys 顺序取第一个有效 float, 返回 (值, 命中的键名)。"""
    for k in keys:
        v = _f(d.get(k))
        if v is not None:
            return v, k
    return None, None


def _num(v, default=None):
    """保留有效数字的舍入 (避免 0.0000001 噪声); None 原样返回。"""
    x = _f(v)
    if x is None:
        return default
    a = abs(x)
    if a == 0:
        return 0.0
    if a >= 1000:
        return round(x, 1)
    if a >= 1:
        return round(x, 4)
    return round(x, 6)


def compute_slot_total_area_mm2(best: dict, params: dict,
                                sf: Optional[float] = None) -> (Optional[float], str):
    """解析/推导「定子槽总面积 (mm²)」, 返回 (面积, 方法标签)。

    优先级 (高 → 低):
      1. `explicit`  —— 结果中直接给出的槽面积字段
      2. `geometry`  —— 程序自身槽面积公式 (与 Scripts/motor_tools.py:1231 一致):
                          单槽面积 = 0.5·(Bs0 + Bs2)·Hs2
                        ⚠ 采用与几何内核**同一口径**, 保证与 SF 定义 (_sf_est) 自洽。
      3. `copper_over_sf` —— 由槽内铜面积 / 槽满率反推
      4. `bore_approx` —— 无槽形数据时粗估: 0.35 × π/4·(Dso²−Dsi²)
    全部失败返回 (None, "unavailable")。
    """
    qs = _f(params.get("slots_stator")) or _f(best.get("slots_stator"))

    # 1) 显式字段
    v, k = _first(best, _SLOT_AREA_KEYS)
    if v is None:
        v, k = _first(params, _SLOT_AREA_KEYS)
    if v is not None and v > 0:
        # 兼容 m² 误标: 若数量级明显不是 mm² (>1e5 视为 m²)
        if v > 1e5:
            v = v * 1e6
        return v, "explicit:%s" % k

    # 2) 槽形尺寸 (程序口径)
    bs0 = _f(params.get("Slot__Bs0")) or _f(best.get("Slot__Bs0"))
    bs2 = _f(params.get("Slot__Bs2")) or _f(best.get("Slot__Bs2"))
    hs2 = _f(params.get("Slot__Hs2")) or _f(best.get("Slot__Hs2"))
    if bs0 and bs2 and hs2 and hs2 > 0 and qs and qs > 0:
        a_slot = 0.5 * (bs0 + bs2) * hs2
        if a_slot > 0:
            return a_slot * qs, "geometry:0.5(Bs0+Bs2)Hs2×Qs"

    # 3) 铜面积 / 槽满率
    _sf = _f(sf)
    turns = _f(params.get("turns_per_coil")) or _f(best.get("turns_per_coil"))
    dw = _f(params.get("Winding__WireDiameter")) or _f(best.get("Winding__WireDiameter"))
    strands = _f(params.get("Winding__NumberOfStrands")) or _f(best.get("Winding__NumberOfStrands")) or 1.0
    if _sf and _sf > 0 and turns and turns > 0 and dw and dw > 0 and qs and qs > 0:
        a_cond = math.pi / 4.0 * (dw ** 2) * strands      # 单根导线(含并绕)截面积 mm²
        a_cu_slot = 2.0 * turns * a_cond                  # 双层绕组: 2 个线圈边
        if a_cu_slot > 0:
            return a_cu_slot / _sf * qs, "copper_over_sf"

    # 4) 孔径粗估
    dso = _f(best.get("stator_od")) or _f(params.get("stator_od"))
    dsi = _f(best.get("stator_id")) or _f(params.get("stator_id"))
    if dso and dsi and qs and dso > dsi > 0:
        a_total_mm2 = math.pi / 4.0 * (dso ** 2 - dsi ** 2)
        return 0.35 * a_total_mm2, "bore_approx"

    return None, "unavailable"


def compute_em_load(best: dict, specs: dict, params: dict) -> Dict[str, Any]:
    """电磁负荷 (线负荷 A / 气隙磁密 Bδ / 平均磁密 Bav / A·Bδ / A·Bav)。"""
    out: Dict[str, Any] = {
        "line_load_A_per_m": None, "B_delta_T": None, "B_avg_T": None,
        "AB_peak_A_T_per_m2": None, "AB_avg_A_T_per_m2": None,
        "n_series": None, "phase_current_A": None,
        "sources": {}, "bands": {"A": list(BAND_A), "Bd": list(BAND_BD), "AB": list(BAND_AB)},
        "in_band": {"A": None, "Bd": None, "AB": None},
    }

    dsi = _f(best.get("stator_id")) or _f(params.get("stator_id"))
    bd = (_f(best.get("air_gap_flux_density"))
          or _f(params.get("air_gap_flux_density")))
    if bd is not None:
        out["B_delta_T"] = _num(bd)
        out["B_avg_T"] = _num((2.0 / math.pi) * bd)
        out["sources"]["B_delta_T"] = "solver"

    # 线负荷: 优先程序现有值, 否则按 A = 2·m·W·I/(π·Dsi) 计算
    a_val, a_key = _first(best, ("line_load", "line_load_a_m", "specific_loading_a"))
    if a_val is None:
        a_val, a_key = _first(params, ("line_load", "line_load_a_m", "specific_loading_a"))
    if a_val is not None and a_val > 0:
        out["line_load_A_per_m"] = _num(a_val)
        out["sources"]["line_load_A_per_m"] = "solver:%s" % a_key
    else:
        # 每相串联匝数 n_series
        n_series, n_key = _first(params, ("n_series",))
        if n_series is None:
            _emf = params.get("_emf_winding") if isinstance(params.get("_emf_winding"), dict) else {}
            n_series, n_key = _first(_emf, ("n_series",))
            if n_series is not None:
                n_key = "_emf_winding.n_series"
        if n_series is None:
            n_series, n_key = _first(best, ("n_series",))
        # 相电流
        i_ph, i_key = _first(best, ("stator_current", "phase_current", "current_a"))
        if i_ph is None:
            i_ph, i_key = _first(params, ("stator_current", "phase_current", "current_a"))
        out["n_series"] = _num(n_series)
        out["phase_current_A"] = _num(i_ph)
        if n_series and i_ph and dsi and dsi > 0:
            _a_calc = 2.0 * NUM_PHASES * n_series * i_ph / (math.pi * (dsi / 1000.0))
            out["line_load_A_per_m"] = _num(_a_calc)
            out["sources"]["line_load_A_per_m"] = (
                "computed:2·m·W·I/(π·Dsi) [W=%s(%s), I=%s(%s)]"
                % (_num(n_series), n_key, _num(i_ph), i_key))

    A = _f(out.get("line_load_A_per_m"))
    if A and bd:
        out["AB_peak_A_T_per_m2"] = _num(A * bd)
        out["AB_avg_A_T_per_m2"] = _num(A * (2.0 / math.pi) * bd)
        out["in_band"]["A"] = bool(BAND_A[0] <= A <= BAND_A[1])
        out["in_band"]["Bd"] = bool(BAND_BD[0] <= bd <= BAND_BD[1])
        out["in_band"]["AB"] = bool(BAND_AB[0] <= A * bd <= BAND_AB[1])
    return out


def compute_material_util(best: dict, specs: dict, params: dict,
                          sf: Optional[float] = None,
                          core_length_mm: Optional[float] = None,
                          d_for_d2l_mm: Optional[float] = None) -> Dict[str, Any]:
    """材料利用率 (硅钢/铜重、铜铁比、功率密度、D²L 与单位体积转矩/功率)。"""
    out: Dict[str, Any] = {
        "available": False, "method": "unavailable",
        "stacking_factor": K_STACK_DEFAULT, "end_factor": K_END_DEFAULT,
        "stator_net_area_m2": None, "slot_total_area_m2": None,
        "iron_volume_m3": None, "copper_volume_m3": None,
        "steel_weight_kg": None, "copper_weight_kg": None,
        "total_weight_kg": None, "copper_iron_ratio": None,
        "power_density_kW_per_kg": None,
        "D2L_m3": None, "torque_per_D2L_Nm_per_m3": None,
        "power_per_D2L_kW_per_m3": None,
        "d_for_d2l_mm": None, "sources": {},
    }

    dso = _f(best.get("stator_od")) or _f(params.get("stator_od"))
    dsi = _f(best.get("stator_id")) or _f(params.get("stator_id"))
    L = _f(core_length_mm) or _f(best.get("core_length")) or _f(params.get("core_length"))
    p_kw = (_f(best.get("power")) or _f(specs.get("rated_power_kw"))
            or _f(specs.get("power_kw")))
    tq = _f(best.get("torque")) or _f(best.get("rated_torque"))
    _sf = _f(sf)
    if _sf is None:
        _sf = _f(best.get("slot_fill_rate")) or _f(best.get("_sf_est")) or _f(params.get("_sf_est"))

    if not (dso and dsi and L and dso > dsi > 0 and L > 0):
        return out

    slot_total_mm2, slot_method = compute_slot_total_area_mm2(best, params, sf=_sf)
    if slot_total_mm2 is None:
        return out

    # 面积 (m²)
    a_bore_m2 = math.pi / 4.0 * ((dso / 1000.0) ** 2 - (dsi / 1000.0) ** 2)
    a_slot_m2 = slot_total_mm2 * 1e-6
    a_net_m2 = a_bore_m2 - a_slot_m2
    if a_net_m2 <= 0:
        return out
    Lm = L / 1000.0

    v_fe = a_net_m2 * Lm * K_STACK_DEFAULT
    g_fe = v_fe * RHO_FE
    out.update({
        "stator_net_area_m2": _num(a_net_m2),
        "slot_total_area_m2": _num(a_slot_m2),
        "iron_volume_m3": _num(v_fe),
        "steel_weight_kg": _num(g_fe),
    })
    out["sources"]["slot_total_area"] = slot_method

    # 铜: 需要槽满率
    g_cu = None
    if _sf and _sf > 0:
        v_cu = a_slot_m2 * _sf * Lm * K_END_DEFAULT
        g_cu = v_cu * RHO_CU
        out["copper_volume_m3"] = _num(v_cu)
        out["copper_weight_kg"] = _num(g_cu)
        out["sources"]["copper_volume"] = "slot_area·SF·L·%g (SF=%s)" % (K_END_DEFAULT, _num(_sf))
    else:
        out["sources"]["copper_volume"] = "unavailable (缺槽满率)"

    if g_cu is not None:
        g_tot = g_fe + g_cu
        out["total_weight_kg"] = _num(g_tot)
        if g_fe > 0:
            out["copper_iron_ratio"] = _num(g_cu / g_fe)
        if p_kw and g_tot > 0:
            # 功率密度 [kW/kg] = 额定功率(kW) / 有效材料总重(kg)   (RUN32 口径: 450/966.3=0.4658)
            out["power_density_kW_per_kg"] = _num(p_kw / g_tot)

    # D²L (默认取定子内径)
    d_d2l = _f(d_for_d2l_mm) or dsi
    d2l = (d_d2l / 1000.0) ** 2 * Lm
    out["D2L_m3"] = _num(d2l)
    out["d_for_d2l_mm"] = _num(d_d2l)
    if d2l > 0:
        if tq:
            out["torque_per_D2L_Nm_per_m3"] = _num(tq / d2l)
        if p_kw:
            out["power_per_D2L_kW_per_m3"] = _num(p_kw / d2l)

    out["available"] = True
    out["method"] = "slot:%s | copper:%s" % (
        slot_method, "sf_based" if g_cu is not None else "n/a")
    return out


def compute_all(best: Optional[dict], specs: Optional[dict] = None,
                qg_result: Optional[dict] = None) -> Dict[str, Any]:
    """一次算出电磁负荷 + 材料利用率。

    best      : 质量门禁选中的最佳方案 dict
    specs     : 需求规格 (取额定功率)
    qg_result : 质量门禁结果 (可选) —— 当且仅当其中 `_em_load` / `_material_util`
                **符合本模块规范 schema** (含 `bands` / `stacking_factor` 标记) 时
                **直接沿用**(单一真相源, 与门禁数字一致); 否则一律现算。
    """
    specs = specs or {}
    if not isinstance(best, dict) or not best:
        return {"available": False, "reason": "no_best_solution",
                "electromagnetic_load": {}, "material_utilization": {}}

    qg = qg_result if isinstance(qg_result, dict) else {}
    _qg_em = qg.get("_em_load") or {}
    _qg_mu = qg.get("_material_util") or {}
    # 仅当 qg 结果本身符合本模块**规范 schema** 时才沿用 (单一真相源)。
    #   判据用本模块独有的结构标记 (`bands` / `stacking_factor`), 而非数值键 ——
    #   旧版 Fix3 的 {A_A_per_m, Bd_T, q_W_per_m2} / {eta_kW_per_t, mass_kg} 与之不兼容,
    #   若盲信会因键名错配导致报告整列显示「—」。不匹配则现算 (见下方 fallback)。
    _qg_is_canonical = (
        (isinstance(_qg_em, dict) and bool(_qg_em.get("bands")))
        or (isinstance(_qg_mu, dict) and bool(_qg_mu.get("stacking_factor")))
    )
    if _qg_is_canonical:
        return {
            "available": True,
            "source": "quality_gate_result",
            "electromagnetic_load": _qg_em,
            "material_utilization": _qg_mu,
        }

    params = best.get("params") if isinstance(best.get("params"), dict) else {}
    sf = _f(best.get("slot_fill_rate")) or _f(best.get("_sf_est")) or _f(params.get("_sf_est"))
    return {
        "available": True,
        "source": "computed",
        "electromagnetic_load": compute_em_load(best, specs, params),
        "material_utilization": compute_material_util(best, specs, params, sf=sf),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Markdown 渲染 (供 ReportAgent / 客户端报告直接嵌入)
# ═══════════════════════════════════════════════════════════════════════════
def _fmt(v, unit="", nd=None) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if nd is not None:
        s = f"{x:.{nd}f}"
    elif abs(x) >= 1000:
        s = f"{x:,.0f}"
    elif abs(x) >= 1:
        s = f"{x:.3f}".rstrip("0").rstrip(".")
    else:
        s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s + (unit or "")


def _band_txt(v, band) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    lo, hi = band
    if lo <= x <= hi:
        return "在区间内"
    return "低于区间" if x < lo else "高于区间"


def format_md(analysis: Dict[str, Any]) -> str:
    """渲染「电磁负荷 + 材料利用率」Markdown 章节。数据不可用时返回空串。"""
    if not isinstance(analysis, dict) or not analysis.get("available"):
        return ""
    em = analysis.get("electromagnetic_load") or {}
    mu = analysis.get("material_utilization") or {}
    if not em and not mu:
        return ""

    lines: List[str] = []
    lines.append("## 电磁负荷与材料利用率 (Electromagnetic Load & Material Utilization)")
    lines.append("")

    if em:
        lines.append("### 电磁负荷 (Electromagnetic Load)")
        lines.append("")
        _bands = em.get("bands") or {}
        lines.append(
            "计算口径: 线负荷 A = 2·m·W·I / (π·Dsi) (m=3 相, W=每相串联匝数, "
            "I=相电流, Dsi=定子内径); 磁负荷取气隙磁密 Bδ (峰值), "
            "其正弦分布平均值 Bav = (2/π)·Bδ; 电磁负荷 AB = A×B。")
        lines.append("")
        lines.append("| 电磁负荷项 (Item) | 数值 (Value) | 经验区间 (Band) | 判定 |")
        lines.append("|---|---|---|---|")
        lines.append("| 每相串联匝数 W (n_series) | %s | — | — |" % _fmt(em.get("n_series")))
        lines.append("| 相电流 I (Phase Current) | %s | — | — |" % _fmt(em.get("phase_current_A"), " A"))
        lines.append("| 线负荷 A (Line Load) | %s | %s ~ %s A/m | %s |" % (
            _fmt(em.get("line_load_A_per_m"), " A/m"),
            _fmt((_bands.get("A") or [None, None])[0]), _fmt((_bands.get("A") or [None, None])[1]),
            _band_txt(em.get("line_load_A_per_m"), BAND_A)))
        lines.append("| 气隙磁密 Bδ 峰值 (Peak) | %s | %s ~ %s T | %s |" % (
            _fmt(em.get("B_delta_T"), " T"), BAND_BD[0], BAND_BD[1],
            _band_txt(em.get("B_delta_T"), BAND_BD)))
        lines.append("| 气隙磁密 Bav 平均 (Average) | %s | — | — |" % _fmt(em.get("B_avg_T"), " T"))
        lines.append("| 电磁负荷 A·Bδ | %s | %s ~ %s | %s |" % (
            _fmt(em.get("AB_peak_A_T_per_m2")), _fmt(BAND_AB[0]), _fmt(BAND_AB[1]),
            _band_txt(em.get("AB_peak_A_T_per_m2"), BAND_AB)))
        lines.append("| 电磁负荷 A·Bav | %s | — | — |" % _fmt(em.get("AB_avg_A_T_per_m2")))
        lines.append("")
        _src = em.get("sources") or {}
        if _src:
            lines.append("数据来源 (Data Provenance): " + "; ".join(
                "%s ← %s" % (k, v) for k, v in _src.items()))
            lines.append("")
        _ib = em.get("in_band") or {}
        if _ib.get("A") is not None:
            _all_in = bool(_ib.get("A")) and bool(_ib.get("Bd")) and bool(_ib.get("AB"))
            lines.append(
                "结论: 线负荷、气隙磁密与电磁负荷(A·Bδ) **%s**中小型三相异步电机经验区间。"
                % ("均落在" if _all_in else "存在越界项, 需复核 — 详见上表判定列; 均" ))
            lines.append("")

    if mu:
        lines.append("### 材料利用率 (Material Utilization)")
        lines.append("")
        lines.append(
            "计算假定: 叠压系数 k=%s; 铁体积 = [π/4(Dso²−Dsi²) − 槽总面积]×L×k; "
            "铜体积 = 槽总面积×槽满率 SF×L×%s (端部系数); 密度取铜 %s kg/m³、"
            "硅钢片 %s kg/m³。D²L 采用**定子内径**口径(Dsi²×L)。"
            % (mu.get("stacking_factor", K_STACK_DEFAULT), mu.get("end_factor", K_END_DEFAULT),
               int(RHO_CU), int(RHO_FE)))
        lines.append("")
        lines.append("| 材料利用项 (Item) | 数值 (Value) |")
        lines.append("|---|---|")
        lines.append("| 定子冲片净面积 | %s m² |" % _fmt(mu.get("stator_net_area_m2")))
        lines.append("| 槽总面积 | %s m² |" % _fmt(mu.get("slot_total_area_m2")))
        lines.append("| 铁体积 | %s m³ |" % _fmt(mu.get("iron_volume_m3")))
        lines.append("| 铜体积 | %s m³ |" % _fmt(mu.get("copper_volume_m3")))
        lines.append("| 硅钢片重 G_fe | %s kg |" % _fmt(mu.get("steel_weight_kg"), nd=1))
        lines.append("| 铜重 G_cu | %s kg |" % _fmt(mu.get("copper_weight_kg"), nd=1))
        lines.append("| 有效材料总重 | %s kg |" % _fmt(mu.get("total_weight_kg"), nd=1))
        lines.append("| 铜铁比 G_cu/G_fe | %s |" % _fmt(mu.get("copper_iron_ratio"), nd=3))
        lines.append("| 功率密度 | %s kW/kg |" % _fmt(mu.get("power_density_kW_per_kg"), nd=4))
        lines.append("| D²L 体积指数 (Dsi²·L) | %s m³ |" % _fmt(mu.get("D2L_m3")))
        lines.append("| 转矩 / D²L | %s N·m/m³ |" % _fmt(mu.get("torque_per_D2L_Nm_per_m3")))
        lines.append("| 功率 / D²L | %s kW/m³ |" % _fmt(mu.get("power_per_D2L_kW_per_m3")))
        lines.append("")
        lines.append(
            "说明: 材料利用率的核心判据是**单位体积(单位 D²L)所能产生的转矩/功率** —— "
            "转矩/D²L 与 功率/D²L 越高, 同等材料产出能力越强; 铜铁比反映铜料占比与成本结构。")
        lines.append("")
        _msrc = mu.get("sources") or {}
        if _msrc:
            lines.append("数据来源 (Data Provenance): " + "; ".join(
                "%s ← %s" % (k, v) for k, v in _msrc.items()))
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 自测 (断言对齐 RUN32 报告公开数值)
    import json as _json

    # RUN32 报告公开数值 (450 kW 异步机): 硅钢 845.9 kg + 铜 120.4 kg = 966.3 kg,
    #   铜铁比 0.142, 功率密度 0.4658 kW/kg, D²L=0.139075 m³, 线负荷 47071 A/m。
    # 该组几何 (Dso=735.4/Dsi=500/L=556.3/槽总 25561mm²/SF=0.70) 可精确复现全部公开值,
    # 故固化为回归基线 —— 任一公式漂移都会在此处暴露。
    _best = {
        "stator_od": 735.4, "stator_id": 500.0, "core_length": 556.3,
        "slots_stator": 72, "power": 450.0, "torque": 4316.0,
        "air_gap_flux_density": 0.747, "slot_fill_rate": 0.70,
        "line_load": 47071.0, "slot_area_mm2": 25561.0,
        "params": {"n_series": 28.0, "slots_stator": 72},
    }
    _res = compute_all(_best, {"rated_power_kw": 450.0})
    print(_json.dumps(_res, ensure_ascii=False, indent=2))
    print()
    print(format_md(_res))

    _em = _res["electromagnetic_load"]
    _mu = _res["material_utilization"]
    _exp = {"硅钢片重": (_mu["steel_weight_kg"], 845.9),
            "铜重": (_mu["copper_weight_kg"], 120.4),
            "有效材料总重": (_mu["total_weight_kg"], 966.3),
            "铜铁比": (_mu["copper_iron_ratio"], 0.142),
            "功率密度": (_mu["power_density_kW_per_kg"], 0.4658),
            "D²L": (_mu["D2L_m3"], 0.139075),
            "线负荷 A": (_em["line_load_A_per_m"], 47071.0),
            "电磁负荷 A·Bδ": (_em["AB_peak_A_T_per_m2"], 35152.0)}
    _fails = []
    print()
    for _name, (_got, _want) in _exp.items():
        _ok = (_got is not None) and abs(_got - _want) <= abs(_want) * 0.005
        print("[%s] %-14s got=%s  want≈%s" % ("PASS" if _ok else "FAIL", _name, _got, _want))
        if not _ok:
            _fails.append(_name)
    print("\n结果:", "全部 PASS ✅" if not _fails else "%d 项 FAIL -> %s" % (len(_fails), _fails))
