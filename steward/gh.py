"""Every GitHub call goes through the `gh` CLI, as slopstopper's own `emit`
does: the runner already has it, it is already authenticated, and the
steward's wheel ships no GitHub client. `RUN` is the one seam the tests
replace."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

RUN = subprocess.run


class GhError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("gh") is not None


def gh(*args: str, cwd: str | None = None, input: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return RUN(["gh", *args], capture_output=True, text=True, cwd=cwd, input=input, timeout=timeout, check=False)


def _api_args(path: str, paginate: bool, method: str, fields: dict[str, str] | None, accept: str | None) -> list[str]:
    args = ["api", path, "-X", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    if accept:
        args += ["-H", f"Accept: {accept}"]
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    return args


def _not_found(result: subprocess.CompletedProcess) -> bool:
    return "404" in result.stderr or "Not Found" in result.stderr


def _parse(stdout: str, paginate: bool) -> Any:
    if not stdout.strip():
        return None
    data = json.loads(stdout)
    if paginate and isinstance(data, list) and data and isinstance(data[0], list):
        return [row for page in data for row in page]
    return data


def api(path: str, *, paginate: bool = False, method: str = "GET", fields: dict[str, str] | None = None,
        accept: str | None = None) -> Any:
    """`gh api` as parsed JSON. 404 returns None; other failures raise."""
    result = gh(*_api_args(path, paginate, method, fields, accept))
    if result.returncode != 0:
        if _not_found(result):
            return None
        raise GhError(f"gh api {path}: {result.stderr.strip() or result.stdout.strip()}")
    return _parse(result.stdout, paginate)


def contents(repo: str, path: str, ref: str | None = None) -> str | None:
    """A file's text from the default branch (or `ref`), None when absent."""
    url = f"repos/{repo}/contents/{path}"
    if ref:
        url += f"?ref={ref}"
    result = gh("api", url, "-H", "Accept: application/vnd.github.raw+json")
    if result.returncode != 0:
        if _not_found(result):
            return None
        raise GhError(f"gh api {url}: {result.stderr.strip()}")
    return result.stdout


def authenticated() -> bool:
    """Whether gh has a token to send. A GitHub App installation token
    cannot call GET /user, so this asks gh for the token it would use rather
    than for an identity; a token that turns out to be wrong fails the
    first real call with a GhError, which exits 2 the same way."""
    result = gh("auth", "token")
    return result.returncode == 0 and bool(result.stdout.strip())


def whoami() -> str | None:
    """The login behind the token, or None (an App token has none)."""
    result = gh("api", "user", "--jq", ".login")
    return result.stdout.strip() if result.returncode == 0 else None
