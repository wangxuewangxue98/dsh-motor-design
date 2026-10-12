# -*- coding: utf-8 -*-
"""几何截图存储治理（改进方案 v1 的「方案 A：TTL + 数量上限」）。

背景（2026-09-12/13 实测）
--------------------------
``Scripts/rmxprt_images/`` 此前**无任何清理策略**：131 张 → 152 张、980MB → 1150MB，
两天增长 ≈490MB/天，磁盘存在枯竭风险。本模块提供常驻清扫器：

1. **TTL**：mtime 超过 ``IMG_SWEEP_TTL_DAYS``（默认 7 天）的图片删除；
2. **数量上限**：清理 TTL 后若仍超过 ``IMG_SWEEP_MAX_FILES``（默认 1000），按 mtime
   从旧到新删至上限；
3. **宽限保护**：mtime 在 ``IMG_SWEEP_GRACE_MIN``（默认 60 分钟）内的文件一律跳过，
   避免误删本轮工作流正在引用/正在抓屏的图；
4. **空目录回收**：清理完后移除 ``rmxprt_images/`` 下的空 run 子目录（方案 C 的分目录结构）。

与既有约定的关系
----------------
- 线程模型完全镜像 ``Core/human_checkpoint.start_sweeper()``：幂等、daemon、永不退出、单轮异常忽略；
- 仅影响 ``rmxprt_images/`` 存储，不触碰求解/评审逻辑；删除后历史图将 404，
  客户端已有「无可用几何截图」降级路径（方案文档 §二 已核验）。

环境变量
--------
===============================  =======  ======================================
``MOTORAI_DISABLE_IMG_SWEEPER``   ``""``   置 1 整体禁用清扫线程
``IMG_SWEEP_TTL_DAYS``            ``7``    TTL 天数（0 = 不按 TTL 清理）
``IMG_SWEEP_MAX_FILES``           ``1000`` 数量上限（0 = 不限）
``IMG_SWEEP_INTERVAL``            ``3600`` 清扫周期（秒，最小 60）
``IMG_SWEEP_GRACE_MIN``           ``60``   宽限分钟数（保护在跑的图）
``IMG_SWEEP_DRY_RUN``             ``""``   置 1 只统计不删除（排障用）
===============================  =======  ======================================
"""
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ── 路径：<repo>/Core/geometry_image_store.py → <repo>/Scripts/rmxprt_images ──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IMAGE_DIR = os.path.join(_REPO_ROOT, "Scripts", "rmxprt_images")

IMAGE_EXTS = (".bmp", ".png", ".jpg", ".jpeg", ".gif")

DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_FILES = 1000
DEFAULT_INTERVAL_S = 3600
DEFAULT_GRACE_MIN = 60
MIN_INTERVAL_S = 60

_sweeper_lock = threading.Lock()
_sweeper_started = False


# ───────────────────────────── 配置 ─────────────────────────────
def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except Exception:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def sweeper_config() -> Dict[str, Any]:
    """当前生效的清扫配置（供日志/可观测性）。"""
    return {
        "enabled": not _env_flag("MOTORAI_DISABLE_IMG_SWEEPER", False),
        "image_dir": DEFAULT_IMAGE_DIR,
        "ttl_days": _env_int("IMG_SWEEP_TTL_DAYS", DEFAULT_TTL_DAYS),
        "max_files": _env_int("IMG_SWEEP_MAX_FILES", DEFAULT_MAX_FILES),
        "interval_s": max(MIN_INTERVAL_S, _env_int("IMG_SWEEP_INTERVAL", DEFAULT_INTERVAL_S)),
        "grace_min": _env_int("IMG_SWEEP_GRACE_MIN", DEFAULT_GRACE_MIN),
        "dry_run": _env_flag("IMG_SWEEP_DRY_RUN", False),
    }


