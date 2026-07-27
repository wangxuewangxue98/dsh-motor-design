"""
db_tools.py - 本地结果存储（SQLite）
所有求解结果存本地，支持查询、排序、导出 CSV

v1.01 更新:
- 新增字段: 槽配合、线圈参数、磁密、电密、发热因数、损耗等
- 新增 solve_mode 字段标记 Motor-CAD / 模拟模式
- 自动迁移旧数据库 (ALTER TABLE)

v1.03.01 更新:
- 数据库路径从 Config 获取 (dev/trial 环境隔离)
- 预建 users / points 表结构 (多租户预留)

v1.03.03 更新:
- 新增 task_queue 表 (全局任务队列)
- 启用 SQLite WAL 模式减少锁冲突

v2.02.01 更新:
- users 表扩展: password_hash, email, membership_level, points_balance, status 等
- 新建 product_lines 表 (产品线配置引擎)
- 新建 projects 表 (项目关联用户+产品线)
- designs 表增加 user_id + project_id 列
- points 表扩展: type(earn/spend), balance_after
"""
import sqlite3
import json
import csv
import os
from datetime import datetime

# v1.03.01: 从环境配置获取数据库路径
# 优先使用环境变量 DB_PATH，回退到默认路径
DB_PATH = os.getenv("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

# 新增字段列表 (用于自动迁移)
NEW_COLUMNS = [
    ("solve_mode", "TEXT DEFAULT 'simulation'"),
    ("slots_stator", "INTEGER"),
    ("slots_rotor", "INTEGER"),
    ("turns_per_coil", "INTEGER"),
    ("stator_tooth_flux_density", "REAL"),
    ("stator_yoke_flux_density", "REAL"),
    ("rotor_yoke_flux_density", "REAL"),
    ("air_gap_flux_density", "REAL"),
    ("current_density", "REAL"),
    ("power_factor", "REAL"),
    ("copper_loss", "REAL"),
    ("iron_loss", "REAL"),
    ("total_loss", "REAL"),
    ("thermal_factor", "REAL"),
    ("copper_temp", "REAL"),
    ("magnet_temp", "REAL"),
    # v1.02.03 结构参数扩展
    ("stator_id", "REAL"),
    ("rotor_od", "REAL"),
    ("core_length", "REAL"),
    ("air_gap", "REAL"),
    ("tooth_width", "REAL"),
    ("yoke_thickness", "REAL"),
    ("shaft_dia", "REAL"),
]


def init_db():
    """初始化数据库表 + 自动迁移新增字段"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # v1.03.03: 启用 WAL 模式减少并发锁冲突
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS designs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                power_kw REAL,
                voltage_v REAL,
                speed_rpm REAL,
                stator_od INTEGER,
                poles INTEGER,
                efficiency REAL,
                max_temp REAL,
                torque REAL,
                power REAL,
                params TEXT,
                created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # 自动迁移: 检查并添加新字段
        cursor = conn.execute("PRAGMA table_info(designs)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in NEW_COLUMNS:
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE designs ADD COLUMN {col_name} {col_type}")
                    print(f"[db_tools] 新增字段: {col_name}")
                except Exception as e:
                    print(f"[db_tools] 字段 {col_name} 添加失败: {e}")
        conn.commit()

        # v1.03.01: 预建多租户表结构 (users / points), 当前未启用
        # v2.02.01: users 表扩展为完整认证体系
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                role TEXT DEFAULT 'guest',
                created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # v2.02.01: users 表自动迁移 — 添加认证相关字段
        cursor = conn.execute("PRAGMA table_info(users)")
        user_cols = {row[1] for row in cursor.fetchall()}
        user_new_cols = [
            ("email", "TEXT"),
            ("password_hash", "TEXT"),
            ("phone", "TEXT"),
            ("membership_level", "TEXT DEFAULT 'trial'"),
            ("points_balance", "INTEGER DEFAULT 0"),
            ("status", "TEXT DEFAULT 'active'"),
            ("last_login", "TIMESTAMP"),
            ("expires_at", "TIMESTAMP"),
        ]
        for col_name, col_type in user_new_cols:
            if col_name not in user_cols:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                    print(f"[db_tools] users 新增字段: {col_name}")
                except Exception as e:
                    print(f"[db_tools] users 字段 {col_name} 添加失败: {e}")
        conn.commit()

        # points 表: 积分系统
        conn.execute("""
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                points INTEGER DEFAULT 0,
                reason TEXT,
                created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # v2.02.01: points 表扩展 — 添加类型和余额快照
        cursor = conn.execute("PRAGMA table_info(points)")
        point_cols = {row[1] for row in cursor.fetchall()}
        point_new_cols = [
            ("type", "TEXT DEFAULT 'earn'"),
            ("balance_after", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in point_new_cols:
            if col_name not in point_cols:
                try:
                    conn.execute(f"ALTER TABLE points ADD COLUMN {col_name} {col_type}")
                    print(f"[db_tools] points 新增字段: {col_name}")
                except Exception:
                    pass
        conn.commit()

        # v2.02.01: designs 表增加 user_id + project_id 列
        cursor = conn.execute("PRAGMA table_info(designs)")
        design_cols = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in [("user_id", "INTEGER"), ("project_id", "TEXT")]:
            if col_name not in design_cols:
                try:
                    conn.execute(f"ALTER TABLE designs ADD COLUMN {col_name} {col_type}")
                    print(f"[db_tools] designs 新增字段: {col_name}")
                except Exception:
                    pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_designs_user ON designs(user_id)")
        conn.commit()

        # v2.02.01: 产品线配置表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                flow_stages TEXT,
                design_template TEXT,
                pricing_model TEXT,
                convergence_intensity TEXT DEFAULT 'standard',
                is_active INTEGER DEFAULT 1,
                created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # v2.02.01: 项目表 — 关联用户与产品线
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_line_id INTEGER,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'active',
                requirements TEXT,
                created_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (product_line_id) REFERENCES product_lines(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)")
        conn.commit()

        # v2.02.01: 插入默认产品线 (如果表为空)
        count = conn.execute("SELECT COUNT(*) FROM product_lines").fetchone()[0]
        if count == 0:
            default_lines = [
                ("A", "技术服务", "按人天计费的技术咨询服务",
                 json.dumps(["requirement", "design", "review", "report"], ensure_ascii=False),
                 "Templates.mot",
                 json.dumps({"type": "daily", "rate": 1000}, ensure_ascii=False),
                 "standard"),
                ("B", "整体解决方案", "从设计到交付的完整方案",
                 json.dumps(["requirement", "design", "review", "cost", "supply", "delivery"], ensure_ascii=False),
                 "160kw_assm_2222.mot",
                 json.dumps({"type": "bom", "margin": 0.3}, ensure_ascii=False),
                 "enhanced"),
                ("C", "快速验证", "快速仿真验证方案可行性",
                 json.dumps(["requirement", "design", "report"], ensure_ascii=False),
                 "Templates.mot",
                 json.dumps({"type": "fixed", "amount": 5000}, ensure_ascii=False),
                 "standard"),
                ("D", "联合开发", "长期联合开发合作",
                 json.dumps(["requirement", "design", "review", "prototype", "test", "delivery"], ensure_ascii=False),
                 "Templates.mot",
                 json.dumps({"type": "milestone", "stages": 4}, ensure_ascii=False),
                 "enhanced"),
            ]
            for line in default_lines:
                conn.execute("""
                    INSERT INTO product_lines (code, name, description, flow_stages, design_template, pricing_model, convergence_intensity)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, line)
            conn.commit()
            print(f"[db_tools] 插入 {len(default_lines)} 条默认产品线")

        # v1.03.03: 全局任务队列表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_queue (
                id TEXT PRIMARY KEY,
                user_id TEXT DEFAULT 'dev',
                source TEXT DEFAULT 'dev',
                type TEXT,
                status TEXT DEFAULT 'queued',
                priority INTEGER DEFAULT 5,
                progress REAL DEFAULT 0.0,
                current_step TEXT,
                log TEXT DEFAULT '[]',
                result_summary TEXT,
                specs TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 索引加速查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_status ON task_queue(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_created ON task_queue(created_at DESC)")
        conn.commit()
        print(f"[db_tools] 数据库就绪: {DB_PATH}")


def save_result(params: dict, result: dict, user_id: int = None, project_id: str = None):
    """保存一组求解结果（包含扩展参数）"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO designs (
                power_kw, voltage_v, speed_rpm, stator_od, poles,
                efficiency, max_temp, torque, power, params,
                solve_mode, slots_stator, slots_rotor, turns_per_coil,
                stator_tooth_flux_density, stator_yoke_flux_density,
                rotor_yoke_flux_density, air_gap_flux_density,
                current_density, power_factor,
                copper_loss, iron_loss, total_loss,
                thermal_factor, copper_temp, magnet_temp,
                stator_id, rotor_od, core_length, air_gap,
                tooth_width, yoke_thickness, shaft_dia,
                user_id, project_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            params.get("power_kw"),
            params.get("voltage"),
            params.get("speed"),
            params.get("stator_od"),
            params.get("poles"),
            result.get("efficiency"),
            result.get("max_temp"),
            result.get("torque"),
            result.get("power"),
            json.dumps(params, ensure_ascii=False),
            result.get("solve_mode", "simulation"),
            result.get("slots_stator") or params.get("slots_stator"),
            result.get("slots_rotor") or params.get("slots_rotor"),
            result.get("turns_per_coil") or params.get("turns_per_coil"),
            result.get("stator_tooth_flux_density"),
            result.get("stator_yoke_flux_density"),
            result.get("rotor_yoke_flux_density"),
            result.get("air_gap_flux_density"),
            result.get("current_density"),
            result.get("power_factor"),
            result.get("copper_loss"),
            result.get("iron_loss"),
            result.get("total_loss"),
            result.get("thermal_factor"),
            result.get("copper_temp"),
            result.get("magnet_temp"),
            result.get("stator_id") or params.get("stator_id"),
            result.get("rotor_od") or params.get("rotor_od"),
            result.get("core_length") or params.get("core_length"),
            result.get("air_gap") or params.get("air_gap"),
            result.get("tooth_width") or params.get("tooth_width"),
            result.get("yoke_thickness") or params.get("yoke_thickness"),
            result.get("shaft_dia") or params.get("shaft_dia"),
            user_id,
            project_id,
        ))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_all_results(user_id: int = None) -> list:
    """获取所有结果 (v2.02.01: 支持按 user_id 过滤)"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM designs WHERE user_id = ? OR user_id IS NULL ORDER BY efficiency DESC",
                (user_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM designs ORDER BY efficiency DESC").fetchall()
        return [dict(r) for r in rows]


def get_top_solutions(n: int = 10, min_efficiency: float = 0, user_id: int = None) -> list:
    """获取效率最高的 N 个方案 (v2.02.01: 支持按 user_id 过滤)"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM designs WHERE efficiency >= ? AND (user_id = ? OR user_id IS NULL) ORDER BY efficiency DESC LIMIT ?",
                (min_efficiency, user_id, n)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM designs WHERE efficiency >= ? ORDER BY efficiency DESC LIMIT ?",
                (min_efficiency, n)
            ).fetchall()
        return [dict(r) for r in rows]


def export_csv(filepath: str = None, user_id: int = None) -> str:
    """导出全部结果到 CSV（包含扩展字段, v2.02.01: 支持 user_id 过滤）"""
    if filepath is None:
        filepath = os.path.join(
            r"D:\MotorDesign\Reports",
            f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )

    results = get_all_results(user_id=user_id)
    if not results:
        return ""

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    fieldnames = [
        "id", "stator_od", "stator_id", "rotor_od", "core_length", "air_gap",
        "poles", "slots_stator", "slots_rotor", "turns_per_coil",
        "voltage_v", "speed_rpm",
        "efficiency", "max_temp", "torque", "power", "power_factor",
        "stator_tooth_flux_density", "stator_yoke_flux_density",
        "rotor_yoke_flux_density", "air_gap_flux_density",
        "current_density", "thermal_factor",
        "copper_loss", "iron_loss", "total_loss",
        "copper_temp", "magnet_temp",
        "solve_mode", "created_time"
    ]
    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    return filepath


def get_stats(user_id: int = None) -> dict:
    """获取统计信息 (v2.02.01: 支持按 user_id 过滤)"""
    with sqlite3.connect(DB_PATH) as conn:
        if user_id is not None:
            total = conn.execute(
                "SELECT COUNT(*) FROM designs WHERE user_id = ? OR user_id IS NULL", (user_id,)
            ).fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        if total == 0:
            return {"total": 0}
        if user_id is not None:
            avg_eff = conn.execute(
                "SELECT AVG(efficiency) FROM designs WHERE user_id = ? OR user_id IS NULL", (user_id,)
            ).fetchone()[0]
            max_eff = conn.execute(
                "SELECT MAX(efficiency) FROM designs WHERE user_id = ? OR user_id IS NULL", (user_id,)
            ).fetchone()[0]
            min_temp = conn.execute(
                "SELECT MIN(max_temp) FROM designs WHERE user_id = ? OR user_id IS NULL", (user_id,)
            ).fetchone()[0]
            mc_count = conn.execute(
                "SELECT COUNT(*) FROM designs WHERE solve_mode = 'motorcad' AND (user_id = ? OR user_id IS NULL)", (user_id,)
            ).fetchone()[0]
        else:
            avg_eff = conn.execute("SELECT AVG(efficiency) FROM designs").fetchone()[0]
            max_eff = conn.execute("SELECT MAX(efficiency) FROM designs").fetchone()[0]
            min_temp = conn.execute("SELECT MIN(max_temp) FROM designs").fetchone()[0]
            mc_count = conn.execute(
                "SELECT COUNT(*) FROM designs WHERE solve_mode = 'motorcad'"
            ).fetchone()[0]
        sim_count = total - mc_count

        return {
            "total": total,
            "avg_efficiency": round(avg_eff, 2),
            "max_efficiency": round(max_eff, 2),
            "min_temp_rise": round(min_temp, 1),
            "motorcad_count": mc_count,
            "simulation_count": sim_count,
        }


# ============================================================
# v2.02.01: 多租户数据库工具函数
# ============================================================

def get_user_by_username(username: str) -> dict:
    """按用户名查询用户"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> dict:
    """按邮箱查询用户"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict:
    """按 ID 查询用户"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user(username: str, email: str, password_hash: str, display_name: str = None,
                role: str = "user", membership_level: str = "trial") -> int:
    """创建用户，返回 user_id"""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO users (username, email, password_hash, display_name, role, membership_level, points_balance)
               VALUES (?, ?, ?, ?, ?, ?, 100)""",
            (username, email, password_hash, display_name or username, role, membership_level)
        )
        conn.commit()
        return cur.lastrowid


