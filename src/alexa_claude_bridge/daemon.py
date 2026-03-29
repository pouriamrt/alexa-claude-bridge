"""Background daemon — polls SQS for Alexa commands and types them into Claude.

Runs as a background process (started by `alexa-bridge start`).
Stops when the flag file ~/.claude-bridge/active is removed.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

import boto3

from .keyboard import inject_command

logger = logging.getLogger(__name__)

BRIDGE_DIR = os.path.expanduser("~/.claude-bridge")
FLAG_FILE = os.path.join(BRIDGE_DIR, "active")
CONFIG_FILE = os.path.join(BRIDGE_DIR, "config.json")


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def run() -> None:
    """Main loop: poll SQS → inject into Claude terminal."""
    config = _load_config()
    queue_url = config["command_queue_url"]
    region = config.get("aws_region", "us-east-1")
    window_title = config.get("window_title", "claude")

    sqs = boto3.client("sqs", region_name=region)

    logger.info("Daemon started — polling %s", queue_url)
    logger.info("Looking for window with '%s' in title", window_title)

    while os.path.exists(FLAG_FILE):
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
            )
        except Exception:
            logger.exception("SQS poll failed, retrying in 5s...")
            time.sleep(5)
            continue

        for msg in resp.get("Messages", []):
            body = json.loads(msg["Body"])
            command = body["command"]
            logger.info("Alexa says: %s", command)

            if inject_command(command, window_title):
                logger.info("Command injected into Claude terminal")
            else:
                logger.warning("Failed to inject — window not found")

            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=msg["ReceiptHandle"],
            )

    logger.info("Flag file removed — daemon shutting down")


def main() -> None:
    """Entry point when run as `python -m alexa_claude_bridge.daemon`."""
    log_file = os.path.join(BRIDGE_DIR, "daemon.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )

    try:
        run()
    except KeyboardInterrupt:
        logger.info("Daemon interrupted")
    except Exception:
        logger.exception("Daemon crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
