#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD COM 连接测试 v2 (32位 Python)

关键策略:
  1. 用 -Embedding 参数启动 Motor-CAD (COM 服务器标准启动方式)
  2. 等待 Motor-CAD 完全加载
  3. 用 32位 Python Dispatch 连接
  4. 如果成功: DoSteadyStateAnalysis + GetVariable 读取结果
"""
import sys
import os
import time
import struct
import subprocess

print(f"Python 位数: {struct.calcsize('P') * 8}")
print(f"Python 路径: {sys.executable}")
print()

# ============================================================
# Step 1: 启动 Motor-CAD (用 -Embedding 参数)
# ============================================================
print("=" * 60)
print("Step 1: 启动 Motor-CAD")
print("=" * 60)

mcad_exe = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
mot_file = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

# 先检查是否已在运行
import win32gui
import win32process

def find_mc():
    """查找已运行的 Motor-CAD"""
    found = []
    def cb(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == 'TMainForm' and 'Motor-CAD' in title and win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                found.append({'hwnd': hwnd, 'pid': pid, 'title': title})
        except:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return found

existing = find_mc()
if existing:
    print(f"  Motor-CAD 已在运行: {existing}")
    # 关闭已有实例
    for w in existing:
        print(f"  关闭 pid={w['pid']}...")
        subprocess.run(['taskkill', '/PID', str(w['pid']), '/F'], capture_output=True)
        time.sleep(3)

# 用 -Embedding 参数启动 (COM 服务器标准方式)
print(f"\n  启动 Motor-CAD (带 -Embedding 参数)...")
print(f"  exe: {mcad_exe}")

# 方法1: -Embedding 参数
proc = subprocess.Popen(
    [mcad_exe, '-Embedding'],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
)
print(f"  进程已启动, PID={proc.pid}")

# 等待窗口出现
print("  等待窗口出现...")
mc_window = None
for i in range(60):  # 最多等60秒
    time.sleep(1)
    found = find_mc()
    if found:
        mc_window = found[0]
        print(f"  [{i+1}s] 窗口出现: hwnd={mc_window['hwnd']}, pid={mc_window['pid']}")
        break
    if i % 10 == 9:
        print(f"  [{i+1}s] 等待中...")

if not mc_window:
    print("  *** 窗口未出现，尝试不带 -Embedding 启动 ***")
    proc = subprocess.Popen([mcad_exe])
    for i in range(60):
        time.sleep(1)
        found = find_mc()
        if found:
            mc_window = found[0]
            print(f"  [{i+1}s] 窗口出现: hwnd={mc_window['hwnd']}, pid={mc_window['pid']}")
            break
        if i % 10 == 9:
            print(f"  [{i+1}s] 等待中...")

if not mc_window:
    print("  *** Motor-CAD 启动失败! ***")
    sys.exit(1)

# 等待额外加载时间
print("  等待额外加载 (20s)...")
time.sleep(20)

# ============================================================
# Step 2: COM 连接 (32位 Python)
# ============================================================
print()
print("=" * 60)
print("Step 2: COM 连接测试")
print("=" * 60)

import win32com.client
import pythoncom

pythoncom.CoInitialize()
print("  COM 已初始化")

mcad = None

# 方法A: GetActiveObject (连接已运行实例)
print("\n  [A] GetActiveObject('MotorCAD.AppAutomation')...")
try:
    mcad = win32com.client.GetActiveObject("MotorCAD.AppAutomation")
    print("  *** GetActiveObject 成功! ***")
except Exception as e:
    print(f"  失败: {e}")
    
    # 方法B: Dispatch
    print("\n  [B] Dispatch('MotorCAD.AppAutomation')...")
    try:
        mcad = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print("  *** Dispatch 成功! ***")
    except Exception as e2:
        print(f"  失败: {e2}")
        
        # 方法C: DispatchEx
        print("\n  [C] DispatchEx('MotorCAD.AppAutomation')...")
        try:
            mcad = win32com.client.DispatchEx("MotorCAD.AppAutomation")
            print("  *** DispatchEx 成功! ***")
        except Exception as e3:
            print(f"  失败: {e3}")
            
            # 方法D: EnsureDispatch
            print("\n  [D] EnsureDispatch('MotorCAD.AppAutomation')...")
            try:
                import win32com.client.gencache
                mcad = win32com.client.gencache.EnsureDispatch("MotorCAD.AppAutomation")
                print("  *** EnsureDispatch 成功! ***")
            except Exception as e4:
                print(f"  失败: {e4}")
                
                # 方法E: comtypes
                print("\n  [E] comtypes.client.CreateObject...")
                try:
                    import comtypes
                    import comtypes.client
                    mcad = comtypes.client.CreateObject("MotorCAD.AppAutomation")
                    print("  *** comtypes 成功! ***")
                    # comtypes 返回的是 IUnknown，需要 QueryInterface
                    print(f"  类型: {type(mcad)}")
                except Exception as e5:
                    print(f"  失败: {e5}")

# ============================================================
# Step 3: 如果 COM 成功，运行计算
# ============================================================
if mcad:
    print()
    print("=" * 60)
    print("Step 3: COM 连接成功! 运行计算")
    print("=" * 60)
    
    # 用 win32com 包装 comtypes 对象
    if hasattr(mcad, 'QueryInterface') and not hasattr(mcad, 'GetVariable'):
        try:
            mcad = win32com.client.Dispatch(mcad)
            print("  comtypes 对象已包装为 win32com")
        except:
            pass
    
    try:
        # 获取基本信息
        print("\n  获取变量...")
        test_vars = [
            "MotorCADprocessID",
            "MotorCADexeName", 
            "CurrentExeDir_MotorLAB",
            "Shaft_Speed_[RPM]",
            "Windings_[Turns]",
        ]
        for var in test_vars:
            try:
                val = mcad.GetVariable(var)
                print(f"    {var} = {val}")
            except Exception as e:
                print(f"    {var}: 失败 ({e})")
        
        # 加载测试文件
        print(f"\n  加载文件: {mot_file}")
        try:
            mcad.LoadFromFile(mot_file)
            print("  加载成功")
            time.sleep(2)
        except Exception as e:
            print(f"  加载失败: {e}")
        
        # 运行稳态热分析
        print("\n  DoSteadyStateAnalysis...")
        start = time.time()
        mcad.DoSteadyStateAnalysis()
        elapsed = time.time() - start
        print(f"  *** 计算完成! 耗时 {elapsed:.1f}s ***")
        
        # 获取温度结果
        print("\n  温度结果:")
        results = {}
        temp_vars = [
            "T_[Winding_Average]",
            "T_[Housing_Average]",
            "T_[Magnet_Average]",
            "T_[Stator_Lam_Average]",
            "T_[Rotor_Lam_Average]",
            "T_[Ambient]",
            "T_[Coolant_Average]",
        ]
        for var in temp_vars:
            try:
                val = mcad.GetVariable(var)
                results[var] = val
                print(f"    {var} = {val} C")
            except:
                pass
        
        # 保存结果
        result_file = r"D:\MotorDesign\Scripts\com_result.txt"
        with open(result_file, "w") as f:
            f.write(f"=== Motor-CAD COM 自动计算结果 ===\n")
            f.write(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Python: {sys.executable} ({struct.calcsize('P')*8}位)\n")
            f.write(f"计算耗时: {elapsed:.1f}s\n\n")
            f.write(f"=== 温度结果 ===\n")
            for k, v in results.items():
                f.write(f"{k} = {v} C\n")
        print(f"\n  结果已保存到: {result_file}")
        
    except Exception as e:
        print(f"  计算失败: {e}")
        import traceback
        traceback.print_exc()
        
        # 保存错误
        with open(r"D:\MotorDesign\Scripts\com_result.txt", "w") as f:
            f.write(f"COM连接: 成功\n")
            f.write(f"计算失败: {e}\n")
else:
    print("\n  *** 所有 COM 方法都失败 ***")
    print("  COM 自动化不可用")
    
    # 保存失败信息
    with open(r"D:\MotorDesign\Scripts\com_result.txt", "w") as f:
        f.write(f"COM连接: 失败\n")
        f.write(f"所有方法 (GetActiveObject, Dispatch, DispatchEx, EnsureDispatch, comtypes) 均失败\n")
        f.write(f"Python: {sys.executable} ({struct.calcsize('P')*8}位)\n")

print()
print("=" * 60)
print("测试完成")
print("=" * 60)
