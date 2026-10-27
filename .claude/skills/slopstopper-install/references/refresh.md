# Refresh-only: diff upstream, re-apply customizations, spot new knobs

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Refresh-only: diff upstream, re-apply customizations, spot new knobs

**Skip this section on a first install.** It's relevant only when `.slopstopper.yml` and `.ss/.workflows-installed` both existed in Step 1 (mode-detection), meaning the installer just ran in-place over an existing slopstopper install.

`install.sh` is idempotent but **not transactional**. Every shipped workflow YAML, `Taskfile.ss.yml`, `.github/actions/ss-*/` and the `# slopstopper begin/end` block in `.gitignore` is rewritten wholesale on every run, from the files the **pinned** `slopstopper-cli` in `mise.toml` ships (a refresh reinstalls that version and never bumps it, as "Move the CLI pin" below explains; so a refresh with an unmoved pin changes nothing but what you deleted or disabled). A refresh of an older install also migrates a legacy `cli_version` from `.slopstopper.yml` into `mise.toml` and strips it, and strips a dead `node_version` key. A few classes of customization get wiped and need re-applying; a few classes of upstream change need manual catch-up because the installer doesn't drag everything across. Walk this section before the local-verify loop in Step 7.

### Re-enable workflows GitHub switched off

A repo quiet enough to need a refresh is often one GitHub has stopped running. It disables any workflow with a `schedule:` trigger after 60 days without repo activity, and a disabled workflow runs on no trigger, pull requests included, so those checks silently vanish from the PR's list. The post-install banner lists any `ss-*` workflow in that state with its `gh workflow enable <id>` command, and `slopstopper doctor` reports the same. Re-enable them (ask the user first; it changes repo settings) before the Step 7 loop, or the confirmation pass on CI will be missing checks.

### Confirm the installed set is the pin's

The workflows come from the pinned `slopstopper-cli`'s wheel, so the installed set is that release's minus the profile's drops and the adopter's deletions. Compare what the pin knows with what is on disk:

```bash
comm -23 \
  <(slopstopper checks list --json | jq -r '.[].workflow' | sort) \
  <(ls .github/workflows/ | grep '^ss-' | sort)
```

Each line is a workflow the profile drops (`slopstopper profile expand <name>`) or one that was deleted and is still recorded in `.ss/.workflows-installed`, which is a choice to confirm, not undo. A check that only a newer release ships is not a gap to copy across by hand; it arrives with the pin, under "Move the CLI pin" below.

### Re-apply customizations the installer wiped

The installer refreshes `Taskfile.ss.yml`, the slopstopper block in `.gitignore` (between its markers only), the `.ss/` overlay, `.ss/.installed-from`, `.githooks/pre-push`, the `.github/actions/ss-*/` composite actions and the `ss-*.yml` workflows wholesale. Anything hand-edited in those files is gone. `.slopstopper.yml`, though, is **never** overwritten by the installer, so the bulk of customization (headers source/format, URLs, pages, og-image path, disabled workflows, hygiene thresholds) survives every re-run. The Node version lives in `mise.toml` and is seeded only when absent, so a bump you made there survives too.

What still needs re-checking after a refresh:

- **Anything hand-edited inside `ss-*.yml` workflow files** beyond what `.slopstopper.yml` covers. Common case: extra workflow-level `permissions:` for a custom integration, a non-standard schedule, or workflow-level env vars beyond the documented URL/PAGES set. Diff against upstream to find them. Push bespoke wording into the check's META in `slopstopper-cli` upstream rather than hand-editing the YAML locally, because workflow edits don't survive `install.sh` re-runs.
- **Anything hand-edited inside `.ss/tests/*.spec.ts`, `.ss/playwright.config.js`, or `.ss/lighthouserc.json`.** These used to be seeded by `install.sh` but now live inside the `slopstopper-cli` wheel. The installer's byte-equality scrub removes unmodified copies (so the wheel's version wins via the templates resolver); customized copies survive in `.ss/` and continue to override.
- **The Node version pin.** It lives in `mise.toml` (`[tools] node`), not `.slopstopper.yml`. `install.sh` seeds `node = "20"` only when none is declared, so a bump you made (`mise use node@22`) survives a refresh and there's nothing to re-sync, since mise locally and CI via `jdx/mise-action` read the same pin. If you mirror URLs as repo variables, re-push those after editing `.slopstopper.yml`.
- **The pre-push hook file** (`.githooks/pre-push`) is refreshed wholesale because it's slopstopper-owned, so hand-edits to it are lost. The `core.hooksPath` **wiring** is re-applied on every refresh **when safe**, meaning when hooksPath is unset or already `.githooks` and no other hook manager is present. It won't override a hooksPath the adopter pointed elsewhere, or a husky/lefthook/pre-commit setup. Note the flip side: an adopter who simply *unset* hooksPath will have it re-wired on the next refresh (empty is indistinguishable from never-set). To opt out durably, pass `--no-hooks` on refreshes or point hooksPath at their own dir.

