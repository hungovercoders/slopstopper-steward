"""Regression tests for the second code review of the steward."""

import subprocess
from pathlib import Path

from steward import discover, inventory, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import refresh
from steward.inventory import Inventory
from steward.plan import Order, plan_repo

CFG = from_dict({"orgs": ["acme"]})


# ── 10: one regex for the pin line
def test_pin_regex_is_shared():
    assert refresh.PIN_LINE is discover.PIN_LINE
    assert discover.read_pin('[tools]\n"pipx:slopstopper-cli" = "0.27.0"\n') == "0.27.0"


# ── 1 + 7: the static set comes from the pinned CLI, licences never
def test_static_checks_derived_from_profile_expand(monkeypatch, tmp_path):
    answers = {
        ("checks", "list"): [
            {"name": "hygiene:complexity", "workflow": "ss-hygiene-complexity-check.yml", "disabled": False},
            {"name": "hygiene:docs-size", "workflow": "ss-hygiene-docs-size-check.yml", "disabled": True},
            {"name": "reliability:smoke", "workflow": "ss-reliability-smoke-tests.yml", "disabled": False},
            {"name": "security:licences", "workflow": "ss-security-licences-check.yml", "disabled": False},
            {"name": "security:sast", "workflow": "ss-security-sast-check.yml", "disabled": False},
        ],
        ("profile", "expand", "library"): {"profile": "library", "workflows": ["ss-reliability-smoke-tests.yml"]},
    }
    monkeypatch.setattr(refresh, "_json", lambda cli, cwd, *args: answers.get(args))
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    assert refresh.static_checks(ws, Path("slopstopper")) == ["hygiene:complexity", "security:sast"]


def test_static_checks_fallback_without_profile_expand(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh, "_json", lambda cli, cwd, *args: None)
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    assert "security:licences" not in refresh.static_checks(ws, Path("slopstopper"))
    assert "hygiene:complexity" in refresh.static_checks(ws, Path("slopstopper"))


def test_verify_runs_only_the_checks_asked(monkeypatch, tmp_path):
    ran = []

    def fake_run(args, **kw):
        ran.append(args[-1])
        return subprocess.CompletedProcess(args, 0, '{"check": "%s", "exit_code": 0, "status": "pass", "failures": []}' % args[-1], "")
    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    assert refresh.verify(ws, Path("slopstopper"), ["hygiene:docs-size"]) == []
    assert ran == ["hygiene:docs-size"]


# ── 2: a bare marker needs a bot author
def test_marker_from_a_person_is_not_a_finding():
    stranger = inventory.classify({"number": 1, "title": "x", "html_url": "u", "labels": [],
                                   "user": {"login": "someone", "type": "User"},
                                   "body": "<!-- slopstopper:check=hygiene:docs-size -->"})
    bot = inventory.classify({**{"number": 2, "title": "x", "html_url": "u", "labels": [],
                                 "body": "<!-- slopstopper:check=hygiene:docs-size -->"},
                              "user": {"login": "github-actions[bot]", "type": "Bot"}})
    labelled = inventory.classify({"number": 3, "title": "x", "html_url": "u", "labels": [{"name": "slopstopper"}],
                                   "user": {"login": "someone", "type": "User"}, "body": ""})
    assert not inventory._is_slopstoppers(stranger)
    assert inventory._is_slopstoppers(bot) and bot.author == "github-actions[bot]"
    assert inventory._is_slopstoppers(labelled) and labelled.kind == "other"


# ── 4: one pin PR at a time
def test_newer_target_does_not_open_a_second_pin_pr():
    inv = Inventory(Repo("acme/a", pinned="0.24.0", installed_from="0.24.0", evidence=["mise-pin"]), "0.28.0")
    inv.open_orders = {"upgrade-0.27.0": {"number": 5, "url": "p"}}
    p = plan_repo(inv, "0.28.0", CFG)
    assert p.orders == [] and p.skipped[0]["order"] == "upgrade-0.28.0" and "upgrade-0.27.0" in p.skipped[0]["why"]


# ── 6: the triage CLI respects the floor
def _order(pinned):
    from steward.inventory import Finding
    return Order("triage-1", "triage", "acme/a", "t", "r", 3, "main", pinned, pinned,
                 Finding(1, "x", "u", [], check="hygiene:docs-size"), True)


def test_verifier_version_floor():
    inv = Inventory(Repo("acme/a"), "0.28.0")
    assert runner._verifier_version(_order("0.27.1"), inv) == "0.27.1"
    assert runner._verifier_version(_order("0.26.0"), inv) == "0.28.0"
    assert runner._verifier_version(_order("latest"), inv) == "0.28.0"
    assert runner._verifier_version(_order("0.26.0"), Inventory(Repo("acme/a"), "0.26.0")) is None


# ── 9: releases fetched once per run
def test_release_notes_fetches_once(monkeypatch):
    calls = []
    runner._RELEASES.clear()
    monkeypatch.setattr(runner.gh, "api", lambda path, **kw: calls.append(path) or [
        {"tag_name": "v0.27.0", "name": "0.27.0", "body": "notes"}, {"tag_name": "v0.26.0", "name": "0.26.0", "body": "old"}])
    assert "notes" in runner.release_notes(CFG, "0.26.0", "0.27.0")
    assert "old" not in runner.release_notes(CFG, "0.26.0", "0.27.0")
    assert len(calls) == 1
    runner._RELEASES.clear()


# ── 3 + 5: excluded paths are never staged; a stale clone dir is replaced
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_excluded_paths_survive_a_gitignore_negation(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".claude/*\n!.claude/\n!.claude/skills/\n!.claude/skills/**\n")
    (repo / "README.md").write_text("# r\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    ws = workspace.prepare(workspace.Workspace("acme/r", repo, "main"))  # the identity a clone gets
    ws.excluded.append(".claude/skills/slopstopper-triage")
    skill = repo / ".claude" / "skills" / "slopstopper-triage"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("fetched\n")
    assert not ws.dirty()  # the fetched skill alone is not a change
    (repo / "mise.toml").write_text("pin\n")
    assert ws.changed_files() == ["mise.toml"]
    assert workspace.commit(ws, "work")
    tracked = _git(repo, "ls-files").stdout.split()
    assert "mise.toml" in tracked and not any(t.startswith(".claude/skills") for t in tracked)


def test_clone_replaces_a_stale_directory(monkeypatch, tmp_path):
    into = tmp_path / "acme__a" / "upgrade-0.27.0"
    into.mkdir(parents=True)
    (into / "stale").write_text("x")
    seen = {}

    def fake_gh(*args, **kw):
        seen["exists_at_clone"] = into.exists()
        into.mkdir(parents=True)
        _git(into, "init", "-q", "-b", "main")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(workspace.gh, "gh", fake_gh)
    ws = workspace.clone("acme/a", "main", into)
    assert seen["exists_at_clone"] is False and not (into / "stale").exists() and ws.path == into
