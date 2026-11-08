import pytest

import hooks
from hooks import HookMode
from messages import UserFacingError
from selection import Clip
from transcript import Transcript, Word

TEXT = ["אז", "הנה", "הסוד:", "זה", "משפט", "חזק", "מאוד.", "ואחרי", "זה", "ממשיכים", "לדבר", "על", "הדברים", "החשובים."]


def make_transcript():
    words = [Word(text, i * 0.5, i * 0.5 + 0.5) for i, text in enumerate(TEXT)]
    return Transcript(words=words, language="he", engine="test")


def make_clip(start_word=0, end_word=13, hook="זה משפט חזק מאוד", title="הסוד החשוב"):
    t = make_transcript()
    return Clip(
        start=t.words[start_word].start, end=t.words[end_word].end,
        start_word=start_word, end_word=end_word, score=90, reason="", title=title, hook_text=hook,
    ), t


def test_none_returns_single_segment():
    clip, t = make_clip()
    plan = hooks.plan_hook(clip, t, HookMode.NONE)
    assert plan.segments == [(0.0, 7.0)]
    assert plan.title_text is None and plan.teaser_text is None
    assert plan.high_risk is False


def test_title_keeps_content_and_sets_title_text():
    clip, t = make_clip()
    plan = hooks.plan_hook(clip, t, HookMode.TITLE)
    assert plan.segments == [(0.0, 7.0)]
    assert plan.title_text == "זה משפט חזק מאוד"
    assert plan.high_risk is False


def test_title_falls_back_to_clip_title_without_hook_text():
    clip, t = make_clip(hook=None)
    assert hooks.plan_hook(clip, t, HookMode.TITLE).title_text == "הסוד החשוב"


def test_teaser_prepends_quoted_sentence_segment_and_keeps_original():
    clip, t = make_clip()
    plan = hooks.plan_hook(clip, t, HookMode.TEASER)
    assert plan.segments == [(1.5, 3.5), (0.0, 7.0)]
    assert plan.teaser_text == "זה משפט חזק מאוד"
    assert plan.high_risk is False


def test_reorder_moves_quote_to_front_and_is_flagged_high_risk():
    clip, t = make_clip()
    plan = hooks.plan_hook(clip, t, HookMode.REORDER)
    assert plan.segments == [(1.5, 3.5), (0.0, 1.5), (3.5, 7.0)]
    assert plan.high_risk is True


def test_reorder_when_quote_already_first_keeps_single_segment():
    clip, t = make_clip(hook="אז הנה הסוד")
    plan = hooks.plan_hook(clip, t, HookMode.REORDER)
    assert plan.segments == [(0.0, 7.0)]
    assert plan.high_risk is True


def test_quote_match_ignores_edge_punctuation_and_case():
    clip, t = make_clip()
    assert hooks.verify_quote("זה משפט חזק מאוד.", t, clip.start_word, clip.end_word) is True
    assert hooks.verify_quote("הסוד", t, clip.start_word, clip.end_word) is True


def test_quote_not_verbatim_raises_hebrew_error():
    clip, t = make_clip(hook="זה משפט ממש חזק מאוד")
    with pytest.raises(UserFacingError) as exc:
        hooks.plan_hook(clip, t, HookMode.TEASER)
    assert exc.value.key == "hook_quote_not_verbatim"


def test_quote_outside_clip_range_rejected():
    clip, t = make_clip(start_word=0, end_word=6, hook="ואחרי זה ממשיכים")
    assert hooks.verify_quote("ואחרי זה ממשיכים", t, 0, 6) is False
    with pytest.raises(UserFacingError) as exc:
        hooks.plan_hook(clip, t, HookMode.TEASER)
    assert exc.value.key == "hook_quote_not_verbatim"


def test_teaser_without_hook_text_raises():
    clip, t = make_clip(hook=None)
    with pytest.raises(UserFacingError) as exc:
        hooks.plan_hook(clip, t, HookMode.TEASER)
    assert exc.value.key == "hook_needs_text"
