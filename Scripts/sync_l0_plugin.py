# -*- coding: utf-8 -*-
r"""
sync_l0_plugin.py — 程序仓镜像 motor-ai-l0 与独立发布仓同步工具（治本 B）
==========================================================================
背景：
  D:\MotorDesign\motor-ai-l0\  是被 MotorDesign git 追踪的「镜像副本」，
  被 app/admin_dsh.py（台账种子 _ensure_seeded + 发布门禁 G1-G4）物理消费。
  真源是独立发布仓 D:\dsh-motor-ai-l0（npm/GitHub 发布物）。
  两者双源漂移会导致管理端台账/门禁读到旧版本（如 0.1.0 vs 0.1.4）。

  本工具把「真源(独立仓) -> 镜像(程序仓)」的同步动作固化，供发版流程
  (motorai-release-pipeline) 在每次发版后调用，消除双源复发。

模式：
  --check          只读：对比两侧，输出漂移报告 + 版本号，不改任何文件、不退出码
                   （默认行为；exit 0 表示已同步，exit 2 表示存在漂移）
  --apply          同步（复制 + 清脏 + 校验 md5），不 git 提交
  --commit         同步 + git add + git commit（镜像改动入 MotorDesign 仓）
  --dry-run        与 --apply 相同，仅打印将做的操作，不落盘

路径（可被环境变量覆盖，避免硬编码）：
  L0_SOURCE_REPO   真源（默认 D:\dsh-motor-ai-l0）
  L0_MIRROR_DIR    镜像（默认 <MotorDesign>/motor-ai-l0，按本脚本位置解析）
  本脚本位于 <MotorDesign>/Scripts/，故镜像默认 = parents[1]/motor-ai-l0

安全约束：
  - 只读真源（L0_SOURCE_REPO），绝不写它。
  - 只写镜像（L0_MIRROR_DIR）。
  - 按真源 .gitignore 规则过滤，排除构建/脏产物（dist/、metadata/、*.tgz、
    node_modules/、__pycache__/、*.pyc、.DS_Store）。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve()
# Scripts/ -> 仓库根
REPO_ROOT = SCRIPT.parent.parent
DEFAULT_MIRROR = REPO_ROOT / "motor-ai-l0"
DEFAULT_SOURCE = Path(r"D:\dsh-motor-ai-l0")

EXCLUDE_DIRS = {"dist", "metadata", "node_modules", "__pycache__", ".git"}
EXCLUDE_SUFFIX = (".tgz", ".pyc")
EXCLUDE_BASE = {".DS_Store"}


def _git(*args, cwd, check=False):
    try:
        out = subprocess.run(["git", *args], cwd=str(cwd),
                             capture_output=True, text=True, timeout=120)
        if check and out.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} rc={out.returncode}: {out.stderr.strip()[:200]}")
        return out.stdout.strip()
    except FileNotFoundError:
        return ""


def _tracked_files(source: Path):
    files = _git("ls-files", cwd=source).splitlines()
    return [f.replace("\\", "/") for f in files if f.strip()]


def _is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] in EXCLUDE_DIRS:
        return True
    if any(rel.endswith(s) for s in EXCLUDE_SUFFIX):
        return True
    if parts[-1] in EXCLUDE_BASE:
        return True
    return False


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pkg_version(p: Path) -> str:
    try:
        return json.loads((p / "package.json").read_text(encoding="utf-8")).get("version", "?")
    except Exception:
        return "(unreadable)"


def compute_white(source: Path):
    """返回 (white, dropped) 两个相对路径列表。"""
    tracked = _tracked_files(source)
    white = [f for f in tracked if not _is_excluded(f)]
    dropped = [f for f in tracked if _is_excluded(f)]
    return white, dropped


def diff_state(source: Path, mirror: Path, white):
    """返回 (missing_in_mirror, hash_mismatch, ok_count)。"""
    missing, mismatch, ok = [], [], 0
    for rel in white:
        s = source / rel
        d = mirror / rel
        if not s.exists():
            continue
        if not d.exists():
            missing.append(rel)
        elif _md5(s) != _md5(d):
            mismatch.append(rel)
        else:
            ok += 1
    return missing, mismatch, ok


def do_apply(source: Path, mirror: Path, dry_run: bool):
    white, dropped = compute_white(source)
    mirror.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print("[dry-run] 将复制/覆盖 %d 个白名单文件 (真源 %s -> 镜像 %s):" % (len(white), source, mirror))
        for rel in white:
            print("   copy", rel)
        print("[dry-run] 将删除镜像脏目录:", [d for d in ("dist", "metadata") if (mirror / d).exists()])
        print("[dry-run] 未落盘。")
        return

    # 1) 复制白名单
    for rel in white:
        s = source / rel
        if not s.exists():
            continue
        d = mirror / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)

    # 2) 清镜像脏目录（git rm + 物理删）
    for sub in ("dist", "metadata"):
        p = mirror / sub
        if p.exists():
            _git("rm", "-r", "-q", "--ignore-unmatch", sub, cwd=mirror)
            shutil.rmtree(p, ignore_errors=True)

    # 3) 校验
    missing, mismatch, ok = diff_state(source, mirror, white)
    status = "OK" if not missing and not mismatch else "MISMATCH"
    print(f"[apply] 复制 {len(white)} 个白名单文件 -> 镜像 | 校验 {status} (ok={ok})")
    if missing:
        print("   镜像缺失:", missing)
    if mismatch:
        print("   hash 不一致:", mismatch)
    print(f"[apply] 源版本 {_pkg_version(source)} | 镜像版本 {_pkg_version(mirror)}")
    return not (missing or mismatch)


def do_commit(mirror: Path, source_ver: str):
    _git("add", "-A", "motor-ai-l0/", cwd=REPO_ROOT)
    msg = (f"chore(mirror): 同步 motor-ai-l0 镜像至 {source_ver} "
           f"(via Scripts/sync_l0_plugin.py)")
    _git("commit", "-m", msg, cwd=REPO_ROOT)
    print(f"[commit] 已提交 MotorDesign: {msg}")
    print("   " + _git("log", "--oneline", "-1", cwd=REPO_ROOT))


def main():
    ap = argparse.ArgumentParser(description="同步 motor-ai-l0 镜像到独立真源")
    ap.add_argument("--check", action="store_true", help="只读漂移报告（默认）")
    ap.add_argument("--apply", action="store_true", help="执行同步（不提交）")
    ap.add_argument("--commit", action="store_true", help="同步 + git 提交")
    ap.add_argument("--dry-run", action="store_true", help="打印将做操作，不落盘")
    ap.add_argument("--source", default=os.getenv("L0_SOURCE_REPO", str(DEFAULT_SOURCE)),
                    help="真源仓路径（默认 D:\\dsh-motor-ai-l0 或 $L0_SOURCE_REPO）")
    ap.add_argument("--mirror", default=os.getenv("L0_MIRROR_DIR", str(DEFAULT_MIRROR)),
                    help="镜像目录（默认 <repo>/motor-ai-l0 或 $L0_MIRROR_DIR）")
    args = ap.parse_args()

    source = Path(args.source)
    mirror = Path(args.mirror)
    if not source.exists():
        print(f"错误: 真源仓不存在 {source}", file=sys.stderr)
        sys.exit(1)

    white, dropped = compute_white(source)
    missing, mismatch, ok = diff_state(source, mirror, white)
    dirty_left = [d for d in ("dist", "metadata") if (mirror / d).exists()]

    print("=" * 60)
    print(f"真源 {source}  version={_pkg_version(source)}")
    print(f"镜像 {mirror}  version={_pkg_version(mirror)}")
    print(f"白名单内容文件 {len(white)} | 排除脏文件 {len(dropped)} | 已一致 {ok}")
    print("=" * 60)

    # --commit 隐含 apply
    if args.commit:
        ok2 = do_apply(source, mirror, dry_run=False)
        if not ok2:
            print("[commit] 同步后校验未通过，放弃提交。")
            sys.exit(3)
        do_commit(mirror, _pkg_version(source))
        sys.exit(0)

    if args.apply:
        ok2 = do_apply(source, mirror, dry_run=False)
        sys.exit(0 if ok2 else 3)

    if args.dry_run:
        do_apply(source, mirror, dry_run=True)
        sys.exit(0)

    # 默认 --check（或无任何动作）：漂移报告
    print("漂移检测:")
    if missing:
        print(f"   ❌ 镜像缺失 {len(missing)} 个:", *missing, sep="\n     ")
    if mismatch:
        print(f"   ❌ hash 不一致 {len(mismatch)} 个:", *mismatch, sep="\n     ")
    if dirty_left:
        print(f"   ⚠️ 镜像残留脏目录: {dirty_left}")
    if not missing and not mismatch and not dirty_left:
        print("   ✅ 两侧一致，无漂移。")
    sys.exit(0 if (not missing and not mismatch and not dirty_left) else 2)


if __name__ == "__main__":
    main()
