import json
import re
import subprocess
from pathlib import Path

import pytest

import qa
from captions import Block
from qa import Frame
from selection import Clip
from transcript import Transcript, Word


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_av(path: Path, *, black=0.0, silent_start=0.0, silent_end=0.0, total=5.0):
    """Small test video: optional black opening, optional silent edges."""
    cmd = []
    vfilters = []
    n_v = 0
    if black:
        cmd += ["-f", "lavfi", "-i", f"color=c=black:s=320x240:r=30:d={black}"]
        n_v += 1
    cmd += ["-f", "lavfi", "-i", f"testsrc=s=320x240:r=30:d={total - black}"]
    n_v += 1
    vfilters.append("".join(f"[{i}:v]" for i in range(n_v)) + f"concat=n={n_v}:v=1:a=0[v]")
    a_idx, a_labels = n_v, []
    for dur, kind in ((silent_start, "anullsrc=r=48000:cl=mono"), (total - silent_start - silent_end, "sine=frequency=440:sample_rate=48000"), (silent_end, "anullsrc=r=48000:cl=mono")):
        if dur > 0:
            cmd += ["-f", "lavfi", "-t", str(dur), "-i", kind]
            a_labels.append(f"[{a_idx}:a]")
            a_idx += 1
    vfilters.append("".join(a_labels) + f"concat=n={len(a_labels)}:v=0:a=1[a]")
    ffmpeg(*cmd, "-filter_complex", ";".join(vfilters), "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", str(total), str(path))
    return path


def info(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    stream = next(s for s in data["streams"] if s["codec_type"] in ("video",))
    return {"width": stream["width"], "height": stream["height"], "duration": float(data["format"].get("duration", 0))}


def kinds(findings):
    return {f.kind for f in findings}


def clip_at(start, end, start_word=0, end_word=1):
    return Clip(start=start, end=end, start_word=start_word, end_word=end_word, score=1, reason="", title="t", hook_text=None)


EMPTY = Transcript(words=[], language="he", engine="t")


def test_caption_cue_times_are_block_midpoints():
    blocks = [Block([Word("א", 0.0, 1.0)], 0.0, 1.0), Block([Word("ב", 2.0, 4.0)], 2.0, 4.0)]
    assert qa.caption_cue_times(blocks) == [0.5, 3.0]


def test_frames_labelled_with_showinfo_times(synthetic_video, tmp_path):
    frames = qa.extract_frames(synthetic_video, [1.0, 3.0, 5.0], tmp_path / "f")
    assert [f.t for f in frames] == pytest.approx([1.0, 3.0, 5.0], abs=0.05)
    assert all(f.path.exists() and f.path.stat().st_size > 0 for f in frames)
    assert all(f.reason for f in frames)


def test_sheet_selection_keeps_between_6_and_8_evenly_spread_frames():
    many = [Frame(Path(f"{i}.png"), float(i), "x") for i in range(12)]
    chosen = qa.sheet_selection(many)
    assert 6 <= len(chosen) <= 8
    assert chosen[0].t == 0.0 and chosen[-1].t == 11.0
    few = many[:5]
    assert qa.sheet_selection(few) == few


def test_contact_sheet_is_four_columns_wide(synthetic_video, tmp_path):
    frames = qa.extract_frames(synthetic_video, [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5], tmp_path / "f")
    sheet = qa.contact_sheet(frames, tmp_path / "sheet.png")
    dims = info(sheet)
    assert dims["width"] == 4 * qa.TILE_W + 3 * 4 + 2 * 4
    assert dims["height"] > dims["width"] * 0.25


def test_dedup_drops_frames_with_mean_diff_at_most_2_0(tmp_path):
    def solid(name, color):
        path = tmp_path / name
        ffmpeg("-f", "lavfi", "-i", f"color=c={color}:s=64x64:d=1", "-frames:v", "1", str(path))
        return Frame(path, 0.0, "x")

    red1, red2, blue = solid("a.png", "red"), solid("b.png", "red"), solid("c.png", "blue")
    kept = qa.dedupe_frames([red1, red2, blue], threshold=2.0)
    assert [f.path.name for f in kept] == ["a.png", "c.png"]
    assert qa.frame_delta(red1.path, blue.path) > 2.0


def test_black_segment_yields_blackdetect_finding(tmp_path):
    video = make_av(tmp_path / "black.mp4", black=2.0)
    assert "black_frames" in kinds(qa.auto_checks(video, 5.0, clip_at(0, 5), EMPTY))


def test_silent_edges_yield_findings(tmp_path):
    video = make_av(tmp_path / "silent.mp4", silent_start=1.0, silent_end=1.0)
    found = kinds(qa.auto_checks(video, 5.0, clip_at(0, 5), EMPTY))
    assert {"silent_start", "silent_end"} <= found


def test_clean_clip_has_no_findings(synthetic_video):
    assert qa.auto_checks(synthetic_video, 10.0, clip_at(0, 10), EMPTY) == []


def test_mid_word_cut_detected_when_clip_boundary_inside_a_word():
    t = Transcript(words=[Word("שלום", 1.0, 2.0), Word("עולם", 2.0, 3.0)], language="he", engine="t")
    assert "mid_word_start" in kinds(qa.check_boundaries(clip_at(1.5, 3.2), t))
    assert "mid_word_end" in kinds(qa.check_boundaries(clip_at(0.8, 2.5), t))
    assert qa.check_boundaries(clip_at(0.8, 3.4), t) == []


def test_duration_mismatch_over_0_3s_flagged(synthetic_video):
    assert "duration_mismatch" in kinds(qa.auto_checks(synthetic_video, 7.0, clip_at(0, 7), EMPTY))


def test_findings_have_plain_hebrew_messages(tmp_path):
    video = make_av(tmp_path / "black.mp4", black=2.0)
    for finding in qa.auto_checks(video, 5.0, clip_at(0, 5), EMPTY):
        assert re.search(r"[\u0590-\u05FF]", finding.message_he)


def test_report_lists_every_frame_path_and_view_all_instruction():
    frames = [Frame(Path("C:/x/f0.png"), 1.0, "כתובית"), Frame(Path("C:/x/f1.png"), 65.5, "כתובית")]
    report = qa.qa_report_markdown(frames, Path("C:/x/sheet.png"), [])
    assert "f0.png" in report and "f1.png" in report
    assert "t=00:01" in report and "t=01:05" in report
    assert "sheet.png" in report
    assert "צפה בכל" in report


def test_preview_frames_returns_ascending_frames_within_the_cap(synthetic_video, tmp_path):
    frames = qa.preview_frames(synthetic_video, tmp_path / "prev", max_frames=5)
    assert 2 <= len(frames) <= 5
    assert [f.t for f in frames] == sorted(f.t for f in frames)
    assert all(f.path.exists() for f in frames)
    assert len(list((tmp_path / "prev").glob("*.jpg"))) == len(frames)
