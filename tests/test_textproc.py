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
