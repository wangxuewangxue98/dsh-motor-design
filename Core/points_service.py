"""
points_service.py - 积分体系服务 (v2.02.01)

功能:
- 签到 (每日 +10)
- 积分获取/消耗记录
- 积分余额查询
- 签到防重 (每日仅一次)

积分规则:
  获取: 签到 +10 | 客户端在线 +2/小时 | 贡献算力 +5/次 | 被他人使用 +10/次 | 推荐新用户 +200
  消耗: 网页求解 50 | 云算力加速 100 | VIP体验 500 | 技术服务抵扣 100=10元
"""
import os
import sys
import sqlite3
from datetime import datetime, date

_PROJECT_ROOT = r"D:\MotorDesign"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, "Scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import db_tools

# 积分规则常量
POINTS_DAILY_CHECKIN = 10
POINTS_CLIENT_ONLINE_HOUR = 2
POINTS_COMPUTE_CONTRIBUTE = 5
POINTS_USED_BY_OTHERS = 10
POINTS_REFER_USER = 200

POINTS_SOLVE_WEB = 50
POINTS_CLOUD_ACCELERATE = 100
POINTS_VIP_TRIAL = 500


def add_points(user_id: int, amount: int, reason: str) -> int:
    """增加积分, 返回新余额"""
    with sqlite3.connect(db_tools.DB_PATH) as conn:
        # 获取当前余额
        row = conn.execute("SELECT points_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return -1
        current_balance = row[0] or 0
        new_balance = current_balance + amount

        # 记录积分变动
        conn.execute(
            """INSERT INTO points (user_id, points, reason, type, balance_after)
               VALUES (?, ?, ?, 'earn', ?)""",
            (user_id, amount, reason, new_balance)
        )
        # 更新余额
        conn.execute("UPDATE users SET points_balance = ? WHERE id = ?", (new_balance, user_id))
        conn.commit()
        return new_balance


def spend_points(user_id: int, amount: int, reason: str) -> dict:
    """消耗积分, 返回 {success, balance, error}"""
    with sqlite3.connect(db_tools.DB_PATH) as conn:
        row = conn.execute("SELECT points_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"success": False, "error": "用户不存在"}
        current_balance = row[0] or 0
        if current_balance < amount:
            return {"success": False, "error": "积分不足", "balance": current_balance}

        new_balance = current_balance - amount
        conn.execute(
            """INSERT INTO points (user_id, points, reason, type, balance_after)
               VALUES (?, ?, ?, 'spend', ?)""",
            (user_id, -amount, reason, new_balance)
        )
        conn.execute("UPDATE users SET points_balance = ? WHERE id = ?", (new_balance, user_id))
        conn.commit()
        return {"success": True, "balance": new_balance}


def get_balance(user_id: int) -> int:
    """获取积分余额"""
    with sqlite3.connect(db_tools.DB_PATH) as conn:
        row = conn.execute("SELECT points_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else 0


def get_points_history(user_id: int, limit: int = 50) -> list:
    """获取积分变动记录"""
    with sqlite3.connect(db_tools.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM points WHERE user_id = ? ORDER BY created_time DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def check_daily_checkin(user_id: int) -> bool:
    """检查今日是否已签到"""
    today = date.today().isoformat()
    with sqlite3.connect(db_tools.DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM points WHERE user_id = ? AND reason LIKE ? AND date(created_time) = ?",
            (user_id, "%每日签到%", today)
        ).fetchone()
        return row[0] > 0


def do_daily_checkin(user_id: int) -> dict:
    """每日签到, 返回 {success, points, balance, error}"""
    if check_daily_checkin(user_id):
        return {"success": False, "error": "今日已签到", "balance": get_balance(user_id)}

    reason = f"每日签到 ({datetime.now().strftime('%Y-%m-%d')})"
    new_balance = add_points(user_id, POINTS_DAILY_CHECKIN, reason)
    return {"success": True, "points": POINTS_DAILY_CHECKIN, "balance": new_balance,
            "reason": reason}
