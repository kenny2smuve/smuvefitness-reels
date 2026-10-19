"""CSV ledgers of the ClaudInk Reel Engine (SPEC.md "Ledger CSV schemas", START_HERE 3.6).

What: owns the six ledger schemas (header order is the contract), reads and writes them only
through tools.safeio, validates their contents, migrates older column names, and gives the
other tools typed helpers (burned hooks, session rest, piece hours, deliveries per date).

Why: the ledgers are the engine's memory between runs. Everything that gates a reel (hook
burned? session resting? piece hours known? two posts already today?) reads from here, so
one module must own the schemas and the parsing rules instead of every tool re-inventing
them. Dates are ISO YYYY-MM-DD, times are ISO with an offset, empty means unknown.

Run:  py -m tools.ledgers ensure | validate [name] | show <name> [--json] | migrate <name> [--write]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from . import config, safeio

# --------------------------------------------------------------------------- schemas
HEADERS: dict[str, list[str]] = {
    "footage": ["path", "size_bytes", "duration_s", "width", "height", "fps", "codec",
                "color_transfer", "rotation", "capture_time", "camera", "folder", "session",
                "used_as_opener_in", "used_in", "indexed_at"],
    "pieces": ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"],
    "sessions": ["session", "folder", "posts_so_far", "last_post", "rest_until", "status"],
    "hooks": ["hook", "type", "used_on", "reel", "result_views", "status"],
    "captions_used": ["caption", "used_on", "reel"],
    "deliveries": ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2",
                   "caption", "status", "posted_shortcode", "views_24h", "views_72h",
                   "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"],
}
NAMES: tuple[str, ...] = tuple(HEADERS)

# Status vocabularies. Empty is always allowed (= unknown / not set yet).
HOOK_STATUS = ("burned", "retired", "losing_type", "ok")
HOOK_BURNED_STATUS = ("burned", "retired", "losing_type")
SESSION_STATUS = ("new", "active", "resting", "spent")
DELIVERY_STATUS = ("planned", "delivered", "held", "posted", "unposted", "legacy", "dryrun",
                   "cancelled")
# Delivery rows that do not count against the 2-per-day cap (never reached post/<date>/).
DELIVERY_NOT_COUNTED = ("held", "legacy", "dryrun", "cancelled")
DELIVERY_TYPES = ("story", "broll", "th")
YES_NO = ("yes", "no")
CAMERAS = ("canon", "iphone", "meta", "unknown")

# Which columns carry which value kind (used by validate()).
DATE_COLS: dict[str, tuple[str, ...]] = {
    "pieces": ("rest_until",),
    "sessions": ("rest_until",),
    "hooks": ("used_on",),
    "captions_used": ("used_on",),
    "deliveries": ("date",),
}
TIME_COLS: dict[str, tuple[str, ...]] = {
    "footage": ("capture_time", "indexed_at"),
}
DATE_OR_TIME_COLS: dict[str, tuple[str, ...]] = {
    "sessions": ("last_post",),
}
NUMERIC_COLS: dict[str, tuple[str, ...]] = {
    "pieces": ("hours",),
    "footage": ("size_bytes", "duration_s", "width", "height", "fps", "rotation"),
    "hooks": ("result_views",),
    "deliveries": ("views_24h", "views_72h", "shares_72h", "saves_72h", "avg_watch_72h"),
}
INT_COLS: dict[str, tuple[str, ...]] = {
    "sessions": ("posts_so_far",),
}
ENUM_COLS: dict[str, dict[str, tuple[str, ...]]] = {
    "pieces": {"color": YES_NO, "healed_footage": YES_NO},
    "sessions": {"status": SESSION_STATUS},
    "hooks": {"status": HOOK_STATUS},
    "deliveries": {"status": DELIVERY_STATUS, "type": DELIVERY_TYPES},
    "footage": {"camera": CAMERAS},
}

# Older column names -> canonical (applied after normalisation: lower, spaces/dashes -> "_",
# trailing "?" dropped). Callers can extend this through migrate(aliases=...).
DEFAULT_ALIASES: dict[str, str] = {
    "views": "result_views", "plays": "result_views", "result": "result_views",
    "used": "used_on", "date_used": "used_on", "used_date": "used_on",
    "file_name": "file", "filename": "file", "mp4": "file",
    "chat": "chat_file", "chat_copy": "chat_file",
    "hooks": "hook1", "hook_1": "hook1", "hook_2": "hook2",
    "shortcode": "posted_shortcode", "posted": "posted_shortcode", "ig_shortcode": "posted_shortcode",
    "24h": "views_24h", "views24h": "views_24h", "72h": "views_72h", "views72h": "views_72h",
    "posts": "posts_so_far", "post_count": "posts_so_far",
    "rest": "rest_until", "rest_date": "rest_until",
    "healed": "healed_footage",
    "size": "size_bytes", "bytes": "size_bytes", "duration": "duration_s",
    "transfer": "color_transfer", "trc": "color_transfer",
    "creationdate": "capture_time", "creation_time": "capture_time", "captured": "capture_time",
    "indexed": "indexed_at",
    "opener_in": "used_as_opener_in",
    "test": "tests", "experiment": "tests",
    "note": "notes", "comment": "notes", "comments": "notes",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Excel "CSV UTF-8" on Windows prefixes the file with a byte-order mark; it must never
# become part of the first column name (U+FEFF + "hook" would hide every burned hook).
_BOM = "\ufeff"


class LedgerError(ValueError):
    """A ledger is missing, has the wrong header, or a call asked for an unknown ledger."""


# --------------------------------------------------------------------------- basics
def path(name: str) -> Path:
    """Path of a ledger CSV under config.LEDGERS (names are the keys of HEADERS)."""
    if name not in HEADERS:
        raise LedgerError(f"unknown ledger {name!r}; known: {', '.join(NAMES)}")
    return config.LEDGERS / f"{name}.csv"


def header_of(name: str) -> list[str]:
    return list(HEADERS[name])


def _read_header(p: Path) -> list[str] | None:
    """First line of a CSV as a list, or None when the file is missing/empty."""
    if not p.exists() or p.stat().st_size == 0:
        return None
    with open(p, "r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            cells = [c.strip() for c in row]
            if cells:
                cells[0] = cells[0].lstrip(_BOM)
            return cells
    return None


def _strip_bom_key(k: str | None) -> str | None:
    if isinstance(k, str) and k.startswith(_BOM):
        return k.lstrip(_BOM)
    return k


def _assert_canonical_header(name: str, p: Path) -> None:
    """Refuse to write into a ledger whose on-disk header differs from the schema: a row
    appended under the wrong columns reads back misaligned (a burned hook looks unburned)."""
    existing = _read_header(p)
    if existing is not None and existing != HEADERS[name]:
        raise LedgerError(f"{name}.csv header is not canonical ({existing}); run migrate first")


def read(name: str) -> list[dict]:
    """Rows of a ledger as dicts (missing canonical columns filled with ""). Missing file = []."""
    header = HEADERS[name]
    rows = safeio.read_csv(path(name))
    out: list[dict] = []
    for r in rows:
        clean = {_strip_bom_key(k): (v if v is not None else "") for k, v in r.items() if k is not None}
        for k in header:
            clean.setdefault(k, "")
        out.append(clean)
    return out


def append(name: str, row: Mapping[str, object]) -> Path:
    """Append one row (unknown keys dropped, missing keys empty). Creates the ledger if needed.
    Refuses (LedgerError, file untouched) when the on-disk header is not canonical."""
    p = path(name)
    _assert_canonical_header(name, p)
    row = {k: _cell(v) for k, v in row.items()}
    return safeio.append_csv_row(p, row, HEADERS[name])


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _match(row: Mapping[str, object], key_fields: Sequence[str], target: Mapping[str, object]) -> bool:
    return all(str(row.get(k, "")).strip() == str(target.get(k, "")).strip() for k in key_fields)


def upsert(name: str, key_fields: Sequence[str], row: Mapping[str, object]) -> dict:
    """Update the first row whose key_fields match, else append; rewrite through safeio.

    Keeps every other row and the canonical column order. Values in `row` override the
    stored ones; columns not mentioned in `row` are kept. Refuses a ledger whose header is not
    canonical (run migrate first) so nothing is silently dropped. Returns the stored row.
    """
    header = HEADERS[name]
    if not key_fields:
        raise LedgerError("upsert needs at least one key field")
    for k in key_fields:
        if k not in header:
            raise LedgerError(f"{name}: key field {k!r} is not a column")
    p = path(name)
    _assert_canonical_header(name, p)
    new = {k: _cell(v) for k, v in row.items() if k in header}
    for k in key_fields:
        if not new.get(k, ""):
            raise LedgerError(f"{name}: key field {k!r} is empty in the row")
    rows = read(name)
    stored: dict | None = None
    for r in rows:
        if _match(r, key_fields, new):
            r.update(new)
            stored = r
            break
    if stored is None:
        stored = {k: new.get(k, "") for k in header}
        rows.append(stored)
    safeio.rewrite_csv(p, rows, header)
    return {k: stored.get(k, "") for k in header}


def ensure_all() -> list[str]:
    """Create every missing (or empty) ledger with its header. Never touches an existing one.
    Returns the names created."""
    created: list[str] = []
    safeio.mkdir(config.LEDGERS)
    for name, header in HEADERS.items():
        p = path(name)
        if p.exists() and p.stat().st_size > 0:
            continue
        with safeio.open_write(p, "w", overwrite=p.exists()) as f:
            csv.writer(f).writerow(header)
        created.append(name)
    return created


def present() -> dict[str, bool]:
    """Which ledgers exist with content (preflight reports this; it never creates them)."""
    return {name: (path(name).exists() and path(name).stat().st_size > 0) for name in NAMES}


# --------------------------------------------------------------------------- validation
def is_iso_date(s: str) -> bool:
    if not _DATE_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def is_iso_time_with_offset(s: str) -> bool:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return d.tzinfo is not None and "T" in s


def is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def is_int(s: str) -> bool:
    return bool(re.match(r"^-?\d+$", s))


def validate_rows(name: str, header: Sequence[str] | None, rows: Iterable[Mapping[str, str]]) -> list[str]:
    """Pure check of a header + rows against the schema. Returns a list of error strings."""
    errors: list[str] = []
    canon = HEADERS[name]
    if header is None:
        return [f"{name}: file is missing or empty (run ensure)"]
    if list(header) != canon:
        errors.append(f"{name}: header is {list(header)}; expected {canon}")
        return errors
    for i, r in enumerate(rows, start=2):   # line 1 is the header
        for col in DATE_COLS.get(name, ()):
            v = (r.get(col) or "").strip()
            if v and not is_iso_date(v):
                errors.append(f"{name} line {i}: {col}={v!r} is not YYYY-MM-DD")
        for col in TIME_COLS.get(name, ()):
            v = (r.get(col) or "").strip()
            if v and not is_iso_time_with_offset(v):
                errors.append(f"{name} line {i}: {col}={v!r} is not an ISO time with offset")
        for col in DATE_OR_TIME_COLS.get(name, ()):
            v = (r.get(col) or "").strip()
            if v and not (is_iso_date(v) or is_iso_time_with_offset(v)):
                errors.append(f"{name} line {i}: {col}={v!r} is not a date or ISO time with offset")
        for col in NUMERIC_COLS.get(name, ()):
            v = (r.get(col) or "").strip()
            if v and not is_numeric(v):
                errors.append(f"{name} line {i}: {col}={v!r} is not numeric")
        for col in INT_COLS.get(name, ()):
            v = (r.get(col) or "").strip()
            if v and not is_int(v):
                errors.append(f"{name} line {i}: {col}={v!r} is not an integer")
        for col, allowed in ENUM_COLS.get(name, {}).items():
            v = (r.get(col) or "").strip()
            if v and v.lower() not in allowed:
                errors.append(f"{name} line {i}: {col}={v!r} not in {'|'.join(allowed)}")
        if None in r or any(k not in canon for k in r):
            errors.append(f"{name} line {i}: has more cells than the header")
    return errors


def validate(name: str) -> list[str]:
    """Validate one ledger file on disk. Empty list = valid."""
    p = path(name)
    header = _read_header(p)
    if header is None:
        return validate_rows(name, None, [])
    with open(p, "r", encoding="utf-8-sig", newline="") as f:    # read only: BOM dropped
        rows = list(csv.DictReader(f))
    return validate_rows(name, header, rows)


def validate_all() -> dict[str, list[str]]:
    return {name: validate(name) for name in NAMES}


# --------------------------------------------------------------------------- migration
def normalize_column(col: str) -> str:
    c = col.strip().lower().rstrip("?").strip()
    c = re.sub(r"[\s\-/]+", "_", c)
    return c.strip("_")


def migrate(name: str, rows: Iterable[Mapping[str, object]],
            aliases: Mapping[str, str] | None = None) -> list[dict]:
    """Map rows with older column names onto the canonical header of `name`.

    Column names are normalised (case, spaces, dashes, trailing "?"), then looked up in
    DEFAULT_ALIASES updated with `aliases`. Columns that still do not map are joined as
    "col=value" into the `notes` column (after any existing notes). When the ledger has no
    notes column they are kept under the key "_unmapped" so the caller can decide (rewrite_csv
    would drop that key). A legacy "hooks" column holding "a | b" fills hook1 and hook2.
    """
    canon = HEADERS[name]
    amap = dict(DEFAULT_ALIASES)
    if aliases:
        amap.update({normalize_column(k): v for k, v in aliases.items()})
    has_notes = "notes" in canon
    out: list[dict] = []
    for r in rows:
        new: dict = {k: "" for k in canon}
        extras: list[str] = []
        for k, v in r.items():
            if k is None:
                continue
            val = _cell(v).strip() if not isinstance(v, list) else " ".join(map(str, v)).strip()
            nk = normalize_column(str(k))
            target = nk if nk in canon else amap.get(nk)
            if target is None or target not in canon:
                if val:
                    extras.append(f"{k.strip()}={val}")
                continue
            if target == "hook1" and name == "deliveries" and nk == "hooks":
                parts = [s.strip() for s in re.split(r"\s*[|;]\s*|\n", val) if s.strip()]
                if parts:
                    new["hook1"] = parts[0]
                if len(parts) > 1 and not new.get("hook2"):
                    new["hook2"] = parts[1]
                continue
            if new.get(target) and target == "notes":
                new[target] = f"{new[target]}; {val}" if val else new[target]
            elif new.get(target) and not val:
                continue
            else:
                new[target] = val
        if extras:
            joined = "; ".join(extras)
            if has_notes:
                new["notes"] = f"{new['notes']}; {joined}" if new.get("notes") else joined
            else:
                new["_unmapped"] = joined
        out.append(new)
    return out


def migrate_file(name: str, aliases: Mapping[str, str] | None = None, *, write: bool = False) -> dict:
    """Migrate the ledger on disk. Dry-run by default; write=True rewrites it (backup kept by
    safeio.rewrite_csv). Returns {"rows": n, "header_before": [...], "unmapped": n, "written": bool}."""
    p = path(name)
    header = _read_header(p)
    if header is None:
        return {"rows": 0, "header_before": None, "unmapped": 0, "written": False}
    with open(p, "r", encoding="utf-8-sig", newline="") as f:    # read only: BOM dropped
        rows = list(csv.DictReader(f))
    new_rows = migrate(name, rows, aliases)
    unmapped = sum(1 for r in new_rows if r.get("_unmapped"))
    if write:
        safeio.rewrite_csv(p, new_rows, HEADERS[name])
    return {"rows": len(new_rows), "header_before": header, "unmapped": unmapped, "written": write}


# --------------------------------------------------------------------------- typed helpers
def _as_date(d: date | datetime | str) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip()
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    return date.fromisoformat(s)


def hooks_all() -> list[dict]:
    return read("hooks")


def burned_hooks() -> list[str]:
    """Hooks that must never be used again (status burned, retired or losing_type).
    Narrower than tools.hook_gate.used_hooks(), which G10 / deliver use: that one returns EVERY
    row of hooks.csv (any status, "ok" rows are hooks already used) plus delivered hooks."""
    return [r["hook"] for r in read("hooks")
            if (r.get("status") or "").strip().lower() in HOOK_BURNED_STATUS and r.get("hook")]


def captions_all() -> list[str]:
    return [r["caption"] for r in read("captions_used") if r.get("caption")]


def sessions_get(session: str) -> dict | None:
    key = session.strip().lower()
    for r in read("sessions"):
        if (r.get("session") or "").strip().lower() == key:
            return r
    return None


def _rest_until_parsed(row: Mapping[str, str]) -> tuple[date | None, bool]:
    """(rest_until as a date or None when empty, True when the cell is unparsable)."""
    v = (row.get("rest_until") or "").strip()
    if not v:
        return None, False
    try:
        return _as_date(v), False
    except ValueError:
        return None, True


def sessions_rest_ok(session: str, today: date) -> bool:
    """True when the session is out of rest on `today` (rest_until is the first day it may be
    used again). Unknown sessions and empty rest_until are not resting. An unparsable
    rest_until (e.g. "10/30/2026" typed by hand) FAILS CLOSED: the session stays resting
    until `validate` is run and the cell fixed, so a typo never un-rests a session."""
    r = sessions_get(session)
    if r is None:
        return True
    until, bad = _rest_until_parsed(r)
    if bad:
        return False
    if until is None:
        return True
    return _as_date(today) >= until


def _status_word(row: Mapping[str, str]) -> str:
    # tolerate "spent: 6+ posts, ..." style remarks (validate() still flags them)
    return (row.get("status") or "").split(":")[0].strip().lower()


def sessions_available(session: str, today: date) -> bool:
    """rest_ok AND status is not spent. A row marked status=resting with no usable rest_until
    (empty or unparsable) is treated as still resting (fail closed)."""
    r = sessions_get(session)
    if r is not None:
        st = _status_word(r)
        if st == "spent":
            return False
        if st == "resting":
            until, bad = _rest_until_parsed(r)
            if bad or until is None:
                return False
    return sessions_rest_ok(session, today)


def session_posts(session: str) -> int:
    r = sessions_get(session)
    if r is None:
        return 0
    v = (r.get("posts_so_far") or "").strip()
    return int(v) if is_int(v) else 0


def pieces_get(piece: str) -> dict | None:
    key = piece.strip().lower()
    for r in read("pieces"):
        if (r.get("piece") or "").strip().lower() == key:
            return r
    return None


def pieces_hours() -> dict[str, str]:
    """piece -> hours, only for pieces whose hours are recorded (never invent hours)."""
    out: dict[str, str] = {}
    for r in read("pieces"):
        h = (r.get("hours") or "").strip()
        if r.get("piece") and h and is_numeric(h):
            out[r["piece"]] = h
    return out


def deliveries_for_date(d: date | str) -> list[dict]:
    key = _as_date(d).isoformat()
    return [r for r in read("deliveries") if (r.get("date") or "").strip() == key]


def deliveries_count_by_date(*, counted_only: bool = True) -> dict[str, int]:
    """date -> number of deliveries. counted_only skips held/legacy/dryrun/cancelled rows."""
    out: dict[str, int] = {}
    for r in read("deliveries"):
        d = (r.get("date") or "").strip()
        if not d:
            continue
        if counted_only and (r.get("status") or "").strip().lower() in DELIVERY_NOT_COUNTED:
            continue
        out[d] = out.get(d, 0) + 1
    return out


def deliveries_posted_since(d: date | str) -> list[dict]:
    """Deliveries dated on/after `d` that were posted (status posted or a shortcode recorded)."""
    since = _as_date(d)
    out: list[dict] = []
    for r in read("deliveries"):
        ds = (r.get("date") or "").strip()
        if not is_iso_date(ds) or date.fromisoformat(ds) < since:
            continue
        posted = (r.get("status") or "").strip().lower() == "posted" or bool((r.get("posted_shortcode") or "").strip())
        if posted:
            out.append(r)
    return out


def footage_rows() -> list[dict]:
    return read("footage")


# --------------------------------------------------------------------------- CLI
def _utf8_stdout() -> None:
    """Captions carry emoji; an interactive Windows console without PYTHONUTF8=1 would raise
    UnicodeEncodeError on print(). Best effort, never fatal."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdout()
    # --json is accepted both before and after the subcommand ("show hooks --json"). The
    # subcommand copy uses SUPPRESS so its default never clobbers a top-level --json.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    ap = argparse.ArgumentParser(prog="py -m tools.ledgers", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", default=False, help="JSON output")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure", help="create missing ledgers with their headers", parents=[common])
    v = sub.add_parser("validate", help="check header, dates, times, numbers and statuses", parents=[common])
    v.add_argument("name", nargs="?", choices=NAMES)
    s = sub.add_parser("show", help="print a ledger", parents=[common])
    s.add_argument("name", choices=NAMES)
    m = sub.add_parser("migrate", help="map older column names onto the canonical header", parents=[common])
    m.add_argument("name", choices=NAMES)
    m.add_argument("--write", action="store_true", help="rewrite the file (default: dry-run)")
    a = ap.parse_args(argv)

    if a.cmd == "ensure":
        created = ensure_all()
        if a.json:
            print(json.dumps({"created": created, "present": present()}))
        else:
            for n in NAMES:
                print(f"{n}: {'created' if n in created else 'present'}")
        return 0
    if a.cmd == "validate":
        result = {a.name: validate(a.name)} if a.name else validate_all()
        if a.json:
            print(json.dumps(result))
        else:
            for n, errs in result.items():
                print(f"{n}: {'OK' if not errs else str(len(errs)) + ' error(s)'}")
                for e in errs:
                    print(f"  {e}")
        return 0 if not any(result.values()) else 1
    if a.cmd == "show":
        rows = read(a.name)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            w = csv.DictWriter(sys.stdout, fieldnames=HEADERS[a.name], extrasaction="ignore",
                               lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return 0
    if a.cmd == "migrate":
        res = migrate_file(a.name, write=a.write)
        if a.json:
            print(json.dumps(res))
        else:
            print(f"{a.name}: {res['rows']} row(s), {res['unmapped']} with unmapped columns, "
                  f"{'written' if res['written'] else 'dry-run (add --write)'}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
