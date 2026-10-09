#!/usr/bin/env python3
"""Time tracker collector daemon.

Polls the frontmost app / focused window / active browser tab every few
seconds and writes merged intervals to SQLite. Designed to be as light as
possible: a handful of native API calls per tick, osascript spawned only
when the focused window actually changes.
"""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

import Quartz
from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    kAXErrorSuccess,
    kAXFocusedWindowAttribute,
    kAXTitleAttribute,
)

from . import db

POLL_SECS = 3
FLUSH_SECS = 15              # persist the open interval at least this often
SUSPEND_GAP_SECS = POLL_SECS * 3  # tick gap larger than this => machine slept

# How long with no input (keyboard/mouse/scroll) before time is billed to AFK.
# Reading a page or thinking through a quiz produces no input, so a short value
# loses that time; the user tunes this in Settings. Read live from config.json.
DEFAULT_AFK_MINUTES = 5
# Media (video wake-lock / audio) keeps counting past the idle threshold, but
# never longer than this without any input — a backstop so a forgotten playing
# tab or a stuck audio device can't run up hours.
MEDIA_WATCH_MAX_SECS = 2 * 3600


def load_afk_secs() -> float:
    try:
        with open(db.CONFIG_PATH) as f:
            minutes = float(json.load(f).get("afk_idle_minutes") or DEFAULT_AFK_MINUTES)
        return max(30.0, minutes * 60)   # floor at 30s so it can't thrash
    except Exception:
        return DEFAULT_AFK_MINUTES * 60

# app name -> AppleScript dialect for reading the active tab URL
BROWSERS = {
    "Google Chrome": "chromium",
    "Brave Browser": "chromium",
    "Microsoft Edge": "chromium",
    "Comet": "chromium",
    "Safari": "safari",
}

APPLESCRIPT = {
    "chromium": 'tell application "{app}" to if (count of windows) > 0 then get URL of active tab of front window',
    "safari": 'tell application "Safari" to if (count of documents) > 0 then get URL of front document',
}

# Terminal agents such as Claude Code animate the first character of the window
# title (braille/star spinner frames). Comparing the raw title would turn every
# frame into a separate event, often every poll. Keep storing the original title
# for display and classification, but ignore only that volatile prefix when
# deciding whether the focused activity actually changed.
_TERMINAL_APPS = ("ghostty", "terminal", "iterm", "warp", "kitty",
                  "alacritty", "wezterm", "tabby", "hyper")
_AGENT_SPINNER_PREFIX = re.compile(r"^[⠀-⣿✳✢✻✽✶✦◐◑◒◓]\s?")


def title_change_key(app_name: str, title: str) -> str:
    """Stable title used only for interval change detection.

    Restrict normalization to terminal apps so legitimate leading symbols in
    document or browser titles remain meaningful.
    """
    app = (app_name or "").lower()
    if any(name in app for name in _TERMINAL_APPS):
        return _AGENT_SPINNER_PREFIX.sub("", title or "", count=1)
    return title or ""


def idle_seconds() -> float:
    return Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    )


def screen_locked() -> bool:
    d = Quartz.CGSessionCopyCurrentDictionary()
    return bool(d and d.get("CGSSessionScreenIsLocked", 0))


# --- video presence via IOKit power assertions -----------------------------
# A playing video holds a "PreventUserIdleDisplaySleep" assertion; browsers and
# players release it within seconds of pause/stop. Reading it (in-process, no
# subprocess) lets us keep lecture-watching — which has no keyboard/mouse input
# — from being billed to AFK. If IOKit can't be reached we fall back to the
# input-only idle test (previous behaviour).
_CF_UTF8 = 0x08000100
_CF_INT64 = 4
try:
    import ctypes
    import ctypes.util

    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _cf.CFNumberGetValue.restype = ctypes.c_bool
    _cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _cf.CFRelease.argtypes = [ctypes.c_void_p]
    _iokit.IOPMCopyAssertionsStatus.restype = ctypes.c_int
    _iokit.IOPMCopyAssertionsStatus.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    # Allocated once and kept for the process lifetime.
    _DISPLAY_KEY = _cf.CFStringCreateWithCString(None, b"PreventUserIdleDisplaySleep", _CF_UTF8)
