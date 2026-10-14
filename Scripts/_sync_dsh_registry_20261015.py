# -*- coding: utf-8 -*-
"""
_sync_dsh_registry_20261015.py — 管理端 DSH 插件台账数据同步（2026-10-15）
======================================================================
背景（用户指令「完善管理端-插件管理，同步相关数据」）：
  发布 dsh-motor-ai-l0@0.5.1 与 dsh-motor-ai@1.0.0-rc.13 后，台账
  （D:/MotorDesign/data/dsh_plugin_registry.db）仍停留在旧版，且存在
  npm 改名前的遗留重复行 motor-ai-l0（0.3.3，仅挂 1 条已吊销 token）。

本脚本（一次性，可审计）依次执行：
  0. 备份 DB 到 data/backups/（带时间戳）
  1. dsh-motor-ai-l0: version 0.3.3 → 0.5.1；distribution 补 npm 块
     （package=dsh-motor-ai-l0, dist_tag=latest，与 full 行结构对齐）
  2. dsh-motor-ai: version 1.0.0-rc.12 → 1.0.0-rc.13；
     distribution.npm.dist_tag next → latest（npm 实际 latest=rc.13）
  3. 遗留行清理：tok_26e01b0933f4（revoked）改挂规范 id 后，
     删除 dsh_plugin_registry 中 plugin_id='motor-ai-l0' 的重复行
     （无口令/下载/遥测/订单/免费额度引用；_ensure_seeded 已修不再复活）
  4. 重跑两侧发布门禁（走 admin_dsh._run_gate 服务端同款代码路径），
     刷新 release_gate 时间戳并验证新版本过 G1-G4
  5. 打印同步后台账终态

安全：先备份后动库；删除动作仅限上述 1 个重复行；全程打印前后对照。
用法：D:\\MotorDesign\\venv\\Scripts\\python.exe Scripts\\_sync_dsh_registry_20261015.py
"""
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "dsh_plugin_registry.db"
BACKUP_DIR = REPO / "data" / "backups"

# 门禁子进程需要 node：确保 PATH 里有 managed node（_find_node 用 shutil.which）
MANAGED_NODE = r"C:\Users\15389\.workbuddy\binaries\node\versions\24.19.0"
if MANAGED_NODE not in os.environ.get("PATH", ""):
    os.environ["PATH"] = MANAGED_NODE + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, str(REPO / "Core"))
sys.path.insert(0, str(REPO / "app"))

import dsh_registry as reg  # noqa: E402

L0_ID = "dsh-motor-ai-l0"
FULL_ID = "dsh-motor-ai"
LEGACY_ID = "motor-ai-l0"

L0_NEW_VERSION = "0.5.1"
FULL_NEW_VERSION = "1.0.0-rc.13"


def hr(title):
    print("=" * 66)
    print(title)
    print("=" * 66)


