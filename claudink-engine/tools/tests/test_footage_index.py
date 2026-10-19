"""Tests for tools/footage_index.py (folder selection, probing, capture time, budget, re-index)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tools.tests.conftest import make_clip

NOW = datetime(2026, 10, 18, 9, 30, 0)   # naive -> treated as ET by the module


def _tiny(path: Path, **kw) -> Path:
    kw.setdefault("seconds", 0.5)
    kw.setdefault("w", 160)
    kw.setdefault("h", 90)
    kw.setdefault("audio", "none")
    return make_clip(path, **kw)


@pytest.fixture()
def fake_drive(engine_root):
    """A fake "My Drive" next to the engine root with the folders the spec names."""
    from tools import config
    root = config.FOOTAGE_ROOT
    s1 = root / "Session 1 of 4"
    _tiny(s1 / "video-001_singular_display.mp4", hdr=True,
          quicktime_creationdate="2026-10-11T14:03:22-0400")
    _tiny(s1 / "video-002_singular_display.mp4", hdr=True,
          quicktime_creationdate="2026-10-11T15:10:05-0400")
    _tiny(root / "Akira back" / "IMG_4410.MOV", creation_time="2026-10-12T18:03:22.000000Z")
    _tiny(root / "Random" / "VID_20261013_101010.mp4")
    (root / "Random" / "notes.txt").write_text("not a video", encoding="utf-8")
    _tiny(root / "Personal" / "holiday.mp4")
    # our own output mirror sits inside My Drive and must never be indexed
    _tiny(config.DRIVE_OUT / "video-999.mp4")
    (root / ".hidden").mkdir()
    return root


def test_select_folders_matches_known_prefix_and_footage_names(fake_drive):
    from tools import footage_index as fi
    names = [p.name for p in fi.select_folders()]
    assert names == ["Akira back", "Random", "Session 1 of 4"]
    assert "Personal" not in names and "ClaudeInk Reels" not in names and ".hidden" not in names


def test_camera_detection():
    from tools import footage_index as fi
    assert fi.camera_of("MVI_0012.MP4") == "canon"
    assert fi.camera_of("IMG_4410.MOV") == "iphone"
    assert fi.camera_of("VID_20261013_101010.mp4") == "meta"
    assert fi.camera_of("video-003_singular_display.mp4") == "meta"
    assert fi.camera_of("holiday.mp4") == "unknown"


def test_capture_time_prefers_quicktime_and_converts_creation_time(engine_root):
    from tools import footage_index as fi
    assert fi.capture_time_from_tags({"com.apple.quicktime.creationdate": "2026-10-11T14:03:22-0400",
                                      "creation_time": "2026-10-11T19:00:00.000000Z"}) \
        == "2026-10-11T14:03:22-04:00"
    # UTC -> ET (EDT in October = -04:00; DST handled by config.TZ, never a fixed offset)
    assert fi.capture_time_from_tags({"creation_time": "2026-10-12T18:03:22.000000Z"}) \
        == "2026-10-12T14:03:22-04:00"
    # after DST ends the same conversion gives -05:00
    assert fi.capture_time_from_tags({"creation_time": "2026-11-02T18:03:22Z"}) \
        == "2026-11-02T13:03:22-05:00"
    assert fi.capture_time_from_tags({}) == ""
    assert fi.capture_time_from_tags(None) == ""


def test_index_writes_rows_with_probed_facts(fake_drive):
    from tools import footage_index as fi, safeio
    res = fi.index(None, now=NOW)
    assert res["errors"] == []
    assert res["stopped"] is None
    assert len(res["indexed"]) == 4
    rows = {r["path"]: r for r in safeio.read_csv(fi.ledger_path())}
    assert list(safeio.read_csv(fi.ledger_path())[0].keys()) == fi.FOOTAGE_HEADER
    v1 = rows[str(fake_drive / "Session 1 of 4" / "video-001_singular_display.mp4")]
    assert v1["capture_time"] == "2026-10-11T14:03:22-04:00"
    assert v1["camera"] == "meta"
    assert v1["codec"] == "hevc"
    assert v1["color_transfer"] == "arib-std-b67"
    assert v1["width"] == "160" and v1["height"] == "90"
    assert float(v1["fps"]) == 30.0
    assert abs(float(v1["duration_s"]) - 0.5) < 0.1
    assert v1["folder"] == "Session 1 of 4" and v1["session"] == "Session 1 of 4"
    assert v1["indexed_at"] == "2026-10-18T09:30:00-04:00"
    assert v1["used_in"] == "" and v1["used_as_opener_in"] == ""
    assert int(v1["size_bytes"]) == (fake_drive / "Session 1 of 4" / "video-001_singular_display.mp4").stat().st_size
    img = rows[str(fake_drive / "Akira back" / "IMG_4410.MOV")]
    assert img["camera"] == "iphone"
    assert img["capture_time"] == "2026-10-12T14:03:22-04:00"
    assert img["color_transfer"] == "bt709"
    vid = rows[str(fake_drive / "Random" / "VID_20261013_101010.mp4")]
    assert vid["camera"] == "meta" and vid["capture_time"] == ""
    assert not any("Personal" in p or "ClaudeInk Reels" in p for p in rows)
    # the budget file records the bytes read today, dated in ET
    budget = json.loads((fi.budget_path()).read_text(encoding="utf-8"))
    assert budget["date"] == "2026-10-18"
    assert budget["bytes"] == res["bytes_counted"] > 0


def test_rotation_is_recorded(engine_root):
    from tools import config, footage_index as fi
    folder = config.FOOTAGE_ROOT / "Session 3"
    _tiny(folder / "MVI_0007.MP4", rotate=90, audio="sine")
    res = fi.index([folder], now=NOW)
    assert res["errors"] == []
    row = fi._existing_rows()[str(folder / "MVI_0007.MP4")]
    assert abs(int(row["rotation"])) == 90
    assert row["camera"] == "canon"


def test_reindex_skips_unchanged_and_reprobes_changed_size(fake_drive):
    from tools import footage_index as fi, safeio
    first = fi.index(None, now=NOW)
    assert len(first["indexed"]) == 4
    second = fi.index(None, now=NOW)
    assert second["indexed"] == []
    assert len(second["skipped_existing"]) == 4
    assert second["bytes_counted"] == 0
    # a changed file (different size) is probed again and its row replaced, not duplicated
    target = fake_drive / "Akira back" / "IMG_4410.MOV"
    _tiny(target, seconds=1.0, creation_time="2026-10-12T20:00:00Z")
    third = fi.index(None, now=NOW)
    assert third["indexed"] == [str(target)]
    rows = safeio.read_csv(fi.ledger_path())
    assert len(rows) == 4
    row = next(r for r in rows if r["path"] == str(target))
    assert row["capture_time"] == "2026-10-12T16:00:00-04:00"
    assert int(row["size_bytes"]) == target.stat().st_size


def test_reindex_keeps_used_in_columns(fake_drive):
    from tools import footage_index as fi, safeio
    fi.index(None, now=NOW)
    target = fake_drive / "Random" / "VID_20261013_101010.mp4"
    rows = safeio.read_csv(fi.ledger_path())
    for r in rows:
        if r["path"] == str(target):
            r["used_in"] = "shoulder7h_A"
            r["used_as_opener_in"] = "shoulder7h_A"
    safeio.rewrite_csv(fi.ledger_path(), rows, fi.FOOTAGE_HEADER)
    _tiny(target, seconds=1.0)
    fi.index(None, now=NOW)
    row = fi._existing_rows()[str(target)]
    assert row["used_in"] == "shoulder7h_A" and row["used_as_opener_in"] == "shoulder7h_A"
    assert abs(float(row["duration_s"]) - 1.0) < 0.1


def test_budget_stops_probing(fake_drive):
    from tools import footage_index as fi, safeio
    folder = fake_drive / "Session 1 of 4"
    clips = fi.list_videos(folder)
    assert len(clips) == 2
    cap = clips[0].stat().st_size + 1          # room for exactly one file
    res = fi.index([folder], now=NOW, max_bytes=cap)
    assert res["stopped"] == "budget"
    assert res["indexed"] == [str(clips[0])]
    assert res["skipped_budget"] == [str(clips[1])]
    assert res["bytes_counted"] == clips[0].stat().st_size
    assert len(safeio.read_csv(fi.ledger_path())) == 1
    # the day's total persists: the next run with the same cap probes nothing new
    again = fi.index([folder], now=NOW, max_bytes=cap)
    assert again["indexed"] == [] and again["stopped"] == "budget"
    assert again["skipped_existing"] == [str(clips[0])]
    # a new day resets the budget
    tomorrow = fi.index([folder], now=datetime(2026, 10, 19, 8, 0, 0), max_bytes=cap)
    assert tomorrow["indexed"] == [str(clips[1])]


def test_probe_cap_counts_less_than_file_size(fake_drive):
    from tools import footage_index as fi
    folder = fake_drive / "Session 1 of 4"
    res = fi.index([folder], now=NOW, probe_cap=1000)
    assert res["bytes_counted"] == 2000
    assert len(res["indexed"]) == 2


def test_dry_run_writes_nothing(fake_drive):
    from tools import footage_index as fi
    res = fi.index(None, now=NOW, dry_run=True)
    assert res["dry_run"] is True
    assert len(res["indexed"]) == 4
    assert not fi.ledger_path().exists()
    assert not fi.budget_path().exists()


def test_bad_file_is_an_error_not_a_crash(engine_root):
    from tools import config, footage_index as fi
    folder = config.FOOTAGE_ROOT / "Sesh 4"
    folder.mkdir(parents=True)
    (folder / "VID_broken.mp4").write_bytes(b"not a video at all")
    good = _tiny(folder / "VID_ok.mp4")
    res = fi.index([folder], now=NOW)
    assert res["indexed"] == [str(good)]
    assert len(res["errors"]) == 1 and res["errors"][0]["path"].endswith("VID_broken.mp4")


def test_non_canonical_header_is_refused(engine_root):
    from tools import footage_index as fi
    fi.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    fi.ledger_path().write_text("path,size\n", encoding="utf-8")
    with pytest.raises(fi.FootageIndexError):
        fi.index([], now=NOW)


def test_cli_json_and_folder_names(fake_drive, capsys):
    from tools import footage_index as fi
    rc = fi.main(["--folders", "Akira back", "--json", "--pause", "0"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["indexed"]) == 1 and out["indexed"][0].endswith("IMG_4410.MOV")
    rc = fi.main(["--folders", "ClaudeInk Reels", "--pause", "0"])
    assert rc == 1
    rc = fi.main(["--dry-run", "--pause", "0"])
    assert rc == 0
    assert "would index" in capsys.readouterr().out


def test_fallback_append_when_ledgers_module_is_missing(fake_drive, monkeypatch):
    from tools import footage_index as fi, safeio
    monkeypatch.setattr(fi, "_ledgers", lambda: None)
    res = fi.index([fake_drive / "Akira back"], now=NOW)
    assert res["errors"] == [] and len(res["indexed"]) == 1
    rows = safeio.read_csv(fi.ledger_path())
    assert len(rows) == 1 and list(rows[0].keys()) == fi.FOOTAGE_HEADER
    assert rows[0]["camera"] == "iphone"


def test_budget_survives_an_interrupted_run(fake_drive, monkeypatch):
    """Ctrl-C (or a lock takeover) on the Nth probe must not lose the bytes already pulled."""
    from tools import footage_index as fi
    folder = fake_drive / "Session 1 of 4"
    clips = fi.list_videos(folder)
    real_probe = fi.probe
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_probe(path)

    monkeypatch.setattr(fi, "probe", flaky)
    with pytest.raises(KeyboardInterrupt):
        fi.index([folder], now=NOW)
    budget = json.loads(fi.budget_path().read_text(encoding="utf-8"))
    assert budget["date"] == "2026-10-18"
    # the first file was probed and the second was started: both are on the books
    assert budget["bytes"] == clips[0].stat().st_size + clips[1].stat().st_size
    # the next run starts from that total, not from zero
    assert fi.load_budget(NOW)["bytes"] == budget["bytes"]
    res = fi.index([folder], now=NOW, max_bytes=budget["bytes"])
    assert res["stopped"] == "budget" and res["indexed"] == []


def test_fallback_reindex_replaces_row_and_keeps_used_in(fake_drive, monkeypatch):
    """Without tools.ledgers a changed file still gets ONE row and keeps used_in."""
    from tools import footage_index as fi, safeio
    monkeypatch.setattr(fi, "_ledgers", lambda: None)
    folder = fake_drive / "Random"
    target = folder / "VID_20261013_101010.mp4"
    fi.index([folder], now=NOW)
    rows = safeio.read_csv(fi.ledger_path())
    rows[0]["used_in"] = "shoulder7h_A"
    rows[0]["used_as_opener_in"] = "shoulder7h_A"
    safeio.rewrite_csv(fi.ledger_path(), rows, fi.FOOTAGE_HEADER)
    _tiny(target, seconds=1.0)
    res = fi.index([folder], now=NOW)
    assert res["indexed"] == [str(target)] and res["errors"] == []
    rows = safeio.read_csv(fi.ledger_path())
    assert len(rows) == 1
    assert rows[0]["used_in"] == "shoulder7h_A" and rows[0]["used_as_opener_in"] == "shoulder7h_A"
    assert int(rows[0]["size_bytes"]) == target.stat().st_size
    assert abs(float(rows[0]["duration_s"]) - 1.0) < 0.1


def test_bom_header_is_tolerated(fake_drive):
    """An Excel-saved footage.csv starts with a UTF-8 BOM; ledgers.py accepts it, so do we."""
    from tools import footage_index as fi, safeio
    fi.index([fake_drive / "Akira back"], now=NOW)
    raw = fi.ledger_path().read_bytes()
    fi.ledger_path().write_bytes(b"\xef\xbb\xbf" + raw)
    rows = fi._existing_rows()
    assert list(rows.values())[0]["path"].endswith("IMG_4410.MOV")
    res = fi.index([fake_drive / "Akira back"], now=NOW)
    assert res["skipped_existing"] and res["indexed"] == []
    assert len(safeio.read_csv(fi.ledger_path())) == 1


def test_probe_stores_stat_size_not_ffprobe_size(fake_drive, monkeypatch):
    from tools import footage_index as fi, safeio
    clip = fake_drive / "Akira back" / "IMG_4410.MOV"
    real = safeio.run_ffprobe

    def lying(args, timeout=120):
        r = real(args, timeout=timeout)
        d = json.loads(r.stdout)
        d["format"]["size"] = "12345"          # Drive still syncing: header size != stat size
        r.stdout = json.dumps(d)
        return r

    monkeypatch.setattr(safeio, "run_ffprobe", lying)
    facts = fi.probe(clip)
    assert facts["size_bytes"] == clip.stat().st_size and facts["probe_size"] == 12345
    first = fi.index([clip.parent], now=NOW)
    assert first["indexed"] == [str(clip)]
    again = fi.index([clip.parent], now=NOW)
    assert again["indexed"] == [] and again["skipped_existing"] == [str(clip)]


def test_resolve_folders_refuses_personal_and_outside(fake_drive, tmp_path):
    from tools import config, footage_index as fi
    assert fi.resolve_folders(["Akira back"]) == [fake_drive / "Akira back"]
    assert fi.resolve_folders([str(fake_drive / "Random")]) == [fake_drive / "Random"]
    with pytest.raises(fi.FootageIndexError, match="not a footage folder"):
        fi.resolve_folders(["Personal"])
    assert fi.resolve_folders(["Personal"], force=True) == [fake_drive / "Personal"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(fi.FootageIndexError, match="outside the footage root"):
        fi.resolve_folders([str(outside)])
    with pytest.raises(fi.FootageIndexError, match="outside the footage root"):
        fi.resolve_folders([str(outside)], force=True)
    with pytest.raises(fi.FootageIndexError, match="output folder"):
        fi.resolve_folders([str(config.DRIVE_OUT)], force=True)
    with pytest.raises(fi.FootageIndexError):
        fi.resolve_folders([str(fake_drive)])            # the whole Drive is never one folder
    with pytest.raises(fi.FootageIndexError, match="not a folder"):
        fi.resolve_folders(["Nope"])
    # a sub-folder of a known session inherits its status
    sub = fake_drive / "Session 1 of 4" / "part2"
    sub.mkdir()
    assert fi.resolve_folders([sub]) == [sub]


def test_cli_folders_needs_a_value_and_force(fake_drive, capsys):
    from tools import footage_index as fi
    with pytest.raises(SystemExit) as ex:
        fi.main(["--folders", "--dry-run"])
    assert ex.value.code == 2
    assert fi.main(["--folders", "Personal", "--json", "--pause", "0"]) == 1
    assert "not a footage folder" in json.loads(capsys.readouterr().out)["error"]
    assert fi.main(["--folders", "Personal", "--force", "--json", "--pause", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["indexed"]) == 1 and out["indexed"][0].endswith("holiday.mp4")
    assert out["probe_cap"] == 32 * 1024 ** 2
    assert fi.main(["--folders", "Random", "--probe-cap", "0", "--json", "--pause", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["probe_cap"] is None
