# Steps 4 and 6: Post-install configuration and badges

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 4: Post-install configuration

The installer seeds a `.slopstopper.yml` config file at the repo root (if one doesn't exist yet) plus sensible defaults for `.github/labeler.yml`, `.github/dependency-review-config.yml` (the licence allowlist the dependency review enforces), `public/_headers` (commented baseline), `.zap/rules.tsv` (common false positives commented), and appends a `.gitignore` block for `.ss/reports/`, `node_modules/` and friends. None of those overwrite existing files, except that the `.gitignore` block between its markers is refreshed on every run. The rest of post-install is editing `.slopstopper.yml` to point the dynamic checks at the right URLs and tuning a few knobs.

### Confirm the profile

```bash
slopstopper profile show        # active profile + every workflow this repo doesn't carry
slopstopper profile detect      # what the repo's contents suggest (advisory)
```

If these disagree, decide which is right and edit `profile:` in `.slopstopper.yml`, then **re-run `install.sh`**, because the key drives the workflow set but only the installer adds or removes the files. Switching is reversible in both directions: `ui → api` deletes the nine browser checks, `api → ui` restores them (profile-dropped workflows are deliberately kept out of `.ss/.workflows-installed`, so the deletion-respect rule doesn't suppress them forever).

### Configure the API checks (if the target serves an API)

All four API checks ship inert and **stay inert until configured**. They exit 0 with a note, so they'll never fail a PR on their own, and they'll also never catch anything. If the target has an API, wire them up here; it's the single highest-value post-install step on an `api`-profile repo. Under `ui` the installer leaves them out when it finds no spec, server framework or `api.*` key, so after setting `api.*` here, re-run `install.sh` to add the workflows:

```yaml
# .slopstopper.yml
api:
  base_path: ''             # prefix the API is served under, e.g. /api/v1
  health:
    path: /health           # unset → reliability:api-health skips
    require_fields: [status]
    expect_fields:
      status: ok
  latency:
    paths: [/health, /v1/items]   # empty → reliability:api-latency skips
    samples: 5
    warmup: 1
    median_ms:              # leave unset at first (see below)
  headers:
    paths: [/health]        # empty → security:api-headers skips
    allowed_origins: [https://app.example.com]
  openapi:
    spec: openapi.json      # unset → security:dast falls back to the site
                            # spider (which finds nothing on an API) AND
                            # hygiene:openapi skips
    served_spec: https://api.example.com/openapi.json
```

Things to get right with the user:

- **`expect_fields` is the point.** Without it, `{"status": "degraded"}` behind an HTTP 200 passes. Ask what the endpoint returns when it's genuinely healthy and encode that.
- **Ship `api.latency` with no budgets.** Set `paths` now and leave `median_ms` / `slowest_ms` / `max_bytes` unset. The check reports the numbers and enforces nothing; after a week of reports the user can set a budget from what they actually saw. A guessed budget on a shared runner is noise, and a check people learn to ignore is worse than no check. Budgets gate on the **median**, so one slow sample won't fail a PR.
- **`api.openapi.spec` is what gives an API repo any DAST at all**, and it is the same key `hygiene:openapi` reads. It takes a URL or a repo-relative file. DAST takes YAML or JSON; **`hygiene:openapi` reads JSON only**. The CLI has no YAML parser, so it skips with that guidance rather than failing. If the spec is YAML, point at the JSON form (most frameworks serve `/openapi.json`) or accept that only DAST uses it.
- **`served_spec` is what makes `hygiene:openapi` worth having.** Without it the check only probes documented paths for reachability; with it, it compares the committed spec's operation set against the deployed one, which is the drift most worth catching and has no false positives. If the target generates its spec at build time, point `spec` at the served URL instead so it can't drift at all.
- **`allow_wildcard_cors`.** If the API is public and read-only, `Access-Control-Allow-Origin: *` is correct and the knob should be `true`. On anything credentialed, leave it `false`. Note that a wildcard sent *with* credentials fails regardless, because browsers reject that pair, so the intended policy was never being enforced.

These checks need a reachable URL and never build the target locally (an API isn't a static bundle). On PRs they audit `urls.preview`; with no preview environment the PR run skips and the deployed-main / scheduled runs carry the coverage. Tell the user, so a skipped check on their first PR doesn't surprise them.

### Edit `.slopstopper.yml`

A first install seeds a short starter with each key commented. Fill these in. A key the starter doesn't carry (`headers.format` below, the hygiene thresholds, the `api:` block) is added by copying its block from the schema reference linked at the top of the file:

```yaml
headers:
  source: public/_headers       # or worker/headers.json, or null to skip the check
  format: cloudflare-text       # or json, meta (source = built HTML root, for a <meta> CSP), or auto

urls:
  production: https://your-site.example.com
  preview:    https://staging.your-site.example.com

pages:
  smoke:         /,/about,/pricing
  e2e:           /,/pricing       # start paths for the user-journey walk
  accessibility: /,/about
  seo:           /

e2e:
  max_links: 25                 # primary-nav links followed from each start path

smoke:
  og_image_path: /og-image.png  # set to '' if you use per-post share images

workflows:
  disabled: []                  # list any ss-*.yml workflows to remove on next install.sh
```

### Optional: tune the hygiene thresholds

Every hygiene check reads its own thresholds from `.slopstopper.yml`, falling back to a sensible default when unset. None are in the starter, so add the ones you need. You don't need to touch these to get started, but if a check fires for a reason that's actually fine (e.g. your repo intentionally has 30 docs pages, or a topic doc is a reference table that cannot split), tune the cap rather than dropping content. The entry-file budgets are the exception: an `AGENTS.md` past ~2,000 tokens is where agents start dropping rules, so route the overflow rather than raising it. Don't tune to silence noise; tune to match a deliberate design decision.

If `docs/` also holds content that isn't agent or contributor documentation (a course, tutorials rendered onto a site), exclude it rather than raising the caps: `hygiene.docs.exclude` takes it out of all three docs checks.

```yaml
hygiene:
  docs:
    exclude: [docs/archive/**]   # (default) skipped by docs-size, docs-structure, docs-accuracy; setting it replaces the default
  docs_size:
    max_total_size_kb: 150   # (default) total .md under docs/ (excl. hygiene.docs.exclude)
    max_file_size_kb: 20     # (default) largest single doc
    max_files: 25            # (default) total doc count
  entry_files:
    max_tokens: 2000         # (default) AGENTS.md (estimated tokens, chars/4)
    readme_max_tokens: 600   # (default) README.md, badges excluded
    map_max_tokens: 1000     # (default) docs/README.md, the map
    require_explicit_routes: true   # (default) every .md link in AGENTS.md says when to read it
    require_claude_include: true    # (default) CLAUDE.md is exactly `@AGENTS.md`
  complexity:
    max_ccn: 15              # (default) CCN ceiling; a function above this fails
    exclude: []              # extra globs to skip; test files and gitignored paths already are
  docs_accuracy:
    extra_paths: []          # (default) globs outside docs/ to scan too, e.g. [app/*.html, .claude/skills/**/*.md]
    ignore_refs: []          # (default) backtick paths never on disk: run-time or generated files, e.g. [.apic/*]
  docs_structure:
    require_routed_docs: true   # (default) every doc has an explicit route from AGENTS.md, the map or a README above it
    max_route_depth: 3          # (default) AGENTS.md → map → directory README → doc
    max_doc_lines: 300          # (default) per topic doc; 0 disables
security:
  sast:
    fail_on: error           # (default) lowest Semgrep severity that fails (error | warning | info | none)
```

`hygiene.complexity.max_ccn` gates locally, in the pre-push hook, and in CI off one exit code (there is no separate CI-only threshold), so `task ss:hygiene:complexity` reproduces the CI result exactly. Drop it to `10` for McCabe-strict.

Larger sites should also opt into `reliability.coverage.*` modes so accessibility/SEO/broken-links audit the whole sitemap on main and only changed pages on PRs. See [`.slopstopper.yml.example`](https://github.com/hungovercoders/slopstopper/blob/main/.slopstopper.yml.example) for the schema reference and resolution order.

### Set the Node version (if not 20)

Node is pinned in `mise.toml` (`[tools] node`), which both mise locally and CI (via `jdx/mise-action`) read, with no `setup-node` step or repo variable. `install.sh` seeds `node = "20"` only when the repo doesn't already declare one. To use a different version:

```bash
mise use node@22
```

### URLs: GitHub repo variables vs. hardcoded vs. inert

`.slopstopper.yml` `urls.*` is the recommended path because it survives `install.sh` re-runs. For per-environment overrides without editing the file, GitHub repo variables (`SMOKE_TEST_URL`, `ACCESSIBILITY_TEST_URL`, `LIGHTHOUSE_URL`, `SEO_TEST_URL`, `BROKEN_LINKS_TEST_URL`, `DAST_TEST_URL`) take precedence and live in repo settings.

Hardcoding inside `ss-reliability-*.yml` workflow files still works but gets wiped on `install.sh` re-run, so avoid it unless you have a reason.

## Step 6: Surface what's installed via README badges

After the workflows are live, surface them in the target's README. One GitHub Actions badge per `ss-*.yml` workflow plus a "powered by slopstopper" advert badge gives anyone landing on the repo an instant sense of what's being checked and that the gates are real.

Generate the block via the CLI:

```bash
slopstopper badges                     # preview to stdout
slopstopper badges > badges.md         # write to a file for review
slopstopper badges --no-advert         # skip the powered-by badge
```

The command:

- Scans `.github/workflows/ss-*.yml` so it only badges workflows actually installed (deletions tracked via `.ss/.workflows-installed` are respected).
- Detects `OWNER/REPO` from `$GITHUB_REPOSITORY` (in CI) or `git remote get-url origin` (locally). Pass `--owner X --repo Y` to override (useful for a freshly-init'd repo with no remote yet).
- Groups badges by loop (Security / Hygiene / Reliability / Operational) with curated short labels (`SAST`, `Dependency CVEs`, `Core Web Vitals`, etc.) and always adds `?branch=main` so PR-run failures don't make the badge flicker red on `main`.
- Includes the static shields.io "powered by slopstopper" advert at the top unless `--no-advert` is passed (no live status, just an advert, which keeps it portable).

**Then paste the block into the README**, at the right insertion point. Most READMEs have a "what this is" intro at the top; the Pipeline status block reads best immediately after that, before the install/usage section, so visitors see what's guarded before they read what it is.
