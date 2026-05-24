# pyPlugy

Plugin framework for Python that composes [pyHooky](https://github.com/kiliansen/pyHooky) for
hook-based extension points and [pyWorkflowy](https://github.com/kiliansen/pyWorkflowy)
for plugin-owned background tasks.

A plugin in pyPlugy is just a tagged collection of hook registrations (plus,
optionally, tasks and typed events). The manager drives lifecycle, isolates
each plugin's registrations via pyHooky's `tag_scope`, and tears them all
down with one `clear_tag` call on unload.

## Install

```bash
uv add pyplugy
```

Core dependencies: `pyhooky`, `packaging`, `pyworkflowy`, `pydantic>=2`. The
`[tasks]` extra exists for back-compat but is a no-op now — pyWorkflowy is
always installed. `ctx.task` still raises a clear error if you don't pass
`tasky=` to `PluginManager(...)` (see [Integration with pyWorkflowy](#integration-with-pyworkflowy)).

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
    # name is auto-derived from the class name → "audit_plugin"
    # (override by setting `name = "audit"` explicitly)
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

Class-form plugins get their `name` auto-derived from the class name via
PascalCase → snake_case (`AuthPlugin` → `"auth_plugin"`, `HTTPAdapter` →
`"http_adapter"`, `Auth` → `"auth"`). Setting `name = "..."` explicitly
always wins.

## PluginContext

The `PluginContext` handed to your setup / lifecycle methods exposes:

| Attribute / method                          | Use                                                                  |
|---------------------------------------------|----------------------------------------------------------------------|
| `ctx.hooks`                                 | The `pyhooky.HookRegistry` hooks land in                             |
| `ctx.before/after/around/on/on_error/hook`  | Thin auto-tagged pyHooky passthroughs                                |
| `ctx.event(name, Schema)`                   | Build a pyHooky `HookPoint` typed with a pydantic model              |
| `ctx.events`                                | `HookPoint`s built from the class-level `events = {...}` declaration |
| `ctx.task`                                  | Register tasks (decorator, `TaskBase` subclass, or callable)         |
| `ctx.scheduler`                             | Shared scheduler for periodic tasks (`Scheduler` protocol)           |
| `ctx.config`                                | Per-plugin config dict (manager-injected)                            |
| `ctx.config_model`                          | Pydantic-validated config instance (when `config_model` declared)    |
| `ctx.dep(name)` / `ctx.depends_on(name)`    | The dep's `PluginContext` (must be declared in requires)             |
| `ctx.api_of(name)`                          | Whatever the dep last `export`-ed                                    |
| `ctx.export(obj)`                           | Publish this plugin's API for dependents (validated vs `api_model`)  |
| `ctx.logger`                                | `logging.Logger` named `pyplugy.<plugin-name>`                       |

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
manager.load(MyPlugin)                  # → LOADED, then ENABLED
manager.disable("audit")                # ENABLED → DISABLED
manager.enable("audit")                 # DISABLED → ENABLED (re-runs on_load)
manager.unload("audit")                 # → UNLOADED
manager.reload("audit")                 # unload + load (cascades by default)
manager.swap("audit", AuditV2())        # replace the instance, cascade-reload deps
```

**Cascade rules.** When a plugin has dependents (hard `requires` or
`peer_requires`), `disable` / `unload` refuse by default and raise
`PluginDependencyError`. Pass `cascade=True` to tear down dependents first
(reverse-topo order):

```python
manager.unload("auth")                  # raises if dependents loaded
manager.unload("auth", cascade=True)    # unload dependents first, then auth
```

`reload` cascades by default (`cascade=True`), restoring each dependent's
prior enabled state after the reload.

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
    await manager.aswap("my-plugin", MyAsyncPluginV2())
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
host storage. When the plugin declared a `config_model`, the new config is
re-validated and the dict is rolled back on failure (raises
`PluginConfigValidationError`).

### Extending `PluginContext`

If a host needs to attach extras to the context plugins receive
(`ctx.app`, `ctx.event_bus`, `ctx.task_queue`, …), subclass
`PluginContext` and pass the subclass — typically bound via
`functools.partial` — as `context_class`:

```python
import functools
from pyplugy import Plugin, PluginContext, PluginManager

class HostContext(PluginContext):
    __slots__ = ("app", "event_bus")

    def __init__(self, *args, app, event_bus, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = app
        self.event_bus = event_bus

manager = PluginManager(
    context_class=functools.partial(HostContext, app=fastapi_app, event_bus=bus),
)
```

The base `PluginContext` uses `__slots__`; subclasses must either declare
their own `__slots__` for the new fields or omit `__slots__` to get a
per-instance `__dict__`.

## Dependencies between plugins

Three kinds of dependency declarations:

| Field               | Semantics                                                                                |
|---------------------|------------------------------------------------------------------------------------------|
| `requires`          | Hard: dep must be loaded; orders the consumer after it. Resolvers can auto-fetch.        |
| `optional_requires` | Soft: if present, orders the consumer after it; if missing, planning proceeds silently.  |
| `peer_requires`     | Hard, host-supplied: must be loaded, but resolvers deliberately skip these.              |
| `conflicts`         | Refuse to coexist with the named plugins (symmetric).                                    |

All four accept `"<name><specifier>"` PEP 440 strings (bare names = any
version):

```python
class Reporting(Plugin):
    version = "0.2.0"
    requires = ("audit>=1.0", "auth<3.0")
    optional_requires = ("metrics",)
    peer_requires = ("event_bus",)
    conflicts = ("legacy_reporting",)
```

On load, the manager performs a topological sort:

- Missing hard dep → `PluginMissingDependencyError` (carries `missing` + `consumer`)
- Missing peer dep → `PluginPeerDependencyError`
- Specifier mismatch → `PluginDependencyError`
- Cycle → `PluginDependencyError`
- Conflict → `PluginConflictError`

### Auto-resolving missing deps

Register a resolver to fetch missing hard deps on demand (peers are
deliberately skipped):

```python
from pathlib import Path

manager.use_entry_point_resolver("myapp.plugins")
manager.use_directory_resolver(Path("./plugins"), recursive=True)

# Or custom:
manager.add_resolver(lambda name: my_lookup(name))  # returns list[Plugin] | None
```

Resolvers run in registration order. The first one returning a non-empty
list contributes its plugins to the batch and planning re-runs.
`resolver_max_rounds` (default 10) caps runaway resolvers.

### Injecting deps into setup signatures

Parameters past `ctx` named after a declared dep are auto-filled with
`ctx.api_of(<name>)`:

```python
class Users(Plugin):
    requires = ("auth",)

    def on_load(self, ctx, auth):  # `auth` = ctx.api_of("auth")
        ...                        # could be a pydantic model, a callable, anything
```

Decorator form works the same way — pyPlugy introspects the wrapped setup:

```python
@plugin("users", requires=["auth"])
def setup(ctx, auth):
    ...
```

`*args` / `**kwargs` are skipped. Parameter names that don't match a
declared dep are left unbound, surfacing as a clear `TypeError` from the
call — pyPlugy doesn't guess.

### Introspection

```python
manager.dependencies_of("reporting")                 # ["audit"]
manager.dependencies_of("reporting", transitive=True)  # ["audit", "auth"]
manager.dependents_of("audit")                       # ["reporting"]
manager.dependency_graph()                           # {"audit": [], "reporting": ["audit"], ...}
manager.dump()                                       # full snapshot incl. graph
```

## Plugin-exported APIs

A plugin can publish a value (callable, pydantic model, dict, …) that its
dependents reach via `ctx.api_of(name)`:

```python
from pydantic import BaseModel

class AuthAPI(BaseModel):
    sign: str

class Auth(Plugin):
    version = "1.0.0"
    api_model = AuthAPI                # optional pydantic schema

    def on_load(self, ctx):
        ctx.export({"sign": "v1"})     # dict → AuthAPI (validated)

class Users(Plugin):
    requires = ("auth",)

    def on_load(self, ctx, auth):       # auto-injected from declared dep
        assert isinstance(auth, AuthAPI)
        assert auth.sign == "v1"
```

`ctx.api_of("auth")` enforces that `"auth"` is declared in
`requires` / `optional_requires` / `peer_requires`. Returns `None` for an
optional dep that isn't loaded. `ctx.export` validates the payload against
`api_model` when declared (dict → model via `model_validate`; existing
instance type-checked).

## Typed events

`ctx.event(name, Schema)` builds a pyHooky `HookPoint` typed with a
pydantic model and bound to the manager's registry — listeners attached
through it are auto-tagged for cleanup:

```python
from pydantic import BaseModel

class LoginEvent(BaseModel):
    user: str

@plugin("auth", version="1.0.0")
def setup(ctx):
    login = ctx.event("auth:login", LoginEvent)

    @login.listen
    def audit(evt: LoginEvent): ...

    login.trigger(user="alice")        # validated against LoginEvent
```

Class-level declaration auto-builds the `HookPoint`s and stores them on
`ctx.events`, namespaced as `"<plugin>:<key>"` in the registry:

```python
class Auth(Plugin):
    events = {"login": LoginEvent, "logout": LogoutEvent}

    def on_load(self, ctx):
        @ctx.events["login"].listen
        def audit(evt: LoginEvent): ...

        ctx.events["login"].trigger(user="alice")
```

## Validated config

Declare `config_model` (a pydantic `BaseModel` subclass) and the manager
validates `ctx.config` on load — and on every `update_config` call,
rolling back the dict if validation fails:

```python
class AuthConfig(BaseModel):
    secret: str

class Auth(Plugin):
    version = "1.0.0"
    config_model = AuthConfig

    def on_load(self, ctx):
        cfg = ctx.config_model          # AuthConfig instance, validated
        assert cfg is not None
        connect(cfg.secret)

manager = PluginManager(configs={"auth": {"secret": "abc"}})
manager.load(Auth())                    # OK
manager.update_config("auth", {"secret": 123})  # raises PluginConfigValidationError
```

`ctx.config` still returns the raw dict — `config_model` is the validated
snapshot, refreshed on each `update_config`.

## Hot-swap

Replace a running plugin instance with a new one of the same name —
dependents are cascade-reloaded around the swap so they pick up the new
exported API:

```python
class AuthV2(Plugin):
    name = "auth"
    version = "2.0.0"
    api_model = AuthAPI

    def on_load(self, ctx):
        ctx.export({"sign": "v2"})

manager.swap("auth", AuthV2())          # users sees v2 after reload
```

`swap` rejects a different-named replacement (it's not a rename) and works
fine with zero dependents (degenerates to unload + load). Async variant:
`manager.aswap`.

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

| Target                          | Fires                                                                |
|---------------------------------|----------------------------------------------------------------------|
| `pyplugy:plugin:load`           | After a plugin reaches LOADED                                        |
| `pyplugy:plugin:enable`         | After enable                                                         |
| `pyplugy:plugin:disable`        | After disable                                                        |
| `pyplugy:plugin:unload`         | Before the plugin's hooks/tasks are torn down                        |
| `pyplugy:plugin:error`          | When any lifecycle method raises `(plugin, exc)`                     |
| `pyplugy:plugin:config-changed` | After `manager.update_config` mutates a loaded plugin's config        |

Listen with `pyhooky.on`:

```python
from pyhooky import on
from pyplugy import HOOK_PLUGIN_LOAD

@on(HOOK_PLUGIN_LOAD)
def announce(plugin):
    print("loaded:", plugin.manifest.name)
```

## Integration with pyWorkflowy

`ctx.task` and `ctx.scheduler` come from the pyWorkflowy surface, wired via
the manager:

```python
import pyworkflowy
from pyworkflowy import TaskBase
from pyworkflowy.schedule import Scheduler
from pyplugy import PluginManager, plugin

manager = PluginManager(tasky=pyworkflowy, scheduler=Scheduler())

@plugin("cron-cleanup", version="0.1.0")
def setup(ctx):
    @ctx.task
    def cleanup(): ...                 # decorator form

    class Vacuum(TaskBase):            # pyWorkflowy class form
        name = "vacuum"
        def run(self): ...

    ctx.task(Vacuum)                   # registers and returns the Task instance
    ctx.scheduler.every("5m").do(cleanup)
```

Without a `tasky` argument, `ctx.task(fn)` raises a clear `RuntimeError`.
The `TaskBase`-class form is self-contained (it bypasses the `tasky=`
requirement) and works on any manager. pyPlugy itself only depends on a
small `typing.Protocol` defined in `pyplugy._tasky_protocol`, so the
`tasky=` argument can be a stub for tests.

## Errors

| Error                            | When                                                                        |
|----------------------------------|-----------------------------------------------------------------------------|
| `PluginError`                    | Base                                                                        |
| `PluginNotFoundError`            | `get` / `enable` / `disable` / `unload` on unknown                          |
| `PluginAlreadyLoadedError`       | Loading a plugin whose name is already loaded                               |
| `PluginDependencyError`          | Specifier mismatch, cycle, blocked cascade, generic dep failure             |
| `PluginMissingDependencyError`   | Hard `requires` entry missing (carries `missing` + `consumer`)              |
| `PluginPeerDependencyError`      | `peer_requires` entry missing — host must supply, resolvers won't help      |
| `PluginConflictError`            | `conflicts` entry collides with another loaded or about-to-load plugin       |
| `PluginConfigValidationError`    | `ctx.config` fails its `config_model` (load or `update_config`)             |
| `PluginLoadError`                | `on_load` / `on_enable` / decorator setup raised                            |
| `PluginUnloadError`              | `on_unload` raised                                                          |
| `PluginManifestError`            | Invalid manifest (missing name, malformed requirement, …)                    |

All inherit from `PluginError`, so one `except PluginError` catches the
family; `isinstance` discriminates.

## Development

```bash
uv sync
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run pyrefly check
```
