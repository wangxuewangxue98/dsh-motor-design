"""
field_mapping_flywheel.py - 字段映射数据飞轮 (流程级自主进化)

将「人工字段映射」沉淀为「可自动复用的映射规则」, 实现录入自动化的闭环进化:
    录入映射 → 记录埋点(learn) → 进化引擎(置信度) → 规则库(全局共享·自动应用)
             → 下次录入时预判(suggest) → 人工修正回灌(correct) → 持续优化

核心表:
  - field_mapping_rules : 映射规则 (source_field -> target_field), 含出现次数/修正次数/
                          置信度/自动应用标记/作用域(project/enterprise)/系列范围
  - field_mapping_audit : 审计日志 (learn/correct/promote/demote/reject), 支持回滚追溯

置信度算法:
  occ < 3            : conf = 0.3 + 0.1*occ                  (仅静态, 不主动建议为 auto)
  occ >= 3           : conf = min(0.6 + 0.1*(occ-3), 0.98)   (生成规则, 仅建议)
  occ>=5 且 修正=0 且 conf>=0.8 : auto_apply=1               (自动应用)
  每次人工修正       : conf -= 0.2 (按 correction_count 计); conf<0.5 -> 降级仅建议
  源名变体           : difflib 相似度 >= 0.6 套用并按相似度打折

三层机制:
  project    : 规则在单一系列内学习/应用
  enterprise : 同一 (source_field,target_field) 在 >=2 个系列出现 -> 自动升级为企业级(全局共享)

安全约束:
  target_field 必须 ∈ 标准字段库 (MOTOR_PARAM_FIELDS + motor_type/motor_model/
  motor_name/series_id/series_name), 非法目标拒收并写审计。

v1.0  2026-08-15  #525 初始版本 (完整三层 + CSV导入&手动录入)
"""
import os
import re
import json
import sqlite3
import difflib
from datetime import datetime
from typing import Dict, List, Any, Optional, Iterable

# 与 case_motor_db 共用同一数据库 (全局共享层)
# 默认库与 case_motor_db 保持一致 (designs_dev.db), 避免无 DB_PATH 环境变量时
# 飞轮表(由 case_motor_db 建在 dev 库)与路由写入(本模块默认库)分叉到不同文件。
# 若显式设置 DB_PATH 环境变量, 两侧仍统一走该值。
try:
    from Core.case_motor_db import DB_PATH as _CM_DB_PATH  # type: ignore
    DB_PATH = _CM_DB_PATH
except Exception:
    DB_PATH = os.getenv("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs_dev.db")

# ---- 进化引擎阈值 (集中管理, 便于调参) ----
MIN_OCC_FOR_RULE = 3        # 达到该出现次数才"生成规则"(可被建议)
MIN_OCC_FOR_AUTO = 5        # 达到该出现次数且零修正才可"自动应用"
AUTO_CONF_THRESHOLD = 0.80  # 自动应用的置信度下限
DEMOTE_CONF_THRESHOLD = 0.50  # 低于此值降级为"仅建议"
CORRECTION_PENALTY = 0.20   # 每次人工修正的置信度惩罚
CONF_CEILING = 0.98         # 置信度上限
FUZZY_RATIO_MIN = 0.60      # 源名变体模糊命中的相似度下限
ENTERPRISE_MIN_SERIES = 2   # 跨 >= N 个系列自动升级企业级


# ============================================================
# 连接 & 建表
# ============================================================

