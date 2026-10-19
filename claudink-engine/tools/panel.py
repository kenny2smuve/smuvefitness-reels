"""The blind critic panel (START_HERE section 7): review packets, anonymisation, calibration
maths and the read-only yt-dlp download plan for his own public reels.

Why: a critic that knows which reel is a winner is not a critic. Every reel (his winners,
medians, flops and today's candidates) becomes the same kind of packet: a 0-3 s strip at
0.25 s steps, a contact sheet every 2 s, a time-stamped transcript and the length (plus the
hook for candidates). HLG masters (arib-std-b67 / smpte2084, his Meta glasses footage) are
tonemapped with the engine's CPU chain before the frames are tiled, so a candidate cut from an
HLG master looks like the SDR rendition Instagram serves and cannot be told apart from the
yt-dlp downloads of his winners by look alone. The packets are then copied under neutral ids
(P01..Pnn, shuffled with a seed) with everything identifying stripped, and the mapping from id
to source is written to state/panel/<round>.mapping.json, outside the round folder, so a critic
that lists cache/panel/<round>/ never sees it. After the critic ranks the panel, `calibration` checks that
winners rank above flops in >= config.CRITIC_PAIR_RATE_MIN of the (winner, flop) pairs, and
`candidate_ok` says whether a shipped candidate landed in the top third with no flop call.

Downloads (`download_plan`, `download`) only ever touch his own public reels through yt-dlp,
never with cookies or a browser session, at most config.PANEL_DOWNLOADS_BURNIN (20) during
burn-in and config.PANEL_DOWNLOADS_DAILY (6) a day after, with a random 3-5 s gap between
requests, and the whole batch stops at the first login wall or rate limit. `dry_run=True`
(the default) only builds the commands; tests never run yt-dlp.

Library functions return data and never print. Every file goes through safeio.

Run:  py -m tools.panel packet <video> --out <dir> [--hook "..."] [--words words.json] [--every 2]
      py -m tools.panel anonymize <packet_dir ...> --round r1 --seed 7 [--truth truth.json]
      py -m tools.panel calibrate --round r1 --ranking P03,P01,... --calls P03=breakout,P01=flop
                                  [--truth truth.json]
      py -m tools.panel plan --mode burnin|daily [--out <dir>] [--already a,b]
      py -m tools.panel download --mode burnin|daily --out <dir> [--run]
      (--json works before or after the subcommand)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import config
from . import safeio

# --------------------------------------------------------------------------- constants
STRIP_END_S = 3.0
STRIP_STEP_S = 0.25
STRIP_FRAMES = 13                  # 0, 0.25, ..., 3.0
STRIP_COLS, STRIP_ROWS = 7, 2
STRIP_CELL = (180, 320)
SHEET_COLS = 5
SHEET_CELL = (216, 384)
TILE_PAD = 2
JPEG_Q = "3"
STRIP_NAME = "strip_0_3s.jpg"
SHEET_NAME = "sheet_2s.jpg"
TRANSCRIPT_NAME = "transcript.txt"
META_NAME = "meta.json"
MAPPING_NAME = "_mapping.json"          # legacy location inside the round folder (read only)
MAPPING_SUFFIX = ".mapping.json"        # state/panel/<round>.mapping.json
NO_TRANSCRIPT = "(no transcript)"
TRUTHS = ("winner", "median", "flop", "candidate")
CALLS = ("breakout", "median", "flop")
# "HTTP Error 429" or a bare 429 token that is not part of a speed/size figure ("429.12KiB/s").
STOP_PATTERN = re.compile(r"\blogin\b|HTTP Error 429|\b429\b(?![.\d])|rate[ -]?limit|too many requests",
                          re.IGNORECASE)
REEL_URL = "https://www.instagram.com/reel/{code}/"
HLG_TRANSFERS = ("arib-std-b67", "smpte2084")
TONEMAP_CPU_FALLBACK = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                        "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


# --------------------------------------------------------------------------- probing
def probe_video(video: Path | str) -> dict:
    """{duration, width, height, fps, color_transfer} via ffprobe (zeros / '' when missing)."""
    r = safeio.run_ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height,r_frame_rate,avg_frame_rate,color_transfer:"
                            "format=duration", "-of", "json", str(video)])
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        d = {}
    st = (d.get("streams") or [{}])[0]
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = st.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/", 1)
            try:
                if float(den) > 0:
                    fps = float(num) / float(den)
                    break
            except ValueError:
                pass
    try:
        duration = float((d.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration": duration, "width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0), "fps": round(fps, 3),
            "color_transfer": str(st.get("color_transfer") or "").lower()}


def is_hlg(transfer: str | None) -> bool:
    return (transfer or "").lower() in HLG_TRANSFERS


def tonemap_chain() -> str:
    """The engine's CPU tonemap (tools.encode.TONEMAP_CPU when importable): the same chain G1
    uses for its proxy, so a candidate's packet shows what viewers see."""
    try:
        from . import encode
        chain = getattr(encode, "TONEMAP_CPU", None)
        if isinstance(chain, str) and chain:
            return chain
    except ImportError:
        pass
    return TONEMAP_CPU_FALLBACK


