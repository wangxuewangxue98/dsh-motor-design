#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 命令行选项 + MotorLABruntime 用法探测
+ 查找内部 PID 存储位置
+ 尝试 UI Automation (Accessibility API)
"""
import sys
import os
import time
import subprocess
import win32gui
import win32con
import win32api
import win32process
import ctypes
from ctypes import wintypes

print("=" * 70)
print("Motor-CAD 命令行 + MotorLAB + PID 查找 + UI Automation")
print("=" * 70)

EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
MOTORLAB_EXE = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib\MotorLABruntime.exe"
EXE_DIR = r"D:\Motor-CAD\11_1_5"

# Step 1: Try MotorLABruntime.exe with different arguments
print("\n[1] MotorLABruntime.exe 用法探测...")

# Try without arguments
print("  1a. 无参数:")
try:
    result = subprocess.run([MOTORLAB_EXE], capture_output=True, text=True, timeout=10, cwd=EXE_DIR)
    print(f"    返回码: {result.returncode}")
    print(f"    stdout: '{result.stdout[:500]}'")
    print(f"    stderr: '{result.stderr[:500]}'")
except subprocess.TimeoutExpired:
    print("    超时 (10s)")
except Exception as e:
    print(f"    错误: {e}")

# Try with PID=0
print("\n  1b. PID=0:")
try:
    result = subprocess.run([MOTORLAB_EXE, "BuildModel", "0", "Motor-CAD_11_1_5.exe"], 
                          capture_output=True, text=True, timeout=10, cwd=EXE_DIR)
    print(f"    返回码: {result.returncode}")
    print(f"    stdout: '{result.stdout[:500]}'")
    print(f"    stderr: '{result.stderr[:500]}'")
except subprocess.TimeoutExpired:
    print("    超时 (10s)")
except Exception as e:
    print(f"    错误: {e}")

# Try with --help
print("\n  1c. --help:")
try:
    result = subprocess.run([MOTORLAB_EXE, "--help"], capture_output=True, text=True, timeout=10, cwd=EXE_DIR)
    print(f"    返回码: {result.returncode}")
    print(f"    stdout: '{result.stdout[:500]}'")
    print(f"    stderr: '{result.stderr[:500]}'")
except subprocess.TimeoutExpired:
    print("    超时 (10s)")
except Exception as e:
    print(f"    错误: {e}")

# Step 2: Check for Motor-LAB Python files and scripts
print("\n\n[2] Motor-LAB 目录扫描...")

motorlab_dir = r"D:\Motor-CAD\11_1_5\Motor-LAB"
for root, dirs, files in os.walk(motorlab_dir):
    for f in files:
        if f.endswith(('.py', '.m', '.bat', '.cmd', '.cfg', '.ini', '.json', '.txt', '.xml')):
            fpath = os.path.join(root, f)
            size = os.path.getsize(fpath)
            if size < 50000:
                print(f"  {fpath} ({size} bytes)")

# Step 3: Check for shared memory or temp files
print("\n\n[3] 查找 PID/IPC 相关文件...")

# Check temp directory for Motor-CAD files
import tempfile
temp_dir = tempfile.gettempdir()
print(f"  Temp 目录: {temp_dir}")

motorcad_temp_files = []
for f in os.listdir(temp_dir):
    if 'motor' in f.lower() or 'mcad' in f.lower() or 'motorlab' in f.lower():
        motorcad_temp_files.append(os.path.join(temp_dir, f))

if motorcad_temp_files:
    print(f"  Motor-CAD 临时文件: {motorcad_temp_files}")
else:
    print("  无 Motor-CAD 临时文件")

# Check for .dat, .pid, .lock files in Motor-CAD directory
print("\n  Motor-CAD 目录中的数据文件:")
for f in os.listdir(EXE_DIR):
    fpath = os.path.join(EXE_DIR, f)
    if os.path.isfile(fpath):
        ext = os.path.splitext(f)[1].lower()
        if ext in ('.dat', '.pid', '.lock', '.tmp', '.cfg', '.ini', '.json', '.xml', '.bin'):
            size = os.path.getsize(fpath)
            print(f"    {f:40s} {size:10d} bytes")

# Check AppData
appdata = os.path.expanduser("~\\AppData")
for root, dirs, files in os.walk(os.path.join(appdata, "Local")):
    for f in files:
        if 'motor' in f.lower() or 'mcad' in f.lower():
            print(f"  AppData\\Local: {os.path.join(root, f)}")
    # Don't recurse too deep
    if root.count(os.sep) > 5:
        dirs.clear()

for root, dirs, files in os.walk(os.path.join(appdata, "Roaming")):
    for f in files:
        if 'motor' in f.lower() or 'mcad' in f.lower():
            print(f"  AppData\\Roaming: {os.path.join(root, f)}")
    if root.count(os.sep) > 5:
        dirs.clear()

# Step 4: Try UI Automation (Accessibility API)
print("\n\n[4] UI Automation (MSAA) 测试...")

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
print(f"  主窗体: hwnd={main_hwnd}")

# Try using AccessibleObjectFromWindow
try:
    # Load oleacc.dll
    oleacc = ctypes.windll.oleacc
    
    # AccessibleObjectFromWindow(hwnd, dwId, riid, ppvObject)
    # OBJID_CLIENT = 0xFFFFFFFC
    # IID_IAccessible = {618716e0-3c3d-11cf-810c-00aa00389b71}
    
    pAcc = ctypes.POINTER(ctypes.c_void_p)()
    hr = oleacc.AccessibleObjectFromWindow(
        main_hwnd,
        ctypes.c_long(0xFFFFFFFC),  # OBJID_CLIENT
        ctypes.byref(ctypes.c_void_p()),  # This won't work properly with ctypes
        ctypes.byref(pAcc)
    )
    print(f"  AccessibleObjectFromWindow HR: {hr}")
except Exception as e:
    print(f"  MSAA 失败: {e}")

# Try using UI Automation via comtypes
try:
    import comtypes
    import comtypes.client
    
    # Try to get UI Automation
    try:
        uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}")
        print(f"  UI Automation 创建成功")
    except:
        print(f"  UI Automation 不可用")
except ImportError:
    print("  comtypes 未安装, 尝试安装...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "comtypes"], 
                      capture_output=True, timeout=30)
        import comtypes
        import comtypes.client
        print("  comtypes 安装成功")
    except Exception as e:
        print(f"  comtypes 安装失败: {e}")

# Step 5: Try using PostMessage with WM_LBUTTONDOWN to PageControl
print("\n\n[5] PostMessage 点击标签 (带前台窗口)...")

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
        'hwnd': int(hwnd), 'class': cls_name, 'title': title,
        'visible': visible, 'ctrl_id': ctrl_id, 'rect': list(rect),
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect, None)

page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
main_pc = max(page_controls, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"  PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# Attach thread input to allow SetForegroundWindow
foreground_hwnd = win32gui.GetForegroundWindow()
_, fg_pid = win32process.GetWindowThreadProcessId(foreground_hwnd)
fg_tid, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)
mc_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)

print(f"  前台窗口 PID: {fg_pid}, Motor-CAD TID: {mc_tid}")

# Try to bring Motor-CAD to foreground
try:
    # AttachThreadInput trick
    ctypes.windll.user32.AttachThreadInput(fg_tid, mc_tid, True)
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(main_hwnd)
    ctypes.windll.user32.AttachThreadInput(fg_tid, mc_tid, False)
    time.sleep(1)
    print("  Motor-CAD 已置于前台")
except Exception as e:
    print(f"  SetForegroundWindow 失败: {e}")

# Now try clicking on tabs using PostMessage
# Tab bar is at the top of the PageControl (y = ~12 in client coords)
# With 13 tabs, estimate positions
# But tabs have different widths - try a range of positions

# First, find which tab is currently visible
vis_tabs = [c for c in all_children if c['class'] == 'TTabSheet' and c['visible']]
print(f"  当前可见标签: {[t['title'] for t in vis_tabs]}")

# Try clicking at different x positions
# The tab bar starts at x=5 (in client coords) and has some padding
# Let's try positions from x=20 to x=980 in steps of 20
print("\n  扫描标签栏位置:")
for x_pos in range(20, 980, 40):
    y_pos = 12  # Tab bar y position in client coords
    
    # Send click via PostMessage
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.05)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.3)
    
    # Check what's visible
    vis_tabs_now = []
    def check_vis(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TTabSheet' and visible and title:
            vis_tabs_now.append(title)
        return True
    win32gui.EnumChildWindows(main_hwnd, check_vis, None)
    
    # Also check for buttons
    vis_btns = []
    def check_btns(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton') and visible and title:
            vis_btns.append(title)
        return True
    win32gui.EnumChildWindows(main_hwnd, check_btns, None)
    
    current = vis_tabs_now[0] if vis_tabs_now else "?"
    if vis_btns:
        print(f"    x={x_pos:4d}: 标签='{current}' 按钮={vis_btns}")
    elif current != '几何':
        print(f"    x={x_pos:4d}: 标签='{current}'")
    
    # If we see Lab or calculation tabs, look deeper
    if any(kw in current for kw in ['实验室', 'Lab', '输出', '计算', 'Electromagnetic', 'Thermal']):
        print(f"    *** 发现关键标签: '{current}' ***")
        if vis_btns:
            print(f"    按钮: {vis_btns}")

# Return to first tab
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, win32api.MAKELONG(20, 12))
time.sleep(0.05)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, win32api.MAKELONG(20, 12))

print("\n" + "=" * 70)
print("完成!")
print("=" * 70)
