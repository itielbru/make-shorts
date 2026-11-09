"""Behaviour added after the first real run: vertical crop per clip, joined parts, social captions, fades."""
import pytest

import captions
import hooks
import render
from hooks import HookMode
from messages import UserFacingError
from selection import Clip
from transcript import Transcript, Word
import clipplan


def _graph(**kw):
    cmd = render.build_ffmpeg_cmd("in.mp4", kw.pop("segments", [(2.0, 6.0)]), ass_name="c.ass",
                                  out_name="o.mp4", **kw)
    return cmd[cmd.index("-filter_complex") + 1]


def test_center_crop_uses_crop_x_per_segment_before_concat():
    graph = _graph(segments=[(1.0, 3.0), (10.0, 14.0)], reframe="center", crop_x=[0.2, 0.8])
    assert "crop=1080:1920:(iw-1080)*0.2:0" in graph
    assert "crop=1080:1920:(iw-1080)*0.8:0" in graph
    assert graph.index("(iw-1080)*0.2") < graph.index("concat=n=2")


def test_center_crop_defaults_to_the_middle():
    assert "crop=1080:1920:(iw-1080)*0.5:0" in _graph(reframe="center")


def test_no_fade_to_black_only_short_audio_fades_at_the_edges():
    graph = _graph(reframe="center")  # 4.0 s clip
    assert ",fade=t=" not in graph and "[vc]fade" not in graph
    assert "afade=t=in:st=0:d=0.05" in graph
    assert "afade=t=out:st=3.85:d=0.15" in graph


def test_joins_between_segments_get_short_audio_fades_against_pops():
    graph = _graph(segments=[(1.0, 3.0), (10.0, 14.0)], reframe="center")
    assert graph.count("afade=t=in:st=0:d=0.04") >= 2
    assert "afade=t=out:st=1.96:d=0.04" in graph


def test_social_preset_is_big_unframed_and_raised_off_the_bottom():
    p = captions.PRESETS["social"]
    assert p.size >= 100 and p.outline == 0
    assert p.words_per_block <= 2
    ass = captions.build_ass(captions.group_words([Word("שלום", 0.0, 0.5)], 2), preset="social")
    margin_v = int(ass.split("Style: Default,")[1].split("\n")[0].split(",")[-2])
    assert margin_v >= 400  # well above the bottom edge, not in the 15% strip


def _transcript():
    words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(30)]
    return Transcript(words=words, language="he", engine="test")


def test_parts_become_padded_time_segments_in_the_given_order():
    t = _transcript()
    segs = clipplan.segments_from_parts(t, [(10, 14), (2, 5)])
    assert len(segs) == 2
    assert segs[0][0] < t.words[10].start and segs[0][1] > t.words[14].end
    assert segs[1][0] < t.words[2].start
    assert segs[0][0] >= t.words[9].end and segs[0][1] <= t.words[15].start  # padding never touches neighbours


def test_adjacent_parts_that_touch_are_merged_into_one_segment():
    t = _transcript()
    # parts with no word between them are one stretch: cut separately, their edge searches
    # overlapped in the same pause and a slice of sound played twice (second code review)
    segs = clipplan.segments_from_parts(t, [(2, 5), (6, 9)])
    assert len(segs) == 1
    segs = clipplan.segments_from_parts(t, [(2, 5), (5, 9)])
    assert len(segs) == 1
    assert len(clipplan.segments_from_parts(t, [(2, 5), (7, 9)])) == 2  # a word left out: a real cut


def test_bad_parts_are_rejected_in_hebrew():
    t = _transcript()
    with pytest.raises(UserFacingError):
        clipplan.segments_from_parts(t, [(5, 2)])
    with pytest.raises(UserFacingError):
        clipplan.segments_from_parts(t, [(0, 99)])


def test_load_plan_reads_parts_crop_and_hook(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"5": {"parts": [[1, 4], [8, 9]], "crop_x": [0.3, 0.7], "hook": "teaser", "hook_text": "w8 w9"}}',
                    encoding="utf-8")
    plan = clipplan.load_plan(path)
    assert plan[5].parts == [(1, 4), (8, 9)]
    assert plan[5].crop_x == [0.3, 0.7]
    assert plan[5].hook == "teaser" and plan[5].hook_text == "w8 w9"


