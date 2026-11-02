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

SS = ".github/workflows/ss-reliability-smoke-tests.yml"


def _clock():
    clock = {"t": 0.0}
    return clock, (lambda: clock["t"]), (lambda s: clock.__setitem__("t", clock["t"] + s))


def _pages(monkeypatch, *pages):
    """check_runs answers each page in turn, then repeats the last."""
    seq = list(pages)
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: seq.pop(0) if len(seq) > 1 else seq[0])


def test_wait_returns_once_complete_and_quiet(monkeypatch):
    _pages(monkeypatch, [_run("a", "queued", None)], [_run("a", "in_progress", None)], [_run("a")])
    monkeypatch.setattr(ci, "workflow_of", lambda repo, job: SS)
    clock, now, sleep = _clock()
    runs, timed_out, workflows = ci.wait("acme/a", "sha", 600, now=now, sleep=sleep)
    assert (runs[0]["conclusion"], timed_out, workflows) == ("success", False, {1: SS})
    assert clock["t"] >= ci.QUIET_SECONDS        # a completed run is not settled until it has held still


def test_wait_holds_until_every_expected_workflow_has_a_run(monkeypatch):
    bot, ours = _run("DCO", rid=1, app="dco"), _run("smoke-test", rid=2)
    _pages(monkeypatch, [bot], [bot], [bot], [bot, ours])
    monkeypatch.setattr(ci, "workflow_of", lambda repo, job: SS if job == 2 else "")
    clock, now, sleep = _clock()
    runs, timed_out, workflows = ci.wait("acme/a", "sha", 1200, expected={SS}, now=now, sleep=sleep)
    assert not timed_out and {r["name"] for r in runs} == {"DCO", "smoke-test"} and workflows[2] == SS


def test_an_expected_workflow_that_never_reports_costs_the_grace_not_the_whole_wait(monkeypatch):
    _pages(monkeypatch, [_run("DCO", rid=1, app="dco")])
    clock, now, sleep = _clock()
    runs, timed_out, _ = ci.wait("acme/a", "sha", 1200, expected={SS}, now=now, sleep=sleep)
    assert not timed_out and len(runs) == 1 and ci.GRACE_SECONDS <= clock["t"] < ci.GRACE_SECONDS + ci.POLL_SECONDS + 1
    clock["t"] = 0.0
    runs, timed_out, _ = ci.wait("acme/a", "sha", 200, expected={SS}, now=now, sleep=sleep)
    assert timed_out                                   # a budget shorter than the grace still times out


def test_wait_gives_up_on_a_head_that_never_gets_a_check(monkeypatch):
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: [])
    clock, now, sleep = _clock()
    assert ci.wait("acme/a", "sha", 1200, now=now, sleep=sleep) == ([], False, {})
    assert clock["t"] >= ci.APPEAR_SECONDS
    clock["t"] = 0.0
    assert ci.wait("acme/a", "sha", 400, expected={SS}, now=now, sleep=sleep) == ([], True, {})   # expected, so it waits


def test_wait_times_out_on_a_check_that_keeps_running(monkeypatch):
    monkeypatch.setattr(ci, "check_runs", lambda repo, sha: [_run("slow", "in_progress", None)])
    monkeypatch.setattr(ci, "workflow_of", lambda repo, job: SS)
    clock, now, sleep = _clock()
    runs, timed_out, _ = ci.wait("acme/a", "sha", 120, now=now, sleep=sleep)
    assert timed_out and runs[0]["status"] == "in_progress" and clock["t"] <= 121


