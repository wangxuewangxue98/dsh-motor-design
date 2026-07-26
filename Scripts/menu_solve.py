#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v6 — 菜单命令 + 键盘快捷键方案

新策略:
  1. 通过菜单命令 (WM_COMMAND) 触发计算 — Delphi 菜单走 TAction 机制, 不是按钮消息
  2. 测试键盘快捷键 (F9, Ctrl+Enter, Alt+S 等)
  3. 使用 UI Automation InvokePattern
  4. 尝试通过 TApplication 窗口操作
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
# Step 1: 找到 Motor-CAD 主窗口
# ============================================================
print("\n[1] 查找 Motor-CAD 窗口...")

main_hwnd = None
app_hwnd = None
mc_pid = None
all_windows = []

def find_mc(hwnd, p):
    global main_hwnd, app_hwnd, mc_pid
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == 6032 or 'Motor' in title or cls == 'TMainForm':
            all_windows.append({
                'hwnd': hwnd, 'cls': cls, 'title': title,
                'vis': vis, 'pid': pid
            })
            if cls == 'TMainForm' and 'Motor' in title:
                main_hwnd = hwnd
                mc_pid = pid
            elif cls == 'TApplication' and 'Motor' in title:
                app_hwnd = hwnd
                mc_pid = pid
    except:
        pass
    return True

win32gui.EnumWindows(find_mc, None)

print(f"  找到 {len(all_windows)} 个相关窗口:")
for w in all_windows:
    print(f"    cls={w['cls']}, title='{w['title'][:40]}', vis={w['vis']}, pid={w['pid']}")

if not main_hwnd:
    # 尝试通过 PID 查找
    print("\n  尝试通过进程查找...")
    def find_by_pid(hwnd, p):
        global main_hwnd, app_hwnd
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == mc_pid:
                if cls == 'TMainForm':
                    main_hwnd = hwnd
                elif cls == 'TApplication':
                    app_hwnd = hwnd
        except:
            pass
        return True
    win32gui.EnumWindows(find_by_pid, None)

if not main_hwnd:
    print("  *** 错误: 未找到 Motor-CAD 主窗口! ***")
    sys.exit(1)

print(f"\n  主窗口: hwnd={main_hwnd}, title='{win32gui.GetWindowText(main_hwnd)}'")
print(f"  可见: {win32gui.IsWindowVisible(main_hwnd)}")
if app_hwnd:
    print(f"  App窗口: hwnd={app_hwnd}, title='{win32gui.GetWindowText(app_hwnd)}'")

# ============================================================
# Step 2: 让主窗口可见
# ============================================================
print("\n[2] 激活主窗口...")

# 先关闭启动对话框 (只关闭 License Error, Default Motor Type, Demo)
startup_dialogs = ['TLicenceErrorForm', 'TForm_Default_Motor_Type', 'TDemo_Form']
for w in all_windows:
    if w['cls'] in startup_dialogs:
        print(f"  关闭对话框: {w['cls']} (hwnd={w['hwnd']})")
        try:
            win32gui.PostMessage(w['hwnd'], win32con.WM_CLOSE, 0, 0)
        except:
            pass

time.sleep(2)

# 尝试多种方式让主窗口可见
print(f"  主窗口 vis (before): {win32gui.IsWindowVisible(main_hwnd)}")

# 方法1: SW_SHOWNORMAL
win32gui.ShowWindow(main_hwnd, win32con.SW_SHOWNORMAL)
time.sleep(0.5)
print(f"  SW_SHOWNORMAL 后 vis: {win32gui.IsWindowVisible(main_hwnd)}")

if not win32gui.IsWindowVisible(main_hwnd):
    # 方法2: SW_RESTORE
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    time.sleep(0.5)
    print(f"  SW_RESTORE 后 vis: {win32gui.IsWindowVisible(main_hwnd)}")

if not win32gui.IsWindowVisible(main_hwnd):
    # 方法3: 通过 TApplication 窗口
    if app_hwnd:
        print(f"  尝试通过 TApplication 窗口...")
        win32gui.ShowWindow(app_hwnd, win32con.SW_SHOW)
        time.sleep(0.5)
        win32gui.SendMessage(app_hwnd, win32con.WM_COMMAND, 0, 0)
        time.sleep(1)
        print(f"  App操作后 vis: {win32gui.IsWindowVisible(main_hwnd)}")

# 尝试设置前台窗口
try:
    win32gui.SetForegroundWindow(main_hwnd)
