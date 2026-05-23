"""Minimal :class:`typing.Protocol` surface describing the bits of pyTasky we use.

pyTasky is an optional dependency. By depending on a protocol instead of the
real package we keep imports loose, tests independent, and make it trivial to
slot in a mock during testing. Real pyTasky integration happens in a follow-up
phase once that library is published.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = [
    "SchedulerProtocol",
    "TaskHandleProtocol",
    "TaskProtocol",
    "TaskRunnerProtocol",
    "TaskyProtocol",
]


@runtime_checkable
class TaskHandleProtocol(Protocol):
    """Opaque handle returned by ``Task.submit`` / ``TaskRunner.submit``.

    pyPlugy never inspects this — it only stores references and (eventually)
    forwards them as ``depends_on`` arguments.
    """

    @property
    def name(self) -> str:  # pragma: no cover - protocol stub
        ...


@runtime_checkable
class TaskProtocol(Protocol):
    """A registered task — what ``@task`` produces.

    pyPlugy only depends on ``name`` (for introspection) and ``submit``
    (for the eventual ``ctx.task`` invocation path).
    """

    name: str

    def submit(self, *args: Any, **kwargs: Any) -> TaskHandleProtocol:  # pragma: no cover
        ...


@runtime_checkable
class TaskRunnerProtocol(Protocol):
    """The optional batch runner. pyPlugy does not require this surface."""

    def submit(self, task: TaskProtocol, *args: Any, **kwargs: Any) -> TaskHandleProtocol:
        ...  # pragma: no cover

    def run(self) -> dict[str, Any]:  # pragma: no cover
        ...


@runtime_checkable
class SchedulerProtocol(Protocol):
    """The optional scheduler exposed via :attr:`PluginContext.scheduler`.

    Plugins use this for periodic / cron-style work. pyPlugy itself only ever
    holds the reference — the manager neither starts nor stops it; that's the
    host application's job.
    """

    def every(self, spec: str) -> Any:  # pragma: no cover
        ...

    def cron(self, spec: str) -> Any:  # pragma: no cover
        ...

    def start(self) -> None:  # pragma: no cover
        ...

    def stop(self) -> None:  # pragma: no cover
        ...


@runtime_checkable
class TaskyProtocol(Protocol):
    """The ``pytasky`` module surface pyPlugy uses.

    A :class:`PluginManager` can be constructed with an explicit ``tasky=``
    argument that satisfies this protocol — typically the real ``pytasky``
    module, but tests pass a hand-rolled stub.
    """

    task: Callable[..., Any]
