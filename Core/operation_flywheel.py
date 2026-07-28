# -*- coding: utf-8 -*-
"""
运营数据飞轮引擎 (Operation Flywheel) v2.04
=============================================
扩展效果飞轮(采纳/拒绝)之外的第二条链路: 效率/成本/质量反馈

双环架构:
  效果飞轮 (flywheel.py): 采纳/拒绝 → 技能模板进化
  运营飞轮 (本模块):     效率/成本/质量 → 智能体自我进化

进化机制:
  1. 需求分析师: 学习追问策略 (缺失参数 → 强制追问)
  2. 方案生成师: 学习高效参数域 (采纳方案分布 → 更新推荐范围)
  3. AI调用策略: 学习本地/云端路由 (成本比 → 自动切换)
  4. 评审推荐师: 学习报告结构优化 (决策耗时 → 调整模板)

阈值触发:
  - 同一智能体连续5次质量评分<70 → 触发行为重训
  - 同类项目连续3次方案有效率<50% → 调整默认参数矩阵
  - 人工决策环节平均耗时连续3次增长 → 推送界面优化建议
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

# ── 阈值常量 ──────────────────────────────────────────────────
QUALITY_THRESHOLD = 70          # 质量评分阈值
QUALITY_CONSECUTIVE_TRIGGER = 5  # 连续N次低质量触发重训
EFFICIENCY_THRESHOLD = 50       # 方案有效率阈值
EFFICIENCY_CONSECUTIVE_TRIGGER = 3  # 连续N次低效率触发调整
COST_RATIO_THRESHOLD = 3.0      # 云端/本地成本比阈值
QUALITY_GAIN_THRESHOLD = 0.05   # 质量提升阈值 (5%)
DECISION_TIME_GROWTH_TRIGGER = 3  # 决策耗时连续增长次数


def _get_conn():
    """获取数据库连接 (自动建表)"""
    conn = sqlite3.connect(DB_PATH)
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


# ═══════════════════════════════════════════════════════════════
# 2. 进化机制: 需求分析师 — 追问策略
# ═══════════════════════════════════════════════════════════════

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
        for _, issues in low_quality:
            if issues:
                try:
                    issue_list = json.loads(issues) if issues.startswith("[") else [issues]
                    for issue in issue_list:
                        # 提取参数名 (如 "torque_nm缺失", "duty_cycle未指定")
                        for keyword in ["torque_nm", "duty_cycle", "overload_pct",
                                        "altitude_m", "protection_class", "frequency_hz",
                                        "starting_torque_ratio", "ambient_temp_c"]:
                            if keyword in str(issue):
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
    """获取参数对应的用户描述关键词"""
    keyword_map = {
        "torque_nm": ["转矩", "扭矩", "力矩", "torque"],
        "duty_cycle": ["工作制", "断续", "连续", "周期", "duty"],
        "overload_pct": ["过载", "超载", "倍数", "overload"],
        "altitude_m": ["海拔", "高原", "altitude"],
        "protection_class": ["防护", "IP", "防爆", "protection"],
        "frequency_hz": ["频率", "50Hz", "60Hz", "400Hz", "frequency"],
        "starting_torque_ratio": ["起动转矩", "启动转矩", "堵转", "starting"],
        "ambient_temp_c": ["环境温度", "工作温度", "ambient"],
    }
    return keyword_map.get(param, [param])


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

        return {"evolved": len(updated) > 0, "updated_ranges": updated,
                "total_adopted": len(rows)}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 4. 进化机制: AI调用策略 — 本地/云端路由
# ═══════════════════════════════════════════════════════════════

def evolve_ai_routing() -> dict:
    """
    分析各步骤的云端 vs 本地成本与质量, 自动调整路由策略

    触发条件: 某步骤云端成本 > 3×本地, 且质量提升 < 5%
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

            if local and cloud and local.get("count", 0) >= 3 and cloud.get("count", 0) >= 3:
                cost_ratio = cloud["avg_cost"] / max(local["avg_cost"], 1)
                quality_gain = (cloud["avg_quality"] - local["avg_quality"]) / 100.0

                if cost_ratio > COST_RATIO_THRESHOLD and quality_gain < QUALITY_GAIN_THRESHOLD:
                    old_pref = routing_rules.get(step, {}).get("prefer", "auto")
                    routing_rules[step] = {
                        "prefer": "local",
                        "reason": f"云端成本{cost_ratio:.1f}×本地, 质量提升仅{quality_gain*100:.1f}%",
                        "cost_ratio": round(cost_ratio, 2),
                        "quality_gain": round(quality_gain, 4),
                        "updated_at": datetime.now().isoformat(),
                    }
                    if old_pref != "local":
                        changes.append(f"{step}: {old_pref} → local (成本比{cost_ratio:.1f}×)")

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
# 6. 运营仪表盘数据
# ═══════════════════════════════════════════════════════════════

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
            },
        }
    finally:
        conn.close()


def run_all_evolution() -> dict:
    """运行所有进化机制 (项目完成后调用)"""
    results = {}
    results["question_strategy"] = evolve_question_strategy()
    results["parameter_domain"] = evolve_parameter_domain()
    results["ai_routing"] = evolve_ai_routing()
    results["quality_check"] = check_quality_threshold()
    results["efficiency_check"] = check_efficiency_threshold()
    results["decision_time_check"] = check_decision_time_growth()
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
    print("运营数据飞轮自测")
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
            quality_issues='["torque_nm缺失", "duty_cycle未指定"]',
        )
        print(f"  项目{i}: {r}")

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

    # 4. 阈值检查
    print("\n--- 质量阈值检查 ---")
    qc = check_quality_threshold()
    print(f"  alerts: {len(qc['alerts'])}")
    for a in qc["alerts"]:
        print(f"    {a['agent']}: 连续{a['consecutive_count']}次, 平均{a['avg_quality']}")

    # 5. 全量进化
    print("\n--- 全量进化 ---")
    all_results = run_all_evolution()
    for k, v in all_results.items():
        evolved = v.get("evolved", len(v.get("alerts", [])) > 0)
        print(f"  {k}: {'有变化' if evolved else '无变化'}")

    print("\n" + "=" * 60)
    print("自测完成")
