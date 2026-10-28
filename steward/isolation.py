"""Running the agent as its own OS user (`STEWARD_AGENT_USER`).

In-process boxing (the tool lists, the stripped environment, the git
hardening) leaves one gap: the agent runs as the same OS user as the
steward, so anything it manages to execute can write the steward's clone,
`.git` and venvs, and so fake the steward's own verification. Run as a
separate user, it cannot. It works in its own copy of the clone, owned by
that user; the steward's files are simply not writable by it, and the
steward's process environment (which holds the push token) is not
readable by it either.

After the run, every process running as the agent user is stopped and
confirmed gone, so nothing the agent left behind (a detached child, a
`setsid` loop) runs on. The steward then never reads the agent's tree
itself: `tar`, running as the agent user, exports it, so the export can
reach only what that user could read anyway, and a tree (or anything in
it) swapped for a link to a steward file reads as a permission error,
not as the steward's file. The stream is unpacked as untrusted data
(Python's `data` extraction filter) into a directory only the steward
can write, and nothing of the agent's can change it from there.

From that copy the steward copies back what changed: regular files, and
symlinks that, once in place, still resolve inside the repo and outside
`.git`. Nothing named `.git` is exported or copied, and nothing is
followed. Anything else (a symlink that leads out, even through a chain
of links; a FIFO; a device) is refused and reported. The copy-back is
fail-closed: a failed export, or a read that fails while planning,
stops it with the clone exactly as it was, never half-synced and never
with content mistaken for deleted, and the copy into the clone is a
transaction: an error part-way rolls every change back.

The steward reaches the user through `sudo -n -u <user>` with
`--preserve-env`, so the account running the steward needs passwordless
sudo to that user with environment passing allowed (a GitHub runner has
it). Because the run stops every process of that user, the user must be
dedicated to one steward run at a time. The base directory the run works
in must be traversable by the agent user, so the venvs and the clone it
copies can be read. The agent user should also be denied cron and `at`
(the run workflow lists it in `/etc/cron.deny` and `/etc/at.deny`); the
steward clears its crontab and `at` queue anyway, before and after it
stops the user's processes.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

AGENT_USER_ENV = "STEWARD_AGENT_USER"
# The agent's PATH: the pinned CLI's venv first, then system locations only
# (never the steward's own home, mise shims or tool caches).
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
# Set per agent run rather than inherited: these name the steward's own
# user and directories, which the agent user cannot use.
PER_USER = ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "RUNNER_TEMP")
# `timeout` runs as the agent user and kills the agent's whole process
# group when time is up: a signal to `sudo` would not reach a process the
# steward does not own.
KILL_AFTER = 30
# The export's tar timestamps come back as floats: an mtime within this of
# the clone's is the same mtime.
MTIME_SLACK_NS = 1000


def timed_out(returncode: int, elapsed: float, limit: int) -> bool:
    """124 is `timeout` firing. 137 is a SIGKILL, which is the timeout only
    when it came after the limit (`--kill-after`); earlier it is something
    else, the OOM killer for one, and must not be reported as a timeout."""
    return returncode == 124 or (returncode == 137 and elapsed >= limit)


class IsolationError(RuntimeError):
    """The agent's work could not be copied back."""


class SetupError(IsolationError):
    """The agent's own copy could not be made: the agent never started."""


def agent_user() -> str | None:
    return os.environ.get(AGENT_USER_ENV) or None


