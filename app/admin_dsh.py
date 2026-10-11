# -*- coding: utf-8 -*-
"""
DSH 插件管理（管理端 tab）—— Flask Blueprint
======================================================================
v3 定案①②③落地：管理端新增「DSH 插件管理」，后端数据层为 Core/dsh_registry.py。

设计原则（与主程序解耦）：
  - 本文件是**独立蓝图**，只依赖标准库 + Core.dsh_registry（stdlib）+ Flask。
  - 不在 app.py 主流程里堆叠路由，避免触碰既有 2000+ 行逻辑；app.py 末尾仅做
    受保护的 `register_blueprint` + 心跳 `plugin_versions` 存储扩展。
  - dsh_registry 路径按 worktree 解析（app/ -> worktree 根 -> Core），不依赖 D:\\MotorDesign。

端点（前缀 /api/admin，即 /api/admin/dsh-plugins/*）：
  1  GET    /dsh-plugins                      列表（?channel=&status=）
  2  POST   /dsh-plugins                      注册（status=draft）
  3  GET    /dsh-plugins/<plugin_id>          详情
  4  PATCH  /dsh-plugins/<plugin_id>          列级局部更新
  5  POST   /dsh-plugins/<plugin_id>/release-gate   跑 W6 三重门禁，回写 triple_ok
  6  POST   /dsh-plugins/<plugin_id>/lock     锁版
  7  POST   /dsh-plugins/<plugin_id>/rollback  回滚到锁版
 8  GET    /dsh-plugins/<plugin_id>/tokens    口令列表
 9  POST   /dsh-plugins/<plugin_id>/tokens/issue  发出口令
 10  POST   /dsh-plugins/<plugin_id>/tokens/revoke 吊销口令
 11  GET    /dsh-plugins/<plugin_id>/usage-stats    插件使用情况（脱敏聚合指标，管理端展示）
 12  POST   /dsh-plugins/<plugin_id>/usage-report   接收插件端脱敏回传（lib/telemetry.mjs POST 聚合 payload）
  +  GET    /dsh-plugins/install-matrix        安装矩阵（复用 /api/client/register 心跳上报的 plugin_versions）
  +  GET    /admin/dsh-plugins                 自带 HTML 管理页（内联 JS，零改动 app.js）
  +  GET/POST /dsh-plugins/<id>/passcodes[/generate|/revoke|/unbind]   商业口令（第一阶段商业化）
  +  GET    /dsh-plugins/<id>/commerce-stats   商业化统计（核销率/7天内到期/已实现收入）
  +  POST   /api/dsh/verify  /api/dsh/free-call  插件端公开验证与每日免费额度（bp_page，无管理员鉴权）

鉴权：AUTH_ENFORCE 由 app.py 在注册时置为 AUTH_OK；dev 模式（AUTH_OK=False）放开。
"""
import os
import sys
import re
import json
import shutil
import subprocess
import time
import threading
import importlib.util
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from pathlib import Path

from flask import Blueprint, request, jsonify, g

# ---------------------------------------------------------------- 路径解析
# app/admin_dsh.py → parents[1] = worktree 根
WORKTREE = Path(__file__).resolve().parents[1]
CORE_DIR = WORKTREE / "Core"
PLUGIN_ROOT = WORKTREE / "motor-ai-l0"
# 门禁按 plugin_id 定位各自仓库根：l0 走 worktree 镜像，full 走独立仓库
# （可用环境变量 DSH_FULL_PLUGIN_ROOT 覆盖，默认 D:/dsh-motor-ai）
PLUGIN_ROOTS = {
    "motor-ai-l0": PLUGIN_ROOT,
    "dsh-motor-ai": Path(os.environ.get("DSH_FULL_PLUGIN_ROOT", r"D:/dsh-motor-ai")),
}
CLIENT_SETTINGS_FILE = WORKTREE / "data" / "client_settings.json"

if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

try:
    import dsh_registry as reg
    REG_OK = True
    REG_ERR = ""
except Exception as e:  # pragma: no cover
    reg = None
    REG_OK = False
    REG_ERR = str(e)

# 由 app.py 注册时置位；dev 模式默认放开
AUTH_ENFORCE = False

