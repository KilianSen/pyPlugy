"""Phase 3 syntactic sugar: name derivation, typed events, dep injection, TaskBase."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel
from pyworkflowy import TaskBase

from pyplugy import (
    Plugin,
    PluginContext,
    PluginManager,
    plugin,
)
from pyplugy._plugin import _derive_plugin_name

# ---------- auto-derived plugin name ----------


@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        ("Auth", "auth"),
        ("AuthPlugin", "auth_plugin"),
        ("HTTPAdapter", "http_adapter"),
        ("XMLParser", "xml_parser"),
        ("PluginV2", "plugin_v2"),
        ("OAuth2Provider", "o_auth2_provider"),
        ("", ""),
    ],
)
def test_derive_plugin_name_helper(class_name: str, expected: str) -> None:
    assert _derive_plugin_name(class_name) == expected


def test_auto_name_simple(manager: PluginManager) -> None:
    class Demo(Plugin):
        version = "1.0.0"

    p = Demo()
    assert p.manifest.name == "demo"
    manager.load(p)
    assert any(info.name == "demo" for info in manager.list_plugins())


def test_auto_name_pascal_case(manager: PluginManager) -> None:
    class HTTPAdapter(Plugin):
        version = "1.0.0"

    assert HTTPAdapter().manifest.name == "http_adapter"


def test_explicit_name_still_wins() -> None:
    class Explicit(Plugin):
        name = "kept-as-is"
        version = "1.0.0"

    assert Explicit().manifest.name == "kept-as-is"


# ---------- ctx.event factory ----------


class _LoginEvent(BaseModel):
    user: str


def test_ctx_event_returns_hookpoint_and_dispatches(manager: PluginManager) -> None:
    captured: list[str] = []

    @plugin("evp", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        login = ctx.event("evp:login", _LoginEvent)

        @login.listen
        def audit(evt: _LoginEvent) -> None:
            captured.append(evt.user)

        login.trigger(user="alice")

    manager.load(setup)
    assert captured == ["alice"]


def test_ctx_event_listener_torn_down_on_unload(manager: PluginManager) -> None:
    captured: list[str] = []

    saved_point: dict[str, object] = {}

    @plugin("evt", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        login = ctx.event("evt:login", _LoginEvent)
        saved_point["p"] = login

        @login.listen
        def audit(evt: _LoginEvent) -> None:
            captured.append(evt.user)

    manager.load(setup)
    manager.unload("evt")
    # After unload, listeners are cleared by tag_scope — triggering should be a no-op.
    saved_point["p"].trigger(user="alice")  # type: ignore[attr-defined]
    assert captured == []


# ---------- class-level events ----------


class _LogoutEvent(BaseModel):
    user: str


def test_class_level_events_populate_ctx(manager: PluginManager) -> None:
    captured: dict[str, object] = {}

    class Auth(Plugin):
        version = "1.0.0"
        events: ClassVar[dict[str, type[BaseModel]]] = {
            "login": _LoginEvent,
            "logout": _LogoutEvent,
        }

        def on_load(self, ctx: PluginContext) -> None:
            captured["keys"] = sorted(ctx.events.keys())
            captured["login_name"] = ctx.events["login"].name
            captured["logout_name"] = ctx.events["logout"].name

    manager.load(Auth())
    assert captured["keys"] == ["login", "logout"]
    # Namespaced via plugin name (auto-derived → "auth").
    assert captured["login_name"] == "auth:login"
    assert captured["logout_name"] == "auth:logout"


def test_class_level_event_listener_fires(manager: PluginManager) -> None:
    received: list[str] = []

    class Auth(Plugin):
        version = "1.0.0"
        events: ClassVar[dict[str, type[BaseModel]]] = {"login": _LoginEvent}

        def on_load(self, ctx: PluginContext) -> None:
            @ctx.events["login"].listen
            def audit(evt: _LoginEvent) -> None:
                received.append(evt.user)

            ctx.events["login"].trigger(user="bob")

    manager.load(Auth())
    assert received == ["bob"]


# ---------- TaskBase support ----------


def test_ctx_task_accepts_taskbase_subclass(manager: PluginManager) -> None:
    captured: dict[str, object] = {}

    class Cleanup(TaskBase):
        name = "cleanup"

        def run(self) -> str:
            return "done"

    @plugin("tb", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        t = ctx.task(Cleanup)
        captured["task"] = t

    manager.load(setup)
    assert captured["task"] is not None
    # Task is stored on the context.
    info = next(p for p in manager.list_plugins() if p.name == "tb")
    assert [i.task.name for i in info.tasks] == ["cleanup"]


# ---------- type-hint dep injection ----------


class _AuthAPI(BaseModel):
    sign: str


def test_injection_class_form(manager: PluginManager) -> None:
    received: dict[str, object] = {}

    class Auth(Plugin):
        name = "i_auth"
        version = "1.0.0"
        api_model = _AuthAPI

        def on_load(self, ctx: PluginContext) -> None:
            ctx.export({"sign": "v1"})

    class Users(Plugin):
        name = "i_users"
        version = "1.0.0"
        requires = ("i_auth",)

        def on_load(self, ctx: PluginContext, i_auth: _AuthAPI) -> None:  # type: ignore[override]
            received["api"] = i_auth

    manager.load_all([Auth(), Users()])
    assert isinstance(received["api"], _AuthAPI)
    assert received["api"].sign == "v1"  # type: ignore[attr-defined]


def test_injection_decorator_form(manager: PluginManager) -> None:
    received: dict[str, object] = {}

    @plugin("d_auth", version="1.0.0")
    def auth(ctx: PluginContext) -> None:
        ctx.export("AUTH-API")

    @plugin("d_users", version="1.0.0", requires=["d_auth"])
    def users(ctx: PluginContext, d_auth: object) -> None:
        received["api"] = d_auth

    manager.load_all([auth, users])
    assert received["api"] == "AUTH-API"


def test_injection_optional_absent_returns_none(manager: PluginManager) -> None:
    received: dict[str, object] = {}

    @plugin("opt_user", version="1.0.0", optional_requires=["opt_missing"])
    def setup(ctx: PluginContext, opt_missing: object) -> None:
        received["dep"] = opt_missing

    manager.load(setup)
    assert received["dep"] is None


def test_injection_undeclared_param_is_not_filled(manager: PluginManager) -> None:
    @plugin("undecl", version="1.0.0")
    def setup(ctx: PluginContext, unknown: object) -> None:
        pass

    # Manager wraps the TypeError from the missing positional arg into PluginLoadError.
    from pyplugy import PluginLoadError

    with pytest.raises(PluginLoadError, match="unknown"):
        manager.load(setup)


def test_injection_skips_varargs(manager: PluginManager) -> None:
    received: dict[str, object] = {}

    @plugin("va_dep", version="1.0.0")
    def dep(ctx: PluginContext) -> None:
        ctx.export("dep-api")

    @plugin("va_user", version="1.0.0", requires=["va_dep"])
    def user(ctx: PluginContext, va_dep: object, *args: object, **kwargs: object) -> None:
        received["dep"] = va_dep
        received["args"] = args
        received["kwargs"] = kwargs

    manager.load_all([dep, user])
    assert received["dep"] == "dep-api"
    assert received["args"] == ()
    assert received["kwargs"] == {}
