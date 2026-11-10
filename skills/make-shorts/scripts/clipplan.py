"""Per-clip edit plan: parts joined from different places, vertical crop position, hook.

A plan file maps a clip id to:
  parts     list of [start_word, end_word] kept in this order (cut anything between them out,
            or join related passages from different parts of the episode)
  crop_x    0..1 position of the vertical crop inside the wide picture (0 = left, 1 = right);
            one number, or one per part
  hook      none | title | teaser (reorder is not allowed on joined parts)
  hook_text a sentence that must appear word for word inside the parts
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from messages import UserFacingError
from transcript import Transcript

LEAD_S = 0.25
TAIL_S = 0.35


@dataclass
class ClipPlan:
    parts: list[tuple[int, int]] | None = None
    crop_x: list[float] | None = None
    hook: str | None = None
    hook_text: str | None = None


def _bad(detail: str) -> UserFacingError:
    return UserFacingError("unknown", detail=detail)


def load_plan(path: Path) -> dict[int, ClipPlan]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _bad("קובץ התוכנית לא נקרא. הוא חייב להיות JSON תקין.") from None
    if not isinstance(raw, dict):
        raise _bad("קובץ התוכנית חייב להיות אובייקט שמפתחו מספר קליפ.")
    plans: dict[int, ClipPlan] = {}
    for key, value in raw.items():
        try:
            clip_id = int(key)
            if not isinstance(value, dict):
                raise TypeError
            parts = value.get("parts")
            if parts is not None:
                parts = [(int(a), int(b)) for a, b in parts]
            crop = value.get("crop_x")
            if crop is not None:
                crop = [float(x) for x in (crop if isinstance(crop, list) else [crop])]
                if any(not 0.0 <= x <= 1.0 for x in crop):
                    raise ValueError
            hook = value.get("hook")
            hook_text = value.get("hook_text")
            if hook is not None and hook not in ("none", "title", "teaser"):
                raise ValueError
            plans[clip_id] = ClipPlan(parts, crop, hook, hook_text if hook_text is None else str(hook_text))
        except (TypeError, ValueError):
            raise _bad(f"התוכנית של קליפ {key} לא תקינה (חלקים, crop_x בין 0 ל-1, או פתיח).") from None
    return plans


EDGE_REACH_S = 0.6   # how far from the word the waveform search for a quiet cut point may go
EDGE_INSIDE_S = 0.05  # word timings are approximate: the search may start a hair inside the word
EDGE_MIN_S = 0.15     # the search always reaches at least this far outside the word


def segments_from_parts(
    t: Transcript, parts: list[tuple[int, int]], quietest: Callable[[float, float], float] | None = None
) -> list[tuple[float, float]]:
    """Word ranges to time segments.

    With `quietest` (from the waveform), each edge is cut at the quietest moment between the
    word and its neighbour, so a cut never clips the start or end of a spoken word. Without it,
    padding stops at the middle of the gap to the next word.
    """
    n = len(t.words)
    merged: list[tuple[int, int]] = []
    for a, b in parts:
        if not (0 <= a <= b < n):
            raise _bad(f"טווח מילים לא תקין: {a}-{b}.")
        # parts that touch (finish_sentences extends a part up to the word before the next one) are
        # one stretch; cut separately, their edge searches overlapped and the sound repeated
        if merged and merged[-1][0] <= a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out: list[tuple[float, float]] = []
    for a, b in merged:
        first, last = t.words[a], t.words[b]
        prev_end = t.words[a - 1].end if a > 0 else 0.0
        if quietest is not None:
            next_start = t.words[b + 1].start if b + 1 < n else last.end + EDGE_REACH_S
            # Words with no pause between them still get a search window of EDGE_MIN_S: the
            # timings are approximate, and the waveform knows better where the word really begins.
            lo = min(max(prev_end, first.start - EDGE_REACH_S), first.start - EDGE_MIN_S)
            hi = max(min(next_start, last.end + EDGE_REACH_S), last.end + EDGE_MIN_S)
            start = quietest(max(0.0, lo), first.start + EDGE_INSIDE_S)
            if next_start < last.end:
                # the timings overlap, so the last word's end time is wrong (seen: 0.5 s too late,
                # running into the next speaker); the real pause lies before the next word
                end = quietest(max(last.start, next_start - EDGE_REACH_S), next_start + EDGE_INSIDE_S)
            else:
                end = quietest(last.end - EDGE_INSIDE_S, hi)
            out.append((round(max(0.0, start), 3), round(end, 3)))
            continue
        start = first.start - min(LEAD_S, max(0.0, (first.start - prev_end) / 2))
        if b + 1 < n:
            end = last.end + min(TAIL_S, max(0.0, (t.words[b + 1].start - last.end) / 2))
        else:
            end = last.end + TAIL_S
        out.append((round(max(0.0, start), 3), round(end, 3)))
    return out


def frame_aligned(segments: list[tuple[float, float]], fps: float) -> list[tuple[float, float]]:
    """Each segment's length rounded to whole frames, keeping its start. The picture of a part is a
    whole number of frames; with picture and sound joined separately, a sound part longer or shorter
    by a fraction of a frame would add up over the joins into a lip-sync drift."""
    out = []
    for a, b in segments:
        frames = max(1, round((b - a) * fps))
        out.append((a, round(a + frames / fps, 6)))
    return out


def frame_aligned_video(
    aligned: list[tuple[float, float]], audio: list[tuple[float, float]],
    video: list[tuple[float, float]], fps: float,
) -> list[tuple[float, float]]:
    """Picture edges matching frame-aligned sound edges. At an L-cut the picture's edge sits some
    frames away from the sound's; that offset is kept, rounded to whole frames, so each picture part
    is a whole number of frames and the picture and sound totals stay exactly equal."""
    offsets = [0.0]
    for (_, a_end), (_, v_end) in zip(audio[:-1], video[:-1]):
        offsets.append(round((v_end - a_end) * fps) / fps)
    offsets.append(0.0)
    return [(round(a + offsets[i], 6), round(b + offsets[i + 1], 6)) for i, (a, b) in enumerate(aligned)]


def quote_in_parts(t: Transcript, parts: list[tuple[int, int]], quote: str) -> bool:
    """Is the quote, word for word, inside one of the parts the clip plays (not in a gap between them)?"""
    from hooks import find_quote

    return any(find_quote(quote, t, a, b) is not None for a, b in parts)


def crop_for_segments(
    segments: list[tuple[float, float]], base: list[tuple[float, float]], crop_x: list[float] | None
) -> list[float]:
    """One crop position per rendered segment. A teaser quote takes the position of the part it comes from."""
    if not crop_x:
        return [0.5] * len(segments)
    if len(crop_x) == 1:
        return [crop_x[0]] * len(segments)
    out: list[float] = []
    for s, e in segments:
        mid = (s + e) / 2
        idx = next((i for i, (bs, be) in enumerate(base) if bs - 1e-6 <= mid <= be + 1e-6), 0)
        out.append(crop_x[min(idx, len(crop_x) - 1)])
    return out


SENTENCE_PAUSE_S = 0.5


def finish_sentences(
    t: Transcript, parts: list[tuple[int, int]], max_extra_words: int = 30, inner_max_words: int = 8,
) -> list[tuple[int, int]]:
    """Move each part's end forward to the end of the sentence.

    A cut in the middle of a sentence is worse than a clip that runs a few seconds longer. The last
    part runs on to punctuation or a real pause. A part followed by another part ends right before
    material the clip leaves out on purpose, so it only moves to punctuation a few words away; with
    no punctuation it stays put (seen: an unpunctuated transcript ran 20 s into a cut argument).
    Parts never grow into the next part or past the end of the transcript.
    """
    n = len(t.words)
    out: list[tuple[int, int]] = []
    for number, (a, b) in enumerate(parts):
        inner = number + 1 < len(parts)
        limit = min(n - 1, b + (inner_max_words if inner else max_extra_words))
        if inner and parts[number + 1][0] > b:
            limit = min(limit, parts[number + 1][0] - 1)
        end, found = b, False
        while end <= limit:
            word = t.words[end]
            if word.text.rstrip("\"'”’)»").endswith(_SENTENCE_END):
                found = True
                break
            if end == limit:
                break
            if not inner and t.words[end + 1].start - word.end >= SENTENCE_PAUSE_S:
                found = True
                break
            end += 1
        out.append((a, end if found or not inner else b))
    return out


_OPENERS =frozenset({"אבל", "כי", "אז", "אלא", "ואז", "וגם", "גם", "למרות", "לכן", "ובכל"})
_SENTENCE_END = (".", "?", "!", "…")
MIN_PART_S = 3.0  # seen in a real clip: a 1.2 s opening part looked frozen and then jumped


def check_parts(t: Transcript, parts: list[tuple[int, int]]) -> list[str]:
    """Plain-Hebrew warnings about continuity: a part that opens mid-thought or ends mid-sentence."""
    warnings: list[str] = []
    last = len(parts)
    for number, (a, b) in enumerate(parts, start=1):
        label = f"חלק {number}" if last > 1 else "הקליפ"
        first = t.words[a].text.strip("\"'”’)»(.,!?;:…-–")
        if first in _OPENERS:
            warnings.append(f"{label} נפתח במילת קישור (\"{first}\"), אז הוא עלול להישמע כהמשך של משהו שלא רואים. כדאי להתחיל מוקדם יותר או לדלג על המילה.")
        if not t.words[b].text.rstrip("\"'”’)»").endswith(_SENTENCE_END):
            if number < last:  # the next stretch was left out on purpose: growing into it is the wrong fix
                warnings.append(f"{label} מסתיים באמצע משפט (\"{t.words[b].text}\"), ממש לפני קטע שהוצא. אם המשפט באמת נגמר שם, הוסף נקודה למילה הזו (correct --edits). אם לא, האריכו את החלק ב-parts.")
            else:
                warnings.append(f"{label} מסתיים באמצע משפט (\"{t.words[b].text}\"). כדאי להאריך עד סוף המשפט.")
        if t.words[b].end - t.words[a].start < MIN_PART_S:
            warnings.append(f"{label} קצר מאוד (פחות מ-{MIN_PART_S:g} שניות). חלק קצר נראה כמו הבזק ואחריו קפיצה, עדיף לוותר עליו או להאריך אותו.")
    return warnings
