import json
import shutil
from pathlib import Path

import pytest

import acquire
import audio
import workspace
from messages import UserFacingError
from runtime import CompletedText
from runtime import run_text as real_run_text


def test_ytdlp_args_contain_no_playlist_N8_format_and_merge_mp4(tmp_path):
    args = acquire.ytdlp_args("https://youtu.be/abc", tmp_path)
    assert args[0] == "yt-dlp"
    assert "--no-playlist" in args
    assert args[args.index("-N") + 1] == "8"
    # 1080p: the vertical crop keeps only a 9:16 strip of the wide picture, so a 720p source is
    # stretched 2.7x and smears; H.264 first, because it decodes far faster than AV1 on a weak CPU
    assert args[args.index("-f") + 1] == (
        "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/b[height<=1080]/bv+ba/b"
    )
    assert args[args.index("--merge-output-format") + 1] == "mp4"
    assert args[-1] == "https://youtu.be/abc"
    assert "--" in args  # the URL can never be read as an option


def test_ytdlp_percent_in_dir_is_escaped(tmp_path):
    odd = tmp_path / "100%done"
    args = acquire.ytdlp_args("https://youtu.be/abc", odd)
    template = args[args.index("-o") + 1]
    assert "100%%done" in template
    assert template.endswith("video.%(ext)s")


def test_403_error_maps_to_update_ytdlp_message():
    err = acquire.diagnose_download_error("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert isinstance(err, UserFacingError)
    assert err.key == "ytdlp_403"
    assert "yt-dlp" in err.message_he


def test_js_runtime_error_maps_to_deno_message():
    err = acquire.diagnose_download_error("WARNING: No supported JavaScript runtime could be found")
    assert err.key == "ytdlp_js_runtime"
    assert "Deno" in err.message_he


def test_unknown_error_maps_to_download_failed_without_leaking_arguments():
    err = acquire.diagnose_download_error("ERROR: something odd happened")
    assert err.key == "download_failed"


def test_probe_reads_dimensions_from_synthetic_video(synthetic_video):
    info = acquire.probe(synthetic_video)
    assert (info.width, info.height) == (1920, 1080)
    assert info.has_video and info.has_audio
    assert 9.5 < info.duration < 10.6
    assert info.title == "synthetic"


def test_probe_audio_only_raises_audio_only_unsupported(synthetic_video, tmp_path):
    mp3 = audio.extract_audio(synthetic_video, tmp_path / "only.mp3")
    with pytest.raises(UserFacingError) as exc:
        acquire.probe(mp3)
    assert exc.value.key == "audio_only_unsupported"


def test_probe_unreadable_file_raises(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(UserFacingError) as exc:
        acquire.probe(junk)
    assert exc.value.key == "unreadable_media"


def test_fetch_local_path_is_used_in_place(synthetic_video, hebrew_tmp):
    ws = workspace.Workspace.create(hebrew_tmp)
    info = acquire.fetch(str(synthetic_video), ws)
    assert info.path == synthetic_video
    assert not (ws.path / "download").exists()


def test_fetch_missing_local_file_raises_file_not_found(hebrew_tmp):
    ws = workspace.Workspace.create(hebrew_tmp)
    with pytest.raises(UserFacingError) as exc:
        acquire.fetch(str(hebrew_tmp / "nope.mp4"), ws)
    assert exc.value.key == "file_not_found"


def test_fetch_url_downloads_into_workspace_and_reads_title(monkeypatch, synthetic_video, hebrew_tmp):
    seen = {}

    def fake_run(cmd, *, timeout, cwd=None):
        if cmd[0] != "yt-dlp":  # ffprobe and friends run for real
            return real_run_text(cmd, timeout=timeout, cwd=cwd)
        seen["cmd"] = cmd
        seen["timeout"] = timeout
        out_dir = Path(cmd[cmd.index("-o") + 1]).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(synthetic_video, out_dir / "video.mp4")
        (out_dir / "video.info.json").write_text(json.dumps({"title": "כותרת הסרטון"}, ensure_ascii=False), encoding="utf-8")
        return CompletedText("", "", 0)

    monkeypatch.setattr(acquire, "run_text", fake_run)
    ws = workspace.Workspace.create(hebrew_tmp)
    info = acquire.fetch("https://www.youtube.com/watch?v=abc", ws)
    assert info.title == "כותרת הסרטון"
    assert ws.path in info.path.parents
    assert seen["timeout"] == 1800
    assert seen["cmd"][-1] == "https://www.youtube.com/watch?v=abc"


def test_fetch_url_failure_is_diagnosed(monkeypatch, hebrew_tmp):
    def failing(cmd, *, timeout, cwd=None):
        return CompletedText("", "ERROR: HTTP Error 403: Forbidden", 1)

    monkeypatch.setattr(acquire, "run_text", failing)
    ws = workspace.Workspace.create(hebrew_tmp)
    with pytest.raises(UserFacingError) as exc:
        acquire.fetch("https://youtu.be/abc", ws)
    assert exc.value.key == "ytdlp_403"
