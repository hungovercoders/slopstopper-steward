"""The `pipx:slopstopper-cli` pin in `mise.toml`.

`tomllib` (stdlib) is the reader of record, so every valid TOML spelling
is understood: either quote style, an inline table with `version`, an
array. A rewrite changes only the version string's own span, keeping the
file's quoting, and the result is parsed again before it is written.
Anything the steward cannot rewrite exactly (an array, an invalid file, a
`tools` table it cannot extend) raises `PinError` rather than appending a
second key that would break every mise-based workflow.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PIN_KEY = "pipx:slopstopper-cli"
_K = re.escape(PIN_KEY)
# The pin line: `lead` up to the value, then a quoted version (`v`) or an
# inline table carrying one (`v2`). Bare keys are invalid TOML but are read
# for tolerance when the file does not parse.
PIN_LINE = re.compile(
    rf"""^(?P<lead>[ \t]*(?:"{_K}"|'{_K}'|{_K})[ \t]*=[ \t]*)"""
    rf"""(?:(?P<q>["'])(?P<v>[^"'\n]*)(?P=q)"""
    rf"""|\{{[^}}\n]*?\bversion[ \t]*=[ \t]*(?P<q2>["'])(?P<v2>[^"'\n]*)(?P=q2)[^}}\n]*\}})""",
    re.M,
)
_TOOLS_HEADER = re.compile(r"^\[tools\][ \t]*(?:#[^\n]*)?(?:\n|\Z)", re.M)


class PinError(RuntimeError):
    """The pin is written in a form the steward will not rewrite."""


def _version_of(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("version")
    elif isinstance(value, list):
        value = _version_of(value[0]) if value else None
    return str(value).strip() or None if isinstance(value, (str, int, float)) else None


def read_pin(text: str | None) -> str | None:
    if not text:
        return None
    try:
        tools = tomllib.loads(text).get("tools") or {}
    except tomllib.TOMLDecodeError:
        match = PIN_LINE.search(text)
        return (match.group("v") if match.group("v") is not None else match.group("v2")).strip() or None if match else None
    return _version_of(tools.get(PIN_KEY)) if isinstance(tools, dict) else None


def _seed(text: str, version: str) -> str:
    line = f'"{PIN_KEY}" = "{version}"\n'
    if not text:
        return f"[tools]\n{line}"
    header = _TOOLS_HEADER.search(text)
    if header:
        head = text[:header.end()]
        return (head if head.endswith("\n") else head + "\n") + line + text[header.end():]
    return text.rstrip("\n") + f"\n\n[tools]\n{line}"


def set_pin(path: Path, version: str) -> str:
    """Rewrite (or seed) the pin. Returns 'moved', 'seeded' or 'unchanged'."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        tools = tomllib.loads(text).get("tools") if text else None
    except tomllib.TOMLDecodeError as exc:
        raise PinError(f"{path.name} is not valid TOML ({exc}); the steward will not edit it") from exc
    if isinstance(tools, dict) and PIN_KEY in tools:
        if _version_of(tools[PIN_KEY]) == version:
            return "unchanged"
        match = PIN_LINE.search(text)
        if not match or isinstance(tools[PIN_KEY], list):
            raise PinError(f"the {PIN_KEY} entry in {path.name} is written in a form the steward does not rewrite "
                           "(an array, or a table on its own lines); move the pin by hand")
        group = "v" if match.group("v") is not None else "v2"
        new, status = text[:match.start(group)] + version + text[match.end(group):], "moved"
    else:
        new, status = _seed(text, version), "seeded"
    try:
        ok = read_pin(new) == version and tomllib.loads(new) is not None
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise PinError(f"rewriting the pin in {path.name} would not leave valid TOML pinning {version}; move it by hand")
    path.write_text(new, encoding="utf-8")
    return status
