"""GCS-backed PDF report storage.

Reports are generated in the browser (`web/src/pdfReport.ts`) and posted here
as bytes rather than rebuilt server-side, so there is exactly one place that
knows how to lay a report out. This module's only job is to hold the finished
file somewhere shareable and hand back a link.

Off by default: `upload_pdf` returns None whenever the bucket isn't
configured, and every caller treats that as "cloud storage isn't set up yet,"
not an error -- callers fall back to the local, in-browser download that
already worked before this existed.
"""

import json
import logging
from datetime import timedelta
from functools import lru_cache
from typing import Any

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _client() -> Any | None:
    """Build the GCS client once per process, or None if not configured.

    Cached the same way `get_embedder` caches the embedding model: building a
    client involves parsing a service-account key, worth doing once.
    """
    settings = get_settings()
    if not settings.gcs_bucket_name or not settings.gcp_service_account_json:
        return None

    try:
        from google.cloud import storage
        from google.oauth2 import service_account
    except ImportError:
        logger.warning("google-cloud-storage not installed; PDF cloud storage disabled")
        return None

    try:
        info = json.loads(settings.gcp_service_account_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        return storage.Client(credentials=credentials, project=info.get("project_id"))
    except Exception:
        # Bad JSON, revoked key, wrong project -- any of these means "not
        # usable right now," not "crash the request that triggered this."
        logger.exception("failed to build GCS client from GCP_SERVICE_ACCOUNT_JSON")
        return None


def upload_pdf(tenant: str, run_id: str, pdf_bytes: bytes) -> dict[str, Any] | None:
    """Upload a report PDF and return a time-limited signed download URL.

    Returns None when cloud storage isn't configured -- the caller's job to
    decide what that means for the request (typically: fall back to local
    download rather than fail it).

    The object path is namespaced by tenant, so one tenant's signed URL can
    never resolve to another tenant's report even if a run_id were guessed --
    isolation here is structural (the path itself), same as the tenant
    predicate on every SQL query in app.knowledge.retrieval.
    """
    client = _client()
    if client is None:
        return None

    settings = get_settings()
    blob_path = f"{tenant}/{run_id}.pdf"

    bucket = client.bucket(settings.gcs_bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(pdf_bytes, content_type="application/pdf")

    ttl = timedelta(hours=settings.pdf_signed_url_ttl_hours)
    url = blob.generate_signed_url(
        version="v4",
        expiration=ttl,
        method="GET",
        response_disposition=f'attachment; filename="research-report-{run_id}.pdf"',
    )

    logger.info("uploaded report pdf tenant=%s run_id=%s bytes=%d", tenant, run_id, len(pdf_bytes))
    return {"url": url, "expires_in_hours": settings.pdf_signed_url_ttl_hours}
