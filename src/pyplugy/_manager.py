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
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pyhooky import HookRegistry, get_default_registry, tag_scope

from pyplugy._context import PluginContext, _activate, _validate_against_model
from pyplugy._depgraph import compute_reverse_graph, parse_requirement, resolve_load_order
from pyplugy._discovery import (
    import_file,
    iter_directory_modules,
    iter_entry_points,
    scan_module,
)
from pyplugy._plugin import Plugin, PluginInfo, PluginState, _DecoratedPlugin, _info_for
from pyplugy.exceptions import (
    PluginAlreadyLoadedError,
    PluginConfigValidationError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginManifestError,
    PluginMissingDependencyError,
    PluginNotFoundError,
    PluginUnloadError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

    from pyplugy._tasky_protocol import SchedulerProtocol, TaskyProtocol

    PluginResolver = Callable[[str], "list[Plugin] | None"]


__all__ = [
    "HOOK_PLUGIN_CONFIG_CHANGED",
    "HOOK_PLUGIN_DISABLE",
    "HOOK_PLUGIN_ENABLE",
    "HOOK_PLUGIN_ERROR",
    "HOOK_PLUGIN_LOAD",
    "HOOK_PLUGIN_UNLOAD",
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
        "_lock",
        "_plugins",
        "_resolver_max_rounds",
        "_resolvers",
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
    ) -> None:
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
        loaded = self.load_all(plugins, enable=enable)
        return loaded[0]

    def load_all(self, plugins: list[Plugin], *, enable: bool = True) -> list[Plugin]:
        """Load several plugins in dependency order."""
        ordered = self._plan_load(plugins)
        for p in ordered:
            self._do_load(p)
            if enable:
                self._do_enable(p)
        return ordered

    def load_all_from_module(self, module: ModuleType, *, enable: bool = True) -> list[Plugin]:
        """Scan ``module`` for plugins and load every one found."""
        return self.load_all(scan_module(module), enable=enable)

    def load_entry_points(self, group: str, *, enable: bool = True) -> list[Plugin]:
        """Load plugins from every entry point in ``group``.

        Each entry point's module is scanned via :func:`scan_module`. Entry
        points that fail to load raise :class:`PluginLoadError` with the
        underlying exception chained.
        """
        collected: list[Plugin] = []
        for ep_name, module in iter_entry_points(group):
            try:
                collected.extend(scan_module(module))
            except Exception as exc:
                raise PluginLoadError(
                    f"failed to scan entry point {ep_name!r} (group {group!r}): {exc}"
                ) from exc
        return self.load_all(collected, enable=enable)

    def load_directory(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
        enable: bool = True,
    ) -> list[Plugin]:
        """Import every matching file under ``path`` and load discovered plugins."""
        collected: list[Plugin] = []
        for module in iter_directory_modules(path, recursive=recursive, pattern=pattern):
            collected.extend(scan_module(module))
        return self.load_all(collected, enable=enable)

    # ---------- async loading helpers ----------

    async def aload(self, target: Any, *, enable: bool = True) -> Plugin:
        """Async variant of :meth:`load` — awaits ``async def`` lifecycle methods."""
        plugins = self._coerce_to_plugins(target)
        if not plugins:
            raise PluginLoadError(f"no plugins found in target {target!r}")
        loaded = await self.aload_all(plugins, enable=enable)
        return loaded[0]

    async def aload_all(self, plugins: list[Plugin], *, enable: bool = True) -> list[Plugin]:
        """Async variant of :meth:`load_all`."""
        ordered = self._plan_load(plugins)
        for p in ordered:
            await self._ado_load(p)
            if enable:
                await self._ado_enable(p)
        return ordered

    async def aload_all_from_module(
        self, module: ModuleType, *, enable: bool = True
    ) -> list[Plugin]:
        """Async variant of :meth:`load_all_from_module`."""
        return await self.aload_all(scan_module(module), enable=enable)

    async def aload_entry_points(self, group: str, *, enable: bool = True) -> list[Plugin]:
        """Async variant of :meth:`load_entry_points`."""
        collected: list[Plugin] = []
        for ep_name, module in iter_entry_points(group):
            try:
                collected.extend(scan_module(module))
            except Exception as exc:
                raise PluginLoadError(
                    f"failed to scan entry point {ep_name!r} (group {group!r}): {exc}"
                ) from exc
        return await self.aload_all(collected, enable=enable)

    async def aload_directory(
        self,
        path: Path,
        *,
        recursive: bool = False,
        pattern: str = "*.py",
        enable: bool = True,
    ) -> list[Plugin]:
        """Async variant of :meth:`load_directory`."""
        collected: list[Plugin] = []
        for module in iter_directory_modules(path, recursive=recursive, pattern=pattern):
            collected.extend(scan_module(module))
        return await self.aload_all(collected, enable=enable)

    # ---------- lifecycle ----------

    def enable(self, name: str) -> None:
        """Transition a DISABLED plugin to ENABLED by re-running its setup."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state == PluginState.ENABLED:
                return
            if slot.state == PluginState.UNLOADED:
                raise PluginError(f"plugin {name!r} is unloaded; load() it first")
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
            if slot.state in (PluginState.DISABLED, PluginState.LOADED):
                return
            if slot.state == PluginState.UNLOADED:
                raise PluginError(f"plugin {name!r} is unloaded; load() it first")
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
            if slot.state == PluginState.UNLOADED:
                raise PluginError(f"plugin {name!r} is unloaded; load() it first")
        await self._ado_enable(slot.plugin)

    async def adisable(self, name: str, *, cascade: bool = False) -> None:
        """Async variant of :meth:`disable`."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED):
                return
            if slot.state == PluginState.UNLOADED:
                raise PluginError(f"plugin {name!r} is unloaded; load() it first")
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
            model_type = slot.plugin.config_model if slot is not None else None
            if model_type is not None:
                try:
                    validated = _validate_against_model(cfg, model_type)
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

    def plugin_tasks(self, name: str) -> list[str]:
        """Task names owned by ``name``."""
        with self._lock:
            slot = self._require_slot(name)
            return [getattr(t, "name", repr(t)) for t in slot.ctx.tasks]

    def list_plugins(self) -> list[PluginInfo]:
        with self._lock:
            slots = list(self._plugins.values())
        out: list[PluginInfo] = []
        for slot in slots:
            targets = tuple(self.plugin_targets(slot.plugin.manifest.name))
            tasks = tuple(self.plugin_tasks(slot.plugin.manifest.name))
            out.append(_info_for(slot.plugin, state=slot.state, targets=targets, tasks=tasks))
        return out

    def dump(self) -> dict[str, Any]:
        """Return a structured snapshot — handy for CLIs and debug output."""
        return {
            "registry": self._hooks_registry.name,
            "plugins": [
                {
                    "name": info.name,
                    "version": info.version,
                    "state": info.state.value,
                    "requires": list(info.requires),
                    "optional_requires": list(info.optional_requires),
                    "conflicts": list(info.conflicts),
                    "description": info.description,
                    "author": info.author,
                    "tags": list(info.tags),
                    "targets": list(info.targets),
                    "tasks": list(info.tasks),
                }
                for info in self.list_plugins()
            ],
            "graph": self.dependency_graph(),
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

    def _plan_load(self, plugins: list[Plugin]) -> list[Plugin]:
        """Validate uniqueness, merge with already-loaded plugins, return dep-ordered list.

        If a hard dependency is missing and resolvers are registered, this
        method calls each resolver with the missing name and retries
        planning. Capped at ``self._resolver_max_rounds`` rounds to surface
        runaway resolvers.
        """
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
                existing = {p.manifest.name for p in batch} | set(self._plugins)
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

    def _plan_load_once(self, plugins: list[Plugin]) -> list[Plugin]:
        with self._lock:
            already: set[str] = set(self._plugins.keys())
            seen: set[str] = set()
            for p in plugins:
                name = p.manifest.name
                if name in already:
                    raise PluginAlreadyLoadedError(f"plugin {name!r} is already loaded")
                if name in seen:
                    raise PluginAlreadyLoadedError(
                        f"plugin {name!r} appears twice in the load batch"
                    )
                seen.add(name)
            # Topo-sort considers both new plugins (for cross-deps in the batch)
            # AND already-loaded ones (so a new plugin can depend on an existing one).
            combined = list(plugins) + [s.plugin for s in self._plugins.values()]
            ordered_combined = resolve_load_order(combined)
            # Keep only the new ones, in the resolved order.
            new_names = {p.manifest.name for p in plugins}
            return [p for p in ordered_combined if p.manifest.name in new_names]

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

    def _call_lifecycle(
        self, plugin: Plugin, method_name: str, ctx: PluginContext
    ) -> Any:
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
                ctx._config_model = _validate_against_model(
                    cfg, plugin.config_model
                )
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
                ctx._events[key] = HookPoint(
                    target, schema, registry=self._hooks_registry
                )
        return ctx

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
        ctx = self._build_ctx(plugin)
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
            slot.ctx._tasks.clear()
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
        ctx = self._build_ctx(plugin)
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
                        raise PluginLoadError(
                            f"re-load failed during enable() for {name!r}: {exc}"
                        ) from exc
            with _activate(slot.ctx), tag_scope(name):
                try:
                    await self._maybe_await(
                        self._call_lifecycle(plugin, "on_enable", slot.ctx)
                    )
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_enable failed for plugin {name!r}: {exc}") from exc
            slot.state = PluginState.ENABLED
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
                    await self._maybe_await(
                        self._call_lifecycle(plugin, "on_disable", slot.ctx)
                    )
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(f"on_disable failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: disabled %r — cleared %d hook(s)", name, len(removed))
            slot.ctx._tasks.clear()
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
                    await self._maybe_await(
                        self._call_lifecycle(plugin, "on_unload", slot.ctx)
                    )
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginUnloadError(f"on_unload failed for plugin {name!r}: {exc}") from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug("pyplugy: unloaded %r — cleared %d hook(s)", name, len(removed))
            with self._lock:
                self._plugins.pop(name, None)
        finally:
            _current_manager.reset(token)
