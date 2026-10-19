"""Perceptual fingerprints of reels (WEAK_POINTS W16, START_HERE 6D G11 and section 8).

Why: once Claudin zooms into a delivered letterbox, adds his hook text and a trending sound,
Claude could not tell its own cuts from his, so most posted reels were logged UNSURE and the
learning loop on Claude's edits stalled. This module makes the attribution mechanical and
also powers the G11 "frame-1 was already posted" check.

How (the tt_dedupe method):
- 9 frames per reel at relative times (i + 0.5) / 9 of the duration, so the same positions
  line up whatever the pixel size or container;
- HLG / PQ masters (Meta glasses footage, arib-std-b67 or smpte2084) are tonemapped with the
  engine's CPU chain (tools.encode.TONEMAP_CPU) before hashing, because Instagram serves his
  re-upload as a tonemapped SDR rendition: both sides go through the same pipeline (G1);
- each frame is scaled to width 360 keeping its aspect and padded to 360x640 on black, so a
  raw landscape clip, a letterboxed 1440x2560 render and a full-bleed 1080x1920 upload all
  land in the same place of the same canvas;
- for the crop scales 1.0, 1.15 and 1.33 the centre of the frame is cropped and resized back
  (he zooms into the letterbox), then the top 18% and bottom 22% are painted mid-grey (he adds
  hook text there, Instagram draws its UI there) and a 64-bit pHash is taken;
- the distance between two reels is, per frame, the smallest Hamming distance over every pair
  of crop scales; the median over the 9 frames decides: <= config.PHASH_MATCH_MAX (12) is a
  match (CLAUDE), >= PHASH_CLAUDIN_MIN (20) is clearly not (CLAUDIN), between is UNSURE;
- frame 1 (t = 0) is hashed separately for G11; `frame1_phash(video)` is the name gates.py
  looks for, and `frame1_report(video, sig=...)` gives the shortcode and the `reused` flag.

A reel whose duration ffprobe cannot read raises RuntimeError instead of producing an empty
signature (an empty signature would compare at distance 64 to everything and be logged
CLAUDIN with no warning).

pHash is imagehash.phash(hash_size=8) when imagehash is installed, otherwise a pure numpy
implementation of the same algorithm (grey, 32x32 Lanczos, DCT-II, top-left 8x8, median
threshold) that produces the same bits.

Signatures live in data/signatures.json (keys = delivery ids or posted shortcodes, each with
kind "delivery" or "posted"). Library functions return data and never print.

Run:  py -m tools.fingerprint sign <video> --key K --kind delivery|posted [--json]
      py -m tools.fingerprint match <video> [--kinds posted,delivery] [--json]
      py -m tools.fingerprint frame1 <video> [--json]
      (--json and --store work before or after the subcommand)
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
HLG_TRANSFERS = ("arib-std-b67", "smpte2084")
# Same chain as tools.encode.TONEMAP_CPU (read from there when importable, so a change to the
# renderer's tonemap is picked up here and both sides of an attribution stay identical).
TONEMAP_CPU_FALLBACK = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                        "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


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


def probe_video(video: Path | str) -> dict:
    """{duration, color_transfer} via one ffprobe call.

    duration is the container (format) duration, falling back to the video stream's own
    duration when the container has none (fragmented / odd MP4s); 0.0 when both are unknown.
    """
    r = safeio.run_ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=duration,color_transfer:format=duration", "-of", "json",
                            str(video)])
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        d = {}
    st = (d.get("streams") or [{}])[0] if isinstance(d.get("streams"), list) else {}
    duration = 0.0
    for raw in ((d.get("format") or {}).get("duration"), st.get("duration")):
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            duration = value
            break
    return {"duration": duration, "color_transfer": str(st.get("color_transfer") or "").lower()}


def probe_duration(video: Path | str) -> float:
    """Duration in seconds (container, else video stream; 0.0 when unknown)."""
    return probe_video(video)["duration"]


def probe_color_transfer(video: Path | str) -> str:
    """'arib-std-b67', 'smpte2084', 'bt709', ... ('' when untagged)."""
    return probe_video(video)["color_transfer"]


def is_hlg(transfer: str | None) -> bool:
    return (transfer or "").lower() in HLG_TRANSFERS


def tonemap_chain() -> str:
    """The engine's CPU tonemap (tools.encode.TONEMAP_CPU when that module is importable)."""
    try:
        from . import encode
        chain = getattr(encode, "TONEMAP_CPU", None)
        if isinstance(chain, str) and chain:
            return chain
    except ImportError:
        pass
    return TONEMAP_CPU_FALLBACK