def test_load_plan_rejects_garbage(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(UserFacingError):
        clipplan.load_plan(path)


def test_teaser_over_custom_segments_puts_the_quote_first_and_keeps_the_parts():
    t = _transcript()
    clip = Clip(start=t.words[1].start, end=t.words[9].end, start_word=1, end_word=9, score=1,
                reason="", title="x", hook_text="w8 w9")
    parts = [(0.8, 2.3), (4.0, 4.9)]
    plan = hooks.plan_hook(clip, t, HookMode.TEASER, segments=parts)
    assert plan.segments[0] == (t.words[8].start, t.words[9].end)
    assert plan.segments[1:] == parts


def test_reorder_over_custom_segments_is_refused():
    t = _transcript()
    clip = Clip(start=0.0, end=4.9, start_word=0, end_word=9, score=1, reason="", title="x", hook_text="w8 w9")
    with pytest.raises(UserFacingError):
        hooks.plan_hook(clip, t, HookMode.REORDER, segments=[(0.0, 1.0), (4.0, 4.9)])


# ---- automatic caption correction: multi-word replacement and deleting hallucinated words

def test_apply_edits_replaces_with_several_words_keeping_indices_and_timing():
    import transcript as tr
    words = [Word("a", 0.0, 0.5), Word("מנהלת", 0.5, 1.0), Word("b", 1.0, 1.5)]
    fixed = tr.apply_edits(words, [{"index": 1, "text": "מנהל שיחות"}])
    assert [w.text for w in fixed] == ["a", "מנהל שיחות", "b"]
    assert (fixed[1].start, fixed[1].end) == (0.5, 1.0)
    assert words[1].text == "מנהלת"  # input untouched


def test_apply_edits_empty_text_removes_the_word_from_captions_but_keeps_indices():
    import transcript as tr
    words = [Word("a", 0.0, 0.5), Word("תודה", 0.5, 1.0), Word("b", 1.0, 1.5)]
    fixed = tr.apply_edits(words, [{"index": 1, "text": ""}])
    assert len(fixed) == 3 and fixed[1].text == ""
    blocks = captions.group_words(captions.remap_words(fixed, [(0.0, 1.5)]), 3)
    assert [w.text for b in blocks for w in b.words] == ["a", "b"]


def test_apply_edits_rejects_out_of_range_index():
    import transcript as tr
    with pytest.raises(UserFacingError):
        tr.apply_edits([Word("a", 0.0, 0.5)], [{"index": 5, "text": "x"}])


# ---- whole-transcript glossary: names and terms found from web context

def test_glossary_replaces_names_everywhere_keeping_punctuation_and_prefixes():
    import transcript as tr
    words = [Word("הפטריותים", 0, 1), Word("בפטריותים,", 1, 2), Word("פטריותים.", 2, 3), Word("שלום", 3, 4)]
    fixed = tr.apply_glossary(words, {"פטריותים": "פטריוטים"})
    assert [w.text for w in fixed] == ["הפטריוטים", "בפטריוטים,", "פטריוטים.", "שלום"]
    assert [(w.start, w.end) for w in fixed] == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_glossary_leaves_unrelated_words_and_does_not_strip_real_prefix_letters():
    import transcript as tr
    words = [Word("בית", 0, 1), Word("ליברמן", 1, 2)]
    fixed = tr.apply_glossary(words, {"ית": "X", "ליברמן": "ליברמן"})
    assert [w.text for w in fixed] == ["בית", "ליברמן"]


# ---- face-aware crop: pure logic (detection itself is exercised on real video)

def test_crop_fraction_centers_the_face_and_clamps_at_the_edges():
    import facecrop
    aspect = 16 / 9
    mid = facecrop.crop_fraction(0.5, aspect)
    assert mid == pytest.approx(0.5, abs=0.01)
    assert facecrop.crop_fraction(0.02, aspect) == 0.0
    assert facecrop.crop_fraction(0.99, aspect) == 1.0
    # a face a bit right of centre needs the crop window a bit right of centre
    assert 0.5 < facecrop.crop_fraction(0.62, aspect) < 0.8


def test_choose_subject_prefers_the_face_whose_mouth_moves_then_the_bigger_face():
    import facecrop
    clusters = [
        facecrop.FaceCluster(cx=0.3, width=0.10, frames=20, motion=5.0),
        facecrop.FaceCluster(cx=0.7, width=0.09, frames=20, motion=40.0),
    ]
    assert facecrop.choose_subject(clusters, total_frames=20).cx == 0.7
    still = [facecrop.FaceCluster(0.3, 0.12, 20, 1.0), facecrop.FaceCluster(0.7, 0.08, 20, 1.0)]
    assert facecrop.choose_subject(still, total_frames=20).cx == 0.3
    assert facecrop.choose_subject([], total_frames=20) is None


def test_steps_merge_neighbours_with_the_same_crop_and_swallow_flicker_shots():
    import facecrop
    shots = [(0.0, 2.0, 0.60), (2.0, 2.2, 0.15), (2.2, 5.0, 0.60), (5.0, 8.0, 0.20)]
    steps = facecrop.steps_from_shots(shots, min_shot_s=0.4)
    assert steps == [(0.0, 0.60), (5.0, 0.20)]


def test_faceless_shots_borrow_the_previous_crop_and_a_leading_one_borrows_the_next():
    import facecrop
    shots = [(0.0, 1.0, None), (1.0, 3.0, 0.4), (3.0, 4.0, None)]
    assert facecrop.fill_faceless(shots) == [(0.0, 1.0, 0.4), (1.0, 3.0, 0.4), (3.0, 4.0, 0.4)]
    assert facecrop.fill_faceless([(0.0, 2.0, None)]) == [(0.0, 2.0, 0.5)]


def test_crop_expression_is_a_nested_time_switch():
    expr = render.crop_x_expression([(0.0, 0.6), (3.2, 0.2), (5.0, 0.9)])
    assert expr == "(iw-1080)*if(lt(t,3.2),0.6,if(lt(t,5),0.2,0.9))"
    assert render.crop_x_expression([(0.0, 0.25)]) == "(iw-1080)*0.25"


def test_ffmpeg_graph_accepts_time_steps_as_the_crop_of_a_segment():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(1.0, 9.0)], ass_name="c.ass", out_name="o.mp4",
                                  reframe="center", crop_x=[[(0.0, 0.6), (3.2, 0.2)]])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "x='(iw-1080)*if(lt(t,3.2),0.6,0.2)'" in graph


