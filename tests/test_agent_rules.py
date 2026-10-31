"""The agent may run exactly the checks the clone can verify, in rule
shapes Claude Code matches, and every refusal is kept."""

import json
import subprocess
from pathlib import Path

from steward import plan, prompts, runner
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import agent
from steward.execute.agent import AgentResult
from steward.inventory import Inventory

CFG = from_dict({"orgs": ["acme"]})


def test_run_rules_are_exact_or_with_arguments_never_a_colon_prefix():
    rules = agent.run_rules(["hygiene:docs-size"])
    assert rules == [
        "Bash(slopstopper run hygiene:docs-size)", "Bash(slopstopper run hygiene:docs-size *)",
        "Bash(slopstopper run --json hygiene:docs-size)", "Bash(slopstopper run --json hygiene:docs-size *)",
    ]
    # A trailing `:*` is Claude Code's legacy prefix form (the prefix, or the
    # prefix then a space), so `hygiene:*` can never match `hygiene:docs-size`.
    assert not any(t.endswith(":*)") for t in agent.allowed_tools(["hygiene:docs-size", "security:sast"]))


def test_run_rules_drop_what_needs_an_environment_and_odd_names():
    assert agent.run_rules(["security:licences", "", "hygiene:x y"]) == []


def test_the_command_carries_the_rules_for_the_checks_handed_in():
    cmd = agent._command("brief", CFG, checks=["security:sast"])
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    assert "Bash(slopstopper run --json security:sast)" in allowed and "Bash(slopstopper --version)" in allowed


def test_refused_calls_are_kept_from_the_result(monkeypatch, tmp_path):
    from steward import workspace
    clone = tmp_path / "c"
    clone.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    ws = workspace.Workspace("acme/a", clone, "main")
    monkeypatch.setattr(agent.isolation, "agent_user", lambda: None)
    monkeypatch.setattr(agent, "available", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    meta = {"total_cost_usd": 0.2, "num_turns": 3, "modelUsage": {"claude-sonnet-4-5-20250929": {"inputTokens": 1}},
            "permission_denials": [
        {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "slopstopper run --json hygiene:docs-size"}},
        {"tool_name": "WebFetch", "tool_use_id": "t2", "tool_input": {"url": "https://x"}},
        "junk",
    ]}

    def fake_claude(args, cwd, env, timeout, **kw):
        (Path(cwd) / ".steward-result.json").write_text(json.dumps({"outcome": "skipped", "summary": "refused"}))
        return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
    monkeypatch.setattr(agent, "_run_to_files", fake_claude)
    result = agent.run(ws, "brief", CFG, checks=["hygiene:docs-size"])
    assert result.denied == ["Bash: slopstopper run --json hygiene:docs-size", "WebFetch"]
    assert "Refused by the agent's box: `Bash: slopstopper run --json hygiene:docs-size`" in runner._agent_section(result)
    assert result.to_dict()["denied"] == result.denied
    assert result.model == "claude-sonnet-4-5-20250929"
    assert "claude-sonnet-4-5-20250929, $0.20, 3 turns" in runner._agent_section(result)


def test_the_brief_names_the_checks_the_agent_may_run():
    order = plan.Order("upgrade-0.27.0", "upgrade", "acme/far", "t", "r", 0, "main", "0.27.0", "0.20.5")
    inv = Inventory(Repo("acme/far", pinned="0.20.5"), "0.27.0")
    brief = prompts.review(order, inv, "x", [], [], "hungovercoders/slopstopper", checks=["hygiene:docs-size", "security:sast"])
    assert "The checks you may run in this clone: `hygiene:docs-size`, `security:sast`." in brief
    assert "none (nothing this clone can verify)" in prompts.verify(order, inv, [], "hungovercoders/slopstopper")


def test_the_model_is_read_from_model_usage_or_left_blank():
    assert agent._model({"modelUsage": {"b": {}, "a": {}}}) == "a + b"
    assert agent._model({}) == "" and agent._model({"modelUsage": "junk"}) == ""


def test_the_run_report_names_the_model(tmp_path):
    from steward import report
    out = runner.Outcome({"id": "upgrade-0.28.0", "repo": "acme/a", "kind": "upgrade"}, "pr", pr_url="https://x/1",
                         agent={"cost_usd": 0.35, "turns": 13, "model": "claude-sonnet-4-5-20250929", "outcome": "fixed"})
    assert "(agent $0.35, 13 turns, claude-sonnet-4-5-20250929)" in report.RunReport("pr", [], [out]).markdown()
