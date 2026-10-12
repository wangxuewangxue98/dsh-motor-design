# -*- coding: utf-8 -*-
"""
评审规则库引擎 (ReviewRulesEngine) — 三层架构·第三层
=====================================================
独立于全局共享层和角色专用层的"第三方监理"：

  1. 一致性校验 — 需求与方案是否匹配(如需求水冷但方案自扇冷)
  2. 双轨制衡 — 放大轨推荐A vs 收敛轨反对A的矛盾检测
  3. 合规检查 — 行业强制性标准(如船用电机必须CCS认证)
  4. 成本阈值 — 报价低于成本时报警、效率低于IE标准报警

使用方式:
  from Core.review_rules import review_engine

  # 一致性检查
  issues = review_engine.check_consistency(specs, design)

  # 双轨制衡
  conflicts = review_engine.check_dual_track(support_report, oppose_report, designs)

  # 合规检查
  compliance = review_engine.check_compliance(specs, design)

  # 成本阈值
  cost_issues = review_engine.check_cost_performance(design, quote=None)

  # 综合检查 (一次性执行所有规则)
  full_report = review_engine.full_check(specs, designs, support_report, oppose_report)
"""
import json
import os
import sys
from typing import Optional

# ── 路径配置 ──────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KNOWLEDGE_DIR = os.path.join(_BASE, "Knowledge")
_SCRIPTS_DIR = os.path.join(_BASE, "Scripts")

if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
if _BASE not in sys.path:
    sys.path.insert(0, os.path.join(_BASE, "Core"))


