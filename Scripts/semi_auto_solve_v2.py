#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-CAD 半自动求解工作流 v2 (增强版)

由于 COM 自动化和所有 UI 自动化方法均无效 (Motor-CAD v11.1.5 限制),
实现完整的半自动工作流:

1. 自动修改 .mot 文件参数 (转速、铜损、水套流量等)
2. 自动启动 Motor-CAD 加载修改后的文件
3. 自动切换到"求解设置"标签页
4. 通知用户手动点击"求解热模型"按钮
5. 监控 FEResultsData 目录变化, 检测计算开始和完成
6. 计算完成后, 尝试多种方式读取结果:
   a. 通过菜单 WM_COMMAND 切换到温度视图
   b. 读取可见文本控件
   c. 提示用户手动导出/输入结果
7. 保存结果到 JSON

用法:
  python semi_auto_solve_v2.py [--mot <mot_file>] [--params <json_file>]
  
参数 JSON 格式:
  {
    "Shaft_Speed_[RPM]": 3000,
    "Stator_Copper_Loss_@Ref_Speed": 400,
    "WJ_Fluid_Inlet_Temperature": 40,
    "WJ_Fluid_Volume_Flow_Rate": 0.0000835
  }
"""
import sys
import os
import time
import struct
import shutil
import json
import argparse
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
OUTPUT_DIR = r"D:\MotorDesign\exports"
RESULT_FILE = os.path.join(OUTPUT_DIR, "motorcad_result.json")

# 标签页 x 坐标映射 (在 PageControl 顶部 y=12 点击)
TAB_X = {
    'geometry': 50,      # 几何
    'winding': 90,       # 绕组
    'input_data': 155,   # 输入数据
    'solve_setup': 240,  # 求解设置
    'temperature': 310,  # 温度
    'output_data': 380,  # 输出数据
    'param_sens': 470,   # 参数相关性
    'script': 545,       # 脚本
    'fluid': 700,        # 流体
}

# 菜单命令 ID
MENU_RUN_SOLVE = 119    # 结果 > 运行求解 (切换到求解视图)
MENU_EXIT_SOLVE = 120   # 结果 > 退出求解 (切换到结果/温度视图)
MENU_NODE_TEMP = 116    # 结果 > 节点温度
MENU_DATA_OUTPUT = 117  # 结果 > 数据输出


def modify_mot_params(mot_path, params, output_path=None):
    """修改 .mot 文件中的参数
    
    Args:
        mot_path: 原始 .mot 文件路径
        params: 参数字典 {key: value}
        output_path: 输出路径 (None 则覆盖原文件)
    
    Returns:
        output_path
    """
    if output_path is None:
        output_path = mot_path
    
    with open(mot_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    
    # 构建参数查找表
    param_keys = set(params.keys())
    modified = {}
    
    for i, line in enumerate(lines):
        if '=' in line:
            key = line.split('=', 1)[0].strip()
            if key in param_keys:
                old_val = line.split('=', 1)[1].strip()
                lines[i] = f"{key}={params[key]}\n"
                modified[key] = {'old': old_val, 'new': str(params[key])}
    
    # 如果参数不存在, 添加到文件末尾
    for key in param_keys:
        if key not in modified:
            lines.append(f"{key}={params[key]}\n")
            modified[key] = {'old': '(new)', 'new': str(params[key])}
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    print(f"  .mot 文件已修改: {output_path}")
    for key, val in modified.items():
        print(f"    {key}: {val['old']} -> {val['new']}")
    
    return output_path


def find_motorcad():
    """查找 Motor-CAD 主窗口"""
    main_hwnd = None
    mc_pid = None
    
    def cb(hwnd, p):
        nonlocal main_hwnd, mc_pid
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if cls == 'TMainForm' and 'Motor' in title and vis:
                main_hwnd = hwnd
                _, mc_pid = win32process.GetWindowThreadProcessId(hwnd)
        except:
            pass
        return True
    
    win32gui.EnumWindows(cb, None)
    return main_hwnd, mc_pid


def switch_tab(main_hwnd, tab_name='solve_setup'):
    """切换到指定标签页"""
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
    
    if not pcs:
        return False
    
    pc_hwnd = max(pcs, key=lambda x: x['size'])['hwnd']
    x = TAB_X.get(tab_name, 240)
    
    lp = win32api.MAKELONG(x, 12)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
    time.sleep(0.1)
    win32gui.PostMessage(pc_hwnd, win32con.WM_LBUTTONUP, 0, lp)
    time.sleep(1)
    return True


def send_menu_command(main_hwnd, cmd_id):
    """发送 WM_COMMAND 菜单命令"""
    win32gui.SendMessage(main_hwnd, win32con.WM_COMMAND, cmd_id, 0)
    time.sleep(1)


def read_visible_texts(main_hwnd):
    """读取所有可见文本控件"""
    texts = []
    def cb(hwnd, p):
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            vis = win32gui.IsWindowVisible(hwnd)
            if vis and title and len(title) > 0:
                texts.append({'cls': cls, 'title': title, 'hwnd': hwnd})
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, cb, None)
    return texts


def monitor_fe_changes(files_before, timeout=3600, main_hwnd=None):
    """监控 FEResultsData 目录变化
    
    Returns:
        (changed: bool, new_files: set, modified_files: set, duration: int)
    """
    calc_started = False
    start_time = time.time()
    last_change_time = None
    
    print(f"\n  初始文件数: {len(files_before)}")
    print(f"  请在 Motor-CAD 中手动点击 '求解热模型' 按钮!")
    print(f"  系统将自动监控计算完成 (最多等待 {timeout} 秒)...")
    print()
    
    for wait in range(timeout):
        time.sleep(1)
        
        # 检查文件变化
        files_now = {}
        if os.path.exists(FE_DIR):
            for f in os.listdir(FE_DIR):
                fpath = os.path.join(FE_DIR, f)
                try:
                    files_now[f] = {
                        'size': os.path.getsize(fpath),
                        'mtime': os.path.getmtime(fpath)
                    }
                except:
                    pass
        
        new_files = set(files_now.keys()) - set(files_before.keys())
        modified_files = set()
        for f in files_before:
            if f in files_now:
                if files_now[f]['mtime'] != files_before[f]['mtime']:
                    modified_files.add(f)
                elif files_now[f]['size'] != files_before[f]['size']:
                    modified_files.add(f)
        
        if new_files or modified_files:
            if not calc_started:
                calc_started = True
                print(f"  [{wait}s] *** 检测到计算开始! ***")
                print(f"    新文件: {len(new_files)}, 修改文件: {len(modified_files)}")
            last_change_time = wait
        
        # 计算完成的判断: 有变化后 10 秒内无新变化
        if calc_started and last_change_time and wait > last_change_time + 10:
            print(f"  [{wait}s] *** 计算完成! ***")
            return True, new_files, modified_files, int(time.time() - start_time)
        
        # 每 30 秒打印等待状态
        if wait % 30 == 0 and wait > 0:
            print(f"  [{wait}s] 等待中... 新={len(new_files)} 改={len(modified_files)}")
    
    print(f"  超时 ({timeout}秒)")
    return calc_started, new_files, modified_files, int(time.time() - start_time)


def extract_results(main_hwnd):
    """尝试从 Motor-CAD UI 提取结果"""
    results = {}
    
    # 方法1: 通过菜单切换到温度视图, 读取文本
    print("  切换到温度视图...")
    send_menu_command(main_hwnd, MENU_EXIT_SOLVE)  # ID=120 切换到结果视图
    time.sleep(2)
    
    texts = read_visible_texts(main_hwnd)
    print(f"  可见文本控件: {len(texts)} 个")
    
    # 提取温度相关文本
    temp_texts = []
    for t in texts:
        title = t['title']
        # 查找包含数字的文本 (可能是温度值)
        if any(c.isdigit() for c in title) and len(title) < 50:
            temp_texts.append(title)
            # 尝试匹配 "节点名 温度值" 或 "温度值 °C" 等模式
            for kw in ['Winding', 'Housing', 'Magnet', 'Tooth', 'Stator', 'Rotor', 'Shaft', 'Bearing']:
                if kw in title:
                    results[kw] = title
    
    if temp_texts:
        print(f"  可能的结果文本 ({len(temp_texts)} 个):")
        for t in temp_texts[:20]:
            print(f"    '{t}'")
    
    # 方法2: 切换到"输出数据"标签页
    print("  切换到输出数据标签页...")
    switch_tab(main_hwnd, 'output_data')
    time.sleep(2)
    
    texts2 = read_visible_texts(main_hwnd)
    for t in texts2:
        if any(c.isdigit() for c in t['title']) and len(t['title']) < 50:
            results[f"output_{t['cls']}"] = t['title']
    
    return results, temp_texts


def run_semi_auto_solve(mot_file=MOT_FILE, params=None, output_mot=None):
    """执行半自动求解工作流"""
    print("=" * 70)
    print("Motor-CAD 半自动求解工作流 v2")
    print("=" * 70)
    
    # Step 1: 修改参数
    if params:
        print(f"\n[1] 修改 .mot 文件参数...")
        if output_mot is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output_mot = os.path.join(OUTPUT_DIR, f"MotorCAD_{timestamp}.mot")
        modify_mot_params(mot_file, params, output_mot)
        work_mot = output_mot
    else:
        work_mot = mot_file
        print(f"\n[1] 使用现有 .mot 文件: {work_mot}")
    
    # Step 2: 查找或启动 Motor-CAD
    print(f"\n[2] 查找 Motor-CAD...")
    main_hwnd, mc_pid = find_motorcad()
    
    if not main_hwnd:
        print("  Motor-CAD 未运行, 启动中...")
        import subprocess
        subprocess.Popen([MCAD_EXE, work_mot])
        print("  等待 70 秒...")
        time.sleep(70)
        main_hwnd, mc_pid = find_motorcad()
    
    if not main_hwnd:
        print("  *** Motor-CAD 启动失败! ***")
        return None
    
    print(f"  Motor-CAD: hwnd={main_hwnd}, pid={mc_pid}")
    print(f"  标题: '{win32gui.GetWindowText(main_hwnd)[:60]}'")
    
    # Step 3: 记录 FEResultsData 初始状态
    print(f"\n[3] 记录初始状态...")
    files_before = {}
    if os.path.exists(FE_DIR):
        for f in os.listdir(FE_DIR):
            fpath = os.path.join(FE_DIR, f)
            try:
                files_before[f] = {
                    'size': os.path.getsize(fpath),
                    'mtime': os.path.getmtime(fpath)
                }
            except:
                pass
    print(f"  FEResultsData 文件数: {len(files_before)}")
    
    # Step 4: 切换到求解标签页
    print(f"\n[4] 切换到 '求解设置' 标签页...")
    send_menu_command(main_hwnd, MENU_RUN_SOLVE)  # ID=119 切换到求解视图
    time.sleep(1)
    switch_tab(main_hwnd, 'solve_setup')
    time.sleep(1)
    
    # 查找求解按钮
    solve_btn_info = None
    def find_solve_btn(hwnd, p):
        nonlocal solve_btn_info
        try:
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls in ('TButton', 'TBitBtn') and ('求解' in title or 'Solve' in title):
                rect = win32gui.GetWindowRect(hwnd)
                solve_btn_info = {'hwnd': hwnd, 'title': title, 'rect': rect}
        except:
            pass
        return True
    win32gui.EnumChildWindows(main_hwnd, find_solve_btn, None)
    
    if solve_btn_info:
        print(f"  求解按钮: '{solve_btn_info['title']}' rect={solve_btn_info['rect']}")
        print(f"  按钮位置 (屏幕坐标): 中心 ({(solve_btn_info['rect'][0]+solve_btn_info['rect'][2])//2}, {(solve_btn_info['rect'][1]+solve_btn_info['rect'][3])//2})")
    else:
        print("  未找到求解按钮 (按钮可能不可见)")
    
    # Step 5: 监控计算
    print(f"\n[5] 等待用户手动求解...")
    print(f"  ╔══════════════════════════════════════════════════╗")
    print(f"  ║  请在 Motor-CAD 窗口中手动点击 '求解热模型' 按钮  ║")
    print(f"  ║  系统会自动检测计算完成                          ║")
    print(f"  ╚══════════════════════════════════════════════════╝")
    
    changed, new_files, modified_files, duration = monitor_fe_changes(
        files_before, timeout=3600, main_hwnd=main_hwnd
    )
    
    # Step 6: 提取结果
    print(f"\n[6] 提取计算结果...")
    ui_results, temp_texts = extract_results(main_hwnd)
    
    # Step 7: 保存结果
    print(f"\n[7] 保存结果...")
    result = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'mot_file': work_mot,
        'params': params or {},
        'calculation_completed': changed,
        'duration_seconds': duration,
        'changed_files': sorted(list(new_files | modified_files)),
        'new_files': sorted(list(new_files)),
        'modified_files': sorted(list(modified_files)),
        'ui_results': ui_results,
        'temp_texts': temp_texts[:20],
        'solve_mode': 'manual_import',
        'note': '半自动求解: 用户手动点击求解按钮, 脚本自动监控和读取结果',
    }
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(RESULT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"  结果已保存: {RESULT_FILE}")
    print(f"  计算完成: {changed}")
    print(f"  耗时: {duration} 秒")
    print(f"  变化文件: {len(new_files)} 新, {len(modified_files)} 改")
    
    if not ui_results and not temp_texts:
        print(f"\n  ⚠️ 无法自动读取温度结果")
        print(f"  请通过以下方式获取结果:")
        print(f"    1. 在 Motor-CAD 中查看 '输出数据' > 'Temperatures' 标签")
        print(f"    2. 使用菜单 '结果 > 节点温度' 查看温度")
        print(f"    3. 手动记录温度值并输入到系统中")
    
    print(f"\n{'='*70}")
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Motor-CAD 半自动求解')
    parser.add_argument('--mot', default=MOT_FILE, help='.mot 文件路径')
    parser.add_argument('--params', help='参数 JSON 文件路径')
    args = parser.parse_args()
    
    params = None
    if args.params and os.path.exists(args.params):
        with open(args.params, 'r') as f:
            params = json.load(f)
    
    result = run_semi_auto_solve(mot_file=args.mot, params=params)
    
    if result:
        print("\n完成!")
    else:
        print("\n失败!")
        sys.exit(1)
