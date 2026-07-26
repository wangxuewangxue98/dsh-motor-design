#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v3 — 多策略点击 + 真实物理鼠标

策略:
  A. 关闭多余 Motor-CAD 实例 (避免干扰)
  B. AttachThreadInput + SetForegroundWindow (可靠前台切换)
  C. SendInput 真实物理鼠标点击 (模拟人手操作)
  D. WM_COMMAND + BN_CLICKED 通知 (Delphi VCL 按钮标准点击方式)
  E. 脚本标签页 Run 按钮 (替代计算触发路径)
  F. FEResultsData 目录监控 + .mot 文件修改时间检查
"""
import sys
import os
import time
import json
import ctypes
import struct
import win32gui
import win32con
import win32api
import win32process
import win32clipboard

# ---- SendInput 相关的 ctypes 结构 ----
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", INPUT_UNION),
    ]

def send_input_click(screen_x, screen_y):
    """使用 SendInput 进行真实物理鼠标点击"""
    # 移动鼠标到目标位置
    ctypes.windll.user32.SetCursorPos(screen_x, screen_y)
    time.sleep(0.05)
    
    # 按下左键
    inp_down = INPUT()
    inp_down.type = INPUT_MOUSE
    inp_down.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=0,
        dwFlags=MOUSEEVENTF_LEFTDOWN,
        time=0, dwExtraInfo=None
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(inp_down))
    time.sleep(0.05)
    
    # 释放左键
    inp_up = INPUT()
    inp_up.type = INPUT_MOUSE
    inp_up.mi = MOUSEINPUT(
        dx=0, dy=0, mouseData=0,
        dwFlags=MOUSEEVENTF_LEFTUP,
        time=0, dwExtraInfo=None
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(inp_up))
    time.sleep(0.1)

def force_set_foreground(hwnd):
    """强制将窗口置于前台 (使用 AttachThreadInput 技巧)"""
    try:
        # 获取当前前台窗口和线程
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd == 0:
            fg_hwnd = hwnd
        fg_tid, fg_pid = win32process.GetWindowThreadProcessId(fg_hwnd)
        target_tid, target_pid = win32process.GetWindowThreadProcessId(hwnd)
        current_tid = win32api.GetCurrentThreadId()
        
        if fg_tid == 0 or target_tid == 0 or current_tid == 0:
            # 线程ID无效，直接尝试 SetForegroundWindow
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
            time.sleep(0.3)
            return win32gui.GetForegroundWindow() == hwnd
        
        # Attach 到前台线程的输入队列
        if fg_tid != target_tid:
            try:
                win32process.AttachThreadInput(current_tid, fg_tid, True)
            except:
                pass
            try:
                win32process.AttachThreadInput(current_tid, target_tid, True)
            except:
                pass
        
        # 显示并恢复窗口
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        
        # 尝试置顶
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        
        # 解除 Attach
        if fg_tid != target_tid:
            time.sleep(0.1)
            try:
                win32process.AttachThreadInput(current_tid, target_tid, False)
            except:
                pass
            try:
                win32process.AttachThreadInput(current_tid, fg_tid, False)
            except:
                pass
        
        time.sleep(0.3)
    except Exception as e:
        print(f"  force_set_foreground 异常: {e}")
        # 退化方案：直接用 keybd_event Alt 键释放锁
        try:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
            ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except:
            pass
    
    return win32gui.GetForegroundWindow() == hwnd

def send_wm_command(parent_hwnd, ctrl_id, btn_hwnd):
    """发送 WM_COMMAND + BN_CLICKED 通知到父窗口"""
    # BN_CLICKED = 0
    wparam = win32api.MAKELONG(ctrl_id, 0)  # LOWORD=ctrl_id, HIWORD=BN_CLICKED
    lparam = btn_hwnd
    # SendMessage 方式
    win32gui.SendMessage(parent_hwnd, win32con.WM_COMMAND, wparam, lparam)

def post_wm_command(parent_hwnd, ctrl_id, btn_hwnd):
    """PostMessage 方式发送 WM_COMMAND"""
    wparam = win32api.MAKELONG(ctrl_id, 0)
    lparam = btn_hwnd
    win32gui.PostMessage(parent_hwnd, win32con.WM_COMMAND, wparam, lparam)

# ============================================================
# 通用工具函数
# ============================================================

def find_motorcad_windows():
    """找到所有 Motor-CAD 主窗口"""
    windows = []
    def callback(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls == 'TMainForm' and 'Motor-CAD' in title and win32gui.IsWindowVisible(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            windows.append({'hwnd': int(hwnd), 'title': title, 'pid': pid})
        return True
    win32gui.EnumWindows(callback, None)
    return windows

def get_all_children(parent_hwnd):
    """获取所有子控件 (非递归)"""
    results = []
    def collect(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            ctrl_id = 0
        rect = win32gui.GetWindowRect(hwnd)
        results.append({
            'hwnd': int(hwnd), 'class': cls, 'title': title,
            'visible': visible, 'enabled': enabled, 'ctrl_id': ctrl_id,
            'rect': list(rect),
        })
        return True
    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return results

def get_visible_buttons(main_hwnd):
    """获取所有可见按钮"""
    buttons = []
    def check(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and \
           win32gui.IsWindowVisible(hwnd) and title:
            enabled = win32gui.IsWindowEnabled(hwnd)
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            buttons.append({
                'title': title, 'hwnd': int(hwnd), 'ctrl_id': ctrl_id,
                'enabled': enabled, 'rect': list(rect), 'class': cls,
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

def get_visible_tabsheets(main_hwnd):
    tabs = []
    def check(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls == 'TTabSheet' and win32gui.IsWindowVisible(hwnd) and title:
            rect = win32gui.GetWindowRect(hwnd)
            tabs.append({'title': title, 'hwnd': int(hwnd), 'rect': list(rect)})
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs

def get_pagecontrols(main_hwnd):
    pcs = []
    def check(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            pcs.append({'hwnd': int(hwnd), 'rect': list(rect), 'ctrl_id': ctrl_id})
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return pcs

def click_tab_x(pc_hwnd, x_pos, y_pos=12):
    """通过 PostMessage 点击 PageControl 标签栏"""
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

def close_dialogs(mc_pid):
    """关闭所有弹窗"""
    closed = []
    def find_and_close(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and visible and cls in ('TMessageForm', 'TForm', 'TDialog'):
            title = win32gui.GetWindowText(hwnd)
            if title and 'Motor-CAD' not in title:
                def get_text(child_hwnd, lparam):
                    child_cls = win32gui.GetClassName(child_hwnd)
                    child_title = win32gui.GetWindowText(child_hwnd)
                    if child_title:
                        closed.append(f"{child_cls}: '{child_title[:200]}'")
                    return True
                win32gui.EnumChildWindows(hwnd, get_text, None)
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    win32gui.EnumWindows(find_and_close, None)
    return closed

def get_status_bar_text(main_hwnd):
    texts = []
    def check(hwnd, lparam):
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TStatusBar':
            text = win32gui.GetWindowText(hwnd)
            if text and text.strip():
                texts.append(text)
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return texts

def find_button_parent(main_hwnd, btn_hwnd):
    """找到按钮的直接父窗口"""
    parent = win32gui.GetParent(btn_hwnd)
    return parent

# ============================================================
# 主流程
# ============================================================

print("=" * 70)
print("Motor-CAD 自动解算 v3 — 多策略点击")
print("=" * 70)

# Step 0: 找到并清理 Motor-CAD 实例
print("\n[0] 查找 Motor-CAD 实例...")
mc_windows = find_motorcad_windows()

if not mc_windows:
    print("  错误: 未找到 Motor-CAD 窗口! 请先启动 Motor-CAD")
    sys.exit(1)

for w in mc_windows:
    print(f"  实例: hwnd={w['hwnd']}, pid={w['pid']}, title='{w['title']}'")

# 如果有多个实例，关闭多余的
if len(mc_windows) > 1:
    print(f"\n  发现 {len(mc_windows)} 个实例，关闭多余的...")
    # 保留第一个，关闭其他
    import subprocess
    for w in mc_windows[1:]:
        print(f"  关闭: pid={w['pid']}, hwnd={w['hwnd']}")
        # 先尝试 WM_CLOSE
        win32gui.PostMessage(w['hwnd'], win32con.WM_CLOSE, 0, 0)
        time.sleep(2)
    
    # 重新查找
    mc_windows = find_motorcad_windows()
    
    # 如果还有多个，用 taskkill 强制关闭多余的
    if len(mc_windows) > 1:
        print(f"  WM_CLOSE 无效，使用 taskkill 强制关闭...")
        for w in mc_windows[1:]:
            print(f"  taskkill: pid={w['pid']}")
            subprocess.run(['taskkill', '/PID', str(w['pid']), '/F'], 
                         capture_output=True, text=True)
            time.sleep(2)
        mc_windows = find_motorcad_windows()
    
    if not mc_windows:
        print("  错误: 关闭后无实例剩余!")
        sys.exit(1)

main_hwnd = mc_windows[0]['hwnd']
_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
print(f"\n  使用实例: hwnd={main_hwnd}, pid={mc_pid}")

# Step 1: 将 Motor-CAD 置于前台
print("\n[1] 将 Motor-CAD 置于前台...")
success = force_set_foreground(main_hwnd)
print(f"  SetForegroundWindow: {'成功' if success else '失败'}")
if not success:
    # 再试一次
    time.sleep(1)
    success = force_set_foreground(main_hwnd)
    print(f"  重试 SetForegroundWindow: {'成功' if success else '失败'}")

# Step 2: 找到主 PageControl
print("\n[2] 查找 PageControl...")
pcs = get_pagecontrols(main_hwnd)
if not pcs:
    print("  错误: 未找到 TPageControl!")
    sys.exit(1)

# 选最大的
main_pc = max(pcs, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  主 PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Step 3: 扫描标签页，定位求解按钮
print("\n[3] 扫描标签页定位求解按钮...")

close_dialogs(mc_pid)
time.sleep(0.5)

# 先切到 "求解设置" 标签 (x=200-280)
# 使用真实鼠标点击标签 (而非 PostMessage) 以确保前台状态
print("  切换到 '求解设置' 标签...")

# 计算 '求解设置' 标签的屏幕坐标
pc_screen_x = pc_rect[0]
pc_screen_y = pc_rect[1]
# 标签在 PageControl 顶部约 y=12 的位置
tab_screen_y = pc_screen_y + 12

# x=240 大约是 '求解设置' 标签位置
tab_x = pc_screen_x + 240
tab_y = tab_screen_y

# 真实鼠标点击标签
print(f"  物理点击标签: screen=({tab_x}, {tab_y})")
send_input_click(tab_x, tab_y)
time.sleep(1)

# 检查当前标签
vis_tabs = get_visible_tabsheets(main_hwnd)
current_tab = vis_tabs[0]['title'] if vis_tabs else "?"
print(f"  当前标签: '{current_tab}'")

# 查找求解按钮
vis_btns = get_visible_buttons(main_hwnd)
print(f"  可见按钮 ({len(vis_btns)} 个):")
for b in vis_btns:
    print(f"    '{b['title']}' hwnd={b['hwnd']} id={b['ctrl_id']} enabled={b['enabled']} rect={b['rect']} class={b['class']}")

solve_thermal_btn = None
solve_em_btn = None

for b in vis_btns:
    if '求解热' in b['title'] or ('Solve' in b['title'] and 'Thermal' in b['title']):
        solve_thermal_btn = b
    if '求解电磁' in b['title'] or ('Solve' in b['title'] and 'Electro' in b['title']):
        solve_em_btn = b

# 如果 '求解热模型' 不在当前标签，扫描所有标签
if not solve_thermal_btn:
    print("\n  当前标签未找到求解按钮，扫描所有标签...")
    for x in range(0, 1000, 10):
        click_tab_x(pc_hwnd, x)
        vis_btns = get_visible_buttons(main_hwnd)
        for b in vis_btns:
            if '求解热' in b['title']:
                solve_thermal_btn = b
                vis_tabs = get_visible_tabsheets(main_hwnd)
                ct = vis_tabs[0]['title'] if vis_tabs else "?"
                print(f"    在 x={x} (标签='{ct}') 找到 '{b['title']}'")
            if '求解电磁' in b['title']:
                solve_em_btn = b
                vis_tabs = get_visible_tabsheets(main_hwnd)
                ct = vis_tabs[0]['title'] if vis_tabs else "?"
                print(f"    在 x={x} (标签='{ct}') 找到 '{b['title']}'")

# 也检查子标签页中的 '求解电磁模型'
if not solve_em_btn:
    print("\n  检查子标签页中的电磁求解按钮...")
    # 切到 '输入数据' 标签
    for x in range(100, 200, 10):
        click_tab_x(pc_hwnd, x)
        vis_tabs = get_visible_tabsheets(main_hwnd)
        if vis_tabs and '输入数据' in vis_tabs[0]['title']:
            # 找子 PageControl
            all_children = get_all_children(main_hwnd)
            sub_pcs = [c for c in all_children if c['class'] == 'TPageControl' and c['visible'] and c['hwnd'] != pc_hwnd]
            for spc in sub_pcs:
                w = spc['rect'][2] - spc['rect'][0]
                print(f"    子 PageControl: hwnd={spc['hwnd']}, rect={spc['rect']}, width={w}")
                for sx in range(0, min(w, 500), 5):
                    click_tab_x(spc['hwnd'], sx)
                    vis_btns = get_visible_buttons(main_hwnd)
                    for b in vis_btns:
                        if '求解电磁' in b['title'] or 'Solve' in b['title']:
                            vis_tabs2 = get_visible_tabsheets(main_hwnd)
                            ct2 = vis_tabs2[0]['title'] if vis_tabs2 else "?"
                            print(f"      子标签 x={sx} ('{ct2}'): '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
                            if '电磁' in b['title'] or 'Electro' in b['title']:
                                solve_em_btn = b
            break

# Step 4: 检查 '脚本' 标签页
print("\n[4] 检查 '脚本' 标签页...")
click_tab_x(pc_hwnd, 540)  # 脚本标签 x≈520-570
time.sleep(0.5)
vis_tabs = get_visible_tabsheets(main_hwnd)
ct = vis_tabs[0]['title'] if vis_tabs else "?"
print(f"  当前标签: '{ct}'")
vis_btns = get_visible_buttons(main_hwnd)
script_run_btn = None
for b in vis_btns:
    print(f"    '{b['title']}' hwnd={b['hwnd']} id={b['ctrl_id']} enabled={b['enabled']} rect={b['rect']}")
    if b['title'] == 'Run':
        script_run_btn = b

# 检查脚本标签页是否有文本框/编辑器控件
all_children = get_all_children(main_hwnd)
edit_controls = [c for c in all_children if c['class'] in ('TMemo', 'TRichEdit', 'TSynEdit', 'TEdit') and c['visible']]
if edit_controls:
    print(f"\n  脚本标签页内的编辑控件 ({len(edit_controls)} 个):")
    for ec in edit_controls:
        print(f"    {ec['class']}: hwnd={ec['hwnd']} rect={ec['rect']} title='{ec['title'][:50]}'")

# ============================================================
# Step 5: 尝试触发电磁计算
# ============================================================

fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
mot_file = r"D:\Motor-CAD\11_1_5\Matlab_Test.mot"

# 记录计算前的状态
files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
mot_mtime_before = os.path.getmtime(mot_file) if os.path.exists(mot_file) else 0

print(f"\n[5] 计算前状态:")
print(f"  FEResultsData 文件数: {len(files_before)}")
print(f"  .mot 文件修改时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mot_mtime_before))}")

# 策略 A: 如果找到 '求解电磁模型' 按钮
if solve_em_btn:
    print(f"\n[A] 尝试点击 '求解电磁模型' 按钮 (多策略)...")
    print(f"    hwnd={solve_em_btn['hwnd']}, ctrl_id={solve_em_btn['ctrl_id']}, rect={solve_em_btn['rect']}")
    
    # 切到正确的标签
    if '输入数据' in str(get_visible_tabsheets(main_hwnd)):
        pass  # 已在正确标签
    
    close_dialogs(mc_pid)
    time.sleep(0.3)
    
    # 策略 A1: WM_COMMAND + BN_CLICKED
    print("\n  [A1] WM_COMMAND + BN_CLICKED...")
    parent_hwnd = find_button_parent(main_hwnd, solve_em_btn['hwnd'])
    print(f"    父窗口: hwnd={parent_hwnd}")
    send_wm_command(parent_hwnd, solve_em_btn['ctrl_id'], solve_em_btn['hwnd'])
    time.sleep(2)
    
    # 检查是否有新文件或弹窗
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    dialogs = close_dialogs(mc_pid)
    status = get_status_bar_text(main_hwnd)
    print(f"    新文件: {len(new_files)}, 弹窗: {len(dialogs)}, 状态: {status}")
    
    if not new_files and not dialogs:
        # 策略 A2: PostMessage WM_COMMAND
        print("\n  [A2] PostMessage WM_COMMAND...")
        post_wm_command(parent_hwnd, solve_em_btn['ctrl_id'], solve_em_btn['hwnd'])
        time.sleep(2)
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        dialogs = close_dialogs(mc_pid)
        print(f"    新文件: {len(new_files)}, 弹窗: {len(dialogs)}")
    
    if not new_files and not dialogs:
        # 策略 A3: 真实物理鼠标点击
        print("\n  [A3] SendInput 真实物理鼠标点击...")
        force_set_foreground(main_hwnd)
        time.sleep(0.5)
        
        # 计算按钮中心点的屏幕坐标
        btn_rect = solve_em_btn['rect']
        cx = (btn_rect[0] + btn_rect[2]) // 2
        cy = (btn_rect[1] + btn_rect[3]) // 2
        print(f"    按钮中心: ({cx}, {cy})")
        
        send_input_click(cx, cy)
        time.sleep(2)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        dialogs = close_dialogs(mc_pid)
        status = get_status_bar_text(main_hwnd)
        print(f"    新文件: {len(new_files)}, 弹窗: {len(dialogs)}, 状态: {status}")
        
        # 检查是否出现 '取消求解' 按钮 (表示计算已开始)
        vis_btns = get_visible_buttons(main_hwnd)
        cancel_btns = [b for b in vis_btns if '取消' in b['title'] or 'Cancel' in b['title']]
        if cancel_btns:
            print(f"    *** 出现取消按钮! 计算可能已开始: {cancel_btns[0]['title']}")
    
    # 如果计算已开始，等待完成
    if new_files or any('取消' in b['title'] for b in get_visible_buttons(main_hwnd)):
        print("\n  等待电磁计算完成...")
        start_time = time.time()
        for wait in range(300):  # 最多等 5 分钟
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            dialogs = close_dialogs(mc_pid)
            
            if new_files and elapsed % 10 == 0:
                print(f"    [{elapsed}s] 新文件: {len(new_files)} {sorted(new_files)[-3:]}")
            if dialogs:
                print(f"    [{elapsed}s] 弹窗: {dialogs}")
            if elapsed % 30 == 0:
                status = get_status_bar_text(main_hwnd)
                print(f"    [{elapsed}s] 状态: {status}")
            
            # 检查取消按钮是否消失 (计算完成)
            vis_btns = get_visible_buttons(main_hwnd)
            has_cancel = any('取消' in b['title'] for b in vis_btns)
            has_solve = any('求解电磁' in b['title'] for b in vis_btns)
            if has_solve and not has_cancel and elapsed > 5:
                print(f"    [{elapsed}s] 求解按钮重新出现，计算完成!")
                break
            if elapsed >= 300:
                print(f"    超时 (300s)")
                break

# 策略 B: 如果找到 '求解热模型' 按钮
if solve_thermal_btn:
    print(f"\n[B] 尝试点击 '求解热模型' 按钮 (多策略)...")
    print(f"    hwnd={solve_thermal_btn['hwnd']}, ctrl_id={solve_thermal_btn['ctrl_id']}, rect={solve_thermal_btn['rect']}")
    
    close_dialogs(mc_pid)
    time.sleep(0.3)
    
    # 策略 B1: WM_COMMAND + BN_CLICKED
    print("\n  [B1] WM_COMMAND + BN_CLICKED...")
    parent_hwnd = find_button_parent(main_hwnd, solve_thermal_btn['hwnd'])
    print(f"    父窗口: hwnd={parent_hwnd}")
    send_wm_command(parent_hwnd, solve_thermal_btn['ctrl_id'], solve_thermal_btn['hwnd'])
    time.sleep(3)
    
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    dialogs = close_dialogs(mc_pid)
    status = get_status_bar_text(main_hwnd)
    print(f"    新文件: {len(new_files)}, 弹窗: {len(dialogs)}, 状态: {status}")
    
    if not new_files and not dialogs:
        # 策略 B2: 真实物理鼠标点击
        print("\n  [B2] SendInput 真实物理鼠标点击...")
        force_set_foreground(main_hwnd)
        time.sleep(0.5)
        
        # 先切到 '求解设置' 标签
        tab_x = pc_rect[0] + 240
        tab_y = pc_rect[1] + 12
        send_input_click(tab_x, tab_y)
        time.sleep(1)
        
        # 重新查找按钮 (切换标签后 hwnd 可能变)
        vis_btns = get_visible_buttons(main_hwnd)
        for b in vis_btns:
            if '求解热' in b['title']:
                solve_thermal_btn = b
                break
        
        if solve_thermal_btn:
            btn_rect = solve_thermal_btn['rect']
            cx = (btn_rect[0] + btn_rect[2]) // 2
            cy = (btn_rect[1] + btn_rect[3]) // 2
            print(f"    按钮中心: ({cx}, {cy})")
            
            send_input_click(cx, cy)
            time.sleep(3)
            
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            dialogs = close_dialogs(mc_pid)
            status = get_status_bar_text(main_hwnd)
            print(f"    新文件: {len(new_files)}, 弹窗: {len(dialogs)}, 状态: {status}")
            
            # 检查取消按钮
            vis_btns = get_visible_buttons(main_hwnd)
            cancel_btns = [b for b in vis_btns if '取消' in b['title']]
            if cancel_btns:
                print(f"    *** 出现取消按钮! 计算可能已开始")
    
    # 如果计算已开始，等待完成
    vis_btns = get_visible_buttons(main_hwnd)
    has_cancel = any('取消' in b['title'] for b in vis_btns)
    if new_files or has_cancel:
        print("\n  等待热计算完成...")
        start_time = time.time()
        for wait in range(180):
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            dialogs = close_dialogs(mc_pid)
            
            if new_files and elapsed % 10 == 0:
                print(f"    [{elapsed}s] 新文件: {len(new_files)}")
            if dialogs:
                print(f"    [{elapsed}s] 弹窗: {dialogs}")
            if elapsed % 30 == 0:
                status = get_status_bar_text(main_hwnd)
                print(f"    [{elapsed}s] 状态: {status}")
            
            vis_btns = get_visible_buttons(main_hwnd)
            has_cancel = any('取消' in b['title'] for b in vis_btns)
            has_solve = any('求解热' in b['title'] for b in vis_btns)
            if has_solve and not has_cancel and elapsed > 5:
                print(f"    [{elapsed}s] 计算完成!")
                break
            if elapsed >= 180:
                print(f"    超时")
                break

# ============================================================
# Step 6: 检查计算后的状态
# ============================================================
print(f"\n[6] 计算后状态检查...")

files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
new_files = files_after - files_before
mot_mtime_after = os.path.getmtime(mot_file) if os.path.exists(mot_file) else 0

print(f"  FEResultsData 文件数: {len(files_after)} (新增: {len(new_files)})")
print(f"  .mot 修改时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mot_mtime_after))}")
print(f"  .mot 是否被修改: {'是' if mot_mtime_after > mot_mtime_before else '否'}")

if new_files:
    print(f"\n  新增文件:")
    for f in sorted(new_files):
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        mtime = os.path.getmtime(fpath)
        mtime_str = time.strftime('%H:%M:%S', time.localtime(mtime))
        print(f"    {f:45s} {size:10d} bytes  {mtime_str}")

# ============================================================
# Step 7: 列出所有结果文件
# ============================================================
print(f"\n[7] 所有结果文件:")
if os.path.exists(fe_dir):
    all_files = sorted(os.listdir(fe_dir), key=lambda f: os.path.getmtime(os.path.join(fe_dir, f)), reverse=True)
    for f in all_files[:20]:
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        mtime = os.path.getmtime(fpath)
        mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
        print(f"    {f:45s} {size:10d} bytes  {mtime_str}")

# ============================================================
# Step 8: 保存结果
# ============================================================
results = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'mc_pid': mc_pid,
    'main_hwnd': main_hwnd,
    'solve_em_btn': solve_em_btn,
    'solve_thermal_btn': solve_thermal_btn,
    'script_run_btn': script_run_btn,
    'new_files': sorted(new_files) if new_files else [],
    'mot_modified': mot_mtime_after > mot_mtime_before,
    'fe_files_count': len(files_after),
}

results_path = r"D:\MotorDesign\Scripts\solve_v3_results.json"
with open(results_path, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print(f"\n结果已保存: {results_path}")

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
