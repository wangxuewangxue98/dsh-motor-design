#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD ShowWindow 标签切换 + 完整 Lab 标签页扫描
"""
import sys
import os
import time
import win32gui
import win32con
import win32api
import win32process
import ctypes

print("=" * 70)
print("Motor-CAD ShowWindow 标签切换 + Lab 扫描")
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
_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)

# First, close any existing dialogs
def close_dialogs():
    def find_and_close(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and visible and cls_name == 'TMessageForm':
            print(f"  关闭弹窗: '{title}'")
            # Try to get the message text
            # Find all child windows (including labels)
            def get_text(child_hwnd, lparam):
                child_cls = win32gui.GetClassName(child_hwnd)
                child_title = win32gui.GetWindowText(child_hwnd)
                if child_title:
                    print(f"    {child_cls}: '{child_title[:100]}'")
                return True
            win32gui.EnumChildWindows(hwnd, get_text, None)
            
            # Also try to get the window text directly
            full_text = win32gui.GetWindowText(hwnd)
            print(f"    窗口文本: '{full_text}'")
            
            # Close it
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    win32gui.EnumWindows(find_and_close, None)

close_dialogs()
time.sleep(1)

# Enumerate all children
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

# Find top-level tabs
top_tabs = []
for c in all_children:
    if c['class'] == 'TTabSheet':
        if abs(c['rect'][0] - pc_rect[0]) < 10 and abs(c['rect'][1] - pc_rect[1]) < 30:
            top_tabs.append(c)

print(f"  顶级标签: {len(top_tabs)} 个")
for i, t in enumerate(top_tabs):
    vis = "V" if t['visible'] else "H"
    print(f"    [{i}] [{vis}] '{t['title']}' hwnd={t['hwnd']}")

# Use ShowWindow to switch to the Lab tab (电磁热虚拟实验室)
print("\n[1] 切换到 '电磁热虚拟实验室' 标签页...")

lab_tab = None
for t in top_tabs:
    if '实验室' in t['title']:
        lab_tab = t
        break

if not lab_tab:
    print("  未找到 Lab 标签页!")
    sys.exit(1)

# Hide currently visible tabs
vis_tabs_before = [t for t in top_tabs if t['visible']]
for vt in vis_tabs_before:
    print(f"  隐藏: '{vt['title']}'")
    win32gui.ShowWindow(vt['hwnd'], win32con.SW_HIDE)

# Show Lab tab
print(f"  显示: '{lab_tab['title']}'")
win32gui.ShowWindow(lab_tab['hwnd'], win32con.SW_SHOW)

# Force repaint
win32gui.InvalidateRect(main_hwnd, None, True)
time.sleep(2)

# Check what's visible now
def get_visible_info():
    vis_tabs = []
    vis_buttons = []
    vis_combos = []
    vis_edits = []
    vis_grids = []
    vis_labels = []
    vis_radios = []
    
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
            vis_tabs.append(title)
        elif cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and title:
            vis_buttons.append({
                'title': title, 'hwnd': int(hwnd), 'ctrl_id': ctrl_id,
                'enabled': enabled, 'rect': list(rect),
            })
        elif cls_name == 'TComboBox' and title:
            vis_combos.append({'title': title, 'ctrl_id': ctrl_id, 'rect': list(rect)})
        elif cls_name in ('TEdit', 'TLabeledEdit') and title:
            vis_edits.append({'title': title, 'ctrl_id': ctrl_id, 'rect': list(rect)})
        elif cls_name == 'TAdvStringGrid':
            vis_grids.append({'ctrl_id': ctrl_id, 'rect': list(rect),
                              'width': rect[2]-rect[0], 'height': rect[3]-rect[1]})
        elif cls_name == 'TLabel' and title:
            vis_labels.append({'title': title, 'ctrl_id': ctrl_id, 'rect': list(rect)})
        elif cls_name in ('TRadioGroup', 'TGroupBox') and title:
            vis_radios.append({'title': title, 'ctrl_id': ctrl_id, 'class': cls_name, 'rect': list(rect)})
        return True
    
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return vis_tabs, vis_buttons, vis_combos, vis_edits, vis_grids, vis_labels, vis_radios

vis_tabs, vis_buttons, vis_combos, vis_edits, vis_grids, vis_labels, vis_radios = get_visible_info()

print(f"\n  可见标签页: {vis_tabs}")
print(f"  可见按钮: {len(vis_buttons)} 个")
for b in vis_buttons:
    en = "启用" if b['enabled'] else "禁用"
    print(f"    [{en}] '{b['title']}' id={b['ctrl_id']} rect={b['rect']}")

print(f"  可见下拉框: {len(vis_combos)} 个")
print(f"  可见输入框: {len(vis_edits)} 个")
print(f"  可见表格: {len(vis_grids)} 个")
for g in vis_grids:
    print(f"    id={g['ctrl_id']} {g['width']}x{g['height']} rect={g['rect']}")

print(f"  可见标签文字: {len(vis_labels)} 个")
for l in vis_labels[:20]:
    print(f"    '{l['title'][:60]}'")

print(f"  可见分组框: {len(vis_radios)} 个")
for r in vis_radios[:20]:
    print(f"    {r['class']:15s} '{r['title'][:50]}'")

# If Lab tab is now visible, scan for sub-tabs and calculation controls
if any('实验室' in t for t in vis_tabs):
    print(f"\n[2] *** Lab 标签页已激活! 扫描子标签... ***")
    
    # Find sub PageControls (visible ones that aren't the main one)
    sub_pcs = []
    for c in all_children:
        if c['class'] == 'TPageControl' and c['hwnd'] != pc_hwnd:
            # Check if it's now visible
            if win32gui.IsWindowVisible(c['hwnd']):
                sub_pcs.append(c)
    
    print(f"  子 PageControl: {len(sub_pcs)} 个")
    
    for spc in sub_pcs:
        spc_hwnd = spc['hwnd']
        spc_rect = spc['rect']
        spc_w = spc_rect[2] - spc_rect[0]
        spc_h = spc_rect[3] - spc_rect[1]
        print(f"\n    PageControl: hwnd={spc_hwnd} rect={spc_rect} {spc_w}x{spc_h}")
        
        # Get all visible sub-tabs
        vis_sub_tabs = []
        all_sub_tabs = []
        def get_sub_tabs(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            if cls_name == 'TTabSheet' and title:
                all_sub_tabs.append({'hwnd': int(hwnd), 'title': title, 'visible': visible})
                if visible:
                    vis_sub_tabs.append(title)
            return True
        win32gui.EnumChildWindows(main_hwnd, get_sub_tabs, None)
        
        print(f"    可见子标签: {vis_sub_tabs}")
        
        # Try switching to each sub-tab using ShowWindow
        for sub_tab in all_sub_tabs:
            if not sub_tab['visible'] and sub_tab['title']:
                # Try to show this sub-tab
                print(f"\n      切换到子标签: '{sub_tab['title']}'")
                
                # Hide currently visible sub-tabs
                for st in all_sub_tabs:
                    if st['visible'] and st['hwnd'] != sub_tab['hwnd']:
                        # Only hide if it's in the same PageControl area
                        st_rect = win32gui.GetWindowRect(st['hwnd'])
                        if abs(st_rect[0] - spc_rect[0]) < 20:
                            win32gui.ShowWindow(st['hwnd'], win32con.SW_HIDE)
                
                # Show the target sub-tab
                win32gui.ShowWindow(sub_tab['hwnd'], win32con.SW_SHOW)
                time.sleep(1)
                
                # Check for buttons
                _, sub_btns, _, _, sub_grids, sub_labels, sub_radios = get_visible_info()
                
                if sub_btns:
                    print(f"      按钮 ({len(sub_btns)} 个):")
                    for b in sub_btns:
                        en = "启用" if b['enabled'] else "禁用"
                        print(f"        [{en}] '{b['title']}' id={b['ctrl_id']} rect={b['rect']}")
                
                if sub_labels:
                    # Show key labels
                    key_labels = [l for l in sub_labels if any(kw in l['title'].lower() for kw in 
                        ['calc', 'run', 'solve', 'start', 'compute', '分析', '计算', '运行', '求解', '开始'])]
                    if key_labels:
                        print(f"      关键标签:")
                        for l in key_labels:
                            print(f"        '{l['title'][:60]}'")
                
                if sub_grids:
                    print(f"      表格 ({len(sub_grids)} 个):")
                    for g in sub_grids:
                        print(f"        id={g['ctrl_id']} {g['width']}x{g['height']}")
        
        # Restore first sub-tab
        if all_sub_tabs:
            first_sub = all_sub_tabs[0]
            for st in all_sub_tabs:
                if st['visible'] and st['hwnd'] != first_sub['hwnd']:
                    st_rect = win32gui.GetWindowRect(st['hwnd'])
                    if abs(st_rect[0] - spc_rect[0]) < 20:
                        win32gui.ShowWindow(st['hwnd'], win32con.SW_HIDE)
            win32gui.ShowWindow(first_sub['hwnd'], win32con.SW_SHOW)

# Try switching to other important tabs
print("\n\n[3] 切换到其他重要标签页...")

important_tabs = ['输出数据', '温度', '求解设置', '输入数据']
for tab_name in important_tabs:
    target = None
    for t in top_tabs:
        if t['title'] == tab_name:
            target = t
            break
    
    if target:
        print(f"\n  --- 切换到 '{tab_name}' ---")
        
        # Hide all visible top-level tabs
        for t in top_tabs:
            if win32gui.IsWindowVisible(t['hwnd']):
                win32gui.ShowWindow(t['hwnd'], win32con.SW_HIDE)
        
        # Show target
        win32gui.ShowWindow(target['hwnd'], win32con.SW_SHOW)
        time.sleep(1)
        
        vis_tabs, vis_buttons, _, _, vis_grids, vis_labels, vis_radios = get_visible_info()
        
        print(f"  可见标签: {vis_tabs}")
        if vis_buttons:
            print(f"  按钮 ({len(vis_buttons)} 个):")
            for b in vis_buttons:
                en = "启用" if b['enabled'] else "禁用"
                print(f"    [{en}] '{b['title']}' id={b['ctrl_id']}")
        
        if vis_grids:
            print(f"  表格 ({len(vis_grids)} 个):")
            for g in vis_grids:
                print(f"    id={g['ctrl_id']} {g['width']}x{g['height']}")
        
        if vis_labels:
            key_labels = [l for l in vis_labels if any(kw in l['title'].lower() for kw in 
                ['calc', 'run', 'solve', 'torque', 'power', 'speed', 'temp', 'loss', 'efficiency',
                 '计算', '运行', '扭矩', '功率', '转速', '温度', '损耗', '效率'])]
            if key_labels:
                print(f"  关键标签 ({len(key_labels)} 个):")
                for l in key_labels[:15]:
                    print(f"    '{l['title'][:60]}'")

# Restore to Geometry tab
print("\n\n[4] 恢复到 '几何' 标签页...")
for t in top_tabs:
    if win32gui.IsWindowVisible(t['hwnd']):
        win32gui.ShowWindow(t['hwnd'], win32con.SW_HIDE)
geometry_tab = top_tabs[0]
win32gui.ShowWindow(geometry_tab['hwnd'], win32con.SW_SHOW)
print(f"  已恢复")

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
