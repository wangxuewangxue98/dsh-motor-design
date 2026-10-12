# -*- coding: utf-8 -*-
"""
V2.1 (#693) 种子智能提取  —  Seed Extractor
============================================
从两个**内部数据源**抽取"含完整几何"的种子冲片, 作为主线 A (main_a,
冲片几何全固定) 的基座。V2.1 设计书 [P48]/[P149-150]/[P157] 定义:

    型谱库 2 个标准冲片  +  案例库 3 个历史冲片  =  5 组种子 (含完整几何)

数据源与几何补全:
  1) 型谱库 Knowledge/y_series_physics.json (65 条标准冲片): 取 2 个
     - 含 stator_od / stator_id / core_length / air_gap / slots / yoke / poles
     - 缺 rotor_od / shaft_dia / tooth_width  -> 物理推断补全
  2) 案例库 case_motors (Core/case_motor_db): 取 3 个历史冲片
     - 直读完整几何 (OCR/录入的 90 字段)
     - 几何部分缺失时按同法推断补全

健壮性 (对齐"纯物理降级兜底"哲学):
  - 任一源不足时, 用另一源回填至 total(默认 5)
  - 案例库当前几乎为空时, 自动以型谱库补齐, 保证漏斗始终拿到 5 组含完整几何种子
  - 对任意种子, 11 个 canonical 几何键必须全部非 None (否则视为抽取失败)

canonical 几何键 (对齐 verify_engine.main_a FIXED_KEYS + 自由参数):
  stator_od, stator_id, rotor_od, shaft_dia, core_length, air_gap,
  tooth_width, yoke_thickness, slots_stator, slots_rotor, poles
"""
import os
import json
import math
import time
import copy
import hashlib
import threading

# ── 路径 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.path.join(_HERE, "..", "Knowledge", "y_series_physics.json")

# ── v4.02.36: 种子抽取缓存 (Stage1 修复 — 同 specs 一次性抽取, 后续调用复用, 消除多次独立抽取的不一致) ──
_SEED_CACHE = {}                  # {key: (result_dict, timestamp)}
_SEED_CACHE_TTL = 3600.0         # 缓存存活 1h
_SEED_CACHE_LOCK = threading.Lock()
_SEED_CACHE_HITS = 0
_SEED_CACHE_MISSES = 0

# ── canonical 几何键 (种子必须完整) ──
SEED_GEOM_KEYS = [
    "stator_od", "stator_id", "rotor_od", "shaft_dia", "core_length",
    "air_gap", "tooth_width", "yoke_thickness", "slots_stator", "slots_rotor", "poles",
]

# ── 物理推断常量 ──
_SHAFT_TAU_PA = 25.0e6      # 许用切应力 (Pa), 用于转矩 -> 轴径
_SHAFT_MIN_MM = 10.0        # 轴径下限
_TOOTH_RATIO = 0.50         # 齿宽 / 槽距(定子内径处) 比例


# ════════════════════════════════════════════════════════════════════
# 几何缺项推断
# ════════════════════════════════════════════════════════════════════
def infer_rotor_od(stator_id, air_gap):
    """转子外径 = 定子内径 - 2*气隙 (径向套嵌)。两者缺一返回 None。"""
    if stator_id is None or air_gap is None:
        return None
    return round(float(stator_id) - 2.0 * float(air_gap), 3)


def infer_shaft_dia(rotor_od, torque_nm=None):
    """轴径推断: 优先按转矩 (d=(16T/πτ)^(1/3)), 否则按转子外径比例; 带下限与上限钳制。"""
    if rotor_od is None:
        return None
    d = None
    if torque_nm and float(torque_nm) > 0:
        t = float(torque_nm)
        d = (16.0 * t / (math.pi * _SHAFT_TAU_PA)) ** (1.0 / 3.0) * 1000.0  # m -> mm
    if d is None or d <= 0:
        d = 0.30 * float(rotor_od)
    d = max(d, _SHAFT_MIN_MM, 0.15 * float(rotor_od))
    d = min(d, 0.60 * float(rotor_od))   # 轴径不得超转子外径 60%, 保证套嵌
    return round(d, 3)


def infer_tooth_width(stator_id, slots_stator):
    """齿宽推断: 定子内径处槽距 * 齿比。两者缺一返回 None。"""
    if stator_id is None or not slots_stator:
        return None
    slot_pitch = math.pi * float(stator_id) / float(slots_stator)
    return round(slot_pitch * _TOOTH_RATIO, 3)


def _calc_rated_torque(power_kw, speed_rpm):
    """由额定功率(kW)/转速(rpm)推算额定转矩(Nm): T = 9550*P/n。任一缺失或非正返回 None。"""
    try:
        p = float(power_kw)
        n = float(speed_rpm)
        if p > 0 and n > 0:
            return round(9550.0 * p / n, 3)
    except (TypeError, ValueError):
        pass
    return None


def _torque_ok(seed_t, target_t, tol=0.20):
    """扭矩门控: 种子额定转矩是否在目标转矩 ±tol 容差内。

    返回 True 表示放行(可用); False 表示超出容差(应剔除)。
    目标转矩或种子转矩不可算时放行(门控失效, 不误杀)。
    """
    if target_t is None or seed_t is None:
        return True
    try:
        target_t, seed_t = float(target_t), float(seed_t)
        if target_t <= 0:
            return True
        return abs(seed_t - target_t) / target_t <= tol
    except (TypeError, ValueError):
        return True


def geom_complete(seed):
    """种子 11 个 canonical 几何键是否全部非 None。"""
    return all(seed.get(k) is not None for k in SEED_GEOM_KEYS)


