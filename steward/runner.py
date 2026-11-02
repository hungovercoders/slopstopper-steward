"""The run: discover, inventory, plan, then work each order in a clone."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from steward import ci, discover, gh, inventory, isolation, prompts, versions, workspace
from steward.redact import redact, unlink
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
CI_MARGIN_SECONDS = 8 * 60    # kept back from a wait for the PR's checks: a push, the body, the report


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
        f"{link}{unlink(summary)}\n\n{unlink(extra)}"
        f"{FOOTER}Opened by [slopstopper-steward]({STEWARD_URL}) for `{order.repo}`. Order `{order.id}`; "
        f"a label `{cfg.ignore_label}` on an issue keeps the steward off it.\n"
        f"<!-- steward:order={order.id} -->\n"
    )


FOOTER = "\n---\n"


def _with_section(body: str, section: str) -> str:
    """`section` above the footer, so the order marker stays last."""
    head, sep, tail = body.rpartition(FOOTER)
    return f"{head}{section}{sep}{tail}" if sep else body + section


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
    ran = ", ".join(x for x in (result.model, f"${result.cost_usd:.2f}" if result.cost_usd is not None else "",
                               f"{result.turns} turns" if result.turns is not None else "") if x)
    text = f"\n**Agent ({result.outcome}, {result.category or 'n/a'}{'; ' + ran if ran else ''}):** {result.summary}"
    if result.notes:
        text += f"\n\nNotes: {result.notes}"
    if result.denied:
        text += "\n\nRefused by the agent's box: " + "; ".join(f"`{d}`" for d in result.denied)
    return text + _self_review_text(result.review)


def _ran(row: dict) -> str:
    return ", ".join(x for x in (row.get("model") or "", f"${row['cost_usd']:.2f}" if row.get("cost_usd") is not None else "",
                                f"{row['turns']} turns" if row.get("turns") is not None else "") if x)


def _self_review_text(review: dict | None) -> str:
    if not review:
        return ""
    ran, none = _ran(review), review["outcome"] == "no verdict"
    text = f"\n\n**Self-review ({review['outcome']}{'; ' + ran if ran else ''}):** {review['note'] if none else review['summary']}"
    if review["findings"]:
        text += "\n" + "\n".join(f"- {f}" for f in review["findings"])
    fix = review.get("correction")
    if fix:
        ran = _ran(fix)
        text += (f"\n\nCorrection round ({fix['outcome']}{'; ' + ran if ran else ''}): {fix['summary'] or fix['error']}"
                 + (f" Notes: {fix['notes']}" if fix["notes"] else ""))
    if review["note"] and not none:
        text += f"\n\n{review['note']}"
    return text


def _review_blocks(result: AgentResult | None) -> str:
    """Why the agent's self-review keeps the PR a draft, or "": a review that
    asked for changes and did not get them. No verdict never blocks."""
    if result and result.review and not result.review.get("settled", True):
        return f"its self-review asked for changes that were not made ({result.review['note']})"
    return ""


def _blocked_section(blocked: str) -> str:
    return (f"### Self-review not settled\n\nOpened as a draft: {blocked}. The findings are above; "
            "address them on this branch before merging.\n\n") if blocked else ""


VERDICTS = ("approved", "changes-requested")


def _agent_pass(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
                ctx: RunContext, brief: str, checks: list[str], apart: list[str]) -> AgentResult:
    """One agent run, then its self-review when it changed the tree: a
    read-only second run over the pending diff, and one correction round
    when that asks for changes. The result is the first run's, with the
    review on it; a correction left `partial` makes the result partial."""
    before = refresh.fingerprint(ws)
    result = agent.run(ws, brief, cfg, cli, timeout=ctx.agent_timeout(), protect=ctx.protected(),
                       preflight=ctx.agent_unavailable, checked=ctx.checked_clis, checks=checks)
    if not cfg.self_review or not result.ran or result.error or refresh.fingerprint(ws) == before:
        return result
    result.review = _self_review(order, inv, cfg, ws, cli, ctx, result, checks)
    if result.review["outcome"] == "changes-requested":
        _correct(order, inv, cfg, ws, cli, ctx, result, checks, apart)
    return result


def _self_review(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
                 ctx: RunContext, result: AgentResult, checks: list[str]) -> dict:
    """The reviewer's verdict on `.steward-change.md`. Anything but a clear
    verdict reads as none, which never blocks; a reviewer that changed the
    tree (it has no editing tool, but Bash can redirect) is not trusted."""
    (ws.path / ".steward-change.md").write_text(refresh.change_diff(ws), encoding="utf-8")
    before = refresh.fingerprint(ws)
    verdict = agent.run(ws, prompts.self_review(order, inv, result.summary, checks), cfg, cli,
                        timeout=ctx.agent_timeout(), protect=ctx.protected(), preflight=ctx.agent_unavailable,
                        checked=ctx.checked_clis, checks=checks, edits=False)
    review = {"outcome": verdict.outcome, "summary": verdict.summary, "findings": verdict.findings, "corrected": False,
              "settled": True, "note": "", "cost_usd": verdict.cost_usd, "turns": verdict.turns, "model": verdict.model}
    if not verdict.ran or verdict.error:
        review.update(outcome="no verdict", note=f"the reviewer did not finish: {verdict.error}")
    elif refresh.fingerprint(ws) != before:
        review.update(outcome="no verdict", note="the reviewer changed the tree, which it must not, so its verdict is not used")
    elif verdict.outcome not in VERDICTS:
        review.update(outcome="no verdict", note=f"the reviewer gave no verdict ({verdict.outcome or 'empty'})")
    elif verdict.outcome == "changes-requested" and not verdict.findings:
        review["outcome"] = "approved"   # the brief says so: no finding is an approval
    return review


def _correct(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
             ctx: RunContext, result: AgentResult, checks: list[str], apart: list[str]) -> None:
    """The one correction round. `fixed` settles the review, whether the
    agent changed the tree or answered a finding in its notes; anything
    else leaves it open, and the PR a draft."""
    review = result.review
    (ws.path / ".steward-review.md").write_text("# Self-review findings\n\n" + "\n".join(f"- {f}" for f in review["findings"])
                                                + "\n", encoding="utf-8")
    before = refresh.fingerprint(ws)
    fix = agent.run(ws, prompts.correct(order, inv, review["findings"], cfg.slopstopper_repo, checks, apart), cfg, cli,
                    timeout=ctx.agent_timeout(), protect=ctx.protected(), preflight=ctx.agent_unavailable,
                    checked=ctx.checked_clis, checks=checks)
    review["correction"] = {"outcome": fix.outcome, "summary": fix.summary, "notes": fix.notes, "error": fix.error,
                            "cost_usd": fix.cost_usd, "turns": fix.turns, "model": fix.model}
    review["corrected"] = fix.ran and refresh.fingerprint(ws) != before
    review["settled"] = fix.ran and not fix.error and fix.outcome == "fixed"
    result.denied += fix.denied
    if fix.ran and fix.outcome == "partial":
        result.outcome, result.notes = "partial", " ".join(n for n in (result.notes, fix.notes) if n)
    if not review["settled"]:
        review["note"] = f"the correction round did not settle it: {fix.error or fix.notes or fix.summary or fix.outcome}"


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


def _review(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
            red: list[dict], unverified: list[dict], ctx: RunContext
            ) -> tuple[list[dict], list[dict], AgentResult | None, str]:
    """The review: the agent works the install skill's refresh against this
    branch, red checks included, then the steward verifies again. Returns
    (red, unverified, agent result, why it was not reviewed or "")."""
    if not cfg.agent_enabled:
        return red, unverified, None, "the agent is disabled in steward.yml"
    why_not = ctx.agent_unavailable()
    if why_not:  # before any fetch: an agent that cannot run needs no inputs
        return red, unverified, None, f"the agent could not run: {why_not}"
    try:
        agent.ensure_skills(ws, order.target, cfg.slopstopper_repo)
    except RuntimeError as exc:
        return red, unverified, None, f"the skills could not be fetched: {exc}"
    marks = [{**w, "state": refresh.issue_state(w["marker"])} for w in refresh.workarounds(ws)]
    (ws.path / ".steward-suite-diff.md").write_text(refresh.suite_diff(ws), encoding="utf-8")
    start = _start(order, inv)
    (ws.path / ".steward-schema-diff.md").write_text(refresh.schema_diff(cfg.slopstopper_repo, start, order.target),
                                                     encoding="utf-8")
    checks, apart = _runnable(ws, cli)
    brief = prompts.review(order, inv, order.details["review"], marks, red, cfg.slopstopper_repo,
                           start=start, checks=checks, apart=apart)
    before = refresh.fingerprint(ws)
    result = _agent_pass(order, inv, cfg, ws, cli, ctx, brief, checks, apart)
    if result.ran and refresh.fingerprint(ws) != before:
        red, unverified = _verify_pass(ws, cli, ctx)
    return red, unverified, result, _unreviewed(result)


def _unreviewed(result: AgentResult) -> str:
    """Why a review the agent was given did not happen, or "" when it did.
    Running is not reviewing: a timeout, a refused copy-back or a missing
    result file all leave `ran` true."""
    if not result.ran:
        return f"the agent could not run: {result.error}"
    if result.error:
        return f"the agent did not finish: {result.error}"
    if result.outcome == "partial":
        return f"the agent left part of it for a person: {result.notes or result.summary}"
    if result.outcome not in ("fixed", "upstream"):
        return f"the agent did not review it: {result.summary or 'no reason given'}"
    return ""


def _start(order: Order, inv: inventory.Inventory) -> str | None:
    """The version the suite moves from: the old pin for an upgrade, the
    version that wrote the suite for a refresh."""
    return order.pinned if order.kind == "upgrade" else inv.repo.installed_from


def _settle(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
            ctx: RunContext) -> tuple[list[dict], list[dict], AgentResult | None, str]:
    """Verify the refreshed suite. With `upgrade.review` the agent reviews
    the refresh either way; without it the agent only sees what went red."""
    red, unverified = _verify_pass(ws, cli, ctx)
    if order.details.get("review"):
        return _review(order, inv, cfg, ws, cli, red, unverified, ctx)
    return (*_settle_red(order, inv, cfg, ws, cli, red, unverified, ctx), "")


def _review_section(order: Order, unreviewed: str) -> str:
    why = order.details.get("review")
    if not why:
        return ""
    if unreviewed:
        return (f"### Not reviewed\n\nThe agent was to review the refresh ({why}), but {unreviewed}. "
                "Opened as a draft: work the `slopstopper-install` skill's refresh section before merging.\n\n")
    return f"### Reviewed\n\nThe agent worked the `slopstopper-install` skill's refresh ({why}).\n\n"


def _runnable(ws: workspace.Workspace, cli: Path) -> tuple[list[str], list[str]]:
    """(the checks this clone can run, which is also what the agent may run;
    the ones the steward runs apart as the agent user, which the brief
    names so the agent knows they will be proved for it)."""
    checks = refresh.static_checks(ws, cli)
    return checks.runnable, checks.environment if isolation.agent_user() else []


def _apart(ws: workspace.Workspace, cli: Path, checks: list[str], ctx: RunContext | None
           ) -> tuple[list[dict], list[dict]]:
    """Run the environment-needing checks apart as the agent user, or say
    why not. Returns (red, unverified)."""
    if not checks:
        return [], []
    user = isolation.agent_user()
    if not user:
        return [], [{"check": c, "why": refresh.ENVIRONMENT_WHY} for c in checks]
    if ctx is None or cli not in ctx.checked_clis:
        try:
            isolation.check_cli(user, cli)
        except isolation.SetupError as exc:
            return [], [{"check": c, "why": f"{refresh.ENVIRONMENT_WHY}; {exc}"} for c in checks]
        if ctx is not None:
            ctx.checked_clis.add(cli)
    return refresh.verify_apart(ws, cli, checks, user), []


def _verify_pass(ws: workspace.Workspace, cli: Path, ctx: RunContext | None = None) -> tuple[list[dict], list[dict]]:
    """One verification pass: list once, run what is runnable here, run the
    environment checks apart. Returns (red, unverified)."""
    checks = refresh.static_checks(ws, cli)
    red = refresh.verify(ws, cli, checks.runnable)
    apart_red, apart_unverified = _apart(ws, cli, checks.environment, ctx)
    deployed = [{"check": c, "why": refresh.DEPLOYED_WHY} for c in checks.deployed]
    return red + apart_red, checks.unverified + apart_unverified + deployed


def _settle_red(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
                red: list[dict], unverified: list[dict], ctx: RunContext) -> tuple[list[dict], list[dict], AgentResult | None]:
    """Hand a red verification to the agent, then verify again. The second
    pass lists the checks again on purpose: the verify brief allows the
    agent to switch a check off through `workflows.disabled`, which changes
    what the repo keeps."""
    if not red or not cfg.agent_enabled:
        return red, unverified, None
    agent.ensure_skills(ws, order.target, cfg.slopstopper_repo)
    checks, apart = _runnable(ws, cli)
    brief = prompts.verify(order, inv, red, cfg.slopstopper_repo, checks=checks, apart=apart)
    result = _agent_pass(order, inv, cfg, ws, cli, ctx, brief, checks, apart)
    if not result.ran:
        return red, unverified, result
    red, unverified = _verify_pass(ws, cli, ctx)
    return red, unverified, result


def _unverified_section(unverified: list[dict]) -> str:
    if not unverified:
        return ""
    rows = "\n".join(f"- `{u['check']}`: {u['why']}" for u in unverified)
    return f"### Not verified by the steward\n\n{rows}\n\nThis PR's own `ss-*` workflows run them.\n\n"


def _sync_locks(ws: workspace.Workspace) -> list[dict]:
    try:
        return refresh.sync_lockfiles(ws, isolation.agent_user())
    except Exception as exc:  # the PR still opens; the body says the lock was not kept in step
        return [{"dir": ".", "status": "failed", "detail": _failure(exc)}]


def _merge_locks(first: list[dict], second: list[dict]) -> list[dict]:
    """One row per directory: a later `synced` or `failed` replaces an earlier `unchanged`."""
    rows = {r["dir"]: r for r in first}
    for r in second:
        if r["status"] != "unchanged" or r["dir"] not in rows:
            rows[r["dir"]] = r
    return list(rows.values())


def _locks_section(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = "\n".join(f"- `{r['dir']}/package-lock.json`: {r['status']}" + (f" ({r['detail']})" if r["detail"] else "")
                       for r in rows)
    return ("### Lockfiles\n\nThe install changed `package.json`, so the lock was regenerated "
            f"(`npm install --package-lock-only --ignore-scripts`):\n\n{lines}\n\n")


def _ci_budget(cfg: Config, ctx: RunContext) -> float:
    cap = cfg.ci_wait_minutes * 60
    left = ctx.remaining()
    return cap if left is None else min(cap, left - CI_MARGIN_SECONDS)


def _settle_ci(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path | None,
               ctx: RunContext, url: str, body: str, draft: bool) -> tuple[str, dict]:
    """Wait for the PR's checks, the last verifier. Red slopstopper checks go
    to the agent with their logs, up to `ci.fix_rounds` times, each wait
    bounded by `ci.wait_minutes` and the run's deadline. The body gets a
    section saying how it ended; a PR that is not green becomes a draft.
    Returns (status, what happened)."""
    info = _ci_info()
    if not cfg.ci_wait:
        info["note"] = "ci.wait is off"
    else:
        try:
            _ci_loop(order, inv, cfg, ws, cli, ctx, info)
        except Exception as exc:  # the PR exists: say what stopped the wait, never lose it
            info["note"] = f"the wait stopped: {_failure(exc)}"
    not_green = bool(info["failing"] or info["timed_out"])
    number = workspace.pr_number(url)
    status = "draft" if draft or not_green else "pr"
    if status == "pr":
        status = _merge(order, cfg, number, info)
    try:
        workspace.update_pr_body(order.repo, number, _with_section(body, _ci_section(info) + _merge_section(info)))
        if not_green and not draft:
            workspace.set_draft(order.repo, number)
    except Exception as exc:
        info["note"] = (info["note"] + " " if info["note"] else "") + f"the PR could not be updated: {_failure(exc)}"
    return status, info


def _merge(order: Order, cfg: Config, number: int, info: dict) -> str:
    """Merge a ready PR when the config covers its kind (`merge.auto` and
    `merge.kinds` for the steward's own, `dependabot.merge` for
    Dependabot's) and the steward saw its checks green itself: a wait that
    was skipped, stopped or saw no check proves nothing, and the PR is
    left for a person. Returns `merged` or `pr`."""
    covered = cfg.dependabot_merge if order.kind == "dependabot" else cfg.merge_auto and order.kind in cfg.merge_kinds
    if not covered:
        return "pr"
    if not info["waited"] or not info.get("checks") or info["note"]:
        info["merge_note"] = info["note"] or "the steward saw no check on the PR, so it has nothing green to merge on"
        return "pr"
    try:
        workspace.merge_pr(order.repo, number, cfg.merge_method, cfg.merge_delete_branch)
    except Exception as exc:  # a protected base, a review rule: the PR stays ready, the body says why
        info["merge_note"] = _failure(exc)
        return "pr"
    info["merged"], info["merge_method"] = True, cfg.merge_method
    return "merged"


def _merge_section(info: dict) -> str:
    if info.get("merged"):
        return (f"### Merged\n\nThe steward merged this PR ({info['merge_method']}) once its checks were green: "
                "`merge.auto` is on in its steward.yml.\n\n")
    if info.get("merge_note"):
        return f"### Not merged\n\n`merge.auto` is on, but {info['merge_note']}. Merge it by hand once read.\n\n"
    return ""


def _ci_info() -> dict:
    return {"waited": False, "rounds": 0, "failing": [], "timed_out": False, "note": "", "agent": [], "locks": []}


def _ci_loop(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path | None,
             ctx: RunContext, info: dict) -> None:
    """Wait, and while a check the agent may work is red and a round is
    left, fix and wait again. On the steward's own PRs that is a
    slopstopper check; on Dependabot's, every red check is the bump's."""
    sha = workspace.head_sha(ws)
    expected, workflows = ci.expected_workflows(ws.path), {}
    for round_ in range(cfg.ci_fix_rounds + 1):
        budget = _ci_budget(cfg, ctx)
        if budget < 60:
            info["note"] = "the run was out of time before the PR's checks finished"
            return
        runs, timed_out, workflows = ci.wait(order.repo, sha, budget, expected, workflows)
        if not runs:  # nothing ran, or nothing registered in time: not red, not green
            info.update(waited=True, checks=0, timed_out=False,
                        note=f"no check ran on the PR within {round(budget / 60)} minutes" if timed_out else "")
            return
        info.update(waited=True, timed_out=timed_out, waited_minutes=round(budget / 60), checks=len(runs))
        red = ci.describe(order.repo, runs, workflows)
        info["failing"] = [{k: r[k] for k in ("name", "conclusion", "url", "workflow", "ours")} for r in red]
        ours = red if order.kind == "dependabot" else [r for r in red if r["ours"]]
        if _ci_stops(cfg, cli, red, ours, timed_out, round_):
            return
        if not _ci_fix_round(order, inv, cfg, ws, cli, ctx, ours, info, round_ + 1):
            return
        sha = workspace.head_sha(ws)


def _ci_stops(cfg: Config, cli: Path | None, red: list, ours: list, timed_out: bool, round_: int) -> bool:
    """Nothing left to do: green, out of time, out of rounds, nothing the
    triage skill covers, or no agent to work it."""
    return (not red or timed_out or round_ == cfg.ci_fix_rounds or not ours
            or not cfg.agent_enabled or cli is None)


def _ci_fix_round(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, cli: Path,
                  ctx: RunContext, ours: list[dict], info: dict, number: int) -> bool:
    """One agent attempt at the red checks, from their logs. The lock is
    kept in step and the steward's own verification re-run before the
    push: a fix that goes red here is not pushed, and the PR is drafted.
    Returns whether a wait should follow."""
    bump = order.kind == "dependabot"
    if not bump and not _skills_ready(order, inv, cfg, ws, info, number):
        return False
    tails = {r["name"]: ci.log_tail(order.repo, r["id"]) for r in ours[:8]}
    (ws.path / ".steward-ci-failures.md").write_text(ci.failures_markdown(ours, tails), encoding="utf-8")
    checks, apart = _runnable(ws, cli)
    brief = _fix_brief(order, inv, cfg, ours, checks, apart)
    # On Dependabot's branch the steward's gate is "nothing newly red": what
    # was red before the bump is the repo's, not the fix's.
    baseline = {r["check"] for r in _verify_pass(ws, cli, ctx)[0]} if bump else set()
    result = _agent_pass(order, inv, cfg, ws, cli, ctx, brief, checks, apart)
    info["agent"].append(result.to_dict())
    stop = _fix_stops(ws, result, number)
    if stop:
        info["note"] = stop
        return False
    info["locks"] += _sync_locks(ws)
    red = [r for r in _verify_after_fix(order, ws, cli, ctx) if r["check"] not in baseline]
    if red:
        info["note"] = (f"the agent's round {number} left the steward's own checks red "
                        f"({', '.join(r['check'] for r in red)}), so it was not pushed")
        return False
    _push_fix(order, ws, result, number)
    info["rounds"] = number
    return True


def _skills_ready(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, info: dict,
                  number: int) -> bool:
    try:
        agent.ensure_skills(ws, order.target or inv.repo.pinned, cfg.slopstopper_repo)
        return True
    except RuntimeError as exc:
        info["note"] = f"the skills could not be fetched for round {number}: {exc}"
        return False


def _fix_brief(order: Order, inv: inventory.Inventory, cfg: Config, ours: list[dict], checks: list[str],
               apart: list[str]) -> str:
    if order.kind == "dependabot":
        body = (gh.api(f"repos/{order.repo}/pulls/{order.details['number']}") or {}).get("body") or ""
        return prompts.dependabot_fix(order, inv, ours, body, cfg.slopstopper_repo, checks=checks, apart=apart)
    return prompts.ci_fix(order, inv, ours, cfg.slopstopper_repo, checks=checks, apart=apart)


def _fix_stops(ws: workspace.Workspace, result: AgentResult, number: int) -> str:
    """Why this round ends without a push, or ""."""
    if not result.ran or not ws.dirty():
        return f"the agent changed nothing in round {number}: {result.summary or result.error}"
    blocked = _review_blocks(result)
    return f"the agent's round {number} was not pushed: {blocked}" if blocked else ""


def _push_fix(order: Order, ws: workspace.Workspace, result: AgentResult, number: int) -> None:
    """Commit and push the round: onto the steward's branch with its lease,
    or fast-forward onto Dependabot's."""
    bump = order.kind == "dependabot"
    what = "fix(deps): adapt to the dependency bump" if bump else "fix(slopstopper): settle the pull request's checks"
    workspace.commit(ws, f"{what} (round {number})\n\n{result.summary or result.error}")
    (workspace.push_onto if bump else workspace.push)(ws, order.branch)


def _verify_after_fix(order: Order, ws: workspace.Workspace, cli: Path, ctx: RunContext) -> list[dict]:
    """The steward's own gate again after the agent's fix: the finding's check
    for a triage order, every static check for an upgrade or refresh."""
    if order.kind == "triage":
        return _recheck(order, ws, cli, ctx)[0]
    return _verify_pass(ws, cli, ctx)[0]


def _ci_section(info: dict) -> str:
    if not info["waited"]:
        return f"### Checks\n\nNot awaited: {info['note']}.\n\n"
    if not info.get("checks"):
        return f"### Checks\n\n{info['note'] or 'No check ran on this PR'}; nothing to wait for.\n\n"
    rounds = info["rounds"]
    after = f" after {rounds} fix round(s)" if rounds else ""
    if info["failing"]:
        rows = "\n".join(f"- `{f['name']}` ({f['conclusion']}): {f['url']}" + ("" if f["ours"] else " (not a slopstopper check)")
                         for f in info["failing"])
        tail = f" {info['note']}." if info["note"] else ""
        return (f"### CI still red{after}\n\n{rows}\n\nOpened as a draft.{tail} Work the `slopstopper-triage` skill "
                "on this branch; the steward's next run does not re-open a settled order.\n\n")
    if info["timed_out"]:
        return (f"### Checks\n\nStill running after {info.get('waited_minutes')} minutes{after}; the steward could not "
                "wait longer. Opened as a draft: mark it ready once they are green.\n\n")
    return f"### Checks\n\nThe PR's {info.get('checks', 0)} checks are green{after}.\n\n"


def work_refresh(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, ctx: RunContext) -> Outcome:
    assert order.target
    moved = refresh.set_pin(ws.path / "mise.toml", order.target)
    cli = refresh.cli_for(order.target, ctx.venv(order.target), cfg.package)
    installed = refresh.install_suite(ws, cli)
    if installed.returncode != 0:
        return Outcome(order.to_dict(), "failed",
                       detail=f"slopstopper install exited {installed.returncode}: {installed.stderr.strip()[-400:]}")
    start = _start(order, inv)
    known = order.kind == "upgrade" or versions.parse(start) is not None
    notes = release_notes(cfg, start, order.target, ctx.releases) if known else ""
    (ws.path / ".steward-release-notes.md").write_text(notes, encoding="utf-8")
    locks = _sync_locks(ws)   # the install's change to package.json, before anything reads the tree
    red, unverified, result, unreviewed = _settle(order, inv, cfg, ws, cli, ctx)
    locks = _merge_locks(locks, _sync_locks(ws))   # and the agent's, if it changed package.json too
    if not workspace.commit(ws, f"{order.title}\n\n{order.reason}."):
        return Outcome(order.to_dict(), "skipped", detail="nothing changed after the refresh")
    workspace.push(ws, order.branch)
    summary = _refresh_summary(order, inv, moved) + _agent_section(result)
    extra = _review_section(order, unreviewed)
    extra += _red_section("Still red after the refresh", red, "Opened as a draft: settle these before merging.\n\n")
    blocked = _review_blocks(result)
    extra += _blocked_section(blocked) + _locks_section(locks) + _unverified_section(unverified)
    if notes:
        extra += f"<details><summary>Release notes between the two pins</summary>\n\n{notes}\n</details>\n"
    draft = bool(red or unreviewed or blocked or any(r["status"] == "failed" for r in locks))
    body = _pr_body(order, cfg, summary, extra)
    url = workspace.open_pr(ws, order.title, body, cfg.pr_labels, draft=draft)
    status, ci_info = _settle_ci(order, inv, cfg, ws, cli, ctx, url, body, draft)
    return Outcome(order.to_dict(), status, pr_url=url, red=red, unverified=unverified,
                   agent=result.to_dict() if result else None, ci=ci_info)


# ── triage ───────────────────────────────────────────────────────────

def _triage_summary(result: AgentResult) -> str:
    summary = f"**{result.category or 'fix'}** · {result.summary}"
    if result.verified:
        summary += "\n\nVerified: " + ", ".join(f"`{v}`" for v in result.verified)
    return summary


def _recheck(order: Order, ws: workspace.Workspace, cli: Path, ctx: RunContext | None = None
             ) -> tuple[list[dict], list[dict]]:
    """The steward's own gate on a finding: the named check, re-run when a
    clone can run it (here, or apart as the agent user), else reported as
    unverified. Returns (red, unverified)."""
    check = order.finding.check if order.finding else None
    if not check:
        what = (order.finding.workflow if order.finding else None) or "workflow"
        return [], [{"check": what, "why": "a workflow failure names no single check the steward can re-run"}]
    checks = refresh.static_checks(ws, cli)
    if check in checks.runnable:
        return refresh.verify(ws, cli, [check]), []
    if check in checks.environment:
        return _apart(ws, cli, [check], ctx)
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
    checks, apart = _runnable(ws, cli)
    brief = prompts.triage(order, inv, issue.get("body") or "", cfg.slopstopper_repo, checks=checks, apart=apart)
    result = _agent_pass(order, inv, cfg, ws, cli, ctx, brief, checks, apart)
    if not result.ran:
        return Outcome(order.to_dict(), "skipped", detail=result.error, agent=result.to_dict())
    red, unverified = _recheck(order, ws, cli, ctx)
    if result.outcome == "skipped" or not workspace.commit(ws, f"{order.title}\n\n{result.summary or order.reason}"):
        return Outcome(order.to_dict(), "skipped", detail=result.summary or result.error or "agent changed nothing",
                       agent=result.to_dict())
    workspace.push(ws, order.branch)
    return _triage_pr(order, cfg, ws, result, red, unverified, inv=inv, cli=cli, ctx=ctx)


def _triage_pr(order: Order, cfg: Config, ws: workspace.Workspace, result: AgentResult,
               red: list[dict], unverified: list[dict], inv: inventory.Inventory | None = None,
               cli: Path | None = None, ctx: RunContext | None = None) -> Outcome:
    """Open the triage PR: a draft unless the agent fixed it and the check is
    not red; `Closes #N` only when the steward verified the check green."""
    assert order.finding
    extra = _red_section(f"`{order.finding.check}` is still red on this branch", red) + _upstream_section(result, cfg)
    blocked = _review_blocks(result)
    extra += _blocked_section(blocked) + _unverified_section(unverified)
    if result.notes:
        extra += f"Notes: {result.notes}\n\n"
    draft = bool(red) or result.outcome != "fixed" or bool(blocked)
    body = _pr_body(order, cfg, _triage_summary(result), extra, closes=not red and not unverified)
    url = workspace.open_pr(ws, order.title, body, cfg.pr_labels, draft=draft)
    status, ci_info = ("draft" if draft else "pr"), None
    if inv is not None and ctx is not None:
        status, ci_info = _settle_ci(order, inv, cfg, ws, cli, ctx, url, body, draft)
    return Outcome(order.to_dict(), status, pr_url=url, red=red, unverified=unverified, agent=result.to_dict(),
                   ci=ci_info)


# ── dependabot ───────────────────────────────────────────────────────

def work_dependabot(order: Order, inv: inventory.Inventory, cfg: Config, ws: workspace.Workspace, ctx: RunContext) -> Outcome:
    """Wait for Dependabot's PR's checks on its own branch and, red, work
    them the way a steward PR's are settled, pushing onto that branch. The
    PR's body is Dependabot's and stays so: the steward says what it did in
    one comment, whose marker keeps the next run off this same head."""
    number = order.details["number"]
    version = _verifier_version(order, inv)
    cli = refresh.cli_for(version, ctx.venv(version), cfg.package) if version else None
    info = _ci_info()
    if not cfg.ci_wait:
        info["note"] = "ci.wait is off, and a Dependabot PR is only its checks"
    else:
        try:
            _ci_loop(order, inv, cfg, ws, cli, ctx, info)
        except Exception as exc:
            info["note"] = f"the wait stopped: {_failure(exc)}"
    status = _dependabot_status(info)
    if status == "green" and _merge(order, cfg, number, info) == "merged":
        status = "merged"
    if status == "red" or info["agent"] or info.get("merge_note"):
        try:
            workspace.comment_pr(order.repo, number, _dependabot_comment(order, cfg, info, workspace.head_sha(ws)))
        except Exception as exc:
            info["note"] = (info["note"] + " " if info["note"] else "") + f"the PR could not be commented on: {_failure(exc)}"
    last = info["agent"][-1] if info["agent"] else None
    return Outcome(order.to_dict(), status, pr_url=order.details["url"], detail=info["note"], agent=last, ci=info)


def _dependabot_status(info: dict) -> str:
    """`green`, `red`, or `skipped` when the checks never settled (none
    ran, still running, not awaited): nothing to act on either way."""
    if info["failing"]:
        return "red"
    if info["waited"] and info.get("checks") and not info["timed_out"]:
        return "green"
    return "skipped"


def _dependabot_comment(order: Order, cfg: Config, info: dict, sha: str) -> str:
    rounds = info["rounds"]
    if info["failing"]:
        rows = "\n".join(f"- `{f['name']}` ({f['conclusion']}): {f['url']}" for f in info["failing"])
        head = (f"The checks on this pull request are still red after {rounds} fix round(s):\n\n{rows}\n\n"
                "Left for a person." if rounds else f"The checks on this pull request went red:\n\n{rows}\n\n")
        if info["note"]:
            head += f"{info['note']}.\n\n"
    elif info.get("merge_note"):
        head = (f"The checks are green{f' after {rounds} fix round(s)' if rounds else ''}, but the steward could not "
                f"merge it: {info['merge_note']}. Merge it by hand once read.\n\n")
    else:
        head = f"The checks are green after {rounds} fix round(s) pushed onto this branch.\n\n"
    agent = "".join(_agent_section(AgentResult(**a)) for a in info["agent"]).lstrip("\n")
    return (f"### slopstopper-steward\n\n{head}{agent}\n\n{FOOTER}Posted by [slopstopper-steward]({STEWARD_URL}); "
            f"order `{order.id}`. A label `{cfg.ignore_label}` on this pull request keeps the steward off it.\n"
            f"<!-- steward:worked={sha} -->\n")


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
        if order.kind == "dependabot":
            workspace.checkout_remote(ws, order.branch)
            return work_dependabot(order, inv, cfg, ws, ctx)
        workspace.branch(ws, order.branch)
        work = work_refresh if order.kind in ("upgrade", "refresh") else work_triage
        return work(order, inv, cfg, ws, ctx)
    except Exception as exc:  # one order's failure must not stop the run
        return Outcome(order.to_dict(), "failed", detail=_failure(exc))




def _failure(exc: Exception) -> str:
    """A failed order's detail. A failed command says what it ran and what
    it printed on stderr (git's own reason for refusing a push, say), with
    anything token-shaped redacted: the detail goes into the run report."""
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        err = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace")
        detail += " " + err.strip()[-400:]
    return redact(detail)[:900]


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
