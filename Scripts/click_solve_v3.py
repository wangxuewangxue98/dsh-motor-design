#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 标签页显示 + 按钮点击 v3

关键发现: TTabSheet vis=0, 标签页切换没有真正显示内容
策略: ShowWindow(TTabSheet, SW_SHOW) + 然后点击按钮
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
# 找到 Motor-CAD 主窗口
# ============================================================
main_hwnd = None
main_pid = None

def find_main(hwnd, p):
    global main_hwnd, main_pid
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls == 'TMainForm' and 'Motor' in title:
            main_hwnd = hwnd
            _, main_pid = win32process.GetWindowThreadProcessId(hwnd)
    except:
        pass
    return True

win32gui.EnumWindows(find_main, None)

if not main_hwnd:
    print("*** Motor-CAD 未运行! ***")
    sys.exit(1)

print(f"Motor-CAD: hwnd={main_hwnd}, pid={main_pid}")

# ============================================================
# 找到所有 TTabSheet 和 TPageControl
# ============================================================
print("\n=== 查找 TTabSheet 和 TPageControl ===")

tabsheets = []
pagecontrols = []

def find_tabs(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        if cls == 'TTabSheet':
            tabsheets.append({'hwnd': hwnd, 'title': title, 'vis': vis, 'rect': rect})
        elif cls == 'TPageControl':
            pagecontrols.append({'hwnd': hwnd, 'title': title, 'vis': vis, 'rect': rect})
    except:
        pass
    return True

win32gui.EnumChildWindows(main_hwnd, find_tabs, None)

print(f"\nTTabSheet ({len(tabsheets)} 个):")
for ts in tabsheets:
    print(f"  hwnd={ts['hwnd']} vis={ts['vis']} title='{ts['title']}' rect={ts['rect']}")

print(f"\nTPageControl ({len(pagecontrols)} 个):")
for pc in pagecontrols:
    print(f"  hwnd={pc['hwnd']} vis={pc['vis']} title='{pc['title']}' rect={pc['rect']}")

# ============================================================
# 尝试多种方法切换到"求解设置"标签页
# ============================================================
print("\n=== 切换到 '求解设置' 标签页 ===")

# 找到主 PageControl
pc_hwnd = max(pagecontrols, key=lambda x: 
    (x['rect'][2]-x['rect'][0]) * (x['rect'][3]-x['rect'][1]))['hwnd']

# 找到 "求解设置" TTabSheet
solve_tabsheet = None
for ts in tabsheets:
    if '求解' in ts['title']:
        solve_tabsheet = ts
        break

if not solve_tabsheet:
    print("*** '求解设置' TTabSheet 未找到! ***")
    sys.exit(1)

print(f"  PageControl: hwnd={pc_hwnd}")
print(f"  TTabSheet: hwnd={solve_tabsheet['hwnd']} title='{solve_tabsheet['title']}' vis={solve_tabsheet['vis']}")

# 方法1: PostMessage 点击标签
print("\n[1] PostMessage 点击标签 (x=240)...")
lp = win32api.MAKELONG(240, 12)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
time.sleep(0.1)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
time.sleep(1)
ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
print(f"  TTabSheet vis={ts_vis}")

# 方法2: TCM_SETCURSEL + WM_NOTIFY
if not ts_vis:
    print("\n[2] TCM_SETCURSEL + WM_NOTIFY...")
    TCM_FIRST = 0x1300
    TCM_SETCURSEL = TCM_FIRST + 12
    TCM_GETCURSEL = TCM_FIRST + 11
    
    # 设置选中索引为3 (求解设置)
    result = win32gui.SendMessage(pc_hwnd, TCM_SETCURSEL, 3, 0)
    print(f"  TCM_SETCURSEL(3): {result}")
    time.sleep(0.5)
    
    # 发送 TCN_SELCHANGE 通知
    # WM_NOTIFY 结构: NMHDR = (hwndFrom, idFrom, code)
    import ctypes
    class NMHDR(ctypes.Structure):
        _fields_ = [
            ('hwndFrom', ctypes.c_void_p),
            ('idFrom', ctypes.c_void_p),
            ('code', ctypes.c_int),
        ]
    
    TCN_FIRST = -550
    TCN_SELCHANGE = TCN_FIRST - 1
    
    nmhdr = NMHDR()
    nmhdr.hwndFrom = pc_hwnd
    nmhdr.idFrom = pc_hwnd
    nmhdr.code = TCN_SELCHANGE
    
    # 发送到 PageControl 的父窗口
    pc_parent = win32gui.GetParent(pc_hwnd)
    print(f"  PageControl 父窗口: {pc_parent} ({win32gui.GetClassName(pc_parent)})")
    
    win32gui.SendMessage(pc_parent, win32con.WM_NOTIFY, pc_hwnd, ctypes.addressof(nmhdr))
    time.sleep(1)
    
    ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
    print(f"  TTabSheet vis={ts_vis}")

# 方法3: ShowWindow(TTabSheet, SW_SHOW)
if not ts_vis:
    print("\n[3] ShowWindow(TTabSheet, SW_SHOW)...")
    win32gui.ShowWindow(solve_tabsheet['hwnd'], win32con.SW_SHOW)
    time.sleep(1)
    ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
    print(f"  TTabSheet vis={ts_vis}")

# 方法4: ShowWindow + ShowWindow(父窗口)
if not ts_vis:
    print("\n[4] ShowWindow(TTabSheet) + ShowWindow(子控件)...")
    # 先隐藏所有其他 TTabSheet
    for ts in tabsheets:
        if ts['hwnd'] != solve_tabsheet['hwnd']:
            win32gui.ShowWindow(ts['hwnd'], win32con.SW_HIDE)
    # 显示目标 TTabSheet
    win32gui.ShowWindow(solve_tabsheet['hwnd'], win32con.SW_SHOW)
    time.sleep(1)
    ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
    print(f"  TTabSheet vis={ts_vis}")

# 方法5: 发送 WM_LBUTTONDOWN/UP 到 PageControl 的标签位置 (用 SendMessage 而非 PostMessage)
if not ts_vis:
    print("\n[5] SendMessage 点击标签...")
    lp = win32api.MAKELONG(240, 12)
    win32gui.SendMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.2)
    win32gui.SendMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(1)
    ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
    print(f"  TTabSheet vis={ts_vis}")

