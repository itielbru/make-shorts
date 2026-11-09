"""Second code review: words lost at edges, sound repeated at joins, an empty picture segment,
a teaser from cut material, drift over joins, false alarms and silent failures."""
import pytest

import captions
import clipplan
import facecrop
import qa
import render
from transcript import Transcript, Word


# 1. a word that is heard must be captioned, even when the cut lands a hair inside it

def test_a_word_cut_a_hair_inside_its_timing_keeps_its_caption():
    words = [Word("ב", 0.6, 1.0), Word("ג.", 1.1, 1.5)]
    assert [w.text for w in captions.remap_words(words, [(0.465, 1.465)])] == ["ב", "ג."]
    assert [w.text for w in captions.remap_words(words, [(0.635, 1.635)])] == ["ב", "ג."]


def test_a_word_mostly_outside_the_segment_is_left_out():
    words = [Word("לפני", 0.0, 0.5), Word("בפנים", 0.6, 1.0)]
    assert [w.text for w in captions.remap_words(words, [(0.4, 1.2)])] == ["בפנים"]


# 2. parts that finish_sentences makes adjacent are one segment, not two overlapping ones

def test_adjacent_parts_become_one_segment():
    words = [Word("א", 0.0, 0.5), Word("ב", 0.6, 1.0), Word("ג", 1.1, 1.5), Word("ד", 1.5, 2.0), Word("ה.", 2.1, 2.6)]
    t = Transcript(words=words, language="he", engine="x")
    assert len(clipplan.segments_from_parts(t, [(0, 2), (3, 4)])) == 1


# 3. snapping joins never empties a short middle part's picture

def test_snap_joins_keeps_a_short_middle_part_visible():
    video = facecrop.snap_joins([(10.0, 20.0), (30.0, 31.8), (40.0, 50.0)], [[], [0.9], []])
    a, b = video[1]
    assert b - a >= 0.5
    assert sum(e - s for s, e in video) == pytest.approx(10.0 + 1.8 + 10.0)


# 4. picture and sound of each part last the same whole number of frames

def test_segments_are_aligned_to_whole_frames_so_joins_do_not_drift():
    segs = clipplan.frame_aligned([(10.013, 12.4567), (20.0, 21.0333)], fps=25.0)
    for (a, b), (oa, _) in zip(segs, [(10.013, 0), (20.0, 0)]):
        assert a == pytest.approx(oa)
        assert round((b - a) * 25, 6) == pytest.approx(round((b - a) * 25))


def test_l_cut_picture_edges_stay_whole_frames_and_match_the_sound_total():
    audio = [(10.0, 20.013), (30.0, 35.0)]
    video = [(10.0, 19.413), (29.4, 35.0)]          # the picture switches 0.6 s before the join
    aligned = clipplan.frame_aligned(audio, 25.0)
    pic = clipplan.frame_aligned_video(aligned, audio, video, 25.0)
    assert sum(b - a for a, b in pic) == pytest.approx(sum(b - a for a, b in aligned))
    for a, b in pic:
        assert (b - a) * 25 == pytest.approx(round((b - a) * 25))


# 5. a teaser quote must come from the parts the clip plays

def test_a_quote_from_a_gap_between_parts_is_refused():
    words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(40)]
    t = Transcript(words=words, language="he", engine="x")
    assert clipplan.quote_in_parts(t, [(0, 9), (20, 29)], "w3 w4 w5")
    assert not clipplan.quote_in_parts(t, [(0, 9), (20, 29)], "w12 w13 w14")


# 6. checks that the source can trip say so, instead of always blaming the render

def test_repeated_frames_are_reported_as_worth_checking_against_the_source():
    text = " ".join(qa.render_findings(video_s=10.0, audio_s=10.0, duplicate_runs=[(3.2, 4)]))
    assert "diagnose" in text


def test_render_output_is_constant_frame_rate_at_the_source_rate():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(0.0, 4.0)], ass_name="c.ass", reframe="center", out_name="o.mp4",
                                  fps=29.97)
    assert cmd[cmd.index("-fps_mode") + 1] == "cfr" and cmd[cmd.index("-r") + 1] == "29.97"


# 7. a check that cannot read the file says so

def test_unreadable_timestamps_are_a_finding_not_a_pass():
    assert qa.sound_timestamp_findings([], 1024 / 48000)
    assert qa.picture_timestamp_findings([], 1 / 25)


# 8. --hide-bottom is range-checked

def test_hide_bottom_out_of_range_is_refused():
    import make_shorts

    with pytest.raises(SystemExit):
        make_shorts.build_parser().parse_args(["render", "--hide-bottom", "0.7"])