# ---- caption timing and look

def _w(text, start, end):
    return Word(text, start, end)


def test_blocks_never_end_on_a_dangling_function_word():
    words = [_w("אני", 0.0, 0.3), _w("לא", 0.3, 0.6), _w("רוצה", 0.6, 1.0), _w("את", 1.0, 1.2), _w("זה", 1.2, 1.5)]
    blocks = captions.make_blocks(words, 2)
    assert [[x.text for x in b.words] for b in blocks][0][-1] not in captions.DANGLING
    for b in blocks[:-1]:
        assert b.words[-1].text not in captions.DANGLING


def test_blocks_break_at_a_pause_even_when_not_full():
    words = [_w("שלום", 0.0, 0.4), _w("עולם", 1.5, 1.9), _w("יפה", 1.9, 2.2)]
    blocks = captions.make_blocks(words, 3)
    assert [len(b.words) for b in blocks] == [1, 2]


def test_gaps_between_blocks_are_closed_and_short_blocks_stay_long_enough():
    words = [_w("אחד", 0.0, 0.15), _w("שניים", 0.7, 1.2), _w("שלושה", 1.2, 1.7), _w("ארבעה", 3.7, 4.2)]
    blocks = captions.make_blocks(words, 1)
    assert blocks[0].end >= blocks[1].start - 1e-6          # 0.49 s gap before the lead-in is closed
    assert blocks[0].end - blocks[0].start >= captions.MIN_BLOCK_S - 1e-6
    assert blocks[2].end < blocks[3].start                  # a real pause stays empty
    assert all(a.end <= b.start + 1e-6 for a, b in zip(blocks, blocks[1:]))  # never overlap


def test_captions_appear_slightly_before_the_voice_and_with_a_soft_pop():
    words = [_w("שלום", 1.0, 1.4), _w("עולם", 1.4, 1.9)]
    blocks = captions.make_blocks(words, 2)
    assert blocks[0].start == pytest.approx(1.0 - captions.LEAD_IN_S)
    ass = captions.build_ass(blocks, preset="social")
    line = next(x for x in ass.splitlines() if x.startswith("Dialogue: 0"))
    assert r"{\fad(" in line and r"\t(" in line


# ---- continuity checks for joined parts

def _sentence_transcript():
    texts = ["אבל", "זה", "לא", "נכון.", "אני", "חושב", "שכן", "כי", "הם", "באו", "אתמול.", "ואז", "הוא", "הלך"]
    return Transcript(words=[Word(x, i * 0.5, i * 0.5 + 0.4) for i, x in enumerate(texts)], language="he", engine="t")


def test_check_parts_warns_about_a_connector_opening_and_a_mid_sentence_end():
    t = _sentence_transcript()
    warnings = clipplan.check_parts(t, [(0, 3), (4, 8)])
    text = " ".join(warnings)
    assert "אבל" in text          # opens with a connector word
    assert "חלק 2" in text        # part 2 ends on "הם" without a sentence end


