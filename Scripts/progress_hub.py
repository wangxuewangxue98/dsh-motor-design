"""
progress_hub.py - 全局进度广播中心

解决跨设备进度可见性问题:
  - 手机端发起工作流/求解时, 电脑端可实时看到进度
  - 所有连接的客户端均可订阅事件流

v1.03.02: 跨设备进度同步 + 任务终止机制
"""
import queue
import threading
import time
from datetime import datetime


class ProgressHub:
    """
    线程安全的进度广播器。

    工作原理:
      1. 工作流/求解启动时调用 start() → 设置全局状态
      2. 每个事件通过 publish() 推送到所有订阅者队列
      3. 客户端通过 /api/progress/stream (SSE) 订阅事件
      4. 任何客户端可调用 cancel() 请求终止当前任务
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._state = {
            "active": False,
            "type": None,           # "workflow" | "solve"
            "current_step": None,    # 当前步骤
            "step_message": None,    # 当前步骤消息
            "started_at": None,
            "elapsed": 0,
            "total": 0,
            "completed": 0,
            "client": None,          # 发起方标识
            "messages": [],          # 最近的消息列表 (最多50条)
        }
        self._subscribers = []       # List[queue.Queue]

    # ==================== 生命周期 ====================

    def start(self, workflow_type: str, client: str = "unknown", total: int = 0):
        """开始一个新的任务会话"""
        with self._lock:
            self._cancel_event.clear()
            self._state = {
                "active": True,
                "type": workflow_type,
                "current_step": None,
                "step_message": None,
                "started_at": datetime.now().isoformat(),
                "elapsed": 0,
                "total": total,
                "completed": 0,
                "client": client,
                "messages": [],
            }
        self._broadcast({"type": "hub_start", **self._snapshot()})

    def finish(self):
        """结束当前任务会话"""
        with self._lock:
            self._state["active"] = False
            snap = self._snapshot()
        self._broadcast({"type": "hub_end", **snap})

    # ==================== 事件发布 ====================

    def publish(self, event: dict):
        """
        发布一个进度事件到所有订阅者, 同时更新全局状态。

        在 app.py 的 generate() 函数中, 每次 yield 前调用此方法。
        """
        with self._lock:
            etype = event.get("type", "")

            if etype == "step_start":
                self._state["current_step"] = event.get("step", "")
                self._state["step_message"] = event.get("message", "")

            elif etype == "progress":
                idx = event.get("index")
                total = event.get("total")
                if idx is not None:
                    self._state["completed"] = idx
                if total is not None:
                    self._state["total"] = total

            elif etype in ("done", "fatal"):
                self._state["active"] = False

            # 追加消息 (保留最近50条)
            msg_entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": etype,
                "step": event.get("step", ""),
                "message": event.get("message", event.get("error", "")),
            }
            self._state["messages"].append(msg_entry)
            if len(self._state["messages"]) > 50:
                self._state["messages"] = self._state["messages"][-50:]

            # 计算已用时间
            if self._state["started_at"]:
                try:
                    started = datetime.fromisoformat(self._state["started_at"])
                    self._state["elapsed"] = int((datetime.now() - started).total_seconds())
                except Exception:
                    pass

            snap = self._snapshot()

        self._broadcast({"type": "hub_event", "event": event, "state": snap})

    # ==================== 取消机制 ====================

    def request_cancel(self, reason: str = "user_requested"):
        """请求取消当前任务"""
        self._cancel_event.set()
        self._broadcast({"type": "cancel_requested", "reason": reason})

    def is_cancelled(self) -> bool:
        """检查是否已请求取消 (供工作流循环检查)"""
        return self._cancel_event.is_set()

    def reset_cancel(self):
        """重置取消标志"""
        self._cancel_event.clear()

    # ==================== 订阅 ====================

    def subscribe(self) -> queue.Queue:
        """创建一个新的订阅者队列"""
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """移除订阅者"""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ==================== 状态查询 ====================

    def get_state(self) -> dict:
        """获取当前状态快照"""
        with self._lock:
            return self._snapshot()

    # ==================== 内部方法 ====================

    def _snapshot(self) -> dict:
        """生成状态快照 (调用方需持锁)"""
        snap = dict(self._state)
        snap["messages"] = list(self._state["messages"])
        return snap

    def _broadcast(self, event: dict):
        """向所有订阅者推送事件 (非阻塞)"""
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 队列满, 跳过 (慢客户端)
                pass
        # 清理已关闭的订阅者 (无法检测, 留给 unsubscribe)


# 全局单例
progress_hub = ProgressHub()
