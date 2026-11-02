# Dependabot orders

How the steward works Dependabot's pull requests in an adopter, with
`dependabot.enabled` in `steward.yml`. Everything else about a run is in
[README.md](README.md); this is the one order kind that does not live on
a `steward/<order>` branch.

## What becomes an order

The inventory lists Dependabot's open PRs: its own (`dependabot[bot]`),
from the same repository, not drafts, without the `labels.ignore` label.
Each becomes a `dependabot-<number>` order after the upgrade, refresh and
triage orders, inside a budget of its own, `dependabot.budget` (default
5): these are not steward PRs, and `budgets.prs_per_repo` counts those,
so a backlog of bumps neither starves nor is starved by the steward's
own work.

Three kinds of PR are reported as skipped, never worked:

- one whose current head a steward comment already names (below): the
  steward had its turn on that head, and a new push from Dependabot or a
  person makes it eligible again;
- one whose bump the steward cannot size, because no version can be read
  (a digest, a date);
- one past `dependabot.updates` (patch and minor by default): a major
  bump is a person's decision.

The size comes from the title (`Bump <dep> from <a> to <b>`) or, for a
grouped PR, from the body's `Updates \`<dep>\` from <a> to <b>` lines; the
biggest step among them is the PR's.

## How it is worked

The order checks out Dependabot's branch at its tip and runs the same
settle loop a steward PR gets: wait for the PR's checks, and while a
round is left (`ci.fix_rounds`), work what is red. Two things differ from
a steward PR:

- **Every red check is the bump's**, the repo's own tests included, not
  only the slopstopper ones the triage skill covers. The agent gets each
  failing job's log tail, the bump's release notes quoted from the PR
  body (as data, cut at 8k), and a brief that says a fix adapts the
  repo to the new version and never loosens, skips or deletes a test,
  never pins the dependency back, never edits the lock by hand. A bump
  the repo should not take is reported, not forced. The agent cannot run
  the repo's build or tests in its box; the PR's CI is the verifier, so
  each round costs a CI run. The self-review applies as everywhere, and
  an unsettled review is not pushed.
- **The steward's own gate is "nothing newly red"**, not "all green": its
  static checks are run before and after the agent, and only a check the
  fix turned red blocks the push, since one red on the base is the
  repo's.

The push is a plain fast-forward onto Dependabot's branch, never the
steward's `--force-with-lease`; a tip that moved since the fetch refuses
it. Dependabot stops rebasing a PR someone else has pushed to, so from
then on the PR is the steward's (and the person's).

## What it leaves behind

Dependabot's body is never rewritten. One "slopstopper-steward" comment
says how it ended, with the agent's account and its self-review: green
after N fix rounds, still red and left for a person, or green but not
merged and why. It carries `<!-- steward:worked=<sha> -->` for the head it
worked, which is what keeps the next run off that head. A PR that was
green at once, with nothing to do, gets neither push nor comment.

With `dependabot.merge` (default false) a PR the steward saw green, at
once or after its rounds, is merged by `merge.method`, deleting the
branch per `merge.delete_branch`. The conditions are the ones a steward
PR's merge has: the wait really saw checks, none red, none still running,
nothing stopped the wait. A merge GitHub refuses (a branch rule wanting a
review, a conflict) is said in the comment and the PR left green for a
person. A conflict clears itself: merging one bump often conflicts the
next (`go.sum`, a lockfile), Dependabot rebases a conflicted PR on its
own, the new head no longer matches the marker, and the next run merges
it. `merge.auto` and `merge.kinds` are a separate switch, for the
steward's own PRs.

A job that was cancelled rather than failed proves nothing either way:
the order ends `skipped` with the job named, no agent runs and no comment
is posted, so a re-run of the job that goes green is merged by the next
run. The order's status in the run report is `green`, `red`, `merged`, or
`skipped` when the checks never settled.

## Keeping it off

- One PR: the `labels.ignore` label (`steward:ignore`).
- A size: `dependabot.updates`.
- Merging: `dependabot.merge: false`.
- All of it: `dependabot.enabled: false`.
