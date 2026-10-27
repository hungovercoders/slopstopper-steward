import pytest

from steward import config


def test_defaults_and_overrides(tmp_path):
    p = tmp_path / "steward.yml"
    p.write_text("orgs: [acme]\nbudgets:\n  prs_per_repo: 1\nrepos:\n  exclude: [acme/skip]\n")
    cfg = config.load(p)
    assert cfg.orgs == ["acme"] and cfg.prs_per_repo == 1 and cfg.repos_per_run == 5
    assert cfg.allows("acme/x") and not cfg.allows("acme/skip")
    assert cfg.pr_labels == ["slopstopper", "steward"]


def test_include_narrows():
    cfg = config.from_dict({"orgs": ["acme"], "repos": {"include": ["acme/a"]}})
    assert cfg.allows("acme/a") and not cfg.allows("acme/b")


@pytest.mark.parametrize("bad", [{}, {"orgs": []}, {"orgs": ["a"], "mode": "yolo"}, {"orgs": ["a"], "upgrade": {"policy": "major"}}])
def test_rejects_bad_config(bad):
    with pytest.raises(ValueError):
        config.from_dict(bad)


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path / "nope.yml")
