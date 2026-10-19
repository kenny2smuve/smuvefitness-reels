"""Tests for tools/deliver.py (section 9 delivery, time rules, HOLD queue, POST_THIS,
re-upload guard incl. legacy rows, hook/caption ledgers, Drive mirror retry, CLI)."""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import config, safeio
from tools.tests.conftest import make_clip

pytest.importorskip("PIL", reason="deliver needs pillow for the frame-1 phash")
pytest.importorskip("numpy", reason="deliver needs numpy for the frame-1 phash")

TAIL = config.CAPTION_TAIL
NOW = datetime(2026, 10, 19, 6, 40, tzinfo=config.TZ)
A_AT = datetime(2026, 10, 19, 11, 0, tzinfo=config.TZ)
B_AT = datetime(2026, 10, 19, 14, 30, tzinfo=config.TZ)
HOOKS_A = ["Would you sit 7 hours for this?", "7 hours in one sitting. Worth it?"]
HOOKS_B = ["Would you sit through this one?", "Healed at 3 months. Still clean?"]
HOOKS_C = ["Could you handle the ribs?", "Ribs or spine: which hurts more?"]
CAPTION = "Would you sit 7 hours for one piece?\n\n" + TAIL
CAPTION_B = "Healed at 3 months. Would you trust it to stay this clean?\n\n" + TAIL
CAPTION_C = "Ribs or spine, which one would you pick first?\n\n" + TAIL
LEDGER_HEADER = ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2",
                 "caption", "status", "posted_shortcode", "views_24h", "views_72h",
                 "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"]


@pytest.fixture(scope="module")
def base_clips(tmp_path_factory):
    """Three short clips with different first frames, built once per module."""
    d = tmp_path_factory.mktemp("dclips")
    return {
        "story": make_clip(d / "story.mp4", seconds=2, w=540, h=960, fps=15, pattern="testsrc2"),
        "broll": make_clip(d / "broll.mp4", seconds=2, w=540, h=960, fps=15, pattern="smptebars"),
        "third": make_clip(d / "third.mp4", seconds=2, w=540, h=960, fps=15, pattern="rgbtestsrc"),
    }


def _sidecars(reel: Path, *, rtype: str = "story", session: str = "Shoulder 7h",
              pieces=("Shoulder",), labels=None, passed: bool = True) -> None:
    """gates.json + edl.json next to a fixture reel (plain writes: fixtures live outside roots)."""
    gates = {"reel": str(reel), "type": rtype, "pass": passed,
             "gates": [{"id": "G1", "name": "frame1", "hard": True, "ok": passed,
                        "detail": "" if passed else "luma std 41.2 < 60 (dark opener)"}]}
    edl = {"name": reel.stem, "type": rtype, "session": session, "pieces": list(pieces),
           "labels": dict(labels or {}), "segs": [], "hooks": [], "caption": ""}
    reel.with_name(reel.stem + ".gates.json").write_text(json.dumps(gates), encoding="utf-8")
    reel.with_name(reel.stem + ".edl.json").write_text(json.dumps(edl), encoding="utf-8")


def _work(tmp_path: Path, base_clips: dict, which: str, name: str, **kw) -> tuple[Path, Path]:
    """A per-test copy of a base clip as <name>.mp4 + <name>_chat.mp4 with sidecars."""
    w = tmp_path / "work"
    w.mkdir(exist_ok=True)
    reel = w / f"{name}.mp4"
    chat = w / f"{name}_chat.mp4"
    shutil.copy(base_clips[which], reel)
    shutil.copy(base_clips[which], chat)
    _sidecars(reel, **kw)
    return reel, chat


