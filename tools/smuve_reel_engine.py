#!/usr/bin/env python3
"""SMUVE reel edit engine (system prompt s6).

Builds a finished 1080x1920 reel from a source clip. No re-grading: source
pixels are untouched except an HDR->SDR tonemap and the text/brand overlays.

  analyze  <src>                     -> beat, speech pauses, HDR flag, duration
  build    <src> --format F --text "..." [opts]   -> out/<name>.mp4
  endcard  --out card.mp4            -> 1.0s SMUVE product card
  contact  <video> --out sheet.jpg   -> 6-frame contact sheet
  firstframe <video> --out f1.png

Formats: caption_bar | overlay | setup_reaction | split | quote | debate
"""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile

W, H = 1080, 1920
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
BRAND_TAG = "SMUVE"
V_ENC = ["-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", "-r", "30", "-colorspace", "bt709",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart"]
A_ENC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def font():
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    raise RuntimeError("no bold sans font found")


def sh(cmd, timeout=600, check=False):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr.strip()[-800:]}")
    return p


def probe(path):
    p = sh(["ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,width,height,color_transfer,color_primaries",
            "-of", "json", path], check=True)
    d = json.loads(p.stdout)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in d["streams"])
    trc = (v.get("color_transfer") or "").lower()
    return {
        "duration": float(d["format"]["duration"]),
        "width": v.get("width"), "height": v.get("height"),
        "has_audio": has_audio,
        "hdr": trc in ("arib-std-b67", "smpte2084"),
        "trc": trc,
    }


# ------------------------------------------------------------------ analyze

def loudness_curve(path, _hop=0.10):
    """Momentary loudness (M) every 100ms from ebur128. The reaction beat is
    the biggest sustained jump in this curve."""
    p = sh(["ffmpeg", "-v", "verbose", "-i", path, "-vn",
            "-filter_complex", "ebur128=metadata=1:framelog=verbose",
            "-f", "null", "-"], timeout=600)
    vals = []
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET:\s*\S+\s+LUFS\s+M:\s*(-?[\d.]+)",
                         p.stderr):
        t, v = float(m.group(1)), float(m.group(2))
        if v < -100:          # ebur128 floor = silence
            v = -70.0
        vals.append((t, v))
    return vals


