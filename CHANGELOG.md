# CHANGELOG


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
