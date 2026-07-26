#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Motor-LAB Runtime 桥接测试
策略: 启动 Motor-CAD + 直接调用 MotorLABruntime.exe 进行计算
绕过 COM 自动化 (Motor-CAD v11.1.5 不注册 class factory)
"""
import sys
import os
import time
import subprocess
import struct

EXE_PATH = r"D:\Motor-CAD\11_1_5\Motor-CAD_11_1_5.exe"
EXE_DIR = r"D:\Motor-CAD\11_1_5"
LAB_EXE = r"D:\Motor-CAD\11_1_5\Motor-LAB\lib\MotorLABruntime.exe"
TEMPLATE = r"D:\Motor-CAD\11_1_5\ActiveX_Automation_in_Motor-CAD\Matlab_Test.mot"

print("=" * 60)
print("Motor-LAB Runtime 桥接测试")
print("=" * 60)

# Step 1: Start Motor-CAD with template file
print("\n[1/3] 启动 Motor-CAD (加载模板)...")
cmd = [EXE_PATH, TEMPLATE]
print(f"  命令: {' '.join(cmd)}")
proc = subprocess.Popen(cmd, cwd=EXE_DIR)
pid = proc.pid
print(f"  Motor-CAD PID = {pid}")

# Wait for Motor-CAD to start
print("  等待 Motor-CAD 启动 (30秒)...")
time.sleep(30)

# Check if process is still running
if proc.poll() is not None:
    print(f"  Motor-CAD 退出! code={proc.returncode}")
    sys.exit(1)
print("  Motor-CAD 运行中")

# Step 2: Try MotorLABruntime.exe with the PID
print(f"\n[2/3] 调用 MotorLABruntime.exe (PID={pid})...")

# The MATLAB example uses: MotorLABruntime.exe <command> <processID> <exeName>
# Try BuildModel first (needed before calculations)
exe_name = "Motor-CAD_11_1_5.exe"

commands_to_try = [
    ["BuildModel", str(pid), exe_name],
    ["CalculateElectromagnetic", str(pid), exe_name],
]

for cmd_args in commands_to_try:
    full_cmd = [LAB_EXE] + cmd_args
    print(f"\n  尝试: {' '.join(cmd_args)}")
    print(f"  完整命令: {' '.join(full_cmd)}")
    
    try:
        result = subprocess.run(
            full_cmd,
            cwd=os.path.dirname(LAB_EXE),
            capture_output=True,
            text=True,
            timeout=60,
            encoding='utf-8',
            errors='replace'
        )
        print(f"  返回码: {result.returncode}")
        if result.stdout:
            print(f"  stdout (前500字符): {result.stdout[:500]}")
        if result.stderr:
            print(f"  stderr (前500字符): {result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        print("  超时 (60秒)")
    except Exception as e:
        print(f"  错误: {e}")

# Step 3: Check for output files
print("\n[3/3] 检查输出文件...")

# Motor-CAD might save results in various locations
output_dirs = [
    os.path.join(EXE_DIR, "Motor-CAD DataX"),
    os.path.join(EXE_DIR, "EmagX"),
    os.path.join(EXE_DIR, "FEResultsDataX"),
    EXE_DIR,
    os.path.dirname(TEMPLATE),
]

for d in output_dirs:
    if os.path.exists(d):
        files = os.listdir(d)
        recent = [f for f in files if f.endswith(('.txt', '.csv', '.dat', '.log', '.out'))]
        if recent:
            print(f"\n  {d}:")
            for f in recent[:10]:
                fpath = os.path.join(d, f)
                mtime = os.path.getmtime(fpath)
                age = time.time() - mtime
                if age < 300:  # Modified in last 5 minutes
                    print(f"    [NEW] {f} ({os.path.getsize(fpath)} bytes, {age:.0f}s ago)")
                else:
                    print(f"    {f} ({os.path.getsize(fpath)} bytes)")

# Also check temp directory
import tempfile
temp_dir = tempfile.gettempdir()
mc_temp = [f for f in os.listdir(temp_dir) if 'motor' in f.lower() or 'mcad' in f.lower()]
if mc_temp:
    print(f"\n  Temp目录中的 Motor-CAD 文件:")
    for f in mc_temp[:10]:
        print(f"    {f}")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
print(f"\nMotor-CAD PID={pid} 仍在运行")
print("可以检查 Motor-CAD 界面是否已加载模板并显示模型")
