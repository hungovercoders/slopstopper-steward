"""The agent as its own OS user.

`sync_back` is tested on its own. The end-to-end test runs a fake `claude`
as a real second user and needs one: `STEWARD_TEST_AGENT_USER` names it
(CI creates `steward-agent`; locally, `sudo useradd --system steward-agent`).
"""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
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
    iso = isolation.Isolated("steward-agent", tmp_path, tmp_path / "tree", tmp_path / "home", tmp_path / "tmp")
    env = {"PATH": "/x", "HOME": "/h", "TMPDIR": "/run/user/1000", "USER": "steward", "RUNNER_TEMP": "/r",
           "ANTHROPIC_API_KEY": "sk-secret", "PYTHONSAFEPATH": "1"}
    cmd = isolation.command(iso, ["claude", "-p", "brief"], env, Path("/venv/bin"), timeout=600)
    assert "sk-secret" not in " ".join(cmd)
    assert "--preserve-env=ANTHROPIC_API_KEY,PYTHONSAFEPATH" in cmd   # the steward's own dirs and names stay behind
    assert cmd[cmd.index("-C") + 1] == str(iso.tree) and f"HOME={iso.home}" in cmd
    assert f"TMPDIR={iso.tmp}" in cmd and "USER=steward-agent" in cmd and "LOGNAME=steward-agent" in cmd
    assert "PATH=/venv/bin:/usr/local/bin:/usr/bin:/bin" in cmd
    i = cmd.index("/usr/bin/timeout")                                 # the agent user's own timeout wraps the run
    assert cmd[i + 1:i + 3] == [f"--kill-after={isolation.KILL_AFTER}", "600"] and cmd[i + 3] == "claude"


def test_set_but_refused_is_loud(monkeypatch):
    seen = {}

    def refuse(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, "", "sudo: sorry, you are not allowed to preserve the environment")
    monkeypatch.setattr(isolation.subprocess, "run", refuse)
    why = isolation.unavailable("nobody-here")
    assert "cannot run as nobody-here" in why and "preserve the environment" in why
    assert any(a.startswith("--preserve-env=") for a in seen["cmd"])   # the probe asks for what the run will ask for
    assert seen["cmd"][-2:] == ["claude", "--version"]


# ── fail-closed copy-back, symlink chains, the fast path, the shared skip list

def test_an_unreadable_file_aborts_with_the_clone_untouched(tmp_path, monkeypatch):
    clone = _tree(tmp_path / "clone", {"README.md": "old\n", "gone.txt": "x"})
    tree = _tree(tmp_path / "tree", {"README.md": "new\n", "secret.bin": "s"})
    real = os.open

    def guarded(path, *a, **k):
        if str(path).endswith("secret.bin"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)
    monkeypatch.setattr(isolation.os, "open", guarded)
    with pytest.raises(isolation.IsolationError, match="secret.bin"):
        isolation.sync_back(tree, clone)
    assert (clone / "README.md").read_text() == "old\n" and (clone / "gone.txt").exists()


def test_an_unreadable_directory_is_never_mistaken_for_deleted(tmp_path, monkeypatch):
    clone = _tree(tmp_path / "clone", {"README.md": "x", "docs/a.md": "a"})
    tree = _tree(tmp_path / "tree", {"README.md": "x", "docs/a.md": "a"})
    real_walk = os.walk

    def walk(top, **kw):
        if Path(top) == tree:
            kw["onerror"](PermissionError(13, "Permission denied", str(tree / "docs")))
        yield from real_walk(top, **kw)
    monkeypatch.setattr(isolation.os, "walk", walk)
    with pytest.raises(isolation.IsolationError, match="docs"):
        isolation.sync_back(tree, clone)
    assert (clone / "docs" / "a.md").exists()


def test_a_symlink_chain_out_of_the_repo_or_into_git_is_refused(tmp_path):
    outer = tmp_path / "work"
    clone = _tree(outer / "clone", {"README.md": "x", ".git/config": "ours"})
    tree = _tree(outer / "tree", {"README.md": "x"})
    os.symlink(".", tree / "here")            # inside on its own
    os.symlink("here/..", tree / "up")        # lexically ".", really the clone's parent
    os.symlink("here/.git", tree / "g")       # lexically "here/.git", really the clone's .git
    os.symlink("README.md", tree / "fine.md")
    out = isolation.sync_back(tree, clone)
    assert not os.path.lexists(clone / "up") and not os.path.lexists(clone / "g")
    assert os.path.islink(clone / "here") and os.readlink(clone / "fine.md") == "README.md"
    assert {r.split(":")[0] for r in out.rejected} == {"up", "g"} and set(out.changed) == {"here", "fine.md"}


