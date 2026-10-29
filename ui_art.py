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


def _seg_width(segs, px, S):
    return sum(font(px * S, bold=b).getlength(visual(t)) for t, b in segs)


def _draw_segs_rtl(d, segs, px, S, x_right, cy, fill):
    """מצייר קטעי טקסט (רגיל/מודגש) מימין לשמאל: הקטע הראשון הכי ימני."""
    x = x_right
    for t, b in segs:
        f = font(px * S, bold=b)
        v = visual(t)
        w = f.getlength(v)
        d.text((x - w, cy), v, font=f, fill=fill, anchor="lm")
        x -= w


def _mic(d, cx, cy, u, col):
    w, h = 4.6 * u, 8.2 * u
    d.rounded_rectangle((cx - w / 2, cy - h / 2 - 1.6 * u, cx + w / 2, cy + h / 2 - 1.6 * u), radius=w / 2, fill=col)
    r = 4.9 * u
    d.arc((cx - r, cy - r - 1.2 * u, cx + r, cy + r - 1.2 * u), 20, 160, fill=col, width=max(1, int(1.3 * u)))
    d.line(((cx, cy + 3.7 * u), (cx, cy + 6.2 * u)), fill=col, width=max(1, int(1.3 * u)))


_PANEL_CACHE = {}


