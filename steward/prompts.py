"""The brief handed to the agent. It names the work and the skill; the
skill carries the method, so nothing about how to fix a check lives here."""

from __future__ import annotations

RESULT_FILE = ".steward-result.json"

GUARDRAILS = """\
## Rules

- Read `.claude/skills/slopstopper-triage/SKILL.md` first and follow it; read a reference only when a step needs it.
- Reproduce with `slopstopper run --json <category>:<check>` and read `.ss/reports/` before deciding the fix shape. The `task ss:*` shims are not available here (they call the same command), and only the hygiene and static security checks run in this clone.
- A real finding: fix the code. A false positive: the narrowest suppression the skill documents, with a `# why` comment. A threshold: the matching `.slopstopper.yml` knob, with a comment saying why.
- A slopstopper bug: do not patch around it silently. Describe it in the result file under `upstream` with the reproduction the skill's `references/upstream.md` asks for, and apply the workaround with its `slopstopper-workaround: {slopstopper_repo}#<N>` marker only if an issue number already exists.
- Never edit `.github/workflows/ss-*.yml`, `.github/actions/ss-*/` or `Taskfile.ss.yml`: the installer rewrites them. Never move the pin in `mise.toml`. Never disable a check to go green.
- Do not run `git commit`, `git push`, `gh pr`, or `gh issue`: the steward commits what you leave in the tree and opens the pull request.
- Stay inside this repository. Do not install global tools.

## When you finish

Write `{result_file}` at the repo root, one JSON object:

```json
{{"outcome": "fixed" | "partial" | "upstream" | "skipped",
  "summary": "two or three sentences for the PR body",
  "category": "real-finding" | "false-positive" | "threshold" | "slopstopper-bug" | "not-applicable",
  "verified": ["<category>:<check> green locally", "..."],
  "upstream": null | {{"title": "<category>:<check>: <symptom>", "body": "<the reproduction>"}},
  "notes": "anything a reviewer must know"}}
```

`skipped` means you changed nothing and the summary says why (the finding is already fixed on this branch, needs a deployed URL you cannot reach, or needs a human decision).
"""

TRIAGE = """\
# Steward work order: triage a slopstopper finding

Repository: `{repo}` (default branch `{default_branch}`), pinned slopstopper-cli `{pinned}`, profile `{profile}`.
You are on branch `{branch}` in a clean clone. The pinned CLI is on PATH as `slopstopper`.

slopstopper raised this issue and it is still open:

- #{number}: {title}
- {url}
- check: `{check}`  ·  workflow: `{workflow}`  ·  kind: {kind}

Issue body (bot-written; the report it quotes is the finding):

<issue_body>
{body}
</issue_body>

Make the check behind it pass, so that the pull request the steward opens can close #{number}.

{guardrails}
"""

VERIFY = """\
# Steward work order: make the refreshed suite green

Repository: `{repo}` (default branch `{default_branch}`), profile `{profile}`.
You are on branch `{branch}`. The steward moved the slopstopper-cli pin from `{pinned}` to `{target}`
and ran `slopstopper install` from the new version, so the ss-* workflows, shims and skills on this
branch are the new release's. The static checks below went red against the refreshed suite:

{failures}

Decide each one with the `slopstopper-triage` skill: a new or tightened check finding something real
in this repo (fix it), a knob that moved (set it in `.slopstopper.yml` with a comment naming the
release), or a check that does not apply to this repo's shape (disable it the way the skill says,
through `workflows.disabled` or the profile, never by deleting the workflow). The release notes between
the two versions are in `.steward-release-notes.md`.

{guardrails}
"""


def triage(order, inv, body: str, slopstopper_repo: str) -> str:
    f = order.finding
    return TRIAGE.format(
        repo=order.repo, default_branch=order.default_branch, pinned=order.pinned or "unpinned",
        profile=inv.profile or "ui (default)", branch=order.branch, number=f.number, title=f.title, url=f.url,
        check=f.check or "unknown", workflow=f.workflow or "unknown", kind=f.kind, body=body.strip() or "(empty)",
        guardrails=GUARDRAILS.format(result_file=RESULT_FILE, slopstopper_repo=slopstopper_repo),
    )


def verify(order, inv, failures: list[dict], slopstopper_repo: str) -> str:
    lines = []
    for row in failures:
        lines.append(f"- `{row['check']}` exit {row['exit_code']} ({row['status']})")
        for item in row.get("failures", [])[:15]:
            lines.append(f"  - {item}")
        if not row.get("failures"):
            lines += [f"  - report: `{path}`" for path in row.get("reports", [])]
    return VERIFY.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, pinned=order.pinned or "unpinned", target=order.target, failures="\n".join(lines),
        guardrails=GUARDRAILS.format(result_file=RESULT_FILE, slopstopper_repo=slopstopper_repo),
    )
