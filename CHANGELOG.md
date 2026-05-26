# CHANGELOG


## v0.4.0 (2026-05-26)

### Chores

- Ruff cleanup for 0.4 tasks work
  ([`a864af0`](https://github.com/KilianSen/pyPlugy/commit/a864af09f645a533f90c3b9e593e922f982c9b6b))

Remove redundant string-quoted annotations (covered by __future__ annotations) and wrap the dump()
  tasks comprehension within the 100-col limit.

### Documentation

- Update README for improved clarity and structure, enhance plugin context details
  ([`be77408`](https://github.com/KilianSen/pyPlugy/commit/be7740801636837f2f237d0c800f2fe765620fc7))

- **tasks**: Document metadata, auto-wire, and 0.4 breaking changes
  ([`dca380c`](https://github.com/KilianSen/pyPlugy/commit/dca380c9d1f53e2e5a962efc1955891d48f34ed9))

Updates README's plugin_tasks signature and adds two new subsections under "Integration with
  pyWorkflowy" (Triggers auto-wire on enable, Host metadata). CHANGELOG gains a 0.4.0 entry listing
  the new features and the three breaking changes to the tasks surface.

### Features

- **tasks**: Add _scheduled_jobs bookkeeping slot to PluginManager
  ([`1df3668`](https://github.com/KilianSen/pyPlugy/commit/1df36680f50a1ba3f98a22bce623984376ecfe00))

Tracks scheduler jobs per plugin so disable() can cancel them. Initialised empty; populated in a
  follow-up commit that wires trigger/cron tasks to the scheduler on enable.

- **tasks**: Add PluginTaskInfo value-object
  ([`eed5d65`](https://github.com/KilianSen/pyPlugy/commit/eed5d65275a48c99613323e4c97ac56f6ceb4b85))

Pairs a registered task with host-supplied metadata. Frozen + slotted; metadata defaults to an empty
  mapping. Re-exported from the package root for use by hosts that introspect ctx.tasks /
  plugin_tasks(name).

- **tasks**: Auto-wire task triggers + cron on enable; teardown on disable
  ([`63f0d50`](https://github.com/KilianSen/pyPlugy/commit/63f0d506a479268ac60ffca9f4e71939951ea600))

After a plugin's on_enable returns, the manager walks ctx.tasks and binds each task with non-empty
  triggers via scheduler.bind_tasks, and each task exposing a cron attribute via
  scheduler.cron(expr).do(task). The resulting jobs are stored on _scheduled_jobs[plugin_name] and
  cancelled when the plugin transitions to DISABLED. Both sync and async lifecycle paths
  participate; the cron path is defensive (no-op when the task has no .cron attribute —
  pyworkflowy.Task doesn't yet).

Hosts can drop their _wire_task / attach_scheduler / wire_pending_triggers boilerplate.

- **tasks**: Ctx.task(metadata=...) + PluginTaskInfo return shape
  ([`1839b0d`](https://github.com/KilianSen/pyPlugy/commit/1839b0da515c9ef61610f5d9f9bd109bfb6efd91))

ctx.task accepts an optional metadata mapping that pyplugy stores verbatim alongside the underlying
  task; ctx.tasks and PluginManager.plugin_tasks(name) now return tuple[PluginTaskInfo, ...] so
  hosts can round-trip from a plugin name back to the actual task objects + their host metadata.
  Internal storage slot renamed _tasks → _task_infos to reflect the type shift.

PluginInfo.tasks still returns task names; will be bumped to PluginTaskInfo in a follow-up commit.

BREAKING CHANGE: PluginManager.plugin_tasks(name) returns tuple[PluginTaskInfo, ...] instead of
  list[str]. Use [i.task.name for i in manager.plugin_tasks(name)] for the prior shape.
  PluginContext.tasks shape changes identically.

- **tasks**: Plugininfo.tasks tuple[PluginTaskInfo, ...]; dump serializes name+metadata
  ([`ab159e6`](https://github.com/KilianSen/pyPlugy/commit/ab159e6e57a59d6c1ede4b6f5be4e1a485c43d05))

PluginInfo.tasks now carries the rich PluginTaskInfo entries instead of bare names, matching the new
  ctx.tasks / plugin_tasks shape. PluginManager.dump() serializes each entry as {"name": ...,
  "metadata": ...} so the JSON snapshot stays machine-readable.

BREAKING CHANGE: PluginInfo.tasks is now tuple[PluginTaskInfo, ...] instead of tuple[str, ...].

- **tasks**: Widen SchedulerProtocol with bind_tasks and cancel
  ([`ecf2d83`](https://github.com/KilianSen/pyPlugy/commit/ecf2d83bc5a360ae1d19e1eeb0dd590c7794ad5a))

SchedulerProtocol now requires bind_tasks(*tasks) and cancel(job) — the surface the manager needs to
  auto-wire trigger-driven tasks at enable time and tear them down at disable. pyworkflowy.Scheduler
  already satisfies both. FakeScheduler/FakeTask in conftest grow matching test doubles
  (FakeTask.triggers, FakeScheduler.bound/cancelled).

### Testing

- **tasks**: Real-pyworkflowy end-to-end for triggers auto-wire
  ([`a151afe`](https://github.com/KilianSen/pyPlugy/commit/a151afee1e4b5af86ff7713ba40c8b0848a44a45))

Proves the protocol surface pyplugy declares matches pyworkflowy's real Scheduler — declaring
  triggers via ctx.task auto-registers an event-driven job, and disable cancels it.


## v0.3.0 (2026-05-24)

### Features

- Update pyworkflowy dependency to 0.2.0 and add pydantic support
  ([`9e72769`](https://github.com/KilianSen/pyPlugy/commit/9e727695aeba460bc5458b3377e1cd8f6f8f14c0))

### Refactoring

- Improve type hints and validation logic in plugin and manager modules
  ([`3626873`](https://github.com/KilianSen/pyPlugy/commit/3626873b9b67a318c448eda0dc7e423548dcde49))

- Remove unnecessary noqa comments and improve type hints in tests
  ([`06cacb9`](https://github.com/KilianSen/pyPlugy/commit/06cacb94c55b6a5263baa42bc9629722571ab22e))

- Streamline function signatures and remove unnecessary line breaks
  ([`1fdb2aa`](https://github.com/KilianSen/pyPlugy/commit/1fdb2aacfe0235b23062aa1e212590c8e800d997))


## v0.2.0 (2026-05-23)

### Features

- Add async lifecycle support and runtime config updates
  ([`08a6dbb`](https://github.com/KilianSen/pyPlugy/commit/08a6dbb3a765e9e806b2957fe4f3c8f22c33d7ab))

- Add async lifecycle support and runtime config updates
  ([`6afc4d0`](https://github.com/KilianSen/pyPlugy/commit/6afc4d0583534a1dc8a7bfe95944d68770637012))


## v0.1.0 (2026-05-23)

### Chores

- Scaffold project (license, gitignore, pyproject, readme)
  ([`a26522f`](https://github.com/KilianSen/pyPlugy/commit/a26522f2819475f518caf2c44d4da8d760f4d792))

### Continuous Integration

- Add CI + Release workflows mirroring pyHooky
  ([`62210bb`](https://github.com/KilianSen/pyPlugy/commit/62210bb7080dd2e82ff1e35b5117c174e687c574))

Adds .github/workflows/ci.yml (matrix test on 3.11-3.14 with ruff check + format check + pyrefly +
  pytest --cov) and release.yml (python-semantic-release with PyPI publish on master). Applies ruff
  format across the codebase so the format-check step is green.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

### Features

- Add plugin manifest, exceptions, tasky protocol, and dep graph
  ([`667661d`](https://github.com/KilianSen/pyPlugy/commit/667661d2743cd432b4d5061f316c6dc6b4016ccc))

Introduces the core value-objects: PluginManifest (frozen+slots), Plugin base class with
  class-attribute manifest fields, the @plugin decorator producing equivalent instances, the
  exception hierarchy, the typing.Protocol surface describing the pyTasky bits pyPlugy depends on,
  and the topological dep resolver (PEP 440 specifiers via packaging).

- Add PluginContext, discovery, and PluginManager
  ([`8882695`](https://github.com/KilianSen/pyPlugy/commit/8882695d10f94dfd0ca0b4aedc4be22d66af7a22))

PluginContext exposes the auto-tagged hook helpers (passthroughs to pyHooky with the manager's
  registry baked in) plus the task / scheduler hooks routed through the pyTasky protocol. Discovery
  covers all three modes: entry points, directory scan (sandboxed module names), and JIT loading
  from instance / class / module / dotted path / Path. The manager drives the UNLOADED -> LOADED ->
  ENABLED <-> DISABLED -> UNLOADED lifecycle and wraps every on_load in pyhooky.tag_scope so
  clear_tag tears the plugin down cleanly. Disable is implemented as tear-down-and-rebuild
  (clear_tag + re-run on_load on enable).

- Add pyworkflowy dependency and update package sources
  ([`34cf298`](https://github.com/KilianSen/pyPlugy/commit/34cf298ed19715b10ae57e65ea29dc3e383061f9))

- Wire real pyTasky integration
  ([`9f12517`](https://github.com/KilianSen/pyPlugy/commit/9f12517a2cbdbbeacefb654737b90edc79457dc0))

Enable the [tasks] optional-dependency and the ../pyTasky uv source so pyPlugy can be installed with
  the real workflow engine. Loosen SchedulerProtocol.every to accept Any (pyTasky takes float |
  timedelta, not the str the original stub assumed) and align cron's parameter name.

Adds end-to-end integration tests that drive real pytasky.Task / TaskRunner / Scheduler through the
  PluginManager, including a hooks-and-tasks-cleared-on-unload bundled case proving the tag-scope
  isolation works alongside task ownership.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

### Refactoring

- Follow pyTasky → pyWorkflowy rename
  ([`0386222`](https://github.com/KilianSen/pyPlugy/commit/03862227473401cfe2b061a8c2685f519b8414e7))

Updates the optional dependency, uv source path, integration test file name, README, and all
  references from pytasky / pyTasky to pyworkflowy / pyWorkflowy. The internal TaskyProtocol class,
  tasky= parameter, and _tasky_protocol module are kept — they describe a structural role
  ("task-engine-shaped"), not a specific implementation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

### Testing

- Cover manager, lifecycle, decorator/class forms, discovery, deps, and ctx tasks/hooks
  ([`9107673`](https://github.com/KilianSen/pyPlugy/commit/91076737eaaa9104a516fb37a9bed629fe9913e7))

Synthesised entry points via importlib.metadata monkeypatch (no real EP required), tempdir-based
  directory scan with recursion, and a FakeTasky that satisfies the pyTasky protocol so ctx.task
  tests don't require the sibling library.
