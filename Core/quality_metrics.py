# -*- coding: utf-8 -*-
"""
运营质量指标模块 (Quality Metrics) v4.01.029
================================================
用户规格「二、3 运营指标接入」:
  · 人工修正率 = override_data 实际应用项数 / 该检查点暴露的总参数数
  · 返工率     = redo/reject 决策数 / 总决策数

数据来源: 各人机检查点(hc_requirement / hc_base_param / hc_design / hc_report)
落库后由运营分析师(OperationAnalyst)消费, 并向客户端运营面板/运营分析报告暴露。

附带闭环(P2): 高频被人工修正的参数 → 自动提升为强制追问规则(forced_questions.json),
使"人工修正"沉淀为"下次主动追问", 减少重复返工。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "KnowledgeBase", "designs_dev.db"),
)
FORCED_QUESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Knowledge", "forced_questions.json")

# 判定为"返工"的决策
REWORK_DECISIONS = ("redo", "reject")
# 自动转追问规则的阈值
PROMOTE_MIN_PROJECTS = 3      # 至少出现在N个不同项目中
PROMOTE_MIN_RATE = 0.40       # 修正率≥40% 才提升为强制追问

# 参数中文名(用于生成追问文案)
_PARAM_CN = {
    "motor_type": "电机类型", "power_kw": "额定功率", "voltage_v": "额定电压",
    "speed_rpm": "额定转速", "frequency_hz": "频率", "poles": "极数",
    "torque_nm": "额定转矩", "cooling": "冷却方式", "protection_class": "防护等级",
    "insulation_class": "绝缘等级", "duty_cycle": "工作制", "mounting": "安装方式",
    "efficiency_class": "能效等级", "altitude_m": "海拔高度",
    "ambient_temp_c": "环境温度", "overload_ratio": "过载倍数",
}


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS human_quality_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            checkpoint_type TEXT NOT NULL,
            checkpoint_id TEXT,
            decision TEXT NOT NULL,
            is_rework INTEGER DEFAULT 0,
            is_timeout INTEGER DEFAULT 0,
            total_params INTEGER DEFAULT 0,
            override_applied INTEGER DEFAULT 0,
            corrected_keys TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hqe_project "
                 "ON human_quality_events(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hqe_created "
                 "ON human_quality_events(created_at)")
    conn.commit()
    return conn


# ============================================================
# 写入
# ============================================================
def record_decision_event(project_id: str,
                          checkpoint_type: str,
                          decision: str,
                          checkpoint_id: str = "",
                          total_params: int = 0,
                          override_applied: int = 0,
                          corrected_keys: Optional[List[str]] = None,
                          is_timeout: bool = False) -> int:
    """记录一次人机检查点决策事件 (失败静默, 绝不影响主流程)"""
    try:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO human_quality_events
                   (project_id, checkpoint_type, checkpoint_id, decision,
                    is_rework, is_timeout, total_params, override_applied,
                    corrected_keys, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (project_id or "", checkpoint_type or "", checkpoint_id or "",
                 decision or "",
                 1 if (decision or "") in REWORK_DECISIONS else 0,
                 1 if is_timeout else 0,
                 int(total_params or 0), int(override_applied or 0),
                 json.dumps(corrected_keys or [], ensure_ascii=False),
                 datetime.now().isoformat())
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return -1


# ============================================================
# 读取 / 聚合
# ============================================================
def _aggregate(rows: List[tuple]) -> Dict[str, Any]:
    """rows: (checkpoint_type, decision, is_rework, is_timeout,
              total_params, override_applied, corrected_keys)"""
    total_decisions = len(rows)
    rework = sum(r[2] for r in rows)
    timeout = sum(r[3] for r in rows)
    total_params = sum(r[4] for r in rows)
    override_applied = sum(r[5] for r in rows)

    by_cp: Dict[str, Dict[str, Any]] = {}
    key_counter: Dict[str, int] = {}
    for r in rows:
        cp = r[0] or "unknown"
        slot = by_cp.setdefault(cp, {"decisions": 0, "rework": 0,
                                     "total_params": 0, "override_applied": 0})
        slot["decisions"] += 1
        slot["rework"] += r[2]
        slot["total_params"] += r[4]
        slot["override_applied"] += r[5]
        try:
            for k in json.loads(r[6] or "[]"):
                key_counter[k] = key_counter.get(k, 0) + 1
        except Exception:
            pass

    for cp, slot in by_cp.items():
        slot["rework_rate"] = round(slot["rework"] / max(slot["decisions"], 1), 4)
        slot["correction_rate"] = round(
            slot["override_applied"] / max(slot["total_params"], 1), 4)

    top_keys = sorted(key_counter.items(), key=lambda x: -x[1])[:10]

    return {
        "total_decisions": total_decisions,
        "rework_count": rework,
        "rework_rate": round(rework / max(total_decisions, 1), 4),
        "timeout_count": timeout,
        "timeout_rate": round(timeout / max(total_decisions, 1), 4),
        "total_params": total_params,
        "override_applied": override_applied,
        "correction_rate": round(override_applied / max(total_params, 1), 4),
        "by_checkpoint": by_cp,
        "top_corrected_params": [
            {"param": k, "param_cn": _PARAM_CN.get(k, k), "count": c}
            for k, c in top_keys
        ],
    }


_SELECT_COLS = ("checkpoint_type, decision, is_rework, is_timeout, "
                "total_params, override_applied, corrected_keys")


def get_project_metrics(project_id: str) -> Dict[str, Any]:
    """单项目运营质量指标"""
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM human_quality_events WHERE project_id=?",
                (project_id,)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e), "total_decisions": 0,
                "rework_rate": 0.0, "correction_rate": 0.0}
    out = _aggregate(rows)
    out["project_id"] = project_id
    out["scope"] = "project"
    return out


def get_global_metrics(days: int = 30) -> Dict[str, Any]:
    """近N天全局运营质量指标"""
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = _get_conn()
        try:
            rows = conn.execute(
                f"SELECT {_SELECT_COLS} FROM human_quality_events WHERE created_at>=?",
                (since,)).fetchall()
            proj_cnt = conn.execute(
                "SELECT COUNT(DISTINCT project_id) FROM human_quality_events "
                "WHERE created_at>=?", (since,)).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e), "total_decisions": 0,
                "rework_rate": 0.0, "correction_rate": 0.0}
    out = _aggregate(rows)
    out["scope"] = "global"
    out["days"] = days
    out["project_count"] = proj_cnt
    return out


# ============================================================
# P2 闭环: 人工修正 → 强制追问规则
# ============================================================
def promote_corrections_to_questions(days: int = 90,
                                     min_projects: int = PROMOTE_MIN_PROJECTS,
                                     min_rate: float = PROMOTE_MIN_RATE,
                                     apply: bool = False) -> Dict[str, Any]:
    """高频被人工修正的参数 → 建议(或直接写入)强制追问规则

    Returns: {"candidates":[...], "applied":[...]}
    """
    result: Dict[str, Any] = {"candidates": [], "applied": []}
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT project_id, corrected_keys FROM human_quality_events "
                "WHERE created_at>=?", (since,)).fetchall()
        finally:
            conn.close()

        projects = {r[0] for r in rows if r[0]}
        total_projects = max(len(projects), 1)
        param_projects: Dict[str, set] = {}
        for pid, keys in rows:
            try:
                for k in json.loads(keys or "[]"):
                    param_projects.setdefault(k, set()).add(pid)
            except Exception:
                continue

        for param, pids in param_projects.items():
            rate = len(pids) / total_projects
            if len(pids) >= min_projects and rate >= min_rate:
                result["candidates"].append({
                    "param": param,
                    "param_cn": _PARAM_CN.get(param, param),
                    "projects": len(pids),
                    "rate": round(rate, 4),
                })
        result["candidates"].sort(key=lambda x: -x["rate"])

        if apply and result["candidates"]:
            result["applied"] = _write_forced_questions(result["candidates"])
    except Exception as e:
        result["error"] = str(e)
    return result


def _write_forced_questions(candidates: List[Dict[str, Any]]) -> List[str]:
    """把候选参数并入 forced_questions.json (已存在则跳过)"""
    applied: List[str] = []
    try:
        if os.path.exists(FORCED_QUESTIONS_PATH):
            with open(FORCED_QUESTIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"version": "auto", "rules": []}
        if isinstance(data, list):          # 兼容旧格式(裸列表)
            data = {"version": "auto", "rules": data}
        rules = data.setdefault("rules", [])
        existing = {r.get("param") for r in rules if isinstance(r, dict)}

        for c in candidates:
            if c["param"] in existing:
                continue
            rules.append({
                "param": c["param"],
                "required": False,
                "keywords": [],
                "message": (f"请明确「{c['param_cn']}」— 该参数在近期 "
                            f"{int(c['rate']*100)}% 的项目中被人工修正, "
                            f"建议在需求中直接给出"),
                "frequency": c["rate"],
                "trigger_count": c["projects"],
                "source": "auto_from_corrections",
                "created_at": datetime.now().isoformat(),
            })
            applied.append(c["param"])

        if applied:
            data["updated_at"] = datetime.now().isoformat()
            with open(FORCED_QUESTIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        return applied
    return applied


if __name__ == "__main__":
    record_decision_event("PROJ_TEST_QM", "requirement_confirm", "approve",
                          total_params=20, override_applied=3,
                          corrected_keys=["cooling", "poles", "motor_type"])
    record_decision_event("PROJ_TEST_QM", "requirement_confirm", "redo",
                          total_params=20, override_applied=0)
    print(json.dumps(get_project_metrics("PROJ_TEST_QM"),
                     ensure_ascii=False, indent=2))
    print(json.dumps(get_global_metrics(30), ensure_ascii=False, indent=2))
