"""Tests for tools/contact_sheet.py: sheet / strip / frame outputs, tile geometry, overwrite rules."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def clip(engine_root, clip_factory):
    return clip_factory("sheet_src.mp4", seconds=5.0, w=320, h=180, audio="none")


def _size(p: Path):
    from PIL import Image
    with Image.open(p) as im:
        im.load()
        return im.size


def test_sheet_geometry(clip, engine_root):
    from tools import contact_sheet as cs
    out = engine_root / "cache" / "sheets" / "sheet.jpg"
    assert cs.sheet(clip, out, every=2.0, cols=5, width=320) == out
    assert out.is_file() and out.stat().st_size > 0
    w, h = _size(out)
    assert w == 5 * 320                 # 5 s / 2 s -> 3 frames (0,2,4) in one row of 5
    assert h == 180
    out2 = engine_root / "cache" / "sheets" / "sheet2.jpg"
    cs.sheet(clip, out2, every=1.0, cols=3, width=160)
    w2, h2 = _size(out2)
    assert w2 == 3 * 160                # 6 frames (0..5) -> 2 rows of 3
    assert h2 == 2 * 90


def test_sheet_on_short_hlg_clip(engine_root, clip_factory):
    from tools import contact_sheet as cs
    short = clip_factory("short_hlg.mov", seconds=0.5, w=160, h=90, hdr=True, audio="none")
    out = engine_root / "cache" / "sheets" / "short.jpg"
    cs.sheet(short, out, every=2.0, cols=5, width=160)
    assert _size(out) == (5 * 160, 90)


def test_strip_has_16_frames_in_two_rows(clip, engine_root):
    from tools import contact_sheet as cs
    out = engine_root / "cache" / "sheets" / "strip.jpg"
    cs.strip(clip, out, start=0.0, end=4.0, fps=4, cols=8)
    w, h = _size(out)
    assert w == 8 * 320 and h == 2 * 180
    later = engine_root / "cache" / "sheets" / "strip_late.jpg"
    cs.strip(clip, later, start=1.0, end=2.0, fps=4, cols=4, width=160)
    assert _size(later) == (4 * 160, 90)


def test_frame_png_and_jpg(clip, engine_root):
    from tools import contact_sheet as cs
    png = engine_root / "cache" / "frames" / "f.png"
    cs.frame(clip, 0.03, png)
    assert _size(png) == (320, 180)
    jpg = engine_root / "cache" / "frames" / "f.jpg"
    cs.frame(clip, 3.5, jpg)
    assert _size(jpg) == (320, 180)


def test_overwrite_only_under_cache(clip, engine_root):
    from tools import contact_sheet as cs, safeio
    cached = engine_root / "cache" / "sheets" / "again.jpg"
    cs.sheet(clip, cached)
    first = cached.stat().st_mtime_ns
    cs.sheet(clip, cached)              # allowed: regenerated in place
    assert cached.is_file()
    assert cached.stat().st_mtime_ns >= first
    kept = engine_root / "runs" / "keep.jpg"
    cs.frame(clip, 0.0, kept)
    with pytest.raises(safeio.SafeIOError):
        cs.frame(clip, 0.0, kept)
    with pytest.raises(safeio.SafeIOError):
        cs.sheet(clip, clip.parent / "outside.jpg")      # outside the engine roots


def test_filters_and_font(monkeypatch, tmp_path):
    from tools import contact_sheet as cs
    f = cs.timestamp_filter()
    assert "%{pts\\:hms}" in f and "box=1" in f and f.startswith("drawtext=")
    fake_font = tmp_path / "arial.ttf"
    fake_font.write_bytes(b"x")
    monkeypatch.setenv("CLAUDINK_FONT", str(fake_font))
    assert cs.font_file() == str(fake_font)
    assert "fontfile='" in cs.timestamp_filter()
    assert cs._escape_path(r"C:\Windows\Fonts\arial.ttf") == "C\\:/Windows/Fonts/arial.ttf"
    monkeypatch.delenv("CLAUDINK_FONT")
    monkeypatch.setattr(cs, "FONT_CANDIDATES", ())
    assert cs.font_file() is None
    assert "fontfile" not in cs.timestamp_filter()
    assert cs.sheet_filter(2.0, 5, 2, 320).endswith("tile=5x2")
    assert "fontsize=17:" in cs.sheet_filter(2.0, 5, 2, 160)
    assert "fontsize=28:" in cs.sheet_filter(2.0, 5, 2, 640)
    assert cs.stamp_size(50) == 10 and cs.stamp_size(180) == 20
    assert "drawtext" not in cs.sheet_filter(2.0, 5, 1, 320, with_font=None)
    assert "padding=2:margin=2" in cs.sheet_filter(2.0, 5, 1, 320, cell=(216, 384), pad=2)
    assert "force_original_aspect_ratio=decrease,pad=216:384" in cs.cell_filter(320, (216, 384))
    assert "fps=4" in cs.strip_filter(0.0, 4, 16, 8, 2, 320)
    assert "setpts=PTS+1.500/TB" in cs.strip_filter(1.5, 4, 4, 4, 1, 160)


def test_bad_args_and_missing_input(engine_root, clip):
    from tools import contact_sheet as cs
    out = engine_root / "cache" / "x.jpg"
    with pytest.raises(ValueError):
        cs.sheet(clip, out, every=0)
    with pytest.raises(ValueError):
        cs.strip(clip, out, start=2.0, end=1.0)
    with pytest.raises(FileNotFoundError):
        cs.frame(clip.with_name("missing.mp4"), 0.0, out)
    with pytest.raises(cs.ContactSheetError):
        bad = clip.with_name("broken.mp4")
        bad.write_bytes(b"nope")
        cs.frame(bad, 0.0, out)


def test_panel_aliases_match_the_critic_packet_layout(engine_root, clip_factory):
    """tools.panel calls make_strip(video, out, start, end, step) / make_sheet(video, out, every)
    and expects 13 frames (0-3 s inclusive) in 7x2 letterboxed 180x320 cells with 2 px gutters,
    and 216x384 sheet cells 5 per row, whatever the source aspect."""
    from tools import contact_sheet as cs
    portrait = clip_factory("portrait.mp4", seconds=7.0, w=720, h=1280, audio="none")
    s = engine_root / "cache" / "p" / "strip_0_3s.jpg"
    cs.make_strip(portrait, s, start=0.0, end=3.0, step=0.25)
    assert _size(s) == (7 * 180 + 6 * 2 + 2 * 2, 2 * 320 + 2 + 2 * 2)
    sh = engine_root / "cache" / "p" / "sheet_2s.jpg"
    cs.make_sheet(portrait, sh, every=2.0)       # 7 s -> frames 0,2,4,6 -> one row of 5
    assert _size(sh) == (5 * 216 + 4 * 2 + 2 * 2, 384 + 2 * 2)
    land = engine_root / "cache" / "p" / "land.jpg"
    cs.make_sheet(clip_factory("land.mp4", seconds=1.0, w=320, h=180, audio="none"), land)
    assert _size(land) == (5 * 216 + 4 * 2 + 2 * 2, 384 + 2 * 2)   # landscape letterboxed, same box


def test_fixed_cells_and_inclusive_end(clip, engine_root):
    from tools import contact_sheet as cs
    out = engine_root / "cache" / "c" / "cells.jpg"
    cs.strip(clip, out, start=0.0, end=2.0, fps=2, cols=5, cell=(100, 100), inclusive_end=True)
    assert _size(out) == (5 * 100, 100)                              # 5 frames: 0,0.5,...,2.0
    assert cs.sheet_layout(7.0, 2.0, 5) == (4, 1)
    assert cs.sheet_layout(0.0, 2.0, 5) == (1, 1)
    assert cs.sheet_layout(10.0, 2.0, 5) == (6, 2)


def test_cli(clip, engine_root, capsys):
    from tools import contact_sheet as cs
    out = engine_root / "cache" / "cli" / "s.jpg"
    assert cs.main(["sheet", str(clip), "--out", str(out), "--every", "2", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["cmd"] == "sheet" and Path(d["out"]) == out and d["bytes"] > 0
    strip = engine_root / "cache" / "cli" / "strip.jpg"
    assert cs.main(["strip", str(clip), "--out", str(strip)]) == 0
    assert "strip:" in capsys.readouterr().out
    fr = engine_root / "cache" / "cli" / "f.png"
    assert cs.main(["frame", str(clip), "--t", "0.03", "--out", str(fr)]) == 0
    capsys.readouterr()
    kept = engine_root / "runs" / "kept.png"
    assert cs.main(["frame", str(clip), "--t", "0.03", "--out", str(kept)]) == 0
    capsys.readouterr()
    assert cs.main(["frame", str(clip), "--t", "0.03", "--out", str(kept), "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
