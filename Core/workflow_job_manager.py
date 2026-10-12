"""
工作流作业管理器 (v4.03 F4) — 把「工作流执行」与「SSE 连接」彻底解耦
================================================================================

修复的故障（RC2 结构性成因）
---------------------------
原先 ``/api/workflow/run`` 把 ``orchestrator.run_streaming(...)`` 直接驱动在
Flask 响应生成器里（``Response(stream_with_context(generate()))``）。这带来两个后果：

1. SSE 一旦断开（代理空闲超时、客户端网络抖动、用户切网），响应生成器被关闭，
   工作流的产出无处可去；工作流内的人工检查点等待逻辑（wait_for_decision 的
   「超时自动放行」分支）一旦随线程消失，其 pending 检查点就成了**孤儿**，
   没有任何活循环再会去处理它 —— 本次事故中 report_review 因此卡死约 12 小时。
2. 客户端其实**已经实现了**断线重连契约（``ai_client._handle_sse_disconnect``
   会 ``GET /api/workflow/events?last_event_id=N`` 续传），但后端从未提供该端点，
   于是重连必然 404 → 4 次退避后放弃 → 退化为 5 分钟检查点轮询兜底。

本模块提供的能力
----------------
* ``WorkflowJob``：在**独立后台线程**中执行工作流；事件先落入带 ``event_id`` 的
  内存缓冲，再由任意数量的订阅者（SSE 连接）读取。断开连接只终止订阅者，
  作业线程继续运行（含检查点等待与超时放行）。
* ``WorkflowJob.subscribe(last_event_id)``：先重放 ``event_id > last_event_id`` 的历史
  事件，再阻塞等待新事件 → 天然支持「断线续传」，且不会重复投递。
* 每个事件都携带 ``event_id``（单调递增）与 ``job_id``；客户端 ``_parse_sse_stream``
  正是靠 ``event["event_id"]`` 维护水位线的，因此**无需改动客户端**即可续传。
* 心跳事件（``type=heartbeat``）**不带** ``event_id``：避免非语义事件推进客户端水位线。

进程边界说明：作业线程为 daemon，随进程退出而终止（与后端「手动/守护重启」无关）；
进程被杀后的恢复由 ``Core/human_checkpoint.py`` 的 F3 清扫器兜底。
"""
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# 空闲超过该秒数即向订阅者注入心跳，防止代理/客户端因长时间无字节而静默断流
HEARTBEAT_INTERVAL = 15.0

# 单作业事件缓冲上限（超出丢弃最旧事件；仅影响极端长流程的完整重放）
BUFFER_LIMIT = 5000

# 作业结束后保留时长（秒），供客户端拉取最终态 / 重连续传 done 事件
RETAIN_SECONDS = 1800

# 同时保留的历史作业数上限
MAX_JOBS = 8


class WorkflowJob:
    """单个工作流作业：独立线程执行 + 事件缓冲 + 多订阅者。"""

    def __init__(self, job_id: str, meta: Optional[Dict[str, Any]] = None):
        self.job_id = job_id
        self.meta = dict(meta or {})
        self.status = "running"          # running / done / error / cancelled
        self.error = ""
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.events: List[dict] = []
        self.dropped_events = 0
        self._next_event_id = 1
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ 生产者
    def emit(self, event: Optional[dict]) -> None:
        """写入一个事件（自动编号 event_id / 注入 job_id）并唤醒所有订阅者。"""
        if not event:
            return
        with self._cond:
            ev = dict(event)
            ev["event_id"] = self._next_event_id
            ev.setdefault("job_id", self.job_id)
            self._next_event_id += 1
            self.events.append(ev)
            if len(self.events) > BUFFER_LIMIT:
                drop = len(self.events) - BUFFER_LIMIT
                del self.events[:drop]
                self.dropped_events += drop
            self._cond.notify_all()

    def finish(self, status: str = "done", error: str = "") -> None:
        """标记作业结束，唤醒订阅者使其可以自然收尾。"""
        with self._cond:
            if self.status == "running":
                self.status = status
                self.error = error or ""
            self.finished_at = time.time()
            self._cond.notify_all()

    # ------------------------------------------------------------------ 消费者
    def subscribe(self, last_event_id: int = 0,
                  heartbeat_interval: float = HEARTBEAT_INTERVAL,
                  should_stop: Optional[Callable[[], bool]] = None):
        """订阅事件流：先重放 last_event_id 之后的历史事件，再等待新事件。

        * 作业仍在运行 → 永久阻塞在条件变量上（空闲时发心跳）；
        * 作业已结束且无新事件 → 生成器自然结束（订阅者据此收尾）。
        * 断开连接（生成器被关闭）只会终止本生成器，不影响作业线程。
        """
        try:
            cursor = int(last_event_id or 0)
        except (TypeError, ValueError):
            cursor = 0

        while True:
            if should_stop is not None and should_stop():
                return
            with self._cond:
                pending = [e for e in self.events if e["event_id"] > cursor]
                if not pending and self.status == "running":
                    self._cond.wait(timeout=heartbeat_interval)
                    pending = [e for e in self.events if e["event_id"] > cursor]
                finished = self.status != "running"

            if pending:
                for ev in pending:
                    cursor = ev["event_id"]
                    yield ev
                continue

            if finished:
                return

            # 事件编号不推进客户端水位线（心跳仅用于保活）
            yield {"type": "heartbeat", "ts": time.time(), "job_id": self.job_id}

    # ------------------------------------------------------------------ 观测
    def snapshot(self) -> dict:
        with self._cond:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "error": self.error,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "events": len(self.events),
                "dropped_events": self.dropped_events,
                "last_event_id": self._next_event_id - 1,
                "meta": dict(self.meta),
            }


