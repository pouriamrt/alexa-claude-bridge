"""Configuration constants for the Alexa-Claude bridge."""

from __future__ import annotations

import os

BRIDGE_DIR = os.path.expanduser("~/.claude-bridge")
FLAG_FILE = os.path.join(BRIDGE_DIR, "active")
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
PID_FILE = os.path.join(BRIDGE_DIR, "daemon.pid")
LOG_FILE = os.path.join(BRIDGE_DIR, "daemon.log")
NOTIFY_SCRIPT = os.path.join(BRIDGE_DIR, "notify")
PENDING_NOTIFY = os.path.join(BRIDGE_DIR, "pending-notify")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
