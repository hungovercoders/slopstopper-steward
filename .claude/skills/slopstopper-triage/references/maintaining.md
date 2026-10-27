# Maintaining this skill when slopstopper changes

> Part of the `slopstopper-triage` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Maintaining this skill when slopstopper changes

Update this skill when:

- A new workflow is added under `slopstopper/.github/workflows/ss-*.yml` → add a row to Step 2's workflow→CLI table and a row to Step 5's gotcha table (with a forward-looking diagnostic step and fix location, not citing the install that surfaced it).
- A workflow is renamed or removed → update both tables.
- A check is added or renamed in `cli/slopstopper/checks/__init__.py`'s `REGISTRY` → update Step 2's table (CLI column AND Taskfile-shim column, since the shim mirrors the CLI name). The new check must also define a `META` dict in its module, which the `test_every_check_has_meta` pytest enforces. PR-comment-only checks need at least `report_path` + `comment_discriminator`; checks that open main-branch issues also need `issue_title` / `issue_labels` / `issue_followup` / `issue_close_comment` (see `cli/slopstopper/emit.py` docstring for the schema).
- A `task ss:*` shim is renamed in `slopstopper/Taskfile.ss.yml` → update Step 2's Taskfile-shim column.
- A new `slopstopper` subcommand ships (e.g. `init`, `inspect`) → mention in the intro and the relevant Step.
- A new suppression mechanism becomes available for an existing check (e.g. a new `.zap/rules.tsv`-shaped file for a different tool) → add a row to Step 4's suppression table.
- A new config-driven knob is added to a check (`config.get("<x>")` in the check module) → add to Step 4's "Config-driven knobs" table, with the default mirroring `.slopstopper.yml.example`.
- A profile is added, or a profile's `disables` list changes in `cli/slopstopper/data/profiles.json` → update the two profile rows in Step 5's gotcha table (they name the checks each profile drops) and the profile note in Step 7.
- A knob is added under `api:` in `.slopstopper.yml.example` → update the API rows in Step 5's gotcha table, which name the specific knob that resolves each symptom.
- The workaround marker (`slopstopper-workaround: hungovercoders/slopstopper#<N>`) or the upstream issue template changes → update `upstream.md` here and the "Remove workarounds" step in `slopstopper-install`'s `refresh.md` together, since the refresh greps for exactly that marker.
- The PR comment shape changes (`emit --target pr-comment`, `--status`, `--on-pass=delete`, or `ss-pr-summary.yml`) → update Step 1's "Fastest route" section and the three comment-layer rows in Step 5's gotcha table.

The AGENTS.md "When making changes" table in the slopstopper repo flags this skill alongside `slopstopper-install` whenever a change of the above kind ships.