def test_check_parts_is_quiet_for_clean_sentence_boundaries():
    t = _sentence_transcript()
    assert clipplan.check_parts(t, [(4, 10)]) == []


def test_check_parts_flags_a_very_short_part():
    t = _sentence_transcript()
    assert any("קצר" in w for w in clipplan.check_parts(t, [(4, 4), (5, 10)]))


# ---- a downloaded video must survive cleanup, and its fingerprint must not depend on the file time

def test_cleanup_keeps_a_downloaded_video_in_a_visible_source_folder(tmp_path):
    import argparse
    from pathlib import Path

    import make_shorts
    import workspace

    ws = workspace.Workspace.create(tmp_path)
    media = ws.path / "download" / "video.mp4"
    media.parent.mkdir()
    media.write_bytes(b"x" * 10)
    make_shorts.ProjectState(project=tmp_path, media_path=str(media), workspace=str(ws.path)).save()
    make_shorts.cmd_cleanup(argparse.Namespace(project=str(tmp_path)))
    loaded = make_shorts.ProjectState.load(tmp_path)
    kept = Path(loaded.media_path)
    assert kept.exists() and (tmp_path / "source") in kept.parents
    assert not ws.path.exists()


def test_fingerprint_ignores_the_modification_time(tmp_path):
    import os

    import make_shorts

    f = tmp_path / "a.bin"
    f.write_bytes(b"hello" * 1000)
    before = make_shorts._fingerprint(f)
    os.utime(f, (1_000_000_000, 1_000_000_000))
    assert make_shorts._fingerprint(f) == before


# ---- the key step: prepare the file, and report presence without ever showing the value

def test_prepare_key_file_adds_an_empty_key_line_and_keeps_existing_lines(config_home):
    import config

    config.config_dir().mkdir(parents=True)
    (config.config_dir() / ".env").write_text("SETUP_COMPLETE=true\n", encoding="utf-8")
    path = config.prepare_key_file("GROQ_API_KEY")
    text = path.read_text(encoding="utf-8")
    assert "SETUP_COMPLETE=true" in text and "GROQ_API_KEY=" in text
    again = config.prepare_key_file("GROQ_API_KEY").read_text(encoding="utf-8")
    assert again.count("GROQ_API_KEY=") == 1


def test_key_present_is_false_for_an_empty_line_and_true_once_filled(config_home, monkeypatch):
    import config

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config.prepare_key_file("GROQ_API_KEY")
    assert config.has_setting("GROQ_API_KEY") is False
    (config.config_dir() / ".env").write_text("GROQ_API_KEY=abc123\n", encoding="utf-8")
    assert config.has_setting("GROQ_API_KEY") is True


def test_setup_status_reports_key_presence_without_the_value(config_home, monkeypatch, capsys):
    import json

    import make_shorts

    monkeypatch.setenv("GROQ_API_KEY", "super-secret-value")
    code = make_shorts.main(["setup", "--json", "--skip-selftest"])
    out = capsys.readouterr().out
    assert json.loads(out)["key_present"] is True
    assert "super-secret-value" not in out


def test_captions_never_carry_over_a_join():
    from captions import make_blocks
    from transcript import Word

    words = [Word("שלום", 0.0, 0.4), Word("לכולם", 0.45, 0.9), Word("אז", 1.0, 1.3), Word("מה", 1.35, 1.7)]
    blocks = make_blocks(words, 4, breaks=[0.95])
    assert [[w.text for w in b.words] for b in blocks] == [["שלום", "לכולם"], ["אז", "מה"]]
    assert blocks[0].end <= 0.95 + 1e-6
    # the caption after the join starts at the join, not before it, and stays up while its words are spoken
    assert blocks[1].start >= 0.95 - 1e-6
    assert blocks[1].end >= 1.7 - 1e-6


def test_parser_knows_recaption_and_recaptioned():
    import make_shorts

    args = make_shorts.build_parser().parse_args(["recaption", "--clips", "1,2"])
    assert args.clips == "1,2"
    args = make_shorts.build_parser().parse_args(["render", "--recaptioned"])
    assert args.recaptioned is True


def test_finish_sentences_extends_to_punctuation_or_pause_without_entering_next_part():
    words = [Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(["א", "ב", "ג", "ד.", "ה", "ו"])]
    t = Transcript(words=words, language="he", engine="x")
    assert clipplan.finish_sentences(t, [(0, 1)]) == [(0, 3)]
    assert clipplan.finish_sentences(t, [(0, 1), (3, 4)]) == [(0, 2), (3, 5)]
    pause = [Word("א", 0, 0.2), Word("ב", 0.3, 0.5), Word("ג", 1.5, 1.7)]
    assert clipplan.finish_sentences(Transcript(words=pause, language="he", engine="x"), [(0, 0)]) == [(0, 1)]


