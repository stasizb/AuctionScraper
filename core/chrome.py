"""Locate a Chrome / Chromium executable across macOS, Linux and Windows.

Search order:
  1. The `CHROME_EXE` environment variable if set and the path exists
  2. `shutil.which` over common binary names on PATH
  3. Platform-specific well-known install locations

Raises FileNotFoundError with a clear message if nothing is found — the
previous code silently used a hardcoded mac-only path and failed mysteriously
on other systems.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


_PATH_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
]

_KNOWN_PATHS_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

_KNOWN_PATHS_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]

_KNOWN_PATHS_WINDOWS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
]


def find_chrome() -> str:
    """Return the absolute path to a Chrome / Chromium executable.

    Raises FileNotFoundError with a helpful message if no binary is found.
    """
    env = os.environ.get("CHROME_EXE")
    if env and Path(env).is_file():
        return env

    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found

    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":
        candidates = _KNOWN_PATHS_MAC
    elif system == "Linux":
        candidates = _KNOWN_PATHS_LINUX
    elif system == "Windows":
        candidates = _KNOWN_PATHS_WINDOWS

    for path in candidates:
        if Path(path).is_file():
            return path

    raise FileNotFoundError(
        "Could not find Chrome / Chromium. Tried PATH names "
        f"{_PATH_NAMES!r} and OS-specific install locations. "
        "Set the CHROME_EXE environment variable to override."
    )
