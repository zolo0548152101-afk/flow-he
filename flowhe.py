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
import os as _os
import sys as _sys


def _fatal_box(msg: str):
    """חלון שגיאה של Windows — כדי שהאפליקציה לעולם לא תיעלם בשקט."""
    try:
        ctypes.windll.user32.MessageBoxW(0, msg[:1500], "Flow-HE", 0x10 | 0x40000)
    except Exception:
        pass


def _excepthook(et, ev, tb):
    import traceback
    text = "".join(traceback.format_exception(et, ev, tb))
    try:
        d = _os.path.join(_os.environ.get("LOCALAPPDATA", _os.path.expanduser("~")), "Flow-HE")
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, "crash.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
    if _os.environ.get("FLOWHE_SELFTEST") != "1":
        _fatal_box("Flow-HE נתקל בשגיאה ונסגר:\n\n" + text[-1200:])


_sys.excepthook = _excepthook
import io
import math
import json
import os
import queue
import sys
import threading
import time
import types
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
    try:
        _logf = open(P("flow.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        _d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Flow-HE")
        os.makedirs(_d, exist_ok=True)
        _logf = open(os.path.join(_d, "flow.log"), "a", encoding="utf-8", buffering=1)
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

# מופע יחיד: מופע חדש סוגר כל עותק ישן (מכל גרסה ומכל תיקייה) ותופס את מקומו
_SHARED = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "Flow-HE")
os.makedirs(_SHARED, exist_ok=True)
QUIT_FLAG = os.path.join(_SHARED, "quit.flag")
MUTEX_NAME = "Local\\FlowHE_single_instance"


def _kill_other_copies():
    """סוגר בכוח תהליכי Flow-HE אחרים: Flow-HE.exe או python שמריץ flowhe.py / flow.py."""
    import subprocess
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne %d -and $_.ProcessId -ne $PID -and ("
        "$_.Name -eq 'Flow-HE.exe' -or $_.CommandLine -like '*flowhe.py*' -or $_.CommandLine -like '*flow_he*flow.py*') }"
        " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }" % os.getpid()
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=20, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return out.stdout.split()
    except Exception as e:
        print("kill_other_copies:", e, flush=True)
        return []


def _take_mutex():
    m = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(m)
        return None
    return m


if sys.platform == "win32" and not SELFTEST:
    _mutex = _take_mutex()
    if not _mutex:
        open(QUIT_FLAG, "w").close()            # בקשה מנומסת (גרסאות חדשות מקשיבות)
        for _ in range(8):
            time.sleep(0.25)
            _mutex = _take_mutex()
            if _mutex: break
    if not _mutex:
        killed = _kill_other_copies()           # גרסה ישנה שלא מקשיבה: סוגרים בכוח
        print("סגרתי עותקים ישנים:", killed, flush=True)
        for _ in range(20):
            time.sleep(0.25)
            _mutex = _take_mutex()
            if _mutex: break
    if not _mutex:
        _fatal_box("Flow-HE כבר פועל ולא הצלחתי לסגור את העותק הקודם.\n"
                   "סגור אותו ממנהל המשימות (Flow-HE.exe או python) ונסה שוב.")
        sys.exit(1)
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
# מקשים כמו ב-Wispr Flow ל-Windows. קונפיג ישן (Alt ימני / Ctrl+Alt+Space) מומר אוטומטית פעם אחת.
WISPR_HOTKEYS = {"push_to_talk": "ctrl+windows", "hands_free": "ctrl+windows+space", "command_mode": "ctrl+windows+alt",
                 "paste_last": "alt+shift+z", "copy_last": "alt+shift+x", "cancel": "esc",
                 "scratchpad": "ctrl+alt+n", "push_to_talk_alt": "right alt"}
if CFG.get("hotkeys_version") != 2:
    HK = dict(WISPR_HOTKEYS)
    CFG["hotkeys"], CFG["hotkeys_version"] = HK, 2
    try:
        with open(P("config.json"), "w", encoding="utf-8") as _f:
            json.dump(CFG, _f, ensure_ascii=False, indent=2)
    except OSError:
        pass
HK = {**WISPR_HOTKEYS, **HK}
HK_SCRATCH, HK_CANCEL = HK["scratchpad"], HK["cancel"]
HK_PASTE_LAST, HK_COPY_LAST = HK["paste_last"], HK["copy_last"]
HK_HOLD_ALT = (HK.get("push_to_talk_alt") or "").lower()
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
            h = ctypes.windll.user32.GetForegroundWindow()
            if h not in OWN_HWNDS:
                self.target_hwnd = h
        except Exception:
            pass
        self.chunks, self.on, self.level = [], True, 0.0
        self.t0 = time.time()
        if os.environ.get("FLOWHE_FAKE_AUDIO") == "1":  # בדיקות CI: אין מיקרופון במכונה
            self.chunks = [np.full((SAMPLE_RATE, 1), 0.05, dtype="float32")]
            return
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self._cb)
        self.stream.start()

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


