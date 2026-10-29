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
from ctypes import wintypes
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
import meeting_doc
import summary
import speakers
from PIL import Image, ImageTk
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


ENV_FILES = (".env", "server.env")  # server.env (כתובת השרת שלך ב-Vercel + טוקן האפליקציה) גובר על .env


def _env_stamp():
    return tuple(os.path.getmtime(P(n)) if os.path.exists(P(n)) else 0 for n in ENV_FILES)


def load_env(override=False):
    _ENV_MTIME["t"] = _env_stamp()
    for i, name in enumerate(ENV_FILES):
        if not os.path.exists(P(name)):
            continue
        with open(P(name), encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if override or i > 0:
                        os.environ[k.strip()] = v.strip()
                    else:
                        os.environ.setdefault(k.strip(), v.strip())


def reload_env_if_changed():
    """אם קובץ .env השתנה (למשל הוחלף בקובץ עם טוקן) — טוענים מחדש בלי להפעיל את האפליקציה מחדש."""
    global URL, TOKEN, CLAUDE_KEY, MISSING_CREDS, AI_EDITS, claude
    if _env_stamp() == _ENV_MTIME["t"]:
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
ROOT = {"tk": None}


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
_FAKE_N = {"n": 0}


def _encode_audio(audio):
    """FLAC — בלי איבוד מידע, חצי מגודל WAV. (Opus נבדק על פגישה אמיתית והשמיט דיבור שקט של דוברת רחוקה —
    בכל רמת איכות. FLAC החזיר טקסט זהה ל-WAV.)"""
    buf = io.BytesIO()
    try:
        sf.write(buf, audio, SAMPLE_RATE, format="FLAC", subtype="PCM_16")
        return buf.getvalue()
    except Exception as e:
        log("FLAC לא זמין, שולח WAV:", e); buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def server_has_jobs() -> bool:
    """השרת שלנו ב-Vercel (flow-he-api) — תומך בחלוקה לדוברים ובעבודות ארוכות. שרת ישן — רק טקסט."""
    return bool(URL) and URL.rstrip("/").endswith("/api/transcribe")


def server_request(audio, diarize=False, num_speakers=None, timeout_s=1800):
    """שולח אודיו לשרת ומחזיר {"text", "segments"}. אם השרת מחזיר job_id (הפעלה קרה / עבודה ארוכה) — מחכים לתוצאה."""
    fake = os.environ.get("FLOWHE_FAKE_TRANSCRIPT")
    if fake:
        time.sleep(float(os.environ.get("FLOWHE_FAKE_DELAY", "0.5")))
        if fake == "__dur__":
            t = f"<{len(audio) / SAMPLE_RATE:.0f}s>"
        elif fake == "__uniq__":  # כל קטע טקסט שונה (בדיקת פגישה: בלי מיזוג/הסרת הד בטעות)
            _FAKE_N["n"] += 1
            t = f"משפט{_FAKE_N['n']} באורך {len(audio) / SAMPLE_RATE:.0f}"
        else:
            t = fake
        return {"text": t, "segments": [{"start": 0.0, "end": len(audio) / SAMPLE_RATE, "text": t, "speaker": None}]}
    reload_env_if_changed()
    if MISSING_CREDS:
        raise RuntimeError("חסר טוקן — העתק את .env לתיקייה " + HERE)
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    body = {"audio_blob": base64.b64encode(_encode_audio(audio)).decode(), "method": "ivrit", "language": "he"}
    if diarize:
        body["diarize"] = True
        if num_speakers and int(num_speakers) > 1:
            body["num_speakers"] = int(num_speakers)
    _LAST_SERVER["t"] = time.time()
    r = requests.post(URL, headers=h, json=body, timeout=150)
    r.raise_for_status()
    d = r.json()
    if d.get("job_id") and "transcription" not in d:
        job_url = URL.rstrip("/").rsplit("/api/", 1)[0] + "/api/job"
        end = time.time() + timeout_s
        while time.time() < end:
            time.sleep(1.5)
            d = requests.get(job_url, params={"id": d["job_id"]}, headers=h, timeout=60).json()
            if d.get("status") == "done":
                break
            if d.get("status") == "error":
                raise RuntimeError("שגיאת שרת: " + str(d.get("error")))
        else:
            raise RuntimeError("השרת לא סיים בזמן")
    if d.get("error"):
        raise RuntimeError("שגיאת שרת: " + str(d["error"]))
    return {"text": (d.get("transcription") or "").strip(), "segments": d.get("segments") or []}


def transcribe(audio) -> str:
    return server_request(audio)["text"]


_LAST_SERVER = {"t": 0.0}


def warm_server():
    """לחצת על מקש ההקלטה -> מעירים את השרת ברקע (אם לא היה בשימוש בשניות האחרונות),
    כדי שעד שהקטע הראשון מוכן הוא כבר ער. עולה כמה מילישניות GPU."""
    if not server_has_jobs() or os.environ.get("FLOWHE_FAKE_TRANSCRIPT") or MISSING_CREDS:
        return
    if time.time() - _LAST_SERVER["t"] < float(CFG.get("warm_skip_s", 10)):
        return
    _LAST_SERVER["t"] = time.time()

    def go():
        try:
            requests.post(URL.rstrip("/").rsplit("/api/", 1)[0] + "/api/warm",
                          headers={"Authorization": f"Bearer {TOKEN}"}, timeout=20)
        except Exception as e:
            log("warm:", e)
    threading.Thread(target=go, daemon=True).start()


def long_parts(path, part_s=None):
    """קובץ ארוך -> חלקים של עד ~18 דקות (נחתכים בנקודה שקטה), כל אחד נכנס בבקשה אחת לשרת. [(offset_s, audio)]"""
    part_s = float(part_s or CFG.get("server_part_s", 1080))
    ch = Chunker(SAMPLE_RATE, min_s=part_s * 0.8, max_s=part_s)
    out, pos = [], 0
    for block in sf.blocks(path, blocksize=SAMPLE_RATE * 30, dtype="float32"):
        for g in ch.feed(block.reshape(-1)):
            out.append((pos / SAMPLE_RATE, g)); pos += len(g)
    g = ch.flush()
    if len(g):
        out.append((pos / SAMPLE_RATE, g))
    return [(o, g) for o, g in out if not is_silent(g)]


def apply_dictionary(text: str) -> str:
    return textproc.apply_dictionary(text, DICT.get("replacements", {}))


def apply_snippets(text: str) -> str:
    return textproc.apply_snippets(text, SNIPPETS)


def dictionary_prompt() -> str:
    words = DICT.get("words", [])
    return ("מילים/שמות שחייבים להיכתב בדיוק כך: " + ", ".join(words) + "\n") if words else ""


FIX_MODEL = CFG.get("fix_model", "claude-sonnet-5-5")  # Haiku "המציא" מילים בבדיקה על הקלטה אמיתית; Sonnet שמרני ומהיר (~2.5s)
_THINK_OFF = {}  # model -> הפרמטר שמכבה חשיבה (שונה בין מודלים); None = לא שולחים


def claude_text(model, system, user, max_tokens=2000, client=None):
    """קריאה ל-Claude שמחזירה רק טקסט (מדלגת על בלוקי חשיבה), ומכבה חשיבה כדי שיהיה מהיר."""
    kw = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
    tries = [_THINK_OFF[model]] if model in _THINK_OFF else [{"type": "disabled"}, {"type": "between_tools"}, None]
    last = None
    for t in tries:
        try:
            msg = (client or claude).messages.create(**kw, **({"thinking": t} if t else {}))
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


def prepare_target() -> bool:
    """מביא את הפוקוס למקום שבו הטקסט צריך להיכנס (שדה מתחת לעכבר / החלון שהיה פעיל). True אם הצליח."""
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
            rec.mouse_target = None  # לא ללחוץ שוב (זה היה מזיז את הסמן באמצע טקסט זמני)
            log("מכניס לשדה שמתחת לעכבר:", window_title(mt[0]))
            return True
    return focus_target()


def send_backspaces(n: int):
    """מוחק n תווים — בדיוק מה שהקלדנו כטקסט זמני, לא יותר."""
    if n <= 0:
        return
    u = ctypes.windll.user32
    VK_BACK, KEYUP = 0x08, 0x0002
    for i in range(0, n, 30):
        k = min(30, n - i)
        arr = (_INPUT * (2 * k))()
        for j in range(k):
            arr[2 * j].type = arr[2 * j + 1].type = 1
            arr[2 * j].u.ki = _KEYBDINPUT(VK_BACK, 0, 0, 0, 0)
            arr[2 * j + 1].u.ki = _KEYBDINPUT(VK_BACK, 0, KEYUP, 0, 0)
        u.SendInput(2 * k, arr, ctypes.sizeof(_INPUT))
        time.sleep(0.01)


def _modifiers_down() -> bool:
    ga = ctypes.windll.user32.GetAsyncKeyState
    return any(ga(vk) & 0x8000 for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C))  # Shift/Ctrl/Alt/Win


class LiveTyper:
    """טקסט זמני בשדה עצמו בזמן ההקלטה: כל קטע שחוזר מוקלד איפה שהסמן, עם ' …' בסוף.
    סופרים כל תו שהוקלד; בסוף מוחקים בדיוק את המספר הזה ומקלידים את הטקסט הסופי.
    אם עברת לחלון אחר בזמן ההקלטה — מפסיקים ולא מוחקים שם כלום."""

    MARK = " …"

    def __init__(self, session):
        self.ses, self.on, self.hwnd = session, True, 0
        self.shown, self.count, self.started, self.dead = "", 0, False, False
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def _run(self):
        while self.on:
            time.sleep(0.25)
            try:
                self._step()
            except Exception as e:
                log("טקסט זמני:", e); self.dead = True
            if self.dead:
                return

    def _step(self):
        txt = self.ses.live_text()
        if not txt or self.dead or _modifiers_down():
            return  # בזמן Ctrl/Win לחוצים לא מקלידים (זה היה הופך לקיצורי מקלדת)
        with self.lock:
            if not self.on:
                return
            u = ctypes.windll.user32
            if not self.started:
                if not prepare_target():
                    self.dead = True; return
                fg = u.GetForegroundWindow()
                paste_apps = [t.lower() for t in CFG.get("paste_apps", [])]
                if is_elevated_window(fg) or CFG.get("insert_method", "type") == "paste" or \
                        any(t and t in window_title(fg).lower() for t in paste_apps):
                    self.dead = True; return
                self.hwnd, self.started = fg, True
            if u.GetForegroundWindow() != self.hwnd:
                log("טקסט זמני: החלון השתנה — מפסיק"); self.dead = True; return
            new = txt + self.MARK
            if new == self.shown:
                return
            if self.shown and new.startswith(self.shown[:-len(self.MARK)]):
                send_backspaces(len(self.MARK)); self.count -= len(self.MARK)
                add = new[len(self.shown) - len(self.MARK):]
            else:  # לא אמור לקרות (הטקסט רק גדל) — מוחקים את הזמני ומקלידים מחדש
                send_backspaces(self.count); self.count = 0
                add = new
            type_text(add); self.count += len(add)
            self.shown = new

    def stop(self):
        self.on = False
        self.thread.join(2)

    def finish(self, final: str) -> bool:
        """מחליף את הטקסט הזמני בסופי. True = טופל (אין צורך ב-deliver רגיל)."""
        self.stop()
        with self.lock:
            if not self.count:
                return False
            u = ctypes.windll.user32
            if u.GetForegroundWindow() != self.hwnd:
                rec.target_hwnd = self.hwnd; rec.mouse_target = None
                if not focus_target() or u.GetForegroundWindow() != self.hwnd:
                    log("טקסט זמני: לא הצלחתי לחזור לחלון — הסופי בלוח")
                    pyperclip.copy(final or "")
                    ui_q.put(("flash", "⚠ עברת חלון — הטקסט הסופי בלוח (Ctrl+V)"))
                    return True
            send_backspaces(self.count)
            log(f"טקסט זמני: נמחקו {self.count} תווים")
            self.count, self.shown = 0, ""
            if final:
                type_text(final)
            return True

    def discard(self):
        self.finish("")


