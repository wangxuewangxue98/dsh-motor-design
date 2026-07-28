"""
成本核算模块 - 记录每步耗时和 token 消耗
让人机协作的运营成本可量化、可优化
"""
import json
import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = os.environ.get("DB_PATH", r"D:\MotorDesign\KnowledgeBase\designs.db")

# 计费标准 (元/百万 token)
CLOUD_INPUT_PRICE = 3.0       # 云端输入
CLOUD_OUTPUT_PRICE = 6.0      # 云端输出
LOCAL_INPUT_PRICE = 0.6       # 本地输入 (按云模型的 1/5)
LOCAL_OUTPUT_PRICE = 1.2      # 本地输出
COMPUTE_PRICE_PER_HOUR = 1.0  # 电脑程序耗时
HUMAN_PRICE_PER_HOUR = 20.0   # 人工耗时


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            task_id TEXT,
            step TEXT NOT NULL,
            executor TEXT NOT NULL,  -- 'AI-Local' / 'AI-Cloud' / 'Computer' / 'Human'
            start_time TEXT,
            end_time TEXT,
            duration_ms INTEGER DEFAULT 0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost REAL DEFAULT 0.0,
            currency TEXT DEFAULT 'CNY',
            model_type TEXT DEFAULT '',  -- local/cloud
            detail TEXT,                 -- JSON 附加信息
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cost_project ON cost_events(project_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cost_step ON cost_events(project_id, step)
    """)
    conn.commit()
    return conn


class TaskCostTracker:
    """任务级成本追踪器"""

    def __init__(self, project_id: str = "", task_id: str = ""):
        self.project_id = project_id or self._new_project_id()
        self.task_id = task_id or ""
        self.events: List[Dict[str, Any]] = []
        self.start_time = datetime.now()

    @staticmethod
    def _new_project_id() -> str:
        return f"PROJ_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def record_ai_step(self, step_name: str, tokens_in: int = 0, tokens_out: int = 0,
                       model_type: str = "local", detail: Optional[dict] = None,
                       duration_ms: int = 0):
        """记录 AI 步骤的 token 消耗"""
        if model_type == "cloud":
            cost = (tokens_in / 1e6) * CLOUD_INPUT_PRICE + (tokens_out / 1e6) * CLOUD_OUTPUT_PRICE
        else:
            cost = (tokens_in / 1e6) * LOCAL_INPUT_PRICE + (tokens_out / 1e6) * LOCAL_OUTPUT_PRICE
        event = {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "step": step_name,
            "executor": f"AI-{model_type.capitalize()}",
            "start_time": datetime.now().isoformat(),
            "end_time": datetime.now().isoformat(),
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": round(cost, 6),
            "model_type": model_type,
            "detail": detail or {},
        }
        self.events.append(event)
        self._save_event(event)
        return event

    def record_compute_time(self, step_name: str, cpu_hours: float,
                            detail: Optional[dict] = None):
        """记录电脑程序耗时"""
        cost = cpu_hours * COMPUTE_PRICE_PER_HOUR
        event = {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "step": step_name,
            "executor": "Computer",
            "start_time": datetime.now().isoformat(),
            "end_time": datetime.now().isoformat(),
            "duration_ms": int(cpu_hours * 3600 * 1000),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": round(cost, 6),
            "model_type": "",
            "detail": detail or {},
        }
        self.events.append(event)
        self._save_event(event)
        return event

    def start_human_step(self, step_name: str, detail: Optional[dict] = None) -> Dict[str, Any]:
        """开始记录人工介入步骤"""
        event = {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "step": step_name,
            "executor": "Human",
            "start_time": datetime.now().isoformat(),
            "end_time": "",
            "duration_ms": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0.0,
            "model_type": "",
            "detail": detail or {},
        }
        self.events.append(event)
        return event

    def end_human_step(self, event: Dict[str, Any], decision: str = ""):
        """结束人工介入步骤"""
        end_time = datetime.now()
        start = datetime.fromisoformat(event["start_time"])
        duration_hours = (end_time - start).total_seconds() / 3600
        event["end_time"] = end_time.isoformat()
        event["duration_ms"] = int(duration_hours * 3600 * 1000)
        event["cost"] = round(duration_hours * HUMAN_PRICE_PER_HOUR, 6)
        if decision:
            event["detail"]["decision"] = decision
        self._save_event(event)

    def record_human_wait(self, step_name: str, wait_seconds: float,
                          decision: str = "", detail: Optional[dict] = None):
        """直接记录人工等待耗时 (用于超时默认)"""
        duration_hours = wait_seconds / 3600
        cost = duration_hours * HUMAN_PRICE_PER_HOUR
        event = {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "step": step_name,
            "executor": "Human",
            "start_time": datetime.now().isoformat(),
            "end_time": datetime.now().isoformat(),
            "duration_ms": int(wait_seconds * 1000),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": round(cost, 6),
            "model_type": "",
            "detail": {**(detail or {}), "decision": decision, "timeout": True},
        }
        self.events.append(event)
        self._save_event(event)
        return event

    def _save_event(self, event: Dict[str, Any]):
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO cost_events
                   (project_id, task_id, step, executor, start_time, end_time,
                    duration_ms, tokens_in, tokens_out, cost, model_type, detail, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event["project_id"], event.get("task_id", ""), event["step"],
                    event["executor"], event.get("start_time", ""), event.get("end_time", ""),
                    event.get("duration_ms", 0), event.get("tokens_in", 0), event.get("tokens_out", 0),
                    event.get("cost", 0.0), event.get("model_type", ""),
                    json.dumps(event.get("detail", {}), ensure_ascii=False, default=str),
                    datetime.now().isoformat()
                )
            )
            conn.commit()
        finally:
            conn.close()

    def generate_report(self) -> Dict[str, Any]:
        """生成运营分析报告"""
        events = self.events or get_cost_events(self.project_id)
        if not events:
            return self._empty_report()

        total_cost = sum(e.get("cost", 0) for e in events)
        total_human_hours = sum(
            e.get("duration_ms", 0) / 3600000 for e in events if e.get("executor") == "Human"
        )
        total_compute_hours = sum(
            e.get("duration_ms", 0) / 3600000 for e in events if e.get("executor") == "Computer"
        )
        ai_local_cost = sum(
            e.get("cost", 0) for e in events if e.get("executor") == "AI-Local"
        )
        ai_cloud_cost = sum(
            e.get("cost", 0) for e in events if e.get("executor") == "AI-Cloud"
        )
        ai_local_tokens = sum(
            e.get("tokens_in", 0) + e.get("tokens_out", 0) for e in events if e.get("executor") == "AI-Local"
        )
        ai_cloud_tokens = sum(
            e.get("tokens_in", 0) + e.get("tokens_out", 0) for e in events if e.get("executor") == "AI-Cloud"
        )

        breakdown = []
        for e in events:
            breakdown.append({
                "step": e.get("step", ""),
                "executor": e.get("executor", ""),
                "duration_ms": e.get("duration_ms", 0),
                "duration_str": self._format_duration(e.get("duration_ms", 0)),
                "cost": round(e.get("cost", 0), 4),
                "tokens_in": e.get("tokens_in", 0),
                "tokens_out": e.get("tokens_out", 0),
            })

        slowest = max(events, key=lambda e: e.get("duration_ms", 0))
        costliest = max(events, key=lambda e: e.get("cost", 0))

        return {
            "project_id": self.project_id,
            "total_cost": round(total_cost, 4),
            "total_human_hours": round(total_human_hours, 4),
            "total_compute_hours": round(total_compute_hours, 4),
            "ai_local_cost": round(ai_local_cost, 4),
            "ai_cloud_cost": round(ai_cloud_cost, 4),
            "ai_local_tokens_m": round(ai_local_tokens / 1e6, 4),
            "ai_cloud_tokens_m": round(ai_cloud_tokens / 1e6, 4),
            "slowest_step": {
                "step": slowest.get("step", ""),
                "executor": slowest.get("executor", ""),
                "duration_ms": slowest.get("duration_ms", 0),
            },
            "costliest_step": {
                "step": costliest.get("step", ""),
                "executor": costliest.get("executor", ""),
                "cost": round(costliest.get("cost", 0), 4),
            },
            "detailed_breakdown": breakdown,
            "generated_at": datetime.now().isoformat(),
        }

    def _empty_report(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "total_cost": 0.0,
            "total_human_hours": 0.0,
            "total_compute_hours": 0.0,
            "ai_local_cost": 0.0,
            "ai_cloud_cost": 0.0,
            "ai_local_tokens_m": 0.0,
            "ai_cloud_tokens_m": 0.0,
            "slowest_step": None,
            "costliest_step": None,
            "detailed_breakdown": [],
            "generated_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _format_duration(duration_ms: int) -> str:
        seconds = duration_ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes = seconds / 60
        if minutes < 60:
            return f"{minutes:.1f}min"
        return f"{minutes/60:.2f}h"


def get_cost_events(project_id: str) -> List[Dict[str, Any]]:
    """查询项目的所有成本事件"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT project_id, task_id, step, executor, start_time, end_time, "
            "duration_ms, tokens_in, tokens_out, cost, model_type, detail "
            "FROM cost_events WHERE project_id = ? ORDER BY id ASC",
            (project_id,)
        ).fetchall()
        return [
            {
                "project_id": r[0], "task_id": r[1], "step": r[2], "executor": r[3],
                "start_time": r[4], "end_time": r[5], "duration_ms": r[6],
                "tokens_in": r[7], "tokens_out": r[8], "cost": r[9],
                "model_type": r[10], "detail": json.loads(r[11]) if r[11] else {},
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_project_cost_report(project_id: str) -> Dict[str, Any]:
    """获取项目成本报告 (独立入口)"""
    tracker = TaskCostTracker(project_id=project_id)
    return tracker.generate_report()


def format_report_markdown(report: Dict[str, Any]) -> str:
    """将成本报告格式化为 Markdown"""
    lines = [
        f"## 项目运营分析报告 ({report['project_id']})",
        "",
        "### 成本概览",
        f"- 总成本: ¥{report['total_cost']:.2f}",
        f"- 人工耗时: {report['total_human_hours']:.2f}h (¥{report['total_human_hours']*HUMAN_PRICE_PER_HOUR:.2f})",
        f"- 电脑耗时: {report['total_compute_hours']:.2f}h (¥{report['total_compute_hours']*COMPUTE_PRICE_PER_HOUR:.2f})",
        f"- AI Token: 本地 {report['ai_local_tokens_m']:.4f}M (¥{report['ai_local_cost']:.2f}) + "
        f"云端 {report['ai_cloud_tokens_m']:.4f}M (¥{report['ai_cloud_cost']:.2f}) = ¥{report['ai_local_cost']+report['ai_cloud_cost']:.2f}",
        "",
        "### 耗时分析",
        "| 环节 | 执行者 | 耗时 | 成本 | 占比 |",
        "|------|--------|------|------|------|",
    ]
    total_cost = report["total_cost"] or 1
    for item in report.get("detailed_breakdown", []):
        ratio = item["cost"] / total_cost * 100
        lines.append(
            f"| {item['step']} | {item['executor']} | {item['duration_str']} | "
            f"¥{item['cost']:.2f} | {ratio:.1f}% |"
        )
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测
    tracker = TaskCostTracker(project_id="PROJ_TEST_001")
    tracker.record_ai_step("需求解析", tokens_in=1200, tokens_out=800, model_type="local")
    tracker.record_ai_step("云端评审", tokens_in=5000, tokens_out=3000, model_type="cloud")
    tracker.record_compute_time("方案求解", cpu_hours=2.3)
    human_event = tracker.start_human_step("方案终选")
    tracker.end_human_step(human_event, decision="approve")
    tracker.record_human_wait("报价确认", wait_seconds=3600, decision="default_medium")

    report = tracker.generate_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n" + format_report_markdown(report))
