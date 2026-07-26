#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 真实计算触发!
1. 用 PostMessage 切换到求解标签页
2. 点击"求解电磁模型"按钮
3. 监控 FEResultsData 目录
4. 读取结果
"""
import sys
import os
import time
import json
import win32gui
import win32con
import win32api
import win32process

print("=" * 70)
print("Motor-CAD 真实计算触发!")
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
    return True

win32gui.EnumWindows(find_main, None)
print(f"  主窗体: hwnd={main_hwnd}")

# Find PageControl
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
        'hwnd': int(hwnd), 'class': cls_name, 'title': title,
        'visible': visible, 'ctrl_id': ctrl_id, 'rect': list(rect),
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect, None)

page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
main_pc = max(page_controls, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Helper: Click on PageControl tab bar
def click_tab(x_pos):
    """Click on tab bar at relative x position"""
    lparam = win32api.MAKELONG(x_pos, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

# Helper: Get all visible buttons
def get_visible_buttons():
    buttons = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            buttons.append({
                'title': title, 'hwnd': int(hwnd), 'ctrl_id': ctrl_id,
                'enabled': enabled, 'rect': list(rect),
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

# Helper: Get visible tabs
def get_visible_tabs():
    tabs = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TTabSheet' and visible and title:
            tabs.append(title)
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs

# Helper: Click a button
def click_button(btn_hwnd):
    """Click a button by sending WM_LBUTTONDOWN/WM_LBUTTONUP"""
    rect = win32gui.GetWindowRect(btn_hwnd)
    center_x = (rect[0] + rect[2]) // 2
    center_y = (rect[1] + rect[3]) // 2
    
    # Convert to client coordinates relative to the button's parent
    # Actually, for buttons, we can send directly to the button window
    lparam = win32api.MAKELONG(
        (rect[2] - rect[0]) // 2,
        (rect[3] - rect[1]) // 2
    )
    
    # Try BM_CLICK first (simplest)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

# Helper: Send BM_CLICK message
def send_bm_click(btn_hwnd):
    """Send BM_CLICK message to button"""
    win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)

# FEResultsData directory
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"

# Step 1: Navigate to the solver tab (x=740 area based on scan)
print("\n[1] 导航到求解标签页...")

# First, close any dialogs
def close_dialogs():
    _, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
    def find_and_close(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and visible and cls_name == 'TMessageForm':
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    win32gui.EnumWindows(find_and_close, None)

close_dialogs()
time.sleep(0.5)

# Click on the solver tab area (x=740 had the calculation buttons)
# Let's try fine-grained scanning around x=720-780
print("  精细扫描 x=700-800...")
for x in range(700, 820, 10):
    click_tab(x)
    vis_tabs = get_visible_tabs()
    vis_btns = get_visible_buttons()
    
    current = vis_tabs[0] if vis_tabs else "?"
    btn_titles = [b['title'] for b in vis_btns]
    
    # Check if we found the solve buttons
    if any('求解' in t for t in btn_titles):
        print(f"  x={x}: 标签='{current}' 按钮={btn_titles}")
        print(f"  *** 找到求解按钮! ***")
        break
    
    # Print tab changes
    if vis_tabs and vis_tabs[0] not in ['几何', '径向']:
        print(f"  x={x}: 标签='{current}'")

# Step 2: Find and click "求解电磁模型" button
print("\n[2] 查找 '求解电磁模型' 按钮...")

vis_btns = get_visible_buttons()
print(f"  当前可见按钮: {len(vis_btns)} 个")
for b in vis_btns:
    en = "启用" if b['enabled'] else "禁用"
    print(f"    [{en}] '{b['title']}' id={b['ctrl_id']} hwnd={b['hwnd']} rect={b['rect']}")

solve_em_btn = None
solve_thermal_btn = None
cancel_solve_btn = None

for b in vis_btns:
    if '求解电磁' in b['title'] or 'Solve' in b['title'] and 'Electro' in b['title']:
        solve_em_btn = b
    elif '求解热' in b['title'] or 'Solve' in b['title'] and 'Thermal' in b['title']:
        solve_thermal_btn = b
    elif '取消' in b['title'] or 'Cancel' in b['title']:
        cancel_solve_btn = b

if solve_em_btn:
    print(f"\n  *** 找到电磁求解按钮: '{solve_em_btn['title']}' ***")
    print(f"  hwnd={solve_em_btn['hwnd']}, enabled={solve_em_btn['enabled']}")
    
    if solve_em_btn['enabled']:
        # Record files before
        files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        print(f"  FEResultsData 文件数 (计算前): {len(files_before)}")
        
        # Click the button
        print(f"\n  >>> 点击 '求解电磁模型' 按钮 <<<")
        send_bm_click(solve_em_btn['hwnd'])
        time.sleep(2)
        
        # Also try PostMessage click
        click_button(solve_em_btn['hwnd'])
        
        # Monitor for calculation progress
        print(f"\n  等待计算完成 (监控 FEResultsData)...")
        max_wait = 120  # 2 minutes max
        calc_started = False
        calc_done = False
        
        for wait_sec in range(max_wait):
            time.sleep(1)
            
            # Check for new files
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            
            if new_files and not calc_started:
                calc_started = True
                print(f"  [{wait_sec}s] 计算开始! 新文件: {new_files}")
            
            # Check for dialogs (error messages)
            _, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            dialog_found = False
            def check_dialog(hwnd, lparam):
                nonlocal_dialogs = []
                cls_name = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                visible = win32gui.IsWindowVisible(hwnd)
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == mc_pid and visible and cls_name == 'TMessageForm':
                    print(f"  [{wait_sec}s] 弹窗: '{title}'")
                    # Get message text
                    def get_text(child_hwnd, lparam):
                        child_cls = win32gui.GetClassName(child_hwnd)
                        child_title = win32gui.GetWindowText(child_hwnd)
                        if child_title:
                            print(f"    {child_cls}: '{child_title[:100]}'")
                        return True
                    win32gui.EnumChildWindows(hwnd, get_text, None)
                    # Close dialog
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return True
            win32gui.EnumWindows(check_dialog, None)
            
            # Check if cancel button is still visible (calculation in progress)
            current_btns = get_visible_buttons()
            cancel_visible = any('取消' in b['title'] for b in current_btns)
            solve_visible = any('求解电磁' in b['title'] for b in current_btns)
            
            if cancel_visible and not calc_started:
                calc_started = True
                print(f"  [{wait_sec}s] 计算中 (取消按钮可见)...")
            
            # Check status bar
            status_bars = [c for c in all_children if c['class'] == 'TStatusBar']
            for sb in status_bars:
                text = win32gui.GetWindowText(sb['hwnd'])
                if text and text.strip():
                    print(f"  [{wait_sec}s] 状态栏: '{text}'")
            
            # If solve button reappears and cancel is gone, calculation is done
            if calc_started and solve_visible and not cancel_visible:
                calc_done = True
                files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                final_new = files_after - files_before
                print(f"  [{wait_sec}s] 计算完成!")
                print(f"  新文件: {final_new}")
                break
            
            # Progress output every 10 seconds
            if wait_sec % 10 == 0 and wait_sec > 0:
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                print(f"  [{wait_sec}s] 等待中... 新文件数: {len(new_files)}")
        
        if not calc_done:
            print(f"\n  超时 ({max_wait}s)")
            files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            final_new = files_after - files_before
            print(f"  最终新文件: {final_new}")
        
        # Check final state
        print(f"\n  最终按钮状态:")
        final_btns = get_visible_buttons()
        for b in final_btns:
            en = "启用" if b['enabled'] else "禁用"
            print(f"    [{en}] '{b['title']}'")
        
        # List FEResultsData files
        print(f"\n  FEResultsData 目录:")
        if os.path.exists(fe_dir):
            for f in sorted(os.listdir(fe_dir)):
                size = os.path.getsize(os.path.join(fe_dir, f))
                print(f"    {f:40s} {size:10d} bytes")
    else:
        print(f"  按钮被禁用!")
else:
    print(f"  未找到电磁求解按钮!")
    print(f"  尝试更多位置...")
    
    # Try scanning more positions
    for x in range(600, 1000, 5):
        click_tab(x)
        vis_btns = get_visible_buttons()
        for b in vis_btns:
            if '求解' in b['title']:
                print(f"  x={x}: 找到 '{b['title']}'")
                solve_em_btn = b
                break
        if solve_em_btn:
            break

# Step 3: Also look for thermal solve button
print("\n\n[3] 查找 '求解热模型' 按钮...")

# Click on x=860 area (where thermal solve was found)
click_tab(860)
time.sleep(0.5)

vis_btns = get_visible_buttons()
for b in vis_btns:
    if '求解热' in b['title']:
        solve_thermal_btn = b
        print(f"  找到: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
        break

if not solve_thermal_btn:
    # Scan around x=860
    for x in range(840, 900, 5):
        click_tab(x)
        vis_btns = get_visible_buttons()
        for b in vis_btns:
            if '求解热' in b['title']:
                solve_thermal_btn = b
                print(f"  x={x}: 找到 '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
                break
        if solve_thermal_btn:
            break

# Step 4: Summary
print("\n\n" + "=" * 50)
print("计算按钮汇总")
print("=" * 50)

if solve_em_btn:
    print(f"  电磁求解: '{solve_em_btn['title']}' hwnd={solve_em_btn['hwnd']} id={solve_em_btn['ctrl_id']}")
else:
    print(f"  电磁求解: 未找到")

if solve_thermal_btn:
    print(f"  热求解: '{solve_thermal_btn['title']}' hwnd={solve_thermal_btn['hwnd']} id={solve_thermal_btn['ctrl_id']}")
else:
    print(f"  热求解: 未找到")

if cancel_solve_btn:
    print(f"  取消求解: '{cancel_solve_btn['title']}' hwnd={cancel_solve_btn['hwnd']}")

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
