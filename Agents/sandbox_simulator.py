"""
沙盘推演师 - 蒙特卡洛 1000 次虚拟推演, 展示最惨失败情形
防止信念变固执: 用数据告诉你"如果错了会多惨"

大脑: 本地 Qwen2.5-3B 解读推演结果 (全本地, 零上云)
收敛阵营: 秩序护盾
"""
import logging
logger = logging.getLogger(__name__)

import os
import sys
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=False)

from base_agent import BaseAgent, extract_ollama_usage
sys.path.insert(0, r"D:\MotorDesign\Core")
try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
from monte_carlo import run_batch, FAILURE_MODES

try:
    from langchain_ollama import ChatOllama
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False


class SandboxSimulator(BaseAgent):
    """沙盘推演师 - 1000 次蒙特卡洛模拟"""

    def __init__(self):
        super().__init__(
            name="SandboxSimulator",
            description="蒙特卡洛 1000 次推演, 用最惨失败情形防止信念变固执"
        )
        self.llm = None
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        if not HAS_LANGCHAIN:
            return None
        try:
            return super()._get_llm(temperature=0.4)
        except Exception as e:
            logger.warning("[SandboxSimulator] LLM 创建失败: %s", e)
            return None

    def run(self, results: list, specs: dict, top_n: int = 5,
            n_trials: int = 1000, **kwargs) -> dict:
        """
        对 Top-N 方案做蒙特卡洛推演
        返回: {"con_report": str, "simulations": [...]}
        """
        top = results[:top_n]
        if not top:
            return {"con_report": "无方案可推演。", "simulations": []}

        # 批量推演 (按失败率升序返回)
        sims = run_batch(top, specs, n_trials=n_trials)

        # 构造解读上下文
        sim_text = "\n".join(
            f"原方案{sim['solution_index']+1}: 失败率{sim['failure_rate']}% ({sim['verdict']}), "
            f"效率P5/P50/P95={sim['percentiles']['efficiency']['p5']}/{sim['percentiles']['efficiency']['p50']}/{sim['percentiles']['efficiency']['p95']}%, "
            f"温升P95={sim['percentiles']['max_temp']['p95']}K, "
            f"主要失败模式={list(sim['failure_modes'].keys()) or '无'}"
            for sim in sims
        )

        # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
        llm = self._get_llm_or_cloud(temperature=0.4)
        _tok_in, _tok_out = 0, 0
        if llm:
            prompt = f"""你是沙盘推演师, 职责是用 1000 次蒙特卡洛推演的最惨情形, 防止设计师的信念变成固执。

需求: {specs.get('power_kw','?')}kW / {specs.get('voltage_v','?')}V / 效率目标{specs.get('efficiency_target','?')}%

对 Top{top_n} 方案各做 {n_trials} 次参数扰动 (±5%) 的推演结果:
{sim_text}

失败模式说明:
{chr(10).join(f'- {m}: {info["label"]} ({info["severity"]}) - {info["description"]}' for m, info in FAILURE_MODES.items())}

请解读:
1. 哪个方案在扰动下鲁棒性最差? 它的"最惨情形"会怎样 (温升/效率崩塌幅度)?
2. 如果坚持选高效率方案, 在实际制造公差下它会退化成什么样子?
3. 是否存在"纸面漂亮但扰动即崩"的方案?

用中文, 300 字以内, 直接给结论, 用数据说话。"""
            try:
                resp = llm.invoke(prompt)
                report = resp.content
                _tok_in, _tok_out = extract_ollama_usage(resp, prompt)
            except Exception:
                report = self._template_report(sims)
                _tok_in, _tok_out = 0, 0
        else:
            report = self._template_report(sims)

        return {
            "con_report": report,
            "simulations": sims,
            "n_trials": n_trials,
            "tokens_in": _tok_in,
            "tokens_out": _tok_out,
        }

    def _template_report(self, sims) -> str:
        lines = [f"蒙特卡洛 {sims[0].get('trials', 1000)} 次推演结果 (按失败率升序):", ""]
        for sim in sims:
            lines.append(
                f"方案{sim['solution_index']+1}: 失败率 {sim['failure_rate']}% [{sim['verdict']}]"
            )
            lines.append(
                f"  效率 P5/P50/P95 = {sim['percentiles']['efficiency']['p5']}/{sim['percentiles']['efficiency']['p50']}/{sim['percentiles']['efficiency']['p95']}%"
            )
            lines.append(
                f"  温升 P95 = {sim['percentiles']['max_temp']['p95']}K"
            )
            if sim["failure_modes"]:
                modes_str = ", ".join(f"{FAILURE_MODES[m]['label']}({c})" for m, c in sim["failure_modes"].items())
                lines.append(f"  失败模式: {modes_str}")
            lines.append("")
        worst = sims[-1]
        lines.append(f"最脆弱方案: 方案{worst['solution_index']+1}, 失败率 {worst['failure_rate']}%, {worst['recommendation']}")
        return "\n".join(lines)
