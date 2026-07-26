# -*- coding: utf-8 -*-
"""
Motor-CAD v11.1.5 COM 自动化测试 v2
修正: 先加载 .mot 文件再进行分析
"""
import threading
import sys
import time
import struct
import traceback
import json

print(f"Python {sys.version}")
print(f"架构: {struct.calcsize('P')*8}位")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

def run_com_test():
    import pythoncom
    pythoncom.CoInitialize()
    
    try:
        import win32com.client
        
        # === 1. 连接 ===
        print("[1] COM 连接...")
        sys.stdout.flush()
        
        mc = None
        try:
            mc = win32com.client.GetActiveObject("MotorCAD.AppAutomation")
            print("  GetActiveObject 成功!")
        except:
            mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
            print("  Dispatch 成功 (新实例)")
        
        time.sleep(3)
        
        # === 2. 加载 .mot 文件 ===
        print(f"\n[2] 加载 .mot 文件: {MOT_FILE}")
        sys.stdout.flush()
        
        try:
            ret = mc.LoadFromFile(MOT_FILE)
            print(f"  LoadFromFile 返回: {ret}")
            time.sleep(5)  # 等待文件加载
        except Exception as e:
            print(f"  LoadFromFile 失败: {e}")
            # 尝试其他方法名
            for method_name in ['LoadFile', 'OpenFile', 'Open', 'load_from_file']:
                try:
                    ret = getattr(mc, method_name)(MOT_FILE)
                    print(f"  {method_name} 成功: {ret}")
                    time.sleep(5)
                    break
                except:
                    pass
        
        # === 3. 读取参数 ===
        print("\n[3] 读取参数...")
        sys.stdout.flush()
        
        # 从 .mot 文件中提取参数名
        mot_vars = []
        with open(MOT_FILE, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('[') and not line.startswith(';'):
                    name = line.split('=')[0].strip()
                    if name and len(name) < 60 and not name.startswith('@'):
                        mot_vars.append(name)
        
        print(f"  .mot 文件中有 {len(mot_vars)} 个参数")
        
        read_vars = {}
        for vname in mot_vars[:50]:  # 前50个参数
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and len(val) == 2:
                    success, value = val
                    if success >= 0:
                        read_vars[vname] = value
                        if len(read_vars) <= 20:
                            print(f"  {vname} = {value}")
                else:
                    read_vars[vname] = val
                    if len(read_vars) <= 20:
                        print(f"  {vname} = {val}")
            except Exception as e:
                pass
        
        print(f"  成功读取 {len(read_vars)}/{min(50, len(mot_vars))} 个参数")
        sys.stdout.flush()
        
        # === 4. 设置参数测试 ===
        print("\n[4] 参数设置测试...")
        sys.stdout.flush()
        
        test_params = [
            ("OperatingSpeed", 3000),
            ("DCBusVoltage", 400),
            ("PeakCurrent", 150),
        ]
        
        for vname, val in test_params:
            try:
                ret = mc.SetVariable(vname, val)
                readback = mc.GetVariable(vname)
                if isinstance(readback, tuple) and readback[0] >= 0:
                    print(f"  {vname}: 设置 {val} -> 读取 {readback[1]}")
                else:
                    print(f"  {vname}: 设置返回 {ret}, 读取返回 {readback}")
            except Exception as e:
                print(f"  {vname}: 失败 {e}")
        
        sys.stdout.flush()
        
        # === 5. 电磁分析 ===
        print("\n[5] 电磁分析 (DoMagneticCalculation)...")
        sys.stdout.flush()
        
        emag_results = {}
        try:
            print("  正在运行电磁计算...")
            t0 = time.time()
            ret = mc.DoMagneticCalculation()
            elapsed = time.time() - t0
            print(f"  返回: {ret} (耗时 {elapsed:.1f}s)")
            
            if (isinstance(ret, tuple) and ret[0] >= 0) or ret == 0 or ret is None:
                print("  电磁分析完成!")
                
                # 读取电磁结果
                emag_vars = [
                    "TorqueAverage", "TorqueRipple", "EMTorque", "PeakTorque",
                    "Efficiency", "TotalLoss", "IronLoss", "CopperLoss",
                    "MagnetLoss", "PowerOutput", "BackEMFRMS", "PhaseResistance",
                    "SaliencyRatio", "PowerFactor", "TerminalVoltage",
                ]
                
                for vname in emag_vars:
                    try:
                        val = mc.GetVariable(vname)
                        if isinstance(val, tuple) and len(val) == 2:
                            if val[0] >= 0:
                                emag_results[vname] = val[1]
                                print(f"  {vname} = {val[1]}")
                        else:
                            emag_results[vname] = val
                            print(f"  {vname} = {val}")
                    except:
                        pass
                
                # 读取转矩波形
                try:
                    torque_data = []
                    for i in range(200):
                        try:
                            result = mc.GetMagneticGraphPoint("TorqueVW", i)
                            if isinstance(result, tuple) and len(result) == 2:
                                x, y = result
                                if isinstance(x, tuple) and x[0] >= 0:
                                    torque_data.append((x[1], y[1] if isinstance(y, tuple) else y))
                                else:
                                    break
                            else:
                                break
                        except:
                            break
                    if torque_data:
                        avg = sum(p[1] for p in torque_data) / len(torque_data)
                        print(f"  转矩波形: {len(torque_data)} 点, 平均={avg:.2f}")
                        emag_results['torque_points'] = len(torque_data)
                        emag_results['torque_avg'] = avg
                except Exception as e:
                    print(f"  转矩波形失败: {e}")
            else:
                print(f"  电磁分析返回错误: {ret}")
        except Exception as e:
            print(f"  电磁分析异常: {e}")
        
        sys.stdout.flush()
        
        # === 6. 热分析 ===
        print("\n[6] 热分析 (DoSteadyStateAnalysis)...")
        sys.stdout.flush()
        
        thermal_results = {}
        try:
            print("  正在运行稳态热分析...")
            t0 = time.time()
            ret = mc.DoSteadyStateAnalysis()
            elapsed = time.time() - t0
            print(f"  返回: {ret} (耗时 {elapsed:.1f}s)")
            
            if (isinstance(ret, tuple) and ret[0] >= 0) or ret == 0 or ret is None:
                print("  热分析完成!")
                
                thermal_vars = [
                    "T_[Winding_Average]", "T_[Winding_Max]",
                    "T_[Housing_Average]", "T_[Housing_Max]",
                    "T_[Magnet_Average]", "T_[Magnet_Max]",
                    "T_[Rotor_Average]", "T_[Rotor_Max]",
                    "T_[Stator_Average]", "T_[Stator_Max]",
                    "T_[Coolant_In]", "T_[Coolant_Out]",
                    "T_[Shaft_Average]", "T_[ Bearing_Average]",
                    "MaxTemperature", "TotalLoss",
                ]
                
                for vname in thermal_vars:
                    try:
                        val = mc.GetVariable(vname)
                        if isinstance(val, tuple) and len(val) == 2:
                            if val[0] >= 0:
                                thermal_results[vname] = val[1]
                                print(f"  {vname} = {val[1]:.2f}")
                        else:
                            thermal_results[vname] = val
                            print(f"  {vname} = {val}")
                    except:
                        pass
            else:
                print(f"  热分析返回错误: {ret}")
        except Exception as e:
            print(f"  热分析异常: {e}")
        
        sys.stdout.flush()
        
        # === 汇总 ===
        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)
        print(f"参数读取: {len(read_vars)} 个")
        print(f"电磁结果: {len(emag_results)} 个")
        print(f"热结果: {len(thermal_results)} 个")
        
        if thermal_results:
            print("\n温度结果:")
            for k, v in thermal_results.items():
                if isinstance(v, (int, float)):
                    print(f"  {k}: {v:.2f} °C")
                else:
                    print(f"  {k}: {v}")
        
        # 保存结果
        output = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mot_file': MOT_FILE,
            'params_read': read_vars,
            'emag_results': emag_results,
            'thermal_results': thermal_results,
        }
        
        with open(r"D:\MotorDesign\Scripts\com_test_v2_results.json", 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n结果已保存到 com_test_v2_results.json")
        
        return output
        
    except Exception as e:
        print(f"严重错误: {e}")
        traceback.print_exc()
    finally:
        pythoncom.CoUninitialize()

# 运行
result = {'done': False}
def thread_func():
    run_com_test()
    result['done'] = True

t = threading.Thread(target=thread_func, daemon=True)
t.start()
t.join(timeout=600)

if t.is_alive():
    print("\n*** 超时 (10分钟) ***")
elif result['done']:
    print("\n测试完成")
