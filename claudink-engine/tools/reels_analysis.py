"""Metrics import and analysis of his reels (START_HERE 3.7 and 8; WEAK_POINTS W1, W2, W12, W15).

Why: the audit's numbers came from reels_history.csv with the latest snapshot per reel, mixed
time zones and a trial column that is wrong for 274 of 275 reels. The engine needs one honest
table (data/reels.csv) where the hit is "5K+ views within 72h", every time is America/New_York,
and every claim in WEAK_POINTS.md can be re-computed from the same rows (register/data-check.md).

What:
- ``load_history(csv_path)`` reads the Porter export with case-insensitive alias mapping of the
  column names (views|plays|..., timestamp|posted|..., snapshot|pulled_at|...). The trial column is
  read and ignored (W15). UTC stamps become aware datetimes; posted_et is the ET conversion.
  avg watch values whose median is > 300 are taken as milliseconds and divided by 1000 (flagged).
  Without a snapshot column the file order per media_id is the snapshot order (flagged).
- ``import_latest(csv_path, out, now)`` writes data/reels.csv: one row per media_id with the
  latest snapshot, the shortcode, posted_et, hours_live, views_24h and views_72h (the last
  snapshot at or before posted + 24h / 72h, "" when that window has not closed or has no
  snapshot), views_72h_source, reach/shares/saves/avg_watch of that 72h snapshot, and is_hit
  (yes/no, "" while unknown: a young reel is not a miss). A reel whose rows carry an unreadable
  snapshot stamp keeps the readable ones (snapshot_flag says what was dropped). data/reels.csv is
  replaced atomically without a ledger backup (it is regenerated from the original every day).
- ``update_delivery_results(reels_rows, deliveries_csv, now)`` (daily-run step 4, section 8
  Results): joins deliveries.csv to the reels rows on posted_shortcode and fills views_24h,
  views_72h, shares_72h, saves_72h, avg_watch_72h (72h columns stay "" while the window is open).
- analysis over the imported rows: hit_rate_by_month, alone_vs_burst (burst = 3+ other posts
  within +/-60 min, a rolling window, see BURST_DEFINITION), watch_buckets, by_hour_et,
  by_weekday, engagement_per_1k (shares/saves per 1K medians), zero_post_days, posts_per_day;
  ``report`` writes data/analysis/summary.md and ``data_check`` writes register/data-check.md
  marking W1, W2, W3 and W12 CONFIRMED / REFUTED / CANNOT with the recomputed numbers (W3 over
  the audit window 10/15 11:29 .. 10/18, current zero-post days reported in the note).
- follows: "follows: n/a" unless a followers file (date,followers) is given; then follows/day is
  the day-over-day difference, labelled an estimate (section 8).

Every time stamp is passed in (``now``); nothing here prints. CLI:
  py -m tools.reels_analysis import [--csv path] [--now ...] [--out data/reels.csv] [--json]
  py -m tools.reels_analysis results [--reels data/reels.csv] [--deliveries ledgers/deliveries.csv]
  py -m tools.reels_analysis report [--since 2026-08-01] [--now ...] [--followers f.csv] [--json]
  py -m tools.reels_analysis data-check [--now ...] [--json]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from . import config
from . import safeio

# --------------------------------------------------------------------------- column aliases
ALIASES: dict[str, tuple[str, ...]] = {
    "media_id": ("media_id", "id", "ig_id", "media", "reel_id", "post_id"),
    "link": ("link", "permalink", "url", "post_url", "reel_url"),
    "caption": ("caption", "text", "description"),
    "posted_utc": ("timestamp", "posted", "created_time", "post_time", "date_posted", "posted_at",
                   "created", "publish_time", "published_at", "posted_utc"),
    "snapshot_utc": ("snapshot", "pulled_at", "fetched_at", "snapshot_ts", "row_date", "as_of",
                     "snapshot_utc", "snapshot_time", "pulled", "fetched", "updated_at"),
    "views": ("views", "plays", "video_views", "play_count", "ig_reels_video_views", "views_count",
              "reel_views"),
    "reach": ("reach", "ig_reels_reach", "accounts_reached"),
    "avg_watch_s": ("avg_watch_s", "avg_watch", "avg_watch_time", "average_watch_time",
                    "ig_reels_avg_watch_time", "avg_watch_time_s", "average_watch_s"),
    "saves": ("saves", "saved", "save_count"),
    "shares": ("shares", "share_count", "sends"),
    "likes": ("likes", "like_count"),
    "comments": ("comments", "comment_count"),
    "trial": ("trial", "is_trial", "trial_reel"),
    "duration_s": ("duration_s", "duration", "video_duration", "length_s", "length"),
}
NUMERIC = ("views", "reach", "avg_watch_s", "saves", "shares", "likes", "comments", "duration_s")
WATCH_MS_THRESHOLD = 300.0           # no reel averages 300 s of watch; above this it is ms
REELS_HEADER = ["media_id", "shortcode", "link", "posted_utc", "posted_et", "snapshot_utc",
                "snapshots", "hours_live", "views", "reach", "avg_watch_s", "saves", "shares",
                "likes", "comments", "duration_s", "views_24h", "views_72h", "views_72h_source",
                "reach_72h", "shares_72h", "saves_72h", "avg_watch_72h", "is_hit", "snapshot_flag",
                "caption"]
# the 72h-window columns of ledgers/deliveries.csv and the data/reels.csv column each comes from
RESULT_COLUMNS = {"views_24h": "views_24h", "views_72h": "views_72h", "shares_72h": "shares_72h",
                  "saves_72h": "saves_72h", "avg_watch_72h": "avg_watch_72h"}
DELIVERIES_HEADER = ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2",
                     "caption", "status", "posted_shortcode", "views_24h", "views_72h",
                     "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"]
WATCH_BUCKETS = (("<8", 0.0, 8.0), ("8-17", 8.0, 17.0), ("17-25", 17.0, 25.0), (">=25", 25.0, None))
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
BURST_WINDOW = timedelta(minutes=60)     # W2: "posted with 3 or more others inside the same hour"
BURST_OTHERS = 3                         # a post is burst with >= 3 others within +/- 60 min
_SHORTCODE_RE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                 "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S",
                 "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z", "%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d")
EPOCH_MIN, EPOCH_MAX = 1e8, 1e13         # a bare number is an epoch (s or ms) only in this range


def _norm_col(name: str) -> str:
    s = str(name or "").strip().lstrip("\ufeff").lower()
    s = re.sub(r"[\s\-./]+", "_", s)
    return s.strip("_")


def map_columns(header: Sequence[str]) -> dict[str, str | None]:
    """canonical name -> actual column (None when absent). First alias in order wins."""
    norm = {_norm_col(h): h for h in header if h is not None}
    out: dict[str, str | None] = {}
    for canon, aliases in ALIASES.items():
        hit = None
        for a in aliases:
            if a in norm:
                hit = norm[a]
                break
        out[canon] = hit
    return out


# --------------------------------------------------------------------------- parsing
def parse_utc(v) -> datetime | None:
    """Any reasonable stamp -> aware UTC datetime (naive = UTC, numbers = epoch s or ms)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=config.UTC)).astimezone(config.UTC)
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        x = float(s)
        if EPOCH_MIN < x < EPOCH_MAX:
            if x > 1e11:
                x /= 1000.0
            return datetime.fromtimestamp(x, tz=config.UTC)
        # a digit string outside the epoch range (20261019, 202610191130) is a compact date
    s2 = s.replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.UTC)
    return dt.astimezone(config.UTC)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in ("na", "n/a", "none", "null", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int_or_blank(v) -> str:
    x = _num(v)
    if x is None:
        return ""
    return str(int(round(x))) if float(x).is_integer() else f"{x:.3f}".rstrip("0").rstrip(".")


def _fmt(x: float | None, nd: int = 3) -> str:
    if x is None:
        return ""
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.{nd}f}".rstrip("0").rstrip(".")


