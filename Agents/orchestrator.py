"""
LangGraph 多智能体编排器 - 双轨辩证制衡体系
放大系统 (评审推荐师) + 收敛系统 (逆向/现实/沙盘/魔鬼) 并行, 汇入决策门

工作流 (v1.03.01):
  START → parse_requirement → generate_designs
    → [fan-out]
        ├→ review_solutions (放大·支持报告)
        └→ convergence_review (收敛·反对报告, 内含4个收敛agent)
    → decision_gate (双报告必读, ready_for_confirm)
    → generate_report → END

  无结果时: generate_designs → generate_report (跳过辩证)

v1.02.02 修复: 并行节点不再返回 step/status (避免 InvalidUpdateError)
  - step 由 run_streaming 从 node_name 设置
  - errors 改为 Annotated[List, operator.add] 允许并行追加

v1.02.03 增强: 实时进度推送 (后台线程 + 队列)
  - step_start 事件: 节点开始前推送
  - progress 事件: 节点执行中通过回调推送子步骤进度
  - batch_solve 逐条推送求解进度 (3/10...)
"""
import logging
logger = logging.getLogger(__name__)

import sys
import os
import time
import json
import queue
import threading
import concurrent.futures as _cf
import operator
from typing import TypedDict, Optional, List, Annotated
from datetime import datetime

sys.path.insert(0, r"D:\MotorDesign\Agents")
sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")


def _qg_exp_int(name, default=0):
    """实验开关读取: 优先环境变量; 缺失时回退 .qg_experiment.json (文件缺=默认关, 零回归)。
    用途: MotorAI Manager 守护拉起的 Flask 不继承手动 export 的环境变量, 故用文件注入以保活。"""
    v = os.environ.get(name)
    if v in (None, ""):
        try:
            _p = os.path.join(r"D:\MotorDesign", ".qg_experiment.json")
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


from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from requirement_agent import RequirementAgent
from design_agent import DesignAgent
from ideation_agent import IdeationAgent
from review_agent import ReviewAgent
from report_agent import ReportAgent
from reverse_reviewer import ReverseReviewer
from reality_checker import RealityChecker
from sandbox_simulator import SandboxSimulator
from devils_advocate import DevilsAdvocate
import audit_logger

# v4.02.33 (L0-B 终选门控): 真实求解解优先于 L0 模拟解的分层排序
try:
    from final_selection import (
        final_selection_rank, select_best, count_real,
        is_real_solution, is_simulated,
    )
except Exception as _fs_imp:  # pragma: no cover - 极端降级
    # 内联副本, 避免模块缺失导致编排器崩溃 (终选逻辑不可失效)
    def is_real_solution(r):
        _m = (r.get("solve_mode") or "").lower()
        _pl = r.get("precision_level") or ""
        return (_m in ("rmxprt", "motorcad")) or (_pl in ("L1", "L2"))
    def is_simulated(r):
        _m = (r.get("solve_mode") or "").lower()
        _pl = r.get("precision_level") or ""
        return (_m == "simulation") or (_pl == "L0")
    def final_selection_rank(r):
        # I-2 (v4.04.0 修4): 与 Scripts/final_selection.py 同口径 —— 门禁标记进排序。
        _failed = bool(r.get("solve_failed")) or r.get("real_solve_ok") is False
        _real = is_real_solution(r)
        _sim = is_simulated(r)
        _tier = 2 if (_real and not _failed) else (1 if (_sim and not _real) else 0)
        # I3-1: 同层按多目标加权分排序 (降级副本仅效率兜底, 避免额外依赖)
        _eta = float(r.get("efficiency", 0) or 0)
        _pf = float(r.get("power_factor", 0) or 0)
        _pf = _pf if _pf > 0 else 0.85
        _tst = float(r.get("start_torque_pu", 0) or 0)
        _tst = _tst if _tst > 0 else 1.0
        _ws = 0.40 * (_eta / 100.0) + 0.25 * min(_pf, 1.0) + 0.20 * min(_tst, 2.5) / 2.5
        if _tier == 2:
            if r.get("physical_flagged"):
                _tier = 1
                _ws *= 0.40
            elif r.get("needs_attention"):
                _ws *= 0.80
        return (_tier, 100.0 * _ws)
    def select_best(results):
        if not results:
            return None
        return sorted(results, key=final_selection_rank, reverse=True)[0]
    def count_real(results):
        return sum(1 for r in (results or []) if is_real_solution(r))
    logger.warning("[orchestrator] final_selection 导入失败, 使用内联降级副本: %s", _fs_imp)


# motor_tools 直接导入 (用于 generate_designs 节点的进度回调)
from motor_tools import generate_param_matrix, batch_solve, get_last_summary, check_solver_status, _get_flywheel_design_count, _b3_full_domain_count
# P0/P1 (v4.02.67): 生成期槽满率预检 + 指令驱动几何重算
try:
    from motor_tools import (prescreen_slot_fill as _prescreen_sf,
                             _apply_em_load_geometry as _apply_em_geom,
                             normalize_air_gap_geometry as _norm_geom)
except Exception:  # pragma: no cover
    _prescreen_sf = _apply_em_geom = _norm_geom = None
# P7 (v4.02.93): 并联支路数覆写 —— 门禁「增并联支路数」动作目标的最近合法值
try:
    from motor_tools import kb5_nearest_legal_parallel_circuits as _par_legal
except Exception:  # pragma: no cover
    _par_legal = None
# P0-hj/hs (2026-09-19): 轭厚系数真源 (与 P5 轭厚/槽深比带**按极数分档**对齐:
#   2 极 hj/hs∈[2.0,3.5]⇔c∈[0.667,0.778]; 4/6 极+ hj/hs∈[1.0,2.5]⇔c∈[0.500,0.714])
try:
    from motor_tools import (_YOKE_COEF_DEFAULT, _YOKE_COEF_BAND,
                             yoke_coef_band_for as _yoke_band_for,
                             yoke_coef_default_for as _yoke_default_for)
except Exception:  # pragma: no cover
    _YOKE_COEF_DEFAULT, _YOKE_COEF_BAND = 0.545455, (0.5000, 0.714286)
    _yoke_band_for = _yoke_default_for = None

# ── P0-3 (2026-09-22): 槽深解绑助手 (motor_tools 为真源, 缺失时用同式兜底) ──────
try:
    from motor_tools import (slot_depth_derived_od as _slot_depth_od,
                             SLOT_RADIAL_CLEARANCE as _SLOT_CLR)
except Exception:  # pragma: no cover
    _SLOT_CLR = 0.5

    def _slot_depth_od(stator_id, hs2, yoke_thickness, clearance=None):
        _c = _SLOT_CLR if clearance is None else float(clearance)
        return float(stator_id) + 2.0 * (float(hs2) + float(yoke_thickness) + _c)


def _yoke_band(poles) -> tuple:
    """按极数分档的轭厚系数带 (motor_tools.yoke_coef_band_for; 缺失→常量兜底)。"""
    if _yoke_band_for is not None:
        try:
            return tuple(_yoke_band_for(poles))
        except Exception:
            pass
    try:
        return (0.666667, 0.777778) if max(2, int(round(float(poles)))) <= 2 else tuple(_YOKE_COEF_BAND)
    except (TypeError, ValueError):
        return tuple(_YOKE_COEF_BAND)


def _yoke_ratio_band(poles) -> tuple:
    """按极数分档的**轭厚/槽深比 hj/hs 带** (由 c 带换算 r = c/(1−c))。

    2 极 (2.0, 3.5) / 4 极及以上 (1.0, 2.5)。
    """
    _lo_c, _hi_c = _yoke_band(poles)

    def _r(c):
        c = max(1e-6, min(0.999999, float(c)))
        return c / (1.0 - c)
    return (_r(_lo_c), _r(_hi_c))


def _yoke_default(poles) -> float:
    """按极数分档的生成侧默认轭厚系数。"""
    if _yoke_default_for is not None:
        try:
            return float(_yoke_default_for(poles))
        except Exception:
            pass
    try:
        return 0.714286 if max(2, int(round(float(poles)))) <= 2 else float(_YOKE_COEF_DEFAULT)
    except (TypeError, ValueError):
        return float(_YOKE_COEF_DEFAULT)


# 2026-09-20 (磁密修复): 槽形失效器 —— 清掉候选自带的完整定子槽形尺寸,
#   使 motor_tools._apply_em_load_geometry 从「透传」分支退回「重算」分支。
#   ⚠ 根因: _has_complete_slot_geometry(d) 为真时 (motor_tools:1211) 槽形直接采用、
#   不重算 ⇒ 只改 slot_fill_target 的改进指令 (narrow_tooth / increase_slot_area)
#   对齿宽毫无影响 (RUN24 实证: 下发 narrow_tooth -0.156, bt 11.056 → 11.11 反增)。
_SLOT_GEOM_KEYS = ("Slot__Bs0", "Slot__Bs1", "Slot__Bs2",
                   "Slot__Hs0", "Slot__Hs1", "Slot__Hs2",
                   "Slot__ToothWidth", "Slot__ParallelTooth")


def _invalidate_slot_geometry(d: dict) -> None:
    """清除候选自带槽形尺寸, 强制下游按新 slot_fill_target 重算槽形/齿宽。"""
    if not isinstance(d, dict):
        return
    for _k in _SLOT_GEOM_KEYS:
        d.pop(_k, None)
    d["_slot_geometry_invalidated"] = True


def _sync_yoke_thickness(d: dict, coef=None) -> bool:
    """★★ 项1 (2026-09-30 用户授权): 几何动作后按当前轭厚系数**重推** yoke_thickness。

    背景: 旧实现中 increase_outer_dia / decrease_stator_od / *_stator_id / deepen_yoke
      只改 od/id 或只写 `_yoke_coef_override`, **从不重算 yoke_thickness** ⇒
      径向空间变了而 hj 不动 (定向跑批实测 hj 对径向空间弹性仅 0.577, 应 1.0),
      且 `_yoke_coef_override` 四轨全不消费 (RUN63_漏洞C定向跑批验证_20260930) ⇒
      这些几何动作全部空转, By 八轮不收敛。

    系数取值优先级: 显式 coef > d['_yoke_coef_override'] > 按极数分档默认 (与
      motor_tools._b3_specs_driven_make_design:4899 的 yt=(Dso−Dsi)/2·c 同式)。
    返回 True 表示已改写。
    """
    if not isinstance(d, dict):
        return False
    try:
        _sod = float(d.get("stator_od") or 0.0)
        _sid = float(d.get("stator_id") or 0.0)
        if not (_sod > _sid > 0):
            return False
        _c = coef
        if not _c:
            _c = float(d.get("_yoke_coef_override") or 0.0) or _yoke_default(
                d.get("poles") or d.get("pole_pairs") or 4)
        _yt = max(2.0, (_sod - _sid) / 2.0 * float(_c))
        d["yoke_thickness"] = round(_yt, 3)
        return True
    except (TypeError, ValueError):
        return False

# P0-3 (v4.02.81): 电磁负荷可达性门禁度量 (A·Bδ / A·J₁ / Bδ / A / J₁ 带内命中率)
try:
    from motor_tools import (check_em_load_bands as _check_em_bands,
                             LAST_EM_LOAD_GATE as _LAST_EM_GATE,
                             em_load_gate_bands as _em_gate_bands)
except Exception:  # pragma: no cover
    _check_em_bands = _em_gate_bands = None
    _LAST_EM_GATE = {}
from motor_tools import LAST_DIVERSITY_METRICS, LAST_FLYWHEEL_EXCLUDED
from motor_tools import quick_l0_estimate, retry_failed_solutions, select_top_with_quota, TOP_N_QUOTA
from motor_tools import make_ai_comment, dedup_results, compute_diversity_metrics
from evolution_engine import find_matching_templates
from operation_flywheel import (
    record_step_metric, record_project_summary, run_all_evolution,
    record_ui_interaction, record_report_metric,
)
from cost_tracker import TaskCostTracker
import human_checkpoint as hc
import json as _json

# v4.01.029: 运营质量指标 (人工修正率/返工率) — 可选导入, 异常不影响主流程
try:
    from quality_metrics import (
        record_decision_event as _qm_record,
        promote_corrections_to_questions as _qm_promote,
    )
    _QM_AVAILABLE = True
except Exception as _qm_err:
    _QM_AVAILABLE = False
    print(f"[orchestrator] quality_metrics 模块未加载: {_qm_err}")

# ═══════════════════════════════════════════════════════════════════════════
# v4.02.83: 混合漏斗四轨生成配额 (单一真源)
# ---------------------------------------------------------------------------
# 用户 2026-09-15 指定: 主线A=500, B1=1200, B2=1200, B3=2400
#   · 主线A (main_a) : 冲片几何全固定 + 磁路法缩放叠长
#   · 旁路B1/B2      : 共用 n_bypass (generate_bypass_schemes 的 per-track 目标数)
#   · 旁路B3         : 独立 b3_count (全域扫描网格均匀抽样)
# 支持环境变量覆盖, 无需改码即可调参 (进程级, 改后需重启 Flask):
#   MOTORAI_FUNNEL_MAIN_A / MOTORAI_FUNNEL_BYPASS / MOTORAI_FUNNEL_B3
# ⚠ 实测去重率 (55kW 样例, 5 种子, v4.02.83):
#   main_a 500→唯一 258 (51.6%) / B1 1200→唯一 347 (28.9%) /
#   B2 1200→唯一 356 (29.7%) / B3 2400→唯一 2399 (100.0%)
#   ⇒ B1/B2 受 _main_a_length_ratios 五档 l_ratio × turns3 × parallel2 循环限制,
#     增量多为重复; B3 走全域网格抽样, 增量全为真实新方案。
#   生成阶段总耗时 ~17.6s (纯 Python, 不含 L0/L1 求解)。
# ═══════════════════════════════════════════════════════════════════════════
def _funnel_quota(env_name: str, default: int) -> int:
    """读取漏斗配额环境变量 (非整数/负值 → 回落 default)。"""
    try:
        return max(0, int(float(os.environ.get(env_name, default))))
    except (TypeError, ValueError):
        return default


FUNNEL_MAIN_A = _funnel_quota("MOTORAI_FUNNEL_MAIN_A", 500)
FUNNEL_BYPASS = _funnel_quota("MOTORAI_FUNNEL_BYPASS", 1200)
FUNNEL_B3 = _funnel_quota("MOTORAI_FUNNEL_B3", 2400)

# v4.00.004: 集中化工作流配置 (并发/队列超时/检查点/智能体模型)
try:
    sys.path.insert(0, r"D:\MotorDesign\Core")
    from workflow_config import get_max_concurrent as _wc_get_max_concurrent
    from workflow_config import get_queue_timeout as _wc_get_queue_timeout
    from workflow_config import set_max_concurrent as _wc_set_max_concurrent
    _WC_AVAILABLE = True
except Exception as _wc_err:
    _WC_AVAILABLE = False
    print(f"[orchestrator] workflow_config 模块未加载: {_wc_err}")

# v4.04: 案例电机数据库 (可选导入)
try:
    from case_motor_db import (
        get_case_motor, create_case_motor, save_verification, update_case_motor
    )
    _CM_DB_OK = True
except Exception as _cm_err:
    _CM_DB_OK = False
    print(f"[orchestrator] 案例电机模块未加载: {_cm_err}")

# v4.01.037 (P0-1): 结构化评审回灌 — 替代"仅正则提取极数/功率"
try:
    from review_feedback_schema import (
        resolve_feedback as _rf_resolve,
        parse_review_feedback as _rf_parse,
        apply_review_feedback as _rf_apply,
        feedback_summary_text as _rf_summary,
        FEEDBACK_OUTPUT_INSTRUCTION as _RF_INSTRUCTION,
    )
    _RF_AVAILABLE = True
except Exception as _rf_err:
    _RF_AVAILABLE = False
    print(f"[orchestrator] review_feedback_schema 模块未加载: {_rf_err}")

# v4.01.038 (P0-2): 决策门质量门禁
try:
    from quality_gate import (
        evaluate_quality_gate as _qg_eval,
        decide_path as _qg_decide,
        quality_gate_summary_text as _qg_summary,
        get_quality_gate_config as _qg_config,
        solver_available_for_rebirth as _qg_solver_ok,
        tighten_gate_config as _qg_tighten,
        # P1/P2 (v4.02.67): 结构化改进指令 / 冲突折中 / 措施效果评价
        extract_round_metrics as _qg_round_metrics,
        build_improvement_instructions as _qg_build_instrs,
        build_compromise_instructions as _qg_build_comps,
        evaluate_measure_effect as _qg_eval_effect,
        # ★ P1-1/P1-2 (2026-09-22 用户指令): 步长真源常量 (与门禁同源, 单点定义)
        BG_ABS_STEP_MAX_T as _BG_STEP_MAX_T,
        BG_ABS_STEP_MIN_T as _BG_STEP_MIN_T,
        WIRE_GAUGE_STEP_MIN as _WG_STEP_MIN,
        WIRE_GAUGE_STEP_MAX as _WG_STEP_MAX,
    )
    _QG_AVAILABLE = True
except Exception as _qg_err:
    _QG_AVAILABLE = False
    print(f"[orchestrator] quality_gate 模块未加载: {_qg_err}")
    _qg_round_metrics = _qg_build_instrs = _qg_build_comps = _qg_eval_effect = None
    # ★ P1: 兜底常量 (与 quality_gate 同值), 防降级时 NameError
    _BG_STEP_MAX_T, _BG_STEP_MIN_T = 0.02, 0.01
    _WG_STEP_MIN, _WG_STEP_MAX = 0.05, 0.25

# ============================================================================
# 2026-09-21 用户指令: 质量门分层, 每层可反复 2 次, 总计 2³=8
# 2026-09-22 用户指令 (P0-1): 升级顺序由 3D→2D→1D **改为 2D→3D→1D**
# ----------------------------------------------------------------------------
# 语义分层 (越往右改动越"结构性", 越往左改动越"装配级"):
#   **2D 门 (冲片定形层)** —— 定冲片: 槽形/槽深/齿宽/轭厚/外径/Qs。
#        绕组按**归一化假定组** (SF=0.70 / J=5.0 / a=2 / L₀=1m / Z₀=1) 处理, 不参与搜索
#        ⇒ 2D 截面与绕组解耦 (见 motor_tools.NORMALIZATION_ASSUMPTIONS)。
#   **3D 门 (冲片锁定层)** —— 冲片已锁定, 只调**绕组**: 匝数 / 线规 / 并绕根数 /
#        并联支路 / 电密 / 铁心长。★ **绝不修改槽形尺寸** (槽宽/齿宽/槽深/外径/Dsi)。
#        唯一对 SF 有效且不碰槽形的杠杆 = **降电密** (increase_wire_gauge, 见 P0-6);
#        「增匝数 + 减小铁长」对 SF 弹性 ≈ 0 (motor_tools 槽满率守恒) 已弃用为等价处方。
#   **1D 门 (基准尺寸层)** —— 允许改**定子内圆 Dsi**, 其余尺寸按比例缩放;
#        改完**回 2D 流程起始** (design_ideation) 重走 2D→3D, 并清零 2D/3D 计数。
#
# 预算 (嵌套): 2D 层满 N 次 ⇒ 升 3D; 3D 层满 N 次 ⇒ 升 1D (清零 2D/3D 计数);
#   1D 触发 N 次后仍不过 ⇒ 转人工。总重生预算 N³ (另有硬兜底防死循环)。
#   ★ 2026-09-21 用户指令: 每层反复次数 3 → 2 (总预算 3³=27 → 2³=8)。
#   ★ 顺序理由 (报告 V2 第一章 / RUN40 实证): 单轮 |ΔSF| 2D 均值 0.2212 vs 3D 均值
#     0.001667 (弹性比 132.7:1), 旧顺序让 38% 预算空转在弹性≈0 的 3D 层; 且新顺序与
#     系列电机设计流程同构 (先定冲片 → 再配绕组 → 最后动机座)。本阶段无模具成本,
#     稀缺资源是**重生轮次** ⇒ 重杠杆优先。
# ============================================================================
_QG_TIER_BUDGET = 2              # 每层可反复次数 (2026-09-21 用户指令: 3→2)
# ★ 分层独立预算 (2026-10-11 用户授权, 零回归): 2D/3D/1D 各自经 env 或 .qg_experiment.json
#   覆盖 (QG_TIER_BUDGET_2D / _3D / _1D); 缺省均回落 _QG_TIER_BUDGET(=2) ⇒ 旧行为完全不变。
_QG_TB_2D = _qg_exp_int("QG_TIER_BUDGET_2D", _QG_TIER_BUDGET)
_QG_TB_3D = _qg_exp_int("QG_TIER_BUDGET_3D", _QG_TIER_BUDGET)
_QG_TB_1D = _qg_exp_int("QG_TIER_BUDGET_1D", _QG_TIER_BUDGET)
# 总重生硬上限: 取「默认 2³=8」与「分层之和」较大者, 缺省仍为 8 (零回归)。
_QG_TIER_TOTAL_CAP = max(_QG_TIER_BUDGET ** 3,
                         _QG_TB_2D + _QG_TB_3D + _QG_TB_1D)   # 默认 8; 3/3/2→8
_QG_TIER_START = "2d"            # ★ P0-1: 起点层 (原为 "3d")
_QG_TIER_ORDER = ("2d", "3d", "1d")         # ★ P0-1: 升级顺序 (原为 3d→2d→1d)
# ★ G1 (2026-10-11 用户授权): tier 路由与 fail_stage 对齐闸门 (默认 0=旧行为, 零回归)。
#   病根 (RUN70 九轮取证): fail_stage 恒为 "2d" (齿磁密/轭磁密/槽满率均属 2d 判据),
#   但 tier 在 2d/2d/3d/3d/1d/2d/3d/3d/3d 间跳转 ⇒ narrow_tooth 只在 tier=2d 的
#   轮 1、2 签发 (轮3/4/8/9 被 3d 槽形白名单剔除、轮5 被 1d 剔除、轮6 被 J4-R1
#   齿宽下限拦截) ⇒ 齿磁密的**唯一直接杠杆 9 轮只发出 2 次**, Bt 1.119~1.403 始终出带。
#   开启后: fail_stage 属 2d 集时, 当前层的允许动作集**并入 2d 白名单** (冲片可动)。
QG_TIER_FAILSTAGE_ALIGN = int(os.environ.get("QG_TIER_FAILSTAGE_ALIGN", "1") or 0)
#   fail_stage ⇒ 是否属「2d 集」(含 mixed: 2d 与其它层混合失败, 2d 判据仍在列)
_QG_FAILSTAGE_2D = {"2d", "mixed", "2d_mixed"}

# 各层**允许下发**的动作白名单 (越浅的层动作越"重")
_QG_TIER_ACTIONS = {
    "3d": {  # 冲片锁定层 —— 只调绕组/铁心长, 不动槽形
        "increase_turns", "reduce_turns", "increase_wire_gauge",
        "increase_wire_section", "wire_resize", "increase_parallel_paths",
        "reduce_current_density", "boost_excitation", "lengthen_core",
        "reduce_core_length",   # 2026-09-21 用户指令: 3D 门槽满率偏低的减铁长动作
    },
    "2d": {  # 几何层 —— 槽形/齿宽/轭厚/外径/磁密目标
        "shrink_slot_keep_yoke", "increase_slot_area", "narrow_tooth",
        "widen_tooth", "deepen_yoke", "increase_outer_dia",
        # ★ 点⑤ (2026-09-27 用户指令): 归一化轭磁密偏低 → 减小定子外径/减小轭宽
        "decrease_stator_od", "shrink_yoke",
        "raise_air_gap_flux", "emf_geo_correct", "pear_slot_optimize",
        # ★ P0-3 (2026-09-22): 槽深独立可调 (P0-2 解绑) ⇒ 新增「由缺口反解槽深」动作。
        #   它是**唯一** 对 SF 有精确自由度的几何纯杠杆 (槽宽由齿宽锚定 ⇒ 面积 ∝ hs2)。
        "set_slot_depth",
        "lengthen_core", "reduce_turns", "increase_turns", "increase_wire_gauge",
        # ↑ increase_turns (2026-09-28 模态感知②): 恒J模态气隙偏低发增匝, 2D 层放行
        #   (执行器 orchestrator:5870 既有, 带 +50% 单轮封顶; voltage 模态不发此动作,
        #    故白名单放行不改变历史行为)。
        "increase_wire_section", "wire_resize", "increase_parallel_paths",
        "reduce_current_density", "boost_excitation",
    },
    "1d": {  # 基准尺寸层 —— 定子内圆 (其余按比例缩放)
        "increase_stator_id", "decrease_stator_id", "adjust_stator_id_length",
    },
}


def _filter_instrs_by_tier(instrs, tier, fail_stage=None):
    """按门禁层过滤改进指令 —— 只保留本层被允许的动作。

    3D 层会滤掉全部槽形/几何动作 (shrink_slot_keep_yoke / increase_slot_area /
    narrow_tooth / widen_tooth / deepen_yoke / increase_outer_dia / *_stator_id),
    从而保证"冲片锁定层不修改槽形尺寸"。

    ★ G1 (2026-10-11 用户授权, QG_TIER_FAILSTAGE_ALIGN 默认 1=启用):
      当「当前层 ≠ 2d」但 fail_stage 属 2d 集时, 把 2d 白名单**并入**允许集。
      依据: 分层白名单的语义前提是「层 = 本轮要动的对象」; 若本轮失败判据落在
      2d 集 (磁密/齿宽/槽满率), 却按 3d/1d 锁定冲片 ⇒ 该失败项的直接杠杆被全部
      滤掉 (RUN70 九轮实证: narrow_tooth 仅 2/9 轮签发), 该层必然空转。
      ⚠ 只并入 2d (不并入 1d): Dsi 仍受 1D 层单值治理约束, 避免跨层越权。
    """
    allow = set(_QG_TIER_ACTIONS.get(str(tier or _QG_TIER_START).lower())
                or _QG_TIER_ACTIONS[_QG_TIER_START])
    _cur = str(tier or _QG_TIER_START).lower()
    if (QG_TIER_FAILSTAGE_ALIGN and _cur != "2d"
            and str(fail_stage or "").lower() in _QG_FAILSTAGE_2D):
        allow |= set(_QG_TIER_ACTIONS.get("2d") or ())
    return [it for it in (instrs or [])
            if isinstance(it, dict) and str(it.get("action") or "") in allow]


def _tier_substitute_instrs(instrs, tier):
    """★ 2026-09-21: 3D 层「几何处方 → 绕组等价处方」替换。

    根因 (RUN33 实测)：槽满率偏低的几何处方 `shrink_slot_keep_yoke` 被分层白名单滤掉后，
    3D 层**没有任何抬 SF 的杠杆**了 —— 只剩同轮的 `reduce_turns`（压 SF），下一轮 SF 更低。
    而门禁的 `_lock_layer` 是按 `fail_stage=="3d"` 判的，本例 `fail_stage=="2d"`
    （齿磁密 + 槽满率都归 2D 集）⇒ 绕组处方分支根本不触发。

    故在**过滤之前**做等价替换：只要本层不允许几何动作，就把几何处方换成装配级等价处方
      shrink_slot_keep_yoke / set_slot_depth (抬 SF) → increase_wire_gauge (降电密 ⇒ 抬 SF)
      increase_slot_area     (压 SF) → reduce_turns
    并按「3D 层只认槽满率方向」消解 增/减匝数 的直接冲突（保留抬 SF 方向）。
    ★ 2026-09-21 用户指令: 3D 门槽满率偏低处方由「增匝数 + 线规 + 并绕」改为「增匝数 + 减小铁长」。
    ★★ P0-6 (2026-09-22 用户指令): 再更正为 **降电密** —— RUN40 实测「增匝数 + 减小铁长」
       对 SF 弹性 ≈ 0（槽满率守恒, 见下），三组指令 SF 仅 +0.0025/+0.0000/+0.0025；
       而降电密 J↓ ⇒ A_cond↑ ⇒ SF↑ 是本层**唯一**有效且不碰槽形的杠杆。
    """
    if str(tier or "3d").lower() != "3d":
        return list(instrs or [])
    sub = []
    for it in (instrs or []):
        if not isinstance(it, dict):
            continue
        a = str(it.get("action") or "")
        if a in ("shrink_slot_keep_yoke", "set_slot_depth"):
            # ★ P0-6 (2026-09-22 用户指令): 3D 层 SF 处方由「增匝数 + 减小铁长」**更正为降电密**。
            #   反证据: 前者对 SF 弹性 ≈ 0 —— motor_tools.py:696-720「槽满率守恒」在匝数
            #     变化后按 d_wire ∝ sqrt(1/tpc) 重算线径 ⇒ Z·A_cond ≡ const; 且
            #     `reduce_core_length` 根本不进 SF 关系式。RUN40 实测三组增匝指令
            #     (本层第 1/2、2/2 次) SF 仅 +0.0025 / +0.0000 / +0.0025 ≈ 空转。
            #   正解: 降电密 → A_cond = (I/a)/J ↑ ⇒ SF = Z·A_cond/S_slot ↑ (真杠杆)。
            #   步长由 SF 缺口反解: SF ∝ 1/J ⇒ J_new = J·(SF_cur/SF_tgt),
            #     ⇒ 相对降幅 = 1 − SF_cur/SF_tgt = |门禁几何处方的 delta| (同一缺口量)。
            #     RUN40 实需 12~20%, 而旧 `increase_wire_gauge` 只给 3% ⇒ 杠杆对、力度不足。
            try:
                _gap = abs(float(it.get("delta") or 0.0))
            except (TypeError, ValueError):
                _gap = 0.0
            # ★ P1-2 (2026-09-22 用户指令): 步长**自适应** = clamp(SF 缺口, 5%, 25%)。
            #   缺口优先由指令自带的**实测 SF 对**反解 (SF ∝ 1/J ⇒ 1 − SF_cur/SF_tgt),
            #   否则退回几何处方 delta 的绝对值 (同一缺口量, 见 P0-3 反解式)。
            #   旧实现吃的是固定 3% ⇒ RUN40 需 12~20% 时力度严重不足。
            _sf_c = _sf_t = 0.0
            try:
                _sf_c = float(it.get("sf_cur") or 0.0)
                _sf_t = float(it.get("sf_tgt") or 0.0)
            except (TypeError, ValueError):
                _sf_c = _sf_t = 0.0
            if _sf_c > 0 and _sf_t > 0:
                _gap = max(_gap, 1.0 - _sf_c / _sf_t)
            if _gap <= 0:
                _gap = 0.12                     # 门禁未给缺口时的保守默认
            _gap = round(max(_WG_STEP_MIN, min(_WG_STEP_MAX, _gap)), 4)
            _note = ("; ⚠ SF 缺口 >60% ⇒ 单靠降电密会跌破 AJ 可行带, "
                     "应判为 2D 几何(槽深过大)问题并升级"
                     if abs(float(it.get("delta") or 0.0)) > 0.60 else "")
            # ★ 项3 (2026-09-30 用户授权): param 统一为 wire_diameter、delta 取正,
            #   与 quality_gate 的 4 处签发点口径一致 (旧: current_density + delta 取负)。
            #   执行器 (:5845) 按 act 分派、不读 param ⇒ 运行时行为不变, 仅统一口径。
            _sub_it = {"action": "increase_wire_gauge", "param": "wire_diameter",
                       "delta": _gap, "conflict": "slot_fill_up",
                       "reason": f"槽满率偏低(冲片锁定层): 加大线规 +{_gap:.0%} "
                                 f"(线径↑⇒电密 J↓⇒导体截面↑⇒占槽率↑) —— 冲片不动, "
                                 f"本层唯一对 SF 有效的杠杆{_note}"}
            if _sf_c > 0 and _sf_t > 0:
                # ★ P1-2: 实测 SF 对随指令下传 ⇒ 执行器可再次独立反解 (双保险)
                _sub_it["sf_cur"] = round(_sf_c, 4)
                _sub_it["sf_tgt"] = round(_sf_t, 4)
            sub.append(_sub_it)
        elif a == "increase_slot_area":
            sub.append({"action": "reduce_turns", "param": "turns_per_coil",
                        "delta": -0.05, "conflict": "slot_fill_down",
                        "reason": "槽满率过高(冲片锁定层): 降匝数压低占槽率, 不增槽面积"})
        else:
            sub.append(it)
    acts = {str(i.get("action") or "") for i in sub}
    if "increase_turns" in acts and "reduce_turns" in acts:
        sub = [i for i in sub if str(i.get("action") or "") != "reduce_turns"]
    return sub


def _default_1d_instr(all_instrs):
    """★ B (2026-09-22 用户指令): 1D 门默认处方**方向感知**。

    旧实现: 门禁未给出 Dsi 方向时, 无差别 `increase_stator_id +3%` (RUN41 实证:
      齿磁密/槽满率低+转矩达标 ⇒ 该默认把 Dsi 越加越大, 与"大马拉小车应减内径"相反)。
    新实现: 优先沿用本轮原始指令里已判定的 Dsi 方向; 若门禁已显式给出
      increase/decrease_stator_id, 默认直接沿用(不反拧); 若**无任何 Dsi 方向信号**,
      默认 `decrease_stator_id` (过设计是高功率实例最常见失败, 见 quality_gate 改进 A;
      欠设计由后续 narrow_tooth / raise_air_gap_flux 杠杆纠正)。

    ★★ S4 (2026-10-10 用户授权): 与 quality_gate 的删除裁定对齐 —— **不再默认减内径**。
      `decrease_stator_id` 已于 2026-09-30 被裁定「打错靶」并从 quality_gate 全量删除
      (quality_gate.py:2396-2399 / :2517 / :2568-2573):
        hj 对 Dsi 弹性仅 +0.877 ⇒ Dsi −5% 时 hj 只 ×0.956 ⇒ By 仅 −0.6%,
        却付 D²L ∝ Dsi² 的 −9.75% 代价 (RUN63 实测 Dsi 被无辜打压 170→161.5→153.4)。
      本函数此前在**无方向信号时恒发**该动作 ⇒ 跨文件口径分裂 (quality_gate 已删、
      orchestrator 仍默认发); 且它恰好打到**唯一有解轨** b3 上 (b3 fixed_keys=() ⇒
      不被方向②拦, 而 a/b1/b2 锁 stator_id 反而被拦 ⇒ 伤害面完全错位)。
      实跑 RUN71 的 1D 层 (event 421/809) 两轮均发 decrease_stator_id(−0.05),
      是 R4 的确定性证据。现改为: 无 Dsi 方向信号 ⇒ 返回空指令集
      (由下方 _1d_assembly_fallback 与审计日志承担, 见 S4)。
    """
    acts = {str(i.get("action") or "") for i in (all_instrs or []) if isinstance(i, dict)}
    if "decrease_stator_id" in acts:
        # ★ S4: 该动作已被裁定删除 —— 即便上游残留也**不再沿用**, 交装配级回退
        return []
    if "increase_stator_id" in acts:
        return [{"action": "increase_stator_id", "param": "stator_id", "delta": 0.05,
                 "conflict": "geometry",
                 "reason": "1D 门默认处方(方向感知): 沿用门禁已判定的加内径方向"}]
    # ★ S4: 无 Dsi 方向信号 ⇒ 不再默认减内径 (打错靶), 返回空集
    return []


# ★ S4 (2026-10-10 用户授权): 1D 层「无 Dsi 方向信号」时的装配级回退。
#   1D 白名单 (_QG_TIER_ACTIONS["1d"]) 只含 *_stator_id ⇒ 分轨指令经
#   _filter_instrs_by_tier(_ri, "1d") 后恒为空 ⇒ by_rail={} ⇒ 执行器回退广播;
#   而 S4 又把广播的 decrease_stator_id 默认撤掉 ⇒ 1D 层若无回退就是**空重生**
#   (浪费一轮预算)。故补一组**不碰冲片几何、全轨可达**的装配级动作 (绕组/铁心长/电密),
#   从本轮原始指令里挑 (缺口口径与门禁一致), 使 1D 层仍具改进力。
#   ⚠ 这些动作不在 1D 白名单内, 但 _qg_tier_plan 的 1D 分支直接赋值不再过滤 ⇒ 生效;
#     且它们不写固定键空间 ⇒ 方向② (orchestrator:6010) 不拦 ⇒ 对全轨可达。
#   ⚠ 开关 QG_1D_ASSEMBLY_FALLBACK=0 ⇒ 回退空指令集 (零回归)。
QG_1D_ASSEMBLY_FALLBACK = 1
_QG_1D_ASSEMBLY_ACTIONS = (
    "increase_wire_gauge", "reduce_turns", "increase_turns", "lengthen_core",
    "reduce_core_length", "increase_parallel_paths", "reduce_current_density",
    "increase_wire_section", "wire_resize", "boost_excitation",
)


def _1d_assembly_fallback(all_instrs):
    """1D 层无 Dsi 方向信号时: 从本轮原始指令挑出不碰几何的装配级动作。"""
    if not QG_1D_ASSEMBLY_FALLBACK:
        return []
    _out = []
    for _it in (all_instrs or []):
        if isinstance(_it, dict) and \
                str(_it.get("action") or "") in _QG_1D_ASSEMBLY_ACTIONS:
            _out.append(_it)
    return _out


def _qg_tier_plan(state, all_instrs, redo_count, stuck=False, fail_stage=None):
    """质量门分层预算状态机 (**2D → 3D → 1D**, 每层 2 次, 总 2³=8)。

    ★ P0-1 (2026-09-22 用户指令): 升级顺序由 3D→2D→1D **改为 2D→3D→1D**。
      依据: RUN40 单轮 |ΔSF| 2D 均值 0.2212 vs 3D 均值 0.001667 (弹性比 132.7:1),
      旧顺序让 38%(3/8) 预算空转在弹性≈0 的 3D 层; 新顺序与系列电机设计流程同构。
    ★ 迁移语义:
        2D 用尽 ⇒ tier="3d" (t3+1, redo=0), 指令按 3D 白名单过滤 (几何处方**先**做
                  「→ 绕组等价处方」替换, 否则一进 3D 层就因白名单空而空转)
        3D 用尽 ⇒ tier="1d" (t1+1, t3/t2 清零, redo=0), route_2d_start=True
        1D 消费完 ⇒ 下一轮**归一为 2D** (回 2D 流程起始, 重新走 2D→3D)
      ⇒ `tier=="1d"` 成为**可达的驻留态** (旧实现中它是不可达死分支, 1D 指令还被
        标成「[3D 门]」, 见报告 V2 §1.2)。

    **纯函数** (不依赖 self, 便于离线回归测试): 只做「本轮该在哪一层、下发什么动作、
    要不要升级/回 2D 起始/转人工」的判定, 日志以 (msg, level) 列表返回交由节点推送。

    参数
      state       : WorkflowState
      all_instrs  : 门禁原始改进指令全集 (未分层过滤)
      redo_count  : 当前层已重生次数
      stuck       : 卡死 (本轮失败原因与上轮完全一致)

    返回 dict: tier / t3 / t2 / t1 / redo / total / instrs / route_2d_start / manual / logs
    """
    st = state or {}
    tier = str(st.get("qg_tier") or _QG_TIER_START).lower()
    t3 = int(st.get("qg_tier3_count", 0) or 0)
    t2 = int(st.get("qg_tier2_count", 0) or 0)   # 旧字段: 兼容保留 (新顺序下不再自增)
    t1 = int(st.get("qg_tier1_count", 0) or 0)
    tot = int(st.get("total_regen_count", 0) or 0)
    per = (_QG_TB_2D if tier == "2d" else (_QG_TB_3D if tier == "3d" else _QG_TB_1D))
    logs = []
    route_2d_start = False
    manual = False
    # ★ P0-1: 1D 门消费完的那一轮 ⇒ 层级归一为 2D (回 2D 流程起始, 重走 2D→3D)。
    #   1D 执行时已把 t3/t2 清零, 故此处只需归一 tier, 无需再动计数。
    if tier == "1d":
        tier = _QG_TIER_START
        logs.append(("[1D 门→2D 起始] 基准尺寸层已执行 (Dsi 变更), 层级重置回 "
                     "2D 门, 重新走 2D→3D (本层计数已清零)", "info"))
    # ★ 先做「几何处方 → 绕组等价处方」替换, 再按层过滤 —— 否则 3D 层会把唯一的
    #   抬 SF 杠杆直接滤掉, 本轮只剩压 SF 的 reduce_turns。
    instrs = _filter_instrs_by_tier(_tier_substitute_instrs(all_instrs, tier), tier,
                                    fail_stage=fail_stage)
    # 本层"杠杆用尽"判据 (满足任一即升级层级):
    #   ① 本层已反复 per 次  ② 本层白名单下无可用动作 (下发即空转)
    #   ③ 卡死 (失败原因与上轮一致 —— 换层级换动作集才有意义)
    exhausted = (redo_count >= per) or (not instrs) or bool(stuck)
    if tot >= _QG_TIER_TOTAL_CAP:
        manual = True
        logs.append((f"累计重生已达硬上限 {_QG_TIER_TOTAL_CAP} 次 "
                     f"(2D={_QG_TB_2D}/3D={_QG_TB_3D}/1D={_QG_TB_1D}, 总上限={_QG_TIER_TOTAL_CAP}), "
                     f"停止重生转人工裁决", "error"))
    elif exhausted:
        if tier == "2d" and t3 < per:
            # ★ P0-1: 2D(冲片定形层) 用尽 ⇒ 升 3D(冲片锁定层)。升级后冲片锁定, 只调绕组。
            tier, t3, redo_count = "3d", t3 + 1, 0
            # 3D 层只认绕组动作 ⇒ 必须**先**把几何处方替换成绕组等价处方 (P0-6: 降电密),
            #   否则白名单过滤后为空 ⇒ 下一轮立刻按判据②再次升级 (空转一轮)。
            instrs = (_filter_instrs_by_tier(
                _tier_substitute_instrs(all_instrs, "3d"), "3d",
                fail_stage=fail_stage)
                or list(all_instrs or []))
            logs.append((f"2D 门(冲片定形层) 已反复 {per} 次仍未通过 ⇒ 升级 "
                         f"**3D 门(冲片锁定层)** (第 {t3}/{per} 次): 冲片锁定, "
                         f"只调绕组 (匝数/线规/并绕根数/并联支路/电密/铁心长), "
                         f"不修改槽形尺寸, 本层计数清零"
                         + (" [卡死: 失败原因与上轮一致]" if stuck else ""), "warn"))
        elif tier == "3d" and t1 < per:
            # ★ P0-1: 3D 用尽 ⇒ 触发 1D, 并**驻留于 1D** (旧实现把 tier 重置为 "3d")。
            tier, t1, t3, t2, redo_count = "1d", t1 + 1, 0, 0, 0
            # 1D 层必须给出 Dsi 处方, 否则"回 2D 起始"就是无差别重跑 ⇒ 空转一轮
            # ★ S4 (2026-10-10): 三级回退 —— Dsi 处方 → (无) → 装配级动作 (不碰几何)。
            #   旧实现第二级恒为 decrease_stator_id(−0.05), 已被 quality_gate 裁定
            #   「打错靶」删除 ⇒ 现撤除, 改由装配级回退承担 (见 _1d_assembly_fallback)。
            #   ⚠ 第一级也要**显式剔除** decrease_stator_id: 它仍在 1D 白名单
            #     (_QG_TIER_ACTIONS["1d"]) 内, 上游一旦残留就会被白名单直接放行
            #     (实测回归 T5.4 复现) ⇒ 与 quality_gate 的删除裁定口径分裂。
            instrs = [i for i in _filter_instrs_by_tier(all_instrs, "1d",
                                             fail_stage=fail_stage)
                      if str((i or {}).get("action") or "") != "decrease_stator_id"] or \
                _default_1d_instr(all_instrs) or \
                _1d_assembly_fallback(all_instrs)
            if not instrs:
                logs.append(("1D 门(基准尺寸层): 无 Dsi 方向信号、且本轮无装配级动作可回退 "
                             "⇒ 本轮不注入几何改进指令 (★S4: 已停用「打错靶」的 "
                             "decrease_stator_id 默认处方)", "warn"))
            route_2d_start = True
            logs.append((f"3D 门(冲片锁定层) 已反复 {per} 次仍未通过 ⇒ 触发 "
                         f"**1D 门(基准尺寸层)** (第 {t1}/{per} 次, **由 3D 层耗尽触发**): "
                         f"修改定子内圆 Dsi (其余尺寸按比例缩放), 回 2D 流程起始 "
                         f"重新走 2D→3D, 2D/3D 计数清零", "warn"))
        else:
            manual = True
    return {"tier": tier, "t3": t3, "t2": t2, "t1": t1, "redo": redo_count, "total": tot,
            "instrs": instrs, "route_2d_start": route_2d_start,
            "manual": manual, "logs": logs}

# G4/G6 (v4.02.92): 2D 磁路预校模块 (设参考铁长 1m 的磁密带校验)
try:
    from _mag_precheck_2d import (
        mag_ok_from_result as _mag_ok_from_result,
        geom_proxy_ok as _geom_proxy_ok,
        lambda_ok as _lambda_ok,
        resolve_dsi_for_length as _resolve_dsi_for_length,
        # ★ P1-3 (2026-09-22 用户指令): Qs 可行集合选择器 (Qs 由"冻结单值"改"可行集合")
        qs_ok as _qs_ok,
        feasible_stator_slots as _feasible_stator_slots,
        # ★ H17 (2026-09-25 用户指令): 径比真源判定 (Dso/Dsi 带内/带外 + 目标比值,
        #   与 2D 门禁 _m2d_dso_dsi_ok / J5 / H6 同源: id_ratio_by_poles)
        dso_dsi_ok as _dso_dsi_ok,
    )
    _MAG_PRECHECK_AVAILABLE = True
except Exception as _mag_err:
    _MAG_PRECHECK_AVAILABLE = False
    print(f"[orchestrator] _mag_precheck_2d 模块未加载: {_mag_err}")
    _mag_ok_from_result = _geom_proxy_ok = _lambda_ok = _resolve_dsi_for_length = None
    _qs_ok = _feasible_stator_slots = _dso_dsi_ok = None


# P3 (v4.02.93): 几何主因判定 —— 供「L1(冲片锁定层) 旋钮用尽 ⇒ 升级 L2(回 2D 起始 改 Dsi+槽形)」。
#   几何主因 = fail_stage 落在 2d 集 (Bt/By/ζ/几何比/齿宽/Qs/起动转矩), 此时冲片锁定层的
#   绕组+叠长旋钮修不了, 必须回 2D 起始改几何; 直接转人工会丢掉一次几何重优化机会。
_GEOM_ROOT_TAGS = ("定子齿磁密", "定子轭磁密", "齿槽比 ζ",
                   "轭厚/槽深比", "Dso/Dsi 比", "齿宽范围", "定子槽数范围",
                   "起动转矩倍数", "长径比 λ")


def _qg_has_geom_root(qg: dict) -> bool:
    """判断质量门禁失败主因是否在几何层 (2D 集)。"""
    if not isinstance(qg, dict):
        return False
    if str(qg.get("fail_stage") or "") == "2d":
        return True
    _tags = qg.get("fail_tags") or []
    return any(t in _GEOM_ROOT_TAGS for t in _tags)


# v2.05.02: 加载飞轮AI路由规则
def _get_flywheel_routing() -> dict:
    """从飞轮加载AI路由规则, 自动决定本地/云端"""
    try:
        rules_path = r"D:\MotorDesign\Knowledge\evolution_rules.json"
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
                return data.get("routing", {})
    except Exception:
        pass
    return {}


def _apply_routing_rules(use_cloud_review: bool, use_cloud_report: bool) -> tuple:
    """v2.05.02: 根据飞轮路由规则自动调整云端/本地标志
    v4.02.07 H-3 二期 T6b: 叠加管理端 ai_flow 持久化云端开关 (cloud_review_enabled),
        以 OR 语义叠加到 web 前端传入值, 不覆盖前端; 默认关闭时两段均跳过。
    """
    # T6b: 管理端 ai_flow 持久化云端开关优先叠加 (OR 语义)
    try:
        from ai_flow_config import get_config as _afc_get_config
        _afc = _afc_get_config().get("requirement", {})
        if _afc.get("cloud_review_enabled"):
            use_cloud_review = use_cloud_review or True
            use_cloud_report = use_cloud_report or True
    except Exception:
        pass
    routing = _get_flywheel_routing()
    if not routing:
        return use_cloud_review, use_cloud_report

    # 评审步骤路由
    review_rule = routing.get("review_solutions", {})
    if review_rule.get("prefer") == "local" and use_cloud_review:
        print(f"[orchestrator] 飞轮路由: 评审步骤切换为本地 ({review_rule.get('reason', '')})")
        use_cloud_review = False

    # 报告步骤路由
    report_rule = routing.get("generate_report", {})
    if report_rule.get("prefer") == "local" and use_cloud_report:
        print(f"[orchestrator] 飞轮路由: 报告步骤切换为本地 ({report_rule.get('reason', '')})")
        use_cloud_report = False

    return use_cloud_review, use_cloud_report


# 节点名 → 状态名映射 (供 run/run_streaming 统一设置)
STEP_STATUS_MAP = {
    "parse_requirement": "parsed",
    "hc_requirement": "req_confirmed",       # v2.05.06: 需求确认
    "generate_base_params": "base_params_ready",  # v4.02: 基准参数生成
    "hc_base_param": "base_param_confirmed",      # v4.02: 基准参数选定
    "generate_designs": "solved",
    "hc_design": "design_confirmed",          # v2.05.06: 方案确认
    "review_solutions": "reviewed",
    "convergence_review": "converged",
    "decision_gate": "ready",
    "human_approval": "approved",
    "generate_report": "completed",
    "hc_report": "report_confirmed",          # v2.05.06: 报告确认
}

# 节点中文显示名
STEP_DISPLAY_NAMES = {
    "parse_requirement": "需求分析师",
    "hc_requirement": "需求确认",             # v2.05.06
    "generate_base_params": "基准参数生成",     # v4.02
    "hc_base_param": "基准参数选定",           # v4.02
    "generate_designs": "方案生成师",
    "hc_design": "方案确认",                  # v2.05.06
    "review_solutions": "评审推荐师",
    "convergence_review": "收敛阵营",
    "decision_gate": "决策门",
    "human_approval": "人工确认",
    "generate_report": "报告撰写员",
    "hc_report": "报告确认",                  # v2.05.06
}

# v2.05.06: 所有人机交互检查点节点 (用于 SSE human_input_required 事件)
_HC_STEPS = {"hc_requirement", "hc_base_param", "hc_design", "human_approval", "hc_report"}


def _estimate_tokens(text) -> int:
    """粗略估算token数 (中英文混合约4字符/token)"""
    if not text:
        return 0
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    elif not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // 4)


def _quality_overall_from_gate(qg_result: dict) -> float:
    """v4.02.104 (问题4): 由质量门禁结果推导 0~100 的整体质量分。

    背景: 原实现把 `quality_overall=90` **硬编码**写进 project_summary, 运营飞轮的
    质量维度 (avg quality_overall / 低质量项目占比) 因此与真实门禁结果完全脱钩 ——
    无论方案是"一次通过"还是"重生 3 轮仍转人工", 记录都是 90 分。

    口径:
      - 门禁通过     → 100.0
      - 门禁未通过   → 通过项 / 参与判定项 × 100 (跳过项 severity=='skip' 不计入分母)
      - 无门禁结果   → 0.0 (显式表示"未知", 不再伪造 90)
    """
    if not isinstance(qg_result, dict) or not qg_result:
        return 0.0
    if qg_result.get("passed"):
        return 100.0
    checks = [c for c in (qg_result.get("checks") or [])
              if isinstance(c, dict) and c.get("severity") != "skip"]
    if not checks:
        return 0.0
    _ok = sum(1 for c in checks if c.get("passed"))
    return round(100.0 * _ok / len(checks), 1)


class WorkflowState(TypedDict):
    """工作流状态 (v2.04 物理内核 + 运营飞轮 + v2.05.03 人机协作)"""
    raw_text: str
    specs: dict
    confidence: float
    results: List[dict]
    top_solutions: List[dict]
    # v2.04: 物理内核
    scenario_info: dict             # 场景识别结果 (catalog/analogy)
    layered_specs: dict             # L1/L2/L3 分层规格
    pipeline_steps: list            # v4.01.051: 8步流水线过程交付物
    # v4.02: 推荐基准参数 (基准参数选定检查点用)
    base_params: dict               # 推荐的电机基准参数 (全部变量)
    base_param_ranges: dict         # 基准参数的不变或变化范围
    ref_motors: list                # 数据库推荐的Top5参考电机
    # 放大轨
    review: str                    # 评审推荐意见 (支持报告)
    recommendation: str
    # 收敛轨
    con_report: str                # 汇总反对报告
    convergence_findings: dict     # 各收敛agent原始发现
    # 决策门
    pro_report: str                # 放大轨最终报告
    ready_for_confirm: bool        # 双报告齐备, 可交付用户确认
    hc_checkpoint_id: str          # v2.05.03: 人工检查点ID (避免LangGraph保留名)
    human_decision: str            # v2.05.03: 人工决策结果
    human_comment: str             # v2.05.03: 人工批注
    # 报告
    report: str
    report_filepath: str
    report_filename: str
    report_docx_filepath: str      # v4.02.103 (P3-C): Word 镜像落盘路径
    matched_templates: list
    project_id: str
    # errors 使用 Annotated 允许并行节点追加 (而非覆盖)
    errors: Annotated[List[str], operator.add]
    solve_count: int
    use_cloud_review: bool
    use_cloud_report: bool
    step: str
    status: str
    timestamp: str
    # v4.04: 案例电机集成
    case_motor_id: int               # 案例电机ID (0=无案例参考)
    case_motor_ref: dict             # 案例电机引用信息 (code/type/series)
    # v4.01.052: 2.1 预处理数据 (案例电机参数预填充 + 增强文本), 必须声明为 State channel
    # 否则 _node_load_case_motor 返回的 _2_1_data 会被 LangGraph 当未知 key 静默丢弃,
    # 导致 _node_parse_requirement 读不到, 客户端 2.1 段显示"未使用案例电机预处理" (与 param_matrix 教训同源)
    _2_1_data: dict
    # v4.01.037 (P0-1): 结构化评审回灌
    review_feedback: dict            # 结构化评审反馈 (review_feedback_schema 结构)
    feedback_applied: list           # 本轮实际回灌生效的参数修正明细
    feedback_source: str             # llm | llm_retry | regex | manual
    need_manual_feedback: bool       # 解析失败, 需人工补充具体参数
    # v4.01.038 (P0-2): 决策门质量门禁 + 有限次方案重生
    quality_gate_result: dict        # 质量门禁评估结果 (quality_gate.evaluate_quality_gate)
    redo_count: int                  # 已触发方案重生次数
    auto_regenerate: bool            # 质量门禁自动重生环路中(跳过 hc_design 人工)
    regenerate_guidance: str         # 重生时回灌给方案生成智能体的改进指引
    # P1/P2 (v4.02.67): 结构化改进指令闭环
    improvement_instructions: list   # 结构化改进指令 [{action,param,delta,reason,conflict}]
    slot_fill_compromises: list      # 冲突折中指令 [{action,param,delta,reason,conflict}]
    # ★ 方案② (2026-10-10 用户授权): 分轨措施 —— 修复「杠杆对象错位」(RUN63/71/73)。
    #   top10 混合排序不变, 消费侧按 source_path 分轨评估+生成「本轨可达」措施;
    #   执行器按候选 source_path 只吃本轨指令集 (无 source_path/未覆盖轨回退广播旧语义)。
    improvement_instructions_by_rail: dict   # {source_path: [instr]} (方案②)
    slot_fill_compromises_by_rail: dict      # {source_path: [comp]} (方案②)
    prev_round_metrics: dict         # 上一轮最佳方案指标快照 (供措施效果评价 Δ 对比)
    measure_effect: dict             # 改进措施效果评价结果 (evaluate_measure_effect)
    # ★ P1-4 (2026-10-11): 锁定轨「杠杆耗尽」识别的跨轮状态 (方案②延伸)
    rail_sf_history: dict           # {rail: [sf,...]} 锁定轨逐轮 SF 历史 (供 N 轮空转判定)
    rail_exhausted: dict            # {rail: True} 本轮被判定杠杆耗尽的轨 (供换轨配额重分配)
    # P3 (v4.02.93): L1(冲片锁定层) 旋钮用尽且主因在几何时, 是否已升级过一次 L2(回 2D 起始)
    l2_escalated: bool                # True = 已触发 L2 几何重优化 (防重复升级死循环)
    # ★ P1-0 (2026-09-22 用户指令): `prev_qg_reasons` **死码**修复 —— 该 key 由
    #   _node_quality_gate 写 _out (≈L5870) 并被同节点下一轮的 `_stuck_now` (≈L5918)
    #   读取, 用于升级判据③「卡死」(本轮失败原因与上轮完全一致 ⇒ 换层级换动作集),
    #   但它**从未在 WorkflowState 声明** ⇒ LangGraph 静默丢弃 ⇒ state.get() 恒为 None
    #   ⇒ `_stuck_now` 恒 False = **死码**。注意 L740 的注释正是拿它当反面教材却未修。
    #   ⚠ 教训: 本类缺陷已复发 5 次 (#522 param_matrix / #523 _2_1_data / #696 /
    #     fine_solve_count / geometry_images) —— 任何新 state 通道必须两处齐声明。
    prev_qg_reasons: list             # 上一轮质量门失败原因快照 (供跨轮「卡死」检测)
    # 2026-09-21 (用户指令): 质量门分层 (3D→2D→1D) 计数 — 每层 2 次, 总 2³=8
    #   ⚠ 必须同时在 WorkflowState / _initial_state 两处声明, 否则 LangGraph
    #     会当未知 key 静默丢弃 (v4.01.042 #522 / prev_qg_reasons 前例)。
    qg_tier: str                      # 当前门禁层: "2d" | "3d" | "1d" (P0-1 起点=2d)
    qg_tier3_count: int               # P0-1: 已升级到 3D 门(冲片锁定层)的次数 (上限 2)
    qg_tier2_count: int               # 旧字段(兼容保留): 已升级到 2D 门的次数 (新顺序下不再自增)
    qg_tier1_count: int               # 已触发 1D 门(改 Dsi, 回 2D 起始)的次数 (上限 2)
    total_regen_count: int            # 累计重生次数 (硬兜底上限 2³=8)
    # v4.02.68 (系列三 Tier A/B): L0↔L1 差异评价 + 残差飞轮标定
    l0_l1_diff: dict                 # L0↔L1 差异摘要 (joins/avg_abs_delta/bias_count/fault_excluded)
    l0_l1_residuals: list            # 本轮残差样本 (已排除故障方案), 供飞轮累积/审计
    l0_calibration: dict             # L0 离线标定结果 (全局偏置 + 高残差区域)
    # v4.01.038 (P0-3): 方案构思 LLM 节点
    ideation_result: str             # 方案构思叙述 (IdeationAgent 输出)
    design_hints: dict               # 结构化构思约束 (emphasis/avoid/notes)
    # v4.01.039 (P1-6 #486): 局部重做计数 — hc_design 驳回仅重跑 generate_designs
    design_redo_count: int           # 已触发方案局部重做次数 (超上限升级为全流程重做)
    # v4.07 (F8): 分阶 redo 计数 — 此前仅有全局 redo_count + design_redo_count,
    #   缺需求/基准参数/报告三阶计数, 无法按阶段统计返工率与触发各自回退策略
    requirement_redo_count: int       # 需求确认阶段已触发返回重做次数
    base_param_redo_count: int        # 基准参数选定阶段已触发返回重做次数
    report_redo_count: int            # 报告确认阶段已触发返回重做次数
    # v4.07 (F9): 人工表格编辑(override_data)按阶段持久化到 state — 供 revise 重跑上游节点时回灌,
    #   否则 parse_requirement / generate_base_params 重新生成会覆盖用户修改, 导致重做后编辑丢失
    requirement_override_data: dict   # 需求确认阶段人工覆盖数据
    base_param_override_data: dict    # 基准参数阶段人工覆盖数据
    # v4.04 (#10): 方案多样性度量 (覆盖度/创新性/变异数) 在 generate_designs 落盘
    diversity_metrics: dict          # 方案矩阵多样性聚合指标 (coverage/innovation/mutated...)
    # v4.01.042 (#522 修复): 参数矩阵必须声明为 State channel
    # 否则 generate_designs 节点返回的 param_matrix 会被 LangGraph 当未知 key 静默丢弃,
    # 导致 app.py 的 update.get("param_matrix", []) 恒为空, 客户端报 "参数矩阵未收到"
    param_matrix: list               # 本轮扫描生成的候选参数矩阵 (交付物/过程展示用)
    # v4.04 (#7 反向闭环): 采纳方案回写案例电机库后记录的新案例ID (0=未回写)
    generated_case_motor_id: int     # AI 采纳方案回写案例电机库的案例ID (双向闭环)
    # v4.06 (#696): 补声明此前未在 WorkflowState 声明、被 LangGraph 静默丢弃的 State channel
    # 这些 key 由 _node_* 返回且被 app.py SSE 转发消费, 未声明 -> LangGraph 丢弃节点返回值
    # -> 客户端收不到对应过程交付物 (与 #522 param_matrix / #523 _2_1_data 同源缺陷)
    extraction_notes: list              # parse_requirement 返回, hc_requirement 消费作 inference_notes
    scenario_summary: dict             # generate_base_params 返回, SSE 基准参数卡片第1段
    ideation_input: dict               # design_ideation 返回, SSE 过程交付物
    quality_gate_passed: bool          # decision_gate 返回, SSE 终选确认 + 客户端决策门展示
    # v4.06 (#696): generate_designs 返回的 4 段过程交付物 (随 SSE step_done 下发)
    param_matrix_meta: dict            # 参数矩阵元数据 (扫描数/实际数/飞轮剔除/多样性)
    solve_summary: dict                # 物理求解摘要 (L1/L2 三级精度 + 计数)
    review_feedback_result: dict       # 评审回灌结果 (applied/skipped/rejected)
    scenario_reidentified: bool        # 评审回灌是否触发场景重新识别
    # v4.06 (#696): 关键真 bug — fine_solve_count 在 _initial_state 设置但此前未声明,
    # LangGraph 从初始输入静默丢弃 -> _node_generate_designs 读到恒为 0 -> L2(Motor-CAD) 永久跳过
    fine_solve_count: int              # 用户选择的精细验证方案数 (0=跳过 Motor-CAD)
    # v4.02.36 (种子评价方案): 种子层评价结论 (阶段2 合理性诊断 + 阶段3 L1 功率校核)
    base_seeds: dict                   # 种子快照 + verification 摘要 (此前未声明, 依赖宽松合并)
    seed_verification: dict            # 独立通道: seed_count/reason_*/l1_* 统计
    # v4.02.74 (#522/#523/#696 同源第 5 次复发): generate_designs 返回但此前未声明的 3 个
    # State channel。未声明 → LangGraph 把节点返回值中这些 key 静默丢弃 →
    # app.py 的 update.get("geometry_images", []) 恒为空 →
    # 客户端「4. top10 方案图示」无论后端是否抓到图都恒显示
    # 「(本批次无可用几何截图 — RMxprt 求解未产出有效几何或无桌面 GUI)」。
    # 注意: 图片文件本身、worker images.geometry 透传、app.py SSE 白名单登记
    # (L2090) 三环均正常, 丢的只有「顶层 state 通道」这一环。
    geometry_images: list              # TOP10 各方案 RMxprt 2D 几何截图
                                       # [{rank,paths,files,urls,od,poles,eff}]
    attempt_index: int                 # 本批次方案生成轮次 (1=首次, >1=门禁/驳回后重生)
    attempt_reason: str                # 轮次成因说明 (客户端「反复生成」段展示)


class MotorAIOrchestrator:
    """双轨辩证制衡编排器"""

    def __init__(self):
        # 放大阵营
        self.req_agent = RequirementAgent()
        self.design_agent = DesignAgent()
        self.ideation_agent = IdeationAgent()
        self.review_agent = ReviewAgent()
        self.report_agent = ReportAgent()
        # 收敛阵营
        self.reverse_reviewer = ReverseReviewer()
        self.reality_checker = RealityChecker()
        self.sandbox_simulator = SandboxSimulator()
        self.devils_advocate = DevilsAdvocate()

        self._current_project_id = None
        self.graph = self._build_graph()
        # v2.05.04: 使用 SqliteSaver 替代 MemorySaver, 实现工作流状态持久化
        # 后端重启后可从检查点恢复工作流 (断点续传)
        import sqlite3
        _checkpoint_db = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                      "KnowledgeBase", "workflow_checkpoints.db")
        os.makedirs(os.path.dirname(_checkpoint_db), exist_ok=True)
        self._checkpoint_conn = sqlite3.connect(_checkpoint_db, check_same_thread=False, timeout=10)
        self._checkpoint_conn.execute("PRAGMA journal_mode=WAL")
        self._checkpoint_conn.execute("PRAGMA synchronous=NORMAL")
        self._checkpoint_conn.execute("PRAGMA busy_timeout=5000")
        # v2.05.08: 主动合并残留 WAL(-wal), 降低 auto-checkpoint 合并主库时的外部锁冲突面
        # (attempt to write a readonly database 多为 WAL/主库被杀毒或备份实时扫描以只读锁挡所致)
        try:
            self._checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self.checkpointer = SqliteSaver(self._checkpoint_conn)
        self.compiled = self.graph.compile(checkpointer=self.checkpointer)

        # 实时进度队列 (线程安全)
        self._progress_queue = queue.Queue()

        # v4.00.04: 工作流并发保护 — 防止多个工作流同时运行导致状态混乱
        # v4.00.06: 改为排队机制 — 多客户端同时发起时排队执行而非拒绝
        # v4.00.07: 改为信号量并发 — 小于max_concurrent时并发, 超出排队
        # v4.00.004: 从 workflow_config 读取 (运行时可动态修改)
        if _WC_AVAILABLE:
            self._max_concurrent = _wc_get_max_concurrent()
        else:
            self._max_concurrent = int(os.getenv("MOTOR_MAX_CONCURRENT", "2"))
        self._active_count = 0
        self._workflow_lock = threading.Lock()
        self._workflow_slot_event = threading.Event()
        self._workflow_slot_event.set()  # 初始: 有空闲槽位
        self._queue_count = 0  # 当前排队等待数
        self._running_since = {}  # thread_id -> start_time (用于僵尸检测)

        # v4.00.07: 线程本地存储 — 每个并发工作流隔离 progress_queue / cost_tracker
        self._tl = threading.local()

        # v2.05.03: 成本追踪器 (每个工作流实例重新绑定project_id)
        # v4.00.07: self.cost_tracker 作为 fallback, 并发时用 thread-local 覆盖
        self.cost_tracker = TaskCostTracker()

    def set_max_concurrent(self, n: int):
        """v4.00.004: 运行时动态设置最大并发数"""
        old = self._max_concurrent
        self._max_concurrent = max(1, min(10, int(n)))
        if _WC_AVAILABLE:
            _wc_set_max_concurrent(self._max_concurrent)
        # 槽位可能释放了, 唤醒等待线程
        self._workflow_slot_event.set()
        logger.info(f"[orchestrator] max_concurrent: {old} -> {self._max_concurrent}")
        return old

        # v3.00.02: 两级求解权限控制 (RMxprt 快速扫描 + Motor-CAD 精细验证)
        self.can_use_motorcad = False   # 是否允许使用主机共享 Motor-CAD
        self.use_local_motorcad = False  # 是否使用客户端本地 Motor-CAD

    def _build_graph(self):
        graph = StateGraph(WorkflowState)

        graph.add_node("parse_requirement", self._node_parse_requirement)
        graph.add_node("load_case_motor", self._node_load_case_motor)       # v4.04: 案例电机加载
        graph.add_node("hc_requirement", self._node_hc_requirement)       # v2.05.06
        graph.add_node("generate_base_params", self._node_generate_base_params)  # v4.02
        graph.add_node("hc_base_param", self._node_hc_base_param)               # v4.02
        graph.add_node("generate_designs", self._node_generate_designs)
        # v4.01.038 (P0-3): 方案构思 LLM 节点 (基准参数选定后, 方案生成前)
        graph.add_node("design_ideation", self._node_design_ideation)
        graph.add_node("hc_design", self._node_hc_design)                 # v2.05.06
        graph.add_node("review_solutions", self._node_review_solutions)
        graph.add_node("convergence_review", self._node_convergence_review)
        # v4.01.038 (P0-2): 决策门前的质量门禁节点 (置信度/硬约束/收敛轨严重冲突 + 有限次重生)
        graph.add_node("quality_gate", self._node_quality_gate)
        graph.add_node("decision_gate", self._node_decision_gate)
        graph.add_node("human_approval", self._node_human_approval)
        graph.add_node("generate_report", self._node_generate_report)
        graph.add_node("hc_report", self._node_hc_report)                 # v2.05.06

        # v4.04: START → load_case_motor → parse_requirement
        graph.add_edge(START, "load_case_motor")
        graph.add_edge("load_case_motor", "parse_requirement")
        # v2.05.06: 需求分析后进入人机确认, reject则回退重新解析
        graph.add_edge("parse_requirement", "hc_requirement")
        graph.add_conditional_edges(
            "hc_requirement",
            self._after_hc_requirement,
            {"proceed": "generate_base_params", "revise": "parse_requirement"}
        )

        # v4.02: 基准参数生成 → 基准参数选定检查点 → 方案构思 → 参数矩阵求解
        graph.add_edge("generate_base_params", "hc_base_param")
        graph.add_conditional_edges(
            "hc_base_param",
            self._after_hc_base_param,
            {"proceed": "design_ideation", "revise": "generate_base_params"}
        )
        # v4.01.038 (P0-3): 方案构思 → 方案生成
        graph.add_edge("design_ideation", "generate_designs")

        # v2.05.06/v4.00.03: 方案生成后进入人机确认, reject统一回退需求分析师重新解析
        graph.add_edge("generate_designs", "hc_design")
        # hc_design 之后根据方案结果分叉 (原 _after_designs 逻辑移到这里)
        graph.add_conditional_edges(
            "hc_design",
            self._after_hc_design,
            {
                "review_pro": "review_solutions",
                "review_con": "convergence_review",
                "skip": "generate_report",
                "revise": "parse_requirement",    # v4.00.03: 统一回退需求分析师
                "design_redo": "design_ideation",  # v4.01.059 (P1-#3): 局部重做经方案构思, 审核意见作 guidance 差异化构思
            }
        )

        # 双轨汇入质量门禁 (P0-2: 决策门前自动校验)
        graph.add_edge("review_solutions", "quality_gate")
        graph.add_edge("convergence_review", "quality_gate")
        # v4.01.038 (P0-2): 质量门禁路由
        #   pass        → 决策门 (人工确认)
        #   regenerate  → 自动重生方案 (回到 generate_designs, 跳过 hc_design 人工)
        #   manual      → 重生超限, 仍进决策门但标注需人工裁决
        graph.add_conditional_edges(
            "quality_gate",
            self._after_quality_gate,
            {
                "pass": "decision_gate",
                "regenerate": "design_ideation",
                # G7 (v4.02.92): 纯 2D(磁路)失败回退到 2D 构思(B3 重优化几何/Dsi);
                # 映射到既有 design_ideation 节点(不新增节点, 图安全)。
                "regenerate_2d": "design_ideation",
                "manual": "decision_gate",
            }
        )
        # v2.05.03: 决策门之后进入人工确认 (双报告齐备时) 或直达报告
        graph.add_conditional_edges(
            "decision_gate",
            self._after_decision_gate,
            {
                "need_human": "human_approval",
                "skip": "generate_report",
            }
        )
        # v2.05.04/v4.00.03: 人工确认后条件路由 - reject回退方案确认重新审核, 否则生成报告
        graph.add_conditional_edges(
            "human_approval",
            self._after_human_approval,
            {
                "proceed": "generate_report",
                "revise": "hc_design",           # v4.00.03: 回退方案确认重新审阅
            }
        )
        # v2.05.06/v4.00.03: 报告生成后进入人机确认, reject回退需求分析师重新解析后重做
        graph.add_edge("generate_report", "hc_report")
        graph.add_conditional_edges(
            "hc_report",
            self._after_hc_report,
            {"proceed": END, "revise": "parse_requirement"}    # v4.00.03: 统一回退需求分析师
        )

        return graph

    def _after_hc_requirement(self, state: WorkflowState) -> str:
        """v2.05.06/v4.00.03: 需求确认后路由 - reject/redo则回退重新解析, 否则生成方案"""
        decision = state.get("human_decision", "auto_approve")
        if decision in ("reject", "redo"):
            comment = state.get("human_comment", "")
            msg = "用户要求返回重做需求" if decision == "redo" else "用户驳回需求"
            if comment:
                msg += f" (审核意见: {comment[:80]})"
                # v4.00.03: 从审核意见提取需求参数修正
                extracted = self._extract_params_from_comment(comment, state)
                if extracted:
                    msg += f" [提取到参数修正: {extracted}]"
            self._push_progress("hc_requirement", f"{msg}, 回退重新解析...", "warn")
            return "revise"
        if decision == "end":
            self._push_progress("hc_requirement", "用户要求结束流程", "warn")
            return "proceed"  # 走正常流程结束
        return "proceed"  # v4.02: proceed → generate_base_params

    def _after_hc_base_param(self, state: WorkflowState) -> str:
        """v4.02: 基准参数选定后路由 - reject则回退重新生成基准参数, 否则进入参数矩阵求解"""
        decision = state.get("human_decision", "auto_approve")
        if decision in ("reject", "redo"):
            comment = state.get("human_comment", "")
            msg = "用户要求返回重做基准参数" if decision == "redo" else "用户驳回基准参数"
            if comment:
                msg += f" (审核意见: {comment[:80]})"
            self._push_progress("hc_base_param", f"{msg}, 回退重新生成基准参数...", "warn")
            return "revise"
        return "proceed"

    def _after_hc_design(self, state: WorkflowState) -> list:
        """v2.05.06/v4.00.03: 方案确认后路由
        - reject: 局部重做(仅重跑 generate_designs, 超上限升级为全流程重做)
        - redo:   全流程重做(回退需求分析师重新解析)
        - 否则根据结果分叉双轨
        """
        # v4.01.038 (P0-2): 质量门禁自动重生环路 — 跳过人工检查点, 直接双轨并行
        if state.get("auto_regenerate"):
            self._push_progress("hc_design",
                                "质量门禁自动重生环路: 跳过人工检查点, 直接进入双轨评审",
                                "info")
            return ["review_pro", "review_con"]
        decision = state.get("human_decision", "auto_approve")
        # v4.01.039 (P1-6 #486): reject=局部重做, redo=全流程重做
        if decision == "redo":
            comment = state.get("human_comment", "")
            msg = "用户要求返回重做方案"
            if comment:
                msg += f" (审核意见: {comment[:80]})"
                extracted = self._extract_params_from_comment(comment, state)
                if extracted:
                    msg += f" [提取到参数修正: {extracted}]"
            self._push_progress("hc_design", f"{msg}, 回退需求分析师重新解析(全流程重做)...", "warn")
            return ["revise"]
        if decision == "reject":
            comment = state.get("human_comment", "")
            _rc = int(state.get("design_redo_count", 0) or 0)
            # v4.01.039 (P1-6 #486): 局部重做上限, 超过则升级为全流程重做
            _MAX_LOCAL_REDO = 3
            if _rc >= _MAX_LOCAL_REDO:
                self._push_progress("hc_design",
                    f"局部重做已达上限({_rc}/{_MAX_LOCAL_REDO}), 升级为全流程重做...",
                    "warn")
                return ["revise"]
            msg = "用户驳回方案, 局部重做(仅重跑方案生成)"
            if comment:
                msg += f" (审核意见带入生成: {comment[:80]})"
                extracted = self._extract_params_from_comment(comment, state)
                if extracted:
                    msg += f" [提取到参数修正: {extracted}]"
            self._push_progress("hc_design", f"{msg} [第 {_rc + 1} 次局部重做]", "warn")
            return ["design_redo"]
        # 正常流程: 有结果则双轨并行, 无结果跳过
        if state.get("results") and len(state["results"]) > 0:
            return ["review_pro", "review_con"]
        return ["skip"]

    def _after_hc_report(self, state: WorkflowState) -> str:
        """v2.05.06/v4.00.03: 报告确认后路由 - reject/redo则回退需求分析师重新解析, 否则结束"""
        decision = state.get("human_decision", "auto_approve")
        if decision in ("reject", "redo"):
            comment = state.get("human_comment", "")
            msg = "用户要求返回重做报告" if decision == "redo" else "用户驳回报告"
            if comment:
                msg += f" (审核意见: {comment[:80]})"
                # v4.00.03: 从审核意见提取参数修正
                extracted = self._extract_params_from_comment(comment, state)
                if extracted:
                    msg += f" [提取到参数修正: {extracted}]"
            self._push_progress("hc_report", f"{msg}, 回退需求分析师重新解析...", "warn")
            return "revise"
        return "proceed"

    def _extract_params_from_comment(self, comment: str, state: dict) -> dict:
        """v4.01.037 (P0-1): 结构化评审回灌 — 解析审核意见为 review_feedback.

        原 v4.00.03 实现是 7 条硬编码正则(只认极数/功率/转速/电压/类型),
        现改为调用 Core/review_feedback_schema 三级链路:
            LLM 结构化 JSON → 全字段中文别名正则兜底 → 需人工填写
        覆盖 FIELD_REGISTRY 全部 30 个可回灌字段(含结构参数)。

        本方法只负责"解析 + 展示", 真正写入 specs/base_params 由
        _ingest_review_feedback() 在节点内完成(节点才能返回 state 更新)。
        返回 {规范字段名: 新值} 便于日志与检查点展示。
        """
        if not comment:
            return {}
        specs_now = state.get("specs", {}) or {}
        base_now = state.get("base_params", {}) or {}
        if not _RF_AVAILABLE:
            return self._extract_params_legacy(comment, state)
        try:
            res = _rf_resolve(comment, comment, specs_now, base_now)
        except Exception as _e:
            self._push_progress("param_extract",
                                f"结构化回灌解析异常, 降级正则: {_e}", "warning")
            return self._extract_params_legacy(comment, state)

        fb = res.get("feedback", {}) or {}
        extracted = {it["field"]: it["new_value"]
                     for it in fb.get("feedback_list", [])}
        if extracted:
            self._push_progress(
                "param_extract",
                f"结构化回灌[{res.get('source')}/{res.get('parse_level')}] "
                f"提取 {len(extracted)} 项修正: {extracted}", "info")
            for _drop in (fb.get("unrecognized") or [])[:3]:
                self._push_progress(
                    "param_extract",
                    f"回灌丢弃 {_drop.get('field')}={_drop.get('new_value')} "
                    f"({_drop.get('error')})", "warning")
        elif res.get("need_manual"):
            self._push_progress(
                "param_extract",
                f"审核意见未解析出可执行的参数修正"
                f"({str(res.get('parse_error', ''))[:60]}), 请在批注中写明"
                f"「参数名 改为 数值」, 例如「极数改为4, 铁心长度改为185」", "warning")
        return extracted

    def _extract_params_legacy(self, comment: str, state: dict) -> dict:
        """v4.00.03 遗留正则提取 (仅在 review_feedback_schema 不可用时降级使用)"""
        import re
        extracted = {}
        specs = state.get("specs", {})
        
        # 极数: "6极" "4极" "改为8极" "极数改为4"
        m = re.search(r'(\d+)\s*极', comment)
        if m:
            poles = int(m.group(1))
            if poles != specs.get("poles"):
                extracted["poles"] = poles
        
        # 功率: "功率改为15kW" "功率15kw" "15千瓦" "15kW"
        m = re.search(r'(?:功率|改为)\s*(\d+\.?\d*)\s*[kK千]?[wW瓦]', comment)
        if not m:
            m = re.search(r'(\d+\.?\d*)\s*[kK千][wW瓦]', comment)
        if m:
            power = float(m.group(1))
            if abs(power - specs.get("power_kw", 0)) > 0.1:
                extracted["power_kw"] = power
        
        # 转速: "转速改为1500" "1500转" "1500rpm"
        m = re.search(r'转速.*?(\d+)|(\d+)\s*[转r]', comment)
        if m:
            rpm = int(m.group(1) or m.group(2))
            if rpm != specs.get("speed_rpm"):
                extracted["speed_rpm"] = rpm
        
        # 电压: "380V" "电压改为220"
        m = re.search(r'(?:电压|改为)\s*(\d+)\s*[Vv]?', comment)
        if not m:
            m = re.search(r'(\d+)\s*[Vv]', comment)
        if m:
            voltage = int(m.group(1))
            if voltage != specs.get("voltage_v"):
                extracted["voltage_v"] = voltage
        
        # 电机类型
        type_map = {
            "异步": "异步电动机", "感应": "异步电动机",
            "永磁": "永磁同步电动机", "同步": "永磁同步电动机",
            "开关磁阻": "开关磁阻电动机", "直流": "直流电动机",
            "无刷": "无刷直流电动机",
        }
        for keyword, mtype in type_map.items():
            if keyword in comment:
                if mtype != specs.get("motor_type"):
                    extracted["motor_type"] = mtype
                break
        
        # v4.00.03: 将提取参数注入 state 的 specs 中 (下一次 parse_requirement 会用到)
        if extracted:
            self._push_progress("param_extract", 
                                f"从审核意见提取参数修正: {extracted}", "info")
        
        return extracted

    # ==================== v4.01.037 (P0-1): 结构化评审回灌 ====================
    # ==================== v4.07 (F9): override_data 回灌工具 ====================
    # v4.02.62 (故障修复 #2 P0-B): 人工覆盖占位符黑名单
    #   根因: 需求确认表在字段未识别时显示占位符 "—", get_override_data 因 "—" 非空而全量上报,
    #         旧回写逻辑只过滤 None/"", 导致 specs 被写入 23 个 "—" 字符串;
    #         下游 _node_generate_base_params 目录排序 `e_poles - poles` 抛
    #         TypeError: unsupported operand type(s) for -: 'int' and 'str' → 工作流异常终止。
    _PLACEHOLDER_VALUES = {"", "—", "–", "－", "-", "--", "---", "*",
                           "未知", "N/A", "n/a", "NA", "null", "None", "nan"}

    @classmethod
    def _is_placeholder_value(cls, val) -> bool:
        """判断人工覆盖值是否为占位符(应跳过, 不写回 specs/base_params)"""
        if val is None:
            return True
        if isinstance(val, str):
            return val.strip() in cls._PLACEHOLDER_VALUES
        return False

    def _apply_override_to_dict(self, target: dict, override_data: dict) -> int:
        """将人工表格编辑(override_data)强制写回目标字典; 返回生效项数。

        override_data 结构: {key: {"value": v}} 或 {key: v}。
        用于 hc 节点应用编辑, 以及 revise 重跑上游节点(parse_requirement /
        generate_base_params)时把持久化的覆盖数据重新套用, 避免重做后编辑丢失。
        空值(None/"")跳过, 不计入生效。
        """
        if not override_data or not isinstance(override_data, dict):
            return 0
        applied = 0
        for _k, _v in override_data.items():
            _val = _v.get("value") if isinstance(_v, dict) else _v
            if _val in (None, ""):
                continue
            # v4.02.62 (故障修复 #2 P0-B): 跳过占位符 "—"/"未知"/"N/A" —— 杜绝 str 污染 specs
            if self._is_placeholder_value(_val):
                continue
            # v4.02.62: 数值型字段自动收敛 —— 原值为数值时, 数字字符串强制转回同型数值
            _old = target.get(_k)
            if (isinstance(_old, (int, float)) and not isinstance(_old, bool)
                    and isinstance(_val, str)):
                try:
                    _val = float(_val) if isinstance(_old, float) else int(float(_val))
                except (TypeError, ValueError):
                    pass
            target[_k] = _val
            applied += 1
        return applied

    def _ingest_review_feedback(self, state: WorkflowState, specs: dict,
                                base_params: Optional[dict] = None,
                                base_param_ranges: Optional[dict] = None,
                                stage: str = "generate_designs") -> dict:
        """把评审/人工反馈结构化回灌到 specs / base_params (原地修改).

        与 _extract_params_from_comment 的分工:
            _extract_params_from_comment — 只解析+展示 (供路由函数打日志)
            _ingest_review_feedback      — 解析+逐项写入 (只能在节点内调用)

        返回 state 更新片段; 无反馈时返回 {}。
        """
        decision = state.get("human_decision", "") or ""
        comment = (state.get("human_comment", "") or "").strip()
        if decision not in ("reject", "redo") or not comment:
            return {}
        if not _RF_AVAILABLE:
            return {}

        self._push_progress(stage, f"包含审核意见: {comment[:60]}", "info")

        # 优先用评审智能体已产出的结构化 JSON, 否则从人工批注解析
        raw_candidate = state.get("review_feedback") or comment
        try:
            res = _rf_resolve(raw_candidate, comment, specs, base_params or {})
            fb = res.get("feedback", {}) or {}
            rep = _rf_apply(fb, specs=specs, base_params=base_params,
                            base_param_ranges=base_param_ranges)
        except Exception as _e:
            self._push_progress(stage, f"结构化回灌失败: {_e}", "warning")
            return {}

        for item in rep.get("applied", []):
            lvl = "error" if item.get("severity") == "critical" else "warn"
            self._push_progress(
                stage,
                f"回灌修正 {item['field_cn']}: {item.get('old_value')}"
                f" → {item['new_value']} ({(item.get('reason') or '')[:40]})", lvl)
        for item in rep.get("skipped", []):
            self._push_progress(stage,
                                f"回灌跳过 {item['field_cn']}={item['value']} (与现值一致)",
                                "info")
        for item in rep.get("rejected", []):
            self._push_progress(stage,
                                f"回灌拒绝 {item.get('field')}={item.get('new_value')}"
                                f" ({item.get('error')})", "warning")

        if not rep.get("applied") and res.get("need_manual"):
            self._push_progress(stage,
                                "审核意见无可执行参数修正, 将按原参数重新生成方案"
                                "(建议在批注中写明「参数名 改为 数值」)", "warning")
        else:
            self._push_progress(
                stage,
                f"结构化回灌完成[{res.get('source')}]: 生效 {len(rep.get('applied', []))} 项, "
                f"跳过 {len(rep.get('skipped', []))} 项, "
                f"拒绝 {len(rep.get('rejected', []))} 项", "success")

        return {"review_feedback": fb,
                "feedback_applied": rep.get("applied", []),
                "feedback_skipped": rep.get("skipped", []),
                "feedback_rejected": rep.get("rejected", []),
                "feedback_source": res.get("source", ""),
                "need_manual_feedback": bool(res.get("need_manual"))}

    def _after_quality_gate(self, state: WorkflowState) -> str:
        """v4.01.038 (P0-2): 质量门禁路由

        读取 quality_gate_result 与 redo_count, 决定:
          pass       → 进入决策门人工确认
          regenerate → 自动重生方案 (回到 generate_designs)
          manual     → 重生超限, 仍进决策门但标注需人工裁决
        """
        qg = state.get("quality_gate_result") or {}
        redo_count = int(state.get("redo_count", 0) or 0)
        if not qg:
            # 模块不可用 / 未评估, 放行 (不阻断既有流程)
            return "pass"
        # R3 (2026-09-20): 优先沿用节点已做出的路由决策 (node 读自增前 redo_count、
        #   条件边读自增后 ⇒ 天然相差 1, 临界轮口径冲突会丢弃末轮改进指令)。
        #   仅当无 hint (旧 state / 模块不可用兜底) 时才退回本处自算。
        _hint = qg.get("_route_hint")
        if _hint in ("pass", "regenerate", "regenerate_2d", "manual"):
            path = _hint
        else:
            path = _qg_decide(qg, redo_count, _qg_config(),
                              prev_reasons=state.get("prev_qg_reasons")) if _QG_AVAILABLE else (
                "pass" if qg.get("passed") else "manual")
        if path in ("regenerate", "regenerate_2d"):
            # 2026-09-21 修复: 原 `if path == "regenerate":` 把 "regenerate_2d" 排除在
            #   外层块之外 ⇒ 1263 的 `if path == "regenerate_2d"` 恒为假、1D 门(改 Dsi 回 2D
            #   起始) 触发的重生指令被 1313 的 `return "pass"` 吞掉转人工, 27 轮预算的最后一层
            #   从未真正执行。现把 "regenerate_2d" 纳入外层条件, 使 1D 层重生可达。
            # P6: 求解器层超时 / workflow 层护盾对账 —— 重生前校验求解器可用性。
            # 若求解器不可用 (死锁/超时/未就绪), 盲目重生只会再次失败并空耗 max_redo 次数,
            # 故直接转人工裁决, 由人工决定是否恢复求解器或改用模拟模式。
            _pre = qg.get("solver_precheck") or {}
            if _pre.get("enforce") and not _pre.get("available", True):
                self._push_progress("quality_gate",
                                    f"方案重生前校验求解器不可用 ({_pre.get('reason', '')}), "
                                    f"转人工裁决而非空耗重生次数", "error")
                return "manual"
            # ── 2026-09-21 (用户指令①): 「回 2D 流程起始」**只由 1D 门**触发 ──
            #   节点在触发 1D 门(改定子内圆 Dsi) 时才会写 _route_hint="regenerate_2d"。
            #   除此之外一律留在本层重生 —— 这是"3D 门不返回 2D 流程起始"的落地点。
            if path == "regenerate_2d":
                self._push_progress(
                    "quality_gate",
                    "1D 门(基准尺寸层): 修改定子内圆 Dsi (其余尺寸按比例缩放), "
                    "回 2D 流程起始重新走 2D→3D; 3D/2D 计数已清零", "warn")
                return "regenerate_2d"
            # G7 (v4.02.92): 失败阶段分流 — 纯 2D(磁路)失败 → regenerate_2d,
            #   回退到 2D 构思(B3 重优化几何/Dsi); 3D/mixed 维持原 regenerate(回 3D 构思)。
            _stage = (qg.get("fail_stage") or "")
            # 2026-09-20 (用户指令): 槽满率偏低 ⇒ **必回 2D 流程起点**。
            #   其修复动作 (减槽高 Hs + 同步减外径 Dso, 保轭宽/By) 是冲片几何操作,
            #   只能在 2D 起点做。故即便同轮混有 3D 集失败 (fail_stage=="mixed"),
            #   也按 2D 路由优先 —— 否则走 3D(冲片锁死层) 重生时槽高/外径改不动 ⇒ 空转。
            _sf_low_2d = any("槽满率下限" in str(t) for t in (qg.get("fail_tags") or []))
            # 2026-09-21 (用户指令①): 旧逻辑在此直接 `return "regenerate_2d"` ——
            #   即 **3D 门一检出槽满率偏低就回 2D 起始改槽形**, 与"冲片锁定层不动槽形"
            #   直接冲突 (也是 SF 在 0.28↔0.78 跨带震荡的放大器之一)。现改为**只提示
            #   不路由**: P0-1 新顺序 (2D→3D→1D) 下, 3D(冲片锁定)层用**降电密**等绕组
            #   杠杆, 连满 2 次才升级 1D 门回 2D 起始改 Dsi。
            if _stage == "2d" or _sf_low_2d:
                _tier_tag = str(qg.get("_qg_tier") or _QG_TIER_START).upper()
                _msg = "[" + _tier_tag + " 门] 检出"
                _msg += "槽满率偏低" if _sf_low_2d else "2D(磁路)主因"
                _msg += " ⇒ 按分层策略本轮不回 2D 流程起始, 留在当前层重生"
                if str(qg.get("_qg_tier") or _QG_TIER_START) == "3d":
                    _msg += " (冲片锁定: 只调绕组 —— 降电密/线规/并绕, 不碰槽形)"
                else:
                    _msg += " (已放开槽形尺寸, 可由缺口反解槽深)"
                self._push_progress("quality_gate", _msg, "info")
            self._push_progress("quality_gate",
                                f"[{str(qg.get('_qg_tier') or _QG_TIER_START).upper()} 门] "
                                f"质量门禁未通过, 触发第 {redo_count + 1} 次方案自动重生",
                                "warn")
            return "regenerate"
        if path == "manual":
            # P3 (v4.02.93): L1 旋钮用尽但主因在几何 ⇒ 升级 L2(回 2D 起始改 Dsi+槽形),
            #   而非直接转人工 (node 侧已置 l2_escalated 并重置 redo 预算, 只升级一次)。
            # R6 (2026-09-20): 增加 `auto_regenerate` 守卫 —— 仅当**本轮刚发生** L2 升级
            #   (节点置 auto_regenerate=True 并重置 redo_count=0) 时才放行 regenerate_2d。
            #   否则 l2_escalated 一旦为真将永久命中此分支, 而"预算耗尽且不可再升级"的
            #   节点已不再自增 redo_count ⇒ 无限回 2D 起点死循环 (每圈空耗一整轮 RMxprt)。
            if state.get("l2_escalated") and state.get("auto_regenerate"):
                self._push_progress("quality_gate",
                                    "几何主因: L1(冲片锁定层)旋钮用尽 ⇒ 升级 L2, "
                                    "回 2D 起始改定子内径 + 修正定/转子槽形", "warn")
                return "regenerate_2d"
            self._push_progress("quality_gate",
                                f"质量门禁连续 {redo_count} 次未通过已达上限, "
                                f"转人工裁决 (决策门将标注风险)", "error")
            return "manual"
        return "pass"

    def _after_decision_gate(self, state: WorkflowState) -> str:
        """v2.05.03: 决策门后路由, 双报告齐备则进入人工确认"""
        if state.get("ready_for_confirm") and state.get("results"):
            return "need_human"
        return "skip"

    def _after_human_approval(self, state: WorkflowState) -> list:
        """v2.05.04/v4.00.03: 人工确认(方案终选)后路由 - reject回退方案确认重新审阅, 否则生成报告"""
        decision = state.get("human_decision", "auto_approve")
        if decision in ("reject", "redo"):
            comment = state.get("human_comment", "")
            msg = "用户要求返回重做选型" if decision == "redo" else "用户驳回选型结果"
            if comment:
                msg += f" (审核意见: {comment[:80]})"
                # v4.00.03: 从审核意见提取参数进行修正
                extracted = self._extract_params_from_comment(comment, state)
                if extracted:
                    msg += f" [提取到参数修正: {extracted}]"
            self._push_progress("human_approval",
                                f"{msg}, 回退方案确认重新审阅...", "warn")
            return ["revise"]
        return ["proceed"]

    def _set_project_id(self, project_id: str):
        for agent in [self.req_agent, self.design_agent, self.review_agent,
                      self.report_agent, self.reverse_reviewer,
                      self.reality_checker, self.sandbox_simulator,
                      self.devils_advocate]:
            agent.project_id = project_id
        # v2.05.03: 同步成本追踪器project_id
        self._get_cost_tracker().project_id = project_id

    # ==================== v4.00.03: 流程时间统计 ====================

    # 阶段映射: 节点名 → 阶段名
    _PHASE_MAP = {
        "parse_requirement": "用户需求分析",
        "hc_requirement": "用户需求分析",
        "generate_base_params": "方案生成",
        "hc_base_param": "方案生成",
        "generate_designs": "方案生成",
        "hc_design": "方案生成",
        "review_solutions": "方案评审",
        "convergence_review": "方案评审",
        "decision_gate": "方案评审",
        "human_approval": "方案评审",
        "generate_report": "报告撰写",
        "hc_report": "报告撰写",
    }

    def generate_timing_summary(self) -> dict:
        """v4.00.04: 生成各阶段时间统计 (使用线程本地的 cost_tracker, 支持并发)"""
        return self._generate_timing_summary_for(self._get_cost_tracker())

    def _generate_timing_summary_for(self, tracker) -> dict:
        """v4.00.07: 生成时间统计 (接受指定的 cost_tracker, 用于并发隔离)"""
        events = tracker.events

        phases = {}
        phase_order = ["用户需求分析", "方案生成", "方案评审", "报告撰写"]

        for e in events:
            step = e.get("step", "")
            phase = self._PHASE_MAP.get(step, "其他")
            if phase not in phases:
                phases[phase] = {
                    "llm_ms": 0,            # 大模型耗时 (AI-Local/AI-Cloud)
                    "other_system_ms": 0,   # 系统其他耗时 (Computer 求解/查询等)
                    "system_ms": 0,         # = llm_ms + other_system_ms
                    "human_ms": 0,          # 人工交互耗时
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "cost": 0.0,
                    "step_count": 0,
                }
            executor = e.get("executor", "")
            duration = e.get("duration_ms", 0)
            if executor == "Human":
                phases[phase]["human_ms"] += duration
            elif executor in ("AI-Local", "AI-Cloud"):
                phases[phase]["llm_ms"] += duration
                phases[phase]["system_ms"] += duration
            else:  # Computer / Solver / Query / 等其他系统任务
                phases[phase]["other_system_ms"] += duration
                phases[phase]["system_ms"] += duration
            phases[phase]["tokens_in"] += e.get("tokens_in", 0)
            phases[phase]["tokens_out"] += e.get("tokens_out", 0)
            phases[phase]["cost"] += e.get("cost", 0)
            phases[phase]["step_count"] += 1

        # 计算总计
        total_llm = sum(p["llm_ms"] for p in phases.values())
        total_other_system = sum(p["other_system_ms"] for p in phases.values())
        total_system = total_llm + total_other_system
        total_human = sum(p["human_ms"] for p in phases.values())
        total_tokens_in = sum(p["tokens_in"] for p in phases.values())
        total_tokens_out = sum(p["tokens_out"] for p in phases.values())
        total_cost = sum(p["cost"] for p in phases.values())

        # 按阶段顺序排列
        ordered_phases = []
        for name in phase_order:
            if name in phases:
                p = phases[name]
                ordered_phases.append({
                    "phase": name,
                    "llm_ms": p["llm_ms"],                          # 大模型耗时
                    "other_system_ms": p["other_system_ms"],        # 系统其他耗时
                    "system_ms": p["system_ms"],                    # 系统耗时 (= LLM+其他)
                    "human_ms": p["human_ms"],                      # 人工交互耗时
                    "subtotal_ms": p["system_ms"] + p["human_ms"],  # 阶段小计
                    "tokens_in": p["tokens_in"],
                    "tokens_out": p["tokens_out"],
                    "tokens_total": p["tokens_in"] + p["tokens_out"],
                    "cost": round(p["cost"], 4),
                    "step_count": p["step_count"],
                })

        return {
            "phases": ordered_phases,
            "total": {
                "llm_ms": total_llm,
                "other_system_ms": total_other_system,
                "system_ms": total_system,
                "human_ms": total_human,
                "subtotal_ms": total_system + total_human,
                "tokens_in": total_tokens_in,
                "tokens_out": total_tokens_out,
                "tokens_total": total_tokens_in + total_tokens_out,
                "cost": round(total_cost, 4),
            },
            "project_id": tracker.project_id,
        }

    # ==================== v4.00.07: 线程本地状态访问器 ====================
    def _get_progress_queue(self):
        """获取当前工作流的进度队列 (线程本地, 支持并发隔离)"""
        return getattr(self._tl, 'progress_queue', None) or self._progress_queue

    def _get_cost_tracker(self):
        """获取当前工作流的成本追踪器 (线程本地, 支持并发隔离)"""
        return getattr(self._tl, 'cost_tracker', None) or self.cost_tracker

    # ==================== v4.01.029: 运营质量指标 ====================
    def _record_quality_event(self, project_id: str, checkpoint_type: str,
                              decision: str, checkpoint_id: str = "",
                              total_params: int = 0, override_applied: int = 0,
                              corrected_keys=None, is_timeout: bool = False):
        """记录人机检查点决策事件 (人工修正率/返工率数据源, 失败静默)"""
        if not _QM_AVAILABLE:
            return
        try:
            _qm_record(project_id=project_id, checkpoint_type=checkpoint_type,
                       decision=decision, checkpoint_id=checkpoint_id,
                       total_params=total_params,
                       override_applied=override_applied,
                       corrected_keys=corrected_keys or [],
                       is_timeout=is_timeout)
        except Exception:
            pass

    @staticmethod
    def _read_override_counts(checkpoint_id: str):
        """只读统计检查点上的人工覆盖项 (不改变任何业务数据) -> (count, keys)"""
        try:
            cp_full = hc.get_checkpoint(checkpoint_id) or {}
            od = (cp_full.get("context") or {}).get("override_data") or {}
            if not isinstance(od, dict):
                return 0, []
            keys = []
            for _k, _v in od.items():
                _val = _v.get("value") if isinstance(_v, dict) else _v
                if _val in (None, ""):
                    continue
                keys.append(_k)
            return len(keys), keys
        except Exception:
            return 0, []

    # ==================== 进度回调 ====================
    def _push_progress(self, step: str, message: str, level: str = "info"):
        """推送子步骤进度到队列 (供 run_streaming 的消费者读取)"""
        self._get_progress_queue().put({
            "type": "progress",
            "step": step,
            "message": message,
            "level": level,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })

    # v4.02.06 (问题② Fix B): step(节点名) → checkpoint_type(DB 类型) 映射,
    # 两者命名不同 (如 step=hc_requirement / checkpoint_type=requirement_confirm),
    # 回查 human_checkpoints 表时必须换算, 否则永远匹配不到 pending 记录。
    STEP_TO_CHECKPOINT_TYPE = {
        "hc_requirement": "requirement_confirm",
        "hc_base_param": "base_param_select",
        "hc_design": "design_review",
        "decision_gate": "design_final_select",
        "hc_report": "report_review",
    }

    def _lookup_pending_checkpoint_id(self, step: str, context: dict = None) -> str:
        """v4.02.06 (问题② Fix B): 按 (project_id, checkpoint_type, status='pending')
        从 human_checkpoints 表回查最新待处理检查点 ID。

        用于 checkpoint_id 意外为空时兜底补全, 杜绝客户端收到空 id 后报
        「无效的检查点 ID，无法提交决策」。project_id 优先取 context, 回落
        self._current_project_id; 命中不到精确 checkpoint_type 时取最新一条 pending。
        """
        try:
            pid = ""
            if isinstance(context, dict):
                pid = str(context.get("project_id") or "").strip()
            if not pid:
                pid = str(getattr(self, "_current_project_id", "") or "").strip()
            rows = hc.list_pending_checkpoints(pid or None) or []
            if not rows:
                return ""
            want = self.STEP_TO_CHECKPOINT_TYPE.get(step, step)
            for r in rows:  # list_pending_checkpoints 已按 created_at DESC 排序
                if want and r.get("checkpoint_type") == want:
                    return str(r.get("id") or "")
            return str(rows[0].get("id") or "")
        except Exception as e:
            print(f"[orchestrator][Fix B] 回查 pending 检查点失败: {e}")
            return ""

    def _push_human_input_required(self, step: str, checkpoint_id: str,
                                    title: str, description: str,
                                    context: dict, timeout_seconds: int,
                                    default_action: str):
        """v2.05.06: 推送人机交互事件到进度队列 (供客户端显示交互窗口)

        v4.02.06 (问题② Fix B): 强制携带 checkpoint_id — 空/None 时先按
        (project_id, checkpoint_type, status='pending') 回查补全后再下发,
        杜绝客户端收到空 id 触发「无效的检查点 ID」。
        """
        _cid = str(checkpoint_id or "").strip()
        if not _cid:
            _cid = self._lookup_pending_checkpoint_id(step, context)
            if _cid:
                self._push_progress(
                    step, f"[Fix B] 事件缺 checkpoint_id, 已从 DB 回查补全: {_cid}", "warning")
            else:
                self._push_progress(
                    step, "[Fix B] 事件缺 checkpoint_id 且回查失败, 客户端可能无法提交决策", "error")
        self._get_progress_queue().put({
            "type": "human_input_required",
            "step": step,
            "checkpoint_id": _cid,
            "title": title,
            "description": description,
            "context": context,
            "timeout_seconds": timeout_seconds,
            "default_action": default_action,
            "options": sorted(hc.VALID_DECISIONS),
        })

    def _push_human_checkpoint(self, step, project_id, checkpoint_type, title, description,
                               db_context, ui_context=None, timeout_seconds=0,
                               default_action="auto_approve") -> str:
        """v4.06: 统一 5 个人工检查点的「DB 上下文(checkpoint)」与「SSE 上下文(UI)」同步。

        此前 hc_requirement 单独构建 context_for_ui, 与存 DB 的 checkpoint context 字段漂移;
        其余检查点虽同源但各自手写, 难以保证一致。本方法以单一来源构建:
        - db_context 存入人类检查点 (供后续 get_checkpoint 读取/审计);
        - ui_context 缺省时直接复用 db_context, 保证客户端弹窗与 DB 存储字段完全一致。
        返回新建的 checkpoint_id。
        """
        _cid = hc.create_checkpoint(
            project_id=project_id,
            checkpoint_type=checkpoint_type,
            title=title,
            description=description,
            context=db_context,
            timeout_seconds=timeout_seconds,
            default_action=default_action,
        )
        # v4.02.06 (问题② Fix B): create_checkpoint 异常返回空 id 时立即回查补全,
        # 保证 checkpoint_id 在「创建 → 推送 → 客户端提交」全链路非空。
        _cid = str(_cid or "").strip()
        if not _cid:
            _cid = self._lookup_pending_checkpoint_id(step, {"project_id": project_id})
            self._push_progress(
                step,
                f"[Fix B] 检查点创建未返回 id, 回查结果: {_cid or '仍为空'}",
                "warning" if _cid else "error")
        self._push_progress(step, f"人工检查点已创建: {_cid}", "info")
        self._push_human_input_required(
            step=step,
            checkpoint_id=_cid,
            title=title,
            description=description,
            context=ui_context if ui_context is not None else db_context,
            timeout_seconds=timeout_seconds,
            default_action=default_action,
        )
        return _cid

    # ==================== v4.06-P4: 统一回退策略 ====================
    # 集中「回退粒度」(超时给哪些关键字段补默认值) 与「上限」(累计重做次数上限),
    # 避免 5 个人工检查点各自硬编码回退逻辑造成口径漂移与无限回退死循环。
    CHECKPOINT_FALLBACK_POLICY = {
        # 需求确认: 超时未确认电机类型 → 默认"异步电动机"放行; 关键字段缺失 → 阻断重做
        "hc_requirement": {"max_redo": 5, "timeout_defaults": {"motor_type": "异步电动机"},
                           "manual_missing_action": "redo"},
        "hc_base_param":  {"max_redo": 5, "timeout_defaults": {}, "manual_missing_action": "redo"},
        "hc_design":      {"max_redo": 5, "timeout_defaults": {}, "manual_missing_action": "redo"},
        "human_approval": {"max_redo": 3, "timeout_defaults": {}, "manual_missing_action": "redo"},
        "hc_report":      {"max_redo": 3, "timeout_defaults": {}, "manual_missing_action": "redo"},
    }

    def _apply_checkpoint_fallback(self, step, state, decision, specs, is_timeout,
                                   missing_critical=None):
        """v4.06-P4: 统一回退策略函数 — 集中管理各人工检查点的「回退粒度」与「上限」。

        回退粒度 (granulity): 超时(auto_confirm)时, 用 policy.timeout_defaults 给未确认的关键字段
            补默认值 (如 hc_requirement 的 motor_type 默认"异步电动机"), 避免单点阻断流程。
        上限 (cap): 累计重做次数(redo_count + design_redo_count)达到 policy.max_redo 时,
            强制放行(approve), 防止无限回退死循环。

        参数:
            step: 检查点名 (hc_requirement / hc_base_param / hc_design / human_approval / hc_report)
            state: WorkflowState (读 redo_count / design_redo_count)
            decision: 当前决策 (approve/auto_confirm/proceed/reject/redo)
            specs: 当前规格 dict (可被 timeout_defaults 原地修改)
            is_timeout: 是否超时
            missing_critical: 非超时状态下是否仍有未确认关键字段 (用于阻断重做)
        返回: (decision, specs, message)
        """
        policy = self.CHECKPOINT_FALLBACK_POLICY.get(step, {})
        max_redo = int(policy.get("max_redo", 5))
        defaults = policy.get("timeout_defaults", {}) or {}
        message = ""

        # ── 上限: 重做次数保护 (达上限强制放行, 防死循环) ──
        _redo = int(state.get("redo_count", 0) or 0)
        _dredo = int(state.get("design_redo_count", 0) or 0)
        if _redo + _dredo >= max_redo:
            if decision in ("reject", "redo"):
                decision = "approve"
                message = f"重做已达上限({_redo + _dredo}/{max_redo}), 强制放行"
            return decision, specs, message

        # ── 回退粒度 ──
        if is_timeout:
            for _k, _v in defaults.items():
                if specs.get(_k) in (None, "", "待确认", "未知"):
                    specs[_k] = _v
                    message = (message + "; " if message else "") + f"超时默认{_k}={_v}"
            # 超时语境下 reject/redo 视为未生效, 默认放行
            if decision in ("reject", "redo"):
                decision = "approve"
                message = (message + " " if message else "") + "超时默认执行放行"
        elif missing_critical:
            # 非超时但关键字段未确认 → 阻断重做 (除非已在上限)
            decision = policy.get("manual_missing_action", "redo")
            message = f"关键字段未确认({missing_critical}), 阻断并要求明确"

        return decision, specs, message

    # ==================== v4.04: 案例电机加载 ====================
    def _node_load_case_motor(self, state: WorkflowState) -> dict:
        """v4.04: 从案例电机库加载参数, 预填充 specs 和 raw_text"""
        case_id = state.get("case_motor_id", 0)
        if not case_id or not _CM_DB_OK:
            # 无案例参考, 直接进入需求解析
            # v4.01.052: 返回空 _2_1_data 占位, 让客户端 2.1 段可识别"无案例预处理"
            return {
                "_2_1_data": {
                    "case_motor_ref": {},
                    "pre_specs": {},
                    "pre_raw_text": "",
                    "is_preprocessed": False,
                }
            }

        self._push_progress("load_case_motor", f"正在加载案例电机 (ID={case_id})...")
        try:
            case = get_case_motor(case_id)
            if not case:
                self._push_progress("load_case_motor",
                                    f"案例电机 ID={case_id} 不存在, 跳过", "warn")
                return {
                    "_2_1_data": {
                        "case_motor_ref": {},
                        "pre_specs": {},
                        "pre_raw_text": "",
                        "is_preprocessed": False,
                    }
                }

            # 构建详细需求描述文本
            power = case.get("rated_power") or "?"
            voltage = case.get("rated_voltage") or 380
            speed = case.get("rated_speed") or "?"
            poles = case.get("poles") or "?"
            eff = case.get("efficiency") or "?"
            pf = case.get("power_factor") or "?"
            motor_type = case.get("motor_type", "三相异步电动机")
            case_code = case.get("case_code", "")

            parts = [
                f"参考案例电机 {motor_type}",
                f"(案例编号: {case_code})" if case_code else "",
                f"设计一台 {power}kW、{voltage}V、{speed}r/min、{poles}极 {motor_type}",
            ]
            # 补充详细参数
            detail_parts = []
            if eff != "?":
                detail_parts.append(f"目标效率 >= {eff}%")
            if pf != "?":
                detail_parts.append(f"功率因数 >= {pf}")
            for key, label in [
                ("stator_od", "定子外径"), ("stator_id", "定子内径"),
                ("rotor_od", "转子外径"), ("core_length", "铁心长度"),
                ("air_gap", "气隙长度"), ("slots", "槽数"),
            ]:
                val = case.get(key)
                if val:
                    detail_parts.append(f"{label} {val}mm")
            for key, label in [
                ("insulation_class", "绝缘等级"), ("protection_class", "防护等级"),
                ("cooling_method", "冷却方式"), ("mounting", "安装方式"),
            ]:
                val = case.get(key)
                if val:
                    detail_parts.append(f"{label} {val}")
            if detail_parts:
                parts.append(", ".join(detail_parts))

            enhanced_text = ". ".join(p for p in parts if p) + "."

            # 预填充 specs (已知参数)
            pre_specs = {}
            field_map = {
                "rated_power": "power_kw", "rated_voltage": "voltage_v",
                "rated_speed": "speed_rpm", "poles": "poles",
                "rated_current": "rated_current_a", "efficiency": "efficiency",
                "power_factor": "power_factor", "frequency": "frequency",
                "stator_od": "stator_od", "stator_id": "stator_id",
                "rotor_od": "rotor_od", "core_length": "core_length",
                "air_gap": "air_gap", "slots": "slots",
                "motor_type": "motor_type",
            }
            for cm_field, spec_field in field_map.items():
                val = case.get(cm_field)
                if val is not None and val != "":
                    pre_specs[spec_field] = val

            # 案例引用信息
            case_ref = {
                "case_id": case_id,
                "case_code": case_code,
                "motor_type": motor_type,
                "series_id": case.get("series_id", ""),
                "completeness": case.get("completeness", 0),
            }

            self._push_progress("load_case_motor",
                                f"案例电机加载完成: {motor_type} {power}kW {poles}极, "
                                f"预填充 {len(pre_specs)} 个参数", "success")

            # 如果原 raw_text 为空, 用增强文本替代; 否则保留原文本
            raw_text = state.get("raw_text", "")
            if not raw_text.strip():
                raw_text = enhanced_text
            else:
                raw_text = enhanced_text + "\n\n" + raw_text

            # v4.01.052: 2.1 预处理数据 (供解析结果展示: 案例电机参数预填充 + 增强文本)
            preprocessed_data = {
                "case_motor_ref": case_ref,
                "pre_specs": pre_specs,
                "pre_raw_text": enhanced_text,
                "is_preprocessed": True,
            }

            return {
                "raw_text": raw_text,
                "specs": pre_specs,
                "case_motor_ref": case_ref,
                "_2_1_data": preprocessed_data,
            }
        except Exception as e:
            self._push_progress("load_case_motor",
                                f"案例电机加载失败: {e}", "error")
            return {"errors": [f"案例电机加载失败: {e}"],
                    "_2_1_data": {
                        "case_motor_ref": {},
                        "pre_specs": {},
                        "pre_raw_text": "",
                        "is_preprocessed": False,
                    }}

    # ==================== 放大轨 ====================
    def _node_parse_requirement(self, state: WorkflowState) -> dict:
        self._push_progress("parse_requirement", "正在解析客户需求文本...")
        # v4.01.051: 重解析时复用已有 project_id, 避免分裂
        existing_pid = state.get("project_id", "")
        if existing_pid:
            project_id = existing_pid
            self._push_progress("parse_requirement",
                                f"重解析, 复用项目ID: {project_id}", "info")
        else:
            project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        self._current_project_id = project_id
        self._set_project_id(project_id)

        # v4.00.01: 如果是返回重做, 将审核意见附加到原始需求文本中
        raw_text = state["raw_text"]
        review_comment = state.get("human_comment", "")
        if review_comment and state.get("human_decision") in ("reject", "redo"):
            raw_text = raw_text + f"\n\n【审核补充意见】{review_comment}"
            self._push_progress("parse_requirement",
                                f"包含审核补充意见: {review_comment[:60]}", "info")

        _start_ts = time.time()
        result = self.req_agent.safe_run(raw_text=raw_text, review_comment=review_comment)
        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            self._push_progress("parse_requirement", f"需求解析失败: {result['error']}", "error")
            record_step_metric(project_id, "parse_requirement", "RequirementAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=result["error"][:200])
            return {"errors": [result["error"]]}

        # v2.04: 提取场景识别和分层规格
        scenario_info = result.get("scenario_info", {})
        layered_specs = result.get("layered_specs", {})
        scenario = scenario_info.get("scenario", "catalog")
        torque = scenario_info.get("corrected_torque_nm", "?")

        self._push_progress("parse_requirement",
                            f"需求解析完成: 类型={result['specs'].get('motor_type', '异步电动机')}, "
                            f"功率={result['specs'].get('power_kw', '?')}kW, "
                            f"电压={result['specs'].get('voltage_v', '?')}V, "
                            f"转速={result['specs'].get('speed_rpm', '?')}rpm, "
                            f"转矩={torque}Nm, 选型模式={scenario}")

        # 记录运营指标
        record_step_metric(project_id, "parse_requirement", "RequirementAgent",
                           duration_ms=_duration_ms, quality_score=result.get("confidence", 0) * 100,
                           quality_issues=json.dumps(result.get("extraction_notes", []),
                                                     ensure_ascii=False)[:200])

        # v2.05.03: 记录AI成本 (本地)
        # v4.06-P1: 优先用 RequirementAgent 真实 token 计量 (ChatOllama usage_metadata), 无则回退启发式
        tokens_in = result.get("tokens_in", 0) or _estimate_tokens(state["raw_text"])
        tokens_out = result.get("tokens_out", 0) or _estimate_tokens(result)
        self._get_cost_tracker().project_id = project_id
        self._get_cost_tracker().record_ai_step("parse_requirement", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type="local",
                                         duration_ms=_duration_ms,
                                         detail={"confidence": result.get("confidence", 0)})

        # v4.00.03: 如果有审核意见提取的参数修正, 覆盖 AI 解析的 specs
        specs = result["specs"]
        if review_comment:
            extracted = self._extract_params_from_comment(review_comment, {"specs": specs})
            if extracted:
                for key, val in extracted.items():
                    specs[key] = val
                    self._push_progress("parse_requirement",
                                        f"审核意见参数覆盖: {key}={val}", "info")

        # v4.04: 案例电机预填充参数 — 保留案例已知值, 仅补充 AI 解析的新字段
        existing_specs = state.get("specs", {})
        if existing_specs and state.get("case_motor_id"):
            for key, val in existing_specs.items():
                if key not in specs:
                    specs[key] = val
            self._push_progress("parse_requirement",
                                f"已合并案例电机预填充参数 ({len(existing_specs)}个)", "info")

        # v4.01.052: 读取 2.1 预处理数据 (从 state 中获取, _node_load_case_motor 已写入)
        _2_1_data = state.get("_2_1_data", {
            "case_motor_ref": {},
            "pre_specs": {},
            "pre_raw_text": "",
            "is_preprocessed": False,
        })

        # v4.07 (F9): revise 重跑时回灌需求确认阶段人工覆盖数据, 避免重做后编辑丢失
        _req_ov = state.get("requirement_override_data", {})
        if _req_ov and isinstance(_req_ov, dict):
            _applied = self._apply_override_to_dict(specs, _req_ov)
            if _applied:
                self._push_progress("parse_requirement",
                                    f"已回灌人工覆盖 {_applied} 项到需求规格", "info")

        return {"specs": specs, "confidence": result["confidence"],
                "scenario_info": scenario_info,
                "layered_specs": layered_specs,
                "project_id": project_id,
                "extraction_notes": result.get("extraction_notes", []),
                "pipeline_steps": result.get("pipeline_steps", []),
                # v4.01.052: 2.1 预处理数据 (供解析结果展示)
                "_2_1_data": _2_1_data,
                "timestamp": datetime.now().isoformat()}

    # ==================== v2.05.06: 需求确认检查点 ====================
    def _node_hc_requirement(self, state: WorkflowState) -> dict:
        """v2.05.06: 需求分析师输出后的人机交互检查点"""
        project_id = state.get("project_id", "")
        specs = state.get("specs", {})
        confidence = state.get("confidence", 0)
        layered = state.get("layered_specs", {})

        # v4.01.028: 计算参数来源标签/追问答/是否需要确认电机类型, 供客户端颜色分区与硬约束
        from requirement_agent import RequirementAgent
        param_sources = RequirementAgent._build_param_sources(layered)
        pending_questions = []
        for _layer in ("L1", "L2", "L3"):
            pending_questions.extend(layered.get(_layer, {}).get("_pending_questions", []))
        require_motor_type = layered.get("L1", {}).get("motor_type") in (None, "", "待确认", "未知")

        self._push_progress("hc_requirement", "创建需求确认检查点...")

        # v4.06: 统一 DB 上下文与 SSE 上下文 (单一来源, 字段强制同步)
        layered = state.get("layered_specs", {})
        catalog_options = layered.get("_catalog_options", {})
        human_ctx = {
            "specs": specs,
            "confidence": confidence,
            "scenario_info": state.get("scenario_info", {}),
            "scenario": state.get("scenario_info", {}).get("scenario", ""),
            "layered_specs": layered,
            "catalog_options": catalog_options,
            "inference_notes": state.get("extraction_notes", []),
            "param_sources": param_sources,
            "pending_questions": pending_questions,
            "require_motor_type": require_motor_type,
            "pipeline_steps": state.get("pipeline_steps", []),
        }
        # v4.01.051: 案例电机 Top 3 匹配结果
        _case_matches = layered.get("_case_motor_matches", [])
        if _case_matches:
            human_ctx["case_motor_matches"] = _case_matches
        if catalog_options:
            human_ctx["hint"] = f"检测到中心高{catalog_options.get('center_height', '')}mm, 请选择极数和功率"

        checkpoint_id = self._push_human_checkpoint(
            step="hc_requirement",
            project_id=project_id,
            checkpoint_type="requirement_confirm",
            title="需求确认",
            description="请审阅需求分析师解析的规格,确认或补充信息",
            db_context=human_ctx,
            timeout_seconds=hc.DEFAULT_TIMEOUTS["requirement_confirm"],
            default_action="auto_approve",
        )

        # 记录人工等待成本
        self._get_cost_tracker().project_id = project_id
        human_event = self._get_cost_tracker().start_human_step("hc_requirement",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            if remaining % 60 == 0 and remaining > 0:
                self._push_progress("hc_requirement",
                                    f"需求确认倒计时: 剩余 {remaining//60} 分钟", "info")

        # v5.1: 本阶段 LLM 工作已完成, 人工检查点即将阻塞等待; 显式释放 Ollama 模型
        #        避免模型在人工思考期间空占显存 (流程内连续阶段仍靠 keep_alive=5m 不反复重载)
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 检查点释放模型失败(忽略): %s", _e)

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_approve")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        # v4.01.028: 应用人工修改(override_data)到 specs — 使需求确认界面编辑真正生效
        applied = 0
        corrected_keys = []
        try:
            cp_full = hc.get_checkpoint(checkpoint_id)
            override_data = (cp_full or {}).get("context", {}).get("override_data", {})
            if override_data and isinstance(override_data, dict):
                for _k, _v in override_data.items():
                    _val = _v.get("value") if isinstance(_v, dict) else _v
                    if _val in (None, ""):
                        continue
                    # v4.02.62 (故障修复 #2 P0-B): 跳过占位符 "—"/"未知"/"N/A" ——
                    #   杜绝人工未编辑字段的占位符写入 specs, 污染下游数值运算
                    if self._is_placeholder_value(_val):
                        continue
                    # v4.01.029: 仅当值实际发生变化才算"人工修正"
                    if str(specs.get(_k, "")) == str(_val):
                        continue
                    specs[_k] = _val
                    corrected_keys.append(_k)
                    applied += 1
                if applied:
                    self._push_progress("hc_requirement",
                                        f"已应用人工修改 {applied} 项到需求规格", "info")
        except Exception as e:
            self._push_progress("hc_requirement", f"应用人工修改失败: {e}", "warn")

        # v4.01.051: P0 硬约束 — 电机类型未确认处理 (v4.06-P4: 统一回退策略函数集中治理)
        #   超时(auto_confirm): 默认设为"异步电动机"并继续, 避免阻断流程
        #   非超时用户主动确认: 若仍未确认则 redo
        _mt_missing = specs.get("motor_type") in (None, "", "待确认", "未知")
        decision, specs, _fb_msg = self._apply_checkpoint_fallback(
            "hc_requirement", state, decision, specs, is_timeout,
            missing_critical=("motor_type" if _mt_missing else None))
        if _fb_msg:
            self._push_progress("hc_requirement", f"⚠️ 回退策略: {_fb_msg}", "warn")

        self._get_cost_tracker().end_human_step(human_event, decision=decision)

        # v4.01.029: 运营质量指标 — 人工修正率 / 返工率数据源
        _total_params = len(param_sources) or len(specs)
        self._record_quality_event(project_id, "requirement_confirm", decision,
                                   checkpoint_id=checkpoint_id,
                                   total_params=_total_params,
                                   override_applied=applied,
                                   corrected_keys=corrected_keys,
                                   is_timeout=is_timeout)
        if _total_params:
            self._push_progress(
                "hc_requirement",
                f"运营指标: 人工修正 {applied}/{_total_params} 项 "
                f"({applied / max(_total_params, 1) * 100:.0f}%), 决策={decision}",
                "info")

        if is_timeout:
            self._push_progress("hc_requirement",
                                f"需求确认超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("hc_requirement",
                                f"收到人工决策: {decision}", "success")

        # v4.07 (F8): 需求确认阶段返回重做计数
        _requirement_redo_count = int(state.get("requirement_redo_count", 0) or 0)
        if decision in ("reject", "redo"):
            _requirement_redo_count += 1

        return {"human_decision": decision,
                "human_comment": comment,
                "hc_checkpoint_id": checkpoint_id,
                "specs": specs,
                "requirement_redo_count": _requirement_redo_count,
                "requirement_override_data": override_data}

    # ==================== v4.02: 基准参数生成 ====================
    def _node_generate_base_params(self, state: WorkflowState) -> dict:
        """v4.02: 生成推荐电机基准参数及变化范围 (方案生成师生成, 矩阵生成前)"""
        specs = state.get("specs", {})
        scenario_info = state.get("scenario_info", {})
        project_id = state.get("project_id", "")

        # v4.02: 如果是基准参数被驳回重做, 从审核意见提取参数修正
        review_comment = state.get("human_comment", "")
        if review_comment and state.get("human_decision") in ("reject", "redo"):
            self._push_progress("generate_base_params",
                                f"包含基准参数审核意见: {review_comment[:60]}", "info")
            # 提取参数修正 (复用 _extract_params_from_comment)
            extracted = self._extract_params_from_comment(review_comment, {"specs": specs})
            if extracted:
                for key, val in extracted.items():
                    specs[key] = val
                    self._push_progress("generate_base_params",
                                        f"审核意见参数覆盖: {key}={val}", "warn")

        self._push_progress("generate_base_params", "正在生成推荐电机基准参数...")

        _start_ts = time.time()

        # 1. 从物理内核获取场景信息
        try:
            from physics_kernel import (determine_scenario, find_closest_catalog,
                                        _load_catalog, compute_torque,
                                        scale_dims_by_torque, _as_int, _as_float)
            from Scripts.physics_l0 import _is_pm_specs
        except ImportError:
            determine_scenario = None
            find_closest_catalog = None
            _load_catalog = None
            compute_torque = None
            scale_dims_by_torque = None
            _as_int = None
            _as_float = None

        # v4.02.62 (故障修复 #2 P0-A): 数值类型防御收敛 —— 杜绝 specs 残留的
        #   "—"/"未知" 等占位符字符串污染下游 int-str 相减, 统一收敛为数值
        poles = _as_int(scenario_info.get("poles") or specs.get("poles", 4), 4)
        power_kw = _as_float(specs.get("power_kw", 0), 0.0)
        speed_rpm = _as_float(specs.get("speed_rpm", 0), 0.0)
        voltage_v = _as_float(specs.get("voltage_v", 0), 0.0)
        motor_type = specs.get("motor_type", "异步电动机")

        # 2. 计算目标转矩
        if compute_torque and power_kw and speed_rpm:
            target_torque = compute_torque(power_kw, speed_rpm)
        else:
            target_torque = 9550 * power_kw / speed_rpm if speed_rpm else 0

        # 3. 查询数据库Top5参考电机
        ref_motors = []
        if _load_catalog and find_closest_catalog:
            catalog = _load_catalog()
            # 按极数过滤
            candidates = [e for e in catalog if e.get("poles") == poles]
            if not candidates:
                candidates = list(catalog)
            # v4.05: 多级优先级排序 — 极数 → 扭矩 → 转速 → 电压 → 冷却方式 → 其他
            # 已在上方按极数过滤(candidates 默认同极数); 但为兼容"无同极数候选"时
            # 回退到全目录的场景, 仍将极数差值作为第一排序键, 确保优先级严格生效。
            target_cooling = str(specs.get("cooling_method", "IC411") or "IC411")
            for e in candidates:
                # v4.02.62 (故障修复 #2 P1): 目录字段同样做数值收敛兜底 ——
                #   即便目录意外混入字符串, 也保证差值运算永不抛 TypeError
                e_poles = _as_int(e.get("poles"), 0) or 0
                e_torque = _as_float(e.get("torque_nm"), 0.0) or 0.0
                e_speed = _as_float(e.get("speed_rpm"), 0.0) or 0.0
                e_volt = _as_float(e.get("voltage_v"), 0.0) or 0.0   # 目录暂无电压字段 → 退化为中性
                e_cool = str(e.get("cooling") or "")
                _poles_diff = abs(e_poles - poles) if e_poles else 999
                _torque_diff = abs(e_torque - target_torque) if (e_torque and target_torque) else 999
                _speed_diff = abs(e_speed - speed_rpm) if (e_speed and speed_rpm) else 999
                _volt_diff = abs(e_volt - voltage_v) if (e_volt and voltage_v) else 0
                _cool_match = 0 if e_cool.startswith(target_cooling) else 1
                _ie_rank = {"IE1": 1, "IE2": 2, "IE3": 3, "IE4": 4, "IE5": 5}.get(
                    str(e.get("efficiency_ie") or "").upper(), 0)
                _eff = -_ie_rank                          # 能效等级高的优先(其他)
                e["_sort_key"] = (_poles_diff, _torque_diff, _speed_diff,
                                  _volt_diff, _cool_match, _eff)
            candidates.sort(key=lambda x: x.get("_sort_key", (999, 999, 999, 0, 1, 0)))
            # 取Top5
            for e in candidates[:5]:
                ref_motors.append({
                    "model": e.get("model", ""),
                    "power_kw": e.get("power_kw", 0),
                    "poles": e.get("poles", 0),
                    # v4.02.14 (#997): 优先条目自带 voltage_v; Y 系列硬用标准 380V; 最后才回退规格电压
                    # 根因: 此前型谱库缺 voltage_v 字段, 兜底到 specs.voltage_v(=660, 来自设计规格),
                    #        导致 Y 系列(标准 380V)参考电机被误显 660V。现 JSON 已补 voltage_v=380,
                    #        并加系列级兜底, 杜绝系列标准电压被规格电压覆盖。
                    "voltage_v": e.get("voltage_v") or (380 if str(e.get("model", "")).upper().startswith("Y") else voltage_v),
                    "frequency_hz": e.get("frequency_hz") or specs.get("frequency_hz", 50),
                    "speed_rpm": e.get("speed_rpm", 0),
                    "torque_nm": round(e.get("torque_nm", 0), 2),
                    "cooling": e.get("cooling", ""),
                    "efficiency_ie": e.get("efficiency_ie", 0),
                    "stator_od_mm": e.get("stator_od_mm", 0),
                    "stator_inner_dia_mm": e.get("stator_inner_dia_mm", 0),
                    "core_length_mm": e.get("core_length_mm", 0),
                    "efficiency": e.get("efficiency", 0),
                    "current_a": e.get("current_a", 0),
                    "slots_stator": e.get("slots_stator", 0),
                    "slots_rotor": e.get("slots_rotor", 0),
                    "air_gap_mm": e.get("air_gap_mm", 0),
                    "rotor_od_mm": e.get("rotor_od_mm", 0),
                })

        self._push_progress("generate_base_params",
                            f"查询数据库: 找到 {len(ref_motors)} 个参考电机")

        # v4.05: 案例电机深度集成 — 如果有案例电机, 使用其已知尺寸作为基准
        case_motor_ref = state.get("case_motor_ref", {})
        case_motor_id = state.get("case_motor_id", 0)
        case_motor = None
        if case_motor_id and _CM_DB_OK:
            try:
                case_motor = get_case_motor(case_motor_id)
            except Exception:
                pass

        # 提取案例电机已知尺寸 (None=未知)
        case_od = case_motor.get("stator_od") if case_motor else None
        case_id_dim = case_motor.get("stator_id") if case_motor else None
        case_rotor_od = case_motor.get("rotor_od") if case_motor else None
        case_core_l = case_motor.get("core_length") if case_motor else None
        case_air_gap = case_motor.get("air_gap") if case_motor else None
        case_slots_s = case_motor.get("slots_stator") if case_motor else None
        case_slots_r = case_motor.get("slots_rotor") if case_motor else None
        case_eff = case_motor.get("efficiency") if case_motor else None
        case_pf = case_motor.get("power_factor") if case_motor else None
        case_current = case_motor.get("rated_current") if case_motor else None

        # 将案例电机加入参考列表首位
        if case_motor:
            # v4.01.019: 由已知尺寸推算派生尺寸, 补全案例电机参考列
            _case_id_dim = case_id_dim or (case_od * 0.6 if case_od else 0)
            _case_rotor_od = case_rotor_od or (
                (_case_id_dim - 2 * (case_air_gap or 0.5)) if _case_id_dim else 0)
            _case_shaft = _case_rotor_od * 0.35 if _case_rotor_od else 0
            case_entry = {
                "model": f"案例: {case_motor.get('case_code', '')}",
                "motor_type": case_motor.get("motor_type", motor_type),
                "power_kw": case_motor.get("rated_power", 0),
                "poles": case_motor.get("poles", 0),
                "voltage_v": case_motor.get("rated_voltage", 0),
                "speed_rpm": case_motor.get("rated_speed", 0),
                "frequency_hz": case_motor.get("frequency_hz", 50),
                "phases": case_motor.get("phases", 3),
                "torque_nm": round(9550 * (case_motor.get("rated_power", 0)) /
                                   max(case_motor.get("rated_speed", 1), 1), 2),
                "stator_od_mm": case_od or 0,
                "stator_inner_dia_mm": case_id_dim or 0,
                "rotor_od_mm": round(_case_rotor_od, 1),
                "shaft_dia_mm": round(_case_shaft, 1),
                "core_length_mm": case_core_l or 0,
                "air_gap_mm": case_air_gap or 0,
                "efficiency": case_eff or 0,
                "power_factor": case_pf or 0,
                "current_a": case_current or 0,
                "slots_stator": case_slots_s or 0,
                "slots_rotor": case_slots_r or 0,
                "cooling_method": case_motor.get("cooling_method", "IC411"),
                "insulation_class": case_motor.get("insulation_class", "F"),
                "protection_class": case_motor.get("protection_class", "IP55"),
                "altitude_m": case_motor.get("altitude_m", 0),
                "overload_pct": case_motor.get("overload_pct", 0),
                "is_case_motor": True,
            }
            ref_motors.insert(0, case_entry)
            known_dims = len([v for v in [case_od, case_id_dim, case_rotor_od,
                                          case_core_l, case_air_gap, case_slots_s, case_slots_r] if v])
            self._push_progress("generate_base_params",
                                f"案例电机参数集成: {case_motor.get('case_code', '')} "
                                f"({known_dims} 个已知尺寸参数)", "info")

        # 4. 生成推荐基准参数 (全部相关变量)
        # v4.02.09 (提案1 细化): 5 种子加权集成 — 案例种子加权优先, 距 specs 接近度修正
        #   (原 v4.05 为 ref_motors[0] 单点推导; 现改用 extract_seeds 的 5 颗种子做加权质心,
        #    案例源权重高于型谱, 用户选定案例额外 boost, 解决"跨机座号加权无效"风险;
        #    连续键加权均值、离散键加权众数; 质心缺项时回退原单点逻辑)
        _seeds = []
        _weights = []
        _case_share = 0.0
        _seed_src = ""
        _centroid = None
        # v4.02.22 Fix2: 预置离散槽配合质心变量。
        #   原缺陷: _cent_ss_val/_cent_sr_val 仅在 `if _seeds:` 分支(:1807-1808)与
        #   except(:1812-1813) 内赋值, 未像 _centroid/_seed_src/_case_share 那样预置。
        #   当 extract_seeds 返回空种子时(种子扭矩±20%门控后极易发生), if 分支被跳过、
        #   try 正常结束不进 except -> 变量未绑定, 至 :1890 读取触发
        #   UnboundLocalError: cannot access local variable '_cent_ss_val'
        #   表现为「基准参数生成」阶段工作流异常终止。此处预置 None 使回退逻辑(:1892
        #   按极数标准槽配合)正常生效。
        _cent_ss_val = None
        _cent_sr_val = None
        try:
            # v4.02.36 (阶段1): 改用缓存版, 与 generate_designs 共享同一批种子, 消除三处独立抽取的不一致
            from Core.seed_extractor import extract_seeds_cached as _seed_extractor_fn
            # v4.02.90 E: 未显式选定案例电机时不回填案例种子 (case_n=0), 避免案例源主导质心
            _case_selected = bool(case_motor_id and case_motor)
            _sd = _seed_extractor_fn(specs, motor_type=motor_type, total=5,
                                     catalog_n=2, case_n=(3 if _case_selected else 0))
            _seeds = _sd.get("seeds") or []
            if _seeds:
                _cont = ["stator_od", "stator_id", "rotor_od", "shaft_dia",
                         "core_length", "air_gap", "tooth_width", "yoke_thickness"]
                _sp = specs or {}
                _sp_p = _sp.get("poles") or _sp.get("pole_count")
                _sp_w = _sp.get("power_kw") or _sp.get("power")
                for _s in _seeds:
                    # 案例源 ×3, 型谱 ×1; v4.02.90 E: 仅当用户显式选定案例电机时案例才加权
                    _bw = 3.0 if (_s.get("source") == "case" and _case_selected) else 1.0
                    # 用户选定案例额外 ×2 (按 motor_model/motor_name 命中)
                    if (_s.get("source") == "case" and case_motor_id and case_motor and
                            str(_s.get("model", "")) in (str(case_motor.get("motor_model", "")),
                                                          str(case_motor.get("motor_name", "")))):
                        _bw *= 2.0
                    # 距 specs 接近度修正 (>1 越接近)
                    _prox = 1.0
                    _sd_p = _s.get("poles")
                    if _sp_p and _sd_p and float(_sd_p) > 0:
                        _prox *= 1.3 if float(_sd_p) == float(_sp_p) else max(0.5, 1 - abs(float(_sd_p) - float(_sp_p)) * 0.1)
                    _sd_w = _s.get("power_kw")
                    if _sp_w and _sd_w and float(_sd_w) > 0:
                        _ratio = float(_sp_w) / float(_sd_w)
                        _prox *= max(0.5, 1 - abs(_ratio - 1.0) * 0.3)
                    _weights.append(_bw * _prox)
                # 连续键加权均值
                _centroid = {}
                for _k in _cont:
                    _num = 0.0
                    _den = 0.0
                    for _s, _w in zip(_seeds, _weights):
                        _v = _s.get(_k)
                        if _v is not None:
                            try:
                                _num += float(_v) * _w
                                _den += _w
                            except (TypeError, ValueError):
                                pass
                    _centroid[_k] = (_num / _den) if _den > 0 else None
                # 离散键加权众数 (槽数)
                def _weighted_mode(key):
                    _g = {}
                    for _s, _w in zip(_seeds, _weights):
                        _v = _s.get(key)
                        if _v is not None:
                            _g[_v] = _g.get(_v, 0) + _w
                    return max(_g.items(), key=lambda kv: kv[1])[0] if _g else None
                _cent_ss = _weighted_mode("slots_stator")
                _cent_sr = _weighted_mode("slots_rotor")
                _case_share = (
                    sum(w for s, w in zip(_seeds, _weights) if s.get("source") == "case")
                    / sum(_weights)
                ) if _weights else 0.0
                _seed_src = (f"种子加权集成(案例{_sd.get('case_count', 0)}"
                             f"/型谱{_sd.get('catalog_count', 0)}"
                             f"/回填{_sd.get('backfilled', 0)})")
                # 将质心值透传给下方气隙/转子外径/轴径/槽配合
                _cent_ss_val = int(_cent_ss) if _cent_ss is not None else None
                _cent_sr_val = int(_cent_sr) if _cent_sr is not None else None
        except Exception as _e:
            logger.debug("[orchestrator] 种子加权集成失败, 回退单点推导: %s", _e)
            _centroid = None
            _cent_ss_val = None
            _cent_sr_val = None

        # v4.02.90 S1 (基准参数确认修改方案): 主锚定 ref_motors[0] (catalog_ref),
        #   不再以加权种子质心(_centroid)决定基准尺寸 —— 解决质心被单颗案例种子主导
        #   (case_motor_id=0 仍回填案例种子, _case_share≈1.0) 导致的非自洽基准。
        #   主锚 → 案例(选定) → 质心(fallback) → 经验 四级回退。
        ref_entry = ref_motors[0] if ref_motors else {}
        if ref_entry and ref_entry.get("stator_od_mm"):
            base_od = ref_entry["stator_od_mm"]
            base_id = ref_entry.get("stator_inner_dia_mm", base_od * 0.6)
            base_l = ref_entry.get("core_length_mm", 100)
            ref_model = ref_entry.get("model", "")
        elif case_od:
            base_od = case_od
            base_id = case_id_dim or (base_od * 0.6)
            base_l = case_core_l or (base_id * 0.9)
            ref_model = f"案例: {case_motor.get('case_code', '')}"
        elif _centroid and all(_centroid.get(k) is not None for k in ("stator_od", "stator_id", "core_length")):
            base_od = _centroid["stator_od"]
            base_id = _centroid["stator_id"]
            base_l = _centroid["core_length"]
            ref_model = _seed_src or "种子加权集成"
        else:
            # 类比推算
            base_od = 100 + (target_torque ** 0.3) * 30 if target_torque else 200
            base_id = base_od * 0.6
            base_l = base_id * 0.9
            ref_model = "经验估算"

        # v4.02.90 S1: 基准来源 = 主锚 catalog_ref (ref_motors[0]) 最先判定
        if ref_entry and ref_entry.get("stator_od_mm"):
            base_motor_source = "catalog_ref"
        elif case_od:
            base_motor_source = "case_motor"
        elif _centroid and all(_centroid.get(k) is not None for k in ("stator_od", "stator_id", "core_length")):
            base_motor_source = "seed_centroid"
        else:
            base_motor_source = "experience"
        _primary_ref = ref_motors[0] if ref_motors else {}

        # ── v4.02.22 Fix4 (#1125): 基准尺寸 D²L 物理对账 ──
        # 缺陷: 上面四路回退**一律直接沿用参考电机的原始尺寸**, 从不按目标转矩做量级校验。
        #   实测 500kW/250rpm/6极 船推: 目标 T = 9550×500/250 = 19100 N·m,
        #   而锚定的 Y280M-6 基准 T = 535.97 N·m → K_V = 35.64, 偏差 3465% ≫ 20%,
        #   却沿用 D=325mm / L=230mm (D²L=0.0243m³, 实需 0.8658m³), 下游几何量级整体
        #   偏小约 35.6 倍 —— 是 RMxprt 全池 solve_failed / power=0 的结构性源头之一。
        # 修复: 偏差 > 20% 时按 **D²L = kT** 且 **λ = L/D** 联合反解基准值与范围
        #   (公式与双路交叉校验详见 physics_kernel.scale_dims_by_torque)。
        _d2l_scale = None
        try:
            # 参考电机额定转矩: 铭牌(功率/转速)优先 → 型谱自带 torque_nm → 由几何反推
            _ref_tq = 0.0
            for _d, _pk, _sk in (
                (case_motor or {}, "rated_power_kw", "rated_speed_rpm"),
                (case_motor or {}, "power_kw", "speed_rpm"),
                (ref_entry or {}, "power_kw", "rated_speed_rpm"),
                (ref_entry or {}, "power_kw", "speed_rpm"),
                (_primary_ref or {}, "power_kw", "rated_speed_rpm"),
            ):
                _p = float(_d.get(_pk) or 0)
                _n = float(_d.get(_sk) or 0)
                if _p > 0 and _n > 0:
                    _ref_tq = 9550.0 * _p / _n
                    break
            if _ref_tq <= 0:
                for _d in (ref_entry or {}, _primary_ref or {}):
                    if float(_d.get("torque_nm") or 0) > 0:
                        _ref_tq = float(_d["torque_nm"])
                        break

            if scale_dims_by_torque and target_torque and base_id and base_l:
                # 保留参考电机的径比, 缩放后按同一比例还原外径
                _raw_id_od = (float(base_id) / float(base_od)) if (base_od and base_id) else 0.6
                # v4.02.87 (绝对法 D³L 反解 · item 1): id/od 比钳制到机型合法带,
                #   杜绝退化种子(如 0.75)把非法径比透传到全部 L0 候选 → 第8项全量淘汰。
                #   与 physics_l0._score_candidate 第8项 _idr_hi 口径一致 (异步 0.78 / PMSM 0.80)。
                _is_pm = bool(_is_pm_specs(specs)) if _is_pm_specs else False
                _idr_lo, _idr_hi = (0.55, 0.80) if _is_pm else (0.55, 0.78)
                _ratio_id_od = max(_idr_lo, min(_idr_hi, _raw_id_od))
                if _ratio_id_od != _raw_id_od:
                    logger.info("[orchestrator] #1125 id/od 比钳制 %.3f→%.3f (%s, 上限%.2f)",
                                _raw_id_od, _ratio_id_od,
                                ("PMSM" if _is_pm else "异步"), _idr_hi)
                _res = scale_dims_by_torque(
                    target_torque_nm=float(target_torque),
                    d_id_mm=float(base_id), l_mm=float(base_l),
                    speed_rpm=speed_rpm,
                    cooling=specs.get("cooling_method") or specs.get("cooling"),
                    ref_torque_nm=_ref_tq or None,
                    machine_type=motor_type,
                    power_kw=specs.get("power_kw") or specs.get("power"),
                    poles=specs.get("poles") or specs.get("pole_count"),
                )
                _d2l_scale = _res
                if _res.get("applied"):
                    _old_od, _old_id, _old_l = (round(float(base_od), 1),
                                                round(float(base_id), 1),
                                                round(float(base_l), 1))
                    base_id = _res["d_id_mm"]
                    base_l = _res["l_mm"]
                    # ── ★ J5 (2026-09-24): Dso 极数比联动 ──
                    #   旧: base_od = base_id / _ratio_id_od (种子原始径比, Y160L-4=0.654)
                    #   ⇒ Dso/Dsi 偏离门禁真源 id_ratio_by_poles 目标 (RUN49 1.437 vs 1.742)。
                    #   新: scale_dims_by_torque 输出 d_od_mm (同一真源), 优先消费;
                    #   缺失时回退旧径比还原 (零回归)。
                    if float(_res.get("d_od_mm") or 0) > 0:
                        base_od = round(float(_res["d_od_mm"]), 1)
                        logger.info(
                            "[orchestrator] J5 Dso 极数比联动: 外径 %.1f→%.1fmm "
                            "(id/od=%.4f, 真源 id_ratio_by_poles, 与门禁 _m2d_dso_dsi_ok 同源)",
                            _old_od, base_od, float(_res.get("id_od_ratio_used") or 0))
                    else:
                        base_od = round(base_id / _ratio_id_od, 1) if _ratio_id_od > 0 else round(base_id / 0.6, 1)
                    logger.info(
                        "[orchestrator] #1125 基准尺寸 D²L 对账生效: 目标T=%.1fN·m vs 参考T=%.1fN·m "
                        "(偏差%.0f%%, K_V=%.2f, λ=%.3f, 方法=%s) → 内径 %.1f→%.1fmm, "
                        "外径 %.1f→%.1fmm, 铁心 %.1f→%.1fmm, 预测T=%.1fN·m (覆盖%.2f)",
                        float(target_torque), float(_res.get("ref_torque_nm") or 0),
                        float(_res.get("deviation") or 0) * 100, float(_res.get("K_V") or 1.0),
                        float(_res.get("lambda") or 0.0), _res.get("method"),
                        _old_id, float(base_id), _old_od, float(base_od),
                        _old_l, float(base_l),
                        float(_res.get("predicted_torque_nm") or 0), float(_res.get("coverage") or 0))
                else:
                    logger.info("[orchestrator] #1125 基准尺寸 D²L 对账: %s", _res.get("reason"))
        except Exception as _e:
            logger.warning("[orchestrator] #1125 基准尺寸 D²L 对账异常(沿用原始尺寸): %s", _e)
            _d2l_scale = None

        # v4.02.14 (#998): 规格功率 ↔ 机座尺寸对账
        # 根因: 种子加权质心可能选到与 specs.power_kw 量级不符的机座(如 11kW 规格却选中 75kW 级
        #        OD≈443mm 机座), 导致真实电磁转矩(数百 N·m)与铭牌 9550·P/n(≈71N·m)严重背离。
        # 修复: 取与机座(base_od)最接近的种子额定功率作为"机座额定功率"; 若与 specs.power_kw 相差 >2×,
        #       则将 specs.power_kw 对齐到机座额定功率并记录警告日志, 使全链路自洽。
        try:
            if '_seeds' in locals() and _seeds:
                _frame_seed = min(
                    _seeds,
                    key=lambda s: abs((float(s.get("stator_od") or 1e9)) - float(base_od or 0)),
                )
                _frame_pw = float(_frame_seed.get("power_kw") or 0)
                _sp_pw = float(power_kw or 0)
                if _frame_pw > 0 and _sp_pw > 0:
                    _ratio = max(_sp_pw, _frame_pw) / min(_sp_pw, _frame_pw)
                    # v4.02.22 (#1125 配套): 原实现**不分方向**一律把 specs.power_kw 对齐到
                    # 机座额定功率。方向错了就致命 —— 实测 500kW 船推: 种子池最大机座仅
                    # Y280M-6 级(55kW), ratio=9.09>2 → specs.power_kw 被改成 55kW,
                    # 目标转矩从 19100N·m 塌缩到 2101N·m, 需求被程序"自我阉割"。
                    # 正确语义: 仅当**机座过大**(_frame_pw > _sp_pw)时才上调规格功率;
                    # 若**机座过小**, 需求功率是硬指标, 应由 #1125 的 D²L 对账放大机座。
                    if _ratio > 2.0 and _frame_pw > _sp_pw:
                        logger.warning(
                            "[orchestrator] #998 规格功率(%.2fkW)与机座(OD=%.1fmm, 额定%.2fkW) "
                            "相差 %.1f× (>2×), 对齐 specs.power_kw=%.2fkW",
                            _sp_pw, float(base_od or 0), _frame_pw, _ratio, _frame_pw)
                        specs["power_kw"] = _frame_pw
                        power_kw = _frame_pw
                        target_torque = (compute_torque(power_kw, speed_rpm)
                                         if compute_torque
                                         else (9550 * power_kw / speed_rpm if speed_rpm else 0))
        except Exception as _e:
            logger.debug("[orchestrator] #998 功率对账异常(跳过对齐): %s", _e)

        # v4.02.90 A (治本 P0 几何断裂): 气隙/转子外径/轴径随定子同步相似缩放。
        #   优先级: 案例已知 > 主锚 catalog_ref (ref_motors[0].air_gap_mm) > 按内径推算;
        #   移除 _centroid 优先 (避免案例种子主导质心混入未缩放原始值, 致 rotor_od 不随定子放大)。
        # v4.02.91 点2: 气隙等比例缩放 = 主锚气隙 × (缩放后内径 / 主锚内径), 钳制工程气隙带。
        _ref_ag = (_primary_ref or {}).get("air_gap_mm") or 0
        _ref_id = (_primary_ref or {}).get("stator_inner_dia_mm") or 0
        if case_air_gap:
            air_gap = case_air_gap
        elif _ref_ag > 0 and _ref_id > 0 and base_id > 0:
            air_gap = round(_ref_ag * (base_id / _ref_id), 3)
            air_gap = max(0.3, min(2.5, air_gap))
        else:
            air_gap = max(0.3, min(2.5, base_id * 0.001))
        rotor_od = case_rotor_od or (base_id - 2 * air_gap)
        shaft_dia = (case_motor.get("shaft_dia") if case_motor else None) or (rotor_od * 0.35)

        # 槽配合: 优先案例, 其次种子质心离散众数, 否则按极数标准配合
        # v4.02.91 点3: 每极改为候选列表; fallback 时按 KV 相对主参考 Z 方向选择 —
        #   KV>=1 (放大): 取 Z_stator >= 主参考 Z_stator (最小满足者, 贴近参考)
        #   KV<=1 (缩小): 取 Z_stator <= 主参考 Z_stator (最大满足者)
        slot_map = {
            2: [(24, 20), (36, 28)],
            4: [(24, 22), (36, 28), (48, 44)],
            6: [(36, 28), (54, 44), (72, 58)],
            8: [(48, 44), (54, 44), (72, 58)],
            10: [(60, 50), (72, 58), (90, 72)],
            12: [(72, 58), (84, 70)],
        }
        if case_slots_s and case_slots_r:
            slots_stator, slots_rotor = case_slots_s, case_slots_r
        elif _cent_ss_val and _cent_sr_val:
            slots_stator, slots_rotor = _cent_ss_val, _cent_sr_val
        else:
            _combos = slot_map.get(poles, [(36, 28)])
            _ref_z = (_primary_ref or {}).get("slots_stator") or 0
            _kv = (_d2l_scale or {}).get("K_V") or 1.0
            if _ref_z > 0 and len(_combos) > 1:
                if _kv >= 1.0:
                    _cand = [c for c in _combos if c[0] >= _ref_z]
                    _chosen = _cand[0] if _cand else max(_combos, key=lambda c: c[0])
                else:
                    _cand = [c for c in _combos if c[0] <= _ref_z]
                    _chosen = _cand[-1] if _cand else min(_combos, key=lambda c: c[0])
                slots_stator, slots_rotor = _chosen
            else:
                slots_stator, slots_rotor = _combos[0]

        # 电气参数
        import math
        phase = 3
        # v4.05: 案例电机有已知效率/功率因数时使用, 否则按经验估算
        est_eff = case_eff if case_eff else 90
        est_pf = case_pf if case_pf else 0.85
        if voltage_v and power_kw:
            line_current = power_kw * 1000 / (math.sqrt(3) * voltage_v * est_eff / 100 * est_pf)
        elif case_current:
            line_current = case_current
        else:
            line_current = 0

        # 5. 构建基准参数 (全部变量)
        base_params = {
            "电机类型": motor_type,
            "额定功率(kW)": power_kw,
            "额定电压(V)": voltage_v,
            "额定转速(rpm)": speed_rpm,
            "极数": poles,
            "频率(Hz)": specs.get("frequency_hz", 50),
            "相数": phase,
            "线电流(A)": round(line_current, 2),
            "目标转矩(N.m)": round(target_torque, 2),
            # 结构参数
            "定子外径(mm)": round(base_od, 1),
            "定子内径(mm)": round(base_id, 1),
            "转子外径(mm)": round(rotor_od, 1),
            "铁心长度(mm)": round(base_l, 1),
            # v4.02.79: 长径比 λ=L/D_id 纳入基准参数 (与 physics_kernel.compute_lambda 同口径)
            "长径比": round(base_l / base_id, 3) if (base_id and base_l) else 0.0,
            "气隙长度(mm)": round(air_gap, 3),
            "轴径(mm)": round(shaft_dia, 1),
            "定子槽数": slots_stator,
            "转子槽数": slots_rotor,
            # 附加参数
            "参考型号": ref_model,
            "冷却方式": specs.get("cooling_method", "IC411"),
            "绝缘等级": specs.get("insulation_class", "F"),
            "防护等级": specs.get("protection_class", "IP55"),
            "海拔(m)": specs.get("altitude_m", 0),
            "过载(%)": specs.get("overload_pct", 0),
            # v4.02.17 P1-2: 基准来源与主参考电机锚定 (主锚 ref_motors[0])
            "基准来源": base_motor_source,
            "主参考型号": _primary_ref.get("model", ref_model) if _primary_ref else ref_model,
            "主参考外径(mm)": round(_primary_ref.get("stator_od_mm", 0), 1) if _primary_ref else 0,
            # v4.02.22 Fix4 (#1125): D²L 物理对账诊断 (基准尺寸是否按目标转矩缩放)
            "基准D²L对账": ("已缩放" if (_d2l_scale and _d2l_scale.get("applied"))
                            else ("无需缩放" if _d2l_scale else "未执行")),
            "参考转矩(N.m)": round(_d2l_scale.get("ref_torque_nm", 0), 1) if _d2l_scale else 0,
            "参考转矩偏差(%)": round((_d2l_scale.get("deviation") or 0) * 100, 1) if _d2l_scale else 0,
            "缩放方法": _d2l_scale.get("method", "") if _d2l_scale else "",
            "缩放倍率KV": _d2l_scale.get("K_V", 0) if _d2l_scale else 0,
            "预测转矩(N.m)": round(_d2l_scale.get("predicted_torque_nm", 0), 1) if _d2l_scale else 0,
            "转矩覆盖比": round(_d2l_scale.get("coverage", 0), 3) if _d2l_scale else 0,
        }
        # v4.05: 案例电机已知性能参数
        if case_eff:
            base_params["目标效率(%)"] = case_eff
        if case_pf:
            base_params["目标功率因数"] = case_pf
        if case_motor:
            base_params["案例电机"] = case_motor.get("case_code", "")

        # 6. 构建基准参数变化范围
        # v4.02.09 (提案1): 案例种子占主导时收窄范围(±4%/±7%), 否则较宽(±8%/±12%)
        _dim_pct = 0.04 if (_case_share > 0.5 or case_od) else 0.08
        _len_pct = 0.07 if (_case_share > 0.5 or case_core_l) else 0.12
        # v4.02.79: 长径比 λ=L/D_id 基准与变化范围。缩放律: 长径比保持不变, 定子内径按
        #   kv_D = kv^(1/4) 缩放 (D 与 L 同比例, 见 physics_kernel.scale_dims_by_torque 类比法)。
        #   变化带优先取极数推荐带 (physics_kernel.LAMBDA_RANGE), 再以基准 λ 为中心 ±15% 夹取。
        _lam_base = round(base_l / base_id, 3) if (base_id and base_l) else 0.0
        _lam_lo, _lam_hi = 0.5, 1.5
        try:
            from physics_kernel import LAMBDA_RANGE as _LAMBDA_RANGE  # type: ignore
            _band = _LAMBDA_RANGE.get(int(poles), (0.5, 1.5))
            _lam_lo, _lam_hi = float(_band[0]), float(_band[1])
        except Exception:
            pass
        if _lam_base > 0:
            # v4.02.90 B: 保证 base ∈ [min, max]; _lam_max 取 max(基准, ±15%, 极数带上限)
            _lam_min = round(max(_lam_base * 0.85, _lam_lo), 3)
            _lam_max = round(max(_lam_base, _lam_base * 1.15, _lam_hi), 3)
            if _lam_min >= _lam_max:          # 兜底: 基准 λ 极端超带 → 退回 ±15%
                _lam_min = round(_lam_base * 0.85, 3)
                _lam_max = round(_lam_base * 1.15, 3)
        else:
            _lam_min, _lam_max = round(_lam_lo, 3), round(_lam_hi, 3)
        # ★ P1-3 (2026-09-22 用户指令): Qs 由「冻结为单一值」改为**可行集合**。
        #   判据来源: `_mag_precheck_2d.feasible_stator_slots`
        #     R1 三相平衡 Qs%(3·gcd(Qs,极对数))==0 / R2 极槽配合 q=Qs/(poles·m)
        #     / R3 齿距工艺窗 t∈[12,30]mm / R5 槽宽下线 w=t−bt≥2.5mm。
        #   `require_coil_pitch=True` 额外要求 Qs % poles == 0 —— RMxprt indm3 整距
        #     单层绕组硬约束 (CoilPitch = Qs/poles 须为整数, 否则报 "Stator slot number
        #     does not match pole number"; 6 极 45 槽即被拒)。该过滤会剔除 q=3.5/4.5
        #     等**分数槽** (63/81/99) —— 属**保守**选择: 宁可少开几个方案, 也不引入
        #     必然硬失败的槽数。若后续接入双层/短距绕组模板, 可放开该开关。
        #   ⚠ 必须**同时**给 min/max —— `physics_kernel._build_param_bounds` 只接受
        #     {min,max} 齐全的项, 只给 `options` 的项会被**整项丢弃**; 这正是旧实现
        #     「Qs 这一维自由度整体缺失」的机制原因 (报告 V2 §9.3 P1-3)。
        _qs_feasible = []
        try:
            if _feasible_stator_slots is not None and base_id and poles:
                _qs_feasible = _feasible_stator_slots(
                    base_id, poles,
                    tooth_width=(_primary_ref or {}).get("tooth_width_mm") or None,
                    require_coil_pitch=True)
        except Exception:
            _qs_feasible = []
        if not _qs_feasible:
            _qs_feasible = [slots_stator]
        base_param_ranges = {
            "定子外径(mm)": {"base": round(base_od, 1), "min": round(base_od * (1 - _dim_pct), 1), "max": round(base_od * (1 + _dim_pct), 1), "fixed": False},
            "定子内径(mm)": {"base": round(base_id, 1), "min": round(base_id * (1 - _dim_pct), 1), "max": round(base_id * (1 + _dim_pct), 1), "fixed": False},
            "转子外径(mm)": {"base": round(rotor_od, 1), "min": round(rotor_od * (1 - _dim_pct), 1), "max": round(rotor_od * (1 + _dim_pct), 1), "fixed": False},
            "铁心长度(mm)": {"base": round(base_l, 1), "min": round(base_l * (1 - _len_pct), 1), "max": round(base_l * (1 + _len_pct), 1), "fixed": False},
            # v4.02.79: 长径比 λ=L/D_id 基准 + 变化范围 (供基准参数选定检查点审阅/调节)
            "长径比": {"base": _lam_base, "min": _lam_min, "max": _lam_max, "fixed": False},
            "气隙长度(mm)": {"base": round(air_gap, 3), "min": 0.3, "max": 1.5, "fixed": bool(case_air_gap)},
            "轴径(mm)": {"base": round(shaft_dia, 1), "min": round(shaft_dia * (1 - _dim_pct), 1), "max": round(shaft_dia * (1 + _dim_pct), 1), "fixed": False},
            "极数": {"base": poles, "options": [poles - 2 if poles > 2 else poles, poles, poles + 2 if poles < 12 else poles], "fixed": False},
            # ★ P1-3: Qs 解冻 —— options = 可行集合, fixed=False, 并附 min/max。
            "定子槽数": {"base": slots_stator, "options": list(_qs_feasible),
                         "min": min(_qs_feasible), "max": max(_qs_feasible),
                         "fixed": False,
                         "options_src": "feasible_stator_slots(R1/R2/R3/R5+整距)"},
            "转子槽数": {"base": slots_rotor, "options": [slots_rotor], "fixed": True},
        }

        _duration_ms = int((time.time() - _start_ts) * 1000)

        _case_info = f", 案例: {case_motor.get('case_code', '')}" if case_motor else ""
        self._push_progress("generate_base_params",
                            f"基准参数生成完成 (参考型号: {ref_model}{_case_info}, "
                            f"外径={base_od:.0f}mm, 内径={base_id:.0f}mm, "
                            f"铁心={base_l:.0f}mm, 极数={poles}极)",
                            "success")
        # ★ P1-3 (2026-09-22 用户指令): Qs 解冻可见化 —— 供客户端/日志确认"这一维真的开了"
        if len(_qs_feasible) > 1:
            self._push_progress(
                "generate_base_params",
                f"[P1-3] 定子槽数 Qs 已解冻: 基准 {slots_stator} ⇒ 可行集合 "
                f"{_qs_feasible} (判据 R1 三相平衡 / R2 q=Qs/(poles·m) / "
                f"R3 齿距窗 / R5 槽宽下线, 且满足整距单层 Qs%{poles}==0)", "info")

        # 记录运营指标
        record_step_metric(project_id, "generate_base_params", "DesignAgent",
                           duration_ms=_duration_ms, quality_score=85,
                           quality_issues=f"ref_motors={len(ref_motors)}, ref_model={ref_model}")

        # v4.02.17 P2-1: 工况识别增强 — 将需求侧 duty_cycle 回灌 scenario_info.condition
        _duty = specs.get("duty_cycle")
        _cond_text = str(scenario_info.get("condition") or "").strip()
        if _cond_text and _cond_text not in ("未知", "None", "null"):
            _condition = _cond_text
        elif _duty and str(_duty).strip() not in ("", "未知", "None", "null"):
            _condition = str(_duty)
        else:
            _condition = "未知"
        scenario_info = dict(scenario_info)
        scenario_info["condition"] = _condition

        # v4.05: 场景信息透传 — 供客户端「基准参数」过程交付物卡片第 1 段结构化展示
        scenario_summary = {
            "poles": scenario_info.get("poles"),
            "torque_nm": scenario_info.get("torque_nm"),
            "corrected_torque_nm": scenario_info.get("corrected_torque_nm"),
            "scenario": scenario_info.get("scenario"),        # "catalog" | "analogy"
            "condition": scenario_info.get("condition"),     # 负载/工况条件
            "reason": scenario_info.get("reason"),           # 判定依据
        }

        # v4.07 (F9): revise 重跑时回灌基准参数阶段人工覆盖数据, 避免重做后编辑丢失
        _bp_ov = state.get("base_param_override_data", {})
        if _bp_ov and isinstance(_bp_ov, dict):
            _applied = self._apply_override_to_dict(base_params, _bp_ov)
            if _applied:
                self._push_progress("generate_base_params",
                                    f"已回灌人工覆盖 {_applied} 项到基准参数", "info")

        return {"base_params": base_params,
                "base_param_ranges": base_param_ranges,
                "ref_motors": ref_motors,
                "scenario_summary": scenario_summary,
                "base_motor_source": base_motor_source,
                "scenario_info": scenario_info,
                "extracted_seeds": {
                    "seeds": _seeds,
                    "weights": [round(w, 3) for w in _weights],
                    "case_share": round(_case_share, 3),
                    "source": _seed_src,
                    "used_centroid": bool(_centroid and all(
                        (_centroid or {}).get(k) is not None
                        for k in ("stator_od", "stator_id", "core_length"))),
                }}

    # ==================== v4.02: 基准参数选定检查点 ====================
    def _node_hc_base_param(self, state: WorkflowState) -> dict:
        """v4.02: 基准参数生成后的人机交互检查点"""
        project_id = state.get("project_id", "")
        base_params = state.get("base_params", {})
        base_param_ranges = state.get("base_param_ranges", {})
        ref_motors = state.get("ref_motors", [])

        self._push_progress("hc_base_param", "创建基准参数选定检查点...")

        checkpoint_id = self._push_human_checkpoint(
            step="hc_base_param",
            project_id=project_id,
            checkpoint_type="base_param_select",
            title="电机基准参数选定",
            description="请审阅推荐的电机基准参数及变化范围, 确认或要求重新生成",
            db_context={
                "base_params": base_params,
                "base_param_ranges": base_param_ranges,
                "ref_motors": ref_motors,
                "checkpoint_type": "base_param_select",
            },
            timeout_seconds=hc.DEFAULT_TIMEOUTS["base_param_select"],
            default_action="auto_approve",
        )

        self._get_cost_tracker().project_id = project_id
        human_event = self._get_cost_tracker().start_human_step("hc_base_param",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            if remaining % 60 == 0 and remaining > 0:
                self._push_progress("hc_base_param",
                                    f"基准参数选定倒计时: 剩余 {remaining//60} 分钟", "info")

        # v5.1: 本阶段 LLM 工作已完成, 人工检查点即将阻塞等待; 显式释放 Ollama 模型
        #        避免模型在人工思考期间空占显存 (流程内连续阶段仍靠 keep_alive=5m 不反复重载)
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 检查点释放模型失败(忽略): %s", _e)

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_approve")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        # F1 (v4.07): 应用人工修改(override_data)到 base_params — 复刻 _node_hc_requirement
        #   使基准参数选定界面中的表格编辑真正回灌 state, 不再被静默丢弃
        applied = 0
        corrected_keys = []
        try:
            cp_full = hc.get_checkpoint(checkpoint_id)
            override_data = (cp_full or {}).get("context", {}).get("override_data", {})
            if override_data and isinstance(override_data, dict):
                for _k, _v in override_data.items():
                    _val = _v.get("value") if isinstance(_v, dict) else _v
                    if _val in (None, ""):
                        continue
                    # v4.02.62 (故障修复 #2 P0-B): 跳过占位符 "—"/"未知"/"N/A"
                    if self._is_placeholder_value(_val):
                        continue
                    # 仅当值实际发生变化才算"人工修正"
                    if str(base_params.get(_k, "")) == str(_val):
                        continue
                    base_params[_k] = _val
                    corrected_keys.append(_k)
                    applied += 1
                if applied:
                    self._push_progress("hc_base_param",
                                        f"已应用人工修改 {applied} 项到基准参数", "info")
        except Exception as e:
            self._push_progress("hc_base_param", f"应用人工修改失败: {e}", "warn")

        self._get_cost_tracker().end_human_step(human_event, decision=decision)

        # v4.01.029: 运营质量指标
        self._record_quality_event(project_id, "base_param_select", decision,
                                   checkpoint_id=checkpoint_id,
                                   total_params=len(base_params or {}),
                                   override_applied=applied,
                                   corrected_keys=corrected_keys,
                                   is_timeout=is_timeout)

        # v4.06-P4: 统一回退策略 — 集中重做上限保护 (本检查点无关键字段补缺)
        decision, _fb_specs, _fb_msg = self._apply_checkpoint_fallback(
            "hc_base_param", state, decision, {}, is_timeout)
        if _fb_msg:
            self._push_progress("hc_base_param", f"⚠️ 回退策略: {_fb_msg}", "warn")

        if is_timeout:
            self._push_progress("hc_base_param",
                                f"基准参数选定超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("hc_base_param",
                                f"收到人工决策: {decision}", "success")

        # v4.07 (F8): 基准参数选定阶段返回重做计数
        _base_param_redo_count = int(state.get("base_param_redo_count", 0) or 0)
        if decision in ("reject", "redo"):
            _base_param_redo_count += 1

        return {"human_decision": decision,
                "human_comment": comment,
                "hc_checkpoint_id": checkpoint_id,
                "base_params": base_params,
                "base_param_redo_count": _base_param_redo_count,
                "base_param_override_data": override_data}

    def _node_design_ideation(self, state: WorkflowState) -> dict:
        """v4.01.038 (P0-3): 方案构思 LLM 节点

        在基准参数选定之后、物理内核求解之前, 调用 IdeationAgent 进行方案构思:
          - 以案例电机 (case_motor_ref) 为种子约束
          - 质量门禁自动重生时 (auto_regenerate + regenerate_guidance) 针对性差异化构思
        输出 ideation_result / design_hints 写入 state, 供 generate_designs 与报告引用。
        """
        specs = state.get("specs", {}) or {}
        base_params = state.get("base_params", {}) or {}
        case_motor = state.get("case_motor_ref") or {}
        guidance = state.get("regenerate_guidance", "") or ""
        is_regen = bool(state.get("auto_regenerate"))
        # v4.01.059 (P1-#3): 局部重做(design_redo_count>0)同样将审核意见作为 guidance 传入 IdeationAgent
        is_redo = int(state.get("design_redo_count", 0) or 0) > 0
        _use_guidance = is_regen or is_redo

        self._push_progress("design_ideation",
                            "方案构思师启动: 基于需求与案例种子生成构思方向" +
                            (" (自动重生差异化)" if is_regen else ""),
                            "info")
        try:
            result = self.ideation_agent.run(
                specs=specs, base_params=base_params,
                case_motor=case_motor if case_motor else None,
                guidance=guidance if _use_guidance else "")
            ideation = result.get("ideation", "") or ""
            hints = result.get("design_hints", {}) or {}
            if result.get("error"):
                self._push_progress("design_ideation",
                                    f"方案构思异常: {result.get('error')}, 沿用物理内核默认扫描",
                                    "warning")
            else:
                self._push_progress("design_ideation",
                                    f"方案构思完成 ({len(ideation)} 字, "
                                    f"约束要点: {hints.get('emphasis', []) or '无'})",
                                    "success")
            # v4.02.13: 暴露构思后处理校验警告 (拓扑冲突/参数范围幻觉)
            _vw = result.get("validation_warnings") or []
            if _vw:
                self._push_progress("design_ideation",
                                    "构思校验警告: " + "; ".join(str(w) for w in _vw[:3]),
                                    "warning")
            return {"ideation_result": ideation, "design_hints": hints,
                    "validation_warnings": _vw,
                    "ideation_input": {
                        "specs": specs,
                        "base_params": base_params,
                        "case_motor": case_motor if case_motor else {},
                        "guidance": guidance if _use_guidance else "",
                    }}
        except Exception as e:
            self._push_progress("design_ideation",
                                f"方案构思节点异常: {e}, 沿用物理内核默认扫描", "error")
            return {"ideation_result": "", "design_hints": {},
                    "ideation_input": {
                        "specs": specs,
                        "base_params": base_params,
                        "case_motor": case_motor if case_motor else {},
                        "guidance": guidance if _use_guidance else "",
                    }}

    # ==================== v4.01.039 (P1-4 #487): 方案精度等级标注与披露 ====================
    def _annotate_precision(self, result: dict) -> dict:
        """P1-4 (#487): 为单个方案结果标注精度等级。
        L2 = Motor-CAD 精细验证(real FEA); L1 = RMxprt 快速扫描; L0 = 模拟估算(降级)。
        precision_estimated 标记效率是否由估算得到(源: rmxprt_tools._efficiency_estimated)。"""
        _mode = result.get("solve_mode", "")
        if _mode == "motorcad":
            _level, _label = "L2", "精细验证(Motor-CAD)"
        elif _mode == "rmxprt":
            _level, _label = "L1", "RMxprt快速扫描"
        else:
            _level, _label = "L0", "模拟估算(降级)"
        result["precision_level"] = _level
        result["precision_label"] = _label
        result["precision_estimated"] = bool(result.get("_efficiency_estimated", False))
        return result

    def _seed_geometry_gate(self, seeds: list) -> list:
        """方案C (2026-09-19): 种子几何门禁 —— 轭厚/槽深比 hj/hs 越界则按 P5 带重算。

        背景 (RUN19 根因): 案例库种子携带**锁定的 yoke_thickness 影子字段**
        (如 58.289mm, 对应 hj/hs=0.579), 该值来自原型的深槽设计, 与按极数分档的
        P5 带 (2 极 [2.0,3.5] / 4 极+ [1.0,2.5]) 不兼容 ⇒ 种子派生的主线 A 恒不达标。

        策略: **重算而非剔除** (种子是稀缺的真实机型参考, 剔除会掏空主线 A):
          - 按候选实际 (Dso−Dsi)/2 与该极数的目标系数重算 hj
            (2 极 c=0.7143⇒hj/hs=2.5; 4/6 极+ c=0.5455⇒hj/hs=1.2);
          - 已落在该极数带内的种子**保留原值不动**, 不动真实设计;
          - 重算后写入 _seed_yoke_recalced 标记 + hj/hs 前后值, 供报告追溯。

        fail-closed: 任一异常 → 原样返回 seeds, 绝不阻断漏斗。
        """
        if not seeds:
            return seeds
        _n_fix = 0
        _n_exempt = 0
        for _s in seeds:
            if not isinstance(_s, dict):
                continue
            # ═══ H35/D1a (2026-10-07 用户授权): 型谱种子豁免 ═══
            #   catalog 种子的轭厚是原厂权威设计 (如 Y160L-4 hj/hs=0.667 属真实深槽
            #   比例, 非异常), 按 P5 通用带改写属"无谓改写真实设计" (2026-09-24
            #   Y160L-4 轨迹报告 I1 建议 ★★★, 实测 18→24.55mm +36% 破坏槽深/SF)。
            #   豁免后 catalog 种子永不携带 _seed_yoke_recalced ⇒ H35 分区恒判
            #   "冲片权威" 可进主线 A; 非 catalog (case/L1注入) 种子照旧重算。
            if str(_s.get("source") or "").strip().lower() == "catalog":
                _n_exempt += 1
                continue
            try:
                _sod = float(_s.get("stator_od") or 0)
                _sid = float(_s.get("stator_id") or 0)
                _hj = float(_s.get("yoke_thickness") or 0)
                if not (_sod > _sid > 0 and _hj > 0):
                    continue
                _R = (_sod - _sid) / 2.0
                _hs = _R - _hj
                if _hs <= 0:
                    continue
                _ratio = _hj / _hs
                # 按极数分档: 带内 ⇒ 保留; 越界 ⇒ 按该极数目标系数重算
                _s_poles = _s.get("poles") or _s.get("pole_pairs") or 4
                try:
                    _s_poles = max(2, int(round(float(_s_poles))))
                except (TypeError, ValueError):
                    _s_poles = 4
                if _s_poles % 2:                 # 防御: 奇数极数取相邻偶数
                    _s_poles = max(2, _s_poles - 1)
                _lo_r, _hi_r = _yoke_ratio_band(_s_poles)
                if _lo_r <= _ratio <= _hi_r:
                    continue          # 已在带内 —— 保留真实设计, 不动
                _c_default = _yoke_default(_s_poles)
                _hj_new = _R * _c_default
                _s["yoke_thickness"] = round(max(2.0, _hj_new), 3)
                _s["_seed_yoke_recalced"] = True
                _s["_seed_hj_hs_before"] = round(_ratio, 3)
                _s["_seed_hj_hs_after"] = round(_hj_new / (_R - _hj_new), 3)
                _s["_seed_yoke_before"] = round(_hj, 3)
                _n_fix += 1
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if _n_fix or _n_exempt:
            try:
                _msg = (f"方案C 种子几何门禁: {_n_fix}/{len(seeds)} 组 hj/hs 越界, "
                        f"已按极数分档带重算轭厚 (2极目标 hj/hs≈2.5 / 4极+≈1.2)")
                if _n_exempt:
                    _msg += f"; 型谱种子豁免 {_n_exempt} 组 (H35/D1a: 原厂轭厚为权威设计不改写)"
                self._push_progress("generate_designs", _msg, "info")
            except Exception:
                pass
        return seeds

    # H35 冲片 11 键 (与 motor_tools._MAIN_A_FIXED_KEYS 同集) — 防御性引用
    _H35_LAMINATION_KEYS = frozenset({
        "stator_od", "stator_id", "rotor_od", "shaft_dia",
        "slots_stator", "slots_rotor", "stator_slot_type", "rotor_slot_type",
        "tooth_width", "yoke_thickness", "poles"})

    # ══ H35/D3-改进 (2026-10-10 用户授权 方案B): 槽形真实性判定 ══
    #   仅当用户显式要求"catalog 种子槽形自动填补 ⇒ 降级"时生效(见 _h35_partition_seeds)。
    #   判定依据: 型谱库 y_series_physics.json 22 字段零槽形, _catalog_to_seed 仅 set
    #   主尺寸+轭厚+槽数, stator_slot_type 恒为 None ⇒ 槽形几何完全缺失, 由下游模板
    #   默认派生 (非原厂真值)。真实几何种子 (case 回流 / L1 注入) 通常携带
    #   stator_slot_type 或显式槽形尺寸键。
    _SLOT_DIM_KEYS = (
        "Slot__Hs_1", "Slot__Hs0", "Slot__Hs0_", "Slot__Bs_0", "Slot__Bs0",
        "slot_hs", "slot_bs", "hs", "bs", "stator_slot_depth",
        "bs0", "bs1", "bs2", "hs0", "hs1", "hs2", "bs_0", "bs_1", "bs_2",
        "hs_0", "hs_1", "hs_2",
    )

    def _seed_slot_geom_real(self, seed: dict) -> bool:
        """种子是否携带**真实槽形几何**(而非下游模板默认派生的自动填补)。

        返回 True ⇒ 槽形可作冲片权威(允许进 main_a 锁 11 键);
        False ⇒ 槽形为自动填补, 不可作 main_a 冲片锚点 (按 H35/D3-改进 降级到
        completed, 只喂 B1/B2/B3, 冲片在 B 轨可调)。

        fail-open: seed 异常 ⇒ 返回 True (不误杀, 沿用旧语义交由其它门禁把关)。
        """
        if not isinstance(seed, dict):
            return True
        try:
            _st = seed.get("stator_slot_type")
            _rt = seed.get("rotor_slot_type")
            if _st and str(_st).strip():
                return True
            if _rt and str(_rt).strip():
                return True
            # 兜底: 显式携带槽形尺寸键亦视为真实
            return any(seed.get(k) is not None for k in self._SLOT_DIM_KEYS)
        except Exception:
            return True

    def _h35_partition_seeds(self, seeds: list):
        """H35 (2026-10-07 用户授权): 读侧二道防线 —— 冲片权威性分区门禁。

        背景: main_a 的设计前提是「种子冲片 = 权威原厂几何, 不可调整」(11 键锁死,
        仅铁心长/绕组可调)。但种子池里混有"补全"种子 —— case 案例回流 (本身是 AI
        生成回写)、方案C 重算轭厚、L1 TOP5 派生注入 —— 把补全值锁进 fixed_keys
        后: ① QG 失败项若落锁定键 ⇒ 数学不可解 (RUN53 八轮实证); ② verify 以补全
        值为偏差真源, 口径失真。

        分区规则 (D1a + D3, 用户 2026-10-07 裁定):
          权威 (可进 main_a):
            source=catalog ∧ ¬_seed_yoke_recalced (D1a 豁免后 catalog 恒不携带)
            ∧ H28 回灌未触及冲片键 (防御; _RULE_INV 已禁止回灌 12 键)
          补全 (禁入 main_a, 只喂 B1/B2/B3):
            其余全部 — source=case / _seed_yoke_recalced / seed_source=l1_top5 /
            H28 触及冲片键的种子
          ★ D3: catalog 的 rotor_od/shaft_dia/tooth_width 推算补全 (seed["inferred"])
            仍算权威 — 由原厂 Dsi/气隙/槽数短链派生, 属原厂冲片的必然推论。
          ★ D3-改进 (2026-10-10 用户授权 方案B): 槽形几何须为**真实原厂几何** —
            catalog 种子若 stator_slot_type 为 None (型谱库 y_series_physics.json
            22 字段零槽形, 槽形由下游模板默认派生 ⇒ 自动填补, 非真值) 判为「槽形
            自动填补」, 降级为补全种子 (不进 main_a, 只喂 B1/B2/B3)。冲片锁定(11键)
            须以真实槽形为前提, 否则质量门对假几何空转打磨耗尽预算 (RUN68 八轮实证:
            八次评估阻断项恒为齿/轭磁密/槽满率, 均属种子固有模态不可达)。
            方案B = 降级不剔除: 保留主尺寸价值, 仅改变其在漏斗中的轨位。

        返回: (authoritative, completed) 二元组; 开关 H35_LAMINATION_PARTITION=0
        时返回 None (调用方沿用原全量语义, 便于对照回退)。
        fail-open: 任一异常返回 None (不阻断漏斗)。
        """
        if str(os.environ.get("H35_LAMINATION_PARTITION", "1")).strip() == "0":
            return None
        try:
            _auth, _comp = [], []
            _n_slot_downgraded = 0
            for _s in (seeds or []):
                if not isinstance(_s, dict):
                    _comp.append(_s)
                    continue
                _src = str(_s.get("source") or "").strip().lower()
                _seed_src = str(_s.get("seed_source") or "").strip().lower()
                _recalced = bool(_s.get("_seed_yoke_recalced"))
                _h28_keys = set(_s.get("_h28_unlocked_keys") or [])
                _h28_touches_lamination = bool(_h28_keys & self._H35_LAMINATION_KEYS)
                _slot_real = self._seed_slot_geom_real(_s)
                if (_src == "catalog" and not _recalced
                        and _seed_src != "l1_top5"
                        and not _h28_touches_lamination
                        and _slot_real):                 # ★ D3-改进: 槽形几何须真实
                    _auth.append(_s)
                else:
                    # catalog 种子槽形几何为自动填补 (stator_slot_type=None) ⇒ 降级:
                    # 不进 main_a (冲片不可锁假几何), 只喂 B1/B2/B3 (D3-改进, 方案B)
                    if _src == "catalog" and not _slot_real:
                        _s["_seed_slot_geom_auto"] = True
                        _n_slot_downgraded += 1
                    _comp.append(_s)
            if _n_slot_downgraded:
                try:
                    self._push_progress(
                        "generate_designs",
                        f"[H35/D3-改进] {_n_slot_downgraded} 颗 catalog 种子槽形几何为自动填补 "
                        f"(stator_slot_type=None), 降级为补全种子 → 仅喂 B1/B2/B3, 不进主线A",
                        "info")
                except Exception:
                    pass
            return _auth, _comp
        except Exception as _e:
            logger.warning("[H35] 冲片权威分区异常, 沿用全量语义: %s", _e)
            return None

    def _seed_evaluate_stage(self, seeds: list, specs: dict, motor_type: str = None) -> tuple:
        """v4.02.36 (种子评价方案 阶段2+阶段3): 种子层合理性 + L1(RMxprt)功率校核。

        按《种子评价方案分析报告》落地:
          - 阶段2 几何/电磁合理性诊断 (默认仅标注不拦截, 低风险);
          - 阶段3 L1 RMxprt 功率偏差 ≤±5% 校核 (默认关闭, 需 config/env 显式开启)。

        fail-closed: 评价链任一环节异常 → 原样返回 seeds, 绝不阻断漏斗。

        Returns:
            (seeds, verification_dict) —— L1 开启且判定 verified=False 的种子会被剔除;
            L1 关闭时 seeds 原样返回, 仅附带阶段2标注。
        """
        if not seeds:
            return seeds, {"seed_count": 0, "l1_enabled": False, "skipped": True}
        try:
            from Core.seed_extractor import evaluate_seeds as _eval_seeds
            # L1 开关: ai_flow_config.design.seed_l1_rmxprt 优先, 其次环境变量
            _cfg = {}
            try:
                from ai_flow_config import get_config as _afc
                _dcfg = (_afc() or {}).get("design", {}) or {}
                _cfg["l1_rmxprt_enabled"] = bool(_dcfg.get("seed_l1_rmxprt", False))
                if _dcfg.get("seed_l1_power_tol"):
                    _cfg["l1_power_tol"] = float(_dcfg["seed_l1_power_tol"])
                if _dcfg.get("seed_l1_voltage"):
                    _cfg["voltage"] = float(_dcfg["seed_l1_voltage"])
            except Exception:
                pass
            _rep = _eval_seeds(seeds, specs, machine_type=motor_type, config=_cfg)
            _seeds_out = _rep.get("seeds") or seeds

            # 阶段2 汇报 (仅标注)
            _warn = int(_rep.get("reason_warn") or 0)
            _err = int(_rep.get("reason_error") or 0)
            self._push_progress(
                "generate_designs",
                f"种子合理性诊断: {len(_seeds_out)} 颗 (警告{_warn}/错误{_err})",
                "warning" if _err else "info")

            # 阶段2 硬门禁(A): 剔除 error 级种子 (功率/转矩/几何不符) — v4.02.44
            _blocked = [s for s in _seeds_out if s.get("reason_blocked")]
            if _blocked:
                _kept = [s for s in _seeds_out if not s.get("reason_blocked")]
                self._push_progress(
                    "generate_designs",
                    f"[A硬门禁] 剔除 {len(_blocked)} 颗 error 级种子 (功率/转矩/几何不符), 保留 {len(_kept)}/{len(_seeds_out)} 颗",
                    "warning")
                _seeds_out = _kept

            # 阶段3 汇报 + 剔除
            if _rep.get("l1_enabled"):
                if not _rep.get("l1_available"):
                    self._push_progress("generate_designs",
                                        "种子L1校核: RMxprt 不可用, 降级放行(不拦截)", "warning")
                else:
                    _ok = int(_rep.get("l1_verified") or 0)
                    _rej = int(_rep.get("l1_rejected") or 0)
                    _kept = [s for s in _seeds_out if s.get("verified") is not False]
                    self._push_progress(
                        "generate_designs",
                        f"种子L1功率校核: 通过{_ok}/驳回{_rej} → 保留 {len(_kept)}/{len(_seeds_out)} 颗",
                        "warning" if _rej else "info")
                    # 全被驳回 → 不剔除 (fail-closed, 避免漏斗断流)
                    if _kept:
                        _seeds_out = _kept
                    else:
                        self._push_progress("generate_designs",
                                            "种子L1校核: 全部驳回, 保底沿用原种子(不断流)", "warning")
            return _seeds_out, _rep
        except Exception as _ee:
            self._push_progress("generate_designs",
                                f"种子评价异常, 沿用原种子: {_ee}", "warning")
            return seeds, {"seed_count": len(seeds), "error": str(_ee), "l1_enabled": False}

    # ── v4.02.92 (G6): 种子 2D 磁密门开关 ──
    #   _SEED_MAG_GATE_ENABLED: L1-TOP5 晋升 main_a 前, 磁密带(Bt/By/Bδ/SF)不达标则
    #     不晋升(避免「转矩达标但磁密饱和」的候选被锁进 main_a 跳过 3.1 的「假2D已优化」)。
    #   _SEED_GEOM_PROXY_HARD: 型谱种子几何代理(仅粗筛畸形)硬拒开关; 默认软(仅记录)。
    _SEED_MAG_GATE_ENABLED = True
    _SEED_GEOM_PROXY_HARD = False

    # ── v4.02.85 (问题2 推荐混合方案): 质量门禁重生时用 L1 已解 TOP5 作种子 ────────
    #   仅继承「力矩达标 ≥90%」的合格候选, 与型谱种子混合, 承接已验证方向并避免
    #   「同方向重复失败」; per_seed 均摊由下游 generate_main_a_designs /
    #   generate_bypass_schemes 保证 (per_seed=ceil(n_per_track/len(seeds)))。
    def _extract_l1_top5_seeds(self, state: WorkflowState, specs: dict) -> list:
        """从上一轮 L1(RMxprt) 已解结果中抽 TOP5, 过滤力矩达标(≥90%) 转成种子。

        返回种子 dict list (含 _MAIN_A_FIXED_KEYS + stator_id + air_gap + 标记键)。
        仅接受几何完整的候选 (主线索 main_a 产出必含全键; 旁路候选缺齿宽/轭厚等则跳过),
        避免几何不全的旁路候选污染主线 A 锚定。
        """
        results = state.get("results") or []
        if not results:
            return []
        # 额定转矩 (与 L3462 同一口径: 9550·P/n)
        _rt = None
        try:
            _rp = float(specs.get("power_kw") or 0)
            _rs = float(specs.get("speed_rpm") or 0)
            if _rp > 0 and _rs > 0:
                _rt = round(9550.0 * _rp / _rs, 2)
        except (TypeError, ValueError):
            _rt = None
        try:
            from motor_tools import _rank_score
        except Exception:
            _rank_score = None
        _sorted = list(results)
        if _rank_score:
            _sorted = sorted(results, key=lambda r: _rank_score(r, _rt), reverse=True)
        _out = []
        for _r in _sorted[:5]:
            _params = _r.get("params") or {}
            if not isinstance(_params, dict):
                continue
            _tq = float(_r.get("torque") or 0.0)
            _compliance = (_tq / _rt) if (_rt and _rt > 0) else 0.0
            if _compliance < 0.90:
                continue  # 力矩未达标, 不继承劣质种子
            # G6 (v4.02.92): 2D 磁密门 — 转矩达标但磁密饱和的候选不晋升 main_a,
            # 留待 B3 去种子化重优化(避免「假2D已优化」锁死 Dsi)。
            if (self._SEED_MAG_GATE_ENABLED
                    and _MAG_PRECHECK_AVAILABLE
                    and _mag_ok_from_result is not None):
                _ok, _det = _mag_ok_from_result(_r)
                if not _ok:
                    continue
            _sd = self._params_to_seed(_params)
            if not _sd:
                continue
            _sd["seed_source"] = "l1_top5"
            _sd["l1_torque_ratio"] = round(_compliance, 4)
            _out.append(_sd)
        return _out

    @staticmethod
    def _params_to_seed(params: dict) -> dict:
        """候选 params → 主线 A 可用种子。仅当全部必需几何键存在且物理合法才接受。

        几何有效性: 尺寸/槽数类键须 >0 (永不为 0); 唯 slots_rotor 允许 =0
        (表贴永磁无转子槽属合法值, 误判为缺失会拒绝合法 PMSM 种子)。
        """
        try:
            from motor_tools import _MAIN_A_FIXED_KEYS
        except Exception:
            _MAIN_A_FIXED_KEYS = ("stator_od", "stator_id", "rotor_od", "shaft_dia",
                                 "slots_stator", "slots_rotor", "stator_slot_type",
                                 "rotor_slot_type", "tooth_width", "yoke_thickness", "poles")
        _pos_keys = ("stator_od", "stator_id", "rotor_od", "shaft_dia",
                     "slots_stator", "tooth_width", "yoke_thickness", "air_gap", "poles")
        _need = _MAIN_A_FIXED_KEYS + ("stator_id", "air_gap")
        for k in _need:
            v = params.get(k)
            if v is None or v == "":
                return None
            if k in _pos_keys:
                try:
                    if float(v) <= 0:
                        return None
                except (TypeError, ValueError):
                    return None
        _sd = {k: params.get(k) for k in _need}
        for _ek in ("turns_per_coil", "parallel_circuits", "parallel_wires"):
            if params.get(_ek) is not None:
                _sd[_ek] = params.get(_ek)
        # G6 (v4.02.92): 几何代理粗筛(型谱种子未跑 L1 时) — 仅记录; 硬拒由
        #   _SEED_GEOM_PROXY_HARD 开关控制(默认 False, 避免误拒合法型谱种子, 观察一轮)。
        _sd["_geom_proxy_ok"] = True
        if _MAG_PRECHECK_AVAILABLE and _geom_proxy_ok is not None:
            try:
                _ok, _ = _geom_proxy_ok(params)
                _sd["_geom_proxy_ok"] = bool(_ok)
                if (getattr(MotorAIOrchestrator, "_SEED_GEOM_PROXY_HARD", False)
                        and not _ok):
                    return None
            except Exception:
                pass
        return _sd

    @staticmethod
    def _merge_seed_lists(base_seeds: list, extra_seeds: list) -> list:
        """型谱种子 + L1 TOP5 混合, 按几何签名去重 (避免完全相同锚点重复占用席位)。"""
        _sig_keys = ("stator_od", "stator_id", "rotor_od", "shaft_dia",
                     "slots_stator", "slots_rotor", "tooth_width",
                     "yoke_thickness", "air_gap")
        _merged = []
        _seen = set()
        for _s in (list(base_seeds or []) + list(extra_seeds or [])):
            if not isinstance(_s, dict):
                continue
            _sig = tuple(str(_s.get(k)) for k in _sig_keys)
            if _sig in _seen:
                continue
            _seen.add(_sig)
            _merged.append(_s)
        return _merged

    def _h28_unlock_fixed_keys(self, seeds: list, state: dict) -> list:
        """H28 (2026-09-26): fixed_keys ↔ QG 失败项一致性闸门 —— 让纠偏**跨轮累积**。

        背景 (RUN53 取证)
        ----------------
        main_a / bypass_b1 把种子的几何键声明为 fixed_keys, 生成期
        `_main_a_make_design` 逐键 `d[k] = seed.get(k)` 原样复制。而 QM 的全部
        失败项 (Bt / By / SF / ζ / Dso-Dsi) 都依赖这些键 ⇒ 纠偏只能改当轮候选,
        下一轮重生又被**未修正的种子复位** ⇒ 纠偏退化为「从同一原点重复」的随机游走
        (RUN53 实测: Bt 0.493→0.82 九轮净 +0.33T, η 四升四降峰谷差 6.98pp,
         硬预算 8/8 耗尽转人工)。

        本闸门的处置
        ------------
        ① 取上轮 best + 其 `_physics.fixed_keys`, 调 quality_gate.locked_geometry_blockade
           算出「被锁死且本轮必须要动」的几何键;
        ② 把这些键**上轮已纠偏的真值回灌种子** ⇒ 下轮以此为固定项 ⇒ 纠偏可梯度累积;
        ③ verify_engine 以 `_seed` 为 reference 判固定项偏差 —— 种子与方案同步 ⇒ 0 偏差,
           不会引入新的 verify 告警;
        ④ 仅在**重生环路**生效 (有 improvement_instructions 或 auto_regenerate),
           首轮生成与 b3(全自由轨, fixed_keys 为空) 天然不受影响。

        安全边界 (任一不满足即原样返回, 零回归)
          · 无 QG 结果 / 无失败项               → 不动
          · fixed_keys 为空 (b3/fallback)       → 不动 (本来就可自由纠偏)
          · best 求解降级 (非真实解)             → 不动 (不可用伪值污染种子)
          · 无可回灌的有效数值                   → 不动
        """
        try:
            from Core.quality_gate import locked_geometry_blockade as _blk  # type: ignore
        except Exception:
            try:
                from quality_gate import locked_geometry_blockade as _blk   # type: ignore
            except Exception as _e:
                logger.warning("[H28] locked_geometry_blockade 不可用, 跳过: %s", _e)
                return list(seeds or [])

        st = state or {}
        # ① 仅重生环路 — 首轮生成时 state 里既无纠偏指令也无 auto_regenerate 标记,
        #    此时即便已写入 quality_gate_result (首轮评估产物) 也不得回灌 (零回归)。
        if not (st.get("improvement_instructions") or st.get("auto_regenerate")):
            return list(seeds or [])
        _qg = st.get("quality_gate_result") or {}
        if not isinstance(_qg, dict) or not _qg.get("checks"):
            return list(seeds or [])

        # ② 上轮 best
        _rs = list(st.get("results") or [])
        _best = _rs[0] if _rs else (st.get("best_solution") or {})
        if not isinstance(_best, dict) or not _best:
            return list(seeds or [])
        if bool(_best.get("solve_failed")) or str(_best.get("solve_mode") or "").lower() \
                not in ("rmxprt", "motorcad"):
            # 仅真实 RMxprt/Motor-CAD 解才允许回灌 (SIMULATED/降级解不可用)
            return list(seeds or [])

        _fk = ((_best.get("_physics") or {}).get("fixed_keys") or [])
        if not _fk:
            return list(seeds or [])          # b3 / fallback: 本来就是自由轨

        _info = _blk(_qg, _fk, _best)
        # ═══ 方向① (2026-09-30 用户授权): 规则级不可变键禁止回灌 ═══
        #   用户裁定「冲片不调整/三圆固定 优先于 H28 回灌」。种子池为 a/b1/b2/b3
        #   四轨共享, 任一轨规则声明不可变的键都不得经回灌改写 —— 否则该轨规则被
        #   跨轮打破 (典型: By 持续失败 ⇒ 把 best 已减小的 stator_od 回灌种子 ⇒
        #   a/b1/b2 定子外径被改 ✗)。覆盖: A 轨冲片 11 键 (motor_tools
        #   ._MAIN_A_FIXED_KEYS 同集) + 气隙(随冲片定) ; B1/B2 规则圆
        #   (定子外圆/定子内圆/转子内圆=轴径) 已含于上。剔除后仍有剩余键
        #   (如 core_length 叠长, 属 A 轨自由维度) 才允许回灌。
        _RULE_INV = frozenset({
            "stator_od", "stator_id", "rotor_od", "shaft_dia",
            "slots_stator", "slots_rotor", "stator_slot_type", "rotor_slot_type",
            "tooth_width", "yoke_thickness", "poles", "air_gap"})
        _patches = dict(_info.get("patches") or {})
        _rule_blocked = sorted(k for k in _patches if k in _RULE_INV)
        if _rule_blocked:
            self._push_progress(
                "generate_designs",
                f"[H28·规则优先] 回灌被拒绝的键 {_rule_blocked} 属规则级不可变键 "
                f"(冲片不调整/三圆固定 优先于回灌) ⇒ 种子几何不改, 纠偏由自由轨承担",
                "warn")
            _patches = {k: v for k, v in _patches.items() if k not in _RULE_INV}
            _info["unlockable"] = [k for k in (_info.get("unlockable") or [])
                                   if k not in _RULE_INV]
        if _info.get("solvable") or not _patches:
            return list(seeds or [])

        # ③ 回灌: **只回灌被本轮实际采用为主参考的那颗种子** ——
        #    其余种子 (含 L1 TOP5 混合种子) 保持原值以保留票池多样性, 避免整池塌缩成
        #    单一几何 (多样性损失会让 L0 预筛失去意义)。
        _ref = str(((_best.get("_physics") or {}).get("ref_model"))
                   or _best.get("ref_model") or "").strip()
        _patched, _n_patch = [], 0
        for _s in (seeds or []):
            if not isinstance(_s, dict):
                _patched.append(_s)
                continue
            _sid = str(_s.get("seed_id") or _s.get("model")
                       or _s.get("motor_model") or "").strip()
            if _ref and _sid and _sid != _ref:
                _patched.append(_s)
                continue
            _ns = dict(_s)
            _ns.update(_patches)
            _ns["_h28_unlocked_keys"] = list(_info["unlockable"])
            _patched.append(_ns)
            _n_patch += 1
        if not _n_patch:
            return list(seeds or [])

        _desc = ", ".join(f"{k}={v}" for k, v in list(_patches.items())[:4])
        self._push_progress(
            "generate_designs",
            f"[H28·固定项解锁] QG 失败项 {_info['failed']} 依赖几何键 "
            f"{_info['blocked_keys']}, 而它们被 fixed_keys 锁死 ⇒ 本轮在原结构上不可解; "
            f"已把上轮已纠偏几何回灌种子 ({_desc}) —— 纠偏改为**跨轮累积**, "
            f"命中 {_n_patch}/{len(_patched)} 颗 (参考型号={_ref or '全部'})", "warn")
        logger.info("[H28] unlock %s patch=%s", _info["blocked_keys"], _patches)
        return _patched

    def _node_generate_designs(self, state: WorkflowState) -> dict:
        count = state.get("solve_count", 10)
        fine_count = state.get("fine_solve_count", 0)
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")
        scenario_info = state.get("scenario_info", {})

        # v4.01.038 (P0-2): 质量门禁自动重生 — 注入改进指引并扩大候选池以寻求通过门禁的方案
        # v4.01.039 (P1-6 #486): 局部重做(reject)同样带入 regenerate_guidance, 扩大候选池
        regen_guidance = state.get("regenerate_guidance", "") or ""
        if regen_guidance:
            # 重生/局部重做时扩大候选数量, 提高命中合格方案的概率 (避免与上次完全相同的失败结果)
            _extra = 5
            count = count + _extra
            _tag = "质量门禁重生" if state.get("auto_regenerate") else "局部重做"
            self._push_progress("generate_designs",
                                f"{_tag}: 候选数 {state.get('solve_count', 10)} → {count}, "
                                f"改进指引已带入: {regen_guidance[:120]}", "warn")
            # P0-3 (方案构思 LLM 节点) 将消费该指引生成差异化方案; 当前物理内核扩大扫描范围

        # v4.01.038 (P0-3): 方案构思结果落盘提示 (供报告/调试追溯)
        _design_hints = state.get("design_hints", {}) or {}
        if _design_hints:
            _em = _design_hints.get("emphasis") or []
            self._push_progress("generate_designs",
                                f"已带入方案构思约束: 重点={_em}; "
                                f"规避={_design_hints.get('avoid', []) or '无'}", "info")

        # v4.01.037 (P0-1): 结构化评审回灌 — 替代原"仅正则提取极数/功率"
        # 覆盖 FIELD_REGISTRY 全部可回灌字段(含定子外径/铁心长度/气隙/槽数等结构参数)
        specs = dict(specs)                              # 拷贝后修改, 显式返回给 state
        base_params = dict(state.get("base_params", {}) or {})
        base_param_ranges = dict(state.get("base_param_ranges", {}) or {})
        _fb_update = self._ingest_review_feedback(
            state, specs, base_params, base_param_ranges, stage="generate_designs")
        if _fb_update.get("feedback_applied"):
            # 参数已变 → 重新识别场景, 使参数矩阵基于修正后的参数生成
            try:
                from physics_kernel import determine_scenario as _ds
                scenario_info = _ds(specs)
                self._push_progress("generate_designs",
                                    f"场景重新识别: 极数={scenario_info.get('poles')}极, "
                                    f"模式={scenario_info.get('scenario')}, "
                                    f"目标转矩={scenario_info.get('corrected_torque_nm', '?')}Nm",
                                    "info")
            except Exception as _se:
                self._push_progress("generate_designs",
                                    f"场景重新识别失败, 沿用原场景: {_se}", "warning")

        # v2.04: 物理内核进度提示
        scenario = scenario_info.get("scenario", "catalog")
        torque = scenario_info.get("corrected_torque_nm", "?")
        poles = scenario_info.get("poles", "?")
        # v4.04 (#9): 飞轮降量透传 — 解析实际目标方案数
        actual_count = _get_flywheel_design_count(specs)
        if not (actual_count and actual_count < count):
            actual_count = count
        self._push_progress("generate_designs",
                            "开始生成参数矩阵 (FIX1 全量生成: 主线A≈50, 旁路各≈12/B3达grid上限12, "
                            "不再截断为19, 全部进L0)...")
        self._push_progress("generate_designs",
                            f"物理内核: 选型模式={scenario}, 目标转矩={torque}Nm, 极数={poles}极")

        _start_ts = time.time()

        # 1. 查找匹配的技能模板
        self._push_progress("generate_designs", "查找匹配的技能模板...")
        matched = find_matching_templates(specs, top_n=3)
        if matched:
            self._push_progress("generate_designs",
                                f"找到 {len(matched)} 个匹配模板: {', '.join(m.get('name', '?') for m in matched[:2])}")

        # 1.5 种子智能提取 (V2.1 #693 → 主线 A 基座)
        _seeds = []
        # v4.01.060: 默认占位, 保证异常分支下 _sp 始终有定义 (供 base_seeds 下发)
        _sp = {"seeds": [], "catalog_count": 0, "case_count": 0,
               "backfilled": 0, "incomplete": 0}
        # v4.02.36: 种子评价报告占位, 保证异常分支下 state 写入不 KeyError
        _seed_verify = {"seed_count": 0, "l1_enabled": False, "skipped": True}
        try:
            # v4.02.36 (阶段1): 缓存版抽取, 同一 specs 全流程复用同一批种子
            from Core.seed_extractor import extract_seeds_cached as _extract_seeds
            # v4.02.10: 传递 case_motor_ref 和 base_params 给种子提取
            _case_motor = state.get("case_motor_ref") or {}
            _sp = _extract_seeds(specs, base_params=base_params, case_motor_ref=_case_motor)
            _seeds = _sp.get("seeds") or []
            self._push_progress("generate_designs",
                                f"种子智能提取: {len(_seeds)} 组 "
                                f"(型谱{_sp.get('catalog_count', 0)}/案例{_sp.get('case_count', 0)}/"
                                f"回填{_sp.get('backfilled', 0)}/缺{_sp.get('incomplete', 0)})", "info")
            # v4.02.10: 提示种子来源
            if _sp.get("case_count", 0) > 0:
                self._push_progress("generate_designs",
                                    f"种子包含案例电机参考 ({_sp['case_count']}组)", "info")
            # v4.02.36 (阶段2+阶段3): 种子评价 —— 合理性诊断 + L1 功率校核 (fail-closed)
            _seeds, _seed_verify = self._seed_evaluate_stage(
                _seeds, specs, motor_type=specs.get("motor_type") or "异步电动机")
            _seeds = self._seed_geometry_gate(_seeds)
            _sp["seeds"] = _seeds
            _sp["verification"] = _seed_verify
        except Exception as _se:
            self._push_progress("generate_designs",
                                f"种子提取失败, 主线A回退通用扫描: {_se}", "warning")

        # 2. 生成参数矩阵 (v2.04 物理内核 / V2.1 #694 主线A / #695 混合漏斗 / #697 三层旁路)
        self._push_progress("generate_designs",
                            "V2.1 混合漏斗: 主线A + 旁路B1/B2/B3 衍生...")

        # v4.02.11 (半自主增强 Step5): LLM 构思建议注入 (fail-closed 回退)
        #   注入点: 调用 llm_ideation_inject 获取参数偏置建议, 并入 design_hints,
        #   供下游 build_candidate_matrix / _build_param_bounds 消费;
        #   LLM 不可用/超时/解析失败 → _design_hints_llm 退回原 _design_hints, 流程不变。
        # v4.02.12: 读取自主模式 (全AI智能/半智能), 默认半智能
        try:
            from ai_flow_config import get_config as _afc_gc
            _autonomous_mode = _afc_gc().get("design", {}).get("autonomous_mode", "semi")
        except Exception:
            _autonomous_mode = "semi"
        _aggressive = (_autonomous_mode == "full")

        # v4.02.12 (Phase 3): Planner — 基于需求 + 记忆检索生成求解策略 (fail-closed)
        _design_plan = {"strategy": "默认混合漏斗策略", "focus": [], "steps": [], "source": "default"}
        _memory_hits = []
        try:
            from Core.agent_memory import query_similar, add_case
            _memory_hits = query_similar(
                specs.get("motor_type") or specs.get("series_id") or "unknown",
                specs, top_k=5)
            from Core.agent_planner import plan as _planner_plan
            _design_plan = _planner_plan(specs, _memory_hits)
        except Exception as _pe:
            self._push_progress("generate_designs",
                                f"[Planner] 规划失败, 用默认策略: {_pe}", "warning")

        # v4.02.14 (#1000): 前置自主区 (V3.0 §5.4) —— 仅全AI智能模式
        #   完整智能体在规划层之后自主生成 design_hints 与 seeds, 替代上游节点产出;
        #   fail-closed: 任一环节失败则 _autonomous_hints 维持 None, 后续走原半自主注入路径。
        _autonomous_hints = None
        if _aggressive:
            try:
                from motor_tools import llm_ideation_inject as _llm_ide_front
                # 1) 自主生成 design_hints: 以空上游 hints 起步, LLM 基于 specs+plan+memory 全量构思
                _auto = _llm_ide_front(specs, list(_seeds or []), {}, aggressive=True)
                if isinstance(_auto, dict):
                    # 归一化 revised_specs (白名单转 float, 与 Step5 注入一致, fail-closed)
                    if isinstance(_auto.get("revised_specs"), dict):
                        _rs = {}
                        for _k, _v in _auto["revised_specs"].items():
                            try:
                                _rs[_k] = float(_v)
                            except Exception:
                                pass
                        _auto["revised_specs"] = _rs
                    # 2) 自主 seeds 重提取 (记忆案例优先, 与上游 base_params 融合)
                    try:
                        # v4.02.36 (阶段1): 缓存版 —— 与上游同参时直接命中, 不再重复抽取
                        from Core.seed_extractor import extract_seeds_cached as _extract_seeds_front
                        _cm = state.get("case_motor_ref") or {}
                        _sp_auto = _extract_seeds_front(specs, base_params=_auto.get("base_params") or base_params, case_motor_ref=_cm)
                        _seeds_auto = _sp_auto.get("seeds") or []
                        if _seeds_auto:
                            # v4.02.36 (阶段2+3): 自主重提取的种子同样过评价链
                            _seeds_auto, _seed_verify_auto = self._seed_evaluate_stage(
                                _seeds_auto, specs, motor_type=specs.get("motor_type") or "异步电动机")
                            _seeds = _seeds_auto
                            _sp = _sp_auto
                            _sp["seeds"] = _seeds_auto
                            _sp["verification"] = _seed_verify_auto
                            _seed_verify = _seed_verify_auto
                            self._push_progress("generate_designs", f"[前置自主区] seeds 重提取: {len(_seeds)} 组 (自主优先)", "info")
                    except Exception as _se3:
                        self._push_progress("generate_designs", f"[前置自主区] seeds 重提取失败, 沿用上游: {_se3}", "warning")
                    # 上游结构字段为基底, 自主 hints 覆盖 (优先级: 自主 > 上游)
                    _autonomous_hints = {**(_design_hints or {}), **_auto}
                    self._push_progress("generate_designs", "[前置自主区] design_hints 已由智能体自主生成 (替代上游节点)", "info")
            except Exception as _ae:
                self._push_progress("generate_designs", f"[前置自主区] 自主生成失败, 回退半自主注入: {_ae}", "warning")

        # v4.02.14 (#1000): Step5 LLM 构思建议注入 (fail-closed)
        #   全AI智能且前置自主区已生成 design_hints → 直接采用自主结果, 跳过重复注入;
        #   其余 (半智能 / 自主失败) → 在 upstream hints 基础上做 LLM 注入 (合并或覆盖)。
        if _autonomous_hints is not None:
            _design_hints_llm = dict(_autonomous_hints)
        else:
            _design_hints_llm = dict(_design_hints or {})
            try:
                from motor_tools import llm_ideation_inject
                _sug = llm_ideation_inject(specs, _seeds, _design_hints_llm, aggressive=_aggressive)
                if isinstance(_sug, dict):
                    if _sug.get("suggested_constraints") and isinstance(_sug["suggested_constraints"], dict):
                        if _aggressive:
                            # 全AI智能: LLM 直接目标缩放覆盖 (已被 prompt 约束 0.7~1.3)
                            _design_hints_llm["suggested_constraints"] = dict(_sug["suggested_constraints"])
                        else:
                            _dc = dict(_design_hints_llm.get("suggested_constraints") or {})
                            _dc.update(_sug["suggested_constraints"])
                            _design_hints_llm["suggested_constraints"] = _dc
                    if _sug.get("emphasis_extra") and isinstance(_sug["emphasis_extra"], list):
                        if _aggressive:
                            _design_hints_llm["emphasis"] = list(_sug["emphasis_extra"])
                        else:
                            _em = list(_design_hints_llm.get("emphasis") or [])
                            for _e in _sug["emphasis_extra"]:
                                if _e not in _em:
                                    _em.append(_e)
                            _design_hints_llm["emphasis"] = _em
                    if _sug.get("seed_bias"):
                        _design_hints_llm["seed_bias"] = _sug.get("seed_bias")
                    # 全AI智能: 接受 LLM 绝对规格覆写建议 (白名单转 float, fail-closed)
                    if _aggressive and _sug.get("revised_specs") and isinstance(_sug["revised_specs"], dict):
                        _rs = _design_hints_llm.setdefault("revised_specs", {})
                        for _k, _v in _sug["revised_specs"].items():
                            try:
                                _rs[_k] = float(_v)
                            except Exception:
                                pass
                    self._push_progress("generate_designs", f"[半自主·Step5] LLM 构思建议已注入参数矩阵 (模式={_autonomous_mode})", "info")
            except Exception as _ie:
                self._push_progress("generate_designs", f"[半自主·Step5] LLM 注入失败, 回退原约束: {_ie}", "warning")

        # FIX4 (#1110): 入口先探种子有效性 — 空(扭矩门控后无合格种子)则直走 B(focused_scan),
        #   跳过四轨漏斗(main_a+b1/b2/b3)编排, 与 build_candidate_matrix 的无种子分支等价但显式短路。
        _has_valid_seeds = bool(_seeds)
        try:
            if not _has_valid_seeds:
                # 无有效种子 → 直接聚焦扫描(全域 B 路), 不再编排四轨
                # FIX-细化: fallback 规模同步改为全域扫描动态 (与有种子 B3 对齐),
                #   不再用固定 solve_count(=10); 普通800 / 高可靠·大功率·高速2400 / 双高5000
                _fallback_count = _b3_full_domain_count(specs)
                self._push_progress("generate_designs",
                                    "[Fix4] 无有效种子(选种转矩门控: 型谱±30%/案例±20% 后为空), 直走 focused_scan 跳过四轨编排",
                                    "info")
                params_list, actual_count = generate_param_matrix(
                    specs, _fallback_count, design_hints=_design_hints_llm,
                    base_param_ranges=base_param_ranges,
                    seeds=None, main_a=False)
                _cand_meta = {"main_a": 0, "bypass_b1": 0, "bypass_b2": 0,
                              "bypass_b3": 0, "fallback": len(params_list)}
            else:
                from Core.scheme_engine import build_candidate_matrix
                # ── 灰度开关 (v4.02.20 · U7→收敛→三方案) ──
                # 默认关闭: 运行态与旧版完全一致; 设环境变量 MOTORAI_GRAYSCALE_U7_CONVERGE=1
                # 仅在「本次需求」灰度启用 U7 平台通用 + U9 方案收敛, 不影响其它需求/默认链路。
                # 真实端到端需在有 AEDT/Motor-CAD 求解器的环境运行; 沙箱可用
                # Scripts/_grayscale_validate_u7_converge.py 做链路逻辑校验。
                import os as _os
                _u7_gray = _os.environ.get("MOTORAI_GRAYSCALE_U7_CONVERGE", "0") == "1"
                # FIX1 (v4.04): 生成全量不再限制 — 解耦生成数量与 L0 预算(actual_count)
                #   原 n_main_a=actual_count(=10) / n_bypass=max(3,actual_count//5)(=3)
                #   把候选池截断成 19, 导致 L0 在已裁小的池上再裁到 10 (裁剪空转)。
                #   现改为全量生成: main_a 取函数自然全量; B1/B2 取 n_bypass; 全部进 L0。
                #   ⚠ v4.02.83 起具体数值由 FUNNEL_MAIN_A / FUNNEL_BYPASS 提供
                #     (500 / 1200, 可经环境变量覆盖), 见下方赋值处。
                #   B3 数量参考「全域扫描办法」(generate_param_matrix 默认 count=30 /
                #   focused_scan 默认 count=10): 改为 focused_scan 同口径的 D×L×poles 全域
                #   扫描 (见 motor_tools._b3_build_combo_grid), 取消原 grid[:12] 硬上限,
                #   b3_count 默认 30 (全域扫描法默认规模)。L0 后由
                #   physics_scan_estimate(l1_budget=10) + 显式配额裁到 10 (main_a 5/B1 2/
                #   B2 2/B3 1), 配额与生成规模解耦, 稳定保 5/2/2/1 (不受频率漂移影响)。
                # v4.02.83: 四轨配额统一由模块级常量提供
                #   (用户 2026-09-15 指定: 主线A=500 / B1=1200 / B2=1200 / B3=2400)
                _GEN_FULL_MAIN_A = FUNNEL_MAIN_A      # 50 → 500
                _GEN_FULL_BYPASS = FUNNEL_BYPASS      # 12 → 1200 (B1/B2 共用一值)
                # FIX-细化-2/-3: B3 数量真正放开 — 参考附件「全域扫描法」(8 变量/10240 组合)
                #   按机型/需求动态 (普通800 / 高可靠·大功率·高速2400 / 双高5000), 高可靠
                #   场景叠加 B_δ/A/J₁/slots 全维度变量, 均匀分层抽样覆盖全域; 且 fallback
                #   路径不再被飞轮超时降量裁剪(FIX-细化-3)。L0 后配额仍为 5/2/2/1
                #   (与生成规模解耦)。
                # ⚠ v4.02.83 起: 有种子四轨路径的 B3 配额改由 FUNNEL_B3 常量给定 (2400),
                #   上述"按机型动态"口径仅保留在无种子 fallback 路径 (上方 _fallback_count)。
                # ── v4.02.25 (#1142): base_params 几何锚定候选矩阵 ──
                # 根因: main_a/bypass 轨道逐字克隆 _seeds 的几何, 而 _seeds 来自 case-seed
                #   (船推 500kW 案例仅 Y280M-6 级 ~450mm), 尺寸过小 ~35.6 倍 → RMxprt 全池
                #   power/failed。#1125 已在上面把 base_od 反解到 ~2053mm(基准D²L对账==已缩放),
                #   但从未流入候选矩阵。此处仅当"已缩放"时, 用 base_params 的定子外径/内径/
                #   叠长/气隙/转子外径/轴径覆盖种子(浅拷贝不动原 _seeds, 供其它节点复用),
                #   并把种子功率/转速对齐 specs, 使 main_a 的 _main_a_scale_length 命中目标转矩
                #   后锁定正确叠长。正常电机(未缩放)不走此分支 → 完全无回归。
                #   保留 main_a 克隆语义(只改上游种子来源), 改动最小且对症。
                # v4.02.85 (问题2 推荐混合方案): 质量门禁重生环路中, 用上一轮 L1 已解
                # TOP5(力矩达标≥90%) 与型谱种子混合, 提升重生收敛性、避免同方向重复失败。
                # per_seed 均摊由下游 generate_main_a_designs / generate_bypass_schemes 保证。
                if regen_guidance or state.get("auto_regenerate"):
                    try:
                        _l1s = self._extract_l1_top5_seeds(state, specs)
                        if _l1s:
                            _n_before = len(_seeds)
                            _seeds = self._merge_seed_lists(_seeds, _l1s)
                            self._push_progress("generate_designs",
                                f"[混合种子] 重生注入 L1 TOP5 合格种子 {len(_l1s)} 颗 "
                                f"(力矩≥90%), 型谱{_n_before} → 混合{len(_seeds)} 颗",
                                "info")
                        else:
                            self._push_progress("generate_designs",
                                "[混合种子] 本轮 L1 结果无力矩≥90% 候选, 沿用型谱种子",
                                "info")
                    except Exception as _me2:
                        self._push_progress("generate_designs",
                            f"[混合种子] L1 TOP5 注入失败, 沿用型谱种子: {_me2}", "warning")
                _matrix_seeds = _seeds
                # ═══ H28 (2026-09-26 RUN53 取证): fixed_keys ↔ QG 失败项一致性闸门 ═══
                #   main_a/bypass_b1 把种子几何键声明为 fixed_keys, 生成期原样复制种子
                #   ⇒ 上一轮纠偏的几何成果在下轮重生时**被未修正的种子复位**, 纠偏退化成
                #   随机游走 (RUN53: 9 轮 Bt 净推进 +0.33T, η 四升四降, 预算 8/8 耗尽)。
                #   此处在重生环路把上轮 best 的已纠偏几何**回灌种子** ⇒ 纠偏可跨轮累积;
                #   verify_engine 以 `_seed` 为 reference 判固定项偏差 ⇒ 两侧同步 ⇒ 0 偏差。
                try:
                    _matrix_seeds = self._h28_unlock_fixed_keys(_matrix_seeds, state)
                except Exception as _h28e:
                    logger.warning("[H28] 固定项解锁闸门异常, 沿用原种子: %s", _h28e)

                if base_params and base_params.get("基准D²L对账") == "已缩放":
                    try:
                        _bod = float(base_params.get("定子外径(mm)") or 0)
                        _bid = float(base_params.get("定子内径(mm)") or 0)
                        _brd = float(base_params.get("转子外径(mm)") or 0)
                        _bl  = float(base_params.get("铁心长度(mm)") or 0)
                        _bag = float(base_params.get("气隙长度(mm)") or 0)
                        _bsd = float(base_params.get("轴径(mm)") or 0)
                        if _bod > 0 and _bid > 0 and _bl > 0:
                            _anchor_geo = {
                                "stator_od": _bod,
                                "stator_id": _bid,
                                "rotor_od": _brd if _brd > 0 else _bid - 2 * (_bag if _bag > 0 else 0.8),
                                "shaft_dia": _bsd if _bsd > 0 else _bid * 0.35,
                                "core_length": _bl,
                                "air_gap": _bag if _bag > 0 else 0.8,
                                "power_kw": float(power_kw) if power_kw else None,
                                "speed_rpm": float(speed_rpm) if speed_rpm else None,
                            }
                            # v4.02.25 (#1142): 丢弃与 k 缩放冲突的槽形/齿/轭字段。
                            #   这些字段源自 ~450mm case-seed, 若随 k≈17.1 缩放到 2053mm 级会产生
                            #   物理不可解几何(实测 assm.exe: "Wedge Thickness is too large")。
                            #   丢弃后 fix① 的 _scale_mm 沿用模板槽形按 k 缩放(同已验证的
                            #   candidate_2053.json 成功路径: 仅载主几何, 无 tooth/yoke/slot_type)。
                            #   槽数按极数取标准 q=2 配合(6极→36), 与 500kW 验证几何一致。
                            _DROP_KEYS = ("tooth_width", "yoke_thickness",
                                          "stator_slot_type", "rotor_slot_type")
                            _poles_i = int(poles) if poles else 6
                            _slot_s = 6 * _poles_i
                            _matrix_seeds = []
                            for _s in (_seeds or []):
                                _ns = dict(_s)  # 浅拷贝, 不动原种子(供其它节点复用)
                                for _k, _v in _anchor_geo.items():
                                    if _v is not None:
                                        _ns[_k] = _v
                                for _k in _DROP_KEYS:
                                    _ns.pop(_k, None)
                                _ns["slots_stator"] = _slot_s
                                _ns["slots_rotor"] = 0
                                _matrix_seeds.append(_ns)
                            logger.info(
                                "[orchestrator] #1142 base_params 几何锚定候选矩阵: 用缩放几何"
                                "(OD=%.1f/ID=%.1f/L=%.1fmm) 覆盖 %d 颗种子, 替代偏小 case-seed",
                                _bod, _bid, _bl, len(_matrix_seeds))
                    except Exception as _e:
                        logger.warning("[orchestrator] #1142 锚定异常(沿用原种子): %s", _e)
                        _matrix_seeds = _seeds
                # ── v4.02.103 (P2): 把基准几何写入 specs['_base_geom'], 供 b3 全域轨锚定 ──
                #   背景: B3(bypass_b3) 轨道 head 注释即为「去种子化, 不依赖任何种子」⇒
                #   #1142 的种子覆盖对它无效, 其几何由目标转矩 D²L 经 b3 自有的
                #   λ/id-ratio 常数独立反推 ⇒ 与 base_params 偏离 (实测 Dsi −9.8% / L +15.8%
                #   / g −58.8%)。现由 motor_tools._b3_anchor(specs) 读取本锚, 七项锚定基准
                #   (Dso/Dsi/L/g/id-od/λ/槽数); 开关 B3_ANCHOR_BASE=0 回退独立全域。
                try:
                    if base_params:
                        _bod_a = float(base_params.get("定子外径(mm)") or 0)
                        _bid_a = float(base_params.get("定子内径(mm)") or 0)
                        _bl_a = float(base_params.get("铁心长度(mm)") or 0)
                        _bag_a = float(base_params.get("气隙长度(mm)") or 0)
                        if _bod_a > 0 and _bid_a > 0 and _bl_a > 0:
                            _geom_anchor = {
                                "stator_od": round(_bod_a, 1),
                                "stator_id": round(_bid_a, 1),
                                "core_length": round(_bl_a, 1),
                                "air_gap": round(_bag_a, 3) if _bag_a > 0 else 0.0,
                                "id_ratio": round(_bid_a / _bod_a, 4) if _bod_a > 0 else 0.0,
                                "lambda": round(_bl_a / _bid_a, 4) if _bid_a > 0 else 0.0,
                                "slots_stator": int(base_params.get("定子槽数") or 0),
                                "slots_rotor": int(base_params.get("转子槽数") or 0),
                                "poles": int(poles) if poles else 0,
                                "source": "base_params",
                            }
                            specs["_base_geom"] = _geom_anchor
                            # ── H6 (2026-09-24): 注入放大链 D_id 基准, 供
                            #   motor_tools._b3_od_center 在锚缺失/关闭时仍以
                            #   放大后 Dsi(经 J5 径比) 推 OD 采样中心, 消除
                            #   采样中心(独立反推 D_id=193.4)与放大链(D_id=238.5)
                            #   的基准分叉。base_params 此处已含 scale_dims_by_torque
                            #   (J5) 放大后的定子内/外径, 物理链派生、非种子。
                            specs["_d2l_amplified"] = {
                                "d_id_mm": round(_bid_a, 1),
                                "d_od_mm": round(_bod_a, 1),
                                "l_mm": round(_bl_a, 1),
                                "source": "base_params_amplified",
                            }
                            logger.info(
                                "[orchestrator] H6 放大链 D_id 基准注入: "
                                "D_id=%.1f Dso=%.1f L=%.1f (供 _b3_od_center 统一基准)",
                                _bid_a, _bod_a, _bl_a)
                            logger.info(
                                "[orchestrator] v4.02.103 b3 锚定基准几何: OD=%.1f ID=%.1f "
                                "L=%.1f g=%.3f id/od=%.3f λ=%.3f Q1=%d",
                                _geom_anchor["stator_od"], _geom_anchor["stator_id"],
                                _geom_anchor["core_length"], _geom_anchor["air_gap"],
                                _geom_anchor["id_ratio"], _geom_anchor["lambda"],
                                _geom_anchor["slots_stator"])
                except Exception as _ea:
                    logger.warning("[orchestrator] v4.02.103 _base_geom 写入异常(不锚定): %s", _ea)
                # ═══ H35 (2026-10-07 用户授权): 冲片权威性分区门禁 ═══
                #   权威种子 (catalog 未改写) → main_a; 补全种子 (case/方案C改写/
                #   L1注入) → 只喂 B1/B2/B3 (B 轨几何可调, 补全值是起点非真源)。
                #   D2: 无权威种子 ⇒ main_a 配额转移给 B1/B2 (scheme_engine 内实现)。
                _h35_part = self._h35_partition_seeds(_matrix_seeds) \
                    if _matrix_seeds else None
                _ma_auth_seeds = None
                if _h35_part is not None:
                    _ma_auth_seeds, _h35_comp = _h35_part
                    _msg = (f"[H35 冲片权威分区] 权威 {len(_ma_auth_seeds)} 颗 "
                            f"(catalog 未改写) → 主线A; 补全 {len(_h35_comp)} 颗 "
                            f"(case/方案C改写/L1注入) → 仅 B1/B2/B3")
                    if not _ma_auth_seeds:
                        _msg += (f"; 无权威种子 ⇒ 主线A配额 {_GEN_FULL_MAIN_A} "
                                 f"转移给 B1/B2 (D2)")
                    self._push_progress("generate_designs", _msg, "info")
                # v4.02.83: B3 配额改为用户指定常量 (2400), 取代原动态档
                #   (普通800/高可靠2400/大功率·高速2400/双高5000)。
                #   动态函数 _b3_full_domain_count 仍服务无种子 fallback 路径 (上方);
                #   置 FUNNEL_B3=0 即回落原动态口径。
                _B3_FULL_DOMAIN_COUNT = FUNNEL_B3 or _b3_full_domain_count(specs)
                _matrix = build_candidate_matrix(
                    specs, seeds=_matrix_seeds if _matrix_seeds else None,
                    design_hints=_design_hints_llm,
                    n_main_a=_GEN_FULL_MAIN_A,
                    n_bypass=_GEN_FULL_BYPASS,
                    b3_count=_B3_FULL_DOMAIN_COUNT,
                    main_a_seeds=_ma_auth_seeds,
                    enable_platform_optim=_u7_gray,
                    enable_convergence=_u7_gray)
                params_list = _matrix["candidates"]
                _cand_meta = _matrix["meta"]
                # ★ P1-4 (2026-10-11): 杠杆耗尽「换轨」 —— 被判定耗尽的锁定轨把部分配额让给 b3
                #   (b3 全自由, 真实几何抬 SF 杠杆 set_slot_depth 落点)。⚠ QG_LEVER_EXHAUST_N=0 ⇒ 跳过。
                if _qg_exp_int("QG_LEVER_EXHAUST_N", 0):
                    _cand_meta = _rebalance_candidate_meta_for_exhausted(state, _cand_meta)
                self._push_progress("generate_designs",
                                    f"混合漏斗完成: {len(params_list)} 组 "
                                    f"(主线A={_cand_meta['main_a']}, "
                                    f"B1={_cand_meta['bypass_b1']}, "
                                    f"B2={_cand_meta['bypass_b2']}, "
                                    f"B3={_cand_meta['bypass_b3']}, "
                                    f"兜底={_cand_meta['fallback']})", "info")
        except Exception as _me:
            self._push_progress("generate_designs",
                                f"混合漏斗失败, 回退聚焦扫描: {_me}", "warning")
            params_list, actual_count = generate_param_matrix(
                specs, count, design_hints=_design_hints_llm,
                base_param_ranges=base_param_ranges,
                seeds=_matrix_seeds if _matrix_seeds else None,
                main_a=bool(_matrix_seeds))
            _cand_meta = {"main_a": 0, "bypass_b1": 0, "bypass_b2": 0,
                          "bypass_b3": 0, "fallback": 0}
        self._push_progress("generate_designs",
                            f"参数矩阵生成完成: {len(params_list)} 组方案")

        # ═══ P1 (v4.02.67): 结构化改进指令注入 — 把门禁重生指令转为矩阵采样偏移 ═══
        #   质量门禁再生时 (state["improvement_instructions"]) 对候选做定向扰动
        #   (降 slot_fill_target / 减匝 / 加长铁心 / 加外径折中), 并重算槽形几何,
        #   使下一轮候选真正"因改进措施而不同"(破除确定性复现 0.85 的空转)。
        _instrs = state.get("improvement_instructions") or []
        _comps = state.get("slot_fill_compromises") or []
        # ★ 方案② (2026-10-10): 分轨措施随行 —— 执行器按候选 source_path 只吃本轨
        _instrs_by_rail = state.get("improvement_instructions_by_rail") or {}
        _comps_by_rail = state.get("slot_fill_compromises_by_rail") or {}
        if _instrs or _comps or _instrs_by_rail:
            _n_applied = self._apply_improvement_instructions(
                params_list, _instrs, _comps, dict(specs),
                instrs_by_rail=_instrs_by_rail or None,
                comps_by_rail=_comps_by_rail or None)
            self._push_progress("generate_designs",
                                f"[改进指令注入] {len(_instrs)} 条改进 + {len(_comps)} 条折中指令 "
                                f"已应用于 {_n_applied}/{len(params_list)} 组候选 (重算槽形)",
                                "info")
            # 2026-09-20 (用户指令): 槽满率偏低 → 收小冲片 的可审计证据 (供事件流/报告)
            _shr = next((_c.get("_sf_shrink") for _c in params_list
                         if isinstance(_c, dict) and _c.get("_sf_shrink")), None)
            if _shr:
                self._push_progress(
                    "generate_designs",
                    f"[槽满率偏低修复] 收小冲片: 槽高 {_shr['hs_old']}→{_shr['hs_new']}mm, "
                    f"定子外径 {_shr['sod_old']}→{_shr['sod_new']}mm "
                    f"(ΔDso=2ΔHs={2 * _shr['dR_mm']:.2f}mm), 轭厚锁定 {_shr['yoke_mm']}mm, "
                    f"轭厚系数 {_shr['yoke_coef_new']} ⇒ 定子轭磁密 By 不变",
                    "info")
            # ★ P0-3 (2026-09-22): set_slot_depth (槽深反解) 的可审计证据
            _ssd = next((_c.get("_sf_set_slot_depth") for _c in params_list
                         if isinstance(_c, dict) and _c.get("_sf_set_slot_depth")), None)
            if _ssd:
                self._push_progress(
                    "generate_designs",
                    f"[槽满率偏低修复·P0-3] 反解槽深: 槽高 {_ssd['hs_old']}→{_ssd['hs_new']}mm "
                    f"(×{_ssd['ratio_sf']:.3f} = SF_cur/SF_tgt), 定子外径 {_ssd['sod_old']}→"
                    f"{_ssd['sod_new']}mm, 轭厚锁定 {_ssd['yoke_mm']}mm, 轭厚系数 "
                    f"{_ssd['yoke_coef_new']} ⇒ 定子轭磁密 By 不变 (槽深/轭厚已解绑)",
                    "info")

        # ═══ P0-3 (v4.02.81): 电磁负荷可达性门禁 — 生成期统计 + 全池带内命中率 ═══
        if _LAST_EM_GATE:
            try:
                _g = dict(_LAST_EM_GATE)
                _bnd = _g.get("bands") or {}
                self._push_progress(
                    "generate_designs",
                    f"[电磁负荷门禁] 负荷组合 {_g.get('n_in')}→{_g.get('n_out')} "
                    f"(剔除 {_g.get('dropped_pct')}%) | Bδ带 {_bnd.get('b_delta')} "
                    f"A带 {_bnd.get('line_load')} J₁带 {_bnd.get('current_density')} "
                    f"σ带 {_bnd.get('ab_product')} | 源={_g.get('source')}"
                    + (f" | 放宽={_g.get('relaxed')}" if _g.get("relaxed") else ""),
                    "info" if not _g.get("relaxed") else "warn")
            except Exception as _ge:
                self._push_progress("generate_designs", f"电磁负荷门禁统计异常(忽略): {_ge}", "warning")
        if _check_em_bands:
            try:
                _em = _check_em_bands(params_list, dict(specs))
                if _em.get("n"):
                    _ab_r = _em.get("ab_rate")
                    self._push_progress(
                        "generate_designs",
                        f"[负荷带内命中] Bδ/A/J₁ 全项合格 {_em['n_ok']}/{_em['n']} "
                        f"({_em['rate']}) | A·Bδ {_em['ab_ok']}/{_em['n_ab']} "
                        f"({_ab_r}) | A·J₁ {_em['aj_ok']}/{_em['n_aj']} ({_em['aj_rate']})",
                        "info" if (_ab_r or 0) >= 0.999 else "warn")
            except Exception as _ee:
                self._push_progress("generate_designs", f"负荷带内命中统计异常(忽略): {_ee}", "warning")

        # ═══ P0 (v4.02.67)+P0-3: 生成期槽满率预检 — 超硬上限**不入选** ═══
        #   drop_hard: True —— 原实现只打标不剔除, 致「目标 0.50 / 实际 0.94」的候选仍进
        #   Top10 (v4.02.67 P1-3 遗留)。fail-safe: 若全池超限则保留原池 (prescreen 内部保证)。
        if _prescreen_sf:
            try:
                # P0-3: 剔除阈值 = min(报告验收 0.65, config 硬上限) —— 更严者为准
                _sf_cap = None
                try:
                    from motor_tools import _EM_GATE_SLOT_FILL_LIMIT as _sf_cap_mt
                    _sf_cap = float(_sf_cap_mt)
                except Exception:
                    _sf_cap = None
                _sf_pre = _prescreen_sf(params_list, drop_hard=True, hard_cap=_sf_cap)
                if _sf_pre.get("n_est"):
                    _dropped = (_sf_pre.get("n_in", 0) - _sf_pre.get("n_out", 0))
                    self._push_progress(
                        "generate_designs",
                        f"[槽满率预检] 预估覆盖 {_sf_pre['n_est']}/{_sf_pre['n_in']} 组 | "
                        f"合格带内={_sf_pre['n_in_band']} 超限={_sf_pre['n_high']} "
                        f"偏低={_sf_pre['n_low']} | SF范围 "
                        f"{_sf_pre['min']}~{_sf_pre['max']} (目标带 {_sf_pre['band']}, "
                        f"硬上限 {_sf_pre.get('hard_cap')})"
                        + (f" | ★已剔除超上限 {_dropped} 组" if _dropped else ""),
                        "info" if _sf_pre['n_high'] == 0 else "warn")
                    if _dropped and _sf_pre.get("list_out"):
                        params_list = _sf_pre["list_out"]
            except Exception as _sfe:
                self._push_progress("generate_designs", f"槽满率预检异常(忽略): {_sfe}", "warning")

        # v4.04 (#10): 方案多样性度量披露 — 覆盖度/创新性/变异数
        # v4.02.35 (D 修复): 缺省/未评估时显式标注"种子路径未评估", 避免静默 0 误导
        #   (v4.02.34 fixA 已使种子路径在聚合出口预填充; 此处仅兜底 n<2 / fallback 等未评估场景)
        _div = dict(LAST_DIVERSITY_METRICS)
        _mut = _div.get("mutated", 0)
        _cov = _div.get("coverage", 0) or 0
        _inn = _div.get("innovation", 0) or 0
        if _cov == 0 and _inn == 0 and not _mut:
            _cov_s = "未评估"
            _inn_s = "未评估"
            _interp = "种子路径未评估"
        else:
            _cov_s = f"{_cov:.3f}"
            _inn_s = f"{_inn:.3f}"
            _interp = _div.get("interpretation", "")
        self._push_progress("generate_designs",
                            f"[多样性] 覆盖度={_cov_s}, "
                            f"创新性={_inn_s}, "
                            f"变异方案={_mut}, 评估: {_interp}",
                            "info")

        # ═══════════════════════════════════════════════════════════
        # v3.00.02: 两级求解工作流
        #   Level 1: RMxprt 快速扫描 (1-3s/方案, 全部方案)
        #   Level 2: Motor-CAD 精细验证 (仅 Top5, 需权限)
        # ═══════════════════════════════════════════════════════════

        # v3.00.11: 从 specs 提取电机类型, 传递给 RMxprt 求解器
        _motor_type = specs.get("motor_type", "异步电动机")

        # V2.1 (#699) → FIX2 (#1109): L0 物理预筛替代档
        #   原 quick_l0_estimate 为 display-only 假估算(无物理依据/非确定性/不门控 L1, 前报 D1/D2/D3)。
        #   改用 Scripts.physics_l0.physics_scan_estimate 做确定性物理评分 + 各路 top 按 L1 预算 K
        #   成比例门控裁剪, 在下方"去重之后、L1(RMxprt)之前"执行 (插入点见报告 §5:2547→2549 间)。
        # FIX1 (v4.04→v5.0): L0 预算 = 客户端用户输入的 solve_count
        # FIX2 (v5.0): 配额按权重 4:2:2:2 **归一化**到 solve_count (Σ配额 ≡ 预算)。
        #   原实现为硬编码 4/2/2/2 (Σ=10, 与 solve_count 脱钩): solve_count>10 时
        #   缺口由 _top_up 按「全局分数最高」补差, B3 候选占 97% → 独吞全部缺口
        #   (实测 solve_count=20 得 4/2/2/12, B3 占 60%)。
        #   改为 _allocate() 最大余数法后 Σ ≡ K 恒成立, _top_up 退回其设计初衷
        #   (仅回收「某路候选数 < 配额」的闲置席位), 不再承担预算缺口分配。
        # FIX3 (v5.0): 预算/配额在此**统一**提前计算 (原在 #1109 裁剪处才定义),
        #   供下方「档就绪」提示与 L0 裁剪同源使用, 消除 K 口径分裂 (P2)。
        _L0_BUDGET = state.get("solve_count", 10)
        # v4.02.87 (G3 · item 4): L0 预筛返回空时, 不再浪费 RMxprt 算力扫全量退化解,
        #   改跑小样本诊断 (可由环境变量 L0_DIAG_CAP 调整, 默认 20)。
        try:
            _L0_DIAG_CAP = int(os.environ.get("L0_DIAG_CAP", "20"))
        except (TypeError, ValueError):
            _L0_DIAG_CAP = 20
        if _L0_DIAG_CAP < 1:
            _L0_DIAG_CAP = 1
        # P1-4 (v4.02.59+): 无种子兜底时全部候选 source_path='fallback', 权重全给 fallback
        #   (与 motor_tools.generate_param_matrix 无种子分支 source_path='fallback' 对齐)。
        #   否则 fallback 桶靠 alloc.get('fallback',1) 兜底取 1 席 + _top_up 补差, 语义不清晰。
        if not _has_valid_seeds:
            _L0_WEIGHTS = {"fallback": 1.0}
        else:
            # ★ H24 (2026-09-25 用户指令): 配额保底 A≥1 / B1≤⌈K/2⌉ / B2≥1 / B3≥2
            #   旧权重 4:2:2:2 下 B3 理论 2 席, 但 B3 候选被 L0 硬门控全灭后席位被
            #   _top_up 回收给 B1 (RUN51 实测 TOP10 中 B3=0/10, B1 占 6~9) ⇒
            #   H6 (B3 轨 OD 中心修复) 永远进不了候选、被配额架空。
            #   新权重 1:5:2:2 (K=10 精确 → A=1/B1=5/B2=2/B3=2), 下方再加显式保底钳。
            _L0_WEIGHTS = {"main_a": 0.1, "bypass_b1": 0.5,
                           "bypass_b2": 0.2, "bypass_b3": 0.2}
        try:
            from Scripts.physics_l0 import _allocate as _l0_allocate
            _L0_QUOTA = _l0_allocate(_L0_WEIGHTS, int(_L0_BUDGET or 10))
        except Exception as _alloc_e:      # 降级: 回退硬编码配额 (与 H24 权重同口径)
            _L0_QUOTA = {"fallback": int(_L0_BUDGET or 10)} if not _has_valid_seeds else {
                "main_a": 1, "bypass_b1": 5, "bypass_b2": 2, "bypass_b3": 2}
            logger.warning("[L0] 配额按比例分配失败(%s), 回退配额 %s",
                           _alloc_e, _L0_QUOTA)
        # ★ H24: 显式保底钳 (K≥6 时生效, 防 _allocate 浮点/回扣把保底席吞掉)
        try:
            _h24_k = int(_L0_BUDGET or 10)
            if _has_valid_seeds and _h24_k >= 6:
                if int(_L0_QUOTA.get("main_a") or 0) < 1:
                    _L0_QUOTA["main_a"] = 1
                if int(_L0_QUOTA.get("bypass_b2") or 0) < 1:
                    _L0_QUOTA["bypass_b2"] = 1
                if int(_L0_QUOTA.get("bypass_b3") or 0) < 2:
                    _L0_QUOTA["bypass_b3"] = 2
                _h24_b1_cap = max(1, _h24_k // 2)
                if int(_L0_QUOTA.get("bypass_b1") or 0) > _h24_b1_cap:
                    _L0_QUOTA["bypass_b1"] = _h24_b1_cap
        except Exception:
            pass

        self._push_progress("generate_designs",
                            f"[Level 0] 物理预筛档就绪: 待去重后对 {len(params_list)} 组候选做评分+"
                            f"各路top裁剪(K={_L0_BUDGET})...")

        # ── Level 1: RMxprt 快速扫描 ──
        self._push_progress("generate_designs",
                            f"[Level 1] RMxprt 快速扫描 {len(params_list)} 组方案 (类型: {_motor_type})...")

        _first_fallback_reported = False
        def _batch_progress_l1(idx, total, params, result):
            nonlocal _first_fallback_reported
            mode_tag = "[RMX]" if result.get("solve_mode") == "rmxprt" else "[SIM]"
            # v4.02.20 (#1079): 结果字段可能为 None (模拟/降级路径), 守卫避免 f-string 抛 TypeError
            try:
                eff = float(result.get("efficiency") or 0)
            except (TypeError, ValueError):
                eff = 0.0
            try:
                temp = float(result.get("max_temp") or 0)
            except (TypeError, ValueError):
                temp = 0.0
            try:
                torque = float(result.get("torque") or 0)
            except (TypeError, ValueError):
                torque = 0.0
            self._push_progress("generate_designs",
                                f"[L1] {idx}/{total} {mode_tag} "
                                f"OD={params.get('stator_od', '?')}mm "
                                f"扭矩={torque:.1f}N·m "
                                f"效率={eff:.1f}% 温升={temp:.0f}K")
            if not _first_fallback_reported and result.get("fallback_reason"):
                _first_fallback_reported = True
                self._push_progress("generate_designs",
                                    f"RMxprt 降级: {result['fallback_reason'][:100]}",
                                    "warning")

        # v4.02.20 (#1079 关联修复): 预求解去重 — 消除 build_candidate_matrix 产出的
        # 重复候选 (日志实测 group1≡group3 / group2≡group4 精确重复), 避免重复 RMxprt
        # 求解 (浪费 6-10s/方案) 并提高退化/不可序列化结果触发概率。
        _seen_keys = set()
        _dedup_params = []
        for _p in (params_list or []):
            if not isinstance(_p, dict):
                _dedup_params.append(_p)
                continue
            try:
                _pk = json.dumps(_p, sort_keys=True, default=str)
            except Exception:
                _pk = repr(_p)
            if _pk in _seen_keys:
                continue
            _seen_keys.add(_pk)
            _dedup_params.append(_p)
        if len(_dedup_params) < len(params_list or []):
            self._push_progress("generate_designs",
                                f"[Level 1] 预求解去重: {len(params_list)}→{len(_dedup_params)} 组 "
                                f"(剔除 {len(params_list) - len(_dedup_params)} 重复)", "info")
        params_list = _dedup_params

        # FIX2 (#1109): L0 物理预筛裁剪 — 去重后、L1(RMxprt)前, 各路 top 按 L1 预算 K 门控
        #   用确定性物理评分对各轨候选排序, 取 top_n[src]=max(1,round(K×share[src]))
        #   (最大余数法归一 Σ≡K), 裁剪后送 L1, 省 RMxprt 算力且消除前报 D1/D2/D3。
        # P0 (2026-09-19): 极数硬约束注入 —— specs.poles 非零时, 预筛前剔除 poles 不匹配候选
        _req_poles = int(specs.get("poles") or 0)
        if _req_poles > 0:
            _pre_filter_n = len(params_list)
            params_list = [d for d in params_list
                          if int(d.get("poles") or 0) == _req_poles]
            _pole_filtered = _pre_filter_n - len(params_list)
            if _pole_filtered > 0:
                self._push_progress("generate_designs",
                                    f"[P0-poles 硬约束] 预筛剔除 poles 不匹配: "
                                    f"{_pre_filter_n}→{len(params_list)} 组 (剔除 {_pole_filtered})",
                                    "info")
        _l0_count = len(params_list)
        _l0_avg_eff = 0.0
        # v4.02.68 (系列三 Tier A): L0 估算保留 —— 原实现在裁剪点只取 pruned_params,
        #   L0 对候选的预测 (η/温升/转矩) 在送 L1 前即被丢弃, 致 AI 评价对 L0 "不可见"。
        #   此处接住 l0_estimates, 待 L1 返回后按设计签名 JOIN 回每个 result。
        _l0_est_map = {}
        try:
            from Scripts.physics_l0 import physics_scan_estimate
            # _L0_BUDGET / _L0_QUOTA 已在上方「档就绪」提示前统一计算 (FIX3/P2),
            #   此处直接复用, 保证显示 K 与实际裁剪 K 同源同值。
            _scan = physics_scan_estimate(params_list, l1_budget=_L0_BUDGET,
                                          quotas=_L0_QUOTA, specs=specs)
            _pruned = _scan.get("pruned_params") or []
            _l0_est_map = _scan.get("l0_estimates") or {}
            if _pruned:
                params_list = _pruned
                _l0_count = len(params_list)
                # v4.02.95 (P1-B): 全灭兜底提示 —— pruned 来自「命中带空→按 |D²L比−1|
                #   取最近 top-K」而非正常门控存活, 须显式告警 (区别于正常裁剪)。
                if _scan.get("fallback_used"):
                    self._push_progress("generate_designs",
                                        f"[Level 0] 全灭兜底(P1-B): {_scan.get('fallback_note')} "
                                        f"→ 仍送 L1 {_l0_count} 组", "warning")
                _all_scores = [s for _lst in _scan.get("scores", {}).values() for s in _lst]
                if _all_scores:
                    _l0_avg_eff = round(sum(_all_scores) / len(_all_scores), 2)
                self._push_progress("generate_designs",
                                    f"[Level 0] 物理预筛裁剪: {_scan.get('total_in')}→{_l0_count} 组 "
                                    f"(各路配额={_scan.get('alloc')}, "
                                    f"剔除{_scan.get('dropped', 0)}组, 省L1算力)",
                                    "info")
                # I3-2 (v4.02.57+): 全域扫描可行率 + 淘汰原因分布 + 可行解分布统计
                _fr = _scan.get("feasible_rate")
                _dr = _scan.get("drop_reasons") or {}
                if _fr is not None:
                    _dr_txt = ", ".join(f"{k}={v}" for k, v in sorted(_dr.items(), key=lambda x: -x[1]))
                    self._push_progress("generate_designs",
                                        f"[Level 0] 可行率={_fr*100:.1f}% "
                                        f"(淘汰原因: {_dr_txt or '无'})", "info")
                    _fd = _scan.get("feasible_dist") or {}
                    _m = (_fd.get("metrics") or {})
                    _eta = _m.get("efficiency") or {}
                    _did = _m.get("stator_id") or {}
                    if _eta.get("min") is not None:
                        self._push_progress("generate_designs",
                                            f"[Level 0] 可行解分布: D_id={_did.get('min')}~{_did.get('max')}mm, "
                                            f"η={_eta.get('min')}~{_eta.get('max')}% (中位{_eta.get('median')}%)",
                                            "info")
            else:
                # v4.02.87 G1 (item 2): L0 返回空时打印 drop_reasons 分布 —— 让「几何比越界」等
                #   淘汰原因可见 (physics_scan_estimate 始终返回该分布, 原仅裁剪成功分支打印)。
                _dr = _scan.get("drop_reasons") or {}
                _dr_txt = ", ".join(f"{k}={v}" for k, v in sorted(_dr.items(), key=lambda x: -x[1]))
                # v4.02.87 G3 (item 4): 不再扫全量退化解, 仅跑小样本诊断 (省 RMxprt 算力)。
                _diag_n = min(_L0_DIAG_CAP, len(params_list))
                self._push_progress("generate_designs",
                                    f"[Level 0] 预筛返回空, 改跑小样本诊断 {_diag_n}/{_l0_count} 组 "
                                    f"(淘汰原因: {_dr_txt or '无'})", "warning")
                params_list = params_list[:_diag_n]
        except Exception as _l0e:
            self._push_progress("generate_designs",
                                f"[Level 0] 物理预筛异常, 回退全量: {_l0e}", "warning")

        results = batch_solve(params_list, progress_cb=_batch_progress_l1,
                              solver_engine="rmxprt", machine_type=_motor_type)

        # ── C2: 物理合理性硬门禁 (v4.02.39: 修复求解成功但结果异常的方案混入TOP) ──
        # 检查项: 效率<阈值 / 转矩=0 / 外径量级异常 (OD<200mm 或 OD>2000mm) / 极数不匹配
        # 策略: 标记为 physical_flagged → 降权排序但不淘汰 (保留人工审核机会)
        _physical_flag_count = 0
        _physical_flag_reasons = []
        try:
            _eff_threshold = 0.5  # 效率<0.5% 视为异常
            _eff_lower = specs.get("efficiency_min", 60.0)  # 类型专属下限
            # P1-6 (v4.02.59+): 目标额定转矩 —— 用于「转矩达成率」门禁
            _tgt_torque = float(specs.get("rated_torque") or 0)
            if not _tgt_torque:
                try:
                    _pk = float(specs.get("power_kw") or specs.get("power") or 0)
                    _speed_rpm = float(specs.get("speed_rpm") or specs.get("speed") or 0)
                    if _pk > 0 and _speed_rpm > 0:
                        _tgt_torque = 9550.0 * _pk / _speed_rpm
                except (TypeError, ValueError):
                    _tgt_torque = 0.0
                # P0 (2026-09-19): 极数硬约束 —— 结果中 poles 不匹配即剔除（非标记）
            _req_poles_res = int(specs.get("poles") or 0)
            if _req_poles_res > 0:
                _pre_c2_n = len(results)
                results = [r for r in results
                          if not (isinstance(r, dict)
                                  and int(r.get("poles") or 0) != _req_poles_res)]
                _pole_c2_removed = _pre_c2_n - len(results)
                if _pole_c2_removed > 0:
                    self._push_progress("generate_designs",
                                        f"[C2-poles 硬剔除] 剔除 poles 不匹配: "
                                        f"{_pre_c2_n}→{len(results)} 组 (剔除 {_pole_c2_removed})",
                                        "warning")
            for r in results:
                if not isinstance(r, dict):
                    continue
                _eff = r.get("efficiency", 0.0)
                _torque = r.get("max_torque", r.get("torque", 0.0))
                _od = r.get("stator_od", r.get("od", 0.0))
                _res_poles = int(r.get("poles") or 0)
                _flags = []
                if _eff < _eff_threshold:
                    _flags.append(f"eff={_eff:.1f}%<阈值")
                if _torque == 0 and r.get("solve_failed"):
                    _flags.append("torque=0且求解失败")
                if _od and (_od < 200 or _od > 2000):
                    _flags.append(f"OD={_od:.0f}mm量级异常")
                # P0: 极数硬约束已在上文剔除，此处仅作兜底记录
                if _req_poles_res > 0 and _res_poles > 0 and _res_poles != _req_poles_res:
                    _flags.append(
                        f"poles={_res_poles}≠需求{_req_poles_res}极 (硬约束)")
                # P1-6 (v4.02.59+): 转矩达成率门禁 —— 退化方案转矩远低于目标
                #   (如 21N·m vs 19100N·m, 达成率 0.11%), 原 C2 阈值均未命中 →
                #   退化方案混入下游 Top-N。达成率 <50% 即标记 physical_flagged。
                try:
                    if _tgt_torque > 0 and _torque > 0 and _torque < 0.5 * _tgt_torque:
                        _flags.append(
                            f"转矩达成率={_torque / _tgt_torque:.1%}<50%"
                            f"(T={_torque:.0f}/{_tgt_torque:.0f}N·m)")
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
                # v4.02.68 (系列一「故障进评价」+ 系列三 C3): RMxprt 故障文本进硬门禁
                #   原 C2 只按数值表象标记 (如 eff=0.0%<阈值), error_detail 里的真实
                #   故障 (几何非法/网格失败/线规溢出) 被丢弃 → 下游评价只见现象不见根因。
                _err_txt = str(r.get("error_detail") or r.get("error") or "").strip()
                if _err_txt:
                    _flags.append(f"RMxprt故障: {_err_txt[:80]}")
                if _flags:
                    r["physical_flagged"] = True
                    r["physical_flag_reason"] = "; ".join(_flags)
                    _physical_flag_count += 1
                    _physical_flag_reasons.append(_flags[0])
        except Exception as _pe:
            self._push_progress("generate_designs",
                                f"[C2硬门禁] 异常: {_pe}", "warning")
        if _physical_flag_count > 0:
            self._push_progress("generate_designs",
                                f"[C2硬门禁] 标记 {_physical_flag_count} 个异常方案: "
                                + ", ".join(_physical_flag_reasons[:3]),
                                "warning")

        # ── B2: 方案级反思门禁 (v4.02.39 诊断改进方案) ──
        # 对通过硬门控但仍可疑的方案（如效率60-75%边缘/温升偏高）调用轻量LLM反思，
        # 标记 "needs_attention" 并输出原因，供下游排序/精评阶段参考。
        _反思_count = 0
        _反思_reasons = []
        try:
            from motor_tools import llm_comment_inject as _反思_hook
            # 筛选可疑方案: 效率60-78% 或 温升>80K 或 转矩偏差大
            _edge_cases = []
            for _r in results:
                if not isinstance(_r, dict):
                    continue
                _eff = float(_r.get("efficiency") or 0)
                _temp = float(_r.get("max_temp") or 0)
                _tq = float(_r.get("torque") or 0)
                _flags = []
                if 0 < _eff < 78:
                    _flags.append(f"eff={_eff:.1f}%偏低")
                if _temp > 80:
                    _flags.append(f"temp={_temp:.0f}K偏高")
                if _tq > 0 and specs.get("rated_torque", 0) > 0:
                    _ratio = _tq / specs["rated_torque"]
                    if _ratio < 0.7 or _ratio > 1.3:
                        _flags.append(f"torque_ratio={_ratio:.2f}偏离")
                # v4.02.68 (系列一): RMxprt 故障信息进反思门禁 —— 让下游排序/精评知道
                #   "此方案 RMxprt 报过故障", 而非仅看到效率数值偏低。
                _err_txt_b2 = str(_r.get("error_detail") or _r.get("error") or "").strip()
                if _err_txt_b2:
                    _flags.append(f"RMxprt故障: {_err_txt_b2[:80]}")
                if _flags:
                    _r["needs_attention"] = True
                    _r["attention_reason"] = "; ".join(_flags)
                    _反思_count += 1
                    _反思_reasons.append(_flags[0])
            if _反思_count > 0:
                self._push_progress("generate_designs",
                                    f"[B2反思门禁] {_反思_count} 个方案需关注: "
                                    + ", ".join(_反思_reasons[:2]),
                                    "info")
        except Exception as _be:
            self._push_progress("generate_designs",
                                f"[B2反思] 异常: {_be}", "warning")

        # Level 1 求解摘要
        _summary_l1 = get_last_summary()
        _rmxprt_count = _summary_l1.get("rmxprt", 0)
        _sim_count_l1 = _summary_l1.get("simulation", 0)

        if _summary_l1.get("error"):
            self._push_progress("generate_designs",
                                f"[Level 1] RMxprt 错误: {_summary_l1['error'][:120]}",
                                "error")
        elif _summary_l1.get("fallback_reason"):
            self._push_progress("generate_designs",
                                f"[Level 1] 降级提示: {_summary_l1['fallback_reason'][:120]}",
                                "warning")

        self._push_progress("generate_designs",
                            f"[Level 1] 完成: RMxprt={_rmxprt_count}, "
                            f"模拟={_sim_count_l1}, "
                            f"平均={_summary_l1.get('avg_time', 0):.1f}s/方案")

        # V2.1 (#700): 失败子项独立重做 — L1 后对单方案求解失败项独立重试 (带重试上限)
        # retry_failed_solutions 内部已守卫: 仅 RMxprt 可用且存在 rmxprt 结果时重试,
        # SIM/测试环境 (全 simulation) → 直接返回不改, 防重试爆炸。
        _retry_stats = {"skipped": True, "attempted": 0, "recovered": 0, "exhausted": 0}
        # v4.02.11 (半自主增强 Step8): 注入 LLM 诊断钩子 (fail-closed, 内部已限频/容错)
        try:
            from motor_tools import llm_diagnose_failure
        except Exception:
            llm_diagnose_failure = None
        results = retry_failed_solutions(results, max_retries=3,
                                         solver_engine="rmxprt",
                                         machine_type=_motor_type,
                                         llm_diag_fn=llm_diagnose_failure,
                                         llm_diag_conf_threshold=(0.4 if _aggressive else 0.6))
        _retry_stats["attempted"] = sum(1 for r in results if r.get("retry_attempted"))
        _retry_stats["recovered"] = sum(1 for r in results if r.get("retry_recovered"))
        _retry_stats["exhausted"] = sum(1 for r in results if r.get("retry_exhausted"))
        _retry_stats["skipped"] = (_retry_stats["attempted"] == 0)
        if _retry_stats["attempted"]:
            self._push_progress("generate_designs",
                                f"[重做] 失败子项独立重做: 尝试={_retry_stats['attempted']}, "
                                f"恢复={_retry_stats['recovered']}, "
                                f"耗尽={_retry_stats['exhausted']}",
                                "info" if _retry_stats["recovered"] else "warning")

        # ── v4.02.68 (系列三 Tier A): L0↔L1 差异评价 (JOIN + 残差累积) ──
        #   把 L0 预测 (l0_estimates, 按设计签名) JOIN 回 L1 实算结果, 供:
        #     (a) AI 评价新增「L0预测 → L1实算 Δ」诊断轴 (Tier A);
        #     (b) 残差飞轮累积 (Tier B) —— 故障方案须排除, 否则 Δη≈-95pp 属伪残差。
        _l0_diff_thr = 5.0     # |Δη| > 5pp 判「L0 显著偏差」
        _l0_residuals = []
        _l0_join_n = 0
        _l0_bias_n = 0
        _l0_fault_excluded_n = 0
        try:
            from Scripts.physics_l0 import design_signature as _design_sig
        except Exception:
            _design_sig = None

        # B 扩展 (2026-09-21): 绕组特征采集 —— 把线径/并绕/电密写入 L0 残差账本,
        #   供离线代理模型评估「绕组维度」的独立信息量。
        #   ⚠ 线径在候选体里的键是 RMxprt 模板名 Winding__WireDiameter, 值形如 "'1.776mm'"
        #     (含字面引号+单位), 须稳健解析; 解析失败记 None(不阻断)。
        #   ⚠ 冷却方式(cooling_method)是 specs 级常量, 候选体无该键 ⇒ per-candidate 零区分度,
        #     不入账本(加了也只是常量列)。
        def _wdia_mm(_v):
            try:
                _s = str(_v).strip().strip("'\"").lower().replace("mm", "").strip()
                return float(_s) if _s else None
            except (TypeError, ValueError):
                return None

        def _wnum(_v, _default=None):
            try:
                return float(_v)
            except (TypeError, ValueError):
                return _default

        if _l0_est_map and _design_sig is not None:
            _l0_deltas = []
            for _r in results:
                if not isinstance(_r, dict):
                    continue
                _p = _r.get("params")
                if not isinstance(_p, dict):
                    continue
                try:
                    _sig = _design_sig(_p)
                except Exception:
                    continue
                _est = _l0_est_map.get(_sig)
                if not _est:
                    continue
                _l0_join_n += 1
                _l0e = _est.get("efficiency")
                _l0t = _est.get("temp_rise_k")
                _r["l0_eff"] = _l0e
                _r["l0_temp"] = _l0t
                _r["l0_torque"] = _est.get("torque_nm")
                _r["l0_score"] = _est.get("score")
                # 故障方案: 不计算差异、不入残差统计 (C3) —— 仅以 fault=1 落账本供审计
                _fault = bool(_r.get("solve_failed") or _r.get("error") or _r.get("error_detail"))
                _r["l0_fault_excluded"] = _fault
                if _fault:
                    _l0_fault_excluded_n += 1
                    _l0_residuals.append({
                        "signature": _sig, "fault": True,
                        "fault_detail": str(_r.get("error_detail") or _r.get("error") or "")[:200],
                        "l0_eff": _l0e, "l1_eff": _r.get("efficiency"),
                        "slot_fill": _r.get("slot_fill_rate") or _r.get("slot_fill"),
                        "stator_od": _p.get("stator_od"), "stator_id": _p.get("stator_id"),
                        "core_length": _p.get("core_length"), "poles": _p.get("poles"),
                        "turns_per_coil": _p.get("turns_per_coil"),
                        "wire_dia": _wdia_mm(_p.get("Winding__WireDiameter")),
                        "n_strands": _wnum(_p.get("Winding__NumberOfStrands")),
                        "current_density": _wnum(_p.get("current_density")),
                        "l0_score": _est.get("score"),
                        "source_path": _r.get("source_path") or _p.get("source_path"),
                    })
                    continue
                try:
                    _e1 = float(_r.get("efficiency") or 0.0)
                except (TypeError, ValueError):
                    _e1 = 0.0
                if _l0e and _e1 > 0:
                    _de = round(_e1 - float(_l0e), 2)
                    _r["delta_eff"] = _de
                    _l0_deltas.append(_de)
                    if abs(_de) > _l0_diff_thr:
                        _r["l0_bias_flag"] = f"L0显著偏差 Δη={_de:+.1f}pp"
                        _l0_bias_n += 1
                if _l0t is not None:
                    try:
                        _t1 = float(_r.get("max_temp") or 0.0)
                    except (TypeError, ValueError):
                        _t1 = 0.0
                    if _t1 > 0:
                        _r["delta_temp"] = round(_t1 - float(_l0t), 1)
                _l0_residuals.append({
                    "signature": _sig,
                    "l0_eff": _l0e,
                    "l1_eff": _e1,
                    "delta_eff": _r.get("delta_eff"),
                    "l0_temp": _l0t,
                    "l1_temp": _r.get("max_temp"),
                    "delta_temp": _r.get("delta_temp"),
                    "slot_fill": _r.get("slot_fill_rate") or _r.get("slot_fill"),
                    "stator_od": _p.get("stator_od"),
                    "stator_id": _p.get("stator_id"),
                    "core_length": _p.get("core_length"),
                    "poles": _p.get("poles"),
                    "turns_per_coil": _p.get("turns_per_coil"),
                    "wire_dia": _wdia_mm(_p.get("Winding__WireDiameter")),
                    "n_strands": _wnum(_p.get("Winding__NumberOfStrands")),
                    "current_density": _wnum(_p.get("current_density")),
                    "l0_score": _est.get("score"),
                    "source_path": _r.get("source_path") or _p.get("source_path"),
                })
            if _l0_join_n:
                _mean_abs = (round(sum(abs(x) for x in _l0_deltas) / len(_l0_deltas), 2)
                             if _l0_deltas else 0.0)
                self._push_progress(
                    "generate_designs",
                    f"[L0-L1差异] JOIN={_l0_join_n} 组, 平均|Δη|={_mean_abs}pp, "
                    f"L0显著偏差={_l0_bias_n} 组 (阈值{_l0_diff_thr}pp)"
                    + (f"; 故障方案 {_l0_fault_excluded_n} 组已排除出残差统计"
                       if _l0_fault_excluded_n else ""),
                    "info")
            else:
                self._push_progress("generate_designs",
                                    "[L0-L1差异] L0 估算未能 JOIN 到任何 L1 结果 (签名不匹配), 跳过差异评价",
                                    "warning")
        elif not _l0_est_map:
            self._push_progress("generate_designs",
                                "[L0-L1差异] 无 L0 估算可用 (预筛未启用/异常), 跳过差异评价", "info")

        # ── v4.02.68 (系列三 Tier B): L0 残差入数据飞轮 + 离线标定 ──
        #   设计口径: 不让 LLM 从 10 个偏差样本"即时发明" L0 系数修正 (top-10 属幸存者
        #   偏差样本, 统计无效)。改为把残差累积进 operation_flywheel, 跨多轮离线标定,
        #   产出「高残差设计区域」提示; AI 每轮角色限于"标注高残差区域"。
        _l0_calibration = {}
        if _l0_residuals:
            try:
                from operation_flywheel import (record_l0_residuals as _rec_l0,
                                                evolve_l0_calibration as _evo_l0)
                _l0_rec = _rec_l0(project_id, _l0_residuals,
                                  specs=specs, motor_type=_motor_type,
                                  round_index=int(state.get("redo_count", 0) or 0))
                _l0_calibration = _evo_l0() or {}
                _cal_regions = _l0_calibration.get("high_residual_regions") or []
                self._push_progress(
                    "generate_designs",
                    f"[L0标定] 残差入库 {_l0_rec.get('inserted', 0)} 条 "
                    f"(累计 {_l0_calibration.get('total_samples', 0)} 条), "
                    f"全局偏置={float(_l0_calibration.get('global_bias_pp', 0) or 0):+.2f}pp"
                    + (f"; 高残差区域: {', '.join(str(x) for x in _cal_regions[:3])}"
                       if _cal_regions else ""),
                    "info")
            except Exception as _fe:
                self._push_progress("generate_designs",
                                    f"[L0标定] 残差飞轮记录失败 (忽略, 不影响主流程): {_fe}",
                                    "warning")

        # ── 按「真实求解优先 + 效率降序」分层排序 (v4.02.33 L0-B 终选门控, 方案A) ──
        # 修复: 原纯效率排序使 L0 模拟解(效率 clamp[80,96]) 凭噪声压过真实 RMxprt 解
        # (实测 93~95%)。分层: tier2=真实且未失败 > tier1=模拟/L0 > tier0=真实但失败。
        # 终选取 results[0]; 全池无真实解时退 L0 并在下方显著告警。
        # I3-1 (v4.02.57+): 同层排序键升级为多目标加权分, 传入 specs 供类型判定
        # (PMSM→cosφ 缺省 0.95 / 起动转矩软起 1.0)。
        results.sort(key=lambda r: final_selection_rank(r, specs), reverse=True)

        # v4.07 (用户请求: 过程交付物多样性度量): 无论主线A(build_candidate_matrix)
        # 还是兜底(generate_param_matrix) 路径, 均基于已求解 results 重算多样性。
        # 修复主线A 路径未触发 compute_diversity_metrics 导致 diversity_metrics 全 0 的缺陷
        # (此前仅兜底路径会填充 LAST_DIVERSITY_METRICS)。
        try:
            compute_diversity_metrics(results)
        except Exception as _de:
            self._push_progress("generate_designs",
                                f"[多样性度量] 重算失败, 沿用默认值: {_de}", "warning")

        # ── Level 2: Motor-CAD 精细验证 (仅 fine_count 个, 默认0=跳过) ──
        # v4.02.13 (#988): 由额定功率/转速推算额定转矩, 供 TOP5/TOP10 排序注入转矩权重
        _rated_torque = None
        try:
            _rp = float(specs.get("power_kw") or 0)
            _rs = float(specs.get("speed_rpm") or 0)
            if _rp > 0 and _rs > 0:
                _rated_torque = round(9550.0 * _rp / _rs, 2)
        except (TypeError, ValueError):
            _rated_torque = None
        _top_n = min(fine_count, len(results)) if fine_count > 0 else 0
        # V2.1 (#701): Top5 终选配额 — 重排全列表, 保证 main_a/B1/B2/B3/fallback 各占最小席位
        # 返回全长度重排列表 (不截断), 配额代表排在前面, L2 仍取 results[:_top_n] 即可获得多样性代表
        results = select_top_with_quota(results, top_n=_top_n, rated_torque=_rated_torque)
        _mc_count_l2 = 0
        _fine_verify_skipped = False
        _skip_reason = ""

        if fine_count <= 0:
            # v3.00.09: 用户未选择精细计算, 跳过 Motor-CAD
            _fine_verify_skipped = True
            _skip_reason = f"用户未选择精细计算 (fine_solve_count={fine_count}), 跳过 Motor-CAD"
            self._push_progress("generate_designs",
                                f"[Level 2] 跳过: {_skip_reason}", "info")
        elif _top_n == 0:
            self._push_progress("generate_designs",
                                "[Level 2] 无有效方案, 跳过精细验证",
                                "warning")
        elif self.use_local_motorcad:
            # 客户端本地 Motor-CAD 模式: 跳过主机精细验证, 由客户端自行求解
            _fine_verify_skipped = True
            _skip_reason = "客户端本地 Motor-CAD 模式, 主机跳过精细验证"
            self._push_progress("generate_designs",
                                f"[Level 2] 跳过: {_skip_reason}", "info")
        elif not self.can_use_motorcad:
            # 权限不足: 降级, 注明原因
            _fine_verify_skipped = True
            _skip_reason = "用户无主机 Motor-CAD 使用权限 (需旗舰会员或管理员), 已降级"
            self._push_progress("generate_designs",
                                f"[Level 2] 跳过: {_skip_reason}", "warning")
        else:
            # 有权限: 执行 Motor-CAD 精细验证
            self._push_progress("generate_designs",
                                f"[Level 2] Motor-CAD 精细验证 Top {_top_n} 方案...")

            top5_params = [r.get("params", {}) for r in results[:_top_n]]

            def _batch_progress_l2(idx, total, params, result):
                mode_tag = "[MC]" if result.get("solve_mode") == "motorcad" else "[SIM]"
                eff = result.get("efficiency", 0)
                self._push_progress("generate_designs",
                                    f"[L2] {idx}/{total} {mode_tag} "
                                    f"OD={params.get('stator_od', '?')}mm "
                                    f"效率={eff:.1f}%")

            fine_results = batch_solve(top5_params, progress_cb=_batch_progress_l2,
                                       solver_engine="motorcad")

            _summary_l2 = get_last_summary()
            _mc_count_l2 = _summary_l2.get("motorcad", 0)

            # 将精细验证结果合并回原结果 (替换 Top5)
            for i, fine_res in enumerate(fine_results):
                if fine_res.get("solve_mode") == "motorcad":
                    # Motor-CAD 结果覆盖 RMxprt 结果
                    fine_res["rmxprt_result"] = {
                        "efficiency": results[i].get("efficiency"),
                        "max_temp": results[i].get("max_temp"),
                        "torque": results[i].get("torque"),
                        "solve_mode": results[i].get("solve_mode"),
                    }
                    fine_res["fine_verified"] = True
                    results[i] = fine_res
                else:
                    # Motor-CAD 降级, 保留 RMxprt 结果但标记
                    results[i]["fine_verify_failed"] = fine_res.get("fallback_reason", "")
                    results[i]["fine_verified"] = False

            self._push_progress("generate_designs",
                                f"[Level 2] 完成: Motor-CAD={_mc_count_l2}, "
                                f"降级={_top_n - _mc_count_l2}")

        # 为跳过的方案标注降级原因
        if _fine_verify_skipped:
            for r in results[:_top_n]:
                r["fine_verified"] = False
                r["fine_verify_skipped"] = _skip_reason

        # v4.01.039 (P1-4 #487): 为全部方案标注精度等级 (L0/L1/L2), 供方案确认与报告披露
        for _r in results:
            self._annotate_precision(_r)

        # V2.1 (#696): 正侧分层核验 (verify_engine) 实时激活
        #  1) batch_solve 将来源标记/种子基准嵌套在 res["params"], 提升到顶层供 verify 读取
        # ★ 2026-09-29 用户授权: 追加电磁负载模态诊断字段透传 (_precompute_u_rated 写入
        #   params 嵌套层, RUN60 实测顶层缺失 ⇒ 事件流/报告/质量门 _resolve_em_mode
        #   均读不到真源)。提升后: SSE step_done / solution_summary 补齐循环 / 质量门
        #   模态感知处方 全链路可见; quality_gate._resolve_em_mode 优先读 results[0]._em_mode。
        for _r in results:
            _rp = _r.get("params") or {}
            for _k in ("source_path", "_seed", "_physics",
                       "_em_mode", "_u_rated_precomputed", "_u_rated_ratio",
                       "_u_rated_ratio_raw", "_u_rated_rewind",
                       "_u_rated_src", "_u_rated_bg_target", "_voltage_required"):
                if _k not in _r and _k in _rp:
                    _r[_k] = _rp[_k]
        #  2) 调用 verify_engine 就地追加 verify_status / verify_summary
        _verify_summary = {}
        try:
            from Core.verify_engine import verify_batch, summarize as _verify_summarize
            verify_batch(results, specs=specs)
            _vstat = _verify_summarize(results)
            _passed = _vstat.get("passed", 0)
            _verify_summary = _vstat
            self._push_progress("generate_designs",
                                f"[正侧分层核验] 通过={_passed}/{_vstat.get('total', 0)} "
                                f"(warning={_vstat.get('warning', 0)}, "
                                f"failed={_vstat.get('failed', 0)})",
                                "success" if _passed else "warning")
        except Exception as _ve:
            self._push_progress("generate_designs",
                                f"[正侧分层核验] 执行失败, 跳过: {_ve}", "warning")

        # v4.02.12 (Phase 2 P0: L2→Top10 重排): 精细验证方案 + 综合最优前置 (仅重排顺序)
        try:
            from motor_tools import rerank_l2_top10
            if fine_count > 0:
                results = rerank_l2_top10(results, top_n=10, rated_torque=_rated_torque)
                self._push_progress("generate_designs",
                                    "[L2→Top10] 已按精细验证+综合分重排, 最优方案前置", "info")
        except Exception as _re:
            self._push_progress("generate_designs",
                                f"[L2→Top10] 重排失败, 保持原序: {_re}", "warning")

        # 预导入 Phase 2 辅助函数 (fail-closed)
        try:
            from motor_tools import compute_nvh_risk, verify_design_checks
            _has_phase2 = True
        except Exception:
            _has_phase2 = False

        # v4.07 (用户请求: 每个方案 AI 一句话评价 + 重复方案去重):
        # 1) 逐方案生成 ai_comment + 附加 NVH 风险 / 设计校验 (规则化可解释物理评价)
        # 2) 去重, 保证 Top-N 交付物为物理互异方案 (修复重复方案未去重缺陷)
        for _r in results:
            if isinstance(_r, dict) and not _r.get("ai_comment"):
                try:
                    _r["ai_comment"] = make_ai_comment(_r)
                except Exception:
                    _r["ai_comment"] = ""
            if _has_phase2 and isinstance(_r, dict):
                try:
                    if "nvh_risk" not in _r:
                        _r["nvh_risk"] = compute_nvh_risk(_r).get("nvh_risk", "低")
                    if "design_checks" not in _r:
                        _r["design_checks"] = verify_design_checks(_r, specs)
                except Exception:
                    pass
        results = dedup_results(results)
        # I3-3 (v4.02.57+): TOP10 单变量扰动敏感度分析 (对齐参考方案 Step6)
        #   对去重后的 TOP10 方案参数做 ±5% 扰动, 用 L0 性能估算器中心差分, 输出敏感度表。
        try:
            from Scripts.physics_l0 import sensitivity_analysis, format_sensitivity_table
            _top_params = [r.get("params") for r in results[:10]
                           if isinstance(r, dict) and isinstance(r.get("params"), dict)]
            if _top_params:
                _sens = sensitivity_analysis(_top_params, specs, perturb=0.05)
                self._push_progress("generate_designs",
                                    "[敏感度] TOP方案单变量扰动(±5%) 敏感度表:\n    "
                                    + format_sensitivity_table(_sens),
                                    "info")
        except Exception as _se3:
            self._push_progress("generate_designs",
                                f"[敏感度] 分析失败, 跳过: {_se3}", "warning")
        # v4.02.11 (半自主增强 Step12): LLM 精评合并 (fail-closed, 新增 ai_comment_llm 字段)
        #   规则评价 ai_comment 保留, 仅对 Top-10 追加 LLM 精评; 全AI智能模式额外取 LLM 优先级重排
        try:
            from motor_tools import llm_comment_inject
            _top_n = 10  # v4.02.14 (#1001): 半智能与全AI智能统一 Top-10 精评
            # v4.02.13 (#990): 逐方案进度回调, 消除慢模型下 LLM 精评静默假死
            self._push_progress("generate_designs",
                                f"[LLM精评] 进入 Top-{_top_n} 方案 LLM 逐方案精评 (可能耗时较长)...",
                                "info")

            def _llm_progress(i, total, r):
                _od = (r.get("stator_od") if isinstance(r, dict) else None) or "?"
                self._push_progress("generate_designs",
                                    f"[LLM精评] 正在生成评价 ({i}/{total}) OD={_od}mm ...",
                                    "info")

            results = llm_comment_inject(results, top_n=_top_n, aggressive=_aggressive,
                                         progress_cb=_llm_progress)
            self._push_progress("generate_designs",
                                f"[LLM精评] Top-{_top_n} 方案精评完成", "success")
            # 全AI智能: 若 LLM 给出优先级, 按优先级稳定重排 (仅排序, 不增删)
            if _aggressive:
                try:
                    if any(isinstance(_r, dict) and _r.get("ai_priority") for _r in results):
                        results.sort(key=lambda r: (
                            -(r.get("ai_priority", 2) if isinstance(r, dict) else 2),
                            -(r.get("efficiency", 0) or 0)))
                        self._push_progress("generate_designs",
                                            "[全AI智能·Step12] 已按 LLM 优先级重排", "info")
                except Exception:
                    pass
        except Exception as _ce:
            self._push_progress("generate_designs",
                                f"[半自主·Step12] LLM 精评失败, 保留规则评价: {_ce}", "warning")

        # v2.03.02: 检测 Motor-CAD 授权错误并推送告警
        if _summary_l1.get("licence_error"):
            self._push_progress("generate_designs",
                                f"[授权错误] {_summary_l1['licence_error']}",
                                "error")

        if results:
            # v4.02.33 (L0-B 终选门控): results 已在 L2804 按 [真实解优先, 效率降序] 分层排序,
            # results[0] 即终选最优 (真实求解解优先于 L0 模拟解)。
            best = results[0]
            best_mode = best.get("solve_mode", "?")
            best_fine = " [精细验证]" if best.get("fine_verified") else ""
            best_prec = best.get("precision_label", "")
            # 终选可信度告警: 终选非真实求解解 → 显著提示
            _real_n = count_real(results)
            _best_is_real = is_real_solution(best) and not best.get("solve_failed")
            if not _best_is_real:
                self._push_progress("generate_designs",
                    f"[终选告警] 终选方案非真实求解解 (L0 模拟/降级或真实求解失败); "
                    f"全池真实解 {_real_n}/{len(results)} 组, 该结果可信度低, 仅供参照!",
                    "error" if _real_n == 0 else "warning")
            self._push_progress("generate_designs",
                                f"两级求解完成: {len(results)} 组方案, "
                                f"终选效率={best.get('efficiency', 0):.2f}% "
                                f"({best_mode}{best_fine}, 精度={best_prec})"
                                f"{'' if _best_is_real else ' [⚠ L0模拟/降级]'}",
                                "success" if _best_is_real else "warning")

        # v4.02.12 (Phase 2 P1): 效率 Map 估算 (基于最优方案额定点, 明确标注估算, fail-closed)
        _efficiency_map = []
        try:
            from motor_tools import estimate_efficiency_map
            if results:
                # v4.02.33: 效率 Map 估算基于终选(真实解优先)方案, 不再取纯效率最高
                _best_for_map = results[0]
                _efficiency_map = estimate_efficiency_map(_best_for_map)
        except Exception as _eme:
            self._push_progress("generate_designs",
                                f"[效率Map] 估算失败, 跳过: {_eme}", "warning")

        # v4.02.12 (Phase 3): Reflection 复盘 + Memory 记忆写入 (均 fail-closed)
        _reflection = {}
        # ── ★ H22 (2026-09-25 用户指令): 案例回写迁出本节点 ──────────────────
        #   原实现每轮 generate_designs 把 results[:3] 回写案例库
        #   (RUN52 实测: 9 轮 × 3 条 = 27 条/次工作流, 且 specs_json.efficiency=0.92
        #   是**需求目标非实测**), 失败/中途设计正固化为未来种子真值 (案例库污染,
        #   近两日累计 89 条同需求重复)。
        #   现迁至 _node_decision_gate (终选点) 并加三闸门, 见 _write_back_case_memory():
        #     ① 真实解 ∧ η>0 ∧ η≥需求下限 ∧ P/T 达标 ≥0.95 ∧ 质量门通过;
        #     ② specs_json.efficiency 写实测值 (禁写需求目标);
        #     ③ 同一 project_id 仅终选回写 1 条。
        try:
            from Core.agent_reflection import reflect as _reflect_fn
            _reflection = _reflect_fn(results, _design_plan, specs)
        except Exception as _rfe:
            self._push_progress("generate_designs",
                                f"[Reflection] 复盘失败, 跳过: {_rfe}", "warning")

        # v2.04: 记录运营指标
        _duration_ms = int((time.time() - _start_ts) * 1000)
        _quality = 80 if results else 0
        if results:
            eff_avg = sum(r.get("efficiency", 0) for r in results) / len(results)
            _quality = min(100, eff_avg)
        record_step_metric(project_id, "generate_designs", "DesignAgent",
                           duration_ms=_duration_ms,
                           quality_score=_quality,
                           quality_issues=f"RMxprt={_rmxprt_count}, "
                                          f"SIM={_sim_count_l1}, "
                                          f"MC_fine={_mc_count_l2}, "
                                          f"results={len(results)}")

        # v2.05.03: 记录电脑计算耗时成本
        self._get_cost_tracker().project_id = project_id
        self._get_cost_tracker().record_compute_time("generate_designs",
                                              cpu_hours=_duration_ms / 3600000.0,
                                              detail={"rmxprt_count": _rmxprt_count,
                                                      "sim_count": _sim_count_l1,
                                                      "mc_fine_count": _mc_count_l2,
                                                      "results": len(results)})

        # v4.01.037 (P0-1): 显式回写被回灌修正的参数, 使修正在 state 中持久化
        _out = {"results": results,
                "matched_templates": matched,
                "param_matrix": params_list,
                # v4.02.12 (Phase 2/3): 新增过程交付物随 state 下传
                "efficiency_map": _efficiency_map,
                "design_plan": _design_plan,
                "reflection_log": _reflection,
                "autonomous_mode": _autonomous_mode,
                # v4.04 (#10): 方案多样性度量随 state 下传 (报告/确认节点可引用)
                "diversity_metrics": dict(LAST_DIVERSITY_METRICS)}
        if _fb_update:
            _out.update(_fb_update)
            _out["specs"] = specs
            if base_params:
                _out["base_params"] = base_params
            if base_param_ranges:
                _out["base_param_ranges"] = base_param_ranges
        if scenario_info:
            _out["scenario_info"] = scenario_info

        # v4.01.055 (用户请求: 参数矩阵过程交付物 4 段结果): 捕获 4 阶段结果随 SSE step_done 下发
        _out["param_matrix_meta"] = {
            "scan_count": len(params_list),
            # v4.04 (#9→P1-4): 飞轮降量后的实际目标方案数随 SSE 过程交付物下发
            "actual_count": actual_count,
            "flywheel_excluded": LAST_FLYWHEEL_EXCLUDED,
            "mutated": LAST_DIVERSITY_METRICS.get("mutated", 0),
            "coverage": LAST_DIVERSITY_METRICS.get("coverage", 0.0),
            "innovation": LAST_DIVERSITY_METRICS.get("innovation", 0.0),
            "clustered": LAST_DIVERSITY_METRICS.get("clustered", False),
            "interpretation": LAST_DIVERSITY_METRICS.get("interpretation", ""),
        }
        # v4.02.61 (故障修复 #1): _sp / _seed_verify 防御性校验 —— 杜绝任何非 dict 漏网触发 .get() 崩溃
        #   (历史回归: v4.02.59 P1-6 在 C2 门禁用 _sp 存转速 float 覆盖了种子 dict, 致 L3427 崩)
        if not isinstance(_sp, dict):
            _sp = {"seeds": [], "catalog_count": 0, "case_count": 0,
                   "backfilled": 0, "incomplete": 0}
        if not isinstance(_seed_verify, dict):
            _seed_verify = {"seed_count": 0, "l1_enabled": False, "skipped": True}
        # v4.01.060 (用户请求: 基准参数增加 5 个种子内容): 种子来源构成随 SSE step_done 下发
        _out["base_seeds"] = {
            "seeds": _seeds,
            "catalog_count": _sp.get("catalog_count", 0),
            "case_count": _sp.get("case_count", 0),
            "backfilled": _sp.get("backfilled", 0),
            "incomplete": _sp.get("incomplete", 0),
            # v4.02.36 (种子评价方案): 阶段2 合理性 + 阶段3 L1 校核摘要随 SSE 下发
            "verification": _seed_verify,
        }
        # v4.02.36: 独立通道, 便于管理端/客户端直接读取种子评价结论
        _out["seed_verification"] = _seed_verify
        # v4.01.057 (用户请求: TOP10方案过程交付物 3 段改造): 捕获物理求解过程摘要随 SSE step_done 下发
        _prec_counts = {
            "L2": sum(1 for r in results if r.get("precision_level") == "L2"),
            "L1": sum(1 for r in results if r.get("precision_level") == "L1"),
            "L0": sum(1 for r in results if r.get("precision_level") == "L0"),
        }
        _out["solve_summary"] = {
            "level1": {
                "engine": "RMxprt",
                "total": len(params_list),
                "rmxprt_count": _rmxprt_count,
                "sim_count": _sim_count_l1,
                "avg_time_s": round(float(_summary_l1.get("avg_time", 0) or 0), 2),
                "fallback_reason": _summary_l1.get("fallback_reason", "") or "",
            },
            "level2": {
                "engine": "Motor-CAD",
                "enabled": (fine_count > 0) and (not _fine_verify_skipped),
                "top_n": _top_n,
                "mc_count": _mc_count_l2,
                "skipped": _fine_verify_skipped,
                "skip_reason": _skip_reason or "",
            },
            "precision": {
                "method": "L0/L1/L2 三级精度标注",
                "L2": "精细验证(Motor-CAD, real FEA)",
                "L1": "RMxprt快速扫描",
                "L0": "模拟估算(降级)",
                "counts": _prec_counts,
            },
            # V2.1 (#696): 正侧分层核验分布随 solve_summary 下发
            "verify": {
                "engine": "verify_engine",
                "total": _verify_summary.get("total", 0),
                "passed": _verify_summary.get("passed", 0),
                "warning": _verify_summary.get("warning", 0),
                "failed": _verify_summary.get("failed", 0),
                "by_path": _verify_summary.get("by_path", {}),
            },
            "candidate_meta": _cand_meta,
            "sort": "按效率降序 (efficiency, reverse=True)",
            "fine_count": fine_count,
            # V2.1 (#699/#700/#701): 三件套统计并入已声明通道 solve_summary
            "level0": {
                "engine": "L0物理预筛(确定性, FIX2#1109)",
                "enabled": True,
                "total": _l0_count,
                "avg_efficiency": _l0_avg_eff,
            },
            "retry": {
                "enabled": (not _retry_stats["skipped"]),
                "attempted": _retry_stats["attempted"],
                "recovered": _retry_stats["recovered"],
                "exhausted": _retry_stats["exhausted"],
            },
            "quota": {
                "enabled": True,
                "top_n": _top_n,
                "policy": "main_a/bypass_b1/bypass_b2/bypass_b3/fallback 各占≥1席",
            },
        }
        if _fb_update:
            _out["review_feedback_result"] = {
                "applied": _fb_update.get("feedback_applied", []),
                "skipped": _fb_update.get("feedback_skipped", []),
                "rejected": _fb_update.get("feedback_rejected", []),
                "source": _fb_update.get("feedback_source", ""),
                "need_manual": _fb_update.get("need_manual_feedback", False),
            }
            # 仅当评审回灌真正改变了参数 (feedback_applied 非空) 才触发了场景重新识别
            _out["scenario_reidentified"] = bool(_fb_update.get("feedback_applied"))

        # v4.01.061 (用户请求: 过程交付物多次反复全量体现): 计算「第N次 + 重复原因」随 SSE 下发
        _a_regen = bool(state.get("auto_regenerate"))
        _a_redo_qg = int(state.get("redo_count", 0) or 0)
        _a_redo = int(state.get("design_redo_count", 0) or 0)
        _attempt_index = 1 + _a_redo_qg + _a_redo
        if _a_regen:
            _attempt_reason = "质量门禁未通过, 自动重生"
        elif _a_redo > 0:
            _attempt_reason = "人工确认(hc_design)局部重做"
        else:
            _attempt_reason = "首次生成 (无反复)"
        if regen_guidance:
            _attempt_reason += f"; 改进指引: {regen_guidance[:160]}"
        # v4.02.68 (系列三 Tier A/B): L0↔L1 差异 + 残差飞轮标定随 SSE 下发
        #   (客户端/报告可据 l0_l1_diff 披露"预筛可靠性", 据 l0_calibration 展示高残差区域)
        _deltas_out = [float(_x["delta_eff"]) for _x in _l0_residuals
                       if isinstance(_x, dict)
                       and isinstance(_x.get("delta_eff"), (int, float))]
        _out["l0_l1_diff"] = {
            "enabled": bool(_l0_est_map),
            "joins": _l0_join_n,
            "bias_count": _l0_bias_n,
            "fault_excluded": _l0_fault_excluded_n,
            "threshold_pp": _l0_diff_thr,
            "avg_abs_delta_pp": (round(sum(abs(_v) for _v in _deltas_out) / len(_deltas_out), 2)
                                 if _deltas_out else 0.0),
            "note": "L0 为经验快筛(非物理求解); 差异仅作诊断轴, 不据此直接改 L0 系数",
        }
        _out["l0_l1_residuals"] = _l0_residuals
        _out["l0_calibration"] = _l0_calibration
        _out["attempt_index"] = _attempt_index
        _out["attempt_reason"] = _attempt_reason
        # v4.01.063 (用户请求: 补 Gap A): 需求解析 8 步流水线过程交付物随 generate_designs SSE 下发,
        # 使方案生成阶段事件自包含 (此前仅 parse_requirement 事件携带)
        _out["pipeline_steps"] = state.get("pipeline_steps", []) or []
        # v4.06 (base_seeds 持久化): 将本批次种子快照挂到实例属性, 供后端
        # GET /api/workflow/seed_snapshot 读取。SSE step_done 已随 app.py L1915 转发
        # base_seeds (context_for_ui 通道已满足), 此处补齐 checkpoint 级持久化, 解决
        # 重生重发 / 重渲染时节点的中间交付物第⑤段「未提取到种子 / 后端未返回 base_seeds」
        # (字段在 workflow_state 被清空后无快照可回补) 的根因。
        # v4.02.x (方案B): TOP10 几何截图路径随 SSE 下发 — 供客户端「4. top10 方案图示」渲染。
        #   来源: 每个 result['images']={'geometry':[path]} 由 worker 抓屏写入,
        #   rmxprt_tools._convert_venv_entry 已透传。此处收敛为 {rank,paths,od,poles,eff} 列表,
        #   保证即便 results 在 SSE 中被裁剪, 几何路径也必达客户端。fail-closed。
        _geom_imgs = []
        for _gi, _gr in enumerate((results or [])[:10]):
            if not isinstance(_gr, dict):
                continue
            _gim = _gr.get("images")
            if not isinstance(_gim, dict):
                continue
            _gpaths = _gim.get("geometry")
            if not _gpaths or not isinstance(_gpaths, list):
                continue
            _valid = [str(_p) for _p in _gpaths if _p]
            if not _valid:
                continue
            # v4.02.69 (跨机可达): 除后端绝对路径外, 同时下发 basename 与相对 URL。
            #   场景: 客户端与后端不在同一主机 (Tailscale/LAN 远程连 5000 口) 时,
            #   worker 抓屏落盘的 D:\...\rmxprt_images\*.bmp 在客户端 os.path.exists 必失败,
            #   客户端可据 files/urls 经 GET /api/rmxprt_image/<file> 拉取并缓存后渲染。
            #   同机场景下 paths 直接命中, 不产生额外网络开销 (客户端优先本地)。
            _files_out, _urls_out = [], []
            for _p in _valid:
                _bn = os.path.basename(str(_p).replace("\\", "/"))
                if not _bn:
                    continue
                _files_out.append(_bn)
                _urls_out.append(f"/api/rmxprt_image/{_bn}")
            _geom_imgs.append({
                "rank": _gi + 1,
                "paths": _valid,
                "files": _files_out,
                "urls": _urls_out,
                "od": _gr.get("stator_od"),
                "poles": _gr.get("poles"),
                "eff": _gr.get("efficiency"),
            })
        _out["geometry_images"] = _geom_imgs

        self._last_base_seeds = _out.get("base_seeds", {}) or {}
        return _out

    # ==================== v2.05.06: 方案确认检查点 ====================
    def _node_hc_design(self, state: WorkflowState) -> dict:
        """v2.05.06: 方案生成师输出后的人机交互检查点"""
        project_id = state.get("project_id", "")
        results = state.get("results", [])

        self._push_progress("hc_design", "创建方案确认检查点...")

        # v4.02.17 P2-2: TOP10 全参数交付物 — 扩到前10, 并补齐全部输入/输出/派生参数
        top_results = results[:10] if results else []
        solution_summary = []
        for i, r in enumerate(top_results):
            eff = r.get("efficiency", 0)
            # v3.00.11: 效率值合理性过滤
            if eff < 0 or eff > 100:
                eff = 0  # 标记无效, 不显示异常值
            item = {
                "rank": i + 1,
                "efficiency": round(eff, 2) if eff else 0,
                "max_temp": round(r.get("max_temp", 0), 1) if r.get("max_temp") else 0,
                "torque": r.get("torque", 0),
                "power": r.get("power", 0),
                # 结构参数 (详细参数对比列)
                "stator_od": r.get("stator_od", 0),
                "stator_id": r.get("stator_id", 0),
                "rotor_od": r.get("rotor_od", 0),
                "core_length": r.get("core_length", 0),
                "air_gap": r.get("air_gap", 0),
                "tooth_width": r.get("tooth_width", 0),
                "yoke_thickness": r.get("yoke_thickness", 0),
                "shaft_dia": r.get("shaft_dia", 0),
                "poles": r.get("poles", 0),
                "slots_stator": r.get("slots_stator", 0),
                "slots_rotor": r.get("slots_rotor", 0),
                "turns_per_coil": r.get("turns_per_coil", 0),
                "voltage": r.get("voltage", 0),
                "speed": r.get("speed", 0),
                # 电磁/热性能 (详细参数对比列)
                "stator_tooth_flux_density": r.get("stator_tooth_flux_density", 0),
                "stator_yoke_flux_density": r.get("stator_yoke_flux_density", 0),
                "rotor_yoke_flux_density": r.get("rotor_yoke_flux_density", 0),
                "air_gap_flux_density": r.get("air_gap_flux_density", 0),
                "current_density": r.get("current_density", 0),
                "power_factor": r.get("power_factor", 0),
                "copper_loss": r.get("copper_loss", 0),
                "iron_loss": r.get("iron_loss", 0),
                "total_loss": r.get("total_loss", 0),
                # v4.07: 每方案 AI 一句话评价
                "ai_comment": r.get("ai_comment", ""),
                "solve_mode": r.get("solve_mode", ""),
                "precision_level": r.get("precision_level", "L1"),
                "precision_label": r.get("precision_label", ""),
            }
            # v4.02.17 P2-2: 补齐全部原始/派生参数 (标量/字符串), 确保 TOP10 "所有参数"完整
            _covered = set(item.keys())
            for _k, _v in r.items():
                if _k in _covered:
                    continue
                if isinstance(_v, dict):
                    if _k == "rmxprt_result":
                        for _rk, _rv in _v.items():
                            if isinstance(_rv, (int, float, str, bool)) or _rv is None:
                                _nk = f"rmxprt_{_rk}"
                                if _nk not in _covered:
                                    item[_nk] = _rv
                    continue
                if isinstance(_v, (int, float, str, bool)) or _v is None:
                    item[_k] = _v
            solution_summary.append(item)

        checkpoint_id = self._push_human_checkpoint(
            step="hc_design",
            project_id=project_id,
            checkpoint_type="design_review",
            title="方案确认",
            description="请审阅方案生成师的求解结果,确认或要求重新生成",
            db_context={
                "total_solutions": len(results),
                "top_solutions": solution_summary,
                "best_efficiency": top_results[0].get("efficiency", 0) if top_results else 0,
                "best_temp": top_results[0].get("max_temp", 0) if top_results else 0,
            },
            timeout_seconds=hc.DEFAULT_TIMEOUTS["design_review"],
            default_action="auto_approve",
        )

        self._get_cost_tracker().project_id = project_id
        human_event = self._get_cost_tracker().start_human_step("hc_design",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            if remaining % 60 == 0 and remaining > 0:
                self._push_progress("hc_design",
                                    f"方案确认倒计时: 剩余 {remaining//60} 分钟", "info")

        # v5.1: 本阶段 LLM 工作已完成, 人工检查点即将阻塞等待; 显式释放 Ollama 模型
        #        避免模型在人工思考期间空占显存 (流程内连续阶段仍靠 keep_alive=5m 不反复重载)
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 检查点释放模型失败(忽略): %s", _e)

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_approve")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        self._get_cost_tracker().end_human_step(human_event, decision=decision)

        # v4.01.029: 运营质量指标
        self._record_quality_event(project_id, "design_review", decision,
                                   checkpoint_id=checkpoint_id,
                                   is_timeout=is_timeout)

        # v4.06-P4: 统一回退策略 — 集中重做上限保护 (本检查点无关键字段补缺)
        decision, _fb_specs, _fb_msg = self._apply_checkpoint_fallback(
            "hc_design", state, decision, {}, is_timeout)
        if _fb_msg:
            self._push_progress("hc_design", f"⚠️ 回退策略: {_fb_msg}", "warn")

        if is_timeout:
            self._push_progress("hc_design",
                                f"方案确认超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("hc_design",
                                f"收到人工决策: {decision}", "success")

        _out_state = {"human_decision": decision,
                      "human_comment": comment,
                      "hc_checkpoint_id": checkpoint_id}
        # v4.01.039 (P1-6 #486): 局部重做 — 计数 + 将审核意见作为生成指引(regenerate_guidance)
        # 注: regenerate_guidance 已在 WorkflowState 声明, generate_designs 会在有指引时扩大候选池
        if decision == "reject":
            _out_state["design_redo_count"] = int(state.get("design_redo_count", 0) or 0) + 1
            if comment:
                _out_state["regenerate_guidance"] = comment
        return _out_state

    # ==================== v4.06: 案例电机对标分析 ====================

    def _load_case_motor_for_review(self, state: WorkflowState) -> dict:
        """加载案例电机数据用于评审对标 (返回 None 表示无案例参考)"""
        case_id = state.get("case_motor_id", 0)
        if not case_id or not _CM_DB_OK:
            return None
        try:
            return get_case_motor(case_id)
        except Exception:
            return None

    def _compute_case_motor_match(self, solutions: list, case_motor: dict) -> list:
        """
        计算每个方案与案例电机已知参数的匹配度。
        返回 [{"solution_index": 0, "match_score": 92.5, "matched_params": {...}, "mismatches": [...]}, ...]
        """
        if not case_motor or not solutions:
            return []

        results = []
        # 案例电机已知参数 (None/空 = 未知, 不参与对标)
        case_fields = {
            "efficiency": ("目标效率", float, 1.0),        # ±1% 容差
            "power_factor": ("功率因数", float, 0.05),       # ±0.05 容差
            "stator_od": ("定子外径", float, 2.0),           # ±2mm 容差
            "stator_id": ("定子内径", float, 2.0),
            "rotor_od": ("转子外径", float, 2.0),
            "core_length": ("铁心长度", float, 5.0),          # ±5mm 容差
            "air_gap": ("气隙长度", float, 0.1),
            "rated_current": ("额定电流", float, 5.0),         # ±5A 容差
        }

        # 方案字段到案例字段的映射
        sol_to_case = {
            "efficiency": "efficiency",
            "power_factor": "power_factor",
            "stator_od": "stator_od",
            "stator_id": "stator_id",
            "rotor_od": "rotor_od",
            "core_length": "core_length",
            "air_gap": "air_gap",
            "rated_current_a": "rated_current",
        }

        for idx, sol in enumerate(solutions[:10]):
            matched = {}
            mismatches = []
            total_fields = 0
            matched_fields = 0

            for sol_key, case_key in sol_to_case.items():
                case_val = case_motor.get(case_key)
                sol_val = sol.get(sol_key)
                if case_val is None or case_val == "" or sol_val is None:
                    continue

                try:
                    case_val = float(case_val)
                    sol_val = float(sol_val)
                except (ValueError, TypeError):
                    continue

                _, _, tolerance = case_fields[case_key]
                diff = abs(sol_val - case_val)
                total_fields += 1
                if diff <= tolerance:
                    matched_fields += 1
                    matched[sol_key] = {"case": case_val, "solution": sol_val, "diff": round(diff, 3)}
                else:
                    mismatches.append({
                        "param": case_fields[case_key][0],
                        "case_value": case_val,
                        "solution_value": sol_val,
                        "diff": round(diff, 3),
                        "tolerance": tolerance,
                    })

            match_score = round(matched_fields / total_fields * 100, 1) if total_fields > 0 else 0.0
            results.append({
                "solution_index": idx,
                "match_score": match_score,
                "matched_params": matched,
                "mismatches": mismatches,
                "total_compared": total_fields,
                "matched_count": matched_fields,
            })

        return results

    def _build_case_motor_review_section(self, match_results: list, case_motor: dict) -> str:
        """构建案例电机对标分析文本 (用于支持报告)"""
        if not case_motor or not match_results:
            return ""

        case_code = case_motor.get("case_code", "")
        motor_type = case_motor.get("motor_type", "")

        parts = [
            f"\n## 案例电机对标分析 (参考: {motor_type} {case_code})\n",
            f"以下为 Top10 方案与案例电机已知参数的匹配度评估:\n",
            "| 方案# | 匹配度 | 匹配参数 | 不匹配参数 |",
            "|-------|--------|----------|------------|",
        ]

        for mr in match_results:
            idx = mr["solution_index"]
            score = mr["match_score"]
            matched_n = mr["matched_count"]
            total = mr["total_compared"]
            mismatch_names = ", ".join(m["param"] for m in mr["mismatches"]) or "无"
            parts.append(f"| 方案{idx+1} | {score}% ({matched_n}/{total}) | {matched_n}项 | {mismatch_names} |")

        # 找出最高匹配度方案
        if match_results:
            best = max(match_results, key=lambda x: x["match_score"])
            parts.append(f"\n**最高匹配度方案**: 方案{best['solution_index']+1} ({best['match_score']}%匹配)")
            if best["mismatches"]:
                parts.append("\n主要偏差:")
                for m in best["mismatches"][:3]:
                    parts.append(f"- {m['param']}: 案例={m['case_value']}, 方案={m['solution_value']}, 偏差={m['diff']}")
            parts.append("\n> 💡 案例电机数据来自实际产品验证, 匹配度越高说明方案越接近成熟设计。")

        return "\n".join(parts)

    def _build_case_motor_convergence_section(self, match_results: list, case_motor: dict) -> str:
        """构建案例电机收敛校验文本 (用于反对报告)"""
        if not case_motor or not match_results:
            return ""

        case_code = case_motor.get("case_code", "")
        parts = [f"\n## 案例电机校验 (参考: {case_code})\n"]

        # 找出低匹配度方案 (警告)
        low_matches = [mr for mr in match_results if mr["match_score"] < 50.0]
        if low_matches:
            parts.append("⚠️ 以下方案与案例电机差异较大, 需关注:\n")
            for mr in low_matches:
                mismatch_detail = "; ".join(
                    f"{m['param']}(偏差{m['diff']})" for m in mr["mismatches"][:2]
                )
                parts.append(f"- 方案{mr['solution_index']+1}: 匹配度仅 {mr['match_score']}% — {mismatch_detail}")
            parts.append("")
        else:
            parts.append("所有方案与案例电机匹配度均 >=50%, 参数一致性良好。\n")

        # 检查关键参数偏差
        critical_params = ["efficiency", "power_factor", "air_gap"]
        for mr in match_results[:5]:
            for m in mr["mismatches"]:
                if any(cp in m["param"] for cp in ["效率", "功率因数", "气隙"]):
                    parts.append(f"- 方案{mr['solution_index']+1} {m['param']}偏差: 案例={m['case_value']}, 方案={m['solution_value']} (超容差{m['diff']})")

        return "\n".join(parts)

    def _case_motor_verdict(self, match_results: list) -> str:
        """v4.06-P3: 由匹配度列表推导案例电机校验结论 (UI 友好结构化 verdict)
        - "empty": 无对标数据 (未匹配到案例已知参数)
        - "warn":  存在匹配度 <50% 的方案 (偏差较大, 需关注)
        - "ok":    所有方案匹配度均 >=50%, 参数一致性良好
        """
        if not match_results:
            return "empty"
        _low = [mr for mr in match_results if mr.get("match_score", 0) < 50.0]
        return "warn" if _low else "ok"

    # ==================== 放大轨 ====================

    def _node_review_solutions(self, state: WorkflowState) -> dict:
        """放大轨: 评审推荐师 → 支持报告"""
        self._push_progress("review_solutions", "评审推荐师开始分析方案矩阵...")
        self._push_progress("review_solutions",
                            f"输入: {len(state['results'])} 组方案, 正在调用本地 LLM 生成支持报告...")

        # v4.02.66: 评审 LLM 单步超时 (点评级护盾) 取 workflow_config 统一治理值,
        #   用于心跳文案与等待上界; 默认 600s (Ollama 过载时避免长时间无日志/感知卡死)。
        try:
            from workflow_config import get_point_review_timeout as _get_prt
            _review_to = int(_get_prt())
        except Exception:
            _review_to = 600

        _start_ts = time.time()
        project_id = state.get("project_id", "")
        use_cloud = state.get("use_cloud_review", False)

        # v4.06: 案例电机对标分析
        case_motor = self._load_case_motor_for_review(state)
        case_match_results = []
        case_review_section = ""
        if case_motor:
            self._push_progress("review_solutions",
                                f"案例电机对标: 参考 {case_motor.get('case_code', '')}, "
                                f"计算 Top10 方案匹配度...")
            case_match_results = self._compute_case_motor_match(state["results"], case_motor)
            case_review_section = self._build_case_motor_review_section(case_match_results, case_motor)

        # v4.02.66: 评审 LLM 可能长阻塞 (Ollama 过载), 改用后台线程执行 safe_run,
        #   主线程每 30s 推送一次心跳进度, 避免长时间无日志导致用户误判"流程卡死"。
        #   心跳等待上界 = _review_to + 60s 容差, 超出则强制 join 取线程结果 (safe_run 内部
        #   已有 600s 护盾, 正常不会触达此上界)。
        _result_holder = {}
        _rv_done = threading.Event()

        def _rv_target():
            try:
                _result_holder["r"] = self.review_agent.safe_run(
                    results=state["results"], specs=state["specs"], use_cloud=use_cloud,
                    case_motor=case_motor, case_match=case_match_results,
                    # v4.02.68 (系列三): L0↔L1 差异 + 高残差区域标定提示 —— 供 AI 评价
                    #   新增「L0预测 vs L1实算」诊断轴, 并对高残差区域做标注 (不发明系数)
                    l0_calibration=state.get("l0_calibration") or {},
                    l0_l1_diff=state.get("l0_l1_diff") or {})
            except Exception as _rv_e:
                _result_holder["r"] = {"error": f"评审线程异常: {_rv_e}"}
            finally:
                _rv_done.set()

        _rv_thread = threading.Thread(target=_rv_target, daemon=True)
        _rv_thread.start()
        _rv_beat = 0
        while not _rv_done.wait(timeout=30):
            _rv_beat += 1
            _rv_elapsed = int(_rv_beat * 30)
            self._push_progress("review_solutions",
                                f"评审 LLM 推理中 (已等待 {_rv_elapsed}s, 单步超时 {_review_to}s) "
                                f"— 模型正常生成中, 请稍候...", "info")
        _rv_thread.join(timeout=_review_to + 60)
        result = _result_holder.get("r", {"error": "评审线程超时未返回"})

        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            err_msg = result["error"]
            self._push_progress("review_solutions", f"评审失败: {err_msg}", "error")
            record_step_metric(project_id, "review_solutions", "ReviewAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=err_msg[:200])
            # v4.01.052: 评审失败时拼入具体错误原因, 让客户端"支持报告"卡片可展示原因
            review_with_error = (
                f"评审失败\n\n"
                f"错误原因: {err_msg}\n\n"
                f"建议:\n"
                f"  - 检查评审模型 (默认: qwen2.5:3b) 是否可用\n"
                f"  - 返回方案矩阵节点, 减少方案数量或调整参数范围\n"
                f"  - 检查网络/服务连接后重试"
            )
            return {"errors": [err_msg],
                    "review": review_with_error,
                    "top_solutions": state["results"][:10]}

        # v4.06: 在支持报告中追加案例电机对标分析
        if case_review_section:
            result["review"] = result["review"] + "\n" + case_review_section
            self._push_progress("review_solutions",
                                f"案例电机对标完成: 最高匹配度 {max(mr['match_score'] for mr in case_match_results) if case_match_results else 0}%",
                                "success")

        # v2.05.02: 记录评审步骤运营指标
        top_count = len(result.get("top_solutions", []))
        has_physics = bool(result.get("physics_analysis"))
        has_case = bool(case_motor)
        _quality = 90 if (has_physics and has_case) else (85 if has_physics else 70)
        record_step_metric(project_id, "review_solutions", "ReviewAgent",
                           duration_ms=_duration_ms,
                           cost_cents=50 if use_cloud else 0,
                           cost_mode="cloud" if use_cloud else "local",
                           quality_score=_quality,
                           quality_issues=f"top={top_count}, physics={'Y' if has_physics else 'N'}, case={'Y' if has_case else 'N'}")

        # v2.05.03: 记录AI评审成本
        self._get_cost_tracker().project_id = project_id
        model_type = "cloud" if use_cloud else "local"
        # v4.06-P1: 优先用 ReviewAgent 真实 token 计量, 无则回退启发式
        _rv_ti = result.get("tokens_in", 0) or 0
        _rv_to = result.get("tokens_out", 0) or 0
        tokens_in = _rv_ti or (_estimate_tokens(state.get("results", [])) + _estimate_tokens(state.get("specs", {})))
        tokens_out = _rv_to or (_estimate_tokens(result.get("review", "")) + _estimate_tokens(result.get("recommendation", "")))
        self._get_cost_tracker().record_ai_step("review_solutions", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type=model_type,
                                         duration_ms=_duration_ms,
                                         detail={"top_count": top_count,
                                                 "has_physics": has_physics,
                                                 "has_case_motor": has_case})

        # v4.01.037 (P0-1): 从评审输出解析结构化 review_feedback
        # 供 ① 决策门质量门禁读取置信度 ② 局部重做时按 feedback_list 逐项回灌
        _review_text = result["review"]
        _rev_fb = {}
        if _RF_AVAILABLE:
            try:
                _pr = _rf_parse(_review_text)
                if _pr.get("ok"):
                    _rev_fb = _pr["feedback"]
                    self._push_progress(
                        "review_solutions",
                        f"结构化评审反馈解析成功[{_pr['level']}]: "
                        f"结论={_rev_fb.get('overall')}, "
                        f"置信度={_rev_fb.get('confidence', 0):.2f}, "
                        f"修正建议={len(_rev_fb.get('feedback_list', []))}项", "info")
                    # 展示用正文剥离 JSON 块 (结构化数据已单独保存)
                    _review_text = self._strip_feedback_json(_review_text)
                else:
                    self._push_progress(
                        "review_solutions",
                        f"评审未输出结构化JSON({str(_pr.get('error', ''))[:50]}), "
                        f"置信度将由质量门禁启发式估算", "warning")
            except Exception as _e:
                self._push_progress("review_solutions",
                                    f"结构化反馈解析异常: {_e}", "warning")

        self._push_progress("review_solutions",
                            f"支持报告已生成 (推荐 {top_count} 个方案)",
                            "success")
        return {"review": _review_text,
                "top_solutions": result["top_solutions"],
                "recommendation": result["recommendation"],
                "review_feedback": _rev_fb}

    @staticmethod
    def _strip_feedback_json(text: str) -> str:
        """v4.01.037: 从评审正文中剥离 ```json {feedback_list...} ``` 结构化块"""
        if not text:
            return text
        import re as _re
        cleaned = _re.sub(r"```(?:json|JSON)?\s*\{[^`]*?feedback_list[^`]*?\}\s*```",
                          "", text, flags=_re.S)
        return cleaned.strip() or text

    # ==================== 收敛轨 ====================
    def _node_convergence_review(self, state: WorkflowState) -> dict:
        """收敛轨: 4个收敛agent → 反对报告"""
        results = state.get("results", [])
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")
        findings = {}
        _conv_start = time.time()
        _conv_errors = []  # v4.01.039 (P1-5 #485): 错误聚合, 记录各剑客失败

        # v4.01.039 (P1-5 #485): 收敛轨真并行
        # 三个独立剑客(reverse/reality/sandbox)并发执行, 墙钟由 sum 降为 max;
        # 魔鬼代言人(devils)依赖前三者发现, 保持其后串行; 案例电机校验保持串行。
        # 注意 safe_run 内部已吞异常并返回 {"error":...}, 故需检测返回 dict 的 error 键判定失败。

        def _run_conv_agent(agent, key, label):
            _ts = time.time()
            try:
                _out = agent.safe_run(results=results, specs=specs)
                # safe_run 吞异常 -> 转失败
                if isinstance(_out, dict) and _out.get("error"):
                    return key, label, False, None, Exception(_out.get("error")), \
                        int((time.time() - _ts) * 1000)
                return key, label, True, _out, None, int((time.time() - _ts) * 1000)
            except Exception as _e:
                return key, label, False, None, _e, int((time.time() - _ts) * 1000)

        _indep_agents = [
            (self.reverse_reviewer, "reverse", "逆向评审师"),
            (self.reality_checker, "reality", "现实检验师"),
            (self.sandbox_simulator, "sandbox", "沙盘推演师"),
        ]
        self._push_progress("convergence_review",
                            f"收敛轨真并行启动: {len(_indep_agents)} 个独立剑客并发执行 "
                            f"(逆向/现实/沙盘)...")
        _exec_start = time.time()
        _conv_tokens_in = 0
        _conv_tokens_out = 0
        _ex = _cf.ThreadPoolExecutor(max_workers=3)
        try:
            _futs = {_ex.submit(_run_conv_agent, a, k, lb): (k, lb)
                     for a, k, lb in _indep_agents}
            for _fut in _cf.as_completed(_futs):
                _k, _lb = _futs[_fut]
                try:
                    _key, _label, _ok, _out, _err, _dur = _fut.result(timeout=300)
                except Exception as _to:  # 硬超时/异常保护, 单个剑客不阻塞整体
                    _key, _label, _ok, _out, _err, _dur = (
                        _k, _lb, False, None, _to,
                        int((time.time() - _exec_start) * 1000))
                if _ok and _out is not None:
                    if _key == "reverse":
                        findings["reverse"] = _out.get("con_report", "")
                        findings["reverse_highlights"] = _out.get("discarded_highlights", [])
                    elif _key == "reality":
                        findings["reality"] = _out.get("con_report", "")
                        findings["feasibility"] = _out.get("feasibility", [])
                    elif _key == "sandbox":
                        findings["sandbox"] = _out.get("con_report", "")
                        findings["simulations"] = _out.get("simulations", [])
                    # v4.06: 累加各收敛 agent 真实 token 计量 (ChatOllama usage)
                    _conv_tokens_in += int(_out.get("tokens_in", 0) or 0)
                    _conv_tokens_out += int(_out.get("tokens_out", 0) or 0)
                    self._push_progress("convergence_review", f"{_label} 完成 (并行)")
                else:
                    _msg = f"{_label}失败: {_err}"
                    findings[_key] = _msg
                    _conv_errors.append(_msg)
                    self._push_progress("convergence_review", _msg, "error")
                record_step_metric(project_id, "convergence_review", _label,
                                   duration_ms=_dur, quality_score=80 if _ok else 0,
                                   quality_issues=(str(_err)[:200] if _err else ""))
        finally:
            # v4.06 修复: 并行剑客超时后不等待挂死线程, 避免收敛节点永久阻塞
            _ex.shutdown(wait=False, cancel_futures=True)
        self._push_progress("convergence_review",
            f"三剑客并行完成, 耗时 {int((time.time() - _exec_start) * 1000)}ms"
            + (f", 失败 {len(_conv_errors)} 个已聚合" if _conv_errors else ""),
            "warn" if _conv_errors else "info")

        # 4. 魔鬼代言人 (引用前三个的发现, 保持串行依赖)
        self._push_progress("convergence_review", "[4/4] 魔鬼代言人启动: 撰写方案遗书 (假设已失败)...")
        _sub_ts = time.time()
        try:
            dev = None
            _dev_exec = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                _dev_fut = _dev_exec.submit(
                    self.devils_advocate.safe_run,
                    results=results, specs=specs,
                    convergence_findings={
                        "reverse": findings.get("reverse", "")[:200],
                        "reality": findings.get("reality", "")[:200],
                        "sandbox": findings.get("sandbox", "")[:200],
                    })
                dev = _dev_fut.result(timeout=300)
            finally:
                # v4.06 修复: 不等待挂死线程, 避免收敛节点永久阻塞 (死锁恢复)
                _dev_exec.shutdown(wait=False, cancel_futures=True)
            if dev is None:
                raise RuntimeError("魔鬼代言人返回空结果")
            findings["devils_advocate"] = dev.get("con_report", "")
            findings["postmortems"] = dev.get("postmortems", [])
            # v4.06: 累加魔鬼代言人真实 token 计量
            _conv_tokens_in += int(dev.get("tokens_in", 0) or 0)
            _conv_tokens_out += int(dev.get("tokens_out", 0) or 0)
            self._push_progress("convergence_review", "[4/4] 魔鬼代言人完成")
            # v2.05.02: 记录魔鬼代言人指标 (用于报告结构进化触发)
            _devils_quality = 75
            if dev.get("postmortems"):
                _devils_quality = 80
            record_step_metric(project_id, "convergence_review", "DevilsAdvocate",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=_devils_quality)
        except Exception as e:
            findings["devils_advocate"] = f"魔鬼代言人失败: {e}"
            _conv_errors.append(f"魔鬼代言人失败: {e}")
            self._push_progress("convergence_review", f"[4/4] 魔鬼代言人失败: {e}", "error")
            record_step_metric(project_id, "convergence_review", "DevilsAdvocate",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=0, quality_issues=str(e)[:200])

        # v4.01.039 (P1-5 #485): 错误聚合结果附入 findings, 供质量门禁/报告可见
        findings["_errors"] = _conv_errors

        # 汇总反对报告
        self._push_progress("convergence_review", "正在汇总四剑客发现为反对报告...")

        # v4.06: 案例电机校验 (第5个收敛视角) — 套 300s 超时护盾, 防止 DB/计算阻塞收敛节点
        # v4.06-P3: 校验结果改为结构化对象 {verdict, error, detail} 替代纯字符串, UI 友好解析
        #   verdict: "ok"(全部>=50%一致) / "warn"(存在<50%偏差) / "empty"(无对标数据) / "error"(超时/异常)
        #   error:   异常信息 (verdict=error 时非空)
        #   detail:  {match_results: [...], section: "<报告文本>"}
        case_motor = self._load_case_motor_for_review(state)
        case_conv_section = ""
        if case_motor:
            self._push_progress("convergence_review",
                                f"[5/5] 案例电机校验: 参考 {case_motor.get('case_code', '')}...")
            _cm_exec = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                def _run_case_motor_check():
                    _match = self._compute_case_motor_match(results, case_motor)
                    _section = self._build_case_motor_convergence_section(_match, case_motor)
                    _verdict = self._case_motor_verdict(_match)
                    return {
                        "verdict": _verdict,
                        "error": None,
                        "detail": {"match_results": _match, "section": _section},
                    }
                _cm_fut = _cm_exec.submit(_run_case_motor_check)
                case_conv_section = _cm_fut.result(timeout=300)
                findings["case_motor_check"] = case_conv_section
                self._push_progress("convergence_review", "[5/5] 案例电机校验完成")
            except Exception as _cm_e:
                # 超时/异常不阻塞收敛节点, 降级为结构化错误对象 (反对报告其余部分仍可用)
                _cm_err = f"案例电机校验超时/失败: {_cm_e}"
                case_conv_section = {"verdict": "error", "error": _cm_err, "detail": None}
                findings["case_motor_check"] = case_conv_section
                self._push_progress("convergence_review", f"[5/5] {_cm_err}", "error")
            finally:
                _cm_exec.shutdown(wait=False, cancel_futures=True)

        con_report = self._assemble_con_report(findings)

        # v4.06: 追加案例电机校验到反对报告 (兼容结构化对象与旧版纯字符串)
        _case_section_text = ""
        if isinstance(case_conv_section, dict):
            _case_section_text = (case_conv_section.get("detail") or {}).get("section") or ""
        elif case_conv_section:
            _case_section_text = str(case_conv_section)
        if _case_section_text:
            con_report = con_report + "\n" + _case_section_text

        _conv_duration = int((time.time() - _conv_start) * 1000)
        self._push_progress("convergence_review", "反对报告生成完成", "success")

        # v2.05.03: 记录收敛阵营AI成本 (统一记为本地, 因四剑客默认本地运行)
        self._get_cost_tracker().project_id = project_id
        # v4.06: 收敛阵营 AI 成本 — 优先用各 agent 真实 token 计量 (ChatOllama usage),
        #        无 usage (全部模板降级/无 LLM) 时回退启发式粗估
        if _conv_tokens_in > 0 or _conv_tokens_out > 0:
            tokens_in = _conv_tokens_in
            tokens_out = _conv_tokens_out
        else:
            tokens_in = _estimate_tokens(results) + _estimate_tokens(specs)
            tokens_out = _estimate_tokens(con_report)
        self._get_cost_tracker().record_ai_step("convergence_review", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type="local",
                                         duration_ms=_conv_duration,
                                         detail={"agents": ["reverse", "reality", "sandbox", "devils",
                                                            "case_motor" if case_motor else None]})

        return {"con_report": con_report,
                "convergence_findings": findings}

    def _assemble_con_report(self, findings: dict) -> str:
        """把4个收敛agent的发现汇总成一份反对报告"""
        sections = [
            ("逆向评审师 · 防止设计独断", findings.get("reverse", "")),
            ("现实检验师 · 防止好高骛远", findings.get("reality", "")),
            ("沙盘推演师 · 防止信念固执", findings.get("sandbox", "")),
            ("魔鬼代言人 · 防止一厢情愿", findings.get("devils_advocate", "")),
        ]
        parts = ["# 收敛系统 · 反对报告\n"]
        for title, body in sections:
            parts.append(f"\n## {title}\n")
            parts.append(body if body else "(无输出)")
        parts.append("\n---\n*本报告由收敛阵营自动生成, 与放大系统的支持报告对照阅读。*")
        return "\n".join(parts)

    # ==================== 决策门质量门禁 (P0-2) ====================
    def _apply_improvement_instructions(self, params_list, instrs, comps, specs,
                                        instrs_by_rail=None, comps_by_rail=None):
        """P1 (v4.02.67): 把结构化改进/折中指令转为候选的采样偏移, 并重算槽形几何。
        ★ 方案② (2026-10-10): instrs_by_rail={source_path:[instr]} 时, 候选按自身
        source_path 只吃本轨指令集 (评估对象=执行对象同轨, 修复杠杆对象错位);
        无 source_path 或该轨未覆盖 ⇒ 回退广播列表 (旧语义, 零回归)。
        指令→参数映射 (相对偏移 delta):
          increase_slot_area  → slot_fill_target += delta(负) → slot_area_req↑ → 槽更大 → 实算 SF↓
          reduce_turns        → turns_per_coil   *= (1+delta)
          lengthen_core / reduce_core_length → core_length *= (1+delta)
          increase_turns      → turns_per_coil   ↑ (3D 门槽满率偏低)
          reduce_core_length  → core_length      ↓ (3D 门槽满率偏低, delta<0)
          reduce_current_density → current_density *= (1+delta)
          increase_outer_dia  → stator_od        *= (1+delta)  (折中: 吸收槽面积增量)
          increase_wire_gauge → current_density  *= (1-|delta|) (等效保持 J 不升)
                                指令口径 (项3, 2026-09-30 统一): param=wire_diameter,
                                delta 取正 —— 表示线径增幅; 执行器按 act 分派不读 param
          shrink_slot_keep_yoke → stator_od 减 2·|delta|·Hs 且锁 yoke_thickness
                                  (hj 不变 ⇒ By 不变) → 槽面积↓ → 实算 SF↑
                                  【2026-09-20 用户指令: 3D 门槽满率偏低 → 回 2D 起点】
          set_slot_depth       → 槽深 hsl *= (1+delta)，外径派生 Dsi+2(hsl+yk+clr)，
                                  锁 hj 与 yoke 系数 (hj 不变 ⇒ By 不变) → SF↑
                                  【★ P0-3 2026-09-22: 由 SF 缺口反解 hsl_new =
                                    hsl·SF_cur/SF_tgt，取代 shrink 的固定 40% 收缩】
        返回实际被改写的候选数。
        """
        if not params_list or not _apply_em_geom or \
           not (instrs or comps or instrs_by_rail or comps_by_rail):
            return 0
        n = 0
        # 2026-09-20: **应用顺序** —— shrink_slot_keep_yoke 必须**最后**执行。
        #   它把 hj 锁死、并据当前 (Dsi,R) 反解 Dso; 若先执行, 后续的
        #   decrease/increase_stator_id / increase_outer_dia 会再缩放 Dso
        #   ⇒ hj 随之漂移, "保轭宽/By 不变" 的不变量被破坏。
        #   放到最后 ⇒ 无论前面几何动作怎么叠, 终态都满足 ΔDso=2ΔHs 且 hj 不变。
        _LAST_ACTIONS = ("shrink_slot_keep_yoke", "set_slot_depth")
        # ★ 杠杆去重 (2026-10-10 用户授权修复, RUN67 取证):
        #   主指令 (instrs) 与折中指令 (comps) 是**两条独立通道** —— 门禁侧 _add 的
        #   去重 (quality_gate.py:1915) 只覆盖主指令, 折中指令可再带同一个 action,
        #   于是同一杠杆在一轮内被**执行器施加两次**。
        #   实测 (RUN67 第 3 轮): increase_wire_gauge(+0.25) 与折中
        #     increase_wire_gauge(+0.03) 同时下发 ⇒ 电密连降两次 (J×0.75×0.75=0.5625),
        #     突破 P1-2 设定的单轮 25% 上限 (WIRE_GAUGE_STEP_MAX), 与「钳位防单轮把 J
        #     压出 AJ 带」的设计意图相悖; 同理 set_slot_depth 若重复下发会叠加收缩槽深。
        #   规则: 同一 action 一轮只执行一次, 取 |delta| 最大者 (更激进者胜出 ——
        #     与门禁 _add 的"取更激进 delta 绝对值"口径一致)。
        # ★ 方案②: 去重+排序逻辑提取为内部函数, 供「广播主路径」与「按轨指令集」
        #   共用 (同一口径: 同 action 取 |delta| 最大者, _LAST_ACTIONS 最后执行)。
        def _merge_ordered(_ins, _cps):
            _merged_all = list(_ins or []) + list(_cps or [])
            _dedup = {}
            for _it in _merged_all:
                if not isinstance(_it, dict):
                    continue
                _a = _it.get("action")
                if not _a:
                    continue
                try:
                    _mag = abs(float(_it.get("delta") or 0))
                except (TypeError, ValueError):
                    _mag = 0.0
                _prev = _dedup.get(_a)
                if _prev is None or _mag > _prev[0]:
                    _dedup[_a] = (_mag, _it)
            return (sorted([_v[1] for _v in _dedup.values()],
                           key=lambda _x: 1 if _x.get("action") in _LAST_ACTIONS else 0),
                    len(_merged_all) - len(_dedup))
        _ordered, _n_deduped = _merge_ordered(instrs, comps)
        if _n_deduped > 0:
            self._push_progress(
                "generate_designs",
                f"[杠杆去重] 主指令与折中指令重复 {_n_deduped} 条, 同 action 取更激进者, "
                f"避免一轮内同一杠杆被施加两次 (电密/槽深连降)", "info")
        # ═══ 方向② (2026-09-30 用户授权): 指令按轨过滤 —— 固定键指令不下发 ═══
        #   指令的主写几何键落在该候选 fixed_keys 内 ⇒ 对该候选跳过。规则优先:
        #   a/b1/b2 的定子外径等规则级键在一轮内也不得被指令临时改动 (旧实现全轨
        #   统一注入 ⇒ 带被改 Dso 的候选进求解器, 下轮靠种子重置回落 —— 纯扰动)。
        #   b3 (fixed_keys=()) 与兜底候选 (无 _physics) 集合为空 ⇒ 全放行, 零回归。
        #   仅列会写入 fixed_keys 空间的动作; 绕组/叠长/槽满率目标类不在表内 ⇒ 恒放行。
        #   注: *_stator_id 执行器按等比连带缩放 Dso (H17), 故主写键含 stator_od;
        #   deepen_yoke/shrink_yoke 的真旋钮是轭厚系数 (驱动 yoke_thickness), 同列。
        _ACT_GEOM_TARGETS = {
            "increase_outer_dia": frozenset({"stator_od"}),
            "decrease_stator_od": frozenset({"stator_od"}),
            "increase_stator_id": frozenset({"stator_id", "stator_od"}),
            "decrease_stator_id": frozenset({"stator_id", "stator_od"}),
            "adjust_stator_id_length": frozenset({"stator_id", "stator_od"}),
            "deepen_yoke": frozenset({"yoke_thickness"}),
            "shrink_yoke": frozenset({"yoke_thickness"}),
            "narrow_tooth": frozenset({"tooth_width"}),
            "widen_tooth": frozenset({"tooth_width"}),
            "shrink_slot_keep_yoke": frozenset({"stator_od", "yoke_thickness"}),
            "set_slot_depth": frozenset({"stator_od", "yoke_thickness"}),
        }
        _n_rule_filtered = 0
        for d in params_list:
            if not isinstance(d, dict):
                continue
            # ═══ 方案② (2026-10-10): 按候选轨选指令集 —— 评估对象=执行对象同轨 ═══
            #   候选带 source_path 且该轨有分轨措施 ⇒ 只吃本轨 (去重+排序同口径);
            #   否则回退广播 _ordered (旧语义: 兜底候选/未覆盖轨零回归)。
            _rail_sp = str(d.get("source_path") or "")
            if instrs_by_rail and _rail_sp in instrs_by_rail:
                _cand_ordered, _ = _merge_ordered(
                    instrs_by_rail.get(_rail_sp),
                    (comps_by_rail or {}).get(_rail_sp))
            else:
                _cand_ordered = _ordered
            _cand_fk = frozenset(
                ((d.get("_physics") or {}).get("fixed_keys")) or [])
            _rule_filtered = []
            changed = False
            for it in _cand_ordered:
                act = it.get("action")
                # 方向②: 该指令主写键与本轨固定键相交 ⇒ 规则禁止, 跳过
                if _cand_fk:
                    _tgt = _ACT_GEOM_TARGETS.get(act)
                    if _tgt and (_cand_fk & _tgt):
                        _rule_filtered.append(
                            {"action": act, "fixed_keys": sorted(_cand_fk & _tgt)})
                        continue
                try:
                    delta = float(it.get("delta") or 0)
                except (TypeError, ValueError):
                    delta = 0.0
                if act == "increase_slot_area":
                    base = float(d.get("slot_fill_target") or 0.50)
                    d["slot_fill_target"] = round(max(0.35, min(0.65, base + delta)), 3)
                    d["_slot_fill_locked"] = True
                    # ★ 强制重算槽形: _apply_em_load_geometry 在「候选已带完整槽形」时
                    #   **直接透传不重算** (motor_tools:1211 `_has_complete_slot_geometry`)
                    #   ⇒ 只改 slot_fill_target 不会改变齿宽。必须清掉槽形尺寸,
                    #   使其退回重算分支, 否则 narrow_tooth 是空指令 (RUN24 实证)。
                    _invalidate_slot_geometry(d)
                    changed = True
                elif act == "raise_air_gap_flux":
                    # 2026-09-20 (磁密修复): Bδ 是 Bt/By 的共同上游 (Bt∝Bδ/bt, By∝Bδ/hj),
                    #   Bδ ∝ 1/N ⇒ 提 Bδ 目标 = 减匝。双写保证两条路径都生效:
                    #   ① air_gap_flux_density 目标 —— 供 **重新生成** 路径
                    #      (b3 生成 L4528 `_bg_cand = d['air_gap_flux_density']` →
                    #       induction_pick_winding(b_g=...) 反算 n_series ∝ 1/b_g);
                    #   ⚠ 二者物理等价 (都是减匝), **不可叠加**: 初版在 raise 里同步减匝,
                    #      实测一轮内 7 → 6 → 4 (Bδ ×1.75 ⇒ Bt 1.94 越过 1.8 上限) ⇒ 跨带震荡。
                    #      故此处只改目标, 实际减匝交给配对的 reduce_turns 指令 (一轮 -10%)。
                    # ★ P1-1 (2026-09-22 用户指令): 相对步长 → **绝对步长** (单位 T)。
                    #   旧式 `_bd *= (1 + |delta|)`, 而门禁给的 delta ∈ [0.10, 0.30]
                    #   ⇒ Bδ≈0.64T 时等效 **+0.064 ~ +0.19T**; 但 Bδ 健康带 [0.55,0.85]
                    #   总宽仅 0.30T, 由匝数整数量化 (ΔBδ/Bδ≈1/Z≈12%) 决定的可行域
                    #   更是只有 ~0.019T ⇒ **一步即跨出整条带** (RUN40 实测
                    #   0.636 → 0.842T, 单轮 +0.206T, 随后下一轮反向再跨回)。
                    #   新规则: step = clamp(|缺口|, 0.01, 0.02) T。
                    #     · 优先读门禁下发的**绝对量** `delta_t` (单位 T, 由 Bδ 缺口反解);
                    #     · 缺该字段 (旧调用/降级) 时按"等效相对量换算"并同样封顶 0.02T,
                    #       保证任何入口都拿不到跨带的大步长。
                    #   诊断字段 `_bg_step_abs_t` 记录实际落下的绝对步长, 供审计。
                    _bd = float(d.get("air_gap_flux_density") or 0.0)
                    if _bd > 0:
                        _dt = it.get("delta_t")
                        try:
                            _dt = float(_dt) if _dt is not None else None
                        except (TypeError, ValueError):
                            _dt = None
                        if _dt is None:                 # 回退: 相对 → 等效绝对
                            _dt = _bd * float(delta)
                        if abs(_dt) > 0:
                            _sgn = 1.0 if _dt >= 0 else -1.0
                            _dt = _sgn * max(_BG_STEP_MIN_T,
                                             min(_BG_STEP_MAX_T, abs(_dt)))
                        else:
                            _dt = 0.0
                        _bd_new = round(max(0.30, min(1.20, _bd + _dt)), 4)
                        if abs(_bd_new - _bd) >= 1e-6:
                            d["air_gap_flux_density"] = _bd_new
                            d["_bg_step_abs_t"] = round(_dt, 4)
                            changed = True
                elif act == "reduce_turns":
                    t = int(d.get("turns_per_coil") or 0)
                    if t > 2:
                        d["turns_per_coil"] = max(2, int(round(t * (1 + delta))))
                        changed = True
                elif act in ("lengthen_core", "reduce_core_length"):
                    # 2026-09-21 用户指令: reduce_core_length = 减小铁心长 (delta 为负),
                    #   与 lengthen_core 共用同一旋钮 (core_length 乘 (1+delta))。铁心长属
                    #   装配级旋钮 (不碰冲片槽形), 故可出现在 3D 门(冲片锁定层)。
                    cl = float(d.get("core_length") or 0)
                    if cl > 0:
                        d["core_length"] = round(cl * (1 + delta), 1)
                        changed = True
                elif act == "reduce_current_density":
                    j = float(d.get("current_density") or 0)
                    if j > 0:
                        d["current_density"] = round(j * (1 + delta), 2)
                        changed = True
                elif act == "increase_outer_dia":
                    od = float(d.get("stator_od") or 0)
                    if od > 0:
                        d["stator_od"] = round(od * (1 + delta), 1)
                        # ★★ 项1 (2026-09-30 用户授权): 外径加大后**必须重推轭厚**。
                        #   旧实现只改 od ⇒ 径向空间 R 变大而 yoke_thickness 不动 ⇒
                        #   多出的径向空间被槽深吃掉 (avail_d=(Dso−Dsi)/2−yt−clearance),
                        #   hj 不变 ⇒ By 不变 ⇒ 本动作空转。同步后 hj ∝ R 才真正放大。
                        _sync_yoke_thickness(d)
                        _invalidate_slot_geometry(d)
                        changed = True
                elif act == "decrease_stator_od":
                    # ★ 点⑤ (2026-09-27 用户指令): 归一化轭磁密偏低 → 减小定子外径。
                    #   hj = (Dso−Dsi)/2·coef ⇒ Dso↓ ⇒ 轭部变薄 ⇒ By↑ (By∝Bδ/hj)。
                    #   ⚠ 覆写型旋钮陷阱 (见 shrink_slot_keep_yoke 注释): coef 不变时
                    #   hj 随 Dso 同比例缩 —— 恰为本动作所需方向。受 Dso/Dsi 比约束,
                    #   门禁侧只发小步长; 越界由下一轮 Dso/Dsi 判据兜底。
                    od = float(d.get("stator_od") or 0)
                    if od > 0:
                        d["stator_od"] = round(od * (1 + delta), 1)
                        changed = True
                elif act == "increase_wire_gauge":
                    # ★ P1-2 (2026-09-22 用户指令): 步长**自适应** —— 由 SF/J 缺口反解,
                    #   钳 [WIRE_GAUGE_STEP_MIN, WIRE_GAUGE_STEP_MAX] = [5%, 25%]。
                    #   旧实现直接吃指令给的 |delta| (门禁侧硬编码 3%), 而 RUN40 实测需
                    #   12~20% ⇒ **杠杆对、力度不足** (3% 步长要 ~19 轮才补上 57% 的缺口)。
                    #   反解: SF ∝ 1/J ⇒ J_new = J·(SF_cur/SF_tgt) ⇒ 相对降幅 = 1 − SF_cur/SF_tgt。
                    #   缺口来源优先级: 指令自带 `sf_cur`/`sf_tgt` > 指令 `delta`。
                    #   守卫: 指令若带 `j_min` (AJ/温升可行带下限) 则夹住, 防压穿。
                    j = float(d.get("current_density") or 0)
                    if j > 0:
                        _frac = abs(float(delta))
                        try:
                            _sc = float(it.get("sf_cur") or 0.0)
                            _st = float(it.get("sf_tgt") or 0.0)
                            if _sc > 0 and _st > 0:
                                _frac = max(_frac, 1.0 - _sc / _st)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                        _frac = max(_WG_STEP_MIN, min(_WG_STEP_MAX, _frac))
                        _j_new = j * (1.0 - _frac)
                        try:
                            _j_lo = float(it.get("j_min") or 0.0)
                        except (TypeError, ValueError):
                            _j_lo = 0.0
                        if _j_lo > 0:
                            _j_new = max(_j_new, _j_lo)
                        if abs(_j_new - j) >= 1e-9:
                            d["current_density"] = round(_j_new, 2)
                            d["_wire_gauge_step"] = round(_frac, 4)
                            changed = True
                # ── 2026-09-21 用户指令①: 3D 门(冲片锁定层) 槽满率偏低专用绕组杠杆 ──
                #   increase_turns        : 增加导线匝数      (turns_per_coil ↑)
                #   increase_wire_section : 增加并绕根数      (并绕根数 ↑ ⇒ 线规×根数 = 截面积↑)
                #   ★ 二者均 **不触碰槽形几何** (槽宽/齿宽/槽深/外径) —— 与"冲片锁定层
                #     只调绕组"的语义自洽; 槽形改动统一由 2D 门 / 1D 门按分层预算下发。
                elif act == "increase_turns":
                    t = int(d.get("turns_per_coil") or 0)
                    if t > 0:
                        # 单轮封顶 +50% (防 Bδ ∝ 1/N 暴跌越出气隙磁密带 [0.55,0.85])
                        _t_new = min(max(t + 1, int(round(t * (1 + abs(delta))))),
                                     int(round(t * 1.5)) + 1)
                        if _t_new != t:
                            d["turns_per_coil"] = _t_new
                            changed = True
                elif act == "increase_wire_section":
                    try:
                        _cur = int(d.get("Winding__NumberOfStrands")
                                   or d.get("_n_strands_auto") or 1)
                    except (TypeError, ValueError):
                        _cur = 1
                    try:
                        _step = max(1, int(round(abs(delta) or 1)))
                    except (TypeError, ValueError):
                        _step = 1
                    _tgt = min(_cur + _step, 20)     # 与 wire_resize 同上限 20
                    if _tgt != _cur:
                        d["_wire_strand_override"] = _tgt
                        d["Winding__NumberOfStrands"] = _tgt
                        changed = True
                # ── B (2026-09-18): 扩展重生动作集 (emf/安匝修正) ──
                #   boost_excitation: 减匝提磁密/安匝 (欠激主因直达修正);
                #     并标记 _emf_geo_force, 使下一轮 generate 强制几何正确绕组 (与 D 闭环一致)。
                #   emf_geo_correct: 仅打标, 强制下一轮绕组走几何正确目标 (不增减匝数亦生效)。
                elif act == "boost_excitation":
                    t = int(d.get("turns_per_coil") or 0)
                    if t > 2:
                        d["turns_per_coil"] = max(2, int(round(t * (1 + delta))))
                        d["_emf_geo_force"] = True
                        changed = True
                elif act == "emf_geo_correct":
                    d["_emf_geo_force"] = True
                    changed = True
                # ── S7 (2026-09-18): 线规重生 ──
                #   wire_resize: 置 _wire_strand_override (当前根数×2, 硬上限 20),
                #     下一轮 _apply_em_load_geometry 据此突破默认上限, 回收线径至工艺带。
                elif act == "wire_resize":
                    try:
                        _cur = int(d.get("Winding__NumberOfStrands")
                                   or d.get("_n_strands_auto") or 5)
                    except (TypeError, ValueError):
                        _cur = 5
                    d["_wire_strand_override"] = min(max(_cur * 2, _cur + 2), 20)
                    changed = True
                # ── P7 (v4.02.93): 并联支路数动作 ──
                #   电密高/温升高 → 增并联支路数分流 (降单路电流密度), **不占槽满率** ——
                #   比加粗线规更干净的绕组杠杆 (加粗会推高 SF)。
                #   ⚠ 该旋钮在 main_a 路径会被 induction_pick_winding 重算覆盖
                #     (motor_tools:2637/2878  d["parallel_circuits"] = _pw["a"]),
                #     故必须靠 _parallel_circuits_override 双保险 (motol_tools
                #     _apply_em_load_geometry + kb5_sanitize_parallel_circuits 消费),
                #     不能只写 parallel_circuits (那会是空动作)。
                elif act == "increase_parallel_paths":
                    try:
                        _cur_a = int(d.get("parallel_circuits") or 1)
                    except (TypeError, ValueError):
                        _cur_a = 1
                    try:
                        _step = max(1, int(round(abs(delta) or 1)))
                    except (TypeError, ValueError):
                        _step = 1
                    _tgt = _cur_a + _step
                    if _par_legal is not None:
                        try:
                            _fix = _par_legal(_tgt, d.get("poles"),
                                              slots=d.get("slots_stator"))
                            if _fix:
                                _tgt = int(_fix)
                        except Exception:
                            pass
                    # 取不到更大合法值 (= 已到该极数上限, 如 6 极 a=6) 时不空转
                    if _tgt > _cur_a:
                        d["_parallel_circuits_override"] = _tgt
                        d["parallel_circuits"] = _tgt
                        changed = True
                # ── G3 (v4.02.92): 定子内径(Dsi)动作 ──
                #   decrease/increase 按 (1+delta) 缩放 Dsi, 同步按原比例缩放 Dso;
                #   adjust_stator_id_length 用 _resolve_dsi_for_length 使 λ 回带(G5 回环)。
                #   改完 Dsi 后循环末 _apply_em_geom 重算齿宽/轭厚。
                #   注: main_a 若显式标 _main_a_locked=True 则跳过(其 Dsi 由种子锚定,
                #   应在 G6 种子级已通过磁密门); 非 main_a 候选一律允许移动 Dsi。
                elif act in ("decrease_stator_id", "increase_stator_id",
                             "adjust_stator_id_length"):
                    if d.get("_main_a_locked") is True:
                        continue
                    try:
                        _sid = float(d.get("stator_id") or 0)
                        _sod = float(d.get("stator_od") or 0)
                    except (TypeError, ValueError):
                        _sid = _sod = 0.0
                    if _sid > 0 and _sod > _sid:
                        _ratio = _sod / _sid
                        if (act == "adjust_stator_id_length"
                                and _resolve_dsi_for_length is not None):
                            _new = _resolve_dsi_for_length(
                                d.get("core_length"), d.get("stator_id"))
                        else:
                            _new = _sid * (1.0 + delta)
                        _new = round(max(20.0, min(500.0, _new)), 1)
                        if abs(_new - _sid) >= 1e-6:
                            d["stator_id"] = _new
                            # ── ★ H17 (2026-09-25 用户指令): 径比越界候选改非等比例缩放 ──
                            #   旧行为 d["stator_od"] = _new * _ratio 是**等比例缩放**:
                            #   Dso/Dsi 是该算子的守恒量 ⇒ QM 用 stator_id 动作去修
                            #   「Dso/Dsi 比越界」在数学上恒不可解 (RUN52 实测: 轮5 应用
                            #   -5% 后 ratio 1.4465 纹丝不动, 却付出 P/T 0.570→0.490、
                            #   正侧分层核验 0/10 的代价)。
                            #   修复: 候选**当前**径比越界时 (判定与门禁 _m2d_dso_dsi_ok
                            #   同源真源 id_ratio_by_poles), 改按真源目标重推
                            #   Dso = Dsi_new · (Dso/Dsi)_target, 一轮即可把 1.4465 → 1.742,
                            #   与 J5/H6 同口径; 带内候选保持等比例缩放 (零回归)。
                            _od_new = round(_new * _ratio, 1)
                            if act != "adjust_stator_id_length" and _dso_dsi_ok is not None:
                                try:
                                    _h17_ok, _h17_det = _dso_dsi_ok(
                                        _sid, _sod, d.get("poles"))
                                    _h17_tgt = float(
                                        (_h17_det or {}).get("target") or 0.0)
                                except Exception:
                                    _h17_ok, _h17_tgt = True, 0.0
                                if (not _h17_ok) and _h17_tgt > 0:
                                    _od_new = round(_new * _h17_tgt, 1)
                                    d["_h17_ratio_repair"] = {
                                        "act": act,
                                        "ratio_from": round(_ratio, 4),
                                        "ratio_to": round(_h17_tgt, 4)}
                            d["stator_od"] = _od_new
                            # FIX-A 补漏: Dsi 变更后重派生 rotor_od，防 Dsi<Dro 几何失效
                            # (RUN14 实测 |Dsi-Dro-2g| 0.05→51.8mm，全批 RMxprt 求解失效)
                            if _norm_geom is not None:
                                _ag_fix = _norm_geom(d)
                                if _ag_fix.get("changed"):
                                    d["_geom_ag_normalized"] = _ag_fix
                            changed = True
                # ── P4 (v4.02.93): 2D 几何动作 (齿宽/轭厚系数据覆写) ──
                #   ★ 平行齿生产口径 (motor_tools:1292): bt = π·d_ctrl/Qs − w_eq, 最终齿宽由
                #     槽面积需求 (w_eq ← _bs_ref ← slot_fill_target 链) 决定, 不由齿宽系数决定 ⇒
                #     narrow_tooth/widen_tooth 直接驱动真实旋钮 slot_fill_target (与
                #     increase_slot_area 同源), 同时记录齿宽系数覆写供非平行齿回退路径。
                #     deepen_yoke 记录 _yoke_coef_override (轭厚系数)。仅 L2(2D 起始) 生效。
                elif act == "deepen_yoke":
                    # P0-hj/hs (2026-09-19): 基值与钳制带按**极数分档**对齐
                    #   (旧: 基 0.35 / 钳 [0.28,0.45] ⇒ hj/hs 上限仅 0.818, 拉满也越界;
                    #    2026-09-19 用户口径: 2 极 [2.0,3.5] / 4 极+ [1.0,2.5])。
                    _yb = _yoke_band(d.get("poles") or d.get("pole_pairs") or 4)
                    _yd = _yoke_default(d.get("poles") or d.get("pole_pairs") or 4)
                    try:
                        _cur = float(d.get("_yoke_coef_override") or _yd)
                    except (TypeError, ValueError):
                        _cur = _yd
                    _yco_new = _cur * (1.0 + abs(delta))
                    _yco_new = round(max(_yb[0], min(_yb[1], _yco_new)), 4)
                    d["_yoke_coef_override"] = _yco_new
                    # ★★ 项1 (2026-09-30 用户授权): 让 deepen_yoke **真正生效**。
                    #   旧实现只写 `_yoke_coef_override` —— 定向跑批实测该键在
                    #   A/B1/B2/B3 四轨**全部 0 消费** (唯一读点 motor_tools:4894 位于
                    #   _b3_specs_driven_make_design 内部新建的 d, specs/profile 不并入
                    #   ⇒ 恒读到 0) ⇒ 本动作实为空指令。此处补三重落子:
                    #     ① 按新系数直接重推 yoke_thickness (当轮即生效);
                    #     ② 写 `_yoke_factor_override`, 供 B1/B2 生成侧消费 —— 否则下一轮
                    #        _bypass_make_design 会按 `yoke_coef_default_for × dims
                    #        ['yoke_factor']` 重算, 把本次成果覆盖 (实测 G6 直写 seed = 0%);
                    #     ③ 作废槽形, 否则候选自带槽形会被 _has_complete_slot_geometry
                    #        分支原样透传 (motor_tools:1358 return d)。
                    _sync_yoke_thickness(d, coef=_yco_new)
                    if _yd > 0:
                        d["_yoke_factor_override"] = round(_yco_new / _yd, 4)
                    _invalidate_slot_geometry(d)
                    changed = True
                elif act == "shrink_yoke":
                    # ★ 点⑤ (2026-09-27 用户指令): 归一化轭磁密偏低 → 减小轭宽
                    #   (deepen_yoke 的反向: 轭厚系数↓ ⇒ hj↓ ⇒ By↑, By∝Bδ/hj)。
                    #   幅度由门禁缺口反解 (delta 负向); 钳在按极数分档的系数带内。
                    _yb = _yoke_band(d.get("poles") or d.get("pole_pairs") or 4)
                    _yd = _yoke_default(d.get("poles") or d.get("pole_pairs") or 4)
                    try:
                        _cur = float(d.get("_yoke_coef_override") or _yd)
                    except (TypeError, ValueError):
                        _cur = _yd
                    _yco_new = _cur * (1.0 + delta)      # delta 负 ⇒ 系数↓
                    d["_yoke_coef_override"] = round(
                        max(_yb[0], min(_yb[1], _yco_new)), 4)
                    changed = True
                elif act in ("narrow_tooth", "widen_tooth"):
                    # bt↓(窄齿 ⇒ Bt↑) ⟺ w_eq↑ ⟺ 槽面积需求↑ ⟺ slot_fill_target↓
                    _dlt = -abs(delta) if act == "narrow_tooth" else abs(delta)
                    _sft = float(d.get("slot_fill_target") or 0.50)
                    d["slot_fill_target"] = round(max(0.35, min(0.65, _sft + _dlt)), 3)
                    d["_slot_fill_locked"] = True   # 同上: 防 _apply_em_load_geometry 覆盖
                    _invalidate_slot_geometry(d)    # 同上: 强制重算槽形, 否则指令为空转
                    try:
                        _curf = float(d.get("_tooth_factor_override") or 0.0) or 1.0
                    except (TypeError, ValueError):
                        _curf = 1.0
                    d["_tooth_factor_override"] = round(
                        max(0.80, min(1.25, _curf * (1.0 + delta))), 3)
                    changed = True
                # ── 2026-09-20 (用户指令): 槽满率偏低 → 收小冲片 (回 2D 起点) ──────
                #   动作: 减小定子槽高 Hs, 同步减小定子外径 Dso, 令 ΔDso = 2·ΔHs,
                #     使 hj = (Dso−Dsi)/2 − Hs 与定子轭磁密 By **保持不变**
                #     ⇒ 只有槽面积↓ ⇒ SF↑ (RUN27 实测 SF=0.2775, 槽深吃满径向空间所致)。
                #   ⚠ **覆写型旋钮陷阱** (本仓反复踩): motor_tools._b3_specs_driven_make_design
                #     `yt_base = (stator_od − stator_id)/2 · _yoke_coef` ⇒ 只改 stator_od
                #     会让 hj 按同系数缩水, By ∝ 1/hj 反而**升高**, 与"保持轭宽不变"相反。
                #     故必须三重落子 (缺一即为空转或反向):
                #       ① stator_od            — 真旋钮 (worker 注入 Stator.OuterDiameter)
                #       ② yoke_thickness       — 显式锁 hj (em_load L1244 直接消费该字段)
                #       ③ _yoke_coef_override  — = hj/R_new, 使**重生成路径**亦守恒
                #     再 _invalidate_slot_geometry 强制槽形重算 —— 否则候选自带
                #     Slot__Hs0/1/2 被透传 (motor_tools L1211), 槽深纹丝不动 (同 RUN24 教训)。
                elif act == "shrink_slot_keep_yoke":
                    if d.get("_main_a_locked") is True:
                        continue        # main_a 冲片几何由种子锚定, 不动外径
                    try:
                        _sod = float(d.get("stator_od") or 0.0)
                        _sid = float(d.get("stator_id") or 0.0)
                    except (TypeError, ValueError):
                        _sod = _sid = 0.0
                    if _sod > 0 and _sid > 0 and _sod > _sid:
                        _R = (_sod - _sid) / 2.0
                        try:
                            _yt = float(str(d.get("yoke_thickness"))
                                        .replace("mm", "").replace("'", "").strip())
                        except (TypeError, ValueError):
                            _yt = 0.0
                        if _yt <= 0:
                            _yt = _R * _yoke_default(d.get("poles") or 4)
                        _hs = _R - _yt - 0.5        # = avail_d (SLOT_DEPTH_MODE='fill')
                        if _hs > 1.0:
                            _dR = _hs * delta                 # delta<0 ⇒ ΔR<0 (槽高↓)
                            _dR = max(_dR, -0.40 * _hs)       # 单轮最多收 40% 槽高
                            _R_new = _R + _dR
                            # 保底: 收完仍留 ≥3mm 槽深, 且 hj 不超新半机宽
                            if _R_new > _yt + 3.5:
                                _sod_new = round(_sid + 2.0 * _R_new, 1)
                                d["stator_od"] = _sod_new
                                d["yoke_thickness"] = round(_yt, 3)   # ② 锁 hj
                                d["_yoke_coef_override"] = round(     # ③ 重生成路径守恒
                                    max(0.05, min(0.95, _yt / _R_new)), 6)
                                d["_sf_shrink"] = {
                                    "hs_old": round(_hs, 3),
                                    "hs_new": round(_hs + _dR, 3),
                                    "dR_mm": round(_dR, 3),
                                    "sod_old": round(_sod, 1),
                                    "sod_new": _sod_new,
                                    "yoke_mm": round(_yt, 3),
                                    "yoke_coef_new": d["_yoke_coef_override"],
                                }
                                _invalidate_slot_geometry(d)      # 强制槽形重算
                                changed = True
                # ── ★ P0-3 (2026-09-22 用户指令): set_slot_depth —— 槽深独立可调 ──────
                #   前提: P0-2 已把「槽深 ↔ 轭厚」解绑 (motor_tools._hs2_override +
                #   slot_depth_derived_od) ⇒ 径向获得 2 个自由度, 槽深成为 **SF 的纯杠杆**。
                #   控制律 (由门禁按 SF 缺口反解): delta = SF_cur/SF_tgt − 1
                #     hsl_new = hsl_cur · (1 + delta) = hsl_cur · SF_cur/SF_tgt
                #   为何这式成立: 槽宽由齿宽/可用槽宽锚定, 与槽深无关 ⇒ 槽面积 ∝ hsl
                #     ⇒ SF ∝ 1/hsl ⇒ 反解即上式 (RUN40 实证精度 ~2%)。
                #   与 shrink_slot_keep_yoke 的差别: 后者只按 1−SF_cur/SF_tgt 收缩且被
                #     40% 钳位截断 (RUN40 需 47% ⇒ 白耗一轮); 本式无该钳位 (限 ±60%)。
                #   五重落子 (缺一即空转或反向):
                #     ① _hs2_override        — 真旋钮 (motor_tools 据此反推外径)
                #     ② stator_od            — 立即生效 (派生量 Dsi+2(hsl+yk+clr))
                #     ③ yoke_thickness       — 显式锁 hj ⇒ By 不变
                #     ④ _yoke_coef_override  — = hj/R_new, 使**重生成路径**亦守恒
                #     (否则 _b3_specs_driven_make_design 的 yt=R·c 会用新外径重算 hj)
                #     ⑤ _invalidate_slot_geometry — 强制槽形重算 (否则 Slot__Hs0/1/2 透传)
                elif act == "set_slot_depth":
                    if d.get("_main_a_locked") is True:
                        continue        # main_a 冲片几何由种子锚定, 不动槽深/外径
                    try:
                        _sod = float(d.get("stator_od") or 0.0)
                        _sid = float(d.get("stator_id") or 0.0)
                    except (TypeError, ValueError):
                        _sod = _sid = 0.0
                    if _sod > 0 and _sid > 0 and _sod > _sid:
                        _R = (_sod - _sid) / 2.0
                        try:
                            _yt = float(str(d.get("yoke_thickness"))
                                        .replace("mm", "").replace("'", "").strip())
                        except (TypeError, ValueError):
                            _yt = 0.0
                        if _yt <= 0:
                            _yt = _R * _yoke_default(d.get("poles") or 4)
                        _hsl = _R - _yt - _SLOT_CLR     # = avail_d (SLOT_DEPTH_MODE='fill')
                        if _hsl > 1.0:
                            try:
                                _r_ratio = 1.0 + float(delta)
                            except (TypeError, ValueError):
                                _r_ratio = 1.0
                            _r_ratio = max(0.40, min(1.60, _r_ratio))
                            _hsl_new = max(3.0, _hsl * _r_ratio)
                            _od_new = _slot_depth_od(_sid, _hsl_new, _yt)
                            _R_new = (_od_new - _sid) / 2.0
                            if _R_new > _yt + 2.0:
                                d["_hs2_override"] = round(_hsl_new, 3)
                                d["stator_od"] = round(_od_new, 3)
                                d["yoke_thickness"] = round(_yt, 3)       # ③ 锁 hj
                                d["_yoke_coef_override"] = round(          # ④ 重生成路径守恒
                                    max(0.05, min(0.95, _yt / _R_new)), 6)
                                d["_sf_set_slot_depth"] = {
                                    "hs_old": round(_hsl, 3),
                                    "hs_new": round(_hsl_new, 3),
                                    "ratio_sf": round(_r_ratio, 4),
                                    "sod_old": round(_sod, 1),
                                    "sod_new": round(_od_new, 1),
                                    "yoke_mm": round(_yt, 3),
                                    "yoke_coef_new": d["_yoke_coef_override"],
                                }
                                _invalidate_slot_geometry(d)           # ⑤ 强制槽形重算
                                changed = True
            if _rule_filtered:
                # 方向② 可审计: 记录本候选被规则优先拦下的指令
                d["_rule_filtered_actions"] = _rule_filtered
                _n_rule_filtered += len(_rule_filtered)
            if changed:
                try:
                    _apply_em_geom(d, specs, line_load=d.get("line_load"),
                                   current_density=d.get("current_density"))
                    n += 1
                except Exception:
                    pass
        if _n_rule_filtered:
            self._push_progress(
                "generate_designs",
                f"[规则优先·方向②] {_n_rule_filtered} 条固定键指令未下发到 "
                f"a/b1/b2 候选 (规则级键一轮内也不动; 明细见候选 _rule_filtered_actions)",
                "info")
        return n


    def _node_quality_gate(self, state: WorkflowState) -> dict:
        """v4.01.038 (P0-2): 决策门前自动校验节点

        评估三道关卡 (评审置信度 / 硬约束 / 收敛轨严重冲突)。
        路由由 _after_quality_gate 决定: pass→决策门, regenerate→重生, manual→人工裁决。
        """
        if not _QG_AVAILABLE:
            self._push_progress("quality_gate",
                                "quality_gate 模块不可用, 跳过门禁 (放行)", "warning")
            return {"quality_gate_result": {"passed": True, "summary": "模块不可用, 跳过"},
                    "auto_regenerate": False, "regenerate_guidance": ""}

        cfg = _qg_config()
        # R2 (2026-09-20): 兜底默认与真源对齐 (DEFAULT_QUALITY_GATE["max_redo"])。
        #   2026-09-21 用户指令: 每层反复次数 3→2, 兜底同步为 2。
        max_redo = int(cfg.get("max_redo", 2))
        redo_count = int(state.get("redo_count", 0) or 0)

        # M4 (M5): 按已重生次数渐进收紧门禁阈值 (redo_count<=0 原样返回, 不收紧),
        # 实现「无推荐→重生→收紧阈值→再评审」回路; 深拷贝不入参, 向后兼容 v4.02.20。
        cfg = _qg_tighten(cfg, redo_count)
        result = _qg_eval(state, cfg=cfg)
        # P6: 质量门禁重生前求解器可用性预检 (纯函数, 仅当门禁未通过、可能重生时才有意义)。
        # 探针在节点内执行一次并随 quality_gate_result 落入 state, 供 _after_quality_gate 路由消费。
        _enforce_solver = bool(cfg.get("enforce_solver_available", True))
        if (not result["passed"]) and _enforce_solver:
            try:
                _solver_status = check_solver_status()
            except Exception as _se:
                _solver_status = {}
                self._push_progress("quality_gate",
                                    f"求解器状态探针异常(保守放行): {_se}", "warning")
            _ok, _reason = _qg_solver_ok(_solver_status, state.get("solve_summary") or {})
            result["solver_precheck"] = {
                "available": _ok, "reason": _reason, "enforce": True,
            }
        self._push_progress("quality_gate", _qg_summary(result, redo_count, max_redo),
                            "success" if result["passed"] else "warn")

        # prev_qg_reasons: 上轮失败原因快照, 供下一轮"卡死"检测 (v4.02.66)
        out: dict = {"quality_gate_result": result,
                     "prev_qg_reasons": result.get("reasons", [])}

        # ── P2 (v4.02.67): 改进措施效果评价 (上轮快照存在时, 量化本轮 Δ 与达成度) ──
        _curr_metrics = _qg_round_metrics(state.get("results") or []) if _qg_round_metrics else {}
        _prev_metrics = state.get("prev_round_metrics") or {}
        if _qg_eval_effect and _prev_metrics and _curr_metrics:
            try:
                _eff = _qg_eval_effect(_prev_metrics, _curr_metrics)
                result["measure_effect"] = _eff
                out["measure_effect"] = _eff
                _tbl = " | ".join(
                    f"{r[0]}:{r[1]}→{r[2]}({r[3]})" for r in _eff.get("table", [])[:4])
                self._push_progress("quality_gate",
                                    f"[措施效果评价] {_eff.get('verdict', '')} · {_tbl}", "info")
            except Exception as _ee:
                self._push_progress("quality_gate", f"措施效果评价异常(忽略): {_ee}", "warning")

        if result["passed"]:
            # 通过: 清除重生环路标记 (含卡死检测用的上轮失败原因)
            # R3 (2026-09-20): 路由意图随结果落 state, 供 _after_quality_gate 直接沿用
            result["_route_hint"] = "pass"
            out["auto_regenerate"] = False
            out["regenerate_guidance"] = ""
            out["prev_qg_reasons"] = []
            out["improvement_instructions"] = []
            out["slot_fill_compromises"] = []
            # ★ 方案②: 通过时清空分轨措施 (与广播列表成对清理)
            out["improvement_instructions_by_rail"] = {}
            out["slot_fill_compromises_by_rail"] = {}
            return out

        # 未通过: 判断是否还能重生
        # v4.02.66: 传入上轮失败原因用于"卡死"检测 (避免重生无改善却空转 ~28min/轮)
        path = _qg_decide(result, redo_count, cfg,
                          prev_reasons=state.get("prev_qg_reasons"))
        # R3 (2026-09-20 用户指令): 把「本节点做出的路由决策」随 quality_gate_result 落入
        #   state。根因: _after_quality_gate 条件边在**节点更新之后**执行, 它读到的
        #   redo_count 已是节点自增后的值 (+1); 路由器若再自行调 _qg_decide 就会在临界轮
        #   把 regenerate 误判成 manual ⇒ 末轮改进指令「生成即作废」(RUN29 铁证: 20:14:39
        #   同秒打印「方案自动重生 (第 2/2 次)」与「转人工裁决」, 中间无第 3 轮
        #   generate_designs)。现由节点一次判定、路由器直接沿用 ⇒ 两侧口径恒一致。
        # ══════ 2026-09-21 用户指令: 质量门分层 (3D → 2D → 1D), 每层 2 次, 总 2³=8 ══════
        #   tier 语义 (越往下改动越"结构性"):
        #     "3d" 冲片锁定层 —— 只调绕组 (匝数/线规/并绕根数/并联支路/电密/铁心长),
        #                        ★ **绝不修改槽形尺寸** (槽宽/齿宽/槽深/外径/Dsi)
        #     "2d" 几何层     —— 放开槽形几何 (槽宽/齿宽/槽深/外径/轭厚/目标填充率)
        #     "1d" 基准尺寸层 —— 改定子内圆 Dsi (其余尺寸按比例缩放), 回 2D 流程起始,
        #                        并把 3D/2D 计数清零 ⇒ 形成 3×3×3=27 的嵌套预算
        _per = _QG_TB_2D  # 占位; _plan 解析出 tier 后于下方重设
        _all_instrs = _qg_build_instrs(result, state) if _qg_build_instrs else []
        # 卡死: 本轮失败原因与上轮完全一致 (换层级换动作集才有意义)
        _stuck_now = bool(state.get("prev_qg_reasons")) and \
                     result.get("reasons") == state.get("prev_qg_reasons")
        # ★ 分层预算判定 (纯函数, 便于离线回归) —— 3D→2D→1D, 每层 2 次, 总 2³=8
        _plan = _qg_tier_plan(state, _all_instrs, redo_count, stuck=_stuck_now,
                              fail_stage=result.get("fail_stage"))
        tier = _plan["tier"]
        t3 = _plan["t3"]
        t2 = _plan["t2"]
        t1 = _plan["t1"]
        tot = _plan["total"]
        # ★ 分层独立预算 (2026-10-11): _per 按解析后的 tier 取对应层预算, 供重生日志分母使用
        _per = (_QG_TB_2D if tier == "2d" else (_QG_TB_3D if tier == "3d" else _QG_TB_1D))
        redo_count = _plan["redo"]
        _instrs = _plan["instrs"]
        _route_2d_start = _plan["route_2d_start"]
        _manual = _plan["manual"]
        for _lg_msg, _lg_lv in _plan["logs"]:
            self._push_progress("quality_gate", _lg_msg, _lg_lv)
        # R3 (2026-09-20 用户指令): 路由意图随结果落 state, 供 _after_quality_gate 沿用
        result["_route_hint"] = ("regenerate_2d" if _route_2d_start
                                 else ("manual" if _manual else "regenerate"))
        result["_qg_tier"] = tier
        result["_qg_counts"] = {"tier": tier, "t3": t3, "t2": t2, "t1": t1,
                                "redo": redo_count, "total": tot + 1}
        # ═══ 方案② (2026-10-10 用户授权): 分轨评估与措施 —— 修复「杠杆对象错位」 ═══
        #   RUN63/71/73 取证链: 门禁评估混合榜首(多为 main_a) ⇒ 措施由它生成后广播四轨;
        #   set_slot_depth 主写 {stator_od,yoke_thickness} 对 a/b1/b2 被方向②全拦
        #   ⇒ 杠杆只落 b3 ⇒ a 轨 SF 永不改善, 槽深逐轮对折。
        #   现按「top10 混合排序不变、消费侧按 source_path 分组」: 每轨用本轨 TOP1
        #   单独评估 + 生成「本轨可达」措施 (锁定轨走 P0-6 绕组等价处方, 见
        #   quality_gate._RAIL_GEOM_LOCKED), 评估对象=执行对象同轨, 错位结构性消除。
        #   ⚠ 路由/tier 预算/主 result 仍由混合榜首决定 —— 本改动只修正措施侧, 零路由回归。
        #   ⚠ fail-open: 任何异常回退广播 (旧语义), 不阻断重生链路。
        _instrs_by_rail: dict = {}
        _comps_by_rail: dict = {}
        if _qg_eval is not None and _qg_build_instrs is not None:
            try:
                _rail_groups: dict = {}
                for _c in (state.get("results") or []):
                    if isinstance(_c, dict):
                        _sp = str(_c.get("source_path") or "fallback")
                        _rail_groups.setdefault(_sp, []).append(_c)
                # 多于 1 组才值得分轨; 单轨时与旧广播等价 (省 1 次评估)
                if len(_rail_groups) > 1:
                    for _sp, _cands in _rail_groups.items():
                        if not _cands:
                            continue
                        _sub = dict(state)
                        _sub["results"] = _cands          # 组内保持混合排序原序 ⇒ [0]=本轨 TOP1
                        _rr = _qg_eval(_sub, cfg=cfg)
                        if not isinstance(_rr, dict) or _rr.get("passed"):
                            continue
                        _ri = list(_qg_build_instrs(_rr, state, rail=_sp) or [])
                        # 全局 tier 过滤照旧生效 (tier=="3d" 时 b3 的槽形动作同样
                        # 替换为绕组等价处方, 与 _qg_tier_plan 对主指令的处理一致)
                        _ri = _filter_instrs_by_tier(
                            _tier_substitute_instrs(_ri, tier), tier) or []
                        if not _ri:
                            continue
                        _instrs_by_rail[_sp] = _ri
                        if _qg_build_comps is not None:
                            _rc = list(_qg_build_comps(_ri, _cands[0]) or [])
                            if _rc:
                                _comps_by_rail[_sp] = _rc
                if _instrs_by_rail:
                    self._push_progress(
                        "quality_gate",
                        "[分轨措施·方案②] " + "; ".join(
                            f"{_sp}:{','.join(sorted(set(i.get('action','') for i in _v)))}"
                            for _sp, _v in _instrs_by_rail.items()), "info")
            except Exception as _pre:
                self._push_progress(
                    "quality_gate",
                    f"[分轨措施·方案②] 生成异常, 回退广播(旧语义): {_pre}", "warning")
                _instrs_by_rail = {}
                _comps_by_rail = {}
        # ★ P1-4 (2026-10-11): 锁定轨「杠杆耗尽」识别 —— 跨轮 SF 历史 + 换轨标记
        #   从各轨指令携带的 sf_cur 取代表 SF 追加进 state["rail_sf_history"][rail];
        #   若指令带 _lever_exhausted ⇒ 标 rail_exhausted。⚠ QG_LEVER_EXHAUST_N=0 ⇒ 跳过(零回归)。
        if _qg_exp_int("QG_LEVER_EXHAUST_N", 0):
            try:
                _exn = _qg_exp_int("QG_LEVER_EXHAUST_N", 0)
                _hist = state.setdefault("rail_sf_history", {})
                _ex = {}
                for _sp, _ri in (_instrs_by_rail or {}).items():
                    _sf = None
                    for _it in _ri:
                        if _it.get("sf_cur") is not None:
                            try:
                                _sf = float(_it.get("sf_cur"))
                            except (TypeError, ValueError):
                                _sf = None
                            if _it.get("_lever_exhausted"):
                                _ex[_sp] = True
                            break
                    if _sf is not None:
                        _h = _hist.setdefault(_sp, [])
                        _h.append(_sf)
                        if len(_h) > (_exn + 2):
                            del _h[: -(_exn + 2)]
                state["rail_exhausted"] = _ex
                if _ex:
                    self._push_progress(
                        "quality_gate",
                        f"[P1-4 杠杆耗尽] 锁定轨 {','.join(sorted(_ex.keys()))} SF 连续"
                        f"{_exn}轮空转 ⇒ 转人工/换轨(配额让 b3 真实几何杠杆轨)", "warning")
            except Exception as _pe4:
                self._push_progress(
                    "quality_gate", f"[P1-4 杠杆耗尽] 跟踪异常(忽略): {_pe4}", "warning")
        if not _manual:
            new_redo = redo_count + 1
            guidance = "质量门禁未通过, 请重新生成方案并重点改进:\n" + "\n".join(
                f"  - {r}" for r in result.get("reasons", []))
            # M4/M1: 支持报告检出『无推荐方案』时, 注入可操作指引,
            # 推动重生阶段明确给出唯一推荐方案 (或显式说明约束冲突点)。
            if result.get("no_recommendation"):
                guidance += (
                    "\n\n[M1] 支持报告中检出『无推荐方案』否定表述, 请重新生成方案, "
                    "并在支持报告中明确给出唯一推荐方案 (须注明推荐理由与关键指标); "
                    "若确无可行方案, 请显式说明约束冲突点, 以便转人工裁决。")
            # P1/P2 (v4.02.67): 由失败原因生成结构化改进指令 + 冲突折中指令
            #   ⚠ _instrs 已在上方按**当前门禁层**过滤完毕 (3D 层不含任何槽形动作),
            #     此处若重新调用 _qg_build_instrs 会把槽形动作又放回来 ⇒ 分层失效。
            _sample = (state.get("results") or [{}])[0] if state.get("results") else {}
            _comps = _qg_build_comps(_instrs, _sample) if _qg_build_comps else []
            out.update({
                "redo_count": new_redo,
                "auto_regenerate": True,
                "regenerate_guidance": guidance,
                "improvement_instructions": _instrs,
                "slot_fill_compromises": _comps,
                # ★ 方案②: 分轨措施落 state (执行器 generate_designs 按候选
                #   source_path 只吃本轨指令集; 空 dict = 回退广播旧语义)
                "improvement_instructions_by_rail": _instrs_by_rail,
                "slot_fill_compromises_by_rail": _comps_by_rail,
                "prev_round_metrics": _curr_metrics,   # 快照供下一轮 Δ 对比
                # 2026-09-21 (用户指令): 分层计数随重生落 state (WorkflowState 已声明)
                # P0-1: 新增 qg_tier3_count (新顺序 2D→3D→1D 的 3D 升级计数)
                "qg_tier": tier,
                "qg_tier3_count": t3,
                "qg_tier2_count": t2,
                "qg_tier1_count": t1,
                "total_regen_count": tot + 1,
            })
            _tier_note = (" (基准尺寸层 · **由 3D 层耗尽触发**)" if tier == "1d" else "")
            self._push_progress("quality_gate",
                                f"[{tier.upper()} 门]{_tier_note} 方案自动重生 "
                                f"(本层第 {new_redo}/{_per} 次, 累计 {tot + 1}/{_QG_TIER_TOTAL_CAP}), "
                                f"已注入改进指引: {result['summary']}", "warn")
            if _instrs:
                self._push_progress("quality_gate",
                                    "结构化改进指令: " + "; ".join(
                                        f"{i['action']}({i['param']}{i['delta']:+g})"
                                        for i in _instrs), "info")
            # P8b (2026-09-21 用户指令 三.1): 槽满率统一仲裁结论 —— 让"为何撤销/降幅"
            #   可见 (否则指令表里少了一项, 排障时无从判断是漏发还是被仲裁裁掉)
            _sfa = result.get("_sf_arbitration") or {}
            if _sfa.get("demand") is not None and _sfa.get("verdict") != "no_conflict":
                self._push_progress(
                    "quality_gate",
                    f"[P8b SF统一仲裁] SF={_sfa.get('sf_current')} 带{_sfa.get('band')} "
                    f"需求={'抬' if _sfa.get('demand') == 1 else ('压' if _sfa.get('demand') == -1 else '保持')} "
                    f"| 撤销={_sfa.get('dropped') or '无'} 降幅={_sfa.get('damped') or '无'} "
                    f"| {_sfa.get('verdict')}", "info")
            if _comps:
                self._push_progress("quality_gate",
                                    "冲突折中指令: " + "; ".join(
                                        f"{c['action']}({c['param']}{c['delta']:+g})"
                                        for c in _comps), "info")
        else:
            # ── 2026-09-21 (用户指令): 三层预算 (P0-1: 2D→3D→1D, 2³=8) 全部用尽 ⇒ 转人工 ──
            #   旧 P3 的"一次性 L2 升级"已由分层状态机取代: 升级/清零件在上方
            #   `_exhausted` 分支统一处理, 此处只负责收尾 (不再有第二条重生路径,
            #   避免两条路径口径打架)。
            out.update({
                "redo_count": redo_count,
                "auto_regenerate": False,
                "regenerate_guidance": "",
                "improvement_instructions": [],
                "slot_fill_compromises": [],
                # ★ 方案②: 转人工时清空分轨措施 (fail-closed, 不带病下发)
                "improvement_instructions_by_rail": {},
                "slot_fill_compromises_by_rail": {},
                # 分层计数保留, 供报告/审计回溯"到底跑到了第几层"
                "qg_tier": tier,
                "qg_tier3_count": t3,
                "qg_tier2_count": t2,
                "qg_tier1_count": t1,
                "total_regen_count": tot,
                "l2_escalated": True,      # 兼容旧字段语义: 已升级过几何层
            })
            self._push_progress(
                "quality_gate",
                f"三层门禁预算用尽 (当前层={tier.upper()}, 3D 升级 {t3}/{_QG_TB_3D}, "
                f"1D 触发 {t1}/{_QG_TB_1D}, 累计重生 {tot}/{_QG_TIER_TOTAL_CAP}) ⇒ "
                f"转人工裁决 (决策门将标注风险)", "error")
        return out

    # ==================== 决策门 ====================
    def _write_back_case_memory(self, state: dict) -> None:
        """★ H22 (2026-09-25 用户指令): 案例回写三闸门 —— 仅终选回写 1 条 (fail-closed)。

        背景 (RUN51/RUN52 取证): 原 generate_designs 每轮回写 results[:3],
        单次工作流写 27 条; specs_json.efficiency=0.92 是需求目标非实测;
        质量门 8 轮全灭仍回写 (CM#548 全 0 效率 / CM#549) ⇒ 失败设计固化为
        未来 query_similar 种子真值 (案例库污染, 近两日 89 条同需求重复)。

        三闸门 (全部通过才回写, 任一不满足静默跳过并推送原因):
          ① 质量门通过 ∧ 终选为真实解 (rmxprt/motorcad 且未 solve_failed)
             ∧ η>0 ∧ η≥需求下限 ∧ 功率/转矩达标比 ≥0.95;
          ② specs_json.efficiency 写**实测值** (归一到需求同口径小数),
             并标 efficiency_source="measured" (禁写需求目标);
          ③ 同一 workflow 仅在终选点 (决策门) 回写 1 条 —— 本方法只在
             _node_decision_gate 调用一次, generate_designs 不再回写。
        """
        _why = ""
        try:
            qg = state.get("quality_gate_result") or {}
            if not qg.get("passed", True):
                _why = "质量门未通过 (失败设计不得固化为案例种子)"
            else:
                best = None
                for _r in (state.get("results") or []):
                    if (isinstance(_r, dict)
                            and _r.get("solve_mode") in ("rmxprt", "motorcad")
                            and not _r.get("solve_failed")):
                        best = _r          # results 已按 [真实解优先, 效率降序] 排序
                        break
                if best is None:
                    _why = "终选无真实求解解 (L0 模拟/降级或全部求解失败)"
                else:
                    eff = float(best.get("efficiency") or 0.0)
                    if eff <= 0:
                        _why = "终选效率非正值"
                    else:
                        specs = dict(state.get("specs") or {})
                        # 闸门①: η ≥ 需求下限 (需求 efficiency 存储口径为小数 0.92)
                        _eff_req = 0.0
                        try:
                            _eff_req = float(specs.get("efficiency") or 0.0)
                        except (TypeError, ValueError):
                            _eff_req = 0.0
                        if 0.0 < _eff_req <= 1.0:
                            _eff_req *= 100.0
                        if _eff_req > 0 and eff < _eff_req:
                            _why = (f"实测效率 {eff:.2f}% 低于需求下限 "
                                    f"{_eff_req:.2f}%")
                        else:
                            # 闸门①: 功率/转矩达标比 ≥ 0.95
                            try:
                                _tgt_kw = float(specs.get("power_kw")
                                                or specs.get("power") or 0.0)
                            except (TypeError, ValueError):
                                _tgt_kw = 0.0
                            _tgt_t = 0.0
                            try:
                                _tgt_t = float(specs.get("rated_torque") or 0.0)
                            except (TypeError, ValueError):
                                _tgt_t = 0.0
                            if _tgt_t <= 0 and _tgt_kw > 0:
                                try:
                                    _rpm = float(specs.get("speed_rpm")
                                                 or specs.get("speed") or 0.0)
                                    if _rpm > 0:
                                        _tgt_t = 9550.0 * _tgt_kw / _rpm
                                except (TypeError, ValueError):
                                    _tgt_t = 0.0
                            _att_ok = True
                            _att_txt = ""
                            try:
                                _pw = float(best.get("power")
                                            or best.get("power_kw") or 0.0)
                                _tq = float(best.get("torque") or 0.0)
                                _att_p = (_pw / _tgt_kw) if _tgt_kw > 0 else 1.0
                                _att_t = (_tq / _tgt_t) if _tgt_t > 0 else 1.0
                                _att_ok = min(_att_p, _att_t) >= 0.95
                                _att_txt = (f"P/T 达标比 {_att_p:.2f}/{_att_t:.2f}")
                            except (TypeError, ValueError):
                                _att_ok = True
                            if not _att_ok:
                                _why = f"{_att_txt} < 0.95 下限"
                            else:
                                # 闸门②: efficiency 写实测 (与需求存储口径一致: 小数)
                                specs["efficiency"] = round(eff / 100.0, 4)
                                specs["efficiency_source"] = "measured"
                                _summary = {
                                    "efficiency": eff,
                                    "max_temp": best.get("max_temp"),
                                    "stator_tooth_flux_density":
                                        best.get("stator_tooth_flux_density"),
                                    "nvh_risk": best.get("nvh_risk"),
                                    "ai_comment": best.get("ai_comment"),
                                    "solve_mode": best.get("solve_mode"),
                                    "power": best.get("power"),
                                    "torque": best.get("torque"),
                                }
                                from Core.agent_memory import add_case
                                _ok = add_case(
                                    specs.get("motor_type")
                                    or specs.get("series_id") or "unknown",
                                    specs, _summary)
                                if _ok:
                                    self._push_progress(
                                        "decision_gate",
                                        f"[Memory] 终选案例回写 1 条 (H22 三闸门通过, "
                                        f"η实测={eff:.2f}%)", "success")
                                else:
                                    self._push_progress(
                                        "decision_gate",
                                        "[Memory] 终选案例回写失败 (add_case 返回 False)",
                                        "warning")
                                return   # 已回写 (或回写失败), 不再走 _why 提示
            if _why:
                self._push_progress("decision_gate",
                                    f"[Memory] 案例回写已拦截 (H22 闸门): {_why}",
                                    "info")
        except Exception as _wb_e:
            try:
                self._push_progress("decision_gate",
                                    f"[Memory] 案例回写异常, 跳过: {_wb_e}", "warning")
            except Exception:
                pass

    def _node_decision_gate(self, state: WorkflowState) -> dict:
        """决策门: 要求支持报告与反对报告同时齐备, 并创建人工检查点

        P0-2 (决策门与质量门禁耦合): 终选须质量门禁通过且双报告齐备。
          质量门禁未通过时, 仍将双报告齐备的方案交由人工裁决 (强制 manual),
          但标记为"不可自动放行", 不再输出"已解锁"误导信息。
        """
        pro = state.get("review", "")
        con = state.get("con_report", "")
        reports_ready = bool(pro and con)
        project_id = state.get("project_id", "")
        checkpoint_id = ""

        # v4.01.038 (P0-2): 质量门禁超限转人工裁决 — 在检查点说明中标注风险
        qg = state.get("quality_gate_result") or {}
        # P0-2 (耦合校验): 质量门禁结论作为终选前置条件; 缺省 True 兼容未跑 QG 的流程
        qg_passed = bool(qg.get("passed", True))
        # 判定"已达重生上限转人工": quality_gate_result 未通过 且 redo_count 已达 max_redo
        _qg_max = 2   # 2026-09-21 用户指令: 5 → 3 → 2 (与 DEFAULT_QUALITY_GATE 对齐)
        try:
            _qg_max = int(_qg_config().get("max_redo", 2))
        except Exception:
            pass
        qg_manual = (isinstance(qg, dict) and not qg.get("passed", True)
                     and int(state.get("redo_count", 0) or 0) >= _qg_max)

        if reports_ready and project_id:
            if qg_passed:
                self._push_progress("decision_gate", "质量门禁通过, 双报告齐备, 决策门已解锁", "success")
            else:
                # P0-2: 质量门禁未通过 → 终选仍交由人工裁决(强制 manual), 不自动放行
                self._push_progress("decision_gate",
                                    "质量门禁未通过, 终选强制人工裁决 (不可自动放行)", "warn")
            # v3.00.28: 补充 AI 汇总的支持/反对报告简介 + 推荐方案简介 + 方案附加信息
            # 让方案终选弹窗可以区分方案、显示 AI 决策依据
            top_solutions_raw = state.get("top_solutions", [])[:10]
            # v3.00.28: 为每个方案附加 rank (UI 显示用) 和 core_length/torque 等区分字段
            top_solutions_enhanced = []
            for i, sol in enumerate(top_solutions_raw):
                if not isinstance(sol, dict):
                    continue
                eff = sol.get("efficiency", 0)
                if eff < 0 or eff > 100:
                    eff = 0
                item = {
                    "rank": sol.get("rank", i + 1),  # 若无 rank 用 i+1
                    "index": i + 1,
                    "efficiency": round(eff, 2) if eff else 0,
                    "max_temp": round(sol.get("max_temp", 0), 1) if sol.get("max_temp") else 0,
                    "stator_od": sol.get("stator_od", 0),
                    "core_length": sol.get("core_length", 0),  # v3.00.28: 铁心长 (用于区分)
                    "torque": round(sol.get("torque", 0), 1) if sol.get("torque") else 0,
                    "poles": sol.get("poles", 0),
                    "solve_mode": sol.get("solve_mode", ""),
                    "precision_level": sol.get("precision_level", "L1"),
                    "precision_label": sol.get("precision_label", ""),
                    "power": sol.get("power", sol.get("power_kw", 0)),
                }
                # v4.02.17 P2-2: 补齐全部输入/输出/派生参数 (标量 + RMxprt 结果展平)
                _covered = set(item.keys())
                for _k, _v in sol.items():
                    if _k in _covered:
                        continue
                    if isinstance(_v, dict):
                        if _k == "rmxprt_result":
                            for _rk, _rv in _v.items():
                                if isinstance(_rv, (int, float, str, bool)) or _rv is None:
                                    _nk = f"rmxprt_{_rk}"
                                    if _nk not in _covered:
                                        item[_nk] = _rv
                        continue
                    if isinstance(_v, (int, float, str, bool)) or _v is None:
                        item[_k] = _v
                top_solutions_enhanced.append(item)

            # v4.06: 案例电机匹配信息 (用于方案终选弹窗)
            case_motor_ctx = None
            case_motor = self._load_case_motor_for_review(state)
            if case_motor:
                case_match = self._compute_case_motor_match(state.get("results", []), case_motor)
                # 为每个增强方案附加 match_score
                for ts in top_solutions_enhanced:
                    idx = ts.get("index", 0) - 1
                    if idx < len(case_match):
                        ts["case_match_score"] = case_match[idx]["match_score"]
                    else:
                        ts["case_match_score"] = 0
                case_motor_ctx = {
                    "case_code": case_motor.get("case_code", ""),
                    "motor_type": case_motor.get("motor_type", ""),
                    "best_match_score": max(m["match_score"] for m in case_match) if case_match else 0,
                    "avg_match_score": round(sum(m["match_score"] for m in case_match) / len(case_match), 1) if case_match else 0,
                }

            # v3.00.28: 提取 AI 报告简介 (取前 300 字符, 包含 Markdown 标题)
            def _extract_brief(text: str, max_chars: int = 300) -> str:
                if not text:
                    return ""
                # 移除 Markdown 标题符, 取第一段非空内容
                cleaned = []
                for line in text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("---"):
                        cleaned.append(line)
                        if sum(len(c) for c in cleaned) > max_chars:
                            break
                brief = " ".join(cleaned)[:max_chars]
                return brief + ("..." if len(text) > max_chars else "")

            # v4.01.038 (P0-2): 质量门禁超限转人工裁决 — 标注风险
            _desc = "请审阅支持报告与反对报告,确认或覆盖AI推荐的最终方案"
            if qg_manual:
                _risk = "; ".join(qg.get("reasons", [])) or "质量门禁未通过"
                _desc = (f"⚠️ 质量门禁未通过且已重生 {state.get('redo_count', 0)} 次仍不满足, "
                         f"需人工裁决。风险: {_risk}")

            checkpoint_id = self._push_human_checkpoint(
                step="decision_gate",
                project_id=project_id,
                checkpoint_type="design_final_select",
                title="方案终选确认" + (" (质量门禁风险)" if (qg_manual or not qg_passed) else ""),
                description=_desc,
                db_context={
                    # 完整报告 (保留 1000 字)
                    "review": pro[:1000],
                    "con_report": con[:1000],
                    "recommendation": state.get("recommendation", "")[:500],
                    # v3.00.28: AI 汇总的简介 (弹窗直接显示)
                    "pros_brief": _extract_brief(pro, 300),
                    "cons_brief": _extract_brief(con, 300),
                    "recommended_brief": state.get("recommendation", "")[:300],
                    # v3.00.28: 增强的候选方案 (含 rank + 附加字段)
                    "top_solutions": top_solutions_enhanced,
                    "total_solutions": len(top_solutions_enhanced),
                    # v4.06: 案例电机匹配信息
                    "case_motor": case_motor_ctx,
                    # v4.01.038 (P0-2): 质量门禁结论 (人工裁决时可见)
                    "quality_gate_result": qg if isinstance(qg, dict) else {},
                    "quality_gate_passed": qg_passed,
                    "quality_gate_manual": qg_manual,
                },
                ui_context={
                    "total_solutions": len(top_solutions_enhanced),
                    "top_solutions": top_solutions_enhanced,
                    # F5 (v4.07): 补全全文报告与质量门禁结果 — 终选弹窗此前仅收 300 字摘要,
                    #   缺 review/con_report 全文与 quality_gate_result, 导致客户端无法展示完整依据
                    "review": pro[:1000],
                    "con_report": con[:1000],
                    "recommendation": state.get("recommendation", "")[:500],
                    "quality_gate_result": qg if isinstance(qg, dict) else {},
                    "pros_brief": _extract_brief(pro, 300),
                    "cons_brief": _extract_brief(con, 300),
                    "recommended_brief": state.get("recommendation", "")[:300],
                    "case_motor": case_motor_ctx,
                    "quality_gate_passed": qg_passed,
                    "quality_gate_manual": qg_manual,
                },
                timeout_seconds=hc.DEFAULT_TIMEOUTS["design_final_select"],  # v4.00.03: 5分钟
                default_action="auto_approve",
            )
        else:
            missing = []
            if not pro:
                missing.append("支持报告")
            if not con:
                missing.append("反对报告")
            missing_txt = "、".join(missing) if missing else "报告"
            # P0-2: 若质量门禁也未通过, 明确标注终选无法解锁 (不自动放行)
            _qg_hint = "" if qg_passed else "；且质量门禁未通过"
            self._push_progress(
                "decision_gate",
                f"决策门未解锁（缺失：{missing_txt}{_qg_hint}），已跳过人工确认，将自动采用 AI 推荐方案",
                "warn",
            )
            # [11] 显式通知客户端"人工确认被跳过"，避免用户误以为弹窗故障（保持双报告齐备才解锁的原逻辑不变）
            self._get_progress_queue().put({
                "type": "human_approval_skipped",
                "missing": missing,
                "quality_gate_passed": qg_passed,
                "message": f"方案终选人工确认已跳过（缺失：{missing_txt}{_qg_hint}），AI 推荐方案将被自动采用",
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            })

        # ★ H22 (2026-09-25 用户指令): 终选点案例回写 (三闸门, 仅 1 条)
        self._write_back_case_memory(state)

        return {"pro_report": pro,
                "ready_for_confirm": reports_ready,
                "quality_gate_passed": qg_passed,
                "hc_checkpoint_id": checkpoint_id}

    # ==================== 人工确认 ====================
    def _node_human_approval(self, state: WorkflowState) -> dict:
        """v2.05.03: 人工确认节点 - 阻塞等待用户决策或超时默认"""
        checkpoint_id = state.get("hc_checkpoint_id", "")
        project_id = state.get("project_id", "")
        if not checkpoint_id:
            return {"human_decision": "auto_skip", "human_comment": "无检查点"}

        self._push_progress("human_approval", "等待人工确认 (方案终选)...", "info")
        # v2.05.03: 开始记录人工等待时间
        self._get_cost_tracker().project_id = project_id
        human_event = self._get_cost_tracker().start_human_step("human_approval",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            # 每60秒推送一次倒计时
            if remaining % 60 == 0:
                self._push_progress("human_approval",
                                    f"人工确认倒计时: 剩余 {remaining//60} 分钟", "info")

        # v5.1: 本阶段 LLM 工作已完成, 人工检查点即将阻塞等待; 显式释放 Ollama 模型
        #        避免模型在人工思考期间空占显存 (流程内连续阶段仍靠 keep_alive=5m 不反复重载)
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 检查点释放模型失败(忽略): %s", _e)

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_approve")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        # 结束人工等待成本记录
        self._get_cost_tracker().end_human_step(human_event, decision=decision)

        # v4.01.029: 运营质量指标
        self._record_quality_event(project_id, "final_approval", decision,
                                   checkpoint_id=checkpoint_id,
                                   is_timeout=is_timeout)

        # v4.06-P4: 统一回退策略 — 集中重做上限保护 (本检查点无关键字段补缺)
        decision, _fb_specs, _fb_msg = self._apply_checkpoint_fallback(
            "human_approval", state, decision, {}, is_timeout)
        if _fb_msg:
            self._push_progress("human_approval", f"⚠️ 回退策略: {_fb_msg}", "warn")

        if is_timeout:
            self._push_progress("human_approval",
                                f"人工确认超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("human_approval",
                                f"收到人工决策: {decision}", "success")

        # v4.04 (#7 反向闭环): 用户采纳方案后, 将最优方案回写为案例电机库新记录
        # 使 AI 生成并采纳的设计成为未来设计的约束种子 (双向闭环接通)
        _gen_cm_id = 0
        if decision not in ("reject", "redo"):
            try:
                _gen_cm_id = self._persist_adopted_case_motor(state)
            except Exception as _rc_err:
                self._push_progress("human_approval",
                                    f"[#7 反向闭环] 回写异常(不影响交付): {_rc_err}",
                                    "warning")

        return {"human_decision": decision,
                "human_comment": comment,
                "ready_for_confirm": True,
                # v4.04 (#7): 回写案例ID 随 state 下传, 报告可引用闭环结果
                "generated_case_motor_id": _gen_cm_id}

    # ==================== v4.04 (#7 反向闭环): 采纳方案回写案例电机库 ====================
    def _persist_adopted_case_motor(self, state: WorkflowState) -> int:
        """v4.04 (#7 反向闭环): 将用户采纳的最优方案回写为案例电机, 未来可作种子。

        仅在案例电机库可用且本次尚未回写时执行; 返回新案例ID (0=未回写)。
        """
        if not _CM_DB_OK:
            return 0
        if state.get("generated_case_motor_id"):
            return int(state.get("generated_case_motor_id", 0))
        results = state.get("results", []) or []
        if not results:
            return 0
        # 采纳方案 = 效率最高者 (results 已按效率降序, 且精度等级已标注)
        best = results[0]
        specs = state.get("specs", {}) or {}
        project_id = state.get("project_id", "")

        def _f(*keys, default=None):
            for k in keys:
                if best.get(k) is not None:
                    return best.get(k)
            return default

        # ── ★ H26 (2026-10-07 用户指令): 入库条件门禁 G1~G6 — 全过才 verified, 否则 draft ──
        # 背景: 本路径原零闸门 (H22 只护 agent_memory 侧), CM#548 / CM#570(eff=0.0 却 verified)
        # 均由此写入; rated_* 原直填 specs 目标值 (伪达标), 下轮 B/A 种子门禁被构造性通过 ⇒ 污染链。
        _gate = {}            # 各闸门通过性 (保序, 供进度/溯源文本)
        _g2_why = []
        # G1 真实解: solve_mode ∈ (rmxprt, motorcad) ∧ ¬solve_failed
        _g1 = (best.get("solve_mode") in ("rmxprt", "motorcad")
               and not best.get("solve_failed"))
        _gate["G1真实解"] = _g1
        # G2 结果有效: η>0 ∧ P/T 达标比≥0.95 ∧ 磁密/温升非异常
        try:
            _eff_v = float(best.get("efficiency") or 0.0)
        except (TypeError, ValueError):
            _eff_v = 0.0
        _g2 = _eff_v > 0
        if not _g2:
            _g2_why.append(f"η={_eff_v:.2f}≤0")
        _tgt_kw = 0.0
        try:
            _tgt_kw = float(specs.get("power_kw") or specs.get("power") or 0.0)
        except (TypeError, ValueError):
            _tgt_kw = 0.0
        _tgt_t = 0.0
        try:
            _tgt_t = float(specs.get("rated_torque") or 0.0)
        except (TypeError, ValueError):
            _tgt_t = 0.0
        if _tgt_t <= 0 and _tgt_kw > 0:
            try:
                _rpm_t = float(specs.get("speed_rpm") or specs.get("speed") or 0.0)
                if _rpm_t > 0:
                    _tgt_t = 9550.0 * _tgt_kw / _rpm_t
            except (TypeError, ValueError):
                _tgt_t = 0.0
        try:
            _pw_v = float(best.get("power") or best.get("power_kw") or 0.0)
            _tq_v = float(best.get("torque") or 0.0)
            _att_p = (_pw_v / _tgt_kw) if _tgt_kw > 0 else 1.0
            _att_t = (_tq_v / _tgt_t) if _tgt_t > 0 else 1.0
            if min(_att_p, _att_t) < 0.95:
                _g2 = False
                _g2_why.append(f"P/T达标比{_att_p:.2f}/{_att_t:.2f}<0.95")
        except (TypeError, ValueError):
            pass
        for _fk in ("stator_tooth_flux_density", "stator_yoke_flux_density",
                    "air_gap_flux_density"):
            try:
                _fv = best.get(_fk)
                if _fv is not None and not (0.0 < float(_fv) <= 2.5):
                    _g2 = False
                    _g2_why.append(f"{_fk}={_fv}异常")
            except (TypeError, ValueError):
                pass
        try:
            _mt_v = best.get("max_temp")
            if _mt_v is not None and not (0.0 < float(_mt_v) <= 250.0):
                _g2 = False
                _g2_why.append(f"温升{_mt_v}K异常")
        except (TypeError, ValueError):
            pass
        _gate["G2结果有效"] = _g2
        # G3 质量门通过 (与 H22 同口径: 结果缺失视为通过)
        _qg_v = state.get("quality_gate_result") or {}
        _g3 = bool(_qg_v.get("passed", True))
        _gate["G3质量门"] = _g3
        # G4 实测口径: rated_* 优先取 best 实测值, 缺失才回退 specs 并显式标记 value_source
        _meas_pw = _f("power", "power_kw")
        try:
            _meas_pw = float(_meas_pw) if _meas_pw is not None else None
        except (TypeError, ValueError):
            _meas_pw = None
        _meas_spd = _f("speed", "rated_speed")
        try:
            _meas_spd = float(_meas_spd) if _meas_spd is not None else None
        except (TypeError, ValueError):
            _meas_spd = None
        _spec_pw = None
        try:
            _spec_pw = float(specs.get("power_kw") or specs.get("power") or 0) or None
        except (TypeError, ValueError):
            _spec_pw = None
        _spec_spd = None
        try:
            _spec_spd = float(specs.get("speed_rpm") or specs.get("speed") or 0) or None
        except (TypeError, ValueError):
            _spec_spd = None
        _rated_pw = _meas_pw if (_meas_pw and _meas_pw > 0) else _spec_pw
        _rated_spd = _meas_spd if (_meas_spd and _meas_spd > 0) else _spec_spd
        if _meas_pw and _meas_pw > 0 and _meas_spd and _meas_spd > 0:
            _value_source = "measured"
        elif _meas_pw and _meas_pw > 0:
            # 速度为求解输入 (规格值), 功率实测 → 分字段标记, 不假称全实测
            _value_source = "measured_power+nameplate_speed"
        elif _rated_pw and _rated_spd:
            _value_source = "nameplate"
        else:
            _value_source = ""
        # G6 D²L/转矩密度对账型谱带 (同极数中位 ±40%; 型谱缺失 fail-open 不拦)
        _g6 = True
        _g6_txt = ""
        try:
            _sid_v = float(best.get("stator_id") or 0.0)
            _cl_v = float(best.get("core_length") or 0.0)
            _tq_g6 = float(best.get("torque") or 0.0)
            if _sid_v > 0 and _cl_v > 0 and _tq_g6 > 0:
                _d2l_v = (_sid_v / 1000.0) ** 2 * (_cl_v / 1000.0)
                _td_v = (_tq_g6 / _d2l_v) if _d2l_v > 0 else 0.0
                _poles_g6 = int(_f("poles", default=0) or 0)
                _ref_td = self._h26_catalog_td2l_median(_poles_g6)
                if _ref_td > 0:
                    _dev_g6 = abs(_td_v - _ref_td) / _ref_td
                    if _dev_g6 > 0.40:
                        _g6 = False
                        _g6_txt = (f"T/D²L={_td_v:.0f} 偏离{_poles_g6}极型谱中位"
                                   f"{_ref_td:.0f} 达{_dev_g6 * 100:.0f}% >±40%")
                    else:
                        _g6_txt = (f"T/D²L={_td_v:.0f} vs {_poles_g6}极型谱中位"
                                   f"{_ref_td:.0f} ({_dev_g6 * 100:.0f}%)")
                else:
                    _g6_txt = "型谱无同极数样本, G6跳过"
            else:
                _g6_txt = "几何/转矩缺失, G6跳过"
        except Exception as _g6_e:
            _g6_txt = f"G6异常跳过: {_g6_e}"
        _gate["G6型谱D²L"] = _g6
        # G5 status 裁决: G1∧G2∧G3∧G6 全过才 verified, 否则一律 draft (不作未来种子)
        _h26_verified = bool(_g1 and _g2 and _g3 and _g6)
        _gate_txt = " ".join(f"{_k}={'✔' if _v else '✗'}" for _k, _v in _gate.items())

        _motor_type = specs.get("motor_type") or "永磁同步电动机"
        data = {
            "motor_type": _motor_type,
            "motor_model": specs.get("motor_model", "") or "",
            "series_id": specs.get("series_id", "") or "",
            "series_name": specs.get("series_name", "") or "",
            "rated_power": _rated_pw,
            "rated_voltage": specs.get("voltage_v") or _f("voltage"),
            "rated_speed": _rated_spd,
            "poles": _f("poles"),
            "efficiency": _f("efficiency"),
            "power_factor": _f("power_factor"),
            "temperature_rise": _f("max_temp"),
            "stator_od": _f("stator_od"),
            "stator_id": _f("stator_id"),
            "rotor_od": _f("rotor_od"),
            "core_length": _f("core_length"),
            "air_gap": _f("air_gap"),
            "tooth_width": _f("tooth_width"),
            "yoke_thickness": _f("yoke_thickness"),
            "shaft_dia": _f("shaft_dia"),
            "slots_stator": _f("slots_stator"),
            "slots_rotor": _f("slots_rotor"),
            "turns_per_coil": _f("turns_per_coil"),
            "parallel_branches": _f("parallel_circuits"),
            "stator_tooth_flux": _f("stator_tooth_flux_density"),
            "stator_yoke_flux": _f("stator_yoke_flux_density"),
            "rotor_yoke_flux": _f("rotor_yoke_flux_density"),
            "air_gap_flux": _f("air_gap_flux_density"),
            "current_density": _f("current_density"),
            "copper_loss": _f("copper_loss"),
            "iron_loss": _f("iron_loss"),
            "total_loss": _f("total_loss"),
            "source_type": "ai_generated",
            "source_detail": (f"MotorAI编排器采纳生成 (project_id={project_id}) | "
                              f"H26门禁 {_gate_txt} | value_source={_value_source or 'unknown'}"),
            "notes": (f"由 AI 团队采纳回写 (精度={best.get('precision_label','')}, "
                      f"模式={best.get('solve_mode','')}); H26: {_g6_txt}"
                      + (f" | G2原因: {'; '.join(_g2_why)}" if _g2_why else "")),
            # ★ H26 G5: status=verified 需 G1∧G2∧G3∧G6 齐备, 否则一律 draft
            "status": "verified" if _h26_verified else "draft",
        }
        try:
            created = create_case_motor(data)
            case_id = created.get("id")
            if not case_id:
                return 0
            # 写入正向验算记录 (供案例库校验/血缘)
            _details = [{"metric": k, "value": best.get(k)}
                        for k in ("efficiency", "max_temp", "torque", "power",
                                  "stator_tooth_flux_density", "current_density",
                                  "total_loss")
                        if best.get(k) is not None]
            # ★ H26: 验算结论与 status 裁决同源 (门禁全过才 pass)
            _verify_result = "pass" if _h26_verified else "warning"
            save_verification(
                case_id, "forward", _verify_result, _details,
                summary=(f"AI采纳方案验算: 效率={best.get('efficiency')}%, "
                         f"温升={best.get('max_temp')}K, 精度={best.get('precision_label','')}"
                         f"; H26门禁 {_gate_txt}"))
            if _h26_verified:
                self._push_progress("human_approval",
                                    f"[#7 反向闭环] 已回写案例电机库 → CM#{case_id} "
                                    f"(H26 门禁全过, status=verified, "
                                    f"value_source={_value_source})", "success")
            else:
                _draft_why = _g2_why if _g2_why else (
                    [] if _h26_verified else
                    [k for k, v in (("G1真实解", _g1), ("G3质量门", _g3),
                                    ("G6型谱D²L", _g6)) if not v])
                self._push_progress("human_approval",
                                    f"[#7 反向闭环] 已回写案例电机库 → CM#{case_id} "
                                    f"(H26 门禁未全过 → status=draft, 不作未来种子; "
                                    f"原因: {'; '.join(_draft_why) or _gate_txt})", "warning")
            return case_id
        except Exception as _e:
            self._push_progress("human_approval",
                                f"[#7 反向闭环] 回写失败(不影响交付): {_e}", "warning")
            return 0

    def _h26_catalog_td2l_median(self, poles: int) -> float:
        """★ H26 G6: 同极数型谱 T/D²L 中位数 (Knowledge/y_series_physics.json)。

        D²L 口径与项目一致: D = 定子内径 stator_inner_dia_mm。
        返回 0 表示无同极数样本 / 型谱不可读 (fail-open, G6 跳过不拦)。
        """
        try:
            import statistics as _stats
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _path = os.path.join(_root, "Knowledge", "y_series_physics.json")
            with open(_path, "r", encoding="utf-8") as _fh:
                _cat = (json.load(_fh) or {}).get("catalog") or []
            _vals = []
            for _r in _cat:
                try:
                    if int(_r.get("poles") or 0) != int(poles or 0):
                        continue
                    _t = float(_r.get("torque_nm") or 0.0)
                    _d2l = float(_r.get("d_squared_l_m3") or 0.0)
                    if _t > 0 and _d2l > 0:
                        _vals.append(_t / _d2l)
                except (TypeError, ValueError):
                    continue
            if not _vals:
                return 0.0
            return float(_stats.median(_vals))
        except Exception:
            return 0.0

    # ==================== 报告 ====================
    def _node_generate_report(self, state: WorkflowState) -> dict:
        self._push_progress("generate_report", "报告撰写员开始生成正式设计报告...")

        # v4.00.01: 如果是报告返回重做, 显示审核意见
        review_comment = state.get("human_comment", "")
        if review_comment and state.get("human_decision") in ("reject", "redo"):
            self._push_progress("generate_report",
                                f"包含报告审核意见: {review_comment[:60]}", "info")

        use_cloud = state.get("use_cloud_report", False)
        review = state.get("review", "无评审意见")
        results = state.get("results", [])
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")

        # v4.00.02: 将审核意见附加到评审意见中, 让报告撰写员知晓用户反馈
        if review_comment and state.get("human_decision") in ("reject", "redo"):
            review = review + f"\n\n## 用户审核意见（返回重做）\n{review_comment}\n\n请在报告中针对以上用户意见进行回应和调整。"

        _start_ts = time.time()
        self._push_progress("generate_report", "调用本地 LLM 生成报告 (可能需要 30-60 秒)...")

        # v3.00.28: 收集流程耗时统计和人工审核意见, 附加到报告中
        timing_summary = {}
        human_reviews = []
        try:
            timing_summary = self.generate_timing_summary()
        except Exception:
            pass
        try:
            from Core.human_checkpoint import get_project_checkpoints
            human_reviews = get_project_checkpoints(project_id)
        except Exception:
            pass

        result = self.report_agent.safe_run(
            specs=specs, results=results, review=review, use_cloud=use_cloud,
            project_id=project_id,
            timing_summary=timing_summary,
            human_reviews=human_reviews,
            # v4.02.104 (问题3): 轮次 → 文件名 <project_id>_R<round>_report_<ts>.md
            attempt_index=state.get("attempt_index", 1),
            # v4.02.104 (三.2): 门禁结果 (含选中方案快照 + 电磁负荷/材料利用率)
            quality_gate_result=state.get("quality_gate_result") or {})

        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            self._push_progress("generate_report", f"报告生成失败: {result['error']}", "error")
            record_step_metric(project_id, "generate_report", "ReportAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=result["error"][:200])
            return {"errors": [result["error"]]}

        self._push_progress("generate_report",
                            f"报告已生成: {result.get('filename', '?')}", "success")
        # v4.02.103 (P3-C): Word 镜像可见性 (客户端可在进度流直接看到落盘位置)
        if result.get("docx_filepath"):
            self._push_progress(
                "generate_report",
                f"Word 镜像已落盘: {os.path.basename(result['docx_filepath'])}", "info")

        # v2.04: 记录运营指标 + 项目汇总
        record_step_metric(project_id, "generate_report", "ReportAgent",
                           duration_ms=_duration_ms,
                           cost_cents=50 if use_cloud else 0,
                           cost_mode="cloud" if use_cloud else "local",
                           quality_score=90)

        # v2.05.03: 记录报告生成AI成本
        self._get_cost_tracker().project_id = project_id
        model_type = "cloud" if use_cloud else "local"
        # v4.06-P1: 优先用 ReportAgent 真实 token 计量, 无则回退启发式
        _rp_ti = result.get("tokens_in", 0) or 0
        _rp_to = result.get("tokens_out", 0) or 0
        tokens_in = _rp_ti or (_estimate_tokens(specs) + _estimate_tokens(results) + _estimate_tokens(review))
        tokens_out = _rp_to or _estimate_tokens(result.get("report", ""))
        self._get_cost_tracker().record_ai_step("generate_report", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type=model_type,
                                         duration_ms=_duration_ms,
                                         detail={"filename": result.get("filename", "")})

        # 生成并保存成本报告 + 运营分析
        # v4.02.104 (问题4): 成本量纲统一 —— 不再自行 *100 换算, 直接取 cost_tracker
        #   给出的**分**口径权威值 total_cost_cents (一元=100分)。
        #   旧写法 int(cost_report["total_cost"] * 100) 与降级分支的裸 50 并存 ⇒
        #   同一字段两种量纲, 运营飞轮成本 KPI 失真。
        _qg_res = state.get("quality_gate_result") or {}
        _quality_overall = _quality_overall_from_gate(_qg_res)
        try:
            cost_report = self._get_cost_tracker().generate_report()
            # 记录项目汇总 (供运营飞轮进化) - 使用真实成本数据
            total_cost_cents = int(cost_report.get("total_cost_cents", 0) or 0)
            total_duration_ms = sum(
                e.get("duration_ms", 0) for e in cost_report.get("detailed_breakdown", [])
            )
            record_project_summary(
                project_id=project_id, specs=specs,
                total_duration_ms=total_duration_ms,
                total_cost_cents=total_cost_cents,
                quality_overall=_quality_overall,
            )
        except Exception:
            # 成本统计异常不影响主流程
            # v4.02.104: 50 元 = 5000 分 (原裸值 50 量纲不明, 与主路径差 100 倍)
            record_project_summary(
                project_id=project_id, specs=specs,
                total_duration_ms=_duration_ms,
                total_cost_cents=5000 if use_cloud else 0,
                quality_overall=_quality_overall,
            )

        # 触发运营飞轮进化 (异步, 不阻塞报告输出)
        try:
            evo_results = run_all_evolution()
            _evo_count = sum(1 for v in evo_results.values()
                             if v.get("evolved") or v.get("alerts"))
            if _evo_count:
                self._push_progress("generate_report",
                                    f"运营飞轮: {_evo_count} 项进化触发",
                                    "info")
        except Exception as e:
            pass  # 飞轮异常不影响主流程

        # v4.01.029: P2 闭环 — 高频人工修正参数自动提升为强制追问规则
        if _QM_AVAILABLE:
            try:
                _promo = _qm_promote(days=90, apply=True)
                if _promo.get("applied"):
                    self._push_progress(
                        "generate_report",
                        f"运营闭环: {len(_promo['applied'])} 个高频修正参数"
                        f"已转为强制追问规则 ({', '.join(_promo['applied'])})",
                        "info")
            except Exception:
                pass

        return {"report": result["report"],
                "report_filepath": result["filepath"],
                "report_filename": result["filename"],
                "report_docx_filepath": result.get("docx_filepath") or ""}

    # ==================== v2.05.06: 报告确认检查点 ====================
    def _node_hc_report(self, state: WorkflowState) -> dict:
        """v2.05.06: 报告生成后的人机交互检查点"""
        project_id = state.get("project_id", "")
        report_filename = state.get("report_filename", "")
        report_filepath = state.get("report_filepath", "")

        self._push_progress("hc_report", "创建报告确认检查点...")

        checkpoint_id = self._push_human_checkpoint(
            step="hc_report",
            project_id=project_id,
            checkpoint_type="report_review",
            title="报告确认",
            description="请审阅报告撰写员生成的设计报告,确认或要求重新生成",
            db_context={
                "report_filename": report_filename,
                "report_filepath": report_filepath,
                "report_preview": state.get("report", "")[:2000],
            },
            timeout_seconds=hc.DEFAULT_TIMEOUTS["report_review"],
            default_action="auto_approve",
        )

        self._get_cost_tracker().project_id = project_id
        human_event = self._get_cost_tracker().start_human_step("hc_report",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            if remaining % 60 == 0 and remaining > 0:
                self._push_progress("hc_report",
                                    f"报告确认倒计时: 剩余 {remaining//60} 分钟", "info")

        # v5.1: 本阶段 LLM 工作已完成, 人工检查点即将阻塞等待; 显式释放 Ollama 模型
        #        避免模型在人工思考期间空占显存 (流程内连续阶段仍靠 keep_alive=5m 不反复重载)
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 检查点释放模型失败(忽略): %s", _e)

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_approve")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        self._get_cost_tracker().end_human_step(human_event, decision=decision)

        # v4.01.029: 运营质量指标
        self._record_quality_event(project_id, "report_review", decision,
                                   checkpoint_id=checkpoint_id,
                                   is_timeout=is_timeout)

        # v4.06-P4: 统一回退策略 — 集中重做上限保护 (本检查点无关键字段补缺)
        decision, _fb_specs, _fb_msg = self._apply_checkpoint_fallback(
            "hc_report", state, decision, {}, is_timeout)
        if _fb_msg:
            self._push_progress("hc_report", f"⚠️ 回退策略: {_fb_msg}", "warn")

        if is_timeout:
            self._push_progress("hc_report",
                                f"报告确认超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("hc_report",
                                f"收到人工决策: {decision}", "success")

        # v4.07 (F8): 报告确认阶段返回重做计数
        _report_redo_count = int(state.get("report_redo_count", 0) or 0)
        if decision in ("reject", "redo"):
            _report_redo_count += 1

        return {"human_decision": decision,
                "human_comment": comment,
                "report_redo_count": _report_redo_count}

    # ==================== 执行入口 ====================
    def _initial_state(self, raw_text, solve_count, fine_solve_count, use_cloud_review, use_cloud_report, case_motor_id=0):
        return {
            "raw_text": raw_text, "specs": {}, "confidence": 0.0,
            "results": [], "top_solutions": [],
            "scenario_info": {}, "layered_specs": {}, "pipeline_steps": [],
            "base_params": {}, "base_param_ranges": {}, "ref_motors": [],
            "review": "", "recommendation": "",
            "con_report": "", "convergence_findings": {},
            "pro_report": "", "ready_for_confirm": False,
            "report": "", "report_filepath": "", "report_filename": "",
            "report_docx_filepath": "",
            "matched_templates": [], "project_id": "",
            "errors": [],
            "solve_count": solve_count,
            "fine_solve_count": fine_solve_count,
            "use_cloud_review": use_cloud_review,
            "use_cloud_report": use_cloud_report,
            "step": "init", "status": "started", "timestamp": "",
            "case_motor_id": case_motor_id,
            "case_motor_ref": {},
            # v4.01.038 (P0-2): 质量门禁 + 方案重生初始态
            "quality_gate_result": {},
            "redo_count": 0,
            "auto_regenerate": False,
            "regenerate_guidance": "",
            # P1/P2 (v4.02.67): 结构化改进指令 / 折中 / 措施效果评价初始态
            "improvement_instructions": [],
            "slot_fill_compromises": [],
            # ★ 方案② (2026-10-10): 分轨措施初始态 —— 与 WorkflowState 成对声明,
            #   否则被 LangGraph 静默丢弃 (P1-0 同款教训)。
            "improvement_instructions_by_rail": {},
            "slot_fill_compromises_by_rail": {},
            "prev_round_metrics": {},
            "measure_effect": {},
            # ★ P1-4 (2026-10-11): 锁定轨「杠杆耗尽」识别的跨轮状态初始态 (与 WorkflowState 成对声明)。
            "rail_sf_history": {},
            "rail_exhausted": {},
            # ★ P1-0 (2026-09-22 用户指令): 卡死检测通道初始态 —— 与 WorkflowState 成对声明。
            #   此前只在 _out 写、未在 state 声明 ⇒ 被 LangGraph 静默丢弃 ⇒ 恒 None。
            "prev_qg_reasons": [],
            # P3 (v4.02.93): L2(回 2D 起始) 升级标记初始态
            "l2_escalated": False,
            # 2026-09-21 (用户指令): 质量门分层, 每层 2 次, 总 2³=8
            # 2026-09-22 (P0-1): 起点/顺序改为 2D→3D→1D (_QG_TIER_START/_QG_TIER_ORDER)
            "qg_tier": _QG_TIER_START,
            "qg_tier3_count": 0,
            "qg_tier2_count": 0,
            "qg_tier1_count": 0,
            "total_regen_count": 0,
            # v4.02.68 (系列三 Tier A/B): L0↔L1 差异评价 + 残差飞轮标定
            "l0_l1_diff": {},
            "l0_l1_residuals": [],
            "l0_calibration": {},
            # v4.01.038 (P0-3): 方案构思初始态
            "ideation_result": "",
            "design_hints": {},
            # v4.01.039 (P1-6 #486): 局部重做计数初始态
            "design_redo_count": 0,
            # v4.07 (F8): 分阶 redo 计数初始态
            "requirement_redo_count": 0,
            "base_param_redo_count": 0,
            "report_redo_count": 0,
            # v4.04 (#10 / #7): 多样性指标 + 反向闭环回写案例ID 初始态
            "diversity_metrics": {},
            "generated_case_motor_id": 0,
            # v4.02.74: 与 WorkflowState 同步声明 (仅在节点 _out 写、未在初始态出现时
            # LangGraph 同样会把这 3 个 key 从初始输入静默丢弃 → 首轮读物恒为默认)
            "geometry_images": [],
            "attempt_index": 1,
            "attempt_reason": "",
        }

    def run(self, raw_text: str, solve_count: int = 10,
            fine_solve_count: int = 0,
            use_cloud_review: bool = False,
            use_cloud_report: bool = False,
            case_motor_id: int = 0) -> dict:
        # v2.05.02: 应用飞轮路由规则
        use_cloud_review, use_cloud_report = _apply_routing_rules(use_cloud_review, use_cloud_report)
        initial_state = self._initial_state(raw_text, solve_count, fine_solve_count, use_cloud_review, use_cloud_report, case_motor_id)
        audit_logger.log(agent_name="Orchestrator", action="workflow_start",
                         parameters={"raw_text": raw_text[:100], "solve_count": solve_count, "fine_solve_count": fine_solve_count, "case_motor_id": case_motor_id},
                         status="started")
        final_state = self.compiled.invoke(
            initial_state,
            config={"configurable": {"thread_id": f"thread_{int(time.time())}"}},
            # v4.01.038 (P0-2): 放宽递归上限, 容纳质量门禁方案重生环路
            # (默认 25 在多次重生 + 多人工检查点下可能不足)
            recursion_limit=60)
        final_step = final_state.get("step", "unknown")
        final_status = STEP_STATUS_MAP.get(final_step, final_state.get("status", "unknown"))
        audit_logger.log(agent_name="Orchestrator", action="workflow_complete",
                         parameters={"project_id": final_state.get("project_id", "")},
                         result={"status": final_status,
                                 "results_count": len(final_state.get("results", [])),
                                 "ready_for_confirm": final_state.get("ready_for_confirm", False)},
                         status="success" if final_status == "completed" else "partial")
        # v5.1: 整个 AI 团队流程结束, 显式释放所有 Ollama 模型, 把显存完全交还
        try:
            from Core.ollama_helper import release_checkpoint_models
            release_checkpoint_models()
        except Exception as _e:
            logger.debug("[orchestrator] 流程结束释放模型失败(忽略): %s", _e)
        return final_state

    def _next_steps_after(self, node_name: str, state_update: dict) -> list:
        """推断节点完成后的下一步 (用于推送 step_start)"""
        if node_name == "load_case_motor":
            case_ref = state_update.get("case_motor_ref", {})
            if case_ref:
                return [{"step": "parse_requirement",
                         "message": f"案例电机 {case_ref.get('case_code', '')} 已加载, "
                                    f"需求分析师开始解析 (含案例参数)..."}]
            return [{"step": "parse_requirement",
                     "message": "需求分析师开始解析客户需求..."}]
        elif node_name == "parse_requirement":
            return [{"step": "hc_requirement",
                     "message": "需求分析师完成解析, 等待人工确认需求..."}]
        elif node_name == "hc_requirement":
            decision = state_update.get("human_decision", "auto_approve")
            if decision in ("reject", "redo"):
                comment = state_update.get("human_comment", "")
                msg = "需求被驳回" if decision == "reject" else "需求返回重做"
                if comment:
                    msg += f" (含审核意见)"
                return [{"step": "parse_requirement",
                         "message": f"{msg}, 需求分析师重新解析 (含审核意见)..."}]
            # v4.02: 需求确认后先生成基准参数, 再进入基准参数选定检查点
            return [{"step": "generate_base_params",
                     "message": "方案生成师开始生成推荐电机基准参数..."}]
        elif node_name == "generate_base_params":
            return [{"step": "hc_base_param",
                     "message": "基准参数生成完成, 等待人工选定基准参数..."}]
        elif node_name == "hc_base_param":
            decision = state_update.get("human_decision", "auto_approve")
            if decision in ("reject", "redo"):
                comment = state_update.get("human_comment", "")
                msg = "基准参数被驳回" if decision == "reject" else "基准参数返回重做"
                if comment:
                    msg += f" (含审核意见)"
                return [{"step": "generate_base_params",
                         "message": f"{msg}, 方案生成师重新生成基准参数 (含审核意见)..."}]
            return [{"step": "generate_designs",
                     "message": "基准参数已确认, 方案生成师开始生成参数矩阵并批量求解..."}]
        elif node_name == "generate_designs":
            return [{"step": "hc_design",
                     "message": "方案生成完成, 等待人工确认方案..."}]
        elif node_name == "hc_design":
            decision = state_update.get("human_decision", "auto_approve")
            if decision in ("reject", "redo"):
                comment = state_update.get("human_comment", "")
                msg = "方案被驳回" if decision == "reject" else "方案返回重做"
                if comment:
                    msg += f" (含审核意见)"
                # v4.00.04: 实际图路由 _after_hc_design reject → parse_requirement
                return [{"step": "parse_requirement",
                         "message": f"{msg}, 需求分析师重新解析 (含审核意见)..."}]
            # v4.00.04: hc_design 节点只在有结果时才会被到达 (generate_designs → hc_design),
            # 所以 approve/auto_first 时必定有结果, 直接返回双轨, 不需要检查 state_update.get("results")
            return [
                {"step": "review_solutions",
                 "message": "评审推荐师开始分析方案矩阵 (放大轨)..."},
                {"step": "convergence_review",
                 "message": "收敛阵营启动: 逆向评审 → 现实检验 → 沙盘推演 → 魔鬼代言人..."},
            ]
        elif node_name == "decision_gate":
            # v2.05.03: 双报告齐备时进入人工确认, 否则直达报告
            if state_update.get("ready_for_confirm") and state_update.get("hc_checkpoint_id"):
                return [{"step": "human_approval",
                         "message": "等待人工确认: 请审阅双报告并做出决策..."}]
            return [{"step": "generate_report",
                     "message": "报告撰写员开始生成正式设计报告..."}]
        elif node_name == "human_approval":
            decision = state_update.get("human_decision", "auto_approve")
            if decision in ("reject", "redo"):
                comment = state_update.get("human_comment", "")
                msg = "方案被驳回" if decision == "reject" else "方案返回重做"
                if comment:
                    msg += f" (含审核意见)"
                # v4.00.04: 实际图路由 _after_human_approval reject → hc_design (重新审阅方案)
                return [{"step": "hc_design",
                         "message": f"{msg}, 回退方案确认重新审阅..."}]
            return [{"step": "generate_report",
                     "message": "报告撰写员开始生成正式设计报告..."}]
        elif node_name == "generate_report":
            return [{"step": "hc_report",
                     "message": "报告生成完成, 等待人工确认报告..."}]
        elif node_name == "hc_report":
            decision = state_update.get("human_decision", "auto_approve")
            if decision in ("reject", "redo"):
                comment = state_update.get("human_comment", "")
                msg = "报告被驳回" if decision == "reject" else "报告返回重做"
                if comment:
                    msg += f" (含审核意见)"
                # v4.00.04: 实际图路由 _after_hc_report reject → parse_requirement
                return [{"step": "parse_requirement",
                         "message": f"{msg}, 需求分析师重新解析 (含审核意见)..."}]
            return []  # 流程结束
        return []

    def run_streaming(self, raw_text: str, solve_count: int = 10,
                      fine_solve_count: int = 0,
                      use_cloud_review: bool = False,
                      use_cloud_report: bool = False,
                      case_motor_id: int = 0):
        """
        流式执行工作流, 实时推送进度事件:
          - step_start: 节点开始前
          - progress: 节点执行中的子步骤进度
          - step_done: 节点完成后 (含结果数据)
          - fatal: 致命错误

        使用后台线程运行 LangGraph stream, 主线程通过队列轮询:
          1. 先 yield step_start (第一个节点)
          2. 循环: drain progress queue → 检查 node_done → yield step_start(下一节点)
          3. 结束时 drain 剩余 progress
        """
        # v2.05.02: 应用飞轮路由规则
        use_cloud_review, use_cloud_report = _apply_routing_rules(use_cloud_review, use_cloud_report)

        # v4.00.07: 信号量并发 — 小于 max_concurrent 时并发执行, 超出排队
        # v4.00.004: 排队超时从 workflow_config 读取 (默认 600s)
        MAX_QUEUE_WAIT = _wc_get_queue_timeout() if _WC_AVAILABLE else 600
        wait_start = time.time()
        acquired_slot = False
        in_queue = False
        my_thread_id = f"wf_{int(time.time() * 1000)}_{id(raw_text) % 100000}"

        try:
            while True:
                with self._workflow_lock:
                    if self._active_count < self._max_concurrent:
                        # 有空闲槽位, 获取执行权
                        self._active_count += 1
                        self._running_since[my_thread_id] = time.time()
                        acquired_slot = True
                        if self._active_count >= self._max_concurrent:
                            self._workflow_slot_event.clear()  # 槽位已满
                        break

                    # v4.00.05: 僵尸检测 — 清理超过 10 分钟的工作流
                    now = time.time()
                    stale_ids = [tid for tid, ts in self._running_since.items()
                                 if now - ts > 600]
                    for sid in stale_ids:
                        logger.warning(f"检测到僵尸工作流 ({sid}, >10min), 自动释放")
                        self._running_since.pop(sid, None)
                    if stale_ids:
                        self._active_count = max(0, self._active_count - len(stale_ids))
                        self._workflow_slot_event.set()
                        continue  # 重试获取槽位

                # 槽位已满, 进入排队等待
                if not in_queue:
                    with self._workflow_lock:
                        self._queue_count += 1
                        my_position = self._queue_count
                        in_queue = True

                waited = int(time.time() - wait_start)
                if waited > MAX_QUEUE_WAIT:
                    yield {"type": "fatal", "error": f"排队等待超时 ({waited}秒), 请稍后重试"}
                    return

                yield {"type": "queued", "message": f"排队等待中 (前方还有 {my_position} 个任务, 已等待 {waited} 秒, 当前 {self._active_count}/{self._max_concurrent} 并发)..."}

                # 等待槽位释放 (5秒超时以定期推送状态)
                self._workflow_slot_event.wait(timeout=5.0)

            # 退出队列
            if in_queue:
                with self._workflow_lock:
                    self._queue_count = max(0, self._queue_count - 1)
                    in_queue = False

            # 成功获取槽位, 开始执行工作流
            yield from self._run_streaming_impl(
                raw_text, solve_count, fine_solve_count,
                use_cloud_review, use_cloud_report, case_motor_id
            )
        finally:
            # v4.00.07: 无论正常结束/异常/客户端断开, 都清理状态
            if in_queue:
                with self._workflow_lock:
                    self._queue_count = max(0, self._queue_count - 1)
            if acquired_slot:
                with self._workflow_lock:
                    self._active_count = max(0, self._active_count - 1)
                    self._running_since.pop(my_thread_id, None)
                    self._workflow_slot_event.set()  # 通知排队的工作流

    def _run_streaming_impl(self, raw_text, solve_count, fine_solve_count,
                            use_cloud_review, use_cloud_report, case_motor_id):
        """工作流实际执行逻辑 (由 run_streaming 调用, 外层有 try/finally 保护)

        v4.00.07: 每个工作流创建独立的 progress_queue / cost_tracker /
        checkpoint_conn / compiled, 支持并发执行互不干扰。
        """
        use_cloud_review, use_cloud_report = _apply_routing_rules(use_cloud_review, use_cloud_report)

        initial_state = self._initial_state(raw_text, solve_count, fine_solve_count, use_cloud_review, use_cloud_report, case_motor_id)
        thread_id = f"thread_{int(time.time())}_{id(raw_text) % 100000}"
        config = {"configurable": {"thread_id": thread_id}}

        # v4.00.07: 每个工作流独立的状态 (支持并发隔离)
        wf_progress_q = queue.Queue()
        wf_cost_tracker = TaskCostTracker()

        # v4.00.07: 每个工作流独立的 SQLite checkpoint 连接
        import sqlite3 as _sqlite3_mod
        _checkpoint_db = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                     "KnowledgeBase", "workflow_checkpoints.db")
        local_conn = _sqlite3_mod.connect(_checkpoint_db, check_same_thread=False, timeout=10)
        local_conn.execute("PRAGMA journal_mode=WAL")
        local_conn.execute("PRAGMA synchronous=NORMAL")
        local_conn.execute("PRAGMA busy_timeout=5000")
        # v2.05.08: 主动合并残留 WAL, 与新工作流独立连接保持一致
        try:
            local_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        local_checkpointer = SqliteSaver(local_conn)
        local_compiled = self.graph.compile(checkpointer=local_checkpointer)

        audit_logger.log(agent_name="Orchestrator", action="workflow_start_stream",
                         parameters={"raw_text": raw_text[:100], "solve_count": solve_count, "fine_solve_count": fine_solve_count, "case_motor_id": case_motor_id},
                         status="started")

        # 后台线程运行 LangGraph stream
        result_queue = queue.Queue()

        # ★ R8 (2026-09-23) 取消传播: 捕获 SSE 线程(已 bind_job)的 job_id, 传入
        #   _worker 线程入口再 bind —— 否则图节点(含 batch_solve→rmxprt 轮询)所在
        #   线程无绑定, is_cancelled() 退回全局标志, 按作业隔离的取消无法触达
        #   求解层的 AEDT 树杀(rmxprt_tools R8 取消检查依赖此绑定)。
        _parent_jid = None
        _ph_mod = None
        try:
            from progress_hub import progress_hub as _ph_mod
            _parent_jid = _ph_mod.current_job_id()
        except Exception:
            _ph_mod = None

        def _worker():
            # v4.00.07: 设置线程本地状态, 使节点方法能访问到本工作流的队列和追踪器
            self._tl.progress_queue = wf_progress_q
            self._tl.cost_tracker = wf_cost_tracker
            # ★ R8: 绑定父线程(SSE)的作业 ⇒ 本线程内 is_cancelled() 只反映本作业
            try:
                if _ph_mod is not None and _parent_jid:
                    _ph_mod.bind_job(_parent_jid)
            except Exception:
                pass
            try:
                for event in local_compiled.stream(initial_state, config=config,
                                                   stream_mode="updates",
                                                   # v4.01.038 (P0-2): 放宽递归上限容纳方案重生环路
                                                   recursion_limit=60):
                    for node_name, state_update in event.items():
                        if state_update is None:
                            state_update = {}
                        result_queue.put(("node_done", node_name, state_update))
            except Exception as e:
                result_queue.put(("error", None, str(e)))
            finally:
                result_queue.put(("finished", None, None))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        # 1. 推送第一个节点的 step_start
        if case_motor_id:
            yield {
                "type": "step_start",
                "step": "load_case_motor",
                "message": f"正在加载案例电机 (ID={case_motor_id})...",
            }
        else:
            yield {
                "type": "step_start",
                "step": "load_case_motor",
                "message": "需求分析师开始解析客户需求...",
            }

        # 2. 主循环: 轮询 progress 和 node_done
        while True:
            # 2a. 检查取消标志 (v1.03.02)
            try:
                from progress_hub import progress_hub
                if progress_hub.is_cancelled():
                    yield {"type": "progress", "step": "cancel", "message": "收到终止信号, 正在停止工作流...", "level": "warn"}
                    # ★ R8 (2026-09-23) 措施2: _worker 线程已绑定本作业, 求解层
                    #   (rmxprt_tools 轮询)收到取消后会树杀 worker+AEDT 再返回,
                    #   故这里有界等待其收尾(原先仅 join(3s) 即遗弃, worker 与
                    #   AEDT 继续跑满超时)。每步 yield 保持 SSE 活性; 超上限则
                    #   放行返回(AEDT 已在 ~2s 内被树杀, 残余仅图收尾)。
                    _waited = 0.0
                    while thread.is_alive() and _waited < 15.0:
                        thread.join(timeout=2.0)
                        _waited += 2.0
                        if thread.is_alive():
                            yield {"type": "progress", "step": "cancel",
                                   "message": "正在终止求解子进程(AEDT)... %ds" % int(_waited),
                                   "level": "info"}
                    yield {"type": "fatal", "error": "用户已终止工作流"}
                    return
            except ImportError:
                pass

            # 2b. 先 drain 进度队列 (非阻塞) — 使用本工作流的局部队列
            drained_any = False
            while not wf_progress_q.empty():
                try:
                    progress_event = wf_progress_q.get_nowait()
                    yield progress_event
                    drained_any = True
                except queue.Empty:
                    break

            # 2c. 等待节点完成事件 (带超时, 以便继续轮询进度)
            try:
                item = result_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            if item[0] == "finished":
                break
            elif item[0] == "error":
                yield {"type": "fatal", "error": item[2]}
                break
            elif item[0] == "node_done":
                node_name = item[1]
                state_update = item[2]

                # drain 在节点执行期间积累的进度事件
                while not wf_progress_q.empty():
                    try:
                        yield wf_progress_q.get_nowait()
                    except queue.Empty:
                        break

                # 推送 step_done
                state_update["step"] = node_name
                state_update["status"] = STEP_STATUS_MAP.get(node_name, node_name)
                state_update["type"] = "step_done"
                yield state_update

                # 推送下一步的 step_start
                next_steps = self._next_steps_after(node_name, state_update)
                for ns in next_steps:
                    yield {
                        "type": "step_start",
                        "step": ns["step"],
                        "message": ns["message"],
                    }
                    # v2.05.03: 人工确认节点额外推送 human_input_required 事件
                    if ns["step"] == "human_approval":
                        checkpoint_id = state_update.get("hc_checkpoint_id", "")
                        cp_info = hc.get_decision_context(checkpoint_id) if checkpoint_id else {}
                        yield {
                            "type": "human_input_required",
                            "step": "human_approval",
                            "checkpoint_id": checkpoint_id,
                            "title": cp_info.get("title", "人工确认"),
                            "description": cp_info.get("description", ""),
                            "context": cp_info.get("context", {}),
                            "timeout_seconds": cp_info.get("timeout_seconds", 86400),
                            "default_action": cp_info.get("default_action", "auto_approve"),
                            "options": cp_info.get("options", sorted(hc.VALID_DECISIONS)),
                        }

        # 3. drain 剩余进度事件
        while not wf_progress_q.empty():
            try:
                yield wf_progress_q.get_nowait()
            except queue.Empty:
                break

        # 确保后台线程结束
        thread.join(timeout=5)

        # v4.00.07: 关闭本工作流的 SQLite 连接
        try:
            local_conn.close()
        except Exception:
            pass

        # v4.00.07: 生成 timing summary 并通过事件传递给 app.py
        # (app.py 不再直接调用 generate_timing_summary, 避免并发时读取错误的 tracker)
        try:
            _timing = self._generate_timing_summary_for(wf_cost_tracker)
        except Exception:
            _timing = {}
        yield {"type": "_timing", "timing": _timing}

def _rebalance_candidate_meta_for_exhausted(state: dict, cand_meta: dict) -> dict:
    """★ P1-4 (2026-10-11): 杠杆耗尽「换轨」配额重分配 —— 纯函数, 便于离线回归。

    被判定耗尽的锁定轨(bypass_b1/bypass_b2), 把至多 SHIFT 组配额让给 bypass_b3
    (b3 全自由, 真实几何抬 SF 杠杆 set_slot_depth 落点); 未耗尽轨不动 ⇒ 零回归。
    """
    if not isinstance(state, dict) or not isinstance(cand_meta, dict):
        return cand_meta
    _ex = state.get("rail_exhausted") or {}
    if not _ex:
        return cand_meta
    _shift = int(os.environ.get("QG_LEVER_EXHAUST_SHIFT", "200") or 200)
    _b3 = int(cand_meta.get("bypass_b3", 0) or 0)
    for _rail in ("bypass_b1", "bypass_b2"):
        if _ex.get(_rail):
            _take = min(_shift, int(cand_meta.get(_rail, 0) or 0))
            if _take > 0:
                cand_meta[_rail] = int(cand_meta[_rail]) - _take
                _b3 += _take
    cand_meta["bypass_b3"] = _b3
    return cand_meta