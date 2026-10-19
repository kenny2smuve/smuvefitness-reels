"""Quality gate for ClaudInk reels: G1-G12 (START_HERE 6D), one report per reel.

What: ``py -m tools.gates <reel.mp4> --type story|broll|th [...]`` measures a finished reel
(plus its EDL, chat copy, transcript and the ledgers) against the twelve gates that predict
reach on his page (WEAK_POINTS W5 hooks, W7 frame 1, W17 "gate only what predicts reach") and
writes ``<stem>.gates.json`` next to the reel. A reel passes when every ``hard`` gate is ok.
Entries that need eyes (G4 needle/approval/reaction/camera, G12 TV/colour/static opener) are
``hard: false`` with ``detail: "needs critic ..."`` and are confirmed by ``panel.py``.

How the picture is measured: one 8-bit 360x640 proxy of the reel is built in ``cache/gates/``
(HLG sources go through the hable tonemap chain, SDR sources only through ``format=yuv420p``;
``proxy_chain(is_hlg)`` exposes that chain so ``calibrate`` runs his winners through the same
pipeline). Everything else (frame-1 luma std, motion, face, black/freeze detection, scene
changes) is read from that proxy. Letterbox bars are stripped first: the active picture area
is the rows whose mean luma over the first 10 proxy frames is above 16. The G11 frame-1 phash
is tools.fingerprint.frame1_phash on the master, the same function that filled the store, so
both sides of the comparison are the same bits.

``py -m tools.gates calibrate <winner videos...>`` writes ``data/calibration.json`` with the
motion threshold (0.6 x the least-moving winner) that ``--calibration`` then uses for G1/G2.

Library functions return data and never print. Every file write goes through tools.safeio,
every ffmpeg/ffprobe call through safeio.run_ffmpeg / run_ffmpeg_analysis / run_ffprobe.
Time stamps are parameters (``now``, ``today``) so tests are deterministic; config.TZ only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config, safeio

# --------------------------------------------------------------------------- constants
PROXY_W, PROXY_H = 360, 640
ACTIVE_LUMA_MIN = 16.5           # rows above this mean luma (first 10 proxy frames) are picture
ACTIVE_FRAMES = 10
MOTION_FPS = 10                  # motion = mean |diff| between proxy frames sampled at 10 fps
MOTION_SPAN_S = 1.0
FRAME1_T = 0.03
TAIL_WINDOW_S = 0.060            # G6: last 60 ms before the out-point
TAIL_RATIO_MAX = 0.5             # G6: tail RMS must stay under 0.5 x the speech RMS ...
TAIL_RMS_FLOOR = 0.005           # ... unless it is below this absolute floor
WORD_END_MARGIN_S = 0.03         # G6: the last word must end 30 ms before the out-point
WORD_WINDOW_BEFORE_S = 1.0
WORD_WINDOW_AFTER_S = 1.8
FREEZE_NOISE = 0.003
SCDET_THRESHOLD = 10
CALIBRATION_FACTOR = 0.6
PROXY_CRF = "23"
TYPES = ("story", "broll", "th")
SUBJECT_TAGS = frozenset({"greeting", "design", "stencil", "healed", "piece", "station", "face"})
STAKES_TAGS = frozenset({"stakes", "hours", "travel", "first-tattoo", "return", "pain", "healed"})
ALLOWED_TRANSFERS = ("arib-std-b67", "bt709")
# edl.py / render_story.py record the export policy word; ffprobe reports the stream tag
TRANSFER_WORDS = {"hlg": "arib-std-b67", "sdr": "bt709"}
GATES: tuple[tuple[str, str], ...] = (
    ("G1", "frame1"), ("G2", "opening"), ("G3", "window3_10"), ("G4", "story_content"),
    ("G5", "clean_picture"), ("G6", "voices"), ("G7", "order_and_sound"), ("G8", "length"),
    ("G9", "spec"), ("G10", "hooks_caption"), ("G11", "uniqueness"), ("G12", "pieces_visual"),
)
GATE_NAMES = dict(GATES)
# deliveries.csv rows that never reached post/<date>/ (mirrors tools.ledgers.DELIVERY_NOT_COUNTED)
DELIVERY_NOT_COUNTED = ("held", "legacy", "dryrun", "cancelled")
NUMPY_HINT = "numpy is required: pip install --user numpy"
CV2_HINT = "pip install --user opencv-python"
FINGERPRINT_HINT = "tools.fingerprint needs pillow and numpy: pip install --user pillow numpy"
HEX_RE = re.compile(r"[0-9a-fA-F]{8,64}")
# keys of a signatures file that group entries rather than name one (the parent key is the id)
STRUCTURAL_KEYS = frozenset({"sig", "signature", "signatures", "frame1_hashes", "hashes", "frames",
                             "posted", "delivery", "entries", "items", "reels"})


class GatesError(RuntimeError):
    """A measurement could not be made (ffmpeg/ffprobe failed, file unreadable)."""


def _np():
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover - numpy is a hard dependency of the kit
        raise ImportError(NUMPY_HINT) from e
    return np


# --------------------------------------------------------------------------- transcript provider
@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


class JsonTranscript:
    """Transcript provider built from ``words.json``: ``{clip: [[start, end, text], ...]}``
    (clip seconds). Duck-types tools.transcribe / tools.tests.stub_transcript: ``words(clip,
    start, end, vad)`` returns the words overlapping [start, end]; ``has(clip)`` says whether
    the clip was transcribed at all.

    Clips are looked up by the path as written in the EDL first and by bare file name only as
    a fallback: Meta glasses exports are ``video-001.mp4`` in every session folder, so a bare
    name shared by two transcribed paths is ambiguous and ``has()`` answers False for it."""

    def __init__(self, words_by_clip: dict):
        self.by_path: dict[str, list[Word]] = {}
        self.by_name: dict[str, list[Word] | None] = {}      # None = ambiguous bare name
        for k, v in (words_by_clip or {}).items():
            if isinstance(v, dict):
                v = v.get("words") or []
            ws = [self._word(w) for w in (v or [])]
            self.by_path[self._path_key(k)] = ws
            name = Path(str(k)).name
            self.by_name[name] = None if name in self.by_name else ws

    @staticmethod
    def _path_key(p) -> str:
        return str(p).replace("\\", "/").strip().lower()

    def _lookup(self, clip) -> list[Word] | None:
        ws = self.by_path.get(self._path_key(clip))
        if ws is not None:
            return ws
        return self.by_name.get(Path(str(clip)).name)

    @staticmethod
    def _word(w) -> Word:
        if isinstance(w, Word):
            return w
        if isinstance(w, dict):
            return Word(float(w.get("start", 0)), float(w.get("end", 0)), str(w.get("text", "")),
                        float(w.get("prob", w.get("probability", 1.0)) or 1.0))
        w = list(w)
        prob = float(w[3]) if len(w) > 3 else 1.0
        return Word(float(w[0]), float(w[1]), str(w[2]) if len(w) > 2 else "", prob)

    @classmethod
    def load(cls, path: Path | str) -> "JsonTranscript":
        obj = safeio.read_json(path, default=None)
        if not isinstance(obj, dict):
            raise GatesError(f"transcript is not a JSON object of clip -> words: {path}")
        return cls(obj)

    def has(self, clip: Path | str) -> bool:
        return self._lookup(clip) is not None

    def words(self, clip: Path | str, start: float | None = None, end: float | None = None,
              vad: bool = True) -> list[Word]:
        ws = self._lookup(clip) or []
        lo = -1e9 if start is None else start
        hi = 1e9 if end is None else end
        return [w for w in ws if w.end >= lo and w.start <= hi]


# --------------------------------------------------------------------------- probe
def _parse_rate(s: str | None) -> float:
    if not s:
        return 0.0
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            return float(a) / float(b) if float(b) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def probe(path: Path | str) -> dict:
    """ffprobe summary of a video file: codec, size, pix_fmt, colour tags, fps, duration,
    audio presence, file size. Raises GatesError when the file cannot be probed."""
    p = Path(path)
    if not p.is_file():
        raise GatesError(f"file not found: {p}")
    r = safeio.run_ffprobe(["-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)])
    if r.returncode != 0 or not r.stdout.strip():
        raise GatesError(f"ffprobe failed on {p.name}: {r.stderr.strip()[:200]}")
    d = json.loads(r.stdout)
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise GatesError(f"no video stream: {p.name}")
    fmt = d.get("format") or {}
    dur = fmt.get("duration") or v.get("duration") or 0
    try:
        duration = float(dur)
    except (TypeError, ValueError):
        duration = 0.0
    rotation = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return {
        "path": str(p), "codec": v.get("codec_name") or "", "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0), "pix_fmt": v.get("pix_fmt") or "",
        "color_transfer": v.get("color_transfer") or "", "color_primaries": v.get("color_primaries") or "",
        "fps": _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate")),
        "duration": duration, "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "rotation": rotation, "size_bytes": p.stat().st_size,
    }


def is_hlg(info: dict) -> bool:
    return (info.get("color_transfer") or "") == "arib-std-b67"


# --------------------------------------------------------------------------- proxy
def proxy_chain(is_hlg: bool) -> str:
    """The 8-bit conversion applied to every proxy (winners and candidates alike)."""
    if is_hlg:
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return "format=yuv420p"


def proxy_filter(is_hlg: bool) -> str:
    """Scale (never upscale beyond 360x640, keep aspect, pad to the proxy canvas) then convert."""
    return (f"scale={PROXY_W}:{PROXY_H}:force_original_aspect_ratio=decrease:flags=bicubic,"
            f"pad={PROXY_W}:{PROXY_H}:(ow-iw)/2:(oh-ih)/2,{proxy_chain(is_hlg)}")


def cache_key(path: Path | str) -> str:
    p = Path(path)
    st = p.stat()
    h = hashlib.sha1(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8"))
    return h.hexdigest()[:10]


def cache_dir() -> Path:
    return safeio.mkdir(config.CACHE / "gates")


def proxy_path(reel: Path | str) -> Path:
    r = Path(reel)
    return cache_dir() / f"{r.stem}.{cache_key(r)}.proxy.mp4"


class MeasureCache:
    """Measurements of one proxy (frame stats, black/freeze/scene analysis, audio RMS per
    window) kept in cache/gates/<proxy>.measure.json so re-gating the same file (HOLD cuts,
    EDL-only fixes) does not decode it again. The file is keyed by the proxy name, which
    already carries the reel's size/mtime hash."""

    def __init__(self, path: Path):
        self.path = path
        obj = safeio.read_json(path, default=None)
        self.data: dict = obj if isinstance(obj, dict) else {}
        self.dirty = False

    def get(self, section: str, key: str | None = None):
        sec = self.data.get(section)
        if key is None:
            return sec
        return sec.get(key) if isinstance(sec, dict) else None

    def set(self, section: str, value, key: str | None = None) -> None:
        if key is None:
            self.data[section] = value
        else:
            self.data.setdefault(section, {})[key] = value
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            safeio.write_json(self.path, self.data, overwrite=True)
            self.dirty = False


