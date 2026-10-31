"""Every upgrade gets an agent review of the refresh unless upgrade.review is off."""

import subprocess
from pathlib import Path

import pytest

from steward import plan, prompts, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import refresh
from steward.execute.agent import AgentResult
from steward.inventory import Inventory

CFG = from_dict({"orgs": ["acme"]})


@pytest.mark.parametrize("start, expect", [
    ("0.26.0", "0.26.0 to 0.27.0"),                     # one minor is reviewed too
    ("0.20.5", "0.20.5 to 0.27.0"),
    (None, "no known starting version (unpinned) to 0.27.0"),   # an unpinned legacy install
    ("d0ba92bf17f9", "no known starting version (d0ba92bf17f9) to 0.27.0"),
])
def test_jump(start, expect):
    assert plan.jump(start, "0.27.0") == expect


def test_the_plan_marks_every_upgrade_for_review():
    near = Inventory(Repo("acme/near", pinned="0.26.0", installed_from="0.26.0", evidence=["mise-pin"]), "0.27.0")
    order = plan.plan_repo(near, "0.27.0", CFG).orders[0]
    assert order.details["review"] == "0.26.0 to 0.27.0" and order.to_dict()["review"]
    assert not order.agentic                            # an upgrade still runs without the agent
    off = from_dict({"orgs": ["acme"], "upgrade": {"review": False}})
    assert "review" not in plan.plan_repo(near, "0.27.0", off).orders[0].details


def test_a_refresh_is_reviewed_from_the_version_that_wrote_the_suite():
    stale = Inventory(Repo("acme/s", pinned="0.27.0", installed_from="0.26.0", evidence=["mise-pin"]), "0.27.0")
    order = plan.plan_repo(stale, "0.27.0", CFG).orders[0]
    assert order.kind == "refresh" and order.details["review"] == "0.26.0 to 0.27.0"


def test_review_is_on_unless_turned_off():
    assert CFG.review is True
    assert from_dict({"orgs": ["a"], "upgrade": {"review": False}}).review is False


def test_the_review_brief_names_the_skill_and_what_the_steward_gathered():
    order = plan.Order("upgrade-0.27.0", "upgrade", "acme/far", "t", "r", 0, "main", "0.27.0", "0.20.5")
    inv = Inventory(Repo("acme/far", pinned="0.20.5"), "0.27.0")
    marks = [{"marker": "hungovercoders/slopstopper#578", "files": [".slopstopper.yml"], "state": "open: licences"}]
    red = [{"check": "hygiene:docs-size", "exit_code": 1, "status": "fail", "failures": ["docs/ over budget"]}]
    brief = prompts.review(order, inv, "0.20.5 to 0.27.0", marks, red, "hungovercoders/slopstopper")
    for needle in ("slopstopper-install/SKILL.md", "references/refresh.md", ".steward-suite-diff.md",
                   ".steward-release-notes.md", "hungovercoders/slopstopper#578", "open: licences",
                   "hygiene:docs-size", "docs/ over budget", "0.20.5 to 0.27.0", ".steward-result.json"):
        assert needle in brief
    assert "Every static check" in prompts.review(order, inv, "x", [], [], "hungovercoders/slopstopper")


