"""Plugin dependency resolution.

Each plugin's :attr:`PluginManifest.requires` entry is a string like
``"other>=2.0"`` — a PEP 440 specifier prefixed with the dependency's name.
The leading identifier is the dependency name; anything that follows the name
is a :class:`packaging.specifiers.SpecifierSet` constraint. A bare name
(``"other"``) means "any version".

:func:`resolve_load_order` returns a list of plugins ordered so that every
plugin appears after the ones it depends on. Missing dependencies, version
mismatches, and cycles all raise :class:`PluginDependencyError`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from pyplugy.exceptions import (
    PluginConflictError,
    PluginDependencyError,
    PluginHostVersionError,
    PluginMissingDependencyError,
    PluginPeerDependencyError,
    PluginSlotConflictError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyplugy._plugin import Plugin


__all__ = [
    "check_host_requirement",
    "compute_reverse_graph",
    "parse_host_requirement",
    "parse_requirement",
    "resolve_load_order",
]


# Plugin names are loose — we just split on the first specifier operator.
_REQ_SPLIT_RE = re.compile(r"(===|==|!=|<=|>=|~=|<|>)")


def parse_requirement(req: str) -> tuple[str, SpecifierSet]:
    """Split ``"other>=2.0"`` into ``("other", SpecifierSet(">=2.0"))``.

    A bare name returns an empty :class:`SpecifierSet` (matches any version).
    Raises :class:`PluginDependencyError` if the specifier portion is malformed.
    """
    if not req or not isinstance(req, str):
        raise PluginDependencyError(f"requirement must be a non-empty string, got {req!r}")
    match = _REQ_SPLIT_RE.search(req)
    if match is None:
        return req.strip(), SpecifierSet()
    name = req[: match.start()].strip()
    spec_str = req[match.start() :].strip()
    if not name:
        raise PluginDependencyError(f"requirement {req!r} has no plugin name before the specifier")
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier as exc:
        raise PluginDependencyError(
            f"requirement {req!r} has an invalid version specifier {spec_str!r}: {exc}"
        ) from exc
    return name, spec


_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_host_requirement(req: str) -> tuple[str | None, SpecifierSet]:
    """Parse a ``host_requires`` string into ``(host_name_or_none, SpecifierSet)``.

    Accepts either a bare PEP 440 specifier (``">=1.0,<2.0"``) or a host-prefixed
    specifier (``"vrs>=1.0,<2.0"`` or ``"my-app"``). Raises
    :class:`PluginDependencyError` when the specifier is malformed.
    """
    if not req or not isinstance(req, str) or not req.strip():
        raise PluginDependencyError(f"host_requires must be a non-empty string, got {req!r}")
    stripped = req.strip()
    match = _REQ_SPLIT_RE.search(stripped)
    if match is None:
        # Bare host name with no version specifier (e.g. "vrs" or "my-app").
        # Reject strings containing spaces or invalid characters that look like broken specifiers.
        if any(ch.isspace() for ch in stripped) or not _HOST_NAME_RE.match(stripped):
            try:
                SpecifierSet(stripped)
            except InvalidSpecifier as exc:
                raise PluginDependencyError(
                    f"host_requires {req!r} has an invalid version specifier: {exc}"
                ) from exc
        return stripped, SpecifierSet()
    host_name = stripped[: match.start()].strip() or None
    if host_name is not None and not _HOST_NAME_RE.match(host_name):
        raise PluginDependencyError(f"host_requires {req!r} has an invalid host name {host_name!r}")
    spec_str = stripped[match.start() :].strip()
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier as exc:
        raise PluginDependencyError(
            f"host_requires {req!r} has an invalid version specifier {spec_str!r}: {exc}"
        ) from exc
    return host_name, spec


def check_host_requirement(
    plugin: Plugin,
    *,
    host_version: str | None,
    host_name: str | None = None,
) -> None:
    """Validate ``plugin.manifest.host_requires`` against ``host_version`` / ``host_name``.

    No-op when the plugin declared no ``host_requires``. Raises
    :class:`PluginHostVersionError` on mismatch.
    """
    req = plugin.manifest.host_requires
    if not req:
        return
    consumer = plugin.manifest.name
    req_host, spec = parse_host_requirement(req)
    if (
        req_host is not None
        and host_name is not None
        and req_host.strip().lower() != host_name.strip().lower()
    ):
        raise PluginHostVersionError(
            f"plugin {consumer!r} requires host {req_host!r} ({req!r}), "
            f"but running host is {host_name!r}",
            consumer=consumer,
        )
    if spec and host_version is not None:
        parsed = _parse_version(host_name or "host", host_version)
        if parsed is None:
            raise PluginHostVersionError(
                f"plugin {consumer!r} requires host {req!r}, but host reports "
                f"non-PEP440 version {host_version!r}; cannot check",
                consumer=consumer,
            )
        if parsed not in spec:
            raise PluginHostVersionError(
                f"plugin {consumer!r} requires host {req!r}, but host version "
                f"{host_version!r} does not satisfy the specifier",
                consumer=consumer,
            )


def _parse_version(name: str, version_str: str) -> Version | None:
    if not version_str:
        return None
    try:
        return Version(version_str)
    except InvalidVersion:
        # Non-PEP440 plugin versions silently disable specifier checks for that
        # plugin — better than refusing to load. We still warn-via-error if a
        # *consumer* requires a specifier on a non-PEP440 dep.
        return None


def resolve_load_order(
    plugins: Iterable[Plugin],
    *,
    host_version: str | None = None,
    host_name: str | None = None,
) -> list[Plugin]:
    """Topologically sort ``plugins`` honoring ``manifest.requires``.

    - Raises :class:`PluginHostVersionError` when ``host_requires`` is not
      satisfied by ``host_version`` / ``host_name``.
    - Raises :class:`PluginMissingDependencyError` when a hard-required
      plugin is missing. The exception carries the missing dep name so
      resolvers can fetch it and retry.
    - Raises :class:`PluginDependencyError` when the available version
      doesn't satisfy the specifier.
    - Raises :class:`PluginConflictError` when two plugins declare each
      other (or are declared by each other) in ``conflicts``.
    - Raises :class:`PluginSlotConflictError` when two plugins declare the
      same ``(slot, name)`` in ``manifest.slots``.
    - Raises :class:`PluginDependencyError` on cycles, naming all plugins
      in the cycle for easy debugging.

    ``optional_requires`` contribute ordering edges only when the named
    plugin is present; missing optional deps are silently ignored.
    """
    plugins_list = list(plugins)
    by_name: dict[str, Plugin] = {}
    for p in plugins_list:
        name = p.manifest.name
        if name in by_name:
            raise PluginDependencyError(f"duplicate plugin name {name!r} in resolve_load_order")
        by_name[name] = p

    # Host compatibility & conflicts first — fail fast before building the graph.
    slot_owners: dict[tuple[str, str], str] = {}
    for plugin_obj in plugins_list:
        check_host_requirement(plugin_obj, host_version=host_version, host_name=host_name)
        consumer = plugin_obj.manifest.name
        for foe in plugin_obj.manifest.conflicts:
            if foe in by_name:
                raise PluginConflictError(
                    f"plugin {consumer!r} conflicts with {foe!r}; cannot load together",
                    consumer=consumer,
                )
        for slot_name, entries in plugin_obj.manifest.slots.items():
            for entry_name in entries:
                key = (slot_name, entry_name)
                if key in slot_owners:
                    owner = slot_owners[key]
                    raise PluginSlotConflictError(
                        f"slot ({slot_name!r}, {entry_name!r}) is claimed by both "
                        f"{owner!r} and {consumer!r}",
                        slot=slot_name,
                        name=entry_name,
                        existing_plugin=owner,
                        new_plugin=consumer,
                    )
                slot_owners[key] = consumer

    # Build adjacency: dep -> dependents (so we can Kahn-sort).
    incoming: dict[str, set[str]] = {n: set() for n in by_name}
    outgoing: dict[str, set[str]] = {n: set() for n in by_name}

    for plugin_obj in plugins_list:
        consumer = plugin_obj.manifest.name
        # Hard requires — missing is an error.
        for req in plugin_obj.manifest.requires:
            dep_name, spec = parse_requirement(req)
            if dep_name not in by_name:
                raise PluginMissingDependencyError(
                    f"plugin {consumer!r} requires {dep_name!r} which is not loaded "
                    f"(requirement {req!r})",
                    missing=dep_name,
                    consumer=consumer,
                )
            dep_plugin = by_name[dep_name]
            if spec:
                parsed = _parse_version(dep_name, dep_plugin.manifest.version)
                if parsed is None:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires {req!r}, but {dep_name!r} reports "
                        f"non-PEP440 version {dep_plugin.manifest.version!r}; cannot check",
                        consumer=consumer,
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires {req!r}, but {dep_name!r} version "
                        f"{dep_plugin.manifest.version!r} does not satisfy the specifier",
                        consumer=consumer,
                    )
            incoming[consumer].add(dep_name)
            outgoing[dep_name].add(consumer)
        # Peer requires — must be present, but resolvers don't auto-fetch.
        # Use a dedicated exception class so the manager's resolver retry
        # loop (which catches PluginMissingDependencyError) skips peers.
        for req in plugin_obj.manifest.peer_requires:
            dep_name, spec = parse_requirement(req)
            if dep_name not in by_name:
                raise PluginPeerDependencyError(
                    f"plugin {consumer!r} requires peer {dep_name!r} which is not loaded "
                    f"(requirement {req!r}); the host must supply it",
                    consumer=consumer,
                )
            dep_plugin = by_name[dep_name]
            if spec:
                parsed = _parse_version(dep_name, dep_plugin.manifest.version)
                if parsed is None:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires peer {req!r}, but {dep_name!r} "
                        f"reports non-PEP440 version {dep_plugin.manifest.version!r}; "
                        "cannot check",
                        consumer=consumer,
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires peer {req!r}, but {dep_name!r} "
                        f"version {dep_plugin.manifest.version!r} does not satisfy "
                        "the specifier",
                        consumer=consumer,
                    )
            incoming[consumer].add(dep_name)
            outgoing[dep_name].add(consumer)
        # Optional requires — missing is silent, version mismatch still raises.
        for req in plugin_obj.manifest.optional_requires:
            dep_name, spec = parse_requirement(req)
            if dep_name not in by_name:
                continue
            dep_plugin = by_name[dep_name]
            if spec:
                parsed = _parse_version(dep_name, dep_plugin.manifest.version)
                if parsed is None:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} optionally requires {req!r}, but "
                        f"{dep_name!r} reports non-PEP440 version "
                        f"{dep_plugin.manifest.version!r}; cannot check",
                        consumer=consumer,
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} optionally requires {req!r}, but "
                        f"{dep_name!r} version {dep_plugin.manifest.version!r} does "
                        f"not satisfy the specifier",
                        consumer=consumer,
                    )
            incoming[consumer].add(dep_name)
            outgoing[dep_name].add(consumer)

    # Kahn's algorithm — process plugins whose deps are already emitted.
    ready = sorted(n for n, deps in incoming.items() if not deps)
    ordered: list[Plugin] = []
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        for dependent in sorted(outgoing[name]):
            incoming[dependent].discard(name)
            if not incoming[dependent]:
                ready.append(dependent)
        # Keep `ready` sorted for deterministic output.
        ready.sort()

    if len(ordered) != len(plugins_list):
        cycle = sorted(n for n, deps in incoming.items() if deps)
        raise PluginDependencyError(f"cyclic plugin dependency involving: {', '.join(cycle)}")
    return ordered


def compute_reverse_graph(plugins: Iterable[Plugin]) -> dict[str, set[str]]:
    """Return ``{dep_name: {consumer_name, ...}}`` over ``plugins``.

    Hard ``requires`` and ``peer_requires`` edges are included — both
    represent a "the consumer genuinely needs this loaded" relationship,
    and both block teardown of the dependency. ``optional_requires``
    are excluded: an optional dep can disappear without breaking the
    consumer, so it does not pin the dependency.
    """
    reverse: dict[str, set[str]] = {}
    names = {p.manifest.name for p in plugins}
    for p in plugins:
        consumer = p.manifest.name
        for req in p.manifest.requires:
            dep_name, _ = parse_requirement(req)
            if dep_name in names:
                reverse.setdefault(dep_name, set()).add(consumer)
        for req in p.manifest.peer_requires:
            dep_name, _ = parse_requirement(req)
            if dep_name in names:
                reverse.setdefault(dep_name, set()).add(consumer)
    return reverse
