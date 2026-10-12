# -*- coding: utf-8 -*-
"""
易经推演评审系统 - 六层聚合规则引擎
======================================
对应《易经》推演逻辑：

    384 爻 → 64 卦 → 8 宫 → 四象 → 三才 → 两仪 → 太极

层级结构：
- Level 1 (爻): 384 个检查点的阴阳判定
- Level 2 (卦): 64 个子领域的加权聚合
- Level 3 (宫): 8 个技术领域的汇总
- Level 4 (四象): 四类技术领域群（阳/阴/太阴/太阳）
- Level 5 (三才): 天/地/人三才维度
- Level 6 (两仪): 可行/不可行二元判定
- Level 7 (太极): 最终评审结论

阴阳判定规则：
- 符合阈值 → 阳 (+1)
- 不符合阈值 → 阴 (-1)
- 强关联项否决 → 直接判阴（一票否决）
"""

import json
import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class YinYang(Enum):
    """阴阳状态"""
    YANG = 1      # 阳：符合
    YIN = -1      # 阴：不符合
    NEUTRAL = 0   # 中性：未判定


@dataclass
class EvaluationResult:
    """单个检查点的评估结果"""
    checkpoint_id: str
    domain: str
    subdomain: str
    checkpoint_name: str
    is_strong: bool
    weight: float
    value: float
    threshold: Tuple[float, float]
    passed: bool
    yinyang: YinYang
    deviation: float = 0.0  # 偏离度


@dataclass
class SubdomainResult:
    """子领域（卦）评估结果"""
    domain: str
    subdomain: str
    score: float           # 加权得分 [0, 1]
    strong_failures: int   # 强关联失败数
    weak_failures: int     # 弱关联失败数
    total_checkpoints: int
    status: str            # 'passed' / 'conditional' / 'failed'
    warnings: List[str] = field(default_factory=list)


@dataclass
class DomainResult:
    """技术领域（宫）评估结果"""
    domain: str
    domain_name: str
    score: float           # 子领域平均分
    weak_domains: int      # 弱领域数
    strong_domains: int    # 强领域数
    status: str            # 'strong' / 'balanced' / 'weak' / 'critical'


@dataclass
class SiXiangResult:
    """四象评估结果"""
    category: str          # '太阳' / '少阳' / '太阴' / '少阴'
    score: float
    domains: List[str]
    summary: str


@dataclass
class SanCaiResult:
    """三才评估结果"""
    dimension: str         # '天' / '地' / '人'
    score: float
    description: str
    n_domains: int = 0     # H28: 参与聚合的域数（0=该维无数据，得分不可信）


@dataclass
class LiangYiResult:
    """两仪评估结果"""
    verdict: str           # '可行' / '不可行'
    confidence: float      # 置信度 [0, 1]
    rationale: str


@dataclass
class TaiJiResult:
    """太极评估结果（最终结论）"""
    overall_score: float   # 综合得分 [0, 1]
    recommendation: str    # 推荐意见
    risk_level: str        # 'low' / 'medium' / 'high' / 'critical'
    key_warnings: List[str]
    detailed_path: str     # 完整推演路径


