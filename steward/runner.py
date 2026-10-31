"""The run: discover, inventory, plan, then work each order in a clone."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from steward import discover, gh, inventory, isolation, prompts, versions, workspace
from steward.config import Config
from steward.execute import agent, refresh
from steward.execute.agent import AgentResult
from steward.plan import Order, Plan, plan_all
from steward.report import Outcome, RunReport
from steward.versions import MIN_INSTALL_VERSION

STEWARD_URL = "https://github.com/hungovercoders/slopstopper-steward"
# An order needs this long, at least, before the token the run pushes with
# expires: clone, install, an agent run, verification, push, PR.
MIN_ORDER_SECONDS = 20 * 60
# What an order keeps back from its agent for verification, push and PR.
AFTER_AGENT_SECONDS = 10 * 60


@dataclass
class RunContext:
    """What one run shares across its orders."""
    base: Path
    deadline: float | None = None  # epoch seconds the push token is good until (STEWARD_DEADLINE)
    releases: dict = field(default_factory=dict)
    agent_check: str | None = None   # agent.available(), once agent_checked
    agent_checked: bool = False
    reach_pending: bool = False      # whether the agent user can reach `base`, still to ask
    checked_clis: set[Path] = field(default_factory=set)   # CLIs proven to run as the agent user

    def agent_unavailable(self) -> str | None:
        """Why the agent cannot run in this run, asked at most once and only
        when an order needs the agent, whether the agent user can reach a
        `--workdir` included."""
        if not self.agent_checked:
            self.agent_check, self.agent_checked = agent.available(), True
        if self.reach_pending and not self.agent_check:
            self.reach_pending = False
            user = isolation.agent_user()
            if user and not isolation.reaches(user, self.base):
                self.agent_check = (f"{user} cannot read and traverse the workdir {self.base}; "
                                    "the agent copies its clone and runs the pinned CLI from there")
        return self.agent_check

    def venv(self, version: str) -> Path:
        """One venv per version per run, outside every clone (the agent's
        working tree), and protected from the agent's edit tools."""
        return self.base / "venvs" / version

    def protected(self) -> list[Path]:
        return [self.base / "venvs"]

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.time()

    def agent_timeout(self) -> int:
        left = self.remaining()
        return 3600 if left is None else int(max(300, min(3600, left - AFTER_AGENT_SECONDS)))


def _deadline() -> float | None:
    try:
        return float(os.environ["STEWARD_DEADLINE"])
    except (KeyError, ValueError):
        return None


def gather(cfg: Config, only: str | None = None) -> tuple[list[inventory.Inventory], dict[str, str | None], str | None]:
    releases = versions.pypi_releases(cfg.package)
    latest = max(releases, key=lambda v: versions.parse(v)) if releases else None  # type: ignore[arg-type]
    repos = discover.discover(cfg, only)
    invs = [inventory.take(r, latest, cfg) for r in repos]
    targets = {inv.repo.full_name: versions.target_version(inv.repo.pinned, releases, cfg.policy, cfg.min_age_days)
               for inv in invs}
    return invs, targets, latest


def release_notes(cfg: Config, pinned: str | None, target: str, cache: dict) -> str:
    """The notes between two pins. `cache` belongs to one run (`execute`
    creates it), so the release list is fetched once per run and never
    outlives it."""
    if cfg.slopstopper_repo not in cache:
        cache[cfg.slopstopper_repo] = gh.api(f"repos/{cfg.slopstopper_repo}/releases?per_page=100", paginate=True) or []
    rows = cache[cfg.slopstopper_repo]
    lo, hi = versions.parse(pinned), versions.parse(target)
    picked = []
    for r in rows:
        v = versions.parse((r.get("tag_name") or "").lstrip("v"))
        if v and hi and v <= hi and (lo is None or v > lo):
            picked.append((v, r))
    picked.sort(reverse=True)
    out = [f"## {r.get('name') or r.get('tag_name')}\n\n{(r.get('body') or '').strip()}\n" for _, r in picked]
    return "\n".join(out) or f"No release notes found between {pinned} and {target}."


