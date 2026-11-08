import dataclasses
import json
import re
from pathlib import Path

import pytest

import captions
import config
import engines
import make_shorts
import profiles
import render
import setup_env
from messages import UserFacingError
from setup_env import EnvStatus, Hardware
from transcript import Transcript, Word

HEBREW = re.compile(r"[֐-׿]")
SENTENCES = [
    "שלום וברוכים הבאים לפרק.",
    "היום נדבר על חרדה ואיך להתמודד איתה.",
    "זה נושא חשוב מאוד לכולנו.",
    "תודה שהקשבתם עד כאן.",
]
WORDS = [
    Word(token, round(i * 0.5, 6), round(i * 0.5 + 0.45, 6))
    for i, token in enumerate(" ".join(SENTENCES).split())
]


class FakeEngine:
    name = "groq"

    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def transcribe(self, audio, *, language, progress):
        self.calls += 1
        progress("מתמלל...")
        if self.error:
            raise self.error
        return Transcript(words=list(WORDS), language=language, engine="groq")


def run(capsys, *argv):
    code = make_shorts.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def project(tmp_path):
    return tmp_path / "proj"


@pytest.fixture
def profile_ready(config_home):
    def make(**overrides):
        base = dataclasses.replace(
            profiles.load_template("podcaster"), engine="groq", cloud_allowed=True, **overrides
        )
        profiles.save_profile(base, config.config_dir() / "profile.json")
        return base
    return make


@pytest.fixture
def fake_engine(monkeypatch):
    engine = FakeEngine()
    engine.requested = []

    def get_engine(name):
        engine.requested.append(name)
        return engine

    monkeypatch.setattr(engines, "get_engine", get_engine)
    return engine


@pytest.fixture
def transcribed(profile_ready, fake_engine, synthetic_video, project, capsys):
    profile_ready()
    assert run(capsys, "ingest", synthetic_video, "--project", project)[0] == 0
    assert run(capsys, "transcribe", "--project", project)[0] == 0
    return project


def write_picks(tmp_path, picks):
    path = tmp_path / "picks.json"
    path.write_text(json.dumps(picks, ensure_ascii=False), encoding="utf-8")
    return path


def pick(s, e, title, score=80):
    return {"start_word": s, "end_word": e, "score": score, "reason": "סיבה", "title": title, "hook_text": None}


# ---------------------------------------------------------------- setup

def patch_env(monkeypatch, **overrides):
    values = dict(ffmpeg=True, ffprobe=True, ytdlp=False, deno=True, libass=True, missing=["yt-dlp"])
    values.update(overrides)
    monkeypatch.setattr(setup_env, "check_environment", lambda: EnvStatus(**values))
    monkeypatch.setattr(setup_env, "detect_hardware", lambda: Hardware("cpu", 10, 16.0, False))


def test_setup_json_reports_first_run_and_missing_tools(config_home, monkeypatch, capsys):
    patch_env(monkeypatch)
    code, out, _ = run(capsys, "setup", "--json", "--skip-selftest")
    data = json.loads(out)
    assert code == 0
    assert data["first_run"] is True
    assert "yt-dlp" in data["missing"]
    assert data["can_proceed"] is True


def test_missing_dependency_exits_2(config_home, monkeypatch, capsys):
    patch_env(monkeypatch, ffmpeg=False, missing=["ffmpeg"])
    code, out, _ = run(capsys, "setup", "--json", "--skip-selftest")
    assert code == 2
    assert json.loads(out)["can_proceed"] is False


def test_setup_runs_caption_selftest_and_reports_the_result(config_home, monkeypatch, capsys, tmp_path):
    patch_env(monkeypatch)
    sample = tmp_path / "sample.mp4"
    sample.write_bytes(b"x")
    monkeypatch.setattr(captions, "render_selftest", lambda ws, font="heebo": sample)
    code, out, _ = run(capsys, "setup", "--json")
    data = json.loads(out)
    assert code == 0 and data["caption_selftest"]["ok"] is True
    assert Path(data["caption_selftest"]["path"]).exists()


def test_setup_stops_if_the_caption_selftest_fails(config_home, monkeypatch, capsys):
    patch_env(monkeypatch)

    def boom(ws, font="heebo"):
        raise UserFacingError("unknown", detail="הכתוביות לא נראו טוב")

    monkeypatch.setattr(captions, "render_selftest", boom)
    code, out, _ = run(capsys, "setup", "--json")
    data = json.loads(out)
    assert code == 2
    assert data["caption_selftest"]["ok"] is False and data["can_proceed"] is False


