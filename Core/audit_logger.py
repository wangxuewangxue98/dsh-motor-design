"""
审计日志系统 - 记录每个智能体的每一步操作
确保全流程可溯源、可审计
"""
import json
import sqlite3
import os
from datetime import datetime
from typing import Optional, Any

LOG_DIR = r"D:\MotorDesign\Logs"
DB_PATH = os.path.join(LOG_DIR, "audit.db")
TEXT_LOG_PATH = os.path.join(LOG_DIR, "audit.log")

os.makedirs(LOG_DIR, exist_ok=True)


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            action TEXT NOT NULL,
            parameters TEXT,
            result TEXT,
            status TEXT DEFAULT 'success',
            duration_ms INTEGER DEFAULT 0,
            project_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            calls INTEGER DEFAULT 0,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            avg_duration_ms REAL DEFAULT 0,
            UNIQUE(date, agent_name)
        )
    """)
    return conn


def log(agent_name: str, action: str, parameters: Any = None,
         result: Any = None, status: str = "success",
         duration_ms: int = 0, project_id: Optional[str] = None):
    """记录一条审计日志"""
    ts = datetime.now().isoformat()
    params_str = json.dumps(parameters, ensure_ascii=False, default=str) if parameters else ""
    result_str = json.dumps(result, ensure_ascii=False, default=str) if result else ""

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (timestamp, agent_name, action, parameters, result, status, duration_ms, project_id) VALUES (?,?,?,?,?,?,?,?)",
            (ts, agent_name, action, params_str, result_str, status, duration_ms, project_id)
        )
        conn.commit()
    finally:
        conn.close()

    # 同时写入文本日志（失败不阻断主流程：audit.db 已是审计溯源真源）
    try:
        log_line = f"[{ts}] [{agent_name}] [{status}] {action}"
        if params_str:
            log_line += f" | params={params_str[:200]}"
        if result_str:
            log_line += f" | result={result_str[:200]}"
        with open(TEXT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as _e:
        # 文本镜像写入失败（如权限不足/文件被占用）仅告警，不影响工作流继续执行
        print(f"[audit_logger][WARN] 文本日志写入失败（已忽略）: {_e}")


def get_recent_logs(limit: int = 50, offset: int = 0, agent: str = "",
                    action: str = "", start_date: str = "", end_date: str = "") -> list:
    """获取审计日志（支持过滤）"""
    conn = _get_conn()
    try:
        query = "SELECT id, timestamp, agent_name, action, parameters, result, status, duration_ms, project_id FROM audit_log WHERE 1=1"
        params = []
        if agent:
            query += " AND agent_name = ?"
            params.append(agent)
        if action:
            query += " AND action = ?"
            params.append(action)
        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)
        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r[0], "timestamp": r[1], "agent_name": r[2], "action": r[3],
                "parameters": r[4], "result": r[5], "status": r[6],
                "duration_ms": r[7], "project_id": r[8]
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_agent_metrics(days: int = 30) -> list:
    """获取智能体性能指标"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT agent_name, SUM(calls) as total_calls, SUM(successes) as total_success, SUM(failures) as total_fail, AVG(avg_duration_ms) as avg_dur FROM agent_metrics GROUP BY agent_name"
        ).fetchall()
        return [
            {
                "agent_name": r[0], "total_calls": r[1] or 0,
                "total_success": r[2] or 0, "total_fail": r[3] or 0,
                "avg_duration_ms": round(r[4] or 0, 1),
                "success_rate": round((r[2] or 0) / max(r[1] or 1, 1) * 100, 1)
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_stats() -> dict:
    """获取审计系统统计"""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        today_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()[0]
        agents = conn.execute("SELECT DISTINCT agent_name FROM audit_log").fetchall()
        return {
            "total_logs": total,
            "today_logs": today_count,
            "active_agents": len(agents),
            "agent_names": [a[0] for a in agents]
        }
    finally:
        conn.close()
