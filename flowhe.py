"""
Flow-HE — הכתבה קולית בעברית בסגנון Wispr Flow, על גבי שירות התמלול שלך בוורסל.

שכבה 1: תמלול  — השירות שלך (method: ivrit).
שכבה 2: עיבוד   — Claude (אופציונלי, דרך ANTHROPIC_API_KEY): ניקוי מילות מילוי, פיסוק,
                  תיקונים תוך כדי דיבור ("לא, תמחק את זה"), Command Mode, סגנון לפי אפליקציה.

קבצים (ליד הסקריפט):
  .env              URL + טוקן + מפתח Claude
  config.json       מקשים, סגנונות לפי אפליקציה, פרטיות, צלילים
  dictionary.json   מילון אישי: מילים שחייבות להיכתב נכון + החלפות
  snippets.json     סניפטים: ביטוי מפעיל -> טקסט מוכן
  history.jsonl     היסטוריית תמלולים (אלא אם privacy_mode)
"""
import base64
import ctypes
import io
import math
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime

import keyboard
import numpy as np
import pyautogui
import pyperclip
import requests
import sounddevice as sd
import soundfile as sf

try:
    import winsound
except ImportError:  # לא Windows
    winsound = None

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

import tkinter as tk

import textproc

# תיקיית הנתונים: ליד ה-EXE כשארוז (PyInstaller), ליד הסקריפט כשרץ כ-.py
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)  # קבצי ברירת מחדל שנארזו בתוך ה-EXE
P = lambda *a: os.path.join(HERE, *a)
SELFTEST = os.environ.get("FLOWHE_SELFTEST") == "1"

