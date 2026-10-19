"""EDL (edit decision list) contract for the ClaudInk Reel Engine (SPEC.md "Shared data contracts").

What: the one module that knows the shape of an EDL JSON file. It loads, validates, saves,
lays segments on a timeline, decides the export transfer (HLG vs SDR), and does the two
transcript-driven refinements every story cut needs before rendering:

- refine_outpoints: every out-point lands exactly OUT_PAD_S after the last word (in both
  directions: a clipped last word is finished, dead air after the line is pulled back; whisper
  word-end times run early and clipped the last word on ~60% of the Oct 2 cuts), finishes an
  unfinished sentence, then follows the 300-3400 Hz voice energy while the voice is still
  sounding (a laugh, a trailing "mmm"); in-points move back to a word onset when speech is
  already on. 6A: no dead air.
- jumpcut_silences: any silence longer than config.JUMPCUT_SILENCE_S inside a dialogue
  island is cut out by splitting the segment (no dead air).

Why a module of its own: gates.py, render_story.py, render_broll.py, hook_gate.py and
deliver.py all read EDLs; one validator means one definition of "valid".

Export transfer: expected_transfer() decides hlg|sdr and records export {transfer,
transfer_decided, reason}. To FORCE a transfer an EDL author writes export {"transfer": "hlg",
"forced": true} (render_story --transfer hlg|sdr sets the same); a bare export.transfer is only
the record of an earlier render and is decided again on the next render.
Mixed sources (HLG glasses + the SDR Canon reveal) are the one place the kit converts between
SDR and HDR, which START_HERE section 12 ("never convert SDR<->HDR") and 6A ("2K HDR in the
original format") do not foresee: a single file cannot carry both. Policy: SDR export, the HLG
segments tonemapped (TRANSFER_POLICY_NOTE), unless forced "hlg" (then the Canon reveal is
upconverted). The note is written into export.policy_note so the daily run can log the
difference in runs/<date>.md (CLAUDE.md: "RULES.md wins ... log the difference") and the
rules-keeper can confirm SDR-on-mixed vs forced-HLG.

order_key: an ISO-8601 time with offset (footage.csv capture_time), or the day-stage form
dNN-sMM (hand-typed; matched case-insensitively with 1-2 digits and normalised). A naive ISO
time is taken as config.TZ with fold=0 (the first of the two 01:xx readings on the November
fall-back night); footage.csv keys always carry the offset, so only hand-typed keys are affected.

Library functions return data and never print. All writes go through tools.safeio.
CLI: py -m tools.edl validate x.json | refine x.json --transcript words.json --out y.json [--jumpcut]
     | timeline x.json [--json]
"""
from __future__ import annotations

import json
import re
from array import array
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from . import config, safeio

# --------------------------------------------------------------------------- vocabulary
TAGS: tuple[str, ...] = (
    "greeting", "design", "stencil", "stakes", "hours", "pain", "humor", "needle", "approval",
    "reaction", "mirror", "camera", "finished", "station", "piece", "talking", "healed", "done",
    "consult", "travel", "first-tattoo", "return",
)
KINDS: tuple[str, ...] = ("I", "M", "C")
TYPES: tuple[str, ...] = ("story", "broll", "th")
HLG_TRANSFERS = {"arib-std-b67", "smpte2084"}
SENTENCE_GAP_S = 0.35          # next word must start within this to be "the same sentence"
JUMPCUT_PAD_S = 0.15           # kept on each side of a removed silence
ENERGY_STOP_BEFORE_WORD_S = 0.03
ENERGY_HOP_S = 0.010
ENERGY_WIN_S = 0.020
TERMINAL_PUNCT = (".", "?", "!", "...", "\u2026")
_DAY_STAGE_RE = re.compile(r"^d(\d{1,2})-s(\d{1,2})$", re.IGNORECASE)
OVERLAP_TOL_S = 0.001          # consecutive same-clip dialogue segments may not share more than this
CLASS_NAMES = {0: "ISO time", 1: "dNN-sMM", 2: "other"}
TRANSFER_POLICY_NOTE = ("mixed HLG+SDR sources: START_HERE 12 says never convert SDR<->HDR, but one "
                        "file cannot carry both; policy is an SDR export with the HLG segments tonemapped "
                        "(forced hlg upconverts the SDR Canon reveal instead). Log this in runs/<date>.md; "
                        "rules-keeper to confirm SDR-on-mixed vs forced-HLG in RULES.md")

