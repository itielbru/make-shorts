"""Hook planning: what the first seconds of a clip look like.

Modes: none, title (text on screen, content unchanged), teaser (quote first,
then the clip as cut), reorder (quote moved to the front; high risk because it
can distort context). A quoted sentence must appear verbatim in the clip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from messages import UserFacingError
from selection import Clip
from transcript import Transcript

_EDGE = re.compile(r"^[\W_]+|[\W_]+$")


class HookMode(str, Enum):
    NONE = "none"
    TITLE = "title"
    TEASER = "teaser"
    REORDER = "reorder"


@dataclass
class HookPlan:
    segments: list[tuple[float, float]]
    title_text: str | None
    teaser_text: str | None
    high_risk: bool


def _norm(token: str) -> str:
    return _EDGE.sub("", token).casefold()


def find_quote(quote: str, t: Transcript, start_word: int, end_word: int) -> tuple[int, int] | None:
    tokens = [n for n in (_norm(x) for x in quote.split()) if n]
    if not tokens:
        return None
    words = [_norm(w.text) for w in t.words]
    for i in range(start_word, end_word - len(tokens) + 2):
        if words[i : i + len(tokens)] == tokens:
            return i, i + len(tokens) - 1
    return None


def verify_quote(quote: str, t: Transcript, start_word: int, end_word: int) -> bool:
    return find_quote(quote, t, start_word, end_word) is not None


def plan_hook(
    clip: Clip, t: Transcript, mode: HookMode, segments: list[tuple[float, float]] | None = None
) -> HookPlan:
    """`segments` replaces the single (start, end) cut when the clip is built from several parts."""
    mode = HookMode(mode)
    whole = list(segments) if segments else [(clip.start, clip.end)]
    if segments and len(segments) > 1 and mode is HookMode.REORDER:
        raise UserFacingError("unknown", detail="הקדמת משפט לא נתמכת בקליפ שמורכב מכמה חלקים. אפשר טיזר במקום.")
    if mode is HookMode.NONE:
        return HookPlan(whole, None, None, False)
    if mode is HookMode.TITLE:
        return HookPlan(whole, clip.hook_text or clip.title, None, False)

    if not clip.hook_text:
        raise UserFacingError("hook_needs_text")
    found = find_quote(clip.hook_text, t, clip.start_word, clip.end_word)
    if found is None:
        raise UserFacingError("hook_quote_not_verbatim")
    quote_start = t.words[found[0]].start
    quote_end = t.words[found[1]].end

    if mode is HookMode.TEASER:
        return HookPlan([(quote_start, quote_end), *whole], None, clip.hook_text, False)

    # REORDER: quote first, then everything else in its original order.
    if found[0] == clip.start_word:
        return HookPlan(whole, None, None, True)
    segments = [(quote_start, quote_end), (clip.start, quote_start)]
    if clip.end - quote_end > 1e-6:
        segments.append((quote_end, clip.end))
    return HookPlan(segments, None, None, True)
