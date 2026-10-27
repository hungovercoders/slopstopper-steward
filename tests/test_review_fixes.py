"""Regression tests for the first code review of the steward."""

import subprocess
from pathlib import Path

import pytest

from steward import yamlish, workspace
from steward.config import from_dict
from steward import sandbox
from steward.execute import agent


# ── yamlish: lists at the key's indent, blank keys are null ──────────

def test_list_at_key_indent():
    data = yamlish.loads("orgs:\n- hungovercoders\n- other\nmode: pr\nrepos:\n  include:\n  - a/b\n  exclude: []\n")
    assert data == {"orgs": ["hungovercoders", "other"], "mode": "pr", "repos": {"include": ["a/b"], "exclude": []}}


def test_blank_key_is_null():
    data = yamlish.loads("repos:\n  include:\n    # - acme/a\n  exclude: [x/y]\nlabels:\n  pr:\nupgrade:\n  min_age_days:\n  policy: patch\nlast:\n")
    assert data == {"repos": {"include": None, "exclude": ["x/y"]}, "labels": {"pr": None},
                    "upgrade": {"min_age_days": None, "policy": "patch"}, "last": None}
    cfg = from_dict({"orgs": ["a"], **data})
    assert cfg.include == [] and cfg.pr_labels == ["slopstopper", "steward"] and cfg.min_age_days == 2 and cfg.policy == "patch"


def test_bare_dash_item_is_null():
    assert yamlish.loads("a:\n  -\n  - x\n") == {"a": [None, "x"]}


# ── agent box: environment and tools ─────────────────────────────────

def test_agent_environment_drops_github_credentials(tmp_path):
    env = agent.environment(tmp_path / "bin" / "slopstopper", base={
        "PATH": "/usr/bin", "HOME": "/h", "GH_TOKEN": "ghs_x", "GITHUB_TOKEN": "ghp_y", "STEWARD_TOKEN": "z",
        "STEWARD_APP_PRIVATE_KEY": "pem", "ANTHROPIC_API_KEY": "sk", "AWS_SECRET_ACCESS_KEY": "no"})
    assert set(env) - set(sandbox.HARDENING) == {"PATH", "HOME", "ANTHROPIC_API_KEY"}
    assert env["PATH"].startswith(str(tmp_path / "bin"))


def test_agent_tools_cannot_write_to_github_or_fetch_code():
    for tool in ("Bash(gh *)", "Bash(git push*)", "Bash(npx *)", "Bash(pip *)", "Bash(curl *)", "WebFetch"):
        assert tool in agent.DISALLOWED_TOOLS
    assert not any(t.startswith(("Bash(npx", "Bash(pip", "Bash(npm", "Bash(gh")) for t in agent.ALLOWED_TOOLS)
    cmd = agent._command("brief", from_dict({"orgs": ["a"]}))
    assert "--disallowedTools" in cmd and "Bash(gh *)" in cmd


# ── skills fetched from one ref ──────────────────────────────────────

def test_fetch_tree_uses_the_ref_it_listed(monkeypatch, tmp_path):
    calls = []

    def api(path, **kw):
        calls.append(path)
        if "git/ref/tags/v9.9.9" in path:
            return None  # the tag does not exist
        if "contents/.claude/skills/slopstopper-triage?ref=main" in path:
            return [{"name": "SKILL.md", "type": "file"}, {"name": "references", "type": "dir"}]
        if "contents/.claude/skills/slopstopper-triage/references?ref=main" in path:
            return [{"name": "upstream.md", "type": "file"}]
        if "contents/.claude/skills/slopstopper-install?ref=main" in path:
            return [{"name": "SKILL.md", "type": "file"}]
        raise AssertionError(path)

    def contents(repo, path, ref=None):
        calls.append(f"{path}@{ref}")
        assert ref == "main", "files must come from the ref that was listed"
        return f"# {path}"
    monkeypatch.setattr(agent.gh, "api", api)
    monkeypatch.setattr(agent.gh, "contents", contents)
    repo = tmp_path / "clone"
    (repo / ".git" / "info").mkdir(parents=True)
    ws = workspace.Workspace("acme/a", repo, "main")
    assert agent.ensure_skills(ws, "9.9.9", "hungovercoders/slopstopper") == "fetched@main"
    assert (repo / ".claude/skills/slopstopper-triage/references/upstream.md").read_text().endswith("upstream.md")
    assert (repo / ".claude/skills/slopstopper-install/SKILL.md").is_file()


def test_fetch_failure_is_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(agent.gh, "api", lambda path, **kw: [] if "contents" in path else {"ref": "x"})
    monkeypatch.setattr(agent.gh, "contents", lambda r, p, ref=None: None)
    repo = tmp_path / "clone"
    (repo / ".git" / "info").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="could not fetch skill"):
        agent.ensure_skills(workspace.Workspace("acme/a", repo, "main"), "0.27.0", "hungovercoders/slopstopper")


# ── the clone can push, and a re-run replaces its own stale branch ───

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_rerun_replaces_an_existing_steward_branch(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "README.md").write_text("# adopter\n")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    _git(seed, "add", "README.md")
    _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "readme")
    _git(seed, "push", "-q", "origin", "main")

    def run_once(n: int) -> str:
        clone = tmp_path / f"clone{n}"
        _git(tmp_path, "clone", "-q", "--depth", "50", "--branch", "main", str(origin), str(clone))
        ws = workspace.prepare(workspace.Workspace("acme/adopter", clone, "main"))
        assert _git(clone, "config", "credential.helper").stdout.strip() == "!gh auth git-credential"
        workspace.branch(ws, "steward/upgrade-0.27.0")
        (clone / "mise.toml").write_text(f"pin {n}\n")
        assert workspace.commit(ws, f"attempt {n}")
        workspace.push(ws, "steward/upgrade-0.27.0")
        return _git(clone, "rev-parse", "HEAD").stdout.strip()

    first = run_once(1)
    second = run_once(2)  # the remote branch exists from run 1: the lease must allow replacing it
    tip = _git(tmp_path, "--git-dir", str(origin), "rev-parse", "steward/upgrade-0.27.0").stdout.strip()
    assert tip == second != first
    log = _git(tmp_path, "--git-dir", str(origin), "log", "--oneline", "steward/upgrade-0.27.0").stdout
    assert "attempt 1" not in log and "attempt 2" in log  # replaced, not stacked
