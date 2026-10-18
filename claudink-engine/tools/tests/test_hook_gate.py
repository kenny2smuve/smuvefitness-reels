"""Tests for tools/hook_gate.py (G10 / W5). Pure logic, no clips, no network."""
from __future__ import annotations

import json

import pytest

from tools import config, safeio
from tools import hook_gate as hg

# The burned list from WEAK_POINTS W5 (hooks.csv seeds)
BURNED = [
    "Best $1,200 you'll ever spend",
    "POV: you finally booked your dream tattoo",
    "Which one would you get? 1-7",
    "Cheap tattoos aren't good. Good tattoos aren't cheap.",
    "Rate these 1-10",
    "Booked out for a reason",
]

# Claude's Oct 17 BB list: 8 of 10 failed their own data (W5)
OCT17_FAILERS = [
    "Best $1,200 you'll ever spend",
    "POV: you finally booked your dream tattoo",
    "Which one would you get? 1-7",
    "Cheap tattoos aren't good. Good tattoos aren't cheap.",
    "Rate my work 1-10",
    "Booked out for a reason",
    "This is what $1,200 a day gets you",
    "Black and grey realism hits different",
]

WINNERS = [
    "Was it worth the 50 hours?",
    "Would u tap on this spot?",
    "He drove from Jacksonville for 10 hours",
]

CAPTIONS = [
    "Would you let your boy pick your next piece?\n\n" + config.CAPTION_TAIL,
    "Day 2 of the Berserk sleeve. Who else is building one?\n\n" + config.CAPTION_TAIL,
]


# --------------------------------------------------------------------------- normalize / similarity
def test_normalize_unifies_quotes_digits_emoji_and_whitespace():
    raw = "  Best  $1,200 you\u2019ll ever spend \U0001F525\U0001F525!!  "
    assert hg.normalize(raw) == "best $# you'll ever spend"
    assert hg.normalize("Rate these 1\u201310") == "rate these #-#"
    assert hg.normalize("Was it worth the 50 hours?") == "was it worth the # hours"
    assert hg.normalize("") == ""


def test_similarity_is_symmetric_and_bounded():
    a, b = "Would you sit 8 hours for one of these?", "Which one would you get? 1-7"
    s = hg.similarity(a, b)
    assert 0.0 <= s <= 1.0
    assert s == pytest.approx(hg.similarity(b, a))
    assert hg.similarity(a, a) == 1.0
    assert hg.similarity("", "anything") == 0.0


def test_fuzzy_match_catches_the_rewritten_dollar_hook():
    s = hg.similarity("Best $1200 you will ever spend", "Best $1,200 you'll ever spend")
    assert s >= config.HOOK_FUZZY_MATCH
    r = hg.check("Best $1200 you will ever spend", BURNED, [])
    assert not r["ok"]
    assert r["matched"] == "Best $1,200 you'll ever spend"
    assert r["similarity_max"] >= config.HOOK_FUZZY_MATCH
    assert "price" in r["losing_types"]


# --------------------------------------------------------------------------- the W5 data
@pytest.mark.parametrize("hook", OCT17_FAILERS)
def test_eight_of_ten_oct17_hooks_fail(hook):
    r = hg.check(hook, BURNED, CAPTIONS)
    assert not r["ok"], hook
    assert r["reasons"]


def test_only_the_eight_hour_question_passes_from_oct17():
    r = hg.check("Would you sit 8 hours for one of these?", BURNED, CAPTIONS)
    assert r["ok"], r["reasons"]
    assert r["has_number"] and r["has_stakes"] and r["addresses_viewer"]
    assert r["losing_types"] == []
    assert r["similarity_max"] < config.HOOK_FUZZY_MATCH


def test_stop_getting_small_tattoos_fails_without_number_stakes_or_viewer():
    r = hg.check("Stop getting small tattoos", BURNED, CAPTIONS)
    assert not r["ok"]
    assert not (r["has_number"] or r["has_stakes"] or r["addresses_viewer"])
    assert any("no number" in x for x in r["reasons"])


@pytest.mark.parametrize("hook", WINNERS)
def test_winners_pass(hook):
    r = hg.check(hook, BURNED, CAPTIONS)
    assert r["ok"], (hook, r["reasons"])
    assert r["has_number"] or r["has_stakes"] or r["addresses_viewer"]


def test_losing_types_carry_w5_evidence():
    names = [n for n, _rx, _ev in hg.LOSING_TYPES]
    assert names == ["price", "which-one", "brag", "booking", "reaction-bait"]
    for _n, _rx, ev in hg.LOSING_TYPES:
        assert "W" in ev and ev.strip()
    assert hg.losing_types("Books open for November") == ["booking"]
    assert hg.losing_types("Be honest") == ["reaction-bait"]
    assert hg.losing_types("Be honest, would you sit 8 hours?") == []      # has a number
    assert hg.losing_types("Did I cook?") == ["brag"]
    assert "price" in hg.losing_types("What does a sleeve cost")
    assert hg.losing_types("Pick one: 1-10") == ["which-one"]