except Exception:
    _iokit = None


def display_sleep_prevented() -> bool:
    """True when an app is holding the display awake — the signal that a video
    is actively playing (released within seconds of pause/stop)."""
    if _iokit is None:
        return False
    status = ctypes.c_void_p()
    if _iokit.IOPMCopyAssertionsStatus(ctypes.byref(status)) != 0 or not status.value:
        return False
    try:
        val = _cf.CFDictionaryGetValue(status, _DISPLAY_KEY)
        if not val:
            return False
        out = ctypes.c_int64(0)
        return bool(_cf.CFNumberGetValue(val, _CF_INT64, ctypes.byref(out)) and out.value > 0)
    finally:
        _cf.CFRelease(status)


# --- audio presence via CoreAudio ------------------------------------------
# The display-sleep assertion above is dropped intermittently by Chromium for
# embedded lecture players (background tab, buffering), so on its own it lets
# lecture time fall through to AFK. A lecture with sound keeps the output device
# running, which we read here (in-process) as a second, steadier "media is
# playing" signal — and it stops the moment the video is paused/ends.
try:
    _coreaudio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

    class _AudioObjectPropertyAddress(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32),
                    ("mScope", ctypes.c_uint32),
                    ("mElement", ctypes.c_uint32)]

    _coreaudio.AudioObjectGetPropertyData.restype = ctypes.c_int32
    _coreaudio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_AudioObjectPropertyAddress),
        ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]

    def _fourcc(s):
        return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])

    _AUDIO_SYSTEM_OBJECT = 1
    _AUDIO_SCOPE_GLOBAL = _fourcc("glob")
    _AUDIO_DEFAULT_OUTPUT = _fourcc("dOut")            # kAudioHardwarePropertyDefaultOutputDevice
    _AUDIO_RUNNING_SOMEWHERE = _fourcc("gone")         # kAudioDevicePropertyDeviceIsRunningSomewhere
except Exception:
    _coreaudio = None


def _audio_property(obj_id, selector) -> int:
    out = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    addr = _AudioObjectPropertyAddress(selector, _AUDIO_SCOPE_GLOBAL, 0)
    if _coreaudio.AudioObjectGetPropertyData(
            obj_id, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out)) != 0:
        return 0
    return out.value


def audio_playing() -> bool:
    """True when the default output device is actively playing audio — a steadier
    'watching a lecture' signal than the display assertion, and it stops on pause."""
    if _coreaudio is None:
        return False
    dev = _audio_property(_AUDIO_SYSTEM_OBJECT, _AUDIO_DEFAULT_OUTPUT)
    return bool(dev) and _audio_property(dev, _AUDIO_RUNNING_SOMEWHERE) != 0


# Apps where "media playing + no input" means the user is watching, not away.
# Browsers (BROWSERS) count too. This gate stops screen-awake utilities
# (caffeinate, Amphetamine) from inflating idle time as if it were activity.
VIDEO_PLAYER_APPS = ("quicktime player", "vlc", "iina", "mpv", "movist", "infuse", "elmedia")


def media_playing(idle) -> bool:
    """Media is actively playing — a video holds the display-sleep assertion, or
    audio is flowing — and it's still within the no-input cap. The two OS signals
    are ORed because Chromium drops the assertion intermittently mid-lecture."""
    return idle < MEDIA_WATCH_MAX_SECS and (display_sleep_prevented() or audio_playing())


def is_media_app(app) -> bool:
    """Whether media playing while `app` is frontmost counts as watching: only a
    browser or a known video player, so background utilities don't get credited."""
    return bool(app) and (app in BROWSERS or any(p in app.lower() for p in VIDEO_PLAYER_APPS))


