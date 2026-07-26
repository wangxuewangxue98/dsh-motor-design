"""
本体进化引擎 - 从历史项目中挖掘规律，更新本体约束
驱动数据飞轮的核心机制
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

ONTOLOGY_PATH = r"D:\MotorDesign\Ontology\motor_ontology.json"
DB_PATH = r"D:\MotorDesign\KnowledgeBase\designs.db"
SKILL_DIR = r"D:\MotorDesign\Knowledge\skill_templates"


def load_ontology() -> dict:
    with open(ONTOLOGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_ontology(ontology: dict):
    ontology["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    with open(ONTOLOGY_PATH, "w", encoding="utf-8") as f:
        json.dump(ontology, f, ensure_ascii=False, indent=2)


def update_metrics(ontology: dict) -> dict:
    """从数据库更新本体指标"""
    if not os.path.exists(DB_PATH):
        return ontology

    conn = sqlite3.connect(DB_PATH)
    try:
        # 确保有 adopted 列
        try:
            conn.execute("ALTER TABLE designs ADD COLUMN adopted INTEGER DEFAULT 0")
        except:
            pass
        try:
            conn.execute("ALTER TABLE designs ADD COLUMN feedback TEXT")
        except:
            pass
        try:
            conn.execute("ALTER TABLE designs ADD COLUMN project_id TEXT")
        except:
            pass
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        adopted = conn.execute("SELECT COUNT(*) FROM designs WHERE adopted = 1").fetchone()[0]

        # 计算平均效率
        avg_eff = conn.execute("SELECT AVG(efficiency) FROM designs").fetchone()[0] or 0
        max_eff = conn.execute("SELECT MAX(efficiency) FROM designs").fetchone()[0] or 0

        ontology["metrics"]["total_designs"] = total
        ontology["metrics"]["adopted_designs"] = adopted
        ontology["metrics"]["adoption_rate"] = round(adopted / max(total, 1) * 100, 1)
        ontology["metrics"]["rules_count"] = len(ontology["entities"]["DesignRule"]["rules"])
        ontology["metrics"]["skill_templates_count"] = len(ontology["entities"]["SkillTemplate"]["templates"])
    finally:
        conn.close()

    return ontology


def create_skill_template(project_id: str, specs: dict, solution: dict, feedback: str = ""):
    """从成功方案创建技能模板"""
    app_type = specs.get("application", "通用")
    template = {
        "template_id": f"SKILL_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "name": f"{app_type}_{specs.get('power_kw', '?')}kW_{specs.get('voltage_v', '?')}V",
        "application": app_type,
        "power_kw": specs.get("power_kw"),
        "voltage_v": specs.get("voltage_v"),
        "cooling": specs.get("cooling"),
        "recommended_params": {
            "stator_od": solution.get("stator_od"),
            "poles": solution.get("poles"),
            "speed": solution.get("speed")
        },
        "performance": {
            "efficiency": solution.get("efficiency"),
            "max_temp": solution.get("max_temp"),
            "torque": solution.get("torque")
        },
        "feedback": feedback,
        "created_time": datetime.now().isoformat(),
        "usage_count": 0,
        "success_rate": 1.0
    }

    # 保存到文件
    os.makedirs(SKILL_DIR, exist_ok=True)
    filepath = os.path.join(SKILL_DIR, f"{template['template_id']}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)

    # 更新本体
    ontology = load_ontology()
    ontology["entities"]["SkillTemplate"]["templates"].append({
        "template_id": template["template_id"],
        "name": template["name"],
        "application": app_type,
        "file": filepath
    })
    update_metrics(ontology)
    save_ontology(ontology)

    return template


def find_matching_templates(specs: dict, top_n: int = 3) -> list:
    """根据需求规格查找匹配的技能模板"""
    ontology = load_ontology()
    templates_meta = ontology["entities"]["SkillTemplate"]["templates"]
    if not templates_meta:
        return []

    matches = []
    for meta in templates_meta:
        filepath = meta.get("file", "")
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            template = json.load(f)

        score = 0
        if template.get("application") == specs.get("application"):
            score += 3
        if template.get("power_kw") == specs.get("power_kw"):
            score += 2
        if template.get("voltage_v") == specs.get("voltage_v"):
            score += 2
        if template.get("cooling") == specs.get("cooling"):
            score += 1

        if score > 0:
            template["match_score"] = score
            matches.append(template)

    matches.sort(key=lambda x: x["match_score"], reverse=True)
    return matches[:top_n]


def mine_design_rule(min_confidence: float = 0.6) -> Optional[dict]:
    """从历史数据中挖掘设计规则"""
    if not os.path.exists(DB_PATH):
        return None

    conn = sqlite3.connect(DB_PATH)
    try:
        # 找出被采纳方案的共同特征
        rows = conn.execute("""
            SELECT params, efficiency, max_temp, torque, adopted
            FROM designs WHERE adopted = 1
            ORDER BY efficiency DESC LIMIT 20
        """).fetchall()
    finally:
        conn.close()

    if len(rows) < 3:
        return None

    # 简单规则挖掘：统计被采纳方案的极数分布
    pole_counts = {}
    for r in rows:
        try:
            params = eval(r[0]) if isinstance(r[0], str) else r[0]
            poles = params.get("poles")
            if poles:
                pole_counts[poles] = pole_counts.get(poles, 0) + 1
        except:
            pass

    if pole_counts:
        best_pole = max(pole_counts, key=pole_counts.get)
        if pole_counts[best_pole] >= 2:
            rule = {
                "id": f"RULE_{datetime.now().strftime('%m%d%H%M')}",
                "condition": f"adopted == true",
                "constraint": f"被采纳方案中 {pole_counts[best_pole]}/{len(rows)} 使用 {best_pole} 极",
                "confidence": round(pole_counts[best_pole] / len(rows), 2),
                "source_projects": len(rows),
                "last_validated": datetime.now().strftime("%Y-%m-%d")
            }

            ontology = load_ontology()
            ontology["entities"]["DesignRule"]["rules"].append(rule)
            update_metrics(ontology)

            # 记录进化历史
            ontology["evolution_history"].append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "version": ontology["version"],
                "change": f"自动挖掘规则: {rule['constraint']}",
                "rules_count": len(ontology["entities"]["DesignRule"]["rules"]),
                "templates_count": len(ontology["entities"]["SkillTemplate"]["templates"])
            })
            save_ontology(ontology)
            return rule

    return None


def get_evolution_dashboard() -> dict:
    """获取进化仪表盘数据"""
    ontology = load_ontology()
    update_metrics(ontology)
    save_ontology(ontology)

    # 技能模板列表
    templates = []
    for meta in ontology["entities"]["SkillTemplate"]["templates"]:
        filepath = meta.get("file", "")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                templates.append(json.load(f))

    # 进化历史
    history = ontology.get("evolution_history", [])

    # 设计规则
    rules = ontology["entities"]["DesignRule"]["rules"]

    return {
        "metrics": ontology["metrics"],
        "skill_templates": templates,
        "design_rules": rules,
        "evolution_history": history,
        "ontology_version": ontology["version"],
        "last_updated": ontology["last_updated"]
    }
