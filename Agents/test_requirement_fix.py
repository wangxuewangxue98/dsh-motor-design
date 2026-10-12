# -*- coding: utf-8 -*-
"""
v4.01.039 需求分析师修复验证
1) 用户示例文本: 不触发任何阻断问题(mounting 是 L3, 不再阻断),
   且"安装空间外径限制250mm"被识别为 outer_diameter_limit_mm=250
2) 缺 L1 的文本: 仍正确触发 L1 阻断(_pending_questions 非空)
"""
import sys, json

sys.path.insert(0, r"D:\MotorDesign\Agents")
sys.path.insert(0, r"D:\MotorDesign\Core")

import requirement_agent as RA


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload

    def invoke(self, prompt):
        return _FakeMsg(self._payload)


# 模拟本地 3B 模型: 给出主要字段, 但不含 outer_diameter(验证文本正则补充),
# 也不含 mounting(验证 L3 不阻断)
FAKE_JSON = json.dumps({
    "L1": {"motor_type": "异步电动机", "power_kw": 10, "voltage_v": 48,
           "speed_rpm": 1500, "torque_nm": None, "poles": 4, "cooling": "水冷"},
    "L2": {"duty_cycle": "S1", "protection_class": "未知", "ambient_temp_c": 40,
           "frequency_hz": None, "altitude_m": None},
    "L3": {"mounting": "未知", "insulation_class": "未知", "shaft_config": "单轴伸",
           "bearing_type": "未知", "application": "叉车"},
})


def _patch(agent_cls, payload):
    agent_cls._get_llm = lambda self: _FakeLLM(payload)
    RA.knowledge_engine.infer_all_params = lambda merged: {}
    RA.knowledge_engine.get_industry_defaults = lambda app, power: {}
    # 避免触达型谱库/目录查询
    agent_cls._extract_center_height_and_query_catalog = lambda self, rt, layered, notes: None


# 缺 L1 的模型输出(模拟模型没识别出任何核心参数)
EMPTY_JSON = json.dumps({
    "L1": {"motor_type": None, "power_kw": None, "voltage_v": None,
           "speed_rpm": None, "torque_nm": None, "poles": None, "cooling": "未知"},
    "L2": {"duty_cycle": "S1", "protection_class": "未知", "ambient_temp_c": 40,
           "frequency_hz": None, "altitude_m": None},
    "L3": {"mounting": "未知", "insulation_class": "未知", "shaft_config": "单轴伸",
           "bearing_type": "未知", "application": "通用"},
})


def test_user_example():
    _patch(RA.RequirementAgent, FAKE_JSON)
    text = ("我们要一台电动叉车驱动电机，异步电动机，1500rpm，4极，"
            "额定功率10kW，48V电池供电，水冷，希望效率不低于93%，"
            "安装空间外径限制250mm。")
    agent = RA.RequirementAgent()
    res = agent.run(text)
    assert "error" not in res, f"run 返回错误: {res.get('error')}"
    layered = res["layered_specs"]

    l1_pending = layered["L1"].get("_pending_questions", [])
    pending_params = [q["param"] for q in l1_pending]
    od = layered["L3"].get("outer_diameter_limit_mm")
    mounting = layered["L3"].get("mounting")

    print("[示例] L1 pending(阻断):", pending_params)
    print("[示例] outer_diameter_limit_mm =", od)
    print("[示例] mounting =", mounting)

    assert l1_pending == [], f"示例不应有任何 L1 阻断问题, 实际: {pending_params}"
    assert "mounting" not in pending_params, "mounting(L3) 不应出现在阻断列表"
    assert od == 250.0, f"外径约束未被识别, 实际: {od}"
    print("  ✓ 示例: 零阻断 + 外径约束=250mm 已识别\n")


def test_missing_l1_blocks():
    _patch(RA.RequirementAgent, EMPTY_JSON)
    text = "一台电机"  # 缺 power/voltage/speed/poles/motor_type
    agent = RA.RequirementAgent()
    res = agent.run(text)
    assert "error" not in res, f"run 返回错误: {res.get('error')}"
    layered = res["layered_specs"]
    l1_pending = layered["L1"].get("_pending_questions", [])
    pending_params = [q["param"] for q in l1_pending]
    print("[缺L1] L1 pending(阻断):", pending_params)
    assert l1_pending, "缺 L1 时必须触发阻断问题"
    assert "power_kw" in pending_params, "power_kw 缺失应阻断"
    print("  ✓ 缺 L1: 正确触发阻断(power_kw 等)\n")


if __name__ == "__main__":
    test_user_example()
    test_missing_l1_blocks()
    print("ALL PASS")
