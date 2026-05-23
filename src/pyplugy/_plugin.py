"""Plugin value-objects and the ``@plugin`` decorator.

A *plugin* is a manifest (name/version/requires/...) plus a setup callable
that registers hooks and tasks against a :class:`~pyplugy._context.PluginContext`.
Two authoring forms produce equivalent :class:`Plugin` instances:

- The :func:`plugin` decorator wraps a ``setup(ctx)`` function.
- Subclassing :class:`Plugin` lets the author override
  ``on_load`` / ``on_unload`` / ``on_enable`` / ``on_disable`` plus declare
  manifest fields as class attributes.

The :class:`PluginManager` only ever sees :class:`Plugin` instances —
the decorator is a thin façade around the class form.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, overload

from pyplugy.exceptions import PluginManifestError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyplugy._context import PluginContext


__all__ = [
    "Plugin",
    "PluginInfo",
    "PluginManifest",
    "PluginState",
    "plugin",
]


class PluginState(StrEnum):
    """Lifecycle states a plugin transitions through inside a manager."""

    UNLOADED = "unloaded"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Immutable plugin metadata.

    ``requires`` is a list of `PEP 440 <https://peps.python.org/pep-0440/>`_
    specifiers prefixed with another plugin's name, e.g. ``"auth>=1.0"``.
    The leading identifier is the dependency name; the rest (optional) is a
    :class:`packaging.specifiers.SpecifierSet`. A bare name (``"auth"``) means
    "any version".
    """

    name: str
    version: str = "0.0.0"
    requires: tuple[str, ...] = ()
    description: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """Snapshot of a plugin's manifest + runtime state, returned by introspection helpers."""

    name: str
    version: str
    state: PluginState
    requires: tuple[str, ...]
    description: str
    author: str
    tags: tuple[str, ...]
    targets: tuple[str, ...]
    tasks: tuple[str, ...]


def _coerce_manifest(
    name: str,
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    description: str = "",
    author: str = "",
    tags: tuple[str, ...] | list[str] | None = None,
) -> PluginManifest:
    if not isinstance(name, str) or not name:
        raise PluginManifestError(
            f"plugin name must be a non-empty string, got {name!r}"
        )
    return PluginManifest(
        name=name,
        version=version,
        requires=tuple(requires or ()),
        description=description,
        author=author,
        tags=tuple(tags or ()),
    )


class Plugin:
    """Base class for the *class form* of a plugin.

    Subclasses declare manifest fields as class attributes::

        class MyPlugin(Plugin):
            name = "my-plugin"
            version = "1.0.0"
            requires = ["other>=2.0"]

            def on_load(self, ctx: PluginContext) -> None: ...

    ``on_load`` is the one method every plugin should override — that's where
    hooks and tasks get registered against ``ctx``. The other lifecycle hooks
    (``on_unload`` / ``on_enable`` / ``on_disable``) are no-ops by default.

    A plugin produced via the :func:`plugin` decorator is an instance of an
    internal subclass that forwards ``on_load`` to the wrapped ``setup``
    function — so a single :class:`Plugin`-shaped contract covers both forms.
    """

    name: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    requires: ClassVar[tuple[str, ...] | list[str]] = ()
    description: ClassVar[str] = ""
    author: ClassVar[str] = ""
    tags: ClassVar[tuple[str, ...] | list[str]] = ()

    def __init__(self, manifest: PluginManifest | None = None) -> None:
        if manifest is None:
            manifest = _coerce_manifest(
                self.name,
                version=self.version,
                requires=tuple(self.requires),
                description=self.description,
                author=self.author,
                tags=tuple(self.tags),
            )
        self._manifest = manifest

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    # ---------- lifecycle hooks (override in subclasses) ----------

    def on_load(self, ctx: PluginContext) -> None:
        """Register hooks/tasks. Called inside a ``tag_scope(self.manifest.name)`` block."""

    def on_unload(self, ctx: PluginContext) -> None:
        """Release external resources.

        Hooks/tasks owned by the plugin are torn down by the manager.
        """

    def on_enable(self, ctx: PluginContext) -> None:
        """Called when the plugin transitions DISABLED → ENABLED."""

    def on_disable(self, ctx: PluginContext) -> None:
        """Called when the plugin transitions ENABLED → DISABLED."""

    def __repr__(self) -> str:
        m = self._manifest
        return f"Plugin(name={m.name!r}, version={m.version!r})"


class _DecoratedPlugin(Plugin):
    """Concrete :class:`Plugin` whose :meth:`on_load` delegates to the wrapped setup."""

    __slots__ = ("_setup",)

    def __init__(self, manifest: PluginManifest, setup: Callable[[PluginContext], None]) -> None:
        super().__init__(manifest)
        self._setup = setup

    def on_load(self, ctx: PluginContext) -> None:
        self._setup(ctx)


@overload
def plugin(name: Callable[[PluginContext], None], /) -> Plugin: ...
@overload
def plugin(
    name: str,
    *,
    version: str = ...,
    requires: tuple[str, ...] | list[str] | None = ...,
    description: str = ...,
    author: str = ...,
    tags: tuple[str, ...] | list[str] | None = ...,
) -> Callable[[Callable[[PluginContext], None]], Plugin]: ...
def plugin(
    name: str | Callable[[PluginContext], None],
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    description: str = "",
    author: str = "",
    tags: tuple[str, ...] | list[str] | None = None,
) -> Plugin | Callable[[Callable[[PluginContext], None]], Plugin]:
    """Wrap a ``setup(ctx)`` callable into a :class:`Plugin`.

    Two call styles::

        @plugin("my-plugin", version="1.0.0", requires=["other>=2.0"])
        def setup(ctx: PluginContext) -> None:
            ...

        # bare form — name is taken from the function's ``__name__``
        @plugin
        def my_plugin(ctx: PluginContext) -> None:
            ...

    The returned object is a :class:`Plugin` instance whose :meth:`Plugin.on_load`
    forwards to ``setup``. Discovery (entry points / directory / JIT) treats
    these instances and :class:`Plugin` subclasses identically.
    """

    def _build(setup: Callable[[PluginContext], None], inferred_name: str | None = None) -> Plugin:
        resolved_name = inferred_name if inferred_name is not None else (
            name if isinstance(name, str) else ""
        )
        manifest = _coerce_manifest(
            resolved_name,
            version=version,
            requires=requires,
            description=description,
            author=author,
            tags=tags,
        )
        return _DecoratedPlugin(manifest, setup)

    if callable(name) and not isinstance(name, str):
        fn = name
        return _build(fn, inferred_name=getattr(fn, "__name__", ""))

    def decorator(fn: Callable[[PluginContext], None]) -> Plugin:
        return _build(fn)

    return decorator


def _info_for(
    plugin_obj: Plugin,
    *,
    state: PluginState,
    targets: tuple[str, ...],
    tasks: tuple[str, ...],
) -> PluginInfo:
    m = plugin_obj.manifest
    return PluginInfo(
        name=m.name,
        version=m.version,
        state=state,
        requires=m.requires,
        description=m.description,
        author=m.author,
        tags=m.tags,
        targets=targets,
        tasks=tasks,
    )
