"""Media and volume control, playing and searching on sites, and window control.

Media keys are the same virtual keys a keyboard's play/pause/next buttons send, so they
work with whatever player Windows routes them to: Spotify, YouTube in the browser, VLC...
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import re
import threading
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from difflib import SequenceMatcher

VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
      "mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF}
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def press(key: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    vk = VK[key]
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


def press_previous_track(dry_run: bool = False) -> None:
    """Goes to the previous track. Two quick presses: the first restarts the current song
    (what players do mid-track), the second moves back one."""
    if dry_run:
        return
    import time
    from .spotify import now_playing
    before = now_playing()
    press("previous")
    if before is not None:
        # Spotify shows the song in its window title: if one press already changed the song
        # (it had only just started), stop there instead of going back two songs.
        time.sleep(0.7)
        if now_playing() != before:
            return
    else:
        time.sleep(0.25)
    press("previous")


# ---- volume ----------------------------------------------------------------------

def _endpoint():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> int:
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def set_volume(percent: int) -> int:
    percent = max(0, min(100, int(percent)))
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(percent / 100, None)
    if percent > 0 and ep.GetMute():
        ep.SetMute(0, None)
    return percent


def set_mute(muted: bool) -> None:
    _endpoint().SetMute(1 if muted else 0, None)


def is_muted() -> bool:
    return bool(_endpoint().GetMute())


class Ducker:
    """Turns the volume down while Friday listens, then puts it back."""

    def __init__(self, level: float = 0.3):
        self.level, self.saved, self.lock = level, None, threading.Lock()

    def duck(self):
        with self.lock:
            if self.saved is not None:
                return
            try:
                self.saved = get_volume()
                if self.saved > 10:
                    set_volume(round(self.saved * self.level))
            except Exception:
                self.saved = None

    def restore(self):
        with self.lock:
            if self.saved is None:
                return
            try:
                set_volume(self.saved)
            except Exception:
                pass
            self.saved = None

    @contextmanager
    def ducked(self):
        self.duck()
        try:
            yield
        finally:
            self.restore()


# ---- play and search on sites ---------------------------------------------------------

SITE_SEARCH = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "twitter": "https://x.com/search?q={q}",
    "x": "https://x.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "news": "https://news.google.com/search?q={q}",
    "images": "https://www.google.com/search?tbm=isch&q={q}",
    "tradingview": "https://www.tradingview.com/search/?query={q}",
}


def search_site(site: str, query: str, dry_run: bool = False) -> str:
    template = SITE_SEARCH[site.lower()]
    # Path-style URLs (Spotify, Maps) need %20; query strings take +.
    in_path = "/search/{q}" in template
    url = template.format(q=urllib.parse.quote(query) if in_path else urllib.parse.quote_plus(query))
    if site.lower() == "spotify" and spotify_app_installed() and not dry_run:
        os.startfile("spotify:search:" + urllib.parse.quote(query))
        return url
    if not dry_run:
        webbrowser.open(url)
    return url


def youtube_first_video(query: str, timeout: float = 6) -> str | None:
    """The top search result's video id, read from YouTube's results page (no API key needed)."""
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except OSError:
        return None
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html) or re.search(r'"videoId":"([\w-]{11})"', html)
    return m.group(1) if m else None


def play_on_youtube(query: str, dry_run: bool = False) -> tuple[bool, str]:
    """Opens and plays the top result; falls back to the results page."""
    vid = youtube_first_video(query)
    url = f"https://www.youtube.com/watch?v={vid}" if vid else SITE_SEARCH["youtube"].format(q=urllib.parse.quote_plus(query))
    if not dry_run:
        webbrowser.open(url)
    return bool(vid), url


_spotify_cache: dict = {}


def spotify_app_installed() -> bool:
    if "v" not in _spotify_cache:
        try:
            import winreg
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "spotify"))
            _spotify_cache["v"] = True
        except OSError:
            _spotify_cache["v"] = False
    return _spotify_cache["v"]


# ---- windows of running apps ----------------------------------------------------------

def _process_name(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def running_windows() -> list[dict]:
    """Visible top-level app windows: [{"pid", "name", "title", "hwnd"}]. Direct Windows calls, ~10 ms."""
    import ctypes.wintypes
    user32 = ctypes.windll.user32
    GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW = 4, -20, 0x80
    found: list[dict] = []
    seen_pids: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in seen_pids:
            return True
        seen_pids.add(pid.value)
        found.append({"pid": pid.value, "name": _process_name(pid.value), "title": title.value, "hwnd": hwnd})
        return True

    user32.EnumWindows(callback, 0)
    return [w for w in found if w["name"]]


PROTECTED = {"explorer", "powershell", "pwsh", "windowsterminal", "cmd", "conhost", "python", "pythonw",
             "claude", "taskmgr", "systemsettings", "applicationframehost", "textinputhost"}


def find_window(spoken: str, windows: list[dict] | None = None) -> tuple[dict | None, float]:
    spoken = re.sub(r"^(the|my)\s+", "", spoken.strip().lower())
    spoken = re.sub(r"\s+(app|application|window|browser)$", "", spoken)
    best, best_score = None, 0.0
    for w in windows if windows is not None else running_windows():
        name, title = w["name"].lower(), w["title"].lower()
        if spoken == name:
            score = 1.0
        elif spoken.replace(" ", "") in name or spoken in title.split(" - ")[-1]:
            score = 0.9
        elif spoken in title:
            score = 0.75
        else:
            score = max(SequenceMatcher(None, spoken, name).ratio(), SequenceMatcher(None, spoken, title).ratio()) * 0.8
        if score > best_score:
            best, best_score = w, score
    return best, best_score


def close_window(w: dict) -> None:
    """Asks the app to close, like clicking X: it can still prompt to save work."""
    WM_CLOSE = 0x0010
    ctypes.windll.user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)


def focus_window(w: dict) -> bool:
    user32 = ctypes.windll.user32
    hwnd = w["hwnd"]
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the foreground app hand over focus; a tap of Alt satisfies that rule.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
    return bool(user32.SetForegroundWindow(hwnd))
