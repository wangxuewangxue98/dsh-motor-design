#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 求解按钮点击 + 计算监控

.mot 文件已加载，标签已切换到求解设置
找到 '求解热模型' 按钮: hwnd=17049102, rect=(477,346,727,385)

尝试多种方法点击按钮，监控 FEResultsData 目录
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
    print("  *** Motor-CAD 未运行! ***")
    sys.exit(1)

print(f"Motor-CAD: hwnd={main_hwnd}, pid={main_pid}")
print(f"  title: {win32gui.GetWindowText(main_hwnd)}")

# ============================================================
# 找 PageControl 并切换到求解设置
# ============================================================
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
pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']

# 切换到求解设置 (x=240, 标签索引3)
lp = win32api.MAKELONG(240, 12)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
time.sleep(0.1)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
time.sleep(1)

# ============================================================
# 找到 '求解热模型' 按钮
# ============================================================
def get_visible_buttons(hwnd):
    btns = []
    def cb(h, p):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(h):
                rect = win32gui.GetWindowRect(h)
                ctrl_id = win32api.GetWindowLong(h, win32con.GWL_ID)
                btns.append({'hwnd': h, 'title': title, 'rect': rect, 'ctrl_id': ctrl_id,
                            'parent': win32gui.GetParent(h)})
        except:
            pass
        return True
    win32gui.EnumChildWindows(hwnd, cb, None)
    return btns

btns = get_visible_buttons(main_hwnd)
solve_btn = None
for b in btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} ctrl_id={b['ctrl_id']} rect={b['rect']}")
    if '求解' in b['title']:
        solve_btn = b

if not solve_btn:
    print("  *** '求解热模型' 按钮未找到! ***")
    sys.exit(1)

print(f"\n找到求解按钮: '{solve_btn['title']}' hwnd={solve_btn['hwnd']}")

# ============================================================
# FEResultsData 目录监控
# ============================================================
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = set()
if os.path.exists(fe_dir):
    files_before = set(os.listdir(fe_dir))
    # 记录文件修改时间
    files_mtime_before = {}
    for f in files_before:
        fpath = os.path.join(fe_dir, f)
        files_mtime_before[f] = os.path.getmtime(fpath)
    print(f"FEResultsData: {len(files_before)} 个文件 (计算前)")
else:
    print(f"FEResultsData 目录不存在: {fe_dir}")

# ============================================================
# 尝试多种方法点击按钮
# ============================================================
btn_hwnd = solve_btn['hwnd']
btn_rect = solve_btn['rect']
btn_id = solve_btn['ctrl_id']
parent = solve_btn['parent']
cx = (btn_rect[2] - btn_rect[0]) // 2
cy = (btn_rect[3] - btn_rect[1]) // 2
lp_btn = win32api.MAKELONG(cx, cy)

def check_calculation_started():
    """检查计算是否已开始"""
    btns = get_visible_buttons(main_hwnd)
    has_cancel = any('取消' in b['title'] for b in btns)
    has_solve = any('求解' in b['title'] for b in btns)
    
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    
    # 也检查文件修改时间
    modified_files = []
    for f in files_before:
        fpath = os.path.join(fe_dir, f)
        if os.path.exists(fpath):
            mtime = os.path.getmtime(fpath)
            if mtime != files_mtime_before.get(f):
                modified_files.append(f)
    
    return has_cancel, has_solve, new_files, modified_files

# 方法1: BM_CLICK (SendMessage)
print(f"\n[方法1] BM_CLICK (SendMessage)...")
win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
time.sleep(3)

has_cancel, has_solve, new_files, mod_files = check_calculation_started()
print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")

if not has_cancel and not new_files and not mod_files:
    # 方法2: WM_COMMAND (发送到父窗口)
    print(f"\n[方法2] WM_COMMAND (ctrl_id={btn_id})...")
    wp = win32api.MAKELONG(btn_id, 0)  # BN_CLICKED = 0
    win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
    time.sleep(3)
    
    has_cancel, has_solve, new_files, mod_files = check_calculation_started()
    print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")

if not has_cancel and not new_files and not mod_files:
    # 方法3: PostMessage 完整鼠标序列
    print(f"\n[方法3] PostMessage 完整鼠标序列...")
    win32gui.PostMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp_btn)
    time.sleep(0.05)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp_btn)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp_btn)
    time.sleep(0.1)
    # 也发送 BN_CLICKED 通知
    wp = win32api.MAKELONG(btn_id, 0)
    win32gui.PostMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
    time.sleep(3)
    
    has_cancel, has_solve, new_files, mod_files = check_calculation_started()
    print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")