def _pr_body(order: Order, cfg: Config, summary: str, extra: str = "", closes: bool = True) -> str:
    """`Closes #N` only when the steward re-ran the finding's check green in
    its clone; otherwise `Refs #N`, so merging never closes an issue on the
    agent's word alone."""
    link = ""
    if order.finding:
        n = order.finding.number
        link = (f"Closes #{n}\n\n" if closes else
                f"Refs #{n}. The steward could not confirm the check green here, so merging leaves the issue open; "
                "close it once the check passes on the default branch (most slopstopper checks close their own issue "
                "when they next pass there).\n\n")
    return (
        f"{link}{summary}\n\n{extra}"
        f"\n---\nOpened by [slopstopper-steward]({STEWARD_URL}) for `{order.repo}`. Order `{order.id}`; "
        f"a label `{cfg.ignore_label}` on an issue keeps the steward off it.\n"
        f"<!-- steward:order={order.id} -->\n"
    )


def _evidence(verdict: dict) -> list[str]:
    """The verdict's failure bullets, or the reports it wrote when the check
    lists none (some reports carry their findings under headings instead)."""
    return list(verdict.get("failures") or []) or [f"report: `{p}`" for p in verdict.get("reports") or []]


def _red_section(heading: str, red: list[dict], tail: str = "") -> str:
    if not red:
        return ""
    rows = [f"- `{r['check']}` ({r['status']})" + "".join(f"\n  - {f}" for f in _evidence(r)[:10]) for r in red]
    return f"### {heading}\n\n" + "\n".join(rows) + f"\n\n{tail}"


def _agent_section(result: AgentResult | None) -> str:
    if not result or not result.ran:
        return ""
    text = f"\n**Agent ({result.outcome}, {result.category or 'n/a'}):** {result.summary}"
    return text + (f"\n\nNotes: {result.notes}" if result.notes else "")


def _upstream_section(result: AgentResult, cfg: Config) -> str:
    if not result.upstream:
        return ""
    return ("### Looks like a slopstopper bug\n\nThe agent judged the fault to be slopstopper's. Raise it on "
            f"`{cfg.slopstopper_repo}` with the form below, then mark the workaround with the issue number.\n\n"
            f"<details><summary>{result.upstream.get('title', 'adopter report')}</summary>\n\n"
            f"{result.upstream.get('body', '')}\n</details>\n\n")


# ── upgrade / refresh ────────────────────────────────────────────────

def _refresh_summary(order: Order, inv: inventory.Inventory, moved: str) -> str:
    if order.kind == "refresh":
        return (f"The suite on `{order.default_branch}` was written by slopstopper-cli {inv.repo.installed_from}; "
                f"this rewrites it from the pinned {order.target}.")
    return (f"`mise.toml` pin {moved}: `{order.pinned or 'none'}` → `{order.target}`, and `slopstopper install` "
            f"from {order.target} rewrote the suite (workflows, shims, composite actions, skills).")


def _verify_pass(ws: workspace.Workspace, cli: Path) -> tuple[list[dict], list[dict]]:
    """One verification pass: list once, run what is runnable. Returns (red, unverified)."""
    checks = refresh.static_checks(ws, cli)
    return refresh.verify(ws, cli, checks.runnable), checks.unverified


def _settle_red(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
                red: list[dict], unverified: list[dict], ctx: RunContext) -> tuple[list[dict], list[dict], AgentResult | None]:
    """Hand a red verification to the agent, then verify again. The second
    pass lists the checks again on purpose: the verify brief allows the
    agent to switch a check off through `workflows.disabled`, which changes
    what the repo keeps."""
    if not red or not cfg.agent_enabled:
        return red, unverified, None
    agent.ensure_skills(ws, order.target, cfg.slopstopper_repo)
    result = agent.run(ws, prompts.verify(order, inv, red, cfg.slopstopper_repo), cfg, cli,
                       timeout=ctx.agent_timeout(), protect=ctx.protected(), preflight=ctx.agent_unavailable,
                       checked=ctx.checked_clis)
    if not result.ran:
        return red, unverified, result
    red, unverified = _verify_pass(ws, cli)
    return red, unverified, result


def _unverified_section(unverified: list[dict]) -> str:
    if not unverified:
        return ""
    rows = "\n".join(f"- `{u['check']}`: {u['why']}" for u in unverified)
    return f"### Not verified by the steward\n\n{rows}\n\nThis PR's own `ss-*` workflows run them.\n\n"


