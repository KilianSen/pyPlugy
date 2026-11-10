"""The :class:`PluginSlotInfo` value-object — a named slot provider plus metadata.

Returned by :attr:`PluginContext.slots`, :meth:`PluginManager.plugin_slots`,
and :meth:`PluginManager.list_slots`. Frozen + slotted; ``metadata`` defaults
to an empty mapping so call sites can ignore it when they don't care.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = ["PluginSlotInfo"]


_EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class PluginSlotInfo:
    """A registered slot provider + the metadata attached at registration time.

    ``slot`` is the extension point category (e.g. ``"hair"`` or ``"fix"``).
    ``name`` is the provider identifier within that slot (e.g. ``"v2"`` or ``"dead"``).
    ``provider`` is the callable or object registered for ``(slot, name)``.
    ``plugin`` is the manifest name of the plugin that owns this entry.
    ``metadata`` is read-only host metadata attached via ``ctx.provide(..., metadata={...})``.
    """

    slot: str
    name: str
    provider: Any
    plugin: str
    metadata: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_METADATA, hash=False)
