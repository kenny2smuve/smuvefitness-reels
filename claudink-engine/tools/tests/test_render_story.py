"""Tests for tools.render_story: preview + full render of a 3-clip EDL, transfer policy (and the
--transfer force), Canon range handling, silent-dialogue warnings, join fallback, sidecars, chat copy."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.tests.conftest import FFMPEG, make_clip

import tools.encode  # noqa: F401  (render engine: present in the kit)


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,pix_fmt,color_transfer,color_primaries,"
                          "sample_rate,channels:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = [s for s in d["streams"] if s["codec_type"] == "audio"]
    return v, a, float(d["format"]["duration"])


def make_canon_clip(path: Path, seconds: float = 2.0, tagged: str | None = None) -> Path:
    """A Canon-style portrait clip. tagged="pc": the stream is TAGGED full range (color_range=pc);
    tagged=None: no colour/range tags at all (ffprobe reports unknown), like many Canon MOVs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".norot.mp4")
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}"]
    if tagged == "pc":
        cmd += ["-vf", "scale=out_range=full,format=yuv420p", "-color_range", "pc"]
    else:
        cmd += ["-vf", "format=yuv420p"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-an", str(tmp)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-display_rotation", "90",
                    "-i", str(tmp), "-c", "copy", str(path)], check=True, capture_output=True, text=True)
    tmp.unlink()
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("story_clips")
    a = make_clip(d / "video-001.mp4", seconds=3.0, audio="speech", hdr=True,
                  quicktime_creationdate="2026-10-11T14:00:00-0400")
    b = make_clip(d / "video-002.mp4", seconds=3.0, audio="speech", hdr=True,
                  quicktime_creationdate="2026-10-11T14:05:00-0400")
    c = make_clip(d / "MVI_0001.mp4", seconds=2.0, audio="sine", rotate=90)
    return a, b, c


@pytest.fixture(scope="module")
def odd_clips(tmp_path_factory):
    """A tagged-full-range Canon clip, an untagged Canon clip, a dialogue clip without audio."""
    d = tmp_path_factory.mktemp("story_odd")
    pc = make_canon_clip(d / "MVI_0009.mp4", tagged="pc")
    untagged = make_canon_clip(d / "MVI_0010.mp4", tagged=None)
    mute = make_clip(d / "video-003.mp4", seconds=2.0, audio="none", hdr=True)
    return pc, untagged, mute


def _fast_profile(**kw):
    from tools import encode
    return encode.Profile(**{"libx265": True, "libx264": True, "chosen": "cpu", "cpu_workers": 2,
                             "cpu_preset": "ultrafast", **kw})


def _story(a, b, c):
    from tools import edl
    return edl.EDL.from_dict({
        "name": "fixture_story", "type": "story", "session": "Fixture", "multi_day": False,
        "canvas": {"w": 1440, "h": 2560},
        "segs": [
            {"clip": str(a), "in": 0.2, "out": 1.4, "kind": "I", "order_key": "2026-10-11T14:00:00-04:00",
             "tags": ["greeting"], "note": "hi"},
            {"clip": str(b), "in": 0.5, "out": 1.7, "kind": "I", "order_key": "2026-10-11T14:05:00-04:00",
             "tags": ["needle"], "note": "buzz"},
            {"clip": str(c), "in": 0.1, "out": 1.1, "kind": "C", "order_key": "2026-10-11T15:00:00-04:00",
             "tags": ["finished"], "note": "reveal"},
        ],
        "hooks": ["Would you sit 7 hours for this?", "7 hours. One session. Worth it?"],
        "caption": "worth it?", "pieces": ["Shoulder"], "labels": {}, "audio_from": None,
    })


