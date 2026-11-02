"""The brief handed to the agent. It names the work and the skill; the
skill carries the method, so nothing about how to fix a check lives here."""

from __future__ import annotations

RESULT_FILE = ".steward-result.json"

GUARDRAILS = """\
## Rules

- Read `.claude/skills/slopstopper-triage/SKILL.md` first and follow it; read a reference only when a step needs it.
- Reproduce with `slopstopper run --json <category>:<check>` and read `.ss/reports/` before deciding the fix shape. The `task ss:*` shims are not available here (they call the same command). The checks you may run in this clone: {checks}. Any other check, and any command not named here, is refused with no one to approve it, so do not retry one.{apart}
- One plain command per Bash call: no `cd` (you are already at the repository root), no pipes, loops or `; echo $?` (the tool reports a non-zero exit code itself). A compound command is refused whole.
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


REVIEW = """\
# Steward work order: review a slopstopper upgrade

Repository: `{repo}` (default branch `{default_branch}`), profile `{profile}`.
You are on branch `{branch}`. {moved} ({why}), so the ss-* workflows, shims and skills
on this branch are `{target}`'s.

The install was mechanical; what it cannot judge is what the `slopstopper-install` skill's refresh is for: read
`.claude/skills/slopstopper-install/SKILL.md`, then work `references/refresh.md` against this branch.
You have no network and no `git diff`, so the steward gathered what that section asks for:

- `.steward-suite-diff.md`: everything the install changed, new files included.
- `.steward-release-notes.md`: the release notes from `{start}` to `{target}`.
- `.steward-schema-diff.md`: the change to `.slopstopper.yml.example`, the config schema, between the two
  versions, which is where a new knob shows.
- The workarounds this repo carries, with the state of each upstream issue:
{workarounds}

{red}

{guardrails}
For this order, `outcome` says whether the review is done: `fixed` when you worked the refresh section
to the end, whether or not anything needed changing (the summary says which); `upstream` when it found a
slopstopper bug; `partial` when something is left for a person (say what in `notes`); `skipped` only
when you could not do the review.
"""


CI_FIX = """\
# Steward work order: settle the pull request's failing checks

Repository: `{repo}` (default branch `{default_branch}`), profile `{profile}`.
You are on branch `{branch}`, the branch of an open pull request the steward made ({what}).
The pull request's own CI ran, and these slopstopper checks failed on it:

{failures}

`.steward-ci-failures.md` holds the end of each failing job's log. A check named in the rules below
as one you may run here, reproduce here first; the browser and API checks need the built site or a
deployed URL and run only on the pull request's CI, which runs again after the steward pushes your
change. Read `.claude/skills/slopstopper-triage/SKILL.md` and work each failure with it: read the log
for the real cause first (a step before the check, such as `npm ci`, failing is the cause, not the
check), then fix it the way the skill says. The steward keeps `package-lock.json` in step with
`package.json` itself after you finish, so do not regenerate a lock by hand.

{guardrails}
For this order, `outcome` is `fixed` when you changed something that should turn each failing check
green, `partial` when some stay for a person (say which in `notes`), `skipped` when you changed nothing.
"""


SELF_REVIEW = """\
# Steward work order: review the change before it is pushed

Repository: `{repo}` (default branch `{default_branch}`), profile `{profile}`.
You are on branch `{branch}`. The steward's agent just worked this order: {what}.
Its own account of the work: {summary}

You are the second pair of eyes, and you can change nothing: no editing tool is available to you.
`.steward-change.md` holds the whole pending change as a unified diff, which is what the steward will
commit; read it, and the files it touches where the diff alone is not enough. Judge the change only:

