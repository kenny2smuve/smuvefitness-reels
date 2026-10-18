"""Index his footage into ledgers/footage.csv (START_HERE 3.6, SPEC "footage.csv").

What: walks the footage folders on his Google Drive mirror (config.FOOTAGE_ROOT), reads the
header of every video file ONCE with ffprobe and records path, size, duration, size, fps,
codec, color transfer (arib-std-b67 = HLG), rotation, capture time, camera, folder and session
in footage.csv. A second run only touches files that are new or whose size changed, so the
ledger is cheap to keep complete.

Why: every cut starts from capture order (the Meta glasses recording time lives in the
com.apple.quicktime.creationdate tag, never in the file name) and every gate that checks shot
reuse or opener reuse reads footage.csv. Drive for desktop may download a file the first time
it is read, so the index is throttled by a daily byte budget (state/index_budget.json, 20 GB
by default) and never copies folders.

Folder selection (when no --folders are given): direct children of FOOTAGE_ROOT whose name is
in config.KNOWN_FOOTAGE_FOLDERS or starts with one of config.FOOTAGE_FOLDER_PREFIXES, plus any
direct child that contains (by file NAME only, nothing is opened) a file starting with VID_,
IMG_, MVI_ or video-. His "ClaudeInk Reels" output folder (config.DRIVE_OUT) and everything
else (personal folders) are never touched.

Run:  py -m tools.footage_index [--folders "Session 1 of 4" "Akira back"] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from . import config, safeio

# Header order is the contract (SPEC.md "Ledger CSV schemas"); tools.ledgers owns the same list.
FOOTAGE_HEADER: list[str] = [
    "path", "size_bytes", "duration_s", "width", "height", "fps", "codec", "color_transfer",
    "rotation", "capture_time", "camera", "folder", "session", "used_as_opener_in", "used_in",
    "indexed_at",
]
# Columns a re-index must never clobber: they are written by deliveries, not by probing.
PRESERVED_COLS = ("used_as_opener_in", "used_in")

# File-name prefixes that mark a folder as footage (matched case-insensitively).
FOOTAGE_NAME_PREFIXES = ("vid_", "img_", "mvi_", "video-")

BUDGET_FILE = "index_budget.json"

PROBE_ENTRIES = ("format=duration,size:"
                 "format_tags=com.apple.quicktime.creationdate,creation_time:"
                 "stream=codec_type,codec_name,width,height,r_frame_rate,color_transfer,color_range:"
                 "stream_side_data=rotation")


class FootageIndexError(RuntimeError):
    """The ledger cannot be used (wrong header) or a folder argument is not a footage folder."""


# --------------------------------------------------------------------------- small helpers
def camera_of(name: str) -> str:
    """Camera from the file name prefix: MVI_ canon, IMG_ iphone, VID_/video- meta, else unknown."""
    n = name.lower()
    if n.startswith("mvi_"):
        return "canon"
    if n.startswith("img_"):
        return "iphone"
    if n.startswith("vid_") or n.startswith("video-"):
        return "meta"
    return "unknown"


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {e.lower() for e in config.VIDEO_EXTS}


def _looks_like_footage_name(name: str) -> bool:
    return name.lower().startswith(FOOTAGE_NAME_PREFIXES)


def _folder_is_footage(folder: Path) -> bool:
    """Known name, known prefix, or holds a VID_/IMG_/MVI_/video- file (names only, no reads)."""
    name = folder.name
    if name in config.KNOWN_FOOTAGE_FOLDERS:
        return True
    if any(name.startswith(p) for p in config.FOOTAGE_FOLDER_PREFIXES):
        return True
    try:
        for child in folder.iterdir():
            if child.is_file() and _looks_like_footage_name(child.name):
                return True
    except OSError:
        return False
    return False


def select_folders(root: Path | None = None) -> list[Path]:
    """Direct children of the footage root that are footage folders, sorted by name.
    config.DRIVE_OUT (our own output mirror) and hidden folders are never selected."""
    root = Path(root) if root is not None else config.FOOTAGE_ROOT
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if safeio.is_under(child, config.DRIVE_OUT) or safeio.is_under(config.DRIVE_OUT, child):
            continue
        if _folder_is_footage(child):
            out.append(child)
    return out


def list_videos(folder: Path) -> list[Path]:
    """Every video file under a footage folder (recursive, hidden folders skipped), sorted."""
    folder = Path(folder)
    found: list[Path] = []
    if not folder.is_dir():
        return found
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            children = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for c in children:
            if c.is_dir():
                if c.name.startswith(".") or safeio.is_under(c, config.DRIVE_OUT):
                    continue
                stack.append(c)
            elif is_video(c):
                found.append(c)
    return sorted(found, key=lambda p: str(p).lower())


# --------------------------------------------------------------------------- time parsing
def _parse_iso(s: str) -> datetime | None:
    s = s.strip()
    if not s:
        return None
    t = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        pass
    # ffprobe sometimes prints "2026-10-11 14:03:22" style stamps
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def capture_time_from_tags(tags: dict | None) -> str:
    """ISO-8601 with offset. com.apple.quicktime.creationdate (the glasses' own clock, keeps
    its own offset) wins; else creation_time (UTC, converted to config.TZ); else "". The file
    name is never used. An unparsable tag is kept verbatim rather than dropped."""
    tags = tags or {}
    qt = str(tags.get("com.apple.quicktime.creationdate") or "").strip()
    if qt:
        dt = _parse_iso(qt)
        if dt is None:
            return qt
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=config.TZ)
        return dt.isoformat(timespec="seconds")
    ct = str(tags.get("creation_time") or "").strip()
    if ct:
        dt = _parse_iso(ct)
        if dt is None:
            return ct
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(config.TZ).isoformat(timespec="seconds")
    return ""


def _fps_of(r_frame_rate: str | None) -> float:
    if not r_frame_rate:
        return 0.0
    try:
        if "/" in r_frame_rate:
            a, b = r_frame_rate.split("/", 1)
            return round(float(a) / float(b), 3) if float(b) else 0.0
        return round(float(r_frame_rate), 3)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- probing
def probe(path: Path) -> dict:
    """Header-only ffprobe of one clip -> the footage.csv fields it determines."""
    r = safeio.run_ffprobe(["-v", "error", "-show_entries", PROBE_ENTRIES, "-of", "json", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(r.stderr or '').strip()[-300:]}")
    try:
        d = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe returned no JSON: {e}") from e
    fmt = d.get("format") or {}
    streams = d.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("no video stream")
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                rotation = 0
    try:
        duration = round(float(fmt.get("duration") or 0.0), 3)
    except ValueError:
        duration = 0.0
    try:
        size = int(fmt.get("size") or 0)
    except ValueError:
        size = 0
    return {
        "size_bytes": size or path.stat().st_size,
        "duration_s": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _fps_of(video.get("r_frame_rate")),
        "codec": video.get("codec_name") or "",
        "color_transfer": (video.get("color_transfer") or "").lower(),
        "rotation": rotation,
        "capture_time": capture_time_from_tags(fmt.get("tags")),
    }


# --------------------------------------------------------------------------- budget
def budget_path() -> Path:
    return config.STATE / BUDGET_FILE


def _day(now: datetime) -> str:
    return _aware(now).astimezone(config.TZ).date().isoformat()


def _aware(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=config.TZ)


def load_budget(now: datetime) -> dict:
    """{date, bytes} for today (ET); a stale file from another day counts as zero."""
    d = safeio.read_json(budget_path(), default=None) or {}
    today = _day(now)
    if d.get("date") != today:
        return {"date": today, "bytes": 0}
    try:
        return {"date": today, "bytes": int(d.get("bytes") or 0)}
    except (TypeError, ValueError):
        return {"date": today, "bytes": 0}


def save_budget(state: dict) -> Path:
    return safeio.write_json(budget_path(), state, overwrite=True)


# --------------------------------------------------------------------------- ledger access
def _ledgers():
    """tools.ledgers when it is importable (another builder's module), else None."""
    try:
        from . import ledgers  # noqa: WPS433
    except ImportError:
        return None
    return ledgers


def ledger_path() -> Path:
    return config.LEDGERS / "footage.csv"


def _existing_rows() -> dict[str, dict]:
    """Current footage.csv keyed by path string. Refuses a non-canonical header."""
    p = ledger_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    with open(p, "r", encoding="utf-8", newline="") as f:
        first = f.readline().rstrip("\r\n")
    header = [c.strip() for c in first.split(",")] if first else []
    if header != FOOTAGE_HEADER:
        raise FootageIndexError(f"footage.csv header is not canonical ({header}); "
                                f"run: py -m tools.ledgers migrate footage --write")
    out: dict[str, dict] = {}
    for r in safeio.read_csv(p):
        key = str(r.get("path") or "").strip()
        if key:
            out[key] = r
    return out


def _store(row: dict, *, update: bool) -> None:
    led = _ledgers()
    if led is not None:
        if update:
            led.upsert("footage", ["path"], row)
        else:
            led.append("footage", row)
        return
    full = {k: row.get(k, "") for k in FOOTAGE_HEADER}
    safeio.append_csv_row(ledger_path(), full, FOOTAGE_HEADER)


# --------------------------------------------------------------------------- main entry
def resolve_folders(names: Iterable[str | Path]) -> list[Path]:
    """--folders arguments: absolute paths or folder names under FOOTAGE_ROOT."""
    out: list[Path] = []
    for n in names:
        p = Path(n)
        if not p.is_absolute():
            p = config.FOOTAGE_ROOT / p
        if not p.is_dir():
            raise FootageIndexError(f"not a folder: {p}")
        if safeio.is_under(p, config.DRIVE_OUT):
            raise FootageIndexError(f"refusing to index our own output folder: {p}")
        out.append(p)
    return out


def index(folders: Sequence[Path] | None, now: datetime,
          max_bytes: int = config.INDEX_BYTES_PER_DAY, dry_run: bool = False, *,
          probe_cap: int | None = None, pause_s: float = 0.0) -> dict:
    """Index every video in `folders` (default: select_folders()) into footage.csv.

    A file already in the ledger with the same size is skipped without being opened. Each
    probed file adds its size (or min(size, probe_cap) when a cap is given) to today's budget;
    the run stops with reason "budget" before the first probe that would push the day past
    max_bytes. dry_run lists what would be probed, opens nothing and writes nothing.

    Returns {indexed, skipped_budget, skipped_existing, errors, bytes_counted, bytes_today,
    stopped, folders, dry_run}.
    """
    now = _aware(now)
    chosen = list(folders) if folders is not None else select_folders()
    existing = _existing_rows()
    budget = load_budget(now)
    counted = 0
    stopped: str | None = None
    indexed: list[str] = []
    skipped_budget: list[str] = []
    skipped_existing: list[str] = []
    errors: list[dict] = []
    stamp = now.astimezone(config.TZ).isoformat(timespec="seconds")

    for folder in chosen:
        folder = Path(folder)
        for clip in list_videos(folder):
            key = str(clip)
            try:
                size = clip.stat().st_size
            except OSError as e:
                errors.append({"path": key, "error": f"stat failed: {e}"})
                continue
            old = existing.get(key)
            if old is not None and str(old.get("size_bytes", "")).strip() == str(size):
                skipped_existing.append(key)
                continue
            if stopped is not None:
                skipped_budget.append(key)
                continue
            cost = size if probe_cap is None else min(size, int(probe_cap))
            if budget["bytes"] + counted + cost > max_bytes:
                stopped = "budget"
                skipped_budget.append(key)
                continue
            counted += cost
            if dry_run:
                indexed.append(key)
                continue
            try:
                facts = probe(clip)
            except Exception as e:  # noqa: BLE001 - one bad file must not stop the index
                errors.append({"path": key, "error": str(e)})
                continue
            row = {
                "path": key,
                **facts,
                "camera": camera_of(clip.name),
                "folder": clip.parent.name,
                "session": folder.name,
                "indexed_at": stamp,
            }
            if old is None:
                for col in PRESERVED_COLS:
                    row[col] = ""
            try:
                _store(row, update=old is not None)
            except Exception as e:  # noqa: BLE001
                errors.append({"path": key, "error": f"ledger write failed: {e}"})
                continue
            existing[key] = {k: str(v) for k, v in row.items()}
            indexed.append(key)
            if pause_s > 0:
                time.sleep(pause_s)

    if not dry_run and counted:
        budget["bytes"] += counted
        save_budget(budget)
    return {
        "indexed": indexed,
        "skipped_budget": skipped_budget,
        "skipped_existing": skipped_existing,
        "errors": errors,
        "bytes_counted": counted,
        "bytes_today": budget["bytes"] + (counted if dry_run else 0),
        "max_bytes": int(max_bytes),
        "stopped": stopped,
        "folders": [str(f) for f in chosen],
        "dry_run": bool(dry_run),
        "ledger": str(ledger_path()),
    }


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Index footage headers into ledgers/footage.csv")
    ap.add_argument("--folders", nargs="*", default=None,
                    help="folder names under the footage root (or absolute paths); default: auto-select")
    ap.add_argument("--dry-run", action="store_true", help="list what would be probed; open and write nothing")
    ap.add_argument("--max-bytes", type=int, default=config.INDEX_BYTES_PER_DAY,
                    help="daily byte budget (default: config.INDEX_BYTES_PER_DAY)")
    ap.add_argument("--probe-cap", type=int, default=None,
                    help="count at most this many bytes per header probe (default: the whole file)")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds to wait between probes (throttle)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    try:
        folders = resolve_folders(a.folders) if a.folders else None
        res = index(folders, now=datetime.now(config.TZ), max_bytes=a.max_bytes, dry_run=a.dry_run,
                    probe_cap=a.probe_cap, pause_s=a.pause)
    except (FootageIndexError, safeio.SafeIOError, OSError) as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"footage_index: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        verb = "would index" if res["dry_run"] else "indexed"
        for p in res["indexed"]:
            print(f"{verb}: {p}")
        for e in res["errors"]:
            print(f"error: {e['path']}: {e['error']}")
        print(f"{verb} {len(res['indexed'])}, unchanged {len(res['skipped_existing'])}, "
              f"budget-skipped {len(res['skipped_budget'])}, errors {len(res['errors'])}, "
              f"bytes counted {res['bytes_counted']} (today {res['bytes_today']} / {res['max_bytes']})"
              + (f", stopped: {res['stopped']}" if res["stopped"] else ""))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
