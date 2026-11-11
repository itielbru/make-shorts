import json

import pytest

import config
import feedback
import make_shorts
from messages import UserFacingError


def test_a_lesson_from_one_project_stays_a_candidate(tmp_path):
    store, p = tmp_path / "lessons.json", tmp_path / "proj"
    l = feedback.record_lesson(store, "לפתוח בשאלה ולא בתשובה", "principle", "start", p)
    l = feedback.record_lesson(store, "", "principle", "start", p, lesson_id=l.id)
    assert not l.is_rule and len(l.projects) == 1


def test_evidence_from_a_second_project_makes_it_a_rule(tmp_path):
    store = tmp_path / "lessons.json"
    l = feedback.record_lesson(store, "כתוביות עבות", "style", "captions", tmp_path / "a")
    l = feedback.record_lesson(store, "", "style", "captions", tmp_path / "b", lesson_id=l.id)
    assert l.is_rule and feedback.load_lessons(store)[0].is_rule


def test_the_user_can_confirm_a_rule_at_once(tmp_path):
    l = feedback.record_lesson(tmp_path / "s.json", "בלי פייד לשחור", "style", "other", tmp_path, confirm=True)
    assert l.is_rule


def test_confirm_on_an_existing_id_sets_confirmed(tmp_path):
    store = tmp_path / "s.json"
    l = feedback.record_lesson(store, "x", "style", "other", tmp_path)
    l = feedback.record_lesson(store, "", "style", "other", tmp_path, lesson_id=l.id, confirm=True)
    assert l.confirmed and l.is_rule


def test_new_id_is_one_above_the_highest_existing(tmp_path):
    store = tmp_path / "s.json"
    feedback.record_lesson(store, "a", "style", "other", tmp_path)
    feedback.record_lesson(store, "b", "style", "other", tmp_path)
    feedback.drop_lesson(store, "L2")
    assert feedback.record_lesson(store, "c", "style", "other", tmp_path).id == "L2"
    feedback.drop_lesson(store, "L1")
    assert feedback.record_lesson(store, "d", "style", "other", tmp_path).id == "L3"


def test_validation(tmp_path):
    store = tmp_path / "s.json"
    for args in [("t", "bad", "other"), ("t", "style", "nope"), ("", "style", "other"), ("  ", "style", "other")]:
        with pytest.raises(UserFacingError):
            feedback.record_lesson(store, *args, tmp_path)
    with pytest.raises(UserFacingError):
        feedback.record_lesson(store, "", "style", "other", tmp_path, lesson_id="L9")
    with pytest.raises(UserFacingError):
        feedback.drop_lesson(store, "L9")


def test_missing_store_is_empty_and_corrupt_store_is_an_error(tmp_path):
    assert feedback.load_lessons(tmp_path / "none.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(UserFacingError):
        feedback.load_lessons(bad)
    with pytest.raises(UserFacingError):
        feedback.record_lesson(bad, "x", "style", "other", tmp_path)
    assert bad.read_text(encoding="utf-8") == "{oops"


def test_store_is_written_as_readable_utf8(tmp_path):
    store = tmp_path / "s.json"
    feedback.record_lesson(store, "כתוביות", "style", "captions", tmp_path)
    assert "כתוביות" in store.read_text(encoding="utf-8")
    assert json.loads(store.read_text(encoding="utf-8"))[0]["id"] == "L1"


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "cfg")


def _real_project(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    (p / "process_config.json").write_text("{}", encoding="utf-8")
    return p


def test_lessons_command_with_no_store_says_none_yet(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert make_shorts.main(["lessons"]) == 0
    assert "אין עדיין לקחים" in capsys.readouterr().out


def test_lessons_command_lists_rules_in_two_lists_then_candidates(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    a, b = _real_project(tmp_path, "a"), _real_project(tmp_path, "b")
    assert make_shorts.main(["lessons", "--add", "כתוביות עבות", "--type", "style", "--category", "captions",
                             "--project", str(a), "--confirm"]) == 0
    assert make_shorts.main(["lessons", "--add", "לפתוח בשאלה", "--type", "principle", "--category", "start",
                             "--project", str(a)]) == 0
    capsys.readouterr()
    assert make_shorts.main(["lessons"]) == 0
    out = capsys.readouterr().out
    assert out.index("העדפות סגנון") < out.index("כתוביות עבות") < out.index("עקרונות עריכה") < out.index("מועמדים")
    assert out.index("מועמדים") < out.index("לפתוח בשאלה")
    assert "פרויקט אחד" in out and "1 פרויקטים" not in out
    assert "start" not in out and "captions" not in out and "פתיחה" in out
    assert make_shorts.main(["lessons", "--id", "L2", "--project", str(b)]) == 0
    capsys.readouterr()
    make_shorts.main(["lessons"])
    out = capsys.readouterr().out
    assert out.index("עקרונות עריכה") < out.index("לפתוח בשאלה") < out.index("מועמדים")


def test_lessons_command_drop_and_unknown_id(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    a = _real_project(tmp_path, "a")
    make_shorts.main(["lessons", "--add", "x", "--type", "style", "--category", "other", "--project", str(a)])
    assert make_shorts.main(["lessons", "--drop", "L1"]) == 0
    assert feedback.load_lessons(tmp_path / "cfg" / feedback.LESSONS_NAME) == []
    assert make_shorts.main(["lessons", "--drop", "L1"]) == 2
    assert make_shorts.main(["lessons", "--id", "L7", "--confirm"]) == 2


def test_lessons_add_needs_an_existing_project(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    store = tmp_path / "cfg" / feedback.LESSONS_NAME
    base = ["lessons", "--add", "x", "--type", "style", "--category", "other"]
    assert make_shorts.main(base) == 2
    assert make_shorts.main(base + ["--project", str(tmp_path / "nope")]) == 2
    (tmp_path / "empty").mkdir()
    assert make_shorts.main(base + ["--project", str(tmp_path / "empty")]) == 2
    assert not store.exists()
    assert "הלקח לא תקין" in capsys.readouterr().err


def test_confirm_only_adds_no_project_and_evidence_needs_a_real_one(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    store = tmp_path / "cfg" / feedback.LESSONS_NAME
    a = _real_project(tmp_path, "a")
    make_shorts.main(["lessons", "--add", "x", "--type", "style", "--category", "other", "--project", str(a)])
    assert make_shorts.main(["lessons", "--id", "L1", "--confirm"]) == 0
    got = feedback.load_lessons(store)[0]
    assert got.confirmed and len(got.projects) == 1
    assert make_shorts.main(["lessons", "--id", "L1"]) == 2
    assert make_shorts.main(["lessons", "--id", "L1", "--project", str(tmp_path / "ghost")]) == 2
    assert len(feedback.load_lessons(store)[0].projects) == 1


def test_lesson_errors_use_the_lesson_wording(tmp_path):
    with pytest.raises(UserFacingError) as e:
        feedback.record_lesson(tmp_path / "s.json", "t", "bad", "other", tmp_path)
    assert e.value.message_he.startswith("הלקח לא תקין")
    with pytest.raises(UserFacingError) as e:
        feedback.drop_lesson(tmp_path / "s.json", "L9")
    assert e.value.message_he.startswith("הלקח לא תקין")


def test_hebrew_counts():
    assert feedback.count_projects(1) == "פרויקט אחד" and feedback.count_projects(3) == "3 פרויקטים"
    assert feedback.count_clips(1) == "קליפ אחד" and feedback.count_clips(2) == "2 קליפים"
