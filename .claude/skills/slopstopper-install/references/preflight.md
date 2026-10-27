# Step 1: Pre-flight

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 1: Pre-flight: read the target repo before installing

**First: what shape is this repo?** Ask before anything else, because it decides which checks are even relevant. The nine browser-and-SEO reliability checks (smoke, E2E, accessibility, Core Web Vitals, SEO, broken links, llms.txt, robots.txt, sitemap) assume HTML, a DOM and a public web surface. On an HTTP API they don't no-op. They build and audit nothing and go **red**. Pick a profile up front:

| Shape | Profile | Install with | Gets |
| ----- | ------- | ------------ | ---- |
| Serves HTML to a browser | `ui` (default) | `bash install.sh` | everything (27 checks; 23 until the repo has an API) |
| JSON/gRPC API, no browser surface | `api` | `bash install.sh --profile api` | the static layer, the four API checks (`api-health`, `api-latency`, `api-headers`, `openapi`) and DAST via ZAP's OpenAPI mode; no browser checks (17 checks) |
| Library, CLI or package, nothing deployed | `library` | `bash install.sh --profile library` | the static layer only (11 checks) |

The four API checks (`reliability:api-health`, `reliability:api-latency`, `security:api-headers`, `hygiene:openapi`) ship under `api` always, and under `ui` only when the repo has an API: an OpenAPI/Swagger spec or proto file, a server-framework dependency, or an API check configured under `api.*` in `.slopstopper.yml`. A UI repo with API routes (Next.js handlers, Astro endpoints) gets them without having to find `workflows.enabled`; a static site doesn't run four jobs that check nothing. They stay inert until `api.health.path` / `api.latency.paths` / `api.headers.paths` / `api.openapi.spec` are set. Configuring them is Step 4, and a `ui` repo that had none needs `install.sh` re-run afterwards to add the workflows.

Note the odd one out: `hygiene:openapi` is the only **hygiene** check that needs a URL, since drift means the spec measured against the thing it documents. That is why `library`, which drops everything URL-driven, is the one profile without a complete static layer.

