"""End to end: a 60 s video and a fixed Hebrew transcript go through every stage."""
import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest

import config
import engines
import make_shorts
import profiles
import transcript as transcript_mod

FIXTURE = Path(__file__).parent / "fixtures" / "hebrew_transcript.json"


class FixtureEngine:
    name = "groq"

    def transcribe(self, audio, *, language, progress):
        return transcript_mod.load_transcript(FIXTURE)


@pytest.fixture(scope="module")
def video_60s(tmp_path_factory):
    out = tmp_path_factory.mktemp("e2e") / "episode.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
         "-t", "60", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True,
    )
    return out


def probe(path):
    data = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True, encoding="utf-8",
    ).stdout)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return video["width"], video["height"], float(data["format"]["duration"]), any(
        s["codec_type"] == "audio" for s in data["streams"]
    )


def test_pipeline_ingest_to_cleanup_on_a_hebrew_project_path(config_home, monkeypatch, capsys, video_60s, hebrew_tmp):
    profiles.save_profile(
        dataclasses.replace(profiles.load_template("therapist"), engine="groq", cloud_allowed=True),
        config.config_dir() / "profile.json",
    )
    monkeypatch.setattr(engines, "get_engine", lambda name: FixtureEngine())
    project = hebrew_tmp / "פרויקט בדיקה"

    def run(*argv):
        code = make_shorts.main([str(a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    assert run("ingest", video_60s, "--project", project)[0] == 0
    assert run("transcribe", "--project", project)[0] == 0

    code, out, _ = run("windows", "--project", project, "--count", 3)
    assert code == 0 and "[0]היום" in out

    words = transcript_mod.load_transcript(FIXTURE).words
    hook = " ".join(w.text for w in words[64:68])
    picks = hebrew_tmp / "picks.json"
    picks.write_text(json.dumps([
        {"start_word": 0, "end_word": 39, "score": 85, "reason": "פתיחה ברורה שמסבירה את הנושא", "title": "מה קורה בגוף", "hook_text": None},
        {"start_word": 60, "end_word": 99, "score": 90, "reason": "טכניקה פשוטה שאפשר ליישם מיד", "title": "מה הכי גרוע?", "hook_text": hook},
    ], ensure_ascii=False), encoding="utf-8")
    code, out, _ = run("refine", "--project", project, "--picks", picks, "--min-s", 10, "--max-s", 30)
    assert code == 0 and "מה קורה בגוף" in out and "מה הכי גרוע?" in out

    code, out, _ = run("render", "--project", project, "--hooks", "2=teaser", "--reframe", "center", "--preset", "plain")
    assert code == 0 and "ראיות חסרות" not in out
    clips = sorted((project / "clips").glob("*.mp4"))
    assert len(clips) == 2
    durations = []
    for clip in clips:
        width, height, duration, has_audio = probe(clip)
        assert (width, height) == (1080, 1920) and has_audio
        durations.append(duration)
    assert durations[0] == pytest.approx(20.0, abs=1.5)
    assert durations[1] > durations[0] - 1.0  # the teaser adds its quote before the clip

    code, out, _ = run("qa", "--project", project)
    assert code == 0 and "צפה בגיליון" in out
    frame_paths = re.findall(r"`([^`]+\.png)`", out)
    assert len(frame_paths) >= 6 and all(Path(p).exists() for p in frame_paths)

    state = json.loads((project / "process_config.json").read_text(encoding="utf-8"))
    workspace_dir = Path(state["workspace"])
    assert run("cleanup", "--project", project)[0] == 0
    assert not workspace_dir.exists()
    assert len(list((project / "clips").glob("*.mp4"))) == 2
