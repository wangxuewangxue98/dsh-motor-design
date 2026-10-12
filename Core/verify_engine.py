"""
正侧分层核验引擎 (V2.1 Phase 0 · 纯新增, 接口不变)

对应设计方案书《AI 混合驱动方案生成引擎 V2.1-Analysis》:
  §3.3 步骤 4  紧急几何自检 (旁路专属硬过滤, 防 L0 崩溃)
  §3.3 步骤 7  正侧分层核验 (0%/+5%/+10%/+15% 差异化阈值 + 系列代际/防护校验)
  TABLE 4      四路径固定约束与正侧放宽比例
  TABLE 8      Phase 0 验收 = 能对固定参数输出 passed / warning / failed

与 Core/quality_gate.py 的职责分工 (方案书 8.4 风险条款, 避免双重判定冲突):
  quality_gate  : 评审置信度 + 全局硬约束 (efficiency_min / temp_max / slot_fill_max)
                  —— 面向"决策门是否放行", 本模块不介入
  verify_engine : 按方案**来源路径**差异化的几何/物理一致性核验 + 系列代际/防护校验
                  —— 面向"单个方案可信度打标", 仅向方案 dict 追加字段

接口承诺 (方案书约束 B, 纯加法):
  verify_batch() 就地为每个方案 dict 追加 verify_status / verify_detail / source_path,
  不删除、不改写任何既有字段 (efficiency / power_factor / torque / _physics ...)。
  下游 review_solutions / convergence_review 读取逻辑零改动。

典型用法::

    from Core.verify_engine import verify_batch, filter_by_geometry, SOURCE_MAIN_A

    kept, rejected = filter_by_geometry(designs)          # L0 之前硬过滤
    verify_batch(results, specs=specs)                    # L1 之后打标
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 来源路径常量 (TABLE 4)
# ─────────────────────────────────────────────────────────────
SOURCE_MAIN_A = "main_a"        # 主线 A: 型谱/案例种子衍生, 冲片几何全固定
SOURCE_BYPASS_B1 = "bypass_b1"  # 旁路 B1: 固定定子外径/内径 + 转子内径
SOURCE_BYPASS_B2 = "bypass_b2"  # 旁路 B2: 固定定子外径 + 转子内径
SOURCE_BYPASS_B3 = "bypass_b3"  # 旁路 B3: 无圆约束, 全自由
SOURCE_FALLBACK = "fallback"    # 纯物理降级兜底 (步骤 10)

ALL_SOURCE_PATHS = (SOURCE_MAIN_A, SOURCE_BYPASS_B1, SOURCE_BYPASS_B2, SOURCE_BYPASS_B3)

# 正侧放宽比例 (TABLE 4 "正侧放宽"列)
RELAX_BY_PATH: Dict[str, float] = {
    SOURCE_MAIN_A: 0.00,
    SOURCE_BYPASS_B1: 0.05,
    SOURCE_BYPASS_B2: 0.10,
    SOURCE_BYPASS_B3: 0.15,
    SOURCE_FALLBACK: 0.10,
}

# 各路径"必须固定"的几何项 (TABLE 4 "固定约束"列)
#   主线 A : 定/转子冲片几何全固定 (可变仅叠长/匝数/线径/磁钢/气隙)
#   B1     : 定子外径 / 定子内径 / 转子外径 / 转子内径(轴径)
#   B2     : 定子外径 / 转子内径(轴径)
#   B3     : (无) — 去种子化(v2): 全自由, 不锚定任何种子键 (含 shaft_dia)
# v4.02.13: 主线A补槽形(stator_slot_type/rotor_slot_type); B1补rotor_od; B3原补shaft_dia
#   20260902 去种子化: B3 释放 shaft_dia, 与 _BYPASS_FIXED_KEYS["bypass_b3"]=() 一致
FIXED_KEYS_BY_PATH: Dict[str, Tuple[str, ...]] = {
    SOURCE_MAIN_A: ("stator_od", "stator_id", "rotor_od", "shaft_dia",
                    "slots_stator", "slots_rotor", "stator_slot_type",
                    "rotor_slot_type", "tooth_width", "yoke_thickness", "poles"),
    SOURCE_BYPASS_B1: ("stator_od", "stator_id", "rotor_od", "shaft_dia"),
    SOURCE_BYPASS_B2: ("stator_od", "shaft_dia"),
    SOURCE_BYPASS_B3: (),  # 去种子化(v2): B3 全自由, 不锚定任何种子键 (含 shaft_dia)
    SOURCE_FALLBACK: ("stator_od",),
}

# TEXT 类固定项: 不参与数值偏差, 只做相等性判定 (v4.02.13 加入主线A固定项)
_TEXT_FIXED_KEYS = ("stator_slot_type", "rotor_slot_type")

# 核验结论三态
STATUS_PASSED = "passed"
STATUS_WARNING = "warning"
STATUS_FAILED = "failed"

# ─────────────────────────────────────────────────────────────
# 默认核验配置 (运行时可由调用方覆盖)
# ─────────────────────────────────────────────────────────────
DEFAULT_VERIFY_CONFIG: Dict[str, Any] = {
    # 数值噪声容差: 主线 A 放宽 0%, 但 round() 会带来微小误差, 给 0.5% 的噪声带
    "numeric_noise_tol": 0.005,
    # 超出放宽 × 该倍数 → 判 failed; 介于放宽与该倍数之间 → warning
    "fail_multiplier": 2.0,
    # failed 门槛下限: 主线 A 放宽 0% 时 fail 阈会退化到 1%(噪声×2), 工程上过苛。
    # 冲片模具是刚性的 —— 0.5%~2% 视为圆整/工艺公差(warning), >2% 视为换了冲片(failed)。
    "min_fail_ratio": 0.02,
    # 通用物理合理区间 (几何自检硬门槛, 比 physics_kernel 的扫描区间略放宽以容纳旁路创新)
    "air_gap_min_mm": 0.15,
    "air_gap_max_mm": 3.00,
    "lambda_min": 0.25,          # λ = 铁心长度 / 定子内径
    "lambda_max": 4.50,
    "slot_depth_min_mm": 2.0,    # B1 槽面积校验: 最小槽深
    "slot_width_min_mm": 1.0,    # B1 槽面积校验: 最小槽口宽
    "slot_area_min_mm2": 20.0,   # B1 槽面积校验: 最小单槽面积
    "q_min": 0.5,                # 每极每相槽数下限 (三相)
    # B3 极数密度: 方案书"极数/外径<0.8", 外径以 **cm** 计 (mm 量纲下该式恒成立、检查失效)。
    #   poles / (od_mm/10) < 0.8  ⇔  poles < 0.08 × od_mm
    #   校核: od=260 → ≤20 极 (极距 π×170/20≈26.7mm, 合理); od=1000 → ≤80 极 (τ≈25.5mm, 合理)
    #   拦截: 48 极 /260mm → 1.85 ≥ 0.8, 极距仅 11.1mm, 漏磁严重且绕组难做 → 判 failed
    "pole_density_max": 0.8,
    "geom_ratio_tol_mm": 0.4,    # |stator_id - rotor_od - 2*air_gap| 允许残差 (#934 收紧: 抓 0.58 残差/容浮点~0.05)
    # 系列代际 / 防护校验 (软约束, 不匹配仅 warning)
    "frame_size_tol": 0.06,      # 推算中心高与标准序列偏差上限
    "enable_series_check": True,
}

# 标准中心高序列 (GB/T 4772 机座号)
STANDARD_FRAME_SIZES = (
    56, 63, 71, 80, 90, 100, 112, 132, 160, 180,
    200, 225, 250, 280, 315, 355, 400, 450,
)

# 型谱库路径 (与 physics_kernel.Y_DB_PATH 同源)
_Y_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Knowledge", "y_series_physics.json",
)
_CATALOG_CACHE: Optional[List[dict]] = None


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────
def get_verify_config(runtime_config: Optional[dict] = None) -> Dict[str, Any]:
    """读取核验配置: runtime_config['verify_engine'] 覆盖默认值, 返回浅拷贝"""
    cfg = dict(DEFAULT_VERIFY_CONFIG)
    if isinstance(runtime_config, dict) and isinstance(runtime_config.get("verify_engine"), dict):
        for k, v in runtime_config["verify_engine"].items():
            if k in cfg:
                cfg[k] = v
    return cfg


def _num(value: Any, default: float = 0.0) -> float:
    """宽松取数: 支持 int/float/纯数字字符串/带单位字符串; 失败返回 default"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    buf = []
    seen_dot = False
    for ch in text:
        if ch.isdigit():
            buf.append(ch)
        elif ch == "." and not seen_dot and buf:
            seen_dot = True
            buf.append(ch)
        elif ch == "-" and not buf:
            buf.append(ch)
        elif buf:
            break
    try:
        return float("".join(buf))
    except (TypeError, ValueError):
        return default