class YiJingAggregator:
    """易经推演聚合引擎"""

    # 阈值配置
    STRONG_FAIL_THRESHOLD = 0.0      # 强关联失败即否决
    CONDITIONAL_THRESHOLD = 0.5      # 条件通过阈值
    PASS_THRESHOLD = 0.8             # 通过阈值

    # 权重配置
    STRONG_WEIGHT = 0.20             # 强关联权重
    WEAK_WEIGHT = 0.10               # 弱关联权重

    # 四象分类阈值
    SUN_THRESHOLD = 0.7              # 太阳（强阳）
    SHAOYANG_THRESHOLD = 0.5         # 少阳
    TAIYIN_THRESHOLD = 0.3           # 太阴（强阴）

    def __init__(self, checkpoints_db):
        """
        初始化聚合引擎

        Args:
            checkpoints_db: Checkpoint 数据库（从 yijing_checkpoints 导入）
        """
        self.checkpoints_db = checkpoints_db
        self.evaluation_log = []

    def _check_threshold(self, value: float, threshold: Tuple[float, float],
                        threshold_type: str) -> bool:
        """
        检查值是否符合阈值

        Args:
            value: 实际值
            threshold: 阈值 (低, 高) 或 (边界, None)
            threshold_type: 'range' / 'min' / 'max' / 'boolean'

        Returns:
            bool: 是否通过
        """
        if threshold_type == 'range':
            low, high = threshold
            if low is None or high is None:
                # 动态阈值：无明确边界视为通过
                return True
            return low <= value <= high
        elif threshold_type == 'min':
            # min 型: (limit, None)
            limit = threshold[0]
            if limit is None:
                return True
            return value >= limit
        elif threshold_type == 'max':
            # max 型: (None, limit)
            limit = threshold[1]
            if limit is None:
                limit = threshold[0]
            if limit is None:
                return True
            return value <= limit
        elif threshold_type == 'boolean':
            return value == 1
        else:
            return False

    def evaluate_checkpoint(self, checkpoint, actual_value: float,
                           threshold_override: Optional[Tuple[float, float]] = None) -> EvaluationResult:
        """
        评估单个检查点

        Args:
            checkpoint: Checkpoint 对象
            actual_value: 实际测量值
            threshold_override: 可选的阈值覆写（用于动态阈值）

        Returns:
            EvaluationResult
        """
        threshold = threshold_override or checkpoint.threshold_value
        passed = self._check_threshold(actual_value, threshold, checkpoint.threshold_type)

        # 计算偏离度
        deviation = 0.0
        if checkpoint.threshold_type == 'range':
            low, high = threshold
            if low is not None and actual_value < low:
                deviation = (low - actual_value) / abs(low)
            elif high is not None and actual_value > high:
                deviation = (actual_value - high) / abs(high)
        elif checkpoint.threshold_type == 'min':
            limit = threshold[0]
            if limit is None:
                deviation = 0.0
            elif actual_value < limit:
                deviation = max(0.0, (limit - actual_value) / abs(limit))
        elif checkpoint.threshold_type == 'max':
            limit = threshold[1] if threshold[1] is not None else threshold[0]
            if limit is None:
                deviation = 0.0
            elif actual_value > limit:
                deviation = max(0.0, (actual_value - limit) / abs(limit))

        # 阴阳判定
        yinyang = YinYang.YANG if passed else YinYang.YIN

        result = EvaluationResult(
            checkpoint_id=checkpoint.id,
            domain=checkpoint.domain,
            subdomain=checkpoint.subdomain,
            checkpoint_name=checkpoint.name,
            is_strong=checkpoint.is_strong,
            weight=checkpoint.default_weight,
            value=actual_value,
            threshold=threshold,
            passed=passed,
            yinyang=yinyang,
            deviation=deviation,
        )

        self.evaluation_log.append(result)
        return result

    def aggregate_subdomain(self, domain: str, subdomain: str,
                           results: List[EvaluationResult]) -> SubdomainResult:
        """
        聚合子领域（卦）评估结果

        算法：
        - 强关联项任意失败 → 子领域否决（一票否决）
        - 加权得分 = Σ(强关联通过×0.2 + 弱关联通过×0.1) / Σ权重

        Args:
            domain: 技术领域
            subdomain: 子领域
            results: 该子领域的评估结果列表

        Returns:
            SubdomainResult
        """
        strong_results = [r for r in results if r.is_strong]
        weak_results = [r for r in results if not r.is_strong]

        # 一票否决检查
        strong_failures = sum(1 for r in strong_results if not r.passed)
        if strong_failures > 0:
            return SubdomainResult(
                domain=domain,
                subdomain=subdomain,
                score=0.0,
                strong_failures=strong_failures,
                weak_failures=0,
                total_checkpoints=len(results),
                status='failed',
                warnings=[f"强关联项 {strong_failures} 项失败，一票否决"]
            )

        # 加权得分计算
        total_weight = len(strong_results) * self.STRONG_WEIGHT + len(weak_results) * self.WEAK_WEIGHT
        score = 0.0

        for r in strong_results:
            if r.passed:
                score += self.STRONG_WEIGHT
            else:
                score += 0.0  # 已通过一票否决，不会到这里

        for r in weak_results:
            if r.passed:
                score += self.WEAK_WEIGHT

        score /= total_weight if total_weight > 0 else 1.0

        # 状态判定
        weak_failures = sum(1 for r in weak_results if not r.passed)
        warnings = []

        if score >= self.PASS_THRESHOLD:
            status = 'passed'
        elif score >= self.CONDITIONAL_THRESHOLD:
            status = 'conditional'
            warnings.append(f"弱关联项 {weak_failures} 项未通过，建议优化")
        else:
            status = 'failed'
            warnings.append(f"综合得分 {score:.2f} 低于阈值，建议重新设计")

        return SubdomainResult(
            domain=domain,
            subdomain=subdomain,
            score=score,
            strong_failures=0,
            weak_failures=weak_failures,
            total_checkpoints=len(results),
            status=status,
            warnings=warnings
        )

    def aggregate_domain(self, domain: str, domain_name: str,
                        subdomain_results: List[SubdomainResult]) -> DomainResult:
        """
        聚合技术领域（宫）评估结果

        Args:
            domain: 技术领域代码
            domain_name: 技术领域名称
            subdomain_results: 子领域评估结果列表

        Returns:
            DomainResult
        """
        scores = [r.score for r in subdomain_results]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        strong_count = sum(1 for r in subdomain_results if r.status == 'passed')
        weak_count = sum(1 for r in subdomain_results if r.status == 'failed')

        # 领域状态判定
        if avg_score >= 0.8 and weak_count == 0:
            status = 'strong'
        elif avg_score >= 0.6:
            status = 'balanced'
        elif avg_score >= 0.4:
            status = 'weak'
        else:
            status = 'critical'

        return DomainResult(
            domain=domain,
            domain_name=domain_name,
            score=avg_score,
            weak_domains=weak_count,
            strong_domains=strong_count,
            status=status
        )

    def aggregate_sixiang(self, domain_results: List[DomainResult]) -> List[SiXiangResult]:
        """
        聚合四象评估结果

        四象分类：
        - 太阳（强阳）: 所有领域得分 ≥ 0.7
        - 少阳（中阳）: 大部分领域得分 ≥ 0.5
        - 太阴（强阴）: 大部分领域得分 < 0.5
        - 少阴（中阴）: 部分领域得分 ≥ 0.5

        Args:
            domain_results: 技术领域评估结果列表

        Returns:
            四象结果列表
        """
        scores = [d.score for d in domain_results]
        avg_score = sum(scores) / len(scores) if scores else 0.0

        # 分类
        if avg_score >= self.SUN_THRESHOLD:
            category = '太阳'
            summary = '整体性能优异，强烈推荐'
        elif avg_score >= self.SHAOYANG_THRESHOLD:
            category = '少阳'
            summary = '性能良好，部分领域需优化'
        elif avg_score >= self.TAIYIN_THRESHOLD:
            category = '少阴'
            summary = '性能一般，多处需改进'
        else:
            category = '太阴'
            summary = '性能不足，建议重新设计'

        # 按得分排序
        sorted_domains = sorted(domain_results, key=lambda x: x.score, reverse=True)
        strong_domains = [d.domain_name for d in sorted_domains[:4]]
        weak_domains = [d.domain_name for d in sorted_domains[-4:]]

        return [SiXiangResult(
            category=category,
            score=avg_score,
            domains=[d.domain_name for d in sorted_domains],
            summary=summary
        )]

    def aggregate_sancai(self, domain_results: List[DomainResult]) -> List[SanCaiResult]:
        """
        聚合三才评估结果

        三才分类：
        - 天：效率能耗 + 电磁性能 + 可靠性寿命
        - 地：机械结构 + 热管理 + 制造工艺
        - 人：材料选型 + 成本效益

        Args:
            domain_results: 技术领域评估结果列表

        Returns:
            三才结果列表
        """
        tian_domains = ['EM', 'EF', 'RELIAB']
        di_domains = ['ME', 'TH', 'MANU']
        ren_domains = ['MT', 'COST']

        results = []

        for dim, domain_names, name in [
            (tian_domains, ['电磁性能', '效率能耗', '可靠性寿命'], '天'),
            (di_domains, ['机械结构', '热管理', '制造工艺'], '地'),
            (ren_domains, ['材料选型', '成本效益'], '人')
        ]:
            # H28: 按域代码(dim)过滤（旧实现误用中文域名列表 domains，恒 False ⇒ 三维全 0）
            domain_scores = [d.score for d in domain_results if d.domain in dim]
            avg_score = sum(domain_scores) / len(domain_scores) if domain_scores else 0.0

            if not domain_scores:
                desc = f"{name}：参与域 0（数据缺失）"
            elif name == '天':
                desc = f"性能维度：{'优秀' if avg_score >= 0.8 else '良好' if avg_score >= 0.6 else '一般'}"
            elif name == '地':
                desc = f"结构维度：{'稳健' if avg_score >= 0.8 else '合格' if avg_score >= 0.6 else '需改进'}"
            else:
                desc = f"人文维度：{'经济' if avg_score >= 0.8 else '合理' if avg_score >= 0.6 else '偏高'}"

            results.append(SanCaiResult(
                dimension=name,
                score=avg_score,
                description=desc,
                n_domains=len(domain_scores)
            ))

        return results

    def aggregate_liangyi(self, si_xiang: SiXiangResult,
                         san_cai: List[SanCaiResult],
                         sancai_missing: bool = False) -> LiangYiResult:
        """
        聚合两仪评估结果（可行/不可行）

        Args:
            si_xiang: 四象结果
            san_cai: 三才结果列表
            sancai_missing: H29 守卫位——True 表示三才三维均无数据（n_domains=0），
                            综合分仅由四象给出，三才 0 分不参与绑架，rationale 显式标注。

        Returns:
            LiangYiResult
        """
        # 综合得分 = 四象得分 × 0.4 + 三才平均 × 0.6
        sixiang_score = si_xiang.score
        sancai_avg = sum(s.score for s in san_cai) / len(san_cai) if san_cai else 0.0
        if sancai_missing:
            overall_score = sixiang_score
            base_rationale = f"三才三维无数据，综合得分仅按四象 {overall_score:.2f} 计算"
        else:
            overall_score = sixiang_score * 0.4 + sancai_avg * 0.6
            base_rationale = ""

        # 判定
        if overall_score >= 0.7:
            verdict = '可行'
            confidence = overall_score
            rationale = f"综合得分 {overall_score:.2f}，达到可行标准"
        elif overall_score >= 0.5:
            verdict = '有条件可行'
            confidence = overall_score * 0.8
            rationale = f"综合得分 {overall_score:.2f}，需优化部分领域"
        else:
            verdict = '不可行'
            confidence = 1.0 - overall_score
            rationale = f"综合得分 {overall_score:.2f}，建议重新设计"
        if base_rationale:
            rationale = f"{rationale}（{base_rationale}）"

        return LiangYiResult(
            verdict=verdict,
            confidence=confidence,
            rationale=rationale
        )

    def aggregate_taiji(self, domain_results: List[DomainResult],
                       si_xiang: SiXiangResult,
                       san_cai: List[SanCaiResult],
                       liang_yi: LiangYiResult,
                       all_subdomain_results: List[SubdomainResult]) -> TaiJiResult:
        """
        聚合太极评估结果（最终结论）

        Args:
            domain_results: 技术领域结果
            si_xiang: 四象结果
            san_cai: 三才结果
            liang_yi: 两仪结果
            all_subdomain_results: 所有子领域结果

        Returns:
            TaiJiResult
        """
        # 综合得分
        overall_score = liang_yi.confidence if liang_yi.verdict == '可行' else 1.0 - liang_yi.confidence

        # 风险等级
        critical_domains = [d for d in domain_results if d.status == 'critical']
        if len(critical_domains) >= 3:
            risk_level = 'critical'
        elif len(critical_domains) >= 1:
            risk_level = 'high'
        elif sum(1 for d in domain_results if d.status == 'weak') >= 3:
            risk_level = 'medium'
        else:
            risk_level = 'low'

        # 生成推荐意见
        recommendations = []
        if liang_yi.verdict == '可行':
            recommendations.append("方案整体可行，建议进入下一阶段")
        elif liang_yi.verdict == '有条件可行':
            recommendations.append("方案需优化，建议针对薄弱环节改进后重新评估")
        else:
            recommendations.append("方案不可行，建议重新设计")

        # 关键警告
        key_warnings = []
        for d in critical_domains:
            key_warnings.append(f"{d.domain_name}领域评分过低 ({d.score:.2f})")

        for s in all_subdomain_results:
            if s.status == 'failed' and s.strong_failures > 0:
                key_warnings.append(f"{s.domain}-{s.subdomain} 强关联项失败")

        # 推演路径
        path = f"""
【易经推演路径】
1. 384 爻判定: 完成 {len(self.evaluation_log)} 项检查点评估
2. 64 卦聚合: {len([s for s in all_subdomain_results if s.status == 'passed'])} 项通过，
              {len([s for s in all_subdomain_results if s.status == 'failed'])} 项失败
3. 8 宫汇总:  {len([d for d in domain_results if d.status == 'strong'])} 强领域，
              {len([d for d in domain_results if d.status == 'critical'])} 临界领域
4. 四象定性: {si_xiang.category} (得分 {si_xiang.score:.2f})
5. 三才评估: 天={san_cai[0].score:.2f} 地={san_cai[1].score:.2f} 人={san_cai[2].score:.2f}
6. 两仪判定: {liang_yi.verdict} (置信度 {liang_yi.confidence:.2f})
7. 太极结论: 综合得分 {overall_score:.2f}，风险等级 {risk_level}
"""

        return TaiJiResult(
            overall_score=overall_score,
            recommendation='\n'.join(recommendations),
            risk_level=risk_level,
            key_warnings=key_warnings[:5],  # 最多 5 条
            detailed_path=path.strip()
        )

    def full_evaluation(self, evaluation_data: Dict) -> Dict:
        """
        完整推演流程

        Args:
            evaluation_data: 评估数据字典，格式：
                {
                    "domain": {
                        "subdomain": {
                            "checkpoint_id": actual_value
                        }
                    }
                }

        Returns:
            完整评估结果字典
        """
        from Core.yijing_checkpoints import (
            build_checkpoint_database, get_checkpoints_by_domain,
            get_checkpoints_by_subdomain
        )

        checkpoints_db = build_checkpoint_database()
        self.evaluation_log = []

        # Level 1: 评估所有检查点
        all_results = []
        for domain, subdomains in evaluation_data.items():
            for subdomain, checkpoints in subdomains.items():
                for cp_id, value in checkpoints.items():
                    cp = next((c for c in checkpoints_db if c.id == cp_id), None)
                    if cp:
                        result = self.evaluate_checkpoint(cp, value)
                        all_results.append(result)

        # Level 2: 聚合子领域（卦）
        subdomain_results = []
        for domain in evaluation_data:
            for subdomain in evaluation_data[domain]:
                domain_results = [r for r in all_results
                                 if r.domain == domain and r.subdomain == subdomain]
                if domain_results:
                    sub_result = self.aggregate_subdomain(domain, subdomain, domain_results)
                    subdomain_results.append(sub_result)

        # Level 3: 聚合技术领域（宫）
        domain_results = []
        from Core.yijing_checkpoints import DOMAINS
        for domain, domain_name in DOMAINS.items():
            if domain in evaluation_data:
                subs = [r for r in subdomain_results if r.domain == domain]
                if subs:
                    domain_result = self.aggregate_domain(domain, domain_name, subs)
                    domain_results.append(domain_result)

        # Level 4: 四象
        si_xiang_results = self.aggregate_sixiang(domain_results)

        # Level 5: 三才
        san_cai_results = self.aggregate_sancai(domain_results)

        # Level 6: 两仪
        # H29: 三才无数据守卫——参与域数为 0 的维度（n_domains=0）其 0 分是数据缺失而非真实差分，
        # 不得参与综合绑架（旧缺陷：三才恒 0 ⇒ 四象"太阳·强烈推荐" vs 两仪"不可行"自相矛盾）
        valid_sancai = [s for s in san_cai_results if getattr(s, 'n_domains', 0) > 0]
        if valid_sancai:
            liang_yi_result = self.aggregate_liangyi(si_xiang_results[0], valid_sancai)
        else:
            liang_yi_result = self.aggregate_liangyi(
                si_xiang_results[0], san_cai_results, sancai_missing=True)

        # Level 7: 太极
        taiji_result = self.aggregate_taiji(
            domain_results, si_xiang_results[0],
            san_cai_results, liang_yi_result, subdomain_results
        )

        # 返回完整结果
        return {
            'evaluation_log': [
                {
                    'id': r.checkpoint_id,
                    'domain': r.domain,
                    'subdomain': r.subdomain,
                    'name': r.checkpoint_name,
                    'value': r.value,
                    'threshold': list(r.threshold),
                    'passed': r.passed,
                    'yinyang': r.yinyang.name,
                    'is_strong': r.is_strong,
                    'weight': r.weight,
                    'deviation': r.deviation
                }
                for r in self.evaluation_log
            ],
            'subdomain_results': [
                {
                    'domain': r.domain,
                    'subdomain': r.subdomain,
                    'score': r.score,
                    'status': r.status,
                    'strong_failures': r.strong_failures,
                    'weak_failures': r.weak_failures,
                    'warnings': r.warnings
                }
                for r in subdomain_results
            ],
            'domain_results': [
                {
                    'domain': r.domain,
                    'domain_name': r.domain_name,
                    'score': r.score,
                    'status': r.status,
                    'strong_domains': r.strong_domains,
                    'weak_domains': r.weak_domains
                }
                for r in domain_results
            ],
            'si_xiang': {
                'category': si_xiang_results[0].category,
                'score': si_xiang_results[0].score,
                'summary': si_xiang_results[0].summary
            },
            'san_cai': [
                {
                    'dimension': r.dimension,
                    'score': r.score,
                    'description': r.description,
                    'n_domains': getattr(r, 'n_domains', 0)
                }
                for r in san_cai_results
            ],
            'liang_yi': {
                'verdict': liang_yi_result.verdict,
                'confidence': liang_yi_result.confidence,
                'rationale': liang_yi_result.rationale
            },
            'taiji': {
                'overall_score': taiji_result.overall_score,
                'recommendation': taiji_result.recommendation,
                'risk_level': taiji_result.risk_level,
                'key_warnings': taiji_result.key_warnings,
                'detailed_path': taiji_result.detailed_path
            }
        }


# 导出
if __name__ == "__main__":
    # 测试示例
    from Core.yijing_checkpoints import build_checkpoint_database

    aggregator = YiJingAggregator(build_checkpoint_database())

    # 模拟评估数据
    test_data = {
        "EM": {
            "磁路设计": {
                "EM0101C01": 0.7,   # Bg=0.7T ✓
                "EM0101C02": 1.5,   # Bt=1.5T ✓
                "EM0101C03": 1.0,   # By=1.0T ✓
            },
            "绕组配置": {
                "EM0201C01": 24,    # 匝数=24 ✓
                "EM0201C02": 1.2,   # 线规=1.2mm ✓
                "EM0201C03": 0.65,  # 槽满率=65% ✓
            }
        },
        "TH": {
            "温升分布": {
                "TH0101C01": 85,    # 绕组温升=85K ✓
                "TH0101C02": 70,    # 铁心温升=70K ✓
            }
        }
    }

    result = aggregator.full_evaluation(test_data)
    print(json.dumps(result, indent=2, ensure_ascii=False))
