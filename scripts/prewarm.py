"""Pre-warm the demo queries into the answer cache.

A cold multi-agent run is 30-70s, which is fine in normal use and terrible in a
recorded walkthrough. Running each canned query once populates the per-tenant
answer cache, so the demo shows the same real answers served instantly.

This warms the cache; it does not fake anything. The cached payload is exactly
what the pipeline produced, and the UI labels it as cached.

Usage:
    python -m scripts.prewarm --base http://127.0.0.1:8123
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# The three demo queries, matching docs/DEMO.md and the UI presets.
DEMO_QUERIES = [
    ("clean", "Should the client adopt AI to automate invoice processing as part of their transformation roadmap?"),
    ("conflicting", "Our internal AP automation pilot measured an error rate of around 6 percent, but AP automation vendors claim under 1 percent. What error rate should we plan for?"),
    ("unanswerable", "What was the client's exact headcount attrition rate in the Singapore office in Q3 2019?"),
]


def _post(url: str, payload: dict, token: str | None = None, timeout: int = 400) -> dict:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local/base URL
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-warm demo queries into cache.")
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--role", default="admin")
    args = parser.parse_args()

    base = args.base.rstrip("/")

    try:
        token = _post(f"{base}/v1/token", {"tenant_id": args.tenant, "role": args.role})["access_token"]
    except urllib.error.HTTPError as exc:
        print(f"could not obtain token ({exc.code}). Is ENABLE_DEV_TOKEN_ENDPOINT on?", file=sys.stderr)
        return 1

    print(f"pre-warming {len(DEMO_QUERIES)} queries for tenant={args.tenant}\n")
    for label, question in DEMO_QUERIES:
        started = time.perf_counter()
        try:
            result = _post(f"{base}/v1/research", {"question": question}, token=token)
        except Exception as exc:  # noqa: BLE001 - report and continue warming the rest
            print(f"  {label:14s} FAILED: {exc}")
            continue
        elapsed = time.perf_counter() - started
        print(
            f"  {label:14s} {elapsed:6.1f}s  verdict={result.get('verdict'):9s} "
            f"conf={result.get('confidence')} conflicts={len(result.get('conflicts', []))} "
            f"run={str(result.get('run_id'))[:8]}"
        )

    # Second pass proves the cache is populated and measures the warmed latency.
    print("\nverifying cache:")
    for label, question in DEMO_QUERIES:
        started = time.perf_counter()
        try:
            result = _post(f"{base}/v1/research", {"question": question}, token=token, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:14s} FAILED: {exc}")
            continue
        elapsed = (time.perf_counter() - started) * 1000
        cached = result.get("cached", False)
        print(f"  {label:14s} {elapsed:7.0f}ms  cached={cached}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