def work_refresh(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, ctx: RunContext) -> Outcome:
    assert order.target
    moved = refresh.set_pin(ws.path / "mise.toml", order.target)
    cli = refresh.cli_for(order.target, ctx.venv(order.target), cfg.package)
    installed = refresh.install_suite(ws, cli)
    if installed.returncode != 0:
        return Outcome(order.to_dict(), "failed",
                       detail=f"slopstopper install exited {installed.returncode}: {installed.stderr.strip()[-400:]}")
    notes = release_notes(cfg, order.pinned, order.target, ctx.releases) if order.kind == "upgrade" else ""
    (ws.path / ".steward-release-notes.md").write_text(notes, encoding="utf-8")
    red, unverified, result = _settle_red(order, inv, cfg, ws, cli, *_verify_pass(ws, cli), ctx)
    if not workspace.commit(ws, f"{order.title}\n\n{order.reason}."):
        return Outcome(order.to_dict(), "skipped", detail="nothing changed after the refresh")
    workspace.push(ws, order.branch)
    summary = _refresh_summary(order, inv, moved) + _agent_section(result)
    extra = _red_section("Still red after the refresh", red, "Opened as a draft: settle these before merging.\n\n")
    extra += _unverified_section(unverified)
    if notes:
        extra += f"<details><summary>Release notes between the two pins</summary>\n\n{notes}\n</details>\n"
    url = workspace.open_pr(ws, order.title, _pr_body(order, cfg, summary, extra), cfg.pr_labels, draft=bool(red))
    return Outcome(order.to_dict(), "draft" if red else "pr", pr_url=url, red=red, unverified=unverified,
                   agent=result.to_dict() if result else None)


# ── triage ───────────────────────────────────────────────────────────

def _triage_summary(result: AgentResult) -> str:
    summary = f"**{result.category or 'fix'}** · {result.summary}"
    if result.verified:
        summary += "\n\nVerified: " + ", ".join(f"`{v}`" for v in result.verified)
    return summary


def _recheck(order: Order, ws: workspace.Workspace, cli: Path) -> tuple[list[dict], list[dict]]:
    """The steward's own gate on a finding: the named check, re-run when a
    clone can run it, else reported as unverified. Returns (red, unverified)."""
    check = order.finding.check if order.finding else None
    if not check:
        what = (order.finding.workflow if order.finding else None) or "workflow"
        return [], [{"check": what, "why": "a workflow failure names no single check the steward can re-run"}]
    checks = refresh.static_checks(ws, cli)
    if check in checks.runnable:
        return refresh.verify(ws, cli, [check]), []
    known = [u for u in checks.unverified if u["check"] == check]
    return [], known or [{"check": check, "why": "the steward cannot run it in a clone: it needs a deployed surface, "
                                                 "or the repo has switched it off"}]


def _verifier_version(order: Order, inv: inventory.Inventory) -> str | None:
    """The slopstopper-cli to put on the agent's PATH and re-check with: the
    pin when it is a release the steward can work with, else PyPI's latest,
    else nothing (a pin like `latest` or one below the floor)."""
    for candidate in (order.pinned, inv.latest):
        if versions.parse(candidate) and not versions.behind(candidate, MIN_INSTALL_VERSION):
            return candidate
    return None


def work_triage(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, ctx: RunContext) -> Outcome:
    assert order.finding
    version = _verifier_version(order, inv)
    if not version:
        return Outcome(order.to_dict(), "skipped", detail=f"no slopstopper-cli at or above {MIN_INSTALL_VERSION} to work with "
                       f"(pin {order.pinned or 'none'}, PyPI latest {inv.latest or 'unknown'})")
    issue = gh.api(f"repos/{order.repo}/issues/{order.finding.number}") or {}
    cli = refresh.cli_for(version, ctx.venv(version), cfg.package)
    agent.ensure_skills(ws, version, cfg.slopstopper_repo)
    result = agent.run(ws, prompts.triage(order, inv, issue.get("body") or "", cfg.slopstopper_repo), cfg, cli,
                       timeout=ctx.agent_timeout(), protect=ctx.protected(), preflight=ctx.agent_unavailable,
                       checked=ctx.checked_clis)
    if not result.ran:
        return Outcome(order.to_dict(), "skipped", detail=result.error, agent=result.to_dict())
    red, unverified = _recheck(order, ws, cli)
    if result.outcome == "skipped" or not workspace.commit(ws, f"{order.title}\n\n{result.summary or order.reason}"):
        return Outcome(order.to_dict(), "skipped", detail=result.summary or result.error or "agent changed nothing",
                       agent=result.to_dict())
    workspace.push(ws, order.branch)
    return _triage_pr(order, cfg, ws, result, red, unverified)