except:
    pass

print(f"  主窗口 vis (after): {win32gui.IsWindowVisible(main_hwnd)}")
print(f"  主窗口 title: '{win32gui.GetWindowText(main_hwnd)}'")

# ============================================================
# Step 3: 获取菜单结构
# ============================================================
print("\n[3] 获取菜单结构...")

menu_hwnd = win32gui.GetMenu(main_hwnd)
if menu_hwnd:
    print(f"  菜单句柄: {menu_hwnd}")
    menu_count = win32gui.GetMenuItemCount(menu_hwnd)
    print(f"  顶级菜单项数: {menu_count}")
    
    all_menu_items = []
    
    for i in range(menu_count):
        item_text = win32gui.GetMenuString(menu_hwnd, i, win32con.MF_BYPOSITION)
        submenu = win32gui.GetSubMenu(menu_hwnd, i)
        print(f"\n  菜单 [{i}]: '{item_text}' (submenu={submenu})")
        
        if submenu:
            sub_count = win32gui.GetMenuItemCount(submenu)
            for j in range(sub_count):
                sub_text = win32gui.GetMenuString(submenu, j, win32con.MF_BYPOSITION)
                item_id = win32gui.GetMenuItemID(submenu, j)
                state = win32gui.GetMenuState(submenu, j, win32con.MF_BYPOSITION)
                enabled = not (state & win32con.MF_GRAYED)
                sep = " (分隔符)" if state & win32con.MF_SEPARATOR else ""
                print(f"    [{j}] ID={item_id}, '{sub_text}', enabled={enabled}{sep}")
                
                if item_id >= 0 and item_id < 0xFFFF and sub_text and not sep:
                    all_menu_items.append({
                        'menu': item_text,
                        'item': sub_text,
                        'id': item_id,
                        'enabled': enabled
                    })
                
                # 检查子子菜单
                subsub = win32gui.GetSubMenu(submenu, j)
                if subsub:
                    subsub_count = win32gui.GetMenuItemCount(subsub)
                    for k in range(subsub_count):
                        ss_text = win32gui.GetMenuString(subsub, k, win32con.MF_BYPOSITION)
                        ss_id = win32gui.GetMenuItemID(subsub, k)
                        print(f"      [{k}] ID={ss_id}, '{ss_text}'")
                        if ss_id >= 0 and ss_id < 0xFFFF and ss_text:
                            all_menu_items.append({
                                'menu': f"{item_text} > {sub_text}",
                                'item': ss_text,
                                'id': ss_id,
                                'enabled': True
                            })
    
    # 查找计算相关的菜单项
    print(f"\n  === 计算相关菜单项 ===")
    calc_keywords = ['计算', '求解', 'Calc', 'Solve', 'Run', 'Analysis', 'Steady', 'Thermal', 'EMag', 'FEA', 'Simulation']
    calc_items = []
    for mi in all_menu_items:
        for kw in calc_keywords:
            if kw.lower() in mi['item'].lower() or kw.lower() in mi['menu'].lower():
                calc_items.append(mi)
                print(f"    {mi['menu']} > {mi['item']} (ID={mi['id']}, enabled={mi['enabled']})")
                break
    
    # 保存菜单结构
    with open(r'D:\MotorDesign\Scripts\menu_structure_v2.json', 'w', encoding='utf-8') as f:
        json.dump(all_menu_items, f, ensure_ascii=False, indent=2)
    print(f"\n  菜单结构已保存: {len(all_menu_items)} 项")
    
else:
    print("  *** 无菜单! ***")
    # Delphi 可能使用 TMainMenu 而不是系统菜单
    # 尝试查找 TMainMenu 子窗口
    menus = []
    def find_menu(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            if 'Menu' in cls:
                menus.append({'hwnd': hwnd, 'cls': cls})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_menu, None)
    if menus:
        print(f"  找到 {len(menus)} 个菜单控件:")
        for m in menus:
            print(f"    {m['cls']}: hwnd={m['hwnd']}")

# ============================================================
# Step 4: 测试键盘快捷键
# ============================================================
print("\n[4] 测试键盘快捷键...")

# 确保 Motor-CAD 在前台
try:
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(0.5)
except:
    pass

# 记录 FEResultsData 初始状态
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = {}
if os.path.exists(fe_dir):
    for f in os.listdir(fe_dir):
        fpath = os.path.join(fe_dir, f)
        files_before[f] = os.path.getmtime(fpath)

