"""Voicemeeter Background — a wallpaper that follows the Voicemeeter window.

Non-invasive: nothing is patched or injected into Voicemeeter. The app creates a
click-through, semi-transparent top-level window and keeps it glued to the real
Voicemeeter window (position, size, z-order, minimise/restore, DPI, virtual
desktops, "always on top").

Windows cannot insert a third-party window between an application's background
and its controls, so the image is blended over the whole client area. Keep the
opacity low for readability.

Not affiliated with VB-Audio. "Voicemeeter" is a trademark of VB-Audio Software.
"""
from __future__ import annotations

import ctypes
import json
import os
import random
import shutil
import sys
import time
import traceback
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageTk

APP_TITLE = "Voicemeeter Background"
APP_NAME = "VoicemeeterBackground"
LEGACY_APP_NAME = "VoicemeeterBG"  # settings folder of earlier versions
_BASE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APP_DIR = _BASE_DIR / APP_NAME
CONFIG_FILE = APP_DIR / "settings.json"
LOG_FILE = APP_DIR / "overlay.log"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")

# Genuine Voicemeeter executables only. Intentionally excludes any *_vmchroma.exe.
VOICEMEETER_EXES = {
    "voicemeeter.exe",
    "voicemeeter_x64.exe",
    "voicemeeterpro.exe",
    "voicemeeterpro_x64.exe",
    "voicemeeter8.exe",
    "voicemeeter8x64.exe",
}

# Tracking behaviour
TICK_FAST_MS = 15            # while following a window (~ one display frame)
TICK_IDLE_MS = 250           # while waiting for Voicemeeter
RESCAN_MISSING_S = 0.4       # look for Voicemeeter this often when it is not found
RESCAN_HIDDEN_S = 1.0        # ... or when it is minimised / hidden
RESIZE_RENDER_INTERVAL_S = 0.06  # max re-render rate while the window is resized
MIN_WIDTH, MIN_HEIGHT = 320, 180

# Win32 constants
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
CLICKTHROUGH_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
GA_ROOT = 2
GW_HWNDPREV = 3
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOOWNERZORDER = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 13

_LOG_LAST: dict[str, float] = {}


def log_exception(context: str, exc: BaseException) -> None:
    """Write technical errors to a user-accessible log without crashing the UI.

    Rate-limited per context so a persistent error cannot flood the log.
    """
    now = time.monotonic()
    if now - _LOG_LAST.get(context, -999.0) < 5.0:
        return
    _LOG_LAST[context] = now
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            stream.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def default_config() -> dict:
    return {
        "mode": "image",
        "enabled": False,
        "interval": 60,
        "random": False,
        "opacity": 22,
        "darkness": 20,
        "blur": 0,
        "fit": "Remplir (recadrer)",
        "fade": 400,
        "titlebar": False,
        "startup": False,
        "overlay_mode_version": 1,
    }


def load_config() -> dict:
    try:
        legacy = _BASE_DIR / LEGACY_APP_NAME / "settings.json"
        if not CONFIG_FILE.exists() and legacy.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(legacy, CONFIG_FILE)
    except Exception:
        pass
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # v1 migration: the old Chroma opacity (~53 %) is far too strong for an overlay.
            if data.get("overlay_mode_version") != 1:
                data["opacity"] = 22
                data["darkness"] = min(30, int(data.get("darkness", 20)))
                data["overlay_mode_version"] = 1
            return {**default_config(), **data}
    except Exception:
        pass
    return default_config()


