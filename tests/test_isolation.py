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
    monkeypatch.setattr(isolation, "read_regular", lambda path: pytest.fail(f"read {path.name}"))
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
    assert (clone / ".git" / "config").read_bytes() == git_config
    assert (venv_bin / "slopstopper").read_text() == "#!/bin/sh\nexit 0\n"
    assert not list(Path("/tmp").glob("steward-agent-*/tree/README.md"))           # its copy is gone


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
    real = isolation._unchanged

    def swap_then_check(dst, st):           # runs between the walk's lstat and the read
        (tree / "README.md").unlink()
        os.symlink(str(secret), tree / "README.md")
        return real(dst, st)
    monkeypatch.setattr(isolation, "_unchanged", swap_then_check)
    with pytest.raises(isolation.IsolationError):
        isolation.sync_back(tree, clone)
    assert _snapshot(clone) == before and "ghs_secret" not in (clone / "README.md").read_text()


def test_read_regular_refuses_a_symlink_and_a_fifo(tmp_path):
    (tmp_path / "real").write_text("x")
    os.symlink(str(tmp_path / "real"), tmp_path / "link")
    os.mkfifo(tmp_path / "fifo")
    assert isolation.read_regular(tmp_path / "real")[0] == b"x"
    with pytest.raises(OSError):
        isolation.read_regular(tmp_path / "link")
    with pytest.raises(isolation.IsolationError):
        isolation.read_regular(tmp_path / "fifo")


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
    real, calls = isolation._write, []

    def full_disk(path, content, executable):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device", str(path))
        real(path, content, executable)
    monkeypatch.setattr(isolation, "_write", full_disk)
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
    result = agent.run(workspace.Workspace("acme/a", clone, "main"), "brief", CFG, why_not=None)
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
    monkeypatch.setattr(runner, "execute", lambda plan, invs, cfg, workdir, agent_check=None: [])
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
