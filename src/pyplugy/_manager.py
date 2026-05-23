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

import logging
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pyhooky import HookRegistry, get_default_registry, tag_scope

from pyplugy._context import PluginContext, _activate
from pyplugy._depgraph import resolve_load_order
from pyplugy._discovery import (
    import_file,
    iter_directory_modules,
    iter_entry_points,
    scan_module,
)
from pyplugy._plugin import Plugin, PluginInfo, PluginState, _info_for
from pyplugy.exceptions import (
    PluginAlreadyLoadedError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginNotFoundError,
    PluginUnloadError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from types import ModuleType

    from pyplugy._tasky_protocol import SchedulerProtocol, TaskyProtocol


__all__ = ["PluginManager", "current_manager"]


HOOK_PLUGIN_LOAD = "pyplugy:plugin:load"
HOOK_PLUGIN_UNLOAD = "pyplugy:plugin:unload"
HOOK_PLUGIN_ENABLE = "pyplugy:plugin:enable"
HOOK_PLUGIN_DISABLE = "pyplugy:plugin:disable"
HOOK_PLUGIN_ERROR = "pyplugy:plugin:error"


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


class _Slot:
    """Internal per-plugin bookkeeping the manager keeps in its table."""

    __slots__ = ("config", "ctx", "plugin", "state")

    def __init__(
        self,
        plugin: Plugin,
        ctx: PluginContext,
        config: Mapping[str, Any] | None,
    ) -> None:
        self.plugin = plugin
        self.ctx = ctx
        self.state: PluginState = PluginState.UNLOADED
        self.config: dict[str, Any] = dict(config or {})


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
        "_hooks_registry",
        "_lock",
        "_plugins",
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
    ) -> None:
        self._hooks_registry: HookRegistry = registry or get_default_registry()
        self._tasky = tasky
        self._scheduler = scheduler
        self._plugins: dict[str, _Slot] = {}
        self._configs: dict[str, dict[str, Any]] = {
            name: dict(cfg) for name, cfg in (configs or {}).items()
        }
        self._lock = threading.RLock()

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

    def disable(self, name: str) -> None:
        """Transition an ENABLED plugin to DISABLED by tearing down its hooks/tasks."""
        with self._lock:
            slot = self._require_slot(name)
            if slot.state in (PluginState.DISABLED, PluginState.LOADED):
                return
            if slot.state == PluginState.UNLOADED:
                raise PluginError(f"plugin {name!r} is unloaded; load() it first")
        self._do_disable(slot.plugin)

    def unload(self, name: str) -> None:
        """Fully unload a plugin: tear down hooks/tasks, run ``on_unload``, drop the slot."""
        with self._lock:
            slot = self._require_slot(name)
        # Disable first if currently enabled — keeps ``on_disable`` semantics tight.
        if slot.state == PluginState.ENABLED:
            self._do_disable(slot.plugin)
        self._do_unload(slot.plugin)

    def reload(self, name: str) -> Plugin:
        """Convenience: :meth:`unload` then :meth:`load` the same :class:`Plugin` instance."""
        with self._lock:
            slot = self._require_slot(name)
            plugin = slot.plugin
        self.unload(name)
        return self.load(plugin)

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
                    "description": info.description,
                    "author": info.author,
                    "tags": list(info.tags),
                    "targets": list(info.targets),
                    "tasks": list(info.tasks),
                }
                for info in self.list_plugins()
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

    # ---------- internals ----------

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
        """Validate uniqueness, merge with already-loaded plugins, return dep-ordered list."""
        with self._lock:
            already: set[str] = set(self._plugins.keys())
            seen: set[str] = set()
            for p in plugins:
                name = p.manifest.name
                if name in already:
                    raise PluginAlreadyLoadedError(
                        f"plugin {name!r} is already loaded"
                    )
                if name in seen:
                    raise PluginAlreadyLoadedError(
                        f"plugin {name!r} appears twice in the load batch"
                    )
                seen.add(name)
            # Topo-sort considers both new plugins (for cross-deps in the batch)
            # AND already-loaded ones (so a new plugin can depend on an existing one).
            combined = list(plugins) + [s.plugin for s in self._plugins.values()]
            try:
                ordered_combined = resolve_load_order(combined)
            except PluginDependencyError:
                raise
            # Keep only the new ones, in the resolved order.
            new_names = {p.manifest.name for p in plugins}
            return [p for p in ordered_combined if p.manifest.name in new_names]

    def _build_ctx(self, plugin: Plugin) -> PluginContext:
        name = plugin.manifest.name
        cfg = self._configs.get(name, {})
        return PluginContext(
            plugin.manifest,
            registry=self._hooks_registry,
            config=cfg,
            tasky=self._tasky,
            scheduler=self._scheduler,
        )

    def _do_load(self, plugin: Plugin) -> None:
        name = plugin.manifest.name
        ctx = self._build_ctx(plugin)
        slot = _Slot(plugin, ctx, ctx.config)
        with self._lock:
            self._plugins[name] = slot
        token = _current_manager.set(self)
        try:
            with _activate(ctx), tag_scope(name):
                try:
                    plugin.on_load(ctx)
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    self._hooks_registry.clear_tag(name)
                    with self._lock:
                        self._plugins.pop(name, None)
                    raise PluginLoadError(
                        f"on_load failed for plugin {name!r}: {exc}"
                    ) from exc
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
        # If transitioning from DISABLED, re-run setup to re-register hooks/tasks.
        token = _current_manager.set(self)
        try:
            if slot.state == PluginState.DISABLED:
                ctx = self._build_ctx(plugin)
                slot.ctx = ctx
                with _activate(ctx), tag_scope(name):
                    try:
                        plugin.on_load(ctx)
                    except Exception as exc:
                        self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                        self._hooks_registry.clear_tag(name)
                        raise PluginLoadError(
                            f"re-load failed during enable() for {name!r}: {exc}"
                        ) from exc
            with _activate(slot.ctx), tag_scope(name):
                try:
                    plugin.on_enable(slot.ctx)
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(
                        f"on_enable failed for plugin {name!r}: {exc}"
                    ) from exc
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
                    plugin.on_disable(slot.ctx)
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginLoadError(
                        f"on_disable failed for plugin {name!r}: {exc}"
                    ) from exc
            # Tear down all hooks tagged with the plugin's name.
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug(
                "pyplugy: disabled %r — cleared %d hook(s)", name, len(removed)
            )
            # Reset the context's task list — tasks are considered torn down.
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
                    plugin.on_unload(slot.ctx)
                except Exception as exc:
                    self._hooks_registry.trigger(HOOK_PLUGIN_ERROR, plugin, exc)
                    raise PluginUnloadError(
                        f"on_unload failed for plugin {name!r}: {exc}"
                    ) from exc
            removed = self._hooks_registry.clear_tag(name)
            _logger.debug(
                "pyplugy: unloaded %r — cleared %d hook(s)", name, len(removed)
            )
            with self._lock:
                self._plugins.pop(name, None)
        finally:
            _current_manager.reset(token)
