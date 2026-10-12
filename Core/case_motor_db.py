"""
case_motor_db.py - 案例电机数据中心数据库模块
管理案例电机参数录入、验算、血缘追踪、草稿等数据

表结构:
  - case_motors: 案例电机主表 (含铭牌/结构/绕组/性能/电磁/损耗参数)
  - case_motor_params: 单个参数记录 (含来源/置信度/状态/修改历史)
  - case_motor_verifications: 验算结果记录
  - case_motor_drafts: 草稿自动保存

v1.0  2026-08-06  初始版本
"""
import sqlite3
import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Any, Optional

DB_PATH = os.getenv("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")


def _local_now() -> str:
    """返回本地当前时间字符串, 用于替代 SQLite CURRENT_TIMESTAMP(UTC)。

    v4.01.045: 修复 created_at/updated_at 与 case_code 时间戳差 8 小时的问题。
    SQLite 的 CURRENT_TIMESTAMP 固定为 UTC, 而 generate_case_code 用 datetime.now()
    取本地时间(GMT+8), 导致同一记录 case_code 时间比 created_at 快 8 小时。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_conn():
    """获取数据库连接 (WAL 模式)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_case_motor_tables():
    """初始化案例电机相关表"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _get_conn() as conn:
        # ---- 案例电机主表 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS case_motors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_code TEXT UNIQUE NOT NULL,
                motor_type TEXT NOT NULL DEFAULT '',
                motor_model TEXT DEFAULT '',
                series_id TEXT DEFAULT '',
                series_name TEXT DEFAULT '',

                -- 铭牌参数
                rated_power REAL,
                rated_voltage REAL,
                rated_current REAL,
                rated_speed REAL,
                rated_freq REAL DEFAULT 50,
                poles INTEGER,
                phase INTEGER DEFAULT 3,
                efficiency REAL,
                power_factor REAL,
                insulation_class TEXT DEFAULT '',
                protection_class TEXT DEFAULT '',
                cooling_method TEXT DEFAULT '',
                -- v4.01.032: 电机总重量 (kg)
                motor_weight REAL,

                -- 结构参数
                stator_od REAL,
                stator_id REAL,
                rotor_od REAL,
                core_length REAL,
                air_gap REAL,
                shaft_dia REAL,
                tooth_width REAL,
                yoke_thickness REAL,

                -- 绕组参数
                slots_stator INTEGER,
                slots_rotor INTEGER,
                turns_per_coil INTEGER,
                wire_dia REAL,
                parallel_branches INTEGER,

                -- 性能参数
                starting_torque REAL,
                max_torque REAL,
                starting_current REAL,
                -- v4.01.032: 倍数类 (相对额定值的比值, 无量纲)
                starting_torque_ratio REAL,
                max_torque_ratio REAL,
                starting_current_ratio REAL,
                noise_level REAL,
                vibration_level TEXT DEFAULT '',
                temperature_rise REAL,

                -- 电磁参数
                stator_tooth_flux REAL,
                stator_yoke_flux REAL,
                rotor_yoke_flux REAL,
                air_gap_flux REAL,
                current_density REAL,

                -- 损耗参数
                copper_loss REAL,
                iron_loss REAL,
                total_loss REAL,
                friction_loss REAL,
                stray_loss REAL,
                wind_loss REAL,

                -- v4.00.009: 定子槽形尺寸
                stator_slot_type TEXT DEFAULT '',
                stator_hs0 REAL,
                stator_hs1 REAL,
                stator_hs2 REAL,
                stator_bs0 REAL,
                stator_bs1 REAL,
                stator_bs2 REAL,
                stator_rs REAL,
                stator_skew REAL,
                stator_slot_ins REAL,

                -- v4.00.009: 转子槽形尺寸
                rotor_id REAL,
                rotor_slot_type TEXT DEFAULT '',
                rotor_hs0 REAL,
                rotor_hs1 REAL,
                rotor_hs2 REAL,
                rotor_bs0 REAL,
                rotor_bs1 REAL,
                rotor_bs2 REAL,
                rotor_cast TEXT DEFAULT '',
                rotor_double_cage TEXT DEFAULT '',
                rotor_deep_bar TEXT DEFAULT '',
                rotor_wound TEXT DEFAULT '',

                -- v4.00.009: 绕组扩展参数
                winding_type TEXT DEFAULT '',
                winding_layer INTEGER,
                conductors_per_slot INTEGER,
                strands INTEGER,
                wire_wrap REAL,
                winding_pitch INTEGER,
                coil_pitch INTEGER,
                connection_type TEXT DEFAULT '',
                rotor_bar_material TEXT DEFAULT '',
                end_ring_material TEXT DEFAULT '',
                rotor_bar_width REAL,
                rotor_bar_height REAL,
                end_ring_width REAL,
                end_ring_height REAL,

                -- v4.00.009: 材料参数
                stator_material TEXT DEFAULT '',
                rotor_material TEXT DEFAULT '',
                conductor_material TEXT DEFAULT '',
                shaft_material TEXT DEFAULT '',

                -- v4.00.009: 其他
                shaft_length REAL,
                reference_speed REAL,

                -- 状态与质量
                status TEXT DEFAULT 'draft',
                completeness_score REAL DEFAULT 0,
                confidence_score REAL DEFAULT 0,
                -- v4.04: 审核信息
                review_comment TEXT DEFAULT '',
                reviewed_by TEXT DEFAULT '',
                reviewed_at TIMESTAMP,

                -- 元数据
                user_id INTEGER,
                source_type TEXT DEFAULT 'manual',
                source_detail TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_status ON case_motors(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_series ON case_motors(series_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_type ON case_motors(motor_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_created ON case_motors(created_at DESC)")

        # ---- 单个参数记录 (血缘追踪) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS case_motor_params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                param_name TEXT NOT NULL,
                param_value TEXT,
                param_unit TEXT DEFAULT '',
                source_type TEXT DEFAULT 'user_input',
                source_detail TEXT DEFAULT '',
                confidence REAL DEFAULT 100,
                status TEXT DEFAULT 'confirmed',
                modified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                modified_reason TEXT DEFAULT '',
                modified_note TEXT DEFAULT '',
                FOREIGN KEY (case_id) REFERENCES case_motors(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cmp_case ON case_motor_params(case_id)")

        # ---- 验算结果 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS case_motor_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                verify_type TEXT DEFAULT 'forward',
                result TEXT DEFAULT 'pending',
                details_json TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                warning_items TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (case_id) REFERENCES case_motors(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cmv_case ON case_motor_verifications(case_id)")

        # ---- 草稿 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS case_motor_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER,
                user_id INTEGER,
                draft_data TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cmd_user ON case_motor_drafts(user_id)")

        # ---- 用户模版 (系列 × 电机类型 二维维度, v4.02: 数据录入改进) ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS series_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                series TEXT NOT NULL DEFAULT '',
                motor_type TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                default_params TEXT DEFAULT '{}',
                typical_efficiency_range TEXT DEFAULT '{}',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_series ON series_templates(series)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_st_type ON series_templates(motor_type)")

        # v4.04: 自动迁移 — 为已有数据库添加审核字段
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            for col in ["review_comment", "reviewed_by", "reviewed_at"]:
                if col not in cols:
                    col_type = "TIMESTAMP" if col == "reviewed_at" else "TEXT"
                    default = "DEFAULT ''" if col != "reviewed_at" else ""
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type} {default}")
                    print(f"[case_motor_db] 迁移: 添加字段 {col}")
        except Exception as e:
            print(f"[case_motor_db] 迁移跳过: {e}")

        # v4.00.009: 自动迁移 — 为已有数据库添加 RMxprt 扩展字段
        _v009_new_cols = {
            "wind_loss": "REAL",
            "stator_slot_type": "TEXT DEFAULT ''",
            "stator_hs0": "REAL", "stator_hs1": "REAL", "stator_hs2": "REAL",
            "stator_bs0": "REAL", "stator_bs1": "REAL", "stator_bs2": "REAL",
            "stator_rs": "REAL", "stator_skew": "REAL", "stator_slot_ins": "REAL",
            "rotor_id": "REAL", "rotor_slot_type": "TEXT DEFAULT ''",
            "rotor_hs0": "REAL", "rotor_hs1": "REAL", "rotor_hs2": "REAL",
            "rotor_bs0": "REAL", "rotor_bs1": "REAL", "rotor_bs2": "REAL",
            "rotor_cast": "TEXT DEFAULT ''", "rotor_double_cage": "TEXT DEFAULT ''",
            "rotor_deep_bar": "TEXT DEFAULT ''", "rotor_wound": "TEXT DEFAULT ''",
            "winding_type": "TEXT DEFAULT ''", "winding_layer": "INTEGER",
            "conductors_per_slot": "INTEGER", "strands": "INTEGER",
            "wire_wrap": "REAL", "winding_pitch": "INTEGER", "coil_pitch": "INTEGER",
            "connection_type": "TEXT DEFAULT ''",
            "rotor_bar_material": "TEXT DEFAULT ''", "end_ring_material": "TEXT DEFAULT ''",
            "rotor_bar_width": "REAL", "rotor_bar_height": "REAL",
            "end_ring_width": "REAL", "end_ring_height": "REAL",
            "stator_material": "TEXT DEFAULT ''", "rotor_material": "TEXT DEFAULT ''",
            "conductor_material": "TEXT DEFAULT ''", "shaft_material": "TEXT DEFAULT ''",
            "shaft_length": "REAL", "reference_speed": "REAL",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v009_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.00.009 迁移: 添加 {added} 个新字段")
        except Exception as e:
            print(f"[case_motor_db] v4.00.009 迁移跳过: {e}")

        # v4.01.009: 自动迁移 — 11大类新字段
        _v010_new_cols = {
            "operating_temp": "REAL",
            "load_type": "TEXT DEFAULT ''",
            "lamination_sectors": "INTEGER",
            "rotor_core_length": "REAL",
            "rotor_winding_type": "TEXT DEFAULT ''",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v010_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.01.009 迁移: 添加 {added} 个新字段 (11大类)")
        except Exception as e:
            print(f"[case_motor_db] v4.01.009 迁移跳过: {e}")

        # v4.01.026: 自动迁移 — 增加电机总重量字段
        _v0126_new_cols = {
            "motor_weight": "REAL",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v0126_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.01.026 迁移: 添加 {added} 个新字段 (重量)")
        except Exception as e:
            print(f"[case_motor_db] v4.01.026 迁移跳过: {e}")

        # v4.01.032: 自动迁移 — 增加倍数类字段 (堵转转矩/最大转矩/堵转电流 相对额定值的比值)
        _v0132_new_cols = {
            "starting_torque_ratio": "REAL",
            "max_torque_ratio": "REAL",
            "starting_current_ratio": "REAL",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v0132_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.01.032 迁移: 添加 {added} 个倍数类新字段")
        except Exception as e:
            print(f"[case_motor_db] v4.01.032 迁移跳过: {e}")

        # v4.01.033: 自动迁移 — 增加 motor_model(具体型号) 字段, 分离「型号」与「类型」
        _v0133_new_cols = {
            "motor_model": "TEXT DEFAULT ''",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v0133_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.01.033 迁移: 添加 motor_model(型号) 字段")
        except Exception as e:
            print(f"[case_motor_db] v4.01.033 迁移跳过: {e}")

        # v4.01.042 (#524): 增加 motor_name(电机名称) 字段
        # 三层语义彻底分离: motor_type=类型类别 / motor_model=具体型号 / motor_name=完整名称
        _v0142_new_cols = {
            "motor_name": "TEXT DEFAULT ''",
        }
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(case_motors)").fetchall()]
            added = 0
            for col, col_type in _v0142_new_cols.items():
                if col not in cols:
                    conn.execute(f"ALTER TABLE case_motors ADD COLUMN {col} {col_type}")
                    added += 1
            if added:
                print(f"[case_motor_db] v4.01.042 迁移: 添加 motor_name(电机名称) 字段")
        except Exception as e:
            print(f"[case_motor_db] v4.01.042 迁移跳过: {e}")

        # #525: 字段映射数据飞轮建表 (延迟导入规避循环依赖, 复用同一 conn)
        try:
            try:
                from Core.field_mapping_flywheel import init_field_mapping_tables
            except Exception:
                from field_mapping_flywheel import init_field_mapping_tables
            init_field_mapping_tables(conn)
            print("[case_motor_db] 字段映射飞轮表初始化完成")
        except Exception as e:
            print(f"[case_motor_db] 字段映射飞轮建表跳过: {e}")

        conn.commit()
        print("[case_motor_db] 案例电机表初始化完成")


# ============================================================
# 案例电机 CRUD
# ============================================================

# 电机参数字段定义 (字段名, 中文名, 单位, 分组)
MOTOR_PARAM_FIELDS = [
    # 1. 通用参数 (General)
    ("rated_power",      "额定功率",   "kW",  "general"),
    ("rated_voltage",    "额定电压",   "V",   "general"),
    ("rated_current",    "额定电流",   "A",   "general"),
    ("rated_speed",      "额定转速",   "r/min", "general"),
    ("rated_freq",       "额定频率",   "Hz",  "general"),
    ("poles",            "极数",       "",    "general"),
    ("phase",            "相数",       "",    "general"),
    ("operating_temp",   "工作温度",   "℃",   "general"),
    ("load_type",        "负载类型",   "",    "general"),
    ("insulation_class", "绝缘等级",   "",    "general"),
    ("protection_class", "防护等级",   "",    "general"),
    ("cooling_method",   "冷却方式",   "",    "general"),
    ("motor_weight",     "电机总重量", "kg",  "general"),
    # 2. 定子参数 (Stator)
    ("stator_od",        "定子外径",   "mm",  "stator"),
    ("stator_id",        "定子内径",   "mm",  "stator"),
    ("core_length",      "铁心长度",   "mm",  "stator"),
    ("air_gap",          "气隙长度",   "mm",  "stator"),
    ("slots_stator",     "定子槽数",   "",    "stator"),
    ("lamination_sectors","叠片扇形数","",    "stator"),
    ("stator_skew",      "定子斜槽",   "slot","stator"),
    ("tooth_width",      "齿宽",       "mm",  "stator"),
    ("yoke_thickness",   "轭厚",       "mm",  "stator"),
    # 3. 定子槽形参数 (Stator Slot)
    ("stator_slot_type", "定子槽形类型", "",  "stator_slot"),
    ("stator_hs0",       "定子槽口高Hs0", "mm","stator_slot"),
    ("stator_hs1",       "定子槽肩高Hs1", "mm","stator_slot"),
    ("stator_hs2",       "定子槽身高Hs2", "mm","stator_slot"),
    ("stator_bs0",       "定子槽口宽Bs0", "mm","stator_slot"),
    ("stator_bs1",       "定子槽肩宽Bs1", "mm","stator_slot"),
    ("stator_bs2",       "定子槽身宽Bs2", "mm","stator_slot"),
    ("stator_rs",        "定子槽底半径Rs","mm","stator_slot"),
    ("stator_slot_ins",  "定子槽绝缘", "mm", "stator_slot"),
    # 4. 转子参数 (Rotor)
    ("rotor_od",         "转子外径",   "mm",  "rotor"),
    ("rotor_id",         "转子内径",   "mm",  "rotor"),
    ("rotor_core_length","转子铁心长度","mm", "rotor"),
    ("slots_rotor",      "转子槽数",   "",    "rotor"),
    ("rotor_cast",       "铸铝转子",   "",    "rotor"),
    ("rotor_double_cage","双笼转子",   "",    "rotor"),
    ("rotor_deep_bar",   "深槽转子",   "",    "rotor"),
    ("rotor_wound",      "绕线转子",   "",    "rotor"),
    # 5. 转子槽形参数 (Rotor Slot)
    ("rotor_slot_type",  "转子槽形类型","",   "rotor_slot"),
    ("rotor_hs0",        "转子槽口高Hs0","mm","rotor_slot"),
    ("rotor_hs1",        "转子槽肩高Hs1","mm","rotor_slot"),
    ("rotor_hs2",        "转子槽身高Hs2","mm","rotor_slot"),
    ("rotor_bs0",        "转子槽口宽Bs0","mm","rotor_slot"),
    ("rotor_bs1",        "转子槽肩宽Bs1","mm","rotor_slot"),
    ("rotor_bs2",        "转子槽身宽Bs2","mm","rotor_slot"),
    # 6. 绕组参数 (Winding)
    ("winding_type",     "绕组类型",   "",    "winding"),
    ("winding_layer",    "绕组层数",   "",    "winding"),
    ("parallel_branches","并联支路数", "",    "winding"),
    ("conductors_per_slot","每槽导体数","",    "winding"),
    ("turns_per_coil",   "每圈匝数",   "",    "winding"),
    ("strands",          "并绕根数",   "",    "winding"),
    ("wire_dia",         "线径",       "mm",  "winding"),
    ("wire_wrap",        "线径包带",   "mm",  "winding"),
    ("winding_pitch",    "绕组节距",   "",    "winding"),
    ("coil_pitch",       "线圈节距",   "",    "winding"),
    ("connection_type",  "接法",       "",    "winding"),
    ("rotor_winding_type","转子绕组类型","",  "winding"),
    ("rotor_bar_material","转子条材料", "",   "winding"),
    ("end_ring_material","端环材料",   "",    "winding"),
    ("rotor_bar_width",  "转子条宽",   "mm",  "winding"),
    ("rotor_bar_height", "转子条高",   "mm",  "winding"),
    ("end_ring_width",   "端环宽",     "mm",  "winding"),
    ("end_ring_height",  "端环高",     "mm",  "winding"),
    # 7. 转轴参数 (Shaft)
    ("shaft_dia",        "轴径",       "mm",  "shaft"),
    ("shaft_length",     "轴长度",     "mm",  "shaft"),
    ("friction_loss",    "摩擦损耗",   "W",   "shaft"),
    ("wind_loss",        "风磨损耗",   "W",   "shaft"),
    ("reference_speed",  "参考转速",   "r/min","shaft"),
    # 8. 材料参数 (Material)
    ("stator_material",  "定子铁芯材料","",    "material"),
    ("rotor_material",   "转子铁芯材料","",    "material"),
    ("conductor_material","导体材料",   "",    "material"),
    ("shaft_material",   "轴材料",     "",    "material"),
    # 9. 性能参数 (Performance)
    ("efficiency",       "效率",       "%",   "performance"),
    ("power_factor",     "功率因数",   "",    "performance"),
    ("starting_torque",  "起动转矩",   "N.m", "performance"),
    ("max_torque",       "最大转矩",   "N.m", "performance"),
    ("starting_current", "起动电流",   "A",   "performance"),
    ("starting_torque_ratio", "堵转转矩倍数", "", "performance"),
    ("max_torque_ratio", "最大转矩倍数", "", "performance"),
    ("starting_current_ratio", "堵转电流倍数", "", "performance"),
    ("temperature_rise", "温升",       "K",   "performance"),
    ("noise_level",      "噪声水平",   "dB",  "performance"),
    ("vibration_level",  "振动等级",   "",    "performance"),
    # 10. 电磁参数 (Electromagnetic)
    ("stator_tooth_flux","定子齿磁密", "T",   "electromagnetic"),
    ("stator_yoke_flux", "定子轭磁密", "T",   "electromagnetic"),
    ("rotor_yoke_flux",  "转子轭磁密", "T",   "electromagnetic"),
    ("air_gap_flux",     "气隙磁密",   "T",   "electromagnetic"),
    ("current_density",  "电流密度",   "A/mm2","electromagnetic"),
    # 11. 损耗参数 (Losses)
    ("copper_loss",      "铜耗",       "W",   "loss"),
    ("iron_loss",        "铁耗",       "W",   "loss"),
    ("total_loss",       "总损耗",     "W",   "loss"),
    ("stray_loss",       "杂散损耗",   "W",   "loss"),
]

# 电机系列模板 (简化版, 实际可从数据库或配置文件加载)
SERIES_TEMPLATES = {
    "Y": {
        "name": "Y系列三相异步电动机",
        "default_params": {
            "rated_freq": 50, "phase": 3, "insulation_class": "F",
            "protection_class": "IP44", "cooling_method": "IC411",
        },
        "typical_efficiency_range": {"2": (92, 96), "4": (89, 95), "6": (86, 93), "8": (83, 91)},
    },
    "Y2": {
        "name": "Y2系列三相异步电动机",
        "default_params": {
            "rated_freq": 50, "phase": 3, "insulation_class": "F",
            "protection_class": "IP54", "cooling_method": "IC411",
        },
        "typical_efficiency_range": {"2": (93, 96.5), "4": (90, 95.5), "6": (87, 94), "8": (84, 92)},
    },
    "YE3": {
        "name": "YE3超超高效三相异步电动机",
        "default_params": {
            "rated_freq": 50, "phase": 3, "insulation_class": "F",
            "protection_class": "IP55", "cooling_method": "IC411",
        },
        "typical_efficiency_range": {"2": (94, 97), "4": (91, 96), "6": (89, 95), "8": (86, 93)},
    },
    "PMSM": {
        "name": "永磁同步电动机",
        "default_params": {
            "rated_freq": 50, "phase": 3, "insulation_class": "F",
            "protection_class": "IP55", "cooling_method": "IC411",
        },
        "typical_efficiency_range": {"2": (94, 98), "4": (93, 97), "6": (92, 96), "8": (90, 95)},
    },
    "BLDC": {
        "name": "无刷直流电动机",
        "default_params": {
            "rated_freq": 0, "phase": 3, "insulation_class": "F",
            "protection_class": "IP54", "cooling_method": "IC411",
        },
        "typical_efficiency_range": {"2": (90, 96), "4": (88, 94), "6": (85, 92), "8": (82, 90)},
    },
}


def generate_case_code(motor_type: str = "") -> str:
    """生成案例编号: CM_{type}_{YYYYMMDDHHmmssffffff}_{rand}

    v4.01.033: 后缀加入微秒 + 随机串, 避免「型号→类型类别」推导后同前缀同秒并发插入
    导致的 case_code UNIQUE 冲突 (如同一系列两行均推导为 异步电动机)。
    """
    now = datetime.now().strftime("%Y%m%d%H%M%S%f")
    prefix = motor_type.upper().replace(" ", "")[:8] if motor_type else "GEN"
    rnd = uuid.uuid4().hex[:6]
    return f"CM_{prefix}_{now}_{rnd}"


def derive_motor_type(model: str) -> str:
    """v4.01.033: 从具体型号(如 Y132s-4)推导电机类型类别(异步电动机/永磁同步电动机/...)。

    系列导入时把「型号」自动归类为系统预设的 motor_type 类别, 解决「型号 vs 类型」语义冲突。
    规则与 SERIES_LIST / MOTOR_TYPES 保持一致: Y*异步, YS/YC单相异步, PMSM永磁同步,
    BLDC直流, SR开关磁阻, SY同步; 其余归「其他」。
    """
    if not model:
        return ""
    s = model.strip().upper()
    if s.startswith("PMSM"):
        return "永磁同步电动机"
    if s.startswith("BLDC") or s.startswith("BLDCM"):
        return "直流电动机"
    if s.startswith("SR"):
        return "开关磁阻电动机"
    if s.startswith("SY"):
        return "同步电动机"
    if s.startswith("YS") or s.startswith("YC"):
        return "单相异步电动机"
    if s.startswith("Y"):
        return "异步电动机"
    return "其他"


def create_case_motor(data: Dict[str, Any]) -> Dict[str, Any]:
    """创建案例电机记录 (v4.00.009: 动态INSERT, 自动支持 MOTOR_PARAM_FIELDS 新增字段)"""
    case_code = data.get("case_code") or generate_case_code(data.get("motor_type", ""))

    # 元数据字段 (不在 MOTOR_PARAM_FIELDS 中)
    # v4.01.033: motor_type 缺省时从 motor_model(具体型号) 自动推导类型类别, 作为单一真相源
    _motor_type = data.get("motor_type", "") or derive_motor_type(data.get("motor_model", ""))
    # v4.01.042 (#524): motor_name(完整名称) 缺省时用「型号+类型」自动拼装, 保证三层语义齐备
    _motor_model = data.get("motor_model", "") or ""
    _motor_name = (data.get("motor_name") or "").strip()
    if not _motor_name:
        _motor_name = (f"{_motor_model}{_motor_type}".strip()
                       if (_motor_model or _motor_type) else "")
    meta = {
        "case_code": case_code,
        "motor_type": _motor_type,
        "motor_model": _motor_model,
        "motor_name": _motor_name,
        "series_id": data.get("series_id", ""),
        "series_name": data.get("series_name", ""),
        "status": data.get("status", "draft"),
        "completeness_score": data.get("completeness_score", 0),
        "confidence_score": data.get("confidence_score", 0),
        "user_id": data.get("user_id"),
        "source_type": data.get("source_type", "manual"),
        "source_detail": data.get("source_detail", ""),
        "notes": data.get("notes", ""),
        # v4.01.045: 显式写入本地时间, 避免 SQLite CURRENT_TIMESTAMP 用 UTC 造成 8h 偏差
        "created_at": data.get("created_at") or _local_now(),
        "updated_at": data.get("updated_at") or _local_now(),
    }

    # 特殊默认值 (部分字段有非 None 默认)
    field_defaults = {
        "rated_freq": 50, "phase": 3,
        "insulation_class": "", "protection_class": "",
        "cooling_method": "", "vibration_level": "",
    }

    # 从 MOTOR_PARAM_FIELDS 动态构建参数字段
    for f in MOTOR_PARAM_FIELDS:
        fname = f[0]
        if fname in data:
            meta[fname] = data[fname]
        elif fname in field_defaults:
            meta[fname] = field_defaults[fname]
        else:
            meta[fname] = None

    columns = list(meta.keys())
    values = list(meta.values())
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)

    with _get_conn() as conn:
        cursor = conn.execute(
            f"INSERT INTO case_motors ({col_str}) VALUES ({placeholders})",
            values
        )
        case_id = cursor.lastrowid
        conn.commit()
    return {"id": case_id, "case_code": case_code, "status": "draft"}


# v4.00.010-dev (#542): 空值防御 —— 允许被显式清空的元数据字段白名单
#   参数字段(MOTOR_PARAM_FIELDS)与语义字段一律不接受空值写入:
#   客户端导入冲突合并时, CSV 未提供的列会带 None/"" 下来, 若直接 UPDATE
#   会把系统已有的额定电流/转速等值清空(数据异常根因 BUG B 的最后一环)。
_UPDATE_CLEARABLE_FIELDS = {"notes", "source_detail", "verified_at", "status"}


def _is_blank_value(val: Any) -> bool:
    """空值判定: None / 空串 / 纯空白 / 常见占位符 视为「未提供」。
    与客户端 data_center_panel._is_empty 语义保持一致。"""
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() in {
            "", "-", "--", "---", "—", "/", "n/a", "na", "null", "none",
            "nan", "nil", "undefined", "无", "未知", "未填", "未提供", "待定", "空",
        }
    return False


def update_case_motor(case_id: int, data: Dict[str, Any]) -> bool:
    """更新案例电机记录 (仅更新提供的字段)

    v4.00.010-dev (#542) 空值防御: 参数/语义字段的空值(None、""、占位符)会被跳过,
    不再生成 SET 子句, 从而杜绝「导入未提供的列把库里已有值清空」。
    需要显式清空时, 仅 _UPDATE_CLEARABLE_FIELDS 内的元数据字段允许写空。
    """
    # 构建动态 SET 子句
    allowed_fields = {f[0] for f in MOTOR_PARAM_FIELDS} | {
        "motor_type", "motor_model", "motor_name",  # v4.01.042 (#524): 三层语义字段
        "series_id", "series_name", "status",
        "completeness_score", "confidence_score", "source_type",
        "source_detail", "notes", "verified_at"
    }
    set_clauses = []
    values = []
    blocked = []
    for key, val in data.items():
        if key not in allowed_fields:
            continue
        if _is_blank_value(val) and key not in _UPDATE_CLEARABLE_FIELDS:
            blocked.append(key)          # ★ 空值拦截: 保留库中原值
            continue
        set_clauses.append(f"{key} = ?")
        values.append(val)
    if blocked:
        try:
            print(f"[case_motor_db] update_case_motor(id={case_id}) 拦截 {len(blocked)} 个空值字段, "
                  f"保留原值: {', '.join(blocked[:12])}{'...' if len(blocked) > 12 else ''}")
        except Exception:
            pass
    if not set_clauses:
        return False
    # v4.01.045: 使用本地时间, 与 create_case_motor 保持一致, 避免 UTC 8h 偏差
    set_clauses.append("updated_at = ?")
    values.append(_local_now())
    values.append(case_id)

    with _get_conn() as conn:
        conn.execute(f"UPDATE case_motors SET {', '.join(set_clauses)} WHERE id = ?", values)
        conn.commit()
    return True


def get_case_motor(case_id: int) -> Optional[Dict[str, Any]]:
    """获取单个案例电机详情 (含参数血缘)"""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM case_motors WHERE id = ?", (case_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        # 获取参数血缘记录
        params = conn.execute(
            "SELECT * FROM case_motor_params WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        result["param_bloodline"] = [dict(p) for p in params]
        # 获取最近验算结果
        verify = conn.execute(
            "SELECT * FROM case_motor_verifications WHERE case_id = ? ORDER BY created_at DESC LIMIT 1",
            (case_id,)
        ).fetchone()
        result["latest_verification"] = dict(verify) if verify else None
        return result


def list_case_motors(
    status: str = None,
    series_id: str = None,
    motor_type: str = None,
    motor_model: str = None,
    keyword: str = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """查询案例电机列表"""
    sql = "SELECT * FROM case_motors WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if series_id:
        sql += " AND series_id = ?"
        params.append(series_id)
    if motor_type:
        sql += " AND motor_type LIKE ?"
        params.append(f"%{motor_type}%")
    if motor_model:
        sql += " AND motor_model LIKE ?"
        params.append(f"%{motor_model}%")
    if keyword:
        sql += " AND (case_code LIKE ? OR motor_type LIKE ? OR motor_model LIKE ? OR notes LIKE ?)"
        params.extend([f"%{keyword}%"] * 4)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def delete_case_motor(case_id: int) -> bool:
    """删除案例电机 (级联删除参数记录和验算记录)"""
    with _get_conn() as conn:
        conn.execute("DELETE FROM case_motor_params WHERE case_id = ?", (case_id,))
        conn.execute("DELETE FROM case_motor_verifications WHERE case_id = ?", (case_id,))
        conn.execute("DELETE FROM case_motor_drafts WHERE case_id = ?", (case_id,))
        cursor = conn.execute("DELETE FROM case_motors WHERE id = ?", (case_id,))
        conn.commit()
        return cursor.rowcount > 0


def find_case_motor_by_fingerprint(
    series_id: str,
    motor_type: str,
    rated_power: Any = None,
    poles: Any = None,
    rated_speed: Any = None,
    motor_model: str = None,
) -> Optional[Dict[str, Any]]:
    """v4.01.026: 导入冲突检测 — 按 (系列号+型号) 查找已录入记录。
    v4.01.033: 新增 motor_model 精确匹配, 因 motor_type 现归类为类别, 需型号参与指纹区分。

    返回首条匹配记录 (dict) 或 None。调用方再比较功率/极数/转速判定
    '完全相同(跳过)' 还是 '参数不同(冲突)'。
    """
    if not series_id and not motor_type and not motor_model:
        return None
    sql = "SELECT * FROM case_motors WHERE 1=1"
    params = []
    if series_id:
        sql += " AND series_id = ?"
        params.append(series_id)
    if motor_type:
        sql += " AND motor_type = ?"
        params.append(motor_type)
    if motor_model:
        sql += " AND motor_model = ?"
        params.append(motor_model)
    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _fp_equal(a: Any, b: Any) -> bool:
    """数值/字符串指纹相等判定 (容差)"""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


# ============================================================
# v4.01.042 (#523): 相似/相同电机检测 —— 审核合并决策支撑
# 场景一.2: 系列电机批量录入打包审核 (批次内横向对比相同/不同项)
# 场景一.3: 审核单条时检测库内相近/相同电机, 列出相同项与差异项, 供「合并」或「新建条目」
# ============================================================

# 身份权重: 决定「是不是同一台电机」的关键字段
_SIM_WEIGHTS_IDENTITY = {
    "motor_model": 10.0, "series_id": 6.0, "motor_type": 4.0,
    "rated_power": 8.0, "poles": 8.0, "rated_voltage": 5.0, "rated_speed": 5.0,
}
# 结构权重: 决定「电磁方案是否雷同」
_SIM_WEIGHTS_STRUCT = {
    "stator_od": 4.0, "stator_id": 4.0, "core_length": 4.0, "air_gap": 3.0,
    "slots_stator": 3.0, "slots_rotor": 2.0, "rotor_od": 2.0, "shaft_dia": 1.5,
    "tooth_width": 1.5, "yoke_thickness": 1.5, "conductors_per_slot": 1.5,
    "winding_type": 1.5, "connection_type": 1.0, "rated_current": 2.0,
    "efficiency": 2.0, "power_factor": 1.5, "rated_freq": 1.5, "motor_weight": 1.0,
}
# 判定阈值
SIM_TOL_REL = 0.02          # 数值相对差 <=2% 视为「相同」
SIM_DECAY_REL = 0.25        # 相对差 >=25% 得 0 分
SIM_VERDICT_IDENTICAL = 0.995
SIM_VERDICT_NEAR = 0.95
SIM_VERDICT_SIMILAR = 0.80


def _field_label_map() -> Dict[str, str]:
    """字段 -> 中文标签 (含 meta 字段)"""
    m = {f[0]: f[1] for f in MOTOR_PARAM_FIELDS}
    m.update({"motor_type": "电机类型", "motor_model": "电机型号",
              "motor_name": "电机名称", "series_id": "系列号",
              "series_name": "系列名称", "case_code": "案例编号",
              "notes": "备注/项目"})
    return m


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _field_score(a: Any, b: Any) -> Optional[float]:
    """单字段相似分 [0,1]; 返回 None 表示两边都空 -> 不参与计分"""
    if _blank(a) and _blank(b):
        return None
    if _blank(a) or _blank(b):
        return 0.0
    try:
        fa, fb = float(a), float(b)
        denom = max(abs(fa), abs(fb))
        if denom < 1e-12:
            return 1.0
        rel = abs(fa - fb) / denom
        if rel <= SIM_TOL_REL:
            return 1.0
        if rel >= SIM_DECAY_REL:
            return 0.0
        return 1.0 - (rel - SIM_TOL_REL) / (SIM_DECAY_REL - SIM_TOL_REL)
    except (TypeError, ValueError):
        return 1.0 if str(a).strip().lower() == str(b).strip().lower() else 0.0


def compare_two_motors(a: Dict[str, Any], b: Dict[str, Any],
                       skip_blank_a: bool = False) -> Dict[str, Any]:
    """逐字段对比两台电机, 返回相似度 + 相同项 + 差异项 (审核合并 UI 直接可用)

    skip_blank_a: 搜索场景专用 —— 若 a(候选) 某字段为空, 视为「未提供/不计入」而非「不匹配」,
                  避免候选只传部分字段时其余空字段把总分拉低到 0 (否则精确匹配也命不中)
    """
    labels = _field_label_map()
    weights = dict(_SIM_WEIGHTS_IDENTITY)
    weights.update(_SIM_WEIGHTS_STRUCT)
    # 其余可比字段按低权重纳入
    for f in MOTOR_PARAM_FIELDS:
        weights.setdefault(f[0], 0.5)

    wsum = 0.0
    score_sum = 0.0
    same_fields, diff_fields = [], []
    identity_all_equal = True

    for field, w in weights.items():
        va, vb = a.get(field), b.get(field)
        if skip_blank_a and _blank(va):
            # 候选未提供该字段 -> 不计入 (不奖不罚)
            continue
        s = _field_score(va, vb)
        if s is None:
            continue
        wsum += w
        score_sum += w * s
        rec = {"field": field, "label": labels.get(field, field),
               "value_a": va, "value_b": vb, "score": round(s, 4)}
        if s >= 1.0:
            same_fields.append(rec)
        else:
            diff_fields.append(rec)
            if field in _SIM_WEIGHTS_IDENTITY:
                identity_all_equal = False

    similarity = (score_sum / wsum) if wsum > 0 else 0.0
    # 差异项按权重降序, 让审核者先看关键差异
    diff_fields.sort(key=lambda r: -weights.get(r["field"], 0.5))

    if identity_all_equal and similarity >= SIM_VERDICT_IDENTICAL:
        verdict, verdict_cn = "identical", "完全相同"
    elif similarity >= SIM_VERDICT_NEAR:
        verdict, verdict_cn = "near_identical", "高度相近"
    elif similarity >= SIM_VERDICT_SIMILAR:
        verdict, verdict_cn = "similar", "相似"
    else:
        verdict, verdict_cn = "different", "不同"

    return {
        "similarity": round(similarity, 4),
        "similarity_pct": round(similarity * 100, 2),
        "verdict": verdict,
        "verdict_cn": verdict_cn,
        "identity_match": identity_all_equal,
        "same_count": len(same_fields),
        "diff_count": len(diff_fields),
        "same_fields": same_fields,
        "diff_fields": diff_fields,
        "suggest_action": ("merge" if verdict in ("identical", "near_identical")
                           else "new_entry"),
    }


def find_similar_case_motors(candidate: Dict[str, Any],
                             exclude_id: Any = None,
                             top_n: int = 5,
                             threshold: float = SIM_VERDICT_SIMILAR,
                             status_filter: str = None,
                             scan_limit: int = 500) -> List[Dict[str, Any]]:
    """在库内检索与 candidate 相近/相同的案例电机 (场景一.3)

    candidate: 待审核电机参数 dict (至少含 motor_model / rated_power / poles 之一)
    exclude_id: 排除自身 id
    threshold: 相似度下限, 低于则不返回
    返回: 按相似度降序的匹配列表, 每项含 case(库内记录) + 对比明细
    """
    sql = "SELECT * FROM case_motors WHERE 1=1"
    params: List[Any] = []
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    if exclude_id not in (None, ""):
        sql += " AND id <> ?"
        params.append(exclude_id)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(scan_limit)

    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    matches = []
    for row in rows:
        cmp_res = compare_two_motors(candidate, row, skip_blank_a=True)
        if cmp_res["similarity"] < threshold:
            continue
        matches.append({
            "case_id": row.get("id"),
            "case_code": row.get("case_code"),
            "motor_name": row.get("motor_name", ""),
            "motor_model": row.get("motor_model", ""),
            "motor_type": row.get("motor_type", ""),
            "series_id": row.get("series_id", ""),
            "status": row.get("status", ""),
            "created_at": row.get("created_at", ""),
            **cmp_res,
        })
    matches.sort(key=lambda m: -m["similarity"])
    return matches[: max(1, int(top_n or 5))]


def compare_case_motor_batch(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批次内横向对比 (场景一.2: 系列电机批量录入打包审核)

    items: 待审核电机 dict 列表 (可来自导入批次, 未入库亦可)
    返回: common(全批一致的字段) / differences(逐字段各台取值) / pairwise(两两相似度)
    """
    labels = _field_label_map()
    n = len(items or [])
    if n == 0:
        return {"count": 0, "common": [], "differences": [], "pairwise": []}

    fields = [f[0] for f in MOTOR_PARAM_FIELDS] + [
        "motor_type", "motor_model", "motor_name", "series_id", "series_name"]
    seen = set()
    ordered_fields = [f for f in fields if not (f in seen or seen.add(f))]

    def tag(it, idx):
        return (it.get("motor_name") or it.get("motor_model")
                or it.get("case_code") or ("#%d" % (idx + 1)))

    tags = [tag(it, i) for i, it in enumerate(items)]
    common, differences = [], []
    for field in ordered_fields:
        vals = [it.get(field) for it in items]
        if all(_blank(v) for v in vals):
            continue
        first = vals[0]
        if all((_field_score(first, v) or 1.0) >= 1.0 for v in vals[1:]):
            common.append({"field": field, "label": labels.get(field, field), "value": first})
        else:
            differences.append({
                "field": field, "label": labels.get(field, field),
                "values": [{"tag": tags[i], "value": vals[i]} for i in range(n)],
            })

    pairwise = []
    for i in range(n):
        for j in range(i + 1, n):
            r = compare_two_motors(items[i], items[j])
            pairwise.append({"a": tags[i], "b": tags[j],
                             "a_index": i, "b_index": j,
                             "similarity": r["similarity"],
                             "similarity_pct": r["similarity_pct"],
                             "verdict": r["verdict"], "verdict_cn": r["verdict_cn"],
                             "diff_count": r["diff_count"]})
    pairwise.sort(key=lambda p: -p["similarity"])

    dup_pairs = [p for p in pairwise if p["verdict"] in ("identical", "near_identical")]
    return {
        "count": n,
        "tags": tags,
        "common_count": len(common),
        "diff_count": len(differences),
        "common": common,
        "differences": differences,
        "pairwise": pairwise,
        "duplicate_pairs": dup_pairs,
        "has_duplicate": bool(dup_pairs),
    }


# ============================================================
# 参数血缘 (单参数级追踪)
# ============================================================

def upsert_param_bloodline(
    case_id: int,
    param_name: str,
    param_value: str,
    param_unit: str = "",
    source_type: str = "user_input",
    source_detail: str = "",
    confidence: float = 100,
    status: str = "confirmed",
    modified_reason: str = "",
    modified_note: str = "",
) -> int:
    """插入或更新单个参数的血缘记录"""
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM case_motor_params WHERE case_id = ? AND param_name = ?",
            (case_id, param_name)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE case_motor_params SET
                    param_value = ?, param_unit = ?, source_type = ?, source_detail = ?,
                    confidence = ?, status = ?, modified_at = CURRENT_TIMESTAMP,
                    modified_reason = ?, modified_note = ?
                WHERE id = ?
            """, (str(param_value), param_unit, source_type, source_detail,
                  confidence, status, modified_reason, modified_note, existing["id"]))
            param_id = existing["id"]
        else:
            cursor = conn.execute("""
                INSERT INTO case_motor_params
                    (case_id, param_name, param_value, param_unit, source_type,
                     source_detail, confidence, status, modified_reason, modified_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (case_id, param_name, str(param_value), param_unit, source_type,
                  source_detail, confidence, status, modified_reason, modified_note))
            param_id = cursor.lastrowid
        conn.commit()
    return param_id


def get_param_bloodline(case_id: int) -> List[Dict[str, Any]]:
    """获取案例电机所有参数的血缘记录"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM case_motor_params WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 验算结果
# ============================================================

def save_verification(
    case_id: int,
    verify_type: str,
    result: str,
    details: List[Dict],
    summary: str = "",
    warning_items: str = "",
) -> int:
    """保存验算结果"""
    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO case_motor_verifications
                (case_id, verify_type, result, details_json, summary, warning_items)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (case_id, verify_type, result, json.dumps(details, ensure_ascii=False),
              summary, warning_items))
        verify_id = cursor.lastrowid
        # 如果验算通过, 更新案例状态
        if result == "pass":
            conn.execute(
                "UPDATE case_motors SET status = 'verified', verified_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (case_id,)
            )
        elif result == "warning":
            conn.execute(
                "UPDATE case_motors SET status = 'warning', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (case_id,)
            )
        elif result == "fail":
            conn.execute(
                "UPDATE case_motors SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (case_id,)
            )
        conn.commit()
    return verify_id


def get_verification_history(case_id: int) -> List[Dict[str, Any]]:
    """获取验算历史"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM case_motor_verifications WHERE case_id = ? ORDER BY created_at DESC",
            (case_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 草稿
# ============================================================

def save_draft(case_id: int, user_id: int, draft_data: Dict) -> int:
    """保存草稿"""
    with _get_conn() as conn:
        if case_id:
            existing = conn.execute(
                "SELECT id FROM case_motor_drafts WHERE case_id = ? AND user_id = ?",
                (case_id, user_id)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE case_motor_drafts SET draft_data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (json.dumps(draft_data, ensure_ascii=False), existing["id"])
                )
                conn.commit()
                return existing["id"]
        cursor = conn.execute("""
            INSERT INTO case_motor_drafts (case_id, user_id, draft_data)
            VALUES (?, ?, ?)
        """, (case_id, user_id, json.dumps(draft_data, ensure_ascii=False)))
        conn.commit()
        return cursor.lastrowid


def load_draft(draft_id: int) -> Optional[Dict[str, Any]]:
    """加载草稿"""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM case_motor_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["draft_data"] = json.loads(row["draft_data"])
        return result


def list_drafts(user_id: int) -> List[Dict[str, Any]]:
    """列出用户草稿"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM case_motor_drafts WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# 统计
# ============================================================

def get_statistics() -> Dict[str, Any]:
    """获取案例电机统计信息"""
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM case_motors").fetchone()["cnt"]
        by_status = {}
        for row in conn.execute("SELECT status, COUNT(*) as cnt FROM case_motors GROUP BY status"):
            by_status[row["status"]] = row["cnt"]
        by_series = {}
        for row in conn.execute("SELECT series_id, COUNT(*) as cnt FROM case_motors GROUP BY series_id"):
            by_series[row["series_id"] or "未分类"] = row["cnt"]
        recent = conn.execute(
            "SELECT COUNT(*) as cnt FROM case_motors WHERE created_at >= date('now', '-7 days')"
        ).fetchone()["cnt"]
    return {
        "total": total,
        "by_status": by_status,
        "by_series": by_series,
        "recent_7days": recent,
    }


def calculate_completeness(data: Dict[str, Any]) -> float:
    """计算参数完整度 (0-100)"""
    # 关键字段权重
    key_fields = {
        "rated_power": 10, "rated_voltage": 8, "rated_speed": 8, "poles": 8,
        "efficiency": 10, "power_factor": 6, "stator_od": 8, "core_length": 6,
        "slots_stator": 5, "turns_per_coil": 5, "temperature_rise": 5,
        "insulation_class": 3, "protection_class": 3, "cooling_method": 3,
        "rated_current": 5, "air_gap": 3, "stator_tooth_flux": 3, "current_density": 3,
        "copper_loss": 2, "iron_loss": 2,
        # v4.00.009: 新增关键字段权重
        "stator_id": 4, "rotor_od": 4, "shaft_dia": 2, "yoke_thickness": 2,
        "stator_hs0": 2, "stator_bs0": 2, "stator_hs2": 3, "stator_bs2": 3,
        "rotor_hs0": 2, "rotor_bs0": 2, "rotor_hs2": 3, "rotor_bs2": 3,
        "winding_type": 2, "connection_type": 2, "conductors_per_slot": 3,
        "stator_material": 2, "conductor_material": 2,
    }
    total_weight = sum(key_fields.values())
    filled_weight = 0
    for field, weight in key_fields.items():
        val = data.get(field)
        if val is not None and str(val).strip() != "":
            filled_weight += weight
    return round(filled_weight / total_weight * 100, 1) if total_weight > 0 else 0


# ==================== 用户模版 (系列 × 电机类型 二维维度, v4.02) ====================

def _row_to_user_template(r):
    """将一行 series_templates 转为 dict (JSON 字段反序列化)"""
    if not r:
        return None
    return {
        "id": r["id"],
        "series": r["series"],
        "motor_type": r["motor_type"],
        "name": r["name"],
        "default_params": json.loads(r["default_params"] or "{}"),
        "typical_efficiency_range": json.loads(r["typical_efficiency_range"] or "{}"),
        "notes": r["notes"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def list_user_templates(series=None, motor_type=None):
    """列出用户自定义模版, 可按系列 / 电机类型过滤"""
    with _get_conn() as conn:
        sql = "SELECT * FROM series_templates WHERE 1=1"
        params = []
        if series:
            sql += " AND series = ?"
            params.append(series)
        if motor_type:
            sql += " AND motor_type = ?"
            params.append(motor_type)
        sql += " ORDER BY series, motor_type, name"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_user_template(r) for r in rows]


def get_user_template(tpl_id):
    """按 id 获取单个用户模版"""
    with _get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM series_templates WHERE id = ?", (tpl_id,)
        ).fetchone()
        return _row_to_user_template(r)


def create_user_template(data):
    """创建用户模版, data 含 series/motor_type/name/default_params/..."""
    now = _local_now()
    with _get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO series_templates
               (series, motor_type, name, default_params, typical_efficiency_range, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                data.get("series", ""),
                data.get("motor_type", ""),
                data.get("name", ""),
                json.dumps(data.get("default_params", {}), ensure_ascii=False),
                json.dumps(data.get("typical_efficiency_range", {}), ensure_ascii=False),
                data.get("notes", ""),
                now, now,
            ),
        )
        tpl_id = cur.lastrowid
    # 退出 with (已提交) 后再单独查询, 避免 WAL 下跨连接未提交不可见
    return get_user_template(tpl_id)


def update_user_template(tpl_id, data):
    """更新用户模版 (全字段覆盖)"""
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM series_templates WHERE id = ?", (tpl_id,)
        ).fetchone()
        if not existing:
            return None
        conn.execute(
            """UPDATE series_templates SET
               series=?, motor_type=?, name=?, default_params=?,
               typical_efficiency_range=?, notes=?, updated_at=?
               WHERE id=?""",
            (
                data.get("series", ""),
                data.get("motor_type", ""),
                data.get("name", ""),
                json.dumps(data.get("default_params", {}), ensure_ascii=False),
                json.dumps(data.get("typical_efficiency_range", {}), ensure_ascii=False),
                data.get("notes", ""),
                _local_now(),
                tpl_id,
            ),
        )
    # 退出 with (已提交) 后再单独查询
    return get_user_template(tpl_id)


def delete_user_template(tpl_id):
    """删除用户模版, 返回是否成功"""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM series_templates WHERE id = ?", (tpl_id,)
        )
        return cur.rowcount > 0