def test_workarounds_are_found_outside_the_skills(tmp_path):
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "upstream.md").write_text("slopstopper-workaround: hungovercoders/slopstopper#1")
    (tmp_path / ".slopstopper.yml").write_text("# slopstopper-workaround: hungovercoders/slopstopper#578 why\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("see slopstopper-workaround: hungovercoders/slopstopper#578\n")
    found = refresh.workarounds(workspace.Workspace("acme/a", tmp_path, "main"))
    assert found == [{"marker": "hungovercoders/slopstopper#578", "files": [".slopstopper.yml", "docs/x.md"]}]


def test_an_issue_github_cannot_answer_for_reads_as_unknown(monkeypatch):
    monkeypatch.setattr(refresh.gh, "api", lambda path: None)
    assert refresh.issue_state("hungovercoders/slopstopper#578").startswith("unknown")
    monkeypatch.setattr(refresh.gh, "api", lambda path: {"state": "closed", "closed_at": "2026-11-01T10:00:00Z", "title": "t"})
    assert refresh.issue_state("hungovercoders/slopstopper#578") == "closed on 2026-11-01: t"


# ── the upgrade path, end to end with GitHub, the CLI and the agent stubbed ──

@pytest.fixture
def upgrade(monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    ws = workspace.Workspace("acme/far", clone, "main")
    seen = {"briefs": [], "draft": None, "body": ""}
    monkeypatch.setattr(refresh, "set_pin", lambda path, target: "moved")
    monkeypatch.setattr(refresh, "cli_for", lambda *a: Path("/venv/bin/slopstopper"))
    monkeypatch.setattr(refresh, "install_suite", lambda ws, cli: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(refresh, "suite_diff", lambda ws: "diff --git a/x b/x\n")
    monkeypatch.setattr(refresh, "issue_state", lambda marker: "open")
    monkeypatch.setattr(runner, "release_notes", lambda *a: "## 0.27.0")
    monkeypatch.setattr(runner, "_verify_pass", lambda ws, cli: ([], []))
    monkeypatch.setattr(runner.agent, "ensure_skills", lambda *a: "present")
    monkeypatch.setattr(runner.agent, "run", lambda ws, brief, *a, **k: seen["briefs"].append(brief)
                        or AgentResult(True, outcome="fixed", summary="carried one knob over"))
    monkeypatch.setattr(workspace, "commit", lambda ws, msg: True)
    monkeypatch.setattr(workspace, "push", lambda ws, branch: None)

    def open_pr(ws, title, body, labels, draft=False):
        seen.update(draft=draft, body=body)
        return "https://github.com/acme/far/pull/1"
    monkeypatch.setattr(workspace, "open_pr", open_pr)
    ctx = runner.RunContext(base=tmp_path, agent_checked=True)
    return ws, ctx, seen


def _order(pinned, cfg=CFG):
    inv = Inventory(Repo("acme/far", pinned=pinned, installed_from=pinned, evidence=["mise-pin"]), "0.27.0")
    return plan.plan_repo(inv, "0.27.0", cfg).orders[0], inv


def test_a_one_minor_upgrade_is_reviewed_and_opens_ready(upgrade):
    ws, ctx, seen = upgrade
    order, inv = _order("0.26.0")
    out = runner.work_refresh(order, inv, CFG, ws, ctx)
    assert len(seen["briefs"]) == 1 and "references/refresh.md" in seen["briefs"][0]
    assert (ws.path / ".steward-suite-diff.md").read_text().startswith("diff --git")
    assert out.status == "pr" and seen["draft"] is False and "### Reviewed" in seen["body"]


def test_with_review_off_a_green_upgrade_never_starts_the_agent(upgrade):
    ws, ctx, seen = upgrade
    off = from_dict({"orgs": ["acme"], "upgrade": {"review": False}})
    order, inv = _order("0.26.0", off)
    out = runner.work_refresh(order, inv, off, ws, ctx)
    assert seen["briefs"] == [] and out.status == "pr" and "Reviewed" not in seen["body"]


def test_an_upgrade_the_agent_cannot_review_opens_as_a_draft(upgrade):
    ws, ctx, seen = upgrade
    order, inv = _order("0.20.5")
    out = runner.work_refresh(order, inv, from_dict({"orgs": ["acme"], "agent": {"enabled": False}}), ws, ctx)
    assert seen["briefs"] == [] and out.status == "draft" and seen["draft"] is True
    assert "### Not reviewed" in seen["body"] and "agent is disabled" in seen["body"]


def test_the_suite_diff_is_never_committed():
    assert ".steward-suite-diff.md" in workspace.LOCAL_ONLY


def test_the_suite_diff_covers_the_slopstopper_files_only(tmp_path):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, env=env, capture_output=True)  # noqa: E731
    run("init", "-q")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ss-smoke.yml").write_text("name: smoke\non: push\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (tmp_path / ".github" / "workflows" / "ss-smoke.yml").write_text("name: smoke\non: pull_request\n")
    (tmp_path / "app.py").write_text("x = 2\n")
    diff = refresh.suite_diff(workspace.Workspace("acme/a", tmp_path, "main"))
    assert "-on: push" in diff and "+on: pull_request" in diff and "app.py" not in diff
