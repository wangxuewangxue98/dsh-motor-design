#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_emag_results.py - 读取 Motor-CAD v11.1.5 电磁分析结果变量

已知:
  - ShowMagneticContext() 可切换到电磁模式
  - DoMagneticCalculation() 返回 0 (成功, 约200秒)
  - DoMagneticThermalCalculation() 返回 0 (成功, 约195秒)
  - 之前的变量名 (Torque, Efficiency 等) 读取失败
  
从 exe 中发现的正确变量名:
  Torque: Torque_[pu], Torque_[Required], Shaft_Torque_[pu], Shaft_Torque_[Required]
  Loss:   Loss_[Total], Loss_[Stator_Copper], Loss_[Rotor_Copper], Loss_[Magnet], ...
  Speed:  Speed_[RPM]
  Iron:   Iron_Loss_[Back_Iron], Iron_Loss_[Tooth], Iron_Loss_[Tooth_Tip]
  Eff:    Efficiency_[Front], Efficiency_[Rear]
"""

import sys
import os
import time
import json

sys.path.insert(0, "D:/MotorDesign")

def log(msg):
    elapsed = time.time() - START_TIME
    print(f"[{elapsed:7.1f}s] {msg}", flush=True)

def safe_getvar(mc, name, timeout_hint=5):
    """安全读取变量, 超时则跳过"""
    try:
        val = mc.GetVariable(name)
        if isinstance(val, tuple):
            if val[0] >= 0:
                return val[1]
            else:
                return f"<code={val[0]}>"
        return val
    except Exception as e:
        return f"<err: {e}>"

def main():
    global START_TIME
    START_TIME = time.time()
    
    MOT_FILE = r"D:\MotorDesign\Templates\160kw_assm_2222.mot"
    
    log("=" * 60)
    log("Motor-CAD v11.1.5 电磁分析结果变量测试")
    log("=" * 60)
    
    import pythoncom
    pythoncom.CoInitialize()
    import win32com.client
    
    log("Dispatch 连接 Motor-CAD...")
    t0 = time.time()
    mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
    log(f"连接成功! 耗时 {time.time()-t0:.1f}s")
    time.sleep(10)
    
    try:
        # 加载文件
        log(f"加载: {MOT_FILE}")
        mc.LoadFromFile(MOT_FILE)
        time.sleep(5)
        
        # 切换到电磁模式
        log("ShowMagneticContext()...")
        mc.ShowMagneticContext()
        time.sleep(5)
        
        # 设置参数
        log("设置参数: DCBusVoltage=540, PeakCurrent=200, Shaft_Speed=130")
        mc.SetVariable("DCBusVoltage", 540)
        mc.SetVariable("PeakCurrent", 200)
        mc.SetVariable("Shaft_Speed", 130)
        
        # 运行电磁分析
        log("DoMagneticCalculation() (约200秒)...")
        t0 = time.time()
        ret = mc.DoMagneticCalculation()
        t_mag = time.time() - t0
        log(f"返回: {ret}, 耗时: {t_mag:.1f}s")
        
        if ret != 0 and not (isinstance(ret, tuple) and ret[0] >= 0):
            log("电磁分析失败, 跳过结果读取")
            return
        
        log("*** 电磁分析成功! 读取结果变量... ***")
        log("")
        
        # === 测试所有发现的变量名 ===
        all_results = {}
        
        # 1. 转矩相关
        log("--- 转矩变量 ---")
        torque_vars = [
            "Torque_[pu]", "Torque_[PU]", "Torque_[Required]",
            "Shaft_Torque_[pu]", "Shaft_Torque_[Required]",
            "Torque", "Average_Torque", "Shaft_Torque",
            "TorqueRippleMsVw", "CoggingTorque",
        ]
        for v in torque_vars:
            val = safe_getvar(mc, v)
            if val is not None and not str(val).startswith("<"):
                log(f"  {v} = {val}")
                all_results[v] = val
            else:
                log(f"  {v} = {val}")
        
        # 2. 损耗相关
        log("--- 损耗变量 ---")
        loss_vars = [
            "Loss_[Total]", "Loss_[Stator_Copper]", "Loss_[Rotor_Copper]",
            "Loss_[Stator_Back_Iron]", "Loss_[Stator_Tooth]", "Loss_[Stator_Tooth_Tip]",
            "Loss_[Rotor_Back_Iron]", "Loss_[Rotor_Tooth]",
            "Loss_[Magnet]", "Loss_[Windage]",
            "Loss_[Friction_-_F_Bearing]", "Loss_[Friction_-_R_Bearing]",
            "Loss_[Stray_Load]", "Loss_[Brush_Friction]",
            "Iron_Loss_[Back_Iron]", "Iron_Loss_[Tooth]", "Iron_Loss_[Tooth_Tip]",
            "Iron_Loss_[Embedded_Magnet_Pole]",
            "TotalLoss", "Copper_Loss", "Iron_Loss",
        ]
        for v in loss_vars:
            val = safe_getvar(mc, v)
            if val is not None and not str(val).startswith("<"):
                log(f"  {v} = {val}")
                all_results[v] = val
            else:
                log(f"  {v} = {val}")
        
        # 3. 功率相关
        log("--- 功率变量 ---")
        power_vars = [
            "OutputPower", "InputPower", "ShaftPower",
            "ElectromagneticPower", "TotalLoss",
            "Power_Stator_Copper_Loss", "Power_Rotor_Iron_Stray_Load_Loss",
        ]
        for v in power_vars:
            val = safe_getvar(mc, v)
            if val is not None and not str(val).startswith("<"):
                log(f"  {v} = {val}")
                all_results[v] = val
            else:
                log(f"  {v} = {val}")
        
        # 4. 效率相关
        log("--- 效率变量 ---")
        eff_vars = [
            "Efficiency_[Front]", "Efficiency_[Rear]",
            "Efficiency", "Overall_Efficiency",
        ]
        for v in eff_vars:
            val = safe_getvar(mc, v)
            if val is not None and not str(val).startswith("<"):
                log(f"  {v} = {val}")
                all_results[v] = val
            else:
                log(f"  {v} = {val}")
        
        # 5. 速度和基本参数
        log("--- 速度和基本参数 ---")
        speed_vars = [
            "Speed_[RPM]", "Shaft_Speed", "Current_Shaft_Speed_RPM",
            "DCBusVoltage", "PeakCurrent",
            "Pole_Number", "Slot_Number",
            "BackEMFConstantKe",
        ]
        for v in speed_vars:
            val = safe_getvar(mc, v)
            if val is not None and not str(val).startswith("<"):
                log(f"  {v} = {val}")
                all_results[v] = val
            else:
                log(f"  {v} = {val}")
        
        # 6. 热结果 (验证耦合计算是否更新了温度)
        log("--- 热结果变量 (DoMagneticCalculation 后) ---")
        thermal_vars = [
            "T_[Winding_Average]", "T_[Winding_Max]",
        ]
        for v in thermal_vars:
            val = safe_getvar(mc, v)
            log(f"  {v} = {val}")
            if val is not None and not str(val).startswith("<"):
                all_results[v] = val
        
        # 总结
        log("")
        log("=" * 60)
        log(f"成功读取 {len(all_results)} 个变量")
        log("=" * 60)
        
        # 保存结果
        result_path = r"D:\MotorDesign\Reports\emag_variables_result.json"
        os.makedirs(os.path.dirname(result_path), exist_ok=True)
        output = {
            "test": "emag_result_variables",
            "mot_file": MOT_FILE,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "magnetic_calc_return": str(ret),
            "magnetic_calc_time_s": round(t_mag, 1),
            "results": all_results,
        }
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        log(f"结果已保存: {result_path}")
        
    except Exception as e:
        log(f"异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        log("退出 Motor-CAD...")
        try:
            mc.Quit()
        except:
            pass
        pythoncom.CoUninitialize()
    
    total = time.time() - START_TIME
    log(f"\n总耗时: {total:.1f}s ({total/60:.1f}分钟)")

if __name__ == "__main__":
    main()
