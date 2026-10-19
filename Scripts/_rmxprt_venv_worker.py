# -*- coding: utf-8 -*-
"""
_rmxprt_venv_worker.py v3 - RMxprt 子进程工作脚本 (Python 3.10 + pyaedt 0.6.x)
=============================================================================================
在 Python 3.10 独立环境中运行, 通过 pyaedt 0.6.x 或 win32com 连接 AEDT 2020.x,
执行 RMxprt 磁路法求解。

v3 变更 (2026-08-01) — "防弹版":
  ★ Desktop 一次启动, 每方案唯一项目名 (时间戳), 避免设计名冲突
  ★ 强制 non_graphical=True, 杜绝弹框卡死
  ★ 创建设计前检查并删除已有同名设计
  ★ Analyze 带超时保护 (120s/方案), 防止卡死
  ★ 每方案循环后 close_project 释放资源
  ★ release_desktop 优雅退出, 避免 "cannot be closed" 警告
  ★ 双方法: pyaedt Desktop (主) → win32com -ng (备)

v2 变更 (2026-07-31):
  - win32com 直接 COM 作为主要方法
  - pyaedt + 版本绕过作为回退方法
  - 快速失败: AEDT 启动 60s 超时

调用方式:
    python310.exe _rmxprt_venv_worker.py \
        --params  <params.json>   # 输入: 参数列表 JSON
        --output  <results.json>  # 输出: 结果列表 JSON
        --var-map <var_map.json>   # 变量映射 JSON
        --aedt-version 2020.1      # AEDT 版本
        --machine-type induction   # 电机类型
"""
import sys
import os
import json
import time
import datetime
import traceback
import argparse
import subprocess
import threading
import tempfile
import shutil
import hashlib
import re

# 版本绕过 (必须在 import pyaedt 之前设置)
os.environ["PYAEDT_IGNORE_VERSION_MISMATCH"] = "1"


def log(msg, level="INFO"):
    """输出日志到 stderr (不干扰 stdout)"""
    ts = time.strftime("%H:%M:%S")
    print(f"[venv_worker] [{ts}] [{level}] {msg}", file=sys.stderr, flush=True)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════
# P0-2 (v4.02.81): 绕组接法显式注入 —— 不再依赖模板 Setup1 的默认值
# ---------------------------------------------------------------------------
# 实证: 模板 indm3/yz200-6.aedt 的 Setup1 内 WindingConnection=0, 其真实求解输出
#   Scripts/diag8_results/yz200-6.results_DV49_SOL1_V41.MExportData_0.res 第 9 行
#   写 "Winding Connection:	Wye"  ⇒ **0 = Y (Wye/Star), 1 = Delta**。
# 风险: worker 此前全文 0 处注入该字段 (历史依赖模板默认 Y) ⇒ 一旦换模板/升 AEDT
#   版本而默认改为 Delta, 会与 motor_tools 的相电压口径 (V_LL/√3) 静默错配, 造成
#   气隙磁密 √3 级偏差且无任何报错。现改为由候选字典 d["winding_connection"] 显式下发。
# ⚠ 本表必须与 Scripts/motor_tools.py::_WINDING_CONNECTION_CODE 保持镜像一致
#   (worker 运行在独立解释器, 不 import motor_tools)。
# ═══════════════════════════════════════════════════════════════════════════
_WINDING_CONNECTION_CODE = {"Y": 0, "WYE": 0, "STAR": 0, "D": 1, "DELTA": 1, "Δ": 1}


def _winding_connection_code(conn):
    """接法 → RMxprt `Setup1.WindingConnection` 数值码 (实证 0=Y/Wye, 1=Delta)。

    缺省/未知一律回 **0 (Y)** —— 与 motor_tools 的相电压缺省口径 (V_LL/√3) 保持一致,
    保证「接法」与「相电压口径」这两个必须同源的量不会独立漂移。
    """
    return _WINDING_CONNECTION_CODE.get(str(conn or "Y").strip().upper(), 0)


def _winding_connection_name(conn):
    """接法 → 人类可读名 (写日志/自检用)。"""
    return "Wye" if _winding_connection_code(conn) == 0 else "Delta"


