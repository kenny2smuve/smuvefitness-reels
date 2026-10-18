"""Pytest setup: point the engine at a temp root and provide synthetic fixture clips.

Nothing here touches the real D:/F:/C: paths. Every test gets an isolated engine root.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parents[2]
if str(KIT_ROOT) not in sys.path:
    sys.path.insert(0, str(KIT_ROOT))

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _reload_config():
    import tools.config as c
    importlib.reload(c)
    import tools.safeio as s
    importlib.reload(s)
    return c


@pytest.fixture()
def engine_root(tmp_path, monkeypatch):
    """A fresh engine root + drive-out + footage root under tmp_path; config reloaded."""
    root = tmp_path / "reel-engine"
    drive = tmp_path / "My Drive" / "ClaudeInk Reels"
    footage = tmp_path / "My Drive"
    metrics = tmp_path / "igserver" / "reels_history.csv"
    for d in (root, drive, footage, metrics.parent):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDINK_ENGINE_ROOT", str(root))
    monkeypatch.setenv("CLAUDINK_DRIVE_OUT", str(drive))
    monkeypatch.setenv("CLAUDINK_FOOTAGE_ROOT", str(footage))
    monkeypatch.setenv("CLAUDINK_METRICS_CSV", str(metrics))
    monkeypatch.setenv("CLAUDINK_LEGACY_OUT", str(tmp_path / "out"))
    c = _reload_config()
    c.ensure_engine_dirs()
    yield root
    _reload_config()


def make_clip(path: Path, *, seconds: float = 4.0, w: int = 1920, h: int = 1080, fps: int = 30,
              pattern: str = "testsrc2", audio: str = "sine", hdr: bool = False,
              extra_vf: str = "", rotate: int | None = None, creation_time: str | None = None,
              quicktime_creationdate: str | None = None, codec: str = "libx264") -> Path:
    """Render a synthetic clip with ffmpeg lavfi sources.

    pattern: testsrc2 (moving), smptebars (static), black, or any lavfi video source expression.
    audio: sine (tone), speech (tone bursts in the 300-3400 Hz band), silence, none.
    hdr: tag + encode as HLG 10-bit HEVC (arib-std-b67 / bt2020) like Meta glasses footage.
    rotate: write a rotation display matrix like Canon portrait clips.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if pattern in ("testsrc2", "testsrc", "smptebars", "rgbtestsrc"):
        vsrc = f"{pattern}=size={w}x{h}:rate={fps}:duration={seconds}"
    elif pattern == "black":
        vsrc = f"color=c=black:size={w}x{h}:rate={fps}:duration={seconds}"
    elif pattern == "gray":
        vsrc = f"color=c=gray:size={w}x{h}:rate={fps}:duration={seconds}"
    else:
        vsrc = pattern
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-f", "lavfi", "-i", vsrc]
    if audio == "sine":
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    elif audio == "speech":
        # 1 kHz tone gated on/off every 0.5 s: energy sits inside the voice band
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=1000:sample_rate=48000:duration={seconds},"
                f"volume='if(lt(mod(t,1),0.5),1,0)':eval=frame"]
    elif audio == "silence":
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(seconds)]
    vf = extra_vf
    if hdr:
        vf = (vf + "," if vf else "") + "format=yuv420p10le"
        cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "28",
                "-x265-params", "log-level=none:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
                "-tag:v", "hvc1"]
    else:
        vf = (vf + "," if vf else "") + "format=yuv420p"
        cmd += ["-c:v", codec, "-preset", "ultrafast", "-crf", "28",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    cmd += ["-vf", vf]
    if audio != "none":
        cmd += ["-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-an"]
    if creation_time:
        cmd += ["-metadata", f"creation_time={creation_time}"]
    if quicktime_creationdate:
        cmd += ["-metadata", f"com.apple.quicktime.creationdate={quicktime_creationdate}",
                "-movflags", "use_metadata_tags"]
    target = path if rotate is None else path.with_name(path.stem + ".norot" + path.suffix)
    cmd += ["-shortest", "-r", str(fps), str(target)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if rotate is not None:
        # Canon portrait clips carry a display-matrix rotation; ffmpeg autorotates on decode.
        subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y",
                        "-display_rotation", str(rotate), "-i", str(target), "-c", "copy",
                        "-map_metadata", "0", "-movflags", "use_metadata_tags", str(path)],
                       check=True, capture_output=True, text=True)
        target.unlink()
    return path


@pytest.fixture()
def clip_factory(tmp_path):
    def _make(name="clip.mp4", **kw):
        return make_clip(tmp_path / "fixtures" / name, **kw)
    return _make
