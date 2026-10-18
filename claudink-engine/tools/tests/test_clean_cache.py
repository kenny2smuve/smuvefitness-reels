"""Tests for tools.clean_cache: retention per subfolder, size cap, dry-run, symlink refusal."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import clean_cache as CC
from tools import config, safeio

NOW = datetime(2026, 10, 18, 6, 40, tzinfo=config.TZ)


def _touch(p: Path, *, days_old: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    ts = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(p, (ts, ts))
    return p


def test_retention_for():
    assert CC.retention_for(Path("transcripts/a.json"), 3) == 30
    assert CC.retention_for(Path("panel/p1/x.png"), 3) == 14
    assert CC.retention_for(Path("segs/abc.mp4"), 3) == 7
    assert CC.retention_for(Path("clips/x.mp4"), 3) == 3
    assert CC.retention_for(Path("x.mp4"), 5) == 5


def test_age_retention_per_subfolder(engine_root):
    c = config.CACHE
    old_root = _touch(c / "clips" / "old.mp4", days_old=5)
    new_root = _touch(c / "clips" / "new.mp4", days_old=1)
    tr_keep = _touch(c / "transcripts" / "keep.json", days_old=20)
    tr_drop = _touch(c / "transcripts" / "drop.json", days_old=40)
    pn_keep = _touch(c / "panel" / "keep.png", days_old=10)
    pn_drop = _touch(c / "panel" / "drop.png", days_old=20)
    sg_keep = _touch(c / "segs" / "keep.mp4", days_old=5)
    sg_drop = _touch(c / "segs" / "drop.mp4", days_old=10)
    gitkeep = _touch(c / ".gitkeep", days_old=400, size=0)
    res = CC.clean(now=NOW)
    assert res["deleted"] == 4 and res["bytes_freed"] == 40
    assert res["kept"] == 5
    for p in (old_root, tr_drop, pn_drop, sg_drop):
        assert not p.exists(), p
    for p in (new_root, tr_keep, pn_keep, sg_keep, gitkeep):
        assert p.exists(), p
    assert {d["reason"] for d in res["deleted_files"]} == {"age"}
    assert res["refused"] == [] and res["dry_run"] is False


def test_size_cap_deletes_oldest_first(engine_root):
    c = config.CACHE
    a = _touch(c / "a.bin", days_old=2.5, size=600)
    b = _touch(c / "b.bin", days_old=1.5, size=600)
    d = _touch(c / "d.bin", days_old=0.5, size=600)
    max_gb = 1000 / 1024 ** 3                     # 1000 bytes budget
    res = CC.clean(now=NOW, max_gb=max_gb)
    assert res["deleted"] == 2 and res["bytes_freed"] == 1200
    assert not a.exists() and not b.exists() and d.exists()
    assert {x["reason"] for x in res["deleted_files"]} == {"size"}
    assert res["kept"] == 1 and res["kept_bytes"] == 600


def test_dry_run_deletes_nothing(engine_root):
    c = config.CACHE
    old = _touch(c / "sub" / "old.mp4", days_old=9)
    (c / "empty").mkdir()
    res = CC.clean(now=NOW, dry_run=True)
    assert res["deleted"] == 1 and res["bytes_freed"] == 10 and res["dry_run"] is True
    assert old.exists() and (c / "empty").is_dir()
    assert str(c / "empty") in res["dirs_removed"]
    res = CC.clean(now=NOW)
    assert not old.exists() and not (c / "empty").exists() and not (c / "sub").exists()
    assert c.is_dir()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlinks need privileges on Windows")
def test_symlinks_are_refused_and_targets_untouched(engine_root, tmp_path):
    c = config.CACHE
    outside = tmp_path / "precious" / "footage.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"keep")
    ts = (NOW - timedelta(days=30)).timestamp()
    os.utime(outside, (ts, ts))
    link = c / "link.mp4"
    link.symlink_to(outside)
    dlink = c / "linkdir"
    dlink.symlink_to(outside.parent, target_is_directory=True)
    real_old = _touch(c / "old.bin", days_old=10)
    res = CC.clean(now=NOW)
    assert outside.exists() and outside.read_bytes() == b"keep"
    assert link.is_symlink() and dlink.is_symlink()
    assert not real_old.exists() and res["deleted"] == 1
    assert sorted(res["refused"]) == sorted([str(link), str(dlink)])


def test_refuses_folder_outside_cache(engine_root, tmp_path):
    other = tmp_path / "not-cache"
    other.mkdir()
    _touch(other / "x.bin", days_old=30)
    with pytest.raises(safeio.SafeIOError):
        CC.clean(cache=other, now=NOW)
    assert (other / "x.bin").exists()
    sub = config.CACHE / "segs"
    _touch(sub / "old.mp4", days_old=30)
    res = CC.clean(cache=sub, now=NOW)
    assert res["deleted"] == 1


def test_plan_is_pure():
    files = [{"path": Path("/c/a"), "rel": Path("a"), "size": 5, "mtime": (NOW - timedelta(days=4)).timestamp()},
             {"path": Path("/c/b"), "rel": Path("b"), "size": 5, "mtime": (NOW - timedelta(days=1)).timestamp()}]
    dele, keep = CC.plan(files, now=NOW, days=3, max_gb=1)
    assert [f["path"].name for f in dele] == ["a"] and [f["path"].name for f in keep] == ["b"]


def test_cli(engine_root, capsys):
    _touch(config.CACHE / "old.bin", days_old=10)
    assert CC.main(["--dry-run"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("clean_cache: would delete 1 file(s)")
    assert CC.main(["--json", "--days", "3"]) == 0
    import json
    res = json.loads(capsys.readouterr().out)
    assert res["deleted"] == 1
