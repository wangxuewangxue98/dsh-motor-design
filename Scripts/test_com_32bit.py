#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD COM 自动化测试 (32位 Python + comtypes)
使用 32位 Python 连接 32位 Motor-CAD COM 服务器
"""
import sys
import os
import time
import struct

print(f"Python 位数: {struct.calcsize('P') * 8}")
print(f"Python 路径: {sys.executable}")

import comtypes
import comtypes.client

print(f"comtypes 版本: {comtypes.__version__}")
print()

# 初始化 COM
comtypes.CoInitialize()
print("COM 已初始化")

# ============================================================
# 尝试创建 Motor-CAD COM 对象
# ============================================================
print("\n=== 尝试连接 Motor-CAD COM ===")
print("创建 MotorCAD.AppAutomation 对象...")

mcad = None
error_msg = ""

try:
    # 方法1: 使用 CreateObject (动态dispatch)
    mcad = comtypes.client.CreateObject("MotorCAD.AppAutomation")
    print("COM 连接成功!")
except Exception as e:
    error_msg = str(e)
    print(f"CreateObject 失败: {e}")

    # 方法2: 尝试小写 ProgID
    try:
        print("\n尝试小写 ProgID: motorcad.appautomation...")
        mcad = comtypes.client.CreateObject("motorcad.appautomation")
        print("COM 连接成功 (小写 ProgID)!")
    except Exception as e2:
        print(f"小写 ProgID 也失败: {e2}")

        # 方法3: 尝试从 CLSID 创建
        try:
            print("\n尝试从 CLSID 创建...")
            import comtypes.GUID
            clsid = comtypes.GUID("{4ACD06EB-5486-42AF-91DB-FB752E64C465}")
            mcad = comtypes.client.CreateObject(clsid)
            print("COM 连接成功 (CLSID)!")
        except Exception as e3:
            print(f"CLSID 也失败: {e3}")

if mcad is None:
    print(f"\n*** COM 连接完全失败! ***")
    print(f"最后错误: {error_msg}")

    # 保存结果
    with open(r"D:\MotorDesign\Scripts\com32_results.txt", "w", encoding="utf-8") as f:
        f.write(f"COM连接: 失败\n")
        f.write(f"错误: {error_msg}\n")
        f.write(f"Python位数: {struct.calcsize('P') * 8}\n")

    comtypes.CoUninitialize()
    sys.exit(1)

# ============================================================
# COM 连接成功，获取基本信息
# ============================================================
print("\n=== 获取基本信息 ===")

def try_get_variable(mcad, var_name):
    """尝试获取变量值"""
    try:
        # comtypes 动态 dispatch
        result = mcad.GetVariable(var_name)
        if isinstance(result, tuple):
            return result[1] if len(result) > 1 else result[0]
        return result
    except Exception as e:
        return f"ERROR: {e}"

def try_invoke(mcad, method_name, *args):
    """尝试调用方法"""
    try:
        method = getattr(mcad, method_name)
        return method(*args)
    except Exception as e:
        return f"ERROR: {e}"

# 获取 Motor-CAD 内部进程 ID
pid = try_get_variable(mcad, "MotorCADprocessID")
print(f"MotorCADprocessID = {pid}")

# 获取可执行文件名
exe_name = try_get_variable(mcad, "MotorCADexeName")
print(f"MotorCADexeName = {exe_name}")

# 获取 MotorLAB 目录
exe_dir = try_get_variable(mcad, "CurrentExeDir_MotorLAB")
print(f"CurrentExeDir_MotorLAB = {exe_dir}")

# 获取转速
speed = try_get_variable(mcad, "Shaft_Speed_[RPM]")
print(f"Shaft_Speed_[RPM] = {speed}")

# 获取绕组匝数
turns = try_get_variable(mcad, "Windings_[Turns]")
print(f"Windings_[Turns] = {turns}")

# ============================================================
# 尝试运行稳态热分析
# ============================================================
print("\n=== 尝试 DoSteadyStateAnalysis ===")
start_time = time.time()

try:
    mcad.DoSteadyStateAnalysis()
    elapsed = time.time() - start_time
    print(f"DoSteadyStateAnalysis 完成! (耗时 {elapsed:.1f}s)")

    # 获取温度结果
    print("\n--- 温度结果 ---")

    temp_vars = [
        "T_[Winding_Average]",
        "T_[Winding_Active_Average]",
        "T_[Housing_Average]",
        "T_[Magnet_Average]",
        "T_[Stator_Lam_Average]",
        "T_[Rotor_Lam_Average]",
        "T_[Shaft_Average]",
        "T_[Bearings_Average]",
        "T_[End_Space_Average]",
        "T_[Coolant_Average]",
    ]

    results = {}
    for var in temp_vars:
        val = try_get_variable(mcad, var)
        print(f"  {var} = {val}")
        if not isinstance(val, str) or "ERROR" not in str(val):
            results[var] = val

    # 保存结果到文件
    results_path = r"D:\MotorDesign\Scripts\com32_results.txt"
    with open(results_path, "w", encoding="utf-8") as f:
        f.write(f"COM连接: 成功\n")
        f.write(f"Python位数: {struct.calcsize('P') * 8}\n")
        f.write(f"MotorCADprocessID: {pid}\n")
        f.write(f"MotorCADexeName: {exe_name}\n")
        f.write(f"CurrentExeDir_MotorLAB: {exe_dir}\n")
        f.write(f"Shaft_Speed_[RPM]: {speed}\n")
        f.write(f"\nDoSteadyStateAnalysis: 成功 (耗时 {elapsed:.1f}s)\n")
        f.write(f"\n--- 温度结果 ---\n")
        for var, val in results.items():
            f.write(f"{var} = {val}\n")

    print(f"\n结果已保存到: {results_path}")

except Exception as e:
    elapsed = time.time() - start_time
    print(f"DoSteadyStateAnalysis 失败 (耗时 {elapsed:.1f}s): {e}")

    # 尝试其他方法
    print("\n尝试其他方法...")

    # 尝试 DoStaticLoadCalculation
    try:
        print("  DoStaticLoadCalculation...")
        mcad.DoStaticLoadCalculation()
        print("  成功!")
    except Exception as e2:
        print(f"  失败: {e2}")

    # 尝试 DoEMagAnalysis
    try:
        print("  DoEMagAnalysis...")
        mcad.DoEMagAnalysis()
        print("  成功!")
    except Exception as e2:
        print(f"  失败: {e2}")

    # 保存错误信息
    with open(r"D:\MotorDesign\Scripts\com32_results.txt", "w", encoding="utf-8") as f:
        f.write(f"COM连接: 成功\n")
        f.write(f"Python位数: {struct.calcsize('P') * 8}\n")
        f.write(f"MotorCADprocessID: {pid}\n")
        f.write(f"DoSteadyStateAnalysis: 失败\n")
        f.write(f"错误: {e}\n")

# ============================================================
# 尝试获取更多结果数据
# ============================================================
print("\n=== 获取更多结果 ===")

# 尝试获取转矩
torque = try_get_variable(mcad, "Shaft_Torque_[Nm]")
print(f"Shaft_Torque_[Nm] = {torque}")

# 尝试获取功率
power = try_get_variable(mcad, "Shaft_Power_[kW]")
print(f"Shaft_Power_[kW] = {power}")

# 尝试获取效率
efficiency = try_get_variable(mcad, "Efficiency_[percent]")
print(f"Efficiency_[percent] = {efficiency}")

# 尝试获取电流
current = try_get_variable(mcad, "Phase_RMS_Current_[A]")
print(f"Phase_RMS_Current_[A] = {current}")

# 关闭 COM
print("\n=== 关闭 COM ===")
try:
    mcad.Quit()
    print("Quit 成功")
except:
    print("Quit 失败 (忽略)")

comtypes.CoUninitialize()
print("\n=== 测试完成 ===")
