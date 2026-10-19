"""Tests for tools/fingerprint.py (phash signatures, attribution, frame-1 reuse, store)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from tools.tests.conftest import FFMPEG, make_clip

pytestmark = pytest.mark.slow


def _has_encoder(name: str) -> bool:
    r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True)
    return name in (r.stdout or "")


def _reencode(src: Path, dst: Path, vf: str | None, crf: int) -> Path:
    """Test-only re-encode (plain ffmpeg; the module under test never uses -y)."""
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-an", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return dst


HOOK_TEXT = ("drawtext=text='WOULD YOU SIT 7 HOURS':fontsize=60:fontcolor=white:"
             "x=(w-tw)/2:y=90:box=1:boxcolor=black@0.7")


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
def hlg_clips(tmp_path_factory):
    """A letterboxed HLG master (Meta glasses style) and his re-upload: the engine's tonemap to
    SDR, zoomed 1.2x, hook text burned in, 1080p-ish H.264 (what Instagram serves back)."""
    if not _has_encoder("libx265"):
        pytest.skip("libx265 missing: cannot build an HLG fixture")
    from tools import fingerprint as fp
    d = tmp_path_factory.mktemp("fphlg")
    master = make_clip(d / "hlg_master.mp4", seconds=3, w=720, h=1280, audio="none", hdr=True,
                       extra_vf="scale=720:405,pad=720:1280:(ow-iw)/2:(oh-ih)/2")
    reupload = _reencode(master, d / "hlg_reupload_sdr.mp4",
                         fp.tonemap_chain() + ",crop=600:1067,scale=720:1280," + HOOK_TEXT, 30)
    return {"master": master, "reupload": reupload}


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


def test_hlg_master_matches_its_tonemapped_reupload(engine_root, hlg_clips):
    """W16 on his real footage: the HLG master is hashed through the same tonemap Instagram's
    SDR rendition went through, so the zoomed, captioned re-upload still reads CLAUDE."""
    from tools import fingerprint as fp
    assert fp.probe_color_transfer(hlg_clips["master"]) == "arib-std-b67"
    assert fp.probe_color_transfer(hlg_clips["reupload"]) == "bt709"
    assert fp.frame_filter(transfer="arib-std-b67").startswith("zscale=")
    assert not fp.frame_filter(transfer="bt709").startswith("zscale=")
    a, b = fp.signature(hlg_clips["master"]), fp.signature(hlg_clips["reupload"])
    assert a["transfer"] == "arib-std-b67" and b["transfer"] == "bt709"
    d = fp.distance(a, b)
    assert d["median"] <= 8, d
    assert fp.label(b, {"A_1100_hlg": a})["label"] == "CLAUDE"
    # frame1_phash is the name gates.py probes and must equal the signature's frame1
    assert fp.frame1_phash(hlg_clips["master"]) == a["frame1"]


def test_frame1_phash_equals_signature_frame1(engine_root, clips, sigs):
    from tools import fingerprint as fp
    assert fp.frame1_phash(clips["letterboxed"]) == sigs["letterboxed"]["frame1"]
    assert len(fp.frame1_phash(clips["orig"])) == 16


def test_unknown_duration_raises(engine_root, clips, monkeypatch):
    from tools import fingerprint as fp
    monkeypatch.setattr(fp, "probe_duration", lambda video: 0.0)
    with pytest.raises(RuntimeError, match="unknown duration"):
        fp.signature(clips["orig"])
    # the stream duration is the fallback when the container has none
    info = fp.probe_video(clips["orig"])
    assert 5.5 < info["duration"] < 6.5 and info["color_transfer"] == "bt709"


def test_distance_validates_signature_shape(sigs):
    from tools import fingerprint as fp
    entry = {"kind": "posted", "video": "x.mp4", "sig": sigs["letterboxed"]}
    # a store entry is unwrapped, so gates/deliver callers cannot trip on it
    assert fp.distance(entry, sigs["orig"])["median"] == fp.distance(sigs["letterboxed"], sigs["orig"])["median"]
    with pytest.raises(ValueError, match="not a signature"):
        fp.distance({"frame1": "0" * 16}, sigs["orig"])
    with pytest.raises(ValueError, match="not a signature"):
        fp.distance({"frames": [], "frame1": "0" * 16}, sigs["orig"])      # the silent-64 case
    with pytest.raises(ValueError):
        fp.distance(sigs["orig"], {"kind": "posted"})
    with pytest.raises(ValueError):
        fp.label({"frames": []}, {"a": sigs["orig"]})
    with pytest.raises(ValueError):
        fp.frame1_pair_distance(sigs["orig"], {"frame1": "0" * 16})


def test_extract_frame_on_a_text_file_raises(engine_root):
    from tools import fingerprint as fp
    bad = engine_root / "cache" / "not_a_video.mp4"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(RuntimeError):
        fp.extract_frame(bad, 0.0, transfer="")
    with pytest.raises(FileNotFoundError):
        fp.extract_frame(engine_root / "missing.mp4", 0.0)


def test_corrupt_store_raises_not_silently_replaced(engine_root):
    from tools import fingerprint as fp
    p = engine_root / "data" / "signatures.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        fp.load_store()
    p.write_text(json.dumps({"signatures": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="signatures"):
        fp.load_store()
    p.unlink()
    assert fp.load_store() == {"version": 1, "signatures": {}}


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
    # a naive now is taken as ET and still written with its offset (DST has ended on Nov 2)
    e2 = fp.add_signature("naive", clips["orig"], "delivery", now=datetime(2026, 11, 3, 2, 30), sig=sigs["orig"])
    assert e2["added_et"] == "2026-11-03T02:30:00-05:00"
    with pytest.raises(ValueError):
        fp.add_signature("badsig", clips["orig"], "delivery", now=now, sig={"frames": []})
    with pytest.raises(ValueError):
        fp.add_signature("bad", clips["orig"], "whatever", now=now, sig=sigs["orig"])
    p = engine_root / "data" / "signatures.json"
    assert p.exists()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw["signatures"]) == {"DeXXposted", "A_1100_orig", "naive"}
    store = fp.load_store()
    assert store["signatures"]["DeXXposted"]["kind"] == "posted"
    assert store["signatures"]["DeXXposted"]["sig"]["frame1"] == sigs["letterboxed"]["frame1"]

    # identical clip: frame-1 distance ~0; bars: far away
    assert fp.frame1_distance(clips["letterboxed"], store, sig=sigs["letterboxed"]) == 0
    assert fp.frame1_report(clips["letterboxed"], store, sig=sigs["letterboxed"])["reused"] is True
    assert fp.frame1_distance(clips["bars"], store, sig=sigs["bars"]) > 12
    assert fp.frame1_distance(clips["bars"], {"signatures": {}}, sig=sigs["bars"]) is None

    rows = fp.find_matches(clips["zoomed"], ("posted", "delivery"), store, sig=sigs["zoomed"])
    assert len(rows) == 3 and all(r["match"] is True for r in rows)
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
    # --json works after the subcommand too (the docstring's position)
    assert fp.main(["frame1", str(clips["letterboxed"]), "--json"]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["reused"] is True and rep["key"] == "DeXX"
    assert fp.main(["match", str(clips["zoomed"]), "--kinds", "posted", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["key"] == "DeXX"
    alt = engine_root / "state" / "alt_store.json"
    assert fp.main(["sign", str(clips["orig"]), "--key", "K2", "--kind", "delivery", "--store", str(alt)]) == 0
    capsys.readouterr()
    assert set(json.loads(alt.read_text(encoding="utf-8"))["signatures"]) == {"K2"}
    assert fp.main(["--json", "frame1", str(engine_root / "missing.mp4")]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
