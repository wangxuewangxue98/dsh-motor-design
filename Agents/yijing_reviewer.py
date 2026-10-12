# -*- coding: utf-8 -*-
"""
易经推演评审智能体 - 核心类实现
==================================
与正侧评价方法并行运行，灰度观察阶段不接入主流程

设计理念：
- 借鉴《易经》六层推演逻辑：爻→卦→宫→象→才→仪→极
- 384 检查点结构化判定，强/弱关联权重区分
- 输出结构化 JSON，便于与正侧评价结果对比

灰度开关：
- YIJING_REVIEW_GRAY.enabled = False（默认关闭）
- 结果存储到 yijing_gray_logs/ 目录
- 仅记录，不注入 pipeline_steps
"""

import json
import os
import logging
import time
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=False)

from base_agent import BaseAgent, extract_ollama_usage
try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")

from Core.yijing_checkpoints import (
    build_checkpoint_database,
    get_checkpoints_by_domain,
    DOMAINS
)
from Core.yijing_aggregator import YiJingAggregator
# 阶段二：完整参数映射表（cp_id → results/specs 字段）
from Core.yijing_mapping import build_evaluation_data as _map_build_evaluation_data


logger = logging.getLogger(__name__)


class IChingReviewer(BaseAgent):
    """易经推演评审师 - 结构化判定 + 六层聚合"""

    # 灰度开关（默认关闭）
    GRAY_ENABLED = os.getenv("YIJING_REVIEW_GRAY_ENABLED", "False").lower() == "true"
    GRAY_LOG_DIR = r"D:\MotorDesign\yijing_gray_logs"

    def __init__(self):
        super().__init__(
            name="IChingReviewer",
            description="易经推演评审师：384 爻结构化判定，六层聚合推演，与正侧评价并行比对"
        )
        self.checkpoints_db = build_checkpoint_database()
        self.aggregator = YiJingAggregator(self.checkpoints_db)
        self.gray_log_counter = 0

    def run(self, results: list, specs: dict,
            positive_review: dict = None,
            negative_review: dict = None,
            **kwargs) -> dict:
        """
        运行易经推演评审

        Args:
            results: Top10 方案结果列表
            specs: 需求规格
            positive_review: 正侧评价结果（ReviewAgent 输出）
            negative_review: 负侧评价结果（四剑客输出）

        Returns:
            {
                "yijing_review": str,           # 文本报告
                "structured_result": dict,      # 结构化 JSON
                "comparison_with_positive": dict,  # 与正侧评价对比
                "warnings": list,               # 警告项
                "gray_log_path": str,           # 灰度日志路径（如启用）
                "mapping_stats": dict,          # 映射来源统计 + 覆盖率（阶段二）
                "na_checkpoints": list,         # 显式 NA 检查点清单（阶段二）
            }
        """
        if not results:
            return {
                "yijing_review": "无方案可评审",
                "structured_result": None,
                "warnings": ["无方案数据"]
            }

        # 提取最佳方案的关键参数（阶段二：完整参数映射表）
        best = results[0]
        mapping = self._extract_evaluation_data(best, specs)
        evaluation_data = mapping["values"]

        # 执行六层推演
        start_time = time.time()
        structured_result = self.aggregator.full_evaluation(evaluation_data)
        elapsed = time.time() - start_time

        # 生成文本报告
        yijing_review = self._generate_text_report(
            structured_result, best, specs, elapsed, mapping
        )

        # 与正侧评价对比（如提供）
        comparison = {}
        if positive_review:
            comparison = self._compare_with_positive(
                structured_result, positive_review
            )

        # 提取警告项
        warnings = self._extract_warnings(structured_result)

        # 灰度日志（如启用）
        gray_log_path = None
        if self.GRAY_ENABLED:
            gray_log_path = self._save_gray_log(
                structured_result, comparison, warnings, best, mapping
            )

        return {
            "yijing_review": yijing_review,
            "structured_result": structured_result,
            "comparison_with_positive": comparison,
            "warnings": warnings,
            "gray_log_path": gray_log_path,
            "elapsed_s": elapsed,
            "method": "yijing_six_layer_inference",
            "mapping_stats": mapping["stats"],
            "na_checkpoints": mapping["na_checkpoints"],
        }

    def _extract_evaluation_data(self, best_result: dict, specs: dict) -> dict:
        """
        从方案结果中提取评估数据（阶段二全映射版）

        委托 Core.yijing_mapping.build_evaluation_data()：
        384 检查点完整归类到 direct/scaled/derived/specs/const/NA 六类来源，
        无数据项用阈值保守默认值兜底（不造假），并给出可审计的来源统计。

        Args:
            best_result: 最佳方案结果字典
            specs: 需求规格

        Returns:
            mapping_result: {
                "values": {domain: {subdomain: {cp_id: value}}},
                "source_map": {cp_id: source 字符串},
                "na_checkpoints": [cp_id],
                "stats": {total, mapped, na, coverage, by_source},
            }
        """
        return _map_build_evaluation_data(best_result or {}, specs or {})

    def _generate_text_report(self, structured_result: dict,
                              best_result: dict, specs: dict,
                              elapsed: float,
                              mapping: Optional[dict] = None) -> str:
        """生成易经推演评审文本报告"""
        taiji = structured_result.get("taiji", {})
        liangyi = structured_result.get("liang_yi", {})
        si_xiang = structured_result.get("si_xiang", {})
        san_cai = structured_result.get("san_cai", [])

        lines = [
            "=" * 60,
            "易经推演评审报告",
            "=" * 60,
            f"需求: {specs.get('power_kw', '?')}kW / {specs.get('voltage_v', '?')}V / {specs.get('cooling', '?')}",
            f"最佳方案: 外径{best_result.get('stator_od', '?')}mm / "
            f"效率{best_result.get('efficiency', '?')}% / "
            f"温升{best_result.get('max_temp', '?')}K",
            f"推演耗时: {elapsed:.2f}s",
            "",
            "【六层推演结果】",
            "",
            f"1. 384 爻判定: 完成 {len(structured_result.get('evaluation_log', []))} 项检查点",
            f"2. 64 卦聚合: {len([s for s in structured_result.get('subdomain_results', []) if s['status'] == 'passed'])} 项通过，"
            f"{len([s for s in structured_result.get('subdomain_results', []) if s['status'] == 'failed'])} 项失败",
            f"3. 8 宫汇总:  {len([d for d in structured_result.get('domain_results', []) if d['status'] == 'strong'])} 强领域，"
            f"{len([d for d in structured_result.get('domain_results', []) if d['status'] == 'critical'])} 临界领域",
            f"4. 四象定性: {si_xiang.get('category', '?')} (得分 {si_xiang.get('score', 0):.2f})",
            "",
            "【三才评估】",
        ]

        for sc in san_cai:
            lines.append(f"  - {sc['dimension']} ({sc['description']})")

        lines.extend([
            "",
            "【两仪判定】",
            f"  结论: {liangyi.get('verdict', '?')}",
            f"  置信度: {liangyi.get('confidence', 0):.2f}",
            f"  理由: {liangyi.get('rationale', '?')}",
            "",
            "【太极结论】",
            f"  综合得分: {taiji.get('overall_score', 0):.2f}",
            f"  风险等级: {taiji.get('risk_level', '?')}",
            f"  推荐意见: {taiji.get('recommendation', '?')}",
            "",
        ])

        # 阶段二：映射覆盖率与来源统计
        if mapping:
            stats = mapping.get("stats", {})
            lines.extend([
                "【参数映射覆盖（阶段二）】",
                f"  检查点总数: {stats.get('total', 0)}",
                f"  已映射: {stats.get('mapped', 0)} (覆盖率 {stats.get('coverage', 0)*100:.1f}%)",
                f"  显式 NA: {len(mapping.get('na_checkpoints', []))} 项（外部数据域，排除出聚合）",
                f"  来源分布: {stats.get('by_source', {})}",
                "",
            ])

        # 关键警告
        key_warnings = taiji.get("key_warnings", [])
        if key_warnings:
            lines.append("【关键警告】")
            for w in key_warnings[:5]:
                lines.append(f"  ⚠ {w}")
            lines.append("")

        lines.append("【详细推演路径】")
        lines.append(taiji.get("detailed_path", "无"))
        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    def _compare_with_positive(self, yijing_result: dict,
                               positive_review: dict) -> dict:
        """
        与正侧评价结果对比

        对比维度：
        1. 推荐方案是否一致
        2. 风险判断是否一致
        3. 分歧点识别

        Args:
            yijing_result: 易经推演结果
            positive_review: 正侧评价结果

        Returns:
            对比分析字典
        """
        # 提取正侧评价的推荐
        positive_rec = positive_review.get("recommendation", "")
        yijing_rec = yijing_result.get("taiji", {}).get("recommendation", "")

        # 简单对比（实际应做语义分析）
        consistent = "可行" in positive_rec and "可行" in yijing_rec

        return {
            "recommendation_consistent": consistent,
            "positive_recommendation": positive_rec[:100],
            "yijing_recommendation": yijing_rec[:100],
            "divergence_points": [],  # 待实现语义分析
            "confidence_gap": abs(
                positive_review.get("confidence", 0.5) -
                yijing_result.get("taiji", {}).get("confidence", 0.5)
            )
        }

    def _extract_warnings(self, structured_result: dict) -> List[str]:
        """提取警告项"""
        warnings = []

        # 从太极结论提取
        taiji = structured_result.get("taiji", {})
        warnings.extend(taiji.get("key_warnings", []))

        # 从子领域结果提取
        for sub in structured_result.get("subdomain_results", []):
            if sub.get("status") == "failed":
                warnings.append(
                    f"{sub['domain']}-{sub['subdomain']} 评估失败 "
                    f"(强关联失败 {sub.get('strong_failures', 0)} 项)"
                )

        # 从领域结果提取
        for dom in structured_result.get("domain_results", []):
            if dom.get("status") == "critical":
                warnings.append(f"{dom['domain_name']} 领域评分过低 ({dom['score']:.2f})")

        return list(set(warnings))  # 去重

    def _save_gray_log(self, structured_result: dict,
                      comparison: dict,
                      warnings: List[str],
                      best_result: dict,
                      mapping: Optional[dict] = None) -> str:
        """保存灰度日志"""
        os.makedirs(self.GRAY_LOG_DIR, exist_ok=True)
        self.gray_log_counter += 1

        log_data = {
            "timestamp": datetime.now().isoformat(),
            "log_id": self.gray_log_counter,
            "specs": {
                "power_kw": best_result.get("power_kw"),
                "voltage_v": best_result.get("voltage_v"),
            },
            "best_solution": {
                "stator_od": best_result.get("stator_od"),
                "efficiency": best_result.get("efficiency"),
                "max_temp": best_result.get("max_temp"),
            },
            "yijing_result": structured_result,
            "comparison": comparison,
            "warnings": warnings,
        }

        # 阶段二：附映射来源统计与 NA 清单（可审计）
        if mapping:
            log_data["mapping_stats"] = mapping.get("stats")
            log_data["na_checkpoints"] = mapping.get("na_checkpoints")

        log_path = os.path.join(
            self.GRAY_LOG_DIR,
            f"yijing_gray_{int(time.time())}_{self.gray_log_counter}.json"
        )
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)

        logger.info(f"[IChingReviewer] 灰度日志已保存: {log_path}")
        return log_path


# 导出
if __name__ == "__main__":
    # 测试示例
    reviewer = IChingReviewer()

    # 模拟输入
    test_results = [{
        "stator_od": 200.0,
        "stator_id": 120.0,
        "core_length": 150.0,
        "efficiency": 92.5,
        "max_temp": 85.0,
        "air_gap_flux_density": 0.7,
        "stator_tooth_flux_density": 1.5,
        "stator_yoke_flux_density": 1.2,
        "slot_fill_rate": 0.65,
        "turns_per_coil": 24,
        "wire_diameter": 1.2,
    }]

    test_specs = {
        "power_kw": 15,
        "voltage_v": 380,
        "cooling": "IC411",
    }

    result = reviewer.run(test_results, test_specs)
    print(result["yijing_review"])
