# Step 2: Reproduce locally

> Part of the `slopstopper-triage` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 2: Reproduce locally

`task ss:*` is the canonical local interface, the same shipped Task in the target's repo as CI uses. Run `task ss:<category>:<check>` and you get the same code path, same exit codes, same reports. The local loop is an order of magnitude faster than pushing to CI per iteration. If the target was installed with `--no-task`, fall back to `slopstopper run <category>:<check>` directly, which works identically.

| Workflow | Canonical (Task) | Underlying CLI | Needs URL? | Needs build? | Needs Docker? |
|---|---|---|---|---|---|
| `ss-security-sast-check.yml` | `task ss:security:sast` | `slopstopper run security:sast` | – | – | – |
| `ss-security-secrets-check.yml` | `task ss:security:secrets` | `slopstopper run security:secrets` | – | – | – |
| `ss-security-vulnerability-all-check.yml` | `task ss:security:vulnerability:all` | `slopstopper run security:vulnerability:all` | – | – | – |
| `ss-security-vulnerability-new-check.yml` | (CI-only, the Dependency Review action) | – | – | – | – |
| `ss-security-licences-check.yml` | `task ss:security:licences` | `slopstopper run security:licences` | – | – | – |
| `ss-security-dast-check.yml` | `task ss:security:dast -- <URL>` | `slopstopper run security:dast <URL>` | ✓ | ✓ (site mode only) | ✓ |
| `ss-security-api-headers-check.yml` | `task ss:security:api-headers -- <URL>` | `slopstopper run security:api-headers <URL>` | ✓ | – (probe a deployed origin) | – |
| `ss-hygiene-complexity-check.yml` | `task ss:hygiene:complexity` | `slopstopper run hygiene:complexity` | – | – | – |
| `ss-hygiene-csp-exceptions-check.yml` | `task ss:hygiene:csp-exceptions` | `slopstopper run hygiene:csp-exceptions` | – | – | – |
| `ss-hygiene-privacy-check.yml` | `task ss:hygiene:privacy` | `slopstopper run hygiene:privacy` | – | – | – |
| `ss-hygiene-docs-accuracy-check.yml` | `task ss:hygiene:docs-accuracy` | `slopstopper run hygiene:docs-accuracy` | – | – | – |
| `ss-hygiene-docs-size-check.yml` | `task ss:hygiene:docs-size` | `slopstopper run hygiene:docs-size` | – | – | – |
| `ss-hygiene-docs-structure-check.yml` | `task ss:hygiene:docs-structure` | `slopstopper run hygiene:docs-structure` | – | – | – |
| `ss-hygiene-entry-files-check.yml` | `task ss:hygiene:entry-files` | `slopstopper run hygiene:entry-files` | – | – | – |
| `ss-hygiene-openapi-check.yml` | `task ss:hygiene:openapi -- <URL>` | `slopstopper run hygiene:openapi <URL>` | ✓ (optional, only the probes need it) | – (never builds locally) | – |
| `ss-hygiene-auto-label-pr.yml` | (CI-only, needs PR context) | – | – | – | – |
| `ss-reliability-smoke-tests.yml` | `task ss:reliability:smoke -- <URL>` | `slopstopper run reliability:smoke <URL>` | ✓ | ✓ (if URL is local) | – |
| `ss-reliability-e2e-check.yml` | `task ss:reliability:e2e -- <URL>` | `slopstopper run reliability:e2e <URL>` | ✓ | ✓ (if URL is local) | – |
| `ss-reliability-accessibility-check.yml` | `task ss:reliability:accessibility -- <URL>` | `slopstopper run reliability:accessibility <URL>` | ✓ | ✓ | – |
| `ss-reliability-core-web-vitals.yml` | `task ss:reliability:cwv -- <URL>` | `slopstopper run reliability:cwv <URL>` | ✓ | ✓ | – |
| `ss-reliability-seo-check.yml` | `task ss:reliability:seo -- <URL>` | `slopstopper run reliability:seo <URL>` | ✓ | ✓ | – |
| `ss-reliability-llms-txt-check.yml` | `task ss:reliability:llms-txt -- <URL>` | `slopstopper run reliability:llms-txt <URL>` | ✓ | ✓ | – |
| `ss-reliability-robots-txt-check.yml` | `task ss:reliability:robots-txt -- <URL>` | `slopstopper run reliability:robots-txt <URL>` | ✓ | ✓ | – |
| `ss-reliability-sitemap-check.yml` | `task ss:reliability:sitemap -- <URL>` | `slopstopper run reliability:sitemap <URL>` | ✓ | ✓ | – |
| `ss-reliability-broken-links-check.yml` | `task ss:reliability:broken-links -- <URL>` | `slopstopper run reliability:broken-links <URL>` | ✓ | ✓ | – |
| `ss-reliability-api-health-check.yml` | `task ss:reliability:api-health -- <URL>` | `slopstopper run reliability:api-health <URL>` | ✓ | – (never builds locally) | – |
| `ss-reliability-api-latency-check.yml` | `task ss:reliability:api-latency -- <URL>` | `slopstopper run reliability:api-latency <URL>` | ✓ | – (never builds locally) | – |
| `ss-workflow-failure-issue.yml` | (CI-only, operational, runs on workflow_run) | – | – | – | – |

For dynamic checks (`URL ✓`): pass the URL as a bare-positional arg after `--` (Task) or directly (CLI). Pointing at `http://localhost:8080` with `slopstopper serve` serving the built site is the fastest local loop.

The four API checks are the exception: they never build and serve the repo (an API isn't a static bundle), so reproduce them against whatever origin actually runs the API, whether a preview environment or a locally-running server you started yourself. `security:api-headers` in particular audits headers that usually come from the edge or proxy, so a clean local run is weaker evidence than a clean run against the deployed URL.

Two more reproduce caveats:

- **`reliability:api-latency` does not reproduce reliably anywhere.** Timings depend on the machine, the network and whether the target is cold. A local run that passes proves nothing about the CI failure, and vice versa. Reproduce it against the *same* environment CI audited, and re-run it a few times before believing a single result.
- **`hygiene:openapi` is the one hygiene check that needs a URL**, and only for its reachability probes. The committed-vs-served comparison needs `api.openapi.served_spec`, not a URL, so `slopstopper run hygiene:openapi` with no argument still reproduces that half.

If a reliability workflow's failure is about *which pages it audited* rather than what it found, the page-list comes from `slopstopper discover <check> --event=<event>`. Run that directly to see the resolved set before reproducing the check itself.

For `security:vulnerability:all`: if a finding shows up only on CI (or only locally), trivy DB freshness or binary version drift is the usual cause, so see `docs/security/VULNERABILITY.md` → "Local/CI Parity" before pursuing the finding as real.
