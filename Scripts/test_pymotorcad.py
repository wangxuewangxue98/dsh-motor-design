# -*- coding: utf-8 -*-
"""
测试 PyMotorCAD MotorCADCompatibility 连接 Motor-CAD v11.1.5
同时探索 ActiveX Methods 窗口
"""
import sys
import os
import time
import threading
import traceback

# 设置 Motor-CAD exe 路径
os.environ["PYMOTORCAD_EXE"] = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"

print("=" * 60)
print("[1] 测试 PyMotorCAD MotorCADCompatibility 连接")
print("=" * 60)

result = {"connected": False, "error": None, "version": None}

def try_connect():
    try:
        import ansys.motorcad.core as pymotorcad
        print(f"PyMotorCAD 版本: {pymotorcad.__version__}")
        
        # 尝试连接
        print("正在启动 MotorCADCompatibility...")
        mc = pymotorcad.MotorCADCompatibility(
            open_new_instance=True,  # 强制新实例
            enable_exceptions=True,
        )
        result["connected"] = True
        result["version"] = mc.get_variable("MotorCADVERSION")
        print(f"连接成功! 版本: {result['version']}")
        mc.quit()
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        print(f"连接失败: {e}")

# 用线程+超时运行
t = threading.Thread(target=try_connect, daemon=True)
t.start()
t.join(timeout=90)  # 90秒超时

if t.is_alive():
    print("\n*** 超时: 连接尝试超过 90 秒未完成 ***")
    print("Motor-CAD v11.1.5 可能不支持 /SCRIPTING 参数")
elif result["error"]:
    print(f"\n错误详情:\n{result.get('traceback', result['error'])}")

print("\n" + "=" * 60)
print("[2] 探索 ActiveX Methods 窗口")
print("=" * 60)

import win32gui
import win32process
import win32con
import ctypes

# 找到 Motor-CAD 主窗口和 ActiveX Methods 窗口
main_hwnd = None
activex_hwnd = None
activex_params_hwnd = None

all_windows = []

def enum_cb(hwnd, p):
    global main_hwnd, activex_hwnd, activex_params_hwnd
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        all_windows.append((hwnd, cls, title, vis, pid))
        if "TMainForm" in cls:
            main_hwnd = hwnd
        if "ActiveXMethods" in cls or "ActiveX" in title:
            activex_hwnd = hwnd
        if "ActiveXParameters" in cls:
            activex_params_hwnd = hwnd
    except:
        pass
    return True

win32gui.EnumWindows(enum_cb, None)

print(f"主窗口: hwnd={main_hwnd}")
print(f"ActiveX Methods 窗口: hwnd={activex_hwnd}")
print(f"ActiveX Parameters 窗口: hwnd={activex_params_hwnd}")

# 如果找到 ActiveX Methods 窗口，探索其子控件
if activex_hwnd:
    print(f"\n--- ActiveX Methods 窗口子控件 ---")
    children = []
    def enum_child(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            children.append((hwnd, cls, title, vis, rect))
        except:
            pass
        return True
    
    win32gui.EnumChildWindows(activex_hwnd, enum_child, None)
    print(f"子控件数: {len(children)}")
    for c in children:
        print(f"  hwnd={c[0]}, cls={c[1]}, text='{c[2]}', vis={c[3]}, rect={c[4]}")
    
    # 尝试显示这个窗口
    print("\n尝试显示 ActiveX Methods 窗口...")
    win32gui.ShowWindow(activex_hwnd, win32con.SW_SHOW)
    win32gui.SetForegroundWindow(activex_hwnd)
    time.sleep(1)
    
    # 再次枚举子控件
    children2 = []
    def enum_child2(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            children2.append((hwnd, cls, title, vis))
        except:
            pass
        return True
    win32gui.EnumChildWindows(activex_hwnd, enum_child2, None)
    print(f"显示后子控件数: {len(children2)}")
    for c in children2:
        print(f"  hwnd={c[0]}, cls={c[1]}, text='{c[2]}', vis={c[3]}")

# 检查 ActiveX Parameters 窗口
if activex_params_hwnd:
    print(f"\n--- ActiveX Parameters 窗口子控件 ---")
    children3 = []
    def enum_child3(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            children3.append((hwnd, cls, title, vis))
        except:
            pass
        return True
    win32gui.EnumChildWindows(activex_params_hwnd, enum_child3, None)
    print(f"子控件数: {len(children3)}")
    for c in children3:
        print(f"  hwnd={c[0]}, cls={c[1]}, text='{c[2]}', vis={c[3]}")

print("\n" + "=" * 60)
print("[3] 检查 Motor-CAD exe 中的 RPC/Scripting 相关字符串")
print("=" * 60)

exe_path = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
try:
    with open(exe_path, "rb") as f:
        data = f.read()
    
    # 搜索关键字符串
    keywords = [b"/SCRIPTING", b"/PORT=", b"RPC", b"jsonrpc", b"JSON-RPC", 
                b"MotorCAD.AppAutomation", b"RegisterClassFactory",
                b"DoSteadyStateAnalysis", b"DoMagneticCalculation",
                b"AppServer", b"IMotorCAD", b"IDispatch",
                b"CoCreateInstance", b"CoRegisterClassObject"]
    
    for kw in keywords:
        # 在二进制中搜索 ASCII 和 Unicode
        ascii_count = data.count(kw)
        unicode_kw = kw.decode('ascii').encode('utf-16-le')
        unicode_count = data.count(unicode_kw)
        if ascii_count > 0 or unicode_count > 0:
            print(f"  {kw.decode('ascii')}: ASCII={ascii_count}, Unicode={unicode_count}")
        else:
            print(f"  {kw.decode('ascii')}: 未找到")
except Exception as e:
    print(f"读取 exe 失败: {e}")

print("\n完成")