def test_preview_render_into_cache(engine_root, clips):
    from tools import config, render_story
    a, b, c = clips
    e = _story(a, b, c)
    out = config.POST / "2026-10-19" / "A_1100_fixture.mp4"
    res = render_story.render(e, out, preview=True, profile=_fast_profile(), stage=False)
    p = Path(res["out"])
    assert p == config.CACHE / "previews" / "A_1100_fixture_preview.mp4" and p.exists()
    v, aud, dur = _probe(p)
    assert (v["width"], v["height"]) == (360, 640) and v["codec_name"] == "h264" and aud
    assert abs(dur - 3.4) < 0.15 and res["transfer"] == "sdr" and "chat" not in res
    assert res["warnings"] == [] and all(s["audio"] == "own" for s in res["segments"])
    # the fixture MVI is TAGGED tv (make_clip writes bt709 tags): trusted, no range conversion
    assert res["segments"][2]["range_assumed"] == "" and res["segments"][2]["range_filter"] == ""
    assert res["segments"][0]["range_filter"] == "" and res["segments"][0]["kind"] == "I"
    assert "range_assumed" not in res["export"]
    assert not out.exists()
    # previews may be re-rendered in place
    res2 = render_story.render(e, out, preview=True, profile=_fast_profile(), stage=False)
    assert Path(res2["out"]) == p and all(s["cached"] for s in res2["segments"])


@pytest.mark.slow
def test_full_render_sdr_because_of_canon_seg(engine_root, clips):
    from tools import config, edl, render_story
    from datetime import datetime
    a, b, c = clips
    e = _story(a, b, c)
    out = config.POST / "2026-10-19" / "A_1100_fixture.mp4"
    now = datetime(2026, 10, 19, 6, 52, 10, tzinfo=config.TZ)
    res = render_story.render(e, out, profile=_fast_profile(), now=now)
    assert Path(res["out"]) == out and out.exists()
    v, aud, dur = _probe(out)
    assert (v["width"], v["height"]) == (1440, 2560) and v["codec_name"] == "hevc"
    assert v["color_transfer"] == "bt709" and v["pix_fmt"] == "yuv420p"
    assert aud and aud[0]["codec_name"] == "aac" and aud[0]["sample_rate"] == "48000" and aud[0]["channels"] == 2
    assert abs(dur - 3.4) < 0.15 and res["join"]["ok"] and res["join"]["mode"] == "copy"
    assert res["decode_errors"] == 0 and res["x_realtime"]
    # clips were staged into cache/clips once
    assert len(list((config.CACHE / "clips").glob("*.mp4"))) == 3
    # chat copy <= 25 MB, decodes, same transfer
    chat = Path(res["chat"]["path"])
    assert chat == out.with_name("A_1100_fixture_chat.mp4") and chat.stat().st_size <= config.CHAT_COPY_MAX_BYTES
    assert res["chat"]["ok"] and res["chat"]["facts"]["transfer"] == "bt709"
    # sidecars
    side = edl.load(out.with_name("A_1100_fixture.edl.json"))
    assert side.export["transfer"] == "sdr" and "Canon" in side.export["reason"]
    assert side.export.get("forced") is None and "START_HERE 12" in side.export["policy_note"]
    assert side.export["rendered_et"] == "2026-10-19T06:52:10-04:00" and side.export["chat"] == str(chat)
    assert side.export["warnings"] == [] and "range_assumed" not in side.export
    rj = json.loads(out.with_name("A_1100_fixture.render.json").read_text(encoding="utf-8"))
    assert rj["master"]["width"] == 1440 and rj["master"]["codec"] == "hevc" and rj["encoder"] == "libx265"
    # a master is never overwritten
    with pytest.raises(Exception):
        render_story.render(e, out, profile=_fast_profile())


@pytest.mark.slow
def test_full_render_hlg_only_keeps_hlg(engine_root, clips):
    from tools import config, edl, render_story
    a, b, _ = clips
    e = edl.EDL.from_dict({
        "name": "hlg_only", "type": "story", "session": "Fixture", "multi_day": False,
        "canvas": {"w": 1440, "h": 2560},
        "segs": [{"clip": str(a), "in": 0.0, "out": 0.8, "kind": "I", "order_key": "d01-s01", "tags": []},
                 {"clip": str(b), "in": 1.0, "out": 1.8, "kind": "M", "order_key": "d01-s02", "tags": ["needle"]}],
        "hooks": ["h1", "h2"], "caption": "", "audio_from": None})
    out = config.HOLD / "hlg_only.mp4"
    res = render_story.render(e, out, profile=_fast_profile(), stage=False)
    v, aud, dur = _probe(out)
    assert v["color_transfer"] == "arib-std-b67" and v["color_primaries"] == "bt2020"
    assert v["pix_fmt"] == "yuv420p10le" and v["codec_name"] == "hevc" and aud
    assert (v["width"], v["height"]) == (1440, 2560) and abs(dur - 1.6) < 0.15
    assert res["transfer"] == "hlg" and res["chat"]["ok"] and res["chat"]["facts"]["transfer"] == "arib-std-b67"
    assert out.with_name("hlg_only.edl.json").exists() and out.with_name("hlg_only.render.json").exists()
    assert "policy_note" not in res["export"]


