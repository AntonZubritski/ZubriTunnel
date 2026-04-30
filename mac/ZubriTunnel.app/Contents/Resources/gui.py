#!/usr/bin/env python3
"""vpn-proxy GUI — управление ключами Outline и точечный запуск программ через прокси."""
from __future__ import annotations  # allow `dict[str, dict]` / `T | None` on Python 3.7-3.9
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

# Paths — handle both dev mode (python gui.py) and PyInstaller-frozen .exe.
# Frozen .exe extracts itself to a temp _MEIPASS at runtime; user-writable
# data (keys/, settings.json) must live next to the .exe instead.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent  # next to ZubriTunnel.exe
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", str(SCRIPT_DIR)))
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = SCRIPT_DIR
KEYS_DIR = SCRIPT_DIR / "keys"
DEFAULT_ADDR = "127.0.0.1:8080"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def bundled_resource(name: str) -> Path:
    """Locate a read-only resource (icon, image) in the PyInstaller bundle
    OR next to gui.py in dev mode."""
    p = BUNDLE_DIR / name
    if p.exists():
        return p
    return SCRIPT_DIR / name


# ---------- helpers ----------

def sanitize_json_bytes(data: bytes) -> bytes:
    """Outline JSON often has raw control bytes inside strings → escape them."""
    out = bytearray()
    in_str = False
    escaped = False
    for b in data:
        if not in_str:
            if b == ord('"'):
                in_str = True
            out.append(b)
            continue
        if escaped:
            escaped = False
            out.append(b)
            continue
        if b == ord('\\'):
            escaped = True
            out.append(b)
            continue
        if b == ord('"'):
            in_str = False
            out.append(b)
            continue
        if b < 0x20:
            out.extend(b"\\u%04x" % b)
            continue
        out.append(b)
    return bytes(out)


def find_go_binary() -> str | None:
    """Найти `go` даже если PATH урезан (.app часто стартует без Homebrew в PATH)."""
    p = shutil.which("go")
    if p:
        return p
    candidates = [
        "/opt/homebrew/bin/go",      # Apple Silicon Homebrew
        "/usr/local/bin/go",          # Intel Homebrew
        "/usr/local/go/bin/go",       # ручная установка с go.dev/dl
        os.path.expanduser("~/go/bin/go"),
        "C:\\Program Files\\Go\\bin\\go.exe",
        "C:\\Go\\bin\\go.exe",
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def enhanced_path_env() -> dict:
    """Среда для subprocess с расширенным PATH — чтобы 'go' и 'brew' нашлись
    даже когда GUI запущен из .app с урезанным PATH."""
    env = os.environ.copy()
    extra = [
        "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/local/bin", "/usr/local/sbin",
        "/usr/local/go/bin",
        os.path.expanduser("~/go/bin"),
    ]
    cur = env.get("PATH", "")
    parts = cur.split(os.pathsep) if cur else []
    for p in extra:
        if p not in parts and os.path.isdir(p):
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    return env


def check_dependencies() -> list:
    """Список ключевых зависимостей с их статусом.
    Возвращает: [{name, ok, detail, fix_label, fix_action}]"""
    deps = []

    # Python (мы уже работаем, значит OK)
    deps.append({
        "name": "Python 3 + Tkinter",
        "ok": True,
        "detail": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})",
        "fix_label": None,
        "fix_action": None,
    })

    # vpn-proxy binary
    cmd = go_command()
    binary_path = cmd[0] if cmd[0] != "go" else None
    deps.append({
        "name": "vpn-proxy",
        "ok": binary_path is not None,
        "detail": binary_path or "не найден — будет fallback на 'go run'",
        "fix_label": "Собрать" if not binary_path else None,
        "fix_action": "build_vpn_proxy" if not binary_path else None,
    })

    # Go SDK (нужен для пересборки)
    go_path = find_go_binary()
    deps.append({
        "name": "Go SDK",
        "ok": go_path is not None,
        "detail": go_path or "не установлен (нужен только если хочешь пересобрать vpn-proxy)",
        "fix_label": "Установить" if not go_path else None,
        "fix_action": "install_go" if not go_path else None,
    })

    # Homebrew (Mac only)
    if IS_MAC:
        brew = shutil.which("brew")
        if not brew:
            for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
                if os.path.exists(p):
                    brew = p
                    break
        deps.append({
            "name": "Homebrew",
            "ok": brew is not None,
            "detail": brew or "не установлен (нужен для установки Go и других пакетов)",
            "fix_label": "Установить" if not brew else None,
            "fix_action": "install_brew" if not brew else None,
        })

    return deps


def go_command() -> list:
    """Return the command list to launch vpn-proxy. Order:
       1. binary next to gui.py (./vpn-proxy or ./vpn-proxy.exe)
       2. mac .app bundle: ../MacOS/vpn-proxy (CI-built universal binary)
       3. `go run .` fallback using absolute go path if Go SDK is installed
    """
    if IS_WIN:
        exe = SCRIPT_DIR / "vpn-proxy.exe"
        if exe.exists():
            return [str(exe)]
    else:
        exe = SCRIPT_DIR / "vpn-proxy"
        if exe.exists():
            return [str(exe)]
        macos_exe = SCRIPT_DIR.parent / "MacOS" / "vpn-proxy"
        if macos_exe.exists():
            return [str(macos_exe)]
    # Fall back to "go run ." using absolute path so PATH-less subprocesses still find it
    go = find_go_binary() or "go"
    return [go, "run", "."]


