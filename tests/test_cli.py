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
    monkeypatch.setattr(cli.gh, "whoami", lambda: "bot")
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
    monkeypatch.setattr(cli.gh, "whoami", lambda: "bot")
    inv = Inventory(Repo("acme/a", pinned="0.24.0", evidence=["mise-pin"]), "0.27.0")
    monkeypatch.setattr(runner, "gather", lambda c, only=None: ([inv], {"acme/a": "0.27.0"}, "0.26.0"))
    rc = cli.main(["--config", str(cfg), "run", "--dry-run", "--report-dir", str(tmp_path / "r"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["mode"] == "plan" and out["outcomes"][0]["status"] == "planned"
    assert (tmp_path / "r" / "run.md").read_text().startswith("# Steward run (plan)")


def test_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "none.yml"), "plan"]) == 2


def test_module_entrypoint():
    done = subprocess.run([sys.executable, "-m", "steward", "--version"], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0 and "slopstopper-steward" in done.stdout