def test_an_untouched_file_is_not_read(tmp_path, monkeypatch):
    clone = _tree(tmp_path / "clone", {"big.bin": "same"})
    tree = _tree(tmp_path / "tree", {"big.bin": "same"})
    st = os.stat(clone / "big.bin")
    os.utime(tree / "big.bin", ns=(st.st_atime_ns, st.st_mtime_ns))   # what cp --preserve=timestamps leaves
    monkeypatch.setattr(isolation, "_regular_fd", lambda root, rel: pytest.fail(f"read {rel}"))
    assert isolation.sync_back(tree, clone).changed == []


def test_the_skip_list_is_the_clones_local_only_list():
    assert agent.STEWARD_FILES is workspace.LOCAL_ONLY


def test_a_copy_back_failure_is_a_result_not_a_crash(tmp_path, monkeypatch):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    monkeypatch.setenv(isolation.AGENT_USER_ENV, "steward-agent")
    monkeypatch.setattr(agent, "available", lambda: None)

    def broken(*a, **k):
        raise isolation.IsolationError("cannot read `x` in the agent's tree: Permission denied")
    monkeypatch.setattr(agent, "_run_isolated", broken)
    result = agent.run(workspace.Workspace("acme/a", clone, "main"), "brief", CFG)
    assert result.ran and result.outcome == "skipped" and "not copied back" in result.error


# ── the agent as a real second user ──────────────────────────────────

FAKE_CLAUDE = r'''#!/usr/bin/python3 -I
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("fake-claude 0.0")
    sys.exit(0)
brief0 = args[args.index("-p") + 1]
if "DETACH=1" in brief0:
    import subprocess
    # outside the agent's process group (setsid), still writing after the agent exits
    subprocess.Popen(["setsid", "sh", "-c", "while true; do date +%N > churn.md; sleep 0.01; done"],
                     start_new_session=True)
    open(".steward-result.json", "w").write(json.dumps({"outcome": "fixed", "summary": "detached"}))
    print(json.dumps({"total_cost_usd": 0.0, "num_turns": 1}))
    sys.exit(0)
if brief0.startswith("SWAPROOT="):
    here = os.getcwd()
    os.rename(here, here + ".moved")             # its own root is its own: the tree can be swapped
    os.symlink(brief0.split("=", 1)[1], here)
    sys.exit(0)
if "HANG=1" in brief0:
    import subprocess, time
    open("before.md", "w").write("written before the hang\n")
    subprocess.Popen(["sleep", "300"])         # a child in the same process group
    time.sleep(300)
    open("after.md", "w").write("never\n")
    sys.exit(0)
brief = dict(kv.split("=", 1) for kv in args[args.index("-p") + 1].split())
seen = {}

def attempt(name, fn):
    try:
        fn()
        seen[name] = "allowed"
    except OSError:
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
os.mkdir("private")
open("private/notes.md", "w").write("private\n")
os.chmod("private", 0o700)                  # a directory only the agent can list
fd = os.open("secret.md", os.O_WRONLY | os.O_CREAT, 0o600)
os.write(fd, b"only mine\n")
os.close(fd)
os.chmod("secret.md", 0o600)                # a file only the agent can read
os.makedirs("vendored/.git")
open("vendored/.git/config", "w").write("[core]\n\tfsmonitor = /tmp/evil\n")
open("vendored/kept.md", "w").write("kept\n")
open(".steward-result.json", "w").write(json.dumps({"outcome": "fixed", "summary": json.dumps(seen)}))
print(json.dumps({"total_cost_usd": 0.0, "num_turns": 1}))
'''


needs_agent_user = pytest.mark.skipif(not AGENT_USER, reason="set STEWARD_TEST_AGENT_USER to a user the test may sudo to")


