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

from pyplugy.exceptions import PluginDependencyError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyplugy._plugin import Plugin


__all__ = ["parse_requirement", "resolve_load_order"]


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

    - Raises :class:`PluginDependencyError` when a required plugin is missing.
    - Raises :class:`PluginDependencyError` when the available version doesn't
      satisfy the specifier.
    - Raises :class:`PluginDependencyError` on cycles, naming all plugins in
      the cycle for easy debugging.
    """
    plugins_list = list(plugins)
    by_name: dict[str, Plugin] = {}
    for p in plugins_list:
        name = p.manifest.name
        if name in by_name:
            raise PluginDependencyError(f"duplicate plugin name {name!r} in resolve_load_order")
        by_name[name] = p

    # Build adjacency: dep -> dependents (so we can Kahn-sort).
    incoming: dict[str, set[str]] = {n: set() for n in by_name}
    outgoing: dict[str, set[str]] = {n: set() for n in by_name}

    for plugin_obj in plugins_list:
        consumer = plugin_obj.manifest.name
        for req in plugin_obj.manifest.requires:
            dep_name, spec = parse_requirement(req)
            if dep_name not in by_name:
                raise PluginDependencyError(
                    f"plugin {consumer!r} requires {dep_name!r} which is not loaded "
                    f"(requirement {req!r})"
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
