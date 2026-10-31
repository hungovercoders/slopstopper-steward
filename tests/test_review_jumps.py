"""Big upgrades get an agent review of the refresh; small ones stay mechanical."""

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


@pytest.mark.parametrize("start, target, gap, expect", [
    ("0.26.0", "0.27.0", 2, None),                      # one minor: mechanical
    ("0.25.0", "0.27.0", 2, "2 minor versions"),
    ("0.20.5", "0.27.0", 2, "7 minor versions"),
    ("0.27.0", "1.0.0", 2, "a major version"),
    (None, "0.27.0", 2, "no known starting version"),   # an unpinned legacy install
    ("d0ba92bf17f9", "0.27.0", 2, "no known starting version"),
    ("0.20.5", "0.27.0", 0, None),                      # 0 turns the review off
])
def test_big_jump(start, target, gap, expect):
    why = plan.big_jump(start, target, gap)
    assert (why is None) if expect is None else (expect in why)


def test_the_plan_marks_only_the_big_jumps():
    far = Inventory(Repo("acme/far", pinned="0.20.5", installed_from="0.20.5", evidence=["mise-pin"]), "0.27.0")
    near = Inventory(Repo("acme/near", pinned="0.26.0", installed_from="0.26.0", evidence=["mise-pin"]), "0.27.0")
    far_order = plan.plan_repo(far, "0.27.0", CFG).orders[0]
    near_order = plan.plan_repo(near, "0.27.0", CFG).orders[0]
    assert "7 minor versions" in far_order.details["review"] and far_order.to_dict()["review"]
    assert "review" not in near_order.details
    assert not far_order.agentic                        # an upgrade still runs without the agent


def test_review_minor_gap_is_configurable():
    assert from_dict({"orgs": ["a"], "upgrade": {"review_minor_gap": 0}}).review_minor_gap == 0
    assert CFG.review_minor_gap == 2


def test_the_review_brief_names_the_skill_and_what_the_steward_gathered():
    order = plan.Order("upgrade-0.27.0", "upgrade", "acme/far", "t", "r", 0, "main", "0.27.0", "0.20.5")
    inv = Inventory(Repo("acme/far", pinned="0.20.5"), "0.27.0")
    marks = [{"marker": "hungovercoders/slopstopper#578", "files": [".slopstopper.yml"], "state": "open: licences"}]
    red = [{"check": "hygiene:docs-size", "exit_code": 1, "status": "fail", "failures": ["docs/ over budget"]}]
    brief = prompts.review(order, inv, "7 minor versions: 0.20.5 to 0.27.0", marks, red, "hungovercoders/slopstopper")
    for needle in ("slopstopper-install/SKILL.md", "references/refresh.md", ".steward-suite-diff.md",
                   ".steward-release-notes.md", "hungovercoders/slopstopper#578", "open: licences",
                   "hygiene:docs-size", "docs/ over budget", "7 minor versions", ".steward-result.json"):
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


def _order(pinned):
    inv = Inventory(Repo("acme/far", pinned=pinned, installed_from=pinned, evidence=["mise-pin"]), "0.27.0")
    return plan.plan_repo(inv, "0.27.0", CFG).orders[0], inv


def test_a_big_jump_is_reviewed_and_opens_ready(upgrade):
    ws, ctx, seen = upgrade
    order, inv = _order("0.20.5")
    out = runner.work_refresh(order, inv, CFG, ws, ctx)
    assert len(seen["briefs"]) == 1 and "references/refresh.md" in seen["briefs"][0]
    assert (ws.path / ".steward-suite-diff.md").read_text().startswith("diff --git")
    assert out.status == "pr" and seen["draft"] is False and "### Reviewed" in seen["body"]


def test_a_small_upgrade_never_starts_the_agent(upgrade):
    ws, ctx, seen = upgrade
    order, inv = _order("0.26.0")
    out = runner.work_refresh(order, inv, CFG, ws, ctx)
    assert seen["briefs"] == [] and out.status == "pr" and "Reviewed" not in seen["body"]


def test_a_big_jump_the_agent_cannot_review_opens_as_a_draft(upgrade):
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