def measure_cache_path(proxy: Path | str) -> Path:
    p = Path(proxy)
    return p.with_name(p.stem + ".measure.json")


def build_proxy(reel: Path | str, info: dict | None = None, *, force: bool = False) -> Path:
    """Build (or reuse) the 360x640 8-bit proxy of a reel in cache/gates/."""
    r = Path(reel)
    info = info or probe(r)
    out = proxy_path(r)
    if out.exists() and out.stat().st_size > 0 and not force:
        return out
    args = ["-i", str(r), "-an", "-sn", "-dn", "-vf", proxy_filter(is_hlg(info)),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", PROXY_CRF, "-pix_fmt", "yuv420p"]
    res = safeio.run_ffmpeg(args, out, overwrite=True)
    if res.returncode != 0 or not out.exists():
        raise GatesError(f"proxy build failed for {r.name}: {res.stderr.strip()[-300:]}")
    return out


def gray_frames(video: Path | str, out: Path | str, *, w: int = PROXY_W, h: int = PROXY_H,
                t_end: float | None = None, frames: int | None = None, fps: int | None = None):
    """Decode frames of a (proxy) video to an (n, h, w) uint8 luma array via a raw dump in cache."""
    np = _np()
    args = ["-i", str(video)]
    if t_end is not None:
        args += ["-t", f"{t_end:.3f}"]
    if frames is not None:
        args += ["-frames:v", str(frames)]
    if fps:
        args += ["-vf", f"fps={fps}"]
    args += ["-an", "-sn", "-dn", "-f", "rawvideo", "-pix_fmt", "gray"]
    res = safeio.run_ffmpeg(args, out, overwrite=True)
    if res.returncode != 0 or not Path(out).exists():
        raise GatesError(f"frame decode failed: {res.stderr.strip()[-300:]}")
    data = np.fromfile(str(out), dtype=np.uint8)
    n = data.size // (w * h)
    if n == 0:
        raise GatesError(f"no frames decoded from {Path(video).name}")
    return data[: n * w * h].reshape(n, h, w)


# --------------------------------------------------------------------------- frame measurements
@dataclass
class FrameStats:
    active: tuple[int, int]            # [y0, y1) rows of the active picture in the proxy
    luma_std: float                    # std of the frame at 0.03 s inside the active area
    motion: list[float]                # |diff| between 10 fps samples over 0-1 s (10 values)
    face: bool | None                  # None = detector unavailable
    frame1: Any = field(default=None, repr=False)   # the 0.03 s luma frame (full proxy)

    def motion_over(self, seconds: float) -> float:
        n = max(1, int(round(seconds * MOTION_FPS)))
        vals = self.motion[:n]
        return float(sum(vals) / len(vals)) if vals else 0.0


def active_rows(frames) -> tuple[int, int]:
    """Rows whose mean luma over the given frames is above ACTIVE_LUMA_MIN -> (y0, y1), even."""
    np = _np()
    rows = frames.astype(np.float32).mean(axis=(0, 2))
    idx = np.where(rows > ACTIVE_LUMA_MIN)[0]
    h = frames.shape[1]
    if idx.size == 0:
        return (0, h)
    y0, y1 = int(idx.min()), int(idx.max()) + 1
    y0 -= y0 % 2
    y1 += y1 % 2
    y1 = min(y1, h)
    if y1 - y0 < 4:
        return (0, h)
    return (y0, y1)


def detect_face(frames) -> bool | None:
    """cv2 Haar frontal face on any of the frames; None when OpenCV or its cascade is missing."""
    try:
        import cv2  # noqa: WPS433  (lazy: heavy optional)
    except ImportError:
        return None
    cls = getattr(cv2, "CascadeClassifier", None)
    data = getattr(cv2, "data", None)
    if cls is None or data is None:
        return None
    xml = Path(getattr(data, "haarcascades", "")) / "haarcascade_frontalface_default.xml"
    if not xml.is_file():
        return None
    try:
        cascade = cls(str(xml))
        if cascade.empty():
            return None
        for f in frames:
            faces = cascade.detectMultiScale(f, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24))
            if len(faces):
                return True
    except Exception:  # cv2 raises cv2.error subclasses; any failure = detector unusable
        return None
    return False


def measure_frames(proxy: Path | str, fps: float, key: str, cache: MeasureCache | None = None) -> FrameStats:
    """Active area, frame-1 luma std, 0-1 s motion series and face flag from the proxy.
    With a MeasureCache the numbers come from cache/gates/ when the proxy was measured before."""
    np = _np()
    cd = cache_dir()
    stem = Path(proxy).stem
    first_path = cd / f"{stem}.first.gray"
    cached = cache.get("frames") if cache is not None else None
    if isinstance(cached, dict) and cached.get("fps") == fps and first_path.is_file():
        try:
            native = np.fromfile(str(first_path), dtype=np.uint8)
            n = native.size // (PROXY_W * PROXY_H)
            native = native[: n * PROXY_W * PROXY_H].reshape(n, PROXY_H, PROXY_W)
            idx = min(n - 1, int(cached["frame1_index"]))
            return FrameStats(active=tuple(cached["active"]), luma_std=float(cached["luma_std"]),
                              motion=[float(x) for x in cached["motion"]], face=cached.get("face"),
                              frame1=native[idx])
        except (KeyError, TypeError, ValueError, OSError):
            pass
    native = gray_frames(proxy, first_path, frames=ACTIVE_FRAMES)
    act = active_rows(native)
    y0, y1 = act
    idx = min(len(native) - 1, max(0, int(round(FRAME1_T * (fps or 30.0)))))
    frame1 = native[idx]
    luma_std = float(frame1[y0:y1].astype(np.float32).std())
    sampled = gray_frames(proxy, cd / f"{stem}.m10.gray", t_end=MOTION_SPAN_S + 0.5 / MOTION_FPS,
                          fps=MOTION_FPS)
    act_s = sampled[:, y0:y1].astype(np.float32)
    if act_s.shape[0] >= 2:
        diffs = np.abs(np.diff(act_s, axis=0)).mean(axis=(1, 2))
        motion = [float(x) for x in diffs]
    else:
        motion = [0.0]
    fs = FrameStats(active=act, luma_std=luma_std, motion=motion, face=detect_face(sampled), frame1=frame1)
    if cache is not None:
        cache.set("frames", {"fps": fps, "active": list(act), "luma_std": luma_std, "motion": motion,
                             "face": fs.face, "frame1_index": idx})
    return fs


# --------------------------------------------------------------------------- ffmpeg analysis
_BLACK_RE = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)")
_FREEZE_START_RE = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END_RE = re.compile(r"freeze_end:\s*([\d.]+)")
_SCD_RE = re.compile(r"lavfi\.scd\.time:\s*([\d.]+)")


def picture_analysis(proxy: Path | str, active: tuple[int, int], duration: float, *,
                     scenes: bool = False, cache: MeasureCache | None = None) -> dict:
    """One analysis pass over the proxy cropped to the active area: blackdetect (config),
    freezedetect (>= FREEZE_MAX_S) and, when asked, scdet scene changes."""
    cached = cache.get("analysis") if cache is not None else None
    if (isinstance(cached, dict) and cached.get("active") == list(active)
            and (not scenes or cached.get("scenes") is not None)):
        return {"black": [tuple(x) for x in cached["black"]], "freezes": [tuple(x) for x in cached["freezes"]],
                "scenes": list(cached.get("scenes") or [])}
    y0, y1 = active
    vf = [f"crop={PROXY_W}:{y1 - y0}:0:{y0}"]
    if scenes:
        vf.append(f"scdet=t={SCDET_THRESHOLD}")
    vf += [config.BLACKDETECT, f"freezedetect=n={FREEZE_NOISE}:d={config.FREEZE_MAX_S}"]
    res = safeio.run_ffmpeg_analysis(["-v", "info", "-nostats", "-i", str(proxy), "-an", "-sn", "-dn",
                                      "-vf", ",".join(vf)])
    if res.returncode != 0:
        raise GatesError(f"picture analysis failed: {res.stderr.strip()[-300:]}")
    log = res.stderr
    black = [(float(a), float(b)) for a, b in _BLACK_RE.findall(log)]
    starts = [float(x) for x in _FREEZE_START_RE.findall(log)]
    ends = [float(x) for x in _FREEZE_END_RE.findall(log)]
    freezes = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        freezes.append((s, e))
    scene_times = sorted({round(float(x), 3) for x in _SCD_RE.findall(log)})
    if cache is not None:
        cache.set("analysis", {"active": list(active), "black": black, "freezes": freezes,
                               "scenes": scene_times if scenes else None})
    return {"black": black, "freezes": freezes, "scenes": scene_times}


