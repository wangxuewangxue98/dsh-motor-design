#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 半自动求解工作流

由于 COM 和按钮点击都不可行，实现半自动工作流:
1. 自动修改 .mot 文件参数
2. 自动启动 Motor-CAD 加载文件
3. 自动切换到求解标签页
4. 通知用户手动点击求解
5. 监控 FEResultsData 目录变化
6. 尝试从"输出数据"标签页读取结果

同时尝试通过 UI 自动化读取"输出数据"标签页的温度结果
"""
import sys
import os
import time
import struct
import shutil
import json
import win32gui
import win32con
import win32api
import win32process

print(f"Python: {struct.calcsize('P')*8}位")

# ============================================================
# 配置
# ============================================================
MOT_FILE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
MCAD_EXE = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
FE_DIR = r"D:\Motor-CAD\11_1_5\FEResultsData"
RESULT_FILE = r"D:\MotorDesign\Scripts\motorcad_result.json"

# ============================================================
# Step 1: 记录 FEResultsData 的初始状态
# ============================================================
print("=== Step 1: 记录 FEResultsData 初始状态 ===")
files_before = {}
if os.path.exists(FE_DIR):
    for f in os.listdir(FE_DIR):
        fpath = os.path.join(FE_DIR, f)
        files_before[f] = {
            'size': os.path.getsize(fpath),
            'mtime': os.path.getmtime(fpath)
        }
print(f"  文件数: {len(files_before)}")

# ============================================================
# Step 2: 找到 Motor-CAD 主窗口
# ============================================================
print("\n=== Step 2: 查找 Motor-CAD ===")

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
    print("  Motor-CAD 未运行，启动中...")
    import subprocess
    subprocess.Popen([MCAD_EXE, MOT_FILE])
    print("  等待 60 秒...")
    time.sleep(60)
    win32gui.EnumWindows(find_main, None)

if not main_hwnd:
    print("  *** Motor-CAD 启动失败! ***")
    sys.exit(1)

print(f"  Motor-CAD: hwnd={main_hwnd}, pid={main_pid}")
print(f"  title: {win32gui.GetWindowText(main_hwnd)}")

# ============================================================
# Step 3: 切换到"输出数据"标签页，读取已有结果
# ============================================================
print("\n=== Step 3: 读取已有计算结果 ===")

# 找 PageControl
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

# 切换到"输出数据"标签 (x=380)
print("  切换到 '输出数据' 标签...")
lp = win32api.MAKELONG(380, 12)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
time.sleep(0.1)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
time.sleep(2)

# 读取所有可见的文本控件
print("  读取控件文本...")
text_controls = []
def find_text(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if vis and title and cls in ('TLabel', 'TEdit', 'TStaticText', 'TMemo', 
                                      'TAdvStringGrid', 'TStringGrid', 'TListView',
                                      'TGroupBox', 'TPanel'):
            text_controls.append({'cls': cls, 'title': title, 'hwnd': hwnd})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_text, None)

# 显示文本控件
print(f"  找到 {len(text_controls)} 个文本控件")
temperature_data = {}
for tc in text_controls:
    if tc['cls'] == 'TLabel':
        title = tc['title']
        # 检查是否包含温度信息
        if any(kw in title for kw in ['温度', 'Temp', 'temp', 'T[', 'T_', '°C', 'C']):
            print(f"    {tc['cls']}: '{title[:80]}'")
            temperature_data[title] = tc['hwnd']
    elif tc['cls'] == 'TGroupBox':
        print(f"    [Group] '{tc['title']}'")

# 尝试读取 TAdvStringGrid / TStringGrid 的内容
def read_grid(grid_hwnd):
    """尝试读取 Grid 控件内容"""
    rows = []
    try:
        # 尝试发送 WM_GETTEXT
        length = win32gui.SendMessage(grid_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if length > 0:
            buf = win32gui.PyMakeBuffer(length + 2)
            win32gui.SendMessage(grid_hwnd, win32con.WM_GETTEXT, length + 2, buf)
            text = buf[:length].decode('gbk', errors='replace')
            rows = text.split('\n')
    except:
        pass
    return rows

# 查找 Grid 控件
grids = []
def find_grid(hwnd, p):
    try:
        cls = win32gui.GetClassName(hwnd)
        if 'Grid' in cls and win32gui.IsWindowVisible(hwnd):
            grids.append({'hwnd': hwnd, 'cls': cls})
    except:
        pass
    return True
win32gui.EnumChildWindows(main_hwnd, find_grid, None)

if grids:
    print(f"\n  找到 {len(grids)} 个 Grid 控件:")
    for g in grids:
        print(f"    {g['cls']}: hwnd={g['hwnd']}")
        rows = read_grid(g['hwnd'])
        if rows:
            print(f"    内容 ({len(rows)} 行):")
            for r in rows[:10]:
                print(f"      {r[:80]}")

# ============================================================
# Step 4: 切换到"求解设置"标签页
# ============================================================
print("\n=== Step 4: 切换到 '求解设置' 标签 ===")
lp = win32api.MAKELONG(240, 12)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
time.sleep(0.1)
win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
time.sleep(1)

# 查找求解按钮
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
    print(f"  按钮: '{b['title']}' rect={b['rect']}")
    if '求解' in b['title']:
        solve_btn = b

# ============================================================
# Step 5: 监控 FEResultsData 变化
# ============================================================
print("\n=== Step 5: 监控 FEResultsData 变化 ===")
print(f"  初始文件数: {len(files_before)}")
print(f"  请在 Motor-CAD 中手动点击 '{solve_btn['title'] if solve_btn else '求解热模型'}' 按钮")
print(f"  系统将自动监控计算完成...")
print()

# 监控循环
calculation_started = False
calculation_completed = False
start_time = time.time()

for wait in range(3600):  # 最多等1小时
    time.sleep(1)
    
    # 检查 FEResultsData 变化
    files_now = {}
    if os.path.exists(FE_DIR):
        for f in os.listdir(FE_DIR):
            fpath = os.path.join(FE_DIR, f)
            files_now[f] = {
                'size': os.path.getsize(fpath),
                'mtime': os.path.getmtime(fpath)
            }
    
    new_files = set(files_now.keys()) - set(files_before.keys())
    modified_files = set()
    for f in files_before:
        if f in files_now:
            if files_now[f]['mtime'] != files_before[f]['mtime']:
                modified_files.add(f)
            elif files_now[f]['size'] != files_before[f]['size']:
                modified_files.add(f)
    
    # 检查按钮变化 (取消按钮出现 = 计算开始)
    btns_now = []
    def check_btns(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls in ('TButton', 'TBitBtn') and title and win32gui.IsWindowVisible(hwnd):
                btns_now.append(title)
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, check_btns, None)
    
    has_cancel = any('取消' in t for t in btns_now)
    has_solve = any('求解' in t for t in btns_now)
    
    if new_files or modified_files:
        if not calculation_started:
            calculation_started = True
            print(f"  [{wait}s] *** 检测到计算开始! ***")
            print(f"    新文件: {len(new_files)}, 修改文件: {len(modified_files)}")
    
    if calculation_started and has_solve and not has_cancel and wait > 5:
        calculation_completed = True
        print(f"  [{wait}s] *** 计算完成! ***")
        break
    
    if wait % 30 == 0 and wait > 0:
        print(f"  [{wait}s] 等待中... 新文件={len(new_files)} 修改={len(modified_files)} 取消={'有' if has_cancel else '无'}")
    
    if wait >= 3600:
        print(f"  超时 (1小时)")
        break

# ============================================================
# Step 6: 读取计算结果
# ============================================================
if calculation_completed or new_files or modified_files:
    print("\n=== Step 6: 读取计算结果 ===")
    
    # 列出新文件和修改的文件
    all_changed = new_files | modified_files
    print(f"  变化的文件 ({len(all_changed)} 个):")
    for f in sorted(all_changed):
        fpath = os.path.join(FE_DIR, f)
        if os.path.exists(fpath):
            size = os.path.getsize(fpath)
            mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(fpath)))
            status = "新" if f in new_files else "改"
            print(f"    [{status}] {f}: {size} bytes, {mtime}")
    
    # 切换到"输出数据"标签页读取结果
    print("\n  切换到 '输出数据' 标签读取结果...")
    lp = win32api.MAKELONG(380, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(3)
    
    # 读取所有可见文本
    result_texts = []
    def read_all_text(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title:
                result_texts.append({'cls': cls, 'title': title, 'hwnd': hwnd})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, read_all_text, None)
    
    # 提取温度相关数据
    temperatures = {}
    for rt in result_texts:
        title = rt['title']
        # 匹配温度值模式 (如 "85.5 C" 或 "温度: 85.5")
        if any(kw in title for kw in ['温度', 'Temp', 'temp', '°C', ' C', 'C)', 'C]']):
            print(f"    {rt['cls']}: '{title[:80]}'")
            temperatures[rt['title']] = rt['hwnd']
    
    # 保存结果
    result = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'calculation_completed': calculation_completed,
        'duration_seconds': int(time.time() - start_time),
        'changed_files': sorted(list(all_changed)),
        'new_files': sorted(list(new_files)),
        'modified_files': sorted(list(modified_files)),
        'temperature_texts': [rt['title'] for rt in result_texts if '温度' in rt['title'] or 'Temp' in rt['title'] or '°C' in rt['title']],
        'all_texts': [{'cls': rt['cls'], 'title': rt['title']} for rt in result_texts if rt['cls'] in ('TLabel', 'TEdit', 'TStaticText')],
    }
    
    with open(RESULT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存到: {RESULT_FILE}")
    
else:
    print("\n  *** 未检测到计算 ***")
    print("  请确认已在 Motor-CAD 中手动点击求解按钮")

print("\n=== 完成 ===")
