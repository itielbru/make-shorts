"""Transcript model shared by every transcription engine.

Words carry start/end seconds. The cache key lets a finished transcript be
reused ("transcribe once, cut many times").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass
class Transcript:
    words: list[Word]
    language: str
    engine: str
    gaps: list[tuple[float, float]] = field(default_factory=list)
    approximate_timing: bool = False


def save_transcript(t: Transcript, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "language": t.language,
        "engine": t.engine,
        "approximate_timing": t.approximate_timing,
        "gaps": [list(g) for g in t.gaps],
        "words": [{"text": w.text, "start": w.start, "end": w.end} for w in t.words],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_transcript(path: Path) -> Transcript:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Transcript(
        words=[Word(w["text"], float(w["start"]), float(w["end"])) for w in data["words"]],
        language=data["language"],
        engine=data["engine"],
        gaps=[(float(a), float(b)) for a, b in data.get("gaps", [])],
        approximate_timing=bool(data.get("approximate_timing", False)),
    )


def cache_key(media_sha256: str, engine: str, language: str) -> str:
    raw = "\x1f".join((media_sha256, engine, language)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def drop_words_in_trailing_silence(
    words: list[Word], silences: list[tuple[float, float]], audio_end: float
) -> list[Word]:
    """Drop words Whisper invents inside a silence that runs to the end of the audio."""
    trailing = [(s, e) for s, e in silences if e >= audio_end - 0.05]
    if not trailing:
        return list(words)
    return [
        w for w in words
        if not any(w.start >= s - 1e-6 and w.end <= e + 1e-6 for s, e in trailing)
    ]


def interpolate_words(segment_text: str, start: float, end: float) -> list[Word]:
    """Spread a segment's words evenly across its time span (degraded, approximate timing)."""
    tokens = segment_text.split()
    if not tokens:
        return []
    step = (end - start) / len(tokens)
    words = []
    for i, token in enumerate(tokens):
        w_start = start + i * step
        w_end = end if i == len(tokens) - 1 else start + (i + 1) * step
        words.append(Word(token, round(w_start, 6), round(w_end, 6)))
    return words