def test_cut_frames_finds_the_exact_frame_of_each_camera_cut():
    import facecrop

    diffs = [0.0, 2.0, 1.5, 40.0, 3.0, 2.0, 2.5, 35.0, 1.0]
    assert facecrop.cut_frames(diffs, threshold=18.0) == [3, 7]


def test_cut_time_is_half_a_frame_before_the_first_frame_of_the_new_shot():
    import facecrop

    # frame 3 at 25 fps starts at 0.12 s; the crop must switch between frame 2 and frame 3
    assert facecrop.cut_times([3, 7], fps=25.0) == pytest.approx([0.10, 0.26])


def test_trim_flashes_drops_a_few_frames_of_another_shot_at_either_edge():
    import facecrop

    start, end, cuts = facecrop.trim_flashes(100.0, 110.0, [0.08, 4.0, 9.9], margin=0.25)
    assert start == pytest.approx(100.08)
    assert end == pytest.approx(109.9)
    assert cuts == pytest.approx([3.92])


def test_trim_flashes_leaves_segments_without_edge_cuts_alone():
    import facecrop

    assert facecrop.trim_flashes(5.0, 9.0, [2.0], margin=0.25) == (5.0, 9.0, [2.0])


def test_shot_index_for_sample_times_uses_exact_cuts():
    import facecrop

    assert facecrop.shot_of([0.0, 0.1, 0.2, 0.3], [0.15]) == [0, 0, 1, 1]


def test_segments_cut_at_the_quietest_point_of_the_waveform_between_words():
    words = [Word("א", 0.0, 1.0), Word("ב", 1.2, 2.0), Word("ג", 2.6, 3.0), Word("ד", 3.1, 4.0)]
    t = Transcript(words=words, language="he", engine="x")
    asked = []

    def quietest(lo, hi):
        asked.append((round(lo, 3), round(hi, 3)))
        return lo + 0.1

    segs = clipplan.segments_from_parts(t, [(1, 1)], quietest=quietest)
    # start window: from the previous word's end to the first word's start; end window: to the next word's start
    assert asked == [(1.0, 1.25), (1.95, 2.6)]
    assert segs == [(1.1, 2.05)]


def test_quietest_point_finds_the_dip_in_an_energy_profile():
    from audio import quietest_in

    energy = [5.0, 4.0, 0.5, 0.4, 3.0, 6.0]  # one value per 10 ms
    assert quietest_in(energy, start=10.0, step=0.01) == pytest.approx(10.03, abs=0.011)


def test_hint_leak_is_detected_when_the_prompt_shows_up_in_the_transcript():
    import make_shorts

    hint = "ערוץ 14, ידיעות אחרונות, סמוטריץ', בן גביר, חרדים"
    leaked = [Word(x, 0, 0) for x in "יכול להיות ש ערוץ 14, ידיעות אחרונות, פליישמן".split()]
    clean = [Word(x, 0, 0) for x in "בלי ערוץ 14 המחנה הלאומי לא היה שורד".split()]
    assert make_shorts.hint_leaked(leaked, hint) is True
    assert make_shorts.hint_leaked(clean, hint) is False


def test_punch_in_alternates_zoom_only_after_joins_inside_the_same_shot():
    import facecrop

    # joins: after seg0 same shot, after seg1 a camera change, after seg2 same shot again
    assert facecrop.punch_in_zooms([True, False, True]) == [1.0, 1.08, 1.08, 1.0]
    assert facecrop.punch_in_zooms([]) == [1.0]
    assert facecrop.punch_in_zooms([True, True]) == [1.0, 1.08, 1.0]


def test_zoomed_segment_scales_up_and_keeps_the_head_in_frame():
    graph = _graph(segments=[(0.0, 2.0), (3.0, 5.0)], reframe="center", crop_x=[0.5, 0.4], zoom=[1.0, 1.08])
    assert "[0:v]setpts=PTS-STARTPTS,scale=-2:1920,crop=1080:1920:" in graph
    assert "[1:v]setpts=PTS-STARTPTS,scale=-2:2074,crop=1080:1920:" in graph
    assert "(ih-1920)*0.3" in graph


