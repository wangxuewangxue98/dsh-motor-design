#!/usr/bin/env python3
"""手动发布脚本：打包 v0.2.1 并写入发布记录"""
import json, os, tarfile, hashlib, shutil, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
VERSION = "0.2.1"
DIST_DIR = ROOT / "dist"
META_DIR = ROOT / "metadata"
BUNDLE_NAME = f"dsh-motor-ai-l0-v{VERSION}.tar.gz"
BUNDLE_PATH = DIST_DIR / BUNDLE_NAME

# 1. 更新 manifest.json
manifest_path = ROOT / "manifest.json"
with open(manifest_path, encoding="utf-8") as f:
    manifest = json.load(f)
manifest["version"] = VERSION
manifest["skill_version"] = VERSION
manifest["skills"][0]["version"] = VERSION
manifest["published_at"] = datetime.datetime.utcnow().isoformat() + "Z"
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)
print(f"✅ manifest.json 已更新 → {VERSION}")

# 2. 打包（排除 .git, node_modules, dist, metadata）
DIST_DIR.mkdir(parents=True, exist_ok=True)
EXCLUDES = {"**.git/**", "**/node_modules/**", "**/dist/**", "**/metadata/**", "**/*.bak_*", "**/_*.mjs"}

def should_exclude(path):
    p = str(path)
    for exc in EXCLUDES:
        if "*" in exc:
            import fnmatch
            if fnmatch.fnmatch(p, f"*{exc.replace('**/', '').replace('/*', '')}*"):
                return True
    return False

with tarfile.open(BUNDLE_PATH, "w:gz") as tar:
    for item in sorted(ROOT.iterdir()):
        if item.name in {".git", "node_modules", "dist", "metadata", ".venv-html-to-docx"}:
            continue
        arcname = item.name
        if item.is_dir():
            for root, dirs, files in os.walk(item):
                # 跳过子目录中的排除项
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "dist", "metadata", ".venv-html-to-docx"}]
                for f in files:
                    fpath = Path(root) / f
                    tar.add(fpath, arcname=fpath.relative_to(ROOT))
        else:
            tar.add(item, arcname=item.name)
size = BUNDLE_PATH.stat().st_size
print(f"✅ 打包成功: {BUNDLE_NAME} ({size:,} bytes)")

# 3. 计算 SHA256
sha256 = hashlib.sha256(BUNDLE_PATH.read_bytes()).hexdigest()
print(f"✅ SHA256: {sha256}")

# 4. 写入发布记录
token_id = f"tok_{sha256[:12]}"
record = {
    "plugin_id": "dsh-motor-ai-l0",
    "version": VERSION,
    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "bundle": BUNDLE_NAME,
    "sha256": sha256,
    "token_id": token_id,
    "channel": "l0",
    "tier": "free"
}
META_DIR.mkdir(parents=True, exist_ok=True)
with open(META_DIR / "publish-log.json", "w", encoding="utf-8") as f:
    json.dump(record, f, indent=2, ensure_ascii=False)
print(f"✅ 发布记录: {META_DIR / 'publish-log.json'}")

print(f"\n📦 安装口令: {token_id}")
print(f"📁 Bundle: {BUNDLE_PATH}")
print(f"🔐 SHA256: {sha256}")