def deliver(text: str):
    """מוציא את הטקסט לפי יעד הפלט שנבחר בבר."""
    if OUTPUT_TARGET == "clipboard":
        pyperclip.copy(text)
        ui_q.put(("flash", "📋 הועתק ללוח"))
    elif OUTPUT_TARGET == "window":
        pyperclip.copy(text)
        ui_q.put(("result", text))
    else:
        if prepare_target():
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
    warm_server()
    LIVE["session"] = rec.session
    LIVE["typer"] = None
    if (kind == "dictate" and rec.session and OUTPUT_TARGET == "paste" and CFG.get("live_insert", True)):
        LIVE["typer"] = LiveTyper(rec.session)
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
    typer = LIVE.get("typer"); LIVE["typer"] = None
    if typer:
        typer.stop()
    peak = 0.0 if audio is None else float(np.abs(audio).max())
    log(f"הקלטה נגמרה: {secs:.1f}s, דגימות={0 if audio is None else len(audio)}, שיא={peak:.4f}"
        + (f", קטעים שכבר נשלחו: {session.sent_before_end}" if session else ""))
    if audio is None or secs < 0.4:
        if typer: typer.discard()
        LIVE["session"] = None
        if session: session.abort()
        log("קצר מדי / אין אודיו"); ui_q.put(("hide", None)); return
    if peak < float(CFG.get("silent_mic_peak", 0.004)) and not os.environ.get("FLOWHE_FAKE_AUDIO"):
        LIVE["session"] = None
        if typer: typer.discard()
        if session: session.abort()
        log("המיקרופון לא קלט קול"); beep("error")
        ui_q.put(("flash", "🎙✖ המיקרופון לא קולט קול — בדוק מיקרופון בתפריט ▾")); return
    ui_q.put(("show", "מתמלל…"))
    PROCESSING["n"] += 1
    threading.Thread(target=process, args=(kind, audio, secs, session, typer), daemon=True).start()


def set_mode(m):
    global mode
    mode = m


def quiet_cancel():
    """ביטול שקט (הקשה קצרה שלא הפכה ללחיצה כפולה)."""
    global mode
    if mode:
        mode = None; rec.stop(); LIVE["session"] = None; ui_q.put(("hide", None)); log("הקשה קצרה — בוטל")
        t = LIVE.get("typer"); LIVE["typer"] = None
        if t: threading.Thread(target=t.discard, daemon=True).start()


def cancel():
    global mode
    with lock:
        if not mode:
            return
        mode = None
        rec.stop()
    LIVE["session"] = None
    t = LIVE.get("typer"); LIVE["typer"] = None
    if t: threading.Thread(target=t.discard, daemon=True).start()
    log("בוטל")
    beep("cancel")
    ui_q.put(("flash", "✖ בוטל"))


LIVE = {"session": None, "typer": None}  # הסשן שהטקסט החי שלו מוצג בבר (בהקלטה ועד סוף התמלול)
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


def process(kind, audio, secs, session=None, typer=None):
    try:
        _process(kind, audio, secs, session, typer)
    finally:
        if LIVE["session"] is session:
            LIVE["session"] = None
        PROCESSING["n"] -= 1


def _process(kind, audio, secs, session=None, typer=None):
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
            if typer: typer.discard()
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
            if AI_EDITS and CFG.get("dictation_fix", False):  # כבוי כברירת מחדל: בבדיקה על הקלטות אמיתיות לא שיפר
                ui_q.put(("show", "מפסק…"))
                final = apply_dictionary(ai_clean(final))
            final = apply_snippets(final)
            if kind == "scratch":
                with open(P("scratchpad.md"), "a", encoding="utf-8") as f:
                    f.write(f"\n### {datetime.now():%d/%m/%Y %H:%M}\n{final}\n")
            else:
                if not (typer and typer.finish(final)):
                    deliver(final)
        LAST["text"] = final
        log(f"פלט ({OUTPUT_TARGET}):", repr(final))
        log_history(kind, raw, final, secs, app)
        beep("done")
        if OUTPUT_TARGET != "window":
            ui_q.put(("flash", "✅ " + (final[:60] + ("…" if len(final) > 60 else ""))))
    except Exception as e:
        if typer:
            try: typer.discard()
            except Exception: pass
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


class PopMenu:
    """תפריט כהה מימין לשמאל, שנסגר בלחיצה בכל מקום מחוץ לו, ב-Esc או במעבר חלון.
    לא גונב פוקוס מהחלון שבו כותבים (כמו הבר עצמו). פריטים:
    dict(text, cmd, mark, hint, sub=callable->items, disabled) | "sep" | ("header", text)."""

    BGC, HOV, EDGE, SEPC = "#1d1d25", "#2f2f3c", "#3b3b4a", "#30303c"
    closed_at = 0.0
    root_menu = None

    def __init__(self, root, items, at, parent=None):
        self.root, self.parent, self.child, self.child_row = root, parent, None, None
        self.sc = screen_scale()
        w = self.win = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=self.EDGE)
        inner = tk.Frame(w, bg=self.BGC)
        inner.pack(padx=1, pady=1)
        self.rows = []
        P_ = lambda v: round(v * self.sc)
        fnt, small = ("Segoe UI", 10), ("Segoe UI", 9)
        tk.Frame(inner, bg=self.BGC, height=P_(4)).pack(fill="x")
        for it in items:
            if it == "sep":
                tk.Frame(inner, bg=self.SEPC, height=1).pack(fill="x", padx=P_(10), pady=P_(4))
                continue
            if isinstance(it, tuple) and it[0] == "header":
                tk.Label(inner, text=rtl(it[1]), bg=self.BGC, fg="#7c7c8c", font=small, anchor="e"
                         ).pack(fill="x", padx=(P_(12), P_(14)), pady=(P_(4), P_(1)))
                continue
            dis = it.get("disabled")
            row = tk.Frame(inner, bg=self.BGC, cursor="arrow" if dis else "hand2")
            row.pack(fill="x", padx=P_(4))
            row.columnconfigure(1, weight=1)
            fg = "#6e6e7c" if dis else FG
            mark = tk.Label(row, text=it.get("mark", ""), bg=self.BGC, fg=ACCENT, font=fnt, width=2)
            mark.grid(row=0, column=2, sticky="e", padx=(0, P_(6)), pady=P_(5))
            txt = tk.Label(row, text=rtl(it["text"]), bg=self.BGC, fg=fg, font=fnt, anchor="e")
            txt.grid(row=0, column=1, sticky="ew", pady=P_(5))
            hint = "‹" if it.get("sub") else it.get("hint", "")
            hl = tk.Label(row, text=hint, bg=self.BGC, fg="#7c7c8c", font=small if it.get("hint") else fnt)
            hl.grid(row=0, column=0, sticky="w", padx=(P_(8), P_(14)))
            parts = (row, mark, txt, hl)
            self.rows.append((row, parts, it))
            if not dis:
                for p in parts:
                    p.bind("<Enter>", lambda e, r=row, ps=parts, i=it: self._hover(r, ps, i))
                    p.bind("<ButtonRelease-1>", lambda e, i=it: self._click(i))
        tk.Frame(inner, bg=self.BGC, height=P_(4)).pack(fill="x")
        w.update_idletasks()
        mw, mh = w.winfo_reqwidth(), w.winfo_reqheight()
        mw = max(mw, P_(230))
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        if parent is None:            # at = מלבן הבר (x0, y0, x1, y1) במסך: מעל הבר, מיושר לקצה הימני שלו
            x0, y0, x1, y1 = at
            x = int(x1 - mw)
            y = int(y0 - mh - P_(8)) if y0 - mh - P_(8) >= 0 else int(y1 + P_(8))
        else:                         # at = שורת ההורה: תת-תפריט נפתח שמאלה
            x0, y0, x1, y1 = at
            x = int(x0 - mw + P_(2)) if x0 - mw >= 0 else int(x1 - P_(2))
            y = int(y0 - P_(5))
        x = max(0, min(x, sw - mw)); y = max(0, min(y, sh - mh))
        w.geometry(f"{mw}x{mh}+{x}+{y}")
        w.update_idletasks()
        h = hwnd_of(w)
        OWN_HWNDS.add(h)
        make_noactivate(w)
        try:  # פינות מעוגלות ב-Windows 11
            v = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(h, 33, ctypes.byref(v), 4)
        except Exception:
            pass
        if parent is None:
            PopMenu.root_menu = self
            self._was_down = self._mouse_down()
            for vk in (0x1B, 1, 2, 4):                          # מנקה את "נלחץ מאז הבדיקה הקודמת"
                ctypes.windll.user32.GetAsyncKeyState(vk)
            self._fg = ctypes.windll.user32.GetForegroundWindow()
            self.win.after(40, self._watch)

    # ---- סגירה
    @staticmethod
    def _mouse_down():
        u = ctypes.windll.user32
        return any(u.GetAsyncKeyState(vk) & 0x8000 for vk in (1, 2, 4))

    def _chain(self):
        m, out = self, []
        while m:
            out.append(m); m = m.child
        return out

    def _inside(self, x, y):
        for m in self._chain():
            try:
                w = m.win
                if w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width() and w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height():
                    return True
            except tk.TclError:
                pass
        return False

    def _watch(self):
        if PopMenu.root_menu is not self:
            return
        try:
            u = ctypes.windll.user32
            st = [u.GetAsyncKeyState(vk) for vk in (1, 2, 4)]     # קריאה אחת: הקריאה מאפסת את "נלחץ מאז"
            down = any(v & 0x8000 for v in st)
            tapped = any(v & 0x0001 for v in st)                    # קליק מהיר בין שתי בדיקות
            if (down and not self._was_down) or (tapped and not down):
                pt = wintypes.POINT(); u.GetCursorPos(ctypes.byref(pt))
                if not self._inside(pt.x, pt.y):
                    self.close(); return
            self._was_down = down
            if u.GetAsyncKeyState(0x1B) & 0x8001:      # Esc (גם לחיצה מהירה בין שתי בדיקות)
                self.close(); return
            fg = u.GetForegroundWindow()
            if fg != self._fg and fg not in OWN_HWNDS:  # עברו לחלון אחר (Alt+Tab)
                self.close(); return
        except Exception as e:
            log("menu watch:", e)
        self.win.after(40, self._watch)

    def close(self):
        top = self
        while top.parent:
            top = top.parent
        for m in reversed(top._chain()):
            try:
                OWN_HWNDS.discard(hwnd_of(m.win))
                m.win.destroy()
            except Exception:
                pass
        if PopMenu.root_menu is top:
            PopMenu.root_menu = None
        PopMenu.closed_at = time.time()

    @classmethod
    def is_open(cls):
        return cls.root_menu is not None

    # ---- ריחוף ולחיצה
    def _paint(self, row, parts, on):
        for p in parts:
            try:
                p.configure(bg=self.HOV if on else self.BGC)
            except tk.TclError:
                pass

    def _hover(self, row, parts, it):
        for r, ps, _ in self.rows:
            self._paint(r, ps, r is row)
        if self.child and self.child_row is not row:
            for m in reversed(self.child._chain()):
                try: m.win.destroy()
                except Exception: pass
            self.child, self.child_row = None, None
        if it.get("sub") and self.child is None:
            try:
                sub = it["sub"]()
            except Exception as e:
                log("submenu:", e); sub = []
            if sub:
                rect = (self.win.winfo_rootx(), row.winfo_rooty(), self.win.winfo_rootx() + self.win.winfo_width(), row.winfo_rooty())
                self.child = PopMenu(self.root, sub, rect, parent=self)
                self.child_row = row

    def _click(self, it):
        if it.get("sub"):
            return
        cmd = it.get("cmd")
        self.close()
        if cmd:
            self.root.after(10, cmd)


