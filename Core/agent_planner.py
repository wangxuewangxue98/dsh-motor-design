# -*- coding: utf-8 -*-
"""v4.02.12 (Phase 3): 自主智能体 — 规划器 Planner。

职责: 基于需求规格 + 记忆检索命中, 生成求解策略 (聚焦维度/步骤/风险点)。
fail-closed: LLM 不可用/超时/解析失败 → 返回确定性默认规划, 绝不阻断主流程。

设计约束:
  - 仅产出"建议", 不修改设计参数; orchestrator 可将其写入过程交付物。
  - 通过 Core.llm_gateway.gw_request 统一路由 (云端优先→本地兜底→规则)。
"""
import json
import os
import threading

import logging
logger = logging.getLogger("agent_planner")

# v4.02.99 (L2 最小侵入): 方案生成师训练知识库（新增文件，由 Scripts/_train_scheme_agent.py S7 写入）
_TRAIN_KB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "Knowledge", "scheme_training_kb.json")


def _load_training_hints(requirement: dict) -> dict:
    """读取本轮训练结论（按规格簇命中）。fail-closed：缺失/损坏/未命中 → {}。

    只读新增文件，不改变 plan() 的返回值结构与任何既有行为。
    """
    try:
        if not os.path.exists(_TRAIN_KB_PATH):
            return {}
        with open(_TRAIN_KB_PATH, "r", encoding="utf-8") as _f:
            _kb = json.load(_f)
        _req = requirement or {}
        _pk = float(_req.get("power_kw") or 0)
        _seg = "P<=30" if _pk <= 30 else ("P30-200" if _pk <= 200 else "P>200")
        _key = "%s|%s|%s极" % (_req.get("motor_type"), _seg, _req.get("poles"))
        return ((_kb.get("buckets") or {}).get(_key) or {})
    except Exception:
        return {}


def _gw_call(agent: str, system: str, user: str, task_type: str,
             temperature: float = 0.4, timeout: float = 45) -> str:
    """统一 LLM 调用封装, 带硬超时; 失败返回 '' (fail-closed)。"""
    _box = {"val": ""}

    def _run():
        try:
            from Core.llm_gateway import gw_request
            _resp = gw_request(agent, system, user,
                               task_type=task_type, temperature=temperature)
            if _resp and getattr(_resp, "ok", False):
                _box["val"] = _resp.content or ""
        except Exception:
            pass

    _t = threading.Thread(target=_run, daemon=True)
    _t.start()
    _t.join(timeout=timeout)
    return _box["val"]


def _safe_dumps(obj, max_chars: int = 2000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s[:max_chars] if len(s) > max_chars else s


def plan(requirement: dict, memory_hits: list = None) -> dict:
    """生成求解策略。

    入参:
      requirement: 需求规格 dict (含 motor_type / rated_power 等)
      memory_hits: query_similar 返回的相似案例列表
    返回: {"strategy": str, "focus": list, "steps": list, "source": "llm"|"default"}
    fail-closed: 异常 → 默认规划, source="default"。
    """
    _default = {
        "strategy": "基于物理内核的混合漏斗生成 + RMxprt 快速扫描 + (可选)Motor-CAD 精细验证",
        "focus": ["高效率", "低温升", "齿轭磁密不超标", "槽满率合规"],
        "steps": [
            "解析需求并提取案例种子",
            "生成参数矩阵 (主线A + 旁路B1/B2/B3)",
            "L1 RMxprt 批量求解",
            "失败子项独立重做",
            "L2 Motor-CAD 精细验证 (若启用)",
            "量化评分 + AI 辅助评价 + 报告",
        ],
        "source": "default",
    }
    try:
        if not isinstance(requirement, dict):
            return _default
        _mem = []
        for _h in (memory_hits or [])[:3]:
            try:
                _mem.append({
                    "similarity": _h.get("similarity"),
                    "summary": _h.get("summary"),
                })
            except Exception:
                continue
        _system = (
            "你是电机电磁设计求解策略规划器。基于需求规格与历史相似案例, 输出 JSON 规划:\n"
            "{\"strategy\":\"总体策略一句话\",\"focus\":[\"聚焦维度1\",\"聚焦维度2\"],"
            "\"steps\":[\"步骤1\",\"步骤2\"]}\n不要解释, 只输出 JSON。"
        )
        # v4.02.99 (L2 最小侵入): 注入方案生成师训练结论（fail-closed，无 KB 时为空对象）
        _train = _load_training_hints(requirement)
        _user = _safe_dumps({
            "requirement": requirement,
            "similar_cases": _mem,
            "training_hints": _train,
        })
        _text = _gw_call("planner_agent", _system, _user, "plan_strategy",
                         temperature=0.4)
        if not _text:
            return _default
        # 容错解析: 截取首个 {...}
        _s = _text.find("{")
        _e = _text.rfind("}")
        if _s < 0 or _e <= _s:
            return _default
        _p = json.loads(_text[_s:_e + 1])
        if not isinstance(_p, dict):
            return _default
        return {
            "strategy": str(_p.get("strategy", _default["strategy"])),
            "focus": _p.get("focus") if isinstance(_p.get("focus"), list) else _default["focus"],
            "steps": _p.get("steps") if isinstance(_p.get("steps"), list) else _default["steps"],
            "source": "llm",
        }
    except Exception as _e:
        logger.warning("[agent_planner] 规划失败, 回退默认: %s", _e)
        return _default
