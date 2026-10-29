"""חלון עם שקיפות אמיתית לכל פיקסל (UpdateLayeredWindow) — קצוות חלקים וצל רך, בלי "צבע שקוף" שמפקסל את השוליים.

פיקסלים שקופים לגמרי לא תופסים לחיצות (העכבר עובר לחלון שמתחת), כמו בווידג'טים של Windows.
"""
import ctypes
from ctypes import wintypes

import numpy as np

WS_EX_LAYERED = 0x00080000
GWL_EXSTYLE = -20
ULW_ALPHA = 2
AC_SRC_OVER, AC_SRC_ALPHA = 0, 1


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


_ready = False


def _setup():
    global _ready, u32, g32
    if _ready:
        return
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    u32.GetDC.argtypes = [vp]; u32.GetDC.restype = vp
    u32.ReleaseDC.argtypes = [vp, vp]
    g32.CreateCompatibleDC.argtypes = [vp]; g32.CreateCompatibleDC.restype = vp
    g32.CreateDIBSection.argtypes = [vp, ctypes.POINTER(BITMAPINFOHEADER), ctypes.c_uint, ctypes.POINTER(vp), vp, wintypes.DWORD]
    g32.CreateDIBSection.restype = vp
    g32.SelectObject.argtypes = [vp, vp]; g32.SelectObject.restype = vp
    g32.DeleteObject.argtypes = [vp]
    g32.DeleteDC.argtypes = [vp]
    u32.UpdateLayeredWindow.argtypes = [vp, vp, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), vp,
                                        ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    u32.UpdateLayeredWindow.restype = wintypes.BOOL
    u32.GetWindowLongW.argtypes = [vp, ctypes.c_int]; u32.GetWindowLongW.restype = ctypes.c_long
    u32.SetWindowLongW.argtypes = [vp, ctypes.c_int, ctypes.c_long]
    _ready = True


def enable(hwnd):
    _setup()
    st = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, st | WS_EX_LAYERED)


def update(hwnd, img, x, y):
    """img: PIL RGBA. מחזיר True אם הצליח."""
    _setup()
    w, h = img.size
    a = np.asarray(img.convert("RGBA"), dtype=np.uint16)
    alpha = a[..., 3:4]
    bgra = np.empty((h, w, 4), dtype=np.uint8)
    bgra[..., 0] = (a[..., 2] * alpha[..., 0] // 255)
    bgra[..., 1] = (a[..., 1] * alpha[..., 0] // 255)
    bgra[..., 2] = (a[..., 0] * alpha[..., 0] // 255)
    bgra[..., 3] = alpha[..., 0]
    buf = bgra.tobytes()
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    screen = u32.GetDC(None)
    mem = g32.CreateCompatibleDC(screen)
    bits = ctypes.c_void_p()
    hbmp = g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ok = False
    try:
        if not hbmp or not bits.value:
            return False
        ctypes.memmove(bits.value, buf, len(buf))
        old = g32.SelectObject(mem, hbmp)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        ok = bool(u32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(int(x), int(y))),
                                          ctypes.byref(wintypes.SIZE(w, h)), mem, ctypes.byref(wintypes.POINT(0, 0)),
                                          0, ctypes.byref(blend), ULW_ALPHA))
        g32.SelectObject(mem, old)
    finally:
        if hbmp:
            g32.DeleteObject(hbmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(None, screen)
    return ok
