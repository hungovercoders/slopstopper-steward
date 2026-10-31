from steward.execute import refresh


def test_set_pin_moves(tmp_path):
    f = tmp_path / "mise.toml"
    f.write_text('[tools]\n"task" = "3"\n"pipx:slopstopper-cli" = "0.24.0"\n"node" = "20"\n')
    assert refresh.set_pin(f, "0.26.0") == "moved"
    assert f.read_text() == '[tools]\n"task" = "3"\n"pipx:slopstopper-cli" = "0.26.0"\n"node" = "20"\n'
    assert refresh.set_pin(f, "0.26.0") == "unchanged"


def test_set_pin_seeds_into_existing_tools(tmp_path):
    f = tmp_path / "mise.toml"
    f.write_text('[tools]\n"task" = "3"\n')
    assert refresh.set_pin(f, "0.26.0") == "seeded"
    assert '"pipx:slopstopper-cli" = "0.26.0"' in f.read_text() and f.read_text().startswith("[tools]\n")


def test_set_pin_creates_file_or_section(tmp_path):
    f = tmp_path / "mise.toml"
    assert refresh.set_pin(f, "0.26.0") == "seeded"
    assert f.read_text() == '[tools]\n"pipx:slopstopper-cli" = "0.26.0"\n'
    g = tmp_path / "other.toml"
    g.write_text("[env]\nFOO = \"bar\"\n")
    assert refresh.set_pin(g, "0.26.0") == "seeded"
    assert g.read_text().endswith('\n[tools]\n"pipx:slopstopper-cli" = "0.26.0"\n')
