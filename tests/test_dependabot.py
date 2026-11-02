"""With `dependabot.enabled`, Dependabot's open PRs are orders: the steward
waits for each one's checks and, red, works them on Dependabot's own branch
the way it settles its own PRs, within `ci.fix_rounds`. It never merges one,
never rewrites Dependabot's body, and says what it did in one comment whose
marker keeps the next run off the same head."""

import subprocess
from pathlib import Path

import pytest

from steward import ci, dependabot, inventory, plan, prompts, runner, workspace
from steward.config import from_dict
from steward.discover import Repo
from steward.execute.agent import AgentResult
from steward.inventory import Inventory

CFG = from_dict({"orgs": ["acme"], "dependabot": {"enabled": True}})
OFF = from_dict({"orgs": ["acme"]})


def _pr(number=7, title="Bump lodash from 4.17.20 to 4.17.21", body="", login=dependabot.BOT, draft=False,
        head_repo="acme/a", labels=(), ref=None, sha="abc1234"):
    return {"number": number, "title": title, "body": body, "html_url": f"https://github.com/acme/a/pull/{number}",
            "draft": draft, "user": {"login": login, "type": "Bot"},
            "labels": [{"name": l} for l in labels],
            "head": {"ref": ref or f"dependabot/npm_and_yarn/lodash-4.17.21", "sha": sha, "repo": {"full_name": head_repo}},
            "base": {"ref": "main", "repo": {"full_name": "acme/a"}}}


# ── reading a PR ──

@pytest.mark.parametrize("old, new, expect", [
    ("4.17.20", "4.17.21", "patch"), ("1.2.3", "1.3.0", "minor"), ("1.9.9", "2.0.0", "major"),
    ("v4", "v5", "major"), ("4.1.0", "4.2", "minor"), ("sha256:abc", "sha256:def", None), ("latest", "1.0.0", None),
])
def test_level(old, new, expect):
    assert dependabot.level(old, new) == expect


def test_updates_come_from_the_title_or_a_grouped_body():
    assert dependabot.updates("chore(deps): bump actions/checkout from 4.1.0 to 4.2.0", "") == [("actions/checkout", "4.1.0", "4.2.0")]
    body = "Bumps the npm group with 2 updates.\n\nUpdates `vite` from 5.0.0 to 5.1.0\nUpdates `react` from 18.2.0 to 19.0.0\n"
    bump = dependabot.from_pr(_pr(title="Bump the npm group across 1 directory with 2 updates", body=body))
    assert bump.updates == [("vite", "5.0.0", "5.1.0"), ("react", "18.2.0", "19.0.0")] and bump.level == "major"
    assert dependabot.from_pr(_pr(title="Bump something", body="")).level is None


def test_only_dependabots_own_ready_prs_count():
    assert dependabot.from_pr(_pr()).head == "dependabot/npm_and_yarn/lodash-4.17.21"
    assert dependabot.from_pr(_pr(login="renovate[bot]")) is None
    assert dependabot.from_pr(_pr(draft=True)) is None
    assert dependabot.from_pr(_pr(head_repo="fork/a")) is None


def test_worked_reads_the_marker_for_this_head_only():
    comments = [{"body": "### slopstopper-steward\n...\n<!-- steward:worked=abc1234 -->"}, {"body": "lgtm"}]
    assert dependabot.worked(comments, "abc1234") and not dependabot.worked(comments, "def5678")


def test_the_inventory_lists_bumps_only_when_enabled(monkeypatch):
    repo = Repo("acme/a", pinned="0.28.0", evidence=["mise-pin"])
    monkeypatch.setattr(inventory.gh, "contents", lambda r, p, ref=None: None)
    calls = []

    def api(path, **kw):
        calls.append(path)
        if "/pulls" in path:
            return [_pr(7), _pr(8, labels=["steward:ignore"]), _pr(9, sha="fff9999"),
                    {"number": 10, "html_url": "p", "head": {"ref": "steward/upgrade-0.29.0"}, "body": "<!-- steward:order=upgrade-0.29.0 -->"}]
        if path.endswith("/issues/9/comments?per_page=100"):
            return [{"body": "<!-- steward:worked=fff9999 -->"}]
        if "/comments" in path:
            return []
        if "/issues?" in path:
            return []
        if "/actions/workflows" in path:
            return {"workflows": []}
        raise AssertionError(path)
    monkeypatch.setattr(inventory.gh, "api", api)
    inv = inventory.take(repo, "0.28.0", CFG)
    assert [(b.number, b.worked) for b in inv.dependabot] == [(7, False), (9, True)]
    assert inv.open_orders == {"upgrade-0.29.0": {"number": 10, "url": "p"}}
    assert inv.to_dict()["dependabot"][0]["level"] == "patch"
    calls.clear()
    assert inventory.take(repo, "0.28.0", OFF).dependabot == [] and not any("/comments" in c for c in calls)


