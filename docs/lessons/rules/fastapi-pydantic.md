# FastAPI & Pydantic: lessons learnt

**Scope.** Routing, dependencies, response models, validation, error handling.

| id | severity | lesson |
|---|---|---|
| FA-001 | high | `extra='ignore'` and `response_model` silently drop undeclared fields |
| FA-002 | high | Every hand-written allow-list between the query and the model is another silent filter |
| FA-003 | high | A response model stricter than the data returns 500 or silently empties a list |
| FA-004 | medium | A response model that inherits the request model's input bounds |
| FA-005 | medium | Two Pydantic classes sharing a name rename a shipped OpenAPI schema |
| FA-006 | high | `Optional[X]` no longer implies a default in Pydantic v2; other v2 behaviour changes |
| FA-007 | critical | A helper inserted between a route decorator and its function hijacks the route |
| FA-008 | critical | Wrapping router imports in `try/except ImportError` turns a broken module into 404s |
| FA-009 | high | Unregistered duplicate routers: tests pass against code that never runs |
| FA-010 | high | `except Exception` in a handler turns deliberate `HTTPException`s into 500s |
| FA-011 | medium | Calling a route function directly bypasses dependency injection |
| FA-012 | high | `app.routes` is lazily wrapped in newer FastAPI, so negative route assertions pass vacuously |
| FA-013 | high | A permission check reads a claim that no dependency populates |
| FA-014 | medium | `List[str]` receiving a null element, where an empty list means 'all' |
| FA-015 | critical | A blocking call inside `async def` stalls every request on the worker |
| FA-016 | medium | `Depends` results are cached per request; `yield`-dependency teardown timing changed |
| FA-017 | medium | BackgroundTasks are not a job queue; `on_event` and `lifespan` don't mix |
| FA-018 | high | CORS with credentials cannot use a wildcard, and echoing any Origin is worse |
| FA-019 | medium | uvicorn behind a proxy: the client IP comes from the proxy, and state is per-process |
| FA-020 | high | Letting the caller set a flag that hides rows is an authorisation decision |

## FA-001 — `extra='ignore'` and `response_model` silently drop undeclared fields

*Severity:* **high** · *Stacks:* pydantic, fastapi

**Symptom.** A field was always empty in the API although the handler built it: a jointly owned record showed one owner, and an edit form rendered blank first- and last-name inputs.

**Root cause.** FastAPI serialises every return value through `response_model` (or the return annotation). Any key the model does not declare is discarded with no warning, and on request models a mistyped client field is dropped the same way.

**Resolution.** Declared the missing fields. A derived test asserts `SELECT aliases ∩ Model.model_fields ⊆ handler dict keys`, so the next column added to the query fails the test instead of rendering blank.

**Prevention.** When a field is always empty, read the response model before debugging the handler or the component. Use `extra='forbid'` on request models, where a 422 is better than silent loss.

**How it is checked.**

- `test`: set(handler_payload) - set(ResponseModel.model_fields) == set()
- `ast`: request models (…Create/…Update/…Request) without model_config extra='forbid'

**Evidence.** 71c59b20f (2026-09-06); tests/backend/test_users_pg_projection_completeness.py

**Sources.** <https://fastapi.tiangolo.com/tutorial/response-model/> <https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.extra>

## FA-002 — Every hand-written allow-list between the query and the model is another silent filter

*Severity:* **high** · *Stacks:* python, fastapi

**Symptom.** An edit returned 200 and was stored nowhere. A SELECTed column never reached the response.

**Root cause.** A field passes through several dict-building stages (`SELECT … AS alias` → an intermediate dict → a normaliser → the response model), and each stage that enumerates keys drops anything it does not list.

**Resolution.** Derived the expected key set from the SQL aliases and the model fields instead of enumerating it a second time.

**Prevention.** When adding a column, grep every dict and allow-list between the query and the response.

**How it is checked.**

