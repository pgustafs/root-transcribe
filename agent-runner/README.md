# Agent runner

Runs [CUGA](https://github.com/cuga-project/cuga-agent) agents over meeting
transcripts for Root Transcribe. The backend worker sends instructions and a
transcript to `POST /run`; this service builds a CUGA agent for that one run and
returns the answer. It is a separate container so that CUGA's dependencies
(about 2 GB) and model-written code stay away from the database and storage
credentials.

Configuration (environment): `LLM_BASE_URL` (OpenAI-compatible, ending in
`/v1`), `LLM_MODEL`, optional `LLM_API_KEY`.

```bash
uv sync
LLM_BASE_URL=http://localhost:8001/v1 LLM_MODEL=my-model \
  uv run uvicorn main:app --host 127.0.0.1 --port 8011
uv run pytest
```
