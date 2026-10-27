# slopstopper-steward

**The org-wide operator for [slopstopper](https://github.com/hungovercoders/slopstopper) adopters.** slopstopper decides what is wrong in one repo and ships how to fix it. The steward decides which repos across an organisation need a hand and drives slopstopper's own tooling at them: it finds every repo with slopstopper installed, moves CLI pins that fell behind, and works the issues slopstopper raised, one pull request each.

It carries no check logic and no fix knowledge of its own. The pinned CLI rewrites and verifies the suite; the `slopstopper-triage` skill the install ships is what the agent works a finding with; the surfaces it relies on are a written contract, tested nightly against slopstopper's `main`.

```bash
pip install -e .
steward discover            # who has slopstopper installed
steward plan                # what a run would open, nothing written
steward run                 # open the PRs (--dry-run for a report only)
steward contract ../slopstopper   # do slopstopper's surfaces still hold?
```

## Docs

- Setting it up for an org (GitHub App or PAT, secrets, the first dry run): [`docs/runbooks/SETUP.md`](./docs/runbooks/SETUP.md)
- How a run works and the contract with slopstopper: [`docs/architecture/README.md`](./docs/architecture/README.md)
- Working in this repo: [`AGENTS.md`](./AGENTS.md); [`docs/README.md`](./docs/README.md) routes to everything else

## Dogfooded

This repo has slopstopper installed on itself (`--profile library`), so the same suite it steers runs on every PR here.

<!-- badges:begin -->

### 🔒 Security
[![Licences](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-licences-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-licences-check.yml)
[![SAST](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-sast-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-sast-check.yml)
[![Secrets](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-secrets-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-secrets-check.yml)
[![Dependency CVEs](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-vulnerability-all-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-vulnerability-all-check.yml)
[![Dependency Review](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-vulnerability-new-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-security-vulnerability-new-check.yml)

### 🧹 Hygiene
[![Auto-label PRs](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-auto-label-pr.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-auto-label-pr.yml)
[![Complexity](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-complexity-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-complexity-check.yml)
[![Docs Accuracy](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-accuracy-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-accuracy-check.yml)
[![Docs Size](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-size-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-size-check.yml)
[![Docs Structure](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-structure-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-docs-structure-check.yml)
[![Entry Files](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-entry-files-check.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-entry-files-check.yml)

### 🤖 Operational
[![Doc Updater](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-doc-updater.lock.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-hygiene-doc-updater.lock.yml)
[![Workflow Failures](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-workflow-failure-issue.yml/badge.svg?branch=main)](https://github.com/hungovercoders/slopstopper-steward/actions/workflows/ss-workflow-failure-issue.yml)
<!-- badges:end -->

## License

MIT; see [LICENSE](./LICENSE).
