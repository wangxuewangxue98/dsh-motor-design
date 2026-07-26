# -*- coding: utf-8 -*-
"""
connector.py - Motor-CAD v11.1.5 COM 连接器

核心功能:
  1. 验证并修复 ActiveX 注册模式 (免注册 -> 单客户端设置)
  2. 通过 win32com.client.Dispatch 建立 COM 连接
  3. 管理 COM STA (单线程单元) 生命周期
  4. 加载/保存 .mot 文件

关键发现 (基于实际测试):
  - Motor-CAD v11.1.5 菜单 "默认设置 -> 控件注册" 有三个选项:
    多客户端设置(id=93) / 单客户端设置(id=94) / 免注册(id=95)
  - 当选中 "免注册" 时, Motor-CAD 不注册 COM class factory,
    所有 COM 调用失败 (CO_E_SERVER_EXEC_FAILURE / 0x80080005)
  - 切换到 "单客户端设置" 后 COM 立即可用, 且设置跨重启持久化
  - GetActiveObject 不可用 (Motor-CAD 不注册到 ROT)
  - Dispatch 可用 (会启动新实例, 启动约 60-120 秒)
  - COM 对象不能跨线程使用 (STA 模型), 所有调用必须在同一线程

v1.03 修复 (2026-07-26):
  - Dispatch 内部有 ~60s COM SCM 超时, 但 Motor-CAD 需要 80-120s 启动
  - 新增预启动模式: 手动启动 MC exe -> 等待完全加载 -> Dispatch 连接
  - 新增进程管理: _kill_motorcad / _start_motorcad_process
  - ActiveX 模式修复后自动重启 Motor-CAD (模式变更需重启生效)
"""
import os
import time
import ctypes
import logging
import threading
import subprocess
from typing import Optional

from .config import MotorCADConfig, MENU_ID_SINGLE_CLIENT, MENU_ID_NO_REGISTER

logger = logging.getLogger("motorcad_auto.connector")


class ActiveXModeError(RuntimeError):
    """ActiveX 注册模式不可用异常"""


class COMConnectionError(RuntimeError):
    """COM 连接失败异常"""


