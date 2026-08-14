"""Corpus ingestion: markdown -> chunks -> pgvector + BM25 index.

Reads `data/corpus/*.md` with YAML frontmatter (source, authority, date,
version), chunks the body, embeds locally with all-MiniLM-L6-v2, and upserts
into Postgres under the demo tenant. Also builds and persists the tenant's BM25
index so lexical retrieval works without a query-time rebuild.

Usage:
    python -m scripts.ingest
    python -m scripts.ingest --reset
"""

import argparse
import hashlib
import logging
import pickle
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from app.config.settings import get_settings
from app.knowledge.retrieval import (
    CHUNKS_TABLE,
    bm25_index_path,
    connect,
    embed_texts,
    tokenize,
)
from app.tenancy.isolation import validate_tenant_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("ingest")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


SAMPLE_DOCS: dict[str, str] = {
    "invoice_automation_assessment.md": """---
source: Internal Finance Operations Assessment
authority: high
date: 2025-11-04
version: 1.2
---

# Accounts Payable Automation Assessment

The client's accounts payable function processes approximately 42,000 supplier
invoices per year across three regional shared service centres. Current handling
is largely manual: invoices arrive by email as PDF attachments, are keyed into
the ERP by clerks, and routed for approval over email. Average processing cost is
GBP 11.40 per invoice against a peer benchmark of GBP 4.10, and average cycle
time from receipt to payment approval is 9.6 days.

Error rates are the dominant pain point. Roughly 6.2 percent of invoices require
rework due to keying errors, mismatched purchase orders, or duplicate
submissions. Duplicate payments recovered through audit totalled GBP 310,000 in
the last financial year.

An intelligent document processing capability applied to invoice intake and
three-way matching is assessed as the highest-return automation candidate in the
finance function. Modelled savings are GBP 280,000 to GBP 350,000 annually once
steady state is reached, against implementation cost of GBP 190,000 and annual
run cost of GBP 45,000. Payback is estimated at 14 to 18 months.

Straight-through processing of 70 to 80 percent of invoice volume is considered
achievable for the client's supplier mix, with the remainder routed to human
review. Vendors quoting above 95 percent straight-through rates are relying on
benchmarks from far more standardised supplier bases than the client's.
""",
    "transformation_roadmap.md": """---
source: Enterprise Transformation Roadmap FY26-FY28
authority: high
date: 2026-01-19
version: 3.0
---

# Transformation Roadmap Sequencing

The transformation programme is sequenced in three waves. Wave 1 (FY26) targets
process standardisation and data quality remediation in finance and procurement.
Wave 2 (FY27) introduces automation and machine learning capability on top of the
standardised processes. Wave 3 (FY28) addresses customer-facing digital services.

The governing principle of the roadmap is that automation is applied only to
processes that have already been standardised. Automating a non-standard process
encodes existing variation into software and raises the cost of later change.
Two prior initiatives at the client failed on precisely this point: a 2023
robotic process automation pilot in procurement was decommissioned after 11
months because underlying process variation across regions made the bots brittle.

Finance is the recommended entry point for Wave 2 automation. It has the highest
transaction volumes, the clearest rules, and the most mature process
documentation following Wave 1 remediation. Accounts payable specifically is
named as the leading candidate.

Programme funding assumes no more than two major automation deployments run
concurrently. Governance capacity, not capital, is the binding constraint.
""",
    "ai_governance_policy.md": """---
source: Group AI Governance and Risk Policy
authority: medium
date: 2025-09-30
version: 2.1
---

# AI Governance Requirements

Any deployment of machine learning or AI capability into a financial process is
classed as a controlled change under the Group AI Governance Policy. Three
requirements apply before production deployment.

First, a human review gate is mandatory for any automated decision with a
financial value above GBP 5,000. Fully autonomous approval of high-value invoices
is not permitted under current policy, regardless of model accuracy.

Second, model outputs affecting the financial ledger must be auditable for seven
years. Vendors that cannot export decision-level audit logs are not eligible.

Third, a bias and error review must be completed quarterly for the first year of
operation, then annually. This carries an ongoing compliance cost of
approximately GBP 30,000 per year per deployed model, which is frequently omitted
from vendor business cases.

Policy review is scheduled for Q3 FY26 and the human review threshold is expected
to be revisited, but planning should assume the current threshold holds.
""",
}


