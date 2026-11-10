"""The :class:`PluginManager` — owns lifecycle, discovery, and coordination.

Responsibilities:

- Hold the table of loaded plugins, their state, and their owned hook targets.
- Drive the ``UNLOADED → LOADED → ENABLED ↔ DISABLED → UNLOADED`` lifecycle.
- Wrap every ``on_load`` / ``on_enable`` call in
  ``pyhooky.tag_scope(plugin.name)`` so every nested hook registration is
  auto-tagged. This is what makes :meth:`unload` cleanly tear down every hook
  the plugin registered.
- Fire ``pyplugy:plugin:*`` listener hooks so hosts can observe the lifecycle.
- Resolve plugin-to-plugin dependencies via topological sort.

**Disable strategy**: we implement disable as *tear down hooks + tasks, keep
manifest*; enable re-runs the plugin's setup. This is cleaner than juggling a
flag and a snapshot — the registry is the source of truth, and there's no
risk of "stale half-registered" state if the plugin's setup mutates external
resources. The trade-off is that ``on_load`` / ``setup`` is re-invoked on
every enable; plugin authors should treat it as a function that may be called
multiple times. ``on_enable`` / ``on_disable`` fire *in addition* to
``on_load`` / ``on_unload`` so plugins that need separate hooks for "lighter"
toggles can override them.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyhooky import HookRegistry, get_default_registry, tag_scope

from pyplugy._context import PluginContext, _activate, _validate_against_model
from pyplugy._depgraph import (
    check_host_requirement,
    compute_reverse_graph,
    parse_requirement,
    resolve_load_order,
)
from pyplugy._discovery import (
    import_file,
    iter_directory_modules,
    iter_entry_points,
    scan_module,
)
from pyplugy._plugin import Plugin, PluginInfo, PluginState, _DecoratedPlugin, _info_for
from pyplugy._slot_info import PluginSlotInfo
from pyplugy.exceptions import (
    PluginAlreadyLoadedError,
    PluginConfigValidationError,
    PluginConflictError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginManifestError,
    PluginMissingDependencyError,
    PluginNotFoundError,
    PluginSlotConflictError,
    PluginSlotNotFoundError,
    PluginUnloadError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from types import ModuleType

    from pyplugy._task_info import PluginTaskInfo
    from pyplugy._tasky_protocol import SchedulerProtocol, TaskyProtocol

    PluginResolver = Callable[[str], "list[Plugin] | None"]

OnLoadError = Literal["raise", "skip", "warn"]
LoadFailurePhase = Literal["discovery", "plan", "load", "enable"]

_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class PluginLoadFailure:
    """Recorded when a plugin fails discovery, planning, or lifecycle under skip/warn mode."""

    source: str
    error: Exception
    phase: LoadFailurePhase
    plugin: Plugin | None = None


__all__ = [
    "HOOK_PLUGIN_CONFIG_CHANGED",
    "HOOK_PLUGIN_DISABLE",
    "HOOK_PLUGIN_ENABLE",
    "HOOK_PLUGIN_ERROR",
    "HOOK_PLUGIN_LOAD",
    "HOOK_PLUGIN_UNLOAD",
    "LoadFailurePhase",
    "OnLoadError",
    "PluginLoadFailure",
    "PluginManager",
    "current_manager",
]


HOOK_PLUGIN_LOAD = "pyplugy:plugin:load"
HOOK_PLUGIN_UNLOAD = "pyplugy:plugin:unload"
HOOK_PLUGIN_ENABLE = "pyplugy:plugin:enable"
HOOK_PLUGIN_DISABLE = "pyplugy:plugin:disable"
HOOK_PLUGIN_ERROR = "pyplugy:plugin:error"
HOOK_PLUGIN_CONFIG_CHANGED = "pyplugy:plugin:config-changed"


_logger = logging.getLogger("pyplugy")
_current_manager: ContextVar[PluginManager | None] = ContextVar(
    "pyplugy_current_manager", default=None
)


def current_manager() -> PluginManager | None:
    """Return the manager driving the in-progress lifecycle call, if any.

    Set by :class:`PluginManager` for the duration of ``load`` / ``unload`` /
    ``enable`` / ``disable``. Contextvar-based, so it propagates correctly
    across asyncio tasks and threads.
    """
    return _current_manager.get()


def _build_injection_kwargs(
    plugin: Plugin, callable_obj: Any, ctx: PluginContext
) -> dict[str, Any]:
    """Match callable parameter names to declared deps; build the inject kwargs."""
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return {}
    params = list(sig.parameters.values())
    # Drop ``self`` if it shows up unbound (bound methods already omit it).
    if params and params[0].name == "self":
        params = params[1:]
    # Drop the leading ``ctx`` positional — that's what we already pass.
    if not params:
        return {}
    params = params[1:]
    if not params:
        return {}
    declared = {
        parse_requirement(r)[0]
        for r in (
            *plugin.manifest.requires,
            *plugin.manifest.optional_requires,
            *plugin.manifest.peer_requires,
        )
    }
    kwargs: dict[str, Any] = {}
    for p in params:
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if p.name in declared:
            kwargs[p.name] = ctx.api_of(p.name)
    return kwargs


class _Slot:
    """Internal per-plugin bookkeeping the manager keeps in its table."""

    __slots__ = ("api", "ctx", "plugin", "state")

    def __init__(self, plugin: Plugin, ctx: PluginContext) -> None:
        self.plugin = plugin
        self.ctx = ctx
        self.state: PluginState = PluginState.UNLOADED
        self.api: Any = None


class PluginManager:
    """Owns loaded plugins, drives lifecycle, and routes hook registrations.

    Constructed with an optional :class:`pyhooky.HookRegistry` (defaults to
    :func:`pyhooky.get_default_registry`), an optional ``tasky`` object
    satisfying :class:`~pyplugy._tasky_protocol.TaskyProtocol`, and an
    optional ``scheduler``. The manager fires its own lifecycle hooks
    (``pyplugy:plugin:*``) through the *same* registry — listeners can attach
    via :func:`pyhooky.on` and be torn down with :func:`pyhooky.HookRegistry.clear_tag`.
    """

    __slots__ = (
        "_configs",
        "_context_class",
        "_hooks_registry",
        "_host_name",
        "_host_version",
        "_load_errors",
        "_lock",
        "_on_load_error",
        "_plugins",
        "_resolver_max_rounds",
        "_resolvers",
        "_scheduled_jobs",
        "_scheduler",
        "_tasky",
    )

    def __init__(
        self,
        *,
        registry: HookRegistry | None = None,
        tasky: TaskyProtocol | None = None,
        scheduler: SchedulerProtocol | None = None,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        context_class: Callable[..., PluginContext] = PluginContext,
        resolver_max_rounds: int = 10,
        host_version: str | None = None,
        host_name: str | None = None,
        on_load_error: OnLoadError = "raise",
    ) -> None:
        if on_load_error not in ("raise", "skip", "warn"):
            raise ValueError(
                f"on_load_error must be 'raise', 'skip', or 'warn', got {on_load_error!r}"
            )
        self._hooks_registry: HookRegistry = registry or get_default_registry()
        self._tasky = tasky
        self._scheduler = scheduler
        self._plugins: dict[str, _Slot] = {}
        self._configs: dict[str, dict[str, Any]] = {
            name: dict(cfg) for name, cfg in (configs or {}).items()
        }
        self._context_class: Callable[..., PluginContext] = context_class
        self._lock = threading.RLock()
        self._resolvers: list[PluginResolver] = []
        self._resolver_max_rounds = resolver_max_rounds
        self._scheduled_jobs: dict[str, list[Any]] = {}
        self._host_version = host_version
        self._host_name = host_name
        self._on_load_error: OnLoadError = on_load_error
        self._load_errors: list[PluginLoadFailure] = []

    # ---------- public properties ----------

    @property
    def registry(self) -> HookRegistry:
        """The :class:`pyhooky.HookRegistry` plugin hooks land in."""
        return self._hooks_registry

    @property
    def tasky(self) -> TaskyProtocol | None:
        return self._tasky

    @property
    def scheduler(self) -> SchedulerProtocol | None:
        return self._scheduler

    @property
    def host_version(self) -> str | None:
        """The host application version used to validate ``manifest.host_requires``."""
        return self._host_version

    @property
    def host_name(self) -> str | None:
        """The optional host application name used to validate ``manifest.host_requires``."""
        return self._host_name

    @property
    def on_load_error(self) -> OnLoadError:
        """Default error policy (``'raise'``, ``'skip'``, or ``'warn'``) for batch loads."""
        return self._on_load_error

    def load_errors(self) -> list[PluginLoadFailure]:
        """Return failures recorded under ``on_error='skip'|'warn'``."""
        with self._lock:
            return list(self._load_errors)

    def clear_load_errors(self) -> None:
        """Clear the list of recorded load failures."""
        with self._lock:
            self._load_errors.clear()

    # ---------- non-executing discovery ----------

    def discover(
        self,
        target: Any,
        *,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Scan ``target`` and register discovered plugins in ``UNLOADED`` state.

        Does **not** run ``on_load`` or ``on_enable`` — useful for CLI listing
        (e.g. ``vrs plugins``) and declarative slot inspection without executing
        plugin setup code.
        """
        mode = self._resolve_error_mode(on_error)
        try:
            plugins = self._coerce_to_plugins(target)
        except Exception as exc:
            if mode == "raise":
                raise
            self._record_failure(
                source=str(target),
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )
            return []
        return self._register_discovered(plugins, mode=mode)

    def discover_module(
        self,
        module: ModuleType,
        *,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Scan ``module`` and register discovered plugins in ``UNLOADED`` state."""
        mode = self._resolve_error_mode(on_error)
        try:
            plugins = scan_module(module)
        except Exception as exc:
            if mode == "raise":
                raise
            self._record_failure(
                source=getattr(module, "__name__", repr(module)),
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )
            return []
        return self._register_discovered(plugins, mode=mode)

    def discover_entry_points(
        self,
        group: str,
        *,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Scan every entry point in ``group`` and register plugins in ``UNLOADED`` state."""
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_entry_points(group, mode=mode)
        return self._register_discovered(collected, mode=mode)

    def discover_directory(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Scan files under ``path`` and register discovered plugins in ``UNLOADED`` state."""
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_directory(
            path,
            recursive=recursive,
            pattern=pattern,
            mode=mode,
        )
        return self._register_discovered(collected, mode=mode)

    # ---------- loading helpers ----------

    def load(self, target: Any, *, enable: bool = True) -> Plugin:
        """Load a single plugin from ``target`` and (by default) enable it.

        ``target`` may be:

        - a :class:`Plugin` instance,
        - a :class:`Plugin` subclass (instantiated here),
        - an already-imported module (scanned for plugins),
        - a dotted module path (imported then scanned),
        - a filesystem path to a ``.py`` file (imported in a sandbox).

        Returns the loaded :class:`Plugin`. For a target that exposes multiple
        plugins, only the *first* is returned and the rest are silently
        loaded — callers wanting all of them should use
        :meth:`load_all_from_module`.
        """
        plugins = self._coerce_to_plugins(target)
        if not plugins:
            raise PluginLoadError(f"no plugins found in target {target!r}")
        loaded = self.load_all(plugins, enable=enable, on_error="raise")
        return loaded[0]

    def load_all(
        self,
        plugins: list[Plugin],
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Load several plugins in dependency order."""
        mode = self._resolve_error_mode(on_error)
        ordered = self._plan_load(plugins, mode=mode)
        if mode == "raise":
            for p in ordered:
                self._do_load(p)
                if enable:
                    self._do_enable(p)
            return ordered

        loaded: list[Plugin] = []
        failed_names: set[str] = set()
        for p in ordered:
            name = p.manifest.name
            missing_dep = self._first_failed_dep(p, failed_names)
            if missing_dep is not None:
                failed_names.add(name)
                with self._lock:
                    self._plugins.pop(name, None)
                dep_exc = PluginDependencyError(
                    f"plugin {name!r} skipped because required dependency "
                    f"{missing_dep!r} failed to load",
                    consumer=name,
                )
                self._record_failure(
                    source=name,
                    error=dep_exc,
                    phase="load",
                    plugin=p,
                    mode=mode,
                )
                continue
            try:
                self._do_load(p)
            except Exception as exc:
                failed_names.add(name)
                self._record_failure(
                    source=name,
                    error=exc,
                    phase="load",
                    plugin=p,
                    mode=mode,
                    trigger_hook=False,
                )
                continue
            if enable:
                try:
                    self._do_enable(p)
                except Exception as exc:
                    failed_names.add(name)
                    self._hooks_registry.clear_tag(name)
                    with self._lock:
                        self._plugins.pop(name, None)
                    self._record_failure(
                        source=name,
                        error=exc,
                        phase="enable",
                        plugin=p,
                        mode=mode,
                        trigger_hook=False,
                    )
                    continue
            loaded.append(p)
        return loaded

    def load_all_from_module(
        self,
        module: ModuleType,
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Scan ``module`` for plugins and load every one found."""
        mode = self._resolve_error_mode(on_error)
        try:
            plugins = scan_module(module)
        except Exception as exc:
            if mode == "raise":
                raise
            self._record_failure(
                source=getattr(module, "__name__", repr(module)),
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )
            return []
        return self.load_all(plugins, enable=enable, on_error=mode)

    def load_entry_points(
        self,
        group: str,
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Load plugins from every entry point in ``group``.

        Each entry point's module is scanned via :func:`scan_module`. Entry
        points that fail to load raise :class:`PluginLoadError` with the
        underlying exception chained (unless ``on_error`` is ``'skip'`` or
        ``'warn'``).
        """
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_entry_points(group, mode=mode)
        return self.load_all(collected, enable=enable, on_error=mode)

    def load_directory(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Import every matching file under ``path`` and load discovered plugins."""
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_directory(
            path,
            recursive=recursive,
            pattern=pattern,
            mode=mode,
        )
        return self.load_all(collected, enable=enable, on_error=mode)

    # ---------- async loading helpers ----------

    async def aload(self, target: Any, *, enable: bool = True) -> Plugin:
        """Async variant of :meth:`load` — awaits ``async def`` lifecycle methods."""
        plugins = self._coerce_to_plugins(target)
        if not plugins:
            raise PluginLoadError(f"no plugins found in target {target!r}")
        loaded = await self.aload_all(plugins, enable=enable, on_error="raise")
        return loaded[0]

    async def aload_all(
        self,
        plugins: list[Plugin],
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Async variant of :meth:`load_all`."""
        mode = self._resolve_error_mode(on_error)
        ordered = self._plan_load(plugins, mode=mode)
        if mode == "raise":
            for p in ordered:
                await self._ado_load(p)
                if enable:
                    await self._ado_enable(p)
            return ordered

        loaded: list[Plugin] = []
        failed_names: set[str] = set()
        for p in ordered:
            name = p.manifest.name
            missing_dep = self._first_failed_dep(p, failed_names)
            if missing_dep is not None:
                failed_names.add(name)
                with self._lock:
                    self._plugins.pop(name, None)
                dep_exc = PluginDependencyError(
                    f"plugin {name!r} skipped because required dependency "
                    f"{missing_dep!r} failed to load",
                    consumer=name,
                )
                self._record_failure(
                    source=name,
                    error=dep_exc,
                    phase="load",
                    plugin=p,
                    mode=mode,
                )
                continue
            try:
                await self._ado_load(p)
            except Exception as exc:
                failed_names.add(name)
                self._record_failure(
                    source=name,
                    error=exc,
                    phase="load",
                    plugin=p,
                    mode=mode,
                    trigger_hook=False,
                )
                continue
            if enable:
                try:
                    await self._ado_enable(p)
                except Exception as exc:
                    failed_names.add(name)
                    self._hooks_registry.clear_tag(name)
                    with self._lock:
                        self._plugins.pop(name, None)
                    self._record_failure(
                        source=name,
                        error=exc,
                        phase="enable",
                        plugin=p,
                        mode=mode,
                        trigger_hook=False,
                    )
                    continue
            loaded.append(p)
        return loaded

    async def aload_all_from_module(
        self,
        module: ModuleType,
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Async variant of :meth:`load_all_from_module`."""
        mode = self._resolve_error_mode(on_error)
        try:
            plugins = scan_module(module)
        except Exception as exc:
            if mode == "raise":
                raise
            self._record_failure(
                source=getattr(module, "__name__", repr(module)),
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )
            return []
        return await self.aload_all(plugins, enable=enable, on_error=mode)

    async def aload_entry_points(
        self,
        group: str,
        *,
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Async variant of :meth:`load_entry_points`."""
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_entry_points(group, mode=mode)
        return await self.aload_all(collected, enable=enable, on_error=mode)

    async def aload_directory(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
        enable: bool = True,
        on_error: OnLoadError | None = None,
    ) -> list[Plugin]:
        """Async variant of :meth:`load_directory`."""
        mode = self._resolve_error_mode(on_error)
        collected = self._collect_directory(
            path,
            recursive=recursive,
            pattern=pattern,
            mode=mode,
        )
        return await self.aload_all(collected, enable=enable, on_error=mode)

    # ---------- lifecycle ----------

    def enable(self, name: str) -> None:
        """Transition a DISABLED, LOADED, or discovered UNLOADED plugin to ENABLED."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.ENABLED:
                return
            plugin_obj = slot.plugin if slot.state == PluginState.UNLOADED else None
        if plugin_obj is not None:
            self.load_all([plugin_obj], enable=True, on_error="raise")
            return
        self._do_enable(slot.plugin)

    def disable(self, name: str, *, cascade: bool = False) -> None:
        """Transition an ENABLED plugin to DISABLED by tearing down its hooks/tasks.

        Raises :class:`PluginDependencyError` if other enabled plugins
        hard-require ``name``, unless ``cascade=True``. When cascading,
        enabled dependents are disabled in reverse-dependency order
        before ``name``.
        """
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED, PluginState.UNLOADED):
                return
        enabled_dependents = self._enabled_dependents(name)
        if enabled_dependents and not cascade:
            raise PluginDependencyError(
                f"cannot disable {name!r}: enabled dependents "
                f"{sorted(enabled_dependents)}; pass cascade=True to disable them too"
            )
        if cascade:
            for dep_name in self._cascade_closure(name):
                if self._plugins[dep_name].state == PluginState.ENABLED:
                    self._do_disable(self._plugins[dep_name].plugin)
        self._do_disable(slot.plugin)

    def unload(self, name: str, *, cascade: bool = False) -> None:
        """Fully unload a plugin: tear down hooks/tasks, run ``on_unload``, drop the slot.

        Raises :class:`PluginDependencyError` if other plugins hard-require
        ``name`` and are still loaded, unless ``cascade=True``. When
        cascading, dependents are disabled+unloaded in reverse-dependency
        order before ``name``.
        """
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                self._plugins.pop(name, None)
                return
        dependents = self._active_dependents(name)
        if dependents and not cascade:
            raise PluginDependencyError(
                f"cannot unload {name!r}: dependents {sorted(dependents)} still loaded; "
                "pass cascade=True to unload them too"
            )
        if cascade:
            for dep_name in self._cascade_closure(name):
                dep_slot = self._plugins[dep_name]
                if dep_slot.state == PluginState.ENABLED:
                    self._do_disable(dep_slot.plugin)
                self._do_unload(dep_slot.plugin)
        # Disable first if currently enabled — keeps ``on_disable`` semantics tight.
        if slot.state == PluginState.ENABLED:
            self._do_disable(slot.plugin)
        self._do_unload(slot.plugin)

    def reload(self, name: str, *, cascade: bool = True) -> Plugin:
        """Unload then re-load the same :class:`Plugin` instance.

        When dependents exist, ``cascade=True`` (the default) tears them
        down before ``name`` and reloads them afterwards, restoring the
        prior enabled/disabled state. ``cascade=False`` raises if any
        plugin still depends on ``name``.
        """
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                raise PluginError(
                    f"cannot reload unloaded plugin {name!r}; call enable({name!r}) instead"
                )
            plugin = slot.plugin
        dependents = self._active_dependents(name)
        if dependents and not cascade:
            raise PluginDependencyError(
                f"cannot reload {name!r}: dependents {sorted(dependents)} still loaded; "
                "pass cascade=True (the default) to reload them too"
            )
        closure = self._cascade_closure(name) if cascade else []
        # Snapshot the plugins + their enabled status before we tear anything down.
        closure_plugins = [self._plugins[n].plugin for n in closure]
        was_enabled = {n: self._plugins[n].state == PluginState.ENABLED for n in closure}
        for dep_name in closure:
            dep_slot = self._plugins[dep_name]
            if dep_slot.state == PluginState.ENABLED:
                self._do_disable(dep_slot.plugin)
            self._do_unload(dep_slot.plugin)
        if slot.state == PluginState.ENABLED:
            self._do_disable(slot.plugin)
        self._do_unload(slot.plugin)
        self._do_load(plugin)
        self._do_enable(plugin)
        # ``closure`` is consumers-first; reverse to load deepest deps first.
        for dep_plugin in reversed(closure_plugins):
            self._do_load(dep_plugin)
            if was_enabled[dep_plugin.manifest.name]:
                self._do_enable(dep_plugin)
        return plugin

    async def aenable(self, name: str) -> None:
        """Async variant of :meth:`enable`."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.ENABLED:
                return
            plugin_obj = slot.plugin if slot.state == PluginState.UNLOADED else None
        if plugin_obj is not None:
            await self.aload_all([plugin_obj], enable=True, on_error="raise")
            return
        await self._ado_enable(slot.plugin)

    async def adisable(self, name: str, *, cascade: bool = False) -> None:
        """Async variant of :meth:`disable`."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED, PluginState.UNLOADED):
                return
        enabled_dependents = self._enabled_dependents(name)
        if enabled_dependents and not cascade:
            raise PluginDependencyError(
                f"cannot disable {name!r}: enabled dependents "
                f"{sorted(enabled_dependents)}; pass cascade=True to disable them too"
            )
        if cascade:
            for dep_name in self._cascade_closure(name):
                if self._plugins[dep_name].state == PluginState.ENABLED:
                    await self._ado_disable(self._plugins[dep_name].plugin)
        await self._ado_disable(slot.plugin)

    async def aunload(self, name: str, *, cascade: bool = False) -> None:
        """Async variant of :meth:`unload`."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                self._plugins.pop(name, None)
                return
        dependents = self._active_dependents(name)
        if dependents and not cascade:
            raise PluginDependencyError(
                f"cannot unload {name!r}: dependents {sorted(dependents)} still loaded; "
                "pass cascade=True to unload them too"
            )
        if cascade:
            for dep_name in self._cascade_closure(name):
                dep_slot = self._plugins[dep_name]
                if dep_slot.state == PluginState.ENABLED:
                    await self._ado_disable(dep_slot.plugin)
                await self._ado_unload(dep_slot.plugin)
        if slot.state == PluginState.ENABLED:
            await self._ado_disable(slot.plugin)
        await self._ado_unload(slot.plugin)

    def swap(self, name: str, new_plugin: Plugin) -> Plugin:
        """Replace a loaded plugin's instance with ``new_plugin``.

        Both plugins must share the same ``manifest.name``. The active
        dependents (hard + peer) are torn down and reloaded around the
        swap, restoring their prior enabled/disabled state. Versions may
        differ — the resolved order is re-validated against the new
        manifest's ``requires``.
        """
        if new_plugin.manifest.name != name:
            raise PluginError(
                f"swap target {name!r} != new plugin's name {new_plugin.manifest.name!r}; "
                "swap cannot rename"
            )
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                self._plugins.pop(name, None)
                self._register_discovered([new_plugin], mode="raise")
                return new_plugin
        closure = self._cascade_closure(name)
        closure_plugins = [self._plugins[n].plugin for n in closure]
        was_enabled = {n: self._plugins[n].state == PluginState.ENABLED for n in closure}
        for dep_name in closure:
            dep_slot = self._plugins[dep_name]
            if dep_slot.state == PluginState.ENABLED:
                self._do_disable(dep_slot.plugin)
            self._do_unload(dep_slot.plugin)
        if slot.state == PluginState.ENABLED:
            self._do_disable(slot.plugin)
        self._do_unload(slot.plugin)
        self._do_load(new_plugin)
        self._do_enable(new_plugin)
        for dep_plugin in reversed(closure_plugins):
            self._do_load(dep_plugin)
            if was_enabled[dep_plugin.manifest.name]:
                self._do_enable(dep_plugin)
        return new_plugin

    async def aswap(self, name: str, new_plugin: Plugin) -> Plugin:
        """Async variant of :meth:`swap`."""
        if new_plugin.manifest.name != name:
            raise PluginError(
                f"swap target {name!r} != new plugin's name {new_plugin.manifest.name!r}; "
                "swap cannot rename"
            )
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                self._plugins.pop(name, None)
                self._register_discovered([new_plugin], mode="raise")
                return new_plugin
        closure = self._cascade_closure(name)
        closure_plugins = [self._plugins[n].plugin for n in closure]
        was_enabled = {n: self._plugins[n].state == PluginState.ENABLED for n in closure}
        for dep_name in closure:
            dep_slot = self._plugins[dep_name]
            if dep_slot.state == PluginState.ENABLED:
                await self._ado_disable(dep_slot.plugin)
            await self._ado_unload(dep_slot.plugin)
        if slot.state == PluginState.ENABLED:
            await self._ado_disable(slot.plugin)
        await self._ado_unload(slot.plugin)
        await self._ado_load(new_plugin)
        await self._ado_enable(new_plugin)
        for dep_plugin in reversed(closure_plugins):
            await self._ado_load(dep_plugin)
            if was_enabled[dep_plugin.manifest.name]:
                await self._ado_enable(dep_plugin)
        return new_plugin

    async def areload(self, name: str, *, cascade: bool = True) -> Plugin:
        """Async variant of :meth:`reload`."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.UNLOADED:
                raise PluginError(
                    f"cannot reload unloaded plugin {name!r}; call aenable({name!r}) instead"
                )
            plugin = slot.plugin
        dependents = self._active_dependents(name)
        if dependents and not cascade:
            raise PluginDependencyError(
                f"cannot reload {name!r}: dependents {sorted(dependents)} still loaded; "
                "pass cascade=True (the default) to reload them too"
            )
        closure = self._cascade_closure(name) if cascade else []
        closure_plugins = [self._plugins[n].plugin for n in closure]
        was_enabled = {n: self._plugins[n].state == PluginState.ENABLED for n in closure}
        for dep_name in closure:
            dep_slot = self._plugins[dep_name]
            if dep_slot.state == PluginState.ENABLED:
                await self._ado_disable(dep_slot.plugin)
            await self._ado_unload(dep_slot.plugin)
        if slot.state == PluginState.ENABLED:
            await self._ado_disable(slot.plugin)
        await self._ado_unload(slot.plugin)
        await self._ado_load(plugin)
        await self._ado_enable(plugin)
        for dep_plugin in reversed(closure_plugins):
            await self._ado_load(dep_plugin)
            if was_enabled[dep_plugin.manifest.name]:
                await self._ado_enable(dep_plugin)
        return plugin

    # ---------- runtime config ----------

    def update_config(
        self,
        name: str,
        new_config: Mapping[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Mutate a plugin's config in place so live ``ctx.config`` reflects the change.

        ``replace=False`` (default) merges ``new_config`` into the existing
        dict (``dict.update``). ``replace=True`` clears the dict first, then
        applies ``new_config`` — useful when keys must be dropped.

        Works whether or not the plugin is currently loaded. If it is loaded,
        fires :data:`HOOK_PLUGIN_CONFIG_CHANGED` with ``(plugin, config_dict)``
        so listeners can react. Identity of the config dict is preserved,
        so plugins that captured a reference to ``ctx.config`` keep seeing
        live values.
        """
        with self._lock:
            cfg = self._configs.setdefault(name, {})
            slot = self._plugins.get(name)
            # Snapshot so we can roll back if validation rejects the new state.
            snapshot = dict(cfg)
            if replace:
                cfg.clear()
            cfg.update(new_config)
            if slot is not None and slot.plugin.config_model is not None:
                try:
                    validated = _validate_against_model(cfg, slot.plugin.config_model)
                except PluginManifestError as exc:
                    cfg.clear()
                    cfg.update(snapshot)
                    raise PluginConfigValidationError(
                        f"update_config rejected for {name!r}: {exc}"
                    ) from exc
                slot.ctx._config_model = validated
        if slot is not None:
            self._hooks_registry.trigger(HOOK_PLUGIN_CONFIG_CHANGED, slot.plugin, cfg)

    # ---------- introspection ----------

    def get(self, name: str) -> Plugin:
        with self._lock:
            return self._require_slot(name).plugin

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            return self._require_slot(name).state == PluginState.ENABLED

    def state(self, name: str) -> PluginState:
        with self._lock:
            return self._require_slot(name).state

    def plugin_targets(self, name: str) -> list[str]:
        """pyHooky targets currently owned by ``name`` (filtered by tag)."""
        with self._lock:
            self._require_slot(name)
        targets: set[str] = set()
        for target_name, h in self._hooks_registry.all_hooks():
            if h.tag == name:
                targets.add(target_name)
        return sorted(targets)

    def plugin_tasks(self, name: str) -> tuple[PluginTaskInfo, ...]:
        """Tasks owned by ``name`` as :class:`PluginTaskInfo` entries.

        Each entry pairs the underlying task object with the host-supplied
        metadata captured at ``ctx.task(metadata=...)``. Returns an empty
        tuple when the plugin is loaded but has no tasks (or is currently
        disabled — disable clears the task list).
        """
        with self._lock:
            slot = self._require_slot(name)
            return slot.ctx.tasks

    def plugin_slots(self, name: str) -> tuple[PluginSlotInfo, ...]:
        """Slot entries owned by ``name`` as :class:`PluginSlotInfo` instances.

        Includes statically declared ``Plugin.slots`` entries (even when
        ``UNLOADED`` or ``DISABLED``) plus any dynamic entries registered via
        ``ctx.provide`` while enabled.
        """
        with self._lock:
            slot = self._require_slot(name)
            return slot.ctx.slots

    def get_slot(
        self,
        slot: str,
        name: str,
        *,
        default: Any = _UNSET,
        auto_enable: bool = False,
    ) -> Any:
        """Return the provider registered for ``(slot, name)`` by an enabled plugin.

        When ``auto_enable=True`` and the provider belongs to a discovered
        (``UNLOADED``), ``LOADED``, or ``DISABLED`` plugin, that plugin is
        automatically enabled first.

        If no enabled plugin provides ``(slot, name)``, returns ``default``
        when provided, or raises :class:`PluginSlotNotFoundError`.
        """
        with self._lock:
            for s in self._plugins.values():
                if s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot and info.name == name:
                        return info.provider

            candidate_plugin: str | None = None
            if auto_enable:
                for p_name, s in self._plugins.items():
                    if s.state == PluginState.ENABLED:
                        continue
                    if any(info.slot == slot and info.name == name for info in s.ctx.slots):
                        candidate_plugin = p_name
                        break

        if candidate_plugin is not None:
            self.enable(candidate_plugin)
            with self._lock:
                s = self._plugins.get(candidate_plugin)
                if s is not None and s.state == PluginState.ENABLED:
                    for info in s.ctx.slots:
                        if info.slot == slot and info.name == name:
                            return info.provider

        if default is not _UNSET:
            return default

        available = tuple(self.slot_names(slot, include_unloaded=auto_enable))
        raise PluginSlotNotFoundError(
            f"no provider named {name!r} in slot {slot!r} (available: {list(available)})",
            slot=slot,
            name=name,
            available=available,
        )

    async def aget_slot(
        self,
        slot: str,
        name: str,
        *,
        default: Any = _UNSET,
        auto_enable: bool = False,
    ) -> Any:
        """Async variant of :meth:`get_slot` (uses :meth:`aenable` when ``auto_enable=True``)."""
        with self._lock:
            for s in self._plugins.values():
                if s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot and info.name == name:
                        return info.provider

            candidate_plugin: str | None = None
            if auto_enable:
                for p_name, s in self._plugins.items():
                    if s.state == PluginState.ENABLED:
                        continue
                    if any(info.slot == slot and info.name == name for info in s.ctx.slots):
                        candidate_plugin = p_name
                        break

        if candidate_plugin is not None:
            await self.aenable(candidate_plugin)
            with self._lock:
                s = self._plugins.get(candidate_plugin)
                if s is not None and s.state == PluginState.ENABLED:
                    for info in s.ctx.slots:
                        if info.slot == slot and info.name == name:
                            return info.provider

        if default is not _UNSET:
            return default

        available = tuple(self.slot_names(slot, include_unloaded=auto_enable))
        raise PluginSlotNotFoundError(
            f"no provider named {name!r} in slot {slot!r} (available: {list(available)})",
            slot=slot,
            name=name,
            available=available,
        )

    def get_slots(
        self,
        slot: str,
        names: Iterable[str] | None = None,
        *,
        auto_enable: bool = False,
    ) -> dict[str, Any]:
        """Return ``{name: provider}`` for ``slot``.

        - When ``names`` is given (e.g. ``["dead", "rings"]``), resolves each
          requested name in order via :meth:`get_slot`, raising
          :class:`PluginSlotNotFoundError` if any name is missing.
        - When ``names is None``, returns all providers registered under
          ``slot`` by enabled plugins (activating discovered/disabled providers
          first when ``auto_enable=True``).
        """
        if names is not None:
            return {n: self.get_slot(slot, n, auto_enable=auto_enable) for n in names}

        if auto_enable:
            with self._lock:
                to_enable = [
                    p_name
                    for p_name, s in self._plugins.items()
                    if s.state != PluginState.ENABLED
                    and any(info.slot == slot for info in s.ctx.slots)
                ]
            for p_name in to_enable:
                self.enable(p_name)

        out: dict[str, Any] = {}
        with self._lock:
            for s in self._plugins.values():
                if s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot:
                        out[info.name] = info.provider
        return out

    async def aget_slots(
        self,
        slot: str,
        names: Iterable[str] | None = None,
        *,
        auto_enable: bool = False,
    ) -> dict[str, Any]:
        """Async variant of :meth:`get_slots` (uses :meth:`aget_slot` / :meth:`aenable`)."""
        if names is not None:
            out_named: dict[str, Any] = {}
            for n in names:
                out_named[n] = await self.aget_slot(slot, n, auto_enable=auto_enable)
            return out_named

        if auto_enable:
            with self._lock:
                to_enable = [
                    p_name
                    for p_name, s in self._plugins.items()
                    if s.state != PluginState.ENABLED
                    and any(info.slot == slot for info in s.ctx.slots)
                ]
            for p_name in to_enable:
                await self.aenable(p_name)

        out: dict[str, Any] = {}
        with self._lock:
            for s in self._plugins.values():
                if s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot:
                        out[info.name] = info.provider
        return out

    def slot_names(self, slot: str, *, include_unloaded: bool = True) -> list[str]:
        """Return sorted provider names registered under ``slot``.

        With ``include_unloaded=True`` (the default), includes declarative
        slots from discovered (``UNLOADED``) and disabled plugins so CLI
        introspection works without executing plugin setup code.
        """
        names: set[str] = set()
        with self._lock:
            for s in self._plugins.values():
                if not include_unloaded and s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot:
                        names.add(info.name)
        return sorted(names)

    def list_slots(
        self,
        slot: str | None = None,
        *,
        include_unloaded: bool = False,
    ) -> list[PluginSlotInfo]:
        """Return :class:`PluginSlotInfo` entries across managed plugins.

        Pass ``slot="fix"`` to filter to a single slot category, or
        ``include_unloaded=True`` to include entries from ``UNLOADED`` /
        ``DISABLED`` plugins.
        """
        out: list[PluginSlotInfo] = []
        with self._lock:
            for s in self._plugins.values():
                if not include_unloaded and s.state != PluginState.ENABLED:
                    continue
                for info in s.ctx.slots:
                    if slot is None or info.slot == slot:
                        out.append(info)
        return out

    def list_plugins(self) -> list[PluginInfo]:
        with self._lock:
            slots = list(self._plugins.values())
        out: list[PluginInfo] = []
        for slot in slots:
            p_name = slot.plugin.manifest.name
            targets = tuple(self.plugin_targets(p_name))
            tasks = self.plugin_tasks(p_name)
            p_slots = self.plugin_slots(p_name)
            out.append(
                _info_for(
                    slot.plugin,
                    state=slot.state,
                    targets=targets,
                    tasks=tasks,
                    slots=p_slots,
                )
            )
        return out

    def dump(self) -> dict[str, Any]:
        """Return a structured snapshot — handy for CLIs and debug output."""
        return {
            "registry": self._hooks_registry.name,
            "host_name": self._host_name,
            "host_version": self._host_version,
            "plugins": [
                {
                    "name": info.name,
                    "version": info.version,
                    "state": info.state.value,
                    "requires": list(info.requires),
                    "optional_requires": list(info.optional_requires),
                    "peer_requires": list(info.peer_requires),
                    "host_requires": info.host_requires,
                    "conflicts": list(info.conflicts),
                    "description": info.description,
                    "author": info.author,
                    "tags": list(info.tags),
                    "targets": list(info.targets),
                    "slots": [
                        {
                            "slot": s.slot,
                            "name": s.name,
                            "metadata": dict(s.metadata),
                        }
                        for s in info.slots
                    ],
                    "tasks": [
                        {
                            "name": getattr(t.task, "name", repr(t.task)),
                            "metadata": dict(t.metadata),
                        }
                        for t in info.tasks
                    ],
                }
                for info in self.list_plugins()
            ],
            "graph": self.dependency_graph(),
            "load_errors": [
                {
                    "source": err.source,
                    "phase": err.phase,
                    "error": str(err.error),
                    "error_type": type(err.error).__name__,
                }
                for err in self.load_errors()
            ],
        }

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._plugins)
            enabled = sum(1 for s in self._plugins.values() if s.state == PluginState.ENABLED)
        return (
            f"PluginManager(registry={self._hooks_registry.name!r}, "
            f"plugins={count}, enabled={enabled})"
        )

    # ---------- dependency introspection ----------

    def dependencies_of(self, name: str, *, transitive: bool = False) -> list[str]:
        """Names this plugin's manifest declares as deps (hard + peer + optional).

        With ``transitive=True``, also follow each dep's own deps recursively.
        Names returned in alphabetical order for stable output.
        """

        def _direct(manifest: Any) -> set[str]:
            out: set[str] = set()
            for group in (manifest.requires, manifest.peer_requires, manifest.optional_requires):
                for req in group:
                    out.add(parse_requirement(req)[0])
            return out

        with self._lock:
            self._require_slot(name)
            if not transitive:
                return sorted(_direct(self._plugins[name].plugin.manifest))
            visited: set[str] = set()
            stack = [name]
            while stack:
                current = stack.pop()
                slot = self._plugins.get(current)
                if slot is None:
                    continue
                for dep_name in _direct(slot.plugin.manifest):
                    if dep_name not in visited:
                        visited.add(dep_name)
                        stack.append(dep_name)
            visited.discard(name)
            return sorted(visited)

    def dependents_of(self, name: str, *, transitive: bool = False) -> list[str]:
        """Names of loaded plugins that hard-require ``name``.

        With ``transitive=True``, also follow each dependent's dependents.
        Only hard ``requires`` edges count — optional deps don't create a
        reverse edge for cascade purposes.
        """
        with self._lock:
            self._require_slot(name)
            reverse = compute_reverse_graph([s.plugin for s in self._plugins.values()])
        if not transitive:
            return sorted(reverse.get(name, set()))
        visited: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            for consumer in reverse.get(current, set()):
                if consumer not in visited:
                    visited.add(consumer)
                    stack.append(consumer)
        return sorted(visited)

    def dependency_graph(self) -> dict[str, list[str]]:
        """Snapshot of the pinning-dep graph: ``{plugin_name: [direct hard+peer deps...]}``.

        Hard ``requires`` and ``peer_requires`` both pin the dep at
        teardown time, so both are surfaced here. Optional deps and
        conflicts are excluded — those are visible in :meth:`dump` and
        :meth:`list_plugins`.
        """
        with self._lock:
            slots = list(self._plugins.values())
        out: dict[str, list[str]] = {}
        for slot in slots:
            manifest = slot.plugin.manifest
            deps: set[str] = set()
            for req in manifest.requires:
                deps.add(parse_requirement(req)[0])
            for req in manifest.peer_requires:
                deps.add(parse_requirement(req)[0])
            out[manifest.name] = sorted(deps)
        return out

    # ---------- internals ----------

    def _active_dependents(self, name: str) -> set[str]:
        """Direct dependents of ``name`` (hard + peer) still in a loaded state.

        Optional deps are intentionally excluded — they don't block teardown.
        """
        with self._lock:
            out: set[str] = set()
            for other_name, slot in self._plugins.items():
                if other_name == name or slot.state == PluginState.UNLOADED:
                    continue
                manifest = slot.plugin.manifest
                pinning = list(manifest.requires) + list(manifest.peer_requires)
                for req in pinning:
                    if parse_requirement(req)[0] == name:
                        out.add(other_name)
                        break
            return out

    def _enabled_dependents(self, name: str) -> set[str]:
        """Direct hard dependents of ``name`` whose state is ENABLED."""
        with self._lock:
            return {
                n
                for n in self._active_dependents(name)
                if self._plugins[n].state == PluginState.ENABLED
            }

    def _cascade_closure(self, name: str) -> list[str]:
        """Return ``name``'s transitive active hard-dependents, consumers-first.

        The target ``name`` itself is *not* included — callers tear these
        names down before tearing down ``name``. Order is reverse-topo so
        that deep dependents come first; safe to iterate and disable/unload
        in sequence.
        """
        closure: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            for dep in self._active_dependents(current):
                if dep in closure:
                    continue
                closure.add(dep)
                stack.append(dep)
        if not closure:
            return []
        with self._lock:
            plugins = [self._plugins[n].plugin for n in (closure | {name})]
        ordered = resolve_load_order(plugins)
        ordered_names = [p.manifest.name for p in ordered]
        # ``ordered_names`` is deps-first; reverse to get consumers-first,
        # then filter out the target.
        return [n for n in reversed(ordered_names) if n in closure]

    def _require_slot(self, name: str) -> _Slot:
        slot = self._plugins.get(name)
        if slot is None:
            raise PluginNotFoundError(f"no plugin named {name!r}")
        return slot

    def _resolve_error_mode(self, on_error: OnLoadError | None) -> OnLoadError:
        mode = self._on_load_error if on_error is None else on_error
        if mode not in ("raise", "skip", "warn"):
            raise ValueError(f"on_error must be 'raise', 'skip', or 'warn', got {mode!r}")
        return mode

    def _record_failure(
        self,
        *,
        source: str,
        error: Exception,
        phase: LoadFailurePhase,
        plugin: Plugin | None,
        mode: OnLoadError,
        trigger_hook: bool = True,
    ) -> None:
        failure = PluginLoadFailure(source=source, error=error, phase=phase, plugin=plugin)
        with self._lock:
            self._load_errors.append(failure)
        if trigger_hook:
            self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, error)
        if mode == "warn":
            _logger.warning("pyplugy: skipping %r during %s: %s", source, phase, error)

    def _collect_entry_points(self, group: str, *, mode: OnLoadError) -> list[Plugin]:
        collected: list[Plugin] = []

        def _on_ep_error(ep_name: str, exc: Exception) -> None:
            wrapped = PluginLoadError(
                f"failed to load entry point {ep_name!r} (group {group!r}): {exc}"
            )
            wrapped.__cause__ = exc
            self._record_failure(
                source=ep_name,
                error=wrapped,
                phase="discovery",
                plugin=None,
                mode=mode,
            )

        ep_iter = iter_entry_points(group, on_error=None if mode == "raise" else _on_ep_error)
        for ep_name, module in ep_iter:
            try:
                collected.extend(scan_module(module))
            except Exception as exc:
                wrapped = PluginLoadError(
                    f"failed to scan entry point {ep_name!r} (group {group!r}): {exc}"
                )
                wrapped.__cause__ = exc
                if mode == "raise":
                    raise wrapped from exc
                self._record_failure(
                    source=ep_name,
                    error=wrapped,
                    phase="discovery",
                    plugin=None,
                    mode=mode,
                )
        return collected

    def _collect_directory(
        self,
        path: Path,
        *,
        recursive: bool,
        pattern: str,
        mode: OnLoadError,
    ) -> list[Plugin]:
        collected: list[Plugin] = []

        def _on_file_error(file_path: str, exc: Exception) -> None:
            self._record_failure(
                source=file_path,
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )

        mod_iter = iter_directory_modules(
            path,
            recursive=recursive,
            pattern=pattern,
            on_error=None if mode == "raise" else _on_file_error,
        )
        try:
            for module in mod_iter:
                try:
                    collected.extend(scan_module(module))
                except Exception as exc:
                    if mode == "raise":
                        raise
                    self._record_failure(
                        source=getattr(module, "__file__", None) or module.__name__,
                        error=exc,
                        phase="discovery",
                        plugin=None,
                        mode=mode,
                    )
        except Exception as exc:
            if mode == "raise":
                raise
            self._record_failure(
                source=str(path),
                error=exc,
                phase="discovery",
                plugin=None,
                mode=mode,
            )
        return collected

    def _slot_owner(
        self,
        slot: str,
        name: str,
        *,
        exclude_plugin: str | None = None,
    ) -> str | None:
        with self._lock:
            for p_name, s in self._plugins.items():
                if p_name == exclude_plugin:
                    continue
                for info in s.ctx.slots:
                    if info.slot == slot and info.name == name:
                        return p_name
        return None

    def _register_discovered(
        self,
        plugins: list[Plugin],
        *,
        mode: OnLoadError,
    ) -> list[Plugin]:
        registered: list[Plugin] = []
        for p in plugins:
            name = p.manifest.name
            try:
                with self._lock:
                    if name in self._plugins:
                        raise PluginAlreadyLoadedError(f"plugin {name!r} is already registered")
                    check_host_requirement(
                        p,
                        host_version=self._host_version,
                        host_name=self._host_name,
                    )
                    for foe in p.manifest.conflicts:
                        if foe in self._plugins:
                            raise PluginConflictError(
                                f"plugin {name!r} conflicts with {foe!r}; cannot register together",
                                consumer=name,
                            )
                    for other_name, other_slot in self._plugins.items():
                        if name in other_slot.plugin.manifest.conflicts:
                            raise PluginConflictError(
                                f"plugin {other_name!r} conflicts with {name!r}; "
                                "cannot register together",
                                consumer=name,
                            )
                    for slot_name, entries in p.manifest.slots.items():
                        for entry_name in entries:
                            owner = self._slot_owner(slot_name, entry_name, exclude_plugin=name)
                            if owner is not None:
                                raise PluginSlotConflictError(
                                    f"slot ({slot_name!r}, {entry_name!r}) is claimed by both "
                                    f"{owner!r} and {name!r}",
                                    slot=slot_name,
                                    name=entry_name,
                                    existing_plugin=owner,
                                    new_plugin=name,
                                )
                    ctx = self._build_ctx(p)
                    slot = _Slot(p, ctx)
                    slot.state = PluginState.UNLOADED
                    self._plugins[name] = slot
                registered.append(p)
            except Exception as exc:
                if mode == "raise":
                    raise
                self._record_failure(
                    source=name,
                    error=exc,
                    phase="plan",
                    plugin=p,
                    mode=mode,
                )
        return registered

    def _first_failed_dep(self, plugin: Plugin, failed_names: set[str]) -> str | None:
        if not failed_names:
            return None
        for req in (*plugin.manifest.requires, *plugin.manifest.peer_requires):
            dep_name, _ = parse_requirement(req)
            if dep_name in failed_names:
                return dep_name
        return None

    def _coerce_to_plugins(self, target: Any) -> list[Plugin]:
        if isinstance(target, Plugin):
            return [target]
        if isinstance(target, type) and issubclass(target, Plugin):
            return [target()]
        if isinstance(target, list):
            out: list[Plugin] = []
            for item in target:
                out.extend(self._coerce_to_plugins(item))
            return out
        # Module
        from types import ModuleType as _MT

        if isinstance(target, _MT):
            return scan_module(target)
        # Path or string path
        from pathlib import Path as _P

        if isinstance(target, _P):
            module = import_file(target)
            return scan_module(module)
        if isinstance(target, str):
            if target.endswith(".py") or "/" in target or "\\" in target:
                module = import_file(_P(target))
                return scan_module(module)
            # Dotted module path
            import importlib

            module = importlib.import_module(target)
            return scan_module(module)
        raise PluginLoadError(
            f"don't know how to load {target!r}: expected Plugin, Plugin subclass, "
            "module, list, Path, or dotted/path string"
        )

    def _plan_load(
        self,
        plugins: list[Plugin],
        *,
        mode: OnLoadError = "raise",
    ) -> list[Plugin]:
        """Validate uniqueness, merge with already-loaded plugins, return dep-ordered list.

        If a hard dependency is missing and resolvers are registered, this
        method calls each resolver with the missing name and retries
        planning. Capped at ``self._resolver_max_rounds`` rounds to surface
        runaway resolvers.
        """
        if mode != "raise":
            return self._plan_load_tolerant(plugins, mode=mode)

        batch = list(plugins)
        seen_misses: list[str] = []
        for _round in range(self._resolver_max_rounds):
            try:
                return self._plan_load_once(batch)
            except PluginMissingDependencyError as exc:
                if not self._resolvers:
                    raise
                extras = self._invoke_resolvers(exc.missing)
                if not extras:
                    raise
                seen_misses.append(exc.missing)
                existing = {p.manifest.name for p in batch} | {
                    n for n, s in self._plugins.items() if s.state != PluginState.UNLOADED
                }
                added = False
                for p in extras:
                    if p.manifest.name in existing:
                        continue
                    batch.append(p)
                    existing.add(p.manifest.name)
                    added = True
                if not added:
                    # Resolver returned plugins but none were new — give up
                    # so we don't loop forever with the same missing name.
                    raise
        raise PluginDependencyError(
            f"resolvers failed to converge after {self._resolver_max_rounds} rounds; "
            f"missing chain: {seen_misses}"
        )

    def _expand_with_unloaded_deps(self, plugins: list[Plugin]) -> list[Plugin]:
        """Pull in any discovered ``UNLOADED`` plugins needed as hard deps of ``plugins``."""
        expanded = list(plugins)
        seen = {p.manifest.name for p in expanded}
        queue = list(expanded)
        while queue:
            current = queue.pop(0)
            m = current.manifest
            for req in m.requires:
                dep_name, _ = parse_requirement(req)
                if dep_name in seen:
                    continue
                slot = self._plugins.get(dep_name)
                if slot is not None and slot.state == PluginState.UNLOADED:
                    seen.add(dep_name)
                    expanded.append(slot.plugin)
                    queue.append(slot.plugin)
        return expanded

    def _plan_load_once(self, plugins: list[Plugin]) -> list[Plugin]:
        with self._lock:
            loaded_existing = {
                n: s.plugin for n, s in self._plugins.items() if s.state != PluginState.UNLOADED
            }
            seen: set[str] = set()
            for p in plugins:
                name = p.manifest.name
                if name in loaded_existing:
                    raise PluginAlreadyLoadedError(f"plugin {name!r} is already loaded")
                if name in seen:
                    raise PluginAlreadyLoadedError(
                        f"plugin {name!r} appears twice in the load batch"
                    )
                seen.add(name)

            expanded = self._expand_with_unloaded_deps(plugins)
            expanded_names = {p.manifest.name for p in expanded}
            # Check slot conflicts against any already-registered plugin (loaded or discovered).
            for p in expanded:
                name = p.manifest.name
                for slot_name, entries in p.manifest.slots.items():
                    for entry_name in entries:
                        owner = self._slot_owner(slot_name, entry_name, exclude_plugin=name)
                        if owner is not None and owner not in expanded_names:
                            raise PluginSlotConflictError(
                                f"slot ({slot_name!r}, {entry_name!r}) is claimed by both "
                                f"{owner!r} and {name!r}",
                                slot=slot_name,
                                name=entry_name,
                                existing_plugin=owner,
                                new_plugin=name,
                            )

            # Topo-sort considers both new plugins (for cross-deps in the batch)
            # AND already-loaded ones (so a new plugin can depend on an existing one).
            combined = list(expanded) + list(loaded_existing.values())
            ordered_combined = resolve_load_order(
                combined,
                host_version=self._host_version,
                host_name=self._host_name,
            )
            # Keep only the new/promoted ones, in the resolved order.
            return [p for p in ordered_combined if p.manifest.name in expanded_names]

    def _plan_load_tolerant(
        self,
        plugins: list[Plugin],
        *,
        mode: OnLoadError,
    ) -> list[Plugin]:
        """Fault-tolerant variant of :meth:`_plan_load` for ``on_error='skip'|'warn'``."""
        with self._lock:
            loaded_existing = {
                n: s.plugin for n, s in self._plugins.items() if s.state != PluginState.UNLOADED
            }

        # 1) Deduplicate and drop already-loaded plugins.
        batch: list[Plugin] = []
        seen: set[str] = set()
        for p in plugins:
            name = p.manifest.name
            if name in loaded_existing:
                err = PluginAlreadyLoadedError(f"plugin {name!r} is already loaded")
                self._record_failure(source=name, error=err, phase="plan", plugin=p, mode=mode)
                continue
            if name in seen:
                err = PluginAlreadyLoadedError(f"plugin {name!r} appears twice in the load batch")
                self._record_failure(source=name, error=err, phase="plan", plugin=p, mode=mode)
                continue
            seen.add(name)
            batch.append(p)

        # 2) Iteratively resolve missing deps (via resolvers) or prune bad plugins.
        tried_resolvers: set[str] = set()
        resolver_rounds = 0
        while batch:
            try:
                return self._plan_load_once(batch)
            except PluginMissingDependencyError as exc:
                if (
                    self._resolvers
                    and exc.missing not in tried_resolvers
                    and resolver_rounds < self._resolver_max_rounds
                ):
                    tried_resolvers.add(exc.missing)
                    resolver_rounds += 1
                    extras = self._invoke_resolvers(exc.missing)
                    existing = {p.manifest.name for p in batch} | set(loaded_existing)
                    added = False
                    for extra in extras:
                        if extra.manifest.name not in existing:
                            batch.append(extra)
                            existing.add(extra.manifest.name)
                            added = True
                    if added:
                        continue
                self._prune_offending_plugin(batch, exc, mode=mode)
            except PluginError as exc:
                self._prune_offending_plugin(batch, exc, mode=mode)
        return []

    def _prune_offending_plugin(
        self,
        batch: list[Plugin],
        exc: PluginError,
        *,
        mode: OnLoadError,
    ) -> None:
        consumer = getattr(exc, "consumer", None)
        if consumer is not None:
            for idx, p in enumerate(batch):
                if p.manifest.name == consumer:
                    removed = batch.pop(idx)
                    self._record_failure(
                        source=consumer,
                        error=exc,
                        phase="plan",
                        plugin=removed,
                        mode=mode,
                    )
                    return
            # Consumer may be an UNLOADED dep pulled in by _expand_with_unloaded_deps.
            with self._lock:
                slot = self._plugins.get(consumer)
                if slot is not None and slot.state == PluginState.UNLOADED:
                    self._plugins.pop(consumer, None)
                    unloaded_plugin = slot.plugin
                else:
                    unloaded_plugin = None
            if unloaded_plugin is not None:
                self._record_failure(
                    source=consumer,
                    error=exc,
                    phase="plan",
                    plugin=unloaded_plugin,
                    mode=mode,
                )
                return

        # Fallback (e.g. cyclic dependency): drop all batch members mentioned as a distinct token,
        # or the last plugin in batch if none matched.
        msg = str(exc)
        dropped = False
        for p in list(batch):
            pattern = rf"(?:^|[^A-Za-z0-9_.-]){re.escape(p.manifest.name)}(?:[^A-Za-z0-9_.-]|$)"
            if re.search(pattern, msg):
                batch.remove(p)
                self._record_failure(
                    source=p.manifest.name,
                    error=exc,
                    phase="plan",
                    plugin=p,
                    mode=mode,
                )
                dropped = True
        if not dropped and batch:
            removed = batch.pop()
            self._record_failure(
                source=removed.manifest.name,
                error=exc,
                phase="plan",
                plugin=removed,
                mode=mode,
            )

    def _invoke_resolvers(self, missing: str) -> list[Plugin]:
        """Try each registered resolver for ``missing``; first non-empty result wins."""
        for resolver in self._resolvers:
            result = resolver(missing)
            if result:
                return list(result)
        return []

    # ---------- resolver registration ----------

    def add_resolver(self, resolver: PluginResolver) -> None:
        """Register a callable that fetches a missing dependency by name.

        Resolvers are tried in registration order during :meth:`load_all`
        whenever a hard ``requires`` entry cannot be satisfied from the
        current batch + already-loaded set. The first resolver returning
        a non-empty list contributes its plugins to the batch and planning
        retries.
        """
        self._resolvers.append(resolver)

    def use_entry_point_resolver(self, group: str) -> None:
        """Convenience: register a resolver that looks up missing deps in ``group``.

        Entry point name is matched against the plugin's manifest name
        after scanning. Useful when the host distributes plugins as
        installable packages — declare deps by name and let setuptools
        do the rest.
        """
        cache: dict[str, list[Plugin]] = {}

        def _resolve(missing: str) -> list[Plugin] | None:
            if not cache:
                for _ep_name, module in iter_entry_points(group):
                    for p in scan_module(module):
                        cache.setdefault(p.manifest.name, []).append(p)
            return cache.get(missing)

        self.add_resolver(_resolve)

    def use_directory_resolver(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
    ) -> None:
        """Convenience: register a resolver that scans ``path`` for missing deps.

        Files under ``path`` are imported lazily on first miss; results are
        cached by plugin name so subsequent misses are cheap.
        """
        cache: dict[str, list[Plugin]] = {}
        scanned = [False]

        def _resolve(missing: str) -> list[Plugin] | None:
            if not scanned[0]:
                for module in iter_directory_modules(path, recursive=recursive, pattern=pattern):
                    for p in scan_module(module):
                        cache.setdefault(p.manifest.name, []).append(p)
                scanned[0] = True
            return cache.get(missing)

        self.add_resolver(_resolve)

    def _call_lifecycle(self, plugin: Plugin, method_name: str, ctx: PluginContext) -> Any:
        """Invoke a plugin lifecycle method, injecting any declared-dep parameters.

        Parameters past ``ctx`` whose names match an entry in the
        plugin's ``requires`` / ``optional_requires`` / ``peer_requires``
        are auto-filled with ``ctx.api_of(name)``. Unrecognised parameter
        names are left unbound, surfacing as a standard ``TypeError``
        from the call — that's the author's signal to declare the dep
        or remove the param.

        Decorator-form plugins introspect the wrapped ``setup`` callable
        rather than the synthetic ``_DecoratedPlugin.on_load`` so the
        user's signature is what matters.
        """
        if isinstance(plugin, _DecoratedPlugin) and method_name == "on_load":
            target = plugin._setup
            kwargs = _build_injection_kwargs(plugin, target, ctx)
            return target(ctx, **kwargs)
        method = getattr(plugin, method_name)
        kwargs = _build_injection_kwargs(plugin, method, ctx)
        return method(ctx, **kwargs)

    @staticmethod
    def _build_declarative_slot_infos(plugin: Plugin) -> list[PluginSlotInfo]:
        name = plugin.manifest.name
        out: list[PluginSlotInfo] = []
        for slot_name, entries in plugin._declarative_slots.items():
            for entry_name, provider in entries.items():
                out.append(
                    PluginSlotInfo(
                        slot=slot_name,
                        name=entry_name,
                        provider=provider,
                        plugin=name,
                    )
                )
        return out

    def _build_ctx(self, plugin: Plugin) -> PluginContext:
        name = plugin.manifest.name
        # ``setdefault`` guarantees ``ctx.config is self._configs[name]`` —
        # which is what makes :meth:`update_config` propagate without a reload.
        cfg = self._configs.setdefault(name, {})
        ctx = self._context_class(
            plugin.manifest,
            registry=self._hooks_registry,
            config=cfg,
            tasky=self._tasky,
            scheduler=self._scheduler,
        )
        # When the plugin declares a config_model, validate eagerly so a
        # bad config surfaces before on_load runs against bogus values.
        if plugin.config_model is not None:
            try:
                ctx._config_model = _validate_against_model(cfg, plugin.config_model)
            except PluginManifestError as exc:
                raise PluginConfigValidationError(
                    f"config for plugin {name!r} failed validation: {exc}"
                ) from exc
        # Class-level ``events`` → one HookPoint per entry, namespaced.
        declared_events = plugin.events
        if declared_events:
            from pyhooky import HookPoint

            for key, schema in declared_events.items():
                target = f"{name}:{key}"
                ctx._events[key] = HookPoint(target, schema, registry=self._hooks_registry)
        # Populate declarative slots so they are visible before on_load runs.
        ctx._slot_infos.extend(self._build_declarative_slot_infos(plugin))
        return ctx

    def _wire_scheduled_tasks(self, plugin: Plugin) -> None:
        """Bind any task with triggers / cron to the scheduler.

        Called from ``_do_enable`` / ``_ado_enable`` after ``on_enable`` has
        run. No-op when the manager has no scheduler configured, or when
        the plugin registered no tasks with triggers or cron.

        Jobs returned by the scheduler are stored on ``_scheduled_jobs`` so
        :meth:`_unwire_scheduled_tasks` can cancel them on disable.
        """
        scheduler = self._scheduler
        if scheduler is None:
            return
        name = plugin.manifest.name
        slot = self._plugins.get(name)
        if slot is None:
            return
        triggered: list[Any] = []
        cron_pairs: list[tuple[str, Any]] = []
        for info in slot.ctx.tasks:
            task = info.task
            if getattr(task, "triggers", None):
                triggered.append(task)
            cron_expr = getattr(task, "cron", None)
            if cron_expr:
                cron_pairs.append((cron_expr, task))
        jobs: list[Any] = []
        if triggered:
            jobs.extend(scheduler.bind_tasks(*triggered))
        for expr, task in cron_pairs:
            jobs.append(scheduler.cron(expr).do(task))
        if jobs:
            self._scheduled_jobs.setdefault(name, []).extend(jobs)

    def _unwire_scheduled_tasks(self, plugin: Plugin) -> None:
        """Cancel jobs registered for ``plugin`` and clear bookkeeping.

        Called from ``_do_disable`` / ``_ado_disable`` before the task list
        is cleared. Safe to call when ``_scheduler`` is None or no jobs
        were registered.
        """
        if self._scheduler is None:
            return
        jobs = self._scheduled_jobs.pop(plugin.manifest.name, None)
        if not jobs:
            return
        for job in jobs:
            try:
                self._scheduler.cancel(job)
            except Exception:
                _logger.exception(
                    "pyplugy: scheduler.cancel() raised for plugin %r — continuing",
                    plugin.manifest.name,
                )

    @staticmethod
    def _reject_coroutine(result: Any, name: str, op: str) -> None:
        """Sync lifecycle path — surface coroutine returns instead of silently leaking them."""
        if inspect.iscoroutine(result):
            result.close()
            raise PluginLoadError(
                f"{op} for plugin {name!r} returned a coroutine; use the async "
                f"variant (aload/aenable/adisable/aunload) to drive async lifecycle methods."
            )

    def _do_load(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        try:
            ctx = self._build_ctx(plugin)
        except Exception as exc:
            self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
            with self._lock:
                self._plugins.pop(name, None)
            raise
        slot = _Slot(plugin, ctx)
        with self._lock:
            self._plugins[name] = slot
        token = _current_manager.set(self)
        try:
            with _activate(ctx), tag_scope(name):
                try:
                    result = self._call_lifecycle(plugin, "on_load", ctx)
                    self._reject_coroutine(result, name, "on_load")
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    self._hooks_registry.clear_tag(name)
                    with self._lock:
                        self._plugins.pop(name, None)
                    if isinstance(exc, PluginSlotConflictError):
                        raise
                    raise PluginLoadError(f"on_load failed for plugin {name!r}: {exc}") from exc
            slot.state = PluginState.LOADED
            self._hooks_registry.trigger(HOOK_PLUGIN_LOAD, plugin)
        finally:
            _current_manager.reset(token)

    def _do_enable(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.ENABLED:
                return
        token = _current_manager.set(self)
        try:
            if slot.state == PluginState.DISABLED:
                ctx = self._build_ctx(plugin)
                slot.ctx = ctx
                with _activate(ctx), tag_scope(name):
                    try:
                        result = self._call_lifecycle(plugin, "on_load", ctx)
                        self._reject_coroutine(result, name, "on_load")
                    except Exception as exc:
                        self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                        self._hooks_registry.clear_tag(name)
                        if isinstance(exc, PluginSlotConflictError):
                            raise
                        raise PluginLoadError(
                            f"re-load failed during enable() for {name!r}: {exc}"
                        ) from exc
            with _activate(slot.ctx), tag_scope(name):
                try:
                    result = self._call_lifecycle(plugin, "on_enable", slot.ctx)
                    self._reject_coroutine(result, name, "on_enable")
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_enable failed for plugin {name!r}: {exc}") from exc
            slot.state = PluginState.ENABLED
            self._wire_scheduled_tasks(plugin)
            self._hooks_registry.trigger(HOOK_PLUGIN_ENABLE, plugin)
        finally:
            _current_manager.reset(token)

    def _do_disable(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED, PluginState.UNLOADED):
                return
        token = _current_manager.set(self)
        try:
            with _activate(slot.ctx):
                try:
                    result = self._call_lifecycle(plugin, "on_disable", slot.ctx)
                    self._reject_coroutine(result, name, "on_disable")
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_disable failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: disabled %r — cleared %d hook(s)", name, len(removed))
            self._unwire_scheduled_tasks(plugin)
            slot.ctx._task_infos.clear()
            slot.ctx._slot_infos.clear()
            slot.ctx._slot_infos.extend(self._build_declarative_slot_infos(plugin))
            slot.state = PluginState.DISABLED
            self._hooks_registry.trigger(HOOK_PLUGIN_DISABLE, plugin)
        finally:
            _current_manager.reset(token)

    def _do_unload(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
        token = _current_manager.set(self)
        try:
            self._hooks_registry.trigger(HOOK_PLUGIN_UNLOAD, plugin)
            with _activate(slot.ctx):
                try:
                    result = self._call_lifecycle(plugin, "on_unload", slot.ctx)
                    self._reject_coroutine(result, name, "on_unload")
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginUnloadError(f"on_unload failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: unloaded %r — cleared %d hook(s)", name, len(removed))
            with self._lock:
                self._plugins.pop(name, None)
        finally:
            _current_manager.reset(token)

    # ---------- async lifecycle internals ----------

    @staticmethod
    async def _maybe_await(result: Any) -> None:
        """Await ``result`` if it is a coroutine; otherwise treat as already-completed sync work."""
        if inspect.iscoroutine(result):
            await result

    async def _ado_load(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        try:
            ctx = self._build_ctx(plugin)
        except Exception as exc:
            self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
            with self._lock:
                self._plugins.pop(name, None)
            raise
        slot = _Slot(plugin, ctx)
        with self._lock:
            self._plugins[name] = slot
        token = _current_manager.set(self)
        try:
            with _activate(ctx), tag_scope(name):
                try:
                    await self._maybe_await(self._call_lifecycle(plugin, "on_load", ctx))
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    self._hooks_registry.clear_tag(name)
                    with self._lock:
                        self._plugins.pop(name, None)
                    if isinstance(exc, PluginSlotConflictError):
                        raise
                    raise PluginLoadError(f"on_load failed for plugin {name!r}: {exc}") from exc
            slot.state = PluginState.LOADED
            self._hooks_registry.trigger(HOOK_PLUGIN_LOAD, plugin)
        finally:
            _current_manager.reset(token)

    async def _ado_enable(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.ENABLED:
                return
        token = _current_manager.set(self)
        try:
            if slot.state == PluginState.DISABLED:
                ctx = self._build_ctx(plugin)
                slot.ctx = ctx
                with _activate(ctx), tag_scope(name):
                    try:
                        await self._maybe_await(self._call_lifecycle(plugin, "on_load", ctx))
                    except Exception as exc:
                        self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                        self._hooks_registry.clear_tag(name)
                        if isinstance(exc, PluginSlotConflictError):
                            raise
                        raise PluginLoadError(
                            f"re-load failed during enable() for {name!r}: {exc}"
                        ) from exc
            with _activate(slot.ctx), tag_scope(name):
                try:
                    await self._maybe_await(self._call_lifecycle(plugin, "on_enable", slot.ctx))
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_enable failed for plugin {name!r}: {exc}") from exc
            slot.state = PluginState.ENABLED
            self._wire_scheduled_tasks(plugin)
            self._hooks_registry.trigger(HOOK_PLUGIN_ENABLE, plugin)
        finally:
            _current_manager.reset(token)

    async def _ado_disable(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED, PluginState.UNLOADED):
                return
        token = _current_manager.set(self)
        try:
            with _activate(slot.ctx):
                try:
                    await self._maybe_await(self._call_lifecycle(plugin, "on_disable", slot.ctx))
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_disable failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: disabled %r — cleared %d hook(s)", name, len(removed))
            self._unwire_scheduled_tasks(plugin)
            slot.ctx._task_infos.clear()
            slot.ctx._slot_infos.clear()
            slot.ctx._slot_infos.extend(self._build_declarative_slot_infos(plugin))
            slot.state = PluginState.DISABLED
            self._hooks_registry.trigger(HOOK_PLUGIN_DISABLE, plugin)
        finally:
            _current_manager.reset(token)

    async def _ado_unload(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        with self._lock:
            slot = self._require_slot(name)
        token = _current_manager.set(self)
        try:
            self._hooks_registry.trigger(HOOK_PLUGIN_UNLOAD, plugin)
            with _activate(slot.ctx):
                try:
                    await self._maybe_await(self._call_lifecycle(plugin, "on_unload", slot.ctx))
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginUnloadError(f"on_unload failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: unloaded %r — cleared %d hook(s)", name, len(removed))
            with self._lock:
                self._plugins.pop(name, None)
        finally:
            _current_manager.reset(token)
