"""Strip what must never reach a PR body or a run report: GitHub tokens,
and userinfo in a URL (`https://x-access-token:...@github.com`)."""

from __future__ import annotations

import re

_SECRET = re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|(?<=://)[^/@\s]+(?=@)")


def redact(text: str) -> str:
    return _SECRET.sub("***", text)
