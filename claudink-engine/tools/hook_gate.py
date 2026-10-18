"""Hook gate (START_HERE 6D G10, RULES D10; fixes WEAK_POINTS W5).

Why: on Oct 17 Claude delivered 10 B-roll hooks and 8 of them were already used, retired,
or a type that had already proven to lose on his page ($-hooks, "which one", brags, booking
announcements, generic reaction bait). The winners carried a concrete number, a stake, or
a question aimed at the viewer ("Was it worth the 50 hours?", "Would u tap on this spot?",
"drove from Jacksonville ... 10 hours"). This module makes that check mechanical.

What it checks for one hook (``check``):
- length <= config.HOOK_MAX_CHARS (55);
- fuzzy similarity to every burned hook (hooks.csv) and every used caption
  (captions_used.csv) stays below config.HOOK_FUZZY_MATCH (0.85);
- no losing type (LOSING_TYPES, each with the W5 evidence);
- at least one of: a number, a stake word, an address to the viewer;
- when frame-1 tags are given: a hook keyword must map to a tag that is really in frame 1;
- when pieces hours are given: an "N hours" / "N sessions" claim must exist in pieces.csv.

``check_pair`` applies it to both hooks of a reel and also requires the two hooks to differ
from each other. ``caption_tail_ok`` is the character-exact caption-tail check.

Library functions return data and never print. CLI: ``py -m tools.hook_gate "hook 1" ["hook 2"]
[--edl x.edl.json] [--caption "..."] [--json]``.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

from . import config
from . import safeio

# --------------------------------------------------------------------------- normalisation
_QUOTE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\u00ad": "-",
    "\u2026": "...",
    "\u00a0": " ",
}
_QUOTE_TABLE = str.maketrans(_QUOTE_MAP)

# punctuation kept by normalize(); everything else that is not alphanumeric/space is dropped
# (emoji, pictographs, arrows, variation selectors, zero-width joiners, ...)
_KEEP_PUNCT = set("'\"-.,!?$#%&/:;()+")
_TRAILING_PUNCT = " .,!?;:-\"'"
_NUMBER_RE = re.compile(r"\d(?:[\d,.]*\d)?")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[\w#$']+")


def _light(text: str) -> str:
    """NFKC + casefold + unified quotes/dashes + collapsed whitespace. Digits and symbols
    are kept, so the losing-type patterns ($, 1,200, 1-10) can still see them."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.translate(_QUOTE_TABLE)
    t = t.casefold()
    return _WS_RE.sub(" ", t).strip()


def normalize(text: str) -> str:
    """Canonical form used for fuzzy matching.

    NFKC, casefold, curly quotes and dashes unified, emoji and symbols stripped, whitespace
    collapsed, every digit run (incl. 1,200 / 1.5) replaced by "#", trailing punctuation
    stripped. "Best $1,200 you'll ever spend" -> "best $# you'll ever spend".
    """
    t = _light(text)
    t = "".join(c for c in t if c.isalnum() or c.isspace() or c in _KEEP_PUNCT)
    t = _NUMBER_RE.sub("#", t)
    t = _WS_RE.sub(" ", t).strip()
    return t.rstrip(_TRAILING_PUNCT).strip()


def _tokens(norm: str) -> list[str]:
    return [tok.strip("'") for tok in _TOKEN_RE.findall(norm) if tok.strip("'")]


