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


# ---------------------------------------------------------------- שומר מילים
# מילות מילוי שמותר למחוק (רק אלה — "כאילו", "יעני" וכו' יכולים להיות בעלי משמעות, לא נוגעים בהם)
FILLERS = {"אה", "אהה", "אההה", "אמ", "אממ", "אמממ", "אם-אם", "הממ", "המ", "אמם", "eh", "uh", "um", "umm", "hmm"}
_WORD = re.compile(r"[^\s]+")
_PUNCT_EDGE = re.compile(r"^([\"'(\[«״]*)(.*?)([.,!?:;\"')\]»״…]*)$")


def _split(tok):
    m = _PUNCT_EDGE.match(tok)
    return m.group(1), m.group(2), m.group(3)


def _norm_word(w):
    return re.sub(r"[֑-ׇ]", "", w).lower()


def guard_words(raw: str, edited: str) -> str:
    """מחזיר טקסט שכל המילים בו לקוחות מהתמלול המקורי, בסדר המקורי.
    מהעריכה לוקחים רק: פיסוק צמוד למילים, מעברי פסקה, ומחיקה של מילות מילוי / חזרה מיידית על מילים.
    כל החלפה או הוספה של מילה מבוטלת — המילה המקורית חוזרת."""
    import difflib
    rt = _WORD.findall(raw)
    if not rt:
        return raw.strip()
    # טוקנים של העריכה, עם סימון מעבר פסקה לפניהם
    et, para = [], []
    for block_i, block in enumerate(re.split(r"\n\s*\n", edited.strip())):
        for j, tok in enumerate(_WORD.findall(block)):
            et.append(tok); para.append(block_i > 0 and j == 0)
    rw = [_norm_word(_split(t)[1]) for t in rt]
    ew = [_norm_word(_split(t)[1]) for t in et]
    out = []          # (pre, word, post, para_break)
    sm = difflib.SequenceMatcher(a=rw, b=ew, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                pre_r, w, post_r = _split(rt[i1 + k])
                pre_e, _, post_e = _split(et[j1 + k])
                out.append([pre_e, w, post_e, para[j1 + k]])
        elif op == "delete":
            span = rw[i1:i2]
            prev = [o[1] for o in out][-len(span):] if out else []
            ok = all(w in FILLERS for w in span) or [_norm_word(x) for x in prev] == span
            if not ok:  # מחיקה של מילה אמיתית — מחזירים אותה
                for k in range(i1, i2):
                    pre, w, post = _split(rt[k]); out.append([pre, w, post, False])
        else:  # replace / insert — המילים המקוריות חוזרות, עם הפיסוק המקורי
            for k in range(i1, i2):
                pre, w, post = _split(rt[k]); out.append([pre, w, post, False])
            if op == "replace" and out and j2 > j1:
                # שומרים את סימן הפיסוק שהעריכה שמה בסוף הקטע (למשל נקודה בסוף משפט)
                _, _, post_e = _split(et[j2 - 1])
                if post_e and not out[-1][2]:
                    out[-1][2] = post_e
    parts = []
    for pre, w, post, pb in out:
        if parts:
            parts.append("\n\n" if pb else " ")
        parts.append(pre + w + post)
    return "".join(parts).strip()


def tidy(text: str) -> str:
    """ניקוי דטרמיניסטי של פלט התמלול: רווחים כפולים, רווח לפני פיסוק."""
    t = re.sub(r"[ \t]{2,}", " ", text or "")
    t = re.sub(r" +([.,!?:;])", r"\1", t)
    return t.strip()
