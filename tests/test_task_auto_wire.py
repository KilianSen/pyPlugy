"""Manager auto-wires task triggers to the scheduler on enable; tears down on disable."""

from __future__ import annotations

from pyplugy import PluginContext, PluginManager, plugin


def test_triggers_bound_on_enable(manager_with_tasks: PluginManager, fake_scheduler) -> None:
    @plugin("evented", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(triggers=("book.added",), payload_map=(("book_id", "book_id"),))
        def handle(book_id: str) -> str:
            return book_id

    manager_with_tasks.load(setup)

    assert len(fake_scheduler.bound) == 1
    assert fake_scheduler.bound[0].trigger == "book.added"


def test_jobs_cancelled_on_disable(manager_with_tasks: PluginManager, fake_scheduler) -> None:
    @plugin("evented2", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(triggers=("x",))
        def h() -> None: ...

    manager_with_tasks.load(setup)
    bound_job = fake_scheduler.bound[0]

    manager_with_tasks.disable("evented2")

    assert bound_job in fake_scheduler.cancelled


def test_jobs_recreated_on_re_enable(manager_with_tasks: PluginManager, fake_scheduler) -> None:
    @plugin("evented3", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(triggers=("y",))
        def h() -> None: ...

    manager_with_tasks.load(setup)
    assert len(fake_scheduler.bound) == 1

    manager_with_tasks.disable("evented3")
    manager_with_tasks.enable("evented3")

    assert len(fake_scheduler.bound) == 2


def test_no_scheduler_is_noop(registry, fake_tasky) -> None:
    """Plugins with triggered tasks still load when scheduler is None — silently."""
    manager = PluginManager(registry=registry, tasky=fake_tasky, scheduler=None)

    @plugin("noscheduler", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(triggers=("nope",))
        def h() -> None: ...

    manager.load(setup)

    infos = manager.plugin_tasks("noscheduler")
    assert len(infos) == 1


def test_task_without_triggers_does_not_bind(
    manager_with_tasks: PluginManager, fake_scheduler
) -> None:
    @plugin("plain", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task
        def cleanup() -> None: ...

    manager_with_tasks.load(setup)
    assert fake_scheduler.bound == []


async def test_async_lifecycle_auto_wires(
    manager_with_tasks: PluginManager, fake_scheduler
) -> None:
    @plugin("async-evented", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(triggers=("z",))
        async def h() -> None: ...

    await manager_with_tasks.aload(setup)

    assert len(fake_scheduler.bound) == 1

    await manager_with_tasks.adisable("async-evented")
    assert len(fake_scheduler.cancelled) == 1
