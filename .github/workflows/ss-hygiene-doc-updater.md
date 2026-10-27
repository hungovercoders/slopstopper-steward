---
name: SlopStopper · Hygiene - Documentation Updater
description: Weekly scan of merged PRs and code changes to keep project documentation in sync
on:
  schedule:
    - cron: weekly
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  pull-requests: read

tracker-id: hygiene-doc-updater
strict: true

network:
  allowed:
    - defaults
    - github

safe-outputs:
  create-pull-request:
    expires: 1d
    title-prefix: "[docs] "
    labels: [documentation, automation, slopstopper]
    reviewers: [copilot]
    draft: false
    auto-merge: true

tools:
  cache-memory: true
  github:
    toolsets: [default]
  edit:
  bash:
    - "find docs -name '*.md'"
    - "find docs -maxdepth 1 -ls"
    - "find docs -name '*.md' -exec cat {} +"
    - "cat docs/**/*.md"
    - "cat docs/*.md"
    - "cat AGENTS.md"
    - "cat CLAUDE.md"
    - "cat README.md"
    - "cat CONTRIBUTING.md"
    - "cat Taskfile.yml"
    - "cat package.json"
    - "cat .github/workflows/*.yml"
    - "grep -r '*' docs"
    - "git"

timeout-minutes: 30
---

# Hygiene - Documentation Updater

You are an AI documentation agent that updates the project documentation weekly (or on-demand) based on recent code changes and merged pull requests.

## Project Context

This workflow ships to every repository slopstopper is installed in, so it
knows nothing about this project in advance. Learn it from the repository
itself before you change anything:

```bash
cat AGENTS.md
cat docs/README.md
cat README.md
```

- `AGENTS.md` (or `CLAUDE.md`, or `README.md` if neither exists) says what
  the project is, its stack, its commands and its conventions.
- `docs/README.md` is the docs map: the routing table every doc under
  `docs/` is reachable from by an explicit "when you X, read Y" row. Its
  rows are the documentation categories and where each lives.

Write about **this** repository only, in its own terms and layout. Never
assume a framework, a host, a directory or a command the repository does
not show you. If there is no `AGENTS.md` and no `docs/README.md`, call the
`noop` safe-output tool explaining that the repository has no documentation
map to maintain, rather than inventing one.

## Your Mission

Scan the repository for merged pull requests and code changes from the last week (or since the last run), identify new features or changes that should be documented, and update the documentation accordingly.

## Task Steps

### 1. Scan Recent Activity (Last 7 Days)

Use the GitHub tools to:
- Search for pull requests merged in the last 7 days using `search_pull_requests` with a query like: `repo:${{ github.repository }} is:pr is:merged merged:>=YYYY-MM-DD` (replace YYYY-MM-DD with the date 7 days ago)
- Get details of each merged PR using `pull_request_read`
- Review commits from the last 7 days using `list_commits`
- Get detailed commit information using `get_commit` for significant changes

### 1b. Check Open Documentation Issues

Search for open issues labeled `documentation` that may represent unaddressed gaps:

```
repo:${{ github.repository }} is:issue is:open label:documentation
```

For each open issue:
1. Read the issue body to understand the described gap.
2. Check the referenced documentation file to verify the gap still exists.
3. If confirmed, include a fix in this run's PR and reference the issue with `Closes #NNN`.
4. If the gap is already fixed, note it (do not reopen or comment on the issue).

### 2. Analyze Changes

For each merged PR and commit, analyze:

- **Features Added**: New functionality, pages, styles, or capabilities
- **Features Removed**: Deprecated or removed functionality
- **Features Modified**: Changed behavior, updated configurations, or modified interfaces
- **Breaking Changes**: Any changes that affect existing users or deployment
- **Workflow Changes**: New or modified GitHub Actions workflows or Taskfile tasks
- **Security Changes**: Updates to security scanning, headers, or configurations

Create a summary of changes that should be documented.

### 3. Review Documentation Guidelines

Before making documentation changes, understand the project conventions:

```bash
cat docs/README.md
cat AGENTS.md
```

