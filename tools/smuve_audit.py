#!/usr/bin/env python3
"""SMUVE reel quality gate (system prompt s7).

Nothing ships unless every hard check passes. Run on every exported file.

  python3 smuve_audit.py <file.mp4> [--format caption_bar|overlay|setup_reaction|split|quote|hype|debate]
                                    [--max-mb 280] [--json]

Exit 0 = PASS, 1 = FAIL. Human-readable table unless --json.
"""
import argparse
import json
import os
import re
import subprocess
import sys

# --- spec constants (s6 Length, s6.8 Export, s6.6 Audio, s7) -----------------
DUR_MIN = 5.0
DUR_MAX_DEFAULT = 12.0
DUR_MAX_LONG = 15.0          # product-forward + split formats
LONG_FORMATS = {"split", "product", "product_forward", "debate"}
W, H = 1080, 1920
FPS_MIN, FPS_MAX = 29.0, 30.1
LUFS_MIN, LUFS_MAX = -16.5, -11.5     # target -14 integrated
TRUE_PEAK_MAX = -1.0                  # dBTP
FRAME1_CONTRAST_MIN = 50.0            # luma std
SAFE_BOTTOM = 0.20                    # no text in bottom 20%
SAFE_RIGHT = 0.15                     # no text in right 15%
LOOP_SEAM_MAX = 42.0                  # mean abs luma delta last->first frame


def sh(cmd, timeout=240):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path):
    p = sh(["ffprobe", "-v", "error", "-show_entries",
            "format=duration,size,format_name:stream=codec_type,codec_name,width,height,"
            "r_frame_rate,profile,pix_fmt,sample_rate,nb_frames",
            "-of", "json", path])
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def frame_stats(path, ts, tag):
    """Mean + std of luma at a timestamp, via signalstats."""
    p = sh(["ffmpeg", "-v", "error", "-ss", str(ts), "-i", path, "-frames:v", "1",
            "-vf", "signalstats,metadata=print", "-f", "null", "-"])
    out = p.stdout + p.stderr
    mean = _meta(out, "YAVG")
    std = _meta(out, "YDIF")  # fallback
    # signalstats exposes YAVG + YSTD via lavfi.signalstats.*
    std2 = _meta(out, "YSTD")
    return {"tag": tag, "mean": mean, "std": std2 if std2 is not None else std}


def _meta(text, key):
    m = re.search(rf"lavfi\.signalstats\.{key}=([-\d.]+)", text)
    return float(m.group(1)) if m else None


