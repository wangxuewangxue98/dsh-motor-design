# -*- coding: utf-8 -*-
"""
结构化评审回灌 Schema 与解析器 (Review Feedback Schema) v4.01.037
====================================================================
用户规格「3.3 结构化评审回灌」:
  · 评审智能体(ReviewAgent / 四剑客 / 人工批注)必须输出统一 JSON 结构
  · 解析失败时自动降级(容错 → 正则兜底 → 人工填写), 不再依赖单一正则提取
  · DesignAgent 收到反馈后按 feedback_list 逐项修改参数, 而非猜测

统一结构 (review_feedback):
{
  "overall": "revise",              # accept | revise | reject
  "confidence": 0.82,               # 评审置信度 0~1
  "summary": "极数与转矩不匹配, 建议降极数并加长铁心",
  "feedback_list": [
      {"field": "poles", "old_value": 6, "new_value": 4,
       "reason": "22000rpm 下 6 极电频率过高, 铁耗超限"},
      {"field": "core_length", "old_value": 170, "new_value": 185,
       "reason": "转矩不足 8%, 需加长铁心"}
  ]
}

设计要点:
  1. FIELD_REGISTRY 统一登记可回灌字段 → 类型/范围/写入目标(specs 或 base_params)
  2. specs 用英文 snake_case 键; base_params 用中文带单位键 → 由本模块自动双写
  3. parse_review_feedback() 五级容错解析(直接/围栏/贪婪/截断补全/逐对象提取)
  4. apply_review_feedback() 逐项类型转换 + 范围校验 + 写入, 越界项进入 rejected
  5. feedback_from_comment() 为无 LLM 场景的正则兜底(覆盖全字段中文别名)
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 一、字段登记表
#    canonical: 规范字段名(英文)
#    cn:        中文名(用于展示 / 正则兜底)
#    type:      int | float | str
#    target:    specs | base_params | control
#    bp_key:    base_params 中的中文键(None=不双写)
#    lo/hi:     数值合法区间
#    options:   离散合法值
#    aliases:   中文/英文别名(正则兜底与 LLM 自由命名归一化)
# ============================================================
FIELD_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ── 需求级参数 (specs) ──
    "motor_type": {
        "cn": "电机类型", "type": "str", "target": "specs", "bp_key": "电机类型",
        "options": ["异步电动机", "永磁同步电动机", "无刷直流电动机",
                    "开关磁阻电动机", "直流电动机", "同步电动机", "发电机"],
        "aliases": ["电机类型", "电机型式", "motor_type", "类型"],
    },
    "power_kw": {
        "cn": "额定功率", "type": "float", "target": "specs", "bp_key": "额定功率(kW)",
        "lo": 0.01, "hi": 5000.0, "unit": "kW",
        "aliases": ["额定功率", "功率", "power_kw", "power"],
    },
    "voltage_v": {
        "cn": "额定电压", "type": "float", "target": "specs", "bp_key": "额定电压(V)",
        "lo": 6.0, "hi": 20000.0, "unit": "V",
        "aliases": ["额定电压", "电压", "voltage_v", "voltage"],
    },
    "speed_rpm": {
        "cn": "额定转速", "type": "int", "target": "specs", "bp_key": "额定转速(rpm)",
        "lo": 1, "hi": 150000, "unit": "rpm",
        "aliases": ["额定转速", "转速", "speed_rpm", "rpm"],
    },
    "poles": {
        "cn": "极数", "type": "int", "target": "specs", "bp_key": "极数",
        "options": [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
        "aliases": ["极数", "极对数x2", "poles", "pole"],
    },
    "frequency_hz": {
        "cn": "频率", "type": "float", "target": "specs", "bp_key": "频率(Hz)",
        "lo": 1.0, "hi": 5000.0, "unit": "Hz",
        "aliases": ["频率", "工频", "frequency_hz", "frequency", "freq"],
    },
    "phase": {
        "cn": "相数", "type": "int", "target": "specs", "bp_key": "相数",
        "options": [1, 2, 3, 6, 9, 12],
        "aliases": ["相数", "phase", "phases"],
    },
    "torque_nm": {
        "cn": "额定转矩", "type": "float", "target": "specs", "bp_key": "目标转矩(N.m)",
        "lo": 0.01, "hi": 500000.0, "unit": "N.m",
        "aliases": ["额定转矩", "目标转矩", "转矩", "扭矩", "torque_nm", "torque"],
    },
    "efficiency": {
        "cn": "目标效率", "type": "float", "target": "specs", "bp_key": "目标效率(%)",
        "lo": 1.0, "hi": 100.0, "unit": "%",
        "aliases": ["目标效率", "效率", "efficiency", "eff"],
    },
    "power_factor": {
        "cn": "功率因数", "type": "float", "target": "specs", "bp_key": "目标功率因数",
        "lo": 0.1, "hi": 1.0,
        "aliases": ["功率因数", "目标功率因数", "power_factor", "cos"],
    },
    "rated_current_a": {
        "cn": "额定电流", "type": "float", "target": "specs", "bp_key": "线电流(A)",
        "lo": 0.01, "hi": 50000.0, "unit": "A",
        "aliases": ["额定电流", "线电流", "电流", "rated_current_a", "current"],
    },
    "cooling_method": {
        "cn": "冷却方式", "type": "str", "target": "specs", "bp_key": "冷却方式",
        "aliases": ["冷却方式", "冷却", "cooling_method", "cooling"],
    },
    "insulation_class": {
        "cn": "绝缘等级", "type": "str", "target": "specs", "bp_key": "绝缘等级",
        "options": ["A", "E", "B", "F", "H", "N", "R", "C"],
        "aliases": ["绝缘等级", "绝缘", "insulation_class", "insulation"],
    },
    "protection_class": {
        "cn": "防护等级", "type": "str", "target": "specs", "bp_key": "防护等级",
        "aliases": ["防护等级", "防护", "protection_class", "ip"],
    },
    "altitude_m": {
        "cn": "海拔高度", "type": "float", "target": "specs", "bp_key": "海拔(m)",
        "lo": 0.0, "hi": 6000.0, "unit": "m",
        "aliases": ["海拔高度", "海拔", "altitude_m", "altitude"],
    },
    "overload_pct": {
        "cn": "过载倍数", "type": "float", "target": "specs", "bp_key": "过载(%)",
        "lo": 0.0, "hi": 400.0, "unit": "%",
        "aliases": ["过载倍数", "过载", "overload_pct", "overload"],
    },
    "ambient_temp_c": {
        "cn": "环境温度", "type": "float", "target": "specs", "bp_key": None,
        "lo": -60.0, "hi": 80.0, "unit": "C",
        "aliases": ["环境温度", "ambient_temp_c", "ambient"],
    },
    "duty_cycle": {
        "cn": "工作制", "type": "str", "target": "specs", "bp_key": None,
        "aliases": ["工作制", "duty_cycle", "duty"],
    },
    "mounting": {
        "cn": "安装方式", "type": "str", "target": "specs", "bp_key": None,
        "aliases": ["安装方式", "安装", "mounting"],
    },
    # ── 结构级参数 (base_params, 同时同步进 specs 供求解器读取) ──
    "stator_od": {
        "cn": "定子外径", "type": "float", "target": "base_params", "bp_key": "定子外径(mm)",
        "lo": 10.0, "hi": 3000.0, "unit": "mm",
        "aliases": ["定子外径", "外径", "stator_od"],
    },
    "stator_id": {
        "cn": "定子内径", "type": "float", "target": "base_params", "bp_key": "定子内径(mm)",
        "lo": 5.0, "hi": 2900.0, "unit": "mm",
        "aliases": ["定子内径", "内径", "stator_id"],
    },
    "rotor_od": {
        "cn": "转子外径", "type": "float", "target": "base_params", "bp_key": "转子外径(mm)",
        "lo": 5.0, "hi": 2900.0, "unit": "mm",
        "aliases": ["转子外径", "rotor_od"],
    },
    "core_length": {
        "cn": "铁心长度", "type": "float", "target": "base_params", "bp_key": "铁心长度(mm)",
        "lo": 5.0, "hi": 5000.0, "unit": "mm",
        "aliases": ["铁心长度", "铁芯长度", "叠长", "core_length", "stack_length"],
    },
    "air_gap": {
        "cn": "气隙长度", "type": "float", "target": "base_params", "bp_key": "气隙长度(mm)",
        "lo": 0.1, "hi": 20.0, "unit": "mm",
        "aliases": ["气隙长度", "气隙", "air_gap", "airgap"],
    },
    "shaft_dia": {
        "cn": "轴径", "type": "float", "target": "base_params", "bp_key": "轴径(mm)",
        "lo": 5.0, "hi": 1000.0, "unit": "mm",
        "aliases": ["轴径", "轴直径", "shaft_dia", "shaft"],
    },
    "slots": {
        "cn": "定子槽数", "type": "int", "target": "base_params", "bp_key": "定子槽数",
        "lo": 3, "hi": 240,
        "aliases": ["定子槽数", "槽数", "slots", "stator_slots"],
    },
    "rotor_slots": {
        "cn": "转子槽数", "type": "int", "target": "base_params", "bp_key": "转子槽数",
        "lo": 3, "hi": 240,
        "aliases": ["转子槽数", "rotor_slots"],
    },
    # ── 流程控制参数 (control) ──
    "solve_count": {
        "cn": "求解方案数", "type": "int", "target": "control", "bp_key": None,
        "lo": 1, "hi": 100,
        "aliases": ["求解方案数", "方案数", "solve_count"],
    },
    "fine_solve_count": {
        "cn": "精细验证数", "type": "int", "target": "control", "bp_key": None,
        "lo": 0, "hi": 20,
        "aliases": ["精细验证数", "精细方案数", "fine_solve_count"],
    },
}

# 别名 → 规范字段名 反查表 (小写)
_ALIAS_INDEX: Dict[str, str] = {}
for _canon, _meta in FIELD_REGISTRY.items():
    _ALIAS_INDEX[_canon.lower()] = _canon
    _ALIAS_INDEX[_meta["cn"]] = _canon
    if _meta.get("bp_key"):
        _ALIAS_INDEX[_meta["bp_key"]] = _canon
    for _a in _meta.get("aliases", []):
        _ALIAS_INDEX[str(_a).lower()] = _canon

# 电机类型关键词 → 规范类型
MOTOR_TYPE_KEYWORDS = [
    ("开关磁阻", "开关磁阻电动机"),
    ("无刷直流", "无刷直流电动机"),
    ("无刷", "无刷直流电动机"),
    ("永磁同步", "永磁同步电动机"),
    ("永磁", "永磁同步电动机"),
    ("异步", "异步电动机"),
    ("感应", "异步电动机"),
    ("直流", "直流电动机"),
    ("同步", "永磁同步电动机"),
]

# ============================================================
# 二、JSON Schema (供 API / 校验 / 文档使用)
# ============================================================
REVIEW_FEEDBACK_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "review_feedback",
    "type": "object",
    "required": ["overall", "confidence", "feedback_list"],
    "properties": {
        "overall": {
            "type": "string",
            "enum": ["accept", "revise", "reject"],
            "description": "总体结论: accept=通过 / revise=需修正 / reject=否决",
        },
        "confidence": {
            "type": "number", "minimum": 0.0, "maximum": 1.0,
            "description": "评审置信度, 供决策门质量门禁使用",
        },
        "summary": {"type": "string", "description": "评审结论摘要(不超过200字)"},
        "feedback_list": {
            "type": "array",
            "description": "逐项参数修正建议; 无修正建议时为空数组",
            "items": {
                "type": "object",
                "required": ["field", "new_value", "reason"],
                "properties": {
                    "field": {"type": "string",
                              "description": "参数名, 取值见 FIELD_REGISTRY"},
                    "old_value": {"description": "原值(可省略, 系统会自动补全)"},
                    "new_value": {"description": "建议值"},
                    "reason": {"type": "string", "description": "修正理由"},
                    "severity": {"type": "string",
                                 "enum": ["info", "warn", "critical"],
                                 "description": "严重程度, 默认 warn"},
                },
            },
        },
    },
}

VALID_OVERALL = ("accept", "revise", "reject")
VALID_SEVERITY = ("info", "warn", "critical")

# ============================================================
# 三、LLM 输出约束提示词 (注入各评审智能体 prompt 末尾)
# ============================================================
FEEDBACK_OUTPUT_INSTRUCTION = """
【输出格式强制要求】
除正文评审意见外, 必须在回答最末尾追加一段 JSON, 用 ```json 围栏包裹, 结构固定为:
```json
{
  "overall": "revise",
  "confidence": 0.80,
  "summary": "一句话评审结论",
  "feedback_list": [
    {"field": "poles", "old_value": 6, "new_value": 4, "reason": "修正理由", "severity": "warn"}
  ]
}
```
规则:
1. overall 只能取 accept / revise / reject 三者之一;
2. confidence 为 0~1 的小数, 表示你对本次评审结论的置信度;
3. field 必须使用下列参数名之一(不要自造字段名):
   %(FIELD_LIST)s
4. 若无需修改任何参数, feedback_list 填空数组 [], overall 填 accept;
5. new_value 只写数值或字符串本身, 不要带单位;
6. JSON 必须完整闭合, 不要输出注释。
""" % {"FIELD_LIST": ", ".join(
    "%s(%s)" % (k, v["cn"]) for k, v in FIELD_REGISTRY.items()
    if v["target"] != "control")}


def build_feedback_instruction(fields: Optional[List[str]] = None) -> str:
    """按需生成字段受限的输出约束提示词(用于只允许改结构参数等场景)"""
    if not fields:
        return FEEDBACK_OUTPUT_INSTRUCTION
    allow = [f for f in fields if f in FIELD_REGISTRY]
    if not allow:
        return FEEDBACK_OUTPUT_INSTRUCTION
    lst = ", ".join("%s(%s)" % (f, FIELD_REGISTRY[f]["cn"]) for f in allow)
    return re.sub(r"(?<=不要自造字段名\):\n   ).*", lst,
                  FEEDBACK_OUTPUT_INSTRUCTION, count=1)


# ============================================================
# 四、解析: 五级容错
# ============================================================
def _strip_fences(text: str) -> List[str]:
    """提取 ```json ... ``` / ``` ... ``` 围栏内容(可能多段)"""
    out = []
    for m in re.finditer(r"```(?:json|JSON)?\s*(.+?)```", text, re.S):
        out.append(m.group(1).strip())
    return out


def _greedy_braces(text: str) -> List[str]:
    """取最外层 { ... } 片段 (从最后一个 { 起逐个尝试, 评审 JSON 常在末尾)"""
    cands = []
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for s in reversed(starts[-8:]):          # 只看最后 8 个起点, 避免爆炸
        depth = 0
        for i in range(s, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    cands.append(text[s:i + 1])
                    break
    # 长片段优先(信息更全)
    cands.sort(key=len, reverse=True)
    return cands[:5]


def _close_all(s: str) -> str:
    """栈式补齐: 按实际嵌套顺序补上缺失的 " } ] (计数法会补错顺序)"""
    stack: List[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    out = s
    if in_str:
        out += '"'
    for ch in reversed(stack):
        out += "}" if ch == "{" else "]"
    return out


def _repair_candidates(frag: str) -> List[str]:
    """截断补全: 生成多个修复候选, 由调用方逐个尝试解析"""
    base = frag.strip()
    cands: List[str] = []

    # A: 直接闭合 (未写完的字符串补引号, 再按栈序补括号)
    cands.append(_close_all(base))

    # B: 丢弃尾部不完整键值对后闭合
    b = base
    if b.count('"') % 2 == 1:
        b = b[:b.rfind('"')]
    b = re.sub(r"[,:]\s*$", "", b)
    b = re.sub(r",?\s*\"[^\"]*\"\s*:?\s*$", "", b)
    b = re.sub(r",\s*$", "", b)
    cands.append(_close_all(b))

    # C: 丢弃最后一个不完整对象后闭合
    idx = base.rfind("{")
    if idx > 0:
        c = re.sub(r",\s*$", "", base[:idx].rstrip())
        cands.append(_close_all(c))

    seen, out = set(), []
    for c in cands:
        c = re.sub(r",\s*([}\]])", r"\1", c).strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _extract_objects(text: str) -> List[dict]:
    """逐对象提取: 从任意文本捞出含 field/new_value 的小对象"""
    items = []
    for m in re.finditer(r"\{[^{}]{0,600}\}", text, re.S):
        frag = m.group(0)
        if '"field"' not in frag and "'field'" not in frag:
            continue
        try:
            obj = json.loads(frag.replace("'", '"'))
        except Exception:
            obj = {}
            fm = re.search(r"[\"']field[\"']\s*:\s*[\"']([^\"']+)", frag)
            nm = re.search(r"[\"']new_value[\"']\s*:\s*([^,}]+)", frag)
            rm = re.search(r"[\"']reason[\"']\s*:\s*[\"']([^\"']*)", frag)
            om = re.search(r"[\"']old_value[\"']\s*:\s*([^,}]+)", frag)
            if not fm:
                continue
            obj["field"] = fm.group(1)
            if nm:
                obj["new_value"] = nm.group(1).strip().strip('"\'')
            if om:
                obj["old_value"] = om.group(1).strip().strip('"\'')
            if rm:
                obj["reason"] = rm.group(1)
        if isinstance(obj, dict) and obj.get("field"):
            items.append(obj)
    return items


def parse_review_feedback(raw: Any) -> Dict[str, Any]:
    """五级容错解析评审智能体输出.

    返回: {"ok": bool, "feedback": {...}, "level": str, "error": str}
      level: direct | fenced | greedy | repaired | objects | none
    """
    if raw is None:
        return {"ok": False, "feedback": _empty_feedback(), "level": "none",
                "error": "空输出"}
    if isinstance(raw, dict):
        fb, errs = validate_feedback(raw)
        return {"ok": not errs, "feedback": fb, "level": "direct",
                "error": "; ".join(errs)}
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return {"ok": False, "feedback": _empty_feedback(), "level": "none",
                "error": "空输出"}

    # L1 直接解析
    candidates: List[Tuple[str, str]] = [("direct", text.strip())]
    # L2 围栏
    for frag in _strip_fences(text):
        candidates.append(("fenced", frag))
    # L3 贪婪括号
    for frag in _greedy_braces(text):
        candidates.append(("greedy", frag))

    for level, frag in candidates:
        obj = _try_json(frag)
        if obj is None:
            # L4 截断补全 (多候选逐个尝试)
            for rep in _repair_candidates(frag):
                obj = _try_json(rep)
                if obj is not None:
                    level = "repaired"
                    break
        if obj is None:
            continue
        if isinstance(obj, list):
            obj = {"feedback_list": obj}
        if not isinstance(obj, dict):
            continue
        if "feedback_list" not in obj and "field" in obj:
            obj = {"feedback_list": [obj]}
        if "feedback_list" not in obj:
            continue
        fb, errs = validate_feedback(obj)
        return {"ok": True, "feedback": fb, "level": level,
                "error": "; ".join(errs)}

    # L5 逐对象提取
    items = _extract_objects(text)
    if items:
        fb, errs = validate_feedback({"feedback_list": items,
                                      "overall": "revise",
                                      "summary": "由零散对象提取(JSON 不完整)"})
        return {"ok": True, "feedback": fb, "level": "objects",
                "error": "; ".join(errs) or "JSON 不完整, 已逐对象提取"}

    return {"ok": False, "feedback": _empty_feedback(), "level": "none",
            "error": "未能从输出中解析出 review_feedback JSON"}


def _try_json(frag: str):
    try:
        return json.loads(frag)
    except Exception:
        return None


def _empty_feedback() -> Dict[str, Any]:
    return {"overall": "revise", "confidence": 0.0, "summary": "",
            "feedback_list": [], "unrecognized": []}


def normalize_field_name(name: Any) -> Optional[str]:
    """字段名归一化: 支持中文名 / base_params 中文键 / 英文别名 / 带单位形式"""
    if name is None:
        return None
    key = str(name).strip()
    if not key:
        return None
    if key in FIELD_REGISTRY:
        return key
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]
    low = key.lower()
    if low in _ALIAS_INDEX:
        return _ALIAS_INDEX[low]
    # 去括号单位: "定子外径(mm)" / "power_kw (kW)"
    bare = re.sub(r"[（(].*?[)）]", "", key).strip()
    if bare in _ALIAS_INDEX:
        return _ALIAS_INDEX[bare]
    if bare.lower() in _ALIAS_INDEX:
        return _ALIAS_INDEX[bare.lower()]
    # 包含匹配(最长别名优先)
    hits = [(len(a), c) for a, c in _ALIAS_INDEX.items()
            if len(a) >= 2 and a in low]
    if hits:
        hits.sort(reverse=True)
        return hits[0][1]
    return None


def coerce_value(field: str, value: Any) -> Tuple[bool, Any, str]:
    """按登记表做类型转换 + 范围/枚举校验.

    返回 (ok, value, error)
    """
    meta = FIELD_REGISTRY.get(field)
    if not meta:
        return False, None, "未登记字段"
    if value is None or (isinstance(value, str) and not value.strip()):
        return False, None, "空值"
    vtype = meta["type"]

    if vtype in ("int", "float"):
        if isinstance(value, bool):
            return False, None, "布尔值不可用于数值字段"
        if isinstance(value, (int, float)):
            num = float(value)
        else:
            m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
            if not m:
                return False, None, "无法解析数值: %s" % value
            num = float(m.group(0))
        if vtype == "int":
            num_i = int(round(num))
            if meta.get("options") and num_i not in meta["options"]:
                return False, None, "取值不在允许集合 %s" % (meta["options"],)
            lo, hi = meta.get("lo"), meta.get("hi")
            if lo is not None and num_i < lo:
                return False, None, "小于下限 %s" % lo
            if hi is not None and num_i > hi:
                return False, None, "大于上限 %s" % hi
            return True, num_i, ""
        lo, hi = meta.get("lo"), meta.get("hi")
        if lo is not None and num < lo:
            return False, None, "小于下限 %s" % lo
        if hi is not None and num > hi:
            return False, None, "大于上限 %s" % hi
        return True, round(num, 4), ""

    # 字符串
    sval = str(value).strip()
    if field == "motor_type":
        for kw, canon in MOTOR_TYPE_KEYWORDS:
            if kw in sval:
                return True, canon, ""
        if sval in meta.get("options", []):
            return True, sval, ""
        return False, None, "无法识别的电机类型: %s" % sval
    if field == "insulation_class":
        m = re.search(r"[AEBFHNRC]", sval.upper())
        if m:
            return True, m.group(0), ""
        return False, None, "无法识别的绝缘等级: %s" % sval
    if field == "protection_class":
        m = re.search(r"IP\s*(\d{2})", sval.upper())
        if m:
            return True, "IP%s" % m.group(1), ""
        return True, sval, ""
    if meta.get("options") and sval not in meta["options"]:
        return False, None, "取值不在允许集合 %s" % (meta["options"],)
    return True, sval, ""


def validate_feedback(obj: dict) -> Tuple[Dict[str, Any], List[str]]:
    """结构规整化: 归一化字段名/严重度/置信度, 剔除非法项到 unrecognized"""
    errors: List[str] = []
    fb = _empty_feedback()

    overall = str(obj.get("overall", "") or "").strip().lower()
    if overall in VALID_OVERALL:
        fb["overall"] = overall
    else:
        if overall:
            errors.append("overall 非法值: %s" % overall)
        fb["overall"] = "revise" if obj.get("feedback_list") else "accept"

    try:
        conf = float(obj.get("confidence", 0.0))
    except Exception:
        conf = 0.0
        errors.append("confidence 无法解析")
    if conf > 1.0:                    # 兼容 LLM 输出 0~100
        conf = conf / 100.0 if conf <= 100.0 else 1.0
    fb["confidence"] = round(max(0.0, min(1.0, conf)), 3)

    fb["summary"] = str(obj.get("summary", "") or "").strip()[:500]

    raw_list = obj.get("feedback_list") or []
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    for item in raw_list:
        if not isinstance(item, dict):
            errors.append("feedback 项非对象: %r" % (item,))
            continue
        canon = normalize_field_name(item.get("field") or item.get("参数")
                                     or item.get("name"))
        newv = item.get("new_value", item.get("建议值", item.get("value")))
        if not canon:
            fb["unrecognized"].append({
                "field": str(item.get("field", "")),
                "new_value": newv,
                "reason": str(item.get("reason", ""))[:200],
                "error": "字段名无法归一化",
            })
            errors.append("字段名无法识别: %s" % item.get("field"))
            continue
        ok, val, err = coerce_value(canon, newv)
        if not ok:
            fb["unrecognized"].append({
                "field": canon, "new_value": newv,
                "reason": str(item.get("reason", ""))[:200], "error": err,
            })
            errors.append("%s 值非法(%s): %r" % (canon, err, newv))
            continue
        sev = str(item.get("severity", "warn")).strip().lower()
        if sev not in VALID_SEVERITY:
            sev = "warn"
        fb["feedback_list"].append({
            "field": canon,
            "field_cn": FIELD_REGISTRY[canon]["cn"],
            "target": FIELD_REGISTRY[canon]["target"],
            "old_value": item.get("old_value", item.get("原值")),
            "new_value": val,
            "reason": str(item.get("reason", item.get("理由", "")) or "")[:300],
            "severity": sev,
        })
    return fb, errors


# ============================================================
# 五、应用: 逐项写入 specs / base_params
# ============================================================
def apply_review_feedback(feedback: Dict[str, Any],
                          specs: Optional[dict] = None,
                          base_params: Optional[dict] = None,
                          base_param_ranges: Optional[dict] = None,
                          control: Optional[dict] = None) -> Dict[str, Any]:
    """按 feedback_list 逐项修改参数 (原地修改传入的 dict).

    返回 {"applied":[...], "skipped":[...], "rejected":[...], "changed":{field:(old,new)}}
      applied  — 已写入
      skipped  — 值与现值一致, 无需修改
      rejected — 越界/类型错误(来自 validate 阶段的 unrecognized)
    """
    applied, skipped = [], []
    changed: Dict[str, Any] = {}
    fb_list = (feedback or {}).get("feedback_list", []) or []

    for item in fb_list:
        field = item.get("field")
        meta = FIELD_REGISTRY.get(field)
        if not meta:
            continue
        newv = item.get("new_value")
        target = meta["target"]
        bp_key = meta.get("bp_key")

        # 取现值 (优先 specs, 再 base_params)
        oldv = None
        if specs is not None and field in specs:
            oldv = specs.get(field)
        elif base_params is not None and bp_key and bp_key in base_params:
            oldv = base_params.get(bp_key)
        elif control is not None and field in control:
            oldv = control.get(field)
        if item.get("old_value") in (None, "") and oldv is not None:
            item["old_value"] = oldv

        if _same_value(oldv, newv):
            skipped.append({"field": field, "field_cn": meta["cn"],
                            "value": newv, "reason": "与现值一致"})
            continue

        # 写入
        if target == "control":
            if control is not None:
                control[field] = newv
        else:
            if specs is not None:
                specs[field] = newv           # 结构参数也同步进 specs 供求解器读取
            if base_params is not None and bp_key:
                base_params[bp_key] = newv
            # 同步 base_param_ranges 的 base 基准值, 保持区间中心跟随
            if base_param_ranges is not None and bp_key and bp_key in base_param_ranges:
                rng = base_param_ranges[bp_key]
                if isinstance(rng, dict):
                    rng["base"] = newv
                    if "min" in rng and isinstance(rng.get("min"), (int, float)) \
                            and isinstance(newv, (int, float)) and newv < rng["min"]:
                        rng["min"] = newv
                    if "max" in rng and isinstance(rng.get("max"), (int, float)) \
                            and isinstance(newv, (int, float)) and newv > rng["max"]:
                        rng["max"] = newv
                    if "options" in rng and newv not in (rng.get("options") or []):
                        rng["options"] = list(rng.get("options") or []) + [newv]

        applied.append({"field": field, "field_cn": meta["cn"],
                        "old_value": oldv, "new_value": newv,
                        "reason": item.get("reason", ""),
                        "severity": item.get("severity", "warn"),
                        "target": target})
        changed[field] = (oldv, newv)

    return {"applied": applied, "skipped": skipped,
            "rejected": list((feedback or {}).get("unrecognized", []) or []),
            "changed": changed}


def _same_value(a: Any, b: Any) -> bool:
    if a is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return str(a).strip() == str(b).strip()


# ============================================================
# 六、正则兜底 (无 LLM / 人工自由文本批注)
# ============================================================
_VERB = r"(?:改为|调整为|调为|改成|修改为|设为|设置为|变更为|应为|建议|取|=|:|：|至|到)?"


def feedback_from_comment(comment: str,
                          specs: Optional[dict] = None,
                          base_params: Optional[dict] = None) -> Dict[str, Any]:
    """从人工/LLM 自由文本批注中正则提取结构化反馈 (降级路径).

    覆盖 FIELD_REGISTRY 全部中文别名, 替代原先只认「极数/功率」的两条正则。
    """
    specs = specs or {}
    base_params = base_params or {}
    items: List[dict] = []
    if not comment:
        return _empty_feedback()
    text = str(comment)
    seen = set()

    # 1) 别名 + 动词 + 数值 (长别名优先, 避免"外径"抢占"定子外径")
    alias_pairs: List[Tuple[str, str]] = []
    for canon, meta in FIELD_REGISTRY.items():
        if meta["target"] == "control":
            continue
        names = {meta["cn"]}
        names.update(a for a in meta.get("aliases", []) if re.search(r"[\u4e00-\u9fff]", a))
        for nm in names:
            alias_pairs.append((nm, canon))
    alias_pairs.sort(key=lambda x: len(x[0]), reverse=True)

    for alias, canon in alias_pairs:
        if canon in seen:
            continue
        meta = FIELD_REGISTRY[canon]
        if meta["type"] == "str":
            continue
        pat = re.escape(alias) + r"\s*" + _VERB + r"\s*(-?\d+(?:\.\d+)?)"
        m = re.search(pat, text)
        if not m:
            continue
        ok, val, _err = coerce_value(canon, m.group(1))
        if ok:
            items.append({"field": canon, "new_value": val,
                          "reason": "自由文本提取: %s" % _ctx(text, m.start()),
                          "severity": "warn"})
            seen.add(canon)

    # 2) 单位后缀型: "4极" "15kW" "1500rpm" "380V" "185mm铁心"
    unit_rules = [
        ("poles", r"(\d+)\s*极"),
        ("power_kw", r"(\d+(?:\.\d+)?)\s*[kK][wW]"),
        ("power_kw", r"(\d+(?:\.\d+)?)\s*千瓦"),
        ("speed_rpm", r"(\d+)\s*(?:rpm|r/min|转/分|转)"),
        ("voltage_v", r"(\d+(?:\.\d+)?)\s*[vV]伏?"),
        ("frequency_hz", r"(\d+(?:\.\d+)?)\s*(?:Hz|HZ|hz|赫兹)"),
        ("torque_nm", r"(\d+(?:\.\d+)?)\s*(?:N\.?m|N·m|牛米|牛顿米)"),
        ("efficiency", r"效率\s*[≥>=大于不低于]*\s*(\d+(?:\.\d+)?)\s*%"),
    ]
    for canon, pat in unit_rules:
        if canon in seen:
            continue
        m = re.search(pat, text)
        if not m:
            continue
        ok, val, _err = coerce_value(canon, m.group(1))
        if ok:
            items.append({"field": canon, "new_value": val,
                          "reason": "自由文本提取: %s" % _ctx(text, m.start()),
                          "severity": "warn"})
            seen.add(canon)

    # 3) 电机类型 / 绝缘 / 防护 关键词
    if "motor_type" not in seen:
        for kw, canon_type in MOTOR_TYPE_KEYWORDS:
            if kw in text:
                items.append({"field": "motor_type", "new_value": canon_type,
                              "reason": "自由文本提取: 关键词「%s」" % kw,
                              "severity": "warn"})
                seen.add("motor_type")
                break
    if "insulation_class" not in seen:
        m = re.search(r"绝缘\s*(?:等级)?\s*" + _VERB + r"\s*([AEBFHNRC])\b", text)
        if m:
            items.append({"field": "insulation_class", "new_value": m.group(1),
                          "reason": "自由文本提取: 绝缘等级", "severity": "info"})
            seen.add("insulation_class")
    if "protection_class" not in seen:
        m = re.search(r"(IP\s*\d{2})", text, re.I)
        if m:
            items.append({"field": "protection_class", "new_value": m.group(1),
                          "reason": "自由文本提取: 防护等级", "severity": "info"})
            seen.add("protection_class")

    fb, _errs = validate_feedback({
        "overall": "revise" if items else "accept",
        "confidence": 0.5 if items else 0.0,
        "summary": ("正则兜底提取 %d 项修正" % len(items)) if items
                   else "未从批注中提取到可执行的参数修正",
        "feedback_list": items,
    })
    # 补全 old_value 便于展示
    for it in fb["feedback_list"]:
        if it.get("old_value") in (None, ""):
            f = it["field"]
            bp_key = FIELD_REGISTRY[f].get("bp_key")
            it["old_value"] = specs.get(f, base_params.get(bp_key) if bp_key else None)
    return fb


def _ctx(text: str, pos: int, width: int = 14) -> str:
    lo = max(0, pos - width)
    hi = min(len(text), pos + width)
    return text[lo:hi].replace("\n", " ")


# ============================================================
# 七、组合入口: LLM 输出优先, 失败自动降级正则, 再失败转人工
# ============================================================
def resolve_feedback(raw_output: Any,
                     comment: str = "",
                     specs: Optional[dict] = None,
                     base_params: Optional[dict] = None,
                     retry_fn=None) -> Dict[str, Any]:
    """统一回灌入口.

    raw_output — 评审智能体原始输出(str/dict), 可为空
    comment    — 人工批注原文(作为兜底来源)
    retry_fn   — 可选: 解析失败时调用 retry_fn() 重新向 LLM 索要一次 JSON

    返回 {"feedback":{...}, "source":"llm|llm_retry|regex|manual", "parse_level":str,
          "parse_error":str, "need_manual":bool}
    """
    res = parse_review_feedback(raw_output)
    if res["ok"] and (res["feedback"]["feedback_list"] or
                      res["feedback"]["overall"] == "accept"):
        return {"feedback": res["feedback"], "source": "llm",
                "parse_level": res["level"], "parse_error": res["error"],
                "need_manual": False}

    # 一次 LLM 重试
    if retry_fn is not None:
        try:
            retry_raw = retry_fn()
        except Exception as e:                     # noqa: BLE001
            retry_raw = None
            res["error"] = "%s; 重试异常: %s" % (res["error"], e)
        if retry_raw:
            res2 = parse_review_feedback(retry_raw)
            if res2["ok"]:
                return {"feedback": res2["feedback"], "source": "llm_retry",
                        "parse_level": res2["level"], "parse_error": res2["error"],
                        "need_manual": False}

    # 正则兜底
    src_text = comment or (raw_output if isinstance(raw_output, str) else "")
    fb = feedback_from_comment(src_text, specs, base_params)
    if fb["feedback_list"]:
        return {"feedback": fb, "source": "regex", "parse_level": "regex",
                "parse_error": res["error"], "need_manual": False}

    return {"feedback": fb, "source": "manual", "parse_level": res["level"],
            "parse_error": res["error"] or "无可执行修正", "need_manual": True}


def feedback_summary_text(feedback: Dict[str, Any], max_items: int = 8) -> str:
    """生成人类可读摘要 (用于 SSE 进度 / 报告披露)"""
    fb = feedback or {}
    lines = []
    ov = {"accept": "通过", "revise": "需修正", "reject": "否决"}.get(
        fb.get("overall", ""), fb.get("overall", "?"))
    lines.append("结论=%s, 置信度=%.2f" % (ov, fb.get("confidence", 0.0)))
    if fb.get("summary"):
        lines.append("摘要: %s" % fb["summary"])
    for it in (fb.get("feedback_list") or [])[:max_items]:
        lines.append("· %s: %s → %s (%s)" % (
            it.get("field_cn") or it.get("field"),
            it.get("old_value", "?"), it.get("new_value"),
            (it.get("reason") or "")[:40]))
    rest = len(fb.get("feedback_list") or []) - max_items
    if rest > 0:
        lines.append("· 其余 %d 项省略" % rest)
    for it in (fb.get("unrecognized") or [])[:3]:
        lines.append("! 丢弃 %s=%s (%s)" % (it.get("field"),
                                            it.get("new_value"), it.get("error")))
    return "\n".join(lines)


# ============================================================
# 自测
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("review_feedback_schema 自测")
    print("=" * 60)

    specs = {"motor_type": "异步电动机", "power_kw": 200.0, "poles": 6,
             "speed_rpm": 22000, "voltage_v": 380}
    bp = {"定子外径(mm)": 260.0, "铁心长度(mm)": 170.0, "极数": 6}
    rng = {"铁心长度(mm)": {"base": 170.0, "min": 150.0, "max": 190.0, "fixed": False},
           "极数": {"base": 6, "options": [4, 6, 8], "fixed": False}}

    # 1) 标准围栏 JSON
    llm_out = """评审意见: 6 极在 22000rpm 下电频率过高。
```json
{"overall":"revise","confidence":0.85,"summary":"降极数并加长铁心",
 "feedback_list":[
   {"field":"poles","old_value":6,"new_value":4,"reason":"电频率过高","severity":"critical"},
   {"field":"铁心长度","new_value":185,"reason":"转矩不足8%"},
   {"field":"效率","new_value":"96.5%","reason":"目标上调"},
   {"field":"不存在的字段","new_value":1,"reason":"测试丢弃"}]}
