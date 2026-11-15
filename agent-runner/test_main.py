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

    def build(instructions: str, tools=None):
        built.append(instructions)
        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(main, "_build_agent", build)
    return built


def test_health_reports_the_model_and_its_context_window(monkeypatch):
    monkeypatch.setattr(main, "context_tokens", lambda: 8192)
    assert client.get("/health").json() == {
        "status": "ok",
        "model": "test-model",
        "web_search": False,
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


def test_chat_makes_one_plain_call_and_can_show_an_image(monkeypatch):
    seen = []

    class FakeModel:
        async def ainvoke(self, messages):
            seen.append(messages)
            return SimpleNamespace(content=[{"type": "text", "text": " # Index scan "}])

    monkeypatch.setattr(main, "_build_model", lambda: FakeModel())
    body = client.post(
        "/chat",
        json={"instructions": "Read the slide.", "input": "", "image_base64": "QUJD"},
    ).json()
    assert body["answer"] == "# Index scan" and body["error"] is None
    system, human = seen[0]
    assert system.content == "Read the slide."
    assert human.content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    # Without an image the input is sent as plain text.
    client.post("/chat", json={"instructions": "x", "input": "Transcript: hi"})
    assert seen[1][1].content == "Transcript: hi"


def test_chat_reports_a_model_error_instead_of_raising(monkeypatch):
    class Refuses:
        async def ainvoke(self, messages):
            raise ValueError("this model does not support image input")

    monkeypatch.setattr(main, "_build_model", lambda: Refuses())
    body = client.post("/chat", json={"instructions": "x", "image_base64": "QUJD"}).json()
    assert body["answer"] == "" and "does not support image input" in body["error"]


def test_research_gives_the_agent_one_search_tool_and_reports_its_pages(monkeypatch):
    monkeypatch.setattr(main, "SEARCH_MCP_URL", "http://mcp/mcp")
    queries: list[str] = []

    async def search(query: str):
        queries.append(query)
        return [
            {"title": "Index scans", "url": "https://example.org/a", "content": "Reads matching rows."},
            {"title": "Again", "url": "https://example.org/a", "content": "Same page."},
        ]

    monkeypatch.setattr(main, "_search", search)
    given = {}

    def build(instructions: str, tools=None):
        given["instructions"], given["tools"] = instructions, tools

        async def invoke(question: str):
            # The agent searches more often than it is allowed to.
            texts = [await tools[0].ainvoke({"query": f"{question} {n}"}) for n in range(6)]
            given["texts"] = texts
            return SimpleNamespace(answer="An index scan reads only matching rows.", error=None)

        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(main, "_build_agent", build)
    body = client.post("/research", json={"question": "What is an index scan?"}).json()
    assert body["answer"].startswith("An index scan") and body["error"] is None
    assert given["tools"][0].name == "web_search"
    assert "web_search" in given["instructions"]
    # Four searches at most; the page is listed once, as the tool returned it.
    assert len(queries) == main.MAX_SEARCHES
    assert "No more searches" in given["texts"][-1]
    assert body["sources"] == [{"title": "Index scans", "url": "https://example.org/a"}]
    assert "https://example.org/a" in given["texts"][0]


def test_research_is_off_without_a_search_server_and_survives_a_failed_search(monkeypatch):
    monkeypatch.setattr(main, "SEARCH_MCP_URL", "")
    body = client.post("/research", json={"question": "What is an index scan?"}).json()
    assert body["error"] == "Web search is not configured."
    assert client.get("/health").json()["web_search"] is False

    monkeypatch.setattr(main, "SEARCH_MCP_URL", "http://mcp/mcp")

    async def broken(query: str):
        raise RuntimeError("The search service answered HTTP 401.")

    monkeypatch.setattr(main, "_search", broken)

    def build(instructions: str, tools=None):
        async def invoke(question: str):
            told = await tools[0].ainvoke({"query": question})
            return SimpleNamespace(answer=f"I could not search. {told}", error=None)

        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(main, "_build_agent", build)
    body = client.post("/research", json={"question": "What is an index scan?"}).json()
    assert "HTTP 401" in body["answer"] and body["sources"] == []
