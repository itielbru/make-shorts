"""Improvements drawn from a real run: source quality, render self-checks, camera-aware edges,
preferences in the profile, a diagnose command, and tests that render for real."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import acquire
import captions
import facecrop
import profiles
import qa
import render
from transcript import Word

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


# --- 1. source quality --------------------------------------------------------------------------

def _info(width, height, bit_rate, fps=25.0):
    return acquire.MediaInfo(path=Path("v.mp4"), title="t", duration=60.0, width=width, height=height,
                             has_video=True, has_audio=True, bit_rate=bit_rate, fps=fps)


def test_a_720p_source_is_warned_about_before_any_work():
    warning = acquire.quality_warning(_info(1280, 720, 1_000_000))
    assert warning and "720" in warning and "1080" in warning


def test_a_heavily_compressed_1080p_source_is_warned_about():
    assert acquire.quality_warning(_info(1920, 1080, 400_000)) is not None


def test_youtubes_own_1080p_of_a_calm_talk_show_passes():
    # 1.07 Mbps H.264: looked sharp when cropped to vertical, so no warning
    assert acquire.quality_warning(_info(1920, 1080, 1_070_287)) is None


def test_a_good_1080p_source_passes_quietly():
    assert acquire.quality_warning(_info(1920, 1080, 4_300_000)) is None
    assert acquire.quality_warning(_info(1920, 1080, 0)) is None  # bitrate unknown: judge by size only


# --- 2. render self-checks ----------------------------------------------------------------------

def test_render_check_flags_picture_and_sound_of_different_length():
    text = " ".join(qa.render_findings(video_s=6.48, audio_s=6.0, duplicate_runs=[]))
    assert "6.5" in text and "6.0" in text


def test_render_check_flags_repeated_frames():
    text = " ".join(qa.render_findings(video_s=10.0, audio_s=10.0, duplicate_runs=[(3.2, 4)]))
    assert "3.2" in text and "4" in text


def test_render_check_is_quiet_for_a_clean_clip():
    assert qa.render_findings(video_s=62.12, audio_s=62.2, duplicate_runs=[]) == []


def test_duplicate_runs_are_found_in_a_list_of_frame_differences():
    diffs = [3.0, 2.0, 0.0, 0.0, 0.0, 2.5, 0.01, 3.0]  # diffs[k]: frame k+1 against frame k
    assert qa.duplicate_runs(diffs, fps=25.0, min_run=2) == [(3 / 25, 3)]


# --- 3. camera-aware edges ----------------------------------------------------------------------

def test_a_short_first_shot_is_avoided_by_starting_earlier_in_silence():
    # speech starts at 100.2; a camera cut at 100.6 would leave a 0.6 s first shot;
    # another cut at 99.3 sits in silence before the first word: start there instead
    words = [Word("קודם.", 97.0, 98.5), Word("שלום", 100.2, 100.5), Word("לכולם", 100.7, 101.2)]
    start, end = facecrop.settle_edges(100.0, 130.0, [99.3, 100.6, 115.0], words)
    assert start == pytest.approx(99.3) and end == pytest.approx(130.0)


def test_a_short_last_shot_is_avoided_by_ending_later_in_silence():
    words = [Word("סוף.", 129.0, 129.6), Word("אחר", 132.0, 132.4)]
    start, end = facecrop.settle_edges(100.0, 130.0, [129.5, 130.8], words)
    assert end == pytest.approx(130.8)


def test_an_edge_is_never_moved_over_a_word():
    # the earlier cut at 99.5 would pull in the word at 99.6-99.9 from the sentence before
    words = [Word("לפני", 99.6, 99.9), Word("שלום", 100.2, 100.5)]
    start, _ = facecrop.settle_edges(100.0, 130.0, [99.5, 100.6], words)
    assert start == pytest.approx(100.0)


def test_a_cut_before_the_first_word_is_still_trimmed_to():
    words = [Word("שלום", 100.5, 100.9)]
    start, _ = facecrop.settle_edges(100.0, 130.0, [100.3], words)
    assert start == pytest.approx(100.3)


def test_an_end_overlapping_the_next_word_is_cut_in_the_real_pause_before_it():
    import clipplan
    from transcript import Transcript

    # seen in a real clip: "בתקשורת." timed 1800.92-1801.94 while the next word starts at 1801.54;
    # the sound shows the pause at ~1801.45, before the next word, not after the bad end time
    words = [Word("ולא", 1800.82, 1800.92), Word("בתקשורת.", 1800.92, 1801.94), Word("חוג", 1801.54, 1801.74)]
    t = Transcript(words=words, language="he", engine="x")
    windows = []

    def quietest(lo, hi):
        windows.append((lo, hi))
        return 1801.45 if lo <= 1801.45 <= hi else (lo + hi) / 2

    (_, end), = clipplan.segments_from_parts(t, [(0, 1)], quietest=quietest)
    assert end == pytest.approx(1801.45)
    assert windows[-1][1] <= 1801.54 + 0.05 + 1e-6


# --- 4. preferences in the profile --------------------------------------------------------------

def test_profile_holds_the_look_preferences_with_the_agreed_defaults():
    p = profiles.Profile(name="x", language="he")
    assert p.slow_zoom is False and p.fade_to_black is False and p.punch_in == pytest.approx(1.08)


def test_profile_rejects_a_punch_in_that_shrinks_or_explodes_the_picture(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"name": "x", "language": "he", "punch_in": 0.9}), encoding="utf-8")
    with pytest.raises(Exception):
        profiles.load_profile(path)


def test_fade_to_black_is_only_added_when_asked():
    plain = render.build_ffmpeg_cmd("in.mp4", [(0.0, 4.0)], ass_name="c.ass", reframe="center", out_name="o.mp4")
    faded = render.build_ffmpeg_cmd("in.mp4", [(0.0, 4.0)], ass_name="c.ass", reframe="center", out_name="o.mp4",
                                    fade_to_black=True)
    assert ",fade=t=out" not in plain[plain.index("-filter_complex") + 1]
    assert ",fade=t=out" in faded[faded.index("-filter_complex") + 1]


def test_punch_in_zooms_take_the_strength_from_the_caller():
    assert facecrop.punch_in_zooms([True], strength=1.2) == [1.0, 1.2]


# --- 5. diagnose --------------------------------------------------------------------------------

def test_clip_time_maps_back_to_the_source():
    segments = [(100.0, 110.0), (200.0, 230.0)]
    assert captions.clip_to_source(5.0, segments) == pytest.approx(105.0)
    assert captions.clip_to_source(12.0, segments) == pytest.approx(202.0)
    assert captions.clip_to_source(99.0, segments) is None


def test_parser_knows_diagnose():
    import make_shorts

    args = make_shorts.build_parser().parse_args(["diagnose", "--clip", "5", "--at", "41.5"])
    assert args.clip == 5 and args.at == pytest.approx(41.5)


def test_a_missing_clip_never_passes_the_render_check(tmp_path):
    import make_shorts
    from messages import UserFacingError

    with pytest.raises(UserFacingError):
        make_shorts._render_check(tmp_path / "gone.mp4", True)


# --- 6. tests that render for real --------------------------------------------------------------

def _make_source(path, colours, fps=25, size="320x180"):
    inputs, labels = [], ""
    for i, (colour, seconds) in enumerate(colours):
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s={size}:r={fps}:d={seconds}"]
        labels += f"[{i}:v]"
    total = sum(s for _, s in colours)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-f", "lavfi", "-i", f"sine=d={total}",
                    "-filter_complex", f"{labels}concat=n={len(colours)}:v=1[v]", "-map", "[v]",
                    "-map", f"{len(colours)}:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(path)], check=True)


def _render(tmp_path, src, segments, **kw):
    (tmp_path / "c.ass").write_text(captions.build_ass([], preset="social"), encoding="utf-8")
    (tmp_path / "fonts").mkdir(exist_ok=True)
    cmd = render.build_ffmpeg_cmd(str(src), segments, ass_name="c.ass", out_name="o.mp4", **kw)
    subprocess.run(cmd, cwd=tmp_path, check=True)
    return tmp_path / "o.mp4"


def _frames_rgb(path):
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-vf", "scale=1:1,format=rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]


def _probe(path, entries, stream="v:0"):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", f"stream={entries}",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return out.stdout.strip().split(",")


@needs_ffmpeg
def test_a_punched_in_multi_part_render_is_vertical_and_in_sync(tmp_path):
    src = tmp_path / "src.mp4"
    _make_source(src, [("red", 3), ("blue", 3)])
    out = _render(tmp_path, src, [(0.0, 2.0), (3.5, 5.5)], reframe="center", crop_x=[0.5, 0.5], zoom=[1.0, 1.08])
    assert _probe(out, "width,height") == ["1080", "1920"]
    video = float(_probe(out, "duration")[0])
    assert video == pytest.approx(4.0, abs=0.05) and _sound_seconds(out) == pytest.approx(4.0, abs=0.08)


def _sound_seconds(path):
    """Length of the decoded sound. The stored stream duration is not trusted: after loudnorm and
    concat it read 3.1 s for 4.0 s of real sound."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return len(raw) / 2 / 8000


