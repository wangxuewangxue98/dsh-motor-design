#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 完整标签扫描 + 精确定位计算按钮
从 x=0 到 x=1000 步长 10，同时检查子标签
"""
import sys
import os
import time
import win32gui
import win32con
import win32api
import win32process

print("=" * 70)
print("Motor-CAD 完整标签扫描")
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

# Close any dialogs
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

# Helper functions
def click_pc(x_pos, y_pos=12):
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

def get_all_visible():
    tabs = []
    buttons = []
    labels = []
    combos = []
    edits = []
    grids = []
    sub_pcs = []
    
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            ctrl_id = 0
        rect = win32gui.GetWindowRect(hwnd)
        
        if not visible:
            return True
        
        if cls_name == 'TTabSheet' and title:
            tabs.append(title)
        elif cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and title:
            buttons.append({
                'title': title, 'hwnd': int(hwnd), 'ctrl_id': ctrl_id,
                'enabled': enabled, 'rect': list(rect),
            })
        elif cls_name == 'TLabel' and title:
            labels.append(title)
        elif cls_name == 'TComboBox' and title:
            combos.append(title)
        elif cls_name in ('TEdit', 'TLabeledEdit') and title:
            edits.append(title)
        elif cls_name == 'TAdvStringGrid':
            grids.append({'ctrl_id': ctrl_id, 'rect': list(rect)})
        elif cls_name == 'TPageControl' and hwnd != pc_hwnd:
            sub_pcs.append({'hwnd': int(hwnd), 'rect': list(rect), 'ctrl_id': ctrl_id})
        return True
    
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs, buttons, labels, combos, edits, grids, sub_pcs

# Full scan
print("\n完整扫描标签栏 (x=0 到 1000, 步长 5):")
print(f"{'x':>5s} {'标签':20s} {'按钮':40s} {'子PC':5s}")
print("-" * 80)

solve_buttons_found = []

for x in range(0, 1001, 5):
    click_pc(x)
    tabs, buttons, labels, combos, edits, grids, sub_pcs = get_all_visible()
    
    current_tab = tabs[0] if tabs else "?"
    btn_titles = [b['title'] for b in buttons]
    has_sub = len(sub_pcs) > 0
    
    # Only print when something changes
    has_solve = any('求解' in t for t in btn_titles)
    if has_solve or has_sub or (current_tab not in ['几何', '径向', '?']):
        sub_str = f"是({len(sub_pcs)})" if has_sub else "否"
        print(f"{x:5d} {current_tab:20s} {str(btn_titles):40s} {sub_str}")
        
        if has_solve:
            for b in buttons:
                if '求解' in b['title']:
                    solve_buttons_found.append({
                        'x': x, 'tab': current_tab, **b
                    })
                    print(f"      >>> {b['title']} | hwnd={b['hwnd']} id={b['ctrl_id']} enabled={b['enabled']} rect={b['rect']}")

# Also try different y positions for the tab bar
print("\n\n尝试不同 y 位置 (x=740):")
for y in range(5, 40, 5):
    click_pc(740, y)
    tabs, buttons, _, _, _, _, _ = get_all_visible()
    current_tab = tabs[0] if tabs else "?"
    btn_titles = [b['title'] for b in buttons]
    if btn_titles:
        print(f"  y={y:3d}: 标签='{current_tab}' 按钮={btn_titles}")

# Summary
print("\n\n" + "=" * 50)
print("求解按钮汇总")
print("=" * 50)

if solve_buttons_found:
    # Deduplicate by button title
    seen = set()
    unique_buttons = []
    for b in solve_buttons_found:
        key = b['title']
        if key not in seen:
            seen.add(key)
            unique_buttons.append(b)
    
    for b in unique_buttons:
        print(f"  '{b['title']}'")
        print(f"    x={b['x']}, 标签='{b['tab']}'")
        print(f"    hwnd={b['hwnd']}, id={b['ctrl_id']}, enabled={b['enabled']}")
        print(f"    rect={b['rect']}")
else:
    print("  未找到求解按钮")
    print("  可能原因: 标签状态已改变, 需要重置")

# Restore to first tab
click_pc(20)

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