def save_config(data: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def enable_dpi_awareness() -> None:
    """Make window coordinates physical pixels.

    Without this, on a 125 % / 150 % display the coordinates read from Voicemeeter's
    window are virtualised and the overlay ends up offset or the wrong size.
    Must run before the Tk root is created.
    """
    if os.name != "nt":
        return
    try:  # Windows 10 1703+: per-monitor v2
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        if fn(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:  # Windows 8.1+: per-monitor
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:  # Vista+: system DPI
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Win32:
    """Small window finder / positioning wrapper. It only reads other windows."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("La superposition native nécessite Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwm = None
        self._path_cache: dict[int, str] = {}
        self._last_geo: tuple | None = None
        self._configure_signatures()

    def _configure_signatures(self):
        u, k = self.user32, self.kernel32
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        u.EnumWindows.argtypes = [self.WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.restype = wintypes.BOOL
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.ClientToScreen.restype = wintypes.BOOL
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        get_long = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        set_long = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t
        self._get_long = get_long
        self._set_long = set_long

        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

        if self.dwm is not None:
            self.dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                       ctypes.c_void_p, wintypes.DWORD]
            self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    # ---- process / window queries -------------------------------------------------

    def _process_path(self, pid: int) -> str:
        if pid in self._path_cache:
            return self._path_cache[pid]
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            path = buffer.value if self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)) else ""
        finally:
            self.kernel32.CloseHandle(handle)
        if len(self._path_cache) > 512:
            self._path_cache.clear()
        self._path_cache[pid] = path
        return path

    def window_exists(self, hwnd) -> bool:
        return bool(self.user32.IsWindow(hwnd))

    def _is_cloaked(self, hwnd) -> bool:
        """True for windows on another virtual desktop (they still report 'visible')."""
        if self.dwm is None:
            return False
        value = wintypes.DWORD(0)
        try:
            hr = self.dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value),
                                                ctypes.sizeof(value))
        except OSError:
            return False
        return hr == 0 and value.value != 0

    def is_trackable(self, hwnd) -> bool:
        return bool(self.user32.IsWindow(hwnd) and self.user32.IsWindowVisible(hwnd)
                    and not self.user32.IsIconic(hwnd) and not self._is_cloaked(hwnd))

    def get_geometry(self, hwnd, cover_titlebar: bool):
        """(x, y, width, height) in physical screen pixels, or None."""
        if cover_titlebar:
            rect = wintypes.RECT()
            ok = False
            if self.dwm is not None:  # visible frame, without the invisible resize border
                ok = self.dwm.DwmGetWindowAttribute(
                    hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
            if not ok and not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            x, y = rect.left, rect.top
            width, height = rect.right - rect.left, rect.bottom - rect.top
        else:
            client = wintypes.RECT()
            if not self.user32.GetClientRect(hwnd, ctypes.byref(client)):
                return None
            top_left = wintypes.POINT(client.left, client.top)
            bottom_right = wintypes.POINT(client.right, client.bottom)
            if not (self.user32.ClientToScreen(hwnd, ctypes.byref(top_left))
                    and self.user32.ClientToScreen(hwnd, ctypes.byref(bottom_right))):
                return None
            x, y = top_left.x, top_left.y
            width, height = bottom_right.x - top_left.x, bottom_right.y - top_left.y
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return None
        return (x, y, width, height)

    def find_voicemeeter_window(self, cover_titlebar: bool) -> dict | None:
        candidates = []

        def enum_callback(hwnd, _lparam):
            try:
                if not self.is_trackable(hwnd):
                    return True
                pid = wintypes.DWORD(0)
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if not pid.value:
                    return True
                path = self._process_path(pid.value)
                if Path(path).name.lower() not in VOICEMEETER_EXES:
                    return True
                geo = self.get_geometry(hwnd, cover_titlebar)
                if geo is None:
                    return True
                length = self.user32.GetWindowTextLengthW(hwnd)
                buffer = ctypes.create_unicode_buffer(max(2, length + 2))
                self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
                title = buffer.value
                candidates.append({
                    "hwnd": hwnd, "geo": geo, "title": title, "path": path,
                    "named": 1 if "voicemeeter" in title.lower() else 0,
                    "area": geo[2] * geo[3],
                })
            except Exception as exc:
                log_exception("Recherche de la fenêtre Voicemeeter", exc)
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(enum_callback), 0)
        if not candidates:
            return None
        # The main window carries "Voicemeeter" in its title; then take the largest one.
        candidates.sort(key=lambda row: (row["named"], row["area"]), reverse=True)
        return candidates[0]

    # ---- overlay window -----------------------------------------------------------

    def root_window(self, child_id):
        return self.user32.GetAncestor(child_id, GA_ROOT) or child_id

    def attach_clickthrough_style(self, hwnd):
        """Idempotent: only writes when a style bit is missing."""
        style = self._get_long(hwnd, GWL_EXSTYLE)
        if style & CLICKTHROUGH_STYLE != CLICKTHROUGH_STYLE:
            self._set_long(hwnd, GWL_EXSTYLE, style | CLICKTHROUGH_STYLE)

    def place_overlay(self, overlay, target, geo, force: bool = False):
        """Glue the overlay on `target`: same rectangle, directly above it in z-order.

        Other windows placed above Voicemeeter still cover the overlay too, and an
        "always on top" Voicemeeter gets an overlay in the topmost band.
        """
        x, y, width, height = geo
        flags = SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW
        target_top = bool(self._get_long(target, GWL_EXSTYLE) & WS_EX_TOPMOST)
        overlay_top = bool(self._get_long(overlay, GWL_EXSTYLE) & WS_EX_TOPMOST)
        previous = self.user32.GetWindow(target, GW_HWNDPREV)

        if previous == overlay and overlay_top == target_top:
            # Already directly above Voicemeeter: nothing to do unless the rectangle moved.
            if geo == self._last_geo and not force:
                return
            flags |= SWP_NOZORDER
            insert_after = wintypes.HWND(0)
        elif not previous:
            insert_after = wintypes.HWND(-1 if target_top else 0)      # HWND_TOPMOST / HWND_TOP
        elif (self._get_long(previous, GWL_EXSTYLE) & WS_EX_TOPMOST) and not target_top:
            insert_after = wintypes.HWND(-2)                           # HWND_NOTOPMOST
        else:
            insert_after = previous

        if not self.user32.SetWindowPos(overlay, insert_after, x, y, width, height, flags):
            self.user32.SetWindowPos(overlay, wintypes.HWND(0), x, y, width, height, flags)
        self._last_geo = geo

    def hide_overlay(self, overlay):
        self._last_geo = None
        self.user32.SetWindowPos(
            overlay, wintypes.HWND(0), 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_HIDEWINDOW)


def cover_resize(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fill `size`, cropping the excess (fast: one resample from a crop box)."""
    sw, sh = source.size
    tw, th = size
    scale = max(tw / sw, th / sh)
    cw, ch = tw / scale, th / scale
    left, top = (sw - cw) / 2, (sh - ch) / 2
    return source.resize(size, Image.Resampling.LANCZOS,
                         box=(left, top, left + cw, top + ch), reducing_gap=2.0)


def contain_resize(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fit entirely inside `size`, centred on a dark canvas."""
    sw, sh = source.size
    scale = min(size[0] / sw, size[1] / sh)
    new_size = (max(1, round(sw * scale)), max(1, round(sh * scale)))
    resized = source.resize(new_size, Image.Resampling.LANCZOS, reducing_gap=2.0)
    canvas = Image.new("RGB", size, (16, 18, 22))
    canvas.paste(resized, ((size[0] - new_size[0]) // 2, (size[1] - new_size[1]) // 2))
    return canvas


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.configure(bg="#f0f0f0")
        try:
            self.ui_scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
        except Exception:
            self.ui_scale = 1.0
        self.cfg = load_config()

        self.image_path = tk.StringVar(value=self.cfg.get("image", ""))
        self.folder_path = tk.StringVar(value=self.cfg.get("folder", ""))
        self.mode = tk.StringVar(value=self.cfg.get("mode", "image"))
        self.enabled = tk.BooleanVar(value=bool(self.cfg.get("enabled", False)))
        self.interval = tk.IntVar(value=int(self.cfg.get("interval", 60)))
        self.random_order = tk.BooleanVar(value=bool(self.cfg.get("random", False)))
        self.opacity = tk.IntVar(value=max(0, min(45, int(self.cfg.get("opacity", 22)))))
        self.darkness = tk.IntVar(value=max(0, min(80, int(self.cfg.get("darkness", 20)))))
        self.blur = tk.IntVar(value=max(0, min(30, int(self.cfg.get("blur", 0)))))
        self.fit = tk.StringVar(value=self.cfg.get("fit", "Remplir (recadrer)"))
        self.fade = tk.IntVar(value=max(0, min(3000, int(self.cfg.get("fade", 400)))))
        self.titlebar = tk.BooleanVar(value=bool(self.cfg.get("titlebar", False)))
        self.startup = tk.BooleanVar(value=bool(self.cfg.get("startup", False)))

        self.current_slide: str | None = self.cfg.get("current_slide")
        self.last_slide_change = time.monotonic()
        self.preview_photo = None
        self.overlay_photo = None
        self.overlay_window: tk.Toplevel | None = None
        self.overlay_label: tk.Label | None = None
        self.overlay_hwnd = None
        self.win32: Win32 | None = None
        self.overlay_visible = False
        self.overlay_shown_once = False
        self.overlay_job = None
        self.last_status = ""
        self.last_overlay_key = None
        self.last_overlay_rendered: Image.Image | None = None
        self.last_render_at = 0.0
        self.source_image_path: str | None = None
        self.fade_transition = None
        self.last_fade_paint = 0.0
        self.applied_alpha: float | None = None
        self.failed_path: str | None = None

        self.target_hwnd = None
        self.target_title = ""
        self.last_scan = 0.0
        self.wait_status = "en attente de la fenêtre Voicemeeter"

        self._source_key = None
        self._source_image: Image.Image | None = None
        self._images_cache = None

        self._build()
        self._refresh_preview()
        # Size the window to its content (works at any display scaling).
        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(1200, self._start_if_configured)

    def _px(self, value: int) -> int:
        return int(value * self.ui_scale)

    # ---- UI -----------------------------------------------------------------------

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("TFrame", background="#f0f0f0")
        style.configure("TLabelframe", background="#f0f0f0")
        style.configure("TLabelframe.Label", background="#f0f0f0")
        style.configure("TLabel", background="#f0f0f0", font=("Segoe UI", 9))
        style.configure("TButton", font=("Segoe UI", 9))

        outer = ttk.Frame(self.root, padding=(16, 12))
        outer.pack(fill="both", expand=True)
        ttk.Checkbutton(outer, text="Afficher le fond sur Voicemeeter", variable=self.enabled,
                        command=self._toggle_enabled).pack(anchor="w", pady=(0, 8))

        group = ttk.LabelFrame(outer, text="Image", padding=10)
        group.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(group, text="Une image", variable=self.mode, value="image",
                        command=self._mode_changed).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(group, textvariable=self.image_path).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(group, text="Parcourir...", command=self.pick_image).grid(row=0, column=2, padx=(4, 0))
        ttk.Radiobutton(group, text="Diaporama d'un dossier", variable=self.mode, value="folder",
                        command=self._mode_changed).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(group, textvariable=self.folder_path).grid(row=1, column=1, sticky="ew", padx=4, pady=(8, 0))
        ttk.Button(group, text="Parcourir...", command=self.pick_folder).grid(row=1, column=2, padx=(4, 0), pady=(8, 0))
        row = ttk.Frame(group)
        row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(row, text="Changer toutes les").pack(side="left")
        ttk.Spinbox(row, from_=5, to=86400, width=6, textvariable=self.interval,
                    command=self._save).pack(side="left", padx=6)
        ttk.Label(row, text="s").pack(side="left")
        ttk.Checkbutton(row, text="Ordre aléatoire", variable=self.random_order,
                        command=self._save).pack(side="left", padx=(16, 10))
        ttk.Button(row, text="Image suivante", command=lambda: self.next_image(manual=True)).pack(side="left")
        group.columnconfigure(1, weight=1)
        ttk.Label(group, text="Formats acceptés : PNG, JPG, JPEG, BMP, WEBP, TIFF").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        appearance = ttk.LabelFrame(outer, text="Apparence", padding=10)
        appearance.pack(fill="x", pady=(0, 8))
        self._slider(appearance, "Opacité de l'image", self.opacity, 0, 45, "%", 0)
        self._slider(appearance, "Assombrir", self.darkness, 0, 80, "%", 1)
        self._slider(appearance, "Flou", self.blur, 0, 30, "", 2)
        ttk.Label(appearance, text="Cadrage").grid(row=3, column=0, sticky="w", pady=5)
        fit_box = ttk.Combobox(appearance, textvariable=self.fit,
                               values=["Remplir (recadrer)", "Ajuster (entière)"],
                               state="readonly", width=24)
        fit_box.grid(row=3, column=1, sticky="w", pady=5)
        fit_box.bind("<<ComboboxSelected>>", lambda _e: self._appearance_changed())
        ttk.Label(appearance, text="Fondu des images (ms)").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(appearance, from_=0, to=3000, increment=100, width=8,
                    textvariable=self.fade, command=self._save).grid(row=4, column=1, sticky="w", pady=5)
        ttk.Label(appearance, text="Aperçu").grid(row=5, column=0, sticky="nw", pady=(8, 0))
        # Text-mode size (characters) is set in _refresh_preview; with an image the label
        # takes the image's own pixel size.
        self.preview = tk.Label(appearance, bg="#222222", fg="white", relief="sunken")
        self.preview.grid(row=5, column=1, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(appearance, text="Actualiser l'aperçu", command=self._refresh_preview).grid(
            row=6, column=1, sticky="w", pady=(8, 0))

        behavior = ttk.LabelFrame(outer, text="Comportement", padding=10)
        behavior.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(behavior, text="Couvrir aussi la barre de titre", variable=self.titlebar,
                        command=self._appearance_changed).pack(anchor="w")
        ttk.Checkbutton(behavior, text="Lancer au démarrage de Windows", variable=self.startup,
                        command=self.toggle_startup).pack(anchor="w", pady=(6, 0))

        apply_group = ttk.LabelFrame(outer, text="Application — sans modification de Voicemeeter", padding=10)
        apply_group.pack(fill="x")
        ttk.Label(apply_group, text=(
            "L'image suit la fenêtre Voicemeeter (position, taille, premier plan), à faible opacité, "
            "dans une fenêtre transparente et non cliquable. "
            "Pour garder les contrôles lisibles, commence par 10 à 25 %."
        ), wraplength=self._px(690), justify="left").pack(anchor="w")
        buttons = ttk.Frame(apply_group)
        buttons.pack(fill="x", pady=(9, 4))
        ttk.Button(buttons, text="Activer / actualiser le fond", command=self.start_overlay).pack(side="left")
        ttk.Button(buttons, text="Masquer le fond", command=self.stop_overlay).pack(side="left", padx=8)
        ttk.Button(buttons, text="Ouvrir les paramètres", command=self.open_config_folder).pack(side="left")
        self.status_var = tk.StringVar(value="État : désactivé")
        self.status_label = ttk.Label(apply_group, textvariable=self.status_var, foreground="#555555")
        self.status_label.pack(anchor="w", pady=(4, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, text="Laisse l'application ouverte (réduite si besoin) pour garder le fond actif.").pack(side="left")
        ttk.Button(footer, text="Fermer", command=self.close).pack(side="right")
        self.root.bind("<Control-Alt-n>", lambda _e: self.next_image(manual=True))
        self.root.bind("<Control-Alt-b>", lambda _e: self._toggle_enabled())

    def _slider(self, parent, label, var, lo, hi, suffix, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        value_label = ttk.Label(parent, text=f"{var.get()} {suffix}".strip(), width=7)
        value_label.grid(row=row, column=2, sticky="w")
        scale = ttk.Scale(parent, from_=lo, to=hi, orient="horizontal",
                          command=lambda value, vv=var, vlabel=value_label, unit=suffix:
                          self._slider_changed(vv, value, vlabel, unit))
        scale.grid(row=row, column=1, sticky="ew", padx=(16, 8), pady=4)
        scale.set(var.get())
        parent.columnconfigure(1, weight=1)

    def _slider_changed(self, var, value, value_label, suffix):
        var.set(int(float(value)))
        value_label.configure(text=f"{var.get()} {suffix}".strip())
        self._save()
        self._refresh_preview()
        self.fade_transition = None

    def _appearance_changed(self):
        self._save()
        self._refresh_preview()
        # A change in image processing/geometry should be rendered fresh on the next tick.
        self.last_overlay_key = None
        self.fade_transition = None
        self.failed_path = None

    def _mode_changed(self):
        self.current_slide = None
        self.last_slide_change = time.monotonic()
        self._images_cache = None
        self.failed_path = None
        self._save()
        self._refresh_preview()
        self.last_overlay_key = None

    def _save(self):
        data = {
            "image": self.image_path.get(), "folder": self.folder_path.get(), "mode": self.mode.get(),
            "enabled": self.enabled.get(), "interval": max(5, int(self.interval.get() or 60)),
            "random": self.random_order.get(), "opacity": self.opacity.get(), "darkness": self.darkness.get(),
            "blur": self.blur.get(), "fit": self.fit.get(), "fade": self.fade.get(),
            "titlebar": self.titlebar.get(), "startup": self.startup.get(),
            "current_slide": self.current_slide, "overlay_mode_version": 1,
        }
        try:
            save_config(data)
        except Exception as exc:
            log_exception("Enregistrement des paramètres", exc)

    def pick_image(self):
        path = filedialog.askopenfilename(
            title="Choisir une image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"), ("Tous les fichiers", "*.*")])
        if path:
            self.image_path.set(path)
            self.mode.set("image")
            self.current_slide = None
            self._images_cache = None
            self.failed_path = None
            self._save()
            self._refresh_preview()
            self.last_overlay_key = None
            if self.enabled.get():
                self.start_overlay(show_errors=False)

    def pick_folder(self):
        path = filedialog.askdirectory(title="Choisir un dossier d'images")
        if path:
            self.folder_path.set(path)
            self.mode.set("folder")
            self.current_slide = None
            self.last_slide_change = time.monotonic()
            self._images_cache = None
            self.failed_path = None
            self._save()
            self._refresh_preview()
            self.last_overlay_key = None
            if self.enabled.get():
                self.start_overlay(show_errors=False)

    # ---- images -------------------------------------------------------------------

    def _get_images(self) -> list[Path]:
        # Cached for a moment: the tracking loop runs ~60 times per second.
        key = (self.mode.get(), self.image_path.get(), self.folder_path.get())
        now = time.monotonic()
        cache = self._images_cache
        if cache and cache[0] == key and now - cache[1] < 1.5:
            return cache[2]
        result: list[Path] = []
        try:
            if key[0] == "image":
                path = Path(key[1]).expanduser()
                result = [path] if path.is_file() and path.suffix.lower() in IMAGE_EXTS else []
            else:
                directory = Path(key[2]).expanduser()
                if directory.is_dir():
                    result = sorted((path for path in directory.iterdir()
                                     if path.is_file() and path.suffix.lower() in IMAGE_EXTS),
                                    key=lambda path: path.name.lower())
        except (OSError, ValueError):
            result = []
        self._images_cache = (key, now, result)
        return result

    def _current_image(self) -> Path | None:
        paths = self._get_images()
        if not paths:
            return None
        if self.mode.get() == "image":
            return paths[0]
        if self.current_slide:
            current = Path(self.current_slide)
            if current in paths:
                return current
        if self.random_order.get():
            chosen = random.choice(paths)
        else:
            chosen = paths[0]
        self.current_slide = str(chosen)
        return chosen

    def _load_source(self, path: Path) -> Image.Image:
        """Decode once and keep it: re-reading the file on every resize would be slow."""
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        key = (str(path), mtime)
        if self._source_key != key or self._source_image is None:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((3840, 3840), Image.Resampling.LANCZOS)
            self._source_image = image
            self._source_key = key
        return self._source_image

    def _processed(self, path: Path, size: tuple[int, int]) -> Image.Image:
        source = self._load_source(path)
        if self.fit.get().startswith("Remplir"):
            image = cover_resize(source, size)
        else:
            image = contain_resize(source, size)
        if self.blur.get():
            image = image.filter(ImageFilter.GaussianBlur(float(self.blur.get())))
        if self.darkness.get():
            image = ImageEnhance.Brightness(image).enhance(1.0 - self.darkness.get() / 100.0)
        return image

    def _refresh_preview(self):
        if not hasattr(self, "preview"):
            return
        image_path = self._current_image()
        if image_path is None:
            self.preview.configure(image="", text="Choisis une image ou un dossier d'images",
                                   width=50, height=8)
            self.preview_photo = None
            return
        try:
            rendered = self._processed(image_path, (self._px(440), self._px(120)))
            # Simulates the expected mix on a neutral background; the real overlay blends
            # the same image over Voicemeeter at the selected opacity.
            rendered = Image.blend(Image.new("RGB", rendered.size, (45, 50, 56)),
                                   rendered, self.opacity.get() / 100.0)
            self.preview_photo = ImageTk.PhotoImage(rendered)
            self.preview.configure(image=self.preview_photo, text="", width=0, height=0)
        except Exception as exc:
            self.preview.configure(image="", text=f"Image impossible à lire : {exc}", width=50, height=8)
            self.preview_photo = None

    def next_image(self, manual: bool = False):
        if self.mode.get() != "folder":
            if manual:
                messagebox.showinfo("Diaporama", "Sélectionne d'abord « Diaporama d'un dossier » pour passer à l'image suivante.")
            return
        paths = self._get_images()
        if not paths:
            if manual:
                messagebox.showwarning("Dossier vide", "Le dossier choisi ne contient aucune image prise en charge.")
            return
        current = self._current_image()
        others = [path for path in paths if path != current]
        if not others:
            selected = paths[0]
        elif self.random_order.get():
            selected = random.choice(others)
        else:
            try:
                selected = paths[(paths.index(current) + 1) % len(paths)]
            except (ValueError, TypeError):
                selected = paths[0]
        self.current_slide = str(selected)
        self.last_slide_change = time.monotonic()
        self.failed_path = None
        self._save()
        self._refresh_preview()
        # Keep the currently shown frame for an optional cross-fade on the overlay.
        self.last_overlay_key = None
        if manual and not self.enabled.get():
            messagebox.showinfo("Image suivante", f"Image sélectionnée : {selected.name}\nActive le fond pour l'afficher sur Voicemeeter.")

    # ---- overlay ------------------------------------------------------------------

    def _toggle_enabled(self):
        if self.enabled.get():
            self.start_overlay()
        else:
            self.stop_overlay()

    def _set_status(self, text: str):
        if text != self.last_status:
            self.status_var.set("État : " + text)
            self.last_status = text

    def start_overlay(self, show_errors: bool = True):
        image_path = self._current_image()
        if image_path is None:
            self.enabled.set(False)
            self._save()
            if show_errors:
                messagebox.showwarning("Image manquante", "Choisis une image valide ou un dossier contenant des images.")
            self._set_status("choisis une image")
            return
        if os.name != "nt":
            if show_errors:
                messagebox.showerror("Windows requis", "La superposition native fonctionne uniquement sous Windows.")
            return
        try:
            if self.win32 is None:
                self.win32 = Win32()
            if self.overlay_window is None or not self.overlay_window.winfo_exists():
                self._create_overlay_window()
            self.failed_path = None
            self.last_overlay_key = None
            self.target_hwnd = None  # re-detect Voicemeeter
            self.last_scan = 0.0
            self.enabled.set(True)
            self._save()
            self._set_status("recherche de Voicemeeter…")
            self._ensure_overlay_tick(0)
        except Exception as exc:
            log_exception("Activation de la superposition", exc)
            self.enabled.set(False)
            self._save()
            self._set_status("erreur — voir overlay.log")
            if show_errors:
                messagebox.showerror("Impossible d'activer le fond", f"{exc}\n\nJournal : {LOG_FILE}")

    def _create_overlay_window(self):
        overlay = tk.Toplevel(self.root)
        overlay.withdraw()
        overlay.overrideredirect(True)
        overlay.configure(bg="#000000", borderwidth=0, highlightthickness=0)
        try:
            overlay.attributes("-topmost", False)
        except tk.TclError:
            pass
        # An explicit geometry stops Tk from resizing the window to its content, which
        # would fight with the exact rectangle we set through SetWindowPos.
        overlay.geometry("200x120+0+0")
        label = tk.Label(overlay, bg="#000000", borderwidth=0, highlightthickness=0,
                         padx=0, pady=0, anchor="nw")
        label.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        overlay.update_idletasks()
        self.overlay_window = overlay
        self.overlay_label = label
        self.overlay_photo = None
        self.overlay_visible = False
        self.overlay_shown_once = False
        self.applied_alpha = None
        self.overlay_hwnd = self.win32.root_window(overlay.winfo_id())
        self.win32.attach_clickthrough_style(self.overlay_hwnd)
        self._apply_alpha()

    def _apply_alpha(self):
        alpha = max(0.0, min(0.45, self.opacity.get() / 100.0))
        if alpha != self.applied_alpha and self.overlay_window is not None:
            self.overlay_window.attributes("-alpha", alpha)
            self.applied_alpha = alpha

    def _ensure_overlay_tick(self, delay: int = TICK_IDLE_MS):
        if self.overlay_job is None and self.enabled.get():
            self.overlay_job = self.root.after(delay, self._overlay_tick)

    def _track_target(self, now: float) -> dict | None:
        """Keep following the same Voicemeeter window; rescan only when it is lost."""
        win32 = self.win32
        hwnd = self.target_hwnd
        if hwnd is not None:
            if not win32.window_exists(hwnd):
                self.target_hwnd = hwnd = None
            elif win32.is_trackable(hwnd):
                geo = win32.get_geometry(hwnd, self.titlebar.get())
                if geo:
                    return {"hwnd": hwnd, "geo": geo, "title": self.target_title}
        # Lost, minimised, hidden, or on another virtual desktop.
        self.wait_status = ("Voicemeeter réduit ou masqué" if hwnd is not None
                            else "en attente de la fenêtre Voicemeeter")
        if now - self.last_scan >= (RESCAN_HIDDEN_S if hwnd is not None else RESCAN_MISSING_S):
            self.last_scan = now
            found = win32.find_voicemeeter_window(self.titlebar.get())
            if found:
                self.target_hwnd = found["hwnd"]
                self.target_title = found["title"] or Path(found["path"]).name
                return {"hwnd": found["hwnd"], "geo": found["geo"], "title": self.target_title}
        return None

    def _overlay_tick(self):
        self.overlay_job = None
        if not self.enabled.get() or self.overlay_window is None:
            return
        delay = TICK_IDLE_MS
        try:
            if self.win32 is None:
                self.win32 = Win32()
            now = time.monotonic()
            target = self._track_target(now)
            image_path = self._current_image()
            if target is None:
                self._hide_overlay()
                self._set_status(self.wait_status)
            elif image_path is None:
                self._hide_overlay()
                self._set_status("image introuvable")
            elif self._show_overlay(target, image_path, now):
                self._set_status(f"actif — suit {target['title']}")
                delay = TICK_FAST_MS
            else:
                self._hide_overlay()
                self._set_status("image illisible — voir overlay.log")

            if self.mode.get() == "folder" and self.enabled.get():
                interval = max(5, int(self.interval.get() or 60))
                if now - self.last_slide_change >= interval:
                    self.next_image(manual=False)
        except Exception as exc:
            log_exception("Boucle de superposition", exc)
            self._set_status("erreur — voir overlay.log")
        self._ensure_overlay_tick(delay)

    def _hide_overlay(self):
        if self.overlay_visible and self.overlay_hwnd is not None and self.win32 is not None:
            self.win32.hide_overlay(self.overlay_hwnd)
        self.overlay_visible = False

    def _show_overlay(self, target: dict, image_path: Path, now: float) -> bool:
        if self.overlay_window is None or self.overlay_label is None or self.win32 is None:
            return False
        x, y, width, height = target["geo"]
        path_key = str(image_path)
        render_key = (path_key, width, height, self.fit.get(), self.darkness.get(), self.blur.get())

        if render_key != self.last_overlay_key:
            previous = self.last_overlay_key
            only_resized = (previous is not None and previous[0] == path_key
                            and previous[3:] == render_key[3:])
            # While the window is being resized, re-render at a limited rate;
            # the final size is always rendered on a later tick.
            if not (only_resized and now - self.last_render_at < RESIZE_RENDER_INTERVAL_S):
                if self.failed_path == path_key:
                    return False
                try:
                    new_image = self._processed(image_path, (width, height))
                except Exception as exc:
                    log_exception("Lecture de l'image", exc)
                    self.failed_path = path_key
                    return False
                old_image = (self.last_overlay_rendered.copy()
                             if self.last_overlay_rendered is not None else None)
                fade_ms = max(0, int(self.fade.get() or 0))
                changed_source = self.source_image_path is not None and self.source_image_path != path_key
                if changed_source and old_image is not None and fade_ms > 0 and old_image.size == new_image.size:
                    self.fade_transition = {
                        "old": old_image, "new": new_image, "started": now,
                        "duration": fade_ms / 1000.0,
                    }
                    self.last_fade_paint = 0.0
                else:
                    self.fade_transition = None
                    self._set_overlay_image(new_image)
                    self.last_overlay_rendered = new_image.copy()
                self.last_overlay_key = render_key
                self.last_render_at = now
                self.source_image_path = path_key

        if self.fade_transition:
            # Paint at most about 25 frames per second during a fade.
            if now - self.last_fade_paint >= 0.04:
                transition = self.fade_transition
                progress = min(1.0, max(0.0, (now - transition["started"]) / transition["duration"]))
                blended = Image.blend(transition["old"], transition["new"], progress)
                self._set_overlay_image(blended)
                self.last_fade_paint = now
                if progress >= 1.0:
                    self.fade_transition = None
                    self.last_overlay_rendered = transition["new"].copy()
                else:
                    self.last_overlay_rendered = blended

        if self.last_overlay_rendered is None:
            return False

        if not self.overlay_shown_once:
            # First display: give Tk the final rectangle, then map it once.
            self.overlay_window.geometry(f"{width}x{height}+{x}+{y}")
            self.overlay_window.deiconify()
            self.overlay_window.update_idletasks()
            self.overlay_hwnd = self.win32.root_window(self.overlay_window.winfo_id())
            self.overlay_shown_once = True

        self.win32.attach_clickthrough_style(self.overlay_hwnd)
        self._apply_alpha()
        self.win32.place_overlay(self.overlay_hwnd, target["hwnd"], target["geo"],
                                 force=not self.overlay_visible)
        self.overlay_visible = True
        return True

    def _set_overlay_image(self, image: Image.Image):
        if not self.overlay_label:
            return
        self.overlay_photo = ImageTk.PhotoImage(image)
        self.overlay_label.configure(image=self.overlay_photo)

    def _destroy_overlay(self):
        if self.overlay_job is not None:
            try:
                self.root.after_cancel(self.overlay_job)
            except tk.TclError:
                pass
            self.overlay_job = None
        if self.overlay_window is not None:
            try:
                self.overlay_window.destroy()
            except tk.TclError:
                pass
        self.overlay_window = None
        self.overlay_label = None
        self.overlay_photo = None
        self.overlay_hwnd = None
        self.overlay_visible = False
        self.overlay_shown_once = False
        self.applied_alpha = None
        self.target_hwnd = None
        self.last_overlay_key = None
        self.last_overlay_rendered = None
        self.fade_transition = None

    def stop_overlay(self):
        self._destroy_overlay()
        self.enabled.set(False)
        self._save()
        self._set_status("désactivé")

    def _start_if_configured(self):
        if self.enabled.get() and self._current_image() is not None:
            self.start_overlay(show_errors=False)
        elif self.enabled.get():
            self._set_status("choisis une image")

    def toggle_startup(self):
        self._save()
        try:
            startup_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft/Windows/Start Menu/Programs/Startup"
            startup_dir.mkdir(parents=True, exist_ok=True)
            shortcut = startup_dir / f"{APP_NAME}.bat"
            legacy = startup_dir / "VoicemeeterBGStudio.bat"
            if legacy.exists():
                legacy.unlink()
            if self.startup.get():
                executable = Path(sys.executable)
                if getattr(sys, "frozen", False):
                    command = f'"{executable}"'
                else:
                    windowless = executable.with_name("pythonw.exe")  # no console window
                    if windowless.exists():
                        executable = windowless
                    command = f'"{executable}" "{Path(__file__).resolve()}"'
                shortcut.write_text("@echo off\nstart \"\" " + command + "\n", encoding="utf-8")
            elif shortcut.exists():
                shortcut.unlink()
        except Exception as exc:
            log_exception("Démarrage Windows", exc)
            messagebox.showwarning("Démarrage Windows", f"Impossible de modifier le démarrage automatique : {exc}")

    def open_config_folder(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(APP_DIR))
        except Exception:
            messagebox.showinfo("Dossier des paramètres", str(APP_DIR))

    def close(self):
        # Leaving the program closes its overlay. The enabled setting is preserved so
        # it can be recreated when the app is launched again (including at Windows startup).
        self._destroy_overlay()
        self._save()
        self.root.destroy()


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        App(root)
    except Exception as exc:
        log_exception("Démarrage de l'application", exc)
        messagebox.showerror(APP_TITLE, f"Erreur au démarrage : {exc}\n\nJournal : {LOG_FILE}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
