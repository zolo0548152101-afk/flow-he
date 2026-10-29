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
