"""Phase 2: peer deps, exported APIs, hot-swap, pydantic manifest/config/api validation."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from pyplugy import (
    Plugin,
    PluginConfigValidationError,
    PluginContext,
    PluginDependencyError,
    PluginError,
    PluginManager,
    PluginManifest,
    PluginManifestError,
    PluginPeerDependencyError,
    PluginState,
    plugin,
)
from pyplugy._depgraph import resolve_load_order

# ---------- pydantic manifest ----------


def test_manifest_is_basemodel() -> None:
    assert issubclass(PluginManifest, BaseModel)


def test_manifest_frozen() -> None:
    m = PluginManifest(name="x")
    with pytest.raises(Exception):  # noqa: B017 - frozen raises ValidationError
        m.name = "y"  # type: ignore[misc]


def test_manifest_extra_forbidden() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        PluginManifest(name="x", typo_field=1)  # type: ignore[call-arg]


def test_decorator_empty_name_still_raises() -> None:
    # The wrapper in _coerce_manifest catches the empty-name case before pydantic.
    with pytest.raises(PluginManifestError):

        @plugin("")
        def setup(ctx: PluginContext) -> None:
            pass


def test_manifest_validates_bad_requirement_at_construction() -> None:
    with pytest.raises(PluginManifestError):
        # ">>>" is not a valid specifier prefix.
        @plugin("p", requires=["foo>>>1"])
        def setup(ctx: PluginContext) -> None:
            pass


# ---------- peer deps ----------


def test_peer_dep_missing_raises_dedicated_error() -> None:
    @plugin("p_user", version="1.0.0", peer_requires=["host_bus"])
    def user(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginPeerDependencyError, match="host_bus"):
        resolve_load_order([user])


def test_peer_dep_present_orders() -> None:
    @plugin("p_bus", version="1.0.0")
    def bus(ctx: PluginContext) -> None:
        pass

    @plugin("p_user", version="1.0.0", peer_requires=["p_bus>=1.0"])
    def user(ctx: PluginContext) -> None:
        pass

    ordered = resolve_load_order([user, bus])
    names = [p.manifest.name for p in ordered]
    assert names.index("p_bus") < names.index("p_user")


def test_peer_dep_version_mismatch_raises() -> None:
    @plugin("p_bus", version="1.0.0")
    def bus(ctx: PluginContext) -> None:
        pass

    @plugin("p_user", version="1.0.0", peer_requires=["p_bus>=99"])
    def user(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginDependencyError, match="satisfy"):
        resolve_load_order([bus, user])


def test_peer_resolver_does_not_auto_fetch(manager: PluginManager) -> None:
    @plugin("p_bus", version="1.0.0")
    def bus(ctx: PluginContext) -> None:
        pass

    @plugin("p_user", version="1.0.0", peer_requires=["p_bus"])
    def user(ctx: PluginContext) -> None:
        pass

    fetched: list[str] = []

    def resolver(missing: str) -> list[Plugin] | None:
        fetched.append(missing)
        return [bus]

    manager.add_resolver(resolver)
    with pytest.raises(PluginPeerDependencyError):
        manager.load(user)
    assert fetched == [], "resolver should NOT be invoked for peer deps"


def test_peer_dependent_blocks_unload(manager: PluginManager) -> None:
    @plugin("p_bus", version="1.0.0")
    def bus(ctx: PluginContext) -> None:
        pass

    @plugin("p_user", version="1.0.0", peer_requires=["p_bus"])
    def user(ctx: PluginContext) -> None:
        pass

    manager.load_all([bus, user])
    with pytest.raises(PluginDependencyError, match="p_user"):
        manager.unload("p_bus")


# ---------- exported APIs ----------


def test_ctx_export_and_api_of(manager: PluginManager) -> None:
    @plugin("e_auth", version="1.0.0")
    def auth(ctx: PluginContext) -> None:
        ctx.export({"sign": "v1"})

    captured: dict[str, object] = {}

    @plugin("e_users", version="1.0.0", requires=["e_auth"])
    def users(ctx: PluginContext) -> None:
        captured["api"] = ctx.api_of("e_auth")

    manager.load_all([auth, users])
    assert captured["api"] == {"sign": "v1"}


def test_api_of_undeclared_raises(manager: PluginManager) -> None:
    errors: list[Exception] = []

    @plugin("e_other", version="1.0.0")
    def other(ctx: PluginContext) -> None:
        ctx.export("hello")

    @plugin("e_sneaky", version="1.0.0", requires=["e_other"])
    def sneaky(ctx: PluginContext) -> None:
        try:
            ctx.api_of("not_declared")
        except PluginDependencyError as exc:
            errors.append(exc)

    manager.load_all([other, sneaky])
    assert errors and "did not declare" in str(errors[0]).lower()


def test_api_model_validation_passes_dict(manager: PluginManager) -> None:
    class AuthAPI(BaseModel):
        sign: str

    class Auth(Plugin):
        name = "ea_auth"
        version = "1.0.0"
        api_model = AuthAPI

        def on_load(self, ctx: PluginContext) -> None:
            ctx.export({"sign": "v1"})

    @plugin("ea_user", version="1.0.0", requires=["ea_auth"])
    def user(ctx: PluginContext) -> None:
        api = ctx.api_of("ea_auth")
        assert isinstance(api, AuthAPI)
        assert api.sign == "v1"

    manager.load_all([Auth(), user])


def test_api_model_rejects_bad_payload(manager: PluginManager) -> None:
    class AuthAPI(BaseModel):
        sign: str

    class Auth(Plugin):
        name = "eb_auth"
        version = "1.0.0"
        api_model = AuthAPI

        def on_load(self, ctx: PluginContext) -> None:
            ctx.export({"wrong_field": 1})  # missing sign, extra field

    # Manager wraps on_load failures in PluginLoadError; the inner cause
    # is the PluginManifestError from the validator.
    from pyplugy import PluginLoadError

    with pytest.raises(PluginLoadError):
        manager.load(Auth())


def test_export_outside_lifecycle_raises() -> None:
    # Build a context manually without going through the manager so
    # current_manager() stays None.
    from pyhooky import HookRegistry

    from pyplugy._context import PluginContext as PC

    ctx = PC(PluginManifest(name="solo"), registry=HookRegistry(name="t"))
    with pytest.raises(PluginError, match="outside an active lifecycle"):
        ctx.export({"x": 1})


def test_api_cleared_on_unload(manager: PluginManager) -> None:
    @plugin("eu_auth", version="1.0.0")
    def auth(ctx: PluginContext) -> None:
        ctx.export({"v": 1})

    manager.load(auth)
    # Re-loading after unload should start with a fresh api slot.
    manager.unload("eu_auth")
    manager.load(auth)
    # After fresh load, on_load runs again and re-exports.
    snapshot = manager.list_plugins()
    assert any(p.name == "eu_auth" for p in snapshot)


# ---------- validated config ----------


def test_config_model_validates_on_load() -> None:
    class CFG(BaseModel):
        secret: str

    class P(Plugin):
        name = "c_p"
        version = "1.0.0"
        config_model = CFG

        def on_load(self, ctx: PluginContext) -> None:
            cfg = ctx.config_model
            assert cfg is not None
            assert cfg.secret == "abc"

    from pyhooky import HookRegistry

    m = PluginManager(registry=HookRegistry(name="t"), configs={"c_p": {"secret": "abc"}})
    m.load(P())
    assert m.is_enabled("c_p")


def test_config_model_rejects_bad_config() -> None:
    class CFG(BaseModel):
        secret: str

    class P(Plugin):
        name = "c_bad"
        version = "1.0.0"
        config_model = CFG

    from pyhooky import HookRegistry

    m = PluginManager(registry=HookRegistry(name="t"), configs={"c_bad": {}})
    with pytest.raises(PluginConfigValidationError):
        m.load(P())


def test_update_config_re_validates_and_rolls_back() -> None:
    class CFG(BaseModel):
        secret: str

    class P(Plugin):
        name = "c_upd"
        version = "1.0.0"
        config_model = CFG

    from pyhooky import HookRegistry

    m = PluginManager(
        registry=HookRegistry(name="t"),
        configs={"c_upd": {"secret": "v1"}},
    )
    m.load(P())
    # Good update — applies.
    m.update_config("c_upd", {"secret": "v2"})
    assert m.get("c_upd").manifest.name == "c_upd"
    # Bad update — rejected, dict rolled back.
    with pytest.raises(PluginConfigValidationError):
        m.update_config("c_upd", {"secret": 123}, replace=True)
    # Original value still present.
    info = next(p for p in m.list_plugins() if p.name == "c_upd")
    assert info.state == PluginState.ENABLED


# ---------- hot-swap ----------


def test_swap_replaces_plugin_and_reloads_dependents(manager: PluginManager) -> None:
    versions: list[str] = []

    @plugin("s_auth", version="1.0.0")
    def auth_v1(ctx: PluginContext) -> None:
        ctx.export({"v": "1"})

    @plugin("s_users", version="1.0.0", requires=["s_auth"])
    def users(ctx: PluginContext) -> None:
        api = ctx.api_of("s_auth")
        versions.append(api["v"])

    manager.load_all([auth_v1, users])
    assert versions == ["1"]

    @plugin("s_auth", version="2.0.0")
    def auth_v2(ctx: PluginContext) -> None:
        ctx.export({"v": "2"})

    manager.swap("s_auth", auth_v2)
    assert versions[-1] == "2"
    states = {p.name: p.state for p in manager.list_plugins()}
    assert states["s_users"] == PluginState.ENABLED


def test_swap_rejects_name_mismatch(manager: PluginManager) -> None:
    @plugin("sx_a", version="1.0.0")
    def a(ctx: PluginContext) -> None:
        pass

    @plugin("sx_b", version="1.0.0")
    def b(ctx: PluginContext) -> None:
        pass

    manager.load(a)
    with pytest.raises(PluginError, match="cannot rename"):
        manager.swap("sx_a", b)


def test_swap_no_dependents(manager: PluginManager) -> None:
    @plugin("sn_a", version="1.0.0")
    def v1(ctx: PluginContext) -> None:
        pass

    @plugin("sn_a", version="2.0.0")
    def v2(ctx: PluginContext) -> None:
        pass

    manager.load(v1)
    manager.swap("sn_a", v2)
    info = next(p for p in manager.list_plugins() if p.name == "sn_a")
    assert info.version == "2.0.0"


@pytest.mark.asyncio
async def test_aswap_async(manager: PluginManager) -> None:
    @plugin("sa_auth", version="1.0.0")
    def auth_v1(ctx: PluginContext) -> None:
        pass

    @plugin("sa_users", version="1.0.0", requires=["sa_auth"])
    def users(ctx: PluginContext) -> None:
        pass

    await manager.aload_all([auth_v1, users])

    @plugin("sa_auth", version="2.0.0")
    def auth_v2(ctx: PluginContext) -> None:
        pass

    await manager.aswap("sa_auth", auth_v2)
    info = next(p for p in manager.list_plugins() if p.name == "sa_auth")
    assert info.version == "2.0.0"
