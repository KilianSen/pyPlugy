"""Core PluginManager surface: load, get, list_plugins, dump, repr."""

from __future__ import annotations

import pytest

from pyplugy import (
    HOOK_PLUGIN_ENABLE,
    HOOK_PLUGIN_LOAD,
    Plugin,
    PluginAlreadyLoadedError,
    PluginContext,
    PluginManager,
    PluginNotFoundError,
    PluginState,
    plugin,
)


@plugin("a", version="1.0.0")
def _a(ctx: PluginContext) -> None:
    @ctx.before("target-a")
    def hook() -> None:
        return None


class _B(Plugin):
    name = "b"
    version = "2.0.0"

    def on_load(self, ctx: PluginContext) -> None:
        @ctx.on("event-b")
        def listener() -> None:
            return None


def test_load_returns_plugin_and_marks_enabled(manager: PluginManager) -> None:
    p = manager.load(_a)
    assert p is _a
    assert manager.is_enabled("a")
    assert manager.state("a") == PluginState.ENABLED


def test_load_without_enable_lands_in_loaded_state(manager: PluginManager) -> None:
    manager.load(_a, enable=False)
    assert manager.state("a") == PluginState.LOADED
    assert not manager.is_enabled("a")


def test_load_class_form_via_subclass(manager: PluginManager) -> None:
    p = manager.load(_B)
    assert isinstance(p, _B)
    assert manager.is_enabled("b")


def test_get_returns_loaded_plugin(manager: PluginManager) -> None:
    manager.load(_a)
    assert manager.get("a") is _a


def test_get_unknown_raises(manager: PluginManager) -> None:
    with pytest.raises(PluginNotFoundError):
        manager.get("missing")


def test_duplicate_load_raises(manager: PluginManager) -> None:
    manager.load(_a)
    with pytest.raises(PluginAlreadyLoadedError):
        manager.load(_a)


def test_list_plugins_returns_info_per_plugin(manager: PluginManager) -> None:
    manager.load(_a)
    manager.load(_B)
    infos = {info.name: info for info in manager.list_plugins()}
    assert set(infos) == {"a", "b"}
    assert infos["a"].state == PluginState.ENABLED
    assert "target-a" in infos["a"].targets


def test_dump_returns_structured_snapshot(manager: PluginManager) -> None:
    manager.load(_a)
    snap = manager.dump()
    assert snap["registry"] == manager.registry.name
    assert any(p["name"] == "a" for p in snap["plugins"])


def test_repr_summarises_counts(manager: PluginManager) -> None:
    manager.load(_a)
    text = repr(manager)
    assert "plugins=1" in text
    assert "enabled=1" in text


def test_manager_level_hooks_fire(manager: PluginManager) -> None:
    seen: list[tuple[str, str]] = []

    from pyhooky import on

    @on(HOOK_PLUGIN_LOAD, registry=manager.registry)
    def on_load(plugin_obj: Plugin) -> None:
        seen.append(("load", plugin_obj.manifest.name))

    @on(HOOK_PLUGIN_ENABLE, registry=manager.registry)
    def on_enable(plugin_obj: Plugin) -> None:
        seen.append(("enable", plugin_obj.manifest.name))

    manager.load(_a)
    assert ("load", "a") in seen
    assert ("enable", "a") in seen
