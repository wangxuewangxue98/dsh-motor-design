# -*- coding: utf-8 -*-
"""
方案构思师智能体 v4.01.038 (P0-3 / 改进资料 3.1)

在「基准参数选定」之后、「方案生成(物理内核求解)」之前插入的 LLM 构思节点:
  - 基于需求规格 + 基准参数, 提出方案构思方向 (拓扑/尺寸/材料/冷却/绕组)
  - 以案例电机 (case_motor) 作为种子约束, 使构思扎根于已验证设计
  - 质量门禁自动重生时 (guidance 非空), 针对门禁未通过原因给出差异化构思

输出:
  {
    "ideation": str,        # 中文构思叙述
    "design_hints": dict,   # 结构化构思约束 {emphasis, avoid, notes, suggested_constraints}
    "used_cloud": bool,
    "error": str|None,
  }
"""
import json
import os
import re
import sys
from typing import Optional

from dotenv import load_dotenv

load_dotenv(r"D:\MotorDesign\.env", override=False)

sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Core")

from base_agent import BaseAgent

try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")


class IdeationAgent(BaseAgent):
    """方案构思师 - LLM 方案构思 + 案例种子约束"""

    def __init__(self):
        super().__init__(
            name="IdeationAgent",
            description="基于需求与案例种子进行方案构思, 指导后续物理内核求解"
        )
        # v4.00.006: 模型每次调用动态读取, 支持运行时切换
        self.llm = None

    def _get_llm(self):
        """每次调用动态创建 LLM, 管理端改模型后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        try:
            return super()._get_llm(temperature=0.6)
        except Exception as e:
            print(f"[IdeationAgent] LLM 创建失败: {e}")
            return None

    # ─── 提示词构建 ───
    def _build_prompt(self, specs: dict, base_params: dict,
                      case_motor: Optional[dict], guidance: str) -> str:
        # 需求关键字段 (英文)
        _keys = ["motor_type", "power_kw", "voltage_v", "speed_rpm", "poles",
                 "frequency_hz", "torque_nm", "efficiency", "cooling_method", "ambient_temp_c"]
        req_lines = []
        for k in _keys:
            v = specs.get(k)
            if v not in (None, ""):
                req_lines.append(f"  - {k}: {v}")
        req_block = "\n".join(req_lines) if req_lines else "  (无)"

        # 基准参数 (中文带单位) 摘要
        bp_lines = []
        for k, v in (base_params or {}).items():
            if isinstance(v, (int, float)) or (isinstance(v, str) and v):
                bp_lines.append(f"  - {k}: {v}")
        bp_block = "\n".join(bp_lines[:20]) if bp_lines else "  (无)"

        # 案例种子
        if case_motor:
            cm_lines = [f"  - 案例编号: {case_motor.get('case_code', '?')}",
                        f"  - 电机类型: {case_motor.get('motor_type', '?')}"]
            for k in ("rated_power", "poles", "stator_od", "core_length", "efficiency", "max_temp"):
                val = case_motor.get(k)
                if val not in (None, ""):
                    cm_lines.append(f"  - {k}: {val}")
            case_block = "\n".join(cm_lines)
        else:
            case_block = "  (无可用案例电机, 仅依据通用设计经验)"

        guidance_block = ""
        if guidance:
            guidance_block = (
                "\n\n## 质量门禁改进指引 (本轮为自动重生, 必须针对性规避以下问题)\n"
                f"{guidance}\n"
                "请提出与上次明显不同的差异化构思方向, 重点解决上述门禁未通过项。"
            )

        # v4.02.13: 计算电机类型与基准几何参考, 用于硬性约束与示例区间
        motor_type = specs.get("motor_type") or "未知"
        _bp_od = (base_params.get("stator_od") or base_params.get("定子外径")
                  or base_params.get("定子外径(mm)"))
        _bp_l = (base_params.get("core_length") or base_params.get("铁心长度")
                 or base_params.get("core_length_mm"))
        try:
            _od = float(_bp_od) if _bp_od not in (None, "") else None
        except (TypeError, ValueError):
            _od = None
        try:
            _l = float(_bp_l) if _bp_l not in (None, "") else None
        except (TypeError, ValueError):
            _l = None
        if _od:
            _sc_ex = {"定子外径(mm)": [round(_od * 0.9, 1), round(_od * 1.1, 1)]}
            if _l:
                _sc_ex["铁心长度(mm)"] = [round(_l * 0.9, 1), round(_l * 1.1, 1)]
            example_json = json.dumps(_sc_ex, ensure_ascii=False)
            _ex_inner = json.dumps({'定子外径(mm)': [_od * 0.9, _od * 1.1]}, ensure_ascii=False)
            example_block = (
                f"  示例(基于基准定子外径 {_od:.0f}mm 推算的 ±10% 区间, 严禁凭空捏造): "
                f"{_ex_inner}"
            )
        else:
            example_json = json.dumps(
                {"定子外径(mm)": ["基准*0.9", "基准*1.1"],
                 "铁心长度(mm)": ["基准*0.8", "基准*1.2"]}, ensure_ascii=False)
            example_block = ("  示例(必须基于下方\"已选定的基准参数\"推算, 禁止凭空捏造; "
                            "例如围绕基准定子外径 ±10%)")

        prompt = f"""你是一名资深电机电磁设计总师。请基于以下信息, 进行方案构思 (Scheme Conception),
