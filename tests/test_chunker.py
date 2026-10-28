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
