# -*- coding: utf-8 -*-
"""
DSH (DeepSeek Harness) 集成智能体 — 阶段一：独立运行通路
================================================================
依据《DSH 集成 MotorAI 可行性分析报告》9.3 改动清单实现。

设计原则（阶段一）:
  1. **完全独立**：不 import / 不修改 orchestrator.py、motor_tools.py、
     workflow_config.py；现有 AI 团队 pipeline 零影响。
  2. **双通道 + fail-closed**：
     - 通道 A（HTTP API）：POST 到 DSH Web 服务可配置端点。
       ⚠ v4.02.83 实证：DSH **并不提供**该 HTTP 端点（`agentTeams` 只是
       in-process cordis service；dsh web 的 `/api/*` 还有 process-token
       鉴权，无凭据一律 401）⇒ **默认关闭**（DSH_HTTP_ENABLED=0），
       保留代码路径以备 DSH 未来提供 API。
     - 通道 B（实战主通道，报告 9.5 方案 B）：`node <dsh bin.js> --profile <p> "<task>"`
       一次性任务模式（执行 → 打印结果 → 退出）。
     - 两通道皆不可用时返回 `available=False`，**降级不阻断**。
  3. **结果 schema 映射**：DSH 输出是自然语言 + 可选结构化字段，
     本模块负责抽取 JSON 块并映射到 MotorAI 现有结果 schema。

配置（环境变量，均可在 .env.dev 覆盖）:
  DSH_API_BASE    默认 http://127.0.0.1:3080   DSH Web 服务根地址
  DSH_API_PATH    默认 /api/agent-teams/generate  HTTP 通道路径
  DSH_PROFILE     默认 web                     DSH profile 名
  DSH_CLI_BIN     默认自动探测 npx 缓存中的 bin.js
  DSH_TIMEOUT_S   默认 480                     单次生成超时(秒)，与 runner PER_CASE_TIMEOUT 对齐
                                               (v4.02.83: 260→480，依据实测成功率/耗时分布)
  DSH_HTTP_ENABLED 默认 0                      v4.02.83: HTTP 通道开关(0=直接用 CLI)。
                                               实测 DSH 不提供该 HTTP 端点且 dsh web 有
                                               process-token 鉴权 ⇒ 默认关闭
  DSH_CLI_MAX_ATTEMPTS 默认 1                  M8: agent 层 CLI 重试次数(重试职责在 runner)
  DSH_CHECKER_ENABLED  默认 1                  M2: 确定性几何校验开关
  DSH_CHECKER_MAX_REGEN 默认 1                 M2: 校验失败后的一次重生成闭环次数
  DSH_ENABLED     默认 1                       总开关(0=强制不可用)

v4.02.75 / 2026-09-13
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:  # 优先 requests（后端已依赖），缺失时退回标准库
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    requests = None  # type: ignore
    _HAS_REQUESTS = False

import urllib.error
import urllib.request

# M3 (v4.02.75)：DSH 设计 JSON 契约单一真相源（12 键 / SCHEMA_MAP / 核心键 / 门控键）
# 全部从 Agents/dsh_schema 导入，消除 5 处手工维护；修改契约只改 dsh_schema.py。
from dsh_schema import (  # type: ignore
    SCHEMA_MAP, JSON_CORE_KEYS, JSON_AUX_KEYS, KV_MIN_CORE,
    DSH_DESIGN_KEYS, json_schema_block,
)
# M1/M11 (v4.02.75)：几何量级区间 + 电磁载荷区间由 Knowledge/ 动态生成（KB 不可用回退）
from dsh_kb_context import (  # type: ignore
    get_geometry_constraints, KB_AVAILABLE as KB_CTX_AVAILABLE,
    KB_VERSION as KB_CTX_VERSION,
)
# M7/M9 (v4.02.75)：失败分类回流 + 健康度统计（供 /api/dsh/stats 与 /api/dsh/status）
from dsh_feedback_store import (  # type: ignore
    classify_failure as _classify_failure,
    record_outcome as _record_dsh_outcome,
    stats as _feedback_stats,
)


def get_feedback_stats(tail: int = 500) -> Dict[str, Any]:
    """DSH 生成健康度统计（M7/M9）：失败分类占比 / 几何校验通过率 / 最近一次口径。"""
    try:
        return _feedback_stats(tail)
    except Exception:
        return {}


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


DEFAULT_API_BASE = "http://127.0.0.1:3080"
DEFAULT_API_PATH = "/api/agent-teams/generate"
DEFAULT_PROFILE = "web"
# ═══════════════════════════════════════════════════════════════════════════
# v4.02.83: HTTP 通道默认【关闭】—— 实证 DSH 不提供该 HTTP 端点
# ---------------------------------------------------------------------------
# 证据 (2026-09-15 实测, @deepseek-ai/dsh @ npm-cache/_npx/1e7f6d9597241db0):
#   ① `agentTeams` 在 DSH 内是 **in-process cordis service** (dsh-tool-cordis),
#      不是 HTTP 路由; 全 scope 扫描仅命中该定义处。
#   ② DSH Web 服务唯一的 HTTP API 是 `/api/remote.mux` (dsh-api-gateway)。
#   ③ dsh web 自带 process-token 浏览器鉴权 (dsh-client-connection::BrowserAuth):
#      无签名 cookie 时 **`/api/` 下任意路径(含不存在的路径)一律 401**
#      (body=`unauthorized`); 根路径 401 正文为
#      "dsh web authentication required; reopen the URL printed by dsh web."
#      ⇒ 401 无法证明目标端点存在, 旧判据(非 404/405/501 即视为可用)必误判;
#        实测 `POST /api/__not_exist__` 与 `POST /api/agent-teams/generate`
#        返回完全相同的 401。
#   ④ launchToken 由 WeakMap(进程内存)随机生成, 无环境变量/无持久化 ⇒
#      MotorAI 无法自动取得, HTTP 通道不可达。
# 结论: 关闭 HTTP 通道 → 直接走 CLI, 省掉每轮一次注定失败的 POST 与 3s 探测。
# 还原: 置 DSH_HTTP_ENABLED=1 (若 DSH 未来真的提供该 API)。
# ═══════════════════════════════════════════════════════════════════════════
HTTP_ENABLED = _env_bool("DSH_HTTP_ENABLED", False)
# CLI 一次性任务模式必须用 headless（`dsh --profile headless "task"` 回答后退出）；
# 用 web 会启动长驻 Web 服务并卡到超时。
DEFAULT_CLI_PROFILE = "headless"
# M12 (超时口径统一): 与 runner Scripts/_run_dsh_100_test.py::PER_CASE_TIMEOUT 对齐，
# 避免「文档 180 / 运行 300 / runner 260」三口径分裂导致 ok=False 偏高的假超时。
# v4.02.83 (超时实证抬升 260 → 480): data/dsh_feedback.jsonl 13 条实测
#   · 成功率 6/13 = 46.2%
#   · **失败 100% 是 timeout**，全部卡在 260.1~262.3s（即上限本身）
#   · 成功样本耗时 min 129.2 / 中位 200.9 / max 367.8 / 均值 210.8s
#   ⇒ 260s 上限正好砍在成功分布的中位数上，且已有成功样本耗时 367.8s。
#   抬到 480s 覆盖成功分布尾部；代价为单例最坏墙钟 260→480s。
#   需同步 runner 的 PER_CASE_TIMEOUT；可用 DSH_TIMEOUT_S 覆盖，置 260 回旧口径。
DEFAULT_TIMEOUT_S = 480
# M8 (重试职责分层): agent 层默认【不重试】(CLI_MAX_ATTEMPTS=1)，重试职责上移到 runner
# (限流类指数退避) —— 使单例最坏墙钟 = timeout，可预测；可用 DSH_CLI_MAX_ATTEMPTS 覆盖。
CLI_MAX_ATTEMPTS = _env_int("DSH_CLI_MAX_ATTEMPTS", 1)
# M2: 确定性几何校验（Core/verify_engine.geometry_self_check）+ 一次重生成闭环
CHECKER_ENABLED = _env_bool("DSH_CHECKER_ENABLED", True)
CHECKER_MAX_REGEN = _env_int("DSH_CHECKER_MAX_REGEN", 1)

# M12 (输出预算分级): 按超时档位给出报告字数额度，避免「≥5000 字」在 260s 内写不完 → 截断/超时。
_OUTPUT_BUDGET_TIERS = (
    (200, 2200),   # timeout < 200s  → ≥2200 字
    (260, 3200),   # 200~260s        → ≥3200 字
    (320, 4500),   # 260~320s        → ≥4500 字
    (10 ** 9, 5000),  # >320s        → ≥5000 字
)


def _output_budget_for_timeout(timeout_s: Any) -> int:
    """按生成超时档位返回报告字数额度（M12 输出预算分级）。"""
    try:
        t = float(timeout_s)
    except (TypeError, ValueError):
        t = float(DEFAULT_TIMEOUT_S)
    for limit, budget in _OUTPUT_BUDGET_TIERS:
        if t < limit:
            return budget
    return _OUTPUT_BUDGET_TIERS[-1][1]

# npx 缓存中 dsh 二进制的候选探测路径（按优先级）
_NPX_CACHE = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Public\AppData\Local"),
    "npm-cache", "_npx")


def _detect_cli_bin() -> str:
    """探测 npx 缓存中的 dsh 启动器 bin.js；未命中返回空串。

    事实（见 dsh-profile-repair 技能）：@deepseek-ai/* 只住在 npx 缓存
    ``<LOCALAPPDATA>/npm-cache/_npx/<hash>/node_modules/@deepseek-ai/dsh/lib/bin.js``，
    profile 的 node_modules 下没有该 scope。
    """
    explicit = (os.environ.get("DSH_CLI_BIN") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    if not os.path.isdir(_NPX_CACHE):
        return ""
    try:
        import glob
        cands = sorted(
            glob.glob(os.path.join(_NPX_CACHE, "*", "node_modules",
                                   "@deepseek-ai", "dsh", "lib", "bin.js")),
            key=lambda p: os.path.getmtime(p), reverse=True)
        return cands[0] if cands else ""
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# 物理约束自检（M1/M11）：区间由 KB 动态生成，越界必重算
# ----------------------------------------------------------------------------
def _physics_self_check_lines(specs: Dict[str, Any], power_class: str,
                              torque_hint: str, f_heff: bool) -> List[str]:
    """构建【物理约束自检】段落（KB 动态区间）。v1/v2 提示词共用，保证硬编码残留=0。"""
    kb = get_geometry_constraints(specs)
    od_lo, od_hi = kb["stator_od_range"]
    nominal = kb.get("nominal_od_mm")
    nominal_txt = (f"（由 P/n 反推名义外径 ≈ {nominal:.0f} mm，允许 ±18%）"
                   if nominal else "（无功率/转速时按应用域经验区间）")
    lines = [
        f"=== 物理约束自检（应用域：{kb['application']}；阈值来源：{kb['source']}）{power_class} ===",
        "",
        f"- 定子外径必须落在 [{od_lo}, {od_hi}] mm 区间{nominal_txt}；超出即判不合理，必须重算。",
        f"- 定子内径必须落在 [{kb['stator_id_range'][0]}, {kb['stator_id_range'][1]}] mm 区间，且 < 定子外径。",
        f"- 铁心长必须落在 [{kb['core_length_range'][0]}, {kb['core_length_range'][1]}] mm 区间。",
        f"- 气隙长度必须落在 [{kb['air_gap_range'][0]}, {kb['air_gap_range'][1]}] mm 工程合理区间。",
        f"- 槽数取整且落在 [{kb['slots_range'][0]}, {kb['slots_range'][1]}] 常规范围；优先极数 {kb['poles_preferred']}。",
        f"- 线负荷 A 应落在 [{kb['line_load_A_A_per_m'][0]}, {kb['line_load_A_A_per_m'][1]}] A/m；"
        f"气隙磁密 B_δ 应落在 [{kb['air_gap_flux_density_T'][0]}, {kb['air_gap_flux_density_T'][1]}] T；"
        f"电密 J₁ 应落在 [{kb['current_density_j1_A_per_mm2'][0]}, {kb['current_density_j1_A_per_mm2'][1]}] A/mm²。",
        "- 效率/功率因数/温升量级须符合该功率转速档位的工程常识（温升不可为负或超绝缘限值）。",
    ]
    if torque_hint:
        lines.append(f"- {torque_hint}。")
    if f_heff:
        lines.append(f"- 效率为硬约束（一票否决），最终方案效率必须满足需求下限（如 ≥{int(kb['efficiency_target'] * 100)}%）。")
    if not kb["kb_available"]:
        lines.append("- ⚠️ 知识库不可用，已回退内置默认区间；几何区间可能与真实机型偏差，结果仅供参考。")
    lines.append("")
    return lines


# ----------------------------------------------------------------------------
# 提示词构建：把 MotorAI 的 specs 转成 DSH 一次性任务描述
# ----------------------------------------------------------------------------
def build_design_prompt(specs: Dict[str, Any]) -> str:
    """把 MotorAI 需求规格翻译成 DSH agent-teams 可执行的中文任务描述。

    v4.02.49: 优先使用原始需求原文(raw_text)直传，不展开结构化字段；
    仅在无原文时回退到结构化字段列表。
    明确要求 DSH 在末尾给出一个 ```json 代码块，便于本模块做 schema 映射。
    """
    def _g(*keys, default=""):
        for k in keys:
            v = specs.get(k)
            if v not in (None, "", []):
                return v
        return default

    raw_text = _g("raw_text", "requirement", default="")

    lines = [
        "你是资深电机电磁设计工程师。请为下列需求给出一套完整的电机设计方案。",
        "",
    ]

    if raw_text:
        # 纯原始文本直传：直接用需求原文，交由 LLM 自行理解
        lines += ["【需求原文】", raw_text]
    else:
        # 无原文时回退到结构化字段
        power = _g("power_kw", "power", default="未指定")
        speed = _g("speed_rpm", "speed", default="未指定")
        volt = _g("voltage", "rated_voltage", default="未指定")
        torque = _g("torque", "rated_torque", default="未指定")
        mtype = _g("motor_type", "type", default="未指定")
        poles = _g("poles", default="未指定")
        freq = _g("frequency", default="未指定")
        cooling = _g("cooling_method", "cooling", default="未指定")
        duty = _g("duty", "scenario", default="未指定")
        lines += [
            "【需求规格】",
            f"- 电机类型: {mtype}",
            f"- 额定功率: {power} kW",
            f"- 额定电压: {volt} V",
            f"- 额定转速: {speed} rpm",
            f"- 额定转矩: {torque} N·m",
            f"- 极数: {poles}",
            f"- 频率: {freq} Hz",
            f"- 冷却方式: {cooling}",
            f"- 工作制/工况: {duty}",
        ]

    lines += [
        "",
        "【交付要求】",
        "1. 给出主要电磁与结构尺寸（定子外径/内径、铁心长、气隙、槽数、极数、"
        "轭厚、齿宽、每槽匝数等），并说明取值依据。",
        "2. 估算效率、功率因数、温升、转矩密度、材料用量与成本区间。",
        "3. 指出关键风险与可制造性约束。",
        "4. 若需求参数之间存在物理冲突，明确指出并给出折中建议。",
        "",
    ]
    # M1/M11：物理约束自检（KB 动态区间）注入到 v1 默认提示词，避免硬编码残留
    lines += _physics_self_check_lines(specs, "", "", False)
    lines += [
        "【输出格式】先用中文分节论述，最后必须输出一个 ```json 代码块，"
        "键名与含义如下（数值用数字，缺失项填 null）：",
    ]
    # M3：schema 块由 dsh_schema.json_schema_block() 生成，与 v2 / SCHEMA_MAP / 门控同源
    lines += json_schema_block().splitlines()
    return "\n".join(lines)


def build_design_prompt_v2(specs: Dict[str, Any],
                           output_budget_chars: Optional[int] = None) -> str:
    """v4.02.71 强化版（全域扫描法 + 强制思维链 + 物理约束自检 + JSON 自检 + 末尾 JSON 强制提醒）：将 MotorAI specs 翻译为提示词，强制走
    Step 1-7 九步流程（需求定义→拓扑初定→参数建模→全域扫描→
    约束筛选→敏感度分析→方案收敛），要求 LLM 输出 Top-5 方案对比
    + 敏感度排序，保持 12 键 JSON schema 兼容。

    与 v1 区别：
    - 方法学：LLM 经验推导 → 全域扫描法（系统性参数搜索）
    - 变量：隐式 → 8-12 个显式设计变量 + 扫描网格
    - 方案：单方案 → Top-5 方案对比 + 最优推荐
    - 优化：单目标 → 硬约束一票否决 + 多目标排序函数
    - 诊断：无 → OAT 单因素扰动 + 敏感度系数

    JSON schema 完全兼容 v1（12 键 + null 缺省），下游解析零改动。
    """
    def _g(*keys, default=""):
        for k in keys:
            v = specs.get(k)
            if v not in (None, "", []):
                return v
        return default

    raw_text = _g("raw_text", "requirement", default="")

    # M12（输出预算分级）：字数下限随超时档位给出，避免在短超时下强求 5000 字导致截断/超时
    _budget = int(output_budget_chars or 5000)

    # ---- 需求特征检测（用于条件注入 VFD/IP67/水冷/低速大转矩等专属约束）----
    try:
        _feat = (raw_text or "") + " " + json.dumps(specs, ensure_ascii=False)
    except Exception:
        _feat = raw_text or ""

    def _has(*kws):
        return any(k in _feat for k in kws)

    f_vfd = _has("变频", "逆变", "变频器", "VFD", "PWM")
    f_ip67 = _has("IP67", "ip67", "防护等级")
    f_water = _has("水冷", "液冷")
    f_lowtorque = _has("低速", "低转速", "大转矩", "大扭矩")
    f_heff = _has("效率", "≥96", ">=96", "96%")

    # ---- 提取 功率/转速（structured 优先，其次原文正则），用于推导额定转矩 ----
    def _fnum(v):
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    power = _fnum(_g("power_kw", "power", default=None))
    speed = _fnum(_g("speed_rpm", "speed", default=None))
    if power is None:
        _m = re.search(r"(\d+(?:\.\d+)?)\s*[kK][wW]", _feat)
        power = float(_m.group(1)) if _m else None
    if speed is None:
        _m = re.search(r"(\d+(?:\.\d+)?)\s*[rR][pP][mM]", _feat)
        speed = float(_m.group(1)) if _m else None
    torque_hint = ""
    power_class = ""
    if power and speed:
        try:
            _tq = 9550.0 * float(power) / float(speed)
            torque_hint = (f"额定转矩目标 ≈ 9550×P/n = 9550×{power:g}/{speed:g} "
                           f"≈ {_tq:.0f} N·m（据此反推 D²L 量级与转矩密度）")
            power_class = f"（给定 {power:g} kW / {speed:g} rpm 档位）"
        except Exception:
            pass

    lines = [
        "你是资深电机电磁设计工程师，精通全域扫描法设计流程。",
        "你必须【严格执行】下列七步全域扫描法，禁止跳过任何一步直接输出结果。",
        "",
        "【强制思维链】在输出最终 JSON 前，必须先在 <thinking> ... </thinking> 标签内完成推理：",
        "1. 需求拆解：列出全部关键约束（功率/转速/电压/效率/防护/冷却/绝缘等），区分硬约束与软目标。",
        "2. 多方案生成：至少构思 3 种电磁方案（不同槽极配合/绕组型式/冷却方式）。",
        "3. 对比评估：从转矩密度、效率、温升、成本角度对比各方案。",
        "4. 迭代优化：选择最优方案并微调参数以满足效率等硬约束。",
        "5. 物理校验：核对定子外径/内径/铁心长/气隙是否落在合理量级（见下文【物理约束自检】）。",
        "6. 最终决策：确定唯一设计方案。",
        "7. JSON 生成：基于最终决策生成 12 键 JSON，并通过自检（见下文【JSON 自检】）。",
        "",
    ]

    if raw_text:
        # 分支A：纯原始文本直传（与 v1 一致）
        lines += ["【需求原文】", raw_text]
    else:
        # 分支B：结构化字段回退（与 v1 一致）
        power = _g("power_kw", "power", default="未指定")
        speed = _g("speed_rpm", "speed", default="未指定")
        volt = _g("voltage", "rated_voltage", default="未指定")
        torque = _g("torque", "rated_torque", default="未指定")
        mtype = _g("motor_type", "type", default="未指定")
        poles = _g("poles", default="未指定")
        freq = _g("frequency", default="未指定")
        cooling = _g("cooling_method", "cooling", default="未指定")
        duty = _g("duty", "scenario", default="未指定")
        lines += [
            "【需求规格】",
            f"- 电机类型: {mtype}",
            f"- 额定功率: {power} kW",
            f"- 额定电压: {volt} V",
            f"- 额定转速: {speed} rpm",
            f"- 额定转矩: {torque} N·m",
            f"- 极数: {poles}",
            f"- 频率: {freq} Hz",
            f"- 冷却方式: {cooling}",
            f"- 工作制/工况: {duty}",
        ]

    # 全域扫描法九步流程（融入 v1 三条交付要求 + 新增 4 步）
    lines += [
        "",
        "=== 全域扫描法七步设计流程 ===",
        "",
        "【Step 1: 需求与约束定义】",
        "- 拆解用户需求，区分硬约束（一票否决，如效率/IP67/绝缘等级）与软目标（参与排序）。",
        "- 输出约束清单：硬约束 H1~Hn（[约束名, 限值, 单位]）；软目标 O1~Om（[目标名, 权重w]）。",
        "- 识别约束冲突（如效率 vs 起动转矩、IP67 vs 散热）。",
        "",
        "【Step 2: 拓扑与尺寸初定】",
        "- 确定极数/槽配合/绕组型式（分布/集中，分数槽/整数槽）。",
        "- 用 D²L 方程估算尺寸量级，给出初始尺寸范围（含理由）。",
        "",
        "【Step 3: 参数化建模（定义 8-12 个设计变量 + 扫描网格）】",
        "按下面的表格格式明确列出每个设计变量的扫描网格（至少含符号、单位、"
        "可选值列表、取值数 4 列）：",
        "| 变量名 | 符号 | 单位 | 扫描网格 | 取值数 |",
        "| --- | --- | --- | --- | --- |",
        "| 定子内径 | D_i1 | mm | ... | ... |",
        "| 铁心长度 | L | mm | ... | ... |",
        "| 气隙磁密 | B_δ | T | ... | ... |",
        "| 线负荷 | A | A/m | ... | ... |",
        "| 定子电密 | J₁ | A/mm² | ... | ... |",
        "| 气隙长度 | δ | mm | ... | ... |",
        "| 槽数 | Q₁ | - | ... | ... |",
        "| 极数 | 2p | - | ... | ... |",
        "（其余变量如转子结构、磁材牌号、每槽匝数、并联支路数按需补全）",
        "- 必须显式给出【组合总数】= ∏Nᵢ 的具体数值（如 2×3×2×2×2 = 48 组），"
        "并据此确认枚举覆盖度。",
        "- 必须给出【枚举表】：将全部组合以表格逐一列出（候选号 / 各变量取值），"
        "不得仅文字描述而省略枚举（measure 10 证据链·枚举表）。",
        "",
        "【Step 4: 全域扫描】",
        "- 网格枚举所有设计变量组合（共 N₁×N₂×...×Nₙ 组）。",
        "- 对每组参数进行完整电磁计算（磁路+电路+热路耦合）。",
        "- 记录每组方案的性能数据：效率、功率因数、温升、转矩密度、起动转矩倍数、"
        "材料用量、成本区间。",
        "- 必须产出【结果矩阵】：以表格呈现每个组合（候选号 / 关键尺寸 / 效率 / 功率因数 / 温升 / 成本），"
        "不得只给结论而隐藏中间计算（measure 10 证据链·结果矩阵）。",
        "- 必须做【冲突引用】：当硬约束 Hᵢ 与软目标 Oⱼ 或另一硬约束冲突时，明确引用冲突双方及其量化影响"
        "（如『H2 效率≥96% 与 O3 低成本 冲突：提效需增磁材→成本 +12%』），"
        "不得笼统说『存在冲突』（measure 10 证据链·冲突引用）。",
        "",
        "【Step 5: 约束筛选与排序】",
        "- 第一道硬约束漏斗：剔除不满足 H1~Hn 的方案，给出被剔除原因。",
        "- 第二道多目标排序函数：score = w₁×η + w₂×cosφ + w₃×(T_st/I_st) - w₄×cost - w₅×mass",
        "  按 score 降序，输出 Top-5 可行方案对比表（方案号/尺寸/效率/功率因数/温升/成本/评分/简述）。",
        "- 推荐其中 1 个最优方案，给出具体理由（≥3 条）。",
        "",
        "【Step 6: 敏感度分析】",
        "- 对推荐方案执行 OAT 单因素扰动（每个变量在网格范围内 ±1 档）。",
        "- 计算敏感度系数 Sᵢ₀ = (Δ指标/指标基线) / (Δ变量/变量基线)，识别 |Sᵢ₀|>1 的关键变量。",
        "- 输出关键变量排序（按 |Sᵢ₀| 降序）并指出哪些是设计主控点。",
        "",
        "【Step 7: 方案收敛】",
        "- 统一冲片/绕组/磁材牌号，形成 A/B 双方案（兼顾效率优先 vs 成本优先）。",
        "- 给出最终方案参数明细表 + 折中说明（针对 Step 1 识别的冲突）。",
        "",
    ]

    # ---- 物理约束自检（M1/M11：区间由 KB 动态生成，越界必重算，硬编码残留=0）----
    lines += _physics_self_check_lines(specs, power_class, torque_hint, f_heff)

    # ---- 条件注入：变频/IP67/水冷/低速大转矩（measure 6/9）----
    if f_vfd or f_water or f_ip67 or f_lowtorque:
        lines += ["=== 工况/防护专项要求 ===", ""]
        if f_vfd:
            lines += [
                "- 变频驱动：H 级绝缘增强耐电压冲击能力；考虑谐波损耗对效率的影响（基波效率须达标）；",
                "  关注轴电流与绝缘风险，必要时说明抑制措施。",
            ]
        if f_lowtorque:
            lines.append("- 低速大转矩：需大转矩设计，转矩密度 > 15 N·m/L；校验起动/过载能力与机械强度。")
        if f_water:
            lines.append("- 水冷：明确水冷通道位置（定子机座/端盖）、冷却介质流量与温升计算、密封结构要点。")
        if f_ip67:
            lines.append("- IP67：明确密封结构设计要点与防护等级验证方法。")
        lines.append("")

    lines += [
        "=== 交付要求 ===",
        "",
        "1. 给出主要电磁与结构尺寸（定子外径/内径、铁心长、气隙、槽数、极数、"
        "轭厚、齿宽、每槽匝数等），并说明取值依据。",
        "2. 估算效率、功率因数、温升、转矩密度、材料用量与成本区间。",
        "3. 指出关键风险与可制造性约束。",
        "4. 若需求参数之间存在物理冲突，明确指出并给出折中建议。",
        "5. 给出 Top-5 方案对比表（含方案号/核心尺寸/效率/功率因数/温升/估算成本/总评分/推荐理由），"
        "并推荐 1 个最优方案（理由 ≥3 条）。",
        "6. 列出关键设计变量敏感度排序（按 |Sᵢ₀| 降序），标注哪些变量为设计主控点。",
        "",
        "=== 报告结构（防短路 + 可读性，measure 4/7）===",
        "报告须含以下章节且每章有实质内容：1.需求分析 2.方案对比(≥3种) 3.最终方案详细设计 "
        "4.电磁计算验证 5.热分析验证 6.结构分析验证 7.结论与建议。",
        f"禁止输出简短或截断的响应，报告总字数不得少于 {_budget} 字；不足时须补充详细分析。",
        "对存在设计不确定性的参数，须在 <thinking> 中标注假设并说明对性能的影响（measure 8）。",
        "",
        "=== 输出格式（强制，measure 10）===",
        "严格按以下顺序输出，json 代码块之后不得有任何额外文本：",
        "1. <thinking> 你的完整推理过程（含 Step1-7 与物理校验）</thinking>",
        f"2. 中文分节论述的报告正文（≥{_budget} 字，含上述 7 章 + Top-5 表 + 敏感度排序）。",
        "3. 一个 ```json 代码块，键名与含义如下（数值用数字，slots_stator/poles 为整数）：",
    ]
    # M3：schema 块由 dsh_schema.json_schema_block() 生成，与 v1 / SCHEMA_MAP / 门控同源
    lines += json_schema_block().splitlines()
    lines += [
        "",
        "=== JSON 自检（输出前逐条核对，measure 2）===",
        "1. 12 键全部存在（10 个数值键不允许为 null；summary/risks 必须填写）。",
        "2. slots_stator、poles 必须为整数；其余数值为数字。",
        "3. stator_od_mm > stator_id_mm > 0。",
        "4. air_gap_mm 在 0.5 ~ 5.0 之间。",
        "5. efficiency_pct 在 0~100 且满足需求效率下限。",
        "6. JSON 合法：括号闭合、无截断、代码块之后无多余文本。",
        "若任一条不满足，须修正后重新输出。",
        "",
        "=== ⚠️ 最后一步（强制，否则本次输出视为无效）===",
        "写完上述报告正文后，你【必须】再输出一个 ```json 代码块，作为整篇输出的最后一段。",
        "该 JSON 块是交付物的必需组成部分：不得省略、不得用表格代替、不得以“详见上表”搪塞；",
        "即使正文已经很长（如三四万字），也必须在结尾补上该 JSON 块。json 代码块之后不得再有任何字符。",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 结果 schema 映射：DSH 输出 -> MotorAI 结果 schema
# ----------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _balanced_json_at(text: str, start: int) -> Optional[Dict[str, Any]]:
    """从 text[start]（应为 '{'）做括号配对并 json 解析；失败或非 dict 返回 None。"""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(text[start:i + 1])
                except Exception:
                    return None
                return v if isinstance(v, dict) else None
    return None


# 设计 JSON 核心键（来自 dsh_schema.JSON_CORE_KEYS / JSON_AUX_KEYS / KV_MIN_CORE）
# ——用于给候选 dict 可信度打分，定义统一在 Agents/dsh_schema.py（M3 单一真相源）。

# DSH 工具调用语法泄漏：<｜DSH｜invoke …>…</｜DSH｜>（竖线为全角 U+FF5C，兼容半角）
_DSH_INVOKE_RE = re.compile(r"<[|｜]\s*DSH\s*[|｜].*?</[|｜]\s*DSH\s*[|｜]>", re.S)


def _strip_dsh_invoke(text: str) -> str:
    """剥离 DSH 工具调用语法泄漏。

    实测 DSH 偶把内部工具调用写进最终输出，形如
      `<｜DSH｜invoke name="read" params>{"file_path": "…package.json"}</｜DSH｜>`
    其中的小 JSON 会被误当作设计 JSON 抽走 → 先剔除。
    """
    if not text or "DSH" not in text:
        return text
    return _DSH_INVOKE_RE.sub("\n", text)


def _json_core_score(d: Any) -> int:
    """候选 dict 的"设计 JSON 可信度"= 命中的核心键个数（空值不计）。"""
    if not isinstance(d, dict):
        return 0
    n = 0
    for k in JSON_CORE_KEYS:
        v = d.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        n += 1
    return n


def _parse_kv_lines(text: str) -> Optional[Dict[str, Any]]:
    """兜底解析纯文本 `key = value` 行（模型偶尔不写 JSON 代码块）。

    仅当命中 >= KV_MIN_CORE 个核心键时采纳，避免误吞正文中的等号语句。
    """
    vals: Dict[str, Any] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if not m:
            continue
        k, raw = m.group(1), m.group(2)
        if k not in JSON_CORE_KEYS and k not in JSON_AUX_KEYS:
            continue
        if k in vals and vals[k] not in (None, "", []):
            continue
        v: Any = raw
        try:
            v = json.loads(raw)
        except Exception:
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
                v = raw[1:-1]
        vals[k] = v
    hit = sum(1 for k in JSON_CORE_KEYS if k in vals)
    return vals if hit >= KV_MIN_CORE else None


def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """从 DSH 输出中抽取**设计 JSON**；按可信度打分择优，而非"首个非空 dict"。

    顺序（v4.02.72 修 B 副作用）：
      0) 先剥离 DSH 工具调用语法泄漏 `<｜DSH｜invoke …>…</｜DSH｜>`；
      1) 收集所有 ```json/``` 围栏块 + 所有 '{' 括号配对候选，逐个解析；
      2) 按 `_json_core_score`（命中设计核心键个数）打分，**取分最高者**；
      3) 若最高分为 0，退到纯文本 `key = value` 行兜底（需 ≥6 个核心键）；
      4) 仍无则返回空 dict（若有）或 None。
    设计要点：旧版（v4.02.71）"遍历候选、优先非空 dict"会被正文里的无关小 JSON
      （如泄漏的 `{"file_path": "…package.json"}`）劫持 → design_raw 变空、JSON 门控误判 False。
      现以"核心键命中数"为准绳，无关小 JSON 得 0 分自然落选。
    """
    if not text:
        return None
    text = _strip_dsh_invoke(text)
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    first_empty: Optional[Dict[str, Any]] = None

    def _consider(v: Any) -> None:
        nonlocal best, best_score, first_empty
        if not isinstance(v, dict):
            return
        if not v:
            if first_empty is None:
                first_empty = v
            return
        s = _json_core_score(v)
        if s > best_score:
            best, best_score = v, s

    # 1) 围栏代码块
    for m in _JSON_BLOCK_RE.finditer(text):
        try:
            v = json.loads(m.group(1))
        except Exception:
            continue
        _consider(v)

    # 2) 遍历所有 '{' 起点做括号配对
    start = text.find("{")
    while start != -1:
        _consider(_balanced_json_at(text, start))
        start = text.find("{", start + 1)

    # 2.5) 核心键命中者优先
    if best is not None and best_score > 0:
        return best

    # 3) 纯文本 `key = value` 兜底（模型偶尔不写 JSON 代码块）
    kv = _parse_kv_lines(text)
    if kv is not None:
        return kv

    # 4) 无核心键命中的非空 dict / 空 dict / None
    return best if best is not None else first_empty


# DSH JSON 键 -> MotorAI 结果 schema 键（统一来自 dsh_schema.SCHEMA_MAP，M3 单一真相源）
# 注意：temperature_rise_k 映射到 max_temp（修复原 max_temp_rise 错位）。


# ----------------------------------------------------------------------------
# v4.02.77: 报告 Markdown 清洗（修前端「窄条表格碎片 + 大量空段」）
# ----------------------------------------------------------------------------
# 根因（2026-09-14 实锤，见 Scripts/_cmp_think_wrap.py 对照实验）：
#   DSH 输出以 <thinking> 开头（提示词强制思维链）。QTextEdit.setMarkdown 把
#   <thinking>...</thinking> 当作 HTML 块上下文，块内 Markdown 表格被降级渲染成
#   1 字宽碎片、标题/列表丢失、段落稀疏 → 前端「大量空段」。
#   实测仅去掉包裹标签（保留思维链内容），表格即恢复全宽正常渲染。
# 附带治理：表格被空行切断（渲染成残片）、连续空行、CR/ANSI/零宽字符污染。
_THINK_TAG_RE = re.compile(r"</?think(?:ing)?>", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ZWCHAR_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def _sanitize_report_markdown(text: str) -> str:
    """清洗 DSH 报告 Markdown，使其在 QTextEdit.setMarkdown 下正常渲染。

    规则（均不改变正文本义，M10 证据链关键词不受影响）：
      1. 去掉 <thinking>/</thinking>（含 <think>）包裹标签——根因修复；
      2. \r\n / 孤立 \r → \n；去 ANSI 转义与零宽字符；
      3. 表格行内全角 ｜ → 半角 |（仅当该行含 ≥2 个 ｜）；
      4. 代码围栏外：表格行组之间的空行剔除（空行会把一张表切成多个残片）；
      5. 连续 ≥2 空行压缩为 1（消除前端空段）。
    """
    if not text:
        return ""
    s = _THINK_TAG_RE.sub("", text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _ANSI_RE.sub("", s)
    s = _ZWCHAR_RE.sub("", s)

    lines = s.split("\n")
    # 全角管道归一（表格候选行）
    lines = [ln.replace("｜", "|") if ln.count("｜") >= 2 else ln for ln in lines]

    def _is_fence(st: str, cur: str) -> str:
        t = st[:3]
        if t in ("```", "~~~"):
            return "" if cur else t
        return cur

    # Pass 1: 压缩连续空行（围栏内不动）
    out: List[str] = []
    fence = ""
    blank = 0
    for ln in lines:
        st = ln.strip()
        fence = _is_fence(st, fence)
        if fence:  # 围栏内原样保留
            out.append(ln)
            continue
        if not st:
            blank += 1
            if blank == 1:
                out.append("")
            continue
        blank = 0
        out.append(ln.rstrip())

    # Pass 2: 剔除表格行组之间的空行（围栏内不动）
    res: List[str] = []
    fence = ""
    i, n = 0, len(out)
    while i < n:
        st = out[i].strip()
        fence = _is_fence(st, fence)
        if not fence and st.startswith("|"):
            j = i
            block: List[str] = []
            while j < n:
                stj = out[j].strip()
                fence = _is_fence(stj, fence)
                if not fence and stj.startswith("|"):
                    block.append(out[j])
                    j += 1
                elif (not fence and not stj and j + 1 < n
                      and out[j + 1].strip().startswith("|")):
                    j += 1  # 表格内部空行：剔除
                else:
                    break
            res.extend(block)
            i = j
        else:
            res.append(out[i])
            i += 1

    return "\n".join(res).strip()


# ----------------------------------------------------------------------------
# v4.02.79: DSH「空报告 / 占位符」检测（fail-closed）
# ----------------------------------------------------------------------------
# 根因（2026-09-14 实锤）: ``~/.dsh/settings.yaml`` 把 agent-default-model 由
#   agnes-2.0-flash 改为 agnes-3.0-flash 后, agnes-cn「排队等待模式」网关对完整 DSH
#   请求回吐占位符 ``<loading>\n\n</loading>``（8 token / ~90ms / stopReason=stop,
#   会话证据 ~/.dsh/sessions/.../session-2e18cf1a/）。
#   旧逻辑: ``_call_cli`` 拿到非空 stdout 即判成功 → ok=True, report_chars=21
#   → 客户端「DSH 方案报告」显示空内容且无任何报错。
# 修复: 报告短于阈值或匹配占位符模板 → 判失败(fail-closed), 让客户端看到明确错误。
_MIN_REPORT_CHARS = 200
_PLACEHOLDER_RE = re.compile(
    r"^(?:<(?:loading|think|thinking)>\s*)+"
    r"(?:</(?:loading|think|thinking)>\s*)*$",
    re.IGNORECASE)


def _is_placeholder_report(report: str) -> bool:
    """报告是否为空/占位符（不可直接下发给客户端）。"""
    s = (report or "").strip()
    if len(s) < _MIN_REPORT_CHARS:
        return True
    return bool(_PLACEHOLDER_RE.match(s))


def map_to_motorai_schema(raw_text: str) -> Dict[str, Any]:
    """把 DSH 自然语言+可选 JSON 输出映射为 MotorAI 结果 schema。

    返回:
      {
        "ok": bool,            是否成功抽取到结构化字段
        "report": str,         完整原文（v4.02.77 起经 _sanitize_report_markdown 清洗，
                               供过程交付物直接展示）
        "design": {...},       映射后的结构化设计参数（仅含非空项）
        "design_raw": {...},   DSH 原始 JSON（未改名）
      }
    """
    report = _sanitize_report_markdown((raw_text or "").strip())
    parsed = _extract_json_block(report) or {}
    design: Dict[str, Any] = {}
    for src_key, dst_key in SCHEMA_MAP.items():
        v = parsed.get(src_key)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        design[dst_key] = v
    return {
        "ok": bool(parsed),
        "report": report,
        "design": design,
        "design_raw": parsed,
    }


# ----------------------------------------------------------------------------
# M2: 确定性几何校验（Core/verify_engine.geometry_self_check）+ 一次重生成闭环
# ----------------------------------------------------------------------------
def _load_geometry_checker():
    """惰性导入 Core.verify_engine.geometry_self_check；不可用返回 None。"""
    try:
        from Core.verify_engine import geometry_self_check  # type: ignore
        return geometry_self_check
    except Exception:
        # 独立运行（sys.path[0]=Agents）时按后端根补路径再试
        try:
            import sys as _sys
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from Core.verify_engine import geometry_self_check  # type: ignore
            return geometry_self_check
        except Exception:
            return None


def run_deterministic_check(design: Dict[str, Any]) -> Dict[str, Any]:
    """对 DSH 映射后的 design 做确定性几何/物理一致性校验（M2）。

    返回 ``{"available": bool, "ok": bool, "reasons": [str]}``。
    checker 不可用时 ``available=False`` 且 ``ok=True``（不阻断，仅标记）。
    复用主流程 ``Core/verify_engine`` 阈值（air_gap/lambda/极槽配合），使 DSH 与主流程同尺子。
    """
    fn = _load_geometry_checker()
    if fn is None:
        return {"available": False, "ok": True, "reasons": []}
    try:
        ok, reasons = fn(dict(design or {}))
        return {"available": True, "ok": bool(ok), "reasons": [str(r) for r in (reasons or [])]}
    except Exception as e:  # checker 自身异常不应阻断生成
        return {"available": False, "ok": True,
                "reasons": [f"checker 异常: {type(e).__name__}: {e}"]}


def _geometry_corrective_hint(reasons: List[str]) -> str:
    """把确定性校验失败原因转成一次重生成的纠错指令（M2 闭环）。"""
    items = "\n".join(f"- {r}" for r in (reasons or [])[:12])
    return (
        "\n\n=== ⚠️ 上一版方案未通过确定性几何校验，请修正后重新给出整套方案 ===\n"
        "程序校验发现以下几何/物理一致性问题：\n"
        f"{items}\n"
        "请严格修正上述问题（保持定子外径 > 定子内径 > 0、气隙与内外径自洽、"
        "长径比 λ 与极槽配合合理），重新输出完整的报告正文 + 末尾 12 键 JSON。"
    )


# ----------------------------------------------------------------------------
# DSH Agent
# ----------------------------------------------------------------------------
class DSHAgent:
    """DSH 集成封装 — 阶段一独立通路（HTTP 主 / CLI 备 / 失败降级）。"""

    name = "DSHAgent"
    description = "调用 DSH (DeepSeek Harness) agent-teams 生成电机设计方案"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.api_base = (cfg.get("api_base")
                         or os.environ.get("DSH_API_BASE")
                         or DEFAULT_API_BASE).rstrip("/")
        self.api_path = (cfg.get("api_path")
                         or os.environ.get("DSH_API_PATH")
                         or DEFAULT_API_PATH)
        self.profile = (cfg.get("profile")
                        or os.environ.get("DSH_PROFILE")
                        or DEFAULT_PROFILE)
        self.cli_profile = (cfg.get("cli_profile")
                            or os.environ.get("DSH_CLI_PROFILE")
                            or DEFAULT_CLI_PROFILE)
        self.timeout_s = int(cfg.get("timeout_s")
                             or _env_int("DSH_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        # M8 (重试职责分层): agent 层 CLI 尝试次数，默认 1（不重试），重试职责在 runner
        self.cli_max_attempts = int(cfg.get("cli_max_attempts")
                                    or _env_int("DSH_CLI_MAX_ATTEMPTS", CLI_MAX_ATTEMPTS))
        self.enabled = bool(cfg.get("enabled", _env_bool("DSH_ENABLED", True)))
        self.cli_bin = cfg.get("cli_bin") or _detect_cli_bin()
        self._status_cache: Optional[Dict[str, Any]] = None
        self._status_ts = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 状态探测
    def check_status(self, force: bool = False, ttl: float = 10.0) -> Dict[str, Any]:
        """探测 DSH 可用性。

        返回:
          {
            "available": bool, "mode": "http"|"cli"|"none",
            "base_url": str, "api_url": str, "profile": str,
            "cli_bin": str, "http_alive": bool, "cli_ready": bool,
            "reason": str, "elapsed_ms": int
          }
        """
        now = time.time()
        if not force and self._status_cache and (now - self._status_ts) < ttl:
            return dict(self._status_cache)

        t0 = time.time()
        http_alive = False
        reason = ""

        if not self.enabled:
            reason = "DSH_ENABLED=0，集成已关闭"
            result = self._pack_status(False, "none", http_alive, reason, t0)
        else:
            cli_ready = bool(self.cli_bin and os.path.isfile(self.cli_bin))
            # v4.02.83: HTTP 通道默认关闭 → 不做注定失败的探测, 直接 CLI
            if HTTP_ENABLED:
                http_alive, http_reason = self._probe_http()
            else:
                http_alive = False
                http_reason = "HTTP 通道已禁用 (DSH_HTTP_ENABLED=0)"
            if http_alive:
                mode = "http"
                reason = f"DSH Web 服务可达 ({self.api_base})"
            elif cli_ready:
                mode = "cli"
                reason = (f"HTTP 通道不可用({http_reason})，"
                          f"回退 CLI 一次性任务模式(profile={self.cli_profile})")
            else:
                mode = "none"
                reason = (f"HTTP 不可用({http_reason})；"
                          f"且未探测到 dsh CLI 二进制(请设 DSH_CLI_BIN)")
            result = self._pack_status(mode != "none", mode, http_alive, reason, t0)
            result["cli_ready"] = cli_ready

        with self._lock:
            self._status_cache = dict(result)
            self._status_ts = time.time()
        return result

    def _pack_status(self, available, mode, http_alive, reason, t0) -> Dict[str, Any]:
        return {
            "available": available,
            "mode": mode,
            "base_url": self.api_base,
            "api_url": f"{self.api_base}{self.api_path}",
            "profile": self.profile,
            # v4.02.83: 补充 CLI profile 字段 —— 旧输出只有 `profile`(=HTTP 用 web),
            #   而实际 CLI 走 headless, 易被误读为"CLI 也在用 web"(web 会起长驻服务并卡到超时)。
            "cli_profile": self.cli_profile,
            "cli_bin": self.cli_bin,
            "http_alive": http_alive,
            "cli_ready": bool(self.cli_bin and os.path.isfile(self.cli_bin)),
            "timeout_s": self.timeout_s,
            "http_enabled": HTTP_ENABLED,
            "enabled": self.enabled,
            "reason": reason,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    def _probe_http(self, timeout: float = 3.0):
        """探测 DSH Web 服务。返回 (alive, reason)。

        直接探测生成端点 (POST {api_path})，而不是仅探测根路径 GET /。
        根路径是 SPA 静态页(200)，会误判 HTTP 通道可用，但真实端点
        /api/agent-teams/generate 可能 404/405，导致每次生成都先走一次
        注定失败的 HTTP 调用再回退 CLI，浪费一轮且污染进度日志。

        v4.02.83 判据修正：**401/403 一律判为不可用**。dsh web 的
        BrowserAuth 对 /api/ 下任意路径(含不存在路径)都返回 401
        (`unauthorized`)，因此 401 既不能证明端点存在、也不是可用的信号；
        它是"缺少浏览器签名 cookie"的表现，而 MotorAI 无法取得 process-token。
        """
        url = f"{self.api_base}{self.api_path}"
        try:
            if _HAS_REQUESTS:
                r = requests.post(url, json={"message": "ping", "profile": self.profile},
                                  timeout=timeout)
                if r.status_code in (401, 403):
                    return False, (f"需浏览器令牌 (POST {self.api_path} -> "
                                   f"{r.status_code}: 无签名 cookie)")
                # 端点存在（非 404/405/501）即视为 HTTP 通道可用
                if r.status_code not in (404, 405, 501):
                    return True, f"POST {self.api_path} -> {r.status_code}"
                return False, f"端点未实现 (POST {self.api_path} -> {r.status_code})"
            req = urllib.request.Request(  # noqa: S310
                url, data=json.dumps({"message": "ping", "profile": self.profile}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return True, f"POST {self.api_path} -> {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, (f"需浏览器令牌 (POST {self.api_path} -> "
                               f"{e.code}: 无签名 cookie)")
            if e.code not in (404, 405, 501):
                return True, f"POST {self.api_path} -> {e.code}"
            return False, f"端点未实现 (POST {self.api_path} -> {e.code})"
        except Exception as e:
            return False, f"{type(e).__name__}"

    # ---------------------------------------------------------------- 方案生成
    def generate_design(
        self,
        specs: Dict[str, Any],
        timeout_s: Optional[int] = None,
        on_progress: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        """调用 DSH 生成电机设计方案。

        返回（永不抛异常，fail-closed）:
          {
            "ok": bool, "mode": "http"|"cli"|"none",
            "report": str,              # DSH 原文（过程交付物直接展示）
            "design": {...},            # 映射到 MotorAI schema 的结构化字段
            "design_raw": {...},        # DSH 原始 JSON
            "prompt": str,              # 实际下发的任务描述
            "elapsed_s": float,
            "error": str                # 失败原因（ok=True 时为空）
          }
        """
        t0 = time.time()
        timeout = int(timeout_s or self.timeout_s)
        # M12: 输出预算按超时档位分级（短超时不强求 5000 字）
        output_budget = _output_budget_for_timeout(timeout)
        # v4.02.54: 支持通过 specs["prompt_version"]=="v2" 切换到全域扫描法版
        pv = (specs or {}).get("prompt_version", "").lower() if specs else ""
        if pv in ("v2", "domain_scan", "scanning"):
            prompt = build_design_prompt_v2(specs or {}, output_budget_chars=output_budget)
            prompt_ver = "v2_domain_scan"
        else:
            prompt = build_design_prompt(specs or {})
            prompt_ver = "v1_classic"

        def _prog(msg, level="info"):
            if on_progress:
                try:
                    on_progress(msg, level)
                except Exception:
                    pass

        st = self.check_status()
        if not st.get("available"):
            return self._fail(st.get("mode", "none"), prompt, t0,
                              st.get("reason", "DSH 不可用"),
                              prompt_ver=prompt_ver, timeout=timeout)

        mode = st.get("mode")
        _prog(f"DSH 通道: {mode}（{st.get('reason', '')}）", "info")

        # M8: 单次调用封装（HTTP→CLI 回退不跨调用重试；重试职责在 runner，最坏墙钟可预测）
        def _invoke(prm):
            m = mode
            if m == "http":
                r, e = self._call_http(specs, prm, timeout, _prog)
                if e:
                    _prog(f"HTTP 通道失败({e})，回退 CLI", "warning")
                    if st.get("cli_ready"):
                        r2, e2 = self._call_cli(prm, timeout, _prog)
                        if e2:
                            return "", f"{e}; {e2}", "cli"
                        return r2, "", "cli"
                    return "", e, "http"
            r, e = self._call_cli(prm, timeout, _prog)
            if e:
                return "", e, "cli"
            return r, "", m

        raw, err, used_mode = _invoke(prompt)
        if err:
            return self._fail(used_mode, prompt, t0, err,
                              prompt_ver=prompt_ver, timeout=timeout)
        mode = used_mode

        mapped = map_to_motorai_schema(raw)

        # v4.02.79: 空报告/占位符 → fail-closed（根因见 _is_placeholder_report 注释）
        if _is_placeholder_report(mapped.get("report", "")):
            _rep = (mapped.get("report") or "").strip()
            return self._fail(
                used_mode, prompt, t0,
                f"DSH 输出为空或占位符（{len(_rep)} 字: {_rep[:40]!r}），未产出有效报告；"
                f"疑似网关排队占位符(<loading>)，请检查 DSH agent-default-model 配置",
                prompt_ver=prompt_ver, timeout=timeout)

        # ---- M2: 确定性几何校验（程序拦截非法样例）+ 一次重生成闭环 ----
        chk = run_deterministic_check(mapped.get("design") or {})
        regenerated = False
        if (CHECKER_ENABLED and chk["available"] and not chk["ok"]
                and CHECKER_MAX_REGEN > 0):
            _prog(f"确定性几何校验未通过({len(chk['reasons'])} 项)，触发一次重生成："
                  f"{'; '.join(chk['reasons'][:3])}", "warning")
            raw2, err2, mode2 = _invoke(prompt + _geometry_corrective_hint(chk["reasons"]))
            if not err2:
                mapped2 = map_to_motorai_schema(raw2)
                chk2 = run_deterministic_check(mapped2.get("design") or {})
                if chk2["ok"] or len(chk2["reasons"]) < len(chk["reasons"]):
                    raw, mapped, chk, mode = raw2, mapped2, chk2, mode2
                    regenerated = True
                    _prog("重生成后确定性几何校验通过" if chk["ok"]
                          else f"重生成后仍有 {len(chk['reasons'])} 项几何问题",
                          "success" if chk["ok"] else "warning")

        _prog(f"DSH 生成完成（{len(mapped.get('report', ''))} 字，"
              f"结构化字段 {len(mapped.get('design', {}))} 项，"
              f"几何校验 {'通过' if chk['ok'] else '未通过'}）", "success")

        result = {
            "ok": True,
            "mode": mode,
            "report": mapped["report"],
            "design": mapped["design"],
            "design_raw": mapped["design_raw"],
            "structured": mapped["ok"],
            "prompt": prompt,
            "prompt_ver": prompt_ver,
            "kb_application": get_geometry_constraints(specs).get("application"),
            "kb_available": KB_CTX_AVAILABLE,
            "kb_version": KB_CTX_VERSION,
            # M2: 确定性校验结果（真实拦截痕迹）
            "checker_available": chk["available"],
            "checker_ok": chk["ok"],
            "checker_reasons": chk["reasons"],
            "regenerated": regenerated,
            # M8 / M12: 超时口径与最坏墙钟
            "timeout_s": timeout,
            "cli_max_attempts": self.cli_max_attempts,
            "worst_case_s": timeout * max(1, self.cli_max_attempts)
                            + (timeout if regenerated else 0),
            "output_budget_chars": output_budget,
            "report_chars": len(mapped.get("report", "")),
            "elapsed_s": round(time.time() - t0, 2),
            "error": "",
            "fail_class": "",
        }
        try:
            _record_dsh_outcome(result)   # M7: 成功结局回流
        except Exception:
            pass
        return result

    def _fail(self, mode, prompt, t0, error: str,
              prompt_ver: str = "v1_classic",
              timeout: int = 0) -> Dict[str, Any]:
        res = {
            "ok": False, "mode": mode, "report": "", "design": {},
            "design_raw": {}, "structured": False, "prompt": prompt,
            "prompt_ver": prompt_ver,
            "checker_available": False, "checker_ok": False,
            "checker_reasons": [], "regenerated": False,
            "timeout_s": timeout,
            "report_chars": 0,
            "fail_class": _classify_failure(error),   # M7: 失败分类
            "elapsed_s": round(time.time() - t0, 2), "error": error,
        }
        try:
            _record_dsh_outcome(res)   # M7: 失败结局回流
        except Exception:
            pass
        return res

    # ------------------------------------------------------------ 通道 A: HTTP
    def _call_http(self, specs, prompt, timeout, _prog):
        """POST 到 DSH Web 服务可配置端点。返回 (raw_text, error)。"""
        url = f"{self.api_base}{self.api_path}"
        payload = {
            "message": prompt,
            "specs": specs or {},
            "profile": self.profile,
            "stream": False,
        }
        _prog(f"POST {url} ...", "info")
        try:
            if _HAS_REQUESTS:
                r = requests.post(url, json=payload, timeout=timeout)
                code = r.status_code
                if code in (404, 405, 501):
                    return "", f"端点未实现 (HTTP {code})"
                if code >= 500:
                    return "", f"服务端错误 (HTTP {code})"
                if code >= 400:
                    return "", f"请求被拒 (HTTP {code}): {r.text[:200]}"
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    try:
                        data = r.json()
                        return self._flatten_http_payload(data), ""
                    except Exception:
                        return r.text or "", ""
                return r.text or "", ""
            req = urllib.request.Request(  # noqa: S310
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.read().decode("utf-8", "ignore"), ""
        except urllib.error.HTTPError as e:
            if e.code in (404, 405, 501):
                return "", f"端点未实现 (HTTP {e.code})"
            return "", f"HTTP {e.code}"
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"

    @staticmethod
    def _flatten_http_payload(data: Any) -> str:
        """把 DSH HTTP 响应的常见形态拍平成文本。"""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for k in ("output", "result", "content", "text", "message",
                      "response", "reply", "answer"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False, indent=2)
            return json.dumps(data, ensure_ascii=False, indent=2)
        if isinstance(data, list):
            return "\n".join(
                json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
                for x in data)
        return str(data)

    # -------------------------------------------------------------- 通道 B: CLI
    def _call_cli(self, prompt, timeout, _prog):
        """一次性任务模式: node <bin.js> --profile <p> "<task>"。

        依据 bin.js --help: `dsh --profile headless "run the tests"` 会
        "answer one task, print the result, and exit"。
        返回 (raw_text, error)。

        稳健性: 默认 self.cli_max_attempts=1（M8: 不重试，重试职责在 runner）；
        如需 agent 内重试可设 DSH_CLI_MAX_ATTEMPTS>1。超时若已有部分输出则直接利用。
        """
        bin_path = self.cli_bin
        if not (bin_path and os.path.isfile(bin_path)):
            return "", "未探测到 dsh CLI 二进制"
        _max_att = max(1, int(self.cli_max_attempts))
        args = [self._resolve_node(), bin_path,
                "--profile", self.cli_profile, prompt]
        for attempt in range(1, _max_att + 1):
            tag = f"(第{attempt}/{_max_att}次)" if _max_att > 1 else ""
            _prog(f"CLI 启动: node dsh --profile {self.cli_profile} "
                  f"(timeout={timeout}s){tag}", "info")
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                    encoding="utf-8", errors="ignore",
                    cwd=os.path.dirname(bin_path), shell=False,
                    env=self._cli_env())
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                if proc.returncode != 0 and not out:
                    tail = err[-400:] if err else f"exit={proc.returncode}"
                    if attempt < _max_att:
                        _prog(f"CLI 退出码 {proc.returncode}，重试...", "warning")
                        continue
                    return "", f"CLI 退出码 {proc.returncode}: {tail}"
                if not out and err:
                    # 部分 profile 把结果打到 stderr
                    out = err
                if not out:
                    if attempt < _max_att:
                        _prog("CLI 无输出，重试...", "warning")
                        continue
                    return "", "CLI 无输出"
                return out, ""
            except subprocess.TimeoutExpired as e:
                # 超时但已生成部分内容: 直接利用, 避免重跑
                partial = (getattr(e, "stdout", None) or "").strip() or \
                          (getattr(e, "stderr", None) or "").strip()
                if partial and len(partial) > 200:
                    _prog(f"CLI 超时但已有部分输出({len(partial)}字)，返回部分结果",
                          "warning")
                    return partial, ""
                if attempt < _max_att:
                    _prog(f"CLI 超时 ({timeout}s)，重试...", "warning")
                    continue
                return "", f"CLI 超时 ({timeout}s, 已重试{_max_att}次)"
            except FileNotFoundError:
                return "", "node 可执行文件未找到"
            except Exception as e:
                if attempt < _max_att:
                    _prog(f"{type(e).__name__}: {e}，重试...", "warning")
                    continue
                return "", f"{type(e).__name__}: {e}"
        return "", "CLI 失败"

    @staticmethod
    def _resolve_node() -> str:
        """定位 node 可执行文件（优先 managed / PATH）。"""
        for cand in ("node", "node.exe"):
            from shutil import which
            p = which(cand)
            if p:
                return p
        managed = r"C:\Users\15389\.workbuddy\binaries\node\versions\24.19.0\node.exe"
        return managed if os.path.isfile(managed) else "node"

    @staticmethod
    def _cli_env():
        env = os.environ.copy()
        # Git Bash 会把 /c/... 转成 \c\... 破坏模块解析；CLI 场景用裸 node 直调不受影响，
        # 但保险起见显式关闭路径转换。
        env["MSYS_NO_PATHCONV"] = "1"
        return env


# ----------------------------------------------------------------------------
# 模块级便捷入口
# ----------------------------------------------------------------------------
_AGENT_SINGLETON: Optional[DSHAgent] = None
_AGENT_LOCK = threading.Lock()


def get_dsh_agent(config: Optional[Dict[str, Any]] = None) -> DSHAgent:
    """进程内单例（配置仅首次生效）。"""
    global _AGENT_SINGLETON
    with _AGENT_LOCK:
        if _AGENT_SINGLETON is None:
            _AGENT_SINGLETON = DSHAgent(config)
        return _AGENT_SINGLETON


def check_status(force: bool = False) -> Dict[str, Any]:
    return get_dsh_agent().check_status(force=force)


def generate_design(specs: Dict[str, Any], timeout_s: Optional[int] = None,
                    on_progress: Optional[Callable[[str, str], None]] = None
                    ) -> Dict[str, Any]:
    return get_dsh_agent().generate_design(specs, timeout_s, on_progress)


# ----------------------------------------------------------------------------
# 自测: python dsh_agent.py
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 64)
    print("DSH Agent 自检 (阶段一)")
    print("=" * 64)
    ag = get_dsh_agent()
    st = ag.check_status(force=True)
    for k in ("available", "mode", "base_url", "api_url", "profile",
              "cli_bin", "http_alive", "cli_ready", "timeout_s", "reason"):
        print(f"  {k:14}: {st.get(k)}")
    print()

    print("--- schema 映射单测 ---")
    sample = """设计方案如下：定子外径 850mm，铁心长 600mm。
```json
{"stator_od_mm": 850, "stator_id_mm": 713, "core_length_mm": 600,
 "air_gap_mm": 1.5, "slots_stator": 72, "poles": 6,
 "efficiency_pct": 95.2, "torque_nm": 19100,
 "summary": "低速大转矩船推 PMSM", "risks": ["槽满率高", "温升偏紧"]}
```"""
    m = map_to_motorai_schema(sample)
    print("  ok:", m["ok"])
    print("  design:", json.dumps(m["design"], ensure_ascii=False))
    print("  risks 保留:", m["design"].get("risks"))
    print()
    print("--- 提示词预览 (前 6 行) ---")
    for ln in build_design_prompt(
            {"power_kw": 500, "speed_rpm": 250, "voltage": 690,
             "torque": 19100, "motor_type": "永磁同步电动机",
             "poles": 6}).splitlines()[:6]:
        print("   ", ln)