def ensure_corpus(corpus_dir: Path) -> None:
    """Create sample documents if the corpus directory is empty."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    if any(corpus_dir.glob("*.md")):
        return

    logger.info("corpus empty; writing %d sample documents", len(SAMPLE_DOCS))
    for filename, content in SAMPLE_DOCS.items():
        (corpus_dir / filename).write_text(content, encoding="utf-8")


def parse_document(path: Path) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from the markdown body."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        logger.warning("%s has no frontmatter; using filename as source", path.name)
        return {"source": path.stem}, raw

    meta = yaml.safe_load(match.group(1)) or {}
    return meta, match.group(2)


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split on paragraph boundaries, packing up to `size` characters.

    Paragraph-aware packing keeps related sentences together, which matters more
    for retrieval quality than hitting an exact chunk length.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            current = f"{current}\n\n{para}".strip() if current else para

    if current:
        chunks.append(current)
    return chunks


def create_schema(reset: bool = False) -> None:
    """Create the chunks table and its indexes."""
    settings = get_settings()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        if reset:
            logger.warning("dropping table %s", CHUNKS_TABLE)
            cur.execute(f"DROP TABLE IF EXISTS {CHUNKS_TABLE};")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CHUNKS_TABLE} (
                chunk_id   TEXT PRIMARY KEY,
                tenant     TEXT NOT NULL,
                text       TEXT NOT NULL,
                source     TEXT,
                authority  TEXT,
                date       TEXT,
                version    TEXT,
                embedding  VECTOR({settings.embed_dim})
            );
            """
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON {CHUNKS_TABLE} (tenant);"
        )
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS chunks_embedding_idx
            ON {CHUNKS_TABLE} USING hnsw (embedding vector_cosine_ops);
            """
        )
        conn.commit()


def build_records(corpus_dir: Path, tenant: str) -> list[dict[str, Any]]:
    """Parse and chunk every document into upsertable records."""
    settings = get_settings()
    records: list[dict[str, Any]] = []

    for path in sorted(corpus_dir.glob("*.md")):
        meta, body = parse_document(path)
        chunks = chunk_text(body, settings.chunk_size_chars, settings.chunk_overlap_chars)
        logger.info("%s -> %d chunks", path.name, len(chunks))

        for index, chunk in enumerate(chunks):
            digest = hashlib.sha256(f"{path.name}:{index}:{chunk}".encode()).hexdigest()[:12]
            records.append(
                {
                    "chunk_id": f"{path.stem}:{index}:{digest}",
                    "tenant": tenant,
                    "text": chunk,
                    "source": str(meta.get("source", path.stem)),
                    "authority": str(meta.get("authority", "unknown")),
                    "date": str(meta.get("date", "unknown")),
                    "version": str(meta.get("version", "unknown")),
                }
            )
    return records


def upsert(records: list[dict[str, Any]]) -> None:
    """Embed and write records to pgvector."""
    logger.info("embedding %d chunks", len(records))
    vectors = embed_texts([r["text"] for r in records])

    with connect() as conn, conn.cursor() as cur:
        for record, vector in zip(records, vectors):
            cur.execute(
                f"""
                INSERT INTO {CHUNKS_TABLE}
                    (chunk_id, tenant, text, source, authority, date, version, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    text = EXCLUDED.text,
                    source = EXCLUDED.source,
                    authority = EXCLUDED.authority,
                    date = EXCLUDED.date,
                    version = EXCLUDED.version,
                    embedding = EXCLUDED.embedding;
                """,  # noqa: S608 - table name is a module constant
                (
                    record["chunk_id"],
                    record["tenant"],
                    record["text"],
                    record["source"],
                    record["authority"],
                    record["date"],
                    record["version"],
                    vector,
                ),
            )
        conn.commit()
    logger.info("upserted %d chunks into pgvector", len(records))


def purge_tenant_chunks(tenant: str) -> int:
    """Delete one tenant's chunks. Keyed on tenant, so no other org is touched."""
    safe = validate_tenant_id(tenant)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {CHUNKS_TABLE} WHERE tenant = %s", (safe,))  # noqa: S608
        deleted = cur.rowcount
        conn.commit()
    logger.warning("purged %d existing chunks for tenant %s", deleted, safe)
    return deleted


def build_bm25(tenant: str) -> int:
    """Build and persist the tenant's BM25 index from what is in Postgres.

    Reading back from the database rather than from the in-memory records keeps
    the lexical index consistent with the vector store even across partial runs.
    """
    from rank_bm25 import BM25Okapi

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT chunk_id, text, source, authority, date FROM {CHUNKS_TABLE} WHERE tenant = %s",  # noqa: S608
            (tenant,),
        )
        rows = cur.fetchall()

    if not rows:
        logger.warning("no chunks for tenant %r; skipping BM25 index", tenant)
        return 0

    records = [
        {
            "chunk_id": r[0],
            "text": r[1],
            "source": r[2],
            "authority": r[3],
            "date": r[4],
        }
        for r in rows
    ]
    corpus = [tokenize(r["text"]) for r in records]

    path = bm25_index_path(tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump({"bm25": BM25Okapi(corpus), "records": records}, fh)

    logger.info("persisted BM25 index (%d docs) to %s", len(records), path)
    return len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest a corpus into pgvector + BM25.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the chunks table")
    parser.add_argument(
        "--tenant",
        default=None,
        help="tenant to ingest under; its own vector rows and BM25 index",
    )
    parser.add_argument(
        "--corpus-dir",
        default=None,
        help="directory of .md documents to ingest (default: CORPUS_DIR). Each org "
        "points at its own directory so tenants hold different documents.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="delete this tenant's existing chunks before ingesting (this tenant only)",
    )
    args = parser.parse_args()

    settings = get_settings()
    # Validated, because the tenant id becomes part of the BM25 index filename.
    tenant = validate_tenant_id(args.tenant or settings.default_tenant)
    corpus_dir = Path(args.corpus_dir or settings.corpus_dir)

    if not corpus_dir.exists():
        logger.error("corpus dir %s does not exist", corpus_dir)
        return 1

    # Only seed the sample docs into the default corpus dir; an org's own
    # directory should never be silently populated with someone else's documents.
    if args.corpus_dir is None:
        ensure_corpus(corpus_dir)

    create_schema(reset=args.reset)

    if args.purge:
        purge_tenant_chunks(tenant)

    records = build_records(corpus_dir, tenant)
    if not records:
        logger.error("no chunks produced; nothing to ingest")
        return 1

    upsert(records)
    indexed = build_bm25(tenant)

    logger.info(
        "done: tenant=%s corpus=%s chunks=%d bm25_docs=%d bm25_index=%s",
        tenant,
        corpus_dir,
        len(records),
        indexed,
        bm25_index_path(tenant),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
