"""Tests for tools/panel.py (packets, anonymisation, calibration maths, download plan)."""
from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.tests.conftest import make_clip
from tools.tests.stub_transcript import StubTranscript


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("panelclips")
    return make_clip(d / "DebCcJvqDI4.mp4", seconds=7, w=720, h=1280, audio="none")


def _img_size(path: Path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size


# --------------------------------------------------------------------------- packets
def test_build_packet_files_and_dimensions(engine_root, clip):
    from tools import panel
    out = engine_root / "cache" / "packets" / "winner1"
    provider = StubTranscript({clip.name: [(0.4, 0.8, "would"), (0.8, 1.2, "you"), (1.2, 1.6, "sit"),
                                           (3.0, 3.4, "seven"), (3.4, 3.9, "hours")]})
    res = panel.build_packet(clip, out, transcript_provider=provider, hook="Would you sit 7 hours?")
    strip, sheet = out / "strip_0_3s.jpg", out / "sheet_2s.jpg"
    assert strip.is_file() and sheet.is_file()
    sw, sh = _img_size(strip)
    # 7 x 2 cells of 180x320 plus padding/margins
    assert 7 * 180 <= sw <= 7 * 180 + 40 and 2 * 320 <= sh <= 2 * 320 + 20
    # 7 s at one frame every 2 s -> 4 frames -> one row of 5 cells (216x384)
    assert res["sheet_frames"] == 4 and res["sheet_rows"] == 1
    w2, h2 = _img_size(sheet)
    assert 5 * 216 <= w2 <= 5 * 216 + 40 and 384 <= h2 <= 384 + 20
    text = (out / "transcript.txt").read_text(encoding="utf-8")
    assert text.splitlines() == ["[0.4-1.6] would you sit", "[3.0-3.9] seven hours"]
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["hook"] == "Would you sit 7 hours?" and meta["size"] == "720x1280"
    assert 6.5 < meta["length_s"] < 7.5 and meta["fps"] == 30.0
    assert res["strip_frames"] == 13


def test_build_packet_without_transcript_or_hook(engine_root, clip):
    from tools import panel
    out = engine_root / "cache" / "packets" / "plain"
    res = panel.build_packet(clip, out)
    assert (out / "transcript.txt").read_text(encoding="utf-8").strip() == "(no transcript)"
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert "hook" not in meta and res["hook"] is None
    with pytest.raises(FileNotFoundError):
        panel.build_packet(engine_root / "nope.mp4", out)


def test_sheet_layout_maths():
    from tools import panel
    assert panel.sheet_layout(45.0, 2.0) == (23, 5)
    assert panel.sheet_layout(7.0, 2.0) == (4, 1)
    assert panel.sheet_layout(0.0, 2.0) == (1, 1)
    assert "tile=5x5:nb_frames=23" in panel.sheet_filter(45.0, 2.0)
    assert "fps=4,select=lt(n\\,13)" in panel.strip_filter() and "tile=7x2" in panel.strip_filter()
    assert "pts\\:hms" in panel.timestamp_filter()


# --------------------------------------------------------------------------- anonymise
def _fake_packet(root: Path, name: str, meta: dict) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for f in ("strip_0_3s.jpg", "sheet_2s.jpg"):
        (d / f).write_bytes(b"\xff\xd8\xff\xd9")
    (d / "transcript.txt").write_text(f"[0.0-1.0] words of {name}\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_anonymize_hides_names_and_mapping_has_all_ids(engine_root):
    from tools import config, panel
    pk = engine_root / "cache" / "packets"
    dirs = [
        _fake_packet(pk, "DebCcJvqDI4", {"length_s": 41.37, "fps": 30, "size": "1080x1920", "source": "DebCcJvqDI4.mp4"}),
        _fake_packet(pk, "DeH4xUmKrxz", {"length_s": 12.0, "fps": 30, "size": "1080x1920", "source": "DeH4xUmKrxz.mp4"}),
        _fake_packet(pk, "DecSt5ZKl46", {"length_s": 20.0, "fps": 30, "size": "1080x1920", "source": "DecSt5ZKl46.mp4"}),
        _fake_packet(pk, "A_1100_shoulder7h", {"length_s": 44.0, "fps": 30, "size": "1440x2560",
                                               "source": "A_1100_shoulder7h.mp4", "hook": "Would you sit 7 hours?"}),
    ]
    res = panel.anonymize(dirs, "r1", 7)
    rdir = engine_root / "cache" / "panel" / "r1"
    assert res["ids"] == ["P01", "P02", "P03", "P04"] and Path(res["dir"]) == rdir
    mapping = json.loads((rdir / "_mapping.json").read_text(encoding="utf-8"))
    assert set(mapping) == {"P01", "P02", "P03", "P04"}
    truths = {Path(v["source"]).name: v["truth"] for v in mapping.values()}
    assert truths == {"DebCcJvqDI4": "winner", "DeH4xUmKrxz": "flop", "DecSt5ZKl46": "median",
                      "A_1100_shoulder7h": "candidate"}
    # the shuffle is seeded: the same seed gives the same order, another seed differs
    expect = list(range(4))
    random.Random(7).shuffle(expect)
    assert [Path(mapping[f"P{i + 1:02d}"]["source"]).name for i in range(4)] == [dirs[j].name for j in expect]
    for pid, entry in mapping.items():
        files = {p.name for p in (rdir / pid).iterdir()}
        assert files == {"strip_0_3s.jpg", "sheet_2s.jpg", "transcript.txt", "meta.json"}
        meta = json.loads((rdir / pid / "meta.json").read_text(encoding="utf-8"))
        assert "source" not in meta and "fps" not in meta and "size" not in meta
        name = Path(entry["source"]).name
        assert name not in json.dumps(meta) and "shoulder7h" not in json.dumps(meta)
        if entry["truth"] == "candidate":
            assert meta == {"length_s": 44.0, "hook": "Would you sit 7 hours?"}
        else:
            assert set(meta) == {"length_s"}          # panel reels: no hook, nothing else
        assert (rdir / pid / "strip_0_3s.jpg").read_bytes().startswith(b"\xff\xd8")
    # explicit truth overrides the shortcode lookup; a re-run with another seed replaces the round
    res2 = panel.anonymize(dirs, "r1", 8, truth={"A_1100_shoulder7h": "winner"})
    mapping2 = panel.load_mapping("r1")
    assert {v["truth"] for v in mapping2.values()} == {"winner", "flop", "median"}
    assert len(list((engine_root / "cache" / "panel" / "r1").iterdir())) == 5  # 4 packets + mapping
    assert res2["n"] == 4
    with pytest.raises(ValueError):
        panel.anonymize(dirs, "../evil", 1)
    with pytest.raises(ValueError):
        panel.anonymize([], "r2", 1)


# --------------------------------------------------------------------------- calibration
def test_calibration_pair_maths():
    from tools import panel
    winners = [f"W{i}" for i in range(7)]
    flops = [f"F{i}" for i in range(4)]
    mapping = {p: {"source": p, "truth": "winner"} for p in winners}
    mapping.update({p: {"source": p, "truth": "flop"} for p in flops})
    mapping["M1"] = {"source": "M1", "truth": "median"}
    # F0 ranked above W5 and W6: 2 misordered pairs out of 28
    ranking = ["W0", "W1", "W2", "W3", "W4", "M1", "F0", "W5", "W6", "F1", "F2", "F3"]
    calls = {"W0": "breakout", "W6": "flop", "F0": "median"}
    res = panel.calibration(ranking, calls, mapping, {})
    assert res["pairs_total"] == 28 and res["pairs_correct"] == 26
    assert res["pair_rate"] == round(26 / 28, 4) and res["calibrated"] is True
    assert res["winners_called_flop"] == ["W6"]
    # flops on top: uncalibrated
    bad = panel.calibration(flops + winners, {}, mapping)
    assert bad["pairs_correct"] == 0 and bad["calibrated"] is False
    # unranked winners count as below everything; an empty panel is never calibrated
    part = panel.calibration(["F0", "W0"], {}, mapping)
    assert part["pairs_correct"] == 3 and "W1" in part["unranked"]
    assert panel.calibration([], {}, {})["calibrated"] is False
    # truth given by source name instead of the mapping's own truth
    m2 = {"P01": {"source": "x/DebCcJvqDI4", "truth": "candidate"}, "P02": {"source": "x/DeH4xUmKrxz", "truth": "candidate"}}
    t = {"DebCcJvqDI4": "winner", "DeH4xUmKrxz": "flop"}
    assert panel.calibration(["P01", "P02"], {}, m2, t)["pairs_correct"] == 1


def test_candidate_ok():
    from tools import panel
    ranking = [f"P{i:02d}" for i in range(1, 13)]          # 12 -> top third = 4
    assert panel.candidate_ok("P04", ranking, {}) is True
    assert panel.candidate_ok("P05", ranking, {}) is False
    assert panel.candidate_ok("P01", ranking, {"P01": "flop"}) is False
    assert panel.candidate_ok("P99", ranking, {}) is False
    assert panel.candidate_ok("A", ["A", "B"], {}) is True   # tiny panels: only the first


# --------------------------------------------------------------------------- downloads
def test_download_plan_caps_and_skips(engine_root):
    from tools import config, panel
    all_codes = panel.panel_codes()
    assert len(all_codes) == 15 and all_codes[0] == config.PANEL_WINNERS[0]
    assert panel.download_plan("burnin", set()) == all_codes[:config.PANEL_DOWNLOADS_BURNIN]
    daily = panel.download_plan("daily", set())
    assert len(daily) == config.PANEL_DOWNLOADS_DAILY
    assert panel.download_plan("daily", set(all_codes[:14])) == [all_codes[14]]
    assert panel.download_plan("burnin", set(all_codes)) == []
    with pytest.raises(ValueError):
        panel.download_plan("weekly", set())


def test_download_dry_run_commands_never_contain_cookies(engine_root):
    from tools import panel
    out = engine_root / "cache" / "panel_dl"
    codes = panel.download_plan("burnin", set())
    res = panel.download(codes, out, dry_run=True, sleeper=lambda s: pytest.fail("slept in dry run"))
    assert len(res) == 15 and all(r["ok"] and r["reason"] == "dry-run" for r in res)
    for r in res:
        cmd = r["cmd"]
        assert cmd[0] == "yt-dlp" and cmd[-1] == f"https://www.instagram.com/reel/{r['code']}/"
        assert "cookies" not in " ".join(cmd).lower()
        assert cmd[cmd.index("-o") + 1] == str(out / (r["code"] + ".%(ext)s"))
    with pytest.raises(ValueError):
        panel.ytdlp_command("not a code!", out)
    from tools import safeio
    with pytest.raises(safeio.SafeIOError):
        panel.download(["DebCcJvqDI4"], Path("/some/where/else"), dry_run=True)


def test_download_stops_on_login_wall_and_sleeps_between(engine_root):
    from tools import panel
    out = engine_root / "cache" / "panel_dl"
    out.mkdir(parents=True)
    (out / "DeE6fyBKWpz.mp4").write_bytes(b"x")       # already there: no request
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def runner(cmd):
        calls.append(cmd)
        code = cmd[-1].rstrip("/").rsplit("/", 1)[-1]
        if code == "DehJPlnKk8I":
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: [Instagram] login required to access this content")
        (out / f"{code}.mp4").write_bytes(b"v")
        return SimpleNamespace(returncode=0, stdout="[download] 100%", stderr="")

    codes = ["DebCcJvqDI4", "DeE6fyBKWpz", "DehJPlnKk8I", "DdCXyjKqI4I", "DdIo7YkgJ9d"]
    res = panel.download(codes, out, dry_run=False, sleeper=sleeps.append, runner=runner,
                         rng=random.Random(1))
    assert [r["code"] for r in calls_codes(calls)] == ["DebCcJvqDI4", "DehJPlnKk8I"]
    assert [r["reason"] for r in res] == ["downloaded", "exists", "login wall",
                                          "skipped: batch stopped (login wall)",
                                          "skipped: batch stopped (login wall)"]
    assert [r["ok"] for r in res] == [True, True, False, False, False]
    assert res[0]["file"].endswith("DebCcJvqDI4.mp4")
    assert len(sleeps) == 1 and 3.0 <= sleeps[0] <= 5.0
    assert panel.stop_reason("", "HTTP Error 429: Too Many Requests") == "rate limited"
    assert panel.stop_reason("[download] 2.1MiB/s bitrate ok", "") is None


def calls_codes(calls):
    return [{"code": c[-1].rstrip("/").rsplit("/", 1)[-1]} for c in calls]


# --------------------------------------------------------------------------- CLI
def test_cli(engine_root, clip, capsys):
    from tools import panel
    out = engine_root / "cache" / "packets" / "cli"
    words = engine_root / "cache" / "words.json"
    words.write_text(json.dumps([[0.5, 0.9, "hello"], [0.9, 1.3, "there"]]), encoding="utf-8")
    assert panel.main(["packet", str(clip), "--out", str(out), "--hook", "H?", "--words", str(words)]) == 0
    assert "packet " in capsys.readouterr().out
    assert (out / "transcript.txt").read_text(encoding="utf-8").startswith("[0.5-1.3] hello there")
    assert panel.main(["--json", "anonymize", str(out), "--round", "r9", "--seed", "3"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ids"] == ["P01"]
    truth = engine_root / "cache" / "truth.json"
    truth.write_text(json.dumps({"P01": "winner"}), encoding="utf-8")
    # one winner, no flops: no pairs -> uncalibrated -> exit 1
    assert panel.main(["calibrate", "--round", "r9", "--ranking", "P01", "--calls", "P01=breakout",
                       "--truth", str(truth)]) == 1
    assert "UNCALIBRATED" in capsys.readouterr().out
    assert panel.main(["--json", "plan", "--mode", "daily"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 6
    assert panel.main(["download", "--mode", "daily", "--out", str(engine_root / "cache" / "dl")]) == 0
    text = capsys.readouterr().out
    assert text.count("dry-run") == 6 and "cookies" not in text
    assert panel.main(["calibrate", "--round", "nope", "--ranking", "P01"]) == 1
