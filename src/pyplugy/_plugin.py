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

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, overload

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from pyplugy.exceptions import PluginManifestError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pyplugy._context import PluginContext
    from pyplugy._task_info import PluginTaskInfo


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


class PluginManifest(BaseModel):
    """Immutable plugin metadata (pydantic-validated).

    ``requires`` is a list of `PEP 440 <https://peps.python.org/pep-0440/>`_
    specifiers prefixed with another plugin's name, e.g. ``"auth>=1.0"``.
    The leading identifier is the dependency name; the rest (optional) is a
    :class:`packaging.specifiers.SpecifierSet`. A bare name (``"auth"``) means
    "any version".

    ``optional_requires`` uses the same string shape as ``requires`` but
    declares a *soft* dependency: if the named plugin is present in the
    load batch (or already loaded) it influences ordering; if missing,
    planning proceeds without error.

    ``peer_requires`` is a *host-supplied* hard dependency: planning
    refuses to load the plugin if the named peer isn't already loaded
    or present in the same batch, and resolvers (entry points,
    directories) deliberately skip it. Use this when the host owns the
    dependency's lifecycle and shouldn't have it auto-pulled.

    ``conflicts`` is a tuple of plain plugin names — planning refuses
    to load a plugin alongside any name it lists in ``conflicts`` (and
    symmetrically refuses if a loaded plugin lists the new one).

    The model is ``frozen`` and ``extra="forbid"`` — unknown fields are
    a manifest error, surfaced via :class:`PluginManifestError`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = "0.0.0"
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    peer_requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    description: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("plugin name must be a non-empty string")
        return v

    @field_validator("requires", "optional_requires", "peer_requires")
    @classmethod
    def _validate_requirements(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # Lazy import to avoid the _depgraph <-> _plugin cycle at module load.
        from pyplugy._depgraph import parse_requirement
        from pyplugy.exceptions import PluginDependencyError

        for req in v:
            try:
                parse_requirement(req)
            except PluginDependencyError as exc:
                # Re-raise as ValueError so pydantic wraps it into ValidationError.
                # _coerce_manifest then surfaces it as PluginManifestError.
                raise ValueError(str(exc)) from exc
        return v


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """Snapshot of a plugin's manifest + runtime state, returned by introspection helpers."""

    name: str
    version: str
    state: PluginState
    requires: tuple[str, ...]
    optional_requires: tuple[str, ...]
    peer_requires: tuple[str, ...]
    conflicts: tuple[str, ...]
    description: str
    author: str
    tags: tuple[str, ...]
    targets: tuple[str, ...]
    tasks: tuple[PluginTaskInfo, ...]


_NAME_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _derive_plugin_name(class_name: str) -> str:
    """Convert a PascalCase class name to snake_case for use as a plugin name.

    Examples:

    - ``"Auth"`` → ``"auth"``
    - ``"AuthPlugin"`` → ``"auth_plugin"``
    - ``"HTTPAdapter"`` → ``"http_adapter"`` (collapses acronym runs)

    Returns the empty string when ``class_name`` itself is empty — the
    caller's downstream validation will reject that as before.
    """
    if not class_name:
        return ""
    return _NAME_BOUNDARY_RE.sub("_", class_name).lower()