@needs_ffmpeg
def test_a_render_keeps_every_frame_in_order(tmp_path):
    src = tmp_path / "src.mp4"
    _make_source(src, [("red", 2), ("green", 2), ("blue", 2)])
    out = _render(tmp_path, src, [(0.0, 3.0), (4.0, 6.0)], reframe="center", crop_x=[0.5, 0.5])
    colours = ["r" if r > max(g, b) else "g" if g > b else "b" for r, g, b in _frames_rgb(out)]
    # first part: red 2 s and green 1 s; second part: blue 2 s; 25 frames a second
    assert colours.count("r") == 50 and colours.count("g") == 25 and colours.count("b") == 50
    assert "".join(colours) == "r" * 50 + "g" * 25 + "b" * 50


@needs_ffmpeg
def test_the_crop_switches_on_the_exact_frame_of_a_camera_cut(tmp_path):
    # a wide source: left half red, right half blue for 1 s, then swapped; the crop follows a "face"
    # on the left in the first shot and on the right in the second
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x360:r=25:d=2",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x360:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=d=2",
                    "-filter_complex", "[0:v][1:v]hstack[a];[1:v][0:v]hstack[b];"
                                       "[a]trim=0:1,setpts=PTS-STARTPTS[a1];[b]trim=0:1,setpts=PTS-STARTPTS[b1];"
                                       "[a1][b1]concat=n=2:v=1[v]",
                    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                   check=True)
    cuts = facecrop.exact_cuts(src, 0.0, 2.0, 25.0)
    assert cuts == pytest.approx([0.98])
    steps = [(0.0, 0.0), (cuts[0], 1.0)]  # follow red: on the left, then on the right after the cut
    out = _render(tmp_path, src, [(0.0, 2.0)], reframe="center", crop_x=[steps])
    colours = ["r" if r > b else "b" for r, g, b in _frames_rgb(out)]
    assert colours == ["r"] * 50  # a switch one frame early or late would show a blue frame
