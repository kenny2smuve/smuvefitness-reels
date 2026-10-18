"""Tests for tools/gates.py (G1-G12).

One module-scoped studio renders a few 1440x2560 letterboxed HEVC segments with make_clip and
assembles every reel variant with the ffmpeg concat demuxer (-c copy), so the whole file stays
well under a minute. Variants that only differ in EDL or ledgers reuse the base reel and its
cached proxy.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.tests.conftest import FFMPEG, make_clip

pytestmark = pytest.mark.slow

FPS = 24
TODAY = date(2026, 10, 19)
LETTERBOX = "pad=1440:2560:0:320"
SEG_DUR = {"I": 4.8, "M18": 1.8, "M29": 2.9, "C": 3.5, "BARS": 3.9, "GRAY": 3.9, "BLACK": 0.5}
CLIP_NAMES = {"I": "video-001.mp4", "M18": "video-002.mp4", "M29": "video-003.mp4", "C": "MVI_0001.mp4",
              "BARS": "video-010.mp4", "GRAY": "video-011.mp4", "BLACK": "video-012.mp4"}
# 10 segs, 38.0 s: 4 needle shots, approval+reaction, camera, finished hold 3.5 s last
PLAN = [
    ("I", "I", ["greeting", "hours"]),
    ("I", "I", ["design"]),
    ("M18", "M", ["stencil"]),
    ("I", "I", ["pain", "humor", "needle"]),
    ("M29", "M", ["needle"]),
    ("I", "I", ["needle", "talking"]),
    ("M29", "M", ["needle"]),
    ("M29", "M", ["camera"]),
    ("I", "I", ["approval", "reaction"]),
    ("C", "C", ["finished"]),
]
HOOKS = ["Would you sit 7 hours for this?", "7 hours. One session. Worth it?"]
ENV_KEYS = ("CLAUDINK_ENGINE_ROOT", "CLAUDINK_DRIVE_OUT", "CLAUDINK_FOOTAGE_ROOT", "CLAUDINK_METRICS_CSV",
            "CLAUDINK_LEGACY_OUT")
PIECES_H = ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"]
HOOKS_H = ["hook", "type", "used_on", "reel", "result_views", "status"]
SESSIONS_H = ["session", "folder", "posts_so_far", "last_post", "rest_until", "status"]
CAPTIONS_H = ["caption", "used_on", "reel"]
DELIV_H = ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2", "caption", "status",
           "posted_shortcode", "views_24h", "views_72h", "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"]


def _reload():
    import tools.config as c
    importlib.reload(c)
    import tools.safeio as s
    importlib.reload(s)
    return c, s


def _segment(path: Path, kind: str) -> Path:
    sec = SEG_DUR[kind]
    if kind in ("I", "M18", "M29", "C"):
        pattern = f"testsrc2=size=1440x1920:rate={FPS}:duration={sec}"
        vf = "scroll=h=0.004:v=0.002," + LETTERBOX          # a moving opener, like a greeting at the door
    elif kind == "BARS":
        pattern = f"smptebars=size=1440x1920:rate={FPS}:duration={sec}"
        vf = LETTERBOX
    elif kind == "GRAY":
        pattern = f"color=c=0x202020:size=1440x1920:rate={FPS}:duration={sec}"
        vf = LETTERBOX
    else:
        pattern = f"color=c=black:size=1440x1920:rate={FPS}:duration={sec}"
        vf = LETTERBOX
    return make_clip(path, seconds=sec, fps=FPS, pattern=pattern, audio="speech", extra_vf=vf, codec="libx265")


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    base = tmp_path_factory.mktemp("gates")
    root = base / "reel-engine"
    drive = base / "My Drive" / "ClaudeInk Reels"
    footage = base / "My Drive"
    for d in (root, drive, footage, base / "igserver", base / "lists"):
        d.mkdir(parents=True, exist_ok=True)
    old = {k: os.environ.get(k) for k in ENV_KEYS}
    os.environ.update({
        "CLAUDINK_ENGINE_ROOT": str(root), "CLAUDINK_DRIVE_OUT": str(drive),
        "CLAUDINK_FOOTAGE_ROOT": str(footage), "CLAUDINK_METRICS_CSV": str(base / "igserver" / "reels_history.csv"),
        "CLAUDINK_LEGACY_OUT": str(base / "out"),
    })
    config, safeio = _reload()
    config.ensure_engine_dirs()
    from tools import gates

    clips = {k: _segment(footage / "Session 1 of 4" / n, k) for k, n in CLIP_NAMES.items()}

    # ledgers
    for row in ({"piece": "Shoulder", "sessions": "1", "hours": "7", "color": "no"},
                {"piece": "Color Dragon", "sessions": "2", "hours": "9", "color": "yes"},
                {"piece": "Unknown Skull", "color": ""}):
        safeio.append_csv_row(config.LEDGERS / "pieces.csv", row, PIECES_H)
    for h in ("Best $1,200 you'll ever spend", "Which one would you get? 1-7"):
        safeio.append_csv_row(config.LEDGERS / "hooks.csv", {"hook": h, "status": "burned"}, HOOKS_H)
    safeio.append_csv_row(config.LEDGERS / "sessions.csv", {"session": "Shoulder 7h", "posts_so_far": "3"}, SESSIONS_H)
    safeio.append_csv_row(config.LEDGERS / "sessions.csv",
                          {"session": "Back piece", "posts_so_far": "4", "rest_until": "2026-10-21", "status": "spent"},
                          SESSIONS_H)
    safeio.write_text(config.LEDGERS / "captions_used.csv", ",".join(CAPTIONS_H) + "\n")
    safeio.write_text(config.LEDGERS / "deliveries.csv", ",".join(DELIV_H) + "\n")

    post = config.POST / TODAY.isoformat()
    st = SimpleNamespace(base=base, root=root, config=config, safeio=safeio, gates=gates, clips=clips, post=post,
                         now=datetime(2026, 10, 19, 6, 52, 10, tzinfo=config.TZ))

    def concat(parts, out: Path) -> Path:
        lst = base / "lists" / (out.stem + ".txt")
        lst.write_text("".join(f"file '{Path(p).as_posix()}'\n" for p in parts), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", str(out)], check=True, capture_output=True, text=True)
        return out

    def edl(plan, name, **over) -> dict:
        t0 = datetime(2026, 10, 11, 14, 0, 0, tzinfo=config.TZ)
        segs = []
        for i, (ck, kind, tags) in enumerate(plan):
            segs.append({"clip": str(clips[ck]), "in": 0.0, "out": SEG_DUR[ck], "kind": kind,
                         "order_key": (t0 + timedelta(minutes=5 * i)).isoformat(), "tags": list(tags), "note": ""})
        doc = {"name": name, "type": "story", "session": "Shoulder 7h", "multi_day": False,
               "canvas": {"w": 1440, "h": 2560}, "segs": segs, "hooks": list(HOOKS),
               "caption": "Would you sit this long for one piece?\n\n" + config.CAPTION_TAIL,
               "pieces": ["Shoulder"], "labels": {"Shoulder": "1 session / 7 hours"}, "audio_from": None}
        doc.update(over)
        return doc

    def write_edl(doc: dict, path: Path) -> Path:
        return safeio.write_json(path, doc, overwrite=True)

    def reel(name: str, plan, *, chat: bool = True) -> Path:
        out = concat([clips[ck] for ck, _k, _t in plan], post / f"{name}.mp4")
        write_edl(edl(plan, name), post / f"{name}.edl.json")
        if chat:
            make_clip(post / f"{name}_chat.mp4", seconds=1.0, w=360, h=640, fps=FPS, audio="speech")
        return out

    def run(reel_path: Path, rtype: str = "story", **kw) -> dict:
        kw.setdefault("today", TODAY)
        kw.setdefault("now", st.now)
        return gates.run_gates(reel_path, rtype, **kw)

    st.concat, st.edl, st.write_edl, st.reel, st.run = concat, edl, write_edl, reel, run
    st.base_reel = reel("A_1100_shoulder7h", PLAN)
    st.base_edl = post / "A_1100_shoulder7h.edl.json"
    yield st

    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _reload()


def gate(report: dict, gid: str, *, hard: bool = True) -> dict:
    for g in report["gates"]:
        if g["id"] == gid and g["hard"] == hard:
            return g
    raise AssertionError(f"{gid} hard={hard} missing from {[(g['id'], g['hard']) for g in report['gates']]}")


def hard_fails(report: dict) -> list[str]:
    return [g["id"] for g in report["gates"] if g["hard"] and not g["ok"]]


# --------------------------------------------------------------------------- the passing story
def test_base_story_passes_every_hard_gate_and_matches_schema(studio):
    rep = studio.run(studio.base_reel)
    assert hard_fails(rep) == [], json.dumps([g for g in rep["gates"] if g["hard"] and not g["ok"]], indent=1)
    assert rep["pass"] is True
    hard_ids = [g["id"] for g in rep["gates"] if g["hard"]]
    assert hard_ids == [f"G{i}" for i in range(1, 13)]
    assert [g["name"] for g in rep["gates"] if g["hard"]] == [
        "frame1", "opening", "window3_10", "story_content", "clean_picture", "voices", "order_and_sound",
        "length", "spec", "hooks_caption", "uniqueness", "pieces_visual"]
    soft = [g for g in rep["gates"] if not g["hard"]]
    assert [g["id"] for g in soft] == ["G4", "G12"] and all(g["detail"].startswith("needs critic") for g in soft)
    for g in rep["gates"]:
        assert set(g) == {"id", "name", "hard", "ok", "value", "detail"} and isinstance(g["value"], dict)
        assert isinstance(g["detail"], str) and g["detail"]
    assert rep["generated_et"] == "2026-10-19T06:52:10-04:00"
    assert rep["edl"] == str(studio.base_edl) and rep["type"] == "story"
    assert 37.5 <= rep["duration_s"] <= 38.5
    g1 = gate(rep, "G1")["value"]
    assert g1["luma_std"] >= 60 and g1["motion"] >= 4.0 and g1["active_rows"] == [80, 560]
    assert "hours" in g1["first_seg_tags"]
    g6 = gate(rep, "G6")["value"]
    assert g6["i_segs"] == 5 and all(c["audio_source"] == "clip" for c in g6["checked"])
    assert gate(rep, "G9")["value"]["codec"] == "hevc" and gate(rep, "G9")["value"]["chat_decodes"] is True
    sidecar = studio.post / "A_1100_shoulder7h.gates.json"
    assert sidecar.is_file() and rep["report"] == str(sidecar)
    on_disk = json.loads(sidecar.read_text(encoding="utf-8"))
    assert on_disk["pass"] is True and len(on_disk["gates"]) == 14
    # the proxy is cached in cache/gates and reused on the next run
    assert Path(rep["proxy"]).is_file() and Path(rep["proxy"]).parent == studio.config.CACHE / "gates"
    mtime = Path(rep["proxy"]).stat().st_mtime_ns
    rep2 = studio.run(studio.base_reel)
    assert rep2["pass"] and Path(rep2["proxy"]).stat().st_mtime_ns == mtime


# --------------------------------------------------------------------------- rendered failing variants
def test_static_opener_fails_g1_motion(studio):
    plan = [("BARS", "I", ["greeting", "hours"]), PLAN[1], PLAN[2], PLAN[3]]
    reel = studio.reel("A_static", plan)
    rep = studio.run(reel)
    g1 = gate(rep, "G1")
    assert not g1["ok"] and "motion" in g1["detail"] and g1["value"]["motion"] < 1.0
    assert "luma" not in g1["detail"]            # bars are bright and high-contrast
    assert gate(rep, "G2")["ok"]                 # the I seg counts as speech within 0.3 s
    assert "G1" in hard_fails(rep)


def test_dark_opener_fails_g1_luma(studio):
    plan = [("GRAY", "I", ["greeting", "hours"]), PLAN[1], PLAN[2], PLAN[3]]
    rep = studio.run(studio.reel("A_dark", plan))
    g1 = gate(rep, "G1")
    assert not g1["ok"] and "luma std" in g1["detail"] and g1["value"]["luma_std"] < 5.0
    assert gate(rep, "G5")["ok"], gate(rep, "G5")["detail"]     # dark is not black


def test_black_segment_fails_g5(studio):
    plan = [PLAN[0], PLAN[1], ("BLACK", "M", ["stencil"]), PLAN[2], PLAN[3]]
    rep = studio.run(studio.reel("A_black", plan))
    g5 = gate(rep, "G5")
    assert not g5["ok"] and "black" in g5["detail"]
    (s, e), = g5["value"]["black"]
    assert 9.4 <= s <= 9.8 and 9.9 <= e <= 10.3
    assert gate(rep, "G1")["ok"], gate(rep, "G1")["detail"]


def test_twenty_second_story_fails_g8(studio):
    plan = [PLAN[0], PLAN[1], PLAN[2], PLAN[3], PLAN[4], PLAN[2]]        # 20.1 s
    rep = studio.run(studio.reel("A_short", plan))
    g8 = gate(rep, "G8")
    assert not g8["ok"] and "outside 36-48s" in g8["detail"] and 19.5 <= g8["value"]["duration_s"] <= 21.5
    # multi-day rule is wider but still has the floor
    rep_md = studio.run(studio.post / "A_short.mp4", multi_day=True)
    assert not gate(rep_md, "G8")["ok"] and gate(rep_md, "G8")["value"]["max_s"] == 75.0


# --------------------------------------------------------------------------- EDL variants on the base reel
def _variant(studio, name: str, **over) -> dict:
    doc = studio.edl(PLAN, name, **over)
    return doc


def test_order_keys_swapped_fail_g7(studio):
    doc = _variant(studio, "swap")
    doc["segs"][3]["order_key"], doc["segs"][5]["order_key"] = doc["segs"][5]["order_key"], doc["segs"][3]["order_key"]
    p = studio.write_edl(doc, studio.post / "v_swap.edl.json")
    rep = studio.run(studio.base_reel, edl=p)
    g7 = gate(rep, "G7")
    assert not g7["ok"] and "backwards" in g7["detail"]
    assert hard_fails(rep) == ["G7"]


def test_canon_seg_in_the_middle_fails_g7(studio):
    doc = _variant(studio, "cmid")
    doc["segs"][4]["kind"] = "C"
    p = studio.write_edl(doc, studio.post / "v_cmid.edl.json")
    rep = studio.run(studio.base_reel, edl=p)
    assert not gate(rep, "G7")["ok"] and "Canon" in gate(rep, "G7")["detail"]
    doc2 = _variant(studio, "afrom", audio_from="video-003.mp4")
    rep2 = studio.run(studio.base_reel, edl=studio.write_edl(doc2, studio.post / "v_afrom.edl.json"))
    assert "audio_from" in gate(rep2, "G7")["detail"]


def test_missing_chat_copy_fails_g9(studio):
    rep = studio.run(studio.base_reel, chat=studio.post / "nope_chat.mp4")
    g9 = gate(rep, "G9")
    assert not g9["ok"] and "chat copy missing" in g9["detail"]
    assert hard_fails(rep) == ["G9"]
    # a transfer promised by the EDL export block must match the file
    doc = _variant(studio, "trc", export={"transfer": "arib-std-b67"})
    rep2 = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_trc.edl.json"))
    assert "export.transfer" in gate(rep2, "G9")["detail"]


def test_burned_hook_and_wrong_caption_tail_fail_g10(studio):
    doc = _variant(studio, "hook", hooks=["Which one would you get? 1-7", HOOKS[1]])
    rep = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_hook.edl.json"))
    g10 = gate(rep, "G10")
    assert not g10["ok"] and "hook 1" in g10["detail"] and g10["value"]["caption_tail_ok"] is True
    assert hard_fails(rep) == ["G10"]
    doc2 = _variant(studio, "tail", caption="Would you sit this long?\n\nText me to book #tattoo")
    rep2 = studio.run(studio.base_reel, edl=studio.write_edl(doc2, studio.post / "v_tail.edl.json"))
    assert "caption tail" in gate(rep2, "G10")["detail"]
    doc3 = _variant(studio, "one", hooks=[HOOKS[0]])
    rep3 = studio.run(studio.base_reel, edl=studio.write_edl(doc3, studio.post / "v_one.edl.json"))
    assert "2 hooks" in gate(rep3, "G10")["detail"]


def test_colour_or_unknown_piece_fails_g12(studio):
    doc = _variant(studio, "colour", pieces=["Color Dragon"])
    rep = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_colour.edl.json"))
    g12 = gate(rep, "G12")
    assert not g12["ok"] and "colour piece" in g12["detail"] and g12["value"]["colour"] == ["Color Dragon"]
    assert "G12" in hard_fails(rep) and gate(rep, "G12", hard=False)["detail"].startswith("needs critic")
    for pieces, word in ((["Mystery Back"], "not in pieces.csv"), (["Unknown Skull"], "no color value"), ([], "no pieces")):
        d = _variant(studio, "p", pieces=pieces)
        r = studio.run(studio.base_reel, edl=studio.write_edl(d, studio.post / "v_piece.edl.json"))
        assert word in gate(r, "G12")["detail"] and "identify piece by frames" in gate(r, "G12")["detail"]


def test_three_second_silent_stretch_fails_g3(studio):
    doc = _variant(studio, "dead")
    doc["segs"][0]["out"] = 4.0                                   # I 0-4
    doc["segs"][1].update({"kind": "M", "out": 3.0, "tags": ["stencil"]})   # M 4-7, no speech, no cut
    rep = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_dead.edl.json"))
    g3 = gate(rep, "G3")
    assert not g3["ok"] and "no speech and no cut" in g3["detail"]
    assert g3["value"]["longest_dead"][:2] == [4.0, 7.0]
    # an M seg that fits under 2 s inside 3-10 s is fine
    assert gate(studio.run(studio.base_reel), "G3")["ok"]


def test_voice_at_the_out_point_fails_g6(studio):
    doc = _variant(studio, "cut")
    doc["segs"][0]["out"] = 4.3                                   # the tone burst is on at 4.0-4.5 s
    rep = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_cut.edl.json"))
    g6 = gate(rep, "G6")
    assert not g6["ok"] and "voice still sounding" in g6["detail"]
    seg0 = g6["value"]["checked"][0]
    assert seg0["tail_rms"] > 0.5 * seg0["speech_rms"] and seg0["audio_source"] == "clip"


def test_transcript_word_ending_after_out_fails_g6_and_counts_as_speech(studio):
    words = [[0.1 + 0.5 * k, 0.5 + 0.5 * k, f"w{k}"] for k in range(9)]            # 0.1 .. 4.5 s
    good = studio.safeio.write_json(studio.config.CACHE / "words_good.json",
                                    {CLIP_NAMES["I"]: words, CLIP_NAMES["C"]: []}, overwrite=True)
    rep = studio.run(studio.base_reel, transcript=good)
    assert hard_fails(rep) == [], hard_fails(rep)
    assert gate(rep, "G2")["value"]["speech_source"] == "transcript"
    assert gate(rep, "G6")["value"]["checked"][0]["last_word"] == "w8"
    bad_words = words[:-1] + [[4.5, 4.85, "bro"]]
    bad = studio.safeio.write_json(studio.config.CACHE / "words_bad.json", {CLIP_NAMES["I"]: bad_words}, overwrite=True)
    rep2 = studio.run(studio.base_reel, transcript=bad)
    g6 = gate(rep2, "G6")
    assert not g6["ok"] and "'bro' ends at 4.85 after out 4.80" in g6["detail"]
    # an injected provider object works the same way
    class Provider:
        def words(self, clip, start=None, end=None, vad=True):
            return [studio.gates.Word(4.6, 4.9, "late")] if Path(clip).name == CLIP_NAMES["I"] else []
    rep3 = studio.run(studio.base_reel, transcript=Provider())
    assert "'late' ends at 4.90" in gate(rep3, "G6")["detail"]


def test_missing_stakes_fails_g2(studio):
    doc = _variant(studio, "stakes")
    doc["segs"][0]["tags"] = ["greeting"]
    doc["segs"][3]["tags"] = ["humor", "needle"]
    rep = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_stakes.edl.json"))
    g2 = gate(rep, "G2")
    assert not g2["ok"] and "stakes" in g2["detail"] and g2["value"]["stakes_at"] is None
    doc["segs"][3]["tags"] = ["pain", "needle"]            # stakes said at 11.4 s: too late
    rep2 = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_stakes2.edl.json"))
    assert "> 3.0s" in gate(rep2, "G2")["detail"]


def test_no_edl_fails_closed(studio):
    rep = studio.run(studio.base_reel, edl=studio.post / "missing.edl.json")
    assert rep["pass"] is False and rep["edl"] is not None
    for gid in ("G4", "G7", "G10", "G12"):
        assert not gate(rep, gid)["ok"] and "no EDL" in gate(rep, gid)["detail"]
    g1 = gate(rep, "G1")
    assert g1["value"]["face"] is None or g1["value"]["face"] is False
    if g1["value"]["face"] is None:
        assert "no face and no EDL tag" in g1["detail"]
    assert "no EDL" in gate(rep, "G2")["detail"]
    assert gate(rep, "G3")["value"]["source"] == "scdet"
    cuts = gate(rep, "G3")["value"]["cuts_in_window"]
    assert any(4.6 <= c <= 5.0 for c in cuts) and any(9.4 <= c <= 9.8 for c in cuts)


def test_broll_and_th_rules(studio):
    rep = studio.run(studio.base_reel, "broll")
    assert gate(rep, "G4")["detail"] == "not applicable to broll" and gate(rep, "G6")["ok"]
    assert [g["id"] for g in rep["gates"] if not g["hard"]] == ["G12"]
    assert not gate(rep, "G2")["ok"] and "station" in gate(rep, "G2")["detail"]
    g3 = gate(rep, "G3")
    assert not g3["ok"] and "longest shot 4.80s" in g3["detail"] and "template not found" not in g3["detail"]
    assert gate(rep, "G8")["ok"]
    rep_th = studio.run(studio.base_reel, "th")
    assert gate(rep_th, "G4")["detail"] == "not applicable to th"
    assert gate(rep_th, "G8")["ok"] and gate(rep_th, "G12")["ok"]
    doc = _variant(studio, "thnp", pieces=[])
    rep_th2 = studio.run(studio.base_reel, "th", edl=studio.write_edl(doc, studio.post / "v_thnp.edl.json"))
    assert gate(rep_th2, "G12")["ok"]                       # a talking head may show no piece


def test_broll_template_match(studio):
    tpl = studio.safeio.write_json(studio.config.TEMPLATES / "test_tpl.json",
                                   {"slots": [{"seconds": 4.8, "role": "station"}, {"seconds": 4.8}, {"seconds": 1.8}]},
                                   overwrite=True)
    plan = [("I", "M", ["station"]), ("I", "M", ["piece"]), ("M18", "M", ["piece"])]
    doc = studio.edl(plan, "br", type="broll", session="", pieces=["Shoulder"])
    p = studio.write_edl(doc, studio.post / "v_broll.edl.json")
    rep = studio.run(studio.base_reel, "broll", edl=p, template=tpl)
    g3 = gate(rep, "G3")
    assert "longest shot 4.80s" in g3["detail"] and "template mismatch" not in g3["detail"]
    assert g3["value"]["template"] == [4.8, 4.8, 1.8]
    assert gate(rep, "G2")["ok"]
    doc["segs"][1]["out"] = 4.4
    rep2 = studio.run(studio.base_reel, "broll", edl=studio.write_edl(doc, studio.post / "v_broll2.edl.json"), template=tpl)
    assert "slot 2: 4.40s vs 4.80s" in gate(rep2, "G3")["detail"]


# --------------------------------------------------------------------------- G11 with ledgers
def test_g11_shot_reuse_session_rest_pieces_per_day_and_phash(studio):
    cfg, sio = studio.config, studio.safeio
    deliveries = cfg.LEDGERS / "deliveries.csv"
    try:
        # a posted reel 4 days ago that used the very same shots
        old_dir = cfg.POST / "2026-10-15"
        studio.write_edl(studio.edl(PLAN, "A_1100_old"), old_dir / "A_1100_old.edl.json")
        sio.append_csv_row(deliveries, {"id": "d1", "date": "2026-10-15", "slot": "A", "type": "story",
                                        "file": "post/2026-10-15/A_1100_old.mp4", "status": "posted"}, DELIV_H)
        rep = studio.run(studio.base_reel)
        g11 = gate(rep, "G11")
        assert not g11["ok"] and "100% of shots were posted" in g11["detail"]
        assert g11["value"]["reuse_fraction"] == 1.0 and g11["value"]["posted_edls"] == 1
        # outside the 14-day window the same reel no longer counts
        rep_old = studio.run(studio.base_reel, today=date(2026, 11, 2))
        assert gate(rep_old, "G11")["value"]["reuse_fraction"] == 0.0
        sio.rewrite_csv(deliveries, [], DELIV_H)

        # session rest
        doc = studio.edl(PLAN, "rest", session="Back piece")
        rep2 = studio.run(studio.base_reel, edl=studio.write_edl(doc, studio.post / "v_rest.edl.json"))
        assert "resting on 2026-10-19" in gate(rep2, "G11")["detail"]
        assert gate(studio.run(studio.base_reel, edl=studio.post / "v_rest.edl.json", today=date(2026, 10, 21)), "G11")["ok"]

        # pieces per day: two other deliveries today already show the Shoulder
        for slot in ("A_1100_other", "B_1430_other"):
            d = studio.edl([("M29", "M", ["piece"])], slot, pieces=["Shoulder"])
            d["segs"][0]["clip"] = "F:/My Drive/Shop shorts/video-099.mp4"
            studio.write_edl(d, studio.post / f"{slot}.edl.json")
            sio.append_csv_row(deliveries, {"id": slot, "date": TODAY.isoformat(), "type": "broll",
                                            "file": f"post/{TODAY.isoformat()}/{slot}.mp4", "status": "delivered"}, DELIV_H)
        rep3 = studio.run(studio.base_reel)
        g11 = gate(rep3, "G11")
        assert not g11["ok"] and "'Shoulder' would be in 3 reels today" in g11["detail"]
        assert g11["value"]["pieces_today"] == {"Shoulder": 3}
        # the reel's own delivery row never counts against itself
        sio.rewrite_csv(deliveries, [{"id": "me", "date": TODAY.isoformat(), "type": "story",
                                      "file": str(studio.base_reel), "status": "delivered"}], DELIV_H)
        assert gate(studio.run(studio.base_reel), "G11")["value"]["pieces_today"] == {"Shoulder": 1}
    finally:
        sio.rewrite_csv(deliveries, [], DELIV_H)

    # frame-1 phash against a posted signature
    pytest.importorskip("imagehash")
    g = studio.gates
    fs = g.measure_frames(g.proxy_path(studio.base_reel), FPS, g.cache_key(studio.base_reel))
    mine = g.frame1_phash_hex(fs.frame1, fs.active)
    sig = sio.write_json(cfg.DATA / "sig_self.json", {"DebCcJvqDI4": {"frame1": mine, "frames": []}}, overwrite=True)
    rep4 = studio.run(studio.base_reel, signatures=sig)
    g11 = gate(rep4, "G11")
    assert not g11["ok"] and "matches posted reel DebCcJvqDI4" in g11["detail"] and g11["value"]["phash_min_distance"] == 0
    far = "f" * len(mine) if not mine.startswith("f") else "0" * len(mine)
    sig2 = sio.write_json(cfg.DATA / "signatures2.json", [{"id": "x", "phash": far}], overwrite=True)
    assert gate(studio.run(studio.base_reel, signatures=sig2), "G11")["ok"]


# --------------------------------------------------------------------------- HLG proxy, calibration, CLI
def test_hlg_proxy_chain_tonemaps_to_8bit_bt709(studio):
    g = studio.gates
    hlg = make_clip(studio.base / "hlg.mp4", seconds=2.0, fps=FPS, hdr=True, audio="none",
                    pattern=f"testsrc2=size=720x960:rate={FPS}:duration=2", extra_vf="pad=720:1280:0:160")
    info = g.probe(hlg)
    assert g.is_hlg(info) and info["codec"] == "hevc" and info["pix_fmt"] == "yuv420p10le" and not info["has_audio"]
    assert g.proxy_chain(True).startswith("zscale=t=linear:npl=100,format=gbrpf32le") and "tonemap=hable" in g.proxy_chain(True)
    assert g.proxy_chain(False) == "format=yuv420p"
    prx = g.build_proxy(hlg, info)
    pinfo = g.probe(prx)
    assert (pinfo["width"], pinfo["height"], pinfo["pix_fmt"]) == (360, 640, "yuv420p")
    assert pinfo["color_transfer"] == "bt709" and not pinfo["has_audio"]
    fs = g.measure_frames(prx, info["fps"], g.cache_key(hlg))
    assert fs.active == (80, 560) and fs.luma_std > 40 and fs.motion_over(1.0) < 4.0   # plain testsrc2 barely moves


def test_calibrate_writes_calibration_and_overrides_thresholds(studio):
    g, cfg = studio.gates, studio.config
    res = g.calibrate([studio.base_reel, studio.post / "A_short.mp4"], now=studio.now)
    cal = cfg.DATA / "calibration.json"
    assert res["path"] == str(cal) and cal.is_file()
    on_disk = json.loads(cal.read_text(encoding="utf-8"))
    motions = [s["motion"] for s in on_disk["samples"]]
    assert len(motions) == 2 and on_disk["motion_threshold"] == pytest.approx(0.6 * min(motions), abs=1e-3)
    assert on_disk["luma_std_min"] == 60.0 and on_disk["generated_et"] == "2026-10-19T06:52:10-04:00"
    assert on_disk["proxy_chain"]["hlg"] == g.proxy_chain(True)
    # the default location is picked up automatically; an explicit file can raise the bar
    rep = studio.run(studio.base_reel)
    assert rep["thresholds"]["source"] == str(cal) and rep["thresholds"]["motion"] == on_disk["motion_threshold"]
    strict = studio.safeio.write_json(cfg.DATA / "strict.json", {"motion_threshold": 99.0}, overwrite=True)
    rep2 = studio.run(studio.base_reel, calibration=strict)
    assert not gate(rep2, "G1")["ok"] and "< 99.00" in gate(rep2, "G1")["detail"]


def test_cli_table_json_and_exit_codes(studio, capsys):
    g = studio.gates
    rc = g.main([str(studio.base_reel), "--type", "story", "--today", TODAY.isoformat(), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    rep = json.loads(out)
    assert rep["pass"] is True and rep["today"] == "2026-10-19" and len(rep["gates"]) == 14
    rc = g.main([str(studio.post / "A_dark.mp4"), "--type", "story", "--today", TODAY.isoformat()])
    out = capsys.readouterr().out
    assert rc == 1 and "FAIL" in out.splitlines()[0] and "G1  frame1          FAIL" in out
    assert any(line.strip().startswith("G4  story_content   CRITIC") for line in out.splitlines())
    assert g.main([str(studio.post / "nope.mp4"), "--type", "story"]) == 1
    assert g.main([str(studio.base_reel), "--type", "story", "--today", "19/10/2026"]) == 1
    cal = studio.config.DATA / "cal_cli.json"
    rc = g.main(["calibrate", str(studio.base_reel), "--out", str(cal), "--json"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out)["path"] == str(cal) and cal.is_file()
    assert g.main(["calibrate", str(studio.post / "nope.mp4")]) == 1


# --------------------------------------------------------------------------- pure helpers
def test_dead_gaps_helper():
    from tools import gates as g
    # speech 0-4.8 and 4.8-8.6, cuts at 4.8, 8.6, 10.6; window 3-10 -> dead 8.6-10 (clipped 1.4)
    gaps = g.dead_gaps([4.8, 8.6, 10.6], [(0.0, 4.8), (4.8, 8.6)], 3.0, 10.0, 38.0)
    assert gaps == [(8.6, 10.6, pytest.approx(1.4))]
    gaps = g.dead_gaps([4.0, 7.0], [(0.0, 4.0)], 3.0, 10.0, 38.0)
    assert [(a, b) for a, b, _c in gaps] == [(4.0, 7.0), (7.0, 38.0)]
    assert gaps[0][2] == pytest.approx(3.0) and gaps[1][2] == pytest.approx(3.0)
    assert g.dead_gaps([], [], 3.0, 10.0, 12.0) == [(0.0, 12.0, pytest.approx(7.0))]


def test_order_key_sortable_and_template_durations():
    from tools import gates as g
    a = g.order_key_sortable("2026-10-11T14:03:22-04:00")
    b = g.order_key_sortable("2026-10-11T19:03:22Z")
    assert a[0] == 0 and a < b
    assert g.order_key_sortable("d01-s03") < g.order_key_sortable("d02-s01")
    assert g.order_key_sortable("") == (2, "")
    assert g._template_durations({"slots": [2.4, {"seconds": 1.2}, {"in": 1.0, "out": 3.0}]}) == [2.4, 1.2, 2.0]
    assert g._template_durations([1, 2]) == [1.0, 2.0] and g._template_durations({"x": 1}) is None
    tr = g.JsonTranscript({"a.mp4": [[0.0, 0.5, "hi"], {"start": 1.0, "end": 1.4, "text": "yo"}]})
    assert [w.text for w in tr.words("F:/x/a.mp4", 0.9, 2.0)] == ["yo"] and tr.has("a.mp4") and not tr.has("b.mp4")
