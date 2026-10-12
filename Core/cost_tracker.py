"""
成本核算模块 - 记录每步耗时和 token 消耗
让人机协作的运营成本可量化、可优化
"""
import json
import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "KnowledgeBase", "designs_dev.db"),
)

# 计费标准 (元/百万 token)
CLOUD_INPUT_PRICE = 3.0       # 云端输入
CLOUD_OUTPUT_PRICE = 6.0      # 云端输出
LOCAL_INPUT_PRICE = 0.6       # 本地输入 (按云模型的 1/5)
LOCAL_OUTPUT_PRICE = 1.2      # 本地输出
COMPUTE_PRICE_PER_HOUR = 1.0  # 电脑程序耗时
HUMAN_PRICE_PER_HOUR = 20.0   # 人工耗时


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
        """生成运营分析报告。

        v4.02.104 (问题4): **量纲统一** —— 对外口径约定：
          - DB (`cost_events.cost`) / 内部计算：**元 (CNY)**
          - 运营飞轮 (`operation_flywheel.record_project_summary` 的 total_cost_cents)：**分**
        本方法同时给出两者，避免调用方各自 *100 造成口径漂移：
          - ``total_cost``       : 元 (向后兼容)
          - ``total_cost_cents`` : 分 (权威整型值, 一元=100分)
        单项 breakdown 亦附 ``cost_cents``。
        """
        events = self.events or get_cost_events(self.project_id)
        if not events:
            return self._empty_report()

        def _cents(v) -> int:
            """元 → 分 (四舍五入取整); 非法值按 0。"""
            try:
                return int(round(float(v or 0) * 100))
            except (TypeError, ValueError):
                return 0

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
            _c = e.get("cost", 0)
            breakdown.append({
                "step": e.get("step", ""),
                "executor": e.get("executor", ""),
                "duration_ms": e.get("duration_ms", 0),
                "duration_str": self._format_duration(e.get("duration_ms", 0)),
                "cost": round(_c, 4),
                "cost_cents": _cents(_c),      # v4.02.104 (问题4): 分口径
                "tokens_in": e.get("tokens_in", 0),
                "tokens_out": e.get("tokens_out", 0),
            })

        slowest = max(events, key=lambda e: e.get("duration_ms", 0))
        costliest = max(events, key=lambda e: e.get("cost", 0))

        return {
            "project_id": self.project_id,
            "total_cost": round(total_cost, 4),
            # v4.02.104 (问题4): 分口径权威值 —— 调用方 (运营飞轮) 直接取用, 不再自行 *100
            "total_cost_cents": _cents(total_cost),
            "ai_local_cost_cents": _cents(ai_local_cost),
            "ai_cloud_cost_cents": _cents(ai_cloud_cost),
            "currency": "CNY",
            "cost_unit": "cost(元) / cost_cents(分)",
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
                "cost_cents": _cents(costliest.get("cost", 0)),
            },
            "detailed_breakdown": breakdown,
            "generated_at": datetime.now().isoformat(),
        }

    def _empty_report(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "total_cost": 0.0,
            "total_cost_cents": 0,        # v4.02.104 (问题4)
            "ai_local_cost_cents": 0,
            "ai_cloud_cost_cents": 0,
            "currency": "CNY",
            "cost_unit": "cost(元) / cost_cents(分)",
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


# v3.00.28: 流程阶段映射 (与 orchestrator._PHASE_MAP 保持一致)
_PHASE_MAP = {
    "parse_requirement": "用户需求分析",
    "hc_requirement": "用户需求分析",
    "generate_base_params": "方案生成",
    "hc_base_param": "方案生成",
    "generate_designs": "方案生成",
    "hc_design": "方案生成",
    "review_solutions": "方案评审",
    "convergence_review": "方案评审",
    "decision_gate": "方案评审",
    "human_approval": "方案评审",
    "generate_report": "报告撰写",
    "hc_report": "报告撰写",
}
_PHASE_ORDER = ["用户需求分析", "方案生成", "方案评审", "报告撰写"]


def _fmt_duration(ms: int) -> str:
    """将毫秒格式化为中文时长"""
    if not ms:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes = seconds / 60
    rem_seconds = int((ms % 60000) / 1000)
    if minutes < 60:
        return f"{minutes:.1f}分{rem_seconds}秒"
    return f"{minutes/60:.1f}小时{minutes % 60:.0f}分"


def _build_timing_section_md(report: Dict[str, Any]) -> str:
    """v3.00.28: 从 report.detailed_breakdown 构建流程耗时统计章节"""
    breakdown = report.get("detailed_breakdown", [])
    if not breakdown:
        return ""

    # 按阶段聚合
    phases = {}
    for item in breakdown:
        step = item.get("step", "")
        phase = _PHASE_MAP.get(step, "其他")
        if phase not in phases:
            phases[phase] = {
                "system_ms": 0, "human_ms": 0,
                "tokens_in": 0, "tokens_out": 0,
            }
        executor = item.get("executor", "")
        duration = item.get("duration_ms", 0)
        if executor == "Human":
            phases[phase]["human_ms"] += duration
        else:
            phases[phase]["system_ms"] += duration
        phases[phase]["tokens_in"] += item.get("tokens_in", 0)
        phases[phase]["tokens_out"] += item.get("tokens_out", 0)

    lines = ["### AI 团队流程耗时统计结果及分析", ""]
    lines.append("本次 AI 团队工作流各阶段时间消耗统计如下:")
    lines.append("")
    lines.append("| 阶段 (Phase) | 系统自动耗时 (System) | 人工耗时 (Human) | 阶段小计 (Subtotal) | Token用量 (Tokens) |")
    lines.append("|------|--------------|----------|----------|-----------|")

    total_sys = 0
    total_hum = 0
    total_tokens = 0

    for name in _PHASE_ORDER:
        if name not in phases:
            continue
        p = phases[name]
        sys_ms = p["system_ms"]
        hum_ms = p["human_ms"]
        sub_ms = sys_ms + hum_ms
        tokens = p["tokens_in"] + p["tokens_out"]
        total_sys += sys_ms
        total_hum += hum_ms
        total_tokens += tokens
        lines.append(f"| {name} | {_fmt_duration(sys_ms)} | {_fmt_duration(hum_ms)} | "
                     f"{_fmt_duration(sub_ms)} | {tokens/1000:.1f}K |")

    # 其他阶段 (如有不在标准4阶段中的)
    for name, p in phases.items():
        if name in _PHASE_ORDER:
            continue
        sys_ms = p["system_ms"]
        hum_ms = p["human_ms"]
        sub_ms = sys_ms + hum_ms
        tokens = p["tokens_in"] + p["tokens_out"]
        total_sys += sys_ms
        total_hum += hum_ms
        total_tokens += tokens
        lines.append(f"| {name} | {_fmt_duration(sys_ms)} | {_fmt_duration(hum_ms)} | "
                     f"{_fmt_duration(sub_ms)} | {tokens/1000:.1f}K |")

    total_sub = total_sys + total_hum
    lines.append(f"| **流程总耗时** | **{_fmt_duration(total_sys)}** | "
                 f"**{_fmt_duration(total_hum)}** | **{_fmt_duration(total_sub)}** | "
                 f"**{total_tokens/1000:.1f}K** |")
    lines.append("")
    lines.append("**耗时分析:**")
    lines.append("")
    lines.append(f"- **系统自动耗时** = AI 大模型推理 + 计算机求解(电磁仿真等)耗时, "
                 f"本次总计 **{_fmt_duration(total_sys)}**")
    lines.append(f"- **人工耗时** = 人工确认/审核检查点等待决策耗时, "
                 f"本次总计 **{_fmt_duration(total_hum)}**")
    if total_sub:
        sys_ratio = total_sys / total_sub * 100
        hum_ratio = total_hum / total_sub * 100
        lines.append(f"- 系统耗时占比 {sys_ratio:.1f}%, 人工耗时占比 {hum_ratio:.1f}%")
    if total_tokens:
        lines.append(f"- AI Token 消耗总计 **{total_tokens/1000:.1f}K**, "
                     f"展示了 AI 团队的推理强度")
    lines.append("")
    return "\n".join(lines)


def _build_reviews_section_md(project_id: str) -> str:
    """v3.00.28: 构建人工审核意见汇总及分析章节"""
    if not project_id:
        return ""
    try:
        from human_checkpoint import get_project_checkpoints
    except ImportError:
        return ""
    reviews = get_project_checkpoints(project_id)
    if not reviews:
        return ""

    lines = ["### 流程中人工审核意见汇总及分析", ""]
    lines.append(f"本次 AI 团队工作流共触发 **{len(reviews)}** 个人工审核检查点,")
    lines.append("审核详情如下:")
    lines.append("")
    lines.append("| # | 检查点类型 | 标题 | 决策 | 决策来源 | 状态 | 用户备注 |")
    lines.append("|---|------------|------|------|----------|------|----------|")

    decision_cn = {
        "approve": "批准", "reject": "反对", "override": "覆盖",
        "modify": "修改", "confirm": "确认", "ignore": "忽略",
        "hold": "暂缓", "redo": "重做", "end": "结束",
        "auto_pass": "自动通过", "auto_confirm": "自动确认",
        "auto_first": "自动选择第一", "auto_reject": "自动反对",
        "auto_medium": "自动中庸", "auto_skip": "自动跳过",
        "auto_hold": "自动暂缓", "auto_ignore": "自动忽略",
        "auto_end": "自动结束", "auto_redo": "自动重做",
    }

    # v4.02.104 (问题2): 统一判据 —— 结构化 decision_type (manual/timeout)
    def _is_timeout(cp: dict) -> bool:
        _dt = cp.get("decision_type")
        if _dt in ("manual", "timeout"):
            return _dt == "timeout"
        return (cp.get("status") == "timeout"
                or (cp.get("decision") or "").startswith("auto_"))

    for i, cp in enumerate(reviews, 1):
        cp_type = cp.get("checkpoint_type", "—")
        title = cp.get("title", "—")
        decision = cp.get("decision", "")
        status = cp.get("status", "—")
        comment = (cp.get("comment") or "").replace("\n", " ").replace("|", "/")[:80]
        decision_disp = decision_cn.get(decision, decision or "—")
        _src = ("人工主动" if not _is_timeout(cp) else "超时自动") if decision else "—"
        lines.append(f"| {i} | {cp_type} | {title} | {decision_disp} | {_src} | "
                     f"{status} | {comment or '—'} |")

    # 统计
    decided = [cp for cp in reviews if cp.get("decision") and not _is_timeout(cp)]
    timeout_cps = [cp for cp in reviews if _is_timeout(cp)]
    rejected_cps = [cp for cp in reviews if cp.get("decision") == "reject"]
    redo_cps = [cp for cp in reviews if cp.get("decision") == "redo"]

    lines.append("")
    lines.append("**审核行为分析:**")
    lines.append("")
    lines.append(f"- **总审核检查点**: {len(reviews)} 个")
    lines.append(f"- **人工主动决策**: {len(decided)} 次 "
                 f"({len(decided)/max(len(reviews),1)*100:.0f}%)")
    if timeout_cps:
        lines.append(f"- **超时自动默认**: {len(timeout_cps)} 次 "
                     f"({len(timeout_cps)/max(len(reviews),1)*100:.0f}%) — "
                     f"建议关注AI预判质量或缩短超时阈值")
    if rejected_cps:
        lines.append(f"- **驳回决策**: {len(rejected_cps)} 次, "
                     f"AI 推荐方案可能未完全匹配用户预期")
    if redo_cps:
        lines.append(f"- **重做决策**: {len(redo_cps)} 次, "
                     f"部分环节需要重新生成以满足要求")
    lines.append("")

    # 列出详细审核意见
    detail_comments = [(cp.get("title"), cp.get("comment"))
                       for cp in reviews
                       if cp.get("comment") and not _is_timeout(cp)]
    if detail_comments:
        lines.append("**详细审核意见:**")
        lines.append("")
        for title, comment in detail_comments:
            lines.append(f"- **{title}**: {comment}")
        lines.append("")
    return "\n".join(lines)


def format_report_markdown(report: Dict[str, Any]) -> str:
    """将成本报告格式化为 Markdown"""
    lines = [
        f"## 项目运营分析报告 ({report['project_id']})",
        "",
        "### 成本概览 (Cost Overview)",
        f"- 总成本 (Total): ¥{report['total_cost']:.2f} "
        f"= {int(round(float(report.get('total_cost', 0) or 0) * 100))} 分",
        f"- 人工耗时 (Human): {report['total_human_hours']:.2f}h (¥{report['total_human_hours']*HUMAN_PRICE_PER_HOUR:.2f})",
        f"- 电脑耗时 (Compute): {report['total_compute_hours']:.2f}h (¥{report['total_compute_hours']*COMPUTE_PRICE_PER_HOUR:.2f})",
        f"- AI Token: 本地 (Local) {report['ai_local_tokens_m']:.4f}M (¥{report['ai_local_cost']:.2f}) + "
        f"云端 (Cloud) {report['ai_cloud_tokens_m']:.4f}M (¥{report['ai_cloud_cost']:.2f}) = ¥{report['ai_local_cost']+report['ai_cloud_cost']:.2f}",
        "",
        "### 耗时分析 (Timing Breakdown)",
        "| 环节 (Step) | 执行者 (Executor) | 耗时 (Duration) | 成本 (Cost) | 占比 (Share) |",
        "|------|--------|------|------|------|",
    ]
    total_cost = report["total_cost"] or 1
    for item in report.get("detailed_breakdown", []):
        ratio = item["cost"] / total_cost * 100
        lines.append(
            f"| {item['step']} | {item['executor']} | {item['duration_str']} | "
            f"¥{item['cost']:.2f} | {ratio:.1f}% |"
        )

    # v3.00.28: 新增 AI 团队流程耗时统计 + 人工审核意见汇总
    timing_md = _build_timing_section_md(report)
    if timing_md:
        lines.append("")
        lines.append(timing_md)

    reviews_md = _build_reviews_section_md(report.get("project_id", ""))
    if reviews_md:
        lines.append("")
        lines.append(reviews_md)

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
