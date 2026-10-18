"""Tests for tools/transcribe.py: ffmpeg audio decode, cached provider with a fake model,
both-pass merge, SRT. The real faster-whisper model is never loaded."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class FakeModel:
    """Looks like faster_whisper.WhisperModel.transcribe: returns (segments, info)."""

    def __init__(self, words):
        self.words = words          # [(start, end, text, prob)] in window seconds
        self.calls: list[dict] = []

    def transcribe(self, audio, **kw):
        self.calls.append({"n": int(getattr(audio, "size", 0)), **kw})
        ws = [SimpleNamespace(start=s, end=e, word=t, probability=p) for s, e, t, p in self.words]
        seg = SimpleNamespace(start=ws[0].start if ws else 0.0, end=ws[-1].end if ws else 0.0,
                              text=" ".join(w.word for w in ws), words=ws)
        return iter([seg]), SimpleNamespace(language="en", language_probability=0.9)


@pytest.fixture()
def speech_clip(engine_root, clip_factory):
    return clip_factory("speech.mp4", seconds=3.0, w=160, h=90, audio="speech")


def test_decode_audio_shape_and_rate(speech_clip):
    import numpy as np
    from tools import transcribe as tr
    a = tr.decode_audio(speech_clip)
    assert a.dtype == np.float32 and a.ndim == 1
    assert abs(a.size - 3.0 * 16000) < 1600          # 3 s at 16 kHz, ~ +-0.1 s
    assert float(np.abs(a).max()) > 0.1              # the tone bursts are there
    w = tr.decode_audio(speech_clip, start=1.0, end=2.0)
    assert abs(w.size - 16000) < 1600
    w8 = tr.decode_audio(speech_clip, start=0.0, end=0.5, sr=8000)
    assert abs(w8.size - 4000) < 800
    assert tr.decode_audio(speech_clip, start=2.0, end=1.0).size == 0


def test_decode_audio_silent_clip_is_empty(engine_root, clip_factory):
    from tools import transcribe as tr
    mute = clip_factory("mute.mp4", seconds=1.0, w=160, h=90, audio="none")
    assert tr.decode_audio(mute).size == 0
    with pytest.raises(FileNotFoundError):
        tr.decode_audio(mute.with_name("missing.mp4"))


def test_words_offset_by_start_and_cached(speech_clip, monkeypatch):
    from tools import config, transcribe as tr
    fake = FakeModel([(0.10, 0.40, " Would", 0.95), (0.45, 0.80, "you", 0.9), (0.85, 1.20, "sit?", 0.8)])
    prov = tr.FasterWhisperTranscript(model=fake)
    words = prov.words(speech_clip, start=1.0, end=2.5, vad=True)
    assert [w.text for w in words] == ["Would", "you", "sit?"]
    assert [w.start for w in words] == [1.10, 1.45, 1.85]
    assert words[-1].end == pytest.approx(2.20)
    assert words[0].prob == pytest.approx(0.95)
    assert isinstance(words[0], tr.Word)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["word_timestamps"] is True and call["vad_filter"] is True
    assert call["condition_on_previous_text"] is False and "language" not in call
    assert 1.5 * 16000 * 0.9 < call["n"] < 1.5 * 16000 * 1.1
    cache = prov.cache_path(speech_clip, 1.0, 2.5, True)
    assert cache.is_file() and cache.parent == config.CACHE / "transcripts"
    d = json.loads(cache.read_text(encoding="utf-8"))
    assert d["vad"] is True and d["model_size"] == "small"
    assert d["words"][0] == {"start": 1.1, "end": 1.4, "text": "Would", "prob": 0.95}
    # cache hit: the model is not called again, by this instance or a fresh one with no model
    assert prov.words(speech_clip, start=1.0, end=2.5, vad=True) == words
    assert len(fake.calls) == 1
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # any model load would raise
    fresh = tr.FasterWhisperTranscript()
    assert fresh.words(speech_clip, start=1.0, end=2.5, vad=True) == words
    # a different window / vad / model size is a different key
    assert prov.cache_path(speech_clip, 1.0, 2.5, False) != cache
    assert prov.cache_path(speech_clip, None, None, True) != cache
    assert tr.FasterWhisperTranscript(model_size="medium", model=fake).cache_path(speech_clip, 1.0, 2.5, True) != cache


def test_no_vad_and_language_passed_through(speech_clip):
    from tools import transcribe as tr
    fake = FakeModel([(0.0, 0.3, "hi", 0.5)])
    prov = tr.FasterWhisperTranscript(model=fake, language="en")
    words = prov.words(speech_clip, vad=False)
    assert words == [tr.Word(0.0, 0.3, "hi", 0.5)]
    assert fake.calls[0]["vad_filter"] is False and fake.calls[0]["language"] == "en"


def test_both_passes_dedupes(speech_clip):
    from tools import transcribe as tr

    class TwoPass(FakeModel):
        def transcribe(self, audio, **kw):
            self.words = ([(0.10, 0.40, "would", 0.9), (0.45, 0.80, "you", 0.9)]
                          if kw.get("vad_filter") else
                          [(0.104, 0.40, "Would", 0.7), (0.45, 0.80, "you", 0.95), (0.85, 1.2, "sit", 0.8)])
            return super().transcribe(audio, **kw)

    fake = TwoPass([])
    prov = tr.FasterWhisperTranscript(model=fake)
    words = prov.both_passes(speech_clip, 0.0, 1.5)
    assert [w.text.lower() for w in words] == ["would", "you", "sit"]
    assert len(fake.calls) == 2
    assert {c["vad_filter"] for c in fake.calls} == {True, False}
    # the higher-probability duplicate wins
    assert next(w for w in words if w.text.lower() == "you").prob == pytest.approx(0.95)


def test_silent_clip_gives_no_words_and_never_calls_model(engine_root, clip_factory):
    from tools import transcribe as tr
    mute = clip_factory("mute2.mp4", seconds=1.0, w=160, h=90, audio="none")
    fake = FakeModel([(0.0, 0.1, "ghost", 0.9)])
    assert tr.FasterWhisperTranscript(model=fake).words(mute) == []
    assert fake.calls == []


def test_model_lazy_import_error_names_pip(speech_clip, monkeypatch):
    from tools import transcribe as tr
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # import raises ImportError
    prov = tr.FasterWhisperTranscript(cache_dir=speech_clip.parent / "nocache")
    with pytest.raises(ImportError, match="pip install faster-whisper"):
        prov.words(speech_clip)


def test_merge_words_and_srt():
    from tools import transcribe as tr
    a = [tr.Word(0.0, 0.3, "Would", 0.9), tr.Word(0.4, 0.6, "you", 0.9)]
    b = [tr.Word(0.004, 0.3, "would", 0.5), tr.Word(2.0, 2.4, "Seven", 0.9), tr.Word(2.5, 2.9, "hours", 0.9)]
    m = tr.merge_words(a, b)
    assert [w.text for w in m] == ["Would", "you", "Seven", "hours"]
    srt = tr.to_srt(m)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:00,600\nWould you\n")
    assert "2\n00:00:02,000 --> 00:00:02,900\nSeven hours\n" in srt
    assert tr.to_srt([]) == ""
    assert tr.to_text(m).splitlines()[0] == "[0.00-0.60] Would you"


def test_provider_alias_for_edl():
    from tools import transcribe as tr
    assert tr.TranscriptProvider is tr.FasterWhisperTranscript


def test_cli_json_and_srt_from_cache(speech_clip, capsys):
    from tools import transcribe as tr
    fake = FakeModel([(0.0, 0.5, "Hello", 0.9), (0.6, 1.0, "there", 0.9)])
    tr.FasterWhisperTranscript(model=fake).words(speech_clip, vad=True)   # warm the cache
    assert tr.main([str(speech_clip), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [w["text"] for w in out["words"]] == ["Hello", "there"]
    assert tr.main([str(speech_clip), "--srt"]) == 0
    assert "Hello there" in capsys.readouterr().out
    assert tr.main([str(speech_clip)]) == 0
    assert "[0.00-1.00] Hello there" in capsys.readouterr().out
    assert tr.main([str(speech_clip.with_name("nope.mp4")), "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
