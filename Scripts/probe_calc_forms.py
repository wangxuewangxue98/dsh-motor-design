#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 计算窗体探测 + 键盘快捷键测试 + 结果文件检查
"""
import sys
import os
import time
import json
import glob
import win32gui
import win32con
import win32api
import win32process

OUTPUT_DIR = r"D:\MotorDesign\Scripts"

print("=" * 70)
print("Motor-CAD 计算窗体 + 快捷键 + 结果文件")
print("=" * 70)

# Find the Motor-CAD main form
main_hwnd = None

def find_motorcad(hwnd, lparam):
    global main_hwnd
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    
    if cls_name == 'TMainForm' and visible and 'Motor-CAD' in title:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        main_hwnd = hwnd
        print(f"  主窗体: hwnd={hwnd}, PID={pid}, title='{title}'")
    return True

win32gui.EnumWindows(find_motorcad, None)

if not main_hwnd:
    print("  未找到 Motor-CAD!")
    sys.exit(1)

# Step 1: Find key calculation forms and enumerate their children
print("\n[1] 计算相关窗体的子控件...")

target_forms = [
    'Tfm_BPMControl',     # BPM 计算
    'Tfm_LabMain',        # Motor-LAB 主窗体
    'Tfm_OutputData',     # 输出数据
    'Tfm_InputData',      # 输入数据
    'Tfm_Temperatures',   # 温度结果
    'Tfm_Transient',      # 瞬态分析
    'Tfm_Sensitivity',    # 灵敏度分析
]

all_children = []

def collect_all_children(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    
    try:
        ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
    except:
        ctrl_id = 0
    
    rect = win32gui.GetWindowRect(hwnd)
    enabled = win32gui.IsWindowEnabled(hwnd)
    
    all_children.append({
        'hwnd': int(hwnd),
        'class': cls_name,
        'title': title,
        'visible': visible,
        'enabled': enabled,
        'ctrl_id': ctrl_id,
        'rect': list(rect),
        'width': rect[2] - rect[0],
        'height': rect[3] - rect[1],
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect_all_children, None)

# For each target form, find its hwnd and enumerate its children
for form_class in target_forms:
    forms = [c for c in all_children if c['class'] == form_class]
    for form in forms:
        form_hwnd = form['hwnd']
        vis = "可见" if form['visible'] else "隐藏"
        print(f"\n  === {form_class} ({vis}) hwnd={form_hwnd} ===")
        
        # Enumerate children of this form
        form_children = []
        def collect_form_children(hwnd, lparam):
            cls_name = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            
            form_children.append({
                'class': cls_name,
                'title': title,
                'visible': visible,
                'ctrl_id': ctrl_id,
                'rect': list(rect),
                'width': rect[2] - rect[0],
                'height': rect[3] - rect[1],
            })
            return True
        
        try:
            win32gui.EnumChildWindows(form_hwnd, collect_form_children, None)
            print(f"    子控件数: {len(form_children)}")
            
            # Show buttons
            buttons = [c for c in form_children if c['class'] in ('TButton', 'TBitBtn', 'TSpeedButton')]
            if buttons:
                print(f"    按钮 ({len(buttons)} 个):")
                for b in buttons:
                    vis = "V" if b['visible'] else "H"
                    en = "E" if True else "D"  # check enabled
                    print(f"      [{vis}] id={b['ctrl_id']:8d} '{b['title'][:50]}'  {b['width']}x{b['height']}")
            
            # Show edits
            edits = [c for c in form_children if c['class'] in ('TEdit', 'TLabeledEdit', 'TMemo', 'TMaskEdit')]
            if edits:
                print(f"    输入框 ({len(edits)} 个):")
                for e in edits:
                    vis = "V" if e['visible'] else "H"
                    print(f"      [{vis}] id={e['ctrl_id']:8d} '{e['title'][:50]}'  {e['width']}x{e['height']}")
            
            # Show grids
            grids = [c for c in form_children if c['class'] in ('TAdvStringGrid', 'TStringGrid', 'TDrawGrid')]
            if grids:
                print(f"    表格 ({len(grids)} 个):")
                for g in grids:
                    vis = "V" if g['visible'] else "H"
                    print(f"      [{vis}] id={g['ctrl_id']:8d} {g['width']}x{g['height']}")
            
            # Show labels
            labels = [c for c in form_children if c['class'] == 'TLabel' and c['title'] and c['visible']]
            if labels:
                print(f"    可见标签 ({len(labels)} 个, 前20):")
                for l in labels[:20]:
                    print(f"      '{l['title'][:60]}'  id={l['ctrl_id']}")
            
            # Show combo boxes
            combos = [c for c in form_children if c['class'] == 'TComboBox']
            if combos:
                print(f"    下拉框 ({len(combos)} 个):")
                for c in combos:
                    vis = "V" if c['visible'] else "H"
                    print(f"      [{vis}] id={c['ctrl_id']:8d} '{c['title'][:50]}'")
                    
        except Exception as e:
            print(f"    枚举失败: {e}")

# Step 2: Check Motor-CAD output directories for result files
print("\n\n[2] 检查 Motor-CAD 结果文件目录...")

# Common Motor-CAD output locations
output_dirs = [
    r"D:\Motor-CAD\11_1_5",
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD",
    r"D:\MotorDesign",
    r"D:\MotorDesign\Templates",
    os.path.expanduser("~\Documents"),
    os.path.expanduser("~\Desktop"),
]

for d in output_dirs:
    if os.path.exists(d):
        # Look for result-like files
        patterns = ['*.csv', '*.txt', '*.out', '*.log', '*.dat', '*.res']
        for pat in patterns:
            files = glob.glob(os.path.join(d, pat))
            if files:
                print(f"\n  {d} ({pat}):")
                for f in files[:10]:
                    size = os.path.getsize(f)
                    mtime = os.path.getmtime(f)
                    mtime_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
                    print(f"    {os.path.basename(f):40s} {size:8d} bytes  {mtime_str}")

# Also check for Motor-CAD specific output patterns
print("\n  搜索 Motor-CAD 输出文件模式:")
for d in output_dirs:
    if os.path.exists(d):
        for pat in ['*output*', '*result*', '*calc*', '*thermal*', *['*magnetic*']]:
            files = glob.glob(os.path.join(d, pat))
            if files:
                print(f"\n  {d} ({pat}):")
                for f in files[:5]:
                    print(f"    {f}")

# Step 3: Look at Tfm_BPMControl more carefully - it likely has calculation buttons
print("\n\n[3] 深入检查 Tfm_BPMControl (BPM计算控制)...")

bpm_forms = [c for c in all_children if c['class'] == 'Tfm_BPMControl']
for form in bpm_forms:
    form_hwnd = form['hwnd']
    print(f"  BPMControl hwnd={form_hwnd}, visible={form['visible']}")
    
    # Get ALL children regardless of type
    bpm_children = []
    def collect_bpm(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            ctrl_id = 0
        rect = win32gui.GetWindowRect(hwnd)
        
        bpm_children.append({
            'hwnd': int(hwnd),
            'class': cls_name,
            'title': title,
            'visible': visible,
            'enabled': enabled,
            'ctrl_id': ctrl_id,
            'rect': list(rect),
        })
        return True
    
    try:
        win32gui.EnumChildWindows(form_hwnd, collect_bpm, None)
        print(f"  子控件总数: {len(bpm_children)}")
        print(f"  所有控件:")
        for c in bpm_children:
            vis = "V" if c['visible'] else "H"
            en = "E" if c['enabled'] else "D"
            title = f"'{c['title'][:40]}'" if c['title'] else ""
            print(f"    [{vis}{en}] {c['class']:20s} id={c['ctrl_id']:8d} {title}")
    except Exception as e:
        print(f"  枚举失败: {e}")

# Step 4: Try keyboard shortcuts
print("\n\n[4] 测试键盘快捷键...")
print("  (仅记录可能的快捷键，不实际发送)")

# Common Motor-CAD shortcuts (from documentation/guesses):
shortcuts = [
    ('F5', '可能: 运行计算'),
    ('F6', '可能: 运行热计算'),
    ('F7', '可能: 运行磁计算'),
    ('Ctrl+E', '可能: 电磁计算'),
    ('Ctrl+T', '可能: 热计算'),
    ('Ctrl+R', '可能: 运行'),
    ('Ctrl+M', '可能: Motor-LAB'),
    ('Alt+C', '可能: 计算菜单'),
]

for key, desc in shortcuts:
    print(f"    {key:10s} - {desc}")

# Step 5: Check .mot file for calculation-related sections
print("\n\n[5] .mot 文件中的计算相关参数...")

mot_file = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"
with open(mot_file, 'r', encoding='latin-1') as f:
    lines = f.readlines()

# Find sections
sections = []
for i, line in enumerate(lines):
    if line.strip().startswith('[') and line.strip().endswith(']'):
        sections.append((i, line.strip()))

print(f"  .mot 文件包含 {len(sections)} 个节:")
for line_num, sec in sections:
    print(f"    {line_num:4d}: {sec}")

# Step 6: Look for Motor-CAD template .mot files
print("\n\n[6] 查找其他 .mot 模板文件...")

mot_patterns = [
    r"D:\Motor-CAD\**\*.mot",
    r"D:\MotorDesign\**\*.mot",
]

for pat in mot_patterns:
    files = glob.glob(pat, recursive=True)
    if files:
        print(f"  模式 {pat}:")
        for f in files:
            size = os.path.getsize(f)
            print(f"    {f}  ({size} bytes)")

# Step 7: Check for Motor-CAD automation examples
print("\n\n[7] Motor-CAD 自动化示例文件...")

example_dirs = [
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD",
    r"D:\Motor-CAD\11_1_5",
]

for d in example_dirs:
    if os.path.exists(d):
        for root, dirs, files in os.walk(d):
            for f in files:
                if f.endswith(('.py', '.m', '.bas', '.vbs', '.js', '.txt')):
                    fpath = os.path.join(root, f)
                    size = os.path.getsize(fpath)
                    if size < 100000:  # Skip large files
                        print(f"    {fpath}  ({size} bytes)")

print("\n" + "=" * 70)
print("探测完成!")
print("=" * 70)
