"""Regression tests for the third code review of the steward."""

import json
import subprocess
from pathlib import Path

import pytest

from steward import runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import refresh
from steward.inventory import Finding, Inventory
from steward.plan import plan_repo

CFG = from_dict({"orgs": ["acme"]})
CLI = Path("slopstopper")

LISTING = [
    {"name": "hygiene:complexity", "workflow": "ss-hygiene-complexity-check.yml", "disabled": False},
    {"name": "hygiene:docs-size", "workflow": "ss-hygiene-docs-size-check.yml", "disabled": True},
    {"name": "reliability:smoke", "workflow": "ss-reliability-smoke-tests.yml", "disabled": False},
    {"name": "security:licences", "workflow": "ss-security-licences-check.yml", "disabled": False},
    {"name": "security:sast", "workflow": "ss-security-sast-check.yml", "disabled": False},
    {"name": "security:secrets", "workflow": "ss-security-secrets-check.yml", "disabled": False},
]
EXPAND = {"profile": "library", "workflows": ["ss-reliability-smoke-tests.yml"]}
DOCTOR = {"ok": False, "exit_code": 1, "tools": [
    {"tool": "node", "needed_by": None, "status": "ok"},
    {"tool": "semgrep", "needed_by": "security:sast", "status": "missing"},
    {"tool": "gitleaks", "needed_by": "security:secrets", "status": "ok"},
]}


def _fake_cli(monkeypatch, answers, verdicts=None):
    """subprocess.run for the pinned CLI, printing indent=2 JSON as the real one does."""
    ran = []

    def fake_run(args, **kw):
        cmd = tuple(args[1:])
        if cmd[0] == "run":
            ran.append(cmd[-1])
            verdict = (verdicts or {}).get(cmd[-1], {"check": cmd[-1], "exit_code": 0, "status": "pass", "failures": [], "reports": []})
            return subprocess.CompletedProcess(args, verdict["exit_code"], json.dumps(verdict, indent=2) + "\n", "check output\n")
        key = cmd[:-1]  # drop --json
        code, payload = answers[key]
        return subprocess.CompletedProcess(args, code, json.dumps(payload, indent=2) + "\n", "")
    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    return ran


ANSWERS = {("checks", "list"): (0, LISTING), ("profile", "expand", "library"): (0, EXPAND), ("doctor",): (1, DOCTOR)}


# ── 1: indented JSON parses
def test_json_reads_the_whole_indented_output(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, ANSWERS)
    assert refresh._json(CLI, tmp_path, "checks", "list") == LISTING
    assert refresh._json(CLI, tmp_path, "doctor") is None  # exit 1 is not ok by default
    assert refresh._json(CLI, tmp_path, "doctor", ok_codes=(0, 1)) == DOCTOR


# ── 3 + 4 + 6: the list is the CLI's; missing tools and environment checks are unverified
def test_static_checks_from_the_cli(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, ANSWERS)
    checks = refresh.static_checks(workspace.Workspace("acme/a", tmp_path, "main"), CLI)
    assert checks.runnable == ["hygiene:complexity", "security:secrets"]
    assert {u["check"]: u["why"] for u in checks.unverified} == {
        "security:licences": "needs the project's installed dependencies; its workflow installs them",
        "security:sast": "`semgrep` is not installed on the steward's runner",
    }


def test_a_cli_that_cannot_list_is_loud(monkeypatch, tmp_path):
    _fake_cli(monkeypatch, {**ANSWERS, ("profile", "expand", "library"): (2, {"error": "no such command"})})
    with pytest.raises(refresh.CannotVerify):
        refresh.static_checks(workspace.Workspace("acme/a", tmp_path, "main"), CLI)


# ── 2: the verdict keeps its failures
def test_verify_keeps_the_verdicts_failures(monkeypatch, tmp_path):
    red_verdict = {"check": "hygiene:entry-files", "exit_code": 1, "status": "fail",
                   "failures": ["- ❌ AGENTS.md over budget"], "reports": [".ss/reports/entry-files/x.md"]}
    ran = _fake_cli(monkeypatch, ANSWERS, {"hygiene:entry-files": red_verdict})
    red = refresh.verify(workspace.Workspace("acme/a", tmp_path, "main"), CLI, ["hygiene:complexity", "hygiene:entry-files"])
    assert ran == ["hygiene:complexity", "hygiene:entry-files"]
    assert len(red) == 1 and red[0]["failures"] == ["- ❌ AGENTS.md over budget"] and red[0]["reports"]


# ── 5: an unlabelled marker issue from a person is reported, never silent
def test_untrusted_marker_issue_is_reported():
    inv = Inventory(Repo("acme/a", pinned="0.27.0", installed_from="0.27.0", evidence=["mise-pin"]), "0.27.0")
    inv.untrusted = [Finding(7, "x", "u7", [], check="hygiene:docs-size", author="someone")]
    p = plan_repo(inv, None, CFG)
    assert p.orders == [] and p.skipped[0]["order"] == "triage-7" and "someone" in p.skipped[0]["why"]
    assert "slopstopper` label" in p.skipped[0]["why"]


def test_inventory_splits_untrusted(monkeypatch):
    from steward import inventory
    issues = [{"number": 1, "title": "t", "html_url": "u", "labels": [], "user": {"login": "pat-user", "type": "User"},
               "body": "<!-- slopstopper:check=hygiene:docs-size -->"},
              {"number": 2, "title": "t", "html_url": "u", "labels": [], "user": {"login": "github-actions[bot]", "type": "Bot"},
               "body": "<!-- slopstopper:check=hygiene:docs-size -->"},
              {"number": 3, "title": "plain", "html_url": "u", "labels": [], "user": {"login": "x", "type": "User"}, "body": ""}]
    monkeypatch.setattr(inventory.gh, "api", lambda path, **kw: issues)
    found, untrusted = inventory._findings(Repo("acme/a"), CFG)
    assert [f.number for f in found] == [2] and [f.number for f in untrusted] == [1]


# ── 7: the release cache belongs to one run
def test_release_notes_cache_is_the_callers(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.gh, "api", lambda path, **kw: calls.append(path) or [
        {"tag_name": "v0.27.0", "name": "0.27.0", "body": "notes"}, {"tag_name": "v0.26.0", "name": "0.26.0", "body": "old"}])
    cache: dict = {}
    assert "notes" in runner.release_notes(CFG, "0.26.0", "0.27.0", cache)
    assert "old" not in runner.release_notes(CFG, "0.26.0", "0.27.0", cache)
    assert len(calls) == 1
    runner.release_notes(CFG, "0.26.0", "0.27.0", {})  # a new run fetches again
    assert len(calls) == 2
    assert not hasattr(runner, "_RELEASES")


# ── 8: one listing per verification pass
def test_one_listing_per_pass(monkeypatch, tmp_path):
    listings = []
    monkeypatch.setattr(runner.refresh, "static_checks",
                        lambda ws, cli: listings.append(1) or refresh.Checks(["hygiene:complexity"], []))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    red, unverified = runner._verify_pass(workspace.Workspace("acme/a", tmp_path, "main"), CLI)
    assert (red, unverified, len(listings)) == ([], [], 1)
