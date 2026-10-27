# Contributing

## Commands

| Task | Does |
| ---- | ---- |
| `task steward:setup` | `.venv` with the package editable and pytest |
| `task steward:test` | the pytest suite; `-- -k <expr>` filters |
| `task steward:contract` | `steward contract` against `../slopstopper` (`SRC=path` for another) |
| `task steward:contract-doc` | regenerate the table in `docs/architecture/CONTRACT.md`; `-- --check` only reports |
| `task ss:hygiene:test` | slopstopper's static suite on this repo; the pre-push hook runs it |

The contract test (`tests/test_contract.py`) reads a slopstopper
checkout: `$SLOPSTOPPER_SRC`, else a sibling `../slopstopper`, else a
shallow clone. CI clones `main`.

## Exit codes

The same three as slopstopper: `0` ran clean, `1` ran and something
failed (an order errored, a surface moved), `2` could not run (no `gh`,
bad config, PyPI unreachable). `--json` prints one JSON value on stdout
and nothing else; add a key freely, never rename or remove one without a
note here.

## Conventions

- **Stdlib only.** GitHub goes through `gh`, PyPI through `urllib`, the
  agent through the `claude` CLI, slopstopper through its own wheel in a
  throwaway venv. `pyproject.toml` has no runtime dependencies and should
  keep none.
- **Pure core, effects at the edge.** `plan.py` and `versions.py` touch
  nothing; `gh.py`, `workspace.py` and `execute/` do. Tests monkeypatch
  `steward.gh.api` and `steward.gh.contents` rather than a subprocess.
- **The brief names the work, the skill holds the method.** A change to
  `steward/prompts.py` that explains how to fix a check belongs upstream
  in slopstopper's skills.
- **A new surface read from slopstopper** is a new row in
  `steward/contract.py`, the regenerated table, and a line in slopstopper's
  `CHANGE_MAP.md` steward row.
- [Conventional Commits](https://www.conventionalcommits.org/); actions
  pinned to a commit SHA with the version in a comment.

## Before a PR

```bash
task steward:test
task steward:contract-doc -- --check
task ss:hygiene:test
```
