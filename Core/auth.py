"""
auth.py - 多租户认证模块 (v2.03.01)

功能:
- JWT token 生成/验证
- 密码哈希 (werkzeug.security)
- 角色分级: guest / user / vip / flagship / admin
- @require_auth / @require_role 装饰器
- dev 环境: 自动登录为 admin (可通过登出按钮退出, 用 admin/admin123 重新登录)
- trial 环境: 需手动登录 (首运行自动创建 admin 用户, admin/admin123)

设计原则:
- 无状态 JWT (不依赖服务端 session 存储)
- token 有效期 7 天
- dev 和 trial 各自独立数据库, admin 用户互相独立
"""
import os
import sys
import jwt
import uuid
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import request, jsonify, g

# 项目路径
_PROJECT_ROOT = r"D:\MotorDesign"
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, "Scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from config.env import Config
import db_tools

# ============================================================
# 常量
# ============================================================
JWT_SECRET = os.getenv("JWT_SECRET", "motor-ai-v2-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7

# 角色等级 (数值越大权限越高)
ROLE_LEVELS = {
    "guest": 0,
    "user": 10,
    "vip": 20,
    "flagship": 30,
    "admin": 100,
}

# 会员等级
MEMBERSHIP_LEVELS = {
    "trial": {"name": "试用用户", "daily_solve_limit": 3, "points_rate": 1.0},
    "standard": {"name": "普通用户", "daily_solve_limit": 1, "points_rate": 1.0},
    "vip": {"name": "VIP 用户", "daily_solve_limit": -1, "points_rate": 2.0},  # -1 = 不限
    "flagship": {"name": "旗舰用户", "daily_solve_limit": -1, "points_rate": 2.0},
}

# 公共 API 路径 (不需要认证)
PUBLIC_PATHS = {
    "/", "/api/status", "/api/env", "/api/auth/register", "/api/auth/login",
    "/api/auth/verify", "/api/test-cases", "/api/network/qrcode",
    "/api/progress/stream", "/api/progress/status",
}

# 静态资源前缀 (不需要认证)
PUBLIC_PREFIXES = ("/static/", "/api/network/")


# ============================================================
# 密码哈希
# ============================================================
def hash_password(password: str) -> str:
    """生成密码哈希"""
    return generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码"""
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


# ============================================================
# JWT Token
# ============================================================
def generate_token(user_id: int, username: str, role: str, membership_level: str = "trial") -> str:
    """生成 JWT token"""
    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "membership_level": membership_level,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRY_DAYS),
        "jti": str(uuid.uuid4())[:8],
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict:
    """验证 JWT token, 返回 payload 或 None"""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def extract_token_from_request() -> str:
    """从请求头提取 Bearer token"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


# ============================================================
# 用户上下文加载 (before_request 钩子)
# ============================================================
def load_user_context():
    """Flask before_request 钩子: 从 JWT 加载用户上下文到 g.user

    dev 环境: 如果没有用户且未手动登出, 自动创建 admin 并自动登录
    trial 环境: 仅从 JWT token 加载用户 (首运行自动创建 admin 但不自动登录)
    """
    g.user = None
    g.token_payload = None

    # 两个环境都确保 admin 用户存在 (各自独立数据库)
    _ensure_admin()

    # dev 环境自动登录 (除非用户手动登出设置了 cookie)
    if Config.IS_DEV and not _is_dev_logged_out():
        admin = db_tools.get_user_by_username("admin")
        if admin:
            g.user = admin
            g.token_payload = {
                "user_id": admin["id"],
                "username": admin["username"],
                "role": admin["role"],
                "membership_level": admin.get("membership_level", "trial"),
            }
            return

    # 从 JWT token 加载用户 (trial 或 dev 手动登出后)
    token = extract_token_from_request()
    if token:
        payload = verify_token(token)
        if payload:
            user = db_tools.get_user_by_id(payload["user_id"])
            if user and user.get("status") == "active":
                g.user = user
                g.token_payload = payload


def _is_dev_logged_out() -> bool:
    """检查 dev 环境用户是否手动登出 (cookie 标志)"""
    from flask import request as _req
    return _req.cookies.get("motorai_skip_auto") == "1"


def _ensure_admin():
    """确保 admin 用户存在 (dev 和 trial 各自独立数据库, 各自独立的 admin)

    dev: admin/admin123, 自动登录, 99999 积分
    trial: admin/admin123, 需手动登录, 99999 积分
    """
    admin = db_tools.get_user_by_username("admin")
    if not admin:
        display_name = "开发者 (Admin)" if Config.IS_DEV else "管理员 (Admin)"
        db_tools.create_user(
            username="admin",
            email="admin@motor-ai.local",
            password_hash=hash_password("admin123"),
            display_name=display_name,
            role="admin",
            membership_level="flagship",
        )
        env_label = "dev" if Config.IS_DEV else "trial"
        print(f"[auth] {env_label} 环境: 已自动创建 admin 用户 (admin/admin123)")
        # 给 admin 补充积分
        admin = db_tools.get_user_by_username("admin")
        if admin:
            db_tools.update_user(admin["id"], points_balance=99999)
    else:
        # 确保已有 admin 用户的角色和权限正确
        needs_update = {}
        if admin.get("role") != "admin":
            needs_update["role"] = "admin"
        if admin.get("membership_level") != "flagship":
            needs_update["membership_level"] = "flagship"
        if not admin.get("password_hash"):
            needs_update["password_hash"] = hash_password("admin123")
        if needs_update:
            db_tools.update_user(admin["id"], **needs_update)
            env_label = "dev" if Config.IS_DEV else "trial"
            print(f"[auth] {env_label} 环境: 已修正 admin 用户属性: {list(needs_update.keys())}")


# ============================================================
# 认证装饰器
# ============================================================
def require_auth(f):
    """要求用户已认证"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if g.user is None:
            return jsonify({"error": "未登录", "code": "UNAUTHORIZED"}), 401
        return f(*args, **kwargs)
    return decorated


def require_role(min_role: str):
    """要求用户达到指定角色等级"""
    min_level = ROLE_LEVELS.get(min_role, 0)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.user is None:
                return jsonify({"error": "未登录", "code": "UNAUTHORIZED"}), 401
            user_level = ROLE_LEVELS.get(g.user.get("role", "guest"), 0)
            if user_level < min_level:
                return jsonify({"error": "权限不足", "code": "FORBIDDEN",
                               "required_role": min_role}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_admin(f):
    """要求管理员权限"""
    return require_role("admin")(f)


def is_path_public(path: str) -> bool:
    """检查路径是否不需要认证"""
    if path in PUBLIC_PATHS:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def is_dev_auto_login() -> bool:
    """dev 环境是否正在自动登录 (dev + 未手动登出)"""
    return Config.IS_DEV and not _is_dev_logged_out()


def should_show_admin_hint() -> bool:
    """是否显示管理员凭据提示 (dev 始终显示, trial 首次运行时显示)"""
    if Config.IS_DEV:
        return True
    # trial: 仅当只有 admin 一个用户时显示 (首次运行)
    try:
        users = db_tools.get_all_users()
        return len(users) <= 1
    except Exception:
        return False


# ============================================================
# 用户信息提取 (供 API 使用)
# ============================================================
def current_user_id() -> int:
    """获取当前用户 ID (未登录返回 None)"""
    if g.user:
        return g.user["id"]
    return None


def current_user_dict() -> dict:
    """获取当前用户信息字典 (脱敏, 不含 password_hash)"""
    if not g.user:
        return None
    u = g.user
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u.get("email"),
        "display_name": u.get("display_name"),
        "role": u.get("role", "guest"),
        "membership_level": u.get("membership_level", "trial"),
        "points_balance": u.get("points_balance", 0),
        "status": u.get("status", "active"),
        "last_login": u.get("last_login"),
        "created_time": u.get("created_time"),
    }


def get_membership_info(level: str) -> dict:
    """获取会员等级信息"""
    info = MEMBERSHIP_LEVELS.get(level, MEMBERSHIP_LEVELS["trial"])
    return {"level": level, **info}
