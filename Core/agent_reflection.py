# -*- coding: utf-8 -*-
"""v4.02.12 (Phase 3): 自主智能体 — 反思器 Reflection。

职责: 设计流程结束后, 基于运行结果 + 规划, 产出结构化反思 (得失/经验/改进点),
并追加写入 data/reflection_log.jsonl 供后续飞轮与复盘。
fail-closed: LLM 不可用 → 返回基于统计的确定性反思; 写入失败不抛异常。

设计约束:
  - 只读分析结果, 不修改已生成的设计方案。
  - 通过 Core.llm_gateway 路由; 超时/异常 → 回退规则反思。
"""
import os
import json
import threading
import datetime

import logging
logger = logging.getLogger("agent_reflection")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_LOG_PATH = os.path.join(_DATA_DIR, "reflection_log.jsonl")

_LOCK = threading.Lock()


def _gw_call(agent: str, system: str, user: str, task_type: str,
             temperature: float = 0.5, timeout: float = 45) -> str:
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


def _rule_reflection(results: list, plan: dict) -> dict:
    """确定性回退反思: 基于统计指标。"""
    _n = len(results) if isinstance(results, list) else 0
    _eff = [float(r.get("efficiency", 0) or 0) for r in (results or []) if isinstance(r, dict)]
    _best = max(_eff) if _eff else 0.0
    _avg = (sum(_eff) / len(_eff)) if _eff else 0.0
    _fine = sum(1 for r in (results or []) if isinstance(r, dict) and r.get("fine_verified"))
    return {
        "reflection": (
            f"本次生成 {_n} 组方案, 最优效率 {_best:.1f}%, 平均 {_avg:.1f}%, "
            f"精细验证 {_fine} 组。建议持续积累案例飞轮以提升多样性与收敛速度。"
        ),
        "lessons": ["优先保证求解成功率", "L2 精细验证可显著提升推荐置信度"],
        "metrics": {"count": _n, "best_eff": round(_best, 2),
                    "avg_eff": round(_avg, 2), "fine_verified": _fine},
        "source": "rule",
    }


def reflect(results: list, plan: dict = None, requirement: dict = None) -> dict:
    """产出反思并写入日志。fail-closed。

    返回: {"reflection": str, "lessons": list, "metrics": dict, "source": str}
    """
    try:
        _rule = _rule_reflection(results, plan)
        try:
            _summary = {
                "result_count": len(results) if isinstance(results, list) else 0,
                "best_eff": _rule["metrics"]["best_eff"],
                "avg_eff": _rule["metrics"]["avg_eff"],
                "fine_verified": _rule["metrics"]["fine_verified"],
                "plan_strategy": (plan or {}).get("strategy"),
            }
            _system = (
                "你是电机设计流程复盘专家。基于本次运行结果与规划, 输出 JSON 反思:\n"
                "{\"reflection\":\"一段中文复盘\",\"lessons\":[\"经验1\",\"经验2\"]}\n"
                "聚焦可改进点。不要解释, 只输出 JSON。"
            )
            _user = _safe_dumps({
                "requirement": requirement,
                "plan": plan,
                "summary": _summary,
            })
            _text = _gw_call("reflection_agent", _system, _user, "reflection",
                             temperature=0.5)
            if _text:
                _s = _text.find("{")
                _e = _text.rfind("}")
                if _s >= 0 and _e > _s:
                    _d = json.loads(_text[_s:_e + 1])
                    if isinstance(_d, dict):
                        return {
                            "reflection": str(_d.get("reflection", _rule["reflection"])),
                            "lessons": _d.get("lessons") if isinstance(_d.get("lessons"), list)
                            else _rule["lessons"],
                            "metrics": _rule["metrics"],
                            "source": "llm",
                        }
        except Exception as _le:
            logger.warning("[agent_reflection] LLM 反思失败, 用规则反思: %s", _le)

        # 写入日志 (fail-closed)
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            _entry = {
                "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reflection": _rule["reflection"],
                "lessons": _rule["lessons"],
                "metrics": _rule["metrics"],
                "source": _rule["source"],
            }
            with _LOCK:
                with open(_LOG_PATH, "a", encoding="utf-8") as _fh:
                    _fh.write(json.dumps(_entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return _rule
    except Exception as _e:
        logger.warning("[agent_reflection] 反思异常: %s", _e)
        return {"reflection": "反思跳过", "lessons": [], "metrics": {},
                "source": "error"}
