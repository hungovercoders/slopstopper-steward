# Steps 2–3: Run the installer, then read what landed

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 2: Run the installer

Before running anything, ensure you're on a clean branch:

```bash
git status                              # must show clean
git checkout -b chore/slopstopper-install   # or chore/slopstopper-refresh for a refresh
```

`install.sh` adds up to 30 workflow files, the three composite actions, a `Taskfile.ss.yml` and a `.slopstopper.yml` to the repo in one shot. On `main` that's an awkward 30+-file commit; on a dedicated branch the diff is reviewable and the rollback is `git checkout main && git branch -D <branch>`. If `git status` is dirty, stop and reconcile first, because the installer doesn't ask before writing into `ss-*.yml` or `Taskfile.ss.yml`. On a refresh, the wipe-and-replace behaviour will clobber anything sitting in the tracked files it rewrites, so commit or stash first.

From the target repo root, download-review-run in two steps:

```bash
curl -fsSL https://raw.githubusercontent.com/hungovercoders/slopstopper/main/install.sh -o install.sh
bash install.sh                                # default Task mode, `ui` profile
bash install.sh --profile api                  # or the profile you agreed above
```

**Lead with the two-step form when you're an agent.** The piped one-liner
(`curl … | bash`) is what the human-facing README advertises, but Claude Code's
auto-mode classifier (and most agent security sandboxes) reject piping external
code straight into `bash`, so it stalls on a permission prompt mid-install. The
two-step form lands the script on disk first, where it can be reviewed, and it
clears the sandbox. The piped form remains the canonical convenience command for humans:

```bash
curl -fsSL https://raw.githubusercontent.com/hungovercoders/slopstopper/main/install.sh | bash
```

`install.sh` pins `slopstopper-cli` in `mise.toml` (`[tools]` "pipx:slopstopper-cli"), installs that version through `mise use`, and hands over to `slopstopper install`, which writes the suite from the files that version of the CLI ships. So the shims and workflows a repo gets are the pinned version's, never main's, and a check the CLI does not know can never land. The installer is idempotent: re-running it refreshes slopstopper-owned files but respects deletions (`.ss/.workflows-installed` lists every workflow it has placed; one missing from disk stays missing until its line is removed). It does **not** bump `slopstopper-cli`: a plain re-run honours the pin, so a breaking upstream release can't reach the repo until someone moves it with `install.sh --upgrade-cli` (latest) or `install.sh --cli-version X.Y.Z` (exact). With the CLI already installed, `slopstopper install [--profile …]` alone writes the suite (and seeds the pin with its own version when the repo has none); run it as `mise exec -- slopstopper install` so it is the pinned version writing, and read the banner's warning if the two differ. An older install that pinned `cli_version` in `.slopstopper.yml` has the value migrated into `mise.toml` and the dead key stripped on the next run; a dead `node_version` key (never read, since node lives in `mise.toml`) is likewise stripped. All slopstopper files live under the `ss` namespace, so your repo's existing files are not touched. If a pre-CLI install left a `.ss/scripts/` directory behind, the installer scrubs it on first run.

**`install.sh` also installs the SlopStopper Claude Code skills at project level**, under `<target>/.claude/skills/slopstopper-{install,triage}/` (each a `SKILL.md` map plus its `references/`), so every contributor that clones the repo picks them up automatically (Claude Code auto-discovers project-level skills). Commit the resulting files alongside the workflows. Pass `--no-skills` or set `SLOPSTOPPER_NO_SKILLS=1` to disable. To refresh just the skills later without re-running the whole installer, use `install-skill.sh` from inside the repo. If you've previously run an older version of `install-skill.sh` that wrote to `~/.claude/skills/slopstopper-*`, the installer warns you so you can clean up the user-level copies before they shadow the project-level ones.

### What `install.sh` writes (and leaves alone)

Three categories of write, in order of "how much trust to extend on re-run":

**Always overwritten (slopstopper-owned, safe to clobber):**

- `Taskfile.ss.yml`, `.ss/.workflows-installed`, `.ss/.installed-from` (the `slopstopper-cli` version that wrote this install; the baseline for the refresh diff), `.github/actions/ss-*/`
- Workflows under `.github/workflows/ss-*.yml` that are in `GENERIC_WORKFLOWS` and not listed in `.slopstopper.yml` `workflows.disabled`
- `mise.toml`, the `"pipx:slopstopper-cli"` + `task` + `node` pins (written via `mise use`); `slopstopper-cli` itself is installed/activated by mise at the **pinned** version. A plain re-run never bumps the CLI; `node` is seeded (`= "20"`) only when the repo doesn't already declare one; `--upgrade-cli` / `--cli-version` move the CLI pin
- `<repo>/.claude/skills/slopstopper-install/` and `<repo>/.claude/skills/slopstopper-triage/`, each `SKILL.md` plus `references/`, replaced as a whole (at project level, with `--no-skills` to opt out)
- `.githooks/pre-push`, the pre-push hygiene gate (opt out with `--no-hooks`). The *file* is always refreshed; the `core.hooksPath` **wiring** is only set when safe (the next list defines it), so it's left alone if the adopter runs their own hook manager

