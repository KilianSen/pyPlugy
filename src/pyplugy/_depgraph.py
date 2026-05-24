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
    PluginMissingDependencyError,
    PluginPeerDependencyError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyplugy._plugin import Plugin


__all__ = ["compute_reverse_graph", "parse_requirement", "resolve_load_order"]


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


def resolve_load_order(plugins: Iterable[Plugin]) -> list[Plugin]:
    """Topologically sort ``plugins`` honoring ``manifest.requires``.

    - Raises :class:`PluginMissingDependencyError` when a hard-required
      plugin is missing. The exception carries the missing dep name so
      resolvers can fetch it and retry.
    - Raises :class:`PluginDependencyError` when the available version
      doesn't satisfy the specifier.
    - Raises :class:`PluginConflictError` when two plugins declare each
      other (or are declared by each other) in ``conflicts``.
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

    # Conflicts first — fail fast before building the graph.
    for plugin_obj in plugins_list:
        consumer = plugin_obj.manifest.name
        for foe in plugin_obj.manifest.conflicts:
            if foe in by_name:
                raise PluginConflictError(
                    f"plugin {consumer!r} conflicts with {foe!r}; cannot load together"
                )

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
                        f"non-PEP440 version {dep_plugin.manifest.version!r}; cannot check"
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires {req!r}, but {dep_name!r} version "
                        f"{dep_plugin.manifest.version!r} does not satisfy the specifier"
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
                    f"(requirement {req!r}); the host must supply it"
                )
            dep_plugin = by_name[dep_name]
            if spec:
                parsed = _parse_version(dep_name, dep_plugin.manifest.version)
                if parsed is None:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires peer {req!r}, but {dep_name!r} "
                        f"reports non-PEP440 version {dep_plugin.manifest.version!r}; "
                        "cannot check"
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} requires peer {req!r}, but {dep_name!r} "
                        f"version {dep_plugin.manifest.version!r} does not satisfy "
                        "the specifier"
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
                        f"{dep_plugin.manifest.version!r}; cannot check"
                    )
                if parsed not in spec:
                    raise PluginDependencyError(
                        f"plugin {consumer!r} optionally requires {req!r}, but "
                        f"{dep_name!r} version {dep_plugin.manifest.version!r} does "
                        f"not satisfy the specifier"
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