def _file_tag(video: Path) -> str:
    try:
        st = video.stat()
        raw = f"{video.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        raw = str(video)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def frame_filter(size: tuple[int, int] = (FRAME_W, FRAME_H), transfer: str | None = None) -> str:
    """Tonemap (HLG/PQ sources only), then scale to fit the canvas keeping aspect (width 360
    for anything 9:16 or wider), then pad to the full canvas centred on black. A letterboxed
    render and a raw clip line up; an HLG master and its SDR re-upload hash the same."""
    w, h = size
    pre = tonemap_chain() + "," if is_hlg(transfer) else ""
    return (f"{pre}scale=w={w}:h={h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24")


def extract_frame(video: Path | str, t: float, size: tuple[int, int] = (FRAME_W, FRAME_H),
                  cache_dir: Path | None = None, transfer: str | None = None):
    """One frame at time t as a PIL.Image (RGB, exactly `size`), via one ffmpeg call.

    -ss sits before -i (keyframe seek then exact), -frames:v 1. The PNG lands in
    cache/fingerprint/ (overwrite is allowed there) and is loaded back. `transfer` is the
    stream's color_transfer (probed when None); HLG/PQ sources are tonemapped first.
    """
    from PIL import Image

    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    if transfer is None:
        transfer = probe_color_transfer(video)
    cache_dir = cache_dir or (config.CACHE / "fingerprint")
    out = cache_dir / f"{_file_tag(video)}_{int(round(t * 1000)):08d}.png"
    args = ["-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
            "-vf", frame_filter(size, transfer), "-f", "image2", "-update", "1"]
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
    """{duration, transfer, frames: [{t, hashes: {"1.0": hex, ...}}], frame1: hex, frame1_hashes}

    Raises RuntimeError when ffprobe reports no usable duration: an empty signature would
    silently compare at distance 64 to everything.
    """
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    duration = float(probe_duration(video) or 0.0)
    if duration <= 0:
        raise RuntimeError(f"unknown duration for {video} (ffprobe gave no format or stream duration)")
    transfer = probe_color_transfer(video)
    frames = []
    for t in frame_times(duration, n):
        img = extract_frame(video, t, cache_dir=cache_dir, transfer=transfer)
        frames.append({"t": t, "hashes": frame_hashes(img, scales)})
    if not frames:
        raise RuntimeError(f"no frames sampled from {video} (duration {duration}, n {n})")
    first = extract_frame(video, 0.0, cache_dir=cache_dir, transfer=transfer)
    f1 = frame_hashes(first, scales)
    return {"duration": duration, "transfer": transfer, "frames": frames,
            "frame1": f1[_scale_key(1.0)] if _scale_key(1.0) in f1 else next(iter(f1.values())),
            "frame1_hashes": f1}


def frame1_phash(video: Path | str) -> str:
    """Frame-1 hash at crop scale 1.0, identical to signature(video)['frame1'] (the name
    tools.gates probes for its G11 check)."""
    video = Path(video)
    transfer = probe_color_transfer(video)
    f1 = frame_hashes(extract_frame(video, 0.0, transfer=transfer))
    return f1[_scale_key(1.0)] if _scale_key(1.0) in f1 else next(iter(f1.values()))


def _as_sig(sig) -> dict:
    """Accept a signature or a store entry ({kind, video, sig}); refuse anything else."""
    if isinstance(sig, dict) and isinstance(sig.get("sig"), dict) and "frames" not in sig:
        sig = sig["sig"]
    if not isinstance(sig, dict) or not sig.get("frames") or not sig.get("frame1"):
        raise ValueError("not a signature: missing frames/frame1 (pass entry['sig'] from the store, "
                         "and never a signature of a reel whose duration was unknown)")
    return sig


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
    sig_a, sig_b = _as_sig(sig_a), _as_sig(sig_b)
    ha, hb = sig_a.get("frame1_hashes"), sig_b.get("frame1_hashes")
    if ha and hb:
        return _min_pair_distance(ha, hb)
    return hamming(sig_a["frame1"], sig_b["frame1"])


def distance(sig_a: dict, sig_b: dict) -> dict:
    """{median, per_frame, frame1}: per frame the min Hamming over all (scale_a, scale_b) pairs.

    Raises ValueError when either argument is not a signature with frames and frame1.
    """
    sig_a, sig_b = _as_sig(sig_a), _as_sig(sig_b)
    fa, fb = sig_a["frames"], sig_b["frames"]
    per = [_min_pair_distance(a["hashes"], b["hashes"]) for a, b in zip(fa, fb)]
    med = float(statistics.median(per))
    return {"median": med, "per_frame": per, "frame1": frame1_pair_distance(sig_a, sig_b)}


def match(sig_a: dict, sig_b: dict) -> bool:
    return distance(sig_a, sig_b)["median"] <= config.PHASH_MATCH_MAX


