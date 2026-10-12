# -*- coding: utf-8 -*-
"""
运营数据飞轮引擎 (Operation Flywheel) v2.05.02
================================================
扩展效果飞轮(采纳/拒绝)之外的第二条链路: 效率/成本/质量反馈

双环架构:
  效果飞轮 (flywheel.py): 采纳/拒绝 → 技能模板进化
  运营飞轮 (本模块):     效率/成本/质量 → 智能体自我进化

六大进化机制:
  1. 需求分析师: 学习追问策略 (缺失参数+领域关键词 → 强制追问)
  2. 方案生成师: 学习高效参数域 (采纳分布→更新范围 + 低效区间剔除 + 超时降量)
  3. AI调用策略: 学习本地/云端路由 (连续5次成本比>3×且质量提升<5% → 切本地)
  4. 评审推荐师: 学习报告结构优化 (决策耗时→关键指标前置/可视化/魔鬼代言人提示词)
  5. 决策界面:   学习信息优先级 (高频修改参数→提升位置 / 常通过参数→自动折叠)
  6. 阈值告警:   连续低质量/低效率/耗时增长 → 推送改进建议

闭环: 进化规则自动注入各智能体 (requirement_agent/motor_tools/orchestrator/review_agent)
"""
import json
import os
import sqlite3
import math
from datetime import datetime
from collections import defaultdict
from typing import Optional

DB_PATH = r"D:\MotorDesign\KnowledgeBase\designs.db"
EVOLUTION_RULES_PATH = r"D:\MotorDesign\Knowledge\evolution_rules.json"
FORCED_QUESTIONS_PATH = r"D:\MotorDesign\Knowledge\forced_questions.json"
PARAM_DOMAIN_PATH = r"D:\MotorDesign\Knowledge\efficient_param_domain.json"
REPORT_STRUCTURE_PATH = r"D:\MotorDesign\Knowledge\report_structure_rules.json"
UI_PRIORITY_PATH = r"D:\MotorDesign\Knowledge\ui_priority_rules.json"

# ── 阈值常量 ──────────────────────────────────────────────────
QUALITY_THRESHOLD = 70          # 质量评分阈值
QUALITY_CONSECUTIVE_TRIGGER = 5  # 连续N次低质量触发重训
EFFICIENCY_THRESHOLD = 50       # 方案有效率阈值
EFFICIENCY_CONSECUTIVE_TRIGGER = 3  # 连续N次低效率触发调整
COST_RATIO_THRESHOLD = 3.0      # 云端/本地成本比阈值
QUALITY_GAIN_THRESHOLD = 0.05   # 质量提升阈值 (5%)
DECISION_TIME_GROWTH_TRIGGER = 3  # 决策耗时连续增长次数

# v2.05.02 新增阈值
ROUTING_CONSECUTIVE_TRIGGER = 5    # AI路由连续N次满足条件才切换
LOW_EFFICIENCY_INTERVAL_THRESHOLD = 40  # 参数区间有效率低于此值→剔除
DECISION_TIME_LONG_THRESHOLD = 1800     # 决策耗时>30分钟(秒)→报告结构优化
UI_MODIFY_FREQ_THRESHOLD = 3            # 参数被修改≥N次→提升到界面顶部
UI_PASS_FREQ_THRESHOLD = 5              # 参数被直接通过≥N次→自动折叠
DEVILS_LOW_QUALITY_THRESHOLD = 60       # 魔鬼代言人质量评分低于此值→优化提示词
QUESTION_FREQ_THRESHOLD = 0.30          # 参数缺失频率>30%→生成追问规则
DESIGN_COUNT_TIMEOUT_RATIO = 0.7        # 超时项目方案数量缩减到原值的70%


