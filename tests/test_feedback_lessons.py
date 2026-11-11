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


def test_lessons_command_with_no_store_says_none_yet(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert make_shorts.main(["lessons"]) == 0
    assert "אין עדיין לקחים" in capsys.readouterr().out


def test_lessons_command_lists_rules_in_two_lists_then_candidates(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert make_shorts.main(["lessons", "--add", "כתוביות עבות", "--type", "style", "--category", "captions",
                             "--project", str(tmp_path / "a"), "--confirm"]) == 0
    assert make_shorts.main(["lessons", "--add", "לפתוח בשאלה", "--type", "principle", "--category", "start",
                             "--project", str(tmp_path / "a")]) == 0
    capsys.readouterr()
    assert make_shorts.main(["lessons"]) == 0
    out = capsys.readouterr().out
    assert out.index("העדפות סגנון") < out.index("כתוביות עבות") < out.index("עקרונות עריכה") < out.index("מועמדים")
    assert out.index("מועמדים") < out.index("לפתוח בשאלה")
    assert "1 פרויקטים" in out
    assert make_shorts.main(["lessons", "--id", "L2", "--project", str(tmp_path / "b")]) == 0
    capsys.readouterr()
    make_shorts.main(["lessons"])
    out = capsys.readouterr().out
    assert out.index("עקרונות עריכה") < out.index("לפתוח בשאלה") < out.index("מועמדים")


def test_lessons_command_drop_and_unknown_id(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    make_shorts.main(["lessons", "--add", "x", "--type", "style", "--category", "other"])
    assert make_shorts.main(["lessons", "--drop", "L1"]) == 0
    assert feedback.load_lessons(tmp_path / "cfg" / feedback.LESSONS_NAME) == []
    assert make_shorts.main(["lessons", "--drop", "L1"]) == 2
    assert make_shorts.main(["lessons", "--id", "L7", "--confirm"]) == 2
