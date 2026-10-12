# -*- coding: utf-8 -*-
"""
需求分析师智能体 v2.04 — 分层类比选型法
==========================================
大脑: 本地 qwen3:4b-instruct (T600 4GB GPU 显存硬约束内最强可用模型, 固定不升级)
      v4.01.052: 引入「正则预提取(Hybrid)」在 LLM 之前捕获高置信固定格式字段(来源: 正则识别)

分层提取:
  L1 粗选层: power_kw, voltage_v, speed_rpm, torque_nm, poles, cooling, motor_type
  L2 细调层: duty_cycle, overload_pct, altitude_m, protection_class, ambient_temp_c, frequency_hz, efficiency
  L3 微调层: mounting, insulation_class, shaft_config, bearing_type, application

场景识别:
  - 型谱模式 (catalog): 标准工况, 可直接查 Y 系列型谱
  - 类比模式 (analogy): 非标工况, 需 D²L 推算

v4.01.038 需求复核节点 (Step 3b, run() 内):
  触发条件: L1核心参数(power/voltage/speed/torque/poles/motor_type)缺失
            OR 整体置信度<0.7 OR 文本超长(>300字)/含歧义表述
  执行: 1)云端大模型(DeepSeek)优先/本地模型兜底做二次提取填补缺失
        2)重跑共享知识引擎推导(infer_all_params)补齐可计算参数
        3)行业惯例库(get_industry_defaults)填充默认值
        4)高置信度合并(云端0.92/本地0.80 vs 初次0.85/推导0.60/默认0.40), 标记来源(标签"AI复核")
        5)二次识别后仍缺失的 L1 参数写入 _pending_questions, 流入人工检查点强制追问, 不可静默放行
"""
import json
import os
import sys
import re
from typing import Optional
from dotenv import load_dotenv
import logging
logger = logging.getLogger(__name__)
from langchain_ollama import ChatOllama

load_dotenv(r"D:\MotorDesign\.env", override=False)

sys.path.insert(0, r"D:\MotorDesign\Scripts")
sys.path.insert(0, r"D:\MotorDesign\Core")

from base_agent import BaseAgent, extract_ollama_usage, _CloudLLMAdapter
try:
    from workflow_config import get_agent_model
except Exception:
    get_agent_model = lambda name: os.getenv("LOCAL_MODEL_NAME", "qwen2.5:3b-instruct-q4_K_M")
try:
    from cloud_llm import CloudLLMUnavailable
except Exception:
    # 兜底: cloud_llm 为必需核心依赖, 正常不会走到; 退化为通用异常类使降级分支仍可捕获
    CloudLLMUnavailable = Exception
from physics_kernel import compute_torque, determine_scenario, find_catalog_by_frame

# v4.01: 加载共享知识引擎 (三层架构·第一层)
from shared_knowledge import knowledge_engine

# v2.05.02: 加载飞轮追问规则
_FLYWHEEL_RULES_PATH = r"D:\MotorDesign\Knowledge\forced_questions.json"


def _load_forced_questions() -> list:
    """加载飞轮生成的强制追问规则"""
    try:
        import json
        if os.path.exists(_FLYWHEEL_RULES_PATH):
            with open(_FLYWHEEL_RULES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("rules", [])
    except Exception:
        pass
    return []


class RequirementAgent(BaseAgent):
    """需求分析师 - 分层提取 + 场景识别"""

    def __init__(self):
        super().__init__(
            name="RequirementAgent",
            description="分层类比选型法: L1粗选+L2细调+L3微调, 场景识别(型谱/类比)"
        )
        # v4.00.006: 模型改为每次调用时动态读取, 支持运行时切换
        self.llm = None

    def _get_llm(self):
        """每次调用时动态创建 LLM, 管理端修改模型配置后立即生效 (v5.1 委托 BaseAgent 集中治理 num_ctx/keep_alive)"""
        try:
            return super()._get_llm(temperature=0.1)
        except Exception as e:
            logger.warning("[RequirementAgent] LLM 创建失败: %s", e)
            return None

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
            "extraction_notes": list, # 提取过程中的补全/推断记录
            "pipeline_steps": list    # v4.01.051: 8步流水线过程交付物
        }
        """
        # v4.00.01: 支持审核意见作为补充上下文
        review_comment = kwargs.get("review_comment", "")

        # v4.01.051: 8 步流水线过程交付物
        pipeline_steps = []
        _notes_marker = 0  # 跟踪 notes 增量, 提取每步新增说明
        # v4.06-P1: 累计真实 token 用量 (主提取 + 二次复核), 供成本审计全链路真实化
        _tot_ti = 0
        _tot_to = 0

        # ── Step 0(预): v4.01.052 正则预提取 — 在 LLM 之前, 捕获高置信固定格式字段
        regex_prior = self._extract_freetext_fields(raw_text)

        # ── Step 1: LLM 三层提取 ──
        # v4.00.006: 每次调用时动态获取 LLM (支持管理端运行时切换模型)
        # v4.02.07 B方案: 主提取走云端感知入口 _get_llm_or_cloud —— 当 agent_model_map 配
        #           "cloud:" 前缀时走云端 (经 DeepSeek 卡代理 → Agnes 端点 → agnes-3.0-flash),
        #           否则走本地 Ollama。云端调用失败 (CloudLLMUnavailable / 连接重置 / 超时)
        #           自动降级本地有效默认模型, 不中断需求解析工作流。
        llm = self._get_llm_or_cloud(temperature=0.1)
        if llm is None:
            return {"error": "LLM 不可用"}
        _went_cloud = getattr(llm, "went_cloud", False)
        prompt = self._build_extraction_prompt(raw_text, review_comment, regex_prior)
        try:
            response = llm.invoke(prompt)
        except CloudLLMUnavailable as _ce:
            logger.warning("[RequirementAgent] 云端主提取不可用, 降级本地 Ollama: %s", _ce)
            llm = self._get_llm()
            if llm is None:
                return {"error": "LLM 不可用 (云端降级本地失败)"}
            _went_cloud = False
            response = llm.invoke(prompt)
        except Exception as _e:
            if _went_cloud:
                logger.warning("[RequirementAgent] 云端主提取异常(%s), 降级本地 Ollama",
                               type(_e).__name__)
                _local = self._get_llm()
                if _local is not None:
                    try:
                        response = _local.invoke(prompt)
                        _went_cloud = False
                    except Exception as _e2:
                        return {"error": f"LLM 调用失败 (云端降级本地亦失败): {_e2}"}
                else:
                    return {"error": "LLM 不可用 (云端降级本地失败)"}
            else:
                raise
        content = response.content.strip()
        content = response.content.strip()
        # v4.06-P1: 捕获主提取真实 token 用量 (ChatOllama usage_metadata), 供成本审计全链路真实化
        _ti_main, _to_main = extract_ollama_usage(response, prompt)
        _tot_ti += _ti_main
        _tot_to += _to_main

        # 清理 markdown 包裹
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])

        extracted = self._robust_json_parse(content)

        pipeline_steps.append({
            "step": 1,
            "name": "LLM三层提取",
            "description": "大模型从用户文本分层提取 L1(粗选)/L2(细调)/L3(微调) 参数",
            "raw_text": raw_text,
            "specs": self._snapshot_specs_raw(extracted),
            "notes_added": [],
        })

        # ── Step 2: 分层归档 + 补全 ──
        notes = []
        layered = self._organize_layers(extracted, notes)

        # v4.02.17 P1-1: 识别驱动方式(工频/变频), 供 _calc_frequency 频率圆整使用
        _drive_type = self._detect_drive_type(raw_text, layered.get("L1", {}).get("motor_type", ""))
        layered["_drive_type"] = _drive_type

        # v4.06-P1: 捕获二次复核真实 token 用量并累加
        _rev_ti, _rev_to = self._review_specs(raw_text, layered, notes) or (0, 0)
        _tot_ti += _rev_ti
        _tot_to += _rev_to

        # ── Step 2(预): v4.01.052 正则先验写入 — 以"正则识别"来源填补 LLM 漏提字段
        self._apply_regex_prior(layered, regex_prior, notes)

        # ── Step 2(补): v4.01.039 文本正则补充 — 仅填补 LLM 漏提的字段
        #   (如"安装空间外径限制250mm"), 不覆盖 LLM 已给出的字段 ──
        self._supplement_from_text(layered, raw_text, notes)

        # ── Step 2a: v2.05.06 中心高提取 + 型谱查询 ──
        self._extract_center_height_and_query_catalog(raw_text, layered, notes)

        # ── Step 2b: v4.01.051 已有案例电机查询 Top 3 ──
        self._query_case_motor_matches(layered, notes)

        pipeline_steps.append({
            "step": 2,
            "name": "分层归档+文本正则补充+中心高型谱+案例电机查询",
            "description": "归档三层结构, 正则补充LLM遗漏字段, 查询Y系列型谱与相似案例电机Top3",
            "specs": self._snapshot_specs(layered),
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # ── Step 3: 物理补全 (转矩/极数/频率) ──
        self._physics_complete(layered, notes)

        pipeline_steps.append({
            "step": 3,
            "name": "物理补全(转矩/极数/频率)",
            "description": "T=9550P/n 计算转矩, 标准同步转速反推极数, f=p×n/60 计算频率",
            "specs": self._snapshot_specs(layered),
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # ── Step 4: v4.01 共享知识引擎补全 (频率/行业默认/环境修正) ──
        self._shared_knowledge_complete(layered, notes)

        pipeline_steps.append({
            "step": 4,
            "name": "共享知识引擎补全",
            "description": "行业默认值/海拔温度降容/效率功率因数查表/电压电流互推",
            "specs": self._snapshot_specs(layered),
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # ── Step 5: v4.01.038 需求复核节点 (二次识别: 缺失/LowConf/歧义触发) ──
        # v4.06-P1: 二次提取 token 已在 Step 2 处捕获并累加 (此处不再重复调用)

        pipeline_steps.append({
            "step": 5,
            "name": "需求复核(二次识别)",
            "description": "L1缺失/低置信度/歧义触发, 云端优先本地兜底二次提取, 高置信度合并",
            "specs": self._snapshot_specs(layered),
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # ── Step 6: v4.01.039 飞轮阻断追问 — 必须在二次识别之后执行,
        #            以减小"系统没识别出来"的误报; 仅 L1 缺失才阻断流程 ──
        self._apply_forced_questions(raw_text, layered, notes)

        pipeline_steps.append({
            "step": 6,
            "name": "飞轮强制追问",
            "description": "forced_questions.json规则检测关键词, L1缺失阻断流程",
            "specs": self._snapshot_specs(layered),
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # ── Step 7: 场景识别 + 置信度评估 ──
        # v4.01.053: 合并前统一收敛数值字段类型 (源头根治 LLM 返回 str 导致的 '>' 比较抛错)
        self._coerce_numeric_fields(layered)
        merged_specs = {**layered["L1"], **layered["L2"], **layered["L3"]}
        scenario_info = determine_scenario(merged_specs)
        confidence = self._assess_confidence(layered, notes)
        param_sources = self._build_param_sources(layered)

        pipeline_steps.append({
            "step": 7,
            "name": "场景识别+置信度评估",
            "description": "型谱/类比模式识别, L1(0.8)+L2(0.12)+L3(0.08)权重+一致性惩罚",
            "specs": self._snapshot_specs(layered),
            "scenario": scenario_info.get("scenario", ""),
            "confidence": confidence,
            "notes_added": list(notes[_notes_marker:]),
        })
        _notes_marker = len(notes)

        # v4.01.030: layered_specs 根级含三个 set (_extracted_keys/_inferred_keys/
        # _defaulted_keys), LangGraph checkpointer 序列化 workflow state 为 JSON 时会
        # 抛 "Object of type set is not JSON serializable"。返回前统一转为有序列表。
        for _kk in ("_extracted_keys", "_inferred_keys", "_defaulted_keys", "_reviewed_keys",
                    "_regex_keys", "_physics_keys"):
            _v = layered.get(_kk)
            if isinstance(_v, set):
                layered[_kk] = sorted(_v)
            elif _v is None:
                layered[_kk] = []

        # ── Step 8: 最终需求结果 (完整规格) ──
        pipeline_steps.append({
            "step": 8,
            "name": "最终需求结果(完整规格)",
            "description": "合并L1+L2+L3, 输出完整需求规格参数",
            "specs": merged_specs,
            "scenario": scenario_info.get("scenario", ""),
            "confidence": confidence,
            "param_sources": param_sources,
        })

        return {
            "specs": merged_specs,
            "layered_specs": layered,
            "scenario_info": scenario_info,
            "raw_text": raw_text,
            "confidence": confidence,
            "extraction_notes": notes,
            "param_sources": param_sources,
            "pipeline_steps": pipeline_steps,
            # v4.06-P1: 真实 token 计量 (主提取 + 二次复核), 供成本审计全链路真实化
            "tokens_in": _tot_ti,
            "tokens_out": _tot_to,
        }

    @staticmethod
    def _snapshot_specs(layered: dict) -> dict:
        """v4.01.051: 展平 layered 的 L1+L2+L3 为一维 dict (过滤 _ 开头内部键)"""
        flat = {}
        for layer in ("L1", "L2", "L3"):
            src = layered.get(layer, {})
            if isinstance(src, dict):
                for k, v in src.items():
                    if k.startswith("_"):
                        continue
                    flat[k] = v
        return flat

    @staticmethod
    def _snapshot_specs_raw(extracted: dict) -> dict:
        """v4.01.051: Step 1 专用 — 展平 LLM 原始输出 (可能含 null 值)"""
        flat = {}
        for layer in ("L1", "L2", "L3"):
            src = extracted.get(layer, {})
            if isinstance(src, dict):
                for k, v in src.items():
                    if k.startswith("_"):
                        continue
                    flat[k] = v
        return flat

    def _build_extraction_prompt(self, raw_text: str, review_comment: str = "",
                                 regex_prior: dict = None) -> str:
        """构建三层提取的 LLM 提示词 (v4.01.052: 支持正则先验注入)"""
        # v4.00.01: 如果有审核意见, 在提示词中增加补充说明
        review_section = ""
        if review_comment and review_comment.strip():
            review_section = f"""