# ── planning ──

def _inv(*bumps):
    inv = Inventory(Repo("acme/a", pinned="0.28.0", installed_from="0.28.0", evidence=["mise-pin"]), "0.28.0")
    inv.dependabot = list(bumps)
    return inv


def test_bumps_are_orders_on_dependabots_branch_within_the_budget():
    patch = dependabot.from_pr(_pr(7))
    major = dependabot.from_pr(_pr(8, title="Bump react from 18.2.0 to 19.0.0"))
    done = dependabot.from_pr(_pr(9)); done.worked = True
    unknown = dependabot.from_pr(_pr(10, title="Bump something"))
    p = plan.plan_repo(_inv(patch, major, done, unknown), "0.28.0", CFG)
    assert [o.id for o in p.orders] == ["dependabot-7"]
    order = p.orders[0]
    assert order.kind == "dependabot" and order.agentic and order.branch == "dependabot/npm_and_yarn/lodash-4.17.21"
    assert order.details["number"] == 7 and order.details["level"] == "patch" and order.to_dict()["branch"] == order.branch
    whys = {r["order"]: r["why"] for r in p.skipped}
    assert "major bump" in whys["dependabot-8"] and "already worked head `abc1234`" in whys["dependabot-9"]
    assert "cannot tell how far" in whys["dependabot-10"]
    assert plan.plan_repo(_inv(patch), "0.28.0", OFF).orders == []
    wide = from_dict({"orgs": ["acme"], "dependabot": {"enabled": True, "updates": ["patch", "minor", "major"]}})
    assert [o.id for o in plan.plan_repo(_inv(patch, major), "0.28.0", wide).orders] == ["dependabot-7", "dependabot-8"]


def test_bumps_have_a_budget_of_their_own_after_the_rest():
    bumps = [dependabot.from_pr(_pr(n)) for n in (7, 8, 9)]
    inv = _inv(*bumps)
    inv.findings = [inventory.Finding(1, "t", "u", ["slopstopper"], check="hygiene:docs-size")]
    inv.open_orders = {"upgrade-0.29.0": {"number": 5, "url": "p"}, "triage-2": {"number": 6, "url": "p"}}   # prs_per_repo spent
    p = plan.plan_repo(inv, "0.28.0", CFG)
    assert [o.id for o in p.orders] == ["dependabot-7", "dependabot-8", "dependabot-9"]   # the steward's own budget is not theirs
    assert [o.id for o in p.deferred] == ["triage-1"]
    two = from_dict({"orgs": ["acme"], "dependabot": {"enabled": True, "budget": 2}})
    p = plan.plan_repo(_inv(*bumps), "0.28.0", two)
    assert [o.id for o in p.orders] == ["dependabot-7", "dependabot-8"] and [o.id for o in p.deferred] == ["dependabot-9"]


def test_config_keys():
    assert OFF.dependabot_enabled is False and OFF.dependabot_updates == ["patch", "minor"]
    assert OFF.dependabot_merge is False and OFF.dependabot_budget == 5
    c = from_dict({"orgs": ["a"], "dependabot": {"enabled": True, "updates": ["patch"], "merge": True, "budget": 10}})
    assert c.dependabot_enabled and c.dependabot_updates == ["patch"] and c.dependabot_merge and c.dependabot_budget == 10
    assert from_dict({"orgs": ["a"], "dependabot": {"merge": "yes"}}).dependabot_merge is False
    with pytest.raises(ValueError, match="dependabot.updates"):
        from_dict({"orgs": ["a"], "dependabot": {"updates": ["huge"]}})
    with pytest.raises(ValueError, match="merge.kinds"):
        from_dict({"orgs": ["a"], "merge": {"kinds": ["dependabot"]}})   # never merged: step two


