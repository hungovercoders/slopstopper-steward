---
name: slopstopper-triage
description: Diagnose and fix a failing slopstopper check, including a check that doesn't apply to the repo's shape (a browser check on an API or library). Use when a user reports a specific slopstopper workflow or task is failing: SAST, secrets, complexity, docs-accuracy, docs-size, docs-structure, entry-files, CSP exceptions, auto-label, smoke, E2E, accessibility, Core Web Vitals, SEO, broken links, DAST, dependency review, API health, API latency, OpenAPI drift, API headers/CORS, the workflow-failure tracker, or the post-deploy Cloudflare trigger. Maps each check to the local task that reproduces it, the report files it generates, the typical root-cause categories (real finding / false positive / threshold too tight), a per-symptom gotcha table with diagnostic steps and the file the fix lives in, and the fourth outcome, a bug in slopstopper itself, which gets raised upstream on hungovercoders/slopstopper and worked around with a marker a later refresh removes. For first-time install OR refreshing an existing install use slopstopper-install.
---


# Triage a failing slopstopper check

You're being asked to fix a slopstopper check that's failing. This skill is reactive. It assumes the install is in place and at least one check is red. If the user is mid-install, `slopstopper-install` Step 7 handed off here; the procedure is the same.

Every check runs through `slopstopper-cli`: each `ss-*.yml` workflow is `uses: ./.github/actions/ss-setup`, then `slopstopper run <category>:<check>`, then `slopstopper emit … --target {pr-comment,issue}`. URL-driven checks add `uses: ./.github/actions/ss-resolve-url`, which turns the event into the URL to audit. If a check ran against the wrong URL, that action's table is the first place to look. So the local reproducer is always `slopstopper run …` (or its `task ss:*` shim).

This file is the map. The long tables live in `references/` and are **read only when a step needs them**, so the whole skill costs one page of context until then.

The shape of the fix is one of four things:

1. **A real finding.** The check is right and the code is wrong. Fix the code.
2. **A false positive.** The heuristic misfires on this codebase. Suppress narrowly via the check's documented mechanism, always with a `# why` comment.
3. **A threshold too tight for this repo.** The check is correct but its config isn't. Tune in `.slopstopper.yml`.
4. **A slopstopper bug.** The check, template, workflow or installer is wrong for every repo, not just this one. Raise it on `hungovercoders/slopstopper` and, if the repo can't wait for the fix, work around it with a `slopstopper-workaround:` marker so the next refresh removes it once the fix ships.

Don't apply blanket suppressions and don't loosen thresholds without writing down why. Don't fix slopstopper's bugs silently inside an adopter repo.

## Step 1: Identify which check is failing

Slopstopper workflows follow the naming pattern `ss-<loop>-<action>-check.yml`. Decode:

- **`<loop>`** = `security`, `hygiene`, `reliability` → maps to `docs/<loop>/README.md` for the loop-level overview.
- **`<action>`** = `sast`, `secrets`, `complexity`, `docs-accuracy`, etc. → maps to a specific check name `<loop>:<action>` in the CLI's `REGISTRY` (`cli/slopstopper/checks/__init__.py`).

If the user only gave you the workflow URL or a failing CI badge, click through to the workflow file under `.github/workflows/` in the target repo and read the `run:` step. Task-mode workflows call `task ss:<category>:<check>`, and `--no-task` mode workflows call `slopstopper run <category>:<check>` directly. Either way the check name is the same one you use locally.

### Fastest route: read the PR summary comment

Every PR carries one rolling comment headed `SlopStopper: N of M checks failed`, with the failures in a table linking straight to their logs, and the passing checks folded below. Start there: it names the failing checks and nothing else, so you don't have to scan the Actions tab or 20 comment cards.

Each failing check also posts its own compact comment, with a verdict line, the failing items, and the full report folded into a `<details>`. Open that fold before pulling the artifact: it is the same report, already on the page.