# תחת pythonw / EXE בלי קונסולה אין stdout: מפנים ל-flow.log
if sys.stdout is None or sys.stderr is None:
    _logf = open(P("flow.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _logf

# בהרצה ראשונה: מעתיקים קבצי ברירת מחדל ליד ה-EXE כדי שיהיה אפשר לערוך אותם
for _name, _src in (("config.json", "config.json"), ("dictionary.json", "dictionary.json"),
                    ("snippets.json", "snippets.json"), (".env", ".env.example")):
    if not os.path.exists(P(_name)) and os.path.exists(os.path.join(BUNDLE, _src)):
        try:
            import shutil
            shutil.copyfile(os.path.join(BUNDLE, _src), P(_name))
        except OSError:
            pass

# מופע יחיד: מופע חדש מבקש מהישן לצאת ותופס את מקומו
QUIT_FLAG = P("quit.flag")
if sys.platform == "win32" and not SELFTEST:
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\FlowHE_single_instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        open(QUIT_FLAG, "w").close()
        ctypes.windll.kernel32.CloseHandle(_mutex)
        for _ in range(40):
            time.sleep(0.25)
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\FlowHE_single_instance")
            if ctypes.windll.kernel32.GetLastError() != 183:
                break
            ctypes.windll.kernel32.CloseHandle(_mutex)
        else:
            sys.exit("Flow-HE כבר רץ ולא הגיב לבקשת סגירה")
    try:
        os.remove(QUIT_FLAG)
    except OSError:
        pass

# ---------------------------------------------------------------- הגדרות
def load_env():
    if os.path.exists(P(".env")):
        with open(P(".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def load_json(name, default):
    if os.path.exists(P(name)):
        with open(P(name), encoding="utf-8") as f:
            return json.load(f)
    return default


load_env()
URL = os.environ.get("TRANSCRIBE_URL")
TOKEN = os.environ.get("TRANSCRIBE_TOKEN")
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY")
MISSING_CREDS = not URL or not TOKEN

CFG = load_json("config.json", {})
HK = CFG.get("hotkeys", {})
HK_TOGGLE = HK.get("toggle", "ctrl+alt+space")
HK_HOLD = HK.get("push_to_talk", "right alt")
HK_COMMAND = HK.get("command_mode", "ctrl+alt+c")
HK_SCRATCH = HK.get("scratchpad", "ctrl+alt+n")
HK_CANCEL = HK.get("cancel", "esc")
SAMPLE_RATE = 16000
AI_EDITS = CFG.get("ai_auto_edits", True) and bool(CLAUDE_KEY) and anthropic is not None
MODEL = CFG.get("model", "claude-haiku-5-5")
PRIVACY = CFG.get("privacy_mode", False)
SOUNDS = CFG.get("sounds", True)
APP_STYLES = CFG.get("app_styles", {})
OUTPUT_TARGET = CFG.get("output_target", "paste")  # paste | clipboard | window
if OUTPUT_TARGET not in ("paste", "clipboard", "window"):
    OUTPUT_TARGET = "paste"
BAR_POS = CFG.get("flow_bar_pos")  # [x, y] או None = מרכז-תחתית


def save_config():
    CFG["output_target"] = OUTPUT_TARGET
    CFG["flow_bar_pos"] = BAR_POS
    try:
        with open(P("config.json"), "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("שמירת config נכשלה:", e)
DEFAULT_STYLE = CFG.get("default_style", "טבעי, ברור, כמו שהמשתמש היה כותב בעצמו")

DICT = load_json("dictionary.json", {"words": [], "replacements": {}})
SNIPPETS = load_json("snippets.json", {})

claude = anthropic.Anthropic(api_key=CLAUDE_KEY) if AI_EDITS else None
ui_q: "queue.Queue[tuple]" = queue.Queue()


def log(*a):
    try:
        print(f"[{datetime.now():%H:%M:%S}]", *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- צלילים / חלון פעיל
def beep(kind):
    if not SOUNDS or not winsound:
        return
    freq = {"start": 880, "stop": 660, "done": 1040, "error": 300}[kind]
    threading.Thread(target=winsound.Beep, args=(freq, 90), daemon=True).start()


def active_window_title() -> str:
    try:
        h = ctypes.windll.user32.GetForegroundWindow()
        n = ctypes.windll.user32.GetWindowTextLengthW(h) + 1
        buf = ctypes.create_unicode_buffer(n)
        ctypes.windll.user32.GetWindowTextW(h, buf, n)
        return buf.value
    except Exception:
        return ""


def style_for(title: str) -> str:
    t = title.lower()
    for key, style in APP_STYLES.items():
        if key.lower() in t:
            return style
    return DEFAULT_STYLE


# ---------------------------------------------------------------- הקלטה
class Recorder:
    def __init__(self):
        self.chunks, self.stream, self.on = [], None, False
        self.level = 0.0      # RMS של הצ'אנק האחרון (0..1) למד העוצמה
        self.target_hwnd = 0  # החלון שהיה פעיל כשההקלטה התחילה — לשם מדביקים
        self.t0 = time.time()

    def _cb(self, indata, frames, t, status):
        if self.on:
            self.chunks.append(indata.copy())
            self.level = float(np.sqrt(np.mean(indata ** 2)))

    def start(self):
        try:
            self.target_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            self.target_hwnd = 0
        self.chunks, self.on, self.level = [], True, 0.0
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self._cb)
        self.stream.start()
        self.t0 = time.time()

    def stop(self):
        self.on = False
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        if not self.chunks:
            return None, 0.0
        audio = np.concatenate(self.chunks, axis=0)
        # מצב לחישה: נרמול עוצמה כדי שדיבור שקט יתומלל טוב
        peak = float(np.abs(audio).max()) or 1.0
        if peak < 0.3:
            audio = audio * (0.6 / peak)
        return audio, time.time() - self.t0


rec = Recorder()
lock = threading.Lock()
mode = None  # None | "dictate" | "command" | "scratch"


# ---------------------------------------------------------------- תמלול + עיבוד
def transcribe(audio) -> str:
    if MISSING_CREDS:
        raise RuntimeError("חסרים TRANSCRIBE_URL / TRANSCRIBE_TOKEN בקובץ .env")
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"audio_blob": base64.b64encode(buf.getvalue()).decode(), "method": "ivrit"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("transcription") or "").strip()


def apply_dictionary(text: str) -> str:
    return textproc.apply_dictionary(text, DICT.get("replacements", {}))


def apply_snippets(text: str) -> str:
    return textproc.apply_snippets(text, SNIPPETS)


def dictionary_prompt() -> str:
    words = DICT.get("words", [])
    return ("מילים/שמות שחייבים להיכתב בדיוק כך: " + ", ".join(words) + "\n") if words else ""


def ai_clean(raw: str, style: str) -> str:
    if not claude:
        return raw
    system = (
        "אתה עורך הכתבה קולית בעברית. תקבל תמלול גולמי של דיבור ותחזיר אך ורק את הטקסט הסופי, "
        "בלי הקדמות, בלי הסברים, בלי מרכאות.\n"
        "כללים:\n"
        "- הסר מילות מילוי (אה, אמם, כאילו, יעני, אוקיי בתחילת משפט) וחזרות/גמגומים.\n"
        "- הוסף פיסוק ופסקאות. רשימות שהוכתבו ('אחד... שתיים...') הפוך לרשימה.\n"
        "- כבד תיקונים תוך כדי דיבור: 'לא, תמחק את זה', 'בעצם', 'רגע, לא' — השאר רק את הגרסה המתוקנת.\n"
        "- אל תוסיף תוכן, אל תסכם, אל תשנה משמעות. השאר מילים באנגלית באנגלית.\n"
        f"- סגנון: {style}\n" + dictionary_prompt()
    )
    msg = claude.messages.create(model=MODEL, max_tokens=2000, system=system,
                                 messages=[{"role": "user", "content": raw}])
    return msg.content[0].text.strip()


def ai_command(selected: str, command: str) -> str:
    system = (
        "אתה עוזר עריכה קולי. המשתמש בחר טקסט ואמר פקודה. בצע את הפקודה על הטקסט "
        "והחזר אך ורק את התוצאה, בלי הסברים. אם הפקודה היא שאלה ולא עריכה — ענה בקצרה.\n"
        + dictionary_prompt()
    )
    user = f"<text>\n{selected}\n</text>\n<command>{command}</command>" if selected else command
    msg = claude.messages.create(model=MODEL, max_tokens=2000, system=system,
                                 messages=[{"role": "user", "content": user}])
    return msg.content[0].text.strip()


# ---------------------------------------------------------------- פלט
def focus_target():
    h = rec.target_hwnd
    if not h:
        return
    try:
        u = ctypes.windll.user32
        if u.GetForegroundWindow() != h:
            u.keybd_event(0x12, 0, 0, 0); u.keybd_event(0x12, 0, 2, 0)  # טריק Alt כדי ש-Windows יאפשר החלפת פוקוס
            u.SetForegroundWindow(h)
            time.sleep(0.15)
    except Exception as e:
        log("focus_target:", e)


def deliver(text: str):
    """מוציא את הטקסט לפי יעד הפלט שנבחר בבר."""
    if OUTPUT_TARGET == "clipboard":
        pyperclip.copy(text)
        ui_q.put(("flash", "📋 הועתק ללוח"))
    elif OUTPUT_TARGET == "window":
        pyperclip.copy(text)
        ui_q.put(("result", text))
    else:
        focus_target()
        paste(text)


def paste(text: str):
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.06)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.4)
    if old is not None:
        try: pyperclip.copy(old)
        except Exception: pass


def get_selection() -> str:
    try:
        before = pyperclip.paste()
    except Exception:
        before = ""
    pyperclip.copy("\x00FLOW\x00")
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.15)
    sel = pyperclip.paste()
    if sel == "\x00FLOW\x00":
        sel = ""
    pyperclip.copy(before)
    return sel


def log_history(kind, raw, final, secs, app):
    if PRIVACY:
        return
    with open(P("history.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "app": app,
                            "raw": raw, "final": final, "seconds": round(secs, 1),
                            "words": len(final.split())}, ensure_ascii=False) + "\n")


