"""
db_tools.py - 本地结果存储（SQLite）
所有求解结果存本地，支持查询、排序、导出 CSV

v1.01 更新:
- 新增字段: 槽配合、线圈参数、磁密、电密、发热因数、损耗等
- 新增 solve_mode 字段标记 Motor-CAD / 模拟模式
- 自动迁移旧数据库 (ALTER TABLE)
"""
import sqlite3
import json
import csv
import os
from datetime import datetime

DB_PATH = r"D:\MotorDesign\KnowledgeBase\designs.db"

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


def save_result(params: dict, result: dict):
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
                tooth_width, yoke_thickness, shaft_dia
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        ))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_all_results() -> list:
    """获取所有结果"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM designs ORDER BY efficiency DESC").fetchall()
        return [dict(r) for r in rows]


def get_top_solutions(n: int = 10, min_efficiency: float = 0) -> list:
    """获取效率最高的 N 个方案"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM designs WHERE efficiency >= ? ORDER BY efficiency DESC LIMIT ?",
            (min_efficiency, n)
        ).fetchall()
        return [dict(r) for r in rows]


def export_csv(filepath: str = None) -> str:
    """导出全部结果到 CSV（包含扩展字段）"""
    if filepath is None:
        filepath = os.path.join(
            r"D:\MotorDesign\Reports",
            f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )

    results = get_all_results()
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


def get_stats() -> dict:
    """获取统计信息"""
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        if total == 0:
            return {"total": 0}
        avg_eff = conn.execute("SELECT AVG(efficiency) FROM designs").fetchone()[0]
        max_eff = conn.execute("SELECT MAX(efficiency) FROM designs").fetchone()[0]
        min_temp = conn.execute("SELECT MIN(max_temp) FROM designs").fetchone()[0]

        # 求解模式统计
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
