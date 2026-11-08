import re

import pytest

import selection
from selection import Clip, Pick
from transcript import Transcript, Word

HEBREW = re.compile(r"[֐-׿]")


def make_transcript(sentence_sizes, word_dur=0.4, gap=0.6):
    """Sentences of N words; the last word of each ends with '.'. Returns (transcript, silences)."""
    words, silences, t, n = [], [], 0.0, 0
    for size in sentence_sizes:
        for i in range(size):
            text = f"w{n}" + ("." if i == size - 1 else "")
            words.append(Word(text, round(t, 6), round(t + word_dur, 6)))
            t += word_dur
            n += 1
        if gap:
            silences.append((round(t, 6), round(t + gap, 6)))
            t += gap
    return Transcript(words=words, language="he", engine="test"), silences


def pick(s, e, score=80, title="קטע"):
    return Pick(start_word=s, end_word=e, score=score, reason="סיבה", title=title, hook_text=None)


def clip(start, end, score):
    return Clip(start=start, end=end, start_word=0, end_word=0, score=score, reason="", title="t", hook_text=None, warnings=[])


def test_shortlist_target_3600s_is_10_and_200s_is_4():
    assert selection.shortlist_target(3600) == 10
    assert selection.shortlist_target(200) == 4
    assert selection.shortlist_target(10) == 3


def test_clip_count_targets_n5_is_4_and_10():
    assert selection.clip_count_targets(5) == (4, 10)
    assert selection.clip_count_targets(1) == (2, 4)
    assert selection.clip_count_targets(20) == (6, 12)


def test_windows_never_split_a_sentence():
    t, _ = make_transcript([10] * 60)  # about 276 s
    windows = selection.build_windows(t, window_s=90.0, overlap_s=30.0)
    assert len(windows) >= 3
    for w in windows:
        assert w.start_word % 10 == 0, "starts on a sentence start"
        assert w.end_word % 10 == 9, "ends on a sentence end"
    assert windows[0].start_word == 0
    assert windows[-1].end_word == len(t.words) - 1
    assert all(b.start_word < a.end_word for a, b in zip(windows, windows[1:])), "windows overlap"


def test_windows_on_empty_transcript_is_empty():
    assert selection.build_windows(Transcript(words=[], language="he", engine="t")) == []


def test_indexed_transcript_tags_words_and_adds_time_markers():
    t, _ = make_transcript([10] * 3)
    text = selection.render_indexed_transcript(t, 0, len(t.words) - 1)
    assert text.startswith("[0]w0")
    assert "[29]w29." in text
    assert "(4s)" in text or "(5s)" in text


def test_refine_snaps_start_to_sentence_start_within_12_words():
    t, silences = make_transcript([10] * 5)
    clips, _ = selection.refine(t, [pick(13, 39)], min_s=5, max_s=100, silences=silences)
    assert clips[0].start_word == 10


def test_refine_snaps_end_to_sentence_end_within_12_words():
    t, silences = make_transcript([10] * 5)
    clips, _ = selection.refine(t, [pick(10, 27)], min_s=5, max_s=100, silences=silences)
    assert clips[0].end_word == 29


def test_refine_snaps_to_word_boundary_with_lead_0_35_and_tail_0_45_into_silence():
    t, silences = make_transcript([10, 10, 10], gap=1.0)
    clips, _ = selection.refine(t, [pick(10, 19)], min_s=1, max_s=60, silences=silences)
    assert clips[0].start == pytest.approx(t.words[10].start - 0.35)
    assert clips[0].end == pytest.approx(t.words[19].end + 0.45)


def test_refine_never_pads_into_a_neighbouring_word_when_there_is_no_silence():
    t, _ = make_transcript([10, 10, 10], gap=0.0)
    clips, _ = selection.refine(t, [pick(10, 19)], min_s=1, max_s=60, silences=[])
    assert clips[0].start >= t.words[9].end - 1e-9
    assert clips[0].end <= t.words[20].start + 1e-9


def test_refine_extends_short_clip_to_later_sentence_end_never_pads():
    t, silences = make_transcript([10] * 6)
    clips, _ = selection.refine(t, [pick(10, 19)], min_s=15, max_s=60, silences=silences)
    assert clips[0].end_word == 49
    assert 15 <= clips[0].end - clips[0].start <= 60


def test_refine_95s_clip_ending_on_sentence_is_kept_when_profile_max_allows():
    t, silences = make_transcript([20] * 12, word_dur=0.5, gap=0.5)
    clips, _ = selection.refine(t, [pick(0, 179)], min_s=20, max_s=120, silences=silences)
    assert clips[0].end_word == 179


def test_refine_trims_back_to_last_sentence_end_under_max():
    t, silences = make_transcript([20] * 12, word_dur=0.5, gap=0.5)
    clips, _ = selection.refine(t, [pick(0, 179)], min_s=20, max_s=90, silences=silences)
    assert clips[0].end_word == 159
    assert clips[0].end - clips[0].start <= 90


def test_refine_drops_clip_that_cannot_reach_min_without_padding():
    t, silences = make_transcript([10] * 3)
    clips, warnings = selection.refine(t, [pick(0, 29, title="קטע קצר")], min_s=20, max_s=90, silences=silences)
    assert clips == []
    assert any("קטע קצר" in w for w in warnings)


def test_dedupe_drops_lower_score_when_overlap_at_least_25_percent_of_shorter():
    high = clip(0.0, 40.0, score=90)
    low = clip(30.0, 60.0, score=70)  # overlap 10 s = 33% of the 30 s shorter clip
    apart = clip(100.0, 130.0, score=50)
    kept = selection.dedupe_and_trim([low, high, apart], max_clips=10)
    assert [c.score for c in kept] == [90, 50]


def test_dedupe_keeps_clips_that_overlap_less_than_25_percent():
    a = clip(0.0, 40.0, score=90)
    b = clip(36.0, 66.0, score=70)  # overlap 4 s = 13% of 30 s
    assert len(selection.dedupe_and_trim([a, b], max_clips=10)) == 2


def test_trim_keeps_highest_scores_not_earliest():
    clips = [clip(0, 20, 50), clip(100, 120, 95), clip(200, 220, 80)]
    kept = selection.dedupe_and_trim(clips, max_clips=2)
    assert sorted(c.score for c in kept) == [80, 95]
    assert [c.start for c in kept] == sorted(c.start for c in kept)


def test_reversed_out_of_range_and_duplicate_picks_are_dropped_with_hebrew_reason_not_crash():
    t, silences = make_transcript([10] * 4)
    picks = [pick(20, 10, title="הפוך"), pick(0, 999, title="חורג"), pick(-1, 5, title="שלילי"),
             pick(10, 19, title="תקין"), pick(10, 19, title="כפול")]
    clips, warnings = selection.refine(t, picks, min_s=1, max_s=60, silences=silences)
    assert [c.title for c in clips] == ["תקין"]
    assert len(warnings) == 4
    assert all(HEBREW.search(w) for w in warnings)


def test_transcript_with_no_words_returns_no_clips_and_plain_message():
    empty = Transcript(words=[], language="he", engine="t")
    clips, warnings = selection.refine(empty, [pick(0, 5)], min_s=20, max_s=90, silences=[])
    assert clips == []
    assert warnings and all(HEBREW.search(w) for w in warnings)


def test_video_shorter_than_min_clip_returns_no_clips_message():
    t, silences = make_transcript([10])  # 4 seconds
    clips, warnings = selection.refine(t, [pick(0, 9)], min_s=20, max_s=90, silences=silences)
    assert clips == []
    assert any("קצר" in w for w in warnings)