- Does it do what the order asked, the way the `slopstopper-triage` skill (or, for an upgrade, the
  `slopstopper-install` skill's refresh section) says? Read the skill if you need its rules.
- Does it break one of the steward's rules: a check disabled to go green, an `ss-*.yml` workflow,
  `ss-*` action or `Taskfile.ss.yml` edited, the pin in `mise.toml` moved, a suppression or threshold
  without a `# why` comment that holds up?
- Does it leave something next to it broken: a reference to a file it moved, a lock out of step, a
  doc that now states the wrong thing, a knob set in the wrong place?

You may run these checks here, read-only, to confirm: {checks}. One plain command per Bash call, no
`cd`, pipes or loops; any other command is refused with no one to approve it, so do not retry one.

Reply with one JSON object and nothing else as your final message:

```json
{{"outcome": "approved" | "changes-requested",
  "summary": "one or two sentences for the PR body",
  "findings": ["<file>: <what is wrong, and what to do instead>", "..."]}}
```

`changes-requested` is for a defect in the change itself: wrong, unsafe, incomplete against the order,
or against the rules above. Style is not a finding, and neither is work the order did not ask for.
An empty `findings` means approved.
"""


CORRECT = """\
# Steward work order: address the review of your change

Repository: `{repo}` (default branch `{default_branch}`), profile `{profile}`.
You are on branch `{branch}`. An earlier pass worked this order: {what}.
A reviewer read the pending change (the diff as it then stood is in `.steward-change.md`, its
findings in `.steward-review.md`) and asked for changes:

{findings}

Address each finding you agree with, in the tree. Where you disagree, leave it and say why in `notes`.
The rules you worked under are unchanged:

{guardrails}
For this order, `outcome` is `fixed` when every finding is addressed or answered, `partial` when
something is left for a person (say what in `notes`), `skipped` when you changed nothing.
"""


def _what(order) -> str:
    return (f"the slopstopper-cli pin moved to `{order.target}`" if order.kind in ("upgrade", "refresh")
            else f"a fix for #{order.finding.number} ({order.finding.check or 'a slopstopper finding'})"
            if order.finding else "an order")


def self_review(order, inv, summary: str, checks=()) -> str:
    names = ", ".join(f"`{c}`" for c in checks) or "none (nothing this clone can verify)"
    return SELF_REVIEW.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, what=_what(order), summary=summary.strip() or "(none given)", checks=names,
    )


def correct(order, inv, findings: list[str], slopstopper_repo: str, checks=(), apart=()) -> str:
    return CORRECT.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, what=_what(order), findings="\n".join(f"- {f}" for f in findings) or "- (none listed)",
        guardrails=_guardrails(slopstopper_repo, checks, apart),
    )


def ci_fix(order, inv, failures: list[dict], slopstopper_repo: str, checks=(), apart=()) -> str:
    rows = [f"- `{f['name']}` ({f['conclusion']}), workflow `{f['workflow'].rsplit('/', 1)[-1]}`: {f['url']}"
            for f in failures]
    return CI_FIX.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, what=_what(order), failures="\n".join(rows),
        guardrails=_guardrails(slopstopper_repo, checks, apart),
    )


def _guardrails(slopstopper_repo: str, checks, apart=()) -> str:
    names = ", ".join(f"`{c}`" for c in checks) or "none (nothing this clone can verify)"
    proved = ""
    if apart:
        proved = (" " + ", ".join(f"`{c}`" for c in apart) + " you cannot run here (it installs the project), but the "
                  "steward runs it after you, in its own box: a change that needs it, such as retiring a workaround on "
                  "it whose upstream issue is closed, is yours to make and the steward's to prove; say in `notes` "
                  "that you did.")
    return GUARDRAILS.format(result_file=RESULT_FILE, slopstopper_repo=slopstopper_repo, checks=names, apart=proved)


def _failure_lines(failures: list[dict]) -> list[str]:
    lines = []
    for row in failures:
        lines.append(f"- `{row['check']}` exit {row['exit_code']} ({row['status']})")
        for item in row.get("failures", [])[:15]:
            lines.append(f"  - {item}")
        if not row.get("failures"):
            lines += [f"  - report: `{path}`" for path in row.get("reports", [])]
    return lines


def review(order, inv, why: str, workarounds: list[dict], failures: list[dict], slopstopper_repo: str,
           start: str | None = None, checks=(), apart=()) -> str:
    marks = [f"  - `{w['marker']}` in {', '.join(f'`{p}`' for p in w['files'])}: {w['state']}" for w in workarounds]
    red = ("The static checks below went red against the refreshed suite; settle them too, with the "
           "`slopstopper-triage` skill:\n\n" + "\n".join(_failure_lines(failures))) if failures else \
        "Every static check the steward can run here is green against the refreshed suite."
    start = start or order.pinned or "unpinned"
    moved = (f"The pin was already `{order.target}`, but `{start}` wrote the suite; the steward re-ran "
             f"`slopstopper install` from `{order.target}`") if order.kind == "refresh" else \
        (f"The steward moved the slopstopper-cli pin from `{order.pinned or 'unpinned'}` to `{order.target}` "
         f"and ran `slopstopper install` from it")
    return REVIEW.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, moved=moved, start=start, target=order.target, why=why,
        workarounds="\n".join(marks) or "  - none", red=red,
        guardrails=_guardrails(slopstopper_repo, checks, apart),
    )


def triage(order, inv, body: str, slopstopper_repo: str, checks=(), apart=()) -> str:
    f = order.finding
    return TRIAGE.format(
        repo=order.repo, default_branch=order.default_branch, pinned=order.pinned or "unpinned",
        profile=inv.profile or "ui (default)", branch=order.branch, number=f.number, title=f.title, url=f.url,
        check=f.check or "unknown", workflow=f.workflow or "unknown", kind=f.kind, body=body.strip() or "(empty)",
        guardrails=_guardrails(slopstopper_repo, checks, apart),
    )


def verify(order, inv, failures: list[dict], slopstopper_repo: str, checks=(), apart=()) -> str:
    lines = _failure_lines(failures)
    return VERIFY.format(
        repo=order.repo, default_branch=order.default_branch, profile=inv.profile or "ui (default)",
        branch=order.branch, pinned=order.pinned or "unpinned", target=order.target, failures="\n".join(lines),
        guardrails=_guardrails(slopstopper_repo, checks, apart),
    )
