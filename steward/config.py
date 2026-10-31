"""`steward.yml`: which organisations, how far to go, how much to spend.

Configuration
    orgs                     GitHub organisations (or users) to steward. Required.
    repos.include            Only these repos (owner/name) are considered when set.
    repos.exclude            Repos never touched (owner/name).
    mode                     plan | pr. `plan` reads and reports, `pr` also clones,
                             fixes and opens pull requests. Default pr; `--dry-run`
                             forces plan for one run.
    upgrade.policy           latest | minor | patch: how far a pin may move in one PR.
    upgrade.min_age_days     Skip a release younger than this (lets a bad release be yanked).
    upgrade.review           true: every upgrade and refresh also gets an agent review of
                             the refresh, the install skill's judgement steps. Default true.
    budgets.repos_per_run    Repos worked per run (plan still covers all).
    budgets.prs_per_repo     Open steward PRs per repo, upgrades included.
    budgets.agent_usd        Dollar cap per agent invocation (claude --max-budget-usd).
    budgets.agent_turns      Turn cap per agent invocation.
    agent.enabled            false disables every agentic order; upgrades still run.
    agent.model              Model name handed to claude --model (empty: the CLI default).
    labels.pr                Labels on every steward PR.
    labels.ignore            An issue carrying this label is never picked up.
    slopstopper.repo         Where slopstopper lives (bugs go here; skills are fetched here).
    slopstopper.package      The PyPI name the pin names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from steward import yamlish

DEFAULT_PATH = Path("steward.yml")
MODES = ("plan", "pr")
POLICIES = ("latest", "minor", "patch")


@dataclass
class Config:
    orgs: list[str]
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    mode: str = "pr"
    policy: str = "latest"
    min_age_days: int = 2
    review: bool = True
    repos_per_run: int = 5
    prs_per_repo: int = 2
    agent_usd: float = 5.0
    agent_turns: int = 60
    agent_enabled: bool = True
    agent_model: str = ""
    pr_labels: list[str] = field(default_factory=lambda: ["slopstopper", "steward"])
    ignore_label: str = "steward:ignore"
    slopstopper_repo: str = "hungovercoders/slopstopper"
    package: str = "slopstopper-cli"

    def allows(self, full_name: str) -> bool:
        if full_name in self.exclude:
            return False
        return not self.include or full_name in self.include


def _section(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"steward.yml: `{key}` must be a map")
    return value


def _num(section: dict, key: str, default, kind):
    """A number with its default when the key is absent or left blank."""
    value = section.get(key)
    if value is None or value == "":
        return default
    try:
        return kind(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"steward.yml: `{key}` must be a number, not {value!r}") from exc


def _listof(value: object, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ValueError(f"steward.yml: `{key}` must be a list")


def from_dict(data: dict) -> Config:
    orgs = _listof(data.get("orgs"), "orgs")
    if not orgs:
        raise ValueError("steward.yml: `orgs` must name at least one organisation")
    repos, upgrade = _section(data, "repos"), _section(data, "upgrade")
    budgets, agent, labels, ss = (
        _section(data, "budgets"), _section(data, "agent"), _section(data, "labels"), _section(data, "slopstopper"),
    )
    cfg = Config(
        orgs=orgs,
        include=_listof(repos.get("include"), "repos.include"),
        exclude=_listof(repos.get("exclude"), "repos.exclude"),
        mode=str(data.get("mode") or "pr"),
        policy=str(upgrade.get("policy") or "latest"),
        min_age_days=_num(upgrade, "min_age_days", 2, int),
        review=upgrade.get("review") is not False,
        repos_per_run=_num(budgets, "repos_per_run", 5, int),
        prs_per_repo=_num(budgets, "prs_per_repo", 2, int),
        agent_usd=_num(budgets, "agent_usd", 5.0, float),
        agent_turns=_num(budgets, "agent_turns", 60, int),
        agent_enabled=agent.get("enabled") is not False,
        agent_model=str(agent.get("model") or ""),
        pr_labels=_listof(labels.get("pr"), "labels.pr") or ["slopstopper", "steward"],
        ignore_label=str(labels.get("ignore") or "steward:ignore"),
        slopstopper_repo=str(ss.get("repo") or "hungovercoders/slopstopper"),
        package=str(ss.get("package") or "slopstopper-cli"),
    )
    if cfg.mode not in MODES:
        raise ValueError(f"steward.yml: mode must be one of {MODES}, not {cfg.mode!r}")
    if cfg.policy not in POLICIES:
        raise ValueError(f"steward.yml: upgrade.policy must be one of {POLICIES}, not {cfg.policy!r}")
    return cfg


def load(path: Path = DEFAULT_PATH) -> Config:
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: copy steward.yml.example and name your orgs")
    return from_dict(yamlish.load(path))
