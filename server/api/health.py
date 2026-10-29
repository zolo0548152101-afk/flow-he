"""GET /api/health — no secrets: config status + short fingerprints for debugging auth."""
import hashlib
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402


def _fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:8]


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        tok = os.environ.get("FLOWHE_TOKEN", "")
        got = self.headers.get("Authorization", "")
        _auth.send(self, 200, {
            "ok": True, "service": "flow-he-server",
            "runpod_configured": bool(os.environ.get("RUNPOD_API_KEY") and os.environ.get("RUNPOD_ENDPOINT_ID")),
            "app_token_configured": bool(tok), "token_fp": _fp(tok), "token_len": len(tok),
            "auth_fp": _fp(got.removeprefix("Bearer ").strip()) if got else None,
            "model": os.environ.get("IVRIT_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2"),
        })
