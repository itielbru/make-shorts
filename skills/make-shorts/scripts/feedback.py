"""Per-project decision log: the user's verdict on each clip, one JSON line per verdict."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from messages import UserFacingError

OUTCOMES = ("approved", "changed", "rejected")
KINDS = ("technical", "creative")
CATEGORIES = {
    "technical": ("sync", "frozen", "framing", "captions", "cut", "audio", "other"),
    "creative": ("start", "end", "moment", "length", "pacing", "hook", "other"),
}
LOG_NAME = "feedback.jsonl"


@dataclass
class Verdict:
    clip: int
    outcome: str
    kind: str | None = None
    category: str | None = None
    reason: str = ""
    at: str = ""


def validate_verdict(v: Verdict) -> None:
    def bad(reason: str) -> UserFacingError:
        return UserFacingError("bad_feedback", reason=reason)

    if isinstance(v.clip, bool) or not isinstance(v.clip, int) or v.clip < 1:
        raise bad("מספר הקליפ חייב להיות מספר שלם חיובי")
    if v.outcome not in OUTCOMES:
        raise bad("התוצאה חייבת להיות אחת מ: " + ", ".join(OUTCOMES))
    if v.outcome == "approved":
        if v.kind or v.category:
            raise bad("קליפ שאושר לא מקבל סוג או קטגוריה")
        return
    if v.kind not in KINDS:
        raise bad("צריך לציין סוג: " + ", ".join(KINDS))
    if v.category not in CATEGORIES[v.kind]:
        raise bad("קטגוריה לא מתאימה לסוג " + v.kind + ", האפשרויות: " + ", ".join(CATEGORIES[v.kind]))


def append_verdict(project: Path, v: Verdict) -> None:
    validate_verdict(v)
    if not v.at:
        v.at = datetime.now().isoformat(timespec="seconds")
    project = Path(project)
    project.mkdir(parents=True, exist_ok=True)
    with (project / LOG_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(v), ensure_ascii=False) + "\n")


def read_verdicts(project: Path) -> tuple[list[Verdict], int]:
    """Latest verdict per clip in clip order, plus the number of unreadable lines skipped."""
    path = Path(project) / LOG_NAME
    if not path.exists():
        return [], 0
    latest: dict[int, Verdict] = {}
    skipped = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            v = Verdict(**json.loads(line))
            validate_verdict(v)
        except (ValueError, TypeError, UserFacingError):
            skipped += 1
            continue
        latest[v.clip] = v
    return [latest[c] for c in sorted(latest)], skipped