if not has_cancel and not new_files and not mod_files:
    # 方法4: pywinauto
    print(f"\n[方法4] pywinauto click()...")
    try:
        from pywinauto import Application
        app = Application(backend='win32').connect(process=main_pid)
        win = app.window(handle=main_hwnd)
        btn = win.child_window(handle=btn_hwnd)
        btn.click()
        time.sleep(3)
        
        has_cancel, has_solve, new_files, mod_files = check_calculation_started()
        print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")
    except Exception as e:
        print(f"  pywinauto 失败: {e}")

if not has_cancel and not new_files and not mod_files:
    # 方法5: 键盘消息
    print(f"\n[方法5] 键盘消息 (Space/Enter)...")
    win32gui.SendMessage(btn_hwnd, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.1)
    # 空格键
    win32gui.PostMessage(btn_hwnd, win32con.WM_KEYDOWN, win32con.VK_SPACE, 0)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_KEYUP, win32con.VK_SPACE, 0)
    time.sleep(3)
    
    has_cancel, has_solve, new_files, mod_files = check_calculation_started()
    print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")

if not has_cancel and not new_files and not mod_files:
    # 方法6: pywinauto click_input() (模拟真实输入)
    print(f"\n[方法6] pywinauto click_input()...")
    try:
        btn.click_input()
        time.sleep(3)
        
        has_cancel, has_solve, new_files, mod_files = check_calculation_started()
        print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")
    except Exception as e:
        print(f"  click_input 失败: {e}")

if not has_cancel and not new_files and not mod_files:
    # 方法7: 发送 WM_COMMAND 到主窗口 (而非父窗口)
    print(f"\n[方法7] WM_COMMAND 到主窗口...")
    wp = win32api.MAKELONG(btn_id, 0)
    win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, wp, btn_hwnd)
    time.sleep(3)
    
    has_cancel, has_solve, new_files, mod_files = check_calculation_started()
    print(f"  取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")

# ============================================================
# 如果计算已开始，等待完成
# ============================================================
if has_cancel or new_files or mod_files:
    print(f"\n*** 计算可能已开始! 等待完成... ***")
    
    for wait in range(600):  # 最多等10分钟
        time.sleep(1)
        has_cancel, has_solve, new_files, mod_files = check_calculation_started()
        
        if wait % 10 == 0:
            print(f"  [{wait}s] 取消={'有' if has_cancel else '无'} 求解={'有' if has_solve else '无'} 新文件={len(new_files)} 修改文件={len(mod_files)}")
        
        # 计算完成的标志: 取消按钮消失, 求解按钮重新出现
        if has_solve and not has_cancel and wait > 5:
            print(f"\n  *** 计算完成! (耗时 {wait}s) ***")
            break
        if wait >= 600:
            print(f"  超时 (600s)")
            break
    
    # 列出新文件
    files_final = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    final_new = files_final - files_before
    print(f"\n  新增文件 ({len(final_new)} 个):")
    for f in sorted(final_new):
        fpath = os.path.join(fe_dir, f)
        print(f"    {f}: {os.path.getsize(fpath)} bytes")
    
    # 修改的文件
    final_mod = []
    for f in files_before:
        fpath = os.path.join(fe_dir, f)
        if os.path.exists(fpath):
            mtime = os.path.getmtime(fpath)
            if mtime != files_mtime_before.get(f):
                final_mod.append(f)
    print(f"\n  修改文件 ({len(final_mod)} 个):")
    for f in sorted(final_mod):
        print(f"    {f}")
    
    # 保存结果
    with open(r"D:\MotorDesign\Scripts\solve_result.txt", "w") as fout:
        fout.write(f"计算触发: 成功\n")
        fout.write(f"按钮: '{solve_btn['title']}'\n")
        fout.write(f"耗时: {wait}s\n")
        fout.write(f"新增文件: {len(final_new)}\n")
        for fn in sorted(final_new):
            fout.write(f"  {fn}\n")
        fout.write(f"修改文件: {len(final_mod)}\n")
        for fn in sorted(final_mod):
            fout.write(f"  {fn}\n")
    print(f"\n  结果已保存到 solve_result.txt")
else:
    print(f"\n*** 所有方法均未触发计算 ***")
    
    # 保存失败信息
    with open(r"D:\MotorDesign\Scripts\solve_result.txt", "w") as fout:
        fout.write(f"计算触发: 失败\n")
        fout.write(f"尝试了7种方法均未成功\n")
        fout.write(f"按钮: '{solve_btn['title']}' hwnd={solve_btn['hwnd']}\n")
    print(f"  结果已保存到 solve_result.txt")

print("\n=== 完成 ===")
