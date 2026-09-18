# SQLAlchemy & asyncpg: lessons learnt

**Scope.** Bind params, transactions, pools, async ORM, poolers.

| id | severity | lesson |
|---|---|---|
| SA-001 | medium | `text()` truncates a bind parameter immediately followed by `::cast` |
| SA-002 | medium | asyncpg cannot infer the type of `(:p IS NULL OR col = :p)` |
| SA-003 | medium | asyncpg encodes a DATE parameter with `date.toordinal()` |
| SA-004 | high | A try/except around a statement does not un-abort the PostgreSQL transaction |
| SA-005 | high | Connection pool exhaustion |
| SA-006 | high | An unguarded database read inside a shared dependency takes down every route |
| SA-007 | high | Async ORM pitfalls: lazy loading raises `MissingGreenlet`; `expire_on_commit` expires everything |
| SA-008 | high | asyncpg prepared-statement caches conflict with PgBouncer and live DDL |

## SA-001 — `text()` truncates a bind parameter immediately followed by `::cast`

*Severity:* **medium** · *Stacks:* sqlalchemy

**Symptom.** `syntax error at or near ':'`: `:meta::jsonb` bound a parameter named `met`.

**Root cause.** SQLAlchemy's bind-parameter regex stops before `::`.

**Resolution.** Use `CAST(:x AS type)`.

**Prevention.** Regex lint.

**How it is checked.**

- `regex`: `:[A-Za-z_]\w*::`

**Evidence.** memory: pattern_sqlalchemy_text_cast_truncates_bindparam

## SA-002 — asyncpg cannot infer the type of `(:p IS NULL OR col = :p)`

*Severity:* **medium** · *Stacks:* asyncpg

**Symptom.** `AmbiguousParameterError` at PREPARE on every call, which mocked tests never reached.

**Root cause.** asyncpg prepares statements server-side and PostgreSQL needs a type for every parameter.

**Resolution.** `CAST(:p AS text)` (text rather than citext when an exact comparison is intended).

**Prevention.** Regex lint.

**How it is checked.**

- `regex`: `:\w+\s+IS\s+(NOT\s+)?NULL(?!.*CAST)`

**Evidence.** 0a83a0db4 (2026-08-10); 9b48824ea

## SA-003 — asyncpg encodes a DATE parameter with `date.toordinal()`

*Severity:* **medium** · *Stacks:* asyncpg, time

**Symptom.** `'str' object has no attribute 'toordinal'`. `CAST(:d AS DATE)` did not help.

**Root cause.** asyncpg performs no implicit string-to-date, uuid or numeric conversion on parameters. `numeric` comes back as Decimal, and json/jsonb as str unless a codec is registered.

**Resolution.** Pass `datetime.date` values, and register JSON codecs.

**Prevention.** Grep for `.isoformat()` values fed into query parameters.

**How it is checked.**

- `regex`: `isoformat\(\)  (in values passed as query params)`

**Evidence.** 5b3651839 (2026-08-27); footgun #21

**Sources.** <https://magicstack.github.io/asyncpg/current/usage.html#type-conversion>

## SA-004 — A try/except around a statement does not un-abort the PostgreSQL transaction

*Severity:* **high** · *Stacks:* postgresql, sqlalchemy

**Symptom.** An outbox insert that 'must not abort' the identity write poisoned the transaction: the commit failed and the user UPDATE was discarded, while the other store's write stood.

**Root cause.** After any error, PostgreSQL rejects every statement until ROLLBACK. Catching the error in Python does not reset that.

**Resolution.** `async with session.begin_nested():` (a SAVEPOINT) around best-effort statements, and a savepoint per DELETE in sweeps.

**Prevention.** Lint for a try around `session.execute` followed by further use of the session.

**How it is checked.**

- `ast`: try: session.execute(...) except: (no rollback/begin_nested) followed by more session use

**Evidence.** 1a065726e (2026-09-01); 6bd711bf9; d991e88b0

## SA-005 — Connection pool exhaustion

*Severity:* **high** · *Stacks:* sqlalchemy, postgresql, performance

**Symptom.** 711 `pg_unavailable` errors in 15 minutes: 4 workers × 30 connections against about 89 available.

**Root cause.** Total connections = (pool_size + max_overflow) × workers × processes. An unbounded gather over N items each opened a session.

**Resolution.** A configurable pool with a budget check that leaves 25% headroom below `max_connections`. Fan-out is bounded by a semaphore, or replaced by a set-based query.

**Prevention.** An `AsyncSession` is never shared between concurrent tasks.

**How it is checked.**

- `regex`: `gather\(\*\[.*session`
- `review`: pool_size × workers vs max_connections

**Evidence.** 306682c57; 2ebcabe4d

**Sources.** <https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks>

## SA-006 — An unguarded database read inside a shared dependency takes down every route

*Severity:* **high** · *Stacks:* fastapi, postgresql, availability

**Symptom.** A database outage 500'd 283 routes, including routes for tenants served entirely by the other store.

**Root cause.** A feature-flag read inside `require_feature()` had no exception boundary.

**Resolution.** Distinguish 'raised' from 'empty', log the exception type, and fall back. A fallback may never ENABLE a protected flag.

**Prevention.** Run the suite with the database unreachable and CLASSIFY the failures.

**How it is checked.**

- `review`: global dependencies calling the DB without an exception boundary

**Evidence.** memory: pattern_an_unguarded_pg_read_behind_283_routes (2026-09-06); 33d67f1e7

## SA-007 — Async ORM pitfalls: lazy loading raises `MissingGreenlet`; `expire_on_commit` expires everything

*Severity:* **high** · *Stacks:* sqlalchemy

**Symptom.** Reading `obj.id` after `await session.commit()` raised `MissingGreenlet`.

**Root cause.** Implicit I/O is impossible under asyncio, and `expire_on_commit=True` forces a refresh on the next attribute access.

**Resolution.** `async_sessionmaker(..., expire_on_commit=False)`, eager loading, and `lazy='raise'` on relationships.

**Prevention.** Lint sessionmaker and relationship configuration.

**How it is checked.**

- `regex`: `async_sessionmaker\((?![^)]*expire_on_commit=False)`
- `regex`: `relationship\((?![^)]*lazy=)`

**Sources.** <https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#preventing-implicit-io-when-using-asyncsession>

## SA-008 — asyncpg prepared-statement caches conflict with PgBouncer and live DDL

*Severity:* **high** · *Stacks:* asyncpg, pgbouncer

**Symptom.** `prepared statement "__asyncpg_stmt_x" already exists`, or 'cached statement plan is invalid' after a migration.

**Root cause.** Statements are cached per connection. PgBouncer transaction mode moves statements between server connections, and DDL invalidates cached plans.

**Resolution.** For PgBouncer: `statement_cache_size=0`, a unique `prepared_statement_name_func` and `NullPool` (or PgBouncer ≥1.21 `max_prepared_statements`). Restart or retry after migrations.

**Prevention.** Check the engine `connect_args` whenever DATABASE_URL targets a pooler.

**How it is checked.**

- `review`: create_async_engine against a pooler port without statement-cache settings

**Sources.** <https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#prepared-statement-cache>