def stats():
    words = secs = n = 0
    if os.path.exists(P("history.jsonl")):
        for line in open(P("history.jsonl"), encoding="utf-8"):
            try:
                d = json.loads(line); words += d["words"]; secs += d["seconds"]; n += 1
            except Exception:
                pass
    wpm = round(words / (secs / 60), 1) if secs else 0
    return f"{n} הכתבות · {words} מילים · {wpm} מילים/דקה"


# ---------------------------------------------------------------- זרימות
def begin(kind):
    global mode
    with lock:
        if mode:
            return
        mode = kind
        try:
            rec.start()
        except Exception as e:
            mode = None
            log("שגיאת מיקרופון:", repr(e)); beep("error")
            ui_q.put(("flash", f"שגיאת מיקרופון: {e}")); return
    log("הקלטה התחילה:", kind)
    beep("start")
    ui_q.put(("rec", kind))


def finish():
    global mode
    with lock:
        if not mode:
            return
        kind, mode = mode, None
        audio, secs = rec.stop()
    beep("stop")
    log(f"הקלטה נגמרה: {secs:.1f}s, דגימות={0 if audio is None else len(audio)}, שיא={0 if audio is None else float(np.abs(audio).max()):.3f}")
    if audio is None or secs < 0.4:
        log("קצר מדי / אין אודיו"); ui_q.put(("hide", None)); return
    ui_q.put(("show", "⏳ מתמלל..."))
    threading.Thread(target=process, args=(kind, audio, secs), daemon=True).start()


