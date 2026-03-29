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

from .config import CONFIG_FILE, FLAG_FILE, LOG_FILE, PENDING_NOTIFY
from .keyboard import inject_command

logger = logging.getLogger(__name__)


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def run() -> None:
    """Main loop: poll SQS → inject into Claude terminal."""
    config = _load_config()
    queue_url = config["command_queue_url"]
    region = config.get("aws_region", "us-east-1")
    window_title = config.get("window_title")
    window_class = config.get("window_class", "CASCADIA_HOSTING_WINDOW_CLASS")
    exclude_titles = config.get("exclude_titles", ["Visual Studio Code"])

    sqs = boto3.client("sqs", region_name=region)

    logger.info("Daemon started — polling %s", queue_url)
    logger.info(
        "Window match: class=%s, title=%s, excluding=%s",
        window_class,
        window_title,
        exclude_titles,
    )

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
            try:
                body = json.loads(msg["Body"])
                command = body["command"]
                logger.info("Alexa says: %s", command)

                injected = inject_command(
                    command,
                    window_title,
                    exclude_titles=exclude_titles,
                    window_class=window_class,
                )
                if injected:
                    # Mark that an Alexa command was injected — Claude checks this
                    with open(PENDING_NOTIFY, "w") as f:
                        f.write(command)
                    logger.info("Command injected into Claude terminal")
                else:
                    logger.warning("Failed to inject — window not found")
            except Exception:
                logger.exception("Failed to process message")
            finally:
                sqs.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=msg["ReceiptHandle"],
                )

    logger.info("Flag file removed — daemon shutting down")


def main() -> None:
    """Entry point when run as `python -m alexa_claude_bridge.daemon`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )

    max_retries = 5
    retry_delay = 3

    for attempt in range(1, max_retries + 1):
        try:
            run()
            break  # Clean exit (flag file removed)
        except KeyboardInterrupt:
            logger.info("Daemon interrupted")
            break
        except Exception:
            logger.exception("Daemon crashed (attempt %d/%d)", attempt, max_retries)
            if attempt < max_retries and os.path.exists(FLAG_FILE):
                logger.info("Restarting in %ds...", retry_delay)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)
            else:
                logger.error("Max retries reached or flag removed — exiting")
                sys.exit(1)


if __name__ == "__main__":
    main()
