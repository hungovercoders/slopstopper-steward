"""Move the pin and rewrite the suite, with the pinned CLI doing the writing.

`slopstopper install` from version X writes the suite X ships, which is
exactly what `install.sh --cli-version X` does after it has moved the
pin, minus mise. The steward moves the pin itself (the one-line rewrite
`mise use` would do), installs slopstopper-cli==X into a throwaway venv
and runs its installer. Then the static checks run against the result:
green ships as a ready PR; red goes to the agent, and a draft PR if the
agent cannot settle it, with the failures in the body.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from steward.checks import NEEDS_ENVIRONMENT
from steward import gh, sandbox
from steward.pin import PIN_KEY, PIN_LINE, PinError, set_pin  # noqa: F401  (re-exported)
from steward.versions import MIN_INSTALL_VERSION
from steward.workspace import LOCAL_ONLY, Workspace


def cli_for(version: str, venv: Path, package: str = "slopstopper-cli", source: Path | None = None) -> Path:
    """The `slopstopper` binary of exactly `version`, in its own venv. `source`
    (a slopstopper `cli/` checkout) installs that instead, for the end-to-end
    test against an unreleased installer."""
    binary, marker = venv / "bin" / "slopstopper", venv / ".steward-installed"
    want = f"source:{source}" if source else f"{package}=={version}"
    if binary.exists() and marker.is_file() and marker.read_text(encoding="utf-8") == want:
        return binary  # one venv per version per run, shared by every order on it
    if not (venv / "bin" / "python").exists():
        venv.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    spec = ["-e", str(source)] if source else [want]
    subprocess.run([str(venv / "bin" / "pip"), "install", "-q", *spec], check=True, capture_output=True, text=True)
    marker.write_text(want, encoding="utf-8")
    return binary


class InstallerMissing(RuntimeError):
    """The CLI at this version has no `install` subcommand."""


def install_suite(ws: Workspace, cli: Path) -> subprocess.CompletedProcess:
    with sandbox.scratch_home() as home:
        done = subprocess.run([str(cli), "install", "--no-topic", str(ws.path)], cwd=ws.path, capture_output=True,
                              text=True, check=False, timeout=600, env=sandbox.environment(cli.parent, home))
    if done.returncode == 2 and "invalid choice: 'install'" in done.stderr:
        raise InstallerMissing(f"{cli} has no `install` subcommand; slopstopper-cli {MIN_INSTALL_VERSION}+ is needed")
    return done


class CannotVerify(RuntimeError):
    """The pinned CLI could not say which checks it has."""


def _json(cli: Path, cwd: Path, *args: str, ok_codes: tuple[int, ...] = (0,)):
    """One `--json` command of the pinned CLI, parsed from its whole stdout
    (the CLI prints indented JSON and nothing else there); None when it
    cannot answer."""
    try:
        with sandbox.scratch_home() as home:
            done = subprocess.run([str(cli), *args, "--json"], cwd=cwd, capture_output=True, text=True, timeout=120,
                                  env=sandbox.environment(cli.parent, home))
    except (subprocess.TimeoutExpired, OSError):
        return None
    if done.returncode not in ok_codes or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


@dataclass
class Checks:
    """What a verification pass runs, and what it cannot and says so."""
    runnable: list[str] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)  # {check, why}


def _missing_tools(cli: Path, cwd: Path) -> dict[str, str]:
    """check name -> the tool `doctor --json` reports missing or broken for it.
    doctor exits 1 when something is missing, which is the answer, not a failure."""
    doctor = _json(cli, cwd, "doctor", ok_codes=(0, 1)) or {}
    out: dict[str, str] = {}
    for row in doctor.get("tools") or []:
        if row.get("status") not in ("missing", "broken"):
            continue
        needed = row.get("needed_by")
        for check in ([needed] if isinstance(needed, str) else needed or []):
            out[str(check)] = str(row.get("tool"))
    return out


def static_checks(ws: Workspace, cli: Path) -> Checks:
    """The checks this clone can verify, all named by the pinned CLI: what
    the repo keeps (`checks list`), minus what the `library` profile drops
    (slopstopper's own list of checks that need a deployed surface). Of
    those, a check whose tool `doctor` reports missing on this runner, or
    one that needs an installed environment, is listed as unverified rather
    than run: its exit 2 would say nothing about the change. The PR's own
    `ss-*` workflows run them."""
    rows = _json(cli, ws.path, "checks", "list")
    expanded = _json(cli, ws.path, "profile", "expand", "library")
    if not isinstance(rows, list) or not isinstance(expanded, dict) or "workflows" not in expanded:
        raise CannotVerify(f"{cli} could not list its checks (`checks list --json`, `profile expand library --json`)")
    dropped = set(expanded["workflows"])
    kept = [r["name"] for r in rows if not r.get("disabled") and r.get("workflow") not in dropped]
    missing = _missing_tools(cli, ws.path)
    out = Checks()
    for check in kept:
        if check in NEEDS_ENVIRONMENT:
            out.unverified.append({"check": check, "why": "needs the project's installed dependencies; its workflow installs them"})
        elif check in missing:
            out.unverified.append({"check": check, "why": f"`{missing[check]}` is not installed on the steward's runner"})
        else:
            out.runnable.append(check)
    return out


CHECK_TIMEOUT = 900

# Trivy's vulnerability DB, kept across verification passes and orders: each
# pass has a fresh HOME, and pulling the DB into an empty cache every time
# is what gets a runner rate-limited by the registry. Trivy writes it; it
# runs nothing from the tree.
TRIVY_CACHE = Path(tempfile.gettempdir()) / "steward-trivy-cache"


def verify(ws: Workspace, cli: Path, checks: Sequence[str]) -> list[dict]:
    """Run the given checks and return the red rows as `run --json` printed
    them. Exit 2 (could not run) counts as red too: the checks handed in are
    ones this clone can run, so a could-not-run here is a real problem. A
    check that hangs is red with the reason, never an exception that would
    throw away the work already done. Runs in the sandbox environment: the
    tree may hold what the agent wrote."""
    red = []
    with sandbox.scratch_home() as home:
        env = {**sandbox.environment(cli.parent, home), "TRIVY_CACHE_DIR": str(TRIVY_CACHE)}
        for check in checks:
            verdict = _run_one(ws, cli, check, env)
            if verdict["exit_code"] != 0:
                red.append(verdict)
    return red


def _run_one(ws: Workspace, cli: Path, check: str, env: dict) -> dict:
    try:
        run = subprocess.run([str(cli), "run", "--json", check], cwd=ws.path, capture_output=True, text=True,
                             timeout=CHECK_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"check": check, "exit_code": 2, "status": "timed-out", "reports": [],
                "failures": [f"`slopstopper run {check}` did not finish within {CHECK_TIMEOUT} s"]}
    try:
        verdict = json.loads(run.stdout) if run.stdout.strip() else {}
    except json.JSONDecodeError:
        verdict = {}
    if not isinstance(verdict, dict):
        verdict = {}
    verdict.setdefault("check", check)
    verdict.setdefault("exit_code", run.returncode)
    verdict.setdefault("status", {0: "pass", 1: "fail"}.get(run.returncode, "cannot-run"))
    verdict.setdefault("failures", [])
    if verdict["exit_code"] != 0:
        verdict["stderr_tail"] = "\n".join(run.stderr.strip().splitlines()[-10:])
    return verdict


DIFF_LIMIT = 60_000


def _tree_diff(ws: Workspace, *flags: str) -> str:
    """Everything that differs from HEAD: tracked changes, then each new file
    as an addition. Before the agent runs, that is the pin move and what
    `slopstopper install` wrote, wherever it wrote it; no list of the paths
    the installer owns is kept here. The steward's own files are left out."""
    spec = ws._pathspec() + [f":(exclude){p.rstrip('/')}" for p in LOCAL_ONLY]
    parts = [ws.git("diff", "--no-color", *flags, "HEAD", *spec, check=False).stdout]
    new = ws.git("ls-files", "-z", "--others", "--exclude-standard", *spec, check=False).stdout
    for rel in sorted(p for p in new.split("\0") if p):
        # --no-index exits 1 when the files differ, which they always do here
        parts.append(ws.git("diff", "--no-color", *flags, "--no-index", "--", os.devnull, rel, check=False).stdout)
    return "".join(parts)


def suite_diff(ws: Workspace) -> str:
    """The install's change, as a unified diff the agent can read (it has no
    `git diff`), cut to a size a brief can hold."""
    text = _tree_diff(ws) or "(the install changed nothing)\n"
    return text if len(text) <= DIFF_LIMIT else text[:DIFF_LIMIT] + "\n… (cut: the rest is in the PR's diff)\n"


def fingerprint(ws: Workspace) -> str:
    """A digest of the tree's change from HEAD, binary files included: equal
    before and after the agent means it left the tree as it was."""
    return hashlib.sha256(_tree_diff(ws, "--binary").encode("utf-8", "surrogateescape")).hexdigest()


# The marker slopstopper's skills put on a workaround (contract.py holds the
# surface): `slopstopper-workaround: <owner>/<repo>#<N>`.
_MARKER = re.compile(r"slopstopper-workaround: ([\w.-]+/[\w.-]+)#(\d+)")
_SKIP_DIRS = {".git", ".ss", ".claude", "node_modules", *(p.rstrip("/") for p in LOCAL_ONLY)}
SCAN_LIMIT = 1_000_000


def _text(path: Path) -> str | None:
    """A regular file's text, or None: a symlink, FIFO or device is never
    opened (it could point anywhere, or block), nor a file over the limit."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > SCAN_LIMIT:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            return fh.read(SCAN_LIMIT + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def workarounds(ws: Workspace) -> list[dict]:
    """Every `slopstopper-workaround: <repo>#<N>` marker in the clone, with
    the files it is in. The skills are left out (they spell the convention),
    and so are the steward's own files."""
    found: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(ws.path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.parent == ws.path and name in LOCAL_ONLY:
                continue
            text = _text(path)
            if text is None:
                continue
            for repo, number in _MARKER.findall(text):
                found.setdefault(f"{repo}#{number}", []).append(str(path.relative_to(ws.path)))
    return [{"marker": m, "files": sorted(set(f))} for m, f in sorted(found.items())]


def issue_state(marker: str) -> str:
    """`open`, `closed on <date>` or `unknown` for a `<repo>#<N>` marker."""
    repo, _, number = marker.partition("#")
    try:
        issue = gh.api(f"repos/{repo}/issues/{number}")
    except gh.GhError:
        issue = None
    if not issue:
        return "unknown (GitHub did not answer; leave the workaround in place)"
    if issue.get("state") == "closed":
        return f"closed on {(issue.get('closed_at') or '')[:10]}: {issue.get('title', '')}"
    return f"open: {issue.get('title', '')}"
