"""זיהוי דוברים לפי הקול, מקומית על המעבד (sherpa-onnx, בלי אינטרנט ובלי השרת).

- OnlineSpeakers: בזמן הפגישה — לכל קטע מחשבים "טביעת קול" (embedding) ומשייכים לדובר הקרוב,
  או פותחים דובר חדש. כיול על קובץ בדיקה של 4 דוברים: אותו דובר >= 0.44, דוברים שונים <= 0.25 -> סף 0.38.
- diarize: אחרי הפגישה — חלוקה מלאה של ההקלטה לקטעי דיבור לפי דובר (pyannote segmentation + 3D-Speaker),
  סף 0.9 שחזר בדיוק את החלוקה של קובץ הבדיקה. אפשר לקבוע מספר דוברים ידוע.
"""
import os
import threading

import numpy as np

SEG_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
EMB_MODEL = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"


def models_dir(*roots):
    for r in roots:
        d = os.path.join(r, "models")
        if os.path.exists(os.path.join(d, EMB_MODEL)) and os.path.exists(os.path.join(d, SEG_MODEL)):
            return d
    return None


class OnlineSpeakers:
    def __init__(self, mdir, threshold=0.38, threads=2):
        import sherpa_onnx as so
        self.ex = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
            model=os.path.join(mdir, EMB_MODEL), num_threads=threads))
        self.th = threshold
        self.cents = []          # מרכזי הדוברים (סכום embeddings מנורמלים)
        self.lock = threading.Lock()

    def embed(self, audio, sr=16000):
        st = self.ex.create_stream()
        st.accept_waveform(sr, np.asarray(audio, dtype="float32"))
        st.input_finished()
        e = np.array(self.ex.compute(st), dtype="float32")
        n = np.linalg.norm(e)
        return e / n if n else e

    def assign(self, audio, sr=16000):
        """מחזיר מספר דובר (0, 1, 2...) לקטע."""
        if len(audio) < sr * 0.8:
            return None  # קצר מדי לזיהוי אמין
        e = self.embed(audio, sr)
        with self.lock:
            if self.cents:
                sims = [float(c @ e / (np.linalg.norm(c) or 1)) for c in self.cents]
                k = int(np.argmax(sims))
                if sims[k] >= self.th:
                    self.cents[k] = self.cents[k] + e
                    return k
            self.cents.append(e.copy())
            return len(self.cents) - 1

    def match(self, e):
        """לאיזה דובר חי מתאים embedding (למיפוי שמות מהשלב החי לשלב הסופי), או None."""
        with self.lock:
            if not self.cents:
                return None
            sims = [float(c @ e / (np.linalg.norm(c) or 1)) for c in self.cents]
            k = int(np.argmax(sims))
            return k if sims[k] >= self.th else None


def diarize(mdir, audio, num_speakers=-1, sr=16000, threshold=0.9, threads=2):
    """[(start_s, end_s, speaker)] לפי סדר הזמן."""
    import sherpa_onnx as so
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=os.path.join(mdir, SEG_MODEL)),
            num_threads=threads),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=os.path.join(mdir, EMB_MODEL), num_threads=threads),
        clustering=so.FastClusteringConfig(num_clusters=int(num_speakers), threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = so.OfflineSpeakerDiarization(cfg)
    r = sd.process(np.asarray(audio, dtype="float32")).sort_by_start_time()
    return [(float(s.start), float(s.end), int(s.speaker)) for s in r]


def turns(segs, max_s=28.0, gap_s=1.5):
    """מאחד קטעים רצופים של אותו דובר לתורות דיבור (עד max_s שניות כל אחת)."""
    out = []
    for a, b, k in segs:
        if out and out[-1][2] == k and a - out[-1][1] <= gap_s and b - out[-1][0] <= max_s:
            out[-1] = (out[-1][0], b, k)
        else:
            out.append((a, b, k))
    return out
