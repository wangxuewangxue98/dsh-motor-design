"""
数据飞轮引擎 - 采纳/拒绝决策驱动的技能进化与规则挖掘
让系统越用越聪明: 每次反馈自动沉淀为知识

飞轮四步:
  1. 数据生成: 每个项目自动记录需求/参数/结果/AI决策/用户修正/客户反馈
  2. 技能化: 成功方案自动抽象为"设计技能模板", 验证一次权重+1
  3. 知识喂养: 从已采纳项目挖掘新规则, 自动更新本体
  4. 闭环迭代: 采纳/拒绝直接强化或弱化技能模板

对接: feedback_store (记录) + evolution_engine (技能/规则) + audit_logger (溯源)
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

import sys
sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Knowledge")
sys.path.insert(0, r"D:\MotorDesign\Scripts")

import audit_logger
from evolution_engine import (
    load_ontology, save_ontology, update_metrics,
    create_skill_template, mine_design_rule, find_matching_templates,
)
from feedback_store import record_feedback, get_feedback_stats

DB_PATH = r"D:\MotorDesign\KnowledgeBase\designs.db"
FAILURE_PATTERNS_PATH = r"D:\MotorDesign\Knowledge\failure_patterns.json"


def _load_failure_patterns() -> dict:
    if os.path.exists(FAILURE_PATTERNS_PATH):
        with open(FAILURE_PATTERNS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"patterns": [], "last_updated": datetime.now().isoformat()}


def _save_failure_patterns(data: dict):
    data["last_updated"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(FAILURE_PATTERNS_PATH), exist_ok=True)
    with open(FAILURE_PATTERNS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def on_adopt(design_id: int, specs: dict, reason: str = "",
             project_id: str = "") -> dict:
    """
    采纳方案时触发飞轮:
      - 记录反馈
      - 创建/强化技能模板 (权重+1)
      - 尝试挖掘新规则 (数据足够时)
      - 更新本体指标
    """
    result = {"design_id": design_id, "action": "adopt"}

    # 1. 记录反馈
    fb = record_feedback(design_id, "adopt", reason, project_id, specs)
    result["feedback"] = fb

    # 2. 获取方案详情
    solution = _get_design(design_id)
    if not solution:
        result["error"] = f"design {design_id} not found"
        return result

    # 3. 创建技能模板
    try:
        template = create_skill_template(project_id, specs, solution, reason)
        result["skill_template"] = template.get("template_id")
        result["template_name"] = template.get("name")
    except Exception as e:
        result["skill_template_error"] = str(e)

    # 4. 强化已存在的同类模板权重
    _boost_matching_templates(specs, solution)

    # 5. 尝试挖掘规则 (需3+采纳方案)
    try:
        rule = mine_design_rule(min_confidence=0.6)
        if rule:
            result["new_rule"] = rule
    except Exception as e:
        result["rule_mine_error"] = str(e)

    # 6. 更新本体指标
    try:
        ontology = load_ontology()
        update_metrics(ontology)
        save_ontology(ontology)
    except Exception:
        pass

    audit_logger.log(
        agent_name="Flywheel", action="on_adopt",
        parameters={"design_id": design_id, "project_id": project_id},
        result={"template": result.get("skill_template"),
                "new_rule": bool(result.get("new_rule"))},
        status="success", project_id=project_id,
    )

    return result


def on_reject(design_id: int, specs: dict, reason: str = "",
              project_id: str = "") -> dict:
    """
    拒绝方案时触发飞轮:
      - 记录反馈
      - 弱化同类技能模板权重 (权重-1, 累积失败特征)
      - 沉淀失败模式供逆向学习
    """
    result = {"design_id": design_id, "action": "reject"}

    # 1. 记录反馈
    fb = record_feedback(design_id, "reject", reason, project_id, specs)
    result["feedback"] = fb

    # 2. 获取方案详情
    solution = _get_design(design_id)
    if not solution:
        result["error"] = f"design {design_id} not found"
        return result

    # 3. 弱化同类模板
    _penalize_matching_templates(specs, solution)

    # 4. 沉淀失败特征
    failure = {
        "timestamp": datetime.now().isoformat(),
        "design_id": design_id,
        "project_id": project_id,
        "specs": specs,
        "solution_snapshot": {
            "stator_od": solution.get("stator_od"),
            "poles": solution.get("poles"),
            "efficiency": solution.get("efficiency"),
            "max_temp": solution.get("max_temp"),
            "torque": solution.get("torque"),
        },
        "reason": reason,
    }
    patterns = _load_failure_patterns()
    patterns["patterns"].append(failure)
    if len(patterns["patterns"]) > 200:
        patterns["patterns"] = patterns["patterns"][-200:]
    _save_failure_patterns(patterns)
    result["failure_pattern_recorded"] = True

    # 5. 更新本体指标
    try:
        ontology = load_ontology()
        update_metrics(ontology)
        save_ontology(ontology)
    except Exception:
        pass

    audit_logger.log(
        agent_name="Flywheel", action="on_reject",
        parameters={"design_id": design_id, "project_id": project_id,
                    "reason": reason[:100]},
        result={"failure_recorded": True},
        status="success", project_id=project_id,
    )

    return result


def _get_design(design_id: int) -> Optional[dict]:
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute(
            "SELECT stator_od, poles, speed, efficiency, max_temp, torque, "
            "slots_stator, slots_rotor, turns_per_coil, voltage_v, params "
            "FROM designs WHERE id = ?", (design_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "stator_od": row[0], "poles": row[1], "speed": row[2],
            "efficiency": row[3], "max_temp": row[4], "torque": row[5],
            "slots_stator": row[6], "slots_rotor": row[7],
            "turns_per_coil": row[8], "voltage_v": row[9],
        }
    finally:
        conn.close()


def _boost_matching_templates(specs: dict, solution: dict):
    """采纳时强化同类技能模板的 usage_count"""
    matches = find_matching_templates(specs, top_n=5)
    for m in matches:
        filepath = m.get("file", "")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    tmpl = json.load(f)
                tmpl["usage_count"] = tmpl.get("usage_count", 0) + 1
                tmpl["success_rate"] = round(
                    tmpl.get("success_rate", 1.0) * 0.9 + 0.1, 3)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(tmpl, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


def _penalize_matching_templates(specs: dict, solution: dict):
    """拒绝时弱化同类技能模板"""
    matches = find_matching_templates(specs, top_n=5)
    for m in matches:
        filepath = m.get("file", "")
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    tmpl = json.load(f)
                tmpl["usage_count"] = tmpl.get("usage_count", 0) + 1
                tmpl["success_rate"] = round(
                    max(0, tmpl.get("success_rate", 1.0) * 0.9 - 0.1), 3)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(tmpl, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


def get_flywheel_status() -> dict:
    """飞轮健康度: 采纳率/技能数/规则数/失败特征数/AI接管率"""
    stats = get_feedback_stats()
    ontology = load_ontology()
    update_metrics(ontology)
    patterns = _load_failure_patterns()

    # AI 接管率: 工作流自动完成步数 / 总决策点
    # 简化: 用采纳率反推 (采纳率越高说明AI生成质量越好)
    adoption_rate = stats.get("adoption_rate", 0)
    ai_intervention_rate = round(adoption_rate * 0.9, 1)  # 估算

    return {
        "adoption_rate": adoption_rate,
        "total_designs": stats.get("total_designs", 0),
        "adopted": stats.get("adopted", 0),
        "rejected": stats.get("rejected", 0),
        "skill_templates_count": ontology["metrics"].get("skill_templates_count", 0),
        "rules_count": ontology["metrics"].get("rules_count", 0),
        "failure_patterns_count": len(patterns.get("patterns", [])),
        "ai_intervention_rate": ai_intervention_rate,
        "feedback_trend": stats.get("feedback_trend", []),
        "ontology_version": ontology.get("version"),
        "last_updated": ontology.get("last_updated"),
    }
