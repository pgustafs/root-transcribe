"""The runner's HTTP contract, with CUGA replaced by a stand-in agent."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("LLM_BASE_URL", "http://llm.invalid/v1")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
RECEIPT = "│ tokens: 10 in / 5 out (15) │\n│ llm calls: 2   tool calls: 0 │"


def use_agent(monkeypatch, invoke):
    built: list[str] = []

    def build(instructions: str):
        built.append(instructions)
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(main, "_build_agent", build)
    return built


def test_health_reports_the_model_and_its_context_window(monkeypatch):
    monkeypatch.setattr(main, "context_tokens", lambda: 8192)
    assert client.get("/health").json() == {
        "status": "ok",
        "model": "test-model",
        "context_tokens": 8192,
    }


def test_no_text_from_the_model_is_an_explained_error(monkeypatch):
    monkeypatch.setattr(main, "context_tokens", lambda: 8192)

    async def invoke(text):
        return SimpleNamespace(answer="No answer found", error=None, receipt=None)

    use_agent(monkeypatch, invoke)
    body = client.post("/run", json={"instructions": "x", "input": "y"}).json()
    assert body["answer"] == ""
    assert "returned no text" in body["error"] and "8,192 tokens" in body["error"]


def test_thinking_is_switched_off_unless_asked_for(monkeypatch):
    built = {}

    class FakeChat:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(main, "ChatOpenAI", FakeChat)
    monkeypatch.setattr(main, "CugaAgent", lambda **kwargs: kwargs)
    main._build_agent("Summarize.")
    assert built["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setattr(main, "THINKING", "default")
    built.clear()
    main._build_agent("Summarize.")
    assert "extra_body" not in built


def test_run_returns_the_answer_and_usage(monkeypatch):
    async def invoke(text):
        assert text == "Transcript: hello"
        return SimpleNamespace(answer="  ## Summary\nHello.  ", error=None, receipt=RECEIPT)

    built = use_agent(monkeypatch, invoke)
    body = client.post("/run", json={"instructions": "Summarize.", "input": "Transcript: hello"}).json()
    assert built == ["Summarize."]  # a fresh agent with exactly these instructions
    assert body["answer"] == "## Summary\nHello."
    assert body["error"] is None
    assert (body["llm_calls"], body["tokens"], body["model"]) == (2, 15, "test-model")


def test_agent_error_is_reported_not_raised(monkeypatch):
    async def invoke(text):
        return SimpleNamespace(answer="Error: boom", error="boom", receipt=None)

    use_agent(monkeypatch, invoke)
    body = client.post("/run", json={"instructions": "x", "input": "y"}).json()
    assert body["answer"] == "" and body["error"] == "boom"


def test_exception_and_timeout_become_errors(monkeypatch):
    async def explode(text):
        raise RuntimeError("connection refused")

    use_agent(monkeypatch, explode)
    body = client.post("/run", json={"instructions": "x", "input": "y"}).json()
    assert body["error"] == "RuntimeError: connection refused"

    async def never(text):
        await asyncio.sleep(30)

    use_agent(monkeypatch, never)
    body = client.post("/run", json={"instructions": "x", "input": "y", "timeout_seconds": 0.05}).json()
    assert "did not finish" in body["error"]


@pytest.mark.parametrize("payload", [{"instructions": "", "input": "y"}, {"instructions": "x"}])
def test_invalid_requests_are_rejected(payload):
    assert client.post("/run", json=payload).status_code == 422
