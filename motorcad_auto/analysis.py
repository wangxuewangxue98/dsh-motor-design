# -*- coding: utf-8 -*-
"""
analysis.py - Motor-CAD v11.1.5 分析执行器

核心功能:
  1. 热分析: DoSteadyStateAnalysis (已验证可用)
  2. 电磁分析: DoMagneticCalculation (已验证可用! 需要 ShowMagneticContext)
  3. 耦合分析: DoMagneticThermalCalculation (已验证可用!)
  4. 结果提取与格式化

关键发现 (基于实际测试 2026-07-26):
  - DoSteadyStateAnalysis() 返回 0 表示成功, 耗时约 2-10 秒
  - DoMagneticCalculation() 返回 0 表示成功, 耗时约 190-200 秒
    *** 必须先调用 ShowMagneticContext() 才能成功! ***
    即使 .mot 文件 Program_Module=BPM-Therm, 切换上下文后电磁分析可用
  - DoMagneticThermalCalculation() 返回 0 表示成功, 耗时约 195 秒
    执行电磁+热耦合计算
  - 电磁结果变量: Shaft_Torque_[pu], OutputPower, InputPower, Loss_[*] 等
  - 效率需计算: OutputPower / InputPower
"""
import time
import logging
from typing import Dict, Any, Optional

from .config import MotorCADConfig
from .params import ParameterManager

logger = logging.getLogger("motorcad_auto.analysis")


class AnalysisResult:
    """单次分析结果容器"""

    def __init__(self):
        self.timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.model_info: Dict[str, Any] = {}
        self.params_set: Dict[str, bool] = {}
        self.thermal: Dict[str, float] = {}
        self.emag: Dict[str, float] = {}
        self.thermal_return: Optional[int] = None
        self.emag_return: Optional[int] = None
        self.thermal_elapsed: float = 0.0
        self.emag_elapsed: float = 0.0
        self.errors: list = []

    def to_dict(self) -> dict:
        """转换为字典 (用于 JSON 序列化)"""
        return {
            "timestamp": self.timestamp,
            "model_info": self.model_info,
            "params_set": self.params_set,
            "thermal": self.thermal,
            "emag": self.emag,
            "thermal_return": self.thermal_return,
            "emag_return": self.emag_return,
            "thermal_elapsed_s": round(self.thermal_elapsed, 2),
            "emag_elapsed_s": round(self.emag_elapsed, 2),
            "errors": self.errors,
        }

    def summary(self) -> str:
        """生成可读的结果摘要"""
        lines = [
            f"=== Motor-CAD 分析结果 ({self.timestamp}) ===",
            f"模型: {self.model_info.get('Program_Version', '?')} "
            f"| {self.model_info.get('Program_Module', '?')}",
            f"极数: {self.model_info.get('Pole_Number', '?')} "
            f"| 槽数: {self.model_info.get('Slot_Number', '?')}",
        ]

        if self.thermal:
            lines.append(f"\n--- 热分析 (返回={self.thermal_return}, "
                        f"{self.thermal_elapsed:.1f}s) ---")
            for k, v in self.thermal.items():
                if isinstance(v, (int, float)):
                    lines.append(f"  {k}: {v:.2f} °C")
                else:
                    lines.append(f"  {k}: {v}")
        else:
            lines.append("\n--- 热分析: 无结果 ---")

        if self.emag:
            lines.append(f"\n--- 电磁分析 (返回={self.emag_return}, "
                        f"{self.emag_elapsed:.1f}s) ---")
            for k, v in self.emag.items():
                if isinstance(v, (int, float)):
                    if "Power" in k and abs(v) > 1000:
                        lines.append(f"  {k}: {v/1000:.2f} kW")
                    elif "Efficiency" in k:
                        lines.append(f"  {k}: {v:.2f}%")
                    elif "Loss" in k and abs(v) > 1000:
                        lines.append(f"  {k}: {v/1000:.2f} kW")
                    else:
                        lines.append(f"  {k}: {v:.4f}")
                else:
                    lines.append(f"  {k}: {v}")
        elif self.emag_return is not None:
            lines.append(f"\n--- 电磁分析: 失败 (返回={self.emag_return}) ---")
            lines.append("  (可能需要检查模型几何或参数设置)")
        else:
            lines.append("\n--- 电磁分析: 未执行 ---")

        if self.errors:
            lines.append("\n--- 错误 ---")
            for err in self.errors:
                lines.append(f"  - {err}")

        return "\n".join(lines)


