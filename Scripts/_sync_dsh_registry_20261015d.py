# -*- coding: utf-8 -*-
"""
_sync_dsh_registry_20261015d.py — 台账四次同步（l0 0.5.2→0.5.3 / full rc.15→rc.16）
====================================================================================
背景：rc.16 / l0 0.5.3 修 L0 四项缺陷（用户三轮压测报告驱动）：
  - P0-2 maxMatrixSize 静默截断（跨 rc.9~rc.15）：截断后仅置 truncated 布尔位，
    无告警无报错 ⇒ 密度被静默改变、Top1 效率与达标判定随 count 漂移。现补
    MATRIX_TRUNCATED 结构化告警 + strictMaxMatrixSize 抛错模式 + MATRIX_HARD_CAP=50000
  - P1-1 冷却 water 静默回退 forced_air：补 COOLING_FALLBACK 告警（行为不变）
  - P0-1 verdict 密度敏感（缓解）：新增 density_check 双密度自检（默认 OFF 零回归）
  - P0-4 默认 8 极静默假达标：按用户建议不改默认逻辑，输出 pole_suggestion 提示

步骤：0 备份 → 1 双侧版本+dist_tag → 2 重跑 G1-G4 → 3 终态断言
用法：Scripts 下运行（python 需能 import Core/app）
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

# 门禁子进程需要 node：确保 PATH 里有 managed node
MANAGED_NODE = r"C:\Users\15389\.workbuddy\binaries\node\versions\24.19.0"
if MANAGED_NODE not in os.environ.get("PATH", ""):
    os.environ["PATH"] = MANAGED_NODE + os.pathsep + os.environ.get("PATH", "")

NEW = {"dsh-motor-ai-l0": "0.5.3", "dsh-motor-ai": "1.0.0-rc.16"}

# ---------------------------------------------------------------- 步骤 0：备份
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
bak = BACKUP_DIR / f"dsh_plugin_registry-{ts}.db"
shutil.copy2(DB, bak)
print(f"[0] 备份 -> {bak}  ({bak.stat().st_size} bytes)")

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

# ---------------------------------------------------------------- 步骤 1：版本刷新
for pid, ver in NEW.items():
    row = cur.execute(
        "SELECT plugin_id, version, distribution FROM dsh_plugin_registry WHERE plugin_id=?", (pid,)
    ).fetchone()
    if row is None:
        sys.exit(f"FATAL: 台账无 {pid} 行（不应发生；先查 _ensure_seeded）")
    dist = json.loads(row["distribution"] or "{}")
    npm = dist.get("npm", {})
    npm["version"] = ver
    npm["dist_tag"] = "latest"
    npm["registry"] = "https://registry.npmjs.org"
    dist["npm"] = npm
    cur.execute(
        "UPDATE dsh_plugin_registry SET version=?, distribution=?, updated_at=datetime('now') WHERE plugin_id=?",
        (ver, json.dumps(dist, ensure_ascii=False), pid),
    )
    print(f"[1] {pid}: {row['version']} -> {ver}  (npm dist_tag=latest)")

con.commit()

# ---------------------------------------------------------------- 步骤 2：重跑双侧 G1-G4
sys.path.insert(0, str(REPO))
os.chdir(REPO)
from app.admin_dsh import _run_gate  # noqa: E402  （Flask 蓝图同款路径）

for pid in NEW:
    r = _run_gate(pid)
    ok = r.get("triple_ok")
    summary = r.get("summary")
    print(f"[2] {pid}: triple_ok={ok}  {summary}")
    for k, g in sorted((r.get("gates") or {}).items()):
        print(f"      {k}: ok={g.get('ok')} detail={g.get('detail')}")
    if not ok:
        sys.exit(f"FATAL: {pid} 门禁未过：{r}")

# ---------------------------------------------------------------- 步骤 3：终态断言
print("\n=== 台账终态 ===")
for row in cur.execute(
    "SELECT plugin_id, channel, version, status, distribution, release_gate FROM dsh_plugin_registry ORDER BY channel"
):
    dist = json.loads(row["distribution"] or "{}")
    gate = json.loads(row["release_gate"] or "{}")
    npm = dist.get("npm", {})
    print(
        f"{row['plugin_id']:<18} ch={row['channel']:<3} ver={row['version']:<14} "
        f"status={row['status']:<10} npm={npm.get('version')}@{npm.get('dist_tag')} "
        f"gate={gate.get('summary')}"
    )

assertions = []
for pid, ver in NEW.items():
    row = cur.execute(
        "SELECT version, status, distribution FROM dsh_plugin_registry WHERE plugin_id=?", (pid,)
    ).fetchone()
    dist = json.loads(row["distribution"] or "{}")
    assertions.append((f"{pid} 版本={ver}", row["version"] == ver))
    assertions.append((f"{pid} status=published", row["status"] == "published"))
    assertions.append((f"{pid} npm.version={ver}", dist.get("npm", {}).get("version") == ver))
    assertions.append((f"{pid} npm.dist_tag=latest", dist.get("npm", {}).get("dist_tag") == "latest"))

n = cur.execute("SELECT COUNT(*) FROM dsh_plugin_registry").fetchone()[0]
assertions.append(("台账恰 2 行（无遗留行）", n == 2))

toks = [t[0] for t in cur.execute("SELECT DISTINCT plugin_id FROM dsh_plugin_tokens")]
print(f"  token plugin_id 集合: {sorted(toks)}")
assertions.append(("token 全挂规范 id（无 motor-ai-l0 遗留）", set(toks) <= set(NEW)))

print("\n=== 终态断言 ===")
allok = True
for name, ok in assertions:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    allok = allok and ok

con.close()
if not allok:
    sys.exit("FATAL: 终态断言失败")

print(f"\nALL_OK  备份={bak}")