def _get_conn():
    """获取数据库连接 (WAL 模式, 与 case_motor_db 一致)"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_field_mapping_tables(conn: Optional[sqlite3.Connection] = None):
    """初始化字段映射飞轮相关表 (CREATE IF NOT EXISTS, 对现有数据零影响)。

    可由 case_motor_db.init_case_motor_tables() 复用已有 conn 调用, 也可独立调用。
    """
    own = conn is None
    if own:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = _get_conn()
    try:
        # ---- 映射规则表 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS field_mapping_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_field TEXT NOT NULL,          -- 归一化源字段名(外部列名)
                source_field_raw TEXT DEFAULT '',    -- 最近一次原始源字段名(未归一)
                target_field TEXT NOT NULL,          -- 系统标准字段
                target_label TEXT DEFAULT '',        -- 目标字段中文标签(快照)
                data_type TEXT DEFAULT '',           -- num / text
                scope TEXT DEFAULT 'project',        -- project / enterprise
                series_scope TEXT DEFAULT '[]',      -- JSON: 出现过的 series_id 列表
                occurrence_count INTEGER DEFAULT 0,
                correction_count INTEGER DEFAULT 0,
                confidence REAL DEFAULT 0.3,
                auto_apply INTEGER DEFAULT 0,         -- 0/1
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_field, target_field)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fmr_source ON field_mapping_rules(source_field)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fmr_scope ON field_mapping_rules(scope)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fmr_auto ON field_mapping_rules(auto_apply)")

        # ---- 审计日志表 ----
        conn.execute("""
            CREATE TABLE IF NOT EXISTS field_mapping_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,        -- learn/correct/promote/demote/reject/rollback
                source_field TEXT DEFAULT '',
                source_field_raw TEXT DEFAULT '',
                target_field TEXT DEFAULT '',
                old_target TEXT DEFAULT '',  -- correct 时: 被替换的目标
                scope TEXT DEFAULT '',
                series_id TEXT DEFAULT '',
                confidence REAL,
                occurrence_count INTEGER,
                correction_count INTEGER,
                detail TEXT DEFAULT '',      -- JSON
                user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fma_action ON field_mapping_audit(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fma_created ON field_mapping_audit(created_at DESC)")

        if own:
            conn.commit()
            print("[field_mapping_flywheel] 字段映射飞轮表初始化完成")
    finally:
        if own:
            conn.close()


# ============================================================
# 标准字段库 (target 合法域)
# ============================================================

_STD_FIELDS_CACHE: Optional[Dict[str, str]] = None


def get_standard_fields() -> Dict[str, str]:
    """返回 {标准字段名: 中文标签}。target_field 必须 ∈ 该集合的键。

    延迟导入 case_motor_db 以规避循环依赖。
    """
    global _STD_FIELDS_CACHE
    if _STD_FIELDS_CACHE is not None:
        return _STD_FIELDS_CACHE
    fields: Dict[str, str] = {}
    try:
        try:
            from Core.case_motor_db import MOTOR_PARAM_FIELDS  # type: ignore
        except Exception:
            from case_motor_db import MOTOR_PARAM_FIELDS  # type: ignore
        for f in MOTOR_PARAM_FIELDS:
            fields[f[0]] = f[1]
    except Exception as e:
        print(f"[field_mapping_flywheel] 载入标准字段库失败(降级为仅 meta): {e}")
    # 三层语义 meta 字段 + 系列
    fields.update({
        "motor_type": "电机类型",
        "motor_model": "电机型号",
        "motor_name": "电机名称",
        "series_id": "系列号",
        "series_name": "系列名称",
    })
    _STD_FIELDS_CACHE = fields
    return fields


def is_valid_target(target_field: str) -> bool:
    return bool(target_field) and target_field in get_standard_fields()


# ============================================================
# 归一化 & 置信度算法
# ============================================================

def _norm(s: Any) -> str:
    """源字段名归一化: 去除非(字母/数字/中文)字符并转小写。

    与客户端 data_center_panel._norm 语义保持一致, 便于 exact 命中。
    """
    if not s:
        return ""
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]', '', str(s).strip().lower())


def _compute_confidence(occ: int, corrections: int) -> float:
    """按出现次数与修正次数计算置信度。"""
    if occ < MIN_OCC_FOR_RULE:
        base = 0.3 + 0.1 * occ
    else:
        base = min(0.6 + 0.1 * (occ - MIN_OCC_FOR_RULE), CONF_CEILING)
    conf = base - CORRECTION_PENALTY * max(0, corrections)
    return round(max(0.0, min(CONF_CEILING, conf)), 4)


