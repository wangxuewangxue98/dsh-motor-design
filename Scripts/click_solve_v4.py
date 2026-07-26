#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 按钮点击 v4 — AttachThreadInput + SetFocus + SendInput

关键: 即使按钮可见, BM_CLICK/PostMessage 无效
尝试: 真正设置焦点 (AttachThreadInput) + SendInput 键盘事件
"""
import sys
import os
import time
import struct
import ctypes
import win32gui
import win32con
import win32api
import win32process

print(f"Python: {struct.calcsize('P')*8}位")

# ============================================================
# 找到 Motor-CAD
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

# 获取主窗口线程
main_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)
current_tid = win32api.GetCurrentThreadId()
print(f"  主窗口线程: {main_tid}, 当前线程: {current_tid}")

# ============================================================
# 切换到求解设置标签
# ============================================================
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
btns = []
def find_btns(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            btns.append({'hwnd': hwnd, 'title': title, 'rect': rect})
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

btn_hwnd = solve_btn['hwnd']
btn_rect = solve_btn['rect']
print(f"\n求解按钮: '{solve_btn['title']}' hwnd={btn_hwnd}")

# ============================================================
# FEResultsData 监控
# ============================================================
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
mtimes_before = {}
for f in files_before:
    mtimes_before[f] = os.path.getmtime(os.path.join(fe_dir, f))
print(f"FEResultsData: {len(files_before)} 个文件")

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
        if os.path.exists(fp) and os.path.getmtime(fp) != mtimes_before.get(f):
            mod_files.append(f)
    return has_cancel, has_solve, new_files, mod_files

# ============================================================
# 方法1: AttachThreadInput + SetFocus + SendInput (键盘)
# ============================================================
print(f"\n[方法1] AttachThreadInput + SetFocus + SendInput...")

# Attach 到 Motor-CAD 的输入队列
attached = False
try:
    win32process.AttachThreadInput(current_tid, main_tid, True)
    attached = True
    print(f"  AttachThreadInput: 成功")
except Exception as e:
    print(f"  AttachThreadInput 失败: {e}")

if attached:
    # 设置前台窗口
    try:
        ctypes.windll.user32.SetForegroundWindow(main_hwnd)
        time.sleep(0.5)
    except:
        pass
    
    # 设置焦点到按钮
    try:
        old_focus = ctypes.windll.user32.SetFocus(btn_hwnd)
        print(f"  SetFocus: old_focus={old_focus}")
        time.sleep(0.3)
    except Exception as e:
        print(f"  SetFocus 失败: {e}")
    
    # 用 SendInput 发送空格键
    print(f"  发送空格键...")
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_SPACE = 0x20
    
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', ctypes.c_ushort),
                    ('wScan', ctypes.c_ushort),
                    ('dwFlags', ctypes.c_ulong),
                    ('time', ctypes.c_ulong),
                    ('dwExtraInfo', ctypes.c_void_p)]
    
    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [('ki', KEYBDINPUT)]
        _anonymous_ = ('_input',)
        _fields_ = [('type', ctypes.c_ulong), ('_input', _INPUT)]
    
    # Key down
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = VK_SPACE
    inp.ki.dwFlags = 0
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    
    time.sleep(0.1)
    
    # Key up
    inp2 = INPUT()
    inp2.type = INPUT_KEYBOARD
    inp2.ki.wVk = VK_SPACE
    inp2.ki.dwFlags = KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(INPUT))
    
    time.sleep(3)
    
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")
    
    # 解除 Attach
    win32process.AttachThreadInput(current_tid, main_tid, False)

# ============================================================
# 方法2: AttachThreadInput + 真实鼠标点击 (SendInput)
# ============================================================
if not hc and not nf and not mf:
    print(f"\n[方法2] AttachThreadInput + SendInput 鼠标点击...")
    
    try:
        win32process.AttachThreadInput(current_tid, main_tid, True)
        attached = True
    except:
        attached = False
    
    if attached:
        # 设置前台
        ctypes.windll.user32.SetForegroundWindow(main_hwnd)
        time.sleep(0.5)
        
        # 计算按钮屏幕坐标
        cx = (btn_rect[0] + btn_rect[2]) // 2
        cy = (btn_rect[1] + btn_rect[3]) // 2
        print(f"  按钮中心: ({cx}, {cy})")
        
        # 移动鼠标到按钮中心
        ctypes.windll.user32.SetCursorPos(cx, cy)
        time.sleep(0.3)
        
        # SendInput 鼠标点击
        INPUT_MOUSE = 0
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                        ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                        ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_void_p)]
        
        class INPUT_M(ctypes.Structure):
            class _INPUT(ctypes.Union):
                _fields_ = [('mi', MOUSEINPUT)]
            _anonymous_ = ('_input',)
            _fields_ = [('type', ctypes.c_ulong), ('_input', _INPUT)]
        
        # Mouse down
        inp = INPUT_M()
        inp.type = INPUT_MOUSE
        inp.mi.dx = 0
        inp.mi.dy = 0
        inp.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT_M))
        
        time.sleep(0.1)
        
        # Mouse up
        inp2 = INPUT_M()
        inp2.type = INPUT_MOUSE
        inp2.mi.dx = 0
        inp2.mi.dy = 0
        inp2.mi.dwFlags = MOUSEEVENTF_LEFTUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp2), ctypes.sizeof(INPUT_M))
        
        time.sleep(3)
        
        hc, hs, nf, mf = check_result()
        print(f"  取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)} 修改={len(mf)}")
        
        win32process.AttachThreadInput(current_tid, main_tid, False)

# ============================================================
# 方法3: 发送 WM_PARENTNOTIFY (模拟按钮点击通知)
# ============================================================
if not hc and not nf and not mf:
    print(f"\n[方法3] WM_PARENTNOTIFY...")
    parent = win32gui.GetParent(btn_hwnd)
    # WM_PARENTNOTIFY with WM_COMMAND
    wp = win32api.MAKELONG(win32con.WM_COMMAND, btn_hwnd)
    win32gui.SendMessage(parent, win32con.WM_PARENTNOTIFY, wp, btn_hwnd)
    time.sleep(3)
    
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 新文件={len(nf)} 修改={len(mf)}")

# ============================================================
# 方法4: 发送 WM_COMMAND 到整个父窗口链
# ============================================================
if not hc and not nf and not mf:
    print(f"\n[方法4] WM_COMMAND 到整个父窗口链...")
    btn_id = win32api.GetWindowLong(btn_hwnd, win32con.GWL_ID)
    wp = win32api.MAKELONG(btn_id, 0)  # BN_CLICKED = 0
    
    h = btn_hwnd
    chain = []
    while h:
        cls = win32gui.GetClassName(h)
        chain.append({'hwnd': h, 'cls': cls})
        h = win32gui.GetParent(h)
    
    for c in chain:
        print(f"  发送 WM_COMMAND 到 {c['cls']} (hwnd={c['hwnd']})...")
        win32gui.SendMessage(c['hwnd'], win32con.WM_COMMAND, wp, btn_hwnd)
        time.sleep(0.5)
    
    time.sleep(3)
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 新文件={len(nf)} 修改={len(mf)}")

# ============================================================
# 方法5: 尝试不同的 BN_CLICKED 代码
# ============================================================
if not hc and not nf and not mf:
    print(f"\n[方法5] 不同 BN_CLICKED 代码...")
    parent = win32gui.GetParent(btn_hwnd)
    btn_id = win32api.GetWindowLong(btn_hwnd, win32con.GWL_ID)
    
    # BN_CLICKED = 0, 但尝试其他值
    for notify_code in [0, 1, 2, 3, 4, 5, 6]:
        wp = win32api.MAKELONG(btn_id, notify_code)
        win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
        time.sleep(0.3)
    
    # 也尝试用 btn_hwnd 作为 ctrl_id
    for notify_code in [0]:
        wp = win32api.MAKELONG(btn_hwnd & 0xFFFF, notify_code)
        win32gui.SendMessage(parent, win32con.WM_COMMAND, wp, btn_hwnd)
        time.sleep(0.3)
    
    time.sleep(3)
    hc, hs, nf, mf = check_result()
    print(f"  取消={'有' if hc else '无'} 新文件={len(nf)} 修改={len(mf)}")

# ============================================================
# 方法6: 检查 Message Display 内容 (可能有错误信息)
# ============================================================
print(f"\n[方法6] 检查 Message Display...")
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

if memo_hwnd and win32gui.IsWindow(memo_hwnd):
    length = win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
    if length > 0:
        buffer = win32gui.PyMakeBuffer(length + 2)
        win32gui.SendMessage(memo_hwnd, win32con.WM_GETTEXT, length + 2, buffer)
        text = buffer[:length].decode('gbk', errors='replace')
        lines = text.strip().split('\n')
        print(f"  Message Display ({len(lines)} 行):")
        for line in lines[-20:]:
            print(f"    {line.strip()}")
    else:
        print("  Message Display 为空")
else:
    print("  Message Display 未找到")

# ============================================================
# 如果计算开始，等待完成
# ============================================================
if hc or nf or mf:
    print(f"\n*** 计算已开始! 等待完成... ***")
    for wait in range(600):
        time.sleep(1)
        hc, hs, nf, mf = check_result()
        if wait % 10 == 0:
            print(f"  [{wait}s] 取消={'有' if hc else '无'} 求解={'有' if hs else '无'} 新文件={len(nf)}")
        if hs and not hc and wait > 5:
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
    
    with open(r"D:\MotorDesign\Scripts\solve_result.txt", "w") as fout:
        fout.write(f"计算触发: 成功\n耗时: {wait}s\n新增文件: {len(final_new)}\n")
        for fn in sorted(final_new):
            fout.write(f"  {fn}\n")
else:
    print(f"\n*** 所有方法均未触发计算 ***")
    with open(r"D:\MotorDesign\Scripts\solve_result.txt", "w") as fout:
        fout.write(f"计算触发: 失败\n尝试了6种方法\n")

print("\n=== 完成 ===")
