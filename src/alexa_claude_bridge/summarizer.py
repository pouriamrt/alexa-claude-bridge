"""Summarize Claude Code output for Alexa speech.

Alexa has a comfortable listening limit of ~30 seconds (~400 words).
Claude Code often produces long, detailed output with code blocks, file paths,
and formatting that doesn't translate well to speech.

This module converts Claude's output into concise, speakable summaries.
"""

from __future__ import annotations

# Maximum characters for Alexa speech (comfortable listening = ~30 seconds)
MAX_SPEECH_CHARS = 600


def summarize_for_alexa(output: str, is_error: bool = False) -> str:
    """Convert Claude Code output into a concise, speakable summary for Alexa.

    TODO: Implement your summarization strategy here.

    Consider these approaches:
    - Simple truncation: take the first/last N characters
    - Smart extraction: grab key lines (e.g., test results, error messages, git status)
    - Pattern matching: detect common output patterns and extract the important bits
    - LLM summarization: call Claude API to summarize (adds latency + cost)

    Trade-offs to think about:
    - Truncation is fast but may cut off the important part
    - Pattern matching handles known cases well but misses novel output
    - LLM summarization is the most flexible but adds 2-5s latency and API cost

    Args:
        output: Raw text output from Claude Code.
        is_error: Whether the command failed.

    Returns:
        A short, speakable string (under MAX_SPEECH_CHARS).
    """
    # ── Your implementation goes here ──
    # For now, a basic first-pass that you can improve:
    prefix = "Error: " if is_error else ""

    cleaned = _strip_for_speech(output)

    if len(cleaned) <= MAX_SPEECH_CHARS:
        return f"{prefix}{cleaned}"

    # Truncate with ellipsis — replace this with something smarter!
    truncated = cleaned[: MAX_SPEECH_CHARS - 30]
    return f"{prefix}{truncated}... That's the summary. Ask me to read the full result for more."


def _strip_for_speech(text: str) -> str:
    """Remove elements that don't work in spoken form."""
    lines = text.splitlines()
    filtered: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Skip empty lines, code fences, horizontal rules
        if not stripped or stripped.startswith("```") or stripped.startswith("---"):
            continue
        # Skip pure file paths that look like diffs
        if stripped.startswith("+++") or stripped.startswith("---"):
            continue
        filtered.append(stripped)

    return ". ".join(filtered)
