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
from motor_tools import generate_param_matrix, batch_solve
from evolution_engine import find_matching_templates


# 节点名 → 状态名映射 (供 run/run_streaming 统一设置)
STEP_STATUS_MAP = {
    "parse_requirement": "parsed",
    "generate_designs": "solved",
    "review_solutions": "reviewed",
    "convergence_review": "converged",
    "decision_gate": "ready",
    "generate_report": "completed",
}

# 节点中文显示名
STEP_DISPLAY_NAMES = {
    "parse_requirement": "需求分析师",
    "generate_designs": "方案生成师",
    "review_solutions": "评审推荐师",
    "convergence_review": "收敛阵营",
    "decision_gate": "决策门",
    "generate_report": "报告撰写员",
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

        # 实时进度队列 (线程安全)
        self._progress_queue = queue.Queue()

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

        result = self.req_agent.safe_run(raw_text=state["raw_text"])
        if "error" in result:
            self._push_progress("parse_requirement", f"需求解析失败: {result['error']}", "error")
            return {"errors": [result["error"]]}

        self._push_progress("parse_requirement",
                            f"需求解析完成: 类型={result['specs'].get('motor_type', '异步电动机')}, "
                            f"功率={result['specs'].get('power_kw', '?')}kW, "
                            f"电压={result['specs'].get('voltage_v', '?')}V, "
                            f"转速={result['specs'].get('speed_rpm', '?')}rpm")
        return {"specs": result["specs"], "confidence": result["confidence"],
                "project_id": project_id,
                "timestamp": datetime.now().isoformat()}

    def _node_generate_designs(self, state: WorkflowState) -> dict:
        count = state.get("solve_count", 10)
        specs = state.get("specs", {})

        self._push_progress("generate_designs", f"开始生成参数矩阵 (目标 {count} 组方案)...")
        self._push_progress("generate_designs", "查找匹配的技能模板...")

        # 1. 查找匹配的技能模板
        matched = find_matching_templates(specs, top_n=3)
        if matched:
            self._push_progress("generate_designs",
                                f"找到 {len(matched)} 个匹配模板: {', '.join(m.get('name', '?') for m in matched[:2])}")

        # 2. 生成参数矩阵
        self._push_progress("generate_designs", "生成参数扫描矩阵...")
        params_list = generate_param_matrix(specs, count)
        self._push_progress("generate_designs",
                            f"参数矩阵生成完成: {len(params_list)} 组方案, 开始批量求解...")

        # 3. 批量求解 (带进度回调)
        def _batch_progress(idx, total, params, result):
            mode_tag = "[MC]" if result.get("solve_mode") == "motorcad" else "[SIM]"
            eff = result.get("efficiency", 0)
            temp = result.get("max_temp", 0)
            self._push_progress("generate_designs",
                                f"求解进度: {idx}/{total} {mode_tag} "
                                f"OD={params.get('stator_od', '?')}mm "
                                f"Poles={params.get('poles', '?')} "
                                f"效率={eff:.1f}% 温升={temp:.0f}K")

        results = batch_solve(params_list, progress_cb=_batch_progress)

        # 4. 按效率排序
        results.sort(key=lambda x: x.get("efficiency", 0), reverse=True)

        if results:
            best = results[0]
            self._push_progress("generate_designs",
                                f"批量求解完成: {len(results)} 组方案, "
                                f"最优效率={best.get('efficiency', 0):.2f}%, "
                                f"温升={best.get('max_temp', 0):.1f}K",
                                "success")

        return {"results": results,
                "matched_templates": matched}

    def _node_review_solutions(self, state: WorkflowState) -> dict:
        """放大轨: 评审推荐师 → 支持报告"""
        self._push_progress("review_solutions", "评审推荐师开始分析方案矩阵...")
        self._push_progress("review_solutions",
                            f"输入: {len(state['results'])} 组方案, 正在调用本地 LLM 生成支持报告...")

        use_cloud = state.get("use_cloud_review", False)
        result = self.review_agent.safe_run(
            results=state["results"], specs=state["specs"], use_cloud=use_cloud)

        if "error" in result:
            self._push_progress("review_solutions", f"评审失败: {result['error']}", "error")
            return {"errors": [result["error"]],
                    "review": "评审失败",
                    "top_solutions": state["results"][:5]}

        self._push_progress("review_solutions",
                            f"支持报告已生成 (推荐 {len(result.get('top_solutions', []))} 个方案)",
                            "success")
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
        self._push_progress("convergence_review", "[1/4] 逆向评审师启动: 检查被丢弃方案中的异类价值...")
        try:
            rev = self.reverse_reviewer.safe_run(results=results, specs=specs)
            findings["reverse"] = rev.get("con_report", "")
            findings["reverse_highlights"] = rev.get("discarded_highlights", [])
            self._push_progress("convergence_review", "[1/4] 逆向评审师完成")
        except Exception as e:
            findings["reverse"] = f"逆向评审失败: {e}"
            self._push_progress("convergence_review", f"[1/4] 逆向评审师失败: {e}", "error")

        # 2. 现实检验师
        self._push_progress("convergence_review", "[2/4] 现实检验师启动: 评估量产可行性 (TRL评级)...")
        try:
            real = self.reality_checker.safe_run(results=results, specs=specs)
            findings["reality"] = real.get("con_report", "")
            findings["feasibility"] = real.get("feasibility", [])
            self._push_progress("convergence_review", "[2/4] 现实检验师完成")
        except Exception as e:
            findings["reality"] = f"现实检验失败: {e}"
            self._push_progress("convergence_review", f"[2/4] 现实检验师失败: {e}", "error")

        # 3. 沙盘推演师
        self._push_progress("convergence_review", "[3/4] 沙盘推演师启动: 蒙特卡洛1000次扰动模拟...")
        try:
            sand = self.sandbox_simulator.safe_run(results=results, specs=specs)
            findings["sandbox"] = sand.get("con_report", "")
            findings["simulations"] = sand.get("simulations", [])
            self._push_progress("convergence_review", "[3/4] 沙盘推演师完成")
        except Exception as e:
            findings["sandbox"] = f"沙盘推演失败: {e}"
            self._push_progress("convergence_review", f"[3/4] 沙盘推演师失败: {e}", "error")

        # 4. 魔鬼代言人 (引用前三个的发现)
        self._push_progress("convergence_review", "[4/4] 魔鬼代言人启动: 撰写方案遗书 (假设已失败)...")
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
        except Exception as e:
            findings["devils_advocate"] = f"魔鬼代言人失败: {e}"
            self._push_progress("convergence_review", f"[4/4] 魔鬼代言人失败: {e}", "error")

        # 汇总反对报告
        self._push_progress("convergence_review", "正在汇总四剑客发现为反对报告...")
        con_report = self._assemble_con_report(findings)

        self._push_progress("convergence_review", "反对报告生成完成", "success")

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

        if ready:
            self._push_progress("decision_gate", "双报告齐备, 决策门已解锁", "success")
        else:
            self._push_progress("decision_gate", "报告未齐备, 决策门锁定", "warn")

        return {"pro_report": pro,
                "ready_for_confirm": ready}

    # ==================== 报告 ====================
    def _node_generate_report(self, state: WorkflowState) -> dict:
        self._push_progress("generate_report", "报告撰写员开始生成正式设计报告...")

        use_cloud = state.get("use_cloud_report", False)
        review = state.get("review", "无评审意见")
        results = state.get("results", [])
        specs = state.get("specs", {})

        self._push_progress("generate_report", "调用本地 LLM 生成报告 (可能需要 30-60 秒)...")

        result = self.report_agent.safe_run(
            specs=specs, results=results, review=review, use_cloud=use_cloud)

        if "error" in result:
            self._push_progress("generate_report", f"报告生成失败: {result['error']}", "error")
            return {"errors": [result["error"]]}

        self._push_progress("generate_report",
                            f"报告已生成: {result.get('filename', '?')}", "success")
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

        # 3. drain 剩余进度事件
        while not self._progress_queue.empty():
            try:
                yield self._progress_queue.get_nowait()
            except queue.Empty:
                break

        # 确保后台线程结束
        thread.join(timeout=5)
