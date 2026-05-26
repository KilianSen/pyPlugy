"""The :class:`PluginTaskInfo` value-object — a task plus its host-specified metadata.

Returned by :attr:`PluginContext.tasks` and :meth:`PluginManager.plugin_tasks`.
Frozen + slotted; ``metadata`` defaults to an empty mapping so call sites
can ignore it when they don't care.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = ["PluginTaskInfo"]


_EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class PluginTaskInfo:
    """A registered task + the metadata the plugin attached at registration time.

    ``task`` is whatever ``ctx.task`` produced — typically a ``pyworkflowy.Task``,
    a ``TaskBase`` instance, or the underlying object a test stub returned.
    ``metadata`` is read-only from pyplugy's perspective: hosts attach it via
    ``ctx.task(metadata={...})`` and read it back through
    :attr:`PluginContext.tasks` / :meth:`PluginManager.plugin_tasks`.
    """

    task: Any
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_METADATA)
