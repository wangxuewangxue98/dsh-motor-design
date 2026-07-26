# -*- coding: utf-8 -*-
"""快速热分析测试 + 模式菜单检查"""
import sys, time, json, threading

MOT_FILE = r"D:\MotorDesign\Templates\160kw_assm_2222.mot"

def main():
    import pythoncom
    pythoncom.CoInitialize()
    try:
        import win32com.client
        import win32gui, ctypes
        ts = lambda: time.strftime('%H:%M:%S')
        
        print(f"[{ts()}] 连接 COM...")
        mc = win32com.client.Dispatch("MotorCAD.AppAutomation")
        print(f"[{ts()}] 连接成功")
        time.sleep(5)
        
        print(f"[{ts()}] 加载 160kw 文件...")
        mc.LoadFromFile(MOT_FILE)
        time.sleep(5)
        
        # 读取参数
        for vname in ["Program_Version", "Pole_Number", "Slot_Number", "DCBusVoltage", "PeakCurrent"]:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and val[0] >= 0:
                    print(f"  {vname} = {val[1]}")
            except: pass
        
        # 热分析
        print(f"\n[{ts()}] 热分析...")
        t0 = time.time()
        ret = mc.DoSteadyStateAnalysis()
        print(f"  返回 {ret} ({time.time()-t0:.1f}s)")
        
        thermal = {}
        for vname in ["T_[Winding_Average]", "T_[Winding_Max]", "T_[Housing_Average]",
                      "T_[Housing_Max]", "T_[Magnet_Average]", "T_[Rotor_Average]",
                      "T_[Stator_Average]", "T_[Coolant_In]", "T_[Coolant_Out]", "MaxTemperature"]:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and val[0] >= 0:
                    thermal[vname] = val[1]
                    if isinstance(val[1], (int, float)):
                        print(f"  {vname} = {val[1]:.2f}")
            except: pass
        
        # 电磁分析 (不修改参数)
        print(f"\n[{ts()}] 电磁分析 (默认参数)...")
        t0 = time.time()
        ret = mc.DoMagneticCalculation()
        print(f"  返回 {ret} ({time.time()-t0:.1f}s)")
        
        emag = {}
        for vname in ["TorqueAverage", "EMTorque", "Efficiency", "TotalLoss", "IronLoss", "CopperLoss"]:
            try:
                val = mc.GetVariable(vname)
                if isinstance(val, tuple) and val[0] >= 0:
                    emag[vname] = val[1]
                    print(f"  {vname} = {val[1]}")
            except: pass
        
        # 检查模式菜单
        print(f"\n[{ts()}] 检查模式菜单...")
        user32 = ctypes.windll.user32
        main_hwnd = None
        def find_main(hwnd, p):
            global main_hwnd
            try:
                if 'TMainForm' in win32gui.GetClassName(hwnd):
                    main_hwnd = hwnd
            except: pass
            return True
        win32gui.EnumWindows(find_main, None)
        
        if main_hwnd:
            menu = win32gui.GetMenu(main_hwnd)
            mode_menu = user32.GetSubMenu(menu, 2)  # 模式 menu
            count = user32.GetMenuItemCount(mode_menu)
            print(f"  模式菜单 ({count} 项):")
            for i in range(count):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetMenuStringW(mode_menu, i, buf, 256, 0x400)
                wid = user32.GetMenuItemID(mode_menu, i)
                state = user32.GetMenuState(mode_menu, i, 0x400)
                checked = bool(state & 0x8)
                print(f"    [{i}] {buf.value} (id={wid}, checked={checked})")
        
        # 保存结果
        output = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'thermal': thermal, 'emag': emag}
        with open(r"D:\MotorDesign\Scripts\com_160kw_results.json", 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"\n{'='*50}")
        print(f"热结果: {len(thermal)} 个, 电磁结果: {len(emag)} 个")
        
    finally:
        pythoncom.CoUninitialize()

result = {'done': False}
t = threading.Thread(target=lambda: (main(), result.__setitem__('done', True)), daemon=True)
t.start()
t.join(timeout=420)
if not result['done']:
    print("\n*** 超时 ***")
