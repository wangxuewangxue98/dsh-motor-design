#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 对话框处理 + UI 自动化

1. 检查并关闭启动对话框 (License Error, Demo, Default Motor Type)
2. 显示主窗口
3. 加载 .mot 文件
4. 切换到求解标签
5. 触发计算
"""
import sys
import os
import time
import struct
import win32gui
import win32con
import win32api
import win32process

print(f"Python: {struct.calcsize('P')*8}位")

# ============================================================
# Step 1: 查找所有 Motor-CAD 窗口
# ============================================================
print("\n=== Step 1: 查找窗口 ===")

windows = {}
def find_all(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if cls in ('TMainForm', 'TApplication', 'TLicenceErrorForm', 
                    'TDemo_Form', 'TForm_Default_Motor_Type', 'TMessageDisplayForm'):
            vis = win32gui.IsWindowVisible(hwnd)
            enabled = win32gui.IsWindowEnabled(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            windows[cls] = {
                'hwnd': hwnd, 'pid': pid, 'title': title,
                'visible': vis, 'enabled': enabled, 'rect': rect
            }
            print(f"  {cls}: hwnd={hwnd}, vis={vis}, enabled={enabled}, title='{title[:40]}'")
    except:
        pass
    return True

win32gui.EnumWindows(find_all, None)

if 'TMainForm' not in windows:
    print("  *** TMainForm 未找到! ***")
    sys.exit(1)

main_hwnd = windows['TMainForm']['hwnd']
main_pid = windows['TMainForm']['pid']
print(f"\n  主窗口: hwnd={main_hwnd}, pid={main_pid}")

# ============================================================
# Step 2: 检查并关闭对话框
# ============================================================
print("\n=== Step 2: 处理对话框 ===")

def get_buttons(hwnd):
    """获取窗口的所有按钮"""
    btns = []
    def cb(h, p):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls in ('TButton', 'TBitBtn', 'Button'):
                btns.append({'hwnd': h, 'title': title, 'cls': cls,
                            'rect': win32gui.GetWindowRect(h),
                            'enabled': win32gui.IsWindowEnabled(h)})
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, cb, None)
    return btns

def get_all_controls(hwnd):
    """获取窗口的所有控件"""
    ctrls = []
    def cb(h, p):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            vis = win32gui.IsWindowVisible(h)
            enabled = win32gui.IsWindowEnabled(h)
            rect = win32gui.GetWindowRect(h)
            ctrls.append({'hwnd': h, 'cls': cls, 'title': title, 
                         'visible': vis, 'enabled': enabled, 'rect': rect})
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, cb, None)
    return ctrls

# 处理 License Error
if 'TLicenceErrorForm' in windows:
    hwnd = windows['TLicenceErrorForm']['hwnd']
    print(f"\n  License Error 对话框 (hwnd={hwnd}):")
    btns = get_buttons(hwnd)
    for b in btns:
        print(f"    按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
    
    # 查找 OK / 确定 / Close 按钮
    for b in btns:
        if any(kw in b['title'].lower() for kw in ['ok', '确定', 'close', '关闭', 'exit']):
            print(f"    点击 '{b['title']}'...")
            win32gui.PostMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            # 也尝试 SendMessage
            win32gui.SendMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            break
    
    # 如果没有找到，直接关闭
    if win32gui.IsWindow(hwnd):
        print(f"    直接关闭对话框...")
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1)

# 处理 Demo Form
if 'TDemo_Form' in windows:
    hwnd = windows['TDemo_Form']['hwnd']
    print(f"\n  Demo 对话框 (hwnd={hwnd}):")
    btns = get_buttons(hwnd)
    for b in btns:
        print(f"    按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
    
    for b in btns:
        if any(kw in b['title'].lower() for kw in ['ok', '确定', 'close', '关闭', 'continue', '继续']):
            print(f"    点击 '{b['title']}'...")
            win32gui.PostMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            win32gui.SendMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            break
    
    if win32gui.IsWindow(hwnd):
        print(f"    直接关闭对话框...")
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1)

# 处理 Default Motor Type
if 'TForm_Default_Motor_Type' in windows:
    hwnd = windows['TForm_Default_Motor_Type']['hwnd']
    print(f"\n  Default Motor Type 对话框 (hwnd={hwnd}):")
    btns = get_buttons(hwnd)
    for b in btns:
        print(f"    按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
    
    for b in btns:
        if any(kw in b['title'].lower() for kw in ['ok', '确定', 'continue', '继续']):
            print(f"    点击 '{b['title']}'...")
            win32gui.PostMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            win32gui.SendMessage(b['hwnd'], win32con.BM_CLICK, 0, 0)
            time.sleep(1)
            break
    
    if win32gui.IsWindow(hwnd):
        print(f"    直接关闭对话框...")
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1)

# ============================================================
# Step 3: 显示主窗口
# ============================================================
print("\n=== Step 3: 显示主窗口 ===")

# 尝试多种方式显示窗口
print(f"  当前状态: vis={win32gui.IsWindowVisible(main_hwnd)}, iconic={win32gui.IsIconic(main_hwnd)}")

# 方法1: ShowWindow
print("  [1] ShowWindow(SW_SHOW)...")
win32gui.ShowWindow(main_hwnd, win32con.SW_SHOW)
time.sleep(1)
print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 方法2: ShowWindow(SW_RESTORE)
if not win32gui.IsWindowVisible(main_hwnd):
    print("  [2] ShowWindow(SW_RESTORE)...")
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    time.sleep(1)
    print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 方法3: ShowWindow(SW_SHOWNORMAL)
if not win32gui.IsWindowVisible(main_hwnd):
    print("  [3] ShowWindow(SW_SHOWNORMAL)...")
    win32gui.ShowWindow(main_hwnd, win32con.SW_SHOWNORMAL)
    time.sleep(1)
    print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 方法4: 通过 TApplication 窗口
if not win32gui.IsWindowVisible(main_hwnd) and 'TApplication' in windows:
    app_hwnd = windows['TApplication']['hwnd']
    print(f"  [4] 通过 TApplication (hwnd={app_hwnd})...")
    # 在 Delphi 中, TApplication 是主窗口, TMainForm 是子窗口
    # 尝试发送 WM_SHOW
    win32gui.PostMessage(main_hwnd, win32con.WM_SHOW, 1, 0)
    time.sleep(1)
    print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 方法5: BringWindowToTop
if not win32gui.IsWindowVisible(main_hwnd):
    print("  [5] BringWindowToTop...")
    win32gui.BringWindowToTop(main_hwnd)
    time.sleep(1)
    print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 方法6: SetWindowPos
if not win32gui.IsWindowVisible(main_hwnd):
    print("  [6] SetWindowPos (HWND_TOP)...")
    win32gui.SetWindowPos(main_hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                         win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    time.sleep(1)
    print(f"    vis={win32gui.IsWindowVisible(main_hwnd)}")

# 检查是否有其他阻塞对话框
print("\n  检查是否有其他阻塞对话框...")
def find_blocking(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == main_pid and cls not in ('TMainForm', 'TApplication'):
            vis = win32gui.IsWindowVisible(hwnd)
            enabled = win32gui.IsWindowEnabled(hwnd)
            if vis or enabled:
                print(f"    {cls}: hwnd={hwnd}, vis={vis}, enabled={enabled}, title='{title[:40]}'")
                # 列出按钮
                btns = get_buttons(hwnd)
                for b in btns:
                    print(f"      按钮: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']}")
    except:
        pass
    return True

win32gui.EnumWindows(find_blocking, None)

# ============================================================
# Step 4: 如果主窗口可见，继续 UI 自动化
# ============================================================
if win32gui.IsWindowVisible(main_hwnd):
    print(f"\n=== Step 4: 主窗口已可见，继续 UI 自动化 ===")
    
    # 找 PageControl
    pcs = []
    def find_pc(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                size = (rect[2]-rect[0]) * (rect[3]-rect[1])
                pcs.append({'hwnd': hwnd, 'rect': rect, 'size': size})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_pc, None)
    
    if pcs:
        pc = max(pcs, key=lambda x: x['size'])
        print(f"  PageControl: hwnd={pc['hwnd']}, rect={pc['rect']}")
        
        # 切换到 '求解设置' 标签 (x=240)
        print("  切换到 '求解设置' 标签...")
        lp = win32api.MAKELONG(240, 12)
        win32gui.PostMessage(pc['hwnd'], win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
        time.sleep(0.1)
        win32gui.PostMessage(pc['hwnd'], win32con.WM_LBUTTONUP, 0, lp)
        time.sleep(1)
        
        # 查找按钮
        btns = get_buttons(main_hwnd)
        print(f"\n  当前按钮 ({len(btns)} 个):")
        for b in btns:
            print(f"    '{b['title']}' hwnd={b['hwnd']} rect={b['rect']} enabled={b['enabled']}")
    else:
        print("  PageControl 未找到!")
else:
    print(f"\n  *** 主窗口仍不可见! ***")
    print("  可能仍有阻塞对话框")

print("\n=== 完成 ===")
