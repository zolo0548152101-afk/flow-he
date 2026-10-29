import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import summary


def test_prompt_has_structure_and_transcript():
    sysm, user = summary.build_prompt("lecture", "short", "שלום עולם", "שיעור 1")
    assert "שאלות לחזרה" in user and "שלום עולם" in user and "קצר" in user
    assert "אל תמציא" in sysm


def test_transcript_from_entries_groups_speakers():
    t = summary.transcript_from_entries([(0, "אני", "היי", "mic"), (3, "אני", "מה נשמע", "mic"), (65, "דובר 2", "טוב", "sys")])
    assert t.count("אני:") == 1 and "[1:05] דובר 2:" in t


def test_parse_and_docx():
    md = "## החלטות\n- לעבור לשרת\n  - בשבוע הבא\nטקסט חופשי"
    assert [k for k, _ in summary.parse(md)] == ["h", "li", "li2", "p"]
    p = os.path.join(tempfile.mkdtemp(), "s.docx")
    summary.write_docx(p, "סיכום", "תת", md)
    assert "לעבור לשרת" in summary.transcript_from_docx(p)
