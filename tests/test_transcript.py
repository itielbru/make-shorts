import transcript
from transcript import Transcript, Word


def test_roundtrip_keeps_hebrew_and_fields(tmp_path):
    t = Transcript(
        words=[Word("שלום", 0.0, 0.5), Word("עולם.", 0.5, 1.0)],
        language="he",
        engine="groq",
        gaps=[(10.0, 12.5)],
        approximate_timing=True,
    )
    path = tmp_path / "t.json"
    transcript.save_transcript(t, path)
    loaded = transcript.load_transcript(path)
    assert loaded == t
    assert "שלום" in path.read_text(encoding="utf-8")


def test_cache_key_changes_with_engine_and_language():
    base = transcript.cache_key("abc", "groq", "he")
    assert base == transcript.cache_key("abc", "groq", "he")
    assert base != transcript.cache_key("abc", "elevenlabs", "he")
    assert base != transcript.cache_key("abc", "groq", "en")
    assert base != transcript.cache_key("abd", "groq", "he")


def test_drop_words_in_trailing_silence_removes_thank_you_after_last_speech():
    words = [Word("שלום", 0.0, 0.5), Word("עולם", 0.5, 1.0), Word("Thank", 58.0, 58.5), Word("you.", 58.5, 59.0)]
    kept = transcript.drop_words_in_trailing_silence(words, silences=[(2.0, 60.0)], audio_end=60.0)
    assert [w.text for w in kept] == ["שלום", "עולם"]


def test_drop_keeps_words_in_silence_that_is_not_trailing():
    words = [Word("a", 10.0, 10.5), Word("b", 40.0, 40.5)]
    kept = transcript.drop_words_in_trailing_silence(words, silences=[(5.0, 20.0)], audio_end=60.0)
    assert [w.text for w in kept] == ["a", "b"]


def test_interpolate_words_evenly_spans_segment():
    words = transcript.interpolate_words("שלום עולם יפה", 10.0, 13.0)
    assert [w.text for w in words] == ["שלום", "עולם", "יפה"]
    assert words[0].start == 10.0
    assert words[-1].end == 13.0
    durations = [round(w.end - w.start, 6) for w in words]
    assert durations == [1.0, 1.0, 1.0]
    assert all(a.end == b.start for a, b in zip(words, words[1:]))


def test_interpolate_words_empty_text_returns_nothing():
    assert transcript.interpolate_words("   ", 0.0, 5.0) == []