def test_merge_holes_fills_a_truncated_retranscription_from_the_old_words():
    import make_shorts

    new = [Word("א", 0.1, 0.5), Word("ב", 0.6, 1.0), Word("תודה", 1.2, 1.4), Word("רבה.", 1.4, 1.6)]
    old = [Word("x", 0.1, 0.5), Word("y", 0.6, 1.0), Word("ג", 1.2, 1.6), Word("ד", 4.0, 4.5), Word("ה", 6.0, 6.5)]
    merged = make_shorts.merge_holes(new, old, duration=7.0)
    assert [w.text for w in merged] == ["א", "ב", "ג", "ד", "ה"]


def test_merge_holes_keeps_a_complete_retranscription():
    import make_shorts

    new = [Word("א", 0.1, 0.5), Word("ב", 0.6, 1.0)]
    assert make_shorts.merge_holes(new, [Word("x", 0.1, 0.5)], duration=1.5) == new


def test_edge_search_reaches_before_a_word_with_no_pause_before_it():
    words = [Word("א", 0.0, 1.0), Word("ב", 1.0, 2.0), Word("ג", 2.0, 3.0)]
    t = Transcript(words=words, language="he", engine="x")
    asked = []
    clipplan.segments_from_parts(t, [(1, 1)], quietest=lambda lo, hi: asked.append((lo, hi)) or lo)
    assert asked[0][0] == pytest.approx(0.85)
    assert asked[1][1] == pytest.approx(2.15)


def test_a_join_between_similar_framings_counts_as_a_jump_even_above_the_cut_threshold():
    import facecrop

    assert facecrop.looks_like_jump(1.5) is True     # the very same shot
    assert facecrop.looks_like_jump(18.1) is True    # same person, slightly different framing (clip 2)
    assert facecrop.looks_like_jump(61.0) is False   # a real change of camera


def test_snap_joins_moves_the_picture_change_to_a_camera_cut_just_before_the_join():
    import facecrop

    video = facecrop.snap_joins([(10.0, 20.0), (30.0, 40.0)], [[9.4], []], reach=1.0)
    assert [x for s in video for x in s] == pytest.approx([10.0, 19.4, 29.4, 40.0])


def test_snap_joins_moves_the_picture_change_to_a_camera_cut_just_after_the_join():
    import facecrop

    video = facecrop.snap_joins([(10.0, 20.0), (30.0, 40.0)], [[], [0.5]], reach=1.0)
    assert [x for s in video for x in s] == pytest.approx([10.0, 20.5, 30.5, 40.0])


def test_snap_joins_leaves_joins_far_from_any_camera_cut():
    import facecrop

    segs = [(10.0, 20.0), (30.0, 40.0)]
    assert facecrop.snap_joins(segs, [[5.0], [3.0]], reach=1.0) == segs


def test_video_segments_are_trimmed_separately_from_the_audio():
    graph_cmd = render.build_ffmpeg_cmd(
        "in.mp4", [(10.0, 20.0), (30.0, 40.0)], ass_name="c.ass", out_name="o.mp4", reframe="center",
        crop_x=[0.5, 0.5], video_segments=[(10.0, 19.4), (29.4, 40.0)],
    )
    graph = graph_cmd[graph_cmd.index("-filter_complex") + 1]
    assert "[1:v]trim=start=0:end=10.6,setpts=PTS-STARTPTS" in graph
    assert "[1:a]atrim=start=0.6:end=10.6,asetpts=PTS-STARTPTS" in graph
    assert "29.400" in graph_cmd and "10.600" in graph_cmd
    # picture and sound are joined by separate concats: one concat would start each part after
    # the longer of its two streams, freezing the picture and pushing it out of sync
    assert "[v0][v1]concat=n=2:v=1:a=0[vc]" in graph
    assert "[a0][a1]concat=n=2:v=0:a=1[ac]" in graph


def _write_av(path, colours, fps=25):
    import subprocess

    inputs, labels = [], ""
    for i, (colour, seconds) in enumerate(colours):
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r={fps}:d={seconds}"]
        labels += f"[{i}:v]"
    total = sum(s for _, s in colours)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-f", "lavfi", "-i", f"sine=d={total}",
                    "-filter_complex", f"{labels}concat=n={len(colours)}:v=1[v]", "-map", "[v]",
                    "-map", f"{len(colours)}:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(path)], check=True)


def _duration(path, stream):
    import subprocess

    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "stream=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def test_an_l_cut_render_keeps_picture_and_sound_the_same_length(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    src = tmp_path / "src.mp4"
    _write_av(src, [("red", 4), ("blue", 4)])
    (tmp_path / "c.ass").write_text(captions.build_ass([], preset="social"), encoding="utf-8")
    (tmp_path / "fonts").mkdir()
    cmd = render.build_ffmpeg_cmd(
        str(src), [(0.0, 3.0), (5.0, 8.0)], ass_name="c.ass", out_name="o.mp4", reframe="blur",
        video_segments=[(0.0, 2.5), (4.5, 8.0)],
    )
    subprocess.run(cmd, cwd=tmp_path, check=True)
    out = tmp_path / "o.mp4"
    assert _duration(out, "v:0") == pytest.approx(6.0, abs=0.08)
    assert _duration(out, "a:0") == pytest.approx(6.0, abs=0.08)


