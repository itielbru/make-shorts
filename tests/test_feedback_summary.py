import config
import feedback
import make_shorts
from feedback import Verdict


def test_summary_counts_technical_and_creative_apart(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        p.mkdir()
    feedback.append_verdict(a, Verdict(1, "approved"))
    feedback.append_verdict(a, Verdict(2, "changed", "technical", "framing", "פנים בקצה"))
    feedback.append_verdict(b, Verdict(1, "rejected", "creative", "end", "נגמר מוקדם"))
    s = feedback.summarize([a, b])
    assert (s.clips, s.approved, s.changed, s.rejected) == (3, 1, 1, 1)
    assert (s.technical, s.creative) == (1, 1)
    assert s.by_category == {"technical/framing": 1, "creative/end": 1}


def test_summary_skips_a_project_that_no_longer_exists(tmp_path):
    s = feedback.summarize([tmp_path / "gone"])
    assert s.clips == 0 and s.missing_projects == [tmp_path / "gone"]


def test_summary_counts_skipped_lines(tmp_path):
    feedback.append_verdict(tmp_path, Verdict(1, "approved"))
    with (tmp_path / feedback.LOG_NAME).open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert feedback.summarize([tmp_path]).skipped_lines == 1


def test_summary_markdown_shows_the_rate_without_fix_in_hebrew():
    s = feedback.Summary(4, 3, 1, 0, 1, 0, {"technical/sync": 1}, [], 0)
    text = feedback.summary_markdown(s, [])
    assert "75%" in text and "אושרו בלי תיקון" in text


def test_summary_markdown_orders_categories_projects_and_notes(tmp_path):
    s = feedback.Summary(5, 1, 3, 1, 2, 2, {"creative/end": 3, "technical/sync": 1}, [tmp_path / "gone"], 2)
    one = feedback.Summary(5, 1, 3, 1, 2, 2, {}, [], 0)
    text = feedback.summary_markdown(s, [("proj1", one)])
    assert text.index("creative/end") < text.index("technical/sync")
    assert "proj1" in text and "gone" in text and "2" in text
    assert "תקלות טכניות" in text and "תיקונים יצירתיים" in text


def test_summary_markdown_with_no_clips_does_not_divide_by_zero():
    text = feedback.summary_markdown(feedback.Summary(0, 0, 0, 0, 0, 0, {}, [], 0), [])
    assert "%" not in text and "אין נתונים" in text


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "cfg")


def test_summary_command_without_registered_projects_says_no_feedback(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    assert make_shorts.main(["summary"]) == 0
    assert "אין עדיין משוב" in capsys.readouterr().out


def test_summary_command_reads_every_registered_project(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    a = tmp_path / "a"
    feedback.append_verdict(a, Verdict(1, "approved"))
    feedback.register_project(a, tmp_path / "cfg" / feedback.REGISTRY_NAME)
    assert make_shorts.main(["summary"]) == 0
    out = capsys.readouterr().out
    assert "100%" in out and "a" in out


def test_summary_command_with_project_ignores_the_registry(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    feedback.append_verdict(a, Verdict(1, "approved"))
    feedback.append_verdict(b, Verdict(1, "rejected", "creative", "end"))
    feedback.register_project(b, tmp_path / "cfg" / feedback.REGISTRY_NAME)
    assert make_shorts.main(["summary", "--project", str(a)]) == 0
    out = capsys.readouterr().out
    assert "100%" in out and "creative/end" not in out
