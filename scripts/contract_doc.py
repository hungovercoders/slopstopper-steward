"""Rewrite the generated block in docs/architecture/CONTRACT.md from
steward/contract.py. `--check` only reports."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steward import contract  # noqa: E402

DOC = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "CONTRACT.md"
BEGIN, END = "<!-- contract:begin -->", "<!-- contract:end -->"


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = f"{head}{BEGIN}\n{contract.table()}\n{END}{tail}"
    if "--check" in sys.argv:
        if new != text:
            print(f"✗ {DOC} is stale: run `task steward:contract-doc`")
            return 1
        print(f"✓ {DOC} matches steward/contract.py")
        return 0
    DOC.write_text(new, encoding="utf-8")
    print(f"✓ wrote {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
