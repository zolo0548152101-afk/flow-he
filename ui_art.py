"""אמנות: לוגו, ציור הבר החלק (anti-aliased) וצלילים רכים — הכול נוצר בקוד, בלי קבצים חיצוניים.

PIL מצייר פי SS ומקטין: כך מתקבלים קצוות חלקים במקום הפיקסלים המדורגים של ה-Canvas של Tk.
"""
import math
import struct

import numpy as np
from PIL import Image, ImageDraw

SS = 4  # supersampling

# פלטה
VIOLET = (124, 92, 255)
SKY = (63, 169, 245)
PILL_BG = (24, 24, 30)
PILL_EDGE = (58, 58, 70)
REC = (244, 63, 94)
DIM_BAR = (62, 62, 76)


def _grad(w, h, c0, c1):
    """מעבר צבע אלכסוני."""
    x = np.linspace(0, 1, w)[None, :]
    y = np.linspace(0, 1, h)[:, None]
    t = np.clip((x * 0.6 + y * 0.4), 0, 1)[..., None]
    arr = (np.array(c0) * (1 - t) + np.array(c1) * t).astype("uint8")
    return Image.fromarray(np.concatenate([arr, np.full((h, w, 1), 255, "uint8")], axis=2), "RGBA")


def make_logo(size=256):
    """ריבוע מעוגל בגרדיאנט סגול-תכלת, ובתוכו חמישה פסי גל קול לבנים מעוגלים."""
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.23), fill=255)
    img.paste(_grad(S, S, VIOLET, SKY), (0, 0), mask)
    # ברק עדין למעלה
    gloss = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gloss).ellipse((-S * 0.3, -S * 0.75, S * 1.3, S * 0.42), fill=38)
    gloss = Image.composite(gloss, Image.new("L", (S, S), 0), mask)
    img = Image.alpha_composite(img, Image.merge("RGBA", (gloss.point(lambda _: 255),) * 3 + (gloss,)))
    d = ImageDraw.Draw(img)
    heights = [0.26, 0.46, 0.62, 0.46, 0.26]
    bw = S * 0.085
    gap = S * 0.06
    total = len(heights) * bw + (len(heights) - 1) * gap
    x = (S - total) / 2
    for h in heights:
        hh = S * h
        d.rounded_rectangle((x, (S - hh) / 2, x + bw, (S + hh) / 2), radius=bw / 2, fill=(255, 255, 255, 255))
        x += bw + gap
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path):
    make_logo(256).save(path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


# ---------------------------------------------------------------- הבר
def _rr(d, box, r, fill, outline=None, width=0):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def render_bar(w, h, scale, state, oy=0, level=0.0, t=0.0, card_h=0, bg_key=(1, 2, 3), ss=None):
    """מצייר את הבר (ואם יש — כרטיס טקסט חי מעליו) כתמונה חלקה בגודל w x h פיקסלים פיזיים.
    הרקע מחוץ לצורות בצבע bg_key שהופך לשקוף בחלון (transparentcolor)."""
    S = ss or SS
    W, H = w * S, h * S
    img = Image.new("RGB", (W, H), bg_key)
    d = ImageDraw.Draw(img)
    u = scale * S                      # יחידה לוגית -> פיקסל-על
    if card_h:
        _rr(d, (0, 0, W - 1, card_h * S - 4 * u), 14 * u, PILL_BG, PILL_EDGE, max(1, int(1 * u)))
    py0, py1 = oy * S, H - 1
    _rr(d, (0, py0, W - 1, py1), (py1 - py0) / 2, PILL_BG, PILL_EDGE, max(1, int(1 * u)))
    cy = (py0 + py1) / 2
    cx = 24 * u
    r = 14 * u
    if state == "rec":
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=REC)
        s = 4.5 * u
        _rr(d, (cx - s, cy - s, cx + s, cy + s), 1.5 * u, (255, 255, 255))
        n, x0, step, bw = 18, 48 * u, 7 * u, 4 * u
        lv = min(1.0, level * 8)
        for i in range(n):
            hh = (4 + 18 * lv * (0.4 + 0.6 * abs(math.sin(i * 0.9 + t * 9)))) * u
            frac = i / n
            col = tuple(int(VIOLET[k] * (1 - frac) + SKY[k] * frac) for k in range(3)) if (frac < lv or i < 2) else DIM_BAR
            _rr(d, (x0 + i * step, cy - hh / 2, x0 + i * step + bw, cy + hh / 2), bw / 2, col)
    elif state in ("busy", "flash"):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(44, 44, 54))
        if state == "busy":  # ספינר: קשת מסתובבת
            a = (t * 360) % 360
            rr = 7 * u
            d.arc((cx - rr, cy - rr, cx + rr, cy + rr), a, a + 260, fill=SKY, width=int(2.4 * u))
        else:
            _logo_mark(d, cx, cy, 7 * u, (220, 220, 235))
    else:  # idle
        grad = _grad(int(2 * r), int(2 * r), VIOLET, SKY)
        m = Image.new("L", grad.size, 0)
        ImageDraw.Draw(m).ellipse((0, 0, grad.size[0] - 1, grad.size[1] - 1), fill=255)
        img.paste(grad.convert("RGB"), (int(cx - r), int(cy - r)), m)
        _logo_mark(d, cx, cy, 7 * u, (255, 255, 255))
    return img.resize((w, h), Image.LANCZOS)


