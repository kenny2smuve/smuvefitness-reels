"""Deterministic transcript provider for tests (no whisper model needed)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 0.99


class StubTranscript:
    """words_by_clip: {clip_name_or_path: [(start, end, text), ...]} in clip seconds."""

    def __init__(self, words_by_clip: dict):
        self.data = {Path(k).name: [Word(*w) if not isinstance(w, Word) else w for w in v]
                     for k, v in words_by_clip.items()}

    def words(self, clip, start=None, end=None, vad=True):
        ws = self.data.get(Path(clip).name, [])
        lo = -1e9 if start is None else start
        hi = 1e9 if end is None else end
        return [w for w in ws if w.end >= lo and w.start <= hi]
