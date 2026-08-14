"""Seed the analytics metrics table for the demo tenant.

Usage:
    python -m scripts.seed_metrics
    python -m scripts.seed_metrics --reset
"""

import argparse
import logging
import sys

from app.config.settings import get_settings
from app.knowledge.retrieval import connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_metrics")

METRICS_TABLE = "metrics"

# (metric_name, value, unit, period, source, note)
SEED_ROWS = [
    (
        "pilot_error_rate",
        6.2,
        "percent",
        "2025-Q4",
        "AP Automation Pilot Telemetry",
        "Share of pilot invoices requiring manual rework.",
    ),
    (
        "pilot_error_rate",
        5.8,
        "percent",
        "2026-Q1",
        "AP Automation Pilot Telemetry",
        "Share of pilot invoices requiring manual rework.",
    ),
    (
        "pilot_straight_through_rate",
        71.4,
        "percent",
        "2026-Q1",
        "AP Automation Pilot Telemetry",
        "Invoices processed with no human touch during the pilot.",
    ),
    (
        "milestone_completion_rate",
        63.0,
        "percent",
        "2026-Q1",
        "Transformation PMO",
        "Wave 1 milestones completed on schedule.",
    ),
    (
        "milestone_completion_rate",
        58.0,
        "percent",
        "2025-Q4",
        "Transformation PMO",
        "Wave 1 milestones completed on schedule.",
    ),
    (
        "monthly_invoice_volume",
        3520.0,
        "invoices",
        "2026-01",
        "ERP Accounts Payable Ledger",
        "Supplier invoices received in the month.",
    ),
    (
        "monthly_invoice_volume",
        3610.0,
        "invoices",
        "2026-02",
        "ERP Accounts Payable Ledger",
        "Supplier invoices received in the month.",
    ),
    (
        "invoice_processing_cost",
        11.40,
        "GBP per invoice",
        "2026-Q1",
        "Finance Operations Costing Model",
        "Fully loaded cost per supplier invoice processed.",
    ),
]


def create_schema(reset: bool = False) -> None:
    """Create the metrics table."""
    with connect() as conn, conn.cursor() as cur:
        if reset:
            logger.warning("dropping table %s", METRICS_TABLE)
            cur.execute(f"DROP TABLE IF EXISTS {METRICS_TABLE};")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {METRICS_TABLE} (
                id           SERIAL PRIMARY KEY,
                tenant       TEXT NOT NULL,
                metric_name  TEXT NOT NULL,
                metric_value DOUBLE PRECISION NOT NULL,
                unit         TEXT,
                period       TEXT,
                source       TEXT,
                note         TEXT,
                UNIQUE (tenant, metric_name, period)
            );
            """
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS metrics_tenant_name_idx ON {METRICS_TABLE} (tenant, metric_name);"
        )
        conn.commit()


def seed(tenant: str) -> int:
    """Insert the seed rows for a tenant."""
    with connect() as conn, conn.cursor() as cur:
        for name, value, unit, period, source, note in SEED_ROWS:
            cur.execute(
                f"""
                INSERT INTO {METRICS_TABLE}
                    (tenant, metric_name, metric_value, unit, period, source, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant, metric_name, period) DO UPDATE SET
                    metric_value = EXCLUDED.metric_value,
                    unit = EXCLUDED.unit,
                    source = EXCLUDED.source,
                    note = EXCLUDED.note;
                """,  # noqa: S608 - table name is a module constant
                (tenant, name, value, unit, period, source, note),
            )
        conn.commit()
    return len(SEED_ROWS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo metrics.")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the metrics table")
    parser.add_argument("--tenant", default=None)
    args = parser.parse_args()

    tenant = args.tenant or get_settings().default_tenant
    create_schema(reset=args.reset)
    count = seed(tenant)
    logger.info("seeded %d metric rows for tenant=%s", count, tenant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
