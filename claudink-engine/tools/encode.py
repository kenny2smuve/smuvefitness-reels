"""Fast render engine for the ClaudInk kit (SPEC.md "Render speed").

What it owns:
- a render *profile* (what this machine can do: NVDEC, NVENC and how many sessions, libplacebo,
  CPU fallback preset), detected cheaply or measured by `bench` and stored in state/render_profile.json;
- ffmpeg argument builders per profile: decode (GPU or CPU), scale+pad to the canvas (GPU scale when
  possible, pad on CPU), tonemap (libplacebo on the GPU, zscale on the CPU), encoders (hevc_nvenc or
  libx265/libx264), chat-copy bitrate maths;
- parallel, content-addressed segment rendering: every EDL segment becomes its own ffmpeg process,
  `nvenc_sessions` of them at once, cached in cache/segs/<key>.mp4 so an EDL fix re-encodes only the
  segments that changed;
- the stream-copy join (concat demuxer, video copied, audio re-encoded once so AAC priming gaps
  never land at a cut);
- one-decode-many-outputs for proxies, sheets and strips of a finished master.

render_story.py and render_broll.py call this module; nothing else builds ffmpeg video commands.
Every output path goes through safeio (never -y, never outside the engine roots).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import config, safeio

# --------------------------------------------------------------------------- constants
HLG_TRANSFERS = {"arib-std-b67", "smpte2084"}
TONEMAP_CPU = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
               "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
TONEMAP_GPU = ("libplacebo=tonemapping=hable:colorspace=bt709:color_primaries=bt709:"
               "color_trc=bt709:range=tv:format=yuv420p")
SDR_TO_HLG = ("zscale=tin=bt709:t=arib-std-b67:pin=bt709:p=bt2020:min=bt709:m=bt2020nc:"
              "rin=tv:r=tv,format=yuv420p10le")
HLG_TAGS = ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
SDR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
AUDIO_KBPS = 256
AUDIO_ARGS = ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000", "-ac", "2"]
PREVIEW_W, PREVIEW_H = 360, 640
SEG_CACHE_DAYS = 7
PROFILE_MAX_AGE_DAYS = 30


# --------------------------------------------------------------------------- profile
@dataclass
class Profile:
    nvdec: bool = False
    nvenc: bool = False
    nvenc_sessions: int = 0
    h264_nvenc: bool = False
    libplacebo: bool = False
    libx265: bool = False
    libx264: bool = True
    cpu_preset: str = "faster"
    cpu_workers: int = 1
    chosen: str = "cpu"                 # "gpu" | "cpu"
    fps_per_path: dict = field(default_factory=dict)
    measured_at: str = ""
    notes: list = field(default_factory=list)

    @property
    def workers(self) -> int:
        if self.chosen == "gpu" and self.nvenc_sessions > 0:
            return self.nvenc_sessions
        return max(1, self.cpu_workers)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Profile":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _ffmpeg_list(flag: str) -> str:
    try:
        p = subprocess.run([config.FFMPEG, "-hide_banner", flag], capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace")
        return p.stdout + p.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def detect(cpu_count: int | None = None) -> Profile:
    """Cheap capability listing (no encoding). `bench()` turns this into measured facts."""
    enc = _ffmpeg_list("-encoders")
    flt = _ffmpeg_list("-filters")
    hw = _ffmpeg_list("-hwaccels")
    cores = cpu_count or os.cpu_count() or 2
    prof = Profile(
        nvdec="cuda" in hw,
        nvenc="hevc_nvenc" in enc,
        h264_nvenc="h264_nvenc" in enc,
        libplacebo=" libplacebo " in flt or "libplacebo" in flt,
        libx265="libx265" in enc,
        libx264="libx264" in enc,
        cpu_workers=max(1, cores // 2),
    )
    prof.chosen = "cpu"
    prof.notes.append("detect() only lists capabilities; run bench to measure and to enable the GPU path")
    return prof


def profile_path() -> Path:
    return config.STATE / "render_profile.json"


def save_profile(prof: Profile) -> Path:
    return safeio.write_json(profile_path(), prof.to_json(), overwrite=True)


def load_profile(max_age_days: int = PROFILE_MAX_AGE_DAYS, now: float | None = None) -> Profile:
    """The measured profile if present and fresh, else a cheap detection (CPU path)."""
    p = profile_path()
    d = safeio.read_json(p)
    if d:
        prof = Profile.from_json(d)
        try:
            age = ((now if now is not None else time.time()) - p.stat().st_mtime) / 86400.0
        except OSError:
            age = 0
        if age <= max_age_days:
            return prof
        prof.notes.append(f"profile older than {max_age_days} days; re-run bench")
        return prof
    return detect()


# --------------------------------------------------------------------------- probes
def probe(clip: Path | str) -> dict:
    """Header-only facts a renderer needs. Rotation is honoured by ffmpeg autorotate; we only
    report the display size so scale maths are right."""
    p = safeio.run_ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height,pix_fmt,color_transfer,color_primaries,"
                            "color_range,r_frame_rate:stream_side_data=rotation:format=duration",
                            "-of", "json", str(clip)])
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {clip}: {p.stderr.strip()[:300]}")
    d = json.loads(p.stdout or "{}")
    v = (d.get("streams") or [{}])[0]
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                rot = 0
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if rot % 180 == 90:
        w, h = h, w
    a = safeio.run_ffprobe(["-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name,sample_rate,channels", "-of", "json", str(clip)])
    has_audio = bool(json.loads(a.stdout or "{}").get("streams")) if a.returncode == 0 else False
    trc = (v.get("color_transfer") or "").lower()
    return {
        "codec": v.get("codec_name"), "width": w, "height": h, "pix_fmt": v.get("pix_fmt"),
        "transfer": trc, "is_hlg": trc in HLG_TRANSFERS,
        "is_10bit": "10" in (v.get("pix_fmt") or ""), "range": v.get("color_range") or "",
        "rotation": rot, "has_audio": has_audio,
        "duration": float((d.get("format") or {}).get("duration") or 0.0),
    }


# --------------------------------------------------------------------------- arg builders
def decode_args(prof: Profile, info: dict) -> list[str]:
    """Input-side options. GPU decode keeps frames on the device for scale_cuda."""
    if prof.chosen == "gpu" and prof.nvdec and info.get("codec") in ("hevc", "h264", "av1"):
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    return []


def _needs_tonemap(info: dict, transfer_out: str) -> bool:
    return bool(info.get("is_hlg")) and transfer_out == "sdr"


def _needs_sdr_to_hlg(info: dict, transfer_out: str) -> bool:
    return (not info.get("is_hlg")) and transfer_out == "hlg"


def video_chain(prof: Profile, info: dict, transfer_out: str, canvas: tuple[int, int] | None = None,
                preview: bool = False, extra_vf: str = "", fps: int = 30) -> str:
    """Scale (never up) to the canvas width, pad to the canvas, colour-convert when the segment's
    transfer differs from the export, set SAR and fps. GPU scale when the frames are on the GPU."""
    cw, ch = (PREVIEW_W, PREVIEW_H) if preview else (canvas or (config.CANVAS_W, config.CANVAS_H))
    gpu_frames = bool(decode_args(prof, info))
    parts: list[str] = []
    src_w = int(info.get("width") or 0)
    tonemap = _needs_tonemap(info, transfer_out)
    upconvert = _needs_sdr_to_hlg(info, transfer_out)
    # target width: never upscale (pad only when the source is narrower)
    tw = min(cw, src_w) if src_w else cw
    if gpu_frames:
        fmt = "p010le" if (transfer_out == "hlg" and not tonemap) else ("p010le" if info.get("is_10bit") else "nv12")
        parts.append(f"scale_cuda={tw}:-2:format={fmt}")
        parts.append("hwdownload")
        parts.append(f"format={fmt}")
    else:
        parts.append(f"scale={tw}:-2:flags=lanczos")
    if tonemap:
        parts.append(TONEMAP_GPU if (prof.chosen == "gpu" and prof.libplacebo) else TONEMAP_CPU)
    elif upconvert:
        parts.append(SDR_TO_HLG)
    elif not gpu_frames:
        # GPU frames are already p010le/nv12, which NVENC takes directly; CPU encoders want planar
        parts.append("format=yuv420p10le" if transfer_out == "hlg" else "format=yuv420p")
    if extra_vf:
        parts.append(extra_vf)
    parts.append(f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black")
    parts.append("setsar=1")
    parts.append(f"fps={fps}")
    return ",".join(parts)


def encoder_args(prof: Profile, transfer_out: str, preview: bool = False, cq: int = 19) -> list[str]:
    """Video encoder for a master segment (or a preview)."""
    if preview:
        if prof.chosen == "gpu" and prof.h264_nvenc:
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "28", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p"]
    tags = HLG_TAGS if transfer_out == "hlg" else SDR_TAGS
    if prof.chosen == "gpu" and prof.nvenc:
        args = ["-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", str(cq),
                "-b:v", "0", "-tag:v", "hvc1"]
        if transfer_out == "hlg":
            args += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        else:
            args += ["-profile:v", "main", "-pix_fmt", "yuv420p"]
        return args + tags
    if prof.libx265:
        args = ["-c:v", "libx265", "-preset", prof.cpu_preset, "-crf", "20", "-tag:v", "hvc1",
                "-x265-params", "log-level=none"]
        if transfer_out == "hlg":
            args += ["-profile:v", "main10", "-pix_fmt", "yuv420p10le"]
        else:
            args += ["-pix_fmt", "yuv420p"]
        return args + tags
    # last resort: H.264 SDR only
    return ["-c:v", "libx264", "-preset", prof.cpu_preset, "-crf", "20", "-pix_fmt", "yuv420p"] + SDR_TAGS


def chat_target_kbps(duration_s: float, max_bytes: int = config.CHAT_COPY_MAX_BYTES,
                     audio_kbps: int = AUDIO_KBPS, headroom: float = 0.96, cap_kbps: int = 12000) -> int:
    """Video bitrate that lands the chat copy under the cap with 4% headroom for container overhead."""
    if duration_s <= 0:
        return cap_kbps
    total = (max_bytes * headroom * 8.0) / duration_s / 1000.0
    return int(max(300, min(cap_kbps, total - audio_kbps)))


def chat_encoder_args(prof: Profile, transfer_out: str, target_kbps: int) -> list[str]:
    """Single-pass bitrate-targeted encode; NVENC does its own two-pass with -multipass fullres."""
    tags = HLG_TAGS if transfer_out == "hlg" else SDR_TAGS
    rate = [f"-b:v", f"{target_kbps}k", "-maxrate", f"{target_kbps}k", "-bufsize", f"{2 * target_kbps}k"]
    if prof.chosen == "gpu" and prof.nvenc:
        args = ["-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-multipass", "fullres",
                "-tag:v", "hvc1"] + rate
        args += ["-profile:v", "main10", "-pix_fmt", "p010le"] if transfer_out == "hlg" else ["-profile:v", "main", "-pix_fmt", "yuv420p"]
        return args + tags
    if prof.libx265:
        args = ["-c:v", "libx265", "-preset", prof.cpu_preset, "-tag:v", "hvc1", "-x265-params", "log-level=none"] + rate
        args += ["-profile:v", "main10", "-pix_fmt", "yuv420p10le"] if transfer_out == "hlg" else ["-pix_fmt", "yuv420p"]
        return args + tags
    return ["-c:v", "libx264", "-preset", prof.cpu_preset, "-pix_fmt", "yuv420p"] + rate + SDR_TAGS


def audio_filter(duration: float, fade: float = 0.01) -> str:
    """Raw audio, only resampled to 48 kHz stereo with a 10 ms fade at each end so cuts never click."""
    st = max(0.0, duration - fade)
    return (f"aresample=48000,aformat=channel_layouts=stereo,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={st:.3f}:d={fade}")


# --------------------------------------------------------------------------- clip staging
def clip_cache_dir() -> Path:
    d = config.CACHE / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_clip(src: Path | str) -> Path:
    """Copy a footage clip from Drive into cache/clips/ once, keyed by name+size+mtime, so every
    seek, probe and segment render reads a local file (Drive streaming never stalls a render).
    Returns the cached path. Never touches the source."""
    s = Path(src)
    if safeio.is_under(s, config.CACHE):
        return s
    st = s.stat()
    key = hashlib.sha1(f"{s.name}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:12]
    dst = clip_cache_dir() / f"{key}_{s.name}"
    if dst.is_file() and dst.stat().st_size == st.st_size:
        return dst
    if dst.exists():
        safeio.remove_cache_file(dst)
    safeio.copy_file(s, dst)
    return dst


# --------------------------------------------------------------------------- segments
@dataclass
class SegJob:
    clip: Path
    start: float
    end: float
    transfer_out: str                   # "hlg" | "sdr"
    canvas: tuple[int, int] = (config.CANVAS_W, config.CANVAS_H)
    preview: bool = False
    extra_vf: str = ""                  # e.g. b-roll eq/denoise after the tonemap
    audio: str = "own"                  # own | silent
    fps: int = 30
    info: dict | None = None            # probe() result, filled lazily

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


def segment_key(job: SegJob, prof: Profile) -> str:
    st = Path(job.clip).stat()
    enc = "gpu" if prof.chosen == "gpu" else f"cpu-{prof.cpu_preset}"
    raw = "|".join([str(Path(job.clip).resolve()), str(st.st_size), str(int(st.st_mtime)),
                    f"{job.start:.3f}", f"{job.end:.3f}", job.transfer_out,
                    f"{job.canvas[0]}x{job.canvas[1]}", "preview" if job.preview else "master",
                    job.extra_vf, job.audio, str(job.fps), enc])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def segment_cmd_args(job: SegJob, prof: Profile) -> list[str]:
    """ffmpeg args (without binary and output) for one segment. Pure function: testable."""
    info = job.info or probe(job.clip)
    job.info = info
    args: list[str] = decode_args(prof, info)
    args += ["-ss", f"{job.start:.3f}", "-to", f"{job.end:.3f}", "-i", str(job.clip)]
    if job.audio == "silent" or not info.get("has_audio"):
        args += ["-f", "lavfi", "-t", f"{job.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = ["-map", "1:a:0"]
        af = []
    else:
        amap = ["-map", "0:a:0"]
        af = ["-af", audio_filter(job.duration)]
    args += ["-map", "0:v:0", *amap]
    args += ["-vf", video_chain(prof, info, job.transfer_out, job.canvas, job.preview, job.extra_vf, job.fps)]
    args += encoder_args(prof, job.transfer_out, preview=job.preview)
    args += af + AUDIO_ARGS
    args += ["-video_track_timescale", "90000", "-movflags", "+faststart", "-shortest"]
    return args


def seg_cache_dir() -> Path:
    d = config.CACHE / "segs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _duration_of(path: Path) -> float:
    p = safeio.run_ffprobe(["-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except Exception:
        return -1.0


def _cached_ok(path: Path, expected: float, tol: float = 0.08) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    d = _duration_of(path)
    return d >= 0 and abs(d - expected) <= tol


def render_segment(job: SegJob, prof: Profile, runner=None) -> dict:
    """Render one segment into the cache (or reuse it). Returns {path, cached, seconds, realtime}."""
    runner = runner or safeio.run_ffmpeg
    key = segment_key(job, prof)
    out = seg_cache_dir() / f"{key}.mp4"
    if _cached_ok(out, job.duration):
        return {"path": out, "cached": True, "seconds": 0.0, "realtime": None, "key": key}
    t0 = time.time()
    args = segment_cmd_args(job, prof)
    res = runner(args, out, inputs=[job.clip], overwrite=True)
    dt = time.time() - t0
    if res.returncode != 0:
        raise RuntimeError(f"segment render failed ({job.clip.name} {job.start}-{job.end}): "
                           f"{(res.stderr or '').strip()[-600:]}")
    if not _cached_ok(out, job.duration, tol=0.15):
        raise RuntimeError(f"segment {key} has the wrong duration (expected {job.duration}s)")
    return {"path": out, "cached": False, "seconds": round(dt, 2),
            "realtime": round(job.duration / dt, 2) if dt > 0 else None, "key": key}


def render_segments(jobs: Sequence[SegJob], prof: Profile, workers: int | None = None,
                    runner=None) -> list[dict]:
    """All segments in parallel (profile.workers at once), order preserved, cache honoured."""
    n = workers or prof.workers
    results: list[dict | None] = [None] * len(jobs)
    with cf.ThreadPoolExecutor(max_workers=max(1, n)) as ex:
        futs = {ex.submit(render_segment, j, prof, runner): i for i, j in enumerate(jobs)}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()     # raises the first failure
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------- join
def join_segments(paths: Sequence[Path], out: Path, expected: float | None = None,
                  overwrite: bool = False) -> dict:
    """Concat demuxer: video stream-copied, audio re-encoded once (no AAC priming gaps at cuts)."""
    out = Path(out)
    lst = seg_cache_dir() / f"concat_{hashlib.sha1('|'.join(map(str, paths)).encode()).hexdigest()[:12]}.txt"
    lines = "".join("file '" + str(Path(p).resolve()).replace("'", r"'\''") + "'\n" for p in paths)
    safeio.write_text(lst, lines, overwrite=True)
    args = ["-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", *AUDIO_ARGS, "-movflags", "+faststart"]
    res = safeio.run_ffmpeg(args, out, inputs=list(paths), overwrite=overwrite)
    if res.returncode != 0:
        raise RuntimeError(f"join failed: {(res.stderr or '').strip()[-600:]}")
    got = _duration_of(out)
    exp = expected if expected is not None else sum(_duration_of(p) for p in paths)
    ok = abs(got - exp) <= 0.1
    return {"path": out, "duration": got, "expected": round(exp, 3), "ok": ok}


def make_chat_copy(master: Path, out: Path, prof: Profile, transfer_out: str, duration: float,
                   overwrite: bool = False) -> dict:
    kbps = chat_target_kbps(duration)
    info = probe(master)
    args = decode_args(prof, info) + ["-i", str(master), "-map", "0:v:0", "-map", "0:a:0?"]
    if decode_args(prof, info):
        fmt = "p010le" if transfer_out == "hlg" else "nv12"
        args += ["-vf", f"scale_cuda=format={fmt},hwdownload,format={fmt}"]
    args += chat_encoder_args(prof, transfer_out, kbps) + AUDIO_ARGS + ["-movflags", "+faststart"]
    t0 = time.time()
    res = safeio.run_ffmpeg(args, out, inputs=[master], overwrite=overwrite)
    if res.returncode != 0:
        raise RuntimeError(f"chat copy failed: {(res.stderr or '').strip()[-600:]}")
    size = Path(out).stat().st_size
    return {"path": Path(out), "bytes": size, "target_kbps": kbps,
            "ok": size <= config.CHAT_COPY_MAX_BYTES, "seconds": round(time.time() - t0, 2)}


def decode_errors(path: Path) -> int:
    """Full decode with -v error; the number of error lines (0 = clean)."""
    p = safeio.run_ffmpeg_analysis(["-v", "error", "-i", str(path)])
    return len([ln for ln in (p.stderr or "").splitlines() if ln.strip()])


# --------------------------------------------------------------------------- one decode, many outputs
def multi_output(master: Path, outputs: Sequence[tuple[str, Path]], prof: Profile | None = None,
                 overwrite: bool = True) -> list[Path]:
    """Decode the master once and write N derived files (proxy, sheet, strip ...).

    outputs: [(filter_chain, out_path), ...]; each chain starts from the decoded frames.
    All outputs must be under the engine roots; by default they live in cache/ (overwrite ok).
    """
    info = probe(master)
    n = len(outputs)
    split = f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n))
    chains = [f"[s{i}]{chain}[o{i}]" for i, (chain, _) in enumerate(outputs)]
    fc = ";".join([split] + chains)
    args = ["-i", str(master), "-filter_complex", fc]
    outs = []
    for i, (_, path) in enumerate(outputs):
        safeio.assert_writable(path, overwrite=overwrite)
        safeio.ensure_parent(path)
        if overwrite and Path(path).exists():
            Path(path).unlink()
        args += ["-map", f"[o{i}]", "-an"]
        if str(path).lower().endswith((".jpg", ".jpeg", ".png")):
            args += ["-frames:v", "1", "-update", "1"]
        args += [str(path)]
        outs.append(Path(path))
    cmd = [config.FFMPEG, "-nostdin", "-hide_banner", "-n", "-v", "error", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"multi-output failed: {(p.stderr or '').strip()[-600:]}")
    return outs


def proxy_chain(is_hlg: bool, w: int = PREVIEW_W, h: int = PREVIEW_H) -> str:
    """The analysis proxy every gate and the critic use: 8-bit, same tonemap for winners and candidates."""
    tm = TONEMAP_CPU + "," if is_hlg else ""
    return f"{tm}scale={w}:{h}:flags=bilinear,format=yuv420p"


# --------------------------------------------------------------------------- bench
def _sample_clip(seconds: float = 2.0, hlg: bool = True) -> Path:
    d = config.CACHE / "bench"
    d.mkdir(parents=True, exist_ok=True)
    out = d / ("sample_hlg.mp4" if hlg else "sample_sdr.mp4")
    if out.exists():
        return out
    vf = "format=yuv420p10le" if hlg else "format=yuv420p"
    args = ["-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-vf", vf]
    if hlg:
        args += ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
                 "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1"] + HLG_TAGS
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast"] + SDR_TAGS
    args += ["-c:a", "aac", "-shortest"]
    res = safeio.run_ffmpeg(args, out, overwrite=True)
    if res.returncode != 0:
        raise RuntimeError("could not build the bench sample: " + (res.stderr or "")[-300:])
    return out


def _try(args: list[str], timeout: int = 120) -> tuple[bool, float, str]:
    t0 = time.time()
    try:
        p = subprocess.run([config.FFMPEG, "-nostdin", "-hide_banner", "-v", "error", *args, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode == 0, time.time() - t0, (p.stderr or "")[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        return False, time.time() - t0, str(e)


def bench(max_sessions: int = 8, seconds: float = 2.0) -> Profile:
    """Measure what this machine can do and store the profile. Safe to run anywhere (no GPU = CPU)."""
    prof = detect()
    sample = _sample_clip(seconds, hlg=True)
    fps = {}
    # 1) NVDEC decode of 10-bit HEVC
    if prof.nvdec:
        ok, dt, err = _try(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(sample),
                            "-vf", "scale_cuda=1440:-2:format=p010le,hwdownload,format=p010le"])
        prof.nvdec = ok
        fps["nvdec_scale"] = round(seconds * 30 / dt, 1) if ok else 0
        if not ok:
            prof.notes.append("NVDEC 10-bit decode failed: " + err.strip()[:120])
    # 2) NVENC encode + session count
    if prof.nvenc:
        ok, dt, err = _try(["-i", str(sample), "-vf", "format=p010le", "-c:v", "hevc_nvenc", "-preset", "p4",
                            "-profile:v", "main10", "-pix_fmt", "p010le"])
        prof.nvenc = ok
        fps["nvenc"] = round(seconds * 30 / dt, 1) if ok else 0
        if not ok:
            prof.notes.append("NVENC encode failed: " + err.strip()[:120])
        sessions = 0
        if ok:
            for k in range(1, max_sessions + 1):
                with cf.ThreadPoolExecutor(max_workers=k) as ex:
                    rs = list(ex.map(lambda _: _try(["-i", str(sample), "-vf", "format=p010le", "-c:v", "hevc_nvenc",
                                                     "-preset", "p4", "-profile:v", "main10", "-pix_fmt", "p010le"]),
                                     range(k)))
                if all(r[0] for r in rs):
                    sessions = k
                else:
                    break
        prof.nvenc_sessions = sessions
    # 3) libplacebo tonemap (needs a Vulkan device)
    if prof.libplacebo:
        ok, dt, err = _try(["-init_hw_device", "vulkan", "-i", str(sample), "-vf", TONEMAP_GPU])
        prof.libplacebo = ok
        fps["libplacebo_tonemap"] = round(seconds * 30 / dt, 1) if ok else 0
        if not ok:
            prof.notes.append("libplacebo unavailable: " + err.strip()[:120])
    # 4) CPU paths
    ok, dt, _ = _try(["-i", str(sample), "-vf", TONEMAP_CPU])
    fps["cpu_tonemap"] = round(seconds * 30 / dt, 1) if ok else 0
    if prof.libx265:
        for preset in ("faster", "medium"):
            ok, dt, _ = _try(["-i", str(sample), "-vf", "scale=1440:-2,pad=1440:2560:0:(oh-ih)/2,format=yuv420p10le",
                              "-c:v", "libx265", "-preset", preset, "-x265-params", "log-level=none"])
            fps[f"libx265_{preset}"] = round(seconds * 30 / dt, 1) if ok else 0
        prof.cpu_preset = "medium" if fps.get("libx265_medium", 0) >= 45 else "faster"
    prof.chosen = "gpu" if (prof.nvenc and prof.nvenc_sessions >= 1) else "cpu"
    prof.fps_per_path = fps
    prof.measured_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_profile(prof)
    return prof


# --------------------------------------------------------------------------- cache hygiene
def prune_segment_cache(days: int = SEG_CACHE_DAYS, now: float | None = None) -> int:
    now = now or time.time()
    n = 0
    for d in (seg_cache_dir(), clip_cache_dir()):
      for p in d.glob("*"):
        try:
            if p.is_file() and now - p.stat().st_mtime > days * 86400:
                safeio.remove_cache_file(p)
                n += 1
        except OSError:
            pass
    return n


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fast render engine: profile, bench, segment cache")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench", help="measure NVDEC/NVENC/libplacebo/CPU and write state/render_profile.json")
    b.add_argument("--max-sessions", type=int, default=8)
    b.add_argument("--json", action="store_true")
    s = sub.add_parser("profile", help="show the active render profile")
    s.add_argument("--json", action="store_true")
    pr = sub.add_parser("prune", help="drop segment cache entries older than N days")
    pr.add_argument("--days", type=int, default=SEG_CACHE_DAYS)
    args = ap.parse_args(argv)
    if args.cmd == "bench":
        prof = bench(args.max_sessions)
        print(json.dumps(prof.to_json(), indent=2) if args.json else
              f"render profile: {prof.chosen} nvdec={prof.nvdec} nvenc={prof.nvenc} sessions={prof.nvenc_sessions} "
              f"libplacebo={prof.libplacebo} cpu_preset={prof.cpu_preset} fps={prof.fps_per_path}")
        return 0
    if args.cmd == "profile":
        prof = load_profile()
        print(json.dumps(prof.to_json(), indent=2) if args.json else
              f"render profile: {prof.chosen} workers={prof.workers} notes={prof.notes}")
        return 0
    if args.cmd == "prune":
        print(f"pruned {prune_segment_cache(args.days)} cached segments")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
