# -*- coding: utf-8 -*-
"""
cli.py - Motor-CAD v11.1.5 自动化命令行工具

用法:
  python -m motorcad_auto.cli status          # 检查 Motor-CAD 状态
  python -m motorcad_auto.cli thermal          # 仅热分析
  python -m motorcad_auto.cli emag             # 仅电磁分析
  python -m motorcad_auto.cli full             # 完整分析 (热+电磁)
  python -m motorcad_auto.cli full --voltage 350 --current 100
  python -m motorcad_auto.cli batch --count 5  # 批量参数扫描

环境变量:
  MOTORCAD_EXE_PATH    - Motor-CAD .exe 路径
  MOTORCAD_MOT_FILE    - .mot 模型文件路径
"""
import sys
import os
import json
import time
import logging
import argparse

# 确保父目录在 path 中 (直接运行时)
_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from motorcad_auto import MotorCADRunner, MotorCADConfig


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def get_config(args) -> MotorCADConfig:
    """从命令行参数构建配置"""
    config = MotorCADConfig()
    if args.mot_file:
        config.mot_file = args.mot_file
    elif os.getenv("MOTORCAD_MOT_FILE"):
        config.mot_file = os.getenv("MOTORCAD_MOT_FILE")
    return config


def cmd_status(args):
    """检查 Motor-CAD 状态"""
    config = get_config(args)
    runner = MotorCADRunner(config)

    print("=" * 50)
    print("Motor-CAD v11.1.5 状态检查")
    print("=" * 50)

    status = runner.check_status()

    print(f"\nEXE 路径: {status['exe_path']}")
    print(f"EXE 存在: {'是' if status['exe_exists'] else '否'}")
    print(f"主窗口:   {'已找到' if status['main_window_found'] else '未找到'}")
    print(f"ActiveX 模式: {status['activex_mode']}")

    if status["activex_mode"] == "none":
        print("\n[警告] ActiveX 模式为 '免注册', COM 不可用!")
        print("       请在 Motor-CAD 菜单中切换: 默认设置 -> 控件注册 -> 单客户端设置")
    elif status["activex_mode"] == "single":
        print("\n[OK] ActiveX 模式为 '单客户端设置', COM 可用")

    # .mot 文件
    mot = config.get_mot_file()
    print(f"\n.mot 文件: {mot}")
    print(f"  存在: {'是' if os.path.exists(mot) else '否'}")

    # 尝试连接
    print("\n尝试 COM 连接...")
    try:
        runner.connect()
        print("[OK] COM 连接成功!")

        # 读取模型信息
        if runner.connector.is_connected:
            from motorcad_auto.params import ParameterManager
            pm = ParameterManager(runner.connector, config)
            print("\n模型信息:")
            info = pm.read_model_info()
            for k, v in info.items():
                print(f"  {k}: {v}")

        runner.disconnect()
    except Exception as e:
        print(f"[失败] COM 连接: {e}")

    return 0


def cmd_thermal(args):
    """仅热分析"""
    config = get_config(args)
    runner = MotorCADRunner(config)

    params = {}
    if args.voltage:
        params["DCBusVoltage"] = args.voltage
    if args.current:
        params["PeakCurrent"] = args.current

    print("执行热分析...")
    with runner:
        result = runner.quick_thermal()

    print(result.summary())

    # 保存 JSON
    json_path = os.path.join(config.result_dir, "thermal_result.json")
    os.makedirs(config.result_dir, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"\n结果已保存: {json_path}")

    return 0 if result.thermal_return == 0 else 1


def cmd_emag(args):
    """仅电磁分析"""
    config = get_config(args)
    runner = MotorCADRunner(config)

    params = {}
    if args.voltage:
        params["DCBusVoltage"] = args.voltage
    if args.current:
        params["PeakCurrent"] = args.current

    print("执行电磁分析...")
    with runner:
        result = runner.quick_emag()

    print(result.summary())

    json_path = os.path.join(config.result_dir, "emag_result.json")
    os.makedirs(config.result_dir, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"\n结果已保存: {json_path}")

    return 0 if result.emag_return == 0 else 1


def cmd_full(args):
    """完整分析 (热 + 电磁)"""
    config = get_config(args)
    runner = MotorCADRunner(config)

    params = {}
    if args.voltage:
        params["DCBusVoltage"] = args.voltage
    if args.current:
        params["PeakCurrent"] = args.current

    print("执行完整分析 (热 + 电磁)...")
    with runner:
        result = runner.run_single(params)

    print(result.summary())

    json_path = os.path.join(config.result_dir, "full_result.json")
    os.makedirs(config.result_dir, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"\n结果已保存: {json_path}")

    return 0


def cmd_batch(args):
    """批量参数扫描"""
    config = get_config(args)
    runner = MotorCADRunner(config)

    # 生成参数扫描矩阵
    param_list = []
    count = args.count or 5
    base_voltage = args.voltage or 350
    base_current = args.current or 100

    for i in range(count):
        param_list.append({
            "DCBusVoltage": base_voltage + i * 10,
            "PeakCurrent": base_current + i * 5,
        })

    print(f"批量参数扫描: {count} 个方案")
    for i, p in enumerate(param_list):
        print(f"  方案 {i+1}: {p}")

    json_path = os.path.join(config.result_dir, "batch_results.json")

    with runner:
        results = runner.run_batch(param_list, save_json=True, json_path=json_path)

    # 打印汇总
    print("\n" + "=" * 60)
    print("批量分析汇总")
    print("=" * 60)
    for i, r in enumerate(results):
        thermal_status = "OK" if r.thermal_return == 0 else f"FAIL({r.thermal_return})"
        emag_status = "OK" if r.emag_return == 0 else f"FAIL({r.emag_return})"
        temp = r.thermal.get("T_[Winding_Average]", "N/A")
        if isinstance(temp, (int, float)):
            temp = f"{temp:.1f}C"
        print(f"  方案 {i+1}: 热分析={thermal_status}, 电磁={emag_status}, "
              f"绕组温度={temp}")

    print(f"\n详细结果: {json_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Motor-CAD v11.1.5 COM 自动化工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mot-file", "-m", help=".mot 模型文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # status
    sp = subparsers.add_parser("status", help="检查 Motor-CAD 状态")

    # thermal
    sp = subparsers.add_parser("thermal", help="仅热分析")
    sp.add_argument("--voltage", type=float, help="DC 母线电压 (V)")
    sp.add_argument("--current", type=float, help="峰值电流 (A)")

    # emag
    sp = subparsers.add_parser("emag", help="仅电磁分析")
    sp.add_argument("--voltage", type=float, help="DC 母线电压 (V)")
    sp.add_argument("--current", type=float, help="峰值电流 (A)")

    # full
    sp = subparsers.add_parser("full", help="完整分析 (热+电磁)")
    sp.add_argument("--voltage", type=float, help="DC 母线电压 (V)")
    sp.add_argument("--current", type=float, help="峰值电流 (A)")

    # batch
    sp = subparsers.add_parser("batch", help="批量参数扫描")
    sp.add_argument("--count", type=int, default=5, help="方案数量")
    sp.add_argument("--voltage", type=float, default=350, help="基准电压")
    sp.add_argument("--current", type=float, default=100, help="基准电流")

    args = parser.parse_args()

    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "status": cmd_status,
        "thermal": cmd_thermal,
        "emag": cmd_emag,
        "full": cmd_full,
        "batch": cmd_batch,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