为后续的参数化求解提供方向性指导。

## 一、客户需求规格
{req_block}

## 二、已选定的基准参数
{bp_block}

## 三、案例电机种子 (优先借鉴其成功结构约束)
{case_block}

## 重要约束 (硬性, 必须严格遵守)
- 本次电机类型: {motor_type}
- **拓扑/材料必须与电机类型兼容**: 异步电动机只能采用异步拓扑, 禁止提出"双馈""永磁""磁阻同步"等不兼容拓扑; 永磁同步电机禁止提出"异步/感应/双馈"拓扑。
- **suggested_constraints 必须基于下方"已选定的基准参数"推算 (±10% 偏差上限), 禁止凭空捏造区间**; 若基准无该参数, 不得给出该项约束。
- 每条构思方向必须标注: 兼容性=[与{motor_type}兼容 / 不兼容-已剔除] 与 约束来源=[案例电机 / 需求规格 / 设计经验]。
{guidance_block}

## 四、输出要求
1. 用中文写出 3~5 条方案构思方向, 覆盖: 拓扑/极数配合、关键尺寸(定子外径/铁心长/气隙)、
   材料(硅钢/永磁视电机类型而定)、冷却方式、绕组形式。每条说明物理依据, 并标注兼容性与约束来源。
2. 若利用了案例电机种子, 明确说明借鉴了哪些已验证结构。
3. 最后用如下 JSON 代码块给出结构化构思约束 (供求解器参考):
   - emphasis: 列表, 你最想强化的 1~3 个方向 (如 转矩/温升/效率/槽满率/轻量化)
   - avoid:    列表, 需要规避的 1~3 个风险 (如 过深槽漏磁/过高电密/过长铁心)
   - suggested_constraints: 对象, **必须给出 1~3 项量化区间约束**, 键为参数中文名(含单位),
                          值为 [下限, 上限] 数值, 且必须落在基准参数 ±10% 内; 禁止留空对象 {{}}
{example_block}
   - notes:    字符串, 补充说明借鉴了案例电机的哪些已验证结构
