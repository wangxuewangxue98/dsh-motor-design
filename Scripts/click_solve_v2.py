#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 求解按钮点击 v2

1. 关闭 Message Display (模态阻塞)
2. 切换到求解设置标签
3. 点击求解热模型按钮
4. 监控 FEResultsData
5. 读取 Message Display 获取计算日志
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
# 关闭 Message Display (如果可见)
# ============================================================
msg_hwnd = None
memo_hwnd = None

def find_msg(hwnd, p):
    global msg_hwnd, memo_hwnd
    try:
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TMessageDisplayForm':
            msg_hwnd = hwnd
        elif cls == 'TMemo' and msg_hwnd:
            memo_hwnd = hwnd
    except:
        pass
    return True

win32gui.EnumWindows(find_msg, None)

# 读取 Message Display 内容
if msg_hwnd and win32gui.IsWindowVisible(msg_hwnd):
    print("\n=== Message Display 内容 ===")
    if memo_hwnd:
        length = win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if length > 0:
            buffer = win32gui.PyMakeBuffer(length + 2)
            win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXT, length + 2, buffer)
            text = buffer[:length].decode('gbk', errors='replace')
            lines = text.strip().split('\n')
            for line in lines[-15:]:
                print(f"  {line.strip()}")
    
    # 关闭 Message Display
    print("\n关闭 Message Display...")
    win32gui.PostMessage(msg_hwnd, win32con.WM_CLOSE, 0, 0)
    time.sleep(2)

# 检查前台窗口
fg = win32gui.GetForegroundWindow()
print(f"\n前台窗口: hwnd={fg}, cls={win32gui.GetClassName(fg) if fg else 'N/A'}")
print(f"主窗口在前台: {fg == main_hwnd}")

# ============================================================
# 切换到求解设置标签
# ============================================================
print("\n=== 切换到求解设置标签 ===")

pcs = []
def find_pc(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        if cls == 'TPageControl' and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            size = (rect[2]-rect[0]) * (rect[3]-rect[1])
            pcs.append({'hwnd': hwnd, 'size': size})
    except:
        pass
    return True

win32gui.EnumChildWindows(main_hwnd, find_pc, None)
pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
lp = win32api.MAKELONG(240, 12)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
time.sleep(0.1)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
time.sleep(1)

# ============================================================
# 找到求解按钮
# ============================================================
print("\n=== 查找求解按钮 ===")

btns = []
def find_btns(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            btns.append({'hwnd': hwnd, 'title': title, 'rect': rect, 'ctrl_id': ctrl_id,
                        'parent': win32gui.GetParent(hwnd)})
    except:
        pass
    return True

win32gui.EnumChildWindows(main_hwnd, find_btns, None)

solve_btn = None
for b in btns:
    print(f"  按钮: '{b['title']}' hwnd={b['hwnd']} rect={b['rect']}")
    if '求解' in b['title']:
        solve_btn = b

if not solve_btn:
    print("*** 求解按钮未找到! ***")
    sys.exit(1)

# ============================================================
# FEResultsData 监控
# ============================================================
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
mtimes_before = {}
for f in files_before:
    mtimes_before[f] = os.path.getmtime(os.path.join(fe_dir, f))
print(f"\nFEResultsData: {len(files_before)} 个文件")

# ============================================================
# 点击求解按钮
# ============================================================
btn_hwnd = solve_btn['hwnd']
btn_rect = solve_btn['rect']
btn_id = solve_btn['ctrl_id']
parent = solve_btn['parent']
cx = (btn_rect[2] - btn_rect[0]) // 2
cy = (btn_rect[3] - btn_rect[1]) // 2
lp_btn = win32api.MAKELONG(cx, cy)

print(f"\n=== 点击求解按钮: '{solve_btn['title']}' ===")
print(f"  hwnd={btn_hwnd}, rect={btn_rect}, ctrl_id={btn_id}")
print(f"  parent={parent} ({win32gui.GetClassName(parent)})")

def check_result():
    btns2 = []
    def cb(h, p):
        try:
            cls = win32gui.GetClassName(h)
            title = win32gui.GetWindowText(h)
            if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(h):
                btns2.append(title)
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, cb, None)
    has_cancel = any('取消' in t for t in btns2)
    has_solve = any('求解' in t for t in btns2)
    files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    new_files = files_now - files_before
    mod_files = []
    for f in files_before:
        fp = os.path.join(fe_dir, f)
        if os.path.exists(fp):
            if os.path.getmtime(fp) != mtimes_before.get(f):
                mod_files.append(f)
    return has_cancel, has_solve, new_files, mod_files

# 方法1: BM_CLICK (SendMessage)
print(f"\n[1] BM_CLICK (SendMessage)...")
win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
time.sleep(3)
hc, hs, nf, mf = check_result()
print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")

if not hc and not nf and not mf:
    # 方法2: PostMessage 完整鼠标序列
    print(f"\n[2] PostMessage 完整鼠标序列...")
    win32gui.PostMessage(btn_hwnd, win32con.WM_MOUSEMOVE, 0, lp_btn)
    time.sleep(0.05)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp_btn)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lp_btn)
    time.sleep(3)
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")

