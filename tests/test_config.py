import pytest

from usage_watch import config
from usage_watch.errors import Problem

OLD_STYLE = """
[defaults]
nudge = "continue"
interval = 120
min_remaining = 5
max_strikes = 3

[accounts.omp]
claude = "claude@1a2b3c4d"

[roles.orchestrator]
nudge = "Usage limit cleared. Continue."

[[override]]
title = "scratch experiment"
ignore = true
"""


def test_old_style_config_loads_and_ignores_nudge_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_STYLE)
    cfg = config.load(path)
    assert cfg.exists and cfg.path == path
    assert cfg.interval == 120
    assert not hasattr(cfg, "nudge")


def test_missing_config_uses_defaults(tmp_path):
    cfg = config.load(tmp_path / "absent.toml")
    assert not cfg.exists
    assert cfg.interval == 300


def test_bad_toml_is_a_prompt(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[defaults\ninterval = ")
    with pytest.raises(Problem) as e:
        config.load(path)
    assert "not valid TOML" in e.value.what
    assert e.value.fix
    assert "fix:" in e.value.render()
