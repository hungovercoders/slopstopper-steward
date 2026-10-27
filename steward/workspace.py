"""A clone the steward works in: branch, commit, push, pull request.

The agent never gets to push; this module is the only writer. Branches
are `steward/<order id>` and force-pushed with lease, because the steward
owns them: a re-run (after a closed PR, or a failed push) must replace the
old branch, not stack on it or be refused by it."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from steward import gh

AUTHOR = ("slopstopper-steward", "steward@slopstopper.dev")
LOCAL_ONLY = (".steward-result.json", ".steward-release-notes.md", ".steward-prompt.md", ".steward-venv/")


@dataclass
class Workspace:
    repo: str
    path: Path
    default_branch: str
    # Paths the steward put in the tree for its own use (fetched skills) and
    # must never commit. A pathspec exclusion, because `.git/info/exclude`
    # loses to a `.gitignore` negation such as the `!.claude/skills/**` the
    # slopstopper installer writes.
    excluded: list[str] = field(default_factory=list)

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=check)

    def _pathspec(self) -> list[str]:
        return ["--", ".", *(f":(exclude){p}" for p in self.excluded)]

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain", *self._pathspec()).stdout.strip())

    def changed_files(self) -> list[str]:
        out = self.git("status", "--porcelain", *self._pathspec()).stdout
        return sorted(line[3:].strip() for line in out.splitlines() if line.strip())


def clone(repo: str, default_branch: str, into: Path) -> Workspace:
    """A fresh clone at `into`. Whatever an earlier run left there (a
    `--workdir` reused, a retry) is removed first: the directory is the
    steward's own, named by repo and order, and `gh repo clone` refuses a
    non-empty one."""
    if into.exists():
        shutil.rmtree(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    result = gh.gh("repo", "clone", repo, str(into), "--", "--depth", "50", "--branch", default_branch, timeout=600)
    if result.returncode != 0:
        raise gh.GhError(f"clone {repo}: {result.stderr.strip()}")
    return prepare(Workspace(repo, into, default_branch))


def prepare(ws: Workspace) -> Workspace:
    """Make a fresh clone one the steward can push from.

    `gh repo clone` lends its credential helper to the clone command only,
    so the clone itself has none: the helper is set here, and reads the
    same GH_TOKEN gh does. A `--depth` clone is single-branch, so the fetch
    refspec is widened to every branch; `branch()` then fetches the order's
    remote branch and the lease in `push()` compares against its real tip.
    """
    ws.git("config", "user.name", AUTHOR[0])
    ws.git("config", "user.email", AUTHOR[1])
    ws.git("config", "credential.helper", "!gh auth git-credential")
    ws.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    exclude = ws.path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(LOCAL_ONLY) + "\n")
    return ws


def branch(ws: Workspace, name: str) -> None:
    """Start the order's branch from the default branch's tip, having fetched
    the remote branch of the same name (if any) so the push lease knows it."""
    ws.git("fetch", "origin", name, check=False)
    ws.git("checkout", "-B", name, f"origin/{ws.default_branch}")


def commit(ws: Workspace, message: str) -> bool:
    if not ws.dirty():
        return False
    ws.git("add", "-A", *ws._pathspec())
    ws.git("commit", "-q", "-m", message)
    return True


def push(ws: Workspace, name: str) -> None:
    ws.git("push", "--force-with-lease", "-u", "origin", name)


def ensure_labels(repo: str, labels: list[str]) -> None:
    for label in labels:
        gh.gh("label", "create", label, "-R", repo, "--force", "--color", "6f42c1",
              "--description", "Opened by slopstopper-steward")


def open_pr(ws: Workspace, title: str, body: str, labels: list[str], draft: bool) -> str:
    ensure_labels(ws.repo, labels)
    args = ["pr", "create", "-R", ws.repo, "--base", ws.default_branch, "--head",
            ws.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "--title", title, "--body-file", "-"]
    for label in labels:
        args += ["--label", label]
    if draft:
        args.append("--draft")
    result = gh.gh(*args, input=body, cwd=str(ws.path))
    if result.returncode != 0 and "--label" in args:
        # A repo that refuses labels still gets its PR; a lost fix is worse than an unlabelled one.
        plain = [a for i, a in enumerate(args) if a != "--label" and (i == 0 or args[i - 1] != "--label")]
        result = gh.gh(*plain, input=body, cwd=str(ws.path))
    if result.returncode != 0:
        raise gh.GhError(f"pr create on {ws.repo}: {result.stderr.strip()}")
    return result.stdout.strip().splitlines()[-1]
