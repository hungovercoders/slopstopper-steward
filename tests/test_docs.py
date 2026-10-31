"""The contract table in the architecture doc is generated from
steward/contract.py; this fails while it is stale (`task steward:contract-doc`)."""

from pathlib import Path

from steward import contract

DOC = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "CONTRACT.md"


def test_contract_doc_carries_the_generated_table():
    assert contract.table() in DOC.read_text(encoding="utf-8")
