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
import json
import os
import queue
import sys
import re
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
except ImportError:
    pystray = None

import tkinter as tk

import textproc
import ui_art
from PIL import ImageTk
from chunker import Chunker, is_silent
from concurrent.futures import ThreadPoolExecutor

# תיקיית הנתונים: ליד ה-EXE כשארוז (PyInstaller), ליד הסקריפט כשרץ כ-.py
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)  # קבצי ברירת מחדל שנארזו בתוך ה-EXE
P = lambda *a: os.path.join(HERE, *a)
try:
    VERSION = open(os.path.join(getattr(sys, "_MEIPASS", HERE), "version.txt"), encoding="utf-8").read().strip()
except OSError:
    VERSION = "dev"
VERSION = os.environ.get("FLOWHE_VERSION_OVERRIDE", VERSION)
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
_ENV_MTIME = {"t": None}


def load_env(override=False):
    if os.path.exists(P(".env")):
        _ENV_MTIME["t"] = os.path.getmtime(P(".env"))
        with open(P(".env"), encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if override:
                        os.environ[k.strip()] = v.strip()
                    else:
                        os.environ.setdefault(k.strip(), v.strip())


def reload_env_if_changed():
    """אם קובץ .env השתנה (למשל הוחלף בקובץ עם טוקן) — טוענים מחדש בלי להפעיל את האפליקציה מחדש."""
    global URL, TOKEN, CLAUDE_KEY, MISSING_CREDS, AI_EDITS, claude
    try:
        t = os.path.getmtime(P(".env"))
    except OSError:
        return
    if t == _ENV_MTIME["t"]:
        return
    load_env(override=True)
    URL, TOKEN = os.environ.get("TRANSCRIBE_URL"), os.environ.get("TRANSCRIBE_TOKEN")
    CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY")
    was_missing, MISSING_CREDS = MISSING_CREDS, not URL or not TOKEN
    log(".env נטען מחדש:", "טוקן קיים" if not MISSING_CREDS else "עדיין חסר טוקן")
    ai_now = CFG.get("ai_auto_edits", True) and bool(CLAUDE_KEY) and anthropic is not None
    if ai_now != AI_EDITS or (ai_now and claude is None):
        AI_EDITS = ai_now
        claude = anthropic.Anthropic(api_key=CLAUDE_KEY, timeout=12.0) if ai_now else None
        log("פיסוק חכם:", "פעיל" if ai_now else "כבוי")
        if ai_now:
            ui_q.put(("flash", "✨ פיסוק חכם הופעל"))
    if was_missing and not MISSING_CREDS:
        ui_q.put(("flash", "✅ הטוקן נטען — אפשר להכתיב"))


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
BAR_PINNED = {"on": bool(CFG.get("bar_pinned", False))}  # האם המשתמש פתח את הבר מהמגש (אחרת מופיע רק בזמן הקלטה)


def save_config():
    CFG["bar_pinned"] = BAR_PINNED["on"]
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

claude = anthropic.Anthropic(api_key=CLAUDE_KEY, timeout=12.0) if AI_EDITS else None
ui_q: "queue.Queue[tuple]" = queue.Queue()


def log(*a):
    try:
        print(f"[{datetime.now():%H:%M:%S}]", *a, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- צלילים / חלון פעיל
_SOUNDS = {}


def beep(kind):
    """צלילים רכים (נוצרים בקוד ב-ui_art) במקום הביפ של Windows."""
    if not SOUNDS or not winsound:
        return
    try:
        if not _SOUNDS:
            _SOUNDS.update(ui_art.make_sounds(float(CFG.get("sound_volume", 0.35))))
        data = _SOUNDS.get(kind)
        if data:
            threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT), daemon=True).start()
    except Exception as e:
        log("צליל:", e)


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
# ---------------------------------------------------------------- מיקרופון
def list_mics():
    """מיקרופונים זמינים (שמות ייחודיים, דרך MME — השמות הכי קריאים ב-Windows)."""
    try:
        apis = sd.query_hostapis()
        mme = next((i for i, a in enumerate(apis) if "MME" in a["name"]), None)
        names = []
        for d in sd.query_devices():
            if d["max_input_channels"] > 0 and (mme is None or d["hostapi"] == mme):
                n = d["name"].strip()
                if n not in names and "Sound Mapper" not in n:
                    names.append(n)
        return names
    except Exception as e:
        log("list_mics:", e); return []


def mic_device_index():
    want = CFG.get("mic_device")
    if not want:
        return None
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and d["name"].strip() == want:
                return i
    except Exception:
        pass
    log("המיקרופון שנבחר לא נמצא, משתמש בברירת המחדל:", want)
    return None


# ---------------------------------------------------------------- הפעלה עם Windows
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_launch_at_login(on: bool):
    if not FROZEN:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "Flow-HE", 0, winreg.REG_SZ, f'"{os.path.join(HERE, "Flow-HE.exe")}"')
            else:
                try: winreg.DeleteValue(k, "Flow-HE")
                except FileNotFoundError: pass
        log("הפעלה עם Windows:", "פעיל" if on else "כבוי")
    except Exception as e:
        log("set_launch_at_login:", e)


# ---------------------------------------------------------------- עדכון עצמי (בלי השתלטות על המחשב)
UPDATE_DIR = P("updates")


def available_update():
    """גרסה שמחכה בתיקיית updates/ (כל החלקים הגיעו), או None."""
    try:
        mf = os.path.join(UPDATE_DIR, "manifest.json")
        if not os.path.exists(mf):
            return None
        m = json.load(open(mf, encoding="utf-8"))
        if m.get("version") == VERSION:
            return None
        if not all(os.path.exists(os.path.join(UPDATE_DIR, x)) for x in m.get("parts", [])):
            return None
        return str(m.get("version"))
    except Exception:
        return None


def check_for_update(force=False):
    """מתקין עדכון מתיקיית updates/ — רק כשהמשתמש ביקש (כפתור "עדכן" / קיצור "Flow-HE עדכון"),
    אלא אם auto_update פעיל. מרכיב, בודק sha256, מחליף את עצמו ומפעיל מחדש.
    Windows מרשה לשנות שם לקובץ EXE שרץ, אז: עצמי -> .old, חדש -> השם שלי, מפעילים, יוצאים."""
    if not FROZEN or mode or PROCESSING["n"]:
        return False  # לא מתעדכנים באמצע הקלטה או תמלול
    if not force and not CFG.get("auto_update", False):
        return False
    mf = os.path.join(UPDATE_DIR, "manifest.json")
    if not os.path.exists(mf):
        return False
    try:
        import hashlib, subprocess
        m = json.load(open(mf, encoding="utf-8"))
        if m.get("version") == VERSION:
            return False
        parts = [os.path.join(UPDATE_DIR, x) for x in m["parts"]]
        if not all(os.path.exists(x) for x in parts):
            return False
        new_exe = os.path.join(HERE, "Flow-HE.new.exe")
        h = hashlib.sha256()
        with open(new_exe, "wb") as out:
            for x in parts:
                data = open(x, "rb").read(); h.update(data); out.write(data)
        if h.hexdigest() != m["sha256"]:
            log("עדכון: sha256 לא תואם, מדלג"); os.remove(new_exe); return False
        me = sys.executable
        old = os.path.join(HERE, "Flow-HE.old.exe")
        try: os.remove(old)
        except OSError: pass
        os.replace(me, old)
        os.replace(new_exe, me)
        for x in parts + [mf]:
            try: os.remove(x)
            except OSError: pass
        log(f"עדכון: {VERSION} -> {m['version']}, מפעיל מחדש")
        # בלי משתני PyInstaller של התהליך הנוכחי, אחרת ה-EXE החדש ינסה להשתמש בתיקייה הזמנית של הישן
        env = {k: v for k, v in os.environ.items()
               if k != "FLOWHE_VERSION_OVERRIDE" and not k.startswith(("_PYI", "_MEI"))}
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex)
        except Exception:
            pass
        subprocess.Popen([me, "--updated"], cwd=HERE, env=env, close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
        os._exit(0)
    except Exception as e:
        log("check_for_update:", e)
        return False


def cleanup_leftovers():
    """מוחק רק קבצי העברה ש-Claude יצר בהתקנות הידניות הקודמות (לא נוגע בשום קובץ אחר)."""
    import glob
    pats = ["Flow-HE.part0*", "Flow-HE.v2.part0*", "v[0-9]*.part0*", "v[0-9]*.tmp", "pkg.zip", "v2.zip",
            "Flow-HE-windows.zip", "Flow-HE.new.exe"]
    files = [f for pat in pats for f in glob.glob(os.path.join(HERE, pat))]
    if os.path.basename(sys.executable).lower() != "flow-he-3.exe":
        files.append(os.path.join(HERE, "Flow-HE-3.exe"))
    for d in (os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "שולחן העבודה"),
              os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")):
        files.append(os.path.join(d, "Flow-HE-update.bat"))
    removed = []
    for f in files:
        try:
            if os.path.isfile(f):
                os.remove(f); removed.append(os.path.basename(f))
        except OSError:
            pass
    if removed:
        log("נוקו קבצי העברה ישנים:", ", ".join(removed))


def ensure_shortcuts():
    """שני קיצורים על שולחן העבודה: "Flow-HE" (הפעלה) ו-"Flow-HE עדכון" (מתקין עדכון שמחכה ומפעיל).
    מחליף את Flow-HE.bat הישן (שהרכיב קבצים בכל הפעלה)."""
    import subprocess
    me = sys.executable
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');$w=New-Object -ComObject WScript.Shell;"
        "foreach($p in @(@('Flow-HE',''),@('Flow-HE עדכון','--update'))){"
        "$f=Join-Path $d ($p[0]+'.lnk');$s=$w.CreateShortcut($f);"
        "if($s.TargetPath -ne $env:FHE_EXE -or $s.Arguments -ne $p[1]){"
        "$s.TargetPath=$env:FHE_EXE;$s.Arguments=$p[1];$s.WorkingDirectory=$env:FHE_DIR;$s.IconLocation=$env:FHE_EXE+',0';"
        "$s.Description='Flow-HE';$s.Save()}};"
        "$b=Join-Path $d 'Flow-HE.bat';if(Test-Path $b){if((Get-Content $b -Raw) -match 'launch.ps1'){Remove-Item $b}}"
    )
    try:
        env = dict(os.environ, FHE_EXE=me, FHE_DIR=HERE)
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           env=env, capture_output=True, text=True, timeout=30, creationflags=0x08000000)
        log("קיצורי דרך:", "עודכנו" if r.returncode == 0 else r.stderr[-300:])
    except Exception as e:
        log("קיצורי דרך:", e)