# ── the brief ──

def _order():
    return plan.Order("dependabot-7", "dependabot", "acme/a", "Bump lodash from 4.17.20 to 4.17.21", "r", 4, "main",
                      "0.28.0", "0.28.0", agentic=True, head="dependabot/npm_and_yarn/lodash-4.17.21",
                      details={"number": 7, "url": "https://github.com/acme/a/pull/7", "sha": "abc1234", "level": "patch",
                               "updates": [["lodash", "4.17.20", "4.17.21"]]})


def test_the_brief_names_the_bump_the_logs_and_the_limits():
    inv = Inventory(Repo("acme/a", pinned="0.28.0"), "0.28.0")
    failing = [{"name": "test (ubuntu)", "conclusion": "failure", "url": "u", "workflow": ".github/workflows/ci.yml", "ours": False}]
    brief = prompts.dependabot_fix(_order(), inv, failing, "Bumps lodash.\n" + "x" * 9000, "hungovercoders/slopstopper",
                                   checks=["hygiene:docs-size"])
    for needle in ("#7", "`lodash` 4.17.20 → 4.17.21", "patch bump", "test (ubuntu)", ".steward-ci-failures.md", "<pr_body>",
                   "… (cut)", "cannot run the repository's own build", "Never loosen, skip", "`hygiene:docs-size`",
                   "should-not-merge", ".steward-result.json"):
        assert needle in brief
    assert "slopstopper-triage/SKILL.md" not in brief
    assert "Dependabot's pull request #7" in prompts.self_review(_order(), inv, "s")
    assert "Never loosen, skip" in prompts.correct(_order(), inv, ["x"], "hungovercoders/slopstopper")


# ── working one ──

def _run(name, status="completed", conclusion="success", rid=1, app="github-actions-other"):
    return {"id": rid, "name": name, "status": status, "conclusion": conclusion, "url": f"https://x/{rid}", "app": app}


@pytest.fixture
def bump(monkeypatch, tmp_path):
    seen = {"agent": 0, "pushes": [], "comments": [], "briefs": [], "api": []}
    waits = []
    monkeypatch.setattr(runner.ci, "wait", lambda repo, sha, seconds, expected=None, workflows=None: (*waits.pop(0), {}))
    monkeypatch.setattr(runner.ci, "expected_workflows", lambda root: set())
    monkeypatch.setattr(runner.ci, "describe", lambda repo, runs, workflows=None: [
        {**r, "workflow": ".github/workflows/ci.yml", "ours": False} for r in ci.failed(runs)])
    monkeypatch.setattr(runner.ci, "log_tail", lambda repo, job: "TypeError: _.chunk is not a function")
    monkeypatch.setattr(runner.gh, "api", lambda path, **k: seen["api"].append(path) or {"body": "Bumps lodash. Release notes: chunk moved."})
    monkeypatch.setattr(runner.refresh, "cli_for", lambda *a: Path("/v/bin/slopstopper"))
    monkeypatch.setattr(runner, "_runnable", lambda ws, cli: (["hygiene:docs-size"], []))
    monkeypatch.setattr(runner, "_verify_pass", lambda ws, cli, ctx=None: (
        [{"check": "hygiene:docs-size", "exit_code": 1, "status": "fail", "failures": ["already over budget on main"]}], []))
    monkeypatch.setattr(runner, "_sync_locks", lambda ws: [])
    monkeypatch.setattr(workspace.Workspace, "dirty", lambda self: True)
    monkeypatch.setattr(runner, "_agent_pass", lambda order, inv, cfg, ws, cli, ctx, brief, checks, apart:
                        seen.__setitem__("agent", seen["agent"] + 1) or seen["briefs"].append(brief)
                        or AgentResult(True, outcome="fixed", summary="used _.chunk from lodash/chunk", cost_usd=0.3, turns=9))
    monkeypatch.setattr(workspace, "head_sha", lambda ws: f"sha{len(seen['pushes'])}")
    monkeypatch.setattr(workspace, "commit", lambda ws, msg: seen.__setitem__("commit", msg) or True)
    monkeypatch.setattr(workspace, "push_onto", lambda ws, branch: seen["pushes"].append(branch))
    monkeypatch.setattr(workspace, "push", lambda ws, branch: (_ for _ in ()).throw(AssertionError("force-with-lease on Dependabot's branch")))
    monkeypatch.setattr(workspace, "comment_pr", lambda repo, n, body: seen["comments"].append((n, body)))
    monkeypatch.setattr(workspace, "merge_pr", lambda repo, n, method, delete: seen.__setitem__("merged", (n, method, delete)))
    monkeypatch.setattr(workspace, "update_pr_body", lambda *a: (_ for _ in ()).throw(AssertionError("Dependabot's body is Dependabot's")))
    inv = Inventory(Repo("acme/a", pinned="0.28.0"), "0.28.0")
    ws = workspace.Workspace("acme/a", tmp_path, "main")

    def go(cfg=CFG):
        ctx = runner.RunContext(base=tmp_path, agent_checked=True)
        return runner.work_dependabot(_order(), inv, cfg, ws, ctx)
    return go, waits, seen