SEG_KEYS = ("clip", "in", "out", "kind", "order_key", "tags", "note")
EDL_KEYS = ("name", "type", "session", "multi_day", "canvas", "segs", "hooks", "caption",
            "pieces", "labels", "audio_from", "export")


# --------------------------------------------------------------------------- dataclasses
@dataclass
class Seg:
    clip: str
    in_: float
    out: float
    kind: str = "I"
    order_key: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""
    extra: dict = field(default_factory=dict)      # unknown keys, kept verbatim

    @property
    def duration(self) -> float:
        return round(self.out - self.in_, 3)

    @classmethod
    def from_dict(cls, d: dict) -> "Seg":
        extra = {k: v for k, v in d.items() if k not in SEG_KEYS}
        return cls(clip=str(d.get("clip", "")), in_=_num(d.get("in")), out=_num(d.get("out")),
                   kind=str(d.get("kind", "")), order_key=str(d.get("order_key", "")),
                   tags=list(d.get("tags") or []), note=str(d.get("note") or ""), extra=extra)

    def to_dict(self) -> dict:
        d = {"clip": self.clip, "in": round(self.in_, 3), "out": round(self.out, 3), "kind": self.kind,
             "order_key": self.order_key, "tags": list(self.tags), "note": self.note}
        d.update(self.extra)
        return d

    def copy(self, **changes) -> "Seg":
        d = dict(clip=self.clip, in_=self.in_, out=self.out, kind=self.kind, order_key=self.order_key,
                 tags=list(self.tags), note=self.note, extra=dict(self.extra))
        d.update(changes)
        return Seg(**d)


@dataclass
class EDL:
    name: str = ""
    type: str = "story"
    session: str = ""
    multi_day: bool = False
    canvas: dict = field(default_factory=lambda: {"w": config.CANVAS_W, "h": config.CANVAS_H})
    segs: list[Seg] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    caption: str = ""
    pieces: list[str] = field(default_factory=list)
    labels: dict = field(default_factory=dict)
    audio_from: object = None
    export: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "EDL":
        if not isinstance(d, dict):
            raise ValueError("EDL must be a JSON object")
        extra = {k: v for k, v in d.items() if k not in EDL_KEYS}
        segs_raw = d.get("segs")
        segs = [Seg.from_dict(s) for s in segs_raw] if isinstance(segs_raw, list) else []
        canvas = d.get("canvas")
        if isinstance(canvas, (list, tuple)) and len(canvas) == 2:
            canvas = {"w": canvas[0], "h": canvas[1]}
        return cls(name=str(d.get("name", "")), type=str(d.get("type", "")),
                   session=str(d.get("session") or ""), multi_day=d.get("multi_day", False),
                   canvas=canvas if isinstance(canvas, dict) else {},
                   segs=segs, hooks=d.get("hooks") if d.get("hooks") is not None else [],
                   caption=str(d.get("caption") or ""), pieces=list(d.get("pieces") or []),
                   labels=dict(d.get("labels") or {}), audio_from=d.get("audio_from"),
                   export=dict(d.get("export") or {}), extra=extra)

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type, "session": self.session, "multi_day": self.multi_day,
             "canvas": dict(self.canvas), "segs": [s.to_dict() for s in self.segs],
             "hooks": list(self.hooks), "caption": self.caption, "pieces": list(self.pieces),
             "labels": dict(self.labels), "audio_from": self.audio_from, "export": dict(self.export)}
        d.update(self.extra)
        return d

    def copy(self, segs: list[Seg] | None = None) -> "EDL":
        return EDL(name=self.name, type=self.type, session=self.session, multi_day=self.multi_day,
                   canvas=dict(self.canvas), segs=[s.copy() for s in (segs if segs is not None else self.segs)],
                   hooks=list(self.hooks), caption=self.caption, pieces=list(self.pieces),
                   labels=dict(self.labels), audio_from=self.audio_from, export=dict(self.export),
                   extra=dict(self.extra))

    @property
    def canvas_wh(self) -> tuple[int, int]:
        return int(self.canvas.get("w", config.CANVAS_W)), int(self.canvas.get("h", config.CANVAS_H))


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------- load / save
def load(path: Path | str, *, strict: bool = True) -> EDL:
    """Read an EDL JSON file. With strict=True (default) a failing validation raises ValueError."""
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        d = json.load(f)
    edl = EDL.from_dict(d)
    if strict:
        errs = validate(edl)
        if errs:
            raise ValueError(f"invalid EDL {p}: " + "; ".join(errs))
    return edl


