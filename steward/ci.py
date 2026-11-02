"""The PR's own checks, read through the GitHub API: the last verifier.

The steward proves in its clone what a clone can prove. The browser and
API checks only exist on the PR, so after opening it the steward waits
for the head commit's check runs, hands the failing slopstopper ones to
the agent with the tail of each job's log, pushes the fix and waits
again, within `ci.wait_minutes` per wait, `ci.fix_rounds` rounds and the
run's token deadline. A PR it cannot get green is a draft that says what
is red. The reads need the App's Checks: read (check runs) and Actions:
read (jobs and their logs).

"Settled" is more than "nothing running": the PR-triggered `ss-*`
workflows on the branch must each have a check run, and the set of runs
must have held still for QUIET_SECONDS, since a PR's runs register over
a minute or more and a bot's instant check would otherwise read as the
whole of CI.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from steward import gh
from steward.redact import redact

POLL_SECONDS = 30
APPEAR_SECONDS = 180          # a head with no check run this long, and none expected, has no CI
QUIET_SECONDS = 90            # the run set must hold still this long before it counts as settled
GRACE_SECONDS = 300           # complete and still this long, the wait ends even if an expected workflow never ran
LOG_LINES = 80
LOG_CHARS = 6000
RED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
WORKFLOW_PREFIX = ".github/workflows/ss-"   # slopstopper's workflows: what the triage skill covers


def check_runs(repo: str, sha: str) -> list[dict]:
    """The check runs on `sha`, each as {id, name, status, conclusion, url, app}."""
    try:
        data = gh.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
    except gh.GhError as exc:
        if "403" in str(exc):
            raise gh.GhError(f"{exc} (reading check runs needs the App's Checks: read permission)") from exc
        raise
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


# `pull_request` only: a `pull_request_target` workflow (the labeler) runs in
# the base's context and does not reliably report a check run on the head.
_ON_PR = re.compile(r"^\s*pull_request\s*:|^on:\s*\[[^\]]*\bpull_request\b(?!_target)", re.M)


def expected_workflows(root: Path) -> set[str]:
    """The slopstopper workflows on the branch that run on a pull request,
    as `.github/workflows/<name>`: each should produce a check run on the
    PR's head, and the wait is not over until each has, or until the runs
    that did come have been complete and still for GRACE_SECONDS (a
    `paths:` filter can keep a workflow from running at all)."""
    out = set()
    for path in sorted((root / ".github" / "workflows").glob("ss-*.yml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _ON_PR.search(text):
            out.add(f".github/workflows/{path.name}")
    return out


def _learn(repo: str, runs: list[dict], workflows: dict) -> None:
    for r in runs:
        if r["id"] not in workflows:
            workflows[r["id"]] = workflow_of(repo, r["id"]) if r["app"] == "github-actions" else ""


def wait(repo: str, sha: str, seconds: float, expected: set[str] = frozenset(), workflows: dict | None = None,
         now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep
         ) -> tuple[list[dict], bool, dict]:
    """Poll until the checks on `sha` have settled, or `seconds` have passed.
    Settled: every run complete, every expected workflow present, and the
    run set unchanged for QUIET_SECONDS. Returns (runs, timed_out, the
    run id -> workflow path map learned on the way). A head on which no
    run appears within APPEAR_SECONDS, with none expected, is settled with
    none; with some expected, the wait goes on to its limit."""
    workflows = {} if workflows is None else workflows
    start = now()
    fingerprint, since = None, start
    runs = check_runs(repo, sha)
    while True:
        fp = tuple(sorted((r["id"], r["status"], r["conclusion"]) for r in runs))
        if fp != fingerprint:
            fingerprint, since = fp, now()
        _learn(repo, runs, workflows)
        seen = set(workflows.values())
        if runs and settled(runs):
            still = now() - since
            if (expected <= seen and still >= QUIET_SECONDS) or still >= GRACE_SECONDS:
                return runs, False, workflows
        if not runs and not expected and now() - start >= APPEAR_SECONDS:
            return [], False, workflows
        if now() - start >= seconds:
            return runs, True, workflows
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


def describe(repo: str, runs: list[dict], workflows: dict | None = None) -> list[dict]:
    """The failed runs, each with its workflow and whether it is slopstopper's
    (`ours`): the ones the triage skill covers and the agent may be asked to settle."""
    workflows = {} if workflows is None else workflows
    _learn(repo, failed(runs), workflows)
    return [{**r, "workflow": workflows[r["id"]], "ours": workflows[r["id"]].startswith(WORKFLOW_PREFIX)}
            for r in failed(runs)]


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