OWN_HWNDS = set()  # חלונות של Flow-HE עצמו — לא יעד להדבקה
PINNED = {"hwnd": 0, "title": ""}  # חלון יעד קבוע שהמשתמש בחר (0 = החלון האחרון שהיה פעיל)


def window_title(h) -> str:
    try:
        u = ctypes.windll.user32
        n = u.GetWindowTextLengthW(h) + 1
        buf = ctypes.create_unicode_buffer(n)
        u.GetWindowTextW(h, buf, n)
        return buf.value
    except Exception:
        return ""


def list_windows(limit=25):
    """חלונות פתוחים שאפשר להדביק אליהם: גלויים, עם כותרת, לא חלונות כלי, לא של Flow-HE."""
    u = ctypes.windll.user32
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        try:
            if not u.IsWindowVisible(h) or h in OWN_HWNDS or u.GetWindow(h, 4):  # GW_OWNER
                return True
            ex = u.GetWindowLongW(h, -20)
            if ex & 0x00000080:  # WS_EX_TOOLWINDOW
                return True
            t = window_title(h).strip()
            if t and t not in ("Program Manager", "Flow-HE", "Flow-HE — תוצאות"):
                found.append((h, t))
        except Exception:
            pass
        return len(found) < limit

    u.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def short(t: str, n=22) -> str:
    t = t.replace("\u202a", "").replace("\u202c", "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"
rec = Recorder()
lock = threading.Lock()
mode = None  # None | "dictate" | "command" | "scratch"


# ---------------------------------------------------------------- תמלול + עיבוד
def transcribe(audio) -> str:
    fake = os.environ.get("FLOWHE_FAKE_TRANSCRIPT")
    if fake:
        time.sleep(0.5); return fake
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
    """מחזיר את הפוקוס לחלון שבו המשתמש היה כשההקלטה התחילה.
    בלי טריק Alt (הוא פותח את התפריט של אפליקציות כמו פנקס רשימות ו-Ctrl+V הולך לתפריט)."""
    h = PINNED["hwnd"] or rec.target_hwnd
    if not h:
        return True  # אין יעד שמור: מדביקים לחלון הפעיל
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        if not u.IsWindow(h):
            if PINNED["hwnd"]:
                log("חלון היעד נסגר:", PINNED["title"]); PINNED.update(hwnd=0, title="")
                ui_q.put(("flash", "⚠ חלון היעד נסגר — הודבק לחלון האחרון"))
                h = rec.target_hwnd
                if not h or not u.IsWindow(h):
                    return False
            else:
                return False
        fg = u.GetForegroundWindow()
        if fg == h:
            return True
        me = k.GetCurrentThreadId()
        fg_tid = u.GetWindowThreadProcessId(fg, None)
        tgt_tid = u.GetWindowThreadProcessId(h, None)
        u.AttachThreadInput(me, fg_tid, True)
        u.AttachThreadInput(me, tgt_tid, True)
        try:
            if u.IsIconic(h):
                u.ShowWindow(h, 9)  # SW_RESTORE
            u.BringWindowToTop(h)
            u.SetForegroundWindow(h)
        finally:
            u.AttachThreadInput(me, tgt_tid, False)
            u.AttachThreadInput(me, fg_tid, False)
        for _ in range(10):
            if u.GetForegroundWindow() == h:
                break
            time.sleep(0.03)
        time.sleep(0.08)
        ok = u.GetForegroundWindow() == h
        log("focus_target:", "ok" if ok else "נכשל")
        return ok
    except Exception as e:
        log("focus_target:", e)
        return False


def deliver(text: str):
    """מוציא את הטקסט לפי יעד הפלט שנבחר בבר."""
    if OUTPUT_TARGET == "clipboard":
        pyperclip.copy(text)
        ui_q.put(("flash", "📋 הועתק ללוח"))
    elif OUTPUT_TARGET == "window":
        pyperclip.copy(text)
        ui_q.put(("result", text))
    else:
        if focus_target():
            paste(text)
        else:
            # כמו ב-Wispr: לא מדביקים לחלון אקראי. שומרים בלוח ומראים את הטקסט
            pyperclip.copy(text)
            ui_q.put(("result", text))
            ui_q.put(("flash", "בחר תיבת טקסט ולחץ Alt+Shift+Z"))


LAST = {"text": ""}  # התמלול האחרון — ל-Alt+Shift+Z / Alt+Shift+X


def paste_last():
    if not LAST["text"]:
        ui_q.put(("flash", "אין תמלול אחרון")); return
    if mode:
        cancel()
    log("הדבקת אחרון")
    paste(LAST["text"])


def copy_last():
    if LAST["text"]:
        pyperclip.copy(LAST["text"]); ui_q.put(("flash", "📋 התמלול האחרון הועתק"))


def paste(text: str):
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    pyperclip.copy(text)
    time.sleep(0.06)
    pyautogui.hotkey("ctrl", "v")
    log("הודבק לחלון:", active_window_title())
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


def set_mode(m):
    global mode
    mode = m


def quiet_cancel():
    """ביטול שקט (הקשה קצרה שלא הפכה ללחיצה כפולה)."""
    global mode
    if mode:
        mode = None; rec.stop(); ui_q.put(("hide", None)); log("הקשה קצרה — בוטל")


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
        LAST["text"] = final
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
        OWN_HWNDS.add(hwnd_of(root))
        if CFG.get("bar_noactivate", True):
            make_noactivate(root)
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
            c.create_text(48, cy - 7, text="לחץ לדיבור", fill=FG, font=("Segoe UI", 10, "bold"), anchor="w")
            c.create_text(48, cy + 9, text="או החזק Caps Lock", fill=DIM, font=("Segoe UI", 8), anchor="w")
            c.create_text(self.W - 14, cy, text="▾", fill=DIM, font=("Segoe UI", 12), anchor="e", tags="target")
            label = "איפה שהסמן" if OUTPUT_TARGET == "paste" else TARGET_LABELS[OUTPUT_TARGET]
            if OUTPUT_TARGET == "paste" and PINNED["hwnd"]:
                label = "→ " + short(PINNED["title"], 16)
            c.create_text(self.W - 28, cy, text=label, fill=ACCENT if PINNED["hwnd"] and OUTPUT_TARGET == "paste" else DIM,
                          font=("Segoe UI", 9), anchor="e", tags="target")

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
        mk = lambda parent: tk.Menu(parent, tearoff=0, bg="#222", fg=FG, activebackground="#333", activeforeground=FG, font=("Segoe UI", 10))
        m = mk(self.root)
        auto = OUTPUT_TARGET == "paste" and not PINNED["hwnd"]
        m.add_command(label=("● " if auto else "   ") + "איפה שהסמן (כמו Wispr)", command=self._auto)
        m.add_command(label="   🎯 נעל על חלון — לחץ עליו…", command=self._pick_by_click)
        wm = mk(m)
        try:
            for h, t in list_windows():
                wm.add_command(label=("● " if h == PINNED["hwnd"] else "   ") + short(t, 45), command=lambda h=h, t=t: self._pin(h, t))
        except Exception as ex:
            log("list_windows:", ex)
        locked = OUTPUT_TARGET == "paste" and PINNED["hwnd"]
        m.add_cascade(label=("● " if locked else "   ") + "נעל על חלון מהרשימה" + (f" (→ {short(PINNED['title'], 18)})" if locked else ""), menu=wm)
        m.add_separator()
        m.add_command(label=("● " if OUTPUT_TARGET == "clipboard" else "   ") + "רק העתק ללוח", command=lambda: self._set_target("clipboard"))
        m.add_command(label=("● " if OUTPUT_TARGET == "window" else "   ") + "הצג בחלון תוצאות", command=lambda: self._set_target("window"))
        m.add_separator()
        m.add_command(label="   הדבק שוב את האחרון  (Alt+Shift+Z)", command=lambda: threading.Thread(target=paste_last, daemon=True).start())
        m.add_command(label="   היסטוריה / חלון תוצאות", command=lambda: self.show_result(None))
        m.add_command(label="   פתח תיקיית הגדרות", command=lambda: os.startfile(HERE))
        m.add_command(label="   יציאה", command=lambda: ui_q.put(("quit", None)))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _auto(self):
        global OUTPUT_TARGET
        PINNED.update(hwnd=0, title=""); OUTPUT_TARGET = "paste"; save_config()
        self.set_state("flash", "הטקסט ייכנס איפה שהסמן")

    def _set_target(self, k):
        global OUTPUT_TARGET
        OUTPUT_TARGET = k; save_config(); self.draw()

    def _pin(self, h, t):
        global OUTPUT_TARGET
        PINNED.update(hwnd=h, title=t)
        OUTPUT_TARGET = "paste"; save_config()
        log("חלון יעד נבחר:", t)
        self.set_state("flash", "🎯 הטקסט ייכנס ל: " + short(t, 26))

    def _unpin(self):
        self._auto()

    def _pick_by_click(self):
        """מצב בחירה: החלון הבא שהמשתמש לוחץ עליו (תוך 10 שניות) הופך ליעד."""
        u = ctypes.windll.user32
        start_fg = u.GetForegroundWindow()
        self.set_state("busy", "🎯 לחץ על החלון שאליו ייכנס הטקסט…")
        deadline = time.time() + 10

        def poll():
            h = u.GetForegroundWindow()
            root_h = u.GetAncestor(h, 2) if h else 0
            if root_h and root_h != start_fg and root_h not in OWN_HWNDS and window_title(root_h).strip():
                self._pin(root_h, window_title(root_h)); return
            if time.time() > deadline:
                self.set_state("flash", "לא נבחר חלון"); return
            self.root.after(150, poll)
        self.root.after(300, poll)

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
        self.win.update_idletasks()
        OWN_HWNDS.add(hwnd_of(self.win))
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
        if os.environ.get("FLOWHE_SELFTEST_PASTE") == "1":
            import subprocess
            u = ctypes.windll.user32
            subprocess.Popen(["notepad.exe"])
            np_h = 0
            for _ in range(60):
                time.sleep(0.25)
                h = u.GetForegroundWindow()
                t = active_window_title().lower()
                if h and h not in OWN_HWNDS and ("notepad" in t or "פנקס" in t or "untitled" in t):
                    np_h = h; break
            report["notepad_found"] = bool(np_h)
            if np_h:
                report["windows_listed"] = [short(t, 40) for _, t in list_windows()][:8]
                bar._pin(np_h, window_title(np_h)); rec.target_hwnd = 0
                shot("pinned")
                # מדמים מצב אמיתי: הפוקוס עבר למקום אחר (הבר / שולחן העבודה)
                u.SetForegroundWindow(u.GetDesktopWindow()); root.update(); time.sleep(0.3)
                sample = "בדיקת הדבקה 123"
                deliver(sample); time.sleep(0.6)
                pyperclip.copy("")
                pyautogui.hotkey("ctrl", "a"); time.sleep(0.1); pyautogui.hotkey("ctrl", "c"); time.sleep(0.3)
                got = pyperclip.paste().strip()
                report["paste_readback"] = got
                report["paste_ok"] = sample in got
                report["ok"] &= report["paste_ok"]
                ImageGrab.grab(all_screens=True).save(os.path.join(out, "screen_paste_notepad.png"))
                subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
        if os.environ.get("FLOWHE_SELFTEST_E2E") == "1":
            selftest_e2e(root, bar, report, out)
        if not MISSING_CREDS and not os.environ.get("FLOWHE_FAKE_TRANSCRIPT"):
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


def selftest_e2e(root, bar, report, out):
    """בדיקה מקצה לקצה כמו משתמש אמיתי: פנקס רשימות בפוקוס -> מקשים -> הטקסט מופיע בו."""
    import subprocess
    from PIL import ImageGrab
    u = ctypes.windll.user32
    expected = os.environ.get("FLOWHE_FAKE_TRANSCRIPT", "")
    bar._auto(); install_hotkeys()
    e2e = report.setdefault("e2e", {})

    def pump_for(sec):
        end = time.time() + sec
        while time.time() < end:
            root.update(); time.sleep(0.05)

    def find_notepad():
        for _ in range(60):
            pump_for(0.25)
            h = u.GetForegroundWindow(); t = active_window_title().lower()
            if h and h not in OWN_HWNDS and ("notepad" in t or "פנקס" in t or "untitled" in t):
                return h
        return 0

    def readback():
        pyperclip.copy("")
        pyautogui.hotkey("ctrl", "a"); time.sleep(0.15); pyautogui.hotkey("ctrl", "c"); pump_for(0.4)
        return pyperclip.paste().strip()

    def clear():
        pyautogui.hotkey("ctrl", "a"); time.sleep(0.1); pyautogui.press("delete"); pump_for(0.3)

    def wait_idle(sec=8):
        end = time.time() + sec
        while time.time() < end and (mode or bar.state == "busy"):
            pump_for(0.1)
        pump_for(0.8)

    subprocess.Popen(["notepad.exe"])
    h = find_notepad()
    e2e["notepad"] = bool(h)
    if not h:
        report["ok"] = False; return
    LAST["text"] = ""

    # מקשים "אמיתיים" דרך SendInput (pyautogui). keyboard.press לא נקלט ב-hook של אותה ספרייה.
    down = lambda *ks: [pyautogui.keyDown(k) for k in ks]
    up = lambda *ks: [pyautogui.keyUp(k) for k in reversed(ks)]

    # 1) החזק Ctrl+Win -> שחרר -> הטקסט בפנקס
    down("ctrl", "win"); pump_for(1.3)
    e2e["ptt_recording"] = mode == "dictate"
    up("ctrl", "win")
    wait_idle()
    got = readback(); e2e["ptt_text"] = got; e2e["ptt_ok"] = expected in got
    ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_ptt.png"))

    # 2) Alt+Shift+Z מדביק שוב את האחרון.
    #    ספריית keyboard מסננת במכוון אירועי Alt מוזרקים (LLKHF_INJECTED|ALTDOWN), כך שאי אפשר להזריק אותם.
    #    מזינים למנוע בדיוק את האירועים שמקלדת אמיתית מייצרת (בפריסה עברית: Z = "ז", קוד 44).
    clear()
    ev = lambda n, sc, t: types.SimpleNamespace(name=n, scan_code=sc, event_type=t)
    for e_ in (ev("alt", 56, "down"), ev("shift", 42, "down"), ev("ז", 44, "down"), ev("ז", 44, "up"),
               ev("shift", 42, "up"), ev("alt", 56, "up")):
        _on_key(e_)
    pump_for(1.5)
    got = readback(); e2e["paste_last_text"] = got; e2e["paste_last_ok"] = expected in got

    # 3) ללא ידיים: Ctrl+Win+Space, ממשיך להקליט אחרי שחרור, Ctrl+Win שוב עוצר
    clear()
    down("ctrl", "win"); pump_for(0.2)
    pyautogui.press("space"); pump_for(0.2)
    up("ctrl", "win"); pump_for(1.2)
    e2e["hands_free_still_recording"] = mode == "dictate"
    ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_hands_free.png"))
    down("ctrl", "win"); pump_for(0.4); up("ctrl", "win")
    wait_idle()
    got = readback(); e2e["hands_free_text"] = got; e2e["hands_free_ok"] = expected in got

    # 4) הקשה קצרה בודדת לא מדביקה כלום
    clear()
    down("ctrl", "win"); pump_for(0.1); up("ctrl", "win"); pump_for(1.5)
    got = readback(); e2e["short_tap_text"] = got; e2e["short_tap_ok"] = (got == "" and not mode)

    # 5) Caps Lock: החזק -> הטקסט בפנקס, ומצב האותיות הגדולות לא משתנה
    clear()
    caps_before = u.GetKeyState(0x14) & 1
    pyautogui.keyDown("capslock"); pump_for(1.3)
    e2e["caps_recording"] = mode == "dictate"
    pyautogui.keyUp("capslock")
    wait_idle()
    got = readback(); e2e["caps_text"] = got; e2e["caps_ok"] = expected in got
    e2e["caps_state_unchanged"] = (u.GetKeyState(0x14) & 1) == caps_before

    # 6) Caps Lock לחיצה כפולה -> ללא ידיים, לחיצה נוספת עוצרת
    clear()
    pyautogui.press("capslock"); pump_for(0.1); pyautogui.press("capslock"); pump_for(1.2)
    e2e["caps_double_tap_hands_free"] = mode == "dictate"
    pyautogui.press("capslock")
    wait_idle()
    got = readback(); e2e["caps_hands_free_text"] = got; e2e["caps_hands_free_ok"] = expected in got
    e2e["caps_state_unchanged_after_taps"] = (u.GetKeyState(0x14) & 1) == caps_before

    report["ok"] &= all(e2e.get(k) for k in ("ptt_recording", "ptt_ok", "paste_last_ok",
                                              "hands_free_still_recording", "hands_free_ok", "short_tap_ok",
                                              "caps_recording", "caps_ok", "caps_state_unchanged",
                                              "caps_double_tap_hands_free", "caps_hands_free_ok",
                                              "caps_state_unchanged_after_taps"))
    keyboard.unhook_all()
    subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)


