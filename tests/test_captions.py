import re
import subprocess
from pathlib import Path

import pytest

import captions
import workspace
from transcript import Word

SKILL = Path(__file__).resolve().parent.parent / "skills" / "make-shorts"


def words(*texts, start=0.0, step=0.5):
    return [Word(t, round(start + i * step, 6), round(start + (i + 1) * step, 6)) for i, t in enumerate(texts)]


def dialogues(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def plain_text(dialogue: str) -> str:
    text = dialogue.split(",", 9)[9].replace("‫", "").replace("‬", "")
    return re.sub(r"\{[^}]*\}", "", text).replace("\\{", "{").replace("\\}", "}")


def test_blocks_hold_at_most_3_words_and_break_at_sentence_end():
    ws = words("א", "ב", "ג.", "ד", "ה", "ו", "ז")
    blocks = captions.group_words(ws, 3)
    assert [len(b.words) for b in blocks] == [3, 3, 1]
    ws2 = words("א", "ב.", "ג", "ד")
    assert [len(b.words) for b in captions.group_words(ws2, 3)] == [2, 2]
    assert blocks[0].start == 0.0 and blocks[0].end == 1.5


def test_ass_playres_is_1080x1920_and_default_font_is_Heebo_and_font_param_selects_other_family():
    blocks = captions.group_words(words("שלום", "עולם"))
    ass = captions.build_ass(blocks, preset="clean")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Heebo" in ass
    assert "Rubik" in captions.build_ass(blocks, preset="clean", font="rubik")


def test_bottom_margin_is_at_least_15_percent_of_height():
    ass = captions.build_ass(captions.group_words(words("שלום")), preset="bold")
    style = next(line for line in ass.splitlines() if line.startswith("Style: Default"))
    margin_v = int(style.split(",")[21])
    assert margin_v >= 288


def test_text_is_never_uppercased_and_latin_word_unchanged():
    blocks = captions.group_words(words("hello", "World", "שלום"))
    ass = captions.build_ass(blocks, preset="bold")
    joined = " ".join(plain_text(d) for d in dialogues(ass))
    assert "hello" in joined and "World" in joined
    assert "HELLO" not in joined


def test_geresh_gershayim_maqaf_preserved():
    blocks = captions.group_words(words("צה״ל", "מקף־עברי", "ג׳ירפה"))
    ass = captions.build_ass(blocks, preset="clean")
    text = " ".join(dialogues(ass))
    for token in ("צה״ל", "מקף־עברי", "ג׳ירפה"):
        assert token in text


def test_ass_special_characters_braces_and_newlines_escaped():
    blocks = captions.group_words(words("שלום{1}", "שורה\nחדשה"))
    ass = captions.build_ass(blocks, preset="clean")
    assert "שלום\\{1\\}" in ass
    assert all(line.startswith("Dialogue:") for line in ass.splitlines() if "שורה" in line)
    assert len(dialogues(ass)) == 1


def test_one_plain_event_per_block_with_block_times_and_no_inline_tags():
    # libass lays out RTL words in the wrong order around mid-line colour tags (seen in the
    # visual check), so each block is one plain event.
    blocks = captions.group_words(words("אחת", "שתיים", "שלוש", "ארבע"))
    events = dialogues(captions.build_ass(blocks, preset="bold"))
    assert len(events) == 2
    assert events[0].split(",")[1] == "0:00:00.00" and events[0].split(",")[2] == "0:00:01.50"
    assert "{" not in events[0].split(",", 9)[9]
    assert plain_text(events[0]) == "אחת שתיים שלוש"


def test_hebrew_blocks_are_wrapped_in_rtl_embedding_and_latin_blocks_are_not():
    # Visual check: without the wrapper libass lays a Hebrew line out left-to-right, which puts a
    # trailing comma on the wrong end and orders "123 עם word" backwards.
    ass = captions.build_ass(captions.group_words(words("שלום", "עולם")), preset="clean")
    hebrew_event = dialogues(ass)[0].split(",", 9)[9]
    assert hebrew_event.startswith("‫") and hebrew_event.endswith("‬")
    latin = captions.build_ass(captions.group_words(words("hello", "world")), preset="clean")
    assert "‫" not in latin


def test_title_event_is_shown_in_first_three_seconds():
    blocks = captions.group_words(words("שלום"))
    ass = captions.build_ass(blocks, preset="clean", title_text="כותרת הפתיח")
    title = next(d for d in dialogues(ass) if "כותרת הפתיח" in d)
    assert title.split(",")[1] == "0:00:00.00" and title.split(",")[2] == "0:00:03.00"
    assert ",Title," in title


def test_mixed_hebrew_english_digits_line_keeps_character_order_in_source_text():
    blocks = captions.group_words(words("שלום", "world", "123", "צה״ל."), words_per_block=4)
    events = dialogues(captions.build_ass(blocks, preset="clean"))
    assert {plain_text(e) for e in events} == {"שלום world 123 צה״ל."}


def test_unknown_preset_or_font_rejected():
    blocks = captions.group_words(words("שלום"))
    with pytest.raises(Exception):
        captions.build_ass(blocks, preset="nope")
    with pytest.raises(Exception):
        captions.build_ass(blocks, preset="clean", font="comic")


def test_remap_words_follows_hook_segments_and_repeats_a_teaser_quote():
    ws = words("אז", "הנה", "הסוד", "זה", "חזק")  # 0.0 .. 2.5
    segments = [(1.0, 2.0), (0.0, 2.5)]  # teaser: quote (הסוד זה) first, then the whole clip
    mapped = captions.remap_words(ws, segments)
    texts = [w.text for w in mapped]
    assert texts == ["הסוד", "זה", "אז", "הנה", "הסוד", "זה", "חזק"]
    assert mapped[0].start == 0.0 and mapped[1].end == pytest.approx(1.0)
    assert mapped[2].start == pytest.approx(1.0)  # original clip starts after the 1 s teaser


def test_every_font_has_its_file_and_ofl_license_next_to_it():
    for key, choice in captions.FONTS.items():
        font_path = SKILL / "fonts" / choice.folder / choice.file
        assert font_path.is_file(), key
        license_text = (SKILL / "fonts" / choice.folder / "OFL.txt").read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text


def test_selftest_sentence_has_hebrew_english_digits_and_hebrew_punctuation():
    s = captions.selftest_sentence()
    assert re.search(r"[֐-׿]", s) and re.search(r"[A-Za-z]", s) and re.search(r"\d", s)
    assert "״" in s and "־" in s


def test_selftest_renders_an_mp4_with_visible_text(hebrew_tmp):
    ws = workspace.Workspace.create(hebrew_tmp)
    out = captions.render_selftest(ws)
    assert out.exists() and out.stat().st_size > 10_000
    frame = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "2", "-i", str(out), "-frames:v", "1",
         "-vf", "crop=1080:600:0:1300,format=gray,scale=64:64", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout
    assert len(frame) == 64 * 64
    assert max(frame) - min(frame) > 40  # captions drawn over a flat background


def test_plain_preset_has_no_outline_and_keeps_a_soft_shadow():
    ass = captions.build_ass(captions.group_words(words("שלום")), preset="plain")
    style = next(line for line in ass.splitlines() if line.startswith("Style: Default"))
    fields = style.split(",")
    assert fields[15] == "1"         # BorderStyle: outline mode
    assert fields[16] == "0"         # Outline width 0: no black line around the letters
    assert int(fields[17]) > 0       # soft shadow so white text stays readable
