import numpy as np
from chunker import Chunker, is_silent

SR = 16000


def speech(sec, amp=0.2, f=220):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype("float32")


def silence(sec):
    return np.zeros(int(sec * SR), dtype="float32")


def feed_realtime(ch, audio, block=0.1):
    out = []
    n = int(block * SR)
    for i in range(0, len(audio), n):
        out += ch.feed(audio[i:i + n])
    return out


def test_cuts_at_pauses():
    audio = np.concatenate([speech(5), silence(0.8), speech(5), silence(0.8), speech(3)])
    ch = Chunker()
    chunks = feed_realtime(ch, audio)
    assert len(chunks) == 2                       # שתי הפסקות -> שני קטעים נחתכו תוך כדי
    rest = ch.flush()
    total = sum(len(c) for c in chunks) + len(rest)
    assert total == len(audio)                    # לא אבד אף דגימה
    for c in chunks:                              # כל חיתוך באמצע שקט, לא באמצע דיבור
        assert abs(c[-1]) < 1e-6


def test_no_cut_before_min():
    audio = np.concatenate([speech(1.5), silence(0.8), speech(1.5)])
    assert feed_realtime(Chunker(), audio) == []


def test_forced_cut_without_pauses():
    audio = speech(30)
    ch = Chunker(max_s=14)
    chunks = feed_realtime(ch, audio)
    assert len(chunks) == 2 and all(len(c) <= 14 * SR for c in chunks)


def test_one_minute_streams():
    # דקה של דיבור עם הפסקות טבעיות כל ~6 שניות -> הרבה קטעים נשלחים לפני הסוף
    parts = []
    for _ in range(9):
        parts += [speech(6), silence(0.6)]
    audio = np.concatenate(parts)
    ch = Chunker()
    chunks = feed_realtime(ch, audio)
    assert len(chunks) >= 8
    assert len(ch.flush()) < 8 * SR               # מה שנשאר לסוף קצר — התמלול הסופי מהיר


def test_is_silent():
    assert is_silent(silence(1)) and not is_silent(speech(1))


def test_quiet_mic_speech_is_not_silent():
    # פרופיל אמיתי ממיקרופון שקט: רעש 0.001, דיבור ~0.008, ממוצע כולל < 0.006
    rng = np.random.default_rng(1)
    sr = 16000
    noise = rng.normal(0, 0.001, int(1.2 * sr))
    speech = rng.normal(0, 0.008, int(0.9 * sr))
    x = np.concatenate([noise[: sr // 2], speech, noise[sr // 2:]]).astype("float32")
    assert float(np.sqrt((x ** 2).mean())) < 0.006
    assert not is_silent(x)


def test_room_noise_is_silent():
    rng = np.random.default_rng(2)
    assert is_silent(rng.normal(0, 0.0015, 3 * 16000).astype("float32"))
    assert is_silent(np.zeros(16000, dtype="float32"))


def test_continuous_speech_is_not_silent():
    rng = np.random.default_rng(3)
    assert not is_silent(rng.normal(0, 0.012, 6 * 16000).astype("float32"))   # מיקרופון שקט, בלי הפסקות
    assert not is_silent(np.full(16000, 0.05, dtype="float32"))               # אות קבוע (CI)
    sr = 16000; tt = np.arange(5 * sr) / sr
    assert not is_silent((0.2 * np.sin(2 * np.pi * 220 * tt)).astype("float32"))


def test_fan_noise_is_silent():
    rng = np.random.default_rng(4)
    assert is_silent(rng.normal(0, 0.005, 4 * 16000).astype("float32"))


def test_rounds_cuts_only_on_long_silence():
    # מצב ספירה: "13 12 11" עם הפסקות קצרות בין המספרים — לא חותכים; שקט של 3.5 שניות — חותכים
    parts = [speech(0.6), silence(1.0), speech(0.6), silence(1.2), speech(0.6), silence(3.5),
             speech(0.6), silence(1.0), speech(0.6)]
    audio = np.concatenate(parts)
    ch = Chunker(min_s=1.0, max_s=45.0, silence_s=3.0)
    chunks = feed_realtime(ch, audio)
    assert len(chunks) == 1                       # רק השקט הארוך חותך
    first = sum(len(p) for p in parts[:5])
    assert abs(len(chunks[0]) - (first + int(1.5 * SR))) < 0.2 * SR   # החיתוך באמצע השקט הארוך
    assert abs(chunks[0][-1]) < 1e-6
    assert len(chunks[0]) + len(ch.flush()) == len(audio)


def test_rounds_forced_cut_without_long_silence():
    # לא עוצרים לדבר במשך דקה — חותכים בכוח אחרי max_s, בנקודה השקטה ביותר, ולא מאבדים דגימות
    audio = np.concatenate([speech(20), silence(1.0), speech(30)])
    ch = Chunker(min_s=1.0, max_s=45.0, silence_s=3.0)
    chunks = feed_realtime(ch, audio)
    assert len(chunks) == 1 and len(chunks[0]) <= 45 * SR
    assert len(chunks[0]) + len(ch.flush()) == len(audio)
