"""Async lifecycle: aload/aenable/adisable/aunload and the sync-path coroutine guard."""

from __future__ import annotations

import pytest
from pyhooky import hook, on

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
    plugin,
)


class _AsyncCycle(Plugin):
    """Plugin whose lifecycle methods are all ``async def``."""

    name = "ac"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def on_load(self, ctx: PluginContext) -> None:
        self.calls.append("on_load")

        @ctx.before("ac-target")
        def stub() -> None:
            return None

    async def on_enable(self, ctx: PluginContext) -> None:
        self.calls.append("on_enable")

    async def on_disable(self, ctx: PluginContext) -> None:
        self.calls.append("on_disable")

    async def on_unload(self, ctx: PluginContext) -> None:
        self.calls.append("on_unload")


async def test_full_async_cycle_awaits_each_lifecycle(manager: PluginManager) -> None:
    seen: list[str] = []
    for name, target in [
        ("load", HOOK_PLUGIN_LOAD),
        ("enable", HOOK_PLUGIN_ENABLE),
        ("disable", HOOK_PLUGIN_DISABLE),
        ("unload", HOOK_PLUGIN_UNLOAD),
    ]:
        on(target, lambda _p, name=name: seen.append(name), registry=manager.registry)

    p = _AsyncCycle()
    await manager.aload(p)
    assert manager.state("ac") == PluginState.ENABLED

    await manager.adisable("ac")
    assert manager.state("ac") == PluginState.DISABLED

    await manager.aenable("ac")
    assert manager.state("ac") == PluginState.ENABLED

    await manager.aunload("ac")
    assert "ac" not in [info.name for info in manager.list_plugins()]

    assert seen == ["load", "enable", "disable", "enable", "disable", "unload"]
    # on_load runs twice (initial + re-load on enable), on_enable twice, on_disable twice.
    assert p.calls.count("on_load") == 2
    assert p.calls.count("on_enable") == 2
    assert p.calls.count("on_disable") == 2
    assert p.calls.count("on_unload") == 1


async def test_aload_accepts_sync_plugins(manager: PluginManager) -> None:
    """Async API stays backward-compatible with non-async plugins."""

    @plugin("sync-via-async", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.before("sva-target")
        def stub() -> None:
            return None

    await manager.aload(setup)
    assert manager.state("sync-via-async") == PluginState.ENABLED
    assert "sva-target" in manager.plugin_targets("sync-via-async")


def test_sync_load_rejects_async_on_load(manager: PluginManager) -> None:
    """Sync load() must error rather than silently dropping the coroutine."""

    class _AsyncOnly(Plugin):
        name = "async-only"
        version = "1.0.0"

        async def on_load(self, ctx: PluginContext) -> None:  # type: ignore[override]
            return None

    with pytest.raises(PluginLoadError, match="async variant"):
        manager.load(_AsyncOnly)

    # Failed load is rolled back: no slot, no hooks tagged.
    assert "async-only" not in [info.name for info in manager.list_plugins()]
    assert all(h.tag != "async-only" for _t, h in manager.registry.all_hooks())


async def test_async_before_hook_on_async_target(manager: PluginManager) -> None:
    """Regression: async on_load may register @ctx.before(async_target) async def hook."""
    calls: list[int] = []

    @hook("evt:async-target", registry=manager.registry)
    async def target(x: int) -> int:
        return x * 2

    class _AsyncHookPlugin(Plugin):
        name = "ah"
        version = "1.0.0"

        async def on_load(self, ctx: PluginContext) -> None:
            @ctx.before("evt:async-target")
            async def pre(x: int) -> None:
                calls.append(x)

    await manager.aload(_AsyncHookPlugin)
    result = await target(7)
    assert result == 14
    assert calls == [7]


async def test_areload_round_trip(manager: PluginManager) -> None:
    p = _AsyncCycle()
    await manager.aload(p)
    again = await manager.areload("ac")
    assert again is p
    assert manager.state("ac") == PluginState.ENABLED
