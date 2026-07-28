"""
task_manager.py - 全局任务管理器

管理所有耗时任务（求解、工作流）的生命周期：
  - 创建任务记录 (持久化到 task_queue 表)
  - 更新任务状态/进度/日志
  - 查询活跃任务
  - 取消任务

v1.03.03: 全局任务队列系统
"""
import sqlite3
import json
import uuid
import os
import time
from datetime import datetime


# 复用 db_tools 的数据库路径
_DB_PATH = os.getenv("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

# 任务状态常量
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 任务类型
TYPE_SOLVE = "solve"
TYPE_WORKFLOW = "workflow"


def _get_conn():
    """获取数据库连接"""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def create_task(
    task_type: str,
    source: str = "dev",
    user_id: str = "dev",
    priority: int = 5,
    specs: dict = None,
) -> str:
    """
    创建一个新任务，返回 task_id。

    Args:
        task_type: "solve" 或 "workflow"
        source: "dev" 或 "trial"
        user_id: 用户标识 (未来多租户)
        priority: 优先级 (0=最高, 10=最低)
        specs: 任务参数 (JSON 序列化存储)
    Returns:
        task_id (UUID 字符串)
    """
    task_id = str(uuid.uuid4())[:8]  # 短 UUID, 足够唯一且易读
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO task_queue (id, user_id, source, type, status, priority, specs, log)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, user_id, source, task_type,
            STATUS_QUEUED, priority,
            json.dumps(specs, ensure_ascii=False) if specs else None,
            json.dumps([], ensure_ascii=False),
        ))
        conn.commit()
    print(f"[task_manager] 任务已创建: {task_id} ({task_type}, source={source})")
    return task_id


def update_task(
    task_id: str,
    status: str = None,
    progress: float = None,
    current_step: str = None,
    log_entry: str = None,
    result_summary: str = None,
):
    """
    更新任务状态。只更新非 None 的字段。

    Args:
        task_id: 任务 ID
        status: 新状态 (queued/running/completed/failed/cancelled)
        progress: 进度 0.0~1.0
        current_step: 当前步骤描述
        log_entry: 追加到日志的消息 (不是替换)
        result_summary: 任务完成后的摘要
    """
    fields = []
    values = []

    if status is not None:
        fields.append("status = ?")
        values.append(status)

    if progress is not None:
        fields.append("progress = ?")
        values.append(round(progress, 4))

    if current_step is not None:
        fields.append("current_step = ?")
        values.append(current_step)

    if result_summary is not None:
        fields.append("result_summary = ?")
        values.append(result_summary)

    # 追加日志条目
    if log_entry is not None:
        fields.append("log = ?")
        # 读取现有日志, 追加新条目
        with _get_conn() as conn:
            row = conn.execute("SELECT log FROM task_queue WHERE id = ?", (task_id,)).fetchone()
            existing_log = json.loads(row["log"]) if row and row["log"] else []
            existing_log.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg": log_entry,
            })
            # 保留最近 100 条
            if len(existing_log) > 100:
                existing_log = existing_log[-100:]
            values.append(json.dumps(existing_log, ensure_ascii=False))

    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.append(task_id)

    with _get_conn() as conn:
        conn.execute(f"""
            UPDATE task_queue SET {', '.join(fields)}
            WHERE id = ?
        """, values)
        conn.commit()


def get_task(task_id: str) -> dict:
    """获取单个任务的详情"""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM task_queue WHERE id = ?", (task_id,)).fetchone()
        if row:
            return _row_to_dict(row)
    return None


def get_active_tasks() -> list:
    """获取所有活跃任务 (queued + running)"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status IN (?, ?) ORDER BY priority ASC, created_at ASC",
            (STATUS_QUEUED, STATUS_RUNNING)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_all_tasks(limit: int = 50) -> list:
    """获取所有任务 (按创建时间倒序)"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_queue ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_tasks_by_status(status: str, limit: int = 50) -> list:
    """按状态筛选任务"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_queue WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def cancel_task(task_id: str) -> bool:
    """
    取消任务。将状态标记为 cancelled。
    如果任务正在运行，调用方需要额外终止 Worker 进程/线程。

    Returns:
        True 如果成功取消, False 如果任务不存在或已完成
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM task_queue WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["status"] in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
            return False
        conn.execute(
            "UPDATE task_queue SET status = ?, current_step = ?, updated_at = ? WHERE id = ?",
            (STATUS_CANCELLED, "用户已终止", datetime.now().isoformat(), task_id)
        )
        conn.commit()
    print(f"[task_manager] 任务已取消: {task_id}")
    return True


def update_task_priority(task_id: str, priority: int) -> bool:
    """
    更新任务优先级 (0=最高, 10=最低)。

    Returns:
        True 如果成功, False 如果任务不存在
    """
    priority = max(0, min(10, priority))
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM task_queue WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE task_queue SET priority = ?, updated_at = ? WHERE id = ?",
            (priority, datetime.now().isoformat(), task_id)
        )
        conn.commit()
    print(f"[task_manager] 任务优先级已更新: {task_id} → {priority}")
    return True


def cleanup_old_tasks(days: int = 30) -> int:
    """清理超过指定天数的已完成/已取消任务，返回删除数量"""
    with _get_conn() as conn:
        cursor = conn.execute(
            """DELETE FROM task_queue
               WHERE status IN (?, ?, ?)
               AND created_at < datetime('now', ?)""",
            (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, f"-{days} days")
        )
        conn.commit()
        return cursor.rowcount


def _row_to_dict(row) -> dict:
    """将 sqlite3.Row 转为字典"""
    d = dict(row)
    # 解析 JSON 字段
    if d.get("log"):
        try:
            d["log"] = json.loads(d["log"])
        except (json.JSONDecodeError, TypeError):
            d["log"] = []
    else:
        d["log"] = []
    if d.get("specs"):
        try:
            d["specs"] = json.loads(d["specs"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d