def test_forced_hlg_on_mixed_edl_is_honoured_and_recorded(engine_root, clips):
    from tools import config, edl, render_story
    a, b, c = clips
    e = _story(a, b, c)
    out = config.POST / "2026-10-19" / "A_1100_forced.mp4"
    res = render_story.render(e, out, preview=True, profile=_fast_profile(), stage=False, transfer="hlg")
    assert res["transfer"] == "hlg" and res["export"]["forced"] is True and "forced" in res["export"]["reason"]
    assert Path(res["out"]).exists() and abs(res["duration"] - 3.4) < 0.15
    side = edl.load(Path(res["out"]).with_name("A_1100_forced_preview.edl.json"))
    assert side.export["forced"] is True and side.export["transfer"] == "hlg"
    assert side.export["transfer_decided"] == "hlg" and "START_HERE 12" in side.export["policy_note"]
    # the sidecar's force survives a re-render with --transfer auto (it is the author's call now) ...
    res2 = render_story.render(side, out, preview=True, profile=_fast_profile(), stage=False)
    assert res2["transfer"] == "hlg"
    # ... while a bare recorded transfer does not
    bare = _story(a, b, c)
    bare.export["transfer"] = "hlg"
    res3 = render_story.render(bare, out, preview=True, profile=_fast_profile(), stage=False)
    assert res3["transfer"] == "sdr" and res3["export"].get("forced") is None
    # forcing sdr on the same mixed EDL
    res4 = render_story.render(e, out, preview=True, profile=_fast_profile(), stage=False, transfer="sdr")
    assert res4["transfer"] == "sdr" and res4["export"]["forced"] is True


def test_pc_range_canon_and_silent_dialogue_segment(engine_root, clips, odd_clips):
    from tools import config, edl, render_story
    a, _b, _c = clips
    pc, untagged, mute = odd_clips
    # unit: the one range implementation both renderers use
    rf = render_story.range_filter
    assert rf({"is_hlg": False, "range": "pc"}, "x.mp4") == ("scale=in_range=full:out_range=limited", "full (tagged pc)")
    assert rf({"is_hlg": False, "range": ""}, "MVI_0003.MOV") == ("scale=in_range=full:out_range=limited", "full (canon)")
    assert rf({"is_hlg": False, "range": "unknown"}, "mvi_0003.mp4")[1] == "full (canon)"
    assert rf({"is_hlg": False, "range": "tv"}, "MVI_0003.mp4") == ("", "")        # a tagged Canon is trusted
    assert rf({"is_hlg": False, "range": ""}, "IMG_0003.mp4") == ("", "")
    assert rf({"is_hlg": True, "range": ""}, "MVI_0003.mp4") == ("", "")
    e = edl.EDL.from_dict({
        "name": "odd", "type": "story", "session": "Fixture", "multi_day": False, "canvas": {"w": 1440, "h": 2560},
        "segs": [{"clip": str(a), "in": 0.2, "out": 1.0, "kind": "I", "order_key": "d01-s01", "tags": ["greeting"]},
                 {"clip": str(mute), "in": 0.2, "out": 1.0, "kind": "I", "order_key": "d01-s02", "tags": ["pain"]},
                 {"clip": str(pc), "in": 0.1, "out": 0.9, "kind": "C", "order_key": "d01-s03", "tags": ["finished"]},
                 {"clip": str(untagged), "in": 0.1, "out": 0.9, "kind": "C", "order_key": "d01-s04", "tags": ["finished"]}],
        "hooks": ["h1", "h2"], "caption": "", "audio_from": None})
    res = render_story.render(e, config.POST / "odd.mp4", preview=True, profile=_fast_profile(), stage=False)
    v, aud, dur = _probe(res["out"])
    assert aud and abs(dur - 3.2) < 0.15 and res["join"]["ok"]
    assert res["segments"][1]["audio"] == "silent" and res["segments"][0]["audio"] == "own"
    assert len(res["warnings"]) == 1 and "video-003.mp4" in res["warnings"][0] and "no audio" in res["warnings"][0]
    assert res["segments"][2]["range_assumed"] == "full (tagged pc)"
    assert res["segments"][2]["range_filter"] == "scale=in_range=full:out_range=limited"
    assert res["segments"][3]["range_assumed"] == "full (canon)"            # no tag + MVI_ name
    assert res["segments"][3]["range_filter"] == "scale=in_range=full:out_range=limited"
    side = edl.load(Path(res["out"]).with_name("odd_preview.edl.json"))
    assert len(side.export["warnings"]) == 1
    assert side.export["range_assumed"] == {"MVI_0009.mp4": "full (tagged pc)", "MVI_0010.mp4": "full (canon)"}
    rj = json.loads(Path(res["out"]).with_name("odd_preview.render.json").read_text(encoding="utf-8"))
    assert rj["warnings"] == res["warnings"]


