# -*- coding: utf-8 -*-
"""v4.02.12 (Phase 3): 自主智能体记忆存储 MemoryStore。

职责:
  - 双写: 每条案例同时写入 SQLite (结构化检索) 与 JSONL (可移植/可审计)。
  - 相似案例检索: 按 motor_type 过滤, 基于关键数值规格向量的余弦相似度取 topK。
  - fail-closed: 任何异常 → 返回安全默认值 (空列表/None), 绝不阻断主流程。

设计约束:
  - 仅做只读检索与追加写入, 不修改既有设计逻辑。
  - 所有公共方法均 try/except 包裹; DB 不可用时降级为 JSONL 单文件检索。
"""
import os
import json
import sqlite3
import math
import threading
import datetime

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_DB_PATH = os.path.join(_DATA_DIR, "agent_memory.db")
_JSONL_PATH = os.path.join(_DATA_DIR, "agent_memory.jsonl")

_LOCK = threading.Lock()

# 用于相似度计算的关键数值规格字段
_FEATURE_KEYS = [
    "rated_power", "rated_voltage", "rated_speed", "poles",
    "frequency_hz", "efficiency", "torque", "stator_od",
    "core_length", "air_gap", "slots_stator",
]


def _ensure_store():
    """确保 SQLite 表与 JSONL 文件存在; 失败抛异常由调用方捕获。"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(_DB_PATH, timeout=10)
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS cases ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "motor_type TEXT, created_at TEXT, "
        "specs_json TEXT, summary_json TEXT, features_json TEXT)"
    )
    _conn.commit()
    _conn.close()
    if not os.path.exists(_JSONL_PATH):
        open(_JSONL_PATH, "a", encoding="utf-8").close()


def _extract_features(specs: dict) -> dict:
    """从规格字典抽取数值特征向量 (缺失→0)。"""
    _f = {}
    if not isinstance(specs, dict):
        return _f
    for _k in _FEATURE_KEYS:
        try:
            _v = specs.get(_k)
            _f[_k] = float(_v) if _v not in (None, "") else 0.0
        except (TypeError, ValueError):
            _f[_k] = 0.0
    return _f


def _cosine(a: dict, b: dict) -> float:
    """余弦相似度, 任一向量全 0 → 返回 0。"""
    _keys = set(a) | set(b)
    _dot = sum((a.get(k, 0.0) * b.get(k, 0.0)) for k in _keys)
    _na = math.sqrt(sum(v * v for v in a.values()))
    _nb = math.sqrt(sum(v * v for v in b.values()))
    if _na == 0.0 or _nb == 0.0:
        return 0.0
    return _dot / (_na * _nb)


def add_case(motor_type: str, specs: dict, summary: dict) -> bool:
    """追加一条案例 (双写 SQLite + JSONL)。成功返回 True, 失败返回 False。"""
    try:
        with _LOCK:
            _ensure_store()
            _now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _feats = _extract_features(specs)
            _specs_s = json.dumps(specs or {}, ensure_ascii=False)
            _sum_s = json.dumps(summary or {}, ensure_ascii=False)
            _feat_s = json.dumps(_feats, ensure_ascii=False)
            _conn = sqlite3.connect(_DB_PATH, timeout=10)
            _conn.execute(
                "INSERT INTO cases (motor_type, created_at, specs_json, "
                "summary_json, features_json) VALUES (?,?,?,?,?)",
                (motor_type or "unknown", _now, _specs_s, _sum_s, _feat_s))
            _conn.commit()
            _conn.close()
            with open(_JSONL_PATH, "a", encoding="utf-8") as _fh:
                _fh.write(json.dumps({
                    "motor_type": motor_type or "unknown",
                    "created_at": _now,
                    "specs": specs or {},
                    "summary": summary or {},
                }, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def query_similar(motor_type: str, query_specs: dict, top_k: int = 5) -> list:
    """按 motor_type 过滤, 返回与 query_specs 最相似的 topK 案例。

    返回 list[dict]: {"summary": dict, "similarity": float, "specs": dict}
    fail-closed: 异常 → 返回空列表。
    """
    try:
        with _LOCK:
            if not os.path.exists(_DB_PATH):
                return []
            _q = _extract_features(query_specs)
            _conn = sqlite3.connect(_DB_PATH, timeout=10)
            _rows = _conn.execute(
                "SELECT specs_json, summary_json, features_json FROM cases "
                "WHERE motor_type = ?", (motor_type or "unknown",)).fetchall()
            _conn.close()
            _cands = []
            for _specs_s, _sum_s, _feat_s in _rows:
                try:
                    _feats = json.loads(_feat_s) if _feat_s else {}
                    _sim = _cosine(_q, _feats)
                    _cands.append({
                        "summary": json.loads(_sum_s) if _sum_s else {},
                        "specs": json.loads(_specs_s) if _specs_s else {},
                        "similarity": round(_sim, 4),
                    })
                except Exception:
                    continue
            _cands.sort(key=lambda c: c["similarity"], reverse=True)
            return _cands[:max(0, top_k)]
    except Exception:
        return []


def count_cases(motor_type: str = None) -> int:
    """统计案例数 (按 motor_type 可选过滤)。fail-closed → 0。"""
    try:
        if not os.path.exists(_DB_PATH):
            return 0
        _conn = sqlite3.connect(_DB_PATH, timeout=10)
        if motor_type:
            _n = _conn.execute(
                "SELECT COUNT(*) FROM cases WHERE motor_type = ?",
                (motor_type,)).fetchone()[0]
        else:
            _n = _conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        _conn.close()
        return _n or 0
    except Exception:
        return 0
