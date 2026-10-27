"""Version arithmetic for the pin: what PyPI has, what policy allows."""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone

_NUM = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# The first slopstopper-cli release whose wheel carries `slopstopper install`
# (the suite written from the wheel's own files). Below it the steward cannot
# rewrite a suite without `install.sh` and mise, so an older release is never
# a target; a repo pinned below it moves straight to a release at or above it.
MIN_INSTALL_VERSION = "0.27.0"


def parse(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    match = _NUM.match(version.strip())
    return tuple(int(g) for g in match.groups()) if match else None  # type: ignore[return-value]


def pypi_releases(package: str, timeout: int = 20) -> dict[str, datetime]:
    """Every final release on PyPI with the time its first file was uploaded.
    Yanked releases and pre-releases are left out: the steward never moves a
    pin onto either."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=timeout) as resp:
        data = json.load(resp)
    out: dict[str, datetime] = {}
    for version, files in data.get("releases", {}).items():
        if parse(version) is None or not files or all(f.get("yanked") for f in files):
            continue
        stamps = [f.get("upload_time_iso_8601") for f in files if f.get("upload_time_iso_8601")]
        when = min(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps) if stamps else datetime.now(timezone.utc)
        out[version] = when
    return out


def target_version(pinned: str | None, releases: dict[str, datetime], policy: str = "latest",
                   min_age_days: int = 0, now: datetime | None = None) -> str | None:
    """The version the pin should move to, or None when it should stay.

    `latest` allows any newer final release; `minor` keeps the major;
    `patch` keeps major and minor. A release younger than `min_age_days`
    is not a target, so a bad release has time to be yanked first. With no
    pin at all (a legacy install) the newest allowed release is the target.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=min_age_days)
    current = parse(pinned)
    candidates = []
    for version, when in releases.items():
        tup = parse(version)
        if tup is None or when > cutoff:
            continue
        if current is not None:
            if tup <= current:
                continue
            if policy == "minor" and tup[0] != current[0]:
                continue
            if policy == "patch" and tup[:2] != current[:2]:
                continue
        candidates.append((tup, version))
    if not candidates:
        return None
    best = max(candidates)[1]
    return best if current is None or parse(best) > current else None


def behind(pinned: str | None, latest: str | None) -> bool:
    a, b = parse(pinned), parse(latest)
    return a is None or (b is not None and b > a)