def test_reencode_join_fallback(engine_root, clips, monkeypatch):
    from tools import config, encode, render_story
    a, b, c = clips
    e = _story(a, b, c)
    calls = []

    def bad_join(paths, out, expected=None, overwrite=False):
        calls.append(out)
        return {"path": Path(out), "duration": 0.0, "expected": expected, "ok": False}
    monkeypatch.setattr(encode, "join_segments", bad_join)
    res = render_story.render(e, config.POST / "fb.mp4", preview=True, profile=_fast_profile(), stage=False)
    assert calls and res["join"]["mode"] == "reencode" and res["join"]["ok"]
    v, aud, dur = _probe(res["out"])
    assert abs(dur - 3.4) < 0.15 and abs(res["join"]["duration"] - 3.4) < 0.15 and aud
    assert (v["width"], v["height"]) == (360, 640) and res["decode_errors"] == 0


def test_transfer_override_and_errors(engine_root, clips):
    from tools import config, edl, render_story
    a, b, c = clips
    e = _story(a, b, c)
    with pytest.raises(ValueError):
        render_story.render(e, config.POST / "x.mp4", transfer="hdr10", profile=_fast_profile())
    bad = e.copy()
    bad.segs[0].out = 0.1
    with pytest.raises(ValueError):
        render_story.render(bad, config.POST / "x.mp4", profile=_fast_profile())
    too_long = e.copy()
    too_long.segs[0].out = 99.0
    with pytest.raises(ValueError):
        render_story.render(too_long, config.POST / "x.mp4", profile=_fast_profile(), stage=False)
    with pytest.raises(ValueError):
        render_story.render(e, config.POST / "x.mov", profile=_fast_profile(), stage=False)
    # forcing hlg on a mixed EDL needs forced: true; a bare transfer is not a force
    forced = e.copy()
    forced.export.update({"transfer": "hlg", "forced": True})
    assert edl.expected_transfer(forced, lambda cl: {"is_hlg": "MVI" not in cl}) == "hlg"
    bare = e.copy()
    bare.export["transfer"] = "hlg"
    assert edl.expected_transfer(bare, lambda cl: {"is_hlg": "MVI" not in cl}) == "sdr"
    # encoder choice: libx265 must really be there before anything is staged
    assert render_story.profile_for("libx265", profile=_fast_profile()).chosen == "cpu"
    assert render_story.profile_for("libx265").chosen == "cpu"           # detected on this machine
    with pytest.raises(ValueError, match="libx265"):
        render_story.profile_for("libx265", profile=_fast_profile(libx265=False))
    with pytest.raises(ValueError, match="libx265"):
        render_story.render(e, config.POST / "nox.mp4", profile=_fast_profile(libx265=False), stage=False)
    assert render_story.profile_for(None, preview=True, profile=_fast_profile(libx265=False)).chosen == "cpu"
    with pytest.raises(ValueError):
        render_story.profile_for("av1_qsv")


def test_cli_preview_and_help(engine_root, clips, capsys):
    from tools import config, edl, render_story
    a, b, c = clips
    p = config.CACHE / "story.edl.json"
    edl.save(_story(a, b, c), p)
    rc = render_story.main([str(p), "--out", str(config.POST / "2026-10-19" / "A_1100_cli.mp4"), "--preview",
                            "--no-stage", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["preview"] and Path(out["out"]).exists()
    assert render_story.main(["missing.json", "--out", str(config.POST / "z.mp4")]) == 1
    with pytest.raises(SystemExit) as ex:
        render_story.main(["--help"])
    assert ex.value.code == 0
    assert "FORCES" in capsys.readouterr().out
