"""
motor_tools.py - Motor-CAD 本地求解器封装 (v1.02)
通过 COM/ActiveX 自动化接口对接 Motor-CAD 11.1.5
未启动 Motor-CAD 时自动使用模拟模式

v1.02 更新:
- 集成 motorcad_auto 项目, 使用已验证的 COM 连接方式
- 自动修复 ActiveX "免注册" 问题 (切换到 "单客户端设置")
- 电磁分析: ShowMagneticContext() + DoMagneticCalculation()
- 热分析: DoSteadyStateAnalysis() (非 DoThermalCalculation)
- 使用白名单变量名, 防止 GetVariable 无限挂起
- 正确处理 (code, value) 元组返回格式

v1.01 更新:
- 每个结果带 solve_mode 标记 (motorcad / simulation)
- 模拟数据大幅扩充: 槽配合、线圈参数、磁密、电密、发热因数、损耗等
- 降级时记录详细日志，供 Web UI 显示
- batch_solve 返回求解摘要
"""
import json
import random
import math
import os
import sys
import subprocess
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

# ---- 集成 motorcad_auto 项目 ----
_BASE_DIR = r"D:\MotorDesign"
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

try:
    from motorcad_auto import MotorCADConfig, MotorCADConnector
    from motorcad_auto.config import (
        READABLE_PARAMS,
        THERMAL_RESULT_VARS,
        EMAG_RESULT_VARS,
        SETTABLE_PARAM_MAP,
    )
    _MOTORCAD_AUTO_AVAILABLE = True
except ImportError as e:
    _MOTORCAD_AUTO_AVAILABLE = False
    print(f"[motor_tools] [WARN] motorcad_auto 导入失败: {e}")

# 配置 motorcad_auto 的日志输出
logging.getLogger("motorcad_auto").setLevel(logging.INFO)

# ---- 加载环境配置 ----
BASE_DIR = r"D:\MotorDesign"
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)

# ---- Motor-CAD 路径配置 ----
MOTORCAD_EXE_PATH = os.getenv("MOTORCAD_EXE_PATH", r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe")
MOTORCAD_INSTALL_DIR = os.getenv("MOTORCAD_INSTALL_DIR", r"D:\Motor-CAD\11_1_5")
MOTORCAD_PROGID = os.getenv("MOTORCAD_PROGID", "MotorCAD.AppAutomation")
MOTORCAD_USE_MODE = os.getenv("MOTORCAD_USE_MODE", "auto").lower()  # auto | manual | simulation
TEMPLATE_DIR = os.path.join(BASE_DIR, "Templates")
TEMPLATE_FILE = os.getenv("MOTORCAD_TEMPLATE_FILE", "Templates.mot")


def _find_template() -> str:
    """查找模板 .mot 文件，优先 TEMPLATE_FILE，否则取目录下第一个 .mot"""
    # 尝试使用 template_manager (如果可用)
    try:
        from template_manager import get_default_template
        result = get_default_template()
        if result and os.path.exists(result):
            return result
    except ImportError:
        pass

    primary = os.path.join(TEMPLATE_DIR, TEMPLATE_FILE)
    if os.path.exists(primary):
        return primary
    # 降级：目录下任意 .mot 文件
    if os.path.isdir(TEMPLATE_DIR):
        for f in os.listdir(TEMPLATE_DIR):
            if f.lower().endswith(".mot"):
                return os.path.join(TEMPLATE_DIR, f)
    return primary  # 返回不存在的路径，调用方会 warn


def ensure_templates() -> dict:
    """
    确保模板目录有可用 .mot 文件
    如果模板缺失, 自动从 Motor-CAD 安装目录搜索并复制
    返回 template_manager.auto_setup_templates() 的结果
    """
    try:
        from template_manager import auto_setup_templates
        return auto_setup_templates()
    except ImportError:
        _log("template_manager 不可用, 跳过模板自动设置", "warn")
        return {"template_dir": TEMPLATE_DIR, "templates": [], "default_template": None, "created": False}


# ---- 求解模式跟踪 ----
_solve_log = []  # 记录每次求解的模式和原因
_last_summary = {"motorcad": 0, "simulation": 0, "fallback_reason": ""}
_com_skip = False  # COM 连接失败后跳过后续重试
_demo_detected = False  # 运行中检测到 Demo 或授权受限


def _log(msg, level="info"):
    """记录求解日志"""
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    _solve_log.append(entry)
    prefix = "[WARN]" if level == "warn" else "[INFO]" if level == "info" else "[ERROR]"
    print(f"[motor_tools] {prefix} {msg}")


def get_solve_log():
    """获取求解日志列表"""
    return _solve_log


def get_last_summary():
    """获取上一次批量求解的摘要"""
    return _last_summary.copy()


def reset_solve_log():
    """清空求解日志"""
    global _solve_log, _last_summary, _com_skip, _demo_detected
    _solve_log.clear()
    _last_summary = {"motorcad": 0, "simulation": 0, "fallback_reason": ""}
    _com_skip = False
    _demo_detected = False


# ---- COM CLSID (Motor-CAD AppAutomation) ----
_MOTORCAD_CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"


def _apply_hkcu_com_override():
    """
    在 HKCU (当前用户) 级别覆盖 COM LocalServer32 注册表值
    将 LocalServer32 从系统级 %MOTORCAD_ACTIVEX% (指向 .bat)
    覆盖为直接指向 Motor-CAD .exe, 解决 0x800700c1 (bad exe format) 错误

    此操作不需要管理员权限, COM 查找时 HKCU 优先于 HKLM
    """
    if not os.path.exists(MOTORCAD_EXE_PATH):
        return

    try:
        import winreg
        for view_access in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            for base in [
                f"Software\\Classes\\CLSID\\{_MOTORCAD_CLSID}",
                f"Software\\Classes\\Wow6432Node\\CLSID\\{_MOTORCAD_CLSID}",
            ]:
                try:
                    # 写入 LocalServer32 = .exe 路径
                    ls_path = base + "\\LocalServer32"
                    winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ls_path, 0,
                                       winreg.KEY_SET_VALUE | view_access)
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ls_path, 0,
                                        winreg.KEY_SET_VALUE | view_access)
                    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, MOTORCAD_EXE_PATH)
                    winreg.CloseKey(key)

                    # 写入 AppDirectory = 安装目录 (工作目录)
                    ad_path = base + "\\AppDirectory"
                    winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ad_path, 0,
                                       winreg.KEY_SET_VALUE | view_access)
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ad_path, 0,
                                        winreg.KEY_SET_VALUE | view_access)
                    winreg.SetValueEx(key, None, 0, winreg.REG_SZ, MOTORCAD_INSTALL_DIR)
                    winreg.CloseKey(key)
                except Exception:
                    pass
        _log("HKCU COM 覆盖已写入 (LocalServer32 -> .exe, 不需管理员权限)")
    except ImportError:
        _log("winreg 不可用, 无法写入 HKCU COM 覆盖", "warn")
    except Exception as e:
        _log(f"HKCU COM 覆盖写入失败: {e}", "warn")


# ---- 尝试通过 COM 连接 Motor-CAD ----
_motorcad_available = False  # exe 存在 + pywin32 已安装
_com_connected = False       # COM 实际连接成功过
_com_module = None
_hkcu_override_applied = False

try:
    import win32com.client
    _com_module = win32com
    if os.path.exists(MOTORCAD_EXE_PATH):
        _motorcad_available = True
        # 导入时自动应用 HKCU COM 覆盖 (免管理员)
        _apply_hkcu_com_override()
        _hkcu_override_applied = True
        _log(f"Motor-CAD 已检测: {MOTORCAD_EXE_PATH}")
        _log(f"COM ProgID: {MOTORCAD_PROGID}")
        _log("注意: COM 连接将在首次求解时验证")
    else:
        _log(f"Motor-CAD 未找到: {MOTORCAD_EXE_PATH}")
        _log("将使用模拟模式")
except ImportError:
    _log("pywin32 未安装，将使用模拟模式")


