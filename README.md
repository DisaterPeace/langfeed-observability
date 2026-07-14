# agent-service

An observability system for an agentic app: **1 orchestrator + 2 agents** (repo
analyzer, bug detector), instrumented with **two push-only telemetry streams**:

- **①** OTel SDK → OTLP (bearer-token) → Collector → **Prometheus** (metrics) /
  **Tempo** (spans) → **Grafana**
- **②** Langfuse SDK `@observe()` → **Langfuse** (LLM traces, sessions, eval scores)

See [docs/report.md](docs/report.md) for the full write-up and
[docs/telemetry-contract.md](docs/telemetry-contract.md) for the telemetry design.

---

## How to run it

### Prerequisites
- **Docker Desktop** running (the whole backend runs in containers).
- **Python 3.x** (for the agent app).
- An **Ollama Cloud API key** (free) from https://ollama.com → Settings → API Keys.
  Without it, the app runs in "mock" mode (no real LLM calls).

### 1. One-time setup
```powershell
# from D:\agent-service
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# create your .env from the template, then fill in real values
copy .env.example .env
#  - set OLLAMA_API_KEY=...        (your Ollama key)
#  - generate the Langfuse keys / secrets per the comments in .env.example
#    (.env is gitignored — never commit it)
```

### 2. Start the backend (9 containers)
```powershell
docker compose up -d
```
First run pulls images (a few minutes). Wait ~1 min for Langfuse to finish its
first-boot database migrations. Check it's ready:
```powershell
docker compose ps
curl http://localhost:3000/api/public/health
```

### 3. Run the agent pipeline
```powershell
.venv\Scripts\python -m app.main
```
This runs the orchestrator against `sample_repo/`: the repo-analyzer summarizes it,
the bug-detector finds bugs (with a quick→thorough feedback loop), and both a
deterministic and an LLM-as-judge eval score are attached to the Langfuse trace.
Run it a few times to populate the dashboards.

### 4. Look at the results

| UI | URL | What you see |
|----|-----|--------------|
| **Langfuse** | http://localhost:3000 | LLM traces: the run tree, prompts, responses, tokens, and eval scores. Log in with the admin user you set in `.env` (`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`). |
| **Grafana** | http://localhost:3001 | Health dashboard "agent-service — Agent Health Overview" (19 panels: latency P95, tokens/sec, cost, error rate, eval quality, ...) |
| **Prometheus** | http://localhost:9090 | Raw metrics + `/alerts` (firing alert rules) |

### 5. Stop / clean up
```powershell
docker compose stop          # pause everything (keeps data)
docker compose up -d         # resume
docker compose down          # remove containers (keeps data volumes)
docker compose down -v       # remove containers AND wipe all stored data
```

---

## Quick reference

- **Smoke-test just the OTel plumbing:** `.venv\Scripts\python app\hello_span.py`
- **Run in mock mode (no LLM cost):** remove/blank `OLLAMA_API_KEY` in `.env`.
- **Disable prompt-content capture (privacy):** set
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` in `.env`.
- **Ports:** Langfuse 3000 · Grafana 3001 · Tempo 3200 · Prometheus 9090 · OTel
  Collector 4317/4318 (bearer-token required) · MinIO 9190/9091 · Postgres 5432 ·
  ClickHouse 8123/9000 · Redis 6379.
