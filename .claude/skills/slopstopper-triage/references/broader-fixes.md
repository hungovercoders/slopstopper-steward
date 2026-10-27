# Steps 6–7: When the fix is broader than one check, and when to delete the check

> Part of the `slopstopper-triage` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 6: When the fix is broader than one check

If multiple checks fail with the same root cause, batch-fix once and re-run all affected checks instead of fixing each in isolation. Common patterns:

- **Node version pin bump** → fixes Core Web Vitals + Accessibility + SEO + Playwright + Smoke + DAST in one edit.
- **Adding `public/_headers` with a security-headers baseline** → fixes DAST's missing-header alerts AND the smoke spec's CORP header check.
- **Setting up the AGENTS.md-first docs layout** (`AGENTS.md` under ~2k tokens with explicit "before you X, read Y" routes and a catch-all to `docs/README.md`; `CLAUDE.md` exactly `@AGENTS.md`; every doc under `docs/` routed) → fixes docs-structure + docs-size + docs-accuracy + entry-files together. The `entry-files` report ships a paste-ready fix per rule; the `docs-structure` report names every unrouted doc and the README that should route it. `slopstopper-install` Step 5 has the decide-first recipe (routes only / build / fan out) and the pre-0.15 migration.
- **Moving the `slopstopper-cli` pin** → if a default tuning was widened upstream, multiple checks go green at once. The CLI is pinned in `mise.toml` (`"pipx:slopstopper-cli"`); bump it with `install.sh --upgrade-cli` (rewrites the pin via `mise use` + installs + commit so CI matches), then re-run the aggregates. Don't install slopstopper-cli globally by hand, because that drifts off the committed pin without updating CI.

The aggregates make this fast: `task ss:hygiene:test` re-runs every hygiene check; `task ss:security:scan` re-runs every security check. Both are just sequenced `slopstopper run …` calls under the hood, so adopters who skip the Taskfile can chain the CLI invocations directly.

## Step 7: When to delete the check instead of fixing it

**First ask whether it's a whole class, not one check.** If a repo is an API or a library, the browser-and-SEO checks don't belong there as a group, which is what `profile:` is for (`slopstopper profile list`). It is the maintained answer because the profile survives a re-run, is visible in review, and reverses cleanly if the repo grows a web surface later. Reach for deletion only for a check that a profile doesn't cover.

A small number of checks are slopstopper.dev-specific and have no place on a target that doesn't share the pattern they guard. Delete them, or list them under `workflows.disabled` in `.slopstopper.yml` (the config-driven form is easier to audit and survives a clone-and-rebuild). Either way `.ss/.workflows-installed` remembers the deletion, so re-running `install.sh` won't bring them back.

Deletable on legitimate grounds:

- **`ss-hygiene-csp-exceptions-check.yml`** guards `worker/headers.json`, which only slopstopper.dev uses. (Already dropped by `profile: library`; and harmless under any profile while `headers.source` is null.)
- **`ss-hygiene-docs-accuracy-check.yml` + `docs-size-check.yml` + `docs-structure-check.yml` + `entry-files-check.yml`**, only if the target genuinely won't keep an `AGENTS.md` and routed docs. If you're keeping docs at all, set up the layout instead (see `slopstopper-install` Step 5). On a repo whose entry file already fits, that is a few route lines.
- **`ss-security-vulnerability-new-check.yml`**, if Dependency Graph / GHAS can't be enabled.
- **`ss-security-dast-check.yml`**, only if nothing is deployed at all. On an API it works via `api.openapi.spec`; deleting it because "DAST is for websites" removes the repo's only dynamic security scanning.
- **The four API workflows** (`ss-reliability-api-health-check.yml`, `ss-reliability-api-latency-check.yml`, `ss-security-api-headers-check.yml`, `ss-hygiene-openapi-check.yml`), only if the repo serves no API at all. They're already inert unconfigured, so deleting them is tidiness rather than necessity; `profile: library` drops all four as part of a coherent set, which is the better lever.

Don't delete a check just because it's failing. The bar is *"this check has nothing meaningful to guard on this target"*, not *"this check is inconvenient"*.
