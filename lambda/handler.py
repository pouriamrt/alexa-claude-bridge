"""AWS Lambda handler for the Alexa Claude Bridge skill.

Handles two intents:
  - RunCommandIntent: sends a command to SQS for the local poller to execute
  - GetResultIntent: reads the latest result from DynamoDB and speaks it
"""

from __future__ import annotations

import json
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Key

sqs = boto3.client("sqs")
dynamodb = boto3.resource("dynamodb")

COMMAND_QUEUE_URL = os.environ["COMMAND_QUEUE_URL"]
RESULTS_TABLE = os.environ["RESULTS_TABLE"]


# ─── Alexa Request Routing ───────────────────────────────────────────

def handler(event: dict, context) -> dict:
    """Main Lambda entry point. Routes Alexa request types to handlers."""
    request_type = event["request"]["type"]

    if request_type == "LaunchRequest":
        return _alexa_response(
            "Claude Bridge is ready. Say something like: run the tests, "
            "or commit my changes."
        )

    if request_type == "IntentRequest":
        return _handle_intent(event["request"]["intent"])

    if request_type == "SessionEndedRequest":
        return _alexa_response("Goodbye.", end_session=True)

    return _alexa_response("I didn't understand that. Try saying: run the tests.")


def _handle_intent(intent: dict) -> dict:
    """Route to the correct intent handler."""
    name = intent["name"]

    if name == "RunCommandIntent":
        return _run_command(intent)

    if name == "GetResultIntent":
        return _get_result()

    if name in ("AMAZON.HelpIntent",):
        return _alexa_response(
            "You can tell me to run any Claude Code command. For example: "
            "run the tests, check git status, or fix the linting errors. "
            "After a command finishes, say: what happened."
        )

    if name in ("AMAZON.CancelIntent", "AMAZON.StopIntent"):
        return _alexa_response("Goodbye.", end_session=True)

    # AMAZON.FallbackIntent or unknown
    return _alexa_response(
        "I didn't catch that. Try: tell Claude to run the tests."
    )


# ─── Intent Handlers ─────────────────────────────────────────────────

def _run_command(intent: dict) -> dict:
    """Send a command to SQS for the local poller to pick up."""
    slots = intent.get("slots", {})
    command_slot = slots.get("command", {})
    command = command_slot.get("value")

    if not command:
        return _alexa_response(
            "I didn't hear a command. Try: tell Claude to run the tests.",
            end_session=False,
        )

    command_id = str(uuid.uuid4())

    sqs.send_message(
        QueueUrl=COMMAND_QUEUE_URL,
        MessageBody=json.dumps({
            "command_id": command_id,
            "command": command,
            "timestamp": int(time.time()),
        }),
    )

    return _alexa_response(
        f"Running: {command}. I'll notify you when it's done, "
        f"or ask me what happened.",
        end_session=True,
    )


def _get_result() -> dict:
    """Retrieve the latest result from DynamoDB and speak it."""
    table = dynamodb.Table(RESULTS_TABLE)

    response = table.query(
        KeyConditionExpression=Key("pk").eq("user#default"),
        ScanIndexForward=False,  # newest first
        Limit=1,
    )

    items = response.get("Items", [])
    if not items:
        return _alexa_response("No results yet. Tell me a command to run first.")

    item = items[0]
    command = item.get("command", "unknown command")
    summary = item.get("summary", "No summary available.")
    is_error = item.get("is_error", False)

    status = "failed" if is_error else "finished"
    return _alexa_response(f"Your command, {command}, {status}. {summary}")


# ─── Response Builder ─────────────────────────────────────────────────

def _alexa_response(text: str, end_session: bool = True) -> dict:
    """Build a standard Alexa skill response."""
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": text,
            },
            "card": {
                "type": "Simple",
                "title": "Claude Bridge",
                "content": text,
            },
            "shouldEndSession": end_session,
        },
    }
