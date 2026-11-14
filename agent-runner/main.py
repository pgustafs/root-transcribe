"""Agent runner: runs one CUGA agent over one piece of text.

A deliberately small service so the CUGA SDK (large, with process-wide
configuration, able to execute model-written code) stays out of the processes
that hold database and storage credentials. It knows only the LLM endpoint.

    POST /run  {"instructions": "...", "input": "...", "timeout_seconds": 600}
            -> {"answer": "...", "error": null, "model": "...", "seconds": 12.3,
                "llm_calls": 1, "tokens": 5231}
    GET /health

Listens on the pod's loopback only (see deploy/root-transcribe.yaml).
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or "not-needed"
WORK_DIR = os.environ.get("AGENT_RUNNER_WORK_DIR") or tempfile.mkdtemp(prefix="agent-runner-")

# CUGA reads its configuration from the environment when it is imported, and a
# few of its internals build their own model client from these instead of using
# the one passed in. Set everything BEFORE the import below.
os.environ.setdefault("AGENT_SETTING_CONFIG", "settings.openai.toml")
os.environ["OPENAI_BASE_URL"] = LLM_BASE_URL
os.environ["OPENAI_API_KEY"] = LLM_API_KEY
os.environ["MODEL_NAME"] = LLM_MODEL
os.environ.setdefault("CUGA_DBS_DIR", os.path.join(WORK_DIR, "dbs"))
os.environ.setdefault("CUGA_LOGGING_DIR", os.path.join(WORK_DIR, "logs"))
# No person is watching a run: never stop to ask a question.
os.environ.setdefault("DYNACONF_ADVANCED_FEATURES__FORCE_AUTONOMOUS_MODE", "true")
os.environ.setdefault("DYNACONF_ADVANCED_FEATURES__RUN_RECEIPT", "true")
os.environ.setdefault("DYNACONF_EVOLVE__ENABLED", "false")
# Do not pick up a stray .env or settings.toml from the working directory.
os.environ.setdefault("ENV_FILE", os.path.join(WORK_DIR, "no.env"))

from cuga import CugaAgent  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

app = FastAPI(title="Root Transcribe agent runner", docs_url=None, redoc_url=None)
# CUGA keeps process-wide state and documents no thread-safety: one run at a time.
_one_at_a_time = asyncio.Lock()


class RunRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=20_000)
    input: str = Field(min_length=1, max_length=2_000_000)
    timeout_seconds: float = Field(default=600, gt=0, le=3600)


class RunResponse(BaseModel):
    answer: str = ""
    error: str | None = None
    model: str = ""
    seconds: float = 0.0
    llm_calls: int | None = None
    tokens: int | None = None


def _receipt_numbers(receipt: object) -> tuple[int | None, int | None]:
    """LLM calls and total tokens from CUGA's run receipt, whatever its shape."""
    text = str(receipt or "")
    calls = re.search(r"llm calls:\s*(\d+)", text)
    tokens = re.search(r"tokens:[^(\n]*\((\d+)\)", text)
    return (int(calls.group(1)) if calls else None, int(tokens.group(1)) if tokens else None)


def _build_agent(instructions: str) -> CugaAgent:
    """A fresh agent per run: instructions are fixed when an agent is built."""
    model = ChatOpenAI(base_url=LLM_BASE_URL, model=LLM_MODEL, api_key=LLM_API_KEY, temperature=0.2)
    return CugaAgent(
        model=model,
        tools=[],  # transcript in, document out; nothing to call
        special_instructions=instructions,
        cuga_folder=tempfile.mkdtemp(prefix="cuga-", dir=WORK_DIR),
        auto_load_policies=False,
        filesystem_sync=False,
        enable_knowledge=False,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok" if LLM_BASE_URL and LLM_MODEL else "unconfigured", "model": LLM_MODEL}


@app.post("/run")
async def run(request: RunRequest) -> RunResponse:
    if not (LLM_BASE_URL and LLM_MODEL):
        return RunResponse(error="The agent runner has no LLM endpoint configured.")
    started = time.monotonic()
    async with _one_at_a_time:
        try:
            agent = _build_agent(request.instructions)
            result = await asyncio.wait_for(agent.invoke(request.input), timeout=request.timeout_seconds)
        except TimeoutError:
            return RunResponse(
                error=f"The model did not finish within {request.timeout_seconds:.0f} seconds.",
                model=LLM_MODEL,
                seconds=round(time.monotonic() - started, 1),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
            return RunResponse(
                error=f"{type(exc).__name__}: {exc}"[:2000],
                model=LLM_MODEL,
                seconds=round(time.monotonic() - started, 1),
            )
    calls, tokens = _receipt_numbers(getattr(result, "receipt", None))
    error = getattr(result, "error", None)
    return RunResponse(
        answer="" if error else str(getattr(result, "answer", "") or "").strip(),
        error=str(error)[:2000] if error else None,
        model=LLM_MODEL,
        seconds=round(time.monotonic() - started, 1),
        llm_calls=calls,
        tokens=tokens,
    )