def token_set_ratio(a: str, b: str) -> float:
    """fuzzywuzzy-style token set ratio on already-normalised strings (0.0..1.0)."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 1.0 if (not ta and not tb and a == b) else 0.0
    inter = " ".join(sorted(ta & tb))
    only_a = " ".join(sorted(ta - tb))
    only_b = " ".join(sorted(tb - ta))
    t0 = inter.strip()
    t1 = (inter + " " + only_a).strip()
    t2 = (inter + " " + only_b).strip()
    if not t0:
        return difflib.SequenceMatcher(None, t1, t2).ratio()
    return max(difflib.SequenceMatcher(None, t0, t1).ratio(),
               difflib.SequenceMatcher(None, t0, t2).ratio(),
               difflib.SequenceMatcher(None, t1, t2).ratio())


def similarity(a: str, b: str) -> float:
    """max(SequenceMatcher ratio, token-set ratio) on the normalised strings."""
    na, nb = normalize(a), normalize(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    return max(seq, token_set_ratio(na, nb))


# --------------------------------------------------------------------------- losing types
# (name, compiled regex on the light-normalised hook, evidence quoting WEAK_POINTS W5)
LOSING_TYPES: list[tuple[str, re.Pattern, str]] = [
    ("price",
     re.compile(r"\$|\bprice\b|\bcosts?\b|\b1,?200\b|\bday rate\b|what\s+\$.*gets you"),
     'W5: "Best $1,200 you\'ll ever spend": used Oct 7. The $-hook on a frame-1 short was '
     'retired 10/9. "This is what $1,200 a day gets you": a $-hook. Skill section 7: no price hooks.'),
    ("which-one",
     re.compile(r"which one|which would you|rate my|rate these|\b1\s*-\s*10\b|pick one|\btop 3\b"),
     'W5: "Which one would you get? 1-7": S02, 1,159. The "which one" family runs 47-75% skip. '
     '"Rate these 1-10" was used Oct 7.'),
    ("brag",
     re.compile(r"hits different|booked out|for a reason|\bbest in\b|did i cook|\bcooked\b"),
     'W5: "Booked out for a reason": used Oct 7. "Black and grey realism hits different": a brag '
     'type. The brag shorts got 146-385.'),
    ("booking",
     re.compile(r"books open|now booking|\bbooking\b|slots open|book now"),
     'W5: booking announcements flop (74.8% skip).'),
    ("reaction-bait",
     re.compile(r"be honest|what do you think|was it worth it|thoughts\?"),
     'W1/W5: station montages with generic captions ("Did I cook?", "Be honest", "Which one?") '
     'all sat at the floor. Reaction bait only counts as losing when the hook has no number.'),
]

_NUMBER_ONLY_EXEMPT = {"reaction-bait"}   # these types are fine when the hook carries a number

_STAKES_RE = re.compile(
    r"\b(hours?|hrs?|sessions?|drove|miles|flew|years|first tattoo|scared|pain|hurt|sit|worth|"
    r"healed|months|days)\b|\d+\s*am\b|all night")
_VIEWER_RE = re.compile(r"\byou\b|\byour\b|\byou'?re\b|would you|could you|\btap\b|\bu\b")
_HOURS_CLAIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|sessions?)\b")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# hook keyword -> frame-1 tags that satisfy it (EDL TAGS vocabulary)
FRAME1_KEYWORDS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"\bhours?\b|\bhrs?\b|\bsessions?\b"), ("hours", "stakes")),
    (re.compile(r"\bdesign\b|\bstencil\b|\bprint\b"), ("design", "stencil")),
    (re.compile(r"\bhealed\b"), ("healed",)),
    (re.compile(r"\bfirst tattoo\b"), ("first-tattoo",)),
    (re.compile(r"\bpain\b|\bhurts?\b|\btap\b"), ("pain",)),
    (re.compile(r"\bdrove\b|\bflew\b|\bmiles\b|\bfrom\b"), ("travel",)),
    (re.compile(r"\bface\b|\bgreeting\b"), ("greeting",)),
]


def has_number(hook: str) -> bool:
    return bool(re.search(r"\d", _light(hook)))


def has_stakes(hook: str) -> bool:
    return bool(_STAKES_RE.search(_light(hook)))


def addresses_viewer(hook: str) -> bool:
    return bool(_VIEWER_RE.search(_light(hook)))


def losing_types(hook: str) -> list[str]:
    """Names of every losing type the hook falls into."""
    light = _light(hook)
    num = has_number(hook)
    out = []
    for name, rx, _evidence in LOSING_TYPES:
        if rx.search(light):
            if name in _NUMBER_ONLY_EXEMPT and num:
                continue
            out.append(name)
    return out


def losing_evidence(name: str) -> str:
    for n, _rx, ev in LOSING_TYPES:
        if n == name:
            return ev
    return ""


def frame1_required_tags(hook: str) -> set[str]:
    """Tags frame 1 must carry for this hook to be honest; empty when no keyword maps."""
    light = _light(hook)
    tags: set[str] = set()
    for rx, mapped in FRAME1_KEYWORDS:
        if rx.search(light):
            tags.update(mapped)
    return tags


def hours_claims(hook: str) -> list[float]:
    """Numbers the hook claims as hours or sessions ("8 hours", "2 sessions")."""
    return [float(m.group(1)) for m in _HOURS_CLAIM_RE.finditer(_light(hook))]


def known_hour_values(pieces_hours: dict) -> set[float]:
    """Every number found in pieces.csv values. Values may be numbers, strings
    ("7", "1 session / 7 hours") or dicts ({"hours": "7", "sessions": "1"})."""
    vals: set[float] = set()

    def _collect(v) -> None:
        if v is None:
            return
        if isinstance(v, dict):
            for x in v.values():
                _collect(x)
            return
        if isinstance(v, (list, tuple, set)):
            for x in v:
                _collect(x)
            return
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            vals.add(float(v))
            return
        for m in _NUM_RE.finditer(str(v)):
            vals.add(float(m.group(0)))

    _collect(pieces_hours)
    return vals


def best_match(hook: str, candidates: Iterable[str]) -> tuple[float, str | None]:
    best, who = 0.0, None
    for c in candidates:
        if not c:
            continue
        s = similarity(hook, c)
        if s > best:
            best, who = s, c
    return best, who


# --------------------------------------------------------------------------- the gate
def check(hook: str, burned: Sequence[str], captions: Sequence[str],
          frame1_tags: Sequence[str] | None = None,
          pieces_hours: dict | None = None) -> dict:
    """Gate one hook. Returns a dict; ``ok`` is True only when every rule passes.

    reasons: why it failed (empty when ok). notes: non-fatal remarks.
    """
    hook = (hook or "").strip()
    reasons: list[str] = []
    notes: list[str] = []
    length = len(hook)

    if not hook:
        reasons.append("empty hook")
    if length > config.HOOK_MAX_CHARS:
        reasons.append(f"length {length} > {config.HOOK_MAX_CHARS}")

    sim_burned, who_b = best_match(hook, burned or [])
    sim_caps, who_c = best_match(hook, captions or [])
    if sim_burned >= sim_caps:
        sim_max, who, src = sim_burned, who_b, "burned hook"
    else:
        sim_max, who, src = sim_caps, who_c, "used caption"
    matched = who if (who is not None and sim_max >= config.HOOK_FUZZY_MATCH) else None
    if matched is not None:
        reasons.append(f"{sim_max:.2f} similar to {src}: {matched!r}")

    lt = losing_types(hook)
    for name in lt:
        reasons.append(f"losing type {name}: {losing_evidence(name)}")

    num, stakes, viewer = has_number(hook), has_stakes(hook), addresses_viewer(hook)
    if hook and not (num or stakes or viewer):
        reasons.append("no number, no stakes, no question to the viewer (W5)")

    frame1_match: bool | None = None
    if frame1_tags is not None:
        required = frame1_required_tags(hook)
        present = {str(t).strip().lower() for t in frame1_tags}
        if not required:
            frame1_match = None
            notes.append("hook maps to no frame-1 keyword; frame-1 match not checked")
        elif required & present:
            frame1_match = True
        else:
            frame1_match = False
            reasons.append(f"hook promises {sorted(required)} but frame 1 shows "
                           f"{sorted(present) or 'nothing tagged'}")

    if pieces_hours is not None:
        claims = hours_claims(hook)
        if claims:
            known = known_hour_values(pieces_hours)
            missing = [c for c in claims if c not in known]
            if missing:
                shown = ", ".join(str(int(m) if m.is_integer() else m) for m in missing)
                reasons.append(f"hours not in pieces.csv: {shown}")

    return {
        "ok": not reasons,
        "hook": hook,
        "reasons": reasons,
        "notes": notes,
        "length": length,
        "similarity_max": round(sim_max, 3),
        "matched": matched,
        "losing_types": lt,
        "has_number": num,
        "has_stakes": stakes,
        "addresses_viewer": viewer,
        "frame1_match": frame1_match,
    }


def check_pair(h1: str, h2: str, burned: Sequence[str], captions: Sequence[str],
               frame1_tags: Sequence[str] | None = None,
               pieces_hours: dict | None = None) -> dict:
    """Both hooks must pass and must not be near-duplicates of each other (< 0.85)."""
    r1 = check(h1, burned, captions, frame1_tags, pieces_hours)
    r2 = check(h2, burned, captions, frame1_tags, pieces_hours)
    pair_sim = similarity(h1 or "", h2 or "")
    reasons: list[str] = []
    if not r1["ok"]:
        reasons.append("hook 1: " + "; ".join(r1["reasons"]))
    if not r2["ok"]:
        reasons.append("hook 2: " + "; ".join(r2["reasons"]))
    if pair_sim >= config.HOOK_FUZZY_MATCH:
        reasons.append(f"hooks are {pair_sim:.2f} similar to each other; give two angles")
    return {"ok": not reasons, "hook1": r1, "hook2": r2,
            "pair_similarity": round(pair_sim, 3), "reasons": reasons}


def caption_tail_ok(caption: str) -> bool:
    """The caption must end with config.CAPTION_TAIL, character for character."""
    if caption is None:
        return False
    return caption.rstrip().endswith(config.CAPTION_TAIL)


# --------------------------------------------------------------------------- ledger access
def _ledger_rows(name: str, csv_path: Path | None = None) -> list[dict]:
    """Rows of ledgers/<name>.csv. Uses tools.ledgers when that module exists (lazy), else
    reads the CSV directly through safeio. Missing file -> []."""
    if csv_path is not None:
        return safeio.read_csv(csv_path)
    try:
        from . import ledgers  # noqa: F401  (another builder's module; may not exist yet)
    except ImportError:
        ledgers = None
    if ledgers is not None:
        for fn in ("read", "load", "rows", "read_rows", "read_ledger"):
            f = getattr(ledgers, fn, None)
            if callable(f):
                try:
                    rows = f(name)
                except Exception:
                    continue
                if isinstance(rows, list):
                    return rows
    return safeio.read_csv(config.LEDGERS / f"{name}.csv")


def burned_hooks(csv_path: Path | None = None) -> list[str]:
    """Every hook already used (any status: burned, retired, losing_type, ok)."""
    rows = _ledger_rows("hooks", csv_path)
    out = []
    for r in rows:
        h = r.get("hook", "") if isinstance(r, dict) else str(r)
        if h and h.strip():
            out.append(h.strip())
    return out


def used_captions(csv_path: Path | None = None) -> list[str]:
    rows = _ledger_rows("captions_used", csv_path)
    out = []
    for r in rows:
        c = r.get("caption", "") if isinstance(r, dict) else str(r)
        if c and c.strip():
            out.append(c.strip())
    return out


def pieces_hours_from_rows(rows: Iterable[dict], only: Sequence[str] | None = None) -> dict:
    """{piece: {"hours": ..., "sessions": ...}} from pieces.csv rows, skipping unknown hours.
    ``only`` restricts it to the pieces shown in a reel."""
    want = {p.strip().lower() for p in only} if only else None
    out: dict = {}
    for r in rows:
        piece = (r.get("piece") or "").strip()
        if not piece:
            continue
        if want is not None and piece.lower() not in want:
            continue
        hours = (r.get("hours") or "").strip()
        sessions = (r.get("sessions") or "").strip()
        if not hours and not sessions:
            continue
        out[piece] = {"hours": hours, "sessions": sessions}
    return out


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="py -m tools.hook_gate",
        description="Hook gate (G10 / W5): burned-hook ledger, losing types, number/stakes/"
                    "viewer rule, frame-1 match, pieces.csv hours, caption tail.")
    p.add_argument("hooks", nargs="*", help="one or two hooks (taken from --edl when omitted)")
    p.add_argument("--edl", type=Path, help="EDL json: hooks, caption, frame-1 tags, pieces")
    p.add_argument("--caption", help="caption to check the exact tail on")
    p.add_argument("--frame1-tags", help="comma-separated tags present in frame 1")
    p.add_argument("--hooks-csv", type=Path, help="hooks.csv to use instead of the ledger")
    p.add_argument("--captions-csv", type=Path, help="captions_used.csv instead of the ledger")
    p.add_argument("--pieces-csv", type=Path, help="pieces.csv instead of the ledger")
    p.add_argument("--no-ledgers", action="store_true", help="do not read any ledger")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    hooks = list(args.hooks)
    caption = args.caption
    frame1_tags = ([t.strip() for t in args.frame1_tags.split(",") if t.strip()]
                   if args.frame1_tags else None)
    edl_pieces: list[str] | None = None

    if args.edl:
        edl = safeio.read_json(args.edl, default=None)
        if not isinstance(edl, dict):
            sys.stderr.write(f"cannot read EDL: {args.edl}\n")
            return 1
        if not hooks:
            hooks = [h for h in (edl.get("hooks") or []) if h][:2]
        if caption is None:
            caption = edl.get("caption")
        if frame1_tags is None:
            segs = edl.get("segs") or []
            if segs and isinstance(segs[0], dict):
                frame1_tags = [str(t) for t in (segs[0].get("tags") or [])]
        edl_pieces = [str(p) for p in (edl.get("pieces") or [])] or None

    if not hooks:
        sys.stderr.write("no hook given (pass one or two hooks, or --edl)\n")
        return 1
    if len(hooks) > 2:
        sys.stderr.write("at most two hooks per reel\n")
        return 1

    if args.no_ledgers:
        burned, captions, pieces_rows = [], [], []
    else:
        burned = burned_hooks(args.hooks_csv)
        captions = used_captions(args.captions_csv)
        pieces_rows = _ledger_rows("pieces", args.pieces_csv)
    pieces_hours = pieces_hours_from_rows(pieces_rows, edl_pieces) if pieces_rows else None

    if len(hooks) == 2:
        result = check_pair(hooks[0], hooks[1], burned, captions, frame1_tags, pieces_hours)
        per = [result["hook1"], result["hook2"]]
    else:
        r = check(hooks[0], burned, captions, frame1_tags, pieces_hours)
        result = {"ok": r["ok"], "hook1": r, "hook2": None, "pair_similarity": None,
                  "reasons": list(r["reasons"])}
        per = [r]

    tail_ok: bool | None = None
    if caption is not None:
        tail_ok = caption_tail_ok(caption)
        if not tail_ok:
            result["reasons"].append("caption tail does not match config.CAPTION_TAIL exactly")
            result["ok"] = False
    result["caption_tail_ok"] = tail_ok
    result["burned_count"] = len(burned)
    result["captions_count"] = len(captions)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for r in per:
            state = "PASS" if r["ok"] else "FAIL"
            why = ("; ".join(r["reasons"]) if r["reasons"] else "; ".join(r["notes"]))
            print(f"{state} [{r['length']}ch sim={r['similarity_max']:.2f}] {r['hook']}"
                  + (f"  <- {why}" if why else ""))
        if result.get("pair_similarity") is not None:
            print(f"pair similarity {result['pair_similarity']:.2f}"
                  + (" (too close)" if result["pair_similarity"] >= config.HOOK_FUZZY_MATCH else ""))
        if tail_ok is not None:
            print("caption tail " + ("ok" if tail_ok else "WRONG"))
        print("OK" if result["ok"] else "BLOCKED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
