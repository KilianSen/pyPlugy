# pyPlugy

Plugin framework for Python that composes [pyHooky](https://github.com/kiliansen/pyHooky) for
hook-based extension points and (optionally) [pyWorkflowy](https://github.com/kiliansen/pyWorkflowy)
for plugin-owned background tasks.

A plugin in pyPlugy is just a tagged collection of hook registrations (plus,
optionally, tasks). The manager drives lifecycle, isolates each plugin's
registrations via pyHooky's `tag_scope`, and tears them all down with one
`clear_tag` call on unload.

## Install

```bash
uv add pyplugy
uv add 'pyplugy[tasks]'   # add pyWorkflowy integration
```

`pyplugy` depends on `pyhooky` and `packaging`. The `[tasks]` extra pulls in
`pyworkflowy` — without it, `ctx.task` raises a clear error pointing at the
extra.

## Defining a plugin

Two equivalent authoring forms:

### Decorator form

```python
from pyplugy import plugin, PluginContext

@plugin("audit", version="1.0.0", requires=["auth>=1.0"], description="Audit log")
def setup(ctx: PluginContext) -> None:
    @ctx.before("checkout")
    def log_start(cart): ...

    @ctx.after("checkout")
    def log_done(result, cart): ...

    @ctx.on("checkout:step")
    def log_step(step, **kw): ...
```

### Class form

```python
from pyplugy import Plugin, PluginContext

class AuditPlugin(Plugin):
    name = "audit"
    version = "1.0.0"
    requires = ("auth>=1.0",)
    description = "Audit log"

    def on_load(self, ctx: PluginContext) -> None:
        @ctx.before("checkout")
        def log_start(cart): ...

    def on_unload(self, ctx: PluginContext) -> None: ...
    def on_enable(self, ctx: PluginContext) -> None: ...
    def on_disable(self, ctx: PluginContext) -> None: ...
```

Both produce a `Plugin` instance with the same `PluginManifest`.

## PluginContext

The `PluginContext` handed to your setup / lifecycle methods exposes:

| Attribute            | Use                                                       |
|----------------------|-----------------------------------------------------------|
| `ctx.hooks`          | The `pyhooky.HookRegistry` hooks land in                  |
| `ctx.before/after/around/on/on_error/hook` | Thin auto-tagged pyHooky passthroughs |
| `ctx.task`           | Register tasks via the manager's pyWorkflowy backend           |
| `ctx.scheduler`      | Shared scheduler for periodic tasks (`Scheduler` protocol) |
| `ctx.config`         | Per-plugin config dict (manager-injected)                 |
| `ctx.logger`         | `logging.Logger` named `pyplugy.<plugin-name>`            |

Every hook registered inside `on_load` is auto-tagged with the plugin's name
because the manager wraps the call in `pyhooky.tag_scope(plugin.name)`.

## Discovery

```python
from pathlib import Path
from pyplugy import PluginManager

manager = PluginManager()

# Entry points
manager.load_entry_points("myapp.plugins")

# Directory
manager.load_directory(Path("./plugins"), recursive=True)

# JIT — any of these:
manager.load(MyPlugin)
manager.load(MyPlugin())
manager.load(some_module)
manager.load("my_app.plugins.audit")        # dotted path
manager.load(Path("./extra/audit.py"))      # .py file
```

## Lifecycle

States: `UNLOADED → LOADED → ENABLED ↔ DISABLED → UNLOADED`.

```python
manager.load(MyPlugin)            # → LOADED, then ENABLED
manager.disable("my-plugin")      # ENABLED → DISABLED (tears down all hooks/tasks)
manager.enable("my-plugin")       # DISABLED → ENABLED (re-runs on_load setup)
manager.unload("my-plugin")       # → UNLOADED (clear_tag, on_unload)
manager.reload("my-plugin")       # unload + load
```

**Disable strategy** — pyPlugy *tears down* a disabled plugin's hooks and
tasks (via `clear_tag`) and *re-runs* its setup on enable. This keeps the
hook registry as the single source of truth and avoids stale half-registered
state when a plugin mutates external resources. Plugin authors should treat
`on_load` / `setup` as idempotent (or at least re-runnable).

### Async lifecycle

`on_load` / `on_enable` / `on_disable` / `on_unload` may be declared
`async def`. Drive an async plugin through the async variants of the
manager API — they accept the same arguments as their sync siblings:

```python
async def startup():
    await manager.aload(MyAsyncPlugin)
    await manager.adisable("my-plugin")
    await manager.aenable("my-plugin")
    await manager.aunload("my-plugin")
```

Calling the sync `manager.load(...)` on a plugin whose `on_load` is
`async def` raises `PluginLoadError` rather than silently leaking the
coroutine.

### Persistence

pyPlugy is in-memory only: it never writes plugin state to disk and never
remembers across process restarts which plugins were enabled. The host owns
that — persist `enabled` flags wherever you keep plugin settings, then on
startup replay them via `manager.load(...)` followed by `manager.enable(...)`
or `manager.disable(...)`.

### Runtime config edits

The dict returned by `ctx.config` is the same object the manager holds in
its internal `_configs` table — edits the host makes propagate without a
reload. Use `manager.update_config(name, new_config)` to do that edit
cleanly:

```python
manager.update_config("audit", {"verbosity": "debug"})           # merge
manager.update_config("audit", {"only": True}, replace=True)     # replace
```

`update_config` fires the `pyplugy:plugin:config-changed` listener hook
(constant `HOOK_PLUGIN_CONFIG_CHANGED`) so plugins can react. It also
works *before* a plugin is loaded — useful for pre-seeding config from
host storage.

### Extending `PluginContext`

If a host needs to attach extras to the context plugins receive
(`ctx.app`, `ctx.event_bus`, `ctx.task_queue`, …), subclass
`PluginContext` and pass the subclass — typically bound via
`functools.partial` — as `context_class`:

```python
import functools
from pyplugy import Plugin, PluginContext, PluginManager

class NovelieContext(PluginContext):
    __slots__ = ("app", "event_bus")

    def __init__(self, *args, app, event_bus, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = app
        self.event_bus = event_bus

manager = PluginManager(
    context_class=functools.partial(NovelieContext, app=fastapi_app, event_bus=bus),
)
```

The base `PluginContext` uses `__slots__`; subclasses must either declare
their own `__slots__` for the new fields or omit `__slots__` to get a
per-instance `__dict__`.

## Dependencies between plugins

`requires` accepts a list of `"<name><specifier>"` strings (PEP 440):

```python
class Reporting(Plugin):
    name = "reporting"
    version = "0.2.0"
    requires = ("audit>=1.0", "auth<3.0")
```

On load, the manager performs a topological sort:

- Missing dependency → `PluginDependencyError`
- Specifier mismatch → `PluginDependencyError`
- Cycle → `PluginDependencyError`

## Tag-based isolation

Every plugin's hooks are tagged with its `name` via `pyhooky.tag_scope`.
Unload is a single call:

```python
manager.unload("audit")
# under the hood:
#   registry.clear_tag("audit")
```

You can list a plugin's targets / tasks with:

```python
manager.plugin_targets("audit")   # ["checkout", "checkout:step", ...]
manager.plugin_tasks("audit")     # ["periodic_flush", ...]
```

## Manager-level hooks

The manager fires pyHooky listener hooks for every lifecycle transition:

| Target                   | Fires                                                |
|--------------------------|------------------------------------------------------|
| `pyplugy:plugin:load`    | After a plugin reaches LOADED                        |
| `pyplugy:plugin:enable`  | After enable                                         |
| `pyplugy:plugin:disable` | After disable                                        |
| `pyplugy:plugin:unload`  | Before the plugin's hooks/tasks are torn down        |
| `pyplugy:plugin:error`   | When any lifecycle method raises (plugin, exc)       |
| `pyplugy:plugin:config-changed` | After `manager.update_config` mutates a loaded plugin's config |

Listen with `pyhooky.on`:

```python
from pyhooky import on
from pyplugy import HOOK_PLUGIN_LOAD

@on(HOOK_PLUGIN_LOAD)
def announce(plugin):
    print("loaded:", plugin.manifest.name)
```

## Integration with pyWorkflowy (optional)

`ctx.task` and `ctx.scheduler` come from the manager-injected pyWorkflowy surface:

```python
import pyworkflowy
from pyworkflowy.schedule import Scheduler
from pyplugy import PluginManager

manager = PluginManager(tasky=pyworkflowy, scheduler=Scheduler())

@plugin("cron-cleanup", version="0.1.0")
def setup(ctx):
    @ctx.task
    def cleanup(): ...
    ctx.scheduler.every("5m").do(cleanup)
```

Without a `tasky` argument, `ctx.task` raises a clear `RuntimeError`. pyPlugy
itself only depends on a small `typing.Protocol` defined in
`pyplugy._tasky_protocol` — so tests and apps don't need pyWorkflowy installed.

## Errors

| Error                       | When                                                |
|-----------------------------|-----------------------------------------------------|
| `PluginError`               | Base                                                 |
| `PluginNotFoundError`       | `get` / `enable` / `disable` / `unload` on unknown   |
| `PluginAlreadyLoadedError`  | Loading a plugin whose name is already loaded         |
| `PluginDependencyError`     | Missing dep, version mismatch, cycle                 |
| `PluginLoadError`           | `on_load` / `on_enable` / decorator setup raised     |
| `PluginUnloadError`         | `on_unload` raised                                   |
| `PluginManifestError`       | Invalid manifest (missing name, ...)                 |

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run pyrefly check
```