def cancel():
    global mode
    with lock:
        if not mode:
            return
        mode = None
        rec.stop()
    log("בוטל")
    ui_q.put(("flash", "✖ בוטל"))


def process(kind, audio, secs):
    app = active_window_title()
    try:
        try:
            sf.write(P("last.wav"), audio, SAMPLE_RATE, subtype="PCM_16")
        except Exception:
            pass
        t0 = time.time()
        raw = transcribe(audio)
        log(f"תמלול ({time.time()-t0:.1f}s) מתוך [{app}]: {raw!r}")
        if not raw:
            ui_q.put(("flash", "לא זוהה דיבור")); return
        if kind == "command":
            selected = get_selection()
            if not claude:
                ui_q.put(("flash", "Command Mode דורש ANTHROPIC_API_KEY")); return
            ui_q.put(("show", "🪄 מבצע..."))
            final = ai_command(selected, raw)
            deliver(final)
        else:
            final = apply_dictionary(raw)
            if AI_EDITS:
                ui_q.put(("show", "✨ מנקה..."))
                final = apply_dictionary(ai_clean(final, style_for(app)))
            final = apply_snippets(final)
            if kind == "scratch":
                with open(P("scratchpad.md"), "a", encoding="utf-8") as f:
                    f.write(f"\n### {datetime.now():%d/%m/%Y %H:%M}\n{final}\n")
            else:
                deliver(final)
        log(f"פלט ({OUTPUT_TARGET}):", repr(final))
        log_history(kind, raw, final, secs, app)
        beep("done")
        if OUTPUT_TARGET != "window":
            ui_q.put(("flash", "✅ " + (final[:60] + ("…" if len(final) > 60 else ""))))
    except Exception as e:
        import traceback
        log("שגיאה:", traceback.format_exc())
        beep("error")
        ui_q.put(("flash", f"שגיאה: {e}"))


# ---------------------------------------------------------------- Flow Bar (חלון צף)
BG, FG, ACCENT, REC, DIM = "#141414", "#f2f2f2", "#4ade80", "#ef4444", "#8a8a8a"
TARGET_LABELS = {"paste": "הדבק בסמן", "clipboard": "העתק ללוח", "window": "הצג בחלון"}


def hwnd_of(win):
    try:
        return ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
    except Exception:
        return 0


def make_noactivate(win):
    """החלון לא גונב פוקוס מהאפליקציה שבה כותבים (WS_EX_NOACTIVATE + TOOLWINDOW)."""
    try:
        u = ctypes.windll.user32
        h = u.GetAncestor(win.winfo_id(), 2)
        GWL_EXSTYLE = -20
        st = u.GetWindowLongW(h, GWL_EXSTYLE)
        u.SetWindowLongW(h, GWL_EXSTYLE, st | 0x08000000 | 0x00000080)
        u.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
    except Exception as e:
        log("make_noactivate:", e)


