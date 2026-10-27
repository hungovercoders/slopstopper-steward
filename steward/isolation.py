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
symlinks whose target stays inside the repo. Nothing under `.git` is ever
copied, nothing is followed, and anything else (a symlink pointing out of
the tree, which the steward's own check runs would follow; a FIFO; a
device) is refused and reported.

The steward reaches the user through `sudo -n -u <user>`, so the account
running the steward needs passwordless sudo to that user (a GitHub runner
has it). The base directory the run works in must be traversable by the
agent user, so the venvs and the clone it copies can be read.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

AGENT_USER_ENV = "STEWARD_AGENT_USER"
# The agent's PATH: the pinned CLI's venv first, then system locations only
# (never the steward's own home, mise shims or tool caches).
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def agent_user() -> str | None:
    return os.environ.get(AGENT_USER_ENV) or None


def _sudo(user: str, *cmd: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", "-u", user, "--", *cmd], capture_output=True, text=True,
                          check=check, timeout=timeout)


def unavailable(user: str) -> str | None:
    """Why the agent cannot run as `user`, or None when it can."""
    if shutil.which("sudo") is None:
        return f"{AGENT_USER_ENV}={user} is set but sudo is not installed"
    try:
        done = _sudo(user, "/usr/bin/env", "--version", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{AGENT_USER_ENV}={user} is set but `sudo -n -u {user}` failed: {exc}"
    if done.returncode != 0:
        return f"{AGENT_USER_ENV}={user} is set but `sudo -n -u {user}` is refused: {done.stderr.strip()[-200:]}"
    return None


@dataclass
class Isolated:
    user: str
    root: Path   # owned by the agent user, 0755: the steward reads the tree back from here
    tree: Path   # the agent's own copy of the clone; its working directory
    home: Path   # the agent's HOME, 0700


@contextmanager
def copy_for(user: str, clone: Path) -> Iterator[Isolated]:
    """The agent's own copy of `clone`, made and removed as the agent user."""
    root = Path(_sudo(user, "mktemp", "-d", "/tmp/steward-agent-XXXXXXXX").stdout.strip())
    try:
        iso = Isolated(user, root, root / "tree", root / "home")
        _sudo(user, "mkdir", "-p", str(iso.tree), str(iso.home))
        _sudo(user, "chmod", "755", str(root))
        _sudo(user, "chmod", "700", str(iso.home))
        _sudo(user, "cp", "-RP", "--preserve=mode,timestamps", f"{clone}/.", str(iso.tree))
        yield iso
    finally:
        _sudo(user, "rm", "-rf", str(root), check=False)


def command(iso: Isolated, argv: Sequence[str], env: Mapping[str, str], path_first: Path | None) -> list[str]:
    """`argv` as the agent user, in its tree, with `env`. Secrets travel in
    the environment (`--preserve-env`), never on the command line where
    another user could read them in the process list; PATH, HOME and the
    directory are not secret and are set explicitly, since sudo's
    secure_path would otherwise replace PATH."""
    path = os.pathsep.join([*( [str(path_first)] if path_first else []), *SYSTEM_PATH])
    keep = sorted(k for k in env if k not in ("PATH", "HOME"))
    preserve = [f"--preserve-env={','.join(keep)}"] if keep else []
    return ["sudo", "-n", *preserve, "-u", iso.user, "--",
            "/usr/bin/env", "-C", str(iso.tree), f"PATH={path}", f"HOME={iso.home}", *argv]


# ── copying the agent's work back ─────────────────────────────────────

@dataclass
class Synced:
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # "path: why"


def _entries(root: Path) -> dict[str, os.stat_result]:
    """Every non-directory entry under `root` except `.git`, by relative path,
    without following symlinks (a symlinked directory is one entry)."""
    out: dict[str, os.stat_result] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in [*filenames, *(d for d in dirnames if os.path.islink(os.path.join(dirpath, d)))]:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            out[rel] = os.lstat(os.path.join(dirpath, name))
    return out


def _escapes(rel: str, target: str) -> bool:
    if os.path.isabs(target):
        return True
    resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return resolved == ".." or resolved.startswith("../") or resolved.split(os.sep)[0] == ".git"


def _make_real_parents(clone: Path, rel: str) -> None:
    """Every directory above `rel` in the clone is a real directory, not a
    symlink the write would go through."""
    current = clone
    for part in Path(rel).parent.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            current.unlink()
        if not current.exists():
            current.mkdir()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _same(src: Path, dst: Path, st: os.stat_result) -> bool:
    if not dst.is_file() or dst.is_symlink():
        return False
    if (os.lstat(dst).st_mode & stat.S_IXUSR) != (st.st_mode & stat.S_IXUSR):
        return False
    return dst.read_bytes() == src.read_bytes()


def sync_back(tree: Path, clone: Path, skip: Iterable[str] = ()) -> Synced:
    """Make the clone's working tree match the agent's, outside `.git`.

    `skip` are clone-relative paths the steward owns locally (its prompt,
    the result file); they are neither copied nor removed."""
    skipped = tuple(s.rstrip("/") for s in skip)

    def owned(rel: str) -> bool:
        return any(rel == s or rel.startswith(s + "/") for s in skipped)

    out = Synced()
    theirs, ours = _entries(tree), _entries(clone)
    # Removals first: an entry the agent replaced (a directory symlink it
    # turned into a real directory) must go before its new contents land.
    for rel in sorted(set(ours) - set(theirs)):
        if owned(rel):
            continue
        _remove(clone / rel)
        out.removed.append(rel)
    for rel, st in sorted(theirs.items()):
        if owned(rel):
            continue
        src, dst = tree / rel, clone / rel
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(src)
            if _escapes(rel, target):
                out.rejected.append(f"{rel}: a symlink to `{target}`, outside the repository")
                continue
            if dst.is_symlink() and os.readlink(dst) == target:
                continue
            _make_real_parents(clone, rel)
            _remove(dst)
            os.symlink(target, dst)
        elif stat.S_ISREG(st.st_mode):
            if _same(src, dst, st):
                continue
            _make_real_parents(clone, rel)
            _remove(dst)
            dst.write_bytes(src.read_bytes())
            dst.chmod(0o755 if st.st_mode & stat.S_IXUSR else 0o644)
        else:
            out.rejected.append(f"{rel}: not a regular file or symlink")
            continue
        out.changed.append(rel)
    return out
