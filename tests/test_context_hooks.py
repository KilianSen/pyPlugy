"""PluginContext hook passthroughs and tag auto-application."""

from __future__ import annotations

from pyhooky import hook

from pyplugy import (
    Plugin,
    PluginContext,
    PluginManager,
    current_context,
    current_manager,
    plugin,
)


def test_ctx_before_after_get_tagged_with_plugin_name(manager: PluginManager) -> None:
    @plugin("tagger", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.before("t-target")
        def before_hook() -> None:
            pass

        @ctx.after("t-target")
        def after_hook(_r: object) -> None:
            pass

    manager.load(setup)
    tags = {h.tag for _t, h in manager.registry.all_hooks() if h.tag is not None}
    assert "tagger" in tags
    # Both hooks tagged
    tagged = manager.registry.hooks_by_tag("tagger")
    assert len(tagged) == 2


def test_listener_triggered_when_enabled_silent_when_disabled(manager: PluginManager) -> None:
    seen: list[str] = []

    @plugin("listener", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.on("evt")
        def on_evt(value: str) -> None:
            seen.append(value)

    manager.load(setup)
    manager.registry.trigger("evt", "one")
    assert seen == ["one"]

    manager.disable("listener")
    manager.registry.trigger("evt", "two")  # no listener registered now
    assert seen == ["one"]

    manager.enable("listener")
    manager.registry.trigger("evt", "three")
    assert seen == ["one", "three"]


def test_around_hook_via_ctx(manager: PluginManager) -> None:
    @hook("around-target", registry=manager.registry)
    def target(x: int) -> int:
        return x * 2

    @plugin("around-plugin", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.around("around-target")
        def add_one(inner, x: int) -> int:
            return inner(x) + 1

    manager.load(setup)
    assert target(5) == 11  # (5*2)+1


def test_current_context_and_manager_inside_on_load(manager: PluginManager) -> None:
    captured: dict[str, object] = {}

    class _P(Plugin):
        name = "introspect"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            captured["ctx"] = current_context()
            captured["manager"] = current_manager()

    manager.load(_P)
    assert captured["ctx"] is not None
    assert captured["manager"] is manager


def test_ctx_logger_named_per_plugin(manager: PluginManager) -> None:
    @plugin("logged", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        assert ctx.logger.name == "pyplugy.logged"

    manager.load(setup)
