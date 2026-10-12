"""
现实检验师 - 评估新技术的量产可行性与技术成熟度
防止远见变好高骛远: 把"理论上可行"和"能量产"分开

大脑: 本地 Qwen2.5-3B (全本地, 零上云)
收敛阵营: 秩序护盾
"""
import logging
logger = logging.getLogger(__name__)

import os
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=False)

from base_agent import BaseAgent, extract_ollama_usage
try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

try:
    from langchain_ollama import ChatOllama
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False


# 技术成熟度曲线 (简化版 TRL 1-9)
TECH_MATURITY = {
    "hairpin_winding": {"trl": 7, "label": "扁线/Hairpin 绕组", "note": "量产可行, 工装投入大, 适合大批量"},
    "squirrel_cage": {"trl": 9, "label": "铸铝转子", "note": "极度成熟, 成本最低"},
    "ipm_vshape": {"trl": 8, "label": "V型 IPM 转子", "note": "成熟, 主流 EV 方案"},
    "ipm_multilayer": {"trl": 6, "label": "多层 IPM", "note": "量产有挑战, 磁钢装配复杂"},
    "srm": {"trl": 5, "label": "开关磁阻", "note": "控制复杂, 噪声大, 量产案例少"},
    "axial_flux": {"trl": 4, "label": "轴向磁通", "note": "样机阶段, 量产工艺不成熟"},
    "amorphous_core": {"trl": 3, "label": "非晶定子铁心", "note": "实验室阶段, 脆性加工难"},
    "superconducting": {"trl": 2, "label": "超导绕组", "note": "前沿研究, 离量产极远"},
}


def _detect_tech_features(solution: dict, specs: dict) -> list:
    """从方案特征推断使用的技术"""
    features = []
    poles = solution.get("poles")
    voltage = specs.get("voltage_v")
    power = specs.get("power_kw")
    speed = specs.get("speed_rpm")

    # 高速 + 低压 → 可能多层 IPM
    if speed and speed > 8000 and voltage and voltage <= 96:
        features.append("ipm_multilayer")
    # 中速主流 → V型 IPM
    elif poles and poles >= 8:
        features.append("ipm_vshape")
    # 大功率低压 → 可能扁线
    if power and power >= 50 and voltage and voltage <= 96:
        features.append("hairpin_winding")
    # 默认铸铝转子 (异步)
    if not features:
        features.append("squirrel_cage")

    return features


class RealityChecker(BaseAgent):
    """现实检验师 - 量产可行性与技术成熟度"""

    def __init__(self):
        super().__init__(
            name="RealityChecker",
            description="评估新技术的量产可行性, 防止远见变好高骛远"
        )
        self.llm = None
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        if not HAS_LANGCHAIN:
            return None
        try:
            return super()._get_llm(temperature=0.3)
        except Exception as e:
            logger.warning("[RealityChecker] LLM 创建失败: %s", e)
            return None
            return None

    def run(self, results: list, specs: dict, top_n: int = 5, **kwargs) -> dict:
        """
        对 Top-N 方案做量产可行性检验
        返回: {"con_report": str, "feasibility": [...]}
        """
        top = results[:top_n]
        if not top:
            return {"con_report": "无方案可检验。", "feasibility": []}

        feasibility = []
        for i, sol in enumerate(top):
            features = _detect_tech_features(sol, specs)
            assessments = []
            for f in features:
                info = TECH_MATURITY.get(f, {"trl": 5, "label": f, "note": "成熟度未知"})
                assessments.append({
                    "feature": f,
                    "label": info["label"],
                    "trl": info["trl"],
                    "note": info["note"],
                    "risk_level": "low" if info["trl"] >= 8 else ("medium" if info["trl"] >= 6 else "high"),
                })
            feasibility.append({
                "solution_index": i,
                "stator_od": sol.get("stator_od"),
                "poles": sol.get("poles"),
                "efficiency": sol.get("efficiency"),
                "tech_assessments": assessments,
                "min_trl": min(a["trl"] for a in assessments) if assessments else 9,
            })

        # 生成报告
        # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
        llm = self._get_llm_or_cloud(temperature=0.3)
        _tok_in, _tok_out = 0, 0
        if llm:
            feas_text = "\n".join(
                f"方案{i+1} (外径{f['stator_od']}mm/{f['poles']}极, 效率{f['efficiency']}%): "
                + "; ".join(f"{a['label']} TRL{a['trl']}({a['risk_level']}风险 - {a['note']})" for a in f["tech_assessments"])
                for i, f in enumerate(feasibility)
            )
            prompt = f"""你是现实检验师, 职责是把"理论上可行"和"能量产"分开, 防止远见变好高骛远。

需求: {specs.get('power_kw','?')}kW / {specs.get('voltage_v','?')}V / 转速{specs.get('speed_rpm','?')}rpm / 应用{specs.get('application','?')}

Top{top_n} 方案的技术成熟度评估 (TRL 1-9, 9为已量产):
{feas_text}

请评估:
1. 哪些方案采用了 TRL < 7 的技术? 这些技术在当前工装/供应链条件下能否真正量产?
2. 高效率方案是否依赖尚不成熟的工艺(如多层 IPM、扁线、非晶)? 如果是, 效率优势能否落地?
3. 给出"可量产"vs"需样机验证"vs"仅理论可行"的三档分类建议。

用中文, 300 字以内, 直接结论。"""
            try:
                resp = llm.invoke(prompt)
                report = resp.content
                _tok_in, _tok_out = extract_ollama_usage(resp, prompt)
            except Exception:
                report = self._template_report(feasibility)
                _tok_in, _tok_out = 0, 0
        else:
            report = self._template_report(feasibility)

        return {
            "con_report": report,
            "feasibility": feasibility,
            "tokens_in": _tok_in,
            "tokens_out": _tok_out,
        }

    def _template_report(self, feasibility) -> str:
        lines = ["量产可行性检验 (TRL 1-9, 9=已量产):", ""]
        for f in feasibility:
            lines.append(f"方案{f['solution_index']+1} (外径{f['stator_od']}mm/{f['poles']}极):")
            for a in f["tech_assessments"]:
                lines.append(f"  - {a['label']}: TRL{a['trl']} ({a['risk_level']}风险) - {a['note']}")
            lines.append("")
        risky = [f for f in feasibility if f["min_trl"] < 7]
        if risky:
            lines.append(f"警告: 方案 {[f['solution_index']+1 for f in risky]} 依赖 TRL<7 技术, 量产需样机验证。")
        else:
            lines.append("所有方案技术成熟度 >= 7, 量产风险可控。")
        return "\n".join(lines)
