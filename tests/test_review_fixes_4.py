"""Regression tests for the fourth code review of the steward."""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from steward import pin, runner, sandbox, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import agent, refresh
from steward.inventory import Finding, Inventory
from steward.plan import Order, Plan
from steward.report import Outcome, RunReport, _cell

CFG = from_dict({"orgs": ["acme"]})


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _repo_with_origin(tmp_path: Path) -> workspace.Workspace:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "README.md").write_text("# adopter\n")
    _git(seed, "add", "README.md")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    return workspace.prepare(workspace.Workspace("acme/adopter", clone, "main"))


# ── 1: what the agent may plant in .git never runs in the steward's git
def test_planted_fsmonitor_and_hooks_do_not_run_in_steward_git(tmp_path):
    ws = _repo_with_origin(tmp_path)
    marker = tmp_path / "pwned"
    monitor = _executable(tmp_path / "monitor.sh", f"touch {marker}")
    _git(ws.path, "config", "core.fsmonitor", str(monitor))
    for hook in ("pre-commit", "pre-push", "commit-msg"):
        _executable(ws.path / ".githooks" / hook, f"touch {marker}")
    _git(ws.path, "config", "core.hooksPath", ".githooks")  # what `slopstopper install` sets in a clone

    _git(ws.path, "status")  # the plant is real: plain git runs it
    assert marker.exists()
    marker.unlink()

    workspace.branch(ws, "steward/upgrade-0.27.0")
    (ws.path / "mise.toml").write_text("pin\n")
    assert ws.dirty()
    assert workspace.commit(ws, "work")
    workspace.push(ws, "steward/upgrade-0.27.0")
    assert not marker.exists()


def test_guarded_git_dir_restores_config_and_clears_hooks(tmp_path):
    ws = _repo_with_origin(tmp_path)
    before = (ws.path / ".git" / "config").read_bytes()
    with workspace.guarded_git_dir(ws):
        with (ws.path / ".git" / "config").open("a") as fh:
            fh.write("[credential]\n\thelper = !sh -c 'echo leaked'\n[core]\n\tfsmonitor = /tmp/x\n")
        _executable(ws.path / ".git" / "hooks" / "pre-push", "exit 0")
        (ws.path / ".git" / "info").mkdir(exist_ok=True)
        (ws.path / ".git" / "info" / "attributes").write_text("* filter=x\n")
    assert (ws.path / ".git" / "config").read_bytes() == before
    assert list((ws.path / ".git" / "hooks").iterdir()) == []
    assert not (ws.path / ".git" / "info" / "attributes").exists()


