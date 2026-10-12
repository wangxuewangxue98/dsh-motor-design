# -*- coding: utf-8 -*-
"""
V2.1 (#695) 混合漏斗 — 聚合多轨候选方案为统一矩阵
================================================

将方案生成阶段的多条来源轨道汇聚为一个统一候选矩阵, 供后续 L1/L2 求解
与正侧分层核验(verify_engine)消费:

  · 主线 A   (main_a)         : 冲片几何全固定 + 磁路法缩放叠长  (#694)
  · 旁路 B1  (bypass_b1)      : 固定定子外径/内径/轴径, 激进性能 (#697)
  · 旁路 B2  (bypass_b2)      : 固定定子外径/轴径, 低成本        (#697)
  · 旁路 B3  (bypass_b3)      : 无固定项(全自由), 高可靠        (#697)
  · 兜底     (fallback)       : 纯物理聚焦扫描 (无种子时)        (#692/#698)

设计要点:
  - 纯加法: 仅聚合, 不改写各轨道方案自身字段(source_path/_seed/_physics)。
  - 向后兼容: 无种子时整体回落 fallback 通用扫描, 行为与旧版一致。
  - 可测试: 不依赖重型求解器/LLM, 直接返回 dict。
"""
import os
import sys
import statistics

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "Scripts"),):
    _ap = os.path.abspath(_p)
    if os.path.isdir(_ap) and _ap not in sys.path:
        sys.path.insert(0, _ap)


