# Set the steward up for an organisation

One repo (this one, or a fork of it) per organisation or group of
organisations. The run is a GitHub Actions workflow in that repo; the
only state is `steward.yml` and the PRs it opens.

## 1. Name the orgs

Copy `steward.yml.example` to `steward.yml`, set `orgs`, and start in
`mode: plan` so the first runs only report. Narrow with `repos.include`
while you watch it work.

## 2. A token that can write to the adopters

`GITHUB_TOKEN` is scoped to the steward repo, so writes elsewhere need one
of two things. A GitHub App is the better one: installable per repo,
revocable, and its PRs trigger the adopters' own `ss-*` workflows, which
a PAT from a user account also does but a `GITHUB_TOKEN` would not.

The steward checks only that `gh` holds a token before it starts; an App
installation token has no user identity, which is fine. A token that
cannot do the work fails the first real call and the run exits 2 with
GitHub's message.

**GitHub App (recommended).** Create one under the org (Settings →
Developer settings → GitHub Apps) with repository permissions
*Contents: read and write*, *Pull requests: read and write*, *Issues:
read*, *Actions: read*, *Metadata: read*. Install it on the adopter
repos (or all repos). Then in the steward repo:

- Actions **variable** `STEWARD_APP_ID`: the App ID
- Actions **secret** `STEWARD_APP_PRIVATE_KEY`: the App's private key (PEM)

**Fine-grained PAT (fallback).** A token with the same permissions on the
adopter repos, stored as the secret `STEWARD_TOKEN`. The workflow uses it
only when no App is configured.

An installation token lasts an hour. The run therefore mints one per
adopter, scoped to that one repo, in its own job, and tells the steward
when it expires (`STEWARD_DEADLINE`, set to 55 minutes after minting).
The steward starts no order with less than 20 minutes left and caps
each agent run to finish 10 minutes before the deadline; what does not
fit is reported `deferred` and the next run picks it up. A PAT has no
such limit and sets no deadline. The plan job mints its read token for
the steward repo's own owner, so stewarding several orgs from one repo
needs `STEWARD_TOKEN` (or the App installed and a plan job per org).

Labels: the steward creates `slopstopper` and `steward` in a repo when
they are missing and never alters one that exists, since `slopstopper`
is the label the adopter's own suite uses. If it cannot add them, the PR
is still opened, unlabelled.

## 3. The agent

On Actions the agent runs as its own OS user: the run workflow creates
`steward-agent` and sets `STEWARD_AGENT_USER`, so it cannot touch the
steward's clone, venvs or token. Running the steward unattended anywhere
else, do the same: create an unprivileged user, give the steward's
account passwordless `sudo -u` to it with environment passing allowed
(`--preserve-env`, which a rule for `ALL` commands implies; a narrower
rule needs `SETENV`), install the `claude` CLI and the `node` it runs on
where that user finds them (`/usr/local/bin`, `/usr/bin` or `/bin`), and
export `STEWARD_AGENT_USER`. Dedicate that user to the steward, one run
at a time: after each agent run the steward stops every process running
as it. A `--workdir` must then be traversable by that user. Before any agentic order the steward probes exactly this, by
running `claude --version` as the agent user the way the run will; if the
probe fails, agentic orders are skipped with its reason rather than run
unisolated.

The agentic orders (a finding to triage, a red check after an upgrade)
run Claude Code headless. Add the secret `ANTHROPIC_API_KEY` (or a
`CLAUDE_CODE_OAUTH_TOKEN`). Without it, upgrades still run and findings
are reported as skipped with the reason. `budgets.agent_usd` and
`budgets.agent_turns` cap each invocation; `agent.enabled: false`
switches the agentic orders off entirely.

## 4. First run

Run **Steward · Run** from the Actions tab with *Plan only* ticked, or
locally:

```bash
gh auth status                      # any token that can read the orgs
pip install -e .
steward discover                    # the adopters and the evidence for each
steward plan                        # the orders, nothing written
steward run --dry-run               # the same as a report in .steward/reports/
```

Read the report. Repos you expected and do not see are missing both the
`mise.toml` pin and `.ss/.installed-from`: run `slopstopper install`
there once. When the plan looks right, set `mode: pr` and widen
`budgets.repos_per_run` as the PRs prove themselves.

## 5. Schedule

`steward-run.yml` runs weekly (Monday 06:17 UTC) and on dispatch; one run
at a time (`concurrency`). A `plan` job dry-runs every adopter, then one
job per repo with work runs it, two at a time, each with its own token. Move the cron to fit the org's review rhythm:
a run that opens PRs nobody reviews only piles up drafts, and the
per-repo budget then stops it from opening more.
