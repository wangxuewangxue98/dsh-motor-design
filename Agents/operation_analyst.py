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


def _get_conn():
    return sqlite3.connect(DB_PATH)


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

    def generate_report(self, cost_report: Dict[str, Any],
                        workflow_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """生成完整运营分析报告"""
        suggestions = self.analyze(cost_report, workflow_context)
        return {
            "project_id": cost_report.get("project_id", ""),
            "suggestions": suggestions,
            "suggestion_count": len(suggestions),
            "risk_level": self._risk_level(cost_report, suggestions),
            "analyzed_at": datetime.now().isoformat(),
            "historical_avg_cost": round(get_historical_avg_cost(), 4),
            "historical_avg_duration_h": round(get_historical_avg_duration(), 4),
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
