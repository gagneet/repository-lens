# Python Core: lessons learnt

**Scope.** Language and stdlib traps: datetimes, rounding, defaults, imports, excepts.

| id | severity | lesson |
|---|---|---|
| PY-001 | high | `d.get(k, default)` does not default a key that is present but None |
| PY-002 | medium | Unparenthesised `x.get(k) or {}.get(...)` binds the wrong way |
| PY-003 | medium | `getattr(obj, name, default)` does not protect against a property that does I/O |
| PY-004 | high | Undefined names in an `except (A, B)` tuple only fail when the try raises |
| PY-005 | high | `''` passed where `None` means 'absent' |
| PY-006 | critical | A try/except that logs and continues turns a hard failure into a permanent silent one |
| PY-007 | high | A function-local import of a nonexistent module hidden by `except: return []` |
| PY-008 | medium | A bare except around `exec_module` leaves a half-loaded module |
| PY-009 | high | An exception classifier must not treat programming errors as outages |
| PY-010 | medium | A try scope narrower than its 'must never fail' claim |
| PY-011 | high | `__main__` or `close()` indented inside a loop: valid syntax, wrong behaviour |
| PY-012 | high | `python path/to/script.py` puts the script's own directory on `sys.path[0]` |
| PY-013 | medium | Eager heavy imports cost seconds at startup; `ImportError` is not the only failure |
| PY-014 | medium | Dependency pins: exact-pin conflicts, editable installs masking pins, undeclared imports |
| PY-015 | high | A 'one-line' suggested edit introduces a missing import |
| PY-016 | high | A context-dependent helper silently misbehaves outside a request |
| PY-017 | critical | Float money plus `round()` (banker's rounding) produces different cents |
| PY-018 | high | Naive datetimes: `utcnow()` is deprecated, and `date.today()` is local time |
| PY-019 | medium | A function returning a numeric string is compared numerically |
| PY-020 | high | Secrets in exception text end up in logs and databases |
| PY-021 | high | A redaction filter on the root logger misses uvicorn's loggers |
| PY-022 | medium | Fire-and-forget `Popen` with no liveness check; a zombie passes `os.kill(pid, 0)` |
| PY-023 | medium | A subprocess argument list still lets a value starting with `-` become an option |
| PY-024 | high | User input interpolated into a regex |

## PY-001 — `d.get(k, default)` does not default a key that is present but None

*Severity:* **high** · *Stacks:* python

**Symptom.** Several live 500s: `None >= 85`, `None.capitalize()`, `float(None)`. A sweep found 42 more sites.

**Root cause.** The default applies only when the key is absent, and both stores wrote explicit nulls.

**Resolution.** `(d.get(k) or default)` or an explicit `is None` branch. Where 0 is a real value, return 'unmeasured' rather than 0.

**Prevention.** Treat 'missing' and 'null' identically at read boundaries.

**How it is checked.**

- `ast`: Call .get(k, default) whose result flows into a comparison, arithmetic, float()/int() or attribute access

**Evidence.** 39efc770f (2026-08-27); 122efca38; f66300c24; 7f303a6bb (2026-09-09); footgun #14

## PY-002 — Unparenthesised `x.get(k) or {}.get(...)` binds the wrong way

*Severity:* **medium** · *Stacks:* python

**Symptom.** The wrong object was returned silently.

**Root cause.** It parses as `x.get(k) or ({}.get(...))`.

**Resolution.** Parenthesise it: `(x.get(k) or {}).get(...)`.

**Prevention.** Lint rule.

**How it is checked.**

- `ast`: BoolOp(Or) whose last operand is a Call on a Dict/List literal attribute

**Evidence.** 122efca38

## PY-003 — `getattr(obj, name, default)` does not protect against a property that does I/O

*Severity:* **medium** · *Stacks:* python, pymongo

**Symptom.** A fully green CI run failed at session teardown with `ServerSelectionTimeoutError`.

**Root cause.** `MongoClient.address` and `.nodes` are properties that perform server selection, and `getattr`'s default catches only `AttributeError`.

**Resolution.** Decide from configuration (URI/env) rather than probing the client, and wrap the access in try/except.

**Prevention.** Be suspicious of `getattr` defaults on client objects, especially in finalisers.

**How it is checked.**

- `regex`: `getattr\(\w*client\w*,\s*['\"](address|nodes|primary|secondaries)['\"]`

**Evidence.** 357fe32c8 (2026-09-08)

## PY-004 — Undefined names in an `except (A, B)` tuple only fail when the try raises

*Severity:* **high** · *Stacks:* python, error-handling

**Symptom.** A mirror failure became a `NameError` 500 after the primary write had already committed.

**Root cause.** The except clause named exception classes the module never imported, and the clause is evaluated only when an exception occurs.

**Resolution.** Removed the wrong names.

**Prevention.** Make ruff F821 a blocking check.

**How it is checked.**

- `lint`: ruff/pyflakes F821 (covers ExceptHandler.type names)

**Evidence.** 2d6d50fab (2026-09-04)

## PY-005 — `''` passed where `None` means 'absent'

*Severity:* **high** · *Stacks:* python, validation

**Symptom.** An email ingest recorded zero messages, because every message raised 422.

**Root cause.** The caller passed `actor_user_id=''`. The validator rejected it, and `CAST('' AS UUID)` fails too.

**Resolution.** An `_optional_uuid` helper treats None as absent, while `''` or malformed input stays an error. The caller now passes None.

**Prevention.** Normalise empty strings at the boundary, or reject them explicitly.

**How it is checked.**

- `ast`: call sites passing '' to params annotated Optional[UUID|str]

**Evidence.** 4d46680a3 (2026-09-08)

## PY-006 — A try/except that logs and continues turns a hard failure into a permanent silent one

*Severity:* **critical** · *Stacks:* python, error-handling

**Symptom.** No registration ever created a user row (the FK violation was swallowed). Status changes were written to only one store (a cast to a nonexistent enum was swallowed). A fresh deploy had no administrator. A cron kill switch vanished under `except ImportError: pass`.

**Root cause.** Broad excepts around writes, labelled 'non-fatal'.

**Resolution.** Narrowed the excepts, asserted post-conditions in tests, and made safety switches fail closed.

**Prevention.** If a write must happen, assert that it happened. 'No exception' is not 'changed a row'.

**How it is checked.**

- `ast`: except Exception/bare except whose body is only pass/logger.*/return None|False|[] and whose try contains insert|update|execute|commit|send
- `regex`: `except ImportError:\s*pass`

**Evidence.** e5a5aa7c8 (2026-09-07); c66066596; footguns #16, #23

## PY-007 — A function-local import of a nonexistent module hidden by `except: return []`

*Severity:* **high** · *Stacks:* python, imports

**Symptom.** A route returned `[]` forever. The module it imported does not exist in the repo.

**Root cause.** Linters and top-level AST audits pass because the import is lazy and wrapped.

**Resolution.** A test resolves every first-party import at any depth with `importlib.util.find_spec`, and checks that the walk actually reached files.

**Prevention.** Never wrap imports in catch-alls.

**How it is checked.**

- `test`: every Import/ImportFrom at any depth with a first-party root resolves via find_spec

**Evidence.** cb5a8cc24 (2026-09-03); tests/backend/test_no_phantom_first_party_imports.py

## PY-008 — A bare except around `exec_module` leaves a half-loaded module

*Severity:* **medium** · *Stacks:* python, imports

**Symptom.** A measurement script published 13/12/128 when the truth was 25/25/103.

**Root cause.** The module raised part-way through (a `@dataclass` needed a `sys.modules` registration). Definitions above the failure point existed, and `getattr(m, X, {})` masked the missing ones.

**Resolution.** Let analysis scripts crash on import failure, and register the module in `sys.modules` before `exec_module`.

**Prevention.** Corroborate headline numbers a second way.

**How it is checked.**

- `ast`: spec.loader.exec_module( inside try with a swallowing handler, or without a prior sys.modules[name] = m

**Evidence.** memory: pattern_my_own_measuring_script_swallowed_its_import

## PY-009 — An exception classifier must not treat programming errors as outages

*Severity:* **high** · *Stacks:* python, error-handling, postgresql

**Symptom.** An SQL bug (undefined column) made a route serve the fallback store permanently. Conversely, a settings read fell back only when the result was EMPTY and raised on an outage.

**Root cause.** `except Exception: return None`, where None means 'unavailable, fall back'.

**Resolution.** `classify_pg_exception` by SQLSTATE: programming errors surface as 500s with no fallback, while infrastructure errors fall back. 'Raised' and 'empty' are separate branches, each logged with its exception type.

**Prevention.** A fallback may never enable a protected flag.

**How it is checked.**

- `ast`: except Exception: return None in functions whose None return triggers a fallback in callers

**Evidence.** 8c5962318; 875a886d2 (2026-08-10); 33d67f1e7 (2026-09-06)

## PY-010 — A try scope narrower than its 'must never fail' claim

*Severity:* **medium** · *Stacks:* python, error-handling

**Symptom.** A notification that 'must never fail a registration' guarded 1 line of a 60-line block, so an unreachable DB would 500 a registration that had already committed.

**Root cause.** The try block did not cover all of the best-effort side effects.

**Resolution.** Wrapped the whole best-effort block, with an AST test asserting every side-effect call sits inside the try.

**Prevention.** State the invariant in a test, not a comment.

**How it is checked.**

- `test`: AST: the try body contains every side-effect call of the best-effort block

**Evidence.** 5282da9bb (2026-09-05)

## PY-011 — `__main__` or `close()` indented inside a loop: valid syntax, wrong behaviour

*Severity:* **high** · *Stacks:* python

**Symptom.** A cron script did nothing when run. A connection closed after the first iteration.

**Root cause.** An indentation slip moved `if __name__ == '__main__':` or `client.close()` into a block.

**Resolution.** Fixed the indentation.

**Prevention.** Lint for these shapes.

**How it is checked.**

- `ast`: If testing __name__ == '__main__' not at module top level; .close() on a client inside a For body

**Evidence.** 284afca11 (2026-08-07)

## PY-012 — `python path/to/script.py` puts the script's own directory on `sys.path[0]`

*Severity:* **high** · *Stacks:* python, imports, packaging

**Symptom.** Combined with `except ImportError: pass`, a kill switch silently vanished. Tests passed because the harness had added `.` to the path.

**Root cause.** Direct script execution changes the import root.

**Resolution.** An explicit path bootstrap plus an unguarded module-level import, and a test that runs the script exactly the way cron does. Watch `Path(__file__).parents[N]` off-by-ones: `parents[1]` cut a scan from 282 files to 28 while still reporting OK.

**Prevention.** Run scripts with `python -m`, or bootstrap explicitly.

**How it is checked.**

- `ast`: files under scripts/ or cron/ importing first-party packages with no sys.path bootstrap

**Evidence.** c66066596; bcd86bed0; f1bef0ac5

## PY-013 — Eager heavy imports cost seconds at startup; `ImportError` is not the only failure

*Severity:* **medium** · *Stacks:* python, imports, performance

**Symptom.** `import server` took 8.9 s, 2.86 s of it weasyprint, matplotlib and openpyxl.

**Root cause.** Heavy optional libraries were imported at module level for use in a few functions. Native libraries fail with `OSError`, not `ImportError`.

**Resolution.** Import lazily, use `importlib.util.find_spec` for availability flags, call `matplotlib.use('Agg')` before importing pyplot, and catch `(ImportError, OSError)`.

**Prevention.** Profile with `python -X importtime`.

**How it is checked.**

- `regex`: `^import (weasyprint|matplotlib\.pyplot|openpyxl|pandas)`
- `regex`: `except ImportError:\s*\n\s*HAS_`

**Evidence.** 9f5f63285 (2026-09-04)

## PY-014 — Dependency pins: exact-pin conflicts, editable installs masking pins, undeclared imports

*Severity:* **medium** · *Stacks:* python, packaging, pip

**Symptom.** A clean `pip install` failed on an unresolvable conflict between pydantic and pydantic_core. `pip install -e` of a sibling repo masked a stale pin. A package was present in the venv but absent from requirements.

**Root cause.** The long-lived venv diverged from requirements.txt.

**Resolution.** Test requirements in a throwaway venv.

**Prevention.** In CI: `pip install --dry-run -r requirements.txt` in a fresh venv, `pip check`, and an import-vs-requirements diff (e.g. deptry).

**How it is checked.**

- `ci`: fresh-venv install + pip check + deptry

**Evidence.** adbb863f7 (2026-08-18); 083d3d71a; ea70287fe

## PY-015 — A 'one-line' suggested edit introduces a missing import

*Severity:* **high** · *Stacks:* python, imports

**Symptom.** A NameError on a live approval path: `datetime.now(timezone.utc)` without importing `timezone`.

**Root cause.** Code-review suggestions are applied without a compile or lint step.

**Resolution.** Added the import.

**Prevention.** Run ruff F821 as a blocking check on changed files.

**How it is checked.**

- `lint`: ruff F821 blocking in CI

**Evidence.** tasks/lessons.md

## PY-016 — A context-dependent helper silently misbehaves outside a request

*Severity:* **high** · *Stacks:* python, contextvars

**Symptom.** 'Missing tenant context' was swallowed by a blanket except, so every script took the fallback path.

**Root cause.** A tenant contextvar is set by request middleware only, and scripts and workers never set it.

**Resolution.** Set and restore the context around calls in scripts and workers.

**Prevention.** Make a missing context raise, and never catch that error.

**How it is checked.**

- `ast`: functions under scripts/, workers/ or cron/ calling a tenant-scoped wrapper without set_ctx_* on the path

**Evidence.** 0052603c9 (2026-08-20)

## PY-017 — Float money plus `round()` (banker's rounding) produces different cents

*Severity:* **critical** · *Stacks:* python, decimal

**Symptom.** Two functions named `dollars_to_cents` shipped side by side and returned different money: `'10.005'` was rejected by one and returned 1001 from the other. `8.115*100 == 811.4999…`.

**Root cause.** `int(round(float(v)*100))` combines binary float error with round-half-even.

**Resolution.** One owner module using `Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP)`, integer cents everywhere, and a float ban in the domain package enforced by a regex+AST test.

**Prevention.** Convert once, at the ingestion boundary.

**How it is checked.**

- `regex`: `round\(\s*float\(|round\(.*\*\s*100\)|Decimal\([^'\"]\w*\)`
- `ast`: float( calls or float literals inside the domain package

**Evidence.** f9e4d34c8 (2026-09-02); 9ee666c1b; tests/backend/test_no_floats_in_domain.py

**Sources.** <https://docs.python.org/3/library/functions.html#round> <https://docs.python.org/3/library/decimal.html#decimal.Decimal.quantize>

## PY-018 — Naive datetimes: `utcnow()` is deprecated, and `date.today()` is local time

*Severity:* **high** · *Stacks:* python, time

**Symptom.** A dedupe compared UTC ISO prefixes against local `date.today()`, so on a UTC+10 host it failed open and re-notified everyone. Comparing naive and aware datetimes raised TypeError.

**Root cause.** `datetime.utcnow()` returns a naive datetime, and `date.today()` uses the host's time zone.

**Resolution.** Use `datetime.now(timezone.utc)` and `zoneinfo` for business dates, and pass `today` in explicitly so it can be tested.

**Prevention.** Enable ruff's DTZ rules.

**How it is checked.**

- `regex`: `utcnow\(\)|utcfromtimestamp\(|datetime\.now\(\)|date\.today\(\)`
- `lint`: ruff DTZ001-DTZ012

**Evidence.** b40e914e4 (2026-09-12); 22b8ae335

**Sources.** <https://docs.python.org/3/library/datetime.html#datetime.datetime.utcnow>

## PY-019 — A function returning a numeric string is compared numerically

*Severity:* **medium** · *Stacks:* python

**Symptom.** Wrong year comparisons: a helper returned `'2026'`, a str.

**Root cause.** String-typed return values were compared against ints.

**Resolution.** Cast at the boundary: `int(str(y).split('-')[0])`.

**Prevention.** Annotate return types and run a type checker.

**How it is checked.**

- `review`: comparisons between str-returning helpers and int literals (mypy/pyright)

**Evidence.** footgun #3

## PY-020 — Secrets in exception text end up in logs and databases

*Severity:* **high** · *Stacks:* python, logging, security

**Symptom.** Driver errors echoed the full connection URI, password included, into log files and a stored job document.

**Root cause.** pymongo and asyncpg include the DSN in some exception messages, and child-process stderr was persisted verbatim.

**Resolution.** A `redact_secrets()` helper handles the URI pattern and the values of secret-named env vars.

**Prevention.** Redact before persisting any `str(exc)` or stderr.

**How it is checked.**

- `ast`: str(exc) or stderr written to DB fields or logger.* without a redaction call

**Evidence.** 602f2f62d (2026-08-20)

## PY-021 — A redaction filter on the root logger misses uvicorn's loggers

*Severity:* **high** · *Stacks:* python, logging, uvicorn

**Symptom.** `?email=` query strings were not redacted in the access logs.

**Root cause.** uvicorn's loggers set `propagate=False`, so root-level filters never see their records.

**Resolution.** Attach the filter to every handler in `logging.Logger.manager.loggerDict`, idempotently.

**Prevention.** Test redaction against the real access-log record.

**How it is checked.**

- `regex`: `logging\.getLogger\(\)\.addFilter`

**Evidence.** 2f630e40a (2026-08-30)

## PY-022 — Fire-and-forget `Popen` with no liveness check; a zombie passes `os.kill(pid, 0)`

*Severity:* **medium** · *Stacks:* python, subprocess

**Symptom.** A job sat at 'starting' forever.

**Root cause.** The child died instantly, and the zombie still answered `kill(pid, 0)`. The script path was resolved relative to the wrong `__file__`.

**Resolution.** Read `/proc/<pid>/stat` for the process state, and preflight the script path.

**Prevention.** Keep and poll `Popen` handles.

**How it is checked.**

- `regex`: `os\.kill\(\w+,\s*0\)`
- `ast`: Popen( whose handle is discarded (no wait/poll)

**Evidence.** memory: pattern_silent_popen_and_playwright_browser_drift

## PY-023 — A subprocess argument list still lets a value starting with `-` become an option

*Severity:* **medium** · *Stacks:* python, security

**Symptom.** A user-supplied value could be read as a command-line flag.

**Root cause.** Validation allowed a leading `-`.

**Resolution.** Require a leading alphanumeric character, or insert `--`.

**Prevention.** Review argv built from user input.

**How it is checked.**

- `review`: user-controlled values in subprocess argv validated by a regex that allows a leading -

**Evidence.** e7d78309f (2026-09-11)

## PY-024 — User input interpolated into a regex

*Severity:* **high** · *Stacks:* python, mongodb, security

**Symptom.** `.` matched another user's address, and `+` made plus-addressed mail fail to match itself, so an email suppression list leaked.

**Root cause.** `{'$regex': f'...{var}...'}` without escaping.

**Resolution.** Use `re.escape`.

**Prevention.** Also applies to `re.compile` with user input.

**How it is checked.**

- `regex`: `\$regex['\"]?\s*:\s*f['\"]|\$regex['\"]?\s*:\s*\w+\b(?!.*re\.escape)`

**Evidence.** c66066596 (2026-08-21)
