"""Dependabot's open pull requests in an adopter, and how far each one
moves a dependency. The steward works a red one the way it settles its
own PRs' checks: a fix round from the failing jobs' logs, pushed onto
Dependabot's branch, then the wait again. It never merges one (step two,
not built) and never rewrites Dependabot's body."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BOT = "dependabot[bot]"
LEVELS = ("patch", "minor", "major")
# `Bump <dep> from <old> to <new>`, with or without a commit-message prefix
TITLE = re.compile(r"[Bb]umps? (?P<dep>\S+) from (?P<old>\S+) to (?P<new>\S+)")
# a grouped PR's body: one `Updates `<dep>` from <old> to <new>` per dependency
UPDATE = re.compile(r"Updates? `(?P<dep>[^`]+)` from (?P<old>\S+) to (?P<new>\S+)")
WORKED_MARKER = re.compile(r"<!--\s*steward:worked=([0-9a-f]{7,40})\s*-->")
_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass
class Bump:
    number: int
    title: str
    url: str
    head: str           # Dependabot's branch
    sha: str            # its tip when the inventory was taken
    body: str
    labels: list[str] = field(default_factory=list)
    updates: list[tuple[str, str, str]] = field(default_factory=list)   # (dependency, from, to)
    worked: bool = False   # a steward comment names this very head: it had its turn

    @property
    def level(self) -> str | None:
        """How far the PR moves things: the biggest step among its updates,
        None when any of them cannot be read as a version (a digest, a
        date) or the PR names none."""
        steps = [level(old, new) for _, old, new in self.updates]
        if not steps or None in steps:
            return None
        return max(steps, key=LEVELS.index)  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "url": self.url, "head": self.head, "sha": self.sha,
                "level": self.level, "updates": [list(u) for u in self.updates], "worked": self.worked}


def version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(text.strip().rstrip(".,"))
    if not match:
        return None
    return tuple(int(g or 0) for g in match.groups())  # type: ignore[return-value]


def level(old: str, new: str) -> str | None:
    a, b = version(old), version(new)
    if a is None or b is None:
        return None
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return "patch"


def updates(title: str, body: str) -> list[tuple[str, str, str]]:
    """The dependencies a PR moves. The title names a single bump; a
    grouped PR's title names only the group, and its body lists each one."""
    match = TITLE.search(title)
    if match:
        return [match.group("dep", "old", "new")]
    return [m.group("dep", "old", "new") for m in UPDATE.finditer(body)]


def worked(comments: list[dict], sha: str) -> bool:
    """Whether a comment on the PR carries the steward's marker for `sha`."""
    return any(WORKED_MARKER.search(c.get("body") or "") and sha in WORKED_MARKER.findall(c.get("body") or "")
               for c in comments)


def from_pr(pr: dict) -> Bump | None:
    """A Bump for an open PR of Dependabot's own, from the same repository
    and not a draft; None for any other PR."""
    if not _dependabots(pr):
        return None
    head, title, body = pr["head"], pr.get("title") or "", pr.get("body") or ""
    return Bump(pr["number"], title, pr.get("html_url", ""), head["ref"], head.get("sha") or "", body,
                _labels(pr), updates(title, body))


def _dependabots(pr: dict) -> bool:
    user, head, base = pr.get("user") or {}, pr.get("head") or {}, pr.get("base") or {}
    same_repo = (head.get("repo") or {}).get("full_name") == (base.get("repo") or {}).get("full_name")
    return user.get("login") == BOT and not pr.get("draft") and same_repo and bool(head.get("ref"))


def _labels(pr: dict) -> list[str]:
    return [l["name"] if isinstance(l, dict) else str(l) for l in pr.get("labels") or []]
