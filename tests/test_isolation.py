"""The agent as its own OS user.

`sync_back` is tested on its own. The end-to-end test runs a fake `claude`
as a real second user and needs one: `STEWARD_TEST_AGENT_USER` names it
(CI creates `steward-agent`; locally, `sudo useradd --system steward-agent`).
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from steward import isolation, workspace
from steward.config import from_dict
from steward.execute import agent

CFG = from_dict({"orgs": ["acme"]})
AGENT_USER = os.environ.get("STEWARD_TEST_AGENT_USER")


# ── sync_back ────────────────────────────────────────────────────────

def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_sync_back_copies_changes_and_removals_but_never_git(tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "old\n", "gone.txt": "x", "keep.txt": "same", ".git/config": "ours",
                                       ".steward-prompt.md": "brief"})
    tree = _tree(tmp_path / "tree", {"README.md": "new\n", "keep.txt": "same", "docs/new.md": "hi", ".git/config": "theirs"})
    (tree / "run.sh").write_text("echo\n")
    (tree / "run.sh").chmod(0o755)
    out = isolation.sync_back(tree, clone, skip=[".steward-prompt.md"])
    assert sorted(out.changed) == ["README.md", "docs/new.md", "run.sh"] and out.removed == ["gone.txt"]
    assert (clone / "README.md").read_text() == "new\n" and not (clone / "gone.txt").exists()
    assert (clone / ".git" / "config").read_text() == "ours"           # .git is never touched
    assert (clone / ".steward-prompt.md").read_text() == "brief"       # the steward's own files stay
    assert os.access(clone / "run.sh", os.X_OK) and out.rejected == []


def test_sync_back_refuses_symlinks_out_of_the_tree(tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    tree = _tree(tmp_path / "tree", {"README.md": "x", "docs/a.md": "a"})
    os.symlink("/proc/1/environ", tree / "leak")
    os.symlink("../../etc/passwd", tree / "docs" / "up")
    os.symlink("../.git/config", tree / "docs" / "git")
    os.symlink("a.md", tree / "docs" / "alias.md")                     # stays inside: fine
    os.mkfifo(tree / "pipe")
    out = isolation.sync_back(tree, clone)
    assert not (clone / "leak").exists() and not (clone / "docs" / "up").is_symlink()
    assert not (clone / "docs" / "git").is_symlink() and not (clone / "pipe").exists()
    assert os.readlink(clone / "docs" / "alias.md") == "a.md"
    assert {r.split(":")[0] for r in out.rejected} == {"leak", "docs/up", "docs/git", "pipe"}


def test_sync_back_never_writes_through_a_symlinked_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    os.symlink(str(outside), clone / "docs")       # a directory symlink in the clone pointing away
    tree = _tree(tmp_path / "tree", {"README.md": "x", "docs/evil.md": "pwned"})
    isolation.sync_back(tree, clone)
    assert not (outside / "evil.md").exists()
    assert (clone / "docs").is_dir() and not (clone / "docs").is_symlink() and (clone / "docs" / "evil.md").exists()


def test_command_keeps_secrets_off_the_command_line(tmp_path):
    iso = isolation.Isolated("steward-agent", tmp_path, tmp_path / "tree", tmp_path / "home")
    cmd = isolation.command(iso, ["claude", "-p", "brief"], {"PATH": "/x", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-secret",
                                                             "PYTHONSAFEPATH": "1"}, Path("/venv/bin"))
    assert "sk-secret" not in " ".join(cmd)
    assert "--preserve-env=ANTHROPIC_API_KEY,PYTHONSAFEPATH" in cmd
    assert cmd[cmd.index("-C") + 1] == str(iso.tree) and f"HOME={iso.home}" in cmd
    assert "PATH=/venv/bin:/usr/local/bin:/usr/bin:/bin" in cmd


def test_set_but_refused_is_loud(monkeypatch):
    monkeypatch.setattr(isolation, "_sudo", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "a password is required"))
    assert "refused" in isolation.unavailable("nobody-here")


# ── the agent as a real second user ──────────────────────────────────

FAKE_CLAUDE = r'''#!/usr/bin/python3 -I
import json, os, sys
args = sys.argv[1:]
brief = dict(kv.split("=", 1) for kv in args[args.index("-p") + 1].split())
seen = {}

def attempt(name, fn):
    try:
        fn()
        seen[name] = "allowed"
    except OSError as exc:
        seen[name] = "denied"

attempt("write steward .git/config", lambda: open(os.path.join(brief["CLONE"], ".git", "config"), "a").write("x"))
attempt("write steward clone file", lambda: open(os.path.join(brief["CLONE"], "README.md"), "a").write("x"))
attempt("write venv slopstopper", lambda: open(os.path.join(brief["VENV"], "bin", "slopstopper"), "w").write("x"))
attempt("read steward environ", lambda: open(f"/proc/{brief['PID']}/environ").read())
seen["GH_TOKEN visible"] = "GH_TOKEN" in os.environ
seen["model key visible"] = os.environ.get("ANTHROPIC_API_KEY") == "sk-test"
seen["cwd is its own copy"] = os.getcwd() != brief["CLONE"]
seen["uid differs"] = os.getuid() != int(brief["UID"])
with open("README.md", "a") as fh:
    fh.write("fixed by the agent\n")
open("new.md", "w").write("new\n")
os.remove("gone.txt")
os.symlink(f"/proc/{brief['PID']}/environ", "leak.md")
open(".steward-result.json", "w").write(json.dumps({"outcome": "fixed", "summary": json.dumps(seen)}))
print(json.dumps({"total_cost_usd": 0.0, "num_turns": 1}))
'''


@pytest.mark.skipif(not AGENT_USER, reason="set STEWARD_TEST_AGENT_USER to a user the test may sudo to")
def test_the_agent_as_its_own_user(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="steward-iso-test-", dir="/tmp"))
    try:
        base.chmod(0o755)
        venv_bin = base / "venvs" / "0.27.0" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "slopstopper").write_text("#!/bin/sh\nexit 0\n")
        claude = venv_bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        for f in (claude, venv_bin / "slopstopper"):
            f.chmod(0o755)
        clone = base / "clones" / "acme__a" / "triage-1"
        clone.mkdir(parents=True)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        for cmd in (["git", "init", "-q", "-b", "main"],):
            subprocess.run(cmd, cwd=clone, check=True, env=env)
        (clone / "README.md").write_text("# a\n")
        (clone / "gone.txt").write_text("bye\n")
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True, env=env)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                       cwd=clone, check=True, env=env)
        git_config = (clone / ".git" / "config").read_bytes()

        monkeypatch.setenv(isolation.AGENT_USER_ENV, AGENT_USER)
        monkeypatch.setenv("GH_TOKEN", "ghs_secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(agent, "available", lambda: isolation.unavailable(AGENT_USER))
        ws = workspace.Workspace("acme/a", clone, "main")
        brief = f"CLONE={clone} VENV={venv_bin.parent} PID={os.getpid()} UID={os.getuid()}"
        result = agent.run(ws, brief, CFG, cli_bin=venv_bin / "slopstopper", timeout=120)

        assert result.ran and result.outcome == "fixed", result.error
        seen = json.loads(result.summary)
        assert seen == {
            "write steward .git/config": "denied", "write steward clone file": "denied",
            "write venv slopstopper": "denied", "read steward environ": "denied",
            "GH_TOKEN visible": False, "model key visible": True, "cwd is its own copy": True, "uid differs": True,
        }
        assert (clone / "README.md").read_text() == "# a\nfixed by the agent\n"      # its work came back
        assert (clone / "new.md").read_text() == "new\n" and not (clone / "gone.txt").exists()
        assert not os.path.lexists(clone / "leak.md") and "leak.md" in result.notes  # the escape was refused, and said
        assert (clone / ".git" / "config").read_bytes() == git_config
        assert (venv_bin / "slopstopper").read_text() == "#!/bin/sh\nexit 0\n"
        assert not list(Path("/tmp").glob("steward-agent-*/tree/README.md"))           # its copy is gone
    finally:
        shutil.rmtree(base, ignore_errors=True)