class FlowBar:
    """הווידג'ט הצף: ריבוע קטן. לחיצה = הקלטה, לחיצה על ■ = סיום, ✕ = ביטול.
    בריחוף מופיעים ⋯ (תפריט) ו-– (הסתרה למגש). לחיצה ימנית = תפריט. גרירה מזיזה."""

    KEY = "#010203"    # רק למצב גיבוי בלי שקיפות אמיתית

    def __init__(self, root):
        self.root = root
        self.sc = screen_scale()
        root.title("Flow-HE")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.KEY)
        self._img = None
        self.c = tk.Canvas(root, width=10, height=10, bg=self.KEY, highlightthickness=0, cursor="hand2")
        self.c.pack()
        self.state, self.msg = "idle", ""
        self.hover = False
        self.grow = 0.0           # 0 = פס זעיר, 1 = גלולה מלאה (בריחוף) — מונפש
        self.hover_since = 0.0
        self.hits, self.body, self.margin, self.zone = {}, (0, 0, 0, 0), 0, (0, 0, 0, 0)
        self.card_h = 0
        self._geom = None
        self.visible = True
        self.flash_job = None
        self.result_win = None
        self.lecture_win = None
        self.meeting_win = None
        self._drag = None
        self.c.bind("<ButtonPress-1>", self._press)
        self.c.bind("<B1-Motion>", self._drag_move)
        self.c.bind("<ButtonRelease-1>", self._release)
        self.c.bind("<ButtonRelease-3>", lambda e: self._open_menu())
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        a = CFG.get("dock_anchor")
        if not (a and 0 <= a[0] < sw and 0 < a[1] <= sh):
            a = self._default_anchor()
        self.anchor = [float(a[0]), float(a[1])]
        # הגובה "הרגיל" (בלי כרטיס טקסט) — לבדיקות
        im, info = ui_art.render_widget(self.sc, "idle")
        self.H = im.size[1]
        self.W, self.cur_h = im.size
        root.geometry(f"{self.W}x{self.H}+{int(self.anchor[0] - self.W / 2)}+{int(self.anchor[1] + info['margin'] - self.H)}")
        root.deiconify()
        root.update()
        OWN_HWNDS.add(hwnd_of(root))
        if CFG.get("bar_noactivate", True):
            make_noactivate(root)
            root.after(300, lambda: make_noactivate(root))
        self.layered = False
        if CFG.get("bar_layered", True):
            try:
                import layered
                layered.enable(hwnd_of(root))
                self.layered = True
            except Exception as e:
                log("שקיפות אמיתית לא זמינה:", e)
        if not self.layered:
            self._fallback()
        self.draw()
        self._sync_visibility()
        self._tick()

    def _default_anchor(self):
        """כמו Wispr: במרכז, ממש מעל שורת המשימות."""
        try:
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
            return [(r.left + r.right) / 2, r.bottom - 6 * self.sc]
        except Exception:
            return [self.root.winfo_screenwidth() / 2, self.root.winfo_screenheight() - 56 * self.sc]

    def _tip(self):
        key = CFG.get("ptt_label") or ("Caps Lock" if PTT_VK == 0x14 else ";")
        return [("הכתבה  ", False), ("החזק " + key, True)]

    def _fallback(self):
        self.layered = False
        try:
            self.root.attributes("-alpha", 0.97, "-transparentcolor", self.KEY)
        except tk.TclError:
            pass

    # ---- ציור
    def _live(self):
        """הטקסט החי להצגה (סוף הטקסט), או ''."""
        if not CFG.get("live_preview", True) or self.state not in ("rec", "busy"):
            return ""
        ses = LIVE["session"]
        t = ses.live_text() if ses else ""
        return t[-400:] if t else ""

    def _icon(self):
        m = self.msg or ""
        if self.state != "flash":
            return "logo"
        if m.startswith(("✅", "📋", "📄")):
            return "ok"
        if m.startswith(("⚠", "✖", "🎙✖")) or "שגיאה" in m or "נכשל" in m or m.startswith("לא "):
            return "warn"
        return "logo"

    def _bubble(self):
        m = self.msg or ""
        if self.state == "flash" and not m.startswith("✅"):
            return ui_art.strip_icon(m)
        if self.state == "busy" and m.startswith("🎯"):
            return ui_art.strip_icon(m)
        return ""

    def draw(self, level=0.0, secs=0.0):
        tip = self._tip() if (self.hover and time.time() - self.hover_since > 0.35) else None
        img, info = ui_art.render_widget(self.sc, self.state, grow=self.grow, level=level,
                                         t=time.time() if self.state == "busy" else secs,
                                         live=self._live(), bubble=self._bubble(), icon=self._icon(), tip=tip)
        self.hits, self.body, self.margin, self.zone = info["hits"], info["body"], info["margin"], info["hover_zone"]
        self.card_h = info["card_h"] if info["has_card"] else 0
        w, h = img.size
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = int(round(self.anchor[0] - w / 2))
        y = int(round(self.anchor[1] + self.margin - h))
        x = max(-int(self.margin), min(x, sw - w + int(self.margin)))
        y = max(0, min(y, sh - h + int(self.margin)))
        if (w, h, x, y) != self._geom:
            self.c.config(width=w, height=h)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.root.update_idletasks()
            self._geom = (w, h, x, y)
        self.W, self.cur_h = w, h
        if self.layered:
            try:
                import layered
                if layered.update(hwnd_of(self.root), img, x, y):
                    return
            except Exception as e:
                log("UpdateLayeredWindow:", e)
            log("עובר לתצוגת גיבוי (בלי שקיפות אמיתית)")
            self._fallback()
        bg = Image.new("RGBA", img.size, (1, 2, 3, 255))
        a = img.getchannel("A").point(lambda v: 255 if v > 140 else 0)   # בלי צל במצב גיבוי
        bg.paste(img, (0, 0), a)
        self._img = ImageTk.PhotoImage(bg.convert("RGB"))
        self.c.delete("all")
        self.c.create_image(0, 0, image=self._img, anchor="nw")

    # ---- אירועי עכבר
    def _hit(self, x, y):
        order = ("cancel", "menu", "main")
        for k in order:
            b = self.hits.get(k)
            if b and b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                return k
        return None

    def _press(self, e):
        self._drag = (e.x_root, e.y_root, tuple(self.anchor), self._hit(e.x, e.y), False)

    def _drag_move(self, e):
        if not self._drag: return
        x0, y0, a0, hit, moved = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if moved or abs(dx) > 4 or abs(dy) > 4:
            self._drag = (x0, y0, a0, hit, True)
            self.anchor = [a0[0] + dx, a0[1] + dy]
            w, h = self.W, self.cur_h
            x, y = int(round(self.anchor[0] - w / 2)), int(round(self.anchor[1] + self.margin - h))
            self.root.geometry(f"+{x}+{y}")
            self._geom = (w, h, x, y)

    def _release(self, e):
        if not self._drag: return
        x0, y0, a0, hit, moved = self._drag
        self._drag = None
        if moved:
            CFG["dock_anchor"] = [round(self.anchor[0]), round(self.anchor[1])]; save_config(); self.draw(); return
        if time.time() - PopMenu.closed_at < 0.35:   # הלחיצה הזאת רק סגרה את התפריט
            return
        if hit == "main":
            if self.state in ("idle", "flash"):
                begin("dictate")
            elif self.state == "rec":
                finish()
        elif hit == "cancel" and self.state == "rec":
            cancel()
        elif hit == "menu":
            self._open_menu()

    def _reset_pos(self):
        CFG.pop("dock_anchor", None); save_config()
        self.anchor = self._default_anchor(); self.draw()

    def _body_rect(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        b = self.body
        return (x + b[0], y + b[1], x + b[2], y + b[3])

    def _open_menu(self):
        if PopMenu.is_open():
            PopMenu.root_menu.close(); return
        if time.time() - PopMenu.closed_at < 0.35:
            return
        try:
            PopMenu(self.root, self._menu_items(), self._body_rect())
        except Exception as ex:
            log("menu:", ex)

    def _menu_items(self):
        dot = lambda on: "●" if on else ""
        auto = OUTPUT_TARGET == "paste" and not PINNED["hwnd"]
        locked = OUTPUT_TARGET == "paste" and bool(PINNED["hwnd"])

        def windows():
            out = []
            try:
                for h, t in list_windows()[:20]:
                    out.append(dict(text=short(t, 42), mark=dot(h == PINNED["hwnd"]), cmd=lambda h=h, t=t: self._pin(h, t)))
            except Exception as ex:
                log("list_windows:", ex)
            return out or [dict(text="אין חלונות פתוחים", disabled=True)]

        def mics():
            cur = CFG.get("mic_device")
            out = [dict(text="ברירת המחדל של Windows", mark=dot(not cur), cmd=lambda: self._set_mic(None))]
            for name in list_mics():
                out.append(dict(text=short(name, 40), mark=dot(name == cur), cmd=lambda n=name: self._set_mic(n)))
            return out

        items = [("header", "לאן הטקסט נכנס"),
                 dict(text="איפה שהסמן", mark=dot(auto), cmd=self._auto),
                 dict(text="לחלון שאלחץ עליו…", cmd=self._pick_by_click),
                 dict(text="לחלון מהרשימה" + (f"  ({short(PINNED['title'], 16)})" if locked else ""), mark=dot(locked), sub=windows),
                 dict(text="רק להעתיק ללוח", mark=dot(OUTPUT_TARGET == "clipboard"), cmd=lambda: self._set_target("clipboard")),
                 dict(text="לחלון התוצאות", mark=dot(OUTPUT_TARGET == "window"), cmd=lambda: self._set_target("window")),
                 "sep"]
        if Meeting.active:
            items.append(dict(text=f"סיים את ה{Meeting.active.title}", mark="■", cmd=lambda: ui_q.put(("meeting", None))))
        else:
            items.append(dict(text="הקלטת פגישה / שיעור", mark="●", cmd=lambda: ui_q.put(("meeting", None))))
        items.append(dict(text="סכם הקלטה שמורה…", mark="✨", cmd=summarize_saved))
        items += ["sep",
                  dict(text="הדבק שוב את האחרון", hint="Alt+Shift+Z", cmd=lambda: threading.Thread(target=paste_last, daemon=True).start())]
        nf = len(failed_files())
        if nf:
            items.append(dict(text=f"נסה שוב הקלטות שנכשלו ({nf})", cmd=lambda: threading.Thread(target=retry_failed, daemon=True).start()))
        cur = CFG.get("mic_device")
        items += [dict(text="מיקרופון" + (f"  ({short(cur, 16)})" if cur else ""), sub=mics),
                  dict(text="הפעל עם Windows", mark="✓" if CFG.get("launch_at_login", True) else "", cmd=self._toggle_login),
                  dict(text="היסטוריה", cmd=lambda: self.show_result(None)),
                  dict(text="הסתר את הכפתור (נשאר במגש)", cmd=self.minimize),
                  dict(text="החזר את הכפתור למרכז", cmd=self._reset_pos),
                  dict(text="תיקיית ההגדרות", cmd=lambda: os.startfile(HERE)),
                  "sep"]
        v = available_update()
        if v:
            items.append(dict(text=f"עדכן לגרסה {v}", mark="⬆", cmd=lambda: check_for_update(force=True)))
        items += [dict(text="יציאה", cmd=lambda: ui_q.put(("quit", None))),
                  ("header", f"Flow-HE · גרסה {VERSION}")]
        return items

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
            ms = 4000 if self._icon() == "warn" else (3000 if self._bubble() else 1600)
            self.flash_job = self.root.after(ms, lambda: self.set_state("idle"))

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
        hv = self._cursor_near() and self.state in ("idle", "flash") and not PopMenu.is_open()
        hv = hv or (PopMenu.is_open() and self.state == "idle")
        if hv and not self.hover:
            self.hover_since = time.time()
        tip_due = hv and not getattr(self, "_tip_shown", False) and time.time() - self.hover_since > 0.35
        self.hover = hv
        target = 1.0 if (hv and self.state == "idle") else 0.0
        moving = abs(self.grow - target) > 1e-3
        if moving:
            step = 0.22 if target > self.grow else 0.16
            self.grow = min(target, self.grow + step) if target > self.grow else max(target, self.grow - step)
        if self.state == "rec":
            self.draw(rec.level, time.time() - rec.t0)
        elif self.state == "busy":
            self.draw()  # הנקודות פועמות; הטקסט החי מתעדכן אם השתנה
        elif (moving or tip_due or (not hv and getattr(self, "_tip_shown", False))) and not self._drag:
            self.draw()
        self._tip_shown = hv and time.time() - self.hover_since > 0.35
        self.root.after(30 if moving else 45, self._tick)

    def _cursor_near(self):
        """העכבר מעל הריבוע (או קרוב אליו) — מציגים את כפתורי התפריט וההסתרה."""
        if not self.visible:
            return False
        try:
            pt = wintypes.POINT(); ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x - self.root.winfo_x(), pt.y - self.root.winfo_y()
            z = self.zone
            return z[0] <= x <= z[2] and z[1] <= y <= z[3]
        except Exception:
            return False

    # ---- חלון תוצאות / היסטוריה
    def show_result(self, text):
        if self.result_win is None or not self.result_win.winfo_exists():
            self.result_win = ResultWindow(self.root)
        self.result_win.show(text)


class _Source:
    """מקור קול אחד בפגישה (המיקרופון = "אני", אודיו המחשב = "משתתפים"): חיתוך בהפסקות, חפיפה, זמן לפי שעון קיר."""

    def __init__(self, name, path, t0):
        self.name, self.t0 = name, t0
        self.q = queue.Queue()
        self.chunker = Chunker(SAMPLE_RATE, min_s=float(CFG.get("meeting_min_s", 1.5)), max_s=float(CFG.get("meeting_max_s", 6.0)))
        self.tail = np.zeros(0, dtype="float32")
        self.pos = 0          # כמה דגימות נכנסו לחותך (= זמן מתחילת הפגישה)
        # תיקון תוך כדי: כל ~75-105 שניות (בהפסקה) חתיכה גדולה נשלחת שוב לשרת ומחליפה את הטקסט הזמני
        rs = float(CFG.get("meeting_refine_s", 75.0))
        self.rchunker = Chunker(SAMPLE_RATE, min_s=rs, max_s=rs + 30)
        self.rpos = 0
        self.rtail = np.zeros(0, dtype="float32")
        self.refined_until = 0.0   # עד איזו שנייה הטקסט כבר מדויק (מכאן והלאה — זמני, באפור)
        self.fed = 0          # כמה דגימות התקבלו בפועל (לסנכרון מול שעון הקיר)
        self.path = path
        try:
            self.snd = sf.SoundFile(path, "w", samplerate=SAMPLE_RATE, channels=1, format="FLAC")
        except Exception as e:
            log("פגישה: לא נשמר אודיו", name, e); self.snd = None
        self.ok = True

    def put(self, x):
        """loopback של Windows לא שולח כלום בזמן שקט — משלימים אפסים לפי שעון הקיר כדי שהזמנים יישארו נכונים."""
        x = np.asarray(x, dtype="float32").reshape(-1)
        behind = int((time.time() - self.t0) * SAMPLE_RATE) - (self.fed + len(x))
        if behind > SAMPLE_RATE // 2:
            self.q.put(np.zeros(behind, dtype="float32")); self.fed += behind
        self.q.put(x); self.fed += len(x)


class Meeting:
    """מצב פגישה (כמו ה-Notetaker של Wispr): מקליט את המיקרופון ("אני") ואת מה שהמחשב משמיע ("משתתפים"),
    מתמלל תוך כדי עם חפיפה של 2 שניות, מציג בחלון גדול מימין לשמאל, ובסוף עובר שוב על הכול ושומר Word."""

    active = None

    KINDS = {"meeting": ("meetings", "הקלטה"), "lecture": ("lectures", "שיעור")}

    def __init__(self, kind="meeting"):
        self.kind = kind if kind in self.KINDS else "meeting"
        folder, self.title = self.KINDS[self.kind]
        os.makedirs(P(folder), exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.base = P(f"{folder}/{self.title}_{stamp}")
        self.t0 = time.time()
        self.on = True
        self.pool = ThreadPoolExecutor(max_workers=int(CFG.get("parallel_requests", 3)))
        self.lock = threading.Lock()
        self.entries = []        # (start_s, speaker, text)
        self.version, self.inflight = 0, 0
        self.final_state = ""
        self.docx = None
        self.rpool = ThreadPoolExecutor(max_workers=1)   # חתיכות התיקון — אחת אחרי השנייה, לפי הסדר
        self.refine_on = CFG.get("meeting_refine", True) and (server_has_jobs() or bool(os.environ.get("FLOWHE_FAKE_TRANSCRIPT")))
        self.refine_used = self.refine_on   # אם יש תיקון תוך כדי — הוא גם הגרסה הסופית (בלי מעבר נוסף על הכול)
        self.refining = 0
        self.spans = []          # (start, end, name, source) של הטקסט המתוקן — לקישור דוברים בין חתיכות
        self.names = {}          # מזהה דובר -> שם לתצוגה ("דובר 1", או שם שהמשתמש נתן)
        self.num_speakers = int(CFG.get("meeting_speakers", 2))  # ברירת מחדל 2 (אוטומטי של ivrit תמיד בוחר את המקסימום); -1 = אוטומטי
        self.mdir = speakers.models_dir(BUNDLE, HERE) if CFG.get("meeting_diarize", True) else None
        self.online = None
        self.server_diarize = server_has_jobs() and CFG.get("meeting_server_diarize", True) \
            and not os.environ.get("FLOWHE_FAKE_TRANSCRIPT")
        if self.server_diarize:
            self.mdir = None  # הכול בענן: בלי חישובי קול על המחשב. בזמן אמת — "אני" / "משתתפים" לפי המקור, כמו Wispr
        if self.mdir:
            try:
                self.online = speakers.OnlineSpeakers(self.mdir, float(CFG.get("speaker_threshold", 0.38)), threads=1)
            except Exception as e:
                log("פגישה: זיהוי דוברים לא זמין:", e); self.mdir = None
        log("פגישה: זיהוי דוברים:", "בשרת (בסוף הפגישה)" if self.server_diarize else ("על המחשב" if self.online else "רק לפי מקור"))
        self.sources = [_Source("אני", self.base + "_אני.flac", self.t0)]
        self.mic_stream = None
        fake = os.environ.get("FLOWHE_FAKE_AUDIO")
        if CFG.get("meeting_system_audio", True):
            self.sources.append(_Source("משתתפים", self.base + "_משתתפים.flac", self.t0))
        self.last_speech = time.time()
        self.runs_left = len(self.sources)   # כמה זרמים עוד לא סיימו (כולל שליחת הזנב לתיקון)
        for src in self.sources:
            threading.Thread(target=self._run, args=(src,), daemon=True).start()
        if fake and fake.startswith("file:"):  # בדיקות: הקלטה אמיתית של כמה דוברים, מוזנת פי 4 ממהירות אמת
            threading.Thread(target=self._fake_file, args=(fake[5:],), daemon=True).start()
        elif fake:
            for k, src in enumerate(self.sources):
                threading.Thread(target=self._fake, args=(src, k), daemon=True).start()
        else:
            self.mic_stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                             callback=lambda x, *a: self.sources[0].put(x.copy()) if self.on else None,
                                             device=mic_device_index())
            self.mic_stream.start()
            if len(self.sources) > 1:
                threading.Thread(target=self._loopback, args=(self.sources[1],), daemon=True).start()
        Meeting.active = self
        warm_server()
        threading.Thread(target=self._keep_warm, daemon=True).start()
        log("פגישה: התחילה ->", self.base)

    def _keep_warm(self):
        """לאורך כל הפגישה השרת לא נרדם (גם בשתיקות) — כדי שהטקסט החי יגיע מהר. אחרי הפגישה הוא נרדם כרגיל."""
        while self.on:
            time.sleep(5)
            if time.time() - self.last_speech > float(CFG.get("keep_warm_idle_s", 300)):
                continue   # 5 דקות בלי דיבור (שכחת לסיים?) — לא מעירים; השרת נרדם ולא עולה כסף
            warm_server()

    def _fake(self, src, k):
        t, block = 0.0, 0.1
        while self.on:
            n = int(block * SAMPLE_RATE); tt = np.arange(n) / SAMPLE_RATE + t
            speaking = ((tt + k * 3.0) % 7.0) < 3.0      # שני הדוברים מדברים לסירוגין
            src.put((0.2 * np.sin(2 * np.pi * (220 + 110 * k) * tt) * speaking).astype("float32"))
            t += block; time.sleep(block)

    def _fake_file(self, path):
        x, _ = sf.read(path, dtype="float32")
        x = x.reshape(-1) if x.ndim == 1 else x.mean(axis=1)
        n = SAMPLE_RATE // 10
        for i in range(0, len(x), n):
            if not self.on:
                return
            self.sources[0].q.put(x[i:i + n]); self.sources[0].fed += len(x[i:i + n])
            time.sleep(0.025)
        self.file_done = True

    def _loopback(self, src):
        """האודיו שהמחשב משמיע (זום, Meet, דפדפן) דרך WASAPI loopback של הרמקול ברירת המחדל."""
        try:
            import soundcard as sc
            spk = sc.default_speaker()
            dev = sc.get_microphone(id=str(spk.name), include_loopback=True)
            with dev.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=1024) as r:
                log("פגישה: מקליט גם את אודיו המחשב:", spk.name)
                while self.on:
                    x = r.record(numframes=SAMPLE_RATE // 10)
                    if len(x):
                        src.put(np.asarray(x, dtype="float32").mean(axis=1) if np.ndim(x) > 1 else x)
        except Exception as e:
            src.ok = False
            log("פגישה: אין הקלטת אודיו מחשב (רק מיקרופון):", e)
            ui_q.put(("flash", "פגישה: מקליט רק מהמיקרופון"))

    def _run(self, src):
        ov = int(float(CFG.get("overlap_s", 2.0)) * SAMPLE_RATE)
        while True:
            x = src.q.get()
            if x is None:
                break
            if src.snd:
                try: src.snd.write(x)
                except Exception: pass
            for seg in src.chunker.feed(x):
                self._submit(src, seg, ov)
            if self.refine_on:
                for big in src.rchunker.feed(x):
                    self._refine(src, big)
        self._submit(src, src.chunker.flush(), ov)
        if self.refine_used:   # הזנב שעוד לא תוקן — חתיכה אחרונה, באותה שיטה (עם חפיפה לזיהוי הדוברים)
            tail = src.rchunker.flush()
            if len(tail):
                self._refine(src, tail)
        if src.snd:
            src.snd.close()
        with self.lock:
            self.runs_left -= 1

    def _submit(self, src, seg, ov):
        start = src.pos / SAMPLE_RATE
        src.pos += len(seg)
        lead = src.tail
        src.tail = seg[-ov:] if ov else src.tail
        if not len(seg) or is_silent(seg):
            return
        self.last_speech = time.time()
        audio = np.concatenate([lead, seg]) if len(lead) else seg
        with self.lock:
            self.inflight += 1
        self.pool.submit(self._job, src.name, start, audio, seg)

    def _nspk_for(self, src):
        """מספר הדוברים שבחרת בחלון חל על המיקרופון (האנשים בחדר). אודיו המחשב — לא ידוע."""
        n = self.num_speakers
        return n if (n and n > 1 and src is self.sources[0]) else None

    def label(self, k):
        with self.lock:
            return self.names.setdefault(k, f"דובר {len(self.names) + 1}")

    def rename(self, old, new):
        with self.lock:
            for k, v in list(self.names.items()):
                if v == old:
                    self.names[k] = new
            self.entries = [(a, new if b == old else b, c, d) for a, b, c, d in self.entries]
            self.version += 1
        log("פגישה: דובר שונה שם:", old, "->", new)

    def _job(self, src_name, start, audio, seg):
        spk = src_name
        try:
            if self.online:
                k = self.online.assign(seg)
                if k is not None:
                    spk = self.label(k)
                else:
                    prev = [e for e in self.entries if e[3] == src_name]
                    spk = prev[-1][1] if prev else src_name
            t = apply_dictionary(textproc.tidy(textproc.clean_chunk(transcribe_with_retry(normalize(audio)))))
        except Exception as e:
            log("פגישה: קטע נכשל:", e); t = ""
        src = next((x for x in self.sources if x.name == src_name), None)
        if src is not None and start < src.refined_until:
            t = ""  # החלק הזה כבר תוקן בחתיכה הגדולה — לא מחזירים טקסט זמני
        with self.lock:
            self.inflight -= 1
            if t:
                prev = [e for e in self.entries if e[3] == src_name]
                if prev:
                    t = textproc.merge_overlap(prev[-1][2], t)
                if t:
                    self.entries.append((start, spk, t, src_name))
                    self.entries.sort(key=lambda e: e[0])
            self.version += 1

    def _refine(self, src, big):
        start = src.rpos / SAMPLE_RATE
        src.rpos += len(big)
        lead = src.rtail
        src.rtail = big[-int(15 * SAMPLE_RATE):]
        end = start + len(big) / SAMPLE_RATE
        if is_silent(big):
            src.refined_until = max(src.refined_until, end); return
        with self.lock:
            self.refining += 1
        self.rpool.submit(self._refine_job, src, start, end, lead, big)

    def _refine_job(self, src, start, end, lead, big):
        """חתיכה גדולה (עם 15 שניות חפיפה מהקודמת): תמלול מדויק + דוברים מהשרת, מחליף את הטקסט הזמני בטווח.
        החפיפה מאפשרת לקשר את הדוברים של החתיכה לשמות שכבר ניתנו (לפי הזמן שבו הם מדברים בחפיפה)."""
        try:
            audio = np.concatenate([lead, big]) if len(lead) else big
            off = start - len(lead) / SAMPLE_RATE
            try:
                res = server_request(normalize(audio), diarize=self.server_diarize, num_speakers=self._nspk_for(src))
                diarized = self.server_diarize
            except RuntimeError as e:
                if "iariz" not in str(e):
                    raise
                # רק משפטים קצרים מאוד בחתיכה — מנוע הדוברים מסרב; מתקנים את הטקסט בלי חלוקה (השמות לפי הזמן)
                log(f"{self.title}: חלוקה לדוברים נכשלה בחתיכה, מתקן טקסט בלבד:", str(e)[:80])
                res = server_request(normalize(audio)); diarized = False
            segs = [(off + float(g.get("start") or 0), off + float(g.get("end") or 0), g.get("speaker"),
                     textproc.tidy(textproc.clean_chunk(g.get("text") or ""))) for g in res["segments"]]
            with self.lock:
                old = [e for e in self.entries if e[3] == src.name]
                spans = [sp for sp in self.spans if sp[3] == src.name]
            # שם לכל דובר מקומי: לפי כמה זמן הוא חופף, בקטע המשותף, לדובר שכבר קיבל שם בחתיכה הקודמת
            # (נבדק על פגישה אמיתית: התאמה לפי זמן חפיפה שמרה את 2 הדוברים עקביים; לפי "התחלה קרובה" — לא)
            names, ov = {}, {}
            if diarized:
                for a, b, spk, t in segs:
                    if spk is None:
                        continue
                    for a2, b2, name, _ in spans:
                        o = min(b, b2) - max(a, a2)
                        if o > 0:
                            ov.setdefault(spk, {}).setdefault(name, 0.0)
                            ov[spk][name] += o
                for spk, m in sorted(ov.items(), key=lambda kv: -max(kv[1].values())):
                    best = max(m, key=m.get)
                    if best not in names.values():
                        names[spk] = best
            live_default = next((e[1] for e in reversed(old)), src.name)
            new, new_spans = [], []
            for a, b, spk, t in segs:
                if not t or a < start - 0.3:
                    continue
                if spk is None or not diarized:
                    near = [e for e in old if abs(e[0] - a) < 4]
                    name = near[0][1] if near else live_default
                else:
                    if spk not in names:
                        names[spk] = self.label(("refine", src.name, start, spk))
                    name = names[spk]
                new_spans.append((a, b, name, src.name))
                if new and new[-1][1] == name and len(new[-1][2]) < 500:
                    new[-1] = (new[-1][0], name, new[-1][2] + " " + apply_dictionary(t), src.name)
                else:
                    new.append((a, name, apply_dictionary(t), src.name))
            with self.lock:
                self.entries = [e for e in self.entries if not (e[3] == src.name and start - 0.5 <= e[0] < end)] + new
                self.spans = [sp for sp in self.spans if not (sp[3] == src.name and sp[0] >= start - 0.5)] + new_spans
                self.entries.sort(key=lambda e: e[0])
                src.refined_until = max(src.refined_until, end)
                self.version += 1
            log(f"{self.title}: תוקן {start:.0f}-{end:.0f}s ({len(new)} קטעים)")
        except Exception as e:
            log(f"{self.title}: תיקון תוך כדי נכשל (הטקסט הזמני נשאר):", e)
        finally:
            with self.lock:
                self.refining -= 1

    def stop(self):
        if not self.on:
            return
        self.on = False
        self.refine_on = False
        if self.mic_stream:
            try: self.mic_stream.stop(); self.mic_stream.close()
            except Exception: pass
        for src in self.sources:
            src.q.put(None)
        Meeting.active = None
        log(f"פגישה: נעצרה אחרי {time.time() - self.t0:.0f}s")
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        time.sleep(1.0)
        end = time.time() + 600
        while (self.inflight or self.refining) and time.time() < end:
            time.sleep(0.3)
        if self.refine_used:
            while (self.runs_left or self.refining) and time.time() < end:   # הזנב נשלח רק אחרי שהזרם נסגר
                time.sleep(0.3)
            with self.lock:
                out = dedupe_echo(sorted(self.entries, key=lambda e: e[0]))
                self.entries = out
                for src in self.sources:
                    src.refined_until = float("inf")
                self.final_state = "done"; self.version += 1
            log(f"{self.title}: הגרסה הסופית = החתיכות המתוקנות ({len({e[1] for e in out})} דוברים)")
            return self._save()
        if CFG.get("meeting_final_pass", True):
            try:
                self._final_pass()
            except Exception as e:
                log("פגישה: מעבר שני נכשל — נשאר התמלול החי:", e)
        self._save()

    def _final_pass(self):
        """מעבר שני: כל מקור בחלונות ארוכים עם חפיפה, הסרת "הד" (מה שהמיקרופון קלט מהרמקולים), ומיזוג לפי זמן."""
        if self.server_diarize:
            try:
                return self._final_pass_server()
            except Exception as e:
                log("פגישה: חלוקה לדוברים בשרת נכשלה, מעבר שני רגיל:", e)
        if self.mdir:
            try:
                return self._final_pass_diarized()
            except Exception as e:
                log("פגישה: חלוקה לדוברים נכשלה, מעבר שני רגיל:", e)
        win_max = float(CFG.get("final_window_s", 28.0))
        ov = int(float(CFG.get("overlap_s", 2.0)) * SAMPLE_RATE)
        jobs = []
        for src in self.sources:
            if not src.snd or not os.path.exists(src.path):
                continue
            ch = Chunker(SAMPLE_RATE, min_s=win_max * 0.4, max_s=win_max)
            pos, tail = 0, np.zeros(0, dtype="float32")
            for block in sf.blocks(src.path, blocksize=SAMPLE_RATE * 10, dtype="float32"):
                for g in ch.feed(block.reshape(-1)):
                    jobs.append((src.name, pos / SAMPLE_RATE, tail, g)); pos += len(g); tail = g[-ov:]
            g = ch.flush()
            if len(g):
                jobs.append((src.name, pos / SAMPLE_RATE, tail, g))
        jobs = [j for j in jobs if not is_silent(j[3])]
        if not jobs:
            return
        total, done = len(jobs), {"n": 0}
        self.final_state = f"0/{total}"; self.version += 1

        def run(j):
            spk, start, tail, g = j
            r = textproc.tidy(textproc.clean_chunk(transcribe_with_retry(normalize(np.concatenate([tail, g]) if len(tail) else g))))
            done["n"] += 1; self.final_state = f"{done['n']}/{total}"; self.version += 1
            return (start, spk, r)
        res = list(self.pool.map(run, jobs))
        out, last = [], {}
        for start, spk, t in sorted(res, key=lambda e: e[0]):
            if not t:
                continue
            if spk in last:
                t = textproc.merge_overlap(last[spk], t)
            if t:
                last[spk] = t
                out.append((start, spk, apply_dictionary(t), spk))
        out = dedupe_echo(out)
        if AI_EDITS and CFG.get("meeting_fix", False):
            out = [(a, b, ai_clean(c), d) for a, b, c, d in out]
        with self.lock:
            self.entries = out
            self.final_state = "done"; self.version += 1
        log(f"פגישה: מעבר שני הסתיים ({total} חלונות)")

    def _final_pass_server(self):
        """הכול בשרת: כל מקור נשלח בחלקים של עד ~18 דקות עם diarize=True (pyannote על ה-GPU של RunPod),
        והשרת מחזיר מי אמר מה ומתי. המחשב רק מקודד את האודיו ושולח."""
        jobs = []
        for src in self.sources:
            if not src.snd or not os.path.exists(src.path):
                continue
            for k, (off, g) in enumerate(long_parts(src.path)):
                jobs.append((src, k, off, g))
        if not jobs:
            return
        total, done = len(jobs), {"n": 0}
        self.final_state = f"0/{total}"; self.version += 1
        def run(j):
            src, k, off, g = j
            res = server_request(normalize(g), diarize=True, num_speakers=self._nspk_for(src))
            done["n"] += 1; self.final_state = f"{done['n']}/{total}"; self.version += 1
            return src, k, off, res["segments"]
        results = list(self.pool.map(run, jobs))
        # שמות: מקור עם דובר יחיד שומר את השם החי (גם אם שינית אותו); אחרת "דובר N" לפי סדר ההופעה
        per_src = {}
        for src, k, off, segs in results:
            per_src.setdefault(src.name, set()).update((k, s.get("speaker")) for s in segs if s.get("text"))
        with self.lock:
            before = list(self.entries)
        live_name = {src.name: next((e[1] for e in before if e[3] == src.name), src.name) for src in self.sources}
        out, votes = [], {}
        for src, k, off, segs in results:
            single = len({spk for _, spk in per_src.get(src.name, set())}) <= 1
            for sgm in segs:
                t = textproc.tidy(textproc.clean_chunk(sgm.get("text") or ""))
                if not t:
                    continue
                a = off + float(sgm.get("start") or 0)
                key = (src.name, k, sgm.get("speaker"))
                name = live_name[src.name] if single else None
                if name is None:  # השם שהדובר קיבל במהלך ההקלטה (כולל שם ששינית) — לפי מי דיבר באותו זמן
                    near = [e for e in before if e[3] == src.name and abs(e[0] - a) < 3]
                    if near:
                        votes.setdefault(key, []).append(near[0][1])
                out.append([a, name, apply_dictionary(t), src.name, key])
        used = set()
        for key, v in sorted(votes.items(), key=lambda kv: -len(kv[1])):
            best = max(set(v), key=v.count)
            if best not in used:
                used.add(best)
                for e in out:
                    if e[4] == key and e[1] is None:
                        e[1] = best
        out.sort(key=lambda e: e[0])
        names, n = {}, 1
        taken = {e[1] for e in out if e[1]}
        for e in out:
            if e[1] is None:
                if e[4] not in names:
                    while f"דובר {n}" in taken:
                        n += 1
                    names[e[4]] = f"דובר {n}"; n += 1
                e[1] = names[e[4]]
        # איחוד משפטים רצופים של אותו דובר לפסקה אחת
        merged = []
        for a, name, t, srcn, _ in out:
            if merged and merged[-1][1] == name and a - merged[-1][0] < 600 and len(merged[-1][2]) < 600:
                merged[-1] = (merged[-1][0], name, merged[-1][2] + " " + t, srcn)
            else:
                merged.append((a, name, t, srcn))
        merged = dedupe_echo(merged)
        with self.lock:
            self.entries = merged
            for src in self.sources:
                src.refined_until = float("inf")
            self.final_state = "done"; self.version += 1
        log(f"פגישה: חלוקה לדוברים בשרת הסתיימה ({total} חלקים, {len({e[1] for e in merged})} דוברים)")

    def _final_pass_diarized(self):
        _set_priority(True)
        try:
            return self._final_pass_diarized_inner()
        finally:
            _set_priority(False)

    def _final_pass_diarized_inner(self):
        """מעבר שני עם חלוקה לדוברים לפי קול: לכל מקור — מי דיבר מתי (pyannote + 3D-Speaker),
        איחוד לתורות דיבור, ותמלול כל תורה בנפרד. שמות מהשלב החי נשמרים (התאמת טביעת קול)."""
        pad = int(0.25 * SAMPLE_RATE)
        jobs = []
        for src in self.sources:
            if not src.snd or not os.path.exists(src.path):
                continue
            x, _ = sf.read(src.path, dtype="float32")
            x = x.reshape(-1)
            if not len(x) or is_silent(x):
                continue
            segs = speakers.diarize(self.mdir, x, num_speakers=self.num_speakers)
            log(f"פגישה: {src.name}: {len(segs)} קטעים, {len({k for _, _, k in segs})} דוברים")
            # שם לכל אשכול: לפי הדובר החי הכי דומה, אחרת דובר חדש
            names = {}
            for k in sorted({k for _, _, k in segs}):
                parts = [x[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)] for a, b, kk in segs if kk == k]
                sample = np.concatenate(parts)[: SAMPLE_RATE * 20]
                live = self.online.match(self.online.embed(sample)) if self.online and len(sample) > SAMPLE_RATE else None
                names[k] = self.label(live) if live is not None else self.label(("final", src.name, k))
            for a, b, k in speakers.turns(segs, max_s=float(CFG.get("final_window_s", 28.0))):
                g = x[max(0, int(a * SAMPLE_RATE) - pad): int(b * SAMPLE_RATE) + pad]
                if len(g) > SAMPLE_RATE * 0.4 and not is_silent(g):
                    jobs.append((a, names[k], g, src.name))
        if not jobs:
            return
        total, done = len(jobs), {"n": 0}
        self.final_state = f"0/{total}"; self.version += 1

        def run(j):
            a, name, g, srcn = j
            try:
                r = textproc.tidy(textproc.clean_chunk(transcribe_with_retry(normalize(g))))
            except Exception as e:
                log("פגישה: תורה נכשלה:", e); r = ""
            done["n"] += 1; self.final_state = f"{done['n']}/{total}"; self.version += 1
            return (a, name, apply_dictionary(r) if r else "", srcn)
        out = [e for e in self.pool.map(run, jobs) if e[2]]
        out = dedupe_echo(sorted(out, key=lambda e: e[0]))
        # מספור מחדש לפי סדר ההופעה ("דובר 1, 2, 3" בלי חורים); שמות שהמשתמש נתן נשארים
        default = re.compile(r"^דובר \d+$")
        order = []
        for e in out:
            if default.match(e[1]) and e[1] not in order:
                order.append(e[1])
        taken = {e[1] for e in out if not default.match(e[1])}
        ren, n = {}, 1
        for old in order:
            while f"דובר {n}" in taken:
                n += 1
            ren[old] = f"דובר {n}"; n += 1
        out = [(a, ren.get(b, b), c, d) for a, b, c, d in out]
        with self.lock:
            for k, v in list(self.names.items()):
                if v in ren:
                    self.names[k] = ren[v]
        with self.lock:
            self.entries = out
            self.final_state = "done"; self.version += 1
        log(f"פגישה: מעבר שני עם דוברים הסתיים ({total} תורות)")

    def _save(self):
        with self.lock:
            entries = list(self.entries)
        dur = time.time() - self.t0
        spk_names = sorted({e[1] for e in entries})
        try:
            with open(self.base + ".md", "w", encoding="utf-8") as f:
                f.write(f"# {self.title} — {datetime.fromtimestamp(self.t0):%d/%m/%Y %H:%M}\n\n")
                for start, spk, t, _src in entries:
                    f.write(f"**{spk}** ({meeting_doc.fmt_time(start)}): {t}\n\n")
        except Exception as e:
            log("פגישה: md:", e)
        try:
            self.docx = meeting_doc.write_docx(
                self.base + ".docx", f"{self.title} — {datetime.fromtimestamp(self.t0):%d/%m/%Y %H:%M}",
                f"משך {meeting_doc.fmt_time(dur)} · דוברים: {', '.join(spk_names) or '—'}", [e[:3] for e in entries],
                show_speakers=len(spk_names) > 1)
            log("פגישה: נשמר Word ->", self.docx)
        except Exception as e:
            log("פגישה: Word נכשל:", e)
        with self.lock:
            self.final_state = "saved"; self.version += 1
        ui_q.put(("flash", "📄 נשמר כקובץ וורד"))


