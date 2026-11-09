import json
import re
import subprocess

import pytest

import render
import workspace
from hooks import HookPlan
from selection import Clip
from transcript import Word

WORDS = [Word(t, round(2.0 + i * 0.6, 6), round(2.0 + i * 0.6 + 0.5, 6))
         for i, t in enumerate(["שלום", "עולם", "זה", "מבחן", "של", "כתוביות"])]


def make_clip(start=2.0, end=6.0):
    return Clip(start=start, end=end, start_word=0, end_word=5, score=80, reason="", title="t", hook_text=None)


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return {
        "width": video["width"], "height": video["height"],
        "duration": float(data["format"]["duration"]),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def do_render(video, parent, *, segments=None, title="כותרת", reframe="blur", has_audio=True, out_dir=None):
    ws = workspace.Workspace.create(parent)
    clip = make_clip()
    plan = HookPlan(segments or [(2.0, 6.0)], None, None, False)
    return render.render_clip(
        video, clip, plan, WORDS, ws, preset="clean", reframe=reframe,
        out_dir=out_dir or parent, title=title, index=1, has_audio=has_audio,
    )


def test_cmd_reencodes_with_libx264_and_never_copies_streams():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(2.0, 6.0)], ass_name="c.ass", reframe="blur", out_name="o.mp4")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "copy" not in cmd
    assert cmd[-1] == "o.mp4"


def test_cmd_has_loudnorm_I_minus_16_and_ass_filter_with_fontsdir():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(2.0, 6.0)], ass_name="c.ass", reframe="blur", out_name="o.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "loudnorm=I=-16" in graph
    assert "ass=c.ass:fontsdir=fonts" in graph


def test_blur_graph_overlays_foreground_on_scaled_cropped_background():
    graph = _graph("blur")
    assert "boxblur" in graph and "overlay=(W-w)/2:(H-h)/2" in graph
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in graph and "crop=1080:1920" in graph


def test_center_graph_scales_height_1920_and_crops_to_1080():
    graph = _graph("center")
    assert "scale=-2:1920,crop=1080:1920" in graph
    assert "boxblur" not in graph


def _graph(reframe):
    cmd = render.build_ffmpeg_cmd("in.mp4", [(2.0, 6.0)], ass_name="c.ass", reframe=reframe, out_name="o.mp4")
    return cmd[cmd.index("-filter_complex") + 1]


def test_multiple_segments_use_one_seeked_input_each_and_concat():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(1.0, 2.0), (2.0, 6.0)], ass_name="c.ass", reframe="center", out_name="o.mp4")
    assert cmd.count("-i") == 2
    assert cmd[cmd.index("-ss") + 1] == "1.000" and cmd[cmd.index("-t") + 1] == "1.000"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=0[vc]" in graph and "concat=n=2:v=0:a=1[ac]" in graph


def test_no_audio_graph_has_no_audio_chain():
    cmd = render.build_ffmpeg_cmd("in.mp4", [(2.0, 6.0)], ass_name="c.ass", reframe="center", out_name="o.mp4", has_audio=False)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "loudnorm" not in graph and "[aout]" not in graph
    assert "[aout]" not in cmd


def test_render_produces_1080x1920_with_audio_and_expected_duration(synthetic_video, tmp_path):
    out = do_render(synthetic_video, tmp_path)
    info = probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"] is True
    assert info["duration"] == pytest.approx(4.0, abs=0.35)


def test_teaser_segments_add_their_duration(synthetic_video, tmp_path):
    out = do_render(synthetic_video, tmp_path, segments=[(1.0, 2.0), (2.0, 6.0)], reframe="center")
    assert probe(out)["duration"] == pytest.approx(5.0, abs=0.4)


def test_source_without_audio_renders_without_audio(synthetic_video, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(synthetic_video), "-an", "-c:v", "copy", str(silent)], check=True)
    out = do_render(silent, tmp_path, has_audio=False)
    assert probe(out)["has_audio"] is False


def test_render_works_when_workspace_parent_has_hebrew_and_spaces(synthetic_video, hebrew_tmp):
    out_dir = hebrew_tmp / "פלט סופי"
    out_dir.mkdir()
    out = do_render(synthetic_video, hebrew_tmp, out_dir=out_dir)
    assert out.exists() and out.parent == out_dir
    assert probe(out)["height"] == 1920


def test_final_file_written_with_sanitized_hebrew_title(synthetic_video, hebrew_tmp):
    title = 'מה זה חרדה? "הסבר": קצר'
    out = do_render(synthetic_video, hebrew_tmp, title=title)
    assert out.name == workspace.final_filename(title, 1)
    assert not re.search(r'[<>:"/\\|?*]', out.name)
    assert out.exists()


def test_workspace_is_left_clean_of_intermediates_names_but_not_deleted(synthetic_video, tmp_path):
    ws = workspace.Workspace.create(tmp_path)
    render.render_clip(
        synthetic_video, make_clip(), HookPlan([(2.0, 6.0)], None, None, False), WORDS, ws,
        preset="clean", reframe="center", out_dir=tmp_path, title="x", index=2,
    )
    assert ws.path.exists()  # the CLI deletes it at the end of the run, not each render


def test_render_writes_an_srt_next_to_the_clip(synthetic_video, tmp_path):
    out = do_render(synthetic_video, tmp_path)
    srt = out.with_suffix(".srt")
    assert srt.exists()
    text = srt.read_text(encoding="utf-8")
    assert "-->" in text and "שלום" in text
