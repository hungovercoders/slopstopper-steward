from steward import discover, inventory

MISE = '[tools]\n"task" = "3"\n"pipx:slopstopper-cli" = "0.24.0"\n'


def test_read_pin_variants():
    assert discover.read_pin(MISE) == "0.24.0"
    assert discover.read_pin('[tools]\npipx:slopstopper-cli = "0.9.0"\n') == "0.9.0"
    assert discover.read_pin('[tools]\n"task" = "3"\n') is None
    assert discover.read_pin(None) is None


def test_classify_check_issue():
    f = inventory.classify({"number": 7, "title": "🛡️ Dependency CVEs", "html_url": "u", "labels": [{"name": "slopstopper"}],
                            "body": "report\n<!-- slopstopper:check=security:vulnerability:all -->\n<!-- slopstopper:workflow=ss-security-vulnerability-all-check.yml -->"})
    assert f.kind == "check" and f.check == "security:vulnerability:all" and f.security
    assert f.workflow == "ss-security-vulnerability-all-check.yml"


def test_classify_workflow_failure():
    f = inventory.classify({"number": 8, "title": "❌ Workflow Failure: SlopStopper · SAST Analysis", "html_url": "u",
                            "labels": [{"name": "workflow-failure"}, {"name": "slopstopper"}], "body": "no marker"})
    assert f.kind == "workflow-failure" and f.workflow == "SlopStopper · SAST Analysis" and not f.security


def test_classify_other():
    f = inventory.classify({"number": 9, "title": "manual", "html_url": "u", "labels": [{"name": "slopstopper"}], "body": ""})
    assert f.kind == "other"


def test_read_profile():
    assert inventory.read_profile("# cfg\nprofile: library\nurls:\n") == "library"
    assert inventory.read_profile("urls:\n") is None


def test_take_reads_issues_prs_and_workflows(monkeypatch):
    repo = discover.Repo("acme/site", pinned="0.24.0", evidence=["mise-pin"])
    files = {".slopstopper.yml": "profile: api\n", inventory.TRIAGE_SKILL: "---\nname: slopstopper-triage\n"}
    monkeypatch.setattr(inventory.gh, "contents", lambda r, p, ref=None: files.get(p))

    def api(path, **kw):
        if path.startswith("repos/acme/site/issues"):
            return [{"number": 1, "title": "x", "html_url": "u", "labels": [{"name": "slopstopper"}],
                     "body": "<!-- slopstopper:check=hygiene:docs-size -->"},
                    {"number": 2, "title": "pr", "html_url": "u", "labels": [], "body": "", "pull_request": {}},
                    {"number": 3, "title": "ignored", "html_url": "u", "labels": [{"name": "steward:ignore"}], "body": ""}]
        if path.startswith("repos/acme/site/actions/workflows"):
            return {"workflows": [{"name": "a", "path": ".github/workflows/ss-x.yml", "state": "disabled_inactivity"},
                                  {"name": "b", "path": ".github/workflows/ss-y.yml", "state": "active"},
                                  {"name": "c", "path": ".github/workflows/own.yml", "state": "disabled_manually"}]}
        if path.startswith("repos/acme/site/pulls"):
            return [{"number": 10, "html_url": "p", "head": {"ref": "steward/upgrade-0.26.0"}, "body": "<!-- steward:order=upgrade-0.26.0 -->"},
                    {"number": 11, "html_url": "p", "head": {"ref": "feature"}, "body": ""}]
        raise AssertionError(path)
    monkeypatch.setattr(inventory.gh, "api", api)
    from steward.config import from_dict
    inv = inventory.take(repo, "0.26.0", from_dict({"orgs": ["acme"]}))
    assert inv.profile == "api" and inv.skills_present
    assert [f.number for f in inv.findings] == [1]
    assert inv.disabled_workflows == [{"name": "a", "path": ".github/workflows/ss-x.yml", "state": "disabled_inactivity"}]
    assert inv.open_orders == {"upgrade-0.26.0": {"number": 10, "url": "p"}}


def test_discover_probes_every_repo(monkeypatch):
    from steward.config import from_dict
    rows = [{"full_name": "acme/a", "default_branch": "main", "topics": ["slopstopper"], "archived": False},
            {"full_name": "acme/b", "default_branch": "trunk", "topics": [], "archived": False},
            {"full_name": "acme/c", "default_branch": "main", "topics": [], "archived": False},
            {"full_name": "acme/old", "default_branch": "main", "topics": ["slopstopper"], "archived": True}]
    monkeypatch.setattr(discover.gh, "api", lambda path, **kw: rows)
    files = {("acme/a", "mise.toml"): MISE, ("acme/b", ".ss/.installed-from"): "0.20.0\n"}
    monkeypatch.setattr(discover.gh, "contents", lambda r, p, ref=None: files.get((r, p)))
    found = discover.discover(from_dict({"orgs": ["acme"]}))
    assert [(r.full_name, r.evidence) for r in found] == [("acme/a", ["topic", "mise-pin"]), ("acme/b", ["installed-from"])]
    assert found[1].installed_from == "0.20.0" and found[1].default_branch == "trunk"
