"""PluginContext task wiring against the mock pyWorkflowy protocol."""

from __future__ import annotations

import pytest

from pyplugy import Plugin, PluginContext, PluginManager, plugin


def test_ctx_task_without_tasky_raises(manager: PluginManager) -> None:
    @plugin("notasks", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        with pytest.raises(RuntimeError, match="pyplugy\\[tasks\\]"):

            @ctx.task
            def t() -> None:
                pass

    manager.load(setup)


def test_ctx_task_bare_decorator(manager_with_tasks: PluginManager, fake_tasky) -> None:
    @plugin("has-task", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task
        def cleanup() -> None:
            return None

    manager_with_tasks.load(setup)
    assert any(t.name == "cleanup" for t in fake_tasky.registered)
    assert [i.task.name for i in manager_with_tasks.plugin_tasks("has-task")] == ["cleanup"]


def test_ctx_task_with_kwargs(manager_with_tasks: PluginManager, fake_tasky) -> None:
    @plugin("kwargs-task", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(retries=3, timeout=10)
        def t() -> None:
            return None

    manager_with_tasks.load(setup)
    t = next(t for t in fake_tasky.registered if t.name == "t")
    assert t.opts == {"retries": 3, "timeout": 10}


def test_ctx_tasks_cleared_on_disable(manager_with_tasks: PluginManager) -> None:
    @plugin("toggle-tasks", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task
        def t() -> None:
            return None

    manager_with_tasks.load(setup)
    assert [i.task.name for i in manager_with_tasks.plugin_tasks("toggle-tasks")] == ["t"]
    manager_with_tasks.disable("toggle-tasks")
    assert manager_with_tasks.plugin_tasks("toggle-tasks") == ()


def test_ctx_task_metadata_passthrough(manager_with_tasks: PluginManager) -> None:
    @plugin("meta-task", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task(metadata={"category": "reactive", "priority": 5})
        def t() -> None:
            return None

    manager_with_tasks.load(setup)
    infos = manager_with_tasks.plugin_tasks("meta-task")
    assert len(infos) == 1
    assert infos[0].task.name == "t"
    assert dict(infos[0].metadata) == {"category": "reactive", "priority": 5}


def test_ctx_task_default_metadata_is_empty(manager_with_tasks: PluginManager) -> None:
    @plugin("nometa", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        @ctx.task
        def t() -> None: ...

    manager_with_tasks.load(setup)
    info = manager_with_tasks.plugin_tasks("nometa")[0]
    assert dict(info.metadata) == {}


def test_scheduler_passed_through(manager_with_tasks: PluginManager, fake_scheduler) -> None:
    seen: dict[str, object] = {}

    class _P(Plugin):
        name = "sched-user"
        version = "1.0.0"

        def on_load(self, ctx: PluginContext) -> None:
            seen["scheduler"] = ctx.scheduler

    manager_with_tasks.load(_P)
    assert seen["scheduler"] is fake_scheduler
