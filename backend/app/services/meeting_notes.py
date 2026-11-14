"""Turning a transcript into a note: prompts, splitting long meetings, cleanup.

Pure logic around one dependency, the agent runner (anything with
``run_sync(instructions=..., text=...)``), so it can be tested without a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from app.services.agent_runner import AgentAnswer
from app.services.note_defaults import OUTPUT_RULES
from app.services.transcript_export import ExportSegment, _clock

# The agent runtime (CUGA) puts its own prompt in front of every run, and the
# answer needs room too. Measured: the prompt is about 4,750 tokens.
AGENT_PROMPT_TOKENS = 5000
ANSWER_TOKENS = 2000
# Conservative: English runs near 4 characters per token, Swedish lower.
CHARS_PER_TOKEN = 3
# Below this the transcript would be cut into slivers and the merge step could
# not hold their notes; better to say so than to return a poor document slowly.
MIN_CONTEXT_TOKENS = 16000


class ContextTooSmall(ValueError):
    """The model's context window cannot hold a useful part of a transcript."""


def input_budget_chars(
    context_tokens: int | None, instructions: str, max_chars: int
) -> int:
    """How many transcript characters fit in one run.

    ``max_chars`` is the configured ceiling; a known context window lowers it
    so prompt + instructions + transcript + answer fit. Unknown window: trust
    the setting.
    """
    if context_tokens is None:
        return max_chars
    if context_tokens < MIN_CONTEXT_TOKENS:
        raise ContextTooSmall(
            f"The language model is served with a context window of "
            f"{context_tokens:,} tokens, which is too small for meeting notes: "
            f"the agent's own prompt takes about {AGENT_PROMPT_TOKENS:,} and the "
            f"transcript and the answer need the rest. Serve the model with at "
            f"least {MIN_CONTEXT_TOKENS:,} tokens of context (32,000 recommended)."
        )
    room = (
        context_tokens
        - AGENT_PROMPT_TOKENS
        - ANSWER_TOKENS
        - (len(instructions) + len(OUTPUT_RULES)) // CHARS_PER_TOKEN
    )
    return max(2000, min(max_chars, room * CHARS_PER_TOKEN))


def transcript_lines(segments: list[ExportSegment]) -> list[str]:
    """``[H:MM:SS] Speaker: text`` — one line per speaker turn."""
    return [f"[{_clock(s.start_ms)}] {s.speaker}: {s.text.strip()}" for s in segments]


def split_parts(lines: list[str], max_chars: int) -> list[str]:
    """Group whole lines into parts of at most ``max_chars`` characters.

    A single line longer than the limit becomes its own part (cut to the
    limit): better a slightly shortened monologue than a failed note.
    """
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = line[:max_chars]
        if current and size + len(line) + 1 > max_chars:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


_FENCE = re.compile(r"\A```(?:markdown|md)?[ \t]*\n(.*?)\n```[ \t]*\Z", re.S)


def clean_markdown(text: str) -> str:
    """Models often wrap the whole document in a code fence; unwrap it."""
    text = text.strip()
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


def write_note(
    runner,
    *,
    instructions: str,
    title: str,
    lines: list[str],
    max_chars: int,
    on_step: Callable[[str], None] = lambda step: None,
) -> AgentAnswer:
    """Run the agent over the transcript; long meetings go part by part.

    ``on_step`` receives ``write`` for a single pass, or ``part:2/4`` and
    ``merge`` for a long meeting.
    """
    full = f"{instructions.strip()}\n\n{OUTPUT_RULES}"
    parts = split_parts(lines, max_chars)
    if len(parts) <= 1:
        on_step("write")
        answer = runner.run_sync(
            instructions=full,
            text=(
                f"Meeting: {title}\n\nTranscript:\n{parts[0] if parts else ''}\n\n"
                "Write the document described in your instructions."
            ),
        )
        return AgentAnswer(text=clean_markdown(answer.text), model=answer.model)

    partial: list[str] = []
    model: str | None = None
    for number, part in enumerate(parts, start=1):
        on_step(f"part:{number}/{len(parts)}")
        answer = runner.run_sync(
            instructions=full,
            text=(
                f"Meeting: {title}\n\nThe meeting is long, so you get it in "
                f"{len(parts)} consecutive parts. This is part {number}. Apply "
                "your instructions to this part only; the parts are combined "
                f"afterwards.\n\nTranscript, part {number} of {len(parts)}:\n{part}"
            ),
        )
        partial.append(clean_markdown(answer.text))
        model = answer.model or model
    on_step("merge")
    merged = runner.run_sync(
        instructions=full,
        text=(
            f"Meeting: {title}\n\nBelow are {len(parts)} documents, each written "
            "from one consecutive part of the same meeting by following your "
            "instructions. Combine them into ONE document, as if it had been "
            "written from the whole transcript: keep the structure your "
            "instructions ask for, merge items that repeat, keep the order of "
            "events, and drop nothing that matters.\n\n"
            + "\n\n".join(
                f"--- Part {number} of {len(parts)} ---\n{text}"
                for number, text in enumerate(partial, start=1)
            )
        ),
    )
    return AgentAnswer(text=clean_markdown(merged.text), model=merged.model or model)
