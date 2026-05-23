"""``PluginManager.update_config`` — live ctx.config + HOOK_PLUGIN_CONFIG_CHANGED."""

from __future__ import annotations

from typing import Any

import pytest
from pyhooky import on

from pyplugy import (
    HOOK_PLUGIN_CONFIG_CHANGED,
    Plugin,
    PluginContext,
    PluginManager,
    PluginNotFoundError,
)


class _Capturing(Plugin):
    """Plugin that retains a reference to ``ctx.config`` so we can assert identity later."""

    name = "cap"
    version = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self.config_ref: dict[str, Any] | None = None

    def on_load(self, ctx: PluginContext) -> None:
        self.config_ref = ctx.config


def test_ctx_config_is_live_reference_to_manager_dict(registry: Any) -> None:
    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"k": 1}})
    manager.load(p)
    assert p.config_ref is not None
    # Identity check — plugin's reference IS the manager's dict.
    assert p.config_ref is manager._configs["cap"]
    # Direct mutation visible to plugin.
    manager._configs["cap"]["k"] = 99
    assert p.config_ref["k"] == 99


def test_update_config_merges_by_default(registry: Any) -> None:
    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"a": 1, "b": 2}})
    manager.load(p)
    manager.update_config("cap", {"b": 22, "c": 3})
    assert p.config_ref == {"a": 1, "b": 22, "c": 3}


def test_update_config_replace_clears_first(registry: Any) -> None:
    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"a": 1, "b": 2}})
    manager.load(p)
    manager.update_config("cap", {"only": True}, replace=True)
    assert p.config_ref == {"only": True}


def test_update_config_preserves_identity(registry: Any) -> None:
    """Replace must clear+update in place, not swap the dict for a new one."""
    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"a": 1}})
    manager.load(p)
    ref_before = p.config_ref
    manager.update_config("cap", {"x": 1}, replace=True)
    assert p.config_ref is ref_before


def test_update_config_fires_hook_when_loaded(registry: Any) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"v": 0}})
    manager.load(p)

    on(
        HOOK_PLUGIN_CONFIG_CHANGED,
        lambda plug, cfg: seen.append((plug.manifest.name, dict(cfg))),
        registry=manager.registry,
    )
    manager.update_config("cap", {"v": 7})

    assert seen == [("cap", {"v": 7})]


def test_update_config_works_before_load(registry: Any) -> None:
    """Hosts may pre-seed config before the plugin is loaded; no hook fires yet."""
    seen: list[Any] = []
    manager = PluginManager(registry=registry)
    on(HOOK_PLUGIN_CONFIG_CHANGED, lambda *a: seen.append(a), registry=manager.registry)

    manager.update_config("not-loaded-yet", {"k": 1})
    assert manager._configs["not-loaded-yet"] == {"k": 1}
    assert seen == []  # no slot → no hook fired


def test_unknown_plugin_load_unaffected_by_pre_seeded_config(registry: Any) -> None:
    """Config seeded via update_config is picked up when the plugin actually loads."""
    p = _Capturing()
    manager = PluginManager(registry=registry)
    manager.update_config("cap", {"pre": True})
    manager.load(p)
    assert p.config_ref == {"pre": True}


def test_update_config_survives_disable_enable(registry: Any) -> None:
    """``_do_enable`` rebuilds ctx on DISABLED→ENABLED; the live config dict must persist."""
    p = _Capturing()
    manager = PluginManager(registry=registry, configs={"cap": {"k": 1}})
    manager.load(p)
    first_ref = p.config_ref
    assert first_ref is not None
    manager.disable("cap")
    manager.update_config("cap", {"k": 2})
    manager.enable("cap")
    # After re-enable, on_load ran again and the plugin re-captured ctx.config.
    assert p.config_ref is first_ref  # same underlying dict
    assert first_ref["k"] == 2


def test_update_config_with_unknown_name_then_no_hook(registry: Any) -> None:
    """update_config never raises for unknown names — pre-seeding is supported."""
    manager = PluginManager(registry=registry)
    manager.update_config("ghost", {"x": 1})  # must not raise


def test_dunder_only_PluginNotFoundError_unrelated(registry: Any) -> None:
    """Sanity: enabling an unknown plugin still raises PluginNotFoundError."""
    manager = PluginManager(registry=registry)
    with pytest.raises(PluginNotFoundError):
        manager.enable("ghost")
