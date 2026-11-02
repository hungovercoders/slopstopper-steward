"""Strip what must never reach a PR body or a run report: GitHub tokens,
and userinfo in a URL (`https://x-access-token:...@github.com`)."""

from __future__ import annotations

import re

_SECRET = re.compile(r"\bgh[opsur]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|(?<=://)[^/@\s]+(?=@)")


def redact(text: str) -> str:
    return _SECRET.sub("***", text)


# A closing keyword right before an issue reference: GitHub reads it in a PR
# body as "this PR closes that issue", whoever's text it is. Release notes
# say "closes [#585](https://github.com/o/r/issues/585)" for every fix.
_CLOSING = re.compile(
    r"\b(?P<first>[cCfFrR])(?P<rest>lose[sd]?|ix(?:e[sd])?|esolve[sd]?)"
    r"(?=:?\s+(?:\[?(?:#|GH-)\d|https?://github\.com/\S+?/(?:issues|pull)/\d|[\w.-]+/[\w.-]+#\d))")


def unlink(text: str) -> str:
    """Break the keyword, not the reference: `closes` becomes `c<!-- -->loses`,
    which renders the same and links nothing. For text the steward quotes
    (release notes, the agent's prose), never for its own `Closes #N`."""
    return _CLOSING.sub(lambda m: f"{m.group('first')}<!-- -->{m.group('rest')}", text)