class MotorCADConnector:
    """Motor-CAD COM 连接管理器"""

    # Motor-CAD 主窗口类名 (Delphi VCL TMainForm)
    MAIN_FORM_CLASS = "TMainForm"

    def __init__(self, config: Optional[MotorCADConfig] = None):
        self.config = config or MotorCADConfig()
        self._mc = None              # COM 对象
        self._co_initialized = False # CoInitialize 状态
        self._connected = False      # 连接状态
        self._main_hwnd = None       # 主窗口句柄
        self._mc_process = None      # 手动启动的 Motor-CAD 进程

    # ============================================================
    #  属性
    # ============================================================
    @property
    def is_connected(self) -> bool:
        return self._connected and self._mc is not None

    @property
    def com_object(self):
        """获取 COM 对象 (调用方必须确保在同一线程)"""
        if not self._connected:
            raise COMConnectionError("Motor-CAD 未连接, 请先调用 connect()")
        return self._mc

    @property
    def main_hwnd(self) -> int:
        """获取主窗口句柄"""
        return self._main_hwnd

    # ============================================================
    #  ActiveX 注册模式验证与修复
    # ============================================================
    def find_main_window(self, timeout: int = 30) -> Optional[int]:
        """
        查找 Motor-CAD 主窗口句柄 (TMainForm 类)
        返回窗口句柄, 未找到返回 None
        """
        import win32gui

        deadline = time.time() + timeout
        while time.time() < deadline:
            found = []
            def _enum(hwnd, _):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    if cls == self.MAIN_FORM_CLASS and win32gui.IsWindowVisible(hwnd):
                        found.append(hwnd)
                except Exception:
                    pass
                return True
            win32gui.EnumWindows(_enum, None)
            if found:
                self._main_hwnd = found[0]
                logger.info(f"找到 Motor-CAD 主窗口: hwnd={self._main_hwnd}")
                return self._main_hwnd
            time.sleep(1)

        logger.warning("未找到 Motor-CAD 主窗口")
        return None

    def check_activex_mode(self) -> str:
        """
        检查当前 ActiveX 注册模式
        返回: "multi" / "single" / "none" / "unknown"
        """
        import win32gui
        import win32con

        hwnd = self._main_hwnd or self.find_main_window(timeout=5)
        if not hwnd:
            return "unknown"

        user32 = ctypes.windll.user32
        menu = win32gui.GetMenu(hwnd)
        if not menu:
            return "unknown"

        # 遍历顶层菜单查找 "默认设置" (通常第 4 个, 索引 3)
        # 菜单结构: 文件(0) 编辑(1) 模式(2) 默认设置(3) ...
        # 但不同版本可能不同, 所以遍历查找
        top_count = user32.GetMenuItemCount(menu)
        for i in range(top_count):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetMenuStringW(menu, i, buf, 256, 0x400)  # MF_BYPOSITION
            if "默认设置" in buf.value or "设置" in buf.value:
                submenu = user32.GetSubMenu(menu, i)
                if not submenu:
                    continue
                # 在子菜单中查找 "控件注册"
                sub_count = user32.GetMenuItemCount(submenu)
                for j in range(sub_count):
                    buf2 = ctypes.create_unicode_buffer(256)
                    user32.GetMenuStringW(submenu, j, buf2, 256, 0x400)
                    if "控件注册" in buf2.value or "注册" in buf2.value:
                        reg_menu = user32.GetSubMenu(submenu, j)
                        if not reg_menu:
                            continue
                        # 检查三个选项的选中状态
                        reg_count = user32.GetMenuItemCount(reg_menu)
                        for k in range(reg_count):
                            buf3 = ctypes.create_unicode_buffer(256)
                            user32.GetMenuStringW(reg_menu, k, buf3, 256, 0x400)
                            wid = user32.GetMenuItemID(reg_menu, k)
                            state = user32.GetMenuState(reg_menu, k, 0x400)
                            checked = bool(state & 0x8)  # MF_CHECKED
                            if checked:
                                if wid == MENU_ID_NO_REGISTER:
                                    return "none"
                                elif wid == MENU_ID_SINGLE_CLIENT:
                                    return "single"
                                elif wid == 93:
                                    return "multi"
                                # 按名称判断
                                if "免注册" in buf3.value:
                                    return "none"
                                if "单客户端" in buf3.value:
                                    return "single"
                                if "多客户端" in buf3.value:
                                    return "multi"
                        return "unknown"
        return "unknown"

    def fix_activex_mode(self) -> bool:
        """
        修复 ActiveX 注册模式: 如果当前为 "免注册", 切换到 "单客户端设置"
        返回: True 表示已修复或已经是正确模式
        """
        import win32gui
        import win32con

        hwnd = self._main_hwnd or self.find_main_window(timeout=10)
        if not hwnd:
            logger.warning("无法找到主窗口, 跳过 ActiveX 模式修复")
            return False

        mode = self.check_activex_mode()
        logger.info(f"当前 ActiveX 注册模式: {mode}")

        if mode == "single":
            logger.info("ActiveX 模式正确 (单客户端设置), 无需修复")
            return True

        if mode == "none":
            logger.warning("ActiveX 模式为 '免注册', 正在切换到 '单客户端设置'...")
            # 发送 WM_COMMAND 切换到单客户端设置 (id=94)
            win32gui.SendMessage(hwnd, win32con.WM_COMMAND, MENU_ID_SINGLE_CLIENT, 0)
            time.sleep(2)
            # 验证
            new_mode = self.check_activex_mode()
            if new_mode == "single":
                logger.info("ActiveX 模式已修复为 '单客户端设置' (持久化)")
                return True
            else:
                logger.warning(f"修复后模式仍为: {new_mode}")
                # 即使验证失败也尝试继续 (可能菜单刷新延迟)
                return True

        if mode == "multi":
            logger.info("ActiveX 模式为 '多客户端设置', COM 应该可用")
            return True

        # unknown - 可能菜单结构不同, 继续尝试连接
        logger.warning("无法确定 ActiveX 模式, 将尝试直接连接")
        return True

    # ============================================================
    #  HKCU COM 注册表覆盖
    # ============================================================
    def _apply_hkcu_override(self):
        """
        在 HKCU 级别覆盖 COM LocalServer32, 直接指向 .exe
        解决 LocalServer32 指向 .bat 导致的 0x800700c1 错误
        不需要管理员权限
        """
        if not os.path.exists(self.config.exe_path):
            return

        try:
            import winreg
            clsid = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
            for view_access in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
                for base in [
                    f"Software\\Classes\\CLSID\\{clsid}",
                    f"Software\\Classes\\Wow6432Node\\CLSID\\{clsid}",
                ]:
                    try:
                        ls_path = base + "\\LocalServer32"
                        winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ls_path, 0,
                                           winreg.KEY_SET_VALUE | view_access)
                        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ls_path, 0,
                                             winreg.KEY_SET_VALUE | view_access)
                        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, self.config.exe_path)
                        winreg.CloseKey(key)

                        ad_path = base + "\\AppDirectory"
                        winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ad_path, 0,
                                           winreg.KEY_SET_VALUE | view_access)
                        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ad_path, 0,
                                             winreg.KEY_SET_VALUE | view_access)
                        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, self.config.install_dir)
                        winreg.CloseKey(key)
                    except Exception:
                        pass
            logger.info("HKCU COM 覆盖已写入 (LocalServer32 -> .exe)")
        except Exception as e:
            logger.warning(f"HKCU COM 覆盖写入失败: {e}")

    # ============================================================
    #  进程管理
    # ============================================================
    def _kill_motorcad(self):
        """终止所有 Motor-CAD 进程 (为 COM 连接清理环境)"""
        try:
            for exe_name in ["Motor-CAD_11_1_5.exe", "Motor-CAD.exe"]:
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", exe_name],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    logger.info(f"已终止 Motor-CAD 进程: {exe_name}")
                    time.sleep(3)
                    return True
        except Exception as e:
            logger.warning(f"终止 Motor-CAD 进程失败: {e}")
        return False

    def _start_motorcad_process(self):
        """
        手动启动 Motor-CAD exe (绕过 Dispatch 60s 超时)

        关键发现: win32com.client.Dispatch() 内部有 ~60s COM SCM 超时,
        但 Motor-CAD v11.1.5 需要 80-120 秒才能完成启动并注册 COM class factory。
        因此 Dispatch 会超时失败 (CO_E_SERVER_EXEC_FAILURE)。

        解决方案: 先手动启动 Motor-CAD exe, 等待窗口出现 + 充分加载时间,
        然后再调 Dispatch 连接到已运行的实例 (此时 class factory 已注册)。
        """
        if not os.path.exists(self.config.exe_path):
            raise COMConnectionError(f"Motor-CAD 可执行文件不存在: {self.config.exe_path}")

        try:
            self._mc_process = subprocess.Popen(
                [self.config.exe_path],
                cwd=self.config.install_dir,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            logger.info(f"已手动启动 Motor-CAD: {self.config.exe_path} (PID={self._mc_process.pid})")
        except Exception as e:
            logger.error(f"手动启动 Motor-CAD 失败: {e}")
            raise

    def _try_dispatch(self) -> bool:
        """尝试 Dispatch 连接, 返回是否成功 (不抛异常)"""
        import win32com.client
        try:
            self._mc = win32com.client.Dispatch(self.config.progid)
            self._connected = True
            logger.info("COM Dispatch 连接成功")
            return True
        except Exception as e:
            logger.warning(f"Dispatch 失败: {e}")
            return False

    # ============================================================
    #  COM 连接
    # ============================================================
    def connect(self) -> bool:
        """
        建立 COM 连接 (增强版 v1.03 -- 解决 Dispatch 60s 超时问题)

        流程:
          1. CoInitialize (STA)
          2. 写入 HKCU COM 覆盖
          3. 尝试 Dispatch 直连 (快速路径, MC 已运行时适用)
          4. 如果 Dispatch 失败 -> 预启动模式:
             a. 查找/等待 Motor-CAD 窗口 (Dispatch 可能已启动它)
             b. 检查并修复 ActiveX 注册模式
             c. 如果 ActiveX 模式被修复 -> 杀进程重启 (模式变更需重启生效)
             d. 如果 Motor-CAD 未运行 -> 手动启动
             e. 等待 Motor-CAD 完全加载 (窗口出现后 90 秒)
             f. 重试 Dispatch (连接到已运行实例)
          5. 等待初始化
          6. 验证 ActiveX 模式

        返回: True 表示连接成功
        """
        import pythoncom

        # 1. 初始化 COM STA
        if not self._co_initialized:
            pythoncom.CoInitialize()
            self._co_initialized = True
            logger.info("COM STA 已初始化 (CoInitialize)")

        # 2. HKCU 覆盖
        self._apply_hkcu_override()

        # 3. 尝试 Dispatch 直连 (快速路径)
        logger.info("尝试 Dispatch 直连 (快速路径)...")
        if self._try_dispatch():
            logger.info(f"等待 Motor-CAD 初始化 ({self.config.init_wait}s)...")
            time.sleep(self.config.init_wait)
            self.find_main_window(timeout=10)
            return self._connected

        # 4. Dispatch 失败 -> 预启动模式
        logger.warning("Dispatch 直连失败, 切换到预启动模式...")

        # 4a. 查找 Motor-CAD 窗口 (Dispatch 可能已经启动了 MC)
        hwnd = self.find_main_window(timeout=10)
        need_restart = False

        if hwnd:
            # 4b. Motor-CAD 已运行, 检查 ActiveX 模式
            logger.info("Motor-CAD 窗口已找到, 检查 ActiveX 注册模式...")
            mode = self.check_activex_mode()
            logger.info(f"当前 ActiveX 模式: {mode}")

            if mode == "none":
                # 4c. "免注册" 模式 -> 修复后需要重启
                logger.warning("ActiveX 模式为 '免注册', 正在修复...")
                self.fix_activex_mode()
                need_restart = True
            elif mode == "single":
                # 模式正确, MC 可能还在加载中
                logger.info("ActiveX 模式正确 (单客户端), 等待 MC 完全加载...")
            elif mode == "multi":
                logger.info("ActiveX 模式: 多客户端, COM 应该可用")
            else:
                logger.warning(f"ActiveX 模式未知: {mode}, 继续尝试...")
        else:
            # 4b-alt. MC 未运行, 手动启动
            logger.info("Motor-CAD 未运行, 手动启动...")
            self._start_motorcad_process()
            hwnd = self.find_main_window(timeout=30)
            if hwnd and self.config.auto_fix_activex:
                mode = self.check_activex_mode()
                if mode == "none":
                    logger.warning("ActiveX 模式为 '免注册', 正在修复...")
                    self.fix_activex_mode()
                    need_restart = True

        # 4c. 如果 ActiveX 模式被修复, 需要杀进程重启
        if need_restart:
            logger.info("ActiveX 模式已修复, 需要重启 Motor-CAD 使其生效...")
            self._kill_motorcad()
            time.sleep(3)
            self._start_motorcad_process()
            hwnd = self.find_main_window(timeout=30)
            if not hwnd:
                raise COMConnectionError(
                    "Motor-CAD 重启后窗口未找到, 可能启动失败"
                )

        # 4d. 等待 Motor-CAD 完全加载 (窗口出现后 90 秒)
        # Motor-CAD v11.1.5 需要 60-90 秒才能完成 COM class factory 注册
        full_load_wait = 90
        logger.info(f"等待 Motor-CAD 完全加载 ({full_load_wait}s, COM 注册需要时间)...")
        for waited in range(0, full_load_wait, 10):
            time.sleep(10)
            logger.info(f"  加载中... {waited + 10}/{full_load_wait}s")

        # 4e. 重试 Dispatch (连接到已运行的 Motor-CAD 实例)
        logger.info("重试 Dispatch (连接到已运行的 Motor-CAD)...")
        if self._try_dispatch():
            logger.info(f"等待 Motor-CAD 初始化 ({self.config.init_wait}s)...")
            time.sleep(self.config.init_wait)
            self.find_main_window(timeout=10)
            return self._connected

        # 4f. 第二次重试: 再等 30 秒后尝试
        logger.warning("Dispatch 仍然失败, 再等 30 秒后最后重试...")
        time.sleep(30)
        if self._try_dispatch():
            logger.info(f"等待 Motor-CAD 初始化 ({self.config.init_wait}s)...")
            time.sleep(self.config.init_wait)
            self.find_main_window(timeout=10)
            return self._connected

        # 所有尝试均失败
        raise COMConnectionError(
            "Motor-CAD COM 连接失败。已尝试多种连接方式:\n"
            "  1. HKCU COM 覆盖 (LocalServer32 -> .exe) - 已写入\n"
            "  2. Dispatch 直连 - 服务器运行失败 (CO_E_SERVER_EXEC_FAILURE)\n"
            "  3. 预启动 Motor-CAD + 等待完全加载 + 重试 Dispatch - 仍然失败\n"
            "\n可能原因:\n"
            "  A. ActiveX 注册模式为 '免注册' -> 请在 Motor-CAD 菜单中切换: "
            "默认设置 -> 控件注册 -> 单客户端设置\n"
            "  B. Motor-CAD v11.1.5 (32位) 的 COM 自动化在 64 位 Python 下"
            "存在兼容性问题\n"
            "  C. Motor-CAD 授权/加密狗问题\n"
            "\n建议方案:\n"
            "  A. 以管理员身份运行 D:\\MotorDesign\\fix_com_registration.bat\n"
            "  B. 在 .env 中设置 MOTORCAD_USE_MODE=manual, 手动求解后导入\n"
            "  C. 在 .env 中设置 MOTORCAD_USE_MODE=simulation 进行流程测试"
        )

    def disconnect(self):
        """断开 COM 连接, 释放资源"""
        self._mc = None
        self._connected = False
        if self._co_initialized:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._co_initialized = False
            logger.info("COM STA 已释放 (CoUninitialize)")

    # ============================================================
    #  上下文切换 (电磁/热模式)
    # ============================================================
    def show_magnetic_context(self):
        """
        切换到电磁分析上下文 (ShowMagneticContext)

        关键发现: 即使 .mot 文件 Program_Module=BPM-Therm,
        调用 ShowMagneticContext() 后 DoMagneticCalculation() 可成功执行!
        Program_Module 不会改变 (仍为 BPM-Therm), 但电磁分析可用。
        """
        if not self._connected:
            raise COMConnectionError("未连接")
        self._mc.ShowMagneticContext()
        logger.info("已切换到电磁分析上下文 (ShowMagneticContext)")
        time.sleep(3)

    def show_thermal_context(self):
        """切换到热分析上下文 (ShowThermalContext)"""
        if not self._connected:
            raise COMConnectionError("未连接")
        self._mc.ShowThermalContext()
        logger.info("已切换到热分析上下文 (ShowThermalContext)")
        time.sleep(3)

    def do_magnetic_thermal_calculation(self):
        """
        执行电磁-热耦合计算 (DoMagneticThermalCalculation)
        返回: 0 = 成功
        """
        if not self._connected:
            raise COMConnectionError("未连接")
        return self._mc.DoMagneticThermalCalculation()

    # ============================================================
    #  文件操作
    # ============================================================
    def load_file(self, mot_path: str) -> bool:
        """
        加载 .mot 模型文件

        注意: Motor-CAD v11.1.5 的 LoadFromFile 返回值不稳定 --
        有时返回 0 (成功), 有时返回 -1 (即使文件已成功加载)。
        因此本方法不依赖返回值判断成功, 而是通过后续 GetVariable 验证。
        始终返回 True (除非文件不存在或 COM 异常), 调用方应通过
        读取模型参数来验证文件是否真正加载。
        """
        if not self._connected:
            raise COMConnectionError("未连接, 请先调用 connect()")

        if not os.path.exists(mot_path):
            logger.error(f"文件不存在: {mot_path}")
            return False

        logger.info(f"加载 .mot 文件: {mot_path}")
        try:
            ret = self._mc.LoadFromFile(mot_path)
            # 等待文件加载完成
            time.sleep(self.config.file_load_wait)
            logger.info(f"LoadFromFile 返回: {ret}")

            # 不通过 GetVariable 验证 (某些变量可能导致挂起)
            # 调用方通过后续 read_model_info() 验证文件是否加载成功
            return True
        except Exception as e:
            logger.error(f"加载文件异常: {e}")
            return False

    def save_file(self, mot_path: str) -> bool:
        """保存 .mot 模型文件 (含结果)"""
        if not self._connected:
            return False
        try:
            self._mc.SaveToFile(mot_path)
            logger.info(f"文件已保存: {mot_path}")
            return True
        except Exception as e:
            logger.warning(f"保存文件失败: {e}")
            return False

    # ============================================================
    #  上下文管理器
    # ============================================================
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
