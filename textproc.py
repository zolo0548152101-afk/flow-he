"""עיבוד טקסט טהור (בלי תלות ב-Windows): מילון תיקונים וסניפטים. נבדק ב-tests/."""
import re

_PREFIX = "ובלמשהכ"  # אותיות שימוש עבריות שיכולות להידבק לפני מילה


def _wb(phrase: str) -> str:
    """גבול-מילה שעובד גם בעברית."""
    return r"(?<![\w\"'])" + re.escape(phrase) + r"(?![\w\"'])"


def apply_dictionary(text: str, replacements: dict) -> str:
    """תיקונים 'אמור X כתוב Y': מילה שלמה, לא רגיש לרישיות, הביטוי הארוך מנצח.
    שומר אותיות שימוש עבריות לפני המילה (לורסל -> ל-Vercel)."""
    for wrong, right in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        pat = r"(?<![\w\"'])([" + _PREFIX + r"]{0,2})" + re.escape(wrong) + r"(?![\w\"'])"
        text = re.sub(
            pat,
            lambda m: m.group(1) + ("-" if m.group(1) and right[:1].isascii() else "") + right,
            text,
            flags=re.IGNORECASE,
        )
    return text


def apply_snippets(text: str, snippets: dict) -> str:
    """ביטוי-מפעיל של סניפט, לבד או בתוך משפט, מוחלף בטקסט המלא. הארוך מנצח.
    סובלני לפסיקים/נקודות שהמתמלל מוסיף בין מילות המפעיל."""
    for trigger, body in sorted(snippets.items(), key=lambda kv: -len(kv[0])):
        trig = re.sub(r"[^\w\s]", "", trigger).strip()
        if not trig or not body:
            continue
        inner = r"[\s,.!?]+".join(map(re.escape, trig.split()))
        pattern = r"(?<![\w\"'])" + inner + r"(?![\w\"'])"
        text = re.sub(pattern, lambda m: body, text, flags=re.IGNORECASE)
    return text


# ביטויים ש-Whisper "ממציא" על שקט/רעש (בעיקר מכתוביות של סרטונים). קטע שכולו אחד מאלה נזרק.
HALLUCINATIONS = {
    "תודה", "תודה רבה", "תודה רבה לכם", "תודה שצפיתם", "תודה על הצפייה", "תודה שצפיתם בסרטון",
    "תודה רבה על הצפייה", "תודה ולהתראות", "להתראות", "ביי", "ביי ביי", "אמן", "שלום",
    "כתוביות", "תרגום", "תורגם", "כתוביות על ידי", "תרגום וכתוביות", "הירשמו לערוץ",
    "אל תשכחו להירשם לערוץ", "מוזיקה", "מוסיקה", "צחוק", "מחיאות כפיים",
    "thank you", "thanks for watching", "thank you for watching", "you", "bye", "music",
}
_CREDITS = re.compile(r"\s*(תורגם|תרגום|כתוביות|סונכרן|הוכן)\s*(על ידי|ע\"י|ע״י|:)[^.!?\n]*[.!?]?\s*$")


def _norm(t: str) -> str:
    return re.sub(r"[^\w\s]", " ", t).strip().lower()


def clean_chunk(text: str) -> str:
    """מנקה את הפלט של קטע אחד: זורק המצאות מוכרות, קרדיטים של כתוביות וחזרות לולאה."""
    t = (text or "").strip()
    if not t:
        return ""
    t = _CREDITS.sub("", t).strip()
    n = re.sub(r"\s+", " ", _norm(t))
    if not n or n in HALLUCINATIONS or re.fullmatch(r"[\[\(].*[\]\)]", t):
        return ""
    # לולאת חזרה: אותה מילה/צמד מילים 4+ פעמים ברצף -> פעם אחת
    t = re.sub(r"(\b[\w\"']+(?:\s+[\w\"']+)?)(?:[\s,.]+\1\b){3,}", r"\1", t)
    return t.strip()
