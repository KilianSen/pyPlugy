"""Plugin whose ``on_load`` always raises — used by the error-path tests."""

from __future__ import annotations

from pyplugy import Plugin, PluginContext


class BrokenPlugin(Plugin):
    name = "broken"
    version = "1.0.0"

    def on_load(self, ctx: PluginContext) -> None:
        raise RuntimeError("intentional explosion")
