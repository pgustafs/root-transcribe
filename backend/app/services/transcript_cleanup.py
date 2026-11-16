"""A cleaned copy of a transcript: a language model corrects what the speech
model misheard, line by line, and nothing else.

The model sees the transcript as numbered lines and answers with the lines it
would change. It cannot be trusted to stay inside its instructions, so every
proposed line is checked here before it is taken:

* a line keeps its number, speaker and time: only its text can change, and
  words never move between lines;
* a proposal that changes more than a few words of a line is a rewrite, not a
  correction, and is dropped (the original line stands);
* a correction replaces a misheard word by the word that was said, which
  sounds like it: a proposal that adds or removes words, or swaps a word for
  an unrelated one ("grind" for "wicket"), is dropped;
* a line of one or two words gives no context to correct from: only its
  punctuation and capitals may change.

The original transcript is never modified; the result is a separate text per
line. Pure logic around one dependency (anything with
``chat_sync(instructions=..., text=...)``), so it can be tested without a model.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Lines sent in one request, and their total size. Small enough that the
#: answer (at most the same lines again) fits any served context window.
MAX_LINES = 40
MAX_CHARS = 6000
#: Lines before a part that the model reads but may not change.
CONTEXT_LINES = 4
#: At most this share of a line's words may differ in a correction (at least
#: one word); more is a rewrite.
MAX_CHANGED_SHARE = 0.25
#: A line this short has no context of its own to correct from.
MIN_WORDS_TO_CORRECT = 3
#: A misheard word resembles the word that was said. Below this likeness of
#: their letters (0-1) the model replaced a word by another one it prefers.
MIN_LIKENESS = 0.4

INSTRUCTIONS = """\
You correct an automatic speech-recognition transcript of a meeting. The \
speech model mishears words; you fix those mistakes and nothing else.

You get numbered lines, one per speaker turn: `number| speaker: text`. Lines \
under "Context" are earlier lines for understanding only.

Answer with the lines you change, one per line, as `number| corrected text` \
(the text only, without the speaker). Leave out every line that needs no \
change. If no line needs a change, answer exactly: NONE

What to correct:
- a word that makes no sense where a similar-sounding word obviously fits \
(for example a name, a product or a technical term the speech model did not \
know);
- spelling, capital letters in names, and punctuation;
- words wrongly split or joined.

What you must not do:
- do not rephrase, shorten, expand, translate or improve the style: this is \
speech, so keep filler words, repetitions, broken-off sentences and dialect \
exactly as they are;
- do not add words that were not said and do not remove words that were;
- do not move words from one line to another, and do not merge or split lines;
- do not guess: if you are not sure a word is wrong, leave the line out.

