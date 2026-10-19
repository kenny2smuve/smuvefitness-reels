"""Trim the engine's cache/ folder (START_HERE 5 step 10; SPEC "Render speed" 6).

What: deletes files under config.CACHE that are older than a retention window, then, if the
folder is still bigger than --max-gb, deletes the oldest files until it fits. Afterwards it
removes the directories left empty. Per-subfolder retention: cache/transcripts/ 30 days (a
whisper pass is expensive), cache/panel/ 14 days (critic packets are re-read during burn-in),
cache/segs/ 7 days (segment cache, re-rendering is cheap but not free), everything else --days
(default 3).

Why: cache/ is the only place the engine may delete anything (RULES 1, SPEC non-negotiables),
and clips copied from Drive plus proxies and segments fill a disk in days. Every file deletion
goes through safeio.remove_cache_file, which refuses anything outside cache/; the only other
removal, an empty directory, goes through remove_cache_dir() below with the same guard (safeio
has no directory API). A path whose resolved location is outside cache/ (a junction or symlink
dropped into cache/) is refused and reported, never followed. A file that cannot be deleted
(still open by ffmpeg, antivirus hold) is reported under "failed" and the sweep continues.

The size pass (when cache/ is still over --max-gb after the age pass) deletes cheap-to-rebuild
files first (clips, proxies, segs: oldest first), and only then transcripts/ and panel/, which
are expensive to regenerate.

Run:  py -m tools.clean_cache [--days 3] [--max-gb 40] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from . import config, safeio

DEFAULT_DAYS = 3
DEFAULT_MAX_GB = 40.0
# subfolder (relative to cache/) -> retention days; longest matching prefix wins
RETENTION_DAYS: dict[str, int] = {
    "transcripts": 30,
    "panel": 14,
    "segs": 7,
}
NEVER_DELETE_NAMES = {".gitkeep"}
# Subfolders whose files are expensive to regenerate: the size pass takes them last.
PROTECTED_SUBFOLDERS = ("transcripts", "panel")


def retention_for(rel: Path, default_days: int) -> int:
    """Retention in days for a file at `rel` (relative to cache/)."""
    parts = rel.parts
    if parts:
        top = parts[0]
        if top in RETENTION_DAYS:
            return RETENTION_DAYS[top]
    return default_days


def is_protected(rel: Path) -> bool:
    """True for files under a PROTECTED_SUBFOLDERS top-level folder (relative to cache/)."""
    return bool(rel.parts) and rel.parts[0] in PROTECTED_SUBFOLDERS


def _is_safe(p: Path, cache: Path) -> bool:
    """True when the path is a regular file (not a link) whose resolved location is in cache/."""
    if p.is_symlink():
        return False
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    return safeio.is_under(p, cache)


def scan(cache: Path | None = None) -> tuple[list[dict], list[str]]:
    """List every file under cache/ with size and mtime. Returns (files, refused_paths).
    Symlinked directories are not descended into; linked or out-of-tree files are refused."""
    cache = Path(cache or config.CACHE)
    files: list[dict] = []
    refused: list[str] = []
    if not cache.is_dir():
        return files, refused
    for dirpath, dirnames, filenames in os.walk(cache, followlinks=False):
        d = Path(dirpath)
        if not safeio.is_under(d, cache):
            refused.append(str(d))
            dirnames[:] = []
            continue
        keep_dirs = []
        for name in dirnames:
            sub = d / name
            if sub.is_symlink() or not safeio.is_under(sub, cache):
                refused.append(str(sub))
            else:
                keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in filenames:
            p = d / name
            if not _is_safe(p, cache):
                refused.append(str(p))
                continue
            try:
                st = p.stat()
            except OSError:
                refused.append(str(p))
                continue
            files.append({"path": p, "rel": p.relative_to(cache), "size": st.st_size,
                          "mtime": st.st_mtime})
    return files, refused


def plan(files: list[dict], *, now: datetime, days: int = DEFAULT_DAYS,
         max_gb: float = DEFAULT_MAX_GB) -> tuple[list[dict], list[dict]]:
    """Decide what to delete. Returns (to_delete, to_keep), each a list of scan() entries with a
    "reason" added on the delete side ("age" or "size")."""
    now_ts = now.timestamp()
    to_delete: list[dict] = []
    survivors: list[dict] = []
    for f in files:
        if f["path"].name in NEVER_DELETE_NAMES:
            survivors.append(f)
            continue
        keep_days = retention_for(f["rel"], days)
        if f["mtime"] < now_ts - keep_days * 86400:
            to_delete.append({**f, "reason": "age"})
        else:
            survivors.append(f)
    budget = int(max_gb * 1024 ** 3)
    total = sum(f["size"] for f in survivors)
    if total > budget:
        # cheap-to-rebuild files first (oldest first within each class), protected last
        survivors.sort(key=lambda f: (is_protected(f["rel"]), f["mtime"]))
        kept: list[dict] = []
        for f in survivors:
            if total > budget and f["path"].name not in NEVER_DELETE_NAMES:
                to_delete.append({**f, "reason": "size"})
                total -= f["size"]
            else:
                kept.append(f)
        survivors = kept
    return to_delete, survivors


def remove_cache_dir(path: Path) -> bool:
    """Remove one EMPTY directory, only inside config.CACHE and never cache/ itself (the same
    guard safeio.remove_cache_file applies to files; safeio has no directory API). Returns True
    when the directory was removed; False when it is not empty, not a directory, or gone."""
    p = Path(path)
    if not safeio.is_under(p, config.CACHE) or safeio.is_under(config.CACHE, p):
        raise safeio.SafeIOError(f"directory removal is only allowed inside cache/: {p}")
    if p.is_symlink() or not p.is_dir():
        return False
    try:
        os.rmdir(p)          # refuses a non-empty directory by itself
    except OSError:
        return False
    return True


def remove_empty_dirs(cache: Path, *, dry_run: bool = False,
                      planned_gone: set[str] | None = None) -> list[str]:
    """Remove directories under cache/ that are empty, bottom-up. cache/ itself stays.
    In a dry run nothing is removed; directories are reported as "would be removed" when they
    are empty now or would be once the paths in `planned_gone` (resolved strings of the files a
    dry run would delete) are gone."""
    removed: list[str] = []
    if not cache.is_dir():
        return removed
    gone: set[str] = set(planned_gone or ())
    for dirpath, dirnames, filenames in os.walk(cache, topdown=False, followlinks=False):
        d = Path(dirpath)
        if d == cache or d.is_symlink() or not safeio.is_under(d, cache):
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        if dry_run:
            if all(str(e) in gone for e in entries):
                removed.append(str(d))
                gone.add(str(d))
            continue
        if entries:
            continue
        if remove_cache_dir(d):
            removed.append(str(d))
    return removed


def clean(cache: Path | None = None, *, days: int = DEFAULT_DAYS, max_gb: float = DEFAULT_MAX_GB,
          now: datetime | None = None, dry_run: bool = False) -> dict:
    """Run the whole sweep. Returns a summary dict:
    {deleted, bytes_freed, kept, kept_bytes, refused, failed, dirs_removed, dry_run, deleted_files}.
    `failed` lists files the sweep could not delete (still open, locked); they are retried on
    the next run and never abort the sweep."""
    cache = Path(cache or config.CACHE)
    if not safeio.is_under(cache, config.CACHE):
        raise safeio.SafeIOError(f"clean_cache only sweeps inside {config.CACHE}: {cache}")
    now = now or datetime.now(config.TZ)
    files, refused = scan(cache)
    to_delete, kept = plan(files, now=now, days=days, max_gb=max_gb)
    deleted = 0
    freed = 0
    deleted_files: list[dict] = []
    failed: list[dict] = []
    for f in to_delete:
        if not dry_run:
            try:
                if not safeio.remove_cache_file(f["path"]):
                    continue
            except OSError as e:          # PermissionError (open/locked file), SafeIOError, ...
                failed.append({"path": str(f["path"]), "size": f["size"], "reason": f["reason"],
                               "error": f"{type(e).__name__}: {e}"})
                continue
        deleted += 1
        freed += f["size"]
        deleted_files.append({"path": str(f["path"]), "size": f["size"], "reason": f["reason"]})
    planned = {str(f["path"]) for f in to_delete} if dry_run else None
    dirs_removed = remove_empty_dirs(cache, dry_run=dry_run, planned_gone=planned)
    return {
        "cache": str(cache),
        "deleted": deleted,
        "bytes_freed": freed,
        "kept": len(kept),
        "kept_bytes": sum(f["size"] for f in kept),
        "refused": refused,
        "failed": failed,
        "dirs_removed": dirs_removed,
        "dry_run": dry_run,
        "days": days,
        "max_gb": max_gb,
        "deleted_files": deleted_files,
    }


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # paths may be non-ASCII
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="py -m tools.clean_cache", description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"delete files older than this (default {DEFAULT_DAYS}; "
                         f"transcripts 30, panel 14, segs 7 regardless)")
    ap.add_argument("--max-gb", type=float, default=DEFAULT_MAX_GB,
                    help=f"then delete oldest first until cache/ is under this (default {DEFAULT_MAX_GB})")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, delete nothing (dirs that would end up empty are counted too)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    a = ap.parse_args(argv)
    try:
        res = clean(days=a.days, max_gb=a.max_gb, dry_run=a.dry_run)
    except safeio.SafeIOError as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"clean_cache: refused: {e}")
        return 1
    if a.json:
        print(json.dumps(res, default=str))
    else:
        tag = "would delete" if a.dry_run else "deleted"
        print(f"clean_cache: {tag} {res['deleted']} file(s), {_fmt_bytes(res['bytes_freed'])}; "
              f"kept {res['kept']} ({_fmt_bytes(res['kept_bytes'])}); "
              f"{len(res['dirs_removed'])} empty dir(s); {len(res['refused'])} refused; "
              f"{len(res['failed'])} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
