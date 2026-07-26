"""
LangGraph 多智能体编排器 - 状态图驱动的工作流
从需求到报告一站式完成，全程审计可溯源

工作流:
  START → RequirementAgent → DesignAgent → ReviewAgent → ReportAgent → END
                                       (条件路由: 无结果时跳过评审)
"""
import sys
import os
import time
import json
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
import audit_logger


class WorkflowState(TypedDict):
    """工作流状态"""
    raw_text: str                    # 客户需求原文
    specs: dict                      # 解析后的规格
    confidence: float                # 解析置信度
    results: List[dict]              # 求解结果
    top_solutions: List[dict]        # Top方案
    review: str                      # 评审意见
    recommendation: str              # 推荐结论
    report: str                      # 报告内容
    report_filepath: str             # 报告文件路径
    report_filename: str             # 报告文件名
    matched_templates: list          # 匹配的技能模板
    project_id: str                  # 项目ID
    status: str                      # 当前状态
    errors: List[str]                # 错误列表
    solve_count: int                 # 求解数量
    use_cloud_review: bool           # 是否用云端评审
    use_cloud_report: bool           # 是否用云端报告
    step: str                        # 当前步骤
    timestamp: str                   # 开始时间


class MotorAIOrchestrator:
    """多智能体编排器"""

    def __init__(self):
        self.req_agent = RequirementAgent()
        self.design_agent = DesignAgent()
        self.review_agent = ReviewAgent()
        self.report_agent = ReportAgent()

        # 所有 agent 共享 project_id
        self._current_project_id = None

        self.graph = self._build_graph()
        self.checkpointer = MemorySaver()
        self.compiled = self.graph.compile(checkpointer=self.checkpointer)

    def _build_graph(self):
        """构建 LangGraph 状态图"""
        graph = StateGraph(WorkflowState)

        # 添加节点
        graph.add_node("parse_requirement", self._node_parse_requirement)
        graph.add_node("generate_designs", self._node_generate_designs)
        graph.add_node("review_solutions", self._node_review_solutions)
        graph.add_node("generate_report", self._node_generate_report)

        # 添加边
        graph.add_edge(START, "parse_requirement")
        graph.add_edge("parse_requirement", "generate_designs")
        graph.add_conditional_edges(
            "generate_designs",
            self._should_review,
            {
                "review": "review_solutions",
                "skip": "generate_report"
            }
        )
        graph.add_edge("review_solutions", "generate_report")
        graph.add_edge("generate_report", END)

        return graph

    def _should_review(self, state: WorkflowState) -> str:
        """条件路由：有结果才评审"""
        if state.get("results") and len(state["results"]) > 0:
            return "review"
        return "skip"

    def _node_parse_requirement(self, state: WorkflowState) -> dict:
        """节点：需求解析"""
        # 生成项目ID
        project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self._current_project_id = project_id

        # 设置所有 agent 的 project_id
        for agent in [self.req_agent, self.design_agent, self.review_agent, self.report_agent]:
            agent.project_id = project_id

        result = self.req_agent.safe_run(raw_text=state["raw_text"])

        if "error" in result:
            return {
                "status": "error",
                "errors": state.get("errors", []) + [result["error"]],
                "step": "parse_requirement"
            }

        return {
            "specs": result["specs"],
            "confidence": result["confidence"],
            "project_id": project_id,
            "status": "parsed",
            "step": "parse_requirement",
            "timestamp": datetime.now().isoformat()
        }

    def _node_generate_designs(self, state: WorkflowState) -> dict:
        """节点：方案生成与求解"""
        count = state.get("solve_count", 10)
        result = self.design_agent.safe_run(
            specs=state["specs"],
            count=count
        )

        if "error" in result:
            return {
                "status": "error",
                "errors": state.get("errors", []) + [result["error"]],
                "step": "generate_designs",
                "results": []
            }

        return {
            "results": result["results"],
            "matched_templates": result.get("matched_templates", []),
            "status": "solved",
            "step": "generate_designs"
        }

    def _node_review_solutions(self, state: WorkflowState) -> dict:
        """节点：方案评审"""
        use_cloud = state.get("use_cloud_review", False)
        result = self.review_agent.safe_run(
            results=state["results"],
            specs=state["specs"],
            use_cloud=use_cloud
        )

        if "error" in result:
            return {
                "status": "error",
                "errors": state.get("errors", []) + [result["error"]],
                "step": "review_solutions",
                "review": "评审失败",
                "top_solutions": state["results"][:5]
            }

        return {
            "review": result["review"],
            "top_solutions": result["top_solutions"],
            "recommendation": result["recommendation"],
            "status": "reviewed",
            "step": "review_solutions"
        }

    def _node_generate_report(self, state: WorkflowState) -> dict:
        """节点：报告生成"""
        use_cloud = state.get("use_cloud_report", False)
        review = state.get("review", "无评审意见")
        results = state.get("results", [])
        specs = state.get("specs", {})

        result = self.report_agent.safe_run(
            specs=specs,
            results=results,
            review=review,
            use_cloud=use_cloud
        )

        if "error" in result:
            return {
                "status": "error",
                "errors": state.get("errors", []) + [result["error"]],
                "step": "generate_report"
            }

        return {
            "report": result["report"],
            "report_filepath": result["filepath"],
            "report_filename": result["filename"],
            "status": "completed",
            "step": "generate_report"
        }

    def run(self, raw_text: str, solve_count: int = 10,
            use_cloud_review: bool = False,
            use_cloud_report: bool = False) -> dict:
        """执行完整工作流"""
        initial_state = {
            "raw_text": raw_text,
            "specs": {},
            "confidence": 0.0,
            "results": [],
            "top_solutions": [],
            "review": "",
            "recommendation": "",
            "report": "",
            "report_filepath": "",
            "report_filename": "",
            "matched_templates": [],
            "project_id": "",
            "status": "started",
            "errors": [],
            "solve_count": solve_count,
            "use_cloud_review": use_cloud_review,
            "use_cloud_report": use_cloud_report,
            "step": "init",
            "timestamp": ""
        }

        # 记录工作流启动
        audit_logger.log(
            agent_name="Orchestrator",
            action="workflow_start",
            parameters={"raw_text": raw_text[:100], "solve_count": solve_count},
            status="started"
        )

        # 执行
        final_state = self.compiled.invoke(
            initial_state,
            config={"configurable": {"thread_id": f"thread_{int(time.time())}"}}
        )

        # 记录工作流完成
        audit_logger.log(
            agent_name="Orchestrator",
            action="workflow_complete",
            parameters={"project_id": final_state.get("project_id", "")},
            result={"status": final_state.get("status"), "results_count": len(final_state.get("results", []))},
            status="success" if final_state.get("status") == "completed" else "partial"
        )

        return final_state

    def run_streaming(self, raw_text: str, solve_count: int = 10,
                      use_cloud_review: bool = False,
                      use_cloud_report: bool = False):
        """流式执行，返回每一步的状态更新"""
        initial_state = {
            "raw_text": raw_text,
            "specs": {},
            "confidence": 0.0,
            "results": [],
            "top_solutions": [],
            "review": "",
            "recommendation": "",
            "report": "",
            "report_filepath": "",
            "report_filename": "",
            "matched_templates": [],
            "project_id": "",
            "status": "started",
            "errors": [],
            "solve_count": solve_count,
            "use_cloud_review": use_cloud_review,
            "use_cloud_report": use_cloud_report,
            "step": "init",
            "timestamp": ""
        }

        thread_id = f"thread_{int(time.time())}"
        config = {"configurable": {"thread_id": thread_id}}

        audit_logger.log(
            agent_name="Orchestrator",
            action="workflow_start_stream",
            parameters={"raw_text": raw_text[:100], "solve_count": solve_count},
            status="started"
        )

        for event in self.compiled.stream(initial_state, config=config, stream_mode="updates"):
            for node_name, state_update in event.items():
                state_update["step"] = node_name
                yield state_update
