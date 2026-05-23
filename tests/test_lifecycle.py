"""Lifecycle transitions: load -> enable -> disable -> enable -> unload."""

from __future__ import annotations

import pytest
from pyhooky import on

from pyplugy import (
    HOOK_PLUGIN_DISABLE,
    HOOK_PLUGIN_ENABLE,
    HOOK_PLUGIN_LOAD,
    HOOK_PLUGIN_UNLOAD,
    Plugin,
    PluginContext,
    PluginLoadError,
    PluginManager,
    PluginState,
)


class _Counter(Plugin):
    name = "lc"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def on_load(self, ctx: PluginContext) -> None:
        self.calls.append("on_load")

        @ctx.before("lc-target")
        def stub() -> None:
            return None

    def on_unload(self, ctx: PluginContext) -> None:
        self.calls.append("on_unload")

    def on_enable(self, ctx: PluginContext) -> None:
        self.calls.append("on_enable")

    def on_disable(self, ctx: PluginContext) -> None:
        self.calls.append("on_disable")


def test_full_cycle_fires_each_hook(manager: PluginManager) -> None:
    seen: list[str] = []

    for name, target in [
        ("load", HOOK_PLUGIN_LOAD),
        ("enable", HOOK_PLUGIN_ENABLE),
        ("disable", HOOK_PLUGIN_DISABLE),
        ("unload", HOOK_PLUGIN_UNLOAD),
    ]:
        on(target, lambda _p, name=name: seen.append(name), registry=manager.registry)

    plugin = _Counter()
    manager.load(plugin)
    assert manager.state("lc") == PluginState.ENABLED

    manager.disable("lc")
    assert manager.state("lc") == PluginState.DISABLED

    manager.enable("lc")
    assert manager.state("lc") == PluginState.ENABLED

    manager.unload("lc")
    assert "lc" not in [info.name for info in manager.list_plugins()]

    # unload first disables the still-ENABLED plugin, hence the second 'disable'.
    assert seen == ["load", "enable", "disable", "enable", "disable", "unload"]
    # on_load runs twice (initial + re-load on enable), on_enable twice, on_disable twice.
    assert plugin.calls.count("on_load") == 2
    assert plugin.calls.count("on_enable") == 2
    assert plugin.calls.count("on_disable") == 2
    assert plugin.calls.count("on_unload") == 1


def test_unload_clears_plugin_tag(manager: PluginManager) -> None:
    plugin = _Counter()
    manager.load(plugin)
    assert "lc-target" in manager.plugin_targets("lc")
    manager.unload("lc")
    # No hook tagged `lc` should remain.
    assert all(h.tag != "lc" for _t, h in manager.registry.all_hooks())


def test_disable_tears_down_hooks_re_enable_restores(manager: PluginManager) -> None:
    plugin = _Counter()
    manager.load(plugin)
    assert "lc-target" in manager.plugin_targets("lc")

    manager.disable("lc")
    assert manager.plugin_targets("lc") == []

    manager.enable("lc")
    assert "lc-target" in manager.plugin_targets("lc")


def test_reload_returns_same_instance(manager: PluginManager) -> None:
    plugin = _Counter()
    manager.load(plugin)
    again = manager.reload("lc")
    assert again is plugin
    assert manager.state("lc") == PluginState.ENABLED


def test_failing_on_load_raises_and_does_not_register(manager: PluginManager) -> None:
    class _Bad(Plugin):
        name = "bad"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            raise RuntimeError("nope")

    with pytest.raises(PluginLoadError):
        manager.load(_Bad)

    assert "bad" not in [info.name for info in manager.list_plugins()]
    assert all(h.tag != "bad" for _t, h in manager.registry.all_hooks())