class FlowBar:
    """הבר הצף: לחיצה על המיקרופון מתחילה הקלטה, לחיצה על ■ מסיימת ומדביקה, ✕ מבטל.
    ▾ בוחר לאן הטקסט הולך. גרירה בכל מקום על הבר מזיזה אותו."""

    W, H = 300, 46

    def __init__(self, root):
        self.root = root
        root.title("Flow-HE")
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-alpha", 0.96)
        root.configure(bg=BG)
        self.c = tk.Canvas(root, width=self.W, height=self.H, bg=BG, highlightthickness=0, cursor="hand2")
        self.c.pack()
        self.state = "idle"       # idle | rec | busy | flash
        self.flash_job = None
        self.menu = None
        self.result_win = None
        self._drag = None
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<B1-Motion>", self._drag_move)
        self.c.bind("<ButtonRelease-1>", self._release)
        self._place()
        root.deiconify()
        root.update()
        if CFG.get("bar_noactivate", True):
            root.after(300, lambda: make_noactivate(root))
        self.draw()
        self._tick()

    # ---- מיקום
    def _place(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if BAR_POS and 0 <= BAR_POS[0] < sw and 0 <= BAR_POS[1] < sh:
            x, y = BAR_POS
        else:
            x, y = (sw - self.W) // 2, sh - self.H - 60
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")

    # ---- ציור
    def _pill(self, x0, y0, x1, y1, fill, outline=""):
        r = (y1 - y0) / 2
        self.c.create_oval(x0, y0, x0 + 2 * r, y1, fill=fill, outline=outline)
        self.c.create_oval(x1 - 2 * r, y0, x1, y1, fill=fill, outline=outline)
        self.c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=outline)

    def draw(self, level=0.0, secs=0.0):
        c = self.c
        c.delete("all")
        self._pill(1, 1, self.W - 1, self.H - 1, "#1e1e1e", "#333")
        cy = self.H / 2
        # כפתור ראשי (שמאל)
        if self.state == "rec":
            c.create_oval(10, cy - 14, 38, cy + 14, fill=REC, outline="", tags="main")
            c.create_rectangle(19, cy - 5, 29, cy + 5, fill="white", outline="", tags="main")  # ■
            # מד עוצמה
            n, x = 18, 48
            lv = min(1.0, level * 8)
            for i in range(n):
                h = 4 + 18 * lv * (0.4 + 0.6 * abs(math.sin(i * 0.9 + secs * 9)))
                col = ACCENT if i / n < lv or i < 2 else "#3a3a3a"
                c.create_rectangle(x + i * 7, cy - h / 2, x + i * 7 + 4, cy + h / 2, fill=col, outline="")
            c.create_text(190, cy, text=f"{int(secs // 60)}:{int(secs % 60):02d}", fill=FG, font=("Segoe UI", 11, "bold"), anchor="w")
            # ✕ ביטול
            c.create_text(self.W - 22, cy, text="✕", fill=DIM, font=("Segoe UI", 13), tags="cancel")
        elif self.state == "busy":
            c.create_oval(10, cy - 14, 38, cy + 14, fill="#333", outline="", tags="main")
            c.create_text(24, cy, text="⏳", fill=FG, font=("Segoe UI Emoji", 12), tags="main")
            c.create_text(48, cy, text=self.msg, fill=FG, font=("Segoe UI", 10), anchor="w")
        elif self.state == "flash":
            c.create_oval(10, cy - 14, 38, cy + 14, fill="#333", outline="", tags="main")
            c.create_text(24, cy, text="🎙", fill=FG, font=("Segoe UI Emoji", 12), tags="main")
            c.create_text(48, cy, text=self.msg, fill=FG, font=("Segoe UI", 10), anchor="w", width=self.W - 60)
        else:  # idle
            c.create_oval(10, cy - 14, 38, cy + 14, fill=ACCENT, outline="", tags="main")
            c.create_text(24, cy, text="🎙", fill="#111", font=("Segoe UI Emoji", 12), tags="main")
            c.create_text(48, cy, text="לחץ לדיבור", fill=FG, font=("Segoe UI", 10, "bold"), anchor="w")
            c.create_text(self.W - 14, cy, text="▾", fill=DIM, font=("Segoe UI", 12), anchor="e", tags="target")
            c.create_text(self.W - 28, cy, text=TARGET_LABELS[OUTPUT_TARGET], fill=DIM, font=("Segoe UI", 9), anchor="e", tags="target")

    # ---- אירועי עכבר
    def _hit(self, x, y):
        for item in self.c.find_overlapping(x, y, x, y):
            tags = self.c.gettags(item)
            if "main" in tags: return "main"
            if "cancel" in tags: return "cancel"
            if "target" in tags: return "target"
        return None

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), self._hit(e.x, e.y), False)

    def _drag_move(self, e):
        if not self._drag: return
        x0, y0, wx, wy, hit, moved = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if moved or abs(dx) > 4 or abs(dy) > 4:
            self._drag = (x0, y0, wx, wy, hit, True)
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _release(self, e):
        global BAR_POS
        if not self._drag: return
        x0, y0, wx, wy, hit, moved = self._drag
        self._drag = None
        if moved:
            BAR_POS = [self.root.winfo_x(), self.root.winfo_y()]; save_config(); return
        if hit == "main":
            if self.state == "idle" or self.state == "flash":
                begin("dictate")
            elif self.state == "rec":
                finish()
        elif hit == "cancel" and self.state == "rec":
            cancel()
        elif hit == "target" and self.state in ("idle", "flash"):
            self._target_menu(e)

    def _target_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg="#222", fg=FG, activebackground="#333", activeforeground=FG, font=("Segoe UI", 10))
        for key, label in TARGET_LABELS.items():
            m.add_command(label=("● " if key == OUTPUT_TARGET else "   ") + label, command=lambda k=key: self._set_target(k))
        m.add_separator()
        m.add_command(label="   היסטוריה / חלון תוצאות", command=lambda: self.show_result(None))
        m.add_command(label="   פתח תיקיית הגדרות", command=lambda: os.startfile(HERE))
        m.add_command(label="   יציאה", command=lambda: ui_q.put(("quit", None)))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _set_target(self, k):
        global OUTPUT_TARGET
        OUTPUT_TARGET = k; save_config(); self.draw()

    # ---- מצבים
    def set_state(self, state, msg=""):
        if self.flash_job:
            self.root.after_cancel(self.flash_job); self.flash_job = None
        self.state, self.msg = state, msg
        self.draw()
        if state == "flash":
            self.flash_job = self.root.after(2500, lambda: self.set_state("idle"))

    def _tick(self):
        # הבר אף פעם לא נשאר ממוזער (Win+D / "הצג שולחן עבודה" ממזערים גם חלונות צפים)
        try:
            h = hwnd_of(self.root)
            if h and ctypes.windll.user32.IsIconic(h):
                ctypes.windll.user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
                self.root.attributes("-topmost", True)
                log("הבר שוחזר ממזעור")
        except Exception:
            pass
        if self.state == "rec":
            self.draw(rec.level, time.time() - rec.t0)
        self.root.after(70, self._tick)

    # ---- חלון תוצאות / היסטוריה
    def show_result(self, text):
        if self.result_win is None or not self.result_win.winfo_exists():
            self.result_win = ResultWindow(self.root)
        self.result_win.show(text)