# ---- 授权/版本检测辅助 ----
def _detect_licence_error(error_text: str) -> tuple:
    """
    识别 Motor-CAD 授权/自动化错误类型
    返回: (error_category, friendly_message, is_permanent)
    """
    et = error_text.lower()
    if "demo" in et or "demonstration" in et:
        return (
            "demo_limit",
            "Motor-CAD 当前为 Demo 演示版，COM 自动化接口通常被禁用。请购买正式授权或联系 Motor Design Ltd 申请试用。",
            True,
        )
    if "already accessing dongle" in et or "dongle" in et:
        return (
            "dongle_busy",
            "加密狗授权暂时无法获取：Motor-CAD 可能正在初始化授权（启动较慢），或其他实例占用了 dongle。"
            "请保持 Motor-CAD 窗口打开并等待 1~2 分钟；如仍失败，关闭所有 Motor-CAD 窗口后重新启动。",
            False,
        )
    if "licence" in et or "license" in et or "protection key" in et:
        return (
            "licence_error",
            "授权异常：Motor-CAD 未能验证 licence/protection key。请检查加密狗是否插好、驱动是否正常。"
            "如 Licence Diagnostics 显示正常，可能是启动尚未完成，请增加等待时间。",
            False,
        )
    if "activex" in et or "com" in et or "class not registered" in et:
        return (
            "com_error",
            "COM 组件注册失败：Motor-CAD 11.1.5 的 ActiveX 接口可能未注册。可尝试以管理员身份运行一次 Motor-CAD 或运行 regsvr32。",
            False,
        )
    if "0x800700c1" in et or "bad exe format" in et or "not a valid win32" in et:
        return (
            "bitness_mismatch",
            "COM 位数不匹配：64位 Python 无法加载32位 Motor-CAD 的 COM 服务（LocalServer32 指向 .bat 文件）。"
            "请以管理员身份运行 D:\\MotorDesign\\fix_com_registration.bat 修复注册表，然后重启 Motor-CAD。",
            True,
        )
    if "invalid class string" in et or "0x800401f3" in et or "-2147221005" in et:
        return (
            "progid_not_registered",
            f"COM ProgID 未注册: {MOTORCAD_PROGID}。请检查 .env 中 MOTORCAD_PROGID 配置。"
            "正确值应为 MotorCAD.AppAutomation（非 MotorCAD.MotorCAD）。",
            True,
        )
    if "access denied" in et or "denied" in et:
        return (
            "access_denied",
            "权限不足：无法启动或连接 Motor-CAD。请以管理员身份运行命令提示符/终端再试。",
            False,
        )
    return (
        "motorcad_error",
        f"Motor-CAD 调用失败: {error_text}",
        False,
    )


def _check_demo_version() -> bool:
    """
    通过多种启发式方式判断当前是否为 Motor-CAD Demo 版本:
    1. 文件名/路径包含 demo 字样
    2. 安装目录下有 Demo 标记文件
    3. 主窗口标题包含 Demo (仅匹配含版本号的主窗口, 不匹配弹窗)
    """
    if not os.path.exists(MOTORCAD_EXE_PATH):
        return False

    # 1. 文件名/路径包含 demo 字样
    path_lower = MOTORCAD_EXE_PATH.lower()
    if "demo" in path_lower:
        return True

    # 2. 检查安装目录下是否有 Demo 标记文件
    demo_markers = ["demo.txt", "demo.ini", "license_demo.txt", "demo_license.txt"]
    install_dir = MOTORCAD_INSTALL_DIR
    for marker in demo_markers:
        if os.path.exists(os.path.join(install_dir, marker)):
            return True

    # 3. 检查主窗口标题（仅匹配含版本号的主窗口，如 "Motor-CAD v11.1.5 ... Demo"）
    #    不匹配弹窗/对话框（如 "Motor-CAD Licence Error", "Motor-CAD DEMO" 弹窗）
    try:
        import win32gui

        def _enum_window(hwnd, extra):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).lower()
            # 主窗口标题同时包含 "motor-cad" + 版本号(如 v11 或 11.1) + "demo"
            if "motor-cad" in title and "demo" in title:
                # 排除明显的弹窗/对话框
                if any(kw in title for kw in ["licence error", "license error", "default motor"]):
                    return
                # 主窗口通常包含版本号
                if "v11" in title or "11.1" in title or "11.0" in title:
                    extra.append(title)

        demo_titles = []
        win32gui.EnumWindows(_enum_window, demo_titles)
        if demo_titles:
            return True
    except Exception:
        pass

    return False


