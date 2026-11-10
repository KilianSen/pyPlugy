"""Phase 4 (VRS): named slots, host_requires, fault-tolerant load, non-executing discovery."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, ClassVar

import pytest

from pyplugy import (
    HOOK_PLUGIN_ERROR,
    Plugin,
    PluginConflictError,
    PluginContext,
    PluginHostVersionError,
    PluginLoadError,
    PluginManager,
    PluginManifest,
    PluginManifestError,
    PluginSlotConflictError,
    PluginSlotInfo,
    PluginSlotNotFoundError,
    PluginState,
    plugin,
)

if TYPE_CHECKING:
    from pathlib import Path


# ==========================================================================
# 1. Named Slots / Extension Point Lookup
# ==========================================================================


def test_declarative_slots_class_and_decorator(manager: PluginManager) -> None:
    def parity_mesher() -> str:
        return "parity"

    def v2_mesher() -> str:
        return "v2"

    class HairParity(Plugin):
        name = "hair_parity"
        version = "1.0.0"
        slots: ClassVar[dict[str, dict[str, object]]] = {"hair": {"parity": parity_mesher}}

    @plugin("hair_v2", version="1.0.0", slots={"hair": {"v2": v2_mesher}})
    def setup_v2(ctx: PluginContext) -> None:
        pass

    manager.load_all([HairParity(), setup_v2])

    assert manager.slot_names("hair") == ["parity", "v2"]
    assert manager.get_slot("hair", "v2") is v2_mesher
    assert manager.get_slot("hair", "parity") is parity_mesher
    assert manager.get_slots("hair") == {"parity": parity_mesher, "v2": v2_mesher}
    assert HairParity().manifest.slots == {"hair": ("parity",)}
    assert HairParity().declarative_slots == {"hair": {"parity": parity_mesher}}


def test_dynamic_slots_provide_and_decorator_forms(manager: PluginManager) -> None:
    def fix_dead() -> str:
        return "dead"

    @plugin("fixes", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        # 1) Direct call with explicit name + provider
        ctx.provide("fix", "dead", fix_dead, metadata={"order": 1})

        # 2) Decorator with explicit name
        @ctx.provide("fix", "layers", metadata={"order": 2})
        def fix_layers() -> str:
            return "layers"

        # 3) Decorator inferring name from fn.__name__
        @ctx.slot("fix")
        def rings() -> str:
            return "rings"

        # 4) Direct callable form inferring name from fn.__name__
        def normals() -> str:
            return "normals"

        ctx.slot("fix", normals)

    manager.load(setup)

    assert manager.slot_names("fix") == ["dead", "layers", "normals", "rings"]
    selected = manager.get_slots("fix", ["dead", "rings"])
    assert list(selected.keys()) == ["dead", "rings"]
    assert selected["dead"]() == "dead"
    assert selected["rings"]() == "rings"

    slots_info = manager.plugin_slots("fixes")
    assert len(slots_info) == 4
    dead_info = next(s for s in slots_info if s.name == "dead")
    assert isinstance(dead_info, PluginSlotInfo)
    assert dead_info.slot == "fix"
    assert dead_info.plugin == "fixes"
    assert dead_info.metadata == {"order": 1}


def test_slot_not_found_carries_available_names(manager: PluginManager) -> None:
    @plugin("hair_p", slots={"hair": {"parity": object(), "v2": object()}})
    def setup(ctx: PluginContext) -> None:
        pass

    manager.load(setup)

    with pytest.raises(PluginSlotNotFoundError) as exc_info:
        manager.get_slot("hair", "v3")

    err = exc_info.value
    assert err.slot == "hair"
    assert err.name == "v3"
    assert err.available == ("parity", "v2")


def test_declarative_slot_conflict_raises_at_plan_time(manager: PluginManager) -> None:
    @plugin("p1", slots={"hair": {"v2": "from_p1"}})
    def p1(ctx: PluginContext) -> None:
        pass

    @plugin("p2", slots={"hair": {"v2": "from_p2"}})
    def p2(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginSlotConflictError) as exc_info:
        manager.load_all([p1, p2])

    assert exc_info.value.slot == "hair"
    assert exc_info.value.name == "v2"
    assert {exc_info.value.existing_plugin, exc_info.value.new_plugin} == {"p1", "p2"}


def test_dynamic_slot_conflict_raises_during_on_load(manager: PluginManager) -> None:
    @plugin("p1")
    def p1(ctx: PluginContext) -> None:
        ctx.provide("hair", "v2", "from_p1")

    @plugin("p2")
    def p2(ctx: PluginContext) -> None:
        ctx.provide("hair", "v2", "from_p2")

    manager.load(p1)
    with pytest.raises(PluginSlotConflictError) as exc_info:
        manager.load(p2)

    assert isinstance(exc_info.value, PluginConflictError)
    assert isinstance(exc_info.value, PluginLoadError)
    assert exc_info.value.existing_plugin == "p1"
    assert exc_info.value.new_plugin == "p2"


def test_dynamic_slots_cleared_on_disable_and_unload(manager: PluginManager) -> None:
    @plugin("dyn_fix")
    def dyn_fix(ctx: PluginContext) -> None:
        ctx.provide("fix", "dead", lambda: "fixed")

    manager.load(dyn_fix)
    assert manager.slot_names("fix", include_unloaded=False) == ["dead"]

    manager.disable("dyn_fix")
    assert manager.slot_names("fix", include_unloaded=False) == []
    with pytest.raises(PluginSlotNotFoundError):
        manager.get_slot("fix", "dead")

    manager.enable("dyn_fix")
    assert manager.get_slot("fix", "dead")() == "fixed"

    manager.unload("dyn_fix")
    assert manager.slot_names("fix") == []


def test_list_slots_and_dump_include_slots(manager: PluginManager) -> None:
    @plugin("p_slots", slots={"hair": {"v2": "mesher"}})
    def p_slots(ctx: PluginContext) -> None:
        ctx.provide("fix", "rings", "fixer", metadata={"tier": "fast"})

    manager.load(p_slots)
    all_slots = manager.list_slots()
    assert {(s.slot, s.name) for s in all_slots} == {("hair", "v2"), ("fix", "rings")}
    fix_only = manager.list_slots("fix")
    assert len(fix_only) == 1
    assert fix_only[0].name == "rings"

    dumped = manager.dump()
    p_dump = next(p for p in dumped["plugins"] if p["name"] == "p_slots")
    assert {"slot": "fix", "name": "rings", "metadata": {"tier": "fast"}} in p_dump["slots"]


def test_manifest_validates_bad_slots() -> None:
    with pytest.raises(PluginManifestError):

        @plugin("bad_slots", slots={"": {"v2": 1}})  # empty slot name
        def s1(ctx: PluginContext) -> None:
            pass

    with pytest.raises(PluginManifestError):

        @plugin("bad_slots2", slots={"hair": {"": 1}})  # empty entry name
        def s2(ctx: PluginContext) -> None:
            pass


# ==========================================================================
# 2. Host API Version Gating (host_requires)
# ==========================================================================


def test_host_requires_validates_specifier_at_manifest_creation() -> None:
    with pytest.raises(PluginManifestError):

        @plugin("bad_host", host_requires=">>>1.0")
        def setup(ctx: PluginContext) -> None:
            pass

    m = PluginManifest(name="ok", host_requires=">=1.0,<2.0")
    assert m.host_requires == ">=1.0,<2.0"


def test_host_requires_passes_when_compatible() -> None:
    mgr = PluginManager(host_name="vrs", host_version="1.4.2")
    assert mgr.host_name == "vrs"
    assert mgr.host_version == "1.4.2"

    @plugin("p_bare", host_requires=">=1.0,<2.0")
    def p_bare(ctx: PluginContext) -> None:
        pass

    @plugin("p_named", host_requires="vrs>=1.2")
    def p_named(ctx: PluginContext) -> None:
        pass

    mgr.load_all([p_bare, p_named])
    assert mgr.is_enabled("p_bare")
    assert mgr.is_enabled("p_named")


def test_host_requires_rejects_incompatible_version() -> None:
    mgr = PluginManager(host_name="vrs", host_version="2.0.0")

    @plugin("legacy_plugin", host_requires=">=1.0,<2.0")
    def legacy(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginHostVersionError, match="legacy_plugin"):
        mgr.load(legacy)


def test_host_requires_rejects_wrong_host_name() -> None:
    mgr = PluginManager(host_name="vrs", host_version="1.5.0")

    @plugin("other_host_plugin", host_requires="blender>=1.0")
    def other(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginHostVersionError, match="blender"):
        mgr.load(other)


# ==========================================================================
# 3. Fault-Tolerant Loading (on_error="skip" | "warn" | "raise")
# ==========================================================================


def test_on_error_skip_tolerates_plan_and_load_failures(caplog: pytest.LogCaptureFixture) -> None:
    mgr = PluginManager(host_version="1.5.0")
    errors_seen: list[tuple[object, Exception]] = []

    def _on_err(plugin_obj: object, exc: Exception) -> None:
        errors_seen.append((plugin_obj, exc))

    mgr.registry.add_listener(HOOK_PLUGIN_ERROR, _on_err)

    @plugin("good_one", version="1.0.0", slots={"hair": {"v2": "v2_fn"}})
    def good_one(ctx: PluginContext) -> None:
        pass

    @plugin("wrong_host", version="1.0.0", host_requires=">=2.0")
    def wrong_host(ctx: PluginContext) -> None:
        pass

    @plugin("missing_dep", version="1.0.0", requires=["nonexistent>=1.0"])
    def missing_dep(ctx: PluginContext) -> None:
        pass

    @plugin("crashes_in_load", version="1.0.0")
    def crashes_in_load(ctx: PluginContext) -> None:
        raise RuntimeError("boom in on_load")

    @plugin("depends_on_crasher", version="1.0.0", requires=["crashes_in_load"])
    def depends_on_crasher(ctx: PluginContext) -> None:
        pass

    loaded = mgr.load_all(
        [good_one, wrong_host, missing_dep, crashes_in_load, depends_on_crasher],
        on_error="warn",
    )

    assert [p.manifest.name for p in loaded] == ["good_one"]
    assert mgr.is_enabled("good_one")
    assert mgr.get_slot("hair", "v2") == "v2_fn"

    failures = mgr.load_errors()
    failed_sources = {f.source: f.phase for f in failures}
    assert failed_sources == {
        "wrong_host": "plan",
        "missing_dep": "plan",
        "crashes_in_load": "load",
        "depends_on_crasher": "load",
    }
    assert len(errors_seen) == 4
    assert "skipping" in caplog.text

    mgr.clear_load_errors()
    assert mgr.load_errors() == []


def test_load_directory_on_error_skip_survives_broken_syntax_and_runtime_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "01_good.py").write_text(
        textwrap.dedent(
            """
            from pyplugy import plugin

            @plugin("good_dir_plugin", slots={"fix": {"dead": "dead_fixer"}})
            def setup(ctx):
                pass
            """
        )
    )
    (tmp_path / "02_syntax_error.py").write_text("def broken(:\n")
    (tmp_path / "03_import_error.py").write_text("import definitely_not_a_real_package_xyz\n")

    mgr = PluginManager(on_load_error="skip")
    loaded = mgr.load_directory(tmp_path)

    assert [p.manifest.name for p in loaded] == ["good_dir_plugin"]
    assert mgr.get_slot("fix", "dead") == "dead_fixer"
    failures = mgr.load_errors()
    assert len(failures) == 2
    assert all(f.phase == "discovery" for f in failures)


def test_aload_all_on_error_skip() -> None:
    import asyncio

    async def _run() -> None:
        mgr = PluginManager(host_version="1.0.0")

        @plugin("async_good")
        async def async_good(ctx: PluginContext) -> None:
            ctx.provide("hair", "v2", "async_v2")

        @plugin("async_bad")
        async def async_bad(ctx: PluginContext) -> None:
            raise ValueError("async boom")

        loaded = await mgr.aload_all([async_good, async_bad], on_error="skip")
        assert [p.manifest.name for p in loaded] == ["async_good"]
        assert mgr.get_slot("hair", "v2") == "async_v2"
        assert len(mgr.load_errors()) == 1
        assert mgr.load_errors()[0].source == "async_bad"

    asyncio.run(_run())


# ==========================================================================
# 4. Non-Executing Discovery (discover_* & UNLOADED state)
# ==========================================================================


def test_discover_lists_plugins_and_slots_without_running_on_load(tmp_path: Path) -> None:
    side_effects: list[str] = []

    @plugin(
        "hair_v2",
        version="1.2.0",
        description="V2 hair mesher",
        slots={"hair": {"v2": lambda: "meshed_v2"}},
    )
    def hair_v2(ctx: PluginContext) -> None:
        side_effects.append("hair_v2_loaded")

    @plugin(
        "fixes_bundle",
        version="1.0.0",
        slots={"fix": {"dead": lambda: "fixed_dead", "rings": lambda: "fixed_rings"}},
    )
    def fixes_bundle(ctx: PluginContext) -> None:
        side_effects.append("fixes_bundle_loaded")

    mgr = PluginManager()
    discovered = mgr.discover([hair_v2, fixes_bundle])

    assert len(discovered) == 2
    # Crucial: on_load was NOT executed during discovery!
    assert side_effects == []

    # Introspection works while UNLOADED:
    infos = mgr.list_plugins()
    assert [(i.name, i.state) for i in infos] == [
        ("hair_v2", PluginState.UNLOADED),
        ("fixes_bundle", PluginState.UNLOADED),
    ]
    assert mgr.slot_names("hair") == ["v2"]
    assert mgr.slot_names("fix") == ["dead", "rings"]
    assert len(mgr.list_slots(include_unloaded=True)) == 3
    assert len(mgr.list_slots(include_unloaded=False)) == 0

    # Selective activation via get_slot(..., auto_enable=True) only runs hair_v2!
    mesher = mgr.get_slot("hair", "v2", auto_enable=True)
    assert mesher() == "meshed_v2"
    assert side_effects == ["hair_v2_loaded"]
    assert mgr.state("hair_v2") == PluginState.ENABLED
    assert mgr.state("fixes_bundle") == PluginState.UNLOADED

    # Enabling fixes_bundle explicitly transitions it from UNLOADED -> ENABLED:
    mgr.enable("fixes_bundle")
    assert side_effects == ["hair_v2_loaded", "fixes_bundle_loaded"]
    assert mgr.state("fixes_bundle") == PluginState.ENABLED


def test_enable_unloaded_plugin_automatically_loads_discovered_dependencies() -> None:
    order: list[str] = []

    @plugin("base_mesh", version="1.0.0")
    def base_mesh(ctx: PluginContext) -> None:
        order.append("base_mesh")
        ctx.export({"scale": 2})

    @plugin("hair_v2", version="1.0.0", requires=["base_mesh"])
    def hair_v2(ctx: PluginContext, base_mesh: dict[str, int]) -> None:
        order.append(f"hair_v2:{base_mesh['scale']}")

    mgr = PluginManager()
    mgr.discover([hair_v2, base_mesh])
    assert order == []

    # Enabling hair_v2 pulls in base_mesh from UNLOADED state in topo order.
    mgr.enable("hair_v2")
    assert order == ["base_mesh", "hair_v2:2"]
    assert mgr.is_enabled("base_mesh")
    assert mgr.is_enabled("hair_v2")


def test_discover_directory_and_entry_points_and_lifecycle_ops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    (tmp_path / "p_dir.py").write_text(
        textwrap.dedent(
            """
            from pyplugy import plugin

            ran = False

            @plugin("p_dir", version="0.5.0", slots={"fix": {"layers": "layers_fn"}})
            def setup(ctx):
                global ran
                ran = True
            """
        )
    )

    mgr = PluginManager()
    discovered = mgr.discover_directory(tmp_path)
    assert [p.manifest.name for p in discovered] == ["p_dir"]
    assert mgr.state("p_dir") == PluginState.UNLOADED
    assert mgr.slot_names("fix") == ["layers"]

    # Disable on UNLOADED is a no-op; unload drops the UNLOADED entry without on_unload.
    mgr.disable("p_dir")
    assert mgr.state("p_dir") == PluginState.UNLOADED
    mgr.unload("p_dir")
    assert mgr.list_plugins() == []

    # Re-discover and activate via get_slots(..., auto_enable=True)
    mgr.discover_directory(tmp_path)
    all_fixes = mgr.get_slots("fix", auto_enable=True)
    assert all_fixes == {"layers": "layers_fn"}
    assert mgr.state("p_dir") == PluginState.ENABLED

    # Async aenable / adisable / aunload on UNLOADED plugin
    async def _async_unloaded() -> None:
        m2 = PluginManager()

        @plugin("async_u", slots={"hair": {"parity": "parity_fn"}})
        async def async_u(ctx: PluginContext) -> None:
            pass

        m2.discover(async_u)
        assert m2.state("async_u") == PluginState.UNLOADED
        await m2.adisable("async_u")
        assert m2.state("async_u") == PluginState.UNLOADED
        await m2.aenable("async_u")
        assert m2.state("async_u") == PluginState.ENABLED
        await m2.aunload("async_u")
        assert m2.list_plugins() == []

    asyncio.run(_async_unloaded())


def test_on_error_skip_handles_cycles_duplicates_and_enable_errors() -> None:
    mgr = PluginManager()

    @plugin("cyc_a", requires=["cyc_b"])
    def cyc_a(ctx: PluginContext) -> None:
        pass

    @plugin("cyc_b", requires=["cyc_a"])
    def cyc_b(ctx: PluginContext) -> None:
        pass

    class FailsEnable(Plugin):
        name = "fails_enable"

        def on_enable(self, ctx: PluginContext) -> None:
            raise RuntimeError("enable boom")

    @plugin("survivor")
    def survivor(ctx: PluginContext) -> None:
        pass

    loaded = mgr.load_all(
        [cyc_a, cyc_b, FailsEnable(), survivor, survivor],
        on_error="skip",
    )
    assert [p.manifest.name for p in loaded] == ["survivor"]
    sources = [f.source for f in mgr.load_errors()]
    assert "survivor" in sources  # duplicate in batch
    assert "cyc_a" in sources
    assert "cyc_b" in sources
    assert "fails_enable" in sources


def test_review_fixes_sentinel_hashing_default_and_async_slots() -> None:
    import asyncio

    mgr = PluginManager()

    @plugin("null_provider_plugin")
    def null_provider_plugin(ctx: PluginContext) -> None:
        # Providing None explicitly must register None rather than returning a decorator!
        res = ctx.provide("hair", "none_mesher", None, metadata={"note": "null"})
        assert res is None

    mgr.load(null_provider_plugin)
    assert mgr.get_slot("hair", "none_mesher") is None
    assert mgr.get_slot("hair", "nonexistent", default="fallback_val") == "fallback_val"

    # PluginSlotInfo must be hashable even when metadata is present
    slot_infos = mgr.list_slots("hair")
    assert len(slot_infos) == 1
    info_set = {slot_infos[0]}
    assert slot_infos[0] in info_set
    with pytest.raises(TypeError):
        slot_infos[0].metadata["mutated"] = True  # type: ignore[index]

    # Async aget_slot / aget_slots with auto_enable=True
    async def _run_async_slots() -> None:
        amgr = PluginManager()

        @plugin("async_hair", slots={"hair": {"v2": None}})
        async def async_hair(ctx: PluginContext) -> None:
            ctx.provide("hair", "v2", lambda: "async_v2_result")

        @plugin("async_fix", slots={"fix": {"dead": "dead_fn", "rings": "rings_fn"}})
        async def async_fix(ctx: PluginContext) -> None:
            pass

        amgr.discover([async_hair, async_fix])
        assert amgr.state("async_hair") == PluginState.UNLOADED
        assert amgr.state("async_fix") == PluginState.UNLOADED

        mesher = await amgr.aget_slot("hair", "v2", auto_enable=True)
        assert mesher() == "async_v2_result"
        assert amgr.state("async_hair") == PluginState.ENABLED
        assert amgr.state("async_fix") == PluginState.UNLOADED

        assert await amgr.aget_slot("hair", "missing", default="def") == "def"

        fixes = await amgr.aget_slots("fix", ["rings", "dead"], auto_enable=True)
        assert list(fixes.keys()) == ["rings", "dead"]
        assert amgr.state("async_fix") == PluginState.ENABLED

        all_fixes = await amgr.aget_slots("fix", auto_enable=True)
        assert set(all_fixes.keys()) == {"dead", "rings"}

    asyncio.run(_run_async_slots())


def test_review_fixes_host_names_and_unloaded_dep_isolation() -> None:
    # 1) Bare hyphenated host name and case-insensitive host matching
    @plugin("for_vrs_core", host_requires="vrs-core")
    def for_vrs_core(ctx: PluginContext) -> None:
        pass

    @plugin("for_vrs_upper", host_requires="VRS-Core>=1.0")
    def for_vrs_upper(ctx: PluginContext) -> None:
        pass

    mgr = PluginManager(host_name="vrs-core", host_version="1.2.0")
    mgr.load_all([for_vrs_core, for_vrs_upper])
    assert mgr.is_enabled("for_vrs_core")
    assert mgr.is_enabled("for_vrs_upper")

    # 2) _expand_with_unloaded_deps must NOT auto-pull optional_requires or peer_requires
    mgr2 = PluginManager()

    @plugin("opt_plugin")
    def opt_plugin(ctx: PluginContext) -> None:
        pass

    @plugin("main_plugin", optional_requires=["opt_plugin"])
    def main_plugin(ctx: PluginContext) -> None:
        pass

    mgr2.discover([opt_plugin, main_plugin])
    mgr2.enable("main_plugin")
    assert mgr2.state("main_plugin") == PluginState.ENABLED
    assert mgr2.state("opt_plugin") == PluginState.UNLOADED


def test_review_fixes_Substring_pruning_and_broken_unloaded_dep() -> None:
    # 1) Substring collision in cycle pruning: "core" must not be pruned when "core_extra" cycles
    mgr = PluginManager()

    @plugin("core")
    def core(ctx: PluginContext) -> None:
        pass

    @plugin("core_extra", requires=["cycle_partner"])
    def core_extra(ctx: PluginContext) -> None:
        pass

    @plugin("cycle_partner", requires=["core_extra"])
    def cycle_partner(ctx: PluginContext) -> None:
        pass

    loaded = mgr.load_all([core, core_extra, cycle_partner], on_error="skip")
    assert [p.manifest.name for p in loaded] == ["core"]

    # 2) Broken UNLOADED dependency must not cause innocent batch plugins to be dropped
    mgr2 = PluginManager()

    @plugin("broken_unloaded_dep", requires=["missing_ghost"])
    def broken_unloaded_dep(ctx: PluginContext) -> None:
        pass

    @plugin("needs_broken", requires=["broken_unloaded_dep"])
    def needs_broken(ctx: PluginContext) -> None:
        pass

    @plugin("innocent_plugin")
    def innocent_plugin(ctx: PluginContext) -> None:
        pass

    mgr2.discover(broken_unloaded_dep)
    loaded2 = mgr2.load_all([needs_broken, innocent_plugin], on_error="skip")
    assert [p.manifest.name for p in loaded2] == ["innocent_plugin"]
    failed_sources = [f.source for f in mgr2.load_errors()]
    assert "broken_unloaded_dep" in failed_sources
    assert "needs_broken" in failed_sources


def test_review_fixes_discover_module_entry_points_and_unloaded_swap_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    from pyplugy.exceptions import PluginError

    mod = types.ModuleType("synthetic_vrs_mod")

    @plugin("from_mod", slots={"hair": {"v2": "v2_fn"}})
    def from_mod(ctx: PluginContext) -> None:
        pass

    mod.from_mod = from_mod  # type: ignore[attr-defined]

    mgr = PluginManager()
    discovered = mgr.discover_module(mod)
    assert [p.manifest.name for p in discovered] == ["from_mod"]
    assert mgr.state("from_mod") == PluginState.UNLOADED

    # reload on UNLOADED raises PluginError
    with pytest.raises(PluginError, match="cannot reload unloaded plugin"):
        mgr.reload("from_mod")

    # swap on UNLOADED replaces the discovered plugin in UNLOADED state
    @plugin("from_mod", version="2.0.0", slots={"hair": {"v2": "v2_fn_new"}})
    def from_mod_v2(ctx: PluginContext) -> None:
        pass

    mgr.swap("from_mod", from_mod_v2)
    assert mgr.state("from_mod") == PluginState.UNLOADED
    assert mgr.get("from_mod").manifest.version == "2.0.0"
    assert mgr.get_slot("hair", "v2", auto_enable=True) == "v2_fn_new"
    assert mgr.state("from_mod") == PluginState.ENABLED

    # discover_entry_points test
    class _FakeEP:
        name = "ep_plugin"

        def load(self) -> types.ModuleType:
            return mod

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda group: [_FakeEP()] if group == "vrs.plugins" else [],
    )
    mgr_ep = PluginManager()
    ep_discovered = mgr_ep.discover_entry_points("vrs.plugins")
    assert [p.manifest.name for p in ep_discovered] == ["from_mod"]
    assert mgr_ep.state("from_mod") == PluginState.UNLOADED
