"""POST /api/summary — {"system": str, "user": str, "max_tokens": int} -> {"text": str}
סיכום עם Claude דרך השרת, כדי שהמפתח של Anthropic יישאר רק ב-Vercel (ANTHROPIC_API_KEY)."""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
import _auth  # noqa: E402

MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-5-5")


def _call(system, user, max_tokens, thinking):
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    if thinking:
        body["thinking"] = thinking
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                          "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=110) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if not _auth.check(self):
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _auth.send(self, 503, {"error": "summary not configured (ANTHROPIC_API_KEY missing on server)"})
        try:
            req = _auth.body(self)
            system, user = str(req.get("system", ""))[:20000], str(req.get("user", ""))[:400000]
            mt = max(256, min(int(req.get("max_tokens", 2000)), 8000))
            last = None
            for th in ({"type": "disabled"}, None):   # כמו באפליקציה: בלי חשיבה = מהיר; אם המודל לא מכיר — בלי הפרמטר
                try:
                    return _auth.send(self, 200, {"text": _call(system, user, mt, th)})
                except urllib.error.HTTPError as e:
                    last = f"{e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
                    if e.code != 400 or "thinking" not in last:
                        break
            return _auth.send(self, 502, {"error": last})
        except Exception as e:
            return _auth.send(self, 500, {"error": f"{type(e).__name__}: {e}"})