```"""
    r = parse_review_feedback(llm_out)
    print("\n[1] 解析等级=%s ok=%s err=%s" % (r["level"], r["ok"], r["error"]))
    print(feedback_summary_text(r["feedback"]))
    rep = apply_review_feedback(r["feedback"], specs, bp, rng)
    print("applied=%d skipped=%d rejected=%d" % (
        len(rep["applied"]), len(rep["skipped"]), len(rep["rejected"])))
    print("specs.poles=%s bp[铁心长度]=%s rng.base=%s" % (
        specs["poles"], bp["铁心长度(mm)"], rng["铁心长度(mm)"]["base"]))
    assert specs["poles"] == 4 and bp["铁心长度(mm)"] == 185.0
    assert rng["铁心长度(mm)"]["base"] == 185.0

    # 2) 截断 JSON
    trunc = '{"overall":"revise","confidence":0.7,"feedback_list":[{"field":"poles","new_value":8,"reason":"测试截断'
    r2 = parse_review_feedback(trunc)
    print("\n[2] 截断解析等级=%s 项数=%d" % (r2["level"], len(r2["feedback"]["feedback_list"])))
    assert r2["ok"] and r2["feedback"]["feedback_list"][0]["new_value"] == 8

    # 3) 无 JSON → 正则兜底
    comment = "极数改为4极, 铁心长度调整为185mm, 定子外径改为270, 目标效率不低于96%, 改用永磁同步"
    r3 = resolve_feedback("这是一段没有 JSON 的评审文字", comment, specs, bp)
    print("\n[3] source=%s 项数=%d" % (r3["source"], len(r3["feedback"]["feedback_list"])))
    print(feedback_summary_text(r3["feedback"]))
    assert r3["source"] == "regex" and len(r3["feedback"]["feedback_list"]) >= 4

    # 4) 越界值被拒
    r4 = parse_review_feedback('{"overall":"revise","confidence":120,'
                               '"feedback_list":[{"field":"poles","new_value":7,"reason":"奇数极"},'
                               '{"field":"air_gap","new_value":99,"reason":"超限"}]}')
    print("\n[4] confidence 归一=%s 合法项=%d 丢弃项=%d" % (
        r4["feedback"]["confidence"], len(r4["feedback"]["feedback_list"]),
        len(r4["feedback"]["unrecognized"])))
    assert r4["feedback"]["confidence"] == 1.0
    assert len(r4["feedback"]["feedback_list"]) == 0
    assert len(r4["feedback"]["unrecognized"]) == 2

    # 5) 需人工
    r5 = resolve_feedback("", "整体看着不太行", {}, {})
    print("\n[5] source=%s need_manual=%s" % (r5["source"], r5["need_manual"]))
    assert r5["need_manual"] is True

    print("\n" + "=" * 60)
    print("全部自测通过 (5/5)")
    print("=" * 60)