**Seeded only if missing (adopter-owned, NEVER overwritten on re-run):**

- `.slopstopper.yml` (config; a first install seeds a short starter from `templates/slopstopper.yml.starter` with `profile`, URLs, page lists, og-image path, `headers.source` and `workflows`, and it links the full schema reference for every other key. Once it exists `install.sh` never touches it, **except** to strip a legacy `cli_version` pin line (value migrated into `mise.toml` once) and a dead `node_version` key (just removed, since node lives in `mise.toml`)). The CLI + node pins now live in `mise.toml`, which the installer writes on first install and rewrites only when you pass `--upgrade-cli` / `--cli-version`
- `.github/labeler.yml`, `.github/dependency-review-config.yml` (the licence allowlist), `.zap/rules.tsv`, `.markdownlint.json`
- Root `Taskfile.yml`, only if absent; otherwise install.sh prints the `includes:` block to paste in
- `package.json`, only if absent (otherwise see below): a minimal manifest named after the repo, `private: true`, slopstopper's devDependencies and **no** `build` script. The browser-check workflows skip the build when there is none and serve the repo as-is, so add `scripts.build` if the site needs building first
- AGENTS.md-first entry files (`README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/README.md`), seeded from `cli/slopstopper/data/templates/entry-files/` when absent and never overwritten. If they exist but break a rule (budget, soft route, `CLAUDE.md` not the pure include, no route to the map), the `ss:hygiene:entry-files` check fails with a paste-ready fix in its report, which you apply manually rather than re-seeding. A pre-0.15 `docs/index.md` is never touched; the installer warns and the check's report carries the rename

**Conservatively additive on shared files (adopter content preserved):**

- `package.json` devDeps merge: adds missing keys; existing keys are kept on version conflict (warning printed, not error).
- `.gitignore`: the `# slopstopper begin` / `# slopstopper end` block is slopstopper-owned. The first install appends it, and every re-run replaces what's between the markers with the current template, so new ignore lines (`.ss/.run/`, `node_modules/`) reach existing installs. Lines outside the markers are never edited. A begin marker with no end marker is left alone with a warning.
- `public/_headers` (only if `public/` exists): appends a commented-out security-headers baseline inside `# slopstopper security headers begin/end` markers. Same idempotent skip on re-run, and a file that already sets a security header (an uncommented `Content-Security-Policy`, `Strict-Transport-Security` or `X-Content-Type-Options` line) is left byte-identical.
- `core.hooksPath` (git config, not a file): set to `.githooks` **only** when the adopter has no custom `core.hooksPath` and no other hook manager (`.husky/`, `lefthook.yml`/`.yaml`, `.pre-commit-config.yaml`). If they already manage hooks, the installer drops `.githooks/pre-push` in but leaves their config alone and prints a one-line opt-in. `--no-hooks` / `SLOPSTOPPER_NO_HOOKS=1` skips the whole step.

**Everything else is left alone.** That covers source code, build outputs, custom workflows under `.github/workflows/` outside the `ss-*` namespace, generic configs (`.eslintrc`, `tsconfig.json`, etc.), and anything under `app/`, `src/`, `worker/`, etc.

**One risk worth flagging:** inline customisations to `ss-*.yml` workflow body content (bespoke `gh issue create` blocks, extra steps, custom env vars beyond what `.slopstopper.yml` covers) get wiped on the next `install.sh` re-run because workflow files are wholesale-replaced. Push bespoke wording into the check's META in `slopstopper-cli` upstream rather than hand-editing the YAML locally. See the **Refresh-only** section below for how to diff and re-apply on refresh.

By default the installer ships **Task-driven workflows**, where every check runs via `task ss:<category>:<check>` so the suite shares one invocation surface with `task build`, `task deploy`, etc. The workflows install the toolchain via `jdx/mise-action`, which reads `mise.toml` and installs both the pinned `slopstopper-cli` and `task`, so adopters don't need either in their CI runners by hand. If the adopter explicitly doesn't want Task in their CI, install with `--no-task`:

