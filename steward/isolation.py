"""Running the agent as its own OS user (`STEWARD_AGENT_USER`).

In-process boxing (the tool lists, the stripped environment, the git
hardening) leaves one gap: the agent runs as the same OS user as the
steward, so anything it manages to execute can write the steward's clone,
`.git` and venvs, and so fake the steward's own verification. Run as a
separate user, it cannot. It works in its own copy of the clone, owned by
that user; the steward's files are simply not writable by it, and the
steward's process environment (which holds the push token) is not
readable by it either.

After the run the steward copies back what changed: regular files, and
symlinks that, once in place, still resolve inside the repo and outside
`.git`. Nothing under `.git` is copied and nothing is followed. Anything
else (a symlink that leads out, even through a chain of links; a FIFO; a
device) is refused and reported. The copy-back is fail-closed: every
read happens before the clone is touched, so a file the steward cannot
read aborts the copy with the clone exactly as it was, never half-synced
and never with content mistaken for deleted.

Before anything is read back, every process running as the agent user
is stopped and confirmed gone, so nothing the agent left behind (a
detached child, a `setsid` loop) can change its tree while the steward
reads it. Files are read through a descriptor opened without following
symlinks and checked to be regular, and the copy into the clone is a
transaction: an error part-way rolls every change back.

The steward reaches the user through `sudo -n -u <user>` with
`--preserve-env`, so the account running the steward needs passwordless
sudo to that user with environment passing allowed (a GitHub runner has
it). Because the run stops every process of that user, the user must be
dedicated to one steward run at a time. The base directory the run works
in must be traversable by the agent user, so the venvs and the clone it
copies can be read.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
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
    root: Path   # owned by the agent user, 0755: the steward reads the tree back from here
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
            _sudo(user, "chmod", "755", str(root), str(iso.tree))
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
    process) would keep changing the tree while the steward reads it. The
    agent user can signal all of its own processes, and nothing else."""
    for _ in range(20):
        _sudo(user, "kill", "-KILL", "-1", check=False, timeout=30)
        if not _alive(user):
            return
        time.sleep(0.1)
    raise IsolationError(f"processes running as {user} would not stop")


def open_up(iso: Isolated) -> None:
    """Make the agent's tree readable by the steward before copying it back.
    The agent owns every entry in it, so as that user it can grant read on
    all of them, whatever umask or tool created them 0700 or 0600."""
    _sudo(iso.user, "chmod", "-R", "u+rwX,go+rX", str(iso.tree), check=False, timeout=1800)