class ReviewRulesEngine:
    """
    评审规则库 — 三层架构·第三层

    跨领域冲突检查规则，独立于智能体角色偏好，
    像公正的第三方监理，专门负责找茬。
    """

    def __init__(self):
        # 加载共享知识引擎
        try:
            from shared_knowledge import knowledge_engine
            self.ke = knowledge_engine
        except Exception:
            self.ke = None

        # 加载设计规则
        self._design_rules = self._load_json("design_rules.json")
        self._condition_corrections = self._load_json("condition_corrections.json")

    def _load_json(self, filename: str) -> dict:
        """加载知识库JSON"""
        path = os.path.join(_KNOWLEDGE_DIR, filename)
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ═══════════════════════════════════════════════════════════
    # 1. 一致性校验 — 需求 vs 方案
    # ═══════════════════════════════════════════════════════════

    def check_consistency(self, specs: dict, design: dict) -> dict:
        """
        需求与方案的一致性校验

        检查方案是否满足需求中的关键约束：
          - 冷却方式一致
          - 电压一致
          - 极数一致
          - 防护等级满足
          - 绝缘等级满足
          - 工作制匹配
          - 外径限制

        返回: {
            "passed": bool,
            "issues": [{"severity": "critical|warning", "field": str, "spec_value": str, "design_value": str, "message": str}],
            "summary": str
        }
        """
        issues = []

        # ── 冷却方式 ──
        spec_cooling = (specs.get("cooling") or "").lower()
        design_cooling = (design.get("cooling") or "").lower()
        if spec_cooling and design_cooling:
            if ("水冷" in spec_cooling or "water" in spec_cooling) and "水冷" not in design_cooling and "water" not in design_cooling:
                issues.append({
                    "severity": "critical",
                    "field": "cooling",
                    "spec_value": spec_cooling,
                    "design_value": design_cooling,
                    "message": "需求要求水冷，但方案中使用了非水冷冷却，严重不匹配"
                })
            elif ("油冷" in spec_cooling or "oil" in spec_cooling) and "油冷" not in design_cooling and "oil" not in design_cooling:
                issues.append({
                    "severity": "critical",
                    "field": "cooling",
                    "spec_value": spec_cooling,
                    "design_value": design_cooling,
                    "message": "需求要求油冷，但方案中使用了非油冷冷却"
                })

        # ── 电压 ──
        spec_voltage = specs.get("voltage_v")
        design_voltage = design.get("voltage") or design.get("voltage_v")
        if spec_voltage and design_voltage and abs(spec_voltage - design_voltage) > 1:
            issues.append({
                "severity": "critical",
                "field": "voltage_v",
                "spec_value": spec_voltage,
                "design_value": design_voltage,
                "message": f"需求电压{spec_voltage}V，方案电压{design_voltage}V，不一致"
            })

        # ── 极数 ──
        spec_poles = specs.get("poles")
        design_poles = design.get("poles")
        if spec_poles and design_poles and spec_poles != design_poles:
            issues.append({
                "severity": "warning",
                "field": "poles",
                "spec_value": spec_poles,
                "design_value": design_poles,
                "message": f"需求极数{spec_poles}极，方案极数{design_poles}极，请确认是否合理"
            })

        # ── 防护等级 ──
        spec_protection = specs.get("protection_class", "").upper()
        design_protection = (design.get("protection_class") or "").upper()
        if spec_protection and design_protection:
            # IP44 > IP23, IP65 > IP44...
            spec_num = int(spec_protection.replace("IP", "")) if spec_protection.startswith("IP") else 0
            design_num = int(design_protection.replace("IP", "")) if design_protection.startswith("IP") else 0
            if design_num < spec_num:
                issues.append({
                    "severity": "critical",
                    "field": "protection_class",
                    "spec_value": spec_protection,
                    "design_value": design_protection,
                    "message": f"需求防护等级{spec_protection}，方案{design_protection}，未满足需求"
                })

        # ── 绝缘等级 ──
        spec_insulation = specs.get("insulation_class", "").upper()
        design_insulation = (design.get("insulation_class") or "").upper()
        if spec_insulation and design_insulation:
            rank = {"A": 1, "E": 2, "B": 3, "F": 4, "H": 5, "C": 6}
            if rank.get(design_insulation, 0) < rank.get(spec_insulation, 0):
                issues.append({
                    "severity": "critical",
                    "field": "insulation_class",
                    "spec_value": spec_insulation,
                    "design_value": design_insulation,
                    "message": f"需求绝缘等级{spec_insulation}，方案{design_insulation}，等级偏低"
                })

        # ── 工作制 ──
        spec_duty = (specs.get("duty_cycle") or "").upper()
        design_duty = (design.get("duty_cycle") or "").upper()
        if spec_duty and design_duty and spec_duty != design_duty:
            issues.append({
                "severity": "warning",
                "field": "duty_cycle",
                "spec_value": spec_duty,
                "design_value": design_duty,
                "message": f"需求工作制{spec_duty}，方案{design_duty}，请确认"
            })

        # ── 外径限制 ──
        spec_od_limit = specs.get("stator_od_limit") or specs.get("od_limit")
        design_od = design.get("stator_od") or design.get("stator_od_mm")
        if spec_od_limit and design_od and design_od > spec_od_limit:
            issues.append({
                "severity": "critical",
                "field": "stator_od_limit",
                "spec_value": spec_od_limit,
                "design_value": design_od,
                "message": f"需求外径限制{spec_od_limit}mm，方案外径{design_od}mm，超标"
            })

        # ── 长径比合理性 ──
        if self.ke:
            d = design.get("stator_id") or design.get("stator_inner_dia_mm")
            l = design.get("core_length") or design.get("core_length_mm")
            poles = design_poles or 4
            if d and l:
                lam_check = self.ke.validate_lambda(l, d, poles)
                if not lam_check["valid"]:
                    issues.append({
                        "severity": "warning",
                        "field": "lambda",
                        "spec_value": f"{lam_check['range']}",
                        "design_value": f"λ={lam_check['lambda']}",
                        "message": lam_check["advice"]
                    })

        critical_count = sum(1 for i in issues if i["severity"] == "critical")
        warning_count = sum(1 for i in issues if i["severity"] == "warning")

        return {
            "passed": critical_count == 0,
            "issues": issues,
            "critical_count": critical_count,
            "warning_count": warning_count,
            "summary": f"一致性校验: {'通过' if critical_count == 0 else '未通过'} "
                       f"({critical_count}严重, {warning_count}警告)"
        }

    # ═══════════════════════════════════════════════════════════
    # 2. 双轨制衡 — 放大轨 vs 收敛轨
    # ═══════════════════════════════════════════════════════════

    def check_dual_track(self, support_report: str, oppose_report: str,
                         designs: list) -> dict:
        """
        双轨制衡检测

        检查放大轨(支持报告)与收敛轨(反对报告)之间的矛盾：
          - 放大轨推荐方案A，但收敛轨判定A有严重问题
          - 长径比超出应用推荐范围
          - 效率与成本矛盾

        返回: {
            "conflicts": [{"type": str, "description": str, "severity": str, "resolution": str}],
            "requires_human_review": bool,
            "summary": str
        }
        """
        conflicts = []

        if not designs:
            return {"conflicts": [], "requires_human_review": False, "summary": "无方案可供双轨分析"}

        best_design = designs[0] if designs else {}
        second_design = designs[1] if len(designs) > 1 else {}

        # ── 长径比超出应用推荐范围 ──
        if self.ke:
            specs_app = best_design.get("_application", "")
            domain = self.ke.get_efficient_domain(specs_app) if specs_app else {}
            od_range = domain.get("stator_od_range", [])
            od = best_design.get("stator_od", 0)
            if od_range and od and (od < od_range[0] or od > od_range[1]):
                conflicts.append({
                    "type": "lambda_out_of_domain",
                    "description": f"放大轨推荐方案外径{od}mm，超出{specs_app}高效域{od_range[0]}-{od_range[1]}mm",
                    "severity": "high",
                    "resolution": "收敛轨检测到方案A尺寸偏离应用高效域，决策门应强制人工审阅双报告"
                })

        # ── 报告关键词矛盾检测 ──
        support_lower = (support_report or "").lower()
        oppose_lower = (oppose_report or "").lower()

        # 效率矛盾
        if "效率最高" in support_report and ("效率偏低" in oppose_report or "效率不足" in oppose_report):
            conflicts.append({
                "type": "efficiency_contradiction",
                "description": "放大轨推荐效率最高方案，但收敛轨认为该方案效率偏低",
                "severity": "high",
                "resolution": "需人工核对效率计算方法和前提假设是否一致"
            })

        # 成本矛盾
        if "成本最低" in support_report and ("成本过高" in oppose_report or "全生命周期成本" in oppose_report):
            conflicts.append({
                "type": "cost_contradiction",
                "description": "放大轨推荐成本最低方案，但收敛轨认为全生命周期成本偏高",
                "severity": "medium",
                "resolution": "需统一成本口径(材料成本 vs 全生命周期成本)"
            })

        # 可靠性矛盾
        if "可靠性" in support_report and ("可靠性不足" in oppose_report or "风险" in oppose_report):
            conflicts.append({
                "type": "reliability_contradiction",
                "description": "放大轨认为方案可靠，但收敛轨检测到可靠性风险",
                "severity": "high",
                "resolution": "需人工审核可靠性假设和MTBF数据"
            })

        # ── 物理量合理性检查 ──
        for i, design in enumerate(designs[:3]):
            phys = design.get("_physics", {})
            lam = phys.get("lambda", 0)
            poles = design.get("poles", 4)
            if self.ke and lam:
                lam_range = self.ke.get_lambda_range(poles)
                if lam < lam_range[0] or lam > lam_range[1]:
                    conflicts.append({
                        "type": "lambda_violation",
                        "description": f"方案{i+1}长径比λ={lam}，超出{poles}极推荐范围{lam_range}",
                        "severity": "medium",
                        "resolution": "收敛轨检测到长径比偏离，建议调整铁心长度或定子内径"
                    })

        high_count = sum(1 for c in conflicts if c["severity"] == "high")
        requires_human = high_count > 0

        return {
            "conflicts": conflicts,
            "requires_human_review": requires_human,
            "high_conflict_count": high_count,
            "summary": f"双轨制衡: {len(conflicts)}个矛盾点 ({high_count}个高风险)，" +
                       ("需人工审阅" if requires_human else "可自动决策")
        }

    # ═══════════════════════════════════════════════════════════
    # 3. 合规检查 — 行业强制性标准
    # ═══════════════════════════════════════════════════════════

    def check_compliance(self, specs: dict, design: dict) -> dict:
        """
        行业强制性标准检查

        检查方案是否满足行业合规要求：
          - 船用电机必须CCS认证
          - 矿用电机必须防爆认证
          - 化工电机必须防爆
          - 效率满足IE标准
          - 温升裕度
          - 功率因数

        返回: {
            "compliant": bool,
            "issues": [{"category": str, "requirement": str, "status": str, "message": str}],
            "missing_certifications": list,
            "summary": str
        }
        """
        issues = []
        missing_certs = []

        application = specs.get("application", "")
        if not application:
            application = design.get("application", "")

        # ── 认证要求检查 ──
        if self.ke:
            required_certs = self.ke.get_compliance_requirements(application)
            existing_certs = specs.get("certifications", [])
            if isinstance(existing_certs, str):
                existing_certs = [existing_certs]

            for cert in required_certs:
                if cert not in existing_certs:
                    missing_certs.append(cert)
                    issues.append({
                        "category": "certification",
                        "requirement": cert,
                        "status": "missing",
                        "message": f"请确认是否需要{cert}认证（{application}应用强制要求）"
                    })

        # ── 效率标准检查 ──
        efficiency = design.get("efficiency", 0)
        thresholds = self._design_rules.get("cost_performance_thresholds", {})
        ie_thresholds = thresholds.get("efficiency_min_by_ie", {})

        for ie_level, ie_min in ie_thresholds.items():
            if efficiency < ie_min:
                issues.append({
                    "category": "efficiency",
                    "requirement": f"{ie_level} >= {ie_min}",
                    "status": "below_standard",
                    "message": f"方案效率{efficiency:.1%}低于{ie_level}标准({ie_min:.0%})，不满足{ie_level}能效要求"
                })
                break  # 只报告最低未满足的标准

        # ── 功率因数检查 ──
        pf = design.get("power_factor", 0)
        pf_min = thresholds.get("power_factor_min", 0.80)
        if pf and pf < pf_min:
            issues.append({
                "category": "power_factor",
                "requirement": f">= {pf_min}",
                "status": "below_standard",
                "message": f"方案功率因数{pf:.3f}低于标准{pf_min}，需优化"
            })

        # ── 温升裕度检查 ──
        temp_rise = design.get("temperature_rise", 0)
        insulation_class = (design.get("insulation_class") or specs.get("insulation_class") or "F").upper()
        if self.ke:
            temp_limit = self.ke.get_insulation_temp_limit(insulation_class)
            margin = temp_limit - temp_rise if temp_rise else 0
            required_margin = thresholds.get("temperature_rise_margin_c", 10)
            if temp_rise and margin < required_margin:
                issues.append({
                    "category": "thermal",
                    "requirement": f"温升裕度 >= {required_margin}°C",
                    "status": "insufficient_margin",
                    "message": f"温升{temp_rise}°C，距绝缘{insulation_class}级上限{temp_limit}°C仅{margin}°C裕度，不足{required_margin}°C"
                })

        # ── 海拔降容检查 ──
        altitude = specs.get("altitude_m", 0)
        if altitude and altitude > 1000 and self.ke:
            derating = self.ke._get_altitude_derating(altitude)
            if derating and derating < 1.0:
                # 检查方案是否考虑了降容
                design_eff = design.get("efficiency", 0)
                if design_eff and not design.get("_altitude_adjusted"):
                    issues.append({
                        "category": "altitude",
                        "requirement": f"海拔{altitude}m需降容系数{derating}",
                        "status": "not_adjusted",
                        "message": f"方案未体现海拔{altitude}m的降容修正(系数={derating})，可能高估了实际输出能力"
                    })

        critical_issues = [i for i in issues if i["status"] in ("missing", "below_standard")]
        compliant = len(critical_issues) == 0 and len(missing_certs) == 0

        return {
            "compliant": compliant,
            "issues": issues,
            "missing_certifications": missing_certs,
            "summary": f"合规检查: {'通过' if compliant else '未通过'} ({len(issues)}个问题)"
        }

    # ═══════════════════════════════════════════════════════════
    # 4. 成本性能阈值检查
    # ═══════════════════════════════════════════════════════════

    def check_cost_performance(self, design: dict, quote: float = None,
                                cost: float = None) -> dict:
        """
        成本与性能的合规阈值检查

        检查:
          - 报价低于成本时报警
          - 效率低于IE标准
          - 性价比分析

        返回: {
            "issues": [{"type": str, "message": str, "severity": str}],
            "alerts": list,
            "summary": str
        }
        """
        issues = []

        # ── 报价低于成本报警 ──
        if quote is not None and cost is not None:
            ratio = (cost - quote) / cost if cost > 0 else 0
            warning_ratio = self._design_rules.get(
                "cost_performance_thresholds", {}
            ).get("cost_warning_ratio", 0.15)
            if ratio > warning_ratio:
                issues.append({
                    "type": "below_cost",
                    "message": f"报价{quote}元低于成本{cost}元，亏损比例{ratio:.1%}超过阈值{warning_ratio:.0%}",
                    "severity": "critical"
                })
            elif quote < cost:
                issues.append({
                    "type": "below_cost",
                    "message": f"报价{quote}元低于成本{cost}元，亏损{cost-quote:.0f}元",
                    "severity": "warning"
                })

        # ── 效率IE标准检查 ──
        efficiency = design.get("efficiency", 0)
        if efficiency:
            thresholds = self._design_rules.get("cost_performance_thresholds", {})
            ie_thresholds = thresholds.get("efficiency_min_by_ie", {})
            for ie_level, ie_min in sorted(ie_thresholds.items()):
                if efficiency >= ie_min:
                    issues.append({
                        "type": "efficiency_standard",
                        "message": f"效率{efficiency:.1%}满足{ie_level}标准({ie_min:.0%})",
                        "severity": "info"
                    })
                    break

        # ── 功率因数 ──
        pf = design.get("power_factor", 0)
        pf_min = self._design_rules.get("cost_performance_thresholds", {}).get("power_factor_min", 0.80)
        if pf and pf < pf_min:
            issues.append({
                "type": "power_factor_low",
                "message": f"功率因数{pf:.3f}低于标准{pf_min}",
                "severity": "warning"
            })

        critical = [i for i in issues if i["severity"] == "critical"]
        warnings = [i for i in issues if i["severity"] == "warning"]

        return {
            "issues": issues,
            "alerts": critical,
            "summary": f"成本性能: {len(critical)}报警, {len(warnings)}警告"
        }

    # ═══════════════════════════════════════════════════════════
    # 5. 综合检查 — 一次性执行所有规则
    # ═══════════════════════════════════════════════════════════

    def full_check(self, specs: dict, designs: list,
                    support_report: str = "", oppose_report: str = "",
                    quote: float = None, cost: float = None) -> dict:
        """
        一次性执行所有评审规则

        返回综合评审报告:
        {
            "consistency": {...},
            "dual_track": {...},
            "compliance": {...},
            "cost_performance": {...},
            "overall": {"passed": bool, "require_human": bool, "summary": str},
            "action": "auto_pass" | "human_review" | "reject"
        }
        """
        results = {}

        # 对每个方案做一致性检查
        consistency_results = []
        for design in designs[:5]:  # 最多检查前5个方案
            consistency_results.append(self.check_consistency(specs, design))
        results["consistency"] = consistency_results

        # 双轨制衡
        results["dual_track"] = self.check_dual_track(support_report, oppose_report, designs)

        # 合规检查 (只检查最优方案)
        best_design = designs[0] if designs else {}
        results["compliance"] = self.check_compliance(specs, best_design)

        # 成本性能
        results["cost_performance"] = self.check_cost_performance(best_design, quote, cost)

        # 综合判断
        consistency_fail = any(not r["passed"] for r in consistency_results)
        dual_track_conflict = results["dual_track"]["requires_human_review"]
        compliance_fail = not results["compliance"]["compliant"]
        cost_alert = len(results["cost_performance"]["alerts"]) > 0

        require_human = dual_track_conflict or compliance_fail or cost_alert
        reject = consistency_fail and compliance_fail

        if reject:
            action = "reject"
        elif require_human:
            action = "human_review"
        else:
            action = "auto_pass"

        results["overall"] = {
            "passed": not (consistency_fail or compliance_fail or cost_alert),
            "require_human": require_human,
            "action": action,
            "summary": self._build_summary(results, action)
        }

        return results

    def _build_summary(self, results: dict, action: str) -> str:
        """构建综合摘要"""
        parts = []

        # 一致性
        consistency = results.get("consistency", [])
        if consistency:
            total_critical = sum(r.get("critical_count", 0) for r in consistency)
            total_warning = sum(r.get("warning_count", 0) for r in consistency)
            parts.append(f"一致性: {total_critical}严重/{total_warning}警告")

        # 双轨
        dual = results.get("dual_track", {})
        parts.append(f"双轨: {dual.get('high_conflict_count', 0)}高风险")

        # 合规
        compliance = results.get("compliance", {})
        parts.append(f"合规: {'通过' if compliance.get('compliant') else '未通过'}")

        # 成本
        cost = results.get("cost_performance", {})
        parts.append(f"成本: {len(cost.get('alerts', []))}报警")

        action_text = {"auto_pass": "自动通过", "human_review": "需人工审阅", "reject": "驳回重做"}
        parts.append(f"→ {action_text.get(action, action)}")

        return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

