"""Finding speakers' names in what was said.

People introduce themselves ("hi, my name is Peter", "jag heter Anna") or are
addressed by name. A language model reads the transcript and proposes a name
for each speaker that has none, quoting the words it relies on. The proposals
are checked here: a suggestion whose quote is not in the transcript is thrown
away, so a name cannot rest on evidence the model made up. Nothing is applied
automatically; a person accepts each name.

Pure logic around one dependency, the agent runner, like ``meeting_notes``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.services.meeting_notes import split_parts
from app.services.transcript_export import _clock

INSTRUCTIONS = """\
You are given part of a meeting transcript. Each line is one speaker turn:
[time] Speaker: text. Some speakers have no name yet and are called
"Speaker 1", "Speaker 2" and so on. Find the real name of each of those
speakers, using only what is said in the transcript. The meeting may be in
English or Swedish.

Evidence that counts:
- The speaker says their own name: "my name is Peter", "I'm Peter", "this is
  Peter speaking", "Peter here", "jag heter Peter", "mitt namn är Peter", "det
  är Peter", "Peter här". Confidence: high.
- Another speaker addresses them by name and they answer in the next turn, or
  they are handed the word by name ("over to you, Anna", "Anna, vad tycker
  du?") and the next turn is theirs. Confidence: medium.

Rules:
- A name that is only mentioned is not evidence: "I talked to Peter yesterday"
  says nothing about who is speaking.
- When someone is addressed by name, the name belongs to the person spoken to,
  never to the person who says it.
- Do not guess. Leave out every speaker you are not sure about. An empty list
  is a good answer.
- Give the name as it is said: the first name, or the full name when it is said.
- Only speakers called "Speaker N" need a name. Ignore speakers already named.

Answer with JSON only, no other text: a list with at most one object per
speaker, in this form:
[{"speaker": "Speaker 2", "name": "Peter", "quote": "hi my name is Peter",
  "confidence": "high"}]
"quote" is the few words from the transcript that show the name, copied exactly
as they are written there, without the time and speaker prefix.
If no name can be found, answer with: []
"""

_AUTO_LABEL = re.compile(r"^SPEAKER_(\d+)$", re.I)
_CONFIDENCE = {"high": 2, "medium": 1}
MAX_NAME_LENGTH = 60


@dataclass(frozen=True)
class Turn:
    """One transcript segment as this module needs it."""

    label: str  # the detected voice, e.g. SPEAKER_00
    name: str | None  # an existing name (typed or from the directory)
    start_ms: int
    text: str


def friendly(label: str) -> str:
    """SPEAKER_00 → "Speaker 1" (what the model sees and answers with)."""
    match = _AUTO_LABEL.match(label)
    return f"Speaker {int(match.group(1)) + 1}" if match else label


def _squash(text: str) -> str:
    """Lower case, punctuation and repeated spaces removed: what "the same
    words" means when checking a quote against the transcript."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _json_list(answer: str) -> list:
    """The JSON list in a model's answer, tolerating code fences and prose."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def parse_suggestions(answer: str, turns: list[Turn]) -> list[dict]:
    """The usable suggestions in one model answer.

    Kept only when: the speaker is one of this transcript's unnamed speakers,
    the name is a plausible name, and the quote really occurs in the
    transcript. ``start_ms`` is the turn holding the quote, preferring one by
    the speaker in question (a self-introduction) over anyone else's.
    """
    unnamed = {friendly(t.label).lower(): t.label for t in turns if not t.name}
    squashed = [(t, _squash(t.text)) for t in turns]
    best: dict[str, dict] = {}
    for item in _json_list(answer):
        if not isinstance(item, dict):
            continue
        label = unnamed.get(str(item.get("speaker", "")).strip().lower())
        name = " ".join(str(item.get("name", "")).split())
        quote = str(item.get("quote", "")).strip()
        needle = _squash(quote)
        if label is None or not name or len(name) > MAX_NAME_LENGTH or not needle:
            continue
        if (
            friendly(label).lower() == name.lower()
            or f" {_squash(name)} " not in f" {needle} "
        ):
            # "Speaker 2" is not a name, and the quote must contain the name.
            continue
        holders = [t for t, text in squashed if needle in text]
        if not holders:
            continue  # the evidence is not in the transcript: drop it
        own = [t for t in holders if t.label == label]
        confidence = str(item.get("confidence", "")).strip().lower()
        if confidence not in _CONFIDENCE:
            confidence = "medium"
        suggestion = {
            "speaker_label": label,
            "name": name,
            "quote": quote[:300],
            "start_ms": (own or holders)[0].start_ms,
            "confidence": confidence,
            "status": "pending",
        }
        current = best.get(label)
        if current is None or (
            _CONFIDENCE[confidence] > _CONFIDENCE[current["confidence"]]
        ):
            best[label] = suggestion
    return list(best.values())


def transcript_lines(turns: list[Turn]) -> list[str]:
    return [
        f"[{_clock(t.start_ms)}] {t.name or friendly(t.label)}: {t.text.strip()}"
        for t in turns
    ]


def suggest_names(
    runner, turns: list[Turn], *, max_chars: int
) -> tuple[list[dict], str | None]:
    """Ask the model, part by part, and merge what survives the checks.

    Returns the suggestions (one per speaker at most, in order of first
    appearance) and the model name. No model run when every speaker is named.
    """
    if all(t.name for t in turns):
        return [], None
    merged: dict[str, dict] = {}
    model: str | None = None
    for part in split_parts(transcript_lines(turns), max_chars):
        answer = runner.run_sync(instructions=INSTRUCTIONS, text=part)
        model = answer.model or model
        for suggestion in parse_suggestions(answer.text, turns):
            current = merged.get(suggestion["speaker_label"])
            if current is None or (
                _CONFIDENCE[suggestion["confidence"]]
                > _CONFIDENCE[current["confidence"]]
            ):
                merged[suggestion["speaker_label"]] = suggestion
    order = list(dict.fromkeys(t.label for t in turns))
    return sorted(merged.values(), key=lambda s: order.index(s["speaker_label"])), model