# ---------------------------------------------------------------- main
def main():
    if not SELFTEST:
        install_hotkeys()
    run_ui()


HK_STATE = {"hold": False, "hands_free": False, "consumed": False, "t_down": 0.0, "pending_tap": 0.0}


_DOWN = set()  # מצב מקשי-העזר, נבנה מהאירועים עצמם (keyboard.is_pressed מתעדכן רק אחרי ה-hook)
_CTRL = {"ctrl", "left ctrl", "right ctrl"}
_WIN = {"windows", "left windows", "right windows"}


def _track(e):
    name = (e.name or "").lower()
    canon = ("ctrl" if name in _CTRL else "win" if name in _WIN else "alt" if "alt" in name
             else "shift" if "shift" in name else name)
    if e.event_type == "down":
        _DOWN.add(canon)
    else:
        _DOWN.discard(canon)
    return name


def _ctrl_win_down() -> bool:
    return "ctrl" in _DOWN and "win" in _DOWN


def _on_key(e):
    """מנוע המקשים בסגנון Wispr Flow:
    החזק Ctrl+Win = דבר, שחרר = הדבקה · Ctrl+Win+Space או לחיצה כפולה = ללא ידיים (לחיצה נוספת עוצרת)
    Ctrl+Win+Alt = Command Mode · Esc = ביטול"""
    st = HK_STATE
    name = _track(e)
    now = time.time()
    combo = _ctrl_win_down()
    if SELFTEST:
        log("key:", name, e.event_type, "combo" if combo else "")

    # Alt+Shift+Z / Alt+Shift+X לפי קוד המקש הפיזי (עובד גם בפריסה עברית, שם Z מדווח כ-ז)
    if e.event_type == "down" and "alt" in _DOWN and "shift" in _DOWN and "ctrl" not in _DOWN:
        if e.scan_code == 44:        # Z
            st["paste_last_pending"] = True; return
        if e.scan_code == 45:        # X
            copy_last(); return
    if st.get("paste_last_pending") and "alt" not in _DOWN and "shift" not in _DOWN:
        st["paste_last_pending"] = False   # מדביקים רק אחרי שחרור Alt/Shift, אחרת Ctrl+V הופך ל-Ctrl+Alt+Shift+V
        threading.Thread(target=paste_last, daemon=True).start()

    # Alt ימני (מקש חלופי, ישן)
    if HK_HOLD_ALT and name in ({HK_HOLD_ALT} | ({"right alt", "alt gr", "altgr"} if HK_HOLD_ALT in ("right alt", "alt gr", "altgr") else set())):
        if e.event_type == "down" and not mode:
            st["alt_hold"] = True; begin("dictate")
        elif e.event_type == "up" and st.get("alt_hold"):
            st["alt_hold"] = False
            if mode == "dictate": finish()
        return

    if combo and not st["hold"]:                     # Ctrl+Win נלחצו
        st["hold"], st["t_down"] = True, now
        if mode and st["pending_tap"]:              # לחיצה שנייה מהירה -> ללא ידיים
            st["pending_tap"], st["hands_free"], st["consumed"] = 0.0, True, True
            log("מצב ללא ידיים (לחיצה כפולה)"); ui_q.put(("rec", "dictate")); return
        if mode and st["hands_free"]:               # במצב ללא ידיים: לחיצה עוצרת
            st["hands_free"], st["consumed"] = False, True
            finish(); return
        if not mode:
            begin("dictate")
        return

    if st["hold"] and e.event_type == "down":
        if name == "space" and mode:
            st["hands_free"], st["consumed"] = True, True
            log("מצב ללא ידיים (Ctrl+Win+Space)"); ui_q.put(("rec", "dictate"))
        elif name in ("alt", "left alt", "right alt") and mode == "dictate" and now - st["t_down"] < 1.5:
            set_mode("command"); log("Command Mode"); ui_q.put(("rec", "command"))
        return

    if st["hold"] and not combo:                     # Ctrl+Win שוחררו
        st["hold"] = False
        if st["consumed"]:
            st["consumed"] = False; return
        if not mode or st["hands_free"]:
            return
        if now - st["t_down"] < 0.3:                 # הקשה קצרה: אולי תחילת לחיצה כפולה
            st["pending_tap"] = now

            def _expire(t=now):
                time.sleep(0.5)
                if HK_STATE["pending_tap"] == t and not HK_STATE["hold"]:
                    HK_STATE["pending_tap"] = 0.0
                    with lock:
                        quiet_cancel()
            threading.Thread(target=_expire, daemon=True).start()
            return
        finish()


