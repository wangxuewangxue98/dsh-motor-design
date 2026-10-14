# -*- coding: utf-8 -*-
"""
_sync_dsh_registry_20261015c.py — 台账三次同步（full rc.14 → rc.15：L2 sanity 门损耗判据）
====================================================================================
背景：rc.15 修 L2 sanity 门缺陷 —— scripts/motorcad_bridge.py 的 _sanity_gate
原先只有「计算效率<10%」/「InputPower>额定×10」两条判据；EMag 求解空转时功率同样
读回 0 ⇒ 0/0 算不出效率、InputPower 也不超阈 ⇒ 两条双双失效、垃圾值被静默采信。
rc.15 新增判据 3「关键损耗字段全 0」（CORE_LOSS_VARS + LOSS_ZERO_EPS=1e-9）。

本脚本只把 full 行 rc.14 → rc.15（l0 行不动，仍是 0.5.2），并重跑 G1-G4
（走 admin_dsh._run_gate 服务端同款路径）。

步骤：0 备份 → 1 full 版本+dist_tag → 2 重跑双侧门禁（l0 也顺带复验）→ 3 终态断言
用法：Scripts 下运行（python 需能 import Core/app）
"""
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "dsh_plugin_registry.db"
BACKUP_DIR = REPO / "data" / "backups"

# 门禁子进程需要 node：确保 PATH 里有 managed node
MANAGED_NODE = r"C:\Users\15389\.workbuddy\binaries\node\versions\24.19.0"
if MANAGED_NODE not in os.environ.get("PATH", ""):
    os.environ["PATH"] = MANAGED_NODE + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, str(REPO / "Core"))
sys.path.insert(0, str(REPO / "app"))

import dsh_registry as reg  # noqa: E402

L0_ID = "dsh-motor-ai-l0"
FULL_ID = "dsh-motor-ai"
L0_KEEP_VERSION = "0.5.2"      # 本轮不动，仅终态断言校验
FULL_NEW_VERSION = "1.0.0-rc.15"


def hr(title):
    print("=" * 66)
    print(title)
    print("=" * 66)


def main():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    hr("[0] 备份台账 DB")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"dsh_plugin_registry-{ts}.db"
    shutil.copy2(DB, backup)
    print(f"  已备份: {backup}  ({backup.stat().st_size:,} bytes)")

    hr(f"[1] {FULL_ID}: version → {FULL_NEW_VERSION}")
    cur = reg.get_plugin(FULL_ID)
    print(f"  现 version={cur['version']}  npm={cur['distribution'].get('npm')}")
    reg.update_plugin_fields(FULL_ID, {"version": FULL_NEW_VERSION})
    dist = dict(cur.get("distribution") or {})
    npm_block = dict(dist.get("npm") or {})
    npm_block.update({"package": "dsh-motor-ai", "dist_tag": "latest"})
    dist["npm"] = npm_block
    reg.update_plugin_fields(FULL_ID, {"distribution": dist})
    after = reg.get_plugin(FULL_ID)
    print(f"  新 version={after['version']}  npm={after['distribution'].get('npm')}")

    hr("[2] 重跑发布门禁（G1-G4，admin_dsh._run_gate）")
    import admin_dsh
    gate_ok = {}
    for pid in (L0_ID, FULL_ID):
        t0 = time.time()
        result = admin_dsh._run_gate(pid)
        ok = result.get("triple_ok")
        gate_ok[pid] = ok
        print(f"  {pid}: triple_ok={ok}  ({time.time() - t0:.0f}s)")
        print(f"    summary: {result.get('summary')}")
        for k, g in sorted((result.get("gates") or {}).items()):
            print(f"    {k.upper()}: {'OK' if g.get('ok') else 'FAIL'} — {g.get('detail')}")

    hr("[3] 同步后台账终态")
    plugins = reg.list_plugins()
    print(f"  插件行数: {len(plugins)}")
    for p in plugins:
        gate = p.get("release_gate") or {}
        npm = (p.get("distribution") or {}).get("npm") or {}
        print(f"  - {p['plugin_id']:<18} ch={p['channel']:<3} ver={p['version']:<14} "
              f"status={p['status']:<10} gate={'OK' if gate.get('triple_ok') else 'FAIL'} "
              f"({gate.get('ts', '')[:19]}) npm={npm}")
    ids = sorted(p["plugin_id"] for p in plugins)
    vers = {p["plugin_id"]: p["version"] for p in plugins}
    want = {L0_ID: L0_KEEP_VERSION, FULL_ID: FULL_NEW_VERSION}
    ok_ids = ids == sorted(want)
    ok_ver = all(vers.get(k) == v for k, v in want.items())
    ok_gate = all(gate_ok.values())
    print()
    print(f"  判定: {'OK' if (ok_ids and ok_ver and ok_gate) else 'FAIL'} "
          f"(行数={ok_ids} 版本={ok_ver} 门禁={ok_gate})")
    print(f"  备份保留于: {backup}")
    return 0 if (ok_ids and ok_ver and ok_gate) else 1


if __name__ == "__main__":
    sys.exit(main())