def test_expected_workflows_are_the_pr_triggered_ss_ones(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ss-a.yml").write_text("on:\n  pull_request:\n    branches: [main]\n")
    (wf / "ss-b.yml").write_text("on: [push, pull_request]\n")
    (wf / "ss-c.yml").write_text("on:\n  schedule:\n    - cron: '0 3 * * 1'\n  workflow_dispatch:\n")
    (wf / "ci.yml").write_text("on: pull_request\n")
    (wf / "ss-hygiene-auto-label-pr.yml").write_text("on:\n  pull_request_target:\n    types: [opened]\n")
    (wf / "ss-d.yml").write_text("on: [push, pull_request_target]\n")
    assert ci.expected_workflows(tmp_path) == {".github/workflows/ss-a.yml", ".github/workflows/ss-b.yml"}


def test_a_403_on_check_runs_names_the_missing_permission(monkeypatch):
    def forbidden(path, **k):
        raise ci.gh.GhError("gh api repos/acme/a/commits/x/check-runs: HTTP 403")
    monkeypatch.setattr(ci.gh, "api", forbidden)
    with pytest.raises(ci.gh.GhError, match="Checks: read"):
        ci.check_runs("acme/a", "x")


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
    seen = {"agent": 0, "pushes": 0, "drafted": False, "body": "", "skills": 0, "locks": 0, "verified": 0}
    waits = []
    monkeypatch.setattr(runner.ci, "wait", lambda repo, sha, seconds, expected=None, workflows=None: (*waits.pop(0), {}))
    monkeypatch.setattr(runner.ci, "expected_workflows", lambda root: set())
    monkeypatch.setattr(runner.ci, "describe", lambda repo, runs, workflows=None: [
        {**r, "workflow": ".github/workflows/ss-reliability-smoke-tests.yml" if r["app"] == "github-actions" else "",
         "ours": r["app"] == "github-actions"} for r in ci.failed(runs)])
    monkeypatch.setattr(runner.ci, "log_tail", lambda repo, job: "npm error EUSAGE")
    monkeypatch.setattr(runner, "_runnable", lambda ws, cli: (["hygiene:docs-size"], []))
    monkeypatch.setattr(runner, "_verify_pass", lambda ws, cli, ctx=None: seen.__setitem__("verified", seen["verified"] + 1) or ([], []))
    monkeypatch.setattr(runner, "_sync_locks", lambda ws: seen.__setitem__("locks", seen["locks"] + 1) or [])
    monkeypatch.setattr(runner.agent, "ensure_skills", lambda *a: seen.__setitem__("skills", seen["skills"] + 1) or "present")
    monkeypatch.setattr(workspace.Workspace, "dirty", lambda self: True)
    monkeypatch.setattr(runner.agent, "run", lambda ws, brief, *a, **k: seen.__setitem__("agent", seen["agent"] + 1)
                        or seen.__setitem__("brief", brief) or AgentResult(True, outcome="fixed", summary="regenerated the lock"))
    monkeypatch.setattr(workspace, "head_sha", lambda ws: f"sha{seen['pushes']}")
    monkeypatch.setattr(workspace, "commit", lambda ws, msg: True)
    monkeypatch.setattr(workspace, "push", lambda ws, branch: seen.__setitem__("pushes", seen["pushes"] + 1))
    monkeypatch.setattr(workspace, "update_pr_body", lambda repo, number, body: seen.__setitem__("body", body))
    monkeypatch.setattr(workspace, "set_draft", lambda repo, number: seen.__setitem__("drafted", True))
    monkeypatch.setattr(workspace, "merge_pr", lambda repo, number, method, delete: seen.__setitem__("merged", (number, method, delete)))
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
    assert seen["skills"] == 1 and seen["locks"] == 1 and seen["verified"] == 1   # skills, lock and the steward's gate, before the push
    assert "green after 1 fix round(s)" in seen["body"]
    assert "smoke-test" in seen["brief"] and ".steward-ci-failures.md" in seen["brief"] and "npm ci" in seen["brief"]


def test_a_fix_that_goes_red_locally_is_not_pushed(settle, monkeypatch):
    go, waits, seen = settle
    monkeypatch.setattr(runner, "_verify_pass", lambda ws, cli, ctx=None: ([{"check": "hygiene:docs-accuracy", "exit_code": 1, "status": "fail", "failures": []}], []))
    waits.append(([_run("smoke-test", conclusion="failure")], False))
    status, info = go()
    assert status == "draft" and seen["pushes"] == 0 and seen["drafted"]
    assert "left the steward's own checks red (hygiene:docs-accuracy), so it was not pushed" in seen["body"]


def test_a_failed_wait_never_loses_the_pr(settle, monkeypatch):
    go, waits, seen = settle
    def boom(*a, **k):
        raise ci.gh.GhError("gh api: HTTP 502")
    monkeypatch.setattr(runner.ci, "wait", boom)
    status, info = go()
    assert status == "pr" and "the wait stopped: GhError: gh api: HTTP 502" in seen["body"] and not seen["drafted"]


def test_no_check_at_all_is_said_and_not_drafted(settle):
    go, waits, seen = settle
    waits.append(([], True))
    status, info = go()
    assert status == "pr" and info["checks"] == 0 and not seen["drafted"]
    assert "no check ran on the PR within" in seen["body"]


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


# ── merge.auto ──

AUTO = from_dict({"orgs": ["acme"], "merge": {"auto": True}})


def test_a_green_pr_is_merged_when_merge_auto_is_on(settle):
    go, waits, seen = settle
    waits.append(([_run("smoke-test"), _run("e2e", rid=2)], False))
    status, info = go(AUTO)
    assert status == "merged" and seen["merged"] == (7, "squash", True) and info["merged"]
    assert "### Merged" in seen["body"] and "(squash)" in seen["body"] and "checks are green" in seen["body"]


def test_merge_is_off_by_default_and_per_kind(settle):
    go, waits, seen = settle
    waits.append(([_run("smoke-test")], False))
    assert go()[0] == "pr" and "merged" not in seen and "Merged" not in seen["body"]
    waits.append(([_run("smoke-test")], False))
    status, info = go(from_dict({"orgs": ["acme"], "merge": {"auto": True, "kinds": ["triage"]}}))
    assert status == "pr" and "merged" not in seen and "merge_note" not in info


def test_a_refused_merge_leaves_the_pr_ready_and_says_why(settle, monkeypatch):
    go, waits, seen = settle
    def refuse(repo, number, method, delete):
        raise ci.gh.GhError("pr merge 7 on acme/a: Base branch requires 1 approving review")
    monkeypatch.setattr(workspace, "merge_pr", refuse)
    waits.append(([_run("smoke-test")], False))
    status, info = go(AUTO)
    assert status == "pr" and not seen["drafted"] and "requires 1 approving review" in info["merge_note"]
    assert "### Not merged" in seen["body"] and "Merge it by hand" in seen["body"]


def test_nothing_short_of_seen_green_is_merged(settle):
    go, waits, seen = settle
    waits.append(([_run("smoke-test", conclusion="failure")], False))
    assert go(from_dict({"orgs": ["acme"], "merge": {"auto": True}, "ci": {"fix_rounds": 0}}))[0] == "draft"
    waits.append(([_run("smoke-test")], False))
    assert go(AUTO, draft=True)[0] == "draft"                       # a draft is a person's
    waits.append(([], True))
    status, info = go(AUTO)                                           # no check ran: nothing green to merge on
    assert status == "pr" and "no check ran on the PR" in info["merge_note"]
    status, info = go(from_dict({"orgs": ["acme"], "merge": {"auto": True}, "ci": {"wait": False}}))
    assert status == "pr" and info["merge_note"] == "ci.wait is off"
    assert "merged" not in seen


def test_a_merge_follows_a_fix_round_too(settle):
    go, waits, seen = settle
    waits += [([_run("smoke-test", conclusion="failure")], False), ([_run("smoke-test", rid=5)], False)]
    status, info = go(from_dict({"orgs": ["acme"], "merge": {"auto": True, "method": "merge", "delete_branch": False}}))
    assert status == "merged" and seen["merged"] == (7, "merge", False) and info["rounds"] == 1


def test_merge_pr_runs_gh(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace.gh, "gh", lambda *a, **k: seen.update(args=a) or subprocess.CompletedProcess([], 0, "", ""))
    workspace.merge_pr("acme/a", 7, "squash", True)
    assert seen["args"] == ("pr", "merge", "7", "-R", "acme/a", "--squash", "--delete-branch")
    workspace.merge_pr("acme/a", 7, "rebase", False)
    assert seen["args"] == ("pr", "merge", "7", "-R", "acme/a", "--rebase")
    monkeypatch.setattr(workspace.gh, "gh", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "GraphQL: Pull request is in clean status"))
    with pytest.raises(ci.gh.GhError, match="clean status"):
        workspace.merge_pr("acme/a", 7)