def silences(path, noise_db=-32, min_dur=0.18):
    p = sh(["ffmpeg", "-v", "info", "-i", path, "-af",
            f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"])
    out = p.stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    pairs = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        pairs.append((s, e))
    return pairs


def scene_changes(path, thresh=0.30):
    p = sh(["ffmpeg", "-v", "error", "-i", path, "-vf",
            f"select='gt(scene,{thresh})',metadata=print", "-an", "-f", "null", "-"])
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", p.stdout + p.stderr)]


def find_beat(path, info):
    """Peak = largest sustained rise in momentary loudness; motion confirms it.

    Rises out of the opening silence are ignored (they are the clip fading in,
    not the reaction), as are peaks in the first 15% of the clip.
    """
    curve = loudness_curve(path)
    beat, score = None, 0.0
    if len(curve) > 6:
        step = max(0.05, curve[1][0] - curve[0][0])
        win = max(2, int(round(0.5 / step)))
        floor_t = info["duration"] * 0.15
        for i in range(win, len(curve)):
            t, after = curve[i]
            if t < floor_t:
                continue
            before = sum(v for _, v in curve[i - win:i]) / win
            if before < -45 or after < -30:      # ramp out of silence, not a beat
                continue
            rise = after - before
            if rise > score:
                score, beat = rise, t
    cuts = scene_changes(path)
    return {"beat": beat, "rise_db": round(score, 2), "scene_cuts": cuts[:20],
            "silences": silences(path)}


def safe_outpoint(path, target, info):
    """Push the out-point into the pause after the last word (+0.1s)."""
    for s, e in silences(path):
        if s >= target - 0.35:
            return min(info["duration"], s + 0.10)
    return min(info["duration"], target)


def cmd_analyze(src):
    info = probe(src)
    b = find_beat(src, info)
    out = {**info, **b}
    print(json.dumps(out, indent=2))
    return 0


# ------------------------------------------------------------------ text

def wrap(text, per_line=22):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= per_line or not cur:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def textfile(td, text):
    p = os.path.join(td, f"t{abs(hash(text)) % 99999}.txt")
    with open(p, "w") as f:
        f.write(text)
    return p


def drawtext(td, text, *, y, size, color="white", box=0, boxcolor="black@0.0",
             borderw=0, bordercolor="black", x="(w-text_w)/2", alpha=None,
             enable=None, line_spacing=10):
    parts = [f"fontfile={font()}", f"textfile={textfile(td, text)}",
             f"fontsize={size}", f"fontcolor={color}", f"x={x}", f"y={y}",
             f"line_spacing={line_spacing}"]
    if box:
        parts += [f"box=1", f"boxcolor={boxcolor}", "boxborderw=28"]
    if borderw:
        parts += [f"borderw={borderw}", f"bordercolor={bordercolor}"]
    if alpha is not None:
        parts.append(f"alpha={alpha}")
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def brand_tag(td):
    # lower-left safe area, ~40% opacity, above the bottom-20% UI band
    return drawtext(td, BRAND_TAG, y=int(H * 0.775), size=38, color="white",
                    alpha=0.40, x=int(W * 0.055), borderw=2, bordercolor="black@0.5")


# ------------------------------------------------------------------ scaling

def fit_chain(info, target_w, target_h, mode="cover"):
    """No re-grade. HDR gets the standard tonemap, everything else passes through."""
    pre = ""
    if info["hdr"]:
        pre = ("zscale=t=linear:npl=400,format=gbrpf32le,"
               "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
               "zscale=t=bt709:m=bt709:r=tv,")
    if mode == "cover":
        return (f"{pre}scale={target_w}:{target_h}:force_original_aspect_ratio=increase:"
                f"flags=lanczos,crop={target_w}:{target_h},setsar=1")
    return (f"{pre}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:"
            f"flags=lanczos,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")



def measure_loudness(src, start, dur):
    """Pass 1 of two-pass loudnorm on the exact trimmed segment."""
    p = sh(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", src,
            "-vn", "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
            "-f", "null", "-"], timeout=600)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        if d.get("input_i") in (None, "-inf", "inf"):
            return None
        return {k: d[k] for k in ("input_i", "input_tp", "input_lra", "input_thresh")}
    except Exception:
        return None


def loudnorm_filter(meas):
    """Pass 2. Linear mode holds the target exactly; dynamic mode overshoots."""
    if not meas:
        return "loudnorm=I=-14:TP=-1:LRA=11"
    return ("loudnorm=I=-14:TP=-1:LRA=11:linear=true"
            f":measured_I={meas['input_i']}:measured_TP={meas['input_tp']}"
            f":measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}"
            ":print_format=summary")


def text_box(text, size, y, max_w=None):
    """Exact pixel box of a centred drawtext block, from the real font metrics."""
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(font(), size)
        lines = text.split("\n")
        widths = [f.getbbox(l)[2] - f.getbbox(l)[0] for l in lines] or [0]
        lh = size * 1.0 + 10
        w = max(widths)
        h = int(lh * len(lines))
    except Exception:
        w, h = int(size * 0.55 * max(len(l) for l in text.split("\n"))), int(size * 1.2 * len(text.split("\n")))
    x0 = int((W - w) / 2)
    return {"x0": x0, "y0": int(y), "x1": int(x0 + w), "y1": int(y + h)}


# ------------------------------------------------------------------ build

def build(args):
    src = args.src
    info = probe(src)
    start = args.start
    end = args.end if args.end else min(info["duration"], start + args.length)
    if args.snap_words and info["has_audio"]:
        end = safe_outpoint(src, end, info)
    dur = round(end - start, 3)
    if dur < 1.0:
        raise SystemExit("clip too short after trim")
    if dur > 6.5 and not args.allow_long_source:
        print(f"  note: {dur:.1f}s from one source - s4 caps ~6s; pass --allow-long-source "
              f"only for your own footage")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        lines = wrap(args.text, 20 if args.format == "caption_bar" else 18)
        body = "\n".join(lines)
        nwords = len(args.text.split())
        if nwords > 12:
            print(f"  WARN  {nwords} words on screen - s6.5 caps 12")

        filters = []
        if args.format == "caption_bar":
            bar_h = int(H * 0.17) + 70 * (len(lines) - 1)
            text_y = int(bar_h * 0.30)
            vid_h = H - bar_h
            filters.append(f"[0:v]{fit_chain(info, W, vid_h, 'cover')}[v]")
            filters.append(f"color=white:s={W}x{bar_h}:d={dur}:r=30[bar]")
            filters.append("[bar][v]vstack=inputs=2[stacked]")
            chain = (f"[stacked]" +
                     drawtext(td, body, y=text_y, size=args.fontsize,
                              color="black") + "," + brand_tag(td) + "[vout]")
            filters.append(chain)
        elif args.format == "split":
            half = H // 2
            text_y = int(H * 0.055)
            filters.append(f"[0:v]{fit_chain(info, W, half, 'cover')}[top]")
            filters.append(f"[1:v]{fit_chain(probe(args.second), W, half, 'cover')}[bot]")
            filters.append("[top][bot]vstack=inputs=2[stacked]")
            filters.append(f"[stacked]" +
                           drawtext(td, body, y=text_y, size=args.fontsize,
                                    color="white", borderw=8) + "," +
                           brand_tag(td) + "[vout]")
        else:  # overlay / setup_reaction / quote / debate
            text_y = int(H * 0.085)
            filters.append(f"[0:v]{fit_chain(info, W, H, 'cover')}[v]")
            filters.append(f"[v]" +
                           drawtext(td, body, y=text_y, size=args.fontsize,
                                    color="white", borderw=9, bordercolor="black") +
                           "," + brand_tag(td) + "[vout]")

        inputs = ["-ss", f"{start}", "-t", f"{dur}", "-i", src]
        if args.format == "split":
            s2 = args.second or src
            inputs += ["-ss", f"{args.second_start}", "-t", f"{dur}", "-i", s2]

        meas = measure_loudness(src, start, dur) if info["has_audio"] else None
        af = loudnorm_filter(meas)
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
               "-filter_complex", ";".join(filters), "-map", "[vout]"]
        if info["has_audio"]:
            cmd += ["-map", "0:a:0", "-af", af, *A_ENC]
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", f"{2 if args.format=='split' else 1}:a", *A_ENC]
        cmd += [*V_ENC, args.out]
        p = sh(cmd, timeout=900)
        if p.returncode != 0:
            print(p.stderr[-1500:])
            raise SystemExit("ffmpeg build failed")

        boxes = [text_box(body, args.fontsize, text_y)]
        boxes.append({"x0": int(W * 0.055), "y0": int(H * 0.775),
                      "x1": int(W * 0.055) + 120, "y1": int(H * 0.775) + 46,
                      "label": "brand_tag"})
        meta = {"file": os.path.basename(args.out), "format": args.format,
                "source": os.path.abspath(src), "in": start, "out": end,
                "duration": dur, "text": args.text, "fontsize": args.fontsize,
                "text_boxes": boxes, "loudnorm_pass1": meas}
        with open(args.out + ".meta.json", "w") as f:
            json.dump(meta, f, indent=2)
    print(f"built {args.out}  ({dur:.2f}s, format={args.format})")
    return 0


