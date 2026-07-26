#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 直接操作:
1. 用 PostMessage 发送鼠标点击到 PageControl 标签栏（不依赖 TCM 消息）
2. 测试键盘快捷键触发计算
3. 监控 FEResultsData 目录变化
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
print("Motor-CAD 直接操作 - 键盘+鼠标")
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

if not main_hwnd:
    print("  未找到 Motor-CAD!")
    sys.exit(1)

print(f"  主窗体: hwnd={main_hwnd}")

# Get Motor-CAD PID
_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
print(f"  PID: {mc_pid}")

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
        'hwnd': int(hwnd),
        'class': cls_name,
        'title': title,
        'visible': visible,
        'ctrl_id': ctrl_id,
        'rect': list(rect),
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect, None)

# Find the main PageControl
page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
main_pc = max(page_controls, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
pc_left, pc_top, pc_right, pc_bottom = pc_rect
pc_width = pc_right - pc_left
pc_height = pc_bottom - pc_top

print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}, size={pc_width}x{pc_height}")

# Find all TTabSheet to know tab names
all_tabs = [c for c in all_children if c['class'] == 'TTabSheet']
print(f"  标签页总数: {len(all_tabs)}")

# The top-level tabs (direct children of the PageControl area)
# These should be the ones with the same rect as the PageControl content area
top_level_tabs = []
for t in all_tabs:
    # Top-level tabs have their rect close to the PageControl's content area
    # (slightly inside the PageControl)
    t_rect = t['rect']
    if abs(t_rect[0] - pc_left) < 10 and abs(t_rect[1] - pc_top) < 30:
        top_level_tabs.append(t)

print(f"  顶级标签页: {len(top_level_tabs)} 个")
for i, t in enumerate(top_level_tabs):
    vis = "可见" if t['visible'] else "隐藏"
    print(f"    [{i}] [{vis}] '{t['title']}'")