def save(edl: EDL, path: Path | str) -> Path:
    """Write an EDL sidecar (.json). Overwrite is allowed where safeio permits text sidecars
    (post/, hold/, cache/, ...); his footage and renders stay untouched."""
    return safeio.write_json(path, edl.to_dict(), overwrite=True)


# --------------------------------------------------------------------------- ordering
def order_key_sortable(key: str) -> tuple:
    """Class + sortable value for an order_key.

    (0, epoch_seconds) for an ISO-8601 time with offset (a naive ISO time is taken as config.TZ,
    fold=0), (1, "dNN-sMM") for the day-stage form (case-insensitive, 1-2 digits, normalised so
    "D1-S3" and "d01-s03" are the same key), (2, key) for anything else.
    Keys of different classes never compare; validate() reports a mix as an error.
    """
    k = (key or "").strip()
    m = _DAY_STAGE_RE.match(k)
    if m:
        return (1, f"d{int(m.group(1)):02d}-s{int(m.group(2)):02d}")
    iso = k
    if re.search(r"[+-]\d{4}$", iso):                       # "-0400" -> "-04:00"
        iso = iso[:-2] + ":" + iso[-2:]
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return (2, k)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return (0, dt.timestamp())


# --------------------------------------------------------------------------- validation
def validate(edl: EDL) -> list[str]:
    """Every problem found, as plain strings (empty list = valid)."""
    errs: list[str] = []
    if not edl.name:
        errs.append("name is required")
    if edl.type not in TYPES:
        errs.append(f"type must be one of {'|'.join(TYPES)}, got {edl.type!r}")
    if not isinstance(edl.multi_day, bool):
        errs.append("multi_day must be true or false")
    w, h = edl.canvas.get("w"), edl.canvas.get("h")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        errs.append("canvas must be {w: int>0, h: int>0}")
    if edl.audio_from is not None:
        errs.append("audio_from must be null: audio always stays with its own clip")
    if not isinstance(edl.hooks, list) or not all(isinstance(x, str) for x in edl.hooks):
        errs.append("hooks must be a list of strings")
    elif edl.type in ("story", "th"):
        if len(edl.hooks) != 2 or not all(x.strip() for x in edl.hooks):
            errs.append("hooks must be exactly 2 non-empty strings")
    elif len(edl.hooks) not in (0, 2):
        errs.append("hooks must be 0 or 2 strings for a b-roll EDL")
    if not isinstance(edl.caption, str):
        errs.append("caption must be a string")
    if not isinstance(edl.pieces, list):
        errs.append("pieces must be a list")
    if not isinstance(edl.labels, dict):
        errs.append("labels must be an object")
    if not edl.segs:
        errs.append("segs must not be empty")
    seen_c = False
    prev_key = None
    classes: dict[int, list[int]] = {}
    for i, s in enumerate(edl.segs):
        tag = f"segs[{i}]"
        prev = edl.segs[i - 1] if i > 0 else None
        if (prev is not None and s.clip and _same_clip(prev.clip, s.clip) and "I" in (prev.kind, s.kind)
                and prev.in_ == prev.in_ and s.in_ == s.in_ and prev.out == prev.out and s.out == s.out):
            overlap = min(prev.out, s.out) - max(prev.in_, s.in_)
            if overlap > OVERLAP_TOL_S:
                errs.append(f"{tag}: overlaps segs[{i - 1}] on the same clip by {overlap:.3f} s "
                            f"({prev.in_}-{prev.out} then {s.in_}-{s.out}): the same words would play twice")
        if not s.clip:
            errs.append(f"{tag}: clip is required")
        if s.in_ != s.in_ or s.out != s.out:                 # NaN check
            errs.append(f"{tag}: in and out must be numbers")
        else:
            if s.in_ < 0:
                errs.append(f"{tag}: in must be >= 0")
            if not s.in_ < s.out:
                errs.append(f"{tag}: in ({s.in_}) must be < out ({s.out})")
        if s.kind not in KINDS:
            errs.append(f"{tag}: kind must be I, M or C, got {s.kind!r}")
        if s.kind == "C":
            seen_c = True
        elif seen_c:
            errs.append(f"{tag}: kind {s.kind} after a C segment (Canon reveals go only at the end)")
        if not isinstance(s.tags, list) or not all(isinstance(t, str) for t in s.tags):
            errs.append(f"{tag}: tags must be a list of strings")
        else:
            bad = [t for t in s.tags if t not in TAGS]
            if bad:
                errs.append(f"{tag}: unknown tags {bad}")
        if not s.order_key:
            errs.append(f"{tag}: order_key is required")
        else:
            key = order_key_sortable(s.order_key)
            classes.setdefault(key[0], []).append(i)
            if prev_key is not None and key[0] == prev_key[0] and key < prev_key:
                errs.append(f"{tag}: order_key {s.order_key!r} goes backwards (must be monotonic)")
            prev_key = key
    if len(classes) > 1:
        where = "; ".join(f"{CLASS_NAMES[c]} in segs {idx}" for c, idx in sorted(classes.items()))
        errs.append(f"order_key values mix classes ({where}); use one scheme per EDL")
    if edl.export:
        t = edl.export.get("transfer")
        if t not in (None, "auto", "hlg", "sdr"):
            errs.append(f"export.transfer must be auto|hlg|sdr, got {t!r}")
        f = edl.export.get("forced")
        if f is not None and not isinstance(f, bool):
            errs.append("export.forced must be true or false")
        if f is True and t not in ("hlg", "sdr"):
            errs.append("export.forced needs export.transfer hlg|sdr")
    return errs