# --------------------------------------------------------------------------- ffmpeg pieces
def _font_file() -> str | None:
    env = os.environ.get("CLAUDINK_FONT")
    if env and Path(env).is_file():
        return env
    for cand in FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _drawtext_escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def timestamp_filter(fontsize: int = 18) -> str:
    """drawtext that stamps each frame with its own pts as hh:mm:ss.mmm (top-left box)."""
    font = _font_file()
    fontpart = f"fontfile='{_drawtext_escape(font)}':" if font else ""
    return (f"drawtext={fontpart}text='%{{pts\\:hms}}':x=6:y=6:fontsize={fontsize}:"
            f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=3")


def cell_filter(cell: tuple[int, int]) -> str:
    w, h = cell
    return (f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")


def _tonemap_part(transfer: str | None) -> str:
    """The tonemap step for HLG/PQ sources, placed after fps/select so only the picked frames
    pay for it; empty for SDR."""
    return tonemap_chain() + "," if is_hlg(transfer) else ""


def strip_filter(transfer: str | None = None) -> str:
    """0-3 s at 4 fps -> 13 stamped frames tiled 7x2 (the last cell stays black)."""
    return (f"fps={1 / STRIP_STEP_S:g},select=lt(n\\,{STRIP_FRAMES}),{_tonemap_part(transfer)}"
            f"{cell_filter(STRIP_CELL)},"
            f"{timestamp_filter(16)},tile={STRIP_COLS}x{STRIP_ROWS}:nb_frames={STRIP_FRAMES}:"
            f"padding={TILE_PAD}:margin={TILE_PAD}:color=black")


def sheet_layout(duration: float, every: float) -> tuple[int, int]:
    """(frames, rows): one frame every `every` seconds from 0, SHEET_COLS per row."""
    n = max(1, int(math.floor(duration / every + 1e-6)) + 1) if duration > 0 else 1
    return n, max(1, math.ceil(n / SHEET_COLS))


def sheet_filter(duration: float, every: float, transfer: str | None = None) -> str:
    n, rows = sheet_layout(duration, every)
    return (f"fps=1/{every:g},select=lt(n\\,{n}),{_tonemap_part(transfer)}{cell_filter(SHEET_CELL)},"
            f"{timestamp_filter(18)},"
            f"tile={SHEET_COLS}x{rows}:nb_frames={n}:padding={TILE_PAD}:margin={TILE_PAD}:color=black")


def _render_image(video: Path, vf: str, out: Path, read_to: float | None = None) -> Path:
    args = []
    if read_to is not None:
        args += ["-t", f"{read_to:.3f}"]
    args += ["-i", str(video), "-vf", vf, "-frames:v", "1", "-update", "1", "-q:v", JPEG_Q,
             "-f", "image2"]
    r = safeio.run_ffmpeg(args, out, overwrite=safeio.is_rewritable(out), timeout=600)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg could not write {out.name}: {(r.stderr or '').strip()[-400:]}")
    return out


def _via_contact_sheet(kind: str, video: Path, out: Path, every: float) -> bool:
    """Use tools.contact_sheet (another builder's module) when it is there and exposes
    make_strip(video, out, start, end, step) / make_sheet(video, out, every). Any mismatch
    falls back to this module's own ffmpeg chain."""
    try:
        from . import contact_sheet as cs
    except ImportError:
        return False
    fn = getattr(cs, "make_strip" if kind == "strip" else "make_sheet", None)
    if not callable(fn):
        return False
    try:
        if kind == "strip":
            fn(video, out, start=0.0, end=STRIP_END_S, step=STRIP_STEP_S)
        else:
            fn(video, out, every=every)
    except Exception:  # noqa: BLE001 - a foreign signature or failure means: do it ourselves
        return False
    return out.is_file() and out.stat().st_size > 0


# --------------------------------------------------------------------------- transcript
def transcript_lines(words: Iterable, gap: float = config.JUMPCUT_SILENCE_S) -> list[str]:
    """Group words into lines at silences > gap: "[12.4-13.9] words"."""
    lines: list[str] = []
    cur: list = []
    for w in words:
        if cur and (w.start - cur[-1].end) > gap:
            lines.append(_fmt_line(cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(_fmt_line(cur))
    return lines


def _fmt_line(ws: list) -> str:
    text = " ".join(str(w.text).strip() for w in ws if str(w.text).strip())
    return f"[{ws[0].start:.1f}-{ws[-1].end:.1f}] {text}"


def transcript_text(video: Path, provider) -> str:
    if provider is None:
        return NO_TRANSCRIPT + "\n"
    words = list(provider.words(video))
    if not words:
        return NO_TRANSCRIPT + "\n"
    return "\n".join(transcript_lines(words)) + "\n"


# --------------------------------------------------------------------------- packet
def build_packet(video: Path | str, out_dir: Path | str, transcript_provider=None,
                 hook: str | None = None, every: float = 2.0) -> dict:
    """Write strip_0_3s.jpg, sheet_2s.jpg, transcript.txt and meta.json into out_dir."""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    out_dir = safeio.mkdir(Path(out_dir))
    info = probe_video(video)
    duration = info["duration"]
    transfer = info["color_transfer"]
    # tools.contact_sheet has no tonemap: an HLG master always goes through this module's own
    # chain so the packet shows the SDR picture viewers (and the critic's winner packets) see.
    hlg = is_hlg(transfer)

    strip = out_dir / STRIP_NAME
    if hlg or not _via_contact_sheet("strip", video, strip, every):
        _render_image(video, strip_filter(transfer), strip, read_to=STRIP_END_S + 0.3)
    sheet = out_dir / SHEET_NAME
    if hlg or not _via_contact_sheet("sheet", video, sheet, every):
        _render_image(video, sheet_filter(duration, every, transfer), sheet)

    tpath = out_dir / TRANSCRIPT_NAME
    safeio.write_text(tpath, transcript_text(video, transcript_provider),
                      overwrite=safeio.is_rewritable(tpath))
    meta = {"length_s": round(duration, 2), "fps": info["fps"],
            "size": f"{info['width']}x{info['height']}", "source": video.name,
            "transfer": transfer, "tonemapped": hlg}
    if hook:
        meta["hook"] = hook
    mpath = out_dir / META_NAME
    safeio.write_json(mpath, meta, overwrite=safeio.is_rewritable(mpath))
    n_sheet, rows = sheet_layout(duration, every)
    return {"dir": str(out_dir), "strip": str(strip), "sheet": str(sheet), "transcript": str(tpath),
            "meta": str(mpath), "length_s": meta["length_s"], "strip_frames": STRIP_FRAMES,
            "sheet_frames": n_sheet, "sheet_rows": rows, "hook": hook}


# --------------------------------------------------------------------------- anonymise
def _norm_truth(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in ("winner", "winners", "breakout", "hit"):
        return "winner"
    if v in ("flop", "flops"):
        return "flop"
    if v in ("median", "medians", "mid"):
        return "median"
    return "candidate"


def truth_of_source(name: str) -> str:
    """winner/median/flop when a panel shortcode is part of the name, else candidate."""
    for code in config.PANEL_WINNERS:
        if code in name:
            return "winner"
    for code in config.PANEL_FLOPS:
        if code in name:
            return "flop"
    for code in config.PANEL_MEDIANS:
        if code in name:
            return "median"
    return "candidate"


def panel_root() -> Path:
    return config.CACHE / "panel"


def panel_round_dir(round_name: str) -> Path:
    """cache/panel/<round>: the round name is a plain token that cannot start with a dot, so
    '.', '..' and hidden names are refused and the folder is always a direct child."""
    name = round_name or ""
    if name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise ValueError(f"round name must be a plain token (letters, digits, _ . -, not starting "
                         f"with a dot): {round_name!r}")
    rdir = panel_root() / name
    if rdir.resolve(strict=False).parent != panel_root().resolve(strict=False):
        raise ValueError(f"round folder is not a direct child of {panel_root()}: {rdir}")
    return rdir


def mapping_path(round_name: str) -> Path:
    """state/panel/<round>.mapping.json: outside the round folder the critic is pointed at."""
    panel_round_dir(round_name)             # same validation of the name
    return config.STATE / "panel" / (round_name + MAPPING_SUFFIX)


def _clear_round(rdir: Path) -> None:
    if not rdir.exists():
        return
    root = panel_root().resolve(strict=False)
    if rdir.resolve(strict=False).parent != root or rdir.resolve(strict=False) == root:
        raise ValueError(f"refusing to clear anything but a round folder under {root}: {rdir}")
    for p in sorted(rdir.rglob("*"), reverse=True):
        if p.is_file():
            safeio.remove_cache_file(p)
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass


def anonymize(packet_dirs: Sequence[Path | str], round_name: str, seed: int,
              truth: dict[str, str] | None = None) -> dict:
    """Copy packets to cache/panel/<round>/P01../ under shuffled ids with identifying data
    stripped; write state/panel/<round>.mapping.json {Pid: {source, truth}} (never shown to
    the critic: only the cache/panel/<round>/P??/ folders are passed to it)."""
    dirs = [Path(d) for d in packet_dirs]
    if not dirs:
        raise ValueError("no packet directories given")
    for d in dirs:
        if not d.is_dir():
            raise FileNotFoundError(d)
    rdir = panel_round_dir(round_name)
    mpath_target = mapping_path(round_name)
    _clear_round(rdir)
    safeio.mkdir(rdir)
    order = list(range(len(dirs)))
    random.Random(seed).shuffle(order)
    width = max(2, len(str(len(dirs))))
    mapping: dict[str, dict] = {}
    ids: list[str] = []
    for i, src_index in enumerate(order, start=1):
        src = dirs[src_index]
        pid = f"P{i:0{width}d}"
        meta = safeio.read_json(src / META_NAME, default={}) or {}
        source_name = str(meta.get("source") or src.name)
        t = None
        if truth:
            t = truth.get(str(src)) or truth.get(src.name) or truth.get(source_name)
        t = _norm_truth(t) if t else truth_of_source(f"{src.name} {source_name}")
        dst = safeio.mkdir(rdir / pid)
        for name in (STRIP_NAME, SHEET_NAME, TRANSCRIPT_NAME):
            if (src / name).is_file():
                safeio.copy_file(src / name, dst / name, overwrite=True)
        clean: dict = {}
        if "length_s" in meta:
            try:
                clean["length_s"] = round(float(meta["length_s"]), 1)
            except (TypeError, ValueError):
                pass
        if t == "candidate" and meta.get("hook"):
            clean["hook"] = str(meta["hook"])
        safeio.write_json(dst / META_NAME, clean, overwrite=True)
        mapping[pid] = {"source": str(src), "truth": t}
        ids.append(pid)
    safeio.mkdir(mpath_target.parent)
    mpath = safeio.write_json(mpath_target, mapping, overwrite=True)
    return {"round": round_name, "dir": str(rdir), "ids": ids, "n": len(ids),
            "mapping_file": str(mpath)}


def load_mapping(round_name: str) -> dict:
    """The id -> {source, truth} key of a round (state/panel/<round>.mapping.json; a legacy
    _mapping.json inside the round folder is still read)."""
    m = safeio.read_json(mapping_path(round_name), default=None)
    if not isinstance(m, dict):
        m = safeio.read_json(panel_round_dir(round_name) / MAPPING_NAME, default=None)
    if not isinstance(m, dict):
        raise FileNotFoundError(f"no mapping for round {round_name} ({mapping_path(round_name)})")
    return m


# --------------------------------------------------------------------------- calibration
def _truth_lookup(pid: str, mapping: dict, truth: dict[str, str] | None) -> str:
    entry = mapping.get(pid) or {}
    src = str(entry.get("source") or "")
    if truth:
        for key in (pid, src, Path(src).name if src else ""):
            if key and key in truth:
                return _norm_truth(truth[key])
    return _norm_truth(entry.get("truth"))


def calibration(ranking: Sequence[str], calls: dict[str, str], mapping: dict,
                truth: dict[str, str] | None = None) -> dict:
    """Pair maths: every (winner, flop) pair counts, correct when the winner ranks above."""
    pos = {pid: i for i, pid in enumerate(ranking)}
    below_all = len(ranking)
    winners = [p for p in mapping if _truth_lookup(p, mapping, truth) == "winner"]
    flops = [p for p in mapping if _truth_lookup(p, mapping, truth) == "flop"]
    total = len(winners) * len(flops)
    correct = 0
    for w in winners:
        for f in flops:
            pw, pf = pos.get(w, below_all), pos.get(f, below_all)
            if pw < pf:
                correct += 1
    rate = (correct / total) if total else 0.0
    called_flop = [w for w in winners if (calls.get(w) or "").strip().lower() == "flop"]
    return {"pair_rate": round(rate, 4), "pairs_total": total, "pairs_correct": correct,
            "winners_called_flop": called_flop, "n_winners": len(winners), "n_flops": len(flops),
            "unranked": sorted(p for p in mapping if p not in pos),
            "calibrated": total > 0 and rate >= config.CRITIC_PAIR_RATE_MIN}


def candidate_ok(pid: str, ranking: Sequence[str], calls: dict[str, str]) -> bool:
    """Shipped candidates must land in the top third of the ranking with no flop call."""
    if pid not in ranking:
        return False
    n = len(ranking)
    top = max(1, n // 3)
    return list(ranking).index(pid) < top and (calls.get(pid) or "").strip().lower() != "flop"


# --------------------------------------------------------------------------- downloads
def panel_codes() -> list[str]:
    """Winners, then flops, then medians (a capped batch still holds both calibration classes)."""
    out: list[str] = []
    for code in (*config.PANEL_WINNERS, *config.PANEL_FLOPS, *config.PANEL_MEDIANS):
        if code not in out:
            out.append(code)
    return out


def download_plan(mode: str, already: Iterable[str] = ()) -> list[str]:
    caps = {"burnin": config.PANEL_DOWNLOADS_BURNIN, "daily": config.PANEL_DOWNLOADS_DAILY}
    if mode not in caps:
        raise ValueError(f"mode must be burnin or daily, got {mode!r}")
    have = set(already)
    todo = [c for c in panel_codes() if c not in have]
    return todo[:caps[mode]]


def existing_codes(out_dir: Path | str) -> set[str]:
    """Shortcodes that already have a media file in out_dir (stem == code)."""
    d = Path(out_dir)
    if not d.is_dir():
        return set()
    return {p.stem for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")}


def ytdlp_command(code: str, out_dir: Path | str) -> list[str]:
    """yt-dlp without cookies, logins or a browser session: public reels only."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,20}", code or ""):
        raise ValueError(f"not an Instagram shortcode: {code!r}")
    out_dir = Path(out_dir)
    if not any(safeio.is_under(out_dir, r) for r in safeio.write_roots()):
        raise safeio.SafeIOError(f"refusing to download outside the engine roots: {out_dir}")
    return ["yt-dlp", "--no-playlist", "-o", str(out_dir / (code + ".%(ext)s")),
            REEL_URL.format(code=code)]


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)


def stop_reason(stdout: str, stderr: str) -> str | None:
    """'login wall' / 'rate limited' when yt-dlp output says so, else None."""
    text = f"{stdout}\n{stderr}"
    m = STOP_PATTERN.search(text)
    if not m:
        return None
    return "login wall" if "login" in m.group(0).lower() else "rate limited"


def download(shortcodes: Sequence[str], out_dir: Path | str, dry_run: bool = True,
             sleeper: Callable[[float], None] | None = None, runner=None,
             rng: random.Random | None = None) -> list[dict]:
    """Fetch his public reels one by one; stop the whole batch at the first login wall or
    rate limit. Returns [{code, ok, reason, cmd, file}]. dry_run only builds the commands."""
    out_dir = Path(out_dir)
    sleeper = sleeper or time.sleep
    runner = runner or _default_runner
    rng = rng or random.Random()
    results: list[dict] = []
    stopped: str | None = None
    have = existing_codes(out_dir)
    requests_made = 0
    for code in shortcodes:
        cmd = ytdlp_command(code, out_dir)
        if any("cookies" in a.lower() for a in cmd):
            raise safeio.SafeIOError("cookies are never passed to yt-dlp")
        if stopped:
            results.append({"code": code, "ok": False, "reason": f"skipped: batch stopped ({stopped})",
                            "cmd": cmd, "file": None})
            continue
        if code in have:
            results.append({"code": code, "ok": True, "reason": "exists", "cmd": cmd,
                            "file": _file_for(out_dir, code)})
            continue
        if dry_run:
            results.append({"code": code, "ok": True, "reason": "dry-run", "cmd": cmd, "file": None})
            continue
        if requests_made:
            sleeper(rng.uniform(*config.PANEL_REQUEST_GAP_S))
        safeio.mkdir(out_dir)
        r = runner(cmd)
        requests_made += 1
        out, err = getattr(r, "stdout", "") or "", getattr(r, "stderr", "") or ""
        rc = getattr(r, "returncode", 1)
        f = _file_for(out_dir, code)
        if rc == 0 and f:
            # a landed file is a success whatever the progress lines say ("429.12KiB/s")
            results.append({"code": code, "ok": True, "reason": "downloaded", "cmd": cmd, "file": f})
            continue
        reason = stop_reason(out, err)
        if reason:
            stopped = reason
            results.append({"code": code, "ok": False, "reason": reason, "cmd": cmd, "file": None})
            continue
        tail = (err.strip().splitlines() or [""])[-1][:200]
        results.append({"code": code, "ok": False, "reason": f"yt-dlp failed: {tail}", "cmd": cmd,
                        "file": f})
    return results


def _file_for(out_dir: Path, code: str) -> str | None:
    for p in sorted(Path(out_dir).glob(code + ".*")) if Path(out_dir).is_dir() else []:
        if p.is_file() and not p.name.endswith(".part"):
            return str(p)
    return None


# --------------------------------------------------------------------------- CLI helpers
@dataclass
class _Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


class _JsonWords:
    """Transcript provider from a JSON list of [start, end, text] (CLI --words)."""

    def __init__(self, path: Path):
        raw = safeio.read_json(path, default=[]) or []
        self._words = [_Word(float(w[0]), float(w[1]), str(w[2])) for w in raw]

    def words(self, clip, start=None, end=None, vad=True):
        return list(self._words)


def _parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_list(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _common_parser() -> argparse.ArgumentParser:
    """--json accepted before or after the subcommand (SUPPRESS so a subparser's default never
    clobbers a flag given at the top level)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    return common


def main(argv: Sequence[str] | None = None) -> int:
    common = _common_parser()
    ap = argparse.ArgumentParser(prog="py -m tools.panel", description=__doc__.splitlines()[0],
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("packet", help="build a review packet for one reel", parents=[common])
    p.add_argument("video", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--hook", default=None)
    p.add_argument("--words", type=Path, default=None, help="JSON [[start,end,text],...] transcript")
    p.add_argument("--every", type=float, default=2.0)

    a_ = sub.add_parser("anonymize", help="copy packets under shuffled neutral ids", parents=[common])
    a_.add_argument("dirs", nargs="+", type=Path)
    a_.add_argument("--round", required=True)
    a_.add_argument("--seed", type=int, default=7)
    a_.add_argument("--truth", type=Path, default=None, help="JSON {source: winner|median|flop|candidate}")

    c = sub.add_parser("calibrate", help="pair maths for a critic ranking", parents=[common])
    c.add_argument("--round", required=True)
    c.add_argument("--ranking", required=True, help="P03,P01,... best first")
    c.add_argument("--calls", default="", help="P03=breakout,P01=flop")
    c.add_argument("--truth", type=Path, default=None)

    pl = sub.add_parser("plan", help="which panel reels to download", parents=[common])
    pl.add_argument("--mode", choices=("burnin", "daily"), required=True)
    pl.add_argument("--out", type=Path, default=None, help="skip codes already in this folder")
    pl.add_argument("--already", default="", help="comma list of shortcodes to skip")

    dl = sub.add_parser("download", help="download panel reels with yt-dlp (dry run unless --run)",
                        parents=[common])
    dl.add_argument("--mode", choices=("burnin", "daily"), required=True)
    dl.add_argument("--out", type=Path, required=True)
    dl.add_argument("--run", action="store_true")

    a = ap.parse_args(argv)
    as_json = bool(getattr(a, "json", False))
    try:
        if a.cmd == "packet":
            provider = _JsonWords(a.words) if a.words else None
            res = build_packet(a.video, a.out, transcript_provider=provider, hook=a.hook, every=a.every)
            print(json.dumps(res, indent=2) if as_json else
                  f"packet {res['dir']} length={res['length_s']}s sheet_frames={res['sheet_frames']}")
            return 0
        if a.cmd == "anonymize":
            truth = safeio.read_json(a.truth, default=None) if a.truth else None
            res = anonymize(a.dirs, a.round, a.seed, truth=truth)
            print(json.dumps(res, indent=2) if as_json else
                  f"round {res['round']}: {res['n']} packets in {res['dir']} "
                  f"(mapping kept in {res['mapping_file']}; give the critic only the P??/ folders)")
            return 0
        if a.cmd == "calibrate":
            mapping = load_mapping(a.round)
            truth = safeio.read_json(a.truth, default=None) if a.truth else None
            res = calibration(_parse_list(a.ranking), _parse_kv(a.calls), mapping, truth)
            print(json.dumps(res, indent=2) if as_json else
                  f"pairs {res['pairs_correct']}/{res['pairs_total']} = {res['pair_rate']:.2f} "
                  f"{'CALIBRATED' if res['calibrated'] else 'UNCALIBRATED'} "
                  f"winners_called_flop={res['winners_called_flop']}")
            return 0 if res["calibrated"] else 1
        if a.cmd == "plan":
            already = set(_parse_list(a.already)) | (existing_codes(a.out) if a.out else set())
            codes = download_plan(a.mode, already)
            print(json.dumps(codes) if as_json else " ".join(codes) or "(nothing to download)")
            return 0
        if a.cmd == "download":
            codes = download_plan(a.mode, existing_codes(a.out))
            res = download(codes, a.out, dry_run=not a.run)
            if as_json:
                print(json.dumps(res, indent=2))
            else:
                for r in res:
                    print(f"{'ok ' if r['ok'] else 'NO '}{r['code']} {r['reason']}")
            return 0 if all(r["ok"] for r in res) else 1
    except (OSError, RuntimeError, ValueError, safeio.SafeIOError) as e:
        if as_json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
