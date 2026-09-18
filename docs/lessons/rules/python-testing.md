# Python Testing: lessons learnt

**Scope.** pytest, mocks, fixtures, async tests, isolation.

| id | severity | lesson |
|---|---|---|
| PT-001 | critical | A module-level `load_dotenv(override=True)` in one test file redirected the whole suite to production |
| PT-002 | critical | `patch(..., create=True)` on a nonexistent attribute lets the real path run |
| PT-003 | high | Patch where a name is USED, not where it is defined |
| PT-004 | critical | Unconstrained mocks on both sides of a boundary: a 100% inert feature with 44 green tests |
| PT-005 | high | A MagicMock where an AsyncMock is needed |
| PT-006 | high | Patching `asyncio.create_task` or a module's `asyncio` with a bare MagicMock hangs at teardown |
| PT-007 | high | Mock data that copies the code's own wrong field names confirms the bug |
| PT-008 | high | Session-scoped autouse fixtures with side effects, and raising finalisers |
| PT-009 | medium | An undeclared test plugin: `asyncio_mode=auto` without pytest-asyncio installed |
| PT-010 | high | Wall-clock-dependent tests fail for only part of each period |
| PT-011 | medium | A filtered, partial or wrong-cwd test run supports no '0 failures' claim |
| PT-012 | medium | Source-text oracles in tests match prose, docstrings and proximity |
| PT-013 | low | `inspect.getsource` against a file edited during the run |
| PT-014 | medium | `SimpleNamespace` stand-ins for request models break when a model gains a field |
| PT-015 | low | A stale `__pycache__` corrupts a bisect; `py_compile` writes a `.pyc` |
| PT-016 | high | Tests without a DB isolation layer write to the real database through default sessions |

## PT-001 — A module-level `load_dotenv(override=True)` in one test file redirected the whole suite to production

*Severity:* **critical** · *Stacks:* pytest, config

**Symptom.** 11,700 tests wrote to the production database for four days, but only in full runs. A single-file run was fine, which inverted the symptom.

**Root cause.** pytest imports every module at collection time, so one module's `override=True` re-pointed `DB_NAME`, and an autouse fixture re-read `os.environ` for every test.

**Resolution.** Capture the database choice once. A guard test pins it, and a subprocess-import test checks that sentinel env vars survive. The conftest refuses the production DB by name.

**Prevention.** Ban `override=True` under tests/. Assert the active DB name at the end of the session.

**How it is checked.**

- `regex`: `load_dotenv\([^)]*override=True  (in tests/ or modules tests import)`

**Evidence.** b68a4ed68 (2026-08-31); 8ea663861 (2026-09-05); tests/backend/test_session_db_pinning.py

## PT-002 — `patch(..., create=True)` on a nonexistent attribute lets the real path run

*Severity:* **critical** · *Stacks:* pytest, unittest.mock

**Symptom.** The suite relayed 2,753 real SMTP sends while the email kill switch was on, exhausted the provider's quota and bounced real notices.

**Root cause.** The patch targeted a function that did not exist, `create=True` hid that, and the send sat inside `try/except: pass`.

**Resolution.** A transport-level gate that tests cannot mock away (it refuses under pytest), plus a session-autouse fixture that makes `smtplib.SMTP`/`SMTP_SSL` raise.

**Prevention.** Verify by counting the sent-log rows before and after a suite run.

**How it is checked.**

- `regex`: `patch\([^)]*create=True`
- `regex`: `except[^:]*:\s*pass  (in test files)`

**Evidence.** b58a5a4d3 (2026-09-01); f7805a246

## PT-003 — Patch where a name is USED, not where it is defined

*Severity:* **high** · *Stacks:* pytest, unittest.mock

**Symptom.** A test failed with `'mongo' == 'postgres'`, or passed by accident on a machine with a live database. Tests read and wrote real stores through unpatched modules.

**Root cause.** `from x import f` binds at import time. Extracting a query into a new store module, which has its own `from database import db`, silently unmocked every caller's `patch('routers.x.db')`.

**Resolution.** Patch the importing module's name, plus a structural test that parses the calls a function makes and asserts each store call is mocked.

**Prevention.** After any extraction refactor, grep the tests that patch the old location.

**How it is checked.**

- `review`: for each patch('a.b.f'), find modules doing `from a.b import f` that the target calls

**Evidence.** d5c15ffe1 (2026-09-06); 6aefd9233; e7b172268

**Sources.** <https://docs.python.org/3/library/unittest.mock.html#where-to-patch>

## PT-004 — Unconstrained mocks on both sides of a boundary: a 100% inert feature with 44 green tests

*Severity:* **critical** · *Stacks:* pytest, unittest.mock

