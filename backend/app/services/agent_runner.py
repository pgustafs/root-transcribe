"""Client for the agent-runner container (CUGA behind a small HTTP service).

The worker sends instructions and text and gets Markdown back. Kept behind a
getter so tests can replace it, like the transcription service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import log_operation

logger = logging.getLogger("app.services.agent_runner")


class AgentRunError(RuntimeError):
    """The agent could not produce an answer; the message is safe to show."""


@dataclass(frozen=True)
class AgentAnswer:
    text: str
    model: str | None = None


class AgentRunner:
    def run_sync(self, *, instructions: str, text: str) -> AgentAnswer:
        """One agent run. Raises :class:`AgentRunError` with a readable reason."""
        timeout = float(settings.NOTES_TIMEOUT_SECONDS)
        try:
            with log_operation(logger, "agent.run", chars=len(text)):
                response = httpx.post(
                    f"{settings.AGENT_RUNNER_URL.rstrip('/')}/run",
                    json={
                        "instructions": instructions,
                        "input": text,
                        "timeout_seconds": timeout,
                    },
                    # The runner enforces the run's own limit; allow it to answer.
                    timeout=httpx.Timeout(timeout + 30, connect=10),
                )
        except httpx.HTTPError as exc:
            raise AgentRunError(
                f"The agent service could not be reached ({type(exc).__name__})."
            ) from None
        if response.status_code != 200:
            raise AgentRunError(
                f"The agent service answered HTTP {response.status_code}."
            )
        body = response.json()
        if body.get("error"):
            raise AgentRunError(str(body["error"])[:1500])
        answer = str(body.get("answer") or "").strip()
        if not answer:
            raise AgentRunError("The model returned an empty answer.")
        return AgentAnswer(text=answer, model=body.get("model") or None)


_runner: AgentRunner | None = None


def get_agent_runner() -> AgentRunner:
    """Return the process-wide agent runner client (worker)."""
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner
