# Operate the steward

## Reading a run

Every run leaves `.steward/reports/run.md` (also the Actions job summary
and the `steward-run` artifact). Three tables:

| Section | Holds |
| ------- | ----- |
| Adopters | each repo, its pin, PyPI's latest, the CLI that wrote its suite, open findings, the evidence it was found by |
| Orders | each order worked and its status: `pr`, `draft`, `skipped` (with why), `failed` (with the error), or `planned` in a dry run |
| Deferred / Skipped | what the budgets left for next time, orders with an open PR, disabled workflows that need an admin |

Agent cost and turns are on the order's row when one ran.

## What each PR means

- **Ready PR, `chore(slopstopper): move the CLI pin to X`.** The pin moved,
  `slopstopper install` from X rewrote the suite, every static check is
  green. Review the diff like any dependency bump; the release notes
  between the two pins are folded into the body.
- **Draft PR with "Still red after the refresh".** The new release
  changed what a check expects and the agent could not settle it inside
  the rules. The body lists the red checks and what the agent tried.
  Finish it by hand with the `slopstopper-triage` skill, or close it and
  put `steward:ignore` on nothing: the next run will open it again until
  the pin moves, which is the point.
- **PR `fix(slopstopper): <check> (closes #N)`.** The agent worked an
  open finding. The body names the category it chose (real finding,
  false positive, threshold) and what it verified. A `# why` comment
  sits on every suppression or threshold change; if it does not convince
  you, ask for the real fix on the PR.
- **Draft with "Looks like a slopstopper bug".** The agent judged the
  fault to be slopstopper's and left the adopter report in the body.
  Raise it on `hungovercoders/slopstopper` (the form applies the label),
  then mark the workaround with the issue number so the next refresh can
  remove it.

## Keeping the steward off something

- An issue: add the label named by `labels.ignore` (`steward:ignore`).
- A repo: `repos.exclude` in `steward.yml`.
- A kind of work: `agent.enabled: false` stops every agentic order;
  `upgrade.policy: patch` keeps pins inside a minor.
- Everything: `mode: plan`.

## Re-running one repo

```bash
steward run --repo owner/name            # or the dispatch input `repo`
steward run --repo owner/name --dry-run
```

An order whose PR is still open is skipped, so closing the PR is how you
ask for a fresh attempt.

## Budgets

| Key | Default | Effect |
| --- | ------- | ------ |
| `budgets.repos_per_run` | 5 | repos worked per run, worst-off first; the rest are listed as deferred |
| `budgets.prs_per_repo` | 2 | open steward PRs per repo, counting the ones already open |
| `budgets.agent_usd` / `agent_turns` | 5 / 60 | per agent invocation |
| `upgrade.min_age_days` | 2 | a release younger than this is not a target yet |

## When a run fails

A `failed` order names the exception; the run exits 1 and the workflow
goes red, but the other orders still ran. A run that exits 2 did not
start: `gh` unauthenticated, `steward.yml` invalid, PyPI unreachable. The
message says which.