【审核补充意见】（请优先考虑以下审核意见,将其融入需求提取中）：
{review_comment}
"""
        # v4.01.052: 正则预提取结果作为"已知条件"注入, 约束模型不重复劳动、减少幻觉
        regex_block = ""
        if regex_prior:
            items = ", ".join(f"{k}={v}" for k, v in regex_prior.items())
            regex_block = f"""
【系统已用正则预提取以下高置信数值(请不要修改, 只补全其余缺失项)】
{items}
"""
        few_shot = """
【示例】
输入："设计一台Y2系列三相异步电机，额定功率15kW，380V供电，50Hz，4极（1460rpm），风冷，B级绝缘，IP55防护，效率达92%"
输出：{"L1":{"motor_type":"异步电动机","power_kw":15,"voltage_v":380,"speed_rpm":1460,"poles":4,"cooling":"风冷"},"L2":{"duty_cycle":"未知","frequency_hz":50,"protection_class":"IP55","efficiency":0.92},"L3":{"insulation_class":"B"}}
说明：该输入未提及任何工作制关键词(断续运行/周期工作/S3/S4/连续/短时/周期/工作制), 故 duty_cycle 严格返回"未知", 严禁推测为 S1/S3 等。
"""
        return f"""你是一个电机需求分析助手。请从以下客户描述中分层提取关键信息，只返回严格JSON，不要任何解释或markdown标记。
请先逐句扫描文本中的数字与单位, 再逐项填入对应字段。

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
        "frequency_hz": 数字或null（电源频率Hz）,
        "efficiency": 数字或null（效率η，如"效率达92%"→0.92，百分数统一转为小数）
    }},
    "L3": {{
        "mounting": "B3(底脚)/B5(法兰)/B35(底脚+法兰)/未知",
        "insulation_class": "B/F/H/未知",
        "shaft_config": "单轴伸/双轴伸/未知",
        "bearing_type": "深沟球/圆柱滚子/未知",
        "application": "应用场景（如叉车/无人机/电动汽车/工业泵/家电/通用）",
        "outer_diameter_limit_mm": "安装空间外径限制(如250mm, 无则null)"
    }}
}}

