# Operate the steward

## Reading a run

Every run leaves `.steward/reports/run.md` and `.steward/reports/run.json`. On Actions the
sweep's dry run is the `steward-plan` artifact and job summary, and each
repo the plan named gets its own job and a `steward-run-<owner>__<repo>`
artifact. Three tables:

| Section | Holds |
| ------- | ----- |
| Adopters | each repo, its pin, PyPI's latest, the CLI that wrote its suite, open findings, the evidence it was found by |
| Orders | each order worked and its status: `pr`, `merged` (with `merge.auto`), `draft`, `skipped` (with why), `failed` (with the error), `deferred` (the push token would expire before it finished; the next run picks it up), or `planned` in a dry run |
| Deferred / Skipped | what the budgets left for next time, orders with an open PR, disabled workflows that need an admin |

Agent cost and turns are on the order's row when one ran, with the
self-review's verdict and cost after them.

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
- **PR `fix(slopstopper): <check> (#N)`.** The agent worked an open
  finding. The body names the category it chose (real finding, false
  positive, threshold) and what it verified. It says `Closes #N` only when
  the steward re-ran that check green in its clone. A `Refs #N` with a
  "Not verified by the steward" section means it could not (a URL-driven
  check, a workflow failure with no single check, a scanner missing on the
  runner): the PR's own checks are the verdict, and the issue stays open
  until the check passes on the default branch. A `# why` comment
  sits on every suppression or threshold change; if it does not convince
  you, ask for the real fix on the PR.
- **Draft with "Looks like a slopstopper bug".** The agent judged the
  fault to be slopstopper's and left the adopter report in the body.
  Raise it on `hungovercoders/slopstopper` (the form applies the label),
  then mark the workaround with the issue number so the next refresh can
  remove it.

### "CI still red" and "Checks"

Every PR body ends with how the PR's own checks went. "The PR's N checks
are green" means the steward waited for them (and, if it says "after a fix
round", pushed a fix the agent made from the failing jobs' logs). "CI still
red" lists the checks that stayed red, with links; the PR is a draft.
Work them with the `slopstopper-triage` skill on the PR's branch: the
steward does not come back to a settled order. "Not awaited" means the run
hit its time limit before the checks finished; the PR is ready and its
checks are whatever the PR page shows. `ci.wait_minutes`, `ci.fix_rounds`
and `ci.wait` in `steward.yml` set the limits.

### "Self-review" and "Merged"

Under the agent's own account of its work sits a second agent's verdict
on the diff: "Self-review (approved)", or "changes-requested" with its
findings and the correction round's answer. "Self-review not settled"
means the findings were not addressed and the PR is a draft: read them,
fix or dismiss them on the branch, and mark it ready. "No verdict" means
the reviewer did not answer usably; nothing is held on it.

"Merged" means `merge.auto` is on and the run merged its own PR once the
checks were green; the run report's row says `merged`. "Not merged" means
it was on but the merge did not happen (GitHub refused it, or the steward
never saw the checks green itself); the PR is ready for a person. Turn it
on in `steward.yml` once the self-review has earned it on a few PRs, and
keep a branch rule on the base if a human review must stay in the loop:
the steward merges with the token's rights and nothing more.

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
ask for a fresh attempt: the next run fetches the old `steward/<order>`
branch and replaces it (a lease against its tip, so nothing stacks).

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
