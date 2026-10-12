"""
方案生成师智能体 - 根据规格生成参数矩阵并批量求解
大脑: 规则引擎 + Motor-CAD API (或模拟)
"""
import json
import os
import time
from typing import Optional
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=False)

from base_agent import BaseAgent
import sys
sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Ontology")

from motor_tools import generate_param_matrix, run_one_design, batch_solve
from motor_tools import LAST_DIVERSITY_METRICS
from evolution_engine import find_matching_templates


class DesignAgent(BaseAgent):
    """方案生成师 - 生成参数矩阵 + 批量求解"""

    def __init__(self):
        super().__init__(
            name="DesignAgent",
            description="根据规格生成参数矩阵并批量求解"
        )

    def run(self, specs: dict, count: int = 10, **kwargs) -> dict:
        """
        生成参数矩阵并求解
        返回: {"results": [...], "total": int, "success": int, "matched_templates": [...]}
        """
        # 1. 查找匹配的技能模板
        matched = find_matching_templates(specs, top_n=3)

        # 2. 生成参数矩阵
        params_list, _ = generate_param_matrix(specs, count)

        # 3. 批量求解 (v3.00.11: 传递电机类型)
        motor_type = specs.get("motor_type", "异步电动机")
        results = batch_solve(params_list, machine_type=motor_type)

        # 4. 按效率排序
        # v4.05 (R1 修复): 排序键改为元组键 (是否为数值, 数值或0),
        # 避免某方案 efficiency=None(或键缺失)时 '>' not supported
        # between 'int' and 'NoneType' 崩溃。dict.get 默认值仅在键缺失时生效,
        # 键存在但值为 None 时返回 None, 故显式用 (值 is not None, 值 or 0)。
        results.sort(
            key=lambda x: (
                isinstance(x.get("efficiency"), (int, float))
                and not isinstance(x.get("efficiency"), bool),
                x.get("efficiency") or 0,
            ),
            reverse=True,
        )

        return {
            "results": results,
            "total": len(params_list),
            "success": len(results),
            "matched_templates": matched,
            "param_count": len(params_list),
            # v4.04 (#10): 方案多样性度量 + 进化变异披露
            "diversity": dict(LAST_DIVERSITY_METRICS),
            "mutated_count": LAST_DIVERSITY_METRICS.get("mutated", 0),
            "coverage": LAST_DIVERSITY_METRICS.get("coverage", 0.0),
            "innovation": LAST_DIVERSITY_METRICS.get("innovation", 0.0),
        }