class ResultWindow:
    """חלון תוצאות: הטקסט האחרון + היסטוריה, עם העתקה והדבקה לחלון שהיה פעיל."""

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("Flow-HE — תוצאות")
        self.win.configure(bg=BG)
        self.win.geometry("460x420")
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        top = tk.Frame(self.win, bg=BG); top.pack(fill="x", padx=10, pady=(10, 4))
        tk.Button(top, text="📋 העתק", command=self.copy, bg="#2a2a2a", fg=FG, relief="flat", padx=10).pack(side="right", padx=4)
        tk.Button(top, text="⤴ הדבק לחלון הקודם", command=self.paste_back, bg="#2a2a2a", fg=FG, relief="flat", padx=10).pack(side="right", padx=4)
        tk.Button(top, text="🗑 נקה", command=lambda: self.text.delete("1.0", "end"), bg="#2a2a2a", fg=FG, relief="flat", padx=10).pack(side="right", padx=4)
        self.text = tk.Text(self.win, bg="#1e1e1e", fg=FG, insertbackground=FG, font=("Segoe UI", 12), wrap="word", relief="flat", padx=10, pady=8, height=8)
        self.text.pack(fill="both", expand=True, padx=10)
        self.text.tag_configure("rtl", justify="right")
        tk.Label(self.win, text="היסטוריה (לחיצה כפולה מעתיקה)", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="e", padx=12, pady=(8, 0))
        self.hist = tk.Listbox(self.win, bg="#1a1a1a", fg=FG, font=("Segoe UI", 10), relief="flat", justify="right", height=7, selectbackground="#333")
        self.hist.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.hist.bind("<Double-Button-1>", self._hist_copy)

    def show(self, text):
        if text is not None:
            self.text.delete("1.0", "end"); self.text.insert("1.0", text, "rtl")
        self.hist.delete(0, "end")
        self.items = []
        if os.path.exists(P("history.jsonl")):
            for line in open(P("history.jsonl"), encoding="utf-8"):
                try: self.items.append(json.loads(line))
                except Exception: pass
        for d in reversed(self.items[-50:]):
            t = d["final"].replace("\n", " ")
            self.hist.insert("end", f"{d['ts'][11:16]}  {t[:70]}{'…' if len(t) > 70 else ''}")
        self.win.deiconify(); self.win.lift()

    def current(self):
        return self.text.get("1.0", "end").strip()

    def copy(self):
        pyperclip.copy(self.current()); ui_q.put(("flash", "📋 הועתק"))

    def paste_back(self):
        t = self.current()
        if not t: return
        self.win.withdraw()
        focus_target(); paste(t)
        ui_q.put(("flash", "✅ הודבק"))

    def _hist_copy(self, e):
        sel = self.hist.curselection()
        if sel:
            d = list(reversed(self.items[-50:]))[sel[0]]
            self.text.delete("1.0", "end"); self.text.insert("1.0", d["final"], "rtl")
            pyperclip.copy(d["final"]); ui_q.put(("flash", "📋 הועתק"))