def _sudo(user: str, *cmd: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["sudo", "-n", "-u", user, "--", *cmd], capture_output=True, text=True,
                              check=check, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise IsolationError(f"`{' '.join(cmd[:2])}` as {user} failed: {(exc.stderr or '').strip()[-300:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise IsolationError(f"`{' '.join(cmd[:2])}` as {user} did not finish within {timeout} s") from exc


@dataclass
class Isolated:
    user: str
    root: Path   # owned by the agent user, 0700: only that user reads the tree
    tree: Path   # the agent's own copy of the clone; its working directory
    home: Path   # the agent's HOME, 0700
    tmp: Path    # the agent's TMPDIR, 0700


def command(iso: Isolated, argv: Sequence[str], env: Mapping[str, str], path_first: Path | None,
            timeout: int | None = None) -> list[str]:
    """`argv` as the agent user, in its tree, with `env`. Secrets travel in
    the environment (`--preserve-env`), never on the command line where
    another user could read them in the process list. PATH, HOME, TMPDIR
    and the user's names are not secret and are set explicitly for the
    agent user, since the steward's own values point at its directories
    (and sudo's secure_path would replace PATH anyway). With `timeout`, the
    run is wrapped in `timeout`, which the agent user owns and so can kill."""
    path = os.pathsep.join([*([str(path_first)] if path_first else []), *SYSTEM_PATH])
    keep = sorted(k for k in env if k not in PER_USER)
    preserve = [f"--preserve-env={','.join(keep)}"] if keep else []
    limit = ["/usr/bin/timeout", f"--kill-after={KILL_AFTER}", str(timeout)] if timeout else []
    return ["sudo", "-n", *preserve, "-u", iso.user, "--",
            "/usr/bin/env", "-C", str(iso.tree), f"PATH={path}", f"HOME={iso.home}", f"TMPDIR={iso.tmp}",
            f"USER={iso.user}", f"LOGNAME={iso.user}", *limit, *argv]


def unavailable(user: str, program: str = "claude") -> str | None:
    """Why the agent cannot run as `user`, or None when it can. The probe is
    the real command's shape (the same sudo flags, `--preserve-env`, PATH,
    `env -C` and `timeout`) running `<program> --version`, so a sudoers rule
    that would refuse the run, or a `program` only the steward's own PATH
    can find, is caught here rather than on every order."""
    if shutil.which("sudo") is None:
        return f"{AGENT_USER_ENV}={user} is set but sudo is not installed"
    probe = Isolated(user, Path("/tmp"), Path("/tmp"), Path("/tmp"), Path("/tmp"))
    env = {"STEWARD_PROBE": "1"}
    try:
        done = subprocess.run(command(probe, [program, "--version"], env, None, timeout=60),
                              capture_output=True, text=True, env={**os.environ, **env}, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{AGENT_USER_ENV}={user} is set but the agent probe failed: {exc}"
    if done.returncode != 0:
        why = (done.stderr or done.stdout).strip()[-300:]
        return (f"{AGENT_USER_ENV}={user} is set but `{program}` cannot run as {user} the way the agent would "
                f"(passwordless sudo with --preserve-env, `{program}` on {':'.join(SYSTEM_PATH)}): {why}")
    return None


def reaches(user: str, path: Path) -> bool:
    """Whether `user` can traverse and read `path` (a `--workdir`, say)."""
    try:
        done = _sudo(user, "test", "-r", str(path), "-a", "-x", str(path), check=False, timeout=30)
    except IsolationError:
        return False
    return done.returncode == 0


def check_cli(user: str, cli_bin: Path) -> None:
    """Prove `user` can run the pinned CLI the agent will be handed, the
    venv's interpreter included, before any copy is made: a venv under a
    home only the steward can enter would otherwise fail inside the agent."""
    path = os.pathsep.join([str(cli_bin.parent), *SYSTEM_PATH])
    try:
        _sudo(user, "/usr/bin/env", f"PATH={path}", str(cli_bin), "--version", timeout=120)
    except IsolationError as exc:
        raise SetupError(f"{user} cannot run the pinned CLI at {cli_bin}: {exc}") from exc


@contextmanager
def copy_for(user: str, clone: Path) -> Iterator[Isolated]:
    """The agent's own copy of `clone`, made and removed as the agent user.
    A failure before the agent starts raises SetupError."""
    try:
        root = Path(_sudo(user, "mktemp", "-d", "/tmp/steward-agent-XXXXXXXX").stdout.strip())
    except IsolationError as exc:
        raise SetupError(str(exc)) from exc
    try:
        iso = Isolated(user, root, root / "tree", root / "home", root / "tmp")
        try:
            _sudo(user, "mkdir", "-p", str(iso.tree), str(iso.home), str(iso.tmp))
            _sudo(user, "chmod", "700", str(root))
            _sudo(user, "chmod", "700", str(iso.home), str(iso.tmp))
            _sudo(user, "cp", "-RP", "--preserve=mode,timestamps", f"{clone}/.", str(iso.tree), timeout=1800)
        except IsolationError as exc:
            raise SetupError(str(exc)) from exc
        yield iso
    finally:
        try:
            stop_all(user)
        except IsolationError:
            pass  # removal below is best effort; the run already reported what mattered
        _sudo(user, "rm", "-rf", str(root), check=False)


def _alive(user: str) -> bool:
    """Whether any process of `user` is still running (zombies are not)."""
    try:
        done = subprocess.run(["ps", "-o", "stat=", "-u", user], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return any(line.strip() and not line.strip().startswith("Z") for line in done.stdout.splitlines())


def stop_all(user: str) -> None:
    """Stop every process running as `user`, and confirm none is left.

    The agent's `timeout` only covers its own process group, and only when
    time runs out; a child the agent detached (`setsid`, a daemonised
    process) would keep running, into the next order. The agent user can
    signal all of its own processes, and nothing else. Its crontab and `at`
    queue are cleared before the kill, so neither starts it again, and once
    more after, for an entry a process added before it died."""
    for _ in range(20):
        _clear_schedules(user)
        _sudo(user, "kill", "-KILL", "-1", check=False, timeout=30)
        if not _alive(user):
            _clear_schedules(user)
            if not _alive(user):
                return
        time.sleep(0.1)
    raise IsolationError(f"processes running as {user} would not stop")


def _clear_schedules(user: str) -> None:
    _sudo(user, "crontab", "-r", check=False, timeout=30)
    _sudo(user, "/bin/sh", "-c", "command -v atq >/dev/null 2>&1 && atq | cut -f1 | xargs -r atrm",
          check=False, timeout=30)


def _stop_quietly(user: str) -> None:
    try:
        stop_all(user)
    except IsolationError:
        pass  # the export then fails on its own, and says so


def open_up(iso: Isolated) -> None:
    """Make every entry of the agent's tree readable by its owner before the
    export: the agent owns all of them, so as that user it can, whatever
    umask or tool created them 0000. Best effort: an entry still unreadable
    fails the export."""
    _sudo(iso.user, "chmod", "-R", "u+rwX", str(iso.tree), check=False, timeout=1800)


def _unpack(stream, into: Path) -> list[str]:
    """Unpack a tar stream into `into` as untrusted data, and return what was
    left out and why. Python's `data` filter refuses absolute paths,
    anything that would land outside `into` (through `..` or a link), links
    that lead out, devices and FIFOs, and drops owners and setuid bits; a
    member it refuses is left out, not fatal."""
    rejected: list[str] = []

    def keep(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
        try:
            return tarfile.data_filter(member, dest)
        except tarfile.FilterError as exc:
            rejected.append(f"{member.name}: {exc}")
            return None

    # Every member passes `keep`, tarfile's `data` filter (no absolute paths, nothing outside
    # `into`, no special files); the rule does not see `filter=`.
    with tarfile.open(fileobj=stream, mode="r|") as archive:  # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
        archive.extractall(into, filter=keep)
    return rejected


def export(iso: Isolated, into: Path, timeout: int = 1800) -> list[str]:
    """Copy the agent's tree into `into`, a fresh directory only the steward
    can write, without the steward reading the tree: `tar` runs as the agent
    user, `.git` entries at any depth left out, hard links as plain files.
    Returns what the unpacking refused. A tar that fails, or a stream that
    does not parse, raises IsolationError: half an export would read as
    deletions."""
    cmd = ["sudo", "-n", "-u", iso.user, "--", "tar", "-C", str(iso.tree), f"--exclude={GIT}",
           "--hard-dereference", "--format=posix", "-cf", "-", "."]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        # `sudo` is root's: stopping the agent user's processes is what ends a stuck tar.
        watchdog = threading.Timer(timeout, _stop_quietly, (iso.user,))
        watchdog.start()
        try:
            rejected = _unpack(proc.stdout, into)
        except (tarfile.TarError, OSError) as exc:
            raise IsolationError(f"the agent's tree could not be unpacked: {exc}") from exc
        finally:
            watchdog.cancel()
            proc.stdout.close()   # a tar still writing gets EPIPE and exits
            returncode = proc.wait()
        if returncode != 0:
            err.seek(0)
            why = err.read().decode("utf-8", "replace").strip()[-300:]
            raise IsolationError(f"exporting the agent's tree as {iso.user} failed (exit {returncode}): {why}")
    return rejected


_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_in(root: Path, rel: str, flags: int = _READ) -> int:
    """Open `rel` under `root` one component at a time, refusing a symlink at
    every step: O_NOFOLLOW alone covers only the last component, and a
    directory on the way swapped for a link would otherwise be followed."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = Path(rel).parts
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], flags, dir_fd=fd)
    finally:
        os.close(fd)


def _regular_fd(root: Path, rel: str) -> int:
    fd = _open_in(root, rel)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IsolationError(f"`{rel}` is not a regular file")
    return fd


def read_regular(root: Path, rel: str) -> tuple[bytes, int]:
    """A regular file's bytes and mode, read without following a symlink at
    any step of the path, checked after opening, so a file or directory
    swapped for a link between being listed and being read is refused."""
    fd = _regular_fd(root, rel)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks), os.fstat(fd).st_mode
    finally:
        os.close(fd)


# ── copying the agent's work back ─────────────────────────────────────

@dataclass
class Synced:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # "path: why"


GIT = ".git"


def _lstat(dirpath: str, name: str, rel: str) -> os.stat_result:
    try:
        return os.lstat(os.path.join(dirpath, name))
    except OSError as exc:
        raise IsolationError(f"`{rel}` changed while being read: {exc.strerror}") from exc


def _entries(root: Path) -> dict[str, os.stat_result]:
    """Every non-directory entry under `root`, by relative path, without
    following symlinks (a symlinked directory is one entry). Anything named
    `.git`, at any depth and of any kind, is left out: in the clone it is
    the steward's, and from the agent's tree it would replace the clone's
    repository (the export leaves them out already). Any directory that
    cannot be read raises: skipping it would make its contents look
    deleted."""
    def fail(exc: OSError) -> None:
        raise IsolationError(f"cannot read `{exc.filename}` in the agent's tree: {exc.strerror}")

    out: dict[str, os.stat_result] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if d != GIT]
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        for name in [*(f for f in filenames if f != GIT), *links]:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            out[rel] = _lstat(dirpath, name, rel)
    return out


def _lexically_escapes(rel: str, target: str) -> bool:
    if os.path.isabs(target):
        return True
    resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return resolved == ".." or resolved.startswith("../") or resolved.split(os.sep)[0] == ".git"


def _inside(clone: Path, path: Path) -> bool:
    """Whether `path`, every link on the way resolved, stays in the clone and out of `.git`."""
    real, root = Path(os.path.realpath(path)), Path(os.path.realpath(clone))
    return real.is_relative_to(root) and not real.is_relative_to(root / ".git")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


@dataclass
class _Plan:
    files: list[str] = field(default_factory=list)                      # rels to copy from the agent's tree
    links: dict[str, str] = field(default_factory=dict)                 # rel -> target
    removals: list[str] = field(default_factory=list)


def _differs(tree: Path, rel: str, dst: Path, st: os.stat_result) -> bool:
    """Whether the copy-back must write `rel`. Cheap checks first, on one
    lstat of the clone's file: another kind, size or executable bit settles
    it; the same mtime means the agent left it alone (the agent's copy and
    the export both keep timestamps). Only then are the contents compared,
    a chunk at a time, through no-follow descriptors."""
    try:
        ds = os.lstat(dst)
    except FileNotFoundError:
        return True
    if (not stat.S_ISREG(ds.st_mode) or ds.st_size != st.st_size
            or (ds.st_mode & stat.S_IXUSR) != (st.st_mode & stat.S_IXUSR)):
        return True
    if abs(ds.st_mtime_ns - st.st_mtime_ns) < MTIME_SLACK_NS:
        return False
    src = _regular_fd(tree, rel)
    try:
        with open(dst, "rb") as theirs_copy:
            while True:
                a, b = os.read(src, 1 << 20), theirs_copy.read(1 << 20)
                if a != b:
                    return True
                if not a:
                    return False
    finally:
        os.close(src)


def _plan_entry(plan: _Plan, rejected: list[str], rel: str, st: os.stat_result, tree: Path, clone: Path) -> None:
    src, dst = tree / rel, clone / rel
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(src)
        if _lexically_escapes(rel, target):
            rejected.append(f"{rel}: a symlink to `{target}`, outside the repository")
        elif not (dst.is_symlink() and os.readlink(dst) == target):
            plan.links[rel] = target
    elif stat.S_ISREG(st.st_mode):
        if _differs(tree, rel, dst, st):
            plan.files.append(rel)
    else:
        rejected.append(f"{rel}: not a regular file or symlink")


def _plan(tree: Path, clone: Path, owned) -> tuple[_Plan, list[str], dict[str, os.stat_result]]:
    """What the copy-back will do, and the clone's entries as they are; the
    clone is not touched. Every comparison has already read what it needed."""
    plan, rejected = _Plan(), []
    theirs, ours = _entries(tree), _entries(clone)
    plan.removals = [rel for rel in sorted(set(ours) - set(theirs)) if not owned(rel)]
    for rel, st in sorted(theirs.items()):
        if owned(rel):
            continue
        try:
            _plan_entry(plan, rejected, rel, st, tree, clone)
        except OSError as exc:
            raise IsolationError(f"cannot read `{rel}` in the agent's tree: {exc.strerror}") from exc
    return plan, rejected, ours


class _Undo:
    """Every change the copy-back makes to the clone, so it can be taken
    back: a path moved aside (renamed into a sibling directory on the same
    filesystem, so nothing is copied), a path created, a directory made."""

    def __init__(self, clone: Path):
        self.dir = Path(tempfile.mkdtemp(prefix=".steward-undo-", dir=clone.parent))
        self.log: list[tuple[str, Path, Path | None]] = []
        self.original: dict[Path, Path] = {}

    def stash(self, path: Path) -> None:
        if not os.path.lexists(path):
            return
        dest = self.dir / str(len(self.log))
        os.rename(path, dest)
        self.original.setdefault(path, dest)
        self.log.append(("stash", path, dest))

    def created(self, path: Path) -> None:
        self.log.append(("create", path, None))

    def made_dir(self, path: Path) -> None:
        self.log.append(("mkdir", path, None))

    def restore(self, path: Path) -> None:
        """Put back whatever was at `path` before the copy-back, and forget
        that the path was touched."""
        if os.path.lexists(path):
            _remove(path)
        dest = self.original.pop(path, None)
        if dest is not None:
            os.rename(dest, path)
        self.log = [entry for entry in self.log if entry[1] != path]

    def rollback(self) -> None:
        for kind, path, dest in reversed(self.log):
            try:
                if kind == "create" and os.path.lexists(path):
                    _remove(path)
                elif kind == "mkdir":
                    path.rmdir()
                elif kind == "stash" and dest is not None:
                    if os.path.lexists(path):
                        _remove(path)
                    os.rename(dest, path)
            except OSError:
                continue  # best effort: keep putting back what can be put back
        self.commit()

    def commit(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _real_parents(clone: Path, rel: str, undo: _Undo) -> None:
    """Every directory above `rel` in the clone is a real directory, not a
    symlink the write would go through."""
    current = clone
    for part in Path(rel).parent.parts:
        current = current / part
        if current.is_symlink() or (os.path.lexists(current) and not current.is_dir()):
            undo.stash(current)
        if not os.path.lexists(current):
            current.mkdir()
            undo.made_dir(current)


def _write_all(fd: int, data: bytes) -> None:
    """os.write may write less than asked (a filling disk, or more than about
    2 GiB at once); loop until all of it is down, and treat no progress as
    a full disk rather than a silently truncated file."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        view = view[written:]


def _copy(tree: Path, rel: str, dst: Path) -> None:
    src = _regular_fd(tree, rel)
    try:
        executable = bool(os.fstat(src).st_mode & stat.S_IXUSR)
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while chunk := os.read(src, 1 << 20):
                _write_all(out, chunk)
            os.fchmod(out, 0o755 if executable else 0o644)
        finally:
            os.close(out)
    finally:
        os.close(src)


def _apply(plan: _Plan, tree: Path, clone: Path, undo: _Undo) -> None:
    # Removals first: an entry the agent replaced (a directory symlink it
    # turned into a real directory) must go before its new contents land.
    for rel in plan.removals:
        undo.stash(clone / rel)
    for rel, target in plan.links.items():
        _real_parents(clone, rel, undo)
        undo.stash(clone / rel)
        undo.created(clone / rel)
        os.symlink(target, clone / rel)
    for rel in plan.files:
        _real_parents(clone, rel, undo)
        undo.stash(clone / rel)
        undo.created(clone / rel)
        _copy(tree, rel, clone / rel)


def _escaping(clone: Path, rels: Iterable[str]) -> set[str]:
    """Those of `rels` (symlinks in the clone) that, resolved, lead outside it or into `.git`."""
    return {rel for rel in rels if not _inside(clone, clone / rel)}


def sync_back(tree: Path, clone: Path, skip: Iterable[str] = ()) -> Synced:
    """Make the clone's working tree match the agent's, outside `.git`.

    `skip` are clone-relative paths the steward owns locally (its prompt,
    the result file); they are neither copied nor removed. `tree` is the
    export, which only the steward can write. Fail-closed: a read that
    fails while planning raises IsolationError before the clone is
    touched, and any failure after that rolls the clone back exactly."""
    skipped = tuple(s.rstrip("/") for s in skip)

    def owned(rel: str) -> bool:
        return any(rel == s or rel.startswith(s + "/") for s in skipped)

    plan, rejected, ours = _plan(tree, clone, owned)
    out = Synced(rejected=rejected)
    links_before = {rel for rel, st in ours.items() if stat.S_ISLNK(st.st_mode)}
    already_out = _escaping(clone, links_before)   # repo links that pointed out before the agent ran are the repo's
    undo = _Undo(clone)
    try:
        _apply(plan, tree, clone, undo)
        # A link can look inside on its own and still lead out through
        # others: resolve each written one with everything in place, and
        # put back what was there before any that ends outside.
        for rel in sorted(plan.links):
            if not _inside(clone, clone / rel):
                undo.restore(clone / rel)
                del plan.links[rel]
                out.rejected.append(f"{rel}: a symlink that resolves outside the repository or into `.git`")
        # And a link the agent did not touch can now lead out because one it
        # did touch changed on the way: that undoes the whole copy-back.
        # The links now are the ones before, less what was removed or
        # overwritten by a file, plus the ones written: no second walk.
        links_now = (links_before - set(plan.removals) - set(plan.files)) | set(plan.links)
        newly_out = _escaping(clone, links_now) - already_out
        if newly_out:
            raise IsolationError("the agent's changes would make " + ", ".join(f"`{r}`" for r in sorted(newly_out))
                                 + " resolve outside the repository or into `.git`")
    except BaseException as exc:
        undo.rollback()
        if isinstance(exc, OSError):
            raise IsolationError(f"writing the agent's work into the clone failed and was rolled back: {exc}") from exc
        raise
    undo.commit()
    out.removed = list(plan.removals)
    out.changed = sorted([*plan.links, *plan.files])
    return out
