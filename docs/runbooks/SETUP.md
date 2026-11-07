# Set the steward up for an organisation

One repo per organisation or group of organisations: this one for
hungovercoders, a copy of it for anyone else. The run is a GitHub Actions
workflow in that repo; the only state is `steward.yml` and the PRs it
opens. Nothing in the package or the workflow names an organisation, so
a copy changes one file and adds its own secrets.

## 1. Your own copy

**Fork (recommended).** Fork `hungovercoders/slopstopper-steward` into
your organisation. The fork keeps its link to upstream, so GitHub's
*Sync fork* button (or `git fetch upstream && git merge upstream/main`)
brings later steward fixes in with one click; your only local commits
are `steward.yml` and the odd badge, so the merge is clean. GitHub
disables scheduled workflows on a fork until you enable Actions on it
once, from the fork's Actions tab.

**Template.** When the repo's owner has marked it a template, *Use this
template* creates a repo with a clean history and no upstream link.
Choose it when a fork is not allowed in your organisation, or you want
no trace of hungovercoders' history. Fixes then come by hand: add
`https://github.com/hungovercoders/slopstopper-steward` as a remote named
`upstream` and merge or cherry-pick when its releases say so.

Either way, keep the name `slopstopper-steward` if you can: every PR body
the steward writes links to the steward repo by that name. Then make the
copy yours:

- `steward.yml`: replace it, step 2. The committed one is hungovercoders'.
- `README.md`: the badge block points at hungovercoders' workflows. Run
  `slopstopper install` once in the copy to regenerate it for yours, or
  delete the block between the `badges:begin` and `badges:end` markers.
- Everything else stays. The `ss-*.yml` workflows and `.slopstopper.yml`
  are slopstopper installed on the steward itself, so your copy is an
  adopter and your steward keeps its own pin current. `steward-ci.yml`
  clones `hungovercoders/slopstopper` for the contract test, which is
  upstream for every copy.

A copy whose `steward.yml` still names the example `your-org` exits 2
before it reads anything. A copy whose `orgs` misses the owner of the
repo it runs in prints a warning on every command, since the App token
in step 3 is minted for that owner; it still runs, for the one case where
one copy stewards several organisations with a PAT.

## 2. Name the orgs

Copy `steward.yml.example` over `steward.yml`, set `orgs` to your
organisation, and start in `mode: plan` so the first runs only report. Narrow with `repos.include`
while you watch it work.

## 3. A token that can write to the adopters

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
*Contents: read and write*, *Workflows: read and write*, *Pull requests:
read and write*, *Issues: read*, *Checks: read*, *Actions: read*,
*Metadata: read*. *Checks* and *Actions* are what let the steward wait for
a PR's own checks and read a failing job's log. The same *Contents* and
*Pull requests* rights let it merge with `merge.auto`; a base branch rule
requiring a review or a status check the steward does not wait for makes
GitHub refuse that merge, which the PR body then reports.
*Workflows* is what lets an upgrade or refresh push the `ss-*.yml` files
`slopstopper install` rewrites: without it GitHub refuses the push, and
the order fails with that refusal. Install it on the adopter repos (or
all repos). Changing an App's permissions later asks the org owner to
accept them on the installation. Then in the steward repo:

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

## 4. The agent

On Actions the agent runs as its own OS user: the run workflow creates
`steward-agent` and sets `STEWARD_AGENT_USER`, so it cannot touch the
steward's clone, venvs or token. Running the steward unattended anywhere
else, do the same: create an unprivileged user, give the steward's
account passwordless `sudo -u` to it with environment passing allowed
(`--preserve-env`, which a rule for `ALL` commands implies; a narrower
rule needs `SETENV`), install the `claude` CLI at the version
`steward-run.yml` pins (its flags are the box) and the `node` it runs on
where that user finds them (`/usr/local/bin`, `/usr/bin` or `/bin`), and
export `STEWARD_AGENT_USER`. Dedicate that user to the steward, one run
at a time: after each agent run the steward stops every process running
as it. Deny it cron and `at` (`/etc/cron.deny`, `/etc/at.deny`): the
steward clears its crontab and `at` queue anyway, before and after it
stops the user's processes. A `--workdir` must then be traversable by
that user; the steward checks that the first time an order needs the
agent. Before any agentic order the steward probes exactly this, by
running `claude --version` as the agent user the way the run will; if the
probe fails, agentic orders are skipped with its reason rather than run
unisolated.

The agentic orders (a finding to triage, a red check after an upgrade)
run Claude Code headless. Give it one of two secrets:

- `CLAUDE_CODE_OAUTH_TOKEN`, to run on a Claude Pro or Max subscription
  rather than API billing: run `claude setup-token` on a machine where
  Claude Code is signed in to that account, and store the token it
  prints. Agent runs then count against that subscription's usage
  limits, shared with your own Claude use, so keep `budgets.agent_turns`
  modest.
- `ANTHROPIC_API_KEY`, billed per token to an Anthropic API account.

Set one, not both: with both present Claude Code uses the API key, and
the API bills it. Without either, upgrades still run and findings
are reported as skipped with the reason. `budgets.agent_usd` and
`budgets.agent_turns` cap each invocation; `agent.enabled: false`
switches the agentic orders off entirely.

## 5. First run

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

## 6. Schedule

`steward-run.yml` runs weekly (Monday 06:17 UTC) and on dispatch; one run
at a time (`concurrency`). A `plan` job dry-runs every adopter, then one
job per repo with work runs it, two at a time, each with its own token. Move the cron to fit the org's review rhythm:
a run that opens PRs nobody reviews only piles up drafts, and the
per-repo budget then stops it from opening more.
