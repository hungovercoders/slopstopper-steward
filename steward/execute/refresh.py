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
from pathlib import Path
from typing import Sequence

from steward.checks import FALLBACK_STATIC, NEEDS_ENVIRONMENT
from steward.discover import PIN_KEY, PIN_LINE
from steward.versions import MIN_INSTALL_VERSION
from steward.workspace import Workspace


def set_pin(mise_toml: Path, version: str) -> str:
    """Rewrite (or seed) the pin. Returns 'moved', 'seeded' or 'unchanged'."""
    if mise_toml.is_file():
        text = mise_toml.read_text(encoding="utf-8")
        if PIN_LINE.search(text):
            new = PIN_LINE.sub(lambda m: f'{m.group(1)}"{version}"', text, count=1)
            if new == text:
                return "unchanged"
            mise_toml.write_text(new, encoding="utf-8")
            return "moved"
        line = f'"{PIN_KEY}" = "{version}"\n'
        if re.search(r"^\[tools\]\s*$", text, re.M):
            text = re.sub(r"^\[tools\]\s*\n", f"[tools]\n{line}", text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n\n[tools]\n{line}"
        mise_toml.write_text(text, encoding="utf-8")
        return "seeded"
    mise_toml.write_text(f'[tools]\n"{PIN_KEY}" = "{version}"\n', encoding="utf-8")
    return "seeded"


def cli_for(version: str, venv: Path, package: str = "slopstopper-cli", source: Path | None = None) -> Path:
    """The `slopstopper` binary of exactly `version`, in its own venv. `source`
    (a slopstopper `cli/` checkout) installs that instead, for the end-to-end
    test against an unreleased installer."""
    if not (venv / "bin" / "python").exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    pip = venv / "bin" / "pip"
    spec = ["-e", str(source)] if source else [f"{package}=={version}"]
    subprocess.run([str(pip), "install", "-q", *spec], check=True, capture_output=True, text=True)
    return venv / "bin" / "slopstopper"


class InstallerMissing(RuntimeError):
    """The CLI at this version has no `install` subcommand."""


def install_suite(ws: Workspace, cli: Path) -> subprocess.CompletedProcess:
    done = subprocess.run([str(cli), "install", "--no-topic", str(ws.path)], cwd=ws.path, capture_output=True,
                          text=True, check=False, timeout=600)
    if done.returncode == 2 and "invalid choice: 'install'" in done.stderr:
        raise InstallerMissing(f"{cli} has no `install` subcommand; slopstopper-cli {MIN_INSTALL_VERSION}+ is needed")
    return done


def _json(cli: Path, cwd: Path, *args: str):
    """One `--json` command of the pinned CLI, parsed; None when it cannot answer."""
    try:
        done = subprocess.run([str(cli), *args, "--json"], cwd=cwd, capture_output=True, text=True, timeout=120)
        return json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0 and done.stdout.strip() else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def static_checks(ws: Workspace, cli: Path) -> list[str]:
    """The checks this clone can run: what the repo keeps (`checks list`),
    minus what the `library` profile drops (slopstopper's own list of
    URL-driven checks), minus what needs an installed environment."""
    rows = _json(cli, ws.path, "checks", "list") or []
    expanded = _json(cli, ws.path, "profile", "expand", "library") or {}
    dropped = set(expanded.get("workflows") or [])
    if not rows or not dropped:
        return [c for c in FALLBACK_STATIC if c not in {r["name"] for r in rows if r.get("disabled")}]
    return [r["name"] for r in rows
            if not r.get("disabled") and r.get("workflow") not in dropped and r["name"] not in NEEDS_ENVIRONMENT]


def verify(ws: Workspace, cli: Path, checks: Sequence[str] | None = None) -> list[dict]:
    """Run the given checks (default: every static check the repo keeps) and
    return the red rows as `run --json` printed them. Exit 2 (could not
    run) counts as red too, because a PR that silently drops a check is the
    thing to avoid."""
    red = []
    for check in (static_checks(ws, cli) if checks is None else checks):
        run = subprocess.run([str(cli), "run", "--json", check], cwd=ws.path, capture_output=True, text=True, timeout=900)
        try:
            verdict = json.loads(run.stdout.strip().splitlines()[-1]) if run.stdout.strip() else {}
        except json.JSONDecodeError:
            verdict = {}
        verdict.setdefault("check", check)
        verdict.setdefault("exit_code", run.returncode)
        verdict.setdefault("status", {0: "pass", 1: "fail"}.get(run.returncode, "cannot-run"))
        verdict.setdefault("failures", [])
        if verdict["exit_code"] != 0:
            verdict["stderr_tail"] = "\n".join(run.stderr.strip().splitlines()[-10:])
            red.append(verdict)
    return red
