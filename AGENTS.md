# slopstopper-steward agent guide

Guide for agents and people working in this repo (`CLAUDE.md` is
`@AGENTS.md`). What most tasks need is here; the routes at the end say
when to open anything else.

## What this repo is

The org-wide operator for slopstopper adopters. `steward/` is a
stdlib-only Python package and CLI: it discovers repos with slopstopper
installed, moves pins that fell behind, and works the issues slopstopper
raised, one PR each. slopstopper owns every check and every fix method;
the steward only drives them. It also has slopstopper installed on itself
(`task ss:*`, the `ss-*.yml` workflows), like any adopter.

## Ground rules

1. **No slopstopper knowledge lives here.** The steward reads surfaces
   slopstopper publishes (`steward/contract.py` lists them) and runs
   slopstopper's own commands. If a fix needs to know how a check works,
   it belongs in slopstopper's skills, not in a steward prompt.
2. **Deterministic where possible, agent only for judgement.** Pins,
   installs and verification are code. The agent sees a finding or a red
   check after a refresh, inside a clone, with no push rights.
3. **Every write is a PR on a `steward/<order>` branch**, carrying
   `<!-- steward:order=<id> -->`. Never a push to a default branch, never a
   merge. One open PR per order; re-runs are idempotent.
4. **Exit codes follow slopstopper's:** 0 ran clean, 1 ran and failed, 2
   could not run. `--json` prints one value and nothing else.
5. **Conventional Commits.** Third-party actions are pinned to a commit SHA.
6. **Run `task steward:test` and `task ss:hygiene:test` before pushing.**

## Commands

| Command                          | Does                                                        |
| -------------------------------- | ----------------------------------------------------------- |
| `task --list`                    | every runnable task                                         |
| `task steward:test`              | pytest, including the slopstopper contract test             |
| `task steward:plan`              | the orders a run would open (needs `gh` auth)               |
| `task steward:run -- --dry-run`  | a full run as a report, nothing written                     |
| `task steward:contract`          | check a slopstopper checkout still carries every surface    |
| `task steward:contract-doc`      | regenerate the surface table in the architecture docs       |
| `task ss:hygiene:test`           | slopstopper's static hygiene suite on this repo             |

## Layout

```
AGENTS.md              this file (CLAUDE.md is @AGENTS.md)
steward/               the package: discover → inventory → plan → execute/ → report; cli.py is the entry
steward/contract.py    the slopstopper surfaces relied on, as data (tests + docs read it)
steward/prompts.py     the agent brief: names the work and the skill, never the method
steward/checks.py      what a clone cannot verify (an installed environment); the rest is asked of the pinned CLI
steward/sandbox.py     the stripped environment the agent and verification share; workspace.py hardens git
tests/                 pytest; test_contract.py is the sync guard
steward.yml            this org's config; steward.yml.example is the schema
.github/workflows/     steward-run.yml (the sweep), steward-ci.yml (pytest + contract), ss-*.yml (slopstopper)
docs/                  one concern per doc; docs/README.md is the map
```

## Routes: read before you act

| When you are…                                                   | Do this                                                                                         |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| changing what the steward reads from slopstopper, or slopstopper moved something | Read [docs/architecture/CONTRACT.md](./docs/architecture/CONTRACT.md). It lists every surface and how a change on either side is caught |
| asking how a run works, what an order is, or where the agent is boxed | Read [docs/architecture/README.md](./docs/architecture/README.md). It has the pipeline, the order kinds and the guardrails |
| setting the steward up for an organisation (tokens, secrets, first run) | Read [docs/runbooks/SETUP.md](./docs/runbooks/SETUP.md). It walks the GitHub App, the secrets and the dry run |
| reading a run report, keeping the steward off an issue, or handling a draft PR | Read [docs/runbooks/OPERATE.md](./docs/runbooks/OPERATE.md). It covers labels, budgets and what each outcome means |
| contributing code: tests, exit codes, commit conventions          | Read [docs/contributing/README.md](./docs/contributing/README.md) first                         |
| doing any task not covered above                                 | Read [docs/README.md](./docs/README.md) before you start; do not guess a convention              |