review_engine = ReviewRulesEngine()


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("ReviewRulesEngine 评审规则库自测")
    print("=" * 60)

    re = review_engine

    # ── 测试1: 一致性校验 ──
    print("\n--- 1. 一致性校验 ---")
    specs = {
        "cooling": "水冷",
        "voltage_v": 690,
        "poles": 6,
        "protection_class": "IP56",
        "insulation_class": "H",
        "application": "船舶推进"
    }
    design = {
        "cooling": "IC411",
        "voltage": 690,
        "poles": 4,
        "protection_class": "IP44",
        "insulation_class": "F",
        "stator_od": 400,
        "stator_id": 240,
        "core_length": 300,
        "efficiency": 0.91,
        "power_factor": 0.85
    }
    consistency = re.check_consistency(specs, design)
    print(f"  通过: {consistency['passed']}")
    print(f"  严重: {consistency['critical_count']}, 警告: {consistency['warning_count']}")
    for issue in consistency["issues"]:
        print(f"    [{issue['severity']}] {issue['field']}: {issue['message']}")

    # ── 测试2: 双轨制衡 ──
    print("\n--- 2. 双轨制衡 ---")
    support = "方案A效率最高，推荐方案A。成本最低。可靠性满足要求。"
    oppose = "方案A效率偏低，全生命周期成本过高，可靠性不足，存在风险。"
    designs = [
        {"stator_od": 480, "poles": 6, "core_length": 350, "stator_id": 280,
         "_physics": {"lambda": 1.25}, "_application": "船舶推进"},
        {"stator_od": 420, "poles": 6, "core_length": 280, "stator_id": 250,
         "_physics": {"lambda": 1.12}},
    ]
    dual = re.check_dual_track(support, oppose, designs)
    print(f"  矛盾点: {len(dual['conflicts'])}")
    print(f"  需人工审阅: {dual['requires_human_review']}")
    for c in dual["conflicts"]:
        print(f"    [{c['severity']}] {c['type']}: {c['description'][:50]}")

    # ── 测试3: 合规检查 ──
    print("\n--- 3. 合规检查 ---")
    compliance = re.check_compliance(specs, design)
    print(f"  合规: {compliance['compliant']}")
    print(f"  缺失认证: {compliance['missing_certifications']}")
    for issue in compliance["issues"]:
        print(f"    [{issue['category']}] {issue['status']}: {issue['message'][:50]}")

    # ── 测试4: 成本性能 ──
    print("\n--- 4. 成本性能 ---")
    cost_check = re.check_cost_performance(design, quote=8000, cost=10000)
    print(f"  报警: {len(cost_check['alerts'])}")
    for issue in cost_check["issues"]:
        print(f"    [{issue['severity']}] {issue['type']}: {issue['message'][:50]}")

    # ── 测试5: 综合检查 ──
    print("\n--- 5. 综合检查 ---")
    full = re.full_check(specs, designs, support, oppose, quote=8000, cost=10000)
    print(f"  总体通过: {full['overall']['passed']}")
    print(f"  需人工: {full['overall']['require_human']}")
    print(f"  动作: {full['overall']['action']}")
    print(f"  摘要: {full['overall']['summary']}")

    print(f"\n{'=' * 60}")
    print("自测完成")