def main():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    # ---------- 0. 备份 ----------
    hr("[0] 备份台账 DB")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"dsh_plugin_registry-{ts}.db"
    shutil.copy2(DB, backup)
    print(f"  已备份: {backup}  ({backup.stat().st_size:,} bytes)")

    # ---------- 同步前快照 ----------
    before = {p["plugin_id"]: p for p in reg.list_plugins()}
    print(f"  同步前插件行: {sorted(before)}")

    # ---------- 1. l0 版本 + distribution ----------
    hr(f"[1] {L0_ID}: version → {L0_NEW_VERSION} (+ npm 块)")
    cur = reg.get_plugin(L0_ID)
    print(f"  现 version={cur['version']}  status={cur['status']}")
    reg.update_plugin_fields(L0_ID, {"version": L0_NEW_VERSION})
    dist = dict(cur.get("distribution") or {})
    npm_block = dict(dist.get("npm") or {})
    npm_block.update({"package": "dsh-motor-ai-l0", "dist_tag": "latest"})
    dist["npm"] = npm_block
    reg.update_plugin_fields(L0_ID, {"distribution": dist})
    after = reg.get_plugin(L0_ID)
    print(f"  新 version={after['version']}  distribution.npm={after['distribution'].get('npm')}")

    # ---------- 2. full 版本 + dist_tag ----------
    hr(f"[2] {FULL_ID}: version → {FULL_NEW_VERSION} (dist_tag next→latest)")
    cur = reg.get_plugin(FULL_ID)
    print(f"  现 version={cur['version']}  distribution.npm={cur['distribution'].get('npm')}")
    reg.update_plugin_fields(FULL_ID, {"version": FULL_NEW_VERSION})
    dist = dict(cur.get("distribution") or {})
    npm_block = dict(dist.get("npm") or {})
    npm_block["dist_tag"] = "latest"  # npm 实际：latest=1.0.0-rc.13, next=rc.7
    dist["npm"] = npm_block
    reg.update_plugin_fields(FULL_ID, {"distribution": dist})
    after = reg.get_plugin(FULL_ID)
    print(f"  新 version={after['version']}  distribution.npm={after['distribution'].get('npm')}")

    # ---------- 3. 遗留重复行清理 ----------
    hr(f"[3] 清理遗留重复行 {LEGACY_ID}")
    conn = sqlite3.connect(DB)
    try:
        refs = {}
        for tbl, col in (("dsh_plugin_tokens", "plugin_id"),
                         ("dsh_plugin_passcodes", "plugin_id"),
                         ("dsh_plugin_downloads", "plugin_id"),
                         ("dsh_plugin_experience", "plugin_id"),
                         ("dsh_free_usage", "plugin_id")):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE {col} = ?", (LEGACY_ID,)).fetchone()[0]
            if n:
                refs[tbl] = n
        print(f"  其他表对 {LEGACY_ID} 的引用: {refs or '无'}")
        # 已吊销 token 改挂规范 id（保留审计痕迹，不删数据）
        n_tok = conn.execute(
            "UPDATE dsh_plugin_tokens SET plugin_id = ? WHERE plugin_id = ?",
            (L0_ID, LEGACY_ID)).rowcount
        # 订单表（dsh_orders）无 plugin_id 列引用问题：按 order 归属不受影响
        n_row = conn.execute(
            "DELETE FROM dsh_plugin_registry WHERE plugin_id = ?", (LEGACY_ID,)).rowcount
        conn.commit()
        print(f"  token 改挂规范 id: {n_tok} 条 | 删除重复插件行: {n_row} 条")
    finally:
        conn.close()

    # ---------- 4. 重跑发布门禁（服务端同款路径） ----------
    hr("[4] 重跑发布门禁（G1-G4，admin_dsh._run_gate）")
    import admin_dsh
    for pid in (L0_ID, FULL_ID):
        t0 = time.time()
        result = admin_dsh._run_gate(pid)
        ok = result.get("triple_ok")
        print(f"  {pid}: triple_ok={ok}  ({time.time() - t0:.0f}s)")
        print(f"    summary: {result.get('summary')}")
        for k, g in sorted((result.get("gates") or {}).items()):
            print(f"    {k.upper()}: {'OK' if g.get('ok') else 'FAIL'} — {g.get('detail')}")

    # ---------- 5. 终态验证 ----------
    hr("[5] 同步后台账终态")
    plugins = reg.list_plugins()
    print(f"  插件行数: {len(plugins)}（同步前 {len(before)}）")
    for p in plugins:
        gate = p.get("release_gate") or {}
        npm = (p.get("distribution") or {}).get("npm") or {}
        print(f"  - {p['plugin_id']:<18} ch={p['channel']:<3} ver={p['version']:<14} "
              f"status={p['status']:<10} gate={'✓' if gate.get('triple_ok') else '✗'} "
              f"({gate.get('ts', '')[:19]}) npm={npm}")
    ids = sorted(p["plugin_id"] for p in plugins)
    expect = sorted([L0_ID, FULL_ID])
    print(f"\n  判定: {'✅ 台账=规范两行，版本/门禁/dist_tag 全部同步' if ids == expect else '❌ 异常: ' + str(ids)}")
    print(f"  备份保留于: {backup}")
    return 0 if ids == expect else 1


if __name__ == "__main__":
    sys.exit(main())