def test_a_green_bump_needs_nothing_and_gets_no_comment(bump):
    go, waits, seen = bump
    waits.append(([_run("test (ubuntu)")], False))
    out = go()
    assert out.status == "green" and seen["agent"] == 0 and seen["comments"] == [] and out.pr_url.endswith("/pull/7")


def test_a_red_bump_is_fixed_on_dependabots_branch_and_commented(bump):
    go, waits, seen = bump
    waits += [([_run("test (ubuntu)", conclusion="failure")], False), ([_run("test (ubuntu)", rid=2)], False)]
    out = go()
    assert out.status == "green" and seen["agent"] == 1 and seen["pushes"] == ["dependabot/npm_and_yarn/lodash-4.17.21"]
    assert seen["commit"].startswith("fix(deps): adapt to the dependency bump (round 1)")
    assert "chunk moved" in seen["briefs"][0] and "#7" in seen["briefs"][0]
    assert seen["api"] == ["repos/acme/a/pulls/7"]
    (number, body), = seen["comments"]
    assert number == 7 and "green after 1 fix round(s)" in body and "used _.chunk" in body and "<!-- steward:worked=sha1 -->" in body
    assert out.ci["rounds"] == 1 and out.agent["summary"] == "used _.chunk from lodash/chunk"


def test_a_check_already_red_before_the_fix_does_not_block_the_push(bump):
    go, waits, seen = bump
    waits += [([_run("test (ubuntu)", conclusion="failure")], False), ([_run("test (ubuntu)", rid=2)], False)]
    assert go().status == "green" and seen["pushes"]   # hygiene:docs-size was red before the agent too


def test_still_red_after_the_rounds_is_said_on_the_pr(bump):
    go, waits, seen = bump
    red = ([_run("test (ubuntu)", conclusion="failure")], False)
    waits += [red, red, red]
    out = go()
    assert out.status == "red" and seen["agent"] == 2 and len(seen["pushes"]) == 2
    (number, body), = seen["comments"]
    assert "still red after 2 fix round(s)" in body and "Left for a person" in body and "<!-- steward:worked=sha2 -->" in body


def test_an_agent_that_changed_nothing_leaves_a_comment_so_the_next_run_moves_on(bump, monkeypatch):
    go, waits, seen = bump
    monkeypatch.setattr(workspace.Workspace, "dirty", lambda self: False)
    waits.append(([_run("test (ubuntu)", conclusion="failure")], False))
    out = go()
    assert out.status == "red" and seen["pushes"] == [] and "changed nothing in round 1" in seen["comments"][0][1]
    assert "<!-- steward:worked=sha0 -->" in seen["comments"][0][1]


def test_an_unsettled_self_review_is_not_pushed(bump, monkeypatch):
    go, waits, seen = bump
    monkeypatch.setattr(runner, "_review_blocks", lambda result: "its self-review asked for changes that were not made (x)")
    waits.append(([_run("test (ubuntu)", conclusion="failure")], False))
    out = go()
    assert out.status == "red" and seen["pushes"] == [] and "was not pushed: its self-review" in seen["comments"][0][1]


def test_no_checks_or_wait_off_is_skipped_quietly(bump):
    go, waits, seen = bump
    waits.append(([], True))
    assert go().status == "skipped" and seen["comments"] == []
    out = go(from_dict({"orgs": ["acme"], "dependabot": {"enabled": True}, "ci": {"wait": False}}))
    assert out.status == "skipped" and "ci.wait is off" in out.detail


