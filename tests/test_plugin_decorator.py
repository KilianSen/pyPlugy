"""@plugin decorator authoring form — manifest correctness and equivalence."""

from __future__ import annotations

import pytest

from pyplugy import Plugin, PluginContext, PluginManifestError, plugin


def test_decorator_with_name_creates_plugin() -> None:
    @plugin("foo", version="2.0.0", requires=["bar>=1.0"], description="hi", author="me")
    def setup(ctx: PluginContext) -> None:
        pass

    assert isinstance(setup, Plugin)
    assert setup.manifest.name == "foo"
    assert setup.manifest.version == "2.0.0"
    assert setup.manifest.requires == ("bar>=1.0",)
    assert setup.manifest.description == "hi"
    assert setup.manifest.author == "me"


def test_bare_decorator_uses_function_name() -> None:
    @plugin
    def my_named_plugin(ctx: PluginContext) -> None:
        pass

    assert isinstance(my_named_plugin, Plugin)
    assert my_named_plugin.manifest.name == "my_named_plugin"


def test_decorator_empty_name_raises() -> None:
    with pytest.raises(PluginManifestError):

        @plugin("")
        def setup(ctx: PluginContext) -> None:
            pass


def test_decorator_tags_field_normalised_to_tuple() -> None:
    @plugin("tagged", tags=["ui", "core"])
    def setup(ctx: PluginContext) -> None:
        pass

    assert setup.manifest.tags == ("ui", "core")


def test_decorator_repr_includes_name_and_version() -> None:
    @plugin("foo", version="1.2.3")
    def setup(ctx: PluginContext) -> None:
        pass

    text = repr(setup)
    assert "foo" in text
    assert "1.2.3" in text