# 方法6: 用 TCM_SETCURSEL + TCM_SETCURFOCUS
if not ts_vis:
    print("\n[6] TCM_SETCURSEL + TCM_SETCURFOCUS...")
    TCM_SETCURFOCUS = TCM_FIRST + 48
    win32gui.SendMessage(pc_hwnd, TCM_SETCURSEL, 3, 0)
    time.sleep(0.1)
    win32gui.SendMessage(pc_hwnd, TCM_SETCURFOCUS, 3, 0)
    time.sleep(1)
    ts_vis = win32gui.IsWindowVisible(solve_tabsheet['hwnd'])
    print(f"  TTabSheet vis={ts_vis}")

# ============================================================
# 如果标签页可见了，点击求解按钮
# ============================================================
print(f"\n=== TTabSheet 可见性: {ts_vis} ===")

if ts_vis:
    # 查找求解按钮
    btns = []
    def find_btns(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls in ('TButton', 'TBitBtn') and title:
                vis = win32gui.IsWindowVisible(hwnd)
                enabled = win32gui.IsWindowEnabled(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                btns.append({'hwnd': hwnd, 'title': title, 'vis': vis, 'enabled': enabled, 'rect': rect})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_btns, None)
    
    print(f"\n按钮 ({len(btns)} 个):")
    for b in btns:
        print(f"  [{'V' if b['vis'] else 'H'}] '{b['title']}' hwnd={b['hwnd']} rect={b['rect']}")
    
    solve_btn = None
    for b in btns:
        if '求解' in b['title'] and b['vis']:
            solve_btn = b
            break
    
    if solve_btn:
        print(f"\n找到可见求解按钮: '{solve_btn['title']}'")
        
        # FEResultsData 监控
        fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
        files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        mtimes_before = {}
        for f in files_before:
            mtimes_before[f] = os.path.getmtime(os.path.join(fe_dir, f))
        print(f"FEResultsData: {len(files_before)} 个文件")
        
        # 点击按钮
        btn_hwnd = solve_btn['hwnd']
        btn_rect = solve_btn['rect']
        cx = (btn_rect[2] - btn_rect[0]) // 2
        cy = (btn_rect[3] - btn_rect[1]) // 2
        lp_btn = win32api.MAKELONG(cx, cy)
        
        print(f"\n点击按钮: hwnd={btn_hwnd}, rect={btn_rect}")
        
        # BM_CLICK
        print("[1] BM_CLICK...")
        win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
        time.sleep(3)
        
        # 检查结果
        btns_after = []
        def check_btns(hwnd, p):
            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(hwnd):
                    btns_after.append(title)
            except: pass
            return True
        win32gui.EnumChildWindows(main_hwnd, check_btns, None)
        has_cancel = any('取消' in t for t in btns_after)
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        print(f"  取消={'有' if has_cancel else '无'} 新文件={len(new_files)}")
        
        if not has_cancel and not new_files:
            print("[2] PostMessage 鼠标序列...")
            win32gui.PostMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp_btn)
            time.sleep(0.05)
            win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp_btn)
            time.sleep(0.1)
            win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp_btn)
            time.sleep(3)
            
            win32gui.EnumChildWindows(main_hwnd, check_btns, None)
            has_cancel = any('取消' in t for t in btns_after)
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            print(f"  取消={'有' if has_cancel else '无'} 新文件={len(new_files)}")
        
        if not has_cancel and not new_files:
            print("[3] pywinauto click()...")
            try:
                from pywinauto import Application
                app = Application(backend='win32').connect(process=main_pid)
                win = app.window(handle=main_hwnd)
                btn = win.child_window(handle=btn_hwnd)
                btn.click()
                time.sleep(3)
                
                win32gui.EnumChildWindows(main_hwnd, check_btns, None)
                has_cancel = any('取消' in t for t in btns_after)
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                print(f"  取消={'有' if has_cancel else '无'} 新文件={len(new_files)}")
            except Exception as e:
                print(f"  失败: {e}")
        
        # 如果计算开始，等待完成
        if has_cancel or new_files:
            print(f"\n*** 计算已开始! 等待完成... ***")
            for wait in range(600):
                time.sleep(1)
                btns_now = []
                win32gui.EnumChildWindows(main_hwnd, check_btns, None)
                has_cancel = any('取消' in t for t in btns_now)
                has_solve = any('求解' in t for t in btns_now)
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                
                if wait % 10 == 0:
                    print(f"  [{wait}s] 取消={'有' if has_cancel else '无'} 新文件={len(new_files)}")
                
                if has_solve and not has_cancel and wait > 5:
                    print(f"  *** 计算完成! ({wait}s) ***")
                    break
                if wait >= 600:
                    print(f"  超时")
                    break
            
            files_final = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            final_new = files_final - files_before
            print(f"\n  新增: {len(final_new)}")
            for f in sorted(final_new):
                print(f"    {f}")
        else:
            print("\n*** 仍未触发计算 ***")
    else:
        print("\n*** 求解按钮不可见! ***")
else:
    print("\n*** TTabSheet 仍不可见! 所有方法失败! ***")

print("\n=== 完成 ===")