def _fill_inferred(seed, torque_nm=None):
    """对种子 dict 补全缺项几何, 返回被推断键的列表。"""
    inferred = []
    sid = seed.get("stator_id")
    ag = seed.get("air_gap")
    rod = seed.get("rotor_od")
    if rod is None:
        rod = infer_rotor_od(sid, ag)
        if rod is not None:
            seed["rotor_od"] = rod
            inferred.append("rotor_od")
    if seed.get("shaft_dia") is None:
        sd = infer_shaft_dia(rod, torque_nm)
        if sd is not None:
            seed["shaft_dia"] = sd
            inferred.append("shaft_dia")
    if seed.get("tooth_width") is None:
        tw = infer_tooth_width(sid, seed.get("slots_stator"))
        if tw is not None:
            seed["tooth_width"] = tw
            inferred.append("tooth_width")
    return inferred


# ════════════════════════════════════════════════════════════════════
# 型谱库种子
# ════════════════════════════════════════════════════════════════════
def _load_catalog():
    if not os.path.exists(_CATALOG_PATH):
        return []
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("catalog", [])


def _catalog_to_seed(row, idx, torque_nm=None):
    seed = {
        "stator_od": row.get("stator_od_mm"),
        "stator_id": row.get("stator_inner_dia_mm"),
        "core_length": row.get("core_length_mm"),
        "air_gap": row.get("air_gap_mm"),
        "yoke_thickness": row.get("yoke_thickness_mm"),
        "slots_stator": row.get("slots_stator"),
        "slots_rotor": row.get("slots_rotor"),
        "poles": row.get("poles"),
        "rotor_od": None,
        "shaft_dia": None,
        "tooth_width": None,
    }
    inferred = _fill_inferred(seed, torque_nm)
    # v4.02.13: 携带 torque(额定转矩) 与 槽形(stator_slot_type/rotor_slot_type) 供种子展示与固定项校验
    seed["torque"] = _calc_rated_torque(row.get("power_kw"), row.get("speed_rpm")) or row.get("torque_nm")
    seed["stator_slot_type"] = row.get("stator_slot_type")
    seed["rotor_slot_type"] = row.get("rotor_slot_type")
    seed.update({
        "seed_id": f"catalog-{idx}",
        "source": "catalog",
        "model": row.get("model"),
        "name": row.get("model"),
        "frame_size": row.get("frame_size"),
        "power_kw": row.get("power_kw"),
        "speed_rpm": row.get("speed_rpm"),
        "inferred": inferred,
    })
    return seed


def _score_catalog(row, specs):
    s = 0.0
    tp = specs.get("poles") or specs.get("pole_count")
    if tp is not None:
        rp = row.get("poles")
        try:
            tp_f, rp_f = float(tp), float(rp)
        except (TypeError, ValueError):
            tp_f = rp_f = None
        if tp_f is not None and rp_f is not None:
            if rp_f == tp_f:
                s += 100.0
            else:
                s -= abs(rp_f - tp_f) * 8.0
    pw = specs.get("power_kw") or specs.get("power")
    if pw is not None:
        rpk = row.get("power_kw")
        try:
            pw_f, rpk_f = float(pw), float(rpk)
        except (TypeError, ValueError):
            pw_f = rpk_f = None
        if pw_f is not None and rpk_f is not None:
            s -= abs(rpk_f - pw_f) * 0.4
    fs = specs.get("frame_size")
    if fs is not None:
        rfs = row.get("frame_size")
        try:
            fs_f, rfs_f = float(fs), float(rfs)
        except (TypeError, ValueError):
            fs_f = rfs_f = None
        if fs_f is not None and rfs_f is not None:
            s -= abs(rfs_f - fs_f) * 0.5
    return s


