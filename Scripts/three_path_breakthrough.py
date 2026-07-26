#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 三路并行突破:
1. MotorLABruntime.exe + Windows PID 测试
2. 导航到 Lab 标签页查找计算按钮
3. .mes 结果文件解析
4. 键盘快捷键测试
"""
import sys
import os
import time
import struct
import subprocess
import win32gui
import win32con
import win32api
import win32process

print("=" * 70)
print("Motor-CAD 三路突破")
print("=" * 70)

# ============================================================
# Path 1: MotorLABruntime.exe + Windows PID
# ============================================================
print("\n" + "=" * 50)
print("[路径1] MotorLABruntime.exe + Windows PID")
print("=" * 50)

motorlab_exe = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib\MotorLABruntime.exe"
exe_name = "Motor-CAD_11_1_5.exe"
exe_dir = r"D:\Motor-CAD\11_1_5"

# Get Windows PIDs of running Motor-CAD instances
motorcad_pids = []

def find_mc_pids(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    if cls_name == 'TMainForm':
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        motorcad_pids.append(pid)
    return True

win32gui.EnumWindows(find_mc_pids, None)
print(f"  Motor-CAD PIDs: {motorcad_pids}")

if os.path.exists(motorlab_exe):
    print(f"  MotorLABruntime.exe 存在: {motorlab_exe}")
    print(f"  文件大小: {os.path.getsize(motorlab_exe)} bytes")
    
    # Try each PID
    for pid in motorcad_pids:
        print(f"\n  --- 尝试 PID={pid} ---")
        
        # Try BuildModel first (should be fast)
        cmd = [motorlab_exe, "BuildModel", str(pid), exe_name]
        print(f"  命令: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                cwd=exe_dir,
                capture_output=True,
                text=True,
                timeout=30
            )
            print(f"  返回码: {result.returncode}")
            print(f"  stdout: {result.stdout[:500]}")
            print(f"  stderr: {result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            print(f"  超时 (30s)")
        except Exception as e:
            print(f"  错误: {e}")
        
        # Check if any result files were created
        results_dir = os.path.join(exe_dir, "FEResultsData")
        if os.path.exists(results_dir):
            files_before = set(os.listdir(results_dir))
            time.sleep(2)
            files_after = set(os.listdir(results_dir))
            new_files = files_after - files_before
            if new_files:
                print(f"  新文件: {new_files}")
            else:
                print(f"  无新文件")
else:
    print(f"  MotorLABruntime.exe 不存在!")

# Also check if there's a MotorLABruntime in other locations
print("\n  搜索 MotorLABruntime.exe...")
for root, dirs, files in os.walk(r"D:\Motor-CAD\11_1_5\Motor-LAB"):
    for f in files:
        if 'runtime' in f.lower() or 'motorlab' in f.lower():
            print(f"    {os.path.join(root, f)}")

# ============================================================
# Path 2: Navigate to Lab tab and find calculation buttons
# ============================================================
print("\n\n" + "=" * 50)
print("[路径2] 导航到 Lab 标签页")
print("=" * 50)

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

if main_hwnd:
    print(f"  主窗体: hwnd={main_hwnd}")
    
    # Find all TPageControl controls
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
            'width': rect[2] - rect[0],
            'height': rect[3] - rect[1],
        })
        return True
    
    win32gui.EnumChildWindows(main_hwnd, collect, None)
    
    # Find the main TPageControl (the one that's visible and large)
    page_controls = [c for c in all_children if c['class'] == 'TPageControl' and c['visible']]
    print(f"  可见 TPageControl: {len(page_controls)} 个")
    for pc in page_controls:
        print(f"    hwnd={pc['hwnd']} id={pc['ctrl_id']} {pc['width']}x{pc['height']} rect={pc['rect']}")
    
    # Find the main TTabSheet that's visible
    vis_tabs = [c for c in all_children if c['class'] == 'TTabSheet' and c['visible']]
    print(f"  可见 TTabSheet: {len(vis_tabs)} 个")
    for t in vis_tabs:
        print(f"    '{t['title']}' hwnd={t['hwnd']} id={t['ctrl_id']}")
    
    # Try to switch to the Lab tab by clicking on the tab control
    # First, let's find ALL tabs (including hidden ones) to identify the Lab tab
    all_tabs = [c for c in all_children if c['class'] == 'TTabSheet']
    lab_tabs = [t for t in all_tabs if 'lab' in t['title'].lower() or '实验室' in t['title'] or 'Lab' in t['title']]
    print(f"\n  Lab 相关标签页: {len(lab_tabs)} 个")
    for t in lab_tabs:
        vis = "可见" if t['visible'] else "隐藏"
        print(f"    [{vis}] '{t['title']}' hwnd={t['hwnd']} id={t['ctrl_id']} parent_rect={t['rect']}")
    
    # The main page control is the first visible one
    if page_controls:
        main_pc = page_controls[0]
        pc_hwnd = main_pc['hwnd']
        
        # Get the number of tabs
        tab_count = win32gui.SendMessage(pc_hwnd, win32con.TCM_GETITEMCOUNT, 0, 0)
        print(f"\n  主 PageControl 标签数: {tab_count}")
        
        # Get tab names
        print("  标签列表:")
        for i in range(min(tab_count, 30)):
            # TCM_GETITEM to get tab text
            # This is complex with pywin32, try alternative
            try:
                # Use TCM_GETITEMRECT to get tab position
                buf = win32gui.PyMakeBuffer(16)  # RECT structure
                win32gui.SendMessage(pc_hwnd, win32con.TCM_GETITEMRECT, i, buf)
                rect_data = buf[:]
                if len(rect_data) >= 16:
                    left, top, right, bottom = struct.unpack('4i', rect_data[:16])
                    print(f"    [{i}] rect=({left},{top},{right},{bottom})")
            except Exception as e:
                print(f"    [{i}] error: {e}")
        
        # Try to select different tabs and find calculation buttons
        # The main tabs are typically: Geometry, InputData, Cooling, Materials, Lab, Output
        # We want to navigate to the Lab tab
        
        # Let's try clicking on different tabs by position
        # First, get the screen coordinates of the PageControl
        pc_rect = main_pc['rect']
        pc_left, pc_top = pc_rect[0], pc_rect[1]
        
        print(f"\n  PageControl 屏幕坐标: ({pc_left}, {pc_top})")
        
        # Tab items are at the top of the PageControl
        # Try clicking at different x positions to switch tabs
        tab_y = pc_top + 12  # Tab labels are near the top
        
        # Try to find the Lab tab (usually around tab index 5-8)
        # Let's click on each tab and see what becomes visible
        for tab_idx in range(min(tab_count, 20)):
            # Get tab rect
            try:
                buf = win32gui.PyMakeBuffer(16)
                win32gui.SendMessage(pc_hwnd, win32con.TCM_GETITEMRECT, tab_idx, buf)
                rect_data = buf[:]
                if len(rect_data) >= 16:
                    left, top, right, bottom = struct.unpack('4i', rect_data[:16])
                    # Convert to screen coordinates
                    screen_x = pc_left + (left + right) // 2
                    screen_y = pc_top + (top + bottom) // 2
                    
                    # Click on this tab
                    print(f"\n    点击标签 [{tab_idx}] at ({screen_x}, {screen_y})...")
                    
                    # Send click
                    win32api.SetCursorPos((screen_x, screen_y))
                    time.sleep(0.3)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
                    time.sleep(0.1)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)
                    time.sleep(1)
                    
                    # Check what tabs are now visible
                    vis_tabs_now = []
                    def check_visible(hwnd, lparam):
                        cls_name = win32gui.GetClassName(hwnd)
                        title = win32gui.GetWindowText(hwnd)
                        visible = win32gui.IsWindowVisible(hwnd)
                        if cls_name == 'TTabSheet' and visible and title:
                            vis_tabs_now.append(title)
                        return True
                    
                    win32gui.EnumChildWindows(main_hwnd, check_visible, None)
                    print(f"    可见标签页: {vis_tabs_now}")
                    
                    # Check for buttons
                    vis_buttons = []
                    def check_buttons(hwnd, lparam):
                        cls_name = win32gui.GetClassName(hwnd)
                        title = win32gui.GetWindowText(hwnd)
                        visible = win32gui.IsWindowVisible(hwnd)
                        enabled = win32gui.IsWindowEnabled(hwnd)
                        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
                            try:
                                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
                            except:
                                ctrl_id = 0
                            vis_buttons.append({
                                'title': title,
                                'hwnd': int(hwnd),
                                'ctrl_id': ctrl_id,
                                'enabled': enabled,
                            })
                        return True
                    
                    win32gui.EnumChildWindows(main_hwnd, check_buttons, None)
                    if vis_buttons:
                        print(f"    可见按钮: {len(vis_buttons)} 个")
                        for b in vis_buttons:
                            en = "启用" if b['enabled'] else "禁用"
                            print(f"      [{en}] '{b['title']}' id={b['ctrl_id']} hwnd={b['hwnd']}")
                    
                    # If we found calculation-related buttons, stop
                    calc_keywords = ['calc', 'run', 'solve', 'analy', 'start', '计算', '运行', '求解', '分析', '开始']
                    calc_buttons = [b for b in vis_buttons if any(kw.lower() in b['title'].lower() for kw in calc_keywords)]
                    if calc_buttons:
                        print(f"\n    *** 找到计算按钮! ***")
                        for b in calc_buttons:
                            print(f"    -> '{b['title']}' hwnd={b['hwnd']} id={b['ctrl_id']}")
                        break
                        
            except Exception as e:
                print(f"    [{tab_idx}] error: {e}")
        
        # Return to first tab
        buf = win32gui.PyMakeBuffer(16)
        win32gui.SendMessage(pc_hwnd, win32con.TCM_GETITEMRECT, 0, buf)
        rect_data = buf[:]
        if len(rect_data) >= 16:
            left, top, right, bottom = struct.unpack('4i', rect_data[:16])
            screen_x = pc_left + (left + right) // 2
            screen_y = pc_top + (top + bottom) // 2
            win32api.SetCursorPos((screen_x, screen_y))
            time.sleep(0.2)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, screen_x, screen_y, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, screen_x, screen_y, 0, 0)

# ============================================================
# Path 3: Parse .mes result files
# ============================================================
print("\n\n" + "=" * 50)
print("[路径3] 解析 .mes 结果文件")
print("=" * 50)

fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"

# Read the smallest .mes file first
mes_files = [f for f in os.listdir(fe_dir) if f.endswith('.mes')]
mes_files.sort(key=lambda f: os.path.getsize(os.path.join(fe_dir, f)))

print(f"  .mes 文件 ({len(mes_files)} 个, 按大小排序):")
for f in mes_files:
    size = os.path.getsize(os.path.join(fe_dir, f))
    print(f"    {f:40s} {size:10d} bytes")

# Try to parse the smallest file
if mes_files:
    smallest = mes_files[0]
    fpath = os.path.join(fe_dir, smallest)
    print(f"\n  解析最小文件: {smallest}")
    
    with open(fpath, 'rb') as f:
        data = f.read()
    
    print(f"  文件大小: {len(data)} bytes")
    print(f"  前 200 字节 (hex):")
    for i in range(0, min(200, len(data)), 16):
        hex_part = ' '.join(f'{b:02x}' for b in data[i:i+16])
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
        print(f"    {i:4d}: {hex_part:48s} {ascii_part}")
    
    # Try to find text strings in the file
    print(f"\n  文本字符串提取:")
    text_parts = []
    current_text = b""
    for byte in data:
        if 32 <= byte < 127:
            current_text += bytes([byte])
        else:
            if len(current_text) >= 4:
                text_parts.append(current_text.decode('ascii', errors='replace'))
            current_text = b""
    if current_text and len(current_text) >= 4:
        text_parts.append(current_text.decode('ascii', errors='replace'))
    
    print(f"  找到 {len(text_parts)} 个文本字符串 (前50个):")
    for i, t in enumerate(text_parts[:50]):
        print(f"    [{i:3d}] '{t[:80]}'")

# Also check .ugf files
ugf_files = [f for f in os.listdir(fe_dir) if f.endswith('.ugf')]
if ugf_files:
    print(f"\n  .ugf 文件 ({len(ugf_files)} 个):")
    for f in ugf_files:
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        print(f"    {f:40s} {size:10d} bytes")
    
    # Parse smallest .ugf
    ugf_files.sort(key=lambda f: os.path.getsize(os.path.join(fe_dir, f)))
    if ugf_files:
        smallest_ugf = ugf_files[0]
        fpath = os.path.join(fe_dir, smallest_ugf)
        print(f"\n  解析: {smallest_ugf}")
        with open(fpath, 'rb') as f:
            data = f.read()
        print(f"  前 100 字节 (hex):")
        for i in range(0, min(100, len(data)), 16):
            hex_part = ' '.join(f'{b:02x}' for b in data[i:i+16])
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
            print(f"    {i:4d}: {hex_part:48s} {ascii_part}")

# Check for .bh file
bh_file = os.path.join(fe_dir, "autofile.bh")
if os.path.exists(bh_file):
    print(f"\n  autofile.bh 内容:")
    with open(bh_file, 'r', encoding='latin-1') as f:
        content = f.read()
    print(f"  {content[:500]}")

print("\n" + "=" * 70)
print("三路突破完成!")
print("=" * 70)
