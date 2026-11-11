import json

import config
import make_shorts
import pytest

import feedback
from feedback import Verdict
from messages import UserFacingError


def test_append_and_read_keep_the_latest_verdict_per_clip(tmp_path):
    feedback.append_verdict(tmp_path, Verdict(1, "approved"))
    feedback.append_verdict(tmp_path, Verdict(2, "changed", "creative", "end", "נגמר לפני השיא"))
    feedback.append_verdict(tmp_path, Verdict(1, "rejected", "technical", "sync", "תקוע"))
    verdicts, skipped = feedback.read_verdicts(tmp_path)
    assert [(v.clip, v.outcome) for v in verdicts] == [(1, "rejected"), (2, "changed")]
    assert verdicts[1].reason == "נגמר לפני השיא" and skipped == 0
    assert verdicts[0].at


def test_a_corrupt_line_is_skipped_and_counted(tmp_path):
    feedback.append_verdict(tmp_path, Verdict(1, "approved"))
    with (tmp_path / "feedback.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    verdicts, skipped = feedback.read_verdicts(tmp_path)
    assert len(verdicts) == 1 and skipped == 1


def test_no_log_file_means_no_verdicts(tmp_path):
    assert feedback.read_verdicts(tmp_path) == ([], 0)


@pytest.mark.parametrize("v", [
    Verdict(1, "maybe"),                                  # unknown outcome
    Verdict(1, "changed"),                                # changed without kind/category
    Verdict(1, "rejected", "creative", "sync"),           # category of the other kind
    Verdict(1, "approved", "technical", "sync"),          # approved carries no kind
])
def test_invalid_verdicts_are_refused(v):
    with pytest.raises(UserFacingError):
        feedback.validate_verdict(v)


def _project(tmp_path, monkeypatch, rendered=("1", "2")):
    (tmp_path / "process_config.json").write_text(
        json.dumps({"rendered": {k: {} for k in rendered}}), encoding="utf-8")
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "cfg")


def test_feedback_refuses_a_clip_that_was_never_rendered(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, ("1",))
    code = make_shorts.main(["feedback", "--project", str(tmp_path), "--clip", "7", "--outcome", "approved"])
    assert code == 2 and "7" in capsys.readouterr().err


def test_feedback_records_and_registers_the_project(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--clip", "2", "--outcome", "changed",
                             "--kind", "creative", "--category", "end", "--reason", "נקטע"]) == 0
    assert feedback.read_verdicts(tmp_path)[0][0].category == "end"
    assert feedback.registered_projects(tmp_path / "cfg" / "projects.json") == [tmp_path.resolve()]


def test_registering_twice_lists_the_project_once_and_corrupt_registry_is_empty(tmp_path):
    reg = tmp_path / "sub" / "projects.json"
    feedback.register_project(tmp_path, reg)
    feedback.register_project(tmp_path, reg)
    assert feedback.registered_projects(reg) == [tmp_path.resolve()]
    reg.write_text("{oops", encoding="utf-8")
    assert feedback.registered_projects(reg) == []
    assert feedback.registered_projects(tmp_path / "none.json") == []


def test_feedback_file_is_all_or_nothing(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    f = tmp_path / "v.json"
    f.write_text(json.dumps([{"clip": 1, "outcome": "approved"},
                             {"clip": 2, "outcome": "changed", "kind": "creative", "category": "nope"}]),
                 encoding="utf-8")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--file", str(f)]) == 2
    assert feedback.read_verdicts(tmp_path) == ([], 0)
    f.write_text(json.dumps([{"clip": 1, "outcome": "approved"}, {"clip": 9, "outcome": "approved"}]),
                 encoding="utf-8")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--file", str(f)]) == 2
    assert feedback.read_verdicts(tmp_path) == ([], 0)
    f.write_text(json.dumps([{"clip": 1, "outcome": "approved"},
                             {"clip": 2, "outcome": "rejected", "kind": "technical", "category": "sync"}]),
                 encoding="utf-8")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--file", str(f)]) == 0
    assert [v.clip for v in feedback.read_verdicts(tmp_path)[0]] == [1, 2]


def test_feedback_needs_exactly_one_input_mode(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    assert make_shorts.main(["feedback", "--project", str(tmp_path)]) == 2
    f = tmp_path / "v.json"
    f.write_text("[]", encoding="utf-8")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--file", str(f),
                             "--clip", "1", "--outcome", "approved"]) == 2
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--clip", "1"]) == 2
    f.write_text("not json", encoding="utf-8")
    assert make_shorts.main(["feedback", "--project", str(tmp_path), "--file", str(f)]) == 2