def test_length_limit():
    sixty = "Would you sit through a full ten hour session for this one??"
    assert len(sixty) == 60
    r = hg.check(sixty, [], [])
    assert not r["ok"]
    assert r["length"] == len(sixty)
    assert any("length" in x for x in r["reasons"])
    short = "Would you sit 8 hours for this?"
    assert hg.check(short, [], [])["ok"]


def test_used_caption_blocks_a_recycled_angle():
    r = hg.check("Would you let your boy pick your next piece?", [], CAPTIONS)
    assert not r["ok"]
    assert r["matched"] is not None and "used caption" in r["reasons"][0]


# --------------------------------------------------------------------------- frame-1 match
def test_frame1_mismatch_fails_and_match_passes():
    hook = "Would you sit 8 hours for one of these?"
    bad = hg.check(hook, BURNED, [], frame1_tags=["greeting", "design"])
    assert bad["frame1_match"] is False and not bad["ok"]
    good = hg.check(hook, BURNED, [], frame1_tags=["stakes"])
    assert good["frame1_match"] is True and good["ok"]
    good2 = hg.check("He drove from Jacksonville for 10 hours", BURNED, [], frame1_tags=["travel"])
    assert good2["frame1_match"] is True and good2["ok"]
    pain = hg.check("Would u tap on this spot?", BURNED, [], frame1_tags=["needle"])
    assert pain["frame1_match"] is False


def test_frame1_without_keyword_is_none_and_passes_with_note():
    r = hg.check("Would you let your mom see this?", BURNED, [], frame1_tags=["greeting"])
    assert r["frame1_match"] is None
    assert r["ok"]
    assert r["notes"]


def test_frame1_not_checked_when_tags_not_given():
    r = hg.check("Would you sit 8 hours for one of these?", BURNED, [])
    assert r["frame1_match"] is None and not r["notes"]


# --------------------------------------------------------------------------- hours claim
def test_hours_not_in_pieces_fails():
    pieces = {"Shoulder": {"hours": "7", "sessions": "1"}, "Mudvayne": "10"}
    bad = hg.check("Would you sit 8 hours for one of these?", [], [], pieces_hours=pieces)
    assert not bad["ok"]
    assert any("hours not in pieces.csv: 8" in x for x in bad["reasons"])
    ok = hg.check("Would you sit 7 hours for one of these?", [], [], pieces_hours=pieces)
    assert ok["ok"], ok["reasons"]
    ok10 = hg.check("Was it worth the 10 hours?", [], [], pieces_hours=pieces)
    assert ok10["ok"]
    sessions = hg.check("Would you sit 1 session for this?", [], [], pieces_hours=pieces)
    assert sessions["ok"]
    no_claim = hg.check("Would u tap on this spot?", [], [], pieces_hours={})
    assert no_claim["ok"]
    unknown = hg.check("Was it worth the 50 hours?", [], [], pieces_hours={})
    assert not unknown["ok"]


def test_pieces_hours_from_rows_skips_unknown_and_filters():
    rows = [{"piece": "Shoulder", "hours": "7", "sessions": "1"},
            {"piece": "Virgin Mary", "hours": "", "sessions": ""},
            {"piece": "Mudvayne", "hours": "10", "sessions": "2"}]
    assert set(hg.pieces_hours_from_rows(rows)) == {"Shoulder", "Mudvayne"}
    assert set(hg.pieces_hours_from_rows(rows, only=["shoulder"])) == {"Shoulder"}
    assert hg.known_hour_values(hg.pieces_hours_from_rows(rows)) == {7.0, 1.0, 10.0, 2.0}


# --------------------------------------------------------------------------- pairs and caption tail
def test_check_pair_requires_two_angles():
    both = hg.check_pair("Was it worth the 50 hours?", "Would u tap on this spot?", BURNED, CAPTIONS)
    assert both["ok"], both["reasons"]
    same = hg.check_pair("Was it worth the 50 hours?", "Was it worth the 50 hours", BURNED, CAPTIONS)
    assert not same["ok"] and same["pair_similarity"] >= 0.85
    one_bad = hg.check_pair("Was it worth the 50 hours?", "Rate my work 1-10", BURNED, CAPTIONS)
    assert not one_bad["ok"] and one_bad["hook1"]["ok"] and not one_bad["hook2"]["ok"]