def _logo_mark(d, cx, cy, size, col):
    heights = [0.45, 0.8, 1.0, 0.8, 0.45]
    bw = size * 0.26
    gap = size * 0.16
    total = len(heights) * bw + (len(heights) - 1) * gap
    x = cx - total / 2
    for hgt in heights:
        hh = size * 1.6 * hgt
        d.rounded_rectangle((x, cy - hh / 2, x + bw, cy + hh / 2), radius=bw / 2, fill=col)
        x += bw + gap


# ---------------------------------------------------------------- הווידג'ט הקומפקטי (שקיפות אמיתית לכל פיקסל)
import re as _re
from PIL import ImageFilter, ImageFont

_FONTS = {}
OK_GREEN = (52, 211, 153)
WARN = (251, 191, 36)
BADGE = (46, 46, 58)
BADGE_EDGE = (84, 84, 102)
BODY = (26, 26, 33)
BODY_EDGE = (62, 62, 76)
BODY_EDGE_HOVER = (96, 96, 120)

# מידות לוגיות (מוכפלות ב-scale)
MARGIN = 12     # מסביב לגוף: צל + תגיות הריחוף
SQ = 48         # הריבוע במצב רגיל
REC_W = 200     # הגלולה בזמן הקלטה
CARD_W = 300    # כרטיס הטקסט החי
GAP = 8