```bash
curl -fsSL https://raw.githubusercontent.com/hungovercoders/slopstopper/main/install.sh | bash -s -- --no-task
```

`--no-task` post-processes each shipped workflow at install time to rewrite `task ss:<X>` lines to `slopstopper run <X>`, which is the same execution path without the Task layer. (The `jdx/mise-action` step stays; mise still installs the pinned CLI, and `task` simply goes unused.) Default mode is the right choice for most adopters; `--no-task` is the escape hatch.

Full two-step variants (all flags, optional explicit target dir):
```bash
curl -fsSL https://raw.githubusercontent.com/hungovercoders/slopstopper/main/install.sh -o install.sh
bash install.sh [TARGET_DIR]                   # default Task mode
bash install.sh --no-task [TARGET_DIR]         # CLI-direct mode
bash install.sh --no-hooks [TARGET_DIR]        # skip the pre-push hygiene hook
```

## Step 3: What just landed

Sanity-check the install dropped what you expect:

- `slopstopper-cli`, installed and activated by **mise** and **pinned in `mise.toml`** (`[tools]` "pipx:slopstopper-cli"). Confirm with `slopstopper --version` (must equal the pin; ensure mise is activated in your shell). When the pin moved but the installing shell still resolves the old binary, the banner says so; re-enter the directory or use `mise exec -- slopstopper …`. Every check runs through this. First install records the latest published version as the pin; it never moves on a plain re-run.
- `mise.toml`, the per-repo toolchain pins (`"pipx:slopstopper-cli"` + `task` + `node`). Both `install.sh` (local) and the `ss-*.yml` workflows (`jdx/mise-action`) read it, so local and CI run the same versions, node included, with no separate `setup-node` step. Commit it. Move the CLI pin deliberately with `install.sh --upgrade-cli` or `install.sh --cli-version X.Y.Z` (both wrap `mise use`). A legacy `cli_version` in `.slopstopper.yml` is migrated here and removed on the next run; a dead `node_version` key is removed too.
- `Taskfile.ss.yml`, thin `task ss:*` shims that each call `slopstopper run <category>:<check>` under the covers. They suit adopters who already drive their dev loop with `task`.
- `Taskfile.yml`, created if missing (otherwise it needs the manual `includes:` block per Step 1.1).
- `.ss/.workflows-installed`, the manifest of what the installer put in place, which is how re-runs respect a deliberate deletion (commit this). With `.ss/.installed-from` below, the only files the installer writes into `.ss/`. The static server for the local loop is not a file in the repo, because it ships inside the wheel as `slopstopper serve` (eject with `slopstopper templates eject server.js` only if you need to customise it).
- `.ss/.installed-from`, the `slopstopper-cli` version that wrote this install; a refresh diffs the schema between its tags (commit this).
- `.slopstopper.yml`, which on a first install is a short starter (the keys most repos set, each commented) with a link to the full schema reference for everything else. Existing configs are left as they are.
- `.github/workflows/ss-*.yml`, the curated installer set, minus whatever the profile drops (`ui`: 23 checks while the repo shows no API, 27 once it does; `api`: 17; `library`: 11; `slopstopper profile list` prints the live set and what each profile holds back or disables), plus `ss-pr-summary.yml`, `ss-workflow-failure-issue.yml` and the doc-updater under all three, and `ss-cloudflare-deployed.yml` in a repo with wrangler config (at the root or one directory down) or `worker/` (Workers Builds raises no GitHub deployment, so it runs the URL-driven checks against each preview and, on the default branch, `urls.production`). Each workflow body is now ~8 lines: `uses: ./.github/actions/ss-setup` (Python + mise toolchain + CLI), `slopstopper run …`, `slopstopper emit … --target pr-comment|issue`.
- Every third-party action in those workflows and actions is pinned to a commit (`uses: actions/checkout@<sha> # v6`). slopstopper's own Dependabot keeps its copies current and a refresh copies the new pins in, so the adopter repo needs no Dependabot config for them, but pins only move when you refresh.
- `.github/actions/ss-setup/`, `.github/actions/ss-resolve-url/` and `.github/actions/ss-playwright/`, the composite steps those workflows call. They are local actions, so nothing is published or version-pinned and everything a workflow does is diffable in the adopter's own tree. `ss-resolve-url` is where the event → URL table lives (`urls.preview` on PRs for the API checks; the browser checks build and serve locally). `ss-playwright` installs Chromium for the four browser checks, cached per Playwright version so a warm run skips the download.
- `ss-pr-summary.yml`, which posts **one** rolling comment per PR summarising every check (`❌ SlopStopper: 2 of 27 checks failed`, failures in a table, the rest folded). The per-check workflows post compact comments (a verdict line, the failing items, the report folded away) and **delete their comment when they pass**, so a green PR carries only the summary. Tell the user this up front: the first green PR looking "empty" of bot comments is the intended behaviour, not a broken install.
- The **🏷 Repo topic** line. When `gh` can reach the repo the installer adds the `slopstopper` repository topic (idempotent; `--no-topic` or `SLOPSTOPPER_NO_TOPIC=1` skips it), so adopter repos can be found as a set with `gh search repos --topic slopstopper`. If the line says gh couldn't reach the repo, run the `gh repo edit --add-topic slopstopper` it prints once the remote exists. Issues and PRs the suite raises (check failures on `main`, the workflow-failure tracker, the doc updater) carry the `slopstopper` label for the same reason; the label is created on first use, so there is nothing to seed.
- If the banner shows **⛔ GitHub has switched off N slopstopper workflow(s)**, GitHub disabled them for 60 days without repo activity and they run on no trigger, PRs included. Each line carries its `gh workflow enable <id>`; ask the user, then run them. `slopstopper doctor` re-checks later. The banner skips this silently when `gh` can't reach the repo.
- `.slopstopper.yml` `profile:`, the key the installer wrote (or left alone). Confirm the installed set matches it with `slopstopper profile show`: it prints the active profile, where it came from, and every workflow this repo is deliberately not carrying. Run it before the missing-workflow comparison below, because a workflow the profile dropped is *supposed* to be absent and will otherwise read as a gap.
- `package.json`, with devDeps merged.
- `.claude/skills/slopstopper-install/` + `.claude/skills/slopstopper-triage/` (each `SKILL.md` + `references/`), the project-level Claude Code playbooks. Claude Code auto-discovers them for any contributor in this repo. Commit them.
- `.githooks/pre-push`, the pre-push hygiene gate (runs `task ss:hygiene:test` before every push). Commit it. On a normal install the installer also sets `git config core.hooksPath .githooks`; confirm with `git config --get core.hooksPath`. If the adopter already runs husky/lefthook/pre-commit (or a custom hooksPath), the file lands but the wiring is skipped with an opt-in note (run `git config core.hooksPath .githooks`, or add `task ss:hygiene:test` to their own manager). `--no-hooks` skips it entirely.

