"""End-to-end integration: real pyHooky + real pyTasky behind PluginManager.

These tests intentionally bypass the mock TaskyProtocol used elsewhere and
drive the actual ``pytasky`` package, proving the protocol surface pyPlugy
declares matches reality.
"""

from __future__ import annotations

from typing import Any

import pytasky
from pytasky import Task, TaskRunner
from pytasky.schedule import Scheduler

from pyplugy import Plugin, PluginContext, PluginManager, plugin


def test_plugin_registers_real_pytasky_task() -> None:
    manager = PluginManager(tasky=pytasky)
    captured: dict[str, Task[Any]] = {}

    @plugin("calc", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(name="double")
        def double(x: int) -> int:
            return x * 2

        captured["double"] = double

    manager.load(setup)
    assert manager.plugin_tasks("calc") == ["double"]
    assert isinstance(captured["double"], Task)

    with TaskRunner() as runner:
        handle = captured["double"].submit(21)
        results = runner.run()

    assert results[handle.name].value == 42


def test_plugin_real_task_with_retry_and_dependencies() -> None:
    manager = PluginManager(tasky=pytasky)
    attempts: dict[str, int] = {"flaky": 0}
    captured: dict[str, Task[Any]] = {}

    @plugin("workflow", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(name="seed")
        def seed() -> int:
            return 10

        @ctx.task(name="flaky", retries=2)
        def flaky(x: int) -> int:
            attempts["flaky"] += 1
            if attempts["flaky"] < 2:
                raise RuntimeError("transient")
            return x + 5

        captured["seed"] = seed
        captured["flaky"] = flaky

    manager.load(setup)
    assert manager.plugin_tasks("workflow") == ["seed", "flaky"]

    with TaskRunner() as runner:
        seed_handle = captured["seed"].submit()
        flaky_handle = captured["flaky"].submit(15, depends_on=[seed_handle])
        results = runner.run()

    assert results[seed_handle.name].value == 10
    assert results[flaky_handle.name].value == 20
    assert attempts["flaky"] == 2


def test_plugin_with_real_scheduler() -> None:
    scheduler = Scheduler()
    manager = PluginManager(tasky=pytasky, scheduler=scheduler)

    @plugin("scheduled", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(name="beat")
        def beat() -> str:
            return "tick"

        assert ctx.scheduler is scheduler
        ctx.scheduler.every(60).do(beat)

    manager.load(setup)
    assert len(scheduler.jobs()) == 1


def test_unload_clears_plugin_tasks() -> None:
    manager = PluginManager(tasky=pytasky)

    @plugin("ephemeral", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(name="work")
        def work() -> None:
            return None

    manager.load(setup)
    assert manager.plugin_tasks("ephemeral") == ["work"]

    manager.unload("ephemeral")
    assert "ephemeral" not in [info.name for info in manager.list_plugins()]


def test_class_plugin_with_real_pytasky() -> None:
    manager = PluginManager(tasky=pytasky)
    captured: dict[str, Task[Any]] = {}

    class WorkerPlugin(Plugin):
        name = "worker"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            @ctx.task(name="run")
            async def run() -> int:
                return 7

            captured["run"] = run

    manager.load(WorkerPlugin)
    assert manager.plugin_tasks("worker") == ["run"]
    assert isinstance(captured["run"], Task)
    assert captured["run"].is_async is True


def test_unload_clears_hooks_alongside_tasks() -> None:
    """The headline integration win: one unload tears down hooks AND tasks."""
    from pyhooky import HookRegistry

    registry = HookRegistry(name="integration")
    manager = PluginManager(registry=registry, tasky=pytasky)

    @plugin("bundled", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.before("some-target")
        def my_hook(*args: Any, **kwargs: Any) -> None:
            return None

        @ctx.task(name="my-task")
        def t() -> int:
            return 1

    manager.load(setup)
    assert manager.plugin_targets("bundled") == ["some-target"]
    assert manager.plugin_tasks("bundled") == ["my-task"]

    manager.unload("bundled")
    assert manager.plugin_targets.__self__ is manager  # sanity: method bound
    # All hooks tagged with the plugin name are gone:
    assert registry.hooks_by_tag("bundled") == []
