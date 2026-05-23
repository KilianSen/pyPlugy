"""Plugin discovery — entry points, directory scan, JIT loading.

Three discovery modes feed plugins into the manager:

- **Entry points**: ``manager.load_entry_points(group="myapp.plugins")``
  resolves each entry point and scans the loaded module for plugins.
- **Directory scanning**: ``manager.load_directory(path, pattern="*.py")``
  imports every match through :func:`importlib.util.spec_from_file_location`
  under a sandboxed module name (``pyplugy._loaded.<stem>``) so we don't
  pollute the global :data:`sys.modules` namespace.
- **JIT (just-in-time)**: ``manager.load(target)`` accepts a class, instance,
  module, dotted path, or filesystem path and figures out what to do.

All three modes converge on :func:`scan_module` — given a module object, it
returns every :class:`Plugin` instance or class found in it.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from pyplugy._plugin import Plugin
from pyplugy.exceptions import PluginLoadError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


__all__ = [
    "import_file",
    "iter_directory_modules",
    "iter_entry_points",
    "scan_module",
]


_SANDBOX_PREFIX = "pyplugy._loaded"


def scan_module(module: ModuleType) -> list[Plugin]:
    """Return every plugin defined in ``module``.

    Recognises three shapes:

    - Module-level :class:`Plugin` instances (e.g. produced by the
      :func:`plugin` decorator).
    - :class:`Plugin` subclasses defined in the module (instantiated here with
      no arguments).
    - A ``setup`` callable wrapped by the :func:`plugin` decorator (already a
      :class:`Plugin` instance — covered by the first case).

    Items imported *from* other modules are skipped: only definitions whose
    ``__module__`` matches ``module.__name__`` are considered. That keeps
    re-imports from accidentally double-registering a plugin discovered via
    another path.
    """
    found: list[Plugin] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()

    for attr_name in dir(module):
        if attr_name.startswith("_"):
            continue
        obj = getattr(module, attr_name, None)

        # 1) Plugin instances (decorator form)
        if isinstance(obj, Plugin):
            if id(obj) in seen_ids:
                continue
            seen_ids.add(id(obj))
            name = obj.manifest.name or attr_name
            if name in seen_names:
                continue
            seen_names.add(name)
            found.append(obj)
            continue

        # 2) Plugin subclasses (class form) — only those defined in this module
        if (
            inspect.isclass(obj)
            and issubclass(obj, Plugin)
            and obj is not Plugin
            and getattr(obj, "__module__", None) == module.__name__
        ):
            try:
                instance = obj()
            except Exception as exc:  # pragma: no cover - defensive
                raise PluginLoadError(
                    f"failed to instantiate plugin class {obj.__name__!r} from "
                    f"module {module.__name__!r}: {exc}"
                ) from exc
            if instance.manifest.name in seen_names:
                continue
            seen_names.add(instance.manifest.name)
            seen_ids.add(id(instance))
            found.append(instance)

    return found


def iter_entry_points(group: str) -> Iterator[tuple[str, ModuleType]]:
    """Yield ``(entry_point_name, module)`` for every entry point in ``group``.

    Each entry point is resolved with :func:`importlib.metadata.entry_points`.
    Targets that don't resolve to a module are imported via dotted-path
    semantics so plugins published as ``"my.module:setup"`` still work
    (the manager scans the parent module).
    """
    eps = importlib.metadata.entry_points(group=group)
    for ep in eps:
        loaded = ep.load()
        if isinstance(loaded, ModuleType):
            yield ep.name, loaded
        else:
            # ``ep.load()`` may return a class/function — use its parent module.
            module_name = getattr(loaded, "__module__", None) or ep.value.split(":")[0]
            module = importlib.import_module(module_name)
            yield ep.name, module


def _sandbox_name(stem: str, *, counter: dict[str, int]) -> str:
    """Pick a unique sandboxed name like ``pyplugy._loaded.foo`` (or ``foo_1`` on collision)."""
    base = f"{_SANDBOX_PREFIX}.{stem}"
    n = counter.get(base, 0)
    counter[base] = n + 1
    return base if n == 0 else f"{base}_{n}"


def import_file(path: Path, *, sandbox_counter: dict[str, int] | None = None) -> ModuleType:
    """Import a ``.py`` file under a sandboxed module name.

    Avoids inserting the loaded module under its bare stem in
    :data:`sys.modules` — that would shadow real top-level packages.
    """
    if sandbox_counter is None:
        sandbox_counter = {}
    module_name = _sandbox_name(path.stem, counter=sandbox_counter)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"cannot build module spec for {path!r}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so intra-module relative imports resolve.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def iter_directory_modules(
    path: Path,
    *,
    recursive: bool = False,
    pattern: str = "*.py",
) -> Iterable[ModuleType]:
    """Yield modules imported from every file matching ``pattern`` under ``path``."""
    if not path.exists():
        raise PluginLoadError(f"plugin directory {path!r} does not exist")
    if not path.is_dir():
        raise PluginLoadError(f"plugin path {path!r} is not a directory")

    sandbox_counter: dict[str, int] = {}
    iterator = path.rglob(pattern) if recursive else path.glob(pattern)
    for file in sorted(iterator):
        if not file.is_file():
            continue
        if file.name == "__init__.py":
            continue
        yield import_file(file, sandbox_counter=sandbox_counter)