# ── 1 + 2 + 3: the agent's run: no credentials, a throwaway HOME, protected paths, restored .git
def test_agent_run_is_boxed(monkeypatch, tmp_path):
    ws = _repo_with_origin(tmp_path)
    before = (ws.path / ".git" / "config").read_bytes()
    monkeypatch.setattr(agent, "available", lambda: None)
    monkeypatch.setenv("GH_TOKEN", "ghs_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen = {}

    def fake_claude(args, cwd=None, env=None, timeout=None, **kw):
        seen.update(args=args, env=env, timeout=timeout, own_group=kw.get("own_group"))
        with (Path(cwd) / ".git" / "config").open("a") as fh:
            fh.write("[core]\n\tfsmonitor = /tmp/evil\n")
        (Path(cwd) / ".steward-result.json").write_text(json.dumps({"outcome": "fixed", "summary": "ok"}))
        return subprocess.CompletedProcess(args, 0, json.dumps({"total_cost_usd": 0.1, "num_turns": 2}), "")
    monkeypatch.setattr(agent, "_run_to_files", fake_claude)
    venvs = tmp_path / "venvs"
    result = agent.run(ws, "brief", CFG, timeout=1234, protect=[venvs])
    assert result.outcome == "fixed" and seen["timeout"] == 1234 and seen["own_group"] is True
    env = seen["env"]
    assert "GH_TOKEN" not in env and env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["HOME"] != os.environ.get("HOME") and not Path(env["HOME"]).exists()  # throwaway, removed after
    assert env["PYTHONSAFEPATH"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    deny = seen["args"][seen["args"].index("--disallowedTools") + 1:]
    assert f"Edit(/{venvs}/**)" in deny and "Edit(./.git/**)" in deny and "Bash(find *)" in deny
    assert (ws.path / ".git" / "config").read_bytes() == before


def test_allowed_tools_carry_no_exec_path():
    banned = ("Bash(find", "Bash(task", "Bash(git grep", "Bash(python", "Bash(rm", "Bash(npx", "Bash(sh", "Bash(bash")
    assert not any(t.startswith(banned) for t in agent.ALLOWED_TOOLS)
    slop = [t for t in agent.ALLOWED_TOOLS if t.startswith("Bash(slopstopper")]
    assert "Bash(slopstopper *)" not in slop
    assert not any("reliability:" in t or "security:dast" in t or "api-" in t for t in slop)


def test_no_allowed_check_runs_an_interpreter_the_repo_names():
    """`security:licences` runs `security.licences.python` from the repo's
    own config (`python: bash` plus a `./pip` script is a shell): an
    allow-listed command must never start a program the repo chose."""
    slop = [t for t in agent.ALLOWED_TOOLS if t.startswith("Bash(slopstopper run")]
    assert not any("security:licences" in t or t.rstrip("*)").endswith("security:") for t in slop)
    assert "Bash(slopstopper run security:licences*)" in agent.DISALLOWED_TOOLS
    assert "Bash(slopstopper run --json security:licences*)" in agent.DISALLOWED_TOOLS


def test_git_status_cannot_be_pointed_at_a_repository_the_agent_wrote():
    for tool in ("Edit", "Write", "MultiEdit"):
        assert f"{tool}(**/.git/**)" in agent.DISALLOWED_TOOLS and f"{tool}(**/.git)" in agent.DISALLOWED_TOOLS
    assert "Bash(cd *)" in agent.DISALLOWED_TOOLS


# ── 2 + 10: one venv per version, outside the clones
def test_run_context_venvs_and_timeouts(tmp_path):
    ctx = runner.RunContext(base=tmp_path)
    assert ctx.venv("0.27.0") == tmp_path / "venvs" / "0.27.0" and ctx.protected() == [tmp_path / "venvs"]
    assert ctx.agent_timeout() == 3600
    ctx.deadline = time.time() + 30 * 60
    assert 1100 < ctx.agent_timeout() <= 1200  # 30 min left, 10 kept back
    ctx.deadline = time.time() + 60
    assert ctx.agent_timeout() == 300


def test_cli_for_reuses_a_venv_for_the_same_version(monkeypatch, tmp_path):
    venv = tmp_path / "venvs" / "0.27.0"
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if args[1:3] == ["-m", "venv"]:
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").write_text("")
        if "install" in args:
            (venv / "bin" / "slopstopper").write_text("")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    first = refresh.cli_for("0.27.0", venv)
    second = refresh.cli_for("0.27.0", venv)
    assert first == second == venv / "bin" / "slopstopper"
    assert sum("install" in c for c in calls) == 1


# ── 4: no order starts that the token cannot see through
def test_orders_defer_when_the_token_is_about_to_expire(monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_DEADLINE", str(time.time() + 5 * 60))
    monkeypatch.setattr(runner.workspace, "clone", lambda *a, **k: pytest.fail("no clone after the deadline"))
    order = Order("upgrade-0.27.0", "upgrade", "acme/a", "t", "r", 0, "main", "0.27.0", "0.26.0")
    inv = Inventory(Repo("acme/a"), "0.27.0")
    outcomes = runner.execute(Plan(orders=[order]), [inv], CFG, tmp_path)
    assert [o.status for o in outcomes] == ["deferred"] and "expires" in outcomes[0].detail


def test_a_temp_base_is_removed_after_the_run(monkeypatch):
    made = []
    real = runner.tempfile.mkdtemp
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    runner.execute(Plan(), [], CFG, None)
    assert made and not Path(made[0]).exists()


# ── 5: the pin is read and rewritten in every valid spelling, never duplicated
@pytest.mark.parametrize("text,expected", [
    ('[tools]\n"pipx:slopstopper-cli" = "0.27.0"\n', '[tools]\n"pipx:slopstopper-cli" = "0.28.0"\n'),
    ("[tools]\n'pipx:slopstopper-cli' = '0.27.0'\n", "[tools]\n'pipx:slopstopper-cli' = '0.28.0'\n"),
    ('[tools]\n"pipx:slopstopper-cli" = { version = "0.27.0", os = ["linux"] }\n',
     '[tools]\n"pipx:slopstopper-cli" = { version = "0.28.0", os = ["linux"] }\n'),
])
def test_set_pin_rewrites_in_place(tmp_path, text, expected):
    f = tmp_path / "mise.toml"
    f.write_text(text)
    assert pin.read_pin(text) == "0.27.0"
    assert pin.set_pin(f, "0.28.0") == "moved"
    assert f.read_text() == expected


def test_set_pin_seeds_after_a_commented_header_and_at_eof(tmp_path):
    f = tmp_path / "mise.toml"
    f.write_text('[tools] # pinned toolchain\n"task" = "3"\n')
    assert pin.set_pin(f, "0.28.0") == "seeded"
    assert f.read_text() == '[tools] # pinned toolchain\n"pipx:slopstopper-cli" = "0.28.0"\n"task" = "3"\n'
    g = tmp_path / "eof.toml"
    g.write_text('[env]\nA = "1"\n[tools]')
    assert pin.set_pin(g, "0.28.0") == "seeded"
    assert pin.read_pin(g.read_text()) == "0.28.0" and g.read_text().count("[tools]") == 1


@pytest.mark.parametrize("text", [
    '[tools]\n"pipx:slopstopper-cli" = ["0.27.0"]\n',      # an array: read, never rewritten
    'not = toml = at all\n',                              # invalid file: never touched
    'tools.node = "20"\n',                                # a dotted table a [tools] header would duplicate
])
def test_set_pin_refuses_rather_than_corrupt(tmp_path, text):
    f = tmp_path / "mise.toml"
    f.write_text(text)
    with pytest.raises(pin.PinError):
        pin.set_pin(f, "0.28.0")
    assert f.read_text() == text


# ── 6: an unverifiable triage never claims to close its issue
def _triage(check=None, workflow=None):
    return Order("triage-9", "triage", "acme/a", "t", "r", 3, "main", "0.27.0", "0.27.0",
                 Finding(9, "x", "u", [], check=check, workflow=workflow, kind="check" if check else "workflow-failure"), True)


def test_recheck_reports_every_unrunnable_case(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.refresh, "static_checks", lambda ws, cli: refresh.Checks(["hygiene:docs-size"], []))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    assert runner._recheck(_triage("hygiene:docs-size"), ws, Path("s")) == ([], [])
    red, unverified = runner._recheck(_triage("reliability:seo"), ws, Path("s"))
    assert red == [] and unverified[0]["check"] == "reliability:seo"
    red, unverified = runner._recheck(_triage(workflow="SlopStopper · SAST Analysis"), ws, Path("s"))
    assert unverified[0]["check"] == "SlopStopper · SAST Analysis"


def test_pr_body_closes_only_when_verified():
    order = _triage("hygiene:docs-size")
    assert runner._pr_body(order, CFG, "s", closes=True).startswith("Closes #9")
    body = runner._pr_body(order, CFG, "s", closes=False)
    assert body.startswith("Refs #9") and "Closes #9" not in body
    assert "(closes #" not in order.title.lower()


# ── 7: an adopter's existing labels are left as they are
def test_ensure_labels_never_forces(monkeypatch):
    calls = []
    monkeypatch.setattr(workspace.gh, "gh", lambda *a, **k: calls.append(a))
    workspace.ensure_labels("acme/a", ["slopstopper", "steward"])
    assert len(calls) == 2 and not any("--force" in c for c in calls)


# ── 8: a hung or odd check is red with a reason, not an exception
def test_verify_survives_timeouts_and_non_objects(monkeypatch, tmp_path):
    def fake_run(args, **kw):
        if args[-1] == "hygiene:complexity":
            raise subprocess.TimeoutExpired(args, kw.get("timeout"))
        return subprocess.CompletedProcess(args, 1, "[]\n", "")
    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    red = refresh.verify(workspace.Workspace("acme/a", tmp_path, "main"), Path("s"),
                         ["hygiene:complexity", "hygiene:docs-size"])
    assert [r["status"] for r in red] == ["timed-out", "fail"]
    assert "did not finish" in red[0]["failures"][0] and red[1]["check"] == "hygiene:docs-size"


def test_verify_and_listing_run_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "ghs_secret")
    envs = []

    def fake_run(args, env=None, **kw):
        envs.append(env)
        return subprocess.CompletedProcess(args, 0, json.dumps({"check": args[-1], "exit_code": 0}), "")
    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    refresh.verify(ws, Path("s"), ["hygiene:docs-size"])
    refresh._json(Path("s"), tmp_path, "checks", "list")
    assert envs and all(e is not None and "GH_TOKEN" not in e and e["PYTHONSAFEPATH"] == "1" for e in envs)


# ── 9: report cells survive newlines and pipes
def test_report_cells_are_single_line_and_escaped():
    assert _cell("exit 1: a | b\nsecond line") == "exit 1: a \\| b second line"
    assert len(_cell("x" * 1000)) == 300
    report = RunReport("pr", outcomes=[Outcome({"repo": "acme/a", "id": "upgrade-0.27.0"}, "failed",
                                               detail="slopstopper install exited 1:\nTraceback | boom")])
    row = [line for line in report.markdown().splitlines() if line.startswith("| acme/a")][0]
    assert row.count(" | ") == 3 and "\\|" in row
