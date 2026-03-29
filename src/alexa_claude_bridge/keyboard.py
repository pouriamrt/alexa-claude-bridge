"""Windows keyboard simulation and window management via ctypes.

No external dependencies — uses only the Windows user32/kernel32 APIs.
Finds the Claude terminal window, focuses it, and pastes commands via clipboard.
Uses the modern SendInput API for reliable keystroke delivery.
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

# SendInput constants
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

# Clipboard
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Retry / timing
CLIPBOARD_RETRIES = 3
CLIPBOARD_RETRY_DELAY = 0.1
FOCUS_SETTLE_DELAY = 0.5
POST_PASTE_DELAY = 0.35
POST_ENTER_DELAY = 0.1


# ── SendInput structures (64-bit safe) ──────────────────────────────
# The INPUT union must include MOUSEINPUT (the largest variant) so that
# sizeof(INPUT) == 40 on 64-bit Windows.  Without it SendInput silently
# ignores the call because cbSize doesn't match.


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _InputUnion),
    ]


def _make_key_input(vk: int, flags: int = 0) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


def _send_inputs(*inputs: INPUT) -> int:
    """Send an array of INPUT structs via SendInput. Returns number sent."""
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(arr), ctypes.byref(arr), ctypes.sizeof(INPUT))
    if sent != len(inputs):
        logger.warning("SendInput sent %d/%d events", sent, len(inputs))
    return sent


# ── Window helpers ───────────────────────────────────────────────────


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
    """Bring a window to the foreground using thread-attachment trick.

    Windows blocks SetForegroundWindow unless the caller is the foreground
    process.  Attaching our thread to the current foreground thread temporarily
    gives us permission to steal focus reliably.
    """
    foreground = user32.GetForegroundWindow()
    if foreground == hwnd:
        return True  # Already focused

    fg_tid = user32.GetWindowThreadProcessId(foreground, None)
    our_tid = kernel32.GetCurrentThreadId()

    attached = False
    if fg_tid != our_tid:
        attached = bool(user32.AttachThreadInput(our_tid, fg_tid, True))

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    result = user32.SetForegroundWindow(hwnd)

    if attached:
        user32.AttachThreadInput(our_tid, fg_tid, False)

    # Verify we actually got focus
    if user32.GetForegroundWindow() != hwnd:
        logger.warning("SetForegroundWindow=%s but foreground is another window", result)
        return False

    return True


# ── Clipboard ────────────────────────────────────────────────────────


def _set_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on success."""
    for attempt in range(CLIPBOARD_RETRIES):
        if user32.OpenClipboard(0):
            break
        logger.debug("Clipboard busy, retry %d/%d", attempt + 1, CLIPBOARD_RETRIES)
        time.sleep(CLIPBOARD_RETRY_DELAY)
    else:
        logger.warning("Failed to open clipboard after %d retries", CLIPBOARD_RETRIES)
        return False

    user32.EmptyClipboard()
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not h_mem:
        logger.warning("GlobalAlloc failed — cannot copy to clipboard")
        user32.CloseClipboard()
        return False
    ptr = kernel32.GlobalLock(h_mem)
    if not ptr:
        logger.warning("GlobalLock failed — cannot copy to clipboard")
        user32.CloseClipboard()
        return False
    ctypes.memmove(ptr, encoded, len(encoded))
    kernel32.GlobalUnlock(h_mem)
    user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    user32.CloseClipboard()
    return True


# ── Window messages (fallback) ───────────────────────────────────────

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_PASTE = 0x0302


# ── Keystroke helpers (SendInput) ────────────────────────────────────


def _send_key(vk: int) -> None:
    """Press and release a single key via SendInput."""
    _send_inputs(
        _make_key_input(vk),
        _make_key_input(vk, KEYEVENTF_KEYUP),
    )


def _send_ctrl_v() -> None:
    """Simulate Ctrl+V (paste) via SendInput — all four events in one call."""
    _send_inputs(
        _make_key_input(VK_CONTROL),
        _make_key_input(VK_V),
        _make_key_input(VK_V, KEYEVENTF_KEYUP),
        _make_key_input(VK_CONTROL, KEYEVENTF_KEYUP),
    )


def _post_ctrl_v(hwnd: int) -> None:
    """Send Ctrl+V directly to a window handle via PostMessage (focus-independent)."""
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_CONTROL, 0)
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_V, 0)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_V, 0)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_CONTROL, 0)


def _post_key(hwnd: int, vk: int) -> None:
    """Send a keypress directly to a window handle via PostMessage."""
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)


# ── Public API ───────────────────────────────────────────────────────


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

    Uses SendInput when focus is obtained, falls back to PostMessage
    (which works even without focus) if SetForegroundWindow fails.

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
            window_title,
            window_class,
        )
        return False

    if not _set_clipboard(command):
        return False

    got_focus = focus_window(hwnd)

    if got_focus:
        # SendInput path — requires foreground focus
        time.sleep(FOCUS_SETTLE_DELAY)
        _send_ctrl_v()
        time.sleep(POST_PASTE_DELAY)
        _send_key(VK_RETURN)
        time.sleep(POST_ENTER_DELAY)
        logger.info("Injected command via SendInput")
    else:
        # PostMessage path — sends keystrokes directly to the window handle
        logger.info("Focus failed, using PostMessage fallback")
        _post_ctrl_v(hwnd)
        time.sleep(POST_PASTE_DELAY)
        _post_key(hwnd, VK_RETURN)
        time.sleep(POST_ENTER_DELAY)
        logger.info("Injected command via PostMessage")

    return True
