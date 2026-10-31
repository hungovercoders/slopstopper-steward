"""Inventory in, work orders out. Pure: nothing here touches GitHub.

Order kinds, in the priority they are worked:
  upgrade   the pin is behind the policy's target: move it and rewrite the suite;
            a big jump (`upgrade.review_minor_gap`) also gets an agent review
  refresh   the pin is current but the suite on disk was written by an older CLI
  triage    an open finding slopstopper raised, security ones first

A repo gets at most `budgets.prs_per_repo` open steward PRs; an order whose
id already has an open PR is dropped, so a re-run is idempotent. Orders a
budget leaves out are kept as `deferred`, so the report says what waits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from steward.config import Config
from steward.inventory import Finding, Inventory
from steward.versions import MIN_INSTALL_VERSION, behind, parse


@dataclass
class Order:
    id: str
    kind: str
    repo: str
    title: str
    reason: str
    priority: int
    default_branch: str = "main"
    target: str | None = None
    pinned: str | None = None
    finding: Finding | None = None
    agentic: bool = False
    details: dict = field(default_factory=dict)

    @property
    def branch(self) -> str:
        return f"steward/{self.id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "repo": self.repo, "title": self.title, "reason": self.reason,
            "priority": self.priority, "branch": self.branch, "target": self.target, "pinned": self.pinned,
            "finding": self.finding.to_dict() if self.finding else None, "agentic": self.agentic, **self.details,
        }


@dataclass
class Plan:
    orders: list[Order] = field(default_factory=list)
    deferred: list[Order] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"orders": [o.to_dict() for o in self.orders], "deferred": [o.to_dict() for o in self.deferred],
                "skipped": self.skipped}


def big_jump(start: str | None, target: str | None, gap: int) -> str | None:
    """Why moving from `start` to `target` is big enough for an agent review
    of the refresh, or None. A suite with no known starting version counts:
    nothing says what it carried."""
    if gap <= 0 or not target:
        return None
    lo, hi = parse(start), parse(target)
    if lo is None or hi is None:
        return f"no known starting version ({start or 'unpinned'}) to {target}"
    if hi[0] != lo[0]:
        return f"a major version: {start} to {target}"
    if hi[1] - lo[1] >= gap:
        return f"{hi[1] - lo[1]} minor versions: {start} to {target}"
    return None


def _upgrade_order(inv: Inventory, target: str | None, gap: int = 0) -> Order | None:
    order = _pin_order(inv, target)
    if order:
        start = order.pinned if order.kind == "upgrade" else inv.repo.installed_from
        review = big_jump(start, order.target, gap)
        if review:
            order.details["review"] = review
    return order


def _pin_order(inv: Inventory, target: str | None) -> Order | None:
    repo = inv.repo
    if target and behind(target, MIN_INSTALL_VERSION):
        return None  # plan_repo records why
    if target and behind(repo.pinned, target):
        reason = (f"pin {repo.pinned} is behind {target}" if repo.pinned
                  else f"no pipx:slopstopper-cli pin in mise.toml; {target} is the target")
        return Order(f"upgrade-{target}", "upgrade", repo.full_name,
                     f"chore(slopstopper): move the CLI pin to {target}", reason, 0,
                     repo.default_branch, target, repo.pinned)
    if repo.pinned and repo.installed_from and parse(repo.installed_from) and parse(repo.pinned) \
            and not behind(repo.pinned, MIN_INSTALL_VERSION) \
            and parse(repo.installed_from) < parse(repo.pinned):  # type: ignore[operator]
        return Order(f"refresh-{repo.pinned}", "refresh", repo.full_name,
                     f"chore(slopstopper): refresh the suite from slopstopper-cli {repo.pinned}",
                     f"the suite was written by {repo.installed_from} but the pin is {repo.pinned}", 1,
                     repo.default_branch, repo.pinned, repo.pinned)
    return None


def _triage_orders(inv: Inventory) -> list[Order]:
    orders = []
    for finding in sorted(inv.findings, key=lambda f: (not f.security, f.number)):
        if finding.kind == "other":
            continue  # plan_repo records it: a labelled issue with no check behind it (an adopter report, a note)
        what = finding.check or finding.workflow or "finding"
        orders.append(Order(
            f"triage-{finding.number}", "triage", inv.repo.full_name,
            f"fix(slopstopper): {what} (#{finding.number})",
            f"open issue #{finding.number}: {finding.title}", 2 if finding.security else 3,
            inv.repo.default_branch, inv.repo.pinned, inv.repo.pinned, finding, agentic=True,
        ))
    return orders


PIN_KINDS = ("upgrade", "refresh")


def _open_pin_pr(inv: Inventory) -> str | None:
    """The id of an open upgrade/refresh PR, whatever its target: the pin
    moves through one PR at a time, so a newer release never opens a second
    one beside a stale one (two pin PRs can merge in the wrong order)."""
    for order_id in inv.open_orders:
        if order_id.split("-", 1)[0] in PIN_KINDS:
            return order_id
    return None


def _stale_suite_below_floor(inv: Inventory) -> bool:
    repo = inv.repo
    if not (repo.pinned and repo.installed_from and parse(repo.installed_from) and parse(repo.pinned)):
        return False
    return behind(repo.pinned, MIN_INSTALL_VERSION) and parse(repo.installed_from) < parse(repo.pinned)  # type: ignore[operator]


def _unworkable(inv: Inventory, target: str | None) -> list[dict]:
    """What the repo needs that no order can do, as report rows."""
    repo, rows = inv.repo, []
    if target and behind(target, MIN_INSTALL_VERSION):
        rows.append({"repo": repo.full_name, "order": f"upgrade-{target}",
                     "why": f"{target} predates `slopstopper install` ({MIN_INSTALL_VERSION}+); nothing to move to yet"})
    elif _stale_suite_below_floor(inv):
        rows.append({"repo": repo.full_name, "order": f"refresh-{repo.pinned}",
                     "why": f"suite written by {repo.installed_from}, but {repo.pinned} has no "
                            f"`slopstopper install` to rewrite it ({MIN_INSTALL_VERSION}+)"})
    for finding in inv.untrusted:
        rows.append({"repo": repo.full_name, "order": f"triage-{finding.number}",
                     "why": f"#{finding.number} carries a slopstopper marker but no label, and {finding.author or 'a person'} "
                            "opened it, not a bot; add the `slopstopper` label to have it worked",
                     "url": finding.url})
    for finding in (f for f in inv.findings if f.kind == "other"):
        rows.append({"repo": repo.full_name, "order": f"triage-{finding.number}",
                     "why": f"#{finding.number} carries the label but names no check or workflow; nothing to reproduce",
                     "url": finding.url})
    return rows


def plan_repo(inv: Inventory, target: str | None, cfg: Config) -> Plan:
    plan = Plan(skipped=_unworkable(inv, target))
    candidates: list[Order] = []
    upgrade = _upgrade_order(inv, target, cfg.review_minor_gap)
    if upgrade:
        candidates.append(upgrade)
    candidates += _triage_orders(inv)
    budget = max(0, cfg.prs_per_repo - len(inv.open_orders))
    for order in sorted(candidates, key=lambda o: o.priority):
        if order.id in inv.open_orders:
            plan.skipped.append({"repo": order.repo, "order": order.id, "why": "open PR", **inv.open_orders[order.id]})
            continue
        stale = _open_pin_pr(inv) if order.kind in PIN_KINDS else None
        if stale:
            plan.skipped.append({"repo": order.repo, "order": order.id,
                                 "why": f"open PR for `{stale}` moves the pin already; merge or close it before a new target",
                                 **inv.open_orders[stale]})
            continue
        if order.agentic and not cfg.agent_enabled:
            plan.skipped.append({"repo": order.repo, "order": order.id, "why": "agent disabled"})
            continue
        if budget <= 0:
            plan.deferred.append(order)
            continue
        plan.orders.append(order)
        budget -= 1
    for w in inv.disabled_workflows:
        plan.skipped.append({"repo": inv.repo.full_name, "order": f"enable-{w['path'].rsplit('/', 1)[-1]}",
                             "why": f"workflow {w['state']}: needs `gh workflow enable` by an admin"})
    return plan


def plan_all(inventories: list[Inventory], targets: dict[str, str | None], cfg: Config) -> Plan:
    """Across repos, worst-off first: security findings, then furthest behind."""
    merged = Plan()
    per_repo = []
    for inv in inventories:
        p = plan_repo(inv, targets.get(inv.repo.full_name), cfg)
        merged.skipped += p.skipped
        merged.deferred += p.deferred
        if p.orders:
            per_repo.append((min(o.priority for o in p.orders), inv.repo.full_name, p.orders))
    per_repo.sort(key=lambda t: (t[0], t[1]))
    for i, (_, _, orders) in enumerate(per_repo):
        if i < cfg.repos_per_run:
            merged.orders += orders
        else:
            merged.deferred += orders
    return merged
