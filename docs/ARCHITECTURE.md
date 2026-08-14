# Architecture

Enterprise Transformation Research Agent — a multi-agent research system that
answers consulting questions from internal documents, public sources, and the
client's own metrics, and refuses to answer when the evidence does not support
one.

---

## Overview

```
                    ┌──────────────────────────────────────────┐
   Browser ────────▶│  FastAPI  (serves the React bundle too)  │
   (React/Vite)     └───────────────────┬──────────────────────┘
                                        │ JWT verified → tenant scope bound
                                        ▼
                    ┌──────────────────────────────────────────┐
                    │        LangGraph orchestration           │
                    │  ingress→classify→router→manager→…       │
                    └───┬──────────────┬───────────────┬───────┘
                        │              │               │
              ┌─────────▼───┐   ┌──────▼──────┐  ┌─────▼────────┐
              │ Enterprise  │   │  Analytics  │  │  External    │
              │   agent     │   │   agent     │  │  agent (A2A) │
              └─────┬───────┘   └──────┬──────┘  └─────┬────────┘
                    │                  │               │ HTTP
        ┌───────────▼──────┐    ┌──────▼──────┐  ┌──────▼─────────┐
        │ pgvector + BM25  │    │  metrics    │  │ separate proc. │
        │ (tenant-scoped)  │    │  table      │  │ search + fetch │
        └──────────────────┘    └─────────────┘  └────────────────┘

   Cross-cutting: Redis (rate limit, answer cache, provider quota)
                  Langfuse (per-node spans)  ·  research_trace (analytics)
```

## The agent flow, step by step

| # | Step | What it does | Why it exists |
|---|---|---|---|
| 1 | **JWT verify** | Decode `tid`/`role`; bind `TenantContext` | Tenant comes from a signed claim, never the request body |
| 2 | **Rate limit** | Per-tenant RPM/RPD in Redis | An over-quota request must cost zero model calls |
| 3 | **Answer cache** | Keyed on (tenant, normalized question) | Checked *after* the limiter so a tenant can't mine its cache to escape quota |
| 4 | **ingress** | Heuristic prompt-injection screen | Blocks before any model call — the cheapest possible rejection |
| 5 | **classify** | Intent, complexity, entities, `datastore_plan` | Turns a question into a routable shape |
| 5b | **augment** | Rule-based `datastore_plan` widening | The classifier LLM is inconsistent; rules only ever *add* signals so an agent is never silently disabled |
| 6 | **router** | Pure fn → agents, budgets, model tier | No I/O, no LLM: routing is reproducible and cost is known before work starts |
| 7 | **manager** | Decompose into ≤4 sub-questions, dispatch in parallel | Depth counter incremented **in code**; `MAX_DEPTH=2` is a circuit breaker, not a prompt instruction |
| 8 | **enterprise agent** | Hybrid BM25 + dense → RRF → FlashRank rerank | Lexical and dense fail differently; RRF fuses on rank, not incomparable scores |
| 9 | **analytics agent** | Intent → **fixed** parameterized SQL | The LLM never writes SQL; worst case is "wrong metric", not "arbitrary query" |
| 10 | **external agent** | Over **A2A HTTP** to a separate process | Real network seam — another team's agent could be swapped in |
| 11 | **fusion** | Normalize per-agent, dedupe, authority+freshness, assign E-ids | Each agent scores on its own scale; normalizing per agent stops one dominating |
| 12 | **conflict** | Detect contradictions across evidence | Surfaced, never silently resolved |
| 13 | **synthesize** | Draft with mandatory E-id citations | Conflicts fed in, so disagreement is represented |
| 14 | **validate** | Groundedness judge → pass / regenerate / abstain | Bounded self-correction (`MAX_REGENERATIONS=1`); computed confidence overwrites the model's self-report |
| 15 | **persist** | Write `research_trace`, finalize Langfuse trace | Every terminal path routes here — abstentions and blocks are outcomes to count |

## Datastore routing

`classify` emits a `datastore_plan`; the router maps signals to agents:

| Signal | Agent | Store |
|---|---|---|
| `vector` | enterprise | pgvector cosine ANN |
| `bm25` | enterprise | rank-bm25 over tenant chunks |
| `analytics_sql` | analytics | `metrics` table, fixed queries |
| `needs_external` | external | web search + allowlisted fetch |

Enterprise is **always** included: internal context grounds an answer even when
the question is mostly about the outside world.

## Security and tenancy

- **Tenant from claims only.** `ResearchRequest` has no tenant field; a smuggled
  `tenant_id` is dropped by the schema before a handler runs.
- **Isolation at the query layer.** Every retrieval and SQL call goes through
  `tenancy.isolation`, which injects the bound tenant. Not a caller convention.
- **Two admin levels.** `admin` is a *tenant* admin (enrollment issues one to
  every tenant) and is pinned to its own data. Cross-tenant reads require
  `platform_admin`, which enrollment never issues. Conflating them would have
  let any tenant read every tenant's operational data.
- **Deny-by-default tools.** Per-agent tool allowlist plus a domain allowlist for
  outbound fetch; both checked at call time, and every denial is recorded.
- **Untrusted evidence.** Retrieved passages are data, not instructions —
  prompts say so explicitly and `guardrails.injection` sanitizes them.

## Key tradeoffs

| Decision | Gained | Paid |
|---|---|---|
| Groq primary, Gemini secondary | Free tier survives a 30-case eval | Groq caps constantly → ~100% fallback rate |
| Fixed SQL for analytics | No arbitrary LLM-authored queries | Only pre-mapped metric intents answerable |
| Synchronous `/research` | Simple client, no polling | 30-70s holds a connection; async design in ADR-010, not built |
| In-process `MemorySaver` | No checkpoint infrastructure | A crashed run restarts rather than resumes |
| Models baked into image | No cold-start download | ~500 MB RSS per instance; memory-bound scaling |
| Computed confidence overrides model | Confidence reflects evidence | Needs calibration against the golden set |

## How each requirement is met

| Requirement | Where | Evidence it works |
|---|---|---|
| **Multi-tenancy** | `tenancy/`, JWT `tid`, `platform_admin` split | Cross-tenant analytics request pinned to caller's scope |
| **A2A** | `app/a2a/` on a2a-sdk 1.1.2 | Agent Card at `/.well-known/agent-card.json`; task reaches `TASK_STATE_COMPLETED` across two PIDs |
| **Guardrails** | `guardrails/` injection, PII, validation | Injection blocked pre-model; blocks counted in `block_rate` |
| **Reliability** | `reliability/` groundedness, confidence, abstain | Abstains rather than fabricating; conflicts cap confidence at 0.6 |
| **Scalability** | Stateless app, external state; ADR-010 | Scaling signals: queue depth > concurrency > p95, never CPU |
| **Cost** | `telemetry/metrics` price table, Redis quota | Per-tenant `est_cost_usd`; capped models skipped before the API rejects |
| **Explainability** | E-id citations, confidence breakdown, evidence drawer | Every claim traces to a chunk the user can read |
| **Continuous improvement** | `eval/online_sampler`, `feedback` | Re-judges sampled runs, computes drift, appends failures for golden-set promotion |
