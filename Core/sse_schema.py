# -*- coding: utf-8 -*-
"""
SSE 事件契约 schema 常量模块 (P5)
================================

定义 AI 团队工作流 SSE 事件流的「字段名 + 类型 + 必填 + 默认值 + 说明」,
供 **前后端字段一致性校验** 与 **契约单测** 使用, 确保三端字段对齐:

  1. 生产者  : ``Agents/orchestrator.py`` 的 ``run_streaming`` / ``_push_*``
  2. 转换层  : ``app/app.py`` 的 ``generate()`` SSE 包装
  3. 消费者  : ``MotorAI_Client_Dev/src/ui/ai_team_panel.py`` 的 ``_handle_sse``

事件类型 (对外)
---------------
  start                  工作流启动
  step_start             节点开始
  progress               子步骤进度 (queued→progress / cancel 均复用此类型)
  step_done              节点完成 (各 step 特有字段见 STEP_FIELDS)
  step_error             节点错误
  human_input_required   需要人工输入
  fatal                  致命错误
  done                   工作流完成
  human_approval_skipped 人工确认被跳过 (客户端内部合成, 后端不发)

内部事件 (不对外, 由 app.py 本地消费)
-------------------------------------
  _timing               计时汇总, app.py 接收后填入最终 done 事件

节点名 (step 取值)
------------------
  load_case_motor  parse_requirement  hc_requirement
  generate_base_params  hc_base_param  generate_designs  hc_design
  review_solutions  convergence_review  decision_gate  generate_report  hc_report
  human_approval
  queue / cancel (progress 的 step 特殊取值)

用法
----
  from Core.sse_schema import validate_sse_event, normalize_sse_event, ALL_KNOWN_FIELDS

  ok, errors = validate_sse_event(event)          # (bool, [str])
  event2    = normalize_sse_event(event)          # 填充默认值后的副本

设计原则
--------
  * 契约是「宽松但可校验」的: step_done 的 step 特有字段大多为可选 (成功/失败分支不同),
    但 ``type`` / ``step`` / ``status`` 为 step_done 的强制字段。
  * 任何在 schema 中未登记的字段都视为「非法」, 由契约单测在 CI 中拦截,
    防止前后端悄悄漂移。
  * 所有字段取值以「转换层 app.py 实际发射的内容」为准 (orchestrator 为源头,
    app.py 为对外契约边界)。

版本: SSE_SCHEMA_VERSION = "1.0.0"
"""
from __future__ import annotations

import numbers
from typing import Any, Dict, List, Optional, Tuple

SSE_SCHEMA_VERSION = "1.0.0"

# ----------------------------- 类型别名 -----------------------------
# 字段 spec 形如: {"allowed": [str, int, ...], "required": bool, "default": Any, "desc": str}
# 这里用 python 类型对象表示允许的类型。
_STR = str
_INT = int
_FLOAT = float
_BOOL = bool
_DICT = dict
_LIST = list
_NONE = type(None)
_ANY = object


def _f(allowed, *, required: bool = False, default: Any = None,
       allow_none: bool = False, desc: str = "") -> Dict[str, Any]:
    """构造字段 spec。"""
    if not isinstance(allowed, (list, tuple)):
        allowed = [allowed]
    if allow_none and _NONE not in allowed:
        allowed = list(allowed) + [_NONE]
    return {
        "allowed": list(allowed),
        "required": required,
        "default": default,
        "allow_none": allow_none,
        "desc": desc,
    }


# ----------------------------- 枚举常量 -----------------------------
EVENT_TYPES: Tuple[str, ...] = (
    "start", "step_start", "progress", "step_done", "step_error",
    "human_input_required", "fatal", "done", "human_approval_skipped",
)

INTERNAL_EVENT_TYPES: Tuple[str, ...] = ("_timing",)

STEP_NAMES: Tuple[str, ...] = (
    "load_case_motor", "parse_requirement", "hc_requirement",
    "generate_base_params", "hc_base_param", "generate_designs", "hc_design",
    "review_solutions", "convergence_review", "decision_gate",
    "generate_report", "hc_report", "human_approval",
    "queue", "cancel",
)

# human_input_required 的合法操作 (与 orchestrator._push_human_input_required /
# run_streaming human_approval 分支保持一致, 见 P5 不一致项 #4 修复)
HUMAN_INPUT_OPTIONS: Tuple[str, ...] = ("approve", "reject", "override")


# ----------------------------- 公共字段 -----------------------------
_COMMON = {
    "type": _f(_STR, required=True, desc="事件类型"),
}

_META_STEP = {
    "step": _f(_STR, required=True, desc="节点名 / 进度归属步骤"),
}