MERGE = from_dict({"orgs": ["acme"], "dependabot": {"enabled": True, "merge": True}})


def test_a_green_bump_is_merged_with_dependabot_merge(bump):
    go, waits, seen = bump
    waits.append(([_run("test (ubuntu)")], False))
    out = go(MERGE)
    assert out.status == "merged" and seen["merged"] == (7, "squash", True) and seen["comments"] == []
    assert out.ci["merged"] and "· merged" in __import__("steward.report", fromlist=["x"]).RunReport("pr", [], [out]).markdown()


def test_a_bump_fixed_in_a_round_is_merged_once_green(bump):
    go, waits, seen = bump
    waits += [([_run("test (ubuntu)", conclusion="failure")], False), ([_run("test (ubuntu)", rid=2)], False)]
    out = go(from_dict({"orgs": ["acme"], "dependabot": {"enabled": True, "merge": True}, "merge": {"method": "rebase", "delete_branch": False}}))
    assert out.status == "merged" and seen["merged"] == (7, "rebase", False) and seen["pushes"]
    assert "green after 1 fix round(s)" in seen["comments"][0][1]       # the fix is still accounted for on the PR


def test_a_refused_merge_is_said_on_the_pr_and_the_pr_left_green(bump, monkeypatch):
    go, waits, seen = bump
    def refuse(repo, n, method, delete):
        raise ci.gh.GhError("pr merge 7 on acme/a: Base branch requires 1 approving review")
    monkeypatch.setattr(workspace, "merge_pr", refuse)
    waits.append(([_run("test (ubuntu)")], False))
    out = go(MERGE)
    assert out.status == "green" and "requires 1 approving review" in out.ci["merge_note"]
    (number, body), = seen["comments"]
    assert "could not merge it" in body and "<!-- steward:worked=sha0 -->" in body   # the next run moves on


def test_merge_is_off_by_default_and_never_for_red(bump):
    go, waits, seen = bump
    waits.append(([_run("test (ubuntu)")], False))
    assert go().status == "green" and "merged" not in seen
    red = ([_run("test (ubuntu)", conclusion="failure")], False)
    waits += [red, red, red]
    assert go(MERGE).status == "red" and "merged" not in seen
    assert from_dict({"orgs": ["acme"], "merge": {"auto": True}}).dependabot_merge is False   # merge.auto is the steward's own


def test_the_work_dispatch_checks_out_dependabots_branch(monkeypatch, tmp_path):
    seen = {}
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    monkeypatch.setattr(workspace, "clone", lambda repo, default, into: ws)
    monkeypatch.setattr(workspace, "checkout_remote", lambda ws_, name: seen.__setitem__("remote", name))
    monkeypatch.setattr(workspace, "branch", lambda ws_, name: (_ for _ in ()).throw(AssertionError("a steward branch for Dependabot's PR")))
    monkeypatch.setattr(runner, "work_dependabot", lambda order, inv, cfg, ws_, ctx: runner.Outcome(order.to_dict(), "green"))
    ctx = runner.RunContext(base=tmp_path, agent_checked=True)
    out = runner._work(_order(), Inventory(Repo("acme/a"), "0.28.0"), CFG, ctx)
    assert out.status == "green" and seen["remote"] == "dependabot/npm_and_yarn/lodash-4.17.21"


def test_workspace_helpers_run_the_right_commands(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(workspace.Workspace, "git", lambda self, *a, check=True: seen.append(a) or subprocess.CompletedProcess([], 0, "", ""))
    ws = workspace.Workspace("acme/a", tmp_path, "main")
    workspace.checkout_remote(ws, "dependabot/x")
    workspace.push_onto(ws, "dependabot/x")
    assert seen == [("fetch", "origin", "dependabot/x"), ("checkout", "-B", "dependabot/x", "origin/dependabot/x"),
                    ("push", "--no-verify", "origin", "dependabot/x")]
    monkeypatch.setattr(workspace.gh, "gh", lambda *a, **k: seen.append((a, k.get("input"))) or subprocess.CompletedProcess([], 0, "", ""))
    workspace.comment_pr("acme/a", 7, "hello")
    assert seen[-1] == (("pr", "comment", "7", "-R", "acme/a", "--body-file", "-"), "hello")
