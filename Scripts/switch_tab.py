#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 弹窗读取 + 标签切换新方案
1. 读取 TMessageForm 弹窗内容
2. 用 TCM_SETCURSEL + WM_NOTIFY 切换标签
3. 用 ShowWindow 直接显示目标 TabSheet
"""
import sys
import os
import time
import win32gui
import win32con
import win32api
import win32process
import struct
import ctypes

# Constants
TCM_FIRST = 0x1300
TCM_SETCURSEL = TCM_FIRST + 12    # 0x130C
TCM_GETCURSEL = TCM_FIRST + 2     # 0x1302
TCM_GETITEMCOUNT = TCM_FIRST + 4  # 0x1304

print("=" * 70)
print("Motor-CAD 弹窗读取 + 标签切换")
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

# Step 1: Find and read ALL dialogs/message forms
print("\n[1] 查找弹窗...")

_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)

dialogs = []
def find_dialogs(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    
    if pid == mc_pid and visible:
        if cls_name not in ('TMainForm', 'TApplication'):
            dialogs.append({
                'hwnd': int(hwnd),
                'class': cls_name,
                'title': title,
            })
            print(f"  弹窗: '{title}' ({cls_name}) hwnd={hwnd}")
            
            # Enumerate children to find text/buttons
            dialog_children = []
            def collect_dialog_children(child_hwnd, lparam):
                child_cls = win32gui.GetClassName(child_hwnd)
                child_title = win32gui.GetWindowText(child_hwnd)
                child_visible = win32gui.IsWindowVisible(child_hwnd)
                child_enabled = win32gui.IsWindowEnabled(child_hwnd)
                try:
                    child_id = win32api.GetWindowLong(child_hwnd, win32con.GWL_ID)
                except:
                    child_id = 0
                rect = win32gui.GetWindowRect(child_hwnd)
                
                dialog_children.append({
                    'hwnd': int(child_hwnd),
                    'class': child_cls,
                    'title': child_title,
                    'visible': child_visible,
                    'enabled': child_enabled,
                    'ctrl_id': child_id,
                    'rect': list(rect),
                })
                return True
            
            win32gui.EnumChildWindows(hwnd, collect_dialog_children, None)
            
            print(f"    子控件 ({len(dialog_children)} 个):")
            for c in dialog_children:
                vis = "V" if c['visible'] else "H"
                en = "E" if c['enabled'] else "D"
                title_str = f"'{c['title'][:60]}'" if c['title'] else ""
                print(f"      [{vis}{en}] {c['class']:20s} id={c['ctrl_id']:8d} {title_str}")
    return True

win32gui.EnumWindows(find_dialogs, None)

if not dialogs:
    print("  无弹窗")

# Step 2: Find the PageControl and try to switch tabs
print("\n\n[2] 标签切换尝试...")

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
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect, None)

# Find PageControl
page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
main_pc = max(page_controls, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Find top-level tabs (13 tabs identified earlier)
top_tabs = []
for c in all_children:
    if c['class'] == 'TTabSheet':
        # Top-level tabs have their left edge close to the PageControl left edge
        if abs(c['rect'][0] - pc_rect[0]) < 10 and abs(c['rect'][1] - pc_rect[1]) < 30:
            top_tabs.append(c)

print(f"  顶级标签: {len(top_tabs)} 个")
for i, t in enumerate(top_tabs):
    vis = "可见" if t['visible'] else "隐藏"
    print(f"    [{i}] [{vis}] '{t['title']}' hwnd={t['hwnd']}")

# Try Method 1: TCM_SETCURSEL
print("\n  方法1: TCM_SETCURSEL...")
for target_idx in range(min(len(top_tabs), 13)):
    # Select tab
    result = win32gui.SendMessage(pc_hwnd, TCM_SETCURSEL, target_idx, 0)
    print(f"    TCM_SETCURSEL({target_idx}) = {result}")
    
    # Send WM_NOTIFY TCN_SELCHANGE
    # TCN_SELCHANGE = -551 (0xFFFFFDD9)
    # NMHDR structure: hwndFrom, idFrom, code
    nmhdr = struct.pack('IiI', pc_hwnd, main_pc['ctrl_id'], -551)
    buf = ctypes.create_string_buffer(nmhdr)
    win32gui.SendMessage(main_hwnd, win32con.WM_NOTIFY, main_pc['ctrl_id'], buf)
    
    time.sleep(0.5)
    
    # Check visible tabs
    vis_tabs = []
    def check_vis(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TTabSheet' and visible and title:
            vis_tabs.append(title)
        return True
    win32gui.EnumChildWindows(main_hwnd, check_vis, None)
    
    print(f"    可见标签: {vis_tabs}")
    
    # If we found a different tab, we're on the right track
    if vis_tabs and vis_tabs[0] != '几何':
        print(f"    *** 标签切换成功! ***")
        break

# Try Method 2: ShowWindow on the target TabSheet
print("\n  方法2: ShowWindow (直接显示TabSheet)...")

# Find the Lab tab (电磁热虚拟实验室)
lab_tabs = [t for t in top_tabs if '实验室' in t['title'] or 'lab' in t['title'].lower()]
if lab_tabs:
    lab_tab = lab_tabs[0]
    lab_hwnd = lab_tab['hwnd']
    print(f"    Lab标签: '{lab_tab['title']}' hwnd={lab_hwnd}")
    
    # Hide currently visible tab
    vis_tabs_now = [t for t in top_tabs if t['visible']]
    for vt in vis_tabs_now:
        print(f"    隐藏: '{vt['title']}'")
        win32gui.ShowWindow(vt['hwnd'], win32con.SW_HIDE)
    
    # Show the Lab tab
    print(f"    显示: '{lab_tab['title']}'")
    win32gui.ShowWindow(lab_hwnd, win32con.SW_SHOW)
    time.sleep(1)
    
    # Check what's visible now
    vis_tabs = []
    vis_buttons = []
    def check_after(hwnd, lparam):
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
    win32gui.EnumChildWindows(main_hwnd, check_after, None)
    
    print(f"    可见标签: {vis_tabs}")
    print(f"    可见按钮: {len(vis_buttons)} 个")
    for b in vis_buttons:
        en = "启用" if b['enabled'] else "禁用"
        print(f"      [{en}] '{b['title']}' id={b['ctrl_id']} rect={b['rect']}")
    
    # If Lab tab is visible, look for sub-tabs and buttons
    if any('实验室' in t or 'lab' in t.lower() for t in vis_tabs):
        print(f"\n    *** Lab 标签页已激活! ***")
        
        # Find sub PageControls
        sub_pcs = [c for c in all_children if c['class'] == 'TPageControl' and c['visible'] and c['hwnd'] != pc_hwnd]
        print(f"    子 PageControl: {len(sub_pcs)} 个")
        for spc in sub_pcs:
            spc_hwnd = spc['hwnd']
            spc_rect = spc['rect']
            print(f"      hwnd={spc_hwnd} rect={spc_rect}")
            
            # Get sub-tabs
            sub_tabs = [c for c in all_children if c['class'] == 'TTabSheet' and c['visible']]
            print(f"      可见子标签: {[t['title'] for t in sub_tabs]}")
            
            # Try clicking on sub-tabs
            # Estimate sub-tab positions
            spc_width = spc_rect[2] - spc_rect[0]
            # Count sub-tabs belonging to this PageControl
            sub_tab_count = len(sub_tabs)
            if sub_tab_count > 0:
                sub_tab_w = spc_width // max(sub_tab_count, 1)
                for si in range(min(sub_tab_count, 10)):
                    sx = spc_rect[0] + sub_tab_w // 2 + si * sub_tab_w
                    sy = spc_rect[1] + 12
                    
                    # Use PostMessage for click
                    lparam = win32api.MAKELONG(sx - spc_rect[0], 12)
                    win32gui.PostMessage(spc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
                    time.sleep(0.1)
                    win32gui.PostMessage(spc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
                    time.sleep(0.5)
                    
                    # Check buttons
                    vis_btns = []
                    def check_btns(hwnd, lparam):
                        cls_name = win32gui.GetClassName(hwnd)
                        title = win32gui.GetWindowText(hwnd)
                        visible = win32gui.IsWindowVisible(hwnd)
                        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
                            try:
                                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
                            except:
                                ctrl_id = 0
                            vis_btns.append({'title': title, 'ctrl_id': ctrl_id, 'hwnd': int(hwnd)})
                        return True
                    win32gui.EnumChildWindows(main_hwnd, check_btns, None)
                    
                    vis_sub_tabs = []
                    def check_sub_tabs(hwnd, lparam):
                        cls_name = win32gui.GetClassName(hwnd)
                        title = win32gui.GetWindowText(hwnd)
                        visible = win32gui.IsWindowVisible(hwnd)
                        if cls_name == 'TTabSheet' and visible and title:
                            vis_sub_tabs.append(title)
                        return True
                    win32gui.EnumChildWindows(main_hwnd, check_sub_tabs, None)
                    
                    print(f"        子标签[{si}]: {vis_sub_tabs}")
                    for b in vis_btns:
                        print(f"          按钮: '{b['title']}' id={b['ctrl_id']}")
        
        # Also enumerate ALL visible controls in the Lab tab
        print(f"\n    Lab 标签页所有可见控件:")
        vis_controls = []
        def check_all_vis(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            if visible and title:
                try:
                    ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
                except:
                    ctrl_id = 0
                rect = win32gui.GetWindowRect(hwnd)
                vis_controls.append({
                    'class': cls_name,
                    'title': title,
                    'ctrl_id': ctrl_id,
                    'rect': list(rect),
                })
            return True
        win32gui.EnumChildWindows(main_hwnd, check_all_vis, None)
        
        for c in vis_controls[:50]:
            print(f"      {c['class']:20s} id={c['ctrl_id']:8d} '{c['title'][:50]}'  rect={c['rect']}")

# Try Method 3: Send WM_COMMAND to Motor-CAD (brute force common IDs)
print("\n\n  方法3: WM_COMMAND 测试...")
print("    (测试常见的 Delphi action 命令 ID)")

# In Delphi, action commands often start from 0x8000 or similar
# Try a range of command IDs to find calculation triggers
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"

# Only try a limited range to be safe
test_ids = list(range(1000, 1100)) + list(range(2000, 2100)) + list(range(3000, 3100))
found_ids = []

for cmd_id in test_ids[:30]:  # Only test 30 to be safe
    files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    
    # Send WM_COMMAND
    win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
    time.sleep(1)
    
    files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_after - files_before
    
    if new_files:
        print(f"    ID={cmd_id}: 新文件! {new_files}")
        found_ids.append(cmd_id)
    
    # Check for dialogs
    dialog_titles = []
    def check_dialog(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and visible and cls_name == 'TMessageForm':
            dialog_titles.append(title)
            print(f"    ID={cmd_id}: 弹窗 '{title}'")
            # Close the dialog
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            time.sleep(0.5)
        return True
    win32gui.EnumWindows(check_dialog, None)

if not found_ids:
    print("    未找到触发计算的命令 ID")

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
