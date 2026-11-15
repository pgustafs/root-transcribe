"""What the language model adds to a note, and the checks on its answers.

Two questions are asked about a note:

* the image: what can be read in it (text, formulas, code), as Markdown;
* the words: a short title, the transcript without fillers, the key terms
  with a definition each and, for a note marked "unclear", a plain
  explanation.

The model's answers are checked here before anything is stored. The tidy
transcript in particular must stay the speaker's words: a version that lost or
gained too much is thrown away and the original is shown instead.

Pure logic around one dependency, the agent runner, like ``speaker_names``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SLIDE_INSTRUCTIONS = """\
You are given one frame from a lecture video or a shared screen. Write down
what can be read in it, as Markdown:
- headings and text as they are written, in the same language;
- code in fenced code blocks, exactly as shown;
- formulas in LaTeX between $ signs;
- a table as a Markdown table;
- a diagram or chart in one or two sentences that say what it shows.
Leave out the video player, browser and window controls, and anything that is
not part of the content. Do not explain or add anything that is not visible.
If the frame has no readable content (a person talking, a blank screen),
answer with exactly: NONE
"""

NOTE_INSTRUCTIONS = """\
You are helping a learner with one note taken while watching a lecture or a
meeting. You get what was said (a speech-to-text transcript, which may have
errors) and sometimes the text of the slide that was showing.

Answer with JSON only, no other text, in this form:
{"title": "...", "clean_text": "...", "terms": [{"term": "...",
"definition": "..."}], "explanation": "..."}

- "title": what this note is about, at most 8 words, no ending period.
- "clean_text": the transcript made readable. Remove filler words, false
  starts and repetitions, fix punctuation and sentence breaks, and correct
  words that speech-to-text clearly got wrong (use the slide text for
  spelling). Keep every statement and the speaker's own wording. Do not
  summarize, shorten or add anything. Empty string if there is no transcript.
- "terms": up to 6 technical terms or names that a learner must know to follow
  this note, each with a definition of one or two sentences as the term is
  used here. An empty list is a good answer.
- "explanation": only if the input says the learner did not understand this
  part. Then explain what is being said in plain words, in at most 120 words,
  with a small example if it helps. Otherwise an empty string.

Write everything in the language of the transcript (or of the slide when there
is no transcript).
"""

MAX_TITLE = 120
MAX_TERMS = 8
MAX_TERM = 80
MAX_DEFINITION = 500
MAX_EXPLANATION = 2000
MAX_SLIDE_TEXT = 12000
MAX_INPUT_CHARS = 24000
#: An image larger than this is not sent to the model.
MAX_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class NoteAnswer:
    title: str | None = None
    clean_text: str | None = None
    terms: list[dict[str, str]] = field(default_factory=list)
    explanation: str | None = None


def build_input(*, text: str | None, slide_text: str | None, unclear: bool) -> str:
    parts: list[str] = []
    if unclear:
        parts.append("The learner marked this part: did not understand.")
    if (text or "").strip():
        parts.append("Transcript:\n" + text.strip())
    else:
        parts.append("Transcript: (none)")
    if (slide_text or "").strip():
        parts.append("Text on the slide:\n" + slide_text.strip())
    return "\n\n".join(parts)[:MAX_INPUT_CHARS]


def parse_slide(answer: str) -> str | None:
    """The slide text, or None when the model saw nothing to read."""
    text = _unfence(answer).strip()
    if not text or text.upper().strip(" .") == "NONE":
        return None
    return text[:MAX_SLIDE_TEXT]


def _unfence(answer: str) -> str:
    """Models often wrap the whole answer in one code fence."""
    match = re.fullmatch(
        r"\s*```[a-zA-Z]*\n(.*?)\n```\s*", answer or "", flags=re.DOTALL
    )
    return match.group(1) if match else (answer or "")


def _json_object(answer: str) -> dict:
    text = _unfence(answer).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model did not answer with JSON.")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("The model did not answer with a JSON object.")
    return value


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def faithful(original: str, cleaned: str) -> bool:
    """Is ``cleaned`` still the same talk? Fillers go, so it may be shorter,
    but a summary (far shorter) or an invention (longer) is refused."""
    before, after = _words(original), _words(cleaned)
    if before == 0 or after == 0:
        return False
    return 0.55 * before <= after <= 1.15 * before + 3


def parse_note(answer: str, *, text: str | None, unclear: bool) -> NoteAnswer:
    """Check the model's JSON. Raises ValueError when it is not usable at all;
    single fields that fail their check are dropped."""
    data = _json_object(answer)
    title = " ".join(str(data.get("title") or "").split()).strip(" .")
    cleaned = str(data.get("clean_text") or "").strip()
    original = (text or "").strip()
    clean_text = cleaned if original and faithful(original, cleaned) else None
    if clean_text == original:
        clean_text = None
    terms: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_terms = data.get("terms")
    for item in raw_terms if isinstance(raw_terms, list) else []:
        if not isinstance(item, dict):
            continue
        term = " ".join(str(item.get("term") or "").split())
        definition = " ".join(str(item.get("definition") or "").split())
        key = term.casefold()
        if not term or not definition or key in seen or len(term) > MAX_TERM:
            continue
        seen.add(key)
        terms.append({"term": term, "definition": definition[:MAX_DEFINITION]})
        if len(terms) == MAX_TERMS:
            break
    explanation = str(data.get("explanation") or "").strip()
    return NoteAnswer(
        title=title[:MAX_TITLE] or None,
        clean_text=clean_text,
        terms=terms,
        explanation=explanation[:MAX_EXPLANATION] if unclear and explanation else None,
    )