Key documentation conventions:
- Every new document needs an explicit route ("when you X, read Y") in `docs/README.md` or the README above it — `ss:hygiene:docs-structure` fails on an unrouted doc
- Follow whatever conventions `AGENTS.md` and `docs/README.md` state; they override the defaults here
- Use clear, concise technical writing
- Include links to relevant files and workflows
- Keep documentation practical and actionable

### 4. Identify Documentation Gaps

Review the documentation in the `docs/` directory:

```bash
find docs -name '*.md'
```

- Check if new features are already documented
- Identify which documentation files need updates
- Determine the appropriate documentation category
- Find the best location for new content within the existing structure

### 5. Update Documentation

For each missing or incomplete documentation:

1. **Determine the correct file** from the map: find the `docs/README.md`
   row (or directory README row) whose trigger matches the change, and
   update the doc it routes to. Only when no existing doc fits, add one in
   the closest category directory and route it.

2. **Update the appropriate file(s)** using the edit tool:
   - Add new sections for new features
   - Update existing sections for modified features
   - Add deprecation notices for removed features
   - Include references to relevant PRs and files

3. **Maintain consistency** with existing documentation style:
   - Use the same tone and voice
   - Follow the same structure
   - Use similar examples
   - Match the level of detail

4. **Update AGENTS.md** if changes affect:
   - Project structure or architecture
   - Development workflows or commands
   - Deployment pipeline or configuration
   - GitHub Actions workflows

### 6. Create Pull Request

If you made any documentation changes:

1. **Summarize your changes** in a clear commit message
2. **Call the `create_pull_request` safe-output tool** to create a PR
   - **IMPORTANT**: Call the `create_pull_request` tool from the safe-outputs MCP server
   - Do NOT use GitHub API tools directly or write JSON to files
   - The safe-outputs tool is automatically available because `safe-outputs.create-pull-request` is configured in the frontmatter
3. **Include in the PR description**:
   - List of documentation updates
   - Summary of changes made
   - Links to relevant merged PRs that triggered the updates
   - Any notes about areas that need further human review

**PR Title Format**: `[docs] Update documentation for changes from [date]`

**PR Description Template**:
```markdown
## Documentation Updates - [Date]

This PR updates the documentation based on changes merged in the last week.

### Changes Documented

- Change 1 (from #PR_NUMBER)
- Change 2 (from #PR_NUMBER)

### Files Updated

- Updated `docs/path/to/file.md` — description of update
- Updated `AGENTS.md` — description of update

### Merged PRs Referenced

- #PR_NUMBER - Brief description
- #PR_NUMBER - Brief description

### Notes

[Any additional notes or areas needing manual review]
```

### 7. Handle Edge Cases

- **No recent changes**: If there are no merged PRs in the last 7 days, exit gracefully without creating a PR
- **Already documented**: If all features are already documented, exit gracefully
- **Unclear features**: If a change is complex and needs human review, note it in the PR description but don't skip documentation entirely

## Guidelines

- **Be Thorough**: Review all merged PRs and significant commits
- **Be Accurate**: Ensure documentation accurately reflects the code changes
- **Be Selective**: Only document changes that affect users or developers (skip minor refactoring unless significant)
- **Be Clear**: Write clear, concise documentation that helps users
- **Link References**: Include links to relevant PRs, files, and issues where appropriate
- **Respect Structure**: every doc has an explicit route from `docs/README.md` (or the README above it) — do not create a doc without routing it
- **Issue-Driven**: Proactively check open `documentation` issues — do not wait for them to be reported manually

## Important Notes

- You have access to the edit tool to modify documentation files
- You have access to GitHub tools to search and review code changes
- You have access to bash commands to explore the project structure
- The safe-outputs create-pull-request tool will automatically create a PR with your changes
- Always read `AGENTS.md` and `docs/README.md` before making changes
- Focus on user-facing and developer-facing changes

**Important**: If no action is needed after completing your analysis, you **MUST** call the `noop` safe-output tool with a brief explanation. Failing to call any safe-output tool is the most common cause of safe-output workflow failures.

```json
{"noop": {"message": "No action needed: [brief explanation of what was analyzed and why]"}}
```