# --------------------------------------------------------------------------- timeline
def timeline(edl: EDL) -> list[tuple[float, float, Seg]]:
    """[(t0, t1, seg)]: where each segment sits on the reel's clock."""
    out = []
    t = 0.0
    for s in edl.segs:
        d = max(0.0, s.out - s.in_)
        out.append((round(t, 3), round(t + d, 3), s))
        t += d
    return out


def duration(edl: EDL) -> float:
    return round(sum(max(0.0, s.out - s.in_) for s in edl.segs), 3)


# --------------------------------------------------------------------------- transfer policy
def _is_hlg(info) -> bool:
    if isinstance(info, dict):
        if "is_hlg" in info:
            return bool(info["is_hlg"])
        return str(info.get("transfer") or info.get("color_transfer") or "").lower() in HLG_TRANSFERS
    return str(info or "").lower() in HLG_TRANSFERS


def forced_transfer(export: dict | None) -> str | None:
    """"hlg" | "sdr" when the EDL author (or --transfer) explicitly forced the export transfer:
    export.transfer is honoured as a force ONLY together with export.forced == true. Without the
    flag, export.transfer is just the recorded decision of an earlier render (render_story writes
    it into the .edl.json sidecar) and the policy decides again on re-render."""
    ex = export or {}
    t = ex.get("transfer")
    if ex.get("forced") is True and t in ("hlg", "sdr"):
        return t
    return None


def expected_transfer(edl: EDL, probe: Callable[[str], dict] | dict) -> str:
    """Decide "hlg" or "sdr" for the export and record it in edl.export
    {transfer, transfer_decided, reason}.

    probe: callable(clip) -> ffprobe-style dict (with is_hlg or transfer), or a {clip: dict} map
    (keyed by path or file name; a clip missing from the map raises KeyError, never "SDR").
    Policy: all sources HLG -> hlg; all SDR -> sdr; a mix -> sdr (the Canon reveal forces an
    SDR export; HLG segments are tonemapped with the standard chain) unless the author forced
    "hlg" with export {transfer: "hlg", forced: true} (then SDR segments are converted to HLG with
    zscale). The decision is separate from the override: `transfer` and `transfer_decided` carry
    the decided value, and only `forced: true` makes `transfer` an input on the next render.
    A mix also writes export.policy_note (TRANSFER_POLICY_NOTE, see the module doc / START_HERE 12).
    """
    forced = forced_transfer(edl.export)
    if isinstance(probe, dict):
        def lookup(c: str) -> dict:
            info = probe.get(c)
            if info is None:
                info = probe.get(Path(c).name)
            if info is None:
                raise KeyError(f"probe map has no entry for clip {c!r} (nor {Path(c).name!r})")
            return info
    else:
        lookup = probe
    flags = {s.clip: _is_hlg(lookup(s.clip)) for s in edl.segs}
    n_hlg = sum(1 for v in flags.values() if v)
    n_sdr = len(flags) - n_hlg
    if forced is not None:
        t, reason = forced, f"forced by export.transfer ({n_hlg} HLG, {n_sdr} SDR sources)"
    elif n_sdr == 0 and n_hlg > 0:
        t, reason = "hlg", "every source is HLG: HLG stays HLG (Main10, bt2020, arib-std-b67)"
    elif n_hlg == 0:
        t, reason = "sdr", "every source is SDR: bt709 export, no conversion"
    else:
        c_sdr = [s.clip for s in edl.segs if s.kind == "C" and not flags[s.clip]]
        why = "the Canon reveal is SDR" if c_sdr else f"{n_sdr} SDR source(s)"
        t, reason = "sdr", f"mixed sources: {why}, so the export is SDR and {n_hlg} HLG source(s) are tonemapped"
    edl.export["transfer"] = t
    edl.export["transfer_decided"] = t
    edl.export["reason"] = reason
    if forced is not None:
        edl.export["forced"] = True
    if n_hlg and n_sdr:
        edl.export["policy_note"] = TRANSFER_POLICY_NOTE
    else:
        edl.export.pop("policy_note", None)
    return t


