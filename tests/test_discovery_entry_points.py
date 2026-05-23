"""Discovery: entry points — synthesised via monkeypatching ``importlib.metadata``."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from pyplugy import PluginManager
from pyplugy._discovery import iter_entry_points


class _FakeEntryPoint:
    """Stand-in for :class:`importlib.metadata.EntryPoint`.

    We don't construct the real type because its constructor signature varies
    across Python versions; a duck-typed object with ``.name``, ``.value`` and
    ``.load()`` is enough for our consumer.
    """

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value

    def load(self) -> Any:
        module_path, _, _ = self.value.partition(":")
        return importlib.import_module(module_path)


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    eps: list[_FakeEntryPoint],
) -> None:
    def fake_entry_points(*, group: str | None = None) -> list[_FakeEntryPoint]:
        return list(eps)

    monkeypatch.setattr(
        "pyplugy._discovery.importlib.metadata.entry_points", fake_entry_points
    )


def test_iter_entry_points_yields_module_per_ep(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(
        monkeypatch,
        "test.group",
        [_FakeEntryPoint("simple", "tests.fixtures.plugin_simple")],
    )

    pairs = list(iter_entry_points("test.group"))
    assert len(pairs) == 1
    name, module = pairs[0]
    assert name == "simple"
    assert module.__name__ == "tests.fixtures.plugin_simple"


def test_manager_load_entry_points(monkeypatch: pytest.MonkeyPatch, manager: PluginManager) -> None:
    _patch_entry_points(
        monkeypatch,
        "test.group",
        [_FakeEntryPoint("simple", "tests.fixtures.plugin_simple")],
    )

    loaded = manager.load_entry_points("test.group")
    names = {p.manifest.name for p in loaded}
    assert names == {"simple"}
