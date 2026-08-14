"""Pre-deploy hardening checklist.

Verifies each item and prints PASS / FAIL / WARN. Exits non-zero if any
production-critical check fails, so it can gate a deploy rather than being a
document someone remembers to read.

Usage:
    python -m scripts.preflight            # check current .env
    python -m scripts.preflight --prod     # enforce production rules
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from app.config.settings import get_settings

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

# Secrets must never be committed. Any tracked file matching these is a hard fail.
_SECRET_FILES = {".env", ".env.production", ".env.local"}
_WEAK_SECRETS = {"", "change-me", "dev", "devsecret", "secret", "changeme", "test"}


class Check:
    def __init__(self, name: str, fn: Callable[[], tuple[str, str]], critical: bool = True):
        self.name, self.fn, self.critical = name, fn, critical


def check_dev_token(prod: bool) -> tuple[str, str]:
    s = get_settings()
    if not s.enable_dev_token_endpoint:
        return PASS, "POST /v1/token is disabled (returns 404)"
    msg = "ENABLE_DEV_TOKEN_ENDPOINT=true — mints tenant credentials to anyone"
    return (FAIL if prod else WARN), msg


def check_jwt_secret(prod: bool) -> tuple[str, str]:
    secret = get_settings().jwt_secret.strip()
    if not secret:
        return FAIL, "JWT_SECRET is empty — the API cannot verify tokens"
    if secret.lower() in _WEAK_SECRETS:
        return FAIL, f"JWT_SECRET is a placeholder value ({secret[:4]}…)"
    if len(secret) < 32:
        return (FAIL if prod else WARN), f"JWT_SECRET is only {len(secret)} chars; use >= 32"
    return PASS, f"JWT_SECRET set ({len(secret)} chars)"


def check_cors(prod: bool) -> tuple[str, str]:
    origins = [o.strip() for o in get_settings().cors_allow_origins.split(",") if o.strip()]
    if "*" in origins:
        return FAIL, "CORS allows '*' — any page could spend a tenant's quota"
    if not origins:
        return PASS, "CORS empty = same-origin only (API serves the UI bundle)"
    return PASS, f"CORS restricted to {origins}"


def check_rate_limits(prod: bool) -> tuple[str, str]:
    s = get_settings()
    if not s.rate_limit_enabled:
        return (FAIL if prod else WARN), "RATE_LIMIT_ENABLED=false — no per-tenant quota"
    return PASS, f"rate limiting on ({s.default_rpm}/min, {s.default_rpd}/day per tenant)"


def check_debug(prod: bool) -> tuple[str, str]:
    s = get_settings()
    problems = []
    if s.log_level.upper() == "DEBUG":
        problems.append("LOG_LEVEL=DEBUG")
    if s.app_env.lower() == "local" and prod:
        problems.append("APP_ENV=local")
    if problems:
        return (FAIL if prod else WARN), "debug settings active: " + ", ".join(problems)
    return PASS, f"APP_ENV={s.app_env} LOG_LEVEL={s.log_level}"


def check_secrets_not_committed(prod: bool) -> tuple[str, str]:
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.split("\n")
    except (subprocess.SubprocessError, OSError) as exc:
        return WARN, f"could not inspect git index: {exc}"

    leaked = [f for f in tracked if Path(f).name in _SECRET_FILES]
    if leaked:
        return FAIL, f"secret files are tracked by git: {leaked}"

    gitignore = Path(".gitignore").read_text() if Path(".gitignore").exists() else ""
    if ".env" not in gitignore:
        return FAIL, ".env is not in .gitignore"
    return PASS, ".env ignored and not tracked"


def check_no_hardcoded_keys(prod: bool) -> tuple[str, str]:
    """Scan tracked source for anything shaped like a live credential."""
    patterns = [
        (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "Google API key"),
        (re.compile(r"gsk_[0-9A-Za-z]{40,}"), "Groq key"),
        (re.compile(r"sk-lf-[0-9a-f-]{20,}"), "Langfuse secret key"),
    ]
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "*.py", "*.ts", "*.tsx", "*.yml", "*.yaml", "*.md"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.split()
    except (subprocess.SubprocessError, OSError) as exc:
        return WARN, f"could not scan: {exc}"

    hits = []
    for path in tracked:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in patterns:
            if pattern.search(text):
                hits.append(f"{path} ({label})")
    if hits:
        return FAIL, f"possible credentials in tracked files: {hits[:5]}"
    return PASS, "no credential-shaped strings in tracked source"


def check_external_datastores(prod: bool) -> tuple[str, str]:
    """On a 1 GB VM, Postgres and Redis must not be local."""
    s = get_settings()
    local = []
    for label, url in (("POSTGRES_URL", s.postgres_url), ("REDIS_URL", s.redis_url)):
        if re.search(r"(localhost|127\.0\.0\.1)", url):
            local.append(label)
    if local and prod:
        return WARN, f"{local} point at localhost; deploy expects external (Neon/Upstash)"
    if local:
        return PASS, f"{local} local — fine for development"
    return PASS, "datastores are external"


def check_ui_built(prod: bool) -> tuple[str, str]:
    index = Path(get_settings().static_dir) / "index.html"
    if not index.exists():
        return (FAIL if prod else WARN), f"UI bundle missing at {index} — run `npm run build` in web/"
    size = sum(f.stat().st_size for f in index.parent.rglob("*") if f.is_file())
    return PASS, f"UI bundle present ({size // 1024} KB)"


def check_tracing(prod: bool) -> tuple[str, str]:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return WARN, "Langfuse keys unset — runs will not be traced (fail-open)"
    return PASS, f"tracing -> {s.langfuse_host}"


CHECKS = [
    Check("Dev token endpoint disabled", check_dev_token),
    Check("Strong JWT secret", check_jwt_secret),
    Check("CORS restricted", check_cors),
    Check("Rate limiting enabled", check_rate_limits),
    Check("Debug settings off", check_debug),
    Check("No secrets committed", check_secrets_not_committed),
    Check("No hardcoded credentials", check_no_hardcoded_keys),
    Check("Datastores external", check_external_datastores, critical=False),
    Check("UI bundle built", check_ui_built),
    Check("Tracing configured", check_tracing, critical=False),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-deploy hardening checklist.")
    parser.add_argument("--prod", action="store_true", help="enforce production rules")
    args = parser.parse_args()

    icons = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m", WARN: "\033[33m!\033[0m"}
    mode = "PRODUCTION" if args.prod else "development"

    print(f"\n{'=' * 72}\nPRE-DEPLOY HARDENING CHECKLIST  ({mode} rules)\n{'=' * 72}")

    failures = 0
    for check in CHECKS:
        try:
            status, detail = check.fn(args.prod)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            status, detail = FAIL, f"check raised: {exc}"
        if status == FAIL and check.critical:
            failures += 1
        print(f"  {icons[status]} {check.name:<32} {detail}")

    print("=" * 72)
    if failures:
        print(f"\033[31m{failures} critical check(s) failed — do not deploy.\033[0m\n")
        return 1
    print("\033[32mAll critical checks passed.\033[0m\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