# 测试的快捷键列表
# (描述, wVk, wScan, dwFlags_for_down)
VK_F9 = 0x78
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_RETURN = 0x0D
VK_S = 0x53
VK_R = 0x52
VK_A = 0x41
VK_T = 0x54

# 使用 PostMessage 发送键盘消息到主窗口
def send_key(vk, ctrl=False, alt=False, shift=False):
    """通过 PostMessage 发送键盘消息"""
    lparam_down = 1 | (win32api.MapVirtualKey(vk, 0) << 16)
    lparam_up = 1 | (win32api.MapVirtualKey(vk, 0) << 16) | (1 << 31)
    
    if ctrl:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0x001D0001)
        time.sleep(0.05)
    if alt:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, win32con.VK_MENU, 0x00380001)
        time.sleep(0.05)
    if shift:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, win32con.VK_SHIFT, 0x002A0001)
        time.sleep(0.05)
    
    win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, vk, lparam_down)
    time.sleep(0.05)
    win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, vk, lparam_up)
    time.sleep(0.05)
    
    if ctrl:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, win32con.VK_CONTROL, 0xC01D0001)
    if alt:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, win32con.VK_MENU, 0xC0380001)
    if shift:
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, win32con.VK_SHIFT, 0xC02A0001)
    time.sleep(0.1)

# 使用 SendInput 发送键盘消息 (需要窗口在前台)
def send_key_input(vk, ctrl=False, alt=False, shift=False):
    """使用 SendInput 发送键盘消息"""
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    
    keys = []
    if ctrl:
        keys.append((win32con.VK_CONTROL, 0))
    if alt:
        keys.append((win32con.VK_MENU, 0))
    if shift:
        keys.append((win32con.VK_SHIFT, 0))
    keys.append((vk, 0))
    
    # Key down
    for k, _ in keys:
        inp = {'type': INPUT_KEYBOARD, 'ki': {'wVk': k, 'wScan': 0, 'dwFlags': 0, 'time': 0, 'dwExtraInfo': 0}}
        ctypes.windll.user32.SendInput(1, ctypes.byref(ctypes.c_int * 0), 0)  # dummy
    
    # 使用 ctypes 直接调用
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort),
                     ("wScan", ctypes.c_ushort),
                     ("dwFlags", ctypes.c_ulong),
                     ("time", ctypes.c_ulong),
                     ("dwExtraInfo", ctypes.c_void_p)]
    
    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("_input",)
        _fields_ = [("type", ctypes.c_ulong), ("_input", _INPUT)]
    
    inputs = []
    if ctrl:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki.wVk = win32con.VK_CONTROL
        inputs.append(inp)
    if alt:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki.wVk = win32con.VK_MENU
        inputs.append(inp)
    if shift:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki.wVk = win32con.VK_SHIFT
        inputs.append(inp)
    
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki.wVk = vk
    inputs.append(inp)
    
    # Key up (reverse order)
    for k in reversed(inputs):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki.wVk = k.ki.wVk
        inp.ki.dwFlags = KEYEVENTF_KEYUP
        inputs.append(inp)
    
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.windll.user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))

# 测试每个快捷键
shortcuts = [
    ("F9", VK_F9, False, False, False),
    ("Ctrl+R", VK_R, True, False, False),
    ("Ctrl+Enter", VK_RETURN, True, False, False),
    ("Ctrl+S", VK_S, True, False, False),
    ("Alt+S", VK_S, False, True, False),
    ("F5", VK_F5, False, False, False),
    ("Ctrl+F9", VK_F9, True, False, False),
    ("F6", VK_F6, False, False, False),
    ("Ctrl+T", VK_T, True, False, False),
    ("Alt+A", VK_A, False, True, False),
]

# 先用 PostMessage 方式测试
for desc, vk, ctrl, alt, shift in shortcuts:
    print(f"\n  测试快捷键: {desc}")
    
    # 检查文件变化前的状态
    files_check1 = {}
    if os.path.exists(fe_dir):
        for f in os.listdir(fe_dir):
            fpath = os.path.join(fe_dir, f)
            files_check1[f] = os.path.getmtime(fpath)
    
    # 发送快捷键 (PostMessage 方式)
    send_key(vk, ctrl, alt, shift)
    time.sleep(2)
    
    # 检查文件变化
    files_check2 = {}
    if os.path.exists(fe_dir):
        for f in os.listdir(fe_dir):
            fpath = os.path.join(fe_dir, f)
            files_check2[f] = os.path.getmtime(fpath)
    
    changed = False
    for f in files_check2:
        if f not in files_check1 or files_check2[f] != files_check1[f]:
            changed = True
            break
    
    new_files = set(files_check2.keys()) - set(files_check1.keys())
    
    if changed or new_files:
        print(f"    *** 检测到文件变化! 新文件={len(new_files)} ***")
        # 这个快捷键有效!
        print(f"    有效快捷键: {desc}")
        break
    else:
        # 检查窗口标题或按钮变化
        title = win32gui.GetWindowText(main_hwnd)
        print(f"    无变化 (title='{title[:40]}')")

