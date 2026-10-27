"""What slopstopper already knows about one adopter, read without cloning.

The pin and the suite version come from discovery. The findings are the
issues slopstopper raised in the repo: every one carries the `slopstopper`
label and a `<!-- slopstopper:check=<name> -->` marker (`slopstopper emit
--target issue`), or the `workflow-failure` label and a `❌ Workflow
Failure: <name>` title (the failure tracker). Open `steward/*` pull
requests are read back so a run never opens the same work twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from steward import gh
from steward.config import Config
from steward.discover import Repo

BRAND_LABEL = "slopstopper"
FAILURE_LABEL = "workflow-failure"
FAILURE_TITLE = re.compile(r"^❌ Workflow Failure: (.+)$")
CHECK_MARKER = re.compile(r"<!--\s*slopstopper:check=([a-z0-9:\-]+)\s*-->")
WORKFLOW_MARKER = re.compile(r"<!--\s*slopstopper:workflow=([A-Za-z0-9._\-]+)\s*-->")
ORDER_MARKER = re.compile(r"<!--\s*steward:order=([A-Za-z0-9._:\-]+)\s*-->")
BRANCH_PREFIX = "steward/"
TRIAGE_SKILL = ".claude/skills/slopstopper-triage/SKILL.md"


@dataclass
class Finding:
    number: int
    title: str
    url: str
    labels: list[str]
    check: str | None = None
    workflow: str | None = None
    kind: str = "check"  # check | workflow-failure | other
    author: str = ""
    author_is_bot: bool = False

    @property
    def security(self) -> bool:
        return (self.check or "").startswith("security:") or "security" in self.labels

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "url": self.url, "labels": self.labels,
                "check": self.check, "workflow": self.workflow, "kind": self.kind, "security": self.security,
                "author": self.author}


@dataclass
class Inventory:
    repo: Repo
    latest: str | None
    profile: str | None = None
    findings: list[Finding] = field(default_factory=list)
    disabled_workflows: list[dict] = field(default_factory=list)
    open_orders: dict[str, dict] = field(default_factory=dict)  # order id -> {number, url}
    skills_present: bool = False

    def to_dict(self) -> dict:
        return {
            **self.repo.to_dict(), "latest": self.latest, "profile": self.profile,
            "findings": [f.to_dict() for f in self.findings],
            "disabled_workflows": self.disabled_workflows, "open_orders": self.open_orders,
            "skills_present": self.skills_present,
        }


def classify(issue: dict) -> Finding:
    labels = [l["name"] if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]
    body = issue.get("body") or ""
    title = issue.get("title") or ""
    user = issue.get("user") or {}
    finding = Finding(issue["number"], title, issue.get("html_url", ""), labels,
                      author=str(user.get("login") or ""), author_is_bot=str(user.get("type") or "") == "Bot")
    check = CHECK_MARKER.search(body)
    workflow = WORKFLOW_MARKER.search(body)
    if check:
        finding.check = check.group(1)
        finding.workflow = workflow.group(1) if workflow else None
    elif FAILURE_LABEL in labels or FAILURE_TITLE.match(title):
        finding.kind = "workflow-failure"
        match = FAILURE_TITLE.match(title)
        finding.workflow = match.group(1) if match else None
    else:
        finding.kind = "other"
    return finding


def _is_slopstoppers(finding: Finding) -> bool:
    """The label needs triage rights to apply, so it is trusted on its own.
    A bare body marker is anyone's to type, so it counts only when a bot
    (the Actions token `emit` and the failure tracker run under) opened the
    issue; otherwise the issue is a person's note and the agent never sees
    its body."""
    if BRAND_LABEL in finding.labels:
        return True
    return finding.kind in ("check", "workflow-failure") and finding.author_is_bot


def read_profile(slopstopper_yml: str | None) -> str | None:
    if not slopstopper_yml:
        return None
    match = re.search(r"^profile:\s*([A-Za-z]+)", slopstopper_yml, re.M)
    return match.group(1) if match else None


def _findings(repo: Repo, cfg: Config) -> list[Finding]:
    """Every open issue, not only the labelled ones: `emit` falls back to an
    unlabelled issue when a repo refuses labels, and the body marker is
    then the only thing that says it is slopstopper's."""
    out = []
    for issue in gh.api(f"repos/{repo.full_name}/issues?state=open&per_page=100", paginate=True) or []:
        if "pull_request" in issue:
            continue
        finding = classify(issue)
        if cfg.ignore_label not in finding.labels and _is_slopstoppers(finding):
            out.append(finding)
    return out


def _disabled_workflows(repo: Repo) -> list[dict]:
    workflows = (gh.api(f"repos/{repo.full_name}/actions/workflows?per_page=100") or {}).get("workflows", [])
    return [{"name": w["name"], "path": w["path"], "state": w["state"]}
            for w in workflows if w.get("path", "").startswith(".github/workflows/ss-") and w.get("state") != "active"]


def _open_orders(repo: Repo) -> dict[str, dict]:
    orders: dict[str, dict] = {}
    for pr in gh.api(f"repos/{repo.full_name}/pulls?state=open&per_page=100", paginate=True) or []:
        ref = (pr.get("head") or {}).get("ref", "")
        if not ref.startswith(BRANCH_PREFIX):
            continue
        marker = ORDER_MARKER.search(pr.get("body") or "")
        orders[marker.group(1) if marker else ref[len(BRANCH_PREFIX):]] = {"number": pr["number"], "url": pr.get("html_url", "")}
    return orders


def take(repo: Repo, latest: str | None, cfg: Config) -> Inventory:
    inv = Inventory(repo, latest)
    inv.profile = read_profile(gh.contents(repo.full_name, ".slopstopper.yml", repo.default_branch))
    inv.skills_present = gh.contents(repo.full_name, TRIAGE_SKILL, repo.default_branch) is not None
    inv.findings = _findings(repo, cfg)
    inv.disabled_workflows = _disabled_workflows(repo)
    inv.open_orders = _open_orders(repo)
    return inv
