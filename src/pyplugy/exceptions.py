"""Plugin-related exceptions raised by :mod:`pyplugy`.

Every exception inherits from :class:`PluginError`, so callers can catch the
whole family with one ``except`` clause and still distinguish individual
failure modes via :func:`isinstance`.
"""

from __future__ import annotations

__all__ = [
    "PluginAlreadyLoadedError",
    "PluginDependencyError",
    "PluginError",
    "PluginLoadError",
    "PluginManifestError",
    "PluginNotFoundError",
    "PluginUnloadError",
]


class PluginError(Exception):
    """Base class for every exception raised by :mod:`pyplugy`."""


class PluginNotFoundError(PluginError):
    """Raised when a manager lookup references a name that is not registered."""


class PluginAlreadyLoadedError(PluginError):
    """Raised when loading a plugin whose ``name`` is already managed."""


class PluginDependencyError(PluginError):
    """Raised on missing dependency, version mismatch, or cyclic dependency graph."""


class PluginLoadError(PluginError):
    """Raised when ``on_load`` (or decorator setup) raises during load."""


class PluginUnloadError(PluginError):
    """Raised when ``on_unload`` raises during unload."""


class PluginManifestError(PluginError):
    """Raised when a plugin's manifest is invalid (missing name, bad version, ...)."""
