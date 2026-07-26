#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 菜单结构与关键窗体探测
聚焦：菜单命令 + 输入数据窗体 + 计算触发方式
"""
import sys
import os
import time
import json
import win32gui
import win32con
import win32api
import win32process

OUTPUT_DIR = r"D:\MotorDesign\Scripts"

print("=" * 70)
print("Motor-CAD 菜单结构与关键窗体探测")
print("=" * 70)

# Step 1: Find ALL Motor-CAD main forms (there are 2 instances)
print("\n[1] 查找所有 Motor-CAD 实例...")

motorcad_instances = []

def find_all_motorcad(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    
    if cls_name == 'TMainForm' and visible and 'Motor-CAD' in title:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        motorcad_instances.append({
            'hwnd': hwnd,
            'pid': pid,
            'title': title,
            'rect': rect,
        })
        print(f"  实例: hwnd={hwnd}, PID={pid}, title='{title}'")
    return True

win32gui.EnumWindows(find_all_motorcad, None)

if not motorcad_instances:
    print("  未找到 Motor-CAD!")
    sys.exit(1)

# Use the last (most recently started) instance
mc = motorcad_instances[-1]
main_hwnd = mc['hwnd']
print(f"\n  使用实例: PID={mc['pid']}, hwnd={main_hwnd}")

# Step 2: Get full menu structure
print("\n[2] 菜单结构...")

def get_menu_tree(menu, depth=0, max_depth=4):
    """Recursively get menu structure"""
    items = []
    try:
        count = win32gui.GetMenuItemCount(menu)
    except:
        return items
    
    for i in range(count):
        try:
            flags = win32gui.GetMenuState(menu, i, win32con.MF_BYPOSITION)
            text = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION)
            
            if not text:
                continue
            
            # Check if separator
            if flags & win32con.MF_SEPARATOR:
                items.append({'text': '---', 'depth': depth, 'sep': True})
                continue
            
            # Check if has submenu
            submenu = win32gui.GetSubMenu(menu, i)
            item = {
                'text': text,
                'depth': depth,
                'index': i,
                'enabled': not (flags & win32con.MF_GRAYED),
                'checked': bool(flags & win32con.MF_CHECKED),
                'has_submenu': submenu != 0,
            }
            
            if submenu and depth < max_depth:
                item['children'] = get_menu_tree(submenu, depth + 1, max_depth)
            
            items.append(item)
        except Exception as e:
            pass
    
    return items

menu = win32gui.GetMenu(main_hwnd)
if menu:
    menu_tree = get_menu_tree(menu)
    
    def print_menu(items, indent=0):
        for item in items:
            prefix = "  " * indent
            if item.get('sep'):
                print(f"{prefix}---")
                continue
            
            text = item['text'].replace('&', '')  # Remove accelerator markers
            en = "" if item['enabled'] else " (disabled)"
            chk = " [x]" if item['checked'] else ""
            
            print(f"{prefix}[{item['index']}] {text}{chk}{en}")
            
            if 'children' in item:
                print_menu(item['children'], indent + 1)
    
    print_menu(menu_tree)
    
    # Save menu structure
    menu_path = os.path.join(OUTPUT_DIR, "menu_structure.json")
    with open(menu_path, 'w', encoding='utf-8') as f:
        json.dump(menu_tree, f, ensure_ascii=False, indent=2)
    print(f"\n  菜单结构已保存: {menu_path}")

# Step 3: Find all Tfm_* forms (hidden calculation forms)
print("\n[3] 查找隐藏的计算窗体 (Tfm_*)...")

all_children = []

def collect_children(hwnd, lparam):
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

win32gui.EnumChildWindows(main_hwnd, collect_children, None)
print(f"  总控件数 (非递归): {len(all_children)}")

# Find Tfm_* forms
fm_forms = [c for c in all_children if c['class'].startswith('Tfm_') or c['class'].startswith('TForm')]
print(f"\n  窗体 ({len(fm_forms)} 个):")
for f in fm_forms:
    vis = "可见" if f['visible'] else "隐藏"
    print(f"    [{vis}] {f['class']:30s} '{f['title'][:40]}'  {f['width']}x{f['height']}  id={f['ctrl_id']}")

# Step 4: Find visible tab sheets
print("\n[4] 当前可见标签页...")

tabs = [c for c in all_children if c['class'] == 'TTabSheet']
vis_tabs = [t for t in tabs if t['visible']]
hid_tabs = [t for t in tabs if not t['visible']]

print(f"  可见标签页 ({len(vis_tabs)} 个):")
for t in vis_tabs:
    print(f"    '{t['title']}'  rect={t['rect']}  id={t['ctrl_id']}")

print(f"  隐藏标签页 ({len(hid_tabs)} 个):")
for t in hid_tabs[:20]:
    print(f"    '{t['title']}'  id={t['ctrl_id']}")

# Step 5: Find TAdvStringGrid (parameter input grids)
print("\n[5] 表格控件 (TAdvStringGrid)...")

grids = [c for c in all_children if c['class'] == 'TAdvStringGrid']
print(f"  表格 ({len(grids)} 个):")
for g in grids:
    vis = "可见" if g['visible'] else "隐藏"
    print(f"    [{vis}] {g['width']}x{g['height']}  rect={g['rect']}  id={g['ctrl_id']}  title='{g['title'][:30]}'")

# Step 6: Find TEdit, TComboBox, TMemo controls
print("\n[6] 输入控件...")

edits = [c for c in all_children if c['class'] in ('TEdit', 'TLabeledEdit')]
combos = [c for c in all_children if c['class'] == 'TComboBox']
memos = [c for c in all_children if c['class'] == 'TMemo']

vis_edits = [e for e in edits if e['visible']]
vis_combos = [c for c in combos if c['visible']]
vis_memos = [m for m in memos if m['visible']]

print(f"  可见输入框 ({len(vis_edits)} 个):")
for e in vis_edits:
    print(f"    '{e['title'][:50]}'  rect={e['rect']}  id={e['ctrl_id']}")

print(f"  可见下拉框 ({len(vis_combos)} 个):")
for c in vis_combos:
    print(f"    '{c['title'][:50]}'  rect={c['rect']}  id={c['ctrl_id']}")

print(f"  可见文本域 ({len(vis_memos)} 个):")
for m in vis_memos:
    print(f"    '{m['title'][:50]}'  rect={m['rect']}  id={m['ctrl_id']}")

# Step 7: Find TButton controls
print("\n[7] 按钮控件...")

buttons = [c for c in all_children if c['class'] in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton')]
vis_buttons = [b for b in buttons if b['visible'] and b['title']]
print(f"  可见按钮 ({len(vis_buttons)} 个):")
for b in vis_buttons:
    print(f"    '{b['title'][:50]}'  rect={b['rect']}  id={b['ctrl_id']}")

# Step 8: Find TLabel with important keywords
print("\n[8] 关键标签 (含计算/参数/结果关键词)...")

labels = [c for c in all_children if c['class'] == 'TLabel']
vis_labels = [l for l in labels if l['visible'] and l['title']]

keywords = ['calc', '计算', 'result', '结果', 'speed', '转速', 'torque', '扭矩', 
            'power', '功率', 'efficiency', '效率', 'temperature', '温度', 'loss', '损耗',
            'voltage', '电压', 'current', '电流', 'flux', '磁通']

key_labels = [l for l in vis_labels if any(kw in l['title'].lower() for kw in keywords)]
print(f"  关键标签 ({len(key_labels)} 个):")
for l in key_labels:
    print(f"    '{l['title'][:60]}'  rect={l['rect']}  id={l['ctrl_id']}")

# Step 9: Try to read grid contents via WM_GETTEXT
print("\n[9] 尝试读取表格内容...")

for g in grids:
    if g['visible']:
        try:
            # Try to get text from the grid
            text = win32gui.GetWindowText(g['hwnd'])
            print(f"    Grid id={g['ctrl_id']}: text='{text[:100]}'")
            
            # Try WM_GETTEXT
            length = win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXTLENGTH, 0, 0)
            if length > 0:
                buf = win32gui.PyMakeBuffer(length + 2)
                win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXT, length + 2, buf)
                text = buf[:length].decode('utf-8', errors='replace')
                print(f"    Grid id={g['ctrl_id']} WM_GETTEXT: '{text[:200]}'")
        except Exception as e:
            print(f"    Grid id={g['ctrl_id']}: error={e}")

# Step 10: Read status bar
print("\n[10] 状态栏...")

status_bars = [c for c in all_children if c['class'] == 'TStatusBar']
for sb in status_bars:
    text = win32gui.GetWindowText(sb['hwnd'])
    print(f"  状态栏: '{text}'")

# Step 11: Examine the .mot file format for parameter manipulation
print("\n[11] 检查 .mot 文件格式...")

mot_file = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
if os.path.exists(mot_file):
    with open(mot_file, 'r', encoding='latin-1') as f:
        lines = f.readlines()
    print(f"  .mot 文件: {mot_file}")
    print(f"  行数: {len(lines)}")
    print(f"  前30行:")
    for i, line in enumerate(lines[:30]):
        print(f"    {i:3d}: {line.rstrip()[:80]}")
    
    # Find parameter lines
    print(f"\n  参数行示例 (含 '=' 或 RPM 或 Speed):")
    for i, line in enumerate(lines):
        low = line.lower()
        if 'speed' in low or 'rpm' in low or 'pole' in low or 'voltage' in low or 'power' in low:
            print(f"    {i:4d}: {line.rstrip()[:80]}")

print("\n" + "=" * 70)
print("探测完成!")
print("=" * 70)
