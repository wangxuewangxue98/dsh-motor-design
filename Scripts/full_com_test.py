# -*- coding: utf-8 -*-
"""
Motor-CAD v11.1.5 COM 自动化全面测试
ActiveX 已启用 (单客户端设置)

测试内容:
1. COM 连接 (GetActiveObject / Dispatch)
2. 参数读取 (GetVariable)
3. 参数设置 (SetVariable)
4. 电磁分析 (DoMagneticCalculation)
5. 热分析 (DoSteadyStateAnalysis)
6. 结果读取
"""
import threading
import sys
import time
import struct
import traceback

print(f"Python {sys.version}")
print(f"架构: {struct.calcsize('P')*8}位")
print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

def run_com_test():
    """在独立线程中运行 COM 测试 (每个线程需要自己的 CoInitialize)"""
    import pythoncom
    pythoncom.CoInitialize()
    
    try:
        import win32com.client
        
        results = {
            'connected': False,
            'method': None,
            'version': None,
            'variables_read': {},
            'variables_set': {},
            'emag_done': False,
            'thermal_done': False,
            'emag_results': {},
            'thermal_results': {},
            'errors': []
        }
        
        # === 1. COM 连接 ===
        print("\n[1] COM 连接...")
        sys.stdout.flush()
        
        mc = None
        # 方法1: GetActiveObject (连接已运行实例)
        try:
            mc = win32com.client.GetActiveObject("MotorCAD.AppAutomation")
            results['method'] = 'GetActiveObject'
            print(f"  GetActiveObject 成功!")
        except Exception as e:
            print(f"  GetActiveObject 失败: {e}")
            # 方法2: Dispatch
            try:
                mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
                results['method'] = 'Dispatch'
                print(f"  Dispatch 成功!")
            except Exception as e2:
                print(f"  Dispatch 失败: {e2}")
                results['errors'].append(f"连接失败: {e2}")
                return results
        
        results['connected'] = True
        print(f"  连接方法: {results['method']}")
        sys.stdout.flush()
        
        # 等待 Motor-CAD 完全就绪
        print("  等待 Motor-CAD 就绪 (5秒)...")
        time.sleep(5)
        
        # === 2. 读取版本和基本参数 ===
        print("\n[2] 读取版本和参数...")
        sys.stdout.flush()
        
        # 尝试多个版本变量名
        version_vars = ["MotorCADVERSION", "ProgramVersion", "Version", "MotorCADVersion"]
        for vname in version_vars:
            try:
                val = mc.GetVariable(vname)
                print(f"  {vname} = {val}")
                if val and val != (-2, None) and val != (-1, None):
                    results['version'] = val
                    break
            except Exception as e:
                print(f"  {vname} 失败: {e}")
        
        # 读取已知参数 (从 .mot 文件)
        test_vars = [
            "StatorOuterDiameter",
            "RotorOuterDiameter", 
            "StackLength",
            "Pole_Number",
            "Slot_Number",
            "Phase_Number",
            "TurnsPerCoil",
            "ParallelCircuit",
            "OperatingSpeed",
            "DCBusVoltage",
            "PeakCurrent",
            "ArmatureCurrent",
            "WindingConnection",
        ]
        
        for vname in test_vars:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and len(val) == 2:
                    success, value = val
                    if success >= 0:
                        results['variables_read'][vname] = value
                        print(f"  {vname} = {value}")
                    else:
                        print(f"  {vname} 读取失败 (success={success})")
                else:
                    results['variables_read'][vname] = val
                    print(f"  {vname} = {val}")
            except Exception as e:
                print(f"  {vname} 异常: {e}")
        
        sys.stdout.flush()
        
        # === 3. 设置参数 ===
        print("\n[3] 设置参数...")
        sys.stdout.flush()
        
        # 读取当前值，然后设置新值
        try:
            orig = mc.GetVariable("StatorOuterDiameter")
            print(f"  原始 StatorOuterDiameter = {orig}")
            
            # 设置新值
            result = mc.SetVariable("StatorOuterDiameter", 0.15)
            print(f"  SetVariable 返回: {result}")
            
            # 读回验证
            new_val = mc.GetVariable("StatorOuterDiameter")
            print(f"  新值 StatorOuterDiameter = {new_val}")
            results['variables_set']['StatorOuterDiameter'] = new_val
            
            # 恢复原值
            if isinstance(orig, tuple) and len(orig) == 2 and orig[0] >= 0:
                mc.SetVariable("StatorOuterDiameter", orig[1])
                print(f"  已恢复原值")
        except Exception as e:
            print(f"  参数设置失败: {e}")
            results['errors'].append(f"参数设置: {e}")
        
        sys.stdout.flush()
        
        # === 4. 电磁分析 ===
        print("\n[4] 电磁分析 (DoMagneticCalculation)...")
        sys.stdout.flush()
        
        try:
            print("  正在运行电磁计算...")
            start_time = time.time()
            
            ret = mc.DoMagneticCalculation()
            elapsed = time.time() - start_time
            print(f"  DoMagneticCalculation 返回: {ret} (耗时 {elapsed:.1f}s)")
            
            if isinstance(ret, tuple) and len(ret) == 2:
                if ret[0] >= 0:
                    results['emag_done'] = True
                    print(f"  电磁分析完成!")
                else:
                    print(f"  电磁分析返回错误码: {ret[0]}")
            elif ret is None or ret == 0:
                results['emag_done'] = True
                print(f"  电磁分析完成!")
            else:
                print(f"  返回值: {ret}")
                
        except Exception as e:
            print(f"  电磁分析失败: {e}")
            results['errors'].append(f"电磁分析: {e}")
        
        sys.stdout.flush()
        
        # === 5. 读取电磁结果 ===
        if results['emag_done']:
            print("\n[5] 读取电磁分析结果...")
            sys.stdout.flush()
            
            emag_result_vars = [
                "TorqueAverage",
                "TorqueRipple",
                "EMTorque",
                "PeakTorque",
                "Efficiency",
                "TotalLoss",
                "IronLoss",
                "CopperLoss",
                "MagnetLoss",
                "PowerOutput",
                "BackEMFRMS",
                "PhaseResistance",
            ]
            
            for vname in emag_result_vars:
                try:
                    val = mc.GetVariable(vname)
                    if isinstance(val, tuple) and len(val) == 2:
                        if val[0] >= 0:
                            results['emag_results'][vname] = val[1]
                            print(f"  {vname} = {val[1]}")
                    else:
                        results['emag_results'][vname] = val
                        print(f"  {vname} = {val}")
                except Exception as e:
                    print(f"  {vname} 失败: {e}")
            
            # 读取图表数据 (转矩波形)
            try:
                print("  读取转矩波形...")
                torque_data = []
                for i in range(100):
                    try:
                        x, y = mc.GetMagneticGraphPoint("TorqueVW", i)
                        if isinstance(x, tuple):
                            if x[0] >= 0:
                                torque_data.append((x[1], y[1] if isinstance(y, tuple) else y))
                            else:
                                break
                        else:
                            torque_data.append((x, y))
                    except:
                        break
                if torque_data:
                    print(f"  转矩波形: {len(torque_data)} 点")
                    print(f"    前5点: {torque_data[:5]}")
                    avg_torque = sum(p[1] for p in torque_data) / len(torque_data)
                    print(f"    平均转矩: {avg_torque:.2f}")
                    results['emag_results']['TorqueWaveform_points'] = len(torque_data)
                    results['emag_results']['TorqueWaveform_avg'] = avg_torque
            except Exception as e:
                print(f"  转矩波形读取失败: {e}")
        
        sys.stdout.flush()
        
        # === 6. 热分析 ===
        print("\n[6] 热分析 (DoSteadyStateAnalysis)...")
        sys.stdout.flush()
        
        try:
            print("  正在运行稳态热分析...")
            start_time = time.time()
            
            ret = mc.DoSteadyStateAnalysis()
            elapsed = time.time() - start_time
            print(f"  DoSteadyStateAnalysis 返回: {ret} (耗时 {elapsed:.1f}s)")
            
            if isinstance(ret, tuple) and len(ret) == 2:
                if ret[0] >= 0:
                    results['thermal_done'] = True
                    print(f"  热分析完成!")
                else:
                    print(f"  热分析返回错误码: {ret[0]}")
            elif ret is None or ret == 0:
                results['thermal_done'] = True
                print(f"  热分析完成!")
            else:
                print(f"  返回值: {ret}")
                
        except Exception as e:
            print(f"  热分析失败: {e}")
            results['errors'].append(f"热分析: {e}")
        
        sys.stdout.flush()
        
        # === 7. 读取热结果 ===
        if results['thermal_done']:
            print("\n[7] 读取热分析结果...")
            sys.stdout.flush()
            
            thermal_result_vars = [
                "T_[Winding_Average]",
                "T_[Winding_Max]",
                "T_[Housing_Average]",
                "T_[Housing_Max]",
                "T_[Magnet_Average]",
                "T_[Magnet_Max]",
                "T_[Rotor_Average]",
                "T_[Stator_Average]",
                "T_[Coolant_In]",
                "T_[Coolant_Out]",
                "MaxTemperature",
                "FluidPumpPressure",
            ]
            
            for vname in thermal_result_vars:
                try:
                    val = mc.GetVariable(vname)
                    if isinstance(val, tuple) and len(val) == 2:
                        if val[0] >= 0:
                            results['thermal_results'][vname] = val[1]
                            print(f"  {vname} = {val[1]}")
                    else:
                        results['thermal_results'][vname] = val
                        print(f"  {vname} = {val}")
                except Exception as e:
                    print(f"  {vname} 失败: {e}")
        
        sys.stdout.flush()
        
        return results
        
    except Exception as e:
        print(f"严重错误: {e}")
        traceback.print_exc()
        return {'connected': False, 'errors': [str(e)]}
    finally:
        pythoncom.CoUninitialize()


