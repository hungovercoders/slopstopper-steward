"""After an agent run that changed the tree, a second, read-only run reviews
the pending diff; changes it asks for get one correction round, and a review
left unanswered makes the PR a draft. A reviewer that gives no verdict never
blocks. With `merge.auto`, a green PR the steward opened is merged by it."""

import json
import subprocess
from pathlib import Path

import pytest

from steward import plan, prompts, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import agent, refresh
from steward.execute.agent import AgentResult
from steward.inventory import Finding, Inventory

CFG = from_dict({"orgs": ["acme"]})
FIXED = AgentResult(True, outcome="fixed", summary="raised the budget with a why", cost_usd=0.2, turns=5)
APPROVED = AgentResult(True, outcome="approved", summary="does what the order asked", cost_usd=0.05, turns=3,
                       model="claude-sonnet-5-5")
ASKS = AgentResult(True, outcome="changes-requested", summary="the comment names no reason",
                   findings=["a.txt: the `# why` says nothing; say which release moved the knob"])


def _repo(tmp_path):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, env=env, capture_output=True)  # noqa: E731
    run("init", "-q")
    (tmp_path / "a.txt").write_text("knob: 1\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return workspace.Workspace("acme/a", tmp_path, "main")


def _scripted(monkeypatch, *steps):
    """`agent.run` as a script: each step is (files to write, the result)."""
    calls, script = [], list(steps)
    def run(ws, brief, cfg, cli=None, *a, edits=True, **k):
        calls.append({"brief": brief, "edits": edits, "checks": k.get("checks")})
        writes, result = script.pop(0)
        for rel, text in writes.items():
            (ws.path / rel).write_text(text)
        return AgentResult(**{**result.to_dict()})
    monkeypatch.setattr(runner.agent, "run", run)
    return calls


def _pass(ws, cfg=CFG, tmp=None):
    order = plan.Order("upgrade-0.28.0", "upgrade", "acme/a", "t", "r", 0, "main", "0.28.0", "0.27.0")
    inv = Inventory(Repo("acme/a", pinned="0.27.0"), "0.28.0")
    ctx = runner.RunContext(base=tmp or ws.path.parent, agent_checked=True)
    return runner._agent_pass(order, inv, cfg, ws, Path("/v/bin/slopstopper"), ctx, "brief", ["hygiene:docs-size"], [])


def test_an_approved_change_is_reviewed_once_and_read_only(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2  # why: 0.28.0\n"}, FIXED), ({}, APPROVED))
    result = _pass(ws)
    assert len(calls) == 2 and calls[1]["edits"] is False and calls[1]["checks"] == ["hygiene:docs-size"]
    assert "+knob: 2" in (tmp_path / ".steward-change.md").read_text() and ".steward-change.md" in calls[1]["brief"]
    assert result.review["outcome"] == "approved" and result.review["settled"] and not result.review["corrected"]
    assert result.outcome == "fixed" and runner._review_blocks(result) == ""
    text = runner._agent_section(result)
    assert "**Self-review (approved; claude-sonnet-5-5, $0.05, 3 turns):** does what the order asked" in text


def test_changes_requested_get_one_correction_round(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2  # why\n"}, FIXED), ({}, ASKS),
                      ({"a.txt": "knob: 2  # why: 0.28.0 tightened it\n"}, AgentResult(True, outcome="fixed", summary="named the release")))
    result = _pass(ws)
    assert len(calls) == 3 and calls[2]["edits"] is True
    assert "say which release moved the knob" in calls[2]["brief"] and ".steward-review.md" in calls[2]["brief"]
    assert "say which release" in (tmp_path / ".steward-review.md").read_text()
    assert result.review["outcome"] == "changes-requested" and result.review["corrected"] and result.review["settled"]
    assert result.review["correction"]["summary"] == "named the release" and runner._review_blocks(result) == ""
    text = runner._agent_section(result)
    assert "- a.txt: the `# why` says nothing" in text and "Correction round (fixed): named the release" in text


def test_a_correction_that_changes_nothing_leaves_the_review_open(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({}, ASKS),
              ({}, AgentResult(True, outcome="skipped", summary="could not see what to change")))
    result = _pass(ws)
    assert not result.review["settled"] and not result.review["corrected"]
    why = runner._review_blocks(result)
    assert "asked for changes that were not made" in why and "could not see what to change" in why
    assert "### Self-review not settled" in runner._blocked_section(why)


def test_a_fixed_correction_that_answers_in_notes_settles_it(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({}, ASKS),
              ({}, AgentResult(True, outcome="fixed", summary="left as is", notes="the release notes name no change")))
    result = _pass(ws)
    assert result.review["settled"] and not result.review["corrected"]


def test_a_partial_correction_makes_the_result_partial(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({}, ASKS),
              ({"a.txt": "knob: 3\n"}, AgentResult(True, outcome="partial", summary="half", notes="a person decides the value")))
    result = _pass(ws)
    assert result.outcome == "partial" and "a person decides" in result.notes and not result.review["settled"]


@pytest.mark.parametrize("verdict, note", [
    (AgentResult(True, outcome="fixed", summary="?"), "gave no verdict (fixed)"),
    (AgentResult(True, error="agent timed out after 9 s"), "did not finish: agent timed out"),
    (AgentResult(False, error="claude CLI not on PATH"), "did not finish: claude CLI not on PATH"),
])
def test_no_verdict_never_blocks(tmp_path, monkeypatch, verdict, note):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({}, verdict))
    result = _pass(ws)
    assert len(calls) == 2 and result.review["outcome"] == "no verdict" and note in result.review["note"]
    assert result.review["settled"] and runner._review_blocks(result) == ""
    assert f"**Self-review (no verdict):** the reviewer {note}" in runner._agent_section(result)


def test_a_reviewer_that_changed_the_tree_is_not_trusted(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({"a.txt": "knob: 9\n"}, ASKS))
    result = _pass(ws)
    assert result.review["outcome"] == "no verdict" and "changed the tree" in result.review["note"]
    assert result.review["settled"]


def test_changes_requested_without_a_finding_is_an_approval(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED), ({}, AgentResult(True, outcome="changes-requested", summary="fine")))
    assert _pass(ws).review["outcome"] == "approved" and len(calls) == 2


