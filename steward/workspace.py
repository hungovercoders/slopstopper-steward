"""A clone the steward works in: branch, commit, push, pull request.

The agent never gets to push; this module is the only writer. Branches
are `steward/<order id>` and force-pushed with lease, because the steward
owns them and a re-run after a failed push must not stack stale commits."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from steward import gh

AUTHOR = ("slopstopper-steward", "steward@slopstopper.dev")
LOCAL_ONLY = (".steward-result.json", ".steward-release-notes.md", ".steward-prompt.md", ".steward-venv/")


@dataclass
class Workspace:
    repo: str
    path: Path
    default_branch: str

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, check=check)

    def dirty(self) -> bool:
        return bool(self.git("status", "--porcelain").stdout.strip())

    def changed_files(self) -> list[str]:
        out = self.git("status", "--porcelain").stdout
        return sorted(line[3:].strip() for line in out.splitlines() if line.strip())


def clone(repo: str, default_branch: str, into: Path) -> Workspace:
    into.parent.mkdir(parents=True, exist_ok=True)
    result = gh.gh("repo", "clone", repo, str(into), "--", "--depth", "50", "--branch", default_branch, timeout=600)
    if result.returncode != 0:
        raise gh.GhError(f"clone {repo}: {result.stderr.strip()}")
    ws = Workspace(repo, into, default_branch)
    ws.git("config", "user.name", AUTHOR[0])
    ws.git("config", "user.email", AUTHOR[1])
    exclude = into / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(LOCAL_ONLY) + "\n")
    return ws


def branch(ws: Workspace, name: str) -> None:
    ws.git("checkout", "-B", name, f"origin/{ws.default_branch}")


def commit(ws: Workspace, message: str) -> bool:
    if not ws.dirty():
        return False
    ws.git("add", "-A")
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
