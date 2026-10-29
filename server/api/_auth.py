import hmac
import json
import os


def check(handler):
    """Bearer token של האפליקציה (FLOWHE_TOKEN ב-Vercel). מחזיר True אם תקין, אחרת שולח 401."""
    want = os.environ.get("FLOWHE_TOKEN", "")
    got = handler.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if want and hmac.compare_digest(want, got):
        return True
    send(handler, 401, {"error": "unauthorized"})
    return False


def send(handler, code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def body(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    return json.loads(handler.rfile.read(n) or b"{}")
