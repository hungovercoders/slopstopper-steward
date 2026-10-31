# The slopstopper contract

The steward re-implements nothing: it reads files slopstopper's installer
leaves behind, parses markers slopstopper's `emit` writes, runs the
pinned CLI's own commands and hands the agent the skills the install
ships. That holds only while those surfaces hold, so they are written
down as data in [`steward/contract.py`](../../steward/contract.py),
checked by this repo's CI and named, for contributors, in slopstopper's
change map. slopstopper's own CI does not run the check.

## How sync is kept

| Guard | Where | Catches |
| ----- | ----- | ------- |
| `tests/test_contract.py` | this repo's CI, on every PR and nightly against slopstopper `main` | a surface slopstopper moved, within a day |
| `steward contract <path>` | the same check as a command, for a local slopstopper checkout or a branch | the same, before the slopstopper PR merges |
| the "steward" row in slopstopper's [`docs/contributing/CHANGE_MAP.md`](https://github.com/hungovercoders/slopstopper/blob/main/docs/contributing/CHANGE_MAP.md) | slopstopper | a contributor there learns which files the steward reads before renaming one |
| skills fetched at the pinned tag, never `main` | `steward/execute/agent.py` | the method the agent follows matching the CLI version that runs |
| the pinned CLI does the writing and the verifying | `steward/execute/refresh.py` | the steward never has to know what a release changed |

The steward tracks slopstopper's **contracts**, not its versions: a new
slopstopper release needs no steward release unless a row below moves.
When one does, change the row and the code that reads it in one commit,
regenerate the table (`task steward:contract-doc`) and say which
slopstopper release moved it.

## Surfaces relied on

Generated from `steward/contract.py`; `tests/test_docs.py` fails while
this table is stale.

<!-- contract:begin -->
| Surface | Text relied on | Steward module | Why |
| --- | --- | --- | --- |
| `cli/slopstopper/installer.py` | `"pipx:slopstopper-cli"` | `discover.read_pin` | the mise.toml key the pin lives under |
| `cli/slopstopper/installer.py` | `.ss/.installed-from` | `discover.probe` | the marker naming the CLI version that wrote the suite |
| `cli/slopstopper/installer.py` | `"--add-topic", "slopstopper"` | `discover.discover` | the repo topic the installer adds |
| `cli/slopstopper/emit.py` | `slopstopper:check=` | `inventory.classify` | the body marker naming the check an issue belongs to |
| `cli/slopstopper/emit.py` | `slopstopper:workflow=` | `inventory.classify` | the body marker naming the workflow that raised an issue |
| `cli/slopstopper/emit.py` | `_BRAND_LABEL = "slopstopper"` | `inventory.take` | the label every emitted issue carries |
| `.github/workflows/ss-workflow-failure-issue.yml` | `const label = 'workflow-failure'` | `inventory.classify` | the label the failure tracker puts on its issues |
| `.github/workflows/ss-workflow-failure-issue.yml` | `\`❌ Workflow Failure: ${workflowName}\`` | `inventory.classify` | the title the failure tracker gives its issues |
| `.claude/skills/slopstopper-triage/SKILL.md` | `name: slopstopper-triage` | `execute.agent` | the skill the agent works a finding with |
| `.claude/skills/slopstopper-install/SKILL.md` | `name: slopstopper-install` | `execute.agent` | the skill the agent verifies a refresh with |
| `.claude/skills/slopstopper-triage/references/upstream.md` | `slopstopper-workaround: hungovercoders/slopstopper#` | `prompts` | the marker a workaround carries so a refresh can remove it |
| `cli/slopstopper/cli.py` | `"install"` | `execute.refresh` | `slopstopper install` writes the suite the pinned CLI ships |
| `cli/slopstopper/cli.py` | `"--no-topic"` | `execute.refresh` | the installer flag that keeps a clone from editing repo topics |
| `docs/contributing/README.md` | `\`run --json <check>\`` | `execute.refresh.verify` | the machine-readable run verdict: {check, exit_code, status, failures, reports} |
| `docs/contributing/README.md` | `\`checks list --json\`` | `execute.refresh.static_checks` | the check listing with its `disabled` flag and workflow |
| `docs/contributing/README.md` | `\`profile expand <n> --json\`` | `execute.refresh.static_checks` | the workflows a profile drops: `library` is slopstopper's own list of URL-driven checks |
| `docs/contributing/README.md` | `\`doctor --json\`` | `execute.refresh.static_checks` | which external tools are missing on the runner |
| `docs/contributing/README.md` | `\`needed_by\`` | `execute.refresh.static_checks` | the check each missing tool belongs to, so that check is reported unverified instead of red |
| `cli/slopstopper/checks/__init__.py` | `"security:licences"` | `checks.NEEDS_ENVIRONMENT` | a registered check that needs the project's installed environment, which a clone lacks |
| `cli/slopstopper/checks/licences.py` | `pip inspect` | `checks.NEEDS_ENVIRONMENT` | why security:licences needs an installed environment: it inventories the interpreter's packages |
| `.claude/skills/slopstopper-install/references/refresh.md` | `## Refresh-only` | `prompts.review` | the refresh section the agent works on every upgrade |
| `.claude/skills/slopstopper-install/references/refresh.md` | `'slopstopper-workaround: hungovercoders/slopstopper#[0-9]+'` | `execute.refresh.workarounds` | the workaround marker the triage skill writes and the refresh removes |
| `.slopstopper.yml.example` | `profile:` | `execute.refresh.schema_diff` | the config schema the review diffs between the two pins, where a new knob shows |
| `release-please-config.json` | `"include-v-in-tag": true` | `execute.agent.ensure_skills` | release tags are v<version>, so skills can be fetched at the pinned tag |
<!-- contract:end -->

## The version floor

`slopstopper install` (the suite written from the wheel's own files)
shipped after slopstopper-cli 0.26.0. `steward/versions.py` names
the first release that carries it as `MIN_INSTALL_VERSION`; a target below
it is reported, never worked, and a repo pinned below it is moved straight
to a release at or above it. Until that release is on PyPI the end-to-end
test drives the executor from a slopstopper source checkout
(`STEWARD_E2E_SOURCE`), which is also what CI does nightly so `main`'s
installer is proven against the steward before it is released.

## Surfaces deliberately not relied on

- **`install.sh`** is not run: it needs mise on the runner and reaches
  for `main`'s copy of itself. The two things it does after bootstrapping
  (`mise use` on one line, then `slopstopper install`) are what
  `execute/refresh.py` does directly.
- **The `slopstopper` topic** is evidence, never the only test: it needs
  admin rights to set, so many installs never get it.
- **The PR summary comment and the per-check PR comments** belong to a
  PR; the steward works from the issues on the default branch.
