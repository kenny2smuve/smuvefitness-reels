"""Preflight of the ClaudInk Reel Engine (START_HERE 3.1): is this PC ready to make reels today?

What: runs a list of checks, each reported as {name, ok, level, detail} where level is the
severity when the check fails (blocker: the engine cannot render or write; warn: it can run
but something is degraded or unverified; info: just reported). Writes state/preflight.md and
exits 0 when there is no failed blocker.

Checks: ffmpeg/ffprobe run and their versions; hevc_nvenc (warn -> libx265 fallback) and at
least one HEVC encoder (blocker); zscale + tonemap filters (blocker); libplacebo (info);
Python >= 3.12; the optional packages (warn each, with the pip command); free disk on the
engine root drive >= 30 GB; FOOTAGE_ROOT/"Shop shorts" listable; METRICS_CSV readable and its
freshness in hours (warn > 26 h); the PC clock zone against America/New_York; knowledge/RULES.md;
which ledgers exist (reported only, never created here); templates/broll_station_first.json and
whether it still carries a PLACEHOLDER key; state/engine.lock and its age;
state/render_profile.json and its age; and a write test in cache/ through safeio.

Why: every failure here used to surface halfway through a render at 06:40. Fix what can be
fixed, log the rest, and continue (START_HERE 3.1).

Run:  py -m tools.preflight [--json] [--no-write] [--bench]
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import config, safeio

MIN_FREE_GB = 30.0
METRICS_STALE_HOURS = 26.0
LOCK_STALE_HOURS = 12.0
RENDER_PROFILE_MAX_DAYS = 30
PY_MIN = (3, 12)
PACKAGES: list[tuple[str, str]] = [        # (import name, pip name)
    ("numpy", "numpy"), ("PIL", "pillow"), ("imagehash", "imagehash"),
    ("cv2", "opencv-python"), ("scipy", "scipy"), ("faster_whisper", "faster-whisper"),
    ("tzdata", "tzdata"),
]
PIP_HINT = "py -m pip install --user " + " ".join(p for _, p in PACKAGES)
TEMPLATE_NAME = "broll_station_first.json"
_TS_COL_WORDS = ("timestamp", "time", "date", "pulled", "snapshot", "fetched", "updated", "taken")


def _c(name: str, ok: bool, level: str, detail: str, **extra) -> dict:
    d = {"name": name, "ok": bool(ok), "level": level, "detail": detail}
    d.update(extra)
    return d


# --------------------------------------------------------------------------- ffmpeg
def _first_line(s: str) -> str:
    for line in (s or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _ffmpeg_list(flag: str) -> tuple[bool, set[str], str]:
    """Run ffmpeg <flag> (-encoders / -filters) and return (ran, names, error)."""
    try:
        r = safeio.run_ffmpeg_analysis([flag], timeout=60)
    except (OSError, ValueError) as e:
        return False, set(), str(e)
    except Exception as e:  # subprocess.TimeoutExpired and friends
        return False, set(), f"{type(e).__name__}: {e}"
    names: set[str] = set()
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^\s*[A-Za-z.]{3,6}\s+(\S+)", line)
        if m and not m.group(1).startswith("="):
            names.add(m.group(1))
    if not names:
        return False, names, _first_line(r.stderr) or f"exit {r.returncode}"
    return True, names, ""


def check_ffmpeg() -> dict:
    try:
        r = safeio.run_ffmpeg_analysis(["-version"], timeout=60)
    except Exception as e:
        return _c("ffmpeg", False, "blocker", f"cannot run {config.FFMPEG}: {e}")
    line = _first_line(r.stdout)
    m = re.search(r"ffmpeg version (\S+)", line)
    if r.returncode != 0 or not m:
        return _c("ffmpeg", False, "blocker", f"{config.FFMPEG} exit {r.returncode}: {_first_line(r.stderr) or line}")
    return _c("ffmpeg", True, "blocker", f"{m.group(1)} ({config.FFMPEG})", version=m.group(1))


def check_ffprobe() -> dict:
    try:
        r = safeio.run_ffprobe(["-version"], timeout=60)
    except Exception as e:
        return _c("ffprobe", False, "blocker", f"cannot run {config.FFPROBE}: {e}")
    line = _first_line(r.stdout)
    m = re.search(r"ffprobe version (\S+)", line)
    if r.returncode != 0 or not m:
        return _c("ffprobe", False, "blocker", f"{config.FFPROBE} exit {r.returncode}: {_first_line(r.stderr) or line}")
    return _c("ffprobe", True, "blocker", f"{m.group(1)} ({config.FFPROBE})", version=m.group(1))


def check_encoders() -> list[dict]:
    ran, names, err = _ffmpeg_list("-encoders")
    if not ran:
        return [_c("hevc_nvenc", False, "warn", f"could not list encoders: {err}"),
                _c("hevc_encoder", False, "blocker", f"could not list encoders: {err}")]
    nvenc = "hevc_nvenc" in names
    x265 = "libx265" in names
    out = [_c("hevc_nvenc", nvenc, "warn",
              "hevc_nvenc available (GPU encode)" if nvenc else "hevc_nvenc missing: renders fall back to libx265 (slow)")]
    if nvenc or x265:
        have = " + ".join(n for n, f in (("hevc_nvenc", nvenc), ("libx265", x265)) if f)
        out.append(_c("hevc_encoder", True, "blocker", f"HEVC encoder: {have}"))
    else:
        out.append(_c("hevc_encoder", False, "blocker",
                      "neither hevc_nvenc nor libx265: install the ffmpeg full build"))
    out.append(_c("h264_nvenc", "h264_nvenc" in names, "info",
                  "h264_nvenc available (fast previews)" if "h264_nvenc" in names else "h264_nvenc missing: previews use libx264"))
    return out


def check_filters() -> list[dict]:
    ran, names, err = _ffmpeg_list("-filters")
    if not ran:
        return [_c("filters_zscale_tonemap", False, "blocker", f"could not list filters: {err}")]
    missing = [f for f in ("zscale", "tonemap") if f not in names]
    out = [_c("filters_zscale_tonemap", not missing, "blocker",
              "zscale + tonemap present" if not missing else f"missing filter(s): {', '.join(missing)} (HLG->SDR chain needs the full build)")]
    out.append(_c("libplacebo", "libplacebo" in names, "info",
                  "libplacebo present (GPU tonemap)" if "libplacebo" in names else "libplacebo missing: HLG->SDR uses the zscale/tonemap CPU chain"))
    return out


# --------------------------------------------------------------------------- python
def check_python(version: tuple[int, int, int] | None = None) -> dict:
    v = version or sys.version_info[:3]
    ok = tuple(v[:2]) >= PY_MIN
    s = ".".join(map(str, v))
    return _c("python", ok, "blocker",
              f"Python {s}" if ok else f"Python {s} < {PY_MIN[0]}.{PY_MIN[1]}: winget install -e --id Python.Python.3.12")


def check_packages(packages: Sequence[tuple[str, str]] = PACKAGES) -> list[dict]:
    out = []
    for mod, pip in packages:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            out.append(_c(f"pkg_{mod}", True, "warn", f"{pip} {ver}".strip()))
        except Exception as e:   # ImportError, or a broken binary wheel raising anything
            out.append(_c(f"pkg_{mod}", False, "warn", f"{pip} not importable ({type(e).__name__}): py -m pip install --user {pip}"))
    return out


# --------------------------------------------------------------------------- disk / folders
def _existing_parent(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def check_disk(root: Path | None = None, min_free_gb: float = MIN_FREE_GB) -> dict:
    root = _existing_parent(Path(root or config.ENGINE_ROOT))
    try:
        u = shutil.disk_usage(root)
    except OSError as e:
        return _c("disk_free", False, "warn", f"cannot read disk usage of {root}: {e}")
    free_gb = u.free / 1024 ** 3
    ok = free_gb >= min_free_gb
    return _c("disk_free", ok, "warn",
              f"{free_gb:.1f} GB free on {root.anchor or root}" + ("" if ok else f" (< {min_free_gb:.0f} GB: run clean_cache, move old renders)"),
              free_gb=round(free_gb, 1))


def check_shop_shorts(folder: Path | None = None) -> dict:
    folder = Path(folder or (config.FOOTAGE_ROOT / "Shop shorts"))
    try:
        n = sum(1 for _ in folder.iterdir())
    except OSError as e:
        return _c("shop_shorts", False, "warn", f"{folder} not listable ({type(e).__name__}): is Drive for desktop running and the folder shared?")
    return _c("shop_shorts", True, "warn", f"{folder} listable ({n} entries)", entries=n)


# --------------------------------------------------------------------------- metrics freshness
def _parse_ts(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s or not re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return None
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=config.UTC)      # the CSV's timestamps are UTC
    return d


def metrics_freshness(path: Path, now: datetime) -> tuple[float | None, str]:
    """Hours since the newest timestamp-like value in the CSV, or (None, reason).
    Falls back to the file's mtime when no column parses."""
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8", newline="", errors="replace") as f:
            r = csv.DictReader(f)
            cols = [c for c in (r.fieldnames or []) if c and any(w in c.lower() for w in _TS_COL_WORDS)]
            newest: datetime | None = None
            rows = 0
            for row in r:
                rows += 1
                for c in cols:
                    d = _parse_ts(row.get(c) or "")
                    if d and (newest is None or d > newest):
                        newest = d
    except OSError as e:
        return None, f"unreadable: {e}"
    if newest is None:
        try:
            mt = datetime.fromtimestamp(p.stat().st_mtime, tz=config.TZ)
        except OSError as e:
            return None, f"unreadable: {e}"
        hours = (now - mt).total_seconds() / 3600
        return hours, f"no timestamp column parsed; file mtime {mt.isoformat(timespec='minutes')} ({rows} rows)"
    hours = (now - newest).total_seconds() / 3600
    return hours, f"newest timestamp {newest.astimezone(config.TZ).isoformat(timespec='minutes')} ({rows} rows, columns {', '.join(cols)})"