- `ast`: module-level *_ALLOWED*/*_KEYS* constants used in {k: v … if k in X}, cross-checked against model fields

**Evidence.** CLAUDE.md §Silent Failures (GET /users, 2026-09-06)

## FA-003 — A response model stricter than the data returns 500 or silently empties a list

*Severity:* **high** · *Stacks:* pydantic, fastapi, mongodb

**Symptom.** A list endpoint returned `ResponseValidationError` 500s. Another returned 0 of 14 rows for two tenants and 87 of 87 for the main one.

**Root cause.** Models declared `created_at: str` while some writers stored BSON datetimes, and a required `id` was missing on some rows. Pydantic v2 does not coerce datetime to str, and FastAPI reports an outbound validation failure as a 500, not a 422. A per-row `try/except ValidationError: continue` then emptied a whole tenant.

**Resolution.** Added a reusable `IsoTimestamp` annotated type that coerces on read and still rejects non-temporal values. Bad rows are logged and counted, not silently skipped.

**Prevention.** Validate a sample of live documents per tenant against every response model, and test with the smallest tenant, not only the largest.

**How it is checked.**

- `ast`: response_model fields typed str whose name matches *_at|*date*
- `ast`: Model(**doc) in a loop inside except ValidationError: continue

**Evidence.** b988d095f (2026-08-26); e09bf4861; 1631e4c5d; 0701dbcf4 (2026-09-04)

## FA-004 — A response model that inherits the request model's input bounds

*Severity:* **medium** · *Stacks:* pydantic, fastapi

**Symptom.** One over-long legacy row caused a 500 on the whole list endpoint.

**Root cause.** `class XResponse(XCreate)` inherited `max_length`/`le`/`pattern`, and FastAPI validates outbound data too.

**Resolution.** Re-declared the bounded fields unconstrained on the response model, with parity tests.

**Prevention.** Input validation belongs to input models only.

**How it is checked.**

- `ast`: a response_model class subclassing a class whose fields carry Field(max_length|le|ge|pattern)

**Evidence.** f7efbbd23 (2026-08-26)

## FA-005 — Two Pydantic classes sharing a name rename a shipped OpenAPI schema

*Severity:* **medium** · *Stacks:* fastapi, pydantic, openapi

**Symptom.** `#/components/schemas/BuildingAssetResponse` became `models__building__BuildingAssetResponse` on four operations that were already shipped. Generated clients broke while the spec stayed valid.

**Root cause.** FastAPI module-qualifies colliding model names. The repo held 21 such pairs.

**Resolution.** Renamed the new model.

**Prevention.** Gate on a committed-spec diff that renames or removes schema keys.

**How it is checked.**

- `shell`: `grep -o '"[a-z_]*__[A-Za-z]*"' openapi.json   # module-qualified schema names`
- `ast`: group `class X(BaseModel)` by name across modules; flag duplicates reachable from app.openapi()

**Evidence.** fd045a533 (2026-09-08)

## FA-006 — `Optional[X]` no longer implies a default in Pydantic v2; other v2 behaviour changes

*Severity:* **high** · *Stacks:* pydantic

**Symptom.** Migrated models started rejecting payloads that omitted a field, and response models skipped rows.

**Root cause.** In v2, `x: Optional[int]` is required but nullable; only `= None` makes it optional. Other v2 changes: `.dict()`/`.parse_obj`/`Config`/`@validator` were renamed, `model_dump()` keeps Decimals and datetimes as Python objects (use `mode='json'`), assignment is not validated without `validate_assignment`, and coercion is lax by default (`'123'`→int, floats into Decimal).

**Resolution.** Write `x: int | None = None` explicitly. Use `strict=True` and `Decimal` or `AwareDatetime` on money and timestamps.

**Prevention.** Run with `-W error::pydantic.warnings.PydanticDeprecatedSince20`.

**How it is checked.**

- `regex`: `:\s*(Optional\[[^\]]+\]|[\w\[\]]+\s*\|\s*None)\s*$  (in models, no default)`
- `regex`: `\.dict\(\)|\.parse_obj\(|@validator\(|@root_validator|class Config:`
- `regex`: `:\s*float\b on amount|price|cents|balance fields`

**Sources.** <https://docs.pydantic.dev/latest/migration/> <https://docs.pydantic.dev/latest/concepts/conversion_table/>

## FA-007 — A helper inserted between a route decorator and its function hijacks the route

*Severity:* **critical** · *Stacks:* fastapi, python-decorators

**Symptom.** `/auth/register` was served by a private address helper, and later `/intelligence/levy-model` by another helper whose positional arguments became query parameters. Nothing raised.

**Root cause.** A decorator applies to the next `def`. With stacked decorators the gap is invisible, and tooling that inserted code at `rindex('@router.get(')` landed between two stacked decorators.

**Resolution.** Moved the helpers above the decorator stack. The `test_no_private_endpoint_registered_as_route` gate was originally scoped to a literal list of 4 of 145 router files and is now derived from the tree.

**Prevention.** Never place code between a decorator and its function. Registered endpoints must have public names.

**How it is checked.**

- `ast`: FunctionDef whose name starts with _ and carries @<router>.(get|post|put|patch|delete|api_route)
- `test`: iterate app.openapi() operations; flag endpoint functions whose __name__ starts with _

**Evidence.** 24590c614 (2026-09-06); 270f61ffa (2026-09-05); tests/backend/test_router_registration_hygiene.py

## FA-008 — Wrapping router imports in `try/except ImportError` turns a broken module into 404s

*Severity:* **critical** · *Stacks:* fastapi, imports

**Symptom.** Every route in a module silently 404'd. One router whose import-guard handler used a `logger` defined 15 lines later would have taken out every auth route.

**Root cause.** `server.py` guarded each `from routers.x import router` with `except ImportError: logger.warning(...)`, so a syntax error or NameError merely skips registration.

**Resolution.** Defined `logger` before the guards, ran pyflakes, and added wiring tests that assert paths exist in `app.openapi()['paths']`.

**Prevention.** Optional routers should fail loudly in CI. Check the logs first whenever an endpoint 404s after an edit.

**How it is checked.**

- `ast`: Try whose body imports routers.* and whose handler does not re-raise
- `lint`: pyflakes/ruff F821 at module scope

**Evidence.** c71feadb8 (2026-08-20); rules/post-compact-critical.md footgun #1

## FA-009 — Unregistered duplicate routers: tests pass against code that never runs

*Severity:* **high** · *Stacks:* fastapi, testing

**Symptom.** A settings card called four routes that returned 404 for months while their unit tests passed. A security test exercised a BOLA guard that existed only in a dead duplicate, while the live inline handler had none.

**Root cause.** Router files that are never imported still define handlers, and tests import those handlers by module path.

**Resolution.** Assert wiring via `app.openapi()`, port guards to the live handler, and point tests at the live handler. Registering a dead router exposed 4 tests that had only ever asserted the dead copy's behaviour.

**Prevention.** Tests reach handlers through the registered app. A duplicate-drift audit compares guard shapes between same-named handlers.

**How it is checked.**

- `shell`: `diff routers/*.py against `from routers.X import` in the app entrypoint`
- `ast`: same-named async def handlers in two modules where only one is registered; tests importing from unwired modules

**Evidence.** CLAUDE.md §The duplicate that RUNS is not the duplicate that is TESTED; tests/backend/test_unwired_router_drift.py

## FA-010 — `except Exception` in a handler turns deliberate `HTTPException`s into 500s

*Severity:* **high** · *Stacks:* fastapi, error-handling

**Symptom.** An invalid page key returned 500 'store failed' and logged 'unexpected failure' instead of a 422.

**Root cause.** Validators that raise `HTTPException(401/422)` ran inside the try, and the error mapper converted every non-domain exception to 500.

**Resolution.** The mapper's first branch is `if isinstance(exc, HTTPException): return exc`.

**Prevention.** Always put `except HTTPException: raise` before `except Exception`.

**How it is checked.**

- `ast`: route function with try/except Exception raising HTTPException(500) and no preceding except HTTPException

**Evidence.** 24e140378 (2026-09-03)

**Sources.** <https://fastapi.tiangolo.com/tutorial/handling-errors/>

## FA-011 — Calling a route function directly bypasses dependency injection

*Severity:* **medium** · *Stacks:* fastapi, pytest

**Symptom.** Tests behaved oddly: parameters arrived as `Header(None)` objects instead of values.

**Root cause.** Invoked as a plain coroutine, `Depends()`/`Header()`/`Query()` defaults are FieldInfo objects. Rate-limited routes also need a real Starlette `Request`.

**Resolution.** Pass explicit keyword arguments, or use `TestClient`/`httpx.AsyncClient`.

**Prevention.** Prefer HTTP-level tests for route behaviour.

**How it is checked.**

- `ast`: direct await <router_fn>(...) in tests omitting kwargs whose defaults are Header(|Query(|Depends(

**Evidence.** 9dd281371 (2026-08-07)

## FA-012 — `app.routes` is lazily wrapped in newer FastAPI, so negative route assertions pass vacuously

*Severity:* **high** · *Stacks:* fastapi, pytest

**Symptom.** `assert path not in {r.path for r in app.routes}` passed trivially: `app.routes` had length 1 while OpenAPI listed 1,176 paths.

**Root cause.** FastAPI 0.141 keeps an `_IncludedRouter` wrapper in `app.routes` instead of flattening the included routes.

**Resolution.** Assert on `app.openapi()['paths']`, or flatten recursively.

**Prevention.** Every negative or count assertion needs a non-empty precondition.

**How it is checked.**

- `regex`: `for r in app\.routes`
- `review`: negative membership assertions over a collection never asserted non-empty

**Evidence.** 4655b0ae6 (2026-09-03); 379cec1d9

## FA-013 — A permission check reads a claim that no dependency populates

*Severity:* **high** · *Stacks:* fastapi, authorization

**Symptom.** The guard never fired in production. Its unit test passed because the test set the claim directly.

**Root cause.** `current_user['governance_offices']` was only hydrated by a dependency that this route did not use.

**Resolution.** Introduced a single resolver function that every route calls.

**Prevention.** Before gating on a claim, confirm which request path populates it.

**How it is checked.**

- `ast`: current_user['k'] / .get('k') in a route with no Depends chain that assigns k

**Evidence.** 969c600; CLAUDE.md §A permission that reads a claim nobody populated

## FA-014 — `List[str]` receiving a null element, where an empty list means 'all'

*Severity:* **medium** · *Stacks:* pydantic, api-design

**Symptom.** An approval looked like a silent no-op, and the client got 'Input should be a valid string'.

**Root cause.** The UI sent `[null]` for unlinked records. Filtering the nulls out would have turned 'remove one' into 'remove all', because the endpoint treated an empty list as 'all'.

**Resolution.** `List[Optional[str]]` with a validator that raises a named error.

**Prevention.** Never let an empty collection mean 'everything' on a destructive endpoint.

**How it is checked.**

- `ast`: handlers where `if not ids:` selects all rows before a destructive write

**Evidence.** 6982aaedc (2026-08-30)

## FA-015 — A blocking call inside `async def` stalls every request on the worker

*Severity:* **critical** · *Stacks:* fastapi, asyncio, uvicorn

**Symptom.** One slow SMTP relay or scraper froze every request on a worker for 180–300 s. An 8.2 s CPU-bound build ran inside a handler.

**Root cause.** `async def` endpoints run on the event loop, so `smtplib`, `subprocess.run`, `requests`, `time.sleep`, sync DB drivers, bcrypt and heavy CPU work block it. (`def` endpoints run in the threadpool.)

**Resolution.** `asyncio.to_thread(...)` / `run_in_threadpool`, plus a per-process cache for CPU-bound results (deep-copied on read). The gate test has positive and negative controls so it does not flag its own fix.

**Prevention.** Enable ruff's ASYNC rules. Mind threadpool exhaustion (40 AnyIO tokens by default).

**How it is checked.**

- `ast`: inside AsyncFunctionDef: smtplib.*, subprocess.run/call/check_output, requests.*, time.sleep, urllib.request, bcrypt.hashpw not wrapped in to_thread/run_in_executor
- `lint`: ruff ASYNC210/ASYNC230/ASYNC251

**Evidence.** 9eb844ac4 (2026-09-04); 602f2f62d; e7d78309f; 6c1e24dd3 (2026-09-13); tests/backend/test_no_blocking_io_in_async_handlers.py

**Sources.** <https://fastapi.tiangolo.com/async/#path-operation-functions> <https://docs.astral.sh/ruff/rules/#flake8-async-async>

## FA-016 — `Depends` results are cached per request; `yield`-dependency teardown timing changed

*Severity:* **medium** · *Stacks:* fastapi

**Symptom.** A dependency that mutates state ran once when it was expected twice. A DB session was already closed when a background task used it.

**Root cause.** `use_cache=True` is the default. Since 0.106 the exit code of a `yield` dependency runs before the response is sent.

**Resolution.** `Depends(fn, use_cache=False)` where re-evaluation is needed. Background tasks open their own session.

**Prevention.** Never pass request-scoped sessions to background work.

**How it is checked.**

- `regex`: `background_tasks\.add_task\([^)]*(session|db)`

**Sources.** <https://fastapi.tiangolo.com/tutorial/dependencies/sub-dependencies/> <https://fastapi.tiangolo.com/release-notes/>

## FA-017 — BackgroundTasks are not a job queue; `on_event` and `lifespan` don't mix

*Severity:* **medium** · *Stacks:* fastapi, starlette

**Symptom.** Work queued with `add_task` was lost on a restart. Startup hooks silently did not run.

**Root cause.** Background tasks run in-process after the response, with no persistence or retry. Once `lifespan=` is passed, `@app.on_event` handlers are not called.

**Resolution.** Use a durable queue (ARQ/Temporal/transactional outbox) for must-happen work, and migrate every startup hook to one lifespan.

**Prevention.** Grep for add_task on email, payment or ledger work.

**How it is checked.**

- `regex`: `on_event\(\"(startup|shutdown)\"\)`
- `regex`: `background_tasks\.add_task\(.*(email|payment|ledger|send)`

**Sources.** <https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat> <https://fastapi.tiangolo.com/advanced/events/>

## FA-018 — CORS with credentials cannot use a wildcard, and echoing any Origin is worse

*Severity:* **high** · *Stacks:* fastapi, security

**Symptom.** Browsers rejected credentialed requests, and the 'fix' (a `.*` origin regex) enabled credentialed cross-origin reads.

**Root cause.** The spec forbids `*` together with credentials. Echoing the request's Origin grants every site access.

**Resolution.** An explicit origin list, with any regex anchored.

**Prevention.** Review CORS configuration on every deploy target.

**How it is checked.**

- `regex`: `allow_origins=\[\"\*\"\]|allow_origin_regex=['\"]\.\*`

**Sources.** <https://fastapi.tiangolo.com/tutorial/cors/> <https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS#credentialed_requests_and_wildcards>

## FA-019 — uvicorn behind a proxy: the client IP comes from the proxy, and state is per-process

*Severity:* **medium** · *Stacks:* uvicorn, security

**Symptom.** Rate limits and audit logs saw the proxy's IP, or trusted a spoofed `X-Forwarded-For`.

**Root cause.** Without `--proxy-headers` and a narrow `--forwarded-allow-ips`, the client host is the proxy's address, and `*` trusts anyone. `--workers N` gives each process its own in-memory limiter.

**Resolution.** Trust only the reverse proxy's IP (`TRUSTED_PROXY_CIDRS`), and keep shared state in Redis.

**Prevention.** Check the systemd `ExecStart` flags.

**How it is checked.**

- `regex`: `--forwarded-allow-ips[= ]['\"]?\*`

**Sources.** <https://www.uvicorn.org/deployment/#proxies-and-forwarded-headers>

## FA-020 — Letting the caller set a flag that hides rows is an authorisation decision

*Severity:* **high** · *Stacks:* fastapi, security

**Symptom.** Any unauthenticated caller could erase their own login attempts from the security dashboard and the risk baseline.

**Root cause.** The login audit took `is_test_data` from an `X-Test-Data` request header, and every read of the audit log excluded test rows.

**Resolution.** The flag is derived server-side (running under pytest, or the header outside production only).

**Prevention.** Audit request models that expose internal flags (mass assignment).

**How it is checked.**

- `regex`: `request\.headers\.get\(['\"]X-Test`
- `regex`: `\$set\":\s*(body|payload|data)\b|\*\*(body|payload)\.(dict|model_dump)\(\)`

**Evidence.** CLAUDE.md §Never let the caller set the flag

**Sources.** <https://cheatsheetseries.owasp.org/cheatsheets/Mass_Assignment_Cheat_Sheet.html>
