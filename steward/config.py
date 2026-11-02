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
    ci.wait                  After opening a PR, wait for its checks and fix what the triage
                             skill covers; a PR that stays red is a draft. Default true.
    ci.wait_minutes          The most one wait for the PR's checks may take. Default 20.
    ci.fix_rounds            Agent attempts at a red PR before it is drafted. Default 2.
    agent.enabled            false disables every agentic order; upgrades still run.
    agent.model              Model name handed to claude --model (empty: the CLI default).
    agent.self_review        After an agent run that changed the tree, a second, read-only run
                             reviews the pending diff; changes it asks for get one correction
                             round. A review left unanswered makes the PR a draft. Default true.
    merge.auto               Merge a steward PR itself once its checks are green and the run
                             has nothing left against it (no draft, no red, no open review).
                             Default false.
    merge.kinds              Which order kinds may be merged: upgrade, refresh, triage.
                             Default [upgrade, refresh].
    merge.method             squash | merge | rebase. Default squash.
    merge.delete_branch      Delete the steward branch after the merge. Default true.
    dependabot.enabled       Work Dependabot's open PRs too: wait for each one's checks and,
                             red, fix on Dependabot's own branch within `ci.fix_rounds`. The
                             steward never merges one. Default false.
    dependabot.updates       Which bumps it may work: patch, minor, major. Default [patch, minor].
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
MERGE_METHODS = ("squash", "merge", "rebase")
ORDER_KINDS = ("upgrade", "refresh", "triage")   # the kinds merge.kinds may name: a Dependabot PR is never merged
LEVELS = ("patch", "minor", "major")


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
    ci_wait: bool = True
    ci_wait_minutes: int = 20
    ci_fix_rounds: int = 2
    agent_model: str = ""
    self_review: bool = True
    merge_auto: bool = False
    merge_kinds: list[str] = field(default_factory=lambda: ["upgrade", "refresh"])
    merge_method: str = "squash"
    merge_delete_branch: bool = True
    dependabot_enabled: bool = False
    dependabot_updates: list[str] = field(default_factory=lambda: ["patch", "minor"])
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
    ci, merge, bot = _section(data, "ci"), _section(data, "merge"), _section(data, "dependabot")
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
        ci_wait=ci.get("wait") is not False,
        ci_wait_minutes=_num(ci, "wait_minutes", 20, int),
        ci_fix_rounds=_num(ci, "fix_rounds", 2, int),
        agent_model=str(agent.get("model") or ""),
        self_review=agent.get("self_review") is not False,
        **_merge(merge),
        dependabot_enabled=bot.get("enabled") is True,
        dependabot_updates=_listof(bot.get("updates"), "dependabot.updates") if bot.get("updates") is not None
        else ["patch", "minor"],
        pr_labels=_listof(labels.get("pr"), "labels.pr") or ["slopstopper", "steward"],
        ignore_label=str(labels.get("ignore") or "steward:ignore"),
        slopstopper_repo=str(ss.get("repo") or "hungovercoders/slopstopper"),
        package=str(ss.get("package") or "slopstopper-cli"),
    )
    _check(cfg)
    return cfg


def _merge(section: dict) -> dict:
    """The `merge:` keys. `auto` is on only for a real `true`."""
    kinds = section.get("kinds")
    return {
        "merge_auto": section.get("auto") is True,
        "merge_kinds": _listof(kinds, "merge.kinds") if kinds is not None else ["upgrade", "refresh"],
        "merge_method": str(section.get("method") or "squash"),
        "merge_delete_branch": section.get("delete_branch") is not False,
    }


def _check(cfg: Config) -> None:
    if cfg.mode not in MODES:
        raise ValueError(f"steward.yml: mode must be one of {MODES}, not {cfg.mode!r}")
    if cfg.policy not in POLICIES:
        raise ValueError(f"steward.yml: upgrade.policy must be one of {POLICIES}, not {cfg.policy!r}")
    if cfg.merge_method not in MERGE_METHODS:
        raise ValueError(f"steward.yml: merge.method must be one of {MERGE_METHODS}, not {cfg.merge_method!r}")
    odd = [k for k in cfg.merge_kinds if k not in ORDER_KINDS]
    if odd:
        raise ValueError(f"steward.yml: merge.kinds must be among {ORDER_KINDS}, not {odd}")
    odd = [k for k in cfg.dependabot_updates if k not in LEVELS]
    if odd:
        raise ValueError(f"steward.yml: dependabot.updates must be among {LEVELS}, not {odd}")


def load(path: Path = DEFAULT_PATH) -> Config:
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: copy steward.yml.example and name your orgs")
    return from_dict(yamlish.load(path))
