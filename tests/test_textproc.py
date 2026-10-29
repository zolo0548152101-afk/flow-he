from textproc import apply_dictionary, apply_snippets

REPL = {"ורסל": "Vercel", "קלוד": "Claude", "גיטהאב": "GitHub", "קלוד קוד": "Claude Code"}
SNIP = {"חתימה שלי": "בברכה,\nהשם שלך", "תודה ונדבר": "תודה רבה, נדבר בהמשך השבוע."}


def test_whole_word():
    assert apply_dictionary("העליתי ורסל", REPL) == "העליתי Vercel"


def test_hebrew_prefix_kept():
    assert apply_dictionary("העליתי לורסל ולגיטהאב", REPL) == "העליתי ל-Vercel ול-GitHub"


def test_longest_wins():
    assert apply_dictionary("פתחתי קלוד קוד", REPL) == "פתחתי Claude Code"


def test_no_substring_match():
    assert apply_dictionary("ורסלים", REPL) == "ורסלים"


def test_snippet_alone():
    assert apply_snippets("תודה ונדבר", SNIP) == "תודה רבה, נדבר בהמשך השבוע."


def test_snippet_in_sentence_with_punct():
    assert apply_snippets("אוקיי, חתימה, שלי.", SNIP) == "אוקיי, בברכה,\nהשם שלך."


def test_snippet_not_inside_word():
    assert apply_snippets("חתימהשלי", SNIP) == "חתימהשלי"


from textproc import clean_chunk


def test_clean_chunk_drops_hallucinations():
    assert clean_chunk("תודה רבה.") == ""
    assert clean_chunk("Thanks for watching!") == ""
    assert clean_chunk("[מוזיקה]") == ""
    assert clean_chunk("  ") == ""


def test_clean_chunk_keeps_real_text():
    assert clean_chunk("תודה רבה על העזרה עם הפרויקט") == "תודה רבה על העזרה עם הפרויקט"
    assert clean_chunk("שלום, מה שלומך?") == "שלום, מה שלומך?"


def test_clean_chunk_strips_credits_and_loops():
    assert clean_chunk("נפגשים מחר בשמונה. תורגם על ידי ישראל ישראלי") == "נפגשים מחר בשמונה."
    assert clean_chunk("כן כן כן כן כן כן") == "כן"


from textproc import guard_words, tidy


def test_guard_rejects_word_swaps():
    raw = "ארבע מאות זוז שמי יקנית את הילד? צדיק. לא זה."
    llm = "ארבע מאות שקל, שמי יקנה את הילד? צודק? לא, זה לא זה."
    out = guard_words(raw, llm)
    assert "זוז," in out and "שקל" not in out
    assert "יקנית" in out and "יקנה" not in out
    assert "צודק" not in out and "צדיק" in out
    assert out.split().__len__() == raw.split().__len__()


def test_guard_takes_punctuation_and_paragraphs():
    raw = "שלום מה שלומך אני בסדר תודה"
    llm = "שלום, מה שלומך?\n\nאני בסדר, תודה."
    assert guard_words(raw, llm) == "שלום, מה שלומך?\n\nאני בסדר, תודה."


def test_guard_allows_filler_and_repeat_removal_only():
    assert guard_words("אה אני רוצה רוצה ללכת", "אני רוצה ללכת.") == "אני רוצה ללכת."
    # מחיקה של מילה אמיתית (לא חזרה) — חוזרת
    assert guard_words("אני לא רוצה ללכת", "אני רוצה ללכת.") == "אני לא רוצה ללכת."


def test_tidy():
    assert tidy("שלום  עולם .  מה  נשמע") == "שלום עולם. מה נשמע"
