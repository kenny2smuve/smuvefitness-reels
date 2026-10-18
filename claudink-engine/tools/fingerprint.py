"""Perceptual fingerprints of reels (WEAK_POINTS W16, START_HERE 6D G11 and section 8).

Why: once Claudin zooms into a delivered letterbox, adds his hook text and a trending sound,
Claude could not tell its own cuts from his, so most posted reels were logged UNSURE and the
learning loop on Claude's edits stalled. This module makes the attribution mechanical and
also powers the G11 "frame-1 was already posted" check.

How (the tt_dedupe method):
- 9 frames per reel at relative times (i + 0.5) / 9 of the duration, so the same positions
  line up whatever the pixel size or container;
- each frame is scaled to width 360 keeping its aspect and padded to 360x640 on black, so a
  raw landscape clip, a letterboxed 1440x2560 render and a full-bleed 1080x1920 upload all
  land in the same place of the same canvas;
- for the crop scales 1.0, 1.15 and 1.33 the centre of the frame is cropped and resized back
  (he zooms into the letterbox), then the top 18% and bottom 22% are painted mid-grey (he adds
  hook text there, Instagram draws its UI there) and a 64-bit pHash is taken;
- the distance between two reels is, per frame, the smallest Hamming distance over every pair
  of crop scales; the median over the 9 frames decides: <= config.PHASH_MATCH_MAX (12) is a
  match (CLAUDE), >= PHASH_CLAUDIN_MIN (20) is clearly not (CLAUDIN), between is UNSURE;
- frame 1 (t = 0) is hashed separately for G11.

pHash is imagehash.phash(hash_size=8) when imagehash is installed, otherwise a pure numpy
implementation of the same algorithm (grey, 32x32 Lanczos, DCT-II, top-left 8x8, median
threshold) that produces the same bits.

Signatures live in data/signatures.json (keys = delivery ids or posted shortcodes, each with
kind "delivery" or "posted"). Library functions return data and never print.

Run:  py -m tools.fingerprint sign <video> --key K --kind delivery|posted [--json]
      py -m tools.fingerprint match <video> [--kinds posted,delivery] [--json]
      py -m tools.fingerprint frame1 <video> [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from . import config
from . import safeio

# --------------------------------------------------------------------------- constants
FRAME_W, FRAME_H = 360, 640
PHASH_CLAUDIN_MIN = 20          # median distance at or above this: clearly not a Claude cut
HASH_SIZE = 8
HIGHFREQ_FACTOR = 4
GREY = (128, 128, 128)
KINDS = ("delivery", "posted")
STORE_VERSION = 1


def _scale_key(s: float) -> str:
    """'1.0', '1.15', '1.33' (the dict keys of a frame's hashes)."""
    txt = f"{float(s):.2f}".rstrip("0")
    return txt + "0" if txt.endswith(".") else txt


SCALE_KEYS = tuple(_scale_key(s) for s in config.PHASH_SCALES)


# --------------------------------------------------------------------------- frames
def frame_times(duration: float, n: int = config.PHASH_FRAMES) -> list[float]:
    """n sample times at relative positions (i + 0.5) / n of the duration."""
    if duration <= 0 or n <= 0:
        return []
    return [round(duration * (i + 0.5) / n, 3) for i in range(n)]


def probe_duration(video: Path | str) -> float:
    """Container duration in seconds via ffprobe (0.0 when unknown)."""
    r = safeio.run_ffprobe(["-v", "error", "-show_entries", "format=duration",
                            "-of", "json", str(video)])
    try:
        return float(json.loads(r.stdout or "{}").get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        return 0.0


def _file_tag(video: Path) -> str:
    try:
        st = video.stat()
        raw = f"{video.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        raw = str(video)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def frame_filter(size: tuple[int, int] = (FRAME_W, FRAME_H)) -> str:
    """Scale to fit the canvas keeping aspect (width 360 for anything 9:16 or wider), then pad
    to the full canvas centred on black. A letterboxed render and a raw clip line up."""
    w, h = size
    return (f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24")


def extract_frame(video: Path | str, t: float, size: tuple[int, int] = (FRAME_W, FRAME_H),
                  cache_dir: Path | None = None):
    """One frame at time t as a PIL.Image (RGB, exactly `size`), via one ffmpeg call.

    -ss sits before -i (keyframe seek then exact), -frames:v 1. The PNG lands in
    cache/fingerprint/ (overwrite is allowed there) and is loaded back.
    """
    from PIL import Image

    video = Path(video)
    cache_dir = cache_dir or (config.CACHE / "fingerprint")
    out = cache_dir / f"{_file_tag(video)}_{int(round(t * 1000)):08d}.png"
    args = ["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", frame_filter(size), "-f", "image2", "-update", "1"]
    r = safeio.run_ffmpeg(args, out, overwrite=True, timeout=120)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg could not extract a frame at {t:.3f}s from {video}: "
                           f"{(r.stderr or '').strip()[-400:]}")
    with Image.open(out) as im:
        img = im.convert("RGB").copy()
    if img.size != tuple(size):
        img = img.resize(size, Image.LANCZOS)
    return img


# --------------------------------------------------------------------------- image ops
def mask_text_bands(img, top: float = config.TEXT_BAND_TOP, bottom: float = config.TEXT_BAND_BOTTOM):
    """Paint the top and bottom bands mid-grey: his hook text, captions and the IG UI live there."""
    from PIL import ImageDraw

    out = img.copy()
    w, h = out.size
    d = ImageDraw.Draw(out)
    top_px = int(round(h * top))
    bot_px = int(round(h * bottom))
    if top_px > 0:
        d.rectangle([0, 0, w, top_px - 1], fill=GREY)
    if bot_px > 0:
        d.rectangle([0, h - bot_px, w, h], fill=GREY)
    return out


def center_crop(img, scale: float):
    """Crop the central 1/scale of the frame and resize back to the frame size (a zoom)."""
    from PIL import Image

    if scale <= 1.0:
        return img.copy()
    w, h = img.size
    cw, ch = max(1, int(round(w / scale))), max(1, int(round(h / scale)))
    left, top = (w - cw) // 2, (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch)).resize((w, h), Image.LANCZOS)


# --------------------------------------------------------------------------- phash
def _dct2_axis0(x):
    """Unnormalised DCT-II along axis 0 (what scipy.fftpack.dct(x, axis=0) computes)."""
    import numpy as np

    n = x.shape[0]
    k = np.arange(n, dtype=np.float64).reshape(n, 1)
    m = np.arange(n, dtype=np.float64).reshape(1, n)
    basis = 2.0 * np.cos(np.pi * k * (2.0 * m + 1.0) / (2.0 * n))      # [k, m]
    return basis @ x


def phash_numpy(img, hash_size: int = HASH_SIZE, highfreq_factor: int = HIGHFREQ_FACTOR) -> str:
    """imagehash.phash re-implemented with numpy only (same bits, same hex layout)."""
    import numpy as np
    from PIL import Image

    img_size = hash_size * highfreq_factor
    small = img.convert("L").resize((img_size, img_size), Image.LANCZOS)
    pixels = np.asarray(small, dtype=np.float64)
    dct = _dct2_axis0(_dct2_axis0(pixels).T).T          # axis 0 then axis 1
    # scipy's FFT-based DCT returns exact zeros for flat regions (black frames, gradients);
    # a matrix DCT leaves float noise there that would flip bits around the median. Round it off.
    low = np.round(dct[:hash_size, :hash_size], 3)
    low[low == 0.0] = 0.0                                # fold -0.0 into 0.0
    med = np.median(low)
    bits = (low > med).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(bool(b))
    return f"{value:0{hash_size * hash_size // 4}x}"


def phash(img) -> str:
    """64-bit perceptual hash as 16 hex chars (imagehash.phash when available)."""
    try:
        import imagehash
    except ImportError:
        return phash_numpy(img)
    return str(imagehash.phash(img, hash_size=HASH_SIZE, highfreq_factor=HIGHFREQ_FACTOR))


def hamming(hex_a: str, hex_b: str) -> int:
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def frame_hashes(img, scales: Sequence[float] = config.PHASH_SCALES) -> dict[str, str]:
    """{scale: phash} for one frame: crop (his zoom) first, then mask the text bands, so the
    masked fraction is the same fraction of what the viewer sees at every scale."""
    return {_scale_key(s): phash(mask_text_bands(center_crop(img, s))) for s in scales}


# --------------------------------------------------------------------------- signatures
def signature(video: Path | str, n: int = config.PHASH_FRAMES,
              scales: Sequence[float] = config.PHASH_SCALES, cache_dir: Path | None = None) -> dict:
    """{duration, frames: [{t, hashes: {"1.0": hex, ...}}], frame1: hex, frame1_hashes: {...}}"""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    duration = probe_duration(video)
    frames = []
    for t in frame_times(duration, n):
        img = extract_frame(video, t, cache_dir=cache_dir)
        frames.append({"t": t, "hashes": frame_hashes(img, scales)})
    first = extract_frame(video, 0.0, cache_dir=cache_dir)
    f1 = frame_hashes(first, scales)
    return {"duration": duration, "frames": frames,
            "frame1": f1[_scale_key(1.0)] if _scale_key(1.0) in f1 else next(iter(f1.values())),
            "frame1_hashes": f1}


def _min_pair_distance(ha: dict, hb: dict) -> int:
    best = HASH_SIZE * HASH_SIZE
    for a in ha.values():
        for b in hb.values():
            d = hamming(a, b)
            if d < best:
                best = d
    return best


def frame1_pair_distance(sig_a: dict, sig_b: dict) -> int:
    """Frame-1 distance: min over crop-scale pairs when both carry frame1_hashes, else plain."""
    ha, hb = sig_a.get("frame1_hashes"), sig_b.get("frame1_hashes")
    if ha and hb:
        return _min_pair_distance(ha, hb)
    return hamming(sig_a["frame1"], sig_b["frame1"])


def distance(sig_a: dict, sig_b: dict) -> dict:
    """{median, per_frame, frame1}: per frame the min Hamming over all (scale_a, scale_b) pairs."""
    fa, fb = sig_a.get("frames", []), sig_b.get("frames", [])
    per = [_min_pair_distance(a["hashes"], b["hashes"]) for a, b in zip(fa, fb)]
    med = float(statistics.median(per)) if per else float(HASH_SIZE * HASH_SIZE)
    return {"median": med, "per_frame": per, "frame1": frame1_pair_distance(sig_a, sig_b)}


def match(sig_a: dict, sig_b: dict) -> bool:
    return distance(sig_a, sig_b)["median"] <= config.PHASH_MATCH_MAX


def label(candidate_sig: dict, delivery_sigs: dict) -> dict:
    """Attribution of a posted reel against delivered renders.

    {label: CLAUDE | CLAUDIN | UNSURE, best_id, median}. CLAUDE when the best median is
    <= config.PHASH_MATCH_MAX, CLAUDIN when >= PHASH_CLAUDIN_MIN, UNSURE between (and when
    there is nothing to compare against: no deliveries means no decision, not a verdict).
    """
    best_id, best = None, None
    for key, sig in delivery_sigs.items():
        d = distance(candidate_sig, sig)["median"]
        if best is None or d < best:
            best_id, best = key, d
    if best is None:
        return {"label": "UNSURE", "best_id": None, "median": None}
    if best <= config.PHASH_MATCH_MAX:
        lab = "CLAUDE"
    elif best >= PHASH_CLAUDIN_MIN:
        lab = "CLAUDIN"
    else:
        lab = "UNSURE"
    return {"label": lab, "best_id": best_id, "median": best}


# --------------------------------------------------------------------------- store
def store_path() -> Path:
    return config.DATA / "signatures.json"


def load_store(path: Path | None = None) -> dict:
    """{"version": 1, "signatures": {key: {kind, video, added_et, sig}}}"""
    p = path or store_path()
    data = safeio.read_json(p, default=None)
    if not isinstance(data, dict) or "signatures" not in data:
        return {"version": STORE_VERSION, "signatures": {}}
    data.setdefault("version", STORE_VERSION)
    return data


def save_store(store: dict, path: Path | None = None) -> Path:
    p = path or store_path()
    return safeio.write_json(p, store, overwrite=True)


def add_signature(key: str, video: Path | str, kind: str, now: datetime | None = None,
                  path: Path | None = None, store: dict | None = None, sig: dict | None = None) -> dict:
    """Compute the signature of `video` (or take `sig`), file it under `key` with kind
    delivery|posted, save the store and return the entry."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not key:
        raise ValueError("key must not be empty")
    now = now or datetime.now(config.TZ)
    st = store if store is not None else load_store(path)
    entry = {"kind": kind, "video": str(Path(video)), "added_et": now.isoformat(timespec="seconds"),
             "sig": sig or signature(video)}
    st["signatures"][key] = entry
    save_store(st, path)
    return {"key": key, **entry}


def entries_of_kind(store: dict, kinds: Iterable[str]) -> dict[str, dict]:
    want = set(kinds)
    return {k: v for k, v in store.get("signatures", {}).items() if v.get("kind") in want}


def find_matches(video: Path | str, kinds: Iterable[str] = KINDS, store: dict | None = None,
                 path: Path | None = None, sig: dict | None = None) -> list[dict]:
    """Every stored signature of the given kinds, ranked by median distance (closest first)."""
    st = store if store is not None else load_store(path)
    cand = sig or signature(video)
    out = []
    for key, entry in entries_of_kind(st, kinds).items():
        d = distance(cand, entry["sig"])
        out.append({"key": key, "kind": entry.get("kind"), "video": entry.get("video"),
                    "median": d["median"], "frame1": d["frame1"],
                    "match": d["median"] <= config.PHASH_MATCH_MAX})
    out.sort(key=lambda r: (r["median"], r["frame1"], r["key"]))
    return out


def frame1_report(video: Path | str, store: dict | None = None, path: Path | None = None,
                  sig: dict | None = None) -> dict:
    """{distance, key, reused}: the closest posted frame 1 to this reel's frame 1 (G11)."""
    st = store if store is not None else load_store(path)
    cand = sig or signature(video)
    best_key, best = None, None
    for key, entry in entries_of_kind(st, ("posted",)).items():
        d = frame1_pair_distance(cand, entry["sig"])
        if best is None or d < best:
            best_key, best = key, d
    return {"distance": best, "key": best_key,
            "reused": best is not None and best <= config.PHASH_MATCH_MAX}


def frame1_distance(video: Path | str, store: dict | None = None, path: Path | None = None,
                    sig: dict | None = None) -> int | None:
    """Min frame-1 distance over posted signatures (None when nothing has been posted yet)."""
    return frame1_report(video, store=store, path=path, sig=sig)["distance"]


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="py -m tools.fingerprint", description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--store", type=Path, default=None, help="signature store (default data/signatures.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign", help="fingerprint a video and file it in the store")
    s.add_argument("video", type=Path)
    s.add_argument("--key", required=True, help="delivery id or posted shortcode")
    s.add_argument("--kind", choices=KINDS, required=True)
    m = sub.add_parser("match", help="rank stored signatures by distance to this video")
    m.add_argument("video", type=Path)
    m.add_argument("--kinds", default="posted,delivery", help="comma list: posted,delivery")
    f = sub.add_parser("frame1", help="frame-1 reuse check against posted reels (exit 1 when reused)")
    f.add_argument("video", type=Path)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "sign":
            entry = add_signature(a.key, a.video, a.kind, path=a.store)
            if a.json:
                print(json.dumps(entry, indent=2))
            else:
                print(f"signed {a.key} ({a.kind}) frame1={entry['sig']['frame1']} "
                      f"frames={len(entry['sig']['frames'])}")
            return 0
        if a.cmd == "match":
            kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
            rows = find_matches(a.video, kinds, path=a.store)
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                if not rows:
                    print("no stored signatures of those kinds")
                for r in rows:
                    print(f"{'MATCH ' if r['match'] else '      '}{r['key']:<24} {r['kind']:<9} "
                          f"median={r['median']:<5g} frame1={r['frame1']}")
            return 0
        if a.cmd == "frame1":
            rep = frame1_report(a.video, path=a.store)
            if a.json:
                print(json.dumps(rep, indent=2))
            else:
                if rep["distance"] is None:
                    print("frame1: no posted reels in the store")
                else:
                    print(f"frame1: distance {rep['distance']} to {rep['key']} "
                          f"({'REUSED' if rep['reused'] else 'ok'})")
            return 1 if rep["reused"] else 0
    except (OSError, RuntimeError, ValueError, safeio.SafeIOError) as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