def pump(root, bar):
    if os.path.exists(QUIT_FLAG):
        log("מופע חדש ביקש לסגור — יוצא")
        try: os.remove(QUIT_FLAG)
        except OSError: pass
        root.destroy(); os._exit(0)
    try:
        while True:
            ev, arg = ui_q.get_nowait()
            if ev == "quit":
                root.destroy(); os._exit(0)
            elif ev == "rec":
                bar.set_state("rec")
            elif ev == "show":
                bar.set_state("busy", arg)
            elif ev == "hide":
                bar.set_state("idle")
            elif ev == "flash":
                bar.set_state("flash", arg)
            elif ev == "result":
                bar.set_state("idle"); bar.show_result(arg)
    except queue.Empty:
        pass
    root.after(60, pump, root, bar)


# ---------------------------------------------------------------- מגש מערכת
def tray():
    if not pystray:
        return
    img = Image.new("RGB", (64, 64), "#111")
    d = ImageDraw.Draw(img); d.ellipse((16, 10, 48, 44), fill="#4ade80"); d.rectangle((28, 44, 36, 56), fill="#4ade80")
    menu = pystray.Menu(
        pystray.MenuItem(lambda _: stats(), None, enabled=False),
        pystray.MenuItem("חלון תוצאות / היסטוריה", lambda: ui_q.put(("result", None))),
        pystray.MenuItem("פתח תיקיית הגדרות", lambda: os.startfile(HERE)),
        pystray.MenuItem("יציאה", lambda icon: (icon.stop(), ui_q.put(("quit", None)))),
    )
    pystray.Icon("flow_he", img, "Flow-HE", menu).run_detached()