def test_finish_marks_setup_complete_only_with_a_valid_profile(config_home, monkeypatch, capsys, profile_ready):
    patch_env(monkeypatch)
    code, _, err = run(capsys, "setup", "--finish", "--skip-selftest", "--json")
    assert code == 2 and HEBREW.search(err)
    profile_ready()
    code, out, _ = run(capsys, "setup", "--finish", "--skip-selftest", "--json")
    assert code == 0 and json.loads(out)["first_run"] is False
    assert config.get_setting("SETUP_COMPLETE") == "true"


# ---------------------------------------------------------------- ingest and transcribe

def test_ingest_writes_state_and_reports_media(profile_ready, synthetic_video, project, capsys):
    profile_ready()
    code, out, err = run(capsys, "ingest", synthetic_video, "--project", project)
    assert code == 0
    assert out.startswith("# ") and "synthetic" in out
    assert "[make-shorts]" in err
    state = json.loads((project / "process_config.json").read_text(encoding="utf-8"))
    assert state["media_title"] == "synthetic"


def test_progress_goes_to_stderr_report_to_stdout(profile_ready, fake_engine, synthetic_video, project, capsys):
    profile_ready()
    run(capsys, "ingest", synthetic_video, "--project", project)
    code, out, err = run(capsys, "transcribe", "--project", project)
    assert code == 0
    assert out.startswith("# ") and "[make-shorts]" not in out
    assert "[make-shorts]" in err


def test_missing_api_key_exits_3_with_hebrew_message_and_no_secret(
    profile_ready, config_home, monkeypatch, synthetic_video, project, capsys
):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    profile_ready()
    run(capsys, "ingest", synthetic_video, "--project", project)
    code, out, err = run(capsys, "transcribe", "--project", project)
    assert code == 3
    assert HEBREW.search(err)


def test_cloud_engine_refused_when_profile_cloud_allowed_false(
    config_home, fake_engine, synthetic_video, project, capsys
):
    base =dataclasses.replace(profiles.load_template("podcaster"), engine="groq", cloud_allowed=False)
    profiles.save_profile(base, config.config_dir() / "profile.json")
    run(capsys, "ingest", synthetic_video, "--project", project)
    code, _, err = run(capsys, "transcribe", "--project", project)
    assert code == 2 and HEBREW.search(err)
    assert fake_engine.calls == 0


def test_transcript_cache_hit_skips_engine_call(profile_ready, fake_engine, synthetic_video, project, capsys):
    profile_ready()
    run(capsys, "ingest", synthetic_video, "--project", project)
    run(capsys, "transcribe", "--project", project)
    code, _, err = run(capsys, "transcribe", "--project", project)
    assert code == 0
    assert fake_engine.calls == 1
    assert "שמור" in err


def test_engine_failure_does_not_switch_engine_silently(
    profile_ready, fake_engine, synthetic_video, project, capsys
):
    profile_ready()
    fake_engine.error = UserFacingError("transcription_failed", provider="Groq", reason="HTTP 500")
    run(capsys, "ingest", synthetic_video, "--project", project)
    code, _, err = run(capsys, "transcribe", "--project", project)
    assert code == 2 and HEBREW.search(err)
    assert fake_engine.requested == ["groq"]


# ---------------------------------------------------------------- windows, refine

def test_windows_shows_indexed_transcript_and_pages(transcribed, capsys):
    code, out, _ = run(capsys, "windows", "--project", transcribed, "--start-window", 0, "--count", 1)
    assert code == 0
    assert "[0]שלום" in out and "חלון 0" in out


def test_refine_reports_clips_and_dropped_picks(transcribed, tmp_path, capsys):
    picks = write_picks(tmp_path, [pick(0, 3, "פתיחה"), pick(0, 999, "שבור")])
    code, out, _ = run(capsys, "refine", "--project", transcribed, "--picks", picks, "--min-s", 1, "--max-s", 8)
    assert code == 0
    assert "פתיחה" in out and "שבור" in out  # the dropped pick is explained
    state = json.loads((transcribed / "process_config.json").read_text(encoding="utf-8"))
    assert [c["title"] for c in state["clips"]] == ["פתיחה"]
    assert state["clips"][0]["id"] == 1


