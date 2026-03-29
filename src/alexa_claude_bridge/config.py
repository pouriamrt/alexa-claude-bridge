"""Configuration constants for the Alexa-Claude bridge."""

from __future__ import annotations

import os

BRIDGE_DIR = os.path.expanduser("~/.claude-bridge")
FLAG_FILE = os.path.join(BRIDGE_DIR, "active")
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")
PID_FILE = os.path.join(BRIDGE_DIR, "daemon.pid")
LOG_FILE = os.path.join(BRIDGE_DIR, "daemon.log")
