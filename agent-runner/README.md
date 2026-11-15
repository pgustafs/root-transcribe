# Agent runner

Runs [CUGA](https://github.com/cuga-project/cuga-agent) agents over meeting
transcripts for Root Transcribe. The backend worker sends instructions and a
transcript to `POST /run`; this service builds a CUGA agent for that one run and
returns the answer. It is a separate container so that CUGA's dependencies
(about 2 GB) and model-written code stay away from the database and storage
credentials.

`POST /chat` is one plain model call without the agent, optionally about an
image (base64). Notebooks use it for short structured answers and for reading
the text on a slide; the latter needs a model that accepts images.

`POST /research` answers one question from the web: a CUGA agent with a single
tool, `web_search`, served by the search MCP server (`search_mcp.py`, a second
container from this image, Tavily behind it). The caller sends the question
only. It is off unless `SEARCH_MCP_URL` is set; the Tavily key (`TAVILY_API_KEY`)
is given to the search container, not to this service.

Configuration (environment): `LLM_BASE_URL` (OpenAI-compatible, ending in
`/v1`), `LLM_MODEL`, optional `LLM_API_KEY`.

```bash
uv sync
LLM_BASE_URL=http://localhost:8001/v1 LLM_MODEL=my-model \
  uv run uvicorn main:app --host 127.0.0.1 --port 8011
uv run pytest
```

## Adding another MCP tool later (for example a RAG server)

Not built; this is the path. Today one MCP server exists (`search_mcp.py`,
tool `web_search`) and the question flow uses it in one place. A second
server, say a retrieval (RAG) server over your own documents, touches the same
five places.

**1. The server.** Any MCP server that speaks streamable HTTP works; it does
not have to be written here. Give it one search tool that returns the same
shape as `web_search`, so the rest needs no special cases:

```
search_documents(query: str, max_results: int = 5)
  -> {"results": [{"title": "...", "url": "...", "content": "..."}]}
```

`url` is what the user will click. For documents without a web address, return
an address your users can open (an intranet link), or see step 4.

**2. The pod** (`deploy/root-transcribe.yaml`, `deploy/configure.py`). Copy
the `search-mcp` block: a container between `# rag-mcp:begin` / `# rag-mcp:end`
markers, a `without_rag_mcp()` function that removes it when it is not
configured, its address as a ConfigMap key (`RAG_MCP_URL`, for example
`http://127.0.0.1:8013/mcp`), and its credentials in a Secret of its own.
Listen on the pod's loopback only. `deploy/test_configure.py` has the test to
copy (`search-mcp` in `test_agent_runner_is_deployed_only_with_an_llm`).

**3. The runner** (`main.py`). `_search()` and `_search_tool()` are written
for one server: `SEARCH_MCP_URL` and the tool name `web_search` are fixed
inside them. Give both the address and the tool name as arguments, then build
one `StructuredTool` per configured server. There are two ways to use the new
tool, and they differ in what the user has agreed to:

- *As a second tool of the research agent* (`/research`): add it to the list
  passed to `_build_agent(RESEARCH_INSTRUCTIONS, [...])` and name it in the
  instructions ("search the company documents first, the web after"). Simple,
  but `/research` only runs when the recording switched web research on.
- *As part of the workspace step* (recommended for internal documents): add an
  endpoint that calls the tool without a model, for example
  `POST /retrieve {"query": "...", "tool": "search_documents"}`, and call it
  from `tasks.answer_study_question` next to `study_answer.search_workspace()`.
  The results become numbered findings (`[S3]`, `[S4]` …) like transcript
  excerpts, and are used whether or not web research is on.

Keep the rule that the agent and the tools get the **question and search
words only**, never the transcript: whatever they are given can end up in a
query to another service.

**4. Sources** (backend `services/study_answer.py`, frontend
`src/api/study.ts` and `components/study/QuestionAnswer.tsx`). Sources are
stored as data with a `kind`: `recording`, `note` or `web`. `web_sources()`
keeps only `http(s)` addresses. Add a kind (`document`) with the fields the
link needs, add it to the `AnswerSource` type, and render it in `SourceLink`.
Sources must keep coming from what the tool returned, not from the model's
text.

**5. Switches** (backend `core/config.py`, `routes/study.py: features`,
`schemas/study.py`, the record dialog). `WEB_SEARCH_ENABLED` and the session's
`research_web` are the pattern: one setting that says the server is deployed,
and, if content leaves the installation, a per-recording switch that is off by
default. A RAG server inside the pod needs only the first.

**Access is the hard part.** The workspace search checks, per user, which
recordings they may read and leaves out restricted ones. A RAG index knows
nothing about that. Either index only material every user of the installation
may see, or pass the user's identity to the tool and let the server filter,
and test that a user never gets an excerpt from a document they cannot open.

**Tests to copy:** `agent-runner/test_main.py`
(`test_research_gives_the_agent_one_search_tool_and_reports_its_pages`), and
in `backend/tests/unit/test_study.py` the `_Answers` runner with
`test_web_research_gets_the_question_only_and_its_failure_is_not_fatal`.
