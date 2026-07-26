#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 控件详细映射 - 获取 Control ID, 层级, 位置
用于构建 UI 自动化的控件查找表
"""
import sys
import os
import json
import time
import win32gui
import win32con
import win32api
import ctypes

# Motor-CAD PID (already running)
TARGET_PID = 30264
OUTPUT_DIR = r"D:\MotorDesign\Scripts"

print("=" * 70)
print("Motor-CAD 控件详细映射")
print("=" * 70)

# Step 1: Find the main form
print("\n[1] 查找 Motor-CAD 主窗体...")

main_hwnd = None
app_hwnd = None

def find_motorcad_windows(hwnd, lparam):
    global main_hwnd, app_hwnd
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    
    if cls_name == 'TMainForm' and visible:
        main_hwnd = hwnd
        print(f"  主窗体: hwnd={hwnd}, class='{cls_name}', title='{title}'")
    elif cls_name == 'TApplication':
        app_hwnd = hwnd
        print(f"  应用窗口: hwnd={hwnd}, class='{cls_name}', title='{title}'")
    return True

win32gui.EnumWindows(find_motorcad_windows, None)

if not main_hwnd:
    print("  未找到主窗体! 尝试从 TApplication 恢复...")
    if app_hwnd:
        win32gui.ShowWindow(app_hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(app_hwnd)
        time.sleep(3)
        win32gui.EnumWindows(find_motorcad_windows, None)

if not main_hwnd:
    print("  错误: 无法找到 Motor-CAD 主窗体")
    sys.exit(1)

# Step 2: Recursively enumerate all controls with full details
print("\n[2] 递归枚举所有控件 (含 Control ID, 层级, 位置)...")

all_controls = []
control_tree = {}

def get_control_info(hwnd, depth, parent_hwnd):
    """Get detailed info about a control"""
    try:
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        
        # Get Control ID (crucial for SendMessage)
        ctrl_id = None
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            pass
        
        # Get control text length
        text_len = len(title) if title else 0
        
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        
        info = {
            'hwnd': hwnd,
            'ctrl_id': ctrl_id,
            'class': cls_name,
            'title': title,
            'text_len': text_len,
            'visible': visible,
            'enabled': enabled,
            'rect': list(rect),
            'width': w,
            'height': h,
            'depth': depth,
            'parent': parent_hwnd,
        }
        
        all_controls.append(info)
        return info
    except Exception as e:
        return None

def enum_children_recursive(hwnd, depth, parent_hwnd):
    """Recursively enumerate child windows"""
    def callback(child_hwnd, lparam):
        info = get_control_info(child_hwnd, depth, parent_hwnd)
        if info:
            # Recurse into children
            enum_children_recursive(child_hwnd, depth + 1, child_hwnd)
        return True
    
    try:
        win32gui.EnumChildWindows(hwnd, callback, None)
    except:
        pass

# Start recursive enumeration from main form
enum_children_recursive(main_hwnd, 0, main_hwnd)

print(f"  总控件数: {len(all_controls)}")

# Step 3: Categorize controls by class
print("\n[3] 按控件类型分类...")

categories = {}
for ctrl in all_controls:
    cls = ctrl['class']
    if cls not in categories:
        categories[cls] = []
    categories[cls].append(ctrl)

print(f"  控件类型分布:")
for cls, ctrls in sorted(categories.items(), key=lambda x: -len(x[1])):
    print(f"    {cls:30s}: {len(ctrls):4d} 个")

# Step 4: Find key controls for automation
print("\n[4] 关键控件识别...")

# Tab controls (TPageControl, TTabSheet)
tab_controls = [c for c in all_controls if c['class'] in ('TPageControl', 'TTabSheet')]
print(f"\n  标签控件 ({len(tab_controls)} 个):")
for c in tab_controls[:20]:
    vis = "V" if c['visible'] else "H"
    print(f"    [{vis}] id={c['ctrl_id']:6d} {c['class']:15s} '{c['title'][:50]}'  rect={c['rect']}")

# Edit fields (TEdit, TMemo, TMaskEdit)
edit_controls = [c for c in all_controls if c['class'] in ('TEdit', 'TMemo', 'TMaskEdit', 'TLabeledEdit')]
print(f"\n  输入框 ({len(edit_controls)} 个):")
for c in edit_controls:
    vis = "V" if c['visible'] else "H"
    print(f"    [{vis}] id={c['ctrl_id']:6d} {c['class']:15s} '{c['title'][:50]}'  rect={c['rect']}  parent={c['parent']}")

# Buttons (TButton, TBitBtn, TSpeedButton)
button_controls = [c for c in all_controls if c['class'] in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton')]
print(f"\n  按钮 ({len(button_controls)} 个):")
# Show visible buttons with text
vis_buttons = [c for c in button_controls if c['visible'] and c['title']]
print(f"  可见且有标题的按钮 ({len(vis_buttons)} 个):")
for c in vis_buttons:
    print(f"    id={c['ctrl_id']:6d} {c['class']:15s} '{c['title'][:50]}'  rect={c['rect']}")

# ComboBoxes
combo_controls = [c for c in all_controls if c['class'] in ('TComboBox', 'TDBComboBox')]
print(f"\n  下拉框 ({len(combo_controls)} 个):")
for c in combo_controls:
    vis = "V" if c['visible'] else "H"
    print(f"    [{vis}] id={c['ctrl_id']:6d} {c['class']:15s} '{c['title'][:50]}'  rect={c['rect']}")

# StringGrids (possible results display)
grid_controls = [c for c in all_controls if c['class'] in ('TStringGrid', 'TDBGrid', 'TDrawGrid')]
print(f"\n  表格 ({len(grid_controls)} 个):")
for c in grid_controls:
    vis = "V" if c['visible'] else "H"
    print(f"    [{vis}] id={c['ctrl_id']:6d} {c['class']:15s} '{c['title'][:50]}'  rect={c['rect']}  size={c['width']}x{c['height']}")

# Labels (to find parameter names)
label_controls = [c for c in all_controls if c['class'] == 'TLabel']
vis_labels = [c for c in label_controls if c['visible'] and c['title']]
print(f"\n  可见标签 ({len(vis_labels)} 个, 显示前40个):")
for c in vis_labels[:40]:
    print(f"    id={c['ctrl_id']:6d} '{c['title'][:60]}'  rect={c['rect']}")

# Step 5: Find forms (TForm, hidden calculation forms)
print("\n[5] 窗体检测 (含隐藏的计算窗体)...")
form_controls = [c for c in all_controls if c['class'].startswith('Tfm_') or c['class'].startswith('TForm')]
print(f"  窗体 ({len(form_controls)} 个):")
for c in form_controls:
    vis = "V" if c['visible'] else "H"
    print(f"    [{vis}] id={c['ctrl_id']:6d} {c['class']:30s} '{c['title'][:40]}'  rect={c['rect']}  size={c['width']}x{c['height']}")

# Step 6: Build parent-child hierarchy for visible controls
print("\n[6] 构建可见控件层级树...")

# Build a map of hwnd -> control info
hwnd_map = {c['hwnd']: c for c in all_controls}

# Build children map
children_map = {}
for c in all_controls:
    parent = c['parent']
    if parent not in children_map:
        children_map[parent] = []
    children_map[parent].append(c)

# Print hierarchy of visible controls (top level under main form)
def print_tree(hwnd, indent=0):
    children = children_map.get(hwnd, [])
    vis_children = [c for c in children if c['visible']]
    for c in vis_children[:50]:  # limit
        prefix = "  " * indent
        title_str = f" '{c['title'][:40]}'" if c['title'] else ""
        id_str = f" id={c['ctrl_id']}" if c['ctrl_id'] else ""
        print(f"{prefix}{c['class']:20s}{title_str}{id_str}  ({c['width']}x{c['height']})")
        # Recurse
        child_vis = [ch for ch in children_map.get(c['hwnd'], []) if ch['visible']]
        if child_vis and indent < 3:
            print_tree(c['hwnd'], indent + 1)

print("\n  可见控件层级树 (前3层):")
print_tree(main_hwnd)

# Step 7: Save full control map to JSON
print("\n[7] 保存完整控件映射到 JSON...")

# Convert hwnd values to int for JSON serialization
json_controls = []
for c in all_controls:
    jc = dict(c)
    jc['hwnd'] = int(c['hwnd'])
    jc['parent'] = int(c['parent']) if c['parent'] else 0
    json_controls.append(jc)

output_path = os.path.join(OUTPUT_DIR, "control_map.json")
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump({
        'main_hwnd': int(main_hwnd),
        'total_controls': len(all_controls),
        'controls': json_controls,
        'categories': {k: len(v) for k, v in categories.items()},
    }, f, ensure_ascii=False, indent=2)

print(f"  已保存到: {output_path}")

# Step 8: Try to read current tab page
print("\n[8] 当前标签页检测...")
# Find visible TTabSheet
visible_tabs = [c for c in all_controls if c['class'] == 'TTabSheet' and c['visible']]
for c in visible_tabs:
    print(f"  可见标签页: '{c['title']}' (id={c['ctrl_id']}, rect={c['rect']})")

# Step 9: Try clicking on menu items
print("\n[9] 菜单结构...")
menu = win32gui.GetMenu(main_hwnd)
if menu:
    count = win32gui.GetMenuItemCount(menu)
    print(f"  菜单项数量: {count}")
    for i in range(min(count, 20)):
        try:
            item_text = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION)
            if item_text:
                # Get submenu
                submenu = win32gui.GetSubMenu(menu, i)
                sub_count = win32gui.GetMenuItemCount(submenu) if submenu else 0
                print(f"    [{i}] '{item_text}' (子菜单: {sub_count} 项)")
                if submenu:
                    for j in range(min(sub_count, 30)):
                        try:
                            sub_text = win32gui.GetMenuString(submenu, j, win32con.MF_BYPOSITION)
                            if sub_text:
                                print(f"      [{i}.{j}] '{sub_text}'")
                        except:
                            pass
        except:
            pass

print("\n" + "=" * 70)
print("映射完成!")
print(f"主窗体 hwnd: {main_hwnd}")
print(f"总控件数: {len(all_controls)}")
print(f"JSON 映射: {output_path}")
print("=" * 70)
