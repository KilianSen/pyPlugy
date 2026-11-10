"""pyPlugy — plugin framework with hooks (pyHooky) and tasks (pyWorkflowy)."""

from __future__ import annotations

from pyplugy._context import PluginContext, current_context
from pyplugy._manager import (
    HOOK_PLUGIN_CONFIG_CHANGED,
    HOOK_PLUGIN_DISABLE,
    HOOK_PLUGIN_ENABLE,
    HOOK_PLUGIN_ERROR,
    HOOK_PLUGIN_LOAD,
    HOOK_PLUGIN_UNLOAD,
    LoadFailurePhase,
    OnLoadError,
    PluginLoadFailure,
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
from pyplugy._slot_info import PluginSlotInfo
from pyplugy._task_info import PluginTaskInfo
from pyplugy.exceptions import (
    PluginAlreadyLoadedError,
    PluginConfigValidationError,
    PluginConflictError,
    PluginDependencyError,
    PluginError,
    PluginHostVersionError,
    PluginLoadError,
    PluginManifestError,
    PluginMissingDependencyError,
    PluginNotFoundError,
    PluginPeerDependencyError,
    PluginSlotConflictError,
    PluginSlotNotFoundError,
    PluginUnloadError,
)

__all__ = [
    "HOOK_PLUGIN_CONFIG_CHANGED",
    "HOOK_PLUGIN_DISABLE",
    "HOOK_PLUGIN_ENABLE",
    "HOOK_PLUGIN_ERROR",
    "HOOK_PLUGIN_LOAD",
    "HOOK_PLUGIN_UNLOAD",
    "LoadFailurePhase",
    "OnLoadError",
    "Plugin",
    "PluginAlreadyLoadedError",
    "PluginConfigValidationError",
    "PluginConflictError",
    "PluginContext",
    "PluginDependencyError",
    "PluginError",
    "PluginHostVersionError",
    "PluginInfo",
    "PluginLoadError",
    "PluginLoadFailure",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginMissingDependencyError",
    "PluginNotFoundError",
    "PluginPeerDependencyError",
    "PluginSlotConflictError",
    "PluginSlotInfo",
    "PluginSlotNotFoundError",
    "PluginState",
    "PluginTaskInfo",
    "PluginUnloadError",
    "__version__",
    "current_context",
    "current_manager",
    "plugin",
]

__version__ = "0.4.0"
