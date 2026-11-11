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
