"""Tests for tools.preflight: real ffmpeg structure, faked ffmpeg lists, freshness, clock, files."""
from __future__ import annotations

import json
import os
import stat
import sys
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


def test_metrics_freshness_from_timestamp_column(engine_root):
    p = config.METRICS_CSV
    # NOW is 2026-10-18T06:40-04:00 = 10:40Z. Newest row is 2h old; stray older rows ignored.
    _write_csv(p, "media_id,caption,timestamp,views,trial\n"
                  "1,hi,2026-10-15T12:00:00Z,100,1\n"
                  "2,yo,2026-10-18T08:40:00Z,200,1\n"
                  "3,na,2026-10-17 20:00:00,300,1\n")
    hours, why = P.metrics_freshness(p, NOW)
    assert abs(hours - 2.0) < 0.01 and "timestamp" in why and "3 rows" in why
    c = P.check_metrics(now=NOW)
    assert c["ok"] is True and c["hours"] == 2.0
    c = P.check_metrics(now=NOW + timedelta(hours=30))
    assert c["ok"] is False and "older than 26" in c["detail"]
    c = P.check_metrics(now=NOW, stale_hours=1.0)
    assert c["ok"] is False


def test_metrics_freshness_fallbacks(engine_root):
    p = config.METRICS_CSV
    assert P.check_metrics(now=NOW)["ok"] is False            # missing file
    _write_csv(p, "media_id,views\n1,2\n")
    ts = (NOW - timedelta(hours=3)).timestamp()
    os.utime(p, (ts, ts))
    hours, why = P.metrics_freshness(p, NOW)
    assert abs(hours - 3.0) < 0.05 and "mtime" in why
    _write_csv(p, "media_id,timestamp\n1,garbage\n")
    hours, why = P.metrics_freshness(p, NOW)
    assert hours is not None and "no timestamp column parsed" in why


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


# --------------------------------------------------------------------------- run / report / CLI
def test_run_writes_report_and_overwrites(engine_root):
    s = P.run(now=NOW)
    assert isinstance(s["ok"], bool) and s["generated_et"].startswith("2026-10-18T06:40:00-04:00")
    assert {"ffmpeg", "ffprobe", "hevc_encoder", "filters_zscale_tonemap", "python", "disk_free",
            "shop_shorts", "metrics_csv", "clock_zone", "rules_md", "ledgers", "template_broll",
            "engine_lock", "render_profile", "cache_write"} <= {c["name"] for c in s["checks"]}
    assert not any(c["name"] == "bench" for c in s["checks"])
    report = config.STATE / "preflight.md"
    assert report.is_file() and "# Preflight" in report.read_text(encoding="utf-8")
    assert s["blockers"] == sum(1 for c in s["checks"] if not c["ok"] and c["level"] == "blocker")
    s2 = P.run(now=NOW + timedelta(hours=1))
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
    rc = P.main(["--json", "--no-write"])
    out = json.loads(capsys.readouterr().out)
    assert rc == (0 if out["ok"] else 1)
    assert not (config.STATE / "preflight.md").exists()
    monkeypatch.setattr(P, "all_checks", lambda now, bench=False: [P._c("x", False, "blocker", "boom")])
    assert P.main([]) == 1
    text = capsys.readouterr().out
    assert "FAIL x: boom" in text and "BLOCKED" in text
    assert (config.STATE / "preflight.md").is_file()
