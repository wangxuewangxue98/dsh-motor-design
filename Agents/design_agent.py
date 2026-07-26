"""
方案生成师智能体 - 根据规格生成参数矩阵并批量求解
大脑: 规则引擎 + Motor-CAD API (或模拟)
"""
import json
import os
import time
from typing import Optional
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=True)

from base_agent import BaseAgent
import sys
sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Ontology")

from motor_tools import generate_param_matrix, run_one_design, batch_solve
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
        params_list = generate_param_matrix(specs, count)

        # 3. 批量求解
        results = batch_solve(params_list)

        # 4. 按效率排序
        results.sort(key=lambda x: x.get("efficiency", 0), reverse=True)

        return {
            "results": results,
            "total": len(params_list),
            "success": len(results),
            "matched_templates": matched,
            "param_count": len(params_list)
        }
