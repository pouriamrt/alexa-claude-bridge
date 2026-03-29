"""PTY wrapper — runs Claude Code REPL with Alexa voice control.

Usage:
    alexa-claude                  # start Claude REPL with Alexa bridge
    alexa-claude --model sonnet   # pass args through to Claude

Instead of running `claude` directly, run `alexa-claude`. You get the exact
same REPL experience, plus Alexa can inject commands and read results.

Architecture:
    ┌──────────────┐     ┌─────────┐     ┌──────────────┐
    │ Your keyboard │────▶│         │────▶│ Your terminal │
    └──────────────┘     │   PTY   │     └──────────────┘
    ┌──────────────┐     │ (Claude)│     ┌──────────────┐
    │ Alexa → SQS  │────▶│         │────▶│ DynamoDB → ⏎ │
    └──────────────┘     └─────────┘     └──────────────┘
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from winpty import PTY

from .config import Config
from .sqs_watcher import SQSWatcher
from .terminal import get_terminal_size, read_key

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down boto3/botocore noise
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    config = Config()
    try:
        config.validate()
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        logger.error("Set COMMAND_QUEUE_URL env var (see .env.example)")
        sys.exit(1)

    # ── Spawn Claude in a PTY ────────────────────────────────────────
    cols, rows = get_terminal_size()
    pty = PTY(cols, rows)

    # Pass through any extra CLI args to claude
    claude_args = " ".join(sys.argv[1:])
    claude_cmd = f"claude {claude_args}".strip()
    logger.info("Starting: %s (Alexa bridge active)", claude_cmd)

    pty.spawn(claude_cmd)

    # ── Shared state for output capture ──────────────────────────────
    output_buffer: list[str] = []
    buffer_lock = threading.Lock()
    capturing = threading.Event()  # set = currently capturing for Alexa

    # ── Thread 1: PTY output → your terminal ─────────────────────────
    def output_reader() -> None:
        """Forward everything Claude writes to your screen."""
        while pty.isalive():
            try:
                data = pty.read()
            except (EOFError, OSError):
                break
            if not data:
                time.sleep(0.01)
                continue

            # Display to terminal
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

            # If Alexa is waiting for output, also buffer it
            if capturing.is_set():
                with buffer_lock:
                    output_buffer.append(data.decode("utf-8", errors="replace"))

    # ── Thread 2: your keyboard → PTY ────────────────────────────────
    def input_reader() -> None:
        """Forward your keystrokes to Claude."""
        while pty.isalive():
            key = read_key()
            if key:
                try:
                    pty.write(key)
                except (EOFError, OSError):
                    break
            else:
                time.sleep(0.01)  # No key available, yield CPU

    # ── Thread 3: SQS → PTY (Alexa commands) ─────────────────────────
    watcher = SQSWatcher(config, pty, output_buffer, buffer_lock, capturing)

    # ── Launch everything ─────────────────────────────────────────────
    threads = [
        threading.Thread(target=output_reader, name="pty-output", daemon=True),
        threading.Thread(target=input_reader, name="kbd-input", daemon=True),
        threading.Thread(target=watcher.run, name="sqs-watcher", daemon=True),
    ]
    for t in threads:
        t.start()

    # Block until Claude exits
    try:
        while pty.isalive():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass

    logger.info("Claude exited. Bridge shutting down.")
