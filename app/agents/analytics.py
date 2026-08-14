"""Analytics agent: answers quantitative questions from the metrics table.

The LLM never writes SQL here. A sub-question is matched to one of a small set
of known metric intents, and each intent maps to a fixed, parameterized,
read-only, tenant-scoped query. That means the blast radius of a bad
classification is "wrong metric returned", not "arbitrary SQL executed".
"""

import logging
import re
from typing import Any

from app.agents.base import Agent, AgentCard
from app.knowledge.evidence import Evidence
from app.providers.tools.permissions import check_tool
from app.tenancy.isolation import resolve_tenant

logger = logging.getLogger(__name__)

AGENT_NAME = "analytics"
METRICS_TABLE = "metrics"

# The only SQL this agent can run. Parameterized on tenant and metric name.
_LATEST_METRIC_SQL = f"""
    SELECT metric_name, metric_value, unit, period, source, note
    FROM {METRICS_TABLE}
    WHERE tenant = %s AND metric_name = %s
    ORDER BY period DESC
    LIMIT %s
"""  # noqa: S608 - table name is a module constant, not user input

# Keyword -> metric name. First match wins, so order matters: more specific
# intents are listed before general ones.
_METRIC_INTENTS: list[tuple[str, re.Pattern[str]]] = [
    ("pilot_error_rate", re.compile(r"\b(error rate|rework|defect|accuracy|exception)\b", re.I)),
    ("pilot_straight_through_rate", re.compile(r"\b(straight[- ]through|touchless|stp)\b", re.I)),
    ("milestone_completion_rate", re.compile(r"\b(milestone|completion|on schedule|programme progress|roadmap progress)\b", re.I)),
    ("monthly_invoice_volume", re.compile(r"\b(volume|how many invoices|invoice count|throughput)\b", re.I)),
    ("invoice_processing_cost", re.compile(r"\b(cost per invoice|processing cost|unit cost)\b", re.I)),
]

KNOWN_METRICS = [name for name, _ in _METRIC_INTENTS]


def match_metrics(subquestion: str) -> list[str]:
    """Map a sub-question to the metric names it is asking about."""
    matched = [name for name, pattern in _METRIC_INTENTS if pattern.search(subquestion)]
    if not matched and re.search(r"\b(invoice|ap|accounts payable)\b", subquestion, re.I):
        # Quantitative question about invoices with no specific metric named.
        matched = ["monthly_invoice_volume", "invoice_processing_cost"]
    return matched


class AnalyticsAgent(Agent):
    """Runs fixed read-only queries and returns tabular evidence."""

    card = AgentCard(
        name=AGENT_NAME,
        description="Answers quantitative questions from the tenant's metrics table.",
        skills=[
            {
                "id": "metric_lookup",
                "name": "Metric Lookup",
                "description": f"Returns measured values for: {', '.join(KNOWN_METRICS)}",
            }
        ],
    )

    def research(self, subquestion: str, tenant: str | None = None, limit: int = 2) -> list[Evidence]:
        """Return evidence for whichever known metrics the sub-question names.

        The metric query is parameterized on the resolved tenant. `resolve_tenant`
        raises rather than defaulting, so this agent cannot read another
        tenant's metrics even if it is handed the wrong id.
        """
        check_tool(AGENT_NAME, "sql_metrics")
        tenant = resolve_tenant(tenant)

        metrics = match_metrics(subquestion)
        if not metrics:
            logger.info("analytics: no known metric matched %r", subquestion)
            return []

        # Imported here so the module stays importable without a live database.
        from app.knowledge.retrieval import connect

        evidence: list[Evidence] = []
        with connect() as conn, conn.cursor() as cur:
            for metric in metrics:
                cur.execute(_LATEST_METRIC_SQL, (tenant, metric, limit))
                rows = cur.fetchall()
                for row in rows:
                    name, value, unit, period, source, note = row
                    text = (
                        f"{name.replace('_', ' ')} for period {period} is "
                        f"{value:g} {unit}. {note or ''}".strip()
                    )
                    evidence.append(
                        Evidence(
                            text=text,
                            source=source or "analytics",
                            authority="high",
                            date=str(period),
                            score=1.0,
                            chunk_id=f"metric:{name}:{period}",
                            metadata={
                                "agent": AGENT_NAME,
                                "tenant": tenant,
                                "metric_name": name,
                                "metric_value": value,
                                "unit": unit,
                                "period": period,
                            },
                        )
                    )

        logger.info("analytics %r -> %d rows across %s", subquestion, len(evidence), metrics)
        return evidence

    async def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Agent-interface entrypoint."""
        evidence = self.research(task["question"], context["tenant"])
        return {"evidence": [e.to_dict() for e in evidence]}
