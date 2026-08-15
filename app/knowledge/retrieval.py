"""Hybrid retrieval: pgvector dense search + BM25, fused with RRF.

Lexical and dense retrieval fail differently — BM25 handles rare terms and exact
identifiers, dense handles paraphrase. RRF merges the two ranked lists using
rank position only, so the incompatible score scales never have to be
reconciled.

Embeddings are computed locally on CPU (all-MiniLM-L6-v2), so retrieval needs no
API key and no network.
"""

import logging
import re
from functools import lru_cache
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from app.config.settings import get_settings
from app.knowledge.evidence import Evidence
from app.tenancy.isolation import requires_tenant, resolve_tenant, validate_tenant_id

logger = logging.getLogger(__name__)

CHUNKS_TABLE = "chunks"
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --- infrastructure --------------------------------------------------------


def connect() -> psycopg.Connection:
    """Open a Postgres connection with the pgvector type adapter registered."""
    conn = psycopg.connect(get_settings().postgres_url)
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # First-ever connection to a brand-new database: the `vector`
        # extension hasn't been created yet, so there is no type to register.
        # This is exactly the connection `create_schema` is about to use to
        # run `CREATE EXTENSION IF NOT EXISTS vector;` -- every connection
        # after that one registers normally.
        logger.info("vector extension not yet installed; continuing without pgvector type adapter")
    return conn


@lru_cache(maxsize=1)
def get_embedder() -> Any:
    """Load the local sentence-transformers model once per process.

    First call downloads the model (~90 MB) and takes a few seconds; later calls
    are cached.
    """
    from sentence_transformers import SentenceTransformer

    settings = get_settings()
    logger.info("loading embedding model %s", settings.embed_model)
    return SentenceTransformer(settings.embed_model, device="cpu")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts into normalized vectors."""
    model = get_embedder()
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return [v.tolist() for v in vectors]


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenization shared by BM25 index and queries.

    Ingestion and query time must use the same function or the index is useless.
    """
    return _TOKEN_RE.findall(text.lower())


@lru_cache(maxsize=32)
def _load_bm25_index(tenant: str) -> tuple[Any, list[dict[str, Any]]] | None:
    """Build the tenant's BM25 index from Postgres and cache it for the life
    of this process.

    This used to read a pickle file `scripts.ingest` wrote to local disk --
    the one piece of server state that lived outside Postgres, which meant a
    second instance (a different laptop, a cloud box) needed that file copied
    over by hand before lexical search worked. Building it from the same
    `chunks` rows dense retrieval already scopes by tenant means every
    instance behaves identically from a cold start, with nothing to sync.
    The cost is a fresh process not seeing a re-ingest until it restarts,
    same as `get_embedder` above already caching the embedding model for the
    process's lifetime.
    """
    from rank_bm25 import BM25Okapi

    safe = validate_tenant_id(tenant)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT chunk_id, text, source, authority, date FROM {CHUNKS_TABLE} WHERE tenant = %s",  # noqa: S608
            (safe,),
        )
        rows = cur.fetchall()

    if not rows:
        return None

    records = [
        {"chunk_id": r[0], "text": r[1], "source": r[2], "authority": r[3], "date": r[4]}
        for r in rows
    ]
    corpus = [tokenize(r["text"]) for r in records]
    return BM25Okapi(corpus), records


# --- retrievers ------------------------------------------------------------


@requires_tenant
def dense_search(query: str, tenant: str | None = None, k: int = 50) -> list[Evidence]:
    """Dense retrieval over pgvector, scoped to one tenant.

    Postgres returns distance (lower is better); we convert to a similarity so
    higher is better everywhere downstream.

    The `tenant = %s` predicate is parameterized and applied to every query.
    `resolve_tenant` raises rather than returning a default, so there is no way
    to reach this SQL without a scope.
    """
    tenant = resolve_tenant(tenant)
    vector = embed_texts([query])[0]

    sql = f"""
        SELECT chunk_id, text, source, authority, date,
               1 - (embedding <=> %s::vector) AS similarity
        FROM {CHUNKS_TABLE}
        WHERE tenant = %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """  # noqa: S608 - table name is a module constant, not user input

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (vector, tenant, vector, k))
        rows = cur.fetchall()

    return [
        Evidence(
            chunk_id=row[0],
            text=row[1],
            source=row[2] or "unknown",
            authority=row[3] or "unknown",
            date=row[4] or "unknown",
            score=float(row[5]),
            # Stamped with its owner so `assert_same_tenant` can verify the
            # fused set after several agents have contributed to it.
            metadata={"retriever": "dense", "tenant": tenant},
        )
        for row in rows
    ]


@requires_tenant
def bm25_search(query: str, tenant: str | None = None, k: int = 50) -> list[Evidence]:
    """Lexical retrieval over the tenant's own chunks (see `_load_bm25_index`).

    Each tenant's index is built only from that tenant's own rows, so there is
    no shared structure to filter -- isolation here is by construction rather
    than by predicate.

    Returns an empty list if the tenant has no chunks yet -- the caller still
    gets dense results rather than a hard failure.
    """
    tenant = resolve_tenant(tenant)
    loaded = _load_bm25_index(tenant)
    if loaded is None:
        logger.warning("no chunks for tenant %r; skipping BM25", tenant)
        return []
    bm25, records = loaded

    scores = bm25.get_scores(tokenize(query))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]

    return [
        Evidence(
            chunk_id=records[i]["chunk_id"],
            text=records[i]["text"],
            source=records[i].get("source", "unknown"),
            authority=records[i].get("authority", "unknown"),
            date=records[i].get("date", "unknown"),
            score=float(scores[i]),
            metadata={"retriever": "bm25", "tenant": tenant},
        )
        for i in ranked
        if scores[i] > 0
    ]


def rrf_fuse(
    dense: list[Evidence],
    bm25: list[Evidence],
    k: int | None = None,
    top_k: int | None = None,
) -> list[Evidence]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank(d)).

    `k` damps how much any single list's top ranks dominate. Documents found by
    both retrievers accumulate score from both, which is the whole point.

    Returns at most `top_k` results (defaults to RETRIEVE_TOP_K).
    """
    settings = get_settings()
    k = settings.rrf_k if k is None else k
    top_k = settings.retrieve_top_k if top_k is None else top_k

    fused: dict[str, Evidence] = {}
    contributions: dict[str, float] = {}

    for ranked_list in (dense, bm25):
        for rank, item in enumerate(ranked_list, start=1):
            key = item.chunk_id
            contributions[key] = contributions.get(key, 0.0) + 1.0 / (k + rank)
            if key not in fused:
                fused[key] = item
                fused[key].metadata = dict(item.metadata)
                fused[key].metadata["retrievers"] = []
            fused[key].metadata["retrievers"].append(
                item.metadata.get("retriever", "unknown")
            )

    for key, item in fused.items():
        item.score = contributions[key]
        item.metadata.pop("retriever", None)

    ordered = sorted(fused.values(), key=lambda e: e.score, reverse=True)
    return ordered[:top_k]


@requires_tenant
def hybrid_search(query: str, tenant: str | None = None, top_k: int | None = None) -> list[Evidence]:
    """Run both retrievers and fuse the results, scoped to one tenant."""
    settings = get_settings()
    tenant = resolve_tenant(tenant)
    per_retriever_k = settings.retrieve_top_k
    dense = dense_search(query, tenant, per_retriever_k)
    lexical = bm25_search(query, tenant, per_retriever_k)
    logger.info("retrieved dense=%d bm25=%d", len(dense), len(lexical))
    return rrf_fuse(dense, lexical, top_k=top_k)
