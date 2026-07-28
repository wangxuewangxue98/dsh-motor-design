"""
LangGraph 多智能体编排器 - 双轨辩证制衡体系
放大系统 (评审推荐师) + 收敛系统 (逆向/现实/沙盘/魔鬼) 并行, 汇入决策门

工作流 (v1.03.01):
  START → parse_requirement → generate_designs
    → [fan-out]
        ├→ review_solutions (放大·支持报告)
        └→ convergence_review (收敛·反对报告, 内含4个收敛agent)
    → decision_gate (双报告必读, ready_for_confirm)
    → generate_report → END

  无结果时: generate_designs → generate_report (跳过辩证)

v1.02.02 修复: 并行节点不再返回 step/status (避免 InvalidUpdateError)
  - step 由 run_streaming 从 node_name 设置
  - errors 改为 Annotated[List, operator.add] 允许并行追加

v1.02.03 增强: 实时进度推送 (后台线程 + 队列)
  - step_start 事件: 节点开始前推送
  - progress 事件: 节点执行中通过回调推送子步骤进度
  - batch_solve 逐条推送求解进度 (3/10...)
"""
import sys
import os
import time
import json
import queue
import threading
import operator
from typing import TypedDict, Optional, List, Annotated
from datetime import datetime

sys.path.insert(0, r"D:\MotorDesign\Agents")
sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from requirement_agent import RequirementAgent
from design_agent import DesignAgent
from review_agent import ReviewAgent
from report_agent import ReportAgent
from reverse_reviewer import ReverseReviewer
from reality_checker import RealityChecker
from sandbox_simulator import SandboxSimulator
from devils_advocate import DevilsAdvocate
import audit_logger

# motor_tools 直接导入 (用于 generate_designs 节点的进度回调)
from motor_tools import generate_param_matrix, batch_solve, get_last_summary
from evolution_engine import find_matching_templates
from operation_flywheel import (
    record_step_metric, record_project_summary, run_all_evolution,
    record_ui_interaction, record_report_metric,
)
from cost_tracker import TaskCostTracker
import human_checkpoint as hc
import json as _json


# v2.05.02: 加载飞轮AI路由规则
def _get_flywheel_routing() -> dict:
    """从飞轮加载AI路由规则, 自动决定本地/云端"""
    try:
        rules_path = r"D:\MotorDesign\Knowledge\evolution_rules.json"
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
                return data.get("routing", {})
    except Exception:
        pass
    return {}


def _apply_routing_rules(use_cloud_review: bool, use_cloud_report: bool) -> tuple:
    """v2.05.02: 根据飞轮路由规则自动调整云端/本地标志"""
    routing = _get_flywheel_routing()
    if not routing:
        return use_cloud_review, use_cloud_report

    # 评审步骤路由
    review_rule = routing.get("review_solutions", {})
    if review_rule.get("prefer") == "local" and use_cloud_review:
        print(f"[orchestrator] 飞轮路由: 评审步骤切换为本地 ({review_rule.get('reason', '')})")
        use_cloud_review = False

    # 报告步骤路由
    report_rule = routing.get("generate_report", {})
    if report_rule.get("prefer") == "local" and use_cloud_report:
        print(f"[orchestrator] 飞轮路由: 报告步骤切换为本地 ({report_rule.get('reason', '')})")
        use_cloud_report = False

    return use_cloud_review, use_cloud_report


# 节点名 → 状态名映射 (供 run/run_streaming 统一设置)
STEP_STATUS_MAP = {
    "parse_requirement": "parsed",
    "generate_designs": "solved",
    "review_solutions": "reviewed",
    "convergence_review": "converged",
    "decision_gate": "ready",
    "human_approval": "approved",
    "generate_report": "completed",
}

# 节点中文显示名
STEP_DISPLAY_NAMES = {
    "parse_requirement": "需求分析师",
    "generate_designs": "方案生成师",
    "review_solutions": "评审推荐师",
    "convergence_review": "收敛阵营",
    "decision_gate": "决策门",
    "human_approval": "人工确认",
    "generate_report": "报告撰写员",
}