Keep the language of each line. Answer with the changed lines only, no \
explanations.
"""


@dataclass(frozen=True)
class Line:
    """One transcript line as the model sees it."""

    speaker: str
    text: str


def _key(word: str) -> str:
    return re.sub(r"[^\w]+", "", word, flags=re.UNICODE).casefold()


def _keys(text: str) -> list[str]:
    return [key for key in (_key(word) for word in text.split()) if key]


def parts(lines: Sequence[Line]) -> list[tuple[int, int]]:
    """``(start, end)`` index ranges: whole lines, at most ``MAX_LINES`` and
    about ``MAX_CHARS`` each (a single longer line is its own part)."""
    out: list[tuple[int, int]] = []
    start, size = 0, 0
    for index, line in enumerate(lines):
        length = len(line.text) + len(line.speaker) + 8
        if index > start and (index - start >= MAX_LINES or size + length > MAX_CHARS):
            out.append((start, index))
            start, size = index, 0
        size += length
    if len(lines) > start:
        out.append((start, len(lines)))
    return out


def request_text(lines: Sequence[Line], start: int, end: int) -> str:
    """The numbered lines of one part, with a little context in front."""

    def numbered(index: int) -> str:
        line = lines[index]
        return f"{index + 1}| {line.speaker}: {line.text.strip()}"

    context = [numbered(i) for i in range(max(0, start - CONTEXT_LINES), start)]
    body = [numbered(i) for i in range(start, end)]
    head = ["Context (do not change):", *context, ""] if context else []
    return "\n".join([*head, "Lines to correct:", *body])


_ANSWER_LINE = re.compile(r"^\s*(\d+)\s*\|\s?(.*)$")


def parse_answer(answer: str, start: int, end: int) -> dict[int, str]:
    """``{line index: proposed text}`` for the lines of this part the model
    answered. Anything else it wrote (explanations, other numbers, a repeated
    speaker name) is ignored or stripped."""
    proposals: dict[int, str] = {}
    for raw in answer.splitlines():
        match = _ANSWER_LINE.match(raw.strip().strip("`"))
        if not match:
            continue
        index = int(match.group(1)) - 1
        text = match.group(2).strip()
        if start <= index < end and text and index not in proposals:
            proposals[index] = text
    return proposals


def _strip_speaker(text: str, speaker: str) -> str:
    """The model sometimes repeats ``speaker:`` in front of its text."""
    prefix = f"{speaker}:"
    if speaker and text.casefold().startswith(prefix.casefold()):
        return text[len(prefix) :].strip()
    return text


def accept(original: str, proposed: str) -> bool:
    """Whether ``proposed`` is a correction of ``original`` and not a rewrite.

    Punctuation and capitals may always change, and words may be split or
    joined ("i kring" / "ikring"). Otherwise words may change only in a line
    long enough to give context, and only as corrections of what was
    misheard:

    * a stretch of words is replaced by words that resemble it
      (``MIN_LIKENESS``); nothing is added and nothing is removed;
    * at most ``MAX_CHANGED_SHARE`` of the line's words change (at least
      one), counting a replaced stretch by its longer side.

    Measured on read Swedish with a known text, these rules keep the
    corrections that lower the error rate and drop most of the changes that
    raise it (a valid word swapped for one the model prefers, a word added
    for grammar).
    """
    before, after = _keys(original), _keys(proposed)
    if not after:
        return False
    if before == after:
        return proposed.strip() != original.strip()
    if len(before) < MIN_WORDS_TO_CORRECT:
        return False
    changed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, before, after, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        was, now = "".join(before[i1:i2]), "".join(after[j1:j2])
        if was == now:
            continue  # the same letters, split or joined differently
        if not was or not now:
            return False  # a word added or removed
        if SequenceMatcher(None, was, now, autojunk=False).ratio() < MIN_LIKENESS:
            return False  # another word, not the same one heard better
        changed += max(i2 - i1, j2 - j1)
    return changed <= max(1, math.floor(len(before) * MAX_CHANGED_SHARE))


@dataclass(frozen=True)
class Cleanup:
    """The outcome: ``texts[i]`` is line ``i`` cleaned, or None when the line
    stands as it is. ``rejected`` counts proposals that were rewrites."""

    texts: list[str | None]
    rejected: int
    model: str | None


def clean(
    runner,
    lines: Sequence[Line],
    *,
    on_step: Callable[[str], None] = lambda step: None,
) -> Cleanup:
    """Ask the model part by part and keep the proposals that pass
    :func:`accept`. Errors from the runner propagate."""
    texts: list[str | None] = [None] * len(lines)
    rejected = 0
    model: str | None = None
    ranges = parts(lines)
    for number, (start, end) in enumerate(ranges, start=1):
        on_step(f"part:{number}/{len(ranges)}")
        answer = runner.chat_sync(
            instructions=INSTRUCTIONS, text=request_text(lines, start, end)
        )
        model = answer.model or model
        for index, proposed in parse_answer(answer.text, start, end).items():
            proposed = _strip_speaker(proposed, lines[index].speaker)
            if accept(lines[index].text, proposed):
                texts[index] = proposed
            elif _keys(proposed) != _keys(lines[index].text):
                rejected += 1
    return Cleanup(texts=texts, rejected=rejected, model=model)
