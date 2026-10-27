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
    status: str  # pr | draft | skipped | failed | planned
    pr_url: str = ""
    detail: str = ""
    agent: dict | None = None
    red: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"order": self.order, "status": self.status, "pr_url": self.pr_url, "detail": self.detail,
                "agent": self.agent, "red": self.red, "unverified": self.unverified}


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
                result = o.pr_url or _cell(o.detail)
                if o.agent and o.agent.get("cost_usd") is not None:
                    result += f" (agent ${o.agent['cost_usd']:.2f}, {o.agent.get('turns')} turns)"
                lines.append(f"| {o.order['repo']} | `{o.order['id']}` | {o.status} | {result} |")
        if self.deferred:
            lines += ["", "## Deferred by budget", ""] + [f"- {d['repo']}: `{d['id']}` ({_cell(d['reason'])})" for d in self.deferred]
        if self.skipped:
            lines += ["", "## Skipped", ""] + [f"- {s['repo']}: `{s['order']}`: {_cell(s['why'])}" + (f" {s['url']}" if s.get('url') else "") for s in self.skipped]
        return "\n".join(lines) + "\n"


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
