"""pyPlugy — plugin framework with hooks (pyHooky) and tasks (pyTasky)."""

from __future__ import annotations

from pyplugy._context import PluginContext, current_context
from pyplugy._manager import (
    HOOK_PLUGIN_DISABLE,
    HOOK_PLUGIN_ENABLE,
    HOOK_PLUGIN_ERROR,
    HOOK_PLUGIN_LOAD,
    HOOK_PLUGIN_UNLOAD,
    PluginManager,
    current_manager,
)
from pyplugy._plugin import (
    Plugin,
    PluginInfo,
    PluginManifest,
    PluginState,
    plugin,
)
from pyplugy.exceptions import (
    PluginAlreadyLoadedError,
    PluginDependencyError,
    PluginError,
    PluginLoadError,
    PluginManifestError,
    PluginNotFoundError,
    PluginUnloadError,
)

__all__ = [
    "HOOK_PLUGIN_DISABLE",
    "HOOK_PLUGIN_ENABLE",
    "HOOK_PLUGIN_ERROR",
    "HOOK_PLUGIN_LOAD",
    "HOOK_PLUGIN_UNLOAD",
    "Plugin",
    "PluginAlreadyLoadedError",
    "PluginContext",
    "PluginDependencyError",
    "PluginError",
    "PluginInfo",
    "PluginLoadError",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginNotFoundError",
    "PluginState",
    "PluginUnloadError",
    "current_context",
    "current_manager",
    "plugin",
]

__version__ = "0.1.0"
