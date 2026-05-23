"""A minimal decorator-form plugin used by discovery tests."""

from __future__ import annotations

from pyplugy import PluginContext, plugin


@plugin("simple", version="1.0.0", description="Simple test plugin")
def setup(ctx: PluginContext) -> None:
    @ctx.before("simple-target")
    def noop(*_args: object, **_kwargs: object) -> None:
        return None
