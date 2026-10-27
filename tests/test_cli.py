import json
import subprocess
import sys
from pathlib import Path

from steward import cli

ROOT = Path(__file__).resolve().parents[1]


def test_contract_command_json(tmp_path, capsys):
    rc = cli.main(["contract", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["surfaces"] == len(out["problems"])


def test_plan_json_shape(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "steward.yml"
    cfg.write_text("orgs: [acme]\n")
    from steward import runner
    from steward.config import from_dict
    from steward.discover import Repo
    from steward.inventory import Inventory
    monkeypatch.setattr(cli.gh, "available", lambda: True)
    monkeypatch.setattr(cli.gh, "authenticated", lambda: True)
    inv = Inventory(Repo("acme/a", pinned="0.24.0", evidence=["mise-pin"]), "0.27.0")
    monkeypatch.setattr(runner, "gather", lambda c, only=None: ([inv], {"acme/a": "0.27.0"}, "0.26.0"))
    rc = cli.main(["--config", str(cfg), "plan", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["orders"][0]["id"] == "upgrade-0.27.0" and out["deferred"] == [] and out["skipped"] == []


def test_run_dry_run_writes_report(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "steward.yml"
    cfg.write_text("orgs: [acme]\n")
    from steward import runner
    from steward.discover import Repo
    from steward.inventory import Inventory
    monkeypatch.setattr(cli.gh, "available", lambda: True)
    monkeypatch.setattr(cli.gh, "authenticated", lambda: True)
    inv = Inventory(Repo("acme/a", pinned="0.24.0", evidence=["mise-pin"]), "0.27.0")
    monkeypatch.setattr(runner, "gather", lambda c, only=None: ([inv], {"acme/a": "0.27.0"}, "0.26.0"))
    rc = cli.main(["--config", str(cfg), "run", "--dry-run", "--report-dir", str(tmp_path / "r"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "plan" and out["outcomes"][0]["status"] == "planned"
    assert (tmp_path / "r" / "run.md").read_text().startswith("# Steward run (plan)")


def test_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "none.yml"), "plan"]) == 2


def test_blank_numeric_key_is_a_default_not_a_traceback(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "steward.yml"
    cfg.write_text("orgs: [acme]\nupgrade:\n  min_age_days:\nrepos:\n  include:\n    # - acme/a\n")
    from steward import config
    loaded = config.load(cfg)
    assert loaded.min_age_days == 2 and loaded.include == []


def test_could_not_run_errors_exit_2(tmp_path, monkeypatch):
    import subprocess
    cfg = tmp_path / "steward.yml"
    cfg.write_text("orgs: [acme]\n")
    monkeypatch.setattr(cli.gh, "available", lambda: True)
    monkeypatch.setattr(cli.gh, "authenticated", lambda: True)
    from steward import runner

    def hang(c, only=None):
        raise subprocess.TimeoutExpired(["gh"], 120)
    monkeypatch.setattr(runner, "gather", hang)
    assert cli.main(["--config", str(cfg), "plan"]) == 2

    def bad_json(c, only=None):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(runner, "gather", bad_json)
    assert cli.main(["--config", str(cfg), "plan"]) == 2


def test_app_token_without_identity_is_accepted(monkeypatch, tmp_path):
    """An installation token cannot call GET /user; the gate asks gh for a token instead."""
    import subprocess as sp
    from steward import gh

    def fake_run(args, **kw):
        if args[:3] == ["gh", "auth", "token"]:
            return sp.CompletedProcess(args, 0, "ghs_installation\n", "")
        if args[:3] == ["gh", "api", "user"]:
            return sp.CompletedProcess(args, 1, "", "HTTP 403: Resource not accessible by integration")
        raise AssertionError(args)
    monkeypatch.setattr(gh, "RUN", fake_run)
    assert gh.authenticated() and gh.whoami() is None


def test_module_entrypoint():
    done = subprocess.run([sys.executable, "-m", "steward", "--version"], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0 and "slopstopper-steward" in done.stdout
