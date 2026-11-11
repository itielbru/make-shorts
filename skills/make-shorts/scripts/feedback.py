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


REGISTRY_NAME = "projects.json"


def registered_projects(registry: Path) -> list[Path]:
    """Projects that have recorded verdicts. A missing or unreadable registry is an empty list."""
    try:
        data = json.loads(Path(registry).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [Path(p) for p in data if isinstance(p, str)]


def register_project(project: Path, registry: Path) -> None:
    registry = Path(registry)
    path = str(Path(project).resolve())
    known = [str(p) for p in registered_projects(registry)]
    if path in known:
        return
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(known + [path], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


@dataclass
class Summary:
    clips: int
    approved: int
    changed: int
    rejected: int
    technical: int
    creative: int
    by_category: dict[str, int]
    missing_projects: list[Path]
    skipped_lines: int


def summarize(projects: list[Path]) -> Summary:
    """Add up the latest verdict of every clip in the given projects. Technical and creative stay apart."""
    s = Summary(0, 0, 0, 0, 0, 0, {}, [], 0)
    for project in projects:
        if not Path(project).is_dir():
            s.missing_projects.append(project)
            continue
        verdicts, skipped = read_verdicts(project)
        s.skipped_lines += skipped
        for v in verdicts:
            s.clips += 1
            if v.outcome == "approved":
                s.approved += 1
                continue
            if v.outcome == "changed":
                s.changed += 1
            else:
                s.rejected += 1
            if v.kind == "technical":
                s.technical += 1
            elif v.kind == "creative":
                s.creative += 1
            key = f"{v.kind}/{v.category}"
            s.by_category[key] = s.by_category.get(key, 0) + 1
    return s


def _percent(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "אין נתונים"


def summary_markdown(s: Summary, per_project: list[tuple[str, Summary]]) -> str:
    lines = ["# סיכום איכות הקליפים", ""]
    if not s.clips:
        lines.append("- אושרו בלי תיקון: אין נתונים")
    else:
        lines.append(f"- אושרו בלי תיקון: {_percent(s.approved, s.clips)} ({s.approved} מתוך {s.clips} קליפים)")
    lines.append(f"- תקלות טכניות: {s.technical}")
    lines.append(f"- תיקונים יצירתיים: {s.creative}")
    if s.by_category:
        lines += ["", "## לפי קטגוריה"]
        for key, n in sorted(s.by_category.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- {key}: {n}")
    if per_project:
        lines += ["", "## לפי פרויקט"]
        for name, p in per_project:
            lines.append(f"- {name}: {p.clips} קליפים, אושרו {p.approved}, שונו {p.changed}, נדחו {p.rejected}")
    notes = []
    if s.missing_projects:
        notes.append("פרויקטים שלא נמצאו: " + ", ".join(str(p) for p in s.missing_projects))
    if s.skipped_lines:
        notes.append(f"שורות שלא נקראו ודולגו: {s.skipped_lines}")
    if notes:
        lines += ["", "; ".join(notes)]
    return "\n".join(lines) + "\n"


LESSON_TYPES = ("style", "principle")
RULE_MIN_PROJECTS = 2
LESSONS_NAME = "lessons.json"
_ALL_CATEGORIES = tuple(dict.fromkeys(c for cats in CATEGORIES.values() for c in cats))


@dataclass
class Lesson:
    id: str
    text: str
    type: str
    category: str
    projects: list[str]
    confirmed: bool = False

    @property
    def is_rule(self) -> bool:
        return self.confirmed or len(set(self.projects)) >= RULE_MIN_PROJECTS


def load_lessons(store: Path) -> list[Lesson]:
    """A missing store is an empty list; a corrupt one is an error, so learned rules are never dropped silently."""
    store = Path(store)
    if not store.exists():
        return []
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("not a list")
        return [Lesson(**item) for item in raw]
    except (OSError, ValueError, TypeError):
        raise UserFacingError("bad_feedback", reason=f"קובץ הלקחים {store} פגום ולא נקרא. לא שיניתי אותו") from None


def save_lessons(store: Path, lessons: list[Lesson]) -> None:
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps([asdict(x) for x in lessons], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _lesson_number(lesson_id: str) -> int:
    digits = lesson_id[1:]
    return int(digits) if lesson_id.startswith("L") and digits.isdigit() else 0


def record_lesson(store: Path, text: str, type: str, category: str, project: Path,
                  lesson_id: str | None = None, confirm: bool = False) -> Lesson:
    def bad(reason: str) -> UserFacingError:
        return UserFacingError("bad_feedback", reason=reason)

    if type and type not in LESSON_TYPES:
        raise bad("סוג הלקח חייב להיות אחד מ: " + ", ".join(LESSON_TYPES))
    if category and category not in _ALL_CATEGORIES:
        raise bad("קטגוריה לא מוכרת, האפשרויות: " + ", ".join(_ALL_CATEGORIES))
    lessons = load_lessons(store)
    path = str(Path(project).resolve())
    if lesson_id is not None:
        lesson = next((x for x in lessons if x.id == lesson_id), None)
        if lesson is None:
            raise bad(f"אין לקח עם המזהה {lesson_id}")
        if path not in lesson.projects:
            lesson.projects.append(path)
        if confirm:
            lesson.confirmed = True
    else:
        if not type or not category:
            raise bad("לקח חדש צריך סוג וקטגוריה")
        if not text or not text.strip():
            raise bad("לקח חדש צריך טקסט")
        n = 1 + max((_lesson_number(x.id) for x in lessons), default=0)
        lesson = Lesson(f"L{n}", text.strip(), type, category, [path], confirm)
        lessons.append(lesson)
    save_lessons(store, lessons)
    return lesson


def drop_lesson(store: Path, lesson_id: str) -> None:
    lessons = load_lessons(store)
    kept = [x for x in lessons if x.id != lesson_id]
    if len(kept) == len(lessons):
        raise UserFacingError("bad_feedback", reason=f"אין לקח עם המזהה {lesson_id}")
    save_lessons(store, kept)