# ----------------------------- step_done 各 step 特有字段 -----------------------------
STEP_FIELDS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "load_case_motor": {
        "case_motor_ref": _f(_DICT, default={}, desc="案例电机引用对象"),
        "pre_specs": _f(_DICT, default={}, desc="预填充规格参数"),
    },
    "parse_requirement": {
        "specs": _f(_DICT, default={}, desc="解析后的需求规格"),
        "confidence": _f((_INT, _FLOAT), default=0, desc="解析置信度 0~1"),
        "project_id": _f(_STR, default="", desc="项目 ID"),
        "pipeline_steps": _f(_LIST, default=[], desc="8 步流水线过程交付物"),
        "_2_1_data": _f(_DICT, default={}, desc="2.1 预处理数据快照"),
        "param_sources": _f(_DICT, default={}, desc="参数来源标注 (正则/物理/LLM)"),
    },
    "generate_base_params": {
        "base_params": _f(_DICT, default={}, desc="基准参数"),
        "base_param_ranges": _f(_DICT, default={}, desc="基准参数可变范围"),
        "ref_motors": _f(_LIST, default=[], desc="参考电机列表"),
        "scenario_summary": _f(_DICT, default={}, desc="场景摘要"),
    },
    "design_ideation": {
        "ideation_result": _f(_STR, default="", desc="构思叙述文本"),
        "design_hints": _f(_DICT, default={}, desc="结构化约束 (suggested_constraints/notes)"),
        "ideation_input": _f(_DICT, default={}, desc="构思输入快照"),
    },
    "generate_designs": {
        "results_count": _f(_INT, default=0, desc="候选方案总数"),
        "top_results": _f(_LIST, default=[], desc="Top5 候选方案"),
        "matched_templates": _f(_LIST, default=[], desc="匹配模板"),
        "param_matrix": _f(_LIST, default=[], desc="参数矩阵"),
        "results": _f(_LIST, default=[], desc="候选方案全集 (客户端防御性读取, 常态由 top_results 提供)"),
        "review_feedback_result": _f(_DICT, default={}, desc="评审反馈结果"),
        "scenario_reidentified": _f(_BOOL, default=False, desc="场景是否重识别"),
        "scenario_info": _f(_DICT, default={}, desc="场景信息"),
        "param_matrix_meta": _f(_DICT, default={}, desc="参数矩阵元信息 (含 actual_count)"),
        "diversity_metrics": _f(_DICT, default={}, desc="多样性度量"),
        "solve_summary": _f(_DICT, default={}, desc="求解摘要 (RMxprt/Motor-CAD 调用统计)"),
    },
    "review_solutions": {
        "review": _f(_STR, default="", desc="评审/支持报告"),
        "recommendation": _f(_STR, default="", desc="推荐结论"),
        "top_solutions": _f(_LIST, default=[], desc="Top 方案"),
        "errors": _f(_LIST, default=[], desc="错误列表 (客户端防御性读取, 常态由 step_error.type 携带 error)"),
    },
    "convergence_review": {
        "con_report": _f(_STR, default="", desc="反对/收敛报告"),
        "convergence_findings": _f(_DICT, default={}, desc="收敛发现"),
    },
    "decision_gate": {
        "pro_report": _f(_STR, default="", desc="支持报告"),
        "con_report": _f(_STR, default="", desc="反对报告"),
        "ready_for_confirm": _f(_BOOL, default=False, desc="是否可进入方案终选确认"),
    },
    "generate_report": {
        "report": _f(_STR, default="", desc="最终报告正文"),
        "report_filepath": _f(_STR, default="", desc="报告落盘路径"),
        "report_filename": _f(_STR, default="", desc="报告文件名"),
    },
    # human_approval / hc_* 节点: 仅有通用字段 + 可能存在的 decision
    "human_approval": {
        "decision": _f(_STR, default="", desc="人工决策结果 (approve/reject/override)"),
    },
    "hc_requirement": {},
    "hc_base_param": {},
    "hc_design": {},
    "hc_report": {},
}


