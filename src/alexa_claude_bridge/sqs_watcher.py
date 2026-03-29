"""SQS watcher — polls for Alexa commands, injects them into the PTY, captures output."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import boto3

from .notifier import notify_alexa
from .summarizer import summarize_for_alexa
from .terminal import strip_ansi

if TYPE_CHECKING:
    import threading

    from winpty import PTY

    from .config import Config

logger = logging.getLogger(__name__)

# How long to wait with no new output before considering Claude "done"
IDLE_TIMEOUT_SECS = 5
# Initial delay after injecting a command (let Claude start processing)
INITIAL_DELAY_SECS = 2
# Max time to wait for a command to finish before giving up
MAX_WAIT_SECS = 300


class SQSWatcher:
    """Watches SQS for Alexa commands and injects them into a running PTY."""

    def __init__(
        self,
        config: Config,
        pty: PTY,
        output_buffer: list[str],
        buffer_lock: threading.Lock,
        capturing: threading.Event,
    ) -> None:
        self.config = config
        self.pty = pty
        self.output_buffer = output_buffer
        self.buffer_lock = buffer_lock
        self.capturing = capturing
        self.sqs = boto3.client("sqs", region_name=config.aws_region)
        self.dynamodb = boto3.resource("dynamodb", region_name=config.aws_region)
        self.table = self.dynamodb.Table(config.results_table)

    def run(self) -> None:
        """Main polling loop. Runs until PTY exits."""
        logger.info("SQS watcher started — listening for Alexa commands")

        while self.pty.isalive():
            try:
                resp = self.sqs.receive_message(
                    QueueUrl=self.config.command_queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=20,
                )
            except Exception:
                logger.exception("SQS poll failed, retrying...")
                time.sleep(5)
                continue

            for msg in resp.get("Messages", []):
                self._process_message(msg)

    def _process_message(self, msg: dict) -> None:
        """Inject a command into the PTY, capture output, store result."""
        body = json.loads(msg["Body"])
        command = body["command"]
        command_id = body["command_id"]

        logger.info("Alexa command [%s]: %s", command_id[:8], command)

        # ── 1. Clear buffer and start capturing ──
        with self.buffer_lock:
            self.output_buffer.clear()
        self.capturing.set()

        # ── 2. Inject command into the PTY (like typing + Enter) ──
        self.pty.write(f"{command}\r\n".encode())

        # ── 3. Wait for output to stabilize ──
        raw_output = self._wait_for_output()
        self.capturing.clear()

        # ── 4. Clean up and summarize ──
        clean_output = strip_ansi(raw_output)
        summary = summarize_for_alexa(clean_output)

        logger.info("Command [%s] done — %d chars captured", command_id[:8], len(clean_output))

        # ── 5. Store in DynamoDB ──
        now = datetime.now(UTC)
        self.table.put_item(
            Item={
                "pk": "user#default",
                "sk": int(now.timestamp() * 1000),
                "command_id": command_id,
                "command": command,
                "result": clean_output[:10_000],
                "summary": summary,
                "timestamp": now.isoformat(),
            }
        )

        # ── 6. Delete from SQS ──
        self.sqs.delete_message(
            QueueUrl=self.config.command_queue_url,
            ReceiptHandle=msg["ReceiptHandle"],
        )

        # ── 7. Proactive Alexa notification ──
        if self.config.notify_me_access_code:
            notify_alexa(f"Claude finished. {summary}", self.config.notify_me_access_code)

    def _wait_for_output(self) -> str:
        """Wait until Claude stops producing output (idle timeout)."""
        time.sleep(INITIAL_DELAY_SECS)

        prev_len = 0
        elapsed = 0

        while elapsed < MAX_WAIT_SECS:
            time.sleep(IDLE_TIMEOUT_SECS)
            elapsed += IDLE_TIMEOUT_SECS

            with self.buffer_lock:
                current_len = sum(len(chunk) for chunk in self.output_buffer)

            if current_len > 0 and current_len == prev_len:
                # No new output for IDLE_TIMEOUT_SECS — Claude is done
                break
            prev_len = current_len

        with self.buffer_lock:
            return "".join(self.output_buffer)
