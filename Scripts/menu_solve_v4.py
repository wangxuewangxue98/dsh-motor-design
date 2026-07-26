#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v6d — 读取温度结果 + 按钮控制 ID 触发计算

发现: WM_COMMAND ID=119 创建了求解按钮和温度窗体
      WM_COMMAND ID=120 使温度窗体可见

策略:
  1. 发送 ID=120 使温度视图可见，读取已有温度结果
  2. 发送 ID=119 切换到求解视图，找到"求解热模型"按钮
  3. 获取按钮的控制 ID (GetDlgCtrlID)
  4. 发送 WM_COMMAND 到按钮的父窗口，触发 OnClick
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

print(f"  主窗口: hwnd={main_hwnd}, title='{win32gui.GetWindowText(main_hwnd)[:50]}'")

# ============================================================
# Step 2: 发送 ID=120 使温度视图可见，读取温度结果
# ============================================================
print("\n[2] 发送 WM_COMMAND ID=120 使温度视图可见...")

win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 120, 0)
time.sleep(2)

# 读取所有可见文本
print("  读取可见文本控件...")
all_texts = []

def read_all_text(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        if vis and title:
            all_texts.append({
                'cls': cls, 'title': title, 'hwnd': hwnd,
                'rect': rect,
                'ctrl_id': win32gui.GetDlgCtrlID(hwnd) if cls in ('TButton', 'TBitBtn') else None
            })
    except:
        pass
    return True

win32gui.EnumChildWindows(main_hwnd, read_all_text, None)

print(f"  可见文本控件: {len(all_texts)} 个")
for t in all_texts:
    extra = f", ctrl_id={t['ctrl_id']}" if t['ctrl_id'] is not None else ""
    print(f"    {t['cls']}: '{t['title'][:70]}' @ ({t['rect'][0]},{t['rect'][1]}){extra}")

# 查找温度数值
print(f"\n  === 温度相关文本 ===")
temp_data = {}
for t in all_texts:
    title = t['title']
    # 匹配温度值模式 (数字后跟 °C 或 C)
    if any(kw in title for kw in ['°C', 'Temp', 'temp', '温度', 'Node_Temp', 'T_', 'T[', 'Winding', 'Housing', 'Magnet', 'Stator', 'Rotor', 'Tooth', 'Shaft', 'Bearing', 'Coolant', 'Water', 'Oil']):
        print(f"    {t['cls']}: '{title[:90]}'")
        temp_data[title] = t['hwnd']

# ============================================================
# Step 3: 查找 TAdvStringGrid 和 TStringGrid 并读取内容
# ============================================================
print(f"\n[3] 查找 Grid 控件...")

grids = []
def find_grids(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if 'Grid' in cls:
            rect = win32gui.GetWindowRect(hwnd)
            grids.append({'hwnd': hwnd, 'cls': cls, 'vis': vis, 'rect': rect})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_grids, None)

print(f"  Grid 控件: {len(grids)} 个")
for g in grids:
    print(f"    {g['cls']}: hwnd={g['hwnd']}, vis={g['vis']}, rect={g['rect']}")

# 尝试读取可见 Grid 的内容
for g in grids:
    if not g['vis']:
        continue
    print(f"\n  读取 {g['cls']} (hwnd={g['hwnd']})...")
    
    # 方法1: WM_GETTEXT
    try:
        length = win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXTLENGTH, 0, 0)
        if length > 0:
            buf = win32gui.PyMakeBuffer(length + 2)
            win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXT, length + 2, buf)
            text = buf[:length].decode('gbk', errors='replace')
            print(f"    WM_GETTEXT ({length} chars): {text[:200]}")
        else:
            print(f"    WM_GETTEXT: 空")
    except Exception as e:
        print(f"    WM_GETTEXT 失败: {e}")
    
    # 方法2: 尝试发送 LB_GETCOUNT / LVM_GETITEMCOUNT (ListView)
    try:
        count = win32gui.SendMessage(g['hwnd'], 0x1004, 0, 0)  # LVM_GETITEMCOUNT
        if count > 0:
            print(f"    LVM_GETITEMCOUNT: {count}")
    except:
        pass

# ============================================================
# Step 4: 发送 ID=119 切换到求解视图，找按钮控制 ID
# ============================================================
print(f"\n[4] 发送 WM_COMMAND ID=119 切换到求解视图...")

win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 119, 0)
time.sleep(2)

