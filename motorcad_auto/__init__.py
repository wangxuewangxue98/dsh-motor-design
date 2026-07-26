# -*- coding: utf-8 -*-
"""
motorcad_auto - Motor-CAD v11.1.5 COM 自动化项目

通过 win32com COM/ActiveX 接口对接 Motor-CAD 11.1.5, 支持电磁分析与热分析。

核心模块:
  connector  - COM 连接管理 + ActiveX 注册模式验证
  params     - 安全的参数读写 (处理 v11.1.5 元组返回格式)
  analysis   - 热分析 (DoSteadyStateAnalysis) + 电磁分析 (DoMagneticCalculation)
  runner     - 高层 API: 单次求解 / 批量求解
  config     - 路径、超时、变量名映射等配置

依赖: pywin32 (pip install pywin32)
"""

from .config import MotorCADConfig
from .connector import MotorCADConnector
from .params import ParameterManager
from .analysis import AnalysisRunner
from .runner import MotorCADRunner

__version__ = "1.0.0"
__all__ = [
    "MotorCADConfig",
    "MotorCADConnector",
    "ParameterManager",
    "AnalysisRunner",
    "MotorCADRunner",
]
