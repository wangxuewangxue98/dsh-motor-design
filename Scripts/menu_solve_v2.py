#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v6b — 菜单命令 + 键盘快捷键 (修正版)

不关闭任何对话框，直接操作可见的主窗口。
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
# Step 1: 找到可见的 Motor-CAD 主窗口
# ============================================================
print("\n[1] 查找可见的 Motor-CAD 主窗口...")

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
    print("  *** 未找到可见的 Motor-CAD 主窗口! ***")
    # 尝试查找不可见的
    def find_any(hwnd, p):
        global main_hwnd, mc_pid
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == 'TMainForm' and 'Motor' in title:
                main_hwnd = hwnd
                _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
                print(f"  找到不可见的主窗口: hwnd={hwnd}, vis={win32gui.IsWindowVisible(hwnd)}")
        except:
            pass
        return True
    win32gui.EnumWindows(find_any, None)
    if main_hwnd:
        # 尝试显示
        print(f"  尝试显示窗口...")
        win32gui.ShowWindow(main_hwnd, win32con.SW_SHOWNORMAL)
        time.sleep(1)
        print(f"  vis after SW_SHOWNORMAL: {win32gui.IsWindowVisible(main_hwnd)}")

if not main_hwnd:
    print("  *** 错误: 未找到 Motor-CAD! ***")
    sys.exit(1)

title = win32gui.GetWindowText(main_hwnd)
vis = win32gui.IsWindowVisible(main_hwnd)
print(f"  主窗口: hwnd={main_hwnd}, vis={vis}")
print(f"  标题: '{title}'")

# 设置前台
try:
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(0.5)
except:
    pass

# ============================================================
# Step 2: 获取菜单结构
# ============================================================
print("\n[2] 获取菜单结构...")

# 辅助函数: 用 ctypes 获取菜单字符串
def get_menu_string(menu_hwnd, pos, by_position=True):
    """用 ctypes 调用 GetMenuStringW 获取菜单项文本"""
    MF_BYPOSITION = 0x00000400
    flag = MF_BYPOSITION if by_position else 0
    # 先获取长度
    length = ctypes.windll.user32.GetMenuStringW(menu_hwnd, pos, None, 0, flag)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetMenuStringW(menu_hwnd, pos, buf, length + 1, flag)
    return buf.value

def get_menu_item_id(menu_hwnd, pos, by_position=True):
    """用 ctypes 调用 GetMenuItemID"""
    MF_BYPOSITION = 0x00000400
    flag = MF_BYPOSITION if by_position else 0
    return ctypes.windll.user32.GetMenuItemID(menu_hwnd, pos)

def get_sub_menu(menu_hwnd, pos, by_position=True):
    """用 ctypes 调用 GetSubMenu"""
    MF_BYPOSITION = 0x00000400
    flag = MF_BYPOSITION if by_position else 0
    return ctypes.windll.user32.GetSubMenu(menu_hwnd, pos)

def get_menu_item_count(menu_hwnd):
    """用 ctypes 调用 GetMenuItemCount"""
    return ctypes.windll.user32.GetMenuItemCount(menu_hwnd)

def get_menu_state(menu_hwnd, pos, by_position=True):
    """用 ctypes 调用 GetMenuState"""
    MF_BYPOSITION = 0x00000400
    flag = MF_BYPOSITION if by_position else 0
    return ctypes.windll.user32.GetMenuState(menu_hwnd, pos, flag)

menu_hwnd = win32gui.GetMenu(main_hwnd)
if menu_hwnd:
    print(f"  菜单句柄: {menu_hwnd}")
    menu_count = get_menu_item_count(menu_hwnd)
    print(f"  顶级菜单项数: {menu_count}")
    
    all_menu_items = []
    
    for i in range(menu_count):
        item_text = get_menu_string(menu_hwnd, i)
        submenu = get_sub_menu(menu_hwnd, i)
        print(f"\n  菜单 [{i}]: '{item_text}' (submenu={submenu})")
        
        if submenu:
            sub_count = get_menu_item_count(submenu)
            for j in range(sub_count):
                sub_text = get_menu_string(submenu, j)
                item_id = get_menu_item_id(submenu, j)
                state = get_menu_state(submenu, j)
                enabled = not (state & win32con.MF_GRAYED)
                sep = " (分隔符)" if state & win32con.MF_SEPARATOR else ""
                if sub_text or sep:
                    print(f"    [{j}] ID={item_id}, '{sub_text}', enabled={enabled}{sep}")
                
                if item_id >= 0 and item_id < 0xFFFF and sub_text and not sep:
                    all_menu_items.append({
                        'menu': item_text,
                        'item': sub_text,
                        'id': item_id,
                        'enabled': enabled
                    })
                
                # 检查子子菜单
                subsub = get_sub_menu(submenu, j)
                if subsub:
                    subsub_count = get_menu_item_count(subsub)
                    for k in range(subsub_count):
                        ss_text = get_menu_string(subsub, k)
                        ss_id = get_menu_item_id(subsub, k)
                        if ss_text:
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
    calc_keywords = ['计算', '求解', 'Calc', 'Solve', 'Run', 'Analysis', 'Steady', 'Thermal', 'EMag', 'FEA', 'Simulation', '计算', 'Steady State']
    calc_items = []
    for mi in all_menu_items:
        for kw in calc_keywords:
            if kw.lower() in mi['item'].lower() or kw.lower() in mi['menu'].lower():
                calc_items.append(mi)
                print(f"    {mi['menu']} > {mi['item']} (ID={mi['id']}, enabled={mi['enabled']})")
                break
    
    # 保存完整菜单结构
    with open(r'D:\MotorDesign\Scripts\menu_structure_v3.json', 'w', encoding='utf-8') as f:
        json.dump(all_menu_items, f, ensure_ascii=False, indent=2)
    print(f"\n  菜单结构已保存: {len(all_menu_items)} 项")
    
