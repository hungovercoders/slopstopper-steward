"""Which repos in an organisation have slopstopper installed.

Three signals, each one the installer itself leaves behind, strongest
first: the `slopstopper` repo topic (`slopstopper install` adds it through
gh when it can), the `"pipx:slopstopper-cli"` pin in `mise.toml` (what CI
runs), and `.ss/.installed-from` (the CLI version that wrote the suite).
The topic alone is not trusted: topics need admin rights, so an install
run by a contributor leaves the repo discoverable only by its files. Every
non-archived repo is probed for the two files, two cheap contents calls
each, so a repo whose topic never landed is still found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from steward import gh
from steward.config import Config

TOPIC = "slopstopper"
PIN_KEY = "pipx:slopstopper-cli"
INSTALLED_FROM = ".ss/.installed-from"
_PIN = re.compile(rf'^\s*"?{re.escape(PIN_KEY)}"?\s*=\s*"([^"]*)"', re.M)


@dataclass
class Repo:
    full_name: str
    default_branch: str = "main"
    archived: bool = False
    topics: list[str] = field(default_factory=list)
    pinned: str | None = None
    installed_from: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def adopter(self) -> bool:
        """Installed means the files are there. The topic alone is a hint
        (it needs admin rights to set and survives an uninstall), never a
        reason to write a suite into a repo."""
        return "mise-pin" in self.evidence or "installed-from" in self.evidence

    def to_dict(self) -> dict:
        return {
            "repo": self.full_name, "default_branch": self.default_branch, "topics": self.topics,
            "pinned": self.pinned, "installed_from": self.installed_from, "evidence": self.evidence,
        }


def read_pin(mise_toml: str | None) -> str | None:
    if not mise_toml:
        return None
    match = _PIN.search(mise_toml)
    return match.group(1).strip() or None if match else None


def org_repos(org: str) -> list[dict]:
    rows = gh.api(f"orgs/{org}/repos?type=all&per_page=100", paginate=True)
    if rows is None:
        rows = gh.api(f"users/{org}/repos?type=owner&per_page=100", paginate=True) or []
    return rows


def probe(repo: Repo) -> Repo:
    """Fill the file-based evidence for one repo."""
    mise = gh.contents(repo.full_name, "mise.toml", repo.default_branch)
    repo.pinned = read_pin(mise)
    if repo.pinned:
        repo.evidence.append("mise-pin")
    marker = gh.contents(repo.full_name, INSTALLED_FROM, repo.default_branch)
    if marker is not None:
        repo.installed_from = marker.strip() or None
        repo.evidence.append("installed-from")
    return repo


def discover(cfg: Config, only: str | None = None) -> list[Repo]:
    """Every adopter across the configured orgs. `only` limits the probe to
    one `owner/name`, which `steward run --repo` uses."""
    found: list[Repo] = []
    for org in cfg.orgs:
        for row in org_repos(org):
            full = row["full_name"]
            if only and full != only:
                continue
            if row.get("archived") or not cfg.allows(full):
                continue
            repo = Repo(full, row.get("default_branch") or "main", False, list(row.get("topics") or []))
            if TOPIC in repo.topics:
                repo.evidence.append("topic")
            probe(repo)
            if repo.adopter:
                found.append(repo)
    return found
