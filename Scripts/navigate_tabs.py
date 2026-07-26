#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 标签页导航 + 计算按钮查找
通过鼠标点击扫描标签栏，切换到 Lab/计算标签页
"""
import sys
import os
import time
import struct
import win32gui
import win32con
import win32api
import win32process

# Win32 Tab Control Messages (not in win32con)
TCM_FIRST = 0x1300
TCM_GETITEMCOUNT = TCM_FIRST + 4    # 0x1304
TCM_GETITEMRECT = TCM_FIRST + 10   # 0x130A
TCM_SETCURSEL = TCM_FIRST + 12     # 0x130C
TCM_GETCURSEL = TCM_FIRST + 2      # 0x1302

print("=" * 70)
print("Motor-CAD 标签导航 + 计算按钮查找")
print("=" * 70)

# Find Motor-CAD main form
main_hwnd = None

def find_main(hwnd, lparam):
    global main_hwnd
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    if cls_name == 'TMainForm' and visible and 'Motor-CAD' in title:
        if main_hwnd is None:
            main_hwnd = hwnd
            print(f"  主窗体: hwnd={hwnd}, title='{title}'")
    return True

win32gui.EnumWindows(find_main, None)

if not main_hwnd:
    print("  未找到 Motor-CAD!")
    sys.exit(1)

# Bring to foreground
try:
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    time.sleep(0.5)
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(1)
except:
    pass

# Find the main PageControl
all_children = []
def collect(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    try:
        ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
    except:
        ctrl_id = 0
    rect = win32gui.GetWindowRect(hwnd)
    
    all_children.append({
        'hwnd': int(hwnd),
        'class': cls_name,
        'title': title,
        'visible': visible,
        'ctrl_id': ctrl_id,
        'rect': list(rect),
        'width': rect[2] - rect[0],
        'height': rect[3] - rect[1],
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect, None)

# Find visible TPageControl
page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
print(f"\n  可见 PageControl: {len(page_controls)} 个")

if not page_controls:
    print("  未找到 PageControl!")
    sys.exit(1)

# Use the outermost (larger) PageControl
main_pc = max(page_controls, key=lambda c: c['width'] * c['height'])
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  主 PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Try to get tab count using raw message
tab_count = win32gui.SendMessage(pc_hwnd, TCM_GETITEMCOUNT, 0, 0)
print(f"  标签数 (TCM_GETITEMCOUNT): {tab_count}")

current_tab = win32gui.SendMessage(pc_hwnd, TCM_GETCURSEL, 0, 0)
print(f"  当前标签 (TCM_GETCURSEL): {current_tab}")

# Get tab item rects
print("\n  标签位置:")
tab_rects = []
for i in range(min(tab_count, 30)):
    try:
        buf = win32gui.PyMakeBuffer(16)
        win32gui.SendMessage(pc_hwnd, TCM_GETITEMRECT, i, buf)
        rect_data = bytes(buf)
        if len(rect_data) >= 16:
            left, top, right, bottom = struct.unpack('4i', rect_data[:16])
            # Convert to screen coordinates
            screen_left = pc_rect[0] + left
            screen_top = pc_rect[1] + top
            screen_right = pc_rect[0] + right
            screen_bottom = pc_rect[1] + bottom
            tab_rects.append((screen_left, screen_top, screen_right, screen_bottom))
            print(f"    [{i}] local=({left},{top},{right},{bottom}) screen=({screen_left},{screen_top},{screen_right},{screen_bottom})")
    except Exception as e:
        print(f"    [{i}] error: {e}")

# Function to check current visible tabs and buttons
def check_visible_controls():
    """Check what tabs and buttons are currently visible"""
    vis_tabs = []
    vis_buttons = []
    
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        
        if cls_name == 'TTabSheet' and visible and title:
            vis_tabs.append(title)
        elif cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            enabled = win32gui.IsWindowEnabled(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            vis_buttons.append({
                'title': title,
                'hwnd': int(hwnd),
                'ctrl_id': ctrl_id,
                'enabled': enabled,
                'rect': list(rect),
            })
        return True
    
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return vis_tabs, vis_buttons

# Function to click on a tab
def click_tab(screen_x, screen_y):
    """Click at the specified screen coordinates"""
    try:
        win32api.SetCursorPos((screen_x, screen_y))
        time.sleep(0.2)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)
        time.sleep(1)
        return True
    except:
        return False

# Scan through all tabs and look for calculation buttons
print("\n" + "=" * 50)
print("扫描所有标签页，查找计算按钮")
print("=" * 50)

# Keywords for calculation buttons
calc_keywords = ['calc', 'run', 'solve', 'analy', 'start', 'execute', 'perform',
                 '计算', '运行', '求解', '分析', '开始', '执行', '仿真', '模拟']

found_calc_buttons = []

for tab_idx, (left, top, right, bottom) in enumerate(tab_rects):
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    
    print(f"\n  --- 标签 [{tab_idx}] at ({center_x}, {center_y}) ---")
    
    # Click on the tab
    if not click_tab(center_x, center_y):
        print(f"    点击失败")
        continue
    
    # Check what's visible
    vis_tabs, vis_buttons = check_visible_controls()
    print(f"    可见标签页: {vis_tabs}")
    
    if vis_buttons:
        print(f"    可见按钮 ({len(vis_buttons)} 个):")
        for b in vis_buttons:
            en = "启用" if b['enabled'] else "禁用"
            print(f"      [{en}] '{b['title']}' id={b['ctrl_id']} hwnd={b['hwnd']} rect={b['rect']}")
        
        # Check for calculation-related buttons
        for b in vis_buttons:
            if any(kw.lower() in b['title'].lower() for kw in calc_keywords):
                found_calc_buttons.append({
                    'tab_index': tab_idx,
                    'tab_name': vis_tabs[0] if vis_tabs else f"tab_{tab_idx}",
                    **b
                })
                print(f"    *** 计算按钮! ***")
    
    # Also check for sub-tabs (if this tab has a nested PageControl)
    # Look for any newly visible TPageControl
    sub_pcs = []
    def find_sub_pc(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TPageControl' and visible:
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            # Skip the main page control
            if hwnd != pc_hwnd:
                sub_pcs.append({
                    'hwnd': int(hwnd),
                    'ctrl_id': ctrl_id,
                    'rect': list(rect),
                })
        return True
    
    win32gui.EnumChildWindows(main_hwnd, find_sub_pc, None)
    
    if sub_pcs:
        print(f"    子 PageControl ({len(sub_pcs)} 个):")
        for spc in sub_pcs:
            spc_hwnd = spc['hwnd']
            spc_rect = spc['rect']
            sub_tab_count = win32gui.SendMessage(spc_hwnd, TCM_GETITEMCOUNT, 0, 0)
            print(f"      hwnd={spc_hwnd} tabs={sub_tab_count} rect={spc_rect}")
            
            # Get sub-tab rects
            for si in range(min(sub_tab_count, 20)):
                try:
                    buf = win32gui.PyMakeBuffer(16)
                    win32gui.SendMessage(spc_hwnd, TCM_GETITEMRECT, si, buf)
                    rect_data = bytes(buf)
                    if len(rect_data) >= 16:
                        sleft, stop, sright, sbottom = struct.unpack('4i', rect_data[:16])
                        screen_x = spc_rect[0] + (sleft + sright) // 2
                        screen_y = spc_rect[1] + (stop + sbottom) // 2
                        
                        # Click on sub-tab
                        click_tab(screen_x, screen_y)
                        
                        # Check for buttons
                        _, sub_buttons = check_visible_controls()
                        if sub_buttons:
                            print(f"        子标签 [{si}] at ({screen_x},{screen_y}):")
                            for b in sub_buttons:
                                en = "启用" if b['enabled'] else "禁用"
                                print(f"          [{en}] '{b['title']}' id={b['ctrl_id']}")
                                if any(kw.lower() in b['title'].lower() for kw in calc_keywords):
                                    found_calc_buttons.append({
                                        'tab_index': tab_idx,
                                        'sub_tab_index': si,
                                        'tab_name': vis_tabs[0] if vis_tabs else f"tab_{tab_idx}",
                                        **b
                                    })
                                    print(f"          *** 计算按钮! ***")
                except:
                    pass

# Summary
print("\n" + "=" * 50)
print("扫描结果汇总")
print("=" * 50)

if found_calc_buttons:
    print(f"\n  找到 {len(found_calc_buttons)} 个计算相关按钮:")
    for b in found_calc_buttons:
        print(f"    标签[{b['tab_index']}] '{b.get('tab_name', '')}'")
        print(f"    按钮: '{b['title']}' id={b['ctrl_id']} hwnd={b['hwnd']}")
        print(f"    位置: {b['rect']}")
        print(f"    启用: {b['enabled']}")
        print()
else:
    print("\n  未找到计算相关按钮!")
    print("  可能原因:")
    print("    1. 计算按钮在更深层级的标签页中")
    print("    2. 计算通过菜单触发（Delphi菜单不可通过Win32访问）")
    print("    3. 计算通过键盘快捷键触发")
    print("    4. 需要先切换到Lab标签页才能看到计算按钮")

# Try keyboard shortcuts
print("\n" + "=" * 50)
print("尝试键盘快捷键")
print("=" * 50)

# Make sure Motor-CAD is in foreground
try:
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(0.5)
except:
    pass

# Try common shortcuts
shortcuts_to_try = [
    ('F5', win32con.VK_F5, None),
    ('F6', win32con.VK_F6, None),
    ('F7', win32con.VK_F7, None),
    ('F9', win32con.VK_F9, None),
    ('Ctrl+R', ord('R'), win32con.VK_CONTROL),
    ('Ctrl+E', ord('E'), win32con.VK_CONTROL),
    ('Ctrl+T', ord('T'), win32con.VK_CONTROL),
    ('Ctrl+M', ord('M'), win32con.VK_CONTROL),
    ('Ctrl+Enter', win32con.VK_RETURN, win32con.VK_CONTROL),
    ('Alt+C', ord('C'), win32con.VK_MENU),
]

for name, key, modifier in shortcuts_to_try:
    print(f"\n  尝试 {name}...")
    
    # Take note of FEResultsData directory before
    fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
    files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    
    # Send keyboard shortcut
    try:
        if modifier:
            win32api.keybd_event(modifier, 0, 0, 0)
            time.sleep(0.1)
        win32api.keybd_event(key, 0, 0, 0)
        time.sleep(0.1)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        if modifier:
            time.sleep(0.1)
            win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)
        
        # Wait for potential calculation
        time.sleep(3)
        
        # Check if new files appeared
        files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_after - files_before
        if new_files:
            print(f"    新文件出现: {new_files}")
        else:
            print(f"    无变化")
        
        # Check status bar for messages
        status_bars = [c for c in all_children if c['class'] == 'TStatusBar']
        for sb in status_bars:
            text = win32gui.GetWindowText(sb['hwnd'])
            if text:
                print(f"    状态栏: '{text}'")
        
        # Check if any dialog appeared
        def find_dialogs(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            if visible and title and cls_name not in ('TMainForm', 'TApplication'):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                # Check if it belongs to Motor-CAD
                if pid in motorcad_pids:
                    print(f"    窗口出现: '{title}' ({cls_name})")
            return True
        
        motorcad_pids = []
        def get_pids(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name == 'TMainForm':
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                motorcad_pids.append(pid)
            return True
        win32gui.EnumWindows(get_pids, None)
        win32gui.EnumWindows(find_dialogs, None)
        
    except Exception as e:
        print(f"    错误: {e}")

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
