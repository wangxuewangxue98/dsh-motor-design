"""P6 单元测试: 质量门禁重生前求解器可用性校验 (纯函数, 无 motor_tools 依赖)。

运行: 在 D:/MotorDesign/Core 目录下
  python test_p6_solver_shield.py
结果写入 test_p6_solver_shield.log (工具不回显 stdout)。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import quality_gate as S  # noqa: E402

_RECORDS = []


def _rec(name, ok, detail=""):
    _RECORDS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL") + " " + name + ((" :: " + detail) if detail else ""))


def _status(eff, rmxprt_avail=True, motorcad_avail=True):
    return {
        "effective_engine": eff,
        "engines": {
            "rmxprt": {"available": rmxprt_avail},
            "motorcad": {"available": motorcad_avail},
        },
    }


def _ss(level1):
    return {"level1": level1}


def run():
    # T1: enforce 关闭 -> 放行
    ok, msg = S.solver_available_for_rebirth(_status("rmxprt"), {}, enforce=False)
    _rec("T1_enforce_off", ok and "禁用" in msg, msg)

    # T2: status 为空 -> 保守放行
    ok, msg = S.solver_available_for_rebirth({}, {}, enforce=True)
    _rec("T2_empty_status_pass", ok, msg)

    # T3: 仅模拟兜底 (无任何真实求解器) -> 不可用
    ok, msg = S.solver_available_for_rebirth(_status("simulation"), {}, enforce=True)
    _rec("T3_sim_only_blocked", (not ok) and "模拟兜底" in msg, msg)

    # T4: 配置 RMxprt 但不可用 -> 不可用
    ok, msg = S.solver_available_for_rebirth(_status("rmxprt", rmxprt_avail=False), {}, enforce=True)
    _rec("T4_rmxprt_unavail_blocked", (not ok) and "PyAEDT/AEDT" in msg, msg)

    # T5: RMxprt 可用 + 无任何求解摘要 -> 放行 (保守)
    ok, msg = S.solver_available_for_rebirth(_status("rmxprt", rmxprt_avail=True), {}, enforce=True)
    _rec("T5_rmxprt_avail_no_summary_pass", ok, msg)

    # T6: 上一轮 L1 整批降级为模拟 + RMxprt 仍不可用 -> 不可用 (超时对账)
    # 注: 该场景同时命中情形2(rmxprt不可用)与情形3(整批模拟), 任一均判定不可用;
    #      故只校验最终判定为 blocked (not ok), 不限定具体 reason 文案。
    ok, msg = S.solver_available_for_rebirth(
        _status("rmxprt", rmxprt_avail=False),
        _ss({"rmxprt_count": 0, "sim_count": 5}), enforce=True)
    _rec("T6_last_batch_all_sim_blocked", (not ok), msg)

    # T7: RMxprt 可用 + L1 混合结果 (有真实 RMxprt 计数) -> 放行 (允许重试一次)
    ok, msg = S.solver_available_for_rebirth(
        _status("rmxprt", rmxprt_avail=True),
        _ss({"rmxprt_count": 3, "sim_count": 2}), enforce=True)
    _rec("T7_mixed_real_pass", ok, msg)

    # T8: RMxprt 可用但 L1 整批模拟 (罕见半死) -> 仍放行 (保守, max_redo 兜底)
    ok, msg = S.solver_available_for_rebirth(
        _status("rmxprt", rmxprt_avail=True),
        _ss({"rmxprt_count": 0, "sim_count": 5}), enforce=True)
    _rec("T8_rmxprt_avail_all_sim_pass_conservative", ok, msg)

    # T9: Motor-CAD 可用、RMxprt 不可用、L1 有 motorcad 真实结果 (sim_count==0)
    ok, msg = S.solver_available_for_rebirth(
        _status("motorcad", rmxprt_avail=False, motorcad_avail=True),
        _ss({"rmxprt_count": 0, "sim_count": 0}), enforce=True)
    _rec("T9_motorcad_usable_pass", ok, msg)

    passed = sum(1 for _, ok, _ in _RECORDS if ok)
    total = len(_RECORDS)
    print("\n==== P6 SOLVER-SHIELD TEST SUMMARY ====")
    print("PASSED=%d/%d" % (passed, total))
    print("OVERALL: " + ("PASS" if passed == total else "FAIL"))
    for name, ok, detail in _RECORDS:
        print(("  [OK] " if ok else "  [XX] ") + name + ((" :: " + detail) if detail else ""))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        print("OVERALL: ERROR")
