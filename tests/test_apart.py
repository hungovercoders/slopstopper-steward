"""The checks that install the project run apart, as the agent user with no
secret in the process; the review also gets the config schema's diff."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from steward import isolation, plan, prompts, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import agent, refresh
from steward.inventory import Finding, Inventory

CFG = from_dict({"orgs": ["acme"]})
AGENT_USER = os.environ.get("STEWARD_TEST_AGENT_USER")
needs_agent_user = pytest.mark.skipif(not AGENT_USER, reason="set STEWARD_TEST_AGENT_USER to a user the test may sudo to")
LICENCES = "security:licences"


def _checks(**kw):
    return lambda ws, cli: refresh.Checks(**kw)


def test_without_an_agent_user_the_environment_checks_are_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.refresh, "static_checks", _checks(runnable=["hygiene:complexity"], environment=[LICENCES]))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    monkeypatch.setattr(runner.isolation, "agent_user", lambda: None)
    red, unverified = runner._verify_pass(workspace.Workspace("acme/a", tmp_path, "main"), Path("/v/bin/slopstopper"))
    assert red == [] and unverified == [{"check": LICENCES, "why": refresh.ENVIRONMENT_WHY}]


def test_with_an_agent_user_they_run_apart_and_a_red_one_is_red(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(runner.refresh, "static_checks", _checks(runnable=["hygiene:complexity"], environment=[LICENCES]))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    monkeypatch.setattr(runner.isolation, "agent_user", lambda: "steward-agent")
    monkeypatch.setattr(runner.isolation, "check_cli", lambda user, cli: seen.update(checked=(user, cli)))
    monkeypatch.setattr(runner.refresh, "verify_apart", lambda ws, cli, checks, user: seen.update(apart=(checks, user))
                        or [{"check": LICENCES, "exit_code": 1, "status": "fail", "failures": ["GPL-3.0: left-pad"]}])
    cli = Path("/v/bin/slopstopper")
    ctx = runner.RunContext(base=tmp_path)
    red, unverified = runner._verify_pass(workspace.Workspace("acme/a", tmp_path, "main"), cli, ctx)
    assert seen == {"checked": ("steward-agent", cli), "apart": ([LICENCES], "steward-agent")}
    assert [r["check"] for r in red] == [LICENCES] and unverified == [] and cli in ctx.checked_clis


def test_an_agent_user_that_cannot_run_the_cli_leaves_them_unverified_with_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.refresh, "static_checks", _checks(environment=[LICENCES]))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    monkeypatch.setattr(runner.isolation, "agent_user", lambda: "steward-agent")

    def refuse(user, cli):
        raise isolation.SetupError("steward-agent cannot run the pinned CLI")
    monkeypatch.setattr(runner.isolation, "check_cli", refuse)
    red, unverified = runner._verify_pass(workspace.Workspace("acme/a", tmp_path, "main"), Path("/v/bin/slopstopper"))
    assert red == [] and unverified[0]["check"] == LICENCES and "cannot run the pinned CLI" in unverified[0]["why"]


def test_a_triage_recheck_of_an_environment_check_runs_apart(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.refresh, "static_checks", _checks(environment=[LICENCES]))
    monkeypatch.setattr(runner.isolation, "agent_user", lambda: "steward-agent")
    monkeypatch.setattr(runner.isolation, "check_cli", lambda user, cli: None)
    monkeypatch.setattr(runner.refresh, "verify_apart", lambda ws, cli, checks, user: [])
    finding = Finding(7, "t", "u", ["slopstopper"], check=LICENCES, workflow="ss-security-licences-check.yml")
    order = plan.Order("triage-7", "triage", "acme/a", "t", "r", 2, "main", "0.28.0", "0.28.0", finding=finding)
    assert runner._recheck(order, workspace.Workspace("acme/a", tmp_path, "main"), Path("/v/bin/slopstopper")) == ([], [])


def test_a_failed_copy_is_a_red_row_not_an_exception(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise isolation.SetupError("mktemp: permission denied")
    monkeypatch.setattr(refresh.isolation, "run_apart", boom)
    red = refresh.verify_apart(workspace.Workspace("acme/a", tmp_path, "main"), Path("/v/bin/slopstopper"), [LICENCES], "steward-agent")
    assert red[0]["status"] == "cannot-run" and "permission denied" in red[0]["failures"][0]


FAKE_CLI = """#!/bin/sh
case "$1" in
  --version) echo "slopstopper 0.28.0"; exit 0;;