def cleanup_after_update():
    old = os.path.join(HERE, "Flow-HE.old.exe")
    for _ in range(20):
        try:
            if os.path.exists(old): os.remove(old)
            break
        except OSError:
            time.sleep(0.5)


class Recorder:
    def __init__(self):
        self.chunks, self.stream, self.on = [], None, False
        self.level = 0.0      # RMS של הצ'אנק האחרון (0..1) למד העוצמה
        self.target_hwnd = 0  # החלון שהיה פעיל כשההקלטה התחילה — לשם מדביקים
        self.mouse_target = None  # (hwnd, x, y) אם העכבר היה מעל שדה טקסט כשההקלטה התחילה
        self.session = None       # StreamSession — תמלול תוך כדי הקלטה
        self.t0 = time.time()

    def _cb(self, indata, frames, t, status):
        if self.on:
            x = indata.copy()
            self.chunks.append(x)
            self.level = float(np.sqrt(np.mean(x ** 2)))
            if self.session:
                self.session.feed(x)

    def start(self):
        try:
            h = ctypes.windll.user32.GetForegroundWindow()
            if h not in OWN_HWNDS:
                self.target_hwnd = h
        except Exception:
            pass
        self.mouse_target = mouse_text_target()
        log("יעד:", window_title(self.target_hwnd), "| עכבר:", self.mouse_target and window_title(self.mouse_target[0]))
        self.chunks, self.on, self.level = [], True, 0.0
        self.session = StreamSession() if CFG.get("stream_transcription", True) else None
        self.t0 = time.time()
        fake = os.environ.get("FLOWHE_FAKE_AUDIO")
        if fake == "1":  # בדיקות CI: אין מיקרופון במכונה
            x = np.full((SAMPLE_RATE, 1), 0.05, dtype="float32")
            self.chunks = [x]
            if self.session: self.session.feed(x)
            return
        if fake == "stream":  # בדיקות CI: "דיבור" סינתטי בזמן אמת עם הפסקות כל 5 שניות
            threading.Thread(target=self._fake_stream, daemon=True).start()
            return
        dev = mic_device_index()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=self._cb, device=dev)
        self.stream.start()

    def _fake_stream(self):
        t, block = 0.0, 0.1
        while self.on:
            n = int(block * SAMPLE_RATE)
            tt = (np.arange(n) / SAMPLE_RATE + t)
            speaking = (tt % 5.6) < 5.0
            x = (0.2 * np.sin(2 * np.pi * 220 * tt) * speaking).astype("float32").reshape(-1, 1)
            self._cb(x, n, None, None)
            t += block; time.sleep(block)

    def stop(self):
        self.on = False
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        if not self.chunks:
            if self.session: self.session.abort()
            return None, 0.0
        audio = np.concatenate(self.chunks, axis=0)
        return audio, time.time() - self.t0


def normalize(audio):
    """מצב לחישה: נרמול עוצמה כדי שדיבור שקט יתומלל טוב."""
    peak = float(np.abs(audio).max()) or 1.0
    return audio * (0.6 / peak) if peak < 0.3 else audio


class StreamSession:
    """תמלול תוך כדי הקלטה: האודיו נחתך בהפסקות ונשלח לשרת במקביל לדיבור.
    כל קטע שחוזר מוצג מיד בבר (live_text). בסוף נשאר לשלוח רק את הקטע האחרון."""

    def __init__(self):
        self.chunker = Chunker(SAMPLE_RATE, min_s=CFG.get("chunk_min_s", 3.0), max_s=CFG.get("chunk_max_s", 12.0))
        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=int(CFG.get("parallel_requests", 3)))
        self.futures = []
        self.texts = {}          # מספר קטע -> טקסט נקי (מתמלא כשהשרת עונה)
        self.lock = threading.Lock()
        self.version = 0         # עולה בכל קטע שחוזר — הבר מצייר מחדש רק כשמשהו השתנה
        self.sent_before_end = 0
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def feed(self, x):
        self.q.put(x.reshape(-1))

    def _run(self):
        while True:
            x = self.q.get()
            if x is None:
                return
            for seg in self.chunker.feed(x):
                self._submit(seg, live=True)

    def _job(self, i, seg):
        t = textproc.clean_chunk(transcribe_with_retry(normalize(seg)))
        with self.lock:
            self.texts[i] = t
            self.version += 1
        return t

    def _submit(self, seg, live=False):
        if is_silent(seg):
            return
        i = len(self.futures)
        if live:
            self.sent_before_end += 1
            log(f"קטע {i + 1} נשלח לתמלול תוך כדי הקלטה ({len(seg) / SAMPLE_RATE:.1f}s)")
        self.futures.append(self.pool.submit(self._job, i, seg))

    def pending(self):
        return sum(1 for f in self.futures if not f.done())

    def live_text(self):
        """הטקסט שכבר חזר, לפי הסדר — עוצר בקטע הראשון שעוד לא חזר (כדי לא להציג חורים)."""
        out = []
        with self.lock:
            for i in range(len(self.futures)):
                if i not in self.texts:
                    break
                if self.texts[i]:
                    out.append(self.texts[i])
        return " ".join(out)

    def collect(self, audio=None):
        """נקרא בסוף ההקלטה ומחזיר את הטקסט הסופי.
        הקטעים הקצרים נועדו רק לתצוגה החיה: Whisper מדויק יותר כשהוא שומע משפט שלם עם ההקשר,
        אז הטקסט הסופי מתומלל מחדש מההקלטה כולה — בחלונות של עד ~28 שניות שנחתכים בהפסקות."""
        self.q.put(None); self.thread.join(5)
        if audio is not None and CFG.get("final_full_pass", True):
            try:
                self.pool.shutdown(wait=False, cancel_futures=True)  # התצוגה החיה כבר לא נחוצה
                return transcribe_full(audio, ThreadPoolExecutor(max_workers=int(CFG.get("parallel_requests", 3))))
            except Exception as e:
                log("תמלול מלא נכשל, משתמש בקטעים:", e)
        self._submit(self.chunker.flush())
        texts = [f.result() for f in self.futures]
        self.pool.shutdown(wait=False)
        return " ".join(t for t in texts if t).strip()

    def abort(self):
        self.q.put(None); self.pool.shutdown(wait=False, cancel_futures=True)


def transcribe_full(audio, pool=None):
    """ההקלטה כולה: עד ~28 שניות בבקשה אחת; ארוכה יותר — חלונות ארוכים שנחתכים בהפסקות, במקביל."""
    x = np.asarray(audio, dtype="float32").reshape(-1)
    max_s = float(CFG.get("final_window_s", 28.0))
    if len(x) <= max_s * SAMPLE_RATE:
        segs = [x]
    else:
        ch = Chunker(SAMPLE_RATE, min_s=max_s * 0.55, max_s=max_s)
        segs = ch.feed(x) + [ch.flush()]
    segs = [g for g in segs if len(g) and not is_silent(g)]
    if not segs:
        return ""
    log(f"תמלול סופי: {len(segs)} חלון/ות מתוך {len(x) / SAMPLE_RATE:.1f}s")
    job = lambda g: textproc.clean_chunk(transcribe_with_retry(normalize(g)))
    if len(segs) == 1 or pool is None:
        texts = [job(g) for g in segs]
    else:
        texts = list(pool.map(job, segs))
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
    return " ".join(t for t in texts if t).strip()


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


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint), ("hCursor", ctypes.c_void_p), ("pt", _POINT)]


