"""The PR's own checks, read through the GitHub API: the last verifier.

The steward proves in its clone what a clone can prove. The browser and
API checks only exist on the PR, so after opening it the steward waits
for the head commit's check runs, hands the failing slopstopper ones to
the agent with the tail of each job's log, pushes the fix and waits
again, within `ci.wait_minutes` per wait, `ci.fix_rounds` rounds and the
run's token deadline. A PR it cannot get green is a draft that says what
is red. The App's Actions: read permission covers the reads here.
"""

from __future__ import annotations

import time
from typing import Callable

from steward import gh
from steward.redact import redact

POLL_SECONDS = 30
APPEAR_SECONDS = 180          # a head with no check run this long has no CI worth waiting for
LOG_LINES = 80
LOG_CHARS = 6000
RED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
WORKFLOW_PREFIX = ".github/workflows/ss-"   # slopstopper's workflows: what the triage skill covers


def check_runs(repo: str, sha: str) -> list[dict]:
    """The check runs on `sha`, each as {id, name, status, conclusion, url, app}."""
    data = gh.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
    rows = data.get("check_runs") if isinstance(data, dict) else None
    out = []
    for r in rows or []:
        out.append({"id": r.get("id"), "name": str(r.get("name") or ""), "status": str(r.get("status") or ""),
                    "conclusion": r.get("conclusion"), "url": str(r.get("html_url") or ""),
                    "app": str(((r.get("app") or {}).get("slug")) or "")})
    return out


def settled(runs: list[dict]) -> bool:
    return bool(runs) and all(r["status"] == "completed" for r in runs)


def failed(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r["status"] == "completed" and r["conclusion"] in RED]


def wait(repo: str, sha: str, seconds: float, now: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep) -> tuple[list[dict], bool]:
    """Poll until every check run on `sha` has completed, or `seconds` have
    passed. Returns (runs, timed_out). A head on which no check run appears
    within APPEAR_SECONDS is settled with none: the repo runs no CI on it."""
    start = now()
    runs = check_runs(repo, sha)
    while True:
        if runs and settled(runs):
            return runs, False
        if not runs and now() - start >= APPEAR_SECONDS:
            return [], False
        if now() - start >= seconds:
            return runs, True
        sleep(min(POLL_SECONDS, max(1.0, seconds - (now() - start))))
        runs = check_runs(repo, sha)


def workflow_of(repo: str, job_id) -> str:
    """The workflow file behind a GitHub Actions check run (its id is the job
    id), or "" for a check another app posted."""
    try:
        job = gh.api(f"repos/{repo}/actions/jobs/{job_id}") or {}
        run = gh.api(f"repos/{repo}/actions/runs/{job['run_id']}") if job.get("run_id") else None
    except (gh.GhError, KeyError, TypeError):
        return ""
    return str((run or {}).get("path") or "")


def describe(repo: str, runs: list[dict]) -> list[dict]:
    """The failed runs, each with its workflow and whether it is slopstopper's
    (`ours`): the ones the triage skill covers and the agent may be asked to settle."""
    out = []
    for r in failed(runs):
        workflow = workflow_of(repo, r["id"]) if r["app"] == "github-actions" else ""
        out.append({**r, "workflow": workflow, "ours": workflow.startswith(WORKFLOW_PREFIX)})
    return out


def log_tail(repo: str, job_id, lines: int = LOG_LINES) -> str:
    """The end of a job's log, redacted and bounded, for the agent's brief."""
    result = gh.gh("api", f"repos/{repo}/actions/jobs/{job_id}/logs", timeout=120)
    if result.returncode != 0:
        return f"(the log could not be read: {redact(result.stderr.strip()[-200:])})"
    tail = "\n".join(result.stdout.strip().splitlines()[-lines:])
    return redact(tail)[-LOG_CHARS:]


def failures_markdown(red: list[dict], tails: dict[str, str]) -> str:
    """`.steward-ci-failures.md`: what failed on the PR and what each job printed last."""
    parts = ["# The PR's failing checks\n"]
    for r in red:
        parts.append(f"## {r['name']} ({r['conclusion']})\n\nWorkflow: `{r['workflow'] or 'not a GitHub Actions job'}`"
                     f"\nRun: {r['url']}\n")
        if r["name"] in tails:
            parts.append("```text\n" + tails[r["name"]] + "\n```\n")
    return "\n".join(parts)


def summary(red: list[dict]) -> str:
    return ", ".join(sorted({r["name"] for r in red}))
