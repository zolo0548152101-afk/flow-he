"""חיתוך הקלטה לקטעים בזמן אמת, בנקודות שקט — כדי לשלוח לתמלול תוך כדי דיבור.

החוק: קטע נחתך כשיש שקט של לפחות `silence_s` אחרי לפחות `min_s` שניות,
או בכוח אחרי `max_s` שניות (בנקודה השקטה ביותר בשנייה האחרונה), כדי לא לחתוך באמצע מילה.
"""
import numpy as np


class Chunker:
    def __init__(self, sr=16000, min_s=4.0, max_s=14.0, silence_s=0.45, frame_s=0.03,
                 abs_thresh=0.008, rel_thresh=0.25):
        self.sr, self.min_n, self.max_n = sr, int(min_s * sr), int(max_s * sr)
        self.frame = int(frame_s * sr)
        self.sil_frames = max(1, int(silence_s / frame_s))
        self.abs_thresh, self.rel_thresh = abs_thresh, rel_thresh
        self.buf = np.zeros(0, dtype="float32")
        self.speech_level = 0.0   # ממוצע נע של עוצמת דיבור, לסף שקט יחסי
        self.offset = 0           # כמה דגימות כבר נחתכו (למעקב)

    def _rms_frames(self, x):
        n = len(x) // self.frame
        if n == 0:
            return np.zeros(0)
        f = x[: n * self.frame].reshape(n, self.frame)
        return np.sqrt((f ** 2).mean(axis=1))

    def _thresh(self):
        return max(self.abs_thresh, self.rel_thresh * self.speech_level)

    def feed(self, x):
        """מוסיף אודיו; מחזיר רשימת קטעים מוכנים לשליחה."""
        x = np.asarray(x, dtype="float32").reshape(-1)
        self.buf = np.concatenate([self.buf, x])
        rms = self._rms_frames(x)
        loud = rms[rms > self._thresh()]
        if len(loud):
            self.speech_level = 0.9 * self.speech_level + 0.1 * float(loud.mean()) if self.speech_level else float(loud.mean())
        out = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            out.append(self.buf[:cut].copy())
            self.buf = self.buf[cut:]
            self.offset += cut
        return out

    def _find_cut(self):
        if len(self.buf) < self.min_n:
            return None
        rms = self._rms_frames(self.buf)
        quiet = rms < self._thresh()
        start_f = self.min_n // self.frame
        run = 0
        for i in range(start_f, len(quiet)):
            run = run + 1 if quiet[i] else 0
            if run >= self.sil_frames:
                mid = i - run // 2          # חותכים באמצע השקט
                return mid * self.frame
        if len(self.buf) >= self.max_n:     # אין שקט — חותכים בנקודה השקטה ביותר בשנייה האחרונה
            lo = max(start_f, (self.max_n - self.sr) // self.frame)
            hi = self.max_n // self.frame
            seg = rms[lo:hi]
            return (lo + int(np.argmin(seg))) * self.frame if len(seg) else self.max_n
        return None

    def flush(self):
        """שארית ההקלטה (בסוף)."""
        rest, self.buf = self.buf, np.zeros(0, dtype="float32")
        return rest


def is_silent(x, thresh=0.006) -> bool:
    """קטע שכולו שקט — לא שולחים לשרת."""
    x = np.asarray(x).reshape(-1)
    return len(x) == 0 or float(np.sqrt((x ** 2).mean())) < thresh
