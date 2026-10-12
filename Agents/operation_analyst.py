"""
运营分析师智能体 - 分析成本报告,提出优化建议
推动一人公司从自动化脚本迈向可运营商业系统
"""
import json
import sqlite3
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

# 建议阈值
HUMAN_STEP_TIME_THRESHOLD_H = 0.5      # 人工环节超过30分钟需优化
CLOUD_COST_THRESHOLD = 5.0             # 云AI成本超5元建议替代
COMPUTE_STEP_TIME_THRESHOLD_H = 1.0    # 计算环节超过1小时建议降量
COST_VS_AVG_RATIO = 1.3                # 超出历史均值30%需复盘
HUMAN_WAIT_TIMEOUT_RATIO = 0.5         # 人工超时默认占比过高需优化界面

# v4.01.029: 运营质量指标阈值
CORRECTION_RATE_THRESHOLD = 0.20       # 人工修正率>20% → AI提取质量待提升
REWORK_RATE_THRESHOLD = 0.20           # 返工率>20% → 前置校验待加强

# v4.01.029: 运营质量指标模块 (人工修正率/返工率)
try:
    from quality_metrics import (get_project_metrics as _qm_project,
                                 get_global_metrics as _qm_global)
    _QM_OK = True
except Exception:  # pragma: no cover
    try:
        from Core.quality_metrics import (get_project_metrics as _qm_project,
                                          get_global_metrics as _qm_global)
        _QM_OK = True
    except Exception:
        _QM_OK = False

        def _qm_project(_pid):
            return {}

        def _qm_global(_days=30):
            return {}


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_historical_avg_cost(days: int = 90) -> float:
    """从 cost_events 表计算历史项目平均总成本"""
    conn = _get_conn()
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """SELECT project_id, SUM(cost) as total
               FROM cost_events
               WHERE created_at >= ?
               GROUP BY project_id""",
            (since,)
        ).fetchall()
        totals = [r[1] for r in rows if r[1] is not None]
        return sum(totals) / max(len(totals), 1)
    finally:
        conn.close()


def get_historical_avg_duration(days: int = 90) -> float:
    """历史项目平均总耗时 (小时)"""
    conn = _get_conn()
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """SELECT project_id, SUM(duration_ms) as total_ms
               FROM cost_events
               WHERE created_at >= ?
               GROUP BY project_id""",
            (since,)
        ).fetchall()
        totals = [r[1] / 3600000 for r in rows if r[1] is not None]
        return sum(totals) / max(len(totals), 1)
    finally:
        conn.close()