bp = Blueprint("dsh_admin", __name__, url_prefix="/api/admin")
# 无前缀蓝图：自带 HTML 管理页（用户友好路径 /admin/dsh-plugins）
bp_page = Blueprint("dsh_admin_page", __name__)


# ============================================================================
# npm 下载量每日采集调度器（方案 C 后端内置，零外部依赖）
# ---------------------------------------------------------------------------
# 把原本依赖外部定时器的「每日 09:00 跑 Scripts/collect_npm_download_stats.py」
# 收编为 Flask 进程内的守护线程：
#   - 每天 09:00 自动采集近 30 天逐日下载量，经 reg.record_downloads 幂等 UPSERT
#     落入 dsh_plugin_downloads；
#   - 网络/依赖失败仅告警，不影响主程序与既有路由；
#   - 提供手动端点 POST /api/admin/dsh-plugins/<id>/collect-downloads 立即触发；
#   - 进程启动 60s 后补一次当日采集（跨日/当天未采过即补，幂等无副作用）。
# 设计原则与蓝图一致：独立、零侵入、失败静默降级。
# ============================================================================
NPM_COLLECT_HOUR = 9          # 每日采集时刻（本地时区）
NPM_COLLECT_MINUTE = 0
NPM_COLLECT_DAYS = 30         # 回溯天数（与外部脚本默认一致）

# 采集目标 (plugin_id, npm package)：真源 npm 包名 dsh-motor-ai-l0 / dsh-motor-ai。
# 注意注册表旧名 motor-ai-l0 并非 npm 包名，不可当包名采集（同脚本 PACKAGES 口径）。
# [2026-10-11] 新增完整版 dsh-motor-ai（台账 channel=l2/tier=flagship）。
NPM_COLLECT_TARGETS = [
    ("dsh-motor-ai-l0", "dsh-motor-ai-l0"),
    ("dsh-motor-ai", "dsh-motor-ai"),
]


def _load_collect_module():
    """按文件路径加载 Scripts/collect_npm_download_stats.py（避免依赖 PYTHONPATH/重名包）。

    该脚本标准库自足（urllib），顶层不访问网络；fetch_series/record_downloads 可安全复用。
    返回 module 对象；失败返回 None。
    """
    script = WORKTREE / "Scripts" / "collect_npm_download_stats.py"
    if not script.exists():
        print(f"[npm-collect][WARN] 采集脚本不存在: {script}")
        return None
    try:
        spec = importlib.util.spec_from_file_location("motorai_collect_npm_downloads", str(script))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"[npm-collect][WARN] 加载采集脚本失败: {e}")
        return None


def _load_download_modules():
    """确保 Core/dsh_registry 已导入并返回 (reg, mod)；不可用时返回 (None, None)。"""
    if reg is None or not REG_OK:
        print("[npm-collect][WARN] Core/dsh_registry 不可用，跳过采集")
        return None, None
    try:
        mod = _load_collect_module()
    except Exception as e:
        print(f"[npm-collect][WARN] 加载采集脚本异常: {e}")
        mod = None
    return reg, mod


def _collect_downloads_run_once(days: int = NPM_COLLECT_DAYS,
                               plugin_id: str = "",
                               package: str = "") -> Dict[str, Any]:
    """执行一次 npm 下载量采集并幂等落库。

    成功返回 {"ok": True, "results": [...], "errors": []}；
    网络/落库失败返回 {"ok": False, "results": [...], "errors": [...]}。
    全程异常兜底，绝不抛出。
    """
    reg_mod, mod = _load_download_modules()
    if reg_mod is None:
        return {"ok": False, "results": [], "errors": ["registry_unavailable"]}

    end = datetime.today().date()
    start = end - timedelta(days=max(1, int(days)) - 1)

    targets = NPM_COLLECT_TARGETS
    if plugin_id or package:
        targets = [(plugin_id or NPM_COLLECT_TARGETS[0][0],
                    package or NPM_COLLECT_TARGETS[0][1])]

    results, errors = [], []
    for pid, pkg in targets:
        try:
            if mod is None:
                errors.append(f"{pid}: collect module unavailable")
                continue
            series = mod.fetch_series(pkg, start, end)
            written = reg_mod.record_downloads(pid, pkg, series)
            nonzero = [s for s in series if s["downloads"] > 0]
            last = nonzero[-1] if nonzero else (series[-1] if series else None)
            results.append({
                "plugin_id": pid,
                "package": pkg,
                "range": f"{start.isoformat()}..{end.isoformat()}",
                "points": len(series),
                "written": written,
                "sum": sum(s["downloads"] for s in series),
                "last_day": last,
            })
        except Exception as e:
            errors.append(f"{pid}: {e}")

    ok = not errors
    if ok:
        print(f"[npm-collect] OK 采集完成: {results}")
    else:
        print(f"[npm-collect][WARN] 采集失败: {errors}")
    return {"ok": ok, "results": results, "errors": errors}


