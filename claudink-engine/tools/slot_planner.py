"""Slot planner: the two daily posting slots (START_HERE 5 step 6, 9, 10; fixes W2/W3/W4/W12).

Why: his outliers went up alone in daytime; bursts (3+ reels in an hour) halved the median and
the hit rate; zero-post days cost 70% of reach in two days; batch deliveries fed the bursts.
So the engine delivers exactly two reels a day, each with a dated slot: A (SEND, a session
story) alone around 11:00 ET and B (SAVE/TRUST) at least 3h later, nothing after 21:00,
never more than 2 posts a day counting what he already posted himself.

``plan(now, his_posts_today, delivered, hold_count, morning_arm=False, target_date=None)``
returns the slots as data. Every time stamp is passed in (``now``) so tests are deterministic.
All rounding and spacing is done on aware datetimes in config.TZ (DST-safe: spacing is
computed in UTC, rounding on the ET wall clock).

Rules, all from config:
- max MAX_POSTS_PER_DAY (2) posts per calendar day, counting his posts and our deliveries;
- A: earliest :00/:30 at or after max(now + MIN_LEAD_MINUTES, 11:00); if the 11:00-11:30 window
  has passed, A = now + 45 min rounded up to the next :00/:30;
- B: >= A + MIN_GAP_HOURS (3h), inside 14:30-16:00 when possible, else the earliest :00/:30
  >= A + 3h that is still <= 21:00;
- every slot >= now + 45 min, nothing after 21:00 (LATEST_POST_HOUR);
- if he posted today: remaining = 2 - n, next slot >= last post + 3h;
- if only one slot fits today: one today, the other is tomorrow's first free slot (A at 11:00
  unless tomorrow's A is already delivered in post/<tomorrow>/);
- if none fit (A would land after 21:00, i.e. now > 20:15): tomorrow 11:00 and 14:30, again
  minus whatever is already delivered for tomorrow (never a 3rd reel on a date);
- morning_arm (experiment X1): A for TOMORROW at 07:00 "post tomorrow ~07:00, alone", B 14:30;
- never two slots of the same letter on one date; a delivered A in post/<date>/ keeps its time
  and only B is planned (idempotent re-runs);
- HOLD below HOLD_MIN (4) adds the note "refill HOLD".

CLI: ``py -m tools.slot_planner --now 2026-10-19T06:40 [--posted 09:12 ...] [--delivered A=11:00]
[--hold 2] [--morning-arm] [--date 2026-10-20] --json``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from . import config

MINUTE_STEP = 30                     # slots land on :00 / :30
MORNING_ARM_TIME = "07:00"           # X1 morning arm, inside config.X1_MORNING_WINDOW
MORNING_ARM_REASON = "post tomorrow ~07:00, alone"
_DELIVERED_RE = re.compile(r"^([AB])_(\d{2})(\d{2})_.+\.mp4$", re.IGNORECASE)


# --------------------------------------------------------------------------- time helpers
def _tz(dt: datetime) -> datetime:
    """An aware datetime expressed in config.TZ. Naive input is refused."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime; pass an aware datetime in {config.TZ_NAME}: {dt!r}")
    return dt.astimezone(config.TZ)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(config.UTC)


def _add(dt: datetime, delta: timedelta) -> datetime:
    """Absolute-time addition (via UTC) so a DST change never shortens or stretches a gap."""
    return (_utc(dt) + delta).astimezone(config.TZ)


def _le(a: datetime, b: datetime) -> bool:
    return _utc(a) <= _utc(b)


def _lt(a: datetime, b: datetime) -> bool:
    return _utc(a) < _utc(b)


def _max(*dts: datetime) -> datetime:
    best = dts[0]
    for d in dts[1:]:
        if _lt(best, d):
            best = d
    return best


def _hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def at(d: date, hhmm: str) -> datetime:
    """Aware datetime for a wall-clock time on a date, in config.TZ."""
    h, m = _hhmm(hhmm)
    return datetime.combine(d, time(h, m), tzinfo=config.TZ)


def round_up_slot(dt: datetime) -> datetime:
    """Round up to the next :00 or :30 on the ET wall clock (a time already on the grid stays)."""
    dt = _tz(dt)
    if dt.second == 0 and dt.microsecond == 0 and dt.minute % MINUTE_STEP == 0:
        return dt
    base = dt.replace(second=0, microsecond=0)
    if dt.minute < MINUTE_STEP:
        return base.replace(minute=MINUTE_STEP)
    return _add(base.replace(minute=0), timedelta(hours=1))