def shortcode_from_link(link: str | None) -> str:
    m = _SHORTCODE_RE.search(str(link or ""))
    return m.group(1) if m else ""


def to_et(dt: datetime | None) -> datetime | None:
    return dt.astimezone(config.TZ) if dt else None


def _read_raw(csv_path: Path) -> tuple[list[str], list[dict]]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return header, rows


# --------------------------------------------------------------------------- load
def load_history(csv_path: Path | str) -> dict:
    """{rows, mapping, flags, columns}: every row of reels_history.csv normalised.

    rows: media_id, link, caption, posted_utc (aware), posted_et (aware), snapshot_utc (aware
    or None), order (file order), views, reach, avg_watch_s, saves, shares, likes, comments,
    duration_s (floats or None). mapping: canonical -> column, with "notes" (trial ignored).
    """
    p = Path(csv_path)
    header, raw = _read_raw(p)
    mapping = map_columns(header)
    flags: list[str] = []
    notes: dict[str, str] = {}
    if mapping.get("trial"):
        notes["trial"] = (f"column {mapping['trial']!r} read but ignored: unreliable "
                          "(flags 274/275 reels as trial, W15)")
    if not mapping.get("media_id"):
        flags.append("no media_id column; rows keyed by link")
    if not mapping.get("snapshot_utc"):
        flags.append("no snapshot column: file row order per media_id is the snapshot order, "
                     "the last row is the latest")
    for canon in ("views", "posted_utc"):
        if not mapping.get(canon):
            flags.append(f"missing column for {canon}")

    rows: list[dict] = []
    for i, r in enumerate(raw):
        g = lambda k: r.get(mapping[k]) if mapping.get(k) else None   # noqa: E731
        mid = str(g("media_id") or "").strip() or shortcode_from_link(g("link")) or str(g("link") or "").strip()
        if not mid:
            continue
        row = {
            "media_id": mid, "link": str(g("link") or "").strip(),
            "caption": str(g("caption") or ""), "order": i,
            "posted_utc": parse_utc(g("posted_utc")), "snapshot_utc": parse_utc(g("snapshot_utc")),
        }
        row["posted_et"] = to_et(row["posted_utc"])
        for k in NUMERIC:
            row[k] = _num(g(k))
        rows.append(row)

    # avg watch in ms? decide per reel (a reel never averages > 300 s) and globally
    by_id: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_id[row["media_id"]].append(row)
    all_watch = [row["avg_watch_s"] for row in rows if row["avg_watch_s"] is not None]
    global_ms = bool(all_watch) and statistics.median(all_watch) > WATCH_MS_THRESHOLD
    ms_reels: list[str] = []
    for mid, grp in by_id.items():
        vals = [x["avg_watch_s"] for x in grp if x["avg_watch_s"] is not None]
        if not vals:
            continue
        if global_ms or statistics.median(vals) > WATCH_MS_THRESHOLD:
            ms_reels.append(mid)
            for x in grp:
                if x["avg_watch_s"] is not None:
                    x["avg_watch_s"] = x["avg_watch_s"] / 1000.0
    if global_ms:
        flags.append("avg_watch column median > 300: values taken as milliseconds and divided by 1000")
    elif ms_reels:
        flags.append(f"avg_watch in milliseconds for {len(ms_reels)} reel(s) (median > 300), "
                     f"divided by 1000: {', '.join(ms_reels[:5])}")
    mapping = dict(mapping)
    mapping["notes"] = notes
    return {"rows": rows, "mapping": mapping, "flags": flags, "columns": header}


