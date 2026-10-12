# -*- coding: utf-8 -*-
"""DSH KB 上下文模块（M1 / M11, v4.02.75）。

从 ``Knowledge/design_rules.json`` 加载：
  - ``efficient_param_domains``  : 按应用场景的高效参数域（外径/内径/铁心长/极数/电压/效率目标）
  - ``lambda_ranges``            : 按极数的长径比
  - ``physics_laws``             : T=9550·P/n、D²L 等物理定律
  - ``dsh_design_ranges``        : DSH 专用动态尺寸/电磁载荷区间（转矩密度、线负荷、气隙磁密、电密等）

为提示词生成【随功率-转速动态生成的几何量级区间】与【行业电磁载荷区间】。
KB 不可用时回退到内置默认（镜像 dsh_design_ranges.generic），保证提示词永不因 KB 缺失崩溃；
此时 ``kb_available=False`` 供前端/日志展示。

导出：``KB_AVAILABLE`` / ``KB_VERSION`` / ``get_kb_meta()`` / ``get_geometry_constraints(specs)``。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

_KB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Knowledge", "design_rules.json")

# 内置默认（KB 缺失时的最后兜底，镜像 dsh_design_ranges.generic；仅作安全网）
_FALLBACK = {
    "stator_od_range": [120, 560],
    "stator_id_range": [67, 360],
    "core_length_range": [65, 520],
    "air_gap_mm": [0.3, 3.0],
    "slots_stator": [12, 96],
    "poles_preferred": [2, 4, 6, 8],
    "voltage_v": 380,
    "efficiency_target": 0.87,
    "torque_density_nm_per_l": 45.0,
    "od_band_ratio": [0.82, 1.18],
    "id_fill_ratio": 0.62,
    "lambda": [0.5, 1.5],
    "line_load_A_A_per_m": [20000, 60000],
    "air_gap_flux_density_T": [0.55, 0.95],
    "current_density_j1_A_per_mm2": [3.0, 8.0],
}

# 应用关键词 -> efficient_param_domains 键（命中即选该域）
_APP_KEYWORDS = {
    "船舶推进": ["船舶", "船推", "船用", "推进", "marine", "propulsion", "推进电机"],
    "电动汽车": ["电动", "ev", "新能源汽车", "车用", "驱动电机"],
    "叉车": ["叉车", "forklift"],
    "工业泵": ["泵", "pump", "水泵", "工业泵"],
    "通用": [],
}


def _load_kb() -> Dict[str, Any]:
    try:
        with open(_KB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_KB = _load_kb()
KB_AVAILABLE = bool(_KB)
KB_VERSION = (_KB.get("_meta", {}) or {}).get("version", "unknown") if _KB else "n/a"

# ── v4.02.103 (P3-A): λ 单一真源出口 ─────────────────────────────────────────
#   原实现直接读 _KB["lambda_ranges"] (design_rules.json 副本, 与
#   physics_kernel.LAMBDA_RANGE 历史同值)。现改为优先委托 kernel 权威,
#   KB 副本仅作 kernel 不可导入时的兜底 —— 消除并列真源。
try:
    import sys as _sys
    _SC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Scripts")
    if _SC not in _sys.path:
        _sys.path.insert(0, _SC)
    from physics_kernel import lambda_band_for_poles as _PK_lambda_band_for_poles
except Exception:
    _PK_lambda_band_for_poles = None


def get_kb_meta() -> Dict[str, Any]:
    """供状态/可观测性接口读取 KB 元信息（M9 复用）。"""
    return {
        "kb_available": KB_AVAILABLE,
        "kb_version": KB_VERSION,
        "kb_path": _KB_PATH if KB_AVAILABLE else None,
    }


def _detect_application(specs: Dict[str, Any]) -> str:
    """从需求原文 / 结构化字段检测应用场景，返回 efficient_param_domains 的键。"""
    raw = ""
    try:
        raw = str(specs.get("raw_text") or specs.get("requirement") or "")
        raw += " " + json.dumps(specs, ensure_ascii=False)
    except Exception:
        raw = str(specs.get("raw_text", ""))
    low = raw.lower()
    for app, kws in _APP_KEYWORDS.items():
        if app == "通用":
            continue
        if any(kw.lower() in low for kw in kws):
            return app
    return "通用"


def _fnum(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _nearest_poles_lambda(poles) -> List[float]:
    """返回与给定极数最匹配的 lambda 区间 [lo, hi]。

    v4.02.103 (P3-A): 优先委托 physics_kernel.lambda_band_for_poles (单一真源);
    kernel 不可用时回退 design_rules.json::lambda_ranges 副本。
    """
    if isinstance(poles, (list, tuple)) and poles:
        p = poles[0]
    else:
        p = poles
    if _PK_lambda_band_for_poles is not None:
        try:
            _band = _PK_lambda_band_for_poles(p)
            if _band:
                return [float(_band[0]), float(_band[1])]
        except Exception:
            pass
    lam = (_KB.get("lambda_ranges", {})) if _KB else {}
    if not lam:
        return list(_FALLBACK["lambda"])
    key = str(p)
    if key in lam and isinstance(lam[key], list):
        return list(lam[key])
    return list(lam.get("default", _FALLBACK["lambda"]))


def get_geometry_constraints(specs: Dict[str, Any]) -> Dict[str, Any]:
    """构建 DSH 提示词的动态几何 + 电磁载荷区间（M1/M11 核心出口）。

    返回 dict（含来源标记与回退标志）；``nominal_od_mm`` 为按 P/n 反推的名义外径
    （无功率/转速时为 None）。
    """
    app = _detect_application(specs)
    domains = (_KB.get("efficient_param_domains", {})) if _KB else {}
    domain = domains.get(app, domains.get("通用", {}))
    sizing = (_KB.get("dsh_design_ranges", {})) if _KB else {}
    generic = sizing.get("generic", {}) or {}
    by_app = sizing.get("by_application", {}).get(app, {}) or {}

    def pick(domain_key, generic_key, default):
        if domain and domain_key in domain:
            return domain[domain_key]
        if generic_key in generic:
            return generic[generic_key]
        return default

    od = list(pick("stator_od_range", "stator_od_range", _FALLBACK["stator_od_range"]))
    sid = list(pick("stator_id_range", "stator_id_range", _FALLBACK["stator_id_range"]))
    cl = list(pick("core_length_range", "core_length_range", _FALLBACK["core_length_range"]))
    poles = pick("poles_preferred", "poles_preferred", _FALLBACK["poles_preferred"])
    volt = pick("voltage_v", "voltage_v", _FALLBACK["voltage_v"])
    eff = pick("efficiency_target", "efficiency_target", _FALLBACK["efficiency_target"])

    load = {
        "air_gap_range": list(generic.get("air_gap_mm", _FALLBACK["air_gap_mm"])),
        "slots_range": list(generic.get("slots_stator", _FALLBACK["slots_stator"])),
        "line_load_A_A_per_m": list(generic.get("line_load_A_A_per_m", _FALLBACK["line_load_A_A_per_m"])),
        "air_gap_flux_density_T": list(generic.get("air_gap_flux_density_T", _FALLBACK["air_gap_flux_density_T"])),
        "current_density_j1_A_per_mm2": list(generic.get("current_density_j1_A_per_mm2", _FALLBACK["current_density_j1_A_per_mm2"])),
    }

    # --- 由 P/n 反推动态名义外径（M11：随功率-转速动态）---
    P = _fnum(specs.get("power_kw") or specs.get("power"))
    n = _fnum(specs.get("speed_rpm") or specs.get("speed"))
    nominal_od = None
    if P and n:
        try:
            T = 9550.0 * P / n  # N·m
            rho = float(by_app.get("torque_density_nm_per_l",
                                  generic.get("torque_density_nm_per_l",
                                              _FALLBACK["torque_density_nm_per_l"])))
            lam_lo = _nearest_poles_lambda(poles)[0]
            if rho and lam_lo:
                vol = T / rho  # L（= dm^3）
                od_nom_dm = (4.0 * vol / (3.141592653589793 * lam_lo)) ** (1.0 / 3.0)
                nominal_od = od_nom_dm * 100.0  # -> mm
                lo, hi = generic.get("od_band_ratio", _FALLBACK["od_band_ratio"])
                od = [round(nominal_od * lo), round(nominal_od * hi)]
                idf = float(generic.get("id_fill_ratio", _FALLBACK["id_fill_ratio"]))
                sid = [round(nominal_od * idf * 0.8), round(nominal_od * idf * 1.1)]
                cl = [round(nominal_od * lam_lo * 0.8), round(nominal_od * lam_lo * 1.2)]
        except Exception:
            nominal_od = None

    source = "kb" if KB_AVAILABLE else "fallback"
    return {
        "application": app,
        "kb_available": KB_AVAILABLE,
        "stator_od_range": od,
        "stator_id_range": sid,
        "core_length_range": cl,
        "air_gap_range": load["air_gap_range"],
        "slots_range": load["slots_range"],
        "poles_preferred": poles,
        "voltage_v": volt,
        "efficiency_target": eff,
        "line_load_A_A_per_m": load["line_load_A_A_per_m"],
        "air_gap_flux_density_T": load["air_gap_flux_density_T"],
        "current_density_j1_A_per_mm2": load["current_density_j1_A_per_mm2"],
        "nominal_od_mm": nominal_od,
        "source": source,
    }
