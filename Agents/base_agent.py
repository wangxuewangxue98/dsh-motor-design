"""
智能体基类 - 所有智能体继承此类
提供审计日志、错误处理、耗时统计等通用能力
"""
import time
import uuid
from datetime import datetime
from typing import Any, Optional
import sys
sys.path.insert(0, r"D:\MotorDesign\Core")
sys.path.insert(0, r"D:\MotorDesign\Scripts")
import audit_logger


class BaseAgent:
    """所有智能体的基类"""

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.project_id: Optional[str] = None

    def _new_project_id(self) -> str:
        self.project_id = f"PROJ_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        return self.project_id

    def _log(self, action: str, parameters: Any = None, result: Any = None,
             status: str = "success", duration_ms: int = 0):
        """记录审计日志"""
        audit_logger.log(
            agent_name=self.name,
            action=action,
            parameters=parameters,
            result=result,
            status=status,
            duration_ms=duration_ms,
            project_id=self.project_id
        )

    def run(self, **kwargs) -> dict:
        """执行智能体任务（子类实现）"""
        raise NotImplementedError

    def safe_run(self, **kwargs) -> dict:
        """带错误处理的执行包装"""
        start = time.time()
        try:
            if not self.project_id:
                self._new_project_id()
            result = self.run(**kwargs)
            duration = int((time.time() - start) * 1000)
            self._log(
                action=f"run:{self.name}",
                parameters=kwargs,
                result=result,
                status="success",
                duration_ms=duration
            )
            return result
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            self._log(
                action=f"run:{self.name}",
                parameters=kwargs,
                result=str(e),
                status="error",
                duration_ms=duration
            )
            return {"error": str(e), "agent": self.name}