else:
    print("  *** 主窗口无系统菜单! ***")
    all_menu_items = []
    calc_items = []

# ============================================================
# Step 3: 记录 FEResultsData 初始状态
# ============================================================
print("\n[3] 记录初始状态...")

fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = {}
if os.path.exists(fe_dir):
    for f in os.listdir(fe_dir):
        fpath = os.path.join(fe_dir, f)
        files_before[f] = {
            'size': os.path.getsize(fpath),
            'mtime': os.path.getmtime(fpath)
        }
print(f"  FEResultsData 文件数: {len(files_before)}")

def check_fe_change(before):
    """检查 FEResultsData 是否有变化"""
    if not os.path.exists(fe_dir):
        return False, set()
    new_files = set()
    for f in os.listdir(fe_dir):
        fpath = os.path.join(fe_dir, f)
        try:
            current_mtime = os.path.getmtime(fpath)
            current_size = os.path.getsize(fpath)
            if f not in before:
                new_files.add(f)
            elif before[f]['mtime'] != current_mtime or before[f]['size'] != current_size:
                new_files.add(f)
        except:
            pass
    return len(new_files) > 0, new_files

# ============================================================
# Step 4: 测试菜单命令触发计算
# ============================================================
print("\n[4] 测试菜单命令触发计算...")

effective_cmd = None

if calc_items:
    for ci in calc_items:
        if not ci['enabled']:
            continue
        print(f"\n  发送菜单命令: {ci['menu']} > {ci['item']} (ID={ci['id']})")
        
        # 记录前状态
        time.sleep(0.5)
        
        # 发送 WM_COMMAND
        win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, ci['id'], 0)
        time.sleep(3)
        
        # 检查文件变化
        changed, new_files = check_fe_change(files_before)
        
        if changed:
            print(f"    *** 检测到文件变化! 新/修改文件: {new_files} ***")
            effective_cmd = f"{ci['menu']} > {ci['item']}"
            break
        else:
            # 检查窗口标题或按钮变化
            title_now = win32gui.GetWindowText(main_hwnd)
            print(f"    无文件变化 (title='{title_now[:50]}')")
            
            # 检查是否有进度窗口出现
            progress_found = [False]
            def check_progress(hwnd, p):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    title_p = win32gui.GetWindowText(hwnd)
                    vis_p = win32gui.IsWindowVisible(hwnd)
                    if vis_p and ('Progress' in title_p or 'progress' in cls.lower() or 'Transient' in title_p):
                        progress_found[0] = True
                        print(f"    发现进度窗口: {cls} '{title_p}'")
                except:
                    pass
                return True
            win32gui.EnumWindows(check_progress, None)
            
            if progress_found[0]:
                print(f"    *** 发现进度窗口! 计算可能已触发 ***")
                effective_cmd = f"{ci['menu']} > {ci['item']}"
                break
else:
    print("  无计算相关菜单项")