def frontmost_app():
    # NSWorkspace.frontmostApplication() goes stale in a daemon that never
    # spins a run loop, so query the window server directly: the first
    # normal-layer window in the front-to-back list belongs to the active app.
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []
        for w in wins:
            if w.get("kCGWindowLayer") != 0:
                continue
            bounds = w.get("kCGWindowBounds", {})
            if bounds.get("Width", 0) < 50 or bounds.get("Height", 0) < 50:
                continue  # cursor, tooltips, and other window-server slivers
            if w.get("kCGWindowOwnerName") in ("Window Server", "Dock"):
                continue
            pid = w.get("kCGWindowOwnerPID")
            ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
            bundle = ra.bundleIdentifier() if ra is not None else ""
            return w.get("kCGWindowOwnerName"), bundle, pid
    except Exception:
        pass
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None, None, None
    return app.localizedName(), app.bundleIdentifier(), app.processIdentifier()


def file_path(value) -> str:
    """POSIX path from a file:// URL or a bare path; '' for anything else."""
    value = str(value or "")
    if value.startswith("file://"):
        return unquote(urlparse(value).path)
    return value if value.startswith("/") else ""


def focused_window(pid):
    """(title, path) of the focused window. `path` comes from the window's
    AXDocument (documents in Excel/Preview/editors, a terminal's cwd): one
    extra in-process AX read on the element we already hold, no subprocess."""
    if pid is None:
        return "", ""
    try:
        app_ref = AXUIElementCreateApplication(pid)
        err, window = AXUIElementCopyAttributeValue(app_ref, kAXFocusedWindowAttribute, None)
        if err != kAXErrorSuccess or window is None:
            return "", ""
        err, title = AXUIElementCopyAttributeValue(window, kAXTitleAttribute, None)
        title = str(title) if err == kAXErrorSuccess and title else ""
        err, doc = AXUIElementCopyAttributeValue(window, "AXDocument", None)
        return title, file_path(doc) if err == kAXErrorSuccess else ""
    except Exception:
        return "", ""


# Office apps may not publish AXDocument; ask them over AppleScript instead,
# but only when the focused window changes (like browser tabs), never per tick.
OFFICE_DOC_SCRIPT = {
    "Microsoft Excel": 'tell application "Microsoft Excel" to if (count of workbooks) > 0 then get full name of active workbook',
    "Microsoft Word": 'tell application "Microsoft Word" to if (count of documents) > 0 then get full name of active document',
    "Microsoft PowerPoint": 'tell application "Microsoft PowerPoint" to if (count of presentations) > 0 then get full name of active presentation',
}


def office_doc_path(app_name: str) -> str:
    script = OFFICE_DOC_SCRIPT.get(app_name)
    if not script:
        return ""
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, text=True, timeout=3)
        return file_path(out.stdout.strip())
    except Exception:
        return ""


def active_tab_url(app_name: str) -> str:
    dialect = BROWSERS.get(app_name)
    if not dialect:
        return ""
    script = APPLESCRIPT[dialect].format(app=app_name)
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        url = out.stdout.strip()
        return url if url.startswith(("http://", "https://", "file://")) else ""
    except Exception:
        return ""


def url_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


