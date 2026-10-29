import hmac
import json
import os


def users():
    """{token: name}. FLOWHE_TOKEN = הבעלים; FLOWHE_USERS = "שם:טוקן;שם2:טוקן2" — קוד הפעלה אישי לכל חבר
    (אפשר לבטל אחד בלי לגעת באחרים, ואף אחד מהם לא רואה את המפתחות של RunPod/Claude)."""
    out = {}
    own = os.environ.get("FLOWHE_TOKEN", "")
    if own:
        out[own] = "owner"
    for part in os.environ.get("FLOWHE_USERS", "").replace(",", ";").split(";"):
        if ":" in part:
            name, tok = part.split(":", 1)
            if tok.strip():
                out[tok.strip()] = name.strip()
    return out


def check(handler):
    """Bearer token. מחזיר את שם המשתמש אם תקין (ונרשם ללוג של Vercel), אחרת שולח 401 ומחזיר None."""
    got = handler.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    for tok, name in users().items():
        if got and hmac.compare_digest(tok, got):
            print(f"user={name} path={handler.path}", flush=True)
            return name
    send(handler, 401, {"error": "unauthorized"})
    return None


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