def mouse_text_target():
    """אם סמן העכבר הוא סמן-טקסט (I) מעל חלון שאינו של Flow-HE — מחזיר (חלון, x, y). אחרת None."""
    if not CFG.get("paste_under_mouse", True):
        return None
    try:
        u = ctypes.windll.user32
        u.LoadCursorW.restype = ctypes.c_void_p
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [_POINT]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        ci = _CURSORINFO(); ci.cbSize = ctypes.sizeof(_CURSORINFO)
        u.GetCursorInfo(ctypes.byref(ci))
        pt = _POINT(); u.GetCursorPos(ctypes.byref(pt))
        ibeam = bool(ci.hCursor) and ci.hCursor == u.LoadCursorW(None, 32513)  # IDC_IBEAM
        child = u.WindowFromPoint(pt)
        root_h = u.GetAncestor(child, 2) if child else None
        cls = ""
        if child:
            buf = ctypes.create_unicode_buffer(128); u.GetClassNameW(ctypes.c_void_p(child), buf, 128); cls = buf.value
        edit_cls = cls.lower().startswith(("edit", "richedit", "scintilla")) or "richeditd2d" in cls.lower()
        log(f"עכבר: ({pt.x},{pt.y}) ibeam={ibeam} class={cls!r} -> {window_title(root_h) if root_h else '-'}")
        if not root_h or root_h in OWN_HWNDS or not (ibeam or edit_cls):
            return None
        return (root_h, pt.x, pt.y)
    except Exception as e:
        log("mouse_text_target:", e)
        return None


def click_at(x, y):
    """לחיצה אמיתית (SendInput) על נקודה במסך — ממקמת את הסמן בשדה שמתחת לעכבר."""
    u = ctypes.windll.user32
    old = _POINT(); u.GetCursorPos(ctypes.byref(old))
    u.SetCursorPos(int(x), int(y))
    u.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.03); u.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTDOWN/UP
    time.sleep(0.12)
    u.SetCursorPos(old.x, old.y)


def short(t: str, n=22) -> str:
    t = t.replace("\u202a", "").replace("\u202c", "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"
rec = Recorder()
lock = threading.Lock()
mode = None  # None | "dictate" | "command" | "scratch"


# ---------------------------------------------------------------- תמלול + עיבוד
def transcribe_with_retry(audio, attempts=3) -> str:
    """שרת לא יציב? עד 3 ניסיונות עם המתנה הולכת וגדלה."""
    for i in range(attempts):
        try:
            return transcribe(audio)
        except Exception as e:
            if i == attempts - 1 or MISSING_CREDS:
                raise
            log(f"תמלול נכשל (ניסיון {i + 1}): {e} — מנסה שוב")
            time.sleep(1.5 * (i + 1))
def transcribe(audio) -> str:
    fake = os.environ.get("FLOWHE_FAKE_TRANSCRIPT")
    if fake:
        time.sleep(float(os.environ.get("FLOWHE_FAKE_DELAY", "0.5")))
        if fake == "__dur__":
            return f"<{len(audio) / SAMPLE_RATE:.0f}s>"
        return fake
    reload_env_if_changed()
    if MISSING_CREDS:
        raise RuntimeError("חסר טוקן — העתק את .env לתיקייה " + HERE)
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


FIX_MODEL = CFG.get("fix_model", "claude-sonnet-5-5")  # Haiku "המציא" מילים בבדיקה על הקלטה אמיתית; Sonnet שמרני ומהיר (~2.5s)
_THINK_OFF = {}  # model -> הפרמטר שמכבה חשיבה (שונה בין מודלים); None = לא שולחים


def claude_text(model, system, user, max_tokens=2000):
    """קריאה ל-Claude שמחזירה רק טקסט (מדלגת על בלוקי חשיבה), ומכבה חשיבה כדי שיהיה מהיר."""
    kw = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    tries = [_THINK_OFF[model]] if model in _THINK_OFF else [{"type": "disabled"}, {"type": "between_tools"}, None]
    last = None
    for t in tries:
        try:
            msg = claude.messages.create(**kw, **({"thinking": t} if t else {}))
            _THINK_OFF[model] = t
            return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text").strip()
        except Exception as e:
            last = e
            if "thinking" not in str(e):
                raise
    raise last


PUNCT_PROMPT = (
    "אתה מוסיף פיסוק לתמלול דיבור בעברית. החזר את אותו הטקסט בדיוק, מילה במילה, ורק:\n"
    "- הוסף או תקן סימני פיסוק (נקודה, פסיק, סימן שאלה) לפי מבנה המשפט.\n"
    "- חלק לפסקאות אם הטקסט ארוך ויש מעבר נושא.\n"
    "- מחק גמגום: מילת מילוי (אה, אמם) או מילה שחוזרת פעמיים ברצף בטעות.\n"
    "אסור לשנות, להחליף, לתקן או להוסיף אף מילה — גם אם נראה לך שהמילה שגויה. "
    "אם משפט לא ברור לך — השאר אותו בלי שינוי.\n"
    "החזר רק את הטקסט, בלי הסברים."
)


def ai_clean(raw: str, style: str = "") -> str:
    """פיסוק חכם: המודל מוסיף רק פיסוק ופסקאות, ו-guard_words מוודא בקוד שאף מילה לא הוחלפה או נוספה —
    כל המילים בתוצאה הן המילים שהשרת תמלל. (בבדיקה על הקלטות אמיתיות, 'תיקון מילים' של מודל שפה הזיק.)"""
    base = textproc.tidy(raw)
    if not claude or not base:
        return base
    try:
        out = claude_text(FIX_MODEL, PUNCT_PROMPT, base)
        return textproc.guard_words(base, out) if out else base
    except Exception as e:
        log("פיסוק חכם נכשל, מכניס את התמלול כמו שהוא:", e)
        return base


def ai_command(selected: str, command: str) -> str:
    system = (
        "אתה עוזר עריכה קולי. המשתמש בחר טקסט ואמר פקודה. בצע את הפקודה על הטקסט "
        "והחזר אך ורק את התוצאה, בלי הסברים. אם הפקודה היא שאלה ולא עריכה — ענה בקצרה.\n"
        + dictionary_prompt()
    )
    user = f"<text>\n{selected}\n</text>\n<command>{command}</command>" if selected else command
    return claude_text(MODEL, system, user)


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
        mt = rec.mouse_target
        # לוחצים בנקודת העכבר רק כשהעכבר מעל חלון *אחר* מזה שבו עבדת; באותו חלון — הטקסט נכנס איפה שהסמן כבר נמצא
        if mt and mt[0] == rec.target_hwnd:
            mt = None
        if mt and not PINNED["hwnd"] and ctypes.windll.user32.IsWindow(mt[0]):
            saved = rec.target_hwnd
            rec.target_hwnd = mt[0]
            ok = focus_target()
            rec.target_hwnd = saved
            if ok:
                click_at(mt[1], mt[2])
                log("מכניס לשדה שמתחת לעכבר:", window_title(mt[0]))
                insert_text(text); return
        if focus_target():
            insert_text(text)
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
    insert_text(LAST["text"])


def copy_last():
    if LAST["text"]:
        pyperclip.copy(LAST["text"]); ui_q.put(("flash", "📋 התמלול האחרון הועתק"))


# ---- הקלדה ישירה (Unicode SendInput): לאפליקציות שלא מקבלות Ctrl+V סינתטי (למשל Claude Desktop)
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_uint),
                ("time", ctypes.c_uint), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT_U(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint), ("u", _INPUT_U)]


def type_text(text: str):
    """מקליד את הטקסט כאירועי Unicode (לא תלוי בפריסת המקלדת ולא בלוח), במנות מהירות."""
    u = ctypes.windll.user32
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    VK_SHIFT, VK_RETURN = 0x10, 0x0D
    events = []

    def key(vk=0, scan=0, flags=0):
        events.append((vk, scan, flags))

    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":  # Shift+Enter: שורה חדשה בלי לשלוח הודעה בצ'אטים
            key(VK_SHIFT); key(VK_RETURN); key(VK_RETURN, flags=KEYEVENTF_KEYUP); key(VK_SHIFT, flags=KEYEVENTF_KEYUP)
            continue
        code = ord(ch)
        units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for unit in units:
            key(0, unit, KEYEVENTF_UNICODE); key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
    for i in range(0, len(events), 60):
        batch = events[i:i + 60]
        arr = (_INPUT * len(batch))()
        for j, (vk, scan, flags) in enumerate(batch):
            arr[j].type = 1  # INPUT_KEYBOARD
            arr[j].u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        u.SendInput(len(batch), arr, ctypes.sizeof(_INPUT))
        time.sleep(0.01)
    log("הוקלד לחלון:", active_window_title())


