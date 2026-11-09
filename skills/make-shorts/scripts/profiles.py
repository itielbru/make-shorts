"""Creator profiles: the domain-specific part of an otherwise general tool.

A profile holds language, domain, what a strong clip is, length limits,
caption look, hook default, red-flag rules the creator defines, engine choice
and privacy consent. Templates ship in profiles/ next to this package.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from captions import FONTS, PRESETS
from messages import UserFacingError

HOOK_MODES = ("none", "title", "teaser", "reorder")
CAPTION_PRESETS = tuple(PRESETS)
ENGINES = ("auto", "groq", "ivrit_local", "elevenlabs")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class Profile:
    name: str
    language: str
    domain: str = ""
    strong_clip_types: list[str] = field(default_factory=list)
    min_s: float = 20
    max_s: float = 90
    caption_preset: str = "clean"
    font: str = "heebo"
    hook_default: str = "none"
    red_flags: list[str] = field(default_factory=list)
    engine: str = "auto"
    cloud_allowed: bool = False
    teaching_mode: bool = True
    style_notes: str = ""  # free text about audience, tone and taste; Claude reads it every run
    # How the picture moves. Defaults are what a real user chose after seeing the alternatives:
    # a fade to black at the end looked bad, a slow zoom on still shots looked too strong,
    # and a 15% punch-in at joins read as a jump of its own (8% hides the join without that).
    slow_zoom: bool = False
    fade_to_black: bool = False
    punch_in: float = 1.08


_FIELDS = {f.name for f in dataclasses.fields(Profile)}


def _bad(reason: str) -> UserFacingError:
    return UserFacingError("bad_profile", reason=reason)


def validate(p: Profile) -> None:
    if not p.name:
        raise _bad("חסר שם")
    if not re.fullmatch(r"[a-z]{2,3}", p.language or ""):
        raise _bad("השפה צריכה להיות קוד קצר באותיות קטנות, למשל he או en")
    if p.hook_default not in HOOK_MODES:
        raise _bad(f"סוג פתיח לא מוכר ({p.hook_default})")
    if p.caption_preset not in CAPTION_PRESETS:
        raise _bad(f"סגנון כתוביות לא מוכר ({p.caption_preset})")
    if p.engine not in ENGINES:
        raise _bad(f"מנוע תמלול לא מוכר ({p.engine})")
    if p.font not in FONTS:
        raise _bad(f"פונט לא מוכר ({p.font}). האפשרויות: " + ", ".join(FONTS))
    if p.min_s <= 0 or p.max_s < p.min_s:
        raise _bad("אורך הקליפ המרבי חייב להיות גדול מהמינימלי")
    if not 1.0 <= p.punch_in <= 1.3:
        raise _bad("הזום בחיבורים (punch_in) צריך להיות בין 1.0 (בלי זום) ל-1.3")


def load_profile(path: Path) -> Profile:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UserFacingError("profile_missing") from None
    except (OSError, ValueError):
        raise _bad("הקובץ אינו JSON תקין") from None
    if not isinstance(data, dict):
        raise _bad("מבנה הקובץ שגוי")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise _bad("שדות לא מוכרים: " + ", ".join(unknown))
    try:
        profile = Profile(**data)
    except TypeError:
        raise _bad("חסרים שדות חובה") from None
    validate(profile)
    return profile


def save_profile(p: Profile, path: Path) -> None:
    validate(p)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dataclasses.asdict(p), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.json"))


def load_template(name: str) -> Profile:
    return load_profile(TEMPLATES_DIR / f"{name}.json")
