"""Tests for tools/reels_analysis.py (import with alias mapping, 24h/72h windows, young reels
unknown, snapshot edge cases, analysis incl. the rolling burst window, results join into
deliveries.csv, data-check of the WEAK_POINTS claims, follows, CLI)."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tools import config, safeio

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=config.TZ)
SNAPS_H = (6, 30, 80)

# media_id, posted ET (naive), avg watch (s; r9 is in ms), views at +6h/+30h/+80h, shares, saves
# r5-r8 are a 4-post burst on 09-05 20:00-20:40 (each has 3 others within 60 min, W2)
REELS = [
    ("r1", "2026-08-10T11:00", 25, (3000, 6000, 7000), 60, 30),
    ("r2", "2026-08-12T11:00", 5, (800, 1500, 1600), 2, 1),
    ("r3", "2026-08-15T14:00", 18, (2000, 5200, 5300), 40, 20),
    ("r4", "2026-08-20T11:00", 6, (500, 900, 950), 1, 1),
    ("r5", "2026-09-05T20:00", 4, (400, 700, 750), 1, 0),
    ("r6", "2026-09-05T20:10", 7, (300, 600, 650), 0, 1),
    ("r7", "2026-09-05T20:20", 22, (2500, 5500, 5600), 50, 20),
    ("r8", "2026-09-05T20:40", 9, (1000, 1800, 1900), 5, 4),
    ("r9", "2026-10-01T11:00", 12500, (1000, 1900, 2000), 6, 3),       # ms
    ("r10", "2026-10-03T11:00", 5, (600, 1200, 1300), 1, 1),
    ("r11", "2026-10-03T12:30", 6, (700, 1300, 1400), 1, 0),
    ("r12", "2026-10-10T11:00", 20, (2000, 5100, 5200), 45, 25),
]
HEADER = ["Media ID", "Permalink", "Caption", "Timestamp", "pulled_at", "Plays", "Reach",
          "avg_watch_time", "Saved", "Shares", "Likes", "Comments", "trial"]
DELIV_HEADER = ["id", "date", "slot", "type", "file", "chat_file", "phash", "hook1", "hook2",
                "caption", "status", "posted_shortcode", "views_24h", "views_72h",
                "shares_72h", "saves_72h", "avg_watch_72h", "tests", "notes"]


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=config.TZ).astimezone(config.UTC)


def write_history(path: Path, *, snapshot_col: bool = True, snaps=SNAPS_H, reels=REELS,
                  stamp=None, bad_snapshot: dict | None = None) -> Path:
    """stamp: optional callable datetime -> str for the Timestamp/pulled_at cells.
    bad_snapshot: {media_id: snapshot index} whose pulled_at cell is garbage."""
    header = list(HEADER) if snapshot_col else [h for h in HEADER if h != "pulled_at"]
    rows = []
    for mid, posted, watch, views, shares, saves in reels:
        p = _utc(posted)
        for i, h in enumerate(snaps):
            snap = p + timedelta(hours=h)
            ts = stamp(p) if stamp else p.isoformat().replace("+00:00", "Z")
            pulled = stamp(snap) if stamp else snap.isoformat()
            if bad_snapshot and bad_snapshot.get(mid) == i:
                pulled = "not a date"
            row = {
                "Media ID": mid, "Permalink": f"https://www.instagram.com/reel/SC_{mid}/",
                "Caption": f"caption {mid} \U0001F525", "Timestamp": ts,
                "pulled_at": pulled, "Plays": str(views[i]),
                "Reach": str(int(views[i] * 0.8)), "avg_watch_time": str(watch),
                "Saved": str(saves), "Shares": str(shares), "Likes": str(views[i] // 20),
                "Comments": "3", "trial": "TRUE",
            }
            rows.append({k: row[k] for k in header})
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


@pytest.fixture()
def history(engine_root):
    return write_history(config.METRICS_CSV)


def by_id(rows):
    return {r["media_id"]: r for r in rows}


def _verdicts(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.startswith("| W"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            out[cells[0]] = (cells[2], cells[3], cells[4])
    return out


# --------------------------------------------------------------------------- load + import
def test_load_history_maps_aliases_and_flags(history):
    from tools import reels_analysis as ra
    h = ra.load_history(history)
    m = h["mapping"]
    assert m["media_id"] == "Media ID" and m["link"] == "Permalink" and m["views"] == "Plays"
    assert m["posted_utc"] == "Timestamp" and m["snapshot_utc"] == "pulled_at"
    assert m["avg_watch_s"] == "avg_watch_time" and m["saves"] == "Saved"
    assert m["trial"] == "trial" and "ignored" in m["notes"]["trial"]
    assert len(h["rows"]) == 36
    r = h["rows"][0]
    assert r["posted_utc"].tzinfo is not None and r["posted_utc"].utcoffset().total_seconds() == 0
    assert r["posted_et"].isoformat() == "2026-08-10T11:00:00-04:00"
    assert r["views"] == 3000.0 and r["saves"] == 30.0
    assert any("milliseconds" in f and "r9" in f for f in h["flags"])
    r9 = [x for x in h["rows"] if x["media_id"] == "r9"]
    assert all(x["avg_watch_s"] == 12.5 for x in r9)
    assert not any("no snapshot column" in f for f in h["flags"])


def test_import_latest_windows_and_hits(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    assert len(rows) == 12
    r1 = by_id(rows)["r1"]
    assert r1["shortcode"] == "SC_r1"
    assert r1["views"] == 7000 and r1["snapshots"] == 3           # latest snapshot (+80h)
    assert r1["views_24h"] == 3000                                  # last snapshot <= +24h is +6h
    assert r1["views_72h"] == 6000 and r1["views_72h_source"] == "snapshot"   # +30h, not +80h
    assert r1["is_hit"] == "yes"
    assert (r1["shares_72h"], r1["saves_72h"], r1["avg_watch_72h"], r1["reach_72h"]) == (60, 30, 25, 4800)
    assert r1["snapshot_flag"] == ""
    assert r1["posted_et"].isoformat() == "2026-08-10T11:00:00-04:00"
    assert r1["hours_live"] == pytest.approx((NOW - r1["posted_et"]).total_seconds() / 3600, abs=0.1)
    assert by_id(rows)["r2"]["is_hit"] == "no" and by_id(rows)["r9"]["avg_watch_s"] == 12.5
    assert by_id(rows)["r9"]["avg_watch_72h"] == 12.5
    hits = sorted(r["media_id"] for r in rows if r["is_hit"] == "yes")
    assert hits == ["r1", "r12", "r3", "r7"]
    # written file round-trips through the string readers
    out = config.DATA / "reels.csv"
    assert out.is_file()
    back = ra.load_reels(out)
    assert [r["media_id"] for r in back] == [r["media_id"] for r in rows]
    assert by_id(back)["r1"]["views_72h"] == "6000" and by_id(back)["r1"]["is_hit"] == "yes"
    assert by_id(back)["r1"]["shares_72h"] == "60" and by_id(back)["r9"]["avg_watch_72h"] == "12.5"
    assert by_id(back)["r1"]["caption"] == "caption r1 \U0001F525"
    meta = json.loads((config.DATA / "reels.import.json").read_text(encoding="utf-8"))
    assert meta["reels"] == 12 and meta["mapping"]["views"] == "Plays"
    # analysis functions give the same answers on typed and string rows
    assert ra.hit_rate_by_month(back) == ra.hit_rate_by_month(rows)
    # a second import replaces the file atomically WITHOUT a state/ledger-backups copy
    ra.import_latest(history, now=NOW + timedelta(hours=1))
    assert not list((config.STATE / "ledger-backups").glob("reels*")) if (config.STATE / "ledger-backups").exists() else True
    assert not (config.DATA / "reels.tmp.csv").exists()
    assert len(ra.load_reels(out)) == 12


def test_import_young_reel_unknown_and_earliest_after(engine_root):
    from tools import reels_analysis as ra
    p = write_history(config.METRICS_CSV, snaps=(100, 120))       # first pull after +72h
    rows = ra.import_latest(p, now=NOW)
    r1 = by_id(rows)["r1"]
    assert r1["views_72h"] == 3000 and r1["views_72h_source"] == "earliest_after"
    assert r1["views_24h"] is None and r1["shares_72h"] == 60
    # a reel younger than 72h that already hit is a hit now (not unknown)
    young = write_history(config.METRICS_CSV.with_name("young.csv"), snaps=(6, 30))
    rows = ra.import_latest(young, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    r12 = by_id(rows)["r12"]
    assert r12["views_72h"] is None and r12["is_hit"] == "yes" and r12["views_72h_source"] == "already_hit"
    assert r12["shares_72h"] is None
    r11 = by_id(rows)["r11"]            # posted 10/03, old enough: known
    assert r11["is_hit"] == "no"


def test_young_reel_is_unknown_not_a_miss(engine_root):
    """The key rule: under 72h and under 5K is UNKNOWN (is_hit ""), never "no"."""
    from tools import reels_analysis as ra
    octo = [r for r in REELS if r[0] in ("r9", "r10", "r11")]
    p = write_history(config.METRICS_CSV, snaps=(6, 30), reels=octo)
    rows = ra.import_latest(p, now=datetime(2026, 10, 4, 20, 0, tzinfo=config.TZ))
    r11 = by_id(rows)["r11"]                     # posted 10/03 12:30: 55h live, 1300 views
    assert r11["views_72h"] is None and r11["is_hit"] == "" and r11["views_72h_source"] == ""
    assert r11["views_24h"] == 700               # the 24h window has closed
    assert by_id(rows)["r10"]["is_hit"] == ""
    assert by_id(rows)["r9"]["is_hit"] == "no"   # posted 10/01: the 72h window closed 10/04 11:00
    months = ra.hit_rate_by_month(rows)
    assert months == [{"month": "2026-10", "posts": 3, "hits": 0, "unknown": 2, "rate": 0.0,
                       "median_views": 1300}]
    back = ra.load_reels()
    assert by_id(back)["r11"]["is_hit"] == "" and by_id(back)["r11"]["views_72h"] == ""


def test_no_snapshot_column_uses_row_order(engine_root):
    from tools import reels_analysis as ra
    p = write_history(config.METRICS_CSV, snapshot_col=False)
    h = ra.load_history(p)
    assert h["mapping"]["snapshot_utc"] is None
    assert any("no snapshot column" in f for f in h["flags"])
    rows = ra.import_latest(p, now=NOW)
    r1 = by_id(rows)["r1"]
    assert r1["views"] == 7000 and r1["views_24h"] is None
    assert r1["views_72h"] == 7000 and r1["views_72h_source"] == "latest_no_snapshot"
    assert r1["is_hit"] == "yes" and r1["snapshot_flag"].startswith("no snapshot stamps")


def test_mixed_snapshot_presence_drops_only_the_bad_row(engine_root):
    from tools import reels_analysis as ra
    p = write_history(config.METRICS_CSV, bad_snapshot={"r1": 2, "r2": 1})
    rows = ra.import_latest(p, now=NOW)
    r1 = by_id(rows)["r1"]
    # the +80h row of r1 is unreadable: the +30h row is the latest, the 72h logic still works
    assert r1["snapshots"] == 2 and r1["views"] == 6000
    assert r1["views_72h"] == 6000 and r1["views_72h_source"] == "snapshot" and r1["is_hit"] == "yes"
    assert r1["snapshot_flag"] == "1 row(s) without a readable snapshot stamp dropped"
    r2 = by_id(rows)["r2"]                       # its +30h row is gone: +6h is the last <= 72h
    assert r2["views_72h"] == 800 and r2["views"] == 1600 and r2["snapshots"] == 2
    assert by_id(rows)["r3"]["snapshot_flag"] == "" and by_id(rows)["r3"]["snapshots"] == 3
    meta = json.loads((config.DATA / "reels.import.json").read_text(encoding="utf-8"))
    assert any("2 reel(s) had rows without a readable snapshot stamp" in f for f in meta["flags"])


def test_epoch_and_compact_timestamps(engine_root):
    from tools import reels_analysis as ra
    p = write_history(config.METRICS_CSV, reels=REELS[:3], stamp=lambda d: str(int(d.timestamp())))
    rows = ra.import_latest(p, now=NOW)
    assert by_id(rows)["r1"]["posted_et"].isoformat() == "2026-08-10T11:00:00-04:00"
    assert by_id(rows)["r1"]["views_72h"] == 6000
    p = write_history(config.METRICS_CSV.with_name("ms.csv"), reels=REELS[:3],
                      stamp=lambda d: str(int(d.timestamp() * 1000)))
    rows = ra.import_latest(p, now=NOW)
    assert by_id(rows)["r1"]["posted_et"].isoformat() == "2026-08-10T11:00:00-04:00"
    # a compact date is a date, not an epoch (20261019 would otherwise be 1970)
    assert ra.parse_utc("20260810").isoformat() == "2026-08-10T00:00:00+00:00"
    assert ra.parse_utc("20260810150000").isoformat() == "2026-08-10T15:00:00+00:00"
    assert ra.parse_utc("1754838000").isoformat() == "2025-08-10T15:00:00+00:00"
    assert ra.parse_utc("12345") is None and ra.parse_utc("") is None and ra.parse_utc("soon") is None


def test_header_only_csv_imports_nothing(engine_root):
    from tools import reels_analysis as ra
    config.METRICS_CSV.write_text(",".join(HEADER) + "\n", encoding="utf-8")
    rows = ra.import_latest(config.METRICS_CSV, now=NOW)
    assert rows == []
    out = config.DATA / "reels.csv"
    assert out.is_file() and out.read_text(encoding="utf-8").strip() == ",".join(ra.REELS_HEADER)
    assert ra.load_reels(out) == []


# --------------------------------------------------------------------------- results (step 4)
def test_update_delivery_results_joins_on_shortcode(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    p = config.LEDGERS / "deliveries.csv"
    safeio.append_csv_row(p, {"id": "2026-08-10_A_x", "date": "2026-08-10", "slot": "A", "status": "posted",
                              "posted_shortcode": "SC_r1"}, DELIV_HEADER)
    safeio.append_csv_row(p, {"id": "2026-10-03_B_y", "date": "2026-10-03", "slot": "B", "status": "delivered"},
                          DELIV_HEADER)                                           # no shortcode yet
    safeio.append_csv_row(p, {"id": "2026-09-05_A_z", "date": "2026-09-05", "slot": "A", "status": "posted",
                              "posted_shortcode": "SC_nope"}, DELIV_HEADER)        # not in reels.csv
    safeio.append_csv_row(p, {"id": "2026-08-12_B_w", "date": "2026-08-12", "slot": "B", "status": "posted",
                              "posted_shortcode": "SC_r2", "views_72h": "1234"}, DELIV_HEADER)   # already filled
    res = ra.update_delivery_results(rows, now=NOW)
    assert (res["updated"], res["pending"], res["missing"], res["skipped"]) == (1, 0, 1, 2)
    assert res["rows"] == ["2026-08-10_A_x"]
    back = {r["id"]: r for r in safeio.read_csv(p)}
    r = back["2026-08-10_A_x"]
    assert (r["views_24h"], r["views_72h"], r["shares_72h"], r["saves_72h"], r["avg_watch_72h"]) == \
        ("3000", "6000", "60", "30", "25")
    assert back["2026-08-12_B_w"]["views_72h"] == "1234" and back["2026-09-05_A_z"]["views_72h"] == ""
    assert list(back["2026-08-10_A_x"].keys()) == DELIV_HEADER
    # a young reel: 24h filled, the 72h columns stay blank until the window closes
    young = write_history(config.METRICS_CSV.with_name("young.csv"), snaps=(6, 30))
    yrows = ra.import_latest(young, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    safeio.append_csv_row(p, {"id": "2026-10-10_A_v", "date": "2026-10-10", "slot": "A", "status": "posted",
                              "posted_shortcode": "SC_r12"}, DELIV_HEADER)
    res = ra.update_delivery_results(yrows, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    assert res["pending"] == 1 and res["updated"] == 0 and res["rows"] == ["2026-10-10_A_v"]
    v = {r["id"]: r for r in safeio.read_csv(p)}["2026-10-10_A_v"]
    assert v["views_24h"] == "2000" and v["views_72h"] == "" and v["shares_72h"] == ""
    # nothing to do -> no rewrite
    res = ra.update_delivery_results(yrows, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    assert res["rows"] == [] and res["pending"] == 1
    assert ra.update_delivery_results(rows, deliveries_csv=config.LEDGERS / "missing.csv")["updated"] == 0


# --------------------------------------------------------------------------- analysis
def test_monthly_alone_burst_watch_and_cadence(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    months = ra.hit_rate_by_month(rows)
    assert [(m["month"], m["posts"], m["hits"], m["unknown"], m["rate"]) for m in months] == [
        ("2026-08", 4, 2, 0, 0.5), ("2026-09", 4, 1, 0, 0.25), ("2026-10", 4, 1, 0, 0.25)]
    assert [m["month"] for m in ra.hit_rate_by_month(rows, since="2026-09-01")] == ["2026-09", "2026-10"]

    g = ra.post_groups(rows)
    assert sorted(r["media_id"] for r in g["burst"]) == ["r5", "r6", "r7", "r8"]
    assert sorted(r["media_id"] for r in g["other"]) == ["r10", "r11"]
    assert sorted(r["media_id"] for r in g["alone"]) == ["r1", "r12", "r2", "r3", "r4", "r9"]
    ab = ra.alone_vs_burst(rows)
    assert ab["burst"]["n"] == 4 and ab["burst"]["median_views"] == 1325 and ab["burst"]["hit_rate"] == 0.25
    assert ab["alone"]["n"] == 6 and ab["alone"]["median_views"] == 3600 and ab["alone"]["hits"] == 3

    wb = ra.watch_buckets(rows)
    b = {x["bucket"]: x for x in wb["buckets"]}
    assert (b["<8"]["n"], b["8-17"]["n"], b["17-25"]["n"], b[">=25"]["n"]) == (6, 2, 3, 1)
    assert b["<8"]["hits"] == 0 and b["17-25"]["hits"] == 3 and b[">=25"]["hit_rate"] == 1.0
    assert b["<8"]["median_duration_s"] is None
    assert any("length confound" in f for f in wb["flags"])

    hours = {h["hour"]: h["n"] for h in ra.by_hour_et(rows)}
    assert hours == {11: 6, 12: 1, 14: 1, 20: 4}
    wd = {w["weekday"]: w["n"] for w in ra.by_weekday(rows)}
    assert sum(wd.values()) == 12 and wd["Sat"] == 8          # 08-15, 09-05 x4, 10-03 x2, 10-10

    e = ra.engagement_per_1k(rows)
    assert e["n_shares"] == 12 and e["shares_per_1k_median"] == pytest.approx(2.1, abs=0.3)
    assert e["saves_per_1k_median"] is not None

    ppd = ra.posts_per_day(rows)
    assert ppd["2026-09-05"] == 4 and ppd["2026-10-03"] == 2 and len(ppd) == 8
    z = ra.zero_post_days(rows, date(2026, 10, 9), date(2026, 10, 12))
    assert z == [date(2026, 10, 9), date(2026, 10, 11), date(2026, 10, 12)]
    assert ra.days_with_at_least(rows, 3) == {"2026-09-05": 4}
    assert ra.days_with_at_least(rows, 12) == {}


def test_burst_is_a_rolling_hour_not_a_clock_hour():
    """W2: 10:40, 10:50, 11:05, 11:15 is one burst of four although it straddles two clock hours;
    three posts in one hour are 'other' (only 2 others each); a 4th post 70 min away is not in
    the window of the first."""
    from tools import reels_analysis as ra
    def row(mid, t):
        return {"media_id": mid, "posted_et": f"2026-10-07T{t}:00-04:00"}
    rows = [row("a", "10:40"), row("b", "10:50"), row("c", "11:05"), row("d", "11:15")]
    g = ra.post_groups(rows)
    assert sorted(r["media_id"] for r in g["burst"]) == ["a", "b", "c", "d"] and g["other"] == []
    rows3 = [row("a", "10:40"), row("b", "10:50"), row("c", "11:05")]
    g = ra.post_groups(rows3)
    assert g["burst"] == [] and sorted(r["media_id"] for r in g["other"]) == ["a", "b", "c"]
    rows = [row("a", "10:00"), row("b", "10:50"), row("c", "10:55"), row("d", "11:10"), row("e", "14:30")]
    g = ra.post_groups(rows)
    # b and c see a, c|b and d within 60 min (burst); a is 70 min from d and d 70 min from a,
    # so each of them sees only two others (other)
    assert sorted(r["media_id"] for r in g["burst"]) == ["b", "c"]
    assert sorted(r["media_id"] for r in g["other"]) == ["a", "d"]
    assert [r["media_id"] for r in g["alone"]] == ["e"]                       # 3h20 after d
    assert "rolling window" in ra.BURST_DEFINITION


def test_report_and_follows(history, tmp_path):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    text = ra.report(rows, since="2026-08-01", now=NOW)
    out = config.DATA / "analysis" / "summary.md"
    assert out.is_file() and out.read_text(encoding="utf-8") == text
    assert "| 2026-08 | 4 | 2 | 0 | 50% |" in text
    assert "follows: n/a" in text
    assert "zero-post days since 2026-08-01" in text
    assert "FLAG: length confound" in text
    assert "rolling window" in text
    # a followers file turns it into a labelled estimate
    f = tmp_path / "followers.csv"
    f.write_text("date,followers\n2026-10-15,1000\n2026-10-16,1050\n2026-10-18,1150\n", encoding="utf-8")
    est = ra.follows_estimate(f)
    assert [(e["date"], e["follows"]) for e in est] == [("2026-10-15", None), ("2026-10-16", 50), ("2026-10-18", 50)]
    assert all(e["label"] == "estimate" for e in est)
    line = ra.follows_line(f)
    assert line.startswith("follows: ~50/day on 2026-10-18") and "estimate" in line
    assert "follows: ~50/day" in ra.report(rows, now=NOW, followers_csv=f)
    assert ra.follows_line(None) == "follows: n/a"
    assert ra.follows_line(tmp_path / "missing.csv") == "follows: n/a"


def test_data_check_verdicts(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    text = ra.data_check(rows, now=NOW)
    out = config.REGISTER / "data-check.md"
    assert out.is_file() and out.read_text(encoding="utf-8") == text
    verdict = _verdicts(text)
    assert set(verdict) == {"W1a", "W1b", "W1c", "W2", "W3", "W12"}
    assert verdict["W1a"][1] == "CONFIRMED" and verdict["W1a"][0].startswith("50% (2/4)")
    assert verdict["W1b"][1] == "REFUTED" and verdict["W1b"][0].startswith("100% (1/1")
    assert verdict["W1c"][1] == "REFUTED" and "2026-08: 50%" in verdict["W1c"][0]
    assert verdict["W2"][1] == "REFUTED" and "burst n=4 median 1325 rate 25%" in verdict["W2"][0]
    assert "rolling window" in verdict["W2"][2]
    assert verdict["W3"][1] == "CONFIRMED"
    assert "0 post(s), 3 zero-post day(s) (2026-10-16, 2026-10-17, 2026-10-18)" in verdict["W3"][0]
    assert "since the audit: 0 zero-post day(s)" in verdict["W3"][2]
    assert verdict["W12"][1] == "REFUTED" and "0 day(s) with 12+ posts" in verdict["W12"][0]
    assert "CANNOT" not in text
    # with the watch column gone, W1a/W1b become CANNOT
    stripped = [{k: ("" if k == "avg_watch_s" else v) for k, v in r.items()} for r in ra.load_reels()]
    text2 = ra.data_check(stripped, now=NOW)
    assert "| W1a |" in text2 and text2.count("CANNOT") >= 2


def test_data_check_w3_window_fixed_and_cannot_rows(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    # he posts again on 10/19: the audit claim (10/15-10/18) stays CONFIRMED, the note shows the
    # current zero-post days separately
    later = rows + [{"media_id": "new", "posted_et": "2026-10-19T09:00:00-04:00", "views": "100", "is_hit": ""}]
    v = _verdicts(ra.data_check(later, now=datetime(2026, 10, 21, 8, 0, tzinfo=config.TZ)))
    assert v["W3"][1] == "CONFIRMED"
    assert "since the audit: 2 zero-post day(s) 2026-10-19..2026-10-21 (2026-10-20, 2026-10-21)" in v["W3"][2]
    assert "last post 2026-10-19T09:00" in v["W3"][2]
    # a post inside the audit window refutes it
    inside = rows + [{"media_id": "x", "posted_et": "2026-10-16T09:00:00-04:00", "views": "100", "is_hit": ""}]
    v = _verdicts(ra.data_check(inside, now=NOW))
    assert v["W3"][1] == "REFUTED" and "1 post(s), 2 zero-post day(s)" in v["W3"][0]
    # no posted times at all: W2, W3 and W12 are CANNOT (column missing)
    notime = [{k: ("" if k in ("posted_et", "posted_utc") else val) for k, val in r.items()} for r in ra.load_reels()]
    v = _verdicts(ra.data_check(notime, now=NOW))
    assert [v[k][1] for k in ("W2", "W3", "W12")] == ["CANNOT", "CANNOT", "CANNOT"]
    assert v["W1c"][1] == "CANNOT"
    # the small-claim tolerance: 15% does not confirm '7%', 9% does; 20%+ keeps the 10 points
    assert ra.pct_tolerance(0.07) == pytest.approx(0.03) and ra.pct_tolerance(0.17) == pytest.approx(0.0425)
    assert ra.pct_tolerance(0.20) == 0.10
    assert not ra._close_pct(0.15, 0.07) and ra._close_pct(0.09, 0.07) and ra._close_pct(0.28, 0.20)


# --------------------------------------------------------------------------- CLI
def test_cli_import_report_data_check_results(history, capsys, tmp_path):
    from tools import reels_analysis as ra
    rc = ra.main(["--now", "2026-10-18T12:00", "import", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["reels"] == 12 and out["hits"] == 4 and out["unknown"] == 0
    rc = ra.main(["--now", "2026-10-18T12:00", "report", "--since", "2026-08-01"])
    assert rc == 0 and "# Reels summary" in capsys.readouterr().out
    f = tmp_path / "followers.csv"
    f.write_text("date,followers\n2026-10-16,1050\n2026-10-18,1150\n", encoding="utf-8")
    rc = ra.main(["--now", "2026-10-18T12:00", "report", "--followers", str(f), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["follows"].startswith("follows: ~50/day on 2026-10-18")
    rc = ra.main(["--now", "2026-10-18T12:00", "data-check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["W3"]["verdict"] == "CONFIRMED" and out["W12"]["verdict"] == "REFUTED"
    p = config.LEDGERS / "deliveries.csv"
    safeio.append_csv_row(p, {"id": "2026-08-10_A_x", "date": "2026-08-10", "slot": "A", "status": "posted",
                              "posted_shortcode": "SC_r1"}, DELIV_HEADER)
    rc = ra.main(["--now", "2026-10-18T12:00", "results", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["updated"] == 1 and out["rows"] == ["2026-08-10_A_x"]
    assert safeio.read_csv(p)[0]["views_72h"] == "6000"
    rc = ra.main(["--now", "2026-10-18T12:00", "results"])
    assert rc == 0 and "results: 0 updated" in capsys.readouterr().out