# ============================================================
# Step 5: 如果快捷键无效，尝试菜单命令
# ============================================================
if not changed and not new_files:
    print("\n[5] 快捷键无效，尝试菜单命令...")
    
    if menu_hwnd and calc_items:
        for ci in calc_items:
            if ci['enabled']:
                print(f"\n  发送菜单命令: {ci['menu']} > {ci['item']} (ID={ci['id']})")
                
                # 记录前状态
                files_check1 = {}
                if os.path.exists(fe_dir):
                    for f in os.listdir(fe_dir):
                        fpath = os.path.join(fe_dir, f)
                        files_check1[f] = os.path.getmtime(fpath)
                
                # 发送 WM_COMMAND
                win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, ci['id'], 0)
                time.sleep(3)
                
                # 检查文件变化
                files_check2 = {}
                if os.path.exists(fe_dir):
                    for f in os.listdir(fe_dir):
                        fpath = os.path.join(fe_dir, f)
                        files_check2[f] = os.path.getmtime(fpath)
                
                changed = False
                for f in files_check2:
                    if f not in files_check1 or files_check2[f] != files_check1[f]:
                        changed = True
                        break
                
                new_files = set(files_check2.keys()) - set(files_check1.keys())
                
                if changed or new_files:
                    print(f"    *** 检测到文件变化! ***")
                    print(f"    有效菜单命令: {ci['menu']} > {ci['item']}")
                    break
                else:
                    print(f"    无变化")
    else:
        print("  无可用菜单项")

# ============================================================
# Step 6: 检查"输出数据"标签页
# ============================================================
print("\n[6] 检查输出数据标签页...")

# 找 PageControl
pcs = []
def find_pc(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            size = (rect[2]-rect[0]) * (rect[3]-rect[1])
            pcs.append({'hwnd': hwnd, 'size': size, 'rect': rect})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_pc, None)

if pcs:
    pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
    print(f"  PageControl: hwnd={pc_hwnd}")
    
    # 切换到"输出数据"标签 (x=380)
    print("  切换到 '输出数据' 标签...")
    lp = win32api.MAKELONG(380, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(2)
    
    # 读取所有可见文本控件
    texts = []
    def read_text(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title and len(title) > 1:
                rect = win32gui.GetWindowRect(hwnd)
                texts.append({'cls': cls, 'title': title, 'hwnd': hwnd, 'rect': rect})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, read_text, None)
    
    print(f"\n  可见文本控件 ({len(texts)} 个):")
    for t in texts[:50]:
        print(f"    {t['cls']}: '{t['title'][:60]}' @ ({t['rect'][0]},{t['rect'][1]})")
    
    # 查找温度相关数据
    print(f"\n  === 温度相关文本 ===")
    temp_keywords = ['温度', 'Temp', 'temp', '°C', ' C]', ' C)', 'Winding', 'Housing', 'Magnet', 'Tooth']
    for t in texts:
        for kw in temp_keywords:
            if kw in t['title']:
                print(f"    {t['cls']}: '{t['title'][:80]}'")
                break

else:
    print("  未找到 PageControl!")

# ============================================================
# 保存结果
# ============================================================
result = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'main_hwnd': main_hwnd,
    'main_title': win32gui.GetWindowText(main_hwnd),
    'main_visible': win32gui.IsWindowVisible(main_hwnd),
    'menu_count': len(all_menu_items) if menu_hwnd else 0,
    'calc_menu_items': len(calc_items) if menu_hwnd else 0,
    'shortcut_tested': len(shortcuts),
    'shortcut_effective': None,
    'fe_files_before': len(files_before),
    'fe_files_after': len(files_check2) if 'files_check2' in dir() else 0,
}

with open(r'D:\MotorDesign\Scripts\menu_solve_result.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n=== 结果已保存 ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