**What's NOT there any more** (if you're updating from a pre-CLI install): `.ss/scripts/` (every Python/bash script lives in `slopstopper-cli`); and `.ss/playwright.config.js`, `.ss/lighthouserc.json`, `.ss/lighthouserc.prod.json`, `.ss/tests/` (now bundled in the wheel, so the installer scrubs unmodified byte-equal copies on re-run but leaves customized files alone since they'll override via the CLI's templates resolver).

### Customising a Playwright spec or lighthouserc

The CLI looks for `.ss/<filename>` first and falls back to the wheel's bundled copy. Use `slopstopper templates` to list, locate, and eject:

```bash
slopstopper templates list                       # see what's bundled + what's already ejected
slopstopper templates path lighthouserc.json     # print the resolved path (override-or-bundled)
slopstopper templates eject lighthouserc.json    # copy bundled → .ss/lighthouserc.json
$EDITOR .ss/lighthouserc.json
```

The CLI picks up the override on the next run. Caveat: customizations don't auto-merge with upstream changes, because you own the file once you eject. `slopstopper templates eject` won't overwrite an existing `.ss/<filename>`, and the installer's byte-equality scrub leaves customized files alone.

**Confirm the installed set is the pinned version's.** The workflows come from the wheel of the `slopstopper-cli` in `mise.toml`, so what landed is exactly that release's set minus the profile's drops and the adopter's deletions; a newer release's checks arrive when the pin moves, not before. To see what the pin leaves out, compare against the release's list and subtract the profile's set:

```bash
# inside the target repo, after install
comm -23 \
  <(slopstopper checks list --json | jq -r '.[].workflow' | sort) \
  <(ls .github/workflows/ | grep '^ss-' | sort)
```

Any line in the output is a check workflow the pinned CLI knows that isn't on disk: either the profile drops it (`slopstopper profile expand <name>` lists those), or it was deleted and `.ss/.workflows-installed` still records it, which is a choice to confirm with the user rather than undo. Nothing here is a gap to fill by hand: a workflow that only a newer release ships is the `--upgrade-cli` conversation.

The installer's stdout summarises what's active vs what needs config. Read it and relay it to the user.