def _panel(kind, payload, scale):
    """כרטיס טקסט חי / בועה / תווית — מצויר פעם אחת לכל טקסט (שמור במטמון), בגודל הסופי, כולל צל."""
    key = (kind, payload if kind != "card" else tuple(payload), round(scale, 3))
    hit = _PANEL_CACHE.get(key)
    if hit:
        return hit
    S = SS
    u = scale * S
    pad = 8 * u
    FILL, EDGE = (12, 12, 15), (255, 255, 255)
    if kind == "card":
        lines = payload
        w, h = 300 * u, len(lines) * 20 * u + 20 * u
        r = 14 * u
    else:
        segs = payload
        w, h = _seg_width(segs, 12.5 * scale, S) + 26 * u, 30 * u
        r = h / 2
    W, H = int(w + 2 * pad), int(h + 2 * pad)
    W += (S - W % S) % S; H += (S - H % S) % S
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 1.5 * u, pad + w, pad + h + 1.5 * u), radius=r, fill=90)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    img.putalpha(sh.filter(ImageFilter.GaussianBlur(4 * u)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((pad, pad, pad + w, pad + h), radius=r, fill=FILL + (242,), outline=EDGE + (45,), width=max(1, int(u)))
    if kind == "card":
        f = font(13 * scale * S)
        y = pad + 10 * u
        for ln in lines:
            v = visual(ln)
            d.text((pad + w - 14 * u - f.getlength(v), y), v, font=f, fill=(242, 242, 246, 255))
            y += 20 * u
    else:
        _draw_segs_rtl(d, segs, 12.5 * scale, S, pad + w - 13 * u, pad + h / 2, (242, 242, 246, 255))
    out = (img.resize((W // S, H // S), Image.LANCZOS), w / S, h / S, pad / S)
    if len(_PANEL_CACHE) > 64:
        _PANEL_CACHE.clear()
    _PANEL_CACHE[key] = out
    return out


_WRAP_CACHE = {}


def wrap_cached(text, scale):
    k = (text, round(scale, 3))
    if k not in _WRAP_CACHE:
        if len(_WRAP_CACHE) > 32:
            _WRAP_CACHE.clear()
        _WRAP_CACHE[k] = wrap_rtl(text, font(13 * scale * SS), (300 - 28) * scale * SS)
    return _WRAP_CACHE[k]


def render_widget(scale, state, grow=0.0, level=0.0, t=0.0, live="", bubble="", icon="", tip=None):
    """הכפתור הצף בסגנון Wispr. מחזיר (img RGBA, info). אזורי הלחיצה ב-info['hits'] בפיקסלים של החלון.
    idle: פס זעיר; grow (0..1) = מעבר חלק לגלולה עם מיקרופון + עיגול תפריט (בריחוף); tip = תווית מעל.
    rec: גלולה שחורה ✕ · נקודות גל · ✓.  busy: שלוש נקודות פועמות.  flash: ✓/! קטן + בועת הודעה."""
    S = SS
    u = scale * S
    M = 10 * u
    GAPu = 6 * u
    # מידות הגלולה
    if state == "rec":
        pw, ph = 108 * u, 28 * u
    elif state in ("busy", "flash"):
        pw, ph = 58 * u, 28 * u
    else:
        g = max(0.0, min(1.0, grow))
        e = g * g * (3 - 2 * g)     # smoothstep
        pw, ph = (36 + (58 - 36) * e) * u, (8 + (28 - 8) * e) * u
    show_menu = state == "idle" and grow > 0.01
    menu_d = 28 * u
    group_w = pw + ((GAPu + menu_d) if state == "idle" else 0)
    # טקסט מעל
    card_lines, bub_segs = [], None
    if live:
        card_lines = wrap_cached(live, scale)
    elif bubble:
        bub_segs = [(fit_line(bubble, font(12.5 * scale * S), 300 * u), False)]
    elif tip and state == "idle" and grow > 0.98:
        bub_segs = tip
    card_w = 300 * u if card_lines else 0
    card_h = (len(card_lines) * 20 * u + 20 * u) if card_lines else 0
    bub_w = (_seg_width(bub_segs, 12.5 * scale, S) + 26 * u) if bub_segs else 0
    bub_h = 30 * u if bub_segs else 0
    top_h = card_h or bub_h
    W = int(max(2 * (29 * u + GAPu + menu_d), group_w, card_w, bub_w) + 2 * M)
    H = int(M + (top_h + 8 * u if top_h else 0) + 28 * u + M)
    S2 = 2 * S
    W += (S2 - W % S2) % S2
    H += (S - H % S) % S
    # רק אזור הכפתור מצויר כאן (הטקסט שמעליו מצויר בנפרד, מהמטמון) — תמונה קטנה = מהיר
    Hp = int(28 * u + 2 * M); Hp += (S - Hp % S) % S
    Wp = int(max(2 * (29 * u + GAPu + menu_d), group_w) + 2 * M); Wp += (S2 - Wp % S2) % S2
    Wp = min(Wp, W)
    y_off, x_off = H - Hp, (W - Wp) // 2
    img = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    base = Hp - M                     # תחתית הגלולה (העוגן)
    cx = Wp / 2
    # הגלולה ממורכזת; בריחוף העיגול נוסף מימין והקבוצה כולה ממורכזת על העוגן של הגלולה
    gx0 = cx - pw / 2
    px0, px1 = gx0, gx0 + pw
    py1 = base - (28 * u - ph) / 2 if state == "idle" else base
    py0 = py1 - ph
    cy = (py0 + py1) / 2
    FILL = (12, 12, 15)
    EDGE = (255, 255, 255)
    # צל רך
    sh = Image.new("L", (Wp, Hp), 0)
    ImageDraw.Draw(sh).rounded_rectangle((px0, py0 + 1.5 * u, px1, py1 + 1.5 * u), radius=ph / 2, fill=90)
    sh = sh.filter(ImageFilter.GaussianBlur(4 * u))
    img.putalpha(sh)
    d = ImageDraw.Draw(img)
    fa = int(200 + 45 * min(1.0, grow)) if state == "idle" else 245
    d.rounded_rectangle((px0, py0, px1, py1), radius=ph / 2, fill=FILL + (fa,), outline=EDGE + (60,), width=max(1, int(u)))
    hits = {}
    white = (255, 255, 255, 255)
    if state == "idle":
        a = int(255 * max(0.0, (grow - 0.35) / 0.65))
        if a > 0:
            _mic(d, (px0 + px1) / 2, cy, u, (255, 255, 255, a))
        hits["main"] = (px0 - 4 * u, base - 28 * u, px1 + 4 * u, base + 4 * u)
        if show_menu:
            ma = int(255 * max(0.0, (grow - 0.4) / 0.6))
            mx = px1 + GAPu + menu_d / 2
            my = base - 14 * u
            r = menu_d / 2 * (0.6 + 0.4 * min(1.0, grow))
            d.ellipse((mx - r, my - r, mx + r, my + r), fill=FILL + (int(245 * ma / 255),), outline=EDGE + (int(60 * ma / 255),), width=max(1, int(u)))
            for k in (-1, 0, 1):
                dr = 1.35 * u
                d.ellipse((mx + k * 4 * u - dr, my - dr, mx + k * 4 * u + dr, my + dr), fill=(255, 255, 255, ma))
            hits["menu"] = (mx - menu_d / 2 - 2 * u, my - menu_d / 2 - 2 * u, mx + menu_d / 2 + 2 * u, my + menu_d / 2 + 2 * u)
    elif state == "rec":
        r = 10 * u
        kx = px0 + 4 * u + r
        d.ellipse((kx - r, cy - r, kx + r, cy + r), fill=(64, 64, 72, 255))
        a = 3.4 * u
        for (p, q) in (((kx - a, cy - a), (kx + a, cy + a)), ((kx - a, cy + a), (kx + a, cy - a))):
            d.line((p, q), fill=(230, 230, 236, 255), width=max(1, int(1.7 * u)))
        hits["cancel"] = (px0 - 2 * u, py0 - 2 * u, kx + r + 3 * u, py1 + 2 * u)
        ox = px1 - 4 * u - r
        d.ellipse((ox - r, cy - r, ox + r, cy + r), fill=white)
        d.line(((ox - 4.2 * u, cy + 0.3 * u), (ox - 1.2 * u, cy + 3.3 * u), (ox + 4.4 * u, cy - 3.0 * u)),
               fill=(12, 12, 15, 255), width=max(1, int(1.9 * u)), joint="curve")
        hits["main"] = (ox - r - 3 * u, py0 - 2 * u, px1 + 2 * u, py1 + 2 * u)
        n = 13
        x0, x1 = kx + r + 7 * u, ox - r - 7 * u
        step = (x1 - x0) / (n - 1)
        lv = min(1.0, level * 9)
        for i in range(n):
            env = math.sin(math.pi * (i + 0.5) / n) ** 0.7
            hh = 2.2 * u + 12 * u * lv * env * (0.45 + 0.55 * abs(math.sin(i * 1.7 + t * 11)))
            x = x0 + i * step
            d.rounded_rectangle((x - 1.1 * u, cy - hh / 2, x + 1.1 * u, cy + hh / 2), radius=1.1 * u, fill=(255, 255, 255, 235))
    elif state == "busy":
        for k in (-1, 0, 1):
            ph_ = (math.sin(t * 7 - (k + 1) * 0.9) + 1) / 2
            rr = (1.6 + 0.9 * ph_) * u
            x = (px0 + px1) / 2 + k * 7 * u
            d.ellipse((x - rr, cy - rr, x + rr, cy + rr), fill=(255, 255, 255, int(110 + 145 * ph_)))
        hits["main"] = (px0, py0, px1, py1)
    else:  # flash
        x = (px0 + px1) / 2
        if icon == "warn":
            d.rounded_rectangle((x - 1.3 * u, cy - 6.5 * u, x + 1.3 * u, cy + 2 * u), radius=1.3 * u, fill=(251, 191, 36, 255))
            d.ellipse((x - 1.6 * u, cy + 3.6 * u, x + 1.6 * u, cy + 6.8 * u), fill=(251, 191, 36, 255))
        elif icon == "ok":
            d.line(((x - 5.5 * u, cy + 0.3 * u), (x - 1.7 * u, cy + 4.1 * u), (x + 5.8 * u, cy - 4 * u)),
                   fill=(52, 211, 153, 255), width=max(1, int(2.2 * u)), joint="curve")
        else:
            _mic(d, x, cy, u, white)
        hits["main"] = (px0, py0, px1, py1)
    out = Image.new("RGBA", (W // S, H // S), (0, 0, 0, 0))
    out.alpha_composite(img.resize((Wp // S, Hp // S), Image.LANCZOS), (x_off // S, y_off // S))
    if card_lines or bub_segs:
        pimg, pw_, ph_, ppad = _panel("card", card_lines, scale) if card_lines else _panel("bub", tuple(bub_segs), scale)
        out.alpha_composite(pimg, (int(round(W / S / 2 - pw_ / 2 - ppad)), int(round(M / S - ppad))))
    out.putalpha(out.getchannel("A").point(lambda v: 0 if v < 8 else v))   # שולי צל שקופים כמעט = לא חוסמים לחיצות
    xo, yo = x_off, y_off
    hits = {k: ((b[0] + xo) / S, (b[1] + yo) / S, (b[2] + xo) / S, (b[3] + yo) / S) for k, b in hits.items()}
    hover_zone = ((min(px0, cx - 50 * u) + xo) / S, (base + yo - 40 * u) / S,
                  (max(px1 + GAPu + menu_d, cx + 50 * u) + xo) / S, (base + yo + 8 * u) / S)
    return out, {"hits": hits, "margin": M / S, "body": ((px0 + xo) / S, (py0 + yo) / S, (px1 + xo) / S, (py1 + yo) / S), "hover_zone": hover_zone,
                 "card_h": (card_h or bub_h) / S, "has_card": bool(card_lines)}


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


def _bubble(f0, f1, dur, vol, decay=22.0, body=0.0):
    """"פופ" רך כמו בווידג'טים מודרניים: סינוס נקי שגולש בתדר, התקפה רכה ודעיכה מהירה.
    body = מעט צליל נמוך מתחת (אוקטבה) לחמימות."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    k = np.clip(t / (dur * 0.35), 0, 1)
    f = f0 + (f1 - f0) * (1 - (1 - k) ** 2)          # גלישה שמאטה בסוף
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = np.sin(ph) + body * np.sin(ph / 2)
    env = np.minimum(1, t / 0.008) * np.exp(-decay * t)
    fade = np.minimum(1, (dur - t) / 0.01)            # בלי "קליק" בסוף
    return x * env * fade * vol


def make_sounds(volume=0.35):
    v = volume * 0.75   # שקט יותר מהגרסה הקודמת
    return {
        # התחלה: "פופ" קצר עולה
        "start": _wav(_bubble(520, 880, 0.16, v, decay=24, body=0.25)),
        # עצירה: אותו פופ בירידה
        "stop": _wav(_bubble(820, 500, 0.16, v * 0.9, decay=24, body=0.25)),
        # הטקסט נכנס: טיפה עדינה וגבוהה
        "done": _wav(_bubble(1250, 1500, 0.09, v * 0.35, decay=45)),
        # ביטול: פופ נמוך יורד
        "cancel": _wav(_bubble(480, 330, 0.16, v * 0.7, decay=22, body=0.2)),
        # שגיאה: שני פופים נמוכים
        "error": _wav(_seq((0.0, _bubble(420, 380, 0.14, v * 0.8, decay=20, body=0.3)),
                           (0.13, _bubble(360, 300, 0.18, v * 0.8, decay=18, body=0.3)))),
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
