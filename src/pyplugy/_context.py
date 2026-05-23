"""The :class:`PluginContext` handed to a plugin's setup / lifecycle methods.

A context bundles everything a plugin needs to register hooks, schedule tasks,
read config, and talk to its manager. The manager always runs ``on_load``
inside a ``pyhooky.tag_scope(plugin_name)`` block, so *every* hook registered
through the context (or directly via ``pyhooky``'s module helpers) is
auto-tagged with the plugin's name. That auto-tagging is the central
mechanism enabling clean unload via ``HookRegistry.clear_tag``.

The task helpers on the context use the :mod:`pyplugy._tasky_protocol` so
plugins don't need pyWorkflowy installed at import time. When the manager has no
``tasky`` configured, ``ctx.task`` raises a clear :class:`RuntimeError`
pointing at the ``pyplugy[tasks]`` extra.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pyhooky import HookRegistry

    from pyplugy._plugin import PluginManifest
    from pyplugy._tasky_protocol import SchedulerProtocol, TaskyProtocol


__all__ = ["PluginContext", "current_context"]


_current_context: ContextVar[PluginContext | None] = ContextVar(
    "pyplugy_current_context", default=None
)


def current_context() -> PluginContext | None:
    """Return the :class:`PluginContext` for the plugin currently being loaded, if any.

    Set by :class:`PluginManager` for the duration of an ``on_load`` /
    ``on_unload`` / ``on_enable`` / ``on_disable`` call. Contextvar-based, so
    it propagates across asyncio tasks and threads spawned during setup.
    """
    return _current_context.get()


class PluginContext:
    """Per-plugin handle for hook registration, task wiring, config, and logging.

    :class:`PluginContext` is constructed by :class:`PluginManager` and passed
    to every lifecycle method. All hook-registration helpers (:meth:`before`,
    :meth:`after`, :meth:`around`, :meth:`on`, :meth:`on_error`) are thin
    passthroughs to the bound :class:`pyhooky.HookRegistry` — they target the
    registry the manager owns by default, but a custom one may be passed at
    construction.

    Because the manager wraps every call to a plugin's lifecycle methods in
    ``pyhooky.tag_scope(plugin.name)``, the ``tag=`` argument can be omitted —
    every registration in the block picks up the plugin name automatically.
    Helpers exposed here merely make that intent explicit and route through
    the manager-owned registry.
    """

    __slots__ = (
        "_config",
        "_logger",
        "_manifest",
        "_registry",
        "_scheduler",
        "_tasks",
        "_tasky",
    )

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        registry: HookRegistry,
        config: Mapping[str, Any] | None = None,
        tasky: TaskyProtocol | None = None,
        scheduler: SchedulerProtocol | None = None,
    ) -> None:
        self._manifest = manifest
        self._registry = registry
        self._config: dict[str, Any] = dict(config or {})
        self._tasky = tasky
        self._scheduler = scheduler
        self._tasks: list[Any] = []
        self._logger = logging.getLogger(f"pyplugy.{manifest.name}")

    # ---------- exposed surface ----------

    @property
    def name(self) -> str:
        return self._manifest.name

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def hooks(self) -> HookRegistry:
        """The :class:`pyhooky.HookRegistry` hooks land in by default."""
        return self._registry

    @property
    def config(self) -> dict[str, Any]:
        """Mutable per-plugin config dict — populated by the manager."""
        return self._config

    @property
    def logger(self) -> logging.Logger:
        """A :class:`logging.Logger` named ``pyplugy.<plugin-name>``."""
        return self._logger

    @property
    def scheduler(self) -> SchedulerProtocol | None:
        """The optional shared scheduler. ``None`` when none is configured."""
        return self._scheduler

    @property
    def tasks(self) -> tuple[Any, ...]:
        """Tasks registered via :meth:`task`, in registration order."""
        return tuple(self._tasks)

    # ---------- hook passthroughs ----------

    def before(self, target: Any, fn: Any = None, **kwargs: Any) -> Any:
        from pyhooky import before as _before

        return _before(target, fn, registry=self._registry, **kwargs)

    def after(self, target: Any, fn: Any = None, **kwargs: Any) -> Any:
        from pyhooky import after as _after

        return _after(target, fn, registry=self._registry, **kwargs)

    def around(self, target: Any, fn: Any = None, **kwargs: Any) -> Any:
        from pyhooky import around as _around

        return _around(target, fn, registry=self._registry, **kwargs)

    def on(self, target: Any, fn: Any = None, **kwargs: Any) -> Any:
        from pyhooky import on as _on

        return _on(target, fn, registry=self._registry, **kwargs)

    def on_error(self, target: Any, fn: Any = None, **kwargs: Any) -> Any:
        from pyhooky import on_error as _on_error

        return _on_error(target, fn, registry=self._registry, **kwargs)

    def hook(self, target: Any = None, fn: Any = None, **kwargs: Any) -> Any:
        """Wrap a callable so plugin hooks fire around it — passthrough to :func:`pyhooky.hook`.

        Supports the same call shapes as :func:`pyhooky.hook`: bare callable,
        bare invocation, explicit string target, or direct ``(name, fn)`` pair.
        """
        from pyhooky import hook as _hook

        # Bare callable form — `@ctx.hook` directly on a function. The bare
        # overload of `pyhooky.hook` doesn't accept registry=, so wrap it.
        if callable(target) and not isinstance(target, str) and fn is None and not kwargs:
            wrapped = self._registry.wrap(getattr(target, "__name__", None) or repr(target), target)
            return wrapped
        if target is None and fn is None:
            return _hook(registry=self._registry, **kwargs)
        if isinstance(target, str) and fn is None:
            return _hook(target, registry=self._registry, **kwargs)
        return _hook(target, fn, registry=self._registry, **kwargs)

    # ---------- task passthroughs ----------

    def task(self, fn: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        """Register a task with the plugin's task scope.

        Requires a :mod:`pyplugy._tasky_protocol`-compatible object on the
        manager (real pyWorkflowy or a test stub). Without one, raises a
        :class:`RuntimeError` pointing at the ``pyplugy[tasks]`` extra.

        Both bare (``@ctx.task``) and parameterised (``@ctx.task(retries=3)``)
        decorator forms work — same call shape as ``@pyworkflowy.task``.
        """
        if self._tasky is None:
            raise RuntimeError(
                f"plugin {self._manifest.name!r} called ctx.task but no task backend is "
                "configured on the manager. Install pyWorkflowy (`pip install pyplugy[tasks]`) "
                "or pass tasky= to PluginManager(...)."
            )

        decorator = self._tasky.task

        def _record(task_obj: Any) -> Any:
            self._tasks.append(task_obj)
            return task_obj

        if fn is None:
            # Parameterised — `@ctx.task(retries=3)`
            inner = decorator(**kwargs)

            def wrap(real_fn: Callable[..., Any]) -> Any:
                return _record(inner(real_fn))

            return wrap

        if callable(fn) and not kwargs:
            # Bare — `@ctx.task` directly on the function.
            return _record(decorator(fn))

        # Mixed form: `ctx.task(fn, retries=3)` — uncommon but legal.
        return _record(decorator(fn, **kwargs))

    # ---------- introspection ----------

    def __repr__(self) -> str:
        return (
            f"PluginContext(name={self._manifest.name!r}, "
            f"registry={self._registry.name!r}, tasks={len(self._tasks)})"
        )


def _activate(ctx: PluginContext) -> Any:
    """Internal helper used by :class:`PluginManager` to publish ``ctx`` via contextvar.

    Returns a contextmanager that restores the previous context on exit.
    """
    from contextlib import contextmanager

    @contextmanager
    def _scope() -> Any:
        token = _current_context.set(ctx)
        try:
            yield ctx
        finally:
            _current_context.reset(token)

    return _scope()