def _load_catalog() -> List[dict]:
    """加载 Y 系列型谱 catalog (65 条), 失败返回空表并降级为跳过系列校验"""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    try:
        with open(_Y_DB_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        catalog = data.get("catalog") if isinstance(data, dict) else None
        _CATALOG_CACHE = list(catalog) if isinstance(catalog, list) else []
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("[verify_engine] 型谱库加载失败, 系列校验降级跳过: %s", exc)
        _CATALOG_CACHE = []
    return _CATALOG_CACHE


def normalize_source_path(design: Optional[dict], fallback: str = SOURCE_FALLBACK) -> str:
    """
    从方案 dict 归一化来源路径。

    兼容多种写法: source_path / _source_path / _physics.source_path /
    quota_tag ("A"/"B1"/"B2"/"B3"), 未知一律回落 fallback。

    ⚠ 默认回落必须是 SOURCE_FALLBACK 而非 SOURCE_MAIN_A —— 这是向后兼容的关键:
      现状 physics_kernel.focused_scan 是"以型谱条目为中心 ±10%/±15% 扫描定子内外径
      与槽数", 语义上并**不满足**主线 A 的"冲片几何全固定(放宽 0%)"。实测 20/20 会被
      判 failed。在 Task #694(主线 A 衍生重构) 落地前, 未显式标注来源的方案一律按
      fallback(放宽 10%, 仅核验 stator_od) 判定, 避免接入即全灭。
      重构完成后, 真正"固定冲片 + 缩放叠长"的方案再显式打 source_path=main_a。
    """
    if not isinstance(design, dict):
        return fallback
    raw = (design.get("source_path")
           or design.get("_source_path")
           or (design.get("_physics") or {}).get("source_path")
           or design.get("quota_tag")
           or "")
    text = str(raw).strip().lower().replace("-", "_")
    if not text:
        return fallback
    if text in RELAX_BY_PATH:
        return text
    alias = {
        "a": SOURCE_MAIN_A, "main": SOURCE_MAIN_A, "mainline": SOURCE_MAIN_A,
        "main_line_a": SOURCE_MAIN_A, "主线a": SOURCE_MAIN_A, "主线": SOURCE_MAIN_A,
        "b1": SOURCE_BYPASS_B1, "旁路b1": SOURCE_BYPASS_B1,
        "b2": SOURCE_BYPASS_B2, "旁路b2": SOURCE_BYPASS_B2,
        "b3": SOURCE_BYPASS_B3, "旁路b3": SOURCE_BYPASS_B3,
        "physics": SOURCE_FALLBACK, "兜底": SOURCE_FALLBACK, "降级": SOURCE_FALLBACK,
    }
    return alias.get(text, fallback)


# ─────────────────────────────────────────────────────────────
# 步骤 4: 紧急几何自检 (旁路专属硬过滤)
# ─────────────────────────────────────────────────────────────
def geometry_self_check(design: dict,
                        source_path: Optional[str] = None,
                        config: Optional[dict] = None) -> Tuple[bool, List[str]]:
    """
    紧急几何自检 —— 在 L0 全员速算之前把不可解方案硬挡掉, 防止磁路法/求解器崩溃。

    分级规则 (方案书 §3.3 步骤 4):
      通用    : 径向套嵌顺序 / 气隙区间 / λ 区间 / 极槽配合 / 数值有效性
      B1 追加 : 槽面积校验 (槽深 / 槽口宽 / 单槽面积 下限)
      B2 追加 : 转子外径 < 定子内径 (严格)
      B3 追加 : 极数 / 定子外径 < 0.8 (极数密度)

    返回: (是否通过, 失败原因列表) —— 通过时原因列表为空
    """
    cfg = get_verify_config(config) if not isinstance(config, dict) or "numeric_noise_tol" not in config else config
    path = source_path or normalize_source_path(design)
    reasons: List[str] = []

    od = _num(design.get("stator_od"))
    sid = _num(design.get("stator_id"))
    rod = _num(design.get("rotor_od"))
    shaft = _num(design.get("shaft_dia"))
    length = _num(design.get("core_length"))
    gap = _num(design.get("air_gap"))
    poles = int(_num(design.get("poles")))
    slots_s = int(_num(design.get("slots_stator")))
    tooth = _num(design.get("tooth_width"))
    yoke = _num(design.get("yoke_thickness"))

    # ── 通用 1: 数值有效性 ──
    for label, value in (("定子外径", od), ("定子内径", sid), ("铁心长度", length)):
        if value <= 0:
            reasons.append(f"{label}无效({value})")
    if reasons:
        return False, reasons

    # ── 通用 2: 径向套嵌顺序 od > sid > rod > shaft > 0 ──
    if sid >= od:
        reasons.append(f"定子内径({sid})≥外径({od})")
    if rod > 0 and rod >= sid:
        reasons.append(f"转子外径({rod})≥定子内径({sid})")
    if shaft > 0 and rod > 0 and shaft >= rod:
        reasons.append(f"轴径({shaft})≥转子外径({rod})")

    # ── 通用 3: 气隙区间 + 与内外径的一致性 ──
    if gap > 0:
        if gap < cfg["air_gap_min_mm"] or gap > cfg["air_gap_max_mm"]:
            reasons.append(f"气隙({gap}mm)超出[{cfg['air_gap_min_mm']},{cfg['air_gap_max_mm']}]")
        if rod > 0 and abs(sid - rod - 2 * gap) > cfg["geom_ratio_tol_mm"]:
            reasons.append(f"气隙不自洽(定子内径{sid}-转子外径{rod}≠2×{gap})")

    # ── 通用 4: λ = L / D 区间 ──
    if sid > 0:
        lam = length / sid
        if lam < cfg["lambda_min"] or lam > cfg["lambda_max"]:
            reasons.append(f"长径比λ={lam:.2f}超出[{cfg['lambda_min']},{cfg['lambda_max']}]")

    # ── 通用 5: 极槽配合 ──
    if poles > 0:
        if poles % 2 != 0:
            reasons.append(f"极数({poles})非偶数")
        if slots_s > 0:
            q = slots_s / float(poles * 3)
            if q < cfg["q_min"]:
                reasons.append(f"每极每相槽数q={q:.2f}<{cfg['q_min']}")

    # ── B1 追加: 槽面积校验 ──
    if path in (SOURCE_BYPASS_B1, SOURCE_BYPASS_B2, SOURCE_BYPASS_B3) and slots_s > 0:
        slot_depth = (od / 2.0 - yoke) - (sid / 2.0)
        if slot_depth < cfg["slot_depth_min_mm"]:
            reasons.append(f"槽深({slot_depth:.2f}mm)<{cfg['slot_depth_min_mm']}mm(轭部过厚)")
        else:
            pitch = math.pi * (sid + slot_depth) / slots_s
            slot_width = pitch - tooth
            if slot_width < cfg["slot_width_min_mm"]:
                reasons.append(f"槽口宽({slot_width:.2f}mm)<{cfg['slot_width_min_mm']}mm(齿宽过大)")
            elif slot_depth * slot_width < cfg["slot_area_min_mm2"]:
                reasons.append(
                    f"单槽面积({slot_depth * slot_width:.1f}mm²)<{cfg['slot_area_min_mm2']}mm²"
                )

    # ── B2 追加: 转子外径 < 定子内径 (严格, 不容忍相等) ──
    if path in (SOURCE_BYPASS_B2, SOURCE_BYPASS_B3):
        if rod <= 0:
            reasons.append("缺转子外径, B2/B3 无法校验")
        elif rod >= sid:
            reasons.append(f"B2/B3 硬约束: 转子外径({rod})必须<定子内径({sid})")

    # ── B3 追加: 极数密度 poles / 外径(cm) < 0.8 ──
    if path == SOURCE_BYPASS_B3 and poles > 0:
        density = poles / (od / 10.0) if od > 0 else 999.0
        if density >= cfg["pole_density_max"]:
            tau = (math.pi * sid / poles) if sid > 0 else 0.0
            reasons.append(
                f"极数密度({density:.2f}极/cm)≥{cfg['pole_density_max']}"
                f"(极距仅{tau:.1f}mm, 极数过密)"
            )

    return (not reasons), reasons


def filter_by_geometry(designs: List[dict],
                       config: Optional[dict] = None,
                       verbose: bool = False) -> Tuple[List[dict], List[dict]]:
    """
    批量硬过滤: 合并入 L0 之前调用。

    返回 (kept, rejected)。rejected 中每项就地追加 _geom_reject_reasons,
    供步骤 9「失败子项独立重做」把结构化失败原因反馈给构思智能体。
    """
    cfg = get_verify_config(config)
    kept: List[dict] = []
    rejected: List[dict] = []
    for design in designs or []:
        if not isinstance(design, dict):
            continue
        path = normalize_source_path(design)
        ok, reasons = geometry_self_check(design, source_path=path, config=cfg)
        if ok:
            kept.append(design)
        else:
            design["_geom_reject_reasons"] = reasons
            design["_geom_reject_path"] = path
            rejected.append(design)
    if verbose and rejected:
        logger.info("[verify_engine] 几何自检剔除 %d/%d 组", len(rejected), len(designs or []))
        for item in rejected[:5]:
            logger.info("  · %s → %s", item.get("_geom_reject_path"), item.get("_geom_reject_reasons"))
    return kept, rejected


# ─────────────────────────────────────────────────────────────
# 系列代际 / 防护校验 (软约束)
# ─────────────────────────────────────────────────────────────
def infer_frame_size(stator_od_mm: float) -> Tuple[Optional[int], float]:
    """
    由定子外径反推机座号(中心高), 返回 (最接近标准机座号, 相对偏差)。

    经验关系: 定子外径 ≈ 中心高 × 1.5 (Y 系列 65 条型谱统计中位值)。
    偏差 = |推算中心高 - 标准值| / 标准值
    """
    if stator_od_mm <= 0:
        return None, 1.0
    est = stator_od_mm / 1.5
    best, best_dev = None, 1.0
    for std in STANDARD_FRAME_SIZES:
        dev = abs(est - std) / float(std)
        if dev < best_dev:
            best, best_dev = std, dev
    return best, best_dev


def find_series_reference(design: dict, specs: Optional[dict] = None) -> Optional[dict]:
    """
    在型谱 catalog 中找与方案最接近的条目 (优先同极数, 再按外径+铁长距离)。

    仅用于系列代际/防护校验与缺参考时的兜底基准, 不参与主线固定项判定。
    """
    catalog = _load_catalog()
    if not catalog:
        return None
    od = _num(design.get("stator_od"))
    length = _num(design.get("core_length"))
    poles = int(_num(design.get("poles")))
    pool = [e for e in catalog if int(_num(e.get("poles"))) == poles] if poles > 0 else []
    if not pool:
        pool = catalog
    best, best_score = None, float("inf")
    for entry in pool:
        d_od = abs(_num(entry.get("stator_od_mm")) - od) / max(od, 1.0)
        d_l = abs(_num(entry.get("core_length_mm")) - length) / max(length, 1.0)
        score = d_od * 2.0 + d_l
        if score < best_score:
            best, best_score = entry, score
    return best


def check_series_generation(design: dict,
                            specs: Optional[dict] = None,
                            reference: Optional[dict] = None,
                            config: Optional[dict] = None) -> List[str]:
    """
    系列代际 / 防护校验 (方案书 §3.3 步骤 7 后半)。

    校验三类软约束, 命中即返回 warning 文案 (不产生 failed):
      1. 机座号: 由定子外径推算的中心高是否落在 GB/T 4772 标准序列附近
      2. 防护/冷却: specs 指定的 protection_class / cooling 与最近型谱条目是否一致
      3. 能效代际: specs 指定的 efficiency_ie / insulation_class 与型谱是否一致
    """
    cfg = get_verify_config(config)
    notes: List[str] = []
    if not cfg.get("enable_series_check", True):
        return notes

    od = _num(design.get("stator_od"))
    frame, dev = infer_frame_size(od)
    if frame is None:
        notes.append("无法推算机座号(定子外径缺失)")
    else:
        # ── v4.02.103 (P3-B): 判据口径修正 ──────────────────────────────
        #   区分「超出标准序列范围」与「序列内偏离」两类情形。
        #   根因: GB/T 4772 标准机座号上限为 H450 (中心高 450mm, 对应定子外径≈675mm)。
        #         大机座 (>~450kW 级) 推算中心高天然 >450mm, 而原判据以「最近标准值
        #         H450」为基准计算偏差 → 必然报 30%+ 偏离, 属**伪告警**(判据不适配
        #         大机座, 并非设计缺陷)。实测: Dso=903.8mm → est 602mm → dev 33.9%
        #         (H450), 但该中心高本就无对应标准机座号可比。
        #   现口径:
        #     · est 超出 序列上限×(1+tol) → notice「按非标大机座处理, 不计机座偏离」
        #     · est 在序列范围内 且 dev>tol → 保留原 warning 文案 (真偏离)
        _est_mm = od / 1.5
        _series_max = max(STANDARD_FRAME_SIZES)
        _fs_tol = float(cfg.get("frame_size_tol", 0.06) or 0.06)
        if _est_mm > _series_max * (1.0 + _fs_tol):
            notes.append(
                f"定子外径{od:.0f}mm 推算中心高{_est_mm:.0f}mm 超出 GB/T 4772 标准序列"
                f"上限 H{_series_max}, 按非标大机座处理(不计机座偏离)")
        elif dev > _fs_tol:
            notes.append(f"定子外径{od:.0f}mm 推算中心高偏离标准机座号 H{frame}({dev * 100:.1f}%)")

    ref = reference if isinstance(reference, dict) else find_series_reference(design, specs)
    if not isinstance(ref, dict):
        return notes

    spec_map = {
        "protection_class": ("protection_class", "防护等级"),
        "cooling": ("cooling", "冷却方式"),
        "insulation_class": ("insulation_class", "绝缘等级"),
        "efficiency_ie": ("efficiency_ie", "能效等级"),
    }
    for spec_key, (ref_key, label) in spec_map.items():
        want = (specs or {}).get(spec_key)
        if not want:
            continue
        got = ref.get(ref_key)
        if not got:
            continue
        want_s, got_s = str(want).strip().upper(), str(got).strip().upper()
        if want_s and got_s and want_s not in got_s and got_s not in want_s:
            notes.append(f"{label}要求“{want}”与型谱{ref.get('model', '?')}的“{got}”不一致")
    return notes


# ─────────────────────────────────────────────────────────────
# 步骤 7: 正侧分层核验
# ─────────────────────────────────────────────────────────────
def _fixed_key_deviation(design: dict, reference: dict, key: str) -> Tuple[Optional[float], str]:
    """
    计算固定项相对偏差, 返回 (偏差比例, 不可判定原因)。

    两类不可判定必须区分 (否则主线 A 永远拿不到 passed):
      "reference" : 基准数据源不含该字段 —— 型谱库天然无 rotor_od/shaft_dia/tooth_width,
                    属数据源限制而非方案缺陷, **不降级**
      "design"    : 方案自身缺该字段 —— 属方案不完整, 降级 warning
    可判定时返回 (偏差, "")。
    """
    # v4.02.13: TEXT 类固定项(槽形)只做相等性判定, 不参与数值偏差
    if key in _TEXT_FIXED_KEYS:
        ref_alias_text = {
            "stator_slot_type": ("stator_slot_type",),
            "rotor_slot_type": ("rotor_slot_type",),
        }
        got = design.get(key)
        ref_val = None
        for alias in ref_alias_text.get(key, (key,)):
            if alias in reference and reference.get(alias) is not None:
                ref_val = reference.get(alias)
                break
        if ref_val is None:
            return None, "reference"   # 基准数据源无槽形 → 不降级
        if got is None:
            return None, "design"      # 方案自身缺槽形 → 降级 warning
        if str(got).strip() == str(ref_val).strip():
            return 0.0, ""             # 槽形一致 → 通过
        return 1.0, ""                 # 槽形偏离 → 判 failed

    ref_alias = {
        "stator_od": ("stator_od", "stator_od_mm"),
        "stator_id": ("stator_id", "stator_inner_dia_mm"),
        "rotor_od": ("rotor_od", "rotor_od_mm"),
        "shaft_dia": ("shaft_dia", "shaft_dia_mm", "rotor_inner_dia_mm"),
        "core_length": ("core_length", "core_length_mm"),
        "air_gap": ("air_gap", "air_gap_mm"),
        "slots_stator": ("slots_stator",),
        "slots_rotor": ("slots_rotor",),
        "tooth_width": ("tooth_width", "tooth_width_mm"),
        "yoke_thickness": ("yoke_thickness", "yoke_thickness_mm"),
        "poles": ("poles",),
    }
    got = _num(design.get(key))
    ref_val = 0.0
    for alias in ref_alias.get(key, (key,)):
        if alias in reference and reference.get(alias) is not None:
            ref_val = _num(reference.get(alias))
            if ref_val:
                break
    if ref_val <= 0:
        return None, "reference"
    if got <= 0:
        return None, "design"
    return abs(got - ref_val) / ref_val, ""


def verify_layered(design: dict,
                   specs: Optional[dict] = None,
                   reference: Optional[dict] = None,
                   source_path: Optional[str] = None,
                   config: Optional[dict] = None) -> Dict[str, Any]:
    """
    对单个方案执行正侧分层核验, 输出 passed / warning / failed 及明细。

    判定顺序:
      1. 紧急几何自检失败                        → failed
      2. 任一固定项偏差 > max(放宽×倍数, 门槛下限) → failed
      3. 任一固定项偏差 > 放宽                    → warning
      4. 无参考基准 / 方案自身缺固定项            → warning
         (基准数据源不含该项 → 仅记录, 不降级)
      5. 系列代际或防护校验命中                    → warning
      6. 其余                                     → passed

    返回 dict::

        {"verify_status": "passed",
         "source_path": "main_a",
         "relax_ratio": 0.0,
         "checked_keys": [...],
         "violations": [{"key":..., "deviation":..., "limit":..., "level":...}],
         "geometry_ok": True,
         "geometry_reasons": [],
         "series_notes": [],
         "reference_model": "Y2001-4",
         "summary": "..."}
    """
    cfg = get_verify_config(config)
    path = source_path or normalize_source_path(design)
    relax = RELAX_BY_PATH.get(path, 0.10)
    noise = cfg["numeric_noise_tol"]
    fail_mul = cfg["fail_multiplier"]
    # ★ P1 (2026-09-30 用户授权·规则优先): 固定键 relax→0
    #   旧口径 limit_warn=max(relax,noise) / limit_fail=max(2×relax, 2%) ⇒
    #   B1 5% / B2 10% 的路径容差把「规则不可调整」键的违约洗白成 passed
    #   (RUN65 实证: B1 Dso −2.5%、B2 −5.66% 全放行, 仅 main_a 0% 判 failed)。
    #   固定键=规则不可变量, 偏差一律按噪声带判定:
    #     > noise(0.5%)                       → warning (圆整/工艺公差带)
    #     > max(2×noise, min_fail_ratio=2%)   → failed  (视为换了冲片)
    #   (与 DEFAULT_VERIFY_CONFIG 注释的「冲片模具刚性」工程口径一致;
    #    RELAX_BY_PATH 保留向后兼容——normalize_source_path 仍用它识别路径,
    #    path_relax_legacy 记录旧值供追溯, 不再参与固定键判定。)
    limit_warn = noise
    limit_fail = max(noise * fail_mul, cfg["min_fail_ratio"])

    detail: Dict[str, Any] = {
        "source_path": path,
        "relax_ratio": 0.0,              # 固定键有效放宽 (P1 后恒为 0)
        "path_relax_legacy": relax,      # 旧路径放宽值 (仅追溯, 不参与判定)
        "checked_keys": [],
        "violations": [],
        "geometry_ok": True,
        "geometry_reasons": [],
        "series_notes": [],
        "reference_model": None,
        "undecidable_keys": [],          # 方案自身缺项 → 降级 warning
        "unavailable_in_reference": [],  # 基准数据源无此项 → 仅记录, 不降级
    }

    # 1. 紧急几何自检 (硬)
    geom_ok, geom_reasons = geometry_self_check(design, source_path=path, config=cfg)
    detail["geometry_ok"] = geom_ok
    detail["geometry_reasons"] = geom_reasons

    # 2/3/4. 固定项偏差核验
    ref = reference
    if not isinstance(ref, dict):
        ref = (design.get("_seed") if isinstance(design.get("_seed"), dict) else None)
    if not isinstance(ref, dict):
        ref = find_series_reference(design, specs)
    if isinstance(ref, dict):
        detail["reference_model"] = ref.get("model") or ref.get("motor_model") or ref.get("motor_name")

    fixed_keys = FIXED_KEYS_BY_PATH.get(path, ())
    has_failed = not geom_ok
    has_warning = False

    if fixed_keys:
        if not isinstance(ref, dict):
            has_warning = True
            detail["undecidable_keys"] = list(fixed_keys)
        else:
            for key in fixed_keys:
                dev, undecided = _fixed_key_deviation(design, ref, key)
                if dev is None:
                    # 基准无此项属数据源限制(型谱库不含 rotor_od/shaft_dia/tooth_width),
                    # 不能因此惩罚方案; 只有方案自身缺项才降级
                    if undecided == "reference":
                        detail["unavailable_in_reference"].append(key)
                    else:
                        detail["undecidable_keys"].append(key)
                    continue
                detail["checked_keys"].append(key)
                if dev > limit_fail:
                    has_failed = True
                    level = STATUS_FAILED
                elif dev > limit_warn:
                    has_warning = True
                    level = STATUS_WARNING
                else:
                    continue
                detail["violations"].append({
                    "key": key,
                    "deviation": round(dev, 4),
                    "limit": round(limit_warn, 4),
                    "level": level,
                })
            if detail["undecidable_keys"]:
                has_warning = True

    # 5. 系列代际 / 防护校验 (软)
    series_notes = check_series_generation(design, specs=specs, reference=ref, config=cfg)
    detail["series_notes"] = series_notes
    if series_notes:
        has_warning = True

    # 6. 汇总
    if has_failed:
        status = STATUS_FAILED
    elif has_warning:
        status = STATUS_WARNING
    else:
        status = STATUS_PASSED
    detail["verify_status"] = status
    # P1: summary 标签展示固定键有效放宽(0), 旧值在 path_relax_legacy 追溯
    detail["summary"] = _build_summary(status, path, 0.0, detail)
    return detail


def _build_summary(status: str, path: str, relax: float, detail: Dict[str, Any]) -> str:
    """生成可直接展示在客户端卡片/报告的一句话结论"""
    label = {SOURCE_MAIN_A: "主线A", SOURCE_BYPASS_B1: "旁路B1",
             SOURCE_BYPASS_B2: "旁路B2", SOURCE_BYPASS_B3: "旁路B3",
             SOURCE_FALLBACK: "物理兜底"}.get(path, path)
    head = f"{label}(放宽{relax * 100:.0f}%)"
    if status == STATUS_FAILED:
        if not detail.get("geometry_ok"):
            return f"{head} 几何自检不通过: " + "; ".join(detail.get("geometry_reasons", [])[:3])
        bad = [v["key"] for v in detail.get("violations", []) if v.get("level") == STATUS_FAILED]
        return f"{head} 固定项超限: {', '.join(bad[:4])}"
    if status == STATUS_WARNING:
        parts = []
        warn_keys = [v["key"] for v in detail.get("violations", []) if v.get("level") == STATUS_WARNING]
        if warn_keys:
            parts.append(f"固定项轻微偏离({', '.join(warn_keys[:3])})")
        if detail.get("undecidable_keys"):
            parts.append(f"缺基准无法判定({', '.join(detail['undecidable_keys'][:3])})")
        if detail.get("series_notes"):
            parts.append(detail["series_notes"][0])
        return f"{head} " + "; ".join(parts) if parts else f"{head} 存在待确认项"
    return f"{head} 分层核验通过"


def verify_batch(results: List[dict],
                 specs: Optional[dict] = None,
                 references: Optional[Dict[str, dict]] = None,
                 config: Optional[dict] = None,
                 verbose: bool = False) -> List[dict]:
    """
    批量核验并**就地**为每个方案追加字段 (方案书约束 B: 纯加法)。

    追加字段:
      source_path    : 归一化来源路径
      verify_status  : passed / warning / failed
      verify_detail  : verify_layered() 完整明细
      verify_summary : 一句话结论 (客户端卡片可直接展示)

    references: 可选 {source_path: 参考基准 dict} 或 {方案 id: 基准};
                缺省时按 _seed → 型谱最近条目 顺序自动取基准。

    返回同一个 list 对象 (保持下游引用不变)。
    """
    cfg = get_verify_config(config)
    for design in results or []:
        if not isinstance(design, dict):
            continue
        path = normalize_source_path(design)
        ref = None
        if isinstance(references, dict):
            ref = references.get(design.get("_seed_id")) or references.get(path)
        detail = verify_layered(design, specs=specs, reference=ref,
                                source_path=path, config=cfg)
        design["source_path"] = path
        design["verify_status"] = detail["verify_status"]
        design["verify_summary"] = detail["summary"]
        design["verify_detail"] = detail
    if verbose:
        stat = summarize(results)
        logger.info("[verify_engine] 核验完成: %s", stat)
    return results


def summarize(results: List[dict]) -> Dict[str, Any]:
    """统计核验结果分布, 供 solve_summary / 报告章节引用"""
    out: Dict[str, Any] = {
        "total": 0,
        "passed": 0, "warning": 0, "failed": 0,
        "by_path": {},
    }
    for design in results or []:
        if not isinstance(design, dict):
            continue
        out["total"] += 1
        status = design.get("verify_status") or STATUS_WARNING
        if status in (STATUS_PASSED, STATUS_WARNING, STATUS_FAILED):
            out[status] += 1
        path = design.get("source_path") or SOURCE_MAIN_A
        slot = out["by_path"].setdefault(path, {"total": 0, "passed": 0, "warning": 0, "failed": 0})
        slot["total"] += 1
        if status in (STATUS_PASSED, STATUS_WARNING, STATUS_FAILED):
            slot[status] += 1
    return out


# ─────────────────────────────────────────────────────────────
# 自测 (python -m Core.verify_engine)
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 78)
    print("verify_engine 自测 — Phase 0 验收: 对固定参数输出 passed/warning/failed")
    print("=" * 78)

    # 以型谱 Y160M-4 为基准种子 (4 极, od=260, 尺寸适中便于覆盖各分支)
    catalog = _load_catalog()
    print(f"\n[0] 型谱库加载: {len(catalog)} 条")
    seed = next((e for e in catalog if e.get("model") == "Y160M-4"), catalog[0] if catalog else {})
    print(f"    基准种子: {seed.get('model')} od={seed.get('stator_od_mm')} "
          f"id={seed.get('stator_inner_dia_mm')} L={seed.get('core_length_mm')} "
          f"poles={seed.get('poles')} slots={seed.get('slots_stator')}/{seed.get('slots_rotor')}")

    def make(**over):
        base = {
            "stator_od": _num(seed.get("stator_od_mm"), 260),
            "stator_id": _num(seed.get("stator_inner_dia_mm"), 170),
            "core_length": _num(seed.get("core_length_mm"), 155),
            "air_gap": _num(seed.get("air_gap_mm"), 0.65),
            "poles": int(_num(seed.get("poles"), 4)),
            "slots_stator": int(_num(seed.get("slots_stator"), 36)),
            "slots_rotor": int(_num(seed.get("slots_rotor"), 32)),
            "yoke_thickness": _num(seed.get("yoke_thickness_mm"), 16.0),
            "tooth_width": 9.0,
        }
        base["rotor_od"] = round(base["stator_id"] - 2 * base["air_gap"], 1)
        base["shaft_dia"] = round(base["rotor_od"] * 0.35, 1)
        base.update(over)
        return base

    od0 = _num(seed.get("stator_od_mm"), 260)
    cases = [
        # ── 主线 A: 放宽 0%, 冲片几何全固定; 叠长是合法自由度 ──
        ("主线A · 仅改叠长(合法自由度)", make(source_path=SOURCE_MAIN_A, core_length=195.0), STATUS_PASSED),
        ("主线A · 定子外径改 1%(工艺公差带)", make(source_path=SOURCE_MAIN_A,
                                             stator_od=od0 * 1.01), STATUS_WARNING),
        ("主线A · 定子外径改 3%(已换冲片)", make(source_path=SOURCE_MAIN_A,
                                            stator_od=od0 * 1.03), STATUS_FAILED),
        ("主线A · 改槽数(冲片固定项违规)", make(source_path=SOURCE_MAIN_A, slots_stator=48), STATUS_FAILED),
        # ── 旁路 B1: 放宽 +5%, 固定定子内外径 + 轴径 ──
        ("旁路B1 · 定子外径改 3%(放宽5%内)", make(source_path=SOURCE_BYPASS_B1,
                                              stator_od=od0 * 1.03), STATUS_PASSED),
        ("旁路B1 · 定子外径改 8%(超放宽)", make(source_path=SOURCE_BYPASS_B1,
                                            stator_od=od0 * 1.08), STATUS_WARNING),
        ("旁路B1 · 轭部过厚致槽深不足", make(source_path=SOURCE_BYPASS_B1, yoke_thickness=44.0), STATUS_FAILED),
        # ── 旁路 B2: 放宽 +10%, 加验转子外径 < 定子内径 ──
        ("旁路B2 · 定子内径自由(合法)", make(source_path=SOURCE_BYPASS_B2, stator_id=180.0,
                                        rotor_od=178.9, air_gap=0.55), STATUS_PASSED),
        ("旁路B2 · 转子外径≥定子内径", make(source_path=SOURCE_BYPASS_B2,
                                        rotor_od=_num(seed.get("stator_inner_dia_mm"), 170)), STATUS_FAILED),
        # ── 旁路 B3: 无圆约束, 放宽 +15%, 加验极数密度 ──
        ("旁路B3 · 无圆约束, 大改仍合法", make(source_path=SOURCE_BYPASS_B3, stator_od=300.0,
                                          stator_id=200.0, rotor_od=198.6, air_gap=0.7,
                                          shaft_dia=60.0, yoke_thickness=18.0,
                                          slots_stator=48, poles=6), STATUS_PASSED),
        ("旁路B3 · 极数密度过高(48极/260)", make(source_path=SOURCE_BYPASS_B3, poles=48,
                                             slots_stator=72, tooth_width=3.0,
                                             yoke_thickness=12.0), STATUS_FAILED),
        # ── 通用硬门槛 ──
        ("通用 · 气隙不自洽", make(source_path=SOURCE_BYPASS_B1, rotor_od=140.0), STATUS_FAILED),
        ("通用 · λ 越界(超长)", make(source_path=SOURCE_BYPASS_B3, core_length=900.0), STATUS_FAILED),
    ]

    print("\n[1] 单方案分层核验")
    print("-" * 78)
    ok_cnt = 0
    for title, design, expect in cases:
        detail = verify_layered(design, specs={"protection_class": "IP44"})
        got = detail["verify_status"]
        flag = "  " if expect is None else ("OK" if got == expect else "!!")
        if expect is None or got == expect:
            ok_cnt += 1
        print(f"{flag} {title}")
        print(f"     → {got.upper():8s} | {detail['summary']}")
        if detail["violations"]:
            for v in detail["violations"][:2]:
                print(f"       · {v['key']}: 偏差{v['deviation'] * 100:.1f}% > 限{v['limit'] * 100:.1f}% [{v['level']}]")
        if detail["geometry_reasons"]:
            print(f"       · 几何: {detail['geometry_reasons'][:2]}")

    print("-" * 78)
    print(f"    断言通过: {ok_cnt}/{len(cases)}")

    print("\n[2] 批量硬过滤 filter_by_geometry")
    pool = [d for _, d, _ in cases]
    kept, rejected = filter_by_geometry([dict(d) for d in pool], verbose=False)
    print(f"    输入 {len(pool)} 组 → 保留 {len(kept)} / 剔除 {len(rejected)}")
    for r in rejected[:4]:
        print(f"    · {r['_geom_reject_path']}: {r['_geom_reject_reasons'][:2]}")

    print("\n[3] 批量核验 verify_batch + summarize (纯加法验证)")
    batch = [dict(d) for d in pool]
    before_keys = set(batch[0].keys())
    verify_batch(batch, specs={"protection_class": "IP44"})
    after_keys = set(batch[0].keys())
    added = sorted(after_keys - before_keys)
    print(f"    新增字段: {added}")
    print(f"    删除字段: {sorted(before_keys - after_keys) or '无 (符合约束B 纯加法)'}")
    print(f"    统计: {json.dumps(summarize(batch), ensure_ascii=False)}")

    print("\n[4] 机座号反推 infer_frame_size")
    for od in (120, 260, 368, 500):
        frame, dev = infer_frame_size(od)
        print(f"    定子外径 {od}mm → H{frame} (偏差 {dev * 100:.1f}%)")

    print("\n" + "=" * 78)
    print("Phase 0 自测完成")
    print("=" * 78)
