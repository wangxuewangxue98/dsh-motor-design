"""
workflow.py - 主工作流脚本
串联：需求解析 → 参数矩阵 → 批量求解 → 结果排序 → AI 分析 → 报告生成

使用方法：
  python workflow.py              # 交互式
  python workflow.py --test       # 测试模式（2组参数，模拟数据）
  python workflow.py --demo       # 演示模式（内置示例需求）

日常使用：每天运行一次，20-40 分钟出结果
"""
import json
import sys
import os
import time
from datetime import datetime

# 确保能找到同目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from motor_tools import generate_param_matrix, batch_solve
from ai_tools import (
    local_parse_requirements, cloud_parse_requirements,
    local_review_solutions, local_draft_report,
    cloud_deep_analysis, cloud_polish_report,
    check_desensitize, cloud_client
)
from db_tools import init_db, save_result, get_top_solutions, export_csv, get_stats


# ============================================================
# 测试用例
# ============================================================
TEST_CASES = [
    "我们要一台电动叉车驱动电机，额定功率10kW，48V电池供电，水冷，希望效率不低于93%，安装空间外径限制250mm。",
    "需要一个伺服电机，功率2.2kW，220V，转速3000rpm左右，自然风冷就行。",
    "做一款无人机电机，功率500W，电压48V，转速10000rpm以上，要求重量轻，效率90%以上。",
    "矿用隔爆电机，55kW，660V，水冷，效率要达到IE4标准（96%），转速1480rpm。",
    "家电用无刷直流电机，120W，24V，转速2000rpm，自然风冷，效率85%以上。",
]


def parse_requirements(raw_text: str) -> dict:
    """
    需求解析：本地优先，失败时降级云端
    """
    print("\n[1] 解析客户需求（本地 AI）...")
    print(f"    输入: {raw_text[:80]}...")

    try:
        specs = local_parse_requirements(raw_text)
        print(f"    本地解析成功: {json.dumps(specs, ensure_ascii=False)}")

        # 检查关键字段是否完整
        if specs.get("voltage_v") is None:
            print("    [警告] 电压未提取到，尝试云端补充...")
            if cloud_client:
                specs = cloud_parse_requirements(raw_text)
                print(f"    云端解析结果: {json.dumps(specs, ensure_ascii=False)}")
            else:
                print("    [提示] 云端未配置，使用默认值")
                specs["voltage_v"] = 48
        return specs

    except Exception as e:
        print(f"    本地解析失败: {e}")
        if cloud_client:
            print("    降级到云端解析...")
            try:
                specs = cloud_parse_requirements(raw_text)
                print(f"    云端解析成功: {json.dumps(specs, ensure_ascii=False)}")
                return specs
            except Exception as e2:
                print(f"    云端也失败: {e2}")
        print("    [提示] 使用默认规格")
        return {
            "power_kw": None, "voltage_v": 48, "speed_rpm": 3000,
            "cooling": "未知", "efficiency_target": None, "stator_od_limit": 250
        }


def solve_and_store(specs: dict, test_mode: bool = False) -> list:
    """
    生成参数矩阵 → 批量求解 → 存入数据库
    """
    print("\n[2] 生成参数扫描矩阵...")

    if test_mode:
        param_list = generate_param_matrix(specs, count=2)
    else:
        param_list = generate_param_matrix(specs, count=30)

    print(f"    共 {len(param_list)} 组参数")

    print("\n[3] 批量求解...")
    results = batch_solve(param_list)

    print(f"\n    求解完成: {len(results)}/{len(param_list)} 组成功")

    # 存入数据库
    print("\n[4] 存入数据库...")
    for r in results:
        params = r["params"].copy()
        params["power_kw"] = specs.get("power_kw")
        save_result(params, r)

    stats = get_stats()
    print(f"    数据库总计: {stats['total']} 组方案")
    print(f"    最高效率: {stats.get('max_efficiency', 'N/A')}%")

    return results