def cmd_endcard(out, dur=1.0):
    with tempfile.TemporaryDirectory() as td:
        f = (f"color=c=#080a0b:s={W}x{H}:d={dur}:r=30," +
             drawtext(td, "SMUVE", y=int(H * 0.40), size=190, color="white") + "," +
             drawtext(td, "7-day free trial\nDM @kenny2smuve",
                      y=int(H * 0.53), size=78, color="#9be36b") +
             ",format=yuv420p")
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
               f"color=c=black:s=16x16:d={dur}:r=30",
               "-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo",
               "-filter_complex", f"{f}[v]", "-map", "[v]", "-map", "1:a",
               *V_ENC, *A_ENC, out]
        p = sh(cmd)
        if p.returncode != 0:
            print(p.stderr[-1200:]); raise SystemExit("endcard failed")
    print(f"built {out}")
    return 0


def cmd_concat(parts, out):
    with tempfile.TemporaryDirectory() as td:
        lst = os.path.join(td, "l.txt")
        with open(lst, "w") as f:
            for p in parts:
                f.write(f"file '{os.path.abspath(p)}'\n")
        # re-encode: sources differ in GOP structure
        p = sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", lst, *V_ENC, *A_ENC, out], timeout=900)
        if p.returncode != 0:
            print(p.stderr[-1200:]); raise SystemExit("concat failed")
    print(f"built {out}")
    return 0