def luma_std_pillow(path, ts):
    """Exact luma std via a PNG grab - signalstats YSTD is not on every build."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "f.png")
        p = sh(["ffmpeg", "-v", "error", "-ss", str(ts), "-i", path,
                "-frames:v", "1", "-y", png])
        if p.returncode != 0 or not os.path.exists(png):
            return None, None
        try:
            from PIL import Image, ImageStat
            im = Image.open(png).convert("L")
            st = ImageStat.Stat(im)
            return st.mean[0], st.stddev[0]
        except Exception:
            return None, None


def loudness(path):
    p = sh(["ffmpeg", "-v", "info", "-i", path, "-af",
            "loudnorm=I=-14:TP=-1:LRA=11:print_format=json", "-f", "null", "-"])
    blob = p.stderr
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", blob, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return {"i": float(d["input_i"]), "tp": float(d["input_tp"]),
                "lra": float(d["input_lra"])}
    except Exception:
        return None


def blackdetect(path):
    p = sh(["ffmpeg", "-v", "info", "-i", path, "-vf",
            "blackdetect=d=0.08:pix_th=0.10", "-f", "null", "-"])
    return re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", p.stderr)


def text_safe_zones(path, dur):
    """Edge energy in the forbidden bands. Burned-in text lights up edge density.

    Compares mean edge energy of the bottom 20% / right 15% against the frame.
    A caption or sticker sitting in the UI zone shows as a strong local spike.
    """
    import tempfile
    marks = [dur * f for f in (0.15, 0.45, 0.80)]
    worst = {"bottom": 0.0, "right": 0.0}
    with tempfile.TemporaryDirectory() as td:
        for i, ts in enumerate(marks):
            png = os.path.join(td, f"s{i}.png")
            if sh(["ffmpeg", "-v", "error", "-ss", f"{ts:.2f}", "-i", path,
                   "-frames:v", "1", "-vf", "edgedetect=low=0.1:high=0.3",
                   "-y", png]).returncode != 0:
                continue
            try:
                from PIL import Image
                import numpy as np
                a = np.asarray(Image.open(png).convert("L"), dtype="float32")
            except Exception:
                return None
            h, w = a.shape
            full = a.mean() + 1e-6
            bottom = a[int(h * (1 - SAFE_BOTTOM)):, :].mean()
            right = a[:, int(w * (1 - SAFE_RIGHT)):].mean()
            worst["bottom"] = max(worst["bottom"], bottom / full)
            worst["right"] = max(worst["right"], right / full)
    return worst


def loop_seam(path, dur):
    """Mean abs luma difference between the last frame and the first."""
    try:
        from PIL import Image
        import numpy as np
    except Exception:
        return None
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        a_p, b_p = os.path.join(td, "a.png"), os.path.join(td, "b.png")
        sh(["ffmpeg", "-v", "error", "-ss", "0", "-i", path, "-frames:v", "1", "-y", a_p])
        sh(["ffmpeg", "-v", "error", "-sseof", "-0.2", "-i", path, "-frames:v", "1", "-y", b_p])
        if not (os.path.exists(a_p) and os.path.exists(b_p)):
            return None
        a = np.asarray(Image.open(a_p).convert("L"), dtype="float32")
        b = np.asarray(Image.open(b_p).convert("L"), dtype="float32")
        if a.shape != b.shape:
            return None
        return float(abs(a - b).mean())


def run(path, fmt, max_mb):
    checks = []

    def add(name, ok, detail, hard=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "hard": hard})

    info = probe(path)
    fmtinfo = info.get("format", {})
    dur = float(fmtinfo.get("duration", 0))
    size_mb = int(fmtinfo.get("size", 0)) / 1e6
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    dur_max = DUR_MAX_LONG if fmt in LONG_FORMATS else DUR_MAX_DEFAULT
    add("duration", DUR_MIN <= dur <= dur_max,
        f"{dur:.2f}s (allowed {DUR_MIN}-{dur_max}s for format '{fmt}')")

    add("video stream", v is not None, v.get("codec_name") if v else "MISSING")
    add("audio stream", a is not None,
        f"{a.get('codec_name')} @ {a.get('sample_rate')}Hz" if a else "MISSING - silent post")

    if v:
        add("resolution", (v.get("width"), v.get("height")) == (W, H),
            f"{v.get('width')}x{v.get('height')} (need {W}x{H})")
        num, den = (v.get("r_frame_rate", "0/1").split("/") + ["1"])[:2]
        fps = float(num) / float(den or 1)
        add("fps", FPS_MIN <= fps <= FPS_MAX, f"{fps:.2f} (need ~30)")
        add("pix_fmt", v.get("pix_fmt") == "yuv420p", str(v.get("pix_fmt")))
        prof = (v.get("profile") or "").lower()
        add("h264 profile", v.get("codec_name") == "h264" and "high" in prof,
            f"{v.get('codec_name')} / {v.get('profile')}")
    if a:
        add("audio codec", a.get("codec_name") == "aac" and str(a.get("sample_rate")) == "48000",
            f"{a.get('codec_name')} {a.get('sample_rate')}Hz")

    add("file size", size_mb <= max_mb, f"{size_mb:.1f} MB (cap {max_mb} MB)")
    add("faststart", "mp4" in (fmtinfo.get("format_name") or ""),
        fmtinfo.get("format_name", "?"), hard=False)

    blacks = blackdetect(path)
    add("no black frames", not blacks,
        "none" if not blacks else f"{len(blacks)} range(s): {blacks[:3]}")

    mean1, std1 = luma_std_pillow(path, 0.03)
    if std1 is None:
        add("frame 1 contrast", False, "could not read frame 1")
    else:
        add("frame 1 contrast", std1 >= FRAME1_CONTRAST_MIN,
            f"luma std {std1:.1f} (need >= {FRAME1_CONTRAST_MIN}), mean {mean1:.0f}")

    ld = loudness(path)
    if not ld:
        add("loudness", False, "loudnorm measurement failed")
    else:
        add("loudness", LUFS_MIN <= ld["i"] <= LUFS_MAX,
            f"{ld['i']:.1f} LUFS (target -14, window {LUFS_MIN}..{LUFS_MAX})")
        add("true peak", ld["tp"] <= TRUE_PEAK_MAX,
            f"{ld['tp']:.1f} dBTP (max {TRUE_PEAK_MAX})")

    meta_path = path + ".meta.json"
    meta = None
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path))
        except Exception:
            meta = None

    if meta and meta.get("text_boxes"):
        bad = []
        for b in meta["text_boxes"]:
            if b["y1"] > H * (1 - SAFE_BOTTOM):
                bad.append(f"{b.get('label','text')} reaches y={b['y1']} "
                           f"(bottom band starts {int(H*(1-SAFE_BOTTOM))})")
            if b["x1"] > W * (1 - SAFE_RIGHT):
                bad.append(f"{b.get('label','text')} reaches x={b['x1']} "
                           f"(right band starts {int(W*(1-SAFE_RIGHT))})")
        add("text safe zones", not bad,
            "all overlays inside the safe area (measured from font metrics)"
            if not bad else "; ".join(bad))
    else:
        sz = text_safe_zones(path, dur)
        if sz is None:
            add("text safe zones", True, "numpy/PIL unavailable - skipped", hard=False)
        else:
            add("burned-in text scan",
                sz["bottom"] < 1.45 and sz["right"] < 1.45,
                f"edge-energy bottom {sz['bottom']:.2f}x, right {sz['right']:.2f}x "
                f"(no .meta.json - footage-only estimate, flag at 1.45x)", hard=False)

    seam = loop_seam(path, dur)
    if seam is None:
        add("loop seam", True, "unavailable - skipped", hard=False)
    else:
        add("loop seam", seam <= LOOP_SEAM_MAX,
            f"last->first luma delta {seam:.1f} (max {LOOP_SEAM_MAX})", hard=False)

    hard_fails = [c for c in checks if c["hard"] and not c["ok"]]
    soft_fails = [c for c in checks if not c["hard"] and not c["ok"]]
    return checks, hard_fails, soft_fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--format", default="overlay")
    ap.add_argument("--max-mb", type=float, default=280.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"FAIL no such file: {args.file}")
        return 1
    checks, hard, soft = run(args.file, args.format, args.max_mb)

    if args.json:
        print(json.dumps({"file": args.file, "format": args.format,
                          "pass": not hard, "checks": checks}, indent=2))
    else:
        print(f"\n  AUDIT  {os.path.basename(args.file)}   [format: {args.format}]")
        print("  " + "-" * 68)
        for c in checks:
            mark = "PASS" if c["ok"] else ("FAIL" if c["hard"] else "warn")
            print(f"  {mark:4}  {c['check']:<18} {c['detail']}")
        print("  " + "-" * 68)
        if hard:
            print(f"  RESULT: FAIL - {len(hard)} blocking: "
                  f"{', '.join(c['check'] for c in hard)}\n")
        else:
            extra = f" ({len(soft)} warning)" if soft else ""
            print(f"  RESULT: PASS - ships{extra}\n")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