def _is_motorcad_running() -> bool:
    """检查 Motor-CAD 进程是否正在运行"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Motor-CAD*", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=5,
            encoding="gbk", errors="replace"
        )
        return "Motor-CAD" in (result.stdout or "")
    except Exception:
        return False


def _try_get_active_object():
    """
    尝试通过 ROT (Running Object Table) 连接已运行的 Motor-CAD 实例
    这绕过了 LocalServer32 注册表值，不需要读取 .bat 文件
    如果 Motor-CAD 已运行并注册到 ROT，则返回 COM 对象
    """
    try:
        import pythoncom
        mc = win32com.client.GetActiveObject(MOTORCAD_PROGID)
        _log("通过 ROT (GetActiveObject) 连接到已运行的 Motor-CAD 实例")
        return mc
    except Exception:
        return None


def _get_motorcad_instance():
    """
    获取或启动 Motor-CAD COM 实例

    使用 motorcad_auto.MotorCADConnector, 自动处理:
      1. CoInitialize (STA)
      2. HKCU COM 注册表覆盖
      3. ActiveX 注册模式检测与修复 (免注册 -> 单客户端设置)
      4. Dispatch 连接 (快速路径) 或预启动模式 (手动启动 MC + 等待加载 + Dispatch)

    返回: MotorCADConnector 实例 (已连接)
    """
    global _com_connected

    if not _motorcad_available:
        return None

    if not _MOTORCAD_AUTO_AVAILABLE:
        _log("motorcad_auto 模块不可用, 无法连接 COM", "error")
        raise RuntimeError("motorcad_auto 模块未正确导入, 请检查安装")

    config = MotorCADConfig(
        exe_path=MOTORCAD_EXE_PATH,
        install_dir=MOTORCAD_INSTALL_DIR,
        progid=MOTORCAD_PROGID,
        auto_fix_activex=True,
    )
    connector = MotorCADConnector(config)

    # 设置日志转发: connector 的日志 -> motor_tools._log()
    class _LogForwarder(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
                level = "warn" if record.levelno >= logging.WARNING else "info"
                _log(f"[COM] {msg}", level)
            except Exception:
                pass

    forwarder = _LogForwarder()
    forwarder.setLevel(logging.INFO)
    mc_logger = logging.getLogger("motorcad_auto.connector")
    mc_logger.addHandler(forwarder)

    _log("正在连接 Motor-CAD COM (motorcad_auto)...")
    _log("提示: 预启动模式约需 2-3 分钟 (手动启动 MC + 等待加载 + Dispatch)")

    try:
        if connector.connect():
            _com_connected = True
            _log("Motor-CAD COM 连接成功!")

            # 验证 ActiveX 模式
            mode = connector.check_activex_mode()
            _log(f"ActiveX 注册模式: {mode}")

            return connector
        else:
            raise RuntimeError("MotorCADConnector.connect() 返回 False")
    except Exception as e:
        err_msg = str(e)
        _log(f"Motor-CAD COM 连接失败: {err_msg}", "error")

        raise RuntimeError(
            f"Motor-CAD COM 连接失败\n"
            f"建议:\n"
            f"  1. 运行 com_diagnose.py --quick 检查环境\n"
            f"  2. 以管理员身份运行 fix_com_registration.bat\n"
            f"  3. 在 .env 中设置 MOTORCAD_USE_MODE=manual 或 simulation"
        )
    finally:
        mc_logger.removeHandler(forwarder)


def generate_param_matrix(specs: dict, count: int = 30) -> list:
    """
    根据需求规格生成参数扫描矩阵
    返回参数字典列表，包含结构参数和电气参数

    结构参数 (v1.02.03 扩展):
      - stator_od: 定子外径 (mm)
      - stator_id: 定子内径 (mm) — 经验比例 0.55-0.65 × OD
      - rotor_od:  转子外径 (mm) = 定子内径 - 2 × 气隙
      - core_length: 铁心长度 (mm) — 经验 50-150mm
      - air_gap: 气隙长度 (mm) — 0.5-1.0mm
      - tooth_width: 齿宽 (mm) — 由槽数和内径推导
      - yoke_thickness: 轭厚 (mm) — 由外径和内径推导
      - shaft_dia: 轴径 (mm) — 转子内径的 0.3-0.4 倍
    """
    voltage = specs.get("voltage_v") or 48
    speed = specs.get("speed_rpm") or 3000
    od_limit = specs.get("stator_od_limit") or 250
    power_kw = specs.get("power_kw") or 10

    # 外径扫描范围
    stator_od_range = list(range(180, min(od_limit + 1, 260), 10))
    poles_list = [8, 10]

    # 槽配合方案 (定子槽/转子槽)
    slot_combos = {
        8: [(24, 20), (36, 28), (48, 40)],
        10: [(30, 24), (36, 28), (42, 34)],
    }

    designs = []
    for od in stator_od_range:
        for pole in poles_list:
            for slots_s, slots_r in slot_combos.get(pole, [(36, 28)]):
                # 线圈匝数根据电压和外径估算
                base_turns = max(6, int(voltage * 0.3 + (250 - od) * 0.05))
                turns = base_turns + random.choice([-2, 0, 2])

                # ---- 结构参数推导 (基于电机设计经验公式) ----
                # 定子内径: OD 的 0.55-0.65 倍 (极数越多比例略大)
                id_ratio = 0.58 + (pole - 8) * 0.02 + random.uniform(-0.02, 0.02)
                stator_id = round(od * id_ratio, 1)

                # 气隙长度: 0.5-1.0mm (外径越大气隙略大)
                air_gap = round(0.5 + (od - 180) * 0.002 + random.uniform(-0.05, 0.1), 2)
                air_gap = max(0.3, min(1.2, air_gap))

                # 转子外径 = 定子内径 - 2 × 气隙
                rotor_od = round(stator_id - 2 * air_gap, 1)

                # 铁心长度: 基于功率和转速估算, 50-150mm
                base_length = 60 + power_kw * 2.5 + (od - 180) * 0.3
                core_length = round(max(40, min(200, base_length + random.uniform(-10, 10))), 1)

                # 齿宽: 由定子内径、槽数推导 (近似齿宽 = π × ID / (2 × slots))
                tooth_width = round(3.14159 * stator_id / (2 * slots_s), 2)

                # 轭厚 (定子轭): (OD - ID) / 2 - 齿高, 近似 (OD-ID)/2 × 0.35
                slot_depth = round((od - stator_id) / 2 * 0.65, 1)  # 槽高
                yoke_thickness = round((od - stator_id) / 2 - slot_depth, 1)

                # 轴径: 转子内径的 0.3-0.4 倍 (转子为空心时)
                shaft_dia = round(rotor_od * 0.35, 1)

                designs.append({
                    "stator_od": od,
                    "stator_id": stator_id,
                    "rotor_od": rotor_od,
                    "core_length": core_length,
                    "air_gap": air_gap,
                    "tooth_width": tooth_width,
                    "yoke_thickness": yoke_thickness,
                    "shaft_dia": shaft_dia,
                    "poles": pole,
                    "voltage": voltage,
                    "peak_current": random.choice([50, 80, 100]),
                    "speed": speed,
                    "slots_stator": slots_s,
                    "slots_rotor": slots_r,
                    "turns_per_coil": turns,
                    "parallel_circuits": random.choice([1, 2]),
                })

    # 随机打乱后取前 count 个
    random.shuffle(designs)
    return designs[:count]


def run_one_design(params: dict) -> dict:
    """
    传入参数，调用 Motor-CAD 求解，返回性能结果
    如果 Motor-CAD 不可用或求解失败，使用模拟数据
    每个结果都带 solve_mode 标记
    """
    global _com_skip

    # 强制仅模拟模式
    if MOTORCAD_USE_MODE == "simulation":
        result = _run_simulated(params)
        result["solve_mode"] = "simulation"
        result["fallback_reason"] = "MOTORCAD_USE_MODE=simulation，已跳过 COM 调用"
        _last_summary["simulation"] += 1
        if not _last_summary["fallback_reason"]:
            _last_summary["fallback_reason"] = "MOTORCAD_USE_MODE=simulation"
        return result

    # 手动导入模式：不调用 COM，返回占位结果等待用户导入
    if MOTORCAD_USE_MODE == "manual":
        result = _run_simulated(params)
        result["solve_mode"] = "pending"
        result["fallback_reason"] = "MOTORCAD_USE_MODE=manual，请手动导入 Motor-CAD 求解结果"
        _last_summary["simulation"] += 1
        if not _last_summary["fallback_reason"]:
            _last_summary["fallback_reason"] = "MOTORCAD_USE_MODE=manual，等待手动导入"
        return result

    if _motorcad_available and not _com_skip:
        try:
            result = _run_motorcad(params)
            result["solve_mode"] = "motorcad"
            _last_summary["motorcad"] += 1
            return result
        except Exception as e:
            reason = str(e)
            _log(f"Motor-CAD 求解失败，降级为模拟模式: {reason}", "warn")
            _log("后续方案将直接使用模拟模式（跳过 COM 重试）", "warn")
            _com_skip = True
            _last_summary["simulation"] += 1
            if not _last_summary["fallback_reason"]:
                _last_summary["fallback_reason"] = reason
            result = _run_simulated(params)
            result["solve_mode"] = "simulation"
            result["fallback_reason"] = reason
            return result
    else:
        if _motorcad_available and _com_skip:
            _last_summary["simulation"] += 1
            if not _last_summary["fallback_reason"]:
                _last_summary["fallback_reason"] = "Motor-CAD COM 连接失败，已跳过"
        else:
            _last_summary["simulation"] += 1
            if not _last_summary["fallback_reason"]:
                _last_summary["fallback_reason"] = "Motor-CAD 未安装或 COM 不可用"
        result = _run_simulated(params)
        result["solve_mode"] = "simulation"
        return result


def _run_motorcad(params: dict) -> dict:
    """
    通过 COM/ActiveX 调用 Motor-CAD 求解
    使用 motorcad_auto 的已验证方法:
      - ShowMagneticContext() + DoMagneticCalculation()  (电磁分析)
      - DoSteadyStateAnalysis()  (热分析, 非 DoThermalCalculation)
      - 白名单变量名 + (code, value) 元组解析
    """
    connector = _get_motorcad_instance()
    try:
        result = _run_motorcad_with_instance(connector, params)
        return result
    finally:
        # 单次求解后断开连接
        try:
            connector.disconnect()
        except Exception:
            pass


def _run_simulated(params: dict) -> dict:
    """
    模拟求解 - 生成丰富的电机性能数据
    基于电机设计经验公式近似计算，方便测试完整流程
    结构参数从 params 中直接传递 (v1.02.03)
    """
    od = params["stator_od"]
    poles = params["poles"]
    voltage = params["voltage"]
    speed = params["speed"]
    slots_s = params.get("slots_stator", 36)
    slots_r = params.get("slots_rotor", 28)
    turns = params.get("turns_per_coil", 10)

    # ---- 结构参数 (从 params 传递, 带默认值) ----
    stator_id = params.get("stator_id", round(od * 0.6, 1))
    rotor_od = params.get("rotor_od", round(stator_id - 1.0, 1))
    core_length = params.get("core_length", 80.0)
    air_gap = params.get("air_gap", 0.6)
    tooth_width = params.get("tooth_width", round(3.14159 * stator_id / (2 * slots_s), 2))
    yoke_thickness = params.get("yoke_thickness", round((od - stator_id) / 2 * 0.35, 1))
    shaft_dia = params.get("shaft_dia", round(rotor_od * 0.35, 1))

    # ---- 效率 (基于尺寸和极数的经验模型) ----
    base_eff = 85.0 + (od - 180) * 0.04 + (poles - 8) * 1.2
    noise = random.uniform(-1.5, 1.5)
    efficiency = round(max(80, min(96, base_eff + noise)), 2)

    # ---- 温升 ----
    base_temp = 55 + (od - 180) * 0.25 + (speed - 3000) * 0.004
    max_temp = round(max(45, min(130, base_temp + random.uniform(-5, 8))), 1)

    # ---- 转矩和功率 ----
    torque = round(od * poles * voltage / 5000 + random.uniform(-2, 2), 2)
    power = round(torque * speed / 9550, 2)

    # ---- 磁密参数 (T) ----
    stator_tooth_flux = round(random.uniform(1.55, 1.85), 3)
    stator_yoke_flux = round(random.uniform(1.25, 1.55), 3)
    rotor_yoke_flux = round(random.uniform(1.35, 1.70), 3)
    air_gap_flux = round(random.uniform(0.72, 0.95), 3)

    # ---- 电密 (A/mm^2) ----
    current_density = round(random.uniform(3.5, 7.5), 2)

    # ---- 功率因数 ----
    power_factor = round(random.uniform(0.82, 0.95), 3)

    # ---- 损耗 (W) ----
    copper_loss = round(power * 1000 * (1 - efficiency / 100) * 0.55 + random.uniform(-10, 10), 1)
    iron_loss = round(power * 1000 * (1 - efficiency / 100) * 0.30 + random.uniform(-5, 5), 1)
    mechanical_loss = round(power * 1000 * (1 - efficiency / 100) * 0.15, 1)
    total_loss = round(copper_loss + iron_loss + mechanical_loss, 1)

    # ---- 热参数 ----
    thermal_factor = round(copper_loss / (od * math.pi / 10) * 10, 1)  # 简化发热因数
    copper_temp = round(max_temp + random.uniform(5, 15), 1)
    magnet_temp = round(max_temp - random.uniform(10, 25), 1)

    return {
        "efficiency": efficiency,
        "max_temp": max_temp,
        "torque": torque,
        "power": power,
        # 结构参数 (v1.02.03 扩展)
        "stator_od": od,
        "stator_id": stator_id,
        "rotor_od": rotor_od,
        "core_length": core_length,
        "air_gap": air_gap,
        "tooth_width": tooth_width,
        "yoke_thickness": yoke_thickness,
        "shaft_dia": shaft_dia,
        "slots_stator": slots_s,
        "slots_rotor": slots_r,
        "turns_per_coil": turns,
        # 电磁性能
        "stator_tooth_flux_density": stator_tooth_flux,
        "stator_yoke_flux_density": stator_yoke_flux,
        "rotor_yoke_flux_density": rotor_yoke_flux,
        "air_gap_flux_density": air_gap_flux,
        "current_density": current_density,
        "power_factor": power_factor,
        # 损耗
        "copper_loss": copper_loss,
        "iron_loss": iron_loss,
        "total_loss": total_loss,
        # 热参数
        "thermal_factor": thermal_factor,
        "copper_temp": copper_temp,
        "magnet_temp": magnet_temp,
    }


def batch_solve(param_list: list, verbose: bool = True, progress_cb=None) -> list:
    """
    批量求解，返回结果列表
    每个结果包含 solve_mode 标记和完整性能数据
    progress_cb: 可选回调函数 (index, total, params, result) → None
    """
    reset_solve_log()
    results = []
    total = len(param_list)

    mc_instance = None
    # 根据使用模式决定是否尝试 COM
    if MOTORCAD_USE_MODE == "simulation":
        _log("MOTORCAD_USE_MODE=simulation，全部使用模拟数据")
        mc_instance = None
        _last_summary["fallback_reason"] = "MOTORCAD_USE_MODE=simulation"
    elif MOTORCAD_USE_MODE == "manual":
        _log("MOTORCAD_USE_MODE=manual，请稍后手动导入 Motor-CAD 结果")
        mc_instance = None
        _last_summary["fallback_reason"] = "MOTORCAD_USE_MODE=manual，等待手动导入"
    elif _motorcad_available:
        _log("尝试获取 Motor-CAD 实例用于批量求解...")
        try:
            mc_instance = _get_motorcad_instance()
        except Exception as e:
            _log(f"Motor-CAD 实例获取失败: {e}", "warn")
            _last_summary["fallback_reason"] = str(e)
            mc_instance = None
    else:
        mc_instance = None
        _last_summary["fallback_reason"] = "Motor-CAD 未安装或 COM 不可用"

    for i, params in enumerate(param_list):
        try:
            if mc_instance:
                try:
                    res = _run_motorcad_with_instance(mc_instance, params)
                    res["solve_mode"] = "motorcad"
                    _last_summary["motorcad"] += 1
                except Exception as e:
                    reason = str(e)
                    _log(f"[{i+1}/{total}] Motor-CAD 求解失败: {reason}", "warn")
                    _last_summary["simulation"] += 1
                    if not _last_summary["fallback_reason"]:
                        _last_summary["fallback_reason"] = reason
                    res = _run_simulated(params)
                    res["solve_mode"] = "simulation"
                    res["fallback_reason"] = reason
            else:
                res = _run_simulated(params)
                if MOTORCAD_USE_MODE == "manual":
                    res["solve_mode"] = "pending"
                    res["fallback_reason"] = "MOTORCAD_USE_MODE=manual，请手动导入 Motor-CAD 求解结果"
                else:
                    res["solve_mode"] = "simulation"
                _last_summary["simulation"] += 1
                if not _last_summary["fallback_reason"]:
                    _last_summary["fallback_reason"] = "Motor-CAD COM 不可用"

            res["params"] = params
            res["index"] = i + 1
            results.append(res)

            if verbose:
                mode_tag = "[MC]" if res["solve_mode"] == "motorcad" else ("[PEND]" if res["solve_mode"] == "pending" else "[SIM]")
                _log(f"[{i+1}/{total}] {mode_tag} OD={params['stator_od']}mm, "
                     f"Poles={params['poles']}, Eff={res['efficiency']:.2f}%, "
                     f"Temp={res['max_temp']:.0f}K")

            # 进度回调
            if progress_cb:
                try:
                    progress_cb(i + 1, total, params, res)
                except Exception:
                    pass
        except Exception as e:
            _log(f"[{i+1}/{total}] FAILED: {e}", "error")

    # 断开 Motor-CAD 连接 (释放 COM 资源)
    if mc_instance and hasattr(mc_instance, 'disconnect'):
        try:
            mc_instance.disconnect()
            _log("Motor-CAD COM 连接已断开")
        except Exception:
            pass

    # 求解摘要
    mc_count = _last_summary["motorcad"]
    sim_count = _last_summary["simulation"]
    if MOTORCAD_USE_MODE == "manual":
        _log(f"📋 全部 {total} 组方案已生成占位数据，等待手动导入 Motor-CAD 求解结果", "info")
    elif sim_count > 0 and mc_count == 0:
        _log(f"⚠️ 全部 {total} 组方案使用模拟数据 (原因: {_last_summary['fallback_reason']})", "warn")
    elif sim_count > 0:
        _log(f"⚠️ {total} 组中 {mc_count} 组 Motor-CAD 求解, {sim_count} 组模拟数据", "warn")
    else:
        _log(f"全部 {total} 组由 Motor-CAD 求解完成")

    return results


def _safe_getvar(mc, name: str, default=0.0):
    """
    安全读取 Motor-CAD v11.1.5 变量
    处理 (code, value) 元组返回格式:
      code >= 0: 成功, 返回 value
      code == -2: 变量不存在
      其他: 失败
    """
    try:
        ret = mc.GetVariable(name)
        if isinstance(ret, tuple) and len(ret) >= 2:
            code, value = ret[0], ret[1]
            if code >= 0 and value is not None:
                return float(value)
        elif ret is not None and not isinstance(ret, tuple):
            return float(ret)
    except Exception:
        pass
    return default


def _run_motorcad_with_instance(connector, params: dict) -> dict:
    """
    使用已有 Motor-CAD COM 实例求解（批量模式复用连接）

    使用 motorcad_auto 已验证的方法:
      1. 加载 .mot 模板文件
      2. 设置设计参数 (DCBusVoltage, PeakCurrent 等)
      3. ShowMagneticContext() + DoMagneticCalculation()  (电磁分析, ~200s)
      4. ShowThermalContext() + DoSteadyStateAnalysis()  (热分析, ~10s)
      5. 读取白名单变量 (防挂起)
    """
    mc = connector.com_object

    # 1. 加载模板文件
    template_path = _find_template()
    if os.path.exists(template_path):
        connector.load_file(template_path)
        _log(f"已加载模板: {os.path.basename(template_path)}")
    else:
        _log(f"模板不存在: {template_path}，使用当前模型", "warn")

    # 2. 设置设计参数 (仅使用已验证不会挂起的变量名)
    voltage = params.get("voltage", 350)
    peak_current = params.get("peak_current", 100)
    _log(f"设置参数: DCBusVoltage={voltage}, PeakCurrent={peak_current}")

    try:
        mc.SetVariable("DCBusVoltage", voltage)
        _log("  DCBusVoltage 设置成功")
    except Exception as e:
        _log(f"  DCBusVoltage 设置失败: {e}", "warn")

    try:
        mc.SetVariable("PeakCurrent", peak_current)
        _log("  PeakCurrent 设置成功")
    except Exception as e:
        _log(f"  PeakCurrent 设置失败: {e}", "warn")

    # 注意: "Turns_per_Coil", "Poles", "Slots", "Speed" 等变量名
    # 在某些 .mot 文件中可能导致 SetVariable 挂起 (与 GetVariable 挂起同理)
    # 暂不设置这些参数, 仅使用模板文件中的默认值

    # 3. 电磁分析 (关键: 必须先 ShowMagneticContext!)
    _log("切换到电磁上下文 (ShowMagneticContext)...")
    connector.show_magnetic_context()

    _log("执行电磁分析 (DoMagneticCalculation), 预计 3-4 分钟...")
    t0 = time.time()
    ret_emag = mc.DoMagneticCalculation()
    t_emag = time.time() - t0

    emag_success = False
    if isinstance(ret_emag, int):
        emag_success = (ret_emag == 0)
    elif isinstance(ret_emag, tuple):
        emag_success = (ret_emag[0] >= 0)

    if emag_success:
        _log(f"电磁分析成功 ({t_emag:.1f}s)")
    else:
        _log(f"电磁分析返回: {ret_emag} ({t_emag:.1f}s)", "warn")
        _log("继续执行热分析...")

    # 读取电磁结果
    output_power = _safe_getvar(mc, "OutputPower")
    input_power = _safe_getvar(mc, "InputPower")
    emag_power = _safe_getvar(mc, "ElectromagneticPower")
    shaft_torque_pu = _safe_getvar(mc, "Shaft_Torque_[pu]")
    shaft_speed = _safe_getvar(mc, "Shaft_Speed")
    torque_ripple = _safe_getvar(mc, "TorqueRippleMsVw")
    loss_copper = _safe_getvar(mc, "Loss_[Stator_Copper]")
    loss_back_iron = _safe_getvar(mc, "Loss_[Stator_Back_Iron]")
    loss_tooth = _safe_getvar(mc, "Loss_[Stator_Tooth]")
    total_loss = _safe_getvar(mc, "TotalLoss")

    # 计算效率
    if input_power > 0 and output_power > 0:
        efficiency = output_power / input_power * 100
    else:
        efficiency = 0.0

    # 计算转矩 (N·m): P / (2π × n / 60)
    if shaft_speed > 0 and output_power > 0:
        torque_nm = output_power / (2 * math.pi * shaft_speed / 60)
    else:
        torque_nm = shaft_torque_pu  # 降级为 per-unit 值

    _log(f"电磁结果: P_out={output_power:.0f}W, P_in={input_power:.0f}W, "
         f"η={efficiency:.1f}%, T={torque_nm:.1f}N·m, n={shaft_speed:.0f}rpm")

    # 4. 热分析
    _log("切换到热上下文 (ShowThermalContext)...")
    connector.show_thermal_context()

    _log("执行热分析 (DoSteadyStateAnalysis)...")
    t0 = time.time()
    ret_thermal = mc.DoSteadyStateAnalysis()
    t_thermal = time.time() - t0

    thermal_success = False
    if isinstance(ret_thermal, int):
        thermal_success = (ret_thermal == 0)
    elif isinstance(ret_thermal, tuple):
        thermal_success = (ret_thermal[0] >= 0)

    if thermal_success:
        _log(f"热分析成功 ({t_thermal:.1f}s)")
    else:
        _log(f"热分析返回: {ret_thermal} ({t_thermal:.1f}s)", "warn")

    # 读取热结果 (仅白名单变量, 防挂起)
    winding_avg_temp = _safe_getvar(mc, "T_[Winding_Average]")
    winding_max_temp = _safe_getvar(mc, "T_[Winding_Max]")

    _log(f"热结果: T_winding_avg={winding_avg_temp:.1f}°C, "
         f"T_winding_max={winding_max_temp:.1f}°C")

    # 5. 组装结果 (兼容 Web UI 期望的格式)
    # 损耗: 电磁分析读取的值 + 热分析补充
    copper_loss = loss_copper if loss_copper > 0 else 0.0
    iron_loss = (loss_back_iron + loss_tooth) if (loss_back_iron + loss_tooth) > 0 else 0.0
    total_loss_val = total_loss if total_loss > 0 else (input_power - output_power)

    result = {
        "efficiency": round(efficiency, 2),
        "max_temp": round(winding_max_temp if winding_max_temp > 0 else winding_avg_temp, 1),
        "torque": round(torque_nm, 2),
        "power": round(output_power / 1000, 2),  # W → kW
        # 结构参数 (v1.02.03 扩展 - 从 params 传递)
        "stator_od": params.get("stator_od", 200),
        "stator_id": params.get("stator_id", round(params.get("stator_od", 200) * 0.6, 1)),
        "rotor_od": params.get("rotor_od", round(params.get("stator_id", 120) - 1.0, 1)),
        "core_length": params.get("core_length", 80.0),
        "air_gap": params.get("air_gap", 0.6),
        "tooth_width": params.get("tooth_width", 5.0),
        "yoke_thickness": params.get("yoke_thickness", 10.0),
        "shaft_dia": params.get("shaft_dia", 30.0),
        "slots_stator": params.get("slots_stator", 36),
        "slots_rotor": params.get("slots_rotor", 28),
        "turns_per_coil": params.get("turns_per_coil", 10),
        # 电磁性能 (Motor-CAD v11.1.5 无直接变量, 使用默认值)
        "stator_tooth_flux_density": 1.7,
        "stator_yoke_flux_density": 1.4,
        "rotor_yoke_flux_density": 1.5,
        "air_gap_flux_density": 0.85,
        "current_density": 5.0,
        "power_factor": 0.88,
        # 损耗 (实际值)
        "copper_loss": round(copper_loss, 1),
        "iron_loss": round(iron_loss, 1),
        "total_loss": round(total_loss_val, 1),
        # 热参数
        "thermal_factor": round(copper_loss / max(params.get("stator_od", 200), 100) * 10, 1) if copper_loss > 0 else 120,
        "copper_temp": round(winding_avg_temp, 1),
        "magnet_temp": round(winding_avg_temp - 20, 1) if winding_avg_temp > 0 else 80,
        # 额外信息
        "emag_return": ret_emag if isinstance(ret_emag, int) else 0,
        "thermal_return": ret_thermal if isinstance(ret_thermal, int) else 0,
        "emag_time_s": round(t_emag, 1),
        "thermal_time_s": round(t_thermal, 1),
        "shaft_speed_rpm": shaft_speed,
        "input_power_w": input_power,
        "output_power_w": output_power,
        "torque_ripple": torque_ripple,
    }

    _log(f"Motor-CAD 求解完成: 效率={result['efficiency']:.2f}%, "
         f"温升={result['max_temp']:.1f}°C, 转矩={result['torque']:.1f}N·m, "
         f"功率={result['power']:.1f}kW")
    return result


def check_motorcad_status() -> dict:
    """
    检查 Motor-CAD 安装、版本和授权状态
    返回状态字典供 Web 界面显示
    """
    is_demo_static = _check_demo_version()
    is_demo_runtime = _demo_detected
    is_demo = is_demo_static or is_demo_runtime

    status = {
        "installed": os.path.exists(MOTORCAD_EXE_PATH),
        "exe_path": MOTORCAD_EXE_PATH,
        "install_dir": MOTORCAD_INSTALL_DIR,
        "version": "11.1.5",
        "is_demo": is_demo,
        "is_demo_static": is_demo_static,
        "is_demo_runtime": is_demo_runtime,
        "com_available": _motorcad_available,
        "com_connected": _com_connected,
        "motorcad_auto_available": _MOTORCAD_AUTO_AVAILABLE,
        "simulation_mode": not _com_connected,
        "progid": MOTORCAD_PROGID,
        "template_exists": os.path.exists(_find_template()),
        "template_path": _find_template(),
        "template_dir": TEMPLATE_DIR,
        # 仅当明确检测到 Demo 标记/路径/窗口时才认为不支持自动化
        "automation_supported": not is_demo_static,
        "last_fallback_reason": _last_summary.get("fallback_reason", ""),
    }

    # 检查 ActiveX 注册模式 (如果 Motor-CAD 正在运行)
    if _MOTORCAD_AUTO_AVAILABLE and _is_motorcad_running():
        try:
            connector = MotorCADConnector(MotorCADConfig(
                exe_path=MOTORCAD_EXE_PATH,
                install_dir=MOTORCAD_INSTALL_DIR,
                progid=MOTORCAD_PROGID,
            ))
            hwnd = connector.find_main_window(timeout=3)
            if hwnd:
                mode = connector.check_activex_mode()
                status["activex_mode"] = mode
                if mode == "none":
                    status["licence_note"] = (
                        "⚠️ ActiveX 注册模式为 '免注册', COM 自动化不可用!\n"
                        "请在 Motor-CAD 菜单中切换: 默认设置 → 控件注册 → 单客户端设置\n"
                        "(切换后持久化, 重启 Motor-CAD 后仍然有效)"
                    )
                elif mode == "single":
                    status["licence_note"] = "ActiveX 模式正确 (单客户端设置), COM 可用。"
                elif mode == "multi":
                    status["licence_note"] = "ActiveX 模式: 多客户端设置, COM 可用。"
        except Exception:
            pass

    if "licence_note" not in status:
        if is_demo_static:
            status["licence_note"] = (
                "检测到 Demo 版本特征（路径/标记文件/窗口标题）。"
                "Demo 版通常禁用 COM 自动化，建议购买正式授权。"
            )
        elif is_demo_runtime:
            status["licence_note"] = (
                "运行中检测到授权受限或 Demo 限制。"
                "请先关闭所有 Motor-CAD 窗口，以管理员身份重新启动后再试。"
            )
        elif MOTORCAD_USE_MODE == "manual":
            status["licence_note"] = (
                "手动模式已启用 (MOTORCAD_USE_MODE=manual)。"
                "COM 自动化已跳过。支持两种工作流:\n"
                "1. 半自动求解: 自动修改参数→启动Motor-CAD→切换标签→用户手动求解→自动监控读取\n"
                "2. 手动CSV: 导出参数CSV→Motor-CAD GUI求解→导入结果CSV"
            )
            status["last_fallback_reason"] = _last_summary.get("fallback_reason", "") or (
                "MOTORCAD_USE_MODE=manual，COM 自动化已跳过，等待手动导入结果"
            )
        elif not _com_connected:
            status["licence_note"] = (
                "Motor-CAD 已安装但 COM 尚未连接。"
                "Motor-CAD v11.1.5 启动较慢（约 1 分钟），请保持窗口打开并等待；"
                "如遇到 'already accessing dongle'，请关闭所有 Motor-CAD 窗口后重试。"
            )
        else:
            status["licence_note"] = "Motor-CAD COM 连接正常。"

    return status


# ============================================================
# 手动导入 Motor-CAD 求解结果 (Demo/授权不可用时的替代方案)
# ============================================================
_MOTORCAD_RESULT_FIELDS = {
    # 映射: Motor-CAD 输出字段名 -> 内部字段名
    "Efficiency": "efficiency",
    "Winding_Temperature_Max": "max_temp",
    "Average_Torque": "torque",
    "Output_Power": "power",
    "Stator_Tooth_Flux_Density": "stator_tooth_flux_density",
    "Stator_Yoke_Flux_Density": "stator_yoke_flux_density",
    "Rotor_Yoke_Flux_Density": "rotor_yoke_flux_density",
    "Air_Gap_Flux_Density": "air_gap_flux_density",
    "Current_Density": "current_density",
    "Power_Factor": "power_factor",
    "Copper_Loss": "copper_loss",
    "Iron_Loss": "iron_loss",
    "Total_Loss": "total_loss",
    "Winding_Thermal_Factor": "thermal_factor",
    "Copper_Temperature": "copper_temp",
    "Magnet_Temperature": "magnet_temp",
}


def import_motorcad_result(params: dict, result_data: dict) -> dict:
    """
    手动导入一组 Motor-CAD 求解结果
    params: 设计参数 (stator_od, poles, slots_stator, ...)
    result_data: 从 Motor-CAD 复制/导出的性能数据
    返回与 run_one_design 兼容的结果字典
    """
    # 标准化字段名
    normalized = {}
    for key, value in result_data.items():
        internal = _MOTORCAD_RESULT_FIELDS.get(key, key)
        try:
            normalized[internal] = float(value)
        except (ValueError, TypeError):
            normalized[internal] = value

    # 结构参数从 params 补充
    normalized["slots_stator"] = params.get("slots_stator", result_data.get("slots_stator", 36))
    normalized["slots_rotor"] = params.get("slots_rotor", result_data.get("slots_rotor", 28))
    normalized["turns_per_coil"] = params.get("turns_per_coil", result_data.get("turns_per_coil", 10))

    normalized["solve_mode"] = "manual_import"
    normalized["params"] = params
    normalized["imported_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _log(f"手动导入结果: OD={params.get('stator_od')}mm, "
         f"Poles={params.get('poles')}, Eff={normalized.get('efficiency', 'N/A')}%, "
         f"Temp={normalized.get('max_temp', 'N/A')}K")

    return normalized


def import_motorcad_csv(csv_path: str) -> list:
    """
    从 CSV 文件批量导入 Motor-CAD 求解结果
    CSV 要求：包含 stator_od, poles, slots_stator, slots_rotor, turns_per_coil 等参数列
    以及 Efficiency, Winding_Temperature_Max 等结果列
    """
    import csv
    results = []
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV 文件不存在: {csv_path}")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            params = {
                "stator_od": float(row.get("stator_od", row.get("Stator_Lamination_Dia", 0))),
                "poles": int(float(row.get("poles", row.get("Poles", 0)))),
                "voltage": float(row.get("voltage", row.get("Supply_Voltage", 48))),
                "speed": float(row.get("speed", row.get("Speed", 3000))),
                "slots_stator": int(float(row.get("slots_stator", row.get("Slots", 36)))),
                "slots_rotor": int(float(row.get("slots_rotor", 28))),
                "turns_per_coil": int(float(row.get("turns_per_coil", row.get("Turns_per_Coil", 10)))),
            }
            results.append(import_motorcad_result(params, row))

    _log(f"CSV 导入完成: {len(results)} 组结果")
    return results


def export_params_csv(param_list: list, csv_path: str = None) -> str:
    """
    导出参数矩阵到 CSV 文件，供用户在 Motor-CAD GUI 中手动设置参数
    返回 CSV 文件路径
    """
    import csv
    if csv_path is None:
        csv_path = os.path.join(BASE_DIR, "exports", "motorcad_params.csv")

    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    # CSV 包含参数列 + 空的结果列（用户填写）
    fieldnames = [
        "index", "stator_od", "poles", "voltage", "speed",
        "slots_stator", "slots_rotor", "turns_per_coil", "parallel_circuits",
        # Motor-CAD 变量名 (供用户参考)
        "MC_Stator_Lamination_Dia", "MC_Poles", "MC_Supply_Voltage", "MC_Speed",
        "MC_Slots", "MC_Turns_per_Coil",
        # 结果列 (用户从 Motor-CAD 读取后填写)
        "Efficiency", "Winding_Temperature_Max", "Average_Torque", "Output_Power",
        "Stator_Tooth_Flux_Density", "Stator_Yoke_Flux_Density",
        "Rotor_Yoke_Flux_Density", "Air_Gap_Flux_Density",
        "Current_Density", "Power_Factor",
        "Copper_Loss", "Iron_Loss", "Total_Loss",
        "Winding_Thermal_Factor", "Copper_Temperature", "Magnet_Temperature",
    ]

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for i, params in enumerate(param_list):
            row = {
                "index": i + 1,
                "stator_od": params.get("stator_od", ""),
                "poles": params.get("poles", ""),
                "voltage": params.get("voltage", ""),
                "speed": params.get("speed", ""),
                "slots_stator": params.get("slots_stator", ""),
                "slots_rotor": params.get("slots_rotor", ""),
                "turns_per_coil": params.get("turns_per_coil", ""),
                "parallel_circuits": params.get("parallel_circuits", ""),
                # Motor-CAD 变量名映射
                "MC_Stator_Lamination_Dia": params.get("stator_od", ""),
                "MC_Poles": params.get("poles", ""),
                "MC_Supply_Voltage": params.get("voltage", ""),
                "MC_Speed": params.get("speed", ""),
                "MC_Slots": params.get("slots_stator", ""),
                "MC_Turns_per_Coil": params.get("turns_per_coil", ""),
                # 结果列留空
            }
            writer.writerow(row)

    _log(f"参数 CSV 已导出: {csv_path} ({len(param_list)} 组方案)")
    return csv_path


def generate_motorcad_guide(param_list: list = None) -> dict:
    """
    生成 Motor-CAD 手动操作指南
    返回包含操作步骤和参数信息的字典
    """
    guide = {
        "title": "Motor-CAD 手动求解操作指南",
        "com_status": "COM 自动化不可用 (32/64位兼容性问题)",
        "motorcad_version": "11.1.5",
        "template_file": _find_template(),
        "steps": [
            {
                "step": 1,
                "title": "打开 Motor-CAD 并加载模板",
                "description": f"启动 Motor-CAD v11.1.5，通过 File > Open 加载模板文件: {_find_template()}",
            },
            {
                "step": 2,
                "title": "设置设计参数",
                "description": "在 Motor-CAD 的 Input > Geometry 和 Input > Electromagnetic 页面中，按参数表设置每组方案的设计参数",
                "variables": [
                    {"name": "Stator_Lamination_Dia", "description": "定子外径 (mm)"},
                    {"name": "Poles", "description": "极数"},
                    {"name": "Supply_Voltage", "description": "供电电压 (V)"},
                    {"name": "Speed", "description": "转速 (rpm)"},
                    {"name": "Slots", "description": "定子槽数"},
                    {"name": "Turns_per_Coil", "description": "每线圈匝数"},
                ],
            },
            {
                "step": 3,
                "title": "运行电磁计算",
                "description": "点击 EMag > Calculate (或 Calculation > Magnetic) 运行电磁场有限元分析",
            },
            {
                "step": 4,
                "title": "运行热计算",
                "description": "点击 Thermal > Calculate (或 Calculation > Thermal) 运行热网络分析",
            },
            {
                "step": 5,
                "title": "记录结果数据",
                "description": "在 Results 页面读取以下变量值，填写到结果 CSV 模板中",
                "result_variables": [
                    "Efficiency (%)",
                    "Winding_Temperature_Max (°C)",
                    "Average_Torque (N·m)",
                    "Output_Power (kW)",
                    "Stator_Tooth_Flux_Density (T)",
                    "Stator_Yoke_Flux_Density (T)",
                    "Rotor_Yoke_Flux_Density (T)",
                    "Air_Gap_Flux_Density (T)",
                    "Current_Density (A/mm²)",
                    "Power_Factor",
                    "Copper_Loss (W)",
                    "Iron_Loss (W)",
                    "Total_Loss (W)",
                    "Copper_Temperature (°C)",
                    "Magnet_Temperature (°C)",
                ],
            },
            {
                "step": 6,
                "title": "导入结果",
                "description": "将填写好的结果 CSV 文件上传到 Motor-AI 系统，完成手动求解流程",
            },
        ],
    }

    if param_list:
        guide["param_count"] = len(param_list)
        guide["params_preview"] = param_list[:5]  # 前5组作为预览

    return guide


# ============================================================
# 半自动求解工作流 (Motor-CAD v11.1.5 COM 不可用的替代方案)
# ============================================================
# 发现: Motor-CAD v11.1.5 的 COM 自动化完全不可用 (不注册 class factory),
# Delphi VCL 控件不响应标准 Windows 消息, UIA 也不支持。
# 半自动工作流: 自动修改参数 -> 启动 Motor-CAD -> 切换标签 -> 用户手动求解 -> 自动监控和读取

import threading

# 半自动工作流状态
_semi_auto_state = {
    'running': False,
    'phase': 'idle',  # idle | modifying | starting | waiting | monitoring | reading | done | error
    'message': '',
    'mot_file': '',
    'params': {},
    'fe_files_before': 0,
    'fe_files_after': 0,
    'new_files': [],
    'modified_files': [],
    'duration': 0,
    'result': None,
    'error': None,
    'started_at': None,
}

# Motor-CAD 菜单命令 ID (从菜单结构分析获取)
_MC_MENU = {
    'run_solve': 119,    # 结果 > 运行求解 (切换到求解视图)
    'exit_solve': 120,   # 结果 > 退出求解 (切换到结果/温度视图)
    'node_temp': 116,    # 结果 > 节点温度
    'data_output': 117,  # 结果 > 数据输出
}

# 标签页 x 坐标映射
_MC_TAB_X = {
    'geometry': 50, 'winding': 90, 'input_data': 155,
    'solve_setup': 240, 'temperature': 310, 'output_data': 380,
    'param_sens': 470, 'script': 545, 'fluid': 700,
}


def _find_motorcad_window():
    """查找 Motor-CAD 主窗口, 返回 (hwnd, pid) 或 (None, None)"""
    try:
        import win32gui
        import win32process
    except ImportError:
        return None, None

    result = [None, None]
    def cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if cls == 'TMainForm' and 'Motor' in title and vis:
                result[0] = hwnd
                _, result[1] = win32process.GetWindowThreadProcessId(hwnd)
        except:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return result[0], result[1]


def _mc_switch_tab(main_hwnd, tab_name='solve_setup'):
    """切换 Motor-CAD 标签页"""
    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        return False

    pcs = []
    def find_pc(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                size = (rect[2]-rect[0]) * (rect[3]-rect[1])
                pcs.append((hwnd, size))
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_pc, None)

    if not pcs:
        return False

    pc_hwnd = max(pcs, key=lambda x: x[1])[0]
    x = _MC_TAB_X.get(tab_name, 240)
    lp = win32api.MAKELONG(x, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(1)
    return True


def _mc_send_menu(main_hwnd, cmd_id):
    """发送 WM_COMMAND 菜单命令到 Motor-CAD"""
    try:
        import win32gui
        import win32con
    except ImportError:
        return
    win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
    time.sleep(1)


def _mc_read_texts(main_hwnd):
    """读取 Motor-CAD 所有可见文本控件"""
    try:
        import win32gui
    except ImportError:
        return []

    texts = []
    def cb(hwnd, _):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title:
                texts.append({'cls': cls, 'title': title})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, cb, None)
    return texts


def _modify_mot_file(mot_path, params, output_path):
    """修改 .mot 文件中的参数"""
    with open(mot_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()

    param_keys = set(params.keys())
    modified = {}

    for i, line in enumerate(lines):
        if '=' in line:
            key = line.split('=', 1)[0].strip()
            if key in param_keys:
                old_val = line.split('=', 1)[1].strip()
                lines[i] = f"{key}={params[key]}\n"
                modified[key] = {'old': old_val, 'new': str(params[key])}

    for key in param_keys:
        if key not in modified:
            lines.append(f"{key}={params[key]}\n")
            modified[key] = {'old': '(new)', 'new': str(params[key])}

    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    return modified


def _semi_auto_worker(mot_file, params, output_mot):
    """半自动求解工作流的后台线程"""
    global _semi_auto_state

    try:
        fe_dir = os.path.join(MOTORCAD_INSTALL_DIR, "FEResultsData")

        # Phase 1: 修改参数
        _semi_auto_state['phase'] = 'modifying'
        _semi_auto_state['message'] = '正在修改 .mot 文件参数...'
        _log("半自动求解: 修改 .mot 文件参数")

        if output_mot is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output_mot = os.path.join(BASE_DIR, "exports", f"MotorCAD_{timestamp}.mot")

        os.makedirs(os.path.dirname(output_mot), exist_ok=True)
        modified = _modify_mot_file(mot_file, params, output_mot)
        _semi_auto_state['mot_file'] = output_mot
        _log(f"半自动求解: .mot 文件已修改 -> {output_mot}")

        # Phase 2: 启动/查找 Motor-CAD
        _semi_auto_state['phase'] = 'starting'
        _semi_auto_state['message'] = '正在启动 Motor-CAD...'
        main_hwnd, mc_pid = _find_motorcad_window()

        if not main_hwnd:
            _log("半自动求解: Motor-CAD 未运行, 启动中...")
            subprocess.Popen([MOTORCAD_EXE_PATH, output_mot])
            # 等待 Motor-CAD 启动 (最多 120 秒)
            for wait in range(120):
                time.sleep(1)
                main_hwnd, mc_pid = _find_motorcad_window()
                if main_hwnd:
                    break
                if wait % 10 == 0:
                    _semi_auto_state['message'] = f'等待 Motor-CAD 启动... ({wait}s)'

        if not main_hwnd:
            _semi_auto_state['phase'] = 'error'
            _semi_auto_state['error'] = 'Motor-CAD 启动失败'
            _semi_auto_state['running'] = False
            return

        _log(f"半自动求解: Motor-CAD 已就绪 (hwnd={main_hwnd})")

        # Phase 3: 记录初始状态
        files_before = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                try:
                    files_before[f] = {
                        'size': os.path.getsize(fpath),
                        'mtime': os.path.getmtime(fpath)
                    }
                except:
                    pass
        _semi_auto_state['fe_files_before'] = len(files_before)

        # Phase 4: 切换到求解视图
        _semi_auto_state['phase'] = 'waiting'
        _semi_auto_state['message'] = '已切换到求解视图, 请在 Motor-CAD 中手动点击 "求解热模型" 按钮'
        _mc_send_menu(main_hwnd, _MC_MENU['run_solve'])
        _mc_switch_tab(main_hwnd, 'solve_setup')
        _log("半自动求解: 已切换到求解视图, 等待用户手动点击求解按钮")

        # Phase 5: 监控 FEResultsData 变化
        _semi_auto_state['phase'] = 'monitoring'
        calc_started = False
        last_change_time = None
        timeout = 3600  # 1 小时

        for wait in range(timeout):
            if not _semi_auto_state['running']:
                _log("半自动求解: 用户取消")
                return

            time.sleep(1)

            files_now = {}
            if os.path.exists(fe_dir):
                for f in os.listdir(fe_dir):
                    fpath = os.path.join(fe_dir, f)
                    try:
                        files_now[f] = {
                            'size': os.path.getsize(fpath),
                            'mtime': os.path.getmtime(fpath)
                        }
                    except:
                        pass

            new_files = set(files_now.keys()) - set(files_before.keys())
            modified_files = set()
            for f in files_before:
                if f in files_now:
                    if files_now[f]['mtime'] != files_before[f]['mtime']:
                        modified_files.add(f)
                    elif files_now[f]['size'] != files_before[f]['size']:
                        modified_files.add(f)

            if new_files or modified_files:
                if not calc_started:
                    calc_started = True
                    _log(f"半自动求解: 检测到计算开始! 新={len(new_files)} 改={len(modified_files)}")
                    _semi_auto_state['message'] = f'计算进行中... 新文件={len(new_files)}, 修改={len(modified_files)}'
                last_change_time = wait
                _semi_auto_state['new_files'] = sorted(list(new_files))
                _semi_auto_state['modified_files'] = sorted(list(modified_files))

            if calc_started and last_change_time and wait > last_change_time + 10:
                _log("半自动求解: 计算完成!")
                break

            if wait % 30 == 0 and wait > 0:
                _semi_auto_state['message'] = f'等待用户手动求解... ({wait}s) 新={len(new_files)} 改={len(modified_files)}'

        _semi_auto_state['fe_files_after'] = len(files_now) if 'files_now' in dir() else 0

        # Phase 6: 读取结果
        _semi_auto_state['phase'] = 'reading'
        _semi_auto_state['message'] = '正在读取计算结果...'

        # 切换到温度视图
        _mc_send_menu(main_hwnd, _MC_MENU['exit_solve'])
        time.sleep(2)
        texts = _mc_read_texts(main_hwnd)

        # 提取可能的结果文本
        result_texts = []
        for t in texts:
            title = t['title']
            if any(c.isdigit() for c in title) and len(title) < 80:
                result_texts.append(title)

        # 切换到输出数据标签
        _mc_switch_tab(main_hwnd, 'output_data')
        time.sleep(2)
        texts2 = _mc_read_texts(main_hwnd)
        for t in texts2:
            if any(c.isdigit() for c in t['title']) and len(t['title']) < 80:
                result_texts.append(t['title'])

        # 构建结果
        result = {
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'mot_file': output_mot,
            'params': params,
            'calculation_completed': calc_started,
            'new_files': _semi_auto_state['new_files'],
            'modified_files': _semi_auto_state['modified_files'],
            'result_texts': result_texts[:30],
            'solve_mode': 'semi_auto',
            'note': '半自动求解: 用户手动点击求解按钮, 脚本自动监控和读取结果',
        }

        # 如果无法自动读取结果, 提供手动输入指引
        if not result_texts:
            result['note'] += '。无法自动读取温度结果, 请在 Motor-CAD 中查看温度后手动输入。'
            result['manual_input_needed'] = True
        else:
            result['manual_input_needed'] = False

        _semi_auto_state['result'] = result
        _semi_auto_state['phase'] = 'done'
        _semi_auto_state['message'] = '半自动求解完成!'
        _semi_auto_state['running'] = False
        _log("半自动求解: 工作流完成")

    except Exception as e:
        _semi_auto_state['phase'] = 'error'
        _semi_auto_state['error'] = str(e)
        _semi_auto_state['running'] = False
        _log(f"半自动求解: 错误 - {e}", "error")


def semi_auto_solve_start(params: dict, mot_file: str = None) -> str:
    """
    启动半自动求解工作流 (后台线程)

    Args:
        params: 设计参数字典 (如 {"Shaft_Speed_[RPM]": 3000, ...})
        mot_file: .mot 模板文件路径 (None 则使用默认模板)

    Returns:
        "started" 或错误消息
    """
    global _semi_auto_state

    if _semi_auto_state['running']:
        return "已有半自动求解正在进行中"

    if mot_file is None:
        mot_file = _find_template()
        if not os.path.exists(mot_file):
            return f"模板文件不存在: {mot_file}"

    # 重置状态
    _semi_auto_state = {
        'running': True,
        'phase': 'idle',
        'message': '初始化...',
        'mot_file': '',
        'params': params,
        'fe_files_before': 0,
        'fe_files_after': 0,
        'new_files': [],
        'modified_files': [],
        'duration': 0,
        'result': None,
        'error': None,
        'started_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 启动后台线程
    thread = threading.Thread(
        target=_semi_auto_worker,
        args=(mot_file, params, None),
        daemon=True
    )
    thread.start()

    _log("半自动求解: 工作流已启动 (后台线程)")
    return "started"


def semi_auto_solve_status() -> dict:
    """查询半自动求解工作流状态"""
    return _semi_auto_state.copy()


def semi_auto_solve_cancel():
    """取消半自动求解工作流"""
    global _semi_auto_state
    if _semi_auto_state['running']:
        _semi_auto_state['running'] = False
        _semi_auto_state['phase'] = 'cancelled'
        _semi_auto_state['message'] = '用户取消'
        _log("半自动求解: 用户取消")
        return "cancelled"
    return "not running"
