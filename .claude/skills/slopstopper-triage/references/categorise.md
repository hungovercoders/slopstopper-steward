# Step 4: Categorise the finding

> Part of the `slopstopper-triage` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 4: Categorise the finding

For each failing finding, decide which of the four buckets it falls into. The first three are below; the fourth, a bug in slopstopper itself, is in `upstream.md`, because its fix is an issue on `hungovercoders/slopstopper` plus a marked workaround rather than a change to this repo.

### Real finding: fix the code

The check is correctly flagging a problem. Examples:
- Secrets check finds an actual leaked API key → revoke immediately, scrub history (`git filter-repo` or BFG), then fix the leak source.
- SAST flags a SQL injection in a query builder → rewrite to use parameterised queries.
- Accessibility flags a missing `alt` on a real image → add the alt text.
- Broken-links check flags an internal link that 404s → fix or remove the link.

There's no escape hatch here, so fix the code.

### False positive: suppress narrowly with `# why`

The check's heuristic is misfiring on this codebase. Use the check's documented suppression mechanism, never a global disable, and always carry a `# why` comment so the next reader knows why the exception is justified.

| Check | Suppression file / mechanism | Granularity |
|---|---|---|
| Secrets (gitleaks) | `.gitleaks.toml` `[allowlist]` block | Path or regex |
| SAST (Semgrep) | `# nosemgrep: <rule-id>` inline, or `.semgrepignore` for path-scoped | Inline or path |
| DAST (OWASP ZAP), CSP findings | `docs/security/CSP_EXCEPTIONS.md` `## Exceptions` → `### /path` heading (glob patterns supported: `/*`, `/blog/*`) | Per-path |
| DAST, other rule false positives | `.zap/rules.tsv` → `<alert-ref>\tIGNORE\t# why` (`10055-13`), or a bare plugin id for every alert of that plugin | Per-alert or per-plugin, site-wide |
| Accessibility (axe-core) | `disabledRules` in the spec, or scope the spec to exclude the offending selector | Per-rule or per-element |
| Vulnerability (Trivy) | `.trivyignore` with the CVE ID + a `# why` line | Per-CVE |
| Licence (`security:licences`, dependency review) | the package's purl under `allow-dependencies-licenses` in `.github/dependency-review-config.yml` + a `# why` line; both checks honour it | Per-package |
| Complexity (lizard) | Refactor the function, or raise `hygiene.complexity.max_ccn` in `.slopstopper.yml` (default 15) with a `# why` note. A flagged file that is generated or vendored → `hygiene.complexity.exclude` | Per-repo (the ceiling is global) |

Each suppression is a documented gate exception, not a way to quiet findings that should be fixed. The PR that adds it is the place reviewers catch it.

### Threshold too tight: tune in `.slopstopper.yml`

The check is correct but the default threshold doesn't fit this repo. **First check `.slopstopper.yml`**, because many checks read their thresholds from there, falling back to a hardcoded default when unset. The example file ([`.slopstopper.yml.example`](https://github.com/hungovercoders/slopstopper/blob/main/.slopstopper.yml.example)) is the canonical schema reference.

Config-driven knobs (no file edit needed beyond `.slopstopper.yml`):

| Check | Keys | Defaults |
| --- | --- | --- |
| `hygiene:docs-size` | `hygiene.docs_size.max_total_size_kb` / `.max_file_size_kb` / `.max_files` | 150 / 20 / 25 |
| `hygiene:entry-files` | `hygiene.entry_files.max_tokens` / `.readme_max_tokens` / `.map_max_tokens` / `.map_path` / `.require_map_pointer` / `.require_explicit_routes` / `.require_claude_include` | 2000 / 600 / 1000 / `docs/README.md` / true / true / true |
| `hygiene:docs-accuracy` | `hygiene.docs_accuracy.extra_paths` | `[]`, so only `docs/` + the root entry files are scanned |
| `hygiene:docs-structure` | `hygiene.docs_structure.require_routed_docs` / `.max_route_depth` / `.max_doc_lines` | `true`, so every doc has an explicit route / 3 / 300 |
| all three docs checks | `hygiene.docs.exclude` (globs) | `[docs/archive/**]`; for non-doc content under `docs/` such as a course, rather than raising the caps |
| `reliability:smoke` | `pages.smoke`, `smoke.og_image_path` | `/`, `/og-image.png` |
| `reliability:e2e` | `pages.e2e`, `e2e.max_links` | `/`, `25` |
| `reliability:{accessibility,seo,broken_links}` | `pages.<check>`, `reliability.coverage.<event>` | `/`, hand-list mode |
| `hygiene:csp-exceptions` | `headers.source`, `headers.format` | unset (graceful skip) |
| `hygiene:privacy` | `privacy.page` / `.url` / `.require_link` / `.hosts` | `auto` (first source `privacy.*`) / `auto` / `true` / `[]`; `page: ''` opts out visibly |
| `hygiene:complexity` | `hygiene.complexity.max_ccn` / `.exclude` | 15 / `[]` (tests and gitignored paths are skipped without it) |
| `reliability:cwv` | `reliability.cwv.public_report` (report hosting, not a threshold; budgets live in `.ss/lighthouserc.json`) | `false`, so reports stay local |

Tunings that are NOT yet config-driven (require code/file edits):

- `.ss/lighthouserc.json` (or the bundled `cli/slopstopper/data/lighthouserc.json`) holds the Core Web Vitals thresholds. Edit the `.ss/` copy if you want adopter-side persistence; the CLI prefers it over the package-data fallback.

Tuning is a real decision. When you raise a threshold, leave a `# why` comment in `.slopstopper.yml` so the next maintainer doesn't roll it back. Don't tune to silence noise; tune to match a deliberate design decision.

### Slopstopper bug: raise it upstream, mark the workaround

When none of the three fits because the check is wrong for every repo (a false positive no mechanism can scope, a traceback, a workflow that fails before the check runs), stop categorising and read `upstream.md`. The fix is an issue on `hungovercoders/slopstopper` and, where the repo can't wait, the narrowest workaround with a `slopstopper-workaround: hungovercoders/slopstopper#<N>` marker on it. The refresh flow removes marked workarounds once their issue is closed; an unmarked one stays forever.
