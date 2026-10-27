from steward import yamlish

SAMPLE = """
# comment
orgs:
  - hungovercoders   # trailing
  - "other org"
repos:
  include: []
  exclude: [a/b, c/d]
mode: pr
upgrade:
  policy: latest
  min_age_days: 2
agent:
  enabled: false
  model: ""
labels:
  ignore: steward:ignore
"""


def test_parses_nested_maps_lists_and_scalars():
    data = yamlish.loads(SAMPLE)
    assert data["orgs"] == ["hungovercoders", "other org"]
    assert data["repos"] == {"include": [], "exclude": ["a/b", "c/d"]}
    assert data["upgrade"] == {"policy": "latest", "min_age_days": 2}
    assert data["agent"] == {"enabled": False, "model": ""}
    assert data["labels"]["ignore"] == "steward:ignore"


def test_unparseable_line_raises():
    import pytest
    with pytest.raises(ValueError):
        yamlish.loads("orgs:\n  - a\nnot a key\n")
