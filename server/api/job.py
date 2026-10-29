"""GET /api/job?id=<job_id> -> {"status": "pending"|"done"|"error", "transcription", "segments"}"""
import sys
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402
import _runpod  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not _auth.check(self):
            return
        try:
            job = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            if not job:
                return _auth.send(self, 400, {"error": "id required"})
            res = _runpod.result_from(_runpod.status(job))
            if res["status"] == "done":
                res["transcription"] = res.pop("text")
            return _auth.send(self, 200, res)
        except Exception as e:
            return _auth.send(self, 500, {"error": f"{type(e).__name__}: {e}"})
