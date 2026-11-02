"""What the steward quotes in a PR body links nothing, and its own sections
sit above the footer."""

from steward import plan, runner
from steward.config import from_dict
from steward.inventory import Finding
from steward.redact import unlink

CFG = from_dict({"orgs": ["acme"]})


def test_closing_keywords_before_a_reference_are_broken_not_the_reference():
    notes = "* **app:** a Learn page ([#586](https://github.com/o/r/issues/586)), closes [#585](https://github.com/o/r/issues/585)"
    out = unlink(notes)
    assert "c<!-- -->loses [#585](https://github.com/o/r/issues/585)" in out and "[#586]" in out
    assert unlink("Fixes #12 and resolves acme/a#3 and Closed: GH-7") == "F<!-- -->ixes #12 and r<!-- -->esolves acme/a#3 and C<!-- -->losed: GH-7"
    assert unlink("fixes https://github.com/o/r/pull/9") == "f<!-- -->ixes https://github.com/o/r/pull/9"
    # prose that is not a reference is left alone
    assert unlink("the fix is in the lock; closes nothing; resolve the knob") == "the fix is in the lock; closes nothing; resolve the knob"


def test_the_pr_body_quotes_without_linking_but_keeps_its_own_closes():
    finding = Finding(12, "t", "u", ["slopstopper"], check="hygiene:docs-size")
    order = plan.Order("triage-12", "triage", "acme/a", "t", "r", 2, "main", "0.28.0", "0.28.0", finding=finding)
    body = runner._pr_body(order, CFG, "the agent says this fixes #12 too", "notes: closes [#585](https://github.com/o/r/issues/585)\n")
    assert body.startswith("Closes #12\n\n")                        # the steward's own, deliberate
    assert "f<!-- -->ixes #12" in body and "c<!-- -->loses [#585]" in body


def test_a_section_added_later_sits_above_the_footer():
    order = plan.Order("upgrade-0.28.0", "upgrade", "acme/a", "t", "r", 0, "main", "0.28.0", "0.27.0")
    body = runner._pr_body(order, CFG, "summary", "### Reviewed\n\nyes\n\n")
    out = runner._with_section(body, "### Checks\n\ngreen\n\n")
    assert out.index("### Checks") < out.index("---\nOpened by") and out.rstrip().endswith("<!-- steward:order=upgrade-0.28.0 -->")
    assert runner._with_section("no footer\n", "### Checks\n") == "no footer\n### Checks\n"