# ---------------------------------------------------------------- 调度线程
_collect_lock = threading.Lock()
_collect_thread: Optional[threading.Thread] = None
_collect_started = False


def _start_npm_collect_scheduler() -> None:
    """启动每日 09:00 npm 下载量采集守护线程（幂等，重复调用仅首次生效）。

    由 app.py 在注册蓝图后调用；进程常驻，守护线程随主进程退出。
    """
    global _collect_thread, _collect_started
    with _collect_lock:
        if _collect_started:
            return
        _collect_started = True
        _collect_thread = threading.Thread(
            target=_npm_collect_loop, name="npm-download-collector", daemon=True)
        _collect_thread.start()
        print(f"[npm-collect] 调度线程已启动: 每日 {NPM_COLLECT_HOUR:02d}:{NPM_COLLECT_MINUTE:02d} "
              f"采集近 {NPM_COLLECT_DAYS} 天 → dsh_plugin_downloads")


def _seconds_until_target(hour: int, minute: int) -> float:
    """计算下一个本地时区 (hour:minute) 的等待秒数（跨日安全）。"""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (target - now).total_seconds()
    if delta <= 0:
        delta += 86400
    return delta


def _npm_collect_loop() -> None:
    """采集主循环：启动 60s 后补一次当日（幂等），其后每日 09:00 采集。"""
    # 启动补采：跨日或当天 09:00 前已启动且未采过时补一次，保证数据新鲜
    try:
        time.sleep(60)
        _collect_downloads_run_once()
    except Exception as e:
        print(f"[npm-collect][WARN] 启动补采异常: {e}")

    while True:
        try:
            wait = _seconds_until_target(NPM_COLLECT_HOUR, NPM_COLLECT_MINUTE)
            time.sleep(max(1.0, wait))
            _collect_downloads_run_once()
        except Exception as e:
            print(f"[npm-collect][WARN] 采集循环异常（{e}），60s 后重试")
            time.sleep(60)


# ---------------------------------------------------------------- 鉴权守卫
def _admin():
    """返回 (allowed, error_response)。AUTH_ENFORCE=False 时放行。"""
    if not AUTH_ENFORCE:
        return True, None
    u = getattr(g, "user", None)
    if not u or u.get("role") != "admin":
        return False, (jsonify({"error": "需要管理员权限"}), 403)
    return True, None


def _wrap(fn):
    """套鉴权守卫，保留原函数名"""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        ok, err = _admin()
        if not ok:
            return err
        return fn(*a, **k)

    return wrapper


# ---------------------------------------------------------------- 手动触发端点
@bp.route("/dsh-plugins/<plugin_id>/collect-downloads", methods=["POST"])
@_wrap
def collect_downloads(plugin_id: str):
    """手动立即触发一次 npm 下载量采集（幂等），供管理端按钮/运维使用。

    请求体可选 {"days": 30, "package": "dsh-motor-ai-l0"}。
    返回 {"ok", "results", "errors", "triggered_at"}。
    """
    data = request.get_json(silent=True) or {}
    try:
        days = int(data.get("days", NPM_COLLECT_DAYS))
    except (TypeError, ValueError):
        days = NPM_COLLECT_DAYS
    pkg = str(data.get("package") or "").strip()
    out = _collect_downloads_run_once(days=days, plugin_id=plugin_id, package=pkg)
    out["triggered_at"] = datetime.now().isoformat(timespec="seconds")
    return jsonify(out), (200 if out["ok"] else 202)


