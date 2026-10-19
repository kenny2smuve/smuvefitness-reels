"""Word-timestamped transcripts with faster-whisper (START_HERE 6A, SPEC "Transcript provider").

What: `FasterWhisperTranscript` is the transcript provider the EDL refiner, the gates and the
critic packet duck-type: `words(clip, start, end, vad) -> list[Word]` with times in clip
seconds. Audio is decoded by ffmpeg to 16 kHz mono float32 PCM and handed to faster-whisper
as a numpy array (so PyAV never has to open his HEVC/HLG files), the model runs on CPU int8,
and every result is cached as JSON under cache/transcripts/ so a window is transcribed once.

Why two passes: the VAD filter drops real speech on quiet POV audio, so `both_passes()` unions
a VAD and a no-VAD pass before anyone decides a line does not exist. The window is decoded
ONCE and both passes run on the same array (a Drive clip is read once, not twice).

Silent b-roll: a header-only ffprobe (`has_audio_stream`) decides whether there is anything to
decode at all; a clip without an audio stream gives an empty array and no model call. The
ffmpeg stderr wording is only a secondary guard.

Why decode_audio() calls subprocess directly instead of tools.safeio: it writes NO file. The
PCM goes to stdout through a pipe (`-f f32le -`), so there is no output path for the write
guard to check, no -y question and nothing on disk. The command is still a list (never a
shell string) and carries -nostdin like every other ffmpeg call in the kit. The transcript
cache itself is written through safeio.write_json.

Run:  py -m tools.transcribe <clip> [--start S --end E] [--no-vad] [--both] [--json|--srt]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import config, safeio

CACHE_SUBDIR = "transcripts"
DEFAULT_MODEL = "small"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"
PIP_HINT = "pip install faster-whisper numpy"
SRT_MAX_CHARS = 42
SRT_GAP_S = config.JUMPCUT_SILENCE_S


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


# --------------------------------------------------------------------------- audio
def has_audio_stream(clip: Path | str) -> bool | None:
    """True/False from a header-only ffprobe (-select_streams a); None when ffprobe itself
    fails (a corrupt file), so the caller lets ffmpeg decide and report the real error."""
    r = safeio.run_ffprobe(["-v", "error", "-select_streams", "a", "-show_entries",
                            "stream=codec_type", "-of", "csv=p=0", str(clip)])
    if r.returncode != 0:
        return None
    return "audio" in (r.stdout or "")


def decode_audio(clip: Path | str, start: float | None = None, end: float | None = None,
                 sr: int = 16000):
    """Decode [start, end] of a clip to mono float32 PCM at `sr` Hz (numpy 1-D array).

    Goes through a pipe, not a file (see the module docstring). A clip without an audio
    stream yields an empty array instead of an error, so silent b-roll transcribes to nothing.
    """
    try:
        import numpy as np
    except ImportError as e:
        raise ImportError(f"numpy is required for transcription: {PIP_HINT}") from e
    clip = Path(clip)
    if not clip.is_file():
        raise FileNotFoundError(clip)
    if has_audio_stream(clip) is False:
        return np.zeros(0, dtype=np.float32)
    cmd = [config.FFMPEG, "-nostdin", "-hide_banner", "-v", "error"]
    if start is not None and float(start) > 0:
        cmd += ["-ss", f"{float(start):.3f}"]
    cmd += ["-i", str(clip)]
    if end is not None:
        s0 = float(start) if start is not None and float(start) > 0 else 0.0
        length = float(end) - s0
        if length <= 0:
            return np.zeros(0, dtype=np.float32)
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-vn", "-sn", "-dn", "-ac", "1", "-ar", str(int(sr)), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=1800)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace")
        if "does not contain any stream" in err or "Output file is empty" in err:
            return np.zeros(0, dtype=np.float32)
        raise RuntimeError(f"ffmpeg audio decode failed for {clip.name}: {err.strip()[-300:]}")
    buf = r.stdout or b""
    buf = buf[: len(buf) - (len(buf) % 4)]
    return np.frombuffer(buf, dtype=np.float32).copy()


# --------------------------------------------------------------------------- provider
class FasterWhisperTranscript:
    """Transcript provider backed by faster-whisper (lazy-loaded) with a JSON cache.

    Inject `model` (anything with `.transcribe(audio, **kw) -> (segments, info)` whose
    segments expose `.words` with `.start/.end/.word/.probability`) to avoid loading the real
    model, as the tests do.
    """

    def __init__(self, model_size: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE,
                 compute_type: str = DEFAULT_COMPUTE, model=None, language: str | None = None,
                 cache_dir: Path | None = None):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = model
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

    # ---- model
    def model(self):
        """The injected model, or faster_whisper.WhisperModel loaded on first use."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise ImportError(f"faster-whisper is not installed: {PIP_HINT}") from e
            self._model = WhisperModel(self.model_size, device=self.device,
                                       compute_type=self.compute_type)
        return self._model

    # ---- cache
    def cache_dir(self) -> Path:
        return self._cache_dir if self._cache_dir is not None else config.CACHE / CACHE_SUBDIR

    def cache_key(self, clip: Path, start: float | None, end: float | None, vad: bool) -> str:
        st = clip.stat()
        parts = [str(clip.resolve(strict=False)), str(st.st_size), str(st.st_mtime_ns),
                "" if start is None else f"{float(start):.3f}",
                "" if end is None else f"{float(end):.3f}",
                "vad" if vad else "novad", self.model_size, self.language or "auto"]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def cache_path(self, clip: Path, start: float | None, end: float | None, vad: bool) -> Path:
        return self.cache_dir() / f"{self.cache_key(clip, start, end, vad)}.json"

    @staticmethod
    def _from_cache(p: Path) -> list[Word] | None:
        """Words from a cache file, or None when it is missing, unreadable, not JSON or not
        in our shape (a corrupt cache must be a miss, never a dead end)."""
        try:
            d = safeio.read_json(p, default=None)
        except (ValueError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(d, dict) or "words" not in d:
            return None
        out: list[Word] = []
        for w in d["words"]:
            try:
                out.append(Word(float(w["start"]), float(w["end"]), str(w["text"]),
                                float(w.get("prob", 1.0))))
            except (KeyError, TypeError, ValueError):
                return None
        return out

    # ---- transcription
    def words(self, clip: Path | str, start: float | None = None, end: float | None = None,
              vad: bool = True) -> list[Word]:
        """Words overlapping [start, end] of the clip, times in clip seconds, cached."""
        clip = Path(clip)
        if not clip.is_file():
            raise FileNotFoundError(clip)
        cp = self.cache_path(clip, start, end, vad)
        cached = self._from_cache(cp) if cp.exists() else None
        if cached is not None:
            return cached
        audio = decode_audio(clip, start, end)
        words = self._words_from_audio(audio, _offset(start), vad)
        self._save_cache(cp, clip, start, end, vad, words)
        return words

    def _words_from_audio(self, audio, offset: float, vad: bool) -> list[Word]:
        """Run the model on an already decoded window; times shifted by `offset`."""
        words: list[Word] = []
        if getattr(audio, "size", 0) > 0:
            kw = {"word_timestamps": True, "vad_filter": bool(vad),
                  "condition_on_previous_text": False}
            if self.language:
                kw["language"] = self.language
            segments, _info = self.model().transcribe(audio, **kw)
            for seg in segments:
                for w in getattr(seg, "words", None) or []:
                    text = str(getattr(w, "word", "")).strip()
                    if not text:
                        continue
                    words.append(Word(round(offset + float(w.start), 3),
                                      round(offset + float(w.end), 3), text,
                                      round(float(getattr(w, "probability", 1.0)), 4)))
        words.sort(key=lambda x: (x.start, x.end))
        return words

    def _save_cache(self, cp: Path, clip: Path, start, end, vad: bool, words: list[Word]) -> None:
        payload = {"clip": str(clip), "start": start, "end": end, "vad": bool(vad),
                   "model_size": self.model_size, "language": self.language or "auto",
                   "words": [asdict(w) for w in words]}
        try:
            safeio.write_json(cp, payload, overwrite=True)
        except safeio.SafeIOError:
            # a cache dir outside the engine root (tests, odd configs) is simply not cached
            pass

    def both_passes(self, clip: Path | str, start: float | None = None,
                    end: float | None = None) -> list[Word]:
        """Union of the VAD and the no-VAD pass, deduped by (round(start, 2), text).
        The window is decoded once; each pass still gets its own cache file."""
        clip = Path(clip)
        if not clip.is_file():
            raise FileNotFoundError(clip)
        results: dict[bool, list[Word]] = {}
        missing: list[bool] = []
        for vad in (True, False):
            cp = self.cache_path(clip, start, end, vad)
            cached = self._from_cache(cp) if cp.exists() else None
            if cached is not None:
                results[vad] = cached
            else:
                missing.append(vad)
        if missing:
            audio = decode_audio(clip, start, end)
            for vad in missing:
                words = self._words_from_audio(audio, _offset(start), vad)
                self._save_cache(self.cache_path(clip, start, end, vad), clip, start, end, vad, words)
                results[vad] = words
        return merge_words(results[True], results[False])


# edl.py looks for one of these names and instantiates it with no arguments.
TranscriptProvider = FasterWhisperTranscript


# --------------------------------------------------------------------------- pure helpers
def _offset(start: float | None) -> float:
    return float(start) if start is not None and float(start) > 0 else 0.0


def merge_words(*passes: Iterable[Word]) -> list[Word]:
    """Union of word lists deduped by (round(start, 2), normalised text), sorted by start."""
    seen: dict[tuple[float, str], Word] = {}
    for ws in passes:
        for w in ws:
            key = (round(float(w.start), 2), w.text.strip().lower())
            if key not in seen or w.prob > seen[key].prob:
                seen[key] = w
    return sorted(seen.values(), key=lambda w: (w.start, w.end))


def _srt_time(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def lines_of(words: Iterable[Word], gap: float = SRT_GAP_S, max_chars: int = SRT_MAX_CHARS) -> list[list[Word]]:
    """Group words into caption lines at silences > gap or when a line gets long."""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            text_len = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            if (w.start - cur[-1].end) > gap or text_len > max_chars:
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def to_srt(words: Iterable[Word]) -> str:
    """SubRip text of the words, one cue per line group."""
    out: list[str] = []
    for i, line in enumerate(lines_of(list(words)), start=1):
        text = " ".join(w.text for w in line)
        out.append(f"{i}\n{_srt_time(line[0].start)} --> {_srt_time(line[-1].end)}\n{text}\n")
    return "\n".join(out)


def to_text(words: Iterable[Word]) -> str:
    """Plain "[start-end] words" lines (what the critic packet shows)."""
    return "\n".join(f"[{l[0].start:.2f}-{l[-1].end:.2f}] " + " ".join(w.text for w in l)
                     for l in lines_of(list(words)))


# --------------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Word-timestamped transcript of a clip (faster-whisper, cached)")
    ap.add_argument("clip")
    ap.add_argument("--start", type=float, default=None, help="window start in clip seconds")
    ap.add_argument("--end", type=float, default=None, help="window end in clip seconds")
    ap.add_argument("--no-vad", action="store_true", help="run without the VAD filter")
    ap.add_argument("--both", action="store_true", help="union of the VAD and no-VAD passes")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper model size (default small)")
    ap.add_argument("--language", default=None, help="force a language (default: auto-detect)")
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--compute-type", default=DEFAULT_COMPUTE)
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the words as JSON")
    fmt.add_argument("--srt", action="store_true", help="print SubRip cues")
    a = ap.parse_args(argv)
    try:
        prov = FasterWhisperTranscript(model_size=a.model, device=a.device, compute_type=a.compute_type,
                                       language=a.language)
        if a.both:
            words = prov.both_passes(a.clip, a.start, a.end)
        else:
            words = prov.words(a.clip, a.start, a.end, vad=not a.no_vad)
    except (ImportError, FileNotFoundError, RuntimeError, OSError) as e:
        if a.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"transcribe: {e}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"clip": str(a.clip), "start": a.start, "end": a.end,
                          "vad": "both" if a.both else (not a.no_vad),
                          "words": [asdict(w) for w in words]}, indent=2, ensure_ascii=False))
    elif a.srt:
        print(to_srt(words))
    else:
        print(to_text(words) if words else "(no words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