- **`.github/dependency-review-config.yml`** is seeded on the first refresh that ships it, and the dependency review switches from the old inline `deny-licenses` to this allowlist. Check that the licences your dependencies already use are on it before merging the refresh; a missing one fails the next PR that touches that dependency.
- **`.github/labeler.yml`** if upstream's labeler template added categories you want (the installer never overwrites your existing file, so new categories don't land automatically).

### Move the CLI pin (intentional upgrades)

`slopstopper-cli` is pinned per-repo in `mise.toml` (`[tools]` "pipx:slopstopper-cli"), and a plain refresh installs exactly that version and never bumps the CLI. This is deliberate: a breaking upstream release can't surprise the repo. To move the pin:

```bash
bash install.sh --upgrade-cli        # bump to the latest published version
bash install.sh --cli-version X.Y.Z  # pin to an exact version
```

Either flag wraps `mise use` to rewrite the `"pipx:slopstopper-cli"` entry in `mise.toml`, install that version locally, and the change ships to CI (`jdx/mise-action`) once you commit. The post-install banner reports the PyPI latest version and points at `install.sh --upgrade-cli` when the pin is behind, so you always know an upgrade is available without it being forced. Before bumping, skim the slopstopper changelog for breaking changes, then run the Step 7 local-verify loop so the new version is green before you push the pin bump.

**The shell that ran the installer may still run the old CLI.** `mise activate` puts the pinned version's install dir on `PATH` and only refreshes it when the prompt hook fires, so after the pin moves, a bare `slopstopper` in that same shell (or an agent's long-lived shell) can still be the previous version. The banner warns when this happens ("This shell still runs slopstopper X, not the pinned Y"). Before the Step 7 loop, confirm `slopstopper --version` matches the pin; if it doesn't, re-enter the directory, or prefix commands with `mise exec --` (`mise exec -- task ss:hygiene:test`), which always resolves the pin.

### Spot newly-shipped knobs in `.slopstopper.yml.example`

The `.slopstopper.yml.example` file in the slopstopper repo is the schema reference. Don't diff it against the repo's `.slopstopper.yml`: a config seeded from the starter deliberately carries only the keys the repo sets, so that diff lists every default as "missing". Diff the schema between the release the previous install came from and the one this refresh came from. The installer records the `slopstopper-cli` version that wrote it in `.ss/.installed-from`, each release is tagged `v<version>`, and the old value is still in `HEAD` until you commit the refresh:

```bash
raw=https://raw.githubusercontent.com/hungovercoders/slopstopper
old="$(git show HEAD:.ss/.installed-from 2>/dev/null)"   # the previous install's version
new="$(cat .ss/.installed-from 2>/dev/null)"              # this refresh's version
if [ -z "$old" ] || [ -z "$new" ]; then
  echo "no version recorded for the previous install or this one; use the fallback below"
elif ! a="$(curl -fsSL "$raw/v$old/.slopstopper.yml.example")" || ! b="$(curl -fsSL "$raw/v$new/.slopstopper.yml.example")"; then
  echo "a recorded version isn't a published release (a dev build?); use the fallback below"
else
  diff <(printf '%s\n' "$a") <(printf '%s\n' "$b")
fi
```

Lines added on the right are knobs shipped since the last install. That release is what the workflows, `Taskfile.ss.yml` and the rest came from, so an empty diff (or `old` = `new`) really does mean no new knobs. The script above refuses to diff after a failed fetch, so never read one as "everything is new".

**Fallback** (no marker on one side, or a version that is not a published release): read the schema's history at https://github.com/hungovercoders/slopstopper/commits/main/.slopstopper.yml.example back to `git log -1 --format=%cs -- Taskfile.ss.yml .github/workflows/`, the last commit in which a refresh changed slopstopper's files. A refresh that changed nothing leaves no commit, so that date can be earlier than the last refresh: some of what you read may already be in place, but nothing newer is missed.