# ---------------------------------------------------------------- 工具
def _client_store():
    """复用主程序客户端心跳存储（data/client_settings.json）"""
    if CLIENT_SETTINGS_FILE.exists():
        try:
            return json.loads(CLIENT_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _find_node():
    return shutil.which("node") or "node"


def _ensure_seeded():
    """首次访问时若 motor-ai-l0 未登记，则按 package.json 自动建 draft 台账。"""
    if not REG_OK:
        return
    pid = "motor-ai-l0"
    try:
        reg.get_plugin(pid)
        return
    except Exception:
        pass
    pkg_path = PLUGIN_ROOT / "package.json"
    if not pkg_path.exists():
        return
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        reg.register_plugin({
            "plugin_id": pid,
            "name": pkg.get("name", pid),
            "channel": "l0",
            "tier": "free",
            "version": pkg.get("version", "0.0.0"),
            "distribution": {"dsh": pkg.get("dsh", {}), "files": pkg.get("files", [])},
        })
    except Exception:
        pass


def _resolve_plugin_root(plugin_id: str):
    """按 plugin_id 映射到真实仓库根目录。"""
    return PLUGIN_ROOTS.get(plugin_id)


def _run_gate(plugin_id: str) -> dict:
    """服务端 W6 三重门禁（按 plugin_id 定位仓库根，分 l0 / full 两套判据）。"""
    root = _resolve_plugin_root(plugin_id)
    if root is None or not root.exists():
        return {
            "triple_ok": False,
            "plugin_id": plugin_id,
            "gates": {"g0": {"ok": False,
                             "detail": f"未配置/不存在仓库根: {plugin_id}（请设 DSH_FULL_PLUGIN_ROOT 指向 full 仓库）"}},
            "summary": "root-missing",
        }
    if plugin_id == "dsh-motor-ai":
        return _run_gate_full(plugin_id, root)
    return _run_gate_l0(plugin_id, root)


def _run_gate_l0(plugin_id: str, root: Path) -> dict:
    """l0（motor-ai-l0）三重门禁（含 knowledge-sync 同步漂移检测）。"""
    gates = {}
    # G1 P1 同步漂移
    sync_py = root / "knowledge-sync" / "sync-constants.py"
    try:
        out = subprocess.run(
            [sys.executable, str(sync_py), "--check", "--json"],
            cwd=str(root), capture_output=True, text=True, timeout=120)
        rep = json.loads(out.stdout)
        gates["g1"] = {
            "ok": (rep.get("drift_count", 1) == 0 and not rep.get("missing")),
            "detail": f"drift {rep.get('drift_count')} / missing {len(rep.get('missing') or [])}",
        }
    except Exception as e:
        gates["g1"] = {"ok": False, "detail": f"sync 失败: {str(e)[:120]}"}
    # G2 构建产物新鲜度
    try:
        subprocess.run([sys.executable, str(sync_py), "--emit", "--json"],
                       cwd=str(root), capture_output=True, text=True, timeout=120, check=True)
        emitted = json.loads((root / "knowledge-sync" / "constants.json").read_text(encoding="utf-8"))
        cur = json.loads(subprocess.run([sys.executable, str(sync_py), "--json"],
                       cwd=str(root), capture_output=True, text=True, timeout=120).stdout)
        gates["g2"] = {"ok": emitted.get("fingerprint") == cur.get("fingerprint"),
                       "detail": f"fingerprint {emitted.get('fingerprint')}"}
    except Exception as e:
        gates["g2"] = {"ok": False, "detail": f"新鲜度检测失败: {str(e)[:120]}"}
    # G3 版本三重一致性
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        v = pkg["version"]
        skill_v = None
        sp = root / "skills" / "motor-l0-estimate" / "SKILL.md"
        if sp.exists():
            skill_v = re.search(r"version:\s*([\d.]+)", sp.read_text(encoding="utf-8"))
            skill_v = skill_v.group(1) if skill_v else None
        cl = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        cl_v = None
        m = re.search(r"##\s*\[([\d.]+)\]", cl)
        if m:
            cl_v = m.group(1)
        gates["g3"] = {"ok": v == skill_v == cl_v and skill_v is not None,
                       "detail": f"pkg={v} skill={skill_v} changelog={cl_v}"}
    except Exception as e:
        gates["g3"] = {"ok": False, "detail": f"版本比对失败: {str(e)[:120]}"}
    # G4 发布前置（node verify + regression）
    node = _find_node()
    log = []
    for script in ("verify.mjs", "regression.mjs"):
        try:
            subprocess.run([node, str(root / "scripts" / script)],
                           cwd=str(root), capture_output=True, text=True, timeout=300, check=True)
            log.append(f"{script} ✅")
        except Exception as e:
            log.append(f"{script} ❌: {str(e)[:80]}")
    gates["g4"] = {"ok": all("✅" in s for s in log), "detail": " | ".join(log)}

    triple_ok = all(g.get("ok") for g in gates.values())
    summary = " ".join(f"{k}={'OK' if v['ok'] else 'FAIL'}" for k, v in gates.items())
    if REG_OK:
        try:
            reg.set_release_gate(plugin_id, triple_ok, summary)
        except Exception:
            pass
    return {"triple_ok": triple_ok, "gates": gates, "summary": summary, "plugin_id": plugin_id}


def _run_gate_full(plugin_id: str, root: Path) -> dict:
    """full（dsh-motor-ai）三重门禁：仓库结构不同于 l0（无 knowledge-sync / 无 regression.mjs）。"""
    gates = {}
    # G1 仓库完整性：根 + package.json + index.mjs + lib/license.mjs + scripts/verify.mjs
    try:
        need = ["package.json", "index.mjs", "lib/license.mjs", "scripts/verify.mjs"]
        missing = [f for f in need if not (root / f).exists()]
        gates["g1"] = {"ok": len(missing) == 0,
                       "detail": "OK" if not missing else f"缺失: {missing}"}
    except Exception as e:
        gates["g1"] = {"ok": False, "detail": f"检查失败: {str(e)[:120]}"}
    # G2 版本三重一致：package.json.version == CHANGELOG 顶 == 各 skill SKILL.md
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        v = pkg["version"]
        cl = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"##\s*\[([^\]]+)\]", cl)
        cl_v = m.group(1) if m else None
        skill_vs = []
        if (root / "skills").exists():
            for sp in (root / "skills").glob("*/SKILL.md"):
                sm = re.search(r"version:\s*([0-9][\w.\-]*)", sp.read_text(encoding="utf-8"))
                if sm:
                    skill_vs.append(sm.group(1))
        skill_ok = len(skill_vs) > 0 and all(s == v for s in skill_vs)
        gates["g2"] = {"ok": v == cl_v and skill_ok,
                       "detail": f"pkg={v} changelog={cl_v} skills={skill_vs}"}
    except Exception as e:
        gates["g2"] = {"ok": False, "detail": f"版本比对失败: {str(e)[:120]}"}
    # G3 双通道激活代码在场（防回归：DM 在线校验函数必须存在）
    try:
        lic = (root / "lib" / "license.mjs").read_text(encoding="utf-8")
        present = all(k in lic for k in ("isOnlinePasscode", "activateOnline", "getDeviceId"))
        gates["g3"] = {"ok": present,
                       "detail": "DM 在线激活函数齐备" if present else "缺 isOnlinePasscode/activateOnline/getDeviceId"}
    except Exception as e:
        gates["g3"] = {"ok": False, "detail": f"检查失败: {str(e)[:120]}"}
    # G4 发布前置：scripts/verify.mjs（A–G 全套门禁）exit 0
    node = _find_node()
    try:
        subprocess.run([node, str(root / "scripts" / "verify.mjs")],
                       cwd=str(root), capture_output=True, text=True, timeout=300, check=True)
        gates["g4"] = {"ok": True, "detail": "verify.mjs ✅"}
    except Exception as e:
        gates["g4"] = {"ok": False, "detail": f"verify.mjs ❌: {str(e)[:120]}"}

    triple_ok = all(g.get("ok") for g in gates.values())
    summary = " ".join(f"{k}={'OK' if v['ok'] else 'FAIL'}" for k, v in gates.items())
    if REG_OK:
        try:
            reg.set_release_gate(plugin_id, triple_ok, summary)
        except Exception:
            pass
    return {"triple_ok": triple_ok, "gates": gates, "summary": summary, "plugin_id": plugin_id}


