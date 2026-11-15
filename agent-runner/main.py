"""Agent runner: runs one CUGA agent over one piece of text.

A deliberately small service so the CUGA SDK (large, with process-wide
configuration, able to execute model-written code) stays out of the processes
that hold database and storage credentials. It knows only the LLM endpoint.

    POST /run  {"instructions": "...", "input": "...", "timeout_seconds": 600}
            -> {"answer": "...", "error": null, "model": "...", "seconds": 12.3,
                "llm_calls": 1, "tokens": 5231}
    POST /chat {"instructions": "...", "input": "...", "image_base64": "...",
                "image_type": "image/png"}
            -> {"answer": "...", "error": null, "model": "..."}
       One plain model call without the agent (short, structured answers, and
       questions about an image).
    GET /health

Listens on the pod's loopback only (see deploy/root-transcribe.yaml).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import urllib.request

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or "not-needed"
WORK_DIR = os.environ.get("AGENT_RUNNER_WORK_DIR") or tempfile.mkdtemp(prefix="agent-runner-")
# Notes are written from the transcript in one pass; a model's "thinking" phase
# only spends the answer budget (a small context window is then used up before
# any text is written). Off unless LLM_ENABLE_THINKING=true. The switch is the
# chat-template flag vLLM and llama.cpp pass to the model; set
# LLM_ENABLE_THINKING=default for a server that rejects unknown request fields.
THINKING = os.environ.get("LLM_ENABLE_THINKING", "false").strip().lower()

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
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

app = FastAPI(title="Root Transcribe agent runner", docs_url=None, redoc_url=None)
# CUGA keeps process-wide state and documents no thread-safety: one run at a time.
_one_at_a_time = asyncio.Lock()


class RunRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=20_000)
    input: str = Field(min_length=1, max_length=2_000_000)
    timeout_seconds: float = Field(default=600, gt=0, le=3600)


class ChatRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=20_000)
    input: str = Field(default="", max_length=200_000)
    # An image the question is about (base64, without a data: prefix).
    image_base64: str | None = Field(default=None, max_length=24_000_000)
    image_type: str = Field(default="image/png", pattern=r"^image/(png|jpeg|webp)$")
    timeout_seconds: float = Field(default=300, gt=0, le=3600)


_context_tokens: int | None = None


def context_tokens() -> int | None:
    """The model's context window as the server reports it (vLLM:
    ``max_model_len``), or None when it does not say. Asked once."""
    global _context_tokens
    if _context_tokens is None and LLM_BASE_URL:
        request = urllib.request.Request(
            f"{LLM_BASE_URL}/models", headers={"Authorization": f"Bearer {LLM_API_KEY}"}
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                entries = json.load(response).get("data") or []
        except Exception:  # noqa: BLE001 - unknown is a valid answer
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id") == LLM_MODEL:
                for key in ("max_model_len", "context_length", "context_window"):
                    if isinstance(entry.get(key), int) and entry[key] > 0:
                        _context_tokens = entry[key]
    return _context_tokens


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


def _build_model() -> ChatOpenAI:
    extra = (
        {}
        if THINKING == "default"
        else {"extra_body": {"chat_template_kwargs": {"enable_thinking": THINKING == "true"}}}
    )
    return ChatOpenAI(
        base_url=LLM_BASE_URL, model=LLM_MODEL, api_key=LLM_API_KEY, temperature=0.2, **extra
    )


def _build_agent(instructions: str) -> CugaAgent:
    """A fresh agent per run: instructions are fixed when an agent is built."""
    return CugaAgent(
        model=_build_model(),
        tools=[],  # transcript in, document out; nothing to call
        special_instructions=instructions,
        cuga_folder=tempfile.mkdtemp(prefix="cuga-", dir=WORK_DIR),
        auto_load_policies=False,
        filesystem_sync=False,
        enable_knowledge=False,
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok" if LLM_BASE_URL and LLM_MODEL else "unconfigured",
        "model": LLM_MODEL,
        "context_tokens": await asyncio.to_thread(context_tokens),
    }


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
    answer = "" if error else str(getattr(result, "answer", "") or "").strip()
    if not error and answer in ("", "No answer found"):
        # CUGA's placeholder when the model produced no text at all.
        window = await asyncio.to_thread(context_tokens)
        answer, error = "", (
            "The model returned no text. Usually its answer budget ran out: the "
            "context window is too small for the agent's prompt plus the transcript"
            + (f" (the server reports {window:,} tokens)" if window else "")
            + ", or the model used the whole budget for reasoning. Serve the model "
            "with a larger context window (32,000 tokens or more is recommended)."
        )
    return RunResponse(
        answer=answer,
        error=str(error)[:2000] if error else None,
        model=LLM_MODEL,
        seconds=round(time.monotonic() - started, 1),
        llm_calls=calls,
        tokens=tokens,
    )


def _answer_text(content: object) -> str:
    """A chat answer as text (some servers return a list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


@app.post("/chat")
async def chat(request: ChatRequest) -> RunResponse:
    """One model call. With an image, the model must be able to read images;
    a model that cannot answers with an error, which is passed on."""
    if not (LLM_BASE_URL and LLM_MODEL):
        return RunResponse(error="The agent runner has no LLM endpoint configured.")
    started = time.monotonic()
    content: object = request.input or "Answer as instructed."
    if request.image_base64:
        content = [
            {"type": "text", "text": request.input or "Here is the image."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{request.image_type};base64,{request.image_base64}"},
            },
        ]
    try:
        result = await asyncio.wait_for(
            _build_model().ainvoke(
                [SystemMessage(content=request.instructions), HumanMessage(content=content)]
            ),
            timeout=request.timeout_seconds,
        )
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
    answer = _answer_text(getattr(result, "content", "")).strip()
    return RunResponse(
        answer=answer,
        error=None if answer else "The model returned an empty answer.",
        model=LLM_MODEL,
        seconds=round(time.monotonic() - started, 1),
    )