def is_elevated_window(h) -> bool:
    """האם החלון שייך לתהליך שרץ כמנהל (ואנחנו לא) — אז Windows חוסם הקלדה/הדבקה אליו."""
    try:
        u, k, a = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.advapi32
        pid = ctypes.c_ulong(); u.GetWindowThreadProcessId(ctypes.c_void_p(h), ctypes.byref(pid))
        k.OpenProcess.restype = ctypes.c_void_p
        hp = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not hp:
            return False
        tok = ctypes.c_void_p()
        try:
            if not a.OpenProcessToken(ctypes.c_void_p(hp), 0x0008, ctypes.byref(tok)):
                return True  # אין גישה לטוקן — סימן שהוא ברמת הרשאה גבוהה יותר
            elev, ret = ctypes.c_ulong(), ctypes.c_ulong()
            a.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(ret))  # TokenElevation
            k.CloseHandle(tok)
            mine = ctypes.windll.shell32.IsUserAnAdmin()
            return bool(elev.value) and not mine
        finally:
            k.CloseHandle(ctypes.c_void_p(hp))
    except Exception as e:
        log("is_elevated_window:", e)
        return False


def insert_text(text: str):
    """מכניס טקסט לחלון הפעיל: הקלדה ישירה לאפליקציות שברשימה, הדבקה לכל השאר."""
    u = ctypes.windll.user32
    fg = u.GetForegroundWindow()
    title = window_title(fg)
    if is_elevated_window(fg):
        log("החלון רץ כמנהל — Windows חוסם הכנסת טקסט:", title)
        pyperclip.copy(text)
        ui_q.put(("flash", "⚠ החלון רץ כמנהל — הטקסט בלוח, לחץ Ctrl+V"))
        return
    # ברירת מחדל: הקלדה ישירה — עובדת בכל מקום (דפדפן, פנקס, Claude, וואטסאפ). הדבקה רק לאפליקציות שברשימה.
    paste_apps = [t.lower() for t in CFG.get("paste_apps", [])]
    method = CFG.get("insert_method", "type")
    if method == "paste" or any(t and t in title.lower() for t in paste_apps):
        paste(text)
    else:
        type_text(text)


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
    time.sleep(1.0)
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
    LIVE["session"] = rec.session
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
    session = rec.session; rec.session = None
    peak = 0.0 if audio is None else float(np.abs(audio).max())
    log(f"הקלטה נגמרה: {secs:.1f}s, דגימות={0 if audio is None else len(audio)}, שיא={peak:.4f}"
        + (f", קטעים שכבר נשלחו: {session.sent_before_end}" if session else ""))
    if audio is None or secs < 0.4:
        LIVE["session"] = None
        if session: session.abort()
        log("קצר מדי / אין אודיו"); ui_q.put(("hide", None)); return
    if peak < float(CFG.get("silent_mic_peak", 0.004)) and not os.environ.get("FLOWHE_FAKE_AUDIO"):
        LIVE["session"] = None
        if session: session.abort()
        log("המיקרופון לא קלט קול"); beep("error")
        ui_q.put(("flash", "🎙✖ המיקרופון לא קולט קול — בדוק מיקרופון בתפריט ▾")); return
    ui_q.put(("show", "מתמלל…"))
    PROCESSING["n"] += 1
    threading.Thread(target=process, args=(kind, audio, secs, session), daemon=True).start()


def set_mode(m):
    global mode
    mode = m


def quiet_cancel():
    """ביטול שקט (הקשה קצרה שלא הפכה ללחיצה כפולה)."""
    global mode
    if mode:
        mode = None; rec.stop(); LIVE["session"] = None; ui_q.put(("hide", None)); log("הקשה קצרה — בוטל")


def cancel():
    global mode
    with lock:
        if not mode:
            return
        mode = None
        rec.stop()
    LIVE["session"] = None
    log("בוטל")
    beep("cancel")
    ui_q.put(("flash", "✖ בוטל"))


LIVE = {"session": None}  # הסשן שהטקסט החי שלו מוצג בבר (בהקלטה ועד סוף התמלול)
PROCESSING = {"n": 0}  # כמה תמלולים בעיבוד כרגע (עד סוף ההדבקה)
FAILED_DIR = P("failed")


def save_failed(audio):
    try:
        os.makedirs(FAILED_DIR, exist_ok=True)
        f = os.path.join(FAILED_DIR, f"{datetime.now():%Y%m%d-%H%M%S}.wav")
        sf.write(f, audio, SAMPLE_RATE, subtype="PCM_16")
        log("הקלטה שנכשלה נשמרה:", f)
        ui_q.put(("flash", "⚠ התמלול נכשל — ההקלטה נשמרה. ▾ ← 'נסה שוב'"))
    except Exception as e:
        log("save_failed:", e)


def failed_files():
    try:
        return sorted(os.path.join(FAILED_DIR, f) for f in os.listdir(FAILED_DIR) if f.endswith(".wav"))
    except OSError:
        return []


def retry_failed():
    """מתמלל מחדש הקלטות שנכשלו ומציג אותן בחלון התוצאות (+ בלוח)."""
    files = failed_files()
    if not files:
        ui_q.put(("flash", "אין הקלטות שנכשלו")); return
    ui_q.put(("show", f"מתמלל מחדש {len(files)} הקלטות…"))
    out = []
    for f in files:
        try:
            audio, _ = sf.read(f, dtype="float32")
            ch = Chunker(SAMPLE_RATE); segs = ch.feed(audio) + [ch.flush()]
            text = " ".join(t for t in (transcribe_with_retry(normalize(x)) for x in segs if not is_silent(x)) if t)
            out.append(text)
            os.replace(f, f + ".done")
        except Exception as e:
            log("retry_failed:", f, e)
    if out:
        joined = "\n\n".join(out); LAST["text"] = joined; pyperclip.copy(joined)
        ui_q.put(("result", joined))
    else:
        ui_q.put(("flash", "עדיין נכשל — בדוק שהשרת פעיל"))


def process(kind, audio, secs, session=None):
    try:
        _process(kind, audio, secs, session)
    finally:
        if LIVE["session"] is session:
            LIVE["session"] = None
        PROCESSING["n"] -= 1


def _process(kind, audio, secs, session=None):
    app = active_window_title()
    try:
        try:
            sf.write(P("last.wav"), audio, SAMPLE_RATE, subtype="PCM_16")
        except Exception:
            pass
        t0 = time.time()
        try:
            raw = session.collect(audio) if session else transcribe_full(audio)
        except Exception:
            save_failed(audio)
            raise
        LAST["wait_s"] = round(time.time() - t0, 1)
        log(f"תמלול ({time.time()-t0:.1f}s אחרי סוף ההקלטה) מתוך [{app}]: {raw!r}")
        if not raw:
            ui_q.put(("flash", "לא זוהה דיבור")); return
        if kind == "command":
            selected = get_selection()
            if not claude:
                ui_q.put(("flash", "Command Mode דורש ANTHROPIC_API_KEY")); return
            ui_q.put(("show", "מבצע…"))
            final = ai_command(selected, raw)
            deliver(final)
        else:
            final = apply_dictionary(textproc.tidy(raw))
            if AI_EDITS:
                ui_q.put(("show", "מפסק…"))
                final = apply_dictionary(ai_clean(final))
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
BG, FG, ACCENT, REC, DIM = "#18181e", "#f2f2f2", "#8b7bff", "#f43f5e", "#8a8a96"
def rtl(t: str) -> str:
    """Tk מצייר בכיוון שמאל-לימין: סימן RLM אחרי כל פיסוק מצמיד אותו לצד הנכון בעברית."""
    if not t:
        return t
    return "\u200f" + re.sub(r"([.,!?:;)\]…]+)", "\\1\u200f", t)


TARGET_LABELS = {"paste": "הדבק בסמן", "clipboard": "העתק ללוח", "window": "הצג בחלון"}


def hwnd_of(win):
    try:
        return ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
    except Exception:
        return 0