def _coerce_manifest(
    name: str,
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    optional_requires: tuple[str, ...] | list[str] | None = None,
    peer_requires: tuple[str, ...] | list[str] | None = None,
    conflicts: tuple[str, ...] | list[str] | None = None,
    description: str = "",
    author: str = "",
    tags: tuple[str, ...] | list[str] | None = None,
) -> PluginManifest:
    if not isinstance(name, str) or not name:
        raise PluginManifestError(f"plugin name must be a non-empty string, got {name!r}")
    try:
        return PluginManifest(
            name=name,
            version=version,
            requires=tuple(requires or ()),
            optional_requires=tuple(optional_requires or ()),
            peer_requires=tuple(peer_requires or ()),
            conflicts=tuple(conflicts or ()),
            description=description,
            author=author,
            tags=tuple(tags or ()),
        )
    except ValidationError as exc:
        raise PluginManifestError(f"invalid manifest for {name!r}: {exc}") from exc


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
    optional_requires: ClassVar[tuple[str, ...] | list[str]] = ()
    peer_requires: ClassVar[tuple[str, ...] | list[str]] = ()
    conflicts: ClassVar[tuple[str, ...] | list[str]] = ()
    description: ClassVar[str] = ""
    author: ClassVar[str] = ""
    tags: ClassVar[tuple[str, ...] | list[str]] = ()

    # Optional pydantic schemas — when declared, the manager validates
    # ``ctx.config`` against ``config_model`` and ``ctx.export(obj)``
    # against ``api_model``. ``Any`` typing keeps the framework
    # independent of pydantic at the import-graph level for subclasses
    # that don't use them.
    config_model: ClassVar[Any | None] = None
    api_model: ClassVar[Any | None] = None

    # Typed events: ``{"login": LoginEvent}`` declared at class scope
    # turns into a ``HookPoint`` per entry, accessible via
    # ``ctx.events["login"]`` and named ``"<plugin_name>:login"`` in the
    # underlying registry. Optional — empty by default.
    events: ClassVar[dict[str, Any]] = {}

    def __init__(self, manifest: PluginManifest | None = None) -> None:
        if manifest is None:
            # When the subclass didn't set ``name``, derive one from the
            # class name (PascalCase → snake_case). This is a pure
            # ergonomic shortcut; ``_DecoratedPlugin`` passes its own
            # name in directly and never hits this branch.
            derived = self.name or _derive_plugin_name(type(self).__name__)
            manifest = _coerce_manifest(
                derived,
                version=self.version,
                requires=tuple(self.requires),
                optional_requires=tuple(self.optional_requires),
                peer_requires=tuple(self.peer_requires),
                conflicts=tuple(self.conflicts),
                description=self.description,
                author=self.author,
                tags=tuple(self.tags),
            )
        self._manifest = manifest

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    # ---------- lifecycle hooks (override in subclasses) ----------
    #
    # Each lifecycle method may be declared ``async def`` instead of ``def``.
    # The synchronous ``PluginManager.load`` / ``enable`` / ``disable`` /
    # ``unload`` entry points reject coroutine-returning lifecycle methods;
    # use the async variants (``aload``/``aenable``/``adisable``/``aunload``)
    # to drive plugins whose lifecycle is async.

    def on_load(self, ctx: PluginContext) -> None | Awaitable[None]:
        """Register hooks/tasks. Called inside a ``tag_scope(self.manifest.name)`` block."""

    def on_unload(self, ctx: PluginContext) -> None | Awaitable[None]:
        """Release external resources.

        Hooks/tasks owned by the plugin are torn down by the manager.
        """

    def on_enable(self, ctx: PluginContext) -> None | Awaitable[None]:
        """Called when the plugin transitions DISABLED → ENABLED."""

    def on_disable(self, ctx: PluginContext) -> None | Awaitable[None]:
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
def plugin(name: Callable[..., None], /) -> Plugin: ...
@overload
def plugin(
    name: str,
    *,
    version: str = ...,
    requires: tuple[str, ...] | list[str] | None = ...,
    optional_requires: tuple[str, ...] | list[str] | None = ...,
    peer_requires: tuple[str, ...] | list[str] | None = ...,
    conflicts: tuple[str, ...] | list[str] | None = ...,
    description: str = ...,
    author: str = ...,
    tags: tuple[str, ...] | list[str] | None = ...,
) -> Callable[[Callable[..., None]], Plugin]: ...
def plugin(
    name: str | Callable[[PluginContext], None],
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    optional_requires: tuple[str, ...] | list[str] | None = None,
    peer_requires: tuple[str, ...] | list[str] | None = None,
    conflicts: tuple[str, ...] | list[str] | None = None,
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
        resolved_name = (
            inferred_name if inferred_name is not None else (name if isinstance(name, str) else "")
        )
        manifest = _coerce_manifest(
            resolved_name,
            version=version,
            requires=requires,
            optional_requires=optional_requires,
            peer_requires=peer_requires,
            conflicts=conflicts,
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
    tasks: tuple[PluginTaskInfo, ...],
) -> PluginInfo:
    m = plugin_obj.manifest
    return PluginInfo(
        name=m.name,
        version=m.version,
        state=state,
        requires=m.requires,
        optional_requires=m.optional_requires,
        peer_requires=m.peer_requires,
        conflicts=m.conflicts,
        description=m.description,
        author=m.author,
        tags=m.tags,
        targets=targets,
        tasks=tasks,
    )
