#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v6e — UI Automation (UIA) 方案

最后尝试: 使用 pywinauto UIA backend 和 comtypes 直接调用 UIA
如果失败, 转入半自动工作流
"""
import sys
import os
import time
import struct
import json
import ctypes
import win32gui
import win32con
import win32api
import win32process

print(f"Python: {struct.calcsize('P')*8}位")
print("=" * 70)

# ============================================================
# Step 1: 找到 Motor-CAD 窗口
# ============================================================
print("\n[1] 查找 Motor-CAD 主窗口...")

main_hwnd = None
mc_pid = None

def find_mc(hwnd, p):
    global main_hwnd, mc_pid
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if cls == 'TMainForm' and 'Motor' in title and vis:
            main_hwnd = hwnd
            _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
    except:
        pass
    return True

win32gui.EnumWindows(find_mc, None)

if not main_hwnd:
    print("  *** 错误: 未找到 Motor-CAD! ***")
    sys.exit(1)

print(f"  主窗口: hwnd={main_hwnd}, pid={mc_pid}")

# 先发送 ID=119 切换到求解视图
print("  发送 WM_COMMAND ID=119 切换到求解视图...")
win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 119, 0)
time.sleep(2)

# ============================================================
# Step 2: 尝试 pywinauto UIA backend
# ============================================================
print("\n[2] 尝试 pywinauto UIA backend...")

uia_success = False
try:
    from pywinauto import Application
    
    print("  连接 UIA backend...")
    app_uia = Application(backend='uia').connect(process=mc_pid)
    main_win_uia = app_uia.window(handle=main_hwnd)
    print(f"  UIA 连接成功!")
    
    # 尝试设置前台
    try:
        main_win_uia.set_focus()
        time.sleep(0.5)
    except:
        pass
    
    # 查找所有按钮
    print("  查找按钮...")
    try:
        buttons = main_win_uia.descendants(control_type="Button")
        print(f"  找到 {len(buttons)} 个按钮 (UIA)")
        for btn in buttons:
            try:
                name = btn.window_text()
                vis = btn.is_visible()
                en = btn.is_enabled()
                rect = btn.rectangle()
                print(f"    '{name[:30]}' vis={vis} en={en} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
                
                if '求解' in name or 'Solve' in name or '计算' in name:
                    print(f"    *** 找到求解按钮! 尝试 Invoke()... ***")
                    
                    # 记录前状态
                    fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
                    files_before = {}
                    if os.path.exists(fe_dir):
                        for f in os.listdir(fe_dir):
                            fpath = os.path.join(fe_dir, f)
                            files_before[f] = os.path.getmtime(fpath)
                    
                    # 尝试 Invoke
                    try:
                        btn.invoke()
                        print("    invoke() 调用完成")
                        time.sleep(5)
                    except Exception as e:
                        print(f"    invoke() 失败: {e}")
                        # 尝试 click
                        try:
                            btn.click()
                            print("    click() 调用完成")
                            time.sleep(5)
                        except Exception as e2:
                            print(f"    click() 失败: {e2}")
                    
                    # 检查文件变化
                    files_after = {}
                    if os.path.exists(fe_dir):
                        for f in os.listdir(fe_dir):
                            fpath = os.path.join(fe_dir, f)
                            files_after[f] = os.path.getmtime(fpath)
                    
                    changed = False
                    for f in files_after:
                        if f not in files_before or files_after[f] != files_before.get(f, 0):
                            changed = True
                            break
                    
                    if changed:
                        print(f"    *** 检测到文件变化! 计算已触发! ***")
                        uia_success = True
                    else:
                        print(f"    无文件变化")
                    
                    if uia_success:
                        break
            except Exception as e:
                print(f"    按钮信息获取失败: {e}")
    except Exception as e:
        print(f"  查找按钮失败: {e}")
    
    # 尝试查找 DataGrid/Table
    if not uia_success:
        print("\n  查找 DataGrid/Table...")
        try:
            # 发送 ID=120 切换到温度视图
            win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 120, 0)
            time.sleep(2)
            
            grids = main_win_uia.descendants(control_type="DataGrid")
            print(f"  找到 {len(grids)} 个 DataGrid")
            for g in grids:
                try:
                    name = g.window_text()
                    vis = g.is_visible()
                    print(f"    Grid: '{name[:30]}' vis={vis}")
                    if vis:
                        # 尝试读取内容
                        try:
                            # 获取子元素
                            items = g.children()
                            print(f"    子元素: {len(items)}")
                            for item in items[:10]:
                                try:
                                    print(f"      {item.window_text()[:60]}")
                                except:
                                    pass
                        except:
                            pass
                except:
                    pass
        except Exception as e:
            print(f"  查找 DataGrid 失败: {e}")

except Exception as e:
    print(f"  UIA 连接失败: {e}")

# ============================================================
# Step 3: 尝试 comtypes 直接调用 UIA
# ============================================================
if not uia_success:
    print("\n[3] 尝试 comtypes 直接调用 UIA...")
    try:
        import comtypes
        from comtypes.client import CreateObject
        
        # 创建 IUIAutomation 对象
        CLSID_CUIAutomation = comtypes.GUID("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
        IID_IUIAutomation = comtypes.GUID("{30CBE57D-D9D0-452A-AB13-7AC5AC8A5FE2}")
        
        # 尝试创建
        try:
            from ctypes import POINTER, byref, c_int
            from comtypes import CoCreateInstance, COMObject, IUnknown, HRESULT, GUID
            
            # 使用 windll 直接调用
            uia = ctypes.windll.uiautomationcore
            print("  uiautomationcore 加载成功")
        except Exception as e:
            print(f"  uiautomationcore 加载失败: {e}")
        
        # 使用 comtypes
        try:
            # 定义 IUIAutomation 接口
            import comtypes.gen.UIAutomationClient as UIAuto
            uia = CreateObject(UIAuto.CUIAutomation, interface=UIAuto.IUIAutomation)
            print("  IUIAutomation 创建成功")
            
            # 从 hwnd 获取 UIA 元素
            element = uia.ElementFromHandle(main_hwnd)
            print(f"  根元素: {element.CurrentName}")
            
            # 查找按钮
            condition = uia.CreatePropertyCondition(UIAuto.UIA_ControlTypePropertyId, UIAuto.UIA_ButtonControlTypeId)
            buttons = element.FindAll(UIAuto.TreeScope_Descendants, condition)
            print(f"  找到 {buttons.Length} 个按钮")
            
            for i in range(buttons.Length):
                btn = buttons.GetElement(i)
                name = btn.CurrentName
                print(f"    按钮 [{i}]: '{name[:40]}'")
                
                if '求解' in name or 'Solve' in name:
                    print(f"    *** 尝试 InvokePattern ***")
                    try:
                        pattern = btn.GetCurrentPattern(UIAuto.UIA_InvokePatternId)
                        pattern.Invoke()
                        print("    Invoke 调用完成!")
                        time.sleep(5)
                        uia_success = True
                    except Exception as e:
                        print(f"    Invoke 失败: {e}")
                    
                    if uia_success:
                        break
            
        except Exception as e:
            print(f"  comtypes UIA 失败: {e}")
            # 尝试不用预生成模块
            try:
                # 直接用 GUID 创建
                # CLSID for CUIAutomation
                clsid = comtypes.GUID("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")
                # 这需要类型库
                print("  尝试通过类型库创建...")
                comtypes.client.GetModule("UIAutomationClient.dll")
                import comtypes.gen.UIAutomationClient as UIAuto
                uia = CreateObject(UIAuto.CUIAutomation)
                print("  成功!")
            except Exception as e2:
                print(f"  类型库方式也失败: {e2}")
                
    except Exception as e:
        print(f"  comtypes 导入失败: {e}")

# ============================================================
# Step 4: 如果 UIA 失败，尝试 SendInput 真实鼠标点击
# ============================================================
if not uia_success:
    print("\n[4] UIA 失败，尝试 SendInput 真实鼠标点击...")
    
    # 找到"求解热模型"按钮的位置
    solve_btn_hwnd = None
    def find_solve_btn(hwnd, p):
        global solve_btn_hwnd
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == 'TButton' and '求解' in title:
                solve_btn_hwnd = hwnd
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_solve_btn, None)
    
    if solve_btn_hwnd:
        rect = win32gui.GetWindowRect(solve_btn_hwnd)
        print(f"  求解按钮: hwnd={solve_btn_hwnd}, rect={rect}")
        print(f"  按钮 {'可见' if win32gui.IsWindowVisible(solve_btn_hwnd) else '不可见'}")
        
        if not win32gui.IsWindowVisible(solve_btn_hwnd):
            # 尝试让按钮可见
            print("  按钮不可见，尝试 ShowWindow...")
            win32gui.ShowWindow(solve_btn_hwnd, win32con.SW_SHOWNORMAL)
            time.sleep(0.5)
            print(f"  ShowWindow 后: vis={win32gui.IsWindowVisible(solve_btn_hwnd)}")
        
        # 计算按钮中心坐标
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        print(f"  按钮中心: ({cx}, {cy})")
        
        # 尝试让窗口前台
        try:
            win32gui.SetForegroundWindow(main_hwnd)
            time.sleep(0.5)
        except:
            pass
        
        # 使用 SendInput 移动鼠标并点击
        print("  使用 SendInput 点击...")
        
        INPUT_MOUSE = 0
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_ABSOLUTE = 0x8000
        
        # 获取屏幕分辨率
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        print(f"  屏幕分辨率: {screen_w}x{screen_h}")
        
        # 转换为绝对坐标
        abs_x = int(cx * 65535 / screen_w)
        abs_y = int(cy * 65535 / screen_h)
        
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long),
                        ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong),
                        ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.c_void_p)]
        
        class INPUT(ctypes.Structure):
            class _INPUT(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT)]
            _anonymous_ = ("_input",)
            _fields_ = [("type", ctypes.c_ulong), ("_input", _INPUT)]
        
        # 记录前状态
        fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
        files_before = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                files_before[f] = os.path.getmtime(fpath)
        
        # 移动鼠标
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi.dx = abs_x
        inp.mi.dy = abs_y
        inp.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.3)
        
        # 按下
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi.dx = abs_x
        inp.mi.dy = abs_y
        inp.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.1)
        
        # 释放
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi.dx = abs_x
        inp.mi.dy = abs_y
        inp.mi.dwFlags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(5)
        
        # 检查文件变化
        files_after = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                files_after[f] = os.path.getmtime(fpath)
        
        changed = False
        for f in files_after:
            if f not in files_before or files_after[f] != files_before.get(f, 0):
                changed = True
                break
        
        if changed:
            print(f"  *** 检测到文件变化! 计算已触发! ***")
            uia_success = True
        else:
            print(f"  无文件变化")
            
            # 检查是否有进度窗口
            progress_found = [False]
            def check_progress(hwnd, p):
                try:
                    title = win32gui.GetWindowText(hwnd)
                    vis = win32gui.IsWindowVisible(hwnd)
                    if vis and ('Progress' in title or 'Transient' in title):
                        progress_found[0] = True
                        print(f"  发现进度窗口: '{title}'")
                except:
                    pass
                return True
            win32gui.EnumWindows(check_progress, None)
            
            if progress_found[0]:
                print(f"  *** 发现进度窗口! 计算可能已触发 ***")
                uia_success = True
    else:
        print("  未找到求解按钮")

# ============================================================
# 结果
# ============================================================
result = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'main_hwnd': main_hwnd,
    'uia_success': uia_success,
}

with open(r'D:\MotorDesign\Scripts\uia_solve_result.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n{'='*70}")
print(json.dumps(result, ensure_ascii=False, indent=2))
print(f"\n{'='*70}")

if uia_success:
    print("✅ 计算触发成功!")
else:
    print("❌ 所有自动触发方法均失败")
    print("建议: 实现半自动工作流")
