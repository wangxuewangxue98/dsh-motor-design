# -*- coding: utf-8 -*-
"""
com_diagnose.py - Motor-CAD COM 连接诊断工具

逐步检查 COM 连接链路中的每个环节:
  1. Python 环境检查 (位数、版本、pywin32)
  2. Motor-CAD 安装检查 (exe 存在、版本)
  3. COM 注册表检查 (CLSID、ProgID、LocalServer32)
  4. ActiveX 注册模式检查 (免注册/单客户端/多客户端)
  5. COM Dispatch 连接测试
  6. 模板文件检查
  7. 基础 COM 方法测试 (LoadFromFile, GetVariable)

用法:
  python com_diagnose.py          # 完整诊断
  python com_diagnose.py --quick  # 快速检查 (不启动 Motor-CAD)
"""
import os
import sys
import time
import struct
import json
from datetime import datetime

# 确保路径
BASE_DIR = r"D:\MotorDesign"
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "Scripts"))
sys.path.insert(0, os.path.join(BASE_DIR, "motorcad_auto"))

RESULTS = []


def check(name, status, detail=""):
    """记录检查结果"""
    RESULTS.append({
        "check": name,
        "status": status,  # PASS / FAIL / WARN / INFO
        "detail": detail,
    })
    icon = {"PASS": "[OK]", "FAIL": "[FAIL]", "WARN": "[WARN]", "INFO": "[INFO]"}[status]
    print(f"  {icon} {name}: {detail}")


