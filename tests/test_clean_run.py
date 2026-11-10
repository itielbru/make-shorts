"""Friction found by a clean run of the skill as a new user (a 76-minute TV broadcast)."""
import json

import clipplan
import qa
import selection
import transcript as tr
from transcript import Transcript, Word


def _t(texts, step=0.3):
    return Transcript(words=[Word(w, i * step, i * step + 0.25) for i, w in enumerate(texts)], language="he", engine="x")


# 3a. refine snapped the chosen end six words back and cut the punchline
def test_refine_keeps_the_chosen_end_by_looking_forward_first():
    texts = ["פתיחה."] + [f"מ{i}" for i in range(40)] + ["שורה."] + ["רק", "לא", "ביבי", "זהו", "המשך", "סוף."]
    t = _t(texts)
    end = texts.index("זהו")
    clips, _ = selection.refine(t, [selection.Pick(1, end, 5, "", "x")], min_s=1, max_s=60, silences=[])
    assert clips[0].end_word >= end


# 3b. an unpunctuated inner part ran 20 s into the argument the user cut out
def test_inner_part_without_punctuation_does_not_grow_into_the_cut():
    t = _t([f"w{i}" for i in range(60)])
    out = clipplan.finish_sentences(t, [(0, 10), (40, 50)])
    assert out[0] == (0, 10)


def test_inner_part_ending_mid_sentence_gets_the_add_a_period_advice():
    t = _t(["א", "ב", "ג.", "ד", "ה."])
    warnings = clipplan.check_parts(t, [(0, 1), (3, 4)])
    assert any("correct --edits" in w for w in warnings)


# 6. glossary: phrases, and the prefix letter that doubled ("בבתורתנו")
def test_glossary_replaces_a_several_word_phrase_keeping_indexes_and_timing():
    words = [Word("אמר", 0, 1), Word("מילטה", 1, 2), Word("דה", 2, 3), Word("אבידה.", 3, 4)]
    fixed = tr.apply_glossary(words, {"מילטה דה אבידה": "מילתא דעבידא"})
    assert [w.text for w in fixed] == ["אמר", "מילתא", "דעבידא.", ""]
    assert [(w.start, w.end) for w in fixed] == [(w.start, w.end) for w in words]


def test_glossary_warns_when_the_value_starts_with_a_prefix_the_key_lacks():
    assert tr.glossary_warnings({"טרוטנו": "בתורתנו"})
    assert not tr.glossary_warnings({"טרוטנו": "תורתנו"})


# 7. junk in the transcript was not flagged
def test_odd_stretches_flags_other_scripts_and_control_tokens():
    words = [Word(x, i, i + 1) for i, x in enumerate(["שלום", "<|sv|>", "עולם", "Привет", "γεια", "טוב", "OK"])]
    assert tr.odd_stretches(words, "he") == [(1, 4)]
    assert tr.odd_stretches([Word("hello", 0, 1), Word("OK", 1, 2)], "he") == []


# 8. qa judged stale edges; it now checks the rendered segments and says what to do
def test_edge_findings_use_rendered_segments_and_give_advice():
    words = [Word("א", 0.0, 1.0), Word("ב", 1.0, 2.0)]
    assert qa.edge_findings(words, [(0.0, 1.5)]) and "parts" in qa.edge_findings(words, [(0.0, 1.5)])[0]
    assert qa.edge_findings(words, [(0.0, 2.0)]) == []
    # timings overlap the next word: the cut sits in the real pause, not inside the word
    overlap = [Word("הראשון.", 854.596, 855.036), Word("מי", 854.976, 855.116)]
    assert qa.edge_findings(overlap, [(850.0, 854.831)]) == []


# 2. recaption threw away every fix made with correct --edits
def test_word_fixes_are_recorded_and_carried_onto_the_new_words(tmp_path):
    import make_shorts

    make_shorts.record_word_fixes(tmp_path, [{"index": 1, "text": "מצחיק"}])
    assert make_shorts.load_word_fixes(tmp_path) == {1: "מצחיק"}
    source = [Word("א", 10.0, 10.5), Word("מצחיק", 10.6, 11.0), Word("ג", 11.1, 11.5)]
    new = [Word("א", 0.0, 0.5), Word("מצליק", 0.62, 1.0), Word("ג", 1.1, 1.5)]
    words, carried, lost = make_shorts.carry_word_fixes(new, source, {1: "מצחיק"}, [(10.0, 12.0)])
    assert [w.text for w in words] == ["א", "מצחיק", "ג"] and carried == 1 and lost == []


def test_correct_recaptioned_shows_and_fixes_clip_words(tmp_path, monkeypatch, capsys):
    import make_shorts

    make_shorts.save_clip_words(tmp_path, 3, [Word("מצליק", 0.0, 0.5)], [(0.0, 1.0)])
    (tmp_path / "process_config.json").write_text(json.dumps({"rendered": {"3": {}}}), encoding="utf-8")
    edits = tmp_path / "e.json"
    edits.write_text(json.dumps([{"index": 0, "text": "מצחיק"}]), encoding="utf-8")
    assert make_shorts.main(["correct", "--project", str(tmp_path), "--recaptioned", "--clips", "3", "--edits", str(edits)]) == 0
    assert "[0]מצחיק" in capsys.readouterr().out
    saved = json.loads((tmp_path / "clip_words" / "03.json").read_text(encoding="utf-8"))
    assert saved["words"][0]["text"] == "מצחיק"