# Function to get visible buttons
def get_visible_buttons():
    buttons = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            enabled = win32gui.IsWindowEnabled(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            buttons.append({
                'title': title,
                'hwnd': int(hwnd),
                'ctrl_id': ctrl_id,
                'enabled': enabled,
                'rect': list(rect),
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

# Function to get visible tabs
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

# Function to click at a position using PostMessage
def post_click(hwnd, x, y):
    """Send mouse click via PostMessage (works even if window is not in foreground)"""
    lparam = win32api.MAKELONG(x, y)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

# Function to send a key
def send_key(key_code, modifier=None):
    """Send keyboard shortcut via PostMessage"""
    if modifier:
        win32api.keybd_event(modifier, 0, 0, 0)
        time.sleep(0.05)
    win32api.keybd_event(key_code, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(key_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    if modifier:
        time.sleep(0.05)
        win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.5)

# Step 1: Try to navigate tabs by clicking on the tab bar
# The tab bar is at the top of the PageControl
# For 9 tabs, each about 112px wide
print("\n" + "=" * 50)
print("[1] 扫描标签栏")
print("=" * 50)

# Calculate tab positions
# Tab bar height is typically about 25 pixels
tab_y = 12  # Relative to PageControl top
tab_bar_height = 25

# The 9 tabs: we know their names from the hidden tab list
# Let's try clicking at different positions
tab_names_found = {}

# Try clicking at each approximate tab position
num_tabs = 9  # From TCM_GETITEMCOUNT
tab_width = pc_width // num_tabs

for i in range(num_tabs):
    # Calculate click position (relative to PageControl)
    rel_x = tab_width // 2 + i * tab_width
    rel_y = tab_y
    
    # Also try with actual mouse
    screen_x = pc_left + rel_x
    screen_y = pc_top + rel_y
    
    print(f"\n  标签 [{i}]: 点击 ({screen_x}, {screen_y}) [相对 ({rel_x}, {rel_y})]")
    
    # Use actual mouse click (more reliable for Delphi)
    try:
        win32api.SetCursorPos((screen_x, screen_y))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)
        time.sleep(0.8)
    except:
        pass
    
    # Check what's visible
    vis_tabs = get_visible_tabs()
    vis_buttons = get_visible_buttons()
    
    print(f"    可见标签: {vis_tabs}")
    if vis_buttons:
        for b in vis_buttons:
            en = "启用" if b['enabled'] else "禁用"
            print(f"    按钮: [{en}] '{b['title']}' id={b['ctrl_id']} hwnd={b['hwnd']} rect={b['rect']}")
    
    tab_names_found[i] = vis_tabs
    
    # Check for calculation-related tabs
    calc_tabs = [t for t in vis_tabs if any(kw in t.lower() for kw in ['lab', '实验室', 'calc', '计算', 'electromagnetic', 'thermal', 'output', '输出'])]
    if calc_tabs:
        print(f"    >>> 发现关键标签: {calc_tabs}")

# Step 2: Navigate to the Lab tab specifically and look for buttons
print("\n\n" + "=" * 50)
print("[2] 深入扫描 Lab/计算 标签页")
print("=" * 50)

# Based on the scan, find which tab index has the Lab
# Let's try each tab and if we see "电磁热虚拟实验室", look deeper
for tab_idx, vis_tabs in tab_names_found.items():
    if any('lab' in t.lower() or '实验室' in t or '计算' in t for t in vis_tabs):
        print(f"\n  标签 [{tab_idx}] 包含: {vis_tabs}")
        
        # Click on this tab again
        screen_x = pc_left + tab_width // 2 + tab_idx * tab_width
        screen_y = pc_top + tab_y
        win32api.SetCursorPos((screen_x, screen_y))
        time.sleep(0.1)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)
        time.sleep(1)
        
        # Look for sub-tabs (nested PageControl)
        sub_children = []
        def collect_sub(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            sub_children.append({
                'hwnd': int(hwnd),
                'class': cls_name,
                'title': title,
                'visible': visible,
                'ctrl_id': ctrl_id,
                'rect': list(rect),
            })
            return True
        
        win32gui.EnumChildWindows(main_hwnd, collect_sub, None)
        
        # Find visible sub PageControls
        sub_pcs = [c for c in sub_children if c['class'] == 'TPageControl' and c['visible'] and c['hwnd'] != pc_hwnd]
        print(f"    子 PageControl: {len(sub_pcs)} 个")
        for spc in sub_pcs:
            spc_rect = spc['rect']
            spc_hwnd = spc['hwnd']
            spc_width = spc_rect[2] - spc_rect[0]
            print(f"      hwnd={spc_hwnd} rect={spc_rect} width={spc_width}")
            
            # Find visible sub-tabs
            vis_sub_tabs = [c for c in sub_children if c['class'] == 'TTabSheet' and c['visible'] and c['title']]
            print(f"      可见子标签: {[t['title'] for t in vis_sub_tabs]}")
            
            # Try clicking on each sub-tab
            # Get all sub-tabs (visible and hidden) that are children of this sub PageControl
            # We need to estimate positions
            all_sub_tabs = [c for c in sub_children if c['class'] == 'TTabSheet']
            sub_tab_count = len([t for t in all_sub_tabs if abs(t['rect'][0] - spc_rect[0]) < 10])
            
            if sub_tab_count > 0:
                sub_tab_width = spc_width // max(sub_tab_count, 1)
                print(f"      子标签数: {sub_tab_count}, 每个宽度: {sub_tab_width}")
                
                for si in range(min(sub_tab_count, 15)):
                    sub_x = spc_rect[0] + sub_tab_width // 2 + si * sub_tab_width
                    sub_y = spc_rect[1] + 12
                    
                    win32api.SetCursorPos((sub_x, sub_y))
                    time.sleep(0.1)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, sub_x, sub_y, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, sub_x, sub_y, 0, 0)
                    time.sleep(0.8)
                    
                    vis_sub = get_visible_tabs()
                    vis_btns = get_visible_buttons()
                    
                    print(f"        子标签[{si}]: {vis_sub}")
                    if vis_btns:
                        for b in vis_btns:
                            en = "启用" if b['enabled'] else "禁用"
                            print(f"        按钮: [{en}] '{b['title']}' id={b['ctrl_id']} rect={b['rect']}")
                    
                    # Check if this is a calculation sub-tab
                    if vis_btns:
                        calc_kw = ['calc', 'run', 'solve', 'analy', 'start', '计算', '运行', '求解', '分析', '开始', '执行']
                        for b in vis_btns:
                            if any(kw.lower() in b['title'].lower() for kw in calc_kw):
                                print(f"        *** 计算按钮: '{b['title']}' ***")

# Step 3: Try keyboard shortcuts to trigger calculation
print("\n\n" + "=" * 50)
print("[3] 键盘快捷键测试")
print("=" * 50)

# Make sure Motor-CAD is focused
try:
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    time.sleep(0.5)
    ctypes.windll.user32.SetForegroundWindow(main_hwnd)
    time.sleep(1)
except:
    pass

# FEResultsData directory to monitor
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"

shortcuts = [
    ('F5', win32con.VK_F5, None, '可能: 运行计算'),
    ('F6', win32con.VK_F6, None, '可能: 热计算'),
    ('F7', win32con.VK_F7, None, '可能: 磁计算'),
    ('F9', win32con.VK_F9, None, '可能: 计算'),
    ('Ctrl+Enter', win32con.VK_RETURN, win32con.VK_CONTROL, '可能: 确定/运行'),
    ('Ctrl+R', ord('R'), win32con.VK_CONTROL, '可能: 运行'),
    ('Ctrl+E', ord('E'), win32con.VK_CONTROL, '可能: 电磁计算'),
    ('Ctrl+T', ord('T'), win32con.VK_CONTROL, '可能: 热计算'),
    ('Ctrl+L', ord('L'), win32con.VK_CONTROL, '可能: Lab计算'),
]

for name, key, mod, desc in shortcuts:
    files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    
    print(f"\n  {name} ({desc})...")
    try:
        send_key(key, mod)
    except:
        pass
    
    time.sleep(3)
    
    files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_after - files_before
    
    if new_files:
        print(f"    *** 新文件: {new_files} ***")
    else:
        print(f"    无变化")

# Step 4: Check if any dialog/message box appeared
print("\n\n" + "=" * 50)
print("[4] 检查弹窗")
print("=" * 50)

dialogs = []
def find_dialogs(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    if visible and title:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and cls_name not in ('TMainForm', 'TApplication'):
            dialogs.append({
                'hwnd': int(hwnd),
                'class': cls_name,
                'title': title,
            })
            print(f"  弹窗: '{title}' ({cls_name}) hwnd={hwnd}")
    return True

win32gui.EnumWindows(find_dialogs, None)

if not dialogs:
    print("  无弹窗")

# Step 5: Read status bar
print("\n\n" + "=" * 50)
print("[5] 状态栏")
print("=" * 50)

status_bars = [c for c in all_children if c['class'] == 'TStatusBar']
for sb in status_bars:
    text = win32gui.GetWindowText(sb['hwnd'])
    print(f"  状态栏: '{text}'")
    
    # Try to get status bar parts
    # SB_GETPARTS = WM_USER+3
    parts = win32gui.SendMessage(sb['hwnd'], win32con.WM_USER + 3, 0, 0)
    print(f"  部分数: {parts}")
    
    # Try to get text from each part
    for i in range(min(parts, 5)):
        # SB_GETTEXTLENGTH = WM_USER+3, SB_GETTEXT = WM_USER+2
        length = win32gui.SendMessage(sb['hwnd'], win32con.WM_USER + 3, i, 0)
        if length > 0:
            buf = win32gui.PyGetMemory(length + 2)
            win32gui.SendMessage(sb['hwnd'], win32con.WM_USER + 2, i, buf)
            try:
                text = bytes(buf).decode('utf-8', errors='replace').rstrip('\x00')
                print(f"    部分[{i}]: '{text}'")
            except:
                pass

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