@pytest.fixture
def world(monkeypatch):
    """A run base the agent user can traverse, a fake venv holding the fake
    claude, and a committed clone."""
    base = Path(tempfile.mkdtemp(prefix="steward-iso-test-", dir="/tmp"))
    base.chmod(0o755)
    venv_bin = base / "venvs" / "0.27.0" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "slopstopper").write_text("#!/bin/sh\nexit 0\n")
    (venv_bin / "claude").write_text(FAKE_CLAUDE)
    for f in (venv_bin / "claude", venv_bin / "slopstopper"):
        f.chmod(0o755)
    clone = base / "clones" / "acme__a" / "triage-1"
    clone.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, check=True, env=env)
    (clone / "README.md").write_text("# a\n")
    (clone / "gone.txt").write_text("bye\n")
    os.symlink("/usr/share/doc", clone / "ext")          # the repo's own link out, which the export refuses
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                   cwd=clone, check=True, env=env)
    monkeypatch.setenv(isolation.AGENT_USER_ENV, AGENT_USER or "")
    monkeypatch.setenv("GH_TOKEN", "ghs_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # The real probe, on the agent's PATH: put the fake where a system install would be.
    monkeypatch.setattr(isolation, "SYSTEM_PATH", (str(venv_bin), *isolation.SYSTEM_PATH))
    try:
        yield venv_bin, clone, workspace.Workspace("acme/a", clone, "main")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@needs_agent_user
def test_the_agent_as_its_own_user(world):
    venv_bin, clone, ws = world
    git_config = (clone / ".git" / "config").read_bytes()
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
    assert (clone / "private" / "notes.md").read_text() == "private\n"            # a 0700 directory, opened up
    assert (clone / "secret.md").read_text() == "only mine\n"                     # a 0600 file, opened up
    assert not os.path.lexists(clone / "leak.md") and "leak.md" in result.notes  # the escape was refused, and said
    assert os.readlink(clone / "ext") == "/usr/share/doc" and "ext:" not in result.notes   # the repo's own, kept quietly
    assert (clone / ".git" / "config").read_bytes() == git_config
    assert (venv_bin / "slopstopper").read_text() == "#!/bin/sh\nexit 0\n"
    assert (clone / "vendored" / "kept.md").read_text() == "kept\n"
    assert not (clone / "vendored" / ".git").exists() and ".git" not in result.notes   # left out, quietly
    assert not list(Path("/tmp").glob("steward-agent-*"))                          # its copy is gone
    assert not list(clone.parent.glob(".steward-export-*"))                        # and so is the export


@needs_agent_user
def test_a_timed_out_agent_is_stopped_before_its_tree_is_copied(world):
    venv_bin, clone, ws = world
    result = agent.run(ws, "HANG=1", CFG, cli_bin=venv_bin / "slopstopper", timeout=3)
    assert result.ran and "timed out after 3 s" in result.error
    assert (clone / "before.md").read_text() == "written before the hang\n"     # what it did is kept
    assert not (clone / "after.md").exists()
    left = subprocess.run(["pgrep", "-u", AGENT_USER, "-f", "sleep 300"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "the agent's child outlived the timeout"


# ── sixth review: races, the transaction, chains through untouched links ──

def _snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*filenames, *dirnames]:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            out[rel] = ("link", os.readlink(p)) if p.is_symlink() else ("dir",) if p.is_dir() else ("file", p.read_bytes())
    return out


def test_a_file_swapped_for_a_symlink_mid_read_is_refused(tmp_path, monkeypatch):
    secret = tmp_path / "steward-secret"
    secret.write_text("GH_TOKEN=ghs_secret\n")
    clone = _tree(tmp_path / "clone", {"README.md": "old\n"})
    tree = _tree(tmp_path / "tree", {"README.md": "new\n"})
    before = _snapshot(clone)
    os.utime(tree / "README.md", ns=(0, 10**9))        # not the clone's mtime: the contents are compared
    real = isolation._regular_fd

    def swap_then_open(root, rel):          # runs between the walk's lstat and the read
        (tree / "README.md").unlink()
        os.symlink(str(secret), tree / "README.md")
        return real(root, rel)
    monkeypatch.setattr(isolation, "_regular_fd", swap_then_open)
    with pytest.raises(isolation.IsolationError):
        isolation.sync_back(tree, clone)
    assert _snapshot(clone) == before and "ghs_secret" not in (clone / "README.md").read_text()


def test_read_regular_refuses_a_symlink_and_a_fifo(tmp_path):
    (tmp_path / "real").write_text("x")
    os.symlink(str(tmp_path / "real"), tmp_path / "link")
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("inner")
    os.symlink("d", tmp_path / "dlink")
    assert isolation.read_regular(tmp_path, "real")[0] == b"x"
    assert isolation.read_regular(tmp_path, "d/f")[0] == b"inner"
    with pytest.raises(OSError):
        isolation.read_regular(tmp_path, "link")
    with pytest.raises(OSError):
        isolation.read_regular(tmp_path, "dlink/f")        # a link anywhere on the path, not just the last step
    with pytest.raises(isolation.IsolationError):
        isolation.read_regular(tmp_path, "fifo")


def test_an_untouched_link_that_now_leads_out_rolls_everything_back(tmp_path):
    clone = _tree(tmp_path / "work" / "clone", {"README.md": "x", "d/keep.md": "k"})
    os.symlink("d/..", clone / "x")          # inside today: d is a real directory
    tree = _tree(tmp_path / "work" / "tree", {"README.md": "changed", "d-gone": ""})
    (tree / "d-gone").unlink()
    os.symlink("d/..", tree / "x")           # untouched by the agent
    os.symlink(".", tree / "d")              # the agent's change: x now resolves to the clone's parent
    before = _snapshot(clone)
    with pytest.raises(isolation.IsolationError, match="`x`"):
        isolation.sync_back(tree, clone)
    assert _snapshot(clone) == before
    assert not list((tmp_path / "work").glob(".steward-undo-*"))


def test_a_rejected_link_puts_back_the_file_it_replaced(tmp_path):
    clone = _tree(tmp_path / "work" / "clone", {"README.md": "x", "docs/guide.md": "the guide\n"})
    tree = _tree(tmp_path / "work" / "tree", {"README.md": "x", "docs/.keep": ""})
    os.symlink(".", tree / "here")
    os.symlink("../here/..", tree / "docs" / "guide.md")   # lexically ".", really the clone's parent
    out = isolation.sync_back(tree, clone)
    assert (clone / "docs" / "guide.md").read_text() == "the guide\n" and not (clone / "docs" / "guide.md").is_symlink()
    assert any(r.startswith("docs/guide.md:") for r in out.rejected) and "docs/guide.md" not in out.changed
    assert "docs/guide.md" not in out.removed


def test_a_failed_write_rolls_the_clone_back(tmp_path, monkeypatch):
    clone = _tree(tmp_path / "work" / "clone", {"a.md": "a", "b.md": "b", "gone.md": "g", "dirlink/x": "x"})
    tree = _tree(tmp_path / "work" / "tree", {"a.md": "A", "b.md": "B", "new/c.md": "c"})
    os.symlink("a.md", tree / "alias.md")
    before = _snapshot(clone)
    real, calls = isolation._copy, []

    def full_disk(tree_, rel, dst):
        calls.append(rel)
        if len(calls) == 2:
            raise OSError(28, "No space left on device", str(dst))
        real(tree_, rel, dst)
    monkeypatch.setattr(isolation, "_copy", full_disk)
    with pytest.raises(isolation.IsolationError, match="rolled back"):
        isolation.sync_back(tree, clone)
    assert _snapshot(clone) == before
    assert not list((tmp_path / "work").glob(".steward-undo-*"))


def test_an_entry_vanishing_mid_walk_is_an_isolation_error(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "tree", {"README.md": "x", "tmp.txt": "t"})
    real = os.lstat
    monkeypatch.setattr(isolation.os, "lstat", lambda p, *a, **k: (_ for _ in ()).throw(
        FileNotFoundError(2, "No such file or directory", p)) if str(p).endswith("tmp.txt") else real(p, *a, **k))
    with pytest.raises(isolation.IsolationError, match="tmp.txt"):
        isolation._entries(tree)


def test_a_slow_sudo_is_an_isolation_error(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    monkeypatch.setattr(isolation.subprocess, "run", slow)
    with pytest.raises(isolation.IsolationError, match="did not finish"):
        isolation._sudo("steward-agent", "chmod", "-R", "x", timeout=5)


def test_only_a_real_timeout_counts_as_one():
    assert isolation.timed_out(124, 1.0, 3600)
    assert isolation.timed_out(137, 3631.0, 3600)          # TERM ignored, killed after --kill-after
    assert not isolation.timed_out(137, 300.0, 3600)       # the OOM killer five minutes in
    assert not isolation.timed_out(0, 3700.0, 3600)


def test_a_setup_failure_says_the_agent_never_started(tmp_path, monkeypatch):
    def refuse(user, *cmd, **k):
        raise isolation.IsolationError("`cp -RP` as steward-agent failed: Permission denied")
    monkeypatch.setattr(isolation, "_sudo", refuse)
    with pytest.raises(isolation.SetupError):
        with isolation.copy_for("steward-agent", tmp_path):
            pass
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    monkeypatch.setenv(isolation.AGENT_USER_ENV, "steward-agent")
    result = agent.run(workspace.Workspace("acme/a", clone, "main"), "brief", CFG, preflight=lambda: None)
    assert not result.ran and "could not be started" in result.error and "copied back" not in result.error


def test_the_probe_runs_once_per_run_and_only_when_needed(monkeypatch, tmp_path):
    from steward import runner
    calls = []
    monkeypatch.setattr(runner.agent, "available", lambda: calls.append(1) or None)
    ctx = runner.RunContext(base=tmp_path)
    assert ctx.agent_unavailable() is None and ctx.agent_unavailable() is None
    assert len(calls) == 1
    from steward.discover import Repo
    from steward.inventory import Inventory
    inv = Inventory(Repo("acme/a", pinned="0.26.0", installed_from="0.26.0", evidence=["mise-pin"]), "0.27.0")
    monkeypatch.setattr(runner, "gather", lambda cfg, only=None: ([inv], {"acme/a": "0.27.0"}, "0.27.0"))
    monkeypatch.setattr(runner, "execute", lambda plan, invs, cfg, workdir, **kw: [])
    calls.clear()
    runner.run(CFG, None, False, tmp_path)      # an upgrade only: nothing agentic
    assert calls == []


@needs_agent_user
def test_a_detached_agent_process_is_stopped_before_the_copy_back(world):
    venv_bin, clone, ws = world
    result = agent.run(ws, "DETACH=1", CFG, cli_bin=venv_bin / "slopstopper", timeout=60)
    assert result.ran and result.outcome == "fixed", result.error
    assert not isolation._alive(AGENT_USER), "a setsid child of the agent outlived the run"
    assert not list(Path("/tmp").glob("steward-agent-*"))


# ── seventh review ───────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["file", "symlink", "nested-dir"])
def test_a_git_entry_in_the_agents_tree_never_replaces_the_repository(tmp_path, kind):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    tree = _tree(tmp_path / "tree", {"README.md": "x", "sub/a.md": "a"})
    if kind == "file":
        (tree / ".git").write_text("gitdir: evil\n")
    elif kind == "symlink":
        os.symlink("sub", tree / ".git")
    else:
        _tree(tree, {"sub/.git/config": "[core]\n\tfsmonitor = /tmp/evil\n"})
    head = (clone / ".git" / "HEAD").read_bytes()
    out = isolation.sync_back(tree, clone)
    assert (clone / ".git").is_dir() and (clone / ".git" / "HEAD").read_bytes() == head
    assert not (clone / "sub" / ".git").exists()
    assert not any(c == ".git" or c.endswith("/.git") or "/.git/" in c for c in out.changed)
    assert out.rejected == []                            # left out, not reported: the export never carries one


def test_the_copys_own_repository_is_not_reported(tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    tree = _tree(tmp_path / "tree", {"README.md": "y", ".git/config": "theirs"})
    assert isolation.sync_back(tree, clone).rejected == []


@pytest.mark.parametrize("make", ["dir", "fifo", "symlink"])
def test_a_bad_result_file_is_no_result_not_a_failed_copy_back(tmp_path, make):
    tree, clone = tmp_path / "tree", tmp_path / "clone"
    tree.mkdir()
    clone.mkdir()
    (tree / "summary.json").write_text("{}")
    target = tree / agent.RESULT_FILE
    {"dir": lambda: target.mkdir(), "fifo": lambda: os.mkfifo(target),
     "symlink": lambda: os.symlink("summary.json", target)}[make]()
    rejected = []
    agent._take_result(tree, clone, rejected)          # must not raise
    assert not (clone / agent.RESULT_FILE).exists() and rejected and agent.RESULT_FILE in rejected[0]


def test_writes_loop_until_every_byte_is_down(monkeypatch, tmp_path):
    real = os.write
    monkeypatch.setattr(isolation.os, "write", lambda fd, data: real(fd, bytes(data[:3])))   # a short write every time
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT)
    isolation._write_all(fd, b"0123456789")
    os.close(fd)
    assert (tmp_path / "f").read_bytes() == b"0123456789"
    monkeypatch.setattr(isolation.os, "write", lambda fd, data: 0)
    fd = os.open(tmp_path / "g", os.O_WRONLY | os.O_CREAT)
    with pytest.raises(OSError):
        isolation._write_all(fd, b"x")                   # no progress is a full disk, never a silent truncation
    os.close(fd)


def test_an_in_place_run_kills_what_the_agent_left_in_its_group(tmp_path):
    marker = "31.4159"
    started = time.monotonic()
    done = agent._run_to_files(["sh", "-c", f"sleep {marker} & echo started"], tmp_path, dict(os.environ), 30,
                               own_group=True)
    assert done.returncode == 0 and "started" in done.stdout and time.monotonic() - started < 10
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "a background job outlived the in-place agent run"


def test_stop_all_clears_cron_and_at_before_and_after_killing(monkeypatch):
    calls = []
    monkeypatch.setattr(isolation, "_sudo",
                        lambda user, *cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(isolation, "_alive", lambda user: False)
    isolation.stop_all("steward-agent")
    kinds = ["cron" if c[0] == "crontab" else "at" if "atrm" in " ".join(c) else c[0] for c in calls]
    assert kinds == ["cron", "at", "kill", "cron", "at"]


def test_an_unreachable_workdir_skips_agentic_orders_before_cloning(monkeypatch, tmp_path):
    from steward import runner
    from steward.discover import Repo
    from steward.inventory import Finding, Inventory
    from steward.plan import Order, Plan
    monkeypatch.setenv(isolation.AGENT_USER_ENV, "steward-agent")
    monkeypatch.setattr(runner.isolation, "reaches", lambda user, path: False)
    monkeypatch.setattr(runner.workspace, "clone", lambda *a, **k: pytest.fail("cloned for an agent that cannot run"))
    order = Order("triage-1", "triage", "acme/a", "t", "r", 3, "main", "0.27.0", "0.27.0",
                  Finding(1, "x", "u", [], check="hygiene:docs-size"), True)
    outcomes = runner.execute(Plan(orders=[order]), [Inventory(Repo("acme/a"), "0.27.0")], CFG, tmp_path,
                              agent_check=None, agent_checked=True)
    assert outcomes[0].status == "skipped" and "cannot read and traverse the workdir" in outcomes[0].detail


def test_the_pinned_cli_must_run_as_the_agent_user(monkeypatch, tmp_path):
    def refuse(user, *cmd, **k):
        raise isolation.IsolationError("`/usr/bin/env PATH=...` as steward-agent failed: Permission denied")
    monkeypatch.setattr(isolation, "_sudo", refuse)
    with pytest.raises(isolation.SetupError, match="cannot run the pinned CLI"):
        isolation.check_cli("steward-agent", tmp_path / "venv" / "bin" / "slopstopper")


def test_differs_reads_only_same_sized_files(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "tree", {"a.md": "short", "b.md": "same!"})
    clone = _tree(tmp_path / "clone", {"a.md": "a longer one", "b.md": "diff!"})
    os.utime(tree / "b.md", ns=(0, 10**9))                 # a different mtime: only the contents can settle it
    opened = []
    real = isolation._regular_fd
    monkeypatch.setattr(isolation, "_regular_fd", lambda root, rel: opened.append(rel) or real(root, rel))
    assert isolation._differs(tree, "a.md", clone, os.lstat(tree / "a.md"))
    assert isolation._differs(tree, "b.md", clone, os.lstat(tree / "b.md"))
    assert opened == ["b.md"]                              # the size difference settled a.md unread


# ── eighth review: the export, aborts, lazy probes ───────────────────

def _tar(members) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                continue
            info.type = {"link": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE, "dev": tarfile.CHRTYPE}[kind]
            info.linkname = data or ""
            archive.addfile(info)
    return buf.getvalue()


def test_the_export_is_unpacked_as_untrusted_data(tmp_path):
    into = tmp_path / "work" / "staging"
    into.mkdir(parents=True)
    stream = io.BytesIO(_tar([
        ("ok.md", "file", b"ok\n"), ("rel.md", "link", "ok.md"),
        ("abs.md", "link", "/etc/passwd"), ("up.md", "link", "../../outside"),
        ("../escaped.md", "file", b"x"),
        ("pipe", "fifo", None), ("tty", "dev", None),
    ]))
    rejected = isolation._unpack(stream, into)
    assert sorted(p.name for p in into.iterdir()) == ["ok.md", "rel.md"]
    assert (into / "ok.md").read_text() == "ok\n" and os.readlink(into / "rel.md") == "ok.md"
    assert not (tmp_path / "work" / "escaped.md").exists()
    assert {r.rel for r in rejected} == {"abs.md", "up.md", "../escaped.md", "pipe", "tty"}
    assert {r.rel: r.link for r in rejected}["abs.md"] == "/etc/passwd"


@needs_agent_user
def test_a_tree_swapped_for_a_link_to_steward_files_is_never_read(world):
    venv_bin, clone, ws = world
    secret = clone.parent.parent / "steward-only"
    secret.mkdir()
    secret.chmod(0o700)
    (secret / "README.md").write_text("GH_TOKEN=ghs_secret\n")
    def worktree():     # the hooks are cleared and the prompt written on every run, by design
        return {k: v for k, v in _snapshot(clone).items() if k.split("/")[0] not in (".git", ".steward-prompt.md")}
    before = worktree()
    result = agent.run(ws, f"SWAPROOT={secret}", CFG, cli_bin=venv_bin / "slopstopper", timeout=60)
    assert result.ran and result.outcome == "skipped" and "not copied back" in result.error, result.error
    assert worktree() == before and "ghs_secret" not in json.dumps(result.to_dict())
    assert not list(clone.parent.glob(".steward-export-*"))


def test_an_interrupted_run_is_stopped_and_reaped(monkeypatch, tmp_path):
    marker = "27.1828"

    class Interrupted(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout == 300:
                raise KeyboardInterrupt
            return super().wait(timeout)
    monkeypatch.setattr(agent.subprocess, "Popen", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        agent._run_to_files(["sleep", marker], tmp_path, dict(os.environ), 300)
    monkeypatch.undo()
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", "an interrupted run left the agent running"


def test_a_stop_that_fails_does_not_hide_the_timeout(tmp_path):
    def broken():
        raise isolation.IsolationError("processes running as steward-agent would not stop")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        agent._run_to_files(["sleep", "2"], tmp_path, dict(os.environ), 0.2, on_abort=broken)
    assert time.monotonic() - started < 30                 # reaped, and not waited on forever


def _isolated_without_sudo(monkeypatch, tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    monkeypatch.setenv(isolation.AGENT_USER_ENV, "steward-agent")

    @contextmanager
    def fake_copy(user, clone_):
        yield isolation.Isolated(user, tmp_path, tmp_path, tmp_path, tmp_path)
    monkeypatch.setattr(isolation, "copy_for", fake_copy)
    return workspace.Workspace("acme/a", clone, "main")


def test_a_timeout_that_cannot_be_copied_back_says_both(monkeypatch, tmp_path):
    ws = _isolated_without_sudo(monkeypatch, tmp_path)

    def hang(cmd, cwd, env, timeout, **k):
        raise subprocess.TimeoutExpired(cmd, timeout)

    def stuck(iso, ws_, rejected):
        raise isolation.IsolationError("processes running as steward-agent would not stop")
    monkeypatch.setattr(agent, "_run_to_files", hang)
    monkeypatch.setattr(agent, "_bring_back", stuck)
    result = agent.run(ws, "brief", CFG, timeout=5, preflight=lambda: None)
    assert result.ran and "timed out after 5 s" in result.error and "would not stop" in result.error


def test_the_pinned_cli_is_checked_once_per_run(monkeypatch, tmp_path):
    ws = _isolated_without_sudo(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(isolation, "check_cli", lambda user, cli: calls.append(cli))
    monkeypatch.setattr(agent, "_run_to_files",
                        lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, "{}", ""))
    monkeypatch.setattr(agent, "_bring_back", lambda iso, ws_, rejected: None)
    checked = set()
    for _ in range(3):
        agent.run(ws, "brief", CFG, cli_bin=tmp_path / "venv" / "bin" / "slopstopper", preflight=lambda: None,
                  checked=checked)
    assert calls == [tmp_path / "venv" / "bin" / "slopstopper"]


def test_the_workdir_is_probed_only_when_an_order_needs_the_agent(monkeypatch, tmp_path):
    from steward import runner
    from steward.discover import Repo
    from steward.inventory import Inventory
    from steward.plan import Order, Plan
    monkeypatch.setenv(isolation.AGENT_USER_ENV, "steward-agent")
    monkeypatch.setattr(runner.isolation, "reaches", lambda user, path: pytest.fail("probed for no agentic order"))
    monkeypatch.setattr(runner.agent, "available", lambda: pytest.fail("probed for no agentic order"))
    monkeypatch.setattr(runner, "work_refresh", lambda *a: runner.Outcome(a[0].to_dict(), "skipped"))
    monkeypatch.setattr(runner.workspace, "clone", lambda *a, **k: None)
    monkeypatch.setattr(runner.workspace, "branch", lambda *a, **k: None)
    order = Order("upgrade-1", "upgrade", "acme/a", "t", "r", 1, "main", "0.26.0", "0.27.0", None, False)
    outcomes = runner.execute(Plan(orders=[order]), [Inventory(Repo("acme/a"), "0.27.0")], CFG, tmp_path)
    assert outcomes[0].status == "skipped"


# ── ninth review ─────────────────────────────────────────────────────

def _export_of(tree: Path, into: Path) -> list:
    """What `export` does, without sudo: tar the tree, unpack it as data."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as archive:
        archive.add(tree, arcname=".", filter=lambda m: None if m.name.split("/")[-1] == ".git" else m)
    buf.seek(0)
    return isolation._unpack(buf, into)


def test_links_the_export_refuses_are_kept_not_deleted(tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "x", "sub/a.md": "a"})
    os.symlink("/usr/share/foo", clone / "ext")
    os.symlink("../../outside", clone / "sub" / "up")
    tree = tmp_path / "tree"
    shutil.copytree(clone, tree, symlinks=True)
    (tree / "README.md").write_text("changed\n")
    os.symlink("/etc/shadow", tree / "new-out")            # a link the agent added that leads out
    staging = tmp_path / "staging"
    staging.mkdir()
    refused = _export_of(tree, staging)
    skip = [r.rel for r in refused]
    out = isolation.sync_back(staging, clone, skip=skip)
    assert out.removed == [] and out.changed == ["README.md"]
    assert os.readlink(clone / "ext") == "/usr/share/foo" and os.readlink(clone / "sub" / "up") == "../../outside"
    assert not os.path.lexists(clone / "new-out")
    notes = isolation.refusal_notes(refused, clone)
    assert [n.split(":")[0] for n in notes] == ["new-out"]   # the repo's own links are not the agent's doing


def test_a_directory_link_made_real_brings_its_files(tmp_path):
    clone = _tree(tmp_path / "clone", {"real/f": "same\n"})
    os.symlink("real", clone / "d")
    tree = _tree(tmp_path / "tree", {"real/f": "same\n", "d/f": "same\n"})
    st = os.stat(clone / "real" / "f")
    for f in (tree / "real" / "f", tree / "d" / "f"):
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))   # what cp -p leaves
    out = isolation.sync_back(tree, clone)
    assert "d/f" in out.changed and "d" in out.removed
    assert (clone / "d").is_dir() and not (clone / "d").is_symlink() and (clone / "d" / "f").read_text() == "same\n"


def test_a_link_under_a_clone_link_is_written_not_assumed(tmp_path):
    clone = _tree(tmp_path / "clone", {"real/x": "x"})
    os.symlink("x", clone / "real" / "l")
    os.symlink("real", clone / "d")
    tree = _tree(tmp_path / "tree", {"real/x": "x", "d/x": "x"})
    os.symlink("x", tree / "real" / "l")
    os.symlink("x", tree / "d" / "l")                       # same target as real/l, seen through the clone's d
    out = isolation.sync_back(tree, clone)
    assert "d/l" in out.changed and os.readlink(clone / "d" / "l") == "x" and not (clone / "d").is_symlink()


def test_a_slow_cleanup_does_not_undo_a_copy_back(monkeypatch, tmp_path):
    def fake(user, *cmd, check=True, timeout=600):
        if cmd[0] == "mktemp":
            return subprocess.CompletedProcess(cmd, 0, str(tmp_path / "root") + "\n", "")
        if cmd[0] == "rm":
            raise isolation.IsolationError(f"`rm -rf` as {user} did not finish within {timeout} s")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(isolation, "_sudo", fake)
    monkeypatch.setattr(isolation, "stop_all", lambda user: None)
    with isolation.copy_for("steward-agent", tmp_path) as iso:
        assert iso.tree == tmp_path / "root" / "tree"         # leaves without raising


def test_an_in_place_result_file_link_is_not_followed(monkeypatch, tmp_path):
    clone = _tree(tmp_path / "clone", {"README.md": "x"})
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    private = tmp_path / "steward-private.json"
    private.write_text(json.dumps({"outcome": "fixed", "summary": "steward-only text"}))
    monkeypatch.delenv(isolation.AGENT_USER_ENV, raising=False)

    def agent_links(cmd, cwd, env, timeout, **k):
        os.symlink(str(private), Path(cwd) / agent.RESULT_FILE)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")
    monkeypatch.setattr(agent, "_run_to_files", agent_links)
    result = agent.run(workspace.Workspace("acme/a", clone, "main"), "brief", CFG, preflight=lambda: None)
    assert "steward-only text" not in json.dumps(result.to_dict())
    assert agent.RESULT_FILE in result.notes
