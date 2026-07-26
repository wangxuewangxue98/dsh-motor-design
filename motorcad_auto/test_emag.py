#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_emag.py - Motor-CAD v11.1.5 电磁分析测试

测试流程:
  1. COM 连接
  2. 加载 .mot 文件 (BPM-Therm 模块)
  3. 调用 ShowMagneticContext() 切换到电磁模式
  4. 检查 Program_Module 是否变化
  5. 尝试 DoMagneticCalculation()
  6. 尝试 DoMagneticThermalCalculation()
  7. 读取电磁分析结果 (Torque, Back_EMF, Efficiency 等)

关键发现:
  - exe 中存在 ShowMagneticContext / ShowThermalContext COM 方法
  - exe 中存在 DoMagneticCalculation / DoMagneticThermalCalculation COM 方法
  - .mot 文件虽为 BPM-Therm 模块, 但包含完整的 [Magnetics] [Magnetic_Winding] [FEA_Settings] 段
"""

import sys
import os
import time
import json
import threading

# 确保 motorcad_auto 包可导入
sys.path.insert(0, "D:/MotorDesign")

def log(msg):
    """带时间戳的日志输出"""
    elapsed = time.time() - START_TIME
    print(f"[{elapsed:7.1f}s] {msg}", flush=True)

def com_call_timeout(func, args=(), timeout=120):
    """带超时的 COM 调用 (在同一线程中执行, 超时后报告)"""
    result = [None]
    exception = [None]
    done = [False]
    
    def wrapper():
        try:
            result[0] = func(*args)
            done[0] = True
        except Exception as e:
            exception[0] = e
            done[0] = True
    
    t = threading.Thread(target=wrapper)
    t.daemon = True
    t.start()
    t.join(timeout=timeout)
    
    if not done[0]:
        return None, TimeoutError(f"COM 调用超时 ({timeout}s)")
    if exception[0]:
        return None, exception[0]
    return result[0], None

def main():
    global START_TIME
    START_TIME = time.time()
    
    MOT_FILE = r"D:\MotorDesign\Templates\160kw_assm_2222.mot"
    
    log("=" * 60)
    log("Motor-CAD v11.1.5 电磁分析测试")
    log("=" * 60)
    
    # Step 1: COM 初始化
    log("Step 1: 初始化 COM...")
    import pythoncom
    pythoncom.CoInitialize()
    import win32com.client
    
    # Step 2: Dispatch 连接
    log("Step 2: Dispatch 连接 Motor-CAD (需 60-90 秒)...")
    t0 = time.time()
    mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
    t_connect = time.time() - t0
    log(f"  连接成功! 耗时 {t_connect:.1f}s")
    time.sleep(10)  # 等待初始化
    
    try:
        # Step 3: 加载 .mot 文件
        log("Step 3: 加载 .mot 文件...")
        log(f"  文件: {MOT_FILE}")
        ret = mc.LoadFromFile(MOT_FILE)
        log(f"  LoadFromFile 返回: {ret}")
        time.sleep(5)
        
        # Step 4: 读取初始状态
        log("Step 4: 读取初始状态...")
        ver = mc.GetVariable("Program_Version")
        log(f"  Program_Version: {ver}")
        
        mod = mc.GetVariable("Program_Module")
        log(f"  Program_Module (初始): {mod}")
        
        poles = mc.GetVariable("Pole_Number")
        log(f"  Pole_Number: {poles}")
        
        slots = mc.GetVariable("Slot_Number")
        log(f"  Slot_Number: {slots}")
        
        # Step 5: 调用 ShowMagneticContext() 切换到电磁模式
        log("Step 5: 调用 ShowMagneticContext()...")
        try:
            mc.ShowMagneticContext()
            log("  ShowMagneticContext 调用成功!")
            time.sleep(5)
            
            # 检查模块是否变化
            mod2 = mc.GetVariable("Program_Module")
            log(f"  Program_Module (切换后): {mod2}")
        except Exception as e:
            log(f"  ShowMagneticContext 失败: {e}")
        
        # Step 6: 设置电磁分析参数
        log("Step 6: 设置电磁分析参数...")
        try:
            mc.SetVariable("DCBusVoltage", 540)
            log("  DCBusVoltage = 540")
        except Exception as e:
            log(f"  SetVariable DCBusVoltage 失败: {e}")
        
        try:
            mc.SetVariable("PeakCurrent", 200)
            log("  PeakCurrent = 200")
        except Exception as e:
            log(f"  SetVariable PeakCurrent 失败: {e}")
        
        # 尝试设置转速 (用 .mot 文件中的变量名)
        for speed_var in ["Shaft_Speed", "Current_Shaft_Speed_RPM"]:
            try:
                mc.SetVariable(speed_var, 130)
                log(f"  {speed_var} = 130")
                break
            except Exception as e:
                log(f"  SetVariable {speed_var} 失败: {e}")
        
        # Step 7: 尝试 DoMagneticCalculation (直接在主线程调用, COM STA 要求)
        log("Step 7: 调用 DoMagneticCalculation() (直接调用, 可能需要 30-180 秒)...")
        t0 = time.time()
        try:
            ret_mag = mc.DoMagneticCalculation()
            t_mag = time.time() - t0
            log(f"  DoMagneticCalculation 返回: {ret_mag} (耗时 {t_mag:.1f}s)")
            err = None
        except Exception as e:
            t_mag = time.time() - t0
            log(f"  DoMagneticCalculation 异常: {e} (耗时 {t_mag:.1f}s)")
            ret_mag = None
            err = e
        
        if ret_mag is not None:
            if ret_mag == 0 or (isinstance(ret_mag, tuple) and ret_mag[0] >= 0):
                log("  *** 电磁分析成功! ***")
                
                # 读取电磁结果
                log("Step 8: 读取电磁分析结果...")
                emag_vars = [
                    "Torque", "Average_Torque", "Shaft_Torque",
                    "Back_EMF", "Back_EMF_RMS",
                    "Efficiency", "Overall_Efficiency",
                    "Power", "Output_Power", "Input_Power",
                    "Copper_Loss", "Iron_Loss", "Total_Loss",
                    "Power_Factor", "Phase_Current", "Phase_Voltage",
                    "MagneticFlux", "Airgap_Flux_Density",
                ]
                results = {}
                for var in emag_vars:
                    try:
                        val = mc.GetVariable(var)
                        if isinstance(val, tuple) and val[0] >= 0:
                            results[var] = val[1]
                            log(f"  {var} = {val[1]}")
                        elif isinstance(val, (int, float, str)):
                            results[var] = val
                            log(f"  {var} = {val}")
                    except Exception:
                        pass
                
                if not results:
                    log("  (未能读取任何电磁结果变量)")
            else:
                log(f"  电磁分析失败 (返回值非0)")
        
        # Step 8: 尝试 DoMagneticThermalCalculation (耦合计算)
        log("Step 8: 调用 DoMagneticThermalCalculation() (耦合计算)...")
        t0 = time.time()
        try:
            ret_couple = mc.DoMagneticThermalCalculation()
            t_couple = time.time() - t0
            log(f"  DoMagneticThermalCalculation 返回: {ret_couple} (耗时 {t_couple:.1f}s)")
        except Exception as e:
            t_couple = time.time() - t0
            log(f"  DoMagneticThermalCalculation 异常: {e} (耗时 {t_couple:.1f}s)")
            ret_couple = None
        
        if ret_couple is not None and (ret_couple == 0 or (isinstance(ret_couple, tuple) and ret_couple[0] >= 0)):
                log("  *** 耦合计算成功! ***")
                
                # 读取耦合结果
                log("Step 9: 读取耦合分析结果...")
                couple_vars = [
                    "Average_Torque", "Overall_Efficiency", "Output_Power",
                    "T_[Winding_Average]", "T_[Winding_Max]",
                    "Copper_Loss", "Iron_Loss", "Total_Loss",
                ]
                for var in couple_vars:
                    try:
                        val = mc.GetVariable(var)
                        if isinstance(val, tuple) and val[0] >= 0:
                            log(f"  {var} = {val[1]}")
                        elif isinstance(val, (int, float, str)):
                            log(f"  {var} = {val}")
                    except Exception:
                        pass
        
        # Step 10: 切回热模式做对比
        log("Step 10: 切回 ShowThermalContext()...")
        try:
            mc.ShowThermalContext()
            log("  ShowThermalContext 调用成功!")
            time.sleep(3)
            mod3 = mc.GetVariable("Program_Module")
            log(f"  Program_Module (热模式): {mod3}")
        except Exception as e:
            log(f"  ShowThermalContext 失败: {e}")
        
        # 保存结果
        log("Step 12: 保存结果文件...")
        result_data = {
            "test": "emag_analysis",
            "mot_file": MOT_FILE,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "connect_time_s": round(t_connect, 1),
            "initial_module": str(mod),
            "after_mag_context_module": str(mod2) if 'mod2' in dir() else "N/A",
            "magnetic_calc_result": str(ret_mag) if 'ret_mag' in dir() else "N/A",
            "magnetic_calc_time_s": round(t_mag, 1) if 't_mag' in dir() else 0,
            "coupled_calc_result": str(ret_couple) if 'ret_couple' in dir() else "N/A",
            "coupled_calc_time_s": round(t_couple, 1) if 't_couple' in dir() else 0,
        }
        
        result_path = r"D:\MotorDesign\Reports\emag_test_result.json"
        os.makedirs(os.path.dirname(result_path), exist_ok=True)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result_data, f, indent=2, ensure_ascii=False)
        log(f"  结果已保存: {result_path}")
        
    except Exception as e:
        log(f"异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 退出 Motor-CAD
        log("退出 Motor-CAD...")
        try:
            mc.Quit()
        except:
            pass
        pythoncom.CoUninitialize()
    
    total = time.time() - START_TIME
    log(f"\n{'=' * 60}")
    log(f"测试完成! 总耗时: {total:.1f}s ({total/60:.1f}分钟)")
    log(f"{'=' * 60}")

if __name__ == "__main__":
    main()
