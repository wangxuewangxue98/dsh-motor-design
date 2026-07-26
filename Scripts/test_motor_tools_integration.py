# -*- coding: utf-8 -*-
"""
test_motor_tools_integration.py
测试 motor_tools.py 与 motorcad_auto 的集成
验证: COM 连接 → 电磁分析 → 热分析 → 结果格式
"""
import sys
import os
import time

# 确保路径正确
BASE_DIR = r"D:\MotorDesign"
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "Scripts"))

# 无缓冲输出
sys.stdout = open(sys.stdout.fileno(), mode='w', buffering=1)

print("=" * 60)
print("motor_tools.py + motorcad_auto 集成测试")
print("=" * 60)

# 导入 motor_tools
print("\n[1] 导入 motor_tools...")
from motor_tools import (
    _motorcad_available,
    _MOTORCAD_AUTO_AVAILABLE,
    MOTORCAD_USE_MODE,
    _get_motorcad_instance,
    _run_motorcad_with_instance,
    _safe_getvar,
    check_motorcad_status,
    generate_param_matrix,
    batch_solve,
    reset_solve_log,
)
print(f"  Motor-CAD available: {_motorcad_available}")
print(f"  motorcad_auto available: {_MOTORCAD_AUTO_AVAILABLE}")
print(f"  USE_MODE: {MOTORCAD_USE_MODE}")

if not _motorcad_available or not _MOTORCAD_AUTO_AVAILABLE:
    print("\n[FAIL] 前置条件不满足, 退出")
    sys.exit(1)

# 检查状态
print("\n[2] 检查 Motor-CAD 状态...")
status = check_motorcad_status()
print(f"  installed: {status['installed']}")
print(f"  com_available: {status['com_available']}")
print(f"  motorcad_auto_available: {status.get('motorcad_auto_available', 'N/A')}")
print(f"  activex_mode: {status.get('activex_mode', 'N/A')}")
print(f"  template_path: {status.get('template_path', 'N/A')}")
print(f"  template_exists: {status.get('template_exists', 'N/A')}")
print(f"  licence_note: {status.get('licence_note', 'N/A')[:100]}")

# 生成测试参数 (仅 1 组, 使用已知安全参数)
print("\n[3] 生成测试参数...")
params = {
    "stator_od": 220,
    "poles": 10,
    "voltage": 350,       # DCBusVoltage (已验证安全)
    "peak_current": 100,  # PeakCurrent (已验证安全, 不要用 120+)
    "speed": 130,
    "slots_stator": 42,
    "slots_rotor": 34,
    "turns_per_coil": 10,
    "parallel_circuits": 1,
}
print(f"  参数: {params}")

# COM 连接 + 单次求解
print("\n[4] 连接 Motor-CAD COM (约 60-90 秒)...")
t0 = time.time()
try:
    connector = _get_motorcad_instance()
    t_connect = time.time() - t0
    print(f"  COM 连接成功! ({t_connect:.1f}s)")
except Exception as e:
    t_connect = time.time() - t0
    print(f"  COM 连接失败 ({t_connect:.1f}s): {e}")
    sys.exit(1)

# 执行求解
print("\n[5] 执行 Motor-CAD 求解 (电磁+热, 预计 4-5 分钟)...")
t0 = time.time()
try:
    result = _run_motorcad_with_instance(connector, params)
    t_solve = time.time() - t0
    print(f"\n  求解完成! ({t_solve:.1f}s)")
    print(f"  效率: {result['efficiency']:.2f}%")
    print(f"  温度: {result['max_temp']:.1f}°C")
    print(f"  转矩: {result['torque']:.2f} N·m")
    print(f"  功率: {result['power']:.2f} kW")
    print(f"  铜耗: {result['copper_loss']:.1f} W")
    print(f"  铁耗: {result['iron_loss']:.1f} W")
    print(f"  总损耗: {result['total_loss']:.1f} W")
    print(f"  EMag返回: {result.get('emag_return', 'N/A')}")
    print(f"  热分析返回: {result.get('thermal_return', 'N/A')}")
    print(f"  EMag时间: {result.get('emag_time_s', 'N/A')}s")
    print(f"  热分析时间: {result.get('thermal_time_s', 'N/A')}s")
    print(f"  轴转速: {result.get('shaft_speed_rpm', 'N/A')} rpm")
    print(f"  输入功率: {result.get('input_power_w', 'N/A')} W")
    print(f"  输出功率: {result.get('output_power_w', 'N/A')} W")

    # 保存结果
    import json
    result["params"] = params
    result["connect_time_s"] = round(t_connect, 1)
    result["solve_time_s"] = round(t_solve, 1)
    result["total_time_s"] = round(t_connect + t_solve, 1)

    result_path = os.path.join(BASE_DIR, "Reports", "motor_tools_integration_test.json")
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  结果已保存: {result_path}")

    print("\n[6] 断开 COM 连接...")
    connector.disconnect()
    print("  已断开")

    print("\n" + "=" * 60)
    print("✓ 集成测试通过!")
    print("=" * 60)

except Exception as e:
    t_solve = time.time() - t0
    print(f"\n  求解失败 ({t_solve:.1f}s): {e}")
    import traceback
    traceback.print_exc()
    try:
        connector.disconnect()
    except:
        pass
    sys.exit(1)
