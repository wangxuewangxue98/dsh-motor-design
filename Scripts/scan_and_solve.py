#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 全面控件扫描 + 计算触发
1. 锁定正确的 Motor-CAD 实例
2. 系统扫描所有主标签和子标签
3. 查找 '求解电磁模型' 和 '求解热模型' 按钮
4. 尝试点击按钮触发计算
5. 监控 FEResultsData 目录变化
6. 解析结果文件
"""
import sys
import os
import time
import json
import struct
import win32gui
import win32con
import win32api
import win32process

print("=" * 70)
print("Motor-CAD 全面扫描 + 计算触发 v2")
print("=" * 70)

# ============================================================
# Step 0: 找到正确的 Motor-CAD 实例（选择内存最大的）
# ============================================================
print("\n[0] 查找 Motor-CAD 实例...")

mc_windows = []
def find_mc(hwnd, lparam):
    cls_name = win32gui.GetClassName(hwnd)
    title = win32gui.GetWindowText(hwnd)
    visible = win32gui.IsWindowVisible(hwnd)
    if cls_name == 'TMainForm' and visible and 'Motor-CAD' in title:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        mc_windows.append({
            'hwnd': int(hwnd), 'title': title, 'pid': pid
        })
    return True

win32gui.EnumWindows(find_mc, None)

if not mc_windows:
    print("  错误: 未找到 Motor-CAD 窗口!")
    sys.exit(1)

for w in mc_windows:
    print(f"  实例: hwnd={w['hwnd']}, pid={w['pid']}, title='{w['title']}'")

# 选择第一个实例
main_hwnd = mc_windows[0]['hwnd']
_, mc_pid = win32process.GetWindowThreadProcessId(main_hwnd)
print(f"  使用实例: hwnd={main_hwnd}, pid={mc_pid}")

# ============================================================
# 工具函数
# ============================================================

def get_all_children(parent_hwnd, depth=0):
    """递归获取所有子控件"""
    results = []
    def collect(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        try:
            ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
        except:
            ctrl_id = 0
        rect = win32gui.GetWindowRect(hwnd)
        results.append({
            'hwnd': int(hwnd), 'class': cls_name, 'title': title,
            'visible': visible, 'enabled': enabled, 'ctrl_id': ctrl_id,
            'rect': list(rect), 'depth': depth,
        })
        return True
    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return results

def get_visible_buttons():
    """获取所有可见按钮"""
    buttons = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        enabled = win32gui.IsWindowEnabled(hwnd)
        if cls_name in ('TButton', 'TBitBtn', 'TSpeedButton', 'TToolButton') and visible and title:
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            rect = win32gui.GetWindowRect(hwnd)
            buttons.append({
                'title': title, 'hwnd': int(hwnd), 'ctrl_id': ctrl_id,
                'enabled': enabled, 'rect': list(rect),
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return buttons

def get_visible_tabsheets():
    """获取所有可见的 TTabSheet"""
    tabs = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TTabSheet' and visible and title:
            rect = win32gui.GetWindowRect(hwnd)
            tabs.append({
                'title': title, 'hwnd': int(hwnd), 'rect': list(rect)
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return tabs

def get_all_pagecontrols():
    """获取所有 TPageControl"""
    pcs = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        if cls_name == 'TPageControl' and visible:
            rect = win32gui.GetWindowRect(hwnd)
            try:
                ctrl_id = win32api.GetWindowLong(hwnd, win32con.GWL_ID)
            except:
                ctrl_id = 0
            pcs.append({
                'hwnd': int(hwnd), 'rect': list(rect), 'ctrl_id': ctrl_id
            })
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return pcs

def click_tab_x(pc_hwnd, x_pos, y_pos=12):
    """通过 PostMessage 点击 PageControl 标签栏"""
    lparam = win32api.MAKELONG(x_pos, y_pos)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

def send_bm_click(btn_hwnd):
    """发送 BM_CLICK 消息"""
    win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)

def post_click_button(btn_hwnd):
    """用 PostMessage 点击按钮"""
    rect = win32gui.GetWindowRect(btn_hwnd)
    cx = (rect[2] - rect[0]) // 2
    cy = (rect[3] - rect[1]) // 2
    lparam = win32api.MAKELONG(cx, cy)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.1)
    win32gui.PostMessage(btn_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    time.sleep(0.5)

def close_dialogs():
    """关闭所有弹窗"""
    closed = []
    def find_and_close(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == mc_pid and visible and cls_name in ('TMessageForm', 'TForm'):
            title = win32gui.GetWindowText(hwnd)
            if title and title != 'Motor-CAD':
                # 获取弹窗内容
                def get_text(child_hwnd, lparam):
                    child_cls = win32gui.GetClassName(child_hwnd)
                    child_title = win32gui.GetWindowText(child_hwnd)
                    if child_title and child_cls not in ('TButton',):
                        closed.append(f"{child_cls}: '{child_title[:200]}'")
                    return True
                win32gui.EnumChildWindows(hwnd, get_text, None)
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    win32gui.EnumWindows(find_and_close, None)
    return closed

def get_status_bar_text():
    """获取状态栏文本"""
    texts = []
    def check(hwnd, lparam):
        cls_name = win32gui.GetClassName(hwnd)
        if cls_name == 'TStatusBar':
            text = win32gui.GetWindowText(hwnd)
            if text and text.strip():
                texts.append(text)
        return True
    win32gui.EnumChildWindows(main_hwnd, check, None)
    return texts

# ============================================================
# Step 1: 找到主 PageControl
# ============================================================
print("\n[1] 查找 PageControl...")

page_controls = get_all_pagecontrols()
if not page_controls:
    print("  错误: 未找到 TPageControl!")
    sys.exit(1)

for pc in page_controls:
    w = pc['rect'][2] - pc['rect'][0]
    h = pc['rect'][3] - pc['rect'][1]
    print(f"  PageControl: hwnd={pc['hwnd']}, rect={pc['rect']}, size={w}x{h}, id={pc['ctrl_id']}")

# 选择最大的 PageControl
main_pc = max(page_controls, key=lambda c: (c['rect'][2]-c['rect'][0]) * (c['rect'][3]-c['rect'][1]))
pc_hwnd = main_pc['hwnd']
pc_rect = main_pc['rect']
print(f"\n  主 PageControl: hwnd={pc_hwnd}, rect={pc_rect}")

# ============================================================
# Step 2: 系统扫描所有主标签 (x=0 到 1000, 步长 10)
# ============================================================
print("\n[2] 系统扫描主标签页...")

close_dialogs()
time.sleep(0.5)

tab_map = {}  # tab_name -> {'x_positions': [], 'buttons': set(), 'sub_pcs': []}

for x in range(0, 1000, 10):
    click_tab_x(pc_hwnd, x)
    
    vis_tabs = get_visible_tabsheets()
    vis_btns = get_visible_buttons()
    
    current_tab = vis_tabs[0]['title'] if vis_tabs else "?"
    btn_titles = [b['title'] for b in vis_btns]
    
    if current_tab not in tab_map:
        tab_map[current_tab] = {'x_positions': [], 'buttons': set(), 'hwnds': set()}
    tab_map[current_tab]['x_positions'].append(x)
    for bt in btn_titles:
        tab_map[current_tab]['buttons'].add(bt)
    for b in vis_btns:
        tab_map[current_tab]['hwnds'].add(b['hwnd'])

print("\n  标签页汇总:")
for tab_name, info in tab_map.items():
    x_range = f"{info['x_positions'][0]}-{info['x_positions'][-1]}"
    buttons = sorted(info['buttons'])
    print(f"\n  [{tab_name}] (x={x_range}, {len(info['x_positions'])}个位置)")
    if buttons:
        for b in buttons:
            print(f"    按钮: '{b}'")
    else:
        print(f"    (无按钮)")

# ============================================================
# Step 3: 查找 '求解电磁模型' 按钮
# ============================================================
print("\n\n[3] 搜索 '求解电磁模型' 按钮...")

solve_em_btn = None
solve_thermal_btn = None

# 在所有标签中搜索
for tab_name, info in tab_map.items():
    for btn_title in info['buttons']:
        if '求解电磁' in btn_title or ('Solve' in btn_title and 'Electro' in btn_title):
            print(f"  在 '{tab_name}' 标签找到: '{btn_title}'")
            # 找到对应的按钮信息
            for x in info['x_positions']:
                click_tab_x(pc_hwnd, x)
                vis_btns = get_visible_buttons()
                for b in vis_btns:
                    if b['title'] == btn_title:
                        solve_em_btn = b
                        print(f"    hwnd={b['hwnd']}, enabled={b['enabled']}, rect={b['rect']}")
                        break
                if solve_em_btn:
                    break
            break
    if solve_em_btn:
        break

if not solve_em_btn:
    print("  在主标签中未找到 '求解电磁模型'")
    print("  检查是否有子标签页 (嵌套 TPageControl)...")
    
    # 检查 '输入数据' 标签是否有子 TPageControl
    for tab_name in ['输入数据', 'Input Data', '求解设置', 'Solve Setup']:
        if tab_name not in tab_map:
            continue
        
        x_pos = tab_map[tab_name]['x_positions'][0]
        click_tab_x(pc_hwnd, x_pos)
        time.sleep(0.5)
        
        # 查找此标签页内的所有 TPageControl
        all_children = get_all_children(main_hwnd)
        sub_pcs = [c for c in all_children if c['class'] == 'TPageControl' and c['visible'] and c['hwnd'] != pc_hwnd]
        
        if sub_pcs:
            print(f"\n  '{tab_name}' 标签内有 {len(sub_pcs)} 个子 TPageControl:")
            for spc in sub_pcs:
                w = spc['rect'][2] - spc['rect'][0]
                h = spc['rect'][3] - spc['rect'][1]
                print(f"    hwnd={spc['hwnd']}, rect={spc['rect']}, size={w}x{h}")
                
                # 扫描子标签
                print(f"    扫描子标签 x=0-{w}...")
                for sx in range(0, w, 10):
                    click_tab_x(spc['hwnd'], sx)
                    vis_btns = get_visible_buttons()
                    solve_btns = [b for b in vis_btns if '求解' in b['title'] or 'Solve' in b['title']]
                    if solve_btns:
                        vis_tabs = get_visible_tabsheets()
                        sub_tab = vis_tabs[0]['title'] if vis_tabs else "?"
                        print(f"    x={sx}: 子标签='{sub_tab}'")
                        for b in solve_btns:
                            print(f"      *** 找到: '{b['title']}' hwnd={b['hwnd']} enabled={b['enabled']} rect={b['rect']}")
                            if '电磁' in b['title'] or 'Electro' in b['title']:
                                solve_em_btn = b
                            elif '热' in b['title'] or 'Thermal' in b['title']:
                                solve_thermal_btn = b
                    else:
                        # 也检查其他有意义的按钮
                        vis_tabs = get_visible_tabsheets()
                        if vis_tabs:
                            all_btns = [b['title'] for b in vis_btns]
                            if all_btns and any('计算' in t or '运行' in t or 'Run' in t for t in all_btns):
                                sub_tab = vis_tabs[0]['title']
                                print(f"    x={sx}: 子标签='{sub_tab}' 按钮={all_btns}")
        else:
            print(f"  '{tab_name}' 标签内无子 TPageControl")

# ============================================================
# Step 4: 查找 '求解热模型' 按钮
# ============================================================
print("\n\n[4] 搜索 '求解热模型' 按钮...")

if not solve_thermal_btn:
    for tab_name, info in tab_map.items():
        for btn_title in info['buttons']:
            if '求解热' in btn_title or ('Solve' in btn_title and 'Thermal' in btn_title):
                print(f"  在 '{tab_name}' 标签找到: '{btn_title}'")
                for x in info['x_positions']:
                    click_tab_x(pc_hwnd, x)
                    vis_btns = get_visible_buttons()
                    for b in vis_btns:
                        if b['title'] == btn_title:
                            solve_thermal_btn = b
                            print(f"    hwnd={b['hwnd']}, enabled={b['enabled']}, rect={b['rect']}")
                            break
                    if solve_thermal_btn:
                        break
                break
        if solve_thermal_btn:
            break

# ============================================================
# Step 5: 汇总找到的按钮
# ============================================================
print("\n\n" + "=" * 50)
print("按钮汇总")
print("=" * 50)

if solve_em_btn:
    print(f"  电磁求解: '{solve_em_btn['title']}'")
    print(f"    hwnd={solve_em_btn['hwnd']}, id={solve_em_btn['ctrl_id']}")
    print(f"    enabled={solve_em_btn['enabled']}, rect={solve_em_btn['rect']}")
else:
    print("  电磁求解: 未找到")

if solve_thermal_btn:
    print(f"  热求解: '{solve_thermal_btn['title']}'")
    print(f"    hwnd={solve_thermal_btn['hwnd']}, id={solve_thermal_btn['ctrl_id']}")
    print(f"    enabled={solve_thermal_btn['enabled']}, rect={solve_thermal_btn['rect']}")
else:
    print("  热求解: 未找到")

# ============================================================
# Step 6: 尝试点击求解按钮
# ============================================================

# FEResultsData 目录
fe_dir = r"D:\Motor-CAD\11_1_5\FEResultsData"
# 也检查可能的备用位置
fe_dir_alt = os.path.join(os.path.dirname(os.path.dirname(main_pc['rect'][0] and __file__)), 'FEResultsData')

# 先记录当前文件
files_before = set()
if os.path.exists(fe_dir):
    files_before = set(os.listdir(fe_dir))
    print(f"\n  FEResultsData 目录: {fe_dir}")
    print(f"  当前文件数: {len(files_before)}")
else:
    print(f"\n  FEResultsData 目录不存在: {fe_dir}")

# 尝试电磁求解
if solve_em_btn and solve_em_btn['enabled']:
    print(f"\n\n[6] 点击 '求解电磁模型' 按钮...")
    print(f"  使用 BM_CLICK...")
    
    close_dialogs()
    time.sleep(0.5)
    
    # 记录开始时间
    start_time = time.time()
    
    # 尝试 BM_CLICK
    send_bm_click(solve_em_btn['hwnd'])
    time.sleep(1)
    
    # 也尝试 PostMessage
    post_click_button(solve_em_btn['hwnd'])
    
    # 监控计算
    print(f"  监控计算进度...")
    max_wait = 180  # 3 minutes
    
    for wait_sec in range(max_wait):
        time.sleep(1)
        elapsed = int(time.time() - start_time)
        
        # 检查新文件
        files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        new_files = files_now - files_before
        
        if new_files:
            print(f"  [{elapsed}s] 新文件: {new_files}")
        
        # 检查弹窗
        dialogs = close_dialogs()
        if dialogs:
            print(f"  [{elapsed}s] 弹窗: {dialogs}")
        
        # 检查状态栏
        status = get_status_bar_text()
        if status:
            print(f"  [{elapsed}s] 状态栏: {status}")
        
        # 检查按钮状态变化
        current_btns = get_visible_buttons()
        cancel_visible = any('取消' in b['title'] for b in current_btns)
        solve_visible = any('求解电磁' in b['title'] for b in current_btns)
        
        if cancel_visible and not solve_visible:
            print(f"  [{elapsed}s] 计算中 (取消按钮可见)...")
        elif not cancel_visible and solve_visible and new_files:
            print(f"  [{elapsed}s] 计算完成!")
            break
        
        if elapsed % 15 == 0 and elapsed > 0:
            print(f"  [{elapsed}s] 等待中... (新文件数: {len(new_files)})")
        
        if elapsed >= max_wait:
            print(f"  超时 ({max_wait}s)")
            break
    
    # 最终状态
    files_after = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
    final_new = files_after - files_before
    print(f"\n  新增文件: {len(final_new)}")
    for f in sorted(final_new):
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        print(f"    {f:40s} {size:10d} bytes")

# 尝试热求解
if solve_thermal_btn and solve_thermal_btn['enabled']:
    print(f"\n\n[7] 点击 '求解热模型' 按钮...")
    
    close_dialogs()
    time.sleep(0.5)
    
    # 先切换到正确的标签
    for tab_name, info in tab_map.items():
        if any('求解热' in bt for bt in info['buttons']):
            click_tab_x(pc_hwnd, info['x_positions'][0])
            break
    
    time.sleep(0.5)
    
    # 重新查找按钮（因为切换标签后 hwnd 可能变化）
    vis_btns = get_visible_buttons()
    for b in vis_btns:
        if '求解热' in b['title']:
            solve_thermal_btn = b
            break
    
    if solve_thermal_btn:
        files_before_thermal = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        
        send_bm_click(solve_thermal_btn['hwnd'])
        time.sleep(1)
        post_click_button(solve_thermal_btn['hwnd'])
        
        print(f"  监控热计算进度...")
        for wait_sec in range(120):
            time.sleep(1)
            
            files_now = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
            new_files = files_now - files_before_thermal
            
            if new_files:
                print(f"  [{wait_sec}s] 新文件: {new_files}")
            
            dialogs = close_dialogs()
            if dialogs:
                print(f"  [{wait_sec}s] 弹窗: {dialogs}")
            
            status = get_status_bar_text()
            if status:
                print(f"  [{wait_sec}s] 状态栏: {status}")
            
            if wait_sec % 15 == 0 and wait_sec > 0:
                print(f"  [{wait_sec}s] 等待中...")
            
            if wait_sec >= 120:
                print(f"  超时")
                break
        
        files_after_thermal = set(os.listdir(fe_dir)) if os.path.exists(fe_dir) else set()
        final_new_thermal = files_after_thermal - files_before_thermal
        print(f"\n  热计算新增文件: {len(final_new_thermal)}")
        for f in sorted(final_new_thermal):
            fpath = os.path.join(fe_dir, f)
            size = os.path.getsize(fpath)
            print(f"    {f:40s} {size:10d} bytes")

# ============================================================
# Step 8: 解析结果文件
# ============================================================
print(f"\n\n[8] 解析结果文件...")

if os.path.exists(fe_dir):
    all_files = sorted(os.listdir(fe_dir))
    print(f"  FEResultsData 目录 ({len(all_files)} 个文件):")
    
    for f in all_files:
        fpath = os.path.join(fe_dir, f)
        size = os.path.getsize(fpath)
        mtime = os.path.getmtime(fpath)
        mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
        print(f"    {f:45s} {size:10d} bytes  {mtime_str}")
    
    # 尝试解析 .mes 文件
    mes_files = [f for f in all_files if f.endswith('.mes')]
    if mes_files:
        print(f"\n  .mes 结果文件 ({len(mes_files)} 个):")
        for mf in mes_files:
            fpath = os.path.join(fe_dir, mf)
            print(f"\n  --- {mf} ---")
            try:
                with open(fpath, 'r', errors='replace') as fh:
                    content = fh.read()
                    # 打印前 2000 字符
                    print(content[:2000])
                    if len(content) > 2000:
                        print(f"  ... (总 {len(content)} 字符)")
            except Exception as e:
                print(f"  读取错误: {e}")
    
    # 也检查 .ugf 文件
    ugf_files = [f for f in all_files if f.endswith('.ugf')]
    if ugf_files:
        print(f"\n  .ugf 文件 ({len(ugf_files)} 个):")
        for uf in ugf_files[:3]:
            fpath = os.path.join(fe_dir, uf)
            size = os.path.getsize(fpath)
            print(f"    {uf}: {size} bytes")
    
    # 检查 .txt 或其他结果文件
    other_files = [f for f in all_files if not f.endswith(('.mes', '.ugf'))]
    if other_files:
        print(f"\n  其他文件 ({len(other_files)} 个):")
        for of in other_files:
            fpath = os.path.join(fe_dir, of)
            size = os.path.getsize(fpath)
            print(f"    {of}: {size} bytes")

# ============================================================
# Step 9: 检查 MotorLAB 输出目录
# ============================================================
print(f"\n\n[9] 检查 MotorLAB 输出...")

motorlab_dirs = [
    r"D:\Motor-CAD\11_1_5\Motor-LAB",
    r"D:\Motor-CAD\11_1_5\FEResultsData",
    r"D:\Motor-CAD\11_1_5",
]

for d in motorlab_dirs:
    if os.path.exists(d):
        # 查找 .mat 文件
        for root, dirs, files in os.walk(d):
            for f in files:
                if f.endswith('.mat'):
                    fpath = os.path.join(root, f)
                    size = os.path.getsize(fpath)
                    mtime = os.path.getmtime(fpath)
                    mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
                    print(f"  .mat: {fpath} ({size} bytes, {mtime_str})")
            # 只搜索一级
            if root != d:
                break

print("\n\n" + "=" * 70)
print("扫描完成!")
print("=" * 70)

# 保存结果
results = {
    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
    'mc_pid': mc_pid,
    'main_hwnd': main_hwnd,
    'tab_map': {k: {
        'x_range': f"{v['x_positions'][0]}-{v['x_positions'][-1]}",
        'buttons': sorted(v['buttons']),
    } for k, v in tab_map.items()},
    'solve_em_btn': solve_em_btn,
    'solve_thermal_btn': solve_thermal_btn,
    'fe_dir': fe_dir,
    'fe_files': sorted(os.listdir(fe_dir)) if os.path.exists(fe_dir) else [],
}

results_path = r"D:\MotorDesign\Scripts\scan_results.json"
with open(results_path, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存: {results_path}")
