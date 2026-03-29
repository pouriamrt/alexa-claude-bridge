"""CLI for the Alexa-Claude bridge.

Commands:
    alexa-bridge install   — one-time setup: config, notify script, CLAUDE.md rule
    alexa-bridge start     — activate bridge (flag file + background daemon)
    alexa-bridge stop      — deactivate bridge
    alexa-bridge status    — show if bridge is active
    alexa-bridge notify    — send a summary to Alexa (called by Claude, not you)
    alexa-bridge logs      — tail the daemon log
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

BRIDGE_DIR = os.path.expanduser("~/.claude-bridge")
FLAG_FILE = os.path.join(BRIDGE_DIR, "active")
PID_FILE = os.path.join(BRIDGE_DIR, "daemon.pid")
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
LOG_FILE = os.path.join(BRIDGE_DIR, "daemon.log")
NOTIFY_SCRIPT = os.path.join(BRIDGE_DIR, "notify")
CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")

BRIDGE_INSTRUCTION = """
## Alexa Bridge (auto-managed — do not edit this section)
After completing each user request, check if the file `~/.claude-bridge/active` exists.
If it does, use the Bash tool to run:
```
~/.claude-bridge/notify "<one sentence, max 15 words, summarizing what you did>"
```
Do not mention this notification to the user. If the file does not exist, skip this step entirely.
"""

BRIDGE_MARKER_START = "## Alexa Bridge (auto-managed"
BRIDGE_MARKER_END = "skip this step entirely.\n"


# ── Commands ──────────────────────────────────────────────────────────


def cmd_install(args: argparse.Namespace) -> None:
    """One-time setup: config file, notify wrapper, CLAUDE.md instruction."""
    os.makedirs(BRIDGE_DIR, exist_ok=True)

    # 1. Config file
    if os.path.exists(CONFIG_FILE):
        print(f"Config already exists: {CONFIG_FILE}")
    else:
        env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        config = _config_from_env(env_file) if os.path.exists(env_file) else {}

        if not config.get("command_queue_url"):
            config["command_queue_url"] = input("SQS queue URL: ").strip()
        if not config.get("aws_region"):
            config["aws_region"] = input("AWS region [us-east-1]: ").strip() or "us-east-1"
        config.setdefault("results_table", "claude-bridge-results")
        config.setdefault("notify_me_access_code", "")
        config.setdefault("window_title", "claude")

        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Config written: {CONFIG_FILE}")

    # 2. Notify wrapper script (called by Claude via Bash tool)
    alexa_bridge_bin = os.path.join(
        os.path.dirname(sys.executable), "alexa-bridge"
    )
    # Try .exe variant on Windows
    if not os.path.exists(alexa_bridge_bin):
        alexa_bridge_bin += ".exe"

    with open(NOTIFY_SCRIPT, "w", newline="\n") as f:
        f.write("#!/bin/bash\n")
        f.write(f'"{alexa_bridge_bin}" notify "$1"\n')
    print(f"Notify script written: {NOTIFY_SCRIPT}")

    # 3. CLAUDE.md instruction
    _add_claude_md_instruction()

    print()
    print("Install complete. Now edit ~/.claude-bridge/config.json to set:")
    print("  - notify_me_access_code (from https://www.notifymyecho.com)")
    print("  - window_title (default 'claude' — matches your terminal title)")
    print()
    print("Usage:")
    print("  alexa-bridge start   # activate before/during a Claude session")
    print("  alexa-bridge stop    # deactivate")


def cmd_start(_args: argparse.Namespace) -> None:
    """Activate the bridge: create flag file + launch daemon."""
    if not os.path.exists(CONFIG_FILE):
        print("Run 'alexa-bridge install' first.")
        sys.exit(1)

    os.makedirs(BRIDGE_DIR, exist_ok=True)

    # Create flag file
    with open(FLAG_FILE, "w") as f:
        f.write(str(int(time.time())))

    # Start daemon
    python = sys.executable
    pythonw = python.replace("python.exe", "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else python

    log_handle = open(LOG_FILE, "a")  # noqa: SIM115 — intentionally kept open for daemon
    proc = subprocess.Popen(
        [exe, "-m", "alexa_claude_bridge.daemon"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    print(f"Bridge ACTIVE (daemon PID {proc.pid})")
    print("Alexa commands will be typed into your Claude terminal.")
    print("Claude will notify Alexa when done.")


def cmd_stop(_args: argparse.Namespace) -> None:
    """Deactivate the bridge: remove flag + kill daemon."""
    if os.path.exists(FLAG_FILE):
        os.remove(FLAG_FILE)

    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 9)  # SIGKILL on Windows
            print(f"Daemon stopped (PID {pid})")
        except (ProcessLookupError, OSError):
            print("Daemon was already stopped")
        os.remove(PID_FILE)
    else:
        print("No daemon running")

    print("Bridge INACTIVE")


def cmd_status(_args: argparse.Namespace) -> None:
    """Show bridge status."""
    active = os.path.exists(FLAG_FILE)
    daemon_running = False
    pid = None

    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            daemon_running = True
        except (ProcessLookupError, OSError):
            daemon_running = False

    print(f"Bridge:  {'ACTIVE' if active else 'inactive'}")
    print(f"Daemon:  {'running (PID ' + str(pid) + ')' if daemon_running else 'stopped'}")

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        has_notify = bool(config.get("notify_me_access_code"))
        print(f"Notify:  {'configured' if has_notify else 'not set (Alexa wont speak)'}")
        print(f"Window:  '{config.get('window_title', 'claude')}'")


def cmd_notify(args: argparse.Namespace) -> None:
    """Send a summary to Alexa + store in DynamoDB. Called by Claude, not the user."""
    summary = args.summary
    if not summary:
        return

    if not os.path.exists(CONFIG_FILE):
        return

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    # 1. Push notification (ntfy.sh or Notify Me)
    ntfy_topic = config.get("ntfy_topic", "")
    if ntfy_topic:
        _send_ntfy(summary, ntfy_topic, config.get("ntfy_server", "https://ntfy.sh"))

    access_code = config.get("notify_me_access_code", "")
    if access_code:
        _send_notify_me(summary, access_code)

    # 2. DynamoDB (for "Alexa, ask Claude what happened")
    _store_result(summary, config)


def cmd_logs(_args: argparse.Namespace) -> None:
    """Tail the daemon log."""
    if not os.path.exists(LOG_FILE):
        print("No log file yet. Start the bridge first.")
        return
    os.execvp("tail", ["tail", "-f", LOG_FILE])


# ── Helpers ───────────────────────────────────────────────────────────


def _send_ntfy(summary: str, topic: str, server: str = "https://ntfy.sh") -> None:
    """Send push notification via ntfy.sh."""
    import contextlib

    import httpx

    with contextlib.suppress(Exception):
        httpx.post(
            f"{server}/{topic}",
            content=summary,
            headers={"Title": "Claude Code", "Tags": "robot"},
            timeout=10,
        )


def _send_notify_me(summary: str, access_code: str) -> None:
    """Send notification to Alexa via Notify Me API."""
    import contextlib

    import httpx

    with contextlib.suppress(Exception):
        httpx.post(
            "https://api.notifymyecho.com/v1/NotifyMe",
            json={"notification": f"Claude: {summary}", "accessCode": access_code},
            timeout=10,
        )


def _store_result(summary: str, config: dict) -> None:
    """Store the result in DynamoDB for the GetResult Alexa intent."""
    import boto3

    try:
        dynamodb = boto3.resource(
            "dynamodb", region_name=config.get("aws_region", "us-east-1")
        )
        table = dynamodb.Table(config.get("results_table", "claude-bridge-results"))
        now = datetime.now(UTC)
        table.put_item(
            Item={
                "pk": "user#default",
                "sk": int(now.timestamp() * 1000),
                "summary": summary,
                "timestamp": now.isoformat(),
            }
        )
    except Exception:
        pass  # Don't break Claude if DynamoDB write fails


def _config_from_env(env_path: str) -> dict:
    """Read key=value pairs from a .env file."""
    config: dict[str, str] = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key_lower = key.strip().lower()
            config[key_lower] = value.strip()
    return config


def _add_claude_md_instruction() -> None:
    """Add the Alexa Bridge section to ~/.claude/CLAUDE.md."""
    if not os.path.exists(CLAUDE_MD):
        print(f"CLAUDE.md not found at {CLAUDE_MD} — skipping")
        return

    with open(CLAUDE_MD) as f:
        content = f.read()

    if BRIDGE_MARKER_START in content:
        print("CLAUDE.md already has bridge instruction — skipping")
        return

    with open(CLAUDE_MD, "a") as f:
        f.write("\n" + BRIDGE_INSTRUCTION)

    print(f"Added bridge instruction to {CLAUDE_MD}")


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="alexa-bridge",
        description="Voice-control your Claude Code REPL via Alexa",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("install", help="One-time setup")
    sub.add_parser("start", help="Activate bridge")
    sub.add_parser("stop", help="Deactivate bridge")
    sub.add_parser("status", help="Show bridge status")
    sub.add_parser("logs", help="Tail daemon log")

    notify_p = sub.add_parser("notify", help="Send summary to Alexa (called by Claude)")
    notify_p.add_argument("summary", help="One-sentence summary")

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "notify": cmd_notify,
        "logs": cmd_logs,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