def screen_scale():
    """קנה המידה של המסך (125% -> 1.25), כדי שהבר ייראה חד ובגודל נכון גם במסכי DPI גבוה."""
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


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

    BW, BH = 300, 46   # גודל לוגי; בפועל מוכפל בקנה המידה של המסך (DPI)
    KEY = "#010203"    # צבע שקוף — הפינות מחוץ לבר המעוגל

    def __init__(self, root):
        self.root = root
        self.sc = screen_scale()
        self.W, self.H = round(self.BW * self.sc), round(self.BH * self.sc)
        root.title("Flow-HE")
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-alpha", 0.97)
        root.configure(bg=self.KEY)
        try:
            root.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            pass
        self._img = None
        self.c = tk.Canvas(root, width=self.W, height=self.H, bg=self.KEY, highlightthickness=0, cursor="hand2")
        self.c.pack()
        self.state = "idle"       # idle | rec | busy | flash
        self.cur_h = self.H       # גובה נוכחי (הבר גדל כשיש טקסט חי)
        self.live_ver = -1
        self.visible = True
        self.flash_job = None
        self.menu = None
        self.result_win = None
        self.lecture_win = None
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
        self._sync_visibility()
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
    def X(self, v):
        return v * self.sc

    def _resize(self, h):
        """משנה את גובה הבר; התחתית נשארת במקום (הבר גדל כלפי מעלה)."""
        if h == self.cur_h:
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        y = max(0, y - (h - self.cur_h))
        self.cur_h = h
        self.c.config(height=h)
        self.root.geometry(f"{self.W}x{h}+{x}+{y}")

    def _live(self):
        """הטקסט החי להצגה (סוף הטקסט, עד ~3 שורות), או ''."""
        if not CFG.get("live_preview", True) or self.state not in ("rec", "busy"):
            return ""
        ses = LIVE["session"]
        t = ses.live_text() if ses else ""
        if len(t) > 120:
            t = "…" + t[-118:].split(" ", 1)[-1]
        return rtl(t)

    def draw(self, level=0.0, secs=0.0):
        c = self.c
        c.delete("all")
        live = self._live()
        extra = 0
        if live:
            item = c.create_text(self.W - self.X(16), 0, text=live, fill=FG, font=("Segoe UI", 10), anchor="ne",
                                 justify="right", width=self.W - self.X(32))
            bb = c.bbox(item); c.delete(item)
            extra = round((bb[3] - bb[1]) + self.X(18)) if bb else 0
        self._resize(self.H + extra)
        # הרקע (בר + כרטיס טקסט) מצויר חלק ב-PIL; הטקסט והאזורים הלחיצים — של Tk
        img = ui_art.render_bar(self.W, self.cur_h, self.sc, self.state, oy=extra, level=level,
                                t=time.time() if self.state == "busy" else secs, card_h=extra, bg_key=(1, 2, 3))
        self._img = ImageTk.PhotoImage(img)
        c.create_image(0, 0, image=self._img, anchor="nw")
        if extra:
            c.create_text(self.W - self.X(16), self.X(9), text=live, fill=FG, font=("Segoe UI", 10), anchor="ne",
                          justify="right", width=self.W - self.X(32), tags="live")
        self._draw_pill(level, secs, extra)

    def _draw_pill(self, level, secs, oy):
        c, X = self.c, self.X
        cy = oy + self.H / 2
        hit = lambda x0, x1, tag: c.create_rectangle(x0, cy - X(20), x1, cy + X(20), fill="", outline="", tags=tag)
        hit(X(6), X(42), "main")  # הכפתור העגול (מצויר בתמונה)
        if self.state == "rec":
            c.create_text(X(186), cy, text=f"{int(secs // 60)}:{int(secs % 60):02d}", fill=FG, font=("Segoe UI", 11, "bold"), anchor="w")
            c.create_text(self.W - X(22), cy, text="✕", fill=DIM, font=("Segoe UI", 12), tags="cancel")
            hit(self.W - X(40), self.W - X(4), "cancel")
        elif self.state in ("busy", "flash"):
            c.create_text(X(48), cy, text=rtl(self.msg), fill=FG, font=("Segoe UI", 10), anchor="w", width=self.W - X(60))
        else:  # idle
            c.create_text(X(48), cy, text="לחץ לדיבור", fill=FG, font=("Segoe UI", 10, "bold"), anchor="w")
            c.create_text(self.W - X(14), cy, text="–", fill=DIM, font=("Segoe UI", 14, "bold"), anchor="e", tags="hide")
            hit(self.W - X(26), self.W - X(2), "hide")
            label = "איפה שהסמן" if OUTPUT_TARGET == "paste" else TARGET_LABELS[OUTPUT_TARGET]
            if OUTPUT_TARGET == "paste" and PINNED["hwnd"]:
                label = "→ " + short(PINNED["title"], 16)
            c.create_text(self.W - X(30), cy, text="▾", fill=DIM, font=("Segoe UI", 12), anchor="e", tags="target")
            c.create_text(self.W - X(44), cy, text=label, fill=ACCENT if PINNED["hwnd"] and OUTPUT_TARGET == "paste" else DIM,
                          font=("Segoe UI", 9), anchor="e", tags="target")

    # ---- אירועי עכבר
    def _hit(self, x, y):
        for item in self.c.find_overlapping(x, y, x, y):
            tags = self.c.gettags(item)
            if "main" in tags: return "main"
            if "cancel" in tags: return "cancel"
            if "target" in tags: return "target"
            if "hide" in tags: return "hide"
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
            BAR_POS = [self.root.winfo_x(), self.root.winfo_y() + (self.cur_h - self.H)]; save_config(); return
        if hit == "main":
            if self.state == "idle" or self.state == "flash":
                begin("dictate")
            elif self.state == "rec":
                finish()
        elif hit == "cancel" and self.state == "rec":
            cancel()
        elif hit == "target" and self.state in ("idle", "flash"):
            self._target_menu(e)
        elif hit == "hide" and self.state in ("idle", "flash"):
            self.minimize()

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
        m.add_command(label=("   ■ עצור הקלטת שיעור" if Lecture.active else "   🎓 הקלטת שיעור רצופה (נשמר לקובץ)"), command=lambda: ui_q.put(("lecture", None)))
        m.add_separator()
        m.add_command(label=("● " if OUTPUT_TARGET == "clipboard" else "   ") + "רק העתק ללוח", command=lambda: self._set_target("clipboard"))
        m.add_command(label=("● " if OUTPUT_TARGET == "window" else "   ") + "הצג בחלון תוצאות", command=lambda: self._set_target("window"))
        m.add_separator()
        m.add_command(label="   הדבק שוב את האחרון  (Alt+Shift+Z)", command=lambda: threading.Thread(target=paste_last, daemon=True).start())
        nf = len(failed_files())
        if nf:
            m.add_command(label=f"   ⟳ נסה שוב הקלטות שנכשלו ({nf})", command=lambda: threading.Thread(target=retry_failed, daemon=True).start())
        mm = mk(m)
        cur = CFG.get("mic_device")
        mm.add_command(label=("● " if not cur else "   ") + "ברירת המחדל של Windows", command=lambda: self._set_mic(None))
        for name in list_mics():
            mm.add_command(label=("● " if name == cur else "   ") + name, command=lambda n=name: self._set_mic(n))
        m.add_cascade(label="   🎙 מיקרופון" + (f" ({short(cur, 18)})" if cur else ""), menu=mm)
        on = bool(CFG.get("launch_at_login", True))
        m.add_command(label=("☑ " if on else "☐ ") + "הפעל עם Windows", command=self._toggle_login)
        m.add_command(label="   היסטוריה / חלון תוצאות", command=lambda: self.show_result(None))
        m.add_command(label="   פתח תיקיית הגדרות", command=lambda: os.startfile(HERE))
        m.add_command(label="   יציאה", command=lambda: ui_q.put(("quit", None)))
        v = available_update()
        if v:
            m.add_command(label=f"   ⬆ עדכן עכשיו לגרסה {v}", command=lambda: check_for_update(force=True))
        m.add_command(label=f"   גרסה {VERSION}", state="disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _set_mic(self, name):
        CFG["mic_device"] = name; save_config()
        self.set_state("flash", "🎙 " + (short(name, 30) if name else "מיקרופון ברירת מחדל"))

    def _toggle_login(self):
        CFG["launch_at_login"] = not bool(CFG.get("launch_at_login", True)); save_config()
        set_launch_at_login(CFG["launch_at_login"])
        self.set_state("flash", "יופעל עם Windows" if CFG["launch_at_login"] else "לא יופעל עם Windows")

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

    # ---- נראות: הבר מוסתר כברירת מחדל, נפתח מהמגש, ומופיע לבד בזמן הקלטה/תמלול
    def _show(self):
        h = hwnd_of(self.root)
        if h:
            ctypes.windll.user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE — לא גונב פוקוס
            self.root.attributes("-topmost", True)
        self.visible = True

    def _hide(self):
        h = hwnd_of(self.root)
        if h:
            ctypes.windll.user32.ShowWindow(h, 0)  # SW_HIDE
        self.visible = False

    def minimize(self):
        BAR_PINNED["on"] = False; save_config(); self._hide()
        log("הבר מוזער למגש")

    def open_bar(self):
        BAR_PINNED["on"] = True; save_config(); self._show()

    def toggle_bar(self):
        self.minimize() if BAR_PINNED["on"] else self.open_bar()

    def _sync_visibility(self):
        want = BAR_PINNED["on"] or self.state in ("rec", "busy", "flash")
        if want and not self.visible:
            self._show()
        elif not want and self.visible:
            self._hide()

    # ---- מצבים
    def set_state(self, state, msg=""):
        if self.flash_job:
            self.root.after_cancel(self.flash_job); self.flash_job = None
        self.state, self.msg = state, msg
        self.draw()
        self._sync_visibility()
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
        elif self.state == "busy" and LIVE["session"] is not None and LIVE["session"].version != self.live_ver:
            self.live_ver = LIVE["session"].version
            self.draw()
        self.root.after(70, self._tick)

    # ---- חלון תוצאות / היסטוריה
    def show_result(self, text):
        if self.result_win is None or not self.result_win.winfo_exists():
            self.result_win = ResultWindow(self.root)
        self.result_win.show(text)


class Lecture:
    """מצב שיעור: הקלטה רצופה (גם שעות), תמלול בחלונות של ~חצי דקה שנחתכים בהפסקות,
    והטקסט נכתב תוך כדי לקובץ lectures/<תאריך>.md (+ האודיו ב-.flac). לא מדביק בשום מקום."""

    active = None

    def __init__(self):
        os.makedirs(P("lectures"), exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.md, self.audio_path = P(f"lectures/שיעור_{stamp}.md"), P(f"lectures/שיעור_{stamp}.flac")
        with open(self.md, "w", encoding="utf-8") as f:
            f.write(f"# שיעור — {datetime.now():%d/%m/%Y %H:%M}\n\n")
        self.chunker = Chunker(SAMPLE_RATE, min_s=float(CFG.get("lecture_min_s", 15.0)), max_s=float(CFG.get("lecture_max_s", 28.0)))
        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=int(CFG.get("parallel_requests", 3)))
        self.results, self.next_i, self.n_sent = {}, 0, 0
        self.lock = threading.Lock()
        self.text = []            # הטקסט שכבר נכתב לקובץ, לפי הסדר
        self.version = 0
        self.on, self.t0, self.stream, self.snd = True, time.time(), None, None
        try:
            self.snd = sf.SoundFile(self.audio_path, "w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")
        except Exception as e:
            log("שיעור: לא נשמר אודיו:", e)
        threading.Thread(target=self._run, daemon=True).start()
        fake = os.environ.get("FLOWHE_FAKE_AUDIO")
        if fake:
            threading.Thread(target=self._fake, daemon=True).start()
        else:
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                         callback=lambda x, *a: self.q.put(x.copy().reshape(-1)) if self.on else None,
                                         device=mic_device_index())
            self.stream.start()
        Lecture.active = self
        log("שיעור: התחיל ->", self.md)

    def _fake(self):
        t, block = 0.0, 0.1
        while self.on:
            n = int(block * SAMPLE_RATE); tt = np.arange(n) / SAMPLE_RATE + t
            self.q.put((0.2 * np.sin(2 * np.pi * 220 * tt) * ((tt % 5.6) < 5.0)).astype("float32"))
            t += block; time.sleep(block)

    def _run(self):
        while True:
            x = self.q.get()
            if x is None:
                break
            if self.snd:
                try: self.snd.write(x)
                except Exception: pass
            for seg in self.chunker.feed(x):
                self._submit(seg)
        self._submit(self.chunker.flush())
        for f in list(self._futs):
            try: f.result(timeout=300)
            except Exception: pass
        if self.snd:
            self.snd.close()
        self.pool.shutdown(wait=False)
        log("שיעור: נשמר", self.md)
        ui_q.put(("flash", "🎓 השיעור נשמר בתיקייה lectures"))
        ui_q.put(("lecture_done", self.md))

    _futs = ()

    def _submit(self, seg):
        if not len(seg) or is_silent(seg):
            return
        i = self.n_sent; self.n_sent += 1
        f = self.pool.submit(self._job, i, seg)
        self._futs = list(self._futs) + [f]

    def _job(self, i, seg):
        try:
            t = textproc.clean_chunk(transcribe_with_retry(normalize(seg)))
            if t:
                t = apply_dictionary(t)
                if AI_EDITS and CFG.get("lecture_fix", True):
                    t = ai_clean(t)
        except Exception as e:
            log("שיעור: קטע נכשל:", e)
            t = "[קטע לא תומלל]"
        with self.lock:
            self.results[i] = t
            # כותבים לקובץ רק לפי הסדר
            while self.next_i in self.results:
                piece = self.results.pop(self.next_i); self.next_i += 1
                if piece:
                    self.text.append(piece)
                    with open(self.md, "a", encoding="utf-8") as f:
                        f.write(piece + "\n\n")
            self.version += 1

    def pending(self):
        return self.n_sent - self.next_i

    def stop(self):
        if not self.on:
            return
        self.on = False
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
        self.q.put(None)
        Lecture.active = None
        log(f"שיעור: נעצר אחרי {time.time() - self.t0:.0f}s, ממתין ל-{self.pending()} קטעים")


