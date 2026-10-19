"""Tests for the fast render engine (CPU path runs here; GPU paths are checked as command builders)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.tests.conftest import make_clip


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,width,height,pix_fmt,color_transfer:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    d = json.loads(out)
    v = d["streams"][0]
    v["duration"] = float(d["format"]["duration"])
    return v


GPU = dict(nvdec=True, nvenc=True, nvenc_sessions=5, h264_nvenc=True, libplacebo=True, libx265=True, chosen="gpu")
HLG_INFO = {"codec": "hevc", "width": 2144, "height": 2864, "pix_fmt": "yuv420p10le", "transfer": "arib-std-b67",
            "is_hlg": True, "is_10bit": True, "has_audio": True, "duration": 30.0, "rotation": 0}
SDR_INFO = {"codec": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "transfer": "bt709",
            "is_hlg": False, "is_10bit": False, "has_audio": True, "duration": 10.0, "rotation": 90}


def test_detect_lists_capabilities(engine_root):
    from tools import encode
    p = encode.detect(cpu_count=4)
    assert p.libx264 and p.cpu_workers == 2 and p.chosen == "cpu"


def test_gpu_builders_use_nvdec_scale_cuda_placebo_nvenc(engine_root):
    from tools import encode
    gpu = encode.Profile(**GPU)
    assert encode.decode_args(gpu, HLG_INFO) == ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    hlg_chain = encode.video_chain(gpu, HLG_INFO, "hlg")
    # scale_cuda takes no expressions: the fit (never an upscale) is computed from the probed size
    assert encode.fit_size(2144, 2864, 1440, 2560) == (1440, 1922)
    assert hlg_chain.startswith("scale_cuda=1440:1922:format=p010le,hwdownload,format=p010le")
    assert "pad=1440:2560" in hlg_chain and "zscale" not in hlg_chain and "libplacebo" not in hlg_chain
    sdr_chain = encode.video_chain(gpu, HLG_INFO, "sdr")
    assert "libplacebo=tonemapping=hable" in sdr_chain and "zscale" not in sdr_chain
    enc = encode.encoder_args(gpu, "hlg")
    assert enc[:2] == ["-c:v", "hevc_nvenc"] and "main10" in enc and "p010le" in enc and "arib-std-b67" in enc
    chat = encode.chat_encoder_args(gpu, "sdr", 4000)
    assert "-multipass" in chat and "fullres" in chat and "-maxrate" in chat and "bt709" in chat
    assert encode.encoder_args(gpu, "sdr", preview=True)[:2] == ["-c:v", "h264_nvenc"]
    assert gpu.workers == 5


def test_cpu_builders_fall_back(engine_root):
    from tools import encode
    cpu = encode.Profile(libx265=True, chosen="cpu", cpu_workers=2)
    assert encode.decode_args(cpu, HLG_INFO) == []
    chain = encode.video_chain(cpu, HLG_INFO, "sdr")
    # CPU scale fits inside the canvas with an expression (never upscales, keeps aspect, even sizes)
    assert chain.startswith("scale=w='min(iw,1440)':h='min(ih,2560)':force_original_aspect_ratio=decrease")
    assert "tonemap=tonemap=hable" in chain and "format=yuv420p," in chain
    chain_hlg = encode.video_chain(cpu, HLG_INFO, "hlg")
    assert "format=yuv420p10le" in chain_hlg
    # never upscale: a 1080-wide Canon clip stays 1080 wide and is padded
    narrow = encode.video_chain(cpu, SDR_INFO, "sdr")
    assert narrow.startswith("scale=w='min(iw,1440)':h='min(ih,2560)'") and "pad=1440:2560" in narrow
    assert encode.fit_size(1080, 1920, 1440, 2560) == (1080, 1920)
    assert encode.fit_size(0, 0, 1440, 2560) == (1440, -2)
    up = encode.video_chain(cpu, SDR_INFO, "hlg")
    assert "t=arib-std-b67" in up
    enc = encode.encoder_args(cpu, "hlg")
    assert enc[:2] == ["-c:v", "libx265"] and "yuv420p10le" in enc
    assert cpu.workers == 2


def test_chat_target_kbps_maths():
    from tools import encode
    assert 4100 <= encode.chat_target_kbps(45) <= 4300
    assert 2350 <= encode.chat_target_kbps(75) <= 2500
    assert encode.chat_target_kbps(1000) >= 300
    assert encode.chat_target_kbps(5) == 12000


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    a = make_clip(d / "video-001.mp4", seconds=2.0, audio="speech", hdr=True,
                  quicktime_creationdate="2026-10-11T14:00:00-0400")
    b = make_clip(d / "video-002.mp4", seconds=1.5, audio="speech")
    c = make_clip(d / "MVI_0001.mp4", seconds=1.5, audio="sine", rotate=90)
    return a, b, c


def test_segments_parallel_cache_join_chat_multi(engine_root, clips):
    from tools import config, encode
    a, b, c = clips
    prof = encode.Profile(libx265=True, chosen="cpu", cpu_workers=2, cpu_preset="ultrafast")
    jobs = [encode.SegJob(a, 0.2, 1.4, "sdr"), encode.SegJob(b, 0.0, 1.0, "sdr"), encode.SegJob(c, 0.1, 1.1, "sdr")]
    calls = {"n": 0}
    from tools import safeio

    def counting_runner(args, out, **kw):
        calls["n"] += 1
        return safeio.run_ffmpeg(args, out, **kw)

    res = encode.render_segments(jobs, prof, runner=counting_runner)
    assert len(res) == 3 and all(not r["cached"] for r in res) and calls["n"] == 3
    for r, j in zip(res, jobs):
        v = _probe(r["path"])
        assert (v["width"], v["height"]) == (1440, 2560) and v["codec_name"] == "hevc"
        assert v["color_transfer"] == "bt709" and abs(v["duration"] - j.duration) < 0.15
    # second run: everything comes from the cache, ffmpeg is not called
    calls["n"] = 0
    res2 = encode.render_segments(jobs, prof, runner=counting_runner)
    assert all(r["cached"] for r in res2) and calls["n"] == 0
    # an EDL fix touches one segment only
    jobs[1] = encode.SegJob(b, 0.0, 1.2, "sdr")
    res3 = encode.render_segments(jobs, prof, runner=counting_runner)
    assert [r["cached"] for r in res3] == [True, False, True] and calls["n"] == 1
    # join: stream-copied video, duration = sum
    master = config.CACHE / "master.mp4"
    j = encode.join_segments([r["path"] for r in res3], master, expected=sum(x.duration for x in jobs))
    assert j["ok"], j
    v = _probe(master)
    assert (v["width"], v["height"]) == (1440, 2560) and v["codec_name"] == "hevc"
    assert encode.decode_errors(master) == 0
    # chat copy under the cap, decodes clean
    chat = encode.make_chat_copy(master, config.CACHE / "master_chat.mp4", prof, "sdr", v["duration"])
    assert chat["ok"] and chat["bytes"] <= config.CHAT_COPY_MAX_BYTES and encode.decode_errors(chat["path"]) == 0
    # one decode, many outputs
    outs = encode.multi_output(master, [
        (encode.proxy_chain(False), config.CACHE / "gates" / "proxy.mp4"),
        ("fps=4,scale=160:-2,tile=8x2", config.CACHE / "gates" / "strip.jpg"),
    ])
    assert all(o.exists() and o.stat().st_size > 0 for o in outs)
    pv = _probe(outs[0])
    assert (pv["width"], pv["height"]) == (360, 640)


def test_hlg_master_keeps_hlg_tags(engine_root, clips):
    from tools import config, encode
    a, _, _ = clips
    prof = encode.Profile(libx265=True, chosen="cpu", cpu_workers=2, cpu_preset="ultrafast")
    jobs = [encode.SegJob(a, 0.0, 0.8, "hlg"), encode.SegJob(a, 1.0, 1.8, "hlg")]
    res = encode.render_segments(jobs, prof)
    master = config.CACHE / "hlg_master.mp4"
    j = encode.join_segments([r["path"] for r in res], master)
    assert j["ok"]
    v = _probe(master)
    assert v["color_transfer"] == "arib-std-b67" and v["pix_fmt"] == "yuv420p10le" and v["codec_name"] == "hevc"


def test_profile_roundtrip_and_prune(engine_root, clips):
    from tools import encode
    p = encode.Profile(**GPU)
    encode.save_profile(p)
    q = encode.load_profile()
    assert q.chosen == "gpu" and q.nvenc_sessions == 5
    # stale profile keeps data but warns
    stale = encode.load_profile(max_age_days=0, now=encode.profile_path().stat().st_mtime + 10 * 86400)
    assert any("re-run bench" in n for n in stale.notes)
    d = encode.seg_cache_dir()
    (d / "old.mp4").write_bytes(b"x")
    import os, time
    os.utime(d / "old.mp4", (time.time() - 10 * 86400, time.time() - 10 * 86400))
    assert encode.prune_segment_cache(days=7) == 1


@pytest.mark.slow
def test_bench_runs_on_cpu_only_box(engine_root):
    from tools import encode
    prof = encode.bench(max_sessions=2)
    assert prof.chosen in ("cpu", "gpu") and encode.profile_path().exists()
    assert "cpu_tonemap" in prof.fps_per_path


def test_stage_clip_copies_once(engine_root, clips):
    from tools import config, encode
    a, _, _ = clips
    c1 = encode.stage_clip(a)
    assert c1.exists() and encode.stage_clip(a) == c1 and c1.parent == config.CACHE / "clips"
    assert encode.stage_clip(c1) == c1          # already cached paths pass through
    assert a.exists()                            # the source is untouched
