"""Shared fixtures for the pyPlugy test suite."""

from __future__ import annotations

from typing import Any

import pytest
from pyhooky import HookRegistry

from pyplugy import PluginManager


class FakeTaskHandle:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeTask:
    """Minimal pyWorkflowy-like task object satisfying :class:`TaskProtocol`."""

    def __init__(self, fn: Any, **opts: Any) -> None:
        self.fn = fn
        self.name = getattr(fn, "__name__", repr(fn))
        self.opts = opts
        # pyworkflowy.Task surface that bind_tasks reads.
        self.triggers: tuple[str, ...] = tuple(opts.get("triggers", ()))
        self.payload_map: tuple[tuple[str, str], ...] = tuple(opts.get("payload_map", ()))

    def submit(self, *args: Any, **kwargs: Any) -> FakeTaskHandle:
        return FakeTaskHandle(self.name)


class FakeTasky:
    """Stand-in for the real ``pyworkflowy`` module, used in tests."""

    def __init__(self) -> None:
        self.registered: list[FakeTask] = []

    def task(self, *args: Any, **kwargs: Any) -> Any:
        # Bare form: `@fake_tasky.task` directly on the function.
        if len(args) == 1 and callable(args[0]) and not kwargs:
            t = FakeTask(args[0])
            self.registered.append(t)
            return t

        # Parameterised: `@fake_tasky.task(retries=3)`
        def decorator(fn: Any) -> FakeTask:
            t = FakeTask(fn, **kwargs)
            self.registered.append(t)
            return t

        return decorator


class _FakeJob:
    __slots__ = ("task", "trigger")

    def __init__(self, task: Any, *, trigger: str | None = None) -> None:
        self.task = task
        self.trigger = trigger


class FakeScheduler:
    def __init__(self) -> None:
        self.started = False
        self.calls: list[tuple[str, Any]] = []
        self.bound: list[Any] = []
        self.cancelled: list[Any] = []

    def every(self, interval: Any) -> Any:
        self.calls.append(("every", interval))
        return self

    def cron(self, expression: str) -> Any:
        self.calls.append(("cron", expression))
        return self

    def do(self, task: Any) -> Any:
        self.calls.append(("do", task))
        return _FakeJob(task)

    def bind_tasks(self, *tasks: Any) -> list[Any]:
        jobs: list[_FakeJob] = []
        for t in tasks:
            for trigger in getattr(t, "triggers", ()) or ():
                job = _FakeJob(t, trigger=trigger)
                jobs.append(job)
                self.bound.append(job)
        return jobs

    def cancel(self, job: Any) -> bool:
        self.cancelled.append(job)
        return True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


@pytest.fixture
def registry() -> HookRegistry:
    """A fresh isolated registry — never share with module-level pyHooky default."""
    return HookRegistry(name="test")


@pytest.fixture
def fake_tasky() -> FakeTasky:
    return FakeTasky()


@pytest.fixture
def fake_scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
def manager(registry: HookRegistry) -> PluginManager:
    return PluginManager(registry=registry)


@pytest.fixture
def manager_with_tasks(
    registry: HookRegistry, fake_tasky: FakeTasky, fake_scheduler: FakeScheduler
) -> PluginManager:
    return PluginManager(registry=registry, tasky=fake_tasky, scheduler=fake_scheduler)
