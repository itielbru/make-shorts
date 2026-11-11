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
    "cloud_not_allowed": (
        "הפרופיל שלך לא מאפשר לשלוח אודיו החוצה, אבל בגרסה הזו התמלול נעשה רק דרך שירות בענן ({engine}). "
        "אם אתה מסכים שהאודיו של הסרטון יישלח אליהם, אפשר לעדכן את הפרופיל. "
        "תמלול מקומי שנשאר אצלך במחשב מתוכנן להמשך."
    ),
    "media_missing": (
        "קובץ הווידאו כבר לא נמצא (אולי הוא נמחק בניקוי, או הועבר). "
        "צריך לטעון אותו שוב עם ingest כדי להמשיך."
    ),
    "bad_edits_file": (
        "קובץ התיקונים לא תקין. הוא צריך להיות רשימה (JSON) שבה לכל תיקון יש index של מילה קיימת "
        "ו-text של מילה אחת בלי רווחים."
    ),
    "no_media": "עוד לא בחרנו סרטון. קודם צריך לתת קישור או קובץ (הפקודה ingest).",
    "no_audio_track": "בסרטון הזה אין רצועת קול, ולכן אין מה לתמלל. נסה סרטון אחר.",
    "no_transcript": "עוד אין תמלול לסרטון. קודם צריך לתמלל אותו (הפקודה transcribe).",
    "no_clips": "עוד לא נבחרו קטעים. קודם צריך לבחור קטעים ולהריץ refine.",
    "no_rendered": "עוד לא רונדרו קליפים לבדיקה. קודם צריך להריץ render.",
    "bad_picks_file": (
        "קובץ הקטעים שנבחרו לא תקין. הוא צריך להיות רשימה (JSON) שבה לכל קטע יש "
        "start_word, end_word, score, reason ו-title."
    ),
    "hook_quote_not_verbatim": (
        "משפט הפתיח שהוצע לא מופיע במילים ובסדר המדויקים שבהם נאמר בקטע. "
        "כדי לא לעוות את מה שנאמר, לא אשתמש בו. אפשר לבחור משפט אחר מתוך הקטע, או לוותר על פתיח."
    ),
    "hook_needs_text": (
        "בחרת פתיח שמצטט משפט מהקטע, אבל עוד לא נבחר המשפט. "
        "אפשר לבחור משפט מתוך הקטע, או לעבור לפתיח של כותרת או לבלי פתיח."
    ),
    "ytdlp_403": (
        "האתר חסם את ההורדה (שגיאה 403). לרוב זה נפתר בעדכון של yt-dlp, התוכנה שמורידה את הסרטון. "
        "אפשר שאעדכן אותה ואנסה שוב (אבקש אישור קודם), או שתעלה את קובץ הווידאו בעצמך."
    ),
    "ytdlp_js_runtime": (
        "כדי להוריד מיוטיוב צריך Deno, תוכנה קטנה שמריצה קוד JavaScript, ולא מצאתי אותה במחשב. "
        "אפשר להתקין אותה (אבקש אישור), או להעלות את קובץ הווידאו בעצמך ואז היא לא נחוצה."
    ),
    "download_failed": (
        "ההורדה לא הצליחה ({reason}). בדוק שהקישור פתוח בדפדפן ושהסרטון לא פרטי. "
        "אפשר גם להוריד אותו בעצמך ולתת לי את הקובץ."
    ),
    "file_not_found": "לא מצאתי את הקובץ \"{path}\". כדאי לבדוק את הנתיב, או לגרור את הקובץ לשיחה.",
    "unreadable_media": (
        "לא הצלחתי לקרוא את הקובץ הזה כסרטון. ייתכן שהוא פגום או בפורמט נדיר. "
        "נסה קובץ אחר (mp4 הוא הבטוח ביותר)."
    ),
    "audio_only_unsupported": (
        "זה קובץ אודיו בלי תמונה. בגרסה הזו אפשר לעבוד רק עם וידאו, כי הקליפים צריכים לכלול תמונה. "
        "אם יש לך גם גרסת וידאו של ההקלטה, אפשר להשתמש בה."
    ),
    "api_key_rejected": (
        "{provider} לא קיבלו את המפתח (נראה שהוא שגוי, פג תוקף או הוקלד לא נכון). "
        "כדאי ליצור מפתח חדש באתר שלהם ולהדביק אותו בקובץ ההגדרות במקום הישן."
    ),
    "transcription_failed": (
        "התמלול דרך {provider} לא הצליח ({reason}). "
        "זה קורה לפעמים כשיש עומס, כשהאינטרנט נופל, או כשחרגו מהמכסה החינמית. "
        "אפשר לנסות שוב בעוד כמה דקות. לא עברתי למנוע אחר בלי לשאול אותך."
    ),
    "engine_not_available": (
        "מנוע התמלול \"{engine}\" עוד לא זמין בגרסה הזו. בינתיים התמלול נעשה דרך Groq בלבד, "
        "והאודיו נשלח אליהם. תמלול מקומי מתוכנן להמשך."
    ),
    "bad_profile": (
        "הפרופיל לא תקין: {reason}. "
        "אפשר להריץ את ההגדרה הראשונית מחדש ליצירת פרופיל חדש, או לתקן את הקובץ ידנית."
    ),
    "profile_missing": (
        "עוד לא נוצר פרופיל יוצר. הפרופיל שומר את התחום, השפה וההעדפות שלך, "
        "כדי שלא נצטרך לשאול אותך שוב. נריץ עכשיו את ההגדרה הראשונית."
    ),
    "bad_feedback": "המשוב לא תקין: {reason}",
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