# 运行测试 (最长 10 分钟，电磁+热分析可能需要几分钟)
print("开始 COM 自动化测试...")
print("这将测试电磁分析和热分析，可能需要几分钟")
sys.stdout.flush()

result = {'done': False, 'data': None}

def thread_func():
    result['data'] = run_com_test()
    result['done'] = True

t = threading.Thread(target=thread_func, daemon=True)
t.start()
t.join(timeout=600)  # 10 分钟超时

if t.is_alive():
    print("\n*** 测试超时 (10分钟) ***")
    print("电磁或热分析可能仍在运行")
elif result['done']:
    data = result['data']
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    print(f"COM 连接: {'成功' if data.get('connected') else '失败'}")
    print(f"连接方法: {data.get('method', 'N/A')}")
    print(f"版本: {data.get('version', 'N/A')}")
    print(f"参数读取: {len(data.get('variables_read', {}))} 个")
    print(f"参数设置: {len(data.get('variables_set', {}))} 个")
    print(f"电磁分析: {'完成' if data.get('emag_done') else '未完成'}")
    print(f"  结果数: {len(data.get('emag_results', {}))}")
    print(f"热分析: {'完成' if data.get('thermal_done') else '未完成'}")
    print(f"  结果数: {len(data.get('thermal_results', {}))}")
    if data.get('errors'):
        print(f"错误: {len(data['errors'])} 个")
        for e in data['errors']:
            print(f"  - {e}")
else:
    print("\n*** 测试异常终止 ***")

print("\n测试完成")