def extract_catalog_seeds(specs, n=2, exclude_models=None, motor_type=None,
                           target_t=None, torque_tol=0.30):
    """从型谱库按 specs 相似度取 n 个标准冲片种子 (含完整几何)。

    v4.02.22 (Fix1): 增加目标转矩门控 — 种子额定转矩超出目标 ±torque_tol
    时剔除(不替补); 目标/种子转矩不可算时门控失效放行(不误杀)。
    v4.02.45 (F2): 型谱门控默认 ±20% → ±30% — ±20% 会把 Y 系列相邻机座
    (如 15kW 目标下的 Y180M-4/Y160M-4) 全部剔除, 种子多样性塌缩。
    motor_type 过滤为 best-effort: 仅当型谱行自带 motor_type 字段且与
    specs 不一致才剔除(当前型谱库无该字段 → 该过滤自动为空操作, 不跨类型误杀)。
    """
    exclude_models = set(exclude_models or [])
    cat = _load_catalog()
    if not cat:
        return []
    scored = []
    for r in cat:
        if r.get("model") in exclude_models:
            continue
        # best-effort 跨类型过滤: 型谱库存在 motor_type 字段且与需求不一致则跳过
        if motor_type and r.get("motor_type") is not None and r.get("motor_type") != motor_type:
            continue
        scored.append((_score_catalog(r, specs or {}), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    seeds = []
    for _, r in scored:
        if len(seeds) >= n:
            break
        torque = r.get("torque_nm")
        s = _catalog_to_seed(r, len(seeds) + 1, torque_nm=torque)
        # v4.02.22 (Fix1): 扭矩门控 — 超出目标容差者不入选(不替补)
        if not _torque_ok(s.get("torque"), target_t, torque_tol):
            continue
        # 仅保留推断后几何完整的种子 (缺基键无法补全者丢弃)
        if geom_complete(s):
            seeds.append(s)
    return seeds


# ════════════════════════════════════════════════════════════════════
# 案例库种子
# ════════════════════════════════════════════════════════════════════
def _case_row_to_seed(row, idx, torque_nm=None):
    seed = {
        "stator_od": row.get("stator_od"),
        "stator_id": row.get("stator_id"),
        "rotor_od": row.get("rotor_od"),
        "shaft_dia": row.get("shaft_dia"),
        "core_length": row.get("core_length"),
        "air_gap": row.get("air_gap"),
        "tooth_width": row.get("tooth_width"),
        "yoke_thickness": row.get("yoke_thickness"),
        "slots_stator": row.get("slots_stator"),
        "slots_rotor": row.get("slots_rotor"),
        "poles": row.get("poles"),
    }
    inferred = _fill_inferred(seed, torque_nm)
    # v4.02.13: 携带 torque(额定转矩) 与 槽形(stator_slot_type/rotor_slot_type)
    seed["torque"] = (_calc_rated_torque(row.get("rated_power"), row.get("rated_speed"))
                      or row.get("rated_torque") or row.get("torque_nm"))
    seed["stator_slot_type"] = row.get("stator_slot_type")
    seed["rotor_slot_type"] = row.get("rotor_slot_type")
    seed.update({
        "seed_id": f"case-{idx}",
        "source": "case",
        "model": row.get("motor_model") or row.get("motor_name") or f"case{row.get('id')}",
        "name": row.get("motor_name") or row.get("motor_model") or f"案例{row.get('id')}",
        "frame_size": row.get("frame_size"),
        "power_kw": row.get("rated_power"),
        "speed_rpm": row.get("rated_speed"),
        "inferred": inferred,
    })
    return seed


def _geom_completeness(row):
    return sum(1 for k in SEED_GEOM_KEYS if row.get(k) not in (None, ""))


def extract_case_seeds(specs, motor_type=None, n=3, target_t=None, torque_tol=0.20):
    """从案例库取 n 个历史冲片种子 (几何完整优先)。案例库稀疏时返回少于 n 个。

    v4.02.22 (Fix1): 增加目标转矩门控 — 种子额定转矩超出目标 ±torque_tol
    时剔除(不替补); 目标/种子转矩不可算时门控失效放行(不误杀)。
    """
    try:
        from case_motor_db import list_case_motors
    except Exception:
        return []
    mt = motor_type or (specs or {}).get("motor_type")
    # v4.02.44 (B硬门禁): 仅取已验证成功案例(status=verified), 杜绝 draft/失败案例当种子
    try:
        if mt:
            rows = list_case_motors("verified", motor_type=mt, limit=200)
        else:
            rows = list_case_motors("verified", limit=200)
    except Exception:
        return []
    if not rows:
        return []
    # 几何完整度降序; 同完整度按功率接近 specs 优先
    pw = (specs or {}).get("power_kw") or (specs or {}).get("power")
    sp = (specs or {}).get("speed_rpm") or (specs or {}).get("speed")
    try:
        pw = float(pw) if pw is not None else None
    except (TypeError, ValueError):
        pw = None
    try:
        sp = float(sp) if sp is not None else None
    except (TypeError, ValueError):
        sp = None
    def _sort_key(r):
        comp = _geom_completeness(r)
        pw_diff = abs((r.get("rated_power") or 0) - (pw or 0)) if pw else 0
        return (comp, -pw_diff)
    rows.sort(key=_sort_key, reverse=True)
    seeds = []
    for r in rows:
        if len(seeds) >= n:
            break
        torque = r.get("rated_torque") or r.get("torque_nm")
        s = _case_row_to_seed(r, len(seeds) + 1, torque_nm=torque)
        # v4.02.22 (Fix1): 转矩门控 — 超出目标容差者不入选(不替补)
        if not _torque_ok(s.get("torque"), target_t, torque_tol):
            continue
        # v4.02.44 (B硬门禁): 功率 + 转速 双门控 (±30% 容差) — 三者均近邻方可入选
        _rp = r.get("rated_power"); _rs = r.get("rated_speed")
        if pw and _rp and abs(float(_rp) - pw) / pw > 0.30:
            continue
        if sp and _rs and abs(float(_rs) - sp) / sp > 0.30:
            continue
        if geom_complete(s):
            seeds.append(s)
    return seeds


# ════════════════════════════════════════════════════════════════════
# 总入口: 5 组种子 (2 型谱 + 3 案例, 不足回填)
# ════════════════════════════════════════════════════════════════════
def extract_seeds(specs=None, motor_type=None, total=5, catalog_n=2, case_n=3,
                  base_params=None, case_motor_ref=None, torque_tol: float = 0.20,
                  catalog_torque_tol: float = 0.30):
    """
    抽取 total 组含完整几何的种子。

    返回 dict:
      {
        "seeds":           [种子 dict, ...]   # 长度 = 通过门控的实际可用数 (可为 0)
        "catalog_count":   int,
        "case_count":      int,
        "backfilled":      int,               # v4.02.22 Fix1: 已移除回填, 恒为 0
        "incomplete":      int,               # 仍含 None 几何键的种子数 (应=0)
      }

    v4.02.10: 新增 base_params 和 case_motor_ref 参数
      - case_motor_ref: 案例电机参数，优先作为种子候选
      - base_params: 基准参数，用于校验种子与用户确认参数的一致性
    v4.02.22 (Fix1): 新增 torque_tol 门控 — 种子额定转矩须落在目标转矩 ±tol 内,
      不符合者剔除且**不替补**; 返回列表可能为空, 由上游 build_candidate_matrix 自动
      回退至 focused_scan 兜底(无种子分支)。
    """
    specs = specs or {}
    base_params = base_params or {}
    case_motor_ref = case_motor_ref or {}

    # v4.02.22 (Fix1): 由 specs 推算目标额定转矩作为门控基准
    target_t = _calc_rated_torque(
        specs.get("power_kw") or specs.get("power"),
        specs.get("speed_rpm") or specs.get("speed"))

    # v4.02.10: 案例电机优先作为种子 (用户显式选定的参考, 免门控)
    case_seeds = []
    if case_motor_ref:
        _case_seed = _make_seed_from_case_motor(case_motor_ref)
        if _case_seed and geom_complete(_case_seed):
            case_seeds.append(_case_seed)
            print(f"[seed_extractor] 案例电机优先种子: {_case_seed.get('model', '?')}")

    # 补充从 specs 提取的案例种子 (经扭矩门控)
    _additional_case_seeds = extract_case_seeds(
        specs, motor_type=motor_type, n=case_n - len(case_seeds),
        target_t=target_t, torque_tol=torque_tol)
    case_seeds.extend(_additional_case_seeds)

    case_models = {s.get("model") for s in case_seeds}
    # v4.02.45 (F2): 型谱门控独立参数 catalog_torque_tol (默认 ±30%); 案例门控维持 torque_tol (±20%)
    cat_seeds = extract_catalog_seeds(
        specs, n=catalog_n, exclude_models=case_models,
        motor_type=motor_type, target_t=target_t, torque_tol=catalog_torque_tol)

    combined = list(case_seeds) + list(cat_seeds)

    # v4.02.22 (Fix1): 移除"全类型回填凑数" — 严格门控后数量可能 < total,
    # 宁缺毋滥, 放它走上游 focused_scan 兜底, 杜绝跨类型假种子污染下游。
    backfilled = 0

    seeds = combined[:total]
    # 统一重编号, 保证 seed_id 全局唯一 (案例在前、型谱在后)
    for i, s in enumerate(seeds):
        s["seed_id"] = f"{s['source']}-{i + 1}"

    # v4.02.10: 校验种子与基准参数的一致性
    if base_params and seeds:
        _consistency = _check_seed_consistency(seeds, base_params)
        if _consistency.get("mismatches"):
            print(f"[seed_extractor] 种子与基准参数不一致: {_consistency['mismatches']}")

    incomplete = sum(1 for s in seeds if any(s.get(k) is None for k in SEED_GEOM_KEYS))
    return {
        "seeds": seeds,
        "catalog_count": sum(1 for s in seeds if s["source"] == "catalog"),
        "case_count": sum(1 for s in seeds if s["source"] == "case"),
        "backfilled": backfilled,
        "incomplete": incomplete,
    }


# ════════════════════════════════════════════════════════════════════
# v4.02.36 Stage1: 种子抽取缓存 — 同 specs + 选种参数只真实抽取一次
# ════════════════════════════════════════════════════════════════════
def _normalize_for_hash(obj):
    """将 dict/list/scalar 稳定化为可排序 repr (用于缓存键, 忽略不影响选种的量)。"""
    try:
        if isinstance(obj, dict):
            return sorted((k, _normalize_for_hash(v)) for k, v in obj.items())
        if isinstance(obj, (list, tuple)):
            return tuple(_normalize_for_hash(v) for v in obj)
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        return repr(obj)
    except Exception:
        return repr(obj)


def _hash_specs(specs=None, motor_type=None, total=5, catalog_n=2, case_n=3,
                torque_tol=0.20, case_motor_ref=None, catalog_torque_tol=0.30):
    """选种口径的缓存键 — 仅纳入**影响选种**的输入。

    - specs / motor_type / total / catalog_n / case_n / torque_tol: 直接影响选种与门控
    - catalog_torque_tol (v4.02.45 F2): 型谱门控独立容差, 影响选种, 须入键
    - case_motor_ref: 作为优先种子参与选种(非空时前置), 须纳入键
    - base_params: 仅用于选种后一致性校验, **不**影响选种结果, 故不入键
    """
    payload = {
        "specs": _normalize_for_hash(specs or {}),
        "motor_type": motor_type,
        "total": total,
        "catalog_n": catalog_n,
        "case_n": case_n,
        "torque_tol": torque_tol,
        "catalog_torque_tol": catalog_torque_tol,
        "case_motor_ref": _normalize_for_hash(case_motor_ref or {}),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def extract_seeds_cached(specs=None, motor_type=None, total=5, catalog_n=2, case_n=3,
                         base_params=None, case_motor_ref=None, torque_tol=0.20,
                         catalog_torque_tol=0.30):
    """v4.02.36 Stage1 — 带 TTL 缓存的种子抽取封装。

    同缓存键(specs + 选种参数)在一次流程内只真实抽取一次, 后续调用直接返回
    深拷贝的缓存副本, 保证同一运行态下 5 颗种子**完全一致**(消除 orchestrator
    三个抽取点各自独立抽取导致的不一致 / 数量漂移)。

    命中缓存时仍按 base_params 跑一致性校验(校验不入账键, 仅告警, 不阻断选种)。
    返回结构与 extract_seeds 完全一致。
    """
    global _SEED_CACHE_HITS, _SEED_CACHE_MISSES
    key = _hash_specs(specs, motor_type=motor_type, total=total, catalog_n=catalog_n,
                      case_n=case_n, torque_tol=torque_tol, case_motor_ref=case_motor_ref,
                      catalog_torque_tol=catalog_torque_tol)
    now = time.time()
    with _SEED_CACHE_LOCK:
        hit = _SEED_CACHE.get(key)
        if hit is not None:
            cached_result, cached_ts = hit
            if now - cached_ts < _SEED_CACHE_TTL:
                _SEED_CACHE_HITS += 1
                result = copy.deepcopy(cached_result)
                # 命中仍跑一致性校验(不入账键, 仅告警)
                if base_params and result.get("seeds"):
                    _consistency = _check_seed_consistency(result["seeds"], base_params)
                    if _consistency.get("mismatches"):
                        print(f"[seed_extractor] (cache-hit) 种子与基准参数不一致: {_consistency['mismatches']}")
                return result
        _SEED_CACHE_MISSES += 1
    # 缓存未命中: 真实抽取(仅选种相关参数参与); 入库存深拷贝, 返回原件
    result = extract_seeds(specs, motor_type=motor_type, total=total, catalog_n=catalog_n,
                           case_n=case_n, base_params=base_params,
                           case_motor_ref=case_motor_ref, torque_tol=torque_tol,
                           catalog_torque_tol=catalog_torque_tol)
    with _SEED_CACHE_LOCK:
        _SEED_CACHE[key] = (copy.deepcopy(result), now)
    return result


def seed_cache_stats():
    """返回种子缓存统计 (命中/未命中/条目数/存活条目)。"""
    with _SEED_CACHE_LOCK:
        now = time.time()
        alive = sum(1 for _, ts in _SEED_CACHE.values() if now - ts < _SEED_CACHE_TTL)
        entries = [{"key": k[:8], "age_s": round(now - ts, 1)} for k, (_, ts) in _SEED_CACHE.items()]
    return {
        "hits": _SEED_CACHE_HITS,
        "misses": _SEED_CACHE_MISSES,
        "total_calls": _SEED_CACHE_HITS + _SEED_CACHE_MISSES,
        "cached_entries": len(_SEED_CACHE),
        "alive_entries": alive,
        "ttl_s": _SEED_CACHE_TTL,
        "entries": entries,
    }


def seed_cache_clear():
    """清空种子缓存 (调试/热更新用)。"""
    with _SEED_CACHE_LOCK:
        n = len(_SEED_CACHE)
        _SEED_CACHE.clear()
    return {"cleared": n}


# ════════════════════════════════════════════════════════════════════
# v4.02.36 Stage2: 种子层物理合理性核查 (几何/电气), 复用 physics_kernel 确定性纯函数
# ════════════════════════════════════════════════════════════════════
def evaluate_seed_reasonableness(seed, specs=None, block_errors=False):
    """种子层物理合理性核查 (几何/电气), 与 physics_l0._score_candidate 同源但用于种子层。

    核查项 (仅诊断, 默认不拦截):
      1) 11 几何键完整性
      2) 径向套嵌 shaft_dia < rotor_od < stator_id < stator_od
      3) 长径比 λ∈极数合理区间 (validate_lambda)
      4) 气隙电磁比 air_gap/stator_id ∈ [0.001, 0.006]
      5) 每槽匝数 50Hz 等效 z_eq ∈ [0.5, 22] (calc_turns_per_slot, parallel_branches=2)
      6) 极数-转速可行性 (异步 _sync_speed_feasible / 同步速≈额定)
    返回 dict: {issues:[{code,level,msg}], warn_count, error_count, ok}
      - ok 默认 True (仅诊断); block_errors=True 时 error_count>0 则 ok=False
    """
    specs = specs or {}
    issues = []
    try:
        from physics_kernel import (validate_lambda, calc_turns_per_slot, _sync_speed_feasible)
    except Exception:
        validate_lambda = calc_turns_per_slot = _sync_speed_feasible = None

    def _f(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    # v4.02.44 (A硬门禁): H2 功率匹配 — 种子额定功率须落在目标功率 ±20% 内
    try:
        _tgt_p = _f(specs.get("power_kw") or specs.get("power"))
        _seed_p = _f(seed.get("power_kw") or seed.get("rated_power"))
        if _tgt_p > 0 and _seed_p > 0:
            if abs(_seed_p - _tgt_p) / _tgt_p > 0.20:
                issues.append({"code": "power_mismatch", "level": "error",
                               "msg": f"种子功率{_seed_p:.0f}kW 偏离目标{_tgt_p:.0f}kW 超过20%"})
    except (TypeError, ValueError):
        pass
    # v4.02.44 (A硬门禁): H1 转矩匹配 — 种子额定转矩须落在目标转矩 ±20% 内
    try:
        _tgt_t = _calc_rated_torque(specs.get("power_kw") or specs.get("power"),
                                    specs.get("speed_rpm") or specs.get("speed"))
        _seed_t = _f(seed.get("torque") or seed.get("rated_torque"))
        if _tgt_t and _tgt_t > 0 and _seed_t > 0:
            if abs(_seed_t - _tgt_t) / _tgt_t > 0.20:
                issues.append({"code": "torque_mismatch", "level": "error",
                               "msg": f"种子转矩{_seed_t:.0f}Nm 偏离目标{_tgt_t:.0f}Nm 超过20%"})
    except (TypeError, ValueError):
        pass

    sid = seed.get("stator_id"); rod = seed.get("rotor_od"); lod = seed.get("stator_od")
    sda = seed.get("shaft_dia"); cl = seed.get("core_length"); ag = seed.get("air_gap")
    poles = seed.get("poles"); sss = seed.get("slots_stator")

    # 1) 几何完整性
    if not geom_complete(seed):
        miss = [k for k in SEED_GEOM_KEYS if seed.get(k) is None]
        issues.append({"code": "geometry_incomplete", "level": "error",
                       "msg": f"几何键缺失: {miss}"})

    # 2) 径向套嵌
    try:
        if all(x is not None for x in (sda, rod, sid, lod)):
            if not (_f(sda) <= _f(rod) <= _f(sid) <= _f(lod)):
                issues.append({"code": "radial_nesting", "level": "error",
                               "msg": f"径向套嵌违例: shaft={sda} rod={rod} sid={sid} sod={lod}"})
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # 3) 长径比
    try:
        if validate_lambda is not None and cl and sid and poles:
            lv = validate_lambda(_f(cl), _f(sid), int(poles))
            if not lv.get("valid"):
                lo, hi = lv.get("range", (0.5, 1.5))
                issues.append({"code": "lambda_out_of_range", "level": "error",
                               "msg": f"长径比={lv.get('lambda')} 超出极数区间{lo, hi}"})
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # 4) 气隙电磁比
    try:
        if ag and sid and _f(sid) > 0:
            r = _f(ag) / _f(sid)
            if not (0.001 <= r <= 0.006):
                issues.append({"code": "air_gap_ratio", "level": "error",
                               "msg": f"气隙电磁比={r:.5f} 偏离[0.001,0.006]"})
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # 5) 每槽匝数 (50Hz 等效) — v4.02.45 (F1): 绝对带 [0.5,22] 为大型机(450kW)标定,
    #    对小型机误杀 (Y160L-4 15kW/4极 EMF 反推 z_eq=26 物理正确却被判 error)。
    #    改为与"目标功率归一 EMF 反推参考值"的偏差门控: 铁长按 P_t/P_s 缩放时 z ∝ 1/cl,
    #    即 z_ref = z_eq × (P_s/P_t); 偏差 ≤±20% 视为合理。参考不可算时仅保留退化下限守卫。
    try:
        if calc_turns_per_slot is not None and sid and cl and poles and sss:
            n_eff = _f(specs.get("speed_rpm") or specs.get("speed"))
            p_eff = _f(poles)
            f_eff = _f(specs.get("frequency") or specs.get("frequency_hz") or 50)
            if p_eff > 0 and n_eff > 0:
                f_syn = n_eff * p_eff / 120.0
                if f_eff <= 0 or (f_syn > 0 and abs(f_eff - f_syn) / f_syn > 0.5):
                    f_eff = f_syn
            v = _f(specs.get("voltage_v") or specs.get("voltage") or 380)
            z = calc_turns_per_slot(v, f_eff or 50.0, _f(sid), _f(cl), int(poles), int(sss), 2)
            if z is not None:
                z_eq = float(z) * (f_eff or 50.0) / 50.0
                # 退化下限守卫 (与旧带下限一致, 尺寸无关): 超大机 z→0 视为几何退化
                if z_eq < 0.5:
                    issues.append({"code": "turns_per_slot", "level": "error",
                                   "msg": f"50Hz等效每槽匝数 z_eq={z_eq:.1f} 低于退化下限0.5"})
                else:
                    _p_t = _f(specs.get("power_kw") or specs.get("power"))
                    _p_s = _f(seed.get("power_kw") or seed.get("rated_power"))
                    if _p_t > 0 and _p_s > 0:
                        _z_ref = z_eq * (_p_s / _p_t)
                        _dev = abs(z_eq - _z_ref) / _z_ref if _z_ref > 0 else 0.0
                        if _dev > 0.20:
                            issues.append({"code": "turns_per_slot", "level": "error",
                                           "msg": f"EMF反推匝数z_eq={z_eq:.1f} 偏离目标功率归一参考"
                                                  f"{_z_ref:.1f} 达{_dev * 100:.0f}% 超过±20% "
                                                  f"(种子{_p_s:.0f}kW vs 目标{_p_t:.0f}kW)"})
                    # 功率信息缺失 → 放行 (门控失效不误杀, 与 _torque_ok 同哲学)
    except Exception:
        pass

    # 6) 极数-转速可行性
    try:
        if poles and _f(poles) > 0:
            mt = str(specs.get("motor_type") or specs.get("type") or "").lower()
            is_ind = any(k in mt for k in ("异步", "induction", "im", "鼠笼", "绕线"))
            is_sync = any(k in mt for k in ("同步", "pmsm", "bldc", "永磁", "sync", "无刷"))
            # v4.02.46 (C, RUN54/54b/54c 取证): 额定转速 n 优先用种子自带值(型谱/案例数据确定),
            # 退化需求解析值。云模型 LLM 偶把 specs.speed_rpm 解析成同步转速(1500)而非额定(1460),
            # 若直接用它算转差会得到 0% 而误杀合格异步种子(如 Y160L-4)。种子自带 speed_rpm 才是真额定值。
            n = _f(seed.get("speed_rpm") or seed.get("rated_speed") or seed.get("speed")
                   or specs.get("speed_rpm") or specs.get("speed"))
            f_eff = _f(specs.get("frequency") or specs.get("frequency_hz") or 0)
            p_eff = _f(poles)
            # 频率缺失时优先由需求额定转速回推(而非种子), 保持与需求一致, 避免被种子污染
            if f_eff <= 0:
                _n_req = _f(specs.get("speed_rpm") or specs.get("speed"))
                if _n_req > 0 and p_eff > 0:
                    f_eff = _n_req * p_eff / 120.0
            if f_eff > 0 and n > 0:
                if is_ind:
                    if _sync_speed_feasible(int(p_eff), f_eff, n) is False:
                        issues.append({"code": "sync_speed", "level": "error",
                                       "msg": "异步转差不在[0.5%,10%], 易解为发电机模式"})
                elif is_sync:
                    n_sync = 120.0 * f_eff / p_eff
                    if abs(n_sync - n) / n > 0.03:
                        issues.append({"code": "sync_speed", "level": "error",
                                       "msg": f"同步速{n_sync:.0f}≠额定{n:.0f}"})
    except Exception:
        pass

    warn_c = sum(1 for i in issues if i["level"] == "warn")
    err_c = sum(1 for i in issues if i["level"] == "error")
    ok = (err_c == 0) if block_errors else True
    return {"issues": issues, "warn_count": warn_c, "error_count": err_c, "ok": ok}


# ════════════════════════════════════════════════════════════════════
# v4.02.36 Stage3: L1(RMxprt) 种子功率校验 — 功率偏差 ≤±5% 视为通过
# ════════════════════════════════════════════════════════════════════
def _seed_to_rmxprt_params(seed, specs, voltage=380):
    """种子 + specs → RMxprt 求解参数。power 必须以 kW 注入 (modify_aedt_text 按 kW→W)。"""
    pk = specs.get("power_kw") or specs.get("power")
    f = specs.get("frequency_hz") or specs.get("frequency") or 50
    n = specs.get("speed_rpm") or specs.get("speed")
    if pk is None or n is None:
        return None
    params = {
        "stator_od": seed.get("stator_od"),
        "stator_id": seed.get("stator_id"),
        "rotor_od": seed.get("rotor_od"),
        "shaft_dia": seed.get("shaft_dia"),
        "core_length": seed.get("core_length"),
        "air_gap": seed.get("air_gap"),
        "slots_stator": seed.get("slots_stator"),
        "slots_rotor": seed.get("slots_rotor"),
        "poles": seed.get("poles"),
        "speed": n,
        "power": pk,            # kW
        "frequency": f,
        "voltage": voltage,
    }
    # 几何/槽配合不全 → 无法求解
    if any(params.get(k) is None for k in
           ("stator_od", "stator_id", "core_length", "poles", "slots_stator")):
        return None
    # 绕组匝数按目标电压/磁密自适应 (平行支路=2), 使 RMxprt 出力贴近目标
    try:
        from physics_kernel import calc_turns_per_slot
        z = calc_turns_per_slot(voltage, float(f) or 50.0, float(params["stator_id"]),
                                float(params["core_length"]), int(params["poles"]),
                                int(params["slots_stator"]), 2)
        if z is not None:
            params["turns_per_coil"] = z
            params["parallel_circuits"] = 2
    except Exception:
        pass
    return params


def verify_seeds_with_rmxprt(seeds, specs, machine_type=None, power_tol=0.05,
                             voltage=380, enable=False, force=False):
    """v4.02.36 Stage3 — L1(RMxprt) 种子功率校验, 功率偏差≤±power_tol 视为通过。

    fail-closed:
      - enable=False 且 force=False → 跳过(视为验证通过, verified=None)
      - RMxprt 不可用 (_RMXPRT_AVAILABLE=False) → 跳过, verified=None, reason=rmxprt_unavailable
      - 求解异常/无有效功率 → 该种子 verified=False, reason=solve_error(不阻断其余)
    返回 (seeds, stats): 原地标注 seeds[i]["verified"/"verify_power_kw"/"verify_power_dev"/"reject_reason"],
      stats 含 enable/available/called/verified_count/rejected_count/error_count/skipped。
    """
    stats = {"enable": enable, "force": force, "available": None, "called": False,
             "verified_count": 0, "rejected_count": 0, "error_count": 0, "skipped_count": 0,
             "details": []}
    if not seeds:
        return seeds, stats

    # 可用性判定
    _rmxprt_ok = False
    try:
        from motor_tools import _RMXPRT_AVAILABLE
        stats["available"] = bool(_RMXPRT_AVAILABLE)
        _rmxprt_ok = bool(_RMXPRT_AVAILABLE)
    except Exception:
        stats["available"] = False
        _rmxprt_ok = False

    target_p = None
    try:
        target_p = float(specs.get("power_kw") or specs.get("power") or 0)
    except (TypeError, ValueError):
        target_p = None

    if not (enable or force) or not _rmxprt_ok or not target_p or target_p <= 0:
        reason = ("disabled" if not (enable or force) else
                  "rmxprt_unavailable" if not _rmxprt_ok else "no_target_power")
        for s in seeds:
            s["verified"] = None
            s["verify_reason"] = reason
            stats["skipped_count"] += 1
        stats["verify_reason_summary"] = reason
        return seeds, stats

    # 构造求解参数 (几何不全的种子直接判不合格)
    param_list = []
    idx_map = []
    for i, s in enumerate(seeds):
        p = _seed_to_rmxprt_params(s, specs, voltage=voltage)
        if p is None:
            s["verified"] = False
            s["reject_reason"] = "insufficient_geometry"
            stats["rejected_count"] += 1
            stats["details"].append({"seed_id": s.get("seed_id"), "verdict": "rejected",
                                     "reason": "insufficient_geometry"})
            continue
        param_list.append(p)
        idx_map.append(i)

    if not param_list:
        return seeds, stats

    # ★ 关键: 直接 batch_solve(rmxprt), 绝不调用 retry_failed_solutions (避免 v4.02.34 冻结复发)
    stats["called"] = True
    try:
        from motor_tools import batch_solve
        results = batch_solve(param_list, verbose=False, solver_engine="rmxprt",
                              machine_type=machine_type)
    except Exception as e:
        stats["error_count"] = len(param_list)
        for i in idx_map:
            seeds[i]["verified"] = False
            seeds[i]["reject_reason"] = f"solve_error:{e}"
            stats["details"].append({"seed_id": seeds[i].get("seed_id"),
                                     "verdict": "error", "reason": str(e)})
        return seeds, stats

    # 比对功率 (RMxprt result["power"] 已是 kW)
    for r, i in zip(results, idx_map):
        s = seeds[i]
        calc_p = None
        try:
            calc_p = float(r.get("power") or r.get("output_power") or 0)
        except (TypeError, ValueError):
            calc_p = 0.0
        dev = abs(calc_p - target_p) / target_p if target_p else None
        s["verify_power_kw"] = round(calc_p, 3)
        s["verify_power_dev"] = round(dev, 4) if dev is not None else None
        if calc_p > 0 and dev is not None and dev <= power_tol:
            s["verified"] = True
            s["verify_reason"] = "power_within_tol"
            stats["verified_count"] += 1
            dverdict = "verified"
        else:
            s["verified"] = False
            s["reject_reason"] = (f"power_dev={dev:.3f}>tol" if calc_p > 0
                                  else "no_valid_power")
            stats["rejected_count"] += 1
            dverdict = "rejected"
        stats["details"].append({
            "seed_id": s.get("seed_id"), "verdict": dverdict,
            "calc_power_kw": round(calc_p, 3),
            "target_power_kw": round(target_p, 3),
            "dev": round(dev, 4) if dev is not None else None,
        })
    return seeds, stats


# ════════════════════════════════════════════════════════════════════
# v4.02.36 种子评价总入口 (Stage2 + Stage3) — orchestrator 调用
# ════════════════════════════════════════════════════════════════════
def evaluate_seeds(seeds, specs, machine_type=None, config=None):
    """种子评价总入口: Stage2 几何/电气合理性(非阻断) + Stage3 L1 RMxprt 功率校验(fail-closed)。

    config 支持:
      l1_rmxprt_enabled (bool, 默认 False) — 是否启用 L1 RMxprt 功率校验
      l1_rmxprt_force   (bool, 默认 False) — 强制启用(覆盖默认关)
      l1_power_tol      (float, 默认 0.05) — 功率偏差容差
      voltage           (float, 默认 380)  — 求解电压平台
    另: 环境变量 SEED_L1_RMXPRT=1 可强制启用 (便于 QA / 临时开启)。
    返回 dict(含 seeds 原地标注结果 + 汇总统计)。
    """
    import os as _os
    config = config or {}
    out = {"seed_count": len(seeds), "reason_checked": 0, "reason_warn": 0,
           "reason_error": 0, "reason_blocked": 0, "blocked_seeds": [],
           "l1_enabled": False, "l1_available": None,
           "l1_verified": 0, "l1_rejected": 0, "l1_error": 0, "l1_skipped": 0,
           "details": [], "seeds": seeds}
    _reason_block = bool(config.get("reason_block_errors", True))
    for s in seeds:
        r = evaluate_seed_reasonableness(s, specs=specs, block_errors=_reason_block)
        s["reason_issues"] = r["issues"]
        s["reason_warn_count"] = r["warn_count"]
        s["reason_error_count"] = r["error_count"]
        s["reason_blocked"] = (bool(_reason_block) and r["error_count"] > 0)
        out["reason_checked"] += 1
        out["reason_warn"] += r["warn_count"]
        out["reason_error"] += r["error_count"]
        if s["reason_blocked"]:
            out["reason_blocked"] += 1
            out["blocked_seeds"].append(s.get("seed_id"))
        out["details"].append({"seed_id": s.get("seed_id"), "source": s.get("source"),
                               "reason_warn": r["warn_count"], "reason_error": r["error_count"],
                               "reason_blocked": s["reason_blocked"]})

    l1_enable = bool(config.get("l1_rmxprt_enabled", False))
    if _os.environ.get("SEED_L1_RMXPRT") in ("1", "true", "True"):
        l1_enable = True
    force = bool(config.get("l1_rmxprt_force", False))
    _, stats = verify_seeds_with_rmxprt(
        seeds, specs, machine_type=machine_type,
        power_tol=float(config.get("l1_power_tol", 0.05)),
        voltage=float(config.get("voltage", 380)),
        enable=l1_enable, force=force)
    out["l1_enabled"] = l1_enable or force
    out["l1_available"] = stats.get("available")
    out["l1_verified"] = stats["verified_count"]
    out["l1_rejected"] = stats["rejected_count"]
    out["l1_error"] = stats["error_count"]
    out["l1_skipped"] = stats["skipped_count"]
    out["l1_stats"] = stats
    return out


def _make_seed_from_case_motor(case_motor: dict) -> dict:
    """从案例电机参数生成种子"""
    try:
        seed = {
            "source": "case",
            "model": case_motor.get("model", "case_motor"),
            "stator_od": case_motor.get("stator_od") or case_motor.get("stator_od_mm"),
            "stator_id": case_motor.get("stator_id") or case_motor.get("stator_inner_dia_mm"),
            "rotor_od": case_motor.get("rotor_od") or case_motor.get("rotor_od_mm"),
            "core_length": case_motor.get("core_length") or case_motor.get("core_length_mm"),
            "air_gap": case_motor.get("air_gap") or case_motor.get("air_gap_mm"),
            "tooth_width": case_motor.get("tooth_width") or case_motor.get("tooth_width_mm"),
            "yoke_thickness": case_motor.get("yoke_thickness") or case_motor.get("yoke_thickness_mm"),
            "shaft_dia": case_motor.get("shaft_dia") or case_motor.get("shaft_dia_mm"),
            "poles": case_motor.get("poles"),
            "slots_stator": case_motor.get("slots_stator"),
            "slots_rotor": case_motor.get("slots_rotor"),
            "torque": (_calc_rated_torque(case_motor.get("rated_power"), case_motor.get("rated_speed"))
                      or case_motor.get("rated_torque") or case_motor.get("torque_nm")),
            "stator_slot_type": case_motor.get("stator_slot_type"),
            "rotor_slot_type": case_motor.get("rotor_slot_type"),
        }
        # 清理 None 值
        return {k: v for k, v in seed.items() if v is not None}
    except Exception as e:
        print(f"[seed_extractor] 案例电机转种子失败: {e}")
        return {}


def _check_seed_consistency(seeds: list, base_params: dict) -> dict:
    """检查种子与基准参数的一致性"""
    mismatches = []
    # 关键几何参数一致性检查
    key_params = ["stator_od", "stator_id", "rotor_od", "core_length", "air_gap"]
    for seed in seeds:
        for param in key_params:
            seed_val = seed.get(param)
            base_val = base_params.get(param)
            if seed_val and base_val:
                try:
                    # 允许 5% 的偏差（工艺公差）
                    if abs(seed_val - base_val) / base_val > 0.05:
                        mismatches.append(f"种子{seed.get('model', '?')}的{param}: {seed_val} vs 基准{base_val}")
                except (TypeError, ValueError):
                    pass
    return {"mismatches": mismatches}


if __name__ == "__main__":
    import pprint
    print("=== 空 specs ===")
    pprint.pprint(extract_seeds({}))
    print("\n=== specs: 30kW / 4极 / 异步电动机 ===")
    pprint.pprint(extract_seeds({"power_kw": 30, "poles": 4, "motor_type": "异步电动机"}))