`slopstopper profile detect` (or the installer's own suggestion in its post-install output) reads the repo's framework configs, HTML, OpenAPI specs and server-framework deps and proposes one. Treat it as a prompt to confirm with the user, not an answer. When in doubt pick the wider profile: a check that runs and goes red is visible; one that's silently off is not. `slopstopper profile list` prints the exact workflow set each one drops, and `workflows.enabled` in `.slopstopper.yml` takes an individual check back (an API that also serves a docs site and wants `broken-links`, say).

A mixed repo (an API *and* its marketing site in one tree) is a `ui` repo. Keep the full set and point the dynamic checks at the site.

Then, whatever the shape, learn enough about the target to predict where it'll bite you:

0. **Are `mise` and `python3` available?** `install.sh` errors out without them. mise is the toolchain manager: it pins `slopstopper-cli` (and `task`) in `mise.toml` and installs them, activating the pinned versions per-directory so the active `slopstopper` follows the repo (this is what stops a single global binary from drifting between repos). mise's pipx backend needs `python3` on PATH. The CLI runs every check, so no mise/Python = no slopstopper. Make sure mise is [activated](https://mise.jdx.dev/getting-started.html) in the shell so the pinned binary lands on `$PATH` (CI handles this via `jdx/mise-action`).

1. **Does the target have an existing `Taskfile.yml`?** If yes, the installer prints include instructions instead of overwriting, so you (or the user) need to manually add:
   ```yaml
   includes:
     ss:
       taskfile: ./Taskfile.ss.yml
   ```
   to the existing Taskfile.

2. **Does the target already have GitHub Actions workflows?** Slopstopper adds up to 30 new `ss-*.yml` workflows (31 on a Cloudflare repo, which also gets `ss-cloudflare-deployed.yml`). That is 27 checks on `ui` (23 until the repo shows an API, since the four API checks are held back; 17 under `--profile api`, 11 under `--profile library`) plus three that ship under every profile: `ss-pr-summary.yml` (the single status comment), `ss-workflow-failure-issue.yml` (raises an issue when a check fails on main) and the doc-updater. They're all `ss-`-prefixed so they group in the Actions UI, but the user should know they're getting that many checks running on every PR.

3. **What `engines.node` does the target need?** Node is pinned in `mise.toml` (`[tools] node`), so mise installs it locally and the workflows get the same version from that pin via `jdx/mise-action` (no `setup-node` step, no repo variable). `install.sh` seeds `node = "20"` on first install and leaves any existing node pin / `.node-version` / `.nvmrc` alone. If the target needs Node 22+ (Astro 6, recent Next, SvelteKit), run `mise use node@22`. That pin is the one source of truth and survives `install.sh` re-runs.

4. **What's the deploy model and serve story?** Reliability/DAST workflows can target either a deployed URL or a local build served on port 8080 by `slopstopper serve` (a static server bundled in the CLI). If the target is anything other than a static site (Astro, Next, SvelteKit, a backend app) you'll need either to replace the workflow's build-and-serve steps with the app's own start command or to point the workflows at a deployed environment. Pair this with the deploy model, since Cloudflare Workers / Vercel / Netlify / GH Pages each call for a different answer.

5. **How does the target manage security headers?** The CSP-exceptions drift check reads from whatever you name in `.slopstopper.yml` `headers.source` (with `headers.format`). Shipped adapters: `json` (for `[{for, values}]` JSON files like slopstopper.dev's `worker/headers.json`), `cloudflare-text` (Cloudflare/Netlify native `_headers` text format), `meta` (the built HTML root, for a `<meta>` CSP), `auto` (a directory → `meta`, `.json` → `json`, anything else → `cloudflare-text`). Adopters managing headers via framework middleware / `vercel.json` / etc. set `source: null` to skip the check entirely. The installer seeds `.slopstopper.yml` with `source: null` so first-PR is green even before you configure anything; you opt the check in by pointing it at your real headers file. A host that can't send headers at all (GitHub Pages, S3 without CloudFront) puts its CSP in a `<meta>` tag and sets `format: meta` with `source` at the built HTML root (e.g. `dist`): CI builds first, and an unbuilt root skips locally; `docs/security/DAST.md` (Static hosts) lists the three header-only DAST alerts to accept by alert ref in `.zap/rules.tsv`.

6. **Does the target's docs layout fit AGENTS.md-first?** `ss-hygiene-docs-structure-check.yml`, `ss-hygiene-docs-accuracy-check.yml` and `ss-hygiene-docs-size-check.yml` expect a `docs/` directory whose map is `docs/README.md` and in which every doc has an explicit "when you X, read Y" route from `AGENTS.md`, the map or a README above it. `ss-hygiene-entry-files-check.yml` enforces the entry-file side: `AGENTS.md` under ~2,000 estimated tokens with every `.md` link an explicit route and one reaching the map, `README.md` under 600 (badges excluded) and linking the map, `CLAUDE.md` exactly `@AGENTS.md`. Two valid choices: set up the layout (see Step 5, which is often just adding routes to an entry file that already fits) or disable the lot (`workflows.disabled` the four docs workflows). A repo on the pre-0.15 Map Pattern (`docs/index.md`, pointer-shaped entry files) needs the migration in Step 5; its `docs/index.md` is reported as legacy until it is renamed.

7. **Does the target serve a site-wide `/og-image.png` with `Cross-Origin-Resource-Policy: cross-origin`?** The slopstopper Playwright smoke test (bundled in the CLI, or your `.ss/tests/smoke.spec.ts` if you ejected one with `slopstopper templates eject tests/smoke.spec.ts`) asserts that `/og-image.png` returns 200, has `Content-Type: image/png`, and the CORP header set to `cross-origin` (so social platforms can embed it). Targets that use per-post share images instead won't have it. Either add a 1200×630 `og-image.png` at the site root with CORP configured for that path (Astro/Cloudflare adapter respects `public/_headers`), or set `smoke.og_image_path: ''` in `.slopstopper.yml` to skip the assertion.

8. **Existing `package.json` devDeps that might collide?** Slopstopper merges in `@axe-core/playwright`, `@lhci/cli`, `@playwright/test`, `markdownlint-cli`. Spot collisions ahead of time.

9. **Does the target have its own README/AGENTS/CLAUDE entry files?** Run `task ss:hygiene:entry-files` early, because it prints the cold-start cost (tokens always loaded, the fallback hop) even when it fails, which is the number Step 5's routes-only / build / fan-out decision turns on. If any of the three are missing, `install.sh` seeds a scaffold from `cli/slopstopper/data/templates/entry-files/` (never overwrites existing files). If they exist but break a rule (over budget, a soft route, a `CLAUDE.md` that is more than `@AGENTS.md`, no route to the map), the report at `.ss/reports/entry-files/entry-file-size-report.md` emits a paste-ready fix for each; apply it during the Step 7 local loop. Most repos with existing entry files need a handful of route lines and the one-line `CLAUDE.md`; flag one that is far over budget up-front, since that is a fan-out, not a paste.

10. **Is GitHub Advanced Security (or public-repo Dependency Graph) enabled?** The `ss-security-vulnerability-new-check.yml` workflow uses `actions/dependency-review-action`, which requires either GHAS on a private repo or the Dependency Graph setting enabled on a public repo. Otherwise the check errors with `Dependency review is not supported on this repository`. Flag this repo-admin setting to the user.

11. **Does the target already have a `.github/labeler.yml`?** Slopstopper ships the auto-label workflow (`ss-hygiene-auto-label-pr.yml`) but not the config, because labels are repo-specific. Without one, the check errors with `The config file was not found`. Plan to ship a labeler config mapping the target's directory structure to labels.

12. **Is the target a private repo?** Flag two things, not one. First, some workflows post issues, comments, and PR labels, so they need `issues: write`, `pull-requests: write` permissions. That is usually fine, but check if the org restricts it. Second, and more important: **GitHub Actions minutes are free on public repos but billed on private ones.** The full suite runs up to 27 checks on every PR (23 on a `ui` repo with no API), and the scheduled reliability/smoke runs add recurring minutes on top of that. The heavier dynamic checks (Playwright, Lighthouse CI, ZAP-in-Docker) are the expensive ones. On a public repo this is a non-issue; on a private repo with a tight minutes budget, slopstopper may not be a good fit as-is. Call the cost out explicitly during pre-flight so the user decides with eyes open. Consider a partial adoption (Step 9) rather than the full suite.

Report what you found to the user before running the installer. The Node-version question and the deploy-model question together drive the largest chunk of first-PR red checks, so call them out specifically.
