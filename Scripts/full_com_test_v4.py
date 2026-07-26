# -*- coding: utf-8 -*-
"""
Motor-CAD v11.1.5 COM 自动化测试 v4 - 单线程干净版
所有 COM 调用都在同一线程中执行
"""
import sys
import time
import json
import threading

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

def main():
    import pythoncom
    pythoncom.CoInitialize()
    
    try:
        import win32com.client
        
        ts = lambda: time.strftime('%H:%M:%S')
        
        # 1. 连接
        print(f"[{ts()}] Dispatch 连接...")
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{ts()}] 连接成功")
        time.sleep(5)
        
        # 2. 加载文件
        print(f"[{ts()}] 加载 .mot 文件...")
        ret = mc.LoadFromFile(MOT_FILE)
        print(f"[{ts()}] LoadFromFile = {ret}")
        time.sleep(5)
        
        # 3. 读取关键参数
        print(f"\n[{ts()}] 读取参数:")
        params = {}
        for vname in ["Program_Version", "Program_Module", "Pole_Number", 
                       "Slot_Number", "DCBusVoltage", "PeakCurrent",
                       "OperatingSpeed", "StackLength", "StatorOuterDiameter"]:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and len(val) == 2:
                    if val[0] >= 0:
                        params[vname] = val[1]
                        print(f"  {vname} = {val[1]}")
                else:
                    params[vname] = val
                    print(f"  {vname} = {val}")
            except Exception as e:
                print(f"  {vname} 错误: {e}")
        
        # 4. 电磁分析
        print(f"\n[{ts()}] === 电磁分析 ===")
        try:
            t0 = time.time()
            ret = mc.DoMagneticCalculation()
            elapsed = time.time() - t0
            print(f"[{ts()}] DoMagneticCalculation = {ret} ({elapsed:.1f}s)")
            
            emag = {}
            if isinstance(ret, tuple):
                success = ret[0] >= 0
            elif ret == 0 or ret is None:
                success = True
            else:
                success = False
                print(f"  电磁分析返回: {ret}")
            
            if success or (isinstance(ret, int) and ret != -1):
                print(f"  读取电磁结果...")
                for vname in ["TorqueAverage", "EMTorque", "Efficiency",
                              "TotalLoss", "IronLoss", "CopperLoss",
                              "PowerOutput", "BackEMFRMS"]:
                    try:
                        val = mc.GetVariable(vname)
                        if isinstance(val, tuple) and val[0] >= 0:
                            emag[vname] = val[1]
                            print(f"  {vname} = {val[1]}")
                        elif not isinstance(val, tuple):
                            emag[vname] = val
                            print(f"  {vname} = {val}")
                    except:
                        pass
        except Exception as e:
            print(f"  电磁分析异常: {e}")
            emag = {}
        
        # 5. 热分析
        print(f"\n[{ts()}] === 热分析 ===")
        try:
            t0 = time.time()
            ret = mc.DoSteadyStateAnalysis()
            elapsed = time.time() - t0
            print(f"[{ts()}] DoSteadyStateAnalysis = {ret} ({elapsed:.1f}s)")
            
            thermal = {}
            if isinstance(ret, tuple):
                success = ret[0] >= 0
            elif ret == 0 or ret is None:
                success = True
            else:
                success = False
            
            if success:
                print(f"  读取热结果...")
                for vname in ["T_[Winding_Average]", "T_[Winding_Max]",
                              "T_[Housing_Average]", "T_[Housing_Max]",
                              "T_[Magnet_Average]", "T_[Magnet_Max]",
                              "T_[Rotor_Average]", "T_[Stator_Average]",
                              "T_[Coolant_In]", "T_[Coolant_Out]",
                              "MaxTemperature"]:
                    try:
                        val = mc.GetVariable(vname)
                        if isinstance(val, tuple) and val[0] >= 0:
                            thermal[vname] = val[1]
                            if isinstance(val[1], (int, float)):
                                print(f"  {vname} = {val[1]:.2f} °C")
                            else:
                                print(f"  {vname} = {val[1]}")
                        elif not isinstance(val, tuple):
                            thermal[vname] = val
                            print(f"  {vname} = {val}")
                    except:
                        pass
        except Exception as e:
            print(f"  热分析异常: {e}")
            thermal = {}
        
        # 6. 汇总
        print(f"\n{'='*60}")
        print(f"参数: {len(params)} 个")
        print(f"电磁结果: {len(emag)} 个")
        print(f"热结果: {len(thermal)} 个")
        
        output = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'params': params,
            'emag': emag,
            'thermal': thermal,
        }
        with open(r"D:\MotorDesign\Scripts\com_test_v4_results.json", 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        print(f"结果已保存")
        
    finally:
        pythoncom.CoUninitialize()

# 线程运行 (整体超时)
result = {'done': False}
def thread_func():
    main()
    result['done'] = True

t = threading.Thread(target=thread_func, daemon=True)
t.start()
t.join(timeout=300)  # 5分钟

if t.is_alive():
    print("\n*** 超时 (5分钟) ***")
elif result['done']:
    print(f"\n[{time.strftime('%H:%M:%S')}] 完成")
