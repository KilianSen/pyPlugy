"""Discovery: JIT — Plugin instance / class / module / dotted path / Path."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from pyplugy import Plugin, PluginContext, PluginManager, plugin
from pyplugy.exceptions import PluginLoadError


def test_jit_load_instance(manager: PluginManager) -> None:
    @plugin("inst", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        pass

    p = manager.load(setup)
    assert p is setup


def test_jit_load_subclass(manager: PluginManager) -> None:
    class P(Plugin):
        name = "sub"
        version = "1.0.0"

    loaded = manager.load(P)
    assert isinstance(loaded, P)


def test_jit_load_module(manager: PluginManager) -> None:
    mod = importlib.import_module("tests.fixtures.plugin_simple")
    loaded = manager.load(mod)
    assert loaded.manifest.name == "simple"


def test_jit_load_dotted_string(manager: PluginManager) -> None:
    loaded = manager.load("tests.fixtures.plugin_simple")
    assert loaded.manifest.name == "simple"


def test_jit_load_path(manager: PluginManager) -> None:
    file = Path(__file__).parent / "fixtures" / "plugin_simple.py"
    loaded = manager.load(file)
    assert loaded.manifest.name == "simple"


def test_jit_load_unknown_type_raises(manager: PluginManager) -> None:
    with pytest.raises(PluginLoadError):
        manager.load(42)  # type: ignore[arg-type]


def test_jit_load_empty_module_raises(manager: PluginManager, tmp_path: Path) -> None:
    f = tmp_path / "empty_mod.py"
    f.write_text("# nothing here\n")
    with pytest.raises(PluginLoadError, match="no plugins"):
        manager.load(f)