Every knob, check, shim and workflow in the repo is the pinned release's, so there is nothing installed that the CLI does not know. The one way to get a mismatch is running `slopstopper install` from a CLI other than the pin (a global `pipx` one, say): its banner then warns that the pin and the writing version differ, and the fix is either to move the pin to that version or to re-run as `mise exec -- slopstopper install`. If a knob you want is not in the diff, it shipped in a later release: check the changelog and move the pin with `install.sh --upgrade-cli`. Most knobs ship with sensible defaults, so no action is required. The link at the top of the repo's `.slopstopper.yml` is the schema to copy blocks from.

Surfaces worth checking explicitly:

- **`profile:`**, the project-shape preset (`ui` / `api` / `library`). A config that predates the key has no `profile:` line, which resolves to `ui`. That is the full suite, exactly as before, so nothing changed under the repo. If the repo is an API or a library, this is the highest-value knob in the diff: setting it and re-running the installer removes the browser-and-SEO checks that were never going to pass. Run `slopstopper profile detect` and confirm with the user before switching. Check `workflows.disabled` at the same time, since entries that a profile now covers can be dropped from it.
- **`hygiene.docs_size.*` / `hygiene.entry_files.*` / `hygiene.docs_structure.*`**, per-check thresholds and rule toggles. Defaults are intentionally tight (150 KB / 25 files; 2000 / 600 / 1000 tokens; 300 lines per doc; route depth 3). **0.15 moved the docs checks to AGENTS.md-first**, and a refresh onto a 0.15+ CLI turns these red on a Map Pattern repo: `entry_files.max_words` is ignored (budgets are estimated tokens under `max_tokens`, `readme_max_tokens` and `map_max_tokens`), `CLAUDE.md` must be exactly `@AGENTS.md` (`require_claude_include`), every `.md` link in `AGENTS.md` must be a trigger-first "read" route (`require_explicit_routes`), the map is `docs/README.md` (`map_path`), `docs_structure.require_indexed_docs` is read as `require_routed_docs` (a doc must be *routed*, not merely linked), and `max_route_depth` / `max_doc_lines` are new. The fix is the Step 5 migration in `references/map-pattern.md`, not the knobs: turn a rule off only when it is genuinely wrong for the repo, with a `# why` comment. If the red comes from non-doc content under `docs/` (a course, rendered tutorials), list it in `hygiene.docs.exclude` (default `[docs/archive/**]`; setting it replaces the default), which takes it out of all three docs checks.
- **`hygiene.complexity.max_ccn`**, the CCN ceiling (default 15). The complexity gate lives in the CLI, so `task ss:hygiene:complexity` fails locally and in the pre-push hook exactly as it does in CI. If a function newly blocks a push and is genuinely well-factored, raise the ceiling here rather than contorting the code. **`hygiene.complexity.exclude`** (globs) skips committed generated or vendored code; test files by convention (`*_test.go`, `*.spec.*`, `tests/` …) and gitignored paths are skipped without it, so a Go repo's test functions don't count against the gate.
- **`hygiene.docs_accuracy.extra_paths`**, globs for files outside `docs/` the accuracy check should scan (task and workflow references in markdown, plus every `github.com/<this repo>/blob|tree/…` link in markdown or HTML, must resolve). It is off by default and worth setting for a repo with a marketing site that links into its own source tree, or that ships skills/instructions naming files, since those rot silently otherwise.
- **`security.sast.fail_on`**, the lowest Semgrep severity that fails `security:sast` (`error` default; `warning`, `info`, `none`). The check's exit code is now the verdict. The workflow no longer re-counts findings in a separate step, so what you see locally is what CI gates on.
- **`api.latency.*`**, endpoints to sample plus three opt-in budgets (`median_ms`, `slowest_ms`, `max_bytes`). Set `paths` and leave the budgets unset on first adoption: the check reports timings and enforces nothing until a budget exists, and budgets should be derived from observed numbers rather than guessed.
- **`api.openapi.served_spec` / `probe_paths` / `ignore_paths`**, new keys alongside the existing `spec` (which DAST already used). `served_spec` is the one worth setting: it turns `hygiene:openapi` from a reachability prober into a committed-vs-deployed drift comparison, which is the signal with no false positives. Note `hygiene:openapi` reads **JSON specs only**, so if `spec` is YAML it skips with guidance, while DAST keeps using it.
- **`reliability.coverage.{pr,main,cron}`**, page-discovery modes. Adopters with a sitemap should opt in to `sitemap` on main and `changed` on PRs; otherwise reliability checks only audit `/` by default.
- **`reliability.cwv.public_report`** (default `false`). Reports stay in `.ss/reports/cwv/lighthouse/` and the workflow artifact. `true` uploads the full Lighthouse report, screenshots and page content included, to Google's temporary public storage: only for a public site whose team wants the shareable link, never for a private repo or an authenticated preview.
- **`reliability.sitemap.llms_ignore_paths`**, globs left out of the llms.txt cross-check only (`/tags/*`, `/page/*`). The report collapses five or more gaps under one section into a single note that names this knob; `ignore_paths` also drops the paths from the crawl and the sitemap diff, so use it only for paths the sitemap should not cover either.
- **`reliability.coverage.cross_cutting_paths`**, escalation triggers for `changed` mode. If a PR-only audit skipped pages that should have been included, this is the lever.