def read_regular(path: Path) -> tuple[bytes, int]:
    """A regular file's bytes and mode, read through a descriptor opened
    without following a symlink and checked after opening, so a file
    swapped for a link (to another process's environment, say) between
    being listed and being read is refused, never read as its target."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IsolationError(f"`{path.name}` is no longer a regular file")
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks), st.st_mode
    finally:
        os.close(fd)


# ── copying the agent's work back ─────────────────────────────────────

@dataclass
class Synced:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # "path: why"


def _entries(root: Path) -> dict[str, os.stat_result]:
    """Every non-directory entry under `root` except `.git`, by relative path,
    without following symlinks (a symlinked directory is one entry). Any
    directory that cannot be read raises: skipping it would make its
    contents look deleted."""
    def fail(exc: OSError) -> None:
        raise IsolationError(f"cannot read `{exc.filename}` in the agent's tree: {exc.strerror}")

    out: dict[str, os.stat_result] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=fail):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in [*filenames, *(d for d in dirnames if os.path.islink(os.path.join(dirpath, d)))]:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            try:
                out[rel] = os.lstat(os.path.join(dirpath, name))
            except OSError as exc:
                raise IsolationError(f"`{rel}` changed while being read: {exc.strerror}") from exc
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


def _unchanged(dst: Path, st: os.stat_result) -> bool:
    """Cheap first: the copy kept timestamps, so equal size, mtime and
    executable bit mean the agent left the file alone."""
    try:
        ds = os.lstat(dst)
    except FileNotFoundError:
        return False
    return (stat.S_ISREG(ds.st_mode) and ds.st_size == st.st_size and ds.st_mtime_ns == st.st_mtime_ns
            and (ds.st_mode & stat.S_IXUSR) == (st.st_mode & stat.S_IXUSR))


@dataclass
class _Plan:
    files: dict[str, tuple[bytes, bool]] = field(default_factory=dict)  # rel -> (content, executable)
    links: dict[str, str] = field(default_factory=dict)                 # rel -> target
    removals: list[str] = field(default_factory=list)


def _new_content(src: Path, dst: Path, st: os.stat_result) -> tuple[bytes, bool] | None:
    """The file's bytes and executable bit when the copy-back must write
    it, else None. Mode and bytes come from the same descriptor."""
    if _unchanged(dst, st):
        return None
    content, mode = read_regular(src)
    executable = bool(mode & stat.S_IXUSR)
    same = (dst.is_file() and not dst.is_symlink() and dst.read_bytes() == content
            and bool(os.lstat(dst).st_mode & stat.S_IXUSR) == executable)
    return None if same else (content, executable)


def _plan_entry(plan: _Plan, rejected: list[str], rel: str, st: os.stat_result, tree: Path, clone: Path) -> None:
    src, dst = tree / rel, clone / rel
    if stat.S_ISLNK(st.st_mode):
        target = os.readlink(src)
        if _lexically_escapes(rel, target):
            rejected.append(f"{rel}: a symlink to `{target}`, outside the repository")
        elif not (dst.is_symlink() and os.readlink(dst) == target):
            plan.links[rel] = target
    elif stat.S_ISREG(st.st_mode):
        new = _new_content(src, dst, st)
        if new is not None:
            plan.files[rel] = new
    else:
        rejected.append(f"{rel}: not a regular file or symlink")


def _plan(tree: Path, clone: Path, owned) -> tuple[_Plan, list[str]]:
    """Everything the copy-back will do, with every byte read already, and
    nothing in the clone touched yet."""
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
    return plan, rejected


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


def _write(path: Path, content: bytes, executable: bool) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, content)
        os.fchmod(fd, 0o755 if executable else 0o644)
    finally:
        os.close(fd)


def _apply(plan: _Plan, clone: Path, undo: _Undo) -> None:
    # Removals first: an entry the agent replaced (a directory symlink it
    # turned into a real directory) must go before its new contents land.
    for rel in plan.removals:
        undo.stash(clone / rel)
    for rel, target in plan.links.items():
        _real_parents(clone, rel, undo)
        undo.stash(clone / rel)
        undo.created(clone / rel)
        os.symlink(target, clone / rel)
    for rel, (content, executable) in plan.files.items():
        _real_parents(clone, rel, undo)
        undo.stash(clone / rel)
        undo.created(clone / rel)
        _write(clone / rel, content, executable)


def _escaping_links(clone: Path) -> set[str]:
    """Every symlink in the clone that, resolved, leads outside it or into `.git`."""
    return {rel for rel, st in _entries(clone).items()
            if stat.S_ISLNK(st.st_mode) and not _inside(clone, clone / rel)}


def sync_back(tree: Path, clone: Path, skip: Iterable[str] = ()) -> Synced:
    """Make the clone's working tree match the agent's, outside `.git`.

    `skip` are clone-relative paths the steward owns locally (its prompt,
    the result file); they are neither copied nor removed. Fail-closed: a
    read that fails raises IsolationError before the clone is touched, and
    any failure while writing rolls the clone back to exactly what it was."""
    skipped = tuple(s.rstrip("/") for s in skip)

    def owned(rel: str) -> bool:
        return any(rel == s or rel.startswith(s + "/") for s in skipped)

    plan, rejected = _plan(tree, clone, owned)
    out = Synced(rejected=rejected)
    already_out = _escaping_links(clone)     # repo links that pointed out before the agent ran are the repo's
    undo = _Undo(clone)
    try:
        _apply(plan, clone, undo)
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
        newly_out = _escaping_links(clone) - already_out
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