class HoldTrigger:
    """מקש בודד בסגנון Wispr: החזק = דבר, שחרר = הדבקה; לחיצה כפולה = ללא ידיים; לחיצה נוספת עוצרת.
    הקשה קצרה בודדת לא עושה כלום."""

    def __init__(self, name):
        self.name, self.down, self.t, self.pending, self.hf, self.consumed = name, False, 0.0, 0.0, False, False

    def press(self):
        if self.down:            # auto-repeat כשמחזיקים
            return
        self.down, self.t = True, time.time()
        if mode and self.pending:
            self.pending, self.hf, self.consumed = 0.0, True, True
            log(f"{self.name}: ללא ידיים"); ui_q.put(("rec", "dictate")); return
        if mode and self.hf:
            self.hf, self.consumed = False, True
            finish(); return
        if not mode:
            begin("dictate")

    def release(self):
        if not self.down:
            return
        self.down = False
        if self.consumed:
            self.consumed = False; return
        if not mode or self.hf:
            return
        now = time.time()
        if now - self.t < 0.3:
            self.pending = now

            def _expire(t=now):
                time.sleep(0.5)
                if self.pending == t and not self.down:
                    self.pending = 0.0
                    with lock:
                        quiet_cancel()
            threading.Thread(target=_expire, daemon=True).start()
            return
        finish()


