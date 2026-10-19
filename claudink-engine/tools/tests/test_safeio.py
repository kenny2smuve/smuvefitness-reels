"""safeio is the seatbelt: these tests pin every refusal it must make."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_writes_only_inside_roots(engine_root, tmp_path):
    from tools import config, safeio
    safeio.write_text(config.CACHE / "a.txt", "x")
    safeio.write_text(config.DRIVE_OUT / "POST" / "2026-10-19" / "POST_THIS.txt", "x")
    with pytest.raises(safeio.SafeIOError):
        safeio.write_text(tmp_path / "outside.txt", "x")
    with pytest.raises(safeio.SafeIOError):
        safeio.write_text(config.FOOTAGE_ROOT / "Session 1 of 4" / "x.txt", "x")   # his footage tree
    with pytest.raises(safeio.SafeIOError):
        safeio.mkdir(tmp_path / "elsewhere")


def test_never_overwrite_renders_but_regenerate_text(engine_root):
    from tools import config, safeio
    mp4 = config.POST / "2026-10-19" / "A_1100_x.mp4"
    safeio.write_bytes(mp4, b"0")
    with pytest.raises(safeio.SafeIOError):
        safeio.write_bytes(mp4, b"1")
    with pytest.raises(safeio.SafeIOError):
        safeio.write_bytes(mp4, b"1", overwrite=True)        # never, even when asked
    txt = config.POST / "2026-10-19" / "POST_THIS.txt"
    safeio.write_text(txt, "v1")
    with pytest.raises(safeio.SafeIOError):
        safeio.write_text(txt, "v2")                          # must be explicit
    safeio.write_text(txt, "v2", overwrite=True)
    assert txt.read_text(encoding="utf-8") == "v2"
    cache_mp4 = config.CACHE / "seg.mp4"
    safeio.write_bytes(cache_mp4, b"0")
    safeio.write_bytes(cache_mp4, b"1", overwrite=True)       # cache may be rewritten


def test_ffmpeg_cmd_is_safe_by_construction(engine_root, tmp_path):
    from tools import config, safeio
    out = config.CACHE / "o.mp4"
    cmd = safeio.ffmpeg_cmd(["-i", str(tmp_path / "in.mp4"), "-c", "copy"], out)
    assert cmd[1:4] == ["-nostdin", "-hide_banner", "-n"] and "-y" not in cmd and cmd[-1] == str(out)
    with pytest.raises(safeio.SafeIOError):
        safeio.ffmpeg_cmd(["-y", "-i", "a.mp4"], out)
    with pytest.raises(safeio.SafeIOError):
        safeio.ffmpeg_cmd(["-i", str(out)], out)              # output == input
    with pytest.raises(safeio.SafeIOError):
        safeio.ffmpeg_cmd(["-i", "a.mp4"], tmp_path / "x.mp4")  # outside roots
    with pytest.raises(safeio.SafeIOError):
        safeio.run_ffmpeg_analysis(["-y", "-i", "a.mp4"])


def test_deletion_only_in_cache_and_staging(engine_root):
    from tools import config, safeio
    f = config.CACHE / "tmp.bin"
    safeio.write_bytes(f, b"x")
    assert safeio.remove_cache_file(f) is True and not f.exists()
    r = config.RUNS / "2026-10-18.md"
    safeio.write_text(r, "run")
    with pytest.raises(safeio.SafeIOError):
        safeio.remove_cache_file(r)
    staged = safeio.stage_for_deletion(r)
    assert staged.exists() and not r.exists() and safeio.is_under(staged, config.TO_DELETE)
    foot = config.FOOTAGE_ROOT / "Session 1 of 4" / "video-001.mp4"
    foot.parent.mkdir(parents=True, exist_ok=True)
    foot.write_bytes(b"footage")
    with pytest.raises(safeio.SafeIOError):
        safeio.stage_for_deletion(foot)                        # never his footage
    assert foot.exists()


def test_copy_refuses_same_and_outside(engine_root, tmp_path):
    from tools import config, safeio
    src = tmp_path / "src.mp4"
    src.write_bytes(b"abc")
    dst = safeio.copy_file(src, config.POST / "d" / "A.mp4")
    assert dst.read_bytes() == b"abc"
    with pytest.raises(safeio.SafeIOError):
        safeio.copy_file(src, src)
    with pytest.raises(safeio.SafeIOError):
        safeio.copy_file(src, tmp_path / "copy.mp4")
    with pytest.raises(safeio.SafeIOError):
        safeio.copy_file(src, dst)                            # exists


def test_csv_helpers_utf8_and_header(engine_root):
    from tools import config, safeio
    p = config.LEDGERS / "hooks.csv"
    hdr = ["hook", "type", "used_on", "reel", "result_views", "status"]
    safeio.append_csv_row(p, {"hook": "Would you sit 8 hours? — café", "status": "ok", "junk": 1}, hdr)
    safeio.append_csv_row(p, {"hook": "two"}, hdr)
    rows = safeio.read_csv(p)
    assert rows[0]["hook"].startswith("Would you sit 8 hours?") and rows[0]["status"] == "ok" and rows[1]["type"] == ""
    safeio.rewrite_csv(p, [{"hook": "only"}], hdr)
    assert [r["hook"] for r in safeio.read_csv(p)] == ["only"]
    assert list((config.STATE / "ledger-backups").glob("hooks.*.csv"))


def test_is_under_is_case_insensitive_on_normcase(engine_root):
    from tools import config, safeio
    assert safeio.is_under(config.CACHE / "x", config.ENGINE_ROOT)
    assert not safeio.is_under(config.ENGINE_ROOT.parent, config.ENGINE_ROOT)
    assert not safeio.is_under(Path(str(config.ENGINE_ROOT) + "-evil") / "x", config.ENGINE_ROOT)