def fetch_ssconf(url: str) -> dict:
    https_url = url.replace("ssconf://", "https://", 1) if url.startswith("ssconf://") else url
    req = urllib.request.Request(https_url, headers={"User-Agent": "vpn-proxy-gui"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    return json.loads(sanitize_json_bytes(raw).decode("utf-8", errors="replace"))


# --- VanyaVPN-style provider API (host extracted from ssconf URL, UUID is the token) ---

import re as _re_uuid

_UUID_RE = _re_uuid.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_ssconf_url(url: str) -> tuple[str, str] | None:
    """Return (host, uuid) from ssconf URL, or None if not parseable."""
    if not url:
        return None
    https_url = url.replace("ssconf://", "https://", 1) if url.startswith("ssconf://") else url
    try:
        from urllib.parse import urlparse
        u = urlparse(https_url)
        host = u.hostname
        m = _UUID_RE.search(u.path)
        if not host or not m:
            return None
        return host, m.group(0)
    except Exception:
        return None


def fetch_provider_locations(ssconf_url: str, lang: str = "ru") -> list:
    """GET /app/v1/sync/available-locations — returns list of {description,value,code,bestLocation,systemLocation,speed}."""
    parsed = parse_ssconf_url(ssconf_url)
    if not parsed:
        raise ValueError("ссылка не похожа на ssconf:// с UUID")
    host, uuid = parsed
    api = f"https://{host}/app/v1/sync/available-locations?lang={lang}&token={uuid}"
    req = urllib.request.Request(api, headers={"User-Agent": "vpn-proxy-gui"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def change_provider_location(ssconf_url: str, location_value: str, lang: str = "ru") -> dict:
    """GET /app/v1/user/location/change — returns {tag: ...}."""
    parsed = parse_ssconf_url(ssconf_url)
    if not parsed:
        raise ValueError("ссылка не похожа на ssconf:// с UUID")
    host, uuid = parsed
    api = f"https://{host}/app/v1/user/location/change?token={uuid}&location={location_value}&lang={lang}"
    req = urllib.request.Request(api, headers={"User-Agent": "vpn-proxy-gui"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def parse_json_text(text: str) -> dict:
    return json.loads(sanitize_json_bytes(text.encode("utf-8")).decode("utf-8", errors="replace"))


def slugify(name: str) -> str:
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    s = "".join(c if c in keep else "-" for c in name.lower())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-") or "key"


def list_keys() -> list:
    KEYS_DIR.mkdir(exist_ok=True)
    out = []
    for f in sorted(KEYS_DIR.glob("*.json")):
        try:
            data = json.loads(sanitize_json_bytes(f.read_bytes()).decode("utf-8", errors="replace"))
            out.append({
                "name": f.stem,
                "path": f,
                "tag": data.get("tag", ""),
                "server": data.get("server", ""),
                "port": data.get("server_port", 0),
                "ok": bool(data.get("method") and data.get("password") and data.get("server")),
            })
        except Exception as e:
            out.append({"name": f.stem, "path": f, "tag": f"(broken: {e})", "server": "", "port": 0, "ok": False})
    return out


def detect_apps() -> list:
    """Return list of (name, command) for known apps installed on this system."""
    apps = []
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            ("VSCode", [r"C:\Program Files\Microsoft VS Code\Code.exe", fr"{local}\Programs\Microsoft VS Code\Code.exe"]),
            ("Git Bash", [r"C:\Program Files\Git\git-bash.exe", r"C:\Program Files (x86)\Git\git-bash.exe"]),
            ("PowerShell", [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"]),
            ("Windows Terminal", [fr"{local}\Microsoft\WindowsApps\wt.exe"]),
            ("Cursor", [fr"{local}\Programs\cursor\Cursor.exe"]),
            ("Chrome", [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]),
            ("Firefox", [r"C:\Program Files\Mozilla Firefox\firefox.exe"]),
            ("Edge", [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]),
        ]
        for name, paths in candidates:
            for p in paths:
                if os.path.exists(p):
                    apps.append((name, [p]))
                    break
    elif IS_MAC:
        # kind: "binary" — launch direct exe (env vars пропагируются: VSCode и Cursor умеют их читать)
        #       "open"   — open -na "App.app" (правильный путь для браузеров чтобы --args работал)
        macs = [
            ("VSCode",          "/Applications/Visual Studio Code.app/Contents/MacOS/Electron", "binary"),
            ("Cursor",          "/Applications/Cursor.app/Contents/MacOS/Cursor",                "binary"),
            ("Sublime",         "/Applications/Sublime Text.app",                                "open"),
            ("PyCharm",         "/Applications/PyCharm.app",                                     "open"),
            ("PyCharm CE",      "/Applications/PyCharm CE.app",                                  "open"),
            ("WebStorm",        "/Applications/WebStorm.app",                                    "open"),
            ("IntelliJ IDEA",   "/Applications/IntelliJ IDEA.app",                               "open"),
            ("Terminal",        "/System/Applications/Utilities/Terminal.app",                   "open"),
            ("iTerm",           "/Applications/iTerm.app",                                       "open"),
            ("Warp",            "/Applications/Warp.app",                                        "open"),
            ("Chrome",          "/Applications/Google Chrome.app",                               "open"),
            ("Firefox",         "/Applications/Firefox.app",                                     "open"),
            ("Safari",          "/Applications/Safari.app",                                      "open"),
            ("Edge",            "/Applications/Microsoft Edge.app",                              "open"),
            ("Brave",           "/Applications/Brave Browser.app",                               "open"),
            ("Opera",           "/Applications/Opera.app",                                       "open"),
            ("Vivaldi",         "/Applications/Vivaldi.app",                                     "open"),
            ("Yandex",          "/Applications/Yandex.app",                                      "open"),
            ("Arc",             "/Applications/Arc.app",                                         "open"),
            ("Slack",           "/Applications/Slack.app",                                       "open"),
            ("Discord",         "/Applications/Discord.app",                                     "open"),
            ("Telegram",        "/Applications/Telegram.app",                                    "open"),
            ("Spotify",         "/Applications/Spotify.app",                                     "open"),
            ("Postman",         "/Applications/Postman.app",                                     "open"),
        ]
        for name, p, kind in macs:
            if not os.path.exists(p):
                continue
            if kind == "binary":
                apps.append((name, [p]))
            else:
                apps.append((name, ["open", "-na", p]))
    return apps


def is_port_free(host: str, port: int) -> bool:
    """Try to bind to host:port. Returns True if successful (port is free)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind((host, port))
            return True
    except OSError:
        return False


def find_free_port(host: str = "127.0.0.1", preferred: int = 8080, avoid: set | None = None) -> int:
    """Try preferred port first, then nearby ranges, then fall back to OS-assigned.
    Pass `avoid` to skip ports already reserved by other proxies in this session.
    """
    avoid = avoid or set()
    candidates = [preferred] + list(range(preferred + 1, preferred + 30)) + list(range(18080, 18100))
    for p in candidates:
        if p in avoid:
            continue
        if is_port_free(host, p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def system_proxy_set(host_port: str | None):
    """Включить/выключить системный HTTP+HTTPS прокси.
       host_port: 'host:port' для включения, None для выключения.
       Возвращает (ok: bool, msg: str)."""
    if os.name == "nt":
        return _system_proxy_windows(host_port)
    if sys.platform == "darwin":
        return _system_proxy_macos(host_port)
    return _system_proxy_linux(host_port)


def _system_proxy_windows(host_port):
    try:
        import winreg
        import ctypes
        KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_WRITE) as k:
            if host_port:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, host_port)
                winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
            else:
                winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        # Notify Windows that proxy settings changed
        try:
            wininet = ctypes.windll.Wininet
            wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
            wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
        except Exception:
            pass
        return True, ""
    except Exception as e:
        return False, str(e)


def _system_proxy_macos(host_port):
    try:
        # Получить список активных сетевых сервисов (Wi-Fi, Ethernet, ...)
        r = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return False, r.stderr or r.stdout
        services = []
        for line in r.stdout.splitlines():
            s = line.strip()
            if not s or s.startswith("*") or s.startswith("An asterisk"):
                continue
            services.append(s)

        for svc in services:
            if host_port:
                host, _, port = host_port.partition(":")
                subprocess.run(["networksetup", "-setwebproxy", svc, host, port],
                               capture_output=True, timeout=5, **_win_subprocess_kwargs())
                subprocess.run(["networksetup", "-setsecurewebproxy", svc, host, port],
                               capture_output=True, timeout=5, **_win_subprocess_kwargs())
                subprocess.run(["networksetup", "-setproxybypassdomains", svc,
                                "localhost", "127.0.0.1", "*.local"],
                               capture_output=True, timeout=5, **_win_subprocess_kwargs())
            else:
                subprocess.run(["networksetup", "-setwebproxystate", svc, "off"],
                               capture_output=True, timeout=5, **_win_subprocess_kwargs())
                subprocess.run(["networksetup", "-setsecurewebproxystate", svc, "off"],
                               capture_output=True, timeout=5, **_win_subprocess_kwargs())
        return True, ""
    except Exception as e:
        return False, str(e)


def _system_proxy_linux(host_port):
    return False, "Системный прокси на Linux не реализован — поставь руками через GNOME/KDE settings"


# Windows: hide console for spawned child processes.
# When ZubriTunnel runs from a PyInstaller --windowed .exe, the parent has
# no console, and every child via subprocess.Popen by default gets a fresh
# console window flashing on screen. CREATE_NO_WINDOW (0x08000000) suppresses it.
WIN_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _win_subprocess_kwargs(extra_flags: int = 0) -> dict:
    """Return kwargs for subprocess.Popen/run that hide the console window
    on Windows. Returns empty dict on other platforms."""
    if os.name != "nt":
        return {}
    return {"creationflags": WIN_NO_WINDOW | extra_flags}


def proxy_env(proxy_url: str) -> dict:
    e = os.environ.copy()
    e.update({
        "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url, "https_proxy": proxy_url,
        "ALL_PROXY": proxy_url, "NO_PROXY": "localhost,127.0.0.1",
    })
    return e


def http_get_via_proxy(url: str, proxy: str, timeout: float = 10.0) -> str:
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    with opener.open(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


# ---------- rounded canvas widgets ----------

def _rounded_rect_points(w: int, h: int, r: int):
    r = max(0, min(r, min(w, h) // 2))
    return [
        r, 0, w - r, 0, w, 0, w, r,
        w, h - r, w, h, w - r, h, r, h,
        0, h, 0, h - r, 0, r, 0, 0,
    ]


class RoundButton(tk.Canvas):
    """Pill-style button drawn on Canvas. Plugs in like ttk.Button."""

    def __init__(self, parent, text: str = "", command=None, *,
                 variant: str = "default",
                 radius: int = 12, padx: int = 16, pady: int = 8,
                 width: int | None = None, font=None, **kw):
        self._variant = variant
        self._command = command
        self._text = text
        self._font = font or UI_FONT
        self._radius = radius
        self._padx = padx
        self._pady = pady
        self._enabled = True
        self._state = "normal"  # normal | hover | pressed | disabled

        # Parent bg for matching outer corners
        try:
            parent_bg = parent.cget("bg")
        except tk.TclError:
            parent_bg = COLORS["bg"]
        super().__init__(parent, highlightthickness=0, bd=0, bg=parent_bg, **kw)
        self._sync_size(width)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._draw())

    def _sync_size(self, width: int | None):
        from tkinter.font import Font as TkFont
        f = TkFont(family=self._font[0], size=self._font[1],
                   weight=("bold" if (len(self._font) > 2 and "bold" in str(self._font[2])) else "normal"))
        tw = f.measure(self._text) + 2 * self._padx
        if width is not None:
            tw = max(tw, width)
        th = f.metrics("linespace") + 2 * self._pady
        self.configure(width=tw, height=th)

    def _palette(self):
        v = self._variant
        if not self._enabled:
            return COLORS["panel"], COLORS["muted"]
        if v == "accent":
            base_bg = COLORS["accent"]; base_fg = COLORS["bg"]
            hover_bg = COLORS["accent_d"]; hover_fg = COLORS["bg"]
            press_bg = COLORS["accent_d"]; press_fg = COLORS["bg"]
        elif v == "tool":
            base_bg = COLORS["panel"]; base_fg = COLORS["text"]
            hover_bg = COLORS["panel2"]; hover_fg = COLORS["accent"]
            press_bg = COLORS["accent_d"]; press_fg = COLORS["bg"]
        else:  # default
            base_bg = COLORS["panel2"]; base_fg = COLORS["text"]
            hover_bg = COLORS["border"]; hover_fg = COLORS["accent"]
            press_bg = COLORS["accent_d"]; press_fg = COLORS["bg"]
        if self._state == "hover":
            return hover_bg, hover_fg
        if self._state == "pressed":
            return press_bg, press_fg
        return base_bg, base_fg

    def _draw(self):
        self.delete("all")
        w = int(self.winfo_width() or self.winfo_reqwidth())
        h = int(self.winfo_height() or self.winfo_reqheight())
        if w < 4 or h < 4:
            return
        bg, fg = self._palette()
        # match parent bg around rounded shape
        try:
            self.configure(bg=self.master.cget("bg"))
        except tk.TclError:
            pass
        self.create_polygon(_rounded_rect_points(w, h, self._radius),
                            smooth=True, fill=bg, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e):
        if not self._enabled: return
        self._state = "hover"; self._draw()

    def _on_leave(self, _e):
        if not self._enabled: return
        self._state = "normal"; self._draw()

    def _on_press(self, _e):
        if not self._enabled: return
        self._state = "pressed"; self._draw()

    def _on_release(self, e):
        if not self._enabled: return
        # detect if release was inside the widget
        x, y = e.x, e.y
        inside = 0 <= x <= self.winfo_width() and 0 <= y <= self.winfo_height()
        self._state = "hover" if inside else "normal"
        self._draw()
        if inside and self._command:
            try:
                self._command()
            except Exception as ex:
                print(f"button cmd error: {ex}", file=sys.stderr)

    # ttk-like API
    def configure(self, **kw):
        if "state" in kw:
            self.set_state(kw.pop("state"))
        if "text" in kw:
            self._text = kw.pop("text")
            self._sync_size(None)
            self._draw()
        if kw:
            super().configure(**kw)

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        return super().cget(key)

    def set_state(self, state: str):
        self._enabled = (state == "normal")
        self._state = "normal" if self._enabled else "disabled"
        self._draw()


class FlowFrame(tk.Frame):
    """Контейнер с flex-wrap layout: дочерние виджеты переносятся на новую строку
    когда не помещаются по ширине. Аналог `display: flex; flex-wrap: wrap;` в CSS."""

    def __init__(self, parent, hgap: int = 6, vgap: int = 6, **kw):
        super().__init__(parent, **kw)
        self._hgap = hgap
        self._vgap = vgap
        self._items = []
        self.bind("<Configure>", lambda _e: self._reflow())

    def add(self, widget):
        self._items.append(widget)
        widget.place(in_=self, x=0, y=0)  # initial pos; _reflow корректирует
        self.after_idle(self._reflow)

    def _reflow(self):
        width = self.winfo_width()
        if width <= 1:
            self.after(50, self._reflow)
            return
        x = 0
        y = 0
        row_h = 0
        for w in self._items:
            try:
                w.update_idletasks()
                bw = w.winfo_reqwidth() or w.winfo_width()
                bh = w.winfo_reqheight() or w.winfo_height()
            except tk.TclError:
                continue
            if x + bw > width and x > 0:
                x = 0
                y += row_h + self._vgap
                row_h = 0
            w.place(in_=self, x=x, y=y)
            x += bw + self._hgap
            row_h = max(row_h, bh)
        new_height = y + row_h + 2
        if abs(self.winfo_height() - new_height) > 2:
            self.configure(height=new_height)


class RoundedCard(tk.Frame):
    """Frame with rounded-rect background drawn behind. Body is inset so the
    rounded corners actually show — otherwise the rectangular body Frame would
    cover them entirely with the same panel colour."""

    _scale_hint = 1.0  # set externally before construction in App.__init__

    def __init__(self, parent, *, radius: int = 14, fill: str | None = None, **kw):
        try:
            parent_bg = parent.cget("bg")
        except tk.TclError:
            parent_bg = COLORS["bg"]
        super().__init__(parent, bg=parent_bg, **kw)
        # Scale radius for hi-DPI so corners look proportionate
        self._radius = int(radius * RoundedCard._scale_hint)
        self._fill = fill or COLORS["panel"]
        # Canvas behind everything, drawn with smooth rounded rect
        self._canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self._canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Body inset by radius so the rounded corners of the canvas
        # are visible at the card's outer edges.
        inset = max(2, int(self._radius * 0.5))
        self.body = tk.Frame(self, bg=self._fill)
        self.body.pack(fill="both", expand=True, padx=inset, pady=inset)
        self.bind("<Configure>", lambda _e: self._draw())

    def _draw(self):
        self._canvas.delete("all")
        w = int(self.winfo_width()); h = int(self.winfo_height())
        if w < 4 or h < 4:
            return
        try:
            self._canvas.configure(bg=self.master.cget("bg"))
        except tk.TclError:
            pass
        self._canvas.create_polygon(_rounded_rect_points(w, h, self._radius),
                                    smooth=True, fill=self._fill, outline="")


# ---------- main GUI ----------

APP_NAME = "ZubriTunnel"
SETTINGS_FILE = SCRIPT_DIR / "settings.json"

DARK_COLORS = {
    "bg":       "#161616",
    "panel":    "#1E1E1E",
    "panel2":   "#2A2A2A",
    "border":   "#3A3A3A",
    "text":     "#E8E8E8",
    "muted":    "#9A9A9A",
    "accent":   "#26C6DA",
    "accent_d": "#00ACC1",
    "ok":       "#26C6DA",
    "warn":     "#F2C94C",
    "err":      "#EF6C6C",
    "select":   "#2C3D42",
}

LIGHT_COLORS = {
    "bg":       "#F2F2F2",
    "panel":    "#FFFFFF",
    "panel2":   "#E8E8E8",
    "border":   "#D0D0D0",
    "text":     "#1A1A1A",
    "muted":    "#666666",
    "accent":   "#00838F",
    "accent_d": "#005662",
    "ok":       "#00838F",
    "warn":     "#B58100",
    "err":      "#C62828",
    "select":   "#CFEEF2",
}

# Active palette — set by apply_theme()
COLORS = dict(DARK_COLORS)


def detect_system_theme() -> str:
    """Return 'dark' or 'light' based on the OS preference."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as k:
                v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                return "light" if v == 1 else "dark"
        except Exception:
            return "dark"
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            return "dark" if r.stdout.strip().lower() == "dark" else "light"
        except Exception:
            return "light"
    return "dark"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def apply_dark_titlebar(root: tk.Tk, dark: bool):
    """Tell Windows DWM to draw the title bar in dark mode."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # Windows 10 1809+ / Windows 11
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            wintypes.DWORD(DWMWA_USE_IMMERSIVE_DARK_MODE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        # Trigger title bar repaint via tiny resize
        w, h = root.winfo_width(), root.winfo_height()
        if w > 0 and h > 0:
            root.geometry(f"{w}x{h+1}")
            root.geometry(f"{w}x{h}")
    except Exception:
        pass

UI_FONT = ("Segoe UI", 10) if os.name == "nt" else ("SF Pro Text", 12)
UI_FONT_BOLD = ("Segoe UI Semibold", 10) if os.name == "nt" else ("SF Pro Display", 12, "bold")
UI_FONT_TITLE = ("Segoe UI Semibold", 13) if os.name == "nt" else ("SF Pro Display", 14, "bold")
UI_FONT_MONO = ("Cascadia Mono", 9) if os.name == "nt" else ("SF Mono", 10)


def resolve_theme(setting: str) -> str:
    """Translate 'system' into actual 'dark' or 'light'."""
    s = (setting or "system").lower()
    if s == "system":
        return detect_system_theme()
    if s in ("dark", "light"):
        return s
    return detect_system_theme()


def apply_theme(root: tk.Tk, mode: str = "system"):
    """Apply visual theme to root and children. mode: 'system' | 'dark' | 'light'."""
    actual = resolve_theme(mode)
    palette = DARK_COLORS if actual == "dark" else LIGHT_COLORS
    COLORS.clear()
    COLORS.update(palette)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=COLORS["bg"])

    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=UI_FONT, borderwidth=0)
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=UI_FONT)
    style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=UI_FONT_TITLE)
    style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"], font=UI_FONT)
    style.configure("Card.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("CardMuted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])

    # Buttons: subtle filled; accent variant for primary action
    style.configure("TButton",
                    background=COLORS["panel2"], foreground=COLORS["text"],
                    padding=(12, 6), borderwidth=0, font=UI_FONT, focusthickness=0,
                    relief="flat")
    style.map("TButton",
              background=[("pressed", COLORS["accent_d"]),
                          ("active", COLORS["border"]),
                          ("disabled", COLORS["panel"])],
              foreground=[("pressed", COLORS["bg"]),
                          ("active", COLORS["accent"]),
                          ("disabled", COLORS["muted"])])

    style.configure("Accent.TButton",
                    background=COLORS["accent"], foreground=COLORS["bg"],
                    padding=(14, 7), font=UI_FONT_BOLD,
                    borderwidth=0, focusthickness=0, relief="flat")
    style.map("Accent.TButton",
              background=[("pressed", COLORS["accent_d"]),
                          ("active", COLORS["accent_d"]),
                          ("disabled", COLORS["panel"])],
              foreground=[("disabled", COLORS["muted"])])

    style.configure("Tool.TButton",
                    background=COLORS["panel"], foreground=COLORS["text"],
                    padding=(8, 4), font=UI_FONT,
                    borderwidth=0, focusthickness=0, relief="flat")
    style.map("Tool.TButton",
              background=[("pressed", COLORS["accent_d"]),
                          ("active", COLORS["panel2"]),
                          ("disabled", COLORS["panel"])],
              foreground=[("pressed", COLORS["bg"]),
                          ("active", COLORS["accent"]),
                          ("disabled", COLORS["muted"])])

    # Treeview — rowheight needs to scale with DPI so text doesn't get clipped
    scale = getattr(root, "_dpi_scale", 1.0)
    style.configure("Treeview",
                    background=COLORS["panel"], fieldbackground=COLORS["panel"],
                    foreground=COLORS["text"], rowheight=int(28 * scale),
                    borderwidth=0, font=UI_FONT)
    style.configure("Treeview.Heading",
                    background=COLORS["panel2"], foreground=COLORS["muted"],
                    relief="flat", padding=(int(8 * scale), int(6 * scale)),
                    font=UI_FONT_BOLD)
    style.map("Treeview",
              background=[("selected", COLORS["select"])],
              foreground=[("selected", COLORS["accent"])])
    style.map("Treeview.Heading", background=[("active", COLORS["panel2"])])

    # Radiobutton
    style.configure("TRadiobutton",
                    background=COLORS["bg"], foreground=COLORS["text"],
                    indicatorcolor=COLORS["panel2"], focusthickness=0,
                    font=UI_FONT, padding=2)
    style.map("TRadiobutton",
              background=[("active", COLORS["bg"]), ("focus", COLORS["bg"])],
              foreground=[("active", COLORS["accent"]),
                          ("selected", COLORS["accent"]),
                          ("disabled", COLORS["muted"])],
              indicatorcolor=[("selected !disabled", COLORS["accent"]),
                              ("active selected", COLORS["accent_d"]),
                              ("active !selected", COLORS["border"]),
                              ("!selected", COLORS["panel2"])])

    # Checkbutton (for completeness)
    style.configure("TCheckbutton",
                    background=COLORS["bg"], foreground=COLORS["text"],
                    indicatorcolor=COLORS["panel2"], focusthickness=0,
                    font=UI_FONT, padding=2)
    style.map("TCheckbutton",
              background=[("active", COLORS["bg"])],
              foreground=[("active", COLORS["accent"]),
                          ("selected", COLORS["accent"]),
                          ("disabled", COLORS["muted"])],
              indicatorcolor=[("selected", COLORS["accent"]),
                              ("active selected", COLORS["accent_d"]),
                              ("!selected", COLORS["panel2"])])

    # LabelFrame
    style.configure("TLabelframe", background=COLORS["bg"], borderwidth=0)
    style.configure("TLabelframe.Label", background=COLORS["bg"], foreground=COLORS["muted"],
                    font=UI_FONT_BOLD)

    # Combobox (for future use)
    style.configure("TCombobox",
                    fieldbackground=COLORS["panel"], background=COLORS["panel2"],
                    foreground=COLORS["text"], arrowcolor=COLORS["muted"],
                    bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
    style.map("TCombobox",
              fieldbackground=[("readonly", COLORS["panel"])],
              foreground=[("readonly", COLORS["text"])],
              bordercolor=[("focus", COLORS["accent"])])

    # Entry
    style.configure("TEntry",
                    fieldbackground=COLORS["panel"], foreground=COLORS["text"],
                    insertcolor=COLORS["accent"], borderwidth=1, bordercolor=COLORS["border"],
                    lightcolor=COLORS["border"], darkcolor=COLORS["border"], padding=4)
    style.map("TEntry", bordercolor=[("focus", COLORS["accent"])])

    # Scrollbar — theme-coloured, no top/bottom arrows
    style.layout("Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {
            "sticky": "ns",
            "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ],
        }),
    ])
    style.layout("Horizontal.TScrollbar", [
        ("Horizontal.Scrollbar.trough", {
            "sticky": "we",
            "children": [
                ("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ],
        }),
    ])
    style.configure("Vertical.TScrollbar",
                    background=COLORS["panel2"],
                    troughcolor=COLORS["bg"],
                    bordercolor=COLORS["bg"],
                    lightcolor=COLORS["panel2"],
                    darkcolor=COLORS["panel2"],
                    relief="flat", borderwidth=0, arrowsize=0, gripcount=0)
    style.map("Vertical.TScrollbar",
              background=[("active", COLORS["accent"]),
                          ("pressed", COLORS["accent_d"])])
    style.configure("Horizontal.TScrollbar",
                    background=COLORS["panel2"],
                    troughcolor=COLORS["bg"],
                    bordercolor=COLORS["bg"],
                    lightcolor=COLORS["panel2"],
                    darkcolor=COLORS["panel2"],
                    relief="flat", borderwidth=0, arrowsize=0)
    style.map("Horizontal.TScrollbar",
              background=[("active", COLORS["accent"]),
                          ("pressed", COLORS["accent_d"])])

    apply_dark_titlebar(root, actual == "dark")
    return actual


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        # Compute DPI scale and resize fonts/window accordingly
        self._dpi_scale = self._detect_dpi_scale()
        try:
            self.tk.call("tk", "scaling", self._dpi_scale * 1.333)
        except Exception:
            pass
        # Tell RoundedCard / RoundButton to scale radius / paddings to DPI
        RoundedCard._scale_hint = self._dpi_scale
        w = int(860 * self._dpi_scale)
        h = int(700 * self._dpi_scale)
        self.geometry(f"{w}x{h}")
        # minimum width so layout doesn't break; height is auto-fitted to content
        self.minsize(int(760 * self._dpi_scale), int(400 * self._dpi_scale))

        self.settings = load_settings()
        self.theme_mode = self.settings.get("theme", "system")  # 'system' | 'dark' | 'light'
        self.actual_theme = apply_theme(self, self.theme_mode)
        self._set_window_icon()

        self.proxies: dict[str, dict] = {}
        self.log_lock = threading.Lock()
        # Windows Job Object — auto-kills vpn-proxy subprocesses when ZubriTunnel exits
        self.win_job = WindowsJobObject()

        self._build_ui()
        self.refresh_keys()
        self.after(500, self._poll_procs)
        # Re-apply dark titlebar after window is fully realised
        self.after(50, lambda: apply_dark_titlebar(self, self.actual_theme == "dark"))
        # Shrink window to content's natural height (no big empty space below)
        self.after(80, self._fit_window_to_content)

    def _fit_window_to_content(self):
        try:
            self.update_idletasks()
            cur_w = self.winfo_width()
            # Find the scroll-inner content height
            inner_h = 0
            scroll = getattr(self, "_scroll_canvas", None)
            if scroll is not None:
                bbox = scroll.bbox("all")
                if bbox:
                    inner_h = bbox[3] - bbox[1]
            # Fallback to full reqheight
            if inner_h <= 0:
                inner_h = self.winfo_reqheight()
            # Add a small margin for title bar / chrome (Tk doesn't include it)
            chrome = int(40 * self._dpi_scale)
            screen_h = self.winfo_screenheight()
            new_h = min(inner_h + chrome, int(screen_h * 0.9))
            if new_h > 100:
                self.geometry(f"{cur_w}x{new_h}")
        except Exception:
            pass

    def _scaled_geom(self, w: int, h: int) -> str:
        """DPI-scaled geometry string for Toplevel dialogs. Без него на 2x экране
        диалоги получают физические 520×180 пикселей, в которые не влезает контент."""
        s = getattr(self, "_dpi_scale", 1.0)
        return f"{int(w * s)}x{int(h * s)}"

    def _style_toplevel(self, win):
        """Apply dark/light theme + dark title bar to a Toplevel popup.
        Without this Tkinter creates Toplevels with the system default
        (usually light gray) bg, and DwmSetWindowAttribute is never called
        for the popup's title bar — leaves a white bar on top of dark body."""
        try:
            win.configure(bg=COLORS["bg"])
        except Exception:
            pass
        win.after(50, lambda: apply_dark_titlebar(
            win, getattr(self, "actual_theme", "dark") == "dark"))

    def _detect_dpi_scale(self) -> float:
        """Return DPI scale factor (1.0 = 96 DPI = 100%)."""
        if os.name == "nt":
            try:
                import ctypes
                hdc = ctypes.windll.user32.GetDC(0)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
                ctypes.windll.user32.ReleaseDC(0, hdc)
                if dpi > 0:
                    return dpi / 96.0
            except Exception:
                pass
        if sys.platform == "darwin":
            # macOS uses Retina 2x; Tk handles this automatically with proper awareness
            return 1.0
        return 1.0

    def _set_window_icon(self):
        ico = bundled_resource("icon.ico")
        png = bundled_resource("icon.png")
        try:
            if IS_WIN and ico.exists():
                self.iconbitmap(default=str(ico))
                self.iconbitmap(str(ico))
            if png.exists():
                img = tk.PhotoImage(file=str(png))
                self.iconphoto(True, img)
                self._icon_ref = img
        except Exception:
            pass

    # ---- UI ----

    def _build_ui(self):
        # log buffer (lines), plus reference to popup window if open
        self._log_buffer: list[str] = []
        self._log_window = None

        self.configure(bg=COLORS["bg"])

        # Scrollable container so content stays accessible if window is small
        scroll_host = tk.Frame(self, bg=COLORS["bg"])
        scroll_host.pack(fill="both", expand=True)
        scroll_canvas = tk.Canvas(scroll_host, bg=COLORS["bg"],
                                  highlightthickness=0, bd=0)
        self._scroll_canvas = scroll_canvas
        vbar = ttk.Scrollbar(scroll_host, orient="vertical", command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=vbar.set)
        scroll_canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        # Direct content frame inside the canvas — no extra padding wrapper to avoid
        # Tk's circular sizing (canvas sizes to inner, inner sizes to outer with expand=True).
        outer = tk.Frame(scroll_canvas, bg=COLORS["bg"], padx=18, pady=16)
        scroll_inner_id = scroll_canvas.create_window((0, 0), window=outer, anchor="nw")

        def _on_canvas_configure(event):
            scroll_canvas.itemconfigure(scroll_inner_id, width=event.width)

        def _on_inner_configure(_event):
            bbox = scroll_canvas.bbox("all")
            if bbox:
                scroll_canvas.configure(scrollregion=bbox)
                # Hide the scrollbar when content fits — keeps the look clean
                inner_h = bbox[3] - bbox[1]
                canvas_h = scroll_canvas.winfo_height()
                if inner_h <= canvas_h:
                    if vbar.winfo_ismapped():
                        vbar.pack_forget()
                else:
                    if not vbar.winfo_ismapped():
                        vbar.pack(side="right", fill="y")

        scroll_canvas.bind("<Configure>", _on_canvas_configure)
        outer.bind("<Configure>", _on_inner_configure)

        def _on_mousewheel(event):
            # Only scroll if there's something to scroll
            bbox = scroll_canvas.bbox("all")
            if not bbox:
                return
            if (bbox[3] - bbox[1]) <= scroll_canvas.winfo_height():
                return
            delta = -1 if (getattr(event, "num", 0) == 5 or event.delta < 0) else 1
            scroll_canvas.yview_scroll(delta * 3, "units")
        self.bind_all("<MouseWheel>", _on_mousewheel)
        self.bind_all("<Button-4>", _on_mousewheel)
        self.bind_all("<Button-5>", _on_mousewheel)

        # Force view to top after initial layout
        self.after(10, lambda: scroll_canvas.yview_moveto(0))

        # Top: brand + theme switch
        head = tk.Frame(outer, bg=COLORS["bg"])
        head.pack(fill="x", pady=(0, 14))
        tk.Label(head, text=APP_NAME, bg=COLORS["bg"], fg=COLORS["text"],
                 font=UI_FONT_TITLE).pack(side="left")
        tk.Label(head, text="• точечный VPN для приложений", bg=COLORS["bg"],
                 fg=COLORS["muted"], font=UI_FONT).pack(side="left", padx=8)
        self.theme_var = tk.StringVar(value=self.theme_mode)
        theme_box = tk.Frame(head, bg=COLORS["bg"])
        theme_box.pack(side="right")
        tk.Label(theme_box, text="тема:", bg=COLORS["bg"], fg=COLORS["muted"],
                 font=UI_FONT).pack(side="left", padx=(0, 6))
        for code, label in [("system", "системная"), ("dark", "тёмная"), ("light", "светлая")]:
            ttk.Radiobutton(theme_box, text=label, value=code, variable=self.theme_var,
                            command=self._on_theme_change).pack(side="left", padx=4)
        # Кнопка проверки зависимостей
        RoundButton(head, text="⚙ проверка системы", variant="tool",
                    command=self.show_deps_dialog).pack(side="right", padx=(0, 14))

        # Status card
        bar = RoundedCard(outer, radius=18, fill=COLORS["panel"])
        bar.pack(fill="x", pady=(0, 12))
        bar.body.configure(bg=COLORS["panel"])
        bar_pad = tk.Frame(bar.body, bg=COLORS["panel"])
        bar_pad.pack(fill="x", padx=18, pady=14)
        self.status_dot = tk.Label(bar_pad, text="●", fg=COLORS["muted"], bg=COLORS["panel"],
                                   font=(UI_FONT[0], 16))
        self.status_dot.pack(side="left")
        self.status_text = tk.Label(bar_pad, text="выбери ключ", anchor="w",
                                    bg=COLORS["panel"], fg=COLORS["text"], font=UI_FONT)
        self.status_text.pack(side="left", padx=10, fill="x", expand=True)

        # Keys card
        kf = self._rounded_card(outer, title="Ключи")
        kf.pack(fill="both", expand=False, pady=(0, 12))

        cols = ("status", "name", "tag", "server", "port")
        self.tree = ttk.Treeview(kf.content, columns=cols, show="headings", height=4, selectmode="browse")
        scale = self._dpi_scale
        # widths are tuned so wide text like "○ не подключён" fits comfortably
        for c, label, w in [("status", "состояние", 220), ("name", "имя", 160), ("tag", "регион", 200),
                            ("server", "сервер", 240), ("port", "порт", 90)]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=int(w * scale), stretch=(c == "server"), anchor="w")
        self.tree.pack(fill="x", pady=(0, 10))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select_key())
        self.tree.bind("<Double-1>", lambda _e: self.show_apps_dialog())

        self.empty_hint = tk.Label(
            kf.content,
            text="Ключей пока нет.  Нажми «+ ssconf://» и вставь ссылку — добавится автоматически.",
            bg=COLORS["panel"], fg=COLORS["muted"], font=UI_FONT,
            wraplength=720, justify="left",
        )

        kbar = tk.Frame(kf.content, bg=COLORS["panel"])
        kbar.pack(fill="x")
        for text, cmd in [
            ("+ ssconf://", self.add_ssconf),
            ("+ JSON", self.add_json),
            ("+ файл…", self.add_file),
            ("клонировать", self.clone_key),
            ("сменить регион", self.change_region),
        ]:
            RoundButton(kbar, text=text, variant="tool", command=cmd).pack(side="left", padx=(0, 6))
        RoundButton(kbar, text="↻", variant="tool", command=self.refresh_keys).pack(side="left", padx=(0, 6))
        RoundButton(kbar, text="удалить", variant="tool", command=self.delete_key).pack(side="right", padx=(6, 0))
        RoundButton(kbar, text="открыть keys/", variant="tool", command=self.open_keys_dir).pack(side="right")

        # Proxy card
        cf = self._rounded_card(outer, title="Прокси")
        cf.pack(fill="x", pady=(0, 12))
        cbar = tk.Frame(cf.content, bg=COLORS["panel"])
        cbar.pack(fill="x")
        self.btn_connect = RoundButton(cbar, text="Подключить", variant="accent", command=self.connect)
        self.btn_connect.pack(side="left", padx=(0, 6))
        self.btn_disconnect = RoundButton(cbar, text="Отключить", variant="default", command=self.disconnect)
        self.btn_disconnect.pack(side="left", padx=(0, 6))
        self.btn_disconnect.set_state("disabled")
        RoundButton(cbar, text="Тест ключа", variant="default", command=self.test_key).pack(side="left", padx=(0, 6))
        RoundButton(cbar, text="Проверить IP", variant="default", command=self.check_ip).pack(side="left", padx=(0, 6))
        RoundButton(cbar, text="Запущенные приложения", variant="default", command=self.show_apps_dialog).pack(side="right")

        # Launch card
        lf = self._rounded_card(outer, title="Запустить через прокси выделенного ключа")
        lf.pack(fill="x", pady=(0, 12))
        self.launch_frame = tk.Frame(lf.content, bg=COLORS["panel"])
        self.launch_frame.pack(fill="x")
        self._build_launch_buttons()

        # Log card — compact footer; full log opens in a separate window
        log_card = self._rounded_card(outer)
        log_card.pack(fill="x", pady=(0, 0))
        log_row = tk.Frame(log_card.content, bg=COLORS["panel"])
        log_row.pack(fill="x")
        tk.Label(log_row, text="Лог", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=UI_FONT_BOLD).pack(side="left")
        self._log_count_label = tk.Label(log_row, text="0 строк", bg=COLORS["panel"],
                                         fg=COLORS["muted"], font=UI_FONT)
        self._log_count_label.pack(side="left", padx=10)
        RoundButton(log_row, text="Открыть в окне", variant="tool",
                    command=self.open_log_window).pack(side="right")

    def _rounded_card(self, parent, title: str | None = None) -> RoundedCard:
        card = RoundedCard(parent, radius=18, fill=COLORS["panel"])
        card.body.configure(bg=COLORS["panel"])
        if title:
            head = tk.Frame(card.body, bg=COLORS["panel"])
            head.pack(fill="x", padx=18, pady=(14, 4))
            tk.Label(head, text=title, bg=COLORS["panel"], fg=COLORS["muted"],
                     font=UI_FONT_BOLD).pack(side="left")
        content = tk.Frame(card.body, bg=COLORS["panel"])
        content.pack(fill="both", expand=True, padx=18, pady=(4, 14))
        card.content = content  # type: ignore[attr-defined]
        return card

    def _build_launch_buttons(self):
        for w in self.launch_frame.winfo_children():
            w.destroy()
        # FlowFrame — автоперенос кнопок когда окно узкое (как flex-wrap)
        flow = FlowFrame(self.launch_frame, bg=COLORS["panel"], hgap=6, vgap=6)
        flow.pack(fill="x", expand=True)
        for name, cmd in detect_apps():
            btn = RoundButton(flow, text=name, variant="tool",
                              command=lambda c=cmd, n=name: self.launch_app(n, c))
            flow.add(btn)
        flow.add(RoundButton(flow, text="Custom…", variant="tool", command=self.launch_custom))
        # Action buttons (git, IDE terminals) — тоже в flow, но они есть всегда
        flow.add(RoundButton(flow, text="git proxy on", variant="tool",
                             command=lambda: self.toggle_git_proxy(True)))
        flow.add(RoundButton(flow, text="git proxy off", variant="tool",
                             command=lambda: self.toggle_git_proxy(False)))
        flow.add(RoundButton(flow, text="IDE terminals on", variant="tool",
                             command=lambda: self.toggle_ide_terminals(True)))
        flow.add(RoundButton(flow, text="IDE terminals off", variant="tool",
                             command=lambda: self.toggle_ide_terminals(False)))
        flow.add(RoundButton(flow, text="системный VPN on", variant="accent",
                             command=lambda: self.toggle_system_proxy(True)))
        flow.add(RoundButton(flow, text="системный VPN off", variant="tool",
                             command=lambda: self.toggle_system_proxy(False)))

    # ---- key management ----

    def refresh_keys(self):
        prev = self.tree.selection()
        prev_id = prev[0] if prev else None
        self.tree.delete(*self.tree.get_children())
        keys = list_keys()
        for k in keys:
            p = self.proxies.get(k["name"])
            if p and p["proc"].poll() is None:
                status = f"●  :{p['addr'].split(':')[1]}"
            else:
                status = "○ не подключён"
            self.tree.insert("", "end", iid=k["name"], values=(status, k["name"], k["tag"], k["server"], k["port"]))
        if keys:
            if prev_id and self.tree.exists(prev_id):
                self.tree.selection_set(prev_id)
            else:
                self.tree.selection_set(self.tree.get_children()[0])
            self.empty_hint.pack_forget()
        else:
            self.empty_hint.pack(fill="x", padx=8, pady=4, before=self.tree)
        self._update_status_display()

    def selected_key(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for k in list_keys():
            if k["name"] == sel[0]:
                return k
        return None

    def _save_key(self, data: dict, default_name: str) -> bool:
        name = simpledialog.askstring("Имя ключа", "Сохранить как (без .json):", initialvalue=slugify(default_name), parent=self)
        if not name:
            return False
        name = slugify(name)
        path = KEYS_DIR / f"{name}.json"
        if path.exists():
            if not messagebox.askyesno("Перезаписать?", f"Файл {path.name} уже есть. Заменить?"):
                return False
        KEYS_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log_msg(f"saved key → {path}")
        self.refresh_keys()
        try:
            self.tree.selection_set(name)
        except Exception:
            pass
        return True

    def add_ssconf(self):
        url = self._ask_string_with_paste(
            "Добавить ssconf://",
            "Вставь ссылку ssconf:// от провайдера VPN.\n"
            "Можно Ctrl+V или нажать кнопку «Вставить из буфера».",
        )
        if not url:
            return
        url = url.strip()
        try:
            data = fetch_ssconf(url)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось скачать: {e}")
            return
        data["_ssconf_url"] = url
        default = data.get("tag") or url.rstrip("/").split("/")[-1] or "ssconf"
        self._save_key(data, default)

    def _ask_string_with_paste(self, title: str, prompt: str, initial: str = "") -> str | None:
        """Custom replacement for simpledialog.askstring with a paste button.
        simpledialog.askstring sometimes hangs on Ctrl+V in Windows due to
        clipboard format negotiation (HTML/RTF in clipboard); this version
        reads clipboard content directly via Tk and offers a button.
        """
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.geometry(self._scaled_geom(520, 180))
        self._style_toplevel(win)
        win.resizable(True, False)

        ttk.Label(win, text=prompt, justify="left", wraplength=480).pack(fill="x", padx=12, pady=(12, 6))

        var = tk.StringVar(value=initial)
        entry = ttk.Entry(win, textvariable=var, width=80)
        entry.pack(fill="x", padx=12, pady=4)
        entry.focus_set()

        result = {"value": None}

        def do_paste():
            try:
                # Read clipboard via Tk (avoids tkinter's slow Ctrl+V path on some systems)
                clip = self.clipboard_get()
                var.set(clip.strip())
            except tk.TclError:
                messagebox.showinfo("Буфер обмена", "В буфере нет текста.", parent=win)

        def ok():
            result["value"] = var.get()
            win.destroy()

        def cancel():
            win.destroy()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="Вставить из буфера", command=do_paste).pack(side="left")
        ttk.Button(bar, text="OK", command=ok).pack(side="right", padx=(4, 0))
        ttk.Button(bar, text="Cancel", command=cancel).pack(side="right")

        entry.bind("<Return>", lambda _e: ok())
        win.bind("<Escape>", lambda _e: cancel())

        win.wait_window()
        return result["value"]

    def add_json(self):
        win = tk.Toplevel(self)
        win.title("Вставь JSON ключа")
        win.geometry(self._scaled_geom(520, 320))
        self._style_toplevel(win)
        txt = scrolledtext.ScrolledText(win, font=("Courier", 9))
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("1.0", '{\n  "method": "chacha20-ietf-poly1305",\n  "password": "...",\n  "server": "1.2.3.4",\n  "server_port": 443,\n  "tag": "Country"\n}\n')
        def save():
            try:
                data = parse_json_text(txt.get("1.0", "end"))
            except Exception as e:
                messagebox.showerror("Ошибка", f"JSON не парсится: {e}")
                return
            win.destroy()
            self._save_key(data, data.get("tag") or "key")
        ttk.Button(win, text="Сохранить", command=save).pack(pady=6)

    def add_file(self):
        f = filedialog.askopenfilename(title="JSON-ключ", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not f:
            return
        try:
            raw = Path(f).read_bytes()
            data = json.loads(sanitize_json_bytes(raw).decode("utf-8", errors="replace"))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не парсится: {e}")
            return
        self._save_key(data, data.get("tag") or Path(f).stem)

    def clone_key(self):
        k = self.selected_key()
        if not k:
            messagebox.showwarning("Выбери ключ", "Сначала выбери ключ для клонирования.")
            return
        # generate next free name: name-2, name-3 ...
        base = k["name"]
        i = 2
        while True:
            candidate = f"{base}-{i}"
            if not (KEYS_DIR / f"{candidate}.json").exists():
                break
            i += 1
        try:
            data = json.loads(sanitize_json_bytes(k["path"].read_bytes()).decode("utf-8", errors="replace"))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не парсится исходный ключ: {e}")
            return
        new_path = KEYS_DIR / f"{candidate}.json"
        new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log_msg(f"клонирован {base} → {candidate}")
        self.refresh_keys()
        self._reselect(candidate)
        messagebox.showinfo(
            "Клонирован",
            f"Создан {candidate}.json — копия {base}.\n\n"
            "Если хочешь поставить на него другой регион — выдели его в списке и нажми «сменить регион»."
        )

    def show_apps_dialog(self):
        k = self.selected_key()
        if not k:
            messagebox.showinfo("Выбери ключ", "Сначала выдели ключ.")
            return
        win = tk.Toplevel(self)
        win.title(f"Приложения через «{k['name']}»")
        win.geometry(self._scaled_geom(560, 360))
        self._style_toplevel(win)
        win.transient(self)
        apply_theme(win)
        win.configure(bg=COLORS["bg"])

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        p = self.proxies.get(k["name"])
        if p and p["proc"].poll() is None:
            ttk.Label(outer, text=f"{k['tag'] or k['name']}  ·  http://{p['addr']}",
                      style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        else:
            ttk.Label(outer, text=f"{k['tag'] or k['name']}", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
            ttk.Label(outer, text="Прокси не запущен", style="Muted.TLabel").pack(anchor="w")

        cols = ("when", "name", "pid", "alive")
        tree = ttk.Treeview(outer, columns=cols, show="headings", height=10, selectmode="browse")
        for c, label, w, anchor in [("when", "запущено", 110, "w"), ("name", "приложение", 200, "w"),
                                    ("pid", "PID", 80, "e"), ("alive", "статус", 110, "w")]:
            tree.heading(c, text=label)
            tree.column(c, width=w, anchor=anchor)
        tree.pack(fill="both", expand=True, pady=(8, 8))

        apps = (p or {}).get("apps", []) if p else []
        if not apps:
            ttk.Label(outer, text="Приложений ещё не запускалось через этот прокси.\n"
                                  "Подключи прокси и жми кнопки в панели «Запустить через прокси».",
                      style="Muted.TLabel", justify="left").pack(anchor="w")
        else:
            for entry in apps:
                # Real status: prefer real_pid (resolved via pgrep for `open -na`),
                # fall back to subprocess status.
                real_pid = entry.get("real_pid")
                proc = entry.get("proc")
                alive = "○ завершено"
                if real_pid:
                    try:
                        os.kill(real_pid, 0)
                        alive = "● работает"
                    except (ProcessLookupError, OSError):
                        pass
                elif proc and proc.poll() is None:
                    alive = "● работает"
                elif entry.get("is_open_launch") and entry.get("app_basename"):
                    # pgrep didn't resolve yet — best-effort name probe
                    try:
                        r = subprocess.run(
                            ["pgrep", "-x", entry["app_basename"]],
                            capture_output=True, text=True, timeout=2,
                        )
                        if r.stdout.strip():
                            alive = "● работает"
                    except Exception:
                        pass
                # Show real_pid if we resolved it, otherwise the subprocess pid
                display_pid = real_pid if real_pid else entry.get("pid", "?")
                tree.insert("", "end", values=(entry["time"], entry["name"], display_pid, alive))

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        ttk.Button(bar, text="Обновить", style="Tool.TButton",
                   command=lambda: (win.destroy(), self.show_apps_dialog())).pack(side="left")
        ttk.Button(bar, text="Закрыть", style="Tool.TButton", command=win.destroy).pack(side="right")

    def delete_key(self):
        k = self.selected_key()
        if not k:
            return
        if not messagebox.askyesno("Удалить?", f"Удалить ключ {k['name']}?"):
            return
        try:
            k["path"].unlink()
            self.log_msg(f"deleted {k['path'].name}")
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
        self.refresh_keys()

    def change_region(self):
        k = self.selected_key()
        if not k:
            messagebox.showwarning("Выбери ключ", "Сначала выбери ключ из списка.")
            return
        # read raw JSON to get _ssconf_url
        try:
            raw = json.loads(sanitize_json_bytes(k["path"].read_bytes()).decode("utf-8", errors="replace"))
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не парсится ключ: {e}")
            return
        ssconf_url = raw.get("_ssconf_url", "")
        if not ssconf_url:
            ssconf_url = simpledialog.askstring(
                "Сменить регион",
                f"У ключа {k['name']} не сохранена исходная ssconf://-ссылка.\n"
                "Вставь её сюда (она нужна для запроса списка регионов у провайдера):",
                parent=self,
            )
            if not ssconf_url:
                return
            ssconf_url = ssconf_url.strip()
            raw["_ssconf_url"] = ssconf_url
            k["path"].write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        threading.Thread(target=self._open_region_dialog, args=(k, ssconf_url), daemon=True).start()

    def _open_region_dialog(self, k: dict, ssconf_url: str):
        try:
            locations = fetch_provider_locations(ssconf_url)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Регионы", f"Не удалось получить список:\n{e}"))
            return
        # filter out systemLocation entries
        visible = [loc for loc in locations if not loc.get("systemLocation")]
        if not visible:
            self.after(0, lambda: messagebox.showinfo("Регионы", "Провайдер не вернул ни одного публичного региона."))
            return
        self.after(0, lambda: self._render_region_dialog(k, ssconf_url, visible))

    def _render_region_dialog(self, k: dict, ssconf_url: str, locations: list):
        win = tk.Toplevel(self)
        win.title(f"Сменить регион — {k['name']}")
        win.geometry(self._scaled_geom(520, 420))
        self._style_toplevel(win)
        win.transient(self)

        ttk.Label(win, text=f"Текущий регион: {k['tag']}", anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(win, text="Выбери регион из списка провайдера и нажми «Применить».", foreground="#888").pack(fill="x", padx=10)

        list_frame = ttk.Frame(win)
        list_frame.pack(fill="both", expand=True, padx=10, pady=8)
        scroll = ttk.Scrollbar(list_frame, orient="vertical")
        listbox = tk.Listbox(list_frame, yscrollcommand=scroll.set, font=("TkDefaultFont", 10))
        scroll.config(command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)

        # sort: best first, then by description
        locations.sort(key=lambda l: (not l.get("bestLocation"), l.get("description", "")))
        for i, loc in enumerate(locations):
            label = loc.get("description", "?")
            if loc.get("bestLocation"):
                label = "★ (быстрый)  " + label
            speed = loc.get("speed")
            if speed:
                label += f"   ~{speed} ms"
            listbox.insert("end", label)
            if loc.get("description") == k["tag"]:
                listbox.selection_set(i)
                listbox.see(i)
        if not listbox.curselection() and locations:
            listbox.selection_set(0)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10, pady=(0, 10))

        def apply():
            sel = listbox.curselection()
            if not sel:
                return
            chosen = locations[sel[0]]
            apply_btn.configure(state="disabled", text="меняю…")
            threading.Thread(target=self._apply_region_change, args=(k, ssconf_url, chosen, win, apply_btn), daemon=True).start()

        apply_btn = ttk.Button(bar, text="Применить", command=apply)
        apply_btn.pack(side="left")
        ttk.Button(bar, text="Закрыть", command=win.destroy).pack(side="right")

    def _apply_region_change(self, k: dict, ssconf_url: str, chosen: dict, dialog: tk.Toplevel, btn):
        existing = self.proxies.get(k["name"])
        was_running = bool(existing and existing["proc"].poll() is None)
        try:
            resp = change_provider_location(ssconf_url, chosen["value"])
            self.log_msg(f"{k['name']}: смена региона → {resp.get('tag', chosen.get('description'))}")
            new_data = fetch_ssconf(ssconf_url)
            new_data["_ssconf_url"] = ssconf_url
            k["path"].write_text(json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8")

            def finish():
                self.refresh_keys()
                self._reselect(k["name"])
                dialog.destroy()
                if was_running:
                    self.log_msg(f"{k['name']}: перезапускаю прокси на новом регионе…")
                    self._stop_proxy_for(k["name"])
                    self.after(500, self.connect)
                else:
                    messagebox.showinfo("Регион сменён", f"Теперь: {new_data.get('tag', chosen.get('description'))}")
            self.after(0, finish)
        except Exception as e:
            self.after(0, lambda: (
                btn.configure(state="normal", text="Применить"),
                messagebox.showerror("Ошибка", f"Не получилось сменить регион:\n{e}"),
            ))

    def _reselect(self, name: str):
        try:
            self.tree.selection_set(name)
        except Exception:
            pass

    def _on_theme_change(self):
        mode = self.theme_var.get()
        self.theme_mode = mode
        self.settings["theme"] = mode
        save_settings(self.settings)
        self.actual_theme = apply_theme(self, mode)
        self.log_msg(f"тема: {mode} (фактически {self.actual_theme})")
        # Re-build the entire UI to pick up new palette (cleanest approach)
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        self.refresh_keys()
        self._update_status_display()
        apply_dark_titlebar(self, self.actual_theme == "dark")

    def open_keys_dir(self):
        KEYS_DIR.mkdir(exist_ok=True)
        if IS_WIN:
            os.startfile(str(KEYS_DIR))  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", str(KEYS_DIR)], **_win_subprocess_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(KEYS_DIR)], **_win_subprocess_kwargs())

    # ---- proxy control ----

    def _used_local_ports(self) -> set:
        used = set()
        for v in self.proxies.values():
            try:
                used.add(int(v["addr"].split(":")[1]))
            except (KeyError, ValueError, IndexError):
                pass
        return used

    def _on_select_key(self):
        self._update_status_display()

    def _update_status_display(self):
        sel = self.selected_key()
        if not sel:
            self.status_dot.configure(fg="#888")
            self.status_text.configure(text="выбери ключ")
            return
        p = self.proxies.get(sel["name"])
        if p and p["proc"].poll() is None:
            self.status_dot.configure(fg="#26C6DA")
            self.status_text.configure(text=f"  {sel['name']}: {p['key_tag']} → http://{p['addr']}")
            self.btn_connect.configure(state="disabled")
            self.btn_disconnect.configure(state="normal")
        else:
            self.status_dot.configure(fg="#888")
            running = len([1 for x in self.proxies.values() if x["proc"].poll() is None])
            extra = f"   ·   ещё запущено: {running}" if running else ""
            self.status_text.configure(text=f"  {sel['name']} не подключён{extra}")
            self.btn_connect.configure(state="normal")
            self.btn_disconnect.configure(state="disabled")

    def _start_proxy_subprocess(self, key_name: str, addr: str) -> subprocess.Popen | None:
        cmd = go_command() + ["-key", key_name, "-no-menu", "-addr", addr]
        self.log_msg(f"$ {' '.join(cmd)}")
        try:
            popen_kwargs = dict(
                cwd=str(SCRIPT_DIR),
                env=enhanced_path_env(),  # /opt/homebrew/bin etc. so 'go' & deps are findable
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if IS_WIN:
                # CREATE_NEW_PROCESS_GROUP: needed to send Ctrl+Break for clean shutdown
                # CREATE_NO_WINDOW: suppress the flashing console window on launch
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | WIN_NO_WINDOW  # type: ignore[attr-defined]
                )
            proc = subprocess.Popen(cmd, **popen_kwargs)
            # Bind to job so vpn-proxy is killed when ZubriTunnel exits, even on hard crash
            if IS_WIN:
                self.win_job.assign(proc)
        except FileNotFoundError as e:
            messagebox.showerror(
                "Не получилось запустить vpn-proxy",
                f"{e}\n\n"
                "Возможные причины:\n"
                "  • vpn-proxy бинарник пропал — нажми «⚙ проверка системы» → «Собрать»\n"
                "  • Go SDK отсутствует — там же кнопка «Установить»\n"
                "  • Скачай свежий релиз: github.com/AntonZubritski/ZubriTunnel/releases",
            )
            return None
        threading.Thread(target=self._pump_log, args=(proc,), daemon=True).start()
        self.proxy_addr = addr
        return proc

    def connect(self):
        k = self.selected_key()
        if not k:
            messagebox.showwarning("Выбери ключ", "Сначала выбери ключ из списка.")
            return
        existing = self.proxies.get(k["name"])
        if existing and existing["proc"].poll() is None:
            messagebox.showinfo("Уже подключён", f"{k['name']} уже работает на {existing['addr']}.")
            return
        # find a free port avoiding ports already used by other running proxies
        free = find_free_port("127.0.0.1", 8080, avoid=self._used_local_ports())
        addr = f"127.0.0.1:{free}"
        proc = self._start_proxy_subprocess(k["name"], addr)
        if not proc:
            return
        self.proxies[k["name"]] = {"proc": proc, "addr": addr, "key_tag": k["tag"] or k["name"]}
        self._update_status_display()
        threading.Thread(target=self._wait_listening, args=(proc, k, addr), daemon=True).start()

    def _wait_listening(self, proc: subprocess.Popen, k: dict, addr: str):
        marker = f"listening on http://{addr}"
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                self.after(0, lambda: self._on_proxy_died(k["name"], "exited before listening"))
                return
            if marker in "".join(self._log_buffer):
                self.after(0, lambda: (self.refresh_keys(), self._reselect(k["name"]), self._update_status_display()))
                return
            time.sleep(0.2)
        self.after(0, lambda: self._on_proxy_died(k["name"], "timed out waiting for proxy"))

    def _on_proxy_died(self, key_name: str, reason: str = ""):
        self.proxies.pop(key_name, None)
        if reason:
            self.log_msg(f"{key_name}: {reason}")
        self.refresh_keys()
        self._reselect(key_name)
        self._update_status_display()

    def disconnect(self):
        k = self.selected_key()
        if not k:
            return
        p = self.proxies.get(k["name"])
        if p:
            # Проверим какие приложения через этот прокси ещё живы — иначе
            # пользователь увидит "VPN отключён" но Chrome продолжит ломиться
            # на мёртвый 127.0.0.1:8081 и интернет не работает.
            live_apps = []
            for entry in p.get("apps", []) or []:
                rpid = entry.get("real_pid")
                if rpid:
                    try:
                        os.kill(rpid, 0)
                        live_apps.append(entry)
                    except (ProcessLookupError, OSError):
                        pass
                else:
                    proc = entry.get("proc")
                    if proc and proc.poll() is None:
                        live_apps.append(entry)
            if live_apps:
                names = ", ".join(sorted(set(a["name"] for a in live_apps)))
                if messagebox.askyesno(
                    "Закрыть приложения через этот прокси?",
                    f"Сейчас работают: {names}.\n\n"
                    "Они стартовали с проксированием на этот ключ. После отключения "
                    "они продолжат работать, но будут пытаться ходить через мёртвый "
                    "прокси — интернет в них работать не будет.\n\n"
                    "Закрыть их вместе с отключением?",
                ):
                    for entry in live_apps:
                        rpid = entry.get("real_pid")
                        try:
                            if rpid:
                                import signal as _sig
                                os.kill(rpid, _sig.SIGTERM)
                                self.log_msg(f"закрыл {entry['name']} (PID {rpid})")
                            else:
                                proc = entry.get("proc")
                                if proc and proc.poll() is None:
                                    proc.terminate()
                                    self.log_msg(f"закрыл {entry['name']}")
                        except Exception as e:
                            self.log_msg(f"не закрыл {entry['name']}: {e}")
        self._stop_proxy_for(k["name"])
        self._update_status_display()

    def _stop_proxy_for(self, key_name: str):
        p = self.proxies.pop(key_name, None)
        if not p:
            return
        proc = p["proc"]
        if proc.poll() is None:
            try:
                if IS_WIN:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
        self.refresh_keys()

    def _poll_procs(self):
        died = [name for name, p in self.proxies.items() if p["proc"].poll() is not None]
        for name in died:
            code = self.proxies[name]["proc"].returncode
            self.log_msg(f"{name}: proxy exited (code {code})")
            self.proxies.pop(name, None)
        if died:
            self.refresh_keys()
            self._update_status_display()
        self.after(500, self._poll_procs)

    def test_key(self):
        k = self.selected_key()
        if not k:
            messagebox.showwarning("Выбери ключ", "Сначала выбери ключ.")
            return
        threading.Thread(target=self._test_key_thread, args=(k,), daemon=True).start()

    def _test_key_thread(self, k: dict):
        self.log_msg(f"--- testing {k['name']} ---")
        # use a different port to not collide with running proxy
        addr = "127.0.0.1:18081"
        cmd = go_command() + ["-key", k["name"], "-no-menu", "-addr", addr]
        try:
            popen_kwargs = dict(cwd=str(SCRIPT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True, encoding="utf-8", errors="replace")
            if IS_WIN:
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:
            self.log_msg(f"test failed to start: {e}")
            return
        threading.Thread(target=self._pump_log, args=(proc,), daemon=True).start()
        # wait listening
        deadline = time.time() + 10
        ok = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if "listening" in "".join(self._log_buffer):
                ok = True
                break
            time.sleep(0.2)
        if not ok:
            self.log_msg("test: proxy did not start")
            try:
                proc.terminate()
            except Exception:
                pass
            return
        # try fetching IP
        try:
            t0 = time.time()
            ip = http_get_via_proxy("https://api.ipify.org", f"http://{addr}", timeout=10)
            dt = (time.time() - t0) * 1000
            msg = f"test {k['name']}: OK, exit IP {ip} ({dt:.0f} ms)"
            self.log_msg(msg)
            self.after(0, lambda: messagebox.showinfo("Тест", f"{k['tag'] or k['name']}\nexit IP: {ip}\nlatency: {dt:.0f} ms"))
        except Exception as e:
            self.log_msg(f"test {k['name']}: FAIL ({e})")
            self.after(0, lambda: messagebox.showerror("Тест", f"{k['name']}: {e}"))
        finally:
            try:
                if IS_WIN:
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def check_ip(self):
        sel = self.selected_key()
        active = self.proxies.get(sel["name"]) if sel else None
        if not active or active["proc"].poll() is not None:
            messagebox.showinfo("Не подключено", "Выделенный ключ не подключён.")
            return
        threading.Thread(target=self._check_ip_thread, args=(sel["name"], active["addr"]), daemon=True).start()

    def _check_ip_thread(self, key_name: str, addr: str):
        try:
            ip = http_get_via_proxy("https://api.ipify.org", f"http://{addr}", timeout=10)
            self.log_msg(f"{key_name}: exit IP = {ip}")
            self.after(0, lambda: messagebox.showinfo("Текущий IP", f"{key_name}: {ip}"))
        except Exception as e:
            self.log_msg(f"{key_name}: check_ip failed: {e}")
            self.after(0, lambda: messagebox.showerror("Ошибка", str(e)))

    # ---- launch ----

    def launch_app(self, name: str, cmd: list):
        sel = self.selected_key()
        active = self.proxies.get(sel["name"]) if sel else None
        if not active or active["proc"].poll() is not None:
            # try fallback to ANY running proxy
            running = [(k, v) for k, v in self.proxies.items() if v["proc"].poll() is None]
            if running:
                if not messagebox.askyesno(
                    "Выделенный ключ не подключён",
                    f"У ключа '{sel['name'] if sel else '?'}' нет активного прокси.\n\n"
                    f"Использовать прокси от '{running[0][0]}' ({running[0][1]['key_tag']})?",
                ):
                    return
                active = running[0][1]
            else:
                if not messagebox.askyesno("Прокси не запущен", "Запустить программу без прокси?"):
                    return
                env = os.environ.copy()
                try:
                    subprocess.Popen(cmd, env=env)
                    self.log_msg(f"launched {name} (без прокси)")
                except Exception as e:
                    messagebox.showerror("Ошибка", f"Не удалось запустить {name}: {e}")
                return

        proxy_url = f"http://{active['addr']}"
        env = proxy_env(proxy_url)
        name_lower = name.lower()
        chromium_brands = {"chrome", "edge", "chromium", "brave", "opera", "yandex", "vivaldi", "cursor", "vscode"}
        # Cursor and VSCode are Electron — they USE Chromium. They respect env vars in code,
        # but the bundled browser (webview) inside also benefits from --proxy-server.
        # However VSCode/Cursor reads http.proxy from settings.json and env, so leave them as env-only.
        chromium_browsers = {"chrome", "edge", "chromium", "brave", "opera", "yandex", "vivaldi"}

        try:
            if name_lower in chromium_browsers:
                final_cmd = self._chromium_cmd_with_proxy(cmd, proxy_url, name_lower)
            elif name_lower == "firefox":
                final_cmd = self._firefox_cmd_with_proxy(cmd, proxy_url)
            elif name_lower == "safari":
                messagebox.showinfo("Safari", "Safari использует системные настройки прокси. "
                                   "Открой System Settings → Network → активное подключение → "
                                   "Details → Proxies → HTTPS Proxy → " + proxy_url)
                final_cmd = cmd
            else:
                final_cmd = cmd
            child = subprocess.Popen(final_cmd, env=env)
            # Track this app under the proxy's apps list
            owner_key = None
            for k_name, p in self.proxies.items():
                if p is active:
                    owner_key = k_name
                    break
            is_open_launch = bool(final_cmd) and final_cmd[0] == "open"
            entry = {
                "name": name,
                "pid": child.pid,
                "proc": child,
                "real_pid": None,
                "is_open_launch": is_open_launch,
                "app_basename": None,
                "time": time.strftime("%H:%M:%S"),
            }
            if is_open_launch:
                # Утилита `open` сразу выходит — найдём настоящий PID запущенного .app через pgrep
                for arg in final_cmd:
                    if isinstance(arg, str) and arg.endswith(".app"):
                        entry["app_basename"] = Path(arg).stem
                        break
                threading.Thread(target=self._resolve_real_pid, args=(entry,), daemon=True).start()
            if owner_key:
                self.proxies[owner_key].setdefault("apps", []).append(entry)
            self.log_msg(f"запустил {name}: PID {child.pid}, через {proxy_url}")
            # Подсказка про running editor
            if name_lower in ("vscode", "cursor", "intellij idea", "pycharm", "pycharm ce", "webstorm"):
                self.log_msg(
                    f"подсказка: если {name} уже был запущен, integrated terminal "
                    "не получит прокси-env. Закрой полностью (Cmd+Q) и нажми снова."
                )
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить {name}: {e}")

    def _resolve_real_pid(self, entry: dict):
        """После запуска через `open -na` найти настоящий PID приложения через pgrep."""
        if not entry.get("app_basename"):
            return
        time.sleep(1.5)
        for attempt in range(3):
            try:
                r = subprocess.run(
                    ["pgrep", "-x", entry["app_basename"]],
                    capture_output=True, text=True, timeout=3,
                )
                pids = [int(p) for p in r.stdout.strip().splitlines() if p.strip()]
                if pids:
                    # Берём самый свежий процесс (наибольший PID)
                    entry["real_pid"] = max(pids)
                    return
            except Exception:
                pass
            time.sleep(1.0)

    def _chromium_cmd_with_proxy(self, base_cmd: list, proxy_url: str, brand: str) -> list:
        """Launch Chromium-based browser with isolated profile that actually uses the proxy."""
        import tempfile
        user_data = Path(tempfile.gettempdir()) / f"vpn-proxy-{brand}"
        user_data.mkdir(exist_ok=True)
        # base_cmd is e.g. [chrome.exe] or ["open", "-na", "Chrome.app"]
        # Chrome flags need to come AFTER any "open -na <app> --args" wrapper
        if base_cmd and base_cmd[0] in ("open",):
            # macOS: open -na "App.app" --args <chrome flags>
            return base_cmd + ["--args",
                               f"--proxy-server={proxy_url}",
                               f"--user-data-dir={user_data}",
                               "--no-first-run",
                               "--no-default-browser-check",
                               "--proxy-bypass-list=<-loopback>"]
        # Windows / Linux: direct exe
        return [base_cmd[0],
                f"--proxy-server={proxy_url}",
                f"--user-data-dir={user_data}",
                "--no-first-run",
                "--no-default-browser-check",
                "--proxy-bypass-list=<-loopback>"]

    def _firefox_cmd_with_proxy(self, base_cmd: list, proxy_url: str) -> list:
        """Firefox: build a temp profile with user.js that points at our proxy."""
        import tempfile
        from urllib.parse import urlparse
        prof = Path(tempfile.gettempdir()) / "vpn-proxy-firefox"
        prof.mkdir(exist_ok=True)
        u = urlparse(proxy_url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 8080
        user_js = prof / "user.js"
        user_js.write_text(
            f'user_pref("network.proxy.type", 1);\n'
            f'user_pref("network.proxy.http", "{host}");\n'
            f'user_pref("network.proxy.http_port", {port});\n'
            f'user_pref("network.proxy.ssl", "{host}");\n'
            f'user_pref("network.proxy.ssl_port", {port});\n'
            f'user_pref("network.proxy.share_proxy_settings", true);\n'
            f'user_pref("network.proxy.no_proxies_on", "localhost,127.0.0.1");\n'
            f'user_pref("browser.shell.checkDefaultBrowser", false);\n',
            encoding="utf-8",
        )
        if base_cmd and base_cmd[0] in ("open",):
            return base_cmd + ["--args", "-no-remote", "-profile", str(prof)]
        return [base_cmd[0], "-no-remote", "-profile", str(prof)]

    def launch_custom(self):
        f = filedialog.askopenfilename(title="Программа")
        if f:
            self.launch_app(Path(f).name, [f])

    def toggle_system_proxy(self, on: bool):
        """Включить/выключить системный HTTP/HTTPS proxy через настройки ОС."""
        if on:
            active = self._selected_or_first_proxy()
            if not active:
                messagebox.showwarning("Подключи прокси",
                    "Сначала подключи ключ — системному прокси нужен порт активного подключения.")
                return
            host_port = active["addr"]
            ok, msg = system_proxy_set(host_port)
            if ok:
                self._system_proxy_active = host_port
                self.log_msg(f"системный прокси: вкл ({host_port})")
                messagebox.showinfo(
                    "Системный VPN включён",
                    f"Весь HTTP/HTTPS-трафик системы теперь идёт через {host_port}.\n\n"
                    "Что работает через VPN:\n"
                    "  • Все браузеры (Chrome, Safari, Firefox, Edge…)\n"
                    "  • Большинство приложений и мессенджеров\n"
                    "  • git, npm, pip и прочие CLI\n\n"
                    "Что НЕ через VPN:\n"
                    "  • UDP-трафик (DNS, видеозвонки, игры)\n"
                    "  • Apps игнорирующие системные настройки\n\n"
                    "⚠ Не забудь выключить перед закрытием ZubriTunnel — иначе интернет ляжет!\n"
                    "(если такое случилось — открой ZubriTunnel снова и нажми «системный VPN off»)"
                )
            else:
                messagebox.showerror("Ошибка", f"Не удалось включить:\n{msg}")
        else:
            ok, msg = system_proxy_set(None)
            if ok:
                self._system_proxy_active = None
                self.log_msg("системный прокси: выкл")
                messagebox.showinfo("Готово", "Системный VPN выключен. Все приложения теперь идут напрямую.")
            else:
                messagebox.showerror("Ошибка", f"Не удалось выключить:\n{msg}")

    def _selected_or_first_proxy(self):
        """Вернуть выбранный ключ если он подключён, иначе любой подключённый."""
        sel = self.selected_key()
        if sel:
            p = self.proxies.get(sel["name"])
            if p and p["proc"].poll() is None:
                return p
        for v in self.proxies.values():
            if v["proc"].poll() is None:
                return v
        return None

    def toggle_git_proxy(self, on: bool):
        try:
            if on:
                active = self._selected_or_first_proxy()
                if not active:
                    messagebox.showwarning("Подключи прокси",
                        "Сначала подключи ключ — git нужен порт активного прокси.")
                    return
                proxy = f"http://{active['addr']}"
                subprocess.run(["git", "config", "--global", "http.proxy", proxy],
                               env=enhanced_path_env(, **_win_subprocess_kwargs()), check=True)
                subprocess.run(["git", "config", "--global", "https.proxy", proxy],
                               env=enhanced_path_env(, **_win_subprocess_kwargs()), check=True)
                self.log_msg(f"git: http.proxy={proxy} (global)")
                messagebox.showinfo("git", f"Все git-команды теперь через {proxy}.\nНе забудь выключить, когда наскучит.")
            else:
                subprocess.run(["git", "config", "--global", "--unset", "http.proxy"], env=enhanced_path_env(, **_win_subprocess_kwargs()))
                subprocess.run(["git", "config", "--global", "--unset", "https.proxy"], env=enhanced_path_env(, **_win_subprocess_kwargs()))
                self.log_msg("git: proxy unset")
                messagebox.showinfo("git", "git proxy выключен.")
        except FileNotFoundError:
            messagebox.showerror("Ошибка", "git не найден в PATH.")

    # ---- IDE integrated-terminal proxy ----

    def _ide_settings_paths(self) -> list:
        """Список (имя, settings.json) для VSCode/Cursor/VSCodium на текущей платформе."""
        if IS_MAC:
            base = Path.home() / "Library" / "Application Support"
        elif IS_WIN:
            base = Path(os.environ.get("APPDATA", str(Path.home())))
        else:
            base = Path.home() / ".config"
        return [
            ("VSCode",   base / "Code" / "User" / "settings.json"),
            ("Cursor",   base / "Cursor" / "User" / "settings.json"),
            ("VSCodium", base / "VSCodium" / "User" / "settings.json"),
        ]

    @staticmethod
    def _strip_jsonc(text: str) -> str:
        """Убрать // и /* */ из JSONC, сохраняя содержимое строк."""
        out = []
        i = 0
        in_str = False
        n = len(text)
        while i < n:
            c = text[i]
            if in_str:
                if c == "\\" and i + 1 < n:
                    out.append(c); out.append(text[i + 1]); i += 2; continue
                if c == '"':
                    in_str = False
                out.append(c); i += 1; continue
            if c == '"':
                in_str = True
                out.append(c); i += 1; continue
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                i += 2
                while i + 1 < n:
                    if text[i] == "*" and text[i + 1] == "/":
                        i += 2
                        break
                    i += 1
                continue
            out.append(c); i += 1
        return "".join(out)

    def _patch_ide_settings(self, path: Path, proxy_url: str, enable: bool) -> None:
        if not path.exists():
            if not enable:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
        else:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                data = {}
            else:
                try:
                    data = json.loads(self._strip_jsonc(text))
                except json.JSONDecodeError:
                    bak = path.with_suffix(path.suffix + ".broken.bak")
                    bak.write_bytes(path.read_bytes())
                    self.log_msg(f"{path.name} битый — забэкапил в {bak.name}, начинаю с пустых настроек")
                    data = {}
            # backup before overwrite
            bak = path.with_suffix(path.suffix + ".bak")
            if not bak.exists():
                bak.write_bytes(path.read_bytes())

        env_key = "terminal.integrated.env.osx" if IS_MAC else \
                  "terminal.integrated.env.windows" if IS_WIN else \
                  "terminal.integrated.env.linux"

        if enable:
            env = data.get(env_key)
            if not isinstance(env, dict):
                env = {}
            env["HTTPS_PROXY"] = proxy_url
            env["HTTP_PROXY"] = proxy_url
            env["ALL_PROXY"] = proxy_url
            env["NO_PROXY"] = "localhost,127.0.0.1"
            data[env_key] = env
            data["http.proxy"] = proxy_url
            data["http.proxyStrictSSL"] = True
        else:
            env = data.get(env_key)
            if isinstance(env, dict):
                for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
                    env.pop(k, None)
                if env:
                    data[env_key] = env
                else:
                    data.pop(env_key, None)
            else:
                data.pop(env_key, None)
            data.pop("http.proxy", None)
            data.pop("http.proxyStrictSSL", None)

        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def toggle_ide_terminals(self, enable: bool) -> None:
        proxy_url = ""
        if enable:
            active = self._selected_or_first_proxy()
            if not active:
                messagebox.showwarning(
                    "Подключи прокси",
                    "Чтобы прописать прокси в settings IDE, сначала подключи ключ.",
                )
                return
            proxy_url = f"http://{active['addr']}"

        patched = []
        skipped = []
        for name, p in self._ide_settings_paths():
            try:
                # IDE считаем установленным если есть .../User/ папка ИЛИ
                # пользователь жмёт «on» (тогда мы её создадим)
                if not p.parent.exists() and not enable:
                    continue
                self._patch_ide_settings(p, proxy_url, enable)
                patched.append(name)
                self.log_msg(f"{name}: terminals proxy {'on' if enable else 'off'} ({p})")
            except Exception as e:
                skipped.append(f"{name}: {e}")
                self.log_msg(f"{name}: ошибка — {e}")

        action = "включён" if enable else "выключен"
        if patched:
            msg = f"Прокси для integrated terminals {action} в:\n  • " + "\n  • ".join(patched)
            if enable:
                msg += f"\n\nProxy URL: {proxy_url}"
                msg += "\n\nЧтобы применилось:\n• Закрой " + ", ".join(patched) + " (Cmd+Q)\n• Открой снова через ZubriTunnel"
            if skipped:
                msg += "\n\nС ошибкой:\n  • " + "\n  • ".join(skipped)
            messagebox.showinfo("Готово", msg)
        elif skipped:
            messagebox.showerror("Ошибка", "Не удалось пропатчить:\n  • " + "\n  • ".join(skipped))
        else:
            messagebox.showinfo("IDE не найдены",
                "Не нашёл VSCode / Cursor / VSCodium в стандартных местах.\n\n"
                "Если ты их используешь — открой их хотя бы раз, чтобы создались папки настроек.")

    # ---- log / status ----

    def _pump_log(self, proc: subprocess.Popen):
        if proc.stdout is None:
            return
        for line in proc.stdout:
            self.log_msg(line.rstrip())

    def log_msg(self, line: str):
        ts = time.strftime("%H:%M:%S")
        msg = f"[{ts}] {line}\n"
        try:
            self.after(0, self._log_append, msg)
        except RuntimeError:
            pass

    def _log_append(self, msg: str):
        self._log_buffer.append(msg)
        # cap buffer at last 5000 lines
        if len(self._log_buffer) > 5000:
            self._log_buffer = self._log_buffer[-5000:]
        try:
            self._log_count_label.configure(text=f"{len(self._log_buffer)} строк")
        except Exception:
            pass
        # if popup is open, append there
        if self._log_window is not None and self._log_window.winfo_exists():
            try:
                w = self._log_window.log_widget  # type: ignore[attr-defined]
                w.configure(state="normal")
                w.insert("end", msg)
                w.see("end")
                w.configure(state="disabled")
            except Exception:
                pass

    def show_deps_dialog(self):
        """Окно «Проверка системы» — список зависимостей со статусом и кнопками установки."""
        win = tk.Toplevel(self)
        win.title("Системные зависимости")
        win.geometry(self._scaled_geom(680, 500))
        win.transient(self)
        win.configure(bg=COLORS["bg"])
        win.after(50, lambda: apply_dark_titlebar(win, self.actual_theme == "dark"))

        outer = tk.Frame(win, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(outer, text="Системные зависимости", bg=COLORS["bg"],
                 fg=COLORS["text"], font=UI_FONT_TITLE).pack(anchor="w", pady=(0, 4))
        tk.Label(outer, text="Зелёная галочка — установлено. Красный крестик — нажми «Установить».",
                 bg=COLORS["bg"], fg=COLORS["muted"], font=UI_FONT,
                 wraplength=620, justify="left").pack(anchor="w", pady=(0, 12))

        list_card = RoundedCard(outer, radius=14, fill=COLORS["panel"])
        list_card.pack(fill="both", expand=True)
        list_card.body.configure(bg=COLORS["panel"])
        rows_frame = tk.Frame(list_card.body, bg=COLORS["panel"])
        rows_frame.pack(fill="both", expand=True, padx=18, pady=14)

        for dep in check_dependencies():
            row = tk.Frame(rows_frame, bg=COLORS["panel"])
            row.pack(fill="x", pady=6)

            mark = "✓" if dep["ok"] else "✗"
            mark_color = COLORS["ok"] if dep["ok"] else COLORS["err"]
            tk.Label(row, text=mark, bg=COLORS["panel"], fg=mark_color,
                     font=(UI_FONT[0], 18, "bold"), width=2).pack(side="left", padx=(0, 8))

            text_box = tk.Frame(row, bg=COLORS["panel"])
            text_box.pack(side="left", fill="x", expand=True)
            tk.Label(text_box, text=dep["name"], bg=COLORS["panel"], fg=COLORS["text"],
                     font=UI_FONT_BOLD).pack(anchor="w")
            tk.Label(text_box, text=dep["detail"], bg=COLORS["panel"], fg=COLORS["muted"],
                     font=UI_FONT, wraplength=440, justify="left").pack(anchor="w")

            if dep["fix_label"] and dep["fix_action"]:
                action = dep["fix_action"]
                RoundButton(row, text=dep["fix_label"], variant="accent",
                            command=lambda a=action, w=win: self._run_dep_fix(a, w)).pack(side="right")

        bottom = tk.Frame(outer, bg=COLORS["bg"])
        bottom.pack(fill="x", pady=(12, 0))
        RoundButton(bottom, text="Обновить", variant="tool",
                    command=lambda: (win.destroy(), self.show_deps_dialog())).pack(side="left")
        RoundButton(bottom, text="Закрыть", variant="tool", command=win.destroy).pack(side="right")

    def _run_dep_fix(self, action: str, parent_win=None):
        """Выполнить действие установки зависимости."""
        if action == "build_vpn_proxy":
            self._build_vpn_proxy_dialog()
        elif action == "install_go":
            self._install_go()
        elif action == "install_brew":
            self._install_brew()
        if parent_win is not None:
            try:
                parent_win.destroy()
            except Exception:
                pass

    def _build_vpn_proxy_dialog(self):
        """Запускает 'go build' в фоне и пишет в лог."""
        if not shutil.which("go"):
            messagebox.showerror("Go не установлен",
                "Сначала установи Go SDK через «проверку системы» — кнопка «Установить» рядом с Go.")
            return
        # Подбираем рабочую директорию: Resources/ для Mac .app, или mac/ для dev mode
        candidates = [SCRIPT_DIR, SCRIPT_DIR.parent / "Resources", SCRIPT_DIR.parent.parent / "Resources"]
        workdir = None
        for c in candidates:
            if (c / "main.go").exists():
                workdir = c
                break
        if not workdir:
            messagebox.showerror("Не нашёл main.go", "main.go должен лежать рядом с gui.py")
            return
        out = SCRIPT_DIR.parent / "MacOS" / "vpn-proxy" if IS_MAC else SCRIPT_DIR / "vpn-proxy.exe"
        out.parent.mkdir(exist_ok=True)
        self.log_msg(f"Сборка vpn-proxy из {workdir}…")
        threading.Thread(target=self._build_thread, args=(str(workdir), str(out)), daemon=True).start()

    def _build_thread(self, workdir: str, out: str):
        try:
            go = find_go_binary() or "go"
            r = subprocess.run([go, "build", "-o", out, "."],
                               cwd=workdir, env=enhanced_path_env(, **_win_subprocess_kwargs()),
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                self.log_msg(f"vpn-proxy собран: {out}")
                self.after(0, lambda: messagebox.showinfo("Готово", f"vpn-proxy собран: {out}"))
            else:
                self.log_msg(f"go build failed: {r.stderr.strip()[:500]}")
                self.after(0, lambda: messagebox.showerror("Сборка упала", r.stderr or r.stdout or "(пустой вывод)"))
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Ошибка", str(e)))

    def _install_go(self):
        if IS_MAC:
            brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
            if os.path.exists(brew):
                self._open_terminal_install(f"arch -arm64 {brew} install go" if "/opt/homebrew" in brew else f"{brew} install go")
            else:
                messagebox.showinfo("Сначала Homebrew",
                    "Сначала установи Homebrew (есть кнопка в «проверке системы»), потом обнови проверку и нажми «Установить» рядом с Go.")
        elif IS_WIN:
            import webbrowser
            webbrowser.open("https://go.dev/dl/")
            messagebox.showinfo("Скачай Go",
                "Открыл https://go.dev/dl/ — скачай инсталлер и запусти. После установки перезапусти ZubriTunnel.")
        else:
            self._open_terminal_install("sudo apt-get install -y golang || sudo dnf install -y golang")

    def _install_brew(self):
        if not IS_MAC:
            messagebox.showinfo("Только Mac", "Homebrew нужен только на macOS.")
            return
        cmd = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        self._open_terminal_install(cmd)

    def _open_terminal_install(self, command: str):
        """Открыть Terminal/cmd с командой установки."""
        if IS_MAC:
            # Экранируем кавычки для AppleScript
            esc = command.replace('\\', '\\\\').replace('"', '\\"')
            script = f'tell application "Terminal" to do script "{esc}"\ntell application "Terminal" to activate'
            subprocess.Popen(["osascript", "-e", script], **_win_subprocess_kwargs())
        elif IS_WIN:
            subprocess.Popen(["cmd.exe", "/k", command], **_win_subprocess_kwargs())
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", command], **_win_subprocess_kwargs())
        self.log_msg(f"открыл терминал: {command}")

    def open_log_window(self):
        if self._log_window is not None and self._log_window.winfo_exists():
            self._log_window.lift()
            self._log_window.focus_force()
            return
        win = tk.Toplevel(self)
        win.title(f"{APP_NAME} — лог")
        win.geometry(self._scaled_geom(780, 500))
        win.configure(bg=COLORS["bg"])
        # Apply dark titlebar to popup too
        win.after(50, lambda: apply_dark_titlebar(win, self.actual_theme == "dark"))

        outer = tk.Frame(win, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=14, pady=14)

        card = RoundedCard(outer, radius=14, fill=COLORS["panel"])
        card.pack(fill="both", expand=True)
        card.body.configure(bg=COLORS["panel"])

        toolbar = tk.Frame(card.body, bg=COLORS["panel"])
        toolbar.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(toolbar, text="Лог", bg=COLORS["panel"], fg=COLORS["muted"],
                 font=UI_FONT_BOLD).pack(side="left")
        RoundButton(toolbar, text="Очистить", variant="tool",
                    command=lambda: self._clear_log()).pack(side="right")
        RoundButton(toolbar, text="Скопировать всё", variant="tool",
                    command=lambda: self._copy_log()).pack(side="right", padx=(0, 6))

        log_widget = scrolledtext.ScrolledText(
            card.body, font=UI_FONT_MONO,
            background=COLORS["bg"], foreground=COLORS["text"],
            insertbackground=COLORS["accent"], borderwidth=0, relief="flat",
            wrap="none",
        )
        log_widget.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        log_widget.insert("end", "".join(self._log_buffer))
        log_widget.see("end")
        log_widget.configure(state="disabled")

        win.log_widget = log_widget  # type: ignore[attr-defined]
        self._log_window = win

        def on_close():
            self._log_window = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)

    def _clear_log(self):
        self._log_buffer.clear()
        self._log_count_label.configure(text="0 строк")
        if self._log_window is not None and self._log_window.winfo_exists():
            w = self._log_window.log_widget  # type: ignore[attr-defined]
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")

    def _copy_log(self):
        text = "".join(self._log_buffer)
        self.clipboard_clear()
        self.clipboard_append(text)

    def _set_status(self, color: str, text: str):
        colors = {"green": "#2ca02c", "yellow": "#dca40a", "red": "#d62728", "grey": "#888888"}
        self.status_dot.configure(fg=colors.get(color, "#888"))
        self.status_text.configure(text=text)


def setup_windows_dpi():
    """Tell Windows we render our own DPI — fixes blurry text on high-DPI screens.
    Must be called BEFORE creating any tk.Tk()."""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            # Per-Monitor v2 (Windows 10 1703+) — best
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except (OSError, AttributeError):
            pass
        try:
            # Per-monitor v1 (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except (OSError, AttributeError):
            pass
        # System-DPI fallback (Vista+)
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class WindowsJobObject:
    """Job Object для авто-убийства всех vpn-proxy дочек при выходе ZubriTunnel.
    Когда последний handle на job закрывается (mainloop exit / process death),
    Windows сам убивает все assigned-процессы в группе.
    No-op на не-Windows и при ошибках — просто молча fallback."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self):
        self.h_job = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_void_p),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            self.h_job = k32.CreateJobObjectW(None, None)
            if not self.h_job:
                return
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(
                self.h_job, 9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info), ctypes.sizeof(info),
            )
            self._k32 = k32
        except Exception:
            self.h_job = None

    def assign(self, proc):
        """Привязать subprocess.Popen объект к job. После этого процесс умрёт
        вместе с job при exit'е ZubriTunnel."""
        if not self.h_job or os.name != "nt":
            return
        try:
            self._k32.AssignProcessToJobObject(self.h_job, int(proc._handle))
        except Exception:
            pass


def setup_windows_taskbar_id():
    """Claim a unique AppUserModelID so the taskbar shows our icon and doesn't
    group us under pythonw.exe."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        # SetCurrentProcessExplicitAppUserModelID expects PCWSTR (wide string).
        # Without argtypes ctypes passes a UTF-8 byte string, the call silently
        # fails, and the taskbar groups under pythonw.exe (with the Python icon).
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [wintypes.LPCWSTR]
        fn.restype = ctypes.c_long
        fn("local.zubritunnel.gui")
    except Exception:
        pass


def setup_macos_app_name(name: str = "ZubriTunnel"):
    """Override macOS process / menu-bar / dock display name from 'Python' to
    our app name. Uses libobjc (no PyObjC dep needed). Best-effort — silently
    skips if any objc call fails."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p

        objc_path = ctypes.util.find_library("objc")
        if not objc_path:
            return
        objc = ctypes.cdll.LoadLibrary(objc_path)
        # Foundation framework registers NSBundle / NSString classes
        try:
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        except OSError:
            return

        objc.objc_getClass.restype = c_void_p
        objc.objc_getClass.argtypes = [c_char_p]
        objc.sel_registerName.restype = c_void_p
        objc.sel_registerName.argtypes = [c_char_p]

        # IMPORTANT: get the actual address of the C function via cast, not
        # ctypes.addressof — addressof returns the address of the Python
        # ctypes wrapper struct, not the function itself, and jumping there
        # causes EXC_BAD_ACCESS / bus error.
        msg_addr = ctypes.cast(objc.objc_msgSend, c_void_p).value
        if not msg_addr:
            return

        FN_obj = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)
        FN_str = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_char_p)
        FN_2obj = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_void_p, c_void_p)
        msg = FN_obj(msg_addr)
        msg_str = FN_str(msg_addr)
        msg_2obj = FN_2obj(msg_addr)

        NSBundle = objc.objc_getClass(b"NSBundle")
        if not NSBundle:
            return
        bundle = msg(NSBundle, objc.sel_registerName(b"mainBundle"))
        if not bundle:
            return
        info = msg(bundle, objc.sel_registerName(b"localizedInfoDictionary"))
        if not info:
            info = msg(bundle, objc.sel_registerName(b"infoDictionary"))
        if not info:
            return

        NSString = objc.objc_getClass(b"NSString")
        if not NSString:
            return
        sel_string = objc.sel_registerName(b"stringWithUTF8String:")
        ns_name = msg_str(NSString, sel_string, name.encode("utf-8"))
        ns_key = msg_str(NSString, sel_string, b"CFBundleName")
        if not ns_name or not ns_key:
            return

        msg_2obj(info, objc.sel_registerName(b"setValue:forKey:"), ns_name, ns_key)
    except Exception:
        pass  # never crash the app for a cosmetic name fix


def main():
    setup_windows_dpi()
    setup_windows_taskbar_id()
    setup_macos_app_name("ZubriTunnel")
    KEYS_DIR.mkdir(exist_ok=True)
    app = App()
    try:
        app.tk.call("tk", "appname", "ZubriTunnel")
    except Exception:
        pass
    # Register atexit so even hard crashes / Ctrl-C cleanup
    import atexit
    def _cleanup_on_exit():
        # Disable system proxy if user left it on — иначе интернет в системе ляжет
        if getattr(app, "_system_proxy_active", None):
            try:
                system_proxy_set(None)
            except Exception:
                pass
        # Kill any remaining vpn-proxy subprocesses
        for name in list(app.proxies.keys()):
            try:
                p = app.proxies.get(name)
                if p and p.get("proc") and p["proc"].poll() is None:
                    p["proc"].kill()
            except Exception:
                pass
    atexit.register(_cleanup_on_exit)
    def on_close():
        for name in list(app.proxies.keys()):
            try:
                app._stop_proxy_for(name)
            except Exception:
                pass
        app.destroy()
    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