def test_caption_tail_exact_and_inexact():
    body = "Would you sit 8 hours for this?\n\n"
    assert hg.caption_tail_ok(body + config.CAPTION_TAIL)
    assert hg.caption_tail_ok(body + config.CAPTION_TAIL + "  \n")      # trailing whitespace only
    assert not hg.caption_tail_ok(body + config.CAPTION_TAIL.replace("fast!", "fast"))
    assert not hg.caption_tail_ok(body + config.CAPTION_TAIL.replace("#artist", "#artists"))
    assert not hg.caption_tail_ok(body + config.CAPTION_TAIL + " #miamitattoo")
    assert not hg.caption_tail_ok(body + config.CAPTION_TAIL.lower())
    assert not hg.caption_tail_ok(body)
    assert not hg.caption_tail_ok(None)


# --------------------------------------------------------------------------- ledgers + CLI
def _seed_ledgers(root):
    hooks_csv = config.LEDGERS / "hooks.csv"
    for h in BURNED:
        safeio.append_csv_row(hooks_csv, {"hook": h, "type": "broll", "used_on": "2026-10-07",
                                          "reel": "", "result_views": "", "status": "burned"},
                              ["hook", "type", "used_on", "reel", "result_views", "status"])
    caps_csv = config.LEDGERS / "captions_used.csv"
    for c in CAPTIONS:
        safeio.append_csv_row(caps_csv, {"caption": c, "used_on": "2026-10-07", "reel": ""},
                              ["caption", "used_on", "reel"])
    pieces_csv = config.LEDGERS / "pieces.csv"
    for piece, hours, sessions in (("Shoulder", "7", "1"), ("Mudvayne", "10", "2"), ("Virgin Mary", "", "")):
        safeio.append_csv_row(pieces_csv, {"piece": piece, "sessions": sessions, "hours": hours,
                                           "color": "no"},
                              ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"])
    return hooks_csv, caps_csv, pieces_csv


def test_burned_hooks_and_captions_load_from_ledgers(engine_root):
    _seed_ledgers(engine_root)
    assert sorted(hg.burned_hooks()) == sorted(BURNED)
    assert hg.used_captions() == CAPTIONS
    try:
        from tools import ledgers  # noqa: F401
        via = "ledgers"
    except ImportError:
        via = "csv"
    assert via in ("ledgers", "csv")


def test_cli_with_edl_json_and_exit_codes(engine_root, capsys):
    _seed_ledgers(engine_root)
    edl = {
        "name": "shoulder7h_A", "type": "story", "session": "Shoulder 7h",
        "segs": [{"clip": "x.mp4", "in": 0, "out": 2, "kind": "I", "order_key": "d01-s01",
                  "tags": ["greeting", "stakes"]}],
        "hooks": ["Would you sit 7 hours for one of these?", "He drove 2 hours for this shoulder"],
        "caption": "Would you sit 7 hours for this?\n\n" + config.CAPTION_TAIL,
        "pieces": ["Shoulder"],
    }
    edl_path = config.CACHE / "shoulder7h_A.edl.json"
    safeio.write_json(edl_path, edl)
    rc = hg.main(["--edl", str(edl_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1                                   # hook 2 claims 2 hours; Shoulder is 7
    assert out["hook1"]["ok"] and out["hook1"]["frame1_match"] is True
    assert not out["hook2"]["ok"]
    assert any("hours not in pieces.csv" in r for r in out["hook2"]["reasons"])
    assert out["caption_tail_ok"] is True
    assert out["burned_count"] == len(BURNED)

    # frame 1 shows greeting + stakes: a pain hook ("tap") is refused, a face hook passes
    rc = hg.main(["Would you sit 7 hours for one of these?", "Would u tap on this spot?",
                  "--edl", str(edl_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["hook2"]["frame1_match"] is False

    rc = hg.main(["Would you sit 7 hours for one of these?", "His face when he saw it after 7 hours",
                  "--edl", str(edl_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"], out["reasons"]
    assert out["hook2"]["frame1_match"] is True and out["pair_similarity"] < 0.85

    rc = hg.main(["Best $1200 you will ever spend"])
    text = capsys.readouterr().out
    assert rc == 1 and "FAIL" in text and "BLOCKED" in text

    rc = hg.main(["Was it worth the 50 hours?", "--caption", "no tail here", "--no-ledgers"])
    text = capsys.readouterr().out
    assert rc == 1 and "caption tail WRONG" in text

    rc = hg.main(["Was it worth the 50 hours?", "--no-ledgers"])
    text = capsys.readouterr().out
    assert rc == 0 and text.strip().endswith("OK")

    assert hg.main([]) == 1


def test_cli_explicit_csv_paths(tmp_path, capsys, engine_root):
    hooks_csv = config.CACHE / "h.csv"
    safeio.append_csv_row(hooks_csv, {"hook": "Would u tap on this spot?", "status": "ok"},
                          ["hook", "type", "used_on", "reel", "result_views", "status"])
    rc = hg.main(["Would u tap on this spot", "--hooks-csv", str(hooks_csv), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["hook1"]["matched"] == "Would u tap on this spot?"