def format_slot(dt: datetime) -> str:
    """'11:00 ET'."""
    return _tz(dt).strftime("%H:%M") + " ET"


def catchup(now: datetime) -> bool:
    """True when the run is late: slot A's default window (11:00-11:30) can no longer be hit
    with the 45-minute lead (START_HERE 10: plan the next valid slots, never a passed one)."""
    now = _tz(now)
    lead = _add(now, timedelta(minutes=config.MIN_LEAD_MINUTES))
    return _lt(at(now.date(), config.SLOT_A_WINDOW[1]), lead)


def _latest(d: date) -> datetime:
    return at(d, f"{config.LATEST_POST_HOUR:02d}:00")


def _slot(letter: str, when: datetime, reason: str, *, existing: bool = False,
          file: str | None = None) -> dict:
    when = _tz(when)
    kind = "SEND" if letter == "A" else "SAVE"
    s = {"slot": letter, "kind": kind, "time": when.isoformat(), "date": when.date().isoformat(),
         "label": format_slot(when), "reason": reason, "existing": existing}
    if file:
        s["file"] = file
    return s


# --------------------------------------------------------------------------- inputs
def _coerce_dt(v, d: date | None = None) -> datetime:
    """Accept an aware datetime, a naive one (taken as ET), an ISO string, or 'HH:MM' on date d."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=config.TZ)
        return _tz(v)
    s = str(v).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        if d is None:
            raise ValueError(f"'{s}' needs a date")
        return at(d, s)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return _tz(dt)


def _normalize_delivered(delivered: Iterable[dict] | None, d: date) -> tuple[list[dict], list[str]]:
    """Delivered entries as {slot, time (aware), date, file} plus a note for every entry that
    was ignored (bad letter, missing or unparsable time, a scan_delivered 'error' marker), so
    the daily run sees 'ignored delivered entry ...' instead of a silently shorter list.
    A bare 'HH:MM' time without a 'date' is taken on date ``d``."""
    out: list[dict] = []
    notes: list[str] = []
    for item in delivered or []:
        if not isinstance(item, dict):
            notes.append(f"ignored delivered entry {item!r}: not a mapping")
            continue
        label = item.get("file") or item.get("slot") or repr(item)
        if item.get("error"):
            notes.append(f"ignored delivered entry {label}: {item['error']}")
            continue
        letter = str(item.get("slot", "")).strip().upper()[:1]
        if letter not in ("A", "B"):
            notes.append(f"ignored delivered entry {label}: slot {item.get('slot')!r} is not A or B")
            continue
        if item.get("time") in (None, ""):
            notes.append(f"ignored delivered entry {label}: no time")
            continue
        on = d
        if item.get("date"):
            dd = item["date"]
            try:
                on = dd if isinstance(dd, date) else date.fromisoformat(str(dd))
            except ValueError:
                notes.append(f"ignored delivered entry {label}: bad date {dd!r}")
                continue
        try:
            when = _coerce_dt(item["time"], on)        # a bare HH:MM lands on the entry's date
        except ValueError as e:
            notes.append(f"ignored delivered entry {label}: bad time {item.get('time')!r} ({e})")
            continue
        when_date = on if item.get("date") else when.date()
        out.append({"slot": letter, "time": when, "date": when_date,
                    "file": item.get("file")})
    return out, notes


def scan_delivered(d: date, post_dir: Path | None = None) -> list[dict]:
    """Reels already delivered for a date: post/<date>/A_1100_name.mp4 (chat copies ignored).
    A file whose HHMM is out of range (A_9999_x.mp4) is returned with time None and an
    'error' marker instead of raising, so one hand-renamed file cannot abort the plan."""
    folder = (post_dir or config.POST) / d.isoformat()
    out = []
    if not folder.is_dir():
        return out
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower().endswith("_chat.mp4"):
            continue
        m = _DELIVERED_RE.match(p.name)
        if not m:
            continue
        letter, hh, mm = m.group(1).upper(), int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            out.append({"slot": letter, "time": None, "date": d, "file": p.name,
                        "error": f"bad time {m.group(2)}{m.group(3)} in the file name"})
            continue
        out.append({"slot": letter, "time": at(d, f"{hh:02d}:{mm:02d}"), "date": d,
                    "file": p.name})
    return out


def hold_count_on_disk(hold_dir: Path | None = None) -> int:
    """Rule-passing cuts waiting in hold/ (top level only; hold/next/ is tomorrow's pair)."""
    folder = hold_dir or config.HOLD
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.iterdir()
               if p.is_file() and p.suffix.lower() == ".mp4"
               and not p.name.lower().endswith("_chat.mp4"))


