# Steps 7–8: Drive every check to green locally, then push

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 7: Drive every check to green locally, **before** pushing

This is the spine of a good install. `task ss:<category>:<action>` is the canonical interface, and humans, agents and CI all go through it. The shipped workflows install Task themselves and invoke checks the same way. Running locally in a tight loop of fix and re-run is an order of magnitude faster than pushing and waiting on CI for each iteration. The goal of this step is that the first CI run on the target's PR is a **confirmation pass**, not a discovery pass.

If the target was installed with `--no-task` (rare; opt-out for adopters who don't want Task in their CI), replace every `task ss:<X>` below with `slopstopper run <X>`, which is the same code with the same exit codes and reports.

The `task ss:hygiene:test` aggregate in Pass A is also what the installed **pre-push hook** runs automatically on every push (unless installed with `--no-hooks`), so once Pass A is green the hook won't block the push. It's a gate, not a substitute for this step: the hook only runs the static hygiene subset, so you still drive the security and Pass B server/browser checks to green here by hand.

**First, confirm the CLI you'll run is the pinned one:** `slopstopper --version` must equal the `"pipx:slopstopper-cli"` pin in `mise.toml`. Straight after a pin move the current shell can still resolve the old binary (see "Move the CLI pin" in `refresh.md`); re-enter the directory or run every command below through `mise exec --`.

**Two passes, in order:**

### Pass A: Static checks (no URL, no build needed, runs in seconds)

Run the two static aggregates first. They cover every static workflow that ships with the install:

```bash
npm install                  # pulls merged devDeps once
task ss:hygiene:test         # docs-size + docs-structure + docs-accuracy + entry-files + csp-exceptions + privacy + complexity
task ss:security:scan        # SAST + secrets + dependency CVEs + DAST (DAST needs a URL; skip or run it after Pass B)
```

Or invoke each shim individually:

```bash
task ss:security:secrets           # Gitleaks (fast, usually surfaces something)
task ss:security:sast              # Semgrep
task ss:security:vulnerability:all # Trivy (CVE scan of dependencies)
task ss:security:licences          # every resolved dependency's licence against .github/dependency-review-config.yml (no tool)
task ss:hygiene:complexity         # lizard
task ss:hygiene:docs-accuracy      # repo-relative link resolver
task ss:hygiene:docs-structure     # every doc under docs/ has an explicit route
task ss:hygiene:docs-size          # docs/ size budget
task ss:hygiene:entry-files        # token budgets + explicit routes on AGENTS.md / README.md / CLAUDE.md / the map
task ss:hygiene:csp-exceptions     # if headers.source is configured
task ss:hygiene:privacy            # ui repos with hosting config: the privacy page names the host and every CSP origin
```

For each failure: fix the root cause locally, re-run **just that one check** to confirm, and only move on once green. Anticipated issues during Pass A are covered in `slopstopper-triage`.

### Pass B: Dynamic checks (need a URL + a built site)

**Under `--profile library` there is no Pass B.** Every URL-driven check is dropped, so Pass A *is* the local loop. Under `--profile api` the browser checks are gone, so Pass B is the four API checks plus DAST:

```bash
task ss:reliability:api-health  -- https://api-preview.example.com
task ss:reliability:api-latency -- https://api-preview.example.com
task ss:security:api-headers    -- https://api-preview.example.com
task ss:hygiene:openapi         -- https://api-preview.example.com
task ss:security:dast           -- https://api-preview.example.com   # needs Docker
```

`hygiene:openapi` is in Pass B despite being a hygiene check: it needs a live API, and it is the one hygiene target `task ss:hygiene:test` deliberately leaves out (that aggregate is what the pre-push hook runs, and is all-static so `git push` never hits the network).

**DAST on an API needs `api.openapi.spec` set** (Step 4). Without it the check falls back to ZAP's baseline scan, which spiders a site from a root URL. A JSON API exposes no links to crawl, so it reports almost nothing. The workflow skips rather than scanning nothing, so an unconfigured `api` repo has **no dynamic security coverage**; say that plainly and treat pointing it at a spec as the step that closes it, not an optional extra.

Run those against a **deployed** environment where you can. The CORS and HSTS headers `api-headers` audits usually come from the edge or proxy, which a local process doesn't reproduce, so a clean local run is weaker evidence than a clean run against a preview URL. And if they print `Nothing to audit, so skipping`, that's the unconfigured state, not a pass: go back to Step 4. A latency run against a preview environment is noisier and slower than production, so read the numbers and don't set a budget from them. `slopstopper profile show` tells you which of the commands below still have a workflow behind them; running a dropped check locally still works (the registry is complete regardless of profile), it just isn't gating anything in CI.

The reliability and DAST shims assert behaviour on a running site. The fastest local loop is to build once, serve it with the CLI's bundled static server on `localhost:8080`, then run each dynamic shim against `http://localhost:8080` as a bare-positional URL arg.

```bash
npm run build                                            # target's own build
slopstopper serve &                                      # bundled static server on :8080
task ss:reliability:smoke         -- http://localhost:8080
task ss:reliability:e2e           -- http://localhost:8080
task ss:reliability:accessibility -- http://localhost:8080
task ss:reliability:cwv           -- http://localhost:8080
task ss:reliability:seo           -- http://localhost:8080
task ss:reliability:llms-txt      -- http://localhost:8080    # needs an app/llms.txt
task ss:reliability:robots-txt    -- http://localhost:8080    # needs an app/robots.txt
task ss:reliability:sitemap       -- http://localhost:8080    # crawls vs app/sitemap.xml
task ss:reliability:broken-links         -- http://localhost:8080
task ss:security:dast             -- http://localhost:8080    # needs Docker for OWASP ZAP
```

`task ss:security:dast` is the heaviest local check (pulls and runs the OWASP ZAP container), so leave it for last in the loop. Skip it locally if Docker isn't installed and run it on CI only.

**Durable pattern for discovery files (`llms.txt`, `sitemap.xml`, `robots.txt`):** if the adopter's stack can emit these at build time, through a framework sitemap integration (e.g. `@astrojs/sitemap`, Next.js `app/sitemap.ts`) or a build-time endpoint that iterates their content (e.g. an Astro `src/pages/sitemap-*.xml.ts` / `src/pages/llms.txt.ts`), steer them to **generate rather than hand-author** these files. A generated file is derived from the routes, so it can't drift out of sync, and `ss:reliability:sitemap` then verifies the generator is actually complete. Prefer generation; treat a static hand-written file as the fallback.

Iterate the same way as Pass A: fix root cause locally, re-run the single task, move on once green.

**Only when both passes are clean do you push.** At that point CI is confirming what you already know.

### When a check fails during the local loop

A few classes of failure come up reliably on first installs (Node version pin, missing security headers, missing `.github/labeler.yml`, third-party-widget a11y violations, ZAP false positives, etc.). The **`slopstopper-triage`** skill has the per-check playbook: symptom → diagnostic step → fix location → cross-link to the relevant category README. Let it handle each failure as you iterate.

Don't try to fix everything yourself inside this skill. Hand off, fix the one check, come back to Pass A / Pass B and re-run.

## Step 8: Push and watch the confirmation pass

After Step 7's local loop is fully green, push to a PR branch. CI should mirror what you saw locally.

Before the push, confirm the repo is discoverable as an adopter. The installer adds the `slopstopper` topic when `gh` can reach the repo; on a fresh repo with no remote yet it can't, so check and add it now:

```bash
gh repo view --json repositoryTopics --jq '.repositoryTopics[].name' | grep -qx slopstopper || gh repo edit --add-topic slopstopper
```

The `slopstopper` label on issues and PRs needs no setup: every workflow that raises one creates it on first use.

A few checks are CI-only by design (Dependency Review needs GHAS or Dependency Graph; auto-label-pr needs `.github/labeler.yml`). If they fail on the first CI run, hand them to the **`slopstopper-triage`** skill, whose check-by-check table covers them alongside the local checks.

If anything was green locally but red on CI: that's signal there's an environmental delta (Node version pin, missing env var, file-permissions, OS-specific tool). Diagnose, fix, and add the difference to the local pre-flight for next time.