def _set_priority(low: bool):
    """עבודה כבדה מקומית (חלוקה לדוברים) בעדיפות נמוכה — שהמחשב לא ירגיש תקוע. הת'רדים של onnxruntime
    לא יורשים עדיפות של ת'רד, לכן משנים את מחלקת העדיפות של התהליך לזמן החישוב ומחזירים אחר כך."""
    try:
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000 if low else 0x20)  # BELOW_NORMAL / NORMAL
    except Exception:
        pass


def dedupe_echo(entries, window_s=6.0):
    """כשהרמקולים פתוחים, המיקרופון קולט גם את המשתתפים. קטע של "אני" שרוב המילים שלו כבר הופיעו
    באותו זמן אצל "משתתפים" — נזרק."""
    others = [(s, set(textproc._norm_word(textproc._split(w)[1]) for w in t.split())) for s, k, t, src in entries if src != "אני"]
    out = []
    for s, k, t, src in entries:
        if src == "אני" and others:
            words = [textproc._norm_word(textproc._split(w)[1]) for w in t.split()]
            near = set().union(*[ws for (so, ws) in others if abs(so - s) <= window_s] or [set()])
            if words and sum(w in near for w in words) / len(words) > 0.6:
                continue
        out.append((s, k, t, src))
    return out


def toggle_meeting(kind="meeting"):
    """פגישה ושיעור — אותו מנגנון (שני מקורות, דוברים, תיקון תוך כדי, Word). רק השם והתיקייה שונים."""
    if Meeting.active:
        Meeting.active.stop()
    else:
        try:
            Meeting(kind)
            beep("start")
            ui_q.put(("meeting_win", None))
        except Exception as e:
            log("הקלטה רצופה:", e); beep("error")
            ui_q.put(("flash", f"שגיאה בהקלטה: {e}"))