def build_candidate_matrix(specs: dict = None,
                           seeds: list = None,
                           design_hints: dict = None,
                           n_main_a: int = 50,
                           n_bypass: int = 10,
                           b3_count: int = 30,
                           enable_main_a: bool = True,
                           enable_bypass: bool = True,
                           main_a_seeds: list = None,
                           enable_platform_optim: bool = False,
                           enable_convergence: bool = False) -> dict:
    """
    聚合多轨候选方案。

    参数:
      specs          : 需求规格 dict
      seeds          : #693 种子智能提取产出的种子 list (11 几何键齐全)
      design_hints   : 方案构思约束 {emphasis, avoid, ...}
      n_main_a       : 主线 A 目标方案数
      n_bypass       : 每条旁路轨道目标方案数 (实际每轨 ~n_bypass)
      b3_count       : B3 轨道目标方案数 (v4.04: 参考「全域扫描办法」, 默认 30)
      enable_main_a  : 是否启用主线 A
      enable_bypass  : 是否启用三层旁路
      main_a_seeds   : H35 (2026-10-07) 主线A 专用种子池 (冲片权威分区产物):
                       None = 未启用分区, 沿用 seeds (原语义, 零回归);
                       非空 list = main_a 只消费权威种子 (B 轨仍吃全量 seeds);
                       空 list = 无权威种子 ⇒ D2: 跳过 main_a, 其配额平分转移
                       给 B1/B2 (共用 n_bypass, 每轨 +n_main_a/2), B3 不变。

    返回:
      {
        "candidates": [ ... 参数字典 ... ],
        "meta": {"main_a": int, "bypass_b1": int, "bypass_b2": int,
                 "bypass_b3": int, "fallback": int},
      }
    """
    specs = specs or {}
    from motor_tools import (generate_main_a_designs,
                             generate_bypass_schemes,
                             generate_param_matrix,
                             _apply_diversity_and_mutation)

    candidates = []
    meta = {"main_a": 0, "bypass_b1": 0,
            "bypass_b2": 0, "bypass_b3": 0, "fallback": 0}
    _from_fallback = False

    # ── H35 (2026-10-07): main_a 种子池分区 + D2 配额转移 ──
    #   main_a_seeds=None → 原语义 (main_a 与 B 轨共用 seeds, 零回归);
    #   main_a_seeds=[]   → 无权威种子: 跳过 main_a, n_main_a 平分进 B1/B2
    #                       (共用 n_bypass ⇒ 每轨 +n_main_a//2), B3 配额不变;
    #   main_a_seeds=[..] → main_a 只吃权威种子, B 轨仍吃全量 seeds。
    _ma_pool = seeds if main_a_seeds is None else main_a_seeds
    _n_bypass_eff = n_bypass
    if (enable_main_a and main_a_seeds is not None
            and not main_a_seeds and seeds):
        _n_bypass_eff = n_bypass + max(1, int(n_main_a) // 2)
        try:
            import logging as _lg
            _lg.getLogger("scheme_engine").info(
                "[H35·D2] 无权威冲片种子 ⇒ main_a 配额 %s 转移: "
                "B1/B2 每轨 n_bypass %s → %s (B3 不变)",
                n_main_a, n_bypass, _n_bypass_eff)
        except Exception:
            pass

    if enable_main_a and _ma_pool:
        _ma = generate_main_a_designs(_ma_pool, specs, n_main_a, design_hints)
        candidates += _ma
        meta["main_a"] = len(_ma)

    if enable_bypass and seeds:
        for _b in generate_bypass_schemes(seeds, specs, _n_bypass_eff, design_hints,
                                          b3_count=b3_count):
            candidates.append(_b)
            meta[_b["source_path"]] += 1

    if not candidates:
        # 无种子 (或所有轨道均未产出): 纯物理兜底聚焦扫描
        _fb, _ = generate_param_matrix(specs, n_main_a, design_hints=design_hints)
        candidates += _fb
        meta["fallback"] = len(_fb)
        _from_fallback = True

    # v4.02.16 (Fix4): 聚合出口统一注入额定功率与频率 (覆盖 main_a/bypass/fallback 全轨)
    #   根因: 主路径 main_a/bypass 的 _main_a_make_design / _bypass_make_design
    #         (motor_tools.py:805 / :930) 构造候选字典时从不写 'power'/'frequency' 键;
    #         唯一注入点 focused_scan Fix3 (physics_kernel.py:1274-1289) 仅在上述
    #         "无种子兜底" 分支生效 → 有种子时(AI 团队常态)主路径候选全缺 power。
    #   后果: 候选到达 _rmxprt_venv_worker.modify_aedt_text 时 params.get('power') 为 None
    #         → 不改写 Setup1.RatedOutputPower → RMxprt 求解模板 yz200-6.aedt 自带默认
    #         额定功率 (11kW/50Hz) → 19 组方案扭矩恒为 ~70.7N.m, 与几何尺寸脱钩
    #         (实测: 需求 75kW 应得 484N.m, 实得 70.7N.m)。
    #   修复: 在聚合出口单点注入, 仅在缺失时写入 (不覆盖各轨道已有的显式值,
    #         如 focused_scan 兜底路径已注入的 power/frequency), 保持纯加法语义。
    _inj_pw = specs.get("power_kw")
    _inj_fq = specs.get("frequency_hz")
    for _d in candidates:
        if _inj_pw is not None and _d.get("power") is None:
            _d["power"] = _inj_pw
        if _inj_fq is not None and _d.get("frequency") is None:
            _d["frequency"] = _inj_fq

    result = {"candidates": candidates, "meta": meta}

    # §2.3.2 优化版 — 平台通用优化 (v4.02.20, 纯 Python, 不触 schema, 默认关闭)
    #   主轴: 优先统一 5 套公共冲片 (跨候选聚类→取最大 top_platforms 簇→每簇吸附到统一
    #         公共冲片); §2.3.3: 每平台代表设计做 3 向转子分岔 (cost/efficiency/balance)。
    #   默认 enable_platform_optim=False, 保持与旧版生成链路 100% 兼容; 置 True 即启用
    #   收敛 (50→top_platforms×3), 不改变任何候选已有字段 (纯加法覆写 D1/Di1/L + 注入转子分支键)。
    if enable_platform_optim:
        _opt = unify_platform_and_bifurcate(candidates, specs)
        _opt["meta"].update(meta)
        result = _opt

    # U9 — 方案收敛 (v4.02.20, 纯 Python, 不触 schema, 默认关闭)
    #   在(可能已平台优化的)候选池上做 50→3 优先级收敛 (A 成本 / B 效率 / C 扭矩),
    #   复用 U7 平台聚类 + U6 多目标排序 + U11 敏感度复核(可选注入 solve_fn)。
    #   默认 enable_convergence=False, 运行态 v4.02.20 不变; 置 True 即输出收敛三方案。
    if enable_convergence:
        _conv = converge_solutions(result.get("candidates", []), specs)
        _conv["meta"].update(result.get("meta", {}))
        result = _conv

    # v4.02.34 (#A): 种子路径(main_a+bypass) 接入多样性度量与变异展开
    #   根因: compute_diversity_metrics / diversity_mutate 此前仅由 generate_param_matrix
    #   (无种子兜底路径) 调用; AI 团队常态走 main_a+bypass 种子路径, build_candidate_matrix
    #   从不更新 LAST_DIVERSITY_METRICS → orchestrator 披露的 [多样性] 覆盖度/创新性/变异方案
    #   恒为 0/"未生成", 且矩阵塌陷时无法触发变异展开破局。
    #   修复: 聚合出口(平台优化/收敛之后)对种子路径候选统一施加 _apply_diversity_and_mutation
    #   (compute → 若 clustered 则 mutation → 重算 → 更新 LAST_DIVERSITY_METRICS)。
    #   无种子兜底路径已由 generate_param_matrix 内部完成, 跳过避免双重变异。
    #   注意: 变异原地扰动连续维度(±jitter), 会流入后续 L0 裁剪 / L1 求解, 属预期行为。
    if not _from_fallback:
        try:
            import logging as _logging
            _applied = _apply_diversity_and_mutation(
                specs, result.get("candidates", []), enable_diversity=True)
            if _applied is not None:
                result["candidates"] = _applied
        except Exception as _de:  # 多样性度量属非关键增强, 失败不影响候选产出
            _logging.getLogger("scheme_engine").warning(
                "[scheme_engine] 多样性度量异常(忽略): %s", _de)

    return result


# ===========================================================================
# §2.3.2 / §2.3.3 优化版 — 平台通用与转子分岔 (v4.02.20, 纯 Python, 不触 schema)
# ---------------------------------------------------------------------------
# 参考: Motor-AI_方案生成优化建议.md §2.3.2 / §2.3.3 优化版
#   主轴: 优先统一 5 套公共冲片 (聚类→选最大簇→标准化档位)
#   转子分岔 3 方向: cost(铝深槽单笼) / efficiency(铜双笼) / balance(铜铝复合笼)
# 字段落点已就绪 (无需新增数据模型列):
#   app.py:5825(rotor_double_cage) / :5828(rotor_deep_bar) / :5959(rotor_material)
#   case_motor_agent.py:1292-1293(low) / :1330(high)
# 候选 dict 实际携带 stator_od/stator_id/core_length 扁平键 (motor_tools.py:907/1254/1358),
# 且不携带转子分岔键 → 分岔为新增键, 不影响既有字段。
# ===========================================================================

# 公共冲片标准档位阶梯 (单位 mm): D1 外径 10 档, Di1 内径 5 档, L 叠长 5 档
_STAMPING_BINS = {
    "stator_od": 10.0,
    "stator_id": 5.0,
    "core_length": 5.0,
}

# 转子分岔 3 方向候选枚举 (measures 不限于铸铝深槽单笼/铸铜双笼, 含创新平衡轴)
ROTOR_BRANCHES = {
    "cost": {            # 方向一·成本优先轴: 铸铝深槽单笼 + 变截面深槽几何创新(零材料成本)
        "rotor_material": "aluminum",
        "rotor_deep_bar": "是",
        "rotor_double_cage": "否",
        "rotor_slot_depth_ratio": 6.0,
        "priority": "cost",
    },
    "efficiency": {      # 方向二·效率优先轴: 铸铜双笼 (上笼高阻 KR≈5.5 / KX≈0.75)
        "rotor_material": "copper",
        "rotor_deep_bar": "否",
        "rotor_double_cage": "是",
        "rotor_slot_depth_ratio": 6.0,
        "KR_target": 5.5,
        "KX_st": 0.75,
        "priority": "efficiency",
    },
    "balance": {         # 方向三·创新平衡轴: 铜铝复合笼 (铜端环+铝导条), 以~50%成本取铜笼60-70%效率
        "rotor_material": "cu_al_composite",
        "rotor_deep_bar": "是",
        "rotor_double_cage": "否",
        "rotor_slot_depth_ratio": 6.0,
        "priority": "balance",
    },
}


def _bin_key(design, bins=_STAMPING_BINS):
    """把候选冲片几何映射到标准档位 key (D1//10*10, Di1//5*5, L//5*5)。"""
    key = []
    for k, step in bins.items():
        v = design.get(k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = None
        if v is None:
            key.append(None)
        else:
            key.append(round(v / step) * step)
    return tuple(key)


def cluster_public_stamping(designs, bins=_STAMPING_BINS):
    """按 (D1, Di1, L) 标准档位聚类候选, 返回 {key: [design,...]}。"""
    clusters = {}
    for d in designs:
        key = _bin_key(d, bins)
        clusters.setdefault(key, []).append(d)
    return clusters


def select_best_cluster(clusters):
    """选最大簇 (可行解最多) 作为公共冲片平台。"""
    if not clusters:
        return None, []
    best_key = max(clusters.items(), key=lambda kv: len(kv[1]))[0]
    return best_key, clusters[best_key]


def unified_stamping_of(cluster_designs, bins=_STAMPING_BINS):
    """取簇内各几何键的中位数作为统一公共冲片基准 (稳健于离群)。"""
    rep = {}
    for k in bins:
        vals = []
        for d in cluster_designs:
            try:
                v = float(d.get(k))
            except (TypeError, ValueError):
                continue
            vals.append(v)
        if vals:
            rep[k] = round(statistics.median(vals), 1)
    return rep


def snap_to_public_stamping(design, stamping):
    """将候选冲片几何吸附到统一公共冲片 (覆写 D1/Di1/L, 标 _public_stamping)。

    §2.3.2 修正 (v4.02.21): 吸附 D1/Di1/L 后, 同步重定心转子外径 rotor_od 使气隙物理自洽
        rotor_od = 新定子内径 − 2×air_gap
    修复灰度验证发现的 U7 气隙不自洽缺陷: 同平台 stator_id 有散布时, 仅重定心定子内径
    而转子外径未同步, 导致 定子内径−转子外径≠2×气隙 被 U5 geometry_self_check 捕获。
    仅当 stamping 含 stator_id 且候选含合法 air_gap 时重算; 否则 fail-open 不改动。
    """
    out = dict(design)
    for k, v in stamping.items():
        out[k] = v
    if "stator_id" in stamping and "air_gap" in out:
        try:
            sid = float(out["stator_id"])
            gap = float(out["air_gap"])
            if gap > 0:
                out["rotor_od"] = round(sid - 2.0 * gap, 1)
        except (TypeError, ValueError):
            pass
    out["_public_stamping"] = True
    return out


def bifurcate_rotor(seed_design, branches=ROTOR_BRANCHES):
    """同一公共冲片下生成 3 方向转子分岔候选 (cost/efficiency/balance)。"""
    out = []
    for name, fields in branches.items():
        variant = dict(seed_design)
        variant.update(fields)
        variant["rotor_branch"] = name
        _base = seed_design.get("source_path") or "platform"
        variant["source_path"] = f"{_base}/rotor_{name}"
        out.append(variant)
    return out


def unify_platform_and_bifurcate(candidates, specs=None,
                                 top_platforms=5, reps_per_platform=1):
    """
    §2.3.2 主轴 + §2.3.3 三向分岔 的聚合后处理:
      1) 跨候选聚类 → 取最大的 top_platforms 簇 (即"优先统一 5 套公共冲片")
      2) 每簇内几何吸附到统一公共冲片 (中位数基准)
      3) 每平台取 top reps_per_platform 代表设计 → 各自 3 向转子分岔
    返回 {"candidates": [...], "meta": {...}}。纯加法, 不改原候选其它字段。
    """
    if not candidates:
        return {"candidates": [], "meta": {}}
    clusters = cluster_public_stamping(candidates)
    ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
    top = ranked[:max(1, min(top_platforms, len(ranked)))]
    branched = []
    stampings = []
    for _key, members in top:
        stamping = unified_stamping_of(members)
        stampings.append(stamping)
        unified = [snap_to_public_stamping(d, stamping) for d in members]
        reps = unified[:max(1, min(reps_per_platform, len(unified)))]
        for r in reps:
            branched += bifurcate_rotor(r)
    return {
        "candidates": branched,
        "meta": {
            "platform_optim": True,
            "n_platforms": len(top),
            "n_clusters": len(clusters),
            "public_stampings": stampings,
            "rotor_branches": list(ROTOR_BRANCHES.keys()),
            "n_output": len(branched),
        },
    }


# ===========================================================================
# 四章 · 智能体职责强化 — U1/U3/U4/U5/U6/U9/U11/U13 纯函数库
# ---------------------------------------------------------------------------
# 参考: Motor-AI_方案生成优化建议.md 第四章 §4.3 伪代码
# 设计原则:
#   · 全部纯函数 (无 AEDT / LLM 依赖), 可在沙箱/单测中直接调用;
#   · opt-in: 仅被 build_candidate_matrix(enable_convergence=...) 或外部 agent
#     显式调用时生效, 不触 schema, 运行态 v4.02.20 完全兼容;
#   · 复用既有能力避免重复:
#       - U6 综合排序复用 motor_tools._rank_score (#988, v4.02.13)
#       - U5 漏斗首层复用 verify_engine.geometry_self_check (分级 1-5 通用 + B1-B3 追加)
# ===========================================================================


# ---------------------------------------------------------------------------
# U1 (§4.3) — 硬约束清单 + 软目标优先级 (需求分析师职责)
# ---------------------------------------------------------------------------
HARD_CONSTRAINTS = [
    {"name": "效率", "limit": ">=96%", "key": "efficiency", "op": ">=", "value": 96.0, "source": "IE4 标准"},
    {"name": "起动转矩", "limit": ">=2.0 pu", "key": "T_st", "op": ">=", "value": 2.0, "source": "重载起动"},
    {"name": "起动电流", "limit": "<=7.5 pu", "key": "I_st", "op": "<=", "value": 7.5, "source": "电网约束"},
    {"name": "最大转矩", "limit": ">=2.8 pu", "key": "T_max", "op": ">=", "value": 2.8, "source": "过载能力"},
    {"name": "定子轭磁密", "limit": "<=1.45 T", "key": "B_j1", "op": "<=", "value": 1.45, "source": "饱和约束"},
    {"name": "定子齿磁密", "limit": "<=1.48 T", "key": "B_t1", "op": "<=", "value": 1.48, "source": "饱和约束"},
    # P0 (v4.02.67): 槽满率参考表对齐单一真相源默认上限 0.75
    #   (工艺分级: 通用0.75 / VPI0.76 / H级成型0.80, 见 Core.workflow_config)
    {"name": "槽满率", "limit": "<=75%", "key": "slot_fill", "op": "<=", "value": 0.75, "source": "工艺上限(通用)"},
    {"name": "绕组温升", "limit": "<=80 K", "key": "temp_rise", "op": "<=", "value": 80.0, "source": "绝缘等级"},
    {"name": "机座外表面温度", "limit": "<=150 ℃", "key": "frame_temp", "op": "<=", "value": 150.0, "source": "隔爆安全"},
]

SOFT_GOALS = [
    {"name": "成本最小化", "weight": 0.30},
    {"name": "工艺成熟度", "weight": 0.20},
    {"name": "平台通用性", "weight": 0.20},
    {"name": "效率余量", "weight": 0.15},
    {"name": "起动性能", "weight": 0.15},
]


def extract_hard_constraints(specs: dict = None) -> dict:
    """
    U1 — 从需求规格抽取硬性约束清单 + 软目标优先级。

    字典序语义: 设计空间搜索时先满足全部 hard (限定可行域), 再在可行域内按 soft 加权优化。
    需求侧可通过 specs["constraints"][key] 覆盖默认阈值 (如特殊工况要求更高效率),
    缺省沿用全局 HARD_CONSTRAINTS。

    返回: {"hard": [...], "soft": [...], "lexical_order": [hard key...]}
    """
    specs = specs or {}
    _override = specs.get("constraints") or {}
    hard = []
    for c in HARD_CONSTRAINTS:
        c2 = dict(c)
        _ov = _override.get(c["key"])
        if _ov is not None and isinstance(_ov, (int, float)):
            c2["value"] = float(_ov)
        hard.append(c2)
    return {
        "hard": hard,
        "soft": list(SOFT_GOALS),
        "lexical_order": [c["key"] for c in hard],
    }


def _cmp_value(v, op, t) -> bool:
    """标量比较辅助: op ∈ {>=, <=, >, <, ==}。"""
    if op == ">=":
        return v >= t
    if op == "<=":
        return v <= t
    if op == ">":
        return v > t
    if op == "<":
        return v < t
    if op == "==":
        return abs(v - t) < 1e-9
    return False


def check_hard_constraints(candidate: dict, constraints: list = None):
    """
    U1 — 对单方案判定硬约束满足情况。
    返回 (all_ok: bool, failed: [constraint dicts])。缺字段视为不满足 (漏斗归因)。
    """
    constraints = constraints or HARD_CONSTRAINTS
    failed = []
    for c in constraints:
        v = candidate.get(c["key"])
        if v is None:
            failed.append(c)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            failed.append(c)
            continue
        if not _cmp_value(v, c["op"], c["value"]):
            failed.append(c)
    return (len(failed) == 0, failed)


# ---------------------------------------------------------------------------
# U3 (§4.1/§4.3) — 设计变量四层分级 (方案构思师/方案生成师职责)
# ---------------------------------------------------------------------------
def layer_variables(specs: dict = None) -> dict:
    """
    U3 — 将电机设计变量分为四个层级 (量级/派生/电路/经验), 供方案生成时按层解耦搜索。
    返回字典含四级键名列表; 与方案书 §4.3 伪代码一一对应。
    """
    return {
        "level1_magnitude": ["D_i1", "L"],                     # D²L 方程决定量级
        "level2_derived": ["D₁", "h_j1", "h_s1", "b_t1"],      # 由 D_i1 派生
        "level3_circuit": ["N_s", "A_wire", "J₁", "J₂"],       # 电压方程 + 电密目标
        "level4_experience": ["δ", "Q₁/Q₂", "y", "斜槽", "a"],  # 经验公式锁定
    }


# ---------------------------------------------------------------------------
# U4 (§4.1/§4.3) — 槽配合禁忌规避 (方案构思师/方案生成师职责)
# ---------------------------------------------------------------------------
def check_slot_taboo(Q1, Q2, poles):
    """
    U4 — 检查槽配合是否避开同步附加转矩禁忌差。
    标准电机理论禁忌差 (p = 极对数 = poles//2): {0, ±2p, ±2p±1, ±4p}。
    返回 (ok: bool, diff: int, taboo_set: set)。缺参或非法返回 (False, None, set())。
    """
    try:
        Q1 = int(Q1); Q2 = int(Q2); p = int(poles) // 2
    except (TypeError, ValueError):
        return (False, None, set())
    if p <= 0:
        return (False, None, set())
    diff = Q1 - Q2
    taboo = {
        0,
        2 * p, -2 * p,
        2 * p + 1, 2 * p - 1, -(2 * p + 1), -(2 * p - 1),
        4 * p, -4 * p,
    }
    return (diff not in taboo, diff, taboo)


# ---------------------------------------------------------------------------
# U5 (§4.3) — 约束漏斗分析 (逆向评审师职责), 首层复用 geometry_self_check
# ---------------------------------------------------------------------------
CONSTRAINT_FUNNEL = [
    {"name": "几何不可行", "key": None},
    {"name": "定子轭磁密超限", "key": "B_j1"},
    {"name": "定子齿磁密超限", "key": "B_t1"},
    {"name": "效率不足", "key": "efficiency"},
    {"name": "起动转矩不足", "key": "T_st"},
    {"name": "起动电流超限", "key": "I_st"},
    {"name": "最大转矩不足", "key": "T_max"},
]

_FUNNEL_LIMITS = {
    "B_j1": (1.45, "<="),
    "B_t1": (1.48, "<="),
    "efficiency": (96.0, ">="),
    "T_st": (2.0, ">="),
    "I_st": (7.5, "<="),
    "T_max": (2.8, ">="),
}


def analyze_constraint_funnel(solutions: list, specs: dict = None, config: dict = None) -> dict:
    """
    U5 — 约束漏斗: 逐方案按"首个未通过约束"归因, 统计各阶段淘汰数量。
    首层"几何不可行"复用 verify_engine.geometry_self_check (返回 (ok, reasons))。
    返回 {"stats": {stage: count}, "first_fail": [{idx,stage,reason}], "feasible": [...]}。
    """
    from Core.verify_engine import geometry_self_check
    specs = specs or {}
    stats = {s["name"]: 0 for s in CONSTRAINT_FUNNEL}
    first_fail = []
    feasible = []
    for i, sol in enumerate(solutions):
        # ── 首层: 几何自校验 (L0 硬过滤复用) ──
        try:
            _gc = geometry_self_check(sol, source_path=sol.get("source_path"), config=config)
            _geom_ok = bool(_gc[0]) if isinstance(_gc, (tuple, list)) and len(_gc) > 0 else False
        except Exception:
            _geom_ok = False
        if not _geom_ok:
            stats["几何不可行"] += 1
            first_fail.append({"idx": i, "stage": "几何不可行", "reason": "geometry_self_check failed"})
            continue
        # ── 后续层: 指标阈值 (按首个失败归因) ──
        dropped = False
        for stage in CONSTRAINT_FUNNEL[1:]:
            key = stage["key"]
            lim = _FUNNEL_LIMITS.get(key)
            if lim is None:
                continue
            v = sol.get(key)
            if v is None:
                stats[stage["name"]] += 1
                first_fail.append({"idx": i, "stage": stage["name"], "reason": f"missing {key}"})
                dropped = True
                break
            try:
                v = float(v)
            except (TypeError, ValueError):
                stats[stage["name"]] += 1
                first_fail.append({"idx": i, "stage": stage["name"], "reason": f"bad {key}"})
                dropped = True
                break
            if not _cmp_value(v, lim[1], lim[0]):
                stats[stage["name"]] += 1
                first_fail.append({"idx": i, "stage": stage["name"],
                                   "reason": f"{key}={v} {lim[1]} {lim[0]}"})
                dropped = True
                break
        if not dropped:
            feasible.append(sol)
    return {"stats": stats, "first_fail": first_fail, "feasible": feasible}


# ---------------------------------------------------------------------------
# U6 (§4.3) — 多目标排序 (评审推荐师职责), 复用 motor_tools._rank_score 兜底
# ---------------------------------------------------------------------------
def multi_objective_sort(solutions: list, specs: dict = None) -> list:
    """
    U6 — 多目标排序: score = 成本 − (η−目标)×4000, 并对优秀起动性能减分 (越低越好)。
    复用 motor_tools._rank_score 作为缺字段候选的兜底优度。
    返回按 score 升序排序的新列表 (不修改入参)。
    """
    from motor_tools import _rank_score
    specs = specs or {}
    target_eff = float(specs.get("efficiency_target", 96.0))
    ranked = []
    for sol in solutions:
        cost = sol.get("material_cost")
        eff = sol.get("efficiency")
        if isinstance(cost, (int, float)) and isinstance(eff, (int, float)):
            score = cost - (eff - target_eff) * 4000.0
            if isinstance(sol.get("T_st"), (int, float)) and sol["T_st"] >= 2.3:
                score -= 50          # 起动性能优异加分
            if isinstance(sol.get("I_st"), (int, float)) and sol["I_st"] <= 6.5:
                score -= 30          # 起动电流低加分
        else:
            # 缺成本/效率 → 用综合排序分取负向, 保持"低分优"语义
            score = -_rank_score(sol)
        ranked.append((score, sol))
    ranked.sort(key=lambda x: x[0])
    return [s for _, s in ranked]


# ---------------------------------------------------------------------------
# U11 (§4.3) — OAT 单因素敏感度分析 (方案生成师/沙盘推演师/魔鬼代言人职责)
# ---------------------------------------------------------------------------
def sensitivity_analysis(scheme: dict, variables: list, solve_fn, perturb: float = 0.10) -> list:
    """
    U11 — 对给定方案做 OAT (One-At-a-Time) ±perturb 单因素扰动, 识别主控变量。
    solve_fn(scheme_dict) -> dict, 须含 'efficiency' 键 (可注入真实求解器或 mock)。
    返回按 |S10| 降序排列的列表 [(var, {S10, eff_range, impact, ...}), ...]。
    S10 = (eff_plus - eff_minus) / 2  (效率对 ±perturb 扰动的半量程灵敏度)。
    """
    base = dict(scheme)
    results = {}
    for var in variables:
        v0 = base.get(var)
        if not isinstance(v0, (int, float)):
            results[var] = {"S10": 0.0, "eff_range": "n/a", "impact": "n/a",
                            "note": "non-numeric"}
            continue
        plus = dict(base); plus[var] = v0 * (1.0 + perturb)
        minus = dict(base); minus[var] = v0 * (1.0 - perturb)
        try:
            eff_plus = float(solve_fn(plus).get("efficiency"))
            eff_minus = float(solve_fn(minus).get("efficiency"))
        except Exception:
            results[var] = {"S10": 0.0, "eff_range": "n/a", "impact": "n/a",
                            "note": "solve failed"}
            continue
        s10 = (eff_plus - eff_minus) / 2.0
        impact = "强" if abs(s10) > 0.1 else ("中" if abs(s10) > 0.05 else "弱")
        results[var] = {
            "S10": round(s10, 4),
            "eff_range": f"{eff_minus:.2f}~{eff_plus:.2f}%",
            "impact": impact,
        }
    ordered = sorted(results.items(), key=lambda kv: abs(kv[1]["S10"]), reverse=True)
    return ordered


# ---------------------------------------------------------------------------
# U9 (§3.3) — 方案收敛机制 (50 → 3 优先级方案), 复用 U7 平台聚类 + U6 排序 + U11 复核
# ---------------------------------------------------------------------------
def converge_solutions(candidates: list, specs: dict = None,
                       top_n_platforms: int = 3, solve_fn=None) -> dict:
    """
    U9 — 从大量候选收敛到 3 组优先级方案 (A 成本优先 / B 效率优先 / C 扭矩优先)。
    五步: ①聚类冲片 ②选 top_n 平台 ③分岔转子(复用 U7 bifurcate_rotor)
          ④敏感度复核(可选 solve_fn) ⑤输出 3 组方案 + 共享平台。
    纯加法, 不改原候选字段; 缺指标字段的候选在该优先级维度被跳过。
    """
    specs = specs or {}
    if not candidates:
        return {"candidates": [], "meta": {"converge": True, "n_output": 0},
                "scheme_a": None, "scheme_b": None, "scheme_c": None,
                "shared_platforms": []}
    # ① 聚类 + ② 选平台
    clusters = cluster_public_stamping(candidates)
    ranked = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
    top = ranked[:max(1, min(top_n_platforms, len(ranked)))]
    platform_reps = []
    stampings = []
    for _key, members in top:
        stamping = unified_stamping_of(members)
        stampings.append(stamping)
        unified = [snap_to_public_stamping(d, stamping) for d in members]
        # ③ 分岔转子 (每平台取"最优代表"做 3 向分岔, 避免首个代表恰好为劣解)
        rep = max(unified, key=lambda d: float(d.get("efficiency") or -1e9))
        branched = bifurcate_rotor(rep)
        platform_reps.append({"platform": stamping, "branches": branched})
    # 汇聚分岔候选作为收敛池
    pool = []
    for pr in platform_reps:
        pool += pr["branches"]
    # 用 U6 排序作为整体优度参考
    pool_sorted = multi_objective_sort(pool, specs)
    # ⑤ 三优先级方案选择 (缺字段维度跳过)
    scheme_a = _pick(pool, key="material_cost", prefer="min")   # 成本优先
    scheme_b = _pick(pool, key="efficiency", prefer="max")      # 效率优先
    scheme_c = _pick(pool, key="T_st", prefer="max")            # 扭矩优先
    # ④ 敏感度复核 (若提供 solve_fn)
    sensitivity = None
    if solve_fn is not None and scheme_b is not None:
        try:
            _lv = layer_variables(specs)
            _vars = _lv["level1_magnitude"] + _lv["level3_circuit"]
            sensitivity = sensitivity_analysis(scheme_b, _vars, solve_fn)
        except Exception:
            sensitivity = None
    return {
        "candidates": pool_sorted,
        "meta": {
            "converge": True,
            "n_platforms": len(top),
            "n_clusters": len(clusters),
            "public_stampings": stampings,
            "rotor_branches": list(ROTOR_BRANCHES.keys()),
            "n_output": len(pool_sorted),
        },
        "scheme_a": scheme_a,    # 成本优先
        "scheme_b": scheme_b,    # 效率优先
        "scheme_c": scheme_c,    # 扭矩优先
        "shared_platforms": stampings,
        "sensitivity_review": sensitivity,
    }


def _pick(pool, key, prefer="min"):
    """在 pool 中按 key 选取 prefer=min/max 的候选 (缺字段跳过), 返回副本或 None。"""
    best = None
    best_v = None
    for sol in pool:
        v = sol.get(key)
        if not isinstance(v, (int, float)):
            continue
        if best_v is None:
            best = sol; best_v = v
        else:
            if (prefer == "min" and v < best_v) or (prefer == "max" and v > best_v):
                best = sol; best_v = v
    return dict(best) if best is not None else None


# ---------------------------------------------------------------------------
# M3 (20260901 改进措施) — TOP10 结构化推荐评估 (评审推荐师职责)
# ---------------------------------------------------------------------------
def analyze_top10_recommendation(results: list, specs: dict = None) -> dict:
    """
    M3 — 对 TOP10 (或任意候选列表) 逐方案做"硬约束达标评分", 产出结构化推荐字典。

    设计意图:
      · 让"是否给出推荐方案"从 review_agent 的自由文本 (易被 M1 否定短语漏检/误检)
        升级为可机器校验的结构化结论, 供 review_agent 与 quality_gate 共同复用;
      · 纯函数 (无 AEDT / LLM 依赖), 沙箱/单测可直接调用;
      · 向后兼容: 仅当被显式调用时生效, 运行态 v4.02.20 行为零影响。

    参数:
      results : 候选方案 list (元素为字段字典; 建议按效率降序, 但函数内部不依赖顺序)
      specs   : 需求规格 dict (用于 extract_hard_constraints 覆盖阈值)

    返回 (结构化推荐字典):
      {
        "recommended"      : bool   # 是否存在满足"全部硬约束"的方案
        "index"            : int|None  # 推荐方案在 results 中的原始下标 (无则 None)
        "best_effort_index": int|None  # 相对最优 (达标项数最多, 效率优先) 下标
        "scores"           : [ {...per-scheme...} ]  # 与 results 等长, 含原始下标
        "reasons"          : [ str ]  # 人类可读推荐/未推荐原因
        "constraints_total": int
      }
      per-scheme 评分字段:
        index / passed / n_pass / total / ratio / failed_keys / efficiency / source_path
    """
    specs = specs or {}
    _hc = extract_hard_constraints(specs).get("hard") or HARD_CONSTRAINTS
    total = len(_hc)

    scores = []
    for idx, sol in enumerate(results or []):
        sol = sol or {}
        all_ok, failed = check_hard_constraints(sol, _hc)
        failed_keys = [c.get("key") for c in failed]
        n_pass = total - len(failed)
        ratio = (n_pass / total) if total else 0.0
        eff = sol.get("efficiency")
        scores.append({
            "index": idx,
            "passed": bool(all_ok),
            "n_pass": n_pass,
            "total": total,
            "ratio": round(ratio, 3),
            "failed_keys": failed_keys,
            "efficiency": eff,
            "source_path": sol.get("source_path"),
        })

    # 推荐判定: 存在"全通过"方案 → 取效率最高者; 否则无推荐
    recommended = False
    rec_index = None
    rec_score = None
    _passing = [s for s in scores if s["passed"]]
    if _passing:
        _passing.sort(key=lambda x: float(x["efficiency"] or -1e9), reverse=True)
        rec_score = _passing[0]
        recommended = True
        rec_index = rec_score["index"]

    # 相对最优 (即便无全通过方案, 也给出达标项数最多者以免评审失语)
    best_effort_index = None
    if scores:
        best_effort_index = max(
            scores,
            key=lambda x: (x["ratio"], float(x["efficiency"] or -1e9))
        )["index"]

    reasons = []
    if recommended:
        reasons.append(
            f"方案{rec_index} 满足全部 {total} 项硬约束, 推荐为首选 "
            f"(效率={rec_score['efficiency']}, 路径={rec_score['source_path']})"
        )
    else:
        if not scores:
            reasons.append("TOP10 为空, 无法给出推荐方案")
        else:
            _be = scores[best_effort_index] if best_effort_index is not None else None
            _miss = ", ".join(_be["failed_keys"]) if _be else "无"
            reasons.append(
                f"TOP10 中无方案满足全部硬约束; 相对最优方案{best_effort_index} "
                f"仅满足 {_be['n_pass'] if _be else 0}/{total} 项, 缺失: {_miss}"
            )

    return {
        "recommended": recommended,
        "index": rec_index,
        "best_effort_index": best_effort_index,
        "scores": scores,
        "reasons": reasons,
        "constraints_total": total,
    }


# ---------------------------------------------------------------------------
# U13 (§4.3) — 残余不确定性量化 (现实检验师职责)
# ---------------------------------------------------------------------------
UNCERTAINTY_SOURCES = [
    {"name": "杂散损耗取值", "impact": "η", "magnitude": "±0.2~0.3 pp",
     "mitigation": "EH-star 实测法 (GB/T 1032)"},
    {"name": "铁耗加工系数", "impact": "P_Fe", "magnitude": "±40 W (≈0.07 pp)",
     "mitigation": "实测铁耗曲线或 Bertotti 模型"},
    {"name": "谐波漏磁导近似", "impact": "X₁, X₂'", "magnitude": "±5% (I_st ±0.2 pu)",
     "mitigation": "Maxwell 2D 漏抗校核"},
    {"name": "双笼参数等效设定", "impact": "T_st, I_st", "magnitude": "需槽形设计落实",
     "mitigation": "转子槽形参数化 + 2D 瞬态场校核"},
    {"name": "趋肤效应参数法", "impact": "K_R, K_X", "magnitude": "±10%",
     "mitigation": "起动过程 2D 瞬态场 (含运动)"},
    {"name": "一维热网络", "impact": "温度", "magnitude": "±10~15 K",
     "mitigation": "Motor-CAD 热网络 + CFD"},
    {"name": "材料性能散差", "impact": "η", "magnitude": "±0.1 pp",
     "mitigation": "进厂检验 (硅钢片铁耗、铜电阻率)"},
]


def residual_uncertainty_report(specs: dict = None) -> dict:
    """U13 — 返回残余不确定性源清单 (现实检验师验证清单输入)。"""
    return {
        "sources": list(UNCERTAINTY_SOURCES),
        "count": len(UNCERTAINTY_SOURCES),
        "note": "上述为磁路法→FEA 校核前的残余不确定性, 须在正式方案定稿前逐条闭环。",
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    from Core.seed_extractor import extract_seeds as _extract_seeds

    print("=" * 70)
    print("scheme_engine 自测 — 混合漏斗聚合 (主线A + 旁路B1/B2/B3)")
    print("=" * 70)

    _specs = {"power_kw": 30, "speed_rpm": 1500, "poles": 4, "voltage_v": 380}
    _sp = _extract_seeds(_specs)
    _seeds = _sp.get("seeds") or []
    print(f"\n[种子] {len(_seeds)} 组")

    _m = build_candidate_matrix(_specs, seeds=_seeds, n_main_a=20, n_bypass=5)
    _c = _m["candidates"]
    _meta = _m["meta"]
    print(f"[混合漏斗] 候选 {len(_c)} 组: {_meta}")
    assert _c, "混合漏斗未产出候选"
    _paths = set(d.get("source_path") for d in _c)
    print(f"[来源分布] {sorted(_paths)}")

    # 无种子 → 兜底
    _m2 = build_candidate_matrix(_specs, seeds=None, n_main_a=10)
    print(f"[无种子兜底] 候选 {len(_m2['candidates'])} 组, 兜底={_m2['meta']['fallback']}")
    assert _m2["meta"]["fallback"] > 0
    print("\n✅ scheme_engine 自测通过")
