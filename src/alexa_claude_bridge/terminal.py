"""Windows terminal I/O helpers for the PTY wrapper."""

from __future__ import annotations

import msvcrt
import os
import re

# Strip ANSI escape sequences (colors, cursor movement, screen clears, etc.)
_ANSI_RE = re.compile(
    r"\x1b"            # ESC
    r"(?:"
    r"\[[0-9;]*[A-Za-z]"   # CSI sequences  (e.g. \e[31m, \e[2J)
    r"|\][^\x07]*\x07"     # OSC sequences  (e.g. \e]0;title\a)
    r"|\([AB012]"           # charset select (e.g. \e(B)
    r")"
)

# Windows virtual-key scancodes → ANSI escape sequences
_SCANCODE_TO_ANSI: dict[str, bytes] = {
    "H": b"\x1b[A",   # Up
    "P": b"\x1b[B",   # Down
    "M": b"\x1b[C",   # Right
    "K": b"\x1b[D",   # Left
    "G": b"\x1b[H",   # Home
    "O": b"\x1b[F",   # End
    "I": b"\x1b[5~",  # Page Up
    "Q": b"\x1b[6~",  # Page Down
    "S": b"\x1b[3~",  # Delete
    "R": b"\x1b[2~",  # Insert
}


def read_key() -> bytes | None:
    """Read a single key (or escape sequence) from the Windows console.

    Returns None if no key is available (non-blocking).
    Returns bytes suitable for writing directly to a PTY.
    """
    if not msvcrt.kbhit():
        return None

    ch = msvcrt.getwch()

    # Special key prefix (arrows, function keys, etc.)
    if ch in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            scancode = msvcrt.getwch()
            return _SCANCODE_TO_ANSI.get(scancode, b"")
        return b""

    # Enter → carriage return (PTY convention)
    if ch == "\r":
        return b"\r"

    # Regular character (including Ctrl+C=\x03, Ctrl+D=\x04, etc.)
    return ch.encode("utf-8")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text for clean speech output."""
    return _ANSI_RE.sub("", text)


def get_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) of the current terminal."""
    size = os.get_terminal_size()
    return size.columns, size.lines