class OperationAnalyst:
    """运营分析师: 从成本与耗时数据中提炼改进建议"""

    def __init__(self):
        self.suggestions: List[str] = []

    def analyze(self, cost_report: Dict[str, Any],
                workflow_context: Optional[Dict[str, Any]] = None) -> List[str]:
        """分析运营数据,返回优化建议列表"""
        self.suggestions = []
        events = cost_report.get("detailed_breakdown", [])

        self._analyze_human_steps(events)
        self._analyze_ai_cost(events, cost_report)
        self._analyze_compute_steps(events)
        self._compare_historical(cost_report)
        self._analyze_timeout_defaults(events)
        self._analyze_decision_efficiency(events, workflow_context)
        self._analyze_quality_metrics(cost_report.get("project_id", ""))

        return self.suggestions

    def _analyze_human_steps(self, events: List[Dict[str, Any]]):
        """1. 分析人工耗时"""
        human_steps = [e for e in events if e.get("executor") == "Human"]
        total_human_hours = sum(e.get("duration_ms", 0) / 3600000 for e in human_steps)
        for step in human_steps:
            duration_h = step.get("duration_ms", 0) / 3600000
            if duration_h > HUMAN_STEP_TIME_THRESHOLD_H:
                ratio = duration_h / max(total_human_hours, 0.001) * 100
                self.suggestions.append(
                    f"人工环节「{step['step']}」耗时 {duration_h:.2f}h,"
                    f"占人工总耗时 {ratio:.0f}%,建议优化决策界面或增加AI预判减少人工判断时间"
                )

    def _analyze_ai_cost(self, events: List[Dict[str, Any]], cost_report: Dict[str, Any]):
        """2. 分析AI成本"""
        cloud_cost = sum(e.get("cost", 0) for e in events if e.get("executor") == "AI-Cloud")
        if cloud_cost > CLOUD_COST_THRESHOLD:
            # 粗略估算本地替代成本 (按1/5)
            local_estimate = cloud_cost / 5
            self.suggestions.append(
                f"云端AI成本 ¥{cloud_cost:.2f},建议评估是否可用本地模型替代部分调用,"
                f"预估可降至 ¥{local_estimate:.2f}"
            )

        # 单一云端步骤成本过高
        for e in events:
            if e.get("executor") == "AI-Cloud" and e.get("cost", 0) > CLOUD_COST_THRESHOLD / 2:
                self.suggestions.append(
                    f"云端步骤「{e['step']}」单次成本 ¥{e['cost']:.2f},"
                    f"建议审查提示词长度或改用本地7B模型"
                )

    def _analyze_compute_steps(self, events: List[Dict[str, Any]]):
        """3. 分析求解耗时"""
        for step in events:
            if step.get("executor") == "Computer":
                duration_h = step.get("duration_ms", 0) / 3600000
                if duration_h > COMPUTE_STEP_TIME_THRESHOLD_H:
                    self.suggestions.append(
                        f"求解「{step['step']}」耗时 {duration_h:.2f}h,"
                        f"建议检查参数矩阵是否可缩减(是否有冗余方案?)"
                    )

    def _compare_historical(self, cost_report: Dict[str, Any]):
        """4. 对比历史平均"""
        avg_cost = get_historical_avg_cost()
        total_cost = cost_report.get("total_cost", 0)
        if avg_cost > 0 and total_cost > avg_cost * COST_VS_AVG_RATIO:
            self.suggestions.append(
                f"本项目总成本 ¥{total_cost:.2f},超出历史均值 {((total_cost/avg_cost)-1)*100:.0f}%,"
                f"建议复盘是否有异常环节"
            )

        avg_duration = get_historical_avg_duration()
        total_duration_h = sum(
            e.get("duration_ms", 0) / 3600000
            for e in cost_report.get("detailed_breakdown", [])
        )
        if avg_duration > 0 and total_duration_h > avg_duration * COST_VS_AVG_RATIO:
            self.suggestions.append(
                f"本项目总耗时 {total_duration_h:.2f}h,超出历史均值 {((total_duration_h/avg_duration)-1)*100:.0f}%,"
                f"建议检查是否存在超时等待或冗余计算"
            )

    def _analyze_timeout_defaults(self, events: List[Dict[str, Any]]):
        """5. 分析超时默认机制触发频率"""
        timeout_events = [
            e for e in events
            if e.get("executor") == "Human" and e.get("detail", {}).get("timeout")
        ]
        if not timeout_events:
            return

        total_human_count = len([e for e in events if e.get("executor") == "Human"])
        ratio = len(timeout_events) / max(total_human_count, 1)
        if ratio > HUMAN_WAIT_TIMEOUT_RATIO:
            steps = ",".join({e.get("step", "") for e in timeout_events})
            self.suggestions.append(
                f"人工环节超时默认触发率 {ratio*100:.0f}%(涉及{steps}),"
                f"建议缩短超时阈值、增强通知或给AI更大默认决策权"
            )

    def _analyze_decision_efficiency(self, events: List[Dict[str, Any]],
                                     workflow_context: Optional[Dict[str, Any]]):
        """6. 分析决策效率"""
        # 如果存在多次修改参数的痕迹
        ui_modifies = 0
        if workflow_context:
            ui_modifies = workflow_context.get("ui_modify_count", 0)
        if ui_modifies >= 3:
            self.suggestions.append(
                f"决策界面参数修改 {ui_modifies} 次,建议将高频修改项前置或增加AI推荐默认值"
            )

    def _analyze_quality_metrics(self, project_id: str):
        """7. v4.01.029: 分析运营质量指标 (人工修正率 / 返工率)"""
        if not _QM_OK or not project_id:
            return
        try:
            proj = _qm_project(project_id) or {}
            glob = _qm_global(30) or {}
        except Exception:
            return

        corr = proj.get("correction_rate", 0) or 0
        if corr > CORRECTION_RATE_THRESHOLD:
            tops = proj.get("top_corrected_params", [])[:3]
            top_txt = "、".join(f"{t['param_cn']}" for t in tops) if tops else "多个参数"
            self.suggestions.append(
                f"人工修正率 {corr*100:.0f}%(修正 {proj.get('override_applied',0)}/"
                f"{proj.get('total_params',0)} 项),高频修正参数: {top_txt},"
                f"建议强化需求提取提示词或把这些参数加入强制追问,减少人工返工"
            )

        rework = proj.get("rework_rate", 0) or 0
        if rework > REWORK_RATE_THRESHOLD:
            worst = ""
            by_cp = proj.get("by_checkpoint", {}) or {}
            if by_cp:
                worst_cp = max(by_cp.items(), key=lambda x: x[1].get("rework_rate", 0))
                if worst_cp[1].get("rework_rate", 0) > 0:
                    worst = f",其中「{worst_cp[0]}」返工率最高 " \
                            f"{worst_cp[1]['rework_rate']*100:.0f}%"
            self.suggestions.append(
                f"返工率 {rework*100:.0f}%({proj.get('rework_count',0)}/"
                f"{proj.get('total_decisions',0)} 次决策为重做/驳回){worst},"
                f"建议在人工确认前增加一致性校验与AI自检,降低无效轮次"
            )

        # 与近30天全局均值对比
        g_corr = glob.get("correction_rate", 0) or 0
        if g_corr and corr > g_corr * 1.5 and corr > 0.1:
            self.suggestions.append(
                f"本项目人工修正率 {corr*100:.0f}% 明显高于近30天均值 "
                f"{g_corr*100:.0f}%,建议复盘本项目需求描述是否过于模糊"
            )

    def get_quality_metrics(self, project_id: str) -> Dict[str, Any]:
        """v4.01.029: 对外暴露运营质量指标 (项目 + 全局)"""
        if not _QM_OK:
            return {"available": False}
        try:
            return {
                "available": True,
                "project": _qm_project(project_id) if project_id else {},
                "global": _qm_global(30),
            }
        except Exception as e:
            return {"available": False, "error": str(e)}

    def generate_report(self, cost_report: Dict[str, Any],
                        workflow_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """生成完整运营分析报告"""
        suggestions = self.analyze(cost_report, workflow_context)
        project_id = cost_report.get("project_id", "")
        return {
            "project_id": project_id,
            "suggestions": suggestions,
            "suggestion_count": len(suggestions),
            "risk_level": self._risk_level(cost_report, suggestions),
            "analyzed_at": datetime.now().isoformat(),
            "historical_avg_cost": round(get_historical_avg_cost(), 4),
            "historical_avg_duration_h": round(get_historical_avg_duration(), 4),
            # v4.01.029: 运营质量指标 (人工修正率/返工率)
            "quality_metrics": self.get_quality_metrics(project_id),
        }

    @staticmethod
    def _risk_level(cost_report: Dict[str, Any], suggestions: List[str]) -> str:
        """根据成本和建议数量评估风险等级"""
        total_cost = cost_report.get("total_cost", 0)
        if total_cost > 50 or len(suggestions) >= 4:
            return "high"
        if total_cost > 20 or len(suggestions) >= 2:
            return "medium"
        return "low"


def analyze_cost_report(project_id: str,
                        workflow_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """独立入口: 根据 project_id 生成运营分析报告"""
    from Core.cost_tracker import get_project_cost_report
    cost_report = get_project_cost_report(project_id)
    analyst = OperationAnalyst()
    return analyst.generate_report(cost_report, workflow_context)


if __name__ == "__main__":
    sample_report = {
        "project_id": "PROJ_TEST_001",
        "total_cost": 47.32,
        "detailed_breakdown": [
            {"step": "方案求解", "executor": "Computer", "duration_ms": 2.1*3600000, "cost": 2.1},
            {"step": "方案终选", "executor": "Human", "duration_ms": 0.4*3600000, "cost": 8.0},
            {"step": "云端深度分析", "executor": "AI-Cloud", "duration_ms": 2000, "cost": 3.0},
        ]
    }
    analyst = OperationAnalyst()
    result = analyst.generate_report(sample_report, {"ui_modify_count": 4})
    print(json.dumps(result, ensure_ascii=False, indent=2))
