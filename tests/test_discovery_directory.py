"""Discovery: directory scanning."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from pyplugy import PluginManager

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_directory_picks_up_plugin_files(manager: PluginManager) -> None:
    # Load only the simple plugin to avoid pulling in the broken/dep ones in the same dir.
    target = FIXTURES / "plugin_simple.py"
    p = manager.load(target)
    assert p.manifest.name == "simple"
    assert manager.is_enabled("simple")


def test_load_directory_with_pattern(tmp_path: Path, manager: PluginManager) -> None:
    p1 = tmp_path / "alpha.py"
    p1.write_text(textwrap.dedent(
        """
        from pyplugy import plugin

        @plugin("alpha", version="1.0.0")
        def setup(ctx):
            pass
        """
    ))
    p2 = tmp_path / "beta.py"
    p2.write_text(textwrap.dedent(
        """
        from pyplugy import plugin

        @plugin("beta", version="1.0.0")
        def setup(ctx):
            pass
        """
    ))

    loaded = manager.load_directory(tmp_path)
    names = {p.manifest.name for p in loaded}
    assert names == {"alpha", "beta"}


def test_load_directory_recursive(tmp_path: Path, manager: PluginManager) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.py").write_text(textwrap.dedent(
        """
        from pyplugy import plugin

        @plugin("inner", version="1.0.0")
        def setup(ctx):
            pass
        """
    ))

    loaded = manager.load_directory(tmp_path, recursive=True)
    assert any(p.manifest.name == "inner" for p in loaded)


def test_load_nonexistent_directory_raises(manager: PluginManager) -> None:
    from pyplugy.exceptions import PluginLoadError

    with pytest.raises(PluginLoadError):
        manager.load_directory(Path("does-not-exist-12345"))