If a previously-failing check started passing after a refresh without any code change, eyeball whether a default got loosened upstream, which the slopstopper changelog flags.

### Spot new checks and task targets

Two surfaces to scan after a refresh:

1. **The CLI's check registry.** `slopstopper --help` lists subcommands; the canonical list of check names is in `cli/slopstopper/checks/__init__.py`'s `REGISTRY` dict upstream. If a new check landed (e.g. `reliability:<new-check>`), it's runnable as `slopstopper run reliability:<new-check>` even before any workflow ships it.
2. **The Taskfile shims.** Re-running the installer refreshes `Taskfile.ss.yml`, which mirrors every CLI check as a `task ss:*` shim. Eyeball:

   ```bash
   task --list | grep '^\* ss:'
   ```

If anything looks unfamiliar, check `Taskfile.ss.yml` for the `desc:` and `summary:` (each shim documents the env vars it forwards). New checks are usually surfaced as part of a new workflow. If a new workflow landed in the diff above, you'll see the matching task here regardless, since `Taskfile.ss.yml` is refreshed wholesale.

If new workflows did land and you want the README badges refreshed to match, re-run `slopstopper badges` and replace the existing Pipeline status block in `README.md`.

### Clean up redundant artefacts

Adopter repos should hold exactly the **expected set** of slopstopper artefacts plus their own customisations and nothing more. Anything outside that set is noise: a former skill that's been folded into another, an old `ss-*-check.yml` that's been renamed upstream, a `.ss/` file that moved into the CLI wheel. Keep the working tree tidy on every refresh.

**What `install.sh` and `install-skill.sh` already clean up automatically:**