def test_motion_check_flags_short_shots_and_jumps_but_not_edge_crops():
    import qa

    findings = qa.motion_findings(
        cuts=[2.0, 2.6, 10.0], joins=[(6.0, True), (8.0, False)], duration=12.0,
        crop_steps=[(0.0, 0.5), (10.0, 0.99)],
    )
    text = " ".join(findings)
    assert "2.0" in text and "0.6" in text      # a 0.6 s shot at 2.0 s
    assert "6.0" in text and "קפיצה" in text     # a join that looks like a jump
    assert "8.0" not in text
    assert "קצה" not in text                     # an edge crop still keeps the face in frame: no warning
    assert qa.motion_findings(cuts=[4.0], joins=[], duration=8.0, crop_steps=[(0.0, 0.5)]) == []
    # a join a frame or two off a real camera cut is a change of picture, not a jump
    # (seen in a real clip: the cut at 22.14, the join counted at 22.2)
    assert qa.motion_findings(cuts=[22.14], joins=[(22.2, True)], duration=40.0, crop_steps=[]) == []


def test_a_joined_part_under_three_seconds_is_flagged():
    words = [Word(f"w{i}.", i * 0.5, i * 0.5 + 0.4) for i in range(40)]
    t = Transcript(words=words, language="he", engine="x")
    warnings = clipplan.check_parts(t, [(0, 4), (10, 30)])  # first part is 2.4 s
    assert any("חלק 1" in w and "קצר" in w for w in warnings)
    assert not any("חלק 2" in w and "קצר" in w for w in warnings)


def test_still_intervals_are_long_shots_with_little_motion():
    import facecrop

    shots = [(0.0, 2.0, 4.5), (2.0, 4.5, 1.2), (4.5, 5.0, 1.0), (5.0, 9.0, 2.9)]
    assert facecrop.still_intervals(shots) == [(2.0, 4.5), (5.0, 9.0)]


def test_push_expression_zooms_slowly_inside_each_still_interval_and_resets_after():
    assert render.push_expression([(2.0, 4.5)]) == "1+if(between(t,2,4.5),min(0.03*(t-2),0.1),0)"
    assert render.push_expression([]) == "1"
    assert render.push_expression([(2.0, 4.5)], var="in/25") == "1+if(between(in/25,2,4.5),min(0.03*(in/25-2),0.1),0)"


def test_push_in_is_applied_only_to_segments_with_still_shots():
    graph = _graph(segments=[(0.0, 6.0), (10.0, 14.0)], reframe="center", crop_x=[0.5, 0.5],
                   push=[[(2.0, 4.5)], []])
    v0 = graph.split("[v0]")[0]
    # sub-pixel zoom: measured on a still frame, resizing in whole pixels stutters (frames that do not
    # change, then a jump); perspective interpolates every frame evenly
    assert "perspective=" in v0 and "interpolation=linear:eval=frame" in v0
    assert "between(in/25,2,4.5)" in v0
    v1 = graph.split("[v1]")[0].split(";")[-1]
    assert "eval=frame" not in v1


def test_trim_flashes_takes_separate_head_and_tail_margins():
    import facecrop

    start, end, cuts = facecrop.trim_flashes(100.0, 110.0, [0.7, 4.0, 9.9], head=1.0, tail=0.0)
    assert start == pytest.approx(100.7) and end == pytest.approx(110.0)
    assert cuts == pytest.approx([3.3, 9.2])


def test_exact_cuts_finds_the_right_frame_when_the_start_is_between_frames(tmp_path):
    import shutil
    import subprocess

    import facecrop

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg missing")
    video = tmp_path / "cut.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x180:r=25:d=1",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=1",
                    "-f", "lavfi", "-i", "sine=d=2",  # an audio track is what makes ffmpeg pad the first frame
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)], check=True)
    # frames sit at 0.00, 0.04, ...; starting at 0.53 the first kept frame is 0.56, the blue one is 1.00
    cuts = facecrop.exact_cuts(video, 0.53, 1.6, 25.0)
    assert cuts == pytest.approx([(11 - 0.5) / 25])
    # counted from `start` instead of from the first frame: the edge can be moved there without
    # keeping the last red frame (0.96 s)
    absolute = facecrop.exact_cuts(video, 0.53, 1.6, 25.0, absolute=True)
    assert absolute == pytest.approx([0.45])
    new_start = 0.53 + absolute[0]
    frame = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{new_start:.3f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=1:1,format=rgb24", "-f", "rawvideo", "-"],
                           capture_output=True, check=True).stdout
    assert frame[2] > frame[0]  # blue, not red