def toggle_lecture():
    if Lecture.active:
        Lecture.active.stop()
    else:
        try:
            Lecture()
            beep("start")
            ui_q.put(("lecture_win", None))
        except Exception as e:
            log("שיעור:", e); beep("error")
            ui_q.put(("flash", f"שגיאה בהקלטת שיעור: {e}"))


class LectureWindow:
    """חלון השיעור: הטקסט מתמלא תוך כדי, זמן, ועצירה."""

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("Flow-HE — שיעור")
        self.win.configure(bg=BG)
        self.win.geometry("520x520")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.win.update_idletasks()
        OWN_HWNDS.add(hwnd_of(self.win))
        top = tk.Frame(self.win, bg=BG); top.pack(fill="x", padx=12, pady=(12, 6))
        self.btn = tk.Button(top, text="■ עצור", command=toggle_lecture, bg=REC, fg="white", relief="flat", padx=14, font=("Segoe UI", 10, "bold"))
        self.btn.pack(side="right")
        tk.Button(top, text="📂 תיקיית השיעורים", command=lambda: os.startfile(P("lectures")), bg="#2a2a34", fg=FG, relief="flat", padx=10).pack(side="right", padx=6)
        self.status = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 10))
        self.status.pack(side="left")
        self.text = tk.Text(self.win, bg="#1e1e26", fg=FG, font=("Segoe UI", 12), wrap="word", relief="flat", padx=12, pady=10)
        self.text.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.text.tag_configure("rtl", justify="right")
        self.ses, self.ver, self.shown = None, -1, 0

    def show(self, ses):
        self.ses, self.ver, self.shown = ses, -1, 0
        self.text.delete("1.0", "end")
        self.btn.config(text="■ עצור", state="normal")
        self.win.deiconify(); self.win.lift()
        self._tick()

    def _tick(self):
        ses = self.ses
        if ses is None or not self.win.winfo_exists():
            return
        if ses.version != self.ver:
            self.ver = ses.version
            with ses.lock:
                new = ses.text[self.shown:]; self.shown = len(ses.text)
            for piece in new:
                self.text.insert("end", rtl(piece) + "\n\n", "rtl")
            if new:
                self.text.see("end")
        el = int(time.time() - ses.t0)
        if ses.on:
            st = f"● מקליט {el // 3600}:{el // 60 % 60:02d}:{el % 60:02d}"
        else:
            st = "נעצר"
            self.btn.config(text="נעצר", state="disabled")
        if ses.pending():
            st += f" · {ses.pending()} קטעים בתמלול"
        elif not ses.on:
            st = "✓ נשמר"
        self.status.config(text=st)
        if ses.on or ses.pending():
            self.win.after(400, self._tick)


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
            elif ev == "toggle_bar":
                bar.toggle_bar()
            elif ev == "open_bar":
                bar.open_bar()
            elif ev == "update":
                check_for_update(force=True)
            elif ev == "lecture":
                toggle_lecture()
            elif ev == "lecture_win" and Lecture.active:
                if bar.lecture_win is None or not bar.lecture_win.win.winfo_exists():
                    bar.lecture_win = LectureWindow(root)
                bar.lecture_win.show(Lecture.active)
    except queue.Empty:
        pass
    root.after(60, pump, root, bar)


