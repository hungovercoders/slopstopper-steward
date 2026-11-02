"""The PR's own checks are the last verifier: the steward waits for them,
fixes what the triage skill covers within its limits, and drafts what it
cannot get green. Lockfiles the install made stale are regenerated first."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from steward import ci, plan, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute import refresh
from steward.execute.agent import AgentResult
from steward.inventory import Inventory

CFG = from_dict({"orgs": ["acme"]})


def _run(name, status="completed", conclusion="success", rid=1, app="github-actions"):
    return {"id": rid, "name": name, "status": status, "conclusion": conclusion, "url": f"https://x/{rid}", "app": app}


# ── ci.py ──

def test_wait_returns_when_every_check_has_completed(monkeypatch):
    pages = iter([[_run("a", "queued", None)], [_run("a", "in_progress", None)], [_run("a")]])
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: next(pages))
    clock = {"t": 0.0}
    runs, timed_out = ci.wait("acme/a", "sha", 600, now=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    assert (runs[0]["conclusion"], timed_out) == ("success", False)


def test_wait_gives_up_on_a_head_that_never_gets_a_check(monkeypatch):
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: [])
    clock = {"t": 0.0}
    assert ci.wait("acme/a", "sha", 1200, now=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)) == ([], False)
    assert clock["t"] >= ci.APPEAR_SECONDS


def test_wait_times_out_on_a_check_that_keeps_running(monkeypatch):
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: [_run("slow", "in_progress", None)])
    clock = {"t": 0.0}
    runs, timed_out = ci.wait("acme/a", "sha", 120, now=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    assert timed_out and runs[0]["status"] == "in_progress" and clock["t"] <= 121


def test_failed_counts_the_red_conclusions_only():
    runs = [_run("ok"), _run("skip", conclusion="skipped"), _run("neutral", conclusion="neutral"),
            _run("bad", conclusion="failure"), _run("gone", conclusion="cancelled"), _run("late", conclusion="timed_out")]
    assert [r["name"] for r in ci.failed(runs)] == ["bad", "gone", "late"]


def test_describe_tells_slopstopper_checks_from_the_repos_own(monkeypatch):
    answers = {"repos/acme/a/actions/jobs/1": {"run_id": 10}, "repos/acme/a/actions/runs/10": {"path": ".github/workflows/ss-reliability-smoke-tests.yml"},
               "repos/acme/a/actions/jobs/2": {"run_id": 20}, "repos/acme/a/actions/runs/20": {"path": ".github/workflows/ci.yml"}}
    monkeypatch.setattr(ci.gh, "api", lambda path, **k: answers.get(path))
    runs = [_run("smoke-test", conclusion="failure", rid=1), _run("Test", conclusion="failure", rid=2),
            _run("Workers Builds", conclusion="failure", rid=3, app="cloudflare"), _run("ok", rid=4)]
    out = ci.describe("acme/a", runs)
    assert [(r["name"], r["ours"]) for r in out] == [("smoke-test", True), ("Test", False), ("Workers Builds", False)]


def test_log_tail_is_bounded_and_redacted(monkeypatch):
    text = "\n".join(f"line {i}" for i in range(500)) + "\nremote: https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/acme/a\n"
    monkeypatch.setattr(ci.gh, "gh", lambda *a, **k: subprocess.CompletedProcess(a, 0, text, ""))
    tail = ci.log_tail("acme/a", 1)
    assert "ghs_" not in tail and "***" in tail and "line 499" in tail and "line 100" not in tail
    monkeypatch.setattr(ci.gh, "gh", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "HTTP 404"))
    assert "could not be read" in ci.log_tail("acme/a", 1)


def test_failures_markdown_carries_each_tail():
    red = [{"name": "smoke-test", "conclusion": "failure", "url": "https://x/1", "workflow": ".github/workflows/ss-reliability-smoke-tests.yml"}]
    text = ci.failures_markdown(red, {"smoke-test": "npm error EUSAGE"})
    assert "## smoke-test (failure)" in text and "npm error EUSAGE" in text and "ss-reliability-smoke-tests.yml" in text


# ── the settle loop ──

@pytest.fixture
def settle(monkeypatch, tmp_path):
    seen = {"agent": 0, "pushes": 0, "drafted": False, "body": ""}
    waits = []
    monkeypatch.setattr(runner.ci, "wait", lambda repo, sha, seconds: waits.pop(0))
    monkeypatch.setattr(runner.ci, "describe", lambda repo, runs: [
        {**r, "workflow": ".github/workflows/ss-reliability-smoke-tests.yml" if r["app"] == "github-actions" else "",
         "ours": r["app"] == "github-actions"} for r in ci.failed(runs)])
    monkeypatch.setattr(runner.ci, "log_tail", lambda repo, job: "npm error EUSAGE")
    monkeypatch.setattr(runner, "_runnable", lambda ws, cli: (["hygiene:docs-size"], []))
    monkeypatch.setattr(runner.agent, "run", lambda ws, brief, *a, **k: seen.__setitem__("agent", seen["agent"] + 1)
                        or seen.__setitem__("brief", brief) or AgentResult(True, outcome="fixed", summary="regenerated the lock"))
    monkeypatch.setattr(workspace, "head_sha", lambda ws: f"sha{seen['pushes']}")
    monkeypatch.setattr(workspace, "commit", lambda ws, msg: True)
    monkeypatch.setattr(workspace, "push", lambda ws, branch: seen.__setitem__("pushes", seen["pushes"] + 1))
    monkeypatch.setattr(workspace, "update_pr_body", lambda repo, number, body: seen.__setitem__("body", body))
    monkeypatch.setattr(workspace, "set_draft", lambda repo, number: seen.__setitem__("drafted", True))
    order = plan.Order("upgrade-0.28.0", "upgrade", "acme/a", "t", "r", 0, "main", "0.28.0", "0.27.0")
    inv = Inventory(Repo("acme/a", pinned="0.27.0"), "0.28.0")
    ws = workspace.Workspace("acme/a", tmp_path, "main")

    def go(cfg=CFG, draft=False, deadline=None):
        ctx = runner.RunContext(base=tmp_path, deadline=deadline, agent_checked=True)
        return runner._settle_ci(order, inv, cfg, ws, Path("/v/bin/slopstopper"), ctx, "https://github.com/acme/a/pull/7", "body\n", draft)
    return go, waits, seen


def test_green_at_once_stays_ready(settle):
    go, waits, seen = settle
    waits.append(([_run("smoke-test"), _run("e2e", rid=2)], False))
    status, info = go()
    assert status == "pr" and info["rounds"] == 0 and seen["agent"] == 0 and not seen["drafted"]
    assert "The PR's 2 checks are green." in seen["body"]


def test_a_red_slopstopper_check_is_fixed_and_waited_for_again(settle):
    go, waits, seen = settle
    waits += [([_run("smoke-test", conclusion="failure")], False), ([_run("smoke-test", rid=5)], False)]
    status, info = go()
    assert status == "pr" and info["rounds"] == 1 and seen["agent"] == 1 and seen["pushes"] == 1 and not seen["drafted"]
    assert "green after 1 fix round(s)" in seen["body"]
    assert "smoke-test" in seen["brief"] and ".steward-ci-failures.md" in seen["brief"] and "npm ci" in seen["brief"]


def test_out_of_rounds_means_a_draft_that_names_the_checks(settle):
    go, waits, seen = settle
    red = ([_run("smoke-test", conclusion="failure")], False)
    waits += [red, red, red]
    status, info = go(from_dict({"orgs": ["acme"], "ci": {"fix_rounds": 2}}))
    assert status == "draft" and seen["agent"] == 2 and seen["drafted"] and info["rounds"] == 2
    assert "### CI still red after 2 fix round(s)" in seen["body"] and "`smoke-test` (failure)" in seen["body"]


def test_a_repos_own_red_check_is_reported_not_worked(settle):
    go, waits, seen = settle
    waits.append(([_run("Test (ubuntu)", conclusion="failure", app="github-actions-other")], False))
    status, info = go()
    assert status == "draft" and seen["agent"] == 0 and seen["drafted"]
    assert "(not a slopstopper check)" in seen["body"]


def test_a_wait_that_runs_out_drafts_the_pr(settle):
    go, waits, seen = settle
    waits.append(([_run("e2e", "in_progress", None)], True))
    status, info = go()
    assert status == "draft" and info["timed_out"] and seen["drafted"] and "Still running after" in seen["body"]


def test_no_time_left_means_not_awaited_and_still_ready(settle):
    go, waits, seen = settle
    status, info = go(deadline=time.time() + 60)
    assert status == "pr" and not info["waited"] and "Not awaited: the run was out of time" in seen["body"]
    assert waits == [] and not seen["drafted"]


def test_wait_can_be_switched_off(settle):
    go, waits, seen = settle
    status, info = go(from_dict({"orgs": ["acme"], "ci": {"wait": False}}))
    assert status == "pr" and "Not awaited: ci.wait is off" in seen["body"]


def test_an_already_draft_pr_is_not_drafted_twice(settle):
    go, waits, seen = settle
    waits.append(([_run("smoke-test", conclusion="failure")], False))
    status, info = go(from_dict({"orgs": ["acme"], "ci": {"fix_rounds": 0}}), draft=True)
    assert status == "draft" and not seen["drafted"]


def test_ci_budget_respects_the_deadline():
    cfg = from_dict({"orgs": ["acme"], "ci": {"wait_minutes": 20}})
    assert runner._ci_budget(cfg, runner.RunContext(base=Path("/tmp"))) == 1200
    soon = runner.RunContext(base=Path("/tmp"), deadline=time.time() + 600)
    assert 100 <= runner._ci_budget(cfg, soon) <= 120


def test_the_report_names_how_ci_ended():
    from steward import report
    def cell(ci_info):
        out = runner.Outcome({"id": "u", "repo": "acme/a"}, "pr", pr_url="https://x/1", ci=ci_info)
        return report.RunReport("pr", [], [out]).markdown()
    assert "CI green" in cell({"waited": True, "rounds": 0, "failing": [], "timed_out": False, "note": ""})
    assert "CI red after 1 fix round(s): smoke-test" in cell({"waited": True, "rounds": 1, "failing": [{"name": "smoke-test"}], "timed_out": False, "note": ""})
    assert "CI not awaited (ci.wait is off)" in cell({"waited": False, "rounds": 0, "failing": [], "timed_out": False, "note": "ci.wait is off"})


# ── lockfiles ──

def _repo(tmp_path):
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "PATH": "/usr/bin:/bin"}
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, env=env, capture_output=True)  # noqa: E731
    run("init", "-q")
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"a": "1"}}))
    (tmp_path / "package-lock.json").write_text("{\"lockfileVersion\": 3}")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return workspace.Workspace("acme/a", tmp_path, "main")


def test_lockfiles_to_sync_finds_the_manifests_the_install_changed(tmp_path):
    ws = _repo(tmp_path)
    assert refresh.lockfiles_to_sync(ws) == []
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"a": "1", "markdownlint-cli": "^0.47.0"}}))
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "package.json").write_text("{}")             # new, but no lock beside it
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text("{}")
    (tmp_path / "app" / "package-lock.json").write_text("{}")
    assert refresh.lockfiles_to_sync(ws) == ["", "app"]


def test_sync_lockfiles_runs_npm_apart_and_brings_the_lock_back(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"a": "1", "b": "2"}}))
    seen = {}

    def apart(user, clone, argv, env, path_first, timeout, bring=()):
        seen.update(user=user, argv=argv, bring=list(bring), key="ANTHROPIC_API_KEY" in env, token="GH_TOKEN" in env)
        return subprocess.CompletedProcess(argv, 0, "", ""), {bring[0]: b"{\"lockfileVersion\": 3, \"b\": true}"}
    monkeypatch.setattr(refresh.isolation, "run_apart_bring", apart)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GH_TOKEN", "ghs_test")
    rows = refresh.sync_lockfiles(ws, "steward-agent")
    assert rows == [{"dir": ".", "status": "synced", "detail": ""}]
    assert seen["argv"][:4] == ["npm", "install", "--package-lock-only", "--ignore-scripts"] and seen["bring"] == ["package-lock.json"]
    assert not seen["key"] and not seen["token"]
    assert (tmp_path / "package-lock.json").read_text().endswith('"b": true}')


def test_sync_lockfiles_says_when_npm_fails(tmp_path, monkeypatch):
    ws = _repo(tmp_path)
    (tmp_path / "package.json").write_text("{\"devDependencies\": {\"a\": \"2\"}}")
    monkeypatch.setattr(refresh.isolation, "run_apart_bring",
                        lambda *a, **k: (subprocess.CompletedProcess([], 1, "", "npm error E404 nope"), {}))
    rows = refresh.sync_lockfiles(ws, "steward-agent")
    assert rows[0]["status"] == "failed" and "E404" in rows[0]["detail"]


def test_the_pr_body_says_what_happened_to_the_lock():
    text = runner._locks_section([{"dir": ".", "status": "synced", "detail": ""}, {"dir": "app", "status": "failed", "detail": "npm exited 1"}])
    assert "### Lockfiles" in text and "`./package-lock.json`: synced" in text and "`app/package-lock.json`: failed (npm exited 1)" in text
    assert runner._locks_section([]) == ""


# ── the deployed checks are named, not dropped ──

def test_deployed_checks_are_listed_as_awaiting_the_prs_ci(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.refresh, "static_checks", lambda ws, cli: refresh.Checks(runnable=[], deployed=["reliability:smoke", "security:dast"]))
    monkeypatch.setattr(runner.refresh, "verify", lambda ws, cli, checks: [])
    red, unverified = runner._verify_pass(workspace.Workspace("acme/a", tmp_path, "main"), Path("/v/bin/slopstopper"))
    assert red == [] and [u["check"] for u in unverified] == ["reliability:smoke", "security:dast"]
    assert all(u["why"] == refresh.DEPLOYED_WHY for u in unverified)


def test_semgrep_lives_where_the_agent_user_can_reach_it():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "steward-run.yml").read_text()
    assert "/opt/steward-tools/semgrep" in text and "RUNNER_TEMP/semgrep" not in text


def test_ci_config_keys():
    c = from_dict({"orgs": ["a"], "ci": {"wait": False, "wait_minutes": 5, "fix_rounds": 0}})
    assert (c.ci_wait, c.ci_wait_minutes, c.ci_fix_rounds) == (False, 5, 0)
    assert (CFG.ci_wait, CFG.ci_wait_minutes, CFG.ci_fix_rounds) == (True, 20, 2)
    assert ".steward-ci-failures.md" in workspace.LOCAL_ONLY