def test_outer_edges_are_not_trimmed_into_speech():
    import facecrop

    # cuts 0.6 s into the first word and 0.2 s before the end of the last word; no silence to grow into
    words = [Word("שלום", 10.25, 10.6), Word("לכולם", 10.7, 11.0), Word("ביי", 19.2, 19.9), Word("x", 20.1, 20.4)]
    assert facecrop.settle_edges(10.0, 20.0, [10.6, 19.8], words) == (10.0, 20.0)
    # without word timing it trims to the cuts, as before
    assert facecrop.settle_edges(10.0, 20.0, [10.6, 19.8], None) == pytest.approx((10.6, 19.8))


def test_trimming_never_leaves_an_empty_segment():
    import facecrop

    start, end, _ = facecrop.trim_flashes(0.0, 1.6, [0.9], head=1.0, tail=1.0)
    assert end - start >= 0.8


def test_waveform_search_is_skipped_when_numpy_is_missing(monkeypatch):
    import sys

    import audio

    monkeypatch.setitem(sys.modules, "numpy", None)
    assert audio.waveform_quietest("x.mp4") is None


def test_a_real_closing_thanks_is_kept_when_the_old_transcript_has_it_too():
    import make_shorts

    new = [Word("א", 0.1, 0.5), Word("ב", 0.6, 1.0), Word("תודה.", 1.2, 1.5)]
    old = [Word("א", 0.1, 0.5), Word("ב", 0.6, 1.0), Word("תודה", 1.2, 1.5)]
    assert [w.text for w in make_shorts.merge_holes(new, old, duration=1.6)] == ["א", "ב", "תודה."]
    # an invented one, where the original has no speech, is still dropped
    old_short = old[:2]
    assert [w.text for w in make_shorts.merge_holes(new, old_short, duration=1.6)] == ["א", "ב"]


def test_retime_words_moves_clip_captions_to_new_segment_edges():
    words = [Word("א", 0.5, 0.9), Word("ב", 2.2, 2.6), Word("ג", 5.5, 5.9)]
    # old clip: 10-13 and 20-24; new clip starts 0.4 s later and the second part 1 s earlier
    moved = captions.retime_words(words, [(10.0, 13.0), (20.0, 24.0)], [(10.4, 13.0), (19.0, 24.0)])
    assert [(w.text, round(w.start, 2)) for w in moved] == [("א", 0.1), ("ב", 1.8), ("ג", 6.1)]
    # a word that falls outside the new edges is dropped
    # a word whose middle (10.7) is cut off is dropped; one cut a hair inside keeps its caption
    gone = captions.retime_words(words, [(10.0, 13.0), (20.0, 24.0)], [(10.8, 13.0), (20.0, 24.0)])
    assert [w.text for w in gone] == ["ב", "ג"]


def test_clip_words_are_saved_with_their_segments_and_retimed_on_load(tmp_path):
    import make_shorts

    make_shorts.save_clip_words(tmp_path, 3, [Word("א", 0.5, 0.9)], [(10.0, 13.0)])
    loaded = make_shorts._load_clip_words(tmp_path, 3, [(10.2, 13.0)], fallback_segments=None)
    assert [(w.text, round(w.start, 2)) for w in loaded] == [("א", 0.3)]


def test_recaption_rejects_a_broken_glossary_in_hebrew(tmp_path):
    import make_shorts

    bad = tmp_path / "g.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(UserFacingError):
        make_shorts.read_glossary(bad)


def test_every_segment_restarts_its_clock_so_crop_times_match_frame_counts():
    graph = _graph(segments=[(2.013, 6.0)], reframe="center", crop_x=[[(0.0, 0.2), (1.5, 0.8)]])
    assert graph.startswith("[0:v]setpts=PTS-STARTPTS,")


def test_fast_motion_is_not_mistaken_for_a_camera_cut():
    import facecrop

    burst = [0.0, 2.0, 15.0, 19.0, 24.0, 19.0, 12.0, 2.0]   # a hand moving fast over several frames
    assert facecrop.cut_frames(burst) == []
    assert facecrop.cut_frames([0.0, 1.0, 1.3, 85.0, 0.9, 1.1]) == [3]   # a real cut stands alone
