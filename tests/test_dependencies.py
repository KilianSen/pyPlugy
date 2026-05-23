"""Dependency resolution: topo sort, missing deps, version mismatches, cycles."""

from __future__ import annotations

import pytest

from pyplugy import (
    Plugin,
    PluginContext,
    PluginDependencyError,
    PluginManager,
    plugin,
)
from pyplugy._depgraph import parse_requirement, resolve_load_order


@plugin("base", version="1.0.0")
def _base(ctx: PluginContext) -> None:
    pass


@plugin("mid", version="2.0.0", requires=["base>=1.0"])
def _mid(ctx: PluginContext) -> None:
    pass


@plugin("top", version="0.1.0", requires=["mid>=2.0", "base"])
def _top(ctx: PluginContext) -> None:
    pass


def test_parse_requirement_bare_name() -> None:
    name, spec = parse_requirement("foo")
    assert name == "foo"
    assert str(spec) == ""


def test_parse_requirement_with_specifier() -> None:
    name, spec = parse_requirement("foo>=1.2,<2.0")
    assert name == "foo"
    assert "1.5" in spec
    assert "2.0" not in spec


def test_resolve_topological_order() -> None:
    ordered = resolve_load_order([_top, _mid, _base])
    names = [p.manifest.name for p in ordered]
    assert names.index("base") < names.index("mid") < names.index("top")


def test_missing_dependency_raises() -> None:
    @plugin("orphan", version="1.0.0", requires=["nope"])
    def setup(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginDependencyError, match="nope"):
        resolve_load_order([setup])


def test_version_mismatch_raises() -> None:
    @plugin("oldreq", version="1.0.0", requires=["base>=99"])
    def setup(ctx: PluginContext) -> None:
        pass

    with pytest.raises(PluginDependencyError, match="satisfy"):
        resolve_load_order([_base, setup])


def test_cycle_raises() -> None:
    class A(Plugin):
        name = "ca"
        version = "1.0.0"
        requires = ("cb",)

    class B(Plugin):
        name = "cb"
        version = "1.0.0"
        requires = ("ca",)

    with pytest.raises(PluginDependencyError, match="cyclic"):
        resolve_load_order([A(), B()])


def test_manager_load_all_orders_by_deps(manager: PluginManager) -> None:
    loaded = manager.load_all([_top, _mid, _base])
    names = [p.manifest.name for p in loaded]
    assert names.index("base") < names.index("mid") < names.index("top")


def test_manager_rejects_load_when_dep_missing(manager: PluginManager) -> None:
    with pytest.raises(PluginDependencyError):
        manager.load(_mid)
