"""Async-flavoured plugin to confirm pyHooky's async helpers wire correctly."""

from __future__ import annotations

from pyplugy import Plugin, PluginContext


class AsyncPlugin(Plugin):
    name = "async-plugin"
    version = "1.0.0"

    def on_load(self, ctx: PluginContext) -> None:
        @ctx.on("async:event")
        async def listener(*_a: object, **_kw: object) -> str:
            return "ok"