def audio_window(src: Path | str, start: float, end: float, out: Path | str):
    """Voice-band (300-3400 Hz) mono 16 kHz float32 samples of [start, end] of a file."""
    np = _np()
    start = max(0.0, start)
    dur = end - start
    if dur <= 0:
        return np.zeros(0, dtype=np.float32)
    lo, hi = config.VOICE_BAND_HZ
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-vn", "-sn", "-dn",
            "-af", f"highpass=f={lo},lowpass=f={hi}", "-f", "f32le", "-ac", "1", "-ar", "16000"]
    res = safeio.run_ffmpeg(args, out, overwrite=True)
    if res.returncode != 0 or not Path(out).exists():
        raise GatesError(f"audio decode failed for {Path(src).name}: {res.stderr.strip()[-200:]}")
    return np.fromfile(str(out), dtype=np.float32)


def rms(x) -> float:
    np = _np()
    if x is None or x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def voice_tail_rms(src: Path | str, a: float, b: float, out: Path | str, *,
                   cache: MeasureCache | None = None) -> tuple[float, float]:
    """(RMS of the last 60 ms before b, RMS of the middle third of [a, b]) in the voice band
    of ``src``. Cached per source file state and window."""
    p = Path(src)
    st = p.stat()
    key = f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}|{a:.3f}|{b:.3f}"
    hit = cache.get("audio", key) if cache is not None else None
    if isinstance(hit, list) and len(hit) == 2:
        return float(hit[0]), float(hit[1])
    x = audio_window(p, a, b + 0.05, out)
    sr = 16000
    n_seg = int(round((b - a) * sr))
    tail_n = int(TAIL_WINDOW_S * sr)
    if x.size < n_seg - tail_n:
        # fail closed: an out-point past the end of the clip's audio would read as "a pause"
        raise GatesError(f"audio shorter than the seg: {x.size / sr:.2f}s decoded for "
                         f"{a:.2f}-{b:.2f}s of {p.name}")
    seg_audio = x[:n_seg]
    third = max(1, n_seg // 3)
    speech_rms = rms(seg_audio[third: 2 * third])
    tail_rms = rms(seg_audio[max(0, n_seg - tail_n): n_seg])
    if cache is not None:
        cache.set("audio", [tail_rms, speech_rms], key)
    return tail_rms, speech_rms


# --------------------------------------------------------------------------- EDL helpers
def load_edl(path: Path | str) -> dict:
    obj = safeio.read_json(path, default=None)
    if not isinstance(obj, dict) or not isinstance(obj.get("segs"), list):
        raise GatesError(f"EDL is not an object with a segs list: {path}")
    return obj


def find_edl(reel: Path | str) -> Path | None:
    """Default EDL: <stem>.edl.json next to the reel, else <name>.edl.json."""
    r = Path(reel)
    for cand in (r.with_name(r.stem + ".edl.json"), r.with_name(r.name + ".edl.json")):
        if cand.is_file():
            return cand
    return None


def find_chat(reel: Path | str) -> Path:
    r = Path(reel)
    return r.with_name(f"{r.stem}_chat{r.suffix}")


def seg_in(seg: dict) -> float:
    return float(seg.get("in") or 0.0)


def seg_out(seg: dict) -> float:
    return float(seg.get("out") or 0.0)


def seg_dur(seg: dict) -> float:
    return max(0.0, seg_out(seg) - seg_in(seg))


def seg_kind(seg: dict) -> str:
    return str(seg.get("kind") or "").strip().upper()


def seg_tags(seg: dict) -> set[str]:
    return {str(t).strip().lower() for t in (seg.get("tags") or []) if str(t).strip()}


def timeline(edl: dict | None) -> list[tuple[float, float, dict]]:
    """[(t0, t1, seg)] on the reel's timeline (sum of previous seg durations)."""
    out: list[tuple[float, float, dict]] = []
    t = 0.0
    for seg in (edl or {}).get("segs") or []:
        if not isinstance(seg, dict):
            continue
        d = seg_dur(seg)
        out.append((t, t + d, seg))
        t += d
    return out


def edl_duration(edl: dict | None) -> float:
    tl = timeline(edl)
    return tl[-1][1] if tl else 0.0


def order_key_sortable(key) -> tuple:
    """ISO datetimes -> (0, epoch seconds); anything else -> (1, string). Uses
    tools.edl.order_key_sortable when that module is importable."""
    try:
        from . import edl as _edl  # noqa: WPS433 (another builder's module)
        fn = getattr(_edl, "order_key_sortable", None)
        if callable(fn):
            return tuple(fn(key))
    except ImportError:
        pass
    except Exception:
        pass
    s = str(key if key is not None else "").strip()
    if not s:
        return (2, "")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return (1, s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TZ)
    return (0, dt.timestamp())


def staged_clip_path(src: Path | str) -> Path | None:
    """Where tools.encode.stage_clip puts a footage clip: cache/clips/<sha1(name|size|mtime)[:12]>_<name>
    (the same key, read from encode when it exposes a helper). None when the source cannot be stat'ed."""
    s = Path(str(src))
    try:
        from . import encode  # noqa: WPS433 (another builder's module)
        for name in ("staged_path", "stage_path", "staged_clip_path"):
            fn = getattr(encode, name, None)
            if callable(fn):
                return Path(fn(s))
    except ImportError:
        pass
    try:
        st = s.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{s.name}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:12]
    return config.CACHE / "clips" / f"{key}_{s.name}"


def resolve_clip(clip: str | None, edl_path: Path | None) -> Path | None:
    """The source clip on this machine, local copies first so Drive never streams during a
    gate run: the copy encode.py staged in cache/clips/ (by its name+size+mtime key, else the
    single cache/clips/*_<name> hit), then the exact EDL path, then the same file name next to
    the EDL, then a bare cache/clips/<name>. None when nothing exists (gates then use the reel)."""
    if not clip:
        return None
    p = Path(str(clip))
    clips_dir = config.CACHE / "clips"
    cands: list[Path] = []
    staged = staged_clip_path(p)
    if staged is not None:
        cands.append(staged)
    try:
        hits = sorted(clips_dir.glob(f"*_{p.name}")) if clips_dir.is_dir() else []
    except OSError:
        hits = []
    if len(hits) == 1:
        cands.append(hits[0])
    cands.append(p)
    if edl_path is not None:
        cands.append(edl_path.parent / p.name)
    cands.append(clips_dir / p.name)
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- thresholds
def load_thresholds(calibration: Path | str | None = None) -> dict:
    """{'motion', 'luma_std', 'source'}: config defaults, overridden by data/calibration.json
    (or the given file) when present."""
    out = {"motion": float(config.FRAME1_MOTION_MIN), "luma_std": float(config.FRAME1_LUMA_STD_MIN),
           "source": "config"}
    p = Path(calibration) if calibration else config.DATA / "calibration.json"
    obj = safeio.read_json(p, default=None) if p.is_file() else None
    if isinstance(obj, dict):
        try:
            if obj.get("motion_threshold") is not None:
                out["motion"] = float(obj["motion_threshold"])
                out["source"] = str(p)
            if obj.get("luma_std_min") is not None:
                out["luma_std"] = float(obj["luma_std_min"])
                out["source"] = str(p)
        except (TypeError, ValueError):
            pass
    return out


# --------------------------------------------------------------------------- gate plumbing
def _gate(gid: str, ok: bool, value: dict, detail: str, *, hard: bool = True) -> dict:
    return {"id": gid, "name": GATE_NAMES[gid], "hard": hard, "ok": bool(ok), "value": value,
            "detail": detail}


def _na(gid: str, rtype: str) -> list[dict]:
    return [_gate(gid, True, {}, f"not applicable to {rtype}")]


def _fmt(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}"


@dataclass
class Ctx:
    reel: Path
    rtype: str
    today: date
    now: datetime
    info: dict | None = None
    probe_error: str = ""
    edl: dict | None = None
    edl_path: Path | None = None
    edl_error: str = ""
    tl: list = field(default_factory=list)
    frames: FrameStats | None = None
    analysis: dict | None = None
    measure_error: str = ""
    thresholds: dict = field(default_factory=dict)
    transcript: Any = None
    chat: Path | None = None
    signatures: Path | None = None
    template: Path | None = None
    multi_day: bool = False
    proxy: Path | None = None
    key: str = ""
    mcache: MeasureCache | None = None

    @property
    def duration(self) -> float:
        if self.info and self.info.get("duration"):
            return float(self.info["duration"])
        return edl_duration(self.edl)

    def first_tags(self) -> set[str]:
        return seg_tags(self.tl[0][2]) if self.tl else set()

    def words_for_seg(self, seg: dict) -> list[Word] | None:
        """Transcript words of a seg's clip around [in, out] (clip seconds). None when there is
        no transcript at all, none for this clip (a partial words.json during the fix loop) or
        the provider has no words for the clip at all: callers then fall back to the EDL
        I-seg rule instead of treating the seg as silent."""
        if self.transcript is None:
            return None
        clip = seg.get("clip")
        if not clip:
            return None
        has = getattr(self.transcript, "has", None)
        try:
            if callable(has) and not has(clip):
                return None
            ws = list(self.transcript.words(Path(str(clip)), seg_in(seg) - WORD_WINDOW_BEFORE_S,
                                            seg_out(seg) + WORD_WINDOW_AFTER_S, vad=False))
            if not ws and not list(self.transcript.words(Path(str(clip)), None, None, vad=False)):
                return None
            return ws
        except Exception:
            return None

    def reel_words(self) -> list[Word]:
        """Words keyed by the reel's own file name (timeline seconds), when the transcript has them."""
        if self.transcript is None:
            return []
        has = getattr(self.transcript, "has", None)
        if callable(has) and not has(self.reel):
            return []
        try:
            return list(self.transcript.words(self.reel, None, None, vad=False))
        except Exception:
            return []


