# -*- coding: utf-8 -*-
"""
params.py - Motor-CAD v11.1.5 参数管理

核心功能:
  1. 安全的 GetVariable: 处理 v11.1.5 的 (success_code, value) 元组返回格式
  2. 安全的 SetVariable: 设置参数并验证
  3. 变量名白名单: 防止查询不存在的变量导致 COM 挂起

关键发现 (基于实际测试):
  - Motor-CAD v11.1.5 ActiveX 的 GetVariable 返回 (code, value) 元组:
      code >= 0: 成功, value 为实际值
      code == -1: 失败
      code == -2: 变量不存在
  - 某些不存在的变量名会导致 GetVariable 无限挂起:
      已确认挂起: "OperatingSpeed", "MaxTemperature", "T_[Housing_Average]"
      原因: BPM-Therm 模块不支持的变量, 查询时 Motor-CAD 可能弹出对话框或进入死循环
  - 解决方案: 仅查询白名单中的变量名 (已验证不会挂起)
"""
import logging
import time
from typing import Any, Dict, Optional, Union

from .config import (
    MotorCADConfig,
    READABLE_PARAMS,
    THERMAL_RESULT_VARS,
    EMAG_RESULT_VARS,
)

logger = logging.getLogger("motorcad_auto.params")


class ParameterManager:
    """Motor-CAD 参数读写管理器"""

    def __init__(self, connector, config: Optional[MotorCADConfig] = None):
        """
        Args:
            connector: MotorCADConnector 实例 (必须已连接)
            config: 配置对象
        """
        self.connector = connector
        self.config = config or MotorCADConfig()

    # ============================================================
    #  底层 COM 调用
    # ============================================================
    def _parse_return(self, ret: Any) -> tuple:
        """
        解析 v11.1.5 ActiveX 返回值
        返回 (success: bool, value: Any)
        """
        if isinstance(ret, tuple) and len(ret) >= 2:
            code = ret[0]
            value = ret[1]
            return (code >= 0, value)
        # 某些方法直接返回值或 0
        if ret is not None:
            return (True, ret)
        return (False, None)

    def get_variable(self, name: str, timeout: Optional[float] = None) -> Optional[Any]:
        """
        读取 Motor-CAD 变量值
        仅对白名单中的变量名调用, 防止挂起

        Args:
            name: Motor-CAD 变量名 (必须在白名单中)
            timeout: 超时秒数 (None = 使用配置默认值)
        Returns:
            变量值, 读取失败返回 None
        """
        mc = self.connector.com_object
        # 白名单检查
        all_known = set(READABLE_PARAMS + THERMAL_RESULT_VARS + EMAG_RESULT_VARS)
        if name not in all_known:
            logger.debug(f"变量 '{name}' 不在白名单中, 跳过 (防挂起)")
            return None

        try:
            ret = mc.GetVariable(name)
            success, value = self._parse_return(ret)
            if success:
                return value
            else:
                logger.debug(f"GetVariable('{name}') 返回失败码: {ret}")
                return None
        except Exception as e:
            logger.debug(f"GetVariable('{name}') 异常: {e}")
            return None

    def set_variable(self, name: str, value: Any) -> bool:
        """
        设置 Motor-CAD 变量值
        失败不会抛出异常, 仅返回 False (某些变量在不同模块下不可用)

        Args:
            name: Motor-CAD 变量名
            value: 要设置的值
        Returns:
            True 表示设置成功
        """
        mc = self.connector.com_object
        try:
            mc.SetVariable(name, value)
            return True
        except Exception as e:
            logger.debug(f"SetVariable('{name}', {value}) 失败: {e}")
            return False

    # ============================================================
    #  批量读取
    # ============================================================
    def read_model_info(self) -> Dict[str, Any]:
        """读取模型基本信息"""
        info = {}
        for name in READABLE_PARAMS:
            val = self.get_variable(name)
            if val is not None:
                info[name] = val
                if isinstance(val, (int, float)):
                    logger.info(f"  {name} = {val}")
                else:
                    logger.info(f"  {name} = {val}")
        return info

    def read_thermal_results(self) -> Dict[str, float]:
        """读取热分析结果 (温度)"""
        results = {}
        for name in THERMAL_RESULT_VARS:
            val = self.get_variable(name)
            if val is not None:
                results[name] = val
                if isinstance(val, (int, float)):
                    logger.info(f"  {name} = {val:.2f}")
                else:
                    logger.info(f"  {name} = {val}")
        return results

    def read_emag_results(self) -> Dict[str, float]:
        """
        读取电磁分析结果
        
        已验证可用的变量 (DoMagneticCalculation 返回 0 后):
          Shaft_Torque_[pu], TorqueRippleMsVw,
          Loss_[Stator_Copper], Loss_[Stator_Back_Iron], Loss_[Stator_Tooth],
          OutputPower, InputPower, ElectromagneticPower
        
        效率需计算: OutputPower / InputPower (无直接变量)
        """
        results = {}
        for name in EMAG_RESULT_VARS:
            val = self.get_variable(name)
            if val is not None:
                results[name] = val
                if isinstance(val, (int, float)):
                    logger.info(f"  {name} = {val:.4f}")
                else:
                    logger.info(f"  {name} = {val}")

        # 计算效率 (OutputPower / InputPower)
        if "OutputPower" in results and "InputPower" in results:
            inp = results["InputPower"]
            out = results["OutputPower"]
            if inp and inp > 0:
                eff = out / inp * 100
                results["Efficiency_Calculated"] = eff
                logger.info(f"  Efficiency_Calculated = {eff:.2f}%")

        return results

    # ============================================================
    #  参数设置 (批量)
    # ============================================================
    def apply_params(self, params: Dict[str, Any]) -> Dict[str, bool]:
        """
        批量设置设计参数
        Args:
            params: {外部API名: 值} 或 {Motor-CAD变量名: 值}
        Returns:
            {变量名: 是否成功}
        """
        from .config import SETTABLE_PARAM_MAP

        results = {}
        for key, value in params.items():
            # 如果 key 是外部 API 名称, 映射到 Motor-CAD 变量名
            mc_name = SETTABLE_PARAM_MAP.get(key, key)
            ok = self.set_variable(mc_name, value)
            results[mc_name] = ok
            if ok:
                logger.info(f"  设置 {mc_name} = {value}")
            else:
                logger.warning(f"  设置 {mc_name} 失败 (变量可能不存在或不可设置)")
        return results

    # ============================================================
    #  格式化输出
    # ============================================================
    @staticmethod
    def format_temperature(results: Dict[str, float]) -> str:
        """格式化温度结果为可读字符串"""
        lines = ["=== 温度结果 ==="]
        for name, val in results.items():
            if isinstance(val, (int, float)):
                lines.append(f"  {name}: {val:.2f} °C")
            else:
                lines.append(f"  {name}: {val}")
        return "\n".join(lines)

    @staticmethod
    def format_emag(results: Dict[str, float]) -> str:
        """格式化电磁结果为可读字符串"""
        lines = ["=== 电磁分析结果 ==="]
        for name, val in results.items():
            if isinstance(val, (int, float)):
                lines.append(f"  {name}: {val:.4f}")
            else:
                lines.append(f"  {name}: {val}")
        return "\n".join(lines)
