import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _rounds_text():
    import textproc
    return textproc.rounds_text


def test_overlap_dropped_by_time_not_by_value():
    rounds_text = _rounds_text()
    # הקטע נשלח עם זנב של שנייה מהקטע הקודם. "11" בזנב כבר דווח; "11" אחרי הזנב הוא סיבוב חדש — נשאר
    segs = [{"start": 0.2, "end": 0.7, "text": "11"},
            {"start": 1.4, "end": 1.9, "text": "11"},
            {"start": 2.5, "end": 3.0, "text": "12"}]
    assert rounds_text(segs, lead_s=1.0) == "11 12"


def test_segment_straddling_boundary_is_kept():
    rounds_text = _rounds_text()
    # מספר שהתחיל רגע לפני סוף הזנב (נחתך בקצה הקטע הקודם) — נשאר, בזכות מרווח החסד
    segs = [{"start": 0.8, "end": 1.3, "text": "17"}, {"start": 2.0, "end": 2.4, "text": "18"}]
    assert rounds_text(segs, lead_s=1.0) == "17 18"


def test_no_lead_keeps_everything():
    rounds_text = _rounds_text()
    segs = [{"start": 0.0, "end": 0.5, "text": "11"}, {"start": 0.9, "end": 1.3, "text": "11"}]
    assert rounds_text(segs, lead_s=0.0) == "11 11"
    assert rounds_text([], lead_s=1.0, fallback="13 14") == "13 14"