# ----------------------------- 事件 schema 主体 -----------------------------
EVENT_SCHEMA: Dict[str, Dict[str, Dict[str, Any]]] = {
    "start": {
        **_COMMON,
        "message": _f(_STR, default="", desc="启动消息"),
        "model": _f(_STR, default="", desc="当前使用模型名"),
        "steps": _f(_LIST, default=[], desc="步骤序列"),
        "quick_solve_count": _f(_INT, default=0, desc="快速求解数量"),
        "fine_solve_count": _f(_INT, default=0, desc="精细求解数量"),
        "case_motor_id": _f((_STR, _NONE), default="", allow_none=True, desc="案例电机 ID"),
    },
    "step_start": {
        **_COMMON,
        **_META_STEP,
        "message": _f(_STR, default="", desc="节点开始消息"),
    },
    "progress": {
        **_COMMON,
        **_META_STEP,
        "message": _f(_STR, default="", desc="进度消息"),
        "level": _f(_STR, default="info", desc="级别 info/warn/error"),
        "timestamp": _f(_STR, default="", desc="HH:MM:SS 时间戳"),
    },
    "step_done": {
        **_COMMON,
        **_META_STEP,
        # status 由 orchestrator.STEP_STATUS_MAP 注入, 是 step_done 的强制契约字段
        "status": _f(_STR, required=True, default="", desc="节点状态 (来自 STEP_STATUS_MAP)"),
    },
    "step_error": {
        **_COMMON,
        **_META_STEP,
        "error": _f(_STR, required=True, desc="错误信息"),
    },
    "human_input_required": {
        **_COMMON,
        **_META_STEP,
        "checkpoint_id": _f(_STR, default="", desc="检查点 ID"),
        "title": _f(_STR, default="人工确认", desc="弹窗标题"),
        "description": _f(_STR, default="", desc="弹窗描述"),
        "context": _f(_DICT, default={}, desc="检查点上下文 (供 UI 渲染)"),
        "timeout_seconds": _f(_INT, default=86400, desc="超时秒数"),
        "default_action": _f(_STR, default="auto_first", desc="默认动作"),
        "options": _f(_LIST, default=list(HUMAN_INPUT_OPTIONS), desc="可选操作集合"),
        "cost_summary": _f(_DICT, default={}, desc="成本摘要 (可选, 客户端人工确认弹窗展示用)"),
    },
    "fatal": {
        **_COMMON,
        "error": _f(_STR, required=True, desc="致命错误详情"),
    },
    "done": {
        **_COMMON,
        "message": _f(_STR, default="", desc="完成消息"),
        "timing": _f(_DICT, default={}, desc="流程耗时统计 (大模型时间/token + 人工耗时)"),
    },
    # 客户端内部合成事件 (后端不发), 仅作为契约登记, 防止 client 端测试误报未知字段
    "human_approval_skipped": {
        **_COMMON,
        "message": _f(_STR, default="", desc="跳过说明"),
    },
}


# ----------------------------- 全量已知字段集合 (供契约单测) -----------------------------
def _collect_known_fields() -> set:
    known = set()
    for et in EVENT_TYPES + INTERNAL_EVENT_TYPES:
        if et in EVENT_SCHEMA:
            known.update(EVENT_SCHEMA[et].keys())
        if et == "step_done":
            for step, fields in STEP_FIELDS.items():
                known.update(fields.keys())
    known.update(EVENT_TYPES)
    known.update(INTERNAL_EVENT_TYPES)
    known.update(STEP_NAMES)
    return known


ALL_KNOWN_FIELDS: set = _collect_known_fields()


# ----------------------------- 类型匹配 -----------------------------
def _matches_type(value: Any, allowed: List[type]) -> bool:
    for t in allowed:
        if t is _ANY:
            return True
        if t is _NONE:
            if value is None:
                return True
            continue
        if isinstance(value, t):
            return True
        # int 兼容 float 期望 (5 视为合法 float)
        if t is _FLOAT and isinstance(value, _INT) and not isinstance(value, bool):
            return True
    return False