提取规则：
- L1层为粗选层，必须提取（power_kw、voltage_v、poles、speed_rpm、efficiency 若文本含相关表述则必须提取, 不得留null）
- L2层为细调层，影响选型修正，尽量提取
- L3层为微调层，有则提取，无则填null或"未知"
- 包含"左右""大约""不低于""以上"等词的数字也要提取
- 字段别名词典: 极数/极对数/p；转速/额定转速/rpm/转/分/r-min；效率/η/能效
- poles: 识别"X极"（如"4极"→4）、"极对数p="、"2p"、"6P"
- speed_rpm: 识别"（1460rpm）""1460转""n=1460""1460r/min"；注意数字常在"rpm/转"之前
- efficiency: 识别"效率达92%""η=0.92""效率92"→统一小数0.92（百分数÷100）
- motor_type: 如果客户未明确说明电机类型，留空(null)，不要默认填"异步电动机"；系统将强制要求人工确认电机类型
- 如果客户给出转矩但没给功率，torque_nm填数字，power_kw填null
- 如果客户给出功率和转速但没给转矩，torque_nm填null（系统会自动计算）
- protection_class: 如果客户提到"防护等级IPXX"则提取
- duty_cycle: 客户提到"断续运行""周期工作"可推断S3, 提到"连续运行"/"S1"可推断S1
- duty_cycle 硬约束: 若文本未提及'断续运行/周期工作/S3/S4/连续/短时/周期/工作制'任一关键词，duty_cycle 必须严格返回 '未知'，严禁推测；不得因"Y2系列"等型号背景假定任何工作制 (S1/S3 等均不可默认)
- ed_pct: 仅当 duty_cycle 为 S3/S4 且文本给出负载持续率(如"S3-40%")时才提取, 否则 null
- altitude_m: 如果客户提到"高原""海拔XXXX米"则提取
- frequency_hz: 如果客户提到"60Hz""400Hz"等则提取，默认50Hz
{few_shot}
{regex_block}
客户描述：{raw_text}{review_section}
JSON:"""

    @staticmethod
    def _repair_json(text: str) -> str:
        """
        修复 LLM 输出中常见的 JSON 语法错误:
        1. 缺少逗号 (key:value 之间)
        2. 尾随逗号 (} 或 ] 前)
        3. 单引号 → 双引号
        4. 注释 (// 和 /* */)
        5. 键缺少引号
        """
        # 去除注释
        text = re.sub(r'//.*?$', '', text, flags=re.MULTILINE)
        text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)

        # 单引号 → 双引号 (值后面的引号)
        text = re.sub(r"'([^']*)'(?=\s*[:,\]}])", r'"\1"', text)
        # 单引号 → 双引号 (键/值前面的引号, 用捕获组避免变宽lookbehind)
        text = re.sub(r"([:,\[{]\s*)'([^']*)'", r'\1"\2"', text)

        # 修复无引号键: { motor_type: "异步" → { "motor_type": "异步"
        text = re.sub(r'([{,])\s*(\w+)\s*:', r'\1 "\2":', text)

        # 修复缺少逗号: "value"\n  "key" → "value",\n  "key"
        # 匹配: 引号值/数字/null/布尔 后面紧跟换行和空白再到引号
        text = re.sub(
            r'(["\d\w\]])\s*\n(\s*["\w])',
            r'\1,\n\2', text
        )

        # 修复尾随逗号: ,} 或 ,]
        text = re.sub(r',\s*([}\]])', r'\1', text)

        return text

    def _robust_json_parse(self, content: str) -> dict:
        """
        多级容错 JSON 解析:
        1. 直接 json.loads
        2. 正则提取 {} 块后 json.loads
        3. 修复后 json.loads
        4. 修复+正则提取后 json.loads
        5. 终极兜底: 正则逐字段提取
        """
        # Step 1: 直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Step 2: 正则提取 JSON 块
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            json_str = match.group()
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass

        # Step 3: 修复后解析
        repaired = self._repair_json(content)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        # Step 4: 修复+正则提取
        match2 = re.search(r'\{.*\}', repaired, re.DOTALL)
        if match2:
            try:
                return json.loads(match2.group())
            except json.JSONDecodeError:
                pass

        # Step 5: 终极兜底 — 正则逐字段提取
        fallback = self._extract_fields_regex(content)
        if fallback:
            return fallback

        raise ValueError(f"无法解析为JSON (已尝试5种修复): {content[:300]}")

    @staticmethod
    def _extract_fields_regex(content: str) -> dict:
        """
        终极兜底: 用正则从 LLM 输出中逐字段提取电机参数
        能提取的: motor_type, power_kw, voltage_v, speed_rpm, torque_nm, poles, cooling
                  duty_cycle, protection_class, ambient_temp_c, frequency_hz, altitude_m
                  mounting, insulation_class, shaft_config, application
        """
        # 字段名 → (正则模式, 类型)
        field_map = {
            # L1
            "motor_type": (r'(?:motor_type|电机类型|类型)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            "power_kw": (r'(?:power_kw|功率|功率kw)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            "voltage_v": (r'(?:voltage_v|电压|电压v)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            "speed_rpm": (r'(?:speed_rpm|转速|rpm)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            "torque_nm": (r'(?:torque_nm|转矩|扭矩)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            "poles": (r'(?:poles|极数|极对数)["\']?\s*[:=]?\s*["\']?(\d+)', int),
            "cooling": (r'(?:cooling|冷却|散热)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            # L2
            "duty_cycle": (r'(?:duty_cycle|工作制|负载特性)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            "protection_class": (r'(?:protection_class|防护等级|防护)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            "ambient_temp_c": (r'(?:ambient_temp_c|环境温度|温度)["\']?\s*[:=]\s*["\']?(-?[\d.]+)', float),
            "frequency_hz": (r'(?:frequency_hz|频率|hz)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            "altitude_m": (r'(?:altitude_m|海拔)["\']?\s*[:=]\s*["\']?([\d.]+)', float),
            # L3
            "mounting": (r'(?:mounting|安装方式|安装形式)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            "insulation_class": (r'(?:insulation_class|绝缘等级|绝缘)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            "application": (r'(?:application|应用|用途)["\']?\s*[:=]\s*["\']?([^"\',\}\n]+)', str),
            # v4.01.039: 安装空间外径限制(如"安装空间外径限制250mm")识别为约束, 不误判为安装方式
            "outer_diameter_limit_mm": (r'(?:外径|外圆直径|最大外径|外直径|直径限制|外尺寸)\s*[:：=]?\s*(?:限制|不超过|≤|不大于|最大|约)?\s*["\']?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米|㎜)?', float),
        }

        result = {"L1": {}, "L2": {}, "L3": {}}
        l1_keys = {"motor_type", "power_kw", "voltage_v", "speed_rpm", "torque_nm", "poles", "cooling"}
        l2_keys = {"duty_cycle", "protection_class", "ambient_temp_c", "frequency_hz", "altitude_m"}
        l3_keys = {"mounting", "insulation_class", "application", "outer_diameter_limit_mm"}

        found_any = False
        for field, (pattern, caster) in field_map.items():
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                raw_val = match.group(1).strip().rstrip(',').strip()
                if raw_val.lower() in ("null", "none", "未知", ""):
                    continue
                try:
                    val = caster(raw_val) if caster != str else raw_val
                except (ValueError, TypeError):
                    continue

                if field in l1_keys:
                    result["L1"][field] = val
                elif field in l2_keys:
                    result["L2"][field] = val
                elif field in l3_keys:
                    result["L3"][field] = val
                found_any = True

        # v4.01.028: 显式极数锁定 — "4极" / "极数4" / "极对数2" 必须钉死, 不被转速反推覆盖
        l1_res = result.setdefault("L1", {})
        if "poles" not in l1_res:
            m_poles = re.search(r'(\d+)\s*极', content)
            if m_poles:
                l1_res["poles"] = int(m_poles.group(1))
                found_any = True
            else:
                m_pp = re.search(r'极对数\s*[:=]?\s*(\d+)', content)
                if m_pp:
                    l1_res["poles"] = int(m_pp.group(1)) * 2
                    found_any = True

        return result if found_any else {}

    @staticmethod
    def _extract_freetext_fields(raw_text: str) -> dict:
        """
        v4.01.052: LLM 三层提取之前, 用宽松正则从自由文本预提取高置信字段(Hybrid 架构·正则先验)。
        捕获 LLM 易漏的固定格式写法: "4极" / "(1460rpm)" / "效率达92%" / "IP55" / "B级绝缘" 等。
        返回扁平 dict(仅含成功捕获), 用于: ① 注入提示词作"已知条件" ② Step2 写入 layered(标签"正则识别")。
        仅捕获带明确单位/关键词的格式固定值; 模糊表述("十几千瓦""效率要高")交给 LLM, 避免误提取带偏模型。
        """
        text = raw_text or ""
        out = {}

        # 转速: 数字在 rpm/转 之前 (覆盖 "1460rpm" "(1460转)" "n=1460" "1460r/min")
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:rpm|r/min|转/分|转每分|转)', text, re.IGNORECASE)
        if m:
            out["speed_rpm"] = float(m.group(1))

        # 极数: "4极" / "极数4" / "6P" (负向环视排除 MPa/HP/IP 等误匹配)
        m = re.search(r'(?<![A-Za-z])(\d+)\s*极', text)
        if not m:
            m = re.search(r'极数\s*[:=]?\s*(\d+)', text)
        if not m:
            m = re.search(r'(?<![A-Za-z])(\d+)\s*[Pp](?![A-Za-z])', text)
        if m:
            out["poles"] = int(m.group(1))

        # 功率: "15kW" / "15千瓦" / "15KW"
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:千瓦|kw|KW|Kw)', text, re.IGNORECASE)
        if m:
            out["power_kw"] = float(m.group(1))

        # 电压: "380V" / "380伏" (排除 V 后的字母, 如 VAC)
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:V|伏)(?![A-Za-z])', text, re.IGNORECASE)
        if m:
            out["voltage_v"] = float(m.group(1))

        # 频率: "50Hz" / "60HZ"
        m = re.search(r'(\d+(?:\.\d+)?)\s*Hz', text, re.IGNORECASE)
        if m:
            out["frequency_hz"] = float(m.group(1))

        # 效率: "效率达92%" / "效率为92%" / "η=0.92" / "效率92" → 百分数统一转小数
        # v4.01.052: 关键词与数值间允许少量连接词(达/为/约/达到等), 用 \D{0,4}? 非贪婪匹配,
        #   覆盖"效率达92%"这类口语化写法(原 \s*[:=]? 写法会漏掉"达")
        m = re.search(r'(?:效率|效\s*率|η)\D{0,4}?(\d+(?:\.\d+)?)\s*%', text)
        if m:
            eff = float(m.group(1))
            out["efficiency"] = round(eff / 100.0, 4) if eff > 1 else eff
        else:
            m2 = re.search(r'(?:效率|效\s*率|η)\D{0,4}?(0?\.\d+)', text)
            if m2:
                out["efficiency"] = float(m2.group(1))

        # 防护等级: IP55
        m = re.search(r'IP\s*(\d{2})', text, re.IGNORECASE)
        if m:
            out["protection_class"] = f"IP{m.group(1)}"

        # 绝缘等级: B/F/H 级
        m = re.search(r'绝缘(?:等级)?\s*[:：]?\s*([BFH])', text, re.IGNORECASE)
        if m:
            out["insulation_class"] = m.group(1).upper()

        # 冷却方式: 风冷/水冷/油冷/自然冷却
        m = re.search(r'(风冷|水冷|油冷|自然冷却|强制风冷)', text)
        if m:
            out["cooling"] = m.group(1)

        return out

    def _apply_regex_prior(self, layered: dict, regex_fields: dict, notes: list):
        """
        v4.01.052: 将正则预提取结果作为"正则识别"来源写入 layered。
        仅填补 LLM/上游仍为 None/"未知" 的字段(不覆盖已有值); 命中字段记入 _regex_keys。
        (LLM 已给出的同字段, 正则值已作为"已知条件"注入提示词, 此处不再覆盖, 避免冲突。)
        """
        if not regex_fields:
            return
        # 字段 → 层 映射 (与 L1/L2/L3 三层结构对齐)
        layer_map = {
            "power_kw": "L1", "voltage_v": "L1", "speed_rpm": "L1", "torque_nm": "L1",
            "poles": "L1", "cooling": "L1", "motor_type": "L1",
            "duty_cycle": "L2", "ed_pct": "L2", "overload_pct": "L2", "altitude_m": "L2",
            "protection_class": "L2", "ambient_temp_c": "L2", "frequency_hz": "L2",
            "efficiency": "L2",
            "mounting": "L3", "insulation_class": "L3", "shaft_config": "L3",
            "bearing_type": "L3", "application": "L3", "outer_diameter_limit_mm": "L3",
        }
        regex_keys = layered.setdefault("_regex_keys", set())
        for k, v in regex_fields.items():
            layer = layer_map.get(k)
            if not layer:
                continue
            cur = layered.get(layer, {}).get(k)
            if cur in (None, "", "未知"):
                layered[layer][k] = v
                regex_keys.add(k)
                notes.append(f"[正则识别] 从原文预提取 {k}={v}")

    def _supplement_from_text(self, layered: dict, raw_text: str, notes: list):
        """
        v4.01.039: 用正则从原始用户文本补充 LLM 漏提的字段(尤其约束类, 如外径限制)。
        仅填补 layered 中仍为 None/空/"未知" 的字段, 绝不覆盖 LLM 已给出的字段。
        各正则模式大多要求 "key[:=]value", 在自由文本中只会命中明显写法,
        不会把"安装空间"误判为安装方式(安装方式正则要求"安装方式/安装形式"前缀)。
        """
        supp = RequirementAgent._extract_fields_regex(raw_text)
        if not supp:
            return
        for layer in ("L1", "L2", "L3"):
            for k, v in supp.get(layer, {}).items():
                if v in (None, "", "未知"):
                    continue
                cur = layered.get(layer, {}).get(k)
                if cur in (None, "", "未知"):
                    layered[layer][k] = v
                    layered.setdefault("_extracted_keys", set()).add(k)
                    notes.append(f"[文本正则补充] 从原文识别 {k}={v}")

    def _organize_layers(self, extracted: dict, notes: list) -> dict:
        """将 LLM 输出归档为三层结构，并补全默认值"""
        l1 = extracted.get("L1", {})
        l2 = extracted.get("L2", {})
        l3 = extracted.get("L3", {})

        # L1 默认值 — v4.01.028: motor_type 不再默认"异步电动机", 改为 None 强制人工确认
        l1.setdefault("motor_type", None)
        # v4.01.052: 删除「转速默认3000rpm」错误源头 — speed 保持 None,
        #   交由 Step3 物理补全(极数+频率反推) 或 人工确认兜底, 绝不静默塞入 3000
        l1.setdefault("cooling", "未知")
        if l1.get("torque_nm") is None:
            l1["torque_nm"] = None  # 留给物理补全
        if l1.get("poles") is None:
            l1["poles"] = None  # 留给物理补全

        # L2 默认值 — v4.01: 频率/海拔/过载不再硬编码, 交给共享知识引擎推导
        l2.setdefault("duty_cycle", "S1")
        l2.setdefault("protection_class", "未知")
        l2.setdefault("ambient_temp_c", 40)
        l2.setdefault("frequency_hz", None)
        l2.setdefault("altitude_m", None)
        l2.setdefault("overload_pct", None)
        l2.setdefault("ed_pct", None)

        # L3 默认值 — v4.01: 绝缘/安装不再硬编码, 交给共享知识引擎从行业默认值补全
        l3.setdefault("mounting", "未知")
        l3.setdefault("insulation_class", "未知")
        l3.setdefault("shaft_config", "单轴伸")
        l3.setdefault("bearing_type", "未知")
        l3.setdefault("application", "通用")
        l3.setdefault("outer_diameter_limit_mm", None)

        # v4.01.028: 记录来源分类, 供人机检查点颜色分区与置信度使用
        extracted_keys = set()
        for layer in ("L1", "L2", "L3"):
            for k, v in extracted.get(layer, {}).items():
                if v not in (None, "", "未知"):
                    extracted_keys.add(k)
        layered_out = {"L1": l1, "L2": l2, "L3": l3}
        layered_out["_extracted_keys"] = extracted_keys
        layered_out["_inferred_keys"] = set()
        layered_out["_defaulted_keys"] = set()
        # v4.01.052: 来源标注扩展 — 正则识别 / 物理补全 单独成集, 供 param_sources 区分
        layered_out["_regex_keys"] = set()
        layered_out["_physics_keys"] = set()
        return layered_out

    def _extract_center_height_and_query_catalog(self, raw_text: str,
                                                   layered: dict, notes: list):
        """
        v2.05.06: 从用户描述中提取中心高(机座号), 查询Y系列型谱
        当功率或极数缺失时, 列出该中心高下的标准配置供用户选择
        """
        import re

        # 提取中心高 (如 "280中心高" "中心高280" "Y280" "机座号280")
        center_height = None
        patterns = [
            r'(\d{2,3})\s*中心高',
            r'中心高\s*(\d{2,3})',
            r'[Yy](\d{2,3})\s*[SMsm]',  # Y280S, Y280M
            r'[Yy](\d{2,3})\b',
            r'机座号?\s*(\d{2,3})',
            r'(\d{3})\s*机座',
        ]
        for pat in patterns:
            match = re.search(pat, raw_text)
            if match:
                try:
                    center_height = int(match.group(1))
                    if 80 <= center_height <= 630:
                        break
                    else:
                        center_height = None
                except (ValueError, IndexError):
                    continue

        if not center_height:
            return

        notes.append(f"检测到中心高: {center_height}mm (机座号Y{center_height})")
        layered["L1"]["center_height"] = center_height

        # 查询型谱: 该中心高下的所有标准配置
        try:
            catalog_entries = find_catalog_by_frame(center_height)
            if not catalog_entries:
                return

            # 检查功率和极数是否缺失
            l1 = layered["L1"]
            power = l1.get("power_kw")
            poles = l1.get("poles")

            if power is None or poles is None:
                # 构建型谱选项表
                options_text = f"中心高{center_height}mm的Y系列电机, 标准配置如下:\n"
                options_text += f"{'极数':>4} | {'同步转速':>8} | {'功率(kW)':>10} | {'型号':>12}\n"
                options_text += "-" * 50 + "\n"

                for entry in catalog_entries:
                    p = entry.get("poles", "?")
                    sync_speed = int(120 * 50 / p) if p and p > 0 else 0
                    pw = entry.get("power_kw", "?")
                    model = entry.get("model", f"Y{center_height}-{p}")

                    # 标记推荐项 (最常见的4极)
                    marker = " ←" if p == 4 else ""
                    options_text += f"{p:>4}极 | {sync_speed:>6}rpm | {pw:>8}kW | {model:>12}{marker}\n"

                options_text += "\n如未指定, 通常默认4极作为初始设计点。"
                notes.append(f"[型谱查询] {len(catalog_entries)}种标准配置可选:\n{options_text}")

                # 将型谱选项存入layered, 供hc_requirement检查点展示
                layered.setdefault("_catalog_options", {
                    "center_height": center_height,
                    "entries": [
                        {
                            "poles": e.get("poles"),
                            "power_kw": e.get("power_kw"),
                            "model": e.get("model", ""),
                            "sync_speed": int(120 * 50 / e["poles"]) if e.get("poles") else 0,
                        }
                        for e in catalog_entries
                    ],
                    "recommendation": "4极" if any(e.get("poles") == 4 for e in catalog_entries) else "",
                })
            else:
                # 功率和极数都有, 验证是否在型谱中
                matched = [e for e in catalog_entries
                           if e.get("poles") == poles and e.get("power_kw") == power]
                if matched:
                    notes.append(f"[型谱验证] {power}kW/{poles}极 匹配型谱: {matched[0].get('model', '')}")
                else:
                    notes.append(f"[型谱验证] {power}kW/{poles}极 不在Y{center_height}标准型谱中, 将按非标设计")

        except Exception as e:
            notes.append(f"[型谱查询] 查询失败: {e}")

    def _query_case_motor_matches(self, layered: dict, notes: list):
        """v4.01.051: 查询已有案例电机库, 取 Top 3 匹配 (按功率/电压/转速/极数/类型相似度评分)"""
        try:
            from case_motor_db import list_case_motors
        except Exception:
            return  # 案例电机模块不可用, 静默跳过

        l1 = layered.get("L1", {})
        power = l1.get("power_kw")
        voltage = l1.get("voltage_v")
        speed = l1.get("speed_rpm")
        poles = l1.get("poles")
        motor_type = l1.get("motor_type")

        if not any([power, voltage, speed, motor_type, poles]):
            return

        try:
            candidates = list_case_motors(
                motor_type=motor_type if motor_type else None,
                status="approved",
                limit=50,
            )
        except Exception:
            candidates = []

        if not candidates:
            return

        # 相似度评分
        scored = []
        for c in candidates:
            score = 0
            if motor_type and motor_type in (c.get("motor_type") or ""):
                score += 3
            if power and c.get("rated_power"):
                if abs(c["rated_power"] - power) / max(abs(c["rated_power"]), 1) < 0.5:
                    score += 2
            if voltage and c.get("rated_voltage"):
                if abs(c["rated_voltage"] - voltage) / max(abs(c["rated_voltage"]), 1) < 0.3:
                    score += 1
            if speed and c.get("rated_speed"):
                if abs(c["rated_speed"] - speed) / max(abs(c["rated_speed"]), 1) < 0.3:
                    score += 2
            if poles and c.get("poles") and c["poles"] == poles:
                score += 2
            if score > 0:
                scored.append((score, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        top3 = scored[:3]
        if not top3:
            return

        matches = [{
            "case_code": c.get("case_code", ""),
            "motor_type": c.get("motor_type", ""),
            "motor_model": c.get("motor_model", ""),
            "rated_power": c.get("rated_power"),
            "rated_voltage": c.get("rated_voltage"),
            "rated_speed": c.get("rated_speed"),
            "poles": c.get("poles"),
            "match_score": score,
        } for score, c in top3]

        layered["_case_motor_matches"] = matches
        notes.append(f"[案例查询] 找到 {len(matches)} 个相似案例电机: " +
                     ", ".join(f"{m['case_code']}({m.get('rated_power', '?')}kW)" for m in matches))

    def _apply_forced_questions(self, raw_text: str, layered: dict, notes: list):
        """
        v2.05.02: 应用飞轮生成的追问规则
        如果用户描述中包含关键词但参数未提取, 自动标记为需追问
        """
        rules = _load_forced_questions()
        if not rules:
            return

        text_lower = raw_text.lower()
        merged = {**layered["L1"], **layered["L2"], **layered["L3"]}

        for rule in rules:
            param = rule.get("param", "")
            keywords = rule.get("keywords", [])
            # 检查用户描述是否包含任何关键词
            matched_kw = [kw for kw in keywords if kw.lower() in text_lower]
            if not matched_kw:
                continue

            # 检查参数是否已被提取
            current_val = merged.get(param)
            if current_val is None or current_val == "未知" or current_val == "":
                msg = rule.get("message", f"请补充{param}信息")
                notes.append(f"[飞轮追问] 检测到关键词{matched_kw[:3]}, {msg}")
                # v4.01.039: 仅 L1 核心参数缺失才阻断流程; L2/L3 缺失不阻断
                # (由行业默认值/推导补全, 用户后续可在检查点修正, 避免"安装空间"
                #  这类约束被误判为安装方式追问而阻断)
                _L1_BLOCKING = {"torque_nm", "power_kw", "voltage_v",
                                "speed_rpm", "poles", "cooling", "motor_type"}
                if param in _L1_BLOCKING:
                    # 去重: 复核节点已就同一 L1 参数追问过则跳过, 避免重复阻断
                    _pq = layered["L1"].setdefault("_pending_questions", [])
                    if any(isinstance(q, dict) and q.get("param") == param for q in _pq):
                        continue
                    _pq.append({"param": param,
                                "keywords_matched": matched_kw[:3],
                                "message": msg})
                else:
                    notes.append(f"[非阻断提示] 检测到关键词{matched_kw[:3]}, "
                                 f"{param} 未提取, 采用默认/推导值, 不阻断流程")

    @staticmethod
    def _is_sync_motor(motor_type: str) -> bool:
        """判断是否为同步类电机(永磁同步/同步/无刷直流/PMSM) — 同步电机无转差"""
        mt = (motor_type or "").lower()
        return ("永磁" in mt or "同步" in mt or "pmsm" in mt
                or "bldc" in mt or "无刷" in mt)

    @staticmethod
    def _detect_drive_type(raw_text: str, motor_type: str = "") -> str:
        """v4.02.17 P1-1: 识别驱动方式 — 变频调速(vfd) vs 工频(pf)
        文本或电机类型含 变频/调速/inverter/VFD/变频器/变频调速/vvvf/矢量控制 等关键词 → vfd
        否则 → pf (工频, 频率固定 50/60Hz)
        """
        blob = f"{(raw_text or '')} {(motor_type or '')}".lower()
        _vfd_kw = ["变频", "调速", "inverter", "vfd", "vvvf", "变频器",
                   "变频调速", "矢量控制", "field oriented", "field-oriented"]
        if any(k in blob for k in _vfd_kw):
            return "vfd"
        return "pf"

    def _calc_frequency(self, speed: float, poles: int, motor_type: str,
                        drive_type: str = None) -> float:
        """v4.02.17 P1-1: 按电机类型+驱动方式计算频率
        - 同步电机用实际转速, 异步电机用同步转速(除转差)
        - 工频(pf): 圆整到整数 (如 50Hz)
        - 变频调速(vfd): 圆整到小数后一位 (如 87.5Hz)
        """
        if drive_type is None:
            drive_type = "pf"
        if self._is_sync_motor(motor_type):
            f_raw = speed * poles / 120.0
        else:
            f_raw = speed / (1 - 0.03) * poles / 120.0
        if drive_type == "vfd":
            return round(f_raw, 1)
        return round(f_raw)

    def _physics_complete(self, layered: dict, notes: list):
        """物理补全: 转矩/极数推算"""
        l1 = layered["L1"]
        l2 = layered["L2"]

        power = l1.get("power_kw")
        speed = l1.get("speed_rpm")
        torque = l1.get("torque_nm")
        poles = l1.get("poles")
        # v4.01: 频率可能为None(未由用户/LLM提供), 临时用50Hz用于极数估算
        frequency = l2.get("frequency_hz") or 50

        # 转矩补全: T = 9550 * P / n
        if torque is None and power and speed:
            torque = compute_torque(power, speed)
            l1["torque_nm"] = torque
            layered.setdefault("_inferred_keys", set()).add("torque_nm")
            notes.append(f"转矩自动计算: T=9550×{power}/{speed}={torque}Nm")

        # 转速反推: 已知极数+频率, 由 n=120*f/p 反推同步转速 (来源: 物理补全)
        # v4.01.052: 配套完善点1 — 删默认3000后, 若文本未给转速但有极数+频率,
        #   反推实际转速, 避免 speed 永久为空; 无频率则不动(交人工确认兜底)
        freq_provided = l2.get("frequency_hz")
        if speed is None and poles and freq_provided:
            n_sync = round(120 * freq_provided / poles)
            is_sync = self._is_sync_motor(l1.get("motor_type", ""))
            speed_val = n_sync if is_sync else round(n_sync * (1 - 0.03))
            l1["speed_rpm"] = speed_val
            speed = speed_val
            layered.setdefault("_physics_keys", set()).add("speed_rpm")
            notes.append(f"转速反推: p={poles}极 × f={freq_provided}Hz → 同步转速{n_sync}rpm, 取实际转速{speed_val}rpm")

        # 极数补全: 根据转速反推 — v4.01.028 改为"最接近标准同步转速"法, 且区分电机类型
        # 显式极数(poles 已给定)绝不覆盖; 仅当 poles=None 时反推
        if poles is None and speed:
            # 估算同步转速: 同步电机无转差(n_sync=n实际); 异步电机按3%转差估算
            is_sync = self._is_sync_motor(l1.get("motor_type", ""))
            if is_sync:
                n_sync_est = speed
            else:
                n_sync_est = speed / (1 - 0.03)
            # 选择最接近的频率档(默认50Hz, 60Hz就近)
            if abs(frequency - 60) < abs(frequency - 50):
                std = [3600, 1800, 1200, 900, 720, 600, 500, 428]
            else:
                std = [3000, 1500, 1000, 750, 600, 500, 428, 375]
            n_sync = min(std, key=lambda s: abs(s - n_sync_est))
            poles = max(2, round(120 * frequency / n_sync))
            if poles % 2 != 0:  # 极数必为偶数
                poles += 1
            l1["poles"] = poles
            layered.setdefault("_inferred_keys", set()).add("poles")
            notes.append(f"极数自动推算: n≈{speed}rpm → {poles}极 (取标准同步转速{n_sync}rpm)")

        # v4.01.051: 频率计算 f = p×n/60 (显式锁定, 不覆盖用户/LLM 已提供值)
        if l2.get("frequency_hz") is None and poles and speed:
            freq = self._calc_frequency(speed, poles, l1.get("motor_type", ""), layered.get("_drive_type"))
            l2["frequency_hz"] = freq
            layered.setdefault("_inferred_keys", set()).add("frequency_hz")
            notes.append(f"频率自动计算: f={freq}Hz (p={poles}极, n={speed}rpm)")


    def _shared_knowledge_complete(self, layered: dict, notes: list):
        """
        v4.01: 使用共享知识引擎补全参数

        调用 SharedKnowledge.infer_all_params() 进行:
        - 频率从极数/转速自动计算 (PMSM无转差, 异步电机3%转差)
        - 行业默认值补全 (冷却/绝缘/过载/海拔/防护等级/安装方式等)
        - 海拔/温度降容计算
        - 效率/功率因数查表
        - 电压-电流互推
        """
        merged = {**layered["L1"], **layered["L2"], **layered["L3"]}

        # 调用共享知识引擎推导
        try:
            completed = knowledge_engine.infer_all_params(merged)
        except Exception as e:
            notes.append(f"[共享知识] 引擎调用失败: {e}, 使用基础默认值")
            # 兜底: 至少补全频率和基本默认值
            l2 = layered["L2"]
            if l2.get("frequency_hz") is None:
                poles = layered["L1"].get("poles")
                speed = layered["L1"].get("speed_rpm")
                if poles and speed:
                    l2["frequency_hz"] = self._calc_frequency(speed, poles, layered["L1"].get("motor_type", ""), layered.get("_drive_type"))
                    notes.append(f"频率兜底计算: f={l2['frequency_hz']}Hz")
                else:
                    l2["frequency_hz"] = 50
            if l2.get("altitude_m") is None:
                l2["altitude_m"] = 0
                layered.setdefault("_defaulted_keys", set()).add("altitude_m")
            if l2.get("overload_pct") is None:
                l2["overload_pct"] = 0
                layered.setdefault("_defaulted_keys", set()).add("overload_pct")
            if layered["L3"].get("insulation_class") in (None, "", "未知"):
                layered["L3"]["insulation_class"] = "F"
                layered.setdefault("_defaulted_keys", set()).add("insulation_class")
            if layered["L2"].get("protection_class") in (None, "", "未知"):
                layered["L2"]["protection_class"] = "IP44"
                layered.setdefault("_defaulted_keys", set()).add("protection_class")
            return

        # 记录推导过程
        inferred = completed.get("_inferred", [])
        for note in inferred:
            notes.append(f"[共享知识] {note}")

        # 将推导结果回写到分层结构
        l1_keys = {"power_kw", "voltage_v", "speed_rpm", "torque_nm", "poles",
                   "cooling", "motor_type"}
        # v4.06-P2: frequency_hz 已在此白名单, 但共享知识引擎同时算出的
        #   sync_speed_rpm / slip 此前**缺登记 → 被静默丢弃**(specs 里恒为 None),
        #   导致 ReportAgent 拿不到权威同步速/转差, 只能按工频 50Hz 自行臆测
        #   (报告正文出现「同步转速1000rpm / 转差率7.5%」等错误叙述)。
        l2_keys = {"duty_cycle", "ed_pct", "overload_pct", "altitude_m",
                   "protection_class", "ambient_temp_c", "frequency_hz", "efficiency",
                   "sync_speed_rpm", "slip"}
        l3_keys = {"mounting", "insulation_class", "shaft_config",
                   "bearing_type", "application"}

        # v4.01.028: 标记被推导引擎补全的字段(用于来源区分)
        inferred_keys = set(completed.get("_confidence", {}).keys())
        layered.setdefault("_inferred_keys", set()).update(inferred_keys)

        for key, val in completed.items():
            if key.startswith("_") or val is None:
                continue
            if key in l1_keys:
                current = layered["L1"].get(key)
                if current in (None, "", "未知"):
                    layered["L1"][key] = val
                    if current in ("未知", "", None):
                        layered.setdefault("_defaulted_keys", set()).add(key)
            elif key in l2_keys:
                current = layered["L2"].get(key)
                if current in (None, "", "未知"):
                    layered["L2"][key] = val
                    if current in ("未知", "", None):
                        layered.setdefault("_defaulted_keys", set()).add(key)
            elif key in l3_keys:
                current = layered["L3"].get(key)
                if current in (None, "", "未知"):
                    layered["L3"][key] = val
                    if current in ("未知", "", None):
                        layered.setdefault("_defaulted_keys", set()).add(key)

        # 确保 frequency_hz 不为 None (兜底) — v4.01.028 按电机类型计算
        if layered["L2"].get("frequency_hz") is None:
            poles = layered["L1"].get("poles")
            speed = layered["L1"].get("speed_rpm")
            if poles and speed:
                layered["L2"]["frequency_hz"] = self._calc_frequency(
                    speed, poles, layered["L1"].get("motor_type", ""), layered.get("_drive_type"))
            else:
                layered["L2"]["frequency_hz"] = 50

        # 确保 altitude_m 不为 None
        if layered["L2"].get("altitude_m") is None:
            layered["L2"]["altitude_m"] = 0

        # 确保 overload_pct 不为 None
        if layered["L2"].get("overload_pct") is None:
            layered["L2"]["overload_pct"] = 0

    # ─────────────────────────────────────────────────────────────
    # v4.01.038: 需求复核节点 (二次提取 + 推导补齐 + 行业惯例 + 合并 + 强制追问)
    # ─────────────────────────────────────────────────────────────
    _AMBIGUOUS_WORDS = ["左右", "大约", "大概", "可能", "或许", "不确定", "差不多",
                        "估计", "约", "以上", "以下", "不低于", "不高于", "近似", "疑似"]
    _L1_CORE = ["power_kw", "voltage_v", "speed_rpm", "torque_nm", "poles", "motor_type"]
    _REVIEW_INIT_EXTRACT_CONF = 0.85   # 初次提取命中视为 0.85
    _REVIEW_INFER_CONF = 0.60
    _REVIEW_DEFAULT_CONF = 0.40
    _REVIEW_CLOUD_CONF = 0.92
    _REVIEW_LOCAL_CONF = 0.80

    def _quick_confidence(self, layered: dict) -> float:
        """复核触发用快速置信度 (仅按 L1 完整度加权, 不含一致性惩罚)"""
        l1 = layered.get("L1", {})
        weights = {"power_kw": 0.25, "voltage_v": 0.25, "speed_rpm": 0.20,
                   "torque_nm": 0.15, "motor_type": 0.10, "poles": 0.05}
        score = 0.0
        for k, w in weights.items():
            v = l1.get(k)
            if v is not None and v != "未知":
                score += w
        if l1.get("motor_type") in (None, "", "待确认", "未知"):
            score -= 0.10
        return max(0.0, min(1.0, score))

    @staticmethod
    def _flatten_layers(layered_or_extracted: dict) -> dict:
        """将 {L1:{}, L2:{}, L3:{}} 结构展平为一维 dict"""
        flat = {}
        for layer in ("L1", "L2", "L3"):
            src = layered_or_extracted.get(layer, {})
            if isinstance(src, dict):
                for k, v in src.items():
                    if k.startswith("_"):
                        continue
                    flat[k] = v
        return flat

    @staticmethod
    def _coerce_numeric_fields(layered: dict) -> None:
        """v4.01.053: 源头数值类型收敛 — 把 L1/L2/L3 已知数值字段从 str 收敛为 int/float。

        根因: _robust_json_parse 的 json.loads 分支原样保留 LLM 输出的字符串类型
          (如 "60"/"1000"/"150"), 流入下游 physics_kernel 的 '>' 比较抛 TypeError
          ('>' not supported between instances of 'str' and 'int')。
        仅收敛; 收敛失败保留原值(不掩盖真实缺失/未知)。
        """
        _float_map = {
            "L1": {"power_kw", "voltage_v", "speed_rpm", "torque_nm"},
            "L2": {"ambient_temp_c", "frequency_hz", "altitude_m", "overload_pct",
                   "ed_pct", "efficiency"},
            "L3": {"outer_diameter_limit_mm"},
        }
        _int_map = {
            "L1": {"poles"},
        }
        for layer, fields in _float_map.items():
            d = layered.get(layer)
            if not isinstance(d, dict):
                continue
            for k in fields:
                v = d.get(k)
                if isinstance(v, str):
                    s = v.strip()
                    if s:
                        try:
                            d[k] = float(s)
                        except ValueError:
                            pass
        for layer, fields in _int_map.items():
            d = layered.get(layer)
            if not isinstance(d, dict):
                continue
            for k in fields:
                v = d.get(k)
                if isinstance(v, str):
                    s = v.strip()
                    if s:
                        try:
                            d[k] = int(float(s))
                        except (ValueError, TypeError):
                            pass

    def _review_should_trigger(self, raw_text: str, layered: dict):
        """返回 (是否触发, 触发原因列表)"""
        reasons = []
        l1 = layered.get("L1", {})
        # 1. 核心 L1 缺失
        missing = [k for k in self._L1_CORE
                   if l1.get(k) in (None, "", "未知")]
        if missing:
            reasons.append("核心参数缺失: " + ", ".join(missing))
        # 2. 置信度低
        conf = self._quick_confidence(layered)
        if conf < 0.7:
            reasons.append(f"整体置信度偏低({conf:.2f}<0.7)")
        # 3. 文本超长 / 歧义
        if len(raw_text) > 300:
            reasons.append(f"文本较长({len(raw_text)}字)")
        amb = [w for w in self._AMBIGUOUS_WORDS if w in raw_text]
        if amb:
            reasons.append("含歧义表述: " + "/".join(amb[:5]))
        return (len(reasons) > 0, reasons)

    def _review_llm_extract(self, raw_text: str, missing_fields: list):
        """
        二次提取: 优先云端大模型(DeepSeek), 不可用则本地模型
        返回 (flat_params_dict, source_str)
        source_str: "复核-云端" / "复核-本地" / "复核-失败"
        """
        system = (
            "你是电机需求参数复核专家。客户描述可能存在缺失或歧义。"
            "请只针对【尚未确定】的字段, 从文本中尽力提取其数值或选项; "
            "确实无法从文本确定的字段填 null。只返回严格 JSON, 不要任何解释。"
            "JSON 结构: {\"L1\":{...}, \"L2\":{...}, \"L3\":{...}}, 字段名与初次提取一致。"
        )
        user = (
            f"尚未确定/缺失的字段: {', '.join(missing_fields)}\n\n"
            f"客户描述:\n{raw_text}\n\n仅补充上述缺失字段的 JSON:"
        )
        # v4.02.07 H-3 二期 T1: 经系统级网关路由 (云端优先, 失败自动降级本地, 对调用方透明)
        from langchain_core.messages import SystemMessage, HumanMessage
        llm = self._get_llm_or_cloud(temperature=0.1)
        if llm is None:
            return {}, "复核-失败", 0, 0
        try:
            resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        except Exception as e:
            logger.warning("[RequirementAgent] 复核二次提取失败: %s", e)
            return {}, "复核-失败", 0, 0
        content = (resp.content or "").strip()
        flat = self._flatten_layers(self._robust_json_parse(content))
        # v4.06-P1: 捕获真实 token 用量 (云端 _CloudAIMessage / 本地 ChatOllama usage_metadata)
        _ti, _to = extract_ollama_usage(resp, f"{system}\n\n{user}")
        _went = getattr(llm, "went_cloud", False)
        return flat, ("复核-云端" if _went else "复核-本地"), _ti, _to

    def _review_specs(self, raw_text: str, layered: dict, notes: list):
        """
        v4.01.038 需求复核节点
        触发条件: L1核心缺 / 置信度<0.7 / 文本长或歧义
        执行:
          1. 二次提取(云端优先, 本地兜底)填补缺失
          2. 重跑推导工具(infer_all_params)
          3. 行业惯例库(get_industry_defaults)填充默认
          4. 高置信度合并, 标记来源
          5. 仍缺失的 L1 → 写入 _pending_questions (配合强制追问, 不可静默放行)
        """
        should, reasons = self._review_should_trigger(raw_text, layered)
        if not should:
            return 0, 0
        logger.info("[RequirementAgent] 触发需求复核: %s", "; ".join(reasons))
        notes.append("[需求复核] 触发: " + "; ".join(reasons))

        l1 = layered["L1"]; l2 = layered["L2"]; l3 = layered["L3"]
        missing_now = [k for k in self._L1_CORE
                       if l1.get(k) in (None, "", "未知")]

        # 字段 → 层 映射
        keymap = {
            **{k: "L1" for k in (set(self._L1_CORE) | {"cooling"})},
            **{k: "L2" for k in ("duty_cycle", "ed_pct", "overload_pct", "altitude_m",
                                 "protection_class", "ambient_temp_c", "frequency_hz",
                                 "efficiency")},
            **{k: "L3" for k in ("mounting", "insulation_class", "shaft_config",
                                  "bearing_type", "application", "outer_diameter_limit_mm")},
        }
        reviewed_keys = layered.setdefault("_reviewed_keys", set())

        # 当前各字段置信度估计 (用于冲突时高置信度优先)
        def _cur_conf(k):
            if k in layered.get("_extracted_keys", set()):
                return self._REVIEW_INIT_EXTRACT_CONF
            if k in layered.get("_inferred_keys", set()):
                return self._REVIEW_INFER_CONF
            if k in layered.get("_defaulted_keys", set()):
                return self._REVIEW_DEFAULT_CONF
            return 0.0  # 仍缺失 → 任何复核值都更高

        # ── 步骤1: 二次提取 (云端优先 / 本地兜底) ──
        # v4.06-P1: 捕获二次提取真实 token 用量
        rev_ti = 0
        rev_to = 0
        rev_flat, rev_src, rev_ti, rev_to = self._review_llm_extract(raw_text, missing_now)
        rev_conf = self._REVIEW_CLOUD_CONF if rev_src == "复核-云端" else self._REVIEW_LOCAL_CONF
        for k, v in rev_flat.items():
            if v in (None, "", "未知"):
                continue
            layer = keymap.get(k)
            if not layer:
                continue
            cur = layered[layer].get(k)
            if cur in (None, "", "未知"):
                layered[layer][k] = v
                reviewed_keys.add(k)
                notes.append(f"[{rev_src}] 补全 {k}={v}")
            else:
                cc = _cur_conf(k)
                if rev_conf > cc:
                    layered[layer][k] = v
                    reviewed_keys.add(k)
                    for s in ("_extracted_keys", "_inferred_keys", "_defaulted_keys"):
                        layered.get(s, set()).discard(k)
                    notes.append(f"[{rev_src}] 高置信度覆盖 {k}: {cur} → {v}")
                else:
                    notes.append(f"[{rev_src}] 保留初次值 {k}={cur} (复核{rev_conf}≤原{cc})")

        # ── 步骤2: 重跑推导工具 ──
        merged = {**layered["L1"], **layered["L2"], **layered["L3"]}
        try:
            completed = knowledge_engine.infer_all_params(merged)
            for note in completed.get("_inferred", []):
                notes.append(f"[需求复核-推导] {note}")
            for key, val in completed.items():
                if key.startswith("_") or val is None:
                    continue
                layer = keymap.get(key)
                if not layer:
                    continue
                cur = layered[layer].get(key)
                if cur in (None, "", "未知"):
                    layered[layer][key] = val
                    layered.setdefault("_inferred_keys", set()).add(key)
                    layered.get("_defaulted_keys", set()).discard(key)
        except Exception as e:
            notes.append(f"[需求复核-推导] 引擎调用失败: {e}")

        # ── 步骤3: 行业惯例库填充默认 (仅仍缺失项) ──
        merged2 = {**layered["L1"], **layered["L2"], **layered["L3"]}
        application = str(merged2.get("application", "") or "通用")
        power = merged2.get("power_kw")
        try:
            defaults = knowledge_engine.get_industry_defaults(application, power)
            if isinstance(defaults, dict):
                for key, val in defaults.items():
                    if val in (None, "", "未知"):
                        continue
                    layer = keymap.get(key)
                    if not layer:
                        continue
                    cur = layered[layer].get(key)
                    if cur in (None, "", "未知"):
                        layered[layer][key] = val
                        layered.setdefault("_defaulted_keys", set()).add(key)
                        notes.append(f"[需求复核-行业惯例] {key}={val} (默认)")
        except Exception as e:
            notes.append(f"[需求复核-行业惯例] 查询失败: {e}")

        # ── 步骤5: 仍缺失的 L1 → 强制追问 (不可静默放行) ──
        still_missing = [k for k in self._L1_CORE
                         if layered["L1"].get(k) in (None, "", "未知")]
        for k in still_missing:
            msg = f"复核后仍缺失核心参数 {k}, 必须人工确认, 不能静默放行"
            notes.append(f"[需求复核-强制追问] {msg}")
            layered["L1"].setdefault("_pending_questions", []).append(
                {"param": k, "keywords_matched": ["复核缺失"], "message": msg})

        # v4.06-P1: 返回二次提取真实 token 用量, 供 run() 累加进成本审计
        return rev_ti, rev_to

    def _assess_confidence(self, layered: dict, notes: list = None) -> float:
        """
        置信度评估
        v4.01.051: L1 权重降至 0.8, L2+L3 合计 0.2
        - L1 (0.80): power_kw 0.20, voltage_v 0.20, speed_rpm 0.16, torque_nm 0.12, motor_type 0.08, poles 0.04
        - L2 (0.12): frequency_hz 0.05, duty_cycle 0.03, protection_class 0.02, ambient_temp_c 0.02
        - L3 (0.08): application 0.04, mounting 0.02, insulation_class 0.02
        v4.01.028: 叠加一致性校验惩罚
        """
        if notes is None:
            notes = []
        l1 = layered["L1"]
        l2 = layered.get("L2", {})
        l3 = layered.get("L3", {})
        weights = {
            # L1 — 合计 0.80
            "power_kw": 0.20,
            "voltage_v": 0.20,
            "speed_rpm": 0.16,
            "torque_nm": 0.12,
            "motor_type": 0.08,
            "poles": 0.04,
            # L2 — 合计 0.12
            "frequency_hz": 0.05,
            "duty_cycle": 0.03,
            "protection_class": 0.02,
            "ambient_temp_c": 0.02,
            # L3 — 合计 0.08
            "application": 0.04,
            "mounting": 0.02,
            "insulation_class": 0.02,
        }
        score = 0.0
        for key, w in weights.items():
            if key in l1:
                val = l1.get(key)
            elif key in l2:
                val = l2.get(key)
            else:
                val = l3.get(key)
            if val is not None and val != "未知":
                score += w
        # motor_type 待确认 → 硬性扣减
        if l1.get("motor_type") in (None, "", "待确认", "未知"):
            score -= 0.10
            notes.append("[一致性] 电机类型未确认, 置信度扣减")

        # 一致性校验惩罚
        penalty, cnotes, flags = self._consistency_check(layered)
        for n in cnotes:
            notes.append(f"[一致性] {n}")
        score = max(0.0, min(1.0, score - penalty))
        return round(score, 2)

    def _consistency_check(self, layered: dict):
        """v4.01.028: 跨字段一致性校验, 返回 (penalty, notes, flags)"""
        l1 = layered.get("L1", {})
        l2 = layered.get("L2", {})
        l3 = layered.get("L3", {})
        notes = []
        flags = {}
        penalty = 0.0

        power = l1.get("power_kw")
        speed = l1.get("speed_rpm")
        torque = l1.get("torque_nm")
        poles = l1.get("poles")
        freq = l2.get("frequency_hz")
        voltage = l1.get("voltage_v")
        application = str(l3.get("application", "") or "")

        # 1. 功率-转矩一致性: T = 9550 * P / n
        if power and speed and torque:
            t_calc = 9550.0 * power / speed
            rel = abs(t_calc - torque) / max(abs(torque), 1e-9)
            if rel > 0.1:
                notes.append(f"转矩{torque}Nm 与 T=9550×{power}/{speed}={t_calc:.0f}Nm 偏差{rel*100:.0f}%")
                penalty += 0.15
                flags["torque_mismatch"] = True

        # 2. 极数-转速-频率一致性: n = 120 * f / p
        if poles and freq and speed:
            n_calc = 120.0 * freq / poles
            rel = abs(n_calc - speed) / max(abs(speed), 1e-9)
            if rel > 0.1:
                notes.append(f"转速{speed}rpm 与 n=120×{freq}/{poles}={n_calc:.0f}rpm 偏差{rel*100:.0f}%")
                penalty += 0.15
                flags["speed_mismatch"] = True

        # 3. 电压-功率-应用场景兼容性 (严重不一致 → 置信度置0)
        app_low = application.lower()
        home_kw = ["家电", "家用", "洗衣机", "冰箱", "空调", "风扇", "吸尘器", "吹风机"]
        if app_low and any(k in app_low for k in home_kw) and voltage and voltage >= 690:
            notes.append(f"严重不一致: 家电应用出现≥690V电压({voltage}V), 置信度置0")
            penalty = 1.0
            flags["voltage_app_fatal"] = True

        # 4. 冷却-防护等级软提示 (水冷通常配高防护)
        cooling = l1.get("cooling")
        prot = l2.get("protection_class")
        if cooling and "水冷" in str(cooling) and prot and str(prot).startswith("IP") \
                and str(prot) in ("IP23", "IP20"):
            notes.append(f"冷却方式为水冷但防护等级{prot}偏低, 建议≥IP54")
            penalty += 0.05
            flags["cooling_protection_soft"] = True

        # 5. v4.01.051: 转速-应用场景一致性 (高转速+家电 / 极低转速+无人机/EV 不合理)
        if speed and application:
            app_lower = application.lower()
            home_kw = ["家电", "家用", "洗衣机", "冰箱", "空调", "风扇"]
            ev_kw = ["电动汽车", "无人机", "电驱", "驱动"]
            if any(k in app_lower for k in home_kw) and speed > 3600:
                notes.append(f"转速{speed}rpm过高, 家电场景通常≤3600rpm")
                penalty += 0.10
                flags["speed_app_mismatch"] = True
            elif any(k in app_lower for k in ev_kw) and speed < 300:
                notes.append(f"转速{speed}rpm过低, 电动汽车/无人机场景通常>500rpm")
                penalty += 0.10
                flags["speed_app_mismatch"] = True

        return min(penalty, 1.0), notes, flags

    @staticmethod
    def _build_param_sources(layered: dict) -> dict:
        """v4.01.052: 为每个参数生成来源标签, 供客户端颜色分区展示
        标签: 正则识别 / 物理补全 / AI识别 / AI计算 / AI增补 / AI复核 / AI建议追问
        优先级(高→低): 待确认 > AI复核 > 正则识别 > 物理补全 > AI识别 > AI计算 > AI增补
        """
        extracted = layered.get("_extracted_keys", set())
        inferred = layered.get("_inferred_keys", set())
        defaulted = layered.get("_defaulted_keys", set())
        reviewed = layered.get("_reviewed_keys", set())
        regex_keys = layered.get("_regex_keys", set())
        physics_keys = layered.get("_physics_keys", set())
        pending = set()
        for layer in ("L1", "L2", "L3"):
            for q in layered.get(layer, {}).get("_pending_questions", []):
                if isinstance(q, dict) and q.get("param"):
                    pending.add(q["param"])

        sources = {}
        for layer in ("L1", "L2", "L3"):
            for k, v in layered.get(layer, {}).items():
                if k.startswith("_") or v in (None, "", "未知"):
                    continue
                if k in pending:
                    sources[k] = "AI建议追问"
                elif k in reviewed:
                    sources[k] = "AI复核"
                elif k in regex_keys:
                    sources[k] = "正则识别"
                elif k in physics_keys:
                    sources[k] = "物理补全"
                elif k in extracted:
                    sources[k] = "AI识别"
                elif k in inferred:
                    sources[k] = "AI计算"
                elif k in defaulted:
                    sources[k] = "AI增补"
                else:
                    sources[k] = "AI识别"
        # motor_type 待确认 → 建议追问
        mt = layered.get("L1", {}).get("motor_type")
        if mt in (None, "", "待确认", "未知"):
            sources["motor_type"] = "AI建议追问"
        return sources
