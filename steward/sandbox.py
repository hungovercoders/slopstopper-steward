"""The environment for anything that runs on files the agent could have touched.

The agent's run and the steward's own verification after it share this:
no GitHub credentials, a throwaway HOME (so no `~/.gitconfig`, no stored
`gh` login, no tool config the agent could have planted), and hardening
that stops Python and git picking up code from the working tree. Only the
agent itself gets the model key.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

KEEP = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL", "NODE_OPTIONS",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "CI", "RUNNER_TEMP")
MODEL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN")
# The XDG base directories, named with HOME wherever HOME is set: a tool
# that honours them (Semgrep puts its settings under `$XDG_CONFIG_HOME`,
# git reads `$XDG_CONFIG_HOME/git/config`) otherwise keeps using the
# steward's own, which the agent user cannot read.
XDG = {"XDG_CONFIG_HOME": ".config", "XDG_CACHE_HOME": ".cache", "XDG_DATA_HOME": ".local/share",
       "XDG_STATE_HOME": ".local/state"}
HARDENING = {
    "PYTHONSAFEPATH": "1",          # `python -m lizard` must not import a lizard.py from the clone
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}


def environment(path_prepend: Path | None = None, home: Path | None = None, model_key: bool = False,
                base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    keep = KEEP + (MODEL_KEYS if model_key else ())
    # An unset Actions secret arrives as an empty string; a model key that is
    # empty is no key, and must not reach the agent as one.
    env = {k: v for k, v in source.items() if k in keep and (v or k not in MODEL_KEYS)}
    if home is not None:
        env["HOME"] = str(home)
        env.update(xdg(home))
    elif "HOME" in source:
        env["HOME"] = source["HOME"]
    env.update(HARDENING)
    if path_prepend:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env.get('PATH', '')}"
    return env


@contextmanager
def scratch_home(prefix: str = "steward-home-") -> Iterator[Path]:
    home = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def xdg(home: Path) -> dict[str, str]:
    """The XDG base directories under `home`."""
    return {k: str(home / rel) for k, rel in XDG.items()}