# --------------------------------------------------------------------------- import
def _snapshot_at_or_before(snaps: list[dict], deadline: datetime) -> dict | None:
    best = None
    for s in snaps:
        if s["snapshot_utc"] is not None and s["snapshot_utc"] <= deadline:
            best = s
    return best


def latest_per_reel(history: dict, now: datetime) -> list[dict]:
    """One row per media_id (typed values) from a load_history() result."""
    now_utc = (now if now.tzinfo else now.replace(tzinfo=config.TZ)).astimezone(config.UTC)
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in history["rows"]:
        by_id[r["media_id"]].append(r)
    out: list[dict] = []
    for mid, grp in by_id.items():
        with_snap = [x for x in grp if x["snapshot_utc"] is not None]
        bad = len(grp) - len(with_snap)
        has_snap = bool(with_snap)
        snap_flag = ""
        if has_snap:
            snaps = sorted(with_snap, key=lambda x: (x["snapshot_utc"], x["order"]))
            if bad:
                snap_flag = f"{bad} row(s) without a readable snapshot stamp dropped"
        else:
            snaps = sorted(grp, key=lambda x: x["order"])
            snap_flag = "no snapshot stamps: file order used, latest row = latest"
        last = snaps[-1]
        posted = next((x["posted_utc"] for x in reversed(snaps) if x["posted_utc"]), None)
        posted_et = to_et(posted)
        hours_live = round((now_utc - posted).total_seconds() / 3600.0, 1) if posted else None
        v24 = v72 = None
        src72 = ""
        s72: dict | None = None
        if posted and has_snap:
            if now_utc >= posted + timedelta(hours=24):
                s = _snapshot_at_or_before(snaps, posted + timedelta(hours=24))
                v24 = s["views"] if s else None
            if now_utc >= posted + timedelta(hours=config.HIT_WINDOW_H):
                s = _snapshot_at_or_before(snaps, posted + timedelta(hours=config.HIT_WINDOW_H))
                if s is not None:
                    v72, src72, s72 = s["views"], "snapshot", s
                else:
                    v72, src72, s72 = snaps[0]["views"], "earliest_after", snaps[0]
        elif posted and not has_snap:
            if now_utc >= posted + timedelta(hours=config.HIT_WINDOW_H):
                v72, src72, s72 = last["views"], "latest_no_snapshot", last
        if v72 is not None:
            is_hit = "yes" if v72 >= config.HIT_VIEWS else "no"
        elif last["views"] is not None and last["views"] >= config.HIT_VIEWS and posted and \
                now_utc < posted + timedelta(hours=config.HIT_WINDOW_H):
            is_hit, src72 = "yes", "already_hit"
        else:
            is_hit = ""
        out.append({
            "media_id": mid, "shortcode": shortcode_from_link(last["link"]), "link": last["link"],
            "posted_utc": posted, "posted_et": posted_et, "snapshot_utc": last["snapshot_utc"],
            "snapshots": len(snaps), "hours_live": hours_live,
            "views": last["views"], "reach": last["reach"], "avg_watch_s": last["avg_watch_s"],
            "saves": last["saves"], "shares": last["shares"], "likes": last["likes"],
            "comments": last["comments"], "duration_s": last["duration_s"],
            "views_24h": v24, "views_72h": v72, "views_72h_source": src72,
            "reach_72h": s72["reach"] if s72 else None,
            "shares_72h": s72["shares"] if s72 else None,
            "saves_72h": s72["saves"] if s72 else None,
            "avg_watch_72h": s72["avg_watch_s"] if s72 else None,
            "is_hit": is_hit, "snapshot_flag": snap_flag,
            "caption": last["caption"],
        })
    out.sort(key=lambda r: (r["posted_utc"] or datetime.min.replace(tzinfo=config.UTC), r["media_id"]))
    return out


def _row_to_csv(r: dict) -> dict:
    out = {}
    for k in REELS_HEADER:
        v = r.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat(timespec="seconds")
        elif v is None:
            out[k] = ""
        elif k in NUMERIC or k in ("views_24h", "views_72h", "hours_live", "reach_72h",
                                   "shares_72h", "saves_72h", "avg_watch_72h"):
            out[k] = _int_or_blank(v)
        else:
            out[k] = str(v)
    return out


