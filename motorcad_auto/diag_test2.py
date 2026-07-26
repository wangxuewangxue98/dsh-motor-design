# -*- coding: utf-8 -*-
"""
diag_test2.py - 逐个验证所有热变量, 找出哪些挂起
"""
import sys, os, time, threading
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.path.insert(0, r"D:\MotorDesign")

MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

ALL_VARS = [
    "Program_Version", "Program_Module", "Pole_Number", "Slot_Number",
    "DCBusVoltage", "PeakCurrent", "WindingConnection",
    "T_[Winding_Average]", "T_[Winding_Max]",
    "T_[Housing_Average]", "T_[Housing_Max]",
    "T_[Magnet_Average]", "T_[Magnet_Max]",
    "T_[Rotor_Average]", "T_[Stator_Average]",
    "T_[Coolant_In]", "T_[Coolant_Out]",
]

def ts():
    return time.strftime('%H:%M:%S')

def main():
    import pythoncom
    pythoncom.CoInitialize()
    try:
        import win32com.client
        print(f"[{ts()}] Dispatch...", flush=True)
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{ts()}] OK, sleep 10s", flush=True)
        time.sleep(10)

        print(f"[{ts()}] LoadFromFile...", flush=True)
        mc.LoadFromFile(MOT_FILE)
        print(f"[{ts()}] Loaded, sleep 5s", flush=True)
        time.sleep(5)

        # 设置参数 + 热分析
        mc.SetVariable("DCBusVoltage", 350)
        mc.SetVariable("PeakCurrent", 100)
        print(f"[{ts()}] DoSteadyStateAnalysis...", flush=True)
        ret = mc.DoSteadyStateAnalysis()
        print(f"[{ts()}] Thermal ret={ret}, now reading vars...", flush=True)

        # 逐个读取变量
        results = {}
        for vname in ALL_VARS:
            print(f"[{ts()}]   {vname}...", end="", flush=True)
            t0 = time.time()
            try:
                val = mc.GetVariable(vname)
                dt = time.time() - t0
                if isinstance(val, tuple) and val[0] >= 0:
                    results[vname] = val[1]
                    print(f" = {val[1]} ({dt:.2f}s)", flush=True)
                elif isinstance(val, tuple):
                    print(f" FAIL(code={val[0]}) ({dt:.2f}s)", flush=True)
                else:
                    print(f" = {val} ({dt:.2f}s)", flush=True)
            except Exception as e:
                dt = time.time() - t0
                print(f" ERR: {e} ({dt:.2f}s)", flush=True)

        print(f"\n[{ts()}] === 汇总 ({len(results)}/{len(ALL_VARS)} 可读) ===", flush=True)
        for k, v in results.items():
            if isinstance(v, (int, float)):
                print(f"  {k}: {v:.4f}", flush=True)
            else:
                print(f"  {k}: {v}", flush=True)

    finally:
        pythoncom.CoUninitialize()

result = {'done': False}
t = threading.Thread(target=lambda: (main(), result.__setitem__('done', True)), daemon=True)
t.start()
t.join(timeout=180)
if not result['done']:
    print(f"\n[{ts()}] *** 超时 (3分钟) *** - 最后一个变量挂起!", flush=True)
