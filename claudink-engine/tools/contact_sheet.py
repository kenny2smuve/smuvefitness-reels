"""Time-stamped contact sheets, opening strips and single frames (START_HERE 6A, 7).

What: `sheet()` tiles one frame every N seconds of a clip (default 2 s, 5 columns, 320 px
cells) with the clip time burned into the top-left corner of every cell; `strip()` tiles the
opening seconds at a higher rate (default 0-4 s at 4 fps, 8 columns = 2 rows of 8 frames);
`frame()` grabs one still at a given time. All three are the pictures the scout, the critic
packet and Claudin look at before a line is kept: eyeline, blur, ceiling/floor shots, TVs.

Why ffmpeg only: one decode per picture, no OpenCV, and every call goes through
tools.safeio.run_ffmpeg (-nostdin -n, never -y). Outputs under cache/ may be regenerated;
anywhere else an existing file is never overwritten.

Font: drawtext needs a font. We look for one that exists (CLAUDINK_FONT, Windows arial,
DejaVu/Liberation on Linux, Helvetica on macOS) and otherwise let drawtext pick its own via
fontconfig; if even that fails the picture is made without time stamps rather than not at all.

Run:  py -m tools.contact_sheet sheet <video> --out x.jpg [--every 2] [--cols 5] [--width 320]
      py -m tools.contact_sheet strip <video> --out x.jpg [--start 0 --end 4 --fps 4 --cols 8]
      py -m tools.contact_sheet frame <video> --t 0.03 --out x.png
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Sequence

from . import config, safeio

FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
DEFAULT_EVERY_S = 2.0
DEFAULT_COLS = 5
DEFAULT_WIDTH = 320
STRIP_START_S = 0.0
STRIP_END_S = 4.0
STRIP_FPS = 4
STRIP_COLS = 8
# The critic packet (tools.panel) wants portrait cells that line up whatever the source
# aspect, 13 frames for 0-3 s inclusive in 7x2, and 2 px gutters; make_strip/make_sheet
# default to that contract.
PANEL_STRIP_END_S = 3.0
PANEL_STRIP_STEP_S = 0.25
PANEL_STRIP_COLS = 7
PANEL_STRIP_CELL = (180, 320)
PANEL_SHEET_CELL = (216, 384)
PANEL_PAD = 2
JPEG_QUALITY = "3"
FONT_FAIL_MARKERS = ("font", "drawtext", "fontconfig", "freetype")


class ContactSheetError(RuntimeError):
    """ffmpeg could not produce the picture."""


# --------------------------------------------------------------------------- font + filters
def font_file() -> str | None:
    """A font file that exists on this machine, or None (drawtext then uses fontconfig)."""
    env = os.environ.get("CLAUDINK_FONT")
    if env and Path(env).is_file():
        return env
    for cand in FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _escape_path(p: str) -> str:
    """drawtext option value: forward slashes, escaped colons and quotes (Windows paths)."""
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def stamp_size(cell_width: int) -> int:
    """Font size so that "00:00:00.000" fits inside a cell of this width (10..28 px)."""
    return max(10, min(28, int(cell_width) // 9))


def timestamp_filter(fontsize: int = 20, with_font: bool = True) -> str:
    """drawtext that burns each frame's own pts as hh:mm:ss.mmm into the top-left with a box."""
    font = font_file() if with_font else None
    fontpart = f"fontfile='{_escape_path(font)}':" if font else ""
    return (f"drawtext={fontpart}text='%{{pts\\:hms}}':x=6:y=6:fontsize={int(fontsize)}:"
            f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=3")


def cell_filter(width: int, cell: tuple[int, int] | None) -> str:
    """Scale to `width` keeping the aspect, or letterbox into a fixed `cell` (w, h) box."""
    if cell is None:
        return f"scale={int(width)}:-2"
    cw, ch = int(cell[0]), int(cell[1])
    return (f"scale=w={cw}:h={ch}:force_original_aspect_ratio=decrease,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black")


def tile_filter(cols: int, rows: int, pad: int) -> str:
    t = f"tile={int(cols)}x{int(rows)}"
    if int(pad) > 0:
        t += f":padding={int(pad)}:margin={int(pad)}:color=black"
    return t


def sheet_filter(every: float, cols: int, rows: int, width: int, with_font: bool = True,
                 cell: tuple[int, int] | None = None, pad: int = 0) -> str:
    """Pick the first frame and then one every `every` seconds (works for clips shorter than
    `every`, unlike the fps filter), scale, stamp, tile."""
    n = cols * rows
    size = stamp_size(cell[0] if cell else width)
    stamp = f"{timestamp_filter(size, with_font=with_font)}," if with_font is not None else ""
    return (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{every:g})',"
            f"select='lt(n\\,{n})',{cell_filter(width, cell)},format=yuv420p,"
            f"{stamp}{tile_filter(cols, rows, pad)}")