class Collector:
    def __init__(self):
        self.conn = db.init_db()
        self.current = None       # open interval dict, or None
        self.row_id = None        # events.id of the open interval
        self.last_flush = 0.0
        self.last_present = time.time()   # last tick the user was present (input or watching)
        self.afk_idle_secs = load_afk_secs()
        self.afk_checked = time.time()    # last time we re-read the threshold from config
        self.running = True

    # ----- interval lifecycle -------------------------------------------
    def _flush(self, force=False):
        if self.current is None:
            return
        now = time.time()
        if not force and now - self.last_flush < FLUSH_SECS:
            return
        c = self.current
        if self.row_id is None:
            cur = self.conn.execute(
                "INSERT INTO events (start_ts, end_ts, app, bundle_id, window_title, url, domain, path, is_afk) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (c["start"], c["end"], c["app"], c["bundle"], c["title"], c["url"], c["dom"],
                 c["path"] or None, c["afk"]),
            )
            self.row_id = cur.lastrowid
        else:
            self.conn.execute("UPDATE events SET end_ts=? WHERE id=?", (c["end"], self.row_id))
        self.conn.commit()
        self.last_flush = now

    def close_interval(self, end_ts=None):
        if self.current is None:
            return
        if end_ts is not None:
            self.current["end"] = max(self.current["start"], end_ts)
        self._flush(force=True)
        # drop sub-second slivers
        if self.row_id is not None and self.current["end"] - self.current["start"] < 1:
            self.conn.execute("DELETE FROM events WHERE id=?", (self.row_id,))
            self.conn.commit()
        self.current = None
        self.row_id = None

    def open_interval(self, start_ts, app, bundle, title, url, afk, path=""):
        self.current = {
            "start": start_ts, "end": start_ts, "app": app, "bundle": bundle,
            "title": title, "url": url, "dom": url_domain(url), "path": path,
            "afk": int(afk),
        }
        self.row_id = None
        self._flush(force=True)

    # ----- one poll tick -------------------------------------------------
    def tick(self, now):
        # Pick up a changed idle threshold from Settings without a restart.
        if now - self.afk_checked > 60:
            self.afk_idle_secs = load_afk_secs()
            self.afk_checked = now

        idle = idle_seconds()
        locked = screen_locked()
        idle_over = idle >= self.afk_idle_secs

        # A user watching a lecture (media playing, no input) on a browser/player
        # is present, not away — count it so it isn't billed to AFK. The media
        # reads and the frontmost lookup (reused below) are paid only when idle.
        app = bundle = pid = None
        watching = False
        if idle_over and not locked and media_playing(idle):
            app, bundle, pid = frontmost_app()
            watching = is_media_app(app)

        if locked or (idle_over and not watching):
            # Bill idle time to AFK from when the user was last present (recent
            # input, or a video last seen playing) — no inflation of the last
            # active app, and watched-video time already credited is preserved.
            afk_since = now if locked else max(now - idle, self.last_present)
            if self.current and not self.current["afk"]:
                self.close_interval(end_ts=afk_since)
            if self.current is None:
                self.open_interval(afk_since, "AFK", "", "", "", afk=True)
            self.current["end"] = now
            self._flush()
            return

        self.last_present = now
        if app is None:
            app, bundle, pid = frontmost_app()
        if app is None:
            return
        title, path = focused_window(pid)

        # An AX path change (e.g. `cd` in a terminal) opens a new interval.
        # An empty AX path doesn't, so the Office fallback path is kept.
        changed = (
            self.current is None
            or self.current["afk"]
            or self.current["app"] != app
            or title_change_key(app, self.current["title"]) != title_change_key(app, title)
            or (path and path != self.current["path"])
        )
        if changed:
            url = active_tab_url(app) if app in BROWSERS else ""
            path = path or office_doc_path(app) or file_path(url)
            self.close_interval(end_ts=now)
            self.open_interval(now, app, bundle, title, url, afk=False, path=path)
        else:
            self.current["end"] = now
            self._flush()

    # ----- main loop ------------------------------------------------------
    def run(self):
        # Trigger the Accessibility permission dialog on first launch.
        trusted = AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
        if not trusted:
            print("[collector] Accessibility not granted yet - window titles will be "
                  "empty until you allow it in System Settings > Privacy & Security "
                  "> Accessibility.", flush=True)

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        print(f"[collector] started, db={db.DB_PATH}", flush=True)

        last_tick = time.time()
        while self.running:
            time.sleep(POLL_SECS)
            now = time.time()
            if now - last_tick > SUSPEND_GAP_SECS:
                # Machine slept or process was suspended: never credit the gap.
                self.close_interval(end_ts=last_tick)
            last_tick = now
            try:
                self.tick(now)
            except Exception as e:
                print(f"[collector] tick error: {e}", file=sys.stderr, flush=True)

        self.close_interval(end_ts=time.time())
        print("[collector] stopped", flush=True)

    def _stop(self, *_):
        self.running = False


def acquire_lock():
    lock = open(db.DATA_DIR + "/collector.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[collector] another instance is already running, exiting", flush=True)
        sys.exit(0)
    return lock


if __name__ == "__main__":
    import os
    os.makedirs(db.DATA_DIR, exist_ok=True)
    _lock = acquire_lock()
    Collector().run()
