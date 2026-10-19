"""Tests for tools/slot_planner.py (W2/W3/W4/W12). Pure logic plus one post/ scan."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from tools import config, safeio
from tools import slot_planner as sp

TZ = config.TZ
UTC = config.UTC


def et(s: str) -> datetime:
    """'2026-10-19T06:40' -> aware ET datetime."""
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def times(result: dict) -> list[tuple[str, str, str]]:
    return [(s["slot"], s["kind"], s["time"]) for s in result["slots"]]


def _assert_invariants(result: dict, now: datetime) -> None:
    """Every plan: lead >= 45 min, nothing after 21:00, >= 3h apart, unique letter per date,
    at most 2 per date, A is SEND and B is SAVE, every time carries the ET offset."""
    lead = now.astimezone(UTC) + timedelta(minutes=config.MIN_LEAD_MINUTES)
    per_date: dict[str, list] = {}
    for s in result["slots"]:
        t = datetime.fromisoformat(s["time"])
        assert t.utcoffset() is not None
        assert t.utcoffset() == t.astimezone(TZ).utcoffset()
        assert s["kind"] == ("SEND" if s["slot"] == "A" else "SAVE")
        assert t.minute in (0, 30) and t.second == 0
        if not s["existing"]:
            assert t.astimezone(UTC) >= lead, (s, now)
        assert (t.hour, t.minute) <= (config.LATEST_POST_HOUR, 0)
        per_date.setdefault(s["date"], []).append((s["slot"], t))
    for d, lst in per_date.items():
        letters = [x[0] for x in lst]
        assert len(letters) == len(set(letters)), f"same letter twice on {d}: {letters}"
        assert len(lst) <= config.MAX_POSTS_PER_DAY
        ts = sorted(x[1].astimezone(UTC) for x in lst)
        for a, b in zip(ts, ts[1:]):
            assert b - a >= timedelta(hours=config.MIN_GAP_HOURS), (d, ts)


# --------------------------------------------------------------------------- scenarios from the spec
def test_normal_morning_run():
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [], [], hold_count=4)
    assert times(r) == [("A", "SEND", "2026-10-19T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-19T14:30:00-04:00")]
    assert r["date"] == "2026-10-19" and r["posts_allowed_today"] == 2
    assert r["blocked"] is None and not any("HOLD" in n for n in r["notes"])
    assert r["catchup"] is False
    _assert_invariants(r, now)


def test_run_at_1230_rounds_up_to_1330_and_b_1630():
    now = et("2026-10-19T12:30")
    r = sp.plan(now, [], [], 4)
    assert times(r) == [("A", "SEND", "2026-10-19T13:30:00-04:00"),
                        ("B", "SAVE", "2026-10-19T16:30:00-04:00")]
    assert "window passed" in r["slots"][0]["reason"]
    assert r["catchup"] is True
    _assert_invariants(r, now)


def test_run_at_1900_one_today_and_tomorrow_1100():
    now = et("2026-10-19T19:00")
    r = sp.plan(now, [], [], 4)
    # 19:00 + 45 min = 19:45 rounds up to 20:00; 20:00 + 3h is after 21:00 so B spills
    assert times(r) == [("A", "SEND", "2026-10-19T20:00:00-04:00"),
                        ("A", "SEND", "2026-10-20T11:00:00-04:00")]
    assert any("tomorrow" in n for n in r["notes"])
    _assert_invariants(r, now)


def test_run_at_2030_both_tomorrow():
    now = et("2026-10-19T20:30")
    r = sp.plan(now, [], [], 4)
    assert times(r) == [("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert r["date"] == "2026-10-20" and r["today"] == "2026-10-19"
    _assert_invariants(r, now)


def test_run_at_2015_is_the_last_minute_for_today():
    r = sp.plan(et("2026-10-19T20:15"), [], [], 4)
    assert times(r)[0] == ("A", "SEND", "2026-10-19T21:00:00-04:00")
    r = sp.plan(et("2026-10-19T20:16"), [], [], 4)
    assert times(r)[0] == ("A", "SEND", "2026-10-20T11:00:00-04:00")


def test_he_posted_at_0912_one_slot_at_1230():
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [et("2026-10-19T09:12")], [], 4)
    assert times(r) == [("A", "SEND", "2026-10-19T12:30:00-04:00")]
    assert r["posts_allowed_today"] == 1 and r["blocked"] is None
    assert "09:12 ET" in r["slots"][0]["reason"]
    _assert_invariants(r, now)


def test_he_posted_twice_blocked():
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [et("2026-10-19T09:12"), et("2026-10-19T13:00")], [], 4)
    assert r["blocked"] == "already 2 today"
    assert r["slots"] == [] and r["posts_allowed_today"] == 0
    assert sp.post_this_lines(r)[0].startswith("Nothing to post today")


def test_his_posts_on_other_days_do_not_count():
    r = sp.plan(et("2026-10-19T06:40"), [et("2026-10-18T09:12"), et("2026-10-18T13:00")], [], 4)
    assert len(r["slots"]) == 2 and r["blocked"] is None


def test_his_late_post_pushes_everything_to_tomorrow():
    now = et("2026-10-19T19:00")
    r = sp.plan(now, [et("2026-10-19T19:30")], [], 4)
    assert times(r) == [("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    _assert_invariants(r, now)


def test_morning_arm_plans_tomorrow_0700_alone_and_1430():
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [], [], 4, morning_arm=True)
    assert times(r) == [("A", "SEND", "2026-10-20T07:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert r["slots"][0]["reason"].startswith("post tomorrow ~07:00, alone")
    assert r["date"] == "2026-10-20" and r["morning_arm"] is True
    lo, hi = config.X1_MORNING_WINDOW
    assert lo <= "07:00" <= hi
    _assert_invariants(r, now)


def test_hold_note_when_below_minimum():
    r = sp.plan(et("2026-10-19T06:40"), [], [], hold_count=2)
    assert any(n.startswith("refill HOLD") for n in r["notes"])
    assert any("refill HOLD" in line for line in sp.post_this_lines(r))
    r = sp.plan(et("2026-10-19T06:40"), [], [], hold_count=config.HOLD_MIN)
    assert not any("HOLD" in n for n in r["notes"])


# --------------------------------------------------------------------------- idempotency
def test_delivered_a_keeps_its_time_and_only_b_is_planned():
    now = et("2026-10-19T06:40")
    delivered = [{"slot": "A", "time": et("2026-10-19T11:00"), "date": date(2026, 10, 19),
                  "file": "A_1100_shoulder7h.mp4"}]
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("A", "SEND", "2026-10-19T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-19T14:30:00-04:00")]
    assert r["slots"][0]["existing"] is True and r["slots"][0]["file"] == "A_1100_shoulder7h.mp4"
    assert r["new_slots"] == [r["slots"][1]]
    _assert_invariants(r, now)
    # re-running later in the day gives the same answer
    r2 = sp.plan(et("2026-10-19T13:00"), [], delivered, 4)
    assert times(r2) == times(r)


def test_delivered_a_and_b_nothing_to_plan():
    delivered = [{"slot": "A", "time": "11:00"}, {"slot": "B", "time": "2026-10-19T14:30:00-04:00"}]
    r = sp.plan(et("2026-10-19T06:40"), [], delivered, 4)
    assert r["blocked"] == "already 2 today" and r["new_slots"] == []
    assert len(r["slots"]) == 2


def test_his_post_plus_delivered_a_reaches_the_cap_with_warning():
    r = sp.plan(et("2026-10-19T10:30"), [et("2026-10-19T10:00")],
                [{"slot": "A", "time": "11:00"}], 4)
    assert r["blocked"] == "already 2 today" and r["posts_allowed_today"] == 1
    assert "WARNING" in r["slots"][0]["reason"] and "13:00 ET" in r["slots"][0]["reason"]


def test_delivered_for_another_date_is_ignored():
    delivered = [{"slot": "A", "time": et("2026-10-18T11:00"), "date": "2026-10-18"}]
    r = sp.plan(et("2026-10-19T06:40"), [], delivered, 4)
    assert len(r["new_slots"]) == 2


def test_target_date_tomorrow_pair_for_hold_next():
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [et("2026-10-19T09:12")], [], 4, target_date=date(2026, 10, 20))
    assert times(r) == [("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert r["posts_allowed_today"] == 2            # his 10/19 post does not count for 10/20


# --------------------------------------------------------------------------- DST (Nov 1 2026 ends DST)
def test_plan_across_dst_change():
    now = et("2026-10-31T20:30")                       # EDT, both slots land on the EST day
    r = sp.plan(now, [], [], 4)
    assert times(r) == [("A", "SEND", "2026-11-01T11:00:00-05:00"),
                        ("B", "SAVE", "2026-11-01T14:30:00-05:00")]
    a, b = (datetime.fromisoformat(s["time"]) for s in r["slots"])
    assert b.astimezone(UTC) - a.astimezone(UTC) == timedelta(hours=3, minutes=30)
    _assert_invariants(r, now)

    now = et("2026-11-01T00:30")                       # still EDT (fold at 02:00)
    assert now.utcoffset() == timedelta(hours=-4)
    r = sp.plan(now, [], [], 4)
    assert times(r)[0] == ("A", "SEND", "2026-11-01T11:00:00-05:00")
    _assert_invariants(r, now)

    now = et("2026-11-01T06:40")
    r = sp.plan(now, [et("2026-11-01T09:12")], [], 4)
    assert times(r) == [("A", "SEND", "2026-11-01T12:30:00-05:00")]
    _assert_invariants(r, now)

    r = sp.plan(et("2026-10-31T06:40"), [], [], 4, morning_arm=True)
    assert times(r)[0] == ("A", "SEND", "2026-11-01T07:00:00-05:00")


def test_add_is_absolute_time_not_wall_clock():
    before = et("2026-11-01T00:30")
    after = sp._add(before, timedelta(hours=3))
    assert after.isoformat() == "2026-11-01T02:30:00-05:00"     # 3 real hours, 2h on the wall clock


# --------------------------------------------------------------------------- helpers and sweep
def test_round_up_slot_and_format():
    assert sp.round_up_slot(et("2026-10-19T13:15")).isoformat() == "2026-10-19T13:30:00-04:00"
    assert sp.round_up_slot(et("2026-10-19T13:30")).isoformat() == "2026-10-19T13:30:00-04:00"
    assert sp.round_up_slot(et("2026-10-19T13:31")).isoformat() == "2026-10-19T14:00:00-04:00"
    assert sp.round_up_slot(et("2026-10-19T13:00:01")).isoformat() == "2026-10-19T13:30:00-04:00"
    assert sp.format_slot(et("2026-10-19T11:00")) == "11:00 ET"
    assert sp.format_slot(datetime(2026, 10, 19, 15, 0, tzinfo=UTC)) == "11:00 ET"


def test_catchup():
    assert sp.catchup(et("2026-10-19T06:40")) is False
    assert sp.catchup(et("2026-10-19T10:45")) is False
    assert sp.catchup(et("2026-10-19T10:46")) is True
    assert sp.catchup(et("2026-10-19T15:00")) is True


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        sp.plan(datetime(2026, 10, 19, 6, 40), [], [], 4)


@pytest.mark.parametrize("posted", [[], ["09:12"], ["06:05"], ["13:40"], ["17:50"]])
def test_sweep_every_quarter_hour_keeps_the_invariants(posted):
    d = date(2026, 10, 19)
    his = [sp.at(d, p) for p in posted]
    for q in range(0, 24 * 4):
        now = sp.at(d, "00:00") + timedelta(minutes=15 * q)
        r = sp.plan(now, his, [], 4)
        _assert_invariants(r, now)
        assert r["slots"] or r["blocked"]
        per_date: dict[str, int] = {}
        for s in r["new_slots"]:
            per_date[s["date"]] = per_date.get(s["date"], 0) + 1
        for p in his:
            per_date[p.date().isoformat()] = per_date.get(p.date().isoformat(), 0) + 1
        assert all(n <= config.MAX_POSTS_PER_DAY for n in per_date.values()), (now, per_date)
        for s in r["new_slots"]:
            if s["date"] == d.isoformat():
                for p in his:
                    assert abs((datetime.fromisoformat(s["time"]).astimezone(UTC) - p.astimezone(UTC))
                               .total_seconds()) >= 3 * 3600


# --------------------------------------------------------------------------- post/ scan and CLI
def test_scan_delivered_reads_post_folder(engine_root):
    d = date(2026, 10, 19)
    folder = config.POST / d.isoformat()
    for name in ("A_1100_shoulder7h.mp4", "A_1100_shoulder7h_chat.mp4", "POST_THIS.txt", "notes.json"):
        safeio.write_text(folder / name, "x")
    found = sp.scan_delivered(d)
    assert len(found) == 1
    assert found[0]["slot"] == "A" and found[0]["file"] == "A_1100_shoulder7h.mp4"
    assert found[0]["time"].isoformat() == "2026-10-19T11:00:00-04:00"
    assert sp.scan_delivered(date(2026, 10, 20)) == []
    r = sp.plan(et("2026-10-19T06:40"), [], found, 4)
    assert times(r) == [("A", "SEND", "2026-10-19T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-19T14:30:00-04:00")]


def test_hold_count_on_disk(engine_root):
    assert sp.hold_count_on_disk() == 0
    for name in ("HOLD_2026-10-18_akira.mp4", "HOLD_2026-10-18_akira_chat.mp4", "HOLD_2026-10-18_b.mp4"):
        safeio.write_text(config.HOLD / name, "x")
    safeio.write_text(config.HOLD / "next" / "A_1100_x.mp4", "x")
    assert sp.hold_count_on_disk() == 2


def test_cli_json_and_text(engine_root, capsys):
    rc = sp.main(["--now", "2026-10-19T06:40", "--posted", "09:12", "--hold", "2", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [(s["slot"], s["time"]) for s in out["slots"]] == [("A", "2026-10-19T12:30:00-04:00")]
    assert any(n.startswith("refill HOLD") for n in out["notes"])

    rc = sp.main(["--now", "2026-10-19T06:40", "--delivered", "A=11:00", "--hold", "4"])
    text = capsys.readouterr().out
    assert rc == 0 and "kept A SEND 2026-10-19 11:00 ET" in text and "plan B SAVE 2026-10-19 14:30 ET" in text

    rc = sp.main(["--now", "2026-10-19T06:40", "--posted", "09:12", "13:00", "--hold", "4"])
    text = capsys.readouterr().out
    assert rc == 0 and "BLOCKED: already 2 today" in text

    rc = sp.main(["--now", "2026-10-19T06:40", "--morning-arm", "--hold", "4", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["slots"][0]["time"] == "2026-10-20T07:00:00-04:00"

    rc = sp.main(["--now", "2026-10-31T20:30", "--hold", "4", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["slots"][0]["time"] == "2026-11-01T11:00:00-05:00"

    assert sp.main(["--now", "not-a-time"]) == 1


# --------------------------------------------------------------------------- review fixes
def test_delivered_b_without_a_plans_a_before_b():
    now = et("2026-10-19T06:40")
    delivered = [{"slot": "B", "time": "14:30", "date": "2026-10-19", "file": "B_1430_x.mp4"}]
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("A", "SEND", "2026-10-19T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-19T14:30:00-04:00")]
    assert r["slots"][0]["existing"] is False and r["slots"][1]["existing"] is True
    assert "before" in r["slots"][0]["reason"]
    _assert_invariants(r, now)
    # too late to fit >= 3h before B: A goes after B + 3h (clock order inverted, noted)
    now = et("2026-10-19T12:00")
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("B", "SAVE", "2026-10-19T14:30:00-04:00"),
                        ("A", "SEND", "2026-10-19T17:30:00-04:00")]
    assert any("inverted" in n for n in r["notes"])
    _assert_invariants(r, now)
    # at 19:00 A still fits after B (20:00 is >= 3h after 14:30 and <= 21:00)
    now = et("2026-10-19T19:00")
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("B", "SAVE", "2026-10-19T14:30:00-04:00"),
                        ("A", "SEND", "2026-10-19T20:00:00-04:00")]
    _assert_invariants(r, now)
    # and when even that is after 21:00, the pair moves to tomorrow (today's B stays listed)
    now = et("2026-10-19T20:30")
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("B", "SAVE", "2026-10-19T14:30:00-04:00"),
                        ("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert r["date"] == "2026-10-20" and [s["date"] for s in r["new_slots"]] == ["2026-10-20"] * 2
    _assert_invariants(r, now)


def test_late_run_with_tomorrow_already_delivered_adds_nothing():
    """A crash/retry after a late run must not plan a 3rd/4th reel for tomorrow."""
    now = et("2026-10-19T21:30")
    delivered = [{"slot": "A", "time": "11:00", "date": "2026-10-20", "file": "A_1100_x.mp4"},
                 {"slot": "B", "time": "14:30", "date": "2026-10-20", "file": "B_1430_y.mp4"}]
    r = sp.plan(now, [], delivered, 4)
    assert r["date"] == "2026-10-20" and r["new_slots"] == []
    assert r["blocked"] == "already 2 on 2026-10-20"
    assert all(s["existing"] for s in r["slots"]) and len(r["slots"]) == 2
    _assert_invariants(r, now)
    # only A delivered for tomorrow: just B is new
    r = sp.plan(now, [], delivered[:1], 4)
    assert times(r) == [("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert [s["slot"] for s in r["new_slots"]] == ["B"] and r["blocked"] is None
    _assert_invariants(r, now)
    # his post tomorrow morning also counts on that date
    r = sp.plan(now, [et("2026-10-20T09:00")], delivered[:1], 4)
    assert r["blocked"] == "already 2 on 2026-10-20" and r["new_slots"] == []


def test_spill_with_tomorrow_already_delivered_takes_the_free_letter():
    now = et("2026-10-19T19:00")
    delivered = [{"slot": "A", "time": "11:00", "date": "2026-10-20", "file": "A_1100_x.mp4"}]
    r = sp.plan(now, [], delivered, 4)
    assert times(r) == [("A", "SEND", "2026-10-19T20:00:00-04:00"),
                        ("A", "SEND", "2026-10-20T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-20T14:30:00-04:00")]
    assert [(s["slot"], s["date"]) for s in r["new_slots"]] == [("A", "2026-10-19"), ("B", "2026-10-20")]
    assert any("tomorrow's B" in n for n in r["notes"])
    _assert_invariants(r, now)
    # tomorrow full: today's single slot only, with a note
    both = delivered + [{"slot": "B", "time": "14:30", "date": "2026-10-20", "file": "B_1430_y.mp4"}]
    r = sp.plan(now, [], both, 4)
    assert [(s["slot"], s["date"]) for s in r["new_slots"]] == [("A", "2026-10-19")]
    assert any("nothing free on 2026-10-20" in n for n in r["notes"])
    _assert_invariants(r, now)


def test_malformed_post_file_name_is_noted_not_fatal(engine_root):
    d = date(2026, 10, 19)
    folder = config.POST / d.isoformat()
    for name in ("A_9999_x.mp4", "B_1430_y.mp4", "A_1260_z.mp4"):
        safeio.write_text(folder / name, "x")
    found = sp.scan_delivered(d)
    assert [(x["slot"], x["file"], x["time"] is None) for x in found] == [
        ("A", "A_1260_z.mp4", True), ("A", "A_9999_x.mp4", True), ("B", "B_1430_y.mp4", False)]
    now = et("2026-10-19T06:40")
    r = sp.plan(now, [], found, 4)
    assert times(r) == [("A", "SEND", "2026-10-19T11:00:00-04:00"),
                        ("B", "SAVE", "2026-10-19T14:30:00-04:00")]
    ignored = [n for n in r["notes"] if n.startswith("ignored delivered entry")]
    assert len(ignored) == 2 and all("bad time" in n for n in ignored)
    _assert_invariants(r, now)


def test_cli_bad_delivered_letter_and_time_are_noted(engine_root, capsys):
    rc = sp.main(["--now", "2026-10-19T06:40", "--delivered", "C=11:00", "A=nope", "B=14:30",
                  "--hold", "4", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [(s["slot"], s["time"]) for s in out["slots"]] == [("A", "2026-10-19T11:00:00-04:00"),
                                                             ("B", "2026-10-19T14:30:00-04:00")]
    ignored = [n for n in out["notes"] if n.startswith("ignored delivered entry")]
    assert len(ignored) == 2
    assert any("C=11:00" in n and "not A or B" in n for n in ignored)
    assert any("A=nope" in n and "bad time" in n for n in ignored)