def run_diagnostics(quick=False):
    """运行完整诊断"""
    print("=" * 60)
    print("Motor-CAD COM 连接诊断")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ============================================================
    # 1. Python 环境
    # ============================================================
    print("\n--- 1. Python 环境 ---")
    print(f"  Python: {sys.executable}")
    print(f"  版本: {sys.version}")
    bits = struct.calcsize("P") * 8
    check("Python 位数", "INFO", f"{bits} 位")
    if bits == 64:
        check("Python 位数兼容性", "PASS",
              "64 位 Python 可通过 LocalServer32 (out-of-process) 连接 32 位 Motor-CAD")
    elif bits == 32:
        check("Python 位数兼容性", "PASS", "32 位 Python 与 32 位 Motor-CAD 完全兼容")
    else:
        check("Python 位数兼容性", "WARN", f"未知位数: {bits}")

    # pywin32
    try:
        import win32com.client
        import pythoncom
        import win32gui
        check("pywin32", "PASS", "win32com, pythoncom, win32gui 均可用")
    except ImportError as e:
        check("pywin32", "FAIL", f"导入失败: {e}")
        return print_diagnosis()

    # ============================================================
    # 2. Motor-CAD 安装
    # ============================================================
    print("\n--- 2. Motor-CAD 安装 ---")
    exe_path = os.getenv("MOTORCAD_EXE_PATH", r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe")
    install_dir = os.getenv("MOTORCAD_INSTALL_DIR", r"D:\Motor-CAD\11_1_5")

    if os.path.exists(exe_path):
        stat = os.stat(exe_path)
        check("Motor-CAD exe", "PASS", f"{exe_path} ({stat.st_size // 1024 // 1024} MB)")
    else:
        check("Motor-CAD exe", "FAIL", f"不存在: {exe_path}")
        return print_diagnosis()

    if os.path.isdir(install_dir):
        check("安装目录", "PASS", install_dir)
    else:
        check("安装目录", "WARN", f"不存在: {install_dir}")

    # 检查进程是否运行
    import subprocess
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Motor-CAD*", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=5, encoding="gbk", errors="replace"
        )
        if "Motor-CAD" in (result.stdout or ""):
            check("Motor-CAD 进程", "INFO", "正在运行")
        else:
            check("Motor-CAD 进程", "INFO", "未运行 (Dispatch 会自动启动)")
    except Exception:
        check("Motor-CAD 进程", "WARN", "无法检测")

    # ============================================================
    # 3. COM 注册表
    # ============================================================
    print("\n--- 3. COM 注册表 ---")
    clsid = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
    progid = "MotorCAD.AppAutomation"

    try:
        import winreg
        # 检查 HKLM
        found_hklm = False
        for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            for base_path in [
                f"Software\\Classes\\CLSID\\{clsid}",
                f"Software\\Classes\\Wow6432Node\\CLSID\\{clsid}",
            ]:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path, 0,
                                         winreg.KEY_READ | view)
                    # 读取 LocalServer32
                    try:
                        ls_key = winreg.OpenKey(key, "LocalServer32", 0, winreg.KEY_READ)
                        try:
                            ls_val = winreg.QueryValue(ls_key, None)
                            check("HKLM LocalServer32", "INFO", f"值: {ls_val}")
                            if ls_val and ls_val.lower().endswith(".bat"):
                                check("LocalServer32 格式", "WARN",
                                      f"指向 .bat 文件 (可能导致 0x800700c1): {ls_val}")
                            elif ls_val and ls_val.lower().endswith(".exe"):
                                check("LocalServer32 格式", "PASS", "指向 .exe 文件")
                        except OSError:
                            check("HKLM LocalServer32", "WARN", "键存在但无默认值")
                        winreg.CloseKey(ls_key)
                        found_hklm = True
                    except FileNotFoundError:
                        pass
                    winreg.CloseKey(key)
                except FileNotFoundError:
                    continue

        # 检查 HKCU 覆盖
        found_hkcu = False
        for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            for base_path in [
                f"Software\\Classes\\CLSID\\{clsid}",
                f"Software\\Classes\\Wow6432Node\\CLSID\\{clsid}",
            ]:
                try:
                    ls_path = base_path + "\\LocalServer32"
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ls_path, 0,
                                         winreg.KEY_READ | view)
                    try:
                        ls_val = winreg.QueryValue(key, None)
                        check("HKCU LocalServer32 覆盖", "PASS", f"值: {ls_val}")
                        if ls_val and ls_val.lower().endswith(".exe"):
                            check("HKCU 覆盖格式", "PASS", "指向 .exe (正确)")
                    except OSError:
                        check("HKCU LocalServer32 覆盖", "WARN", "键存在但无默认值")
                    winreg.CloseKey(key)
                    found_hkcu = True
                except FileNotFoundError:
                    continue

        if not found_hklm and not found_hkcu:
            check("COM 注册", "FAIL", "CLSID 未在注册表中找到, COM 不可用")
        elif not found_hkcu:
            check("HKCU 覆盖", "WARN", "未写入 HKCU 覆盖 (motor_tools 导入时会自动写入)")

    except ImportError:
        check("注册表检查", "WARN", "winreg 不可用")

    # ProgID 检查
    try:
        from win32com.client.gencache import EnsureDispatch
        # 不实际调用, 只检查 ProgID 是否可解析
        check("ProgID", "INFO", f"{progid} (将在 Dispatch 时验证)")
    except Exception:
        pass

    # ============================================================
    # 4. ActiveX 注册模式 (即使快速模式也检查, 因为不影响 Motor-CAD)
    # ============================================================
    print("\n--- 4. ActiveX 注册模式 ---")
    try:
        from motorcad_auto.connector import MotorCADConnector
        from motorcad_auto.config import MotorCADConfig

        config = MotorCADConfig(exe_path=exe_path, install_dir=install_dir, progid=progid)
        connector = MotorCADConnector(config)

        # 查找主窗口
        hwnd = connector.find_main_window(timeout=5)
        if hwnd:
            mode = connector.check_activex_mode()
            if mode == "single":
                check("ActiveX 模式", "PASS", "单客户端设置 (COM 可用)")
            elif mode == "multi":
                check("ActiveX 模式", "PASS", "多客户端设置 (COM 可用)")
            elif mode == "none":
                check("ActiveX 模式", "FAIL",
                      "免注册! COM 不可用! 请在 Motor-CAD 菜单中切换: "
                      "默认设置 -> 控件注册 -> 单客户端设置")
            else:
                check("ActiveX 模式", "WARN", f"未知模式: {mode}")
        else:
            check("Motor-CAD 窗口", "INFO", "未运行 (Dispatch 会自动启动)")
    except Exception as e:
        check("ActiveX 检查", "WARN", f"异常: {e}")

    if quick:
        print("\n--- 快速模式: 跳过 COM Dispatch 连接测试 ---")
        # 仍然检查模板
        print("\n--- 6. 模板文件 ---")
        try:
            from template_manager import get_template_info
            info = get_template_info()
            if info["template_exists"]:
                check("模板文件", "PASS", info["template_path"])
            else:
                check("模板文件", "FAIL", "模板不存在, 运行 template_manager.py 自动创建")
        except ImportError:
            template_path = os.path.join(BASE_DIR, "Templates", "Templates.mot")
            if os.path.exists(template_path):
                check("模板文件", "PASS", template_path)
            else:
                check("模板文件", "WARN", f"不存在: {template_path}")
        return print_diagnosis()

    # ============================================================
    # 5. COM Dispatch 连接
    # ============================================================
    print("\n--- 5. COM Dispatch 连接 ---")
    print("  (启动 Motor-CAD, 预计 60-90 秒)...")
    t0 = time.time()
    try:
        connector = MotorCADConnector(config)
        if connector.connect():
            t_connect = time.time() - t0
            check("COM Dispatch", "PASS", f"连接成功 ({t_connect:.1f}s)")
        else:
            t_connect = time.time() - t0
            check("COM Dispatch", "FAIL", f"connect() 返回 False ({t_connect:.1f}s)")
            return print_diagnosis()
    except Exception as e:
        t_connect = time.time() - t0
        err = str(e)
        check("COM Dispatch", "FAIL", f"连接失败 ({t_connect:.1f}s): {err}")

        # 诊断错误类型
        if "0x80080005" in err.lower() or "server execution" in err.lower():
            check("错误诊断", "WARN",
                  "CO_E_SERVER_EXEC_FAILURE: ActiveX 可能处于 '免注册' 模式")
        elif "0x800700c1" in err.lower() or "bad exe" in err.lower():
            check("错误诊断", "WARN",
                  "位数不匹配: LocalServer32 可能指向 .bat 文件")
        elif "0x800401f3" in err.lower() or "invalid class string" in err.lower():
            check("错误诊断", "WARN",
                  "ProgID 未注册")
        return print_diagnosis()

    # ============================================================
    # 6. 模板文件
    # ============================================================
    print("\n--- 6. 模板文件 ---")
    try:
        from template_manager import get_template_info
        info = get_template_info()
        if info["template_exists"]:
            check("模板文件", "PASS", info["template_path"])
        else:
            check("模板文件", "FAIL", "模板不存在, 运行 template_manager.py 自动创建")
    except ImportError:
        # 降级检查
        template_path = os.path.join(BASE_DIR, "Templates", "Templates.mot")
        if os.path.exists(template_path):
            check("模板文件", "PASS", template_path)
        else:
            check("模板文件", "WARN", f"不存在: {template_path}")

    # ============================================================
    # 7. 基础 COM 方法测试
    # ============================================================
    print("\n--- 7. 基础 COM 方法测试 ---")
    mc = connector.com_object

    # GetVariable (安全变量)
    try:
        ret = mc.GetVariable("Program_Version")
        if isinstance(ret, tuple) and len(ret) >= 2:
            check("GetVariable(Program_Version)", "PASS", f"返回: {ret}")
        else:
            check("GetVariable(Program_Version)", "PASS", f"返回: {ret}")
    except Exception as e:
        check("GetVariable(Program_Version)", "FAIL", f"异常: {e}")

    # GetVariable (Pole_Number)
    try:
        ret = mc.GetVariable("Pole_Number")
        check("GetVariable(Pole_Number)", "PASS", f"返回: {ret}")
    except Exception as e:
        check("GetVariable(Pole_Number)", "WARN", f"异常: {e}")

    # LoadFromFile
    try:
        from template_manager import get_default_template
        template = get_default_template()
        if template:
            ret = mc.LoadFromFile(template)
            time.sleep(3)
            check("LoadFromFile", "PASS", f"返回: {ret}, 文件: {os.path.basename(template)}")
        else:
            check("LoadFromFile", "WARN", "无可用模板文件")
    except Exception as e:
        check("LoadFromFile", "FAIL", f"异常: {e}")

    # ShowMagneticContext
    try:
        mc.ShowMagneticContext()
        time.sleep(2)
        check("ShowMagneticContext", "PASS", "电磁上下文切换成功")
    except Exception as e:
        check("ShowMagneticContext", "FAIL", f"异常: {e}")

    # 断开连接
    try:
        connector.disconnect()
        check("Disconnect", "PASS", "COM 连接已断开")
    except Exception:
        pass

    return print_diagnosis()


