"""The YAML subset `steward.yml` is written in, parsed with the stdlib.

Nested maps by indentation, `- item` lists (at the key's indent or deeper),
inline `[a, b]` lists, quoted or bare scalars, `#` comments, and a key with
nothing after it reads as null, as YAML does. Anchors, multi-line scalars
and flow maps are not supported: the config needs none of them, and a
dependency-free package is one an adopter's runner can `pip install` in
seconds. An unparseable line raises, because a silently dropped budget
is worse than a loud one.
"""

from __future__ import annotations

import re
from pathlib import Path

_KV = re.compile(r"^([A-Za-z0-9_\-]+):\s*(.*)$")
_ITEM = re.compile(r"^-(?:\s+(.*))?$")
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


class _Open:
    """A `key:` with nothing after it, waiting to learn whether a map, a
    list or nothing follows. Closed as null when nothing did."""

    def __init__(self, owner: dict, key: str, indent: int):
        self.owner, self.key, self.indent = owner, key, indent
        self.value: dict | list | None = None

    def close(self) -> None:
        if self.value is None:
            self.owner[self.key] = None


def _parse_item(stack: list, pending: _Open | None, indent: int, scalar: object, lineno: int) -> None:
    if pending is not None and pending.value is None and indent >= pending.indent:
        pending.value = pending.owner[pending.key] = [scalar]
        stack.append((indent, pending.value))
        return
    while len(stack) > 1 and (stack[-1][0] > indent or (stack[-1][0] == indent and not isinstance(stack[-1][1], list))):
        _pop(stack)
    top_indent, top = stack[-1]
    if not (isinstance(top, list) and top_indent == indent):
        raise ValueError(f"steward.yml:{lineno}: list item with no list to belong to")
    top.append(scalar)


def _pop(stack: list) -> None:
    _, node = stack.pop()
    if isinstance(node, _Open):
        node.close()


def loads(text: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    pending: _Open | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        item = _ITEM.match(body)
        if item:
            _parse_item(stack, pending, indent, _scalar(item.group(1) or ""), lineno)
            continue
        while len(stack) > 1 and stack[-1][0] >= indent:
            _pop(stack)
        parent = stack[-1][1]
        if isinstance(parent, _Open):
            if parent.value is None:
                parent.value = parent.owner[parent.key] = {}
            parent = parent.value
        kv = _KV.match(body)
        if not kv or not isinstance(parent, dict):
            raise ValueError(f"steward.yml:{lineno}: cannot parse {raw!r}")
        key, value = kv.group(1), kv.group(2)
        if value == "":
            pending = _Open(parent, key, indent)
            parent[key] = None
            stack.append((indent, pending))
            continue
        pending = None
        parent[key] = _scalar(value)
    while len(stack) > 1:
        _pop(stack)
    return root


def load(path: Path) -> dict:
    return loads(path.read_text(encoding="utf-8"))