def font(px, bold=False):
    key = (int(px), bold)
    if key not in _FONTS:
        names = (["segoeuib.ttf", "seguisb.ttf"] if bold else ["segoeui.ttf"]) + \
                (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"])
        f = None
        for n in names:
            for d in ("C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/", ""):
                try:
                    f = ImageFont.truetype(d + n, int(px), layout_engine=ImageFont.Layout.BASIC); break
                except Exception:
                    continue
            if f:
                break
        _FONTS[key] = f or ImageFont.load_default()
    return _FONTS[key]


def visual(s):
    """סדר תצוגה (bidi) — PIL מצייר משמאל לימין, אז עברית צריך להפוך לפני הציור."""
    try:
        from bidi.algorithm import get_display
        return get_display(s, base_dir="R")
    except Exception:
        return s


def strip_icon(s):
    """מוריד אימוג'י/סמלים מתחילת הודעה (האייקון מצויר בנפרד)."""
    return _re.sub(r"^[^\w\u0590-\u05FF\"'(]+", "", s or "").strip()


def wrap_rtl(text, f, max_w, max_lines=3):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and f.getlength(visual(cand)) > max_w:
            lines.append(cur); cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        lines[0] = "…" + lines[0]
    return lines


def fit_line(text, f, max_w):
    if f.getlength(visual(text)) <= max_w:
        return text
    while text and f.getlength(visual(text + "…")) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _shadow(size, box, r, blur, alpha, dy):
    m = Image.new("L", size, 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(m).rounded_rectangle((x0, y0 + dy, x1, y1 + dy), radius=r, fill=alpha)
    return m.filter(ImageFilter.GaussianBlur(blur))


def render_widget(scale, state, hover=False, level=0.0, t=0.0, secs=0.0, live="", bubble="", icon="logo"):
    """מצייר את כל החלון הצף כ-RGBA (שקיפות אמיתית, קצוות חלקים).
    מחזיר (img, info): info['hits'] = אזורי לחיצה בפיקסלים של החלון, info['margin'] = שוליים מתחת לגוף."""
    S = SS
    sc = scale
    u = sc * S
    M = MARGIN * u
    body_w = (REC_W if state == "rec" else SQ) * u
    body_h = SQ * u
    # טקסט חי / בועה
    card_lines, bub = [], ""
    fpx = 13 * sc
    if live:
        f = font(fpx * S)
        card_lines = wrap_rtl(live, f, (CARD_W - 28) * u)
    elif bubble:
        f = font(12.5 * sc * S)
        bub = fit_line(bubble, f, 300 * u)
    card_w = CARD_W * u if card_lines else 0
    card_h = (len(card_lines) * 20 * u + 20 * u) if card_lines else 0
    bub_w = (font(12.5 * sc * S).getlength(visual(bub)) + 28 * u) if bub else 0
    bub_h = 32 * u if bub else 0
    top_h = card_h or bub_h
    content_w = max(body_w, card_w, bub_w)
    W = int(content_w + 2 * M)
    H = int(M + (top_h + GAP * u if top_h else 0) + body_h + M)
    W += W % S and S - W % S  # מתחלק ב-S
    H += H % S and S - H % S
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bx0 = (W - body_w) / 2
    by1 = H - M
    by0 = by1 - body_h
    bx1 = bx0 + body_w
    br = body_h / 2 if state == "rec" else 15 * u

    # צללים
    sh = _shadow((W, H), (bx0, by0, bx1, by1), br, 6 * u, 120, 2 * u)
    panels = []
    if card_lines:
        panels.append(((W - card_w) / 2, M, (W + card_w) / 2, M + card_h, 14 * u))
    elif bub:
        panels.append(((W - bub_w) / 2, M, (W + bub_w) / 2, M + bub_h, bub_h / 2))
    for p in panels:
        s2 = _shadow((W, H), p[:4], p[4], 6 * u, 110, 2 * u)
        sh = Image.fromarray(np.maximum(np.asarray(sh), np.asarray(s2)))
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.putalpha(sh)
    d = ImageDraw.Draw(img)

    # כרטיס / בועה
    for (x0, y0, x1, y1, r) in panels:
        d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=BODY + (250,), outline=BODY_EDGE + (255,), width=max(1, int(u)))
    if card_lines:
        f = font(fpx * S)
        y = M + 10 * u
        for ln in card_lines:
            v = visual(ln)
            d.text(((W + card_w) / 2 - 14 * u - f.getlength(v), y), v, font=f, fill=(242, 242, 246, 255))
            y += 20 * u
    elif bub:
        f = font(12.5 * sc * S)
        v = visual(bub)
        d.text(((W - f.getlength(v)) / 2, M + bub_h / 2), v, font=f, fill=(242, 242, 246, 255), anchor="lm")

    # הגוף
    edge = BODY_EDGE_HOVER if hover else BODY_EDGE
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=br, fill=BODY + (255,), outline=edge + (255,), width=max(1, int(1.2 * u)))
    cy = (by0 + by1) / 2
    hits = {}
    if state == "rec":
        # מימין: עצירה; משמאל: ביטול; באמצע: גל קול ושעון
        sx = bx1 - 26 * u
        r = 16 * u
        d.ellipse((sx - r, cy - r, sx + r, cy + r), fill=REC + (255,))
        s = 5 * u
        d.rounded_rectangle((sx - s, cy - s, sx + s, cy + s), radius=1.8 * u, fill=(255, 255, 255, 255))
        hits["main"] = (sx - 22 * u, by0, bx1, by1)
        kx = bx0 + 24 * u
        r = 12 * u
        d.ellipse((kx - r, cy - r, kx + r, cy + r), fill=BADGE + (255,))
        a = 4.2 * u
        for (p, q) in (((kx - a, cy - a), (kx + a, cy + a)), ((kx - a, cy + a), (kx + a, cy - a))):
            d.line((p, q), fill=(200, 200, 212, 255), width=int(1.8 * u))
        hits["cancel"] = (bx0, by0, kx + 16 * u, by1)
        n, x0, step, bw = 11, bx0 + 46 * u, 6.5 * u, 3.6 * u
        lv = min(1.0, level * 8)
        for i in range(n):
            hh = (4 + 20 * lv * (0.35 + 0.65 * abs(math.sin(i * 0.9 + t * 9)))) * u
            frac = i / n
            col = tuple(int(VIOLET[k] * (1 - frac) + SKY[k] * frac) for k in range(3)) if (frac < lv + 0.08) else DIM_BAR
            d.rounded_rectangle((x0 + i * step, cy - hh / 2, x0 + i * step + bw, cy + hh / 2), radius=bw / 2, fill=col + (255,))
        ft = font(12.5 * sc * S, bold=True)
        d.text((bx1 - 50 * u, cy), f"{int(secs // 60)}:{int(secs % 60):02d}", font=ft, fill=(242, 242, 246, 255), anchor="rm")
    else:
        cx = (bx0 + bx1) / 2
        r = 15 * u
        hits["main"] = (bx0, by0, bx1, by1)
        if state == "busy":
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(40, 40, 50, 255))
            a = (t * 360) % 360
            rr = 11 * u
            d.arc((cx - rr, cy - rr, cx + rr, cy + rr), a, a + 250, fill=SKY + (255,), width=int(2.6 * u))
            _logo_mark(d, cx, cy, 4.5 * u, (150, 150, 170, 255))
        elif icon == "ok":
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=OK_GREEN + (255,))
            d.line(((cx - 6 * u, cy + 0.5 * u), (cx - 1.8 * u, cy + 4.6 * u), (cx + 6.5 * u, cy - 4.5 * u)),
                   fill=(18, 40, 32, 255), width=int(2.6 * u), joint="curve")
        elif icon == "warn":
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WARN + (255,))
            d.rounded_rectangle((cx - 1.4 * u, cy - 7.5 * u, cx + 1.4 * u, cy + 2.5 * u), radius=1.4 * u, fill=(40, 30, 10, 255))
            d.ellipse((cx - 1.7 * u, cy + 4.3 * u, cx + 1.7 * u, cy + 7.7 * u), fill=(40, 30, 10, 255))
        else:
            g = _grad(int(2 * r), int(2 * r), VIOLET, SKY)
            m = Image.new("L", g.size, 0)
            ImageDraw.Draw(m).ellipse((0, 0, g.size[0] - 1, g.size[1] - 1), fill=255)
            img.paste(g, (int(cx - r), int(cy - r)), m)
            d = ImageDraw.Draw(img)
            _logo_mark(d, cx, cy, 7 * u, (255, 255, 255, 255))
        if hover and state in ("idle", "flash"):
            br_ = 9.5 * u
            for name, (bx, by) in (("menu", (bx0 + 3 * u, by0 + 3 * u)), ("hide", (bx1 - 3 * u, by0 + 3 * u))):
                d.ellipse((bx - br_, by - br_, bx + br_, by + br_), fill=BADGE + (255,), outline=BADGE_EDGE + (255,), width=max(1, int(u)))
                if name == "hide":
                    d.line(((bx - 4 * u, by), (bx + 4 * u, by)), fill=(225, 225, 235, 255), width=int(1.8 * u))
                else:
                    for k in (-1, 0, 1):
                        dr = 1.3 * u
                        d.ellipse((bx + k * 3.6 * u - dr, by - dr, bx + k * 3.6 * u + dr, by + dr), fill=(225, 225, 235, 255))
                hits[name] = (bx - br_ - 2 * u, by - br_ - 2 * u, bx + br_ + 2 * u, by + br_ + 2 * u)
    out = img.resize((W // S, H // S), Image.LANCZOS)
    hits = {k: tuple(v / S for v in b) for k, b in hits.items()}
    body = (bx0 / S, by0 / S, bx1 / S, by1 / S)
    return out, {"hits": hits, "margin": M / S, "body": body, "card_h": (card_h or bub_h) / S, "has_card": bool(card_lines)}


# ---------------------------------------------------------------- צלילים
SR = 44100


def _tone(freqs, dur, vol, attack=0.006, decay=9.0, glide=None):
    """צליל רך: סינוס + הרמוניות חלשות, התקפה קצרה ודעיכה מעריכית (כמו מרימבה/פעמון קטן)."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for i, f in enumerate(freqs):
        f_t = f if glide is None else f * (1 + glide * t / dur)
        ph = 2 * np.pi * np.cumsum(np.full(n, 1.0) * f_t) / SR
        out += (np.sin(ph) + 0.18 * np.sin(2 * ph) + 0.05 * np.sin(3 * ph)) / (1 + i * 0.6)
    env = np.minimum(1, t / attack) * np.exp(-decay * t)
    return out * env * vol


def _seq(*parts):
    """צלילים ברצף עם חפיפה: (offset_s, samples)."""
    end = max(int(o * SR) + len(x) for o, x in parts)
    out = np.zeros(end + int(0.02 * SR))
    for o, x in parts:
        i = int(o * SR)
        out[i:i + len(x)] += x
    return out


def _wav(x):
    x = np.clip(x / max(1.0, float(np.abs(x).max()) / 0.9), -1, 1)
    pcm = (x * 32767).astype("<i2").tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16)
    return hdr + b"data" + struct.pack("<I", len(pcm)) + pcm


def make_sounds(volume=0.35):
    v = volume
    return {
        # התחלה: שני צלילים עולים ורכים (E5 -> B5)
        "start": _wav(_seq((0.0, _tone([659.3], 0.22, v)), (0.07, _tone([987.8], 0.30, v * 0.9)))),
        # עצירה: אותו מוטיב בירידה
        "stop": _wav(_seq((0.0, _tone([987.8], 0.20, v * 0.85)), (0.065, _tone([659.3], 0.30, v * 0.8)))),
        # סיום: "טיק" עדין וקצר
        "done": _wav(_tone([1318.5], 0.12, v * 0.45, decay=28)),
        # ביטול
        "cancel": _wav(_tone([523.3], 0.18, v * 0.6, decay=16, glide=-0.15)),
        # שגיאה: שני צלילים נמוכים
        "error": _wav(_seq((0.0, _tone([329.6], 0.18, v * 0.8, decay=14)), (0.14, _tone([277.2], 0.24, v * 0.8, decay=12)))),
    }


if __name__ == "__main__":  # תצוגה מקדימה
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    make_logo(256).save(f"{out}/logo.png")
    save_ico(f"{out}/flowhe.ico")
    for st in ("idle", "rec", "busy"):
        render_bar(450, 69, 1.5, st, level=0.08, t=0.3).save(f"{out}/bar_{st}.png")
    for k, b in make_sounds().items():
        open(f"{out}/sound_{k}.wav", "wb").write(b)
