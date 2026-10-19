"""Tests for tools.preflight: real ffmpeg structure, faked ffmpeg lists, freshness, clock, files."""
from __future__ import annotations

import json
import os
import stat
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import config, preflight as P, safeio

NOW = datetime(2026, 10, 18, 6, 40, tzinfo=config.TZ)       # EDT, -04:00
WINTER = datetime(2026, 11, 15, 6, 40, tzinfo=config.TZ)    # EST, -05:00

FAKE_SH = """#!/bin/sh
case "$*" in
  *-version*) echo "ffmpeg version 7.1-fake Copyright (c) 2000-2024 the FFmpeg developers"; exit 0;;
  *-encoders*) printf 'Encoders:\\n V..... = Video\\n ------\\n%s\\n' "$ENC"; exit 0;;
  *-filters*)  printf 'Filters:\\n  T.. = Timeline support\\n%s\\n' "$FLT"; exit 0;;
esac
exit 1
"""


def _fake_ffmpeg(tmp_path, monkeypatch, enc_lines: str, flt_lines: str) -> None:
    script = tmp_path / "fake-ffmpeg.sh"
    script.write_text(FAKE_SH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("ENC", enc_lines)
    monkeypatch.setenv("FLT", flt_lines)
    monkeypatch.setattr(config, "FFMPEG", str(script))


def _names(checks: list[dict]) -> dict[str, dict]:
    return {c["name"]: c for c in checks}


# --------------------------------------------------------------------------- real binaries
def test_real_ffmpeg_and_ffprobe(engine_root):
    c = P.check_ffmpeg()
    assert c["ok"] and c["level"] == "blocker" and c["version"]
    c = P.check_ffprobe()
    assert c["ok"] and c["version"]
    enc = _names(P.check_encoders())
    assert {"hevc_nvenc", "hevc_encoder", "h264_nvenc"} <= set(enc)
    if enc["hevc_nvenc"]["ok"]:
        assert "compiled in" in enc["hevc_nvenc"]["detail"]
    assert enc["hevc_nvenc"]["level"] == "warn" and enc["hevc_encoder"]["level"] == "blocker"
    flt = _names(P.check_filters())
    assert flt["filters_zscale_tonemap"]["level"] == "blocker" and flt["libplacebo"]["level"] == "info"
    for c in list(enc.values()) + list(flt.values()):
        assert set(c) >= {"name", "ok", "level", "detail"}


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fake binary is a sh script")
def test_fake_ffmpeg_without_nvenc_falls_back_to_libx265(engine_root, tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch,
                 " V....D libx265              libx265 H.265 / HEVC (codec hevc)\n V....D libx264              x264",
                 " ... zscale            V->V       zscale\n .S. tonemap           V->V       tonemap")
    assert P.check_ffmpeg()["version"] == "7.1-fake"
    enc = _names(P.check_encoders())
    assert enc["hevc_nvenc"]["ok"] is False and "libx265" in enc["hevc_nvenc"]["detail"]
    assert enc["hevc_encoder"]["ok"] is True
    assert enc["h264_nvenc"]["ok"] is False
    flt = _names(P.check_filters())
    assert flt["filters_zscale_tonemap"]["ok"] is True and flt["libplacebo"]["ok"] is False


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fake binary is a sh script")
def test_fake_ffmpeg_missing_everything_is_blocker(engine_root, tmp_path, monkeypatch):
    _fake_ffmpeg(tmp_path, monkeypatch,
                 " V....D libx264              x264",
                 " ... scale             V->V       scale")
    enc = _names(P.check_encoders())
    assert enc["hevc_encoder"]["ok"] is False and enc["hevc_encoder"]["level"] == "blocker"
    flt = _names(P.check_filters())
    assert flt["filters_zscale_tonemap"]["ok"] is False and "zscale, tonemap" in flt["filters_zscale_tonemap"]["detail"]


def test_missing_ffmpeg_binary_is_blocker(engine_root, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FFMPEG", str(tmp_path / "nope" / "ffmpeg"))
    monkeypatch.setattr(config, "FFPROBE", str(tmp_path / "nope" / "ffprobe"))
    assert P.check_ffmpeg()["ok"] is False and P.check_ffmpeg()["level"] == "blocker"
    assert P.check_ffprobe()["ok"] is False
    enc = _names(P.check_encoders())
    assert enc["hevc_encoder"]["ok"] is False


# --------------------------------------------------------------------------- python / packages / disk
def test_python_and_packages_and_disk(engine_root):
    assert P.check_python((3, 12, 0))["ok"] and P.check_python((3, 13, 2))["ok"]
    c = P.check_python((3, 11, 9))
    assert c["ok"] is False and c["level"] == "blocker" and "3.12" in c["detail"]
    pk = _names(P.check_packages([("json", "json"), ("no_such_pkg_xyz", "no-such-pkg")]))
    assert pk["pkg_json"]["ok"] is True
    assert pk["pkg_no_such_pkg_xyz"]["ok"] is False and "pip install --user no-such-pkg" in pk["pkg_no_such_pkg_xyz"]["detail"]
    assert all(c["level"] == "warn" for c in pk.values())
    d = P.check_disk(min_free_gb=0.0)
    assert d["ok"] is True and d["free_gb"] >= 0
    d = P.check_disk(min_free_gb=10 ** 9)
    assert d["ok"] is False and d["level"] == "warn"
    d = P.check_disk(root=engine_root / "does" / "not" / "exist", min_free_gb=0.0)
    assert d["ok"] is True


def test_shop_shorts(engine_root):
    c = P.check_shop_shorts()
    assert c["ok"] is False and c["level"] == "warn"
    folder = config.FOOTAGE_ROOT / "Shop shorts"
    folder.mkdir()
    (folder / "VID_1.mp4").write_bytes(b"")
    c = P.check_shop_shorts()
    assert c["ok"] is True and c["entries"] == 1


# --------------------------------------------------------------------------- metrics freshness
def _write_csv(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _set_mtime(p: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(p, (ts, ts))


def test_metrics_freshness_from_timestamp_column(engine_root):
    p = config.METRICS_CSV
    # NOW is 2026-10-18T06:40-04:00 = 10:40Z. Newest row is 2h old; stray older rows ignored.
    _write_csv(p, "media_id,caption,timestamp,views,trial\n"
                  "1,hi,2026-10-15T12:00:00Z,100,1\n"
                  "2,yo,2026-10-18T08:40:00Z,200,1\n"
                  "3,na,2026-10-17 20:00:00,300,1\n")
    _set_mtime(p, NOW - timedelta(hours=2.5))                 # mtime older than the newest row
    fr = P.metrics_freshness(p, NOW)
    assert abs(fr["hours_newest_ts"] - 2.0) < 0.01 and abs(fr["hours_mtime"] - 2.5) < 0.05
    assert abs(fr["hours"] - 2.0) < 0.01 and fr["kind"] == "fallback" and fr["columns"] == ["timestamp"]
    assert "timestamp" in fr["detail"] and "3 rows" in fr["detail"] and "mtime" in fr["detail"]
    c = P.check_metrics(now=NOW)
    assert c["ok"] is True and c["hours"] == 2.0 and c["hours_newest_ts"] == 2.0 and c["hours_mtime"] == 2.5
    assert c["timestamp_kind"] == "fallback"
    c = P.check_metrics(now=NOW + timedelta(hours=30))
    assert c["ok"] is False and "older than 26" in c["detail"]
    c = P.check_metrics(now=NOW, stale_hours=1.0)
    assert c["ok"] is False


def test_metrics_naive_timestamp_is_utc(engine_root):
    p = config.METRICS_CSV
    # newest row is naive: read as UTC -> 2026-10-18 08:40Z = 2 h before NOW (10:40Z)
    _write_csv(p, "media_id,timestamp\n1,2026-10-15T12:00:00Z\n2,2026-10-18 08:40:00\n")
    _set_mtime(p, NOW - timedelta(hours=5))
    fr = P.metrics_freshness(p, NOW)
    assert abs(fr["hours_newest_ts"] - 2.0) < 0.01 and abs(fr["hours"] - 2.0) < 0.01
    assert P._parse_ts("2026-10-18 08:40:00") == datetime(2026, 10, 18, 8, 40, tzinfo=config.UTC)
    assert P._parse_ts("2026-10-18T08:40:00", naive_tz=config.TZ) == datetime(2026, 10, 18, 8, 40, tzinfo=config.TZ)
    assert P._parse_ts("2026-10-18T08:40:00-04:00", naive_tz=config.TZ).utcoffset() == timedelta(hours=-4)
    assert P._parse_ts("garbage") is None and P._parse_ts("") is None


def test_metrics_fresh_file_with_old_publish_timestamps_passes(engine_root):
    """START_HERE day one: nothing posted since 10/15 but the cloud task rewrote the file an
    hour ago. The media timestamp is a publish time, not the pull time -> mtime wins."""
    p = config.METRICS_CSV
    _write_csv(p, "media_id,timestamp,views\n1,2026-10-15T12:00:00Z,100\n")
    _set_mtime(p, NOW - timedelta(hours=1))
    c = P.check_metrics(now=NOW)
    assert c["ok"] is True and abs(c["hours"] - 1.0) < 0.05
    assert c["hours_newest_ts"] > 60 and abs(c["hours_mtime"] - 1.0) < 0.05
    # both stale -> warn
    _set_mtime(p, NOW - timedelta(hours=40))
    c = P.check_metrics(now=NOW)
    assert c["ok"] is False and "is the cloud task running" in c["detail"]
    # a snapshot-like column beats the media timestamp even when the latter is newer
    _write_csv(p, "media_id,timestamp,pulled_at,views\n"
                  "1,2026-10-18T10:00:00Z,2026-10-17T10:40:00Z,100\n")
    _set_mtime(p, NOW - timedelta(hours=40))
    fr = P.metrics_freshness(p, NOW)
    assert fr["kind"] == "snapshot" and fr["columns"] == ["pulled_at"]
    assert abs(fr["hours_newest_ts"] - 24.0) < 0.01 and abs(fr["hours"] - 24.0) < 0.01
    assert P.check_metrics(now=NOW)["ok"] is True
    assert P.check_metrics(now=NOW + timedelta(hours=3))["ok"] is False


def test_metrics_freshness_fallbacks(engine_root):
    p = config.METRICS_CSV
    assert P.check_metrics(now=NOW)["ok"] is False            # missing file
    _write_csv(p, "media_id,views\n1,2\n")
    _set_mtime(p, NOW - timedelta(hours=3))
    fr = P.metrics_freshness(p, NOW)
    assert abs(fr["hours"] - 3.0) < 0.05 and fr["hours_newest_ts"] is None and "mtime" in fr["detail"]
    assert fr["kind"] is None and fr["columns"] == []
    _write_csv(p, "media_id,timestamp\n1,garbage\n")
    fr = P.metrics_freshness(p, NOW)
    assert fr["hours"] is not None and "no timestamp column parsed" in fr["detail"]
    c = P.check_metrics(now=NOW)
    assert c["ok"] is True and c["hours_newest_ts"] is None


# --------------------------------------------------------------------------- clock
def test_clock_offsets_with_injected_now():
    ok, detail = P.clock_offset_matches(NOW, timedelta(hours=-4))
    assert ok and "-04:00" in detail
    ok, _ = P.clock_offset_matches(NOW, timedelta(hours=-5))
    assert not ok
    ok, detail = P.clock_offset_matches(WINTER, timedelta(hours=-5))
    assert ok and "-05:00" in detail
    ok, _ = P.clock_offset_matches(WINTER, timedelta(hours=-4))
    assert not ok
    ok, detail = P.clock_offset_matches(datetime(2026, 10, 18, 10, 40, tzinfo=config.UTC), timedelta(0))
    assert not ok and "+00:00" in detail
    with pytest.raises(ValueError):
        P.clock_offset_matches(datetime(2026, 10, 18, 10, 40))
    c = P.check_clock(now=NOW, local_offset=timedelta(hours=-4))
    assert c["ok"] and c["level"] == "warn"
    c = P.check_clock(now=NOW, local_offset=timedelta(hours=1))
    assert not c["ok"] and "Eastern" in c["detail"]
    # negative non-whole-hour offsets print from the total seconds
    ok, detail = P.clock_offset_matches(NOW, timedelta(hours=-3, minutes=-30))
    assert not ok and "PC clock -03:30" in detail and "-04:00" in detail
    assert P._fmt_offset(timedelta(hours=5, minutes=45)) == "+05:45"
    assert P._fmt_offset(timedelta(hours=-9, minutes=-30)) == "-09:30"
    assert P._fmt_offset(timedelta(0)) == "+00:00" and P._fmt_offset(None) == "?"


# --------------------------------------------------------------------------- files on disk
def test_rules_ledgers_template(engine_root):
    assert P.check_rules_md()["ok"] is False
    safeio.write_text(config.KNOWLEDGE / "RULES.md", "# rules\n")
    assert P.check_rules_md()["ok"] is True

    c = P.check_ledgers()
    assert c["ok"] is False and "hooks" in c["missing"] and "ensure" in c["detail"]
    assert not list(config.LEDGERS.glob("*.csv")), "preflight must never create ledgers"
    from tools import ledgers
    ledgers.ensure_all()
    assert P.check_ledgers()["ok"] is True

    t = _names(P.check_template())
    assert t["template_broll"]["ok"] is False and "template_placeholder" not in t
    tp = config.TEMPLATES / P.TEMPLATE_NAME
    safeio.write_json(tp, {"PLACEHOLDER": True, "shots": [{"start": 0.0, "end": 4.6}]})
    t = _names(P.check_template())
    assert t["template_broll"]["ok"] is True
    assert t["template_placeholder"]["ok"] is False and t["template_placeholder"]["detail"].startswith("!!!")
    tp.unlink()
    safeio.write_json(tp, {"shots": [{"start": 0.0, "end": 4.6, "note": "placeholder"}]})
    t = _names(P.check_template())
    assert t["template_placeholder"]["ok"] is True
    tp.unlink()
    safeio.write_json(tp, {"shots": [{"src": "PLACEHOLDER"}]})
    assert _names(P.check_template())["template_placeholder"]["ok"] is False


def test_engine_lock_and_render_profile(engine_root):
    c = P.check_engine_lock(now=NOW)
    assert c["ok"] and c["level"] == "info"
    lock = config.STATE / "engine.lock"
    safeio.write_json(lock, {"owner": "daily-run", "start": (NOW - timedelta(minutes=5)).isoformat()})
    c = P.check_engine_lock(now=NOW)
    assert c["ok"] and c["owner"] == "daily-run" and abs(c["age_minutes"] - 5) < 0.1
    c = P.check_engine_lock(now=NOW + timedelta(hours=20))
    assert c["ok"] is False and c["level"] == "warn" and "stale" in c["detail"]
    lock.unlink()
    # a naive local ISO start (engine-written, ET) is 5 min old, not 4 h
    safeio.write_json(lock, {"owner": "daily-run", "start": (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()})
    c = P.check_engine_lock(now=NOW)
    assert c["ok"] and abs(c["age_minutes"] - 5) < 0.1
    lock.unlink()
    safeio.write_text(lock, "burn-in\n")
    ts = (NOW - timedelta(minutes=30)).timestamp()
    os.utime(lock, (ts, ts))
    c = P.check_engine_lock(now=NOW)
    assert c["ok"] and abs(c["age_minutes"] - 30) < 0.5

    c = P.check_render_profile(now=NOW)
    assert c["ok"] is False and "bench" in c["detail"]
    prof = config.STATE / "render_profile.json"
    safeio.write_json(prof, {"chosen": "nvenc", "generated_et": (NOW - timedelta(days=3)).isoformat()})
    c = P.check_render_profile(now=NOW)
    assert c["ok"] and "nvenc" in c["detail"] and c["age_days"] == 3.0
    c = P.check_render_profile(now=NOW + timedelta(days=40))
    assert c["ok"] is False and "> 30 days" in c["detail"]
    prof.unlink()
    safeio.write_json(prof, {"chosen": "cpu"})
    ts = (NOW - timedelta(days=45)).timestamp()
    os.utime(prof, (ts, ts))
    assert P.check_render_profile(now=NOW)["ok"] is False
    # encode.Profile writes a naive local "measured_at": read as ET, beats the mtime
    prof.unlink()
    safeio.write_json(prof, {"chosen": "gpu", "measured_at": (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()})
    os.utime(prof, (ts, ts))
    c = P.check_render_profile(now=NOW)
    assert c["ok"] and c["age_days"] == 2.0 and "gpu" in c["detail"]


def test_cache_write_round_trip(engine_root):
    c = P.check_cache_write()
    assert c["ok"] and c["level"] == "blocker"
    assert not list(config.CACHE.glob(".preflight-write-*"))
    c = P.check_cache_write(cache=engine_root.parent / "outside")
    assert c["ok"] is False and c["level"] == "blocker"


def test_maybe_bench_without_render_story(engine_root, monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.render_story", None)   # force ImportError
    c = P.maybe_bench(NOW)
    assert c["ok"] is False and c["level"] == "warn" and "not available" in c["detail"]
    safeio.write_json(config.STATE / "render_profile.json", {"chosen": "nvenc", "generated_et": NOW.isoformat()})
    assert P.maybe_bench(NOW)["ok"] is True


class _FakeProfile:
    chosen = "gpu"
    nvenc = True


def _stub_render_story(monkeypatch, *, fail: bool = False, no_bench: bool = False) -> types.ModuleType:
    stub = types.ModuleType("tools.render_story")
    calls = []

    def bench(max_sessions: int = 8):
        calls.append(max_sessions)
        if fail:
            raise RuntimeError("no sample clip")
        safeio.write_json(config.STATE / "render_profile.json",
                          {"chosen": "gpu", "measured_at": NOW.replace(tzinfo=None).isoformat()}, overwrite=True)
        return _FakeProfile()

    if not no_bench:
        stub.bench = bench
    stub.calls = calls
    monkeypatch.setitem(sys.modules, "tools.render_story", stub)
    return stub


def test_maybe_bench_with_stub_render_story_refreshes_profile(engine_root, monkeypatch):
    stub = _stub_render_story(monkeypatch)
    c = P.maybe_bench(NOW)
    assert c["ok"] is True and c["level"] == "info" and "chosen path gpu" in c["detail"]
    assert stub.calls == [8]
    assert P.check_render_profile(now=NOW)["ok"] is True
    assert P.maybe_bench(NOW)["detail"].startswith("render profile fresh") and stub.calls == [8]
    # stale profile -> bench runs again
    prof = config.STATE / "render_profile.json"
    prof.unlink()
    safeio.write_json(prof, {"chosen": "cpu", "measured_at": (NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()})
    assert P.maybe_bench(NOW)["ok"] is True and stub.calls == [8, 8]
    assert P._chosen_of({"chosen": "cpu"}) == "cpu" and P._chosen_of(object()) == "?" and P._chosen_of(None) == "?"


def test_maybe_bench_failure_and_missing_bench_are_warnings(engine_root, monkeypatch):
    _stub_render_story(monkeypatch, fail=True)
    c = P.maybe_bench(NOW)
    assert c["ok"] is False and c["level"] == "warn" and "RuntimeError" in c["detail"]
    _stub_render_story(monkeypatch, no_bench=True)
    c = P.maybe_bench(NOW)
    assert c["ok"] is False and "no bench()" in c["detail"]


def test_run_benches_by_default_and_reports_profile_after(engine_root, monkeypatch):
    stub = _stub_render_story(monkeypatch)
    s = P.run(now=NOW, write=False)
    names = [c["name"] for c in s["checks"]]
    assert "bench" in names and names.index("bench") < names.index("render_profile")
    by = {c["name"]: c for c in s["checks"]}
    assert by["bench"]["ok"] and by["render_profile"]["ok"] and stub.calls == [8]
    s = P.run(now=NOW, write=False, bench=False)
    assert not any(c["name"] == "bench" for c in s["checks"]) and stub.calls == [8]


# --------------------------------------------------------------------------- run / report / CLI
def test_run_writes_report_and_overwrites(engine_root):
    s = P.run(now=NOW, bench=False)
    assert isinstance(s["ok"], bool) and s["generated_et"].startswith("2026-10-18T06:40:00-04:00")
    assert {"ffmpeg", "ffprobe", "hevc_encoder", "filters_zscale_tonemap", "python", "disk_free",
            "shop_shorts", "metrics_csv", "clock_zone", "rules_md", "ledgers", "template_broll",
            "engine_lock", "render_profile", "cache_write"} <= {c["name"] for c in s["checks"]}
    assert not any(c["name"] == "bench" for c in s["checks"])
    report = config.STATE / "preflight.md"
    assert report.is_file() and "# Preflight" in report.read_text(encoding="utf-8")
    assert s["blockers"] == sum(1 for c in s["checks"] if not c["ok"] and c["level"] == "blocker")
    s2 = P.run(now=NOW + timedelta(hours=1), bench=False)
    assert "07:40" in report.read_text(encoding="utf-8") and s2["report"] == str(report)
    assert not list(config.LEDGERS.glob("*.csv"))


def test_summary_and_markdown_from_injected_checks():
    checks = [P._c("a", True, "blocker", "fine"),
              P._c("b", False, "warn", "!!! LOUD !!! with | pipe"),
              P._c("c", False, "info", "x")]
    s = P.summarize(checks, NOW)
    assert s["ok"] is True and s["blockers"] == 0 and s["warnings"] == 1
    md = P.to_markdown(s)
    assert "READY" in md and "**!!! LOUD !!!" in md and "\\|" in md
    s = P.summarize(checks + [P._c("d", False, "blocker", "dead")], NOW)
    assert s["ok"] is False and "BLOCKED" in P.to_markdown(s)


def test_cli_json_and_exit_code(engine_root, capsys, monkeypatch):
    rc = P.main(["--json", "--no-write", "--no-bench"])
    out = json.loads(capsys.readouterr().out)
    assert rc == (0 if out["ok"] else 1)
    assert not any(c["name"] == "bench" for c in out["checks"])
    assert not (config.STATE / "preflight.md").exists()
    seen = {}

    def fake_all_checks(now, bench=True):
        seen["bench"] = bench
        return [P._c("x", False, "blocker", "boom")]

    monkeypatch.setattr(P, "all_checks", fake_all_checks)
    assert P.main([]) == 1 and seen["bench"] is True
    capsys.readouterr()
    assert P.main(["--no-bench"]) == 1 and seen["bench"] is False
    text = capsys.readouterr().out
    assert "FAIL x: boom" in text and "BLOCKED" in text
    assert (config.STATE / "preflight.md").is_file()
