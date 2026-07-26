# -*- coding: utf-8 -*-
"""
config.py - Motor-CAD v11.1.5 自动化项目配置

集中管理所有路径、超时、变量名映射等配置。
变量名映射基于实际 COM 测试验证 (full_com_test_v5.py / test_160kw.py)。
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List


# ============================================================
#  路径配置
# ============================================================
MOTORCAD_EXE_PATH = os.getenv(
    "MOTORCAD_EXE_PATH",
    r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe",
)
MOTORCAD_INSTALL_DIR = os.getenv(
    "MOTORCAD_INSTALL_DIR",
    r"D:\Motor-CAD\11_1_5",
)

# COM ProgID (Motor-CAD ActiveX 自动化接口)
MOTORCAD_PROGID = "MotorCAD.AppAutomation"
# COM CLSID
MOTORCAD_CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"

# 模板文件目录
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Templates")

# 默认 .mot 文件 (优先使用, 可被 config 覆盖)
DEFAULT_MOT_FILES = [
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot",
    os.path.join(TEMPLATE_DIR, "160kw_assm_2222.mot"),
    os.path.join(TEMPLATE_DIR, "Templates.mot"),
]


# ============================================================
#  超时配置 (秒)
# ============================================================
COM_CONNECT_TIMEOUT = 120      # Dispatch 连接最大等待
COM_INIT_WAIT = 10             # 连接成功后等待初始化 (Motor-CAD 启动较慢)
FILE_LOAD_WAIT = 8             # LoadFromFile 后等待 (文件加载需要时间)
THERMAL_TIMEOUT = 120          # DoSteadyStateAnalysis 超时 (实际约 2-10 秒)
EMAG_TIMEOUT = 300             # DoMagneticCalculation 超时 (实际约 190-200 秒)
GETVAR_TIMEOUT = 10            # 单个 GetVariable 超时 (防挂起)


# ============================================================
#  ActiveX 注册模式菜单 ID
#  菜单路径: 默认设置(&T) -> 控件注册
#  Motor-CAD v11.1.5 (Delphi VCL) 菜单项 ID
# ============================================================
MENU_ID_MULTI_CLIENT = 93   # 多客户端设置
MENU_ID_SINGLE_CLIENT = 94  # 单客户端设置 (推荐, COM 可用)
MENU_ID_NO_REGISTER = 95    # 免注册 (COM 不可用!)


# ============================================================
#  已验证可用的变量名映射
#  基于实际 COM 测试, 仅包含确认不会导致挂起的变量名
# ============================================================

# --- 可读取的模型参数 (GetVariable 返回 (code, value) 元组, code>=0 表示成功) ---
# 注意: 仅包含已验证不会导致挂起的变量名
# "OperatingSpeed" 已移除 — 在某些 .mot 文件中会导致 GetVariable 无限挂起
READABLE_PARAMS: List[str] = [
    "Program_Version",
    "Program_Module",
    "Pole_Number",
    "Slot_Number",
    "DCBusVoltage",
    "PeakCurrent",
    "WindingConnection",
]

# --- 可设置的设计参数 (SetVariable) ---
#  key = 外部API名称, value = Motor-CAD 变量名
SETTABLE_PARAM_MAP: Dict[str, str] = {
    "dc_bus_voltage": "DCBusVoltage",
    "peak_current": "PeakCurrent",
    # 以下变量名可能因 .mot 文件模块不同而不可用, 设置时自动 try/except
    # 注意: "OperatingSpeed" 在某些 .mot 中会导致 GetVariable 挂起, 不在可读列表中
    "stator_od": "Stator_Lamination_Dia",
    "voltage": "Supply_Voltage",
    "poles": "Poles",
    "speed": "Speed",
    "slots": "Slots",
    "turns_per_coil": "Turns_per_Coil",
}

# --- 热分析结果变量 (DoSteadyStateAnalysis 后读取) ---
# 已验证可用 (BPM-Therm 模块, Matlab_Test.mot):
#   T_[Winding_Average], T_[Winding_Max]
# 已确认挂起 (BPM-Therm 模块不支持的变量, 查询时无限挂起):
#   T_[Housing_Average], MaxTemperature
# 未验证 (可能挂起, 使用前请用 cli.py test-var 命令测试):
#   T_[Housing_Max], T_[Magnet_*], T_[Rotor_Average], T_[Stator_Average],
#   T_[Coolant_In], T_[Coolant_Out]
THERMAL_RESULT_VARS: List[str] = [
    "T_[Winding_Average]",
    "T_[Winding_Max]",
]

# 实验性热变量 (未验证, 可能挂起, 需单独测试后才能加入 THERMAL_RESULT_VARS)
THERMAL_RESULT_VARS_EXPERIMENTAL: List[str] = [
    "T_[Housing_Average]",   # 已确认挂起!
    "T_[Housing_Max]",       # 未验证
    "T_[Magnet_Average]",    # 未验证
    "T_[Magnet_Max]",        # 未验证
    "T_[Rotor_Average]",     # 未验证
    "T_[Stator_Average]",    # 未验证
    "T_[Coolant_In]",        # 未验证
    "T_[Coolant_Out]",       # 未验证
    # "MaxTemperature",      # 已确认挂起!
]

# --- 电磁分析结果变量 (DoMagneticCalculation 后读取) ---
# 已验证可用 (BPM-Therm 模块 + ShowMagneticContext, 160kw_assm_2222.mot):
#   Shaft_Torque_[pu], TorqueRippleMsVw, Loss_[Stator_Copper],
#   Loss_[Stator_Back_Iron], Loss_[Stator_Tooth], OutputPower,
#   InputPower, ElectromagneticPower, Shaft_Speed
# 返回 code=-2 (变量不存在, 不挂起):
#   Torque, Average_Torque, Shaft_Torque, CoggingTorque,
#   Iron_Loss_[*], Copper_Loss, Iron_Loss, ShaftPower,
#   Efficiency, Overall_Efficiency, BackEMFConstantKe, Speed_[RPM]
EMAG_RESULT_VARS: List[str] = [
    "Shaft_Torque_[pu]",
    "Shaft_Torque_[Required]",
    "TorqueRippleMsVw",
    "Loss_[Total]",
    "Loss_[Stator_Copper]",
    "Loss_[Rotor_Copper]",
    "Loss_[Stator_Back_Iron]",
    "Loss_[Stator_Tooth]",
    "Loss_[Stator_Tooth_Tip]",
    "Loss_[Rotor_Back_Iron]",
    "Loss_[Rotor_Tooth]",
    "Loss_[Magnet]",
    "Loss_[Windage]",
    "Loss_[Friction_-_F_Bearing]",
    "Loss_[Friction_-_R_Bearing]",
    "Loss_[Stray_Load]",
    "TotalLoss",
    "OutputPower",
    "InputPower",
    "ElectromagneticPower",
]

# 电磁分析后可读取的模型参数 (扩展)
EMAG_MODEL_PARAMS: List[str] = [
    "Shaft_Speed",
    "Current_Shaft_Speed_RPM",
    "DCBusVoltage",
    "PeakCurrent",
    "Pole_Number",
    "Slot_Number",
]


# ============================================================
#  配置数据类
# ============================================================
@dataclass
class MotorCADConfig:
    """Motor-CAD 自动化配置"""

    # Motor-CAD 可执行文件路径
    exe_path: str = MOTORCAD_EXE_PATH
    install_dir: str = MOTORCAD_INSTALL_DIR

    # COM ProgID
    progid: str = MOTORCAD_PROGID

    # .mot 模型文件路径 (None = 使用默认查找)
    mot_file: str = ""

    # 超时 (秒)
    connect_timeout: int = COM_CONNECT_TIMEOUT
    init_wait: int = COM_INIT_WAIT
    file_load_wait: int = FILE_LOAD_WAIT
    thermal_timeout: int = THERMAL_TIMEOUT
    emag_timeout: int = EMAG_TIMEOUT
    getvar_timeout: int = GETVAR_TIMEOUT

    # 是否在连接前自动修复 ActiveX 注册模式
    auto_fix_activex: bool = True

    # 是否执行电磁分析 (已验证可用! 需要 ShowMagneticContext)
    run_emag: bool = True

    # 是否执行热分析
    run_thermal: bool = True

    # 是否在求解后保存结果 .mot 文件
    save_result_file: bool = True

    # 结果保存目录
    result_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "Reports"
    )

    def get_mot_file(self) -> str:
        """获取 .mot 文件路径, 如果未指定则自动查找"""
        if self.mot_file and os.path.exists(self.mot_file):
            return self.mot_file
        # 按优先级查找默认文件
        for f in DEFAULT_MOT_FILES:
            if os.path.exists(f):
                return f
        return self.mot_file or DEFAULT_MOT_FILES[0]

    def get_result_path(self, suffix: str = "_result.mot") -> str:
        """获取结果文件保存路径"""
        mot = self.get_mot_file()
        base = os.path.splitext(os.path.basename(mot))[0]
        os.makedirs(self.result_dir, exist_ok=True)
        return os.path.join(self.result_dir, base + suffix)
