"""Transcript model shared by every transcription engine.

Words carry start/end seconds. The cache key lets a finished transcript be
reused ("transcribe once, cut many times").
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from messages import UserFacingError


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


def apply_edits(words: list[Word], edits: list[dict]) -> list[Word]:
    """Fix misheard words without moving any index.

    `text` may hold several words (one heard word that is really two) or be empty to
    drop a hallucinated word from the captions. Timing stays that of the original word.
    """
    fixed = list(words)
    try:
        pairs = [(int(e["index"]), str(e["text"]).strip()) for e in edits]
    except (KeyError, TypeError, ValueError):
        raise UserFacingError("bad_edits_file") from None
    for index, text in pairs:
        if not 0 <= index < len(fixed):
            raise UserFacingError("bad_edits_file")
        fixed[index] = Word(text, fixed[index].start, fixed[index].end)
    return fixed


_PREFIXES = "בהלמוכש"
_EDGE_PUNCT = ".,!?;:\"'()[]…-–"


def apply_glossary(words: list[Word], mapping: dict[str, str]) -> list[Word]:
    """Replace misheard names and terms across the whole transcript.

    Keeps edge punctuation and timing, and also matches the word with one Hebrew prefix letter
    (ב, ה, ל, מ, ו, כ, ש) in front, for keys of three letters or more.
    """
    phrases = {tuple(k.split()): v for k, v in mapping.items() if len(k.split()) > 1}
    if phrases:
        words = _apply_phrases(words, phrases)
    fixed: list[Word] = []
    for w in words:
        text = w.text
        core = text.strip(_EDGE_PUNCT)
        if not core:
            fixed.append(w)
            continue
        head = text[: text.index(core)]
        tail = text[text.index(core) + len(core):]
        replacement = None
        if core in mapping:
            replacement = mapping[core]
        elif len(core) > 3 and core[0] in _PREFIXES and len(core) - 1 >= 3 and core[1:] in mapping:
            replacement = core[0] + mapping[core[1:]]
        fixed.append(Word(head + replacement + tail, w.start, w.end) if replacement is not None else w)
    return fixed


def _apply_phrases(words: list[Word], phrases: dict[tuple[str, ...], str]) -> list[Word]:
    """Several-word keys ("מילטה דה אבידה") matched on consecutive words, punctuation aside. The
    replacement words take the matched slots in order; extra words join the last slot, and unused
    slots are emptied (an empty word is not shown), so no index or timing moves."""
    out = list(words)
    cores = [w.text.strip(_EDGE_PUNCT) for w in words]
    i = 0
    while i < len(out):
        hit = next((k for k in sorted(phrases, key=len, reverse=True) if tuple(cores[i:i + len(k)]) == k), None)
        if hit is None:
            i += 1
            continue
        k = len(hit)
        first, last = out[i].text, out[i + k - 1].text
        head = first[: first.index(cores[i])] if cores[i] else ""
        tail = last[last.index(cores[i + k - 1]) + len(cores[i + k - 1]):] if cores[i + k - 1] else ""
        new = phrases[hit].split()
        slots = new[: k - 1] + [" ".join(new[k - 1:])] if len(new) >= k else new + [""] * (k - len(new))
        slots[0] = head + slots[0]
        filled = [j for j, s in enumerate(slots) if s]
        if filled:
            slots[filled[-1]] += tail
        for j in range(k):
            out[i + j] = Word(slots[j], out[i + j].start, out[i + j].end)
        i += k
    return out


def glossary_warnings(mapping: dict[str, str]) -> list[str]:
    """Entries that will not do what was meant. A value that starts with a prefix letter its key
    lacks doubles the letter on a prefixed word (seen: "טרוטנו": "בתורתנו" made "בבתורתנו")."""
    out = []
    for key, value in mapping.items():
        if len(key.split()) > 1:
            continue
        if value[:1] in _PREFIXES and key[:1] != value[:1] and len(value) > 3:
            out.append(f"במילון, \"{key}\" ← \"{value}\": הערך מתחיל באות קידומת ({value[0]}) שאין במפתח. "
                       "אם זו קידומת, כתוב את המפתח ואת הערך בלי הקידומת, כי הקוד מוסיף אותה לבד.")
    return out


_ALLOWED_SCRIPTS = {"he": ("HEBREW",), "ar": ("ARABIC",), "ru": ("CYRILLIC",), "uk": ("CYRILLIC",), "el": ("GREEK",)}


def _odd_word(text: str, language: str) -> bool:
    import unicodedata

    if "<|" in text or "|>" in text:  # a Whisper control token leaked into the text
        return True
    allowed = ("LATIN",) + _ALLOWED_SCRIPTS.get(language, ())
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            if not name.startswith(allowed):
                return True
    return False


def odd_stretches(words: list[Word], language: str, join_gap: int = 3) -> list[tuple[int, int]]:
    """Word ranges with letters from another script or Whisper control tokens: a stretch in another
    language, or the engine inventing text. Close hits (up to `join_gap` words apart) are one range."""
    hits = [i for i, w in enumerate(words) if _odd_word(w.text, language)]
    ranges: list[tuple[int, int]] = []
    for i in hits:
        if ranges and i - ranges[-1][1] <= join_gap + 1:
            ranges[-1] = (ranges[-1][0], i)
        else:
            ranges.append((i, i))
    return ranges