# ───────────────────────────── 扫描 ─────────────────────────────
def _iter_images(root: str) -> List[Tuple[str, float, int]]:
    """返回 [(path, mtime, size)]，仅含图片扩展名与 .part 半成品。"""
    out: List[Tuple[str, float, int]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            low = fn.lower()
            if not (low.endswith(IMAGE_EXTS) or low.endswith(".part")):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out.append((fp, st.st_mtime, st.st_size))
    return out


def image_store_stats(root: Optional[str] = None) -> Dict[str, Any]:
    """只读统计（不删除），供 /api/status 等可观测性出口使用。"""
    root = root or DEFAULT_IMAGE_DIR
    rows = _iter_images(root)
    total_b = sum(r[2] for r in rows)
    oldest_days = None
    if rows:
        oldest_days = round((time.time() - min(r[1] for r in rows)) / 86400.0, 2)
    cfg = sweeper_config()
    by_ext: Dict[str, int] = {}
    for fp, _m, _s in rows:
        ext = os.path.splitext(fp)[1].lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
    return {
        "dir": root,
        "files": len(rows),
        "bytes": total_b,
        "mb": round(total_b / 1048576.0, 2),
        "by_ext": by_ext,
        "oldest_days": oldest_days,
        "ttl_days": cfg["ttl_days"],
        "max_files": cfg["max_files"],
    }


# ───────────────────────────── 清扫 ─────────────────────────────
def sweep_images(root: Optional[str] = None,
                 ttl_days: Optional[int] = None,
                 max_files: Optional[int] = None,
                 grace_min: Optional[int] = None,
                 dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """执行一轮清扫。返回统计 dict（任何单文件失败都不中断整轮）。"""
    cfg = sweeper_config()
    root = root or cfg["image_dir"]
    ttl_days = cfg["ttl_days"] if ttl_days is None else ttl_days
    max_files = cfg["max_files"] if max_files is None else max_files
    grace_min = cfg["grace_min"] if grace_min is None else grace_min
    dry_run = cfg["dry_run"] if dry_run is None else dry_run

    now = time.time()
    grace_s = max(0, int(grace_min)) * 60
    rows = _iter_images(root)

    ttl_deleted: List[str] = []
    cap_deleted: List[str] = []
    skipped_grace = 0
    failed: List[str] = []
    freed = 0

    # 1) TTL
    survivors: List[Tuple[str, float, int]] = []
    for fp, mt, sz in rows:
        if grace_s and (now - mt) < grace_s:
            skipped_grace += 1
            survivors.append((fp, mt, sz))
            continue
        if ttl_days and ttl_days > 0 and (now - mt) > ttl_days * 86400:
            if _unlink(fp, dry_run, failed):
                ttl_deleted.append(fp)
                freed += sz
            continue
        survivors.append((fp, mt, sz))

    # 2) 数量上限（先 TTL 后按 mtime 从旧到新裁）
    if max_files and max_files > 0 and len(survivors) > max_files:
        survivors.sort(key=lambda r: r[1])  # 旧 → 新
        need = len(survivors) - max_files
        for fp, mt, sz in survivors:
            if need <= 0:
                break
            if grace_s and (now - mt) < grace_s:
                continue  # 宽限内不删，宁可达不到上限
            if _unlink(fp, dry_run, failed):
                cap_deleted.append(fp)
                freed += sz
                need -= 1

    # 3) 回收空目录（run_*/ 分目录结构的残留）
    removed_dirs = _prune_empty_dirs(root, dry_run)

    remaining = _iter_images(root)
    res = {
        "ok": True,
        "dry_run": bool(dry_run),
        "dir": root,
        "ttl_days": ttl_days,
        "max_files": max_files,
        "grace_min": grace_min,
        "scanned": len(rows),
        "ttl_deleted": len(ttl_deleted),
        "cap_deleted": len(cap_deleted),
        "deleted_total": len(ttl_deleted) + len(cap_deleted),
        "skipped_grace": skipped_grace,
        "bytes_freed": freed,
        "mb_freed": round(freed / 1048576.0, 2),
        "remaining_files": len(remaining),
        "remaining_mb": round(sum(r[2] for r in remaining) / 1048576.0, 2),
        "removed_dirs": removed_dirs,
        "failed": failed[:10],
    }
    return res


def _unlink(fp: str, dry_run: bool, failed: List[str]) -> bool:
    """删除单文件；dry_run 时只记账。成功返回 True。"""
    if dry_run:
        return True
    try:
        os.remove(fp)
        return True
    except Exception:
        failed.append(fp)
        return False


def _prune_empty_dirs(root: str, dry_run: bool) -> List[str]:
    """移除 root 下的空子目录（仅一层，保持结构简单可控）。"""
    removed: List[str] = []
    if not os.path.isdir(root):
        return removed
    try:
        entries = os.listdir(root)
    except OSError:
        return removed
    for name in entries:
        sub = os.path.join(root, name)
        if not os.path.isdir(sub):
            continue
        try:
            if os.listdir(sub):
                continue
            if not dry_run:
                os.rmdir(sub)
            removed.append(sub)
        except Exception:
            pass
    return removed


# ───────────────────────────── 常驻线程 ─────────────────────────────
def start_image_sweeper(interval: Optional[int] = None) -> bool:
    """幂等启动几何图清扫线程（daemon）。重复调用只生效一次。

    返回 True 表示本次真正启动了线程；False 表示已运行 / 被禁用。
    """
    global _sweeper_started
    cfg = sweeper_config()
    if not cfg["enabled"]:
        print("[geom-sweeper] 已禁用 (MOTORAI_DISABLE_IMG_SWEEPER)")
        return False
    with _sweeper_lock:
        if _sweeper_started:
            return False
        _sweeper_started = True

    _interval = max(MIN_INTERVAL_S, int(interval or cfg["interval_s"]))

    def _loop():
        while True:
            try:
                time.sleep(_interval)
                r = sweep_images()
                if r.get("deleted_total"):
                    print(f"[geom-sweeper] 已清理 {r['deleted_total']} 张 "
                          f"({r['mb_freed']}MB)：TTL {r['ttl_deleted']} / 超限 {r['cap_deleted']}；"
                          f"剩余 {r['remaining_files']} 张 / {r['remaining_mb']}MB")
                else:
                    print(f"[geom-sweeper] 无淘汰：{r['scanned']} 张 / "
                          f"{r['remaining_mb']}MB (TTL={r['ttl_days']}d, 上限={r['max_files']})")
            except Exception as _e:
                # 清扫器必须永不退出：单轮异常忽略，下轮继续
                print(f"[geom-sweeper] 清扫异常(已忽略, 下轮重试): {_e}")

    threading.Thread(target=_loop, name="geom-image-sweeper", daemon=True).start()
    print(f"[geom-sweeper] 已启动 (interval={_interval}s, ttl={cfg['ttl_days']}d, "
          f"max_files={cfg['max_files']}, grace={cfg['grace_min']}min, dir={cfg['image_dir']})")
    return True


if __name__ == "__main__":
    import json
    print("config:", json.dumps(sweeper_config(), ensure_ascii=False, indent=2))
    print("stats :", json.dumps(image_store_stats(), ensure_ascii=False, indent=2))
    print("sweep :", json.dumps(sweep_images(dry_run=True), ensure_ascii=False, indent=2))
