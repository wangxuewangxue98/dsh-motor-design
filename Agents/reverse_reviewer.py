"""
逆向评审师 - 在被淘汰的方案中寻找"异类品味"
防止设计风格独断: 主流推荐可能集体忽视某个被低估的方案

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


class ReverseReviewer(BaseAgent):
    """逆向评审师 - 替被淘汰方案说话"""

    def __init__(self):
        super().__init__(
            name="ReverseReviewer",
            description="在被淘汰方案中寻找被低估的异类, 防止设计风格独断"
        )
        self.llm = None
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        if not HAS_LANGCHAIN:
            return None
        try:
            return super()._get_llm(temperature=0.6)
        except Exception as e:
            logger.warning("[ReverseReviewer] LLM 创建失败: %s", e)
            return None
            return None

    def run(self, results: list, specs: dict, top_n: int = 5, **kwargs) -> dict:
        """
        审视被淘汰的方案 (Top-N 之外)
        返回: {"con_report": str, "discarded_highlights": list}
        """
        if len(results) <= top_n:
            return {
                "con_report": "所有方案均在 Top-N 内, 无被淘汰方案可审视。",
                "discarded_highlights": []
            }

        discarded = results[top_n:]
        adopted = results[:top_n]

        # 找出被淘汰方案中的"异类": 某项指标比 Top-N 最佳还好的
        highlights = []
        if adopted:
            best_eff = max((r.get("efficiency") or 0) for r in adopted)
            best_torque = max((r.get("torque") or 0) for r in adopted)
            lowest_temp = min((r.get("max_temp") or 999) for r in adopted)

            for i, r in enumerate(discarded):
                eff = r.get("efficiency") or 0
                tq = r.get("torque") or 0
                tmp = r.get("max_temp") or 999
                reasons = []
                if eff >= best_eff * 0.99:
                    reasons.append(f"效率 {eff:.1f}% 接近 Top 组最佳")
                if tq >= best_torque * 0.98:
                    reasons.append(f"转矩 {tq:.1f} N·m 接近 Top 组最佳")
                if tmp <= lowest_temp * 1.02:
                    reasons.append(f"温升 {tmp:.1f}K 优于或持平 Top 组最低")
                if reasons:
                    highlights.append({
                        "index_in_discarded": i,
                        "stator_od": r.get("stator_od"),
                        "poles": r.get("poles"),
                        "efficiency": eff,
                        "max_temp": tmp,
                        "torque": tq,
                        "reasons": reasons,
                    })

        # 构造上下文
        adopted_summary = "; ".join(
            f"Top{i+1}: 外径{r.get('stator_od','?')}mm/{r.get('poles','?')}极 效率{r.get('efficiency',0):.1f}% 温升{r.get('max_temp',0):.1f}K"
            for i, r in enumerate(adopted)
        )
        discarded_summary = "\n".join(
            f"淘汰方案{i+1}: 外径{r.get('stator_od','?')}mm/{r.get('poles','?')}极 效率{r.get('efficiency',0):.1f}% 温升{r.get('max_temp',0):.1f}K 转矩{r.get('torque',0):.1f}N·m"
            for i, r in enumerate(discarded[:8])
        )

        # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
        llm = self._get_llm_or_cloud(temperature=0.6)
        _tok_in, _tok_out = 0, 0
        if llm and highlights:
            prompt = f"""你是逆向评审师, 职责是替被淘汰的方案说话, 防止设计风格独断。

需求规格: {specs.get('power_kw','?')}kW / {specs.get('voltage_v','?')}V / {specs.get('cooling','?')}

被采纳的 Top{top_n} 方案:
{adopted_summary}

被淘汰的方案:
{discarded_summary}

其中这几个被淘汰方案在某些单项指标上接近甚至优于 Top 组:
{chr(10).join(f'- 外径{h["stator_od"]}mm/{h["poles"]}极: ' + '; '.join(h['reasons']) for h in highlights[:5])}

请用批判性视角分析:
1. Top 组的推荐是否因为某种"路径依赖"(例如都偏向高效率而忽视温升/转矩)而集体淘汰了这些异类?
2. 被淘汰的异类是否藏着被忽视的价值(如更低的温升余量、更均衡的性能、更低的成本)?
3. 如果坚持只看 Top 组, 可能错过什么?

用中文, 300 字以内, 直接给出结论, 不要客套。"""
            try:
                resp = llm.invoke(prompt)
                report = resp.content
                _tok_in, _tok_out = extract_ollama_usage(resp, prompt)
            except Exception as e:
                report = self._template_report(adopted, discarded, highlights)
                _tok_in, _tok_out = 0, 0
        else:
            report = self._template_report(adopted, discarded, highlights)

        return {
            "con_report": report,
            "discarded_highlights": highlights,
            "discarded_count": len(discarded),
            "tokens_in": _tok_in,
            "tokens_out": _tok_out,
        }

    def _template_report(self, adopted, discarded, highlights) -> str:
        if not highlights:
            return f"已审视 {len(discarded)} 个被淘汰方案, 未发现单项指标优于 Top 组的异类。当前推荐路径无明显独断风险。"
        lines = [f"已审视 {len(discarded)} 个被淘汰方案, 发现 {len(highlights)} 个异类在某项指标上接近 Top 组:", ""]
        for h in highlights[:5]:
            lines.append(f"- 外径{h['stator_od']}mm/{h['poles']}极: " + "; ".join(h["reasons"]))
        lines.append("")
        lines.append("提示: 这些异类可能因路径依赖被集体淘汰。若 Top 组均偏向高效率, 可能忽视了温升余量或转矩均衡。建议复核淘汰理由是否充分。")
        return "\n".join(lines)
