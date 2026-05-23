"""``PluginManager(context_class=...)`` — host-provided PluginContext subclasses."""

from __future__ import annotations

import functools
from typing import Any

import pytest

from pyplugy import Plugin, PluginContext, PluginManager


class _HostContext(PluginContext):
    """Subclass that attaches host-specific surface plugins can rely on."""

    __slots__ = ("app", "event_bus")

    def __init__(
        self,
        *args: Any,
        app: object,
        event_bus: object,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.app = app
        self.event_bus = event_bus


def test_context_class_is_instantiated_and_attrs_accessible(registry: Any) -> None:
    sentinel_app = object()
    sentinel_bus = object()
    captured: dict[str, Any] = {}

    class _Probe(Plugin):
        name = "probe"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            captured["ctx_class"] = type(ctx).__name__
            captured["app"] = getattr(ctx, "app", None)
            captured["bus"] = getattr(ctx, "event_bus", None)
            # Inherited surface still works.
            captured["name"] = ctx.name

    manager = PluginManager(
        registry=registry,
        context_class=functools.partial(_HostContext, app=sentinel_app, event_bus=sentinel_bus),
    )
    manager.load(_Probe)

    assert captured["ctx_class"] == "_HostContext"
    assert captured["app"] is sentinel_app
    assert captured["bus"] is sentinel_bus
    assert captured["name"] == "probe"


def test_default_context_class_is_plugin_context(registry: Any) -> None:
    captured: dict[str, Any] = {}

    class _Probe(Plugin):
        name = "probe"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            captured["ctx_class"] = type(ctx).__name__

    manager = PluginManager(registry=registry)
    manager.load(_Probe)
    assert captured["ctx_class"] == "PluginContext"


def test_context_class_carries_through_disable_enable_cycle(registry: Any) -> None:
    """``_do_enable`` rebuilds ctx on DISABLED→ENABLED — the subclass must be used again."""
    seen: list[str] = []

    class _Probe(Plugin):
        name = "probe"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            seen.append(type(ctx).__name__)

    manager = PluginManager(
        registry=registry,
        context_class=functools.partial(_HostContext, app=object(), event_bus=object()),
    )
    manager.load(_Probe)
    manager.disable("probe")
    manager.enable("probe")
    # _Probe.on_load ran twice (initial + re-enable), both inside _HostContext.
    assert seen == ["_HostContext", "_HostContext"]


def test_context_class_works_with_async_lifecycle(registry: Any) -> None:
    pytest.importorskip("pytest_asyncio")
    captured: dict[str, Any] = {}

    class _AsyncProbe(Plugin):
        name = "probe-async"
        version = "1.0.0"

        async def on_load(self, ctx: PluginContext) -> None:
            captured["app"] = getattr(ctx, "app", None)

    async def _run() -> None:
        manager = PluginManager(
            registry=registry,
            context_class=functools.partial(_HostContext, app="the-app", event_bus="the-bus"),
        )
        await manager.aload(_AsyncProbe)

    import asyncio

    asyncio.run(_run())
    assert captured["app"] == "the-app"