def label(candidate_sig: dict, delivery_sigs: dict) -> dict:
    """Attribution of a posted reel against delivered renders.

    {label: CLAUDE | CLAUDIN | UNSURE, best_id, median}. CLAUDE when the best median is
    <= config.PHASH_MATCH_MAX, CLAUDIN when >= PHASH_CLAUDIN_MIN, UNSURE between (and when
    there is nothing to compare against: no deliveries means no decision, not a verdict).
    """
    candidate_sig = _as_sig(candidate_sig)
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
    """{"version": 1, "signatures": {key: {kind, video, added_et, sig}}}

    A missing store is empty; a corrupt one raises ValueError (it is never silently replaced,
    his posted history lives there).
    """
    p = path or store_path()
    try:
        data = safeio.read_json(p, default=None)
    except ValueError as e:
        raise ValueError(f"signature store {p} is not valid JSON: {e}") from e
    if data is None:
        return {"version": STORE_VERSION, "signatures": {}}
    if not isinstance(data, dict) or not isinstance(data.get("signatures"), dict):
        raise ValueError(f"signature store {p} has no 'signatures' object")
    data.setdefault("version", STORE_VERSION)
    return data


def save_store(store: dict, path: Path | None = None) -> Path:
    p = path or store_path()
    return safeio.write_json(p, store, overwrite=True)


def add_signature(key: str, video: Path | str, kind: str, now: datetime | None = None,
                  path: Path | None = None, store: dict | None = None, sig: dict | None = None) -> dict:
    """Compute the signature of `video` (or take `sig`), file it under `key` with kind
    delivery|posted, save the store and return the entry. added_et always carries the ET
    offset (a naive `now` is taken as ET)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if not key:
        raise ValueError("key must not be empty")
    now = now or datetime.now(config.TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.TZ)
    st = store if store is not None else load_store(path)
    entry = {"kind": kind, "video": str(Path(video)), "added_et": now.isoformat(timespec="seconds"),
             "sig": _as_sig(sig) if sig is not None else signature(video)}
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
def _common_parser() -> argparse.ArgumentParser:
    """--json / --store accepted before or after the subcommand (SUPPRESS so a subparser's
    default never clobbers a flag given at the top level)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    common.add_argument("--store", type=Path, default=argparse.SUPPRESS,
                        help="signature store (default data/signatures.json)")
    return common


def main(argv: Sequence[str] | None = None) -> int:
    common = _common_parser()
    ap = argparse.ArgumentParser(prog="py -m tools.fingerprint", description=__doc__.splitlines()[0],
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign", help="fingerprint a video and file it in the store", parents=[common])
    s.add_argument("video", type=Path)
    s.add_argument("--key", required=True, help="delivery id or posted shortcode")
    s.add_argument("--kind", choices=KINDS, required=True)
    m = sub.add_parser("match", help="rank stored signatures by distance to this video", parents=[common])
    m.add_argument("video", type=Path)
    m.add_argument("--kinds", default="posted,delivery", help="comma list: posted,delivery")
    f = sub.add_parser("frame1", help="frame-1 reuse check against posted reels (exit 1 when reused)",
                       parents=[common])
    f.add_argument("video", type=Path)
    a = ap.parse_args(argv)
    as_json = bool(getattr(a, "json", False))
    store = getattr(a, "store", None)

    try:
        if a.cmd == "sign":
            entry = add_signature(a.key, a.video, a.kind, path=store)
            if as_json:
                print(json.dumps(entry, indent=2))
            else:
                print(f"signed {a.key} ({a.kind}) frame1={entry['sig']['frame1']} "
                      f"frames={len(entry['sig']['frames'])}")
            return 0
        if a.cmd == "match":
            kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
            rows = find_matches(a.video, kinds, path=store)
            if as_json:
                print(json.dumps(rows, indent=2))
            else:
                if not rows:
                    print("no stored signatures of those kinds")
                for r in rows:
                    print(f"{'MATCH ' if r['match'] else '      '}{r['key']:<24} {r['kind']:<9} "
                          f"median={r['median']:<5g} frame1={r['frame1']}")
            return 0
        if a.cmd == "frame1":
            rep = frame1_report(a.video, path=store)
            if as_json:
                print(json.dumps(rep, indent=2))
            else:
                if rep["distance"] is None:
                    print("frame1: no posted reels in the store")
                else:
                    print(f"frame1: distance {rep['distance']} to {rep['key']} "
                          f"({'REUSED' if rep['reused'] else 'ok'})")
            return 1 if rep["reused"] else 0
    except (OSError, RuntimeError, ValueError, safeio.SafeIOError) as e:
        if as_json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