# ---------------------------------------------------------------- מגש מערכת
def tray():
    if not pystray:
        return
    img = ui_art.make_logo(64)
    menu = pystray.Menu(
        pystray.MenuItem("הצג / הסתר את הבר", lambda: ui_q.put(("toggle_bar", None)), default=True),
        pystray.MenuItem(lambda _: stats(), None, enabled=False),
        pystray.MenuItem(lambda _: ("■ עצור הקלטת שיעור" if Lecture.active else "🎓 הקלטת שיעור"), lambda: ui_q.put(("lecture", None))),
        pystray.MenuItem("חלון תוצאות / היסטוריה", lambda: ui_q.put(("result", None))),
        pystray.MenuItem(lambda _: (f"⬆ עדכן לגרסה {available_update()}" if available_update() else f"אין עדכון חדש (גרסה {VERSION})"),
                         lambda: ui_q.put(("update", None)), enabled=lambda _: bool(available_update())),
        pystray.MenuItem("פתח תיקיית הגדרות", lambda: os.startfile(HERE)),
        pystray.MenuItem("יציאה", lambda icon: (icon.stop(), ui_q.put(("quit", None)))),
    )
    pystray.Icon("flow_he", img, f"Flow-HE {VERSION} — החזק את {PTT_NAME} לדיבור", menu).run_detached()


# ---------------------------------------------------------------- self-test (רץ ב-GitHub Actions על Windows)
def selftest(root, bar):
    BAR_PINNED["on"] = True; bar._sync_visibility()
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
        bar.set_state("busy", "מתמלל…"); shot("busy")
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
                for _ in range(10):            # פנקס רשימות שזה עתה נפתח צריך רגע להיות מוכן לקלט
                    root.update(); time.sleep(0.15)
                report["windows_listed"] = [short(t, 40) for _, t in list_windows()][:8]
                bar._pin(np_h, window_title(np_h)); rec.target_hwnd = 0
                shot("pinned")
                # מדמים מצב אמיתי: הפוקוס עבר למקום אחר (הבר / שולחן העבודה)
                u.SetForegroundWindow(u.GetDesktopWindow()); root.update(); time.sleep(0.3)
                sample = "בדיקת הדבקה 123"
                deliver(sample); time.sleep(1.0)
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


def focus_hwnd(h):
    saved = rec.target_hwnd; rec.target_hwnd = h
    try:
        return focus_target()
    finally:
        rec.target_hwnd = saved


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

    def wait_idle(sec=10):
        pump_for(0.3)
        end = time.time() + sec
        while time.time() < end and (mode or PROCESSING["n"] > 0):
            pump_for(0.1)
        pump_for(0.5)

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
    pyautogui.keyDown(PTT_PYAUTOGUI); pump_for(1.3)
    e2e["caps_recording"] = mode == "dictate"
    pyautogui.keyUp(PTT_PYAUTOGUI)
    wait_idle()
    got = readback(); e2e["caps_text"] = got; e2e["caps_ok"] = expected in got
    e2e["caps_state_unchanged"] = (u.GetKeyState(0x14) & 1) == caps_before

    # 6) Caps Lock לחיצה כפולה -> ללא ידיים, לחיצה נוספת עוצרת
    clear()
    pyautogui.press(PTT_PYAUTOGUI); pump_for(0.1); pyautogui.press(PTT_PYAUTOGUI); pump_for(1.2)
    e2e["caps_double_tap_hands_free"] = mode == "dictate"
    pyautogui.press(PTT_PYAUTOGUI)
    wait_idle()
    got = readback(); e2e["caps_hands_free_text"] = got; e2e["caps_hands_free_ok"] = expected in got
    e2e["caps_state_unchanged_after_taps"] = (u.GetKeyState(0x14) & 1) == caps_before

    # 7) הטקסט נכנס לשדה שמתחת לעכבר, גם כשחלון אחר בפוקוס
    clear()
    subprocess.Popen(["notepad.exe"])
    hb = 0
    for _ in range(60):
        pump_for(0.25)
        x_ = u.GetForegroundWindow()
        if x_ and x_ != h and x_ not in OWN_HWNDS and "notepad" in active_window_title().lower():
            hb = x_; break
    e2e["second_notepad"] = bool(hb)
    if hb:
        u.MoveWindow(h, 0, 0, 500, 400, True); u.MoveWindow(hb, 520, 0, 500, 400, True); pump_for(0.5)
        # פוקוס על A, העכבר מעל אזור הטקסט של B
        focus_hwnd(h); pump_for(0.3)
        pyautogui.moveTo(770, 200); pump_for(0.4)
        e2e["mouse_target_detected"] = bool(mouse_text_target())
        pyautogui.keyDown(PTT_PYAUTOGUI); pump_for(1.2); pyautogui.keyUp(PTT_PYAUTOGUI)
        wait_idle(); pump_for(0.5)
        focus_hwnd(hb); got_b = readback()
        focus_hwnd(h); got_a = readback()
        e2e["mouse_b_text"], e2e["mouse_a_text"] = got_b, got_a
        e2e["mouse_target_ok"] = expected in got_b and expected not in got_a
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_mouse_target.png"))

    # 9) בר מוסתר: לא נראה במנוחה, מופיע בזמן הקלטה, נעלם אחרי
    focus_hwnd(h); clear()
    bar.minimize(); bar.set_state("idle"); pump_for(0.4)
    bh = hwnd_of(root)
    e2e["hidden_when_idle"] = not u.IsWindowVisible(bh)
    pyautogui.keyDown(PTT_PYAUTOGUI); pump_for(0.8)
    e2e["visible_while_recording"] = bool(u.IsWindowVisible(bh))
    ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_hidden_mode_recording.png"))
    pyautogui.keyUp(PTT_PYAUTOGUI); wait_idle(); pump_for(3.0)
    e2e["hidden_after_done"] = not u.IsWindowVisible(bh)
    e2e["hidden_mode_text_ok"] = expected in readback()
    bar.open_bar(); pump_for(0.3)
    e2e["tray_open_shows"] = bool(u.IsWindowVisible(bh))
    report["ok"] &= all(e2e.get(k) for k in ("hidden_when_idle", "visible_while_recording", "hidden_after_done",
                                              "hidden_mode_text_ok", "tray_open_shows"))

    # 10) תמלול תוך כדי הקלטה: 13 שניות "דיבור" עם הפסקות, כל קטע לוקח 2 שניות בשרת
    focus_hwnd(h); clear()
    saved_env = {k: os.environ.get(k) for k in ("FLOWHE_FAKE_AUDIO", "FLOWHE_FAKE_TRANSCRIPT", "FLOWHE_FAKE_DELAY")}
    os.environ.update(FLOWHE_FAKE_AUDIO="stream", FLOWHE_FAKE_TRANSCRIPT="__dur__", FLOWHE_FAKE_DELAY="2")
    pyautogui.press(PTT_PYAUTOGUI); pump_for(0.1); pyautogui.press(PTT_PYAUTOGUI)   # ללא ידיים
    pump_for(13)
    sent_live = rec.session.sent_before_end if rec.session else 0
    e2e["live_text_while_recording"] = LIVE["session"].live_text() if LIVE["session"] else ""
    e2e["bar_grew_while_recording"] = bar.cur_h > bar.H
    try:
        bx, by = bar.root.winfo_x(), bar.root.winfo_y()
        ImageGrab.grab(bbox=(bx - 20, by - 20, bx + bar.W + 20, by + bar.cur_h + 20), all_screens=True).save(os.path.join(out, "bar_live_stream.png"))
    except Exception as ex:
        log("shot live:", ex)
    t_stop = time.time()
    pyautogui.press(PTT_PYAUTOGUI)                                                 # עצירה
    wait_idle(20)
    e2e["stream_chunks_sent_while_recording"] = sent_live
    e2e["stream_wait_after_stop_s"] = round(time.time() - t_stop, 1)
    got = readback(); e2e["stream_text"] = got
    # התצוגה החיה מגיעה מקטעים; הטקסט הסופי — מתמלול אחד של ההקלטה כולה (~13 שניות)
    e2e["stream_ok"] = (sent_live >= 2 and got.count("<") == 1 and e2e["stream_wait_after_stop_s"] < 6.0
                        and e2e["live_text_while_recording"].count("<") >= 1 and e2e["bar_grew_while_recording"])
    e2e["bar_back_to_normal"] = bar.cur_h == bar.H
    report["ok"] &= e2e["bar_back_to_normal"]

    # 10ב) צילום של טקסט חי בעברית (בדיקה ויזואלית של RTL ופיסוק)
    class _Demo:
        version = 1
        def live_text(self):
            return "שלום, זו בדיקה של תמלול חי בעברית. האם הפיסוק בצד הנכון? כן! ונוסיף עוד שורה כדי לראות גלישה של טקסט ארוך, עם Vercel ו-GitHub באמצע."
    LIVE["session"] = _Demo(); bar.set_state("busy", "מתמלל…"); pump_for(0.4)
    try:
        bx, by = bar.root.winfo_x(), bar.root.winfo_y()
        ImageGrab.grab(bbox=(bx - 20, by - 20, bx + bar.W + 20, by + bar.cur_h + 20), all_screens=True).save(os.path.join(out, "bar_live_hebrew.png"))
    except Exception as ex:
        log("shot live he:", ex)
    LIVE["session"] = None; bar.set_state("idle"); pump_for(0.2)
    for k_, v_ in saved_env.items():
        if v_ is None: os.environ.pop(k_, None)
        else: os.environ[k_] = v_
    report["ok"] &= bool(e2e["stream_ok"])

    # 11) מצב שיעור: 13 שניות "הרצאה" -> קובץ md שמתמלא לפי הסדר, חלון שיעור, ואז עצירה ושמירה
    saved_env = {k: os.environ.get(k) for k in ("FLOWHE_FAKE_AUDIO", "FLOWHE_FAKE_TRANSCRIPT", "FLOWHE_FAKE_DELAY")}
    os.environ.update(FLOWHE_FAKE_AUDIO="stream", FLOWHE_FAKE_TRANSCRIPT="__dur__", FLOWHE_FAKE_DELAY="1")
    CFG["lecture_min_s"], CFG["lecture_max_s"] = 3.0, 6.0
    ui_q.put(("lecture", None)); pump_for(13)
    lec = Lecture.active
    e2e["lecture_started"] = lec is not None
    e2e["lecture_window"] = bool(bar.lecture_win and bar.lecture_win.win.winfo_viewable())
    try:
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_lecture.png"))
    except Exception:
        pass
    ui_q.put(("lecture", None)); pump_for(0.5)
    t_end = time.time() + 20
    while lec and lec.pending() and time.time() < t_end:
        pump_for(0.3)
    pump_for(1.0)
    try:
        body = open(lec.md, encoding="utf-8").read() if lec else ""
    except Exception:
        body = ""
    e2e["lecture_file_text"] = body[-200:]
    e2e["lecture_ok"] = e2e["lecture_started"] and e2e["lecture_window"] and body.count("<") >= 2 and Lecture.active is None \
        and os.path.exists(lec.audio_path)
    for k_, v_ in saved_env.items():
        if v_ is None: os.environ.pop(k_, None)
        else: os.environ[k_] = v_
    report["ok"] &= bool(e2e["lecture_ok"])

    # 8) מסלול ההקלדה הישירה (כמו ל-Claude Desktop): מגדירים את פנקס הרשימות כאפליקציית-הקלדה
    focus_hwnd(h); clear()
    pyperclip.copy("לוח-לא-אמור-להיות-מודבק")
    pyautogui.keyDown(PTT_PYAUTOGUI); pump_for(1.2); pyautogui.keyUp(PTT_PYAUTOGUI)
    wait_idle()
    got = readback(); e2e["typed_text"] = got; e2e["typed_ok"] = expected in got and "לוח-לא" not in got
    e2e["elevated_check_runs"] = is_elevated_window(h) in (True, False)

    report["ok"] &= all(e2e.get(k) for k in ("typed_ok", "elevated_check_runs"))
    report["ok"] &= all(e2e.get(k) for k in ("mouse_target_detected", "mouse_target_ok")) if e2e.get("second_notepad") else False
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