# ----------------------------- 有效 spec 合成 -----------------------------
def get_effective_spec(event_type: str, step: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """返回某事件类型的有效字段 spec (step_done 会合并该 step 的特有字段)。"""
    base = dict(EVENT_SCHEMA.get(event_type, {}))
    if event_type == "step_done" and step:
        base.update(STEP_FIELDS.get(step, {}))
    return base


# ----------------------------- 校验 -----------------------------
def validate_sse_event(event: Dict[str, Any], *, strict: bool = True
                       ) -> Tuple[bool, List[str]]:
    """校验单个 SSE 事件是否符合契约。

    返回 (ok, errors)。errors 为空表示通过。

    :param strict: True 时, 出现未知字段或类型不符即报错;
                   False 时, 仅校验必填字段 (宽松模式, 用于运行态宽松接受)。
    """
    if not isinstance(event, dict):
        return False, ["event must be a dict"]
    et = event.get("type")
    if et in INTERNAL_EVENT_TYPES:
        return True, []  # 内部事件 (如 _timing) 不对外校验
    if et not in EVENT_SCHEMA:
        return False, [f"unknown event type: {et!r}"]

    spec = get_effective_spec(et, event.get("step"))
    errors: List[str] = []

    # 必填字段
    for fname, fspec in spec.items():
        if fspec["required"] and fname not in event:
            errors.append(f"[{et}] missing required field '{fname}'")

    # 已提供字段的类型 / 未知字段
    for fname, fval in event.items():
        if fname == "type":
            continue
        fspec = spec.get(fname)
        if fspec is None:
            if strict:
                errors.append(f"[{et}] unknown field '{fname}'")
            continue
        if fval is None:
            if not fspec.get("allow_none", False):
                errors.append(f"[{et}] field '{fname}' is null (not allowed)")
            continue
        if not _matches_type(fval, fspec["allowed"]):
            allowed_names = "/".join(getattr(t, "__name__", str(t)) for t in fspec["allowed"])
            errors.append(
                f"[{et}] field '{fname}' type mismatch: expected {allowed_names}, "
                f"got {type(fval).__name__}"
            )

    return (len(errors) == 0), errors


# ----------------------------- 归一化 (填充默认值) -----------------------------
def normalize_sse_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """返回填充了所有默认值的新事件 dict (不影响原对象)。"""
    et = event.get("type")
    if et not in EVENT_SCHEMA:
        return dict(event)
    spec = get_effective_spec(et, event.get("step"))
    out = dict(event)
    for fname, fspec in spec.items():
        if fname not in out:
            out[fname] = fspec["default"]
    return out


# ----------------------------- 文档输出 (供报告) -----------------------------
def describe_schema() -> str:
    """生成人类可读的契约 Markdown, 用于交付报告附录。"""
    lines = [f"# SSE 事件契约 v{SSE_SCHEMA_VERSION}", ""]
    for et in EVENT_TYPES:
        lines.append(f"## {et}")
        spec = get_effective_spec(et, None)
        if et == "step_done":
            lines.append("> step_done 字段随 step 变化, 见下方各 step 附表")
        for fname, fspec in spec.items():
            req = "必填" if fspec["required"] else "可选"
            allowed = "/".join(getattr(t, "__name__", str(t)) for t in fspec["allowed"])
            lines.append(f"- `{fname}` ({allowed}, {req}) — {fspec['desc']}")
        lines.append("")
    lines.append("## step_done 各 step 特有字段")
    for step, fields in STEP_FIELDS.items():
        if not fields:
            lines.append(f"- `{step}`: (仅通用字段)")
            continue
        for fname, fspec in fields.items():
            allowed = "/".join(getattr(t, "__name__", str(t)) for t in fspec["allowed"])
            lines.append(f"- `{step}.{fname}` ({allowed}) — {fspec['desc']}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测: 构造一个最小合法事件流并校验
    samples = [
        {"type": "start", "message": "x", "model": "qwen2.5:3b", "steps": [],
         "quick_solve_count": 3, "fine_solve_count": 1, "case_motor_id": "M1"},
        {"type": "step_start", "step": "load_case_motor", "message": "loading"},
        {"type": "progress", "step": "queue", "message": "wait", "level": "info", "timestamp": "10:00:00"},
        {"type": "step_done", "step": "load_case_motor", "status": "done",
         "case_motor_ref": {}, "pre_specs": {}},
        {"type": "step_done", "step": "parse_requirement", "status": "done",
         "specs": {}, "confidence": 0.9, "project_id": "P1", "pipeline_steps": [],
         "_2_1_data": {}, "param_sources": {}},
        {"type": "human_input_required", "step": "hc_requirement", "checkpoint_id": "c1",
         "title": "t", "description": "d", "context": {}, "timeout_seconds": 300,
         "default_action": "auto_first", "options": list(HUMAN_INPUT_OPTIONS)},
        {"type": "step_error", "step": "parse_requirement", "error": "boom"},
        {"type": "fatal", "error": "dead"},
        {"type": "done", "message": "ok", "timing": {}},
    ]
    ok_all = True
    for s in samples:
        ok, errs = validate_sse_event(s)
        print(f"[{'OK ' if ok else 'FAIL'}] {s['type']:<22} {errs if errs else ''}")
        ok_all = ok_all and ok
    # 负例: 缺必填
    bad = {"type": "step_done", "step": "x"}
    ok, errs = validate_sse_event(bad)
    print(f"[{'OK ' if not ok else 'FAIL'}] negative-missing-status: {errs}")
    # 负例: 未知字段
    bad2 = {"type": "start", "step": "x", "weird": 1}
    ok, errs = validate_sse_event(bad2)
    print(f"[{'OK ' if not ok else 'FAIL'}] negative-unknown-field: {errs}")
    print("ALL_SAMPLES_OK" if ok_all else "SAMPLES_FAILED")