def get_all_users() -> list:
    """获取所有用户列表 (管理员用)"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, username, email, display_name, role, membership_level, points_balance, status, last_login, created_time FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: int, **fields) -> bool:
    """更新用户字段"""
    if not fields:
        return False
    set_clauses = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {set_clauses} WHERE id = ?", values)
        conn.commit()
        return True


def get_product_lines() -> list:
    """获取所有产品线"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM product_lines WHERE is_active = 1 ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_product_line_by_id(pl_id: int) -> dict:
    """按 ID 获取产品线"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM product_lines WHERE id = ?", (pl_id,)).fetchone()
        return dict(row) if row else None


def create_project(user_id: int, name: str, product_line_id: int = None,
                   description: str = None, requirements: str = None) -> str:
    """创建项目，返回 project_id (UUID)"""
    import uuid
    project_id = str(uuid.uuid4())[:8]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO projects (id, user_id, product_line_id, name, description, requirements)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, user_id, product_line_id, name, description, requirements)
        )
        conn.commit()
    return project_id


def get_projects_by_user(user_id: int) -> list:
    """获取用户的项目列表"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT p.*, pl.name as product_line_name, pl.code as product_line_code
               FROM projects p
               LEFT JOIN product_lines pl ON p.product_line_id = pl.id
               WHERE p.user_id = ? ORDER BY p.created_time DESC""",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_project_by_id(project_id: str) -> dict:
    """获取项目详情"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None
