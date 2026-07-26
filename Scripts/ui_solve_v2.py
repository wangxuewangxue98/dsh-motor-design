#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD UI 自动化 (32位 Python)

直接操作主窗口:
1. 找到 PageControl
2. 切换到求解设置标签
3. 找到并点击求解按钮
4. 尝试多种点击方法
5. 监控 FEResultsData 目录
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
        if cls == 'TMainForm' and 'Motor-CAD' in title:
            main_hwnd = hwnd
            _, main_pid = win32process.GetWindowThreadProcessId(hwnd)
    except:
        pass
    return True

win32gui.EnumWindows(find_main, None)

if not main_hwnd:
    print("  *** Motor-CAD 未运行! ***")
    sys.exit(1)

print(f"Motor-CAD: hwnd={main_hwnd}, pid={main_pid}")

# 先关闭所有阻塞对话框
def close_dialogs():
    """关闭 Motor-CAD 的所有非主窗口对话框"""
    closed = []
    def cb(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == main_pid and hwnd != main_hwnd:
                if cls not in ('TApplication',):
                    # 列出控件
                    def get_btns(h, p2):
                        try:
                            c = win32gui.GetClassName(h)
                            t = win32gui.GetWindowText(h)
                            if c in ('TButton', 'TBitBtn') and t:
                                # 点击 OK/确定 按钮
                                if any(kw in t.lower() for kw in ['ok', '确定', 'close', '关闭']):
                                    print(f"    点击 '{t}' (hwnd={h})")
                                    win32gui.PostMessage(h, win32con.BM_CLICK, 0, 0)
                                    time.sleep(0.3)
                                    win32gui.SendMessage(h, win32con.BM_CLICK, 0, 0)
                                    time.sleep(0.3)
                        except:
                            pass
                        return True
                    win32gui.EnumChildWindows(hwnd, get_btns, None)
                    
                    # 如果没有按钮, 直接关闭
                    title = win32gui.GetWindowText(hwnd)
                    if win32gui.IsWindow(hwnd):
                        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                        closed.append(cls)
        except:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return closed

print("\n关闭阻塞对话框...")
closed = close_dialogs()
time.sleep(2)
if closed:
    print(f"  已关闭: {closed}")

# 重新查找主窗口
win32gui.EnumWindows(find_main, None)
if not main_hwnd:
    print("  *** 主窗口已关闭! ***")
    sys.exit(1)

# ============================================================
# 找到所有 TPageControl
# ============================================================
print("\n=== 查找 PageControl ===")

pcs = []
def find_pc(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TPageControl':
            rect = win32gui.GetWindowRect(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            size = (rect[2]-rect[0]) * (rect[3]-rect[1])
            pcs.append({'hwnd': hwnd, 'rect': rect, 'size': size, 'visible': vis})
    except:
        pass
    return True

win32gui.EnumChildWindows(main_hwnd, find_pc, None)
print(f"  找到 {len(pcs)} 个 PageControl:")
for pc in pcs:
    print(f"    hwnd={pc['hwnd']}, rect={pc['rect']}, vis={pc['visible']}, size={pc['size']}")

if not pcs:
    print("  *** PageControl 未找到! ***")
    sys.exit(1)

# 选最大的 PageControl
pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
pc_rect = win32gui.GetWindowRect(pc_hwnd)
print(f"\n  使用最大 PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# ============================================================
# 切换标签并扫描按钮
# ============================================================
print("\n=== 扫描所有标签页的按钮 ===")

def get_all_buttons(hwnd):
    """获取所有可见按钮"""
    btns = []
    def cb(h, p):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls in ('TButton', 'TBitBtn') and title:
                vis = win32gui.IsWindowVisible(h)
                enabled = win32gui.IsWindowEnabled(h)
                rect = win32gui.GetWindowRect(h)
                ctrl_id = win32api.GetWindowLong(h, win32con.GWL_ID)
                btns.append({
                    'hwnd': h, 'title': title, 'cls': cls,
                    'visible': vis, 'enabled': enabled, 'rect': rect,
                    'ctrl_id': ctrl_id,
                    'parent': win32gui.GetParent(h)
                })
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, cb, None)
    return btns

def click_tab_x(pc, x, y=12):
    """通过 PostMessage 点击标签"""
    lp = win32api.MAKELONG(x, y)
    win32gui.PostMessage(pc, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(0.8)

# 标签页 x 坐标映射 (来自之前的扫描)
tabs = [
    (50,   '几何'),
    (90,   '绕组'),
    (155,  '输入数据'),
    (240,  '求解设置'),
    (310,  '温度'),
    (380,  '输出数据'),
    (470,  '参数相关性'),
    (545,  '脚本'),
    (700,  '流体'),
]

# 扫描每个标签
solve_buttons = []
for x, name in tabs:
    click_tab_x(pc_hwnd, x)
    btns = get_all_buttons(main_hwnd)
    visible_btns = [b for b in btns if b['visible']]
    if visible_btns:
        print(f"\n  [{name}] (x={x}):")
        for b in visible_btns:
            print(f"    '{b['title']}' hwnd={b['hwnd']} ctrl_id={b['ctrl_id']} enabled={b['enabled']} rect={b['rect']}")
            if '求解' in b['title'] or 'solve' in b['title'].lower() or 'calc' in b['title'].lower():
                solve_buttons.append({'tab': name, 'tab_x': x, **b})

# ============================================================
# 尝试点击求解按钮
# ============================================================
if solve_buttons:
    print(f"\n=== 找到 {len(solve_buttons)} 个求解按钮 ===")
    
    for sb in solve_buttons:
        print(f"\n  尝试: '{sb['title']}' (标签: {sb['tab']})")
        print(f"    hwnd={sb['hwnd']}, ctrl_id={sb['ctrl_id']}, parent={sb['parent']}")
        
        # 先切到对应标签
        click_tab_x(pc_hwnd, sb['tab_x'])
        time.sleep(1)
        
        # 重新查找按钮（hwnd 可能变化）
        btns = get_all_buttons(main_hwnd)
        target = None
        for b in btns:
            if b['title'] == sb['title'] and b['visible']:
                target = b
                break
        
        if not target:
            print(f"    按钮未找到（可能已不可见）")
            continue
        
        btn_hwnd = target['hwnd']
        btn_rect = target['rect']
        btn_id = target['ctrl_id']
        parent = target['parent']
        cx = (btn_rect[2] - btn_rect[0]) // 2
        cy = (btn_rect[3] - btn_rect[1]) // 2
        lp = win32api.MAKELONG(cx, cy)
        
        # FEResultsData 目录监控
        fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
        files_before = set()
        if os.path.exists(fe_dir):
            files_before = set(os.listdir(fe_dir))
        print(f"    FEResultsData: {len(files_before)} 个文件")
        
        # 方法1: BM_CLICK (SendMessage)
        print(f"    [1] BM_CLICK (SendMessage)...")
        win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
        time.sleep(2)
        
        # 检查是否有变化
        btns_after = get_all_buttons(main_hwnd)
        has_cancel = any('取消' in b['title'] for b in btns_after if b['visible'])
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        print(f"      取消按钮={'有' if has_cancel else '无'}, 新文件={len(new_files)}")
        
        if has_cancel or new_files:
            print(f"    *** 计算可能已开始! ***")
        else:
            # 方法2: WM_COMMAND (发送到父窗口)
            print(f"    [2] WM_COMMAND (ctrl_id={btn_id})...")
            wp = win32api.MAKELONG(btn_id, 0)  # BN_CLICKED = 0
            win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
            time.sleep(2)
            
            btns_after = get_all_buttons(main_hwnd)
            has_cancel = any('取消' in b['title'] for b in btns_after if b['visible'])
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before
            print(f"      取消按钮={'有' if has_cancel else '无'}, 新文件={len(new_files)}")
            
            if has_cancel or new_files:
                print(f"    *** 计算可能已开始! ***")
            else:
                # 方法3: 完整鼠标消息序列 (PostMessage)
                print(f"    [3] PostMessage 完整鼠标序列...")
                win32gui.PostMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp)
                time.sleep(0.05)
                win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                time.sleep(0.1)
                win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp)
                time.sleep(0.1)
                # 也发送 BN_CLICKED 通知
                win32gui.PostMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
                time.sleep(2)
                
                btns_after = get_all_buttons(main_hwnd)
                has_cancel = any('取消' in b['title'] for b in btns_after if b['visible'])
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                print(f"      取消按钮={'有' if has_cancel else '无'}, 新文件={len(new_files)}")
                
                if has_cancel or new_files:
                    print(f"    *** 计算可能已开始! ***")
                else:
                    # 方法4: 尝试不同的 ctrl_id 值
                    print(f"    [4] 尝试不同 ctrl_id 值...")
                    for cid in [btn_id, btn_hwnd & 0xFFFF, 0, 1, 2, 1000, 1001, 1002]:
                        wp = win32api.MAKELONG(cid, 0)
                        win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
                        time.sleep(0.5)
                    
                    btns_after = get_all_buttons(main_hwnd)
                    has_cancel = any('取消' in b['title'] for b in btns_after if b['visible'])
                    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                    new_files = files_now - files_before
                    print(f"      取消按钮={'有' if has_cancel else '无'}, 新文件={len(new_files)}")
        
        # 如果计算已开始，等待完成
        if has_cancel or new_files:
            print(f"\n    等待计算完成...")
            for wait in range(300):
                time.sleep(1)
                btns_now = get_all_buttons(main_hwnd)
                has_cancel = any('取消' in b['title'] for b in btns_now if b['visible'])
                has_solve = any('求解' in b['title'] for b in btns_now if b['visible'])
                files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
                new_files = files_now - files_before
                
                if wait % 10 == 0:
                    print(f"    [{wait}s] 取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)}")
                
                if has_solve and not has_cancel and wait > 5:
                    print(f"    *** 计算完成! (耗时 {wait}s) ***")
                    break
                if wait >= 300:
                    print(f"    超时 (300s)")
                    break
            
            # 列出新文件
            files_final = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            final_new = files_final - files_before
            print(f"\n    新增文件 ({len(final_new)} 个):")
            for f in sorted(final_new):
                fpath = os.path.join(fe_dir, f)
                print(f"      {f}: {os.path.getsize(fpath)} bytes")
            
            break  # 成功，退出循环
else:
    print("\n  *** 未找到任何求解按钮! ***")

print("\n=== 完成 ===")
