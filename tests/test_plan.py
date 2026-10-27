from steward.config import from_dict
from steward.discover import Repo
from steward.inventory import Finding, Inventory
from steward.plan import plan_all, plan_repo


def _inv(name="acme/a", pinned="0.24.0", installed_from="0.24.0", findings=(), open_orders=None, disabled=()):
    inv = Inventory(Repo(name, pinned=pinned, installed_from=installed_from, evidence=["mise-pin"]), "0.26.0")
    inv.findings = list(findings)
    inv.open_orders = dict(open_orders or {})
    inv.disabled_workflows = list(disabled)
    return inv


def _finding(n, check=None, kind="check", labels=()):
    return Finding(n, f"issue {n}", f"u{n}", list(labels), check=check, kind=kind)


CFG = from_dict({"orgs": ["acme"]})


def test_upgrade_first_then_security_then_rest_within_budget():
    inv = _inv(findings=[_finding(1, "hygiene:docs-size"), _finding(2, "security:sast")])
    p = plan_repo(inv, "0.27.0", CFG)
    assert [o.id for o in p.orders] == ["upgrade-0.27.0", "triage-2"]
    assert [o.id for o in p.deferred] == ["triage-1"]
    assert p.orders[0].branch == "steward/upgrade-0.27.0" and p.orders[1].details["static"] is False


def test_open_pr_dedupes_and_counts_against_budget():
    inv = _inv(findings=[_finding(1, "hygiene:docs-size")], open_orders={"upgrade-0.27.0": {"number": 5, "url": "p"}})
    p = plan_repo(inv, "0.27.0", CFG)
    assert [o.id for o in p.orders] == ["triage-1"]
    assert p.skipped[0]["why"] == "open PR"


def test_refresh_when_suite_older_than_pin():
    p = plan_repo(_inv(pinned="0.26.0", installed_from="0.22.0"), None, CFG)
    assert [o.kind for o in p.orders] == ["refresh"] and p.orders[0].target == "0.26.0"


def test_current_repo_with_no_findings_is_quiet():
    assert plan_repo(_inv(pinned="0.26.0", installed_from="0.26.0"), None, CFG).orders == []


def test_unpinned_legacy_install_upgrades():
    p = plan_repo(_inv(pinned=None, installed_from=None), "0.27.0", CFG)
    assert p.orders[0].kind == "upgrade" and "no pipx" in p.orders[0].reason


def test_agent_disabled_skips_triage_only():
    cfg = from_dict({"orgs": ["acme"], "agent": {"enabled": False}})
    p = plan_repo(_inv(findings=[_finding(1, "security:sast")]), "0.27.0", cfg)
    assert [o.id for o in p.orders] == ["upgrade-0.27.0"] and p.skipped[0]["why"] == "agent disabled"


def test_disabled_workflows_are_reported_not_worked():
    p = plan_repo(_inv(pinned="0.26.0", installed_from="0.26.0", disabled=[{"name": "x", "path": ".github/workflows/ss-x.yml", "state": "disabled_inactivity"}]), None, CFG)
    assert p.orders == [] and "gh workflow enable" in p.skipped[0]["why"]


def test_plan_all_orders_repos_worst_first_and_applies_repo_budget():
    cfg = from_dict({"orgs": ["acme"], "budgets": {"repos_per_run": 1}})
    quiet = _inv("acme/quiet", pinned="0.26.0", installed_from="0.26.0")
    behind = _inv("acme/behind")
    hot = _inv("acme/hot", pinned="0.26.0", installed_from="0.26.0", findings=[_finding(3, "security:secrets")])
    p = plan_all([quiet, behind, hot], {"acme/quiet": None, "acme/behind": "0.27.0", "acme/hot": None}, cfg)
    assert [o.repo for o in p.orders] == ["acme/behind"]  # priority 0 beats a security triage (2)
    assert [o.repo for o in p.deferred] == ["acme/hot"]


def test_target_below_install_floor_is_reported_not_worked():
    p = plan_repo(_inv(pinned="0.20.0", installed_from="0.20.0"), "0.26.0", CFG)
    assert p.orders == [] and "predates" in p.skipped[0]["why"]
