"""Tests for tools/reels_analysis.py (import with alias mapping, 24h/72h windows, analysis,
data-check of the WEAK_POINTS claims, follows)."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tools import config

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=config.TZ)
SNAPS_H = (6, 30, 80)

# media_id, posted ET (naive), avg watch (s; r9 is in ms), views at +6h/+30h/+80h, shares, saves
REELS = [
    ("r1", "2026-08-10T11:00", 25, (3000, 6000, 7000), 60, 30),
    ("r2", "2026-08-12T11:00", 5, (800, 1500, 1600), 2, 1),
    ("r3", "2026-08-15T14:00", 18, (2000, 5200, 5300), 40, 20),
    ("r4", "2026-08-20T11:00", 6, (500, 900, 950), 1, 1),
    ("r5", "2026-09-05T20:00", 4, (400, 700, 750), 1, 0),
    ("r6", "2026-09-05T20:10", 7, (300, 600, 650), 0, 1),
    ("r7", "2026-09-05T20:20", 22, (2500, 5500, 5600), 50, 20),
    ("r8", "2026-09-10T11:00", 9, (1000, 1800, 1900), 5, 4),
    ("r9", "2026-10-01T11:00", 12500, (1000, 1900, 2000), 6, 3),       # ms
    ("r10", "2026-10-03T11:00", 5, (600, 1200, 1300), 1, 1),
    ("r11", "2026-10-03T12:30", 6, (700, 1300, 1400), 1, 0),
    ("r12", "2026-10-10T11:00", 20, (2000, 5100, 5200), 45, 25),
]
HEADER = ["Media ID", "Permalink", "Caption", "Timestamp", "pulled_at", "Plays", "Reach",
          "avg_watch_time", "Saved", "Shares", "Likes", "Comments", "trial"]


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=config.TZ).astimezone(config.UTC)


def write_history(path: Path, *, snapshot_col: bool = True, snaps=SNAPS_H) -> Path:
    header = list(HEADER) if snapshot_col else [h for h in HEADER if h != "pulled_at"]
    rows = []
    for mid, posted, watch, views, shares, saves in REELS:
        p = _utc(posted)
        for i, h in enumerate(snaps):
            snap = p + timedelta(hours=h)
            row = {
                "Media ID": mid, "Permalink": f"https://www.instagram.com/reel/SC_{mid}/",
                "Caption": f"caption {mid} \U0001F525", "Timestamp": p.isoformat().replace("+00:00", "Z"),
                "pulled_at": snap.isoformat(), "Plays": str(views[i]),
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
    assert r1["posted_et"].isoformat() == "2026-08-10T11:00:00-04:00"
    assert r1["hours_live"] == pytest.approx((NOW - r1["posted_et"]).total_seconds() / 3600, abs=0.1)
    assert by_id(rows)["r2"]["is_hit"] == "no" and by_id(rows)["r9"]["avg_watch_s"] == 12.5
    hits = sorted(r["media_id"] for r in rows if r["is_hit"] == "yes")
    assert hits == ["r1", "r12", "r3", "r7"]
    # written file round-trips through the string readers
    out = config.DATA / "reels.csv"
    assert out.is_file()
    back = ra.load_reels(out)
    assert [r["media_id"] for r in back] == [r["media_id"] for r in rows]
    assert by_id(back)["r1"]["views_72h"] == "6000" and by_id(back)["r1"]["is_hit"] == "yes"
    assert by_id(back)["r1"]["caption"] == "caption r1 \U0001F525"
    meta = json.loads((config.DATA / "reels.import.json").read_text(encoding="utf-8"))
    assert meta["reels"] == 12 and meta["mapping"]["views"] == "Plays"
    # analysis functions give the same answers on typed and string rows
    assert ra.hit_rate_by_month(back) == ra.hit_rate_by_month(rows)


def test_import_young_reel_unknown_and_earliest_after(engine_root):
    from tools import reels_analysis as ra
    p = write_history(config.METRICS_CSV, snaps=(100, 120))       # first pull after +72h
    rows = ra.import_latest(p, now=NOW)
    r1 = by_id(rows)["r1"]
    assert r1["views_72h"] == 3000 and r1["views_72h_source"] == "earliest_after"
    assert r1["views_24h"] is None
    # a reel younger than 72h is unknown, not a miss (unless it already hit)
    young = write_history(config.METRICS_CSV.with_name("young.csv"), snaps=(6, 30))
    rows = ra.import_latest(young, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    r12 = by_id(rows)["r12"]
    assert r12["views_72h"] is None and r12["is_hit"] == "yes" and r12["views_72h_source"] == "already_hit"
    r11 = by_id(rows)["r11"]            # posted 10/03, old enough: known
    assert r11["is_hit"] == "no"
    older = write_history(config.METRICS_CSV.with_name("old.csv"), snaps=(6, 30))
    rows = ra.import_latest(older, now=datetime(2026, 10, 11, 20, 0, tzinfo=config.TZ))
    # r12 posted 10/10 11:00 with 5100 at +30h -> already a hit; make a non-hit young reel
    assert ra.hit_rate_by_month(rows)[-1]["unknown"] == 0


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
    assert r1["is_hit"] == "yes"


# --------------------------------------------------------------------------- analysis
def test_monthly_alone_burst_watch_and_cadence(history):
    from tools import reels_analysis as ra
    rows = ra.import_latest(history, now=NOW)
    months = ra.hit_rate_by_month(rows)
    assert [(m["month"], m["posts"], m["hits"], m["unknown"], m["rate"]) for m in months] == [
        ("2026-08", 4, 2, 0, 0.5), ("2026-09", 4, 1, 0, 0.25), ("2026-10", 4, 1, 0, 0.25)]
    assert [m["month"] for m in ra.hit_rate_by_month(rows, since="2026-09-01")] == ["2026-09", "2026-10"]

    g = ra.post_groups(rows)
    assert sorted(r["media_id"] for r in g["burst"]) == ["r5", "r6", "r7"]
    assert sorted(r["media_id"] for r in g["other"]) == ["r10", "r11"]
    assert sorted(r["media_id"] for r in g["alone"]) == ["r1", "r12", "r2", "r3", "r4", "r8", "r9"]
    ab = ra.alone_vs_burst(rows)
    assert ab["burst"]["n"] == 3 and ab["burst"]["median_views"] == 750 and ab["burst"]["hit_rate"] == pytest.approx(1 / 3, abs=0.001)
    assert ab["alone"]["n"] == 7 and ab["alone"]["median_views"] == 2000 and ab["alone"]["hits"] == 3

    wb = ra.watch_buckets(rows)
    b = {x["bucket"]: x for x in wb["buckets"]}
    assert (b["<8"]["n"], b["8-17"]["n"], b["17-25"]["n"], b[">=25"]["n"]) == (6, 2, 3, 1)
    assert b["<8"]["hits"] == 0 and b["17-25"]["hits"] == 3 and b[">=25"]["hit_rate"] == 1.0
    assert b["<8"]["median_duration_s"] is None
    assert any("length confound" in f for f in wb["flags"])

    hours = {h["hour"]: h["n"] for h in ra.by_hour_et(rows)}
    assert hours == {11: 7, 12: 1, 14: 1, 20: 3}
    wd = {w["weekday"]: w["n"] for w in ra.by_weekday(rows)}
    assert sum(wd.values()) == 12 and wd["Sat"] == 7          # 08-15, 09-05 x3, 10-03 x2, 10-10

    e = ra.engagement_per_1k(rows)
    assert e["n_shares"] == 12 and e["shares_per_1k_median"] == pytest.approx(2.1, abs=0.3)
    assert e["saves_per_1k_median"] is not None

    ppd = ra.posts_per_day(rows)
    assert ppd["2026-09-05"] == 3 and ppd["2026-10-03"] == 2 and len(ppd) == 9
    z = ra.zero_post_days(rows, date(2026, 10, 9), date(2026, 10, 12))
    assert z == [date(2026, 10, 9), date(2026, 10, 11), date(2026, 10, 12)]
    assert ra.days_with_at_least(rows, 3) == {"2026-09-05": 3}
    assert ra.days_with_at_least(rows, 12) == {}


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
    verdict = {}
    for line in text.splitlines():
        if line.startswith("| W"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            verdict[cells[0]] = (cells[2], cells[3])
    assert set(verdict) == {"W1a", "W1b", "W1c", "W2", "W3", "W12"}
    assert verdict["W1a"][1] == "CONFIRMED" and verdict["W1a"][0].startswith("50% (2/4)")
    assert verdict["W1b"][1] == "REFUTED" and verdict["W1b"][0].startswith("100% (1/1")
    assert verdict["W1c"][1] == "REFUTED" and "2026-08: 50%" in verdict["W1c"][0]
    assert verdict["W2"][1] == "REFUTED" and "burst n=3 median 750" in verdict["W2"][0]
    assert verdict["W3"][1] == "CONFIRMED" and "2026-10-16, 2026-10-17, 2026-10-18" in verdict["W3"][0]
    assert verdict["W12"][1] == "REFUTED" and "0 day(s) with 12+ posts" in verdict["W12"][0]
    assert "CANNOT" not in text
    # with the watch column gone, W1a/W1b become CANNOT
    stripped = [{k: ("" if k == "avg_watch_s" else v) for k, v in r.items()} for r in ra.load_reels()]
    text2 = ra.data_check(stripped, now=NOW)
    assert "| W1a |" in text2 and text2.count("CANNOT") >= 2


# --------------------------------------------------------------------------- CLI
def test_cli_import_report_data_check(history, capsys):
    from tools import reels_analysis as ra
    rc = ra.main(["--now", "2026-10-18T12:00", "import", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["reels"] == 12 and out["hits"] == 4 and out["unknown"] == 0
    rc = ra.main(["--now", "2026-10-18T12:00", "report", "--since", "2026-08-01"])
    assert rc == 0 and "# Reels summary" in capsys.readouterr().out
    rc = ra.main(["--now", "2026-10-18T12:00", "data-check", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["W3"]["verdict"] == "CONFIRMED" and out["W12"]["verdict"] == "REFUTED"
