#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v5 — pywinauto + WindowFromPoint 验证

新策略:
  1. 使用 pywinauto 的 set_focus() 解决前台窗口问题
  2. 用 WindowFromPoint 验证按钮位置是否正确
  3. 使用 pywinauto 的 click() 方法点击按钮
  4. 同时尝试 pywinauto 的 type_keys() 键盘激活
"""
import sys
import os
import time
import ctypes
import win32gui
import win32con
import win32api
import win32process
from pywinauto import Application, findwindows
from pywinauto.timings import Timings

print("=" * 70)
print("Motor-CAD 自动解算 v5 — pywinauto")
print("=" * 70)

# ============================================================
# Step 0: 找到 Motor-CAD 窗口
# ============================================================
print("\n[0] 查找 Motor-CAD 窗口...")

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
            print(f"  找到: hwnd={hwnd}, pid={mc_pid}, title='{title}'")
    except:
        pass
    return True

win32gui.EnumWindows(find_mc, None)

if not main_hwnd:
    print("  错误: 未找到 Motor-CAD!")
    sys.exit(1)

# ============================================================
# Step 1: 使用 pywinauto 连接到 Motor-CAD
# ============================================================
print("\n[1] 使用 pywinauto 连接...")

try:
    # 方法1: connect 到已有进程
    app = Application(backend='win32').connect(process=mc_pid)
    print(f"  pywinauto 连接成功 (pid={mc_pid})")
    
    # 获取主窗口
    main_win = app.window(handle=main_hwnd)
    print(f"  主窗口: '{main_win.window_text()}'")
    print(f"  可见: {main_win.is_visible()}")
    print(f"  启用: {main_win.is_enabled()}")
    
    # 尝试 set_focus
    print("\n  尝试 set_focus()...")
    try:
        main_win.set_focus()
        time.sleep(0.5)
        print(f"  set_focus 完成, 前台: {win32gui.GetForegroundWindow() == main_hwnd}")
    except Exception as e:
        print(f"  set_focus 失败: {e}")
    
    # 尝试 restore
    try:
        if main_win.is_minimized():
            main_win.restore()
            time.sleep(0.5)
            print("  窗口已恢复")
    except:
        pass

except Exception as e:
    print(f"  pywinauto 连接失败: {e}")
    print("  尝试使用 backend='uia'...")
    try:
        app = Application(backend='uia').connect(process=mc_pid)
        main_win = app.window(handle=main_hwnd)
        print(f"  UIA 连接成功")
        main_win.set_focus()
        time.sleep(0.5)
    except Exception as e2:
        print(f"  UIA 连接也失败: {e2}")
        sys.exit(1)

# ============================================================
# Step 2: 找到 PageControl 和切换标签
# ============================================================
print("\n[2] 查找控件...")

# 获取所有子控件
def get_all_children(parent_hwnd):
    results = []
    def collect(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            enabled = win32gui.IsWindowEnabled(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            results.append({
                'hwnd': int(hwnd), 'class': cls, 'title': title,
                'visible': visible, 'enabled': enabled, 'rect': list(rect),
            })
        except:
            pass
        return True
    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return results

children = get_all_children(main_hwnd)

# 找 PageControl
pcs = [c for c in children if c['class'] == 'TPageControl' and c['visible']]
if pcs:
    main_pc = max(pcs, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
    pc_hwnd = main_pc['hwnd']
    pc_rect = main_pc['rect']
    print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}")
else:
    print("  错误: 未找到 PageControl!")
    sys.exit(1)

# ============================================================
# Step 3: 切换到 '求解设置' 标签
# ============================================================
print("\n[3] 切换到 '求解设置' 标签...")

# 使用 PostMessage 切换标签
def click_tab_x(pc_hwnd, x_pos, y_pos=12):
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

click_tab_x(pc_hwnd, 240)
time.sleep(1)

# 验证当前标签
def get_visible_tabsheets(main_hwnd):
    tabs = []
    def check(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == 'TTabSheet' and win32gui.IsWindowVisible(hwnd) and title:
                tabs.append(title)
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs

current_tabs = get_visible_tabsheets(main_hwnd)
print(f"  当前标签: {current_tabs}")

# ============================================================
# Step 4: 找到 '求解热模型' 按钮并验证位置
# ============================================================
print("\n[4] 查找 '求解热模型' 按钮...")

def get_visible_buttons(main_hwnd):
    buttons = []
    def check(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls in ('TButton', 'TBitBtn', 'TSpeedButton') and \
               win32gui.IsWindowVisible(hwnd) and title:
                enabled = win32gui.IsWindowEnabled(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                buttons.append({
                    'title': title, 'hwnd': int(hwnd),
                    'enabled': enabled, 'rect': list(rect),
                })
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

vis_btns = get_visible_buttons(main_hwnd)
solve_btn = None
for b in vis_btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']} rect={b['rect']}")
    if '求解热' in b['title']:
        solve_btn = b

if not solve_btn:
    print("  '求解热模型' 按钮未找到!")
    sys.exit(1)

# ============================================================
# Step 5: 使用 WindowFromPoint 验证按钮位置
# ============================================================
print(f"\n[5] 验证按钮位置...")

btn_rect = solve_btn['rect']
cx = (btn_rect[0] + btn_rect[2]) // 2
cy = (btn_rect[1] + btn_rect[3]) // 2
print(f"  按钮中心: ({cx}, {cy})")

# 用 WindowFromPoint 检查该位置是什么窗口
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

pt = POINT(cx, cy)
hwnd_at_point = ctypes.windll.user32.WindowFromPoint(pt)
print(f"  WindowFromPoint({cx}, {cy}) = hwnd {hwnd_at_point}")
if hwnd_at_point:
    print(f"    类名: {win32gui.GetClassName(hwnd_at_point)}")
    print(f"    标题: '{win32gui.GetWindowText(hwnd_at_point)}'")
    print(f"    是按钮? {hwnd_at_point == solve_btn['hwnd']}")

    # 检查是否是按钮的父窗口
    parent = win32gui.GetParent(solve_btn['hwnd'])
    print(f"    按钮父窗口: {parent}")
    print(f"    是父窗口? {hwnd_at_point == parent}")

# ============================================================
# Step 6: 尝试多种方法点击按钮
# ============================================================
print(f"\n[6] 点击 '求解热模型' (多方法)...")

fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
print(f"  FEResultsData 文件数: {len(files_before)}")

# 方法1: pywinauto click
print("\n  [方法1] pywinauto click()...")
try:
    btn_elem = main_win.child_window(handle=solve_btn['hwnd'])
    print(f"    pywinauto 控件: {btn_elem}")
    btn_elem.click()
    time.sleep(3)
    
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    print(f"    新文件: {len(new_files)}")
    
    if not new_files:
        # 检查是否有取消按钮出现
        vis_btns = get_visible_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in vis_btns)
        print(f"    取消按钮: {'有' if has_cancel else '无'}")
except Exception as e:
    print(f"    pywinauto click 失败: {e}")

# 方法2: pywinauto click_input (真实鼠标)
print("\n  [方法2] pywinauto click_input()...")
try:
    # 重新切到求解设置标签
    click_tab_x(pc_hwnd, 240)
    time.sleep(0.5)
    
    # 重新查找按钮
    vis_btns = get_visible_buttons(main_hwnd)
    for b in vis_btns:
        if '求解热' in b['title']:
            solve_btn = b
            break
    
    btn_elem = main_win.child_window(handle=solve_btn['hwnd'])
    btn_elem.click_input()
    time.sleep(3)
    
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    print(f"    新文件: {len(new_files)}")
    
    vis_btns = get_visible_buttons(main_hwnd)
    has_cancel = any('取消' in b['title'] for b in vis_btns)
    print(f"    取消按钮: {'有' if has_cancel else '无'}")
except Exception as e:
    print(f"    click_input 失败: {e}")

# 方法3: 直接用 mouse_event (不依赖前台窗口)
print("\n  [方法3] 直接 mouse_event (不依赖前台)...")
try:
    # 重新切到求解设置标签
    click_tab_x(pc_hwnd, 240)
    time.sleep(0.5)
    
    # 重新查找按钮
    vis_btns = get_visible_buttons(main_hwnd)
    for b in vis_btns:
        if '求解热' in b['title']:
            solve_btn = b
            break
    
    btn_rect = solve_btn['rect']
    cx = (btn_rect[0] + btn_rect[2]) // 2
    cy = (btn_rect[1] + btn_rect[3]) // 2
    
    # 先检查 WindowFromPoint
    pt = POINT(cx, cy)
    hwnd_at = ctypes.windll.user32.WindowFromPoint(pt)
    print(f"    WindowFromPoint({cx},{cy}) = {hwnd_at} (按钮={solve_btn['hwnd']})")
    
    # 移动鼠标并点击
    ctypes.windll.user32.SetCursorPos(cx, cy)
    time.sleep(0.1)
    
    # 检查鼠标位置下的窗口
    hwnd_under_cursor = ctypes.windll.user32.WindowFromPoint(pt)
    print(f"    鼠标下窗口: {hwnd_under_cursor}")
    
    # 点击
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(3)
    
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    print(f"    新文件: {len(new_files)}")
    
    vis_btns = get_visible_buttons(main_hwnd)
    has_cancel = any('取消' in b['title'] for b in vis_btns)
    print(f"    取消按钮: {'有' if has_cancel else '无'}")
    
    if has_cancel or new_files:
        print("    *** 计算可能已开始! ***")
except Exception as e:
    print(f"    mouse_event 失败: {e}")

# 方法4: 尝试先检查数据，再求解
print("\n  [方法4] 先 '检查数据' 再 '求解热模型'...")
try:
    # 切到输入数据标签
    click_tab_x(pc_hwnd, 150)
    time.sleep(1)
    
    # 找检查数据按钮
    vis_btns = get_visible_buttons(main_hwnd)
    check_btn = None
    for b in vis_btns:
        if '检查' in b['title']:
            check_btn = b
            break
    
    if check_btn:
        print(f"    点击 '检查数据'...")
        btn_elem = main_win.child_window(handle=check_btn['hwnd'])
        btn_elem.click()
        time.sleep(3)
        
        # 检查弹窗
        def close_dialogs(mc_pid):
            closed = []
            def find_and_close(hwnd, lparam):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    visible = win32gui.IsWindowVisible(hwnd)
                    _, p = win32process.GetWindowThreadProcessId(hwnd)
                    if p == mc_pid and visible and cls in ('TMessageForm', 'TForm', 'TDialog'):
                        title = win32gui.GetWindowText(hwnd)
                        if title and 'Motor-CAD' not in title and 'Message' not in title:
                            def get_text(ch, lp):
                                try:
                                    t = win32gui.GetWindowText(ch)
                                    if t:
                                        closed.append(t[:200])
                                except:
                                    pass
                                return True
                            win32gui.EnumChildWindows(hwnd, get_text, None)
                            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except:
                    pass
                return True
            win32gui.EnumWindows(find_and_close, None)
            return closed
        
        dialogs = close_dialogs(mc_pid)
        if dialogs:
            print(f"    弹窗: {dialogs}")
    
    # 切回求解设置标签
    click_tab_x(pc_hwnd, 240)
    time.sleep(1)
    
    # 重新查找求解按钮
    vis_btns = get_visible_buttons(main_hwnd)
    for b in vis_btns:
        if '求解热' in b['title']:
            solve_btn = b
            break
    
    if solve_btn:
        # 用 pywinauto click_input
        btn_elem = main_win.child_window(handle=solve_btn['hwnd'])
        print(f"    click_input '求解热模型'...")
        btn_elem.click_input()
        time.sleep(5)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        print(f"    新文件: {len(new_files)}")
        
        vis_btns = get_visible_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in vis_btns)
        has_solve = any('求解热' in b['title'] for b in vis_btns)
        print(f"    取消按钮: {'有' if has_cancel else '无'}, 求解按钮: {'有' if has_solve else '无'}")
        
        if has_cancel or new_files:
            print("    *** 计算可能已开始! 等待完成... ***")
            start_time = time.time()
            for wait in range(300):
                time.sleep(1)
                elapsed = int(time.time() - start_time)
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                d = close_dialogs(mc_pid)
                
                if elapsed % 15 == 0:
                    vis_btns = get_visible_buttons(main_hwnd)
                    has_cancel = any('取消' in b['title'] for b in vis_btns)
                    has_solve = any('求解热' in b['title'] for b in vis_btns)
                    print(f"    [{elapsed}s] 新文件={len(new_files)} 取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'}")
                    if d:
                        print(f"           弹窗: {d}")
                
                if has_solve and not has_cancel and elapsed > 5:
                    print(f"    [{elapsed}s] 计算完成!")
                    break
                if elapsed >= 300:
                    print(f"    超时")
                    break
            
            files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            final_new = files_after - files_before
            print(f"\n    最终新增文件: {len(final_new)}")
            for f in sorted(final_new):
                fpath = os.path.join(fe_dir, f)
                size = os.path.getsize(fpath)
                print(f"      {f}: {size} bytes")

except Exception as e:
    print(f"    方法4 失败: {e}")

# ============================================================
# 最终状态
# ============================================================
print(f"\n[7] 最终状态...")
files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
new_files = files_after - files_before
print(f"  FEResultsData 文件数: {len(files_after)} (新增: {len(new_files)})")
if new_files:
    for f in sorted(new_files):
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        mtime = os.path.getmtime(fpath)
        mtime_str = time.strftime('%H:%M:%S', time.localtime(mtime))
        print(f"    {f}: {size} bytes ({mtime_str})")

print("\n" + "=" * 70)
print("v5 完成!")
print("=" * 70)
