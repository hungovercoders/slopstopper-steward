# Decisions

Significant decisions and their rationale. One row each; a supporting
note (`<date>-<slug>.md`) only when the row cannot carry the context.

## Decisions

| Date | Decision | Status | Rationale | Notes |
| ---- | -------- | ------ | --------- | ----- |
| 2026-10-27 | The steward is a separate repo and package from slopstopper | accepted | slopstopper is per-repo and ships into every adopter; org-wide discovery, budgets and cross-repo PRs need a token and a schedule no adopter should carry. Keeping them apart keeps the adopter install small and the trust boundary explicit | none |
| 2026-10-27 | The steward carries no check or fix knowledge; it consumes a written contract | accepted | Duplicating slopstopper's method would rot on every release. Reading the installer's files, `emit`'s markers and the shipped skills, with a contract test both sides can run, keeps one source of truth | [architecture/CONTRACT.md](../architecture/CONTRACT.md) |
| 2026-10-27 | Deterministic executors first; an agent only for judgement calls | accepted | Moving a pin and rewriting the suite is `slopstopper install`; verification is `slopstopper run --json`. Only a finding, or a red check after a refresh, needs a decision, and that is the one place a model runs, boxed and budgeted | none |
| 2026-10-27 | Claude Code headless is the agent engine, reading the repo's own skills | accepted | The fix method already ships as Claude Code skills into every adopter, so the engine that loads them natively needs no translation layer and no compile step. The engine sits behind `execute/agent.py`; another runner would replace that module only | none |
| 2026-10-27 | Skills come from the adopter's install, else the pinned release tag, never `main` | accepted | The method must match the CLI version that verifies the work; slopstopper issue #571 records the same requirement for its standalone skills refresher | none |

## Status values

`proposed`, `accepted`, `superseded`, `rejected`.

## Decision template

Use when a row needs more context than it can hold; name the note
`<date>-<slug>.md` and link it from the Notes column.

- Date, Status, Owner, Related area
- Decision summary (one or two sentences)
- Context: the problem, constraint or trigger
- Options considered, with the trade-off of each
- Consequences: what becomes easier, what becomes harder
