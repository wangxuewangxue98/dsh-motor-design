#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v4 — 深度 UI 自动化

新策略:
  1. 检查 Message Display 窗口内容
  2. 先 '检查数据' 再 '求解'
  3. 使用 SendMessage (同步) 而非 PostMessage
  4. 尝试 WM_KEYDOWN VK_SPACE/VK_RETURN 激活按钮
  5. 使用 keybd_event Alt 技巧 + SetForegroundWindow
  6. 监控 Message Display 和 FEResultsData 双通道
"""
import sys
import os
import time
import ctypes
import win32gui
import win32con
import win32api
import win32process

# ============================================================
# 工具函数
# ============================================================

def find_motorcad_windows():
    """找到所有 Motor-CAD 相关窗口"""
    windows = {'main': None, 'message_display': None, 'application': None, 'all': []}
    def callback(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if 'Motor-CAD' in title or cls in ('TMainForm', 'TMessageDisplayForm', 'TApplication'):
                if visible or cls == 'TApplication':
                    windows['all'].append({
                        'hwnd': int(hwnd), 'class': cls, 'title': title,
                        'pid': pid, 'visible': visible
                    })
                    if cls == 'TMainForm' and visible:
                        windows['main'] = int(hwnd)
                    elif cls == 'TMessageDisplayForm' and visible:
                        windows['message_display'] = int(hwnd)
                    elif cls == 'TApplication':
                        windows['application'] = int(hwnd)
        except:
            pass
        return True
    win32gui.EnumWindows(callback, None)
    return windows

def get_all_children(parent_hwnd):
    results = []
    def collect(hwnd, lparam):
        try:
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
        except:
            pass
        return True
    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return results

def get_visible_buttons(main_hwnd):
    buttons = []
    def check(hwnd, lparam):
        try:
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
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

def get_visible_tabsheets(main_hwnd):
    tabs = []
    def check(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == 'TTabSheet' and win32gui.IsWindowVisible(hwnd) and title:
                rect = win32gui.GetWindowRect(hwnd)
                tabs.append({'title': title, 'hwnd': int(hwnd), 'rect': list(rect)})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs

def get_pagecontrols(main_hwnd):
    pcs = []
    def check(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                try:
                    ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
                except:
                    ctrl_id = 0
                pcs.append({'hwnd': int(hwnd), 'rect': list(rect), 'ctrl_id': ctrl_id})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return pcs

def click_tab_x(pc_hwnd, x_pos, y_pos=12):
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

def force_foreground(hwnd):
    """使用 keybd_event Alt 技巧强制前台窗口"""
    try:
        # Alt 键按下再释放，重置前台锁
        ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)       # VK_MENU down
        ctypes.windll.user32.keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up
        
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(0.3)
        
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as e:
        print(f"  force_foreground 异常: {e}")
        return False

def send_mouse_click_sync(btn_hwnd):
    """使用 SendMessage 同步发送鼠标点击 (确保消息被处理)"""
    rect = win32gui.GetWindowRect(btn_hwnd)
    cx = (rect[2] - rect[0]) // 2
    cy = (rect[3] - rect[1]) // 2
    lparam = win32api.MAKELONG(cx, cy)
    
    # 完整的鼠标点击序列
    win32gui.SendMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.05)
    win32gui.SendMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.1)
    
    # 也发送 BN_CLICKED 通知到父窗口
    parent = win32gui.GetParent(btn_hwnd)
    if parent:
        try:
            ctrl_id = win32api.GetWindowLong(btn_hwnd, win32con.GWL_ID)
        except:
            ctrl_id = btn_hwnd
        wparam = win32api.MAKELONG(ctrl_id, 0)  # BN_CLICKED = 0
        win32gui.SendMessage(parent, win32con.WM_COMMAND, wparam, btn_hwnd)

def send_keyboard_activate(btn_hwnd):
    """使用键盘消息激活按钮 (VK_SPACE/VK_RETURN)"""
    # 先设置焦点到按钮
    win32gui.SendMessage(btn_hwnd, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.05)
    
    # VK_SPACE 按下再释放 (标准按钮激活方式)
    win32gui.SendMessage(btn_hwnd, win32con.WM_KEYDOWN, win32con.VK_SPACE, 0)
    time.sleep(0.05)
    win32gui.SendMessage(btn_hwnd, win32con.WM_KEYUP, win32con.VK_SPACE, 0)
    time.sleep(0.1)
    
    # 也试 VK_RETURN
    win32gui.SendMessage(btn_hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    time.sleep(0.05)
    win32gui.SendMessage(btn_hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)

def physical_click(screen_x, screen_y):
    """使用 SendInput 进行真实物理鼠标点击"""
    ctypes.windll.user32.SetCursorPos(screen_x, screen_y)
    time.sleep(0.05)
    
    # 按下
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.05)
    # 释放
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(0.1)

def get_message_display_text(msg_hwnd):
    """获取 Message Display 窗口的文本内容"""
    if not msg_hwnd:
        return []
    
    texts = []
    def collect(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if title and cls != 'TButton':
                texts.append({'class': cls, 'text': title})
        except:
            pass
        return True
    win32gui.EnumChildWindows(msg_hwnd, collect, None)
    return texts

def close_dialogs(mc_pid):
    closed = []
    def find_and_close(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == mc_pid and visible and cls in ('TMessageForm', 'TForm', 'TDialog'):
                title = win32gui.GetWindowText(hwnd)
                if title and 'Motor-CAD' not in title and 'Message Display' not in title:
                    def get_text(child_hwnd, lparam):
                        try:
                            child_cls = win32gui.GetClassName(child_hwnd)
                            child_title = win32gui.GetWindowText(child_hwnd)
                            if child_title:
                                closed.append(f"{child_cls}: '{child_title[:200]}'")
                        except:
                            pass
                        return True
                    win32gui.EnumChildWindows(hwnd, get_text, None)
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except:
            pass
        return True
    win32gui.EnumWindows(find_and_close, None)
    return closed

def get_status_bar_text(main_hwnd):
    texts = []
    def check(hwnd, lparam):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == 'TStatusBar':
                text = win32gui.GetWindowText(hwnd)
                if text and text.strip():
                    texts.append(text)
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return texts

# ============================================================
# 主流程
# ============================================================

print("=" * 70)
print("Motor-CAD 自动解算 v4 — 深度 UI 自动化")
print("=" * 70)

# Step 0: 找到 Motor-CAD 窗口
print("\n[0] 查找 Motor-CAD 窗口...")
mc = find_motorcad_windows()

for w in mc['all']:
    print(f"  {w['class']}: hwnd={w['hwnd']}, pid={w['pid']}, visible={w['visible']}, title='{w['title'][:50]}'")

main_hwnd = mc['main']
msg_hwnd = mc['message_display']
app_hwnd = mc['application']

if not main_hwnd:
    print("  错误: 未找到 Motor-CAD 主窗口!")
    sys.exit(1)

_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
print(f"\n  主窗口: hwnd={main_hwnd}, pid={mc_pid}")
print(f"  消息窗口: hwnd={msg_hwnd}")
print(f"  应用窗口: hwnd={app_hwnd}")

# Step 1: 检查 Message Display 窗口
print("\n[1] 检查 Message Display 窗口内容...")
msg_texts = get_message_display_text(msg_hwnd)
if msg_texts:
    print(f"  消息内容 ({len(msg_texts)} 条):")
    for t in msg_texts[-10:]:  # 最后10条
        print(f"    [{t['class']}] {t['text'][:100]}")
else:
    print("  无消息内容 (或窗口不存在)")

# Step 2: 前台化
print("\n[2] 将 Motor-CAD 置于前台...")
force_foreground(main_hwnd)
print(f"  前台: {'成功' if win32gui.GetForegroundWindow() == main_hwnd else '失败'}")

# Step 3: 找到主 PageControl
print("\n[3] 查找 PageControl...")
pcs = get_pagecontrols(main_hwnd)
if not pcs:
    print("  错误: 未找到 TPageControl!")
    sys.exit(1)

main_pc = max(pcs, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  主 PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Step 4: 先到 '输入数据' 标签点击 '检查数据'
print("\n[4] 切换到 '输入数据' 标签, 点击 '检查数据'...")

# 用 PostMessage 切换标签
click_tab_x(pc_hwnd, 150)  # 输入数据标签 x≈120-190
time.sleep(1)

vis_tabs = get_visible_tabsheets(main_hwnd)
current_tab = vis_tabs[0]['title'] if vis_tabs else "?"
print(f"  当前标签: '{current_tab}'")

vis_btns = get_visible_buttons(main_hwnd)
check_data_btn = None
for b in vis_btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']} rect={b['rect']}")
    if '检查' in b['title'] or 'Check' in b['title']:
        check_data_btn = b

if check_data_btn:
    print(f"\n  点击 '检查数据' 按钮 (多方法)...")
    
    # 方法1: SendMessage 同步点击
    print("  [方法1] SendMessage 同步鼠标点击...")
    send_mouse_click_sync(check_data_btn['hwnd'])
    time.sleep(2)
    
    # 检查消息窗口
    msg_texts = get_message_display_text(msg_hwnd)
    if msg_texts:
        print(f"  消息窗口新增 {len(msg_texts)} 条:")
        for t in msg_texts[-5:]:
            print(f"    [{t['class']}] {t['text'][:100]}")
    
    dialogs = close_dialogs(mc_pid)
    if dialogs:
        print(f"  弹窗: {dialogs}")
    
    # 方法2: 键盘激活
    print("  [方法2] 键盘 VK_SPACE 激活...")
    send_keyboard_activate(check_data_btn['hwnd'])
    time.sleep(2)
    
    msg_texts = get_message_display_text(msg_hwnd)
    if msg_texts:
        print(f"  消息窗口新增:")
        for t in msg_texts[-5:]:
            print(f"    [{t['class']}] {t['text'][:100]}")
    
    dialogs = close_dialogs(mc_pid)
    if dialogs:
        print(f"  弹窗: {dialogs}")
else:
    print("  '检查数据' 按钮未找到")

# Step 5: 切换到 '求解设置' 标签
print("\n[5] 切换到 '求解设置' 标签...")
click_tab_x(pc_hwnd, 240)  # 求解设置标签 x≈200-280
time.sleep(1)

vis_tabs = get_visible_tabsheets(main_hwnd)
current_tab = vis_tabs[0]['title'] if vis_tabs else "?"
print(f"  当前标签: '{current_tab}'")

vis_btns = get_visible_buttons(main_hwnd)
solve_thermal_btn = None
solve_em_btn = None
cancel_btn = None

for b in vis_btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']} rect={b['rect']}")
    if '求解热' in b['title'] or ('Solve' in b['title'] and 'Thermal' in b['title']):
        solve_thermal_btn = b
    if '求解电磁' in b['title']:
        solve_em_btn = b
    if '取消' in b['title'] or 'Cancel' in b['title']:
        cancel_btn = b

# Step 6: 尝试点击 '求解热模型'
if solve_thermal_btn:
    print(f"\n[6] 点击 '求解热模型' 按钮 (多策略深度尝试)...")
    print(f"    hwnd={solve_thermal_btn['hwnd']}, rect={solve_thermal_btn['rect']}")
    
    # FEResultsData 目录
    fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
    files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    print(f"    FEResultsData 文件数: {len(files_before)}")
    
    close_dialogs(mc_pid)
    time.sleep(0.5)
    
    # 策略1: SendMessage 同步鼠标点击 + BN_CLICKED
    print("\n  [策略1] SendMessage 同步鼠标点击 + BN_CLICKED...")
    send_mouse_click_sync(solve_thermal_btn['hwnd'])
    time.sleep(3)
    
    # 检查结果
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    msg_texts = get_message_display_text(msg_hwnd)
    dialogs = close_dialogs(mc_pid)
    status = get_status_bar_text(main_hwnd)
    
    print(f"    新文件: {len(new_files)}")
    print(f"    弹窗: {len(dialogs)}")
    print(f"    状态栏: {status}")
    if msg_texts:
        print(f"    消息窗口最后3条:")
        for t in msg_texts[-3:]:
            print(f"      [{t['class']}] {t['text'][:100]}")
    
    # 检查是否出现取消按钮
    vis_btns = get_visible_buttons(main_hwnd)
    has_cancel = any('取消' in b['title'] for b in vis_btns)
    if has_cancel:
        print("    *** 出现取消按钮! 计算可能已开始!")
    
    if not new_files and not has_cancel:
        # 策略2: 键盘激活
        print("\n  [策略2] 键盘 VK_SPACE + VK_RETURN 激活...")
        # 先前台化
        force_foreground(main_hwnd)
        time.sleep(0.5)
        
        # 重新切到求解设置标签
        click_tab_x(pc_hwnd, 240)
        time.sleep(0.5)
        
        # 重新查找按钮
        vis_btns = get_visible_buttons(main_hwnd)
        for b in vis_btns:
            if '求解热' in b['title']:
                solve_thermal_btn = b
                break
        
        send_keyboard_activate(solve_thermal_btn['hwnd'])
        time.sleep(3)
        
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        msg_texts = get_message_display_text(msg_hwnd)
        dialogs = close_dialogs(mc_pid)
        
        print(f"    新文件: {len(new_files)}")
        print(f"    弹窗: {len(dialogs)}")
        if msg_texts:
            print(f"    消息窗口最后3条:")
            for t in msg_texts[-3:]:
                print(f"      [{t['class']}] {t['text'][:100]}")
        
        vis_btns = get_visible_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in vis_btns)
        if has_cancel:
            print("    *** 出现取消按钮! 计算可能已开始!")
    
    if not new_files and not has_cancel:
        # 策略3: 真实物理鼠标点击 (前台化后)
        print("\n  [策略3] SendInput 真实物理鼠标点击...")
        force_foreground(main_hwnd)
        time.sleep(0.5)
        
        # 切到求解设置标签
        pc_screen_x = pc_rect[0]
        pc_screen_y = pc_rect[1]
        # 物理点击标签
        tab_x = pc_screen_x + 240
        tab_y = pc_screen_y + 12
        print(f"    物理点击标签: ({tab_x}, {tab_y})")
        physical_click(tab_x, tab_y)
        time.sleep(1)
        
        vis_tabs = get_visible_tabsheets(main_hwnd)
        current_tab = vis_tabs[0]['title'] if vis_tabs else "?"
        print(f"    当前标签: '{current_tab}'")
        
        # 重新查找按钮
        vis_btns = get_visible_buttons(main_hwnd)
        for b in vis_btns:
            if '求解热' in b['title']:
                solve_thermal_btn = b
                break
        
        if solve_thermal_btn:
            btn_rect = solve_thermal_btn['rect']
            cx = (btn_rect[0] + btn_rect[2]) // 2
            cy = (btn_rect[1] + btn_rect[3]) // 2
            print(f"    物理点击按钮: ({cx}, {cy})")
            
            # 确保前台
            fg = win32gui.GetForegroundWindow()
            print(f"    当前前台: {fg} (Motor-CAD={main_hwnd})")
            
            physical_click(cx, cy)
            time.sleep(3)
            
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            msg_texts = get_message_display_text(msg_hwnd)
            dialogs = close_dialogs(mc_pid)
            status = get_status_bar_text(main_hwnd)
            
            print(f"    新文件: {len(new_files)}")
            print(f"    弹窗: {len(dialogs)}")
            print(f"    状态栏: {status}")
            if msg_texts:
                print(f"    消息窗口最后3条:")
                for t in msg_texts[-3:]:
                    print(f"      [{t['class']}] {t['text'][:100]}")
            
            vis_btns = get_visible_buttons(main_hwnd)
            has_cancel = any('取消' in b['title'] for b in vis_btns)
            if has_cancel:
                print("    *** 出现取消按钮! 计算可能已开始!")
    
    # 如果计算已开始，等待完成
    if new_files or has_cancel:
        print("\n  等待计算完成...")
        start_time = time.time()
        for wait in range(300):
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            dialogs = close_dialogs(mc_pid)
            
            if elapsed % 10 == 0:
                msg_texts = get_message_display_text(msg_hwnd)
                vis_btns = get_visible_buttons(main_hwnd)
                has_cancel = any('取消' in b['title'] for b in vis_btns)
                has_solve = any('求解热' in b['title'] for b in vis_btns)
                print(f"    [{elapsed}s] 新文件={len(new_files)} 取消按钮={'有' if has_cancel else '无'} 求解按钮={'有' if has_solve else '无'}")
                if msg_texts:
                    latest = msg_texts[-1]['text'][:80] if msg_texts else ""
                    print(f"           最新消息: {latest}")
            
            vis_btns = get_visible_buttons(main_hwnd)
            has_cancel = any('取消' in b['title'] for b in vis_btns)
            has_solve = any('求解热' in b['title'] for b in vis_btns)
            if has_solve and not has_cancel and elapsed > 5:
                print(f"    [{elapsed}s] 计算完成!")
                break
            if elapsed >= 300:
                print(f"    超时")
                break
        
        files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        final_new = files_after - files_before
        print(f"\n  最终新增文件: {len(final_new)}")
        for f in sorted(final_new):
            fpath = os.path.join(fe_dir, f)
            size = os.path.getsize(fpath)
            print(f"    {f}: {size} bytes")
else:
    print("\n  '求解热模型' 按钮未找到!")

# Step 7: 最终消息窗口检查
print("\n[7] 最终消息窗口检查...")
msg_texts = get_message_display_text(msg_hwnd)
if msg_texts:
    print(f"  消息窗口内容 ({len(msg_texts)} 条):")
    for t in msg_texts[-15:]:
        print(f"    [{t['class']}] {t['text'][:120]}")
else:
    print("  无消息内容")

# Step 8: 列出 FEResultsData 文件
print(f"\n[8] FEResultsData 文件列表:")
if os.path.exists(fe_dir):
    all_files = sorted(os.listdir(fe_dir), key=lambda f: os.path.getmtime(os.path.join(fe_dir, f)), reverse=True)
    for f in all_files[:15]:
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        mtime = os.path.getmtime(fpath)
        mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
        print(f"    {f:45s} {size:10d} bytes  {mtime_str}")

print("\n" + "=" * 70)
print("v4 完成!")
print("=" * 70)