def _triage_pr(order: Order, cfg: Config, ws: workspace.Workspace, result: AgentResult,
               red: list[dict], unverified: list[dict]) -> Outcome:
    """Open the triage PR: a draft unless the agent fixed it and the check is
    not red; `Closes #N` only when the steward verified the check green."""
    assert order.finding
    extra = _red_section(f"`{order.finding.check}` is still red on this branch", red) + _upstream_section(result, cfg)
    extra += _unverified_section(unverified)
    if result.notes:
        extra += f"Notes: {result.notes}\n\n"
    draft = bool(red) or result.outcome != "fixed"
    body = _pr_body(order, cfg, _triage_summary(result), extra, closes=not red and not unverified)
    url = workspace.open_pr(ws, order.title, body, cfg.pr_labels, draft=draft)
    return Outcome(order.to_dict(), "draft" if draft else "pr", pr_url=url, red=red, unverified=unverified,
                   agent=result.to_dict())


# ── the run ──────────────────────────────────────────────────────────

def execute(plan: Plan, invs: list[inventory.Inventory], cfg: Config, workdir: Path | None,
            agent_check: str | None = None, agent_checked: bool = False) -> list[Outcome]:
    by_repo = {inv.repo.full_name: inv for inv in invs}
    own_base = workdir is None
    ctx = RunContext(base=workdir or Path(tempfile.mkdtemp(prefix="steward-")), deadline=_deadline(),
                     agent_check=agent_check, agent_checked=agent_checked)
    user = isolation.agent_user()
    if user and own_base:
        ctx.base.chmod(0o755)  # mkdtemp is 0700; the agent user reads the venvs and copies its clone from here
    elif user:
        ctx.reach_pending = True
    outcomes = []
    try:
        for order in plan.orders:
            left = ctx.remaining()
            if left is not None and left < MIN_ORDER_SECONDS:
                outcomes.append(Outcome(order.to_dict(), "deferred", detail=(
                    f"the run's GitHub token expires in {max(0, int(left // 60))} min, too soon to finish this order; "
                    "the next run picks it up")))
                continue
            outcomes.append(_work(order, by_repo[order.repo], cfg, ctx))
    finally:
        if own_base:
            shutil.rmtree(ctx.base, ignore_errors=True)
    return outcomes


def _work(order: Order, inv: inventory.Inventory, cfg: Config, ctx: RunContext) -> Outcome:
    if order.agentic and ctx.agent_unavailable():
        # Skip before paying for a clone and a venv the agent could not use.
        return Outcome(order.to_dict(), "skipped", detail=str(ctx.agent_unavailable()))
    try:
        ws = workspace.clone(order.repo, order.default_branch, ctx.base / "clones" / order.repo.replace("/", "__") / order.id)
        workspace.branch(ws, order.branch)
        work = work_refresh if order.kind in ("upgrade", "refresh") else work_triage
        return work(order, inv, cfg, ws, ctx)
    except Exception as exc:  # one order's failure must not stop the run
        return Outcome(order.to_dict(), "failed", detail=_failure(exc))


# A credential that could appear in a tool's error output: GitHub tokens,
# and userinfo in a URL (`https://x-access-token:...@github.com`).
_SECRET = re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|(?<=://)[^/@\s]+(?=@)")


def _failure(exc: Exception) -> str:
    """A failed order's detail. A failed command says what it ran and what
    it printed on stderr (git's own reason for refusing a push, say), with
    anything token-shaped redacted: the detail goes into the run report."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        err = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace")
        detail += " " + err.strip()[-400:]
    return _SECRET.sub("***", detail)[:900]


def run(cfg: Config, only: str | None, dry_run: bool, workdir: Path | None) -> RunReport:
    invs, targets, _ = gather(cfg, only)
    plan = plan_all(invs, targets, cfg)
    mode = "plan" if dry_run or cfg.mode == "plan" else "pr"
    report = RunReport(mode, repos=[inv.to_dict() for inv in invs], deferred=[o.to_dict() for o in plan.deferred],
                       skipped=plan.skipped)
    if mode == "plan":
        report.outcomes = [Outcome(o.to_dict(), "planned", detail=o.reason) for o in plan.orders]
        return report
    # Probe only when an order needs the agent; the answer then serves the run.
    agentic = any(o.agentic for o in plan.orders)
    why_not = agent.available() if agentic else None
    if why_not:
        report.skipped.append({"repo": "*", "order": "agentic orders", "why": why_not})
        plan.orders = [o for o in plan.orders if not o.agentic]
    report.outcomes = execute(plan, invs, cfg, workdir, agent_check=why_not, agent_checked=agentic)
    return report


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