# --------------------------------------------------------------------------- the plan
_MAX_HOPS = 3     # how many days forward plan() may look when nothing fits the target date


def _next_day(now: datetime, his: Sequence[datetime], deliv_all: Sequence[dict],
              hold_count: int, nd: date, hops: int) -> dict:
    """The plan for the next date, with the same rules (cap, delivered A/B, his posts)."""
    return plan(now, his, deliv_all, hold_count, morning_arm=False, target_date=nd, _hops=hops + 1)


def _carry_notes(sub: dict) -> list[str]:
    """Notes of a next-day sub-plan worth repeating in the parent (never its HOLD note)."""
    return [n for n in sub.get("notes", []) if not n.startswith("refill HOLD")]


def plan(now: datetime, his_posts_today: Sequence[datetime] | None, delivered: Sequence[dict] | None,
         hold_count: int, morning_arm: bool = False, target_date: date | None = None,
         _hops: int = 0) -> dict:
    """Plan today's two slots. See the module docstring for the rules.

    ``delivered`` entries may carry any date; only the target date's are kept here, the next
    date's are consulted when a slot spills to tomorrow (never a 3rd reel on a date)."""
    now = _tz(now)
    today = now.date()
    d = target_date or today
    if morning_arm and target_date is None:
        d = today + timedelta(days=1)
    tomorrow = d + timedelta(days=1)
    max_posts = config.MAX_POSTS_PER_DAY
    gap = timedelta(hours=config.MIN_GAP_HOURS)
    lead = _add(now, timedelta(minutes=config.MIN_LEAD_MINUTES))
    notes: list[str] = []
    slots: list[dict] = []
    blocked: str | None = None

    his = sorted((_tz(p) for p in (his_posts_today or [])), key=_utc)
    his_on_d = [p for p in his if p.date() == d]
    n_his = len(his_on_d)
    deliv_all, deliv_notes = _normalize_delivered(delivered, d)
    if _hops == 0:
        notes.extend(deliv_notes)
    deliv = [x for x in deliv_all if x["date"] == d]
    deliv_by = {x["slot"]: x for x in deliv}
    if len(deliv) != len(deliv_by):
        notes.append("WARNING: two delivered reels share a slot letter; keeping the first of each")
        seen: dict = {}
        for x in deliv:
            seen.setdefault(x["slot"], x)
        deliv_by = seen
        deliv = list(seen.values())

    posts_allowed_today = max(0, max_posts - n_his)
    to_plan = max(0, max_posts - n_his - len(deliv))

    for x in sorted(deliv, key=lambda x: _utc(x["time"])):
        reason = f"already delivered in post/{d.isoformat()}/; keeps its time"
        for p in his_on_d:
            if abs((_utc(x["time"]) - _utc(p)).total_seconds()) < gap.total_seconds():
                earliest = _add(p, gap)
                reason += f"; WARNING: < 3h from his {format_slot(p)} post, tell him >= {format_slot(earliest)}"
        slots.append(_slot(x["slot"], x["time"], reason, existing=True, file=x.get("file")))

    if hold_count < config.HOLD_MIN:
        notes.append(f"refill HOLD ({hold_count}/{config.HOLD_MIN})")

    full = "already 2 today" if d == today else f"already 2 on {d.isoformat()}"
    if n_his >= max_posts:
        blocked = full
        notes.append(f"he posted {n_his}x on {d.isoformat()} ({', '.join(format_slot(p) for p in his_on_d)}); "
                     "nothing more today")
        return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)
    if to_plan <= 0:
        blocked = full
        notes.append(f"{n_his} his + {len(deliv)} delivered on {d.isoformat()}; nothing to plan")
        return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)

    # ---- morning arm (X1): tomorrow 07:00 alone + 14:30, delivered the day before
    if morning_arm:
        if "A" not in deliv_by:
            a_time = at(d, MORNING_ARM_TIME)
            if _lt(a_time, lead):
                a_time = round_up_slot(lead)
                notes.append("morning arm requested too late for 07:00; A moved to the next valid slot")
            slots.append(_slot("A", a_time, MORNING_ARM_REASON + " (X1 morning arm)"))
        else:
            a_time = deliv_by["A"]["time"]
        if to_plan >= 2 or ("A" in deliv_by and "B" not in deliv_by):
            b_time = _max(_add(a_time, gap), at(d, config.SLOT_B_WINDOW[0]), lead)
            b_time = round_up_slot(b_time)
            if _le(b_time, _latest(d)):
                slots.append(_slot("B", b_time, "X1 morning arm: B >= 3h after A, 14:30-16:00"))
            else:
                notes.append("morning arm: no B fits that day")
        return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)

    # ---- normal day
    # earliest: lead and 3h after his last post; after_deliv: also 3h after every delivery
    earliest = lead
    if his_on_d:
        earliest = _max(earliest, _add(his_on_d[-1], gap))
        notes.append(f"he posted at {format_slot(his_on_d[-1])}; next slot >= 3h later")
    after_deliv = earliest
    for x in deliv:
        after_deliv = _max(after_deliv, _add(x["time"], gap))

    latest = _latest(d)
    a_existing = deliv_by.get("A")
    b_existing = deliv_by.get("B")
    a_before_b = False
    if a_existing is not None:
        a_time = a_existing["time"]
        a_fits = True
        notes.append("A already delivered; planning B only")
    else:
        a_time = round_up_slot(_max(earliest, at(d, config.SLOT_A_WINDOW[0])))
        if b_existing is not None:
            # B delivered without an A: A goes BEFORE B when >= 3h fit, else after B + 3h
            if _le(_add(a_time, gap), b_existing["time"]):
                a_before_b = True
                notes.append(f"B already delivered at {format_slot(b_existing['time'])}; "
                             "A planned >= 3h before it")
            else:
                a_time = round_up_slot(_max(after_deliv, at(d, config.SLOT_A_WINDOW[0])))
                notes.append(f"B already delivered at {format_slot(b_existing['time'])} and A does "
                             "not fit >= 3h before it; A planned >= 3h after B (clock order inverted)")
        a_fits = _le(a_time, latest)

    if not a_fits:
        # nothing fits on d: the same rules for the next date (its deliveries and his posts count)
        why = (f"no slot fits {d.isoformat()} (earliest valid {format_slot(earliest)} is after "
               f"{config.LATEST_POST_HOUR:02d}:00)")
        if _hops >= _MAX_HOPS:
            blocked = f"no slot within {_MAX_HOPS} days"
            notes.append(why + f"; gave up after {_MAX_HOPS} days")
            return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)
        sub = _next_day(now, his, deliv_all, hold_count, tomorrow, _hops)
        notes.append(why + f"; planned {sub['date']}")
        notes.extend(_carry_notes(sub))
        for s in sub["slots"]:
            if not s["existing"]:
                s = dict(s, reason=f"{sub['date']}: {s['reason']} ({why})")
            slots.append(s)
        if sub["blocked"]:
            blocked = sub["blocked"]
        return _result(date.fromisoformat(sub["date"]), today, now, slots, posts_allowed_today,
                       notes, blocked, morning_arm)

    if a_existing is None:
        in_window = _le(a_time, at(d, config.SLOT_A_WINDOW[1]))
        if in_window:
            reason = "A window 11:00-11:30, posted alone"
        elif his_on_d and _le(_add(his_on_d[-1], gap), a_time) and _lt(lead, _add(his_on_d[-1], gap)):
            reason = f"earliest :00/:30 >= 3h after his {format_slot(his_on_d[-1])} post, posted alone"
        else:
            reason = f"A window passed; now + {config.MIN_LEAD_MINUTES} min rounded up to :00/:30"
            if his_on_d:
                reason += f"; >= 3h after his {format_slot(his_on_d[-1])} post"
        if b_existing is not None:
            reason += ("; >= 3h before the delivered B" if a_before_b
                       else "; >= 3h after the delivered B")
        slots.append(_slot("A", a_time, reason))
        to_plan -= 1

    if to_plan <= 0:
        if n_his:
            notes.append("one slot today (his post took the other)")
        return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)

    b_min = _max(_add(a_time, gap), lead, earliest)
    b_time = round_up_slot(_max(b_min, at(d, config.SLOT_B_WINDOW[0])))
    if _le(b_time, at(d, config.SLOT_B_WINDOW[1])):
        slots.append(_slot("B", b_time, "B window 14:30-16:00, >= 3h after A"))
    elif _le(b_time, latest):
        slots.append(_slot("B", b_time, f"B window passed; earliest :00/:30 >= A + 3h and <= "
                                        f"{config.LATEST_POST_HOUR:02d}:00"))
    else:
        # only one slot fits d: the second comes from the next date's own plan, so a slot
        # already delivered there (post/<tomorrow>/) is kept and never doubled
        why = (f"B did not fit {d.isoformat()} (A + 3h is after {config.LATEST_POST_HOUR:02d}:00)")
        if _hops >= _MAX_HOPS:
            notes.append(f"only one slot fits {d.isoformat()}; {why}; gave up looking further")
            return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)
        sub = _next_day(now, his, deliv_all, hold_count, tomorrow, _hops)
        existing_next = [s for s in sub["slots"] if s["existing"]]
        new_next = [s for s in sub["new_slots"] if s["date"] == tomorrow.isoformat()]
        slots.extend(existing_next)
        if new_next:
            pick = new_next[0]
            notes.append(f"only one slot fits {d.isoformat()}; second is tomorrow's "
                         f"{pick['slot']} at {pick['label']}")
            slots.append(dict(pick, reason=f"{pick['date']}: {pick['reason']} ({why})"))
        else:
            notes.append(f"only one slot fits {d.isoformat()}; {why}; nothing free on "
                         f"{tomorrow.isoformat()} ({sub['blocked'] or 'no new slot'})")
        notes.extend(n for n in _carry_notes(sub) if "WARNING" in n or n.startswith("ignored"))
    return _result(d, today, now, slots, posts_allowed_today, notes, blocked, morning_arm)


