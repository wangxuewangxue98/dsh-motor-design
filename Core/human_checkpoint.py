"""
人机协作检查点 - 支持超时默认机制
一人公司四大部门的工作流中,凡需人工确认的节点均通过此模块阻塞等待
"""
import json
import sqlite3
import os
import time
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

# 默认超时时间 (秒) —— 对应用户方案中的四个部门
DEFAULT_TIMEOUTS = {
    "customer_service_confirm": 2 * 3600,      # 客服:需求确认 2小时
    "customer_service_followup": 30 * 60,      # 客服:追问草稿 30分钟
    "customer_service_weekly": 1 * 3600,       # 客服:周报 1小时
    "customer_service_delivery": 4 * 3600,     # 客服:交付验收 4小时
    "design_final_select": 24 * 3600,          # 设计:方案终选 24小时
    "marketing_evaluate": 72 * 3600,           # 营销:机会评估 72小时
    "marketing_publish": 48 * 3600,            # 营销:内容发布 48小时
    "management_quote": 24 * 3600,             # 管理:报价确认 24小时
    "management_contract": 12 * 3600,          # 管理:合同审核 12小时
}

# 默认动作映射
DEFAULT_ACTIONS = {
    "customer_service_confirm": "auto_confirm",
    "customer_service_followup": "auto_send",
    "customer_service_weekly": "auto_send",
    "customer_service_delivery": "auto_pass",
    "design_final_select": "auto_first",
    "marketing_evaluate": "auto_ignore",
    "marketing_publish": "auto_hold",
    "management_quote": "auto_medium",
    "management_contract": "auto_pass",
}

