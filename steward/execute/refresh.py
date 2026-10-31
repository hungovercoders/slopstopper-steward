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

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from steward.checks import NEEDS_ENVIRONMENT
from steward import sandbox
from steward.pin import PIN_KEY, PIN_LINE, PinError, set_pin  # noqa: F401  (re-exported)
from steward.versions import MIN_INSTALL_VERSION
from steward.workspace import Workspace


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


def verify(ws: Workspace, cli: Path, checks: Sequence[str]) -> list[dict]:
    """Run the given checks and return the red rows as `run --json` printed
    them. Exit 2 (could not run) counts as red too: the checks handed in are
    ones this clone can run, so a could-not-run here is a real problem. A
    check that hangs is red with the reason, never an exception that would
    throw away the work already done. Runs in the sandbox environment: the
    tree may hold what the agent wrote."""
    red = []
    with sandbox.scratch_home() as home:
        env = sandbox.environment(cli.parent, home)
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