def _pieces_csv():
    safeio.append_csv_row(config.LEDGERS / "pieces.csv",
                          {"piece": "Shoulder", "sessions": "1", "hours": "7"},
                          ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"])
    safeio.append_csv_row(config.LEDGERS / "pieces.csv",
                          {"piece": "Mudvayne", "sessions": "", "hours": ""},
                          ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"])


def _rows(name: str = "deliveries") -> list[dict]:
    p = config.LEDGERS / f"{name}.csv"
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _his_post(iso: str, mid: str = "1") -> None:
    safeio.append_csv_row(config.DATA / "reels.csv", {"media_id": mid, "posted_et": iso},
                          ["media_id", "posted_et"])


def _deliver_a(dl, tmp_path, base_clips, **over):
    reel, chat = _work(tmp_path, base_clips, "story", "shoulder7h_A",
                       labels={"Shoulder": "1 session / 7 hours"})
    kw = dict(slot="A", when=A_AT, kind="SEND", hooks=HOOKS_A, caption=CAPTION, now=NOW,
              tests="X1 arm B: A at 11:00 alone")
    kw.update(over)
    return dl.deliver(reel, chat, **kw)


def _deliver_b(dl, tmp_path, base_clips, **over):
    reel, chat = _work(tmp_path, base_clips, "broll", "mudvayne_B", rtype="broll", session="",
                       pieces=("Mudvayne",))
    kw = dict(slot="B", when=B_AT, kind="SAVE", hooks=HOOKS_B, caption=CAPTION_B, now=NOW,
              tests="X2: hour labels on")
    kw.update(over)
    return dl.deliver(reel, chat, **kw)


# --------------------------------------------------------------------------- full delivery
def test_full_deliver_a_then_b(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    safeio.write_text(config.POST / "SHOOT_CARD.txt", "SHOOT CARD\n- say the hours out loud\n")

    ra = _deliver_a(dl, tmp_path, base_clips)
    assert ra["ok"], ra
    day = config.POST / "2026-10-19"
    assert (day / "A_1100_shoulder7h-a.mp4").is_file()
    assert (day / "A_1100_shoulder7h-a_chat.mp4").is_file()
    assert (day / "A_1100_shoulder7h-a.edl.json").is_file()
    assert (day / "A_1100_shoulder7h-a.gates.json").is_file()
    assert ra["id"] == "2026-10-19_A_shoulder7h-a"

    rb = _deliver_b(dl, tmp_path, base_clips)
    assert rb["ok"], rb
    assert (day / "B_1430_mudvayne-b.mp4").is_file()

    rows = _rows()
    assert [r["slot"] for r in rows] == ["A", "B"]
    assert all(r["status"] == "delivered" for r in rows)
    assert all(len(r["phash"]) == 16 and int(r["phash"], 16) >= 0 for r in rows)
    assert rows[0]["hook1"] == HOOKS_A[0] and rows[0]["hook2"] == HOOKS_A[1]
    assert rows[0]["tests"] == "X1 arm B: A at 11:00 alone"
    assert rows[0]["file"] == "post/2026-10-19/A_1100_shoulder7h-a.mp4"
    assert rows[0]["type"] == "story" and rows[1]["type"] == "broll"

    # hooks.csv and captions_used.csv record what went out (FOLLOWUP 15)
    hk = _rows("hooks")
    assert [(h["hook"], h["status"], h["used_on"], h["reel"]) for h in hk] == [
        (HOOKS_A[0], "ok", "2026-10-19", ra["id"]), (HOOKS_A[1], "ok", "2026-10-19", ra["id"]),
        (HOOKS_B[0], "ok", "2026-10-19", rb["id"]), (HOOKS_B[1], "ok", "2026-10-19", rb["id"])]
    cu = _rows("captions_used")
    assert [(c["caption"], c["used_on"], c["reel"]) for c in cu] == [
        (CAPTION, "2026-10-19", ra["id"]), (CAPTION_B, "2026-10-19", rb["id"])]

    # Drive mirror: the four files + POST_THIS
    drive = config.DRIVE_POST / "2026-10-19"
    for n in ("A_1100_shoulder7h-a.mp4", "A_1100_shoulder7h-a_chat.mp4", "A_1100_shoulder7h-a.edl.json",
              "A_1100_shoulder7h-a.gates.json", "B_1430_mudvayne-b.mp4", "POST_THIS.txt"):
        assert (drive / n).is_file(), n

    # the signature store knows both deliveries
    store = json.loads((config.DATA / "signatures.json").read_text(encoding="utf-8"))
    assert set(store["signatures"]) == {ra["id"], rb["id"]}

    txt = (day / "POST_THIS.txt").read_text(encoding="utf-8")
    assert txt == (drive / "POST_THIS.txt").read_text(encoding="utf-8")
    lines = txt.splitlines()
    a_idx = lines.index("A - post at 11:00 ET, alone")
    b_idx = lines.index("B - post at least 3h after you post A (earliest 14:30 ET)")
    assert a_idx < b_idx
    assert lines[a_idx + 1] == "Type: SEND"
    assert lines[a_idx + 2] == "Hook 1: " + HOOKS_A[0]
    assert lines[a_idx + 3] == "Hook 2: " + HOOKS_A[1]
    assert lines[a_idx + 4] == "Caption:"
    assert lines[a_idx + 5] == "Would you sit 7 hours for one piece?"
    assert lines[a_idx + 7] == TAIL
    assert "Labels: Shoulder: 1 session / 7 hours" in lines[a_idx:b_idx]
    assert "Add a trending sound at post time." in lines[a_idx:b_idx]
    assert "Share to grid if it is at 1.5x median at ~3h." in lines[a_idx:b_idx]
    assert "Tests: X1 arm B: A at 11:00 alone" in lines[a_idx:b_idx]
    assert "TikTok: post the same file and caption there yourself (Metricool cap resets Nov 1)." in lines
    assert "Reply with hours/sessions for: Mudvayne" in lines[b_idx:]
    assert not any(l.startswith("Reply with hours") for l in lines[a_idx:b_idx])
    assert lines[b_idx + 1] == "Type: SAVE"
    assert not any(l.startswith("Labels:") for l in lines[b_idx:])
    assert txt.rstrip().endswith("SHOOT CARD\n- say the hours out loud")
    assert not any(l.startswith("BLOCKED") for l in lines)
    assert "(caption tail is NOT exact" not in txt


def test_tiktok_line_absent_after_nov_1_and_blocked_breakout_lines(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    later = datetime(2026, 11, 2, 7, 0, tzinfo=config.TZ)
    txt = dl.post_this_text("2026-10-19", later)
    assert "TikTok:" not in txt
    assert "A - post at 11:00 ET, alone" in txt

    # blocked.txt: EVERY non-empty line (the 5.11 stock warning is the second line), then
    # notice.txt verbatim, then breakout < 24h -> "Share ... to the grid"
    safeio.write_text(config.STATE / "blocked.txt",
                      "PC was asleep at 10:05; relay missed A\n\n"
                      "BLOCKED: Upload your last session's glasses clips to Drive\n")
    safeio.write_text(config.STATE / "notice.txt", "Stock: 2 sessions left, shoot this week\n")
    safeio.write_json(config.STATE / "breakout.json",
                      {"shortcode": "DehJPlnKk8I", "at": (NOW - timedelta(hours=2)).isoformat()})
    lines = dl.post_this_text("2026-10-19", NOW).splitlines()
    assert lines[0] == "BLOCKED: PC was asleep at 10:05; relay missed A"
    assert lines[1] == "BLOCKED: Upload your last session's glasses clips to Drive"
    assert lines[2] == "Stock: 2 sessions left, shoot this week"
    assert lines[3] == "Share DehJPlnKk8I to the grid"
    assert lines[4] == ""
    # breakout older than 24h is dropped
    safeio.write_json(config.STATE / "breakout.json",
                      {"shortcode": "DehJPlnKk8I", "at": (NOW - timedelta(hours=30)).isoformat()},
                      overwrite=True)
    assert "to the grid" not in dl.post_this_text("2026-10-19", NOW)


def test_relay_passed_blocked_line(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    late = datetime(2026, 10, 19, 10, 30, tzinfo=config.TZ)       # after the 10:05 A relay
    r = _deliver_a(dl, tmp_path, base_clips, now=late, when=datetime(2026, 10, 19, 11, 30, tzinfo=config.TZ))
    assert r["ok"], r
    lines = Path(r["post_this"]).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "BLOCKED: relay passed: take A from the Drive folder"
    assert "A - post at 11:30 ET, alone" in lines


# --------------------------------------------------------------------------- refusals
def test_cap_and_same_slot_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    # same slot letter again (different reel)
    reel, chat = _work(tmp_path, base_clips, "third", "akira_A", session="Akira")
    r = dl.deliver(reel, chat, "A", A_AT, "SEND", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("slot A already delivered" in x for x in r["refused"])
    assert _deliver_b(dl, tmp_path, base_clips)["ok"]
    # third reel of the day: the cap
    reel, chat = _work(tmp_path, base_clips, "third", "third_th", rtype="th", session="Talking head")
    r = dl.deliver(reel, chat, "A", datetime(2026, 10, 19, 18, 0, tzinfo=config.TZ), "SEND",
                   HOOKS_C, CAPTION_C, NOW)
    assert not r["ok"]
    assert any("already holds 2" in x for x in r["refused"]), r
    assert len(_rows()) == 2
    assert not list((config.POST / "2026-10-19").glob("*third*"))


def test_time_rules_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    # a slot that has already passed (section 10), under MIN_LEAD_MINUTES, and after 21:00
    r = _deliver_a(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 6, 0, tzinfo=config.TZ))
    assert not r["ok"] and any("has passed or is under 45 min" in x for x in r["refused"]), r
    r = _deliver_a(dl, tmp_path, base_clips, when=NOW + timedelta(minutes=30))
    assert not r["ok"] and any("has passed or is under 45 min" in x for x in r["refused"])
    r = _deliver_a(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 23, 0, tzinfo=config.TZ))
    assert not r["ok"] and any("at or after 21:00 ET" in x for x in r["refused"])
    assert _rows() == []
    # tomorrow's slot is fine even when its clock time is earlier than now (next-day delivery)
    r = _deliver_a(dl, tmp_path, base_clips, when=datetime(2026, 10, 20, 7, 0, tzinfo=config.TZ),
                   now=datetime(2026, 10, 19, 18, 0, tzinfo=config.TZ))
    assert r["ok"], r
    assert Path(r["files"]["reel"]).name == "A_0700_shoulder7h-a.mp4"
    # B inside 3h of that A is refused (14:30 is fine: delivered in the next test)
    r = _deliver_b(dl, tmp_path, base_clips, when=datetime(2026, 10, 20, 9, 0, tzinfo=config.TZ),
                   now=datetime(2026, 10, 19, 18, 0, tzinfo=config.TZ))
    assert not r["ok"] and any("under 3h from slot A at 07:00" in x for x in r["refused"]), r
    r = _deliver_b(dl, tmp_path, base_clips, when=datetime(2026, 10, 20, 10, 0, tzinfo=config.TZ),
                   now=datetime(2026, 10, 19, 18, 0, tzinfo=config.TZ))
    assert r["ok"], r


def test_short_lead_forced_and_logged(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    r = _deliver_a(dl, tmp_path, base_clips, when=NOW + timedelta(minutes=20),
                   force_reason="relay already waiting")
    assert r["ok"], r
    note = _rows()[0]["notes"]
    assert "FORCED past 'slot 07:00" in note and "relay already waiting" in note
    # the latest-hour rule cannot be forced
    r = _deliver_b(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 21, 30, tzinfo=config.TZ),
                   force_reason="whatever")
    assert not r["ok"] and any("at or after 21:00" in x for x in r["refused"])


def test_his_own_posts_shift_or_block_the_slot(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    # he posted at 10:30 (data/reels.csv): A at 11:00 is inside 3h, 13:30 is fine
    _his_post("2026-10-19T10:30:00-04:00")
    r = _deliver_a(dl, tmp_path, base_clips)
    assert not r["ok"] and any("he posted at 10:30 ET" in x and "shift the slot" in x for x in r["refused"]), r
    assert _rows() == []
    r = _deliver_a(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 13, 30, tzinfo=config.TZ))
    assert r["ok"], r
    # his post + our A = 2: a B is over the cap even 3h later
    r = _deliver_b(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 17, 0, tzinfo=config.TZ))
    assert not r["ok"] and any("he posted 1x on 2026-10-19 (10:30)" in x and "cap 2/day" in x
                               for x in r["refused"]), r
    # but a post of his within 30 min of the delivered A IS that A (he posted it): B still fits
    _his_post("2026-10-19T13:40:00-04:00", mid="2")
    (config.DATA / "reels.csv").write_text(
        "media_id,posted_et\n2,2026-10-19T13:40:00-04:00\n", encoding="utf-8")
    r = _deliver_b(dl, tmp_path, base_clips, when=datetime(2026, 10, 19, 17, 0, tzinfo=config.TZ),
                   now=datetime(2026, 10, 19, 14, 0, tzinfo=config.TZ))
    assert r["ok"], r


def test_same_type_or_session_refused_unless_forced(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    reel, chat = _work(tmp_path, base_clips, "third", "shoulder_again_B", rtype="story",
                       session="Shoulder 7h")
    r = dl.deliver(reel, chat, "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("same type story and session Shoulder 7h" in x for x in r["refused"])
    r = dl.deliver(reel, chat, "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW, force_reason="multi-day story")
    assert r["ok"], r
    assert "FORCED" in _rows()[1]["notes"] and "multi-day story" in _rows()[1]["notes"]


def test_failing_gates_refused_and_force_logged(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "dark_A", passed=False)
    r = dl.deliver(reel, chat, "A", A_AT, "SEND", HOOKS_A, CAPTION, NOW)
    assert not r["ok"] and any("gates failed (G1)" in x for x in r["refused"])
    assert _rows() == []
    r = dl.deliver(reel, chat, "A", A_AT, "SEND", HOOKS_A, CAPTION, NOW,
                   force_reason="HOLD is empty, never zero")
    assert r["ok"], r
    note = _rows()[0]["notes"]
    assert "FORCED past gates" in note and "HOLD is empty, never zero" in note
    # a missing gates sidecar is a refusal, and so is a missing EDL sidecar
    reel2, chat2 = _work(tmp_path, base_clips, "broll", "nogates_B", rtype="broll", session="")
    reel2.with_name("nogates_B.gates.json").unlink()
    r = dl.deliver(reel2, chat2, "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("no gates sidecar" in x for x in r["refused"])
    reel3, chat3 = _work(tmp_path, base_clips, "broll", "noedl_B", rtype="broll", session="")
    reel3.with_name("noedl_B.edl.json").unlink()
    r = dl.deliver(reel3, chat3, "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("no EDL sidecar noedl_B.edl.json" in x for x in r["refused"])
    # a missing chat copy
    r = dl.deliver(reel3, reel3.with_name("nope_chat.mp4"), "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("chat copy not found" in x for x in r["refused"])


def test_chat_size_and_hook_count_refused(engine_root, tmp_path, base_clips, monkeypatch):
    from tools import deliver as dl
    _pieces_csv()
    r = _deliver_a(dl, tmp_path, base_clips, hooks=HOOKS_A + ["A third angle you did not ask for?"])
    assert not r["ok"] and any("exactly two hooks are required, got 3" in x for x in r["refused"])
    r = _deliver_a(dl, tmp_path, base_clips, hooks=[HOOKS_A[0]])
    assert not r["ok"] and any("exactly two hooks are required, got 1" in x for x in r["refused"])
    monkeypatch.setattr(config, "CHAT_COPY_MAX_BYTES", 1024)
    r = _deliver_a(dl, tmp_path, base_clips)
    assert not r["ok"] and any("chat copy is" in x and "MB > 0 MB" in x for x in r["refused"])
    assert _rows() == []


def test_reupload_same_frame1_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    # same picture under a new name on another day
    reel, chat = _work(tmp_path, base_clips, "story", "shoulder_recut_A")
    r = dl.deliver(reel, chat, "A", A_AT + timedelta(days=1), "SEND", HOOKS_B, CAPTION_B,
                   NOW + timedelta(days=1))
    assert not r["ok"], r
    assert any(x.startswith("re-upload: frame 1 matches delivery 2026-10-19_A_shoulder7h-a") for x in r["refused"])
    assert not (config.POST / "2026-10-20").exists()
    assert len(_rows()) == 1


def test_legacy_row_and_legacy_signature_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    from tools import fingerprint as fp
    _pieces_csv()
    # one of the ~180 old renders catalogued as legacy in deliveries.csv (never signed as posted)
    ph = fp.frame1_phash(base_clips["story"])
    safeio.append_csv_row(config.LEDGERS / "deliveries.csv",
                          {"id": "legacy_out_batch18_03", "date": "2026-10-07", "status": "legacy",
                           "file": "D:/ClaudeInk/out/batch18_03.mp4", "phash": ph}, LEDGER_HEADER)
    r = _deliver_a(dl, tmp_path, base_clips)
    assert not r["ok"], r
    assert any("re-upload: frame 1 matches delivery legacy_out_batch18_03 (status legacy" in x
               for x in r["refused"])
    # dryrun / cancelled / planned rows never carried a render: they do not block
    (config.LEDGERS / "deliveries.csv").unlink()
    for st in ("dryrun", "cancelled", "planned"):
        safeio.append_csv_row(config.LEDGERS / "deliveries.csv",
                              {"id": f"x_{st}", "date": "2026-10-07", "status": st, "phash": ph}, LEDGER_HEADER)
    r = _deliver_a(dl, tmp_path, base_clips)
    assert r["ok"], r
    # a legacy entry in the signature store (footage_index/panel filed the old render) blocks the
    # full 9-frame signature match even when the frame-1 row is gone
    fp.add_signature("out_batch18_04", base_clips["broll"], "legacy", now=NOW)
    r = _deliver_b(dl, tmp_path, base_clips)
    assert not r["ok"] and any("signature matches legacy reel out_batch18_04" in x for x in r["refused"]), r


def test_posted_signature_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    from tools import fingerprint as fp
    _pieces_csv()
    # a reel he already posted (attribution store) with the same frame 1
    fp.add_signature("DehJPlnKk8I", base_clips["story"], "posted", now=NOW)
    r = _deliver_a(dl, tmp_path, base_clips)
    assert not r["ok"] and any("matches posted reel DehJPlnKk8I" in x for x in r["refused"])


def test_never_reupload_list_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "Copy_DehRelXKsds")
    r = dl.deliver(reel, chat, "A", A_AT, "SEND", HOOKS_A, CAPTION, NOW)
    assert not r["ok"] and any("NEVER_REUPLOAD" in x for x in r["refused"])
    assert _rows() == []


def test_hook_gate_and_caption_tail_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    safeio.append_csv_row(config.LEDGERS / "hooks.csv",
                          {"hook": HOOKS_A[0], "status": "burned", "used_on": "2026-10-07"},
                          ["hook", "type", "used_on", "reel", "result_views", "status"])
    r = _deliver_a(dl, tmp_path, base_clips)
    assert not r["ok"] and any("hook gate: hook 1" in x and "similar to burned hook" in x for x in r["refused"])
    r = _deliver_a(dl, tmp_path, base_clips, hooks=["Would you sit 7 hours here?", "7 hours in one go. Worth it?"],
                   caption="Nice piece\n\n#tattoo")
    assert not r["ok"] and any("exact tail" in x for x in r["refused"])
    # an hours claim that pieces.csv cannot back is refused too
    r = _deliver_a(dl, tmp_path, base_clips, hooks=["Would you sit 9 hours here?", "9 hours in one go. Worth it?"])
    assert not r["ok"] and any("hours not in pieces.csv: 9" in x for x in r["refused"])
    assert _rows() == []


def test_caption_body_reuse_refused(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    # the same caption body (different hooks, different reel) the next day: refused
    r = _deliver_b(dl, tmp_path, base_clips, caption=CAPTION, when=B_AT + timedelta(days=1),
                   now=NOW + timedelta(days=1))
    assert not r["ok"] and any("caption body already used" in x for x in r["refused"]), r
    # captions_used.csv alone (a caption he used by hand) blocks it too
    (config.LEDGERS / "deliveries.csv").unlink()
    r = _deliver_b(dl, tmp_path, base_clips, caption=CAPTION, when=B_AT + timedelta(days=1),
                   now=NOW + timedelta(days=1))
    assert not r["ok"] and any("caption body already used" in x for x in r["refused"]), r
    r = _deliver_b(dl, tmp_path, base_clips, when=B_AT + timedelta(days=1), now=NOW + timedelta(days=1))
    assert r["ok"], r


def test_mirror_skipped_when_drive_missing(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    shutil.rmtree(config.DRIVE_OUT)
    r = _deliver_a(dl, tmp_path, base_clips)
    assert r["ok"], r
    assert r["mirrored"] == [] and r["post_this_drive"] is None
    assert any("Drive folder missing" in n for n in r["notes"])
    assert (config.POST / "2026-10-19" / "POST_THIS.txt").is_file()


def test_mirror_retry_after_crash_keeps_same_size_and_notes_conflicts(engine_root, tmp_path, base_clips):
    """A crashed run left files in Drive: same size -> kept, different size -> noted, never raised."""
    from tools import deliver as dl
    _pieces_csv()
    ddir = config.DRIVE_POST / "2026-10-19"
    ddir.mkdir(parents=True)
    shutil.copy(base_clips["story"], ddir / "A_1100_shoulder7h-a.mp4")          # identical
    (ddir / "A_1100_shoulder7h-a.gates.json").write_text("{}", encoding="utf-8")   # different
    r = _deliver_a(dl, tmp_path, base_clips)
    assert r["ok"], r
    assert any("already had A_1100_shoulder7h-a.mp4 (same size); kept" in n for n in r["notes"])
    assert any(n.startswith("Drive mirror failed for A_1100_shoulder7h-a.gates.json") for n in r["notes"])
    assert (ddir / "A_1100_shoulder7h-a_chat.mp4").is_file() and (ddir / "POST_THIS.txt").is_file()
    assert len(_rows()) == 1


def test_naive_times_and_non_ascii_stem(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "Se\u00f1or Akira \u00e9p\u00e9e 7h",
                       session="Akira", labels={"Shoulder": "7 hours"})
    r = dl.deliver(reel, chat, "A", datetime(2026, 10, 19, 11, 0), "SEND", HOOKS_A, CAPTION,
                   datetime(2026, 10, 19, 6, 40))
    assert r["ok"], r
    assert Path(r["files"]["reel"]).name == "A_1100_senor-akira-epee-7h.mp4"
    assert r["id"] == "2026-10-19_A_senor-akira-epee-7h"
    assert dl.slugify("\u00c9t\u00e9 -- 2026!") == "ete-2026"
    assert dl.slugify("\u4e2d\u6587") == "reel"


# --------------------------------------------------------------------------- HOLD
def test_hold_round_trip_and_pick(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    from tools import fingerprint as fp
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "shoulder7h_B")
    out = dl.hold(reel, chat, None, None, "shoulder7h_B", "2026-10-18")
    assert out == config.HOLD / "HOLD_2026-10-18_shoulder7h-b.mp4" and out.is_file()
    for suf in ("_chat.mp4", ".edl.json", ".gates.json"):
        assert (config.HOLD / f"HOLD_2026-10-18_shoulder7h-b{suf}").is_file(), suf
    reel2, chat2 = _work(tmp_path, base_clips, "broll", "mud_B", rtype="broll", session="", passed=False)
    dl.hold(reel2, chat2, None, None, "mud_B", "2026-10-17")
    reel3, chat3 = _work(tmp_path, base_clips, "third", "akira_A", session="Akira")
    dl.hold(reel3, chat3, None, None, "akira_A", "2026-10-16")

    lst = dl.hold_list(NOW)
    assert [(r["name"], r["date"], r["age_days"], r["gates_pass"]) for r in lst] == [
        ("shoulder7h-b", "2026-10-18", 1, True), ("mud-b", "2026-10-17", 2, False),
        ("akira-a", "2026-10-16", 3, True)]
    assert dl.hold_count() == 2                      # mud-b fails gates: not a usable cut (W3)
    assert dl.hold_pick(now=NOW)["name"] == "shoulder7h-b"
    assert dl.hold_pick(["shoulder7h-b"], now=NOW)["name"] == "akira-a"      # mud-b fails gates
    assert dl.hold_pick(["shoulder7h-b", "akira-a"], now=NOW) is None

    # its own 'held' ledger row (same file) never blocks a HOLD cut; a held row of ANOTHER file
    # with the same frame 1 does
    pick = dl.hold_pick(now=NOW)
    safeio.append_csv_row(config.LEDGERS / "deliveries.csv",
                          {"id": "held_shoulder7h-b", "date": "2026-10-18", "status": "held",
                           "file": "hold/HOLD_2026-10-18_shoulder7h-b.mp4",
                           "phash": fp.frame1_phash(base_clips["story"])}, LEDGER_HEADER)
    r = dl.deliver(Path(pick["file"]), Path(pick["chat"]), "A", A_AT, "SEND", HOOKS_A, CAPTION, NOW)
    assert r["ok"], r
    assert Path(r["files"]["reel"]).name == "A_1100_shoulder7h-b.mp4"
    assert "from HOLD" in _rows()[1]["notes"]
    # never two HOLD cuts together
    pick2 = dl.hold_pick(["shoulder7h-b"], now=NOW)
    r = dl.deliver(Path(pick2["file"]), Path(pick2["chat"]), "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("never two HOLD cuts" in x for x in r["refused"])
    # a fresh render with the frame 1 of a held cut of another name is a re-upload
    reel4, chat4 = _work(tmp_path, base_clips, "third", "akira_recut_B", rtype="broll", session="Akira 2")
    safeio.append_csv_row(config.LEDGERS / "deliveries.csv",
                          {"id": "held_akira-a", "date": "2026-10-16", "status": "held",
                           "file": "hold/HOLD_2026-10-16_akira-a.mp4",
                           "phash": fp.frame1_phash(base_clips["third"])}, LEDGER_HEADER)
    r = dl.deliver(reel4, chat4, "B", B_AT, "SAVE", HOOKS_B, CAPTION_B, NOW)
    assert not r["ok"] and any("matches delivery held_akira-a (status held" in x for x in r["refused"]), r
    # holding the same name twice is refused by safeio (never overwrite a render)
    with pytest.raises(safeio.SafeIOError):
        dl.hold(reel, chat, None, None, "shoulder7h_B", "2026-10-18")


# --------------------------------------------------------------------------- plan
def test_plan_today_uses_post_folder_ledger_and_reels_csv(engine_root, tmp_path, base_clips):
    import tools.slot_planner  # noqa: F401  (sibling module: present in the kit)
    from tools import deliver as dl
    _pieces_csv()
    r0 = dl.plan_today(NOW)
    assert [(s["slot"], s["label"]) for s in r0["slots"]] == [("A", "11:00 ET"), ("B", "14:30 ET")]
    assert r0["hold_count"] == 0 and any("refill HOLD" in n for n in r0["notes"])
    assert _deliver_a(dl, tmp_path, base_clips)["ok"]
    r1 = dl.plan_today(datetime(2026, 10, 19, 9, 0, tzinfo=config.TZ), hold_count_=4)
    assert r1["delivered"] == [{"slot": "A", "time": "11:00", "date": "2026-10-19",
                                "file": "A_1100_shoulder7h-a.mp4"}]
    assert [(s["slot"], s["label"], s["existing"]) for s in r1["slots"]] == [
        ("A", "11:00 ET", True), ("B", "14:30 ET", False)]
    # his own post today from data/reels.csv counts against the cap
    _his_post("2026-10-19T08:00:00-04:00")
    r2 = dl.plan_today(datetime(2026, 10, 19, 9, 0, tzinfo=config.TZ), hold_count_=4)
    assert r2["his_posts"] == ["2026-10-19T08:00:00-04:00"]
    assert r2["posts_allowed_today"] == 1
    assert [s["slot"] for s in r2["new_slots"]] == []


def test_plan_today_spill_sees_tomorrows_post_folder(engine_root, tmp_path, base_clips):
    from tools import deliver as dl
    _pieces_csv()
    # tomorrow's A is already delivered (a file in post/2026-10-20/); nothing today
    tomorrow = config.POST / "2026-10-20"
    safeio.write_bytes(tomorrow / "A_1100_early.mp4", b"\x00")
    late = datetime(2026, 10, 19, 20, 30, tzinfo=config.TZ)          # A would land after 21:00
    r = dl.plan_today(late, hold_count_=4)
    assert {"slot": "A", "time": "11:00", "date": "2026-10-20", "file": "A_1100_early.mp4"} in r["delivered"]
    new = [(s["slot"], s["date"], s["label"]) for s in r["new_slots"]]
    assert new and all(d == "2026-10-20" for _s, d, _l in new)
    assert ("A", "2026-10-20", "11:00 ET") not in new            # tomorrow's A is taken, never doubled
    assert len(new) <= 1


# --------------------------------------------------------------------------- CLI
def test_cli_deliver_and_post_this(engine_root, tmp_path, base_clips, capsys):
    from tools import deliver as dl
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "shoulder7h_A")
    cap = tmp_path / "c.txt"
    cap.write_text(CAPTION, encoding="utf-8")
    rc = dl.main(["--now", "2026-10-19T06:40", "deliver", str(reel), "--chat", str(chat), "--slot", "A",
                  "--when", "2026-10-19T11:00", "--kind", "SEND", "--hook", HOOKS_A[0], "--hook", HOOKS_A[1],
                  "--caption-file", str(cap), "--labels", "Shoulder=1 session / 7 hours",
                  "--tests", "X1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["id"] == "2026-10-19_A_shoulder7h-a"
    rc = dl.main(["--now", "2026-10-19T06:41", "deliver", str(reel), "--chat", str(chat), "--slot", "B",
                  "--when", "2026-10-19T14:30", "--kind", "SAVE", "--hook", HOOKS_B[0], "--hook", HOOKS_B[1],
                  "--caption", CAPTION])
    assert rc == 1 and "REFUSED" in capsys.readouterr().out
    rc = dl.main(["--now", "2026-10-19T06:42", "post-this", "--date", "2026-10-19"])
    txt = capsys.readouterr().out
    assert rc == 0 and "Labels: Shoulder: 1 session / 7 hours" in txt and "Tests: X1" in txt
    rc = dl.main(["list-hold"])
    assert rc == 0 and "HOLD is empty" in capsys.readouterr().out


def test_cli_force_no_mirror_and_plan(engine_root, tmp_path, base_clips, capsys):
    import tools.slot_planner  # noqa: F401  (sibling module: present in the kit)
    from tools import deliver as dl
    _pieces_csv()
    reel, chat = _work(tmp_path, base_clips, "story", "dark_A", passed=False)
    args = ["deliver", str(reel), "--chat", str(chat), "--slot", "A", "--when", "2026-10-19T11:00",
            "--kind", "SEND", "--hook", HOOKS_A[0], "--hook", HOOKS_A[1], "--caption", CAPTION]
    rc = dl.main(["--now", "2026-10-19T06:40"] + args)
    assert rc == 1 and "REFUSED: gates failed (G1)" in capsys.readouterr().out
    rc = dl.main(["--now", "2026-10-19T06:40"] + args + ["--force", "HOLD empty", "--no-mirror", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["mirrored"] == [] and out["post_this_drive"] is None
    assert not (config.DRIVE_POST / "2026-10-19").exists()
    assert "FORCED past gates" in _rows()[0]["notes"] and "HOLD empty" in _rows()[0]["notes"]
    rc = dl.main(["--now", "2026-10-19T09:00", "plan", "--hold", "4"])
    txt = capsys.readouterr().out
    assert rc == 0 and "A" in txt and "B" in txt and "11:00" in txt
    rc = dl.main(["--now", "2026-10-19T09:00", "plan", "--posted", "08:00", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["posts_allowed_today"] == 1 and out["his_posts"] == ["2026-10-19T08:00:00-04:00"]