# ---------------------------------------------------------------- self-test (רץ ב-GitHub Actions על Windows)
def selftest(root, bar):
    """מצלם את הבר בכל מצב, ובודק תמלול מול השרת אם יש טוקן. כותב selftest.json ויוצא."""
    from PIL import ImageGrab
    out = os.environ.get("FLOWHE_SELFTEST_OUT", P("selftest"))
    os.makedirs(out, exist_ok=True)
    report = {"ok": True, "steps": []}

    def drain():
        while True:
            try: ui_q.get_nowait()
            except queue.Empty: break

    def shot(name):
        drain(); root.update(); time.sleep(0.4); drain(); root.update()
        x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
        ImageGrab.grab(all_screens=True).save(os.path.join(out, f"screen_{name}.png"))
        ImageGrab.grab(bbox=(x - 20, y - 20, x + w + 20, y + h + 20), all_screens=True).save(os.path.join(out, f"bar_{name}.png"))
        report["steps"].append({"state": name, "geom": root.winfo_geometry(), "viewable": bool(root.winfo_viewable())})
        if not root.winfo_viewable():
            report["ok"] = False

    try:
        shot("idle")
        rec.t0 = time.time() - 7; rec.level = 0.08
        bar.set_state("rec"); bar.draw(0.08, 7.0); shot("recording")
        bar.set_state("busy", "⏳ מתמלל..."); shot("busy")
        bar.set_state("flash", "✅ שלום עולם, זו בדיקה"); shot("done")
        h = hwnd_of(root)
        ctypes.windll.user32.ShowWindow(h, 6)  # SW_MINIMIZE, כמו "הצג שולחן עבודה"
        root.update(); time.sleep(0.3)
        report["minimized_by_os"] = bool(ctypes.windll.user32.IsIconic(h))
        bar._tick(); root.update(); time.sleep(0.3); root.update()
        report["restored_after_minimize"] = not ctypes.windll.user32.IsIconic(h) and bool(ctypes.windll.user32.IsWindowVisible(h))
        shot("after_minimize")
        report["ok"] &= report["restored_after_minimize"]
        bar.set_state("idle")
        bar.show_result("טקסט לדוגמה בחלון התוצאות"); root.update(); time.sleep(0.4)
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "screen_result_window.png"))
        if not MISSING_CREDS:
            wav = os.environ.get("FLOWHE_SELFTEST_WAV")
            if wav and os.path.exists(wav):
                audio, sr = sf.read(wav, dtype="float32")
                if audio.ndim > 1: audio = audio.mean(axis=1)
                t0 = time.time()
                report["transcription"] = transcribe(audio)
                report["transcribe_seconds"] = round(time.time() - t0, 1)
    except Exception:
        import traceback
        report["ok"] = False; report["error"] = traceback.format_exc()
    with open(os.path.join(out, "selftest.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log("selftest:", json.dumps(report, ensure_ascii=False))
    root.destroy(); os._exit(0 if report["ok"] else 1)


# ---------------------------------------------------------------- main
def main():
    if not SELFTEST:
        install_hotkeys()
    run_ui()


def install_hotkeys():
    keyboard.add_hotkey(HK_TOGGLE, lambda: finish() if mode else begin("dictate"))
    keyboard.add_hotkey(HK_SCRATCH, lambda: finish() if mode else begin("scratch"))
    keyboard.add_hotkey(HK_CANCEL, cancel)
    # Push-to-talk: לחוץ והחזק. Alt ימני מדווח בשמות שונים לפי פריסת מקלדת (right alt / alt gr / altgr)
    hold_names = {HK_HOLD.lower()}
    if HK_HOLD.lower() in ("right alt", "alt gr", "altgr"):
        hold_names |= {"right alt", "alt gr", "altgr"}
    hold_down = {"on": False}

    def _hook(e):
        name = (e.name or "").lower()
        if "alt" in name or "ctrl" in name:  # רק מקשי-עזר, לדיאגנוסטיקה
            log("מקש:", name, e.event_type)
        if name not in hold_names:
            return
        if e.event_type == "down" and not hold_down["on"]:
            hold_down["on"] = True
            begin("dictate")
        elif e.event_type == "up" and hold_down["on"]:
            hold_down["on"] = False
            if mode == "dictate":
                finish()
    keyboard.hook(_hook)
    # Command Mode: לחוץ והחזק
    cmd_keys = HK_COMMAND.split("+")
    keyboard.add_hotkey(HK_COMMAND, lambda: begin("command"))
    keyboard.on_release_key(cmd_keys[-1], lambda e: finish() if mode == "command" else None)

    print("Flow-HE מוכן.")
    try:
        print("  מיקרופון ברירת מחדל:", sd.query_devices(kind="input")["name"])
    except Exception as e:
        print("  אין מיקרופון ברירת מחדל:", e)
    print(f"  {HK_HOLD:18} לחוץ-והחזק להכתבה")
    print(f"  {HK_TOGGLE:18} הפעל/עצור הכתבה")
    print(f"  {HK_COMMAND:18} Command Mode (בחר טקסט, החזק, אמור פקודה)")
    print(f"  {HK_SCRATCH:18} פתק קולי -> scratchpad.md")
    print(f"  {HK_CANCEL:18} ביטול")
    print(f"  AI auto-edits: {'פעיל' if AI_EDITS else 'כבוי (אין ANTHROPIC_API_KEY)'} · privacy: {PRIVACY}")
    print("  " + stats())


def run_ui():
    root = tk.Tk()
    root.report_callback_exception = lambda et, ev, tb: log("Tk error:", "".join(__import__("traceback").format_exception(et, ev, tb)))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    bar = FlowBar(root)

    def _diag():
        try:
            log(f"bar: geom={root.winfo_geometry()} viewable={root.winfo_viewable()} mapped={root.winfo_ismapped()} "
                f"screen={root.winfo_screenwidth()}x{root.winfo_screenheight()} state={root.state()}")
        except Exception as e:
            log("diag:", e)
    root.after(1500, _diag)
    if MISSING_CREDS and not SELFTEST:
        root.after(800, lambda: ui_q.put(("flash", "⚠ חסר טוקן בקובץ .env")))
    if SELFTEST:
        root.after(500, lambda: selftest(root, bar))
    else:
        tray()
    root.after(60, pump, root, bar)
    root.mainloop()


if __name__ == "__main__":
    main()
