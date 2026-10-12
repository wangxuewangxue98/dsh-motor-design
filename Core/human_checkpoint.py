"""
人机协作检查点 - 支持超时默认机制
一人公司四大部门的工作流中,凡需人工确认的节点均通过此模块阻塞等待

v4.03 (F1/F2/F3) 结构修复：
  F1  get_checkpoint 改为 **SQLite 权威**（原「优先读内存 _pending」在进程重启/
      多 worker/跨上下文提交时会让等待方永远读到自身旧副本 → 「已决策却跑满倒计时」）；
  F2  submit_decision 决策值归一化（auto_approve → approve，修掉客户端弹窗倒计时
      到期提交 default_action 被静默拒绝）+ 以 DB 为权威落库，内存退化为只读缓存；
  F3  新增与工作流线程解耦的孤儿检查点清扫线程 start_sweeper()，消除
      「工作流线程死亡 → pending 检查点无人放行 → UI 僵尸弹窗卡死」12 小时类故障。
"""
import json
import sqlite3
import os
import time
import threading
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "KnowledgeBase", "designs_dev.db"),
)

# 默认超时时间 (秒) —— 对应用户方案中的四个部门
DEFAULT_TIMEOUTS = {
    "customer_service_confirm": 2 * 3600,      # 客服:需求确认 2小时
    "customer_service_followup": 30 * 60,      # 客服:追问草稿 30分钟
    "customer_service_weekly": 1 * 3600,       # 客服:周报 1小时
    "customer_service_delivery": 4 * 3600,     # 客服:交付验收 4小时
    "design_final_select": 5 * 60,             # 设计:方案终选 5分钟
    "marketing_evaluate": 72 * 3600,           # 营销:机会评估 72小时
    "marketing_publish": 48 * 3600,            # 营销:内容发布 48小时
    "management_quote": 24 * 3600,             # 管理:报价确认 24小时
    "management_contract": 12 * 3600,          # 管理:合同审核 12小时
    # v2.05.06: 每个节点人机交互检查点 (v4.00.02: 用户在线审阅自动延长的报告确认)
    "requirement_confirm": 3 * 60,             # 需求分析师:需求确认 3分钟
    "base_param_select": 3 * 60,               # 方案生成师:基准参数选定 3分钟
    "design_review": 3 * 60,                   # 方案生成师:方案确认 3分钟
    "report_review": 10 * 60,                  # 报告撰写员:报告确认 10分钟
    "case_motor_review": 30 * 60,              # 案例电机:入库审核 30分钟
}

# v4.07 (F2/F7): AI 团队人机交互统一决策值集合 — 与客户端 4 按钮契约对齐
#   approve(批准) / reject(驳回, 退回上一步) / redo(返回重做, 全流程重做) / end(继续/不停止)
#   移除冗余的 "override": 参数修改通过表格编辑 + 点击 approve 完成 (override_data 回灌)
VALID_DECISIONS = {"approve", "reject", "redo", "end"}

# v4.07 (F10): 默认动作 → 超时实际落库决策 映射 — 所有 auto_* 超时均等价于 approve(放行/继续)
#   同时兼容历史遗留 auto_confirm/auto_first/auto_pass, 保证旧检查点数据不报错
_DEFAULT_ACTION_TO_DECISION = {
    "auto_approve": "approve",
    "auto_confirm": "approve",
    "auto_first": "approve",
    "auto_pass": "approve",
}

# 默认动作映射
DEFAULT_ACTIONS = {
    "customer_service_confirm": "auto_confirm",
    "customer_service_followup": "auto_send",
    "customer_service_weekly": "auto_send",
    "customer_service_delivery": "auto_pass",
    "design_final_select": "auto_first",
    "marketing_evaluate": "auto_ignore",
    "marketing_publish": "auto_hold",
    "management_quote": "auto_medium",
    "management_contract": "auto_pass",
    # v2.05.06: 节点人机交互默认动作 (v4.07 F10: 统一为 auto_approve)
    "requirement_confirm": "auto_approve",     # 超时自动确认需求
    "base_param_select": "auto_approve",       # 超时自动确认基准参数
    "design_review": "auto_approve",           # 超时自动选择最优方案
    "report_review": "auto_approve",           # 超时自动通过报告
    "case_motor_review": "auto_approve",       # 超时自动通过案例入库
}

