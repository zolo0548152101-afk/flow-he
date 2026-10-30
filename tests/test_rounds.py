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


def test_continuous_cuts_lose_nothing():
    """ספירה/הכתבה: הקלטה אחת רציפה, חיתוך בשקט של 2 שניות — הקטעים צמודים, סכום האורכים = כל ההקלטה."""
    import numpy as np
    from chunker import Chunker
    sr = 16000
    t = np.arange(int(sr * 40)) / sr
    speaking = ((t % 6.0) < 3.5) | ((t > 20) & (t < 27))   # דיבור, הפסקות של 2.5 שניות, וקטע דיבור ארוך
    x = (0.2 * np.sin(2 * np.pi * 220 * t) * speaking).astype("float32")
    ch = Chunker(sr, min_s=1.0, max_s=15.0, silence_s=2.0, cut_at="start", start_pad_s=0.4)
    out = []
    for i in range(0, len(x), 1600):
        out += ch.feed(x[i:i + 1600])
    out.append(ch.flush())
    assert sum(len(g) for g in out) == len(x)          # אין חור ואין חפיפה
    assert np.array_equal(np.concatenate(out), x)      # בדיוק אותו אודיו, לפי הסדר
    assert len([g for g in out if len(g) > sr]) >= 4   # באמת נחתך בהפסקות


def test_filler_only_dropped_real_words_kept():
    import textproc
    for f in ("אההההה", "אממ", "אה, אה", "הממ"):
        assert textproc.clean_chunk(f) == ""
    for w in ("אמא", "אם", "מה", "תשע, עשר"):
        assert textproc.clean_chunk(w) == w