# --------------------------------------------------------------------------- G1 frame 1
def gate_g1(ctx: Ctx) -> list[dict]:
    if ctx.frames is None:
        return [_gate("G1", False, {}, f"no proxy measurement: {ctx.measure_error or ctx.probe_error}")]
    fs, thr = ctx.frames, ctx.thresholds
    mot = fs.motion_over(MOTION_SPAN_S)
    first = ctx.first_tags()
    tag_hit = sorted(first & SUBJECT_TAGS)
    reasons: list[str] = []
    if fs.luma_std < thr["luma_std"]:
        reasons.append(f"luma std {_fmt(fs.luma_std, 1)} < {_fmt(thr['luma_std'], 0)} (dark or flat opener)")
    if mot < thr["motion"]:
        reasons.append(f"motion {_fmt(mot)} < {_fmt(thr['motion'])} (static opener)")
    if not fs.face and not tag_hit:
        if ctx.edl is None:
            reasons.append("no face and no EDL tag")
        else:
            reasons.append(f"no face detected and first seg tags {sorted(first)} name no subject")
    value = {"luma_std": round(fs.luma_std, 2), "motion": round(mot, 3), "face": fs.face,
             "first_seg_tags": sorted(first), "active_rows": list(fs.active), "active_luma_min": ACTIVE_LUMA_MIN,
             "motion_threshold": thr["motion"], "luma_std_min": thr["luma_std"]}
    if reasons:
        detail = "; ".join(reasons)
    else:
        subject = "face" if fs.face else "tags " + ",".join(tag_hit)
        detail = (f"luma std {_fmt(fs.luma_std, 1)} >= {_fmt(thr['luma_std'], 0)}, motion {_fmt(mot)} >= "
                  f"{_fmt(thr['motion'])}, subject: {subject}")
    if fs.face is None:
        detail += f" (face detector unavailable: {CV2_HINT})"
    return [_gate("G1", not reasons, value, detail)]


# --------------------------------------------------------------------------- G2 opening
def gate_g2(ctx: Ctx) -> list[dict]:
    if ctx.frames is None:
        return [_gate("G2", False, {}, f"no proxy measurement: {ctx.measure_error or ctx.probe_error}")]
    fs, thr = ctx.frames, ctx.thresholds
    if ctx.rtype == "broll":
        mot = fs.motion_over(0.5)
        station = "station" in ctx.first_tags()
        reasons = []
        if ctx.edl is None:
            reasons.append("no EDL: first shot unknown")
        elif not station:
            reasons.append(f"first seg is not tagged station ({sorted(ctx.first_tags())})")
        if mot < thr["motion"]:
            reasons.append(f"station shot motion {_fmt(mot)} < {_fmt(thr['motion'])} over 0-0.5 s")
        value = {"motion_0_0.5": round(mot, 3), "station_first": station}
        return [_gate("G2", not reasons, value,
                      "; ".join(reasons) or f"station first, motion {_fmt(mot)} over 0-0.5 s")]

    speech_t: float | None = None
    speech_src = "none"
    if ctx.tl:
        t0, _t1, seg = ctx.tl[0]
        ws = ctx.words_for_seg(seg)
        if ws is not None:
            starts = [max(0.0, w.start - seg_in(seg)) for w in ws if w.end > seg_in(seg)]
            if starts:
                speech_t, speech_src = min(starts), "transcript"
        elif seg_kind(seg) == "I":
            speech_t = 0.0
            speech_src = "EDL I seg" if ctx.transcript is None else "EDL I seg (no transcript for clip)"
    rw = ctx.reel_words() if speech_t is None else []
    if rw:
        speech_t, speech_src = max(0.0, min(w.start for w in rw)), "transcript (reel)"
    mot03 = fs.motion_over(0.3)
    speech_ok = speech_t is not None and speech_t <= config.OPEN_WITHIN_S
    motion_ok = mot03 >= thr["motion"]
    stakes_t: float | None = None
    for t0, _t1, seg in ctx.tl:
        if seg_tags(seg) & STAKES_TAGS:
            stakes_t = t0 if stakes_t is None else min(stakes_t, t0)
    stakes_ok = stakes_t is not None and stakes_t <= config.STAKES_BY_S + 1e-6
    reasons = []
    if not (speech_ok or motion_ok):
        reasons.append(f"nothing within {config.OPEN_WITHIN_S}s: speech at "
                       f"{'none' if speech_t is None else _fmt(speech_t)}, motion {_fmt(mot03)} < {_fmt(thr['motion'])}")
    if not stakes_ok:
        if ctx.edl is None:
            reasons.append("no EDL: stakes unknown")
        elif stakes_t is None:
            reasons.append(f"no seg tagged {sorted(STAKES_TAGS)} (stakes never said)")
        else:
            reasons.append(f"stakes seg starts at {_fmt(stakes_t, 1)}s > {config.STAKES_BY_S}s")
    value = {"speech_start": None if speech_t is None else round(speech_t, 3), "speech_source": speech_src,
             "motion_0_0.3": round(mot03, 3), "stakes_at": None if stakes_t is None else round(stakes_t, 3)}
    ok_detail = (f"speech at {'none' if speech_t is None else _fmt(speech_t)}s ({speech_src}), motion "
                 f"{_fmt(mot03)}, stakes at {_fmt(stakes_t if stakes_t is not None else -1, 1)}s")
    return [_gate("G2", not reasons, value, "; ".join(reasons) or ok_detail)]


# --------------------------------------------------------------------------- G3 window 3-10
def dead_gaps(cuts: Iterable[float], speech: Iterable[tuple[float, float]], w0: float, w1: float,
              duration: float) -> list[tuple[float, float, float]]:
    """Gaps with neither a cut nor speech, clipped to [w0, w1]: [(start, end, clipped_len)]."""
    acts = sorted([(float(t), float(t)) for t in cuts] + [(float(a), float(b)) for a, b in speech])
    merged: list[list[float]] = []
    for a, b in acts:
        if merged and a <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    bounds = [(0.0, 0.0)] + [(a, b) for a, b in merged] + [(duration, duration)]
    gaps = []
    for (_a0, e_prev), (s_next, _b1) in zip(bounds, bounds[1:]):
        if s_next <= e_prev:
            continue
        clipped = min(s_next, w1) - max(e_prev, w0)
        if clipped > 0:
            gaps.append((e_prev, s_next, clipped))
    return gaps


def _template_durations(obj) -> list[float] | None:
    """Slot durations of a b-roll template, accepting a few shapes: {"slots": [2.4, ...]},
    {"slots": [{"seconds": 2.4}, ...]}, {"segs"/"shots"/"durations": [...]}, or a bare list."""
    if obj is None:
        return None
    seq = obj
    if isinstance(obj, dict):
        seq = None
        for k in ("slots", "shots", "segs", "durations"):
            if isinstance(obj.get(k), list):
                seq = obj[k]
                break
    if not isinstance(seq, list):
        return None
    out: list[float] = []
    for item in seq:
        if isinstance(item, (int, float)):
            out.append(float(item))
        elif isinstance(item, dict):
            for k in ("seconds", "duration", "dur", "s", "len"):
                if isinstance(item.get(k), (int, float)):
                    out.append(float(item[k]))
                    break
            else:
                if isinstance(item.get("in"), (int, float)) and isinstance(item.get("out"), (int, float)):
                    out.append(float(item["out"]) - float(item["in"]))
    return out or None


def gate_g3(ctx: Ctx) -> list[dict]:
    dur = ctx.duration
    if ctx.rtype == "broll":
        if ctx.tl:
            shots = [seg_dur(seg) for _t0, _t1, seg in ctx.tl]
            src = "EDL"
        elif ctx.analysis is not None:
            pts = [0.0] + [t for t in ctx.analysis["scenes"] if 0 < t < dur] + [dur]
            shots = [b - a for a, b in zip(pts, pts[1:])]
            src = "scdet"
        else:
            return [_gate("G3", False, {}, f"no shot data: {ctx.measure_error or ctx.probe_error}")]
        longest = max(shots) if shots else 0.0
        reasons = []
        if longest > config.BROLL_MAX_SHOT + 1e-6:
            reasons.append(f"longest shot {_fmt(longest)}s > {config.BROLL_MAX_SHOT}s")
        tpl = None
        tpl_path = ctx.template or (config.TEMPLATES / "broll_station_first.json")
        if tpl_path.is_file():
            tpl = _template_durations(safeio.read_json(tpl_path, default=None))
        mismatches: list[str] = []
        if tpl:
            if len(tpl) != len(shots):
                mismatches.append(f"{len(shots)} shots vs {len(tpl)} template slots")
            for i, (a, b) in enumerate(zip(shots, tpl)):
                if abs(a - b) > config.BROLL_TOL + 1e-6:
                    mismatches.append(f"slot {i + 1}: {_fmt(a)}s vs {_fmt(b)}s")
            if mismatches:
                reasons.append("template mismatch: " + "; ".join(mismatches[:4]))
        value = {"shots": [round(s, 3) for s in shots], "longest": round(longest, 3), "source": src,
                 "template": None if tpl is None else [round(x, 3) for x in tpl]}
        detail = "; ".join(reasons) if reasons else (
            f"{len(shots)} shots, longest {_fmt(longest)}s" + (" match the template" if tpl else
                                                              "; template not found, durations unchecked"))
        return [_gate("G3", not reasons, value, detail)]

    # story / th: no stretch over MAX_DEAD_STRETCH_S with neither speech nor a cut in [3, 10]
    w0 = float(config.RETENTION_WINDOW[0])
    speech: list[tuple[float, float]] = []
    speech_src = "none"
    if ctx.tl:
        cuts = [t0 for t0, _t1, _seg in ctx.tl[1:]]
        final_hold = ctx.tl[-1][1] - ctx.tl[-1][0]
        src = "EDL"
        n_transcript = n_fallback = 0
        for t0, t1, seg in ctx.tl:
            ws = ctx.words_for_seg(seg)
            if ws is not None:
                n_transcript += 1
                for w in ws:
                    a = t0 + (w.start - seg_in(seg))
                    b = t0 + (w.end - seg_in(seg))
                    a, b = max(t0, a), min(t1, b)
                    if b > a:
                        speech.append((a, b))
            elif seg_kind(seg) == "I":
                n_fallback += 1
                speech.append((t0, t1))
        if n_transcript and n_fallback:
            speech_src = f"transcript ({n_transcript} segs) + EDL I segs ({n_fallback} without transcript)"
        elif n_transcript:
            speech_src = "transcript"
        elif n_fallback:
            speech_src = "EDL I segs"
    elif ctx.analysis is not None:
        cuts = list(ctx.analysis["scenes"])
        final_hold = float(config.FINISHED_HOLD_MIN_S)
        src = "scdet"
        speech = [(w.start, w.end) for w in ctx.reel_words()]
        speech_src = "transcript (reel)" if speech else "none"
    else:
        return [_gate("G3", False, {}, f"no cut data: {ctx.measure_error or ctx.probe_error}")]
    w1 = min(float(config.RETENTION_WINDOW[1]), dur - final_hold)
    gaps = dead_gaps(cuts, speech, w0, w1, dur) if w1 > w0 else []
    worst = max(gaps, key=lambda g: g[2]) if gaps else None
    bad = [g for g in gaps if g[2] > config.MAX_DEAD_STRETCH_S + 1e-6]
    value = {"window": [round(w0, 2), round(w1, 2)], "cuts_in_window": [round(c, 2) for c in cuts if w0 <= c <= w1],
             "speech_intervals": len(speech), "speech_source": speech_src, "source": src,
             "longest_dead": None if worst is None else [round(worst[0], 2), round(worst[1], 2), round(worst[2], 2)]}
    if bad:
        g = max(bad, key=lambda x: x[2])
        detail = (f"{_fmt(g[2], 1)}s with no speech and no cut at {_fmt(g[0], 1)}-{_fmt(g[1], 1)}s "
                  f"(> {config.MAX_DEAD_STRETCH_S}s)")
    else:
        detail = (f"longest dead stretch {_fmt(worst[2], 1) if worst else '0.0'}s in "
                  f"{_fmt(w0, 1)}-{_fmt(w1, 1)}s ({src})")
    return [_gate("G3", not bad, value, detail)]