def strip_filter(start: float, fps: float, frames: int, cols: int, rows: int, width: int,
                 with_font: bool = True, cell: tuple[int, int] | None = None, pad: int = 0) -> str:
    """Resample to `fps`, keep `frames` frames, shift pts back to clip time (the input was
    seeked with -ss, which restarts pts at 0), scale, stamp, tile."""
    size = stamp_size(cell[0] if cell else width)
    stamp = f"{timestamp_filter(size, with_font=with_font)}," if with_font is not None else ""
    return (f"fps={fps:g},select='lt(n\\,{frames})',setpts=PTS+{start:.3f}/TB,"
            f"{cell_filter(width, cell)},format=yuv420p,{stamp}{tile_filter(cols, rows, pad)}")


# --------------------------------------------------------------------------- helpers
def duration_of(video: Path | str) -> float:
    r = safeio.run_ffprobe(["-v", "error", "-show_entries", "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(video)])
    if r.returncode != 0:
        raise ContactSheetError(f"ffprobe failed on {video}: {(r.stderr or '').strip()[-300:]}")
    try:
        return float((r.stdout or "0").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def _overwrite_ok(out: Path) -> bool:
    return safeio.is_under(out, config.CACHE)


def _image_args(out: Path) -> list[str]:
    if out.suffix.lower() in (".jpg", ".jpeg"):
        return ["-q:v", JPEG_QUALITY]
    return []


def _run(pre: list[str], video: Path, vf_with_font: str, vf_no_text: str | None, out: Path,
         timeout: int = 600) -> Path:
    """Run ffmpeg; on a font/drawtext failure retry once without the time stamps."""
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(video)
    out = Path(out)
    overwrite = _overwrite_ok(out)
    attempts = [vf_with_font]
    if vf_no_text is not None:
        attempts.append(vf_no_text)
    last_err = ""
    for i, vf in enumerate(attempts):
        args = [*pre, "-i", str(video), "-vf", vf, "-frames:v", "1", *_image_args(out)]
        r = safeio.run_ffmpeg(args, out, overwrite=overwrite, timeout=timeout)
        if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return out
        last_err = (r.stderr or "").strip()
        if i == 0 and len(attempts) > 1 and not any(m in last_err.lower() for m in FONT_FAIL_MARKERS):
            break
    raise ContactSheetError(f"ffmpeg could not write {out.name}: {last_err[-400:]}")


# --------------------------------------------------------------------------- public API
def sheet_layout(duration: float, every: float, cols: int) -> tuple[int, int]:
    """(frames, rows): frame 0 plus one every `every` seconds, `cols` per row."""
    n = max(1, int(math.floor(duration / every + 1e-6)) + 1) if duration > 0 else 1
    return n, max(1, math.ceil(n / int(cols)))


def sheet(video: Path | str, out: Path | str, every: float = DEFAULT_EVERY_S,
          cols: int = DEFAULT_COLS, width: int = DEFAULT_WIDTH, *,
          cell: tuple[int, int] | None = None, pad: int = 0) -> Path:
    """Contact sheet: one stamped frame every `every` seconds, `cols` per row, cells `width` px
    wide (or letterboxed into a fixed `cell` box, with `pad` px gutters). Rows are computed
    from the clip duration so the whole clip fits on one picture."""
    video, out = Path(video), Path(out)
    every = float(every)
    if every <= 0 or int(cols) < 1 or int(width) < 16:
        raise ValueError("every must be > 0, cols >= 1, width >= 16")
    n, rows = sheet_layout(duration_of(video), every, int(cols))
    vf = sheet_filter(every, int(cols), rows, int(width), cell=cell, pad=pad)
    vf_plain = sheet_filter(every, int(cols), rows, int(width), with_font=None, cell=cell, pad=pad)
    return _run([], video, vf, vf_plain, out)


def strip(video: Path | str, out: Path | str, start: float = STRIP_START_S, end: float = STRIP_END_S,
          fps: float = STRIP_FPS, cols: int = STRIP_COLS, width: int = DEFAULT_WIDTH, *,
          cell: tuple[int, int] | None = None, pad: int = 0, inclusive_end: bool = False) -> Path:
    """Opening strip: [start, end] at `fps` frames per second tiled `cols` wide
    (defaults: 0-4 s at 4 fps = 16 frames in 2 rows of 8). `inclusive_end` also keeps the
    frame at `end` itself (0-3 s at 4 fps -> 13 frames, the critic packet's layout)."""
    video, out = Path(video), Path(out)
    start, end, fps = float(start), float(end), float(fps)
    if end <= start or fps <= 0 or int(cols) < 1:
        raise ValueError("need end > start, fps > 0, cols >= 1")
    frames = max(1, int(round((end - start) * fps)) + (1 if inclusive_end else 0))
    rows = max(1, math.ceil(frames / int(cols)))
    pre: list[str] = []
    if start > 0:
        pre += ["-ss", f"{start:.3f}"]
    pre += ["-t", f"{frames / fps + 0.5 / fps:.3f}"]
    vf = strip_filter(start, fps, frames, int(cols), rows, int(width), cell=cell, pad=pad)
    vf_plain = strip_filter(start, fps, frames, int(cols), rows, int(width), with_font=None,
                            cell=cell, pad=pad)
    return _run(pre, video, vf, vf_plain, out)


def frame(video: Path | str, t: float, out: Path | str) -> Path:
    """One still at clip time `t` (png keeps full quality; jpg is fine for a quick look)."""
    video, out = Path(video), Path(out)
    t = float(t)
    pre = ["-ss", f"{t:.3f}"] if t > 0 else []
    return _run(pre, video, "null", None, out)


# tools.panel (the critic packet) calls these names with its own layout: portrait cells that
# line up whatever the source aspect, 0-3 s inclusive at 0.25 s in 7x2, 2 px gutters.
def make_sheet(video: Path | str, out: Path | str, every: float = DEFAULT_EVERY_S,
               cols: int = DEFAULT_COLS, cell: tuple[int, int] | None = PANEL_SHEET_CELL,
               pad: int = PANEL_PAD) -> Path:
    return sheet(video, out, every=every, cols=cols, width=cell[0] if cell else DEFAULT_WIDTH,
                 cell=cell, pad=pad)


def make_strip(video: Path | str, out: Path | str, start: float = STRIP_START_S,
               end: float = PANEL_STRIP_END_S, step: float = PANEL_STRIP_STEP_S,
               cols: int = PANEL_STRIP_COLS, cell: tuple[int, int] | None = PANEL_STRIP_CELL,
               pad: int = PANEL_PAD) -> Path:
    return strip(video, out, start=start, end=end, fps=1.0 / float(step), cols=cols,
                 width=cell[0] if cell else DEFAULT_WIDTH, cell=cell, pad=pad, inclusive_end=True)


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Time-stamped contact sheet / opening strip / single frame")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sheet", help="one frame every N seconds, tiled")
    s.add_argument("video")
    s.add_argument("--out", required=True)
    s.add_argument("--every", type=float, default=DEFAULT_EVERY_S)
    s.add_argument("--cols", type=int, default=DEFAULT_COLS)
    s.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    s.add_argument("--json", action="store_true")

    t = sub.add_parser("strip", help="the opening seconds at a high frame rate, tiled")
    t.add_argument("video")
    t.add_argument("--out", required=True)
    t.add_argument("--start", type=float, default=STRIP_START_S)
    t.add_argument("--end", type=float, default=STRIP_END_S)
    t.add_argument("--fps", type=float, default=STRIP_FPS)
    t.add_argument("--cols", type=int, default=STRIP_COLS)
    t.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    t.add_argument("--json", action="store_true")

    f = sub.add_parser("frame", help="one still at a time")
    f.add_argument("video")
    f.add_argument("--t", type=float, required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "sheet":
            out = sheet(a.video, a.out, every=a.every, cols=a.cols, width=a.width)
        elif a.cmd == "strip":
            out = strip(a.video, a.out, start=a.start, end=a.end, fps=a.fps, cols=a.cols, width=a.width)
        else:
            out = frame(a.video, a.t, a.out)
    except (ContactSheetError, safeio.SafeIOError, FileNotFoundError, ValueError, OSError) as e:
        if a.json:
            print(json.dumps({"error": str(e), "cmd": a.cmd}))
        else:
            print(f"contact_sheet {a.cmd}: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"cmd": a.cmd, "video": str(a.video), "out": str(out),
                          "bytes": out.stat().st_size}))
    else:
        print(f"{a.cmd}: {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