class SummaryWindow:
    """סיכום עם Claude: בוחרים סוג שיחה + רמת פירוט, מקבלים סיכום מסודר שנשמר גם ל-Word."""

    CARD, CARD_SEL = "#22222b", "#2b2740"

    def __init__(self, root, transcript_fn, title, base_path, default_kind="work"):
        sc = screen_scale()
        self.win = tk.Toplevel(root)
        self.win.title(f"Flow-HE — סיכום: {title}")
        self.win.configure(bg=BG)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h = min(int(640 * sc), sw - 40), min(int(720 * sc), sh - 80)
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        self.win.update_idletasks()
        OWN_HWNDS.add(hwnd_of(self.win))
        self.transcript_fn, self.title, self.base = transcript_fn, title, base_path
        self.kind, self.length = default_kind, CFG.get("summary_length", "short")
        self.result, self.md, self.path = None, "", None
        self.body = tk.Frame(self.win, bg=BG)
        self.body.pack(fill="both", expand=True, padx=22, pady=18)
        self._choose()
        self.win.lift(); self.win.focus_force()

    def _clear(self):
        for c in self.body.winfo_children():
            c.destroy()

    def _lbl(self, parent, text, size=11, bold=False, fg=FG, bg=BG, **pack):
        l = tk.Label(parent, text=rtl(text), bg=bg, fg=fg, font=("Segoe UI", size, "bold" if bold else "normal"),
                     anchor="e", justify="right")
        l.pack(fill="x", **pack)
        return l

    def _btn(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2",
                         bg=ACCENT if primary else "#2a2a34", fg="white" if primary else FG,
                         activebackground="#7465e6" if primary else "#3a3a48", activeforeground="white",
                         font=("Segoe UI", 11, "bold" if primary else "normal"), padx=18, pady=6)

    # ---- שלב 1: מה לסכם
    def _choose(self):
        self._clear()
        b = self.body
        self._lbl(b, "איזה סיכום להכין?", 16, True)
        self._lbl(b, "בחר את סוג השיחה — לכל סוג מבנה סיכום שמתאים לו.", 10, fg=DIM, pady=(2, 12))
        self.cards = {}
        for key, label, desc, _ in summary.TYPES:
            card = tk.Frame(b, bg=self.CARD, cursor="hand2", highlightthickness=2, highlightbackground=self.CARD)
            card.pack(fill="x", pady=3)
            l1 = self._lbl(card, label, 12, True, bg=self.CARD, padx=14, pady=(7, 0))
            l2 = self._lbl(card, desc, 10, fg=DIM, bg=self.CARD, padx=14, pady=(0, 7))
            for x in (card, l1, l2):
                x.bind("<Button-1>", lambda e, k=key: self._pick(k))
            self.cards[key] = (card, l1, l2)
        self._lbl(b, "רמת פירוט", 11, True, pady=(16, 6))
        row = tk.Frame(b, bg=BG); row.pack(fill="x")
        self.len_btns = {}
        for key, label, _ in summary.LENGTHS:          # הראשון הכי ימני
            bt = tk.Button(row, text=label, relief="flat", cursor="hand2", font=("Segoe UI", 11), padx=22, pady=5,
                           command=lambda k=key: self._pick_len(k))
            bt.pack(side="right", padx=(8, 0))
            self.len_btns[key] = bt
        self.err = self._lbl(b, "", 10, fg="#fb7185", pady=(10, 0))
        foot = tk.Frame(b, bg=BG); foot.pack(side="bottom", fill="x")
        self._btn(foot, "✨  צור סיכום", self._run, primary=True).pack(side="left")
        self._pick(self.kind); self._pick_len(self.length)

    def _pick(self, k):
        self.kind = k
        for key, (card, l1, l2) in self.cards.items():
            sel = key == k
            bg = self.CARD_SEL if sel else self.CARD
            card.configure(bg=bg, highlightbackground=ACCENT if sel else self.CARD)
            l1.configure(bg=bg); l2.configure(bg=bg)

    def _pick_len(self, k):
        self.length = k
        CFG["summary_length"] = k
        for key, bt in self.len_btns.items():
            sel = key == k
            bt.configure(bg=ACCENT if sel else "#2a2a34", fg="white" if sel else FG,
                         activebackground="#7465e6" if sel else "#3a3a48")

    # ---- שלב 2: מסכמים
    def _run(self):
        if not CLAUDE_KEY or anthropic is None:
            self.err.configure(text=rtl("חסר ANTHROPIC_API_KEY בקובץ server.env או ‎.env — בלי זה אי אפשר לסכם.")); return
        try:
            text = self.transcript_fn()
        except Exception as e:
            self.err.configure(text=rtl(f"לא הצלחתי לקרוא את התמלול: {e}")); return
        if len(text.split()) < 15:
            self.err.configure(text=rtl("התמלול קצר מדי לסיכום.")); return
        save_config()
        self._clear()
        self._lbl(self.body, "מסכם…", 16, True)
        tname = summary.type_by_key(self.kind)[1]
        lname = summary.length_by_key(self.length)[1]
        self._lbl(self.body, f"{tname} · {lname} · {len(text.split())} מילים. זה לוקח בדרך כלל 10–40 שניות.", 10, fg=DIM)
        self.result = None
        kind, length = self.kind, self.length

        def work():
            try:
                system, user = summary.build_prompt(kind, length, text, self.title)
                cl = anthropic.Anthropic(api_key=CLAUDE_KEY, timeout=240.0)
                md = claude_text(CFG.get("summary_model", "claude-sonnet-5-5"), system, user,
                                 max_tokens=6000 if length == "detailed" else 2000, client=cl)
                path = f"{self.base}_סיכום_{tname.split(' /')[0].replace(' ', '_')}.docx"
                try:
                    summary.write_docx(path, f"סיכום — {self.title}", f"{tname} · {lname} · {datetime.now():%d/%m/%Y %H:%M}", md)
                except Exception as e:
                    log("סיכום: Word לא נשמר:", e); path = None
                log("סיכום נוצר:", tname, lname, len(md), "תווים ->", path)
                self.result = ("ok", md, path)
            except Exception as e:
                log("סיכום נכשל:", e)
                self.result = ("err", str(e), None)
        threading.Thread(target=work, daemon=True).start()
        self._wait()

    def _wait(self):
        if self.result is None:
            self.win.after(250, self._wait); return
        st, md, path = self.result
        if st == "err":
            self._choose(); self.err.configure(text=rtl("הסיכום נכשל: " + short(md, 120))); return
        self.md, self.path = md, path
        self._show()

    # ---- שלב 3: התוצאה
    def _show(self):
        self._clear()
        b = self.body
        top = tk.Frame(b, bg=BG); top.pack(fill="x", pady=(0, 10))
        tk.Label(top, text=rtl("סיכום"), bg=BG, fg=FG, font=("Segoe UI", 16, "bold")).pack(side="right")
        self._btn(top, "📋 העתק", self._copy).pack(side="left")
        if self.path:
            self._btn(top, "📄 פתח Word", lambda: os.startfile(self.path)).pack(side="left", padx=8)
        self._btn(top, "סיכום אחר", self._choose).pack(side="left")
        frame = tk.Frame(b, bg="#1e1e26"); frame.pack(fill="both", expand=True)
        sb = tk.Scrollbar(frame); sb.pack(side="left", fill="y")
        t = tk.Text(frame, bg="#1e1e26", fg=FG, font=("Segoe UI", 12), wrap="word", relief="flat", padx=18, pady=14,
                    spacing1=2, spacing3=6, yscrollcommand=sb.set)
        t.pack(side="right", fill="both", expand=True)
        sb.config(command=t.yview)
        t.tag_configure("p", justify="right")
        t.tag_configure("h", justify="right", font=("Segoe UI", 13, "bold"), foreground="#a99bff", spacing1=12)
        t.tag_configure("li", justify="right", rmargin=0, lmargin1=0)
        t.tag_configure("li2", justify="right", foreground="#c8c8d2")
        for kind, text in summary.parse(self.md):
            if kind == "h":
                t.insert("end", rtl(text) + "\n", "h")
            elif kind == "li":
                t.insert("end", rtl("•  " + text) + "\n", "li")
            elif kind == "li2":
                t.insert("end", rtl("      ◦  " + text) + "\n", "li2")
            else:
                t.insert("end", rtl(text) + "\n", "p")
        t.configure(state="disabled")
        if self.path:
            self._lbl(b, "נשמר גם כקובץ Word ליד ההקלטה.", 9, fg="#6b6b7a", pady=(8, 0))

    def _copy(self):
        txt = "\n".join(("• " if k == "li" else "  ◦ " if k == "li2" else "") + x for k, x in summary.parse(self.md))
        pyperclip.copy(txt)
        ui_q.put(("flash", "📋 הסיכום הועתק"))


