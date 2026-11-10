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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, overload

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pyplugy.exceptions import PluginManifestError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pyplugy._context import PluginContext
    from pyplugy._slot_info import PluginSlotInfo
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

    ``host_requires`` is an optional PEP 440 specifier (e.g. ``">=1.0,<2.0"``
    or ``"vrs>=1.0,<2.0"``) checked against the manager's ``host_version``
    and ``host_name``.

    ``conflicts`` is a tuple of plain plugin names — planning refuses
    to load a plugin alongside any name it lists in ``conflicts`` (and
    symmetrically refuses if a loaded plugin lists the new one).

    ``slots`` maps slot category names to the provider names declared by
    the plugin (e.g. ``{"hair": ("v2",), "fix": ("dead", "rings")}``).

    The model is ``frozen`` and ``extra="forbid"`` — unknown fields are
    a manifest error, surfaced via :class:`PluginManifestError`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = "0.0.0"
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    peer_requires: tuple[str, ...] = ()
    host_requires: str = ""
    conflicts: tuple[str, ...] = ()
    description: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()
    slots: dict[str, tuple[str, ...]] = Field(default_factory=dict)

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

    @field_validator("host_requires")
    @classmethod
    def _validate_host_requires(cls, v: str) -> str:
        if not v:
            return ""
        from pyplugy._depgraph import parse_host_requirement
        from pyplugy.exceptions import PluginDependencyError

        try:
            parse_host_requirement(v)
        except PluginDependencyError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @field_validator("slots", mode="before")
    @classmethod
    def _validate_slots(cls, v: Any) -> dict[str, tuple[str, ...]]:
        if v is None:
            return {}
        if not isinstance(v, Mapping):
            raise ValueError("slots must be a mapping of slot_name -> provider names or mapping")
        out: dict[str, tuple[str, ...]] = {}
        for slot_name, entries in v.items():
            if not isinstance(slot_name, str) or not slot_name:
                raise ValueError(f"slot name must be a non-empty string, got {slot_name!r}")
            if isinstance(entries, Mapping):
                names = tuple(entries.keys())
            elif isinstance(entries, Iterable) and not isinstance(entries, (str, bytes)):
                names = tuple(entries)
            else:
                raise ValueError(
                    f"slot {slot_name!r} entries must be a mapping or iterable of names"
                )
            for entry_name in names:
                if not isinstance(entry_name, str) or not entry_name:
                    raise ValueError(
                        f"provider name in slot {slot_name!r} must be a non-empty string, "
                        f"got {entry_name!r}"
                    )
            out[slot_name] = names
        return out


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
    host_requires: str = ""
    slots: tuple[PluginSlotInfo, ...] = ()


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


