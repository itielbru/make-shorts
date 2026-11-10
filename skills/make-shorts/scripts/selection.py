"""Clip selection: scoring windows, snapping Claude's word picks to real
boundaries, and cleaning up the result.

Claude reads the indexed transcript and returns Pick objects made of word
indices (never seconds). Code turns them into Clips: it snaps to sentence
edges, keeps within the length limits without padding, and places the cut in
silence. Every number is a default that Claude may override for one video.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from transcript import Transcript, Word

SENTENCE_PUNCT = (".", "?", "!", "…")
PAUSE_S = 0.7


@dataclass
class Window:
    id: int
    start_word: int
    end_word: int
    start: float
    end: float


@dataclass
class Pick:
    start_word: int
    end_word: int
    score: int
    reason: str
    title: str
    hook_text: str | None = None


@dataclass
class Clip:
    start: float
    end: float
    start_word: int
    end_word: int
    score: int
    reason: str
    title: str
    hook_text: str | None
    warnings: list[str] = field(default_factory=list)


def shortlist_target(duration_s: float) -> int:
    return max(3, min(10, int(duration_s // 90) + 2))


def clip_count_targets(n: int) -> tuple[int, int]:
    """(floor, ceiling) for how many clips to deliver from a shortlist of n."""
    return max(2, min(6, n // 2 + 2)), min(12, max(4, 2 * n))


def _sentence_ends(words: list[Word]) -> list[bool]:
    last = len(words) - 1
    flags = []
    for i, w in enumerate(words):
        text = w.text.rstrip("\"'”’)»")
        flags.append(
            text.endswith(SENTENCE_PUNCT) or i == last or words[i + 1].start - w.end >= PAUSE_S
        )
    return flags


def _sentence_starts(ends: list[bool]) -> list[bool]:
    return [i == 0 or ends[i - 1] for i in range(len(ends))]


def build_windows(t: Transcript, window_s: float = 90.0, overlap_s: float = 30.0) -> list[Window]:
    """Scoring windows aligned to sentence boundaries, overlapping by about overlap_s."""
    words = t.words
    n = len(words)
    if n == 0:
        return []
    ends = _sentence_ends(words)
    starts = _sentence_starts(ends)
    stride = max(window_s - overlap_s, 1.0)
    windows: list[Window] = []
    s = 0
    while s < n:
        e = next(i for i in range(s, n) if ends[i])
        while True:
            k = next((i for i in range(e + 1, n) if ends[i]), None)
            if k is None or words[k].end - words[s].start > window_s:
                break
            e = k
        windows.append(Window(len(windows), s, e, words[s].start, words[e].end))
        if e >= n - 1:
            break
        candidate = next(
            (i for i in range(s + 1, n) if starts[i] and words[i].start >= words[s].start + stride),
            e + 1,
        )
        s = min(candidate, e + 1)
    return windows


def render_indexed_transcript(t: Transcript, start_word: int, end_word: int) -> str:
    """[index]word tokens with a (Ns) time marker at least every 4 seconds."""
    words = t.words
    parts: list[str] = []
    last_marker = words[start_word].start
    for i in range(start_word, end_word + 1):
        w = words[i]
        if w.start - last_marker >= 4.0:
            parts.append(f"({int(w.start)}s)")
            last_marker = w.start
        parts.append(f"[{i}]{w.text}")
    return " ".join(parts)


def _snap_start(starts: list[bool], s: int, limit: int) -> int:
    if starts[s]:
        return s
    for d in range(1, limit + 1):
        cands = [i for i in (s - d, s + d) if 0 <= i < len(starts) and starts[i]]
        if cands:
            return min(cands)  # ties prefer the earlier sentence
    return s


def _snap_end(ends: list[bool], e: int, limit: int) -> int:
    """The chosen last word often closes the point without punctuation in the transcript (seen:
    "...רק לא ביבי. זהו" snapped back six words and cut the punchline). So look forward first;
    go back only when no sentence ends within reach ahead."""
    if ends[e]:
        return e
    ahead = next((i for i in range(e + 1, min(len(ends), e + limit + 1)) if ends[i]), None)
    if ahead is not None:
        return ahead
    behind = next((i for i in range(e - 1, max(-1, e - limit - 1), -1) if ends[i]), None)
    return behind if behind is not None else e


def _edges(raw_start, raw_end, prev_end, next_start, silences, lead_s, tail_s, snap_s):
    before = next((s for s in silences if abs(s[1] - raw_start) <= snap_s and s[0] <= raw_start), None)
    start_t = max(raw_start - lead_s, before[0]) if before else raw_start - min(lead_s, 0.25)
    if prev_end is not None:
        start_t = max(start_t, prev_end)
    after = next((s for s in silences if abs(s[0] - raw_end) <= snap_s and s[1] >= raw_end), None)
    end_t = min(raw_end + tail_s, after[1]) if after else raw_end + min(tail_s, 0.35)
    if next_start is not None:
        end_t = min(end_t, next_start)
    return round(max(start_t, 0.0), 6), round(max(end_t, raw_end), 6)


def refine(
    t: Transcript,
    picks: list[Pick],
    *,
    min_s: float,
    max_s: float,
    silences: list[tuple[float, float]],
    lead_s: float = 0.35,
    tail_s: float = 0.45,
    snap_s: float = 1.5,
    sentence_snap_words: int = 12,
) -> tuple[list[Clip], list[str]]:
    words = t.words
    n = len(words)
    if n == 0:
        return [], ["לא נמצאו מילים בתמלול, ולכן אין ממה לחתוך. כדאי לבדוק שיש דיבור בסרטון ושהתמלול הצליח."]
    ends = _sentence_ends(words)
    starts = _sentence_starts(ends)
    total = words[-1].end - words[0].start

    def span(a: int, b: int) -> float:
        return words[b].end - words[a].start

    clips: list[Clip] = []
    warnings: list[str] = []
    seen: set[tuple[int, int]] = set()
    for p in picks:
        s, e = p.start_word, p.end_word
        problem = None
        if not (isinstance(s, int) and isinstance(e, int)):
            problem = "האינדקסים של המילים אינם מספרים"
        elif s < 0 or e < 0 or s >= n or e >= n:
            problem = "האינדקסים חורגים מהתמלול"
        elif e < s:
            problem = "הסוף של הקטע לפני ההתחלה שלו"
        if problem:
            warnings.append(f'דילגתי על הקטע "{p.title}": {problem}.')
            continue

        s2 = _snap_start(starts, s, sentence_snap_words)
        e2 = _snap_end(ends, e, sentence_snap_words)
        if e2 < s2:
            s2, e2 = s, e
        if (s2, e2) in seen:
            warnings.append(f'דילגתי על הקטע "{p.title}": הוא זהה לקטע אחר שכבר נבחר.')
            continue
        seen.add((s2, e2))

        if span(s2, e2) > max_s:
            fit = next((k for k in range(e2, s2 - 1, -1) if ends[k] and span(s2, k) <= max_s), None)
            if fit is None:
                warnings.append(f'דילגתי על הקטע "{p.title}": משפט אחד בו ארוך מהמקסימום ({max_s:.0f} שניות).')
                continue
            e2 = fit
        while span(s2, e2) < min_s:
            nxt = next((i for i in range(e2 + 1, n) if ends[i]), None)
            if nxt is None or span(s2, nxt) > max_s:
                break
            e2 = nxt
        if span(s2, e2) < min_s:
            if total < min_s:
                warnings.append(f'הסרטון קצר מהאורך המינימלי של קליפ, ולכן דילגתי על "{p.title}".')
            else:
                warnings.append(
                    f'הקטע "{p.title}" קצר מדי ({span(s2, e2):.0f} שניות) ולא הצלחתי להאריך אותו בלי לרפד.'
                )
            continue

        start_t, end_t = _edges(
            words[s2].start,
            words[e2].end,
            words[s2 - 1].end if s2 > 0 else None,
            words[e2 + 1].start if e2 + 1 < n else None,
            silences,
            lead_s,
            tail_s,
            snap_s,
        )
        clips.append(Clip(start_t, end_t, s2, e2, p.score, p.reason, p.title, p.hook_text))
    return clips, warnings


def _overlap(a: Clip, b: Clip) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def dedupe_and_trim(clips: list[Clip], max_clips: int) -> list[Clip]:
    """Drop lower-scored clips that overlap a kept one by 25% or more of the shorter clip,
    then keep the best max_clips by score (not the earliest). Result is in time order."""
    kept: list[Clip] = []
    for c in sorted(clips, key=lambda c: c.score, reverse=True):
        shorter = lambda other: min(c.end - c.start, other.end - other.start)
        if any(_overlap(c, k) >= 0.25 * shorter(k) for k in kept):
            continue
        kept.append(c)
    kept = kept[:max_clips]
    return sorted(kept, key=lambda c: c.start)