```json
{{
  "emphasis": ["提高转矩", "降低温升"],
  "avoid": ["避免过长铁心导致机座超限"],
  "suggested_constraints": {example_json},
  "notes": "借鉴案例电机的短铁心高叠长比结构"
}}
```
请直接输出, 不要使用前导客套语。"""
        return prompt

    # ─── 解析 ───
    @staticmethod
    def _parse_hints(text: str) -> dict:
        """从 LLM 输出提取 design_hints JSON; 失败返回空 dict
        v4.04 (#10): 改用 json.JSONDecoder.raw_decode 逐层扫描, 支持嵌套花括号
        """
        if not text:
            return {}
        _HINT_KEYS = ("emphasis", "avoid", "suggested_constraints", "notes",
                      "focus", "priority")
        decoder = json.JSONDecoder()

        def _try_raw(s: str):
            # 从首个 '{' 起逐字节解析首个完整 JSON 对象 (支持嵌套)
            start = s.find("{")
            if start < 0:
                return None
            try:
                obj, _ = decoder.raw_decode(s[start:])
            except Exception:
                return None
            if isinstance(obj, dict) and any(k in obj for k in _HINT_KEYS):
                return obj
            return None

        try:
            # 优先: ```json ... ``` 代码块
            m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
            if m:
                obj = _try_raw(m.group(1))
                if obj is not None:
                    return obj
            # 回退: 全文扫描首个完整 JSON 对象
            obj = _try_raw(text)
            if obj is not None:
                return obj
        except Exception:
            pass
        return {}

    # ─── 后处理校验 (v4.02.13: 遏制方案构思师幻觉) ───
    # 电机类型 → 禁止出现的冲突拓扑关键词
    _FORBIDDEN_TOPICS = {
        "异步电动机": ["双馈", "永磁", "磁阻同步", "开关磁阻", "同步磁阻", "永磁体"],
        "永磁同步": ["异步", "感应", "双馈"],
        "同步": ["异步", "感应", "双馈"],
        "开关磁阻": ["永磁", "双馈", "异步", "感应"],
    }
    # suggested_constraints 中文标签 → 基准参数候选键 (英文/中文均试)
    _LABEL_TO_BP_KEY = {
        "定子外径(mm)": ("stator_od", "定子外径", "定子外径(mm)"),
        "定子内径(mm)": ("stator_id", "定子内径", "定子内径(mm)"),
        "转子外径(mm)": ("rotor_od", "转子外径", "转子外径(mm)"),
        "铁心长度(mm)": ("core_length", "铁心长度", "core_length_mm", "铁心长"),
        "气隙长度(mm)": ("air_gap", "气隙", "气隙长度", "气隙长度(mm)"),
        "绕组匝数": ("turns_per_coil", "匝数", "turns"),
    }

    @classmethod
    def _forbidden_for(cls, motor_type: str):
        """返回该电机类型禁止出现的拓扑关键词列表。"""
        if not motor_type:
            return []
        mt = str(motor_type)
        _for = []
        for k, topics in cls._FORBIDDEN_TOPICS.items():
            if k in mt or mt in k:
                _for.extend(topics)
        if "异步" in mt:  # 通用兜底: 异步类电机一律禁双馈/永磁/磁阻
            for t in ("双馈", "永磁", "磁阻同步", "开关磁阻"):
                if t not in _for:
                    _for.append(t)
        return _for

    @classmethod
    def _validate_constraints(cls, hints: dict, base_params: dict, warnings: list):
        """校验 suggested_constraints 区间合理性: 严重偏离基准(>2×或<0.5×)则钳制并报警。"""
        if not isinstance(hints, dict) or not isinstance(warnings, list):
            return hints
        sc = hints.get("suggested_constraints")
        if not isinstance(sc, dict):
            return hints
        bp = base_params or {}
        for label, rng in sc.items():
            cand = cls._LABEL_TO_BP_KEY.get(label)
            base_val = None
            if cand:
                for ck in cand:
                    v = bp.get(ck)
                    if isinstance(v, (int, float)) and v > 0:
                        base_val = float(v)
                        break
                    if isinstance(v, dict) and v.get("base") is not None:
                        try:
                            base_val = float(v["base"])
                            break
                        except (TypeError, ValueError):
                            pass
            if base_val is None:
                continue  # 基准无该项, 不强行校验
            if isinstance(rng, (list, tuple)) and len(rng) >= 2:
                try:
                    lo, hi = float(rng[0]), float(rng[1])
                except (TypeError, ValueError):
                    warnings.append(f"suggested_constraints[{label}] 区间非法, 已忽略")
                    continue
                mid = (lo + hi) / 2.0
                if mid < base_val * 0.5 or lo > base_val * 2.0 or hi < base_val * 0.2:
                    sc[label] = [round(base_val * 0.9, 3), round(base_val * 1.1, 3)]
                    warnings.append(
                        f"suggested_constraints[{label}] 范围[{lo},{hi}]严重偏离基准{base_val:.1f}, 已钳制至±10%")
        return hints

    @classmethod
    def _validate_topology(cls, ideation_text: str, motor_type: str, warnings: list):
        """校验构思叙述是否含与电机类型冲突的拓扑建议 (幻觉)。"""
        if not ideation_text or not isinstance(warnings, list):
            return
        forbidden = cls._forbidden_for(motor_type)
        if not forbidden:
            return
        for topic in forbidden:
            if topic in ideation_text:
                warnings.append(
                    f"构思叙述包含与[{motor_type}]冲突的拓扑建议: “{topic}”, 已标记待复核")

    def run(self, specs: dict, base_params: dict, case_motor: Optional[dict] = None,
            guidance: str = "", use_cloud: bool = False, **kwargs) -> dict:
        specs = specs or {}
        base_params = base_params or {}
        prompt = self._build_prompt(specs, base_params, case_motor, guidance)

        # v4.02.99 B: 原 self._get_llm() 硬绑定本地 ChatOllama (不经网关/不查云路由),
        # 且 agent_model_map 原无 IdeationAgent 键 → 恒走 qwen2.5:3b。
        # 改走统一入口: 配置为云端 → _GatewayCloudLLM (云端优先, 失败自动降级本地)。
        llm = self._get_llm_or_cloud(temperature=0.6)
        if llm is None:
            return {"ideation": "LLM 不可用, 跳过方案构思",
                    "design_hints": {}, "used_cloud": False,
                    "error": "llm_unavailable"}

        try:
            resp = llm.invoke(prompt)
            text = getattr(resp, "content", str(resp))
        except Exception as e:
            return {"ideation": f"方案构思调用失败: {e}",
                    "design_hints": {}, "used_cloud": False, "error": str(e)}

        hints = self._parse_hints(text)
        # v4.02.13: 后处理校验 — 遏制拓扑/参数范围幻觉
        _warnings = []
        hints = self._validate_constraints(hints, base_params, _warnings)
        self._validate_topology(text, specs.get("motor_type", ""), _warnings)
        # v4.02.99 B: used_cloud 原取入参 use_cloud (orchestrator 未传 → 恒 False),
        # 不反映实际路由; 改为取网关实际结果 (云端成功 went_cloud=True, 降级本地则 False)。
        _actually_cloud = bool(getattr(llm, "went_cloud", False))
        return {
            "ideation": text,
            "design_hints": hints,
            "used_cloud": _actually_cloud,
            "error": None,
            "validation_warnings": _warnings,
        }
