# -*- coding: utf-8 -*-
"""
评审推荐师智能体 v2.04 — 物理逻辑解释 + 分层对比
==================================================
大脑: 本地 Qwen2.5-3B（快速点评）+ 云端 DeepSeek（深度分析，按需）

改造点:
  1. 选型路径叙述: 需求→转矩→场景→型谱/类比→聚焦扫描→最优方案
  2. 物理核验: D²L/lambda/torque 匹配度验证
  3. 分层对比表: L1粗选核验 + L2细调影响 + L3微调配置
  4. 机座号变动分析: 解释为何选这个机座号
"""
import json
import os
import sys
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=True)

sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Core")

from base_agent import BaseAgent
from ai_tools import check_desensitize, auto_desensitize
from physics_kernel import (
    determine_scenario, find_closest_catalog, analogy_estimate,
    compute_d2l, compute_torque, compute_lambda, validate_lambda,
    build_selection_narrative, shift_frame,
)

# v2.05.02: 加载飞轮报告结构优化规则
_REPORT_RULES_PATH = r"D:\MotorDesign\Knowledge\report_structure_rules.json"


def _load_report_structure_rules() -> dict:
    """加载飞轮生成的报告结构优化规则"""
    try:
        import json
        if os.path.exists(_REPORT_RULES_PATH):
            with open(_REPORT_RULES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


class ReviewAgent(BaseAgent):
    """评审推荐师 - 物理逻辑解释 + 分层对比 + 专家推荐"""

    def __init__(self):
        super().__init__(
            name="ReviewAgent",
            description="分层类比选型法: 选型路径叙述 + 物理核验 + 分层对比 + 专家推荐"
        )
        model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        use_gpu = os.getenv("USE_GPU", "true").lower() == "true"

        self.llm = ChatOllama(
            model=model,
            temperature=0.3,
            base_url=base_url,
            **({"num_gpu": 0} if not use_gpu else {})
        )

        # 云端客户端（按需）
        _ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        _allow_cloud = os.getenv("ALLOW_CLOUD_API", "true") == "true"
        self.cloud_available = bool(_ds_key) and _ds_key != "sk-your-key-here" and _allow_cloud
        if self.cloud_available:
            from openai import OpenAI
            self.cloud_client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url=os.getenv("CLOUD_API_BASE", "https://api.deepseek.com")
            )

    def run(self, results: list, specs: dict, use_cloud: bool = False, **kwargs) -> dict:
        """
        评审方案 (物理逻辑解释 + 分层对比)

        返回: {
            "review": str,              # 完整评审报告 (物理分析 + LLM点评)
            "physics_analysis": str,    # 物理分析部分 (独立提取)
            "top_solutions": list,      # Top5 方案
            "recommendation": str,      # 推荐摘要
            "used_cloud": bool,
            "desensitize_warnings": list,
        }
        """
        if not results:
            return {"review": "无方案可评审", "top_solutions": [], "recommendation": ""}

        top = results[:5]

        # ── Step 1: 物理分析 (不依赖 LLM) ──
        scenario_info = determine_scenario(specs)
        best_design = top[0] if top else None
        physics_analysis = self._build_physics_analysis(specs, scenario_info, top)

        # ── Step 2: 准备 LLM 对比文本 ──
        comparison_text = self._build_comparison_text(top)

        # 脱敏检查
        is_safe, warnings = check_desensitize(comparison_text)
        if not is_safe:
            comparison_text = "[警告: 检测到可能的敏感信息，已过滤]\n" + "\n".join(warnings)

        # ── Step 3: LLM 评审 ──
        llm_review = self._llm_review(specs, comparison_text, physics_analysis, use_cloud)

        # ── Step 4: 合并报告 ──
        full_review = physics_analysis + "\n\n---\n\n" + llm_review

        # 提取推荐
        recommendation = ""
        for line in llm_review.split("\n"):
            if "推荐" in line and "方案" in line:
                recommendation = line.strip()
                break

        return {
            "review": full_review,
            "physics_analysis": physics_analysis,
            "top_solutions": top,
            "recommendation": recommendation,
            "used_cloud": use_cloud and self.cloud_available,
            "desensitize_warnings": warnings if not is_safe else [],
        }

    def _build_physics_analysis(self, specs: dict, scenario_info: dict,
                                top: list) -> str:
        """构建物理分析报告 (选型路径 + 核验 + 分层对比)"""
        # v2.05.02: 加载飞轮报告结构规则
        report_rules = _load_report_structure_rules()
        key_first = report_rules.get("key_metrics_first", {}).get("enabled", False)
        reduce_text = report_rules.get("reduce_text", {}).get("enabled", False)
        max_chars = report_rules.get("reduce_text", {}).get("max_paragraph_chars", 999)

        parts = []

        # v2.05.02: 如果启用了关键指标前置, 先输出关键参数对比
        if key_first and top:
            priority_order = report_rules.get("key_metrics_first", {}).get(
                "priority_order", ["efficiency", "max_temp", "torque"])
            parts.append("## 关键指标速览 (飞轮优化: 前置)\n")
            header = "| 方案 | " + " | ".join(priority_order) + " |\n"
            sep = "|------|" + "|".join(["------" for _ in priority_order]) + "|\n"
            parts.append(header + sep)
            for i, d in enumerate(top):
                vals = []
                for key in priority_order:
                    v = d.get(key, "?")
                    if isinstance(v, float):
                        vals.append(f"{v:.1f}")
                    else:
                        vals.append(str(v))
                parts.append(f"| {i+1} | " + " | ".join(vals) + " |\n")
            parts.append("\n")

        # ── 1. 选型路径叙述 ──
        best = top[0] if top else None
        narrative = build_selection_narrative(specs, scenario_info, best)
        parts.append(narrative)

        # ── 2. 物理核验表 ──
        parts.append("\n## 物理核验表\n")
        parts.append("| 方案 | D(mm) | L(mm) | D²L(m³) | λ | λ校验 | 估算转矩(Nm) | 参考型号 |\n")
        parts.append("|------|-------|-------|---------|---|-------|-------------|----------|\n")
        for i, d in enumerate(top):
            phys = d.get("_physics", {})
            d_val = d.get("stator_id", "?")
            l_val = d.get("core_length", "?")
            d2l = phys.get("d_squared_l", compute_d2l(d_val, l_val) if isinstance(d_val, (int, float)) else "?")
            lam = phys.get("lambda", compute_lambda(l_val, d_val) if isinstance(d_val, (int, float)) else "?")
            lam_ok = "✓" if phys.get("lambda_valid", True) else "✗"
            est_t = phys.get("estimated_torque", "?")
            ref = phys.get("ref_model", "?")[:12]
            parts.append(f"| {i+1} | {d_val} | {l_val} | {d2l} | {lam} | {lam_ok} | {est_t} | {ref} |\n")

        # ── 3. 分层对比 ──
        parts.append("\n## 分层对比分析\n")

        # L1 粗选核验
        parts.append("### L1 粗选核验\n")
        target_t = scenario_info.get("corrected_torque_nm", "?")
        parts.append(f"- 目标转矩: {target_t}Nm\n")
        ref_entry = find_closest_catalog(target_t, poles=scenario_info.get("poles"))
        if ref_entry:
            parts.append(f"- Y系列基准: {ref_entry.get('model', '?')} "
                         f"(T={ref_entry.get('torque_nm', '?')}Nm, "
                         f"D={ref_entry.get('stator_inner_dia_mm', '?')}mm, "
                         f"L={ref_entry.get('core_length_mm', '?')}mm)\n")
            kv = target_t / ref_entry["torque_nm"] if ref_entry["torque_nm"] else 1.0
            parts.append(f"- 转矩比 K_V: {round(kv, 4)}\n")
            shift = shift_frame(abs(kv - 1.0))
            parts.append(f"- 机座号变动: {shift['description']}\n")

        # L2 细调影响
        parts.append("\n### L2 细调影响\n")
        cond = scenario_info.get("condition", {})
        if cond.get("correction_factor", 1.0) != 1.0:
            parts.append(f"- 修正系数: {cond['correction_factor']}\n")
            for k, v in cond.get("factors", {}).items():
                if k != "_note":
                    parts.append(f"  - {k}: {v}\n")
            parts.append(f"- 修正后转矩: {target_t}Nm\n")
        else:
            parts.append("- 工况为标准条件, 无需修正\n")

        # L3 微调配置
        parts.append("\n### L3 微调配置\n")
        l3_fields = ["mounting", "insulation_class", "shaft_config", "bearing_type", "application"]
        l3_items = []
        for f in l3_fields:
            val = specs.get(f)
            if val and val != "未知":
                l3_items.append(f"{f}={val}")
        if l3_items:
            parts.append(f"- {', '.join(l3_items)}\n")
        else:
            parts.append("- 使用默认配置\n")

        # ── 4. 机座号变动分析 ──
        if ref_entry and best:
            parts.append("\n## 机座号变动分析\n")
            best_phys = best.get("_physics", {})
            ref_frame = ref_entry.get("frame_size", "?")
            best_d = best.get("stator_id", 0)
            ref_d = ref_entry.get("stator_inner_dia_mm", 0)
            if best_d and ref_d:
                d_change = (best_d - ref_d) / ref_d * 100 if ref_d else 0
                parts.append(f"- 基准机座号: {ref_frame} (D={ref_d}mm)\n")
                parts.append(f"- 实际内径: {best_d}mm (偏差 {d_change:+.1f}%)\n")
                if abs(d_change) < 5:
                    parts.append(f"- 判定: 同机座号微调, 尺寸偏差在5%以内\n")
                elif abs(d_change) < 20:
                    parts.append(f"- 判定: 机座号浮动1~2档, 属于细调范围\n")
                else:
                    parts.append(f"- 判定: 机座号跨档选型, 需关注系列适配性\n")

        return "".join(parts)

    def _build_comparison_text(self, top: list) -> str:
        """构建方案对比文本 (供 LLM 使用)"""
        lines = []
        for i, r in enumerate(top):
            line = f"方案{i+1}: 外径{r.get('stator_od', '?')}mm, 极数{r.get('poles', '?')}"
            if r.get('slots_stator'):
                line += f", 定子槽{r['slots_stator']}/转子槽{r.get('slots_rotor', '?')}"
            if r.get('turns_per_coil'):
                line += f", 匝数{r['turns_per_coil']}"
            if r.get('stator_id'):
                line += f", 内径{r['stator_id']}mm, 铁心长{r.get('core_length', '?')}mm"
            if r.get('air_gap'):
                line += f", 气隙{r['air_gap']}mm"
            line += f", 效率{r.get('efficiency', 0):.2f}%, 温升{r.get('max_temp', 0):.1f}K"
            line += f", 转矩{r.get('torque', 0):.1f}N·m"
            if r.get('stator_tooth_flux_density'):
                line += f", 齿磁密{r['stator_tooth_flux_density']}T, 气隙磁密{r.get('air_gap_flux_density', '?')}T"
            if r.get('current_density'):
                line += f", 电密{r['current_density']}A/mm²"
            if r.get('copper_loss'):
                line += f", 铜耗{r['copper_loss']}W, 铁耗{r.get('iron_loss', '?')}W"
            if r.get('thermal_factor'):
                line += f", 发热因数{r['thermal_factor']}"
            # 物理内核附加信息
            phys = r.get("_physics", {})
            if phys:
                line += f" [D²L={phys.get('d_squared_l', '?')}, λ={phys.get('lambda', '?')}, 参考={phys.get('ref_model', '?')[:15]}]"
            lines.append(line)
        return "\n".join(lines)

    def _llm_review(self, specs: dict, comparison_text: str,
                    physics_analysis: str, use_cloud: bool) -> str:
        """调用 LLM 生成评审报告"""

        if use_cloud and self.cloud_available:
            response = self.cloud_client.chat.completions.create(
                model=os.getenv("CLOUD_MODEL_NAME", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": """你是一位资深电机设计评审专家。你将收到一批经过脱敏处理的电机方案性能数据（含结构参数、电磁参数、损耗和热参数），以及基于D²L物理内核的选型路径分析。

重要声明：
1. 这些数据来自真实的电磁仿真计算，性能指标（效率、温升、转矩、磁密等）是真实可信的。
2. 出于数据安全考虑，只向你展示了性能结果，隐去了具体的材料牌号和客户信息。
3. 你的任务是：基于这些真实的性能指标和物理分析，对各方案进行专业对比分析，指出优缺点、风险并给出推荐。
4. 报告中直接陈述分析结论，严禁使用"虚构"、"假设"、"模拟数据"等字样。
5. 报告语气：专业、客观，如同你审阅的是本公司内部的设计方案。

输出格式要求（逐方案点评）：
## 方案1
- 结构: 槽配合合理性、匝数是否适当、内外径比例、长径比评估
- 电磁: 磁密是否在合理范围(齿磁密1.5~1.9T)、电密是否过高(>8A/mm²)
- 热性能: 温升是否可接受(<100K)、发热因数评估
- 损耗: 铜耗/铁耗比例分析
- 物理核验: D²L匹配度、长径比合理性
- 总结: 优缺点综述

## 方案2
...

## 推荐方案
推荐方案X，因为...

## 风险提示
- 列出关键风险点"""},
                    {"role": "user", "content": f"""需求规格：{specs.get('motor_type', '异步电动机')}, 功率{specs.get('power_kw')}kW, 电压{specs.get('voltage_v')}V, 冷却{specs.get('cooling')}, 效率目标{specs.get('efficiency_target')}%

=== D²L 物理内核分析 ===
{physics_analysis}

=== 方案详细对比（含结构/电磁/损耗/热参数/物理核验）===
{comparison_text}"""}
                ],
                temperature=0.3
            )
            return response.choices[0].message.content
        else:
            # 本地点评
            prompt = f"""你是电机设计评审专家。以下是基于规格"{specs.get('motor_type', '异步电动机')}/{specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}"生成的Top5方案，已包含D²L物理内核分析。

=== D²L 物理内核分析 ===
{physics_analysis}

=== 方案详细对比 ===
{comparison_text}

请逐方案做详细技术点评，每个方案包含：
1. 结构特点（槽配合合理性、匝数是否适当、内外径比例、铁心长度、长径比）
2. 电磁性能（磁密是否在合理范围1.5~1.9T、电密是否过高>8A/mm²）
3. 热性能（温升是否可接受<100K、发热因数）
4. 损耗分析（铜耗/铁耗比例）
5. 物理核验（D²L匹配度、长径比是否在最优区间）
6. 优缺点总结

格式：
## 方案1
- 结构: ...
- 电磁: ...
- 热性能: ...
- 损耗: ...
- 物理核验: ...
- 总结: ...

## 方案2
...

## 推荐方案
推荐方案X，因为...

## 风险提示
- ..."""

            response = self.llm.invoke(prompt)
            return response.content
