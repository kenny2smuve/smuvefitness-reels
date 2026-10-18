"""Tests for tools/fingerprint.py (phash signatures, attribution, frame-1 reuse, store)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from tools.tests.conftest import FFMPEG, make_clip

pytestmark = pytest.mark.slow


def _reencode(src: Path, dst: Path, vf: str | None, crf: int) -> Path:
    """Test-only re-encode (plain ffmpeg; the module under test never uses -y)."""
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-an", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dst


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    """One landscape 6 s testsrc2 clip and three derived versions, built once per module."""
    d = tmp_path_factory.mktemp("fpclips")
    orig = make_clip(d / "orig.mp4", seconds=6, w=1280, h=720, audio="none")
    # letterboxed 1080x1920 at another CRF: the "full-bleed vs letterboxed line up" case
    letterboxed = _reencode(orig, d / "letterboxed.mp4",
                            "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2", 34)
    # his version: zoomed 1.2x into the letterbox with a hook text band at the top
    zoomed = _reencode(letterboxed, d / "zoomed.mp4",
                       "crop=900:1600,scale=1080:1920,"
                       "drawtext=text='WOULD YOU SIT 7 HOURS':fontsize=90:fontcolor=white:"
                       "x=(w-tw)/2:y=120:box=1:boxcolor=black@0.7", 30)
    bars = make_clip(d / "bars.mp4", seconds=6, w=1080, h=1920, pattern="smptebars", audio="none")
    return {"orig": orig, "letterboxed": letterboxed, "zoomed": zoomed, "bars": bars}


@pytest.fixture(scope="module")
def sigs(tmp_path_factory, clips):
    """Signatures of the four clips, computed once per module under a throwaway engine root."""
    from tools.tests.conftest import _reload_config
    root = tmp_path_factory.mktemp("fpengine")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CLAUDINK_ENGINE_ROOT", str(root))
        c = _reload_config()
        c.ensure_engine_dirs()
        from tools import fingerprint as fp
        out = {k: fp.signature(v) for k, v in clips.items()}
    _reload_config()
    return out


def test_frame_times_are_centred_fractions():
    from tools import fingerprint as fp
    assert fp.frame_times(9.0, 9) == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5]
    assert fp.frame_times(0.0) == []
    assert len(fp.frame_times(45.0)) == 9


def test_extract_frame_pads_to_canvas(engine_root, clips):
    from tools import fingerprint as fp
    img = fp.extract_frame(clips["orig"], 1.0)
    assert img.size == (360, 640)
    # landscape picture sits in the middle band: top and bottom rows are the black pad
    assert img.getpixel((180, 5)) == (0, 0, 0) and img.getpixel((180, 634)) == (0, 0, 0)
    assert img.getpixel((180, 320)) != (0, 0, 0)
    assert any((engine_root / "cache" / "fingerprint").glob("*.png"))


def test_mask_and_crop_shapes(engine_root, clips):
    from tools import fingerprint as fp
    img = fp.extract_frame(clips["letterboxed"], 2.0)
    masked = fp.mask_text_bands(img)
    assert masked.size == img.size
    assert masked.getpixel((10, 10)) == fp.GREY and masked.getpixel((10, 639)) == fp.GREY
    assert masked.getpixel((180, 320)) == img.getpixel((180, 320))
    assert fp.center_crop(img, 1.33).size == (360, 640)
    assert fp.center_crop(img, 1.0).tobytes() == img.tobytes()


def test_signature_shape(sigs):
    s = sigs["orig"]
    assert 5.5 < s["duration"] < 6.5
    assert len(s["frames"]) == 9
    assert set(s["frames"][0]["hashes"]) == {"1.0", "1.15", "1.33"}
    assert all(len(h) == 16 for h in s["frames"][0]["hashes"].values())
    assert len(s["frame1"]) == 16 and s["frame1"] == s["frame1_hashes"]["1.0"]


def test_letterboxed_reencode_matches(sigs):
    from tools import fingerprint as fp
    d = fp.distance(sigs["orig"], sigs["letterboxed"])
    assert d["median"] <= 6, d
    assert fp.match(sigs["orig"], sigs["letterboxed"])


def test_zoomed_with_text_band_matches(sigs):
    from tools import fingerprint as fp
    d = fp.distance(sigs["letterboxed"], sigs["zoomed"])
    assert d["median"] <= 8, d
    assert fp.match(sigs["letterboxed"], sigs["zoomed"])
    assert fp.match(sigs["orig"], sigs["zoomed"])


def test_bars_do_not_match(sigs):
    from tools import fingerprint as fp
    d = fp.distance(sigs["orig"], sigs["bars"])
    assert d["median"] > 20, d
    assert not fp.match(sigs["orig"], sigs["bars"])


def test_label_claude_claudin_unsure(sigs):
    from tools import fingerprint as fp
    deliveries = {"A_1100_x": sigs["orig"], "B_1430_y": sigs["bars"]}
    lab = fp.label(sigs["zoomed"], deliveries)
    assert lab["label"] == "CLAUDE" and lab["best_id"] == "A_1100_x"
    lab = fp.label(sigs["bars"], {"A_1100_x": sigs["orig"]})
    assert lab["label"] == "CLAUDIN"
    assert fp.label(sigs["bars"], {}) == {"label": "UNSURE", "best_id": None, "median": None}
    # a median between the two thresholds is UNSURE
    sig_a = {"frames": [{"t": 0, "hashes": {"1.0": "0" * 16}}], "frame1": "0" * 16}
    sig_b = {"frames": [{"t": 0, "hashes": {"1.0": "0" * 12 + "ffff"}}], "frame1": "0" * 16}
    assert fp.distance(sig_a, sig_b) == {"median": 16.0, "per_frame": [16], "frame1": 0}
    assert fp.label(sig_a, {"d": sig_b})["label"] == "UNSURE"


def test_store_round_trip_and_frame1(engine_root, clips, sigs):
    from tools import config, fingerprint as fp
    now = datetime(2026, 10, 19, 6, 52, 10, tzinfo=config.TZ)
    e = fp.add_signature("DeXXposted", clips["letterboxed"], "posted", now=now, sig=sigs["letterboxed"])
    assert e["added_et"] == "2026-10-19T06:52:10-04:00"
    fp.add_signature("A_1100_orig", clips["orig"], "delivery", now=now, sig=sigs["orig"])
    with pytest.raises(ValueError):
        fp.add_signature("bad", clips["orig"], "whatever", now=now, sig=sigs["orig"])
    p = engine_root / "data" / "signatures.json"
    assert p.exists()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw["signatures"]) == {"DeXXposted", "A_1100_orig"}
    store = fp.load_store()
    assert store["signatures"]["DeXXposted"]["kind"] == "posted"
    assert store["signatures"]["DeXXposted"]["sig"]["frame1"] == sigs["letterboxed"]["frame1"]

    # identical clip: frame-1 distance ~0; bars: far away
    assert fp.frame1_distance(clips["letterboxed"], store, sig=sigs["letterboxed"]) == 0
    assert fp.frame1_report(clips["letterboxed"], store, sig=sigs["letterboxed"])["reused"] is True
    assert fp.frame1_distance(clips["bars"], store, sig=sigs["bars"]) > 12
    assert fp.frame1_distance(clips["bars"], {"signatures": {}}, sig=sigs["bars"]) is None

    rows = fp.find_matches(clips["zoomed"], ("posted", "delivery"), store, sig=sigs["zoomed"])
    assert len(rows) == 2 and rows[0]["match"] is True and rows[1]["match"] is True
    assert all(r["kind"] in ("posted", "delivery") for r in rows)
    rows = fp.find_matches(clips["zoomed"], ("posted",), store, sig=sigs["zoomed"])
    assert [r["key"] for r in rows] == ["DeXXposted"]


def test_numpy_fallback_equals_imagehash(engine_root, clips):
    imagehash = pytest.importorskip("imagehash")
    import numpy as np
    from PIL import Image
    from tools import fingerprint as fp

    rng = np.random.default_rng(7)
    imgs = [Image.fromarray(rng.integers(0, 256, size=(640, 360, 3), dtype=np.uint8)) for _ in range(5)]
    imgs.append(Image.new("RGB", (360, 640), (0, 0, 0)))               # black frame
    imgs.append(Image.new("RGB", (360, 640), (200, 30, 30)))           # flat colour
    frame = fp.extract_frame(clips["letterboxed"], 3.0)
    imgs += [frame, fp.mask_text_bands(fp.center_crop(frame, 1.33))]
    for im in imgs:
        assert fp.phash_numpy(im) == str(imagehash.phash(im))
    assert fp.hamming("0000000000000000", "ffffffffffffffff") == 64


def test_cli(engine_root, clips, capsys):
    from tools import fingerprint as fp
    assert fp.main(["sign", str(clips["letterboxed"]), "--key", "DeXX", "--kind", "posted"]) == 0
    assert "signed DeXX" in capsys.readouterr().out
    assert fp.main(["--json", "match", str(clips["zoomed"]), "--kinds", "posted"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["key"] == "DeXX" and rows[0]["match"] is True
    assert fp.main(["frame1", str(clips["letterboxed"])]) == 1          # reused frame 1
    assert "REUSED" in capsys.readouterr().out
    assert fp.main(["--json", "frame1", str(engine_root / "missing.mp4")]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
