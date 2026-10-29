"""POST /api/transcribe — תואם לפורמט הישן של האפליקציה:
   {"audio_blob": <base64 wav/ogg/flac>, "method": "ivrit"}  ->  {"transcription": "..."}
הרחבות: "diarize": true (+ "num_speakers") -> {"job_id"} ; מתשאלים ב-GET /api/job?id=...
אודיו קצר מחכה לתשובה (runsync); אם RunPod עוד לא סיים (הפעלה קרה) מחזירים job_id."""
import sys
import os
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402
import _runpod  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _auth.check(self):
            return
        try:
            req = _auth.body(self)
            blob, url = req.get("audio_blob"), req.get("url")
            if not blob and not url:
                return _auth.send(self, 400, {"error": "audio_blob or url required"})
            diarize = bool(req.get("diarize"))
            p = _runpod.payload(blob_b64=blob, url=url, diarize=diarize,
                                language=req.get("language", "he"), num_speakers=req.get("num_speakers"),
                                diar_engine=req.get("diar_engine"), min_speakers=req.get("min_speakers"),
                                max_speakers=req.get("max_speakers"))
            if diarize or req.get("async"):
                resp = _runpod.submit(p)
                return _auth.send(self, 200, {"job_id": resp.get("id"), "status": "pending"})
            res = _runpod.result_from(_runpod.submit(p, sync_wait_s=float(req.get("wait_s", 60))))
            if res["status"] == "done":
                return _auth.send(self, 200, {"transcription": res["text"], "segments": res["segments"],
                                              "timing": res.get("timing")})
            if res["status"] == "pending":
                return _auth.send(self, 202, {"job_id": res["job_id"], "status": "pending"})
            return _auth.send(self, 502, {"error": res.get("error")})
        except Exception as e:
            return _auth.send(self, 500, {"error": f"{type(e).__name__}: {e}"})
