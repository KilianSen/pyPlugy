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

    **Subclassing.** Hosts that need to attach extra surface to the context
    (a FastAPI app, an event bus, a task queue …) should subclass and either
    declare a matching ``__slots__`` for the new attributes or omit
    ``__slots__`` entirely to fall back to a per-instance ``__dict__``::

        class MyContext(PluginContext):
            __slots__ = ("app", "event_bus")

            def __init__(self, *args, app, event_bus, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.app = app
                self.event_bus = event_bus

    Wire the subclass into the manager via the ``context_class`` parameter,
    typically with :func:`functools.partial` to bind host extras::

        manager = PluginManager(
            context_class=functools.partial(MyContext, app=fastapi_app, event_bus=bus)
        )

    **Mutable config.** When the manager passes a ``dict`` for ``config``,
    :attr:`config` returns that same object — changes the host makes to it
    (directly or via :meth:`PluginManager.update_config`) are visible to the
    plugin without a reload.
    """

    __slots__ = (
        "_config",
        "_config_model",
        "_events",
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
        # Preserve identity when the caller already hands us a dict — the
        # manager relies on this so live edits to ``manager._configs[name]``
        # propagate to plugins without a reload. Foreign mappings are still
        # copied defensively.
        if isinstance(config, dict):
            self._config: dict[str, Any] = config
        else:
            self._config = dict(config or {})
        self._tasky = tasky
        self._scheduler = scheduler
        self._tasks: list[Any] = []
        self._logger = logging.getLogger(f"pyplugy.{manifest.name}")
        # Populated by the manager once it has the Plugin instance and can
        # consult ``plugin.config_model``. Stays None when no model is declared.
        self._config_model: Any | None = None
        # Populated by the manager from ``plugin.events`` — one HookPoint per
        # declared key, namespaced as ``"<plugin_name>:<key>"``.
        self._events: dict[str, Any] = {}

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
    def events(self) -> dict[str, Any]:
        """Typed :class:`~pyhooky.HookPoint` instances keyed by ``Plugin.events``.

        Empty when the plugin didn't declare any. Each ``HookPoint`` is
        bound to the manager's registry; listeners attached to them are
        auto-tagged for cleanup.
        """
        return self._events

    @property
    def config_model(self) -> Any | None:
        """Validated config model instance, or ``None`` when the plugin declared no schema.

        The manager populates this when the plugin declares
        ``config_model``. After :meth:`PluginManager.update_config` runs,
        the model is re-validated from the (now-updated) ``config`` dict.
        If a plugin mutates ``ctx.config`` directly, ``config_model``
        won't pick up the change until the next ``update_config`` call —
        explicit mutation is the loose path, validated updates are the
        sharp one.
        """
        return self._config_model

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

    # ---------- typed events (pyHooky HookPoint factory) ----------

    def event(self, name: str, schema: Any) -> Any:
        """Return a pyHooky :class:`~pyhooky.HookPoint` bound to this manager.

        ``schema`` is a pydantic :class:`~pydantic.BaseModel` subclass.
        The returned ``HookPoint`` dispatches through the same registry
        the manager owns, so listeners attached inside the plugin's
        lifecycle are tagged with the plugin name automatically (via
        the existing ``tag_scope`` block) and torn down with it.

        Plugins typically bind the result locally::

            login = ctx.event("auth:login", LoginEvent)
            @login.listen
            def audit(evt: LoginEvent): ...
            login.trigger(user="alice")
        """
        from pyhooky import HookPoint

        return HookPoint(name, schema, registry=self._registry)

    # ---------- dependency access ----------

    def dep(self, name: str) -> PluginContext | None:
        """Return the :class:`PluginContext` of a declared dependency.

        ``name`` must appear in this plugin's ``requires`` or
        ``optional_requires``; otherwise a :class:`PluginDependencyError`
        is raised so the manifest stays the single source of truth for the
        plugin graph. For hard deps the manager guarantees the slot exists
        (load order resolves dependencies first). For optional deps that
        aren't currently loaded, returns ``None``.

        Must be called inside a lifecycle hook (``on_load`` / ``on_enable``
        / etc.) so that :func:`~pyplugy._manager.current_manager` is set.
        """
        from pyplugy._manager import current_manager
        from pyplugy.exceptions import PluginDependencyError

        declared = set(self._manifest.requires) | set(self._manifest.optional_requires)
        # Strip version specifiers — match by name only.
        from pyplugy._depgraph import parse_requirement

        declared_names = {parse_requirement(r)[0] for r in declared}
        if name not in declared_names:
            raise PluginDependencyError(
                f"plugin {self._manifest.name!r} did not declare dep {name!r}; "
                f"add it to requires or optional_requires"
            )
        manager = current_manager()
        if manager is None:
            raise PluginDependencyError(
                f"ctx.dep({name!r}) called outside an active lifecycle hook; "
                "no current manager"
            )
        slot = manager._plugins.get(name)  # noqa: SLF001 - manager-internal lookup
        if slot is None:
            # Optional dep that isn't loaded — silently absent.
            if name in {parse_requirement(r)[0] for r in self._manifest.optional_requires}:
                return None
            # Hard dep should always be present at this point.
            raise PluginDependencyError(
                f"plugin {self._manifest.name!r} requires {name!r} but it is not loaded"
            )
        return slot.ctx

    depends_on = dep

    def export(self, obj: Any) -> Any:
        """Publish ``obj`` as this plugin's API for dependents.

        ``ctx.api_of(name)`` returns whatever was last passed here.
        Must be called inside a lifecycle hook (``on_load`` / ``on_enable``).
        Calling more than once is allowed — the latest call wins, which
        makes it safe to call from both ``on_load`` and ``on_enable``.

        When the plugin declares ``api_model`` (a pydantic
        :class:`~pydantic.BaseModel` subclass), ``obj`` is validated
        against it: a dict is parsed into the model, an existing model
        instance is type-checked, and anything else raises
        :class:`PluginManifestError`-equivalent error from pydantic.

        Returns the stored value (the validated model, or ``obj``
        verbatim when no ``api_model`` is declared).
        """
        from pyplugy._manager import current_manager
        from pyplugy.exceptions import PluginError

        manager = current_manager()
        if manager is None:
            raise PluginError(
                f"ctx.export(...) called outside an active lifecycle hook for "
                f"plugin {self._manifest.name!r}"
            )
        slot = manager._plugins.get(self._manifest.name)  # noqa: SLF001
        if slot is None:
            raise PluginError(
                f"ctx.export(...) called for {self._manifest.name!r} but the "
                "manager has no slot for it — was on_load aborted?"
            )
        validated = _validate_against_model(obj, slot.plugin.api_model)
        slot.api = validated
        return validated

    def api_of(self, name: str) -> Any:
        """Return the API another plugin published via :meth:`export`.

        ``name`` must appear in this plugin's
        ``requires`` / ``optional_requires`` / ``peer_requires`` — the
        same declaration check that :meth:`dep` enforces. Returns ``None``
        if the dep is loaded but never called ``export``, or if it's an
        unsatisfied optional dep.
        """
        from pyplugy._depgraph import parse_requirement
        from pyplugy._manager import current_manager
        from pyplugy.exceptions import PluginDependencyError

        declared = {
            parse_requirement(r)[0]
            for r in (
                *self._manifest.requires,
                *self._manifest.optional_requires,
                *self._manifest.peer_requires,
            )
        }
        if name not in declared:
            raise PluginDependencyError(
                f"plugin {self._manifest.name!r} did not declare dep {name!r}; "
                f"add it to requires / optional_requires / peer_requires"
            )
        manager = current_manager()
        if manager is None:
            raise PluginDependencyError(
                f"ctx.api_of({name!r}) called outside an active lifecycle hook"
            )
        slot = manager._plugins.get(name)  # noqa: SLF001
        if slot is None:
            return None
        return slot.api

    # ---------- task passthroughs ----------

    def task(self, fn: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        """Register a task with the plugin's task scope.

        Requires a :mod:`pyplugy._tasky_protocol`-compatible object on the
        manager (real pyWorkflowy or a test stub). Without one, raises a
        :class:`RuntimeError` pointing at the ``pyplugy[tasks]`` extra.

        Both bare (``@ctx.task``) and parameterised (``@ctx.task(retries=3)``)
        decorator forms work — same call shape as ``@pyworkflowy.task``.
        """
        def _record(task_obj: Any) -> Any:
            self._tasks.append(task_obj)
            return task_obj

        # pyWorkflowy class form: ``ctx.task(MyTaskClass)`` — instantiating
        # a TaskBase subclass returns a Task directly (via TaskBase.__new__),
        # so we register that instance and return it. Lazy import keeps
        # pyworkflowy optional for plugins that only use the decorator form.
        # Handled before the tasky-required check so this form works even
        # when the manager has no tasky configured.
        if isinstance(fn, type):
            try:
                from pyworkflowy import TaskBase
            except ImportError:
                TaskBase = None  # type: ignore[assignment]
            if TaskBase is not None and issubclass(fn, TaskBase):
                return _record(fn())

        if self._tasky is None:
            raise RuntimeError(
                f"plugin {self._manifest.name!r} called ctx.task but no task backend is "
                "configured on the manager. Install pyWorkflowy (`pip install pyplugy[tasks]`) "
                "or pass tasky= to PluginManager(...)."
            )

        decorator = self._tasky.task

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


def _validate_against_model(obj: Any, model: Any | None) -> Any:
    """Coerce/validate ``obj`` against ``model`` (a pydantic ``BaseModel`` subclass).

    ``model=None`` → return ``obj`` as-is. ``obj`` already an instance of
    ``model`` → returned unchanged. ``obj`` is a dict → parsed via
    ``model.model_validate``. Anything else with a non-None model raises
    :class:`PluginManifestError` so misuses fail loudly at the boundary.
    """
    from pydantic import BaseModel, ValidationError

    from pyplugy.exceptions import PluginManifestError

    if model is None:
        return obj
    if isinstance(obj, model):
        return obj
    if isinstance(obj, dict):
        try:
            return model.model_validate(obj)
        except ValidationError as exc:
            raise PluginManifestError(
                f"export payload failed validation against {model.__name__}: {exc}"
            ) from exc
    if isinstance(obj, BaseModel):
        raise PluginManifestError(
            f"export payload is a {type(obj).__name__} instance but {model.__name__} "
            "was declared as api_model; types don't match"
        )
    raise PluginManifestError(
        f"export payload of type {type(obj).__name__} cannot be coerced to {model.__name__}; "
        "pass a dict or an instance of the declared model"
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