# --------------------------------------------------------------------------- G4 story content
def gate_g4(ctx: Ctx) -> list[dict]:
    if ctx.rtype != "story":
        return _na("G4", ctx.rtype)
    soft = _gate("G4", False, {"needs_critic": True},
                 "needs critic: confirm needle/approval/reaction/camera visually", hard=False)
    if ctx.edl is None:
        return [_gate("G4", False, {}, "no EDL: story content tags unknown"), soft]
    counts = {k: 0 for k in ("needle", "approval", "reaction", "camera", "finished")}
    for _t0, _t1, seg in ctx.tl:
        for k in counts:
            if k in seg_tags(seg):
                counts[k] += 1
    last = ctx.tl[-1][2] if ctx.tl else {}
    last_dur = seg_dur(last) if last else 0.0
    last_finished = "finished" in seg_tags(last) if last else False
    reasons = []
    if counts["needle"] < config.MIN_NEEDLE_SHOTS:
        reasons.append(f"{counts['needle']} needle shots < {config.MIN_NEEDLE_SHOTS}")
    for k in ("approval", "reaction", "camera"):
        if counts[k] < 1:
            reasons.append(f"no seg tagged {k}")
    if not last_finished:
        reasons.append("last seg is not tagged finished")
    elif last_dur < config.FINISHED_HOLD_MIN_S - 1e-6:
        reasons.append(f"finished hold {_fmt(last_dur, 1)}s < {config.FINISHED_HOLD_MIN_S}s")
    value = {**counts, "finished_hold_s": round(last_dur, 3), "last_seg_finished": last_finished}
    detail = "; ".join(reasons) or (f"{counts['needle']} needle, approval, reaction, camera, "
                                    f"finished hold {_fmt(last_dur, 1)}s")
    return [_gate("G4", not reasons, value, detail), soft]


# --------------------------------------------------------------------------- G5 clean picture
def gate_g5(ctx: Ctx) -> list[dict]:
    if ctx.analysis is None:
        return [_gate("G5", False, {}, f"no picture analysis: {ctx.measure_error or ctx.probe_error}")]
    black = ctx.analysis["black"]
    dur = ctx.duration
    last_t0 = ctx.tl[-1][0] if ctx.tl else None
    bad_freezes = []
    for s, e in ctx.analysis["freezes"]:
        if last_t0 is not None:
            # with an EDL only the final hold may freeze (START_HERE G5): the freeze must start
            # inside the last seg, not two seconds before it and run into it
            allowed = s >= last_t0 - 0.1
        else:
            allowed = e >= dur - 0.25            # no EDL: a freeze that runs to the end is the hold
        if not allowed:
            bad_freezes.append((s, e))
    reasons = []
    if black:
        s, e = black[0]
        reasons.append(f"black frames at {_fmt(s)}-{_fmt(e)}s ({len(black)} range{'s' if len(black) > 1 else ''})")
    if bad_freezes:
        s, e = bad_freezes[0]
        reasons.append(f"frozen picture {_fmt(s, 1)}-{_fmt(e, 1)}s (>= {config.FREEZE_MAX_S}s, not the final hold)")
    value = {"black": [[round(a, 3), round(b, 3)] for a, b in black],
             "freezes": [[round(a, 3), round(b, 3)] for a, b in ctx.analysis["freezes"]],
             "active_rows": list(ctx.frames.active) if ctx.frames else None}
    return [_gate("G5", not reasons, value, "; ".join(reasons) or "no black frames, no freeze outside the final hold")]


# --------------------------------------------------------------------------- G6 voices
def gate_g6(ctx: Ctx) -> list[dict]:
    if ctx.rtype == "broll":
        return _na("G6", ctx.rtype)
    if ctx.edl is None:
        return [_gate("G6", False, {}, "no EDL: out-points unknown")]
    cd = cache_dir()
    checks: list[dict] = []
    fails: list[str] = []
    for i, (t0, t1, seg) in enumerate(ctx.tl):
        if seg_kind(seg) != "I":
            continue
        s_in, s_out = seg_in(seg), seg_out(seg)
        entry: dict[str, Any] = {"seg": i, "out": round(s_out, 3)}
        # (a) transcript: the last word that starts before the out-point must end before it
        ws = ctx.words_for_seg(seg) or []
        if ws:
            before = [w for w in ws if w.start < s_out]
            if before:
                last = max(before, key=lambda w: w.start)
                entry["last_word"] = last.text
                entry["last_word_end"] = round(last.end, 3)
                if last.end > s_out - WORD_END_MARGIN_S + 1e-9:
                    fails.append(f"seg {i}: '{last.text}' ends at {_fmt(last.end)} after out {_fmt(s_out)}")
        # (b) voice-band energy at the out-point
        length = s_out - s_in
        if length < 0.2:
            checks.append(entry)
            continue
        src = resolve_clip(seg.get("clip"), ctx.edl_path)
        if src is not None:
            a, b = s_in, s_out
            entry["audio_source"] = "clip"
        else:
            a, b = t0, t1
            entry["audio_source"] = "reel"
            src = ctx.reel
        try:
            tail_rms, speech_rms = voice_tail_rms(src, a, b, cd / f"{ctx.reel.stem}.{ctx.key}.a{i}.f32",
                                                 cache=ctx.mcache)
        except GatesError as e:
            entry["error"] = str(e)
            fails.append(f"seg {i}: {e}")
            checks.append(entry)
            continue
        entry.update({"tail_rms": round(tail_rms, 5), "speech_rms": round(speech_rms, 5)})
        if tail_rms > TAIL_RATIO_MAX * speech_rms and tail_rms > TAIL_RMS_FLOOR:
            fails.append(f"seg {i}: voice still sounding at out {_fmt(s_out)} "
                         f"(tail {tail_rms:.3f} vs speech {speech_rms:.3f})")
        checks.append(entry)
    n_i = len(checks)
    value = {"i_segs": n_i, "checked": checks, "fails": len(fails)}
    if fails:
        detail = fails[0] + (f" (+{len(fails) - 1} more)" if len(fails) > 1 else "")
    else:
        detail = f"all {n_i} dialogue out-points land in a pause" if n_i else "no I segs to check"
    return [_gate("G6", not fails, value, detail)]


# --------------------------------------------------------------------------- G7 order and sound
def gate_g7(ctx: Ctx) -> list[dict]:
    if ctx.edl is None:
        return [_gate("G7", False, {}, "no EDL: order keys unknown")]
    segs = [seg for _t0, _t1, seg in ctx.tl]
    reasons = []
    missing = [i for i, s in enumerate(segs) if not str(s.get("order_key") or "").strip()]
    if missing:
        reasons.append(f"seg {missing[0]} has no order_key" + (f" (+{len(missing) - 1} more)" if len(missing) > 1 else ""))
    keys = [order_key_sortable(s.get("order_key")) for s in segs]
    for i in range(1, len(keys)):
        if keys[i] < keys[i - 1]:
            reasons.append(f"order_key goes backwards at seg {i}: {segs[i].get('order_key')!r} < "
                           f"{segs[i - 1].get('order_key')!r}")
            break
    kinds = [seg_kind(s) for s in segs]
    if "C" in kinds:
        first_c = kinds.index("C")
        if any(k != "C" for k in kinds[first_c:]):
            reasons.append(f"Canon (C) seg at {first_c} is followed by a non-C seg; reveals go last")
    af = ctx.edl.get("audio_from")
    if af not in (None, "", []):
        reasons.append(f"audio_from is set ({af!r}); audio must stay with its own clip")
    seg_af = [i for i, s in enumerate(segs) if s.get("audio_from") not in (None, "")]
    if seg_af:
        reasons.append(f"seg {seg_af[0]} has audio_from; audio must stay with its own clip")
    value = {"segs": len(segs), "kinds": "".join(k or "?" for k in kinds), "missing_order_keys": len(missing)}
    return [_gate("G7", not reasons, value, "; ".join(reasons) or f"{len(segs)} segs in order, C last, own audio")]


