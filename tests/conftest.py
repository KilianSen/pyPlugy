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
    """Minimal pyTasky-like task object satisfying :class:`TaskProtocol`."""

    def __init__(self, fn: Any, **opts: Any) -> None:
        self.fn = fn
        self.name = getattr(fn, "__name__", repr(fn))
        self.opts = opts

    def submit(self, *args: Any, **kwargs: Any) -> FakeTaskHandle:
        return FakeTaskHandle(self.name)


class FakeTasky:
    """Stand-in for the real ``pytasky`` module, used in tests."""

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


class FakeScheduler:
    def __init__(self) -> None:
        self.started = False
        self.calls: list[tuple[str, Any]] = []

    def every(self, spec: str) -> Any:
        self.calls.append(("every", spec))
        return self

    def cron(self, spec: str) -> Any:
        self.calls.append(("cron", spec))
        return self

    def do(self, task: Any) -> Any:
        self.calls.append(("do", task))
        return self

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
