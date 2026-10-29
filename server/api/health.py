"""GET /api/health — no secrets: config status only."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        tok = os.environ.get("FLOWHE_TOKEN", "")
        _auth.send(self, 200, {
            "ok": True, "service": "flow-he-server",
            "runpod_configured": bool(os.environ.get("RUNPOD_API_KEY") and os.environ.get("RUNPOD_ENDPOINT_ID")),
            "app_token_configured": bool(tok),
            "model": os.environ.get("IVRIT_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2"),
        })