def summarize_saved():
    """בוחרים Word של הקלטה שמורה ומסכמים אותו."""
    from tkinter import filedialog
    init = P("meetings") if os.path.isdir(P("meetings")) else HERE
    path = filedialog.askopenfilename(title="בחר הקלטה לסיכום", initialdir=init,
                                      filetypes=[("Word", "*.docx")])
    if not path:
        return
    base = os.path.splitext(path)[0]
    kind = "lecture" if "שיעור" in os.path.basename(path) or "lectures" in path else "work"
    SummaryWindow(ROOT["tk"], lambda: summary.transcript_from_docx(path), os.path.basename(base), base, kind)


class MeetingWindow:
    """חלון פגישה גדול: כל דובר בצבע משלו, מיושר לימין, מתעדכן תוך כדי."""

    PALETTE = ["#8b7bff", "#38bdf8", "#f59e0b", "#34d399", "#f472b6", "#a3e635", "#fb7185", "#22d3ee"]

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("Flow-HE — פגישה")
        self.win.configure(bg=BG)
        # חלון גדול מההתחלה (80% מהמסך); אם המשתמש שינה גודל/מיקום — נפתח שוב בדיוק כך
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        g = CFG.get("meeting_geom")
        m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", g or "")
        if m and int(m[1]) >= 400 and int(m[2]) >= 300 and -50 < int(m[3]) < sw - 100 and -10 < int(m[4]) < sh - 100:
            self.win.geometry(g)
        else:
            w, h = int(sw * 0.8), int(sh * 0.82)
            self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        if CFG.get("meeting_zoomed"):
            try: self.win.state("zoomed")
            except tk.TclError: pass
        self._gjob = None
        self.win.bind("<Configure>", self._remember_geom)
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.win.update_idletasks()
        OWN_HWNDS.add(hwnd_of(self.win))
        top = tk.Frame(self.win, bg=BG); top.pack(fill="x", padx=18, pady=(16, 8))
        self.title_lbl = tk.Label(top, text="פגישה", bg=BG, fg=FG, font=("Segoe UI", 16, "bold"))
        self.title_lbl.pack(side="right")
        self.status = tk.Label(top, text="", bg=BG, fg=DIM, font=("Segoe UI", 11))
        self.status.pack(side="right", padx=14)
        self.btn = tk.Button(top, text="■  סיים פגישה", command=toggle_meeting, bg=REC, fg="white", relief="flat",
                             padx=16, pady=4, font=("Segoe UI", 10, "bold"), cursor="hand2", activebackground="#d6304d")
        self.btn.pack(side="left")
        self.open_btn = tk.Button(top, text="📄 פתח Word", command=self._open_doc, bg="#2a2a34", fg=FG, relief="flat",
                                  padx=12, pady=4, font=("Segoe UI", 10), cursor="hand2", state="disabled")
        self.open_btn.pack(side="left", padx=8)
        self.sum_btn = tk.Button(top, text="✨ סכם", command=self._summarize, bg="#2a2a34", fg=FG, relief="flat",
                                 padx=12, pady=4, font=("Segoe UI", 10), cursor="hand2")
        self.sum_btn.pack(side="left", padx=(0, 8))
        tk.Button(top, text="📂", command=lambda: os.startfile(P("meetings")), bg="#2a2a34", fg=FG, relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left")
        self.nspk = tk.StringVar(value="אוטומטי")
        om = tk.OptionMenu(top, self.nspk, "אוטומטי", "2", "3", "4", "5", "6", "7", "8", command=self._set_nspk)
        om.config(bg="#2a2a34", fg=FG, relief="flat", highlightthickness=0, activebackground="#3a3a48", activeforeground=FG,
                  font=("Segoe UI", 10), cursor="hand2")
        om["menu"].config(bg="#2a2a34", fg=FG, activebackground="#3a3a48")
        om.pack(side="left", padx=(12, 2))
        tk.Label(top, text=":מספר דוברים", bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(side="left")
        tk.Label(self.win, text="טקסט אפור = זמני, מתוקן כל דקה וחצי · לחיצה כפולה על שם דובר — לשנות את השם (נשמר גם ב-Word)", bg=BG, fg="#6b6b7a",
                 font=("Segoe UI", 9)).pack(side="bottom", anchor="e", padx=20, pady=(0, 10))
        frame = tk.Frame(self.win, bg="#1e1e26"); frame.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        sb = tk.Scrollbar(frame); sb.pack(side="left", fill="y")
        self.text = tk.Text(frame, bg="#1e1e26", fg=FG, font=("Segoe UI", 14), wrap="word", relief="flat",
                            padx=18, pady=14, spacing1=2, spacing3=10, yscrollcommand=sb.set, cursor="arrow")
        self.text.pack(side="right", fill="both", expand=True)
        sb.config(command=self.text.yview)
        self.text.tag_configure("rtl", justify="right")
        self.text.tag_configure("time", foreground="#6b6b7a", font=("Segoe UI", 9))
        self.text.bind("<Key>", lambda e: "break" if e.keysym not in ("c", "C", "a", "A") or not (e.state & 4) else None)
        self.colors = {}
        self.ses, self.ver = None, -1

    def _remember_geom(self, e=None):
        if e is not None and e.widget is not self.win:
            return
        if self._gjob:
            self.win.after_cancel(self._gjob)

        def save():
            self._gjob = None
            if SELFTEST:      # בבדיקות ה-CFG מכיל ערכי בדיקה זמניים — לא לשמור אותם לקובץ
                return
            try:
                z = self.win.state() == "zoomed"
                CFG["meeting_zoomed"] = z
                if not z and self.win.state() == "normal":
                    CFG["meeting_geom"] = self.win.geometry()
                save_config()
            except tk.TclError:
                pass
        self._gjob = self.win.after(800, save)

    def _set_nspk(self, v):
        if self.ses:
            self.ses.num_speakers = -1 if v == "אוטומטי" else int(v)
            CFG["meeting_speakers"] = self.ses.num_speakers; save_config()

    def _tag(self, spk):
        """תגית צבע לדובר + לחיצה כפולה לשינוי שם."""
        if spk not in self.colors:
            i = len(self.colors)
            tag = f"spk{i}"
            self.colors[spk] = tag
            self.text.tag_configure(tag, foreground=self.PALETTE[i % len(self.PALETTE)], font=("Segoe UI", 11, "bold"))
            self.text.tag_bind(tag, "<Double-Button-1>", lambda e, t=tag: self._rename(t))
            self.text.tag_bind(tag, "<Enter>", lambda e: self.text.config(cursor="hand2"))
            self.text.tag_bind(tag, "<Leave>", lambda e: self.text.config(cursor="arrow"))
        return self.colors[spk]

    def _rename(self, tag):
        from tkinter import simpledialog
        old = next((k for k, v in self.colors.items() if v == tag), None)
        if not old or not self.ses:
            return
        new = simpledialog.askstring("שם הדובר", f"שם חדש במקום \"{old}\":", parent=self.win, initialvalue=old)
        if new and new.strip() and new.strip() != old:
            new = new.strip()
            self.colors[new] = self.colors.pop(old)
            self.ses.rename(old, new)

    def _summarize(self):
        ses = self.ses
        if not ses:
            return
        base = os.path.splitext(ses.docx)[0] if ses.docx else ses.base
        SummaryWindow(self.win, lambda: summary.transcript_from_entries(list(ses.entries)), ses.title, base,
                      "lecture" if ses.kind == "lecture" else "work")

    def _open_doc(self):
        if self.ses and self.ses.docx and os.path.exists(self.ses.docx):
            os.startfile(self.ses.docx)

    def show(self, ses):
        self.ses, self.ver = ses, -1
        self.win.title(f"Flow-HE — {ses.title}")
        self.title_lbl.config(text=ses.title)
        self.nspk.set("אוטומטי" if ses.num_speakers < 2 else str(ses.num_speakers))
        self.text.delete("1.0", "end")
        self.btn.config(text=f"■  סיים {ses.title}", state="normal")
        self.open_btn.config(state="disabled")
        self.win.deiconify(); self.win.lift()
        self._tick()

    def _render(self, entries):
        at_end = self.text.yview()[1] > 0.95
        self.text.delete("1.0", "end")
        if not entries:
            self.text.tag_configure("hint", justify="right", foreground="#6b6b7a", font=("Segoe UI", 12))
            self.text.insert("1.0", rtl("מקשיב… הדברים יופיעו כאן כמה שניות אחרי שכל דובר עוצר לנשום.") + "\n", "hint")
            return
        self.text.tag_configure("tent", foreground="#8a8a96")   # זמני — עוד לא תוקן בחתיכה הגדולה
        refined = {src.name: src.refined_until for src in self.ses.sources} if self.ses and self.ses.on else {}
        prev = None
        for start, spk, t, _src in entries:
            if spk != prev:
                if prev is not None:
                    self.text.insert("end", "\n", "rtl")
                self.text.insert("end", rtl(spk), ("rtl", self._tag(spk)))
                self.text.insert("end", "   " + meeting_doc.fmt_time(start) + "\n", ("rtl", "time"))
            tent = _src in refined and start >= refined[_src]
            self.text.insert("end", rtl(t) + "\n", ("rtl", "tent") if tent else "rtl")
            prev = spk
        if at_end:
            self.text.see("end")

    def _tick(self):
        ses = self.ses
        if ses is None or not self.win.winfo_exists():
            return
        if ses.version != self.ver:
            self.ver = ses.version
            with ses.lock:
                entries = list(ses.entries)
            self._render(entries)
        el = int(time.time() - ses.t0)
        if ses.on:
            st = f"● מקליט {meeting_doc.fmt_time(el)}"
            if any(not s.ok for s in ses.sources[1:]):
                st += " · מיקרופון בלבד"
            if ses.inflight:
                st += f" · {ses.inflight} בתמלול"
            if ses.refining:
                st += " · מתקן…"
        else:
            self.btn.config(text=f"ה{ses.title} הסתיימה" if ses.kind == "meeting" else "השיעור הסתיים", state="disabled")
            if ses.final_state == "saved":
                st = "✓ נשמר כקובץ Word"
                self.open_btn.config(state="normal")
            elif ses.final_state and ses.final_state != "done":
                st = f"משפר את התמלול {ses.final_state}"
            else:
                st = "מסיים…"
        self.status.config(text=st)
        if ses.on or ses.final_state != "saved":
            self.win.after(500, self._tick)


class ResultWindow:
    """חלון תוצאות: הטקסט האחרון + היסטוריה, עם העתקה והדבקה לחלון שהיה פעיל."""

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.title("Flow-HE — תוצאות")
        self.win.configure(bg=BG)
        _sc = screen_scale()
        self.win.geometry(f"{int(620 * _sc)}x{int(560 * _sc)}")
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)
        self.win.update_idletasks()
        OWN_HWNDS.add(hwnd_of(self.win))
        btn = dict(bg="#2a2a34", fg=FG, relief="flat", padx=12, pady=4, cursor="hand2", activebackground="#3a3a48",
                   activeforeground=FG, font=("Segoe UI", 10))
        top = tk.Frame(self.win, bg=BG); top.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(top, text="תמלול אחרון", bg=BG, fg=FG, font=("Segoe UI", 14, "bold")).pack(side="right")
        tk.Button(top, text="📋 העתק", command=self.copy, **btn).pack(side="left", padx=(0, 6))
        tk.Button(top, text="⤴ הדבק לחלון הקודם", command=self.paste_back, **btn).pack(side="left", padx=6)
        tk.Button(top, text="🗑 נקה", command=lambda: self.text.delete("1.0", "end"), **btn).pack(side="left", padx=6)
        self.text = tk.Text(self.win, bg="#1e1e26", fg=FG, insertbackground=FG, font=("Segoe UI", 13), wrap="word",
                            relief="flat", padx=16, pady=12, height=7, spacing3=6)
        self.text.pack(fill="both", expand=True, padx=18)
        self.text.tag_configure("rtl", justify="right")
        tk.Label(self.win, text="היסטוריה · לחיצה כפולה מעתיקה", bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(anchor="e", padx=20, pady=(12, 4))
        self.hist = tk.Listbox(self.win, bg="#1e1e26", fg=FG, font=("Segoe UI", 11), relief="flat", justify="right", height=8,
                               selectbackground="#3a3a48", activestyle="none", highlightthickness=0, borderwidth=0)
        self.hist.pack(fill="both", expand=True, padx=18, pady=(0, 18))
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
                toggle_meeting("lecture")
            elif ev == "meeting":
                toggle_meeting("meeting")
            elif ev == "meeting_win" and Meeting.active:
                if bar.meeting_win is None or not bar.meeting_win.win.winfo_exists():
                    bar.meeting_win = MeetingWindow(root)
                bar.meeting_win.show(Meeting.active)
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
        pystray.MenuItem(lambda _: (f"■ סיים את ה{Meeting.active.title}" if Meeting.active else "● הקלטת פגישה / שיעור"), lambda: ui_q.put(("meeting", None))),
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

    # 9ב) הווידג'ט: ריחוף מציג ⋯ ו-–, התפריט נפתח, ונסגר בלחיצה מחוץ לו
    e2e["bar_layered"] = bar.layered
    bar.set_state("idle"); pump_for(0.3)
    wx, wy = root.winfo_x(), root.winfo_y()
    b = bar.body
    pyautogui.moveTo(int(wx + (b[0] + b[2]) / 2), int(wy + (b[1] + b[3]) / 2)); pump_for(0.5)
    pump_for(0.6)
    e2e["hover_shows_controls"] = bool(bar.hover and bar.grow > 0.99 and "menu" in bar.hits)
    try:
        ImageGrab.grab(bbox=(wx - 10, wy - 10, wx + bar.W + 10, wy + bar.cur_h + 10), all_screens=True).save(os.path.join(out, "bar_hover.png"))
    except Exception:
        pass
    mb = bar.hits.get("menu")
    wx, wy = root.winfo_x(), root.winfo_y()      # התווית מעל הכפתור הגדילה את החלון כלפי מעלה
    if mb:
        pyautogui.click(int(wx + (mb[0] + mb[2]) / 2), int(wy + (mb[1] + mb[3]) / 2)); pump_for(0.7)
    e2e["menu_opens"] = PopMenu.is_open()
    ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_menu.png"))
    pyautogui.click(u.GetSystemMetrics(0) // 2 - 300, 140); pump_for(0.6)
    e2e["menu_closes_on_outside_click"] = not PopMenu.is_open()
    if PopMenu.is_open():
        PopMenu.root_menu.close()
    bar._open_menu(); pump_for(0.4)
    pyautogui.press("esc"); pump_for(0.4)
    e2e["menu_closes_on_esc"] = not PopMenu.is_open()
    if PopMenu.is_open():
        PopMenu.root_menu.close()
    pyautogui.moveTo(u.GetSystemMetrics(0) // 2, 300); pump_for(0.3)
    report["ok"] &= all(e2e.get(k) for k in ("hover_shows_controls", "menu_opens", "menu_closes_on_outside_click", "menu_closes_on_esc"))

    # 10) תמלול תוך כדי הקלטה: 13 שניות "דיבור" עם הפסקות, כל קטע לוקח 2 שניות בשרת
    pyautogui.moveTo(5, 5)  # העכבר לא מעל שדה — הטקסט נכנס איפה שהסמן
    focus_hwnd(h); clear()
    type_text("קיים: "); pump_for(0.3)   # טקסט שהיה לפני ההקלטה — אסור שיימחק
    saved_env = {k: os.environ.get(k) for k in ("FLOWHE_FAKE_AUDIO", "FLOWHE_FAKE_TRANSCRIPT", "FLOWHE_FAKE_DELAY")}
    os.environ.update(FLOWHE_FAKE_AUDIO="stream", FLOWHE_FAKE_TRANSCRIPT="__dur__", FLOWHE_FAKE_DELAY="2")
    pyautogui.press(PTT_PYAUTOGUI); pump_for(0.1); pyautogui.press(PTT_PYAUTOGUI)   # ללא ידיים
    pump_for(13)
    def read_edit(hw):
        ed = u.FindWindowExW(hw, 0, "Edit", None) or u.FindWindowExW(hw, 0, "RichEditD2DPT", None) or hw
        n = u.SendMessageW(ed, 0x000E, 0, 0) + 1  # WM_GETTEXTLENGTH
        buf = ctypes.create_unicode_buffer(n); u.SendMessageW(ed, 0x000D, n, buf)  # WM_GETTEXT
        return buf.value
    lf_end = time.time() + 3   # מחכים שההקלדה של הקטע האחרון תסתיים (קריאה באמצע הקלדה = חתוך)
    lf = read_edit(h)
    while "…" not in lf and time.time() < lf_end:
        pump_for(0.2); lf = read_edit(h)
    e2e["live_in_field_while_recording"] = lf
    try:
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_live_in_field.png"))
    except Exception:
        pass
    sent_live = rec.session.sent_before_end if rec.session else 0
    e2e["live_text_while_recording"] = LIVE["session"].live_text() if LIVE["session"] else ""
    e2e["bar_grew_while_recording"] = bar.card_h > 0
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
    lf = e2e["live_in_field_while_recording"]
    e2e["live_in_field_ok"] = lf.startswith("קיים:") and "…" in lf and "<" in lf
    e2e["final_replaced_live_ok"] = got.startswith("קיים:") and "…" not in got and got.count("<") == 1
    report["ok"] &= e2e["live_in_field_ok"] and e2e["final_replaced_live_ok"]
    e2e["stream_ok"] = (sent_live >= 1 and got.count("<") == 1 and e2e["stream_wait_after_stop_s"] < 6.0
                        and e2e["live_text_while_recording"].count("<") >= 1 and e2e["bar_grew_while_recording"])
    e2e["bar_back_to_normal"] = bar.card_h == 0
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

    # 11) שיעור = אותו מנגנון כמו פגישה: חלון, תיקון תוך כדי (חתיכה גדולה מחליפה את הזמני), מעבר סופי ו-Word בתיקיית השיעורים
    saved_env = {k: os.environ.get(k) for k in ("FLOWHE_FAKE_AUDIO", "FLOWHE_FAKE_TRANSCRIPT", "FLOWHE_FAKE_DELAY")}
    os.environ.update(FLOWHE_FAKE_AUDIO="stream", FLOWHE_FAKE_TRANSCRIPT="__uniq__", FLOWHE_FAKE_DELAY="0.3")
    CFG["meeting_min_s"], CFG["meeting_max_s"], CFG["meeting_refine_s"] = 2.0, 6.0, 8.0
    CFG["meeting_system_audio"] = False
    ui_q.put(("lecture", None)); pump_for(16)
    lec = Meeting.active
    t_r = time.time() + 12      # המחשב של ה-CI איטי לפעמים: מחכים שהתיקון הראשון יחזור לפני שעוצרים
    while lec and lec.sources[0].refined_until < 8 and time.time() < t_r:
        pump_for(0.3)
    e2e["lecture_started"] = bool(lec and lec.kind == "lecture")
    e2e["lecture_window"] = bool(bar.meeting_win and bar.meeting_win.win.winfo_viewable())
    e2e["lecture_refined_until_s"] = round(lec.sources[0].refined_until, 1) if lec else 0
    try:
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_lecture.png"))
    except Exception:
        pass
    ui_q.put(("meeting", None)); pump_for(0.5)
    t_end = time.time() + 40
    while lec and lec.final_state != "saved" and time.time() < t_end:
        pump_for(0.3)
    pump_for(0.5)
    e2e["lecture_docx"] = bool(lec and lec.docx and os.path.exists(lec.docx) and "lectures" in lec.docx)
    e2e["lecture_ok"] = (e2e["lecture_started"] and e2e["lecture_window"] and e2e["lecture_docx"]
                         and e2e["lecture_refined_until_s"] >= 8 and Meeting.active is None)
    CFG["meeting_system_audio"] = True
    CFG["meeting_refine_s"] = 75.0
    if bar.meeting_win:
        bar.meeting_win.win.withdraw()
    for k_, v_ in saved_env.items():
        if v_ is None: os.environ.pop(k_, None)
        else: os.environ[k_] = v_
    report["ok"] &= bool(e2e["lecture_ok"])

    # 12) מצב פגישה: הקלטה אמיתית של 4 דוברים על "מיקרופון" אחד -> זיהוי דוברים לפי קול, חלון, מעבר שני, Word
    saved_env = {k: os.environ.get(k) for k in ("FLOWHE_FAKE_AUDIO", "FLOWHE_FAKE_TRANSCRIPT", "FLOWHE_FAKE_DELAY")}
    four = os.path.join(HERE, "models", "test-four-speakers.wav")
    if not os.path.exists(four):
        four = os.path.join(BUNDLE, "models", "test-four-speakers.wav")
    has_four = os.path.exists(four)
    e2e["meeting_diarize_models"] = bool(speakers.models_dir(BUNDLE, HERE))
    os.environ.update(FLOWHE_FAKE_AUDIO=("file:" + four) if has_four else "stream", FLOWHE_FAKE_TRANSCRIPT="__uniq__", FLOWHE_FAKE_DELAY="0.3")
    CFG["meeting_min_s"], CFG["meeting_max_s"] = 2.0, 8.0
    CFG["meeting_system_audio"] = not has_four
    CFG["meeting_speakers"] = -1   # הקובץ הוא 4 דוברים: אוטומטי
    CFG["meeting_refine"] = False  # כאן בודקים את זיהוי הדוברים המקומי (בלי שרת אמיתי)
    ui_q.put(("meeting", None)); pump_for(16 if has_four else 15)
    mt = Meeting.active
    e2e["meeting_started"] = mt is not None
    e2e["meeting_window"] = bool(bar.meeting_win and bar.meeting_win.win.winfo_viewable())
    e2e["meeting_live_speakers"] = sorted({e[1] for e in mt.entries}) if mt else []
    try:
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_meeting_live.png"))
    except Exception:
        pass
    ui_q.put(("meeting", None)); pump_for(0.5)
    t_end = time.time() + 60
    while mt and mt.final_state != "saved" and time.time() < t_end:
        pump_for(0.3)
    pump_for(1.0)
    try:
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_meeting_done.png"))
    except Exception:
        pass
    e2e["meeting_docx"] = bool(mt and mt.docx and os.path.exists(mt.docx))
    e2e["meeting_final_speakers"] = sorted({e[1] for e in mt.entries}) if mt else []
    if has_four and e2e["meeting_diarize_models"]:
        # 4 דוברים אמיתיים: בזמן אמת לפחות 2, ובמעבר השני 3-5 (המודל שחזר 4 בבדיקה מקומית)
        spk_ok = len(e2e["meeting_live_speakers"]) >= 2 and 3 <= len(e2e["meeting_final_speakers"]) <= 5
    else:
        spk_ok = len(e2e["meeting_final_speakers"]) >= 1
    e2e["meeting_ok"] = (e2e["meeting_started"] and e2e["meeting_window"] and e2e["meeting_docx"] and spk_ok)
    CFG["meeting_refine"] = True
    CFG["meeting_system_audio"] = True
    if mt and mt.docx:
        try:
            import shutil; shutil.copy(mt.docx, os.path.join(out, "meeting.docx"))
        except Exception:
            pass
    if bar.meeting_win:
        bar.meeting_win.win.withdraw()
    # 12) חלון הסיכום: מסך הבחירה מוצג; תוצאה לדוגמה מוצגת ונשמרת ל-Word
    try:
        sw_ = SummaryWindow(root, lambda: "x " * 50, "בדיקה", os.path.join(out, "בדיקה"), "lecture")
        pump_for(0.6)
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_summary_choose.png"))
        sw_.md = "## הנושא בשורה אחת\nבדיקה של סיכום, עם Vercel באמצע.\n## רעיונות מרכזיים\n- רעיון ראשון\n  - פירוט\n- רעיון שני"
        sw_.path = summary.write_docx(os.path.join(out, "summary.docx"), "סיכום — בדיקה", "הרצאה · קצר", sw_.md)
        sw_._show(); pump_for(0.6)
        ImageGrab.grab(all_screens=True).save(os.path.join(out, "e2e_summary_result.png"))
        e2e["summary_ui_ok"] = os.path.exists(sw_.path)
        sw_.win.destroy()
    except Exception as ex:
        e2e["summary_ui_ok"] = False; e2e["summary_ui_err"] = str(ex)
    report["ok"] &= e2e["summary_ui_ok"]
    for k_, v_ in saved_env.items():
        if v_ is None: os.environ.pop(k_, None)
        else: os.environ[k_] = v_
    report["ok"] &= bool(e2e["meeting_ok"])

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
        if canon not in _DOWN:
            _DOWN_T[canon] = time.time()
        _DOWN.add(canon)
    else:
        _DOWN.discard(canon)
    # הספרייה מפספסת לפעמים שחרור של Ctrl/Alt (למשל אחרי Ctrl+V שהאפליקציה עצמה שולחת),
    # ואז כל אות נראית כמו קיצור. בודקים מול Windows שמקשי-העזר באמת לחוצים.
    try:
        gks = ctypes.windll.user32.GetAsyncKeyState
        for mod, vks in _MOD_VK.items():
            if (mod in _DOWN and mod != canon and time.time() - _DOWN_T.get(mod, 0) > 1.5
                    and not any(gks(v) & 0x8000 for v in vks)):
                _DOWN.discard(mod)
    except Exception:
        pass
    return name


_DOWN_T = {}
_MOD_VK = {"ctrl": (0x11,), "alt": (0x12,), "shift": (0x10,), "win": (0x5B, 0x5C)}


def mods_really_down(*mods):
    try:
        gks = ctypes.windll.user32.GetAsyncKeyState
        return all(any(gks(v) & 0x8000 for v in _MOD_VK[m]) for m in mods)
    except Exception:
        return True


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
    if CFG.get("scratch_hotkey", False):   # כבוי: Ctrl+Alt+N הופעל בטעות בזמן הקלדה
        keyboard.add_hotkey(HK_SCRATCH, lambda: (finish() if mode else begin("scratch")) if mods_really_down("ctrl", "alt") else None)

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
    print(f"  {HK_CANCEL:18} ביטול")
    print(f"  AI auto-edits: {'פעיל' if AI_EDITS else 'כבוי (אין ANTHROPIC_API_KEY)'} · privacy: {PRIVACY}")
    print("  " + stats())


def run_ui():
    root = tk.Tk()
    ROOT["tk"] = root
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
