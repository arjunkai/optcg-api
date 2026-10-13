"""Shared wrangler-call helper with transient-failure retry.

Cloudflare 5xx, edge timeouts, and network blips cause sporadic non-zero
exits from `npx wrangler d1 execute`. Backfill scripts that loop over
many batch files used to bail on the first failure — that's how LID 237
got stranded on an intermediate signal between batches 5 and 9 on
2026-05-17 (see project_ptcg_promo_enumeration_plan.md, Phase 1k).

Both reads (`--json --command "SELECT ..."`) and writes
(`--file=batch.sql`) are idempotent for our backfill workloads:
  - reads return the same payload on retry
  - UPDATE batches use deterministic WHERE clauses
  - INSERT batches use OR IGNORE

So blanket retry-with-backoff is safer than classifying transient vs
hard failures by stderr substring. Hard failures still surface after
attempts exhaust.

Usage:
    from scripts.wrangler_retry import run_wrangler

    WRANGLER = ["node", "./node_modules/wrangler/bin/wrangler.js",
                "d1", "execute", "optcg-cards"]

    # write batch
    result = run_wrangler(WRANGLER + ["--remote", f"--file={path}"])
    if result.returncode != 0:
        print(f"FAIL after retries: {(result.stderr or '')[:400]}")
        sys.exit(1)

    # read query
    result = run_wrangler(WRANGLER + ["--remote", "--json",
                                       "--command", sql])
    if result.returncode != 0:
        sys.exit(1)
    payload = result.stdout
"""

from __future__ import annotations

import json
import subprocess
import time

WRANGLER_MAX_ATTEMPTS = 3
WRANGLER_RETRY_BACKOFF_SECONDS = (5, 15)  # waits before attempts 2 and 3


def run_wrangler(
    cmd: list[str],
    max_attempts: int = WRANGLER_MAX_ATTEMPTS,
    backoff_seconds: tuple[int, ...] = WRANGLER_RETRY_BACKOFF_SECONDS,
) -> subprocess.CompletedProcess:
    """Run a wrangler command, retrying on non-zero exit.

    Always captures stdout/stderr (text, utf-8, errors=replace) so the
    caller can inspect output regardless of pattern. Returns the final
    CompletedProcess — either the first success or the last failure.
    The caller decides how to react to a final non-zero returncode.

    Prints a one-line retry notice between attempts so background runs
    are diagnosable from logs.
    """
    last_result: subprocess.CompletedProcess | None = None
    for attempt in range(1, max_attempts + 1):
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            if attempt > 1:
                print(f"     ok after {attempt} attempt(s)")
            return result
        last_result = result
        if attempt < max_attempts:
            # backoff_seconds is indexed by (attempt - 1); clamp to last
            # entry if caller passes a shorter tuple than max_attempts.
            wait_idx = min(attempt - 1, len(backoff_seconds) - 1)
            wait = backoff_seconds[wait_idx]
            # stderr starts with wrangler's config banner; the cause is
            # in stdout (JSON error under --json) or at stderr's end.
            err = wrangler_error(result).replace("\n", " ")[-200:]
            print(f"     attempt {attempt} failed ({err}); "
                  f"retrying in {wait}s...")
            time.sleep(wait)
    assert last_result is not None
    return last_result


def wrangler_error(result: subprocess.CompletedProcess) -> str:
    """The reason a failed `wrangler ... --json` call gave, as one string.

    With --json, wrangler prints its error as `{"error": {"text": ...}}`
    on stdout. stderr holds only the config banner (the yellow WARNING
    about `"unsafe" fields` in wrangler.toml), so printing stderr alone
    hides the cause. Falls back to the tail of stdout + stderr when stdout
    has no JSON error object.
    """
    stdout = result.stdout or ""
    start = stdout.find("{")
    if start >= 0:
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            parts = [err.get("text") or ""]
            parts += [n.get("text", "") for n in err.get("notes") or []
                      if isinstance(n, dict)]
            text = " ".join(p.strip() for p in parts if p and p.strip())
            if text:
                return text
    return (stdout + (result.stderr or "")).strip()[-1500:]


def wrangler_json_rows(result: subprocess.CompletedProcess) -> list[dict]:
    """Rows from a successful `wrangler d1 execute --json --command`.

    Parses stdout only (wrangler's warnings go to stderr) and starts at
    the first line that opens the JSON array, so a stray banner line on
    stdout can't be taken for the payload. Raises ValueError when the
    call failed or stdout holds no parseable result, rather than quietly
    returning no rows.
    """
    if result.returncode != 0:
        raise ValueError(wrangler_error(result))
    stdout = result.stdout or ""
    lines = stdout.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            try:
                payload = json.loads("".join(lines[i:]))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0].get("results") or []
    raise ValueError(f"no JSON result in wrangler output: {stdout.strip()[-500:]!r}")