**Symptom.** A shipped feature did nothing in production while its whole test suite, CI and an AI review were green.

**Root cause.** An `AsyncMock()` without a spec accepts any arguments, so the failing call's wrong signature was never exercised.

**Resolution.** Patch only the unit of work, let real argument validation run, and use `autospec=True` / `create_autospec`.

**Prevention.** Require at least one test per boundary that calls the real callee.

**How it is checked.**

- `regex`: `(Async|Magic)Mock\(\)(?!.*spec)`

**Evidence.** 831c9285a; 4d46680a3 (2026-09-08)

**Sources.** <https://docs.python.org/3/library/unittest.mock.html#autospeccing>

## PT-005 — A MagicMock where an AsyncMock is needed

*Severity:* **high** · *Stacks:* pytest, asyncio

**Symptom.** `TypeError: object MagicMock can't be used in 'await'`, often swallowed. Adding one awaited DB call broke tests in six files.

**Root cause.** The production code awaits the method, and a plain MagicMock returns a non-awaitable. Inside `gather` the error can be hidden.

**Resolution.** Use `AsyncMock` for awaited methods, and shared DB-mock factories.

**Prevention.** After adding an awaited call on a shared path, grep for its mocks.

**How it is checked.**

- `regex`: `mock_db\.\w+\.\w+\s*=\s*MagicMock\(`

**Evidence.** 9dd281371; backend/CLAUDE.md

## PT-006 — Patching `asyncio.create_task` or a module's `asyncio` with a bare MagicMock hangs at teardown

*Severity:* **high** · *Stacks:* pytest, asyncio, sqlalchemy

**Symptom.** 50 tests PASSED, then the process hung forever at loop finalisation.

**Root cause.** `AsyncSession.__aexit__` uses `asyncio.shield(create_task(...))`, so the connection never returned to the pool. Patching the whole module turned `gather` into a MagicMock too.

**Resolution.** A replacement that closes the coroutine and returns a resolved Future, with `gather` passed through.

**Prevention.** Diagnose hangs with `PYTHONFAULTHANDLER=1 timeout -s ABRT 45 pytest ...`.

**How it is checked.**

- `regex`: `patch\(['\"](asyncio\.create_task|[\w.]+\.asyncio)['\"]\)(?!.*side_effect)`

**Evidence.** 26536b233 (2026-09-06); 5282da9bb

## PT-007 — Mock data that copies the code's own wrong field names confirms the bug

*Severity:* **high** · *Stacks:* pytest

**Symptom.** 13 tests mocked `site_settings.find_one({'id':'main'})`, a document that has never existed, so the real API returned $0 for every tenant while the tests passed.

**Root cause.** The mocks were written from the code, not from the data.

**Resolution.** Built fixtures from real documents or schemas.

**Prevention.** Flag mocks of collections or keys that no writer ever writes.

**How it is checked.**

- `review`: cross-check mock keys/collections against grep of inserts/updates

**Evidence.** memory: pattern_a_test_that_mirrors_the_bug_confirms_it

## PT-008 — Session-scoped autouse fixtures with side effects, and raising finalisers

*Severity:* **high** · *Stacks:* pytest

**Symptom.** Every test run raised the production login rate limit from 10 to 200 per minute. A raising finaliser failed a fully green run.

**Root cause.** Fixing a broken shared client re-armed an autouse fixture that wrote to it, and the finaliser had no guard.

**Resolution.** Removed the write, and wrapped the finalisers.

**Prevention.** When you repair a shared handle, grep its callers and ask which of them WRITE.

**How it is checked.**

- `regex`: `scope=['\"]session['\"],\s*autouse=True`

**Evidence.** 62cb84d47; 357fe32c8 (2026-09-08)

## PT-009 — An undeclared test plugin: `asyncio_mode=auto` without pytest-asyncio installed

*Severity:* **medium** · *Stacks:* pytest, packaging

**Symptom.** Tests errored in CI with 'async fixture with no plugin' while passing locally.

**Root cause.** pytest-asyncio was installed in the dev venv but absent from the CI requirements.

**Resolution.** Declared the plugin.

**Prevention.** If pytest.ini sets `asyncio_mode`, require pytest-asyncio in the requirements file.

**How it is checked.**

- `config`: pytest.ini asyncio_mode set ⇒ pytest-asyncio in requirements

**Evidence.** 9426f7de5; 61cd1c392 (2026-09-02)

## PT-010 — Wall-clock-dependent tests fail for only part of each period

*Severity:* **high** · *Stacks:* pytest, time

**Symptom.** Tests passed for most of a quarter and failed for the 14 days after each due date. They were written off as flaky.

**Root cause.** The logic read the real clock, and the grace-window branch existed only inside that window.