# --------------------------------------------------------------------------- G8 length
def gate_g8(ctx: Ctx) -> list[dict]:
    dur = ctx.duration
    if ctx.rtype == "story":
        if ctx.multi_day:
            lo, hi, rule = float(config.STORY_SINGLE_RANGE[0]), float(config.STORY_MULTI_MAX), "multi-day story"
        else:
            lo, hi = (float(x) for x in config.STORY_SINGLE_RANGE)
            rule = "single-session story"
    elif ctx.rtype == "th":
        lo, hi = (float(x) for x in config.TH_RANGE)
        rule = "talking head"
    else:
        lo, hi, rule = float(config.BROLL_MIN), float("inf"), "b-roll"
    ok = lo - 1e-6 <= dur <= hi + 1e-6
    rng = f">= {lo:g}s" if hi == float("inf") else f"{lo:g}-{hi:g}s"
    value = {"duration_s": round(dur, 3), "min_s": lo, "max_s": None if hi == float("inf") else hi,
             "multi_day": ctx.multi_day}
    detail = f"{_fmt(dur, 1)}s {'within' if ok else 'outside'} {rng} ({rule})"
    return [_gate("G8", ok, value, detail)]


# --------------------------------------------------------------------------- G9 spec
def gate_g9(ctx: Ctx) -> list[dict]:
    if ctx.info is None:
        return [_gate("G9", False, {}, f"ffprobe failed: {ctx.probe_error}")]
    info = ctx.info
    reasons = []
    allowed_codecs = ("hevc", "h264") if ctx.rtype == "broll" else ("hevc",)
    if info["codec"] not in allowed_codecs:
        reasons.append(f"codec {info['codec'] or '?'} not in {list(allowed_codecs)}")
    if (info["width"], info["height"]) != (config.CANVAS_W, config.CANVAS_H):
        reasons.append(f"{info['width']}x{info['height']} != {config.CANVAS_W}x{config.CANVAS_H}")
    trc = info["color_transfer"]
    if trc not in ALLOWED_TRANSFERS:
        reasons.append(f"color_transfer {trc or 'untagged'} not in {list(ALLOWED_TRANSFERS)}")
    # edl.py writes export.transfer as hlg|sdr (auto = not decided yet); map it to the ffprobe tag
    want_word = str(((ctx.edl or {}).get("export") or {}).get("transfer") or "").strip().lower() if ctx.edl else ""
    want_trc = TRANSFER_WORDS.get(want_word, want_word)
    if want_word and want_word != "auto" and trc != want_trc:
        reasons.append(f"color_transfer {trc or 'untagged'} != EDL export.transfer {want_word} ({want_trc})")
    if not info["has_audio"]:
        reasons.append("no audio stream")
    chat = ctx.chat
    chat_size = None
    chat_ok = False
    if chat is None or not chat.is_file():
        reasons.append(f"chat copy missing: {chat.name if chat else '?'}")
    else:
        chat_size = chat.stat().st_size
        if chat_size > config.CHAT_COPY_MAX_BYTES:
            reasons.append(f"chat copy {chat_size / 1048576:.1f} MB > {config.CHAT_COPY_MAX_BYTES // 1048576} MB")
        res = safeio.run_ffmpeg_analysis(["-v", "error", "-i", str(chat)])
        err = res.stderr.strip()
        chat_ok = res.returncode == 0 and not err
        if not chat_ok:
            reasons.append(f"chat copy does not decode cleanly: {err[-120:] or 'ffmpeg exit ' + str(res.returncode)}")
    value = {"codec": info["codec"], "width": info["width"], "height": info["height"],
             "color_transfer": trc, "pix_fmt": info["pix_fmt"], "has_audio": info["has_audio"],
             "chat": str(chat) if chat else None, "chat_bytes": chat_size, "chat_decodes": chat_ok}
    detail = "; ".join(reasons) or (f"{info['codec']} {info['width']}x{info['height']} {trc}, audio, "
                                    f"chat {chat_size / 1048576:.1f} MB decodes")
    return [_gate("G9", not reasons, value, detail)]


# --------------------------------------------------------------------------- G10 hooks and caption
def _pieces_rows() -> list[dict]:
    return safeio.read_csv(config.LEDGERS / "pieces.csv")


def gate_g10(ctx: Ctx) -> list[dict]:
    if ctx.edl is None:
        return [_gate("G10", False, {}, "no EDL: hooks and caption unknown")]
    try:
        from . import hook_gate  # noqa: WPS433 (another builder's module)
    except ImportError:
        return [_gate("G10", False, {}, "hook_gate unavailable")]
    hooks = [str(h).strip() for h in (ctx.edl.get("hooks") or []) if str(h).strip()]
    caption = ctx.edl.get("caption")
    reasons = []
    pair = None
    if len(hooks) != 2:
        reasons.append(f"{len(hooks)} hooks; every reel needs exactly 2 hooks given separately")
    else:
        burned = hook_gate.burned_hooks()
        captions = hook_gate.used_captions()
        rows = _pieces_rows()
        pieces = [str(p) for p in (ctx.edl.get("pieces") or [])] or None
        # always a dict ({} when pieces.csv is empty): hook_gate then FAILS an hours claim it cannot
        # verify instead of passing it with a note (never invent hours, START_HERE 3.6)
        pieces_hours = hook_gate.pieces_hours_from_rows(rows, pieces)
        pair = hook_gate.check_pair(hooks[0], hooks[1], burned, captions, sorted(ctx.first_tags()), pieces_hours)
        if not pair["ok"]:
            reasons.extend(pair["reasons"])
    tail_ok = bool(hook_gate.caption_tail_ok(caption or ""))
    if not tail_ok:
        reasons.append("caption tail is not the exact booking line")
    value = {"hooks": hooks[:2], "hook_gate": pair, "caption_tail_ok": tail_ok,
             "caption_chars": len(caption or "")}
    return [_gate("G10", not reasons, value, "; ".join(reasons) or "both hooks pass the hook gate, caption tail exact")]


# --------------------------------------------------------------------------- G11 uniqueness
def _hex_hashes(obj, out: list[tuple[str, str]], ident: str = "") -> None:
    """Collect (id, hex) frame-1 hashes from a signatures file of unknown exact shape (the
    fallback when the file is not a tools.fingerprint store). An entry that names a ``kind``
    other than posted is skipped with its subtree; a child dict without an id of its own
    inherits the parent key (``{"DebCc": {"sig": {"frame1": ...}}}`` -> id DebCc, not sig)."""
    keys = ("frame1", "frame1_phash", "phash_frame1", "phash", "frame_1")
    if isinstance(obj, dict):
        kind = obj.get("kind")
        if isinstance(kind, str) and kind.strip().lower() != "posted":
            return
        own = obj.get("id") or obj.get("shortcode") or obj.get("reel") or obj.get("key")
        my_id = str(own) if own else ident
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and HEX_RE.fullmatch(v):
                out.append((my_id, v))
                break
            if isinstance(v, dict):
                for kk in ("frame1", "hash", "phash"):
                    vv = v.get(kk)
                    if isinstance(vv, str) and HEX_RE.fullmatch(vv):
                        out.append((my_id, vv))
                        break
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                child = my_id if (own or str(k) in STRUCTURAL_KEYS) else str(k)
                _hex_hashes(v, out, child)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _hex_hashes(v, out, f"{ident}[{i}]" if ident else f"[{i}]")


def phash_distance(a_hex: str, b_hex: str) -> int | None:
    """Hamming distance between two hex hashes of the same length (pure Python, like
    tools.fingerprint.hamming); None when the two are not comparable."""
    try:
        a, b = str(a_hex).strip().lower(), str(b_hex).strip().lower()
        if not a or len(a) != len(b):
            return None
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return None


def _fingerprint_module():
    """tools.fingerprint (lazy; needs pillow + numpy) or None when it cannot be imported."""
    try:
        from . import fingerprint  # noqa: WPS433 (another builder's module)
    except ImportError:
        return None
    return fingerprint


def posted_signatures(path: Path | str, fp=None) -> list[tuple[str, list[str], str]]:
    """[(key, frame-1 hex hashes (every crop scale), video)] of the kind=posted entries of a
    signatures file. A tools.fingerprint store is read through fingerprint.load_store /
    entries_of_kind; any other shape falls back to the tolerant _hex_hashes walk. Deliveries
    (kind delivery) never count: a reel re-gated after deliver.py filed it must not match itself."""
    p = Path(path)
    if not p.is_file():
        return []
    store = None
    if fp is not None:
        try:
            store = fp.load_store(p)
        except (ValueError, OSError):
            store = None
    out: list[tuple[str, list[str], str]] = []
    if store is not None:
        entries = (fp.entries_of_kind(store, ("posted",)) if hasattr(fp, "entries_of_kind") else
                   {k: v for k, v in store.get("signatures", {}).items()
                    if isinstance(v, dict) and v.get("kind") == "posted"})
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            sig = entry.get("sig") if isinstance(entry.get("sig"), dict) else entry
            hashes: list[str] = []
            f1h = sig.get("frame1_hashes")
            if isinstance(f1h, dict):
                hashes += [str(v) for v in f1h.values() if isinstance(v, str)]
            if isinstance(sig.get("frame1"), str):
                hashes.append(sig["frame1"])
            hashes = list(dict.fromkeys(h.lower() for h in hashes if HEX_RE.fullmatch(h)))
            if hashes:
                out.append((str(key), hashes, str(entry.get("video") or "")))
        return out
    pairs: list[tuple[str, str]] = []
    _hex_hashes(safeio.read_json(p, default=None), pairs)
    return [(k, [h.lower()], "") for k, h in pairs]


def _same_file(a: Path | str | None, b: Path) -> bool:
    if not a:
        return False
    pa = Path(str(a))
    try:
        if pa.is_file() and pa.resolve() == b.resolve():
            return True
    except OSError:
        pass
    return pa.name == b.name