# --------------------------------------------------------------------------- voice energy
def _energy_cache_dir() -> Path:
    return config.CACHE / "energy"


def ffmpeg_voice_energy(clip: Path | str, t0: float, t1: float, hop: float = ENERGY_HOP_S,
                        win: float = ENERGY_WIN_S) -> list[tuple[float, float]]:
    """[(t, rms)] of the 300-3400 Hz band in [t0, t1] of the clip (t in clip seconds).

    Decodes through ffmpeg (highpass=300,lowpass=3400, mono 16 kHz f32le) into cache/energy/
    via safeio and reads the samples back; no numpy needed.
    """
    clip = Path(clip)
    t0 = max(0.0, t0)
    if t1 <= t0:
        return []
    sr = 16000
    lo, hi = config.VOICE_BAND_HZ
    raw = _energy_cache_dir() / f"{clip.stem}_{t0:.3f}_{t1:.3f}.f32"
    args = ["-ss", f"{t0:.3f}", "-to", f"{t1:.3f}", "-i", str(clip), "-vn", "-map", "0:a:0",
            "-af", f"highpass=f={lo},lowpass=f={hi},aresample={sr},aformat=channel_layouts=mono",
            "-f", "f32le", "-acodec", "pcm_f32le"]
    res = safeio.run_ffmpeg(args, raw, inputs=[clip], overwrite=True)
    if res.returncode != 0 or not raw.exists():
        return []
    samples = array("f")
    with open(raw, "rb") as f:
        data = f.read()
    samples.frombytes(data[: len(data) - (len(data) % 4)])
    safeio.remove_cache_file(raw)
    n = len(samples)
    hop_n, win_n = max(1, int(sr * hop)), max(1, int(sr * win))
    out: list[tuple[float, float]] = []
    i = 0
    while i < n:
        chunk = samples[i: i + win_n]
        if not chunk:
            break
        acc = 0.0
        for v in chunk:
            acc += v * v
        out.append((round(t0 + i / sr, 4), (acc / len(chunk)) ** 0.5))
        i += hop_n
    return out