# ============================================================
# Step 5: 测试键盘快捷键
# ============================================================
if not effective_cmd:
    print("\n[5] 菜单命令无效，测试键盘快捷键...")
    
    # 确保前台
    try:
        win32gui.SetForegroundWindow(main_hwnd)
        time.sleep(0.5)
    except:
        pass
    
    VK_F9 = 0x78
    VK_F5 = 0x74
    VK_RETURN = 0x0D
    VK_S = 0x53
    VK_R = 0x52
    VK_T = 0x54
    VK_A = 0x41
    
    shortcuts = [
        ("F9", VK_F9, False, False, False),
        ("Ctrl+R", VK_R, True, False, False),
        ("Ctrl+Enter", VK_RETURN, True, False, False),
        ("Ctrl+S", VK_S, True, False, False),
        ("F5", VK_F5, False, False, False),
        ("Ctrl+F9", VK_F9, True, False, False),
    ]
    
    for desc, vk, ctrl, alt, shift in shortcuts:
        print(f"\n  测试快捷键: {desc}")
        
        # 用 PostMessage 发送
        lparam_down = 1 | (win32api.MapVirtualKey(vk, 0) << 16)
        lparam_up = lparam_down | (1 << 31)
        
        if ctrl:
            win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0x001D0001)
            time.sleep(0.05)
        
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYDOWN, vk, lparam_down)
        time.sleep(0.05)
        win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, vk, lparam_up)
        time.sleep(0.05)
        
        if ctrl:
            win32gui.PostMessage(main_hwnd, win32con.WM_KEYUP, win32con.VK_CONTROL, 0xC01D0001)
        
        time.sleep(3)
        
        # 检查文件变化
        changed, new_files = check_fe_change(files_before)
        
        if changed:
            print(f"    *** 检测到文件变化! ***")
            effective_cmd = f"快捷键 {desc}"
            break
        else:
            # 检查进度窗口
            progress_found = [False]
            def check_progress2(hwnd, p):
                try:
                    title_p = win32gui.GetWindowText(hwnd)
                    vis_p = win32gui.IsWindowVisible(hwnd)
                    if vis_p and ('Progress' in title_p or 'Transient' in title_p):
                        progress_found[0] = True
                except:
                    pass
                return True
            win32gui.EnumWindows(check_progress2, None)
            
            if progress_found[0]:
                print(f"    *** 发现进度窗口! ***")
                effective_cmd = f"快捷键 {desc}"
                break
            else:
                print(f"    无变化")

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
            title_t = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title_t and len(title_t) > 0:
                rect = win32gui.GetWindowRect(hwnd)
                texts.append({'cls': cls, 'title': title_t, 'hwnd': hwnd, 'rect': rect})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, read_text, None)
    
    print(f"\n  可见文本控件 ({len(texts)} 个):")
    for t in texts[:80]:
        print(f"    {t['cls']}: '{t['title'][:70]}' @ ({t['rect'][0]},{t['rect'][1]})")
    
    # 查找温度相关数据
    print(f"\n  === 温度/结果相关文本 ===")
    temp_keywords = ['Temp', 'temp', '°C', 'Winding', 'Housing', 'Magnet', 'Tooth', 'Stator', 'Rotor', 'Loss', 'Speed', 'Torque', 'Power', 'Efficiency', 'Node_Temp', 'T_', 'T[', 'Average']
    for t in texts:
        for kw in temp_keywords:
            if kw in t['title']:
                print(f"    {t['cls']}: '{t['title'][:90]}'")
                break
else:
    print("  未找到 PageControl!")

# ============================================================
# Step 7: 尝试发送所有 WM_COMMAND ID (暴力扫描)
# ============================================================
if not effective_cmd and all_menu_items:
    print("\n[7] 暴力扫描所有菜单命令 ID...")
    print("  (尝试所有非计算相关的菜单项，看是否有隐藏的计算命令)")
    
    # 获取所有唯一的菜单 ID
    all_ids = list(set(mi['id'] for mi in all_menu_items if mi['enabled']))
    print(f"  共 {len(all_ids)} 个启用的菜单 ID")
    
    # 只测试 ID 在合理范围内的
    for mi in all_menu_items:
        if not mi['enabled']:
            continue
        # 跳过已经测试过的计算相关项
        if mi in calc_items:
            continue
        
        # 只测试可能和计算相关的（包含数字或者特定关键词的）
        # 实际上这里我们测试所有未测试的菜单项
        
    # 不要暴力扫描所有菜单项，太危险
    # 只打印总结
    print(f"  (跳过暴力扫描，太危险)")

# ============================================================
# 保存结果
# ============================================================
result = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'main_hwnd': main_hwnd,
    'main_title': win32gui.GetWindowText(main_hwnd),
    'main_visible': win32gui.IsWindowVisible(main_hwnd),
    'menu_items_count': len(all_menu_items),
    'calc_menu_items': len(calc_items),
    'effective_command': effective_cmd,
    'fe_files_before': len(files_before),
}

with open(r'D:\MotorDesign\Scripts\menu_solve_result_v2.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n{'='*70}")
print(f"结果:")
print(json.dumps(result, ensure_ascii=False, indent=2))
print(f"{'='*70}")
