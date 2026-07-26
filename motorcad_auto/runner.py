# -*- coding: utf-8 -*-
"""
runner.py - Motor-CAD v11.1.5 自动化高层 API

提供简洁的接口供外部调用:
  - run_single(): 单次分析 (热 + 电磁)
  - run_batch(): 批量分析 (参数扫描)
  - quick_thermal(): 快速热分析
  - quick_emag(): 快速电磁分析

使用示例:
  from motorcad_auto import MotorCADRunner, MotorCADConfig

  config = MotorCADConfig(mot_file=r"D:\\path\\to\\model.mot")
  runner = MotorCADRunner(config)
  result = runner.run_single(params={"DCBusVoltage": 350, "PeakCurrent": 100})
  print(result.summary())
"""
import os
import json
import time
import logging
from typing import Dict, Any, List, Optional

from .config import MotorCADConfig
from .connector import MotorCADConnector
from .analysis import AnalysisRunner, AnalysisResult

logger = logging.getLogger("motorcad_auto.runner")


class MotorCADRunner:
    """Motor-CAD 自动化运行器 (高层 API)"""

    def __init__(self, config: Optional[MotorCADConfig] = None):
        self.config = config or MotorCADConfig()
        self.connector = MotorCADConnector(self.config)
        self._analyzer: Optional[AnalysisRunner] = None

    # ============================================================
    #  连接管理
    # ============================================================
    def connect(self) -> bool:
        """连接 Motor-CAD"""
        ok = self.connector.connect()
        if ok:
            self._analyzer = AnalysisRunner(self.connector, self.config)
        return ok

    def disconnect(self):
        """断开连接"""
        self._analyzer = None
        self.connector.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    # ============================================================
    #  单次分析
    # ============================================================
    def run_single(self, params: Optional[dict] = None,
                   mot_file: Optional[str] = None,
                   run_thermal: Optional[bool] = None,
                   run_emag: Optional[bool] = None) -> AnalysisResult:
        """
        执行单次分析

        Args:
            params: 设计参数 (如 {"DCBusVoltage": 350, "PeakCurrent": 100})
            mot_file: .mot 文件路径 (None = 使用配置默认值)
            run_thermal: 是否执行热分析 (None = 使用配置)
            run_emag: 是否执行电磁分析 (None = 使用配置)
        Returns:
            AnalysisResult 对象
        """
        if not self.connector.is_connected:
            self.connect()

        if not self.connector.is_connected:
            result = AnalysisResult()
            result.errors.append("Motor-CAD 连接失败")
            return result

        # 临时覆盖配置
        if run_thermal is not None:
            self.config.run_thermal = run_thermal
        if run_emag is not None:
            self.config.run_emag = run_emag

        self._analyzer = AnalysisRunner(self.connector, self.config)
        return self._analyzer.run_full_analysis_timed(params, mot_file)

    # ============================================================
    #  快捷方法
    # ============================================================
    def quick_thermal(self, mot_file: Optional[str] = None) -> AnalysisResult:
        """仅执行热分析"""
        return self.run_single(mot_file=mot_file, run_thermal=True, run_emag=False)

    def quick_emag(self, mot_file: Optional[str] = None) -> AnalysisResult:
        """仅执行电磁分析"""
        return self.run_single(mot_file=mot_file, run_thermal=False, run_emag=True)

    # ============================================================
    #  批量分析
    # ============================================================
    def run_batch(self, param_list: List[dict],
                  mot_file: Optional[str] = None,
                  save_json: bool = True,
                  json_path: Optional[str] = None) -> List[AnalysisResult]:
        """
        批量参数扫描分析

        Args:
            param_list: 参数字典列表
            mot_file: .mot 文件 (所有方案使用同一文件)
            save_json: 是否保存结果到 JSON
            json_path: JSON 保存路径
        Returns:
            AnalysisResult 列表
        """
        if not self.connector.is_connected:
            self.connect()

        results = []
        total = len(param_list)
        logger.info(f"批量分析开始, 共 {total} 个方案")

        for i, params in enumerate(param_list):
            logger.info(f"\n{'='*60}")
            logger.info(f"方案 {i+1}/{total}: {params}")
            logger.info(f"{'='*60}")

            result = self.run_single(params, mot_file)
            results.append(result)

            # 逐个保存 (防止中途崩溃丢失数据)
            if save_json:
                self._save_batch_json(results, json_path, i, total)

        # 汇总
        logger.info(f"\n{'='*60}")
        logger.info(f"批量分析完成: {total} 个方案")
        thermal_ok = sum(1 for r in results if r.thermal_return == 0)
        emag_ok = sum(1 for r in results if r.emag_return == 0)
        logger.info(f"  热分析成功: {thermal_ok}/{total}")
        logger.info(f"  电磁分析成功: {emag_ok}/{total}")

        if save_json:
            self._save_batch_json(results, json_path, total - 1, total, final=True)

        return results

    def _save_batch_json(self, results: List[AnalysisResult],
                         json_path: Optional[str], idx: int, total: int,
                         final: bool = False):
        """保存批量结果到 JSON"""
        if not json_path:
            json_path = os.path.join(
                self.config.result_dir,
                f"batch_results_{time.strftime('%Y%m%d_%H%M%S')}.json"
            )
        os.makedirs(os.path.dirname(json_path), exist_ok=True)

        data = {
            "batch_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total": total,
            "completed": idx + 1,
            "is_final": final,
            "results": [r.to_dict() for r in results],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        if final:
            logger.info(f"批量结果已保存: {json_path}")

    # ============================================================
    #  模式检查 (不执行分析)
    # ============================================================
    def check_status(self) -> dict:
        """
        检查 Motor-CAD 连接状态和 ActiveX 模式 (不执行分析)
        Returns:
            状态字典
        """
        status = {
            "exe_exists": os.path.exists(self.config.exe_path),
            "exe_path": self.config.exe_path,
            "connected": self.connector.is_connected,
            "activex_mode": "unknown",
            "main_window_found": False,
        }

        # 查找主窗口
        hwnd = self.connector.find_main_window(timeout=5)
        if hwnd:
            status["main_window_found"] = True
            status["activex_mode"] = self.connector.check_activex_mode()

        return status
