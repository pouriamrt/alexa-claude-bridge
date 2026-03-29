"""Configuration for the Alexa-Claude bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """All settings come from environment variables with sensible defaults."""

    aws_region: str = os.getenv("AWS_REGION", "us-east-1")
    command_queue_url: str = os.getenv("COMMAND_QUEUE_URL", "")
    results_table: str = os.getenv("RESULTS_TABLE", "claude-bridge-results")
    claude_timeout: int = int(os.getenv("CLAUDE_TIMEOUT", "300"))

    # Optional: Notify Me (notifymyecho.com) access code for proactive Alexa speech
    notify_me_access_code: str = os.getenv("NOTIFY_ME_ACCESS_CODE", "")

    def validate(self) -> None:
        if not self.command_queue_url:
            msg = "COMMAND_QUEUE_URL environment variable is required"
            raise ValueError(msg)
