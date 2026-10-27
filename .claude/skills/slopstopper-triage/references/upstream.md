# Step 4b: A slopstopper bug: raise it upstream, work around it with a marker

> Part of the `slopstopper-triage` skill. `SKILL.md` says when to read this; it is not loaded until then.

## When the fault is slopstopper's, not the repo's

The three usual buckets (real finding, false positive, threshold) all assume the check is right about *something*. The fourth is different: the check, its template, its workflow or the installer is wrong for every repo, and this one just hit it first. The test is "where would the fix live?" If the answer is `hungovercoders/slopstopper` rather than this repo, it is this bucket.

Signals:

- A false positive the suppression table cannot scope narrowly: the check misreads a shape it claims to support (a route form it documents, a lockfile format it lists, a header source it has an adapter for).
- Exit `2` from the CLI's own code: a Python traceback, an argument the parser rejects, a report path that is never written.
- A workflow or composite action that fails before the check runs, on a runner the suite targets.
- A knob this skill or the docs describe that the CLI ignores at the pinned version, after confirming the pin is at a release that has it (`refresh.md`, "Spot newly-shipped knobs").
- The installer writing something wrong on a refresh (a workaround of its own lives in `.ss/.workflows-installed` only by accident).

Fixing it locally and moving on leaves the bug for the next adopter, and leaves this repo carrying a workaround nobody will remember to remove. So both halves below are the fix: the issue goes upstream, and the workaround carries a marker that points at the issue.

## Raise the issue upstream

1. **Check it is not already known.** Search open issues and PRs first; a known bug gets a comment with this repo's reproduction, not a duplicate:

   ```bash
   gh issue list -R hungovercoders/slopstopper --state all --search "<check> <symptom>"
   gh pr list -R hungovercoders/slopstopper --search "<check> <symptom>"
   ```

   If it exists, comment with the reproduction below and use its number for the marker.

2. **Gather the facts the maintainer needs.** The check name (`<category>:<check>`), the CLI version (`slopstopper --version`, which must equal the `"pipx:slopstopper-cli"` pin in `mise.toml`), the profile (`slopstopper profile show`), the exact command, its output trimmed to the relevant lines, what you expected instead, and the workaround you are about to apply.

3. **Open it.** One issue per bug, titled `<check>: <symptom in one line>`:

   ```bash
   gh issue create -R hungovercoders/slopstopper \
     --title "<category>:<check>: <symptom in one line>" --body-file - <<'BODY'
   ## Problem statement

   <what the check did, one paragraph>

   ## Reproduction

   - slopstopper-cli: <version>  ·  profile: <ui|api|library>  ·  repo: <owner/name, or "private">
   - command: `<task ss:… or slopstopper run …>`
   - output:
     ```
     <trimmed>
     ```
   - expected: <one line>

   ## Workaround applied

   <file and line, or "none">, marked `slopstopper-workaround: hungovercoders/slopstopper#<this issue>`

   ## Acceptance criteria

   - [ ] <the check behaves as expected on the shape above>
   - [ ] <a test pins it>
   BODY
   ```

   Don't pass `--label`: only collaborators on `hungovercoders/slopstopper` can set labels there, and `gh issue create --label` fails outright on a label it can't apply. The title prefix is the discovery contract for adopter reports; the maintainers label them `slopstopper` on triage. The `slopstopper` label on issues *inside* the adopter repo is a different thing: every workflow that raises one there creates it on first use.

4. **If `gh` cannot reach `hungovercoders/slopstopper`** (no auth, a locked-down runner), print the filled-in body and ask the user to file it through the "Adopter report" form at https://github.com/hungovercoders/slopstopper/issues/new/choose, which asks for the same facts and applies the label itself. Do not skip the issue because the tool is missing.

5. **Tell the user the issue number** and put it in the marker.

## Work around it with a marker

Never a silent workaround. Every one carries, in the file's own comment syntax, on the line it changes or the line above:

```
slopstopper-workaround: hungovercoders/slopstopper#<N> <one line: what it works around>
```

That is `# ` in `.slopstopper.yml`, `Taskfile.yml`, Python, shell, `.gitleaks.toml`, `.trivyignore`, `.zap/rules.tsv` and workflow YAML; `//` in JS and TS; `<!-- -->` in Markdown and HTML. Examples:

```yaml
# .slopstopper.yml
workflows:
  disabled:
    # slopstopper-workaround: hungovercoders/slopstopper#NNN tracker closes the wrong issue on a repo with two failing checks
    - ss-workflow-failure-issue.yml
hygiene:
  complexity:
    # slopstopper-workaround: hungovercoders/slopstopper#MMM lizard counts gitignored build output
    exclude: [dist/**]
```

```python
subprocess.run(cmd, shell=True)  # nosemgrep: subprocess-shell-true  slopstopper-workaround: hungovercoders/slopstopper#NNN rule fires on a constant string
```

Rules:

- **Narrowest scope that makes the check green**, the same as any suppression. A marker does not license a blanket disable.
- **One issue per marker.** Two bugs on one line get two markers.
- **Don't hand-edit `ss-*.yml` to work around it.** The installer rewrites those on every refresh, so the workaround vanishes and the marker with it. If the only possible workaround is inside a workflow, say so in the issue and disable the workflow through `workflows.disabled` with the marker there instead.
- **The marker is the contract with the refresh.** `slopstopper-install`'s refresh step "Remove workarounds whose upstream issue is closed" (`references/refresh.md` in that skill) greps for every marker, asks GitHub for the issue's state, and removes the workarounds whose issue is closed, then re-runs the check to prove the fix covers this repo. An unmarked workaround is invisible to it and stays forever.

To list what this repo is currently working around:

```bash
grep -rn --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=.ss --exclude-dir=.claude \
  'slopstopper-workaround: hungovercoders/slopstopper#' .
```
