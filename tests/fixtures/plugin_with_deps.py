"""Class-form plugin with a dependency — exercised by dep / discovery tests."""

from __future__ import annotations

from pyplugy import Plugin, PluginContext


class WithDeps(Plugin):
    name = "with-deps"
    version = "0.5.0"
    requires = ("simple>=1.0",)
    description = "Depends on simple"

    def on_load(self, ctx: PluginContext) -> None:
        @ctx.on("with-deps:event")
        def listener(*_a: object, **_kw: object) -> None:
            return None
