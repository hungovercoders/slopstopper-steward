"""The deterministic executor against a throwaway repo: pin 0.25.1 → the
PyPI version under test, `slopstopper install` from it, the static checks.
Needs PyPI, so it runs only with STEWARD_E2E=1 (CI sets it)."""

import os
import subprocess
from pathlib import Path

import pytest

from steward.execute import refresh
from steward.versions import MIN_INSTALL_VERSION
from steward.workspace import Workspace

pytestmark = pytest.mark.skipif(not os.environ.get("STEWARD_E2E"), reason="set STEWARD_E2E=1 to hit PyPI")
# STEWARD_E2E_SOURCE points at a slopstopper `cli/` checkout: the installer
# shipped after 0.26.0, so until that release is on PyPI (and in CI always, so
# the nightly proves main's installer still does what the steward expects)
# the executor is driven from source.
SOURCE = Path(os.environ["STEWARD_E2E_SOURCE"]) if os.environ.get("STEWARD_E2E_SOURCE") else None
TARGET = os.environ.get("STEWARD_E2E_VERSION", MIN_INSTALL_VERSION)


def test_upgrade_executor_end_to_end(tmp_path):
    repo = tmp_path / "adopter"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "mise.toml").write_text('[tools]\n"task" = "3"\n"pipx:slopstopper-cli" = "0.25.1"\n')
    (repo / ".slopstopper.yml").write_text("profile: library\n")
    (repo / "README.md").write_text("# adopter\n")
    ws = Workspace("acme/adopter", repo, "main")

    assert refresh.set_pin(repo / "mise.toml", TARGET) == "moved"
    cli = refresh.cli_for(TARGET, tmp_path / "venv", source=SOURCE)
    assert cli.is_file()
    installed = refresh.install_suite(ws, cli)
    assert installed.returncode == 0, installed.stderr
    if not SOURCE:
        assert (repo / ".ss" / ".installed-from").read_text().strip() == TARGET
    assert (repo / ".github" / "workflows" / "ss-security-sast-check.yml").is_file()
    assert (repo / ".claude" / "skills" / "slopstopper-triage" / "SKILL.md").is_file()
    assert not (repo / ".github" / "workflows" / "ss-security-dast-check.yml").exists()  # library profile

    checks = refresh.static_checks(ws, cli)  # the real CLI's listing, not a fallback
    assert "hygiene:complexity" in checks.runnable and "hygiene:docs-structure" in checks.runnable
    assert not any(c.startswith("reliability:") for c in checks.runnable)  # library drops the URL-driven checks
    assert checks.environment == ["security:licences"]        # run apart as the agent user, never here
    assert "security:licences" not in {u["check"] for u in checks.unverified}
    red = refresh.verify(ws, cli, checks.runnable)
    assert {r["check"] for r in red} <= {"hygiene:entry-files"}, red  # a bare README is the one expected red
    assert all("reports" in r for r in red)  # the whole verdict was parsed, not synthesised from the exit code
    assert ws.dirty() and "mise.toml" in ws.changed_files()