def _normalize_declarative_slots(
    plugin_name: str,
    raw_slots: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Validate and copy a declarative ``{slot: {name: provider}}`` mapping."""
    if not raw_slots:
        return {}
    if not isinstance(raw_slots, Mapping):
        raise PluginManifestError(
            f"invalid manifest for {plugin_name!r}: slots must be a mapping of "
            f"{{slot: {{name: provider}}}}, got {type(raw_slots).__name__}"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for slot_name, entries in raw_slots.items():
        if not isinstance(slot_name, str) or not slot_name:
            raise PluginManifestError(
                f"invalid manifest for {plugin_name!r}: slot name must be a non-empty string, "
                f"got {slot_name!r}"
            )
        if not isinstance(entries, Mapping):
            raise PluginManifestError(
                f"invalid manifest for {plugin_name!r}: slot {slot_name!r} must map "
                f"provider names to providers, got {type(entries).__name__}"
            )
        slot_dict: dict[str, Any] = {}
        for entry_name, provider in entries.items():
            if not isinstance(entry_name, str) or not entry_name:
                raise PluginManifestError(
                    f"invalid manifest for {plugin_name!r}: provider name in slot "
                    f"{slot_name!r} must be a non-empty string, got {entry_name!r}"
                )
            slot_dict[entry_name] = provider
        normalized[slot_name] = slot_dict
    return normalized


def _coerce_manifest(
    name: str,
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    optional_requires: tuple[str, ...] | list[str] | None = None,
    peer_requires: tuple[str, ...] | list[str] | None = None,
    host_requires: str = "",
    conflicts: tuple[str, ...] | list[str] | None = None,
    description: str = "",
    author: str = "",
    tags: tuple[str, ...] | list[str] | None = None,
    slots: Mapping[str, Any] | None = None,
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
            host_requires=host_requires,
            conflicts=tuple(conflicts or ()),
            description=description,
            author=author,
            tags=tuple(tags or ()),
            slots=dict(slots or {}),
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
    host_requires: ClassVar[str] = ""
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

    # Named slot providers declared statically at class scope:
    # ``{"hair": {"v2": v2_mesher}, "fix": {"dead": fix_dead}}``.
    # Inspectable even while the plugin is UNLOADED.
    slots: ClassVar[Mapping[str, Mapping[str, Any]]] = {}

    def __init__(
        self,
        manifest: PluginManifest | None = None,
        *,
        declarative_slots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        raw_slots = declarative_slots if declarative_slots is not None else self.slots
        if manifest is None:
            # When the subclass didn't set ``name``, derive one from the
            # class name (PascalCase → snake_case). This is a pure
            # ergonomic shortcut; ``_DecoratedPlugin`` passes its own
            # name in directly and never hits this branch.
            derived = self.name or _derive_plugin_name(type(self).__name__)
            normalized_slots = _normalize_declarative_slots(derived, raw_slots)
            manifest = _coerce_manifest(
                derived,
                version=self.version,
                requires=tuple(self.requires),
                optional_requires=tuple(self.optional_requires),
                peer_requires=tuple(self.peer_requires),
                host_requires=self.host_requires,
                conflicts=tuple(self.conflicts),
                description=self.description,
                author=self.author,
                tags=tuple(self.tags),
                slots={s: tuple(entries.keys()) for s, entries in normalized_slots.items()},
            )
        else:
            normalized_slots = _normalize_declarative_slots(manifest.name, raw_slots)
        self._manifest = manifest
        self._declarative_slots: dict[str, dict[str, Any]] = normalized_slots

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def declarative_slots(self) -> dict[str, dict[str, Any]]:
        """Copy of the statically declared ``{slot: {name: provider}}`` mapping."""
        return {s: dict(entries) for s, entries in self._declarative_slots.items()}

    # ---------- lifecycle hooks (override in subclasses) ----------
    #
    # Each lifecycle method may be declared ``async def`` instead of ``def``.
    # The synchronous ``PluginManager.load`` / ``enable`` / ``disable`` /
    # ``unload`` entry points reject coroutine-returning lifecycle methods;
    # use the async variants (``aload``/``aenable``/``adisable``/``aunload``)
    # to drive plugins whose lifecycle is async.

    def on_load(self, ctx: PluginContext) -> Awaitable[None] | None:
        """Register hooks/tasks. Called inside a ``tag_scope(self.manifest.name)`` block."""

    def on_unload(self, ctx: PluginContext) -> Awaitable[None] | None:
        """Release external resources.

        Hooks/tasks owned by the plugin are torn down by the manager.
        """

    def on_enable(self, ctx: PluginContext) -> Awaitable[None] | None:
        """Called when the plugin transitions DISABLED → ENABLED."""

    def on_disable(self, ctx: PluginContext) -> Awaitable[None] | None:
        """Called when the plugin transitions ENABLED → DISABLED."""

    def __repr__(self) -> str:
        m = self._manifest
        return f"Plugin(name={m.name!r}, version={m.version!r})"


class _DecoratedPlugin(Plugin):
    """Concrete :class:`Plugin` whose :meth:`on_load` delegates to the wrapped setup."""

    __slots__ = ("_setup",)

    def __init__(
        self,
        manifest: PluginManifest,
        setup: Callable[..., Any],
        *,
        declarative_slots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(manifest, declarative_slots=declarative_slots)
        self._setup = setup

    def on_load(self, ctx: PluginContext) -> Any:
        return self._setup(ctx)


@overload
def plugin(name: Callable[..., Any], /) -> Plugin: ...
@overload
def plugin(
    name: str,
    *,
    version: str = ...,
    requires: tuple[str, ...] | list[str] | None = ...,
    optional_requires: tuple[str, ...] | list[str] | None = ...,
    peer_requires: tuple[str, ...] | list[str] | None = ...,
    host_requires: str = ...,
    conflicts: tuple[str, ...] | list[str] | None = ...,
    description: str = ...,
    author: str = ...,
    tags: tuple[str, ...] | list[str] | None = ...,
    slots: Mapping[str, Mapping[str, Any]] | None = ...,
) -> Callable[[Callable[..., Any]], Plugin]: ...
def plugin(
    name: str | Callable[..., Any],
    *,
    version: str = "0.0.0",
    requires: tuple[str, ...] | list[str] | None = None,
    optional_requires: tuple[str, ...] | list[str] | None = None,
    peer_requires: tuple[str, ...] | list[str] | None = None,
    host_requires: str = "",
    conflicts: tuple[str, ...] | list[str] | None = None,
    description: str = "",
    author: str = "",
    tags: tuple[str, ...] | list[str] | None = None,
    slots: Mapping[str, Mapping[str, Any]] | None = None,
) -> Plugin | Callable[[Callable[..., Any]], Plugin]:
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
        normalized_slots = _normalize_declarative_slots(resolved_name, slots)
        manifest = _coerce_manifest(
            resolved_name,
            version=version,
            requires=requires,
            optional_requires=optional_requires,
            peer_requires=peer_requires,
            host_requires=host_requires,
            conflicts=conflicts,
            description=description,
            author=author,
            tags=tags,
            slots={s: tuple(entries.keys()) for s, entries in normalized_slots.items()},
        )
        return _DecoratedPlugin(manifest, setup, declarative_slots=normalized_slots)

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
    slots: tuple[PluginSlotInfo, ...] = (),
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
        host_requires=m.host_requires,
        slots=slots,
    )