_lock = threading.Lock()
_pending: Dict[str, Dict[str, Any]] = {}  # 内存中的待处理检查点 (checkpoint_id -> info)

# v4.02.92 (P1): 客户端「流程设置」本机超时偏好 — 按 (project_id, checkpoint_type) 记忆。
# 客户端弹出检查点时会以 as_preference=1 PUT /api/human/checkpoint/<cid>/timeout 应用本机偏好;
# 此处记忆后, 本项目同类型**后续新建**检查点直接继承 (根治: 质量门重生轮新建检查点回落
# DEFAULT_TIMEOUTS, 而客户端 SSE 断流后逐点应用通道失效 ⇒ 每轮多等 ~170s)。
_timeout_prefs: Dict[str, Dict[str, int]] = {}

# v4.00.004: 运行时覆盖支持 (从 workflow_config 加载)
try:
    from workflow_config import get_checkpoint_timeout as _get_wc_timeout
    from workflow_config import is_checkpoint_timeout_set as _wc_timeout_set
except Exception:
    _get_wc_timeout = None
    _wc_timeout_set = None


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS human_checkpoints (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            checkpoint_type TEXT NOT NULL,
            title TEXT,
            description TEXT,
            context TEXT,                  -- JSON
            status TEXT DEFAULT 'pending', -- pending/approved/rejected/overridden/timeout
            decision TEXT,
            -- v4.02.104 (问题2): 决策来源类型 —— 'manual'(人工主动) / 'timeout'(超时自动放行)。
            --   此前 decision 字段把「人工主动批准」与「超时自动放行」都写成 approve,
            --   运营 KPI(人工介入率/超时放行率/审核效率) 全部失真, 无从区分。
            --   存量库由下方 _ensure_decision_type_column() 自动补列(无数据丢失)。
            decision_type TEXT DEFAULT NULL,
            comment TEXT,
            timeout_seconds INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            default_action TEXT,
            cost_event_id INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hc_project ON human_checkpoints(project_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hc_status ON human_checkpoints(status)
    """)
    _ensure_decision_type_column(conn)
    conn.commit()
    return conn


_dec_type_checked = False


def _ensure_decision_type_column(conn=None) -> bool:
    """v4.02.104 (问题2): 幂等补列 —— 给存量 human_checkpoints 表加 decision_type。

    CREATE TABLE IF NOT EXISTS 对**已存在**的表不生效 ⇒ 老库不会自动获得新列。
    此处用 PRAGMA table_info 探测, 缺列则 ALTER TABLE ADD COLUMN (SQLite 支持在线
    加可空列, 不重建表、不丢数据)。进程内只探测一次(_dec_type_checked), 避免每连接
    一次 PRAGMA 开销; 加列后置位。
    返回 True 表示本次调用真的执行了 ALTER TABLE。
    """
    global _dec_type_checked
    if _dec_type_checked:
        return False
    _own = conn is None
    c = conn or _get_conn()
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(human_checkpoints)")}
        if "decision_type" not in cols:
            c.execute("ALTER TABLE human_checkpoints ADD COLUMN decision_type TEXT DEFAULT NULL")
            c.commit()
            print("[human_checkpoint] 已补列 human_checkpoints.decision_type (问题2 修复)")
            _dec_type_checked = True
            return True
        _dec_type_checked = True
        return False
    finally:
        if _own:
            c.close()


def create_checkpoint(project_id: str, checkpoint_type: str,
                      title: str = "", description: str = "",
                      context: Optional[dict] = None,
                      timeout_seconds: Optional[int] = None,
                      default_action: Optional[str] = None) -> str:
    """创建人工检查点,返回 checkpoint_id。

    v4.03: id 原为秒级精度 (``HC_YYYYmmddHHMMSS_<project>``)，同一秒内为同一项目
    连续创建两个检查点会撞 ``UNIQUE constraint failed: human_checkpoints.id`` 并抛
    IntegrityError —— 该异常会沿 ``_push_human_checkpoint`` 冒泡直接中断工作流。
    现保持原格式不变，仅在真的发生冲突时追加微秒后缀重试，不改变既有 id 形态。
    """
    base_id = f"HC_{datetime.now().strftime('%Y%m%d%H%M%S')}_{project_id}"
    checkpoint_id = base_id
    # v4.00.004: 支持运行时覆盖 — workflow_config 优先, 其次 DEFAULT_TIMEOUTS
    # v4.02.110 (C): 优先级裁决修正。orchestrator 创建检查点时**硬传**了
    #   timeout_seconds=hc.DEFAULT_TIMEOUTS[ctype] (如 design_final_select=300 /
    #   report_review=600), 原链路 `_pref or timeout_seconds or _wc_timeout or DEFAULT`
    #   因 timeout_seconds 恒非空而**永远压过**管理端 workflow_config.json 的用户设置
    #   (配 10s 实得 300/600)。现改为: 仅当管理端**显式配置过该 key** (is_checkpoint_timeout_set)
    #   时, 其用户值才压过 orchestrator 硬传的内置默认; 未配置的 key 保留内置默认,
    #   绝不落 get_checkpoint_timeout 的 3600 兜底 (否则 design_final_select 会从 300 劣化到 3600)。
    _explicit_wc_timeout = None
    if _get_wc_timeout:
        if (_wc_timeout_set is None or _wc_timeout_set(checkpoint_type)):
            _explicit_wc_timeout = _get_wc_timeout(checkpoint_type)
        else:
            _explicit_wc_timeout = None
    _pref = (_timeout_prefs.get(project_id) or {}).get(checkpoint_type)
    # 优先级: 本机偏好(_pref) > 管理端显式配置(_explicit_wc_timeout, 可为0) >
    #         orchestrator内置默认(timeout_seconds) > DEFAULT_TIMEOUTS
    # 用 if/elif 而非 or 链, 以便"管理端显式配置为 0"的边界也能被正确采用。
    if _pref:
        timeout = int(_pref)
    elif _explicit_wc_timeout is not None:
        timeout = int(_explicit_wc_timeout)
    elif timeout_seconds:
        timeout = int(timeout_seconds)
    else:
        timeout = DEFAULT_TIMEOUTS.get(checkpoint_type, 3600)
    default_act = default_action or DEFAULT_ACTIONS.get(checkpoint_type, "auto_pass")
    created_at = datetime.now().isoformat()

    conn = _get_conn()
    try:
        for _attempt in range(5):
            try:
                conn.execute(
                    """INSERT INTO human_checkpoints
                       (id, project_id, checkpoint_type, title, description, context,
                        status, timeout_seconds, created_at, default_action)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (checkpoint_id, project_id, checkpoint_type, title, description,
                     json.dumps(context or {}, ensure_ascii=False, default=str),
                     "pending", timeout, created_at, default_act)
                )
                break
            except sqlite3.IntegrityError:
                # 同一秒内同项目重复创建 → 追加微秒后缀去重
                checkpoint_id = f"{base_id}_{datetime.now().strftime('%f')}"
        else:
            raise sqlite3.IntegrityError(f"检查点 id 连续冲突, 无法创建: {base_id}")
        conn.commit()
    finally:
        conn.close()

    with _lock:
        _pending[checkpoint_id] = {
            "id": checkpoint_id,
            "project_id": project_id,
            "checkpoint_type": checkpoint_type,
            "title": title,
            "description": description,
            "status": "pending",
            "created_at": created_at,
            "timeout_seconds": timeout,
            "default_action": default_act,
            "context": context or {},
        }
    return checkpoint_id


def _normalize_decision(decision: str) -> str:
    """v4.03 (F2/F6): 决策值归一化 — 把 default_action 值映射为有效决策。

    背景（本次事故 RC1 的直接成因）：客户端「人工确认弹窗」自身的倒计时到期时，
    会以 ``default_action``（如 ``auto_approve``）调用 ``_on_decision(default_action)``
    提交决策（客户端 ai_team_panel.py 的 ``_update_countdown`` → ``_do_close(default_action)``）。
    而 ``VALID_DECISIONS`` 只含 approve/reject/redo/end → 原实现在第一行就 ``return False``
    **静默拒绝**，DB 状态永远停在 pending，工作流侧的 ``wait_for_decision`` 感知不到任何
    决策，只能跑满整个超时（事故现象：23:39:17 提交 auto_approve → 23:42:16 才超时放行）。

    返回：归一化后的有效决策；无法归一化时返回空串（调用方据此拒绝）。
    """
    d = (decision or "").strip()
    if d in VALID_DECISIONS:
        return d
    # auto_approve / auto_confirm / auto_first / auto_pass → approve
    return _DEFAULT_ACTION_TO_DECISION.get(d, "")


def _get_from_db(checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """从 SQLite 读取检查点（权威来源）。不存在时返回 None。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, project_id, checkpoint_type, title, description, context, "
            "status, decision, decision_type, comment, timeout_seconds, created_at, "
            "decided_at, default_action "
            "FROM human_checkpoints WHERE id = ?",
            (checkpoint_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "project_id": row[1], "checkpoint_type": row[2],
            "title": row[3], "description": row[4],
            "context": json.loads(row[5]) if row[5] else {},
            "status": row[6], "decision": row[7], "decision_type": row[8],
            "comment": row[9], "timeout_seconds": row[10], "created_at": row[11],
            "decided_at": row[12], "default_action": row[13],
        }
    finally:
        conn.close()


def get_checkpoint(checkpoint_id: str) -> Optional[Dict[str, Any]]:
    """获取检查点详情。

    v4.03 (F1): **SQLite 为唯一权威来源**。原实现「优先读内存 _pending」，
    在下列任一情况下等待方会一直读到自身的旧内存副本（status=pending）：
      - 后端进程重启（双进程守卫自动重启、OOM 回收、系统休眠）→ 新进程
        ``_pending`` 为空，而旧进程里阻塞的 ``wait_for_decision`` 已消失；
      - 未来多 worker 部署（waitress/gunicorn 多进程）→ 各进程 ``_pending`` 不共享；
      - 决策由工作流之外的上下文提交。
    现改为：先读 DB；仅当 DB 中不存在该行时，才回落内存副本（兼容历史极少数
    「只建内存、未落库」的调用路径）。
    """
    cp = _get_from_db(checkpoint_id)
    if cp is not None:
        return cp
    with _lock:
        if checkpoint_id in _pending:
            return dict(_pending[checkpoint_id])
    return None


def submit_decision(checkpoint_id: str, decision: str,
                    comment: str = "", cost_event_id: Optional[int] = None,
                    decision_type: Optional[str] = None) -> bool:
    """提交人工决策。

    v4.03 (F2): ① 决策值先归一化（``auto_approve`` 等 default_action 值 → ``approve``），
    修掉「客户端弹窗倒计时到期提交 auto_approve 被静默拒绝」这一 RC1 直接成因；
    ② **以 DB 为权威落库**，内存 ``_pending`` 退化为只读缓存，不再参与判定 —— 消除
    「跨上下文/跨进程内存不同步」导致的「已决策却跑满倒计时」。

    v4.02.104 (问题2): 新增 ``decision_type`` —— 结构化区分「人工主动决策」与
    「超时自动放行」，修复运营 KPI 失真。
      - 显式传入时以传入值为准（``wait_for_decision``/清扫器传 "timeout"）；
      - 未传入时按**原始决策值**自动推定：``auto_*``（客户端弹窗自身倒计时到期提交的
        default_action）⇒ "timeout"；其余合法决策值 ⇒ "manual"。
    返回：是否成功落库。
    """
    _raw = (decision or "").strip()
    if decision_type is None:
        # auto_* 是「弹窗倒计时到期自动提交的默认动作」⇒ 判定为超时放行
        decision_type = "timeout" if _raw and _raw not in VALID_DECISIONS else "manual"
    decision = _normalize_decision(_raw)
    if not decision:
        return False

    decided_at = datetime.now().isoformat()

    # 1) DB 为权威: 仅当该检查点仍为 pending 时落库
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE human_checkpoints
               SET status = ?, decision = ?, decision_type = ?, comment = ?,
                   decided_at = ?, cost_event_id = ?
               WHERE id = ? AND status = 'pending'""",
            (decision, decision, decision_type, comment, decided_at,
             cost_event_id, checkpoint_id)
        )
        updated = cursor.rowcount > 0
        conn.commit()
    finally:
        conn.close()

    # 2) 同步本进程内存缓存 (best-effort, 不参与判定)
    with _lock:
        _cached = _pending.get(checkpoint_id)
        if _cached is not None:
            if updated:
                _cached.update({
                    "status": decision, "decision": decision,
                    "decision_type": decision_type,
                    "comment": comment, "decided_at": decided_at,
                })
                _pending.pop(checkpoint_id, None)  # 已决策, 释放内存

    # 3) 兼容「仅内存创建、未落库」的极少数路径: DB 无该行时以内存状态判定
    if not updated and _get_from_db(checkpoint_id) is None:
        with _lock:
            _cached = _pending.get(checkpoint_id)
            if _cached is not None and _cached.get("status") in ("", "pending"):
                _cached.update({
                    "status": decision, "decision": decision,
                    "decision_type": decision_type,
                    "comment": comment, "decided_at": decided_at,
                })
                _pending.pop(checkpoint_id, None)
                updated = True

    return updated


def wait_for_decision(checkpoint_id: str, poll_interval: float = 1.0,
                      on_progress: Optional[callable] = None) -> Dict[str, Any]:
    """
    阻塞等待人工决策或超时。
    返回: {"status": "approved"/"timeout"/..., "decision": ..., "comment": ..., "wait_seconds": ...}
    """
    cp = get_checkpoint(checkpoint_id)
    if not cp:
        return {"status": "not_found", "decision": "", "comment": "", "wait_seconds": 0}

    timeout_seconds = cp.get("timeout_seconds", 3600)
    default_action = cp.get("default_action", "auto_pass")
    checkpoint_type = cp.get("checkpoint_type", "")
    start = datetime.now()

    while True:
        # 检查是否已有决策
        fresh = get_checkpoint(checkpoint_id)
        # v4.01.019: 每次循环重读超时时间, 使人工中途调整立即生效
        if fresh:
            timeout_seconds = fresh.get("timeout_seconds", timeout_seconds)
            default_action = fresh.get("default_action", default_action)
        if fresh and fresh.get("status") not in ("pending", ""):
            decided_at = fresh.get("decided_at")
            wait_seconds = (datetime.fromisoformat(decided_at) - start).total_seconds() if decided_at else 0
            return {
                "status": fresh["status"],
                "decision": fresh.get("decision", ""),
                # v4.02.104 (问题2): 透出决策来源 (manual/timeout), 供上层统计不再失真
                "decision_type": fresh.get("decision_type"),
                "comment": fresh.get("comment", ""),
                "wait_seconds": wait_seconds,
            }

        elapsed = (datetime.now() - start).total_seconds()
        if elapsed >= timeout_seconds:
            # v4.07 (F10): 默认动作统一为 auto_approve, 超时实际落库为有效决策 "approve"
            #   (避免 submit_decision 因非 VALID_DECISIONS 而静默拒绝, 修复 v4.07 收敛默认动作时的回归)
            _timeout_decision = _DEFAULT_ACTION_TO_DECISION.get(default_action, "approve")
            # v4.02.104 (问题2): 显式标记为超时放行 —— 与人工主动批准在 DB 层可区分
            submit_decision(checkpoint_id, _timeout_decision,
                            comment=f"超时 {int(elapsed)} 秒, 自动执行默认动作",
                            cost_event_id=None, decision_type="timeout")
            return {
                "status": _timeout_decision,
                "decision": _timeout_decision,
                "decision_type": "timeout",
                "comment": f"超时 {int(elapsed)} 秒, 自动执行默认动作",
                "wait_seconds": elapsed,
                "timeout": True,
            }

        if on_progress:
            remaining = int(timeout_seconds - elapsed)
            on_progress(remaining)

        time.sleep(poll_interval)


def list_pending_checkpoints(project_id: Optional[str] = None) -> list:
    """列出待处理检查点"""
    conn = _get_conn()
    try:
        if project_id:
            rows = conn.execute(
                "SELECT id, project_id, checkpoint_type, title, status, created_at, timeout_seconds "
                "FROM human_checkpoints WHERE status = 'pending' AND project_id = ? "
                "ORDER BY created_at DESC",
                (project_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, checkpoint_type, title, status, created_at, timeout_seconds "
                "FROM human_checkpoints WHERE status = 'pending' "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r[0], "project_id": r[1], "checkpoint_type": r[2],
                "title": r[3], "status": r[4], "created_at": r[5],
                "timeout_seconds": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_decision_context(checkpoint_id: str) -> dict:
    """获取决策上下文,供前端展示"""
    cp = get_checkpoint(checkpoint_id)
    if not cp:
        return {}
    return {
        "checkpoint_id": cp.get("id", checkpoint_id),
        "project_id": cp.get("project_id", ""),
        "title": cp.get("title", ""),
        "description": cp.get("description", ""),
        "options": sorted(VALID_DECISIONS),
        "context": cp.get("context", {}),
        "timeout_seconds": cp.get("timeout_seconds", 3600),
        "default_action": cp.get("default_action", "auto_pass"),
    }


def update_checkpoint_context(checkpoint_id: str, context: dict) -> bool:
    """v4.01.014: 更新检查点的 context 字段 (用于保存用户弹窗修改的覆盖数据)"""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE human_checkpoints SET context = ? WHERE id = ?",
            (json.dumps(context, ensure_ascii=False, default=str), checkpoint_id)
        )
        conn.commit()
        updated = cursor.rowcount > 0
        if updated:
            with _lock:
                if checkpoint_id in _pending:
                    _pending[checkpoint_id]["context"] = context
        return updated
    finally:
        conn.close()


def update_checkpoint_timeout(checkpoint_id: str, timeout_seconds: int,
                              as_preference: bool = False) -> tuple[bool, str]:
    """v4.01.020: 更新检查点的超时时间 (用户弹窗手动延长)
    v4.02.92 (P1): as_preference=True 时同时把该值记忆为 (project_id, checkpoint_type)
    的「本机超时偏好」—— 客户端「流程设置」偏好通道; 后续同项目同类型新建检查点
    在 create_checkpoint 处自动继承, 不再回落 DEFAULT_TIMEOUTS。
    返回: (success, reason)  reason 为空表示成功, 否则为 'not_found'/'already_decided'
    """
    timeout_seconds = max(10, int(timeout_seconds))
    conn = _get_conn()
    try:
        # 先确认 checkpoint 存在与状态, 便于给出明确错误
        row = conn.execute(
            "SELECT status, project_id, checkpoint_type FROM human_checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if not row:
            return False, "not_found"
        if row[0] != "pending":
            return False, "already_decided"
        conn.execute(
            "UPDATE human_checkpoints SET timeout_seconds = ? WHERE id = ? AND status = 'pending'",
            (timeout_seconds, checkpoint_id)
        )
        conn.commit()
        with _lock:
            if checkpoint_id in _pending:
                _pending[checkpoint_id]["timeout_seconds"] = timeout_seconds
            if as_preference and row[1] and row[2]:
                _timeout_prefs.setdefault(str(row[1]), {})[str(row[2])] = int(timeout_seconds)
        return True, ""
    finally:
        conn.close()


def get_timeout_preference(project_id: str, checkpoint_type: str) -> int:
    """v4.02.92 (P1): 读取 (project, type) 的本机超时偏好, 无记录返回 0 (供回归/观测)。"""
    return int((_timeout_prefs.get(project_id) or {}).get(checkpoint_type) or 0)


def get_project_checkpoints(project_id: str) -> list:
    """v3.00.28: 查询项目的所有人工检查点 (供报告生成时汇总审核意见)"""
    if not project_id:
        return []
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, project_id, checkpoint_type, title, description, context, "
            "status, decision, decision_type, comment, timeout_seconds, created_at, "
            "decided_at, default_action "
            "FROM human_checkpoints WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,)
        ).fetchall()
        result = []
        for row in rows:
            ctx = json.loads(row[5]) if row[5] else {}
            result.append({
                "id": row[0], "project_id": row[1], "checkpoint_type": row[2],
                "title": row[3], "description": row[4],
                "context": ctx,
                "status": row[6], "decision": row[7], "decision_type": row[8],
                "comment": row[9], "timeout_seconds": row[10], "created_at": row[11],
                "decided_at": row[12], "default_action": row[13],
            })
        return result
    finally:
        conn.close()


def touch_user_activity():
    """v4.01: (已废弃) 在线延长功能已移除, 此函数保留为空以便兼容旧 API 调用."""
    pass


# ============================================================================
# v4.03 (F3): 孤儿检查点清扫 — 消除 RC2「人工检查点永久卡死」
# ============================================================================
# 事故：工作流等待线程若随 SSE 请求线程 / 进程一起消失（断流、进程重启、守护线程
# 被回收），它创建的 pending 检查点就成为「孤儿」——因为『超时自动放行』逻辑
# 只活在工作流线程内部的 wait_for_decision 里，没有任何活循环再去处理它。
# 客户端因此长期显示僵尸确认弹窗（本次事故：report_review 卡死约 12 小时）。
#
# 本清扫器与工作流线程**完全解耦**：独立后台线程按 created_at + timeout_seconds
# 自行判定超时，并以 default_action（归一化后）落库，使状态收敛、UI 解冻。
SWEEPER_INTERVAL = int(os.environ.get("HC_SWEEPER_INTERVAL", "20") or 20)
SWEEPER_ENABLED = (os.environ.get("HC_SWEEPER_ENABLED", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off")
_sweeper_started = False
_sweeper_lock = threading.Lock()


def list_orphan_checkpoints(grace_seconds: int = 0) -> list:
    """列出「已超时但仍 pending」的孤儿检查点（只读，供状态查询/诊断/单测）。"""
    now = datetime.now()
    out = []
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, project_id, checkpoint_type, created_at, timeout_seconds, default_action "
            "FROM human_checkpoints WHERE status = 'pending'"
        ).fetchall()
    finally:
        conn.close()
    for cid, pid, ctype, created_at, timeout_seconds, default_action in rows:
        try:
            created = datetime.fromisoformat(str(created_at))
        except Exception:
            continue
        timeout = int(timeout_seconds or 0) or DEFAULT_TIMEOUTS.get(ctype, 3600)
        overdue = (now - created).total_seconds() - timeout - max(0, int(grace_seconds))
        if overdue >= 0:
            out.append({
                "id": cid, "project_id": pid, "checkpoint_type": ctype,
                "created_at": str(created_at), "timeout_seconds": timeout,
                "overdue_seconds": int(overdue),
                "default_action": default_action,
                "resolved_decision": _DEFAULT_ACTION_TO_DECISION.get(default_action or "", "approve"),
            })
    return out


def sweep_expired_checkpoints(grace_seconds: int = 0) -> list:
    """扫描并放行「已超时但仍 pending」的检查点，返回被清扫的 checkpoint_id 列表。

    grace_seconds: 额外宽限秒数（0 = 与 wait_for_decision 完全同一口径）。
    并发安全：落库使用 `WHERE id=? AND status='pending'` 原子更新 —— 即使工作流侧的
    wait_for_decision 恰好在同一刻超时放行，也只有一个 UPDATE 会生效，不会重复处理。
    决策值经 _normalize_decision 归一化，因此 auto_* 默认动作不会被静默拒绝。
    """
    now = datetime.now()
    swept = []
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, created_at, timeout_seconds, default_action, checkpoint_type "
            "FROM human_checkpoints WHERE status = 'pending'"
        ).fetchall()
        for cid, created_at, timeout_seconds, default_action, ctype in rows:
            try:
                created = datetime.fromisoformat(str(created_at))
            except Exception:
                continue
            timeout = int(timeout_seconds or 0) or DEFAULT_TIMEOUTS.get(ctype, 3600)
            elapsed = (now - created).total_seconds()
            if elapsed < timeout + max(0, int(grace_seconds)):
                continue
            decision = _normalize_decision(default_action or "") or "approve"
            cur = conn.execute(
                "UPDATE human_checkpoints SET status=?, decision=?, decision_type=?, "
                "comment=?, decided_at=?, "
                "cost_event_id=NULL WHERE id=? AND status='pending'",
                (decision, decision, "timeout",
                 f"超时 {int(elapsed)} 秒未收到决策, 由后台清扫器按默认动作自动放行",
                 now.isoformat(), cid))
            if cur.rowcount > 0:
                swept.append(cid)
                with _lock:
                    _pending.pop(cid, None)
        conn.commit()
    finally:
        conn.close()
    return swept


def start_sweeper(interval: Optional[int] = None) -> bool:
    """幂等启动后台孤儿检查点清扫线程（daemon）。重复调用只生效一次。

    返回 True 表示本次调用真正启动了线程；False 表示已在运行/被禁用。
    环境变量：HC_SWEEPER_ENABLED=0 可整体禁用；HC_SWEEPER_INTERVAL 调整周期(秒)。
    """
    global _sweeper_started
    if not SWEEPER_ENABLED:
        return False
    with _sweeper_lock:
        if _sweeper_started:
            return False
        _sweeper_started = True

    _interval = max(5, int(interval or SWEEPER_INTERVAL))

    def _loop():
        while True:
            try:
                time.sleep(_interval)
                swept = sweep_expired_checkpoints()
                if swept:
                    print(f"[hc-sweeper] 已放行 {len(swept)} 个超时未决(孤儿)检查点: "
                          f"{swept[:3]}{' ...' if len(swept) > 3 else ''}")
            except Exception as _e:
                # 清扫器必须永不退出: 单轮异常忽略, 下一轮继续
                print(f"[hc-sweeper] 清扫异常(已忽略, 下轮重试): {_e}")

    threading.Thread(target=_loop, name="hc-checkpoint-sweeper", daemon=True).start()
    print(f"[hc-sweeper] 已启动 (interval={_interval}s)")
    return True


if __name__ == "__main__":
    # 自测
    cp_id = create_checkpoint(
        project_id="PROJ_TEST_002",
        checkpoint_type="design_final_select",
        title="方案终选确认",
        description="请审阅支持报告与反对报告,选择最终方案",
        context={"review": "支持...", "con_report": "反对..."},
        timeout_seconds=5
    )
    print("created:", cp_id)
    # 模拟外部在2秒后提交决策
    def submit_later():
        time.sleep(2)
        submit_decision(cp_id, "approve", "同意AI推荐")
    threading.Thread(target=submit_later, daemon=True).start()
    result = wait_for_decision(cp_id, poll_interval=0.5, on_progress=lambda r: print(f"remaining {r}s"))
    print("result:", result)
