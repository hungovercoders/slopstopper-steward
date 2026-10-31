from steward import prompts
from steward.discover import Repo
from steward.inventory import Finding, Inventory
from steward.plan import Order


def _order():
    f = Finding(12, "🛡️ CVEs", "https://x/12", ["slopstopper"], check="security:vulnerability:all")
    return Order("triage-12", "triage", "acme/a", "fix", "why", 2, "main", "0.26.0", "0.26.0", f, True), Inventory(Repo("acme/a"), "0.26.0")


def test_triage_prompt_names_skill_issue_and_result_file():
    order, inv = _order()
    text = prompts.triage(order, inv, "the body", "hungovercoders/slopstopper")
    for needle in ("slopstopper-triage/SKILL.md", "#12", "security:vulnerability:all", prompts.RESULT_FILE,
                   "slopstopper-workaround: hungovercoders/slopstopper#", "Never edit `.github/workflows/ss-*.yml`", "the body"):
        assert needle in text


def test_verify_prompt_points_at_reports_when_no_bullets():
    order, inv = _order()
    order.kind, order.target, order.pinned = "upgrade", "0.27.0", "0.24.0"
    text = prompts.verify(order, inv, [{"check": "hygiene:entry-files", "exit_code": 1, "status": "fail", "failures": [],
                                        "reports": [".ss/reports/entry-files/entry-file-size-report.md"]}], "hungovercoders/slopstopper")
    assert "report: `.ss/reports/entry-files/entry-file-size-report.md`" in text


def test_verify_prompt_lists_failures():
    order, inv = _order()
    order.kind, order.target, order.pinned = "upgrade", "0.26.0", "0.24.0"
    text = prompts.verify(order, inv, [{"check": "hygiene:entry-files", "exit_code": 1, "status": "fail", "failures": ["- ❌ AGENTS.md over budget"]}], "hungovercoders/slopstopper")
    assert "`hygiene:entry-files` exit 1" in text and "AGENTS.md over budget" in text and "0.24.0" in text
