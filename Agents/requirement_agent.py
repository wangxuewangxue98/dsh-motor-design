# -*- coding: utf-8 -*-
"""
需求分析师智能体 v2.04 — 分层类比选型法
==========================================
大脑: 本地 Qwen2.5-3B

分层提取:
  L1 粗选层: power_kw, voltage_v, speed_rpm, torque_nm, poles, cooling, motor_type
  L2 细调层: duty_cycle, overload_pct, altitude_m, protection_class, ambient_temp_c, frequency_hz
  L3 微调层: mounting, insulation_class, shaft_config, bearing_type, application

场景识别:
  - 型谱模式 (catalog): 标准工况, 可直接查 Y 系列型谱
  - 类比模式 (analogy): 非标工况, 需 D²L 推算
"""
import json
import os
import sys
import re
from typing import Optional
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=True)

sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Core")

from base_agent import BaseAgent
from physics_kernel import compute_torque, determine_scenario


class RequirementAgent(BaseAgent):
    """需求分析师 - 分层提取 + 场景识别"""

    def __init__(self):
        super().__init__(
            name="RequirementAgent",
            description="分层类比选型法: L1粗选+L2细调+L3微调, 场景识别(型谱/类比)"
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
        解析客户需求文本 (三层提取 + 场景识别)

        返回: {
            "specs": {...},           # 合并后的完整规格 (L1+L2+L3)
            "layered_specs": {        # 分层规格
                "L1": {...}, "L2": {...}, "L3": {...}
            },
            "scenario_info": {...},   # 场景识别结果
            "raw_text": str,
            "confidence": float,
            "extraction_notes": list  # 提取过程中的补全/推断记录
        }
        """
        # ── Step 1: LLM 三层提取 ──
        prompt = self._build_extraction_prompt(raw_text)
        response = self.llm.invoke(prompt)
        content = response.content.strip()

        # 清理 markdown 包裹
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])

        try:
            extracted = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                extracted = json.loads(match.group())
            else:
                raise ValueError(f"无法解析为JSON: {content[:200]}")

        # ── Step 2: 分层归档 + 补全 ──
        notes = []
        layered = self._organize_layers(extracted, notes)

        # ── Step 3: 物理补全 (转矩/极数) ──
        self._physics_complete(layered, notes)

        # ── Step 4: 场景识别 ──
        merged_specs = {**layered["L1"], **layered["L2"], **layered["L3"]}
        scenario_info = determine_scenario(merged_specs)

        # ── Step 5: 置信度评估 ──
        confidence = self._assess_confidence(layered)

        return {
            "specs": merged_specs,
            "layered_specs": layered,
            "scenario_info": scenario_info,
            "raw_text": raw_text,
            "confidence": confidence,
            "extraction_notes": notes,
        }

    def _build_extraction_prompt(self, raw_text: str) -> str:
        """构建三层提取的 LLM 提示词"""
        return f"""你是一个电机需求分析助手。请从以下客户描述中分层提取关键信息，只返回严格JSON，不要任何解释或markdown标记。

JSON格式（三层结构）：
{{
    "L1": {{
        "motor_type": "异步电动机/永磁同步电动机/无刷直流电动机/开关磁阻电动机",
        "power_kw": 数字（功率kW）,
        "voltage_v": 数字（电压V）,
        "speed_rpm": 数字或null（额定转速rpm）,
        "torque_nm": 数字或null（额定转矩Nm，如客户直接给出）,
        "poles": 数字或null（极数，如客户明确）,
        "cooling": "自然风冷/水冷/油冷/强制风冷/未知"
    }},
    "L2": {{
        "duty_cycle": "S1/S2/S3/S4/未知",
        "ed_pct": 数字或null（负载持续率%，如S3-40%）,
        "overload_pct": 数字或null（过载要求%，如150%×60s）,
        "altitude_m": 数字或null（海拔m）,
        "protection_class": "IP23/IP44/IP54/IP55/IP65/未知",
        "ambient_temp_c": 数字或null（环境温度°C）,
        "frequency_hz": 数字或null（电源频率Hz）
    }},
    "L3": {{
        "mounting": "B3(底脚)/B5(法兰)/B35(底脚+法兰)/未知",
        "insulation_class": "B/F/H/未知",
        "shaft_config": "单轴伸/双轴伸/未知",
        "bearing_type": "深沟球/圆柱滚子/未知",
        "application": "应用场景（如叉车/无人机/电动汽车/工业泵/家电/通用）"
    }}
}}

提取规则：
- L1层为粗选层，必须提取（power_kw和voltage_v是硬性要求）
- L2层为细调层，影响选型修正，尽量提取
- L3层为微调层，有则提取，无则填null或"未知"
- 包含"左右""大约""不低于""以上"等词的数字也要提取
- motor_type: 如果客户未明确说明电机类型，默认填"异步电动机"
- 如果客户给出转矩但没给功率，torque_nm填数字，power_kw填null
- 如果客户给出功率和转速但没给转矩，torque_nm填null（系统会自动计算）
- protection_class: 如果客户提到"防护等级IPXX"则提取
- duty_cycle: 如果客户提到"断续运行""周期工作"则推断S3
- altitude_m: 如果客户提到"高原""海拔XXXX米"则提取
- frequency_hz: 如果客户提到"60Hz""400Hz"等则提取，默认50Hz

客户描述：{raw_text}
JSON:"""

    def _organize_layers(self, extracted: dict, notes: list) -> dict:
        """将 LLM 输出归档为三层结构，并补全默认值"""
        l1 = extracted.get("L1", {})
        l2 = extracted.get("L2", {})
        l3 = extracted.get("L3", {})

        # L1 默认值
        l1.setdefault("motor_type", "异步电动机")
        if l1.get("speed_rpm") is None:
            l1["speed_rpm"] = 3000
            notes.append("转速未指定, 默认3000rpm")
        l1.setdefault("cooling", "未知")
        if l1.get("torque_nm") is None:
            l1["torque_nm"] = None  # 留给物理补全
        if l1.get("poles") is None:
            l1["poles"] = None  # 留给物理补全

        # L2 默认值
        l2.setdefault("duty_cycle", "S1")
        l2.setdefault("protection_class", "IP44")
        l2.setdefault("ambient_temp_c", 40)
        l2.setdefault("frequency_hz", 50)
        l2.setdefault("altitude_m", 0)
        l2.setdefault("overload_pct", 0)
        l2.setdefault("ed_pct", None)

        # L3 默认值
        l3.setdefault("mounting", "未知")
        l3.setdefault("insulation_class", "F")
        l3.setdefault("shaft_config", "单轴伸")
        l3.setdefault("bearing_type", "未知")
        l3.setdefault("application", "通用")

        return {"L1": l1, "L2": l2, "L3": l3}

    def _physics_complete(self, layered: dict, notes: list):
        """物理补全: 转矩/极数推算"""
        l1 = layered["L1"]
        l2 = layered["L2"]

        power = l1.get("power_kw")
        speed = l1.get("speed_rpm")
        torque = l1.get("torque_nm")
        poles = l1.get("poles")
        frequency = l2.get("frequency_hz", 50)

        # 转矩补全: T = 9550 * P / n
        if torque is None and power and speed:
            torque = compute_torque(power, speed)
            l1["torque_nm"] = torque
            notes.append(f"转矩自动计算: T=9550×{power}/{speed}={torque}Nm")

        # 极数补全: 根据转速推算
        if poles is None and speed:
            sync_speed = speed * 1.03 if speed < 3000 else speed * 1.02
            if sync_speed > 2500:
                poles = 2
            elif sync_speed > 1200:
                poles = 4
            elif sync_speed > 800:
                poles = 6
            else:
                poles = 8
            l1["poles"] = poles
            notes.append(f"极数自动推算: n≈{speed}rpm → {poles}极 (同步转速{int(120*frequency/poles)}rpm)")

    def _assess_confidence(self, layered: dict) -> float:
        """
        置信度评估 (基于 L1 粗选层的完整度)
        - power_kw + voltage_v: 硬性 (各0.3)
        - speed_rpm + torque_nm: 重要 (各0.2)
        - motor_type + poles: 辅助 (各0.1) [poles可能被自动推算]
        """
        l1 = layered["L1"]
        weights = {
            "power_kw": 0.25,
            "voltage_v": 0.25,
            "speed_rpm": 0.20,
            "torque_nm": 0.15,
            "motor_type": 0.10,
            "poles": 0.05,
        }
        score = 0.0
        for key, w in weights.items():
            val = l1.get(key)
            if val is not None and val != "未知":
                score += w
        return round(min(score, 1.0), 2)
