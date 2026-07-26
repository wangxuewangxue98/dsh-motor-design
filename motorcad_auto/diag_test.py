# -*- coding: utf-8 -*-
"""
diag_test.py - 精确诊断 motorcad_auto 挂起位置
每个 COM 调用前后都打印, 带线程超时
"""
import sys, os, time, threading
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.path.insert(0, r"D:\MotorDesign")

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

def ts():
    return time.strftime('%H:%M:%S')

def main():
    import pythoncom
    pythoncom.CoInitialize()
    try:
        import win32com.client
        print(f"[{ts()}] Dispatch 连接...", flush=True)
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{ts()}] 连接成功, 等待 10s 初始化...", flush=True)
        time.sleep(10)

        # 1. LoadFromFile
        print(f"[{ts()}] LoadFromFile('{MOT_FILE}')...", flush=True)
        ret = mc.LoadFromFile(MOT_FILE)
        print(f"[{ts()}] LoadFromFile 返回: {ret}", flush=True)
        time.sleep(5)

        # 2. 读取 Program_Version
        print(f"[{ts()}] GetVariable('Program_Version')...", flush=True)
        val = mc.GetVariable("Program_Version")
        print(f"[{ts()}] Program_Version = {val}", flush=True)

        # 3. 读取 Pole_Number
        print(f"[{ts()}] GetVariable('Pole_Number')...", flush=True)
        val = mc.GetVariable("Pole_Number")
        print(f"[{ts()}] Pole_Number = {val}", flush=True)

        # 4. SetVariable DCBusVoltage
        print(f"[{ts()}] SetVariable('DCBusVoltage', 350)...", flush=True)
        mc.SetVariable("DCBusVoltage", 350)
        print(f"[{ts()}] SetVariable OK", flush=True)

        # 5. SetVariable PeakCurrent
        print(f"[{ts()}] SetVariable('PeakCurrent', 100)...", flush=True)
        mc.SetVariable("PeakCurrent", 100)
        print(f"[{ts()}] SetVariable OK", flush=True)

        # 6. DoSteadyStateAnalysis
        print(f"[{ts()}] DoSteadyStateAnalysis()...", flush=True)
        t0 = time.time()
        ret = mc.DoSteadyStateAnalysis()
        print(f"[{ts()}] DoSteadyStateAnalysis 返回: {ret} ({time.time()-t0:.1f}s)", flush=True)

        # 7. 读取温度结果
        for vname in ["T_[Winding_Average]", "T_[Winding_Max]", "MaxTemperature"]:
            print(f"[{ts()}] GetVariable('{vname}')...", flush=True)
            val = mc.GetVariable(vname)
            print(f"[{ts()}] {vname} = {val}", flush=True)

        print(f"\n[{ts()}] === 诊断完成 ===", flush=True)

    finally:
        pythoncom.CoUninitialize()

result = {'done': False}
t = threading.Thread(target=lambda: (main(), result.__setitem__('done', True)), daemon=True)
t.start()
t.join(timeout=240)
if not result['done']:
    print(f"\n[{ts()}] *** 超时 (4分钟) *** - 某个 COM 调用挂起", flush=True)