def write_csv_no_backup(target: Path, rows: Iterable[dict], header: Sequence[str]) -> Path:
    """Replace a CSV under data/ (or any rewritable dir) atomically without the
    state/ledger-backups/ copy that safeio.rewrite_csv keeps: data/reels.csv is regenerated
    from the read-only original every morning, a backup of it is pure growth."""
    import os
    target = Path(target)
    tmp = target.with_name(target.stem + ".tmp.csv")
    with safeio.open_write(tmp, "w", overwrite=True) as f:
        w = csv.DictWriter(f, fieldnames=list(header), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    safeio.assert_writable(target, overwrite=True)
    os.replace(tmp, target)
    return target


def import_latest(csv_path: Path | str | None = None, out: Path | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Write data/reels.csv (latest snapshot per reel + 24h/72h values) and return the rows."""
    src = Path(csv_path) if csv_path else config.METRICS_CSV
    target = Path(out) if out else (config.DATA / "reels.csv")
    now = now or datetime.now(config.TZ)
    hist = load_history(src)
    rows = latest_per_reel(hist, now)
    csv_rows = [_row_to_csv(r) for r in rows]
    write_csv_no_backup(target, csv_rows, REELS_HEADER)
    flags = list(hist["flags"])
    dropped = sum(1 for r in rows if r.get("snapshot_flag", "").endswith("dropped"))
    if dropped and hist["mapping"].get("snapshot_utc"):
        flags.append(f"{dropped} reel(s) had rows without a readable snapshot stamp (dropped; "
                     "see snapshot_flag)")
    meta = {"source": str(src), "imported_et": now.astimezone(config.TZ).isoformat(timespec="seconds"),
            "reels": len(rows), "snapshots": len(hist["rows"]), "mapping": hist["mapping"],
            "flags": flags}
    safeio.write_json(target.with_name(target.stem + ".import.json"), meta, overwrite=True)
    return rows


# --------------------------------------------------------------------------- results (daily-run step 4)
def update_delivery_results(reels_rows: Sequence[dict], deliveries_csv: Path | None = None,
                            now: datetime | None = None) -> dict:
    """Fill views_24h, views_72h, shares_72h, saves_72h, avg_watch_72h of every deliveries.csv
    row that has a posted_shortcode and an empty 72h column, from the imported reels rows
    (joined on shortcode; the 72h values come from the last snapshot at or before posted + 72h,
    "" while that window is still open). Rewrites the ledger through safeio.rewrite_csv.
    Returns {updated, skipped, pending, missing, rows}: pending = window still open,
    missing = shortcode not in the reels rows."""
    p = Path(deliveries_csv) if deliveries_csv else (config.LEDGERS / "deliveries.csv")
    rows = safeio.read_csv(p)
    if not rows:
        return {"updated": 0, "skipped": 0, "pending": 0, "missing": 0, "rows": [], "path": str(p)}
    by_code: dict[str, dict] = {}
    for r in reels_rows:
        code = str(r.get("shortcode") or "").strip()
        if code:
            by_code[code] = r
    header = list(rows[0].keys())
    for col in DELIVERIES_HEADER:
        if col not in header:
            header.append(col)
    updated = skipped = pending = missing = 0
    touched: list[str] = []
    for r in rows:
        code = (r.get("posted_shortcode") or "").strip()
        if not code:
            skipped += 1
            continue
        if (r.get("views_72h") or "").strip():
            skipped += 1
            continue
        reel = by_code.get(code)
        if reel is None:
            missing += 1
            continue
        if reel.get("views_72h") in (None, ""):
            v24 = reel.get("views_24h")
            if v24 not in (None, "") and not (r.get("views_24h") or "").strip():
                r["views_24h"] = _int_or_blank(v24)
                touched.append(r.get("id") or code)
            pending += 1
            continue
        for ledger_col, reel_col in RESULT_COLUMNS.items():
            v = reel.get(reel_col)
            if v not in (None, ""):
                r[ledger_col] = _int_or_blank(v)
        updated += 1
        touched.append(r.get("id") or code)
    if touched:
        safeio.rewrite_csv(p, rows, header)
    return {"updated": updated, "skipped": skipped, "pending": pending, "missing": missing,
            "rows": touched, "path": str(p)}


def load_reels(path: Path | None = None) -> list[dict]:
    """Rows of data/reels.csv (strings; the analysis functions parse them)."""
    return safeio.read_csv(path or (config.DATA / "reels.csv"))


# --------------------------------------------------------------------------- row access
def _posted_et(r: dict) -> datetime | None:
    v = r.get("posted_et")
    if isinstance(v, datetime):
        return v.astimezone(config.TZ)
    dt = parse_utc(v) if v else None
    if dt is None:
        u = r.get("posted_utc")
        dt = u if isinstance(u, datetime) else (parse_utc(u) if u else None)
    return dt.astimezone(config.TZ) if dt else None


def _hit(r: dict) -> bool | None:
    v = r.get("is_hit")
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s in ("yes", "true", "1"):
        return True
    if s in ("no", "false", "0"):
        return False
    return None


def _views(r: dict) -> float | None:
    return _num(r.get("views"))


def _median(xs: Iterable[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(statistics.median(xs)) if xs else None


def group_stats(rows: Sequence[dict]) -> dict:
    """{n, median_views, hits, known, unknown, hit_rate}; median over the latest views."""
    hits = sum(1 for r in rows if _hit(r) is True)
    known = sum(1 for r in rows if _hit(r) is not None)
    return {"n": len(rows), "median_views": _median(_views(r) for r in rows),
            "hits": hits, "known": known, "unknown": len(rows) - known,
            "hit_rate": round(hits / known, 3) if known else None}


def _since_date(since) -> date | None:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        return since.date()
    if isinstance(since, date):
        return since
    return date.fromisoformat(str(since)[:10])


# --------------------------------------------------------------------------- analysis
def hit_rate_by_month(rows: Sequence[dict], since=None) -> list[dict]:
    s = _since_date(since)
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        p = _posted_et(r)
        if p is None or (s and p.date() < s):
            continue
        by[p.strftime("%Y-%m")].append(r)
    out = []
    for month in sorted(by):
        st = group_stats(by[month])
        out.append({"month": month, "posts": st["n"], "hits": st["hits"], "unknown": st["unknown"],
                    "rate": st["hit_rate"], "median_views": st["median_views"]})
    return out


BURST_DEFINITION = (f"alone = nothing else within {config.MIN_GAP_HOURS:g}h either side; burst = "
                    f"{BURST_OTHERS}+ other posts within +/-{int(BURST_WINDOW.total_seconds() // 60)} "
                    "min (rolling window on the posted times, W2 'with 3 or more others inside the "
                    "same hour'; not clock-hour buckets); other = the rest")


def post_groups(rows: Sequence[dict]) -> dict[str, list[dict]]:
    """alone (nothing else within MIN_GAP_HOURS either side), burst (at least BURST_OTHERS other
    posts within +/- BURST_WINDOW, a rolling window so 10:40/10:50/11:05/11:15 is one burst),
    other. A reel without a posted time goes nowhere. See BURST_DEFINITION."""
    timed = [(r, _posted_et(r)) for r in rows]
    timed = [(r, p) for r, p in timed if p is not None]
    gap = timedelta(hours=config.MIN_GAP_HOURS)
    groups: dict[str, list[dict]] = {"alone": [], "burst": [], "other": []}
    for i, (r, p) in enumerate(timed):
        near_gap = 0
        near_hour = 0
        for j, (_o, q) in enumerate(timed):
            if j == i:
                continue
            delta = abs(q - p)
            if delta <= gap:
                near_gap += 1
            if delta <= BURST_WINDOW:
                near_hour += 1
        if near_gap == 0:
            groups["alone"].append(r)
        elif near_hour >= BURST_OTHERS:
            groups["burst"].append(r)
        else:
            groups["other"].append(r)
    return groups


def alone_vs_burst(rows: Sequence[dict]) -> dict:
    return {k: group_stats(v) for k, v in post_groups(rows).items()}


def watch_buckets(rows: Sequence[dict]) -> dict:
    """Buckets of avg_watch_s with n, median views, hit rate and median duration when known.
    The length confound is flagged: avg watch rises with reel length by construction."""
    buckets = []
    for label, lo, hi in WATCH_BUCKETS:
        grp = []
        for r in rows:
            w = _num(r.get("avg_watch_s"))
            if w is None:
                continue
            if w >= lo and (hi is None or w < hi):
                grp.append(r)
        st = group_stats(grp)
        durs = [_num(r.get("duration_s")) for r in grp]
        st["bucket"] = label
        st["median_duration_s"] = _median(durs)
        buckets.append(st)
    has_dur = any(b["median_duration_s"] is not None for b in buckets)
    flags = ["length confound: average watch time grows with reel length, so a bucket mixes "
             "'held attention' with 'was a longer reel'; compare within one length class"]
    if not has_dur:
        flags.append("no duration column in the data: median duration per bucket unavailable")
    n_watch = sum(b["n"] for b in buckets)
    return {"buckets": buckets, "n_with_watch": n_watch, "flags": flags}


def by_hour_et(rows: Sequence[dict]) -> list[dict]:
    by: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        p = _posted_et(r)
        if p is not None:
            by[p.hour].append(r)
    return [{"hour": h, **group_stats(by[h])} for h in sorted(by)]


def by_weekday(rows: Sequence[dict]) -> list[dict]:
    by: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        p = _posted_et(r)
        if p is not None:
            by[p.weekday()].append(r)
    return [{"weekday": WEEKDAYS[d], **group_stats(by[d])} for d in sorted(by)]


def engagement_per_1k(rows: Sequence[dict]) -> dict:
    """Median shares and saves per 1K views over reels with views > 0."""
    sh, sv = [], []
    for r in rows:
        v = _views(r)
        if not v or v <= 0:
            continue
        s = _num(r.get("shares"))
        if s is not None:
            sh.append(s / v * 1000.0)
        s = _num(r.get("saves"))
        if s is not None:
            sv.append(s / v * 1000.0)
    return {"shares_per_1k_median": round(_median(sh), 2) if sh else None,
            "saves_per_1k_median": round(_median(sv), 2) if sv else None,
            "n_shares": len(sh), "n_saves": len(sv)}


def posts_per_day(rows: Sequence[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        p = _posted_et(r)
        if p is not None:
            out[p.date().isoformat()] += 1
    return dict(sorted(out.items()))


def zero_post_days(rows: Sequence[dict], since, until) -> list[date]:
    s, u = _since_date(since), _since_date(until)
    if s is None or u is None:
        return []
    have = set(posts_per_day(rows))
    out = []
    d = s
    while d <= u:
        if d.isoformat() not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


def days_with_at_least(rows: Sequence[dict], n: int) -> dict[str, int]:
    return {d: c for d, c in posts_per_day(rows).items() if c >= n}


# --------------------------------------------------------------------------- follows
def follows_estimate(followers_csv: Path | str) -> list[dict]:
    """[{date, followers, follows}] from a date,followers file; follows = day-over-day diff."""
    rows = safeio.read_csv(Path(followers_csv))
    pts = []
    for r in rows:
        cols = {_norm_col(k): v for k, v in r.items() if k}
        d = cols.get("date") or cols.get("day")
        f = _num(cols.get("followers") or cols.get("follower_count") or cols.get("count"))
        if not d or f is None:
            continue
        try:
            pts.append((date.fromisoformat(str(d).strip()[:10]), f))
        except ValueError:
            continue
    pts.sort()
    out = []
    prev = None
    for d, f in pts:
        follows = None if prev is None else int(round(f - prev[1]))
        if prev is not None and (d - prev[0]).days > 1:
            follows = round((f - prev[1]) / (d - prev[0]).days)
        out.append({"date": d.isoformat(), "followers": int(round(f)), "follows": follows,
                    "label": "estimate"})
        prev = (d, f)
    return out


def follows_line(followers_csv: Path | str | None = None) -> str:
    if not followers_csv or not Path(followers_csv).is_file():
        return "follows: n/a"
    est = [e for e in follows_estimate(followers_csv) if e["follows"] is not None]
    if not est:
        return "follows: n/a"
    last = est[-1]
    med = _median(e["follows"] for e in est)
    return (f"follows: ~{last['follows']}/day on {last['date']} (median {_fmt(med)}/day over "
            f"{len(est)} days; estimate by day-matching, not per reel)")


# --------------------------------------------------------------------------- report
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _md_table(header: Sequence[str], body: Iterable[Sequence]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in body:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def report(rows: Sequence[dict], since=None, now: datetime | None = None,
           followers_csv: Path | str | None = None, out: Path | None = None) -> str:
    """Markdown summary written to data/analysis/summary.md (returned as text)."""
    now = (now or datetime.now(config.TZ)).astimezone(config.TZ)
    s = _since_date(since)
    sel = [r for r in rows if _posted_et(r) is not None and (s is None or _posted_et(r).date() >= s)]
    L: list[str] = []
    L.append(f"# Reels summary ({now.date().isoformat()} ET)")
    L.append("")
    L.append(f"Rows: {len(sel)} reels" + (f" since {s.isoformat()}" if s else "") +
             f". Hit = {config.HIT_VIEWS}+ views within {config.HIT_WINDOW_H}h "
             "(unknown = too young or no snapshot). Median views = latest snapshot.")
    L.append("")
    st = group_stats(sel)
    L.append(f"Overall: {st['hits']} hits / {st['known']} known ({_pct(st['hit_rate'])}), "
             f"{st['unknown']} unknown, median views {_fmt(st['median_views'])}.")
    L.append("")
    L.append("## Hit rate by month")
    L.extend(_md_table(["month", "posts", "hits", "unknown", "rate", "median views"],
                       [(m["month"], m["posts"], m["hits"], m["unknown"], _pct(m["rate"]),
                         _fmt(m["median_views"])) for m in hit_rate_by_month(sel)]))
    L.append("")
    L.append("## Alone vs burst (W2)")
    ab = alone_vs_burst(sel)
    L.extend(_md_table(["group", "n", "median views", "hits/known", "rate"],
                       [(k, v["n"], _fmt(v["median_views"]), f"{v['hits']}/{v['known']}",
                         _pct(v["hit_rate"])) for k, v in ab.items()]))
    L.append(BURST_DEFINITION + ".")
    L.append("")
    L.append("## Average watch buckets (W1)")
    wb = watch_buckets(sel)
    L.extend(_md_table(["avg watch s", "n", "median views", "hits/known", "rate", "median duration"],
                       [(b["bucket"], b["n"], _fmt(b["median_views"]), f"{b['hits']}/{b['known']}",
                         _pct(b["hit_rate"]), _fmt(b["median_duration_s"]) or "n/a")
                        for b in wb["buckets"]]))
    for f in wb["flags"]:
        L.append(f"FLAG: {f}")
    L.append("")
    L.append("## By hour (ET)")
    L.extend(_md_table(["hour", "n", "median views", "rate"],
                       [(f"{h['hour']:02d}", h["n"], _fmt(h["median_views"]), _pct(h["hit_rate"]))
                        for h in by_hour_et(sel)]))
    L.append("")
    L.append("## By weekday")
    L.extend(_md_table(["weekday", "n", "median views", "rate"],
                       [(w["weekday"], w["n"], _fmt(w["median_views"]), _pct(w["hit_rate"]))
                        for w in by_weekday(sel)]))
    L.append("")
    e = engagement_per_1k(sel)
    L.append("## Shares and saves per 1K views (medians)")
    L.append(f"shares/1K: {_fmt(e['shares_per_1k_median'])} (n={e['n_shares']}); "
             f"saves/1K: {_fmt(e['saves_per_1k_median'])} (n={e['n_saves']})")
    L.append("")
    L.append("## Cadence (W3, W12)")
    ppd = posts_per_day(sel)
    first = s or (date.fromisoformat(min(ppd)) if ppd else now.date())
    zpd = zero_post_days(sel, first, now.date())
    big = days_with_at_least(sel, 12)
    L.append(f"posts/day: {len(ppd)} active days; zero-post days since {first.isoformat()}: "
             f"{len(zpd)}" + (f" ({', '.join(d.isoformat() for d in zpd[-10:])})" if zpd else ""))
    L.append(f"days with 12+ posts: {len(big)}" +
             (f" ({', '.join(f'{d}: {c}' for d, c in big.items())})" if big else ""))
    L.append("")
    L.append("## Follows")
    L.append(follows_line(followers_csv))
    L.append("")
    text = "\n".join(L) + "\n"
    target = out or (config.DATA / "analysis" / "summary.md")
    safeio.write_text(target, text, overwrite=True)
    return text


# --------------------------------------------------------------------------- data check (WEAK_POINTS)
PCT_TOL = 0.10          # a percentage claim of 20%+ is confirmed within 10 points
PCT_TOL_SMALL_MIN = 0.03     # under 20%: within max(3 points, 25% of the claim)
PCT_TOL_SMALL_REL = 0.25
REL_TOL = 0.25          # a median / count claim is confirmed within 25%
W3_AUDIT_FROM = datetime(2026, 10, 15, 11, 29, tzinfo=config.TZ)   # "nothing posted since 10/15 11:29"
W3_AUDIT_UNTIL = date(2026, 10, 18)                                 # the audit date


def pct_tolerance(claim: float) -> float:
    """10 points for claims of 20%+; max(3 points, 25% relative) under 20%, so a recomputed
    15% does not confirm a '7%' claim."""
    if claim >= 0.20:
        return PCT_TOL
    return max(PCT_TOL_SMALL_MIN, PCT_TOL_SMALL_REL * claim)


def _close_pct(a: float | None, claim: float) -> bool:
    return a is not None and abs(a - claim) <= pct_tolerance(claim)


def _close_rel(a: float | None, claim: float) -> bool:
    return a is not None and claim and abs(a - claim) / claim <= REL_TOL


def _verdict(ok_list: Sequence[bool | None]) -> str:
    if any(x is None for x in ok_list):
        return "CANNOT"
    return "CONFIRMED" if all(ok_list) else "REFUTED"


def data_check(rows: Sequence[dict], now: datetime | None = None, out: Path | None = None) -> str:
    """Recompute the WEAK_POINTS claims W1, W2, W3, W12 and write register/data-check.md."""
    now = (now or datetime.now(config.TZ)).astimezone(config.TZ)
    items: list[tuple[str, str, str, str, str]] = []   # id, claim, recomputed, verdict, note
    has_watch = any(_num(r.get("avg_watch_s")) is not None for r in rows)
    has_time = any(_posted_et(r) is not None for r in rows)
    has_hit = any(_hit(r) is not None for r in rows)

    # W1a: 57% of October reels under 10 s average watch
    octo = [r for r in rows if _posted_et(r) is not None and _posted_et(r).strftime("%Y-%m") == "2026-10"]
    if has_watch and octo:
        w = [_num(r.get("avg_watch_s")) for r in octo]
        w = [x for x in w if x is not None]
        short = [r for r in octo if (_num(r.get("avg_watch_s")) or 0) < 10 and _num(r.get("avg_watch_s")) is not None]
        share = len(short) / len(w) if w else None
        st_short = group_stats(short)
        items.append(("W1a", "Oct: 57% of reels under 10 s avg watch (50/87), 1 of them hit",
                      f"{_pct(share)} ({len(short)}/{len(w)}), {st_short['hits']} hit",
                      _verdict([_close_pct(share, 0.57)]), "share of Oct reels with avg watch < 10 s"))
        long_ = [r for r in octo if (_num(r.get("avg_watch_s")) is not None and _num(r.get("avg_watch_s")) >= 17)]
        st_long = group_stats(long_)
        items.append(("W1b", "Oct: 17 s+ avg watch hit 17% (4/24)",
                      f"{_pct(st_long['hit_rate'])} ({st_long['hits']}/{st_long['known']}, {st_long['unknown']} unknown)",
                      _verdict([_close_pct(st_long["hit_rate"], 0.17) if st_long["known"] else None]),
                      "hit = 5K within 72h here, latest views in the audit"))
    else:
        items.append(("W1a", "Oct: 57% of reels under 10 s avg watch", "n/a", "CANNOT",
                      "avg_watch column missing" if not has_watch else "no October rows"))
        items.append(("W1b", "Oct: 17 s+ avg watch hit 17%", "n/a", "CANNOT",
                      "avg_watch column missing" if not has_watch else "no October rows"))

    # W1c: monthly hit rate 20 / 15 / 7
    months = {m["month"]: m for m in hit_rate_by_month(rows)}
    claims = {"2026-08": 0.20, "2026-09": 0.15, "2026-10": 0.07}
    if has_hit and any(m in months for m in claims):
        parts, oks = [], []
        for m, c in claims.items():
            x = months.get(m)
            if x is None or not (x["posts"] - x["unknown"]):
                parts.append(f"{m}: n/a")
                oks.append(None)
            else:
                parts.append(f"{m}: {_pct(x['rate'])} ({x['hits']}/{x['posts'] - x['unknown']}, {x['unknown']} unknown)")
                oks.append(_close_pct(x["rate"], c))
        direction = None
        rates = [months[m]["rate"] for m in claims if m in months and months[m]["rate"] is not None]
        if len(rates) == 3:
            direction = rates[0] > rates[1] > rates[2]
        items.append(("W1c", "monthly hit rate 20% Aug, 15% Sep, 7% Oct", "; ".join(parts),
                      _verdict(oks), "falling Aug > Sep > Oct: " +
                      ("yes" if direction else "no" if direction is False else "n/a")))
    else:
        items.append(("W1c", "monthly hit rate 20% Aug, 15% Sep, 7% Oct", "n/a", "CANNOT",
                      "is_hit unknown for every row" if not has_hit else "no Aug-Oct rows"))

    # W2: burst median 1356 / 10% vs alone 1908 / 24%
    if has_time and rows:
        ab = alone_vs_burst(rows)
        b, a = ab["burst"], ab["alone"]
        rec = (f"burst n={b['n']} median {_fmt(b['median_views'])} rate {_pct(b['hit_rate'])}; "
               f"alone n={a['n']} median {_fmt(a['median_views'])} rate {_pct(a['hit_rate'])}")
        oks = [_close_rel(b["median_views"], 1356) if b["n"] else None,
               _close_pct(b["hit_rate"], 0.10) if b["known"] else None,
               _close_rel(a["median_views"], 1908) if a["n"] else None,
               _close_pct(a["hit_rate"], 0.24) if a["known"] else None]
        direction = (b["median_views"] is not None and a["median_views"] is not None
                     and b["median_views"] < a["median_views"])
        items.append(("W2", "burst median 1,356 views / 10% hit vs alone 1,908 / 24%", rec,
                      _verdict(oks), "alone beats burst on median views: " + ("yes" if direction else "no")
                      + "; " + BURST_DEFINITION))
    else:
        items.append(("W2", "burst 1,356 / 10% vs alone 1,908 / 24%", "n/a", "CANNOT", "no posted times"))

    # W3: nothing posted 10/15 11:29 .. 10/18 (the audit window); current zero-post days separately
    if has_time and rows:
        times = [_posted_et(r) for r in rows if _posted_et(r) is not None]
        in_window = sorted(t for t in times if W3_AUDIT_FROM < t <= datetime(
            W3_AUDIT_UNTIL.year, W3_AUDIT_UNTIL.month, W3_AUDIT_UNTIL.day, 23, 59, 59, tzinfo=config.TZ))
        zpd_audit = zero_post_days(rows, W3_AUDIT_FROM.date() + timedelta(days=1), W3_AUDIT_UNTIL)
        last_post = max(times, default=None)
        zpd_now = zero_post_days(rows, W3_AUDIT_UNTIL + timedelta(days=1), now.date()) \
            if now.date() > W3_AUDIT_UNTIL else []
        rec = (f"audit window {W3_AUDIT_FROM.strftime('%m/%d %H:%M')}..{W3_AUDIT_UNTIL.isoformat()}: "
               f"{len(in_window)} post(s), {len(zpd_audit)} zero-post day(s) "
               f"({', '.join(d.isoformat() for d in zpd_audit) or 'none'})")
        note = (f"since the audit: {len(zpd_now)} zero-post day(s) "
                f"{W3_AUDIT_UNTIL + timedelta(days=1)}..{now.date().isoformat()}"
                + (f" ({', '.join(d.isoformat() for d in zpd_now[-10:])})" if zpd_now else "")
                + f"; last post {last_post.isoformat(timespec='minutes') if last_post else 'n/a'}; ET dates")
        items.append(("W3", "nothing posted 10/15 11:29 .. 10/18 (zero-post days since 10/15)", rec,
                      "CONFIRMED" if not in_window else "REFUTED", note))
    else:
        items.append(("W3", "zero-post days since 10/15", "n/a", "CANNOT", "no posted times"))

    # W12: days with 12-20 posts
    if has_time and rows:
        big = days_with_at_least(rows, 12)
        ppd = posts_per_day(rows)
        rec = (f"{len(big)} day(s) with 12+ posts" +
               (f": {', '.join(f'{d} ({c})' for d, c in big.items())}" if big else "") +
               f"; max {max(ppd.values()) if ppd else 0}/day")
        items.append(("W12", "some days have 12-20 posts and some have 0", rec,
                      "CONFIRMED" if big else "REFUTED", "posts per ET calendar day"))
    else:
        items.append(("W12", "days with 12+ posts", "n/a", "CANNOT", "no posted times"))

    L = [f"# Data check of WEAK_POINTS.md ({now.date().isoformat()} ET)", "",
         f"Rows: {len(rows)} reels from data/reels.csv. Hit = {config.HIT_VIEWS}+ views within "
         f"{config.HIT_WINDOW_H}h (the audit used latest views). Tolerance: percentages within "
         f"{int(PCT_TOL * 100)} points (claims under 20%: max({int(PCT_TOL_SMALL_MIN * 100)} points, "
         f"{int(PCT_TOL_SMALL_REL * 100)}% of the claim)), medians within {int(REL_TOL * 100)}%.", ""]
    L.extend(_md_table(["item", "claim", "recomputed", "verdict", "note"], items))
    L.append("")
    counts = defaultdict(int)
    for it in items:
        counts[it[3]] += 1
    L.append("Verdicts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    L.append("")
    text = "\n".join(L) + "\n"
    target = out or (config.REGISTER / "data-check.md")
    safeio.write_text(target, text, overwrite=True)
    return text


# --------------------------------------------------------------------------- CLI
def _parse_now(s: str | None) -> datetime:
    if not s:
        return datetime.now(config.TZ)
    dt = datetime.fromisoformat(s)
    return (dt.replace(tzinfo=config.TZ) if dt.tzinfo is None else dt).astimezone(config.TZ)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="py -m tools.reels_analysis",
                                description="Import reels_history.csv and analyse the reels.")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--now", default=None, help="ISO time (default: the clock, ET)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--now", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    si = sub.add_parser("import", parents=[common], help="write data/reels.csv from the history CSV")
    si.add_argument("--csv", type=Path, default=None, help="reels_history.csv (default config)")
    si.add_argument("--out", type=Path, default=None, help="default data/reels.csv")
    sr = sub.add_parser("report", parents=[common], help="write data/analysis/summary.md")
    sr.add_argument("--since", default=None)
    sr.add_argument("--followers", type=Path, default=None, help="date,followers CSV")
    sr.add_argument("--reels", type=Path, default=None, help="default data/reels.csv")
    sd = sub.add_parser("data-check", parents=[common], help="write register/data-check.md")
    sd.add_argument("--reels", type=Path, default=None)
    sx = sub.add_parser("results", parents=[common],
                        help="fill the 24h/72h result columns of ledgers/deliveries.csv (step 4)")
    sx.add_argument("--reels", type=Path, default=None, help="default data/reels.csv")
    sx.add_argument("--deliveries", type=Path, default=None, help="default ledgers/deliveries.csv")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = _parse_now(args.now)
    try:
        if args.cmd == "import":
            rows = import_latest(args.csv, args.out, now)
            hits = sum(1 for r in rows if r["is_hit"] == "yes")
            unknown = sum(1 for r in rows if r["is_hit"] == "")
            if args.json:
                print(json.dumps({"reels": len(rows), "hits": hits, "unknown": unknown,
                                  "out": str(args.out or (config.DATA / "reels.csv"))}))
            else:
                print(f"imported {len(rows)} reels ({hits} hits, {unknown} unknown) -> "
                      f"{args.out or (config.DATA / 'reels.csv')}")
            return 0
        rows = load_reels(args.reels)
        if not rows:
            print("no rows: run import first", file=sys.stderr)
            return 1
        if args.cmd == "results":
            res = update_delivery_results(rows, args.deliveries, now)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"results: {res['updated']} updated, {res['pending']} still inside 72h, "
                      f"{res['missing']} shortcode(s) not in reels.csv, {res['skipped']} skipped "
                      f"-> {res['path']}")
            return 0
        if args.cmd == "report":
            text = report(rows, args.since, now, args.followers)
            if args.json:
                print(json.dumps({"months": hit_rate_by_month(rows, args.since),
                                  "alone_vs_burst": alone_vs_burst(rows),
                                  "watch": watch_buckets(rows),
                                  "engagement": engagement_per_1k(rows),
                                  "follows": follows_line(args.followers)}, indent=2, default=str))
            else:
                print(text, end="")
            return 0
        if args.cmd == "data-check":
            text = data_check(rows, now)
            if args.json:
                verdicts = {}
                for line in text.splitlines():
                    if line.startswith("| W"):
                        cells = [c.strip() for c in line.strip("|").split("|")]
                        verdicts[cells[0]] = {"verdict": cells[3], "recomputed": cells[2]}
                print(json.dumps(verdicts, indent=2))
            else:
                print(text, end="")
            return 0
    except (safeio.SafeIOError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
