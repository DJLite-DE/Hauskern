# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
winsys.py – Windows-Extras ohne Zusatzpakete:
  * Symbol im Infobereich (Tray) mit Menü
  * Benachrichtigungen (erscheinen als Windows-Toast)
  * Autostart über HKCU\\...\\Run
  * App-Icon (.ico) wird beim ersten Start selbst erzeugt
"""
import os
import queue
import struct
import sys
import threading

IS_WIN = os.name == "nt"


# --------------------------------------------------------------------------- #
# Icon erzeugen (32x32, blaues Kachel-Symbol mit Haus)
# --------------------------------------------------------------------------- #
def make_icon(path, size=32):
    if os.path.exists(path):
        return path
    px = []
    r = size * 0.22
    for y in range(size):
        row = []
        for x in range(size):
            # abgerundetes Quadrat
            cx = min(max(x + 0.5, r), size - r)
            cy = min(max(y + 0.5, r), size - r)
            inside = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r
            col = (0, 0, 0, 0)
            if inside:
                col = (0x3d, 0x8b, 0xfd, 255)
                fx, fy = (x + 0.5) / size, (y + 0.5) / size
                roof = 0.18 <= fy <= 0.5 and abs(fx - 0.5) <= (fy - 0.18) * 1.05
                body = 0.46 <= fy <= 0.8 and 0.28 <= fx <= 0.72
                door = 0.6 <= fy <= 0.8 and 0.44 <= fx <= 0.56
                if (roof or body) and not door:
                    col = (255, 255, 255, 255)
            row.append(col)
        px.append(row)
    pixels = b"".join(bytes((b, g, r_, a)) for row in reversed(px) for (r_, g, b, a) in row)
    mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
    img = bih + pixels + mask
    data = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(img), 22) + img
    with open(path, "wb") as f:
        f.write(data)
    return path


# --------------------------------------------------------------------------- #
# Autostart
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _pythonw():
    exe = sys.executable
    d, n = os.path.split(exe)
    if n.lower() == "python.exe" and os.path.exists(os.path.join(d, "pythonw.exe")):
        return os.path.join(d, "pythonw.exe")
    return exe


def set_autostart(name, script, enable):
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, f'"{_pythonw()}" "{os.path.abspath(script)}" --tray')
        else:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass


def get_autostart(name):
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, name)
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Tray-Icon
# --------------------------------------------------------------------------- #
class Tray:
    """Tray-Symbol in eigenem Thread. on_event(name) wird mit 'open' / 'quit' aufgerufen (aus dem Thread!)."""

    WM_TRAY = 0x0400 + 20
    WM_NOTIFY_APP = 0x0400 + 21
    ID_OPEN, ID_QUIT = 1001, 1002

    def __init__(self, tooltip, icon_path, on_event):
        self.tooltip, self.icon_path, self.on_event = tooltip, icon_path, on_event
        self.hwnd = None
        self.ok = False
        self._msgs = queue.Queue()
        if IS_WIN:
            threading.Thread(target=self._run, daemon=True).start()

    def notify(self, title, text, warning=False):
        if not IS_WIN:
            print(f"[Hinweis] {title}: {text}")
            return
        self._msgs.put((title[:63], text[:255], warning))
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, self.WM_NOTIFY_APP, 0, 0)

    def close(self):
        if self.hwnd:
            self._u.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE

    # -------- Win32 --------
    def _run(self):
        import ctypes
        from ctypes import wintypes as w
        u = ctypes.WinDLL("user32", use_last_error=True)
        s = ctypes.WinDLL("shell32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._u = u
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("a", w.DWORD), ("b", w.WORD), ("c", w.WORD), ("d", ctypes.c_ubyte * 8)]

        class NID(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

        u.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u.CreateWindowExW.restype = w.HWND
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        u.LoadImageW.restype = w.HANDLE
        u.LoadIconW.argtypes = [w.HINSTANCE, w.LPVOID]
        u.LoadIconW.restype = w.HICON
        u.CreatePopupMenu.restype = w.HMENU
        u.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
        u.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
        u.DestroyMenu.argtypes = [w.HMENU]
        u.SetForegroundWindow.argtypes = [w.HWND]
        u.DestroyWindow.argtypes = [w.HWND]
        s.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NID)]
        k.GetModuleHandleW.restype = w.HMODULE

        hinst = k.GetModuleHandleW(None)
        hicon = u.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x40) if self.icon_path else None
        if not hicon:
            hicon = u.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION

        nid = NID()
        nid.cbSize = ctypes.sizeof(NID)
        nid.uID = 1
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = hicon
        nid.szTip = self.tooltip[:127]

        def wndproc(hwnd, msg, wp, lp):
            try:
                if msg == self.WM_TRAY:
                    ev = lp & 0xFFFF
                    if ev in (0x0202, 0x0203, 0x0405):  # Linksklick, Doppelklick, Klick auf Hinweis
                        self.on_event("open")
                    elif ev == 0x0205:  # Rechtsklick
                        menu = u.CreatePopupMenu()
                        u.AppendMenuW(menu, 0, self.ID_OPEN, "Öffnen")
                        u.AppendMenuW(menu, 0x800, 0, None)  # Trennlinie
                        u.AppendMenuW(menu, 0, self.ID_QUIT, "Beenden")
                        pt = w.POINT()
                        u.GetCursorPos(ctypes.byref(pt))
                        u.SetForegroundWindow(hwnd)
                        cmd = u.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0, hwnd, None)
                        u.PostMessageW(hwnd, 0, 0, 0)
                        u.DestroyMenu(menu)
                        if cmd == self.ID_OPEN:
                            self.on_event("open")
                        elif cmd == self.ID_QUIT:
                            self.on_event("quit")
                    return 0
                if msg == self.WM_NOTIFY_APP:
                    while not self._msgs.empty():
                        title, text, warn = self._msgs.get_nowait()
                        nid.uFlags = 0x10  # NIF_INFO
                        nid.szInfoTitle = title
                        nid.szInfo = text
                        nid.dwInfoFlags = 0x02 if warn else 0x04 | 0x20  # Warnung bzw. eigenes großes Icon
                        nid.hBalloonIcon = hicon
                        s.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY
                    return 0
                if msg == 0x0010:  # WM_CLOSE
                    u.DestroyWindow(hwnd)
                    return 0
                if msg == 0x0002:  # WM_DESTROY
                    s.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
                    u.PostQuitMessage(0)
                    return 0
            except Exception:
                pass
            return u.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc = WNDPROC(wndproc)  # Referenz halten!
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "HauskernTray"
        u.RegisterClassW(ctypes.byref(wc))
        hwnd = u.CreateWindowExW(0, wc.lpszClassName, "HauskernTray", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            return
        nid.hWnd = hwnd
        nid.uFlags = 0x01 | 0x02 | 0x04  # MESSAGE | ICON | TIP
        if not s.Shell_NotifyIconW(0, ctypes.byref(nid)):  # NIM_ADD
            return
        self.hwnd, self.ok = hwnd, True
        if not self._msgs.empty():
            u.PostMessageW(hwnd, self.WM_NOTIFY_APP, 0, 0)
        msg = w.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        self.hwnd, self.ok = None, False