def delivery_edl_path(row: dict) -> Path | None:
    f = (row.get("file") or "").strip()
    if not f:
        return None
    p = Path(f)
    cands = [p]
    if not p.is_absolute():
        cands.append(config.ENGINE_ROOT / p)
        cands.append(config.POST / p)
    for c in cands:
        e = find_edl(c)
        if e is not None:
            return e
    return None


def shot_overlap(edl: dict, others: Iterable[dict]) -> tuple[float, float]:
    """(overlapping seconds, total seconds) of this EDL's shots against other EDLs: same clip
    file name and overlapping [in, out]."""
    by_name: dict[str, list[tuple[float, float]]] = {}
    for o in others:
        for seg in o.get("segs") or []:
            if isinstance(seg, dict) and seg.get("clip"):
                by_name.setdefault(Path(str(seg["clip"])).name, []).append((seg_in(seg), seg_out(seg)))
    total = 0.0
    overlap = 0.0
    for seg in edl.get("segs") or []:
        if not isinstance(seg, dict):
            continue
        d = seg_dur(seg)
        total += d
        a, b = seg_in(seg), seg_out(seg)
        covered = 0.0
        for c, e in by_name.get(Path(str(seg.get("clip") or "")).name, []):
            covered += max(0.0, min(b, e) - max(a, c))
        overlap += min(d, covered)
    return overlap, total


def _session_rest_ok(session: str, today: date) -> bool:
    try:
        from . import ledgers  # noqa: WPS433
        return bool(ledgers.sessions_rest_ok(session, today))
    except ImportError:
        pass
    for r in safeio.read_csv(config.LEDGERS / "sessions.csv"):
        if (r.get("session") or "").strip().lower() == session.strip().lower():
            v = (r.get("rest_until") or "").strip()
            if not v:
                return True
            try:
                return today >= date.fromisoformat(v[:10])
            except ValueError:
                return True
    return True


def _row_date(row: dict) -> date | None:
    try:
        return date.fromisoformat((row.get("date") or "").strip()[:10])
    except ValueError:
        return None


def gate_g11(ctx: Ctx) -> list[dict]:
    reasons: list[str] = []
    notes: list[str] = []
    value: dict[str, Any] = {}

    # (a) frame-1 phash against posted signatures: one hashing method on both sides
    # (tools.fingerprint.frame1_phash, the same function deliver.py and the store use). When
    # fingerprint cannot run, nothing is compared and a hard=false entry says so (soft warn).
    soft: dict | None = None
    sig_path = ctx.signatures or (config.DATA / "signatures.json")
    fp = _fingerprint_module()
    sigs = posted_signatures(sig_path, fp)
    value["signatures"] = len(sigs)
    if fp is None:
        soft = _gate("G11", False, {"needs_critic": True, "phash": "not compared"},
                     f"needs critic: frame-1 phash not compared (tools.fingerprint unavailable: {FINGERPRINT_HINT})",
                     hard=False)
    elif not sigs:
        notes.append("no posted signatures to compare" if not sig_path.is_file()
                     else "signatures file holds no posted frame-1 hashes")
    else:
        try:
            mine = str(fp.frame1_phash(ctx.reel) or "")
        except Exception as e:  # ffmpeg/PIL failure on the master: say so, never guess
            mine = ""
            soft = _gate("G11", False, {"needs_critic": True, "phash": "not compared"},
                         f"needs critic: frame-1 phash not compared ({type(e).__name__}: {e})", hard=False)
        if mine:
            value["frame1_phash"] = mine
            best: tuple[int, str] | None = None
            skipped_self = 0
            for key, hashes, video in sigs:
                if _same_file(video, ctx.reel):
                    skipped_self += 1
                    continue
                ds = [d for h in hashes if (d := phash_distance(mine, h)) is not None]
                if ds and (best is None or min(ds) < best[0]):
                    best = (min(ds), key)
            if skipped_self:
                value["signatures_of_this_reel_skipped"] = skipped_self
            if best is not None:
                dmin, who = best
                value["phash_min_distance"] = dmin
                value["phash_nearest"] = who
                if dmin <= config.PHASH_MATCH_MAX:
                    reasons.append(f"frame 1 matches posted reel {who} (phash distance {dmin} <= {config.PHASH_MATCH_MAX})")
            elif len(sigs) > skipped_self:
                soft = _gate("G11", False, {"needs_critic": True, "phash": "not compared"},
                             "needs critic: frame-1 phash not compared (posted signatures are not 64-bit hex hashes)",
                             hard=False)

    # (b) shot reuse vs the last SHOT_REUSE_WINDOW_DAYS of posted deliveries (story/th)
    rows = safeio.read_csv(config.LEDGERS / "deliveries.csv")
    if ctx.edl is not None and ctx.rtype in ("story", "th"):
        since = ctx.today - timedelta(days=config.SHOT_REUSE_WINDOW_DAYS)
        others: list[dict] = []
        unreadable = 0
        for r in rows:
            if (r.get("status") or "").strip().lower() != "posted":
                continue
            d = _row_date(r)
            if d is None or d < since or d > ctx.today:
                continue
            if Path((r.get("file") or "").strip()).name == ctx.reel.name:
                continue
            e = delivery_edl_path(r)
            if e is None:
                unreadable += 1
                continue
            try:
                others.append(load_edl(e))
            except GatesError:
                unreadable += 1
        overlap, total = shot_overlap(ctx.edl, others)
        frac = overlap / total if total > 0 else 0.0
        value.update({"reuse_fraction": round(frac, 3), "reuse_seconds": round(overlap, 2),
                      "posted_edls": len(others), "posted_without_edl": unreadable})
        if frac > config.SHOT_REUSE_MAX_FRACTION + 1e-9:
            reasons.append(f"{frac:.0%} of shots were posted in the last {config.SHOT_REUSE_WINDOW_DAYS} days "
                           f"(> {config.SHOT_REUSE_MAX_FRACTION:.0%})")
    elif ctx.edl is None:
        reasons.append("no EDL: shot reuse, session and pieces unknown")

    # (c) session rest
    if ctx.edl is not None:
        session = str(ctx.edl.get("session") or "").strip()
        if session:
            rest_ok = _session_rest_ok(session, ctx.today)
            value["session"] = session
            value["session_rest_ok"] = rest_ok
            if not rest_ok:
                reasons.append(f"session {session!r} is resting on {ctx.today.isoformat()}")

    # (d) pieces per day
    if ctx.edl is not None:
        pieces = [str(p).strip() for p in (ctx.edl.get("pieces") or []) if str(p).strip()]
        counts = {p: 1 for p in pieces}
        for r in rows:
            if _row_date(r) != ctx.today:
                continue
            if (r.get("status") or "").strip().lower() in DELIVERY_NOT_COUNTED:
                continue
            if Path((r.get("file") or "").strip()).name == ctx.reel.name:
                continue
            e = delivery_edl_path(r)
            if e is None:
                continue
            try:
                theirs = {str(p).strip().lower() for p in (load_edl(e).get("pieces") or [])}
            except GatesError:
                continue
            for p in pieces:
                if p.lower() in theirs:
                    counts[p] += 1
        value["pieces_today"] = counts
        over = [p for p, n in counts.items() if n > config.MAX_REELS_PER_PIECE_PER_DAY]
        if over:
            reasons.append(f"piece {over[0]!r} would be in {counts[over[0]]} reels today "
                           f"(> {config.MAX_REELS_PER_PIECE_PER_DAY})")

    detail = "; ".join(reasons) if reasons else "frame 1 unique, shots fresh, session rested, pieces within the daily cap"
    if notes:
        detail += " (" + "; ".join(notes) + ")"
    out = [_gate("G11", not reasons, value, detail)]
    if soft is not None:
        out.append(soft)
    return out


# --------------------------------------------------------------------------- G12 pieces and visual
def gate_g12(ctx: Ctx) -> list[dict]:
    soft = _gate("G12", False, {"needs_critic": True},
                 "needs critic: no TV/screen, no colour piece, no static opener", hard=False)
    if ctx.edl is None:
        return [_gate("G12", False, {}, "no EDL: pieces unknown; identify piece by frames"), soft]
    pieces = [str(p).strip() for p in (ctx.edl.get("pieces") or []) if str(p).strip()]
    rows = {(r.get("piece") or "").strip().lower(): r for r in _pieces_rows() if (r.get("piece") or "").strip()}
    unknown, colour, blank = [], [], []
    for p in pieces:
        r = rows.get(p.lower())
        if r is None:
            unknown.append(p)
            continue
        c = (r.get("color") or "").strip().lower()
        if c == "no":
            continue
        (blank if not c else colour).append(p)
    reasons = []
    if not pieces and ctx.rtype in ("story", "broll"):
        reasons.append("no pieces listed; identify piece by frames")
    if unknown:
        reasons.append(f"piece {unknown[0]!r} not in pieces.csv; identify piece by frames")
    if blank:
        reasons.append(f"piece {blank[0]!r} has no color value in pieces.csv; identify piece by frames")
    if colour:
        reasons.append(f"piece {colour[0]!r} is a colour piece (color={rows[colour[0].lower()].get('color')!r})")
    value = {"pieces": pieces, "unknown": unknown, "colour": colour, "color_blank": blank}
    detail = "; ".join(reasons) or (f"{len(pieces)} piece(s) in pieces.csv with color=no" if pieces
                                    else "no pieces shown")
    return [_gate("G12", not reasons, value, detail), soft]


GATE_FUNCS = (gate_g1, gate_g2, gate_g3, gate_g4, gate_g5, gate_g6, gate_g7, gate_g8, gate_g9,
              gate_g10, gate_g11, gate_g12)


# --------------------------------------------------------------------------- run
def _as_date(d: date | datetime | str | None, fallback: date) -> date:
    if d is None:
        return fallback
    if isinstance(d, datetime):
        return d.astimezone(config.TZ).date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d).strip()[:10])


def _load_transcript(transcript) -> Any:
    if transcript is None:
        return None
    if hasattr(transcript, "words"):
        return transcript
    return JsonTranscript.load(Path(str(transcript)))


def report_path(reel: Path | str) -> Path:
    r = Path(reel)
    return r.with_name(r.stem + ".gates.json")