def _decide_auto_apply(occ: int, corrections: int, conf: float) -> int:
    """是否可自动应用: 出现>=5 且 零修正 且 置信度>=0.8。"""
    if occ >= MIN_OCC_FOR_AUTO and corrections == 0 and conf >= AUTO_CONF_THRESHOLD:
        return 1
    return 0


def _series_list(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    try:
        v = json.loads(raw)
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []
    except Exception:
        return []


# ============================================================
# 审计
# ============================================================

def _audit(conn, action: str, *, source_field="", source_field_raw="",
           target_field="", old_target="", scope="", series_id="",
           confidence=None, occurrence_count=None, correction_count=None,
           detail=None, user_id=None):
    conn.execute("""
        INSERT INTO field_mapping_audit
            (action, source_field, source_field_raw, target_field, old_target,
             scope, series_id, confidence, occurrence_count, correction_count,
             detail, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (action, source_field, source_field_raw, target_field, old_target,
          scope, series_id,
          confidence, occurrence_count, correction_count,
          json.dumps(detail, ensure_ascii=False) if detail is not None else "",
          user_id))


def list_audit(limit: int = 100, action: str = None) -> List[Dict[str, Any]]:
    """查询审计日志 (支持回溯/回滚参考)。"""
    sql = "SELECT * FROM field_mapping_audit WHERE 1=1"
    params: List[Any] = []
    if action:
        sql += " AND action = ?"
        params.append(action)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ============================================================
# 学习 (record + evolve)
# ============================================================

def _normalize_mapping_items(mappings: Any) -> List[Dict[str, str]]:
    """把入参统一为 [{source_field, target_field, data_type}] 列表。

    支持:
      - dict: {源列名: 系统字段}
      - list: [{"source_field":..,"target_field":..,"data_type":..}, ...]
    """
    items: List[Dict[str, str]] = []
    if isinstance(mappings, dict):
        for src, tgt in mappings.items():
            items.append({"source_field": str(src), "target_field": str(tgt),
                          "data_type": ""})
    elif isinstance(mappings, (list, tuple)):
        for m in mappings:
            if isinstance(m, dict):
                items.append({
                    "source_field": str(m.get("source_field", "")),
                    "target_field": str(m.get("target_field", "")),
                    "data_type": str(m.get("data_type", "")),
                })
    return items


def learn_mappings(mappings: Any, series_id: str = "", user_id: int = None) -> Dict[str, Any]:
    """记录一批人工确认的字段映射, 驱动规则进化。

    对每条 (source_field -> target_field):
      1. 校验 target 合法 (非法 -> 拒收 + 审计 reject)
      2. upsert 规则: occurrence_count+1, 合入 series_id, 重算 confidence/auto_apply
      3. 跨 >=2 系列 -> 自动升级 enterprise
      4. 写审计 learn (及 promote)

    返回: {learned, rejected, promoted, rules:[...]}
    """
    std = get_standard_fields()
    items = _normalize_mapping_items(mappings)
    learned, rejected, promoted = 0, 0, 0
    out_rules = []
    series_id = (series_id or "").strip()

    with _get_conn() as conn:
        for it in items:
            raw = it["source_field"].strip()
            tgt = it["target_field"].strip()
            src = _norm(raw)
            if not src or not tgt:
                continue
            # 特殊映射占位 (客户端把 __motor_type__ 之类当特殊列) 不入库
            if tgt.startswith("__") and tgt.endswith("__"):
                continue
            if tgt not in std:
                rejected += 1
                _audit(conn, "reject", source_field=src, source_field_raw=raw,
                       target_field=tgt, series_id=series_id, user_id=user_id,
                       detail={"reason": "target 不在标准字段库"})
                continue

            row = conn.execute(
                "SELECT * FROM field_mapping_rules WHERE source_field=? AND target_field=?",
                (src, tgt)
            ).fetchone()

            if row:
                occ = int(row["occurrence_count"]) + 1
                corr = int(row["correction_count"])
                series = _series_list(row["series_scope"])
                if series_id and series_id not in series:
                    series.append(series_id)
                scope = row["scope"]
                new_promoted = False
                if scope != "enterprise" and len(series) >= ENTERPRISE_MIN_SERIES:
                    scope = "enterprise"
                    new_promoted = True
                conf = _compute_confidence(occ, corr)
                auto = _decide_auto_apply(occ, corr, conf)
                conn.execute("""
                    UPDATE field_mapping_rules SET
                        source_field_raw=?, data_type=COALESCE(NULLIF(?,''), data_type),
                        target_label=?, occurrence_count=?, confidence=?, auto_apply=?,
                        scope=?, series_scope=?, last_seen=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (raw, it["data_type"], std.get(tgt, ""), occ, conf, auto,
                      scope, json.dumps(series, ensure_ascii=False), row["id"]))
                _audit(conn, "learn", source_field=src, source_field_raw=raw,
                       target_field=tgt, scope=scope, series_id=series_id,
                       confidence=conf, occurrence_count=occ, correction_count=corr,
                       user_id=user_id)
                if new_promoted:
                    promoted += 1
                    _audit(conn, "promote", source_field=src, target_field=tgt,
                           scope="enterprise", series_id=series_id,
                           confidence=conf, occurrence_count=occ,
                           detail={"series_scope": series}, user_id=user_id)
                rule_id = row["id"]
            else:
                occ, corr = 1, 0
                series = [series_id] if series_id else []
                conf = _compute_confidence(occ, corr)
                auto = _decide_auto_apply(occ, corr, conf)
                cur = conn.execute("""
                    INSERT INTO field_mapping_rules
                        (source_field, source_field_raw, target_field, target_label,
                         data_type, scope, series_scope, occurrence_count,
                         correction_count, confidence, auto_apply)
                    VALUES (?, ?, ?, ?, ?, 'project', ?, ?, ?, ?, ?)
                """, (src, raw, tgt, std.get(tgt, ""), it["data_type"],
                      json.dumps(series, ensure_ascii=False), occ, corr, conf, auto))
                rule_id = cur.lastrowid
                _audit(conn, "learn", source_field=src, source_field_raw=raw,
                       target_field=tgt, scope="project", series_id=series_id,
                       confidence=conf, occurrence_count=occ, correction_count=corr,
                       user_id=user_id)
            learned += 1
            out_rules.append({"id": rule_id, "source_field": src, "target_field": tgt,
                              "confidence": conf, "occurrence_count": occ,
                              "auto_apply": auto})
        conn.commit()
    return {"learned": learned, "rejected": rejected, "promoted": promoted,
            "rules": out_rules}


# ============================================================
# 建议 (predict)
# ============================================================

def _match_rule_rows(conn, src: str, series_id: str) -> List[sqlite3.Row]:
    """精确命中: source_field==src。优先 enterprise, 其次匹配当前系列的 project。"""
    return conn.execute(
        "SELECT * FROM field_mapping_rules WHERE source_field=? ORDER BY confidence DESC",
        (src,)
    ).fetchall()


def _rule_applicable(row, series_id: str) -> bool:
    """规则是否适用于当前系列: enterprise 全局适用; project 需系列匹配 (或规则无系列限定)。"""
    if row["scope"] == "enterprise":
        return True
    series = _series_list(row["series_scope"])
    if not series:
        return True
    return (not series_id) or (series_id in series)


def suggest_mappings(source_fields: Iterable[str], series_id: str = "") -> Dict[str, Any]:
    """对一批外部源字段名给出映射建议。

    优先级: 学习-自动应用(auto) > 学习-建议(exact) > 模糊(fuzzy, difflib>=0.6 打折)
    返回: {suggestions: {原始源列名: {...}}, stats:{...}}
    每条建议含: target_field/target_label/confidence/auto_apply/scope/match_type/ratio/source
    """
    series_id = (series_id or "").strip()
    src_list = [str(s) for s in (source_fields or [])]
    suggestions: Dict[str, Any] = {}
    auto_cnt = sugg_cnt = fuzzy_cnt = miss_cnt = 0

    with _get_conn() as conn:
        all_rows = conn.execute("SELECT * FROM field_mapping_rules").fetchall()
        # 预建 归一源名 -> 候选规则 索引
        by_src: Dict[str, List[sqlite3.Row]] = {}
        for r in all_rows:
            by_src.setdefault(r["source_field"], []).append(r)
        all_src_keys = list(by_src.keys())

        for raw in src_list:
            src = _norm(raw)
            best = None  # (target_field, conf, auto, scope, match_type, ratio, rule_id)

            # 1) 精确命中
            if src in by_src:
                cands = [r for r in by_src[src] if _rule_applicable(r, series_id)]
                if cands:
                    # auto_apply 优先, 再按置信度; project 内系列匹配优先于无系列
                    cands.sort(key=lambda r: (r["auto_apply"], r["confidence"],
                                              1 if series_id in _series_list(r["series_scope"]) else 0),
                               reverse=True)
                    r = cands[0]
                    if r["confidence"] >= 0.3:  # 有效规则
                        best = (r["target_field"], round(float(r["confidence"]), 4),
                                int(r["auto_apply"]), r["scope"], "exact", 1.0, r["id"])

            # 2) 模糊命中 (源名变体)
            if best is None and src and all_src_keys:
                near = difflib.get_close_matches(src, all_src_keys, n=1, cutoff=FUZZY_RATIO_MIN)
                if near:
                    ratio = difflib.SequenceMatcher(None, src, near[0]).ratio()
                    cands = [r for r in by_src[near[0]] if _rule_applicable(r, series_id)]
                    cands.sort(key=lambda r: r["confidence"], reverse=True)
                    if cands:
                        r = cands[0]
                        conf = round(float(r["confidence"]) * ratio, 4)  # 相似度打折
                        if conf >= 0.3:
                            best = (r["target_field"], conf, 0, r["scope"],
                                    "fuzzy", round(ratio, 4), r["id"])

            if best is None:
                miss_cnt += 1
                continue

            tgt, conf, auto, scope, mtype, ratio, rid = best
            # 模糊命中一律不自动应用, 仅建议
            if mtype == "fuzzy":
                auto = 0
            # 置信度不足自动应用阈值 -> 降级仅建议
            if auto and conf < AUTO_CONF_THRESHOLD:
                auto = 0
            if auto:
                auto_cnt += 1
            elif mtype == "fuzzy":
                fuzzy_cnt += 1
            else:
                sugg_cnt += 1
            std = get_standard_fields()
            suggestions[raw] = {
                "source_field": src,
                "target_field": tgt,
                "target_label": std.get(tgt, ""),
                "confidence": conf,
                "confidence_pct": round(conf * 100, 1),
                "auto_apply": bool(auto),
                "scope": scope,
                "match_type": mtype,
                "ratio": ratio,
                "rule_id": rid,
                "source": "flywheel_auto" if auto else "flywheel_suggest",
            }
    return {
        "suggestions": suggestions,
        "stats": {"total": len(src_list), "auto": auto_cnt, "suggest": sugg_cnt,
                  "fuzzy": fuzzy_cnt, "missed": miss_cnt},
    }


# ============================================================
# 修正回灌 (correct)
# ============================================================

def record_correction(source_field: str, wrong_target: str, correct_target: str,
                      series_id: str = "", user_id: int = None) -> Dict[str, Any]:
    """人工把系统建议/自动映射的 (source->wrong_target) 改为 (source->correct_target)。

    动作:
      1. 惩罚错误规则: correction_count+1, 重算 conf(降0.2), conf<0.5 -> 降级(auto_apply=0)
      2. 学习正确映射 (等价一次 learn, 强化正确规则)
      3. 审计 correct (及 demote)
    """
    std = get_standard_fields()
    src = _norm(source_field)
    wrong_target = (wrong_target or "").strip()
    correct_target = (correct_target or "").strip()
    series_id = (series_id or "").strip()
    result = {"penalized": False, "demoted": False, "reinforced": False}
    if not src:
        return result

    with _get_conn() as conn:
        # 1) 惩罚错误规则
        if wrong_target and wrong_target != correct_target:
            row = conn.execute(
                "SELECT * FROM field_mapping_rules WHERE source_field=? AND target_field=?",
                (src, wrong_target)
            ).fetchone()
            if row:
                occ = int(row["occurrence_count"])
                corr = int(row["correction_count"]) + 1
                conf = _compute_confidence(occ, corr)
                auto = _decide_auto_apply(occ, corr, conf)
                demoted = (row["auto_apply"] == 1 and auto == 0) or conf < DEMOTE_CONF_THRESHOLD
                conn.execute("""
                    UPDATE field_mapping_rules SET
                        correction_count=?, confidence=?, auto_apply=?,
                        last_seen=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (corr, conf, auto, row["id"]))
                _audit(conn, "correct", source_field=src, target_field=correct_target,
                       old_target=wrong_target, scope=row["scope"], series_id=series_id,
                       confidence=conf, occurrence_count=occ, correction_count=corr,
                       user_id=user_id)
                result["penalized"] = True
                if demoted:
                    result["demoted"] = True
                    _audit(conn, "demote", source_field=src, target_field=wrong_target,
                           scope=row["scope"], series_id=series_id, confidence=conf,
                           occurrence_count=occ, correction_count=corr,
                           detail={"reason": "conf<0.5 或 撤销自动应用"}, user_id=user_id)
        conn.commit()

    # 2) 学习正确映射 (合法才学)
    if correct_target and correct_target in std and not (
            correct_target.startswith("__") and correct_target.endswith("__")):
        learn_mappings([{"source_field": source_field, "target_field": correct_target}],
                       series_id=series_id, user_id=user_id)
        result["reinforced"] = True
    return result


# ============================================================
# 规则查询
# ============================================================

def list_rules(scope: str = None, series_id: str = None,
               min_confidence: float = 0.0, only_auto: bool = False,
               limit: int = 500) -> List[Dict[str, Any]]:
    """查询映射规则 (管理端/调试可视)。"""
    sql = "SELECT * FROM field_mapping_rules WHERE confidence >= ?"
    params: List[Any] = [min_confidence]
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
    if only_auto:
        sql += " AND auto_apply = 1"
    sql += " ORDER BY auto_apply DESC, confidence DESC, occurrence_count DESC LIMIT ?"
    params.append(limit)
    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    # 系列过滤 (project 需系列匹配; enterprise 全局)
    out = []
    for r in rows:
        r["series_scope"] = _series_list(r.get("series_scope"))
        if series_id and r["scope"] != "enterprise":
            if r["series_scope"] and series_id not in r["series_scope"]:
                continue
        out.append(r)
    return out


def get_stats() -> Dict[str, Any]:
    """飞轮规则库统计。"""
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM field_mapping_rules").fetchone()["c"]
        auto = conn.execute("SELECT COUNT(*) c FROM field_mapping_rules WHERE auto_apply=1").fetchone()["c"]
        ent = conn.execute("SELECT COUNT(*) c FROM field_mapping_rules WHERE scope='enterprise'").fetchone()["c"]
        corr = conn.execute("SELECT COUNT(*) c FROM field_mapping_rules WHERE correction_count>0").fetchone()["c"]
        audit_n = conn.execute("SELECT COUNT(*) c FROM field_mapping_audit").fetchone()["c"]
    return {"rules": total, "auto_apply": auto, "enterprise": ent,
            "corrected": corr, "audit_entries": audit_n}


def delete_rule(rule_id: int, user_id: int = None) -> bool:
    """删除单条规则 (可回滚支撑: 误学习后清理)。"""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM field_mapping_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM field_mapping_rules WHERE id=?", (rule_id,))
        _audit(conn, "rollback", source_field=row["source_field"],
               target_field=row["target_field"], scope=row["scope"],
               detail={"deleted_rule_id": rule_id}, user_id=user_id)
        conn.commit()
    return True


# ============================================================
# 自测 (verification-before-completion)
# ============================================================

if __name__ == "__main__":
    import tempfile
    # 用临时库自测, 不污染生产 designs.db
    tmp = os.path.join(tempfile.gettempdir(), "fmf_selftest.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    DB_PATH = tmp  # noqa: F811 (覆盖模块级常量供自测)
    os.environ["DB_PATH"] = tmp
    print("=== field_mapping_flywheel 自测 ===")
    init_field_mapping_tables()

    print("\n[1] 标准字段库校验")
    std = get_standard_fields()
    print(f"  标准字段数: {len(std)}; motor_weight in std? {'motor_weight' in std}")
    assert "motor_weight" in std and "motor_type" in std

    print("\n[2] 学习: 重量->motor_weight (系列A, 反复5次, 应达 auto_apply)")
    for i in range(5):
        r = learn_mappings({"重量": "motor_weight"}, series_id="Y2")
    print("  learn:", r)
    rules = list_rules()
    wr = [x for x in rules if x["target_field"] == "motor_weight"][0]
    print(f"  occ={wr['occurrence_count']} conf={wr['confidence']} auto={wr['auto_apply']} scope={wr['scope']}")
    assert wr["occurrence_count"] == 5 and wr["auto_apply"] == 1

    print("\n[3] 三层升级: 同映射出现在第二个系列 -> enterprise")
    learn_mappings({"重量": "motor_weight"}, series_id="YE3")
    wr = [x for x in list_rules() if x["target_field"] == "motor_weight"][0]
    print(f"  scope={wr['scope']} series_scope={wr['series_scope']}")
    assert wr["scope"] == "enterprise"

    print("\n[4] 建议: 精确 + 模糊(源名变体)")
    s = suggest_mappings(["重量", "重 量", "额定功率kW"], series_id="Y2")
    for k, v in s["suggestions"].items():
        print(f"  {k!r} -> {v['target_field']} conf={v['confidence']} "
              f"type={v['match_type']} auto={v['auto_apply']}")
    print("  stats:", s["stats"])
    assert s["suggestions"]["重量"]["auto_apply"] is True

    print("\n[5] 非法目标拒收")
    r = learn_mappings({"乱码列": "not_a_field"}, series_id="Y2")
    print("  ", r)
    assert r["rejected"] == 1 and r["learned"] == 0

    print("\n[6] 修正回灌: 建议 A 被改为 B, A 降权")
    for i in range(5):
        learn_mappings({"扭矩": "max_torque"}, series_id="Y2")
    before = [x for x in list_rules() if x["target_field"] == "max_torque"][0]
    cr = record_correction("扭矩", "max_torque", "starting_torque", series_id="Y2")
    after = [x for x in list_rules() if x["target_field"] == "max_torque"][0]
    print(f"  correct: {cr}")
    print(f"  max_torque conf {before['confidence']} -> {after['confidence']} "
          f"auto {before['auto_apply']} -> {after['auto_apply']}")
    assert after["confidence"] < before["confidence"]

    print("\n[7] 统计 & 审计")
    print("  stats:", get_stats())
    print("  audit(最近5):")
    for a in list_audit(limit=5):
        print(f"    #{a['id']} {a['action']} {a['source_field']}->{a['target_field']} conf={a['confidence']}")

    print("\n=== 全部自测通过 ===")