- `<repo>/.claude/skills/<name>/` directories listed in `OBSOLETE_SKILLS`, currently `install-slopstopper` (single-skill legacy) and `slopstopper-update` (folded into `slopstopper-install`). The list lives in `install-skill.sh`, which `install.sh` runs.
- `.ss/scripts/`, a pre-CLI artefact scrubbed wholesale on every install.
- Byte-equal copies of `.ss/playwright.config.js`, `.ss/lighthouserc.json`, `.ss/lighthouserc.prod.json` and each `.ss/tests/*.spec.ts`, which moved into the slopstopper-cli wheel; byte-identical adopter copies are removed spec by spec (the wheel's version wins via the templates resolver). Customised copies survive. CLI 0.17 and earlier left such copies behind after any local smoke, e2e, accessibility or broken-links run; later releases stage them in the self-ignoring `.ss/.run/` instead, so a run never adds files to `.ss/`.
- Workflows the adopter explicitly disabled via `.slopstopper.yml` `workflows.disabled`, removed on every install.
- `.github/actions/<name>/` directories listed in `OBSOLETE_ACTIONS` in `cli/slopstopper/suite.py` (empty today). The shipped composite actions (`ss-setup`, `ss-resolve-url`, `ss-playwright`) are replaced wholesale on every run.

**What you should sanity-check manually on refresh** (the installer can't auto-detect these without an explicit removal list):

```bash
# 0. Legacy docs map: a pre-0.15 install seeded docs/index.md; the map is docs/README.md now.
#    The installer warns but never renames adopter content, so do it in the refresh commit
#    (`git mv docs/index.md docs/README.md`, then rewrite its rows as routes; Step 5 covers it).
ls docs/index.md 2>/dev/null && echo 'legacy map: migrate (Step 5)'

# 1. Skills: anything in .claude/skills/slopstopper-* that isn't in the current expected set is stale
ls -d .claude/skills/slopstopper-*/ 2>/dev/null
# Expected currently: slopstopper-install, slopstopper-triage. Anything else → flag for the user.

# 2. Composite actions: anything in .github/actions/ss-* other than the shipped set is stale
ls -d .github/actions/ss-*/ 2>/dev/null
# Expected currently: ss-setup, ss-resolve-url, ss-playwright. Anything else → flag for the user (and, if it
# was a slopstopper action that got renamed, add it to OBSOLETE_ACTIONS upstream).

# 3. Workflows: anything matching ss-*-check.yml that doesn't exist upstream is stale
diff \
  <(curl -s https://api.github.com/repos/hungovercoders/slopstopper/contents/.github/workflows | jq -r '.[].name' | grep '^ss-' | grep -vE '^ss-release\.yml$' | sort) \
  <(ls .github/workflows/ | grep '^ss-' | sort)
# Lines prefixed with `>` are local-only ss-* files, either stale or a workflow the adopter
# deliberately customised away from upstream. Ask the user before deleting.
```

The line to draw before deleting:

- **Slopstopper-shipped, no longer in the expected set** → delete. Examples: `slopstopper-update/SKILL.md`, an `ss-old-check.yml` that was renamed in a recent slopstopper release.
- **Adopter-customised, intentionally divergent** → keep. Examples: an `ss-*.yml` the adopter forked under the same name with bespoke logic, a hand-edited `Taskfile.ss.yml` shim. `.ss/.workflows-installed` tracks adopter deletions but not adopter modifications, so when in doubt, ask the user.
- **Adopter-added content under `.claude/skills/` with a non-`slopstopper-` prefix** → not slopstopper's, never touch.

When a clean-up surfaces something the installer should've handled automatically, the fix is to add it to `OBSOLETE_SKILLS` (or the equivalent list for the artefact type) upstream (`OBSOLETE_SKILLS` lives in `install-skill.sh`; the workflow and action lists in `install.sh`). See the AGENTS.md change-impact table's "Removing a slopstopper-shipped artefact" row. The next adopter then gets the cleanup for free.

### Remove workarounds whose upstream issue is closed

When `slopstopper-triage` works around a bug in slopstopper itself, it leaves a marker on the workaround that names the upstream issue, `slopstopper-workaround: hungovercoders/slopstopper#<N>`, in the file's own comment syntax (`upstream.md` in that skill has the convention). A refresh is the moment to collect on those: the pin moved, so fixes may have shipped, and a workaround that outlives its bug keeps a check degraded for no reason.

List every marker and ask GitHub for each issue's state:

```bash
grep -rhoE --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.ss --exclude-dir=.claude \
  'slopstopper-workaround: hungovercoders/slopstopper#[0-9]+' . \
  | grep -oE '[0-9]+$' | sort -un | while read -r n; do
    printf '#%s  %s\n' "$n" "$(gh issue view "$n" -R hungovercoders/slopstopper --json state,title,closedAt \
      --jq '"\(.state)  \(.title)  \(.closedAt // "")"' 2>/dev/null || echo 'UNKNOWN  gh could not reach hungovercoders/slopstopper')"
  done
```

Then, per issue:

- **CLOSED.** First check the fix is in the version this repo runs: the CLI pin in `mise.toml` must be a release cut after `closedAt` (the slopstopper changelog names the issue), and for a workflow or installer bug the refresh you just ran is what carries it. If the pin is behind, move it (`install.sh --upgrade-cli`, above) before touching the workaround. Then `grep -rn 'slopstopper#<N>'` for every marker, remove the **workaround** (the disabled workflow, the exclude glob, the raised threshold, the `nosemgrep`), not just the comment, and re-run the check it was protecting (`task ss:<category>:<check>`). Green: keep the removal in the refresh commit, one line in its message per issue. Still red: the fix did not cover this repo's case. Put the workaround back, comment on the closed issue with the reproduction, and keep the marker pointing at it.
- **OPEN.** Leave it. List it in the refresh summary to the user ("still working around #N: <title>") so the cost stays visible.
- **UNKNOWN.** `gh` could not reach the repo. Leave every workaround in place and say so; never remove one on a guess.

`--exclude-dir=.claude` keeps the shipped skills out of it, since `upstream.md` spells the convention out. An unmarked workaround is invisible to this step. If you find one while reading the diff (a `workflows.disabled` entry or a threshold with a `# why` that describes a slopstopper fault rather than a repo decision), file the issue through `slopstopper-triage`'s `upstream.md` and add the marker, so the next refresh can retire it.
