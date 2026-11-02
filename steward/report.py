"""One run, written down: the plan, what each order did, what waits."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPORT_DIR = Path(".steward/reports")


def _cell(text: object, limit: int = 300) -> str:
    """One line, pipes escaped, bounded: safe inside a markdown table row."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


@dataclass
class Outcome:
    order: dict
    status: str  # pr | merged | draft | skipped | failed | deferred | planned
    pr_url: str = ""
    detail: str = ""
    agent: dict | None = None
    red: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    ci: dict | None = None

    def to_dict(self) -> dict:
        return {"order": self.order, "status": self.status, "pr_url": self.pr_url, "detail": self.detail,
                "agent": self.agent, "red": self.red, "unverified": self.unverified, "ci": self.ci}


@dataclass
class RunReport:
    mode: str
    repos: list[dict] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "started": self.started, "repos": self.repos,
                "outcomes": [o.to_dict() for o in self.outcomes], "deferred": self.deferred, "skipped": self.skipped}

    def markdown(self) -> str:
        lines = [f"# Steward run ({self.mode}) · {self.started}", ""]
        lines += ["## Adopters", "", "| Repo | Pin | Latest | Suite written by | Open findings | Evidence |", "| --- | --- | --- | --- | --- | --- |"]
        for r in self.repos:
            lines.append(f"| {r['repo']} | {r.get('pinned') or '—'} | {r.get('latest') or '—'} | {r.get('installed_from') or '—'} "
                         f"| {len(r.get('findings', []))} | {', '.join(r.get('evidence', []))} |")
        lines += ["", "## Orders", ""]
        if not self.outcomes:
            lines.append("Nothing to do.")
        else:
            lines += ["| Repo | Order | Status | Result |", "| --- | --- | --- | --- |"]
            for o in self.outcomes:
                result = (o.pr_url or _cell(o.detail)) + _agent_cell(o.agent) + _ci_cell(o.ci)
                lines.append(f"| {o.order['repo']} | `{o.order['id']}` | {o.status} | {result} |")
        lines += self._failures()
        if self.deferred:
            lines += ["", "## Deferred by budget", ""] + [f"- {d['repo']}: `{d['id']}` ({_cell(d['reason'])})" for d in self.deferred]
        if self.skipped:
            lines += ["", "## Skipped", ""] + [f"- {s['repo']}: `{s['order']}`: {_cell(s['why'])}" + (f" {s['url']}" if s.get('url') else "") for s in self.skipped]
        return "\n".join(lines) + "\n"

    def _failures(self) -> list[str]:
        """Each failed order's whole detail: the table cell is cut short, and
        the reason (git's own words for a refused push, say) is at the end."""
        failed = [o for o in self.outcomes if o.status == "failed"]
        if not failed:
            return []
        lines = ["", "## Failures", ""]
        for o in failed:
            lines += [f"`{o.order['id']}` on {o.order['repo']}:", "", "~~~text", o.detail.replace("~~~", "~ ~ ~"), "~~~", ""]
        return lines[:-1]


def write(report: RunReport, out_dir: Path = REPORT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "run.md", out_dir / "run.json"
    md.write_text(report.markdown(), encoding="utf-8")
    js.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report.markdown())
    return md, js


def _agent_cell(agent: dict | None) -> str:
    """What the agent run cost, how long it took and what it ran, for the table."""
    if not agent or agent.get("cost_usd") is None:
        return ""
    model = f", {agent['model']}" if agent.get("model") else ""
    review = agent.get("review") or {}
    tail = ""
    if review.get("outcome"):
        tail = f"; self-review {review['outcome']}" + (", corrected" if review.get("corrected") else "")
        if review.get("cost_usd") is not None:
            tail += f" ${review['cost_usd']:.2f}"
    return f" (agent ${agent['cost_usd']:.2f}, {agent.get('turns')} turns{model}{tail})"


def _ci_cell(ci: dict | None) -> str:
    """How the PR's own checks ended, for the table."""
    if not ci:
        return ""
    if ci.get("note") and not ci.get("waited"):
        return f" · CI not awaited ({ci['note']})"
    rounds = ci.get("rounds") or 0
    if ci.get("failing"):
        names = ", ".join(sorted({f["name"] for f in ci["failing"]}))
        return f" · CI red after {rounds} fix round(s): {names}"
    if ci.get("timed_out"):
        return f" · CI still running after {ci.get('waited_minutes', '?')} min"
    green = " · CI green" + (f" after {rounds} fix round(s)" if rounds else "")
    if ci.get("merged"):
        return green + " · merged"
    if ci.get("merge_note"):
        return green + f" · not merged ({ci['merge_note']})"
    return green
