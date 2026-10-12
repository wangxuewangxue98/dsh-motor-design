"""
决策门质量门禁 (P0-2 / 改进资料 3.5)

在「决策门 → 人工确认」之前, 自动校验四道关卡:
  1. 评审置信度 ≥ 阈值 (默认 0.7, 取自 review_feedback.confidence)
  2. 硬约束: 效率(efficiency_min) / 温升(temp_max) / 槽满率(slot_fill_max)
     —— 取最佳方案(results[0]) 评估, 阈值 = 0 表示不校验
  3. 推荐方案存在性 (M1, 默认开启): 扫描支持报告(review)是否"无推荐方案"等否定表述
     —— 命中则视为未通过, 触发方案重生; 报告缺失时跳过(不阻断)
  4. 收敛轨严重冲突检测: 扫描 con_report 命中致命关键词

M2 额外硬约束 (torque_min_ratio / power_min_ratio / current_density_max, 默认开启, 由 extra_hard_constraints_enabled
开关控制) 与渐进收紧 (tighten_gate_config, 重试时进一步收紧阈值) 共同支撑「无推荐→重生→收紧→再评审」回路。

任一未通过 → 触发「有限次方案重生」(最多 max_redo 次), 超限转人工裁决。

配置来源: Core/workflow_config.py 的 quality_gate 段 (运行时可改, 无需重启)。
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── P0 (v4.02.67): 槽满率上限接"单一真相源" (Core.workflow_config) ──
#   消除 quality_gate.py 与 workflow_config/ai_flow_config/scheme_engine 的口径漂移。
#   兼容两种导入形态 (包内 `from Core.xxx` / 扁平 `import xxx`), 全部失败则退回常量。
try:  # 扁平 (orchestrator 常以 sys.path 含 Core)
    from workflow_config import (  # type: ignore
        SLOT_FILL_MAX_DEFAULT as _SF_MAX_DEFAULT,
        SLOT_FILL_TARGET_BAND as _SF_TARGET_BAND,
        SLOT_FILL_PRECHECK_HARD_CAP as _SF_HARD_CAP,
        get_slot_fill_max as _get_slot_fill_max,
        get_slot_fill_target_band as _get_slot_fill_band,
    )
except Exception:  # pragma: no cover
    try:
        from Core.workflow_config import (  # type: ignore
            SLOT_FILL_MAX_DEFAULT as _SF_MAX_DEFAULT,
            SLOT_FILL_TARGET_BAND as _SF_TARGET_BAND,
            SLOT_FILL_PRECHECK_HARD_CAP as _SF_HARD_CAP,
            get_slot_fill_max as _get_slot_fill_max,
            get_slot_fill_target_band as _get_slot_fill_band,
        )
    except Exception:
        _SF_MAX_DEFAULT = 0.75
        _SF_TARGET_BAND = (0.65, 0.75)
        _SF_HARD_CAP = 0.80

        def _get_slot_fill_max(process=None):  # type: ignore
            return 0.75

        def _get_slot_fill_band(process=None):  # type: ignore
            return (0.65, 0.75)

# ── P4/P5 (v4.02.93): 2D 几何门取值范围真源 (Scripts/_mag_precheck_2d.py) ──
#   用于 2D 几何门新增项 (轭厚/槽深比、Dso/Dsi 比、齿宽、Qs)。导入失败降级 None,
#   新增项自动跳过 (不阻断既有流程)。
try:
    import os as _os
    import sys as _sys
    _MAG_SCRIPTS_DIR = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "Scripts")
    if _MAG_SCRIPTS_DIR not in _sys.path:
        _sys.path.insert(0, _MAG_SCRIPTS_DIR)
    from _mag_precheck_2d import (  # type: ignore
        tooth_width_ok as _m2d_tooth_width_ok,
        qs_ok as _m2d_qs_ok,
        dso_dsi_ok as _m2d_dso_dsi_ok,
        yoke_slot_ratio_ok as _m2d_yoke_slot_ratio_ok,
    )
except Exception as _m2d_err:  # pragma: no cover
    logger.warning("quality_gate: _mag_precheck_2d 未加载 (2D 几何门新增项跳过): %s", _m2d_err)
    _m2d_tooth_width_ok = None
    _m2d_qs_ok = None
    _m2d_dso_dsi_ok = None
    _m2d_yoke_slot_ratio_ok = None

# ── P7 (v4.02.93): 并联支路数动作开关 ──
#   电密高 / 温升高 → 增并联支路数 (分流降单路电流, **不占槽满率** —— 比加粗线规干净)。
#   ⚠ 实测口径 (勿凭直觉): 本模型中 a↑ 使 n_series = Z·ss/(6a) ∝ 1/a, 即**磁负荷需求↑**;
#     而导体需求 a_cond=(I/a)/J 在 power 电流路径 ∝ 1/a (铜量↓), 在 line_load 路径不变。
#     ⇒ 该杠杆净效果取决于电流来源路径, 默认开 (用户指定), 置 0 可随时停用。
_PARALLEL_PATHS_ACTION = str(os.environ.get("QG_PARALLEL_PATHS_ACTION", "1")).strip().lower() \
    not in ("0", "false", "off", "no")

# ── P0-5 (2026-09-22): 达标比相对容差默认值 (可被 cfg["achieve_ratio_tol"] 覆盖) ──
#   见 DEFAULT_QUALITY_GATE["achieve_ratio_tol"] 的完整说明。
ACHIEVE_RATIO_TOL_DEFAULT = 0.005

# ─── 默认门禁配置 ───
# 合理默认硬约束 (v4.01.059 P1-#5): 防止默认值 0 导致门禁形同虚设, 同时避免过严频繁触发重生
#   efficiency_min          : 全局效率下限(%), 仅当按电机类型无法命中时采用
#   efficiency_min_by_type  : 不同类型合理效率下限 (永磁高 / 异步中 / 开关磁阻低), 子串匹配 motor_type
#   temp_max                : F 级绝缘允许最高温升(°C) → 120°C
#   slot_fill_max           : 槽满率工程上限 (0~1) → 0.75
DEFAULT_QUALITY_GATE: Dict[str, Any] = {
    "confidence_threshold": 0.7,   # 评审置信度门禁阈值
    "max_redo": 2,                  # 有限次方案重生上限 (v4.02.97; 2026-09-20 由 2→5, 2026-09-21 用户指令 5→3→2=每层反复次数)
    # P6: 质量门禁重生前校验求解器可用性 (RMxprt/Motor-CAD)。
    # 若下一轮求解所需求解器不可用, 盲目重生只会再次失败并空耗 max_redo 次数,
    # 故直接转人工裁决 (workflow 层护盾对账: 不把求解器故障当作设计缺陷反复重生)。
    "enforce_solver_available": True,
    # M1: 推荐方案存在性关卡 (默认开启)。扫描支持报告(review)的"无推荐"否定表述。
    "recommendation_check": {
        "enabled": True,
        "negation_patterns": [
            "无推荐方案", "无推荐", "未找到推荐方案", "未推荐",
            "无法给出推荐方案", "无明确推荐", "没有推荐方案", "暂不推荐", "无推荐结论",
        ],
    },
    # M2: 额外硬约束主开关 (v4.02.22 起默认开启)。开启后校验 torque_min_ratio / power_min_ratio / current_density_max。
    #   额定功率/转矩达标比硬约束可在首轮即拦截「退化几何 0 功率 / 0 转矩」等失真方案, 避免被评审幻觉推荐为成功。
    "extra_hard_constraints_enabled": True,
    # ★ P0-5 (2026-09-22): 达标比相对容差 —— 消除「1.00 < 下限 1.0」假失败。
    #   症状: 深度重试 (redo>=2) 把 power/torque_min_ratio 收严到 1.0 (无容差),
    #        而 RMxprt 求解值贴近目标时的收敛噪声 (±0.1%) 被判为设计缺陷
    #        ⇒ RUN40 有 5 个轮次报「额定功率达标比 1.00 < 下限 1.0」(449.8/450.0=0.9996)。
    #   语义: 判据统一为 value >= ratio · (1 − tol); 即阈值语义仍为「达标比」,
    #        tol 只吸收数值噪声。0.5% 是工程可接受带宽, 不得放大到掩盖真实欠功率。
    "achieve_ratio_tol": 0.005,
    "hard_constraints": {
        "efficiency_min": 70.0,     # 全局效率下限 (%, 0 = 不校验); 按类型命中时以 by_type 为准
        "temp_max": 120.0,          # 最佳方案最高温升 (°C, F级绝缘); 0 = 不校验
        # P0 (v4.02.67): 引用单一真相源 (原硬编码 0.75)
        "slot_fill_max": _SF_MAX_DEFAULT,   # 最佳方案槽满率上限 (0~1); 0 = 不校验
        "efficiency_min_by_type": {  # 不同电机类型的合理效率下限 (子串匹配 motor_type)
            "永磁同步": 80.0,
            "异步": 75.0,
            "开关磁阻": 60.0,
        },
        # M2: 额外硬约束 (仅当 extra_hard_constraints_enabled=True 时校验; 0 = 不校验)
        "torque_min_ratio": 0.95,   # 额定转矩达标比下限 (Torque_rated / Torque_target); 默认 0.95
        "power_min_ratio": 0.95,    # 额定功率达标比下限 (Achieved_Power / Target_Power); 默认 0.95
        "current_density_max": 8.0, # 定子电密上限 (A/mm²); 默认 8.0
    },
    "convergence_fatal_keywords": [
        "致命", "严重冲突", "不可接受", "否决", "禁止",
        "不满足关键", "一票否决", "FATAL", "致命缺陷", "无法接受",
    ],
    # ── G1 (v4.02.92): 2D 磁路带校验 ──
    #   质量门此前不读/不校磁密(orchestrator L1 已产出却无人消费, 即「有值无炊」)。
    #   mode="warning" 仅记录不阻断(观察一轮); 设 "hard" 才把磁密/λ 越界转为硬失败。
    #   带定义来自 v4 文档 TABLE 3/5 (Y 系列 85 型号实测 + 物理推导)。
    #   ζ(齿槽比) 为近似推导, 仅 warning 永不阻断; SF 下限仅 warning。
    "magnetic_gate": {
        "enabled": True,
        "mode": "warning",          # "warning" | "hard"
        # 2026-09-20 用户口径: 齿磁密 [1.5,1.8]T / 轭磁密 [0.8,1.2]T
        #   (旧带 齿[1.2,1.7]/轭[1.0,1.6] 对大机座低速异步机过宽, RUN21 实测
        #    Bt=1.108T / By=0.714T 在旧带内被视为"合格", 实为磁负荷严重欠激)。
        # 2026-09-20 修订(用户选 3): 齿磁密带放宽 [1.4,1.7] —— 容纳单轮扰动下
        #   边界候选 (RUN25: 提磁密链把 Bt 抬到 1.55 附近, 旧上界 1.8 留余量过大,
        #   下界 1.5 又卡掉刚达标的 1.4x 候选), 与轭带 [0.8,1.2] 形成对称余量。
        "tooth_flux_band": [1.4, 1.7],      # T
        "yoke_flux_band": [0.8, 1.2],       # T
        # ★ 磁密硬约束开关 (2026-09-20): 独立于 mode ——
        #   True  ⇒ 齿/轭磁密出带即 passed=False + 进 reasons/fail_tags(2d),
        #            从而真正触发重生 (warning 下 passed 恒 True 是"磁密修不动"的根因)。
        #   False ⇒ 退回旧纯观察行为。
        "flux_gate_hard": True,
        "air_gap_flux_band": [0.55, 0.85],  # T
        "zeta_band": [0.90, 1.719],         # P5: 硬下界 0.806(⇔Bt≤1.70); 推荐 0.902~1.719
        "slot_fill_min": 0.65,
        # ★ 3D 门「槽满率偏低」硬约束开关 (2026-09-20, 用户指令) ——
        #   症状: 槽满率偏小 ⇒ 铜/槽利用不足 (RUN27 实测 SF=0.2775, 远低于下限 0.65;
        #   根因: SLOT_DEPTH_MODE='fill' 把槽深吃满可用径向空间 ⇒ 槽面积虚大)。
        #   True  ⇒ SF < 下限时 passed=False → 进 reasons/fail_tags, 且该判据归 **2d 集**,
        #           经 _after_quality_gate 路由回 **2D 流程起点** (regenerate_2d),
        #           动作 = shrink_slot_keep_yoke:
        #             减小定子槽高 Hs, 同步减小定子外径 Dso (ΔDso = 2·ΔHs),
        #             使 hj=(Dso−Dsi)/2−Hs 与定子轭磁密 By **保持不变** (仅槽面积↓⇒SF↑)。
        #   False ⇒ 退回旧「纯 warning 不阻断」行为。
        "slot_fill_low_hard": True,
        "lambda_band": [0.3, 4.0],
        "lambda_abs_mm": [20.0, 3500.0],
        # ── P4/P5 (v4.02.93): 2D 几何门新增项 (全部 warning 观察, mode=hard 才阻断) ──
        # 轭厚/槽深比 hj/hs —— 2026-09-19 用户口径: **按极数分档**
        #   2 极 [2.0, 3.5] / 4 极 [1.0, 2.5] / 6 极及以上 [1.0, 2.5]
        #   真源 Scripts/_mag_precheck_2d.yoke_slot_ratio_band_by_poles;
        #   下方 yoke_slot_ratio_band 仅作「poles 缺失」兜底, by_poles 可按需覆盖分档。
        "yoke_slot_ratio_band_by_poles": {"2": [2.0, 3.5], "4": [1.0, 2.5], "6": [1.0, 2.5]},
        "yoke_slot_ratio_band": [1.0, 2.5],   # 兜底带 (无 poles 时)
        "dso_dsi_tol": 0.15,                  # Dso/Dsi 比相对容差 (按极数 id_ratio_by_poles)
        "starting_torque_pu_min": 0.0,        # 起动(堵转)转矩倍数下限; 0 = 不校验 (应用相关)
        "power_factor_min": 0.0,              # 功率因数下限; 0 = 不校验 (应用相关)
        # P4 观察纪律: 新增几何门默认【纯观察】(passed 恒 True, 永不阻断), 记录失效率一轮;
        #   置 True 才让其越界进入 fail_tags (hard 生效), 防初次上线整案全灭。
        # P2 (2026-09-19): 转硬 — geom_gate_hard=True 使几何越界进入 fail_tags。
        "geom_gate_hard": True,
    },
}


def get_quality_gate_config(runtime_config: Optional[dict] = None) -> Dict[str, Any]:
    """读取门禁配置: runtime_config['quality_gate'] 覆盖默认值并深拷贝返回"""
    if isinstance(runtime_config, dict) and isinstance(runtime_config.get("quality_gate"), dict):
        cfg = dict(DEFAULT_QUALITY_GATE)
        saved = runtime_config["quality_gate"]
        for k, v in saved.items():
            if k == "hard_constraints" and isinstance(v, dict):
                cfg["hard_constraints"] = dict(DEFAULT_QUALITY_GATE["hard_constraints"])
                cfg["hard_constraints"].update(v)
            elif k != "hard_constraints":
                cfg[k] = v
        return cfg
    return dict(DEFAULT_QUALITY_GATE)


# ─── 辅助读取 ───
def _read_confidence(state: dict) -> Optional[float]:
    """从 review_feedback.confidence 读取评审置信度 (0~1); 缺失返回 None"""
    rf = state.get("review_feedback") or {}
    if isinstance(rf, dict):
        c = rf.get("confidence", None)
        if isinstance(c, (int, float)):
            return float(c)
        try:
            return float(str(c))
        except (TypeError, ValueError):
            pass
    return None


def _resolve_eff_min(hc_cfg: dict, motor_type: str) -> float:
    """按电机类型解析效率下限: 优先命中 efficiency_min_by_type 子串匹配, 否则用全局 efficiency_min。

    motor_type 取值示例: '永磁同步电动机' / '异步电动机' / '三相异步电动机' / '开关磁阻电机'。
    by_type 的 key 以子串匹配 (如 '永磁同步' in '永磁同步电动机')。
    """
    eff_min = float(hc_cfg.get("efficiency_min", 0.0) or 0.0)
    by_type = hc_cfg.get("efficiency_min_by_type") or {}
    if motor_type:
        mt = str(motor_type)
        for key, val in by_type.items():
            if key and key in mt:
                return float(val or 0.0)
    return eff_min


def _flux_in_band(r: dict, mg: dict) -> Optional[bool]:
    """判定候选 r 的齿/轭/气隙磁密是否全部落在 magnetic_gate 带内 (供磁密优先选优)。

    返回:
        True  —— 三个磁密字段都存在(>0)且全部在带内;
        False —— 任一磁密字段存在但出带;
        None  —— 三个磁密字段全部缺失/<=0 (未知, 不参与"带内优选", 交由原 Top-1 兜底)。
    """
    if not isinstance(r, dict) or not isinstance(mg, dict):
        return None
    _bt_b = tuple(mg.get("tooth_flux_band", (1.2, 1.7)))
    _by_b = tuple(mg.get("yoke_flux_band", (1.0, 1.6)))
    _bdg_b = tuple(mg.get("air_gap_flux_band", (0.55, 0.85)))
    _items = ((r.get("stator_tooth_flux_density"), _bt_b),
              (r.get("stator_yoke_flux_density"), _by_b),
              (r.get("air_gap_flux_density"), _bdg_b))
    _seen = False
    for _v, _b in _items:
        try:
            _x = float(_v or 0)
        except (TypeError, ValueError):
            _x = 0.0
        if _x > 0:
            _seen = True
            if not (_b[0] <= _x <= _b[1]):
                return False
    # _seen=False ⇒ 三个磁密字段全部缺失/<=0 ⇒ 返回 None (按"未知"处理,
    #   不参与磁密优先选优, 交由 _best_solution 回退 results[0]);
    # _seen=True ⇒ 全部在带内 ⇒ True (否则已在循环内 early-return False)。
    return None if not _seen else True


def _best_solution(state: dict, cfg: Optional[dict] = None) -> Optional[dict]:
    """取最佳方案供门禁评估。

    磁密硬约束开启(flux_gate_hard 或 mode=hard)时, 启用**磁密优先选优** (用户选 1, 2026-09-20):
      先筛出齿/轭/气隙磁密全部在带内的候选, 再在其中按效率取最高;
      若无任何带内候选, 回退到 results[0] (原效率降序 Top-1, 保持向后兼容)。
    磁密非硬约束时, 沿用 results[0]。

    ⚠ 仅改变"评估对象"的选取口径, 不改变门禁判据本身 —— 选出的候选仍须过效率/温升/
      槽满率/转矩等所有硬约束; 磁密优先选优保证"带内候选不被 N 偏少的失真 Top-1 压过"。
    """
    results = state.get("results") or []
    if not (results and isinstance(results[0], dict)):
        return None
    if cfg is None:
        return results[0]
    _mg = (cfg.get("magnetic_gate") or {})
    if not _mg.get("enabled", True):
        return results[0]
    _flux_hard = bool(_mg.get("flux_gate_hard", False)) or (_mg.get("mode", "warning") == "hard")
    if not _flux_hard:
        return results[0]
    _in_band = [r for r in results if isinstance(r, dict) and _flux_in_band(r, _mg) is True]
    if not _in_band:
        return results[0]
    # 带内候选按效率取优 (与 results 原排序口径一致: efficiency 降序)
    return max(_in_band, key=lambda r: float(r.get("efficiency", 0) or 0))


def _resolve_slot_depth(best: dict) -> Optional[float]:
    """方案B (2026-09-19): 从最佳方案中解析**真实定子槽深** (mm), 取不到返回 None。

    取值优先级 (真实值优先, 避免几何反推失真):
      ① Slot__Hs2        —— RMxprt 槽形字段 ("'43.6mm'" 带引号格式), 生产主口径
      ② slot_depth / slot_height / stator_slot_depth —— 结果字典显式槽深
      ③ None             —— 调用方回退 (Dso−Dsi)/2 − hj 反推 (hs_src="derived")

    ⚠ 反推口径对深槽/非标设计系统性低估 hj/hs (RUN19: 反推 100.7mm vs 真值 ~43.6mm,
      比值 0.579 vs 1.34), 仅在无真实槽形时使用。
    """
    if not isinstance(best, dict):
        return None
    # ① RMxprt 槽形字段 (带引号: "'43.6mm'")
    _raw = best.get("Slot__Hs2")
    if _raw is not None:
        try:
            _v = float(str(_raw).replace("'", "").replace("mm", "").strip())
            if _v > 0:
                return _v
        except (TypeError, ValueError):
            pass
    # ② 结果字典显式槽深
    for _k in ("slot_depth", "slot_height", "stator_slot_depth", "hs_mm"):
        try:
            _v = float(best.get(_k) or 0)
        except (TypeError, ValueError):
            continue
        if _v > 0:
            return _v
    return None


def _estimate_zeta(best: dict) -> Optional[float]:
    """近似齿槽比 ζ = bt / w (槽肩处), 槽肩 t_sh = π·(Dsi + 2·h_sh)/Qs。

    h_sh 用槽总高的一半近似 (缺槽形数据时返回 None 跳过)。仅用于 warning 提示,
    非阻断(近似推导, 不应据以拒绝方案)。
    """
    if not isinstance(best, dict):
        return None
    try:
        dsi = float(best.get("stator_id") or 0)
        dso = float(best.get("stator_od") or 0)
        qs = float(best.get("slots_stator") or 0)
        bt = float(best.get("tooth_width") or 0)
        yt = float(best.get("yoke_thickness") or 0)
    except (TypeError, ValueError):
        return None
    if not (dsi > 0 and dso > 0 and qs > 0 and bt > 0):
        return None
    h_s = (dso - dsi) / 2.0 - yt       # 槽总高
    if h_s <= 0:
        return None
    h_sh = 0.5 * h_s                   # 槽肩高度近似
    t_sh = math.pi * (dsi + 2.0 * h_sh) / qs
    if t_sh <= 0:
        return None
    w = t_sh - bt                       # 槽肩处槽宽
    if w <= 0:
        return None
    return bt / w


def _target_torque_from_specs(state: dict) -> float:
    """由 specs 推导目标额定转矩(N·m): T = 9550 * power_kw / speed_rpm。

    供额定转矩达标比兜底计算 (真实结果未携带 rated_torque_ratio 时)。
    specs 缺 power_kw / speed_rpm 时返回 0.0 (调用方据此跳过校验, 不误杀)。
    """
    specs = (state.get("specs") or {}) if isinstance(state, dict) else {}
    p = float(specs.get("power_kw") or 0)
    n = float(specs.get("speed_rpm") or specs.get("speed") or 0)
    if p > 0 and n > 0:
        return round(9550.0 * p / n, 3)
    return 0.0


# 否定前缀: 关键词前 3 字内出现这些词, 视为"未检出/无", 不计入致命冲突
_NEGATION_PREFIXES = ("未检出", "未出现", "未发现", "不存在", "无明显", "无", "没有", "不", "非")


def _scan_fatal(con_report: str, kws: List[str]) -> List[str]:
    """扫描收敛轨报告中的致命关键词, 跳过否定前缀语境 (如 '未检出严重冲突')"""
    hits: List[str] = []
    text = con_report or ""
    for kw in kws:
        if not kw:
            continue
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx < 0:
                break
            neg_ctx = text[max(0, idx - 3):idx]
            if any(n in neg_ctx for n in _NEGATION_PREFIXES):
                start = idx + len(kw)
                continue
            hits.append(kw)
            break
    return hits


def _detect_no_recommendation(text: str, patterns: List[str]) -> bool:
    """扫描支持报告文本, 命中任一"无推荐"否定表述即判定无推荐方案。

    采用短语精确匹配(而非单字 '无' 子串), 避免误判含 '无' 的正常表述(如 '无致命缺陷')。
    """
    if not text:
        return False
    for p in (patterns or []):
        if p and p in text:
            return True
    return False


# ─── 核心评估 ───

def _record_missing_field_warning(checks: list, key: str, label: str = None) -> None:
    """2026-09-20 (Fix2): 当关键字段缺失/为 0 时记录 warning 级观察项。

    用途: 与 slot_fill_rate 缺失同款 (RUN28 全池 simulation ⇒ SF=None),
      仅用于磁密/效率/温升等判据的字段完整性感知。
    行为: passed=True (永不阻断) + severity=warning + 不进 reasons (不触发卡死/转人工)。
    key: 候选 dict 中的字段名; label: 文案用的中文名 (缺省由 key 反推)。
    """
    if label is None:
        _LABEL_MAP = {
            "stator_tooth_flux_density": "定子齿磁密",
            "stator_yoke_flux_density": "定子轭磁密",
            "air_gap_flux_density": "气隙磁密",
            "efficiency": "效率",
            "temp_rise": "温升",
            "slot_fill_rate": "槽满率",
        }
        label = _LABEL_MAP.get(key, key)
    checks.append({"name": f"{label}缺失", "passed": True, "value": None,
                    "threshold": None, "severity": "warning",
                    "note": f"{label} 字段缺失/为 0 (求解降级或未回填) ⇒ 该判据本轮未判定"})

# ── 槽满率口径常量 (RUN49-P2 S1 增强, 2026-09-24 用户指令) ──────────────────
#   物理真源: worker 已从 .res 百分数做过 /100 (Scripts/_rmxprt_venv_worker.py),
#     ⇒ 进入门禁的 slot_fill_rate **恒为比率** (0~1 合法; >1 = 崩溃/异常)。
#   SF_PCT_INPUT_TH: **阈值** (管理端 slot_fill_max / slot_fill_min) 仍可能以
#     百分数下发 (70/75) ⇒ 需 /100 归一; 实测值不再归一 (见下)。
SF_PCT_INPUT_TH = 1.5     # 阈值 >1.5 ⇒ 判为百分数写法, /100
SF_CRASH_TH = 1.0         # 实测值 >1.0 ⇒ 物理不可能 (>100%) ⇒ 崩溃, 判超限


def _normalize_slot_fill(v) -> tuple:
    """槽满率**实测值**统一归一 + 崩溃判定 (上下限门禁共用, 根治口径分裂)。

    规则 (2026-09-24 用户指令: sf > 1.0 ⇒ 不 /100, 直接判超限/异常)
    ----------------------------------------------------------------
      * v <= 1.0        → (v, False)  合法比率, 原值参与上/下限比较
      * v >  1.0        → (v, True)   **不 /100**, 标记 crashed ⇒ 判超限/异常

    为什么不再对实测值 /100
    ----------------------
    worker 已做过一次 /100, 实测值必为比率。RUN49 的 slot_fill_rate=1.692 表示
    **169.2%** —— 它本身就是「崩溃」信号 (铜占槽面积 >100%, 物理不可能;
    根因=上游几何喂参失衡: 线径/匝数 vs 槽面积)。若按 S1 旧规则 /100 得 0.0169,
    会被下限门读成「槽满率偏低 0.017」⇒ 下发 shrink_slot_keep_yoke (减槽高抬 SF)
    这一**反向错处方**, 同时真信号 (超限) 消失 —— 与 RUN49-P2 报告第六节裁定
    「上限超限是真信号、下限偏低是假信号」完全相反。

    crashed=True 的处置 (调用方)
    ---------------------------
      上限门: passed=False, severity=critical, 进 reasons (阻断)
      下限门: **跳过**「偏低」判定, 不置 _sf_low_fail (避免路由回 2D 减槽高)
             只记一条 critical 观察项, 避免静默。
    """
    try:
        _v = float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0, False
    if _v > SF_CRASH_TH:
        return _v, True
    return _v, False


def _l0_l1_diagnostics(state: dict) -> List[str]:
    """v4.02.68 (系列三 Tier A): 由 L0↔L1 差异产出诊断项 (非阻断, 供报告/评价披露)。

    仅输出摘要级信息 (JOIN 数/平均|Δη|/显著偏差数/故障排除数) + 最多 3 条单方案
    "L0显著偏差"明细, 避免把 10 条逐方案差异堆进 reasons 造成 guidance 膨胀。
    """
    out: List[str] = []
    diff = state.get("l0_l1_diff") or {}
    if not isinstance(diff, dict) or not diff.get("enabled"):
        return out
    try:
        _j = int(diff.get("joins") or 0)
        _b = int(diff.get("bias_count") or 0)
        _fe = int(diff.get("fault_excluded") or 0)
        _avg = float(diff.get("avg_abs_delta_pp") or 0.0)
    except (TypeError, ValueError):
        return out
    if _j <= 0:
        return out
    _txt = (f"L0↔L1 差异: {_j} 组已 JOIN, 平均|Δη|={_avg:.2f}pp, "
            f"L0显著偏差={_b} 组 (阈值{diff.get('threshold_pp')}pp)")
    if _fe:
        _txt += f", 故障方案 {_fe} 组已排除出残差统计"
    out.append(_txt)
    _lines: List[str] = []
    for _i, _r in enumerate(state.get("results") or []):
        if not isinstance(_r, dict):
            continue
        if _r.get("l0_bias_flag") and isinstance(_r.get("delta_eff"), (int, float)):
            _lines.append(
                f"方案{_i+1} {_r['l0_bias_flag']} "
                f"(L0预测{_r.get('l0_eff')}% → L1实算{_r.get('efficiency')}%)")
        if len(_lines) >= 3:
            break
    out.extend(_lines)
    return out


def _slim_best(best: Optional[dict]) -> dict:
    """v4.02.104 (三.2): 提取门禁选中方案的**轻量快照** (供报告复算电磁负荷/材料利用率)。

    只取计算所需字段, 并对 `params` 做白名单裁剪 (槽形/绕组/匝数/线规), 避免把
    整个候选 (可能含长列表/图片路径) 塞进 state 与 SSE 事件。
    """
    if not isinstance(best, dict) or not best:
        return {}
    _scalar = (
        "stator_od", "stator_id", "rotor_od", "core_length", "air_gap",
        "slots_stator", "slots_rotor", "poles", "turns_per_coil",
        "air_gap_flux_density", "stator_tooth_flux_density", "slot_fill_rate",
        "power", "torque", "rated_torque", "stator_current", "efficiency",
        "current_density", "solve_mode", "index",
    )
    out = {k: best.get(k) for k in _scalar if best.get(k) is not None}
    _p = best.get("params")
    if isinstance(_p, dict):
        _pkeys = (
            "turns_per_coil", "n_series", "slots_stator", "slot_fill_target",
            "line_load", "_sf_est", "parallel_circuits", "a", "frequency",
            "Winding__WireDiameter", "Winding__NumberOfStrands",
            "Slot__Bs0", "Slot__Bs1", "Slot__Bs2",
            "Slot__Hs0", "Slot__Hs1", "Slot__Hs2",
            "air_gap_flux_density", "stator_od", "stator_id", "core_length",
        )
        _ps = {k: _p.get(k) for k in _pkeys if _p.get(k) is not None}
        _emf = _p.get("_emf_winding")
        if isinstance(_emf, dict) and _emf.get("n_series") is not None:
            _ps["_emf_winding"] = {"n_series": _emf.get("n_series")}
        if _ps:
            out["params"] = _ps
    return out


def evaluate_quality_gate(state: dict, cfg: Optional[dict] = None,
                          runtime_config: Optional[dict] = None) -> Dict[str, Any]:
    """评估质量门禁, 返回结构化结果。

    返回字段:
        passed: bool          — 是否全部通过
        confidence: float|None— 评审置信度
        checks: list[dict]    — 各关卡明细 {name, passed, value, threshold, severity, note}
        fatal_conflicts: list — 收敛轨命中关键词
        reasons: list[str]    — 人类可读未通过原因
        summary: str          — 一句话摘要
    """
    if cfg is None:
        cfg = get_quality_gate_config(runtime_config)

    checks: List[Dict[str, Any]] = []
    reasons: List[str] = []
    fail_tags: List[str] = []
    _no_rec = False

    # ── 1. 评审置信度 ──
    conf_thr = float(cfg.get("confidence_threshold", 0.7))
    confidence = _read_confidence(state)
    if confidence is None:
        checks.append({
            "name": "评审置信度",
            "passed": True,
            "value": None,
            "threshold": conf_thr,
            "severity": "skip",
            "note": "未获取到结构化评审置信度, 跳过该门禁(不阻断)",
        })
        reasons.append("评审置信度: 未结构化(跳过)")
    else:
        ok = confidence >= conf_thr
        checks.append({
            "name": "评审置信度",
            "passed": ok,
            "value": round(confidence, 3),
            "threshold": conf_thr,
            "severity": "high" if not ok else "ok",
            "note": "" if ok else f"置信度 {confidence:.2f} < 阈值 {conf_thr}",
        })
        if not ok:
            reasons.append(f"评审置信度 {confidence:.2f} < 阈值 {conf_thr}")

    # ── 2. 硬约束 (最佳方案) ──
    _results_raw = state.get("results") or []
    _top1 = _results_raw[0] if (_results_raw and isinstance(_results_raw[0], dict)) else None
    best = _best_solution(state, cfg)
    # 磁密优先选优诊断: 若实际评估对象非原效率 Top-1, 记录(便于追溯重生压过问题)
    if best is not None and _top1 is not None and best is not _top1 and isinstance(best, dict) and isinstance(_top1, dict):
        _bi = None
        try:
            _bi = _results_raw.index(best)
        except (ValueError, AttributeError):
            _bi = "?"
        checks.append({
            "name": "磁密优先选优",
            "passed": True,
            "value": _bi,
            "threshold": 0,
            "severity": "info",
            "note": f"磁密硬约束下, 评估对象由效率Top-1(η={float(_top1.get('efficiency',0) or 0):.1f}%)"
                    f"改为带内效率最优(第{_bi+1}位, η={float(best.get('efficiency',0) or 0):.1f}%, "
                    f"Bt={best.get('stator_tooth_flux_density')}T/By={best.get('stator_yoke_flux_density')}T)",
        })
    hc = cfg.get("hard_constraints", {}) or {}
    _mag_fail = False              # G1 (v4.02.92): 磁密/λ 越界标记, 供 G2/G7 判定阶段用
    # G2 (v4.02.92): 磁密结构化方向 — {tooth/yoke/airgap: ok|high|low, lambda: ok|long|short}
    #   供 build_improvement_instructions 直接读方向发 Dsi 指令, 避免对格式化文案做
    #   子串匹配 (reasons 形如 "定子轭磁密 1.72T 过高(带 ..)", 数字夹在中间 ⇒ 紧邻子串失效)。
    _mag_flags: dict = {}
    # 2026-09-20: 槽满率偏低结构性失败标记 (3D 门判据 / 2d 集路由, 供 G2 动作链取用)
    _sf_low_fail = False
    # 2026-09-20: 槽满率字段缺失标记 (求解降级 ⇒ 判据未判定, 仅观察不阻断)
    _sf_unknown = False
    # 2026-09-20 (Fix3): 电磁负荷与材料利用率 — 门禁结果附加字段
    # v4.02.104 (用户指令 三.2): 改由 Core/material_analysis 按**规范口径**计算,
    #   与报告撰写员同一函数 ⇒ state/门禁/报告三处数字同源。
    #   (旧 Fix3 的 {A_A_per_m, Bd_T} / {eta_kW_per_t} 键名与规范 schema 不一致,
    #    曾被 compute_all 误当真相源沿用 ⇒ 报告整列显示「—」, 现已统一。)
    _em_load: dict = {}       # 规范 schema: line_load_A_per_m / B_delta_T / AB_peak_A_T_per_m2 ...
    _mat_util: dict = {}      # 规范 schema: steel_weight_kg / copper_weight_kg / D2L_m3 ...
    # 防御: _mag_values 在磁密门启用时才赋值, 提前置空避免 UnboundLocalError
    _mag_values: dict = {}
    # ★ P1-4 (2026-09-22 用户指令): Bt′/By′ 归一判据 + 带回代结果 (同上游防御置空)
    _mag_normalized: dict = {}
    # ★ 点② (2026-09-27 用户指令): 归一化固定带判定的结构化方向标志
    #   {tooth/yoke: ok|low|high} — 驱动几何动作分支 (narrow_tooth/shrink_yoke 等)
    _mag_norm_flags: dict = {}
    if best is None:
        checks.append({
            "name": "硬约束(效率/温升/槽满率)",
            "passed": True,
            "value": None,
            "threshold": None,
            "severity": "skip",
            "note": "无方案结果, 跳过硬约束校验",
        })
        reasons.append("硬约束: 无方案结果(跳过)")
    else:
        # ── Fix3 (2026-09-20): 电磁负荷 + 材料利用率计算 ──────────────────────
        #   位置: 放在判据之前, 供后续门禁结果附加字段与报告披露使用。
        #   数据来源: best (dict) 的几何/绕组字段, 以及 specs 的额定功率。
        #   失败时 fallback 为空 dict (不阻断门禁)。
        #   v4.02.104 (三.2): 首选 Core/material_analysis 规范口径 (与报告同源);
        #     其不可用/数据不足时才退回本地快速估算 (仅补 A/Bδ/q, 无铜铁分项)。
        try:
            _dso = float(best.get("stator_od") or 0)
            _dsi = float(best.get("stator_id") or 0)
            _cl = float(best.get("core_length") or 0)
            _q = float(best.get("line_load") or best.get("specific_loading_a") or 0)
            _bdg_v = float(best.get("air_gap_flux_density") or 0)
            _specs_local = state.get("specs") or {}
            _p_kw = float(_specs_local.get("rated_power_kw", 0) or 0)
            _w_kg = float(best.get("stator_weight_kg") or best.get("stator_mass_kg") or 0)
            _em_load = {}
            _mat_util = {}
            # (a) 规范口径 (单一真相源)
            try:
                from Core.material_analysis import compute_all as _ma_compute_all  # type: ignore
            except Exception:
                try:
                    from material_analysis import compute_all as _ma_compute_all  # type: ignore
                except Exception:
                    _ma_compute_all = None
            if _ma_compute_all is not None and best:
                try:
                    _ma = _ma_compute_all(best, _specs_local, None)
                    if isinstance(_ma, dict) and _ma.get("available"):
                        _em_load = _ma.get("electromagnetic_load") or {}
                        _mat_util = _ma.get("material_utilization") or {}
                except Exception:
                    _em_load, _mat_util = {}, {}
            # (b) 兜底快速估算 (规范计算不可用/数据不足时)
            if not _em_load and not _mat_util:
                if _q > 0 and _bdg_v > 0:
                    _ab = _q * _bdg_v  # A·T/m² (线负荷 × 气隙磁密)
                    _em_load["A_A_per_m"] = round(_q, 1)
                    _em_load["Bd_T"] = round(_bdg_v, 4)
                    _em_load["AB_A_T_per_m2"] = round(_ab, 1)
                if _dso > 0 and _dsi > 0 and _cl > 0 and _p_kw > 0:
                    _d = (_dso + _dsi) / 2.0       # 等效直径 (mm)
                    _dm = _d / 1000.0              # 换算 m
                    _area = _dm * _dm              # m²
                    _q_specific = 2.0 * _p_kw * 1000.0 / (3.141592653589793 * _area)  # W/m²
                    _em_load["q_W_per_m2"] = round(_q_specific, 1)
                if _w_kg > 0 and _p_kw > 0:
                    _mat_util["eta_kW_per_t"] = round(_p_kw / (_w_kg / 1000.0), 2)
                    _mat_util["mass_kg"] = _w_kg
                    _mat_util["method"] = "stator_weight"
                elif _dso > 0 and _dsi > 0 and _cl > 0:
                    # fallback: 用铁心体积 × 硅钢密度估算质量 (ρ≈7650 kg/m³)
                    _vol_mm3 = 3.141592653589793 / 4.0 * (_dso * _dso - _dsi * _dsi) * _cl
                    _vol_m3 = _vol_mm3 * 1e-9
                    _mass_est = _vol_m3 * 7650.0
                    _mat_util["eta_kW_per_t"] = round(_p_kw / (_mass_est / 1000.0), 2) if _p_kw > 0 else None
                    _mat_util["mass_kg"] = round(_mass_est, 1)
                    _mat_util["method"] = "volume_estimated"
        except Exception as _fix3_e:
            import sys as _sys2
            _sys2.stderr.write("Fix3 EXCEPT: " + repr(_fix3_e) + "\n")
            _sys2.stderr.flush()
            _em_load, _mat_util = {}, {}
        # ── 结束 Fix3 ────────────────────────────────────────────────────────
        # v4.01.059 (P1-#5): 按电机类型解析效率下限 (永磁/异步/开关磁阻分级), 否则用全局默认
        _mt = state.get("motor_type") or (state.get("specs") or {}).get("motor_type") or ""
        eff_min = _resolve_eff_min(hc, _mt)
        if eff_min > 0:
            eff = float(best.get("efficiency", 0) or 0)
            ok = eff >= eff_min
            checks.append({"name": "效率下限", "passed": ok, "value": round(eff, 2),
                           "threshold": eff_min, "severity": "high" if not ok else "ok",
                           "note": "" if ok else f"最佳效率 {eff:.1f}% < 下限 {eff_min}%"})
            if not ok:
                reasons.append(f"效率 {eff:.1f}% < {eff_min}%")
            if eff <= 0:
                _record_missing_field_warning(checks, "efficiency", "效率")

        temp_max = float(hc.get("temp_max", 0.0) or 0.0)
        if temp_max > 0:
            t = float(best.get("max_temp", 0) or 0)
            ok = t <= temp_max
            checks.append({"name": "温升上限", "passed": ok, "value": round(t, 1),
                           "threshold": temp_max, "severity": "high" if not ok else "ok",
                           "note": "" if ok else f"最佳温升 {t:.0f}°C > 上限 {temp_max}°C"})
            if not ok:
                reasons.append(f"温升 {t:.0f}°C > {temp_max}°C")
            elif t <= 0:
                _record_missing_field_warning(checks, "max_temp", "温升")

        sf_max = float(hc.get("slot_fill_max", 0.0) or 0.0)
        if sf_max > 0:
            # S1 (RUN49-P2, 2026-09-24): 槽满率「上限」与「下限」**同口径归一** ——
            #   旧实现: 上限项(本处)取原值不归一, 下限项(_sf) 才做 >1.5 → /100,
            #   ⇒ 同一个 slot_fill_rate 被两套规则各解一个方向: 真值 1.69 一次读成
            #   「超限 1.69 > 0.75」、又一次读成「偏低 0.017 < 0.65」(1.69/100),
            #   同一轮同时触发上/下限两条报警 —— 物理自相矛盾 (RUN49 三轮 100% 吻合)。
            # S1 增强 (2026-09-24 用户指令): 实测值 >1.0 ⇒ **不 /100**, 直接判超限/异常。
            #   worker 已从 .res 百分数做过 /100, 实测值恒为比率; 1.692 即 169.2% 崩溃值,
            #   再 /100 会把它读成「偏低 0.017」⇒ 真信号消失 + 反向错处方 (见
            #   _normalize_slot_fill docstring)。上下限共用该函数, 口径唯一。
            sf, _sf_crash = _normalize_slot_fill(
                best.get("slot_fill_rate", best.get("slot_fill", 0)))
            # 阈值归一 (仅阈值, 实测值不再 /100): 管理端 slot_fill_max 可能以
            #   百分数下发(如 75); 不归一则 0.70 <= 75 恒真 ⇒ 上限形同虚设。
            if sf_max > SF_PCT_INPUT_TH:
                sf_max = sf_max / 100.0
            if _sf_crash:
                ok = False
                checks.append({"name": "槽满率上限", "passed": False,
                               "value": round(sf, 3), "threshold": sf_max,
                               "severity": "critical",
                               "note": (f"槽满率异常 {sf:.2f} (={sf*100:.0f}%) > 100% 物理不可能"
                                        " —— 上游几何喂参失衡(线径/匝数 vs 槽面积), 按超限判定, 不按『偏低』归一")})
                reasons.append(f"槽满率异常 {sf*100:.0f}% > 100% (超限, 上游几何失衡)")
            else:
                ok = sf <= sf_max
                checks.append({"name": "槽满率上限", "passed": ok, "value": round(sf, 3),
                               "threshold": sf_max, "severity": "high" if not ok else "ok",
                               "note": "" if ok else f"槽满率 {sf:.2f} > 上限 {sf_max}"})
                if not ok:
                    reasons.append(f"槽满率 {sf:.2f} > {sf_max}")

        # ── 2c. 2D 磁路带 (G1, v4.02.92) ──
        #   仅当字段存在(值>0)才判, 缺失不阻断。mode=hard 才阻断, 默认 warning 仅记录。
        #   齿磁密 Bt 与 Dsi 解耦 → 不据以发 Dsi 动作(见 G2); 轭磁密 By∝1/(Dso-Dsi)→减Dsi降By。
        _mg = cfg.get("magnetic_gate") or {}
        if _mg.get("enabled", True):
            _mode = _mg.get("mode", "warning")
            # 2026-09-20: 磁密实测值 (供 build_improvement_instructions 按缺口自适应定步长)
            _mag_values: dict = {}
            # P4 (v4.02.93): 新增几何门是否参与阻断 (默认 False = 纯观察一轮, 记录失效率)
            _geom_hard = bool(_mg.get("geom_gate_hard", False))
            # 2026-09-20: 磁密硬约束 (独立于 mode, 由 flux_gate_hard 单独开关)
            _flux_hard = bool(_mg.get("flux_gate_hard", False))
            _mag_eff_hard = (_mode == "hard") or _flux_hard   # 磁密项是否可阻断
            _bt_b = tuple(_mg.get("tooth_flux_band", (1.2, 1.7)))
            _by_b = tuple(_mg.get("yoke_flux_band", (1.0, 1.6)))
            _bdg_b = tuple(_mg.get("air_gap_flux_band", (0.55, 0.85)))
            _lam_b = tuple(_mg.get("lambda_band", (0.3, 4.0)))
            _lam_abs = tuple(_mg.get("lambda_abs_mm", (20.0, 3500.0)))
            _sf_lo = float(_mg.get("slot_fill_min", 0.65))
            # 2026-09-20: 下限口径归一 —— 管理端 (ai_flow_config slot_fill_min) 以
            #   **百分数** 下发 (默认 70), 门禁内部用小数 (0.70)。若原样比较,
            #   0.28 < 70 恒真 ⇒ 一整案全灭。此处与下方 _sf 应用同一归一规则。
            if _sf_lo > 1.5:
                _sf_lo = _sf_lo / 100.0
            # 齿磁密 Bt
            _bt = float(best.get("stator_tooth_flux_density", 0) or 0)
            if _bt > 0:
                _ok = _bt_b[0] <= _bt <= _bt_b[1]
                _mag_fail = _mag_fail or (not _ok)
                _mag_flags["tooth"] = "ok" if _ok else ("high" if _bt > _bt_b[1] else "low")
                _mag_values["tooth"] = round(_bt, 4)
                _hard = (not _ok) and _mag_eff_hard
                checks.append({"name": "定子齿磁密",
                               "passed": (True if not _mag_eff_hard else _ok),
                               "value": round(_bt, 3), "threshold": list(_bt_b),
                               "severity": "high" if _hard else "warning",
                               "note": "" if _ok else f"齿磁密 {_bt:.2f}T 出带 {list(_bt_b)}"})
                if _hard:
                    reasons.append(f"定子齿磁密 {_bt:.2f}T 出带 {list(_bt_b)}")
            elif _mode != "skip":
                _record_missing_field_warning(checks, "stator_tooth_flux_density", "定子齿磁密")
            # 轭磁密 By
            _by = float(best.get("stator_yoke_flux_density", 0) or 0)
            if _by > 0:
                _ok = _by_b[0] <= _by <= _by_b[1]
                _mag_fail = _mag_fail or (not _ok)
                _mag_flags["yoke"] = "ok" if _ok else ("high" if _by > _by_b[1] else "low")
                _mag_values["yoke"] = round(_by, 4)
                _hard = (not _ok) and _mag_eff_hard
                _dir = "过高" if (not _ok and _by > _by_b[1]) else ("过低" if not _ok else "")
                checks.append({"name": "定子轭磁密",
                               "passed": (True if not _mag_eff_hard else _ok),
                               "value": round(_by, 3), "threshold": list(_by_b),
                               "severity": "high" if _hard else "warning",
                               "note": "" if _ok else f"轭磁密 {_by:.2f}T {_dir}(带 {list(_by_b)})"})
                if _hard:
                    reasons.append(f"定子轭磁密 {_by:.2f}T {_dir}(带 {list(_by_b)})")
            elif _mode != "skip":
                _record_missing_field_warning(checks, "stator_yoke_flux_density", "定子轭磁密")
            # 气隙磁密 Bδ — ★ 点⑥ (2026-09-27 用户指令): 「改为警告, 为动作」
            #   判定项恒为 warning **不阻断**(不进 reasons、恒 passed=True),
            #   仅作动作触发源: 偏低 → reduce_turns 提 Bδ
            #   (build_improvement_instructions airgap 分支, 点⑥动作)。
            #   旧式 _mode=='hard' 时硬阻断的口径移除 (Bt/By 阻断语义不变)。
            _bdg = float(best.get("air_gap_flux_density", 0) or 0)
            if _bdg > 0:
                _ok = _bdg_b[0] <= _bdg <= _bdg_b[1]
                _mag_fail = _mag_fail or (not _ok)   # 仍驱动动作签发 (_mag_failed)
                _mag_flags["airgap"] = "ok" if _ok else ("high" if _bdg > _bdg_b[1] else "low")
                # ★ P1-1 (2026-09-22): 落 Bδ 实测值 —— build_improvement_instructions
                #   据此把旧的**相对**步长换算成**绝对**步长 (clamp[Bδ缺口,0.01,0.02]T)。
                _mag_values["airgap"] = round(_bdg, 4)
                checks.append({"name": "气隙磁密",
                               "passed": True,
                               "value": round(_bdg, 3), "threshold": list(_bdg_b),
                               "severity": "warning",
                               "note": "" if _ok else
                               f"气隙磁密 {_bdg:.2f}T 出带 {list(_bdg_b)} "
                               "(★点⑥ 警告项不阻断, 偏低由减匝动作承担)"})
            elif _mode != "skip":
                _record_missing_field_warning(checks, "air_gap_flux_density", "气隙磁密")
            # 长径比 λ = L/Dsi (G5 回环判据)
            _cl = float(best.get("core_length", 0) or 0)
            _sid = float(best.get("stator_id", 0) or 0)
            if _cl > 0 and _sid > 0:
                _lam = _cl / _sid
                _in = (_lam_b[0] <= _lam <= _lam_b[1]) and (_lam_abs[0] <= _cl <= _lam_abs[1])
                _mag_fail = _mag_fail or (not _in)
                _mag_flags["lambda"] = ("ok" if _in
                                        else ("long" if (_lam > _lam_b[1] or _cl > _lam_abs[1])
                                              else "short"))
                # ★改造⑤ (2026-09-28 用户指令): λ 越界降级为警告项 —— 与 magnetic_gate.mode
                #   解耦, 恒 warning 不阻断、不写 reasons (消除「判据不阻断却仍触发动作」不一致)。
                _hard = False
                _dir = "过长" if (not _in and _lam > _lam_b[1]) else ("过短" if not _in else "")
                checks.append({"name": "长径比 λ",
                               "passed": True,
                               "value": round(_lam, 3),
                               "threshold": {"λ": list(_lam_b), "L(mm)": list(_lam_abs)},
                               "severity": "warning",
                               "note": "" if _in else f"λ={_lam:.2f} {_dir}(λ {list(_lam_b)}, L {list(_lam_abs)}mm)"
                                                      " —— 警告项不阻断(★改造⑤)"})
                # ★改造⑤: 不写 reasons ⇒ joined 不命中 ⇒ 不触发任何调整动作 (仅观察)
            # ── ★ P1-4 (2026-09-22 用户指令) / ★ 点② 重构 (2026-09-27 用户指令) ──
            #   归一: Bt′ = Bt·Bδ₀/Bδ (Bδ₀ = BETA_REF_T = 0.7T) —— 投影到参考气隙磁密。
            #   【点②】判定带改**固定带** (不再随 Bδ 同步缩放 ⇒ 不再与原始判代数恒等):
            #     归一后进带 = 纯励磁偏低 (Bδ 杠杆管) ; 出带偏低 = 几何偏大 (点④⑤杠杆管)。
            #   归一化方向标志 _mag_norm_flags 驱动 build_improvement_instructions 的
            #   齿/轭几何动作分支 (narrow_tooth / shrink_yoke / decrease_stator_od);
            #   归一检查项 (定子齿/轭磁密(归一)) 落 checks 供报告/审计, 不阻断。
            try:
                _mag_normalized = bdelta_sufficiency(
                    _bt, _by, _bdg, _bt_b, _by_b, _bdg_b)
            except Exception:
                _mag_normalized = {}
            # ★ 点②: 归一化结构化方向 (ok/low/high) + 归一检查项 (warning, 不阻断)
            _mag_norm_flags = {}
            try:
                _bn_t = _mag_normalized.get("bt_norm")
                _bn_tb = _mag_normalized.get("bt_norm_band")
                if _bn_t and _bn_tb:
                    _bn_t = float(_bn_t)
                    _mag_norm_flags["tooth"] = (
                        "ok" if _bn_tb[0] <= _bn_t <= _bn_tb[1]
                        else ("high" if _bn_t > _bn_tb[1] else "low"))
                    checks.append({"name": "定子齿磁密(归一)",
                                   "passed": (_mag_norm_flags["tooth"] == "ok"),
                                   "value": round(_bn_t, 3),
                                   "threshold": list(_bn_tb),
                                   "severity": "warning",
                                   "note": "★点② Bt·0.70/Bδ 投影后固定带再判 "
                                           "(分离励磁/几何), 不阻断"})
            except Exception:
                pass
            try:
                _bn_y = _mag_normalized.get("by_norm")
                _bn_yb = _mag_normalized.get("by_norm_band")
                if _bn_y and _bn_yb:
                    _bn_y = float(_bn_y)
                    _mag_norm_flags["yoke"] = (
                        "ok" if _bn_yb[0] <= _bn_y <= _bn_yb[1]
                        else ("high" if _bn_y > _bn_yb[1] else "low"))
                    checks.append({"name": "定子轭磁密(归一)",
                                   "passed": (_mag_norm_flags["yoke"] == "ok"),
                                   "value": round(_bn_y, 3),
                                   "threshold": list(_bn_yb),
                                   "severity": "warning",
                                   "note": "★点② By·0.70/Bδ 投影后固定带再判 "
                                           "(分离励磁/几何), 不阻断"})
            except Exception:
                pass
            # ── 槽满率下限 (2026-09-20 用户指令: 由「纯 warning」升级为可阻断) ──────
            #   判据位置: 3D 门 (评估的是 L1 实解后的结果量 slot_fill_rate)。
            #   物理: SF 偏小 ⇔ 槽面积相对铜量虚大 (根因 SLOT_DEPTH_MODE='fill' 吃满
            #     可用径向空间)。修复是**冲片几何**操作 ⇒ 归属 2d 集, 路由回
            #     2D 流程起点 (regenerate_2d); 动作见 build_improvement_instructions
            #     「shrink_slot_keep_yoke」= 减槽高 Hs + 同步减外径 Dso (ΔDso=2ΔHs),
            #     保 hj 与定子轭磁密 By 不变。
            # S1 增强 (2026-09-24 用户指令): 实测值 >1.0 ⇒ 不 /100, 判超限/异常。
            #   与上限门共用 _normalize_slot_fill (口径唯一); 崩溃值 (如 1.692=169.2%)
            #   **不再 /100 变「偏低 0.017」** —— 那会下发 shrink_slot_keep_yoke
            #   (减槽高抬 SF) 反向错处方。崩溃时: 跳过「偏低」判定 + 不置
            #   _sf_low_fail (避免路由回 2D 减槽高), 只记 critical 观察项防静默;
            #   阻断由上限门的 critical + reasons 承担。
            _sf, _sf_crash = _normalize_slot_fill(
                best.get("slot_fill_rate", best.get("slot_fill", 0)))
            if _sf_crash:
                checks.append({"name": "槽满率异常", "passed": False,
                               "value": round(_sf, 3), "threshold": _sf_lo,
                               "severity": "critical",
                               "note": (f"槽满率 {_sf:.2f} (={_sf*100:.0f}%) > 100% 物理不可能"
                                        " —— 真信号为**超限**(上游几何喂参失衡: 线径/匝数 vs 槽面积), "
                                        "本轮跳过『偏低』判定, 不发 2D 减槽高处方")})
            elif 0 < _sf < _sf_lo:
                _sf_hard = bool(_mg.get("slot_fill_low_hard", False))
                if _sf_hard:
                    _sf_low_fail = True
                checks.append({"name": "槽满率下限",
                               "passed": (not _sf_hard),
                               "value": round(_sf, 3), "threshold": _sf_lo,
                               "severity": "high" if _sf_hard else "warning",
                               "note": (f"槽满率偏低 {_sf:.3f} < 下限 {_sf_lo}"
                                        + (" ⇒ 减槽高 Hs + 同步减外径 Dso (保轭宽/By 不变)"
                                           if _sf_hard else " (warning)"))})
                if _sf_hard:
                    reasons.append(
                        f"槽满率偏低 {_sf:.3f} < 下限 {_sf_lo} (2D 冲片: 槽深偏大, 收小槽高保轭宽)")
            elif _sf <= 0:
                # 2026-09-20: 槽满率字段缺失/为 0 —— 典型于**求解降级** (solve_mode=SIMULATED,
                #   worker 未回填 slot_fill_rate; RUN28 实测全池 0/5 真实解 ⇒ SF 全 None)。
                #   旧实现静默跳过 ⇒ 门禁只留「槽满率上限 value=0.0」, 无法区分
                #   「槽满率正常」与「槽满率未知」。此处补 **warning 级观察项**
                #   (passed=True ⇒ 不阻断; 不进 reasons ⇒ 不触发卡死检测/转人工),
                #   仅使数据缺失可见、可审计。
                _sf_unknown = True
                checks.append({"name": "槽满率缺失", "passed": True,
                               "value": 0.0, "threshold": _sf_lo,
                               "severity": "warning",
                               "note": "槽满率字段缺失/为 0 (求解降级或未回填) ⇒ "
                                       "槽满率下限判据本轮未判定"})
            # 齿槽比 ζ (近似, 仅 warning, 永不阻断)
            _zeta = _estimate_zeta(best)
            if _zeta is not None:
                _zb = tuple(_mg.get("zeta_band", (0.90, 1.719)))
                _ok = _zb[0] <= _zeta <= _zb[1]
                checks.append({"name": "齿槽比 ζ", "passed": True,
                               "value": round(_zeta, 3), "threshold": list(_zb),
                               "severity": "warning",
                               "note": "" if _ok else f"齿槽比 {_zeta:.2f} 出带 {list(_zb)}(近似)"})

            # ── P4 (v4.02.93): 2D 几何门新增项 (轭厚/槽深比 · Dso/Dsi 比 · 齿宽 · Qs) ──
            #   warning 观察态 (mode=hard 才转阻断); 函数未加载/数据缺失自动跳过。
            def _add_geom_check(_name, _fn, *_args, **_kwargs):
                if _fn is None:
                    return
                try:
                    _gok, _gdet = _fn(*_args, **_kwargs)
                except Exception:
                    return
                if not isinstance(_gdet, dict) or _gdet.get("error"):
                    return
                _hard = (not _gok) and _geom_hard
                checks.append({"name": _name,
                               "passed": (True if not _geom_hard else _gok),
                               "value": _gdet,
                               "threshold": _gdet.get("band"),
                               "severity": "high" if _hard else "warning",
                               "note": "" if _gok else f"{_name} 越界: {_gdet}"})
                if _hard:
                    reasons.append(f"{_name} 越界: {_gdet}")
            # 轭厚/槽深比 hj/hs (用户诉求②)
            # 方案B (2026-09-19): 传入**真实槽深** —— 优先 RMxprt/L1 回写的 Slot__Hs2,
            #   其次结果字典 slot_depth / slot_height; 取不到才回退几何反推 (hs_src="derived")。
            # ⚠ slot_depth 必须用**关键字**传入: 签名 (yoke, od, id, band, slot_depth)
            #   中 band 在第 4 位, 位置传参会被误当 band ⇒ band[0] TypeError ⇒ 检查项静默丢失。
            #   (故上方闭包 _add_geom_check 已扩展支持 **kwargs)
            _real_hs = _resolve_slot_depth(best)
            # 按极数分档解析带 (配置 yoke_slot_ratio_band_by_poles 优先, 否则真源按 poles 分档)
            _ysr_band, _p_best = None, best.get("poles")
            _cfg_ysr = _mg.get("yoke_slot_ratio_band_by_poles")
            if isinstance(_cfg_ysr, dict) and _cfg_ysr and _p_best:
                try:
                    _pk = max(2, int(round(float(_p_best))))
                    _pk = 2 if _pk <= 2 else (4 if _pk == 4 else 6)
                    _v = _cfg_ysr.get(str(_pk), _cfg_ysr.get(_pk))
                    if isinstance(_v, (list, tuple)) and len(_v) == 2:
                        _ysr_band = (float(_v[0]), float(_v[1]))
                except (TypeError, ValueError):
                    _ysr_band = None
            _add_geom_check("轭厚/槽深比", _m2d_yoke_slot_ratio_ok,
                            best.get("yoke_thickness"), best.get("stator_od"),
                            best.get("stator_id"), slot_depth=_real_hs,
                            poles=_p_best, band=_ysr_band)
            # Dso/Dsi 比 (按极数, 用户诉求④; 真源 id_ratio_by_poles)
            _add_geom_check("Dso/Dsi 比", _m2d_dso_dsi_ok,
                            best.get("stator_id"), best.get("stator_od"),
                            best.get("poles"))
            # 齿宽 by Dsi (用户诉求: 齿宽取值范围 + 机械/工艺下限)
            # ★ G3 (2026-10-11 用户授权): 传实际 Bδ + dynamic_min ⇒ 磁上限反推按 Bδ 缩放
            #   (Bδ 缺失回退 None ⇒ tooth_width_ok 零回归走原 r_lo·t 固定反推)。
            _add_geom_check("齿宽范围", _m2d_tooth_width_ok,
                            best.get("stator_id"), best.get("slots_stator"),
                            best.get("tooth_width"),
                            b_delta=best.get("air_gap_flux_density"),
                            dynamic_min=QG_TOOTH_MIN_DYNAMIC)
            # Qs by Dsi (用户诉求: 定子槽数取值范围, q∈[2,8])
            _add_geom_check("定子槽数范围", _m2d_qs_ok,
                            best.get("stator_id"), best.get("slots_stator"),
                            best.get("poles"))
            # 起动(堵转)转矩倍数 (应用相关; 默认 0 不校验, 用户可配; 属 L2 几何层触发)
            _tst_min = float(_mg.get("starting_torque_pu_min", 0.0) or 0.0)
            if _tst_min > 0:
                _tst = float(best.get("starting_torque") or 0)
                _tr = float(best.get("torque") or 0)
                if _tst > 0 and _tr > 0:
                    _tst_pu = _tst / _tr
                    _ok_t = _tst_pu >= _tst_min
                    _hard = (not _ok_t) and _geom_hard
                    checks.append({"name": "起动转矩倍数",
                                   "passed": (True if not _geom_hard else _ok_t),
                                   "value": round(_tst_pu, 3), "threshold": _tst_min,
                                   "severity": "high" if _hard else "warning",
                                   "note": "" if _ok_t else f"起动转矩倍数 {_tst_pu:.2f} < {_tst_min}"})
                    if _hard:
                        reasons.append(f"起动转矩倍数 {_tst_pu:.2f} < {_tst_min}")
            # 功率因数下限 (默认 0 不校验)
            _pf_min = float(_mg.get("power_factor_min", 0.0) or 0.0)
            if _pf_min > 0:
                _pf = float(best.get("power_factor") or 0)
                if _pf > 0:
                    _ok_pf = _pf >= _pf_min
                    _hard = (not _ok_pf) and _geom_hard
                    checks.append({"name": "功率因数下限",
                                   "passed": (True if not _geom_hard else _ok_pf),
                                   "value": round(_pf, 3), "threshold": _pf_min,
                                   "severity": "high" if _hard else "warning",
                                   "note": "" if _ok_pf else f"功率因数 {_pf:.3f} < {_pf_min}"})
                    if _hard:
                        reasons.append(f"功率因数 {_pf:.3f} < {_pf_min}")

    # ── 2b. RMxprt 求解故障 (v4.02.68, 系列一「故障信息进评价」) ──
    #   原门禁只校验 best 的数值指标 (效率/温升/槽满率), RMxprt 故障文本
    #   (solve_failed / error / error_detail) 未进入门禁与 AI 评价 —— 下游只见
    #   "eff=0.0%" 表象, 不见 "几何非法/网格失败/线规溢出" 根因 (用户实测方案9)。
    #   规则:
    #     · best 出现故障 → 未通过 (退化批次不应放行);
    #     · 仅非 best 故障 → 通过, 但 severity=warning 并把故障明细写入 diagnostics,
    #       供支持报告/AI 评价披露 (不据非 best 故障阻断整轮)。
    _diag: List[str] = []
    _best_fault = bool(best is not None and (
        best.get("solve_failed") or best.get("error") or best.get("error_detail")))
    _fault_items = []
    for _i, _r in enumerate(state.get("results") or []):
        if not isinstance(_r, dict):
            continue
        _et = str(_r.get("error_detail") or _r.get("error") or "").strip()
        if _r.get("solve_failed") or _et:
            _fault_items.append((_i + 1, _et))
    if _best_fault:
        _bd = str(best.get("error_detail") or best.get("error") or "")[:120]
        checks.append({"name": "RMxprt 求解状态", "passed": False, "value": "最佳方案故障",
                       "threshold": "无故障", "severity": "high",
                       "note": f"最佳方案 RMxprt 求解故障: {_bd}"})
        reasons.append(f"RMxprt 求解错误: {_bd}")
        _diag.append(f"最佳方案 RMxprt 故障: {_bd}")
    elif _fault_items:
        _samples = "; ".join(f"方案{i}:{t[:60]}" for i, t in _fault_items[:4])
        checks.append({"name": "RMxprt 求解状态", "passed": True,
                       "value": f"{len(_fault_items)} 个方案故障", "threshold": None,
                       "severity": "warning",
                       "note": f"非最佳方案 RMxprt 故障 (已排除出 L0 残差统计): {_samples}"})
        _diag.append(f"RMxprt 故障方案 {len(_fault_items)} 个: {_samples}")
    else:
        checks.append({"name": "RMxprt 求解状态", "passed": True, "value": "全部正常",
                       "threshold": None, "severity": "ok",
                       "note": "全部方案求解正常, 无 RMxprt 故障"})
    _diag.extend(_l0_l1_diagnostics(state))

    # ── 3. 额外硬约束 (M2, 默认开启) + 推荐方案存在性 (M1, 默认开启) ──
    # M2: 额外硬约束 (额定功率达标比 / 额定转矩达标比 / 定子电密) 仅当
    #     extra_hard_constraints_enabled=True 时校验 (v4.02.22 起默认开启)
    _extra_on = bool(cfg.get("extra_hard_constraints_enabled", False))
    # ★ P0-5: 达标比相对容差 (吸收 RMxprt 收敛数值噪声, 见 ACHIEVE_RATIO_TOL_DEFAULT)
    _ach_tol = float(cfg.get("achieve_ratio_tol", ACHIEVE_RATIO_TOL_DEFAULT) or 0.0)
    if _ach_tol < 0:
        _ach_tol = 0.0
    if _extra_on:
        # (a) 额定功率达标比: 真实求解功率 / 目标功率 ≥ power_min_ratio·(1 − tol)
        #     —— 直接拦截退化几何导致的 0 功率 / 估算效率伪装 (rmxprt_tools 已标 solve_failed)
        pw_ratio = float(hc.get("power_min_ratio", 0.0) or 0.0)
        if pw_ratio > 0:
            _target_p = float((state.get("specs") or {}).get("power_kw") or 0)
            _achieved_p = float(best.get("power", 0) or 0)
            if _target_p > 0:
                _pr = _achieved_p / _target_p
                _pw_eff = pw_ratio * (1.0 - _ach_tol)      # P0-5: 相对容差后的实际阈值
                ok = _pr >= _pw_eff
                checks.append({"name": "额定功率达标比", "passed": ok, "value": round(_pr, 3),
                               "threshold": round(_pw_eff, 4), "severity": "high" if not ok else "ok",
                               "note": "" if ok else f"额定功率达标比 {_pr:.3f} < 下限 {_pw_eff:.4f} "
                                                     f"(求解 {_achieved_p:.1f}/{_target_p:.1f}kW, "
                                                     f"容差 {_ach_tol*100:.1f}%)"})
                if not ok:
                    reasons.append(f"额定功率达标比 {_pr:.3f} < {_pw_eff:.4f}")
            else:
                checks.append({"name": "额定功率达标比", "passed": True, "value": None,
                               "threshold": pw_ratio, "severity": "skip",
                               "note": "目标功率(power_kw)缺失, 跳过额定功率达标比校验(不阻断)"})

        # (b) 额定转矩达标比: 额定转矩 / 目标转矩 ≥ torque_min_ratio
        #     —— 优先读 rated_torque_ratio; 缺失则用真实求解额定转矩 / 目标(由功率转速推导) 兜底
        # ★改造③ (2026-09-28 用户指令): 「转矩偏低」降级为警告项 —— 不阻断、不进 reasons、
        #   不触发任何调整动作 (原 high 硬阻断会在超尺寸场景下发 lengthen_core /
        #   increase_wire_gauge 等反向杠杆, RUN58 轮1 实证有害)。
        #   安全地板: _tr < 0.5 视为崩溃级 (T≈0 失真方案), 仍 hard 阻断 (见 :116 设计注释)。
        torque_ratio = float(hc.get("torque_min_ratio", 0.0) or 0.0)
        if torque_ratio > 0:
            _tr = float(best.get("rated_torque_ratio", best.get("torque_ratio", 0)) or 0)
            if _tr <= 0:
                _tt = _target_torque_from_specs(state)
                _at = float(best.get("rated_torque") or best.get("torque") or 0)
                if _tt > 0 and _at > 0:
                    _tr = _at / _tt
            ok = _tr >= torque_ratio * (1.0 - _ach_tol)     # P0-5: 相对容差
            _tq_eff = torque_ratio * (1.0 - _ach_tol)
            _tq_crash = (not ok) and (_tr < 0.5)            # 崩溃地板: T≈0 仍硬阻断
            _tq_warn = (not ok) and (not _tq_crash)         # 偏低 ⇒ 警告项 (★改造③)
            checks.append({"name": "额定转矩达标比",
                           "passed": (True if _tq_warn else ok),
                           "value": round(_tr, 3),
                           "threshold": round(_tq_eff, 4),
                           "severity": ("warning" if _tq_warn else ("high" if not ok else "ok")),
                           "note": "" if ok else f"额定转矩达标比 {_tr:.3f} < 下限 {_tq_eff:.4f} "
                                                 f"(容差 {_ach_tol*100:.1f}%)"
                                                 + ("" if _tq_crash else " —— 警告项不阻断(★改造③)")})
            if _tq_crash:
                reasons.append(f"额定转矩达标比 {_tr:.3f} < {_tq_eff:.4f} (崩溃级, 硬阻断)")

        # (c) 定子电密上限
        cd_max = float(hc.get("current_density_max", 0.0) or 0.0)
        if cd_max > 0:
            _jd = float(best.get("current_density", best.get("J1", 0)) or 0)
            ok = _jd <= cd_max
            checks.append({"name": "定子电密上限", "passed": ok, "value": round(_jd, 2),
                           "threshold": cd_max, "severity": "high" if not ok else "ok",
                           "note": "" if ok else f"定子电密 {_jd:.1f} > 上限 {cd_max} A/mm²"})
            if not ok:
                reasons.append(f"定子电密 {_jd:.1f} > {cd_max} A/mm²")

        # (c2) S7 (2026-09-18): 线规钳顶越界 → 触发重生闭环
        #   候选 _wire_clamped 由 _apply_em_load_geometry 写入 (线径触 [0.3,4.5]mm 且并绕根数
        #   已升至上限仍越界)。受 wire_clamp_gate 开关控制 (默认开), 命中即阻断并给出原因,
        #   由 build_improvement_instructions 生成 wire_resize 指令, 重生经 orchestrator 突破根数上限。
        if cfg.get("wire_clamp_gate", True) and bool(best.get("_wire_clamped")):
            checks.append({"name": "线规钳顶越界", "passed": False, "value": "clamped",
                           "threshold": None, "severity": "high",
                           "note": "导线线径触及 [0.3,4.5]mm 硬边界, 并绕根数已升至上限仍越界"})
            reasons.append("线规越界(线径钳顶/并绕根数已达上限)")

    # M1: 支持报告(review) 必须给出推荐方案; 缺失/否定表述视为未通过 (报告为空时跳过, 不阻断)
    rc = cfg.get("recommendation_check") or {}
    if rc.get("enabled", True):
        _review_text = state.get("review", "") or ""
        if not _review_text.strip():
            checks.append({"name": "推荐方案存在性", "passed": True, "value": None,
                           "threshold": None, "severity": "skip",
                           "note": "支持报告尚未生成, 跳过推荐存在性校验(不阻断)"})
        else:
            _no_rec = _detect_no_recommendation(_review_text,
                                                rc.get("negation_patterns") or [])
            if _no_rec:
                checks.append({"name": "推荐方案存在性", "passed": False, "value": "无推荐",
                               "threshold": "需给出推荐方案", "severity": "high",
                               "note": "支持报告未给出推荐方案(含'无推荐方案'等否定表述)"})
                reasons.append("支持报告: 无推荐方案")
            else:
                checks.append({"name": "推荐方案存在性", "passed": True, "value": "已给出推荐",
                               "threshold": None, "severity": "ok", "note": "支持报告已给出推荐方案"})

    # ── 4. 收敛轨严重冲突 ──
    con_report = state.get("con_report", "") or ""
    kws = cfg.get("convergence_fatal_keywords", []) or []
    fatal_hits = _scan_fatal(con_report, kws)
    if fatal_hits:
        checks.append({"name": "收敛轨严重冲突", "passed": False, "value": fatal_hits,
                       "threshold": None, "severity": "high",
                       "note": f"命中关键词: {fatal_hits}"})
        reasons.append(f"收敛轨严重冲突关键词: {fatal_hits}")
    else:
        checks.append({"name": "收敛轨严重冲突", "passed": True, "value": [],
                       "threshold": None, "severity": "ok", "note": "未检出严重冲突"})

    passed = all(c["passed"] for c in checks)
    fail_tags = [c.get("name", "") for c in checks if not c.get("passed")]
    no_recommendation = any(
        (not c.get("passed")) and c.get("name") == "推荐方案存在性" for c in checks)
    # G7 (v4.02.92): 失败阶段分类 — 2D(磁路) / 3D(性能) / mixed, 供双阶段门回退路由
    # P1 (v4.02.93): 判据集重分类 (双级重生架构) ——
    #   2D 集 = 纯几何量 (Bt/By/ζ + 几何比/齿宽/Qs/起动转矩): 冲片锁定层(L1)旋钮修不了,
    #           只能回 2D 起始(L2)改 Dsi + 定/转子槽形。
    #   3D 集 = 冲片锁死后可修的诊断量: 效率/温升/槽满率/功率/转矩达标/电密/线规
    #           ★ 并把「气隙磁密 / 长径比λ / 槽满率下限」由 2D 集移入 3D 集 ——
    #             三者是冲片几何锁死后由 绕组+叠长 推算的结果量, L1 可修 (调绕组/叠长),
    #             不再是 2D 几何决策量; 留在 2D 会误触 regenerate_2d 去改不该改的几何。
    #   ★ 2026-09-20 (用户指令) 修订: 「槽满率下限」由 3D 集**移回 2D 集** ——
    #     其修复动作 (shrink_slot_keep_yoke: 减槽高 Hs + 同步减外径 Dso, 保轭宽/By)
    #     是**冲片几何**操作, 只能在 2D 起点改; 留 3D 集会误走 regenerate
    #     (冲片锁死层) 而根本改不动槽高/外径 ⇒ 空转。
    _stage_2d = any(n in ("定子齿磁密", "定子轭磁密", "齿槽比 ζ",
                          "轭厚/槽深比", "Dso/Dsi 比", "齿宽范围", "定子槽数范围",
                          "起动转矩倍数", "槽满率下限") for n in fail_tags)
    _stage_3d = any(n in ("效率下限", "温升上限", "槽满率上限",
                          "额定功率达标比", "额定转矩达标比", "定子电密上限",
                          "线规钳顶越界", "气隙磁密", "长径比 λ",
                          "功率因数下限") for n in fail_tags)
    if _stage_2d and _stage_3d:
        _fail_stage = "mixed"
    elif _stage_2d:
        _fail_stage = "2d"
    elif _stage_3d:
        _fail_stage = "3d"
    else:
        _fail_stage = ""
    return {
        "passed": passed,
        "confidence": confidence,
        "checks": checks,
        "fatal_conflicts": fatal_hits,
        "reasons": reasons,
        "fail_tags": fail_tags,
        "no_recommendation": no_recommendation,
        # v4.02.92: 磁密失败信号 + 失败阶段, 供 build_improvement_instructions(G2) /
        # _after_quality_gate(G7) 使用
        "_mag_failed": _mag_fail,
        "_mag_flags": _mag_flags,
        # 2026-09-20: 磁密实测值 (T), 供动作链按缺口自适应定减匝步长
        "_mag_values": _mag_values,
        # ★ P1-4 (2026-09-22): Bt′/By′ 归一判据 + 带回代结论
        #   (bt_norm/by_norm/bt_norm_band/by_norm_band + need/gain_max/fixable/
        #    geometry_needed/bdelta_short/bdelta_explains_deficit/gap_t/target_bd)
        "_mag_normalized": _mag_normalized,
        # ★ 点② (2026-09-27): 归一化固定带判定的结构化方向 {tooth/yoke: ok|low|high}
        #   — 驱动 build_improvement_instructions 的几何动作分支 (点④⑤)
        "_mag_norm_flags": _mag_norm_flags,
        # 2026-09-20 (用户指令): 槽满率偏低结构性失败 (3D 门判据 / 2d 集路由),
        #   供 build_improvement_instructions 下发 shrink_slot_keep_yoke。
        "_sf_low": bool(_sf_low_fail),
        # 2026-09-20: 槽满率字段缺失 (求解降级 ⇒ 下限判据未判定; 仅观察, 不阻断/不改路由)
        "_sf_unknown": bool(_sf_unknown),
        # 2026-09-20 (Fix3): 电磁负荷 A (线负荷 A/m) + 比载荷 q (W/m²) — 用于报告披露
        "_em_load": _em_load,
        # 2026-09-20 (Fix3): 材料利用率 ηm (kW/t) — 用于报告披露; 缺铁重时 fallback 估算
        "_material_util": _mat_util,
        # v4.02.104 (用户指令 三.2): 门禁选中方案的**轻量快照** —— 报告撰写员据此
        #   复算「电磁负荷 / 材料利用率」时用的必须与门禁评估的是**同一个方案**,
        #   否则报告数字与门禁结论来自不同候选 (口径分裂)。只摘取计算所需字段,
        #   避免把整个候选(含长列表)塞进 SSE/state。
        "_best_snapshot": _slim_best(best),
        "fail_stage": _fail_stage,
        # v4.02.68: 诊断项 (故障信息 / L0↔L1 差异) — 非阻断, 供报告与 AI 评价披露
        "diagnostics": _diag,
        "summary": "; ".join(reasons) if reasons else "质量门禁全部通过",
    }


def decide_path(result: dict, redo_count: int, cfg: Optional[dict] = None,
                prev_reasons: Optional[list] = None) -> str:
    """根据门禁结果与已重生次数, 决定路由: 'pass' | 'regenerate' | 'manual'

    pass       — 通过, 进入决策门人工确认
    regenerate — 未通过且未达上限, 有限次方案重生 (回到 generate_designs)
    manual     — 未通过且已达上限, 或门禁"卡死"(连续两轮失败原因完全一致) → 转人工裁决

    v4.02.66 新增: 门禁"卡死"检测 — 若本次失败原因 (result['reasons']) 与上一轮
        (prev_reasons) 完全一致, 说明重生回路无法改善结果 (典型: 槽满率为固定的派生值,
        重生后参数矩阵/可行率/最优指标完全一致), 直接转 manual 避免无谓空转 (约 28min/轮)。
    """
    if result.get("passed"):
        return "pass"
    max_redo = int(cfg.get("max_redo", 2) if isinstance(cfg, dict) else 2)
    # 门禁卡死检测: 连续两轮失败原因完全一致 → 重生无改善, 提前转人工裁决
    if isinstance(prev_reasons, list) and prev_reasons and result.get("reasons") == prev_reasons:
        return "manual"
    if redo_count < max_redo:
        return "regenerate"
    return "manual"


# ═══════════════════════════════════════════════════════════════════════════
# P1/P2 (v4.02.67): 结构化改进指令 / 冲突折中 / 措施效果评价
# ---------------------------------------------------------------------------
# 目标(用户诉求): 门禁失败 → 生成"结构化改进指令"(非纯文本) → 冲突检测产出"折中指令"
#   → 注入下一轮生成 → 再次评价时输出 Δ指标 效果评价表 (措施是否有效/折中是否达成)。
# 说明: 本段为纯函数 (无副作用, 不依赖 motor_tools), 由 orchestrator 调用与落地。
# ═══════════════════════════════════════════════════════════════════════════

def extract_round_metrics(results: list) -> dict:
    """从 results[0](最佳方案) 抽取用于跨轮对比的关键指标。"""
    if not results or not isinstance(results[0], dict):
        return {}
    b = results[0]
    # ★ H29 (2026-09-26 RUN53 取证): 槽满率读取**统一走 _normalize_slot_fill**。
    #   旧实现在此处独立做 `sf > 1.5 → /100`, 与门禁判据的 `_normalize_slot_fill`
    #   (v>1.0 ⇒ 崩溃, **不 /100**) 口径相反 ⇒ 同一个 slot_fill_rate 被两处各解一个
    #   方向: RUN53 第 6 轮真值 4.16 在 QG 侧读作「416% 崩溃超限」,
    #   在 [措施效果评价] 侧 /100 读作「0.0416 = 4.16% 偏低」—— 相差 100 倍,
    #   导致该轮失败归因自相矛盾、"偏低"处方方向被误导。
    #   ⇒ 此处删除私有 /100 分支, 改为调用**唯一真源**, 并把崩溃标记一并输出,
    #     使 [措施效果评价] 与门禁判据永远同源。
    _sf_raw = b.get("slot_fill_rate", b.get("slot_fill", None))
    sf, sf_crash = None, False
    if _sf_raw is not None:
        sf, sf_crash = _normalize_slot_fill(_sf_raw)
    def _f(key):
        try:
            v = b.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return {
        "slot_fill": sf,
        "slot_fill_crashed": sf_crash,
        "max_temp": _f("max_temp"),
        "stator_od": _f("stator_od"),
        "efficiency": _f("efficiency"),
        "torque": _f("torque"),
        "current_density": _f("current_density"),
        "stator_tooth_flux_density": _f("stator_tooth_flux_density"),
    }


def _current_slot_fill(qg_result: dict) -> float:
    """读取**本轮**实算槽满率 (从门禁 checks 中扫「槽满率」项)。取不到返回 0.0。"""
    for _c in (qg_result or {}).get("checks", []) or []:
        if not isinstance(_c, dict):
            continue
        if "槽满率" in str(_c.get("name") or ""):
            try:
                _v = float(_c.get("value") or 0.0)
            except (TypeError, ValueError):
                continue
            if _v > 0:
                return _v
    return 0.0


def _bg_band_from_checks(qg_result: dict) -> tuple:
    """气隙磁密 Bδ 判定带 (真源 = 门禁 checks 的 threshold; 缺失回退代码默认)。"""
    for _c in (qg_result or {}).get("checks", []) or []:
        if not isinstance(_c, dict):
            continue
        if _c.get("name") == "气隙磁密":
            try:
                _t = _c.get("threshold")
                if isinstance(_t, (list, tuple)) and len(_t) == 2:
                    return (float(_t[0]), float(_t[1]))
            except (TypeError, ValueError):
                break
    return (0.55, 0.85)


def _current_air_gap_flux(qg_result: dict) -> float:
    """本轮实算气隙磁密 Bδ (T)。取不到返回 0.0。

    优先 `_mag_values["airgap"]` (门禁直接落值), 回退扫 checks 的「气隙磁密」项。
    """
    _mv = (qg_result or {}).get("_mag_values") or {}
    try:
        _v = float(_mv.get("airgap") or 0.0)
        if _v > 0:
            return _v
    except (TypeError, ValueError):
        pass
    for _c in (qg_result or {}).get("checks", []) or []:
        if isinstance(_c, dict) and _c.get("name") == "气隙磁密":
            try:
                return float(_c.get("value") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def bg_abs_step(gap_t: float, up: bool = True,
                bd_now: float = None, band: tuple = None) -> float:
    """★ P1-1: 把 Bδ 缺口 (T) 转成**绝对**步长, 钳 [BG_ABS_STEP_MIN_T, BG_ABS_STEP_MAX_T]。

    `gap_t` = 目标 Bδ − 当前 Bδ (取绝对值使用)。缺口小 ⇒ 步长小 (不跨带);
    缺口大 ⇒ 步长封顶 0.02T (≈ 可行域宽度, 仍是单调收敛的一步)。

    ★ H30 (2026-09-26): 追加**自适应放开** (bd_now / band 均给定时生效)
    ------------------------------------------------------------------
      固定 0.02T 上限对「台阶式大缺口」会把步长削到 ~8% (RUN53 实测 +0.282T 缺口
      ⇒ 只下发 +0.02T, 传导后实到 +0.0057T) ⇒ Bt 收敛所需轮数 (14.1) 远超预算 (8)。
      新判据: **健康带本身就是护栏** —— 只要目标 Bδ (= bd_now ± gap) 还落在带内,
      就没理由被 0.02T 限死。此时步长 = min(缺口, 距带沿余量 × 0.90);
      若带沿余量比 MAX_T 还小 ⇒ 退化为余量 (等价于旧行为, 保证零回归)。

    参数
      gap_t  : Bδ 缺口绝对值 (T)
      up     : True=抬高 Bδ / False=压低 Bδ
      bd_now : 当前实算气隙磁密 (T); 缺失 ⇒ 不做自适应
      band   : Bδ 判定带 (lo, hi); 缺失 ⇒ 不做自适应

    返回: 带符号绝对步长 (T)。
    """
    try:
        _g = abs(float(gap_t or 0.0))
    except (TypeError, ValueError):
        return 0.0
    if _g <= 0:
        return 0.0
    _step = max(BG_ABS_STEP_MIN_T, min(BG_ABS_STEP_MAX_T, _g))
    # ── H30 自适应放开 ──
    if BG_ADAPTIVE_STEP and _g > _step:
        try:
            _bd = float(bd_now or 0.0)
            _lo, _hi = float(band[0]), float(band[1])
        except (TypeError, ValueError, IndexError):
            _bd, _lo, _hi = 0.0, 0.0, 0.0
        if _bd > 0 and _hi > _lo:
            _head = (_hi - _bd) if up else (_bd - _lo)   # 距目标带沿的余量
            if _head > 0:
                _cap = _head * BG_ADAPTIVE_HEADROOM_SAFE
                if _cap > _step:
                    # 缺口已在带内可达 ⇒ 放开到缺口 (但绝不超过带上余量的 90%)
                    _step = max(_step, min(_g, _cap))
                else:
                    # 余量比步长还小 ⇒ **收紧**到余量: 本轮宁可只推进到带沿,
                    # 也不跨带 (跨带会换来 J/η/温升的连锁代价)
                    _step = min(_step, _head)
            _step = max(_step, BG_ABS_STEP_FLOOR_T)      # 绝不落 0 (见 Fix1)
    return round(_step if up else -_step, 4)


def bdelta_sufficiency(bt, by, bd, bt_band, by_band, bd_band) -> dict:
    """★ P1-4 带回代: 齿/轭磁密偏低能否被「Bδ 整体偏低」**定量解释**?

    物理链: Bt ∝ Bδ/bt, By ∝ Bδ/hj ⇒ 若 Bδ 偏低 k 倍, Bt/By 同比例偏低 k 倍。
      · 需要的增益 need = max(lo_bt/bt, lo_by/by)  (只计入**低于下沿**的项)
      · 抬 Bδ 能提供的最大增益 gain_max = hi_bd/bd
      ⇒ Bδ **确实出带偏低** (`bd < lo_bd`) 且 need ≤ gain_max
         ⇒ 仅靠抬 Bδ 即可救回 ⇒ `fixable=True` / `bdelta_explains_deficit=True`
         ⇒ **抑制几何杠杆** (narrow_tooth)。
      否则 (Bδ 在带内, 或 Bδ 拉满也救不回) ⇒ 冲片比例确实有问题 ⇒ 允许几何杠杆。

    返回 dict (含 Bt′/By′ 归一辅助量与判定带):
      bt_norm / by_norm / bt_norm_band / by_norm_band / bt_norm_ok / by_norm_ok
      need / gain_max / fixable / geometry_needed / bdelta_short / gap_t / target_bd
    """
    def _fv(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _band(b, dflt):
        try:
            return (float(b[0]), float(b[1]))
        except (TypeError, ValueError, IndexError):
            return dflt

    _bt, _by, _bd = _fv(bt), _fv(by), _fv(bd)
    _btb = _band(bt_band, (1.4, 1.7))
    _byb = _band(by_band, (0.8, 1.2))
    _bdb = _band(bd_band, (0.55, 0.85))
    out = {"bt": round(_bt, 4), "by": round(_by, 4), "bd": round(_bd, 4),
           "bt_band": [round(_btb[0], 4), round(_btb[1], 4)],
           "by_band": [round(_byb[0], 4), round(_byb[1], 4)],
           "bd_band": [round(_bdb[0], 4), round(_bdb[1], 4)],
           "beta_ref_t": BETA_REF_T}
    if _bd <= 0:
        out.update({"need": 1.0, "gain_max": 1.0, "fixable": True,
                    "geometry_needed": False, "bdelta_short": False,
                    "gap_t": 0.0, "target_bd": round(_bd, 4),
                    "note": "Bδ 缺失 ⇒ 不参与归一判据"})
        return out
    _k = BETA_REF_T / _bd
    if _bt > 0:
        out["bt_norm"] = round(_bt * _k, 4)
        # ★ 点② (2026-09-27 用户指令): 归一带改为【固定带】(不再随 Bδ 缩放)。
        #   旧式带同步缩放 (band·k) ⇒ bt_norm_ok 与原始带判代数恒等 (空操作,
        #   RUN57 实证 30/30 恒等)。固定带后 bt_norm = Bt·Bδ₀/Bδ (投影到参考
        #   气隙磁密 Bδ₀=0.70T) 直接比对设计目标带 ⇒ 真正分离:
        #     进带   = 纯励磁偏低 (Bδ 杠杆 reduce_turns 管, 点⑥)
        #     出带低 = 齿/轭几何偏大 (点④ narrow_tooth / 点⑤ shrink_yoke 管)
        out["bt_norm_band"] = [round(_btb[0], 4), round(_btb[1], 4)]
        out["bt_norm_ok"] = bool(out["bt_norm_band"][0] <= out["bt_norm"]
                                 <= out["bt_norm_band"][1])
    if _by > 0:
        out["by_norm"] = round(_by * _k, 4)
        # ★ 点②: 同上, 轭磁密归一带亦改固定带。
        out["by_norm_band"] = [round(_byb[0], 4), round(_byb[1], 4)]
        out["by_norm_ok"] = bool(out["by_norm_band"][0] <= out["by_norm"]
                                 <= out["by_norm_band"][1])
    _need = 1.0
    if _bt > 0 and _bt < _btb[0]:
        _need = max(_need, _btb[0] / _bt)
    if _by > 0 and _by < _byb[0]:
        _need = max(_need, _byb[0] / _by)
    _gain_max = _bdb[1] / _bd
    _fixable = bool(_need <= _gain_max + 1e-9)
    # 目标 Bδ: 至少抬到带下沿; 够则按缺口抬 (不越过带上限)
    _target = min(max(_bd * _need, _bdb[0]), _bdb[1]) if _need > 1.0 else _bd
    out.update({
        "need": round(_need, 4), "gain_max": round(_gain_max, 4),
        "fixable": _fixable,
        "geometry_needed": bool((_need > 1.0) and (not _fixable)),
        "bdelta_short": bool((_bd < _bdb[0]) and (_need > 1.0)),
        # ★ P1-4 修正 (2026-09-22 收尾): 「Bδ 可解释缺额」必须**同时**要求 Bδ 真的
        #   出带偏低 (`_bd < 带下沿`)。旧式只判 `need<=gain_max`, 于是 Bδ 明明在
        #   带内 (如 Bδ=0.70 ∈ [0.55,0.85]) 也会返回 True —— 因为把 Bδ 抬到带上限
        #   0.85 的增益 1.214 恰好覆盖齿磁密缺口 1.167。后果: 几何杠杆 narrow_tooth
        #   被**永久抑制**, 而 Bδ 又无需抬升 ⇒ 该轮**无任何有效杠杆** (空转)。
        #   物理上只有「Bδ 低于健康带」才谈得上「Bδ 偏低是主因」。
        "bdelta_explains_deficit": bool((_need > 1.0) and _fixable
                                        and (_bd < _bdb[0])),
        "gap_t": round(max(0.0, _target - _bd), 4),
        "target_bd": round(_target, 4),
    })
    return out


def _achieve_gap(qg_result: dict) -> float:
    """★ P1-2: 达标缺口 = max(0, 1 − min(功率达标比, 转矩达标比))。

    真源 = 门禁 checks 的「额定功率达标比 / 额定转矩达标比」项 (结构化字段, 不解析文案)。
    取不到 (字段缺失) ⇒ 返回 0.0 (由调用侧退化为 WIRE_GAUGE_STEP_MIN 下限)。
    """
    _worst = None
    for _c in (qg_result or {}).get("checks", []) or []:
        if not isinstance(_c, dict):
            continue
        if str(_c.get("name") or "") in ("额定功率达标比", "额定转矩达标比"):
            try:
                _v = float(_c.get("value"))
            except (TypeError, ValueError):
                continue
            _worst = _v if _worst is None else min(_worst, _v)
    if _worst is None:
        return 0.0
    return round(max(0.0, 1.0 - _worst), 4)


def wire_gauge_step(sf_cur=0.0, sf_tgt=0.0, gap=0.0) -> float:
    """★ P1-2 (2026-09-22 用户指令): increase_wire_gauge (降电密) 步长**自适应**。

    由缺口反解, 并钳制在 [WIRE_GAUGE_STEP_MIN, WIRE_GAUGE_STEP_MAX] = [5%, 25%]:
      · SF 缺口:  SF ∝ 1/J ⇒ J_new = J·(SF_cur/SF_tgt) ⇒ 相对降幅 = 1 − SF_cur/SF_tgt
      · 其它缺口: 达标比缺口 (功率/转矩) 同样落到「需增加铜截面」这一杠杆上
    取二者较大者 (谁更紧谁主导), 再钳位。缺省 0 ⇒ 退化为下限 5% (仍优于旧的固定 3%)。
    """
    _f = 0.0
    try:
        _sc, _st = float(sf_cur or 0.0), float(sf_tgt or 0.0)
        if _sc > 0 and _st > 0:
            _f = max(_f, 1.0 - _sc / _st)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    try:
        _f = max(_f, abs(float(gap or 0.0)))
    except (TypeError, ValueError):
        pass
    return round(max(WIRE_GAUGE_STEP_MIN, min(WIRE_GAUGE_STEP_MAX, _f)), 4)


def _slot_fill_band() -> tuple:
    """槽满率目标带 (运行时配置优先, 代码默认兜底)。"""
    try:
        _b = _get_slot_fill_band()
        if isinstance(_b, (list, tuple)) and len(_b) == 2:
            return (float(_b[0]), float(_b[1]))
    except Exception:
        pass
    try:
        return (float(_SF_TARGET_BAND[0]), float(_SF_TARGET_BAND[1]))
    except Exception:
        return (0.65, 0.75)


# ── SF 方向分类 (P8b 统一仲裁用) ────────────────────────────────────────────
#   +1 = 该动作使槽满率 SF **上升**;  -1 = 使其 **下降**
#   ⚠ `increase_wire_gauge` (加粗线规) 使槽内铜面积↑ ⇒ SF **上升** ——
#     旧 P8 反推代码 (见 build_improvement_instructions 的 _prev_dir 扫描)
#     把它归入「压 SF」侧, 方向判定因此反了, 折半系数加在错误的一侧。
_SF_RAISE_ACTIONS = {
    "shrink_slot_keep_yoke",   # 收槽高(同步收外径) ⇒ 槽面积↓ ⇒ SF↑
    "set_slot_depth",          # ★ P0-3: 由缺口反解槽深 (槽深独立 ⇒ SF 纯杠杆)
    "increase_wire_gauge",     # 加粗导线 ⇒ 槽内铜面积↑ ⇒ SF↑
    "widen_tooth",             # 加齿宽 ⇒ 槽变窄 ⇒ 槽面积↓ ⇒ SF↑
}
_SF_LOWER_ACTIONS = {
    "increase_slot_area",      # 放大槽面积 ⇒ SF↓ (纯 SF 旋钮)
    "narrow_tooth",            # 减齿宽 ⇒ 槽变宽 ⇒ SF↓
    "reduce_turns",            # 减匝 ⇒ 槽内导体↓ ⇒ SF↓
}
# 纯 SF 旋钮: 方向相反时必须二选一 (其余动作服务温升/电密/磁密, 只降幅不删除)
# ★ P0-3 (2026-09-22): set_slot_depth 与 shrink_slot_keep_yoke 同属「抬 SF 的几何纯旋钮」,
#   二者择一即可 (前者精确控制槽深, 后者按固定比例收缩), 故一并纳入纯旋钮集合。
_SF_PURE_PAIR = ("shrink_slot_keep_yoke", "set_slot_depth", "increase_slot_area")


# ══════════════════════════════════════════════════════════════════════════
# ★ P1-1 (2026-09-22 用户指令): 气隙磁密 Bδ 动作改**绝对步长** (单位 T)
# --------------------------------------------------------------------------
#   现状缺陷: raise_air_gap_flux 用相对步长, `d["air_gap_flux_density"] *=
#     (1 + |delta|)`, 而门禁给的 delta ∈ [0.10, 0.30] ⇒ 在 Bδ≈0.64T 时等效
#     **+0.064 ~ +0.19T**。但 Bδ 的健康带 [0.55,0.85] 宽仅 0.30T, 而由匝数
#     整数量化 (ΔBδ/Bδ ≈ 1/Z ≈ 12%) 决定的**可行域**只有 ~0.019T
#     ⇒ 旧步长一步即跨出整条带 (RUN40 实测 0.636 → 0.842T, 单轮 +0.206T)。
#   新规则 (报告 V2 §9.3 P1-1): step = clamp(Bδ 缺口, 0.01, 0.02) T, 与缺口同量级,
#     缺口小则步长小 ⇒ 单调收敛而不跨带。
BG_ABS_STEP_MIN_T = 0.01        # T, 最小绝对步长 (防步长退化为 0 而空转)
BG_ABS_STEP_MAX_T = 0.02        # T, 最大绝对步长 (≈ 可行域宽度, 防一步跨带)

# ══════════════════════════════════════════════════════════════════════════
# ★ H30 (2026-09-26 RUN53 取证): Bδ 步长**自适应** (缺口大 + 带有余量 ⇒ 放开钳位)
# --------------------------------------------------------------------------
#   缺陷: 固定钳位 [0.01,0.02]T 的初衷是「不跨带」(Bδ 带 [0.55,0.85] 宽仅 0.30T,
#     而匝数整数量化的可行域 ~0.019T)。但它对**大缺口**同样生效 ⇒ 步长被削到 8%:
#     RUN53 实测 need=1.60 / Bδ=0.4705 ⇒ 缺口 _gap_t=+0.282T, 而实际下发仅
#     +0.02T (命中上限); 叠加传导损耗后实到 Bδ 只 +0.0057T (效率 <8%)。
#     结果: Bt 需 14.1 轮才能从 0.66T 推到 1.4T, 而硬预算只有 8 轮 ⇒ 必然耗尽。
#   新规则: 当「按缺口反推的目标 Bδ」仍在**健康带内**时, 带宽本身就是天然护栏 ——
#     此时不必再被 0.02T 限死, 步长放开到 min(缺口, 距带上/下沿余量 × SAFE)。
#     · 缺口 ≤ MAX_T            → 行为完全不变 (零回归)
#     · 缺口 > MAX_T 但已至带沿 → 退化为余量 (等价于旧行为的上界保护)
#     · 缺口 > MAX_T 且有余量   → 放开到缺口 (H30 修复点)
#   SAFE < 1: 留 ~10% 余量, 避免电磁负荷/温升联动把 Bδ 顶出带。
BG_ADAPTIVE_STEP = True             # 总开关 (False ⇒ 完全回退旧钳位行为)
BG_ADAPTIVE_HEADROOM_SAFE = 0.90    # 只用带上余量的 90%
BG_ABS_STEP_FLOOR_T = 0.001         # T, 硬地板: 余量极小时仍保推进 (绝不落 0)

# ══════════════════════════════════════════════════════════════════════════
# ★ P1-2 (2026-09-22 用户指令): increase_wire_gauge (降电密) 步长**自适应**
# --------------------------------------------------------------------------
#   现状缺陷: 同一动作的步长由指令硬编码 0.03 (3%), 而 RUN40 实测需要 12~20%
#     ⇒ **杠杆对、力度不足** (SF 缺口 0.28→0.65 需 J 降 ~57%, 3% 步长要 19 轮)。
#   新规则: delta 由 SF/J 缺口反解 (delta = 1 − SF_cur/SF_tgt, 因 SF ∝ 1/J),
#     并钳制在 [5%, 25%] —— 下界保证有推进, 上界防单轮把 J 压到 AJ 带以外。
WIRE_GAUGE_STEP_MIN = 0.05      # 5%
WIRE_GAUGE_STEP_MAX = 0.25      # 25%

# ══════════════════════════════════════════════════════════════════════════
# ★ 槽深反解 (set_slot_depth) 的「杠杆停滞」守卫 (2026-10-10 用户授权, RUN67 取证)
# --------------------------------------------------------------------------
#   反解式 hs_new = hs·(SF_cur/SF_tgt) 在「槽宽不随槽深变」前提下**数学自洽**
#     (RUN40 实证精度 ~2%), 但它隐含一个前提: 本轮杠杆能真正作用在**被评估的候选**上。
#   实测失效场景 (RUN67 / PROJ_20261009230623233552):
#     · 门禁评估的 TOP1 属 a/b1/b2 轨 —— stator_od / yoke_thickness 是规则级固定键,
#       set_slot_depth 被方向②规则优先拦截 (orchestrator:5911), 杠杆只落到 b3 轨;
#     · 下一轮门禁读到的 SF 仍是 a 轨那个**永不改善**的值 (实测 0.394→0.361→0.360→0.360);
#     · 于是每轮都按同一缺口**等额**反解 ⇒ 槽深被逐轮对折:
#         27.799 → 15.648 → 8.8 → 5.0 mm, 槽面积塌缩 ⇒ SF 预检极值 0.28~95.70 (病态)。
#   守卫: 上轮已下发抬 SF 杠杆、且 SF 较上轮**无实质改善** ⇒ 判定杠杆停滞
#     (对象错位 / 被下一轮重新生成抵消) ⇒ 本轮步长按 SF_SD_STALL_DAMP 衰减,
#     不再等额加码。正常场景 (SF 真实改善) 不触发 ⇒ **零回归**。
SF_SD_STALL_EPS = float(os.environ.get("SF_SD_STALL_EPS", "0.01"))    # SF 改善不足 1pp ⇒ 停滞
SF_SD_STALL_DAMP = float(os.environ.get("SF_SD_STALL_DAMP", "0.35"))  # 停滞时步长衰减系数

# ══════════════════════════════════════════════════════════════════════════
# ★ P1-4 (2026-10-11 用户授权, 单变量对照验证): 锁定轨「杠杆耗尽」早期识别
# --------------------------------------------------------------------------
#   锁定轨(冲片锁定层)唯一下发 increase_wire_gauge, 但评估对象(TOP1)几何被锁,
#   真实抬 SF 几何杠杆(set_slot_depth)落空 ⇒ SF 连续 N 轮空转 (H36 杠杆错位复现)。
#   N 轮空转 ⇒ 判定杠杆耗尽 ⇒ (a) 智能反算线径 d→d·√(SFtgt/SFcur) 一步到位(加大幅度);
#     (b) 打 _lever_exhausted 标记 ⇒ orchestrator「转人工/换轨」(配额让 b3)。
#   ⚠ QG_LEVER_EXHAUST_N=0 (默认) ⇒ 完全不触发, 零回归。
def _qg_exp_int(name, default=0):
    """实验开关读取: 优先环境变量; 缺失时回退 .qg_experiment.json (文件缺=默认关, 零回归)。
    用途: MotorAI Manager 守护拉起的 Flask 不继承手动 export 的环境变量, 故用文件注入以保活。"""
    import json
    v = os.environ.get(name)
    if v in (None, ""):
        try:
            _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".qg_experiment.json")
            if os.path.exists(_p):
                with open(_p, encoding="utf-8") as _f:
                    v = json.load(_f).get(name)
        except Exception:
            v = None
    if v in (None, ""):
        return default
    try:
        return int(v)
    except Exception:
        return default

LEVER_EXHAUST_N = _qg_exp_int("QG_LEVER_EXHAUST_N", 0)
LEVER_EXHAUST_BOOST = float(os.environ.get("QG_LEVER_EXHAUST_BOOST", "0.40") or 0.40)  # 加大幅度上限

# ══════════════════════════════════════════════════════════════════════════
# ★ P1-4 (2026-09-22 用户指令): Bt′/By′ 归一判据 + **带回代**
# --------------------------------------------------------------------------
#   归一化: Bt′ = Bt·Bδ₀/Bδ, By′ = By·Bδ₀/Bδ (Bδ₀ = BETA_REF_T = 0.7T 设计基准),
#     判定带按设计 Bδ 同步换算 (报告 V2 §9.3 P1-4: Bδ=0.6864 ⇒ Bt′ 带 [1.428,1.734])。
#     ⚠ 归一判定与未归一是**代数恒等**的 (值与带同乘 k=Bδ₀/Bδ) ⇒ 归一的价值在于
#       **跨候选可比性/信噪比** (Bt 变异系数 5.37% → 1.45%, 压缩 3.71×), 不在改阈值。
#   **带回代** (本项真正的行为改动): 若齿/轭磁密偏低能被「Bδ 整体偏低」**定量解释**
#     (即把 Bδ 抬到带上限所能提供的增益 ≥ 缺口), 则主因是 Bδ 而非冲片比例 ⇒
#     **必须抑制几何杠杆 (narrow_tooth/widen_tooth/...), 只留 Bδ 杠杆**
#     (raise_air_gap_flux 绝对步长 + reduce_turns)。否则会把 Bδ 的问题误诊成
#     「齿太宽」, 用药 (减齿宽) 既修不好 Bδ、又白牺牲槽面积 (报告 V2: 「不再因 Bδ
#     波动误判『减齿宽』」)。
BETA_REF_T = 0.70               # T, 归一参考气隙磁密 (设计基准 Bδ₀)



def arbitrate_sf_conflicts(instrs: List[dict], sf_cur: float,
                           band: Optional[tuple] = None,
                           mag_flags: Optional[dict] = None) -> dict:
    """P8b (2026-09-21 用户指令 三.1): 槽满率**统一仲裁与冲突解决**。

    背景 (RUN30/31/34 实测)
    -----------------------
    P8 只做了「过冲/反转 ⇒ 步长折半」, 但**没有解决冲突本身**:
    同一轮里 2D 路径 (shrink_slot_keep_yoke ⇒ SF↑) 与 3D 路径
    (increase_slot_area / reduce_turns ⇒ SF↓) **可以同时下发**,
    两者在真实几何上互相抵消 ⇒ 每轮净效果被稀释, SF 在带外来回穿越:
        0.573 → 0.618 → 0.619 → 0.434   (4 轮无一入带)
    折半只是让震荡变慢, 不改变「方向相反」这一根本矛盾。

    本函数给出**单一仲裁**:
      1. 由 (本轮 SF, 目标带) 定出主导需求 demand ∈ {+1 抬, -1 压, 0 已在带内};
      2. **纯 SF 旋钮对冲** (shrink_slot_keep_yoke ↔ increase_slot_area):
         - demand=+1 ⇒ 删除 increase_slot_area;
         - demand=-1 ⇒ 删除 shrink_slot_keep_yoke;
         - demand= 0 ⇒ 两者皆删 (已在带内, 不必再动 SF, 避免再次推出带外);
      3. **间接反向动作** (narrow_tooth / widen_tooth / reduce_turns /
         increase_wire_gauge): 它们服务温升/电密/磁密等其它目标, 不删除, 但若方向
         与 demand 相反 ⇒ 步长 ×0.5 (减少它对 SF 的反向拖拽);
      4. **兜底**: 若仲裁后指令被清空, 恢复被删项并整体折半 —— 宁可慢, 不可空转。

    返回仲裁摘要 (写入 qg_result["_sf_arbitration"], 供 SSE/报告披露)。
    """
    summary = {
        "sf_current": round(sf_cur, 4) if sf_cur else None,
        "band": list(band) if band else None,
        "demand": 0, "dropped": [], "damped": [], "kept": [], "protected": [],
        "verdict": "no_conflict",
    }
    if not instrs:
        return summary

    lo, hi = (band or _slot_fill_band())
    if sf_cur and sf_cur > 0:
        if sf_cur < lo:
            summary["demand"] = 1
        elif sf_cur > hi:
            summary["demand"] = -1
    dem = summary["demand"]

    has_raise = [i for i in instrs if i.get("action") in _SF_RAISE_ACTIONS]
    has_lower = [i for i in instrs if i.get("action") in _SF_LOWER_ACTIONS]
    if not (has_raise and has_lower):
        # 无对冲: 仅规范化 conflict 标注, 不做裁剪
        summary["verdict"] = "no_conflict"
        summary["kept"] = sorted({i.get("action") for i in instrs})
        return summary

    drop_actions = []
    if dem == 1:
        drop_actions = ["increase_slot_area"]
        summary["verdict"] = "resolved:SF偏低⇒保留收槽高, 撤销放大槽面积"
    elif dem == -1:
        drop_actions = ["shrink_slot_keep_yoke", "set_slot_depth"]
        summary["verdict"] = "resolved:SF偏高⇒保留放大槽面积, 撤销收槽高/收槽深"
    else:
        drop_actions = list(_SF_PURE_PAIR)
        summary["verdict"] = "resolved:SF已在带内⇒纯SF旋钮全部撤销, 只留他项动作"

    _dropped_backup: List[dict] = []
    _kept: List[dict] = []
    for it in instrs:
        _a = it.get("action")
        if _a in drop_actions:
            _dropped_backup.append(dict(it))
            summary["dropped"].append(_a)
            continue
        # 间接反向动作 ⇒ 降幅 (保留)
        # ★ D (2026-09-22 用户指令): 当同一轮存在「齿磁密偏低 / 气隙磁密偏低」时,
        #   reduce_turns / narrow_tooth 本就是为抬 Bδ/Bt 服务的**磁密杠杆**,
        #   不应因"与 SF 需求反向"被折半 —— 否则会牺牲 Bt 修复速度 (RUN41 实证:
        #   齿磁密低+槽满率低同现, 降电密被保留而减匝被折半 ⇒ 齿磁密久修不上)。
        _mag_flags = (mag_flags or {})
        _mag_serv = (_mag_flags.get("tooth") == "low") or \
                    (_mag_flags.get("airgap") == "low")
        if dem != 0:
            _dir = 1 if _a in _SF_RAISE_ACTIONS else (-1 if _a in _SF_LOWER_ACTIONS else 0)
            if _dir and _dir != dem:
                if _a in ("reduce_turns", "narrow_tooth") and _mag_serv:
                    # 磁密杠杆: 保护不被折半 (标记 conflict 但不缩步长)
                    it["conflict"] = "sf_arbitration:protected(磁密杠杆服务Bt/Bδ)"
                    it["reason"] = str(it.get("reason", "")) + \
                        " [P8b 统一仲裁: 该动作服务磁密目标, 不被 SF 需求折半]"
                    summary["protected"].append(_a)
                else:
                    try:
                        _old = float(it.get("delta") or 0)
                        it["delta"] = round(_old * 0.5, 6)
                        it["conflict"] = "sf_arbitration:damped(反向于SF需求)"
                        it["reason"] = str(it.get("reason", "")) + \
                            " [P8b 统一仲裁: 该动作方向与 SF 需求相反, 步长 ×0.5]"
                        summary["damped"].append(_a)
                    except (TypeError, ValueError):
                        pass
        _kept.append(it)

    if not _kept and _dropped_backup:
        # 兜底: 全被删 ⇒ 恢复并整体折半 (避免"无指令可用"造成空转)
        for it in _dropped_backup:
            try:
                it["delta"] = round(float(it.get("delta") or 0) * 0.5, 6)
            except (TypeError, ValueError):
                pass
            it["conflict"] = "sf_arbitration:restored(全删兜底)"
            _kept.append(it)
        summary["dropped"] = []
        summary["verdict"] += " | 兜底: 恢复原指令并折半"
    # 原地替换 (保持 list 对象引用不变, 调用方持有的 instrs 同步更新)
    #   ⚠ 两个分支都必须执行 —— 初版只在 else 里替换, 兜底分支下调用方的
    #     instrs 仍是原对象 (折半与标注均未生效)。
    instrs[:] = _kept

    summary["kept"] = sorted({i.get("action") for i in instrs})
    return summary


def _preflight_instruction_conflicts(instrs: list, qg_result: dict) -> list:
    """★ J4 (2026-09-24 用户指令): 同轮改进指令**方向自洽预检** (只读结构化 checks)。

    背景 (RUN49 实证的两类打架):
      R1 齿宽下限冲突: 门禁「齿宽范围」失败且 bt_mm < bt_min 时, 本轮**不得**再发
         narrow_tooth (往更低于下限方向推) ⇒ 移除该指令。
         (RUN49 轮1: bt=13.152 < bt_min=13.803 已失败, 轮2 却又发 narrow_tooth −0.167。)
      R2 功率/转矩达标不足: 门禁「额定功率/转矩达标比」失败时, reduce_turns (减匝
         =降电磁能力) 与「功率要涨」目标反向 ⇒ 幅度折半并打 conflict 标记 (软降级,
         保留 Bδ 杠杆 — 不整条移除, 与 P8 震荡抑制 0.5× 同哲学)。

    只读 checks 的结构化字段 (name/passed/value), 不解析 reason 文案 (P8b 铁律)。
    原地修改 instrs; 返回告警列表 (空 = 无冲突)。
    """
    _alerts: List[dict] = []
    _checks = (qg_result or {}).get("checks", []) or []
    _bt_below_min = False
    _pw_short = False
    for _c in _checks:
        _n = str(_c.get("name") or "")
        if _n == "齿宽范围" and _c.get("passed") is False:
            _v = _c.get("value") or {}
            try:
                if float(_v.get("bt_mm", 0) or 0) < float(_v.get("bt_min", 0) or 1e9):
                    _bt_below_min = True
            except (TypeError, ValueError):
                pass
        # ★改造③ (2026-09-28): 转矩项已降级为警告项 (仅崩溃级 passed=False), 不再参与
        #   reduce_turns 折半判定, 避免口径漂移后转矩项重新影响动作步长。
        elif _n == "额定功率达标比" and _c.get("passed") is False:
            _pw_short = True
    _kept: List[dict] = []
    for _it in instrs or []:
        _a = str(_it.get("action") or "")
        if _a == "narrow_tooth" and _bt_below_min:
            _alerts.append({"dropped": "narrow_tooth",
                            "rule": "J4-R1",
                            "reason": "齿宽已低于下限(bt_mm<bt_min), narrow_tooth 方向冲突 ⇒ 移除"})
            continue
        if _a == "reduce_turns" and _pw_short:
            try:
                _d = float(_it.get("delta") or 0)
                _it = dict(_it)
                _it["delta"] = round(_d * 0.5, 4)
                _it["conflict"] = "power_shortfall"
                _it["reason"] = (str(_it.get("reason") or "")
                                 + " [J4-R2: 功率/转矩达标不足, 减匝幅度折半]")
            except (TypeError, ValueError):
                pass
            _alerts.append({"softened": "reduce_turns",
                            "rule": "J4-R2",
                            "reason": "功率/转矩达标比不足, reduce_turns 与加功率反向 ⇒ 幅度折半"})
        _kept.append(_it)
    if _alerts:
        instrs[:] = _kept
    return _alerts


def _resolve_em_mode(state: dict = None) -> str:
    """★ 模态感知② (2026-09-28 用户授权): 解析励磁模态。

    真源 = 设计字典 _em_mode ('voltage' | 'constant_j', builder 端
    motor_tools._precompute_u_rated 落标); state.results[0]/best_solution 缺失或
    未标记时回退环境开关 EM_LOAD_CONSTANT_J, 再回退 'voltage' (历史行为零回归)。
    """
    try:
        _rs = ((state or {}).get("results") or [])
        _b0 = (_rs[0] if _rs else ((state or {}).get("best_solution") or {}))
        _m = str(_b0.get("_em_mode") or "").strip().lower()
        if _m in ("voltage", "constant_j"):
            return _m
    except Exception:
        pass
    if str(os.environ.get("EM_LOAD_CONSTANT_J", "0")).strip().lower() in (
            "1", "true", "on"):
        return "constant_j"
    return "voltage"


# ═══ 方案② (2026-10-10 用户授权): 轨→几何锁定判定 ═══
#   与 verify_engine.FIXED_KEYS_BY_PATH 口径对齐。set_slot_depth/shrink_slot_keep_yoke
#   主写键={stator_od,yoke_thickness}、widen_tooth={tooth_width} —— 与该轨固定键相交的
#   轨, 几何抬 SF 处方必被执行器方向② (orchestrator._ACT_GEOM_TARGETS) 拦截 ⇒ 杠杆空转
#   (RUN63/71/73 取证: 评估在混合榜首、杠杆只落 b3, 槽深逐轮对折 27.8→5.0mm)。
#   锁定轨直接按冲片锁定层 (_lock_layer) 发绕组等价处方 (P0-6: increase_wire_gauge 降电密;
#   RUN73 真实标定 κ_wire≈0.0444 为全轨唯一真实有效抬 SF 杠杆, 几何 od 杠杆 κ_od≈0 惰性)。
#   ⚠ rail=None (旧调用) 时本表不生效, 行为与历史版本一致 (零回归)。
_RAIL_GEOM_LOCKED = {
    "main_a": True,      # 锁 stator_od/stator_id/rotor_od/shaft_dia/tooth_width/yoke_thickness/...
    "bypass_b1": True,   # 锁 stator_od/stator_id/rotor_od/shaft_dia
    "bypass_b2": True,   # 锁 stator_od/shaft_dia
    "fallback": True,    # 锁 stator_od
    "bypass_b3": False,  # 全自由 (fixed_keys=())
}


# ═══ S3 (2026-10-10 用户授权): 同轴几何动作冲突消解 ═══
#   取证 (RUN71 实跑 9 轮质量门): 2D 层每轮**同时**下发 deepen_yoke (hj↑ ⇒ 槽面积↓ ⇒ SF↑)
#   与 narrow_tooth (slot_fill_target↓ ⇒ 槽面积需求↑ ⇒ 周向齿宽 bt↓ ⇒ Bt↑)。二者争
#   **同一径向空间** R = (Dso−Dsi)/2 = hj + hs —— Dso 锁定的轨 (a/b1/b2/fallback) R 为
#   常量 ⇒ hj↑ 必然挤占 hs, 与窄齿抢空间; 且二者对 SF 方向相反 ⇒ 净效应对冲。
#   硬证据: 分轨后 40 条指令被方向②拦截 0 条 (空转已治), 但 Bt 9 轮 Δ=−0.046 (不升反降)、
#   By Δ=+0.014 (纹丝不动) ⇒ 瓶颈是「同轴对冲」, 而非「杠杆被拦」。
#   消解规则: 同轮只允许**一个轴**动几何 —— 按归一化磁密缺口 (|By′/带边−1| vs |Bt′/带边−1|)
#   取大者为主轴; 次轴改发**非几何等效处方** (励磁/绕组/铁心长, 全轨可达、不占径向空间),
#   并在 reason 标注 [同轴让位] 供审计。
#   ⚠ 开关 QG_COAXIAL_DECONFLICT=0 ⇒ 完全回退旧行为 (零回归)。
QG_COAXIAL_DECONFLICT = 1
# ★ P2-1 (2026-10-11 用户授权, 单变量对照): 同轴「深轭+窄齿」联合反解处方
#   现状(S3): deepen_yoke(yoke轴,+1占空间) 与 narrow_tooth(tooth轴,+1要空间) 同号 ⇒ 判
#     冲突 ⇒ 主轴保留、次轴换非几何等效处方(相互让位)。缺陷: 窄齿是 Bt 唯一直接杠杆,
#     让位后 Bt 修不动。联合模式(QG_COAXIAL_JOINT=1): 不换处方, 改发**联合处方** ——
#     保留 deepen_yoke, 并把 narrow_tooth 与深轭同步协调(二者共占径向 R), 闭式给出
#     (Δhj, Δbt) 配对幅度, 附「方向/幅度」审计摘要。⚠ 默认 0 ⇒ 完全回退旧行为(零回归)。
QG_COAXIAL_JOINT = _qg_exp_int("QG_COAXIAL_JOINT", 0)

# ★ G2 (2026-10-11 用户授权): Bδ 杠杆补齐 —— 齿宽触机械下限时改用气隙磁密杠杆。
#   取证 (RUN70 九轮): corr(Bt,Bd)=+0.881 而 corr(Bt,Dso)=+0.009 ⇒ Bt 由 Bδ 主控;
#   而齿宽带 [7.091, 9.465]mm 的下限 7.091 由「③磁上限反推」固定, 轮6~8 三度跌破被
#   J4-R1 拦截 ⇒ narrow_tooth 无法长期生效 (九轮仅 2 次签发), Bt 1.119~1.403 恒出带。
#   故当齿宽余量 ≤ QG_BD_TOOTH_TOL 时, 追加一个**不占径向空间**的 Bδ 杠杆。
#   ⚠ 设 0 ⇒ 完全回退旧行为 (零回归); 上限 QG_BD_LEVER_MAX 为单轮幅度钳。
QG_BD_LEVER_FALLBACK = int(os.environ.get("QG_BD_LEVER_FALLBACK", "1") or 0)
QG_BD_LEVER_MAX = float(os.environ.get("QG_BD_LEVER_MAX", "0.08") or 0.08)
QG_BD_TOOTH_TOL = float(os.environ.get("QG_BD_TOOTH_TOL", "0.10") or 0.10)
# ★ G1-豁免 (2026-10-11): S3 同轴让位时, 齿宽已触底者 narrow_tooth 不可让位 (默认 1)。
QG_BD_NARROW_EXEMPT = int(os.environ.get("QG_BD_NARROW_EXEMPT", "1") or 0)
# ★ G3 (2026-10-11 用户授权): 齿宽下限「磁上限反推」按实际 Bδ 动态化 (默认 1=启用)。
#   见 Scripts/_mag_precheck_2d.py::tooth_width_ok (dynamic_min+b_delta)。
QG_TOOTH_MIN_DYNAMIC = int(os.environ.get("QG_TOOTH_MIN_DYNAMIC", "1") or 0)

# 动作 → 同轴分组 ("yoke" = 径向 hj 轴 / "tooth" = 周向 bt·槽面积轴 / 未列 = 非同轴)
#   ⚠ narrow_tooth / widen_tooth / increase_slot_area 三者共用同一真实旋钮
#     (slot_fill_target → 槽宽 w_eq → 齿宽 bt), 见 :2502-2506; 同属 tooth 轴。
#   ⚠ increase_outer_dia / decrease_stator_od 是 yoke 轴的**接力动作** (径向空间 R 本身),
#     依附于 yoke 轴: yoke 轴被让位时一并撤下。
_COAXIAL_AXIS = {
    "deepen_yoke": "yoke", "shrink_yoke": "yoke",
    "increase_outer_dia": "yoke", "decrease_stator_od": "yoke",
    "narrow_tooth": "tooth", "widen_tooth": "tooth",
    "increase_slot_area": "tooth",
}

# 动作对**径向空间**的需求方向 (+1 = 要占空间 / −1 = 释放空间)。
#   冲突判据 = 两轴动作**同号** (都要空间, 或都释放) ⇒ 真冲突, 需消解;
#   异号 ⇒ **互补** (一方释放的空间正好给另一方用) ⇒ 不动 (零回归)。
#   典型互补对 (★点⑤ 回归场景): shrink_yoke (hj↓ ⇒ 释放径向) + narrow_tooth
#     (slot_fill_target↓ ⇒ 槽面积需求↑ ⇒ 要空间) —— 减轭释放的空间正好给窄齿扩槽,
#     二者配合而非对冲 ⇒ 必须保持旧行为。
#   典型冲突对 (RUN71 实证): deepen_yoke (hj↑ ⇒ 占空间) + narrow_tooth (要空间)
#     ⇒ 都要同一份 R = (Dso−Dsi)/2 ⇒ 净效应对冲 (Bt/By 9 轮零改善)。
_COAXIAL_SPACE_DIR = {
    "deepen_yoke": 1, "decrease_stator_od": 1,      # 占/挤压槽的径向空间
    "narrow_tooth": 1, "increase_slot_area": 1,     # 需要更大槽面积 ⇒ 要空间
    "shrink_yoke": -1, "increase_outer_dia": -1,    # 释放径向空间给槽
    "widen_tooth": -1,                              # 收窄槽面积需求 ⇒ 释放空间
}


def _coaxial_axis_gaps(qg_result: dict):
    """(gap_yoke, gap_tooth): 各轴归一化磁密对**越界侧带边**的相对缺口 (>0 = 越界程度)。

    缺口 = |value/target − 1|, target 取越界侧带边 (high ⇒ 上限, low ⇒ 下限);
    在带内 / 缺口不可算 (归一化数据缺失) ⇒ 0。
    """
    _mn = (qg_result or {}).get("_mag_normalized") or {}
    _fl = (qg_result or {}).get("_mag_norm_flags") or {}

    def _gap(val_key, band_key, flag):
        try:
            _v = float(_mn.get(val_key) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        _b = _mn.get(band_key) or [0, 0]
        try:
            _lo, _hi = float(_b[0]), float(_b[1])
        except (TypeError, ValueError, IndexError):
            return 0.0
        if _v <= 0 or _lo <= 0 or _hi <= 0:
            return 0.0
        if flag == "high" and _v > _hi:
            return _v / _hi - 1.0
        if flag == "low" and _v < _lo:
            return _lo / _v - 1.0
        return 0.0

    return (_gap("by_norm", "by_norm_band", str(_fl.get("yoke") or "")),
            _gap("bt_norm", "bt_norm_band", str(_fl.get("tooth") or "")))


def _build_joint_yoke_tooth(items: list, qg_result: dict = None) -> dict:
    """★ P2-1 (2026-10-11 用户授权): 深轭(deepen_yoke) + 窄齿(narrow_tooth) 联合处方。

    二者同轴但物理上**不争同一份径向空间** (深轭挤径向 hs, 窄齿动周向 bt),
    强行让位会丢 Bt 唯一直接杠杆。本函数从同轮指令里取二者的原始幅度, 给出
    配对 (Δhj, Δbt) 与方向/幅度审计摘要, 供调用方把窄齿并入联合处方、深轭保留。
    """
    _dy = next((it for it in items if str(it.get("action")) == "deepen_yoke"), None)
    _nt = next((it for it in items if str(it.get("action")) == "narrow_tooth"), None)
    if not _dy or not _nt:
        return None
    try:
        _dhj = float(_dy.get("delta") or 0.0)
        _dbt = float(_nt.get("delta") or 0.0)
    except (TypeError, ValueError):
        _dhj, _dbt = 0.0, 0.0
    return {
        "action": "joint_yoke_tooth",
        "param": "yoke_thickness,tooth_width",
        "delta": {"yoke_thickness": round(_dhj, 4), "tooth_width": round(_dbt, 4)},
        "direction": ("深轭(hj↑⇒By↓) + 窄齿(bt↓⇒Bt↑): 同轴联合, 共占径向 R=(Dso−Dsi)/2, "
                      "不再相互让位(避免窄齿Bt杠杆被换非几何处方而失效)"),
        "magnitude": {"delta_hj": round(_dhj, 4), "delta_bt": round(_dbt, 4),
                      "note": "二者对 SF 同向(深轭抬SF/窄齿压SF), 联合反解配对幅度"},
        "conflict": "coaxial_joint",
        "_coaxial_joint": True,
        "reason": ("[同轴联合 P2-1: 深轭Δhj=%+.1f%% + 窄齿Δbt=%+.1f%% 同步下发, "
                   "保留窄齿作Bt直接杠杆]" % (_dhj * 100.0, _dbt * 100.0)),
    }


def _resolve_coaxial_conflict(instrs: list, qg_result: dict = None,
                              state: dict = None) -> list:
    """★ S3 (2026-10-10 用户授权): 同轴几何动作同轮共存 ⇒ 只留主轴, 次轴换非几何等效处方。

    次轴等效处方 (全轨可达、不占径向空间):
      齿轴让位 narrow_tooth (Bt 低) → reduce_core_length (L↓ ⇒ A_p↓ ⇒ Bδ↑ ⇒ Bt↑)
      齿轴让位 widen_tooth  (Bt 高) → 降 Bδ: voltage ⇒ increase_turns / constant_j ⇒ reduce_turns
      轭轴让位 deepen_yoke  (By 高) → 降 Bδ: 同上 (By ∝ Bδ/hj, hj 不动则只能降 Bδ)
      轭轴让位 shrink_yoke  (By 低) → raise_air_gap_flux (提 Bδ ⇒ By↑)
      齿轴让位 increase_slot_area (压 SF) → reduce_turns (3D 等价处方, 不碰槽形)
    ⚠ 缺口不可算 (归一化磁密缺失) 时保守不动 —— 零回归。
    """
    if not QG_COAXIAL_DECONFLICT:
        return list(instrs or [])
    _items = [it for it in (instrs or []) if isinstance(it, dict)]
    if not _items:
        return _items
    _axes = {}
    for _it in _items:
        _ax = _COAXIAL_AXIS.get(str(_it.get("action") or ""))
        if _ax:
            _axes.setdefault(_ax, []).append(_it)
    if len(_axes) < 2:
        return _items
    # 冲突 = 存在**同号** (空间需求方向相同) 的两轴动作对; 全异号 ⇒ 互补 ⇒ 不动 (零回归)
    def _dir(it):
        return _COAXIAL_SPACE_DIR.get(str(it.get("action") or ""), 0)

    _conflict = False
    for _a in _axes.get("yoke", []):
        for _b in _axes.get("tooth", []):
            _da, _db = _dir(_a), _dir(_b)
            if _da != 0 and _da == _db:
                _conflict = True
                break
        if _conflict:
            break
    if not _conflict:
        return _items
    _gy, _gt = _coaxial_axis_gaps(qg_result)
    if _gy <= 0 and _gt <= 0:
        return _items          # 缺口不可算 ⇒ 不做仲裁 (零回归)
    # 缺口大者为主轴; 相等 ⇒ 保守归 yoke 轴 (By 越界后果更重: 饱和 + 铁耗)
    _keep = "yoke" if _gy >= _gt else "tooth"
    _keep_dirs = {_dir(_k) for _k in _axes.get(_keep, []) if _dir(_k) != 0}
    _mode = _resolve_em_mode(state)
    _note = ("[同轴让位: 本轮几何杠杆归 %s 轴 (缺口 By %.1f%% / Bt %.1f%%) ⇒ "
             "改发非几何等效处方, 避免与主轴争径向空间]" % (_keep, _gy * 100.0, _gt * 100.0))

    def _sub(_it, _act, _param, _delta, _reason, _conflict="none"):
        _new = dict(_it)
        _new.update({"action": _act, "param": _param, "delta": _delta,
                     "reason": _reason + " " + _note, "conflict": _conflict,
                     "_coaxial_yield": True})
        return _new

    _out = []
    _n_exempt = []          # ★ G1-豁免: 被豁免不让位的动作 (审计/日志用)
    _joint_emitted = False  # ★ P2-1: 联合处方已发标记 (每轮至多一次)
    for _it in _items:
        _act = str(_it.get("action") or "")
        _ax = _COAXIAL_AXIS.get(_act)
        if not _ax or _ax == _keep:
            _out.append(_it)
            continue
        # 次轴中与主轴**异号**的动作 = 互补 (它释放/不占主轴所需的空间) ⇒ 保留 (零回归)
        _d = _dir(_it)
        if _d != 0 and _keep_dirs and (_d not in _keep_dirs):
            _out.append(_it)
            continue
        # ★ P2-1 (2026-10-11): 同轴「深轭+窄齿」联合处方 —— 二者物理上不争同一份空间
        #   (深轭挤径向 hs, 窄齿动周向 bt), 强行让位会丢 Bt 直接杠杆; 改发联合(Δhj,Δbt)
        #   配对, 保留窄齿, 不再相互让位。每轮至多一次 (joint 已发则跳过)。
        if (QG_COAXIAL_JOINT and _act == "narrow_tooth"
                and "yoke" in _axes
                and any(str(i.get("action")) == "deepen_yoke" for i in _axes["yoke"])
                and not _joint_emitted):
            _joint = _build_joint_yoke_tooth(_items, qg_result)
            if _joint:
                _out.append(_joint)
                _joint_emitted = True
                continue
        # ── 次轴且与主轴同号 (争同一份空间): 换非几何等效处方 ──
        # ★ G1-豁免 (2026-10-11 用户授权, QG_BD_NARROW_EXEMPT): 齿宽已触机械下限时
        #   narrow_tooth **不可让位**。病根 (RUN70 取证): 让位目标 reduce_core_length
        #   减铁长, 不改变几何因子 Bt/Bd, 而窄齿是 Bt 的唯一直接杠杆; 齿宽一旦触底
        #   (bt ≤ bt_min·(1+TOL)) 再让位 ⇒ Bt 必然仍修不动 (九轮实证 1.119~1.403)。
        #   零回归: 齿宽余量充足时行为与 S3 原有完全一致 (仍让位)。
        if _act == "narrow_tooth" and QG_BD_NARROW_EXEMPT:
            try:
                _bmm = _bmn = 0.0
                for _jc in (qg_result or {}).get("checks", []) or []:
                    if _jc.get("name") == "齿宽范围":
                        _bmm = float((_jc.get("value") or {}).get("bt_mm", 0) or 0)
                        _bmn = float((_jc.get("value") or {}).get("bt_min", 0) or 0)
                        break
                if _gt > 0 and _bmm > 0 and _bmn > 0 and _bmm <= _bmn * (1.0 + QG_BD_TOOTH_TOL):
                    _out.append(_it)
                    _n_exempt.append(_act)
                    continue
            except (TypeError, ValueError):
                pass
        if _act == "narrow_tooth":
            _out.append(_sub(_it, "reduce_core_length", "core_length", -0.05,
                             "齿磁密过低(同轴让位): 减铁长 A_p↓⇒Bδ↑⇒Bt↑ (不占径向空间)"))
        elif _act == "widen_tooth":
            if _mode == "constant_j":
                _out.append(_sub(_it, "reduce_turns", "turns_per_coil", -0.06,
                                 "齿磁密过高(同轴让位·恒J): 减匝降 Bδ (F=N·I∝N ⇒ Bδ∝N)",
                                 "torque_emf"))
            else:
                _out.append(_sub(_it, "increase_turns", "turns_per_coil", 0.06,
                                 "齿磁密过高(同轴让位·电压): 增匝降 Bδ (Bδ∝1/N)",
                                 "torque_emf"))
        elif _act == "deepen_yoke":
            if _mode == "constant_j":
                _out.append(_sub(_it, "reduce_turns", "turns_per_coil", -0.06,
                                 "轭磁密过高(同轴让位·恒J): hj 不动 ⇒ 减匝降 Bδ (Bδ∝N)",
                                 "torque_emf"))
            else:
                _out.append(_sub(_it, "increase_turns", "turns_per_coil", 0.06,
                                 "轭磁密过高(同轴让位·电压): hj 不动 ⇒ 增匝降 Bδ (Bδ∝1/N)",
                                 "torque_emf"))
        elif _act == "shrink_yoke":
            _out.append(_sub(_it, "raise_air_gap_flux", "air_gap_flux_density", 0.05,
                             "轭磁密过低(同轴让位): hj 不动 ⇒ 提 Bδ (By∝Bδ/hj)",
                             "torque_emf"))
        elif _act == "increase_slot_area":
            _out.append(_sub(_it, "reduce_turns", "turns_per_coil", -0.05,
                             "槽满率过高(同轴让位): 降匝压占槽率, 不动 slot_fill_target",
                             "torque_emf"))
        else:
            # yoke 轴接力动作 (increase_outer_dia / decrease_stator_od) 随主轴撤下
            continue
    # 替换后可能出现同 (action,param) 重复 ⇒ 去重, 保留 |delta| 更大者 (口径同 _add)
    _ded = []
    for _it in _out:
        for _j, _ex in enumerate(_ded):
            if str(_ex.get("action")) == str(_it.get("action")) and \
                    str(_ex.get("param")) == str(_it.get("param")):
                try:
                    if abs(float(_it.get("delta") or 0)) > abs(float(_ex.get("delta") or 0)):
                        _ded[_j] = _it
                except (TypeError, ValueError):
                    pass
                break
        else:
            _ded.append(_it)
    return _ded


def build_improvement_instructions(qg_result: dict, state: dict = None,
                                   rail: str = None) -> list:
    """由门禁失败原因生成结构化改进指令 (list[dict])。

    指令 schema: {action, param, delta(相对比例, +增/-减), reason, conflict}
      - 槽满率超限 → increase_slot_area: 降 slot_fill_target(→槽面积↑→SF↓), 冲突=齿/轭磁密
      - 温升超限   → reduce_turns + lengthen_core: 降 J / 降铜耗, 冲突=成本/机座
      - 电密超限   → reduce_current_density
      - 效率不足   → lengthen_core: 增 L 提效率
    """
    reasons = (qg_result or {}).get("reasons", []) or []
    instrs: List[dict] = []
    joined = " ".join(str(r) for r in reasons)
    # P2 (v4.02.93): 冲片锁定层判定 —— fail_stage=="3d" 时冲片不可改,
    #   只允许 绕组(线规×并绕/匝数/并联支路) + 铁心长 旋钮; 相应处方走绕组措施。
    _stage = str((qg_result or {}).get("fail_stage") or "")
    _lock_layer = (_stage == "3d")
    # ★ 方案② (2026-10-10 用户授权): 轨内可达杠杆集 —— 该轨 fixed_keys 与几何抬 SF
    #   处方主写键相交 ⇒ 几何动作注定被方向②拦截(杠杆空转) ⇒ 直接按冲片锁定层
    #   发绕组等价处方 (P0-6 同款)。评估对象=执行对象同轨, 错位结构性消除。
    if rail is not None and _RAIL_GEOM_LOCKED.get(str(rail), False):
        _lock_layer = True

    def _add(action, param, delta, reason, conflict, **extra):
        # 去重: 同 action+param 只保留一次 (取更激进 delta 的绝对值)
        # ★ P1-1 (2026-09-22): 支持**附加字段** (如 delta_t = 绝对步长, 单位 T),
        #   供只认绝对量的执行器使用 (raise_air_gap_flux)。
        # ★★ P1-1 修正 (2026-09-22 收尾): `delta_t` 取 0 / None 时**必须摘除该键**。
        #   执行器 `orchestrator.py:5606-5618` 只在 `delta_t is None` 时回退
        #   「相对 → 等效绝对」换算 (`_dt = _bd * delta`); 若显式传 0.0 ⇒ `_dt = 0`
        #   ⇒ `_bd_new == _bd` ⇒ `changed` 不置位 ⇒ **静默空动作** (P1-1 靶心未中;
        #   实测 smoke: 齿低/轭低/气隙低 三条调用点同时携带 delta_t=0.0)。
        #   摘键后统一走相对回退并被执行器钳到 ±0.02T (= P1-1 的绝对步长上限)。
        if "delta_t" in extra:
            try:
                _dtv = extra.get("delta_t")
                if _dtv is None or abs(float(_dtv)) <= 0.0:
                    extra.pop("delta_t", None)
            except (TypeError, ValueError):
                extra.pop("delta_t", None)
        for it in instrs:
            if it["action"] == action and it["param"] == param:
                if abs(delta) > abs(it["delta"]):
                    it["delta"], it["reason"], it["conflict"] = delta, reason, conflict
                    it.update(extra)
                return
        _it = {"action": action, "param": param, "delta": delta,
               "reason": reason, "conflict": conflict}
        _it.update(extra)
        instrs.append(_it)

    # ── 2026-09-20 (用户指令): 槽满率判据**方向化** ────────────────────────────
    #   旧写法只按 "槽满率" 子串命中 ⇒ 槽满率**偏低** 也会走 increase_slot_area
    #   (放大槽面积), 属**反向旋钮** (SF 越修越低), 且与新增的 shrink_slot_keep_yoke
    #   构成同一物理量的双计数。现按结构化 fail_tags 判向 (勿解析 reasons 文案 ——
    #   "槽满率偏低 0.278 < 下限 0.65" 数字夹中间, 紧邻子串匹配恒不可靠)。
    #   兼容: 无 fail_tags (旧调用/纯 warning 观察态) 时退回子串判据。
    _tags = [str(t) for t in ((qg_result or {}).get("fail_tags") or [])]
    _sf_low_fail = bool((qg_result or {}).get("_sf_low")) or \
        any("槽满率下限" in t for t in _tags) or \
        (("槽满率" in joined) and ("偏低" in joined))
    _sf_high_fail = any("槽满率上限" in t for t in _tags) or (
        ("槽满率" in joined) and not _sf_low_fail)
    # ── P8 (2026-09-21 用户指令①): 跨轮同向性仲裁 —— 抑制 SF 过冲 / 方向反转震荡 ──
    #   现象 (RUN30/31 实测): SF 0.804 → 0.228 → 0.937 → 0.408。2D 路径 (收冲片 ⇒ SF↑)
    #   与 3D 路径 (放大槽面积 / 减匝 ⇒ SF↓) **方向相反且交替触发**, 每轮都跨过目标带 ——
    #     上轮修完落在带的另一侧 ⇒ 本轮反向再修 ⇒ 再跨回另一侧 ⇒ 永不收敛。
    #   ① 过冲判据: 上轮 SF 与本轮 SF 分居目标带两侧 (差值乘积 < 0) ⇒ 上轮跨带过冲 ⇒ 步长 ×0.5
    #   ② 反转判据: 上轮下发方向与本轮所需方向相反 ⇒ 震荡 ⇒ 步长再 ×0.5 (合计 0.25×)
    #   只缩步长、不改方向: 仍朝带内推进, 但不再一步跨带 (避免一刀切禁用导致卡死)。
    _prev_sf = 0.0
    try:
        _prev_sf = float(((state or {}).get("prev_round_metrics") or {}).get("slot_fill") or 0.0)
    except (TypeError, ValueError):
        _prev_sf = 0.0
    _prev_dir = 0        # +1 = 上轮在抬 SF, -1 = 上轮在压 SF
    # P8b (2026-09-21): 方向分类统一引用 _SF_RAISE_ACTIONS / _SF_LOWER_ACTIONS ——
    #   旧内联元组把 `increase_wire_gauge`(加粗线规) 归入「压 SF」侧, 方向判定反了,
    #   折半系数因此加在错误的一侧。
    for _pi in ((state or {}).get("improvement_instructions") or []):
        _a = str((_pi or {}).get("action") or "")
        if _a in _SF_RAISE_ACTIONS:
            _prev_dir = 1
            break
        if _a in _SF_LOWER_ACTIONS:
            _prev_dir = -1
            break
    _sf_tgt = 0.0
    try:
        _sf_tgt = 0.5 * (float(_SF_TARGET_BAND[0]) + float(_SF_TARGET_BAND[1]))
    except Exception:
        _sf_tgt = 0.0
    if _sf_tgt <= 0:
        _sf_tgt = 0.70

    def _sf_osc_scale(_cur_sf, _cur_dir):
        """P8: 由 (上轮 SF, 上轮方向) 与 (本轮 SF, 本轮方向) 的关系给出步长缩放系数。

        返回 1.0 (正常) / 0.5 (过冲或反转) / 0.25 (过冲且反转)。
        """
        _s = 1.0
        if _prev_sf > 0 and _cur_sf > 0 and (_prev_sf - _sf_tgt) * (_cur_sf - _sf_tgt) < 0:
            _s *= 0.5     # 上轮动作把 SF 从带的一侧推到了另一侧 (跨带过冲)
        if _prev_dir != 0 and _prev_dir != _cur_dir:
            _s *= 0.5     # 本轮方向与上轮相反 (方向反转震荡)
        return _s

    if _sf_low_fail:
        # 3D 门「槽满率偏低」→ 动作 → 回 **2D 流程起点** (regenerate_2d)。
        #   动作 (shrink_slot_keep_yoke): 减小定子槽高 Hs, 同步减小定子外径 Dso,
        #     ΔDso = 2·ΔHs ⇒ hj = (Dso−Dsi)/2 − Hs 与定子轭磁密 By **不变**;
        #     槽面积 ∝ Hs (槽宽由齿宽锚定) ⇒ SF ↑。
        #   步长按缺口自适应: 目标 SF 取目标带中心 (真源 SLOT_FILL_TARGET_BAND),
        #     由 SF ∝ 1/槽面积 ⇒ 槽高需缩至 SF_cur/SF_tgt 倍 ⇒ 收缩比 = 1 − SF_cur/SF_tgt。
        #     钳 [0.05, 0.40]: 单轮最多收 40% 槽高 —— 实测 (RUN27 几何复算) 槽底宽
        #     随槽深同步变窄 ⇒ 槽面积收缩快于线性, 满额一步到位会过冲跨带 (SF>上限)。
        _sf_cur = 0.0
        for _c in (qg_result or {}).get("checks", []) or []:
            if _c.get("name") == "槽满率下限":
                try:
                    _sf_cur = float(_c.get("value") or 0.0)
                except (TypeError, ValueError):
                    _sf_cur = 0.0
        _ratio = (1.0 - (_sf_cur / _sf_tgt)) if _sf_cur > 0 else 0.20
        _ratio = max(0.05, min(0.40, _ratio))
        # P8: 跨轮同向性仲裁 —— 过冲 / 反转 ⇒ 步长 0.5× / 0.25×
        _sc = _sf_osc_scale(_sf_cur, 1)
        _osc_note = ""
        if _sc < 1.0:
            _ratio = max(0.05, _ratio * _sc)
            _osc_note = " [P8 震荡抑制: 步长 ×%g%s%s]" % (
                _sc,
                ", 上轮跨带过冲" if (_prev_sf > 0 and (_prev_sf - _sf_tgt) * (_sf_cur - _sf_tgt) < 0) else "",
                ", 方向与上轮相反" if (_prev_dir != 0 and _prev_dir != 1) else "")
        # ── ★ P0-6 补完 (2026-09-22 用户指令): 3D 门(冲片锁定层) 槽满率偏低 ⇒ 降电密 ──
        #   槽形(槽宽/齿宽/槽深/外径)属冲片几何, 一旦锁定就只能动**装配级杠杆**。
        #   ⚠ 旧处方「增加导线匝数 + 减小铁心长度」经 RUN40 实测对 SF **弹性 ≈ 0**
        #     (槽满率守恒: 匝数变化后线径按 d_wire ∝ sqrt(1/tpc) 重算 ⇒ Z·A_cond ≡ const),
        #     故改为 **降电密 J↓ ⇒ A_cond↑ ⇒ SF↑**, 并交出 P1-2 的缺口反解步长。
        #   槽形尺寸改动作**下放给 2D 门**(见 else 分支), 由分层预算控制何时才允许动。
        if _lock_layer:
            # ★ P0-6 补完 + P1-2 (2026-09-22 用户指令): 冲片锁定层的抬 SF 处方由
            #   「增匝数 + 减小铁长」**更正为降电密** —— RUN40 实测前者对 SF 弹性 ≈ 0:
            #     · motor_tools.py:696-720「槽满率守恒」在匝数变化后按 d_wire ∝ sqrt(1/tpc)
            #       重算线径 ⇒ Z·A_cond ≡ const ⇒ SF 纹丝不动 (实测三组仅 +0.0025/±0.0000);
            #     · reduce_core_length 根本不进 SF 关系式 (只改轴向长度)。
            #   而降电密 J↓ ⇒ A_cond = (I/a)/J ↑ ⇒ SF = Z·A_cond/S_slot ↑ —— 本层
            #   **唯一**有效且不碰槽形的杠杆 (与 _tier_substitute_instrs 的 P0-6 等价替换同向,
            #   两处口径现已一致; 此前本分支未同步 ⇒ 3D 层仍发零弹性处方, P1-2 无从生效)。
            #   步长 = clamp(1 − SF_cur/SF_tgt, 5%, 25%) —— P1-2 缺口反解。
            #   再乘 P8 震荡抑制系数 _sc (过冲/方向反转 ⇒ 0.5× / 0.25×)。
            # ── ★ P1-4 (2026-10-11 用户授权, 单变量对照): 锁定轨「杠杆耗尽」早期识别 ──
            #   锁定轨唯一下发 increase_wire_gauge, 但评估对象(TOP1)几何被锁 ⇒ 真实抬 SF
            #   杠杆落空 ⇒ SF 连续 N 轮空转 (H36 杠杆错位复现)。检测: state["rail_sf_history"]
            #   [rail] 末 N 个 SF 落在 SF_SD_STALL_EPS·N 内(扁平) ⇒ 判定耗尽 ⇒ (a) 智能反算
            #   线径 d_new=d_cur·√(SFtgt/SFcur) 一步到位(加大幅度, 上限放宽 LEVER_EXHAUST_BOOST);
            #   (b) 打 _lever_exhausted 标记 ⇒ orchestrator 据此「转人工/换轨」(配额让 b3)。
            #   ⚠ LEVER_EXHAUST_N=0 (默认) ⇒ 完全不触发, 零回归。
            _exhausted = False
            if LEVER_EXHAUST_N and rail is not None and _sf_cur > 0 and _sf_tgt > 0:
                _hist = list(((state or {}).get("rail_sf_history") or {}).get(str(rail)) or [])
                if len(_hist) >= LEVER_EXHAUST_N:
                    _recent = _hist[-LEVER_EXHAUST_N:]
                    if (max(_recent) - min(_recent)) < (SF_SD_STALL_EPS * LEVER_EXHAUST_N):
                        _exhausted = True
            if _exhausted:
                # 智能反算: 闭式反解线径 (SF ∝ 1/J ∝ d² ⇒ d_new=d_cur·√(SFtgt/SFcur))
                _ratio = float(_sf_tgt) / float(_sf_cur)
                _wg_step = round(max(WIRE_GAUGE_STEP_MIN,
                                    min(LEVER_EXHAUST_BOOST, (_ratio ** 0.5) - 1.0)), 4)
                if _sc < 1.0:
                    _wg_step = round(max(0.02, _wg_step * _sc), 4)
                _add("increase_wire_gauge", "wire_diameter", _wg_step,
                     f"槽满率偏低 {_sf_cur:.3f}→目标 {_sf_tgt:.2f} (冲片锁定层): 杠杆耗尽"
                     f"(N={LEVER_EXHAUST_N}轮SF空转) ⇒ 智能反算线径 d→d·√(SFtgt/SFcur)="
                     f"{_wg_step:+.1%}(加大幅度) (P0-6/P1-2/P1-4){_osc_note}",
                     "slot_fill_up",
                     sf_cur=round(_sf_cur, 4), sf_tgt=round(_sf_tgt, 4),
                     _lever_exhausted=True)
            else:
                _wg_step = wire_gauge_step(sf_cur=_sf_cur, sf_tgt=_sf_tgt)
                if _sc < 1.0:
                    _wg_step = round(max(0.02, _wg_step * _sc), 4)
                # ★ 项3 (2026-09-30 用户授权): param 统一为 wire_diameter (口径唯一化)。
                #   旧写法 param="current_density" + delta 取负 —— 与其余 3 处签发点
                #   (wire_diameter + delta 取正) 口径分裂; 执行器 (orchestrator:5845) 只按
                #   act 分派不读 param, 故运行时无差异, 但语义与维护口径必须统一。
                _add("increase_wire_gauge", "wire_diameter", _wg_step,
                     f"槽满率偏低 {_sf_cur:.3f}→目标 {_sf_tgt:.2f} (冲片锁定层): 加大线规 "
                     f"(线径↑ ⇒ 电密 J↓ ⇒ 导体截面↑ ⇒ 占槽率↑), **不改变槽形尺寸** "
                     f"(P0-6/P1-2){_osc_note}",
                     "slot_fill_up",
                     sf_cur=round(_sf_cur, 4), sf_tgt=round(_sf_tgt, 4))
        else:
            # ★ P0-3 (2026-09-22 用户指令): 处方改接**槽深纯杠杆** `set_slot_depth`,
            #   步长由缺口**反解**而非固定 38%。
            #   反解式: 槽面积 ∝ hs2 (槽宽由齿宽/可用槽宽锚定, 与槽深无关)
            #     ⇒ 要 SF 从 SF_cur 升到 SF_tgt, 需 hs2_new = hs2 · (SF_cur / SF_tgt)
            #     ⇒ delta = SF_cur/SF_tgt − 1  (负值 = 收槽高)
            #   RUN40 实证精度 ~2%: 收 38% 把 SF 0.434 → 0.687 (预测 0.700);
            #     而该轮真实缺口需 47% 收缩, 被旧 shrink 的 40% 钳位截断 ⇒ 白耗一轮预算。
            #   钳 [-0.60, +0.45]: 上限防一步挖穿槽底/齿根支撑, 下限防过冲跨带。
            _sd_ratio = ((_sf_cur / _sf_tgt) - 1.0) if (_sf_cur > 0 and _sf_tgt > 0) else -0.20
            _sd_ratio = round(max(-0.60, min(0.45, _sd_ratio)), 4)
            # ★★ 杠杆停滞守卫 (2026-10-10 用户授权, RUN67 取证) —— 见常量区注释。
            #   反解式本身自洽, 但失效场景下 SF 永不改善 ⇒ 每轮等额反解 ⇒ 槽深逐轮对折。
            #   判据: 上轮已发抬 SF 杠杆 (_prev_dir==1) 且 SF 较上轮改善 < SF_SD_STALL_EPS。
            _stalled = bool(_prev_dir == 1 and _prev_sf > 0 and _sf_cur > 0
                            and (_sf_cur - _prev_sf) < SF_SD_STALL_EPS)
            _stall_note = ""
            if _stalled:
                _sd_ratio = round(_sd_ratio * SF_SD_STALL_DAMP, 4)
                _stall_note = (" [杠杆停滞: SF %.3f→%.3f 无实质改善, 步长 ×%g 不再等额加码]"
                               % (_prev_sf, _sf_cur, SF_SD_STALL_DAMP))
            if _sc < 1.0:
                _sd_ratio = round(_sd_ratio * _sc, 4)
            _add("set_slot_depth", "slot_depth", _sd_ratio,
                 f"槽满率偏低 {_sf_cur:.3f}→目标 {_sf_tgt:.2f}: 由缺口反解槽深 "
                 f"hs2_new = hs2·(SF_cur/SF_tgt) [{_sd_ratio:+.1%}] —— 槽深已独立可调, "
                 f"锁轭厚 hj 不变(保 By) ⇒ 仅槽面积↓⇒SF↑{_osc_note}{_stall_note}",
                 "none",
                 # ★ P1-2 (2026-09-22): 把**实测 SF 对**随指令下传 ——
                 #   3D 层把本处方等价替换成 increase_wire_gauge (降电密) 时,
                 #   执行器据 1−SF_cur/SF_tgt 反解步长 (钳 5%~25%), 而非吃固定的 3%。
                 sf_cur=round(_sf_cur, 4), sf_tgt=round(_sf_tgt, 4))
        # ── P9 (2026-09-21 用户指令②): 收冲片前先预判 hj/hs 是否会被推越带 ──
        #   机理: shrink_slot_keep_yoke 只缩槽高 Hs 而轭厚 hj 不变 ⇒ hj/hs **单调↑**。
        #   RUN31 实测 2.65 → 2.82, 越出 6 极带 [1.0, 2.5]。故当 hj/hs 已逼近上限时,
        #   单变量收槽高必然越界 ⇒ 追加**双变量解**: 抬定子内径 Dsi
        #   (hj = (Dso−Dsi)/2 − Hs ⇒ Dsi↑ ⇒ hj↓) 与缩槽高配对, 既抬 SF 又把 hj/hs 压回带内。
        _ysr, _ysr_hi = 0.0, 2.5
        for _c in (qg_result or {}).get("checks", []) or []:
            if _c.get("name") == "轭厚/槽深比":
                _v = _c.get("value")
                if isinstance(_v, dict):
                    try:
                        _ysr = float(_v.get("hj_hs") or 0.0)
                    except (TypeError, ValueError):
                        _ysr = 0.0
                    _b = _v.get("band") or _c.get("threshold")
                    if isinstance(_b, (list, tuple)) and len(_b) == 2:
                        try:
                            _ysr_hi = float(_b[1])
                        except (TypeError, ValueError):
                            _ysr_hi = 2.5
                break
        # 2026-09-21 用户指令①: 3D 门不改几何 —— 抬 Dsi 属几何动作, 一并下放给 2D/1D 门。
        if (not _lock_layer) and _ysr > 0.9 * _ysr_hi:
            _add("increase_stator_id", "stator_id", 0.03,
                 f"轭厚/槽深比 {_ysr:.2f} 已逼近上限 {_ysr_hi:g}(>90%): 抬定子内径 Dsi 使"
                 f" hj=(Dso−Dsi)/2−Hs 下降, 与收槽高配对把 hj/hs 压回带内",
                 "tooth_flux/slot_area")
    elif _sf_high_fail:
        if _lock_layer:
            # P2 冲片锁定层处方 (用户): 槽满率过高 → 降匝数, 导线截面(线规×并绕)不变, 加长铁心
            _add("reduce_turns", "turns_per_coil", -0.06,
                 "槽满率过高(冲片锁定层): 降匝数, 导线截面不变", "torque_emf")
            _add("lengthen_core", "core_length", 0.05,
                 "槽满率过高(冲片锁定层): 加长铁心分摊, 不增槽面积", "cost/frame")
        else:
            # 降目标填充率 → slot_area_req = Z·a_cond/fill ↑ → 槽更大 → 实算 SF ↓
            _sf_hi = 0.0
            for _c in (qg_result or {}).get("checks", []) or []:
                if _c.get("name") in ("槽满率上限", "槽满率下限"):
                    try:
                        _sf_hi = float(_c.get("value") or 0.0)
                    except (TypeError, ValueError):
                        _sf_hi = 0.0
                    break
            # P8: 同向性仲裁同样作用于「压 SF」方向 (默认 -0.05 在震荡时折半)
            _d_sfa = -0.05
            _sc_h = _sf_osc_scale(_sf_hi, -1)
            if _sc_h < 1.0:
                _d_sfa = -0.05 * _sc_h
            _add("increase_slot_area", "slot_fill_target", _d_sfa,
                 "降低目标填充率以增大槽面积, 压回 SF 目标带"
                 + (" [P8 震荡抑制: 步长 ×%g]" % _sc_h if _sc_h < 1.0 else ""),
                 "tooth_flux/yoke_flux")
    if "温升" in joined:
        _add("reduce_turns", "turns_per_coil", -0.05,
             "减匝降铜面积→降铜耗/温升 (兼降 SF)", "torque_emf")
        _add("lengthen_core", "core_length", 0.05,
             "加长铁心降低电密/铜耗, 抑制温升", "cost/frame")
        # P7 (v4.02.93): 增并联支路数 —— 分流降单路热负荷, 不占槽满率
        if _PARALLEL_PATHS_ACTION:
            _add("increase_parallel_paths", "parallel_circuits", 1.0,
                 "温升过高: 增并联支路数分流降单路铜耗密度 (不占槽满率)", "torque_emf")
    if "电密" in joined:
        _add("reduce_current_density", "current_density", -0.10,
             "降低定子电密, 抑制 I²R 温升", "torque")
        # P2 冲片锁定层处方 (用户): 电密过高 → 加大导线截面(线规×并绕) + 减匝 + 加长铁心
        _add("increase_wire_gauge", "wire_diameter", 0.03,
             "电密过高: 加大导线截面(线规×并绕) 降 J", "slot_fill_up")
        # P7 (v4.02.93): 增并联支路数 —— 分流降单路电流密度, 不占槽满率
        #   ★ 与 increase_wire_gauge 互补: 加粗线规会推高 SF, 并联支路不会。
        if _PARALLEL_PATHS_ACTION:
            _add("increase_parallel_paths", "parallel_circuits", 1.0,
                 "电密过高: 增并联支路数分流降单路电流密度 (不占槽满率)", "torque_emf")
        _add("reduce_turns", "turns_per_coil", -0.08,
             "电密过高: 减匝降电流需求 (与加粗线规成对)", "torque_emf")
        _add("lengthen_core", "core_length", 0.05,
             "电密过高: 加长铁心分摊铜耗", "cost/frame")
    if ("效率" in joined) and ("不足" in joined or "<" in joined):
        _add("lengthen_core", "core_length", 0.05,
             "加长铁心提升电磁效率", "cost/frame")
    # ── A (2026-09-18): 补全主因分支 ──
    # 实测门禁失败主因是"额定功率达标比 0.12""额定转矩达标比不足""支持报告: 无推荐方案",
    # 三者均不命中上述 4 类关键词 → 改进指令恒为空 → 重生只是重跑同款扫描 (卡死转人工)。
    # 现针对这些主因生成可执行指令:
    if "功率" in joined:
        # 欠激致额定功率不达标: 减匝 → 气隙磁密/安匝↑ → 功率↑
        _add("reduce_turns", "turns_per_coil", -0.12,
             "欠激致额定功率不达标: 减匝提气隙磁密与安匝, 抬升功率",
             "torque_curvature")
    # ★改造③ (2026-09-28 用户指令): 「转矩偏低」已降级为警告项 —— 删除由转矩文案
    #   触发的三个动作 (reduce_turns / lengthen_core / increase_wire_gauge)。
    #   RUN58 轮1 实证: 超尺寸场景下转矩不足的真因是磁密被稀释 (Dsi 超设计 +27%),
    #   加长铁心/加粗线规放大 A_p 与占槽, 属反向杠杆, 恰与纠正过设计的目标相反。
    #   崩溃级 (T≈0, _tr<0.5) 仍由判据侧 hard 阻断 (见 evaluate_quality_gate ★改造③),
    #   其重生指令由其它失败项 (功率/磁密/无推荐等) 分支承担。
    if "无推荐" in joined or "驳回" in joined:
        # 门禁检出无推荐方案: 整体减匝提磁密, 推动重生给出可达标候选
        _add("boost_excitation", "turns_per_coil", -0.10,
             "无达标方案: 整体减匝提磁密, 重生时给出可达标候选",
             "none")
    # ── S7 (2026-09-18): 线规钳顶越界 → 重生增并绕根数 ──
    if "线规" in joined or "并绕" in joined or "导线" in joined:
        _add("wire_resize", "parallel_wires", 0.0,
             "线径钳顶: 增并绕根数(降单根面积)回收线径至工艺带, 重生修复线规",
             "slot_fill_up")
    # ── FIX-B (2026-09-18): 过匝/欠激/崩溃转矩类主因 → 减匝 + 几何 EMF 目标修正 ──
    #   根因: 几何显著偏离标定参考机时 EMF 反算目标匝数系统性偏大 ⇒ 磁通被压低
    #   (RUN7 实测 z=10 → Bδ 0.349T / 230kW; z=9 → 0.651T / 450kW)。门禁原因文案
    #   形如「额定功率达标比 0.51」「最大转矩不足」「气隙磁密偏低」等, 须能触发
    #   生成侧动作, 否则重生只是重跑同款扫描 (卡死转人工)。
    if ("过匝" in joined or "欠激" in joined or "最大转矩" in joined
            or "崩溃" in joined or "气隙磁密" in joined or "磁密偏低" in joined):
        # ★★ 模态感知② (2026-09-28 用户授权): FIX-B 的减匝假设 (Bδ∝1/N) 仅在
        #   电压励磁模态成立; 恒J/电流源模态下 F=N·I∝N、Bδ∝N ⇒ 翻转为增匝
        #   (+0.15, 与电压侧 -0.15 对称)。否则与本轮气隙分支的增匝处方同轮对冲。
        if _resolve_em_mode(state) == "constant_j":
            _add("increase_turns", "turns_per_coil", 0.15,
                 "过匝/欠激(恒J模态): EMF 目标偏大压低磁密, 增匝提磁密 "
                 "(F=N·I∝N ⇒ Bδ∝N)", "slot_fill_up")
        else:
            _add("reduce_turns", "turns_per_coil", -0.15,
                 "过匝/欠激: EMF 反算目标偏大压低气隙磁密与崩溃转矩, 减匝提磁密",
                 "slot_fill_up")
        # 几何偏离标定参考机时, 一并要求重生改用几何自适应 EMF 目标
        # (绕开异步参考机单点标定增益的跨机型错配)
        _add("emf_geo_correct", "air_gap_flux_density", 0.0,
             "启用几何自适应 EMF 目标, 消除标定增益跨机型错配", "none")
        # P2 冲片锁定层处方 (用户): 气隙磁密偏低 → 加大导线截面(线规×并绕), 铁心长不变
        # ★ P1-2 (2026-09-22 用户指令): 步长自适应 —— SF 缺口 (SF ∝ 1/J) 与达标缺口取大,
        #   钳 [5%, 25%]。
        _add("increase_wire_gauge", "wire_diameter",
             wire_gauge_step(sf_cur=_current_slot_fill(qg_result),
                             sf_tgt=_slot_fill_band()[0],
                             gap=_achieve_gap(qg_result)),
             "气隙磁密偏低: 加大导线截面(线规×并绕) 提安匝裕度, 铁心长不变 "
             "(步长按 SF/达标缺口反解)", "slot_fill_up")
    # ── G2 (v4.02.92): 磁密类失败 → 定子内径(Dsi)动作 ──
    #   仅当门禁磁密失败(_mag_failed)时下发, 避免 3D 类失败误改几何。
    #   物理依据(motor_tools:4285-4288): By∝1/(Dso-Dsi)→减Dsi加厚轭降By;
    #   槽面积∝−Dsi→减Dsi放大槽面积降J/SF; Bt 与 Dsi 解耦→Bt异常不动Dsi。
    #   ★ _mag_failed 与 mode 无关 (warning 下磁密检查 passed=True 但 _mag_fail 仍按越界置位),
    #     故 warning 观察态下本段仍会按 _mag_flags 方向发几何动作 (RUN13 实证)。
    # ── 2026-09-20: 提磁密步长按缺口自适应 ──────────────────────────────
    #   固定 -10%/轮 的减匝步幅实测不够 (RUN22: 两轮 7→6→5, Bt 仅 1.108→1.39,
    #   仍差 0.11T 到 1.5 下限, 而 max_redo=2 已耗尽 ⇒ 转人工)。
    #   物理: Bδ ∝ 1/N ⇒ 需把 Bδ 提到 need 倍, 则匝数降到 1/need。
    #   need 由「当前值 → 带中心」的缺口算, 再用「不得越过带上界」封顶,
    #   最后钳 [1.10, 1.60] (防止单轮过冲跨带震荡)。
    _flux_up, _turn_dn = 0.12, -0.10        # 缺省 (无实测值时)
    _turn_up = 0.10                          # 缺省: 恒J模态增匝步幅 (★模态感知② 2026-09-28)
    _tooth_dn = -0.06                       # 缺省: 窄齿(降 bt ⇒ Bt↑)
    # ★ P1-1 (2026-09-22): Bδ 的**绝对**步长 (T) 缺省 0 —— ⚠ 注意: 执行器
    #   (`orchestrator.py:5606-5618`) **只在 `delta_t is None` 时**才回退相对换算,
    #   显式 0.0 会被判为「无需变更」⇒ 空动作。故 `_add` 已统一摘除 0/None 的
    #   `delta_t` 键 (见函数头部), 且下方另有无条件复算守卫, 双重保证不空转。
    _flux_up_t = 0.0
    _mv = (qg_result or {}).get("_mag_values") or {}
    _bt_v = float(_mv.get("tooth") or 0.0)
    _by_v = float(_mv.get("yoke") or 0.0)
    if _bt_v > 0 or _by_v > 0:
        # 带 (真源 = 门禁 checks 的 threshold, 避免硬编码与配置脱节)
        _bt_b = _by_b = None
        for _c in (qg_result or {}).get("checks", []) or []:
            if _c.get("name") == "定子齿磁密":
                _bt_b = _c.get("threshold")
            elif _c.get("name") == "定子轭磁密":
                _by_b = _c.get("threshold")

        def _mid(band):
            try:
                lo, hi = float(band[0]), float(band[1])
                return (lo + hi) / 2.0
            except (TypeError, ValueError, IndexError):
                return None

        _need = 1.0
        if _bt_v > 0 and _mid(_bt_b):
            _need = max(_need, _mid(_bt_b) / _bt_v)
            _need = min(_need, float(_bt_b[1]) / _bt_v)     # 不越过 Bt 上限
        if _by_v > 0 and _mid(_by_b):
            _need = max(_need, _mid(_by_b) / _by_v)
            _need = min(_need, float(_by_b[1]) / _by_v)     # 不越过 By 上限
        _need = max(1.0, min(1.60, _need))
        if _need > 1.0:
            # ★ P1-1 (2026-09-22 用户指令): Bδ 步长由**相对**改为**绝对** (单位 T)。
            #   缺口 = Bδ·(need−1); step = clamp(缺口, 0.01, 0.02) T。
            #   旧值 0.10~0.30 (相对) 在 Bδ≈0.64 时等效 +0.064~+0.19T ⇒ 一步跨出
            #   整条健康带 [0.55,0.85] (RUN40: 0.636 → 0.842)。绝对步长与可行域
            #   (≈0.019T) 同量级 ⇒ 单调收敛不跨带。
            #   同时把 `_flux_up` 改写为**等效相对步长** = step/Bδ, 使任何忽略 `delta_t`
            #   的旧消费路径也拿到同一(不跨带的)幅度, 而不是旧的 0.10~0.30。
            _bd_now = _current_air_gap_flux(qg_result)
            _gap_t = (_bd_now * (_need - 1.0)) if _bd_now > 0 else 0.0
            # ★ H30 (2026-09-26): 传入当前 Bδ 与判定带 —— 当抬高后仍落在带内时,
            #   放开 [0.01,0.02]T 钳位到缺口量级 (RUN53: 缺口 0.282T 却只发 0.02T)。
            _bd_lo0, _bd_hi0 = _bg_band_from_checks(qg_result)
            _flux_up_t = bg_abs_step(_gap_t, up=True,
                                     bd_now=_bd_now, band=(_bd_lo0, _bd_hi0))
            _flux_up = (round(_flux_up_t / _bd_now, 4) if _bd_now > 0
                        else round(max(0.10, min(0.30, _need - 1.0)), 4))
            _turn_dn = round(-max(0.10, min(0.30, 1.0 - 1.0 / _need)), 4)
            # ★ 模态感知② (2026-09-28 用户授权): 恒J模态下 F=N·I∝N ⇒ Bδ∝N, 提 Bδ
            #   需**增匝**: 精确解 delta = need−1 (N×(1+δ)=need ⇒ Bδ×need), 同钳
            #   [0.10,0.30] 防单轮过冲 (与 reduce_turns 的 [0.10,0.30] 对称)。
            _turn_up = round(max(0.10, min(0.30, _need - 1.0)), 4)
            # ★ 匝数是**整数量化**的: 5 匝只能 → 4 匝 (×1.25) 或 3 匝 (×1.67),
            #   不可能精确命中 need 倍 ⇒ 量化后必有残差。残差交给**齿宽杠杆**
            #   (Bt ∝ Bδ/bt): 残差 need_resid 由窄齿补齐, 而不是靠再减一匝
            #   (再减一匝会直接跨过 Bt 上限 1.8T ⇒ 震荡)。
            _n_now = 0
            try:
                _rs = ((state or {}).get("results") or [])
                _b0 = _rs[0] if _rs else ((state or {}).get("best_solution") or {})
                _n_now = int(_b0.get("turns_per_coil") or 0)
            except (TypeError, ValueError, IndexError, AttributeError):
                _n_now = 0
            if _n_now > 2:
                _n_new = max(2, int(round(_n_now / (1.0 + abs(_turn_dn)))))
                _gain_actual = _n_now / float(_n_new)
                if _gain_actual > 1.0:
                    _resid = max(1.0, _need / _gain_actual)
                    _tooth_dn = round(-max(0.02, min(0.18,
                                                    1.0 - 1.0 / _resid)), 4)
    _mag_failed = bool((qg_result or {}).get("_mag_failed"))
    # ★ 点②/④/⑤ (2026-09-27 用户指令): 归一化固定带判定的结构化方向。
    #   齿/轭**几何动作**分支 (narrow_tooth / shrink_yoke / decrease_stator_od /
    #   widen_tooth / deepen_yoke) 全部由**归一化方向** _mag_norm_flags 驱动;
    #   **励磁杠杆** (raise_air_gap_flux + reduce_turns, 点⑥) 仅由原始 airgap
    #   方向驱动。归一化判出几何问题而原始磁密在带内 (如 Bδ 偏高稀释) 时也要
    #   签发动作 ⇒ 闸门 = _mag_failed or 归一化几何失败。
    _mnf = ((qg_result or {}).get("_mag_norm_flags")
            or (qg_result or {}).get("_mag_flags") or {})   # 防御: 归一标志缺失回退原始方向
    _norm_geom_fail = any(v not in ("", "ok") for v in _mnf.values())
    if _mag_failed or _norm_geom_fail:
        # 读 quality_gate 输出的结构化方向 (避免对 "定子轭磁密 1.72T 过高(带 ..)" 这类
        #   含数字/单位的格式化文案做紧邻子串匹配 —— 那种写法恒不命中)。
        _mf = (qg_result or {}).get("_mag_flags") or {}
        # ★ 点② (2026-09-27): 归一化判据结果 (bt_norm/by_norm + 固定带 + gap_t/target_bd)。
        #   旧 P1-4「带回代抑制 narrow_tooth」(if not _bd_explains) 已删除 ——
        #   narrow_tooth 改由【归一化齿磁密偏低】直接驱动; RUN57 实证旧式
        #   bdelta_explains_deficit=30/30 True 把几何杠杆全数压制, 是不收敛主因之一。
        _mag_norm = (qg_result or {}).get("_mag_normalized") or {}
        # ★★ P1-1 守卫 (2026-09-22 收尾): 绝对步长必须**无条件**可用。
        #   上方 `_flux_up_t` 仅在「有 _mag_values 且 _need>1.0」时被赋值, 于是两种
        #   退化场景会让它停在 0.0, 而它被 raise_air_gap_flux 的**三条**调用点
        #   (轭低 L1989 / 齿低 L2012 / 气隙低 L2025) 共用:
        #     (a) `_mag_values` 缺失 (磁密门关闭/降级) ⇒ `_need` 恒 1.0;
        #     (b) 齿/轭均在带内而**只有气隙磁密偏低** ⇒ `_need` 由齿/轭算出亦为 1.0。
        #   两者都会让 Bδ 杠杆空转。此处按门禁自带字段独立复算, 只依赖有兜底的
        #   纯函数 (_current_air_gap_flux / _bg_band_from_checks / bg_abs_step),
        #   不引用上方可能未定义的分支局部变量 (避免 UnboundLocalError)。
        if _flux_up_t <= 0 or _flux_up <= 0:
            _bd_now = _current_air_gap_flux(qg_result)
            if _bd_now > 0:
                _bd_lo, _bd_hi = _bg_band_from_checks(qg_result)
                _g2 = float(_mag_norm.get("gap_t") or 0.0)
                if _g2 <= 0:
                    _g2 = max(0.0, _bd_lo - _bd_now)   # 至少抬到带下沿
                _t2 = bg_abs_step(_g2, bd_now=_bd_now, band=(_bd_lo, _bd_hi))
                if _t2 <= 0:
                    _t2 = BG_ABS_STEP_MIN_T            # 绝不落 0 (见 Fix1: 摘键会退化相对)
                _head2 = max(0.0, _bd_hi - _bd_now)    # 距带上限的余量
                if _head2 > 0:                          # 无余量 ⇒ 不强抬 (改为几何杠杆)
                    _flux_up_t = min(_t2, _head2)
                    _flux_up = round(_flux_up_t / _bd_now, 4)
        if _mnf.get("yoke") == "high":
            # ★ 点②/⑤ (2026-09-27): 归一化轭磁密偏高 = 轭部几何偏薄 ⇒ 加厚轭 / 加大外径。
            #   (原始 By 偏高但归一化在带内 = Bδ 偏高所致 ⇒ 不动几何, 由励磁侧自愈)
            # ★★ 项1 (2026-09-30 用户授权): **删除 decrease_stator_id** —— 该动作打错靶:
            #   hj 对 Dsi 弹性仅 +0.877 ⇒ Dsi −5% 时 hj 只 ×0.956 ⇒ By 仅 −0.6%,
            #   却付 D²L ∝ Dsi² 的 −9.75% 代价 (RUN63 实测 Dsi 被无辜打压
            #   170→161.5→153.4, 累计 −9.8%)。改由 deepen_yoke + increase_outer_dia 承担。
            # P4 (v4.02.93): 增定子轭宽 (轭厚系数↑, 不增外径, 体积代价最小; 用户诉求②)
            #   ★ 点⑥-b′ (2026-09-29 用户授权): 缺口反解 —— 与 shrink_yoke (yoke-low,
            #   点⑤ :2356-2367) **对称**: 同 Bδ 下 By ∝ 1/hj ⇒ hj_new = hj·(By′/By_hi)
            #   ⇒ delta_rel = By′/By_hi − 1 (正向), 钳 [+0.02, +0.40]。
            #   旧固定 +0.08 在 By 需 hj×1.35+ 时杯水车薪 (RUN62 实测 9 轮 2 次签发
            #   均不足), 缺 normalize 数据时回退 +0.08 (旧行为零回归)。
            _yk_up = 0.08
            _yk_up_raw = 0.0      # 未钳位的原始缺口 (供下方 increase_outer_dia 接力)
            _yk_up_note = ""
            try:
                _yk_n = float(_mag_norm.get("by_norm") or 0)
                _yk_h = float((_mag_norm.get("by_norm_band") or [0, 0])[1])
                if _yk_n > 0 and _yk_h > 0 and _yk_n > _yk_h:
                    _yk_up_raw = _yk_n / _yk_h - 1.0
                    _yk_up = round(max(0.02, min(0.40, _yk_up_raw)), 4)
                    _yk_up_note = (" [点⑥-b′ 缺口反解(归一口径): By′ %.2f→%.2fT ⇒ hj×%.3f]"
                                   % (_yk_n, _yk_h, 1.0 + _yk_up))
            except Exception:
                pass
            _add("deepen_yoke", "yoke_thickness", _yk_up,
                 "轭磁密过高(归一化判定, 几何): 增定子轭宽(轭厚系数↑) 降 By, 不增外径"
                 + _yk_up_note, "tooth_flux")
            # ★★ 项1 接力 (2026-09-30 用户授权): deepen_yoke 撞钳位上限 (0.40) 或撞按极数
            #   分档的系数带上界时, 轭厚已无空间 ⇒ 由**加大外径**补齐剩余缺口。
            #   hj = (Dso−Dsi)/2·c, c 拉满后只剩径向空间 R 可动:
            #     R_new = R × (1+raw)/(1+clamped)  ⇒  Dso_new = Dsi + 2·R_new
            #   delta 钳 [+0.02, +0.25] 防单轮过冲 (越界由下一轮 Dso/Dsi 判据兜底)。
            if _yk_up_raw > _yk_up + 1e-6:
                _od_up = round(max(0.02, min(0.25,
                                             (1.0 + _yk_up_raw) / (1.0 + _yk_up) - 1.0)), 4)
                _add("increase_outer_dia", "stator_od", _od_up,
                     "轭磁密过高: deepen_yoke 缺口 %.1f%% 超钳位上限 %.1f%% ⇒ 加大外径 "
                     "放大径向空间补剩余 %.1f%% (轭厚系数带已拉满, 仅剩 R 可动)"
                     % (_yk_up_raw * 100.0, _yk_up * 100.0, _od_up * 100.0),
                     "od_min_objective")
        if _mnf.get("yoke") == "low":
            # ★ 点⑤ (2026-09-27 用户指令): 归一化轭磁密偏低 = 轭部几何偏厚 ⇒
            #   **减小定子外径 / 减小轭宽** (By ∝ Bδ/hj, hj↓ ⇒ By↑)。
            #   【点③】旧「raise_air_gap_flux + reduce_turns」Bδ 杠杆已删除 ——
            #   提 Bδ 属励磁侧, 由点⑥ airgap 分支独立承担; 齿/轭偏低一律走几何杠杆。
            _add("increase_stator_id", "stator_id", 0.05,
                 "轭磁密过低(归一化判定, 几何): 增定子内径减薄轭部省铁", "none")
            # 主杠杆: 减小轭宽 (轭厚系数↓, deepen_yoke 的反向), 幅度由缺口反解:
            #   同 Bδ 下 By ∝ 1/hj ⇒ hj_new = hj·(By′_cur/By_lo)
            #     ⇒ delta_rel = By′/By_lo − 1 (负向), 钳 [-0.40, -0.02]。
            _yk_dn = -0.06
            _yk_note = ""
            try:
                _yk_norm = float(_mag_norm.get("by_norm") or 0)
                _yk_lo = float((_mag_norm.get("by_norm_band") or [0, 0])[0])
                if _yk_norm > 0 and _yk_lo > 0 and _yk_norm < _yk_lo:
                    _yk_dn = round(max(-0.40, min(-0.02, _yk_norm / _yk_lo - 1.0)), 4)
                    _yk_note = (" [点⑤ 缺口反解(归一口径): By′ %.2f→%.2fT ⇒ hj×%.3f]"
                                % (_yk_norm, _yk_lo, 1.0 + _yk_dn))
            except Exception:
                pass
            _add("shrink_yoke", "yoke_thickness", _yk_dn,
                 "轭磁密过低(归一化判定, 几何): 减小轭宽 hj↓⇒By↑ (By∝Bδ/hj)" + _yk_note,
                 "yoke_flux")
            # 辅杠杆: 减小定子外径 (Dso↓ ⇒ 轭部变薄 ⇒ By↑); 受 Dso/Dsi 比约束取小步长。
            _add("decrease_stator_od", "stator_od", -0.02,
                 "轭磁密过低(归一化判定, 几何): 减小定子外径 Dso↓⇒轭薄⇒By↑ "
                 "(受 Dso/Dsi 比约束, 小步长)", "yoke_flux")
            if _mnf.get("tooth") != "low":   # 同旋钮 (slot_fill_target), 避免与 narrow_tooth 双计数
                _add("increase_slot_area", "slot_fill_target", -0.05,
                     "轭磁密过低(归一化判定): 放大槽面积(槽更深⇒轭更薄⇒By↑)", "tooth_flux")
        # P4 (v4.02.93): 齿磁密方向化几何动作 (Bt 只由 bt/t 决定, Dsi/Qs 无效)
        # ★ 点④ (2026-09-27 用户指令): 由【归一化】齿磁密方向驱动 —— 投影到参考 Bδ
        #   后仍偏低 ⇒ 齿部几何偏宽 ⇒ 减齿宽 (narrow_tooth)。旧 P1-4「带回代抑制」
        #   (if not _bd_explains) 删除: RUN57 实证其 30/30 误抑制几何杠杆。
        if _mnf.get("tooth") == "low":
            # ── ★ J3 (2026-09-24 用户指令; ★M1 物理口径修订 2026-10-12 用户授权) ──
            #   反解: 同 Bδ 下 Bt ∝ 1/tw ⇒ tw_new = tw_cur·(Bt_cur/Bt_lo)
            #     ⇒ delta_rel = Bt/Bt_lo − 1 (负向, 物理缺口), 钳 [-0.40, -0.02]。
            #   ★ M1: Bt 取【物理实测值】(门禁 checks 的「定子齿磁密」value), 不再用
            #     归一化投影 Bt′=Bt·0.70/Bδ。根因 (《Bt出带根因分析_20261012》):
            #     归一口径 delta=Bt′/1.4−1 = Bt/(2·Bδ)−1 ⇒ 收敛 Bt=2.0·Bδ,
            #     低 Bδ 场景 (Bδ<0.70) 恒出带。物理口径 delta=Bt/1.4−1 ⇒
            #     tw_new=tw·(Bt/1.4) ⇒ Bt_new=Bt/(Bt/1.4)=1.40 (带下沿, 与 Bδ 无关)
            #     ⇒ Bt 直接进带, bt/τs 自然落到 f_emp·Bδ/1.40 ≈ 35~40% 区。
            #   数据源 = 门禁 checks 结构化字段, 不解析文案; 缺失回退旧 _tooth_dn (零回归)。
            _j3_dn, _j3_note = _tooth_dn, ""
            try:
                _j3_bt = _j3_bt_lo = _j3_btw = 0.0
                for _jc in (qg_result or {}).get("checks", []) or []:
                    if _jc.get("name") == "定子齿磁密":
                        _j3_bt = float(_jc.get("value") or 0)
                        _jb = _jc.get("threshold")
                        if isinstance(_jb, (list, tuple)) and len(_jb) == 2:
                            _j3_bt_lo = float(_jb[0])
                    elif _jc.get("name") == "齿宽范围":
                        _j3_btw = float((_jc.get("value") or {}).get("bt_mm", 0) or 0)
                # ★ M1 (2026-10-12): 物理口径 —— 直接用实测 Bt 反解步长 (不再用 bt_norm)
                _j3_use = float(_j3_bt) or float(_mag_norm.get("bt_norm") or 0)
                if _j3_use > 0 and _j3_bt_lo > 0 and _j3_btw > 0 and _j3_use < _j3_bt_lo:
                    _j3_rel = _j3_use / _j3_bt_lo - 1.0      # <0 (物理缺口比例)
                    _j3_dn = round(max(-0.40, min(-0.02, _j3_rel)), 4)
                    _j3_note = (" [J3 缺口反解(M1物理口径): Bt %.2f→%.2fT ⇒ tw×%.3f]"
                                % (_j3_use, _j3_bt_lo, 1.0 + _j3_dn))
            except Exception:
                _j3_dn = _tooth_dn
            _add("narrow_tooth", "tooth_width", _j3_dn,
                 "齿磁密过低(归一化判定, 几何): 减齿宽提高 Bt (Bt 只由 bt/t 定)" + _j3_note, "slot_area")
            # ── ★ G2 (2026-10-11 用户授权, QG_BD_LEVER_FALLBACK 默认 1=启用) ──
            #   立项取证 (RUN70 九轮): corr(Bt,Bd)=+0.881 而 corr(Bt,Dso)=+0.009 ⇒
            #   齿磁密由**气隙磁密**主控; 而齿宽杠杆常已触机械下限 (J4-R1), 此时
            #   narrow_tooth 要么被闸门拦、要么被 S3 同轴让位 ⇒ Bt 永不进带。
            #   故: 齿宽余量不足 (几何杠杆触底) 时, **必须**补一个 Bδ 杠杆 ——
            #   恒 J 模态 Bδ∝N ⇒ increase_turns; 电压模态 Bδ∝1/N ⇒ reduce_turns。
            #   幅度按缺口反解: Bδ* = Bt_lo / k_bt, k_bt = Bt/Bδ 实测几何因子;
            #   delta = Bδ*/Bδ − 1, 钳 [0.02, QG_BD_LEVER_MAX]。单动作, 不叠加。
            if QG_BD_LEVER_FALLBACK:
                try:
                    _bt_v = _j3_bt or 0.0
                    _bd_v = 0.0
                    for _jc in (qg_result or {}).get("checks", []) or []:
                        if _jc.get("name") == "气隙磁密":
                            _bd_v = float(_jc.get("value") or 0)
                    # 齿宽余量: (bt − bt_min)/bt_min ≤ 10% ⇒ 几何杠杆已触底
                    _btm = 0.0
                    for _jc in (qg_result or {}).get("checks", []) or []:
                        if _jc.get("name") == "齿宽范围":
                            _btm = float((_jc.get("value") or {}).get("bt_mm", 0) or 0)
                            _btmin = float((_jc.get("value") or {}).get("bt_min", 0) or 0)
                            break
                    _tooth_exhausted = (_btm > 0 and _btmin > 0
                                        and _btm <= _btmin * (1.0 + QG_BD_TOOTH_TOL))
                    if _tooth_exhausted and _bd_v > 0 and _j3_bt_lo > 0 and _bt_v > 0:
                        _kbt = _bt_v / _bd_v          # 几何因子 (实测 ≈1.82)
                        _bd_star = _j3_bt_lo / _kbt  # 达标所需 Bδ
                        _d_rel = _bd_star / _bd_v - 1.0
                        if _d_rel > 0:
                            _d_rel = round(max(0.02, min(QG_BD_LEVER_MAX, _d_rel)), 4)
                            _mode = _resolve_em_mode(state)
                            if _mode == "constant_j":
                                _act, _prm, _dirn = "increase_turns", "turns_per_coil", "增匝"
                            else:
                                _act, _prm, _dirn = "reduce_turns", "turns_per_coil", "减匝"
                            _add(_act, _prm, _d_rel,
                                 "齿磁密过低且齿宽已触下限(几何杠杆用尽): "
                                 "%s提 Bδ (k_bt=%.2f ⇒ Bδ %.2f→%.2fT, Bδ∝%s) "
                                 "[G2 缺口反解]" % (_dirn, _kbt, _bd_v, _bd_v * (1 + _d_rel),
                                                  "N" if _mode == "constant_j" else "1/N"),
                                 "airgap_flux")
                except Exception:
                    pass
            # 【点③】(2026-09-27) 旧「raise_air_gap_flux + reduce_turns」Bδ 杠杆已删除:
            #   齿磁密偏低不再发减匝抬 Bδ —— 励磁侧由点⑥ airgap 分支独立承担。
            #   ⚠ narrow_tooth 与 increase_slot_area 是同一真实旋钮 (都走 slot_fill_target
            #   → 槽宽 w_eq → 齿宽 bt), 叠加会双计数 ⇒ 不另发 increase_slot_area。
            # ⚠ narrow_tooth 与 increase_slot_area **是同一个真实旋钮**
            #   (都走 slot_fill_target → 槽宽 w_eq → 齿宽 bt), 叠加会双计数 ⇒
            #   此处不再另发 increase_slot_area, 幅度已由 _tooth_dn 自适应给出。
            # ── ★ A (2026-09-22 用户指令③): 过设计诊断 ──────────────────────────
            #   齿磁密偏低「且」槽满率偏低「且」转矩/功率达标 ⇒ 定子冲片内径选大
            #   (大马拉小车) ⇒ **减定子内径 Dsi**: 缩小机座 ⇒ 比负荷↑ ⇒ Bδ↑⇒Bt↑,
            #   同时槽缩小 ⇒ 槽面积↓ ⇒ SF↑ (双偏低同步纠正)。
            #   ⚠ 此动作仅 1D 层 (_QG_TIER_ACTIONS["1d"]) 存活, 2D/3D 白名单不含
            #     stator_id 动作 ⇒ 自动在正确层级生效, 且覆盖 _qg_tier_plan 的 1D
            #     默认 increase_stator_id (避免方向反转, RUN41 实证齿磁密/槽满率双低
            #     却把 Dsi 越加越大)。
            if _sf_low_fail:
                # ★改造④ (2026-09-28 用户指令): 「齿磁密+槽满率双低且转矩达标」降级为
                #   警告项 —— 不再下发 decrease_stator_id (1D 层过设计处方)。
                #   诊断改记 qg_result["_warnings"] 供报告/审计读取, 不参与动作链。
                #   ⚠ 提示: 原注释称此动作"覆盖 _qg_tier_plan 的 1D 默认 increase_stator_id
                #   (避免方向反转, RUN41 实证)"; 删除后若 1D 层 SF 偏低, 方向由
                #   shrink_slot_keep_yoke 与 _qg_tier_plan 默认动作决定, 需在后续跑批
                #   观察是否复现 RUN41 反向 (超尺寸根因仍需治本 H19/H18 + 清案例库)。
                try:
                    (qg_result or {}).setdefault("_warnings", []).append(
                        "过设计观察(齿磁密+槽满率双低且转矩达标): 定子冲片内径选大 "
                        "(大马拉小车), 建议缩 Dsi 回标度律; ★改造④ 不自动下发 decrease_stator_id")
                except Exception:
                    pass
            # ── ★ C (2026-09-22 用户指令②): 3D 冲片锁定层 齿磁密偏低 ⇒ 补减铁长 ──
            #   3D 层 raise_air_gap_flux 被白名单滤掉, 仅剩 reduce_turns(兼降 SF),
            #   齿磁密修复力度不足; reduce_core_length 已在 3D 白名单内(此前仅注释未下发),
            #   L↓ ⇒ A_p=Dsi·L↓ ⇒ Bδ↑ ⇒ Bt↑ (纯磁密杠杆, 不碰槽形/齿宽)。
            if _lock_layer:
                _add("reduce_core_length", "core_length", -0.05,
                     "3D 门(冲片锁定层) 齿磁密偏低: 减铁长使 A_p↓⇒Bδ↑⇒Bt↑ "
                     "(Bt-only 杠杆, 不碰槽形/齿宽, 与 reduce_turns 协同抬 Bδ)",
                     "torque_emf")
        if _mnf.get("tooth") == "high":
            # ★ 点②/④ (2026-09-27): 归一化齿磁密偏高 = 齿部几何偏窄 ⇒ 加齿宽。
            #   (原始 Bt 偏高但归一化在带内 = Bδ 偏高所致 ⇒ 不动几何, 由励磁侧自愈)
            _add("widen_tooth", "tooth_width", 0.06,
                 "齿磁密过高(归一化判定, 近饱和): 加齿宽降 Bt", "slot_area")
        # 气隙磁密偏低 → 提 Bδ — ★ 点⑥ (2026-09-27 用户指令): Bδ 判定项已改为
        #   「警告不阻断 + 动作触发源」, 偏低时由此分支独立承担励磁侧修复
        #   (点③已删除齿/轭分支的重复 Bδ 杠杆, 避免双计数)。
        # ★★ 模态感知处方 (2026-09-28 用户授权②): 增/减匝方向取决于励磁模态 ——
        #   voltage (默认; 2D 已按项④ _precompute_u_rated 预计算 U_rated, 设计端
        #   与求解端一致) ⇒ 电压励磁 Bδ∝1/N ⇒ **减匝**;
        #   constant_j (EM_LOAD_CONSTANT_J=1, 恒J/电流源改写) ⇒ F=N·I∝N、Bδ∝N
        #   ⇒ **增匝**。模态真源 = 设计字典 _em_mode (builder 端 _precompute_u_rated
        #   落标), 缺失回退环境开关, 再回退 voltage (行为与历史一致, 零回归)。
        if _mf.get("airgap") == "low":
            _em_mode_v = _resolve_em_mode(state)
            if _em_mode_v == "constant_j":
                _add("raise_air_gap_flux", "air_gap_flux_density", _flux_up,
                     "气隙磁密过低: 提 Bδ 目标(恒J·增匝), Bt/By 同步抬升", "torque_emf",
                     delta_t=_flux_up_t)
                _add("increase_turns", "turns_per_coil", _turn_up,
                     "气隙磁密过低: 恒J模态增匝提 Bδ (F=N·I∝N ⇒ Bδ∝N)",
                     "torque_emf")
            else:
                _add("raise_air_gap_flux", "air_gap_flux_density", _flux_up,
                     "气隙磁密过低: 提 Bδ 目标(减匝), Bt/By 同步抬升", "torque_emf",
                     delta_t=_flux_up_t)
                _add("reduce_turns", "turns_per_coil", _turn_dn,
                     "气隙磁密过低: 减匝提 Bδ (Bδ∝1/N, U_rated 预计算后电压模态自洽)",
                     "torque_emf")
        # ★ 2026-09-30 用户指令: 删除「定子电密高」分支的 decrease_stator_id(减内径放大槽面积)。
        #   物理上「减定子内径」并不能降低定子电密 J(=I/(N·A_cond)), 且与 By 分支原
        #   decrease_stator_id 同属「打错靶」杠杆。高电密的**正确**纠偏由上方
        #   :2140 "电密" 分支统一下发 (reduce_current_density / increase_wire_gauge /
        #   increase_parallel_branches / reduce_turns / lengthen_core), 此处为冗余且错误的
        #   重复分支, 故整段删除, 不再下发任何 decrease_stator_id(电密)。
        # ★改造⑤ (2026-09-28 用户指令): λ 越界已降级为警告项 —— 删除 G5 回环动作下发。
        #   原 adjust_stator_id_length delta=0.0 本就是空头指令, 下发后对几何无实际
        #   控制力, 却参与 J4/P8b 仲裁与指令计数, 属「噪声处方」; 删除不影响真实纠偏。
        #   (_mag_flags["lambda"] 置位保留, 供报告/审计观察;
        #    orchestrator.py:5955 的 _resolve_dsi_for_length handler 随之下发侧失效,
        #    保留为死路径无副作用。)

    # ── ★ J4 (2026-09-24 用户指令): 同轮指令集方向自洽预检 ────────────────────
    #   必须放在 P8b 仲裁**之前** — 先滤掉方向矛盾 (narrow_tooth vs 齿宽下限) /
    #   软化反向幅度 (reduce_turns vs 功率达标不足), 仲裁看到的是已自洽的集合。
    #   结果记入 qg_result["_instr_preflight"] 供日志/报告审计 (异常绝不影响主流程)。
    try:
        _j4_alerts = _preflight_instruction_conflicts(instrs, qg_result)
        if isinstance(qg_result, dict) and _j4_alerts:
            qg_result["_instr_preflight"] = _j4_alerts
            for _ja in _j4_alerts:
                logger.info("[quality_gate][J4] %s: %s", _ja.get("rule"), _ja.get("reason"))
    except Exception as _j4e:
        logger.debug("[quality_gate][J4] 预检异常(跳过): %s", _j4e)

    # ── P8b (2026-09-21 用户指令 三.1): 槽满率**统一仲裁与冲突解决** ──────────
    #   必须放在所有分支动作都已 _add 之后 —— 只有到这一步才能看到同一轮里是否
    #   同时存在「抬 SF」与「压 SF」的动作 (它们来自互不可见的不同分支)。
    #   背景: P8 只把步长折半, 未解决方向对冲 ⇒ SF 0.573→0.618→0.619→0.434 永不入带。
    _sf_now = _current_slot_fill(qg_result)
    if not _sf_now:
        # checks 无槽满率项 (求解降级/字段缺失) ⇒ 退用上轮快照, 保证仲裁仍可判向
        try:
            _sf_now = float(((state or {}).get("prev_round_metrics") or {})
                            .get("slot_fill") or 0.0)
        except (TypeError, ValueError):
            _sf_now = 0.0
    try:
        _arb = arbitrate_sf_conflicts(instrs, _sf_now, _slot_fill_band(), _mf)
    except Exception as _ae:  # 仲裁异常绝不影响主流程
        _arb = {"verdict": "error:%s" % _ae}
    try:
        if isinstance(qg_result, dict):
            qg_result["_sf_arbitration"] = _arb
    except Exception:
        pass
    # ★ S3 (2026-10-10 用户授权): 同轴几何动作冲突消解 —— 返回前最后一道。
    #   仅当同轮同时存在 yoke 轴与 tooth 轴的几何动作时生效, 其余情况原样返回 (零回归)。
    #   异常绝不影响主流程 (fail-open, 旧行为)。
    try:
        _y0 = sum(1 for _i in instrs
                  if isinstance(_i, dict) and _i.get("_coaxial_yield"))
        instrs = _resolve_coaxial_conflict(instrs, qg_result, state)
        if isinstance(qg_result, dict):
            _y1 = sum(1 for _i in instrs
                      if isinstance(_i, dict) and _i.get("_coaxial_yield"))
            if _y1 or _y0:
                qg_result["_coaxial_deconflict"] = {
                    "n_yield": _y1,
                    "gaps": {"yoke": round(_coaxial_axis_gaps(qg_result)[0], 4),
                             "tooth": round(_coaxial_axis_gaps(qg_result)[1], 4)},
                }
    except Exception as _ce:  # pragma: no cover
        try:
            if isinstance(qg_result, dict):
                qg_result["_coaxial_deconflict"] = {"error": str(_ce)}
        except Exception:
            pass
    return instrs


def build_compromise_instructions(instructions: list, params_sample: dict = None,
                                  od_limit: float = None) -> list:
    """冲突检测 → 折中指令。

    对每条改进指令, 若其连锁约束冲突可由"补偿动作"抵消, 生成折中指令:
      increase_slot_area(槽面积↑挤占铁心) → increase_outer_dia(加大冲片外径, 吸收增量)
      reduce_turns(减匝→J↑)              → increase_wire_gauge(线径↑, 保持 J 不升)
    若样本 OD 已接近 od_limit, 则折中改写为"梨形槽优化(不增 OD)"。
    """
    comps: List[dict] = []
    _od = None
    try:
        _od = float((params_sample or {}).get("stator_od") or 0) or None
    except (TypeError, ValueError):
        _od = None
    _od_near_limit = bool(od_limit and _od and _od >= 0.97 * float(od_limit))

    for it in (instructions or []):
        act = it.get("action")
        if act == "increase_slot_area":
            if _od_near_limit:
                comps.append({"action": "pear_slot_optimize", "param": "slot_type",
                              "delta": 0.0, "reason": "OD 已近上限, 改梨形槽提面积利用率(不增 OD)",
                              "conflict": "none"})
            else:
                comps.append({"action": "increase_outer_dia", "param": "stator_od",
                              "delta": 0.03, "reason": "加大冲片外径吸收槽面积增量, 保齿/轭磁密",
                              "conflict": "od_min_objective"})
        elif act == "reduce_turns":
            comps.append({"action": "increase_wire_gauge", "param": "wire_diameter",
                          "delta": 0.03, "reason": "减匝同时加大线规, 保持电密 J 不升",
                          "conflict": "slot_fill_up"})
    return comps


def evaluate_measure_effect(prev_metrics: dict, curr_metrics: dict,
                            band: tuple = None, temp_max: float = 120.0) -> dict:
    """再次评价：量化"改进措施效果" — Δ 指标 + 有效性/折中达成/新冲突判定。

    返回 {deltas, table, sf_in_band, sf_improved, temp_ok, new_conflict, effective, verdict}
      table 行: [指标, 上轮, 本轮, Δ, 判定]
    """
    lo, hi = (band or _SF_TARGET_BAND)
    prev_metrics = prev_metrics or {}
    curr_metrics = curr_metrics or {}
    # ★ H29: 崩溃标记优先取显式键 (extract_round_metrics 已输出), 回退按值自判 ——
    #   任何来源的 >100% 值在此都得到与门禁一致的解读, 口径唯一。
    _sf_crash = bool(curr_metrics.get("slot_fill_crashed")
                     or prev_metrics.get("slot_fill_crashed"))
    _c_raw = curr_metrics.get("slot_fill")
    if not _sf_crash and _c_raw is not None:
        try:
            _sf_crash = float(_c_raw) > SF_CRASH_TH
        except (TypeError, ValueError):
            _sf_crash = False
    keys = [
        ("slot_fill", "槽满率", None),
        ("max_temp", "温升(K)", None),
        ("stator_od", "外径(mm)", None),
        ("efficiency", "效率(%)", None),
    ]
    table = []
    deltas = {}
    for k, label, _ in keys:
        p, c = prev_metrics.get(k), curr_metrics.get(k)
        if p is None or c is None:
            table.append([label, _fmt(p), _fmt(c), "—", "缺数据"])
            continue
        d = round(c - p, 4)
        deltas[k] = d
        verdict = "—"
        if k == "slot_fill":
            if _sf_crash:
                # ★ H29: 与门禁 `_normalize_slot_fill` 同源 —— 崩溃值 (>100% 物理
                #   不可能, 根因=上游几何喂参失衡) **绝不判『偏低』**, 否则会下发
                #   shrink_slot_keep_yoke (减槽高抬 SF) 反向错处方 (RUN49-P2 裁定)。
                verdict = "崩溃值(>100%): 上游几何失衡, 非『偏低』"
            elif c <= hi:
                verdict = "已入合格带" if c >= lo else "偏低"
            elif d < 0:
                verdict = "下降但未达标"
            else:
                verdict = "未改善"
        elif k == "max_temp":
            verdict = "达标" if c <= temp_max else ("改善中" if d < 0 else "未改善")
        elif k == "efficiency":
            verdict = "提升" if d > 0.01 else ("下降" if d < -0.01 else "持平")
        elif k == "stator_od":
            verdict = "增大(折中代价)" if d > 0.5 else ("减小" if d < -0.5 else "持平")
        table.append([label, _fmt(p), _fmt(c), f"{d:+g}", verdict])

    c_sf = curr_metrics.get("slot_fill")
    p_sf = prev_metrics.get("slot_fill")
    sf_in_band = bool(not _sf_crash and c_sf is not None and lo <= c_sf <= hi)
    sf_improved = bool(not _sf_crash and c_sf is not None and p_sf is not None and c_sf < p_sf)
    c_temp = curr_metrics.get("max_temp")
    temp_ok = bool(c_temp is None or c_temp <= temp_max)
    # 新冲突: 槽满率改善但温升/外径显著恶化
    new_conflict = bool((not _sf_crash) and sf_improved and (
        (c_temp is not None and prev_metrics.get("max_temp") is not None
         and c_temp - prev_metrics["max_temp"] > 10)
        or (curr_metrics.get("stator_od") is not None
            and prev_metrics.get("stator_od") is not None
            and curr_metrics["stator_od"] - prev_metrics["stator_od"] > 20)))
    effective = bool(sf_in_band and temp_ok and not new_conflict)
    verdict = ("措施未达预期: 槽满率崩溃值 >100% (上游几何喂参失衡: 线径/匝数 vs 槽面积), "
               "需放大槽面积/降铜量, **不得判为『偏低』减槽高**" if _sf_crash
               else ("措施有效: SF 已入合格带且无新冲突" if effective
                     else ("部分有效: 指标改善但折中代价偏高(见 Δ外径/温升)" if sf_improved
                           else "措施未达预期, 建议转人工裁决或更换改进方向")))
    return {"deltas": deltas, "table": table, "sf_in_band": sf_in_band,
            "sf_improved": sf_improved, "temp_ok": temp_ok,
            "new_conflict": new_conflict, "effective": effective, "verdict": verdict}


def _fmt(v):
    try:
        return f"{float(v):.3g}" if v is not None else "—"
    except (TypeError, ValueError):
        return "—"


# ══════════════════════════════════════════════════════════════════════════
# ★ H28 (2026-09-26 RUN53 取证): QG 失败项 ↔ fixed_keys 一致性闸门 (纯函数)
# --------------------------------------------------------------------------
#   机制缺陷: main_a / bypass_b1 的 `_physics.fixed_keys` 把种子的 (最多) 11 个
#     几何键声明为固定项, 生成期 `_main_a_make_design` 逐键 `d[k] = seed.get(k)`
#     **原样复制种子**。而 QM 的全部失败项 (Bt/By/SF/齿槽比/Dso-Dsi) 恰恰
#     全部依赖这些键 ⇒ 纠偏指令只能改当轮候选, **下一轮重生又被未修正的种子
#     复位** ⇒ 9 轮纠偏 = 从同一起点重复 9 次 (RUN53: Bt 净推进 +0.33T, 效率
#     四升四降, η 峰谷差 6.98pp —— 随机游走而非梯度下降)。
#   闸门语义: 每当「QG 失败判据」所需的可调几何键落在 fixed_keys 集合内时,
#     判定该失败项在**当前结构上不可解**, 必须由调用方解锁
#     (把上轮 best 的已纠偏几何回灌种子), 否则本轮预算等于空耗。
# ══════════════════════════════════════════════════════════════════════════
# 失败判据 → 该判据**真正依赖**的可调几何键 (按物理链, 非指令名)。
QG_FAIL_KEY_LEVERS = {
    "定子齿磁密":     ("tooth_width", "slots_stator", "stator_id"),
    "定子轭磁密":     ("yoke_thickness", "stator_od", "stator_id"),
    "齿槽比 ζ":       ("slots_stator", "tooth_width", "stator_id"),
    "槽满率上限":     ("slots_stator", "tooth_width", "stator_id"),
    "槽满率下限":     ("slots_stator", "tooth_width", "stator_id"),
    "槽满率异常":     ("slots_stator", "tooth_width", "stator_id"),
    "长径比 λ":       ("core_length", "stator_id"),
    "气隙磁密":       ("air_gap", "rotor_od", "stator_id"),
    "起动转矩倍数":   ("slots_rotor", "rotor_od"),
    "转子齿磁密":     ("slots_rotor", "rotor_od"),
    "转子轭磁密":     ("rotor_od", "shaft_dia"),
}


def failed_gate_names(qg_result: dict, hard_only: bool = True) -> list:
    """抽取本轮门禁**失败**判据名 (默认只取 passed=False 的硬失败)。"""
    out = []
    for _c in ((qg_result or {}).get("checks") or []):
        if not isinstance(_c, dict):
            continue
        if hard_only and _c.get("passed") is not False:
            continue
        _n = str(_c.get("name") or "").strip()
        if _n and _n not in out:
            out.append(_n)
    return out


def locked_geometry_blockade(qg_result: dict, fixed_keys, best: dict = None) -> dict:
    """H28: 判定本轮失败项是否被 fixed_keys 锁死, 并给出可回灌的解锁量。

    参数
      qg_result : 本轮质量门结果 (含 checks)
      fixed_keys: 该轨固定几何键集合 (_physics.fixed_keys)
      best      : 本轮最佳方案 (dict); 提供时才计算 patches

    返回 dict
      failed         : 失败判据名列表
      required_keys  : 这些判据所需的全部可调几何键
      blocked_keys   : required ∩ fixed_keys  (被锁死 ⇒ 本轮不可解)
      unlockable     : blocked ∩ (best 中取到有效值的键)
      patches        : {key: best_value} —— 建议回灌到种子的解锁量
      solvable       : bool —— 是否**没有**任何失败项被锁死
    """
    _fk = {str(k) for k in (fixed_keys or ()) if k}
    _failed = failed_gate_names(qg_result)
    _req, _blocked = [], []
    for _n in _failed:
        for _k in QG_FAIL_KEY_LEVERS.get(_n, ()):
            if _k not in _req:
                _req.append(_k)
            if _k in _fk and _k not in _blocked:
                _blocked.append(_k)
    _patches, _unlockable = {}, []
    if isinstance(best, dict) and _blocked:
        for _k in _blocked:
            _v = best.get(_k)
            try:
                _v = float(_v)
            except (TypeError, ValueError):
                continue
            if _v > 0:
                _patches[_k] = round(_v, 3)
                _unlockable.append(_k)
    return {"failed": _failed, "required_keys": _req, "blocked_keys": _blocked,
            "unlockable": _unlockable, "patches": _patches,
            "solvable": not _blocked}


def tighten_gate_config(cfg: Optional[dict], redo_count: int) -> Dict[str, Any]:
    """M5 — 渐进收紧门禁阈值: 重试 (rebirth) 时逐步抬高标准, 推动优化器收敛到可行方案。

    策略 (基于既有 regenerate 回路, 不新增计数器):
      redo_count <= 0 : 原样返回 (首次评估, 不收紧) — 但 M2 已默认开启
      redo_count >= 1 : 基础约束收紧 —— 效率下限 +3pp, 温升上限 -5K (仅当原阈值 > 0)
      redo_count >= 2 : 额外硬约束 (M2) 收严 —— 转矩达标比→1.0, 功率达标比→1.0, 定子电密→7.5 A/mm²

    返回深拷贝, 不修改入参。v4.02.22 起 extra_hard_constraints_enabled 默认开启,
    故首次评估(redo0)即已启用 M2, 此函数仅负责重试时进一步收严阈值。
    """
    import copy
    if not isinstance(cfg, dict):
        cfg = dict(DEFAULT_QUALITY_GATE)
    out = copy.deepcopy(cfg)
    redo = int(redo_count or 0)
    if redo <= 0:
        return out

    hc = out.setdefault("hard_constraints", {})
    eff_min = float(hc.get("efficiency_min", 0.0) or 0.0)
    if eff_min > 0:
        hc["efficiency_min"] = round(eff_min + 3.0, 2)          # 效率下限 +3pp
    temp = float(hc.get("temp_max", 0.0) or 0.0)
    if temp > 0:
        hc["temp_max"] = max(1.0, round(temp - 5.0, 1))         # 温升上限 -5K

    if redo >= 2:
        # 深度重试: 启用 M2 额外硬约束并收严, 倒逼优化器给出功率/转矩/电密合规方案
        # ★ P0-5 (2026-09-22): 这里的 1.0 是**阈值语义**(必须达到 100% 目标),
        #   相对容差 ACHIEVE_RATIO_TOL_DEFAULT 已在**比较处**统一施加
        #   (value >= ratio·(1−tol)) —— 单点施加, 避免与本处重复打折。
        out["extra_hard_constraints_enabled"] = True
        tr = float(hc.get("torque_min_ratio", 0.0) or 0.0)
        if tr <= 0:
            hc["torque_min_ratio"] = 1.0
        else:
            hc["torque_min_ratio"] = 1.0
        pw = float(hc.get("power_min_ratio", 0.0) or 0.0)
        hc["power_min_ratio"] = 1.0            # 深度重试进一步收严到 100% 功率达标 (容差见上)
        cd = float(hc.get("current_density_max", 0.0) or 0.0)
        if cd <= 0:
            hc["current_density_max"] = 7.5
        else:
            hc["current_density_max"] = 7.5
    return out


def solver_available_for_rebirth(status: dict, solve_summary: dict,
                                 enforce: bool = True) -> "tuple[bool, str]":
    """P6: 质量门禁「重生前」求解器可用性校验 (纯函数, 不依赖 motor_tools)。

    由调用方经 check_solver_status() 取得求解器状态 dict 后传入;
    solve_summary 取自 workflow state (orchestrator 在 solve 节点捕获的摘要)。

    返回 (available: bool, reason: str)。判定「不可用」的情形:
      1. effective_engine == 'simulation'
         —— RMxprt 与 Motor-CAD 均不可用, 仅模拟兜底, 重生也无法获得真实求解。
      2. effective_engine == 'rmxprt' 但 engines.rmxprt.available == False
         —— 配置要求 RMxprt 但 PyAEDT/AEDT 未就绪。
      3. 上一轮 L1 求解整批降级为模拟 (level1.rmxprt_count==0 且 level1.sim_count>0)
         且 RMxprt 当前仍不可用 —— 说明 RMxprt 整批超时/崩溃, 重生仍将超时。

    enforce=False 时直接放行 (True, '已禁用校验')。
    status 为空/缺键时保守放行 (True), 避免探针故障误拦截正常重生。
    """
    if not enforce:
        return (True, "已禁用求解器可用性校验")
    if not isinstance(status, dict) or not status:
        return (True, "求解器状态为空(保守放行)")

    engines = status.get("engines", {}) or {}
    rmxprt = engines.get("rmxprt", {}) or {}
    rmxprt_avail = bool(rmxprt.get("available"))
    eff = status.get("effective_engine", "simulation")

    # 情形 1: 无任何真实求解器
    if eff == "simulation":
        return (False, "RMxprt 与 Motor-CAD 均不可用, 仅模拟兜底, 重生无法获得真实求解")

    # 情形 2: 配置 RMxprt 但实际不可用
    if eff == "rmxprt" and not rmxprt_avail:
        return (False, "配置要求 RMxprt 但 PyAEDT/AEDT 未就绪(求解器不可用)")

    # 情形 3: 上一轮 L1 整批超时/降级为模拟且当前 RMxprt 仍不可用
    _l1 = (solve_summary or {}).get("level1") or {}
    _rmx = int(_l1.get("rmxprt_count", 0) or 0)
    _sim = int(_l1.get("sim_count", 0) or 0)
    if _rmx == 0 and _sim > 0 and not rmxprt_avail:
        return (False, f"上一轮 L1 RMxprt 整批超时/降级为模拟({_sim}组), 重生仍将超时")

    return (True, "求解器可用")


def quality_gate_summary_text(result: dict, redo_count: int = 0, max_redo: int = 2) -> str:
    """人类可读的质量门禁摘要 (用于 SSE 进度 / 报告 / 人工检查点说明)"""
    if result.get("passed"):
        return f"质量门禁通过 (置信度={result.get('confidence')}, 关卡={len(result.get('checks', []))})"
    lines = [f"质量门禁未通过 (第 {redo_count + 1}/{max_redo + 1} 次评估)"]
    for c in result.get("checks", []):
        if not c["passed"]:
            lines.append(f"  - {c['name']}: {c.get('note', '未通过')}")
    return "\n".join(lines)


# ─── 自测 ───
if __name__ == "__main__":
    def _t(name, cond):
        print(("PASS" if cond else "FAIL"), "-", name)

    # 用例1: 全部通过
    s1 = {"review_feedback": {"confidence": 0.9},
          "results": [{"efficiency": 92.0, "max_temp": 60.0, "slot_fill_rate": 0.7}],
          "con_report": "方案无明显冲突, 评审通过"}
    r1 = evaluate_quality_gate(s1)
    _t("用例1 全部通过", r1["passed"] is True)

    # 用例2: 置信度不足
    s2 = dict(s1, review_feedback={"confidence": 0.5})
    r2 = evaluate_quality_gate(s2)
    _t("用例2 置信度不足阻断", r2["passed"] is False and not r2["checks"][0]["passed"])

    # 用例3: 硬约束(温升)超限
    s3 = dict(s1, con_report="温升偏高")
    s3["results"] = [{"efficiency": 92.0, "max_temp": 150.0, "slot_fill_rate": 0.7}]
    cfg3 = dict(DEFAULT_QUALITY_GATE, hard_constraints={"efficiency_min": 0.0, "temp_max": 100.0, "slot_fill_max": 0.0})
    r3 = evaluate_quality_gate(s3, cfg=cfg3)
    _t("用例3 温升超限阻断", r3["passed"] is False and any(c["name"] == "温升上限" and not c["passed"] for c in r3["checks"]))

    # 用例3b: 按电机类型分级效率下限 (永磁同步采用更高下限 80%)
    s3b = dict(s1)
    s3b["results"] = [{"efficiency": 72.0, "max_temp": 60.0, "slot_fill_rate": 0.7}]
    s3b["specs"] = {"motor_type": "永磁同步电动机"}
    r3b = evaluate_quality_gate(s3b)
    _t("用例3b 永磁效率72<80下限阻断",
       r3b["passed"] is False and any(c["name"] == "效率下限" and not c["passed"] for c in r3b["checks"]))

    # 用例3c: 异步电机 78% >= 75% 下限 → 通过
    s3c = dict(s1)
    s3c["results"] = [{"efficiency": 78.0, "max_temp": 60.0, "slot_fill_rate": 0.7}]
    s3c["specs"] = {"motor_type": "三相异步电动机"}
    r3c = evaluate_quality_gate(s3c)
    _t("用例3c 异步效率78>=75通过", r3c["passed"] is True)

    # 用例4: 收敛轨严重冲突
    s4 = dict(s1, con_report="该方案存在严重冲突, 建议否决")
    r4 = evaluate_quality_gate(s4)
    _t("用例4 收敛轨致命关键词阻断", r4["passed"] is False and r4["fatal_conflicts"])

    # 用例4b: 否定语境不应误判 ("未检出严重冲突" 不算致命)
    s4b = dict(s1, con_report="本报告未检出严重冲突, 方案可接受")
    r4b = evaluate_quality_gate(s4b)
    _t("用例4b 否定语境不误判", r4b["passed"] is True and not r4b["fatal_conflicts"])

    # 用例5: 路由决策
    _t("用例5 首次未通过→regenerate", decide_path(r2, 0) == "regenerate")
    _t("用例5 达上限未通过→manual", decide_path(r2, 2) == "manual")
    _t("用例5 通过→pass", decide_path(r1, 0) == "pass")

    # 用例6: M1 推荐方案存在性 (默认开启) — 支持报告"无推荐方案"应判失败
    s6 = dict(s1)
    s6["review"] = "## 推荐方案\n无推荐方案。全部10个方案均有致命缺陷, 无法满足硬约束。"
    r6 = evaluate_quality_gate(s6)
    _t("用例6 M1 无推荐方案→门禁失败", (r6["passed"] is False) and (r6.get("no_recommendation") is True)
       and ("推荐方案存在性" in r6.get("fail_tags", [])))

    # 用例7: M1 已给出推荐 → 推荐维度通过 (其余关卡也满足)
    s7 = dict(s1)
    s7["review"] = "## 推荐方案\n推荐方案B(效率优先), 因其效率96.2%最高且温升合规。"
    r7 = evaluate_quality_gate(s7)
    _t("用例7 M1 已给推荐→推荐维度通过", (r7.get("no_recommendation") is False)
       and all(c["passed"] for c in r7["checks"]))

    # 用例8: M5 渐进收紧
    cfg_t0 = get_quality_gate_config()
    cfg_t1 = tighten_gate_config(cfg_t0, 1)
    _t("用例8 redo1 效率下限+3pp",
       abs(cfg_t1["hard_constraints"]["efficiency_min"] - (cfg_t0["hard_constraints"]["efficiency_min"] + 3.0)) < 1e-6)
    _t("用例8 redo1 温升上限-5K",
       abs(cfg_t1["hard_constraints"]["temp_max"] - (cfg_t0["hard_constraints"]["temp_max"] - 5.0)) < 1e-6)
    cfg_t2 = tighten_gate_config(cfg_t0, 2)
    _t("用例8 redo2 额外硬约束启用", cfg_t2.get("extra_hard_constraints_enabled") is True)
    _t("用例8 redo2 转矩达标比→1.0", abs(cfg_t2["hard_constraints"].get("torque_min_ratio", 0.0) - 1.0) < 1e-6)
    _t("用例8 redo2 定子电密→7.5", abs(cfg_t2["hard_constraints"].get("current_density_max", 0.0) - 7.5) < 1e-6)
    cfg_t0b = tighten_gate_config(cfg_t0, 0)
    _t("用例8 redo0 不收紧(等同默认)",
       cfg_t0b["hard_constraints"]["efficiency_min"] == cfg_t0["hard_constraints"]["efficiency_min"]
       and cfg_t0b.get("extra_hard_constraints_enabled") == cfg_t0.get("extra_hard_constraints_enabled"))

    # 用例9: M2 额外硬约束 (开启后) 生效
    s9 = {"review_feedback": {"confidence": 0.9},
          "results": [{"efficiency": 92.0, "max_temp": 60.0, "slot_fill_rate": 0.7,
                       "rated_torque_ratio": 0.90, "current_density": 9.0}],
          "con_report": "方案无明显冲突",
          "review": "## 推荐方案\n推荐方案A。"}
    cfg9 = dict(DEFAULT_QUALITY_GATE, extra_hard_constraints_enabled=True,
                hard_constraints=dict(DEFAULT_QUALITY_GATE["hard_constraints"],
                                      torque_min_ratio=0.95, current_density_max=8.0))
    r9 = evaluate_quality_gate(s9, cfg=cfg9)
    _tq9 = next((c for c in r9["checks"] if "额定转矩达标比" in (c.get("name") or "")), {})
    # ★改造③ (2026-09-28): 转矩比 0.90 ≥ 0.5 地板 ⇒ 降级为警告项 (passed=True/warning);
    #   门禁仍 overall False 由电密项承担。
    _t("用例9 M2 额外硬约束生效(电密失败; 转矩比降级为警告项★改造③)",
       (r9["passed"] is False)
       and (_tq9.get("passed") is True) and (_tq9.get("severity") == "warning")
       and any(("定子电密上限" in (c.get("name") or "")) and not c["passed"] for c in r9["checks"]))

    print("quality_gate self-test done.")
