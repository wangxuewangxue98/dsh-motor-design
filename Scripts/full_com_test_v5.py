# -*- coding: utf-8 -*-
"""
Motor-CAD v11.1.5 COM 自动化测试 v5 - 最终版
只用已知可用的变量，直接运行电磁+热分析
"""
import sys, time, json, threading

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

def main():
    import pythoncom
    pythoncom.CoInitialize()
    try:
        import win32com.client
        ts = lambda: time.strftime('%H:%M:%S')
        
        print(f"[{ts()}] 连接 COM...")
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{ts()}] 连接成功, 等待初始化...")
        time.sleep(5)
        
        print(f"[{ts()}] 加载文件...")
        mc.LoadFromFile(MOT_FILE)
        time.sleep(5)
        print(f"[{ts()}] 文件已加载")
        
        # 只读已知可用参数
        params = {}
        for vname in ["Program_Version", "Pole_Number", "Slot_Number", 
                       "DCBusVoltage", "PeakCurrent", "WindingConnection"]:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and val[0] >= 0:
                    params[vname] = val[1]
                    print(f"  {vname} = {val[1]}")
            except: pass
        
        # 设置参数 (修改转速和电流)
        print(f"\n[{ts()}] 设置参数...")
        try:
            mc.SetVariable("DCBusVoltage", 350)
            mc.SetVariable("PeakCurrent", 100)
            print("  DCBusVoltage=350, PeakCurrent=100")
        except Exception as e:
            print(f"  设置失败: {e}")
        
        # 电磁分析
        print(f"\n[{ts()}] 电磁分析...")
        emag = {}
        try:
            t0 = time.time()
            ret = mc.DoMagneticCalculation()
            elapsed = time.time() - t0
            print(f"  返回 {ret} ({elapsed:.1f}s)")
            
            # 不管返回值，都尝试读取结果
            for vname in ["TorqueAverage", "EMTorque", "Efficiency",
                          "TotalLoss", "IronLoss", "CopperLoss", "PowerOutput"]:
                try:
                    val = mc.GetVariable(vname)
                    if isinstance(val, tuple) and val[0] >= 0:
                        emag[vname] = val[1]
                        print(f"  {vname} = {val[1]}")
                except: pass
        except Exception as e:
            print(f"  异常: {e}")
        
        # 热分析
        print(f"\n[{ts()}] 热分析...")
        thermal = {}
        try:
            t0 = time.time()
            ret = mc.DoSteadyStateAnalysis()
            elapsed = time.time() - t0
            print(f"  返回 {ret} ({elapsed:.1f}s)")
            
            for vname in ["T_[Winding_Average]", "T_[Winding_Max]",
                          "T_[Housing_Average]", "T_[Housing_Max]",
                          "T_[Magnet_Average]", "T_[Magnet_Max]",
                          "T_[Rotor_Average]", "T_[Stator_Average]",
                          "T_[Coolant_In]", "T_[Coolant_Out]", "MaxTemperature"]:
                try:
                    val = mc.GetVariable(vname)
                    if isinstance(val, tuple) and val[0] >= 0:
                        thermal[vname] = val[1]
                        if isinstance(val[1], (int, float)):
                            print(f"  {vname} = {val[1]:.2f}")
                        else:
                            print(f"  {vname} = {val[1]}")
                except: pass
        except Exception as e:
            print(f"  异常: {e}")
        
        # 保存结果文件 (带结果)
        try:
            mc.SaveToFile(MOT_FILE.replace(".mot", "_result.mot"))
            print(f"\n[{ts()}] 结果已保存到 _result.mot")
        except: pass
        
        # 汇总
        print(f"\n{'='*60}")
        print(f"参数: {len(params)}, 电磁: {len(emag)}, 热: {len(thermal)}")
        if thermal:
            print("\n温度结果:")
            for k, v in thermal.items():
                if isinstance(v, (int, float)):
                    print(f"  {k}: {v:.2f} °C")
        
        output = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'params': params, 'emag': emag, 'thermal': thermal}
        with open(r"D:\MotorDesign\Scripts\com_final_results.json", 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        print("结果已保存到 com_final_results.json")
        
    finally:
        pythoncom.CoUninitialize()

result = {'done': False}
t = threading.Thread(target=lambda: (main(), result.__setitem__('done', True)), daemon=True)
t.start()
t.join(timeout=300)
if not result['done']:
    print("\n*** 超时 ***")
print(f"\n[{time.strftime('%H:%M:%S')}] 完成")
