"""Tests for tools.edl: contract validation, ordering, transfer policy, out-point refinement, jump cuts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.tests.conftest import make_clip
from tools.tests.stub_transcript import StubTranscript


def _edl(segs, **kw):
    from tools import edl
    d = {"name": "t", "type": "story", "session": "S", "multi_day": False,
         "canvas": {"w": 1440, "h": 2560}, "hooks": ["Would you sit 7 hours?", "7 hours. Worth it?"],
         "caption": "c", "pieces": [], "labels": {}, "audio_from": None}
    d["segs"] = segs
    d.update(kw)
    return edl.EDL.from_dict(d)


def _seg(clip="a.mp4", i=0.0, o=1.0, kind="I", key="2026-10-11T14:00:00-04:00", tags=None, **kw):
    return {"clip": clip, "in": i, "out": o, "kind": kind, "order_key": key, "tags": tags or [], **kw}


K2 = "2026-10-11T14:05:00-04:00"
K3 = "2026-10-11T15:00:00-04:00"


# --------------------------------------------------------------------------- validate
def test_valid_edl_has_no_errors():
    from tools import edl
    e = _edl([_seg(o=2.0), _seg(i=3.0, o=4.0, key=K2, kind="M", tags=["needle"]),
              _seg(clip="MVI_1.mp4", kind="C", key=K3, tags=["finished"])])
    assert edl.validate(e) == []
    assert edl.duration(e) == 4.0
    tl = edl.timeline(e)
    assert [(t0, t1) for t0, t1, _ in tl] == [(0.0, 2.0), (2.0, 3.0), (3.0, 4.0)]


@pytest.mark.parametrize("patch,needle", [
    ({"name": ""}, "name"),
    ({"type": "short"}, "type"),
    ({"audio_from": "a.mp4"}, "audio_from"),
    ({"hooks": ["only one"]}, "hooks"),
    ({"hooks": "x"}, "hooks"),
    ({"canvas": {"w": 0, "h": 2560}}, "canvas"),
    ({"segs": []}, "segs"),
    ({"multi_day": "no"}, "multi_day"),
])
def test_validate_top_level_errors(patch, needle):
    from tools import edl
    segs = patch.pop("segs", [_seg()])
    e = _edl(segs, **patch)
    errs = edl.validate(e)
    assert errs and any(needle in x for x in errs), errs


@pytest.mark.parametrize("seg,needle", [
    (_seg(kind="X"), "kind"),
    (_seg(i=2.0, o=1.0), "must be <"),
    (_seg(i=-1.0, o=1.0), ">= 0"),
    (_seg(tags=["banana"]), "unknown tags"),
    (_seg(key=""), "order_key is required"),
    (_seg(clip=""), "clip is required"),
    ({"clip": "a.mp4", "in": "x", "out": 1, "kind": "I", "order_key": "d01-s01", "tags": []}, "numbers"),
])
def test_validate_segment_errors(seg, needle):
    from tools import edl
    errs = edl.validate(_edl([seg]))
    assert errs and any(needle in x for x in errs), errs


def test_validate_order_and_canon_rules():
    from tools import edl
    back = _edl([_seg(key=K2), _seg(key="2026-10-11T14:00:00-04:00")])
    assert any("backwards" in x for x in edl.validate(back))
    mixed = _edl([_seg(key="2026-10-11T14:00:00-04:00"), _seg(i=2.0, o=3.0, key="d01-s02")])
    msg = [x for x in edl.validate(mixed) if "mix classes" in x]
    assert msg and "ISO time in segs [0]" in msg[0] and "dNN-sMM in segs [1]" in msg[0], msg
    c_first = _edl([_seg(kind="C"), _seg(kind="I", key=K2)])
    assert any("after a C" in x for x in edl.validate(c_first))
    ok = _edl([_seg(key="d01-s01", o=1.0), _seg(key="D1-S1", i=1.0, o=2.0),   # same key, normalised
               _seg(key="d02-s01", kind="C", i=5.0, o=6.0)])
    assert edl.validate(ok) == []
    broll = _edl([_seg(kind="M", key="d01-s01", tags=["station"])], type="broll", hooks=[])
    assert edl.validate(broll) == []
    bad_export = _edl([_seg()], export={"transfer": "hdr10"})
    assert any("export.transfer" in x for x in edl.validate(bad_export))
    assert any("forced" in x for x in edl.validate(_edl([_seg()], export={"forced": True})))


def test_validate_reports_same_clip_dialogue_overlap():
    from tools import edl
    # two consecutive I segments on one clip sharing 0.4 s: the same words would play twice
    e = _edl([_seg(i=1.0, o=2.4), _seg(i=2.0, o=2.5, key=K2)])
    errs = edl.validate(e)
    assert len(errs) == 1 and "overlaps segs[0]" in errs[0] and "0.400 s" in errs[0], errs
    # I then M on the same clip overlapping: still an error; M then M (b-roll shots) is a montage choice
    assert any("overlaps" in x for x in edl.validate(_edl([_seg(i=1.0, o=2.4), _seg(i=2.0, o=2.5, kind="M", key=K2)])))
    assert edl.validate(_edl([_seg(i=0.0, o=1.6, kind="M", key="d01-s01", tags=["piece"]),
                              _seg(i=1.5, o=2.7, kind="M", key="d01-s02", tags=["station"])],
                             type="broll", hooks=[])) == []
    # touching ranges and other clips are fine
    assert edl.validate(_edl([_seg(i=1.0, o=2.0), _seg(i=2.0, o=2.5, key=K2)])) == []
    assert edl.validate(_edl([_seg(i=1.0, o=2.4), _seg(clip="b.mp4", i=2.0, o=2.5, key=K2)])) == []


# --------------------------------------------------------------------------- ordering
def test_order_key_sortable_classes_and_order():
    from tools import edl
    a = edl.order_key_sortable("2026-10-11T14:03:22-04:00")
    b = edl.order_key_sortable("2026-10-11T18:03:22+00:00")       # same instant, other offset
    c = edl.order_key_sortable("2026-10-11T14:03:23-0400")        # compact offset form
    assert a[0] == 0 and a == b and c > a
    assert edl.order_key_sortable("d01-s03") == (1, "d01-s03")
    assert edl.order_key_sortable("d01-s03") < edl.order_key_sortable("d02-s01")
    assert edl.order_key_sortable("whatever")[0] == 2
    naive = edl.order_key_sortable("2026-10-11T14:03:22")          # taken as config.TZ
    assert naive == a
    # hand-typed day-stage keys: case-insensitive, 1-2 digits, normalised to dNN-sMM
    assert edl.order_key_sortable("D01-S01") == (1, "d01-s01")
    assert edl.order_key_sortable("d1-s1") == (1, "d01-s01")
    assert edl.order_key_sortable("d1-s10") < edl.order_key_sortable("D02-S01")
    assert edl.order_key_sortable("d001-s01")[0] == 2               # three digits is not the form


# --------------------------------------------------------------------------- transfer policy
def test_expected_transfer_policy():
    from tools import edl
    probe = {"a.mp4": {"is_hlg": True}, "b.mp4": {"transfer": "arib-std-b67"}, "MVI_1.mp4": {"transfer": "bt709"}}
    mix = _edl([_seg("a.mp4"), _seg("b.mp4", key=K2), _seg("MVI_1.mp4", kind="C", key=K3)])
    assert edl.expected_transfer(mix, probe) == "sdr"
    assert mix.export["transfer"] == "sdr" and "Canon" in mix.export["reason"]
    assert "START_HERE 12" in mix.export["policy_note"]            # the daily run logs this difference
    hlg = _edl([_seg("a.mp4"), _seg("b.mp4", key=K2)])
    assert edl.expected_transfer(hlg, lambda c: probe[c]) == "hlg" and "policy_note" not in hlg.export
    sdr = _edl([_seg("MVI_1.mp4")])
    assert edl.expected_transfer(sdr, probe) == "sdr" and "no conversion" in sdr.export["reason"]
    # a force needs forced: true ...
    forced = _edl([_seg("a.mp4"), _seg("MVI_1.mp4", kind="C", key=K3)], export={"transfer": "hlg", "forced": True})
    assert edl.expected_transfer(forced, probe) == "hlg" and "forced" in forced.export["reason"]
    assert forced.export["forced"] is True and forced.export["transfer_decided"] == "hlg"
    # ... a bare transfer is only the record of an earlier render, never a force
    bare = _edl([_seg("a.mp4"), _seg("MVI_1.mp4", kind="C", key=K3)], export={"transfer": "hlg"})
    assert edl.expected_transfer(bare, probe) == "sdr" and edl.forced_transfer(bare.export) is None
    # a dict probe that misses a clip is an error, not "SDR"
    with pytest.raises(KeyError, match="c.mp4"):
        edl.expected_transfer(_edl([_seg("a.mp4"), _seg("c.mp4", key=K2)]), probe)


# --------------------------------------------------------------------------- load / save / roundtrip
def test_load_save_roundtrip_and_strict(engine_root, tmp_path):
    from tools import config, edl
    e = _edl([_seg(o=2.0, note="hi", bonus=1)], extra_key="kept")
    p = config.CACHE / "x.edl.json"
    edl.save(e, p)
    back = edl.load(p)
    assert back.to_dict() == e.to_dict()
    assert back.segs[0].extra == {"bonus": 1} and back.extra == {"extra_key": "kept"}
    edl.save(back, p)                                  # sidecar overwrite is allowed
    bad = config.CACHE / "bad.json"
    bad.write_text(json.dumps({"name": "b", "type": "story", "segs": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        edl.load(bad)
    assert edl.load(bad, strict=False).segs == []
    with pytest.raises(Exception):
        edl.save(e, tmp_path / "outside.json")         # outside the engine roots


# --------------------------------------------------------------------------- refine_outpoints
NO_ENERGY = lambda clip, t0, t1: []  # noqa: E731


def test_refine_out_lands_in_the_pause_both_directions():
    from tools import edl
    st = StubTranscript({"a.mp4": [(2.0, 2.4, "seven"), (2.5, 2.95, "hours."), (4.0, 4.3, "ok")]})
    # clipped last word: out 2.90 -> 2.95 + OUT_PAD_S
    e = _edl([_seg(i=1.0, o=2.90)])
    r = edl.refine_outpoints(e, st, audio_energy=NO_ENERGY)
    assert r.segs[0].out == pytest.approx(3.07, abs=1e-3)
    assert r.segs[0].in_ == 1.0 and r.segs[0].extra["refined"] == {"in": 1.0, "out": 2.9}
    assert e.segs[0].out == 2.90                       # input untouched
    # dead air after the line: out 3.50 is pulled back to the same 3.07 and the pull is recorded (6A)
    e2 = _edl([_seg(i=1.0, o=3.50)])
    r2 = edl.refine_outpoints(e2, st, audio_energy=NO_ENERGY)
    assert r2.segs[0].out == pytest.approx(3.07, abs=1e-3)
    assert r2.segs[0].extra["refined"]["pulled_back_s"] == pytest.approx(0.43, abs=1e-3)
    # even a short tail (0.3 s) is pulled back: nothing is kept on the transcript alone
    e3 = _edl([_seg(i=1.0, o=3.25)])
    r3 = edl.refine_outpoints(e3, st, audio_energy=NO_ENERGY)
    assert r3.segs[0].out == pytest.approx(3.07, abs=1e-3) and "pulled_back_s" in r3.segs[0].extra["refined"]
    # a segment with no word inside is left alone and flagged
    e4 = _edl([_seg(i=3.2, o=3.8)])
    r4 = edl.refine_outpoints(e4, st, audio_energy=NO_ENERGY)
    assert (r4.segs[0].in_, r4.segs[0].out) == (3.2, 3.8) and r4.segs[0].extra["refined"]["skipped"]


def test_refine_finishes_the_sentence_within_cap():
    from tools import edl
    st = StubTranscript({"a.mp4": [(2.5, 2.95, "sit"), (3.1, 3.4, "down"), (3.5, 3.8, "please."), (4.5, 4.8, "so")]})
    e = _edl([_seg(i=1.0, o=2.90)])
    r = edl.refine_outpoints(e, st, audio_energy=NO_ENERGY)
    assert r.segs[0].out == pytest.approx(3.92, abs=1e-3)
    # gap > 0.35 s: the sentence is not followed
    st2 = StubTranscript({"a.mp4": [(2.5, 2.95, "sit"), (3.4, 3.7, "down.")]})
    r2 = edl.refine_outpoints(e, st2, audio_energy=NO_ENERGY)
    assert r2.segs[0].out == pytest.approx(3.07, abs=1e-3)
    # the cap (OUT_SENTENCE_MAX_S) stops a run-on sentence on a whole word
    st3 = StubTranscript({"a.mp4": [(2.5, 2.95, "a"), (3.0, 3.5, "b"), (3.6, 4.0, "c"), (4.1, 4.6, "d"), (4.7, 5.2, "e.")]})
    r3 = edl.refine_outpoints(e, st3, audio_energy=NO_ENERGY)
    assert r3.segs[0].out == pytest.approx(4.12, abs=1e-3)


def test_refine_in_point_moves_to_word_onset_except_opener():
    from tools import edl
    words = [(1.0, 1.4, "hey."), (2.5, 2.9, "yes.")]
    st = StubTranscript({"a.mp4": words, "b.mp4": words})
    e = _edl([_seg(i=1.2, o=1.5), _seg(clip="b.mp4", i=1.2, o=1.5, key=K2)])
    r = edl.refine_outpoints(e, st, audio_energy=NO_ENERGY)
    assert r.segs[0].in_ == 1.2                         # opener never gets dead lead-in
    assert r.segs[1].in_ == pytest.approx(1.0)          # second line starts on the word onset
    assert r.segs[1].out == pytest.approx(1.52, abs=1e-3)
    st2 = StubTranscript({"a.mp4": [(1.0, 2.0, "heeey."), (2.5, 2.9, "yes.")],
                          "b.mp4": [(1.0, 2.0, "heeey."), (2.5, 2.9, "yes.")]})
    e2 = _edl([_seg(i=1.2, o=1.5), _seg(clip="b.mp4", i=1.9, o=2.2, key=K2)])
    r2 = edl.refine_outpoints(e2, st2, audio_energy=NO_ENERGY)
    assert r2.segs[1].in_ == pytest.approx(1.5)         # capped at IN_ONSET_MAX_S


def test_refine_same_clip_guards():
    from tools import edl
    st = StubTranscript({"a.mp4": [(1.0, 1.4, "hey."), (1.6, 2.0, "there"), (2.1, 2.5, "friend.")]})
    # out is capped at the next segment's in on the same clip (the sentence would run into it)
    e = _edl([_seg(i=1.0, o=1.7), _seg(i=2.0, o=2.5, key=K2)])
    r = edl.refine_outpoints(e, st, audio_energy=NO_ENERGY)
    assert [(s.in_, s.out) for s in r.segs] == [(1.0, 2.0), (2.0, 2.62)]
    assert r.segs[0].extra["refined"]["out_capped_at_next_in"] == 2.0 and edl.validate(r) == []
    # in never moves before the previous segment's refined out
    e2 = _edl([_seg(i=1.0, o=1.7), _seg(i=1.8, o=2.5, key=K2)])
    r2 = edl.refine_outpoints(e2, st, audio_energy=NO_ENERGY)
    assert [(s.in_, s.out) for s in r2.segs] == [(1.0, 1.8), (1.8, 2.62)]
    assert r2.segs[1].extra["refined"]["in_clamped_to_prev_out"] == 1.8 and edl.validate(r2) == []
    # the editor overlapped two lines: the overlap is recorded and validate() reports it
    e3 = _edl([_seg(i=1.0, o=2.4), _seg(i=2.2, o=2.5, key=K2)])
    r3 = edl.refine_outpoints(e3, st, audio_energy=NO_ENERGY)
    assert r3.segs[0].out == pytest.approx(2.62) and r3.segs[1].in_ == 2.2
    assert r3.segs[1].extra["refined"]["overlap_with_prev_s"] == pytest.approx(0.42, abs=1e-3)
    assert any("overlaps segs[0]" in x for x in edl.validate(r3))


def test_refine_energy_extension_with_injected_and_ffmpeg_energy(engine_root, tmp_path):
    from tools import edl
    # injected energy: loud until 1.5 s, next word at 2.0 -> stops at 1.5 (< limit 1.97)
    def energy(clip, t0, t1):
        t, out = t0, []
        while t < t1:
            out.append((round(t, 3), 0.5 if t < 1.5 else 0.01))
            t += 0.01
        return out
    st = StubTranscript({"a.mp4": [(1.0, 1.2, "hello."), (2.0, 2.3, "there.")]})
    e = _edl([_seg(i=0.9, o=1.2)])
    r = edl.refine_outpoints(e, st, audio_energy=energy)
    assert 1.45 <= r.segs[0].out <= 1.56
    # max extension: energy never drops, next word far away -> +OUT_EXTEND_MAX_S past 1.32
    loud = lambda clip, t0, t1: [(round(t0 + i * 0.01, 3), 0.5) for i in range(int((t1 - t0) / 0.01) + 2)]  # noqa: E731
    st_far = StubTranscript({"a.mp4": [(1.0, 1.2, "hello."), (4.0, 4.3, "there.")]})
    r2 = edl.refine_outpoints(e, st_far, audio_energy=loud)
    assert r2.segs[0].out == pytest.approx(1.32 + 0.8, abs=0.02)
    # the default ffmpeg energy on the "speech" fixture: bursts on for 0.5 s every 1 s
    clip = make_clip(tmp_path / "fixtures" / "video-009.mp4", seconds=3.0, audio="speech")
    frames = edl.ffmpeg_voice_energy(clip, 0.8, 1.8)
    assert frames and frames[0][0] == pytest.approx(0.8, abs=0.02)
    on = [r for t, r in frames if 1.1 <= t <= 1.4]
    off = [r for t, r in frames if 1.6 <= t <= 1.75]
    assert min(on) > 5 * max(off)
    st_real = StubTranscript({clip.name: [(1.0, 1.2, "hello."), (2.0, 2.3, "there.")]})
    e_real = _edl([_seg(clip=str(clip), i=0.9, o=1.2)])
    r3 = edl.refine_outpoints(e_real, st_real)
    assert 1.44 <= r3.segs[0].out <= 1.58, r3.segs[0].out


def test_refine_leaves_m_and_c_segments_alone():
    from tools import edl
    st = StubTranscript({"a.mp4": [(0.5, 2.9, "long.")]})
    e = _edl([_seg(kind="M", o=1.0), _seg(kind="C", i=0.2, o=1.0, key=K3)])
    r = edl.refine_outpoints(e, st, audio_energy=NO_ENERGY)
    assert [(s.in_, s.out) for s in r.segs] == [(0.0, 1.0), (0.2, 1.0)]


# --------------------------------------------------------------------------- jumpcut_silences
def test_jumpcut_splits_on_long_gap_and_copies_fields():
    from tools import edl
    st = StubTranscript({"a.mp4": [(0.2, 0.5, "so"), (1.6, 1.9, "yeah"), (2.0, 2.3, "right.")]})
    e = _edl([_seg(i=0.0, o=2.5, tags=["humor"], note="n")])
    r = edl.jumpcut_silences(e, st)
    assert [(s.in_, s.out) for s in r.segs] == [(0.0, 0.65), (1.45, 2.5)]
    assert all(s.order_key == e.segs[0].order_key and s.tags == ["humor"] and s.note == "n" and s.kind == "I"
               for s in r.segs)
    assert edl.validate(r) == []
    # a 0.5 s gap is kept
    st2 = StubTranscript({"a.mp4": [(0.2, 0.5, "so"), (1.0, 1.3, "yeah.")]})
    assert len(edl.jumpcut_silences(e, st2).segs) == 1


# --------------------------------------------------------------------------- CLI
def test_cli_validate_timeline_refine(engine_root, capsys):
    from tools import config, edl
    p = config.CACHE / "cli.edl.json"
    edl.save(_edl([_seg(i=1.0, o=2.9, clip="a.mp4"), _seg(i=0.0, o=1.0, kind="C", clip="MVI_1.mp4", key=K3)]), p)
    assert edl.main(["validate", str(p)]) == 0
    assert "OK" in capsys.readouterr().out
    assert edl.main(["validate", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    bad = config.CACHE / "bad.edl.json"
    bad.write_text(json.dumps({"name": "", "type": "story", "segs": [_seg(kind="Q")]}), encoding="utf-8")
    assert edl.main(["validate", str(bad)]) == 1
    assert "INVALID" in capsys.readouterr().out
    assert edl.main(["timeline", str(p), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["duration"] == 2.9 and out["segs"][1]["t0"] == 1.9
    words = config.CACHE / "words.json"
    words.write_text(json.dumps({"a.mp4": [[2.0, 2.4, "seven"], [2.5, 2.95, "hours."]]}), encoding="utf-8")
    dst = config.CACHE / "cli.refined.edl.json"
    assert edl.main(["refine", str(p), "--transcript", str(words), "--out", str(dst), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["changed"] and edl.load(dst).segs[0].out == pytest.approx(3.07, abs=1e-3)


def test_cli_refine_jumpcut_splits_segments(engine_root, capsys):
    from tools import config, edl
    p = config.CACHE / "jc.edl.json"
    edl.save(_edl([_seg(i=0.0, o=2.5, clip="a.mp4", tags=["humor"])]), p)
    words = config.CACHE / "jc_words.json"
    words.write_text(json.dumps({"a.mp4": [[0.2, 0.5, "so"], [1.6, 1.9, "yeah"], [2.0, 2.3, "right."]]}),
                     encoding="utf-8")
    dst = config.CACHE / "jc.refined.edl.json"
    assert edl.main(["refine", str(p), "--transcript", str(words), "--out", str(dst), "--jumpcut", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    got = edl.load(dst)
    assert res["segs"] == 2 and len(got.segs) == 2
    assert got.segs[0].in_ == 0.0 and got.segs[0].out == pytest.approx(0.62, abs=1e-3)   # "so" + pad
    assert got.segs[1].in_ == pytest.approx(1.45) and got.segs[1].out == pytest.approx(2.42, abs=1e-3)
    assert all(s.tags == ["humor"] and s.extra.get("jumpcut") for s in got.segs)
    # without --jumpcut the 1.0 s gap stays inside one segment
    dst2 = config.CACHE / "jc.plain.edl.json"
    assert edl.main(["refine", str(p), "--transcript", str(words), "--out", str(dst2)]) == 0
    assert len(edl.load(dst2).segs) == 1
