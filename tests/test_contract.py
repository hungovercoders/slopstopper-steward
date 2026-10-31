"""The sync test: every slopstopper surface the steward relies on still
holds. Points at a slopstopper checkout: $SLOPSTOPPER_SRC, else a sibling
`../slopstopper`, else a shallow clone of main into a temp dir (CI)."""

import os
import subprocess
from pathlib import Path

import pytest

from steward import contract


def _src(tmp_path_factory) -> Path:
    env = os.environ.get("SLOPSTOPPER_SRC")
    if env:
        return Path(env)
    sibling = Path(__file__).resolve().parents[2] / "slopstopper"
    if (sibling / "cli" / "pyproject.toml").is_file():
        return sibling
    target = tmp_path_factory.mktemp("slopstopper")
    done = subprocess.run(["git", "clone", "--depth", "1", "https://github.com/hungovercoders/slopstopper", str(target)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"no slopstopper checkout and clone failed: {done.stderr.strip()[-200:]}")
    return target


def test_every_surface_holds(tmp_path_factory):
    src = _src(tmp_path_factory)
    problems = contract.check(src)
    assert not problems, "\n".join(problems)


def test_check_reports_missing(tmp_path):
    problems = contract.check(tmp_path)
    assert len(problems) == len(contract.SURFACES) and all("missing" in p for p in problems)


def test_table_renders_every_row():
    assert contract.table().count("\n") == len(contract.SURFACES) + 1