def _result(d: date, today: date, now: datetime, slots: list[dict], allowed: int,
            notes: list[str], blocked: str | None, morning_arm: bool) -> dict:
    slots = sorted(slots, key=lambda s: s["time"])
    return {
        "date": d.isoformat(),
        "today": today.isoformat(),
        "now": now.isoformat(),
        "slots": slots,
        "new_slots": [s for s in slots if not s["existing"]],
        "posts_allowed_today": allowed,
        "notes": notes,
        "blocked": blocked,
        "morning_arm": morning_arm,
        "catchup": catchup(now),
    }


def post_this_lines(result: dict) -> list[str]:
    """Short lines for POST_THIS.txt: 'A (SEND): 11:00 ET', 'B (SAVE): post >=3h after A (earliest 14:30)'."""
    lines = []
    a_label = None
    for s in result["slots"]:
        if s["slot"] == "A":
            a_label = s["label"]
            lines.append(f"A (SEND): {s['label']} on {s['date']}, alone")
        else:
            lines.append(f"B (SAVE): post >=3h after you post A (earliest {s['label']}) on {s['date']}")
    if result.get("blocked"):
        lines.insert(0, f"Nothing to post today: {result['blocked']}")
    for n in result.get("notes", []):
        if n.startswith("refill HOLD"):
            lines.append(n)
    return lines


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="py -m tools.slot_planner",
        description="Plan today's two posting slots (A SEND ~11:00 alone, B SAVE >= 3h later).")
    p.add_argument("--now", help="ISO time, e.g. 2026-10-19T06:40 (ET when no offset); default: now")
    p.add_argument("--posted", nargs="*", default=[], help="his posts today, HH:MM or ISO")
    p.add_argument("--delivered", nargs="*", default=None,
                   help="already delivered slots, e.g. A=11:00; default: scan post/<date>/")
    p.add_argument("--hold", type=int, default=None, help="HOLD count; default: count hold/*.mp4")
    p.add_argument("--morning-arm", action="store_true", help="X1: A tomorrow 07:00 alone, B 14:30")
    p.add_argument("--date", help="target date YYYY-MM-DD (default: today, or tomorrow with --morning-arm)")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        now = _coerce_dt(args.now) if args.now else datetime.now(config.TZ)
        target = date.fromisoformat(args.date) if args.date else None
        d = target or (now.date() + timedelta(days=1) if args.morning_arm else now.date())
        posted = [_coerce_dt(s, now.date()) for s in args.posted]
        if args.delivered is None:
            delivered = scan_delivered(d)
        else:
            delivered = []
            for item in args.delivered:
                letter, _, when = item.partition("=")
                # raw time: an unparsable one becomes an "ignored delivered entry" note
                delivered.append({"slot": letter.strip().upper(), "time": when.strip(), "date": d,
                                  "file": item})
        hold = args.hold if args.hold is not None else hold_count_on_disk()
        result = plan(now, posted, delivered, hold, morning_arm=args.morning_arm, target_date=target)
    except (ValueError, OSError) as e:
        sys.stderr.write(f"slot_planner: {e}\n")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for s in result["slots"]:
            tag = "kept" if s["existing"] else "plan"
            print(f"{tag} {s['slot']} {s['kind']} {s['date']} {s['label']}  {s['reason']}")
        for n in result["notes"]:
            print(f"note: {n}")
        if result["blocked"]:
            print(f"BLOCKED: {result['blocked']}")
    return 0 if (result["slots"] or result["blocked"]) else 1


if __name__ == "__main__":
    sys.exit(main())
