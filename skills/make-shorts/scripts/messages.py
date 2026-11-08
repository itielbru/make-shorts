"""User-facing messages in simple Hebrew.

Every message says what happened, why, and what to do now. Scripts raise
UserFacingError(key, **fmt) and the CLI prints message_he to stderr and exits
with the error's exit code. Secrets and command-line arguments never go into
a message: only the program name does.
"""
from __future__ import annotations

# Exit codes: 0 success, 2 missing dependency or config error, 3 API key needed.
EXIT_CODES: dict[str, int] = {"api_key_needed": 3}
DEFAULT_EXIT_CODE = 2

MESSAGES: dict[str, str] = {
    "cannot_run": (
        "לא הצלחתי להפעיל את התוכנה \"{program}\". "
        "כנראה שהיא לא מותקנת או שהמחשב לא יודע איפה היא נמצאת. "
        "אפשר להריץ את ההגדרה הראשונית שוב, והיא תגיד מה חסר ותציע איך להתקין."
    ),
    "timed_out": (
        "התוכנה \"{program}\" לקחה יותר זמן מהצפוי ועצרתי אותה. "
        "זה קורה לפעמים עם קבצים גדולים או אינטרנט איטי. אפשר לנסות שוב, או לבחור קובץ קצר יותר."
    ),
    "bad_setting_name": (
        "שם ההגדרה \"{name}\" לא תקין. שם הגדרה יכול להכיל רק אותיות באנגלית, מספרים וקו תחתון. "
        "זו שגיאה פנימית של הכלי, לא של הקובץ שלך."
    ),
    "bad_setting_value": (
        "הערך של ההגדרה \"{name}\" מכיל תווים שאי אפשר לשמור בקובץ ההגדרות. "
        "נסה ערך בלי שורות חדשות ובלי שני סוגי גרשיים יחד."
    ),
    "config_error": (
        "קובץ ההגדרות לא נקרא כראוי (שורה {line}). "
        "אפשר לפתוח אותו, לתקן את השורה או למחוק אותה, ולהריץ שוב."
    ),
    "api_key_needed": (
        "כדי להשתמש ב-{provider} צריך מפתח (סיסמה שמאפשרת לתוכנה לבקש שירות). "
        "אין צורך לשלוח אותו לצ'אט. הכלי יצור קובץ הגדרות ותוכל להדביק בו את המפתח בעצמך."
    ),
    "bad_profile": (
        "הפרופיל לא תקין: {reason}. "
        "אפשר להריץ את ההגדרה הראשונית מחדש ליצירת פרופיל חדש, או לתקן את הקובץ ידנית."
    ),
    "profile_missing": (
        "עוד לא נוצר פרופיל יוצר. הפרופיל שומר את התחום, השפה וההעדפות שלך, "
        "כדי שלא נצטרך לשאול אותך שוב. נריץ עכשיו את ההגדרה הראשונית."
    ),
    "unknown": "משהו לא עבד כמו שצריך ({detail}). אפשר לנסות שוב, ואם זה חוזר לספר לי מה קרה לפני כן.",
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class UserFacingError(Exception):
    """An error whose message is safe and understandable to show the user."""

    def __init__(self, key: str, **fmt: object) -> None:
        self.key = key
        self.fmt = fmt
        template = MESSAGES.get(key, MESSAGES["unknown"])
        data = _SafeDict(fmt)
        if key not in MESSAGES:
            data.setdefault("detail", key)
        self.message_he = template.format_map(data)
        self.exit_code = EXIT_CODES.get(key, DEFAULT_EXIT_CODE)
        super().__init__(self.message_he)
