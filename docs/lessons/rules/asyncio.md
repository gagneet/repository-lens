# asyncio: lessons learnt

**Scope.** Tasks, gather, cancellation, event loops, blocking calls.

| id | severity | lesson |
|---|---|---|
| AS-001 | high | A fire-and-forget `create_task` can be garbage-collected mid-flight |
| AS-002 | medium | `gather` does not cancel siblings, and `return_exceptions=True` results go unchecked |
| AS-003 | medium | Swallowing `CancelledError` breaks shutdown and timeouts |
| AS-004 | high | Two `asyncio.run()` calls in one script: 'Future attached to a different loop' |
| AS-005 | medium | `asyncio.get_event_loop()` under Python 3.12 is order-dependent in tests |
| AS-006 | medium | Module-level async client singletons are bound to one loop |
| AS-007 | high | `asyncio.gather` added without `import asyncio`, with the NameError swallowed |

## AS-001 — A fire-and-forget `create_task` can be garbage-collected mid-flight

*Severity:* **high** · *Stacks:* asyncio

**Symptom.** Background work vanished, and 'Task exception was never retrieved' appeared at GC time.

**Root cause.** The event loop keeps only weak references to tasks.

**Resolution.** Keep tasks in a set with `add_done_callback(set.discard)`, or use a `TaskGroup`.

**Prevention.** Enable ruff RUF006.

**How it is checked.**

- `regex`: `^\s*asyncio\.create_task\(`
- `lint`: ruff RUF006

**Sources.** <https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task>

## AS-002 — `gather` does not cancel siblings, and `return_exceptions=True` results go unchecked

*Severity:* **medium** · *Stacks:* asyncio

**Symptom.** Failures were unobserved while sibling tasks kept running. An unbounded `gather` over 87 items exhausted a pool of 15 connections.

**Root cause.** The first exception propagates while the others continue. Exceptions returned in the result list are easy to ignore.

**Resolution.** Use `TaskGroup` (3.11+), or check `isinstance(r, BaseException)`. Bound the fan-out with a semaphore, or better, with a set-based query.

**Prevention.** Lint for gather over a comprehension that opens DB sessions.

**How it is checked.**

- `regex`: `gather\(\*\[.*for .* in`
- `regex`: `return_exceptions=True`

**Evidence.** 306682c57; 2ebcabe4d

**Sources.** <https://docs.python.org/3/library/asyncio-task.html#task-groups>

## AS-003 — Swallowing `CancelledError` breaks shutdown and timeouts

*Severity:* **medium** · *Stacks:* asyncio

**Symptom.** Shutdown hung, and `asyncio.timeout()` stopped working.

**Root cause.** `except BaseException` or a bare `except:` in a coroutine catches cancellation (a `BaseException` since 3.8).

**Resolution.** Catch `Exception`, and add a regression test that cancellation propagates.

**Prevention.** Enable ruff BLE001 and E722.

**How it is checked.**

- `regex`: `except\s*:|except BaseException`

**Evidence.** 4013acac6 (2026-08-06); ef545cce8

## AS-004 — Two `asyncio.run()` calls in one script: 'Future attached to a different loop'

*Severity:* **high** · *Stacks:* asyncio

**Symptom.** The dry run always worked, and the first real `--apply` crashed.

**Root cause.** A lazily created connection pool was bound to the first loop, which was already closed.

**Resolution.** Use a single `asyncio.run(main())`.

**Prevention.** Lint for more than one `asyncio.run` in a module's main path.

**How it is checked.**

- `regex`: `asyncio\.run\(  (count > 1 per module)`

**Evidence.** tasks/lessons.md LESSON 35 (2026-08-06)

## AS-005 — `asyncio.get_event_loop()` under Python 3.12 is order-dependent in tests

*Severity:* **medium** · *Stacks:* asyncio, pytest

**Symptom.** 77 tests passed in isolation and 38 failed in the full run.

**Root cause.** Since 3.12, no loop is created implicitly in a thread that has none.

**Resolution.** Use `asyncio.run()`.

**Prevention.** Grep for `get_event_loop().run_until_complete`.

**How it is checked.**

- `regex`: `get_event_loop\(\)\.run_until_complete`

**Evidence.** 1bb7e78c8 (2026-09-04)

## AS-006 — Module-level async client singletons are bound to one loop

*Severity:* **medium** · *Stacks:* asyncio, pytest, motor, sqlalchemy

**Symptom.** 'Task attached to a different loop' within a single test module.

**Root cause.** TestClient allocates a loop per instance, and `from database import db` holds a direct reference, so rebinding `database.db` never reaches the modules that imported it.

**Resolution.** Use function-scoped reset fixtures and mutate the singleton in place. Give loop-bound clients the same scope as their loop (pytest-asyncio `loop_scope`).

**Prevention.** Set `asyncio_mode`, `asyncio_default_fixture_loop_scope` and the test loop scope explicitly in pytest.ini.

**How it is checked.**

- `config`: pytest.ini defines asyncio_mode and asyncio_default_fixture_loop_scope
- `regex`: `def event_loop\(`

**Evidence.** 379cec1d9 (2026-08-10); e7b172268

**Sources.** <https://pytest-asyncio.readthedocs.io/en/stable/reference/configuration.html>

## AS-007 — `asyncio.gather` added without `import asyncio`, with the NameError swallowed

*Severity:* **high** · *Stacks:* asyncio, imports

**Symptom.** A feature silently did nothing.

**Root cause.** The call site sat inside `try/except Exception`.

**Resolution.** Added the import.

**Prevention.** Make ruff F821 blocking.

**How it is checked.**

- `shell`: `grep -rln 'asyncio\.\(gather\|wait\)' | xargs grep -L '^import asyncio'`

**Evidence.** ef545cce8 (2026-06-05)
