# Step 10: When to hand off, and maintaining this skill

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 10: When to hand off + maintaining this skill

This skill is one of two in the slopstopper skill set:

- **`slopstopper-install`** (this one), for a first-time install OR refresh of an existing install. The mode-detection branch at the top of the skill routes you to the right subset of steps.
- **`slopstopper-triage`**, to diagnose a failing slopstopper check end-to-end: workflow → local task → report → finding category → fix location.

Hand off to `slopstopper-triage` mid-install whenever a check fails during Step 7's local loop, or during a refresh when a previously-green check goes red.

### Maintaining this skill when slopstopper changes

This skill names specific files, env vars, workflow IDs, the `GENERIC_WORKFLOWS` list in `cli/slopstopper/suite.py`, and the local `task ss:*` commands that mirror each workflow. **Any change to slopstopper that touches one of those needs a corresponding update here**, or the skill silently drifts away from reality.

Triggers that require revisiting this skill:

- A workflow is added, removed, or renamed under `slopstopper/.github/workflows/ss-*.yml` → update the workflow count in the intro, Step 1.2, and Step 3; add/remove the matching local-CLI row in Step 7's Pass A or Pass B; add/remove the badge example in Step 6. Also classify it into the profiles: whether it applies to an API or a library decides which `disables` lists in `cli/slopstopper/data/profiles.json` it belongs to, and the per-profile counts quoted in Step 1.2, Step 3 and Pass B move with it. (The per-check failure entry lives in `slopstopper-triage`, so update there too.)
- A check workflow is added or renamed → its `name:` must also land in `ss-pr-summary.yml`'s `workflow_run.workflows` list, or the PR summary silently stops re-rendering when that check finishes. Check this whenever the workflow inventory in Step 3 changes.
- A profile is added, or a profile's `disables` list changes in `cli/slopstopper/data/profiles.json` → update the shape table in Step 1's pre-flight framing, the per-profile workflow counts (Step 1.2, Step 2, Step 3), the Pass B callout in Step 7, and the `profile:` bullet in the Refresh-only knobs list.
- A new key lands under `api:` in `.slopstopper.yml.example` → update Step 4's "Configure the API checks" block, which is the only place this skill spells that schema out.
- The `GENERIC_WORKFLOWS` tuple in `cli/slopstopper/suite.py` changes → confirm the "What just landed" inventory in Step 3 still matches.
- A check is added or renamed in `cli/slopstopper/checks/__init__.py`'s `REGISTRY` → update Step 7's `slopstopper run` list (and `slopstopper-triage`'s reproduce table).
- A `task ss:*` shim is renamed in `slopstopper/Taskfile.ss.yml` → update the matching command in Step 7's Pass A or Pass B (and `slopstopper-triage`).
- A new env var is introduced for a dynamic check → add to the URL-defaults list in Step 4 and to the Pass B example in Step 7.
- A new `slopstopper` subcommand is added (e.g. `init`, `inspect`) → mention in the intro and surface in the relevant Step.
- The installer's behaviour changes (new tracked-files mechanism, different deletion semantics, additional refresh targets, CLI install path change) → update the "Refresh-only" section's "what the installer wipes / leaves alone" lists.
- The pre-push hook changes (which checks it runs, the `--no-hooks` flag, the `core.hooksPath` wiring guard, or the `.githooks/pre-push` path) → update Step 3's inventory, the "What `install.sh` writes" lists, Step 7's hook callout, and the Refresh-only hook bullet (and the gotcha row in `slopstopper-triage`).
- The installer's repo-topic step changes (the topic name, the `--no-topic` flag, how it detects `gh`) → update Step 3's banner bullet in `install.md` and the topic confirmation under Step 8 in `verify.md`.
- The workaround marker (`slopstopper-workaround: hungovercoders/slopstopper#<N>`) changes → update the "Remove workarounds" grep in the Refresh-only section and `upstream.md` in `slopstopper-triage` together, since the two sides of the contract must match byte for byte.
- A new hardcoded-on-reinstall surface emerges (another file the installer overwrites that users commonly hand-edit) → add to the "Re-apply customizations" subsection of the "Refresh-only" section.

The companion to this is `AGENTS.md` in the slopstopper repo: its "When making changes" table flags the skill as a follow-on target whenever a change of the above kind ships. If you're updating slopstopper itself and that table isn't pointing readers back here, fix that first.

The skills are installed at project level by `slopstopper install` (what `install.sh` hands over to), from the wheel, or by `install-skill.sh` standalone, from `main`. Both iterate the same list (`SKILLS` in `cli/slopstopper/suite.py` and in `install-skill.sh`: `slopstopper-install`, `slopstopper-triage`) and install each `<repo>/.claude/skills/<skill>/` directory: `SKILL.md` (validated to start with frontmatter) plus every `references/*.md` it links. A reference file the map doesn't link is never installed. If you rename a skill, add a third, change the frontmatter contract, or otherwise change the shape of what ships, update both lists (and `OBSOLETE_SKILLS` in both for a removed name) AND `docs/runbooks/INSTALL_SKILLS.md` in the same change.
