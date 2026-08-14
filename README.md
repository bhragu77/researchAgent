# Enterprise Transformation Research Agent

A multi-agent research system that answers enterprise transformation questions
from internal documents, public sources, and the client's own metrics — with
citations on every claim, and an explicit refusal when the evidence doesn't
support an answer.

Built on FastAPI + LangGraph, with hybrid retrieval (BM25 + pgvector + FlashRank),
an A2A-networked external agent, per-tenant isolation, groundedness validation,
and Langfuse tracing.

---

## Quick start

**Prerequisites:** Docker + Docker Compose. Nothing else — Python, Node, and the
ML models are all inside the images.

```bash
git clone https://github.com/bhragu77/researchAgent.git && cd researchAgent
cp .env.example .env          # then edit: see "Environment" below
docker compose -f deploy/docker-compose.yml --profile local-datastores up --build
```

Then, once containers are healthy:

```bash
docker compose -f deploy/docker-compose.yml exec app python -m scripts.ingest --reset
docker compose -f deploy/docker-compose.yml exec app python -m scripts.seed_metrics --reset
docker compose -f deploy/docker-compose.yml exec app python -m scripts.prewarm
```

Open **http://localhost:8000** and sign in as `demo` / `admin`.

> First build takes ~5 minutes: it installs CPU-only torch and bakes the
> embedding + rerank models into the image so no query pays a download.

### Without Docker

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
cd web && npm ci && npm run build && cd ..

cp .env.example .env
docker compose -f deploy/docker-compose.yml --profile local-datastores up -d postgres redis

python -m scripts.ingest --reset
python -m scripts.seed_metrics --reset
uvicorn app.a2a.server:app --port 8500 &     # A2A external agent
uvicorn app.main:app --port 8000             # API + UI
```

## Environment

Copy `.env.example` to `.env`. The essentials:

| Variable | Purpose | Notes |
|---|---|---|
| `GROQ_API_KEY` | Primary LLM | Free tier at console.groq.com/keys |
| `GEMINI_API_KEY` | Fallback LLM | Free tier at aistudio.google.com/apikey |
| `POSTGRES_URL` | Corpus + traces | Neon free tier in production |
| `REDIS_URL` | Rate limit, cache, quota | Upstash free tier in production |
| `JWT_SECRET` | Token signing | **Generate one:** `openssl rand -hex 32` |
| `LANGFUSE_*` | Tracing | Optional — the app runs fine without it (fail-open) |

At least one LLM key is required. Everything else has a working default for
local use.

## Layout

| Path | Purpose |
|---|---|
| `app/api/` | Routes, JWT auth, schemas |
| `app/orchestration/` | LangGraph state, graph, router, nodes |
| `app/agents/` | Enterprise, analytics, external agents |
| `app/a2a/` | A2A server + client (external agent runs as its own process) |
| `app/knowledge/` | Hybrid retrieval, reranking, evidence |
| `app/guardrails/` `app/reliability/` | Injection/PII; groundedness, confidence, abstention |
| `app/tenancy/` | Tenant context, isolation, enrollment |
| `app/telemetry/` | Tracing, metrics, `research_trace`, analytics, feedback |
| `app/eval/` | Golden set, offline eval, online sampler |
| `web/` | React + Vite + Tailwind UI (built and served by the API) |
| `deploy/` | Dockerfiles, compose, tunnel |
| `docs/` | Architecture, decisions, demo script |
| `render.yaml` | Render Blueprint (app + A2A services) |

## Operations

```bash
python -m scripts.preflight --prod     # hardening checklist; exits non-zero on failure
python -m scripts.ingest --reset       # re-ingest data/corpus/*.md
python -m app.eval.run_eval            # offline eval against the golden set
python -m app.eval.online_sampler      # re-judge sampled live runs, compute drift
```

## Deploy

Postgres (Neon), Redis (Upstash) and Langfuse (cloud) are always **external**
to the app itself — nothing here expects to host its own datastore.

### Render (recommended)

`render.yaml` at the repo root is a Blueprint defining the two services this
app needs: `research-agent-app` (public, serves the API + built UI) and
`research-agent-a2a` (private, the external-research agent — never exposed to
the internet, only reachable from the app over Render's internal network).

1. Create a Neon Postgres project and an Upstash Redis database; copy their
   connection strings.
2. In the Render dashboard: **New +** → **Blueprint**, point it at this repo.
   Render reads `render.yaml` and creates both services.
3. Fill in the secret env vars Render prompts for on each service — `POSTGRES_URL`
   (Neon), `REDIS_URL` (Upstash), `GROQ_API_KEY`/`GEMINI_API_KEY`,
   `TAVILY_API_KEY`, and, if using OAuth sign-in, the Google/GitHub client
   ID/secret pairs.
4. Once `research-agent-app` is live, run against Neon (from your own
   machine, with `POSTGRES_URL` pointed at it):
   ```bash
   python -m scripts.ingest --reset
   python -m scripts.seed_metrics --reset
   ```
5. Run `python -m scripts.preflight --prod` before treating it as live —
   it enforces dev token endpoint disabled, strong `JWT_SECRET`, CORS not
   `*`, rate limiting on, debug off, no secrets committed or hardcoded.

### Single VM (alternative)

For a VM instead of Render, `deploy/docker-compose.yml`'s `tunnel` profile
runs **app + A2A agent + Cloudflare tunnel**, so the VM needs no inbound
firewall rule and no TLS certificate:

```bash
# on the VM
git clone https://github.com/bhragu77/researchAgent.git && cd researchAgent
cp .env.example .env      # point POSTGRES_URL/REDIS_URL at Neon/Upstash

python -m scripts.preflight --prod    # must pass before proceeding

export CLOUDFLARE_TUNNEL_TOKEN=<token from the Cloudflare dashboard>
docker compose -f deploy/docker-compose.yml --profile tunnel up -d --build
```

For scaling beyond one instance — connection pooling, worker split, the async
research path — see `docs/DECISIONS.md` (ADR-010).

## Documentation

- **`docs/ARCHITECTURE.md`** — pipeline diagram, the 15-step flow, datastore
  routing, tenancy model, tradeoffs, and a requirement-coverage table.
- **`docs/DECISIONS.md`** — decision log, including the autoscaling design.
- **`docs/DEMO.md`** — the three-query walkthrough plus isolation and injection
  proofs.
