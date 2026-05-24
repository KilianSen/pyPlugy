"""Plugin-related exceptions raised by :mod:`pyplugy`.

Every exception inherits from :class:`PluginError`, so callers can catch the
whole family with one ``except`` clause and still distinguish individual
failure modes via :func:`isinstance`.
"""

from __future__ import annotations

__all__ = [
    "PluginAlreadyLoadedError",
    "PluginConfigValidationError",
    "PluginConflictError",
    "PluginDependencyError",
    "PluginError",
    "PluginLoadError",
    "PluginManifestError",
    "PluginMissingDependencyError",
    "PluginNotFoundError",
    "PluginPeerDependencyError",
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


class PluginMissingDependencyError(PluginDependencyError):
    """Raised when a required dependency is not loaded. Carries the missing name.

    The :attr:`missing` attribute lets resolvers in
    :class:`~pyplugy._manager.PluginManager` fetch the named plugin and retry
    planning, without re-parsing the error message.
    """

    def __init__(self, message: str, *, missing: str, consumer: str) -> None:
        super().__init__(message)
        self.missing = missing
        self.consumer = consumer


class PluginConflictError(PluginDependencyError):
    """Raised when two plugins declare mutually exclusive ``conflicts`` entries."""


class PluginPeerDependencyError(PluginDependencyError):
    """Raised when a ``peer_requires`` entry is not satisfied by the host.

    Peer dependencies are *not* candidates for auto-resolution: a host
    that uses ``peer_requires`` is asserting it will supply the named
    plugin itself. Resolvers therefore skip this exception entirely.
    """


class PluginConfigValidationError(PluginError):
    """Raised when a plugin's config dict fails its ``config_model`` schema."""


class PluginLoadError(PluginError):
    """Raised when ``on_load`` (or decorator setup) raises during load."""


class PluginUnloadError(PluginError):
    """Raised when ``on_unload`` raises during unload."""


class PluginManifestError(PluginError):
    """Raised when a plugin's manifest is invalid (missing name, bad version, ...)."""