_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}  # 内存中的待处理检查点 (checkpoint_id -> info)


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS human_checkpoints (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            checkpoint_type TEXT NOT NULL,
            title TEXT,
            description TEXT,
            context TEXT,                  -- JSON
            status TEXT DEFAULT 'pending', -- pending/approved/rejected/overridden/timeout
            decision TEXT,
            comment TEXT,
            timeout_seconds INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            default_action TEXT,
            cost_event_id INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hc_project ON human_checkpoints(project_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hc_status ON human_checkpoints(status)
    """)
    conn.commit()
    return conn


def create_checkpoint(project_id: str, checkpoint_type: str,
                      title: str = "", description: str = "",
                      context: Optional[dict] = None,
                      timeout_seconds: Optional[int] = None,
                      default_action: Optional[str] = None) -> str:
    """创建人工检查点,返回 checkpoint_id"""
    checkpoint_id = f"HC_{datetime.now().strftime('%Y%m%d%H%M%S')}_{project_id}"
    timeout = timeout_seconds or DEFAULT_TIMEOUTS.get(checkpoint_type, 3600)
    default_act = default_action or DEFAULT_ACTIONS.get(checkpoint_type, "auto_pass")
    created_at = datetime.now().isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO human_checkpoints
               (id, project_id, checkpoint_type, title, description, context,
                status, timeout_seconds, created_at, default_action)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (checkpoint_id, project_id, checkpoint_type, title, description,
             json.dumps(context or {}, ensure_ascii=False, default=str),
             "pending", timeout, created_at, default_act)
        )
        conn.commit()
    finally:
        conn.close()

    with _lock:
        _pending[checkpoint_id] = {
            "project_id": project_id,
            "checkpoint_type": checkpoint_type,
            "status": "pending",
            "created_at": created_at,
            "timeout_seconds": timeout,
            "default_action": default_act,
            "context": context or {},
        }
    return checkpoint_id


def get_checkpoint(checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """获取检查点详情"""
    # 优先读内存
    with _lock:
        if checkpoint_id in _pending:
            return dict(_pending[checkpoint_id])

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, project_id, checkpoint_type, title, description, context, "
            "status, decision, comment, timeout_seconds, created_at, decided_at, default_action "
            "FROM human_checkpoints WHERE id = ?",
            (checkpoint_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "project_id": row[1], "checkpoint_type": row[2],
            "title": row[3], "description": row[4],
            "context": json.loads(row[5]) if row[5] else {},
            "status": row[6], "decision": row[7], "comment": row[8],
            "timeout_seconds": row[9], "created_at": row[10],
            "decided_at": row[11], "default_action": row[12],
        }
    finally:
        conn.close()


def submit_decision(checkpoint_id: str, decision: str,
                    comment: str = "", cost_event_id: Optional[int] = None) -> bool:
    """提交人工决策"""
    allowed = {"approve", "reject", "override", "modify", "confirm", "ignore", "hold"}
    if decision not in allowed:
        return False

    with _lock:
        if checkpoint_id in _pending:
            _pending[checkpoint_id]["status"] = decision
            _pending[checkpoint_id]["decision"] = decision
            _pending[checkpoint_id]["comment"] = comment
            _pending[checkpoint_id]["decided_at"] = datetime.now().isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE human_checkpoints
               SET status = ?, decision = ?, comment = ?, decided_at = ?, cost_event_id = ?
               WHERE id = ? AND status = 'pending'""",
            (decision, decision, comment, datetime.now().isoformat(), cost_event_id, checkpoint_id)
        )
        updated = conn.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def wait_for_decision(checkpoint_id: str, poll_interval: float = 1.0,
                      on_progress: Optional[callable] = None) -> Dict[str, Any]:
    """
    阻塞等待人工决策或超时。
    返回: {"status": "approved"/"timeout"/..., "decision": ..., "comment": ..., "wait_seconds": ...}
    """
    cp = get_checkpoint(checkpoint_id)
    if not cp:
        return {"status": "not_found", "decision": "", "comment": "", "wait_seconds": 0}

    timeout_seconds = cp.get("timeout_seconds", 3600)
    default_action = cp.get("default_action", "auto_pass")
    start = datetime.now()

    while True:
        # 检查是否已有决策
        fresh = get_checkpoint(checkpoint_id)
        if fresh and fresh.get("status") not in ("pending", ""):
            decided_at = fresh.get("decided_at")
            wait_seconds = (datetime.fromisoformat(decided_at) - start).total_seconds() if decided_at else 0
            return {
                "status": fresh["status"],
                "decision": fresh.get("decision", ""),
                "comment": fresh.get("comment", ""),
                "wait_seconds": wait_seconds,
            }

        elapsed = (datetime.now() - start).total_seconds()
        if elapsed >= timeout_seconds:
            # 超时: 应用默认动作
            submit_decision(checkpoint_id, default_action,
                            comment=f"超时 {int(elapsed)} 秒, 自动执行默认动作", cost_event_id=None)
            return {
                "status": default_action,
                "decision": default_action,
                "comment": f"超时 {int(elapsed)} 秒, 自动执行默认动作",
                "wait_seconds": elapsed,
                "timeout": True,
            }

        if on_progress:
            remaining = int(timeout_seconds - elapsed)
            on_progress(remaining)

        time.sleep(poll_interval)


def list_pending_checkpoints(project_id: Optional[str] = None) -> list:
    """列出待处理检查点"""
    conn = _get_conn()
    try:
        if project_id:
            rows = conn.execute(
                "SELECT id, project_id, checkpoint_type, title, status, created_at, timeout_seconds "
                "FROM human_checkpoints WHERE status = 'pending' AND project_id = ? "
                "ORDER BY created_at DESC",
                (project_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, checkpoint_type, title, status, created_at, timeout_seconds "
                "FROM human_checkpoints WHERE status = 'pending' "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r[0], "project_id": r[1], "checkpoint_type": r[2],
                "title": r[3], "status": r[4], "created_at": r[5],
                "timeout_seconds": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_decision_context(checkpoint_id: str) -> dict:
    """获取决策上下文,供前端展示"""
    cp = get_checkpoint(checkpoint_id)
    if not cp:
        return {}
    return {
        "checkpoint_id": cp["id"],
        "project_id": cp["project_id"],
        "title": cp.get("title", ""),
        "description": cp.get("description", ""),
        "options": ["approve", "reject", "override"],
        "context": cp.get("context", {}),
        "timeout_seconds": cp.get("timeout_seconds", 3600),
        "default_action": cp.get("default_action", "auto_pass"),
    }


if __name__ == "__main__":
    # 自测
    cp_id = create_checkpoint(
        project_id="PROJ_TEST_002",
        checkpoint_type="design_final_select",
        title="方案终选确认",
        description="请审阅支持报告与反对报告,选择最终方案",
        context={"review": "支持...", "con_report": "反对..."},
        timeout_seconds=5
    )
    print("created:", cp_id)
    # 模拟外部在2秒后提交决策
    def submit_later():
        time.sleep(2)
        submit_decision(cp_id, "approve", "同意AI推荐")
    threading.Thread(target=submit_later, daemon=True).start()
    result = wait_for_decision(cp_id, poll_interval=0.5, on_progress=lambda r: print(f"remaining {r}s"))
    print("result:", result)
