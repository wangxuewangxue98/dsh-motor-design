"""
魔鬼代言人 - 用刻薄的失败者口吻写"遗书", 挑战一厢情愿的假设
防止一厢情愿: 假设这个方案已经失败了, 倒推它为什么死

大脑: 本地 Qwen2.5-3B 角色扮演 (全本地, 零上云)
收敛阵营: 秩序护盾

哲学: 真正的危险不是你看不到的风险, 而是你不愿相信的风险。
      让 AI 假装这个方案已经死了, 用"事后诸葛亮"的口吻逼你看清盲点。
"""
import os
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=True)

from base_agent import BaseAgent

try:
    from langchain_ollama import ChatOllama
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False


class DevilsAdvocate(BaseAgent):
    """魔鬼代言人 - 写方案的"遗书""""

    def __init__(self):
        super().__init__(
            name="DevilsAdvocate",
            description="用刻薄失败者口吻写遗书, 挑战一厢情愿的假设"
        )
        self.llm = None
        if HAS_LANGCHAIN:
            model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
            base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            use_gpu = os.getenv("USE_GPU", "true").lower() == "true"
            self.llm = ChatOllama(
                model=model,
                temperature=0.8,
                base_url=base_url,
                **({"num_gpu": 0} if not use_gpu else {})
            )

    def run(self, results: list, specs: dict, top_n: int = 3,
            convergence_findings: dict = None, **kwargs) -> dict:
        """
        为 Top-N 方案各写一封"遗书"
        convergence_findings: 其他收敛 agent 的发现 (逆向/现实/沙盘), 供遗书引用

        返回: {"con_report": str, "postmortems": [...]}
        """
        top = results[:top_n]
        if not top:
            return {"con_report": "无方案可审判。", "postmortems": []}

        # 汇总其他收敛发现作为"罪证"
        evidence_lines = []
        if convergence_findings:
            if convergence_findings.get("reverse"):
                evidence_lines.append(f"逆向评审师发现: {convergence_findings['reverse'][:120]}")
            if convergence_findings.get("reality"):
                evidence_lines.append(f"现实检验师发现: {convergence_findings['reality'][:120]}")
            if convergence_findings.get("sandbox"):
                evidence_lines.append(f"沙盘推演师发现: {convergence_findings['sandbox'][:120]}")
        evidence = "\n".join(evidence_lines) if evidence_lines else "(其他收敛 agent 未提供罪证)"

        postmortems = []
        for i, sol in enumerate(top):
            pm = self._write_postmortem(sol, specs, i + 1, evidence)
            postmortems.append({"solution_index": i, "postmortem": pm})

        # 汇总报告
        if self.llm:
            sol_summary = "\n".join(
                f"方案{i+1}: 外径{sol.get('stator_od','?')}mm/{sol.get('poles','?')}极, "
                f"效率{sol.get('efficiency',0):.1f}%, 温升{sol.get('max_temp',0):.1f}K, "
                f"转矩{sol.get('torque',0):.1f}N·m"
                for i, sol in enumerate(top)
            )
            prompt = f"""你是魔鬼代言人。现在假设上面这些方案已经全部失败了, 客户退了单, 公司赔了钱。你要用刻薄、讽刺、失败者的口吻写一份"项目遗书", 倒推它们为什么死。

需求: {specs.get('power_kw','?')}kW / {specs.get('voltage_v','?')}V / {specs.get('cooling','?')} / 效率目标{specs.get('efficiency_target','?')}%

方案数据:
{sol_summary}

其他收敛智能体的"罪证":
{evidence}

请写遗书, 必须包含:
1. 死因诊断: 这个方案最可能是怎么死的? (热失控? 量产不出来? 客户嫌贵? 磁钢退磁?)
2. 被忽视的警告: 设计时哪个信号其实已经亮红灯, 但被"理论上没问题"盖过去了?
3. 事后诸葛亮: 如果重来一次, 第一个该改的是什么?

语气: 刻薄、不留情面、像在追悼会上揭短。但每句话都要指向真实的技术风险, 不是空骂。
用中文, 400 字以内。"""
            try:
                resp = self.llm.invoke(prompt)
                report = resp.content
            except Exception:
                report = self._template_report(top, specs, postmortems)
        else:
            report = self._template_report(top, specs, postmortems)

        return {
            "con_report": report,
            "postmortems": postmortems,
        }

    def _write_postmortem(self, sol, specs, idx, evidence) -> str:
        """单方案遗书 (模板降级)"""
        eff = sol.get("efficiency", 0)
        tmp = sol.get("max_temp", 0)
        tq = sol.get("torque", 0)
        lines = [
            f"=== 方案{idx} 遗书 ===",
            f"死因: 效率{eff:.1f}% / 温升{tmp:.1f}K / 转矩{tq:.1f}N·m",
        ]
        if tmp > 90:
            lines.append(f"  → 温升 {tmp:.1f}K 已逼近绝缘极限, 长期运行绕组老化加速, 最终热击穿。")
        if eff and specs.get("efficiency_target") and eff < specs["efficiency_target"]:
            lines.append(f"  → 效率 {eff:.1f}% 未达目标 {specs['efficiency_target']}%, 客户验收不过。")
        if tq and specs.get("power_kw") and tq < specs["power_kw"] * 9.55 / max(specs.get("speed_rpm", 3000), 1) * 0.95:
            lines.append(f"  → 转矩 {tq:.1f}N·m 不足以拖动额定负载, 堵转烧毁。")
        lines.append(f"罪证: {evidence[:100]}")
        return "\n".join(lines)

    def _template_report(self, top, specs, postmortems) -> str:
        lines = ["=== 魔鬼代言人 · 项目遗书 ===", ""]
        lines.append(f"假设: 上述 {len(top)} 个方案已全部失败, 客户退单, 公司赔钱。")
        lines.append("")
        for pm in postmortems:
            lines.append(pm["postmortem"])
            lines.append("")
        lines.append("事后诸葛亮: 如果重来, 第一个该做的是——把温升余量从'刚好够'改成'留 15%', 把'理论上可行'的技术换成'已量产'的。")
        return "\n".join(lines)
