"""Optional deps, conflicts, cascade teardown, resolvers, ctx.dep, introspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyplugy import (
    Plugin,
    PluginConflictError,
    PluginContext,
    PluginDependencyError,
    PluginManager,
    PluginMissingDependencyError,
    PluginState,
    plugin,
)
from pyplugy._depgraph import (
    compute_reverse_graph,
    resolve_load_order,
)


# ---------- manifest fields ----------


def test_manifest_defaults_preserve_backwards_compat() -> None:
    @plugin("p", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        pass

    assert setup.manifest.optional_requires == ()
    assert setup.manifest.conflicts == ()


def test_manifest_accepts_optional_requires_and_conflicts() -> None:
    @plugin(
        "p",
        version="1.0.0",
        optional_requires=["foo>=1.0"],
        conflicts=["bar"],
    )
    def setup(ctx: PluginContext) -> None:
        pass

    assert setup.manifest.optional_requires == ("foo>=1.0",)
    assert setup.manifest.conflicts == ("bar",)


def test_class_form_supports_new_fields() -> None:
    class P(Plugin):
        name = "p"
        version = "1.0.0"
        optional_requires = ("foo",)
        conflicts = ("bar",)

    p = P()
    assert p.manifest.optional_requires == ("foo",)
    assert p.manifest.conflicts == ("bar",)


# ---------- depgraph: optional + conflicts + reverse ----------


def test_optional_dep_present_orders() -> None:
    @plugin("base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("mid", version="1.0.0", optional_requires=["base>=1.0"])
    def mid(ctx: PluginContext) -> None:
        pass

    ordered = resolve_load_order([mid, base])
    names = [p.manifest.name for p in ordered]
    assert names.index("base") < names.index("mid")


def test_optional_dep_missing_is_silent() -> None:
    @plugin("solo", version="1.0.0", optional_requires=["absent"])
    def solo(ctx: PluginContext) -> None:
        pass

    ordered = resolve_load_order([solo])
    assert [p.manifest.name for p in ordered] == ["solo"]


def test_optional_dep_version_mismatch_still_raises() -> None:
    @plugin("base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("mid", version="1.0.0", optional_requires=["base>=99"])
    def mid(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginDependencyError, match="satisfy"):
        resolve_load_order([base, mid])


def test_conflicts_in_batch_raise() -> None:
    @plugin("a", version="1.0.0", conflicts=["b"])
    def a(ctx: PluginContext) -> None:
        pass

    @plugin("b", version="1.0.0")
    def b(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginConflictError, match="conflicts"):
        resolve_load_order([a, b])


def test_missing_hard_dep_raises_typed_exception() -> None:
    @plugin("orphan", version="1.0.0", requires=["nope"])
    def setup(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginMissingDependencyError) as exc_info:
        resolve_load_order([setup])
    assert exc_info.value.missing == "nope"
    assert exc_info.value.consumer == "orphan"


def test_compute_reverse_graph_hard_only() -> None:
    @plugin("base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("hard", version="1.0.0", requires=["base"])
    def hard(ctx: PluginContext) -> None:
        pass

    @plugin("soft", version="1.0.0", optional_requires=["base"])
    def soft(ctx: PluginContext) -> None:
        pass

    rg = compute_reverse_graph([base, hard, soft])
    assert rg == {"base": {"hard"}}


# ---------- manager: conflicts against already-loaded ----------


def test_conflict_against_already_loaded(manager: PluginManager) -> None:
    @plugin("first", version="1.0.0")
    def first(ctx: PluginContext) -> None:
        pass

    @plugin("second", version="1.0.0", conflicts=["first"])
    def second(ctx: PluginContext) -> None:
        pass

    manager.load(first)
    with pytest.raises(PluginConflictError):
        manager.load(second)


# ---------- manager: cascade unload / disable ----------


def _make_chain() -> tuple[Plugin, Plugin, Plugin]:
    """Return three plugins forming base <- mid <- top."""

    @plugin("c_base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("c_mid", version="1.0.0", requires=["c_base"])
    def mid(ctx: PluginContext) -> None:
        pass

    @plugin("c_top", version="1.0.0", requires=["c_mid"])
    def top(ctx: PluginContext) -> None:
        pass

    return base, mid, top


def test_unload_refuses_with_active_dependents(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    with pytest.raises(PluginDependencyError, match="c_mid|c_top"):
        manager.unload("c_base")


def test_unload_cascade_tears_down_dependents(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    manager.unload("c_base", cascade=True)
    assert "c_base" not in {p.name for p in manager.list_plugins()}
    assert "c_mid" not in {p.name for p in manager.list_plugins()}
    assert "c_top" not in {p.name for p in manager.list_plugins()}


def test_disable_refuses_with_enabled_dependents(manager: PluginManager) -> None:
    base, mid, _top = _make_chain()
    manager.load_all([base, mid])
    with pytest.raises(PluginDependencyError, match="c_mid"):
        manager.disable("c_base")


def test_disable_cascade(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    manager.disable("c_base", cascade=True)
    states = {p.name: p.state for p in manager.list_plugins()}
    assert states["c_base"] == PluginState.DISABLED
    assert states["c_mid"] == PluginState.DISABLED
    assert states["c_top"] == PluginState.DISABLED


def test_optional_dependents_do_not_block_unload(manager: PluginManager) -> None:
    @plugin("o_base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("o_user", version="1.0.0", optional_requires=["o_base"])
    def user(ctx: PluginContext) -> None:
        pass

    manager.load_all([base, user])
    # Should not raise — optional deps don't pin teardown.
    manager.unload("o_base")
    names = {p.name for p in manager.list_plugins()}
    assert "o_base" not in names
    assert "o_user" in names


def test_reload_cascades_by_default(manager: PluginManager) -> None:
    calls: list[str] = []

    @plugin("r_base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        calls.append("base")

    @plugin("r_top", version="1.0.0", requires=["r_base"])
    def top(ctx: PluginContext) -> None:
        calls.append("top")

    manager.load_all([base, top])
    calls.clear()
    manager.reload("r_base")
    # Both should have been re-loaded; both should still be enabled.
    states = {p.name: p.state for p in manager.list_plugins()}
    assert states["r_base"] == PluginState.ENABLED
    assert states["r_top"] == PluginState.ENABLED
    assert "base" in calls and "top" in calls


def test_reload_no_cascade_raises_when_dependents(manager: PluginManager) -> None:
    base, mid, _top = _make_chain()
    manager.load_all([base, mid])
    with pytest.raises(PluginDependencyError):
        manager.reload("c_base", cascade=False)


# ---------- manager: ctx.dep ----------


def test_ctx_dep_returns_dependency_context(manager: PluginManager) -> None:
    captured: dict[str, object] = {}

    @plugin("d_auth", version="1.0.0")
    def auth(ctx: PluginContext) -> None:
        ctx.config["who"] = "auth"

    @plugin("d_users", version="1.0.0", requires=["d_auth>=1.0"])
    def users(ctx: PluginContext) -> None:
        dep = ctx.dep("d_auth")
        captured["dep"] = dep
        captured["who"] = dep.config["who"] if dep else None

    manager.load_all([auth, users])
    assert captured["who"] == "auth"


def test_ctx_dep_undeclared_raises(manager: PluginManager) -> None:
    errors: list[Exception] = []

    @plugin("d_target", version="1.0.0")
    def target(ctx: PluginContext) -> None:
        pass

    @plugin("d_sneaky", version="1.0.0", requires=["d_target"])
    def sneaky(ctx: PluginContext) -> None:
        try:
            ctx.dep("not_declared")
        except PluginDependencyError as exc:
            errors.append(exc)

    manager.load_all([target, sneaky])
    assert errors and "did not declare" in str(errors[0]).lower()


def test_ctx_dep_optional_returns_none_when_absent(manager: PluginManager) -> None:
    captured: dict[str, object] = {}

    @plugin("d_solo", version="1.0.0", optional_requires=["d_missing"])
    def solo(ctx: PluginContext) -> None:
        captured["dep"] = ctx.dep("d_missing")

    manager.load(solo)
    assert captured["dep"] is None


# ---------- manager: introspection ----------


def test_dependencies_of_direct_and_transitive(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    assert manager.dependencies_of("c_top") == ["c_mid"]
    assert manager.dependencies_of("c_top", transitive=True) == ["c_base", "c_mid"]


def test_dependents_of(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    assert manager.dependents_of("c_base") == ["c_mid"]
    assert manager.dependents_of("c_base", transitive=True) == ["c_mid", "c_top"]


def test_dependency_graph(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    manager.load_all([base, mid, top])
    graph = manager.dependency_graph()
    assert graph == {"c_base": [], "c_mid": ["c_base"], "c_top": ["c_mid"]}


def test_dump_includes_new_fields(manager: PluginManager) -> None:
    @plugin("d_p", version="1.0.0", optional_requires=["foo"], conflicts=["bar"])
    def p(ctx: PluginContext) -> None:
        pass

    manager.load(p)
    snap = manager.dump()
    entry = next(e for e in snap["plugins"] if e["name"] == "d_p")
    assert entry["optional_requires"] == ["foo"]
    assert entry["conflicts"] == ["bar"]
    assert "graph" in snap


# ---------- manager: resolver registry ----------


def test_resolver_fetches_missing_dep(manager: PluginManager) -> None:
    @plugin("res_base", version="1.0.0")
    def base(ctx: PluginContext) -> None:
        pass

    @plugin("res_user", version="1.0.0", requires=["res_base"])
    def user(ctx: PluginContext) -> None:
        pass

    def resolver(name: str) -> list[Plugin] | None:
        if name == "res_base":
            return [base]
        return None

    manager.add_resolver(resolver)
    manager.load(user)  # base is not in the batch — resolver supplies it
    names = {p.name for p in manager.list_plugins()}
    assert "res_base" in names and "res_user" in names


def test_resolver_runaway_caps_out(manager: PluginManager) -> None:
    @plugin("r_user", version="1.0.0", requires=["never_resolves"])
    def user(ctx: PluginContext) -> None:
        pass

    # Resolver that returns a plugin with the *same* missing dep — never satisfies.
    def bad_resolver(name: str) -> list[Plugin] | None:
        @plugin(f"placeholder_{name}", version="1.0.0", requires=[name])
        def chain(ctx: PluginContext) -> None:
            pass

        return [chain]

    manager.add_resolver(bad_resolver)
    with pytest.raises(PluginDependencyError, match="converge|loaded"):
        manager.load(user)


def test_directory_resolver_loads_missing_dep(
    manager: PluginManager, tmp_path: Path
) -> None:
    plugin_file = tmp_path / "dep_plugin.py"
    plugin_file.write_text(
        "from pyplugy import plugin\n"
        "from pyplugy import PluginContext\n"
        "@plugin('dir_dep', version='1.0.0')\n"
        "def setup(ctx: PluginContext) -> None:\n"
        "    pass\n"
    )

    @plugin("dir_user", version="1.0.0", requires=["dir_dep"])
    def user(ctx: PluginContext) -> None:
        pass

    manager.use_directory_resolver(tmp_path)
    manager.load(user)
    names = {p.name for p in manager.list_plugins()}
    assert "dir_dep" in names and "dir_user" in names


# ---------- async cascade ----------


@pytest.mark.asyncio
async def test_aunload_cascade(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    await manager.aload_all([base, mid, top])
    await manager.aunload("c_base", cascade=True)
    assert manager.list_plugins() == []


@pytest.mark.asyncio
async def test_aunload_refuses_without_cascade(manager: PluginManager) -> None:
    base, mid, _top = _make_chain()
    await manager.aload_all([base, mid])
    with pytest.raises(PluginDependencyError):
        await manager.aunload("c_base")


@pytest.mark.asyncio
async def test_areload_cascade(manager: PluginManager) -> None:
    base, mid, top = _make_chain()
    await manager.aload_all([base, mid, top])
    await manager.areload("c_base")
    states = {p.name: p.state for p in manager.list_plugins()}
    assert states["c_base"] == PluginState.ENABLED
    assert states["c_mid"] == PluginState.ENABLED
    assert states["c_top"] == PluginState.ENABLED