if not hc and not nf and not mf:
    # 方法3: pywinauto
    print(f"\n[3] pywinauto click()...")
    try:
        from pywinauto import Application
        app = Application(backend='win32').connect(process=main_pid)
        win = app.window(handle=main_hwnd)
        btn = win.child_window(handle=btn_hwnd)
        btn.click()
        time.sleep(3)
        hc, hs, nf, mf = check_result()
        print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")
    except Exception as e:
        print(f"  失败: {e}")

if not hc and not nf and not mf:
    # 方法4: WM_COMMAND
    print(f"\n[4] WM_COMMAND (parent + main)...")
    wp = win32api.MAKELONG(btn_id, 0)
    win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
    time.sleep(1)
    win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, wp, btn_hwnd)
    time.sleep(3)
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")

if not hc and not nf and not mf:
    # 方法5: 键盘消息
    print(f"\n[5] 键盘消息 (Space)...")
    win32gui.SendMessage(btn_hwnd, win32con.WM_SETFOCUS, 0, 0)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_KEYDOWN, win32con.VK_SPACE, 0)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_KEYUP, win32con.VK_SPACE, 0)
    time.sleep(3)
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")

if not hc and not nf and not mf:
    # 方法6: pywinauto click_input (真实鼠标)
    print(f"\n[6] pywinauto click_input()...")
    try:
        btn.click_input()
        time.sleep(3)
        hc, hs, nf, mf = check_result()
        print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")
    except Exception as e:
        print(f"  失败: {e}")

# ============================================================
# 如果计算已开始，等待完成
# ============================================================
if hc or nf or mf:
    print(f"\n*** 计算已开始! 等待完成... ***")
    for wait in range(600):
        time.sleep(1)
        hc, hs, nf, mf = check_result()
        if wait % 10 == 0:
            print(f"  [{wait}s] 取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")
        if hs and not hc and wait > 5:
            print(f"  *** 计算完成! ({wait}s) ***")
            break
        if wait >= 600:
            print(f"  超时")
            break
    
    files_final = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    final_new = files_final - files_before
    final_mod = []
    for f in files_before:
        fp = os.path.join(fe_dir, f)
        if os.path.exists(fp) and os.path.getmtime(fp) != mtimes_before.get(f):
            final_mod.append(f)
    print(f"\n  新增: {len(final_new)}, 修改: {len(final_mod)}")
    for f in sorted(final_new):
        print(f"    新: {f}")
    for f in sorted(final_mod):
        print(f"    改: {f}")
    
    # 保存结果
    with open(r"D:\MotorDesign\Scripts\solve_result.txt", "w") as fout:
        fout.write(f"计算触发: 成功\n")
        fout.write(f"耗时: {wait}s\n")
        fout.write(f"新增文件: {len(final_new)}\n")
        for fn in sorted(final_new):
            fout.write(f"  {fn}\n")
        fout.write(f"修改文件: {len(final_mod)}\n")
        for fn in sorted(final_mod):
            fout.write(f"  {fn}\n")
else:
    print(f"\n*** 所有方法均未触发计算 ***")

# ============================================================
# 读取 Message Display 获取日志
# ============================================================
print(f"\n=== Message Display 最新内容 ===")
if memo_hwnd and win32gui.IsWindow(memo_hwnd):
    length = win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
    if length > 0:
        buffer = win32gui.PyMakeBuffer(length + 2)
        win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXT, length + 2, buffer)
        text = buffer[:length].decode('gbk', errors='replace')
        lines = text.strip().split('\n')
        for line in lines[-30:]:
            print(f"  {line.strip()}")

print("\n=== 完成 ===")