# ---------------------------------------------------------------- 端点 1/2
@bp.route("/dsh-plugins", methods=["GET"])
@_wrap
def list_plugins():
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    channel = request.args.get("channel")
    status = request.args.get("status")
    return jsonify({"plugins": reg.list_plugins(channel, status)})


@bp.route("/dsh-plugins", methods=["POST"])
@_wrap
def register_plugin():
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    try:
        row = reg.register_plugin(data)
        return jsonify({"plugin": row}), 201
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------- 端点 3/4
@bp.route("/dsh-plugins/<plugin_id>", methods=["GET"])
@_wrap
def get_plugin(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        return jsonify({"plugin": reg.get_plugin(plugin_id)})
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>", methods=["PATCH"])
@_wrap
def update_plugin(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    try:
        row = reg.update_plugin_fields(plugin_id, data)
        return jsonify({"plugin": row})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


# ---------------------------------------------------------------- 端点 5
@bp.route("/dsh-plugins/<plugin_id>/release-gate", methods=["POST"])
@_wrap
def release_gate(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404
    result = _run_gate(plugin_id)
    return jsonify(result)


# ---------------------------------------------------------------- 端点 6/7
@bp.route("/dsh-plugins/<plugin_id>/lock", methods=["POST"])
@_wrap
def lock_plugin(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        return jsonify({"plugin": reg.lock_version(plugin_id)})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>/rollback", methods=["POST"])
@_wrap
def rollback_plugin(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        return jsonify({"plugin": reg.rollback_version(plugin_id)})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


# ---------------------------------------------------------------- 端点 8/9/10
@bp.route("/dsh-plugins/<plugin_id>/tokens", methods=["GET"])
@_wrap
def list_tokens(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    status = request.args.get("status")
    try:
        return jsonify({"tokens": reg.list_tokens(plugin_id, status)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/dsh-plugins/<plugin_id>/tokens/issue", methods=["POST"])
@_wrap
def issue_token(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    token_id = data.get("token_id")
    if not token_id:
        return jsonify({"error": "token_id 必填"}), 400
    try:
        row = reg.issue_token(
            plugin_id, token_id,
            token_type=data.get("type", "one_time"),
            tier=data.get("tier"),
            expires_at=data.get("expires_at"),
        )
        return jsonify({"token": row}), 201
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>/tokens/revoke", methods=["POST"])
@_wrap
def revoke_token(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    token_id = data.get("token_id")
    if not token_id:
        return jsonify({"error": "token_id 必填"}), 400
    try:
        row = reg.revoke_token(token_id)
        return jsonify({"token": row})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


# ---------------------------------------------------------------- 安装矩阵（复用客户端心跳）
@bp.route("/dsh-plugins/install-matrix", methods=["GET"])
@_wrap
def install_matrix():
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    clients = [
        {"client_id": cid, "plugin_versions": cfg.get("plugin_versions", {})}
        for cid, cfg in _client_store().items()
    ]
    return jsonify({"matrix": reg.build_install_matrix(clients)})


# ---------------------------------------------------------------- 插件使用情况（脱敏聚合指标）
@bp.route("/dsh-plugins/<plugin_id>/usage-stats", methods=["GET"])
@_wrap
def usage_stats(plugin_id):
    """管理端「插件使用情况」卡片数据源：读 SQLite 累计的脱敏聚合指标。
    无上报记录时返回 reported=False 的空结构（前端显示「暂无数据」）。"""
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    try:
        row = reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404
    stats = reg.get_usage_stats(plugin_id)
    return jsonify({
        "plugin_id": plugin_id,
        "plugin_version": row.get("version"),
        "usage_stats": stats,
    })


@bp.route("/dsh-plugins/<plugin_id>/download-stats", methods=["GET"])
@_wrap
def download_stats(plugin_id):
    """管理端「插件使用统计」标签数据源：npm 公开分发下载量趋势（方案 C）。

    数据由 Scripts/collect_npm_download_stats.py 采集落入 dsh_plugin_downloads；
    无采集记录时返回 reported=False 空结构（前端显示「暂无数据」）。
    注意：npm 下载计数含作者自装/CI/镜像抓取，不等于真实用户数，只能看趋势。
    """
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    try:
        reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404

    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    return jsonify({
        "plugin_id": plugin_id,
        "download_stats": reg.get_download_stats(plugin_id, days=days),
    })


@bp.route("/dsh-plugins/<plugin_id>/usage-report", methods=["POST"])
@_wrap
def usage_report(plugin_id):
    """接收插件端（lib/telemetry.mjs）POST 的脱敏聚合 payload 并入库。
    - 只接受插件白名单聚合字段，逐条明细绝不外发；
    - 鉴权：复用插件管理口令（token）——可选带 X-DSH-Token 头做来源校验；
      dev 模式（AUTH_ENFORCE=False）直接放行。
    - 累计合并（多次上报累加计数 / 取最新时间版本），见 reg.ingest_usage_stats。"""
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    try:
        reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404

    payload = request.get_json(force=True, silent=True) or {}
    # 来源口令校验（可选）：若台账有 active token 且带 X-DSH-Token 则校验匹配
    token = request.headers.get("X-DSH-Token", "").strip()
    if token:
        active = [t for t in reg.list_tokens(plugin_id, status="active")
                  if t.get("token_id") == token]
        if not active:
            return jsonify({"error": "口令无效或已吊销"}), 403

    try:
        stats = reg.ingest_usage_stats(plugin_id, payload, source="endpoint")
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "accepted": True,
        "plugin_id": plugin_id,
        "calls_total": stats.get("calls_total", 0),
        "report_count": stats.get("report_count", 0),
        "message": "已合并插件端脱敏聚合指标（仅累计次数/成败/耗时/规模，不含设计内容）",
    }), 202


@bp.route("/dsh-plugins/<plugin_id>/experience-report", methods=["POST"])
@_wrap
def experience_report(plugin_id):
    """接收插件端（lib/experience-upload.mjs）POST 的脱敏设计样本（design_exp）并逐条入库。
    - 与 usage-report 并行的第二条通道：回传的是「实际设计了哪些电机」（规格+几何+
      结论+误差带），脱敏白名单裁剪 + rec_id 幂等去重 + 批量/体量护栏；
    - 服务端白名单裁剪（_sanitize_design_exp_record），不信任客户端字段，红线键丢弃；
    - 鉴权：与 usage-report 同款——可选 X-DSH-Token 来源校验，dev 模式放行。"""
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    try:
        reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404

    payload = request.get_json(force=True, silent=True) or {}
    token = request.headers.get("X-DSH-Token", "").strip()
    if token:
        active = [t for t in reg.list_tokens(plugin_id, status="active")
                  if t.get("token_id") == token]
        if not active:
            return jsonify({"error": "口令无效或已吊销"}), 403

    try:
        res = reg.ingest_experience(plugin_id, payload, source="endpoint")
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({
        "accepted": True,
        "plugin_id": plugin_id,
        "inserted": res.get("inserted", 0),
        "deduped": res.get("deduped", 0),
        "rejected": res.get("rejected", 0),
        "truncated": res.get("truncated", False),
        "message": "已入库插件端脱敏设计样本（规格+几何+结论，红线键已裁剪，rec_id 幂等去重）",
    }), 202


@bp.route("/dsh-plugins/<plugin_id>/experience-stats", methods=["GET"])
@_wrap
def experience_stats(plugin_id):
    """可行性画像：按 motor_type × verdict 聚合 + spec 覆盖率 + 版本分布。
    喂管理端「设计经验统计」标签与 L0 重训盲区识别（哪些规格段被反复查、哪段可行率塌）。"""
    if not REG_OK:
        _ensure_seeded()
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    _ensure_seeded()
    try:
        reg.get_plugin(plugin_id)
    except reg.NotFound:
        return jsonify({"error": f"插件不存在: {plugin_id}"}), 404
    try:
        stats = reg.get_experience_stats(plugin_id)
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"plugin_id": plugin_id, "experience_stats": stats}), 200


# ---------------------------------------------------------------- 自带 HTML 管理页
@bp_page.route("/admin/dsh-plugins", methods=["GET"])
def dsh_plugins_page():
    from flask import Response
    html_path = Path(__file__).resolve().parent / "templates" / "dsh_plugins.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        html = f"<h1>DSH 插件管理页缺失</h1><p>{e}</p>"
    return Response(html, mimetype="text/html")


# ============================================================================
# 商业口令（第一阶段商业化：价格阶梯 + 每日免费额度 + 设备绑定验证）
# ---------------------------------------------------------------------------
# 管理端路由（管理员鉴权，走 _wrap，前缀 /api/admin/dsh-plugins/*）：
#   GET  /dsh-plugins/<id>/passcodes           口令列表 (?status=)
#   POST /dsh-plugins/<id>/passcodes/generate  批量生成 {plan, count, note, order_ref}
#   POST /dsh-plugins/<id>/passcodes/revoke    吊销 {code}
#   POST /dsh-plugins/<id>/passcodes/unbind    解绑设备 {code}
#   GET  /dsh-plugins/<id>/commerce-stats      商业化统计（核销率/7天内到期/已实现收入）
# 插件端公开路由（无管理员鉴权，挂 bp_page，供 DSH 插件运行时调用）：
#   POST /api/dsh/verify      {code, device_id} → {valid, plan, expires_at, days_left}
#   POST /api/dsh/free-call   {plugin_id, device_id} → {allowed, used, remaining, limit}
# 说明：授权验证只判「是否已付费、是否过期、是否本机」，不验证功能；
#       每日免费 5 次为服务端配额（DSH_FREE_DAILY_LIMIT 可覆盖），重装不重置。
#       接口契约与商业方案中 Cloudflare Workers 版 /api/verify 对齐，后续可平移上云。
# ============================================================================
@bp.route("/dsh-plugins/<plugin_id>/passcodes", methods=["GET"])
@_wrap
def list_passcodes(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        return jsonify({"passcodes": reg.list_passcodes(plugin_id, request.args.get("status"))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/dsh-plugins/<plugin_id>/passcodes/generate", methods=["POST"])
@_wrap
def generate_passcodes(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    try:
        rows = reg.generate_passcodes(
            plugin_id,
            data.get("plan", ""),
            data.get("count", 0),
            note=data.get("note") or "",
            order_ref=data.get("order_ref") or "",
        )
        return jsonify({"passcodes": rows, "generated": len(rows)}), 201
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>/passcodes/revoke", methods=["POST"])
@_wrap
def revoke_passcode(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "code 必填"}), 400
    try:
        return jsonify({"passcode": reg.revoke_passcode(code)})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>/passcodes/unbind", methods=["POST"])
@_wrap
def unbind_passcode(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "code 必填"}), 400
    try:
        return jsonify({"passcode": reg.unbind_passcode(code)})
    except reg.RegistryError as e:
        return jsonify({"error": str(e)}), 400
    except reg.NotFound as e:
        return jsonify({"error": str(e)}), 404


@bp.route("/dsh-plugins/<plugin_id>/commerce-stats", methods=["GET"])
@_wrap
def commerce_stats(plugin_id):
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    try:
        return jsonify({"commerce_stats": reg.passcode_stats(plugin_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------- 插件端公开路由（无管理员鉴权）
@bp_page.route("/api/dsh/verify", methods=["POST"])
def dsh_commerce_verify():
    """口令授权验证：激活即绑定设备并起算到期日。契约同商业方案 /api/verify。"""
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    return jsonify(reg.verify_passcode(data.get("code"), data.get("device_id")))


@bp_page.route("/api/dsh/free-call", methods=["POST"])
def dsh_commerce_free_call():
    """每日免费额度检查+计数：allowed=False 时插件应拒绝执行并提示升级付费。"""
    if not REG_OK:
        return jsonify({"error": f"注册表不可用: {REG_ERR}"}), 500
    data = request.get_json(force=True) or {}
    return jsonify(reg.free_call(
        data.get("plugin_id") or "dsh-motor-ai-l0", data.get("device_id")))
