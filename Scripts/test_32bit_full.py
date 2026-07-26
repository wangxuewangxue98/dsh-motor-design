#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 32位 Python 全面自动化测试

测试:
  1. COM: win32com.client.Dispatch (pywin32 标准方式)
  2. COM: win32com.client.GetActiveObject (连接已运行实例)
  3. COM: comtypes (备选)
  4. UI: pywinauto 按钮点击 (32位 -> 32位)
  5. UI: SendMessageTimeout 检查窗口响应性
  6. UI: WM_COMMAND with 不同的 ctrl_id 值
"""
import sys
import os
import time
import struct

print(f"Python 位数: {struct.calcsize('P') * 8}")
print(f"Python 路径: {sys.executable}")
print()

# ============================================================
# Part 1: COM 测试
# ============================================================
print("=" * 60)
print("Part 1: COM 自动化测试")
print("=" * 60)

# 1a: win32com.client.Dispatch
print("\n[1a] win32com.client.Dispatch...")
try:
    import win32com.client
    import pythoncom
    
    pythoncom.CoInitialize()
    print("  COM 已初始化")
    
    # 先尝试 GetActiveObject (连接到已运行的实例)
    print("\n  尝试 GetActiveObject('MotorCAD.AppAutomation')...")
    try:
        mcad = win32com.client.GetActiveObject("MotorCAD.AppAutomation")
        print("  *** GetActiveObject 成功! ***")
        
        # 获取变量
        try:
            pid = mcad.GetVariable("MotorCADprocessID")
            print(f"  MotorCADprocessID = {pid}")
        except Exception as e:
            print(f"  GetVariable 失败: {e}")
        
    except Exception as e:
        print(f"  GetActiveObject 失败: {e}")
        
        # 尝试 Dispatch (创建新实例)
        print("\n  尝试 Dispatch('MotorCAD.AppAutomation')...")
        try:
            mcad = win32com.client.Dispatch("MotorCAD.AppAutomation")
            print("  *** Dispatch 成功! ***")
        except Exception as e2:
            print(f"  Dispatch 失败: {e2}")
            
            # 尝试 DispatchEx
            print("\n  尝试 DispatchEx...")
            try:
                mcad = win32com.client.DispatchEx("MotorCAD.AppAutomation")
                print("  *** DispatchEx 成功! ***")
            except Exception as e3:
                print(f"  DispatchEx 失败: {e3}")
                
                # 尝试从 CLSID
                print("\n  尝试从 CLSID 创建...")
                try:
                    import win32com.client.gencache
                    mcad = win32com.client.gencache.EnsureDispatch("MotorCAD.AppAutomation")
                    print("  *** EnsureDispatch 成功! ***")
                except Exception as e4:
                    print(f"  EnsureDispatch 失败: {e4}")
                    
                    # 1b: comtypes
                    print("\n[1b] comtypes 尝试...")
                    try:
                        import comtypes
                        import comtypes.client
                        mcad = comtypes.client.CreateObject("MotorCAD.AppAutomation")
                        print("  *** comtypes 成功! ***")
                    except Exception as e5:
                        print(f"  comtypes 失败: {e5}")
                        print("\n  *** 所有 COM 方法都失败! ***")
                        mcad = None

except ImportError as e:
    print(f"  win32com 导入失败: {e}")
    mcad = None

# 如果 COM 成功，运行计算
if mcad:
    print("\n  === COM 连接成功，运行计算 ===")
    
    try:
        # 获取基本信息
        print("  获取变量...")
        for var in ["MotorCADprocessID", "MotorCADexeName", "CurrentExeDir_MotorLAB", 
                     "Shaft_Speed_[RPM]", "Windings_[Turns]"]:
            try:
                val = mcad.GetVariable(var)
                print(f"    {var} = {val}")
            except:
                pass
        
        # 运行稳态热分析
        print("\n  DoSteadyStateAnalysis...")
        start = time.time()
        mcad.DoSteadyStateAnalysis()
        elapsed = time.time() - start
        print(f"  完成! 耗时 {elapsed:.1f}s")
        
        # 获取温度结果
        print("\n  温度结果:")
        for var in ["T_[Winding_Average]", "T_[Housing_Average]", "T_[Magnet_Average]",
                     "T_[Stator_Lam_Average]", "T_[Rotor_Lam_Average]"]:
            try:
                val = mcad.GetVariable(var)
                print(f"    {var} = {val} C")
            except:
                pass
        
        # 保存结果
        with open(r"D:\MotorDesign\Scripts\com32_success.txt", "w") as f:
            f.write(f"COM连接: 成功\n")
            f.write(f"DoSteadyStateAnalysis: 成功 ({elapsed:.1f}s)\n")
            for var in ["T_[Winding_Average]", "T_[Housing_Average]", "T_[Magnet_Average]"]:
                try:
                    val = mcad.GetVariable(var)
                    f.write(f"{var} = {val}\n")
                except:
                    pass
        
        mcad.Quit()
        print("\n  结果已保存到 com32_success.txt")
        
    except Exception as e:
        print(f"  计算失败: {e}")
        
        # 保存错误
        with open(r"D:\MotorDesign\Scripts\com32_success.txt", "w") as f:
            f.write(f"COM连接: 成功\n")
            f.write(f"计算失败: {e}\n")
else:
    print("\n  COM 不可用，继续 UI 自动化测试...")

# ============================================================
# Part 2: UI 自动化测试
# ============================================================
print("\n" + "=" * 60)
print("Part 2: UI 自动化测试 (32位 Python)")
print("=" * 60)

import win32gui
import win32con
import win32api
import win32process

# 找到 Motor-CAD 窗口
main_hwnd = None
mc_pid = None

def find_mc(hwnd, lparam):
    global main_hwnd, mc_pid
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls == 'TMainForm' and 'Motor-CAD' in title and win32gui.IsWindowVisible(hwnd):
            main_hwnd = hwnd
            _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
    except:
        pass
    return True

win32gui.EnumWindows(find_mc, None)

if not main_hwnd:
    print("  Motor-CAD 未找到!")
    sys.exit(1)

print(f"  Motor-CAD: hwnd={main_hwnd}, pid={mc_pid}")

# 检查窗口响应性 (SendMessageTimeout)
print("\n  检查窗口响应性...")
try:
    result = win32gui.SendMessageTimeout(
        main_hwnd, win32con.WM_NULL, 0, 0,
        0x0002,  # SMTO_ABORTIFHUNG
        5000  # 5秒超时
    )
    print(f"  SendMessageTimeout 返回: {result}")
    print(f"  窗口响应: {'是' if result[1] == 0 else '否'}")
except Exception as e:
    print(f"  SendMessageTimeout 失败: {e}")

# 找到 PageControl
def get_pagecontrols(hwnd):
    pcs = []
    def check(h, l):
        try:
            cls = win32gui.GetClassName(h)
            if cls == 'TPageControl' and win32gui.IsWindowVisible(h):
                pcs.append(h)
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, check, None)
    return pcs

pcs = get_pagecontrols(main_hwnd)
if pcs:
    # 找最大的
    pc_hwnd = max(pcs, key=lambda h: 
        (win32gui.GetWindowRect(h)[2]-win32gui.GetWindowRect(h)[0]) * 
        (win32gui.GetWindowRect(h)[3]-win32gui.GetWindowRect(h)[1]))
    pc_rect = win32gui.GetWindowRect(pc_hwnd)
    print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}")
else:
    print("  PageControl 未找到!")
    sys.exit(1)

# 切换到 '求解设置' 标签
print("\n  切换到 '求解设置' 标签...")
def click_tab(pc, x, y=12):
    lp = win32api.MAKELONG(x, y)
    win32gui.PostMessage(pc, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(0.5)

click_tab(pc_hwnd, 240)
time.sleep(1)

# 查找按钮
def get_buttons(hwnd):
    btns = []
    def check(h, l):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls in ('TButton', 'TBitBtn') and win32gui.IsWindowVisible(h) and title:
                btns.append({'title': title, 'hwnd': h, 'rect': win32gui.GetWindowRect(h)})
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, check, None)
    return btns

btns = get_buttons(main_hwnd)
solve_btn = None
for b in btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} rect={b['rect']}")
    if '求解热' in b['title']:
        solve_btn = b

if solve_btn:
    print(f"\n  找到 '求解热模型': hwnd={solve_btn['hwnd']}, rect={solve_btn['rect']}")
    
    # FEResultsData
    fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
    files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    
    # 方法A: pywinauto (32位)
    print("\n  [A] pywinauto (32位)...")
    try:
        from pywinauto import Application
        app = Application(backend='win32').connect(process=mc_pid)
        win = app.window(handle=main_hwnd)
        
        # set_focus
        try:
            win.set_focus()
            time.sleep(0.5)
            print(f"    set_focus: 前台={win32gui.GetForegroundWindow() == main_hwnd}")
        except Exception as e:
            print(f"    set_focus: {e}")
        
        # click
        btn = win.child_window(handle=solve_btn['hwnd'])
        btn.click()
        time.sleep(3)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new = files_now - files_before
        print(f"    新文件: {len(new)}")
        
        # 检查取消按钮
        btns2 = get_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in btns2)
        print(f"    取消按钮: {'有' if has_cancel else '无'}")
        
    except Exception as e:
        print(f"    pywinauto 失败: {e}")
    
    # 方法B: SendMessage (同步) 完整点击序列
    print("\n  [B] SendMessage 完整点击序列...")
    try:
        btn_hwnd = solve_btn['hwnd']
        rect = solve_btn['rect']
        cx = (rect[2] - rect[0]) // 2
        cy = (rect[3] - rect[1]) // 2
        lp = win32api.MAKELONG(cx, cy)
        
        # 1. WM_MOUSEMOVE
        win32gui.SendMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp)
        time.sleep(0.05)
        
        # 2. WM_LBUTTONDOWN
        win32gui.SendMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
        time.sleep(0.05)
        
        # 3. WM_LBUTTONUP
        win32gui.SendMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp)
        time.sleep(0.1)
        
        # 4. BN_CLICKED 通知到父窗口 (尝试多种 ctrl_id)
        parent = win32gui.GetParent(btn_hwnd)
        print(f"    父窗口: {parent}, 类名: {win32gui.GetClassName(parent)}")
        
        # 尝试不同的 ctrl_id
        for cid in [0, btn_hwnd, win32api.GetWindowLong(btn_hwnd, win32con.GWL_ID)]:
            wp = win32api.MAKELONG(cid, 0)  # BN_CLICKED = 0
            win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
            time.sleep(0.1)
        
        time.sleep(3)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new = files_now - files_before
        print(f"    新文件: {len(new)}")
        
        btns2 = get_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in btns2)
        print(f"    取消按钮: {'有' if has_cancel else '无'}")
        
    except Exception as e:
        print(f"    SendMessage 失败: {e}")
    
    # 方法C: PostMessage (异步)
    print("\n  [C] PostMessage 完整点击序列...")
    try:
        btn_hwnd = solve_btn['hwnd']
        rect = solve_btn['rect']
        cx = (rect[2] - rect[0]) // 2
        cy = (rect[3] - rect[1]) // 2
        lp = win32api.MAKELONG(cx, cy)
        
        # 重新切到求解设置
        click_tab(pc_hwnd, 240)
        time.sleep(0.5)
        
        # 重新找按钮
        btns2 = get_buttons(main_hwnd)
        for b in btns2:
            if '求解热' in b['title']:
                btn_hwnd = b['hwnd']
                rect = b['rect']
                cx = (rect[2] - rect[0]) // 2
                cy = (rect[3] - rect[1]) // 2
                lp = win32api.MAKELONG(cx, cy)
                break
        
        win32gui.PostMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp)
        time.sleep(0.05)
        win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
        time.sleep(0.1)
        win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp)
        time.sleep(0.1)
        
        parent = win32gui.GetParent(btn_hwnd)
        cid = win32api.GetWindowLong(btn_hwnd, win32con.GWL_ID)
        wp = win32api.MAKELONG(cid, 0)
        win32gui.PostMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
        
        time.sleep(5)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new = files_now - files_before
        print(f"    新文件: {len(new)}")
        
        btns3 = get_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in btns3)
        has_solve = any('求解热' in b['title'] for b in btns3)
        print(f"    取消按钮: {'有' if has_cancel else '无'}, 求解按钮: {'有' if has_solve else '无'}")
        
        if has_cancel or new:
            print("    *** 计算可能已开始! ***")
            # 等待完成
            for wait in range(180):
                time.sleep(1)
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new = files_now - files_before
                btns3 = get_buttons(main_hwnd)
                has_cancel = any('取消' in b['title'] for b in btns3)
                has_solve = any('求解热' in b['title'] for b in btns3)
                
                if wait % 15 == 0:
                    print(f"    [{wait}s] 新文件={len(new)} 取消={'有' if has_cancel else '无'}")
                
                if has_solve and not has_cancel and wait > 5:
                    print(f"    [{wait}s] 计算完成!")
                    break
                if wait >= 180:
                    print(f"    超时")
                    break
            
            files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            final = files_after - files_before
            print(f"    最终新增: {len(final)}")
            for f in sorted(final):
                print(f"      {f}: {os.path.getsize(os.path.join(fe_dir, f))} bytes")
        
    except Exception as e:
        print(f"    PostMessage 失败: {e}")

else:
    print("  '求解热模型' 未找到!")

# ============================================================
# 最终结果
# ============================================================
print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
