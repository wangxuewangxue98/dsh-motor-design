#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 自动解算 v6c — 多方法触发计算

尝试:
  1. pywinauto menu_select (Delphi 菜单可能需要特殊处理)
  2. SendMessage WM_COMMAND (同步执行)
  3. PostMessage 到 TApplication 窗口
  4. 检查菜单命令是否打开新窗口
  5. 读取已有温度结果 (Temperatures 子标签)
"""
import sys
import os
import time
import struct
import json
import ctypes
import win32gui
import win32con
import win32api
import win32process

print(f"Python: {struct.calcsize('P')*8}位")
print("=" * 70)

# ============================================================
# Step 1: 找到 Motor-CAD 窗口
# ============================================================
print("\n[1] 查找 Motor-CAD 主窗口...")

main_hwnd = None
app_hwnd = None
mc_pid = None

def find_mc(hwnd, p):
    global main_hwnd, app_hwnd, mc_pid
    try:
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        vis = win32gui.IsWindowVisible(hwnd)
        if cls == 'TMainForm' and 'Motor' in title and vis:
            main_hwnd = hwnd
            _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
        elif cls == 'TApplication' and 'Motor' in title and vis:
            app_hwnd = hwnd
            _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
    except:
        pass
    return True

win32gui.EnumWindows(find_mc, None)

if not main_hwnd:
    print("  *** 错误: 未找到 Motor-CAD! ***")
    sys.exit(1)

print(f"  主窗口: hwnd={main_hwnd}, vis={win32gui.IsWindowVisible(main_hwnd)}")
print(f"  标题: '{win32gui.GetWindowText(main_hwnd)[:60]}'")
if app_hwnd:
    print(f"  App窗口: hwnd={app_hwnd}")

# ============================================================
# Step 2: 记录所有窗口 (用于后续比较)
# ============================================================
def get_all_windows():
    """获取所有可见窗口的 hwnd 集合"""
    windows = set()
    def cb(hwnd, p):
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    windows.add(hwnd)
        except:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return windows

def get_all_child_windows(parent_hwnd):
    """获取所有子窗口"""
    children = set()
    def cb(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            children.add((hwnd, cls, title, vis))
        except:
            pass
        return True
    win32gui.EnumChildWindows(parent_hwnd, cb, None)
    return children

# ============================================================
# Step 3: 记录 FEResultsData 初始状态
# ============================================================
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
files_before = {}
if os.path.exists(fe_dir):
    for f in os.listdir(fe_dir):
        fpath = os.path.join(fe_dir, f)
        files_before[f] = {
            'size': os.path.getsize(fpath),
            'mtime': os.path.getmtime(fpath)
        }
print(f"\n  FEResultsData 文件数: {len(files_before)}")

def check_fe_change():
    """检查 FEResultsData 是否有变化"""
    if not os.path.exists(fe_dir):
        return False, set()
    new_files = set()
    for f in os.listdir(fe_dir):
        fpath = os.path.join(fe_dir, f)
        try:
            current_mtime = os.path.getmtime(fpath)
            current_size = os.path.getsize(fpath)
            if f not in files_before:
                new_files.add(f)
            elif files_before[f]['mtime'] != current_mtime or files_before[f]['size'] != current_size:
                new_files.add(f)
        except:
            pass
    return len(new_files) > 0, new_files

# ============================================================
# Step 4: 方法1 — pywinauto menu_select
# ============================================================
print("\n[4] 方法1: pywinauto menu_select...")

try:
    from pywinauto import Application
    app = Application(backend='win32').connect(process=mc_pid)
    main_win = app.window(handle=main_hwnd)
    
    # 尝试设置前台
    try:
        main_win.set_focus()
        time.sleep(0.5)
    except:
        pass
    
    print(f"  pywinauto 连接成功")
    print(f"  尝试 menu_select('结果->运行求解')...")
    
    # 记录前状态
    windows_before = get_all_windows()
    
    try:
        # 尝试不同的菜单路径格式
        # Delphi 菜单可能使用 & 前缀
        main_win.menu_select("结果->运行求解")
        time.sleep(3)
        print("  menu_select 完成")
    except Exception as e:
        print(f"  menu_select 失败: {e}")
        # 尝试英文菜单名
        try:
            print("  尝试英文菜单名...")
            main_win.menu_select("Result->Run Solve")
            time.sleep(3)
        except Exception as e2:
            print(f"  英文菜单名也失败: {e2}")
    
    # 检查变化
    windows_after = get_all_windows()
    new_windows = windows_after - windows_before
    if new_windows:
        print(f"  *** 新窗口出现! {len(new_windows)} 个 ***")
        for nw in new_windows:
            try:
                cls = win32gui.GetClassName(nw)
                title = win32gui.GetWindowText(nw)
                print(f"    {cls}: '{title[:60]}'")
            except:
                pass
    
    changed, new_files = check_fe_change()
    if changed:
        print(f"  *** FEResultsData 文件变化! ***")
    
    if not new_windows and not changed:
        # 检查子窗口变化
        children_before = get_all_child_windows(main_hwnd)
        time.sleep(1)
        children_after = get_all_child_windows(main_hwnd)
        new_children = children_after - children_before
        if new_children:
            print(f"  *** 新子窗口出现! {len(new_children)} 个 ***")
            for nc in new_children:
                print(f"    {nc[1]}: '{nc[2][:50]}' vis={nc[3]}")
        else:
            print("  无变化")

except Exception as e:
    print(f"  pywinauto 连接失败: {e}")

# ============================================================
# Step 5: 方法2 — SendMessage WM_COMMAND (同步)
# ============================================================
print("\n[5] 方法2: SendMessage WM_COMMAND (同步)...")

# ID=119 是 "运行求解"
windows_before = get_all_windows()
children_before = get_all_child_windows(main_hwnd)

# 先切换到"求解设置"标签页
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
if pcs:
    pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
    # 切换到"求解设置"标签 (x=240)
    lp = win32api.MAKELONG(240, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(1)
    print("  已切换到'求解设置'标签")

# 发送 WM_COMMAND ID=119 到主窗口 (SendMessage 同步)
print("  SendMessage(WM_COMMAND, 119, 0) 到主窗口...")
result = win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, 119, 0)
print(f"  SendMessage 返回: {result}")
time.sleep(3)

# 检查变化
windows_after = get_all_windows()
new_windows = windows_after - windows_before
if new_windows:
    print(f"  *** 新窗口出现! ***")
    for nw in new_windows:
        try:
            cls = win32gui.GetClassName(nw)
            title = win32gui.GetWindowText(nw)
            print(f"    {cls}: '{title[:60]}'")
        except:
            pass

children_after = get_all_child_windows(main_hwnd)
new_children = children_after - children_before
if new_children:
    print(f"  *** 新子窗口出现! {len(new_children)} 个 ***")
    for nc in new_children:
        print(f"    {nc[1]}: '{nc[2][:50]}' vis={nc[3]}")

changed, new_files = check_fe_change()
if changed:
    print(f"  *** FEResultsData 文件变化! ***")

if not new_windows and not new_children and not changed:
    print("  无变化")

# 也尝试发送到 TApplication 窗口
if app_hwnd:
    print(f"\n  SendMessage(WM_COMMAND, 119, 0) 到 TApplication 窗口...")
    windows_before2 = get_all_windows()
    result2 = win32gui.SendMessage(app_hwnd, win32con.WM_COMMAND, 119, 0)
    print(f"  SendMessage 返回: {result2}")
    time.sleep(3)
    
    windows_after2 = get_all_windows()
    new_windows2 = windows_after2 - windows_before2
    if new_windows2:
        print(f"  *** 新窗口出现! ***")
        for nw in new_windows2:
            try:
                cls = win32gui.GetClassName(nw)
                title = win32gui.GetWindowText(nw)
                print(f"    {cls}: '{title[:60]}'")
            except:
                pass
    else:
        print("  无变化")

# ============================================================
# Step 6: 方法3 — 尝试所有计算相关菜单 ID
# ============================================================
print("\n[6] 方法3: 尝试所有结果菜单 ID...")

# 结果菜单下的所有 ID
result_ids = [
    (115, "热路图"),
    (116, "节点温度"),
    (117, "数据输出"),
    (118, "瞬态曲线"),
    (119, "运行求解 Ctrl+R"),
    (120, "退出求解 Ctrl+K"),
    (121, "复制结果进行对比"),
]

for item_id, item_name in result_ids:
    print(f"\n  发送 WM_COMMAND ID={item_id} ({item_name})...")
    
    windows_before = get_all_windows()
    
    win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, item_id, 0)
    time.sleep(2)
    
    windows_after = get_all_windows()
    new_windows = windows_after - windows_before
    
    if new_windows:
        print(f"    *** 新窗口出现! ***")
        for nw in new_windows:
            try:
                cls = win32gui.GetClassName(nw)
                title = win32gui.GetWindowText(nw)
                vis = win32gui.IsWindowVisible(nw)
                print(f"      {cls}: '{title[:60]}' vis={vis}")
            except:
                pass
    else:
        # 检查子窗口
        children_before = get_all_child_windows(main_hwnd)
        time.sleep(0.5)
        children_after = get_all_child_windows(main_hwnd)
        new_children = children_after - children_before
        if new_children:
            print(f"    *** 新子窗口! {len(new_children)} 个 ***")
            for nc in new_children:
                if nc[3]:  # vis=True
                    print(f"      {nc[1]}: '{nc[2][:50]}' vis={nc[3]}")
        
        changed, new_files = check_fe_change()
        if changed:
            print(f"    *** FEResultsData 变化! ***")
        else:
            print(f"    无变化")

# ============================================================
# Step 7: 读取已有温度结果
# ============================================================
print("\n[7] 读取已有温度结果...")

# 切换到"输出数据"标签 (x=380)
if pcs:
    pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
    lp = win32api.MAKELONG(380, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(2)
    print("  已切换到'输出数据'标签")

    # 查找子 PageControl (输出数据下的 Temperatures/Main 子标签)
    sub_pcs = []
    def find_sub_pc(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            if cls == 'TPageControl':
                rect = win32gui.GetWindowRect(hwnd)
                sub_pcs.append({'hwnd': hwnd, 'rect': rect})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_sub_pc, None)
    
    print(f"  找到 {len(sub_pcs)} 个 PageControl")
    for spc in sub_pcs:
        print(f"    hwnd={spc['hwnd']}, rect={spc['rect']}")
    
    # 尝试切换到 Temperatures 子标签
    if len(sub_pcs) >= 2:
        sub_pc = sub_pcs[1]  # 第二个可能是子 PageControl
        print(f"\n  尝试切换到 Temperatures 子标签...")
        # 点击子标签的第一个标签 (x 偏移 ~15)
        for x_offset in [15, 45, 75, 105, 135, 165]:
            lp = win32api.MAKELONG(x_offset, 8)
            win32gui.PostMessage(sub_pc['hwnd'], win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
            time.sleep(0.05)
            win32gui.PostMessage(sub_pc['hwnd'], win32con.WM_LBUTTONUP, 0, lp)
            time.sleep(1)
            
            # 读取可见文本
            texts = []
            def read_text(hwnd, p):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                    vis = win32gui.IsWindowVisible(hwnd)
                    if vis and title and len(title) > 1:
                        texts.append({'cls': cls, 'title': title, 'hwnd': hwnd})
                except:
                    pass
                return True
            win32gui.EnumChildWindows(main_hwnd, read_text, None)
            
            # 过滤温度相关文本
            temp_texts = [t for t in texts if any(kw in t['title'] for kw in 
                ['Temp', 'temp', '°C', 'Winding', 'Housing', 'Magnet', 'Tooth', 'Stator', 'Rotor', 'Node', 'T[', 'T_', 'Average', '°', '度'])]
            
            if temp_texts:
                print(f"\n  x={x_offset}: 找到 {len(temp_texts)} 个温度文本!")
                for tt in temp_texts[:20]:
                    print(f"    {tt['cls']}: '{tt['title'][:80]}'")
                break
            elif x_offset == 15:
                print(f"  x={x_offset}: {len(texts)} 个文本，无温度")
                # 打印所有文本看看
                for t in texts[:15]:
                    print(f"    {t['cls']}: '{t['title'][:60]}'")

    # 也直接读取所有可见文本
    print(f"\n  === 所有可见文本控件 ===")
    all_texts = []
    def read_all(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title:
                all_texts.append({'cls': cls, 'title': title})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, read_all, None)
    for t in all_texts:
        print(f"    {t['cls']}: '{t['title'][:70]}'")

# ============================================================
# 保存结果
# ============================================================
result = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'main_hwnd': main_hwnd,
    'main_title': win32gui.GetWindowText(main_hwnd),
    'effective_command': None,
    'fe_files_before': len(files_before),
    'fe_files_after': len(os.listdir(fe_dir)) if os.path.exists(fe_dir) else 0,
}

with open(r'D:\MotorDesign\Scripts\menu_solve_result_v3.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n{'='*70}")
print(json.dumps(result, ensure_ascii=False, indent=2))