# --------------------------------------------------------------------------- out/in refinement
def _ends_sentence(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and t.endswith(TERMINAL_PUNCT)


def refine_outpoints(edl: EDL, provider, audio_energy: Callable | None = None) -> EDL:
    """Return a new EDL whose I segments end in a pause and start on a word onset.

    Per I segment (words re-read non-VAD in [in-1.0, out+1.8]):
    1. out = end of the last word starting before out + config.OUT_PAD_S.
    2. If that word has no terminal punctuation and the next word starts within 0.35 s, follow
       the sentence word by word (each must start within 0.35 s of the previous end) up to
       config.OUT_SENTENCE_MAX_S past the step-1 out.
    3. Voice-band energy (audio_energy(clip, t0, t1) -> [(t, rms)], default ffmpeg decode):
       extend while rms > 0.5 x the segment's speech RMS, at most config.OUT_EXTEND_MAX_S,
       stopping 0.03 s before the next word.
    4. in: if a word is sounding at in, move back to its onset (max config.IN_ONSET_MAX_S);
       never for the first segment of the reel (no dead lead-in on the opener).
    Step 1 moves the out-point in BOTH directions: a clipped last word is finished, and dead air
    the editor left after the line is pulled back to last word end + OUT_PAD_S (recorded as
    extra["refined"]["pulled_back_s"]). Nothing is kept "as a reaction beat" on the transcript
    alone: a laugh or a trailing sound after the last word is voice energy, and step 3 follows it.
    Guards (reviewer findings, Oct 18):
    - Only words INSIDE the segment count for step 1. A segment with no transcribed word is left
      untouched and marked extra["refined"]["skipped"] = "no words in segment" (whisper may have
      missed the line: run VAD and non-VAD before deciding a line does not exist), instead of
      collapsing onto a word that ended before the in-point.
    - Two consecutive segments on the same clip should never overlap: this segment's out is capped
      at the next segment's original in, and its in never moves before the previous segment's
      refined out (the same words must not play twice across a jump cut). When the previous
      segment already covers this one so the clamp would leave < 0.1 s, the editor's in is kept
      and extra["refined"]["overlap_with_prev_s"] records the overlap; validate() then reports it.
    The original in/out are kept in seg.extra["refined"].
    """
    energy = audio_energy or ffmpeg_voice_energy
    new_segs: list[Seg] = []
    for idx, seg in enumerate(edl.segs):
        if seg.kind != "I":
            new_segs.append(seg.copy())
            continue
        prev = new_segs[idx - 1] if idx > 0 else None                    # already refined
        nxt_seg = edl.segs[idx + 1] if idx + 1 < len(edl.segs) else None  # original
        same_prev = prev is not None and _same_clip(prev.clip, seg.clip)
        same_next = (nxt_seg is not None and _same_clip(nxt_seg.clip, seg.clip)
                     and nxt_seg.in_ >= seg.out - 1e-6)
        words = sorted(provider.words(Path(seg.clip), seg.in_ - 1.0, seg.out + 1.8, vad=False),
                       key=lambda w: (w.start, w.end))
        extra = dict(seg.extra)
        inside = [w for w in words if w.start < seg.out and w.end > seg.in_]
        if not inside:
            extra["refined"] = {"in": round(seg.in_, 3), "out": round(seg.out, 3),
                                "skipped": "no words in segment"}
            new_segs.append(seg.copy(extra=extra))
            continue
        new_in, new_out = seg.in_, seg.out
        # 1 + 2: last word inside the segment, then finish the sentence
        last = inside[-1]
        li = words.index(last)
        spoken_out = last.end + config.OUT_PAD_S
        cap = spoken_out + config.OUT_SENTENCE_MAX_S
        cur = last
        while not _ends_sentence(cur.text) and li + 1 < len(words):
            nxt = words[li + 1]
            if nxt.start - cur.end > SENTENCE_GAP_S or nxt.end + config.OUT_PAD_S > cap:
                break
            spoken_out = nxt.end + config.OUT_PAD_S
            cur, li = nxt, li + 1
        refined: dict = {"in": round(seg.in_, 3), "out": round(seg.out, 3)}
        if spoken_out < seg.out - 1e-6:
            refined["pulled_back_s"] = round(seg.out - spoken_out, 3)   # dead air removed (6A)
        new_out = max(spoken_out, seg.in_ + 0.1)   # land OUT_PAD_S into the pause, both directions
        # 3: follow the voice energy
        next_words = [w for w in words if w.start >= new_out - 1e-6]
        limit = new_out + config.OUT_EXTEND_MAX_S
        if next_words:
            limit = min(limit, next_words[0].start - ENERGY_STOP_BEFORE_WORD_S)
        if limit > new_out + ENERGY_HOP_S:
            frames = energy(Path(seg.clip), max(0.0, new_in), limit) or []
            speech = [r for t, r in frames if any(w.start <= t <= w.end for w in words if w.end > new_in and w.start < new_out)]
            if not speech:
                speech = [r for t, r in frames if t < new_out]
            ref = (sum(speech) / len(speech)) if speech else 0.0
            if ref > 0:
                t_ext = new_out
                for t, r in frames:
                    if t < new_out:
                        continue
                    if t > limit:
                        break
                    if r > 0.5 * ref:
                        t_ext = t + ENERGY_HOP_S
                    else:
                        break
                new_out = min(max(new_out, t_ext), limit)
        # never run into the next segment of the same clip (its words would play twice)
        if same_next and nxt_seg.in_ > seg.in_ + 0.1 and new_out > nxt_seg.in_:
            refined["out_capped_at_next_in"] = round(nxt_seg.in_, 3)
            new_out = nxt_seg.in_
        # 4: in-point onto the word onset
        if idx > 0:
            on = [w for w in words if w.start < seg.in_ < w.end]
            if on:
                new_in = max(on[0].start, seg.in_ - config.IN_ONSET_MAX_S, 0.0)
            if same_prev and prev.out > new_in + 1e-6:
                clamped = max(new_in, prev.out)
                if new_out - clamped >= 0.1:
                    new_in = clamped
                    refined["in_clamped_to_prev_out"] = round(prev.out, 3)
                else:
                    # the previous segment already covers this one: keep the editor's in and
                    # record the overlap so validate()/G7 report it instead of hiding it
                    new_in = seg.in_
                    refined["overlap_with_prev_s"] = round(prev.out - seg.in_, 3)
        extra["refined"] = refined
        new_segs.append(seg.copy(in_=round(new_in, 3), out=round(new_out, 3), extra=extra))
    return edl.copy(segs=new_segs)


def _same_clip(a: str, b: str) -> bool:
    return a == b or Path(a).resolve(strict=False) == Path(b).resolve(strict=False)


def jumpcut_silences(edl: EDL, provider) -> EDL:
    """Return a new EDL where every silence > config.JUMPCUT_SILENCE_S between two words inside
    an I segment is removed by splitting the segment (0.15 s kept on each side of the gap).
    order_key, tags, kind and note are copied to every part."""
    new_segs: list[Seg] = []
    for seg in edl.segs:
        if seg.kind != "I":
            new_segs.append(seg.copy())
            continue
        words = sorted((w for w in provider.words(Path(seg.clip), seg.in_, seg.out, vad=False)
                        if w.end > seg.in_ and w.start < seg.out), key=lambda w: (w.start, w.end))
        cuts: list[tuple[float, float]] = []
        for a, b in zip(words, words[1:]):
            if b.start - a.end > config.JUMPCUT_SILENCE_S:
                cuts.append((a.end + JUMPCUT_PAD_S, b.start - JUMPCUT_PAD_S))
        if not cuts:
            new_segs.append(seg.copy())
            continue
        start = seg.in_
        parts = 0
        for c0, c1 in cuts:
            c0, c1 = max(c0, start), min(c1, seg.out)
            if c1 <= c0:
                continue
            if c0 > start + 0.05:
                parts += 1
                new_segs.append(seg.copy(in_=round(start, 3), out=round(c0, 3),
                                         extra={**seg.extra, "jumpcut": parts}))
            start = c1
        if seg.out > start + 0.05:
            parts += 1
            new_segs.append(seg.copy(in_=round(start, 3), out=round(seg.out, 3),
                                     extra={**seg.extra, "jumpcut": parts}))
    return edl.copy(segs=new_segs)


# --------------------------------------------------------------------------- transcript helpers (CLI)
@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


class JsonWordsProvider:
    """TranscriptProvider from a JSON file: {"<clip name or path>": [[start, end, text], ...]}
    or {"<clip>": [{"start":..,"end":..,"text":..}, ...]}. Used by the CLI; tests use
    tools/tests/stub_transcript.py."""

    def __init__(self, data: dict):
        self.data: dict[str, list[Word]] = {}
        for k, ws in data.items():
            out = []
            for w in ws:
                if isinstance(w, dict):
                    out.append(Word(float(w["start"]), float(w["end"]), str(w.get("text", "")),
                                    float(w.get("prob", 1.0))))
                else:
                    out.append(Word(float(w[0]), float(w[1]), str(w[2]) if len(w) > 2 else "",
                                    float(w[3]) if len(w) > 3 else 1.0))
            self.data[Path(k).name] = out

    @classmethod
    def from_file(cls, path: Path | str) -> "JsonWordsProvider":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def words(self, clip, start=None, end=None, vad=True):
        ws = self.data.get(Path(clip).name, [])
        lo = -1e9 if start is None else start
        hi = 1e9 if end is None else end
        return [w for w in ws if w.end >= lo and w.start <= hi]


def _real_provider():
    """tools.transcribe (another builder's module) when present, else a clear error."""
    try:
        from . import transcribe  # noqa: WPS433
    except ImportError as e:
        raise RuntimeError("no --transcript given and tools.transcribe is not available "
                           f"({e}); pass --transcript words.json") from e
    factory = getattr(transcribe, "provider", None)
    if callable(factory):
        return factory()
    for name in ("FasterWhisperTranscript", "TranscriptProvider", "Transcriber", "WhisperTranscript", "Provider"):
        cls = getattr(transcribe, name, None)
        if cls is not None:
            return cls()
    raise RuntimeError("tools.transcribe has no provider class; pass --transcript words.json")


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="EDL contract: validate, refine out-points, print the timeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="check an EDL file")
    v.add_argument("edl")
    v.add_argument("--json", action="store_true")
    r = sub.add_parser("refine", help="refine in/out points from the transcript (and optionally jump-cut silences)")
    r.add_argument("edl")
    r.add_argument("--transcript", help="words JSON {clip: [[start, end, text], ...]}; default: tools.transcribe")
    r.add_argument("--out", required=True)
    r.add_argument("--jumpcut", action="store_true", help="also split segments around silences > JUMPCUT_SILENCE_S")
    r.add_argument("--json", action="store_true")
    t = sub.add_parser("timeline", help="print where each segment sits on the reel clock")
    t.add_argument("edl")
    t.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        return _run(args)
    except (OSError, ValueError, RuntimeError, safeio.SafeIOError) as e:
        # json.JSONDecodeError is a ValueError; FileNotFoundError is an OSError
        print(f"error: {e}")
        return 1


def _run(args) -> int:
    if args.cmd == "validate":
        edl = load(args.edl, strict=False)
        errs = validate(edl)
        if args.json:
            print(json.dumps({"edl": args.edl, "ok": not errs, "errors": errs, "duration": duration(edl)}, indent=2))
        else:
            print(f"{args.edl}: {'OK' if not errs else 'INVALID'} ({len(edl.segs)} segs, {duration(edl)} s)")
            for e in errs:
                print("  - " + e)
        return 0 if not errs else 1

    if args.cmd == "timeline":
        edl = load(args.edl, strict=False)
        rows = [{"t0": t0, "t1": t1, "kind": s.kind, "clip": s.clip, "in": s.in_, "out": s.out,
                 "tags": s.tags, "note": s.note} for t0, t1, s in timeline(edl)]
        if args.json:
            print(json.dumps({"edl": args.edl, "duration": duration(edl), "segs": rows}, indent=2))
        else:
            for r_ in rows:
                print(f"{r_['t0']:7.2f}-{r_['t1']:7.2f} {r_['kind']} {Path(r_['clip']).name} "
                      f"{r_['in']:.2f}-{r_['out']:.2f} {','.join(r_['tags'])} {r_['note']}")
            print(f"total {duration(edl)} s")
        return 0

    if args.cmd == "refine":
        edl = load(args.edl)
        provider = JsonWordsProvider.from_file(args.transcript) if args.transcript else _real_provider()
        # jump cuts first, so every resulting part's out-point gets the pause/energy treatment
        refined = jumpcut_silences(edl, provider) if args.jumpcut else edl
        refined = refine_outpoints(refined, provider)
        errs = validate(refined)
        if errs:
            print("refined EDL is invalid: " + "; ".join(errs))
            return 1
        save(refined, args.out)
        changes = []
        skipped = []
        for b in refined.segs:
            r = (b.extra or {}).get("refined") or {}
            if r.get("skipped"):
                skipped.append({"clip": Path(b.clip).name, "in": b.in_, "out": b.out, "why": r["skipped"]})
            elif r and (r.get("in"), r.get("out")) != (b.in_, b.out):
                changes.append({"clip": Path(b.clip).name, "in": [r["in"], b.in_], "out": [r["out"], b.out]})
        if args.json:
            print(json.dumps({"in": args.edl, "out": args.out, "segs": len(refined.segs),
                              "duration": duration(refined), "changed": changes, "skipped": skipped}, indent=2))
        else:
            print(f"refined {args.edl} -> {args.out}: {len(refined.segs)} segs, {duration(refined)} s, "
                  f"{len(changes)} boundary change(s), {len(skipped)} segment(s) without words")
            for s in skipped:
                print(f"  ! {s['clip']} {s['in']:.2f}-{s['out']:.2f}: {s['why']} (check VAD and non-VAD passes)")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