class AnalysisRunner:
    """Motor-CAD 分析执行器"""

    def __init__(self, connector, config: Optional[MotorCADConfig] = None):
        """
        Args:
            connector: MotorCADConnector 实例 (必须已连接)
            config: 配置对象
        """
        self.connector = connector
        self.config = config or MotorCADConfig()
        self.params = ParameterManager(connector, config)

    # ============================================================
    #  热分析
    # ============================================================
    def run_thermal(self) -> tuple:
        """
        执行稳态热分析 (DoSteadyStateAnalysis)
        Returns:
            (return_code: int, results: dict)
            return_code 0 = 成功
        """
        mc = self.connector.com_object
        logger.info("执行稳态热分析 (DoSteadyStateAnalysis)...")

        t0 = time.time()
        try:
            ret = mc.DoSteadyStateAnalysis()
            elapsed = time.time() - t0
            logger.info(f"热分析返回: {ret} ({elapsed:.1f}s)")

            # 读取温度结果
            results = self.params.read_thermal_results()
            return ret, results
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"热分析异常: {e} ({elapsed:.1f}s)")
            return -999, {}

    # ============================================================
    #  电磁分析
    # ============================================================
    def run_emag(self) -> tuple:
        """
        执行电磁分析 (DoMagneticCalculation)
        
        关键: 必须先调用 ShowMagneticContext() 切换到电磁上下文!
        即使 .mot 文件为 BPM-Therm 模块, 切换后电磁分析可成功执行。
        
        Returns:
            (return_code: int, results: dict)
            return_code 0 = 成功
        """
        mc = self.connector.com_object
        logger.info("执行电磁分析 (DoMagneticCalculation)...")

        # 切换到电磁上下文 (关键步骤!)
        self.connector.show_magnetic_context()

        t0 = time.time()
        try:
            ret = mc.DoMagneticCalculation()
            elapsed = time.time() - t0
            logger.info(f"电磁分析返回: {ret} ({elapsed:.1f}s)")

            # 读取电磁结果
            results = self.params.read_emag_results()
            return ret, results
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"电磁分析异常: {e} ({elapsed:.1f}s)")
            return -999, {}

    # ============================================================
    #  电磁-热耦合分析
    # ============================================================
    def run_emag_thermal(self) -> tuple:
        """
        执行电磁-热耦合计算 (DoMagneticThermalCalculation)
        
        同时计算电磁性能和热性能, 结果包含电磁参数和温度。
        需要先调用 ShowMagneticContext() 切换上下文。
        
        Returns:
            (return_code: int, results: dict)
            return_code 0 = 成功
        """
        mc = self.connector.com_object
        logger.info("执行电磁-热耦合分析 (DoMagneticThermalCalculation)...")

        # 切换到电磁上下文
        self.connector.show_magnetic_context()

        t0 = time.time()
        try:
            ret = mc.DoMagneticThermalCalculation()
            elapsed = time.time() - t0
            logger.info(f"耦合分析返回: {ret} ({elapsed:.1f}s)")

            # 读取电磁+热结果
            results = self.params.read_emag_results()
            results.update(self.params.read_thermal_results())
            return ret, results
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"耦合分析异常: {e} ({elapsed:.1f}s)")
            return -999, {}

    # ============================================================
    #  完整分析 (热 + 电磁)
    # ============================================================
    def run_full_analysis(self, params: Optional[dict] = None,
                          mot_file: Optional[str] = None) -> AnalysisResult:
        """
        执行完整分析流程:
          1. 加载 .mot 文件
          2. 设置设计参数
          3. 读取模型信息
          4. 执行热分析
          5. 执行电磁分析 (可选)
          6. 保存结果文件

        Args:
            params: 设计参数字典 (可选)
            mot_file: .mot 文件路径 (可选, 默认使用配置)
        Returns:
            AnalysisResult 对象
        """
        result = AnalysisResult()

        # 1. 加载文件
        mot_path = mot_file or self.config.get_mot_file()
        if not self.connector.load_file(mot_path):
            result.errors.append(f"文件加载失败: {mot_path}")
            return result

        # 2. 设置参数
        if params:
            result.params_set = self.params.apply_params(params)

        # 3. 读取模型信息
        logger.info("读取模型信息...")
        result.model_info = self.params.read_model_info()

        module = result.model_info.get("Program_Module", "")
        logger.info(f"模型模块: {module}")

        # 4. 热分析
        if self.config.run_thermal:
            ret, thermal = self.run_thermal()
            result.thermal_return = ret if isinstance(ret, int) else int(ret) if ret else 0
            result.thermal = thermal
            result.thermal_elapsed = 0  # 在 run_thermal 内部计时
            if ret != 0 and ret != -999:
                result.errors.append(f"热分析返回非零: {ret}")
        else:
            logger.info("跳过热分析 (config.run_thermal=False)")

        # 5. 电磁分析
        if self.config.run_emag:
            ret, emag = self.run_emag()
            result.emag_return = ret if isinstance(ret, int) else int(ret) if ret else 0
            result.emag = emag
            result.emag_elapsed = 0
            if ret == -1:
                result.errors.append(
                    "电磁分析返回 -1 (可能需要检查模型几何或参数)"
                )
            elif ret == -999:
                result.errors.append("电磁分析异常")
        else:
            logger.info("跳过电磁分析 (config.run_emag=False)")

        # 6. 保存结果
        if self.config.save_result_file:
            result_path = self.config.get_result_path()
            self.connector.save_file(result_path)

        return result

    # ============================================================
    #  重新计时版本 (更精确)
    # ============================================================
    def run_full_analysis_timed(self, params: Optional[dict] = None,
                                mot_file: Optional[str] = None) -> AnalysisResult:
        """带精确计时的完整分析"""
        result = AnalysisResult()

        # 1. 加载文件
        mot_path = mot_file or self.config.get_mot_file()
        if not self.connector.load_file(mot_path):
            result.errors.append(f"文件加载失败: {mot_path}")
            return result

        # 2. 设置参数
        if params:
            result.params_set = self.params.apply_params(params)

        # 3. 读取模型信息
        logger.info("读取模型信息...")
        result.model_info = self.params.read_model_info()

        module = result.model_info.get("Program_Module", "")
        logger.info(f"模型模块: {module}")

        # 4. 热分析 (带计时)
        if self.config.run_thermal:
            mc = self.connector.com_object
            logger.info("执行稳态热分析 (DoSteadyStateAnalysis)...")
            t0 = time.time()
            try:
                ret = mc.DoSteadyStateAnalysis()
                result.thermal_elapsed = time.time() - t0
                result.thermal_return = ret if isinstance(ret, int) else 0
                logger.info(f"热分析返回: {ret} ({result.thermal_elapsed:.1f}s)")
                result.thermal = self.params.read_thermal_results()
            except Exception as e:
                result.thermal_elapsed = time.time() - t0
                result.thermal_return = -999
                result.errors.append(f"热分析异常: {e}")
                logger.error(f"热分析异常: {e}")

        # 5. 电磁分析 (带计时)
        if self.config.run_emag:
            mc = self.connector.com_object
            logger.info("执行电磁分析 (DoMagneticCalculation)...")

            # 切换到电磁上下文 (关键步骤!)
            self.connector.show_magnetic_context()

            t0 = time.time()
            try:
                ret = mc.DoMagneticCalculation()
                result.emag_elapsed = time.time() - t0
                result.emag_return = ret if isinstance(ret, int) else 0
                logger.info(f"电磁分析返回: {ret} ({result.emag_elapsed:.1f}s)")
                result.emag = self.params.read_emag_results()
                if ret == -1:
                    result.errors.append(
                        "电磁分析返回 -1 (可能需要检查模型几何或参数)"
                    )
            except Exception as e:
                result.emag_elapsed = time.time() - t0
                result.emag_return = -999
                result.errors.append(f"电磁分析异常: {e}")
                logger.error(f"电磁分析异常: {e}")

        # 6. 保存结果
        if self.config.save_result_file:
            result_path = self.config.get_result_path()
            self.connector.save_file(result_path)

        return result
