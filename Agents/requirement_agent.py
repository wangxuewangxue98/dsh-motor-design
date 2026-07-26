"""
需求分析师智能体 - 将客户口语化需求转为结构化设计规格
大脑: 本地 Qwen2.5-3B
"""
import json
import os
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=True)

from base_agent import BaseAgent


class RequirementAgent(BaseAgent):
    """需求分析师 - 解析客户需求为结构化规格"""

    def __init__(self):
        super().__init__(
            name="RequirementAgent",
            description="将客户口语化需求转为结构化设计规格"
        )
        model = os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        use_gpu = os.getenv("USE_GPU", "true").lower() == "true"

        self.llm = ChatOllama(
            model=model,
            temperature=0.1,
            base_url=base_url,
            **({"num_gpu": 0} if not use_gpu else {})
        )

    def run(self, raw_text: str, **kwargs) -> dict:
        """
        解析客户需求文本
        返回: {"specs": {...}, "raw_text": str, "confidence": float}
        """
        prompt = f"""你是一个电机需求分析助手。请从以下客户描述中提取关键信息，只返回严格JSON，不要任何解释或markdown标记。

JSON格式：
{{
    "power_kw": 数字（功率kW）,
    "voltage_v": 数字（电压V）,
    "speed_rpm": 数字或null（转速rpm）,
    "cooling": "自然风冷/水冷/油冷/未知",
    "efficiency_target": 数字或null（效率目标%）,
    "stator_od_limit": 数字或null（外径限制mm）,
    "application": "应用场景（如叉车/无人机/电动汽车/工业泵/家电/通用）"
}}

注意：
- 包含"左右""大约""不低于""以上"等词的数字也要提取
- 如果信息缺失，对应字段填null
- application 字段根据描述推断

客户描述：{raw_text}
JSON:"""

        response = self.llm.invoke(prompt)
        content = response.content.strip()

        # 清理可能的 markdown 包裹
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])

        try:
            specs = json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取 JSON 部分
            import re
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                specs = json.loads(match.group())
            else:
                raise ValueError(f"无法解析为JSON: {content[:200]}")

        # 补全默认值
        specs.setdefault("speed_rpm", 3000)
        specs.setdefault("cooling", "未知")
        specs.setdefault("application", "通用")

        # 置信度评估
        required = ["power_kw", "voltage_v"]
        filled = sum(1 for k in required if specs.get(k) is not None)
        confidence = filled / len(required)

        return {
            "specs": specs,
            "raw_text": raw_text,
            "confidence": confidence
        }
