# -*- coding: utf-8 -*-
"""
test_motorcad_auto.py - motorcad_auto 项目端到端测试
分步骤验证, 无缓冲输出, 带全局超时保护
"""
import sys, os, time, json, threading

# 无缓冲
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
os.environ['PYTHONUNBUFFERED'] = '1'

# 确保路径
sys.path.insert(0, r"D:\MotorDesign")

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def test_step1_imports():
    """步骤1: 验证导入"""
    log("=" * 50)
    log("步骤1: 验证包导入")
    try:
        from motorcad_auto import MotorCADConfig, MotorCADConnector, MotorCADRunner
        from motorcad_auto.params import ParameterManager
        from motorcad_auto.analysis import AnalysisRunner, AnalysisResult
        log("  [OK] 所有模块导入成功")
        return True
    except Exception as e:
        log(f"  [FAIL] 导入失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_step2_config():
    """步骤2: 验证配置"""
    log("\n步骤2: 验证配置")
    try:
        from motorcad_auto import MotorCADConfig
        config = MotorCADConfig()
        # 默认关闭电磁分析 (BPM-Therm 模块不支持, 且可能耗时很长)
        config.run_emag = False
        log(f"  EXE路径: {config.exe_path}")
        log(f"  EXE存在: {os.path.exists(config.exe_path)}")
        mot = config.get_mot_file()
        log(f"  MOT文件: {mot}")
        log(f"  MOT存在: {os.path.exists(mot)}")
        log(f"  结果目录: {config.result_dir}")
        log(f"  电磁分析: {config.run_emag} (BPM-Therm 模块默认关闭)")
        log("  [OK] 配置验证通过")
        return config
    except Exception as e:
        log(f"  [FAIL] 配置错误: {e}")
        return None

def test_step3_window_check(config):
    """步骤3: 检查 Motor-CAD 窗口和 ActiveX 模式 (不启动新实例)"""
    log("\n步骤3: 检查 Motor-CAD 窗口 (不启动新实例)")
    try:
        import win32gui
        found = []
        def _enum(hwnd, _):
            try:
                cls = win32gui.GetClassName(hwnd)
                if cls == "TMainForm" and win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    found.append((hwnd, title))
            except:
                pass
            return True
        win32gui.EnumWindows(_enum, None)

        if found:
            log(f"  [OK] 找到 Motor-CAD 主窗口: {found[0]}")
            from motorcad_auto.connector import MotorCADConnector
            conn = MotorCADConnector(config)
            conn._main_hwnd = found[0][0]
            mode = conn.check_activex_mode()
            log(f"  ActiveX 模式: {mode}")
            if mode == "none":
                log("  [警告] 免注册模式, COM 不可用! 尝试修复...")
                conn.fix_activex_mode()
                time.sleep(2)
                mode2 = conn.check_activex_mode()
                log(f"  修复后模式: {mode2}")
            return True
        else:
            log("  [INFO] Motor-CAD 未运行 (无主窗口)")
            return False
    except Exception as e:
        log(f"  [FAIL] 窗口检查失败: {e}")
        return False

def test_step4_5_analysis(config):
    """步骤4+5: COM 连接 + 完整分析"""
    log("\n步骤4: COM 连接 (可能需要 60-90 秒)...")
    try:
        from motorcad_auto import MotorCADRunner
        runner = MotorCADRunner(config)
        t0 = time.time()
        ok = runner.connect()
        elapsed = time.time() - t0
        log(f"  连接耗时: {elapsed:.1f}s")
        if not ok:
            log("  [FAIL] COM 连接失败")
            return None

        log("  [OK] COM 连接成功!")

        # 步骤5: 分析
        log("\n步骤5: 热分析 (电磁分析已关闭)")
        params = {"DCBusVoltage": 350, "PeakCurrent": 100}
        t0 = time.time()
        result = runner.run_single(params, run_emag=False)
        elapsed = time.time() - t0
        log(f"  分析耗时: {elapsed:.1f}s")

        print(result.summary(), flush=True)

        # 保存 JSON
        json_path = os.path.join(config.result_dir, "test_result.json")
        os.makedirs(config.result_dir, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        log(f"\n  结果已保存: {json_path}")

        # 断开
        log("\n步骤6: 断开连接")
        runner.disconnect()
        log("  [OK] 已断开")

        return result
    except Exception as e:
        log(f"  [FAIL] 分析失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def main():
    log("Motor-CAD v11.1.5 自动化项目端到端测试")
    log(f"Python: {sys.executable}")
    log(f"版本: {sys.version}")

    if not test_step1_imports():
        return 1

    config = test_step2_config()
    if not config:
        return 1

    test_step3_window_check(config)

    # 步骤4+5 在线程中运行, 带超时保护
    result_holder = {'done': False, 'result': None}
    def _run():
        try:
            result_holder['result'] = test_step4_5_analysis(config)
        except Exception as e:
            log(f"  [FAIL] 线程异常: {e}")
        finally:
            result_holder['done'] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=300)  # 5 分钟超时

    if not result_holder['done']:
        log("\n*** 超时 (5分钟) ***")
        log("可能某个 COM 调用挂起, 请检查变量名白名单")
        return 1

    log("\n" + "=" * 50)
    log("测试完成")
    return 0

if __name__ == "__main__":
    sys.exit(main())