# 查找所有 TButton 和 TBitBtn
buttons = []
def find_buttons(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        parent = win32gui.GetParent(hwnd)
        ctrl_id = win32gui.GetDlgCtrlID(hwnd)
        if cls in ('TButton', 'TBitBtn') and title:
            buttons.append({
                'hwnd': hwnd, 'cls': cls, 'title': title,
                'vis': vis, 'enabled': enabled, 'rect': rect,
                'parent': parent, 'ctrl_id': ctrl_id
            })
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_buttons, None)

print(f"\n  所有按钮 ({len(buttons)} 个):")
for b in buttons:
    print(f"    '{b['title'][:30]}' cls={b['cls']}, vis={b['vis']}, enabled={b['enabled']}, ctrl_id={b['ctrl_id']}, hwnd={b['hwnd']}, parent={b['parent']}")

# 查找"求解热模型"按钮
solve_btn = None
for b in buttons:
    if '求解' in b['title'] or 'Solve' in b['title'] or '计算' in b['title'] or 'Calculate' in b['title'] or 'Run' in b['title']:
        solve_btn = b
        print(f"\n  *** 求解按钮: '{b['title']}' ctrl_id={b['ctrl_id']} ***")
        break

# ============================================================
# Step 5: 尝试通过按钮控制 ID 发送 WM_COMMAND
# ============================================================
if solve_btn:
    print(f"\n[5] 通过控制 ID 发送 WM_COMMAND 触发计算...")
    print(f"  按钮: '{solve_btn['title']}'")
    print(f"  控制 ID: {solve_btn['ctrl_id']}")
    print(f"  父窗口: {solve_btn['parent']}")
    
    # 记录 FEResultsData 初始状态
    fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
    files_before = {}
    if os.path.exists(fe_dir):
        for f in os.listdir(fe_dir):
            fpath = os.path.join(fe_dir, f)
            files_before[f] = {
                'size': os.path.getsize(fpath),
                'mtime': os.path.getmtime(fpath)
            }
    print(f"  FEResultsData 文件数 (before): {len(files_before)}")
    
    # 方法1: WM_COMMAND 到父窗口
    if solve_btn['ctrl_id'] and solve_btn['ctrl_id'] > 0:
        print(f"\n  方法1: WM_COMMAND (ctrl_id={solve_btn['ctrl_id']}) 到父窗口...")
        win32gui.SendMessage(solve_btn['parent'], win32con.WM_COMMAND, solve_btn['ctrl_id'], 0)
        time.sleep(3)
        
        # 检查文件变化
        files_after = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                files_after[f] = {
                    'size': os.path.getsize(fpath),
                    'mtime': os.path.getmtime(fpath)
                }
        
        changed = False
        for f in files_after:
            if f not in files_before or files_after[f]['mtime'] != files_before[f].get('mtime', 0):
                changed = True
                break
        
        if changed:
            print(f"    *** 检测到文件变化! ***")
        else:
            print(f"    无文件变化")
    
    # 方法2: WM_COMMAND 到主窗口
    if solve_btn['ctrl_id'] and solve_btn['ctrl_id'] > 0:
        print(f"\n  方法2: WM_COMMAND (ctrl_id={solve_btn['ctrl_id']}) 到主窗口...")
        win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, solve_btn['ctrl_id'], 0)
        time.sleep(3)
        
        files_after = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                files_after[f] = os.path.getmtime(fpath)
        
        changed = False
        for f in files_after:
            if f not in files_before or files_after[f] != files_before.get(f, {}).get('mtime', 0):
                changed = True
                break
        
        if changed:
            print(f"    *** 检测到文件变化! ***")
        else:
            print(f"    无文件变化")
    
    # 方法3: BM_CLICK (再次尝试，也许在正确上下文中有效)
    print(f"\n  方法3: BM_CLICK 到按钮...")
    win32gui.SendMessage(solve_btn['hwnd'], win32con.BM_CLICK, 0, 0)
    time.sleep(3)
    
    files_after = {}
    if os.path.exists(fe_dir):
        for f in os.listdir(fe_dir):
            fpath = os.path.join(fe_dir, f)
            files_after[f] = os.path.getmtime(fpath)
    
    changed = False
    for f in files_after:
        if f not in files_before or files_after[f] != files_before.get(f, {}).get('mtime', 0):
            changed = True
            break
    
    if changed:
        print(f"    *** 检测到文件变化! ***")
    else:
        print(f"    无文件变化")
    
    # 方法4: 发送 BN_CLICKED 通知到父窗口
    if solve_btn['ctrl_id'] and solve_btn['ctrl_id'] > 0:
        print(f"\n  方法4: WM_COMMAND BN_CLICKED 到父窗口...")
        # BN_CLICKED = 0, wParam = HIWORD(0) | LOWORD(ctrl_id)
        wparam = solve_btn['ctrl_id']  # LOWORD = ctrl_id, HIWORD = 0 (BN_CLICKED)
        win32gui.SendMessage(solve_btn['parent'], win32con.WM_COMMAND, wparam, solve_btn['hwnd'])
        time.sleep(3)
        
        files_after = {}
        if os.path.exists(fe_dir):
            for f in os.listdir(fe_dir):
                fpath = os.path.join(fe_dir, f)
                files_after[f] = os.path.getmtime(fpath)
        
        changed = False
        for f in files_after:
            if f not in files_before or files_after[f] != files_before.get(f, {}).get('mtime', 0):
                changed = True
                break
        
        if changed:
            print(f"    *** 检测到文件变化! ***")
        else:
            print(f"    无文件变化")
    
    # 方法5: 尝试所有可能的控制 ID 值 (如果 GetDlgCtrlID 返回 0)
    if not solve_btn['ctrl_id'] or solve_btn['ctrl_id'] <= 0:
        print(f"\n  方法5: 控制ID无效，尝试暴力扫描...")
        # Delphi 按钮的控制 ID 通常在一个范围内
        for test_id in range(1000, 1100):
            win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, test_id, 0)
            time.sleep(0.5)
            
            files_after = {}
            if os.path.exists(fe_dir):
                for f in os.listdir(fe_dir):
                    fpath = os.path.join(fe_dir, f)
                    try:
                        files_after[f] = os.path.getmtime(fpath)
                    except:
                        pass
            
            changed = False
            for f in files_after:
                if f not in files_before or files_after[f] != files_before.get(f, {}).get('mtime', 0):
                    changed = True
                    break
            
            if changed:
                print(f"    *** 控制ID={test_id} 触发了计算! ***")
                break
        else:
            print(f"    1000-1099 范围内无效果")

