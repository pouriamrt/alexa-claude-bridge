"""Windows keyboard simulation and window management via ctypes.

No external dependencies — uses only the Windows user32/kernel32 APIs.
Finds the Claude terminal window, focuses it, and pastes commands via clipboard.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import time

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ── Set proper 64-bit return/arg types (prevents handle truncation) ───
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

# Virtual key codes
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002

# Clipboard
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def find_window(
    title_fragment: str | None = None,
    exclude: list[str] | None = None,
    window_class: str | None = None,
) -> int | None:
    """Find a visible window by title substring and/or window class name.

    *window_class* — if set, only windows with this exact class name match.
    *title_fragment* — if set, the window title must contain this text (case-insensitive).
    *exclude* — windows whose title contains any of these strings are skipped.

    At least one of *title_fragment* or *window_class* must be provided.
    """
    exclude_lower = [e.lower() for e in (exclude or [])]
    matches: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        # Check window class first (cheapest filter)
        if window_class:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            if cls_buf.value != window_class:
                return True

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()

        if any(ex in title for ex in exclude_lower):
            return True
        if title_fragment and title_fragment.lower() not in title:
            return True

        matches.append(hwnd)
        return True

    user32.EnumWindows(_enum_callback, 0)
    return matches[0] if matches else None


def focus_window(hwnd: int) -> bool:
    """Bring a window to the foreground."""
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


def _set_clipboard(text: str) -> None:
    """Copy text to the system clipboard."""
    if not user32.OpenClipboard(0):
        logger.warning("Failed to open clipboard")
        return

    user32.EmptyClipboard()
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not h_mem:
        logger.warning("GlobalAlloc failed — cannot copy to clipboard")
        user32.CloseClipboard()
        return
    ptr = kernel32.GlobalLock(h_mem)
    if not ptr:
        logger.warning("GlobalLock failed — cannot copy to clipboard")
        user32.CloseClipboard()
        return
    ctypes.memmove(ptr, encoded, len(encoded))
    kernel32.GlobalUnlock(h_mem)
    user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    user32.CloseClipboard()


def _send_key(vk: int) -> None:
    """Press and release a single key."""
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    """Simulate Ctrl+V (paste)."""
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def inject_command(
    command: str,
    window_title: str | None = None,
    exclude_titles: list[str] | None = None,
    window_class: str | None = None,
) -> bool:
    """Paste a command into a terminal window and press Enter.

    1. Finds a window matching *window_class* and/or *window_title*
       (skipping any whose title matches *exclude_titles*)
    2. Brings it to the foreground
    3. Copies the command to clipboard → Ctrl+V → Enter

    Returns True if injection succeeded.
    """
    if exclude_titles is None:
        exclude_titles = ["Visual Studio Code"]
    hwnd = find_window(
        title_fragment=window_title,
        exclude=exclude_titles,
        window_class=window_class,
    )
    if not hwnd:
        logger.warning(
            "No matching window (title=%s, class=%s) — is Claude running?",
            window_title, window_class,
        )
        return False

    if not focus_window(hwnd):
        logger.warning("Could not focus the Claude window")
        return False

    time.sleep(0.3)  # Let the window activate

    _set_clipboard(command)
    time.sleep(0.05)
    _send_ctrl_v()
    time.sleep(0.15)
    _send_key(VK_RETURN)

    logger.info("Injected command into terminal")
    return True
