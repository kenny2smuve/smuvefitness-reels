"""Tests for tools.ledgers: schemas, safeio-backed writes, validation, migration, helpers."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime

import pytest

from tools import config
from tools import ledgers as L


def _raw_header(name: str) -> list[str]:
    with open(L.path(name), "r", encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


# --------------------------------------------------------------------------- schemas / ensure
def test_headers_match_spec():
    assert L.HEADERS["footage"][0] == "path" and L.HEADERS["footage"][-1] == "indexed_at"
    assert L.HEADERS["pieces"] == ["piece", "sessions", "hours", "color", "healed_footage", "rest_until", "notes"]
    assert L.HEADERS["sessions"] == ["session", "folder", "posts_so_far", "last_post", "rest_until", "status"]
    assert L.HEADERS["hooks"] == ["hook", "type", "used_on", "reel", "result_views", "status"]
    assert L.HEADERS["captions_used"] == ["caption", "used_on", "reel"]
    assert L.HEADERS["deliveries"][:4] == ["id", "date", "slot", "type"]
    assert L.HEADERS["deliveries"][-2:] == ["tests", "notes"]
    assert len(L.HEADERS["deliveries"]) == 19


def test_path_unknown_name(engine_root):
    with pytest.raises(L.LedgerError):
        L.path("nope")
    assert L.path("hooks") == config.LEDGERS / "hooks.csv"


def test_ensure_all_creates_once_and_never_touches_existing(engine_root):
    created = L.ensure_all()
    assert sorted(created) == sorted(L.NAMES)
    for n in L.NAMES:
        assert _raw_header(n) == L.HEADERS[n]
    L.append("hooks", {"hook": "7 hours. Worth it?", "status": "ok"})
    assert L.ensure_all() == []
    assert L.read("hooks")[0]["hook"] == "7 hours. Worth it?"
    assert all(L.present().values())


# --------------------------------------------------------------------------- append / read / upsert
def test_append_and_read_round_trip(engine_root):
    L.append("pieces", {"piece": "Shoulder", "hours": 7, "color": False, "rest_until": date(2026, 10, 23),
                        "bogus": "dropped"})
    rows = L.read("pieces")
    assert rows == [{"piece": "Shoulder", "sessions": "", "hours": "7", "color": "no",
                     "healed_footage": "", "rest_until": "2026-10-23", "notes": ""}]
    assert L.read("sessions") == []       # missing ledger reads as empty


def test_upsert_inserts_then_updates_and_keeps_others(engine_root):
    L.ensure_all()
    L.append("sessions", {"session": "Akira", "posts_so_far": "2", "status": "active"})
    r = L.upsert("sessions", ["session"], {"session": "Shoulder 7h", "folder": "Session 3", "posts_so_far": 1})
    assert r["session"] == "Shoulder 7h" and r["posts_so_far"] == "1"
    r2 = L.upsert("sessions", ["session"], {"session": "Shoulder 7h", "posts_so_far": 2, "last_post": "2026-10-18"})
    assert r2["folder"] == "Session 3"          # untouched column kept
    assert r2["posts_so_far"] == "2"
    rows = L.read("sessions")
    assert [x["session"] for x in rows] == ["Akira", "Shoulder 7h"]
    assert _raw_header("sessions") == L.HEADERS["sessions"]
    backups = list((config.STATE / "ledger-backups").glob("sessions.*.csv"))
    assert backups, "rewrite must keep a backup of the previous version"


def test_upsert_refuses_bad_header_and_empty_key(engine_root):
    p = L.path("hooks")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hook,views\nfoo,12\n", encoding="utf-8")
    with pytest.raises(L.LedgerError):
        L.upsert("hooks", ["hook"], {"hook": "foo", "status": "ok"})
    L.ensure_all()
    with pytest.raises(L.LedgerError):
        L.upsert("pieces", ["piece"], {"piece": "", "hours": "3"})
    with pytest.raises(L.LedgerError):
        L.upsert("pieces", ["nope"], {"piece": "x"})


# --------------------------------------------------------------------------- validate
def test_validate_good_rows(engine_root):
    L.ensure_all()
    L.append("footage", {"path": "F:/x/VID_1.mp4", "size_bytes": "100", "duration_s": "12.5",
                         "capture_time": "2026-10-11T14:03:22-04:00", "camera": "meta",
                         "indexed_at": "2026-10-18T06:40:00-04:00"})
    L.append("pieces", {"piece": "Mudvayne", "hours": "10", "color": "no", "rest_until": "2026-10-23"})
    L.append("sessions", {"session": "Akira", "posts_so_far": "2", "last_post": "2026-10-15T11:29:00-04:00",
                          "rest_until": "2026-10-22", "status": "resting"})
    L.append("hooks", {"hook": "x", "used_on": "2026-10-07", "result_views": "1200", "status": "burned"})
    L.append("captions_used", {"caption": "c", "used_on": "2026-10-07"})
    L.append("deliveries", {"id": "d1", "date": "2026-10-19", "slot": "A", "type": "story",
                            "status": "delivered", "views_24h": ""})
    assert all(errs == [] for errs in L.validate_all().values()), L.validate_all()


def test_validate_reports_each_kind_of_error(engine_root):
    L.ensure_all()
    L.append("pieces", {"piece": "A", "hours": "seven", "color": "maybe", "rest_until": "10/23/2026"})
    L.append("footage", {"path": "p", "capture_time": "2026-10-11T14:03:22", "camera": "gopro"})
    L.append("sessions", {"session": "s", "posts_so_far": "two", "last_post": "yesterday", "status": "tired"})
    L.append("hooks", {"hook": "h", "status": "used"})
    L.append("deliveries", {"id": "d", "date": "2026-13-40", "type": "music", "status": "sent", "views_72h": "n/a"})
    errs = L.validate("pieces")
    assert any("hours='seven' is not numeric" in e for e in errs)
    assert any("color='maybe'" in e for e in errs)
    assert any("rest_until='10/23/2026' is not YYYY-MM-DD" in e for e in errs)
    errs = L.validate("footage")
    assert any("capture_time" in e and "offset" in e for e in errs)
    assert any("camera='gopro'" in e for e in errs)
    errs = L.validate("sessions")
    assert any("posts_so_far='two'" in e for e in errs)
    assert any("last_post='yesterday'" in e for e in errs)
    assert any("status='tired'" in e for e in errs)
    assert any("status='used' not in burned|retired|losing_type|ok" in e for e in L.validate("hooks"))
    errs = L.validate("deliveries")
    assert any("date='2026-13-40'" in e for e in errs)
    assert any("type='music'" in e for e in errs)
    assert any("status='sent'" in e for e in errs)
    assert any("views_72h='n/a'" in e for e in errs)
    for e in errs:
        assert "line 2" in e


def test_validate_header_and_missing(engine_root):
    assert L.validate("hooks") == ["hooks: file is missing or empty (run ensure)"]
    p = L.path("hooks")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hook,used_on,reel,result_views,status\n", encoding="utf-8")
    errs = L.validate("hooks")
    assert len(errs) == 1 and "header" in errs[0]


# --------------------------------------------------------------------------- migrate
def test_migrate_maps_aliases_and_keeps_unmapped_in_notes():
    old = [{"ID": "d1", "Date": "2026-10-07", "file_name": "A_1100_x.mp4", "hooks": "Hook one | Hook two",
            "Posted shortcode": "DeH4x", "24h": "900", "72h": "1300", "old_col": "keep me",
            "notes": "orig", "": ""}]
    rows = L.migrate("deliveries", old, aliases={})
    r = rows[0]
    assert r["id"] == "d1" and r["date"] == "2026-10-07" and r["file"] == "A_1100_x.mp4"
    assert r["hook1"] == "Hook one" and r["hook2"] == "Hook two"
    assert r["posted_shortcode"] == "DeH4x" and r["views_24h"] == "900" and r["views_72h"] == "1300"
    assert r["notes"] == "orig; old_col=keep me"
    assert set(r) == set(L.HEADERS["deliveries"])


def test_migrate_without_notes_column_and_custom_alias():
    old = [{"Hook": "Best $1,200", "views": "1200", "used": "2026-10-07", "Status": "burned", "who": "claude"}]
    rows = L.migrate("hooks", old, aliases={"who": "reel"})
    assert rows[0]["hook"] == "Best $1,200" and rows[0]["result_views"] == "1200"
    assert rows[0]["used_on"] == "2026-10-07" and rows[0]["status"] == "burned" and rows[0]["reel"] == "claude"
    rows = L.migrate("hooks", [{"hook": "h", "extra": "x"}])
    assert rows[0]["_unmapped"] == "extra=x"
    rows = L.migrate("sessions", [{"Session": "s", "Posts so far": "3", "rest-until": "2026-10-22"}])
    assert rows[0]["posts_so_far"] == "3" and rows[0]["rest_until"] == "2026-10-22"


def test_migrate_file_dry_run_then_write(engine_root):
    p = L.path("hooks")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hook,views,used,status\nfoo,12,2026-10-07,burned\n", encoding="utf-8")
    res = L.migrate_file("hooks")
    assert res["rows"] == 1 and res["written"] is False and _raw_header("hooks") == ["hook", "views", "used", "status"]
    res = L.migrate_file("hooks", write=True)
    assert res["written"] is True and _raw_header("hooks") == L.HEADERS["hooks"]
    assert L.read("hooks")[0]["result_views"] == "12"
    assert L.validate("hooks") == []


# --------------------------------------------------------------------------- typed helpers
def test_hook_and_caption_helpers(engine_root):
    L.ensure_all()
    L.append("hooks", {"hook": "burned one", "status": "burned"})
    L.append("hooks", {"hook": "retired one", "status": "Retired"})
    L.append("hooks", {"hook": "losing one", "status": "losing_type"})
    L.append("hooks", {"hook": "fine one", "status": "ok"})
    L.append("hooks", {"hook": "unknown one", "status": ""})
    assert L.burned_hooks() == ["burned one", "retired one", "losing one"]
    assert len(L.hooks_all()) == 5
    L.append("captions_used", {"caption": "Would you sit 7 hours?", "used_on": "2026-10-01"})
    assert L.captions_all() == ["Would you sit 7 hours?"]


def test_session_helpers(engine_root):
    L.ensure_all()
    L.append("sessions", {"session": "Shoulder 7h", "posts_so_far": "1", "rest_until": "2026-10-23", "status": "resting"})
    L.append("sessions", {"session": "Atlas", "posts_so_far": "6", "rest_until": "", "status": "spent"})
    L.append("sessions", {"session": "Akira", "posts_so_far": "", "status": "active"})
    assert L.sessions_rest_ok("Shoulder 7h", date(2026, 10, 22)) is False
    assert L.sessions_rest_ok("Shoulder 7h", date(2026, 10, 23)) is True
    assert L.sessions_rest_ok("shoulder 7h", date(2026, 10, 30)) is True      # case-insensitive
    assert L.sessions_rest_ok("Shoulder 7h", datetime(2026, 10, 24, 6, 40, tzinfo=config.TZ)) is True
    assert L.sessions_rest_ok("Atlas", date(2026, 10, 18)) is True          # empty rest_until
    assert L.sessions_available("Atlas", date(2026, 10, 18)) is False      # spent
    L.append("sessions", {"session": "MJ", "status": "spent: 6+ posts; do not use"})
    assert L.sessions_available("MJ", date(2026, 10, 18)) is False       # remark after the enum
    assert L.sessions_available("Akira", date(2026, 10, 18)) is True
    assert L.sessions_rest_ok("Never heard of it", date(2026, 10, 18)) is True
    assert L.session_posts("Shoulder 7h") == 1
    assert L.session_posts("Atlas") == 6
    assert L.session_posts("Akira") == 0 and L.session_posts("missing") == 0
    assert L.sessions_get("AKIRA")["status"] == "active"


def test_piece_helpers(engine_root):
    L.ensure_all()
    L.append("pieces", {"piece": "Mudvayne", "hours": "10", "color": "no"})
    L.append("pieces", {"piece": "Shoulder", "hours": "7.5", "color": "no"})
    L.append("pieces", {"piece": "Virgin Mary", "hours": "", "color": "no"})
    L.append("pieces", {"piece": "Mystery", "hours": "unknown", "color": ""})
    assert L.pieces_hours() == {"Mudvayne": "10", "Shoulder": "7.5"}
    assert L.pieces_get("mudvayne")["hours"] == "10"
    assert L.pieces_get("nothing") is None


def test_delivery_helpers(engine_root):
    L.ensure_all()
    L.append("deliveries", {"id": "a", "date": "2026-10-18", "slot": "A", "status": "delivered"})
    L.append("deliveries", {"id": "b", "date": "2026-10-18", "slot": "B", "status": "posted", "posted_shortcode": "Dxx"})
    L.append("deliveries", {"id": "h", "date": "2026-10-18", "slot": "", "status": "held"})
    L.append("deliveries", {"id": "c", "date": "2026-10-17", "slot": "A", "status": "unposted"})
    L.append("deliveries", {"id": "d", "date": "2026-10-10", "slot": "A", "status": "", "posted_shortcode": "Dyy"})
    L.append("deliveries", {"id": "e", "date": "", "status": "dryrun"})
    assert [r["id"] for r in L.deliveries_for_date("2026-10-18")] == ["a", "b", "h"]
    assert [r["id"] for r in L.deliveries_for_date(date(2026, 10, 17))] == ["c"]
    assert L.deliveries_count_by_date() == {"2026-10-18": 2, "2026-10-17": 1, "2026-10-10": 1}
    assert L.deliveries_count_by_date(counted_only=False)["2026-10-18"] == 3
    assert [r["id"] for r in L.deliveries_posted_since("2026-10-11")] == ["b"]
    assert [r["id"] for r in L.deliveries_posted_since(date(2026, 10, 1))] == ["b", "d"]
    assert L.footage_rows() == []


# --------------------------------------------------------------------------- CLI
def test_cli(engine_root, capsys):
    assert L.main(["ensure"]) == 0
    out = capsys.readouterr().out
    assert "hooks: created" in out
    assert L.main(["validate"]) == 0
    L.append("hooks", {"hook": "x", "status": "nope"})
    assert L.main(["validate", "hooks"]) == 1
    assert "status='nope'" in capsys.readouterr().out
    assert L.main(["--json", "show", "hooks"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["hook"] == "x"
    assert L.main(["show", "hooks"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == ",".join(L.HEADERS["hooks"])
    assert L.main(["--json", "migrate", "hooks"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] is False