def test_an_unsettled_review_in_a_fix_round_is_not_pushed(settle, monkeypatch):
    go, waits, seen = settle
    monkeypatch.setattr(runner, "_review_blocks", lambda result: "its self-review asked for changes that were not made (x)")
    waits.append(([_run("smoke-test", conclusion="failure")], False))
    status, info = go(AUTO)
    assert status == "draft" and seen["pushes"] == 0 and "was not pushed: its self-review asked" in seen["body"]
    assert "merged" not in seen


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


def test_lock_rows_merge_with_the_later_change_winning():
    first = [{"dir": ".", "status": "synced", "detail": ""}, {"dir": "app", "status": "unchanged", "detail": ""}]
    second = [{"dir": ".", "status": "unchanged", "detail": ""}, {"dir": "app", "status": "failed", "detail": "x"}]
    merged = {r["dir"]: r["status"] for r in runner._merge_locks(first, second)}
    assert merged == {".": "synced", "app": "failed"}


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


import os
AGENT_USER = os.environ.get("STEWARD_TEST_AGENT_USER")


@pytest.mark.skipif(not AGENT_USER, reason="set STEWARD_TEST_AGENT_USER to a user the test may sudo to")
def test_a_file_brought_back_from_an_apart_run_keeps_its_bytes():
    import tempfile
    from steward import isolation, sandbox
    base = Path(tempfile.mkdtemp(prefix="steward-bring-test-", dir="/tmp"))
    base.chmod(0o755)
    clone = base / "clone"
    clone.mkdir()
    clone.chmod(0o755)
    (clone / "keep.txt").write_text("x")
    proc, files = isolation.run_apart_bring(AGENT_USER, clone, ["/bin/sh", "-c", "printf 'a\\r\\nb\\377' > out.bin"],
                                            sandbox.environment(None), None, 60, bring=["out.bin", "missing.bin"])
    assert proc.returncode == 0 and files == {"out.bin": b"a\r\nb\xff"}