class WorkflowJobManager:
    """作业注册表：创建/查询/清理作业。线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, WorkflowJob] = {}
        self._current_id = ""

    # ---------------------------------------------------------------- 生命周期
    def start_job(self, runner: Callable[[WorkflowJob], None],
                  meta: Optional[Dict[str, Any]] = None,
                  job_id: Optional[str] = None) -> WorkflowJob:
        """创建作业并在**独立后台线程**启动 runner(job)。

        runner 内部用 ``job.emit(event)`` 推送事件、以 ``job.finish(status)`` 收尾；
        runner 抛出的异常会被捕获并落为 ``status="error"``，不会影响进程。
        """
        self.prune()
        jid = job_id or f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        job = WorkflowJob(jid, meta)
        with self._lock:
            self._jobs[jid] = job
            self._current_id = jid

        def _target():
            try:
                runner(job)
                if job.status == "running":
                    job.finish("done")
            except Exception as exc:  # runner 异常不应杀死进程
                job.finish("error", str(exc))
                try:
                    job.emit({"type": "fatal", "error": str(exc)})
                except Exception:
                    pass

        threading.Thread(target=_target, name=f"wf-job-{jid}", daemon=True).start()
        return job

    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> Optional[WorkflowJob]:
        if not job_id:
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def current(self) -> Optional[WorkflowJob]:
        """返回「当前作业」：优先最近一个仍在运行的作业，否则最近一个仍在保留期的作业。"""
        with self._lock:
            cur = self._jobs.get(self._current_id)
            if cur is not None and (cur.status == "running" or self._in_retain(cur)):
                return cur
            running = [j for j in self._jobs.values() if j.status == "running"]
            if running:
                return max(running, key=lambda j: j.created_at)
            alive = [j for j in self._jobs.values() if self._in_retain(j)]
            return max(alive, key=lambda j: j.created_at) if alive else None

    @staticmethod
    def _in_retain(job: WorkflowJob) -> bool:
        end = job.finished_at or job.created_at
        return (time.time() - end) <= RETAIN_SECONDS

    def list_jobs(self) -> List[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.snapshot() for j in jobs]

    def prune(self) -> int:
        """清理超出保留期或超出数量上限的已结束作业，返回清理数量。"""
        removed = 0
        with self._lock:
            for jid, job in list(self._jobs.items()):
                if job.status != "running" and not self._in_retain(job):
                    del self._jobs[jid]
                    removed += 1
            if len(self._jobs) > MAX_JOBS:
                finished = sorted(
                    [j for j in self._jobs.values() if j.status != "running"],
                    key=lambda j: j.created_at)
                for job in finished[:max(0, len(self._jobs) - MAX_JOBS)]:
                    self._jobs.pop(job.job_id, None)
                    removed += 1
        return removed


_manager_singleton: Optional[WorkflowJobManager] = None
_manager_singleton_lock = threading.Lock()


def get_job_manager() -> WorkflowJobManager:
    """获取进程级唯一的作业管理器。"""
    global _manager_singleton
    with _manager_singleton_lock:
        if _manager_singleton is None:
            _manager_singleton = WorkflowJobManager()
        return _manager_singleton