else:
    print("\n[5] 未找到求解按钮")

# ============================================================
# Step 6: 再次发送 ID=120 读取温度结果
# ============================================================
print(f"\n[6] 再次发送 ID=120 读取温度结果...")

win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 120, 0)
time.sleep(2)

# 读取所有可见文本
texts_final = []
def read_final(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if vis and title and len(title) > 1:
            texts_final.append({'cls': cls, 'title': title, 'hwnd': hwnd})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, read_final, None)

print(f"  可见文本: {len(texts_final)} 个")

# 打印所有可能包含数值的文本
print(f"\n  === 可能包含结果的文本 ===")
for t in texts_final:
    title = t['title']
    # 包含数字的文本可能是结果
    if any(c.isdigit() for c in title) and len(title) < 100:
        print(f"    {t['cls']}: '{title}'")

# 查找 TAdvStringGrid
grids_final = []
def find_grids_final(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if 'Grid' in cls and vis:
            grids_final.append({'hwnd': hwnd, 'cls': cls})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_grids_final, None)

if grids_final:
    print(f"\n  可见 Grid 控件: {len(grids_final)} 个")
    for g in grids_final:
        print(f"    {g['cls']}: hwnd={g['hwnd']}")
        # 尝试 WM_GETTEXT
        length = win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXTLENGTH, 0, 0)
        if length > 0:
            buf = win32gui.PyMakeBuffer(length + 2)
            win32gui.SendMessage(g['hwnd'], win32con.WM_GETTEXT, length + 2, buf)
            text = buf[:length].decode('gbk', errors='replace')
            print(f"      内容: {text[:200]}")

# ============================================================
# 保存结果
# ============================================================
result = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'main_hwnd': main_hwnd,
    'solve_button': {
        'title': solve_btn['title'] if solve_btn else None,
        'ctrl_id': solve_btn['ctrl_id'] if solve_btn else None,
        'hwnd': solve_btn['hwnd'] if solve_btn else None,
        'vis': solve_btn['vis'] if solve_btn else None,
        'parent': solve_btn['parent'] if solve_btn else None,
    } if solve_btn else None,
    'visible_texts_count': len(texts_final),
    'temp_texts': [t['title'] for t in all_texts if any(kw in t['title'] for kw in ['°C', 'Temp', '温度', 'Node'])],
}

with open(r'D:\MotorDesign\Scripts\menu_solve_result_v4.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n{'='*70}")
print(json.dumps(result, ensure_ascii=False, indent=2))
