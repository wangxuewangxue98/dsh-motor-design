import sys, traceback
sys.path.insert(0, r"D:\MotorDesign")
from Core import sse_schema as S

lines = []
try:
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
         "default_action": "auto_first", "options": list(S.HUMAN_INPUT_OPTIONS)},
        {"type": "step_error", "step": "parse_requirement", "error": "boom"},
        {"type": "fatal", "error": "dead"},
        {"type": "done", "message": "ok", "timing": {}},
    ]
    ok_all = True
    for s in samples:
        ok, errs = S.validate_sse_event(s)
        lines.append(f"[{'OK ' if ok else 'FAIL'}] {s['type']:<22} {errs if errs else ''}")
        ok_all = ok_all and ok
    bad = {"type": "step_done", "step": "x"}
    ok, errs = S.validate_sse_event(bad)
    lines.append(f"[{'OK ' if not ok else 'FAIL'}] negative-missing-status: {errs}")
    bad2 = {"type": "start", "step": "x", "weird": 1}
    ok, errs = S.validate_sse_event(bad2)
    lines.append(f"[{'OK ' if not ok else 'FAIL'}] negative-unknown-field: {errs}")
    n = S.normalize_sse_event({"type": "step_done", "step": "generate_designs"})
    lines.append(f"normalize fill keys: {sorted(k for k in n if k not in ('type','step'))}")
    lines.append("ALL_SAMPLES_OK" if ok_all else "SAMPLES_FAILED")
    lines.append(f"ALL_KNOWN_FIELDS count = {len(S.ALL_KNOWN_FIELDS)}")
except Exception:
    lines.append("EXCEPTION:\n" + traceback.format_exc())

open(r"D:\MotorDesign\Core\sse_quickcheck.log", "w", encoding="utf-8").write("\n".join(lines))
