# Step 5: Set up the docs layout (AGENTS.md first)

> Part of the `slopstopper-install` skill. `SKILL.md` says when to read this; it is not loaded until then.

## Step 5: Set up the docs layout (if keeping the docs-* checks)

**Why this layout, beyond passing the check.** `AGENTS.md` is loaded into every agent conversation, and it is prompt-cached, so the tokens it carries are cheap per turn, while every "go read another file" hop costs a whole tool turn on every task that takes it. The failure mode is not size but rule count: instruction-following degrades as competing rules pile up, and ~2,000 estimated tokens (chars/4) holds roughly 40–60 rules, about where models start dropping some. So `AGENTS.md` **carries what most tasks need** within that budget (ground rules, the command surface, the layout, the conventions most changes touch) and **routes the rest** to `docs/` with explicit, conditional lines: "Before you change CI, read docs/ci.md, which defines what `task ci` runs." Measured downstream: an unrouted runbook was skipped one run in three; a routed one was read every time. A "see docs/" is decoration agents skip. Frame it to the user as rules that get followed and tasks that start with zero extra reads, not paperwork the check demands.

The four workflows `ss-hygiene-entry-files-check.yml`, `ss-hygiene-docs-structure-check.yml`, `ss-hygiene-docs-accuracy-check.yml` and `ss-hygiene-docs-size-check.yml` enforce it. The shape they expect:

```
README.md         human orientation + quick start, ≤ 600 tokens (badges excluded); links the map
AGENTS.md         the working layer, ≤ 2,000 tokens; every .md link an explicit route; one reaches the map
CLAUDE.md         exactly `@AGENTS.md`
docs/README.md    the map: one trigger-first "read" row per doc AGENTS.md does not route directly, ≤ 1,000 tokens
docs/<topic>.md   one concern each, ≤ 300 lines; a directory with its own README only when a topic split
```

**Decide how much to change first. The least change wins.** Measure the target's entry file (`task ss:hygiene:entry-files` prints the cold-start cost even when it fails) and pick one outcome; say which in your summary:

- **Routes only.** An entry file already auto-loads (`CLAUDE.md` or `AGENTS.md`), fits ~2,000 tokens, and its rules are followed. Do not move content. Rename `CLAUDE.md` → `AGENTS.md` and make `CLAUDE.md` the include (a rename plus one line, not a rewrite), add an explicit route for every doc it does not route to, add the catch-all route to `docs/README.md`, and stop. This is the right answer more often than it feels.
- **Build.** Nothing auto-loads (a README is the only doc, or `AGENTS.md` is a stub). `install.sh` seeded the scaffolds (below); fill `AGENTS.md` in priority order (ground rules, commands, layout, conventions, gotchas, routes) and stop at the budget.
- **Fan out.** The entry file is over budget. Keep what most tasks need; move the content that serves the fewest tasks into `docs/<topic>.md` and route it. A one-line rule stays inline; its procedure, tables and examples go to the doc.

**Auto-seeded scaffolds.** On a fresh install `install.sh` writes `README.md`, `AGENTS.md`, `CLAUDE.md` and `docs/README.md` from `cli/slopstopper/data/templates/entry-files/` when they don't already exist, so a greenfield clone is green on Step 7's local loop. A file that exists is left alone; remediation runs through the reports (next paragraph). A pre-0.15 install that seeded `docs/index.md` gets a warning instead: the map is a README now, because the repo UI renders a README in place when someone browses `docs/`.

**Remediating existing files.** `task ss:hygiene:entry-files` writes `.ss/reports/entry-files/entry-file-size-report.md` with a paste-ready fix per violation, covering the route line for a missing map pointer, the one-line `@AGENTS.md` body for `CLAUDE.md`, a map skeleton, the `git mv docs/index.md docs/README.md` for a legacy map, and the list of soft routes to reword. `task ss:hygiene:docs-structure` writes `.ss/reports/docs/docs-structure-report.md` listing every unrouted doc with the README that should route it. Read both, confirm the placement with the user if a file is non-trivial, apply, re-run.

**Route wording that gets followed** puts the trigger first, then "read", then the file as a markdown link, then what it holds. In a table, the header's first cell is the trigger ("When you are…") and each row's second cell starts with "Read":

| Soft (skipped)                   | Explicit (followed)                                                      |
| -------------------------------- | ------------------------------------------------------------------------ |
| See docs/ci.md for CI details.   | Before you change CI, hooks or the check scripts, read [docs/ci.md](docs/ci.md). |
| Docs live in docs/.              | For any task not covered above, read [docs/README.md](docs/README.md) before you start. |

Close `AGENTS.md`'s routes with that catch-all so uncovered tasks don't guess. Two hops is the ceiling for a topic doc (AGENTS.md → map → doc); a directory README adds one, and the check fails past three.

**Migrating a pre-0.15 Map Pattern repo** (`docs/index.md` + per-category READMEs + thin pointer entry files): `git mv docs/index.md docs/README.md`; rewrite its category table as a "When you are… | Do this" table; rewrite each category README's `## Contents` list the same way (every sub-doc needs a row); fold any real content out of the old pointer `AGENTS.md` into a working layer (ground rules, commands, layout, routes); make `CLAUDE.md` exactly `@AGENTS.md`; split any doc past 300 lines. The structure report names every doc still unrouted; iterate until it is green.

**Knobs** in `.slopstopper.yml` if you need to override (defaults shown):

```yaml
hygiene:
  entry_files:
    max_tokens: 2000               # AGENTS.md
    readme_max_tokens: 600         # README.md, badges excluded
    map_max_tokens: 1000           # docs/README.md
    map_path: docs/README.md
    require_map_pointer: true
    require_explicit_routes: true  # false accepts soft links in AGENTS.md
    require_claude_include: true   # false accepts any CLAUDE.md
  docs_structure:
    require_routed_docs: true
    max_route_depth: 3
    max_doc_lines: 300             # 0 disables
```

Tune a knob only for a deliberate design decision, never to silence a finding, because budgets are the feature.

**Cross-references in docs:** the `docs-accuracy` check scans for `` `backtick-quoted` `` filenames and broken markdown links. A bare name (`foo.sh`) or partial path (`demos/foo.sh`) resolves if any file in the repo has that name or ends with that path; a full repo-relative path (`scripts/foo.sh`) is still clearest for readers.

If none of this fits the target (a short-lived prototype, single-file tool, generated docs only), delete the four workflows instead. `.ss/.workflows-installed` remembers the deletion, so re-installs don't bring them back.
