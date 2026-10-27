"""The surfaces of slopstopper the steward relies on, as data.

The steward reads slopstopper's own artefacts and runs slopstopper's own
commands; it never re-implements a check or a fix. That only stays true
while these surfaces hold, so each row names the file in the slopstopper
source and the text that must be in it. `steward contract <path>` checks a
checkout, `tests/test_contract.py` runs it in CI against slopstopper's
main, and `docs/contributing/CHANGE_MAP.md` over there lists the same rows
so a change on either side is caught before it ships.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Surface:
    path: str
    needle: str
    used_by: str
    why: str


SURFACES: tuple[Surface, ...] = (
    Surface("cli/slopstopper/installer.py", '"pipx:slopstopper-cli"', "discover.read_pin",
            "the mise.toml key the pin lives under"),
    Surface("cli/slopstopper/installer.py", ".ss/.installed-from", "discover.probe",
            "the marker naming the CLI version that wrote the suite"),
    Surface("cli/slopstopper/installer.py", '"--add-topic", "slopstopper"', "discover.discover",
            "the repo topic the installer adds"),
    Surface("cli/slopstopper/emit.py", "slopstopper:check=", "inventory.classify",
            "the body marker naming the check an issue belongs to"),
    Surface("cli/slopstopper/emit.py", "slopstopper:workflow=", "inventory.classify",
            "the body marker naming the workflow that raised an issue"),
    Surface("cli/slopstopper/emit.py", '_BRAND_LABEL = "slopstopper"', "inventory.take",
            "the label every emitted issue carries"),
    Surface(".github/workflows/ss-workflow-failure-issue.yml", "const label = 'workflow-failure'", "inventory.classify",
            "the label the failure tracker puts on its issues"),
    Surface(".github/workflows/ss-workflow-failure-issue.yml", "`❌ Workflow Failure: ${workflowName}`", "inventory.classify",
            "the title the failure tracker gives its issues"),
    Surface(".claude/skills/slopstopper-triage/SKILL.md", "name: slopstopper-triage", "execute.agent",
            "the skill the agent works a finding with"),
    Surface(".claude/skills/slopstopper-install/SKILL.md", "name: slopstopper-install", "execute.agent",
            "the skill the agent verifies a refresh with"),
    Surface(".claude/skills/slopstopper-triage/references/upstream.md", "slopstopper-workaround: hungovercoders/slopstopper#",
            "prompts", "the marker a workaround carries so a refresh can remove it"),
    Surface("cli/slopstopper/cli.py", '"install"', "execute.refresh",
            "`slopstopper install` writes the suite the pinned CLI ships"),
    Surface("cli/slopstopper/cli.py", '"--no-topic"', "execute.refresh",
            "the installer flag that keeps a clone from editing repo topics"),
    Surface("docs/contributing/README.md", "`run --json <check>`", "execute.refresh.verify",
            "the machine-readable run verdict: {check, exit_code, status, failures, reports}"),
    Surface("docs/contributing/README.md", "`checks list --json`", "execute.refresh.verify",
            "the check listing with its `disabled` flag"),
    Surface("release-please-config.json", '"include-v-in-tag": true', "execute.agent.ensure_skills",
            "release tags are v<version>, so skills can be fetched at the pinned tag"),
)


def check(src: Path) -> list[str]:
    """Problems with a slopstopper checkout, empty when every surface holds."""
    problems = []
    for s in SURFACES:
        path = src / s.path
        if not path.is_file():
            problems.append(f"{s.path}: missing (needed by {s.used_by}: {s.why})")
        elif s.needle not in path.read_text(encoding="utf-8", errors="replace"):
            problems.append(f"{s.path}: no longer contains {s.needle!r} (needed by {s.used_by}: {s.why})")
    return problems


def table() -> str:
    rows = ["| Surface | Text relied on | Steward module | Why |", "| --- | --- | --- | --- |"]
    for s in SURFACES:
        needle = s.needle.replace("|", "\\|").replace("`", "\\`")
        rows.append(f"| `{s.path}` | `{needle}` | `{s.used_by}` | {s.why} |")
    return "\n".join(rows)