**Resolution.** Pass `today` explicitly and pin `_now` in tests.

**Prevention.** A defect that appears for 14 days a quarter reads as flaky, so treat intermittent date failures as real.

**How it is checked.**

- `review`: date.today()/datetime.now() inside functions that take a date param; date-relative asserts without a clock fixture

**Evidence.** 4e3a38b28 (2026-09-01); b40e914e4; 3a834630d

## PT-011 — A filtered, partial or wrong-cwd test run supports no '0 failures' claim

*Severity:* **medium** · *Stacks:* pytest

**Symptom.** `pytest -k capital` skipped `TestCapitalShock` (6 failures). A path argument skipped a whole test root (133 tests). Running from the wrong directory produced FileNotFoundErrors that were mistaken for pre-existing failures.

**Root cause.** `-k` is case-sensitive, and path arguments override `testpaths`.

**Resolution.** One canonical command (`venv/bin/python -m pytest -q` from the repo root), run in full after any fix.

**Prevention.** Record the tree SHA in the run header, and don't edit files during a background run.

**How it is checked.**

- `ci`: forbid -k/path-filtered pytest as a merge gate

**Evidence.** memory: pattern_filtered_test_runs_hide_failures; CLAUDE.md §Backend

## PT-012 — Source-text oracles in tests match prose, docstrings and proximity

*Severity:* **medium** · *Stacks:* pytest, ast

**Symptom.** A `getsource` search for `date.today()` failed on its own docstring. A proximity check (`except Exception` within 1,200 characters) passed broken code and failed correct code.

**Root cause.** Substring and regex tests do not know the difference between code and comments.

**Resolution.** Use `tokenize` to drop COMMENT and STRING tokens, `__code__.co_names`, or AST walks.

**Prevention.** A change that should move a number and doesn't means the oracle is broken.

**How it is checked.**

- `regex`: `inspect\.getsource\(.*\)\s*(in|\.find|re\.search)`

**Evidence.** b40e914e4; 5282da9bb

## PT-013 — `inspect.getsource` against a file edited during the run

*Severity:* **low** · *Stacks:* python, pytest

**Symptom.** A source-scanning test failed in a background run, then passed on its own and looked flaky.

**Root cause.** Line numbers are recorded at import time and the file is read at call time, so a +3-line edit shifted the slice.

**Resolution.** Don't edit files during a test run, and run CI on an immutable checkout.

**Prevention.** Prefer AST checks over `getsource` slicing.

**How it is checked.**

- `review`: tests slicing getsource by line numbers

**Evidence.** memory: pattern_editing_source_during_a_background_test_run

## PT-014 — `SimpleNamespace` stand-ins for request models break when a model gains a field

*Severity:* **medium** · *Stacks:* pytest, pydantic

**Symptom.** One commit produced 51 failing tests with `AttributeError`.

**Root cause.** The tests duck-typed request models.

**Resolution.** Construct real models (`Model(...)` or `model_construct`).

**Prevention.** Grep for SimpleNamespace passed as a model.

**How it is checked.**

- `regex`: `SimpleNamespace\(  (in tests passed to BaseModel-annotated params)`

**Evidence.** 0d0145d2e (2026-08-19)

## PT-015 — A stale `__pycache__` corrupts a bisect; `py_compile` writes a `.pyc`

*Severity:* **low** · *Stacks:* python

**Symptom.** A confident, reproducible, completely wrong first-bad commit. A permission error (EACCES) reported as a syntax error.

**Root cause.** Bytecode from another checkout was reused, and `py_compile` writes its output next to the source.

**Resolution.** Set `PYTHONDONTWRITEBYTECODE` or clear the caches between checkouts, and check syntax with `compile(src, path, 'exec')`.

**Prevention.** Clean caches in any bisect script.

**How it is checked.**

- `shell`: `git bisect run sh -c 'find . -name __pycache__ -exec rm -rf {} +; pytest ...'`

**Evidence.** 26536b233; f876d1ab7 (2026-08-28)

## PT-016 — Tests without a DB isolation layer write to the real database through default sessions

*Severity:* **high** · *Stacks:* pytest, postgresql

**Symptom.** Tests silently wrote real rows. Best-effort telemetry writers hid the errors.

**Root cause.** Services fell back to a default session built from `DATABASE_URL` when `pg_session_factory=None`, and `_safe_*` writers never raise.

**Resolution.** Autouse mocks for the default factories, plus a 'production unreachable under pytest' guard.

**Prevention.** A dedicated test database, with the redirect in the ROOT conftest.

**How it is checked.**

- `regex`: `pg_session_factory=None  (in tests/)`

**Evidence.** memory: pattern_test_no_db_override_writes_to_real_control_plane
