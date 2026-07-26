# -*- coding: utf-8 -*-
"""
Motor-CAD v11.1.5 COM 自动化测试 v3 - 精简版
直接运行电磁+热分析，每个 COM 调用有独立超时
"""
import threading
import sys
import time
import json

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

def com_call_with_timeout(mc, method_name, args=(), timeout=60):
    """带超时的 COM 调用"""
    result = {'done': False, 'value': None, 'error': None}
    
    def call():
        try:
            method = getattr(mc, method_name)
            result['value'] = method(*args)
            result['done'] = True
        except Exception as e:
            result['error'] = str(e)
            result['done'] = True
    
    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout=timeout)
    
    if not result['done']:
        return None, f"超时({timeout}s)"
    return result['value'], result['error']

def main():
    import pythoncom
    pythoncom.CoInitialize()
    
    try:
        import win32com.client
        
        print(f"[{time.strftime('%H:%M:%S')}] 连接 COM...")
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{time.strftime('%H:%M:%S')}] Dispatch 成功")
        time.sleep(3)
        
        # 加载文件
        print(f"[{time.strftime('%H:%M:%S')}] 加载 .mot 文件...")
        ret, err = com_call_with_timeout(mc, "LoadFromFile", (MOT_FILE,), timeout=30)
        print(f"[{time.strftime('%H:%M:%S')}] LoadFromFile: ret={ret}, err={err}")
        time.sleep(5)
        
        # 读取版本
        ret, err = com_call_with_timeout(mc, "GetVariable", ("Program_Version",), timeout=10)
        print(f"[{time.strftime('%H:%M:%S')}] Version: {ret}")
        
        # 读取几个关键参数
        for vname in ["Pole_Number", "Slot_Number", "DCBusVoltage", "PeakCurrent", "OperatingSpeed"]:
            ret, err = com_call_with_timeout(mc, "GetVariable", (vname,), timeout=10)
            if ret and isinstance(ret, tuple) and ret[0] >= 0:
                print(f"  {vname} = {ret[1]}")
            elif ret is not None:
                print(f"  {vname} = {ret}")
        
        # 电磁分析
        print(f"\n[{time.strftime('%H:%M:%S')}] === 电磁分析 ===")
        ret, err = com_call_with_timeout(mc, "DoMagneticCalculation", timeout=300)
        print(f"[{time.strftime('%H:%M:%S')}] DoMagneticCalculation: ret={ret}, err={err}")
        
        emag_results = {}
        if ret is not None:
            success = False
            if isinstance(ret, tuple) and ret[0] >= 0:
                success = True
            elif ret == 0 or ret is None:
                success = True
            
            if success:
                print("  电磁分析完成! 读取结果...")
                for vname in ["TorqueAverage", "TorqueRipple", "EMTorque", "Efficiency",
                              "TotalLoss", "IronLoss", "CopperLoss", "PowerOutput",
                              "BackEMFRMS", "PhaseResistance", "SaliencyRatio"]:
                    ret, err = com_call_with_timeout(mc, "GetVariable", (vname,), timeout=10)
                    if ret and isinstance(ret, tuple) and ret[0] >= 0:
                        emag_results[vname] = ret[1]
                        print(f"  {vname} = {ret[1]}")
                    elif ret is not None and not isinstance(ret, tuple):
                        emag_results[vname] = ret
                        print(f"  {vname} = {ret}")
        
        # 热分析
        print(f"\n[{time.strftime('%H:%M:%S')}] === 热分析 ===")
        ret, err = com_call_with_timeout(mc, "DoSteadyStateAnalysis", timeout=120)
        print(f"[{time.strftime('%H:%M:%S')}] DoSteadyStateAnalysis: ret={ret}, err={err}")
        
        thermal_results = {}
        if ret is not None:
            success = False
            if isinstance(ret, tuple) and ret[0] >= 0:
                success = True
            elif ret == 0 or ret is None:
                success = True
            
            if success:
                print("  热分析完成! 读取结果...")
                for vname in ["T_[Winding_Average]", "T_[Winding_Max]",
                              "T_[Housing_Average]", "T_[Housing_Max]",
                              "T_[Magnet_Average]", "T_[Magnet_Max]",
                              "T_[Rotor_Average]", "T_[Stator_Average]",
                              "T_[Coolant_In]", "T_[Coolant_Out]",
                              "MaxTemperature", "TotalLoss"]:
                    ret, err = com_call_with_timeout(mc, "GetVariable", (vname,), timeout=10)
                    if ret and isinstance(ret, tuple) and ret[0] >= 0:
                        thermal_results[vname] = ret[1]
                        if isinstance(ret[1], (int, float)):
                            print(f"  {vname} = {ret[1]:.2f}")
                        else:
                            print(f"  {vname} = {ret[1]}")
                    elif ret is not None and not isinstance(ret, tuple):
                        thermal_results[vname] = ret
                        print(f"  {vname} = {ret}")
        
        # 保存结果
        output = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mot_file': MOT_FILE,
            'emag_results': emag_results,
            'thermal_results': thermal_results,
        }
        
        with open(r"D:\MotorDesign\Scripts\com_test_v3_results.json", 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"\n{'='*60}")
        print(f"电磁结果: {len(emag_results)} 个")
        print(f"热结果: {len(thermal_results)} 个")
        if thermal_results:
            print("\n温度结果:")
            for k, v in thermal_results.items():
                if isinstance(v, (int, float)):
                    print(f"  {k}: {v:.2f} °C")
        print(f"\n结果已保存到 com_test_v3_results.json")
        
    finally:
        pythoncom.CoUninitialize()

result = {'done': False}
def thread_func():
    main()
    result['done'] = True

t = threading.Thread(target=thread_func, daemon=True)
t.start()
t.join(timeout=600)

if t.is_alive():
    print("\n*** 超时 (10分钟) ***")
elif result['done']:
    print(f"\n[{time.strftime('%H:%M:%S')}] 测试完成")