def _estimate_tokens(text) -> int:
    """粗略估算token数 (中英文混合约4字符/token)"""
    if not text:
        return 0
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    elif not isinstance(text, str):
        text = str(text)
    return max(1, len(text) // 4)


class WorkflowState(TypedDict):
    """工作流状态 (v2.04 物理内核 + 运营飞轮 + v2.05.03 人机协作)"""
    raw_text: str
    specs: dict
    confidence: float
    results: List[dict]
    top_solutions: List[dict]
    # v2.04: 物理内核
    scenario_info: dict             # 场景识别结果 (catalog/analogy)
    layered_specs: dict             # L1/L2/L3 分层规格
    # 放大轨
    review: str                    # 评审推荐意见 (支持报告)
    recommendation: str
    # 收敛轨
    con_report: str                # 汇总反对报告
    convergence_findings: dict     # 各收敛agent原始发现
    # 决策门
    pro_report: str                # 放大轨最终报告
    ready_for_confirm: bool        # 双报告齐备, 可交付用户确认
    checkpoint_id: str             # v2.05.03: 人工检查点ID
    human_decision: str            # v2.05.03: 人工决策结果
    human_comment: str             # v2.05.03: 人工批注
    # 报告
    report: str
    report_filepath: str
    report_filename: str
    matched_templates: list
    project_id: str
    # errors 使用 Annotated 允许并行节点追加 (而非覆盖)
    errors: Annotated[List[str], operator.add]
    solve_count: int
    use_cloud_review: bool
    use_cloud_report: bool
    step: str
    status: str
    timestamp: str


class MotorAIOrchestrator:
    """双轨辩证制衡编排器"""

    def __init__(self):
        # 放大阵营
        self.req_agent = RequirementAgent()
        self.design_agent = DesignAgent()
        self.review_agent = ReviewAgent()
        self.report_agent = ReportAgent()
        # 收敛阵营
        self.reverse_reviewer = ReverseReviewer()
        self.reality_checker = RealityChecker()
        self.sandbox_simulator = SandboxSimulator()
        self.devils_advocate = DevilsAdvocate()

        self._current_project_id = None
        self.graph = self._build_graph()
        self.checkpointer = MemorySaver()
        self.compiled = self.graph.compile(checkpointer=self.checkpointer)

        # 实时进度队列 (线程安全)
        self._progress_queue = queue.Queue()

        # v2.05.03: 成本追踪器 (每个工作流实例重新绑定project_id)
        self.cost_tracker = TaskCostTracker()

    def _build_graph(self):
        graph = StateGraph(WorkflowState)

        graph.add_node("parse_requirement", self._node_parse_requirement)
        graph.add_node("generate_designs", self._node_generate_designs)
        graph.add_node("review_solutions", self._node_review_solutions)
        graph.add_node("convergence_review", self._node_convergence_review)
        graph.add_node("decision_gate", self._node_decision_gate)
        graph.add_node("human_approval", self._node_human_approval)
        graph.add_node("generate_report", self._node_generate_report)

        graph.add_edge(START, "parse_requirement")
        graph.add_edge("parse_requirement", "generate_designs")

        # 分叉: 有结果则双轨并行, 无结果跳过
        graph.add_conditional_edges(
            "generate_designs",
            self._after_designs,
            {
                "review_pro": "review_solutions",
                "review_con": "convergence_review",
                "skip": "generate_report",
            }
        )

        # 双轨汇入决策门
        graph.add_edge("review_solutions", "decision_gate")
        graph.add_edge("convergence_review", "decision_gate")
        # v2.05.03: 决策门之后进入人工确认 (双报告齐备时) 或直达报告
        graph.add_conditional_edges(
            "decision_gate",
            self._after_decision_gate,
            {
                "need_human": "human_approval",
                "skip": "generate_report",
            }
        )
        graph.add_edge("human_approval", "generate_report")
        graph.add_edge("generate_report", END)

        return graph

    def _after_designs(self, state: WorkflowState) -> list:
        """分叉路由: 有结果则放大+收敛并行, 否则跳过"""
        if state.get("results") and len(state["results"]) > 0:
            return ["review_pro", "review_con"]
        return ["skip"]

    def _after_decision_gate(self, state: WorkflowState) -> str:
        """v2.05.03: 决策门后路由, 双报告齐备则进入人工确认"""
        if state.get("ready_for_confirm") and state.get("results"):
            return "need_human"
        return "skip"

    def _set_project_id(self, project_id: str):
        for agent in [self.req_agent, self.design_agent, self.review_agent,
                      self.report_agent, self.reverse_reviewer,
                      self.reality_checker, self.sandbox_simulator,
                      self.devils_advocate]:
            agent.project_id = project_id
        # v2.05.03: 同步成本追踪器project_id
        self.cost_tracker.project_id = project_id

    # ==================== 进度回调 ====================
    def _push_progress(self, step: str, message: str, level: str = "info"):
        """推送子步骤进度到队列 (供 run_streaming 的消费者读取)"""
        self._progress_queue.put({
            "type": "progress",
            "step": step,
            "message": message,
            "level": level,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })

    # ==================== 放大轨 ====================
    def _node_parse_requirement(self, state: WorkflowState) -> dict:
        self._push_progress("parse_requirement", "正在解析客户需求文本...")
        project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self._current_project_id = project_id
        self._set_project_id(project_id)

        _start_ts = time.time()
        result = self.req_agent.safe_run(raw_text=state["raw_text"])
        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            self._push_progress("parse_requirement", f"需求解析失败: {result['error']}", "error")
            record_step_metric(project_id, "parse_requirement", "RequirementAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=result["error"][:200])
            return {"errors": [result["error"]]}

        # v2.04: 提取场景识别和分层规格
        scenario_info = result.get("scenario_info", {})
        layered_specs = result.get("layered_specs", {})
        scenario = scenario_info.get("scenario", "catalog")
        torque = scenario_info.get("corrected_torque_nm", "?")

        self._push_progress("parse_requirement",
                            f"需求解析完成: 类型={result['specs'].get('motor_type', '异步电动机')}, "
                            f"功率={result['specs'].get('power_kw', '?')}kW, "
                            f"电压={result['specs'].get('voltage_v', '?')}V, "
                            f"转速={result['specs'].get('speed_rpm', '?')}rpm, "
                            f"转矩={torque}Nm, 选型模式={scenario}")

        # 记录运营指标
        record_step_metric(project_id, "parse_requirement", "RequirementAgent",
                           duration_ms=_duration_ms, quality_score=result.get("confidence", 0) * 100,
                           quality_issues=json.dumps(result.get("extraction_notes", []),
                                                     ensure_ascii=False)[:200])

        # v2.05.03: 记录AI成本 (本地)
        tokens_in = _estimate_tokens(state["raw_text"])
        tokens_out = _estimate_tokens(result)
        self.cost_tracker.project_id = project_id
        self.cost_tracker.record_ai_step("parse_requirement", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type="local",
                                         detail={"confidence": result.get("confidence", 0)})

        return {"specs": result["specs"], "confidence": result["confidence"],
                "scenario_info": scenario_info,
                "layered_specs": layered_specs,
                "project_id": project_id,
                "timestamp": datetime.now().isoformat()}

    def _node_generate_designs(self, state: WorkflowState) -> dict:
        count = state.get("solve_count", 10)
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")
        scenario_info = state.get("scenario_info", {})

        # v2.04: 物理内核进度提示
        scenario = scenario_info.get("scenario", "catalog")
        torque = scenario_info.get("corrected_torque_nm", "?")
        poles = scenario_info.get("poles", "?")
        self._push_progress("generate_designs",
                            f"开始生成参数矩阵 (目标 {count} 组方案)...")
        self._push_progress("generate_designs",
                            f"物理内核: 选型模式={scenario}, 目标转矩={torque}Nm, 极数={poles}极")

        _start_ts = time.time()

        # 1. 查找匹配的技能模板
        self._push_progress("generate_designs", "查找匹配的技能模板...")
        matched = find_matching_templates(specs, top_n=3)
        if matched:
            self._push_progress("generate_designs",
                                f"找到 {len(matched)} 个匹配模板: {', '.join(m.get('name', '?') for m in matched[:2])}")

        # 2. 生成参数矩阵 (v2.04: 使用物理内核聚焦扫描)
        self._push_progress("generate_designs", "D²L 物理内核聚焦扫描...")
        params_list = generate_param_matrix(specs, count)
        self._push_progress("generate_designs",
                            f"参数矩阵生成完成: {len(params_list)} 组方案, 开始批量求解...")

        # 3. 批量求解 (带进度回调)
        _first_fallback_reported = False
        def _batch_progress(idx, total, params, result):
            nonlocal _first_fallback_reported
            mode_tag = "[MC]" if result.get("solve_mode") == "motorcad" else "[SIM]"
            eff = result.get("efficiency", 0)
            temp = result.get("max_temp", 0)
            self._push_progress("generate_designs",
                                f"求解进度: {idx}/{total} {mode_tag} "
                                f"OD={params.get('stator_od', '?')}mm "
                                f"Poles={params.get('poles', '?')} "
                                f"效率={eff:.1f}% 温升={temp:.0f}K")
            # 首次降级时推送告警
            if not _first_fallback_reported and result.get("fallback_reason"):
                _first_fallback_reported = True
                self._push_progress("generate_designs",
                                    f"Motor-CAD 降级: {result['fallback_reason'][:100]}",
                                    "warning")

        results = batch_solve(params_list, progress_cb=_batch_progress)

        # v2.03.02: 检测 Motor-CAD 授权错误并推送告警
        _summary = get_last_summary()
        if _summary.get("licence_error"):
            self._push_progress("generate_designs",
                                f"[授权错误] {_summary['licence_error']}",
                                "error")
            self._push_progress("generate_designs",
                                f"已自动降级为模拟模式完成 {len(results)} 组方案 "
                                f"(MC={_summary.get('motorcad', 0)}, "
                                f"SIM={_summary.get('simulation', 0)})",
                                "warning")
        elif _summary.get("fallback_reason"):
            self._push_progress("generate_designs",
                                f"Motor-CAD 降级提示: {_summary['fallback_reason'][:120]}",
                                "warning")

        # 4. 按效率排序
        results.sort(key=lambda x: x.get("efficiency", 0), reverse=True)

        if results:
            best = results[0]
            self._push_progress("generate_designs",
                                f"批量求解完成: {len(results)} 组方案, "
                                f"最优效率={best.get('efficiency', 0):.2f}%, "
                                f"温升={best.get('max_temp', 0):.1f}K",
                                "success")

        # v2.04: 记录运营指标
        _duration_ms = int((time.time() - _start_ts) * 1000)
        _mc_count = _summary.get("motorcad", 0)
        _sim_count = _summary.get("simulation", 0)
        _quality = 80 if results else 0
        if results:
            eff_avg = sum(r.get("efficiency", 0) for r in results) / len(results)
            _quality = min(100, eff_avg)
        record_step_metric(project_id, "generate_designs", "DesignAgent",
                           duration_ms=_duration_ms,
                           quality_score=_quality,
                           quality_issues=f"MC={_mc_count}, SIM={_sim_count}, "
                                          f"results={len(results)}")

        # v2.05.03: 记录电脑计算耗时成本
        self.cost_tracker.project_id = project_id
        self.cost_tracker.record_compute_time("generate_designs",
                                              cpu_hours=_duration_ms / 3600000.0,
                                              detail={"mc_count": _mc_count,
                                                      "sim_count": _sim_count,
                                                      "results": len(results)})

        return {"results": results,
                "matched_templates": matched}

    def _node_review_solutions(self, state: WorkflowState) -> dict:
        """放大轨: 评审推荐师 → 支持报告"""
        self._push_progress("review_solutions", "评审推荐师开始分析方案矩阵...")
        self._push_progress("review_solutions",
                            f"输入: {len(state['results'])} 组方案, 正在调用本地 LLM 生成支持报告...")

        _start_ts = time.time()
        project_id = state.get("project_id", "")
        use_cloud = state.get("use_cloud_review", False)

        result = self.review_agent.safe_run(
            results=state["results"], specs=state["specs"], use_cloud=use_cloud)

        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            self._push_progress("review_solutions", f"评审失败: {result['error']}", "error")
            record_step_metric(project_id, "review_solutions", "ReviewAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=result["error"][:200])
            return {"errors": [result["error"]],
                    "review": "评审失败",
                    "top_solutions": state["results"][:5]}

        # v2.05.02: 记录评审步骤运营指标
        top_count = len(result.get("top_solutions", []))
        has_physics = bool(result.get("physics_analysis"))
        _quality = 85 if has_physics else 70
        record_step_metric(project_id, "review_solutions", "ReviewAgent",
                           duration_ms=_duration_ms,
                           cost_cents=50 if use_cloud else 0,
                           cost_mode="cloud" if use_cloud else "local",
                           quality_score=_quality,
                           quality_issues=f"top={top_count}, physics={'Y' if has_physics else 'N'}")

        # v2.05.03: 记录AI评审成本
        self.cost_tracker.project_id = project_id
        model_type = "cloud" if use_cloud else "local"
        tokens_in = _estimate_tokens(state.get("results", [])) + _estimate_tokens(state.get("specs", {}))
        tokens_out = _estimate_tokens(result.get("review", "")) + _estimate_tokens(result.get("recommendation", ""))
        self.cost_tracker.record_ai_step("review_solutions", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type=model_type,
                                         detail={"top_count": top_count,
                                                 "has_physics": has_physics})

        self._push_progress("review_solutions",
                            f"支持报告已生成 (推荐 {top_count} 个方案)",
                            "success")
        return {"review": result["review"],
                "top_solutions": result["top_solutions"],
                "recommendation": result["recommendation"]}

    # ==================== 收敛轨 ====================
    def _node_convergence_review(self, state: WorkflowState) -> dict:
        """收敛轨: 4个收敛agent → 反对报告"""
        results = state.get("results", [])
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")
        findings = {}
        _conv_start = time.time()

        # 1. 逆向评审师
        self._push_progress("convergence_review", "[1/4] 逆向评审师启动: 检查被丢弃方案中的异类价值...")
        _sub_ts = time.time()
        try:
            rev = self.reverse_reviewer.safe_run(results=results, specs=specs)
            findings["reverse"] = rev.get("con_report", "")
            findings["reverse_highlights"] = rev.get("discarded_highlights", [])
            self._push_progress("convergence_review", "[1/4] 逆向评审师完成")
            record_step_metric(project_id, "convergence_review", "ReverseReviewer",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=80)
        except Exception as e:
            findings["reverse"] = f"逆向评审失败: {e}"
            self._push_progress("convergence_review", f"[1/4] 逆向评审师失败: {e}", "error")
            record_step_metric(project_id, "convergence_review", "ReverseReviewer",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=0, quality_issues=str(e)[:200])

        # 2. 现实检验师
        self._push_progress("convergence_review", "[2/4] 现实检验师启动: 评估量产可行性 (TRL评级)...")
        _sub_ts = time.time()
        try:
            real = self.reality_checker.safe_run(results=results, specs=specs)
            findings["reality"] = real.get("con_report", "")
            findings["feasibility"] = real.get("feasibility", [])
            self._push_progress("convergence_review", "[2/4] 现实检验师完成")
            record_step_metric(project_id, "convergence_review", "RealityChecker",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=80)
        except Exception as e:
            findings["reality"] = f"现实检验失败: {e}"
            self._push_progress("convergence_review", f"[2/4] 现实检验师失败: {e}", "error")
            record_step_metric(project_id, "convergence_review", "RealityChecker",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=0, quality_issues=str(e)[:200])

        # 3. 沙盘推演师
        self._push_progress("convergence_review", "[3/4] 沙盘推演师启动: 蒙特卡洛1000次扰动模拟...")
        _sub_ts = time.time()
        try:
            sand = self.sandbox_simulator.safe_run(results=results, specs=specs)
            findings["sandbox"] = sand.get("con_report", "")
            findings["simulations"] = sand.get("simulations", [])
            self._push_progress("convergence_review", "[3/4] 沙盘推演师完成")
            record_step_metric(project_id, "convergence_review", "SandboxSimulator",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=85)
        except Exception as e:
            findings["sandbox"] = f"沙盘推演失败: {e}"
            self._push_progress("convergence_review", f"[3/4] 沙盘推演师失败: {e}", "error")
            record_step_metric(project_id, "convergence_review", "SandboxSimulator",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=0, quality_issues=str(e)[:200])

        # 4. 魔鬼代言人 (引用前三个的发现)
        self._push_progress("convergence_review", "[4/4] 魔鬼代言人启动: 撰写方案遗书 (假设已失败)...")
        _sub_ts = time.time()
        try:
            dev = self.devils_advocate.safe_run(
                results=results, specs=specs,
                convergence_findings={
                    "reverse": findings.get("reverse", "")[:200],
                    "reality": findings.get("reality", "")[:200],
                    "sandbox": findings.get("sandbox", "")[:200],
                })
            findings["devils_advocate"] = dev.get("con_report", "")
            findings["postmortems"] = dev.get("postmortems", [])
            self._push_progress("convergence_review", "[4/4] 魔鬼代言人完成")
            # v2.05.02: 记录魔鬼代言人指标 (用于报告结构进化触发)
            _devils_quality = 75
            if dev.get("postmortems"):
                _devils_quality = 80
            record_step_metric(project_id, "convergence_review", "DevilsAdvocate",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=_devils_quality)
        except Exception as e:
            findings["devils_advocate"] = f"魔鬼代言人失败: {e}"
            self._push_progress("convergence_review", f"[4/4] 魔鬼代言人失败: {e}", "error")
            record_step_metric(project_id, "convergence_review", "DevilsAdvocate",
                               duration_ms=int((time.time() - _sub_ts) * 1000),
                               quality_score=0, quality_issues=str(e)[:200])

        # 汇总反对报告
        self._push_progress("convergence_review", "正在汇总四剑客发现为反对报告...")
        con_report = self._assemble_con_report(findings)

        _conv_duration = int((time.time() - _conv_start) * 1000)
        self._push_progress("convergence_review", "反对报告生成完成", "success")

        # v2.05.03: 记录收敛阵营AI成本 (统一记为本地, 因四剑客默认本地运行)
        self.cost_tracker.project_id = project_id
        tokens_in = _estimate_tokens(results) + _estimate_tokens(specs)
        tokens_out = _estimate_tokens(con_report)
        self.cost_tracker.record_ai_step("convergence_review", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type="local",
                                         detail={"agents": ["reverse", "reality", "sandbox", "devils"]})

        return {"con_report": con_report,
                "convergence_findings": findings}

    def _assemble_con_report(self, findings: dict) -> str:
        """把4个收敛agent的发现汇总成一份反对报告"""
        sections = [
            ("逆向评审师 · 防止设计独断", findings.get("reverse", "")),
            ("现实检验师 · 防止好高骛远", findings.get("reality", "")),
            ("沙盘推演师 · 防止信念固执", findings.get("sandbox", "")),
            ("魔鬼代言人 · 防止一厢情愿", findings.get("devils_advocate", "")),
        ]
        parts = ["# 收敛系统 · 反对报告\n"]
        for title, body in sections:
            parts.append(f"\n## {title}\n")
            parts.append(body if body else "(无输出)")
        parts.append("\n---\n*本报告由收敛阵营自动生成, 与放大系统的支持报告对照阅读。*")
        return "\n".join(parts)

    # ==================== 决策门 ====================
    def _node_decision_gate(self, state: WorkflowState) -> dict:
        """决策门: 要求支持报告与反对报告同时齐备, 并创建人工检查点"""
        pro = state.get("review", "")
        con = state.get("con_report", "")
        ready = bool(pro and con)
        project_id = state.get("project_id", "")
        checkpoint_id = ""

        if ready and project_id:
            self._push_progress("decision_gate", "双报告齐备, 决策门已解锁", "success")
            # v2.05.03: 创建人工检查点 (设计-方案终选)
            checkpoint_id = hc.create_checkpoint(
                project_id=project_id,
                checkpoint_type="design_final_select",
                title="方案终选确认",
                description="请审阅支持报告与反对报告,确认或覆盖AI推荐的最终方案",
                context={
                    "review": pro[:1000],
                    "con_report": con[:1000],
                    "recommendation": state.get("recommendation", "")[:500],
                    "top_solutions": state.get("top_solutions", [])[:3],
                },
                timeout_seconds=hc.DEFAULT_TIMEOUTS["design_final_select"],  # 24h
                default_action="auto_first"
            )
            self._push_progress("decision_gate", f"人工检查点已创建: {checkpoint_id}", "info")
        else:
            self._push_progress("decision_gate", "报告未齐备, 决策门锁定", "warn")

        return {"pro_report": pro,
                "ready_for_confirm": ready,
                "checkpoint_id": checkpoint_id}

    # ==================== 人工确认 ====================
    def _node_human_approval(self, state: WorkflowState) -> dict:
        """v2.05.03: 人工确认节点 - 阻塞等待用户决策或超时默认"""
        checkpoint_id = state.get("checkpoint_id", "")
        project_id = state.get("project_id", "")
        if not checkpoint_id:
            return {"human_decision": "auto_skip", "human_comment": "无检查点"}

        self._push_progress("human_approval", "等待人工确认 (方案终选)...", "info")
        # v2.05.03: 开始记录人工等待时间
        self.cost_tracker.project_id = project_id
        human_event = self.cost_tracker.start_human_step("human_approval",
                                                          detail={"checkpoint_id": checkpoint_id})

        def _progress_cb(remaining: int):
            # 每60秒推送一次倒计时
            if remaining % 60 == 0:
                self._push_progress("human_approval",
                                    f"人工确认倒计时: 剩余 {remaining//60} 分钟", "info")

        result = hc.wait_for_decision(checkpoint_id, poll_interval=2.0,
                                      on_progress=_progress_cb)

        decision = result.get("decision", "auto_first")
        comment = result.get("comment", "")
        is_timeout = result.get("timeout", False)

        # 结束人工等待成本记录
        self.cost_tracker.end_human_step(human_event, decision=decision)

        if is_timeout:
            self._push_progress("human_approval",
                                f"人工确认超时, 默认执行: {decision}", "warn")
        else:
            self._push_progress("human_approval",
                                f"收到人工决策: {decision}", "success")

        return {"human_decision": decision,
                "human_comment": comment,
                "ready_for_confirm": True}

    # ==================== 报告 ====================
    def _node_generate_report(self, state: WorkflowState) -> dict:
        self._push_progress("generate_report", "报告撰写员开始生成正式设计报告...")

        use_cloud = state.get("use_cloud_report", False)
        review = state.get("review", "无评审意见")
        results = state.get("results", [])
        specs = state.get("specs", {})
        project_id = state.get("project_id", "")

        _start_ts = time.time()
        self._push_progress("generate_report", "调用本地 LLM 生成报告 (可能需要 30-60 秒)...")

        result = self.report_agent.safe_run(
            specs=specs, results=results, review=review, use_cloud=use_cloud)

        _duration_ms = int((time.time() - _start_ts) * 1000)

        if "error" in result:
            self._push_progress("generate_report", f"报告生成失败: {result['error']}", "error")
            record_step_metric(project_id, "generate_report", "ReportAgent",
                               duration_ms=_duration_ms, quality_score=0,
                               quality_issues=result["error"][:200])
            return {"errors": [result["error"]]}

        self._push_progress("generate_report",
                            f"报告已生成: {result.get('filename', '?')}", "success")

        # v2.04: 记录运营指标 + 项目汇总
        record_step_metric(project_id, "generate_report", "ReportAgent",
                           duration_ms=_duration_ms,
                           cost_cents=50 if use_cloud else 0,
                           cost_mode="cloud" if use_cloud else "local",
                           quality_score=90)

        # v2.05.03: 记录报告生成AI成本
        self.cost_tracker.project_id = project_id
        model_type = "cloud" if use_cloud else "local"
        tokens_in = _estimate_tokens(specs) + _estimate_tokens(results) + _estimate_tokens(review)
        tokens_out = _estimate_tokens(result.get("report", ""))
        self.cost_tracker.record_ai_step("generate_report", tokens_in=tokens_in,
                                         tokens_out=tokens_out, model_type=model_type,
                                         detail={"filename": result.get("filename", "")})

        # 生成并保存成本报告 + 运营分析
        try:
            cost_report = self.cost_tracker.generate_report()
            # 记录项目汇总 (供运营飞轮进化) - 使用真实成本数据
            total_cost_cents = int(cost_report.get("total_cost", 0) * 100)
            total_duration_ms = sum(
                e.get("duration_ms", 0) for e in cost_report.get("detailed_breakdown", [])
            )
            record_project_summary(
                project_id=project_id, specs=specs,
                total_duration_ms=total_duration_ms,
                total_cost_cents=total_cost_cents,
                quality_overall=90,
            )
        except Exception:
            # 成本统计异常不影响主流程
            record_project_summary(
                project_id=project_id, specs=specs,
                total_duration_ms=_duration_ms,
                total_cost_cents=50 if use_cloud else 0,
                quality_overall=90,
            )

        # 触发运营飞轮进化 (异步, 不阻塞报告输出)
        try:
            evo_results = run_all_evolution()
            _evo_count = sum(1 for v in evo_results.values()
                             if v.get("evolved") or v.get("alerts"))
            if _evo_count:
                self._push_progress("generate_report",
                                    f"运营飞轮: {_evo_count} 项进化触发",
                                    "info")
        except Exception as e:
            pass  # 飞轮异常不影响主流程

        return {"report": result["report"],
                "report_filepath": result["filepath"],
                "report_filename": result["filename"]}

    # ==================== 执行入口 ====================
    def _initial_state(self, raw_text, solve_count, use_cloud_review, use_cloud_report):
        return {
            "raw_text": raw_text, "specs": {}, "confidence": 0.0,
            "results": [], "top_solutions": [],
            "scenario_info": {}, "layered_specs": {},
            "review": "", "recommendation": "",
            "con_report": "", "convergence_findings": {},
            "pro_report": "", "ready_for_confirm": False,
            "report": "", "report_filepath": "", "report_filename": "",
            "matched_templates": [], "project_id": "",
            "errors": [],
            "solve_count": solve_count,
            "use_cloud_review": use_cloud_review,
            "use_cloud_report": use_cloud_report,
            "step": "init", "status": "started", "timestamp": "",
        }

    def run(self, raw_text: str, solve_count: int = 10,
            use_cloud_review: bool = False,
            use_cloud_report: bool = False) -> dict:
        # v2.05.02: 应用飞轮路由规则
        use_cloud_review, use_cloud_report = _apply_routing_rules(use_cloud_review, use_cloud_report)
        initial_state = self._initial_state(raw_text, solve_count, use_cloud_review, use_cloud_report)
        audit_logger.log(agent_name="Orchestrator", action="workflow_start",
                         parameters={"raw_text": raw_text[:100], "solve_count": solve_count},
                         status="started")
        final_state = self.compiled.invoke(
            initial_state,
            config={"configurable": {"thread_id": f"thread_{int(time.time())}"}})
        final_step = final_state.get("step", "unknown")
        final_status = STEP_STATUS_MAP.get(final_step, final_state.get("status", "unknown"))
        audit_logger.log(agent_name="Orchestrator", action="workflow_complete",
                         parameters={"project_id": final_state.get("project_id", "")},
                         result={"status": final_status,
                                 "results_count": len(final_state.get("results", [])),
                                 "ready_for_confirm": final_state.get("ready_for_confirm", False)},
                         status="success" if final_status == "completed" else "partial")
        return final_state

    def _next_steps_after(self, node_name: str, state_update: dict) -> list:
        """推断节点完成后的下一步 (用于推送 step_start)"""
        if node_name == "parse_requirement":
            return [{"step": "generate_designs",
                     "message": "方案生成师开始生成参数矩阵并批量求解..."}]
        elif node_name == "generate_designs":
            if state_update.get("results"):
                return [
                    {"step": "review_solutions",
                     "message": "评审推荐师开始分析方案矩阵 (放大轨)..."},
                    {"step": "convergence_review",
                     "message": "收敛阵营启动: 逆向评审 → 现实检验 → 沙盘推演 → 魔鬼代言人..."},
                ]
            else:
                return [{"step": "generate_report",
                         "message": "报告撰写员开始生成报告..."}]
        elif node_name == "decision_gate":
            # v2.05.03: 双报告齐备时进入人工确认, 否则直达报告
            if state_update.get("ready_for_confirm") and state_update.get("checkpoint_id"):
                return [{"step": "human_approval",
                         "message": "等待人工确认: 请审阅双报告并做出决策..."}]
            return [{"step": "generate_report",
                     "message": "报告撰写员开始生成正式设计报告..."}]
        elif node_name == "human_approval":
            return [{"step": "generate_report",
                     "message": "报告撰写员开始生成正式设计报告..."}]
        return []

    def run_streaming(self, raw_text: str, solve_count: int = 10,
                      use_cloud_review: bool = False,
                      use_cloud_report: bool = False):
        """
        流式执行工作流, 实时推送进度事件:
          - step_start: 节点开始前
          - progress: 节点执行中的子步骤进度
          - step_done: 节点完成后 (含结果数据)
          - fatal: 致命错误

        使用后台线程运行 LangGraph stream, 主线程通过队列轮询:
          1. 先 yield step_start (第一个节点)
          2. 循环: drain progress queue → 检查 node_done → yield step_start(下一节点)
          3. 结束时 drain 剩余 progress
        """
        # v2.05.02: 应用飞轮路由规则
        use_cloud_review, use_cloud_report = _apply_routing_rules(use_cloud_review, use_cloud_report)
        initial_state = self._initial_state(raw_text, solve_count, use_cloud_review, use_cloud_report)
        thread_id = f"thread_{int(time.time())}"
        config = {"configurable": {"thread_id": thread_id}}

        audit_logger.log(agent_name="Orchestrator", action="workflow_start_stream",
                         parameters={"raw_text": raw_text[:100], "solve_count": solve_count},
                         status="started")

        # 清空进度队列
        while not self._progress_queue.empty():
            try:
                self._progress_queue.get_nowait()
            except queue.Empty:
                break

        # 后台线程运行 LangGraph stream
        result_queue = queue.Queue()

        def _worker():
            try:
                for event in self.compiled.stream(initial_state, config=config, stream_mode="updates"):
                    for node_name, state_update in event.items():
                        if state_update is None:
                            state_update = {}
                        result_queue.put(("node_done", node_name, state_update))
            except Exception as e:
                result_queue.put(("error", None, str(e)))
            finally:
                result_queue.put(("finished", None, None))

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        # 1. 推送第一个节点的 step_start
        yield {
            "type": "step_start",
            "step": "parse_requirement",
            "message": "需求分析师开始解析客户需求...",
        }

        # 2. 主循环: 轮询 progress 和 node_done
        while True:
            # 2a. 检查取消标志 (v1.03.02)
            try:
                from progress_hub import progress_hub
                if progress_hub.is_cancelled():
                    yield {"type": "progress", "step": "cancel", "message": "收到终止信号, 正在停止工作流...", "level": "warn"}
                    # 等待后台线程结束
                    thread.join(timeout=3)
                    yield {"type": "fatal", "error": "用户已终止工作流"}
                    return
            except ImportError:
                pass

            # 2b. 先 drain 进度队列 (非阻塞)
            drained_any = False
            while not self._progress_queue.empty():
                try:
                    progress_event = self._progress_queue.get_nowait()
                    yield progress_event
                    drained_any = True
                except queue.Empty:
                    break

            # 2c. 等待节点完成事件 (带超时, 以便继续轮询进度)
            try:
                item = result_queue.get(timeout=0.3)
            except queue.Empty:
                continue

            if item[0] == "finished":
                break
            elif item[0] == "error":
                yield {"type": "fatal", "error": item[2]}
                break
            elif item[0] == "node_done":
                node_name = item[1]
                state_update = item[2]

                # drain 在节点执行期间积累的进度事件
                while not self._progress_queue.empty():
                    try:
                        yield self._progress_queue.get_nowait()
                    except queue.Empty:
                        break

                # 推送 step_done
                state_update["step"] = node_name
                state_update["status"] = STEP_STATUS_MAP.get(node_name, node_name)
                state_update["type"] = "step_done"
                yield state_update

                # 推送下一步的 step_start
                next_steps = self._next_steps_after(node_name, state_update)
                for ns in next_steps:
                    yield {
                        "type": "step_start",
                        "step": ns["step"],
                        "message": ns["message"],
                    }
                    # v2.05.03: 人工确认节点额外推送 human_input_required 事件
                    if ns["step"] == "human_approval":
                        checkpoint_id = state_update.get("checkpoint_id", "")
                        cp_info = hc.get_decision_context(checkpoint_id) if checkpoint_id else {}
                        yield {
                            "type": "human_input_required",
                            "step": "human_approval",
                            "checkpoint_id": checkpoint_id,
                            "title": cp_info.get("title", "人工确认"),
                            "description": cp_info.get("description", ""),
                            "context": cp_info.get("context", {}),
                            "timeout_seconds": cp_info.get("timeout_seconds", 86400),
                            "default_action": cp_info.get("default_action", "auto_first"),
                            "options": cp_info.get("options", ["approve", "reject", "override"]),
                        }

        # 3. drain 剩余进度事件
        while not self._progress_queue.empty():
            try:
                yield self._progress_queue.get_nowait()
            except queue.Empty:
                break

        # 确保后台线程结束
        thread.join(timeout=5)
