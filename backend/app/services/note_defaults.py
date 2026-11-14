"""The note agents shipped with the app.

Their text is the default a built-in agent returns to with "Reset to default".
Instructions are written for a model that receives the whole transcript as
``[H:MM:SS] Speaker: text`` lines.
"""

# ruff: noqa: E501 - prose for the model reads best unwrapped
from __future__ import annotations

from dataclasses import dataclass

# Appended to every agent's instructions at run time (not stored, not editable):
# the output contract the app depends on.
OUTPUT_RULES = """\
Output rules (always apply):
- Answer with the finished document only, in Markdown. No preamble, no closing remarks, no code fences around the document.
- Write in the language the meeting was held in, unless the instructions above name another language.
- Use only what is said in the transcript. If something is not mentioned, leave it out or write that it was not discussed; never invent names, dates or numbers.
- Refer to people by the speaker names used in the transcript."""


@dataclass(frozen=True)
class BuiltinAgent:
    key: str
    name: str
    description: str
    instructions: str


BUILTIN_AGENTS: tuple[BuiltinAgent, ...] = (
    BuiltinAgent(
        key="summary",
        name="Summary",
        description="What the meeting was about, what was decided and what is still open.",
        instructions="""\
Write a summary of this meeting for someone who was not there.

Use these sections, each as a level-2 heading, and leave out a section that would be empty:
- Summary: three to six sentences on what the meeting was about and how it ended.
- Key points: a bullet list of the main things discussed, most important first.
- Decisions: a bullet list of what was decided, with who decided or agreed where that is clear.
- Open questions: a bullet list of what was left unresolved.

Keep it short and factual. Do not list tasks here.""",
    ),
    BuiltinAgent(
        key="tasks",
        name="Task list",
        description="The things people agreed to do, with owner and deadline where mentioned.",
        instructions="""\
List the tasks that came out of this meeting: everything someone agreed, offered or was asked to do.

Write a Markdown checklist under the level-2 heading "Tasks", one task per line, in this form:
- [ ] **Owner** — what to do (due: date or time frame, only if one was mentioned)

Use "Unassigned" as the owner when nobody took the task. Start each task with a verb.
After the list, add a level-2 heading "Not decided" with a bullet list of proposed tasks that nobody committed to; leave it out if there are none.
If the meeting produced no tasks, say so in one sentence.""",
    ),
)

BUILTIN_BY_KEY = {agent.key: agent for agent in BUILTIN_AGENTS}