def cmd_contact(video, out, n=6):
    info = probe(video)
    with tempfile.TemporaryDirectory() as td:
        shots = []
        for i in range(n):
            ts = info["duration"] * (i + 0.5) / n
            p = os.path.join(td, f"s{i}.png")
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", video,
                "-frames:v", "1", "-vf", "scale=360:-1", p])
            if os.path.exists(p):
                shots.append(p)
        ins = []
        for s in shots:
            ins += ["-i", s]
        fc = "".join(f"[{i}:v]" for i in range(len(shots))) + \
             f"xstack=inputs={len(shots)}:layout=0_0|w0_0|w0+w1_0|0_h0|w0_h0|w0+w1_h0[v]"
        p = sh(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", fc,
                "-map", "[v]", "-q:v", "3", out])
        if p.returncode != 0:
            print(p.stderr[-800:]); raise SystemExit("contact sheet failed")
    print(f"built {out}")
    return 0


def cmd_firstframe(video, out):
    sh(["ffmpeg", "-y", "-v", "error", "-ss", "0.03", "-i", video,
        "-frames:v", "1", out], check=True)
    print(f"built {out}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze"); a.add_argument("src")

    b = sub.add_parser("build")
    b.add_argument("src")
    b.add_argument("--format", default="overlay",
                   choices=["caption_bar", "overlay", "setup_reaction", "split",
                            "quote", "debate"])
    b.add_argument("--text", required=True)
    b.add_argument("--start", type=float, default=0.0)
    b.add_argument("--end", type=float, default=None)
    b.add_argument("--length", type=float, default=6.0)
    b.add_argument("--fontsize", type=int, default=72)
    b.add_argument("--second", default=None, help="second source for split")
    b.add_argument("--second-start", type=float, default=0.0)
    b.add_argument("--snap-words", action="store_true", default=True)
    b.add_argument("--allow-long-source", action="store_true")
    b.add_argument("--out", required=True)

    e = sub.add_parser("endcard"); e.add_argument("--out", required=True)
    e.add_argument("--dur", type=float, default=1.0)

    c = sub.add_parser("concat"); c.add_argument("parts", nargs="+")
    c.add_argument("--out", required=True)

    s = sub.add_parser("contact"); s.add_argument("video"); s.add_argument("--out", required=True)
    ff = sub.add_parser("firstframe"); ff.add_argument("video"); ff.add_argument("--out", required=True)

    args = ap.parse_args()
    if args.cmd == "analyze":
        return cmd_analyze(args.src)
    if args.cmd == "build":
        return build(args)
    if args.cmd == "endcard":
        return cmd_endcard(args.out, args.dur)
    if args.cmd == "concat":
        return cmd_concat(args.parts, args.out)
    if args.cmd == "contact":
        return cmd_contact(args.video, args.out)
    if args.cmd == "firstframe":
        return cmd_firstframe(args.video, args.out)


if __name__ == "__main__":
    sys.exit(main())
