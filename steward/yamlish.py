"""The YAML subset `steward.yml` is written in, parsed with the stdlib.

Nested maps by two-space indentation, `- item` lists, inline `[a, b]`
lists, quoted or bare scalars, `#` comments. Anchors, multi-line scalars
and flow maps are not supported: the config needs none of them, and a
dependency-free package is one an adopter's runner can `pip install` in
seconds. An unparseable line raises, because a silently dropped budget
is worse than a loud one.
"""

from __future__ import annotations

import re
from pathlib import Path

_KV = re.compile(r"^([A-Za-z0-9_\-]+):\s*(.*)$")
_ITEM = re.compile(r"^-\s*(.*)$")
_KEYWORDS = {"": None, "null": None, "~": None, "true": True, "false": False}


def _scalar(raw: str) -> object:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    low = raw.lower()
    if low in _KEYWORDS:
        return _KEYWORDS[low]
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def loads(text: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    pending_key: tuple[dict, str] | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        item = _ITEM.match(body)
        if item and (pending_key or isinstance(parent, list)):
            if pending_key:
                owner, key = pending_key
                owner[key] = parent = []
                stack.append((indent - 1, parent))
                pending_key = None
            if not isinstance(parent, list):
                raise ValueError(f"steward.yml:{lineno}: list item under a non-list")
            parent.append(_scalar(item.group(1)))
            continue
        kv = _KV.match(body)
        if not kv or not isinstance(parent, dict):
            raise ValueError(f"steward.yml:{lineno}: cannot parse {raw!r}")
        key, value = kv.group(1), kv.group(2)
        if value == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            pending_key = (parent, key)
            continue
        pending_key = None
        parent[key] = _scalar(value)
    return root


def load(path: Path) -> dict:
    return loads(path.read_text(encoding="utf-8"))