def analyze_and_report(raw_text: str, specs: dict, results: list) -> str:
    """
    筛选 → 本地点评 → 云端深度分析 → 生成报告
    """
    eff_target = specs.get("efficiency_target") or 0

    # 筛选达标方案
    print("\n[5] 筛选达标方案...")
    qualified = [r for r in results if r["efficiency"] >= eff_target]
    if not qualified:
        print(f"    无方案达到效率目标 {eff_target}%，取全部方案中效率最高的 5 个")
        qualified = sorted(results, key=lambda x: x["efficiency"], reverse=True)

    qualified.sort(key=lambda x: x["efficiency"], reverse=True)
    top5 = qualified[:5]

    print(f"    达标方案: {len(qualified)} 个，取 Top {len(top5)}")

    # 本地 AI 一句话点评
    print("\n[6] 本地 AI 方案点评...")
    try:
        review = local_review_solutions(top5)
        print(review)
    except Exception as e:
        print(f"    本地点评失败: {e}")
        review = "（本地点评不可用）"

    # 准备脱敏对比文本
    lines = []
    for i, d in enumerate(top5):
        p = d["params"]
        lines.append(
            f"方案{i+1}: 外径{p['stator_od']}mm, 极数{p['poles']}, "
            f"效率{d['efficiency']:.2f}%, 温升{d['max_temp']:.0f}K, "
            f"转矩{d['torque']:.1f}N·m"
        )
    comparison_text = "\n".join(lines)

    # 脱敏自检
    print("\n[7] 脱敏自检...")
    is_safe, warning = check_desensitize(comparison_text)
    if is_safe:
        print(f"    ✅ {warning}，可以发送云端")
    else:
        print(f"    ⚠️ {warning}，需要脱敏处理")

    # 云端深度分析
    print("\n[8] 云端深度分析...")
    analysis = cloud_deep_analysis(comparison_text)
    if analysis.startswith("[云端 AI 未配置]"):
        print("    " + analysis)
    else:
        print(f"    云端分析完成 ({len(analysis)} 字)")

    # 生成报告
    print("\n[9] 生成报告...")
    try:
        draft = local_draft_report(specs, top5, review)
        print("    本地草稿完成")
    except Exception as e:
        print(f"    本地草稿失败: {e}")
        draft = f"需求: {raw_text}\n\n推荐方案:\n{comparison_text}\n\n分析:\n{analysis}"

    # 如果云端可用，润色报告
    if cloud_client and not analysis.startswith("[云端 AI 未配置]"):
        print("    云端润色中...")
        try:
            final_report = cloud_polish_report(draft, comparison_text)
            print("    云端润色完成")
        except Exception as e:
            print(f"    云端润色失败: {e}，使用本地草稿")
            final_report = draft
    else:
        final_report = draft

    # 保存报告
    report_path = os.path.join(
        r"D:\MotorDesign\Reports",
        f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 电机设计方案报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"## 原始需求\n\n{raw_text}\n\n")
        f.write(f"## 提取规格\n\n```json\n{json.dumps(specs, ensure_ascii=False, indent=2)}\n```\n\n")
        f.write(f"## 方案对比\n\n{comparison_text}\n\n")
        f.write(f"## 方案点评\n\n{review}\n\n")
        f.write(f"## 深度分析\n\n{analysis}\n\n")
        f.write(f"## 报告正文\n\n{final_report}\n")

    print(f"\n    报告已保存: {report_path}")

    # 导出 CSV
    csv_path = export_csv()
    if csv_path:
        print(f"    CSV 已导出: {csv_path}")

    return report_path


# ============================================================
# 主流程
# ============================================================
def run(raw_text: str = None, test_mode: bool = False):
    """运行完整工作流"""
    print("=" * 60)
    print("  Motor-AI 设计辅助系统 (纯 Windows 版)")
    print("=" * 60)
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  模式: {'测试 (2组参数)' if test_mode else '正常 (30组参数)'}")
    print("=" * 60)

    init_db()

    # 1. 需求输入
    if raw_text is None:
        print("\n请粘贴客户需求描述（输入完后按两次回车）:")
        lines = []
        empty_count = 0
        while True:
            line = input()
            if line == "":
                empty_count += 1
                if empty_count >= 2:
                    break
            else:
                empty_count = 0
                lines.append(line)
        raw_text = "\n".join(lines)

    # 2. 需求解析
    specs = parse_requirements(raw_text)

    # 3. 求解
    results = solve_and_store(specs, test_mode=test_mode)

    # 4. 分析与报告
    report_path = analyze_and_report(raw_text, specs, results)

    print("\n" + "=" * 60)
    print("  全流程完成！")
    print("=" * 60)
    print(f"  报告: {report_path}")
    print(f"  数据库: D:\\MotorDesign\\KnowledgeBase\\designs.db")
    print("=" * 60)

    return report_path


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--test" in args:
        # 测试模式：2组参数，模拟数据
        run(raw_text=TEST_CASES[0], test_mode=True)
    elif "--demo" in args:
        # 演示模式：依次跑5个测试用例
        for case in TEST_CASES:
            run(raw_text=case, test_mode=True)
    else:
        # 交互式
        run()
