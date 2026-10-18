"""Delivery: put a gated reel into post/<date>/ with its slot, hooks, caption and POST_THIS.txt
(START_HERE section 9, section 5 steps 6-9 and 11; fixes WEAK_POINTS W3, W4, W11, W12).

Why: batch deliveries of 5-17 reels fed the bursts, Claude's cuts stopped getting posted, old
renders were re-delivered with retired hooks, and Copy_DehRelXKsds re-uploaded a posted reel.
This module makes "exactly two reels a day, each with a dated slot, two hooks and the caption"
mechanical, and refuses everything the rules forbid before a single file is copied.

What ``deliver`` checks, in order, before copying anything:
1. ``<stem>.gates.json`` exists next to the reel and ``pass`` is true (``force_reason`` overrides
   this one check and is logged into deliveries.csv notes);
2. the reel stem is not in config.NEVER_REUPLOAD (hard);
3. post/<date>/ plus deliveries.csv hold fewer than config.MAX_POSTS_PER_DAY reels for that
   date and the slot letter is unused (hard; idempotent re-runs never exceed 2);
4. the other reel of the day is not the same type or session (force_reason overrides) and not
   also a HOLD cut (hard: never two HOLD cuts together);
5. the two hooks pass tools.hook_gate.check_pair against the hook and caption ledgers and the
   pieces hours, and the caption ends with the exact tail (hard);
6. frame-1 phash (tools.fingerprint) matches no delivered or posted reel (hard re-upload guard).

Then it copies reel, chat copy, .edl.json and .gates.json into post/<date>/ as
``<slot>_<HHMM>_<slug>.mp4`` (+ ``_chat.mp4``, ``.edl.json``, ``.gates.json``), appends
deliveries.csv, files the signature in data/signatures.json, mirrors the files to the Drive
POST folder (skipped with a note when the Drive folder is missing) and regenerates
post/<date>/POST_THIS.txt (plain, short, for his phone) plus its Drive copy.

The HOLD queue: ``hold`` files a runner-up as hold/HOLD_<date>_<name>.mp4 (+ sidecars),
``hold_list`` / ``hold_pick`` read it back. ``plan_today`` wraps tools.slot_planner.plan with
what post/<date>/ and deliveries.csv already hold and what he posted today (data/reels.csv).

Every time stamp is a parameter (``now``, ``when``) so tests are deterministic; all wall-clock
text is config.TZ. Library functions return data and never print.

CLI (exit 0 ok / 1 refused or failed, ``--json`` for machine output):
  py -m tools.deliver plan --now 2026-10-19T06:40
  py -m tools.deliver deliver <reel.mp4> --chat <chat.mp4> --slot A --when 2026-10-19T11:00
      --kind SEND --hook "..." --hook "..." --caption-file c.txt [--edl x.edl.json]
      [--labels "Shoulder=1 session / 7 hours"] [--tests "..."] [--force "reason"] [--no-mirror]
  py -m tools.deliver hold <reel.mp4> --chat <chat.mp4> --name shoulder7h_B --date 2026-10-19
  py -m tools.deliver post-this --date 2026-10-19 [--now ...]
  py -m tools.deliver list-hold
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from . import config
from . import safeio

SLOTS = ("A", "B")
KINDS = ("SEND", "SAVE")
RELAY_TIMES = {"A": "10:05", "B": "13:05"}          # the phone relay (START_HERE section 2)
TIKTOK_CAP_RESET = date(2026, 11, 1)                # Metricool free cap resets (W13)
BREAKOUT_MAX_AGE_H = 24.0
DELIVERIES_HEADER = ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2",
                     "caption", "status", "posted_shortcode", "views_24h", "views_72h",
                     "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"]
# deliveries.csv rows that never reached post/<date>/ and do not count or guard anything
NOT_COUNTED = ("held", "legacy", "dryrun", "cancelled")
_DELIVERED_RE = re.compile(r"^([AB])_(\d{2})(\d{2})_(.+)\.mp4$", re.IGNORECASE)
_HOLD_RE = re.compile(r"^HOLD_(\d{4}-\d{2}-\d{2})_(.+)$")
_HOLD_FILE_RE = re.compile(r"^HOLD_(\d{4}-\d{2}-\d{2})_(.+)\.mp4$", re.IGNORECASE)


# --------------------------------------------------------------------------- small helpers
def _tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def parse_when(s: str | datetime) -> datetime:
    """ISO string (naive = ET) or datetime -> aware datetime in config.TZ."""
    if isinstance(s, datetime):
        return _tz(s)
    return _tz(datetime.fromisoformat(str(s).strip()))


def slugify(text: str) -> str:
    """ASCII kebab-case of a reel stem: 'Shoulder 7h_A' -> 'shoulder-7h-a'."""
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return t or "reel"


def reel_slug(reel: Path) -> tuple[str, str]:
    """(slug, source): a HOLD_<date>_<name> file keeps only <name> and is marked 'hold'."""
    stem = Path(reel).stem
    m = _HOLD_RE.match(stem)
    if m:
        return slugify(m.group(2)), "hold"
    return slugify(stem), "render"


def gates_path(reel: Path) -> Path:
    return Path(reel).with_name(Path(reel).stem + ".gates.json")


def edl_path(reel: Path) -> Path:
    return Path(reel).with_name(Path(reel).stem + ".edl.json")


def post_dir(d: date | str) -> Path:
    return config.POST / _date_str(d)


def drive_post_dir(d: date | str) -> Path:
    return config.DRIVE_POST / _date_str(d)


def manifest_path(d: date | str) -> Path:
    return config.STATE / "post_this" / f"{_date_str(d)}.json"


def _date_str(d: date | str | datetime) -> str:
    if isinstance(d, datetime):
        return _tz(d).date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d).strip()[:10]


def _as_date(d: date | str | datetime) -> date:
    return date.fromisoformat(_date_str(d))


def _at(d: date, hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=config.TZ)


def _hhmm(dt: datetime) -> str:
    return _tz(dt).strftime("%H:%M")


def _read_csv_rows(path: Path) -> list[dict]:
    return safeio.read_csv(path) if path.exists() else []


def deliveries_path() -> Path:
    return config.LEDGERS / "deliveries.csv"


def pieces_rows() -> list[dict]:
    return _read_csv_rows(config.LEDGERS / "pieces.csv")


# --------------------------------------------------------------------------- what is delivered
def delivered_in_post(d: date | str) -> list[dict]:
    """Reels in post/<date>/: [{slot, time (HH:MM), file, slug}] (chat copies ignored)."""
    folder = post_dir(d)
    out: list[dict] = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower().endswith("_chat.mp4"):
            continue
        m = _DELIVERED_RE.match(p.name)
        if not m:
            continue
        out.append({"slot": m.group(1).upper(), "time": f"{m.group(2)}:{m.group(3)}",
                    "file": p.name, "slug": m.group(4)})
    return out


def delivered_in_ledger(d: date | str) -> list[dict]:
    """deliveries.csv rows for a date that count against the cap (status not held/legacy/...)."""
    key = _date_str(d)
    out = []
    for r in _read_csv_rows(deliveries_path()):
        if (r.get("date") or "").strip() != key:
            continue
        if (r.get("status") or "").strip().lower() in NOT_COUNTED:
            continue
        out.append(r)
    return out


def delivered_slots(d: date | str) -> dict[str, dict]:
    """{slot letter: {slot, time, file, source}} from post/<date>/ merged with deliveries.csv."""
    slots: dict[str, dict] = {}
    for x in delivered_in_post(d):
        slots.setdefault(x["slot"], {"slot": x["slot"], "time": x["time"], "file": x["file"],
                                     "source": "post"})
    for r in delivered_in_ledger(d):
        letter = (r.get("slot") or "").strip().upper()[:1]
        if letter not in SLOTS or letter in slots:
            continue
        fname = Path(r.get("file") or "").name
        m = _DELIVERED_RE.match(fname)
        t = f"{m.group(2)}:{m.group(3)}" if m else ""
        slots[letter] = {"slot": letter, "time": t, "file": fname, "source": "ledger"}
    return slots


def his_posts_on(d: date | str, reels_csv: Path | None = None) -> list[datetime]:
    """His posts on a date from data/reels.csv (posted_et), [] when the file is missing."""
    p = reels_csv or (config.DATA / "reels.csv")
    key = _date_str(d)
    out = []
    for r in _read_csv_rows(p):
        s = (r.get("posted_et") or "").strip()
        if not s:
            continue
        try:
            dt = _tz(datetime.fromisoformat(s))
        except ValueError:
            continue
        if dt.date().isoformat() == key:
            out.append(dt)
    return sorted(out)


# --------------------------------------------------------------------------- manifest
def load_manifest(d: date | str) -> dict:
    data = safeio.read_json(manifest_path(d), default=None)
    if not isinstance(data, dict) or "entries" not in data:
        return {"date": _date_str(d), "entries": []}
    return data


def save_manifest(d: date | str, manifest: dict) -> Path:
    return safeio.write_json(manifest_path(d), manifest, overwrite=True)


def _entries_for(d: date | str) -> list[dict]:
    """POST_THIS entries for a date: the manifest, completed from deliveries.csv when a
    delivered slot is missing from it (manifest lost or written by an older version)."""
    man = load_manifest(d)
    entries = [e for e in man.get("entries", []) if e.get("slot") in SLOTS]
    have = {e["slot"] for e in entries}
    for r in delivered_in_ledger(d):
        letter = (r.get("slot") or "").strip().upper()[:1]
        if letter not in SLOTS or letter in have:
            continue
        fname = Path(r.get("file") or "").name
        m = _DELIVERED_RE.match(fname)
        t = f"{m.group(2)}:{m.group(3)}" if m else ""
        edl = _edl_sidecar_for(d, fname)
        entries.append({
            "slot": letter, "time": t, "kind": "SEND" if letter == "A" else "SAVE",
            "hooks": [r.get("hook1") or "", r.get("hook2") or ""],
            "caption": r.get("caption") or "", "labels": dict(edl.get("labels") or {}),
            "pieces": list(edl.get("pieces") or []), "tests": r.get("tests") or "",
            "file": fname, "type": r.get("type") or "", "session": edl.get("session") or "",
            "source": "hold" if "from HOLD" in (r.get("notes") or "") else "render",
            "delivered_at": "",
        })
        have.add(letter)
    entries.sort(key=lambda e: (e["slot"], e.get("time") or ""))
    return entries


def _edl_sidecar_for(d: date | str, fname: str) -> dict:
    p = post_dir(d) / (Path(fname).stem + ".edl.json")
    data = safeio.read_json(p, default=None)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- POST_THIS
def pieces_without_hours(pieces: Sequence[str], rows: Sequence[dict] | None = None) -> list[str]:
    """Pieces shown in a reel whose hours are empty (or missing) in pieces.csv."""
    rows = pieces_rows() if rows is None else rows
    known = {}
    for r in rows:
        name = (r.get("piece") or "").strip()
        if name:
            known[name.lower()] = (r.get("hours") or "").strip()
    out = []
    for p in pieces or []:
        p = str(p).strip()
        if p and not known.get(p.lower()):
            out.append(p)
    return out


def blocked_line(now: datetime, d: date | str, entries: Sequence[dict]) -> list[str]:
    """Top BLOCKED lines: state/blocked.txt, then any reel delivered after its relay time."""
    lines: list[str] = []
    p = config.STATE / "blocked.txt"
    if p.is_file():
        txt = safeio.read_text(p).strip()
        if txt:
            first = txt.splitlines()[0].strip()
            lines.append(first if first.upper().startswith("BLOCKED:") else f"BLOCKED: {first}")
    dd = _as_date(d)
    for e in entries:
        relay = _at(dd, RELAY_TIMES.get(e.get("slot", ""), "10:05"))
        delivered_at = e.get("delivered_at") or ""
        try:
            when_delivered = _tz(datetime.fromisoformat(delivered_at)) if delivered_at else None
        except ValueError:
            when_delivered = None
        missed = (when_delivered > relay) if when_delivered is not None else (_tz(now) > relay)
        if missed:
            lines.append(f"BLOCKED: relay passed: take {e['slot']} from the Drive folder")
    return lines


def breakout_line(now: datetime) -> str | None:
    """'Share <shortcode> to the grid' while state/breakout.json is < 24 h old."""
    p = config.STATE / "breakout.json"
    data = safeio.read_json(p, default=None)
    if not isinstance(data, dict):
        return None
    code = str(data.get("shortcode") or data.get("code") or "").strip()
    if not code:
        return None
    stamp = None
    for k in ("at", "when", "detected_et", "detected_at", "time", "generated_et"):
        v = data.get(k)
        if v:
            try:
                stamp = _tz(datetime.fromisoformat(str(v)))
                break
            except ValueError:
                continue
    if stamp is None:
        stamp = datetime.fromtimestamp(p.stat().st_mtime, tz=config.TZ)
    age_h = (_tz(now) - stamp).total_seconds() / 3600.0
    if age_h < BREAKOUT_MAX_AGE_H:
        return f"Share {code} to the grid"
    return None


def post_this_text(d: date | str, now: datetime, entries: Sequence[dict] | None = None,
                   pieces: Sequence[dict] | None = None) -> str:
    """The full POST_THIS.txt for a date (plain and short, read on a phone in 10 seconds)."""
    now = _tz(now)
    entries = list(_entries_for(d) if entries is None else entries)
    entries.sort(key=lambda e: (e.get("slot", ""), e.get("time") or ""))
    lines: list[str] = []
    lines.extend(blocked_line(now, d, entries))
    bo = breakout_line(now)
    if bo:
        lines.append(bo)
    for e in entries:
        if lines:
            lines.append("")
        slot, t = e.get("slot", "?"), e.get("time") or "??:??"
        if slot == "A":
            lines.append(f"A - post at {t} ET, alone")
        else:
            lines.append(f"B - post at least {int(config.MIN_GAP_HOURS)}h after you post A "
                         f"(earliest {t} ET)")
        lines.append(f"Type: {e.get('kind') or ('SEND' if slot == 'A' else 'SAVE')}")
        hooks = list(e.get("hooks") or [])
        lines.append(f"Hook 1: {hooks[0] if len(hooks) > 0 else ''}")
        lines.append(f"Hook 2: {hooks[1] if len(hooks) > 1 else ''}")
        caption = (e.get("caption") or "").rstrip()
        lines.append("Caption:")
        lines.append(caption)
        if not caption.endswith(config.CAPTION_TAIL):
            lines.append("(caption tail is NOT exact; fix before posting)")
        labels = e.get("labels") or {}
        if labels:
            lines.append("Labels: " + "; ".join(f"{k}: {v}" for k, v in labels.items()))
        lines.append("Add a trending sound at post time.")
        lines.append("Share to grid if it is at 1.5x median at ~3h.")
        lines.append(f"Tests: {e.get('tests') or 'none'}")
        if now.date() < TIKTOK_CAP_RESET:
            lines.append("TikTok: post the same file and caption there yourself "
                         "(Metricool cap resets Nov 1).")
        missing = pieces_without_hours(e.get("pieces") or [], pieces)
        if missing:
            lines.append("Reply with hours/sessions for: " + ", ".join(missing))
    card = config.POST / "SHOOT_CARD.txt"
    if card.is_file():
        txt = safeio.read_text(card).rstrip()
        if txt:
            lines.append("")
            lines.append(txt)
    return "\n".join(lines).rstrip("\n") + "\n"


def write_post_this(d: date | str, now: datetime, mirror: bool = True) -> dict:
    """Regenerate post/<date>/POST_THIS.txt (overwrite allowed for .txt under post/) and its
    Drive copy. Returns {path, drive_path|None, text, notes}."""
    text = post_this_text(d, now)
    target = post_dir(d) / "POST_THIS.txt"
    safeio.write_text(target, text, overwrite=True)
    notes: list[str] = []
    drive_path = None
    if mirror:
        if config.DRIVE_OUT.is_dir():
            drive_path = drive_post_dir(d) / "POST_THIS.txt"
            safeio.write_text(drive_path, text, overwrite=True)
        else:
            notes.append(f"Drive folder missing ({config.DRIVE_OUT}); POST_THIS not mirrored")
    return {"path": target, "drive_path": drive_path, "text": text, "notes": notes}


# --------------------------------------------------------------------------- the checks
def _gates_info(reel: Path) -> tuple[dict | None, str | None]:
    p = gates_path(reel)
    data = safeio.read_json(p, default=None)
    if not isinstance(data, dict):
        return None, f"no gates sidecar {p.name} next to the reel (run py -m tools.gates first)"
    if data.get("pass") is not True:
        failed = [g.get("id") for g in data.get("gates", [])
                  if g.get("hard") and not g.get("ok")]
        return data, f"gates failed ({', '.join(str(x) for x in failed) or 'pass is not true'})"
    return data, None


def _hook_check(hooks: Sequence[str], caption: str, pieces: Sequence[str] | None) -> list[str]:
    from . import hook_gate     # lazy: another builder's module
    reasons: list[str] = []
    if len([h for h in hooks if h and h.strip()]) < 2:
        reasons.append("two hooks are required")
        return reasons
    ph = hook_gate.pieces_hours_from_rows(pieces_rows(), only=list(pieces) if pieces else None)
    res = hook_gate.check_pair(hooks[0], hooks[1], hook_gate.burned_hooks(),
                               hook_gate.used_captions(), None, ph)
    if not res["ok"]:
        reasons.extend("hook gate: " + r for r in res["reasons"])
    if not hook_gate.caption_tail_ok(caption):
        reasons.append("caption does not end with the exact tail: " + config.CAPTION_TAIL)
    return reasons


def _reupload_check(reel: Path) -> tuple[dict | None, list[str]]:
    """Frame-1 phash against deliveries.csv rows and posted signatures (hard)."""
    from . import fingerprint   # lazy: needs pillow + numpy
    sig = fingerprint.signature(reel)
    f1 = sig["frame1"]
    reasons: list[str] = []
    for r in _read_csv_rows(deliveries_path()):
        if (r.get("status") or "").strip().lower() in NOT_COUNTED:
            continue
        ph = (r.get("phash") or "").strip()
        if not ph:
            continue
        try:
            dist = fingerprint.hamming(f1, ph)
        except ValueError:
            continue
        if dist <= config.PHASH_MATCH_MAX:
            reasons.append(f"re-upload: frame 1 matches delivery {r.get('id') or r.get('file')} "
                           f"(distance {dist})")
            break
    rep = fingerprint.frame1_report(reel, sig=sig)
    if rep.get("reused"):
        reasons.append(f"re-upload: frame 1 matches posted reel {rep.get('key')} "
                       f"(distance {rep.get('distance')})")
    return sig, reasons


# --------------------------------------------------------------------------- deliver
def deliver(reel: Path, chat: Path, slot: str, when: datetime, kind: str, hooks: Sequence[str],
            caption: str, now: datetime, edl: Path | None = None, labels: dict | None = None,
            tests: str = "", mirror: bool = True, force_reason: str | None = None) -> dict:
    """Deliver one reel into post/<date>/ for a slot. Returns {ok, refused: [...], ...}."""
    reel, chat = Path(reel), Path(chat)
    now = _tz(now)
    when = _tz(when)
    slot = str(slot).strip().upper()
    kind = str(kind).strip().upper()
    hooks = [str(h).strip() for h in (hooks or [])]
    caption = caption if caption is not None else ""
    d = when.date()
    ds = d.isoformat()
    refused: list[str] = []
    notes: list[str] = []

    if slot not in SLOTS:
        refused.append(f"slot must be A or B, got {slot!r}")
    if kind not in KINDS:
        refused.append(f"kind must be SEND or SAVE, got {kind!r}")
    if not reel.is_file():
        refused.append(f"reel not found: {reel}")
    if not chat.is_file():
        refused.append(f"chat copy not found: {chat}")
    if refused:
        return {"ok": False, "refused": refused, "date": ds, "slot": slot}

    # 1. gates
    gates, gerr = _gates_info(reel)
    if gerr:
        if force_reason:
            notes.append(f"FORCED past gates ({gerr}): {force_reason}")
        else:
            refused.append(gerr)

    # 2. never re-upload list
    if reel.stem.lower() in {x.lower() for x in config.NEVER_REUPLOAD}:
        refused.append(f"{reel.stem} is in NEVER_REUPLOAD (re-uploads a posted reel, W11)")

    # 3. cap and slot letter
    slots = delivered_slots(d)
    if len(slots) >= config.MAX_POSTS_PER_DAY:
        refused.append(f"post/{ds}/ already holds {len(slots)} reels (max {config.MAX_POSTS_PER_DAY})")
    elif slot in slots:
        refused.append(f"slot {slot} already delivered for {ds}: {slots[slot]['file']}")

    # 4. the other reel of the day: type, session, HOLD pairing
    slug, source = reel_slug(reel)
    edl_src = Path(edl) if edl else edl_path(reel)
    edl_data = safeio.read_json(edl_src, default=None) if edl_src.is_file() else None
    edl_data = edl_data if isinstance(edl_data, dict) else {}
    rtype = str((gates or {}).get("type") or edl_data.get("type") or "")
    session = str(edl_data.get("session") or "")
    for other in _entries_for(d):
        if other.get("slot") == slot:
            continue
        if other.get("source") == "hold" and source == "hold":
            refused.append("never two HOLD cuts on one day (other slot is already a HOLD cut)")
        same = []
        if rtype and other.get("type") and rtype == other.get("type"):
            same.append(f"type {rtype}")
        if session and other.get("session") and session == other.get("session"):
            same.append(f"session {session}")
        if same:
            msg = f"same {' and '.join(same)} as slot {other.get('slot')} on {ds}"
            if force_reason:
                notes.append(f"FORCED past '{msg}': {force_reason}")
            else:
                refused.append(msg)

    # 5. hooks and caption
    pieces = [str(p) for p in (edl_data.get("pieces") or [])]
    refused.extend(_hook_check(hooks, caption, pieces))

    if refused:
        return {"ok": False, "refused": refused, "date": ds, "slot": slot, "notes": notes}

    # 6. re-upload guard (frame-1 phash)
    sig, rr = _reupload_check(reel)
    if rr:
        return {"ok": False, "refused": rr, "date": ds, "slot": slot, "notes": notes}

    # ---- copy
    stem = f"{slot}_{when.strftime('%H%M')}_{slug}"
    folder = post_dir(d)
    files = {
        "reel": folder / f"{stem}.mp4",
        "chat": folder / f"{stem}_chat.mp4",
    }
    safeio.copy_file(reel, files["reel"])
    safeio.copy_file(chat, files["chat"])
    if edl_src.is_file():
        files["edl"] = safeio.copy_file(edl_src, folder / f"{stem}.edl.json")
    gp = gates_path(reel)
    if gp.is_file():
        files["gates"] = safeio.copy_file(gp, folder / f"{stem}.gates.json")

    # ---- ledger
    rid = f"{ds}_{slot}_{slug}"
    if source == "hold":
        notes.append(f"from HOLD ({reel.name})")
    row = {
        "id": rid, "date": ds, "slot": slot, "type": rtype,
        "file": f"post/{ds}/{files['reel'].name}", "chat_file": f"post/{ds}/{files['chat'].name}",
        "phash": sig["frame1"], "hook1": hooks[0], "hook2": hooks[1], "caption": caption,
        "status": "delivered", "tests": tests or "", "notes": "; ".join(notes),
    }
    safeio.append_csv_row(deliveries_path(), row, DELIVERIES_HEADER)
    try:
        from . import fingerprint
        fingerprint.add_signature(rid, files["reel"], "delivery", now=now, sig=sig)
    except Exception as exc:   # the delivery stands even if the store write fails
        notes.append(f"signature store not updated: {exc}")

    # ---- Drive mirror
    mirrored: list[Path] = []
    if mirror:
        if config.DRIVE_OUT.is_dir():
            ddir = drive_post_dir(d)
            for key, src in files.items():
                mirrored.append(safeio.copy_file(src, ddir / src.name))
        else:
            notes.append(f"Drive folder missing ({config.DRIVE_OUT}); mirror skipped")

    # ---- manifest + POST_THIS
    man = load_manifest(d)
    man["entries"] = [e for e in man.get("entries", []) if e.get("slot") != slot]
    man["entries"].append({
        "id": rid, "slot": slot, "time": when.strftime("%H:%M"), "when": when.isoformat(),
        "kind": kind, "hooks": hooks[:2], "caption": caption,
        "labels": dict(labels or edl_data.get("labels") or {}), "pieces": pieces,
        "tests": tests or "", "file": files["reel"].name, "type": rtype, "session": session,
        "source": source, "delivered_at": now.isoformat(timespec="seconds"),
    })
    man["entries"].sort(key=lambda e: e["slot"])
    save_manifest(d, man)
    pt = write_post_this(d, now, mirror=mirror)
    notes.extend(pt["notes"])

    return {
        "ok": True, "id": rid, "date": ds, "slot": slot, "kind": kind, "time": when.strftime("%H:%M"),
        "files": {k: str(v) for k, v in files.items()}, "phash": sig["frame1"],
        "mirrored": [str(p) for p in mirrored], "post_this": str(pt["path"]),
        "post_this_drive": str(pt["drive_path"]) if pt["drive_path"] else None,
        "notes": notes, "refused": [],
    }


# --------------------------------------------------------------------------- HOLD queue
def hold(reel: Path, chat: Path, edl: Path | None, gates: Path | None, name: str,
         d: date | str) -> Path:
    """Copy a runner-up into hold/ as HOLD_<date>_<name>.mp4 (+ _chat.mp4, .edl.json, .gates.json)."""
    reel, chat = Path(reel), Path(chat)
    ds = _date_str(d)
    stem = f"HOLD_{ds}_{slugify(name)}"
    out = safeio.copy_file(reel, config.HOLD / f"{stem}.mp4")
    safeio.copy_file(chat, config.HOLD / f"{stem}_chat.mp4")
    edl_src = Path(edl) if edl else edl_path(reel)
    if edl_src.is_file():
        safeio.copy_file(edl_src, config.HOLD / f"{stem}.edl.json")
    g_src = Path(gates) if gates else gates_path(reel)
    if g_src.is_file():
        safeio.copy_file(g_src, config.HOLD / f"{stem}.gates.json")
    return out


def hold_list(now: datetime | None = None) -> list[dict]:
    """Cuts waiting in hold/ (top level): [{name, date, age_days, gates_pass, file, chat, edl, gates}]."""
    today = _tz(now).date() if now else None
    out: list[dict] = []
    folder = config.HOLD
    if not folder.is_dir():
        return out
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower().endswith("_chat.mp4"):
            continue
        m = _HOLD_FILE_RE.match(p.name)
        if not m:
            continue
        ds, name = m.group(1), m.group(2)
        gp = p.with_name(p.stem + ".gates.json")
        g = safeio.read_json(gp, default=None)
        gates_pass = bool(isinstance(g, dict) and g.get("pass") is True)
        try:
            age = (today - date.fromisoformat(ds)).days if today else None
        except ValueError:
            age = None
        chat = p.with_name(p.stem + "_chat.mp4")
        edl = p.with_name(p.stem + ".edl.json")
        out.append({"name": name, "date": ds, "age_days": age, "gates_pass": gates_pass,
                    "file": str(p), "chat": str(chat) if chat.is_file() else None,
                    "edl": str(edl) if edl.is_file() else None,
                    "gates": str(gp) if gp.is_file() else None})
    out.sort(key=lambda r: (r["date"], r["name"]), reverse=True)
    return out


def hold_pick(exclude_names: Sequence[str] = (), now: datetime | None = None) -> dict | None:
    """The most recent passing HOLD cut whose name is not excluded (None when the queue is dry)."""
    ex = {str(n).strip().lower() for n in exclude_names or ()}
    for r in hold_list(now):
        if not r["gates_pass"]:
            continue
        if r["name"].lower() in ex or Path(r["file"]).stem.lower() in ex:
            continue
        return r
    return None


def hold_count() -> int:
    return len(hold_list())


# --------------------------------------------------------------------------- plan
def plan_today(now: datetime, his_posts_today: Sequence[datetime] | None = None,
               hold_count_: int | None = None, morning_arm: bool = False,
               target_date: date | None = None) -> dict:
    """tools.slot_planner.plan with delivered = post/<date>/ + deliveries.csv and his posts
    from data/reels.csv (posted_et on that date) when not given."""
    from . import slot_planner   # lazy: another builder's module
    now = _tz(now)
    d = target_date or (now.date() + timedelta(days=1) if morning_arm else now.date())
    delivered = []
    for letter, x in delivered_slots(d).items():
        if x.get("time"):
            delivered.append({"slot": letter, "time": x["time"], "date": d.isoformat(),
                              "file": x.get("file")})
    his = list(his_posts_today) if his_posts_today is not None else his_posts_on(d)
    hc = hold_count() if hold_count_ is None else int(hold_count_)
    result = slot_planner.plan(now, his, delivered, hc, morning_arm=morning_arm, target_date=d)
    result["delivered"] = delivered
    result["his_posts"] = [p.isoformat() for p in his]
    result["hold_count"] = hc
    return result


# --------------------------------------------------------------------------- CLI
def _parse_labels(items: Sequence[str] | None) -> dict:
    out: dict = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
        elif ":" in it:
            k, v = it.split(":", 1)
        else:
            raise argparse.ArgumentTypeError(f"--labels wants piece=label, got {it!r}")
        out[k.strip()] = v.strip()
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="py -m tools.deliver",
                                description="Deliver gated reels into post/<date>/ with POST_THIS.txt.")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--now", default=None, help="ISO time (default: the clock, ET)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--now", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", parents=[common], help="plan today's two slots")
    sp.add_argument("--posted", action="append", default=[], help="his post time today HH:MM or ISO")
    sp.add_argument("--hold", type=int, default=None, help="HOLD count (default: count hold/)")
    sp.add_argument("--morning-arm", action="store_true")
    sp.add_argument("--date", default=None)

    sd = sub.add_parser("deliver", parents=[common], help="deliver one reel to a slot")
    sd.add_argument("reel", type=Path)
    sd.add_argument("--chat", type=Path, required=True)
    sd.add_argument("--slot", required=True, choices=list(SLOTS))
    sd.add_argument("--when", required=True, help="slot time, ISO (naive = ET)")
    sd.add_argument("--kind", required=True, choices=list(KINDS))
    sd.add_argument("--hook", action="append", default=[], help="hook text (give two)")
    sd.add_argument("--caption-file", type=Path, default=None)
    sd.add_argument("--caption", default=None)
    sd.add_argument("--edl", type=Path, default=None)
    sd.add_argument("--labels", action="append", default=[], help="piece=label")
    sd.add_argument("--tests", default="")
    sd.add_argument("--force", default=None, help="reason to deliver past failing gates (logged)")
    sd.add_argument("--no-mirror", action="store_true")

    sh = sub.add_parser("hold", parents=[common], help="file a runner-up in hold/")
    sh.add_argument("reel", type=Path)
    sh.add_argument("--chat", type=Path, required=True)
    sh.add_argument("--edl", type=Path, default=None)
    sh.add_argument("--gates", type=Path, default=None)
    sh.add_argument("--name", required=True)
    sh.add_argument("--date", required=True)

    st = sub.add_parser("post-this", parents=[common], help="regenerate POST_THIS.txt for a date")
    st.add_argument("--date", required=True)
    st.add_argument("--no-mirror", action="store_true")

    sub.add_parser("list-hold", parents=[common], help="list the HOLD queue")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = parse_when(args.now) if args.now else datetime.now(config.TZ)
    try:
        if args.cmd == "plan":
            from . import slot_planner
            d = date.fromisoformat(args.date) if args.date else None
            his = None
            if args.posted:
                dd = d or now.date()
                his = [slot_planner._coerce_dt(x, dd) for x in args.posted]
            res = plan_today(now, his, args.hold, morning_arm=args.morning_arm, target_date=d)
            if args.json:
                print(json.dumps(res, indent=2, default=str))
            else:
                for line in slot_planner.post_this_lines(res):
                    print(line)
                for n in res.get("notes", []):
                    print("note: " + n)
            return 0
        if args.cmd == "deliver":
            if args.caption_file:
                caption = safeio.read_text(args.caption_file)
            elif args.caption is not None:
                caption = args.caption
            else:
                print("deliver: --caption-file or --caption is required", file=sys.stderr)
                return 1
            res = deliver(args.reel, args.chat, args.slot, parse_when(args.when), args.kind,
                          args.hook, caption, now, edl=args.edl, labels=_parse_labels(args.labels),
                          tests=args.tests, mirror=not args.no_mirror, force_reason=args.force)
            if args.json:
                print(json.dumps(res, indent=2, default=str))
            elif res["ok"]:
                print(f"delivered {res['id']} -> {res['files']['reel']}")
                for n in res.get("notes", []):
                    print("note: " + n)
            else:
                for r in res["refused"]:
                    print("REFUSED: " + r)
            return 0 if res["ok"] else 1
        if args.cmd == "hold":
            out = hold(args.reel, args.chat, args.edl, args.gates, args.name, args.date)
            print(json.dumps({"held": str(out)}) if args.json else f"held {out}")
            return 0
        if args.cmd == "post-this":
            res = write_post_this(args.date, now, mirror=not args.no_mirror)
            if args.json:
                print(json.dumps({"path": str(res["path"]), "drive_path": str(res["drive_path"]),
                                  "notes": res["notes"], "text": res["text"]}, indent=2))
            else:
                print(res["text"], end="")
                for n in res["notes"]:
                    print("note: " + n)
            return 0
        if args.cmd == "list-hold":
            rows = hold_list(now)
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['date']} {r['name']} age={r['age_days']} "
                          f"gates={'PASS' if r['gates_pass'] else 'FAIL'}")
                if not rows:
                    print("HOLD is empty")
            return 0
    except (safeio.SafeIOError, FileNotFoundError, ValueError, ImportError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
