"""Class-form Plugin authoring — manifest population, lifecycle defaults."""

from __future__ import annotations

from pyplugy import Plugin, PluginContext


class _Sample(Plugin):
    name = "sample"
    version = "0.4.2"
    requires = ("other>=0.1",)
    description = "sample desc"
    author = "tester"
    tags = ("alpha",)


def test_class_form_manifest_pulls_class_attrs() -> None:
    inst = _Sample()
    assert inst.manifest.name == "sample"
    assert inst.manifest.version == "0.4.2"
    assert inst.manifest.requires == ("other>=0.1",)
    assert inst.manifest.description == "sample desc"
    assert inst.manifest.author == "tester"
    assert inst.manifest.tags == ("alpha",)


def test_default_lifecycle_methods_are_noops() -> None:
    class _P(Plugin):
        name = "noop"

    p = _P()
    p.on_load(None)  # type: ignore[arg-type]
    p.on_unload(None)  # type: ignore[arg-type]
    p.on_enable(None)  # type: ignore[arg-type]
    p.on_disable(None)  # type: ignore[arg-type]


def test_class_and_decorator_forms_equivalent() -> None:
    from pyplugy import plugin

    @plugin("eq", version="1.0.0")
    def setup(ctx: PluginContext) -> None:
        pass

    class Eq(Plugin):
        name = "eq"
        version = "1.0.0"

    assert setup.manifest == Eq().manifest
