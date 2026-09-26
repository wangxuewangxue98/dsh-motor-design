import os, sys, json, tarfile, hashlib, sqlite3
from datetime import datetime, timedelta, timezone

PLUGIN_DIR = r"D:\dsh-motor-ai-l0"
DIST_DIR = os.path.join(PLUGIN_DIR, "dist")
BUNDLE = os.path.join(DIST_DIR, "dsh-motor-ai-l0-v0.2.2.tar.gz")
REGISTRY_PY = r"D:\MotorDesign\Core\dsh_registry.py"
PLUGIN_ID = "dsh-motor-ai-l0"
VERSION = "0.2.2"

sys.path.insert(0, os.path.dirname(REGISTRY_PY))
import dsh_registry as R

EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", "dist", ".workbuddy"}
EXCLUDE_EXT = {".bak", ".pyc", ".pyo"}

def walk_files(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in EXCLUDE_DIRS]
        for f in fn:
            if os.path.splitext(f)[1] in EXCLUDE_EXT:
                continue
            yield os.path.join(dp, f)

print("== 打包 ==")
os.makedirs(DIST_DIR, exist_ok=True)
count = 0
with tarfile.open(BUNDLE, "w:gz") as tf:
    for fp in sorted(walk_files(PLUGIN_DIR)):
        rel = os.path.relpath(fp, PLUGIN_DIR)
        tf.add(fp, arcname=rel)
        count += 1
print("  条目数:", count)

print("== SHA256 ==")
h = hashlib.sha256()
with open(BUNDLE, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
sha = h.hexdigest()
print("  ", sha)

print("== 注册表 ==")
R.update_plugin_fields(PLUGIN_ID, {"version": VERSION, "status": "published"}, db_path=None)
R.set_release_gate(PLUGIN_ID, True, "v0.2.2 修复代理 coef 空间错位(模型 v2.2.0) + 默认配置同步 + 代理端到端 verify 61/61", db_path=None)
token_id = f"tok_{sha[:12]}"
R.issue_token(PLUGIN_ID, token_id, token_type="one_time", tier="free",
              expires_at=(datetime.now(timezone.utc) + timedelta(days=90)).isoformat(), db_path=None)
print("  version ->", VERSION, "| token ->", token_id)

print("== publish-log ==")
log = {
    "plugin_id": PLUGIN_ID,
    "version": VERSION,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "bundle": os.path.basename(BUNDLE),
    "sha256": sha,
    "token_id": token_id,
    "channel": "l0",
    "tier": "free",
}
with open(os.path.join(PLUGIN_DIR, "metadata", "publish-log.json"), "w", encoding="utf-8") as f:
    json.dump(log, f, ensure_ascii=False, indent=2)
print("  ", json.dumps(log, ensure_ascii=False))
print("DONE")