# מקש ההקלטה: ברירת מחדל המקש שמתחת ל-Esc (VK_OEM_3: ` באנגלית, ; בעברית). 0x14 = Caps Lock.
PTT_VK = int(str(CFG.get("ptt_key_vk", "0xC0")), 0)
PTT_NAME = {0xC0: "המקש שמתחת ל-Esc", 0x14: "Caps Lock"}.get(PTT_VK, f"מקש {PTT_VK:#x}")
PTT_PYAUTOGUI = {0xC0: "`", 0x14: "capslock"}.get(PTT_VK, "`")  # לבדיקות
CAPS = HoldTrigger(PTT_NAME)


_caps_q: "queue.Queue[str]" = queue.Queue()


def _caps_worker():
    while True:
        ev = _caps_q.get()
        try:
            if SELFTEST:
                log("caps:", ev)
            CAPS.press() if ev == "down" else CAPS.release()
        except Exception as ex:
            log("caps worker:", ex)


def _caps_hook_thread():
    """hook מקלדת ישיר של Windows שבולע את Caps Lock (כמו ב-Wispr: לא מחליף אותיות גדולות)
    ומעביר את הלחיצה לתור. ה-hook עצמו חייב לחזור מהר, אחרת Windows מסיר אותו."""
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
    u.SetWindowsHookExW.restype = ctypes.c_void_p
    k.GetModuleHandleW.restype = ctypes.c_void_p
    state = {"down": False}

    def proc(n, wparam, lparam):
        if n == 0:
            kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            # לפי VK וגם לפי scan code 0x29 (המקש הפיזי שמתחת ל-Esc) — כך זה עובד בכל פריסת מקלדת, גם בעברית
            hit = kb.vkCode == PTT_VK or (PTT_VK == 0xC0 and kb.scanCode == 0x29 and not (kb.flags & 0x01))
            if hit and not any(
                    u.GetAsyncKeyState(m) & 0x8000 for m in (0x11, 0x12, 0x5B, 0x5C)):  # בלי Ctrl/Alt/Win (Ctrl+` נשאר לאפליקציות)
                is_down = wparam in (0x100, 0x104)  # WM_KEYDOWN / WM_SYSKEYDOWN
                if is_down != state["down"]:     # מתעלמים מ-auto-repeat
                    state["down"] = is_down
                    _caps_q.put("down" if is_down else "up")
                return 1                          # בולעים את המקש
        return u.CallNextHookEx(None, n, wparam, lparam)

    cb = HOOKPROC(proc)
    _caps_hook_thread.cb = cb  # שומרים הפניה כדי שלא ייאסף
    h = u.SetWindowsHookExW(13, cb, k.GetModuleHandleW(None), 0)  # WH_KEYBOARD_LL
    log(f"hook מקש הקלטה ({PTT_NAME}):", "פעיל" if h else f"נכשל ({k.GetLastError()})")
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))


def install_hotkeys():
    keyboard.hook(_on_key)
    if CFG.get("caps_lock_ptt", True) and not getattr(install_hotkeys, "caps_started", False):
        install_hotkeys.caps_started = True
        threading.Thread(target=_caps_worker, daemon=True).start()
        threading.Thread(target=_caps_hook_thread, daemon=True).start()
    keyboard.add_hotkey(HK_CANCEL, lambda: (HK_STATE.update(hands_free=False, pending_tap=0.0), cancel()))
    keyboard.add_hotkey(HK_SCRATCH, lambda: finish() if mode else begin("scratch"))

    print("Flow-HE מוכן.")
    try:
        print("  מיקרופון ברירת מחדל:", sd.query_devices(kind="input")["name"])
    except Exception as e:
        print("  אין מיקרופון ברירת מחדל:", e)
    print(f"  {PTT_NAME} (החזק)   דבר, שחרר = הטקסט נכנס איפה שהסמן · לחיצה כפולה = ללא ידיים")
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
    try:
        _icon = ImageTk.PhotoImage(ui_art.make_logo(64)); root.iconphoto(True, _icon); root._icon = _icon
    except Exception as e:
        log("icon:", e)
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
    if FROZEN and not SELFTEST:
        cleanup_after_update()
        threading.Thread(target=lambda: (time.sleep(5), cleanup_leftovers()), daemon=True).start()
        if CFG.get("launch_at_login", True):
            set_launch_at_login(True)
    if FROZEN and (not SELFTEST or os.environ.get("FLOWHE_UPDATE_TEST")):
        force_now = "--update" in sys.argv or bool(os.environ.get("FLOWHE_UPDATE_TEST"))
        if force_now and not available_update():
            root.after(1200, lambda: ui_q.put(("flash", f"אין עדכון חדש · גרסה {VERSION}")))
        notified = {"v": None}

        def _upd():
            v = available_update()
            if v and force_now:
                check_for_update(force=True)
            elif v and notified["v"] != v and not mode:
                notified["v"] = v
                log("עדכון זמין:", v)
                ui_q.put(("flash", f"⬆ גרסה {v} מוכנה — ▾ > עדכן עכשיו"))
            check_for_update()  # מתקין לבד רק אם auto_update פעיל
            root.after(int(CFG.get("update_check_s", 15)) * 1000, _upd)
        root.after(1500 if force_now else 3000, _upd)
        threading.Thread(target=ensure_shortcuts, daemon=True).start() if not SELFTEST else None
    log("גרסה:", VERSION)

    def _env_watch():
        reload_env_if_changed()
        root.after(4000, _env_watch)
    root.after(4000, _env_watch)
    if not SELFTEST and not MISSING_CREDS:
        root.after(900, lambda: ui_q.put(("flash", f"Flow-HE פועל · החזק את {PTT_NAME} ודבר · הבר במגש למטה")))
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
