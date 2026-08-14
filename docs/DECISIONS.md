# Decision Log

## ADR-001:

## ADR-002:

## ADR-003:

---

## ADR-010: Horizontal scaling and autoscaling strategy

**Status:** Accepted (design only — not provisioned). Phase 5.

**Context.** The system runs today as a single instance on a free tier. Load is
bursty and heterogeneous: a `/health` probe costs microseconds, a shallow
factual query a few seconds, and a decomposed multi-agent research run over a
minute. Any scaling design that treats those as one workload will either
over-provision for the common case or time out on the expensive one.

### Decision 1 — The app process is stateless; all state is externalized

No durable state lives in the application process. Everything that outlives a
request is in Postgres (corpus chunks with pgvector embeddings, tenant config,
`research_trace`, `feedback`, `online_eval`) or Redis (rate-limit counters,
answer cache, provider quota counters).

This is what makes horizontal scaling possible at all: any instance can serve
any request, no session affinity is required, and an instance can be killed
mid-rollout without losing anything but its in-flight requests.

Two deliberate exceptions, both bounded and both acceptable to lose:

| In-process state | Why it is acceptable |
| --- | --- |
| LangGraph `MemorySaver` checkpointer | Only used within a single run. A crashed run is retried from the start, not resumed. Moving to a Postgres checkpointer is the change required for resumable long runs. |
| Embedding + reranker models (~500 MB RSS) | Read-only, identical on every instance. Costs memory and cold-start time, not correctness. |

The second exception drives the sizing floor: each instance needs roughly 1 GB
of memory before serving a single request, so scaling out is memory-bound well
before it is CPU-bound.

### Decision 2 — API and background workers scale independently

They have different bottlenecks and must not share a scaling signal.

* **API tier** — bound by concurrent request count and by *waiting* on provider
  calls, not by local CPU. Scales on request concurrency and p95 latency.
* **Worker tier** — online-eval re-judging, ingestion, index rebuilds. Bound by
  throughput, tolerant of delay, and interruptible. Scales on queue depth.

Coupling them means either paying for idle API capacity to run a nightly eval
sweep, or having an eval sweep starve live traffic. Today `online_sampler`
runs its re-judge on a daemon thread inside the API process — acceptable at
one instance, and the first thing to move to a real worker when traffic grows.

### Decision 3 — Long research runs go async (`task_id` + poll)

A decomposed multi-agent run can exceed 60 s. Holding an HTTP connection open
for that duration fights every default timeout between the client and the app
(load balancer idle timeouts are typically 60 s), and it makes autoscaling
worse: instances cannot drain, so deploys and scale-in events kill live work.

The target shape:

```
POST /research/async  -> 202 { task_id }        # enqueue to Redis, return now
GET  /research/status/{task_id} -> queued | running | done + result
```

The API instance then does no long work, so it scales on a fast, honest signal,
and workers scale on **queue depth** — the only metric that actually reflects
research backlog. CPU on a worker blocked awaiting a Gemini response is near
zero, which is exactly why CPU-based autoscaling would fail here.

*Not implemented in Phase 5* (listed as optional, and time went to observability).
`POST /research` remains synchronous. The queue-depth signal is the load-bearing
part of the design and should be built before the first real concurrent load.

### Decision 4 — Retrieval, vector store, and LLM are separate scaling tiers

Each has a different constraint and a different remedy:

| Tier | Constraint | How it scales |
| --- | --- | --- |
| App (API/worker) | Memory (models resident) | More instances |
| Postgres + pgvector | Disk I/O, index size, connection count | Vertical first, then read replicas; PgBouncer before replicas |
| Embedding / rerank | CPU, per-instance | Scales with app; separate inference service if it dominates |
| LLM providers | **External quota**, not our capacity | Cannot scale out — must degrade |

The last row is the one that breaks naive autoscaling. Adding instances does
not add Groq quota; it exhausts the shared quota faster and turns a slow system
into a failing one. That is why the provider chain is quota-aware in Redis
(`app.providers.llm.quota`): the cap is global across instances, so the guard
must be in shared state, not per-process. Under provider saturation the correct
response is to degrade — fall back down the chain, then serve an explicitly
degraded answer — not to scale up.

Postgres connections deserve the same care: every new instance multiplies the
connection count, and pgvector queries hold connections for the duration of an
ANN scan. A connection pooler is required before the second or third instance,
not after.

### Decision 5 — Today single instance; path is ASG / Cloud Run

Free tier runs one instance of each container via `deploy/docker-compose.yml`.
The migration path, in the order the constraints will actually bite:

1. **PgBouncer** in front of Postgres — before instance #2.
2. **Split the worker tier** — move online-eval and ingestion off the API
   process; Redis is already the queue substrate.
3. **Implement the async path** (Decision 3) so workers have a queue-depth
   signal to scale on.
4. **Cloud Run** (or an ASG behind an ALB) for the API tier: scale on
   concurrency, `min_instances=1` to keep models warm — a cold start pays the
   model-load cost, so scale-to-zero is a false economy here.
5. **Managed Postgres with pgvector** and a read replica once retrieval reads
   dominate writes.

**Scaling signals, in priority order:** queue depth (workers) > request
concurrency (API) > p95 latency (both). Explicitly *not* CPU — every tier here
spends most of its time blocked on I/O or an external provider, so CPU
utilization under-reports load precisely when the system is most saturated.

**Consequences.** Statelessness is a constraint the code must keep paying for:
any future per-instance cache, in-memory session, or local file write breaks
horizontal scaling. The provider-quota guard must stay in Redis rather than
becoming a process-local optimization, since its correctness depends on being
shared.
