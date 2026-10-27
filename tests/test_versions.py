from datetime import datetime, timedelta, timezone

from steward import versions

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
REL = {"0.24.0": OLD, "0.25.1": OLD, "0.26.0": OLD, "1.0.0": OLD, "1.0.1": NOW}


def test_parse():
    assert versions.parse("v1.2.3") == (1, 2, 3) and versions.parse("1.2") is None and versions.parse(None) is None


def test_latest_policy_skips_too_young():
    assert versions.target_version("0.25.1", REL, "latest", 2, NOW) == "1.0.0"
    assert versions.target_version("0.25.1", REL, "latest", 0, NOW) == "1.0.1"


def test_minor_and_patch_policies():
    assert versions.target_version("0.24.0", REL, "minor", 0, NOW) == "0.26.0"
    assert versions.target_version("0.25.1", REL, "patch", 0, NOW) is None
    assert versions.target_version("1.0.0", REL, "patch", 0, NOW) == "1.0.1"


def test_current_pin_stays():
    assert versions.target_version("1.0.1", REL, "latest", 0, NOW) is None


def test_unpinned_targets_newest_allowed():
    assert versions.target_version(None, REL, "latest", 2, NOW) == "1.0.0"


def test_behind():
    assert versions.behind("0.25.0", "0.26.0") and not versions.behind("0.26.0", "0.26.0") and versions.behind(None, "0.1.0")
