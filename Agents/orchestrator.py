"""
LangGraph 多智能体编排器 - 双轨辩证制衡体系
放大系统 (评审推荐师) + 收敛系统 (逆向/现实/沙盘/魔鬼) 并行, 汇入决策门

工作流 (v1.02.01):
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
"""
import sys
import os
import time
import json
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


# 节点名 → 状态名映射 (供 run/run_streaming 统一设置)
STEP_STATUS_MAP = {
    "parse_requirement": "parsed",
    "generate_designs": "solved",
    "review_solutions": "reviewed",
    "convergence_review": "converged",
    "decision_gate": "ready",
    "generate_report": "completed",
}


class WorkflowState(TypedDict):
    """工作流状态 (v1.02.02 双轨 - 修复并行冲突)"""
    raw_text: str
    specs: dict
    confidence: float
    results: List[dict]
    top_solutions: List[dict]
    # 放大轨
    review: str                    # 评审推荐意见 (支持报告)
    recommendation: str
    # 收敛轨
    con_report: str                # 汇总反对报告
    convergence_findings: dict     # 各收敛agent原始发现
    # 决策门
    pro_report: str                # 放大轨最终报告
    ready_for_confirm: bool        # 双报告齐备, 可交付用户确认
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

    def _build_graph(self):
        graph = StateGraph(WorkflowState)

        graph.add_node("parse_requirement", self._node_parse_requirement)
        graph.add_node("generate_designs", self._node_generate_designs)
        graph.add_node("review_solutions", self._node_review_solutions)
        graph.add_node("convergence_review", self._node_convergence_review)
        graph.add_node("decision_gate", self._node_decision_gate)
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
        graph.add_edge("decision_gate", "generate_report")
        graph.add_edge("generate_report", END)

        return graph

    def _after_designs(self, state: WorkflowState) -> list:
        """分叉路由: 有结果则放大+收敛并行, 否则跳过"""
        if state.get("results") and len(state["results"]) > 0:
            return ["review_pro", "review_con"]
        return ["skip"]

    def _set_project_id(self, project_id: str):
        for agent in [self.req_agent, self.design_agent, self.review_agent,
                      self.report_agent, self.reverse_reviewer,
                      self.reality_checker, self.sandbox_simulator,
                      self.devils_advocate]:
            agent.project_id = project_id

    # ==================== 放大轨 ====================
    def _node_parse_requirement(self, state: WorkflowState) -> dict:
        project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self._current_project_id = project_id
        self._set_project_id(project_id)

        result = self.req_agent.safe_run(raw_text=state["raw_text"])
        if "error" in result:
            return {"errors": [result["error"]]}
        return {"specs": result["specs"], "confidence": result["confidence"],
                "project_id": project_id,
                "timestamp": datetime.now().isoformat()}

    def _node_generate_designs(self, state: WorkflowState) -> dict:
        count = state.get("solve_count", 10)
        result = self.design_agent.safe_run(specs=state["specs"], count=count)
        if "error" in result:
            return {"errors": [result["error"]], "results": []}
        return {"results": result["results"],
                "matched_templates": result.get("matched_templates", [])}

    def _node_review_solutions(self, state: WorkflowState) -> dict:
        """放大轨: 评审推荐师 → 支持报告"""
        use_cloud = state.get("use_cloud_review", False)
        result = self.review_agent.safe_run(
            results=state["results"], specs=state["specs"], use_cloud=use_cloud)
        if "error" in result:
            return {"errors": [result["error"]],
                    "review": "评审失败",
                    "top_solutions": state["results"][:5]}
        return {"review": result["review"],
                "top_solutions": result["top_solutions"],
                "recommendation": result["recommendation"]}

    # ==================== 收敛轨 ====================
    def _node_convergence_review(self, state: WorkflowState) -> dict:
        """收敛轨: 4个收敛agent → 反对报告"""
        results = state.get("results", [])
        specs = state.get("specs", {})
        findings = {}

        # 1. 逆向评审师
        try:
            rev = self.reverse_reviewer.safe_run(results=results, specs=specs)
            findings["reverse"] = rev.get("con_report", "")
            findings["reverse_highlights"] = rev.get("discarded_highlights", [])
        except Exception as e:
            findings["reverse"] = f"逆向评审失败: {e}"

        # 2. 现实检验师
        try:
            real = self.reality_checker.safe_run(results=results, specs=specs)
            findings["reality"] = real.get("con_report", "")
            findings["feasibility"] = real.get("feasibility", [])
        except Exception as e:
            findings["reality"] = f"现实检验失败: {e}"

        # 3. 沙盘推演师
        try:
            sand = self.sandbox_simulator.safe_run(results=results, specs=specs)
            findings["sandbox"] = sand.get("con_report", "")
            findings["simulations"] = sand.get("simulations", [])
        except Exception as e:
            findings["sandbox"] = f"沙盘推演失败: {e}"

        # 4. 魔鬼代言人 (引用前三个的发现)
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
        except Exception as e:
            findings["devils_advocate"] = f"魔鬼代言人失败: {e}"

        # 汇总反对报告
        con_report = self._assemble_con_report(findings)

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
        """决策门: 要求支持报告与反对报告同时齐备"""
        pro = state.get("review", "")
        con = state.get("con_report", "")
        ready = bool(pro and con)
        return {"pro_report": pro,
                "ready_for_confirm": ready}

    # ==================== 报告 ====================
    def _node_generate_report(self, state: WorkflowState) -> dict:
        use_cloud = state.get("use_cloud_report", False)
        review = state.get("review", "无评审意见")
        results = state.get("results", [])
        specs = state.get("specs", {})
        result = self.report_agent.safe_run(
            specs=specs, results=results, review=review, use_cloud=use_cloud)
        if "error" in result:
            return {"errors": [result["error"]]}
        return {"report": result["report"],
                "report_filepath": result["filepath"],
                "report_filename": result["filename"]}

    # ==================== 执行入口 ====================
    def _initial_state(self, raw_text, solve_count, use_cloud_review, use_cloud_report):
        return {
            "raw_text": raw_text, "specs": {}, "confidence": 0.0,
            "results": [], "top_solutions": [],
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

    def run_streaming(self, raw_text: str, solve_count: int = 10,
                      use_cloud_review: bool = False,
                      use_cloud_report: bool = False):
        initial_state = self._initial_state(raw_text, solve_count, use_cloud_review, use_cloud_report)
        thread_id = f"thread_{int(time.time())}"
        config = {"configurable": {"thread_id": thread_id}}
        audit_logger.log(agent_name="Orchestrator", action="workflow_start_stream",
                         parameters={"raw_text": raw_text[:100], "solve_count": solve_count},
                         status="started")
        for event in self.compiled.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, state_update in event.items():
                if state_update is None:
                    state_update = {}
                # 统一设置 step 和 status (节点返回值中不再包含这些字段)
                state_update["step"] = node_name
                state_update["status"] = STEP_STATUS_MAP.get(node_name, node_name)
                yield state_update