def _get_conn():
    """获取数据库连接 (自动建表)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operation_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            step TEXT NOT NULL,
            agent TEXT NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            cost_cents INTEGER DEFAULT 0,
            cost_mode TEXT DEFAULT 'local',
            quality_score REAL DEFAULT 100,
            quality_issues TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_summary (
            project_id TEXT PRIMARY KEY,
            specs_snapshot TEXT,
            total_duration_ms INTEGER,
            total_cost_cents INTEGER,
            adopted_design_id INTEGER,
            adopted_params TEXT,
            decision_time_s INTEGER,
            quality_overall REAL,
            project_type TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evolution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            agent TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            trigger_detail TEXT,
            action_taken TEXT,
            rule_data TEXT
        )
    """)
    # v2.05.02: UI交互记录 (参数修改/直接通过)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ui_interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            param_name TEXT NOT NULL,
            action TEXT NOT NULL,
            value_from TEXT,
            value_to TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    # v2.05.02: 报告阅读记录 (章节停留时间/滚动次数)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            section_name TEXT NOT NULL,
            view_time_s REAL DEFAULT 0,
            scroll_count INTEGER DEFAULT 0,
            timestamp TEXT NOT NULL
        )
    """)
    # v4.02.68 (系列三 Tier B): L0↔L1 残差账本 —— L0(经验快筛) 预测 vs L1(RMxprt) 实算
    #   的偏差逐轮累积, 供跨轮离线标定 (拟合修正项/训练轻量替代模型), 把 L0 从
    #   "粗筛" 升级为 "自改进估计器"。故障方案以 fault=1 落库但不参与统计 (防伪残差)。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS l0_residuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            round_index INTEGER DEFAULT 0,
            motor_type TEXT,
            signature TEXT,
            l0_eff REAL, l1_eff REAL, delta_eff REAL,
            l0_temp REAL, l1_temp REAL, delta_temp REAL,
            slot_fill REAL,
            stator_od REAL, stator_id REAL, core_length REAL, poles REAL,
            turns_per_coil REAL,
            wire_dia REAL, n_strands REAL, current_density REAL,
            l0_score REAL,
            source_path TEXT,
            fault INTEGER DEFAULT 0,
            fault_detail TEXT,
            -- ── v1.0 温升/损耗标定扩列（Part A2）：让每轮 AI-Team 自动累积损耗真值 ──
            -- 原表仅有效率与温升标量，无损耗拆分与工况 ⇒ 损耗模型无法标定（见标定规格 §1.1）
            -- 新增列一律 NULL 容忍：既有 8246 行不受影响，老路径写入不报错（fail-open）
            max_temp REAL,             -- L1(RMxprt) 温升真值 K，与 l1_temp 同义，冗余便于跨表对齐
            copper_loss REAL,          -- 铜损 W
            iron_loss REAL,            -- 铁损 W
            mechanical_loss REAL,      -- 机械损 W
            total_loss REAL,           -- 总损耗 W（标定主目标）
            line_current_a REAL,       -- 线电流 A（大电流域判据）
            cooling TEXT,              -- 冷却方式 IC411/IC416/液冷...
            power_kw REAL,             -- 额定功率 kW（反算损耗必需）
            voltage_v REAL,            -- 额定电压 V
            -- ── v1.0 标定补列（Part B 执行中发现的缺口）────────────────────
            -- 铜损公式 P=ρ·I²·Qs·N·l/(a·aWire) 必需槽数 Qs 与并联支路数 a，
            -- 缺任一则铜损无法独立标定（且不能用真值反推 —— 那会让拟合恒等于真值）。
            slots_stator REAL,         -- 定子槽数 Qs
            parallel_circuits REAL,    -- 并联支路数 a
            speed_rpm REAL,            -- 额定转速 rpm
            timestamp TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_l0_res_pid ON l0_residuals(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_l0_res_ts ON l0_residuals(timestamp)")
    return conn


# ═══════════════════════════════════════════════════════════════
# 1. 运营指标采集
# ═══════════════════════════════════════════════════════════════

def record_step_metric(project_id: str, step: str, agent: str,
                       duration_ms: int = 0, cost_cents: int = 0,
                       cost_mode: str = "local", quality_score: float = 100,
                       quality_issues: str = "") -> dict:
    """
    记录单个步骤的运营指标

    参数:
      project_id: 项目ID
      step: 工作流步骤 (parse_requirement / generate_designs / review_solutions / ...)
      agent: 智能体名称
      duration_ms: 耗时(毫秒)
      cost_cents: 成本(分, 1元=100分)
      cost_mode: "local" 或 "cloud"
      quality_score: 质量评分(0-100)
      quality_issues: 质量问题描述
    """
    conn = _get_conn()
    try:
        ts = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO operation_metrics "
            "(project_id, step, agent, duration_ms, cost_cents, cost_mode, "
            "quality_score, quality_issues, timestamp) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (project_id, step, agent, duration_ms, cost_cents, cost_mode,
             quality_score, quality_issues, ts)
        )
        conn.commit()
        return {"project_id": project_id, "step": step, "recorded": True}
    finally:
        conn.close()


def record_project_summary(project_id: str, specs: dict,
                           total_duration_ms: int, total_cost_cents: int,
                           adopted_design_id: int = None, adopted_params: dict = None,
                           decision_time_s: int = 0, quality_overall: float = 100,
                           project_type: str = "") -> dict:
    """记录项目完成时的汇总信息"""
    conn = _get_conn()
    try:
        # 推断项目类型
        if not project_type:
            app = (specs.get("application") or "").lower()
            if "叉车" in app or "forklift" in app:
                project_type = "叉车电机"
            elif "汽车" in app or "ev" in app.lower():
                project_type = "电动汽车电机"
            elif "泵" in app or "pump" in app.lower():
                project_type = "工业泵电机"
            elif "无人机" in app or "drone" in app.lower():
                project_type = "无人机电机"
            else:
                project_type = "通用电机"

        conn.execute(
            "INSERT OR REPLACE INTO project_summary "
            "(project_id, specs_snapshot, total_duration_ms, total_cost_cents, "
            "adopted_design_id, adopted_params, decision_time_s, quality_overall, "
            "project_type, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, json.dumps(specs, ensure_ascii=False),
             total_duration_ms, total_cost_cents,
             adopted_design_id,
             json.dumps(adopted_params or {}, ensure_ascii=False),
             decision_time_s, quality_overall, project_type,
             datetime.now().isoformat())
        )
        conn.commit()
        return {"project_id": project_id, "recorded": True, "type": project_type}
    finally:
        conn.close()


def record_ui_interaction(project_id: str, param_name: str, action: str,
                          value_from: str = "", value_to: str = "") -> dict:
    """
    记录用户在决策界面的参数交互

    参数:
      project_id: 项目ID
      param_name: 参数名 (如 efficiency_target, mounting, insulation_class)
      action: "modify" (用户修改了参数) 或 "pass" (用户直接通过未修改)
      value_from: 修改前的值 (modify时填写)
      value_to: 修改后的值 (modify时填写)
    """
    conn = _get_conn()
    try:
        ts = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO ui_interactions (project_id, param_name, action, "
            "value_from, value_to, timestamp) VALUES (?,?,?,?,?,?)",
            (project_id, param_name, action, value_from, value_to, ts)
        )
        conn.commit()
        return {"project_id": project_id, "param": param_name, "action": action, "recorded": True}
    finally:
        conn.close()


def record_report_metric(project_id: str, section_name: str,
                         view_time_s: float = 0, scroll_count: int = 0) -> dict:
    """
    记录用户在阅读报告时的行为数据

    参数:
      project_id: 项目ID
      section_name: 报告章节名 (如 "物理核验表", "方案对比", "风险提示")
      view_time_s: 该章节停留时间(秒)
      scroll_count: 该章节内滚动次数
    """
    conn = _get_conn()
    try:
        ts = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO report_metrics (project_id, section_name, view_time_s, "
            "scroll_count, timestamp) VALUES (?,?,?,?,?)",
            (project_id, section_name, view_time_s, scroll_count, ts)
        )
        conn.commit()
        return {"project_id": project_id, "section": section_name, "recorded": True}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 2. 进化机制: 需求分析师 — 追问策略
# ═══════════════════════════════════════════════════════════════

# 参数中文别名映射 (用于从issue文本中智能匹配参数名)
_PARAM_ALIASES = {
    "torque_nm": ["转矩", "扭矩", "力矩"],
    "duty_cycle": ["工作制", "S1", "S2", "S3", "S4", "duty"],
    "overload_pct": ["过载", "超载"],
    "altitude_m": ["海拔", "高原"],
    "protection_class": ["防护", "IP"],
    "frequency_hz": ["频率", "Hz"],
    "starting_torque_ratio": ["起动转矩", "启动转矩", "堵转"],
    "ambient_temp_c": ["环境温度", "工作温度"],
    "ed_pct": ["负载持续率", "ED", "暂载率"],
    "mounting": ["安装方式", "底脚", "法兰"],
    "insulation_class": ["绝缘等级", "绝缘"],
    "cooling": ["冷却方式", "水冷", "风冷"],
}


def _param_alias_match(param: str, text: str) -> bool:
    """检查文本中是否包含参数的中文别名"""
    aliases = _PARAM_ALIASES.get(param, [])
    return any(alias in text for alias in aliases)


def evolve_question_strategy() -> dict:
    """
    分析质量评分低的项目, 提取缺失参数模式, 生成强制追问规则

    触发条件: 质量评分 < 80 的项目中, 某参数缺失频率 > 30%
    进化动作: 生成追问规则, 注入需求分析师的提示词
    """
    conn = _get_conn()
    try:
        # 查找质量评分低的步骤
        low_quality = conn.execute(
            "SELECT project_id, quality_issues FROM operation_metrics "
            "WHERE step = 'parse_requirement' AND quality_score < 80 "
            "ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()

        if not low_quality:
            return {"evolved": False, "reason": "无低质量记录"}

        # 分析缺失参数模式
        missing_params = defaultdict(int)
        total_low = len(low_quality)
        # v2.05.02: 扩展检测的参数列表
        detectable_params = [
            "torque_nm", "duty_cycle", "overload_pct", "altitude_m",
            "protection_class", "frequency_hz", "starting_torque_ratio",
            "ambient_temp_c", "ed_pct", "mounting", "insulation_class", "cooling",
        ]
        for _, issues in low_quality:
            if issues:
                try:
                    issue_list = json.loads(issues) if issues.startswith("[") else [issues]
                    for issue in issue_list:
                        issue_str = str(issue)
                        for keyword in detectable_params:
                            if keyword in issue_str or _param_alias_match(keyword, issue_str):
                                missing_params[keyword] += 1
                except (json.JSONDecodeError, TypeError):
                    pass

        # 生成追问规则
        new_rules = []
        rules = _load_forced_questions()
        for param, count in missing_params.items():
            freq = count / total_low if total_low > 0 else 0
            if freq >= 0.30:  # 缺失频率 > 30%
                rule_key = f"force_ask_{param}"
                if not any(r.get("param") == param for r in rules.get("rules", [])):
                    keywords = _get_param_keywords(param)
                    rule = {
                        "param": param,
                        "frequency": round(freq, 2),
                        "keywords": keywords,
                        "message": f"请补充{param}信息，这会影响电机选型精度",
                        "created_at": datetime.now().isoformat(),
                        "trigger_count": count,
                    }
                    rules.setdefault("rules", []).append(rule)
                    new_rules.append(rule)

        if new_rules:
            _save_forced_questions(rules)
            _log_evolution("RequirementAgent", "question_strategy",
                           f"新增{len(new_rules)}条追问规则",
                           json.dumps(new_rules, ensure_ascii=False))

        return {"evolved": len(new_rules) > 0, "new_rules": new_rules,
                "total_low_quality": total_low}
    finally:
        conn.close()


def _get_param_keywords(param: str) -> list:
    """获取参数对应的用户描述关键词 (v2.05.02 增强领域关键词)"""
    keyword_map = {
        "torque_nm": ["转矩", "扭矩", "力矩", "torque"],
        "duty_cycle": ["工作制", "断续", "连续", "周期", "duty", "S3", "S4"],
        "overload_pct": ["过载", "超载", "倍数", "overload", "150%", "200%"],
        "altitude_m": ["海拔", "高原", "altitude", "3000米", "4000米"],
        "protection_class": ["防护", "IP", "防爆", "protection", "IP65", "IP55"],
        "frequency_hz": ["频率", "50Hz", "60Hz", "400Hz", "frequency", "中频"],
        "starting_torque_ratio": ["起动转矩", "启动转矩", "堵转", "starting",
                                  "重载", "频繁起动", "破碎机", "搅拌机", " crusher", "mixer"],
        "ambient_temp_c": ["环境温度", "工作温度", "ambient", "高温", "低温", "-40", "60度"],
        "ed_pct": ["负载持续率", "ED", "暂载率", "40%", "60%"],
        "mounting": ["安装", "底脚", "法兰", "B3", "B5", "B35", "mounting"],
        "insulation_class": ["绝缘", "insulation", "F级", "H级", "B级"],
        "cooling": ["冷却", "水冷", "风冷", "油冷", "cooling", "强制风冷"],
    }
    return keyword_map.get(param, [param])


# ═══════════════════════════════════════════════════════════════
# 2b. v4.02.68 (系列三 Tier B): L0↔L1 残差采集 + 离线标定
#   设计口径 (报告 6/8 节): 不让 LLM 从 top-10 偏差样本"即时发明" L0 系数修正
#   (top-10 属"因 L0 评分高而被选中"的幸存者偏差样本), 而是把残差累积入库、
#   跨多轮离线标定 → 产出「全局偏置」+「高残差设计区域」; AI 每轮角色限于
#   "标注高残差区域", 而非给出 L0 公式改动。
# ═══════════════════════════════════════════════════════════════

L0_CAL_MIN_SAMPLES = 20     # 标定最少样本数 (低于此只报全局, 不输出区域结论)
L0_CAL_WINDOW = 400         # 标定窗口 (最近 N 条; 防久远工况漂移覆盖当前)
L0_CAL_REGION_MIN_N = 5     # 单区域最少样本数 (低于此不判定该区域)
L0_CALIBRATION_PATH = r"D:\MotorDesign\Knowledge\l0_calibration.json"


def record_l0_residuals(project_id: str, residuals: list, specs: dict = None,
                        motor_type: str = "", round_index: int = 0) -> dict:
    """把一轮 L0↔L1 残差样本写入 l0_residuals 账本 (fail-open, 不抛异常)。

    residuals 条目: {signature, l0_eff, l1_eff, delta_eff, l0_temp, l1_temp,
                     delta_temp, slot_fill, stator_od, stator_id, core_length,
                     poles, l0_score, source_path, fault, fault_detail}
    无有效 delta_eff 且非 fault 的条目跳过; fault 条目以 fault=1 落库供审计,
    但被 evolve_l0_calibration 排除出统计 (防 Δη≈-95pp 伪残差污染)。
    """
    result = {"inserted": 0, "skipped": 0, "fault": 0, "error": ""}
    try:
        rows = []
        ts = datetime.now().isoformat()
        for it in (residuals or []):
            if not isinstance(it, dict):
                result["skipped"] += 1
                continue
            _fault = bool(it.get("fault"))
            if _fault:
                result["fault"] += 1
            elif it.get("delta_eff") is None or it.get("l0_eff") is None:
                result["skipped"] += 1
                continue
            rows.append((
                project_id, int(round_index or 0),
                (motor_type or (specs or {}).get("motor_type") or ""),
                it.get("signature"),
                _fnum(it.get("l0_eff")), _fnum(it.get("l1_eff")), _fnum(it.get("delta_eff")),
                _fnum(it.get("l0_temp")), _fnum(it.get("l1_temp")), _fnum(it.get("delta_temp")),
                _fnum(it.get("slot_fill")),
                _fnum(it.get("stator_od")), _fnum(it.get("stator_id")),
                _fnum(it.get("core_length")), _fnum(it.get("poles")),
                _fnum(it.get("turns_per_coil")),
                _fnum(it.get("wire_dia")), _fnum(it.get("n_strands")),
                _fnum(it.get("current_density")),
                _fnum(it.get("l0_score")), it.get("source_path"),
                1 if _fault else 0, str(it.get("fault_detail") or "")[:300],
                # ── v1.0 损耗/工况真值（A3）：调用方未传则 NULL，不影响既有写入 ──
                _fnum(it.get("max_temp")),
                _fnum(it.get("copper_loss")), _fnum(it.get("iron_loss")),
                _fnum(it.get("mechanical_loss")), _fnum(it.get("total_loss")),
                _fnum(it.get("line_current_a")),
                (it.get("cooling") if isinstance(it.get("cooling"), str) else None),
                _fnum(it.get("power_kw")), _fnum(it.get("voltage_v")),
                # 铜损标定必需：P=ρ·I²·Qs·N·l/(a·aWire) 中的 Qs 与 a
                _fnum(it.get("slots_stator")), _fnum(it.get("parallel_circuits")),
                _fnum(it.get("speed_rpm")), ts,
            ))
        if not rows:
            return result
        conn = _get_conn()
        try:
            conn.executemany(
                "INSERT INTO l0_residuals "
                "(project_id, round_index, motor_type, signature, "
                " l0_eff, l1_eff, delta_eff, l0_temp, l1_temp, delta_temp, slot_fill, "
                " stator_od, stator_id, core_length, poles, turns_per_coil, "
                " wire_dia, n_strands, current_density, l0_score, source_path, "
                " fault, fault_detail, "
                " max_temp, copper_loss, iron_loss, mechanical_loss, total_loss, "
                " line_current_a, cooling, power_kw, voltage_v, "
                " slots_stator, parallel_circuits, speed_rpm, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"
                " ,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            result["inserted"] = len(rows)
        finally:
            conn.close()
    except Exception as e:  # fail-open: 账本异常不得影响主流程
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _fnum(v):
    """安全转 float; 非法/None → None (保持 SQL NULL)。"""
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None


def evolve_l0_calibration(window: int = L0_CAL_WINDOW,
                          min_samples: int = L0_CAL_MIN_SAMPLES) -> dict:
    """跨轮 L0 离线标定: 统计 L0 预测偏差, 输出全局偏置与高残差设计区域。

    返回:
      {total_samples, window, global_bias_pp, global_mae_pp,
       regions: [{dim, band, n, bias_pp, mae_pp}],
       high_residual_regions: [str], updated_at, note}
    约定: delta = L1实算η - L0预测η; 正 = L0 低估, 负 = L0 高估。
    """
    out = {"total_samples": 0, "window": 0, "global_bias_pp": 0.0,
           "global_mae_pp": 0.0, "regions": [], "high_residual_regions": [],
           "updated_at": datetime.now().isoformat(), "note": ""}
    try:
        conn = _get_conn()
        try:
            total_all = conn.execute(
                "SELECT COUNT(*) FROM l0_residuals WHERE fault=0").fetchone()[0]
            rows = conn.execute(
                "SELECT delta_eff, delta_temp, stator_od, slot_fill, poles "
                "FROM l0_residuals WHERE fault=0 AND delta_eff IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (int(window),)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        out["note"] = f"标定查询失败: {type(e).__name__}: {e}"
        return out

    out["total_samples"] = int(total_all or 0)
    out["window"] = len(rows)
    deltas = [float(r[0]) for r in rows if r[0] is not None]
    if not deltas:
        out["note"] = "无有效残差样本 (故障方案已排除), 暂不标定"
        _save_json(L0_CALIBRATION_PATH, out)
        return out

    n = len(deltas)
    bias = sum(deltas) / n
    mae = sum(abs(x) for x in deltas) / n
    out["global_bias_pp"] = round(bias, 2)
    out["global_mae_pp"] = round(mae, 2)

    if n < min_samples:
        out["note"] = f"样本不足 ({n}/{min_samples}), 仅报全局偏置, 不输出区域结论"
        _save_json(L0_CALIBRATION_PATH, out)
        return out

    # 区域分带: 外径三分位 / 槽满率工艺带 / 极数
    ods = sorted(float(r[2]) for r in rows if r[2] is not None)
    od_bands = None
    if len(ods) >= 9:
        _q1, _q2 = ods[len(ods) // 3], ods[(2 * len(ods)) // 3]
        od_bands = (_q1, _q2)

    def _od_band(od):
        if od is None or od_bands is None:
            return None
        if od <= od_bands[0]:
            return f"外径≤{od_bands[0]:.0f}mm"
        if od <= od_bands[1]:
            return f"外径{od_bands[0]:.0f}~{od_bands[1]:.0f}mm"
        return f"外径>{od_bands[1]:.0f}mm"

    def _sf_band(sf):
        if sf is None:
            return None
        if sf < 0.65:
            return "槽满率<0.65"
        if sf <= 0.75:
            return "槽满率0.65~0.75"
        return "槽满率>0.75"

    def _poles_band(p):
        if p is None:
            return None
        return f"{int(p)}极"

    def _collect(key_fn):
        groups = defaultdict(list)
        for r in rows:
            if r[0] is None:
                continue
            b = key_fn(r[2], r[3], r[4])
            if b is None:
                continue
            groups[b].append(float(r[0]))
        res = []
        for b, vals in groups.items():
            if len(vals) < L0_CAL_REGION_MIN_N:
                continue
            _b = sum(vals) / len(vals)
            _m = sum(abs(x) for x in vals) / len(vals)
            res.append({"dim": key_fn.__name__, "band": b, "n": len(vals),
                        "bias_pp": round(_b, 2), "mae_pp": round(_m, 2)})
        return res

    regions = []
    for _fn, _dim in ((lambda od, sf, p: _od_band(od), "外径带"),
                      (lambda od, sf, p: _sf_band(sf), "槽满率带"),
                      (lambda od, sf, p: _poles_band(p), "极数")):
        for _rg in _collect(_fn):
            _rg["dim"] = _dim
            regions.append(_rg)
    regions.sort(key=lambda x: -x["mae_pp"])
    out["regions"] = regions

    # 高残差判定: 区域 MAE 超全局 1.5 倍 (>2pp 下限), 或区域偏置相对全局偏置偏离 >2pp
    #   (后者避免"全局性偏置"把所有分带一并标红 —— 只标真正异于全局的区域)
    _hi_mae = max(2.0, mae * 1.5)
    for _rg in regions:
        _excess = abs(_rg["bias_pp"] - bias)
        if _rg["mae_pp"] > _hi_mae or _excess > 2.0:
            out["high_residual_regions"].append(
                f"{_rg['dim']}={_rg['band']} (n={_rg['n']}, "
                f"偏置{_rg['bias_pp']:+.1f}pp, MAE={_rg['mae_pp']:.1f}pp)")
    out["high_residual_regions"] = out["high_residual_regions"][:5]
    out["note"] = (f"样本 {n} 条 (累计 {out['total_samples']} 条, 已排除故障方案); "
                   "正偏置=L0低估, 负偏置=L0高估")
    _save_json(L0_CALIBRATION_PATH, out)
    try:
        _log_evolution("ReviewAgent", "l0_calibration",
                       f"L0 离线标定: 全局偏置 {out['global_bias_pp']:+.2f}pp, "
                       f"MAE {out['global_mae_pp']:.2f}pp, "
                       f"高残差区域 {len(out['high_residual_regions'])} 个",
                       json.dumps(out, ensure_ascii=False)[:1500])
    except Exception:
        pass
    return out


def get_l0_calibration_hint() -> dict:
    """只读读取最近一次 L0 标定结果 (不存在则返回空结构, 不触发标定)。"""
    return _load_json(L0_CALIBRATION_PATH, {})


# ═══════════════════════════════════════════════════════════════
# 3. 进化机制: 方案生成师 — 高效参数域
# ═══════════════════════════════════════════════════════════════

def evolve_parameter_domain() -> dict:
    """
    统计被采纳方案的参数分布, 更新高效参数域

    触发条件: 有 ≥3 个被采纳方案
    进化动作: 计算各参数的 95% 置信区间, 更新推荐范围
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT adopted_params FROM project_summary "
            "WHERE adopted_params IS NOT NULL AND adopted_params != '' "
            "ORDER BY created_at DESC LIMIT 100"
        ).fetchall()

        if len(rows) < 3:
            return {"evolved": False, "reason": f"采纳方案不足3个 (当前{len(rows)})"}

        # 收集参数值
        param_values = defaultdict(list)
        for (params_str,) in rows:
            try:
                params = json.loads(params_str) if params_str else {}
                for key in ["stator_od", "stator_id", "core_length", "poles",
                            "turns_per_coil", "air_gap", "slots_stator"]:
                    if key in params and params[key] is not None:
                        param_values[key].append(float(params[key]))
            except (json.JSONDecodeError, TypeError):
                pass

        # 计算置信区间
        domain = _load_param_domain()
        updated = []
        for key, values in param_values.items():
            if len(values) >= 3:
                values.sort()
                low = values[int(len(values) * 0.05)]
                high = values[int(len(values) * 0.95)]
                old_range = domain.get("ranges", {}).get(key, {})
                domain.setdefault("ranges", {})[key] = {
                    "low": round(low, 1),
                    "high": round(high, 1),
                    "median": round(values[len(values) // 2], 1),
                    "sample_count": len(values),
                    "updated_at": datetime.now().isoformat(),
                }
                if old_range:
                    updated.append(f"{key}: {old_range.get('low','?')}~{old_range.get('high','?')} → {low:.1f}~{high:.1f}")
                else:
                    updated.append(f"{key}: 新增 {low:.1f}~{high:.1f}")

        if updated:
            domain["last_updated"] = datetime.now().isoformat()
            _save_param_domain(domain)
            _log_evolution("DesignAgent", "parameter_domain",
                           f"更新{len(updated)}个参数范围",
                           json.dumps(updated, ensure_ascii=False))

        # v2.05.02: 低效区间剔除 — 分析质量低的方案参数, 标记排除区间
        low_quality_designs = conn.execute(
            "SELECT project_id, quality_issues FROM operation_metrics "
            "WHERE step = 'generate_designs' AND quality_score < 80 "
            "ORDER BY timestamp DESC LIMIT 30"
        ).fetchall()

        exclusion_zones = []
        if low_quality_designs:
            # 从project_summary中获取这些低质量项目的采纳参数
            for pid, issues in low_quality_designs:
                adopted = conn.execute(
                    "SELECT adopted_params FROM project_summary WHERE project_id = ?",
                    (pid,)
                ).fetchone()
                if adopted and adopted[0]:
                    try:
                        params = json.loads(adopted[0])
                        # 标记低效区间
                        for key in ["stator_od", "core_length"]:
                            if key in params and params[key] is not None:
                                val = float(params[key])
                                zone_key = f"exclude_{key}"
                                existing = domain.setdefault("exclusion_zones", {}).setdefault(zone_key, [])
                                if not any(abs(z["value"] - val) < val * 0.05 for z in existing):
                                    existing.append({
                                        "value": round(val, 1),
                                        "reason": f"低质量项目{pid}: 质量<{QUALITY_THRESHOLD}",
                                        "count": 1,
                                    })
                                else:
                                    for z in existing:
                                        if abs(z["value"] - val) < val * 0.05:
                                            z["count"] += 1
                                            break
                    except (json.JSONDecodeError, TypeError):
                        pass

            # 标记高频低效区间 (出现≥2次的)
            for zone_key, zones in domain.get("exclusion_zones", {}).items():
                high_freq = [z for z in zones if z["count"] >= 2]
                if high_freq:
                    exclusion_zones.append(f"{zone_key}: {len(high_freq)}个低效区间")
                    domain.setdefault("exclusion_zones", {})[zone_key] = zones

        # v2.05.02: 超时降量 — 分析方案生成耗时, 对慢项目降低默认方案数
        slow_projects = conn.execute(
            "SELECT project_id, duration_ms FROM operation_metrics "
            "WHERE step = 'generate_designs' AND duration_ms > 120000 "  # >2分钟
            "ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()

        design_count_adjustments = []
        if len(slow_projects) >= 2:
            # 获取这些项目的类型
            for pid, dur in slow_projects:
                ptype_row = conn.execute(
                    "SELECT project_type FROM project_summary WHERE project_id = ?",
                    (pid,)
                ).fetchone()
                ptype = ptype_row[0] if ptype_row else "通用电机"
                old_count = domain.get("design_count", {}).get(ptype, 30)
                new_count = max(15, int(old_count * DESIGN_COUNT_TIMEOUT_RATIO))
                domain.setdefault("design_count", {})[ptype] = new_count
                design_count_adjustments.append(
                    f"{ptype}: {old_count}→{new_count} (超时{dur/1000:.0f}s)")

        if exclusion_zones or design_count_adjustments:
            domain["last_updated"] = datetime.now().isoformat()
            _save_param_domain(domain)
            if exclusion_zones:
                _log_evolution("DesignAgent", "parameter_domain_exclude",
                               f"标记{len(exclusion_zones)}个低效参数区间",
                               json.dumps(exclusion_zones, ensure_ascii=False))
            if design_count_adjustments:
                _log_evolution("DesignAgent", "design_count_adjust",
                               f"调整{len(design_count_adjustments)}类项目方案数",
                               json.dumps(design_count_adjustments, ensure_ascii=False))

        return {"evolved": len(updated) > 0 or len(exclusion_zones) > 0,
                "updated_ranges": updated,
                "exclusion_zones": exclusion_zones,
                "design_count_adjustments": design_count_adjustments,
                "total_adopted": len(rows)}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 4. 进化机制: AI调用策略 — 本地/云端路由
# ═══════════════════════════════════════════════════════════════

def evolve_ai_routing() -> dict:
    """
    分析各步骤的云端 vs 本地成本与质量, 自动调整路由策略

    v2.05.02 增强: 连续N次满足条件才切换 (避免偶发波动误判)
    触发条件: 某步骤连续 ROUTING_CONSECUTIVE_TRIGGER(5) 次满足:
              云端成本 > 3×本地, 且质量提升 < 5%
    进化动作: 将该步骤切换为本地优先
    """
    conn = _get_conn()
    try:
        # 按步骤统计本地/云端的成本和质量
        steps = conn.execute(
            "SELECT step, cost_mode, AVG(cost_cents) as avg_cost, "
            "AVG(quality_score) as avg_quality, COUNT(*) as cnt "
            "FROM operation_metrics "
            "WHERE cost_cents > 0 OR cost_mode = 'cloud' "
            "GROUP BY step, cost_mode"
        ).fetchall()

        if not steps:
            return {"evolved": False, "reason": "无成本数据"}

        # 按步骤分组
        step_stats = defaultdict(dict)
        for step, mode, avg_cost, avg_quality, cnt in steps:
            step_stats[step][mode] = {
                "avg_cost": avg_cost or 0,
                "avg_quality": avg_quality or 100,
                "count": cnt,
            }

        routing_rules = _load_evolution_rules().get("routing", {})
        changes = []

        for step, modes in step_stats.items():
            local = modes.get("local", {})
            cloud = modes.get("cloud", {})

            # v2.05.02: 连续N次检查
            if local and cloud and local.get("count", 0) >= 3 and cloud.get("count", 0) >= 3:
                cost_ratio = cloud["avg_cost"] / max(local["avg_cost"], 1)
                quality_gain = (cloud["avg_quality"] - local["avg_quality"]) / 100.0

                # 检查最近N条记录是否连续满足条件
                recent_records = conn.execute(
                    "SELECT cost_mode, cost_cents, quality_score FROM operation_metrics "
                    "WHERE step = ? AND (cost_cents > 0 OR cost_mode = 'cloud') "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (step, ROUTING_CONSECUTIVE_TRIGGER * 2)
                ).fetchall()

                # 计算每条记录的成本比和质量差
                consecutive_count = 0
                local_costs = [r[1] for r in recent_records if r[0] == "local"]
                cloud_costs = [r[1] for r in recent_records if r[0] == "cloud"]
                local_avg = sum(local_costs) / max(len(local_costs), 1)
                cloud_avg = sum(cloud_costs) / max(len(cloud_costs), 1)

                for mode, cost, quality in recent_records:
                    if mode == "cloud" and local_avg > 0:
                        ratio = cost / local_avg
                        if ratio > COST_RATIO_THRESHOLD:
                            consecutive_count += 1

                meets_consecutive = consecutive_count >= ROUTING_CONSECUTIVE_TRIGGER

                if cost_ratio > COST_RATIO_THRESHOLD and quality_gain < QUALITY_GAIN_THRESHOLD:
                    old_pref = routing_rules.get(step, {}).get("prefer", "auto")
                    # v2.05.02: 连续满足或已有≥5条云端记录时触发
                    should_switch = meets_consecutive or cloud.get("count", 0) >= ROUTING_CONSECUTIVE_TRIGGER

                    if should_switch:
                        routing_rules[step] = {
                            "prefer": "local",
                            "reason": f"云端成本{cost_ratio:.1f}×本地, 质量提升仅{quality_gain*100:.1f}%, "
                                      f"连续{consecutive_count}次超阈值",
                            "cost_ratio": round(cost_ratio, 2),
                            "quality_gain": round(quality_gain, 4),
                            "consecutive_count": consecutive_count,
                            "updated_at": datetime.now().isoformat(),
                        }
                        if old_pref != "local":
                            changes.append(f"{step}: {old_pref} → local "
                                         f"(成本比{cost_ratio:.1f}×, 连续{consecutive_count}次)")

        if changes:
            rules = _load_evolution_rules()
            rules["routing"] = routing_rules
            rules["last_updated"] = datetime.now().isoformat()
            _save_evolution_rules(rules)
            _log_evolution("AIRouting", "ai_routing",
                           f"调整{len(changes)}个步骤的路由策略",
                           json.dumps(changes, ensure_ascii=False))

        return {"evolved": len(changes) > 0, "changes": changes,
                "step_stats": {k: dict(v) for k, v in step_stats.items()}}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 5. 阈值检查: 连续低质量/低效率/耗时增长
# ═══════════════════════════════════════════════════════════════

def check_quality_threshold() -> dict:
    """
    检查同一智能体是否连续N次质量评分 < 阈值
    触发: 生成行为重训建议
    """
    conn = _get_conn()
    try:
        agents = conn.execute(
            "SELECT DISTINCT agent FROM operation_metrics"
        ).fetchall()

        alerts = []
        for (agent,) in agents:
            recent = conn.execute(
                "SELECT quality_score, project_id FROM operation_metrics "
                "WHERE agent = ? ORDER BY timestamp DESC LIMIT ?",
                (agent, QUALITY_CONSECUTIVE_TRIGGER)
            ).fetchall()

            if len(recent) >= QUALITY_CONSECUTIVE_TRIGGER:
                all_low = all(score < QUALITY_THRESHOLD for score, _ in recent)
                if all_low:
                    avg_q = sum(s for s, _ in recent) / len(recent)
                    alert = {
                        "agent": agent,
                        "consecutive_count": len(recent),
                        "avg_quality": round(avg_q, 1),
                        "threshold": QUALITY_THRESHOLD,
                        "action": "建议触发行为重训: 检查提示词/模型/参数",
                        "project_ids": [pid for _, pid in recent],
                    }
                    alerts.append(alert)
                    _log_evolution(agent, "quality_threshold",
                                   f"连续{len(recent)}次质量<{QUALITY_THRESHOLD}",
                                   json.dumps(alert, ensure_ascii=False))

        return {"alerts": alerts, "checked_agents": len(agents)}
    finally:
        conn.close()


def check_efficiency_threshold() -> dict:
    """
    检查同类项目是否连续N次方案有效率 < 阈值
    触发: 调整默认参数矩阵
    """
    conn = _get_conn()
    try:
        types = conn.execute(
            "SELECT DISTINCT project_type FROM project_summary "
            "WHERE project_type IS NOT NULL"
        ).fetchall()

        alerts = []
        for (ptype,) in types:
            recent = conn.execute(
                "SELECT quality_overall, project_id FROM project_summary "
                "WHERE project_type = ? ORDER BY created_at DESC LIMIT ?",
                (ptype, EFFICIENCY_CONSECUTIVE_TRIGGER)
            ).fetchall()

            if len(recent) >= EFFICIENCY_CONSECUTIVE_TRIGGER:
                all_low = all(q < EFFICIENCY_THRESHOLD for q, _ in recent)
                if all_low:
                    alert = {
                        "project_type": ptype,
                        "consecutive_count": len(recent),
                        "avg_efficiency": round(sum(q for q, _ in recent) / len(recent), 1),
                        "threshold": EFFICIENCY_THRESHOLD,
                        "action": f"建议调整{ptype}的默认参数矩阵: " +
                                  "降低方案数量、缩小扫描范围",
                    }
                    alerts.append(alert)
                    _log_evolution("DesignAgent", "efficiency_threshold",
                                   f"{ptype}连续{len(recent)}次效率<{EFFICIENCY_THRESHOLD}",
                                   json.dumps(alert, ensure_ascii=False))

        return {"alerts": alerts, "checked_types": len(types)}
    finally:
        conn.close()


def check_decision_time_growth() -> dict:
    """
    检查人工决策耗时是否连续N次增长
    触发: 推送界面优化建议
    """
    conn = _get_conn()
    try:
        recent = conn.execute(
            "SELECT decision_time_s, project_id FROM project_summary "
            "WHERE decision_time_s > 0 ORDER BY created_at DESC LIMIT ?",
            (DECISION_TIME_GROWTH_TRIGGER,)
        ).fetchall()

        if len(recent) < DECISION_TIME_GROWTH_TRIGGER:
            return {"alerts": [], "reason": "数据不足"}

        # 检查是否连续增长 (最近的 → 最远的)
        times = [t for t, _ in reversed(recent)]  # 按时间正序
        is_growing = all(times[i] < times[i + 1] for i in range(len(times) - 1))

        if is_growing:
            alert = {
                "consecutive_count": len(times),
                "time_trend": times,
                "latest_time": times[-1],
                "action": "人工决策耗时持续增长, 建议: " +
                          "1) 将高频参数提升到界面最显著位置; " +
                          "2) 折叠低频修改项; " +
                          "3) 增加可视化对比图表",
            }
            _log_evolution("UI", "decision_time_growth",
                           f"决策耗时连续{len(times)}次增长",
                           json.dumps(alert, ensure_ascii=False))
            return {"alerts": [alert]}
        return {"alerts": []}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 5b. 进化机制: 评审推荐师 — 报告结构优化 (v2.05.02 新增)
# ═══════════════════════════════════════════════════════════════

def evolve_report_structure() -> dict:
    """
    分析人工决策耗时和报告阅读行为, 优化评审报告结构

    触发条件:
      - 决策耗时 > 30分钟 (DECISION_TIME_LONG_THRESHOLD)
      - 某报告章节平均阅读时间异常长 → 信息呈现效率低
      - 魔鬼代言人质量评分 < 60 → 提示词需优化

    进化动作:
      1. 关键对比指标前置 (效率/温升/转矩 放到报告最前)
      2. 增加可视化图表标记 (提示前端渲染图表)
      3. 减少冗长文字 (限制段落字数)
      4. 优化魔鬼代言人提示词 (要求更具体的风险分析)
    """
    conn = _get_conn()
    try:
        rules = _load_report_structure_rules()
        changes = []

        # 1. 检查决策耗时
        long_decisions = conn.execute(
            "SELECT project_id, decision_time_s FROM project_summary "
            "WHERE decision_time_s > ? ORDER BY created_at DESC LIMIT 10",
            (DECISION_TIME_LONG_THRESHOLD,)
        ).fetchall()

        if len(long_decisions) >= 2:
            avg_time = sum(t for _, t in long_decisions) / len(long_decisions)
            rules["key_metrics_first"] = {
                "enabled": True,
                "reason": f"平均决策耗时{avg_time/60:.0f}分钟, 关键指标应前置",
                "priority_order": ["efficiency", "max_temp", "torque", "stator_od", "poles"],
                "updated_at": datetime.now().isoformat(),
            }
            rules["add_visualization"] = {
                "enabled": True,
                "reason": "增加可视化图表以加速决策",
                "chart_types": ["radar_chart", "bar_comparison", "efficiency_scatter"],
                "updated_at": datetime.now().isoformat(),
            }
            rules["reduce_text"] = {
                "enabled": True,
                "reason": "限制段落字数, 突出数据对比",
                "max_paragraph_chars": 200,
                "updated_at": datetime.now().isoformat(),
            }
            changes.append(f"关键指标前置+可视化+精简文字 (决策耗时{avg_time/60:.0f}分钟)")

        # 2. 检查报告章节阅读时间
        section_stats = conn.execute(
            "SELECT section_name, AVG(view_time_s) as avg_time, "
            "AVG(scroll_count) as avg_scroll, COUNT(*) as cnt "
            "FROM report_metrics GROUP BY section_name"
        ).fetchall()

        for section, avg_time, avg_scroll, cnt in section_stats:
            if cnt >= 2 and avg_time and avg_time > 120:  # 单章节平均>2分钟
                rules.setdefault("section_overrides", {})[section] = {
                    "issue": "阅读时间过长",
                    "avg_view_time_s": round(avg_time, 1),
                    "avg_scroll_count": round(avg_scroll or 0, 1),
                    "action": "精简内容或拆分为子章节",
                    "updated_at": datetime.now().isoformat(),
                }
                changes.append(f"章节'{section}'平均阅读{avg_time:.0f}s → 建议精简")

        # 3. 检查魔鬼代言人质量
        devils_quality = conn.execute(
            "SELECT AVG(quality_score) as avg_q, COUNT(*) as cnt "
            "FROM operation_metrics WHERE agent LIKE '%Devil%' OR agent LIKE '%魔鬼%'"
        ).fetchone()

        if devils_quality and devils_quality[1] >= 2:
            avg_q = devils_quality[0] or 100
            if avg_q < DEVILS_LOW_QUALITY_THRESHOLD:
                rules["devils_advocate_prompt_boost"] = {
                    "enabled": True,
                    "reason": f"魔鬼代言人平均质量{avg_q:.0f}<{DEVILS_LOW_QUALITY_THRESHOLD}, 需优化提示词",
                    "prompt_addition": (
                        "请针对每个方案给出具体的风险点, 包括: "
                        "1) 量化风险(如'温升裕度仅5K, 夏季可能超限'); "
                        "2) 失效模式(如'轴承寿命在重载下可能不足'); "
                        "3) 量产风险(如'该槽配合在冲压工艺中废品率偏高'). "
                        "避免笼统的'可能存在问题'式表述。"
                    ),
                    "updated_at": datetime.now().isoformat(),
                }
                changes.append(f"魔鬼代言人提示词优化 (质量{avg_q:.0f}<{DEVILS_LOW_QUALITY_THRESHOLD})")

        if changes:
            rules["last_updated"] = datetime.now().isoformat()
            _save_report_structure_rules(rules)
            _log_evolution("ReviewAgent", "report_structure",
                           f"报告结构优化{len(changes)}项",
                           json.dumps(changes, ensure_ascii=False))

        return {"evolved": len(changes) > 0, "changes": changes,
                "rules_summary": {k: v.get("enabled", True) if isinstance(v, dict) else v
                                  for k, v in rules.items() if k != "last_updated"}}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 5c. 进化机制: 决策界面 — 信息优先级 (v2.05.02 新增)
# ═══════════════════════════════════════════════════════════════

def evolve_decision_ui() -> dict:
    """
    分析用户在决策界面的参数交互模式, 优化信息展示优先级

    触发条件:
      - 某参数被修改次数 ≥ UI_MODIFY_FREQ_THRESHOLD(3) → 提升到界面顶部
      - 某参数被直接通过次数 ≥ UI_PASS_FREQ_THRESHOLD(5) → 自动折叠+默认值

    进化动作:
      1. 生成参数优先级规则 (promote/collapse)
      2. 注入前端界面配置
    """
    conn = _get_conn()
    try:
        # 统计各参数的修改/通过次数
        param_stats = conn.execute(
            "SELECT param_name, action, COUNT(*) as cnt "
            "FROM ui_interactions GROUP BY param_name, action"
        ).fetchall()

        if not param_stats:
            return {"evolved": False, "reason": "无UI交互数据"}

        # 按参数汇总
        param_actions = defaultdict(lambda: {"modify": 0, "pass": 0})
        for param, action, cnt in param_stats:
            param_actions[param][action] = cnt

        rules = _load_ui_priority_rules()
        changes = []

        promote_list = []
        collapse_list = []

        for param, actions in param_actions.items():
            modify_count = actions["modify"]
            pass_count = actions["pass"]
            total = modify_count + pass_count

            # 高频修改 → 提升到顶部
            if modify_count >= UI_MODIFY_FREQ_THRESHOLD:
                promote_list.append({
                    "param": param,
                    "modify_count": modify_count,
                    "reason": f"被修改{modify_count}次, 提升展示优先级",
                })

            # 常通过 → 自动折叠
            if pass_count >= UI_PASS_FREQ_THRESHOLD and modify_count == 0:
                collapse_list.append({
                    "param": param,
                    "pass_count": pass_count,
                    "reason": f"连续{pass_count}次直接通过, 自动折叠+默认值",
                })

        if promote_list:
            rules["promote_params"] = {
                "params": [p["param"] for p in promote_list],
                "details": promote_list,
                "updated_at": datetime.now().isoformat(),
            }
            changes.extend([f"提升参数'{p['param']}'到顶部" for p in promote_list])

        if collapse_list:
            rules["collapse_params"] = {
                "params": [c["param"] for c in collapse_list],
                "details": collapse_list,
                "updated_at": datetime.now().isoformat(),
            }
            changes.extend([f"折叠参数'{c['param']}'" for c in collapse_list])

        if changes:
            rules["last_updated"] = datetime.now().isoformat()
            _save_ui_priority_rules(rules)
            _log_evolution("UI", "decision_ui_priority",
                           f"界面优先级调整{len(changes)}项",
                           json.dumps(changes, ensure_ascii=False))

        return {"evolved": len(changes) > 0, "changes": changes,
                "promote_params": [p["param"] for p in promote_list],
                "collapse_params": [c["param"] for c in collapse_list]}
    finally:
        conn.close()

def get_operation_dashboard() -> dict:
    """获取运营仪表盘数据 (供前端展示)"""
    conn = _get_conn()
    try:
        # 效率统计
        step_efficiency = conn.execute(
            "SELECT step, agent, AVG(duration_ms) as avg_ms, "
            "MAX(duration_ms) as max_ms, MIN(duration_ms) as min_ms, "
            "COUNT(*) as cnt "
            "FROM operation_metrics GROUP BY step ORDER BY avg_ms DESC"
        ).fetchall()

        # 成本统计
        cost_stats = conn.execute(
            "SELECT cost_mode, SUM(cost_cents) as total, "
            "AVG(cost_cents) as avg, COUNT(*) as cnt "
            "FROM operation_metrics WHERE cost_cents > 0 "
            "GROUP BY cost_mode"
        ).fetchall()

        # 质量统计
        quality_stats = conn.execute(
            "SELECT agent, AVG(quality_score) as avg_q, "
            "MIN(quality_score) as min_q, COUNT(*) as cnt "
            "FROM operation_metrics GROUP BY agent"
        ).fetchall()

        # 项目汇总
        project_count = conn.execute(
            "SELECT COUNT(*) FROM project_summary"
        ).fetchone()[0]

        # 进化规则
        rules = _load_evolution_rules()
        forced_q = _load_forced_questions()
        param_d = _load_param_domain()
        report_rules = _load_report_structure_rules()
        ui_rules = _load_ui_priority_rules()

        # v2.05.02: 进化日志 (最近10条)
        recent_evolutions = conn.execute(
            "SELECT timestamp, agent, trigger_type, action_taken "
            "FROM evolution_log ORDER BY id DESC LIMIT 10"
        ).fetchall()

        # v2.05.02: UI交互统计
        ui_stats = conn.execute(
            "SELECT param_name, action, COUNT(*) as cnt "
            "FROM ui_interactions GROUP BY param_name, action "
            "ORDER BY cnt DESC LIMIT 20"
        ).fetchall()

        return {
            "efficiency": [
                {"step": r[0], "agent": r[1], "avg_ms": r[2],
                 "max_ms": r[3], "min_ms": r[4], "count": r[5]}
                for r in step_efficiency
            ],
            "cost": [
                {"mode": r[0], "total_cents": r[1], "avg_cents": r[2], "count": r[3]}
                for r in cost_stats
            ],
            "quality": [
                {"agent": r[0], "avg_score": round(r[1], 1),
                 "min_score": r[2], "count": r[3]}
                for r in quality_stats
            ],
            "project_count": project_count,
            "evolution_rules": {
                "routing": rules.get("routing", {}),
                "forced_questions": forced_q.get("rules", []),
                "param_domain": param_d.get("ranges", {}),
                "param_exclusion_zones": param_d.get("exclusion_zones", {}),
                "design_count_adjustments": param_d.get("design_count", {}),
                "report_structure": report_rules,
                "ui_priority": ui_rules,
            },
            "recent_evolutions": [
                {"timestamp": r[0], "agent": r[1], "trigger": r[2], "action": r[3]}
                for r in recent_evolutions
            ],
            "ui_interactions": [
                {"param": r[0], "action": r[1], "count": r[2]}
                for r in ui_stats
            ],
        }
    finally:
        conn.close()


def run_all_evolution() -> dict:
    """运行所有进化机制 (项目完成后调用)"""
    results = {}
    results["question_strategy"] = evolve_question_strategy()
    results["parameter_domain"] = evolve_parameter_domain()
    results["ai_routing"] = evolve_ai_routing()
    results["report_structure"] = evolve_report_structure()
    results["decision_ui"] = evolve_decision_ui()
    results["quality_check"] = check_quality_threshold()
    results["efficiency_check"] = check_efficiency_threshold()
    results["decision_time_check"] = check_decision_time_growth()
    # v4.02.68 (系列三 Tier B): L0↔L1 残差离线标定 (高残差区域 → 评价提示)
    results["l0_calibration"] = evolve_l0_calibration()
    return results


# ═══════════════════════════════════════════════════════════════
# 辅助: 文件读写
# ═══════════════════════════════════════════════════════════════

def _load_json(path: str, default: dict) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_evolution_rules() -> dict:
    return _load_json(EVOLUTION_RULES_PATH, {"routing": {}, "last_updated": ""})


def _save_evolution_rules(data: dict):
    _save_json(EVOLUTION_RULES_PATH, data)


def _load_forced_questions() -> dict:
    return _load_json(FORCED_QUESTIONS_PATH, {"rules": []})


def _save_forced_questions(data: dict):
    _save_json(FORCED_QUESTIONS_PATH, data)


def _load_param_domain() -> dict:
    return _load_json(PARAM_DOMAIN_PATH, {"ranges": {}, "last_updated": ""})


def _save_param_domain(data: dict):
    _save_json(PARAM_DOMAIN_PATH, data)


def _load_report_structure_rules() -> dict:
    return _load_json(REPORT_STRUCTURE_PATH, {"last_updated": ""})


def _save_report_structure_rules(data: dict):
    _save_json(REPORT_STRUCTURE_PATH, data)


def _load_ui_priority_rules() -> dict:
    return _load_json(UI_PRIORITY_PATH, {"last_updated": ""})


def _save_ui_priority_rules(data: dict):
    _save_json(UI_PRIORITY_PATH, data)


def _log_evolution(agent: str, trigger_type: str, action: str, detail: str):
    """记录进化日志"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO evolution_log (timestamp, agent, trigger_type, "
            "trigger_detail, action_taken, rule_data) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(), agent, trigger_type, detail, action, detail)
        )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("运营数据飞轮自测 v2.05.02")
    print("=" * 60)

    # 1. 记录测试数据
    print("\n--- 记录步骤指标 ---")
    for i in range(6):
        r = record_step_metric(
            project_id=f"TEST_{i:04d}",
            step="parse_requirement",
            agent="RequirementAgent",
            duration_ms=2000 + i * 500,
            cost_cents=0,
            cost_mode="local",
            quality_score=65 + i * 2,  # 低质量递增
            quality_issues='["torque_nm缺失", "duty_cycle未指定", "starting_torque_ratio缺失"]',
        )
        print(f"  项目{i}: {r}")

    # 1b. 记录UI交互数据 (v2.05.02)
    print("\n--- 记录UI交互 ---")
    for i in range(5):
        record_ui_interaction(f"UI_TEST_{i:04d}", "efficiency_target", "modify", "92", f"{90+i}")
        record_ui_interaction(f"UI_TEST_{i:04d}", "insulation_class", "pass", "", "")
    print("  UI交互记录完成 (efficiency_target×5 modify, insulation_class×5 pass)")

    # 1c. 记录报告阅读数据 (v2.05.02)
    print("\n--- 记录报告阅读 ---")
    for i in range(3):
        record_report_metric(f"RPT_TEST_{i:04d}", "物理核验表", 150.0, 5)
        record_report_metric(f"RPT_TEST_{i:04d}", "方案对比", 45.0, 2)
    print("  报告阅读记录完成 (物理核验表×3 @150s, 方案对比×3 @45s)")

    # 2. 追问策略进化
    print("\n--- 追问策略进化 ---")
    result = evolve_question_strategy()
    print(f"  evolved: {result.get('evolved')}")
    for rule in result.get("new_rules", []):
        print(f"  新规则: {rule['param']} (频率{rule['frequency']}) 关键词={rule['keywords']}")

    # 3. 仪表盘
    print("\n--- 运营仪表盘 ---")
    dash = get_operation_dashboard()
    print(f"  项目数: {dash['project_count']}")
    print(f"  效率统计: {len(dash['efficiency'])} 个步骤")
    print(f"  质量统计: {len(dash['quality'])} 个智能体")
    for q in dash["quality"]:
        print(f"    {q['agent']}: 平均{q['avg_score']}, 最低{q['min_score']}, 次数{q['count']}")
    print(f"  UI交互统计: {len(dash.get('ui_interactions', []))} 个参数")
    print(f"  进化日志: {len(dash.get('recent_evolutions', []))} 条")

    # 4. 阈值检查
    print("\n--- 质量阈值检查 ---")
    qc = check_quality_threshold()
    print(f"  alerts: {len(qc['alerts'])}")
    for a in qc["alerts"]:
        print(f"    {a['agent']}: 连续{a['consecutive_count']}次, 平均{a['avg_quality']}")

    # 5. 报告结构进化 (v2.05.02)
    print("\n--- 报告结构进化 ---")
    rs = evolve_report_structure()
    print(f"  evolved: {rs.get('evolved')}")
    for c in rs.get("changes", []):
        print(f"  变更: {c}")

    # 6. 决策界面进化 (v2.05.02)
    print("\n--- 决策界面进化 ---")
    ui = evolve_decision_ui()
    print(f"  evolved: {ui.get('evolved')}")
    print(f"  提升参数: {ui.get('promote_params', [])}")
    print(f"  折叠参数: {ui.get('collapse_params', [])}")

    # 7. 全量进化
    print("\n--- 全量进化 ---")
    all_results = run_all_evolution()
    for k, v in all_results.items():
        evolved = v.get("evolved", len(v.get("alerts", [])) > 0)
        print(f"  {k}: {'有变化' if evolved else '无变化'}")

    print("\n" + "=" * 60)
    print("自测完成")