def _probe_winding_connection(res_path):
    """只读: 从 .res GENERAL DATA 段读 RMxprt **实际**采用的接法字符串。

    返回 "Wye" / "Delta" / None(未取到)。用于求解后回读自检 —— 见
    `_check_winding_connection`。
    """
    try:
        with open(res_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
        m = re.search(r"Winding\s+Connection:\s*([A-Za-z]+)", head)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def _check_winding_connection(res_path, params):
    """求解后回读自检: 实算接法 vs 设计意图接法。

    P0-2 验收口径: 若两者不一致 → 相电压口径已错配 (磁密会有 √3 级偏差),
    必须 WARN 暴露而非静默通过。返回 (ok: bool, actual: str|None)。
    """
    actual = _probe_winding_connection(res_path)
    if actual is None:
        return True, None                       # 取不到不判失败 (老版 .res 无该行)
    expect = _winding_connection_name(params.get("winding_connection"))
    ok = actual.strip().lower() == expect.strip().lower()
    if not ok:
        log(f"  ★ P0-2 接法错配: 设计意图={expect} 实算={actual} "
            f"(.res 口径与 motor_tools 相电压不匹配, 磁密将出现 √3 级偏差!)", "WARN")
    else:
        log(f"  P0-2 接法自检 OK: {actual} (预期 {expect})", "DEBUG")
    return ok, actual


def _ansysedt_pids():
    """当前存活的 ansysedt.exe pid 集合（跨进程串扰治理用）。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ansysedt.exe",
                            "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=10)
        out = r.stdout.decode("gbk", errors="replace")
    except Exception:
        return set()
    pids = set()
    for ln in out.splitlines():
        parts = [x.strip().strip('"') for x in ln.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _pid_parent_map():
    """pid -> ppid 映射（CreateToolhelp32Snapshot）。失败返回 {}。"""
    try:
        import ctypes
        import ctypes.wintypes as _wt

        class _PE32(ctypes.Structure):
            _fields_ = [("dwSize", _wt.DWORD), ("cntUsage", _wt.DWORD),
                        ("th32ProcessID", _wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", _wt.DWORD), ("cntThreads", _wt.DWORD),
                        ("th32ParentProcessID", _wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", _wt.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k = ctypes.windll.kernel32
        snap = k.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return {}
        pe = _PE32()
        pe.dwSize = ctypes.sizeof(_PE32)
        out = {}
        ok = k.Process32First(snap, ctypes.byref(pe))
        while ok:
            out[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
            ok = k.Process32Next(snap, ctypes.byref(pe))
        k.CloseHandle(snap)
        return out
    except Exception:
        return {}


def _orphan_aedt_pids():
    """★ 跨进程干扰根治（2026-09-21）：父进程已退出的 ansysedt.exe（孤儿）。

    「基线保留」策略的盲区：为不误杀并发消费方，本 worker 对**启动前已存在**的
    ansysedt 一律不碰。但历史批次泄漏的实例其父进程早已退出（实测累积 6 个
    20KB 空壳），仍被当作「别人的实例」永久保留 ⇒ 占用 COM/Automation 端口与
    license 席位 ⇒ 新批次 DispatchEx 连接被拖慢（实测等 28s）甚至失败降级。

    判定：ppid==0(无父) 或 ppid 已不在进程表 ⇒ 无人负责 ⇒ 可安全清理。
    ⚠ 本批自己拉起的实例父进程(worker / svchost COM 宿主)存活 ⇒ 绝不会被误判。
    """
    pids = _ansysedt_pids()
    if not pids:
        return set()
    pm = _pid_parent_map()
    if not pm:
        return set()
    alive = set(pm.keys())
    out = set()
    for _pid in pids:
        _ppid = pm.get(_pid)
        if _ppid is None:
            continue
        if _ppid == 0 or _ppid not in alive:
            out.add(_pid)
    return out


# 本 worker 启动那一刻已存在的 AEDT 实例 = 别人的（父进程存活者受保护；
# 其父已死的孤儿由 _orphan_aedt_pids() 额外回收，见 kill_aedt_processes）
_PRE_AEDT_PIDS = _ansysedt_pids()


def _zombie_aedt_pids():
    """P3 (2026-09-23): ansysedt 僵尸对象判定（进程本体已死，仅内核对象被宿主句柄引用残留）。

    2026-09-23 12:26 实测定案：僵尸对象 tasklist 仍可见、提权 taskkill 拒绝访问，
    但 OpenProcess(QUERY_LIMITED) 一律 err=5、零 CPU/零内存、license 已释放 ⇒ 对 COM 无实质
    影响，清理无收益（仅重启机器可彻底清除）。
    判据（三者同时满足才判僵尸，否则保守按受保护活实例）：
      ① OpenProcess(QUERY_LIMITED) 失败且 GetLastError=5
      ② 父进程为 svchost.exe（COM LocalServer32 宿主）
      ③ age > RMXPRT_ZOMBIE_AGE_H(默认 6h) 且累计 CPU < 1s（静止）
    psutil 为可选依赖（embedded venv 已装）；不可用时返回空集（保守不动）。
    """
    import ctypes
    pids = _ansysedt_pids()
    if not pids:
        return set()
    try:
        import psutil
    except Exception:
        return set()
    try:
        _age_h = float(os.getenv("RMXPRT_ZOMBIE_AGE_H", "6.0"))
    except Exception:
        _age_h = 6.0
    pm = _pid_parent_map()
    out = set()
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    now = time.time()
    for pid in pids:
        try:
            k.SetLastError(0)
            h = k.OpenProcess(0x1000, False, int(pid))
            if h:
                code = ctypes.c_ulong()
                if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                    out.add(pid)          # 句柄可读且对象已退出 ⇒ 僵尸
                k.CloseHandle(h)
                continue
            if ctypes.get_last_error() != 5:
                continue                  # 无法判定 ⇒ 保守不动
            _pr = psutil.Process(pid)
            _age = (now - _pr.create_time()) / 3600.0
            _cpu = _pr.cpu_times()
            _cpu_idle = (_cpu.user + _cpu.system) < 1.0
            _svchost = False
            _host_dead = False
            try:
                _hn = (psutil.Process(_pr.ppid()).name() or "").lower()
                _svchost = (_hn == "svchost.exe")
            except Exception:
                _host_dead = True           # 宿主已死（2026-09-23 实测 17 僵尸全命中此态）
            if _age > _age_h and _cpu_idle and (_svchost or _host_dead or pm.get(pid) == 0):
                out.add(pid)
        except Exception:
            continue
    return out


def _write_aedt_launch_fail_flag():
    """P6a (2026-09-23): AEDT 启动层失败 ⇒ 写 flag 文件（供 rmxprt_tools P6 席位监控）。

    主进程在整批零真解时检查该 flag（mtime < 300s）⇒ 判定疑似 license 席位耗尽,
    打 WARN 排查指引并强制下批 preflight 复检（绕能力缓存的旧 usable=True 假象）。
    """
    try:
        import tempfile as _tf
        _flag = os.path.join(_tf.gettempdir(), "motorai_aedt_launch_fail.flag")
        with open(_flag, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "pid": os.getpid(),
                       "method": "AEDT_COM_WAIT_TIMEOUT_90s"}, fh)
        log("[P6] AEDT 启动层失败 flag 已写入: %s" % _flag, "WARN")
    except Exception as e:
        log("[P6] 写 AEDT 启动失败 flag 异常(忽略): %r" % e)

# 2026-09-21 用户指令③(批内并发): 本 worker 是并行分片之一时为 True。
#   此时"本批差集"里会混入**兄弟分片**正在用的 ansysedt ⇒ 差集清理会误杀兄弟
#   ⇒ 改为只回收孤儿(父进程已死), 绝不按差集杀。由 CLI --no-diff-kill 置位。
_NO_DIFF_KILL = False


def kill_aedt_processes():
    """清理本 worker 自己拉起的 AEDT 进程。

    ★ 跨进程串扰根治（P0-5）：旧实现 `taskkill /F /IM ansysedt.exe` 按**进程名**全机杀，
      会把并发消费方（生产 Flask / 训练批 / 另一个设计任务）正在求解的 AEDT 一起杀掉，
      导致对方 Analyze 中断 ⇒ 静默降级 SIMULATED。已实测：训练批 20260919_005958 期间
      外部 worker 两次 "Killed ansysedt.exe processes"，本批降级率随之冲到 50%。

    改为**按 pid 范围杀**：只杀本 worker 启动后新出现的实例（差集），基线实例保留。
    回退开关: RMXPRT_SCOPED_KILL=0 ⇒ 恢复旧的全局按名杀。
    """
    if os.getenv("RMXPRT_SCOPED_KILL", "1") == "0":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ansysedt.exe"],
                           capture_output=True, timeout=10)
            log("Killed ansysedt.exe processes (GLOBAL, RMXPRT_SCOPED_KILL=0)")
        except Exception:
            pass
        return
    if _NO_DIFF_KILL:
        # 2026-09-21 批内并发: 并行分片只回收孤儿, 跳过本批差集
        #   (差集在并行时含兄弟分片的 AEDT ⇒ 会把兄弟杀掉 ⇒ 对方静默降级)
        try:
            _orph2 = _orphan_aedt_pids()
        except Exception:
            _orph2 = set()
        for _pid2 in sorted(_orph2):
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(_pid2)],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        if _orph2:
            # ★ P2 (2026-09-23): 杀后回读校验, 杜绝「Killed」谎日志
            #   (taskkill 拒绝访问时返回码被吞, 实测报杀 17 个实存 18 个)
            # ★ P3 (2026-09-23): 残留区分「僵尸对象」(零影响, INFO) 与「受保护活实例」(ERROR 需人工)
            time.sleep(1.0)
            _still2 = sorted(set(_ansysedt_pids()) & set(_orph2))
            _gone2 = [p for p in sorted(_orph2) if p not in set(_still2)]
            if _still2:
                _zb2 = sorted(set(_still2) & _zombie_aedt_pids())
                _pr2 = [p for p in _still2 if p not in set(_zb2)]
                if _pr2:
                    log("P2 校验: ansysedt pids=%s 清理后仍存活(受保护活实例), "
                        "COM 端口与 license 席位仍被占用, 需人工回收" % (_pr2,), "ERROR")
                if _zb2:
                    log("P2 校验: ansysedt pids=%s 为僵尸对象(本体已死仅内核对象残留, 零 CPU/内存/license, "
                        "无实质影响; 重启机器可彻底清除)" % (_zb2,))
            log("Killed ansysedt.exe pids=%s (ephemeral: 仅孤儿回收, 跳过本批差集; "
                "实际退出=%d/%d)" % (_gone2, len(_gone2), len(_orph2)))
        return
    try:
        # 本 worker 自己拉起的（差集）+ 历史泄漏的孤儿（父进程已死）
        mine = set(_ansysedt_pids()) - _PRE_AEDT_PIDS
        _orph = _orphan_aedt_pids()
        mine |= _orph
    except Exception:
        mine = set()
        _orph = set()
    mine = sorted(mine)
    for pid in mine:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    # ★ P2 (2026-09-23): 杀后回读校验, 杜绝「Killed」谎日志 ——
    #   taskkill 拒绝访问(受保护 COM 宿主实例)时返回码被吞, 实测 2026-09-23
    #   10:44/10:52 报杀 17 个实存 18 个, 直到 11:18 才真正清零。
    # ★ P3 (2026-09-23): 残留区分「僵尸对象」(零影响, INFO) 与「受保护活实例」(ERROR 需人工)
    time.sleep(1.0)
    _still = sorted(set(_ansysedt_pids()) & set(mine))
    _gone = [p for p in mine if p not in set(_still)]
    if _still:
        _zb = sorted(set(_still) & _zombie_aedt_pids())
        _pr = [p for p in _still if p not in set(_zb)]
        if _pr:
            log("P2 校验: ansysedt pids=%s 清理后仍存活(受保护活实例, 非僵尸), "
                "COM 端口与 license 席位仍被占用, 需人工回收" % (_pr,), "ERROR")
        if _zb:
            log("P2 校验: ansysedt pids=%s 为僵尸对象(本体已死仅内核对象残留, 零 CPU/内存/license, "
                "无实质影响; 重启机器可彻底清除)" % (_zb,))
    log("Killed ansysedt.exe pids=%s (scoped: 本批差集+孤儿回收; 保留基线 %s; "
        "实际退出=%d/%d)" % (_gone, sorted(set(_PRE_AEDT_PIDS) - set(mine)),
                             len(_gone), len(mine)))


# ============================================================
# 14 类电机类型注册表 (RMxprt AEDT 2020.1)
# ============================================================

# AEDT 示例目录根路径
_AEDT_EXAMPLES = r"D:\Program Files\AnsysEM\AnsysEM20.1\Win64\Examples\RMxprt"

# 完整的 14 类电机注册表
# 每个条目: key → {rmxprt_type, template, design_name, description}
MOTOR_TYPE_REGISTRY = {
    # --- 感应电机 ---
    "induction": {
        "rmxprt_type": "Three Phase Induction Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "indm3", "yz200-6.aedt"),
        "design_name": "yz200-6",
        "description": "三相异步电动机 (默认)",
        "category": "induction",
    },
    "induction_single_phase": {
        "rmxprt_type": "Single Phase Induction Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "indm1", "3hp.aedt"),
        "design_name": "RMxprtDesign",
        "description": "单相感应电动机",
        "category": "induction",
    },

    # --- 永磁电机 ---
    "pmsm": {
        "rmxprt_type": "Three Phase Permanent Magnet Synchronous Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "PMSG_4p50Hz550W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "三相永磁同步电动机",
        "category": "pm",
    },
    "bldc": {
        "rmxprt_type": "Brushless Permanent-Magnet DC Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "BLDC_4p1500rpm550W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "无刷直流电动机",
        "category": "pm",
    },
    "pmdc": {
        "rmxprt_type": "Permanent-Magnet DC Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "PMDC_2p2400rpm75W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "永磁直流电动机",
        "category": "pm",
    },

    # --- 磁阻电机 ---
    "srm": {
        "rmxprt_type": "Switched Reluctance Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "SRM_8-6p1500rpm550W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "开关磁阻电动机",
        "category": "reluctance",
    },
    "synrm": {
        "rmxprt_type": "Synchronous Reluctance Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "hysm", "ipm_HysRotor.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "同步磁阻电动机",
        "category": "reluctance",
    },

    # --- 同步电机 ---
    "wound_sync": {
        "rmxprt_type": "Wound Synchronous Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "synm3", "Bjs7_mot.aedt"),
        "design_name": "Bjs7_mot",
        "description": "电励磁同步电动机",
        "category": "synchronous",
    },
    "sync_generator": {
        "rmxprt_type": "Three Phase Synchronous Generator",
        "template": os.path.join(_AEDT_EXAMPLES, "syng3", "bjs10-61.aedt"),
        "design_name": "bjs10-61",
        "description": "三相同步发电机",
        "category": "synchronous",
    },
    "lssm": {
        "rmxprt_type": "Line-Start Permanent Magnet Synchronous Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "LSSM_4p50Hz550W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "自启动永磁同步电动机",
        "category": "synchronous",
    },

    # --- 特殊电机 ---
    "universal": {
        "rmxprt_type": "Universal Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "UniM_2p12500rpm100W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "通用电动机 (交直流通用串励)",
        "category": "special",
    },
    "claw_pole": {
        "rmxprt_type": "Claw Pole Generator",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "CPSG_4p50Hz550VA.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "爪极发电机",
        "category": "special",
    },
    "afpm": {
        "rmxprt_type": "Permanent-Magnet Brushless Linear Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "afpm", "afbldc_1.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "永磁无刷直线电动机",
        "category": "special",
    },
    "flux_switching": {
        "rmxprt_type": "Flux-Switching Permanent Magnet Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "afpm", "afassm_1.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "磁通切换永磁电动机",
        "category": "special",
    },
    "sync_relay": {
        "rmxprt_type": "Synchronous Reluctance Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "hysm", "ipm_HysRotor.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "磁阻同步电动机 (sync_relay 别名)",
        "category": "reluctance",
    },
    # --- 显式不支持类型 (RMxprt 暂无专用模板) — 单列 + 告警, 禁止静默错判 ---
    # v4.02.20 (根因D-1): 步进/伺服此前被静默回落为异步/永磁同步。现显式单列,
    #   解析时打 WARN + 写入 _RESOLVE_EVENTS 审计, 不再"静默错判"。
    #   模板路由仍暂用最接近模板(步进→异步/伺服→PMSM)以保链路不崩, 但明确标记
    #   supported=False + misjudged_to, 供编排层/报告显式告知用户需人工确认或补模板。
    "stepper": {
        "rmxprt_type": "Three Phase Induction Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "indm3", "yz200-6.aedt"),
        "design_name": "yz200-6",
        "description": "步进电机 (RMxprt 暂无专用模板, 暂按异步模板求解并告警)",
        "category": "unsupported",
        "supported": False,
        "misjudged_to": "induction",
    },
    "servo": {
        "rmxprt_type": "Three Phase Permanent Magnet Synchronous Motor",
        "template": os.path.join(_AEDT_EXAMPLES, "manual", "PMSG_4p50Hz550W.aedt"),
        "design_name": "RMxprtDesign1",
        "description": "伺服电机 (RMxprt 暂无专用模板, 暂按永磁同步模板求解并告警)",
        "category": "unsupported",
        "supported": False,
        "misjudged_to": "pmsm",
    },
}

# 兼容性别名
_TYPE_ALIASES = {
    "im": "induction",
    "im3": "induction",
    "induction_3phase": "induction",
    "im1": "induction_single_phase",
    "induction_1phase": "induction_single_phase",
    "pmsm_3phase": "pmsm",
    "pmsg": "pmsm",
    "ipm": "pmsm",
    "dc": "pmdc",
    "pm_dc": "pmdc",
    "sr": "srm",
    "switched_reluctance": "srm",
    "synrel": "synrm",
    "wound_synchronous": "wound_sync",
    "sync_motor": "wound_sync",
    "sync_gen": "sync_generator",
    "synchronous_generator": "sync_generator",
    "line_start_pmsm": "lssm",
    "uni": "universal",
    "universal_motor": "universal",
    "clawpole": "claw_pole",
    "cpsg": "claw_pole",
    "linear": "afpm",
    "afpm_bldc": "afpm",
    "fspm": "flux_switching",
    "flux_switch": "flux_switching",
    # ★ 中文名别名 (支持需求解析Agent直接输出)
    "异步电动机": "induction", "三相异步电动机": "induction", "异步电机": "induction",
    "感应电动机": "induction", "感应电机": "induction", "三相感应电动机": "induction",
    "单相异步电动机": "induction_single_phase", "单相感应电动机": "induction_single_phase",
    "永磁同步电动机": "pmsm", "永磁同步电机": "pmsm", "永磁电机": "pmsm",
    "无刷直流电动机": "bldc", "无刷直流电机": "bldc", "无刷电机": "bldc",
    "永磁直流电动机": "pmdc", "永磁直流电机": "pmdc",
    "开关磁阻电动机": "srm", "开关磁阻电机": "srm", "磁阻电机": "srm",
    "同步磁阻电动机": "synrm", "同步磁阻电机": "synrm",
    "电励磁同步电动机": "wound_sync", "电励磁同步电机": "wound_sync",
    "同步发电机": "sync_generator", "三相同步发电机": "sync_generator",
    "自启动永磁同步电动机": "lssm", "自启动永磁电机": "lssm",
    "通用电动机": "universal", "通用电机": "universal", "串励电动机": "universal",
    "爪极发电机": "claw_pole", "爪极电机": "claw_pole",
    "直线电动机": "afpm", "直线电机": "afpm",
    "磁通切换电动机": "flux_switching", "磁通切换电机": "flux_switching",
    # ★ v4.02.20 (根因D-1): 步进/伺服显式单列, 禁止静默错判为异步/永磁同步
    "步进电动机": "stepper", "步进电机": "stepper", "步进": "stepper",
    "伺服电动机": "servo", "伺服电机": "servo", "伺服": "servo",
}


# v4.02.20 (根因D-1): 显式不支持类型 + 解析审计
_UNSUPPORTED_TYPES = {"stepper", "servo"}
_RESOLVE_EVENTS = []  # [{input, resolved, supported, note}] — 供编排层/报告审计


def _log_resolve(inp, resolved, supported, note):
    _RESOLVE_EVENTS.append({
        "input": inp, "resolved": resolved,
        "supported": supported, "note": note,
    })
    if len(_RESOLVE_EVENTS) > 200:
        del _RESOLVE_EVENTS[:100]


def resolve_motor_type(machine_type):
    """解析电机类型 (支持别名)

    v4.02.20 (根因D-1): 步进/伺服此前被静默回落为异步/永磁同步。现显式单列:
      解析到 unsupported 类型时打 WARN 并写入 _RESOLVE_EVENTS 审计, 不再"静默错判"。
      模板路由仍暂用最接近模板(步进→异步/伺服→PMSM)以保链路不崩, 但明确标记
      supported=False, 供编排层/报告显式告知用户需人工确认或补充专用模板。

    Args:
        machine_type: 用户输入的电机类型标识
    Returns:
        标准化的电机类型 key (在 MOTOR_TYPE_REGISTRY 中存在)
    """
    if not machine_type:
        _log_resolve(machine_type, "induction", True, "空类型, 默认异步")
        return "induction"

    mt = machine_type.lower().strip()

    if mt in MOTOR_TYPE_REGISTRY:
        resolved = mt
    elif mt in _TYPE_ALIASES:
        resolved = _TYPE_ALIASES[mt]
        log(f"Motor type alias: '{machine_type}' -> '{resolved}'")
    else:
        log(f"Unknown motor type '{machine_type}', defaulting to 'induction'", "WARN")
        _log_resolve(machine_type, "induction", True,
                     "未知类型静默回落异步(建议补充别名/模板)")
        return "induction"

    if resolved in _UNSUPPORTED_TYPES:
        entry = MOTOR_TYPE_REGISTRY.get(resolved, {})
        note = (f"类型'{machine_type}'解析为'{resolved}'(RMxprt 暂无专用模板), "
                f"暂按'{entry.get('misjudged_to', '?')}'模板求解并显式告警, "
                f"禁止静默错判; 建议补充专用模板或人工确认")
        log(note, "WARN")
        _log_resolve(machine_type, resolved, False, note)
    return resolved


def is_supported_motor_type(machine_type) -> bool:
    """v4.02.20 (根因D-1): 该电机类型是否有 RMxprt 专用模板可真实求解。

    步进/伺服(及未来其他 unsupported 类型)返回 False —— 编排层应据此
    显式告知用户"暂不支持/需人工确认", 而非当作已成功求解。
    """
    if not machine_type:
        return True  # 空类型默认异步, 视为支持
    mt = resolve_motor_type(machine_type)
    entry = MOTOR_TYPE_REGISTRY.get(mt, {})
    return bool(entry.get("supported", True))


def get_machine_type_str(machine_type):
    """获取 RMxprt 机器类型字符串"""
    mt = resolve_motor_type(machine_type)
    return MOTOR_TYPE_REGISTRY[mt]["rmxprt_type"]


def get_template_path(machine_type):
    """获取电机类型对应的 AEDT 模板文件路径"""
    mt = resolve_motor_type(machine_type)
    return MOTOR_TYPE_REGISTRY[mt]["template"]


def get_template_design_name(machine_type):
    """获取模板中的设计名"""
    mt = resolve_motor_type(machine_type)
    return MOTOR_TYPE_REGISTRY[mt]["design_name"]


# ============================================================
# 文件管理系统: 锁清理 + 唯一命名 + 结果后清理
# ============================================================

def _effective_output_map(var_map, machine_type):
    """v4.02.30 (#1129-① 扩展): 合并 type_output_overrides[motor_type] 到 base output_map。

    此前 worker 在三条求解路径(pyaedt / openproject .res / win32com)都只取 base output_map,
    从不合并 type_output_overrides, 致 pmsm/bldc/srm/sync_generator 等专属输出字段
    (back_emf / torque_ripple / cogging_torque 等, 定义在 Knowledge/rmxprt_variable_map.json)
    从未被抽取, 恒为 0。现按 resolve_motor_type 归一后的类型 key 合并; override 键与 base
    不冲突(均为专属增项), 故直接 dict 合并(base 保留, override 追加/覆盖同名)。
    """
    base = var_map.get("output_map", {}) or {}
    overrides = (var_map.get("type_output_overrides") or {}).get(resolve_motor_type(machine_type), {}) or {}
    if not overrides:
        return base
    merged = dict(base)
    merged.update(overrides)
    return merged


def cleanup_lock_files(target_dir=None):
    """运行前全局清扫 *.lock 残留 (孤儿锁)

    残留的 .lock 文件会导致 "Project already exists" 错误。

    Args:
        target_dir: 目标目录 (None=TEMP 目录)
    Returns:
        清理的文件数
    """
    import glob
    if target_dir is None:
        target_dir = tempfile.gettempdir()

    count = 0
    # 清理 .lock 文件
    for f in glob.glob(os.path.join(target_dir, "*.lock")):
        try:
            os.remove(f)
            count += 1
        except Exception:
            pass

    # 清理 .auto 自动保存文件
    for f in glob.glob(os.path.join(target_dir, "*.auto")):
        try:
            os.remove(f)
            count += 1
        except Exception:
            pass

    if count > 0:
        log(f"Cleanup: removed {count} stale lock/auto files from {target_dir}")
    return count


def generate_unique_project_name(machine_type, params, index):
    """生成唯一项目名 (类型+参数哈希+时间戳+PID)

    命名规则: {TYPE}_{HASH}_{TIMESTAMP}_{PID}
    示例: IM_a3f8_143022_12345

    Args:
        machine_type: 电机类型
        params: 参数字典 (用于生成哈希)
        index: 方案索引
    Returns:
        唯一项目文件名 (不含扩展名)
    """
    import hashlib

    mt = resolve_motor_type(machine_type)
    # 类型前缀 (大写缩写)
    type_prefix = {
        "induction": "IM",
        "induction_single_phase": "IM1",
        "pmsm": "PMSM",
        "bldc": "BLDC",
        "pmdc": "PMDC",
        "srm": "SRM",
        "synrm": "SynRM",
        "wound_sync": "WSM",
        "sync_generator": "SG",
        "lssm": "LSSM",
        "universal": "UNI",
        "claw_pole": "CPSG",
        "afpm": "AFPM",
        "flux_switching": "FSPM",
        "sync_relay": "SynRM",
    }.get(mt, "MTR")

    # 参数哈希 (取前4位)
    param_str = json.dumps(params, sort_keys=True, default=str)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:4]

    # 时间戳 + PID
    ts = datetime.datetime.now().strftime("%H%M%S")
    pid = os.getpid()

    return f"{type_prefix}_{param_hash}_{ts}_{pid}_{index+1}"


def cleanup_project_files(aedt_path):
    """结果提取后清理整个项目目录 (释放 50-200MB/任务)

    清理内容:
      - .aedt 项目文件
      - .aedtresults 结果目录
      - .lock 锁文件
      - .auto 自动保存
      - .bak 备份文件
      - .results 目录
      - temp/ amat/ syslib/ 临时缓存

    Args:
        aedt_path: .aedt 项目文件路径
    Returns:
        清理的文件/目录数
    """
    import shutil
    count = 0
    base = aedt_path
    base_noext = os.path.splitext(base)[0]
    temp_dir = os.path.dirname(base)

    # 清理主文件及关联文件
    for ext in [".aedt", ".aedtresults", ".lock", ".auto", ".bak", ".results",
                ".asol_priv", ".amod", ".aprj"]:
        path = base_noext + ext if ext != ".aedt" else base
        if os.path.isfile(path):
            try:
                os.remove(path)
                count += 1
            except Exception:
                pass
        elif os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
                count += 1
            except Exception:
                pass

    # 清理临时缓存目录 (在项目目录下)
    for cache_dir in ["temp", "amat", "syslib"]:
        cache_path = os.path.join(temp_dir, cache_dir)
        if os.path.isdir(cache_path):
            try:
                shutil.rmtree(cache_path, ignore_errors=True)
                count += 1
            except Exception:
                pass

    return count


# ============================================================
# 方法 A: pyaedt Desktop + Rmxprt (主要方法)
# ============================================================

def solve_via_pyaedt_v3(param_list, var_map, aedt_version="2020.1", machine_type="induction"):
    """使用 pyaedt 0.6.x Desktop + Rmxprt 求解 (防弹版)

    流程:
      1. Desktop 一次启动 (non_graphical=True)
      2. 每方案: 唯一项目名 → Rmxprt → 设变量 → Analyze → 读结果 → close_project
      3. release_desktop 优雅退出
    """
    # 猴子补丁: 绕过版本检查
    try:
        from pyaedt import desktop as _desktop_mod
        if hasattr(_desktop_mod, "_assert_version"):
            _orig_assert = _desktop_mod._assert_version
            def _patched_assert(ver):
                if ver.startswith("2020"):
                    return
                _orig_assert(ver)
            _desktop_mod._assert_version = _patched_assert
            log("Version check bypassed (pyaedt desktop._assert_version)")
        if hasattr(_desktop_mod, "_check_aedt_version"):
            _desktop_mod._check_aedt_version = lambda x: None
    except Exception as e:
        log(f"Version patch failed (may still work): {e}", "WARN")

    import pyaedt
    from pyaedt.desktop import Desktop
    from pyaedt import Rmxprt as RmxprtFactory

    log(f"pyaedt {pyaedt.__version__} loaded")
    log("=== 方法 A: pyaedt Desktop + Rmxprt (防弹版) ===")

    machine_type_str = get_machine_type_str(machine_type)
    input_map = var_map.get("input_map", {})
    output_map = _effective_output_map(var_map, machine_type)

    # 1. 启动 Desktop (一次) — v4.02.06: 看门狗防启动死锁
    log(f"Starting AEDT Desktop (non_graphical=True, version={aedt_version})...")
    _write_heartbeat(0, len(param_list), "startup")
    t_desktop = time.time()
    d = [None]

    def _start_desktop():
        d[0] = Desktop(
            specified_version=aedt_version,
            non_graphical=True,          # ★ 关键! 防止弹出任何对话框
            new_desktop_session=True,
        )

    # 2026-09-23 批内并发 (cc=10): 多实例同时拉起 ansysedt 时启动显著变慢
    #   ⇒ 启动看门狗须可配置, 由父进程按并发度注入(子进程独立 env, 互不影响)。
    _desktop_to = 90
    try:
        _desktop_to = max(60, int(str(os.getenv("RMXPRT_DESKTOP_START_TIMEOUT", "90")).strip()))
    except Exception:
        _desktop_to = 90
    _dt = threading.Thread(target=_start_desktop, daemon=True)
    _dt.start()
    _dt.join(timeout=_desktop_to)
    if d[0] is None:
        raise RuntimeError(f"Desktop 启动超时 ({_desktop_to}s 看门狗, 疑似 AEDT 死锁)")
    desktop_time = time.time() - t_desktop
    _write_heartbeat(0, len(param_list), "solving")
    log(f"Desktop started in {desktop_time:.1f}s")

    results = []
    rmxprt_count = 0

    try:
        # 2. 批量求解
        for i, params in enumerate(param_list):
            t0 = time.time()
            _write_heartbeat(i, len(param_list), "solving")
            ts = datetime.datetime.now().strftime("%H%M%S")

            # ★ 唯一项目名 (避免 "Design already exists" 错误)
            # ★★ 2026-09-23 批内并发 (cc=10) 防御性加固: 项目名/设计名**加 PID**。
            #   原仅「索引+秒级时间戳」, 多个并发 worker 若在同一秒创建项目会产生
            #   **同名**工程 (MotorAI_1_143022) 且落同一默认工程目录 ⇒ 存在互相踩踏
            #   (同名覆盖 / "Design already exists" / COM 争用) 的风险。
            #   注: 历史「cc=4 挂死」后经重测证实真凶是 Winding879 坏点而非此项,
            #   但加 PID 零成本且彻底消除同名风险 ⇒ 保留。
            _pid_sfx = "%d" % os.getpid()
            proj_name = f"MotorAI_{i+1}_{ts}_{_pid_sfx}"
            design_name = f"Design_{i+1}_{ts}_{_pid_sfx}"

            log(f"[{i+1}/{len(param_list)}] Creating project '{proj_name}', design '{design_name}'...")

            rmx = None
            try:
                # ★ 使用 new_desktop_session=False 复用已启动的 Desktop
                rmx = RmxprtFactory(
                    projectname=proj_name,
                    designname=design_name,
                    solution_type=machine_type_str,
                    non_graphical=True,
                    new_desktop_session=False,   # ★ 复用 Desktop
                    specified_version=aedt_version,
                    close_on_exit=False,
                )
                log(f"[{i+1}] Rmxprt created: project={rmx.project_name}, design={rmx.design_name}")

                # 设置变量
                _write_heartbeat(i, len(param_list), "setting_variables")
                var_set_count = _set_variables_pyaedt(rmx, params, input_map)
                log(f"[{i+1}] Variables set: {var_set_count} OK")

                # 创建 Setup
                try:
                    rmx.create_setup(setupname="Setup1")
                    log(f"[{i+1}] Setup1 created")
                except Exception as e:
                    log(f"[{i+1}] create_setup warning: {e}", "WARN")

                # ★ P0-2: 显式注入绕组接法 (不依赖模板/默认值; 与 motor_tools 相电压口径同源)
                try:
                    _wc = _winding_connection_code(params.get('winding_connection'))
                    rmx.setups["Setup1"].props["WindingConnection"] = _wc
                    log(f"[{i+1}] P0-2 接法注入(pyaedt): WindingConnection={_wc} "
                        f"({_winding_connection_name(params.get('winding_connection'))})")
                except Exception as _e:
                    log(f"[{i+1}] P0-2 接法注入(pyaedt)失败(退回模板默认): {_e}", "WARN")

                # ★ Analyze 带超时保护
                log(f"[{i+1}] Analyzing (timeout=120s)...")
                _write_heartbeat(i, len(param_list), "running_magnetostatic")
                analyze_ok = _analyze_with_timeout(rmx, "Setup1", timeout=120)

                if not analyze_ok:
                    raise TimeoutError("Analyze exceeded 120s timeout")

                # 读取结果
                _write_heartbeat(i, len(param_list), "reading_results")
                raw = _read_results_pyaedt(rmx, output_map)
                # 方案B: 几何截图 (win32 抓屏, best-effort, fail-closed)
                _imgs = _capture_aedt_geometry(_IMG_EXPORT_DIR, proj_name, i + 1, params)
                _res = _sample_aedt_resources()
                solve_time = time.time() - t0

                entry = {
                    "status": "success",
                    "raw_results": raw,
                    "params": params,
                    "solve_time": round(solve_time, 2),
                    "error": "",
                    "images": _imgs,
                    "aedt_resource": _res,
                }
                results.append(entry)
                _append_progress(entry)
                rmxprt_count += 1
                log(f"[{i+1}] Solved OK: {len(raw)} results, {solve_time:.1f}s")

            except Exception as e:
                solve_time = time.time() - t0
                err_detail = traceback.format_exc()
                _lic = "license" in (str(e).lower() + err_detail.lower())
                entry = {
                    "status": "error",
                    "raw_results": {},
                    "params": params,
                    "solve_time": round(solve_time, 2),
                    "error": str(e),
                    "error_detail": err_detail[-2000:] if len(err_detail) > 2000 else err_detail,
                    "licence_error": _lic,
                }
                results.append(entry)
                _append_progress(entry)
                if _lic:
                    log(f"[{i+1}] License 错误: {e}", "ERROR")
                else:
                    log(f"[{i+1}] Error: {e}", "ERROR")

            finally:
                # ★ 每方案循环后关闭项目 (释放资源!)
                if rmx is not None:
                    try:
                        rmx.close_project(save=False)
                        log(f"[{i+1}] Project closed")
                    except Exception as e:
                        log(f"[{i+1}] close_project warning: {e}", "WARN")

        _write_heartbeat(len(param_list), len(param_list), "done")

    finally:
        # 3. ★ 优雅退出 Desktop (避免 "cannot be closed" 警告)
        log("Releasing Desktop...")
        try:
            d.release_desktop(close_projects=True, close_on_exit=True)
            log("Desktop released gracefully")
        except Exception as e:
            log(f"release_desktop warning: {e}", "WARN")
            # 终极兜底
            kill_aedt_processes()

    log(f"Method A done: {rmxprt_count}/{len(param_list)} success")

    # ★ 如果全部失败, 抛出异常触发 win32com 回退
    if rmxprt_count == 0 and len(param_list) > 0:
        first_error = results[0].get("error", "unknown") if results else "no results"
        raise RuntimeError(f"pyaedt method: 0/{len(param_list)} success. First error: {first_error}")

    return results


def _set_variables_pyaedt(rmx, params, input_map):
    """通过 __setitem__ 设置 RMxprt 变量"""
    count = 0
    for param_key, mapping in input_map.items():
        rmxprt_var = mapping.get("rmxprt_var", param_key)
        value = params.get(param_key)
        if value is None:
            default = mapping.get("default")
            if default is not None:
                value = default
            else:
                continue

        # auto_compute
        auto_compute = mapping.get("auto_compute")
        if auto_compute:
            try:
                expr = auto_compute
                for k, v in params.items():
                    expr = expr.replace(k, str(v))
                value = round(float(eval(expr)), 1)
            except Exception:
                pass

        # 单位处理
        if isinstance(value, (int, float)):
            var_lower = rmxprt_var.lower()
            if any(x in var_lower for x in ["diameter", "width", "length", "height", "gap", "tooth", "yoke", "shaft"]):
                value_str = f"{value}mm"
            elif "voltage" in var_lower:
                value_str = f"{value}V"
            elif "speed" in var_lower:
                value_str = f"{value}rpm"
            elif "power" in var_lower:
                value_str = f"{value}kW"
            elif "frequency" in var_lower:
                value_str = f"{value}Hz"
            else:
                value_str = str(value)
        else:
            value_str = str(value)

        try:
            rmx[rmxprt_var] = value_str
            count += 1
        except Exception as e:
            log(f"  Set {rmxprt_var}={value_str} failed: {e}", "WARN")
            # 尝试 change_property 作为回退
            try:
                rmx.change_property(rmx.odesign, "Machine", "Machine", rmxprt_var, value_str)
                count += 1
            except Exception:
                pass

    return count


def _analyze_with_timeout(rmx, setup_name="Setup1", timeout=120):
    """带超时保护的 Analyze

    使用线程执行 analyze_setup, 主线程等待 timeout 秒。
    如果超时, 返回 False (调用者应跳过此方案)。
    """
    result = {"done": False, "error": None}

    def _do_analyze():
        try:
            rmx.analyze_setup(setup_name)
            result["done"] = True
        except Exception as e:
            result["error"] = e
            result["done"] = True  # 标记完成 (有错误)

    t = threading.Thread(target=_do_analyze, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if result["error"]:
        raise result["error"]
    return result["done"]


# v4.02.105 (2026-09-19): 补导出转子槽形几何 (rotor slot geometry)
#   异步机鼠笼转子槽形此前从未生成/提取 (rotor_slot_type=None 走 RMxprt 默认/AutoDesign),
#   本报告需求要求导出转子槽口宽/槽肩高/槽深/导条宽/导条深/槽型等。最稳妥做法: 求解后从
#   RMxprt 工程读回转子槽形 design variable, fail-closed, 绝不阻断求解主流程。
#   注: 定子槽形已通过 Bs0/Hs0/Hs1/Hs2 等 design variable 透传; 转子侧对称命名 Rs0/Rs1/Rs2/
#   BarBreadth/BarDepth。读取失败(变量不存在)则静默跳过, 由报告标注"未导出"。
_ROTOR_SLOT_VARS = [
    ("Brs0", "rotor_slot_Bs0"),            # 转子槽口宽
    ("Hrs0", "rotor_slot_Hs0"),            # 转子槽口高
    ("Hrs1", "rotor_slot_Hs1"),            # 转子槽肩高
    ("Hrs2", "rotor_slot_Hs2"),            # 转子槽深
    ("RotorSlotType", "rotor_slot_type"),  # 槽型 (若模板暴露)
    ("ToothWidth", "rotor_tooth_width"),   # 转子齿宽 (若模板暴露)
]


def _read_rotor_slot_pyaedt(rmx):
    """从 PyAEDT RMxprt 设计读回转子槽形几何 (fail-closed)"""
    out = {}
    for rv, key in _ROTOR_SLOT_VARS:
        for acc in (lambda: rmx.get_output_variable(rv), lambda: rmx[rv]):
            try:
                v = acc()
                if v is not None:
                    out[key] = float(v) if isinstance(v, (int, float)) else v
                    break
            except Exception:
                pass
    return out


def _read_rotor_slot_com(oDesign):
    """从 win32com RMxprt Design 读回转子槽形几何 (fail-closed)"""
    out = {}
    for rv, key in _ROTOR_SLOT_VARS:
        for acc in (lambda: oDesign.GetOutputVariable(rv),
                    lambda: (oDesign.GetDesignVariableValue(rv)
                             if hasattr(oDesign, "GetDesignVariableValue") else None)):
            try:
                v = acc()
                if v is not None:
                    out[key] = float(v) if isinstance(v, (int, float)) else v
                    break
            except Exception:
                pass
    return out


def _read_rotor_slot_from_aedt(aedt_path):
    """★ v4.02.105: 从临时 .aedt 文本解析转子鼠笼槽形 (UserDefinedSlot / UserDefHalfSlot)。

    RMxprt 异步机转子槽形为 SlotType='UserDefinedSlot', 几何由
    $begin 'UserDefHalfSlot' 内的 7× SlotSegmentDef(SegmentType / Value1=径向mm /
    Value2=切向mm) 定义。它既非 design variable (GetDesignVariableValue 返回空),
    也不在 .res 标准报表显式列出, 故直接解析 .aedt 文本 —— 与 modify_aedt_text
    同源, 得到的是 RMxprt 实际求解所用的「缩放后真实输入值」。

    派生量 (HalfSlot=false ⇒ Value2 即全槽切向宽, 不镜像):
      - rotor_slot_depth_mm        = Σ Value1 (槽总径向深)
      - rotor_slot_max_width_mm    = max(Value2) (最大全槽切向宽)
      - rotor_slot_opening_mm      = 首个非零 Value2 (槽口全宽)
      - rotor_slot_segments        = 原始 7 段定义 (供报告逐段列表)

    fail-closed: 任何异常/缺失返回 {} (绝不阻断求解主流程)。
    """
    out = {}
    try:
        import re as _re
        with open(aedt_path, 'r', encoding='utf-8', errors='ignore') as _f:
            _txt = _f.read()
        _b0 = _txt.find("$begin 'UserDefHalfSlot'")
        if _b0 < 0:
            log("  [rotor slot] UserDefHalfSlot block not found in .aedt", "DEBUG")
            return out
        _b1 = _txt.find("$end 'UserDefHalfSlot'", _b0)
        if _b1 < 0:
            _b1 = _b0 + 6000
        _seg = _txt[_b0:_b1]
        _segs = []
        _st = _v1 = _v2 = None
        for _ln in _seg.splitlines():
            _s = _ln.strip()
            _m1 = _re.match(r"^SegmentType=(\d+)", _s)
            _m2 = _re.match(r"^Value1='?([\d.]+)\s*mm'?", _s)
            _m3 = _re.match(r"^Value2='?([\d.]+)\s*mm'?", _s)
            if _m1:
                _st = int(_m1.group(1))
            elif _m2:
                _v1 = float(_m2.group(1))
            elif _m3:
                _v2 = float(_m3.group(1))
                # 一个 segment 的三段齐了 → 收口
                _segs.append({"seg": len(_segs) + 1,
                              "type": _st,
                              "v1_radial_mm": _v1,
                              "v2_tangential_mm": _v2})
                _st = _v1 = _v2 = None
        if not _segs:
            log("  [rotor slot] no SlotSegmentDef parsed", "DEBUG")
            return out
        out["rotor_slot_type"] = "UserDefinedSlot"
        out["rotor_slot_segments"] = _segs
        _depth = sum(s["v1_radial_mm"] for s in _segs)
        out["rotor_slot_depth_mm"] = round(_depth, 3)
        _max_t = max(s["v2_tangential_mm"] for s in _segs)
        out["rotor_slot_max_width_mm"] = round(_max_t, 3)
        _opening = 0.0
        for s in _segs:
            if s["v2_tangential_mm"] > 0:
                _opening = s["v2_tangential_mm"]
                break
        out["rotor_slot_opening_mm"] = round(_opening, 3)
        log(f"  [rotor slot] parsed {len(_segs)} segments: depth={out['rotor_slot_depth_mm']}mm "
            f"max_w={out['rotor_slot_max_width_mm']}mm opening={out['rotor_slot_opening_mm']}mm")
    except Exception as _e:
        log(f"  [rotor slot aedt parse] failed: {_e}", "WARN")
    return out


def _read_results_pyaedt(rmx, output_map):
    """读取 RMxprt 求解结果"""
    raw = {}
    for our_key, mapping in output_map.items():
        rmxprt_var = mapping.get("rmxprt_var", our_key)
        try:
            val = rmx.get_output_variable(rmxprt_var)
            if val is not None:
                raw[rmxprt_var] = float(val)
        except Exception:
            try:
                val = rmx[rmxprt_var]
                if val is not None:
                    raw[rmxprt_var] = float(val)
            except Exception:
                pass
    # v4.02.105: 补导出转子槽形几何
    try:
        raw.update(_read_rotor_slot_pyaedt(rmx))
    except Exception:
        pass
    return raw


# ═══════════════════════════════════════════════════════════════════════════
# v4.02.11 (RMxprt 增强 P1a/P1b): 图片导出 + AEDT 资源监控 — best-effort, fail-closed
#   所有导出/采样全程 try/except 包裹, 任一失败仅返回已成功部分 / 空 dict, 不阻断求解主流程。
#   需 AEDT 运行态与模板支持特定报表, 无法在此环境验证, 故以防御性代码落地。
# ═══════════════════════════════════════════════════════════════════════════
_IMG_EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rmxprt_images")
try:
    os.makedirs(_IMG_EXPORT_DIR, exist_ok=True)
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# v4.02.76 几何截图存储优化（改进方案 v1 的 X + Z + C + 去重）
#   X  BMP → PNG：8.09MB/张 → ~70KB/张（约 120x，纯 stdlib 编码器，
#      worker venv 无需安装 Pillow；宿主若已装 Pillow 则自动优先使用）
#   Z  抓屏即降分辨率：MOTORAI_GEOM_IMAGE_SCALE（默认 0.5，可设 1.0 关掉）
#   C  按 run 分目录落盘：rmxprt_images/<run_id>/，配合 Core/geometry_image_store
#      的「归档即删」语义（run 目录整体淘汰）
#   去重  按几何相关参数签名跳过未变几何的重复抓屏 —— 修复
#      「同一几何在多轮求解中反复抓屏」（实测 geom_01 被抓 22 次）
# 回滚开关：MOTORAI_GEOM_IMAGE_SCALE=1.0 关降分辨率；
#           MOTORAI_GEOM_KEEP_BMP=1 额外保留 BMP；删除本段并恢复 SaveBitmapFile 即可回退。
# ═══════════════════════════════════════════════════════════════════════════════
try:
    _WORKER_DIR = os.path.dirname(os.path.abspath(__file__))
    if _WORKER_DIR not in sys.path:
        sys.path.insert(0, _WORKER_DIR)
    import _geom_image_codec as _geom_codec
    _GEOM_CODEC_OK = True
except Exception as _e_codec:
    _geom_codec = None
    _GEOM_CODEC_OK = False
    log(f"  [geom] PNG 编码器不可用, 回退 BMP: {_e_codec}", "WARN")

_GEOM_RUN_ID = None                      # 懒生成：每个 worker 进程一个 run 目录
_GEOM_SIG_INDEX = None                   # 懒加载：{参数签名: 相对路径} 跨轮持久去重
_GEOM_DEDUP_HITS = 0                     # 本轮去重命中次数（供日志统计）
_GEOM_SIG_INDEX_PATH = os.path.join(_IMG_EXPORT_DIR, "_index.json")


def _geom_scale():
    """Z: 抓屏降分辨率比例（默认 0.5；异常/<=0 → 1.0 即原分辨率）。"""
    try:
        _v = float((os.environ.get("MOTORAI_GEOM_IMAGE_SCALE") or "0.5").strip())
        return _v if _v > 0 else 1.0
    except Exception:
        return 1.0


def _geom_keep_bmp():
    """回滚辅助：置 1 时额外保留临时 BMP（默认转换后即删）。"""
    return (os.environ.get("MOTORAI_GEOM_KEEP_BMP") or "").strip() in ("1", "true", "True")


# ── 抓屏后台化（提案方案A）：无侵入 PrintWindow + 回滚开关 ───────────────────
# MOTORAI_GEOM_CAPTURE_INTRUSIVE=1 → 恢复旧行为（SW_MAXIMIZE + SetForegroundWindow +
#   依赖前台的 BitBlt），会抢焦点/全屏覆盖；仅在无侵入路径异常时临时兜底。默认 0 = 无侵入。
def _geom_capture_intrusive():
    return (os.environ.get("MOTORAI_GEOM_CAPTURE_INTRUSIVE") or "").strip() in ("1", "true", "True")


def _win32_print_window(hwnd, hdc, flags=2):
    """直接调 user32!PrintWindow —— 本环境 pywin32 未导出该符号，故走 ctypes。

    PW_RENDERFULLCONTENT(2) 由 DWM 把窗口内容合成进目标 DC，**窗口被遮挡/非前台/
    处于后台亦可出图** ⇒ 无需 SetForegroundWindow/SW_MAXIMIZE（提案 §2 根因）。
    返回 True=渲染成功；False/异常=调用方回退 BitBlt。
    （实测：普通 GUI 窗口 flag=2 返回 1；控制台/直绘窗口返回 0。）
    """
    try:
        import ctypes
        import ctypes.wintypes as _wt
        _u = ctypes.windll.user32
        _u.PrintWindow.restype = _wt.BOOL
        _u.PrintWindow.argtypes = [_wt.HWND, _wt.HDC, _wt.UINT]
        return bool(_u.PrintWindow(_wt.HWND(int(hwnd)), _wt.HDC(int(hdc)), _wt.UINT(int(flags))))
    except Exception as _e_pw:
        log(f"  [geom-capture] ctypes PrintWindow 不可用, 回退 BitBlt: {_e_pw}", "WARN")
        return False


def _geom_run_dir(base_dir):
    """C: 懒创建本轮 run 目录 rmxprt_images/run_<stamp>_<pid>/，失败回退平铺。"""
    global _GEOM_RUN_ID
    if _GEOM_RUN_ID is None:
        _stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        _GEOM_RUN_ID = f"run_{_stamp}_{os.getpid()}"
    _d = os.path.join(base_dir, _GEOM_RUN_ID)
    try:
        os.makedirs(_d, exist_ok=True)
        return _d
    except Exception:
        return base_dir


def _geom_rel(path):
    """相对 _IMG_EXPORT_DIR 的路径（索引持久化用，避免绝对路径写死）。"""
    try:
        return os.path.relpath(path, _IMG_EXPORT_DIR).replace("\\", "/")
    except Exception:
        return os.path.basename(path)


def _geom_index_load():
    """加载跨轮签名索引，并顺带剔除已失效条目（文件已被 sweeper 清理）。"""
    global _GEOM_SIG_INDEX
    if _GEOM_SIG_INDEX is not None:
        return _GEOM_SIG_INDEX
    _raw = {}
    try:
        with open(_GEOM_SIG_INDEX_PATH, "r", encoding="utf-8") as _f:
            _d = json.load(_f)
        if isinstance(_d, dict):
            _raw = _d
    except Exception:
        _raw = {}
    _live = {}
    for _sig, _rel in _raw.items():
        if os.path.exists(os.path.join(_IMG_EXPORT_DIR, str(_rel).replace("/", os.sep))):
            _live[_sig] = _rel
    _GEOM_SIG_INDEX = _live
    return _GEOM_SIG_INDEX


def _geom_index_save():
    try:
        _tmp = _GEOM_SIG_INDEX_PATH + ".part"
        with open(_tmp, "w", encoding="utf-8") as _f:
            json.dump(_GEOM_SIG_INDEX or {}, _f, ensure_ascii=False)
        os.replace(_tmp, _GEOM_SIG_INDEX_PATH)
    except Exception:
        pass


def _geom_signature(params):
    """几何去重签名：对输入参数字典做稳定 hash（含全部输入，保守但绝不误复用）。"""
    if not isinstance(params, dict) or not params:
        return ""
    try:
        _payload = json.dumps({str(k): params[k] for k in sorted(params, key=str)},
                              sort_keys=True, default=str)
        return hashlib.md5(_payload.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""



def _export_images_pyaedt(rmx, out_dir, proj_name, index):
    """best-effort 导出磁场云图 + 转矩曲线 (pyaedt 路径, P1a)。

    返回已成功导出的图片路径 dict (可能为空)。fail-closed: 异常 → 返回 {}。
    """
    imgs = {}
    try:
        import os
        os.makedirs(out_dir, exist_ok=True)
        _stem = f"{proj_name}_{index}"
        # 1) 设计概览截图 (几何/磁场) — pyaedt Desktop.export_image
        try:
            _desktop = getattr(rmx, "odesktop", None) or getattr(rmx, "desktop", None)
            if _desktop is not None and hasattr(_desktop, "export_image"):
                _p = os.path.join(out_dir, f"{_stem}_view.png")
                _desktop.export_image(_p)
                if os.path.exists(_p):
                    imgs["design_view"] = _p
        except Exception as _e:
            log(f"[{index}] export design_view skipped: {_e}", "WARN")
        # 2) 转矩曲线报表导出 (best-effort, 版本 API 差异大)
        try:
            _rep = rmx.create_report("Torque", "Transient")
            if _rep is not None:
                _p = os.path.join(out_dir, f"{_stem}_torque.png")
                if hasattr(_rep, "export_to_jpg"):
                    _rep.export_to_jpg(_p)
                elif hasattr(_rep, "export_image"):
                    _rep.export_image(_p)
                if os.path.exists(_p):
                    imgs["torque_curve"] = _p
        except Exception as _e:
            log(f"[{index}] export torque_curve skipped: {_e}", "WARN")
    except Exception:
        pass
    return imgs


def _find_aedt_main_window():
    """定位 AEDT 主窗口 (顶层 + 可见 + 标题含 RMxprt/Ansys/Electronics Desktop)。

    返回首个匹配窗口句柄, 未找到返回 0。best-effort。
    注: 用户实测 L1 解算时 AEDT 以 GUI 实例运行 (DispatchEx 另起可见实例),
    故可抓屏; 无桌面会话时返回 0, 调用方 fail-closed。
    """
    try:
        import win32gui
    except Exception:
        return 0
    _found = []

    def _enum(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetParent(hwnd) != 0:
                return
            _title = win32gui.GetWindowText(hwnd) or ""
            if ("RMxprt" in _title) or ("Ansys" in _title) or ("Electronics Desktop" in _title):
                _found.append(hwnd)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception:
        pass
    return _found[0] if _found else 0


def _capture_aedt_geometry(out_dir, proj_name, index, params=None):
    """方案B+X+Z+C+去重: 抓屏 RMxprt Machine Editor 的 2D 截面几何视图。

    机制: RMxprt 设计只有 Machine 属性树编辑器, 无几何导出 API (实测证伪 ExportModelImageToFile);
    其 2D 截面图仅 GUI 渲染 → 只能窗口抓屏。

    v4.02.76 存储优化（改进方案 v1 的 X + Z + C + 去重）:
      - X 格式 BMP → PNG（8.09MB/张 → ~70KB/张，客户端 QPixmap 原生支持）;
      - Z 按 MOTORAI_GEOM_IMAGE_SCALE 降分辨率（默认 0.5）;
      - C 落盘 rmxprt_images/<run_id>/ 分目录;
      - 去重 参数签名命中且原图仍在 → 复制复用，不再重复抓屏。

    无侵入抓屏（提案方案A · 删除抢焦点/最大化）:
      - 删除 SW_MAXIMIZE + SetForegroundWindow（会遮挡全屏 + 强抢焦点，打断用户前台操作）;
      - 改用 PrintWindow(PW_RENDERFULLCONTENT=2)，由 DWM 合成，窗口非前台/被遮挡/后台仍可出图;
        ★ 本环境 pywin32 未导出 PrintWindow（旧版无此符号，旧代码一直 AttributeError → 静默回退
          BitBlt，故必须先抢焦点才能出图）⇒ 现用 ctypes 直接调 user32!PrintWindow 使无侵入成立;
      - sleep 1.2s → 0.3s（无最大化动画，仅等 DWM 合成稳定）;
      - ★ 最小化窗口**不** SW_RESTORE: SW_RESTORE 会从最小化恢复并激活到前台(打断用户),
        本轮已删; AEDT 最小化时 PrintWindow 可能抓空白, 靠去重缓存(_geom_signature 命中即
        复制旧图)兜底; 建议抓屏期让 AEDT 后台可见(不最小化)以无侵入出好图, 必须最小化场景需方案B隔离桌面;
      - 回滚: MOTORAI_GEOM_CAPTURE_INTRUSIVE=1 恢复旧的 SW_MAXIMIZE + SetForegroundWindow 行为。

    调用时机: 必须在该方案 Analyze 之后、CloseProject 之前 (几何只在内存)。
    fail-closed: 任何异常/无窗口/无桌面 → 返回 {} (不影响主求解)。
    """
    global _GEOM_DEDUP_HITS
    try:
        import os
        import time
        import win32gui
        import win32ui
        import win32con
    except Exception as _e:
        log(f"  [geom-capture] win32 不可用, 跳过: {_e}", "WARN")
        return {}
    try:
        # ── 去重: 同一几何不重复抓屏（根治 geom_01 被抓 22 次）──────────────
        _sig = _geom_signature(params)
        if _sig:
            _rel = _geom_index_load().get(_sig)
            if _rel:
                _src = os.path.join(_IMG_EXPORT_DIR, str(_rel).replace("/", os.sep))
                if os.path.exists(_src):
                    _dst = os.path.join(_geom_run_dir(out_dir), os.path.basename(_src))
                    try:
                        if os.path.abspath(_src) != os.path.abspath(_dst):
                            shutil.copy2(_src, _dst)
                        _GEOM_DEDUP_HITS += 1
                        log(f"  [geom-capture] 几何未变, 复用已抓屏 "
                            f"(去重 #{_GEOM_DEDUP_HITS}): {_dst}")
                        return {"geometry": [_dst]}
                    except Exception as _e_cp:
                        log(f"  [geom-capture] 复用复制失败, 改为重新抓屏: {_e_cp}", "WARN")

        out_dir = _geom_run_dir(out_dir)
        os.makedirs(out_dir, exist_ok=True)
        _hwnd = _find_aedt_main_window()
        if not _hwnd:
            log("  [geom-capture] 未找到 AEDT 主窗口, 跳过", "WARN")
            return {}
        # ── 抓屏后台化（提案方案A）: 不再抢焦点 / 不再全屏覆盖 ──────────────
        # 原实现 SW_MAXIMIZE + SetForegroundWindow 会: ① 最大化覆盖全屏遮挡用户桌面;
        # ② 强抢键盘/鼠标焦点 ⇒ 打断用户在其它程序的前台操作（根因见提案 §2）。
        # 默认（无侵入）: 不置顶、不抢焦点, 改用 PrintWindow(PW_RENDERFULLCONTENT=2) 由 DWM
        # 合成出图 —— 窗口被遮挡/非前台/处于后台仍可渲染。完全最小化的窗口在个别
        # AEDT/Windows 版本下 PrintWindow(flag=2) 可能出空白 → 仅 SW_RESTORE 恢复为普通
        # 窗口（不抢焦点）。回滚: MOTORAI_GEOM_CAPTURE_INTRUSIVE=1 恢复旧的抢焦点行为。
        try:
            if _geom_capture_intrusive():
                # 回滚路径: 旧行为(抢焦点 + 全屏覆盖), 仅在无侵入路径异常时临时兜底
                win32gui.ShowWindow(_hwnd, win32con.SW_MAXIMIZE)
                win32gui.SetForegroundWindow(_hwnd)
                time.sleep(1.2)
            else:
                # 无侵入: 绝不调用 ShowWindow / SetForegroundWindow / BringWindowToTop。
                # ★ 关键修复(2026-09-20 第二轮): 即便 AEDT 处于最小化(IsIconic)也**不**
                #   SW_RESTORE —— Windows 上从最小化 SW_RESTORE 会顺带把窗口激活到前台,
                #   打断用户前台操作(用户反馈"AEDT 还会跳前台"的根因即此)。
                #   最小化窗口 DWM 停止合成, PrintWindow(flag=2) 可能抓到空白; 此时靠
                #   去重缓存(_geom_signature 命中即复制旧图)兜底, 不强行激活窗口。
                #   建议: 抓屏期让 AEDT 后台可见(不要最小化)即可无侵入出好图; 必须最小化
                #   场景需方案B隔离桌面(让 AEDT 在独立桌面"可见"被抓)。
                time.sleep(0.3)  # 无最大化动画, 仅等 DWM 合成稳定（原 1.2s）
        except Exception:
            pass
        _left, _top, _right, _bottom = win32gui.GetWindowRect(_hwnd)
        _w = _right - _left
        _h = _bottom - _top
        if _w <= 10 or _h <= 10:
            return {}
        _hwnd_dc = win32gui.GetWindowDC(_hwnd)
        _mfc_dc = win32ui.CreateDCFromHandle(_hwnd_dc)
        _save_dc = _mfc_dc.CreateCompatibleDC()
        _bmp = win32ui.CreateBitmap()
        _bmp.CreateCompatibleBitmap(_mfc_dc, _w, _h)
        _save_dc.SelectObject(_bmp)
        try:
            import ctypes as _ct
            _fg_before = _ct.windll.user32.GetForegroundWindow()
            # PW_RENDERFULLCONTENT=2: 由 DWM 合成窗口内容, 被遮挡/非前台亦可出图
            # (无侵入抓屏前提)。本环境 pywin32 未导出 PrintWindow ⇒ 走 ctypes(见
            # _win32_print_window); 返回 False = 窗口不支持/最小化空白, 回退 BitBlt。
            _pw_ok = _win32_print_window(_hwnd, _save_dc.GetSafeHdc(), 2)
            _fg_after = _ct.windll.user32.GetForegroundWindow()
            if _fg_after == _hwnd and _fg_before != _hwnd:
                log("  [geom-capture][WARN] PrintWindow 期间 AEDT 被激活到前台(异常, 非本函数触发)", "WARN")
            elif _fg_before != _hwnd:
                log(f"  [geom-capture] PrintWindow 无侵入 OK(rc={_pw_ok}, AEDT 未抢前台)")
            if not _pw_ok:
                raise OSError("PrintWindow returned 0")
        except Exception:
            # BitBlt 取屏幕像素, 仅当 AEDT 当前是前台/未被遮挡才出真内容;
            # 被遮挡时抓到的是遮挡物(垃圾图)。无侵入前提下, 非前台则跳过 BitBlt,
            # 靠去重缓存复用旧图, 不产垃圾图也不抢焦点。
            import ctypes as _ct2
            _fg = _ct2.windll.user32.GetForegroundWindow()
            if _fg == _hwnd or _geom_capture_intrusive():
                _save_dc.BitBlt((0, 0), (_w, _h), _mfc_dc, (0, 0), win32con.SRCCOPY)
            else:
                log("  [geom-capture] PrintWindow 失败且 AEDT 非前台, 跳过 BitBlt(避免抓遮挡物), 靠去重缓存", "WARN")
        _stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        _geom_scale_v = _geom_scale()
        if _GEOM_CODEC_OK:
            # X: 先由 GDI 落临时 BMP（复用既有可靠路径），转换后即删
            _bmp_path = os.path.join(out_dir, f".tmp_geom_{index:02d}_{_stamp}.bmp")
            _fpath = os.path.join(out_dir, f"geom_{index:02d}_{_stamp}.png")
        else:
            _bmp_path = os.path.join(out_dir, f"geom_{index:02d}_{_stamp}.bmp")
            _fpath = _bmp_path
        _bmp.SaveBitmapFile(_save_dc, _bmp_path)
        # 清理 GDI 资源: DeleteDC/ReleaseDC 失败一律忽略, 绝不影响"保存成功"的返回
        # (fix: _mfc_dc 由 CreateDCFromHandle(_hwnd_dc) 包装, 其底层窗口 DC 已 ReleaseDC,
        #  不可再 DeleteDC, 否则抛 "DeleteDC failed" 被外层 except 吞掉 → 成功被误判为失败)
        try:
            win32gui.ReleaseDC(_hwnd, _hwnd_dc)
        except Exception:
            pass
        try:
            _save_dc.DeleteDC()
        except Exception:
            pass
        try:
            _mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.DeleteObject(_bmp.GetHandle())
        except Exception:
            pass
        # X: BMP → PNG 无损转换（fail-open: 失败则保留 BMP，截图绝不丢失）
        if _GEOM_CODEC_OK and _bmp_path != _fpath:
            try:
                if os.path.exists(_bmp_path) and os.path.getsize(_bmp_path) > 0:
                    _n = _geom_codec.bmp_to_png_file(_bmp_path, _fpath, scale=_geom_scale_v)
                    log(f"  [geom-capture] BMP→PNG 完成: {os.path.basename(_fpath)} "
                        f"{_n / 1024.0:.1f}KB (scale={_geom_scale_v})")
                    if not _geom_keep_bmp():
                        try:
                            os.remove(_bmp_path)
                        except Exception:
                            pass
                else:
                    _fpath = _bmp_path
            except Exception as _e_cv:
                log(f"  [geom-capture] BMP→PNG 失败, 保留 BMP: {_e_cv}", "WARN")
                _fpath = _bmp_path
        if os.path.exists(_fpath) and os.path.getsize(_fpath) > 0:
            # 去重索引落盘（跨轮持久化：同几何下轮直接复用，不再抓屏）
            if _sig:
                try:
                    _idx = _geom_index_load()
                    _idx[_sig] = _geom_rel(_fpath)
                    _geom_index_save()
                except Exception:
                    pass
            log(f"  [geom-capture] 已抓屏: {_fpath}")
            return {"geometry": [_fpath]}
        return {}
    except Exception as _e:
        log(f"  [geom-capture] 失败(忽略): {_e}", "WARN")
        return {}


def _sample_aedt_resources():
    """best-effort 采样 AEDT 进程资源占用 (P1b 资源监控)。

    返回快照 dict {aedt_mem_mb, aedt_procs}; psutil 不可用 / 无 AEDT 进程 → 空 dict。
    """
    try:
        import psutil
        _procs = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                _n = (p.info.get("name") or "").lower()
                if "ansysedt" in _n:
                    _procs.append(p)
            except Exception:
                pass
        if not _procs:
            return {}
        _rss = sum((p.info.get("memory_info").rss for p in _procs if p.info.get("memory_info")), 0)
        return {
            "aedt_mem_mb": round(_rss / (1024 * 1024), 1),
            "aedt_procs": len(_procs),
        }
    except Exception:
        return {}


# ============================================================
# 方法 C: OpenProject 模板方案 (主方法 — 绕开 InsertDesign)
# ============================================================

# 默认模板 (异步电机, 兼容旧代码引用)
TEMPLATE_AEDT_PATH = MOTOR_TYPE_REGISTRY["induction"]["template"]
TEMPLATE_DESIGN_NAME = MOTOR_TYPE_REGISTRY["induction"]["design_name"]


# ============================================================================
# 已知「模板中本就不存在(由 RMxprt 派生或本就缺省)」的可选注入字段
# fix #1128-②: 这些字段注入目标缺失时只记 DEBUG, 不警告,
#   避免无害/派生变量警告掩盖真实注入失败 (如几何/绕组字段名拼写错位)。
#   - Frequency: RMxprt 按 poles×speed/120 推导, Setup1 一般无此字段 (通用)
#   - PMSM/BLDC: PoleArc 在 Magnet 段定义、Rotor 无 NumberOfSlots (模板缺省)
# ============================================================================
_GLOBAL_BENIGN_FIELDS = {
    ("Setup1", "Frequency"),
}
_BENIGN_ABSENT_FIELDS = {
    "pmsm": {("Machine", "PoleArc"), ("Rotor", "NumberOfSlots")},
    "bldc": {("Machine", "PoleArc"), ("Rotor", "NumberOfSlots")},
}


# ============================================================================
# S2 (v4.02.57+): 磁钢厚磁路方程 —— 弃用 k_rotor 线性缩放
#   根因: 磁钢厚按转子外径线性缩放 (3.5mm × rotor_od/74) 虚高 3.2~4.8 倍,
#         因磁钢厚由磁路方程 h_M = F_M / H_M 决定, 气隙磁动势 F_δ 只与气隙磁密
#         和气隙长度有关, 与转子外径无关 → 任何线性/开方缩放律对磁钢厚都失效。
# ============================================================================
_MAGNET_MATERIALS = {
    # 牌号(小写): (剩磁 B_r [T], 矫顽力 H_c [kA/m]) —— 20°C 标称值
    "xg196/96": (1.25, 900.0),   # NdFeB (模板 Pole.MagnetType=XG196/96)
    "n35sh":    (1.20, 876.0),   # NdFeB 耐温
    "n38sh":    (1.24, 907.0),   # NdFeB 耐温
    "sm2co17":  (0.95, 720.0),   # 钐钴 Sm2Co17 (150°C 剩磁约 0.82~0.90, 此处 20°C 值)
    "smco28":   (1.05, 756.0),   # 钐钴
}


def _magnet_thickness_from_magnetic_circuit(params, air_gap, rotor_od, stator_id):
    """S2 (v4.02.57+): 按磁路方程计算磁钢厚 h_M (mm), 弃用 k_rotor 线性缩放。

    四步磁路 (对齐「永磁电机参考交流电机案例设计方案分析」报告):
      F_δ = B_g · g_eff / μ_0            气隙磁动势 (A)
      F_M = F_δ · K_s                    磁钢需提供磁动势 (含饱和/漏磁)
      H_M = H_c · (1 − k_work)           去磁曲线工作点磁场强度 (A/m)
      h_M = F_M / H_M                    磁钢厚 (m → mm)

    参数 (均可经 params 覆盖, 否则取永磁工程默认):
      B_g    目标气隙磁密峰值 (永磁 0.90~1.00 T; 默认 0.95)
      B_r/H_c 磁钢剩磁/矫顽力 (默认 XG196/96=1.25T/900kA/m; 可传 magnet_br/magnet_hc
             或 magnet_material 查表)
      g_eff  有效气隙 = air_gap × K_c (K_c 卡特系数 1.2)
      K_s    饱和/漏磁系数 1.2
      k_work 工作点剩磁比 0.85 (留 15% 退磁裕度)
    """
    import math
    mu0 = 4e-7 * math.pi
    try:
        B_g = float(params.get('air_gap_flux_density') or params.get('b_gap_target') or 0.95)
    except (TypeError, ValueError):
        B_g = 0.95
    _mat = str(params.get('magnet_material') or params.get('magnet_type') or '').lower()
    _def = _MAGNET_MATERIALS.get(_mat, (1.25, 900.0))
    try:
        B_r = float(params.get('magnet_br') or _def[0])
    except (TypeError, ValueError):
        B_r = _def[0]
    try:
        H_c = float(params.get('magnet_hc') or _def[1]) * 1000.0   # kA/m -> A/m
    except (TypeError, ValueError):
        H_c = _def[1] * 1000.0
    # 气隙: air_gap 缺失则从 (stator_id − rotor_od)/2 反推, 再兜底 1.5mm (永磁推荐 1.5~2.0)
    try:
        g = float(air_gap or 0.0)
    except (TypeError, ValueError):
        g = 0.0
    if g <= 0:
        try:
            g = (float(stator_id) - float(rotor_od)) / 2.0
        except (TypeError, ValueError):
            g = 0.0
    if g <= 0:
        g = 1.5
    K_c = 1.2       # 卡特系数
    K_s = 1.2       # 饱和/漏磁系数
    k_work = 0.85   # 工作点剩磁比 (去磁裕度)
    g_eff = g * K_c * 1e-3   # mm -> m
    if B_r <= B_g:
        B_g = max(0.1, B_r * 0.85)   # 防御: 目标磁密不得超过剩磁
    F_delta = B_g * g_eff / mu0
    F_M = F_delta * K_s
    H_M = H_c * (1.0 - k_work)
    if H_M <= 0:
        return 0.0
    return F_M / H_M * 1e3


def modify_aedt_text(aedt_path, params, machine_type="induction"):
    """★ 文本编辑修改 .aedt 文件参数 (绕开 COM ChangeProperty)

    在 OpenProject 之前调用, 直接编辑 .aedt 文本文件中的属性值。
    使用 $begin 'Section' 标记区分 Stator / Rotor / Machine / Setup1 同名参数。

    支持的业务参数 (通用):
      poles, speed, stator_od, stator_id, core_length,
      slots_stator, slots_rotor, turns_per_coil, parallel_circuits,
      voltage, power, frequency, air_gap, shaft_dia, rotor_od

    电机类型特定参数 (通过 params 传入, 自动匹配):
      PMSM/BLDC: magnet_thickness, magnet_width, pole_type
      SRM: turns_per_pole, stator_poles, rotor_poles
      同步电机: field_turns, field_wire_dia
    """
    import re

    with open(aedt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    original = content  # 保存原始内容用于比较

    # 计算派生参数
    stator_id = params.get('stator_id')
    air_gap = params.get('air_gap')
    rotor_od = params.get('rotor_od')
    if stator_id and air_gap and not rotor_od:
        rotor_od = round(stator_id - 2 * air_gap, 1)

    def replace_in_section(text, section_name, prop_name, new_value, machine_type=machine_type):
        """在 $begin 'section' ... $end 'section' 块内替换 prop_name 的值。

        fix #1128-②: 当目标字段属于「已知模板中不存在的可选派生字段」时,
        缺失只记 DEBUG(不警告), 避免无害警告掩盖真实注入失败。
        """
        pattern = rf"(\$begin\s+'{re.escape(section_name)}'[\s\S]*?{re.escape(prop_name)}=)([^\r\n]+)"
        replacement = rf"\g<1>{new_value}"
        new_text, count = re.subn(pattern, replacement, text, count=1)
        if count == 0:
            _key = (section_name, prop_name)
            _benign = _key in _GLOBAL_BENIGN_FIELDS or _key in _BENIGN_ABSENT_FIELDS.get(machine_type, set())
            if _benign:
                # fix #1128-②: 良性缺省派生字段 → 仅 DEBUG, 不 WARNING(避免无害告警掩盖真实注入失败)
                log(f"  DEBUG: {prop_name} 在 '{section_name}' 段缺省(已知无害, 由 RMxprt 派生/模板缺省), 跳过注入", "DEBUG")
            else:
                # 非良性缺失 = 真实注入失败, 必须保留 WARNING 暴露(不被 ② 掩盖)
                log(f"  WARNING: {prop_name} not found in section '{section_name}'", "WARN")
        # count > 0: 字段已找到并替换, 静默成功(不再误报 not found)
        return new_text

    def _scale_mm(section_name, prop_name, k):
        """★ fix ①: 读取 section 内 prop_name 当前 'NNNmm' 值, 乘以 k 后写回。
        仅缩放非零长度几何; '0mm'(=自动, 如 MagnetWidth/槽满率无关) 或非 mm 值 (如 AUTO/整数计数) 原样跳过,
        避免破坏 RMxprt 自动选线等语义。返回更新后的 content。"""
        if k == 1.0:
            return content
        pat = rf"(\$begin\s+'{re.escape(section_name)}'[\s\S]*?{re.escape(prop_name)}=)([^\r\n]+)"
        m = re.search(pat, content)
        if not m:
            return content
        val = m.group(2).strip().strip("'").strip()
        mm = re.match(r"^([\d.]+)\s*mm$", val)
        if not mm:
            return content
        num = float(mm.group(1))
        if num == 0:
            return content
        new_val = f"'{num * k:.2f}mm'"
        return replace_in_section(content, section_name, prop_name, new_val, machine_type=machine_type)

    # 1. Machine section: NumberOfPoles, ReferenceSpeed
    if params.get('poles') is not None:
        content = replace_in_section(content, 'Machine', 'NumberOfPoles', str(int(params['poles'])))
    if params.get('speed') is not None:
        content = replace_in_section(content, 'Machine', 'ReferenceSpeed', f"'{params['speed']}rpm'")
    # R2 (#1134) 极弧系数 α_p 注入 (PoleArc, Machine 段): 仅当 specs 含 pole_arc 时改写,
    #   否则沿用模板缺省 (仿 power 门控, 避免无谓覆盖导致转矩静默失真)。
    if params.get('pole_arc') is not None:
        _pa = float(params['pole_arc'])
        if 0.0 < _pa <= 1.0:
            content = replace_in_section(content, 'Machine', 'PoleArc', f"{_pa:.3f}")

    # 2. Stator section: OuterDiameter, InnerDiameter, Length, NumberOfSlots
    if params.get('stator_od') is not None:
        content = replace_in_section(content, 'Stator', 'OuterDiameter', f"'{params['stator_od']}mm'")
    if params.get('stator_id') is not None:
        content = replace_in_section(content, 'Stator', 'InnerDiameter', f"'{params['stator_id']}mm'")
    if params.get('core_length') is not None:
        content = replace_in_section(content, 'Stator', 'Length', f"'{params['core_length']}mm'")
    if params.get('slots_stator') is not None:
        content = replace_in_section(content, 'Stator', 'NumberOfSlots', str(int(params['slots_stator'])))

    # 3. Rotor section: OuterDiameter, InnerDiameter, Length, NumberOfSlots
    if rotor_od is not None:
        content = replace_in_section(content, 'Rotor', 'OuterDiameter', f"'{rotor_od}mm'")
    if params.get('shaft_dia') is not None:
        content = replace_in_section(content, 'Rotor', 'InnerDiameter', f"'{params['shaft_dia']}mm'")
    if params.get('core_length') is not None:
        content = replace_in_section(content, 'Rotor', 'Length', f"'{params['core_length']}mm'")
    if params.get('slots_rotor') is not None:
        content = replace_in_section(content, 'Rotor', 'NumberOfSlots', str(int(params['slots_rotor'])))

    # 4. ★ Stator Winding (位于 'Winding' section 内, 非 'Stator' — v4.02.16 修正 section 错位)
    #    极数变更时必须同步重算线圈节距 CoilPitch, 否则绕组与极数失配 →
    #    气隙磁密塌缩 / 转矩失真 (如 6 极模板 yz200-6 强制 4 极仍用 CoilPitch=12)。
    #    CoilPitch = round(定子槽数 / 极数) (整距, 即每极槽数 τ_p = NS/(2·极对数) = NS/极数),
    #    与 NumberOfPoles 保持一致。模板 yz200-6: 72 槽 / 6 极 → CoilPitch=12 (=72/6) 实证。
    #    ⚠ 旧式 NS/(2×极数) 把极对数当极数用 → 节距减半 (6), 与极数失配 →
    #      "Stator slot number does not match pole number" / 气隙磁密塌缩。
    _wp = params.get('poles') or 4
    _ws = params.get('slots_stator') or 0
    if _ws and _wp:
        _coil_pitch = max(1, int(round(_ws / (1.0 * _wp))))
        content = replace_in_section(content, 'Winding', 'CoilPitch', str(_coil_pitch))
    if params.get('parallel_circuits') is not None:
        content = replace_in_section(content, 'Winding', 'ParallelBranches', str(int(params['parallel_circuits'])))

    # 4.5 ★ P0-5 绕组口径分离（2026-10-18，补 Y160L/Y180M 真值时实证）
    #    旧行为: turns_per_coil(l0 口径, 非"每槽导体数") 直灌 ConductorsPerSlot
    #      ⇒ 匝数虚高 3~4 倍(如 Y160L-4 l0 值 31 vs 真实 9) → 反电势暴涨
    #      → 电流/功率/磁密同比例坍缩(实测 15kW 机只出 2.8kW)。
    #    新行为: 显式 conductors_per_slot(真实每槽导体数) 优先;
    #      仅 turns_per_coil 时回退旧行为并 WARN(不静默);
    #      wire_dia>0 注入线径, wire_dia==0 写 '0mm'(RMxprt 按槽满率自动选线)。
    _cps = params.get('conductors_per_slot')
    if _cps is None and params.get('turns_per_coil') is not None:
        _cps = params['turns_per_coil']
        log("  WARN: conductors_per_slot 缺省, 回退 turns_per_coil 直灌 ConductorsPerSlot"
            "（旧口径, 匝数可能虚高, 功率/磁密坍缩风险）", "WARN")
    if _cps is not None:
        content = replace_in_section(content, 'Winding', 'ConductorsPerSlot', f"'{int(_cps)}'")
    if params.get('wire_dia') is not None:
        _wd = float(params['wire_dia'])
        _wd_str = f"'{_wd:.3f}mm'" if _wd > 0 else "'0mm'"
        content = replace_in_section(content, 'Winding', 'WireDiameter', _wd_str)
    if params.get('n_strands') is not None:
        content = replace_in_section(content, 'Winding', 'NumberOfStrands', str(int(params['n_strands'])))

    # 5. Setup1 section: RatedOutputPower, RatedVoltage, RatedSpeed, Frequency
    if params.get('power') is not None:
        power_w = float(params['power']) * 1000  # kW → W
        content = replace_in_section(content, 'Setup1', 'RatedOutputPower', f"'{power_w}W'")
    if params.get('voltage') is not None:
        content = replace_in_section(content, 'Setup1', 'RatedVoltage', f"'{params['voltage']}V'")
    if params.get('speed') is not None:
        content = replace_in_section(content, 'Setup1', 'RatedSpeed', f"'{params['speed']}rpm'")
    if params.get('frequency') is not None:
        content = replace_in_section(content, 'Setup1', 'Frequency', f"'{params['frequency']}Hz'")

    # 6. ★ P0-2 (v4.02.81): 显式注入绕组接法 —— 不再依赖模板 Setup1 的默认值。
    #    位置: Setup1 段内 (与 Frequency 同级, 属 AnalysisSetup 而非 Winding 段)。
    #    映射: 0 = Y (Wye/Star) / 1 = Delta —— 由 yz200-6.aedt 模板值 0 与其求解输出
    #          .res "Winding Connection: Wye" 对照实证。
    #    与 motor_tools.phase_voltage_from_line 的相电压口径严格同源 (同一
    #    d["winding_connection"]), 使「接法」与「相电压」不再可能独立漂移。
    content = replace_in_section(content, 'Setup1', 'WindingConnection',
                                 str(_winding_connection_code(params.get('winding_connection'))))
    log(f"  P0-2 接法注入: WindingConnection="
        f"{_winding_connection_code(params.get('winding_connection'))} "
        f"({_winding_connection_name(params.get('winding_connection'))})")

    # 6. ★ 电机类型特定参数 (根据 machine_type 和 params 中存在的 key 自动处理)

    # PMSM / BLDC: 磁体参数
    if params.get('magnet_thickness') is not None:
        content = replace_in_section(content, 'Rotor', 'MagnetThickness', f"'{params['magnet_thickness']}mm'")
    if params.get('magnet_width') is not None:
        content = replace_in_section(content, 'Rotor', 'MagnetWidth', f"'{params['magnet_width']}mm'")
    if params.get('pole_type') is not None:
        content = replace_in_section(content, 'Rotor', 'PoleType', str(int(params['pole_type'])))
    if params.get('pole_embrace') is not None:
        content = replace_in_section(content, 'Rotor', 'PoleEmbrace', str(params['pole_embrace']))

    # SRM: 磁阻电机特定参数
    if params.get('turns_per_pole') is not None:
        content = replace_in_section(content, 'Stator', 'TurnsperPole', f"'{params['turns_per_pole']}'")
    if params.get('stator_pole_number') is not None:
        content = replace_in_section(content, 'Stator', 'StatorPole', str(int(params['stator_pole_number'])))
    if params.get('rotor_pole_number') is not None:
        content = replace_in_section(content, 'Rotor', 'RotorPole', str(int(params['rotor_pole_number'])))

    # 同步电机: 励磁绕组参数
    if params.get('field_turns') is not None:
        content = replace_in_section(content, 'Rotor', 'FieldWindingTurns', str(int(params['field_turns'])))
    if params.get('field_wire_dia') is not None:
        content = replace_in_section(content, 'Rotor', 'FieldWireDiameter', f"'{params['field_wire_dia']}mm'")

    # 单相感应: 启动电容
    if params.get('capacitor_value') is not None:
        content = replace_in_section(content, 'Setup1', 'CapacitorValue', f"'{params['capacitor_value']}uF'")

    # 通用: FrictionLoss, WindLoss (转轴参数)
    if params.get('friction_loss') is not None:
        content = replace_in_section(content, 'Machine', 'FrictionLoss', f"'{params['friction_loss']}W'")
    if params.get('wind_loss') is not None:
        content = replace_in_section(content, 'Machine', 'WindLoss', f"'{params['wind_loss']}W'")

    # 6.5 ★ fix ① (PMSM/BLDC 治本): 模板残留 550W 尺度几何/运行模式按 k 统一缩放, 使几何自洽解出 power>0。
    #   根因: 仅注入 ~15 字段时, 磁钢(3.5mm)/槽形(0.5~6mm)/运行模式(Generator/InfiniteBus) 停留在 550W 模板值,
    #   与放大后的定子(450mm vs 120mm) 不自洽 → AEDT 执行错误 (②③ 已诚实暴露失败, 本条治本)。
    #   k = stator_od / 模板OD(120mm); 仅当候选未以 section__propname 显式提供时才注入, 尊重上游覆盖。
    # v4.02.98 (方案A 配套): 统一解析 I3(ToothWidth) 依赖 —— 原 _sid/_slots_new 仅在下方
    #   pmsm/bldc/ipm 分支 (L1513-1527) 内定义, 异步(induction) 路径变量缺失致 I3 条件
    #   `_sid > 0` 恒不成立 ⇒ ToothWidth 恒为模板 '0mm' (平行齿更依赖显式齿宽)。
    #   此处对所有机型统一解析一次, PM 分支随后覆盖, 异步路径因此首次生效。
    try:
        _sid = float(params.get('stator_id') or 0)
    except Exception:
        _sid = 0.0
    try:
        _slots_new = int(params.get('slots_stator') or 0)
    except Exception:
        _slots_new = 0

    if machine_type in ("pmsm", "bldc", "ipm"):
        # ===== 模板基准 (PMSG_4p50Hz550W.aedt; BLDC/ipm 同 550W 尺度, 基准一致) =====
        _TPL_OD = 120.0        # Stator.OuterDiameter (mm)
        _TPL_ID = 75.0         # Stator.InnerDiameter (mm)
        _TPL_SLOTS = 24        # Stator.NumberOfSlots
        _TPL_ROTOR_OD = 74.0   # Rotor.OuterDiameter (mm)
        _TPL_RADIAL = _TPL_OD - _TPL_ID  # 径向槽深空间基准 (45mm)
        try:
            _sod = float(params.get('stator_od') or 0)
        except Exception:
            _sod = 0.0
        try:
            _sid = float(params.get('stator_id') or 0)
        except Exception:
            _sid = 0.0
        try:
            _sr_od = float(rotor_od or 0)  # rotor_od 已于函数前部派生 (stator_id - 2*air_gap)
        except Exception:
            _sr_od = 0.0
        try:
            _slots_new = int(params.get('slots_stator') or 0)
        except Exception:
            _slots_new = 0

        # ===== 分轴锚定缩放系数 (D1-D4) =====
        _k        = (_sod / _TPL_OD) if _sod > 1e-6 else 1.0                     # 整体(锚外径), 仅次要长度字段
        _k_id     = (_sid / _TPL_ID) if _sid > 1e-6 else _k                     # D1 槽宽锚内径
        _k_radial = ((_sod - _sid) / _TPL_RADIAL) if (_sod > _sid > 0) else _k  # D2 槽深锚径向空间
        _k_rotor  = (_sr_od / _TPL_ROTOR_OD) if _sr_od > 1e-6 else _k           # D4 磁钢锚转子外径
        # D3 槽数重定槽宽: 齿距 = π·id/slots, 槽宽随齿距缩放 → 乘 (模板槽数/新槽数)
        _slot_ratio = (_TPL_SLOTS / _slots_new) if _slots_new > 0 else 1.0
        _k_slot_w = _k_id * _slot_ratio                                          # D1 + D3
        log(f"  [fix①] PMSM scale k(od)={_k:.3f} k_id={_k_id:.3f} k_radial={_k_radial:.3f} "
            f"k_rotor={_k_rotor:.3f} k_slot_w={_k_slot_w:.3f} (slots {_TPL_SLOTS}->{_slots_new})", "DEBUG")

        # 运行模式: 模板 Generator/InfiniteBus → Motor/RatedLoad (执行错误主因之一)
        #   注意 OperationType/LoadType 位于 Setup1 嵌套的 SolveSetup 块内 (非 Machine 段)
        if 'Setup1__OperationType' not in params:
            content = replace_in_section(content, 'Setup1', 'OperationType', "'Motor'")
        if 'Setup1__LoadType' not in params:
            content = replace_in_section(content, 'Setup1', 'LoadType', "'RatedLoad'")

        # S2 (v4.02.57+): 磁钢厚按磁路方程计算, 弃用 k_rotor 线性缩放
        #   根因: 线性缩放 h_M=3.5mm×(rotor_od/74) 虚高 3.2~4.8 倍 (本案例 38.4 vs 物理 8~15mm);
        #   磁钢厚由磁路方程 h_M=F_M/H_M 决定, F_δ 只与气隙磁密/气隙长度有关, 与转子外径无关。
        #   MagnetWidth='0mm'=按极弧自动, 不缩放 (Bridge/Rib/Offset 仍按 k_rotor 缩放, 见下)。
        if 'Pole__MagnetThickness' not in params:
            _h_m = _magnet_thickness_from_magnetic_circuit(params, air_gap, rotor_od, stator_id)
            if _h_m and _h_m > 0:
                content = replace_in_section(content, 'Pole', 'MagnetThickness', f"'{_h_m:.2f}mm'")
        # 永磁转子桥/肋/偏移同锚转子 (0 跳过)
        for _p in ('Bridge', 'Rib', 'Offset'):
            if f'Pole__{_p}' not in params:
                content = _scale_mm('Pole', _p, _k_rotor)

        # D2 槽深锚径向空间 (Hs*), D1/D3 槽宽锚内径+槽数 (Bs*); section 名修正为 'Slot'
        #   执行错误主因: 550W 微槽装不下放大后的导体
        for _p in ('Hs0', 'Hs1', 'Hs2'):
            if f'Slot__{_p}' not in params and f'Stator__{_p}' not in params:
                content = _scale_mm('Slot', _p, _k_radial)
        # P0-2 (v4.02.59+): k_slot_w 自洽重算 —— 模板 Bs2/τ=69% 超工程上限 50%,
        #   缩放后 Bs2 仍 69% → 槽底过宽、齿过窄、轭被槽挤薄 (截图「槽深/轭窄」成因之一)。
        #   钳制 k_slot_w 使 Bs2 缩放后 ≤ 0.45×新齿距, 保平行齿几何自洽。
        import math as _math_p02
        _bs2_tpl = 6.8   # 模板 Slot.Bs2 (实测 PMSG_4p50Hz550W.aedt)
        if _slots_new > 0 and _sid > 0 and _bs2_tpl > 0:
            _pitch_new = _math_p02.pi * _sid / _slots_new
            _k_slot_w_max = 0.45 * _pitch_new / _bs2_tpl
            if _k_slot_w_max > 0 and _k_slot_w > _k_slot_w_max:
                log(f"  [P0-2] k_slot_w 钳制 {_k_slot_w:.3f} -> {_k_slot_w_max:.3f} "
                    f"(Bs2≤0.45×齿距 {_pitch_new:.1f}mm)", "DEBUG")
                _k_slot_w = _k_slot_w_max
        for _p in ('Bs0', 'Bs1', 'Bs2'):
            if f'Slot__{_p}' not in params and f'Stator__{_p}' not in params:
                content = _scale_mm('Slot', _p, _k_slot_w)

        # 定子/绕组次要长度字段按整体 k 缩放 (0=自动 跳过)
        for _sec, _p in [('Stator', 'SkewWidth'), ('Winding', 'SlotLiner'),
                         ('Winding', 'WedgeThickness'), ('Winding', 'InterturnInsulation')]:
            if f'{_sec}__{_p}' not in params:
                content = _scale_mm(_sec, _p, _k)

        # ===== I1 (v4.02.57+): 关闭 AutoDesign —— 「定子槽形缺失」根因修复 =====
        #   实测四模板 Slot.AutoDesign: PMSG=true(独有) / BLDC=false / ipm=false / yz200-6=false。
        #   AutoDesign=true → RMxprt 按 NumberOfSlots×ID 自动设计, 忽略显式 Hs/Bs/ToothWidth,
        #   NumberOfSlots 变更后状态机 fallback 到 slotless → bore-OD 间光面环。
        #   强制 false 让显式槽形生效; 对 bldc/ipm 幂等无害(模板本就是 false)。
        if 'Slot__AutoDesign' not in params:
            content = replace_in_section(content, 'Slot', 'AutoDesign', 'false')

        # ===== I2 (v4.02.57+, 实测修正): SlotType 原生值已正确, 不做强制覆盖 =====
        #   PMSG='1'平行齿 / BLDC='2'梯形槽 / ipm=2梯形槽 / yz200-6='2'梯形槽 —— 各模板自洽。
        #   ParallelTooth 为旧版遗留字段(四模板均 false), 以 SlotType 为准, 不动。
        #   如需类型级覆盖, 上游经 Stator__SlotType 键走 #7 通用扩展注入即可。

        # ===== I3 (v4.02.57+): ToothWidth 显式 —— 消除 AutoDesign 关闭后的齿宽歧义 =====
        #   模板残留 ToothWidth='0mm'(AutoDesign 模式派生; 关闭后无值)。上游显式给 Slot__Bs2 时,
        #   齿宽 = 齿距 − Bs2, 保证平行齿几何自洽。
        # v4.02.97 (第2条, 仅定子): 齿宽改按 Bs1 口径 —— 齿距取 D = stator_id + 2*(Hs0+Hs1),
        #   槽宽取 Bs1 (缺则退 Bs2); 控制齿宽 = π·D_ctrl/Q − Bs1 (齿磁密控制截面 = 齿槽比面)。
        #   AutoDesign=true 时交由 RMxprt 自派生, 不显式覆盖。
        _ad_on = str(params.get('Slot__AutoDesign', '')).strip().strip("'").lower() in ('true', 'yes', '1')
        if (not _ad_on and 'Slot__ToothWidth' not in params
                and ('Slot__Bs1' in params or 'Slot__Bs2' in params)
                and _slots_new > 0 and _sid > 0):
            import re as _re
            import math as _math

            def _mm_v97(_k):
                _mm_m = _re.match(r"^'?([\d.]+)\s*mm'?$", str(params.get(_k, '')).strip())
                return float(_mm_m.group(1)) if _mm_m else 0.0
            _bs_v = _mm_v97('Slot__Bs1') or _mm_v97('Slot__Bs2')
            _hs0v = _mm_v97('Slot__Hs0')
            _hs1v = _mm_v97('Slot__Hs1')
            _d_ctrl = (_sid + 2.0 * (_hs0v + _hs1v)) if (_hs0v > 0 or _hs1v > 0) else _sid
            if _bs_v > 0:
                _tw = _math.pi * _d_ctrl / _slots_new - _bs_v
                if _tw > 0:
                    content = replace_in_section(content, 'Slot', 'ToothWidth', "'{:.2f}mm'".format(_tw))

        # ===== P1-5 (v4.02.59+): 运行态机械损耗按 k 缩放 =====
        #   根因: 模板 FrictionLoss='12W'/WindLoss='0W' 固定 550W 尺度, 放大后不缩放 →
        #   机械损耗占额定功率比例失真 (500kW 级机械损耗应 ~kW 级), 影响效率/温升口径。
        #   FrictionLoss ∝ D²L·转速 ≈ k²; WindLoss ∝ 表面积 ≈ k (模板 WindLoss=0, 缩放后仍 0)。
        #   注意: _scale_mm 仅匹配 'NNNmm', 机械损耗为 'NNNW', 需单独按 W 单位缩放。
        if 'Machine__FrictionLoss' not in params:
            import re as _re_p15
            _m_fl = _re_p15.search(
                rf"(\$begin\s+'Machine'[\s\S]*?FrictionLoss=)([^\r\n]+)", content)
            if _m_fl:
                _v_fl = _m_fl.group(2).strip().strip("'").strip()
                _mm_fl = _re_p15.match(r"^([\d.]+)\s*W$", _v_fl)
                if _mm_fl and float(_mm_fl.group(1)) > 0:
                    _nv_fl = float(_mm_fl.group(1)) * (_k * _k)
                    content = replace_in_section(content, 'Machine', 'FrictionLoss',
                                                 "'{:.2f}W'".format(_nv_fl))
        if 'Machine__WindLoss' not in params:
            import re as _re_p15w
            _m_wl = _re_p15w.search(
                rf"(\$begin\s+'Machine'[\s\S]*?WindLoss=)([^\r\n]+)", content)
            if _m_wl:
                _v_wl = _m_wl.group(2).strip().strip("'").strip()
                _mm_wl = _re_p15w.match(r"^([\d.]+)\s*W$", _v_wl)
                if _mm_wl and float(_mm_wl.group(1)) > 0:
                    _nv_wl = float(_mm_wl.group(1)) * _k
                    content = replace_in_section(content, 'Machine', 'WindLoss',
                                                 "'{:.2f}W'".format(_nv_wl))

    # 6.5b ★ fix①-IND (异步机治本, 对齐 PMSM fix①): 定子槽 + 转子鼠笼槽/端环几何按 k 统一缩放。
    #   根因: 仅注入 ~15 字段时, 定子槽形(Hs0/Hs1/Hs2/Bs0/Bs1/Bs2)与转子 UserDefHalfSlot
    #   (Value1/Value2)/端环(EndRingWidth/Height) 停留模板 yz200-6 尺度(OD=327/ID=210/转子209.2),
    #   与放大后的定子(OD~1359/ID~853)/转子(OD~851) 不自洽 →
    #   定子槽面积塌缩 + 转子鼠笼条过小(截图为证: 转子无可见槽形) → Pout~0.04W 退化求解。
    #   k 锚定: 定子槽深 Hs* 锚径向空间 (OD-ID); 定子槽宽 Bs* 锚内径×槽数比 (钳 Bs2 ≤ 0.45 齿距);
    #   转子鼠笼槽 UserDefHalfSlot 段 Value1/Value2 与端环锚转子外径 (与 PMSM fix① 同口径)。
    if machine_type == "induction":
        # ===== 模板基准 (indm3/yz200-6.aedt) =====
        _TPL_OD_I = 327.0
        _TPL_ID_I = 210.0
        _TPL_SLOTS_I = 72
        _TPL_ROTOR_OD_I = 209.2
        _TPL_ROTOR_SLOTS_I = 58   # 修1: 模板转子槽数 (Rotor.NumberOfSlots=58, 取证 2026-09-16)
        _TPL_RADIAL_I = _TPL_OD_I - _TPL_ID_I
        try:
            _sod_i = float(params.get('stator_od') or 0)
        except Exception:
            _sod_i = 0.0
        try:
            _sid_i = float(params.get('stator_id') or 0)
        except Exception:
            _sid_i = 0.0
        try:
            _sr_od_i = float(rotor_od or 0)
        except Exception:
            _sr_od_i = 0.0
        try:
            _slots_i = int(params.get('slots_stator') or 0)
        except Exception:
            _slots_i = 0
        # ===== 分轴锚定缩放系数 =====
        _k_i = (_sod_i / _TPL_OD_I) if _sod_i > 1e-6 else 1.0
        _k_id_i = (_sid_i / _TPL_ID_I) if _sid_i > 1e-6 else _k_i
        _k_rad_i = ((_sod_i - _sid_i) / _TPL_RADIAL_I) if (_sod_i > _sid_i > 0) else _k_i
        _k_rotor_i = (_sr_od_i / _TPL_ROTOR_OD_I) if _sr_od_i > 1e-6 else _k_i
        _slot_ratio_i = (_TPL_SLOTS_I / _slots_i) if _slots_i > 0 else 1.0
        _k_slot_w_i = _k_id_i * _slot_ratio_i
        import math as _m_i
        _bs2_tpl_i = 8.2   # 模板 Slot.Bs2
        if _slots_i > 0 and _sid_i > 0 and _bs2_tpl_i > 0:
            _pitch_i = _m_i.pi * _sid_i / _slots_i
            _k_slot_w_max_i = 0.45 * _pitch_i / _bs2_tpl_i
            if _k_slot_w_max_i > 0 and _k_slot_w_i > _k_slot_w_max_i:
                _k_slot_w_i = _k_slot_w_max_i
        # 修1 (v4.04.0 2026-09-16): 转子鼠笼槽切向锚「槽距比」+ ≤0.45×槽距 钳制
        #   根因: 旧版 UserDefHalfSlot 的 Value1/Value2 共用 _k_rotor_i(转子外径比),
        #   转子外径放大而槽数变化时切向宽度超出新槽距(π·rotor_od/N) → RMxprt 报
        #   "The user defined slots overlap"。现切向(Value2)改锚槽距比 (OD/N)new÷(OD/N)tpl
        #   并逐值钳制 ≤0.45×新槽距; 径向(Value1)保持 OD 比 (与 _diag_rmx_ab_test.py
        #   消除重叠的 k_rotor_tan 口径一致)。
        try:
            _rslots_i = int(params.get('slots_rotor') or 0)
        except Exception:
            _rslots_i = 0
        # v4.02.97 成对落地 (转子镜像): 转子控制截面口径 D_ctrl = rod − 2*(Hrs0+Hrs1)
        #   (与 motor_tools._rotor_bs1_control 同源)。Hrs0/Hrs1 优先取候选 params 显式
        #   回写 Rotor__Hrs0/Hrs1; 缺省回退模板 yz200-6 UserDefHalfSlot 前两段 V1
        #   (槽口 0.5 / 斜壁1 0.5mm) × k_rotor —— 不臆造, 也不落回外径面旧口径。
        _tpl_hrs0_i = 0.5   # 模板 UserDefHalfSlot 段1 V1 (槽口, 径向)
        _tpl_hrs1_i = 0.5   # 模板 UserDefHalfSlot 段2 V1 (斜壁1, 径向)
        _v97_hrs0_i = _tpl_hrs0_i * _k_rotor_i
        _v97_hrs1_i = _tpl_hrs1_i * _k_rotor_i
        for _v97k_i in ('Rotor__Hrs0', 'Rotor__Hrs1'):
            _v97v_i = params.get(_v97k_i)
            if _v97v_i not in (None, ''):
                try:
                    _v97f_i = float(str(_v97v_i).replace('mm', '').strip("'"))
                except Exception:
                    _v97f_i = None
                if _v97f_i is not None:
                    if _v97k_i.endswith('Hrs0'):
                        _v97_hrs0_i = _v97f_i
                    else:
                        _v97_hrs1_i = _v97f_i
        _k_rotor_tan_i = _k_rotor_i
        _rotor_pitch_new_i = 0.0
        _d_ctrl_new_i = 0.0
        _d_ctrl_tpl_i = _TPL_ROTOR_OD_I - 2.0 * (_tpl_hrs0_i + _tpl_hrs1_i)
        if _rslots_i > 0 and _sr_od_i > 1e-6 and _TPL_ROTOR_SLOTS_I > 0:
            # v4.02.97: L1711 锚点切控制截面口径 —— 槽距 = π·(rod − 2*(Hrs0+Hrs1))/Q_r
            #   (旧口径 π·rod/Q_r 锚在外径面; 模板 −0.86% / 450kW 实例 −0.96%, 同向收紧)
            _d_ctrl_new_i = _sr_od_i - 2.0 * (_v97_hrs0_i + _v97_hrs1_i)
            if _d_ctrl_new_i > 1e-6:
                _rotor_pitch_new_i = _m_i.pi * _d_ctrl_new_i / _rslots_i
                _rp_tpl_i = _m_i.pi * _d_ctrl_tpl_i / _TPL_ROTOR_SLOTS_I
                _k_rotor_tan_i = (_rotor_pitch_new_i / _rp_tpl_i) if _rp_tpl_i > 1e-6 else _k_rotor_i
        log(f"  [v4.02.97 ROTOR-Bs1] 控制截面 D_ctrl={_d_ctrl_new_i:.2f}mm "
            f"(Hrs0={_v97_hrs0_i:.3f} Hrs1={_v97_hrs1_i:.3f}, 模板 Hrs0/1×k_rotor={_tpl_hrs0_i*_k_rotor_i:.3f}/"
            f"{_tpl_hrs1_i*_k_rotor_i:.3f}) pitch=π·D_ctrl/Q_r={_rotor_pitch_new_i:.3f} "
            f"k_rotor_tan={_k_rotor_tan_i:.4f}", "DEBUG")
        log(f"  [fix①-IND] induction scale k(od)={_k_i:.3f} k_id={_k_id_i:.3f} "
            f"k_radial={_k_rad_i:.3f} k_rotor={_k_rotor_i:.3f} k_slot_w={_k_slot_w_i:.3f} "
            f"k_rotor_tan={_k_rotor_tan_i:.3f} (stator slots {_TPL_SLOTS_I}->{_slots_i}, "
            f"rotor slots {_TPL_ROTOR_SLOTS_I}->{_rslots_i})", "DEBUG")
        # --- 定子槽 (Slot 段): Hs* 锚径向空间, Bs* 锚内径×槽数比 ---
        for _p in ('Hs0', 'Hs1', 'Hs2'):
            if f'Slot__{_p}' not in params and f'Stator__{_p}' not in params:
                content = _scale_mm('Slot', _p, _k_rad_i)
        for _p in ('Bs0', 'Bs1', 'Bs2'):
            if f'Slot__{_p}' not in params and f'Stator__{_p}' not in params:
                content = _scale_mm('Slot', _p, _k_slot_w_i)
        # --- 转子端环 (Rotor 的 Winding 段): 锚转子外径 ---
        for _erp in ('EndRingWidth', 'EndRingHeight'):
            _key_i = _erp + "='"
            _idx_i = content.find(_key_i)
            if _idx_i >= 0:
                _q0_i = _idx_i + len(_key_i)
                _q1_i = content.find("'", _q0_i)
                _num_i = content[_q0_i:_q1_i]
                if _num_i.endswith("mm"):
                    try:
                        _n0_i = float(_num_i[:-2])
                    except ValueError:
                        _n0_i = 0.0
                    if _n0_i > 0:
                        content = content[:_q0_i] + f"{_n0_i * _k_rotor_i:.2f}mm" + content[_q1_i:]
        # --- 转子鼠笼槽 (UserDefHalfSlot 段内 Value1/Value2): 锚转子外径 ---
        _b0_i = content.find("$begin 'UserDefHalfSlot'")
        _b1_i = content.find("$end 'UserDefHalfSlot'", _b0_i) if _b0_i >= 0 else -1
        if _b0_i >= 0 and _b1_i > _b0_i:
            _seg_i = content[_b0_i:_b1_i]
            _seg_out_i = []
            for _ln_i in _seg_i.splitlines(keepends=True):
                _st_i = _ln_i.lstrip()
                if _st_i.startswith("Value1='") or _st_i.startswith("Value2='"):
                    _qa_i = _ln_i.find("'")
                    _qb_i = _ln_i.find("'", _qa_i + 1)
                    _num2_i = _ln_i[_qa_i + 1:_qb_i]
                    if _num2_i.endswith("mm"):
                        try:
                            _v2_i = float(_num2_i[:-2])
                        except ValueError:
                            _v2_i = 0.0
                        if _v2_i > 0:
                            # 修1: Value1=径向(槽深) → OD 比; Value2=切向(槽宽) → 槽距比
                            if _st_i.startswith("Value2='"):
                                _k_used_i = _k_rotor_tan_i
                                _scaled_i = _v2_i * _k_used_i
                                # v4.02.97: ≤0.45×新转子槽距 钳制 (逐值) —— 槽距已切控制截面
                                #   口径 D_ctrl = rod − 2*(Hrs0+Hrs1) (L1711 同批), 钳制随截面自动收紧
                                if _rotor_pitch_new_i > 0:
                                    _cap_i = 0.45 * _rotor_pitch_new_i
                                    if _scaled_i > _cap_i:
                                        log(f"  [修1] 切向 Value2 {_v2_i}mm×{_k_used_i:.3f}="
                                            f"{_scaled_i:.2f}mm 超 0.45×槽距={_cap_i:.2f}mm, 钳制", "DEBUG")
                                        _scaled_i = _cap_i
                                _ln_i = _ln_i[:_qa_i + 1] + f"{_scaled_i:.2f}mm" + _ln_i[_qb_i:]
                            else:
                                _ln_i = _ln_i[:_qa_i + 1] + f"{_v2_i * _k_rotor_i:.2f}mm" + _ln_i[_qb_i:]
                _seg_out_i.append(_ln_i)
            content = content[:_b0_i] + "".join(_seg_out_i) + content[_b1_i:]

            # --- I3' (v4.02.98): 异步(IND) 路径补显式齿宽 ToothWidth -----------------
            #   原 I3 (L1606) 位于 pmsm/bldc/ipm 分支 (缩进 8) 内, 异步路径恒不执行 ⇒
            #   ToothWidth 保持模板残留 '0mm'。平行齿 (Stator.SlotType='1') 更依赖显式齿宽,
            #   故在此按 Bs1 口径补写: ToothWidth = π·D_ctrl/Q − Bs1,
            #   D_ctrl = stator_id + 2*(Hs0+Hs1) —— 与 I3 / motor_tools._stator_bs1_control 同口径。
            _ad_on_i = str(params.get('Slot__AutoDesign', '')).strip().strip("'").lower() in (
                'true', 'yes', '1')
            if (not _ad_on_i and 'Slot__ToothWidth' not in params
                    and _slots_i > 0 and _sid_i > 0):
                import re as _re_i

                def _mm_i(_k):
                    _m = _re_i.match(r"^'?([\d.]+)\s*mm'?$", str(params.get(_k, '')).strip())
                    return float(_m.group(1)) if _m else 0.0

                _bs_i = _mm_i('Slot__Bs1') or _mm_i('Slot__Bs2')
                _hs0_i = _mm_i('Slot__Hs0')
                _hs1_i = _mm_i('Slot__Hs1')
                _dc_i = (_sid_i + 2.0 * (_hs0_i + _hs1_i)) if (_hs0_i > 0 or _hs1_i > 0) else _sid_i
                if _bs_i > 0:
                    _tw_i = _m_i.pi * _dc_i / _slots_i - _bs_i
                    if _tw_i > 0:
                        content = replace_in_section(content, 'Slot', 'ToothWidth',
                                                     "'{:.2f}mm'".format(_tw_i))
                        log(f"  [v4.02.98 IND-I3] ToothWidth={_tw_i:.2f}mm "
                            f"(D_ctrl={_dc_i:.2f}, Q={_slots_i}, Bs1={_bs_i:.3f})", "DEBUG")

    # 7. ★ 通用扩展: 遍历 params 中的额外参数 (key 格式: section__propname)
    for key, val in params.items():
        if '__' in key and key not in ('stator_id', 'rotor_od'):
            parts = key.split('__', 1)
            if len(parts) == 2:
                section, prop = parts
                try:
                    content = replace_in_section(content, section, prop, str(val))
                except Exception:
                    pass

    # ===== B-1/B-2 (L0 独有杠杆 L1 补注入, 防坍缩假绿灯) =======================
    #   背景: RMxprt(L1) worker 历史上不消费 tooth_width / yoke_thickness 短名 —— 齿宽只经
    #   Slot__Bs1/Bs2 自动派生(I3/I3'), 轭厚只有缩放上界钳, 无直驱路径 ⇒ L0 这两个杠杆在
    #   L1 层坍缩(L1 解不随 tooth/yoke 档位变化), 产生"假绿灯"盲区(见 P2-4/P2-5)。本块在
    #   通用扩展(#7)之后直驱两杠杆, 压过 I3/I3' 自动派生, 并做回读自检(grep 回写值进日志,
    #   防假绿灯: 临时 .aedt 目录删了仍有证据):
    #     B-1 tooth_width    → Slot.ToothWidth (含 BUG11 齿距上界 self-check, D_ctrl 口径同 I3)
    #     B-2 yoke_thickness → Slot.Hs2 残差反驱 (模板 yz200-6 定子无独立轭键,
    #        T_yoke=(OD-ID)/2-(Hs0+Hs1+Hs2), 给定 yoke 反解 Hs2_new=half-Hs0-Hs1-yoke)
    def _read_mm(section, prop):
        """从当前 content 读 section 段 prop 的 'NNNmm' 数值, 失败返回 0.0。"""
        m = re.search(r"\$begin\s+'" + re.escape(section) + r"'[\s\S]*?"
                      + re.escape(prop) + r"=('[\d.eE+-]+(?:mm)?)'", content)
        if not m:
            return 0.0
        raw = m.group(1).strip().strip("'")
        num = re.match(r"^\s*([\d.eE+-]+)", raw)
        return float(num.group(1)) if num else 0.0

    import math as _b_math

    # --- B-1: tooth_width 残差反驱 Slot.Bs1 (P2-5: 求解器吃槽宽, 不吃派生 ToothWidth) ---
    #   pilot 铁证 (rmx39_pilotB): 固定 yoke=38, tooth 4/7/9 (写 Slot.ToothWidth 字段)
    #   → L1 Bt/η 全等(坍缩), 证明 openproject 求解器按 槽宽Bs1 自派生齿宽(齿距−Bs1), 不读
    #   ToothWidth 派生展示字段。故 B-1 改与 B-2 同构: 给定 L0 齿宽 tooth → 残差反驱
    #   槽宽 Bs1_new = pitch − tooth (求解器真吃 Bs1 ⇒ Bt 随档位单调, 盲区消除), 同步写
    #   ToothWidth 仅展示。只读缩放后 Hs0/Hs1 (fix①-IND 已发生), pitch=π·D_ctrl/Q 同 I3 口径。
    if params.get('tooth_width') not in (None, ''):
        try:
            _b1_tw = float(str(params['tooth_width']).replace('mm', '').strip("'"))
        except Exception:
            _b1_tw = None
        if _b1_tw and _b1_tw > 0:
            _b1_q = int(params.get('slots_stator') or 0)
            _b1_id = _read_mm('Stator', 'InnerDiameter')
            _b1_hs0 = _read_mm('Slot', 'Hs0')
            _b1_hs1 = _read_mm('Slot', 'Hs1')
            _b1_dc = _b1_id + 2.0 * (_b1_hs0 + _b1_hs1) if (_b1_hs0 > 0 or _b1_hs1 > 0) else _b1_id
            _b1_pitch = _b_math.pi * _b1_dc / _b1_q if _b1_q > 0 else 0.0
            _b1_bs_new = _b1_pitch - _b1_tw if _b1_pitch > 0 else 0.0
            _b1_bs_wrote = _b1_bs_new
            _b1_clamped = False
            if _b1_bs_new < 0.5:
                log("  [B-1] tooth_width=%.2fmm 使 Bs1=%.2fmm < 0.5mm (齿宽超槽距), 钳 0.5mm 标记不可满足"
                    % (_b1_tw, _b1_bs_new), "WARN")
                _b1_bs_wrote = 0.5
                _b1_clamped = True
            if _b1_pitch > 0:
                content = replace_in_section(content, 'Slot', 'Bs1', "'%.2fmm'" % _b1_bs_wrote)
                content = replace_in_section(content, 'Slot', 'ToothWidth', "'%.2fmm'" % _b1_tw)
            # 回读自检: 从写入后 content grep 回 Bs1/ToothWidth 实际值 (防假绿灯, 临时目录删了仍有证据)
            _b1_bs_back = _read_mm('Slot', 'Bs1')
            _b1_tw_back = _read_mm('Slot', 'ToothWidth')
            log("  [B-1] tooth=%.2fmm → Bs1 残差反驱写=%.2fmm 回读=%.2fmm "
                "(pitch=π·%.1f/%d=%.2f ⇒ 齿宽回读=%.2fmm%s)" % (
                    _b1_tw, _b1_bs_wrote, _b1_bs_back, _b1_dc, _b1_q, _b1_pitch, _b1_tw_back,
                    ", 已钳制" if _b1_clamped else ""), "DEBUG")

    # --- B-2: yoke_thickness 残差反驱 Slot.Hs2 ---------------------------------
    if params.get('yoke_thickness') not in (None, ''):
        try:
            _b2_yoke = float(str(params['yoke_thickness']).replace('mm', '').strip("'"))
        except Exception:
            _b2_yoke = None
        if _b2_yoke and _b2_yoke > 0:
            _b2_od = _read_mm('Stator', 'OuterDiameter')
            _b2_id = _read_mm('Stator', 'InnerDiameter')
            _b2_hs0 = _read_mm('Slot', 'Hs0')
            _b2_hs1 = _read_mm('Slot', 'Hs1')
            _b2_half = (_b2_od - _b2_id) / 2.0
            _b2_hs2_new = _b2_half - _b2_hs0 - _b2_hs1 - _b2_yoke
            _b2_hs2_wrote = _b2_hs2_new
            _b2_clamped = False
            if _b2_hs2_new < 0.5:
                log("  [B-2] 反解 Hs2=%.2fmm < 0.5mm (yoke=%.2f 侵占槽/齿空间), 钳 0.5mm 标记不可满足" % (_b2_hs2_new, _b2_yoke), "WARN")
                _b2_hs2_wrote = 0.5
                _b2_clamped = True
            content = replace_in_section(content, 'Slot', 'Hs2', "'%.2fmm'" % _b2_hs2_wrote)
            # 回读自检: grep 回 Hs2 实际值 + 复算 T_yoke
            _b2_back = _read_mm('Slot', 'Hs2')
            _b2_yoke_back = _b2_half - _b2_hs0 - _b2_hs1 - _b2_back
            log("  [B-2] yoke 目标=%.2fmm → Hs2 反驱写=%.2fmm 回读=%.2fmm "
                "(half=(%.1f-%.1f)/2=%.1f Hs0=%.2f Hs1=%.2f ⇒ T_yoke回读=%.2fmm%s)" % (
                    _b2_yoke, _b2_hs2_wrote, _b2_back, _b2_od, _b2_id, _b2_half,
                    _b2_hs0, _b2_hs1, _b2_yoke_back, ", 已钳制" if _b2_clamped else ""), "DEBUG")

    if content != original:
        with open(aedt_path, 'w', encoding='utf-8') as f:
            f.write(content)
        log(f"  .aedt text modified: {sum(1 for a, b in zip(original.splitlines(), content.splitlines()) if a != b)} lines changed")
        return True
    else:
        log("  .aedt text: no changes needed (params may be missing)")
        return False


def read_res_file(res_path):
    """★ 从 .res 纯文本文件解析 RMxprt 求解结果

    .res 文件是 AEDT RMxprt 求解后生成的纯文本结果报告,
    包含完整的额定负载、空载、堵转等运行数据。

    返回字典: {rmxprt_var_name: float_value}
    """
    import re

    try:
        with open(res_path, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        log(f"  read_res_file: cannot read {res_path}: {e}", "ERROR")
        return {}

    results = {}

    # .res 文件中的标签 → 正则模式 (提取数值部分)
    RES_PATTERNS = {
        'Efficiency':               r'Efficiency \(%\):\s*([\d.eE+-]+)',
        # v4.02.29 (#1129-① 扩展): 电机/异步用 "Mechanical Shaft Torque"; PMSG/发电机用 "Rated Torque" —— 二者取一
        'Output_Torque':            r'(?:Mechanical Shaft|Rated) Torque \(N\.m\):\s*([\d.eE+-]+)',
        # v4.02.16 (Fix5): 异步机 .res 的启动转矩 / 最大转矩真实标签
        #   此前 output_map 的 starting_torque.rmxprt_var 写 "Starting_Torque",
        #   但 .res 实际标签是 "Locked-Rotor Torque"(启动/堵转, 实测 288.312N.m)
        #   与 "Break-Down Torque"(最大/崩溃, 实测 510.846N.m) → 两个都取不到,
        #   starting_torque 恒为 0 → max_torque 回退额定输出转矩 → 客户端
        #   「最大扭矩」列与「扭矩」列完全相同。二者是不同物理量, 不可混用。
        'Breakdown_Torque':         r'Break-Down Torque \(N\.m\):\s*([\d.eE+-]+)',
        'Locked_Rotor_Torque':      r'Locked-Rotor Torque \(N\.m\):\s*([\d.eE+-]+)',
        # v2026-10-19 (P2 转矩真值集): 堵转(启动)电流倍数 —
        #   .res 标签 "Locked-Rotor Current Ratio"(如 yz200-6 实测 7.62851),
        #   即 Ist/Irated, 异步机启动电流标定关键量。新增可选字段, 不影响既有映射。
        'Locked_Rotor_Current_Ratio': r'Locked-Rotor Current Ratio:\s*([\d.eE+-]+)',
        # v4.02.19 (#1065): 输出/输入功率(Output_Power/Input_Power)已从本字典移除,
        #   改为下方 _extract_power_kw 多标签兼容解析 —— 见函数尾部注释。
        # v4.02.29 (#1129-① 扩展): 发电机 .res 同时含 "Power Factor:"(真实值, 如 0.471891)
        #   与 "Rated Power Factor:"(=1 额定回显); 用负向后查 (?<!Rated ) 跳过额定回显,
        #   否则会误取 1.0 而掩盖真实功率因数。
        'Power_Factor':             r'(?<!Rated )Power Factor:\s*([\d.eE+-]+)',
        'Stator_Tooth_Flux_Density':r'Stator-Teeth Flux Density \(Tesla\):\s*([\d.eE+-]+)',
        'Stator_Yoke_Flux_Density': r'Stator-Yoke Flux Density \(Tesla\):\s*([\d.eE+-]+)',
        'Rotor_Yoke_Flux_Density':  r'Rotor-Yoke Flux Density \(Tesla\):\s*([\d.eE+-]+)',
        # RUN86 #3 修复: 转子齿磁密 —— RMxprt .res 标签 "Rotor-Teeth Flux Density (Tesla):",
        #   旧版缺此正则 ⇒ 定子齿/轭/转子轭均有、唯独转子齿恒 null。命名与定子对称
        #   (Stator-Teeth / Rotor-Teeth)。
        'Rotor_Tooth_Flux_Density': r'Rotor-Teeth Flux Density \(Tesla\):\s*([\d.eE+-]+)',
        'Air_Gap_Flux_Density':     r'Air-Gap Flux Density \(Tesla\):\s*([\d.eE+-]+)',
        # v4.02.29 (#1129-① 扩展): 电机/异步用 "Stator Current Density"; PMSG/发电机用 "Armature Current Density"
        'Stator_Current_Density':   r'(?:Stator|Armature) Current Density \(A/mm\^2\):\s*([\d.eE+-]+)',
        'Slip':                     r'Rated Slip:\s*([\d.eE+-]+)',
        'Stator_Current':           r'Stator Phase Current \(A\):\s*([\d.eE+-]+)',
        'Iron_Loss':                r'Iron-Core Loss \(W\):\s*([\d.eE+-]+)',
        'Total_Loss':               r'Total Loss \(W\):\s*([\d.eE+-]+)',
        # v4.02.29 (#1129-① 扩展): 槽满率 —— 电机用 "Slot Fill Factor (%)", 发电机用 "Stator Slot Fill Factor (%)";
        #   用负向后查 (?<!Limited ) 跳过 "Limited Slot Fill Factor"(上限值), 取实际槽满率; 百分数 /100 归一到分数
        # S2 (RUN49-P2, 2026-09-24): 旧正则 `(?<!Limited )(?:Stator )?` 的**负向后查可被绕过** ——
        #   对 "Limited Stator Slot Fill Factor (%):  75", 匹配起点可从 "Slot Fill Factor" 起算
        #   (因 (?:Stator )? 可选), 此时前 9 字符是 "d Stator" 而非 "Limited " ⇒ 后查放行,
        #   误抓 **上限值 75** 而非真实槽满率。实测见 Scripts/_probe_sf_regex_20260924.py:
        #     PMSM 样本(Limited 行在前): 旧正则→75(错)  行首锚定→72.5297(真值)
        #     RUN49 异步机样本:          两者一致(169.2)  ⇒ 169% 非解析 bug, 是实算真值
        #   改为 **行首锚定** `^[ \t]*(?:Stator )?`: "Limited ..." 行行首是 Limited 天然排除;
        #   异步机无 "Stator " 前缀的 "Slot Fill Factor" 不受影响 (re.MULTILINE 已启用)。
        #   ([ \t]* 而非 \s*, 避免 \s 含 \n 导致跨行误匹配)
        'Slot_Fill_Rate':           r'^[ \t]*(?:Stator )?Slot Fill Factor \(%\):\s*([\d.eE+-]+)',
        # v4.02.30 (#1129-① 扩展): PMSM/发电机 .res 含 "Cogging Torque (N.m):" 标签; 合并 type_output_overrides 后
        #   cogging_torque 的 rmxprt_var="Cogging_Torque" 需此处有对应键才能从 .res 文本抽取(方法C)。
        #   (Back_EMF / Torque_Ripple 在 RMxprt .res 文本报告中通常不含, 仅 pyaedt/COM 路径经 AEDT 输出变量可取)
        'Cogging_Torque':           r'Cogging Torque \(N\.m\):\s*([\d.eE+-]+)',
    }

    for key, pattern in RES_PATTERNS.items():
        # re.MULTILINE: 使 ^ 匹配行首 (FixB 功率标签行首锚定依赖此标志)
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            try:
                results[key] = float(m.group(1))
            except ValueError:
                pass

    # v4.02.28 (#1129-①): slot_fill_rate 归一为分数 (0~1)
    #   .res 标签 "Slot Fill Factor (%)" 量纲为百分数(如 70.5 表示 70.5%),
    #   而 var_map / quality_gate / scheme_engine 全程以分数口径(0.705)流转, 故此处 /100。
    if 'Slot_Fill_Rate' in results:
        _sf_pct = float(results['Slot_Fill_Rate'])
        # S2 (RUN49-P2, 2026-09-24): 物理合理性诊断 —— 槽满率 >100% 物理不可能
        #   (铜截面积不可能超过槽面积)。RUN49 实测 169%/267%/263%, 经 .res 取证确认
        #   **是 RMxprt 实算真值** (异步机正则无误、量纲 /100 亦无误), 根因在上游
        #   几何喂参失衡 —— _apply_em_load_geometry 写入的线径/匝数 vs 槽面积不匹配,
        #   与生成期预检 _sf_est 同轮也算出 0.015~1.4989 (含 >1) 相互佐证。
        #   ⇒ **不钳位**: >100% 本身是有效故障信号 (报告第六节裁定), 钳位会掩盖真故障;
        #      此处保留真值并打点告警, 使崩溃可归因、可审计。
        if _sf_pct > 100.0:
            # RUN86 #2 修复: 物理不可能 → 显式标故障信号, 供 orchestrator/质量门/报告识别为
            #   非合法填充率(而非"偏低"误归一)。质量门已按 _sf_crash 判 critical 失败;
            #   此处额外置布尔标志, 使 TOP10/报告可显式剔除/标注该故障候选。
            results['Slot_Fill_Rate_Invalid'] = True
            log(f"槽满率物理异常: .res 实测 {_sf_pct:.2f}% > 100% (物理不可能) —— "
                f"非 .res 解析/量纲问题(已取证), 根因多为上游几何喂参失衡"
                f"(线径/匝数 vs 槽面积); 按真值保留并归一为 {_sf_pct / 100.0:.4f}",
                level="WARN")
        results['Slot_Fill_Rate'] = round(_sf_pct / 100.0, 6)

    # v4.02.19 (#1065): 输出/输入功率多标签兼容 + 单位统一为 kW
    #   .res 功率标签随电机类型/运行模式变化, 单位可能是 kW 或 W:
    #   - 异步机(电动):      "Output Power (kW):" / "Input Power (kW):"
    #   - PMSM/BLDC(电动):   "Output Power (W):"  / "Input Power (W):"   ← 单位 W!
    #   - 发电机态:          "Output Electrical Power (kW):" / "Input Mechanical Power (kW):"
    #   - 额定/需求回显:      "Rated Output Power (kW):" / "Given Output Power (kW):"
    #   旧正则仅认 "(kW)" 口径 → PMSM/BLDC 的 (W) 标签与 "Rated/Give" 前缀均不匹配
    #   → output_power 取 0 → _finalize_result 判 power<=0 为求解失效 → 聚合剔除
    #   (实测 PMSG 真实解出 15kW, 但 efficiency 能读出 95.99, 故真相是解析口径问题)。
    #   修复: 按"真实求解值优先"顺序尝试多模式, 命中后用 scale 统一换算成 kW。
    #   单位必须 kW —— _finalize_result 用 power 算额定转矩(9550·P/n), 且 efficiency=out/in,
    #   若 output 取 W 而 input 取 kW 会让效率比与转矩同时失真。
    def _extract_power_kw(pattern_scale_pairs):
        """按优先级尝试多模式, 返回首个命中值(已换算 kW), 否则 None"""
        for pat, scale in pattern_scale_pairs:
            m = re.search(pat, text, re.MULTILINE)
            if m:
                try:
                    return round(float(m.group(1)) * scale, 6)
                except ValueError:
                    continue
        return None

    # 输出功率: 真实求解值优先 (W 口径的 PMSG/BLDC > 发电态 Electrical/Mechanical
    #           > 异步 kW > 额定回显兜底)。"Given/Rated" 前缀放最后, 避免命中被求的需求回显。
    out_p = _extract_power_kw([
        (r'^\s*Output Power \(W\):\s*([\d.eE+-]+)', 0.001),                          # PMSM/BLDC 真实输出(W)
        (r'^\s*Output Electrical Power \(kW\):\s*([\d.eE+-]+)', 1.0),                # 发电态
        (r'^\s*Output Mechanical Power \(kW\):\s*([\d.eE+-]+)', 1.0),                # 发电态
        (r'^\s*Output (?:Electrical |Mechanical )?Power \(kW\):\s*([\d.eE+-]+)', 1.0),  # 异步 kW
        (r'^\s*Rated Output Power \(kW\):\s*([\d.eE+-]+)', 1.0),                     # 额定回显兜底
    ])
    if out_p is not None:
        results['Output_Power'] = out_p

    # 输入功率: 同口径
    in_p = _extract_power_kw([
        (r'^\s*Input Power \(W\):\s*([\d.eE+-]+)', 0.001),                           # PMSM/BLDC 真实输入(W)
        (r'^\s*Input Mechanical Power \(kW\):\s*([\d.eE+-]+)', 1.0),                 # 发电态
        (r'^\s*Input Electrical Power \(kW\):\s*([\d.eE+-]+)', 1.0),                 # 发电态
        (r'^\s*Input (?:Electrical |Mechanical )?Power \(kW\):\s*([\d.eE+-]+)', 1.0),   # 异步 kW
    ])
    if in_p is not None:
        results['Input_Power'] = in_p

    # 铜耗 = 定子铜耗 + 转子铜耗
    # v4.02.29 (#1129-① 扩展): 兼容两类标签口径:
    #   电机/异步: "Copper Loss of Stator/Rotor Winding (W):" (定+转子分绕组, 转子可能缺失→仅定子)
    #   PMSG/发电机: "Armature Copper Loss (W):" (电枢=定子, 无转子绕组, 该标签即总量)
    #   取「可用」铜耗并求和; 若发电机电枢铜耗与分绕组铜耗同时出现(理论不共存), 以电枢总量为准避免重复累加。
    m_scu = re.search(r'Copper Loss of Stator Winding \(W\):\s*([\d.eE+-]+)', text)
    m_rcu = re.search(r'Copper Loss of Rotor Winding \(W\):\s*([\d.eE+-]+)', text)
    m_acu = re.search(r'Armature Copper Loss \(W\):\s*([\d.eE+-]+)', text)
    _cu_parts = []
    if m_scu:
        _cu_parts.append(float(m_scu.group(1)))
    if m_rcu:
        _cu_parts.append(float(m_rcu.group(1)))
    if m_acu:
        if _cu_parts:
            # 电枢铜耗即总量, 与定/转子分绕组铜耗不共存; 避免重复累加
            _cu_parts = [float(m_acu.group(1))]
        else:
            _cu_parts.append(float(m_acu.group(1)))
    if _cu_parts:
        results['Copper_Loss'] = sum(_cu_parts)

    # 额定转速
    m_spd = re.search(r'Rated Shaft Speed \(rpm\):\s*([\d.eE+-]+)', text)
    if m_spd:
        results['Rated_Speed'] = float(m_spd.group(1))

    log(f"  read_res_file: extracted {len(results)} values from {os.path.basename(res_path)}")
    return results


def find_res_file(temp_aedt_path, design_name=TEMPLATE_DESIGN_NAME):
    """在 .aedtresults 目录中查找 .res 结果文件

    路径模式: {dir}/{basename}.aedtresults/{design}.results/*/0.res
    """
    import glob
    temp_dir = os.path.dirname(temp_aedt_path)
    temp_basename = os.path.splitext(os.path.basename(temp_aedt_path))[0]
    results_base = os.path.join(temp_dir, f"{temp_basename}.aedtresults")

    # 方式 1: 精确路径模式
    pattern = os.path.join(results_base, f"{design_name}.results", "*", "*.res")
    res_files = glob.glob(pattern)

    # 方式 2: 递归搜索所有 .res 文件
    if not res_files:
        pattern2 = os.path.join(results_base, "**", "*.res")
        res_files = [f for f in glob.glob(pattern2, recursive=True)
                     if not f.endswith('.results')]

    if res_files:
        log(f"  Found .res file: {res_files[0]}")
        return res_files[0]

    log(f"  .res file not found in {results_base}", "WARN")
    return None


def solve_via_openproject(param_list, var_map, aedt_version="2020.1", machine_type="induction"):
    """★ 主方法: OpenProject 模板 + 文本编辑参数 + Analyze("Setup1") + .res 文件读取

    完全绕开 InsertDesign 和 ChangeProperty (AEDT 2020.1 COM 限制)。
    支持 14 类电机, 根据电机类型自动选择模板。

    流程:
      0. ★ 全局清扫 *.lock 残留 (文件管理第一层)
      1. subprocess.Popen 启动 AEDT -ng (无界面, 只启动一次)
      2. win32com DispatchEx 连接
      3. 每方案:
         a. ★ 生成唯一项目名 (类型+参数哈希+时间戳+PID)
         b. 复制模板 .aedt 到临时目录 (唯一文件名)
         c. ★ 文本编辑 .aedt 文件修改电机参数 (绕开 ChangeProperty)
         d. OpenProject 打开修改后的文件
         e. Analyze("Setup1") (带超时保护)
         f. ★ 从 .res 文件读取结果 (绕开 GetOutputVariable)
         g. CloseProject 释放资源
         h. ★ 结果提取后清理项目目录 (文件管理第二层)
      4. QuitApplication 优雅退出
    """
    import win32com.client
    import shutil
    import tempfile

    # 解析电机类型
    mt = resolve_motor_type(machine_type)
    mt_info = MOTOR_TYPE_REGISTRY[mt]
    log(f"=== 方法 C: OpenProject v3 (电机类型: {mt} - {mt_info['description']}) ===")

    # ★ 文件管理第一层: 运行前全局清扫 *.lock 残留
    cleanup_lock_files()

    # ★ 根据电机类型选择模板
    template_path = mt_info["template"]
    template_design_name = mt_info["design_name"]

    # 检查模板文件
    if not os.path.exists(template_path):
        raise RuntimeError(f"RMxprt 模板文件不存在: {template_path} (电机类型: {mt})")
    log(f"模板文件: {template_path}")
    log(f"模板设计名: {template_design_name}")

    output_map = _effective_output_map(var_map, machine_type)

    # 1. 启动 AEDT -ng (无界面)
    aedt_exe = os.path.join(r"D:\Program Files\AnsysEM\AnsysEM20.1", "Win64", "ansysedt.exe")
    if not os.path.exists(aedt_exe):
        for candidate in [
            r"D:\Program Files\AnsysEM\AnsysEM20.1\Win64\ansysedt.exe",
            r"C:\Program Files\AnsysEM\AnsysEM20.1\Win64\ansysedt.exe",
        ]:
            if os.path.exists(candidate):
                aedt_exe = candidate
                break
        else:
            raise RuntimeError(f"AEDT executable not found: {aedt_exe}")

    log(f"Launching AEDT: {aedt_exe} -ng (WaitForLicense 已移除, ANSYSLMD_TIMEOUT={os.environ.get('ANSYSLMD_TIMEOUT')})")
    _write_heartbeat(0, len(param_list), "startup")
    proc = subprocess.Popen(
        [aedt_exe, "-ng"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    log(f"AEDT process started, PID={proc.pid}")
    # ★ [2026-10-10 方案 I] 启动守卫降级为 WARN (不再 raise)。
    # 本机 Ansys EDT 2020.1.0 单独 `-ng` 会立即 returncode=1 退出, 这是有意设计:
    # -ng 秒退无害, 后续 DispatchEx("Ansoft.ElectronicsDesktop") 会自拉起全新 GUI 桌面,
    # 每次求解都拿新鲜桌面 => 对环境内尸体/COM 残留免疫 (A/B 实证: -ng 在 10 尸体下 53.7s success;
    # -Iconic 复用 Popen 实例, 丧失该免疫, 同环境 OpenProject 全崩)。
    # 守卫仅记录秒退事实供审计, 绝不中断 (中断会破坏 DispatchEx 兜底路径)。
    time.sleep(3)
    rc = proc.poll()
    if rc is not None:
        log(
            f"AEDT -ng 已秒退: returncode={rc} (PID={proc.pid}) — 预期行为(旧路径), "
            f"将由 DispatchEx 自拉起桌面兜底; 若后续 COM 也失败再查许可/环境。",
            "WARN",
        )

    oDesktop = None
    results = []
    rmxprt_count = 0
    temp_files = []  # 跟踪临时文件用于清理

    try:
        # 2. 等待 AEDT COM 对象可用 (最多 90 秒)
        log("Waiting for AEDT COM object (max 90s)...")
        prog_id = f"Ansoft.ElectronicsDesktop.{aedt_version}"

        for attempt in range(90):
            try:
                oApp = win32com.client.DispatchEx(prog_id)
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via DispatchEx({prog_id}) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            try:
                oApp = win32com.client.DispatchEx("Ansoft.ElectronicsDesktop")
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via DispatchEx(Ansoft.ElectronicsDesktop) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            try:
                oApp = win32com.client.Dispatch(prog_id)
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via Dispatch({prog_id}) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            # P6a (2026-09-23): AEDT 启动层失败（秒退/ license 耗尽）⇒ 写 flag,
            #   供主进程 rmxprt_tools P6 席位监控识别（整批零真解 + flag < 300s）
            _write_aedt_launch_fail_flag()
            raise RuntimeError("AEDT COM object not available after 90s timeout")

        # 3. 批量求解
        _write_heartbeat(0, len(param_list), "solving")
        for i, params in enumerate(param_list):
            t0 = time.time()

            # ★ 生成唯一项目名 (类型+参数哈希+时间戳+PID)
            unique_name = generate_unique_project_name(machine_type, params, i)

            # ★ 复制模板到临时目录 (唯一文件名)
            temp_aedt = os.path.join(
                tempfile.gettempdir(),
                f"{unique_name}.aedt"
            )
            try:
                shutil.copy2(template_path, temp_aedt)
                temp_files.append(temp_aedt)
                log(f"[{i+1}/{len(param_list)}] Template ({mt}) copied to: {temp_aedt}")
            except Exception as e:
                log(f"[{i+1}] Copy template failed: {e}", "ERROR")
                entry = {
                    "status": "error",
                    "raw_results": {},
                    "params": params,
                    "solve_time": 0,
                    "error": f"Copy template: {e}",
                }
                results.append(entry)
                _append_progress(entry)
                continue

            # ★ 文本编辑参数 (在 OpenProject 之前!)
            log(f"[{i+1}] Modifying .aedt text parameters (type: {mt})...")
            try:
                modify_aedt_text(temp_aedt, params, machine_type=mt)
            except Exception as e:
                log(f"[{i+1}] Text modify failed: {e}", "WARN")
                # 继续, 用模板默认参数也能跑

            oProject = None
            actual_proj_name = ""

            try:
                # ★ OpenProject 打开修改后的文件 (绕开 InsertDesign!)
                log(f"[{i+1}] Opening project: {temp_aedt}")
                oProject = oDesktop.OpenProject(temp_aedt)
                if oProject is None:
                    raise RuntimeError("OpenProject returned None")

                # 获取实际项目名
                try:
                    actual_proj_name = oProject.GetName()
                except Exception:
                    actual_proj_name = os.path.splitext(os.path.basename(temp_aedt))[0]
                log(f"[{i+1}] Project opened: '{actual_proj_name}'")

                # ★ 获取设计对象 (模板自带 RMxprt 设计)
                # 注意: GetDesign() 返回的对象没有 Analyze 方法!
                #       必须用 GetActiveDesign() 或先 SetActiveDesign()
                oDesign = None
                try:
                    designs = oProject.GetTopDesignList()
                    log(f"[{i+1}] Designs in project: {designs}")
                    if designs:
                        # 先设置为活动设计
                        try:
                            oProject.SetActiveDesign(designs[0])
                            log(f"[{i+1}] SetActiveDesign: '{designs[0]}'")
                        except Exception:
                            pass  # 有些版本不需要 SetActiveDesign
                except Exception as e:
                    log(f"[{i+1}] GetTopDesignList failed: {e}", "WARN")

                # 优先使用 GetActiveDesign (有 Analyze 方法的 COM 对象)
                try:
                    oDesign = oProject.GetActiveDesign()
                except Exception as e:
                    log(f"[{i+1}] GetActiveDesign failed: {e}", "WARN")

                # 回退: 如果 GetActiveDesign 返回 None, 尝试 GetDesign
                if oDesign is None:
                    try:
                        if designs and len(designs) > 0:
                            oDesign = oProject.GetDesign(designs[0])
                    except Exception:
                        pass

                if oDesign is None:
                    raise RuntimeError("No design found in template project")

                design_name = ""
                try:
                    design_name = oDesign.GetName()
                    # design_name 可能包含 ":DesignName" 前缀
                    if ":" in design_name:
                        design_name = design_name.split(":")[-1]
                except Exception:
                    design_name = template_design_name
                log(f"[{i+1}] Design acquired: '{design_name}'")

                # ★ Analyze("Setup1") 带超时保护
                _write_heartbeat(i, len(param_list), "running_magnetostatic")
                log(f"[{i+1}] Analyzing Setup1 (timeout=120s)...")
                analyze_ok = _analyze_com_with_timeout(oDesign, timeout=120)

                if not analyze_ok:
                    raise TimeoutError("Analyze exceeded 120s timeout")

                log(f"[{i+1}] Analyze completed")

                # ★ 验证求解成功 (检查 AEDT 消息) — fix #1128-③: 显式捕获执行错误
                _exec_error = False
                _exec_msg = ""
                # ★ fix ① 诊断增强: 提取「具体」RMxprt 错误, 而非仅尾部泛化 "execution error"。
                #   msgs 为消息对象/字符串列表, 逐条归一化后:
                #     - 含 Normal completion → 求解成功确认
                #     - 含 error/aborted → 标记执行错误, 并优先抽取含具体原因的句
                #       (Error:/cannot/invalid/must be/greater than/less than/fill/overflow/
                #        failed/unable/exceed/negative/zero 等), 落入 error_detail 供迭代定位。
                #   fix #1128-③ 的诚实暴露逻辑不变, 仅让错误文本更可诊断。
                try:
                    msgs = oDesktop.GetMessages(actual_proj_name, "", 0)
                    if msgs:
                        import re as _re
                        _msg_lines = []
                        try:
                            for _m in msgs:
                                try:
                                    _s = str(_m)
                                except Exception:
                                    _s = ""
                                if _s:
                                    _msg_lines.append(_s)
                        except Exception:
                            _msg_lines = [str(msgs)]
                        _full_msg = "\n".join(_msg_lines)
                        if "Normal completion" in _full_msg:
                            log(f"[{i+1}] Solver confirmed: 'Normal completion of simulation'")
                        elif "error" in _full_msg.lower() or "aborted" in _full_msg.lower():
                            _exec_error = True
                            _specific = ""
                            for _ln in _msg_lines:
                                _low = _ln.lower()
                                if any(k in _low for k in [
                                    "error:", "cannot", "invalid", "must be", "greater than",
                                    "less than", "fill", "overflow", "failed", "unable",
                                    "exceed", "negative", "zero", "not valid", "out of range",
                                ]):
                                    _specific = _ln.strip()
                                    break
                            _exec_msg = _specific if _specific else _full_msg[-600:]
                            log(f"[{i+1}] Solver execution error detected (specific): {_exec_msg}", "ERROR")
                            log(f"[{i+1}] Full AEDT messages ({len(_msg_lines)} lines):\n{_full_msg[:4000]}", "DEBUG")
                except Exception as _e:
                    log(f"  GetMessages failed: {_e}", "WARN")

                # ★ 从 .res 文件读取结果 (必须在 CloseProject 之前!)
                _write_heartbeat(i, len(param_list), "reading_results")
                log(f"[{i+1}] Reading results from .res file...")
                res_path = find_res_file(temp_aedt, design_name)
                if res_path:
                    raw = read_res_file(res_path)
                    # ★ P0-2: 求解后回读自检 —— 实算接法 vs 设计意图接法 (错配即 √3 级磁密偏差)
                    _wc_ok, _wc_actual = _check_winding_connection(res_path, params)
                    if not _wc_ok:
                        log(f"[{i+1}] ★ 接法错配: 设计={_winding_connection_name(params.get('winding_connection'))} "
                            f"实算={_wc_actual} — 请核对 motor_tools 相电压口径", "WARN")
                    # v4.02.16 (Fix2 诊断): 导出完整 .res 文本供离线标签定位 (env RMXPRT_DUMP_RES 指定路径)
                    _dump = os.environ.get("RMXPRT_DUMP_RES")
                    if _dump and os.path.exists(res_path):
                        try:
                            with open(res_path, "r", encoding="utf-8", errors="ignore") as _rf:
                                _rtxt = _rf.read()
                            with open(_dump, "w", encoding="utf-8") as _df:
                                _df.write(_rtxt)
                            log(f"  [diag] .res 全文已导出至 {_dump}")
                        except Exception as _e:
                            log(f"  [diag] 导出 .res 失败: {_e}", "WARN")
                else:
                    # .res 缺失: 标记 raw 空 (AEDT 未产出有效结果)
                    log(f"[{i+1}] .res not found (no valid RMxprt output produced)", "WARN")
                    raw = {}

                # fix #1128-③: 诚实暴露执行错误 — .res 缺失或 AEDT 报 execution error 时,
                #   不再伪装 success(全字段填0), 直接置 status="error" 并带错误上下文,
                #   与 #1116 求解失败显式化一致; 由上层 (_convert_venv_entry) 如实传播, 不做 L0 模拟掩盖。
                _solved_ok = bool(raw)
                if _solved_ok:
                    # ★ 映射结果到 output_map 的 key (.res 值已是目标单位, 不应用 scale)
                    mapped = {}
                    for our_key, mapping in output_map.items():
                        rmxprt_var = mapping.get("rmxprt_var", our_key)
                        val = raw.get(rmxprt_var)
                        if val is not None:
                            mapped[our_key] = round(float(val), 6)
                        else:
                            mapped[our_key] = mapping.get("default", 0)
                    # RUN86 #2: 透传槽满率物理故障标志 (Slot_Fill_Rate_Invalid) 到 mapped,
                    #   使 orchestrator 的 L1 结果 _r 可据以标注/剔除 >100% 非合法填充率候选。
                    if raw.get("Slot_Fill_Rate_Invalid"):
                        mapped["slot_fill_rate_physically_invalid"] = True
                    # 方案B: 几何截图 (win32 抓屏, best-effort, fail-closed)
                    _imgs = _capture_aedt_geometry(_IMG_EXPORT_DIR, actual_proj_name, i + 1, params)
                    _res = _sample_aedt_resources()
                    solve_time = time.time() - t0
                    # v4.02.105: 补导出转子槽形几何 (UserDefHalfSlot 文本解析, 真实输入值, fail-closed)
                    try:
                        _rs = _read_rotor_slot_from_aedt(temp_aedt)
                        for _k, _v in _rs.items():
                            raw[_k] = _v
                            mapped[_k] = _v
                    except Exception:
                        pass
                    entry = {
                        "status": "success",
                        "raw_results": raw,
                        "results": mapped,
                        "params": params,
                        "solve_time": round(solve_time, 2),
                        "error": "",
                        "images": _imgs,
                        "aedt_resource": _res,
                    }
                    results.append(entry)
                    _append_progress(entry)
                    rmxprt_count += 1
                    log(f"[{i+1}] Solved OK: {len(raw)} raw, {len(mapped)} mapped, {solve_time:.1f}s")
                else:
                    solve_time = time.time() - t0
                    if _exec_error:
                        # P0-4 (#修复): error 前缀去掉 "AEDT" 字样, 语义与 _is_aedt_failure 判定解耦
                        _err = f"求解失败(参数): {_exec_msg}"
                    elif not raw:
                        _err = ".res 文件缺失 — 求解器未产出有效结果(模板不匹配/几何非法/求解崩溃)"
                    else:
                        _err = "RMxprt 未产出有效结果"
                    entry = {
                        "status": "error",
                        "raw_results": raw,
                        "results": {},
                        "params": params,
                        "solve_time": round(solve_time, 2),
                        "error": _err,
                        "error_detail": _exec_msg,
                        "images": [],
                        "aedt_resource": {},
                    }
                    results.append(entry)
                    _append_progress(entry)
                    log(f"[{i+1}] Solve FAILED (honest): {_err}", "ERROR")

            except Exception as e:
                solve_time = time.time() - t0
                err_detail = traceback.format_exc()
                _lic = "license" in (str(e).lower() + err_detail.lower())
                entry = {
                    "status": "error",
                    "raw_results": {},
                    "results": {},
                    "params": params,
                    "solve_time": round(solve_time, 2),
                    "error": str(e),
                    "error_detail": err_detail[-2000:] if len(err_detail) > 2000 else err_detail,
                    "licence_error": _lic,
                }
                results.append(entry)
                _append_progress(entry)
                if _lic:
                    log(f"[{i+1}] License 错误: {e}", "ERROR")
                else:
                    log(f"[{i+1}] Error: {e}", "ERROR")

            finally:
                # ★ 每方案后关闭项目 (带超时保护)
                # 注意: 必须在读取 .res 之后关闭!
                close_result = {"done": False}
                def _close_proj():
                    try:
                        if actual_proj_name:
                            oDesktop.CloseProject(actual_proj_name)
                            close_result["done"] = True
                        elif oProject:
                            try:
                                name = oProject.GetName()
                                oDesktop.CloseProject(name)
                                close_result["done"] = True
                            except Exception:
                                pass
                    except Exception:
                        try:
                            oProj = oDesktop.GetActiveProject()
                            if oProj:
                                oDesktop.CloseProject(oProj.GetName())
                                close_result["done"] = True
                        except Exception:
                            pass

                t_close = threading.Thread(target=_close_proj, daemon=True)
                t_close.start()
                t_close.join(timeout=15)

                if close_result["done"]:
                    log(f"[{i+1}] Project closed: '{actual_proj_name}'")
                else:
                    log(f"[{i+1}] CloseProject timed out (15s), skipping", "WARN")

                # ★ 文件管理第二层: 结果提取后清理项目目录 (释放 50-200MB/任务)
                cleaned = cleanup_project_files(temp_aedt)
                if cleaned > 0:
                    log(f"[{i+1}] Cleaned up {cleaned} project files/dirs")
                # 标记已清理, 避免在 finally 中重复清理
                if temp_aedt in temp_files:
                    temp_files.remove(temp_aedt)

    finally:
        # 4. ★ 优雅退出
        log("Quitting AEDT...")
        try:
            if oDesktop:
                oDesktop.QuitApplication()
                log("QuitApplication called")
                time.sleep(3)
        except Exception as e:
            log(f"QuitApplication warning: {e}", "WARN")
        kill_aedt_processes()

        # ★ 文件管理: 清理剩余的临时文件 (未清理的错误方案)
        for tmp_file in temp_files:
            cleaned = cleanup_project_files(tmp_file)
        if temp_files:
            log(f"Final cleanup: {len(temp_files)} remaining project dirs cleaned")

        # ★ 文件管理第三层: 再次清扫锁文件
        cleanup_lock_files()

    log(f"Method C done: {rmxprt_count}/{len(param_list)} success")

    # fix #1128-③: 全部失败时不再抛 RuntimeError 丢弃已构建的诚实错误条目,
    #   而是返回 results(含 per-candidate 详细 error/error_detail), 由上层如实传播;
    #   仅当 results 为空(循环在产出任何条目前就崩溃, 属基础设施故障)才上抛,
    #   触发 main() 的 auto 方法回退(pyaedt/com)。
    if not results:
        first_error = results[0].get("error", "unknown") if results else "no results produced"
        raise RuntimeError(f"openproject method: 0/{len(param_list)} success (no entries produced). First error: {first_error}")
    if rmxprt_count == 0:
        log(f"openproject: 0/{len(param_list)} 真实求解成功, 但已产出 {len(results)} 条诚实错误条目(不丢弃, 如实返回)", "WARN")
    _write_heartbeat(len(param_list), len(param_list), "done")
    return results


# ============================================================
# 方法 B: win32com 直接 COM (备用方法, -ng 模式)
# ============================================================

def solve_via_win32com_v3(param_list, var_map, aedt_version="2020.1", machine_type="induction"):
    """使用 win32com 直接 COM + -ng 模式 (防弹版)

    流程:
      1. subprocess.Popen 启动 AEDT -ng (无界面)
      2. win32com Dispatch 连接
      3. 每方案: NewProject + InsertDesign + ChangeProperty + Analyze + CloseProject
      4. QuitApplication 优雅退出
    """
    import win32com.client

    log("=== 方法 B: win32com 直接 COM (-ng 模式) ===")

    # AEDT 可执行文件
    aedt_install = r"D:\Program Files\AnsysEM\AnsysEM20.1"
    aedt_exe = os.path.join(aedt_install, "Win64", "ansysedt.exe")
    if not os.path.exists(aedt_exe):
        for candidate in [
            r"D:\Program Files\AnsysEM\AnsysEM20.1\Win64\ansysedt.exe",
            r"C:\Program Files\AnsysEM\AnsysEM20.1\Win64\ansysedt.exe",
        ]:
            if os.path.exists(candidate):
                aedt_exe = candidate
                break
        else:
            raise RuntimeError(f"AEDT executable not found: {aedt_exe}")

    machine_type_str = get_machine_type_str(machine_type)
    input_map = var_map.get("input_map", {})
    output_map = _effective_output_map(var_map, machine_type)

    # 1. ★ 启动 AEDT -ng (秒退无害, DispatchEx 自拉起兜底; 见 solve_via_openproject 同款注释)
    log(f"Launching AEDT: {aedt_exe} -ng -WaitForLicense")
    proc = subprocess.Popen(
        [aedt_exe, "-ng", "-WaitForLicense"],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    log(f"AEDT process started, PID={proc.pid}")
    # ★ [2026-10-10 方案 I] 启动守卫降级为 WARN (同 site 1, 不 raise)。
    time.sleep(3)
    rc = proc.poll()
    if rc is not None:
        log(
            f"AEDT -ng 已秒退: returncode={rc} (PID={proc.pid}) — 预期行为(旧路径), "
            f"将由 DispatchEx 自拉起桌面兜底; 若后续 COM 也失败再查许可/环境。",
            "WARN",
        )

    oDesktop = None
    results = []
    rmxprt_count = 0

    try:
        # 2. 等待 AEDT COM 对象可用 (最多 90 秒)
        log("Waiting for AEDT COM object (max 90s)...")
        prog_id = "Ansoft.ElectronicsDesktop"
        version_prog_id = f"Ansoft.ElectronicsDesktop.{aedt_version}"

        oApp = None
        for attempt in range(90):
            try:
                # ★ 使用 DispatchEx 强制创建新实例, 避免连接到僵尸进程
                oApp = win32com.client.DispatchEx(version_prog_id)
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via DispatchEx({version_prog_id}) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            try:
                oApp = win32com.client.DispatchEx(prog_id)
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via DispatchEx({prog_id}) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            # 也尝试普通 Dispatch 作为回退
            try:
                oApp = win32com.client.Dispatch(version_prog_id)
                oDesktop = oApp.GetAppDesktop()
                if oDesktop:
                    log(f"AEDT COM connected via Dispatch({version_prog_id}) (attempt {attempt+1})")
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            # P6a (2026-09-23): AEDT 启动层失败（秒退/ license 耗尽）⇒ 写 flag,
            #   供主进程 rmxprt_tools P6 席位监控识别（整批零真解 + flag < 300s）
            _write_aedt_launch_fail_flag()
            raise RuntimeError("AEDT COM object not available after 90s timeout")

        # 3. 批量求解
        for i, params in enumerate(param_list):
            t0 = time.time()
            ts = datetime.datetime.now().strftime("%H%M%S")
            _write_heartbeat(i, len(param_list), "solving")

            # ★ 唯一项目名
            proj_name = f"COM_Motor_{i+1}_{ts}"
            design_name = f"D_{i+1}_{ts}"

            log(f"[{i+1}/{len(param_list)}] Creating project '{proj_name}'...")

            try:
                # ★ NewProject
                oProject = oDesktop.NewProject()
                # SetName 在 AEDT 2020.1 COM 上可能不存在, 跳过
                actual_proj_name = proj_name
                try:
                    oProject.SetName(proj_name)
                    log(f"[{i+1}] Project name set: {proj_name}")
                except Exception:
                    # SetName 不可用, 获取自动生成的项目名
                    try:
                        actual_proj_name = oProject.GetName()
                    except Exception:
                        actual_proj_name = ""
                    log(f"[{i+1}] SetName skipped, actual name: '{actual_proj_name}'")
                log(f"[{i+1}] Project created: '{actual_proj_name}'")

                # ★ InsertDesign — 尝试多种签名
                oDesign = None
                insert_errors = []

                for sign_desc, args in [
                    (f'InsertDesign("RMxprt", "{design_name}", "{machine_type_str}", "")',
                     ("RMxprt", design_name, machine_type_str, "")),
                    # ★ 用户参考方案: Maxwell 设计 + RMxprt 方案类型
                    (f'InsertDesign("Maxwell", "{design_name}", "RMxprt", "")',
                     ("Maxwell", design_name, "RMxprt", "")),
                    (f'InsertDesign("RMxprt", "{design_name}", "", "")',
                     ("RMxprt", design_name, "", "")),
                ]:
                    try:
                        log(f"[{i+1}] Trying: {sign_desc}")
                        # ★ InsertDesign 带超时 (30s), 防止挂起
                        insert_result = {"done": False, "error": None, "design": None}
                        def _do_insert():
                            try:
                                oProject.InsertDesign(*args)
                                insert_result["done"] = True
                            except Exception as ie:
                                insert_result["error"] = ie
                                insert_result["done"] = True

                        t_ins = threading.Thread(target=_do_insert, daemon=True)
                        t_ins.start()
                        t_ins.join(timeout=30)

                        if not insert_result["done"]:
                            log(f"[{i+1}] InsertDesign timed out (30s)", "WARN")
                            insert_errors.append(f"{sign_desc}: TIMEOUT(30s)")
                            continue

                        if insert_result["error"]:
                            raise insert_result["error"]

                        oDesign = oProject.GetActiveDesign()
                        if oDesign:
                            log(f"[{i+1}] InsertDesign succeeded")
                            break
                    except Exception as e:
                        err_str = str(e)
                        insert_errors.append(f"{sign_desc}: {err_str}")
                        log(f"[{i+1}] InsertDesign failed: {err_str}", "WARN")
                        # 检查设计是否实际已创建
                        try:
                            designs = oProject.GetTopDesignList()
                            if designs:
                                oDesign = oProject.GetDesign(designs[-1])
                                if oDesign:
                                    log(f"[{i+1}] Design found despite error: {designs[-1]}")
                                    break
                        except Exception:
                            pass
                        time.sleep(0.5)

                if oDesign is None:
                    raise RuntimeError(
                        f"All InsertDesign attempts failed:\n" + "\n".join(insert_errors)
                    )

                # 设置参数
                _write_heartbeat(i, len(param_list), "setting_variables")
                _set_params_com(oDesign, params, input_map, machine_type_str)
                log(f"[{i+1}] Parameters set")

                # ★ Analyze 带超时
                _write_heartbeat(i, len(param_list), "running_magnetostatic")
                log(f"[{i+1}] Analyzing (timeout=120s)...")
                analyze_ok = _analyze_com_with_timeout(oDesign, timeout=120)

                if not analyze_ok:
                    raise TimeoutError("Analyze exceeded 120s timeout")

                # 读取结果
                _write_heartbeat(i, len(param_list), "reading_results")
                raw = _read_results_com(oDesign, output_map)
                _imgs = _capture_aedt_geometry(_IMG_EXPORT_DIR, proj_name, i + 1, params)
                _res = _sample_aedt_resources()
                solve_time = time.time() - t0

                results.append({
                    "status": "success",
                    "raw_results": raw,
                    "params": params,
                    "solve_time": round(solve_time, 2),
                    "error": "",
                    "images": _imgs,
                    "aedt_resource": _res,
                })
                rmxprt_count += 1
                log(f"[{i+1}] Solved OK: {len(raw)} results, {solve_time:.1f}s")

            except Exception as e:
                solve_time = time.time() - t0
                err_detail = traceback.format_exc()
                _lic = "license" in (str(e).lower() + err_detail.lower())
                results.append({
                    "status": "error",
                    "raw_results": {},
                    "params": params,
                    "solve_time": round(solve_time, 2),
                    "error": str(e),
                    "error_detail": err_detail[-2000:] if len(err_detail) > 2000 else err_detail,
                    "licence_error": _lic,
                })
                if _lic:
                    log(f"[{i+1}] License 错误: {e}", "ERROR")
                else:
                    log(f"[{i+1}] Error: {e}", "ERROR")

            finally:
                # ★ 每方案后关闭项目 (释放资源!) — 带超时保护
                # 使用 actual_proj_name (可能与 proj_name 不同, 如果 SetName 跳过了)
                close_name = actual_proj_name if 'actual_proj_name' in dir() else proj_name

                def _do_close_project():
                    """在子线程中关闭项目, 防止挂起"""
                    try:
                        oDesktop.CloseProject(close_name)
                        return True
                    except Exception:
                        try:
                            oProj = oDesktop.GetActiveProject()
                            if oProj:
                                oDesktop.CloseProject(oProj.GetName())
                                return True
                        except Exception:
                            pass
                    return False

                close_result = {"done": False}
                def _close_thread():
                    close_result["done"] = _do_close_project()

                t_close = threading.Thread(target=_close_thread, daemon=True)
                t_close.start()
                t_close.join(timeout=15)  # CloseProject 最多等 15 秒

                if close_result["done"]:
                    log(f"[{i+1}] Project closed: '{close_name}'")
                else:
                    log(f"[{i+1}] CloseProject timed out or failed (15s), skipping", "WARN")

    finally:
        # 4. ★ 优雅退出
        log("Quitting AEDT...")
        try:
            if oDesktop:
                oDesktop.QuitApplication()
                log("QuitApplication called")
                time.sleep(3)
        except Exception as e:
            log(f"QuitApplication warning: {e}", "WARN")
        # 确保进程终止
        kill_aedt_processes()

    log(f"Method B done: {rmxprt_count}/{len(param_list)} success")

    # 如果全部失败, 抛出异常让调用者知道
    if rmxprt_count == 0 and len(param_list) > 0:
        first_error = results[0].get("error", "unknown") if results else "no results"
        raise RuntimeError(f"win32com method: 0/{len(param_list)} success. First error: {first_error}")

    return results


def _set_params_com(oDesign, params, input_map, machine_type_str):
    """通过 ChangeProperty 设置参数 (win32com)"""
    # 设置电机类型
    try:
        oDesign.ChangeProperty([
            "NAME:AllTabs",
            ["NAME:Machine", [
                "NAME:PropServers", "Machine:Machine"
            ], [
                "NAME:ChangedProps",
                ["NAME:Machine Type", "Value:=", machine_type_str]
            ]]
        ])
    except Exception:
        pass

    # 按类别分组
    general_props = []
    machine_props = []
    stator_props = []
    rotor_props = []

    for param_key, mapping in input_map.items():
        rmxprt_var = mapping.get("rmxprt_var", param_key)
        value = params.get(param_key)
        if value is None:
            default = mapping.get("default")
            if default is not None:
                value = default
            else:
                continue

        auto_compute = mapping.get("auto_compute")
        if auto_compute:
            try:
                expr = auto_compute
                for k, v in params.items():
                    expr = expr.replace(k, str(v))
                value = round(float(eval(expr)), 1)
            except Exception:
                pass

        if isinstance(value, (int, float)):
            var_lower = rmxprt_var.lower()
            if any(x in var_lower for x in ["diameter", "width", "length", "height", "gap", "tooth", "yoke", "shaft"]):
                value_str = f"{value}mm"
            elif "voltage" in var_lower:
                value_str = f"{value}V"
            elif "speed" in var_lower:
                value_str = f"{value}rpm"
            elif "power" in var_lower:
                value_str = f"{value}kW"
            elif "frequency" in var_lower:
                value_str = f"{value}Hz"
            else:
                value_str = str(value)
        else:
            value_str = str(value)

        prop_entry = [f"NAME:{rmxprt_var}", "Value:=", value_str]
        var_lower = rmxprt_var.lower()

        if any(x in var_lower for x in ["pole", "power", "voltage", "speed", "frequency", "current", "branch"]):
            general_props.append(prop_entry)
        elif "stator" in var_lower or "slot" in var_lower and "rotor" not in var_lower:
            stator_props.append(prop_entry)
        elif "rotor" in var_lower:
            rotor_props.append(prop_entry)
        else:
            machine_props.append(prop_entry)

    # 批量设置
    tabs = ["NAME:AllTabs"]
    if general_props:
        tabs.append(["NAME:General", ["NAME:PropServers", "Machine:General"], ["NAME:ChangedProps", *general_props]])
    if machine_props:
        tabs.append(["NAME:Machine", ["NAME:PropServers", "Machine:Machine"], ["NAME:ChangedProps", *machine_props]])
    if stator_props:
        tabs.append(["NAME:Stator", ["NAME:PropServers", "Machine:Stator"], ["NAME:ChangedProps", *stator_props]])
    if rotor_props:
        tabs.append(["NAME:Rotor", ["NAME:PropServers", "Machine:Rotor"], ["NAME:ChangedProps", *rotor_props]])

    try:
        if len(tabs) > 1:
            oDesign.ChangeProperty(tabs)
    except Exception as e:
        log(f"ChangeProperty batch failed: {e}, trying individual sets", "WARN")
        for prop_list in [general_props, machine_props, stator_props, rotor_props]:
            for prop in prop_list:
                try:
                    var_name = prop[0].replace("NAME:", "")
                    oDesign.SetVariableValue(var_name, prop[2])
                except Exception:
                    pass

    # ★ P0-2: 显式注入绕组接法 —— Setup1 级属性 (非 Machine 变量, 故不走 input_map)。
    #   0 = Y (Wye/Star) / 1 = Delta, 由 yz200-6 模板值与 .res "Winding Connection: Wye" 对照实证。
    try:
        _wc = _winding_connection_code(params.get('winding_connection'))
        oDesign.ChangeProperty([
            "NAME:AllTabs",
            ["NAME:AnalysisSetup", ["NAME:PropServers", "AnalysisSetup:Setup1"],
             ["NAME:ChangedProps", ["NAME:WindingConnection", "Value:=", _wc]]]
        ])
        log(f"  P0-2 接法注入(COM): WindingConnection={_wc} "
            f"({_winding_connection_name(params.get('winding_connection'))})")
    except Exception as _e:
        log(f"  P0-2 接法注入(COM)失败(退回模板默认): {_e}", "WARN")


def _read_results_com(oDesign, output_map):
    """读取 RMxprt 求解结果 (win32com)"""
    raw = {}
    for our_key, mapping in output_map.items():
        rmxprt_var = mapping.get("rmxprt_var", our_key)
        try:
            val = oDesign.GetOutputVariable(rmxprt_var)
            if val is not None:
                raw[rmxprt_var] = float(val)
        except Exception:
            try:
                val = oDesign.GetDesignVariableValue(rmxprt_var) if hasattr(oDesign, 'GetDesignVariableValue') else None
                if val is not None:
                    raw[rmxprt_var] = float(val)
            except Exception:
                pass
    # v4.02.105: 补导出转子槽形几何
    try:
        raw.update(_read_rotor_slot_com(oDesign))
    except Exception:
        pass
    return raw


def _analyze_com_with_timeout(oDesign, setup_name="Setup1", timeout=120):
    """带超时保护的 COM Analyze

    Args:
        oDesign: COM Design 对象
        setup_name: Setup 名称 (AEDT 2020.1 必须传入, 否则报参数数目错误)
        timeout: 超时秒数

    注意: COM 对象是 STA (Single-Threaded Apartment), 必须在创建线程调用。
    子线程调用会报 "GetActiveDesign.Analyze" 之类的 COM 错误。
    因此这里优先在主线程直接调用, 仅在超时时用子线程监控。
    """
    # ★ 方式 1: 主线程直接调用 (COM STA 兼容)
    log(f"    Analyze: direct call on main thread (setup='{setup_name}')")
    try:
        oDesign.Analyze(setup_name)
        log(f"    Analyze: completed on main thread")
        return True
    except Exception as e:
        err_str = str(e)
        log(f"    Analyze: direct call failed: {err_str}", "WARN")

        # 如果是超时类错误, 不再重试
        if "timeout" in err_str.lower() or "timed out" in err_str.lower():
            raise

    # ★ 方式 2: 子线程调用 (备选, 可能因 COM STA 失败)
    log(f"    Analyze: fallback to threaded call (timeout={timeout}s)")
    result = {"done": False, "error": None}

    def _do_analyze():
        try:
            oDesign.Analyze(setup_name)
            result["done"] = True
        except Exception as e:
            result["error"] = e
            result["done"] = True

    t = threading.Thread(target=_do_analyze, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if result["error"]:
        raise result["error"]
    return result["done"]


# ============================================================
# 主入口
# ============================================================

# ── v4.02.06: 心跳 + 增量进度 (B/C) + 许可超时 (G) ───────────────
_HEARTBEAT_PATH = ""
_PROGRESS_PATH = ""


def _write_heartbeat(index, total, phase):
    global _HEARTBEAT_PATH
    if not _HEARTBEAT_PATH:
        return
    try:
        with open(_HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "index": index, "total": total, "phase": phase}, f)
    except Exception:
        pass


def _append_progress(entry):
    global _PROGRESS_PATH
    if not _PROGRESS_PATH:
        return
    try:
        existing = []
        if os.path.exists(_PROGRESS_PATH):
            try:
                with open(_PROGRESS_PATH, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        existing.append(entry)
        tmp = _PROGRESS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
        os.replace(tmp, _PROGRESS_PATH)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="RMxprt venv worker v3 (Python 3.10)")
    parser.add_argument("--params", required=True, help="Input params JSON path")
    parser.add_argument("--output", required=True, help="Output results JSON path")
    parser.add_argument("--var-map", required=True, help="Variable map JSON path")
    parser.add_argument("--aedt-version", default="2020.1", help="AEDT version")
    parser.add_argument("--machine-type", default="induction",
                        help="Machine type (induction/pmsm/bldc/srm/synrm/wound_sync/sync_generator/lssm/universal/claw_pole/afpm/flux_switching/induction_single_phase)")
    parser.add_argument("--method", default="auto", choices=["auto", "openproject", "pyaedt", "com"],
                        help="Solving method: auto (openproject→pyaedt→com), openproject, pyaedt, com")
    parser.add_argument("--heartbeat", default="", help="Heartbeat JSON path for monitor")
    parser.add_argument("--progress", default="", help="Incremental progress JSON path for monitor")
    parser.add_argument("--preflight", action="store_true", help="Preflight probe mode")
    parser.add_argument("--no-diff-kill", action="store_true",
                        help="并行分片模式: AEDT 清理仅回收孤儿, 跳过本批差集(避免误杀兄弟分片)")
    args = parser.parse_args()

    global _HEARTBEAT_PATH, _PROGRESS_PATH, _NO_DIFF_KILL
    _HEARTBEAT_PATH = args.heartbeat or ""
    _PROGRESS_PATH = args.progress or ""
    _NO_DIFF_KILL = bool(getattr(args, "no_diff_kill", False))
    # G: 许可超时, 避免 -WaitForLicense 无限等待 (v4.02.06)
    os.environ["ANSYSLMD_TIMEOUT"] = os.environ.get("ANSYSLMD_TIMEOUT", "30")

    # 解析电机类型
    resolved_type = resolve_motor_type(args.machine_type)
    mt_info = MOTOR_TYPE_REGISTRY[resolved_type]

    log(f"Starting RMxprt venv worker v3")
    log(f"  params: {args.params}")
    log(f"  output: {args.output}")
    log(f"  aedt-version: {args.aedt_version}")
    log(f"  machine-type: {args.machine_type} -> {resolved_type} ({mt_info['description']})")
    log(f"  template: {mt_info['template']}")
    log(f"  method: {args.method}")

    # 加载输入
    param_list = load_json(args.params)
    var_map = load_json(args.var_map)

    if not isinstance(param_list, list):
        param_list = [param_list]

    log(f"Loaded {len(param_list)} param sets")

    # v4.02.34: 启动即写心跳 — 覆盖"启动 AEDT / 等待 COM 对象"阶段 (此前心跳写在
    # solve_via_openproject 内部 AEDT 启动+COM 等待之后, 导致该阶段心跳文件为空,
    # 主进程 STARTUP_WATCHDOG 读不到时间戳而失效, 单个重试 worker 可卡等 ~90s 不被杀)。
    _write_heartbeat(0, len(param_list), "startup")

    results = []
    t_total = time.time()
    error_msg = ""
    method_used = "unknown"
    pyaedt_ver = "N/A"

    # 清理残留进程
    kill_aedt_processes()

    try:
        if args.method in ("auto", "openproject"):
            # ★ 方法 C: OpenProject 模板 (主方法 — 绕开 InsertDesign)
            method_used = "openproject"
            results = solve_via_openproject(param_list, var_map, args.aedt_version, args.machine_type)

        elif args.method == "pyaedt":
            # 方法 A: pyaedt Desktop + Rmxprt
            method_used = "pyaedt"
            import pyaedt
            pyaedt_ver = pyaedt.__version__
            results = solve_via_pyaedt_v3(param_list, var_map, args.aedt_version, args.machine_type)

        elif args.method == "com":
            # 方法 B: win32com 直接 COM
            method_used = "win32com"
            results = solve_via_win32com_v3(param_list, var_map, args.aedt_version, args.machine_type)

    except Exception as e:
        error_msg = str(e)
        err_detail = traceback.format_exc()
        log(f"FATAL ({method_used}): {e}", "ERROR")
        log(err_detail, "ERROR")

        # auto 模式: openproject 失败 → pyaedt → win32com
        if args.method == "auto" and method_used == "openproject":
            log("OpenProject failed, trying pyaedt fallback...", "WARN")
            kill_aedt_processes()
            time.sleep(2)
            try:
                method_used = "pyaedt"
                import pyaedt
                pyaedt_ver = pyaedt.__version__
                results = solve_via_pyaedt_v3(param_list, var_map, args.aedt_version, args.machine_type)
                error_msg = ""
            except Exception as e2:
                log(f"pyaedt also failed: {e2}", "WARN")
                kill_aedt_processes()
                time.sleep(2)
                try:
                    method_used = "win32com"
                    results = solve_via_win32com_v3(param_list, var_map, args.aedt_version, args.machine_type)
                    error_msg = ""
                except Exception as e3:
                    error_msg = f"openproject: {e} | pyaedt: {e2} | win32com: {e3}"
                    log(f"All methods failed: {e3}", "ERROR")
                    for params in param_list[len(results):]:
                        results.append({
                            "status": "error",
                            "raw_results": {},
                            "params": params,
                            "solve_time": 0,
                            "error": error_msg,
                        })
        elif args.method == "auto" and method_used == "pyaedt":
            log("pyaedt failed, trying win32com fallback...", "WARN")
            kill_aedt_processes()
            time.sleep(2)
            try:
                method_used = "win32com"
                results = solve_via_win32com_v3(param_list, var_map, args.aedt_version, args.machine_type)
                error_msg = ""
            except Exception as e2:
                error_msg = f"pyaedt: {e} | win32com: {e2}"
                log(f"win32com also failed: {e2}", "ERROR")
                for params in param_list[len(results):]:
                    results.append({
                        "status": "error",
                        "raw_results": {},
                        "params": params,
                        "solve_time": 0,
                        "error": error_msg,
                    })
        else:
            for params in param_list[len(results):]:
                results.append({
                    "status": "error",
                    "raw_results": {},
                    "params": params,
                    "solve_time": 0,
                    "error": error_msg,
                })

    finally:
        # 最终清理
        kill_aedt_processes()

    total_time = time.time() - t_total

    output = {
        "status": "error" if error_msg else "ok",
        "method": method_used,
        "pyaedt_version": pyaedt_ver,
        "total_time": round(total_time, 2),
        "results": results,
        "error": error_msg,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    success_count = sum(1 for r in results if r.get("status") == "success")
    log(f"Done: {success_count}/{len(results)} success, {total_time:.1f}s, method={method_used}, status={output['status']}")


if __name__ == "__main__":
    try:
        main()
    finally:
        # ★ COM 线程清理
        try:
            import pythoncom
            pythoncom.CoUninitialize()
            log("COM uninitialized (CoUninitialize)")
        except Exception:
            pass
