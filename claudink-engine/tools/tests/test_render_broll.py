"""Tests for tools.render_broll and templates/broll_station_first.json."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.tests.conftest import make_clip

pytest.importorskip("tools.encode", reason="tools.encode (render engine) is not in the tree yet")


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,pix_fmt,color_transfer,color_primaries,"
                          "color_space:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = [s for s in d["streams"] if s["codec_type"] == "audio"]
    return v, a, float(d["format"]["duration"])


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("broll_clips")
    hlg = make_clip(d / "IMG_0001.mp4", seconds=3.0, audio="sine", hdr=True)
    hlg2 = make_clip(d / "IMG_0002.mp4", seconds=2.0, audio="none", hdr=True)
    canon = make_clip(d / "MVI_0001.mp4", seconds=2.0, audio="sine", rotate=90)
    canon2 = make_clip(d / "MVI_0002.mp4", seconds=3.0, audio="none", rotate=90)
    return hlg, hlg2, canon, canon2


def _fast_profile():
    from tools import encode
    return encode.Profile(libx265=True, libx264=True, chosen="cpu", cpu_workers=2, cpu_preset="ultrafast")


def _shots(hlg, hlg2, canon, canon2):
    # 3 station + 6 piece shots, HLG and SDR mixed (template order: st, p, p, st, p, p, st, p, p)
    return [
        {"clip": str(hlg), "in": 0.0, "role": "station", "note": "wide station"},
        {"clip": str(canon), "in": 0.1, "role": "piece", "piece": "Shoulder"},
        {"clip": str(canon2), "in": 0.0, "role": "piece", "piece": "Forearm"},
        {"clip": str(canon2), "in": 1.5, "role": "station"},
        {"clip": str(hlg2), "in": 0.2, "role": "piece", "piece": "Shoulder"},
        {"clip": str(canon), "in": 0.3, "role": "piece", "piece": "Back"},
        {"clip": str(hlg), "in": 1.8, "role": "station"},
        {"clip": str(canon2), "in": 1.2, "role": "piece", "piece": "Forearm"},
        {"clip": str(hlg2), "in": 0.9, "role": "piece", "piece": "Calf"},
    ]


def test_template_is_valid_and_sums(engine_root):
    from tools import config, render_broll
    t = render_broll.load_template()
    assert t["name"] == "station_first" and t["total_s"] == config.BROLL_TEMPLATE_LEN
    assert abs(sum(s["s"] for s in t["slots"]) - t["total_s"]) < 1e-6
    assert t["canvas"] == [config.CANVAS_W, config.CANVAS_H] and t["silent"] is True and t["text"] is False
    assert max(s["s"] for s in t["slots"]) <= config.BROLL_MAX_SHOT
    assert render_broll.template_has_placeholder(t)      # still a stand-in until RULES.md 2 is dropped in
    bad = {**t, "slots": t["slots"][:-1]}
    assert any("sum" in e for e in render_broll.validate_template(bad))
    assert any("role" in e for e in render_broll.validate_template({**t, "slots": [{"role": "hero", "s": 1}]}))


def test_assign_slots_and_role_count_errors(clips):
    from tools import render_broll
    shots = _shots(*clips)
    t = render_broll.load_template()
    plan = render_broll.assign_slots(shots, t)
    assert [p["role"] for p in plan] == [s["role"] for s in t["slots"]]
    assert plan[0]["out"] == 2.4 and plan[1]["out"] == pytest.approx(1.7)
    with pytest.raises(ValueError, match="station"):
        render_broll.assign_slots(shots[1:], t)          # 2 station shots
    with pytest.raises(ValueError, match="piece"):
        render_broll.assign_slots(shots + [shots[1]], t)  # 7 piece shots
    with pytest.raises(ValueError, match="role"):
        render_broll.normalize_shots([{"clip": "x.mp4", "in": 0, "role": "hero"}])
    with pytest.raises(ValueError):
        render_broll.normalize_shots([])


def test_shot_filters_follow_the_approved_method():
    from tools import render_broll
    hlg = render_broll.shot_filters({"is_hlg": True, "range": "tv"})
    assert hlg.startswith("eq=gamma=1.12:contrast=1.04:saturation=1.05") and "hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.5" in hlg
    assert render_broll.shot_filters({"is_hlg": False, "range": "tv"}) == ""
    assert render_broll.shot_filters({"is_hlg": False, "range": "pc"}) == "scale=in_range=full:out_range=limited"
    # the HLG chain in the render engine is the exact tonemap from the skill, then our look
    from tools import encode
    prof = encode.Profile(libx265=True, chosen="cpu")
    info = {"codec": "hevc", "width": 1920, "height": 1080, "is_hlg": True, "is_10bit": True, "range": "tv"}
    chain = encode.video_chain(prof, info, "sdr", (1440, 2560), extra_vf=hlg)
    assert ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,eq=gamma=1.12") in chain
    assert "transpose" not in chain and "pad=1440:2560" in chain and "fps=30" in chain


def test_build_edl_sidecar_shape(clips):
    from tools import edl, render_broll
    t = render_broll.load_template()
    plan = render_broll.assign_slots(_shots(*clips), t)
    e = render_broll.build_edl(plan, t, "b1", {"hooks": ["Which one?", "Rate these 1-10"]})
    assert e.type == "broll" and edl.validate(e) == [] and e.audio_from is None
    assert [s.tags for s in e.segs] == [[sl["role"]] for sl in t["slots"]]
    assert e.pieces == ["Shoulder", "Forearm", "Back", "Calf"] and e.segs[1].extra["piece"] == "Shoulder"
    assert [s.order_key for s in e.segs] == [f"d01-s{i:02d}" for i in range(1, 10)]
    assert edl.duration(e) == pytest.approx(13.6)
    plan[3]["order_key"] = "2026-10-11T14:00:00-04:00"
    with pytest.raises(ValueError, match="order_key"):
        render_broll.build_edl(plan, t, "b1")


@pytest.mark.slow
def test_full_broll_render(engine_root, clips):
    from tools import config, edl, render_broll
    from datetime import datetime
    out = config.POST / "2026-10-19" / "B_1430_fixture.mp4"
    shots = {"name": "fixture_broll", "shots": _shots(*clips), "hooks": ["Which one?", "Rate these 1-10"],
             "caption": "pick one"}
    now = datetime(2026, 10, 19, 7, 0, 0, tzinfo=config.TZ)
    res = render_broll.render(shots, None, out, now=now, profile=_fast_profile(), stage=False)
    assert out.exists() and res["template"] == "station_first" and res["template_placeholder"]
    v, aud, dur = _probe(out)
    assert (v["width"], v["height"]) == (1440, 2560) and v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p"
    assert v["color_transfer"] == "bt709" and v["color_primaries"] == "bt709"
    assert aud and aud[0]["codec_name"] == "aac"                   # silent track present
    assert abs(dur - 13.6) <= config.BROLL_TOL and res["duration_ok"] and res["join"]["ok"]
    assert res["decode_errors"] == 0 and res["chat"]["ok"]
    assert Path(res["chat"]["path"]).stat().st_size <= config.CHAT_COPY_MAX_BYTES
    assert [s["hlg"] for s in res["slots"]] == [True, False, False, False, True, False, True, False, True]
    side = edl.load(out.with_name("B_1430_fixture.edl.json"))
    assert side.type == "broll" and side.hooks == ["Which one?", "Rate these 1-10"] and side.caption == "pick one"
    assert side.export["transfer"] == "sdr" and side.export["rendered_et"] == "2026-10-19T07:00:00-04:00"
    assert side.pieces == ["Shoulder", "Forearm", "Back", "Calf"]
    rj = json.loads(out.with_name("B_1430_fixture.render.json").read_text(encoding="utf-8"))
    assert rj["master"]["width"] == 1440 and len(rj["slots"]) == 9
    # the silence check: the audio track is there but carries nothing
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(out), "-af", "volumedetect",
                        "-vn", "-f", "null", "-"], capture_output=True, text=True)
    import re
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", p.stderr)
    assert m and (m.group(1) == "-inf" or float(m.group(1)) < -80.0), p.stderr[-400:]


def test_cli_errors_and_help(engine_root, clips, capsys):
    from tools import config, render_broll
    shots = config.CACHE / "shots.json"
    shots.write_text(json.dumps(_shots(*clips)[1:]), encoding="utf-8")       # wrong station count
    assert render_broll.main([str(shots), "--out", str(config.POST / "b.mp4"), "--no-stage"]) == 1
    assert "station" in capsys.readouterr().out
    assert render_broll.main(["missing.json", "--out", str(config.POST / "b.mp4")]) == 1
    with pytest.raises(SystemExit) as ex:
        render_broll.main(["--help"])
    assert ex.value.code == 0
