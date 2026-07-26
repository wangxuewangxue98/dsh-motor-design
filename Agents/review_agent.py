"""
评审推荐师智能体 - 对比方案，给出专家级推荐与风险提示
大脑: 本地 Qwen3B（快速点评）+ 云端大模型（深度分析，按需）
"""
import json
import os
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=True)

from base_agent import BaseAgent
import sys
sys.path.insert(0, r"D:\MotorDesign\Scripts")
from ai_tools import check_desensitize, auto_desensitize


class ReviewAgent(BaseAgent):
    """评审推荐师 - 方案对比与推荐"""

    def __init__(self):
        super().__init__(
            name="ReviewAgent",
            description="对比方案，给出推荐与风险提示"
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

        # 云端客户端（按需）— v1.02.01: 严格排除占位符, 确保全本地零上云
        _ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        self.cloud_available = bool(_ds_key) and _ds_key != "sk-your-key-here"
        if self.cloud_available:
            from openai import OpenAI
            self.cloud_client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url=os.getenv("CLOUD_API_BASE", "https://api.deepseek.com")
            )

    def run(self, results: list, specs: dict, use_cloud: bool = False, **kwargs) -> dict:
        """
        评审方案
        返回: {"review": str, "top_solutions": [...], "recommendation": str}
        """
        if not results:
            return {"review": "无方案可评审", "top_solutions": [], "recommendation": ""}

        # 取 Top5
        top = results[:5]

        # 准备脱敏对比文本（含详细参数）
        lines = []
        for i, r in enumerate(top):
            line = f"方案{i+1}: 外径{r.get('stator_od', '?')}mm, 极数{r.get('poles', '?')}"
            if r.get('slots_stator'):
                line += f", 定子槽{r['slots_stator']}/转子槽{r.get('slots_rotor', '?')}"
            if r.get('turns_per_coil'):
                line += f", 匝数{r['turns_per_coil']}"
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
            lines.append(line)
        comparison_text = "\n".join(lines)

        # 脱敏检查
        is_safe, warnings = check_desensitize(comparison_text)
        if not is_safe:
            comparison_text = "[警告: 检测到可能的敏感信息，已过滤]\n" + "\n".join(warnings)

        if use_cloud and self.cloud_available:
            # 云端深度分析
            response = self.cloud_client.chat.completions.create(
                model=os.getenv("CLOUD_MODEL_NAME", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": "你是一位资深电机设计工程师。请根据以下脱敏后的方案性能数据（含结构参数、电磁参数、损耗和热参数），进行详细的对比分析。从磁密饱和度、电密合理性、损耗分布、热性能等角度评估每个方案，指出优缺点、风险并给出推荐。用中文回答。"},
                    {"role": "user", "content": f"需求规格：功率{specs.get('power_kw')}kW, 电压{specs.get('voltage_v')}V, 冷却{specs.get('cooling')}, 效率目标{specs.get('efficiency_target')}%\n\n方案详细对比（含结构/电磁/损耗/热参数）：\n{comparison_text}"}
                ],
                temperature=0.3
            )
            review = response.choices[0].message.content
        else:
            # 本地点评
            prompt = f"""你是电机设计评审专家。以下是基于规格"{specs.get('power_kw')}kW/{specs.get('voltage_v')}V/{specs.get('cooling')}"生成的Top5方案详细数据：

{comparison_text}

请逐方案做详细技术点评，每个方案包含：
1. 结构特点（槽配合合理性、匝数是否适当）
2. 电磁性能（磁密是否在合理范围1.5~1.9T、电密是否过高>8A/mm²）
3. 热性能（温升是否可接受<100K、发热因数）
4. 损耗分析（铜耗/铁耗比例）
5. 优缺点总结

格式：
## 方案1
- 结构: ...
- 电磁: ...
- 热性能: ...
- 损耗: ...
- 总结: ...

## 方案2
...

## 推荐方案
推荐方案X，因为..."""

            response = self.llm.invoke(prompt)
            review = response.content

        # 提取推荐
        recommendation = ""
        for line in review.split("\n"):
            if "推荐" in line and "方案" in line:
                recommendation = line.strip()
                break

        return {
            "review": review,
            "top_solutions": top,
            "recommendation": recommendation,
            "used_cloud": use_cloud and self.cloud_available,
            "desensitize_warnings": warnings if not is_safe else []
        }
