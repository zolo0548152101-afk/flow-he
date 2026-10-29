"""POST /api/warm — מעיר את ה-worker ב-RunPod בלי לחכות (האפליקציה קוראת לזה ברגע שלוחצים על מקש ההקלטה,
כך שעד שהקטע הראשון מוכן — השרת כבר ער). עבודה של חצי שנייה שקט: כמה מילישניות GPU."""
import base64
import os
import struct
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402
import _runpod  # noqa: E402


def _silence_wav(sec=0.5, sr=16000):
    n = int(sec * sr)
    pcm = b"\x00\x00" * n
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    return base64.b64encode(hdr + b"data" + struct.pack("<I", len(pcm)) + pcm).decode()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _auth.check(self):
            return
        try:
            resp = _runpod.submit(_runpod.payload(blob_b64=_silence_wav()))
            return _auth.send(self, 200, {"ok": True, "job_id": resp.get("id")})
        except Exception as e:
            return _auth.send(self, 500, {"error": f"{type(e).__name__}: {e}"})