def check_metrics(path: Path | None = None, now: datetime | None = None,
                  stale_hours: float = METRICS_STALE_HOURS) -> dict:
    path = Path(path or config.METRICS_CSV)
    now = now or datetime.now(config.TZ)
    if not path.is_file():
        return _c("metrics_csv", False, "warn", f"{path} missing: the hourly insights task has not written it")
    hours, why = metrics_freshness(path, now)
    if hours is None:
        return _c("metrics_csv", False, "warn", f"{path} {why}")
    ok = hours <= stale_hours
    return _c("metrics_csv", ok, "warn",
              f"{hours:.1f} h old ({why})" + ("" if ok else f": older than {stale_hours:.0f} h, is the cloud task running?"),
              hours=round(hours, 2))


# --------------------------------------------------------------------------- clock
def clock_offset_matches(now: datetime, local_offset: timedelta | None = None) -> tuple[bool, str]:
    """Compare the PC's local UTC offset with America/New_York at the same instant.
    `now` must be timezone-aware; `local_offset` defaults to the PC clock's current offset."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    expected = now.astimezone(config.TZ).utcoffset()
    local = local_offset if local_offset is not None else datetime.now().astimezone().utcoffset()
    ok = local == expected
    fmt = lambda td: f"{'+' if td >= timedelta(0) else '-'}{abs(td.seconds // 3600 + td.days * 24):02d}:{(abs(td).seconds // 60) % 60:02d}"  # noqa: E731
    return ok, f"PC clock {fmt(local)}, {config.TZ_NAME} {fmt(expected)}"


def check_clock(now: datetime | None = None, local_offset: timedelta | None = None) -> dict:
    now = now or datetime.now(config.TZ)
    ok, detail = clock_offset_matches(now, local_offset)
    return _c("clock_zone", ok, "warn",
              detail + ("" if ok else ": set Windows to Eastern Time; the slot times assume it"))


# --------------------------------------------------------------------------- files on disk
def check_rules_md(path: Path | None = None) -> dict:
    p = Path(path or (config.KNOWLEDGE / "RULES.md"))
    ok = p.is_file()
    return _c("rules_md", ok, "warn", f"{p} present" if ok else f"{p} missing: format rules cannot be verified")


def check_ledgers() -> dict:
    try:
        from . import ledgers
    except ImportError as e:
        return _c("ledgers", False, "warn", f"tools.ledgers not importable: {e}")
    pres = ledgers.present()
    missing = [n for n, ok in pres.items() if not ok]
    return _c("ledgers", not missing, "warn",
              "all ledgers present" if not missing else f"missing: {', '.join(missing)} (py -m tools.ledgers ensure)",
              missing=missing)


def _has_placeholder(obj) -> bool:
    if isinstance(obj, dict):
        return any(str(k) == "PLACEHOLDER" or _has_placeholder(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_placeholder(v) for v in obj)
    return isinstance(obj, str) and obj.strip() == "PLACEHOLDER"


def check_template(path: Path | None = None) -> list[dict]:
    p = Path(path or (config.TEMPLATES / TEMPLATE_NAME))
    if not p.is_file():
        return [_c("template_broll", False, "warn", f"{p} missing: B-roll shorts cannot be built")]
    try:
        obj = json.loads(safeio.read_text(p))
    except (OSError, ValueError) as e:
        return [_c("template_broll", False, "warn", f"{p} unreadable JSON: {e}")]
    out = [_c("template_broll", True, "warn", f"{p} present")]
    placeholder = _has_placeholder(obj) or "PLACEHOLDER" in safeio.read_text(p)
    out.append(_c("template_placeholder", not placeholder, "warn",
                  "template carries no PLACEHOLDER key" if not placeholder else
                  "!!! TEMPLATE STILL CARRIES THE PLACEHOLDER KEY: its timings are NOT his approved B-roll method. "
                  "Fill it from RULES.md section 2 before building any B-roll short !!!"))
    return out


def _read_lock(p: Path) -> dict:
    try:
        txt = safeio.read_text(p)
    except OSError:
        return {}
    try:
        obj = json.loads(txt)
        return obj if isinstance(obj, dict) else {"raw": txt.strip()}
    except ValueError:
        return {"raw": txt.strip()[:200]}


def check_engine_lock(path: Path | None = None, now: datetime | None = None,
                      stale_hours: float = LOCK_STALE_HOURS) -> dict:
    p = Path(path or (config.STATE / "engine.lock"))
    now = now or datetime.now(config.TZ)
    if not p.exists():
        return _c("engine_lock", True, "info", "no engine.lock: no other session is running")
    info = _read_lock(p)
    started = None
    for k in ("start", "started", "start_time", "started_at", "time"):
        if info.get(k):
            started = _parse_ts(str(info[k]))
            if started:
                break
    if started is None:
        try:
            started = datetime.fromtimestamp(p.stat().st_mtime, tz=config.TZ)
        except OSError:
            started = now
    age_min = (now - started).total_seconds() / 60
    owner = info.get("owner", "unknown")
    stale = age_min > stale_hours * 60
    return _c("engine_lock", not stale, "warn" if stale else "info",
              f"engine.lock held by {owner} for {age_min:.0f} min" + (" (stale? the daily run waits 20 min then delivers from HOLD)" if stale else ""),
              age_minutes=round(age_min, 1), owner=owner)


def check_render_profile(path: Path | None = None, now: datetime | None = None,
                         max_days: int = RENDER_PROFILE_MAX_DAYS) -> dict:
    p = Path(path or (config.STATE / "render_profile.json"))
    now = now or datetime.now(config.TZ)
    hint = "run py -m tools.render_story bench (or py -m tools.preflight --bench)"
    if not p.is_file():
        return _c("render_profile", False, "warn", f"{p} missing: {hint}")
    try:
        obj = json.loads(safeio.read_text(p))
    except (OSError, ValueError) as e:
        return _c("render_profile", False, "warn", f"{p} unreadable: {e}; {hint}")
    written = None
    for k in ("generated_et", "generated", "written", "bench_time", "time"):
        if isinstance(obj, dict) and obj.get(k):
            written = _parse_ts(str(obj[k]))
            if written:
                break
    if written is None:
        written = datetime.fromtimestamp(p.stat().st_mtime, tz=config.TZ)
    age_days = (now - written).total_seconds() / 86400
    ok = age_days <= max_days
    chosen = obj.get("chosen", "?") if isinstance(obj, dict) else "?"
    return _c("render_profile", ok, "warn",
              f"{age_days:.0f} days old, chosen path {chosen}" + ("" if ok else f" (> {max_days} days): {hint}"),
              age_days=round(age_days, 1))


def check_cache_write(cache: Path | None = None) -> dict:
    cache = Path(cache or config.CACHE)
    p = cache / f".preflight-write-{uuid.uuid4().hex[:8]}.tmp"
    try:
        safeio.write_text(p, "preflight\n")
        ok = p.is_file()
        removed = safeio.remove_cache_file(p)
    except Exception as e:
        return _c("cache_write", False, "blocker", f"cannot write in {cache}: {type(e).__name__}: {e}")
    if not ok or not removed or p.exists():
        return _c("cache_write", False, "blocker", f"write/delete round trip failed in {cache}")
    return _c("cache_write", True, "blocker", f"write + delete OK in {cache}")


# --------------------------------------------------------------------------- bench (optional)
def maybe_bench(now: datetime) -> dict:
    """Run tools.render_story bench when the profile is missing/stale. Lazy import: the module
    belongs to another builder and may be absent."""
    prof = check_render_profile(now=now)
    if prof["ok"]:
        return _c("bench", True, "info", "render profile fresh, bench skipped")
    try:
        from . import render_story  # noqa: WPS433
    except ImportError as e:
        return _c("bench", False, "warn", f"tools.render_story not available ({e}); profile not refreshed")
    fn = getattr(render_story, "bench", None)
    if fn is None:
        return _c("bench", False, "warn", "tools.render_story has no bench(); profile not refreshed")
    try:
        res = fn()
    except Exception as e:
        return _c("bench", False, "warn", f"bench failed: {type(e).__name__}: {e}")
    chosen = res.get("chosen", "?") if isinstance(res, dict) else "?"
    return _c("bench", True, "info", f"bench done, chosen path {chosen}")


# --------------------------------------------------------------------------- run / report
def all_checks(now: datetime, *, bench: bool = False) -> list[dict]:
    checks: list[dict] = []
    checks.append(check_ffmpeg())
    checks.append(check_ffprobe())
    checks.extend(check_encoders())
    checks.extend(check_filters())
    checks.append(check_python())
    checks.extend(check_packages())
    checks.append(check_disk())
    checks.append(check_shop_shorts())
    checks.append(check_metrics(now=now))
    checks.append(check_clock(now=now))
    checks.append(check_rules_md())
    checks.append(check_ledgers())
    checks.extend(check_template())
    checks.append(check_engine_lock(now=now))
    checks.append(check_render_profile(now=now))
    checks.append(check_cache_write())
    if bench:
        checks.append(maybe_bench(now))
    return checks


def summarize(checks: list[dict], now: datetime) -> dict:
    blockers = [c for c in checks if not c["ok"] and c["level"] == "blocker"]
    warns = [c for c in checks if not c["ok"] and c["level"] == "warn"]
    return {
        "ok": not blockers,
        "generated_et": now.astimezone(config.TZ).isoformat(timespec="seconds"),
        "engine_root": str(config.ENGINE_ROOT),
        "blockers": len(blockers),
        "warnings": len(warns),
        "checks": checks,
    }


def to_markdown(summary: dict) -> str:
    lines = ["# Preflight", "",
             f"- generated: {summary['generated_et']} ({config.TZ_NAME})",
             f"- engine root: {summary['engine_root']}",
             f"- result: {'READY' if summary['ok'] else 'BLOCKED'} "
             f"({summary['blockers']} blocker(s), {summary['warnings']} warning(s))", ""]
    loud = [c for c in summary["checks"] if not c["ok"] and c["detail"].startswith("!!!")]
    for c in loud:
        lines += [f"**{c['detail']}**", ""]
    lines += ["| check | ok | level | detail |", "|---|---|---|---|"]
    for c in summary["checks"]:
        detail = c["detail"].replace("|", "\\|")
        lines.append(f"| {c['name']} | {'yes' if c['ok'] else 'NO'} | {c['level']} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def run(now: datetime | None = None, *, write: bool = True, bench: bool = False,
        checks: Callable[[datetime], list[dict]] | None = None) -> dict:
    """Run every check, write state/preflight.md (overwrite allowed in state/), return the summary."""
    now = now or datetime.now(config.TZ)
    items = checks(now) if checks else all_checks(now, bench=bench)
    summary = summarize(items, now)
    if write:
        out = config.STATE / "preflight.md"
        try:
            safeio.mkdir(config.STATE)
            safeio.write_text(out, to_markdown(summary), overwrite=True)
            summary["report"] = str(out)
        except (OSError, safeio.SafeIOError) as e:
            summary["checks"].append(_c("report_write", False, "warn", f"could not write {out}: {e}"))
            summary["warnings"] += 1
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="py -m tools.preflight", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--no-write", action="store_true", help="do not write state/preflight.md")
    ap.add_argument("--bench", action="store_true",
                    help="run tools.render_story bench when state/render_profile.json is missing or older than 30 days")
    a = ap.parse_args(argv)
    summary = run(write=not a.no_write, bench=a.bench)
    if a.json:
        print(json.dumps(summary, default=str))
    else:
        for c in summary["checks"]:
            flag = "ok  " if c["ok"] else {"blocker": "FAIL", "warn": "warn", "info": "info"}[c["level"]]
            print(f"{flag} {c['name']}: {c['detail']}")
        print(f"preflight: {'READY' if summary['ok'] else 'BLOCKED'} "
              f"({summary['blockers']} blocker(s), {summary['warnings']} warning(s))"
              + (f" -> {summary['report']}" if summary.get("report") else ""))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