A `⚠️` verdict is neither. The advisory checks (`hygiene:docs-size`) report findings while deliberately exiting 0, so they post `⚠️ …: has alerts` and keep the comment. The job is green and the summary shows ✅, because the alert is a nudge rather than a gate.

**A green PR has no per-check comments at all.** Passing checks delete theirs (`emit --on-pass=delete`), so the summary is the only comment. An absent comment means "passed", not "didn't run". The summary's own table is what distinguishes those.


## Step 2: Reproduce locally

Every workflow has a `task ss:<category>:<check>` shim and a `slopstopper run <category>:<check>` form; dynamic checks take the URL as a bare positional after `--`. The browser checks build and serve the repo on `localhost:8080`; the four API checks and `hygiene:openapi` never build locally, so reproduce them against the environment CI audited. `reliability:api-latency` does not reproduce reliably anywhere; re-run it in the same environment a few times before believing one result.

The full workflow → task → CLI table, with the URL / local-build / Docker columns, is in **`references/reproduce.md`**.

## Step 3: Read the report

Most checks write structured output under `.ss/reports/<check>/`:

- `.ss/reports/<check>/<check>-report.md` is human-readable and the one you want first.
- `.ss/reports/<check>/<check>-report.json` is machine-readable, useful for grep / jq.
- `slopstopper run --json <category>:<check>` prints one object on stdout (`exit_code`, `status`, the `- ❌` `failures` the PR comment lists, the `reports` it wrote) with the check's own output on stderr, so you can read the verdict without parsing text. `slopstopper doctor --json` and `slopstopper profile show --json` do the same for tools and the profile.

For DAST: `.ss/reports/dast/dast-report.json` + `.ss/reports/dast/dast-report.md` + `.ss/reports/dast/dast-gate.json` (the swallow-vs-block decisions the gate made, with `source` field naming which exception mechanism let each one through).

For Playwright: `playwright-report/` at the target repo root (Playwright's own HTML report).

Read the report before deciding the fix shape. Most reports name the file, line, rule, and severity, which collapses Step 4 immediately.


## Step 4: Categorise the finding

Real finding → fix the code. False positive → the check's own suppression mechanism (`.gitleaks.toml`, `# nosemgrep`, `.zap/rules.tsv`, `.trivyignore`, `docs/security/CSP_EXCEPTIONS.md`…), narrowly, with `# why`. Threshold → the matching `.slopstopper.yml` knob (`hygiene.complexity.max_ccn`, `hygiene.docs_size.*`, `hygiene.entry_files.*`, `api.*`, `security.sast.fail_on`…).

The suppression table and the config-driven-knob table are in **`references/categorise.md`**.

Slopstopper bug → **`references/upstream.md`**: how to tell, the issue to open (`gh issue create -R hungovercoders/slopstopper --label slopstopper`), and the marker every workaround carries.

## Step 5: Per-symptom gotcha table

Once you know the symptom, look it up before reasoning from scratch: **`references/gotchas.md`** maps each one (a browser check red on an API repo, a check green in Actions but its comment says failed, DAST reporting nothing against an API, `api-latency` failing in CI and passing locally, exit `2` with no findings…) to the diagnostic step, the root cause, the fix, and the file it lives in.

## Steps 6–7: Broader fixes, and deleting the check instead

When the failing check is really a symptom of the repo's shape (wrong profile, no AGENTS.md-first docs layout, no headers source), fix the shape. When a check genuinely does not apply, delete it deliberately, through `workflows.disabled` or the profile, never a silent workflow deletion. **`references/broader-fixes.md`** covers both, including which checks are safe to drop and which never are.

## When to hand off

- **First-time install** on a new repo → `slopstopper-install` (you're here because that skill handed off mid-install, so go back when this check is green).
- **Refreshing an existing install** → `slopstopper-install`; its refresh section is the mechanical "re-run installer + re-apply customizations" loop, and it is what removes the `slopstopper-workaround:` markers this skill leaves once their issue is closed. A check that is new since the last refresh is still this skill's job.

Anything that changes slopstopper itself needs a matching edit here or the tables rot. **`references/maintaining.md`** lists the triggers.
