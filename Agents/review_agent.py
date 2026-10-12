import logging
logger = logging.getLogger(__name__)

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
import time
import concurrent.futures as _cf
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=False)

sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Core")

from base_agent import BaseAgent, extract_ollama_usage
try:
    from workflow_config import get_agent_model, get_point_review_timeout
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
from ai_tools import check_desensitize, auto_desensitize
# v4.01.037 (P0-1): 结构化评审回灌 — 强制 LLM 追加 review_feedback JSON
try:
    from review_feedback_schema import FEEDBACK_OUTPUT_INSTRUCTION as _RF_INSTRUCTION
except Exception as _rf_err:  # noqa: BLE001
    _RF_INSTRUCTION = ""
    print(f"[review_agent] review_feedback_schema 未加载: {_rf_err}")
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

    # v4.00.011: LLM 评审硬超时(秒), 可在测试中覆写
    #   v4.00.011b (2026-08-19): 300→1000 — 实测 qwen3:4b 逐方案点评(10方案矩阵)正常推理
    #   即可超过 300s, 300s 会误杀仍在正常工作的长任务。
    #   v4.00.011c (2026-08-19): 1000→2000 — 用户观察模型仍正常跑, 2000s 作为挂死兜底线。
    #   v4.02.66: 2000→600 — Ollama 过载时单步实测可拖到 28min, 2000s 兜底过长致用户感知"卡死";
    #     收口 600s, 既容忍冷启+长推理突刺, 又能在卡死 ~10min 内熔断降级。
    # v4.06-P2: 该值已收口到 workflow_config.llm_runtime.point_review_timeout_s 统一治理
    #   (调用级 request_timeout vs 点评级 point_review_timeout_s 语义分层, 见 workflow_config.py)。
    #   运行态由 get_point_review_timeout() 读取 (环境变量 OLLAMA_REVIEW_TIMEOUT 优先),
    #   此处保留为静态兜底常量, 容器测试仍可直接覆写。
    LLM_REVIEW_TIMEOUT_S = 600

    def __init__(self):
        super().__init__(
            name="ReviewAgent",
            description="分层类比选型法: 选型路径叙述 + 物理核验 + 分层对比 + 专家推荐"
        )
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换
        self.llm = None
        # v4.02.06 H-3 二期 T4: 云端客户端改走统一工厂 (BaseAgent._get_cloud_client),
        # 不再硬编码 deepseek OpenAI 构建. cloud_model/cloud_available 由 BaseAgent 工厂辅助解析.
        # (原实现误置于 _get_llm 的 return 之后成为死代码, 导致 use_cloud=True 时
        #  self.cloud_available 未定义; 此处修正到 __init__, 行为恢复正确)
        self.cloud_model = self._resolve_cloud_model()
        self.cloud_available = self._cloud_usable()

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        try:
            return super()._get_llm(temperature=0.3)
        except Exception as e:
            logger.warning("[ReviewAgent] LLM 创建失败: %s", e)
            return None

    def run(self, results: list, specs: dict, use_cloud: bool = False,
            l0_calibration: dict = None, l0_l1_diff: dict = None, **kwargs) -> dict:
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

        top = results[:10]

        # ── Step 1: 物理分析 (不依赖 LLM) ──
        scenario_info = determine_scenario(specs)
        best_design = top[0] if top else None
        physics_analysis = self._build_physics_analysis(specs, scenario_info, top)

        # v4.06: 提取案例电机参数 (从 kwargs)
        case_motor = kwargs.get("case_motor")
        case_match = kwargs.get("case_match")

        # ── Step 2: 准备 LLM 对比文本 ──
        # v4.02.68 (系列三 Tier A): 传 L0↔L1 差异 + 高残差区域标定, 逐方案渲染
        #   「[L0预测η → L1实算η, Δ]」与「[RMxprt 故障]」诊断行。
        l0_calibration = l0_calibration or {}
        l0_l1_diff = l0_l1_diff or {}
        comparison_text = self._build_comparison_text(
            top, case_motor=case_motor,
            l0_calibration=l0_calibration, l0_l1_diff=l0_l1_diff)

        # 脱敏检查 (v4.00.011c: 内部评审报告 — 命中仅附加提示, 不替换原始对比数据)
        #   原逻辑命中即把整个 comparison_text 替换为警告, 会丢失全部方案对比数据;
        #   内部 AI 团队流程非客户交付, 对比数据价值 > 脱敏, 故保留原文 + 一行提示。
        is_safe, warnings = check_desensitize(comparison_text)
        desensitize_note = ""
        if not is_safe:
            _warn = warnings if isinstance(warnings, str) else "; ".join(warnings)
            desensitize_note = (
                f"⚠️ 提示: 对比数据含可能敏感信息({_warn}), "
                "仅供内部 AI 团队评审参考, 请勿外发客户。\n"
            )

        # ── Step 3: LLM 评审 ──
        # v4.00.011 (2026-08-19): 硬超时保护 — _llm_review 内 llm.invoke 无超时,
        #   Ollama worker 挂死时永久阻塞 → quality_gate 永远等不到 review_pro →
        #   整个 workflow 冻结。与收敛阵营 future.result(timeout=...) 同款降落伞,
        #   超时/异常时降级返回「物理分析 + 方案对比数据 + LLM 点评跳过」而非冻结。
        #   v4.00.011b: 超时 300→1000s, 避免误杀正常长推理 (10方案点评 >300s 属正常)。
        _ts = time.time()
        _ex = _cf.ThreadPoolExecutor(max_workers=1)
        _llm_text = ""
        _llm_ti = 0
        _llm_to = 0
        # v4.06-P2: 点评级超时收口到 workflow_config 统一治理 (调用级 vs 点评级语义分层)
        _to = get_point_review_timeout()
        try:
            _llm_text, _llm_ti, _llm_to = _ex.submit(
                self._llm_review, specs, comparison_text, physics_analysis, use_cloud,
                case_motor=case_motor, case_match=case_match,
                l0_calibration=l0_calibration, l0_l1_diff=l0_l1_diff).result(
                    timeout=_to)
        except Exception as _e:
            _dur = int((time.time() - _ts) * 1000)
            _reason = (f"超时(>{_to}s)" if isinstance(_e, _cf.TimeoutError)
                       else f"{type(_e).__name__}: {_e}")
            try:
                self._log("llm_review_timeout_guard", {"timeout_s": _to},
                          str(_reason), status="error", duration_ms=_dur)
            except Exception:
                pass
            _llm_text = (
                f"⚠️ LLM 点评失败/超时 ({_reason}), 本轮已跳过 LLM 逐方案点评。\n\n"
                f"{desensitize_note}"
                "以下为方案详细对比数据 (供人工评审参考):\n\n"
                f"{comparison_text}"
            )
            _llm_ti = _llm_to = 0
        finally:
            # 不等待挂死线程, 避免评审节点永久阻塞 (同收敛阵营 v4.06 修复)
            _ex.shutdown(wait=False, cancel_futures=True)

        # ── Step 4: 合并报告 ──
        full_review = physics_analysis + "\n\n---\n\n" + _llm_text

        # 提取推荐
        recommendation = ""
        for line in _llm_text.split("\n"):
            # v4.00.011: 跳过 markdown 标题行, 避免把 "## 推荐方案" 标题误当推荐内容
            if line.strip().startswith("#"):
                continue
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
            # v4.06-P1: 真实 token 计量 (逐方案点评 LLM), 供成本审计全链路真实化
            "tokens_in": _llm_ti,
            "tokens_out": _llm_to,
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

    def _build_comparison_text(self, top: list, case_motor: dict = None,
                               l0_calibration: dict = None,
                               l0_l1_diff: dict = None) -> str:
        """构建方案对比文本 (供 LLM 使用)
        v4.06: 增加 case_motor 参考数据注入
        v4.02.68 (系列三 Tier A): 逐方案追加 L0↔L1 差异行与 RMxprt 故障行 ——
          原对比文本只有 L1 实算字段, LLM 对 L0 预测"不可见", 无从评价预筛可靠性;
          故障方案也只剩 eff=0 表象, 丢失 error_detail 根因。
          限界: 每方案最多 1 行 Δ 摘要 + 1 行故障摘要 (C4 — 防 prompt/token 膨胀)。
        """
        l0_calibration = l0_calibration or {}
        l0_l1_diff = l0_l1_diff or {}
        lines = []
        # v4.06: 案例电机参考数据 (放在对比文本最前面, 让 LLM 优先感知)
        if case_motor and isinstance(case_motor, dict):
            lines.append("=== 案例电机参考数据 ===")
            lines.append(f"案例编号: {case_motor.get('case_code', '?')}")
            lines.append(f"电机型号: {case_motor.get('motor_type', '?')}")
            ref_lines = []
            if case_motor.get('efficiency'):
                ref_lines.append(f"效率{case_motor['efficiency']}%")
            if case_motor.get('power_factor'):
                ref_lines.append(f"功率因数{case_motor['power_factor']}")
            if case_motor.get('stator_od'):
                ref_lines.append(f"定子外径{case_motor['stator_od']}mm")
            if case_motor.get('stator_id'):
                ref_lines.append(f"定子内径{case_motor['stator_id']}mm")
            if case_motor.get('rotor_od'):
                ref_lines.append(f"转子外径{case_motor['rotor_od']}mm")
            if case_motor.get('core_length'):
                ref_lines.append(f"铁心长{case_motor['core_length']}mm")
            if case_motor.get('air_gap'):
                ref_lines.append(f"气隙{case_motor['air_gap']}mm")
            if case_motor.get('rated_current'):
                ref_lines.append(f"额定电流{case_motor['rated_current']}A")
            if ref_lines:
                lines.append("关键参数: " + ", ".join(ref_lines))
            lines.append("请将各方案与上述案例电机参数进行对标分析, 指出参数偏差和匹配度。")
            lines.append("")
        lines.append("=== 方案详细对比 ===")
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
            # ── v4.02.68 (系列三 Tier A): L0↔L1 差异诊断行 (每方案最多 1 行) ──
            _l0e = r.get("l0_eff")
            _de = r.get("delta_eff")
            if _l0e is not None or isinstance(_de, (int, float)):
                _d_txt = f"{_de:+.2f}pp" if isinstance(_de, (int, float)) else "n/a"
                _bias = f" ⚠{r['l0_bias_flag']}" if r.get("l0_bias_flag") else ""
                lines.append(
                    f"  [L0-L1] L0预测η={_l0e if _l0e is not None else '?'}% → "
                    f"L1实算η={r.get('efficiency', 0):.2f}%, Δ={_d_txt}{_bias}")
            elif r.get("l0_fault_excluded"):
                lines.append("  [L0-L1] 该方案 RMxprt 故障, 已排除出 L0 残差统计")
            # ── v4.02.68 (系列一): RMxprt 故障诊断行 (每方案最多 1 行) ──
            _et = str(r.get("error_detail") or r.get("error") or "").strip()
            if _et:
                _lic = " (License)" if r.get("licence_error") else ""
                lines.append(f"  [RMxprt 故障{_lic}] {_et[:160]}")
            elif r.get("solve_failed"):
                lines.append("  [RMxprt 故障] 求解失败 (无 error_detail), 性能量已置 0")
        # ── v4.02.68 (系列三 Tier A/B): 预筛可靠性与高残差区域提示 (限界, 全局 1 段) ──
        _hint = self._build_l0_hint_section(l0_l1_diff, l0_calibration)
        if _hint:
            lines.append("")
            lines.append(_hint)
        return "\n".join(lines)

    def _build_l0_hint_section(self, l0_l1_diff: dict, l0_calibration: dict) -> str:
        """构建 L0 预筛可靠性 + 高残差区域提示 (供 LLM 作为诊断轴, 而非改 L0 系数)。"""
        out = []
        diff = l0_l1_diff if isinstance(l0_l1_diff, dict) else {}
        if diff.get("enabled") and int(diff.get("joins") or 0) > 0:
            out.append("=== L0 预筛可靠性 (L0预测 vs L1实算) ===")
            out.append(
                f"JOIN={diff.get('joins')} 组, 平均|Δη|={diff.get('avg_abs_delta_pp')}pp, "
                f"L0显著偏差={diff.get('bias_count')} 组 (阈值{diff.get('threshold_pp')}pp)")
            if diff.get("fault_excluded"):
                out.append(f"注: {diff.get('fault_excluded')} 组故障方案已排除出残差统计。")
        cal = l0_calibration if isinstance(l0_calibration, dict) else {}
        _regions = cal.get("high_residual_regions") or []
        if cal.get("total_samples"):
            out.append("=== L0 离线标定 (跨轮残差累积, 飞轮) ===")
            out.append(f"累计残差样本={cal.get('total_samples')} 条, "
                       f"全局偏置={float(cal.get('global_bias_pp', 0) or 0):+.2f}pp "
                       f"(正=L0低估 / 负=L0高估)")
            if _regions:
                out.append("高残差设计区域 (L0 估算薄弱, 需谨慎):")
                for _rg in _regions[:5]:
                    out.append(f"  - {_rg}")
        return "\n".join(out)

    def _llm_review(self, specs: dict, comparison_text: str,
                    physics_analysis: str, use_cloud: bool,
                    case_motor: dict = None, case_match: list = None,
                    l0_calibration: dict = None, l0_l1_diff: dict = None):
        """调用 LLM 生成评审报告
        v4.06: 增加 case_motor/case_match 参数, 注入 LLM prompt
        v4.06-P1: 返回值由 str 改为 (content:str, tokens_in:int, tokens_out:int),
                  供成本审计全链路真实化
        """
        # v4.06: 构建案例电机对标指令
        case_instruction = ""
        if case_motor and isinstance(case_motor, dict):
            case_code = case_motor.get('case_code', '')
            case_instruction = (
                f"\n\n=== 案例电机对标指令 ===\n"
                f"当前设计方案参考了案例电机 {case_code} ({case_motor.get('motor_type', '')})。\n"
                f"请逐方案分析与案例电机关键参数的匹配度, 指出主要偏差。\n"
            )
            if case_match:
                match_summary = []
                for m in case_match[:5]:
                    mi = m.get('solution_index', 0)
                    ms = m.get('match_score', 0)
                    match_summary.append(f"方案{mi+1}: 匹配度{ms:.1f}%")
                case_instruction += "预计算匹配度: " + " | ".join(match_summary) + "\n"

        # v4.02.68 (系列三 Tier A/B): L0↔L1 差异 + RMxprt 故障 解读指令
        #   限界: 明确"每方案限 1 行" + 禁止 LLM 自行发明 L0 系数修正 (报告 3.2/6 结论:
        #   top-10 属幸存者偏差样本, 据 10 个偏差样本改 L0 系数统计无效)。
        _l0_diff_on = bool(isinstance(l0_l1_diff, dict) and l0_l1_diff.get("enabled"))
        _l0_cal_on = bool(isinstance(l0_calibration, dict)
                          and l0_calibration.get("total_samples"))
        l0_instruction = ""
        if _l0_diff_on or _l0_cal_on:
            l0_instruction = (
                "\n\n=== 补充诊断要求 (L0 预筛 / RMxprt 故障) ===\n"
                "1) 逐方案解读 [L0-L1] 行 (L0 预测 vs L1 实算的 Δη): 指出偏差方向与可能成因"
                "(如 L0 对高槽满率 / 大外径 / 低极数设计偏乐观)。\n"
                "2) 对出现 [RMxprt 故障] 的方案: 解释故障根因并给出改进方向 (几何/线规/网格), "
                "不得把它当作正常性能结果点评。\n"
                "3) 若给出『高残差设计区域』: 仅需标注其为 L0 估算薄弱区并提示复核, "
                "严禁自行提出 L0 公式或系数的修改方案。\n"
                "4) 上述 (1)(2) 每方案限 1 行, 保持简洁, 不展开长段落。\n")

        # v4.02.07 H-3 二期 T1: 经系统级网关路由 (云端优先, 失败自动降级本地, 对调用方透明)
        llm = self._get_llm_or_cloud(temperature=0.3) if (use_cloud and self.cloud_available) else None
        if llm is not None:
            response = llm.invoke([
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
- 案例对标: 与案例电机参数的偏差和匹配度评估
- 总结: 优缺点综述

## 方案2
...

## 推荐方案
推荐方案X，因为...

## 风险提示
- 列出关键风险点
""" + _RF_INSTRUCTION + l0_instruction},
                    {"role": "user", "content": f"""需求规格：{specs.get('motor_type', '异步电动机')}, 功率{specs.get('power_kw')}kW, 电压{specs.get('voltage_v')}V, 冷却{specs.get('cooling')}, 效率目标{specs.get('efficiency_target')}%

=== D²L 物理内核分析 ===
{physics_analysis}

=== 方案详细对比（含结构/电磁/损耗/热参数/物理核验）===
{comparison_text}{case_instruction}"""}
            ])
            # v4.06-P1: 捕获真实 token 用量 (云端 _CloudAIMessage / 本地 ChatOllama usage_metadata, 云/地通用)
            _ti, _to = extract_ollama_usage(response)
            return response.content, _ti, _to
        else:
            # 本地点评
            prompt = f"""你是电机设计评审专家。以下是基于规格"{specs.get('motor_type', '异步电动机')}/{specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}"生成的Top5方案，已包含D²L物理内核分析。

=== D²L 物理内核分析 ===
{physics_analysis}

=== 方案详细对比 ===
{comparison_text}{case_instruction}

请逐方案做详细技术点评，每个方案包含：
1. 结构特点（槽配合合理性、匝数是否适当、内外径比例、铁心长度、长径比）
2. 电磁性能（磁密是否在合理范围1.5~1.9T、电密是否过高>8A/mm²）
3. 热性能（温升是否可接受<100K、发热因数）
4. 损耗分析（铜耗/铁耗比例）
5. 物理核验（D²L匹配度、长径比是否在最优区间）
6. 案例对标（与案例电机参数的偏差和匹配度评估）
7. 优缺点总结

格式：
## 方案1
- 结构: ...
- 电磁: ...
- 热性能: ...
- 损耗: ...
- 物理核验: ...
- 案例对标: ...
- 总结: ...

## 方案2
...

## 推荐方案
推荐方案X，因为...

## 风险提示
- ...
""" + _RF_INSTRUCTION + l0_instruction

            # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
            llm = self._get_llm()
            if llm is None:
                return "LLM 不可用", 0, 0
            response = llm.invoke(prompt)
            # v4.06-P1: 捕获本地真实 token 用量 (ChatOllama usage_metadata)
            _ti, _to = extract_ollama_usage(response, prompt)
            return response.content, _ti, _to