def run_gates(reel: Path | str, rtype: str, *, edl: Path | str | None = None, chat: Path | str | None = None,
              transcript=None, signatures: Path | str | None = None, today: date | str | None = None,
              template: Path | str | None = None, calibration: Path | str | None = None,
              multi_day: bool | None = None, now: datetime | None = None, proxy: Path | str | None = None,
              write: bool = True) -> dict:
    """Run G1-G12 on a reel and (by default) write <stem>.gates.json next to it.

    Returns the report dict (schema in SPEC.md). ``transcript`` is a words.json path or any
    object with ``words(clip, start, end, vad)``. ``now`` and ``today`` make the run deterministic.
    """
    if rtype not in TYPES:
        raise ValueError(f"type must be one of {TYPES}, got {rtype!r}")
    reel_p = Path(reel)
    now = now or datetime.now(config.TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.TZ)
    ctx = Ctx(reel=reel_p, rtype=rtype, today=_as_date(today, now.astimezone(config.TZ).date()), now=now)
    ctx.thresholds = load_thresholds(calibration)
    ctx.signatures = Path(signatures) if signatures else None
    ctx.template = Path(template) if template else None
    ctx.chat = Path(chat) if chat else find_chat(reel_p)

    ctx.edl_path = Path(edl) if edl else find_edl(reel_p)
    if ctx.edl_path is not None:
        try:
            ctx.edl = load_edl(ctx.edl_path)
        except GatesError as e:
            ctx.edl_error = str(e)
            ctx.edl = None
    ctx.tl = timeline(ctx.edl)
    if multi_day is None:
        multi_day = bool((ctx.edl or {}).get("multi_day", False))
    ctx.multi_day = bool(multi_day)
    try:
        ctx.transcript = _load_transcript(transcript)
    except GatesError as e:
        ctx.transcript = None
        ctx.edl_error = (ctx.edl_error + "; " if ctx.edl_error else "") + str(e)

    try:
        ctx.info = probe(reel_p)
    except GatesError as e:
        ctx.probe_error = str(e)

    if ctx.info is not None:
        try:
            ctx.key = cache_key(reel_p)
            if proxy and Path(proxy).is_file():
                ctx.proxy = Path(proxy)
            else:
                ctx.proxy = build_proxy(reel_p, ctx.info)
            ctx.mcache = MeasureCache(measure_cache_path(ctx.proxy))
            ctx.frames = measure_frames(ctx.proxy, ctx.info["fps"], ctx.key, ctx.mcache)
            need_scenes = ctx.edl is None
            ctx.analysis = picture_analysis(ctx.proxy, ctx.frames.active, ctx.duration, scenes=need_scenes,
                                            cache=ctx.mcache)
        except (GatesError, ImportError, OSError) as e:
            ctx.measure_error = str(e)

    gates: list[dict] = []
    for fn, (gid, _name) in zip(GATE_FUNCS, GATES):
        try:
            gates.extend(fn(ctx))
        except Exception as e:  # a gate that crashes must fail closed, never pass by omission
            gates.append(_gate(gid, False, {"error": type(e).__name__}, f"gate error: {type(e).__name__}: {e}"))
    passed = all(g["ok"] for g in gates if g["hard"])
    if ctx.mcache is not None:
        try:
            ctx.mcache.save()
        except safeio.SafeIOError:
            pass
    report = {
        "reel": str(reel_p), "type": rtype, "edl": str(ctx.edl_path) if ctx.edl_path else None,
        "chat": str(ctx.chat) if ctx.chat else None, "pass": passed,
        "generated_et": now.astimezone(config.TZ).isoformat(timespec="seconds"),
        "today": ctx.today.isoformat(), "duration_s": round(ctx.duration, 3), "multi_day": ctx.multi_day,
        "proxy": str(ctx.proxy) if ctx.proxy else None, "thresholds": ctx.thresholds,
        "notes": [n for n in (ctx.probe_error, ctx.edl_error, ctx.measure_error) if n],
        "gates": gates,
    }
    if write:
        target = report_path(reel_p)
        try:
            safeio.write_json(target, report, overwrite=True)
        except safeio.SafeIOError:
            target = cache_dir() / target.name
            safeio.write_json(target, report, overwrite=True)
        report["report"] = str(target)
    return report


def format_table(report: dict) -> str:
    lines = [f"{report['reel']}  type={report['type']}  {'PASS' if report['pass'] else 'FAIL'}"]
    for g in report["gates"]:
        if g["hard"]:
            status = "PASS" if g["ok"] else "FAIL"
        else:
            status = "ok" if g["ok"] else "CRITIC"
        lines.append(f"  {g['id']:<4}{g['name']:<16}{status:<7}{g['detail']}")
    if report.get("report"):
        lines.append(f"  report: {report['report']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- calibrate
def calibrate(videos: Sequence[Path | str], *, out: Path | str | None = None, now: datetime | None = None,
              write: bool = True) -> dict:
    """Measure his winners through the same proxy chain and write data/calibration.json:
    {motion_threshold: 0.6 x min(winner motions), luma_std_min: 60, samples: [...]}."""
    if not videos:
        raise ValueError("calibrate needs at least one video")
    now = now or datetime.now(config.TZ)
    samples = []
    for v in videos:
        p = Path(v)
        info = probe(p)
        prx = build_proxy(p, info)
        mc = MeasureCache(measure_cache_path(prx))
        fs = measure_frames(prx, info["fps"], cache_key(p), mc)
        mc.save()
        samples.append({"file": str(p), "luma_std": round(fs.luma_std, 2),
                        "motion": round(fs.motion_over(MOTION_SPAN_S), 3), "is_hlg": is_hlg(info),
                        "active_rows": list(fs.active)})
    motions = [s["motion"] for s in samples]
    result = {
        "motion_threshold": round(CALIBRATION_FACTOR * min(motions), 3),
        "luma_std_min": float(config.FRAME1_LUMA_STD_MIN),
        "samples": samples,
        "generated_et": now.astimezone(config.TZ).isoformat(timespec="seconds"),
        "proxy_chain": {"hlg": proxy_chain(True), "sdr": proxy_chain(False)},
        "note": f"motion_threshold = {CALIBRATION_FACTOR} x the least-moving winner over 0-1 s on the 360x640 proxy",
    }
    if write:
        target = Path(out) if out else config.DATA / "calibration.json"
        safeio.write_json(target, result, overwrite=True)
        result["path"] = str(target)
    return result


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="py -m tools.gates",
        description="Quality gate G1-G12 for one reel; writes <stem>.gates.json next to it and prints "
                    "a PASS/FAIL table. Subcommand: 'py -m tools.gates calibrate <winner videos...>' "
                    "writes data/calibration.json (frame-1 motion threshold from his winners).")
    p.add_argument("reel", type=Path, help="the rendered reel (.mp4)")
    p.add_argument("--type", required=True, choices=TYPES, dest="rtype", help="story | broll | th")
    p.add_argument("--edl", type=Path, help="EDL json (default: <stem>.edl.json next to the reel)")
    p.add_argument("--chat", type=Path, help="chat copy (default: <stem>_chat.mp4 next to the reel)")
    p.add_argument("--transcript", type=Path, help="words.json {clip_name: [[start, end, text], ...]}")
    p.add_argument("--signatures", type=Path, help="posted frame-1 signatures (default data/signatures.json)")
    p.add_argument("--today", help="YYYY-MM-DD for the ledger checks (default: today in ET)")
    p.add_argument("--template", type=Path, help="b-roll template (default templates/broll_station_first.json)")
    p.add_argument("--calibration", type=Path, help="data/calibration.json with motion_threshold")
    p.add_argument("--multi-day", action="store_true", help="story length rule for multi-day sessions")
    p.add_argument("--proxy", type=Path, help="reuse an existing 360x640 proxy instead of building one")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    return p


def _build_calibrate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="py -m tools.gates calibrate",
        description="Measure winner reels through the gate proxy chain and write data/calibration.json.")
    p.add_argument("videos", nargs="+", type=Path, help="his winner reels (downloaded mp4s)")
    p.add_argument("--out", type=Path, help="output json (default data/calibration.json)")
    p.add_argument("--json", action="store_true", help="print the calibration as JSON")
    return p


def _main_calibrate(argv: Sequence[str]) -> int:
    args = _build_calibrate_parser().parse_args(argv)
    missing = [str(v) for v in args.videos if not Path(v).is_file()]
    if missing:
        sys.stderr.write("missing: " + ", ".join(missing) + "\n")
        return 1
    try:
        result = calibrate(args.videos, out=args.out)
    except (GatesError, ImportError, safeio.SafeIOError) as e:
        sys.stderr.write(f"calibrate failed: {e}\n")
        return 1
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for s in result["samples"]:
            print(f"{Path(s['file']).name}: luma std {s['luma_std']}, motion {s['motion']}")
        print(f"motion_threshold {result['motion_threshold']}, luma_std_min {result['luma_std_min']} -> {result.get('path')}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # gate details quote hook text and captions (emoji included); a cp1252 console must not
    # raise UnicodeEncodeError after the report was already written
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "calibrate":
        return _main_calibrate(argv[1:])
    args = _build_parser().parse_args(argv)
    if not args.reel.is_file():
        sys.stderr.write(f"reel not found: {args.reel}\n")
        return 1
    try:
        today = date.fromisoformat(args.today) if args.today else None
    except ValueError:
        sys.stderr.write(f"--today must be YYYY-MM-DD, got {args.today!r}\n")
        return 1
    try:
        report = run_gates(args.reel, args.rtype, edl=args.edl, chat=args.chat, transcript=args.transcript,
                           signatures=args.signatures, today=today, template=args.template,
                           calibration=args.calibration, multi_day=True if args.multi_day else None,
                           proxy=args.proxy)
    except (GatesError, safeio.SafeIOError, ValueError) as e:
        sys.stderr.write(f"gates failed: {e}\n")
        return 1
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_table(report))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
