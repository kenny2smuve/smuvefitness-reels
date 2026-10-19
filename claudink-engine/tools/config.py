"""Single source of truth for paths, time zone and constants of the ClaudInk Reel Engine.

Every tool imports from here. Nothing else hard-codes a path, an offset or a caption tail.

All paths can be overridden with environment variables so the same code runs on Claudin's
Windows PC (defaults below) and in a Linux test container (tests set the variables).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- time
TZ = ZoneInfo("America/New_York")          # everything the engine writes is in ET
TZ_NAME = "America/New_York"
UTC = ZoneInfo("UTC")


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# --------------------------------------------------------------------------- roots
ENGINE_ROOT = _env_path("CLAUDINK_ENGINE_ROOT", r"D:\ClaudeInk\reel-engine")
DRIVE_OUT = _env_path("CLAUDINK_DRIVE_OUT", r"F:\My Drive\ClaudeInk Reels")   # his Drive mirror (writable)
FOOTAGE_ROOT = _env_path("CLAUDINK_FOOTAGE_ROOT", r"F:\My Drive")               # read-only footage
LEGACY_OUT = _env_path("CLAUDINK_LEGACY_OUT", r"D:\ClaudeInk\out")              # old renders, read-only
METRICS_CSV = _env_path("CLAUDINK_METRICS_CSV",
                        r"C:\Users\Firelake330\Desktop\igserver\reels_history.csv")  # read-only original

# The only two trees any tool may write into (safeio enforces this).
WRITE_ROOTS: tuple[Path, ...] = (ENGINE_ROOT, DRIVE_OUT)

# Engine sub-folders
KNOWLEDGE = ENGINE_ROOT / "knowledge"
LEDGERS = ENGINE_ROOT / "ledgers"
DATA = ENGINE_ROOT / "data"
CACHE = ENGINE_ROOT / "cache"
POST = ENGINE_ROOT / "post"
HOLD = ENGINE_ROOT / "hold"
RUNS = ENGINE_ROOT / "runs"
STATE = ENGINE_ROOT / "state"
REGISTER = ENGINE_ROOT / "register"
TEMPLATES = ENGINE_ROOT / "templates"
TO_DELETE = ENGINE_ROOT / "_to_delete"
DRIVE_POST = DRIVE_OUT / "POST"

# Footage folders he named. New folders that contain VID_/IMG_/MVI_ files are also indexed.
KNOWN_FOOTAGE_FOLDERS = [
    "Shop shorts", "Session 1 of 4", "Session 2 of 4", "Session 3", "Sesh 4",
    "Talking head 10 8", "Rework flowers",
]
FOOTAGE_FOLDER_PREFIXES = ["Akira", "Berserk", "Atlas"]
VIDEO_EXTS = {".mov", ".mp4", ".MOV", ".MP4"}

# --------------------------------------------------------------------------- binaries
def _find_bin(env: str, win_default: str, name: str) -> str:
    cand = os.environ.get(env) or win_default
    if Path(cand).exists():
        return cand
    found = shutil.which(name)
    return found or cand


FFMPEG = _find_bin("CLAUDINK_FFMPEG", r"D:\ClaudeInk\ffmpeg71\bin\ffmpeg.exe", "ffmpeg")
FFPROBE = _find_bin("CLAUDINK_FFPROBE", r"D:\ClaudeInk\ffmpeg71\bin\ffprobe.exe", "ffprobe")
PYTHON = sys.executable

# --------------------------------------------------------------------------- posting rules (RULES.md §2 mirrors)
MAX_POSTS_PER_DAY = 2
MIN_GAP_HOURS = 3.0
LATEST_POST_HOUR = 21            # nothing after 9pm ET
SLOT_A_WINDOW = ("11:00", "11:30")
SLOT_B_WINDOW = ("14:30", "16:00")
X1_MORNING_WINDOW = ("06:30", "08:30")
MIN_LEAD_MINUTES = 45            # a slot must be at least this far in the future
FALLBACK_NEXT_DAY = ("11:00", "14:30")
HOLD_MIN = 4

CAPTION_TAIL = ("For all tattoo inquiries text (786) 766-3774 Books fill up fast! "
                "#tattoo #miamitattooartist #art #miami #artist")
BOOKING_PHONE = "(786) 766-3774"

# --------------------------------------------------------------------------- gates (§6D)
HIT_VIEWS = 5000                 # North Star: 5K+ views within 72h
HIT_WINDOW_H = 72
CHAT_COPY_MAX_BYTES = 25 * 1024 * 1024
CANVAS_W, CANVAS_H = 1440, 2560
STORY_SINGLE_RANGE = (36.0, 48.0)
STORY_MULTI_MAX = 75.0
BROLL_MIN = 10.0
BROLL_TEMPLATE_LEN = 13.6
BROLL_MAX_SHOT = 4.6
BROLL_TOL = 0.2
TH_RANGE = (30.0, 42.0)
FRAME1_LUMA_STD_MIN = 60.0
FRAME1_MOTION_MIN = 4.0          # mean |frame diff| over 0-1s on an 8-bit 360p proxy; recalibrate on his winners
OPEN_WITHIN_S = 0.3
STAKES_BY_S = 3.0
RETENTION_WINDOW = (3.0, 10.0)
MAX_DEAD_STRETCH_S = 2.0
BLACKDETECT = "blackdetect=d=0.01:pix_th=0.10"
FREEZE_MAX_S = 4.0
MIN_NEEDLE_SHOTS = 4
FINISHED_HOLD_MIN_S = 3.0
HOOK_MAX_CHARS = 55
HOOK_FUZZY_MATCH = 0.85
PHASH_MATCH_MAX = 12
PHASH_FRAMES = 9
PHASH_SCALES = (1.0, 1.15, 1.33)
TEXT_BAND_TOP = 0.18             # fraction of height masked at the top (his hook text)
TEXT_BAND_BOTTOM = 0.22          # bottom band (captions / UI)
SHOT_REUSE_MAX_FRACTION = 0.40   # of story shots seen in the last 14 days of posts
SHOT_REUSE_WINDOW_DAYS = 14
MAX_REELS_PER_PIECE_PER_DAY = 2
VOICE_BAND_HZ = (300, 3400)
OUT_PAD_S = 0.12
OUT_EXTEND_MAX_S = 0.8
OUT_SENTENCE_MAX_S = 1.2
IN_ONSET_MAX_S = 0.4
JUMPCUT_SILENCE_S = 0.6
INDEX_BYTES_PER_DAY = 20 * 1024 ** 3
PROBE_BYTES_CAP = 32 * 1024 ** 2   # a header probe counts min(size, this) against the day (FOLLOWUP_CHANGES 4)
PANEL_DOWNLOADS_BURNIN = 20
PANEL_DOWNLOADS_DAILY = 6
PANEL_REQUEST_GAP_S = (3, 5)
CRITIC_PAIR_RATE_MIN = 0.80

# Reels on his page used to calibrate the critic (RULES.md §7)
PANEL_WINNERS = ["DebCcJvqDI4", "DeE6fyBKWpz", "DehJPlnKk8I", "DdCXyjKqI4I",
                 "DdIo7YkgJ9d", "Ddf6nmHAAEW", "DcaS1GggTH3"]
PANEL_MEDIANS = ["DecSt5ZKl46", "Deb9ku4ROcV", "DeUn6DRqdCs", "DehQeZMKCOL"]
PANEL_FLOPS = ["DeH4xUmKrxz", "DePYEZMKGLX", "DeP08KMK3he", "DeOL0b1KIfu"]

# Held by the Metricool cap, never deliver to IG again as-is
NEVER_REUPLOAD = ["Copy_DehRelXKsds"]


def ensure_engine_dirs() -> None:
    """Create the engine's own folders (never anything outside WRITE_ROOTS)."""
    for d in (KNOWLEDGE, LEDGERS, DATA, DATA / "analysis", CACHE, POST, HOLD, HOLD / "next",
              RUNS, STATE, REGISTER, TEMPLATES, ENGINE_ROOT / "tools", ENGINE_ROOT / "weekly"):
        d.mkdir(parents=True, exist_ok=True)


def describe() -> dict:
    return {
        "ENGINE_ROOT": str(ENGINE_ROOT), "DRIVE_OUT": str(DRIVE_OUT),
        "FOOTAGE_ROOT": str(FOOTAGE_ROOT), "METRICS_CSV": str(METRICS_CSV),
        "FFMPEG": FFMPEG, "FFPROBE": FFPROBE, "TZ": TZ_NAME, "PYTHON": PYTHON,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(describe(), indent=2))