def test_no_review_without_a_change_or_with_it_off(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({}, AgentResult(True, outcome="skipped", summary="already fixed")))
    assert _pass(ws).review is None and len(calls) == 1
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, FIXED))
    off = from_dict({"orgs": ["acme"], "agent": {"self_review": False}})
    assert _pass(ws, off).review is None and len(calls) == 1


def test_an_agent_that_did_not_finish_is_not_reviewed(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    calls = _scripted(monkeypatch, ({"a.txt": "knob: 2\n"}, AgentResult(True, error="agent timed out after 9 s")))
    assert _pass(ws).review is None and len(calls) == 1


# ── the reviewer's box ──

def test_the_reviewer_has_no_writing_tool():
    tools = agent.allowed_tools(["hygiene:docs-size"], edits=False)
    assert not any(t.startswith(("Edit", "Write", "MultiEdit", "Bash(echo", "Bash(mkdir")) for t in tools)
    assert "Read" in tools and "Bash(slopstopper run hygiene:docs-size)" in tools and "Bash(cat *)" in tools
    cmd = agent._command("brief", CFG, checks=["hygiene:docs-size"], edits=False)
    deny = cmd[cmd.index("--disallowedTools") + 1:cmd.index("--max-turns")]
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit"} <= set(deny)
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    allowed = agent._command("brief", CFG, checks=["hygiene:docs-size"])
    assert "Write" in allowed and "Write" not in allowed[allowed.index("--disallowedTools"):]


def test_the_verdict_is_read_from_the_final_message(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    text = "Here is my verdict:\n```json\n" + json.dumps({"outcome": "changes-requested", "summary": "s", "findings": ["x: y", ""]}) + "\n```\n"
    meta = {"result": text, "total_cost_usd": 0.03, "num_turns": 2, "modelUsage": {"claude-sonnet-5-5": {}}}
    monkeypatch.setattr(agent, "_launch", lambda *a, **k: (subprocess.CompletedProcess([], 0, json.dumps(meta), ""), None))
    out = agent.run(ws, "brief", CFG, preflight=lambda: None, edits=False)
    assert out.outcome == "changes-requested" and out.findings == ["x: y"] and out.cost_usd == 0.03
    assert out.model == "claude-sonnet-5-5"
    meta["result"] = "I could not decide."
    out = agent.run(ws, "brief", CFG, preflight=lambda: None, edits=False)
    assert out.outcome in ("partial", "skipped") and out.findings == []
    assert agent.verdict_bytes("no json here") is None


def test_the_briefs_name_the_diff_the_findings_and_the_rules():
    order = plan.Order("fix-7", "triage", "acme/a", "t", "r", 2, "main", "0.28.0", "0.28.0",
                       finding=Finding(7, "t", "u", [], check="hygiene:docs-size", workflow="ss-x.yml"))
    inv = Inventory(Repo("acme/a", pinned="0.28.0"), "0.28.0")
    brief = prompts.self_review(order, inv, "raised the budget", checks=["hygiene:docs-size"])
    for needle in (".steward-change.md", "a fix for #7 (hygiene:docs-size)", "raised the budget", "`hygiene:docs-size`",
                   '"approved" | "changes-requested"', "no editing tool"):
        assert needle in brief
    assert ".steward-result.json" not in brief
    fix = prompts.correct(order, inv, ["a.txt: wrong"], "hungovercoders/slopstopper", checks=["hygiene:docs-size"])
    assert "- a.txt: wrong" in fix and ".steward-result.json" in fix and "Never move the pin" in fix


def test_the_review_files_are_never_committed():
    assert ".steward-change.md" in workspace.LOCAL_ONLY and ".steward-review.md" in workspace.LOCAL_ONLY


def test_change_diff_is_the_whole_pending_change(tmp_path):
    ws = _repo(tmp_path)
    assert refresh.change_diff(ws) == "(nothing changed)\n"
    (tmp_path / "a.txt").write_text("knob: 2\n")
    (tmp_path / ".steward-change.md").write_text("old")
    diff = refresh.change_diff(ws)
    assert "+knob: 2" in diff and ".steward-change.md" not in diff


# ── config and report ──

def test_config_keys():
    assert CFG.self_review is True and CFG.merge_auto is False and CFG.merge_kinds == ["upgrade", "refresh"]
    assert (CFG.merge_method, CFG.merge_delete_branch) == ("squash", True)
    c = from_dict({"orgs": ["a"], "agent": {"self_review": False},
                   "merge": {"auto": True, "kinds": ["triage"], "method": "rebase", "delete_branch": False}})
    assert (c.self_review, c.merge_auto, c.merge_kinds, c.merge_method, c.merge_delete_branch) == (False, True, ["triage"], "rebase", False)
    assert from_dict({"orgs": ["a"], "merge": {"auto": "yes"}}).merge_auto is False   # only a real true turns it on
    with pytest.raises(ValueError, match="merge.method"):
        from_dict({"orgs": ["a"], "merge": {"method": "fast-forward"}})
    with pytest.raises(ValueError, match="merge.kinds"):
        from_dict({"orgs": ["a"], "merge": {"kinds": ["upgrade", "everything"]}})


def test_the_report_row_names_the_review_and_the_merge():
    from steward import report
    agent_row = {"cost_usd": 0.2, "turns": 5, "model": "m", "review": {"outcome": "changes-requested", "corrected": True, "cost_usd": 0.05}}
    out = runner.Outcome({"id": "u", "repo": "acme/a"}, "merged", pr_url="https://x/1", agent=agent_row,
                         ci={"waited": True, "rounds": 0, "failing": [], "timed_out": False, "note": "", "merged": True})
    md = report.RunReport("pr", [], [out]).markdown()
    assert "self-review changes-requested, corrected $0.05" in md and "CI green · merged" in md and "| merged |" in md
    out.ci = {**out.ci, "merged": False, "merge_note": "base branch policy prohibits the merge"}
    assert "not merged (base branch policy" in report.RunReport("pr", [], [out]).markdown()
