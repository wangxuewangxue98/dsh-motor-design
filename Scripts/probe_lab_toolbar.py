#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD Tfm_LabMain 深度探测 + 工具栏 + 键盘快捷键测试
"""
import sys
import os
import time
import win32gui
import win32con
import win32api
import win32process
import ctypes

print("=" * 70)
print("Motor-CAD LabMain + 工具栏 + 快捷键")
print("=" * 70)

# Find Motor-CAD main form (use the first one found)
main_hwnd = None

def find_motorcad(hwnd, lparam):
    global main_hwnd
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    if cls_name == 'TMainForm' and visible and 'Motor-CAD' in title:
        if main_hwnd is None:
            main_hwnd = hwnd
    return True

win32gui.EnumWindows(find_motorcad, None)

if not main_hwnd:
    print("  未找到 Motor-CAD!")
    sys.exit(1)

print(f"  主窗体: hwnd={main_hwnd}")

# Step 1: Enumerate ALL children and find Tfm_LabMain + ToolBar
print("\n[1] 枚举所有子控件...")

all_children = []

def collect_children(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    enabled = win32gui.IsWindowEnabled(hwnd)
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
        'enabled': enabled,
        'ctrl_id': ctrl_id,
        'rect': list(rect),
        'width': rect[2] - rect[0],
        'height': rect[3] - rect[1],
    })
    return True

win32gui.EnumChildWindows(main_hwnd, collect_children, None)
print(f"  总控件数: {len(all_children)}")

# Step 2: Find ToolBar controls
print("\n[2] 工具栏控件...")
toolbar_classes = ['TToolBar', 'TToolButton', 'TToolBar32', 'ToolbarWindow32', 
                   'TActionToolBar', 'TControlBar', 'TCoolBar', 'TPanel']
toolbars = [c for c in all_children if c['class'] in toolbar_classes]
print(f"  工具栏相关控件 ({len(toolbars)} 个):")
for t in toolbars[:30]:
    vis = "V" if t['visible'] else "H"
    en = "E" if t['enabled'] else "D"
    print(f"    [{vis}{en}] {t['class']:20s} id={t['ctrl_id']:8d} '{t['title'][:40]}'  {t['width']}x{t['height']}  rect={t['rect']}")

# Step 3: Deep dive into Tfm_LabMain
print("\n[3] Tfm_LabMain 深度探测...")

lab_forms = [c for c in all_children if c['class'] == 'Tfm_LabMain']
for form in lab_forms:
    form_hwnd = form['hwnd']
    print(f"  LabMain: hwnd={form_hwnd}, visible={form['visible']}, {form['width']}x{form['height']}")
    
    lab_children = []
    def collect_lab(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            ctrl_id = 0
        rect = win32gui.GetWindowRect(hwnd)
        
        lab_children.append({
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
    
    try:
        win32gui.EnumChildWindows(form_hwnd, collect_lab, None)
        print(f"  子控件数: {len(lab_children)}")
        
        # Show ALL children with details
        print(f"\n  所有子控件:")
        for c in lab_children:
            vis = "V" if c['visible'] else "H"
            en = "E" if c['enabled'] else "D"
            title = f"'{c['title'][:50]}'" if c['title'] else ""
            print(f"    [{vis}{en}] {c['class']:20s} id={c['ctrl_id']:8d} {title}  {c['width']}x{c['height']}  rect={c['rect']}")
        
        # Highlight buttons
        buttons = [c for c in lab_children if c['class'] in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton')]
        if buttons:
            print(f"\n  按钮 ({len(buttons)} 个):")
            for b in buttons:
                vis = "V" if b['visible'] else "H"
                en = "E" if b['enabled'] else "D"
                print(f"    [{vis}{en}] id={b['ctrl_id']:8d} '{b['title'][:50]}'  hwnd={b['hwnd']}")
                
    except Exception as e:
        print(f"  枚举失败: {e}")

# Step 4: Check for TActionManager or TActionList (Delphi action-based UI)
print("\n\n[4] 查找 Action 控件...")
action_classes = ['TActionManager', 'TActionMainMenuBar', 'TActionToolBar', 
                  'TCustomActionMainMenuBar', 'TActionClient']
action_controls = [c for c in all_children if any(ac in c['class'] for ac in action_classes)]
print(f"  Action 控件 ({len(action_controls)} 个):")
for a in action_controls:
    vis = "V" if a['visible'] else "H"
    print(f"    [{vis}] {a['class']:30s} '{a['title'][:40]}'  {a['width']}x{a['height']}  rect={a['rect']}")

# Step 5: Look for any control with "calculate", "run", "solve" in title
print("\n\n[5] 查找含计算/运行关键词的控件...")

keywords = ['calc', 'run', 'solve', 'analy', 'simul', 'compute', 'start',
            '计', '算', '运', '行', '求', '解', '分', '析', '仿', '真',
            'electromagnetic', 'thermal', 'magnetic', 'EMag', 'FEA']

key_controls = [c for c in all_children if c['title'] and 
                any(kw.lower() in c['title'].lower() for kw in keywords)]
print(f"  关键控件 ({len(key_controls)} 个):")
for c in key_controls:
    vis = "V" if c['visible'] else "H"
    en = "E" if c['enabled'] else "D"
    print(f"    [{vis}{en}] {c['class']:20s} id={c['ctrl_id']:8d} '{c['title'][:60]}'  {c['width']}x{c['height']}")

# Step 6: Try to find the Motor-CAD calculation tab
print("\n\n[6] 查找计算相关标签页...")

tabs = [c for c in all_children if c['class'] == 'TTabSheet']
print(f"  所有标签页 ({len(tabs)} 个):")
for t in tabs:
    vis = "V" if t['visible'] else "H"
    print(f"    [{vis}] '{t['title']}'  id={t['ctrl_id']}")

# Step 7: Check the FEResultsData directory
print("\n\n[7] 检查 FEResultsData 目录...")

fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
if os.path.exists(fe_dir):
    print(f"  目录存在: {fe_dir}")
    files = os.listdir(fe_dir)
    print(f"  文件数: {len(files)}")
    for f in files[:30]:
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath) if os.path.isfile(fpath) else "DIR"
        print(f"    {f:40s}  {size}")
else:
    print(f"  目录不存在: {fe_dir}")

# Step 8: Read the MATLAB example scripts for clues about UI actions
print("\n\n[8] MATLAB 示例脚本中的 COM 方法...")

examples = [
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\MotorCAD_Matlab_Script_Example_1.m",
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Thermal_Calc.m",
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Lab\MotorLABautomation.m",
    r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Lab\matlab_motorlab_example.m",
]

for ex_path in examples:
    if os.path.exists(ex_path):
        print(f"\n  === {os.path.basename(ex_path)} ===")
        with open(ex_path, 'r', encoding='latin-1') as f:
            content = f.read()
        print(f"  {content[:2000]}")

# Step 9: Try to capture Motor-CAD window screenshot to understand the UI
print("\n\n[9] 尝试截图 Motor-CAD 窗口...")

try:
    # Bring Motor-CAD to foreground
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(main_hwnd)
    time.sleep(2)
    
    # Get window rect
    rect = win32gui.GetWindowRect(main_hwnd)
    print(f"  窗口位置: {rect}")
    print(f"  窗口大小: {rect[2]-rect[0]}x{rect[3]-rect[1]}")
    
    # Try to capture using PrintWindow
    import win32ui
    from PIL import Image
    
    left, top, right, bottom = rect
    width = right - left
    height = bottom - top
    
    hwnd_dc = win32gui.GetWindowDC(main_hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    
    # Use PrintWindow (3 = PW_RENDERFULLCONTENT)
    result = ctypes.windll.user32.PrintWindow(main_hwnd, save_dc.GetSafeHdc(), 3)
    print(f"  PrintWindow result: {result}")
    
    screenshot_path = os.path.join(r"D:\MotorDesign\Scripts", "motorcad_screenshot.png")
    bitmap.SaveBitmapFile(save_dc, screenshot_path)
    print(f"  截图已保存: {screenshot_path}")
    
    # Cleanup
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(main_hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())
    
except Exception as e:
    print(f"  截图失败: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
print("探测完成!")
print("=" * 70)