def test_bad_picks_file_is_reported_plainly(transcribed, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run(capsys, "refine", "--project", transcribed, "--picks", bad)
    assert code == 2 and HEBREW.search(err)


# ---------------------------------------------------------------- render, qa, cleanup

@pytest.fixture
def refined(transcribed, tmp_path, capsys):
    picks = write_picks(tmp_path, [pick(0, 3, "פתיחה"), pick(4, 9, "נושא החרדה")])
    code, _, _ = run(capsys, "refine", "--project", transcribed, "--picks", picks, "--min-s", 1, "--max-s", 8)
    assert code == 0
    return transcribed


def test_render_reuses_state_without_retranscribing(refined, monkeypatch, capsys):
    def forbidden(name):
        raise AssertionError("the engine must not be used when rendering")

    monkeypatch.setattr(engines, "get_engine", forbidden)
    code, out, _ = run(capsys, "render", "--project", refined, "--clips", "1", "--reframe", "center", "--preset", "plain")
    assert code == 0
    files = list((refined / "clips").glob("*.mp4"))
    assert len(files) == 1
    assert files[0].name in out


def test_failed_render_stage_still_produces_partial_report_with_unavailable_evidence(refined, monkeypatch, capsys):
    real = render.render_clip

    def flaky(src, clip, plan, words, ws, **kwargs):
        if kwargs["index"] == 2:
            raise UserFacingError("unknown", detail="boom")
        return real(src, clip, plan, words, ws, **kwargs)

    monkeypatch.setattr(render, "render_clip", flaky)
    code, out, _ = run(capsys, "render", "--project", refined, "--reframe", "center")
    assert code == 0
    assert "ראיות חסרות" in out and "boom" in out
    assert len(list((refined / "clips").glob("*.mp4"))) == 1


def test_render_exits_1_when_every_clip_fails(refined, monkeypatch, capsys):
    def always(*a, **k):
        raise UserFacingError("unknown", detail="boom")

    monkeypatch.setattr(render, "render_clip", always)
    code, out, _ = run(capsys, "render", "--project", refined)
    assert code == 1 and "ראיות חסרות" in out


def test_qa_lists_frames_for_rendered_clips(refined, capsys):
    run(capsys, "render", "--project", refined, "--clips", "1", "--reframe", "center")
    code, out, _ = run(capsys, "qa", "--project", refined)
    assert code == 0
    assert "צפה בכל" in out
    frame_paths = re.findall(r"`([^`]+\.png)`", out)
    assert len(frame_paths) >= 3 and all(Path(p).exists() for p in frame_paths)


def test_cleanup_removes_workspace_but_keeps_outputs(refined, capsys):
    run(capsys, "render", "--project", refined, "--clips", "1", "--reframe", "center")
    state = json.loads((refined / "process_config.json").read_text(encoding="utf-8"))
    workspace_dir = Path(state["workspace"])
    assert workspace_dir.exists()
    code, _, _ = run(capsys, "cleanup", "--project", refined)
    assert code == 0
    assert not workspace_dir.exists()
    assert len(list((refined / "clips").glob("*.mp4"))) == 1


def test_setup_without_a_profile_does_not_claim_a_privacy_choice_the_user_never_made(config_home, monkeypatch, capsys):
    patch_env(monkeypatch)
    code, out, _ = run(capsys, "setup", "--json", "--skip-selftest")
    advice = json.loads(out)["advice"]
    assert advice["engine"] == "groq"
    assert "בחרת" not in advice["reason_he"] and "הסכמת" not in advice["reason_he"]


def test_correct_shows_clip_text_and_applies_word_fixes(refined, capsys):
    code, out, _ = run(capsys, "correct", "--project", refined, "--clips", "1")
    assert code == 0 and "[0]שלום" in out
    edits = refined / "edits.json"
    edits.write_text(json.dumps([{"index": 2, "text": "ברוכות"}], ensure_ascii=False), encoding="utf-8")
    code, out, _ = run(capsys, "correct", "--project", refined, "--edits", edits)
    assert code == 0 and "[2]ברוכות" in out
    state = json.loads((refined / "process_config.json").read_text(encoding="utf-8"))
    saved = transcript_mod_load(state["transcript_path"])
    assert saved.words[2].text == "ברוכות"
    assert (saved.words[2].start, saved.words[2].end) == (WORDS[2].start, WORDS[2].end)


def test_correct_rejects_bad_edits(refined, capsys):
    edits = refined / "edits.json"
    edits.write_text(json.dumps([{"index": 999, "text": "x"}]), encoding="utf-8")
    code, _, err = run(capsys, "correct", "--project", refined, "--edits", edits)
    assert code == 2 and HEBREW.search(err)
    edits.write_text(json.dumps([{"index": 1, "text": "שתי מילים"}], ensure_ascii=False), encoding="utf-8")
    assert run(capsys, "correct", "--project", refined, "--edits", edits)[0] == 2


def test_render_reports_when_the_source_video_is_gone(refined, capsys):
    state_path = refined / "process_config.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["media_path"] = str(refined / "deleted.mp4")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, _, err = run(capsys, "render", "--project", refined)
    assert code == 2 and "ingest" in err


def transcript_mod_load(path):
    import transcript as transcript_mod
    return transcript_mod.load_transcript(Path(path))