esac
# red if any secret reached this process, green otherwise
if [ -n "$ANTHROPIC_API_KEY$CLAUDE_CODE_OAUTH_TOKEN$GH_TOKEN$GITHUB_TOKEN" ]; then
  printf '{"check":"%s","exit_code":1,"status":"fail","failures":["a secret reached the check"]}' "$3"; exit 1
fi
printf '{"check":"%s","exit_code":0,"status":"pass","failures":[],"cwd":"%s"}' "$3" "$(pwd)"
"""


@needs_agent_user
def test_apart_runs_as_the_agent_user_with_no_secret_in_its_environment(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="steward-apart-test-", dir="/tmp"))
    base.chmod(0o755)
    venv_bin = base / "venvs" / "0.28.0" / "bin"
    venv_bin.mkdir(parents=True)
    cli = venv_bin / "slopstopper"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    clone = base / "clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text("[project]\nname='x'\n")
    for p in (base / "venvs", base / "venvs" / "0.28.0", venv_bin, clone):
        p.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GH_TOKEN", "ghs_test")
    ws = workspace.Workspace("acme/a", clone, "main")
    isolation.check_cli(AGENT_USER, cli)
    red = refresh.verify_apart(ws, cli, [LICENCES], AGENT_USER, timeout=60)
    assert red == [], red
    done = isolation.run_apart(AGENT_USER, clone, [str(cli), "run", "--json", LICENCES],
                               refresh.sandbox.environment(cli.parent), cli.parent, 60)
    out = json.loads(done.stdout)
    assert out["exit_code"] == 0 and out["cwd"] != str(clone) and out["cwd"].endswith("/tree")


# ── the brief and the schema diff ──

def test_the_brief_says_which_checks_the_steward_proves_and_asks_for_plain_commands():
    order = plan.Order("upgrade-0.28.0", "upgrade", "acme/a", "t", "r", 0, "main", "0.28.0", "0.27.0")
    inv = Inventory(Repo("acme/a", pinned="0.27.0"), "0.28.0")
    brief = prompts.review(order, inv, "0.27.0 to 0.28.0", [], [], "hungovercoders/slopstopper",
                           checks=["hygiene:docs-size"], apart=[LICENCES])
    assert "`security:licences` you cannot run here" in brief and "the steward runs it after you" in brief
    assert "One plain command per Bash call" in brief and ".steward-schema-diff.md" in brief
    assert "you cannot run here" not in prompts.verify(order, inv, [], "hungovercoders/slopstopper")


def test_echo_is_allowed():
    assert "Bash(echo *)" in agent.ALLOWED_TOOLS


def test_schema_diff_between_two_tags(monkeypatch):
    texts = {"v0.27.0": "profile: ui\nhygiene:\n  docs_size: 150\n", "v0.28.0": "profile: ui\nhygiene:\n  docs_size: 150\n  new_knob: 1\n"}
    monkeypatch.setattr(refresh.gh, "contents", lambda repo, path, ref=None: texts.get(ref))
    diff = refresh.schema_diff("hungovercoders/slopstopper", "0.27.0", "0.28.0")
    assert "+  new_knob: 1" in diff and "@ v0.27.0" in diff and "@ v0.28.0" in diff
    assert "same at v0.28.0 and v0.28.0" in refresh.schema_diff("hungovercoders/slopstopper", "0.28.0", "0.28.0")
    assert "missing at v0.20.0" in refresh.schema_diff("hungovercoders/slopstopper", "0.20.0", "0.28.0")
    assert "no known starting version" in refresh.schema_diff("hungovercoders/slopstopper", None, "0.28.0")


def test_a_schema_fetch_failure_is_said_not_read_as_nothing_new(monkeypatch):
    def fail(repo, path, ref=None):
        raise refresh.gh.GhError("gh api: 403")
    monkeypatch.setattr(refresh.gh, "contents", fail)
    assert "could not be fetched" in refresh.schema_diff("hungovercoders/slopstopper", "0.27.0", "0.28.0")


def test_the_schema_diff_is_never_committed():
    assert ".steward-schema-diff.md" in workspace.LOCAL_ONLY
