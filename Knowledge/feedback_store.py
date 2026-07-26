"""
反馈存储系统 - 记录方案采纳/拒绝决策
驱动数据飞轮的核心数据结构
"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = r"D:\MotorDesign\KnowledgeBase\designs.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    # 确保 designs 表有 adopted 和 feedback 字段
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

    # 反馈记录表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            design_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            project_id TEXT,
            specs_snapshot TEXT
        )
    """)
    return conn


def record_feedback(design_id: int, action: str, reason: str = "",
                    project_id: str = "", specs: dict = None) -> dict:
    """
    记录反馈
    action: "adopt" 或 "reject"
    """
    conn = _get_conn()
    try:
        ts = datetime.now().isoformat()

        # 记录反馈日志
        conn.execute(
            "INSERT INTO feedback_log (timestamp, design_id, action, reason, project_id, specs_snapshot) VALUES (?,?,?,?,?,?)",
            (ts, design_id, action, reason, project_id, json.dumps(specs or {}, ensure_ascii=False))
        )

        # 更新 designs 表
        adopted = 1 if action == "adopt" else 0
        conn.execute(
            "UPDATE designs SET adopted = ?, feedback = ? WHERE id = ?",
            (adopted, reason, design_id)
        )

        conn.commit()

        return {
            "design_id": design_id,
            "action": action,
            "reason": reason,
            "timestamp": ts,
            "success": True
        }
    finally:
        conn.close()


def get_feedback_stats() -> dict:
    """获取反馈统计"""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        adopted = conn.execute("SELECT COUNT(*) FROM designs WHERE adopted = 1").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM designs WHERE adopted = 0 AND feedback IS NOT NULL").fetchone()[0]

        # 采纳率趋势
        recent = conn.execute("""
            SELECT date(timestamp) as date, action, COUNT(*) as cnt
            FROM feedback_log
            GROUP BY date(timestamp), action
            ORDER BY date(timestamp)
            LIMIT 30
        """).fetchall()

        trend = []
        for r in recent:
            trend.append({"date": r[0], "action": r[1], "count": r[2]})

        return {
            "total_designs": total,
            "adopted": adopted,
            "rejected": rejected,
            "adoption_rate": round(adopted / max(total, 1) * 100, 1),
            "feedback_trend": trend
        }
    finally:
        conn.close()


def get_recent_feedback(limit: int = 20) -> list:
    """获取最近的反馈记录"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT f.id, f.timestamp, f.design_id, f.action, f.reason, f.project_id,
                   d.efficiency, d.max_temp, d.torque
            FROM feedback_log f
            LEFT JOIN designs d ON f.design_id = d.id
            ORDER BY f.id DESC LIMIT ?
        """, (limit,)).fetchall()

        return [
            {
                "id": r[0], "timestamp": r[1], "design_id": r[2],
                "action": r[3], "reason": r[4], "project_id": r[5],
                "efficiency": r[6], "max_temp": r[7], "torque": r[8]
            }
            for r in rows
        ]
    finally:
        conn.close()