def print_diagnosis():
    """打印诊断摘要"""
    print("\n" + "=" * 60)
    print("诊断摘要")
    print("=" * 60)

    pass_count = sum(1 for r in RESULTS if r["status"] == "PASS")
    fail_count = sum(1 for r in RESULTS if r["status"] == "FAIL")
    warn_count = sum(1 for r in RESULTS if r["status"] == "WARN")

    print(f"  通过: {pass_count}  失败: {fail_count}  警告: {warn_count}")

    if fail_count == 0 and warn_count == 0:
        print("\n  ✓ 所有检查通过, COM 自动化应该可用")
    elif fail_count == 0:
        print("\n  ⚠ 有警告项, COM 可能可用但建议检查警告内容")
    else:
        print("\n  ✗ 有失败项, COM 自动化不可用")
        print("  失败项:")
        for r in RESULTS:
            if r["status"] == "FAIL":
                print(f"    - {r['check']}: {r['detail']}")

    # 保存结果
    result_path = os.path.join(BASE_DIR, "Reports", "com_diagnosis.json")
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "python": sys.executable,
            "results": RESULTS,
            "summary": {"pass": pass_count, "fail": fail_count, "warn": warn_count},
        }, f, indent=2, ensure_ascii=False)
    print(f"\n  诊断报告已保存: {result_path}")

    return RESULTS


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run_diagnostics(quick=quick)
