#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD COM 测试 v3 - 先正常启动 Motor-CAD，再连接 COM
策略: 避免 -Embedding 导致的挂起问题
"""
import sys
import os
import time
import subprocess
import traceback

import pythoncom
pythoncom.CoInitialize()

import win32com.client

PROGID = "MotorCAD.AppAutomation"
EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
EXE_DIR = r"D:\Motor-CAD\11_1_5"

print("=" * 60)
print("Motor-CAD COM 测试 v3 (正常启动 + COM 连接)")
print("=" * 60)

# Step 1: 正常启动 Motor-CAD (不带 -Embedding)
print("\n[1/3] 正常启动 Motor-CAD (无 -Embedding)...")
proc = subprocess.Popen([EXE_PATH], cwd=EXE_DIR)
print(f"  已启动 PID={proc.pid}")

# Step 2: 等待 Motor-CAD 变为响应状态
print("\n[2/3] 等待 Motor-CAD 完全启动...")
import ctypes
import ctypes.wintypes

# Use WaitForInputIdle to wait for the app to be ready
WAIT_TIMEOUT = 120  # seconds
poll_interval = 3
start_time = time.time()
responsive = False

while time.time() - start_time < WAIT_TIMEOUT:
    time.sleep(poll_interval)
    elapsed = time.time() - start_time
    
    # Check if process is still alive
    if proc.poll() is not None:
        print(f"  Motor-CAD exited unexpectedly (code={proc.returncode})")
        break
    
    # Check if the process is responding using the Windows API
    try:
        import win32process
        import win32gui
        import win32con
        
        # Find the main window
        hwnd = win32gui.FindWindow(None, None)
        # Use EnumWindows to find Motor-CAD's window
        mc_hwnd = None
        
        def callback(hwnd, lparam):
            global mc_hwnd
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == proc.pid and win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        mc_hwnd = hwnd
                        return False  # Stop enumeration
            except:
                pass
            return True
        
        win32gui.EnumWindows(callback, None)
        
        if mc_hwnd:
            # Check if window is responding (SendMessageTimeout)
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.wintypes.DWORD()
            ret = ctypes.windll.user32.SendMessageTimeoutW(
                mc_hwnd, 0x0000, 0, 0, SMTO_ABORTIFHUNG, 2000, ctypes.byref(result)
            )
            if ret:
                title = win32gui.GetWindowText(mc_hwnd)
                print(f"  [{elapsed:.0f}s] Motor-CAD 响应正常! 窗口标题: '{title}'")
                responsive = True
                break
            else:
                print(f"  [{elapsed:.0f}s] 窗口存在但未响应 (等待中...)")
        else:
            print(f"  [{elapsed:.0f}s] 等待窗口出现...")
    except Exception as e:
        print(f"  [{elapsed:.0f}s] 检查状态时: {e}")

if not responsive:
    elapsed = time.time() - start_time
    print(f"  {elapsed:.0f}s 后仍未响应")
    
    # Try connecting anyway - sometimes COM works even if window check fails
    print("  尝试直接连接 COM...")

# Step 3: 尝试 COM 连接
print("\n[3/3] 尝试 COM 连接...")

mc = None
methods_tried = []

# Method A: GetActiveObject (connect to running instance)
print("\n  [A] GetActiveObject (连接已运行实例)...")
try:
    mc = win32com.client.GetActiveObject(PROGID)
    print(f"  -> 成功! GetActiveObject 连接成功")
    methods_tried.append(("GetActiveObject", True, ""))
except Exception as e:
    print(f"  -> 失败: {e}")
    methods_tried.append(("GetActiveObject", False, str(e)))

# Method B: Dispatch (might connect to running instance or start new one)
if mc is None:
    print("\n  [B] Dispatch (创建/连接 Motor-CAD)...")
    dispatch_start = time.time()
    try:
        mc = win32com.client.Dispatch(PROGID)
        dispatch_time = time.time() - dispatch_start
        print(f"  -> 成功! Dispatch 连接成功 (耗时 {dispatch_time:.1f}s)")
        methods_tried.append(("Dispatch", True, f"{dispatch_time:.1f}s"))
    except Exception as e:
        dispatch_time = time.time() - dispatch_start
        print(f"  -> 失败 ({dispatch_time:.1f}s): {e}")
        methods_tried.append(("Dispatch", False, str(e)))

# Method C: DispatchEx (always create new instance)
if mc is None:
    print("\n  [C] DispatchEx (强制新实例)...")
    try:
        mc = win32com.client.DispatchEx(PROGID)
        print(f"  -> 成功! DispatchEx 连接成功")
        methods_tried.append(("DispatchEx", True, ""))
    except Exception as e:
        print(f"  -> 失败: {e}")
        methods_tried.append(("DispatchEx", False, str(e)))

# Method D: CoCreateInstance via CLSID
if mc is None:
    print("\n  [D] CoCreateInstance (直接 CLSID)...")
    try:
        CLSID = "{4ACD06EB-5486-42AF-91DB-FB752E64C465}"
        mc = win32com.client.Dispatch(f"new:{CLSID}")
        print(f"  -> 成功! CLSID Dispatch 连接成功")
        methods_tried.append(("CLSID Dispatch", True, ""))
    except Exception as e:
        print(f"  -> 失败: {e}")
        methods_tried.append(("CLSID Dispatch", False, str(e)))

# Summary
print("\n" + "=" * 60)
print("测试结果汇总")
print("=" * 60)
for name, success, info in methods_tried:
    status = "OK" if success else "FAIL"
    print(f"  {name:20s}: {status} {info}")

if mc:
    print("\nCOM 连接成功! 测试基本操作...")
    try:
        mc.Visible = True
        print("  Visible = True: OK")
    except Exception as e:
        print(f"  Visible: {e}")
    
    # Try loading template
    template = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
    if os.path.exists(template):
        try:
            print(f"  LoadFromFile({template})...")
            mc.LoadFromFile(template)
            print("  -> OK")
        except Exception as e:
            print(f"  -> {e}")
    
    # Try SetVariable / GetVariable
    try:
        mc.SetVariable("Speed", 3000)
        val = mc.GetVariable("Speed")
        print(f"  SetVariable/GetVariable: Speed = {val}")
    except Exception as e:
        print(f"  SetVariable/GetVariable: {e}")
    
    print("\nMotor-CAD COM 自动化可用!")
    print("保持 Motor-CAD 运行，不退出。")
else:
    print("\n所有 COM 连接方法均失败。")
    print("\n可能原因:")
    print("  1. Motor-CAD v11.1.5 不注册 COM class factory")
    print("  2. 32/64 位 COM marshaling 兼容性问题")
    print("  3. Motor-CAD 需要特定启动模式")
    print("\n建议:")
    print("  - 继续使用手动求解工作流")
    print("  - 或尝试安装 32 位 Python 进行 COM 连接")

pythoncom.CoUninitialize()