CAPS = HoldTrigger("Caps Lock")


def _on_caps(e):
    """Caps Lock כמקש דיבור. מחזיר False כדי לבלוע את המקש (לא מחליף אותיות גדולות)."""
    if SELFTEST:
        log("caps:", e.event_type)
    if e.event_type == "down":
        CAPS.press()
    else:
        CAPS.release()
    return False


def install_hotkeys():
    keyboard.hook(_on_key)
    if CFG.get("caps_lock_ptt", True):
        try:
            keyboard.hook_key("caps lock", _on_caps, suppress=True)
        except Exception as ex:
            log("Caps Lock hook נכשל:", ex)
    keyboard.add_hotkey(HK_CANCEL, lambda: (HK_STATE.update(hands_free=False, pending_tap=0.0), cancel()))
    keyboard.add_hotkey(HK_SCRATCH, lambda: finish() if mode else begin("scratch"))

    print("Flow-HE מוכן.")
    try:
        print("  מיקרופון ברירת מחדל:", sd.query_devices(kind="input")["name"])
    except Exception as e:
        print("  אין מיקרופון ברירת מחדל:", e)
    print("  Caps Lock (החזק)   דבר, שחרר = הטקסט נכנס איפה שהסמן · לחיצה כפולה = ללא ידיים")
    print("  Ctrl+Win (החזק)    דבר, שחרר = הטקסט נכנס איפה שהסמן")
    print("  Ctrl+Win+Space     ללא ידיים (או לחיצה כפולה על Ctrl+Win); לחיצה נוספת עוצרת")
    print("  Ctrl+Win+Alt       Command Mode")
    print(f"  {HK_PASTE_LAST:18} הדבק שוב את התמלול האחרון · {HK_COPY_LAST} העתק אותו")
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
    if not SELFTEST and not MISSING_CREDS:
        root.after(900, lambda: ui_q.put(("flash", "החזק Caps Lock ודבר — הטקסט ייכנס איפה שהסמן")))
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
