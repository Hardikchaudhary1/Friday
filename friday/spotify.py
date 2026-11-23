"""Plays a specific song in the Spotify desktop app.

Spotify has no command-line way to play a track, so Friday opens Spotify's own search for
what you said and presses the matching "Play <song>" button through Windows UI Automation
(the accessibility interface screen readers use). No Spotify account setup is needed.
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse

_STOP = {"the", "a", "an", "by", "of", "and", "on", "in", "to", "song", "track", "music", "spotify", "from", "feat"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOP}


def _uia():
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    return UIA, comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=UIA.IUIAutomation)


def spotify_window() -> dict | None:
    from .media import running_windows
    return next((w for w in running_windows() if w["name"].lower() == "spotify"), None)


def now_playing() -> str | None:
    """'Artist - Song' while playing; None when paused or closed (Spotify shows its own name then)."""
    w = spotify_window()
    if not w or w["title"].lower().startswith("spotify"):
        return None
    return w["title"]


def play_song(query: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Searches Spotify and plays the best-matching song. Returns (ok, song name or reason)."""
    w = spotify_window()
    if not w:
        return False, "Spotify isn't open"
    UIA, uia = _uia()
    wanted = _words(query)
    os.startfile("spotify:search:" + urllib.parse.quote(query))
    button_type = uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId)
    window_bottom = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        try:
            root = uia.ElementFromHandle(w["hwnd"])
            rect_w = root.CurrentBoundingRectangle
            found = root.FindAll(UIA.TreeScope_Descendants, button_type)
            best = None
            for i in range(found.Length):
                try:
                    el = found.GetElement(i)
                    name = el.CurrentName or ""
                    rect = el.CurrentBoundingRectangle
                except Exception:      # the page changed under us; skip this element
                    continue
                if not name.startswith("Play ") or name == "Play":
                    continue
                # Only rows visible in the results area: not the player bar, sidebar, or below the fold.
                if rect.top > rect_w.bottom - 130 or rect.left - rect_w.left < 80:
                    continue
                title_words = _words(name[5:])
                overlap = len(wanted & title_words)
                if not overlap:
                    continue
                # Prefer titles that match more of what you said and add fewer extra words
                # ("Believer" over "Believer (feat. Lil Wayne)"), then the higher row.
                score = (overlap - 0.15 * len(title_words - wanted), -rect.top)
                if best is None or score > best[0]:
                    best = (score, el, name[5:])
            if best:
                _, el, title = best
                el.GetCurrentPattern(UIA.UIA_InvokePatternId).QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                return True, title
        except Exception:
            continue   # Spotify was mid-navigation; look again
    return False, "no matching song showed up"
