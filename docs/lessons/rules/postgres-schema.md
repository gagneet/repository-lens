# PostgreSQL: Schema, Types & Privileges: lessons learnt

**Scope.** Constraints, NULLs, types, ownership, timeouts.

| id | severity | lesson |
|---|---|---|
| PG-011 | medium | `information_schema.data_type` reports citext, domains and enums as `USER-DEFINED` |
| PG-012 | high | A cast to an enum type that does not exist, inside a broad except |
| PG-013 | critical | A NULL in a composite UNIQUE key collides with nothing |
| PG-014 | medium | A text discriminator column with no CHECK constraint splits silently |
| PG-015 | low | The ORM naming convention rewrites explicit constraint names |
| PG-016 | high | A backfill with no change to the writers is not an invariant |
| PG-017 | medium | `ON CONFLICT … DO UPDATE … RETURNING id` returns the existing row's OLD id |
| PG-018 | medium | Schema gates run against a freshly migrated database prove only that the migrations agree with themselves |
| PG-019 | critical | A permission-losing default on a dark code path |
| PG-020 | high | Money and time types: `timestamp` versus `timestamptz`, `numeric` versus float/`money` |
| PG-021 | medium | Bitemporal 'current' means both open intervals |
| PG-022 | low | Sequences are not transactional; identity versus serial |
| PG-023 | high | `SECURITY DEFINER` functions without a pinned `search_path` |
| PG-024 | medium | No `statement_timeout`, `lock_timeout` or `idle_in_transaction_session_timeout` |
| PG-025 | high | A GRANT is not ownership, and the gap fails one step at a time |

## PG-011 — `information_schema.data_type` reports citext, domains and enums as `USER-DEFINED`

*Severity:* **medium** · *Stacks:* postgresql

**Symptom.** A PII sweep filtering on `data_type IN ('text','character varying')` skipped `users.email` and reported 'clean'.

**Root cause.** Extension and domain types report as `USER-DEFINED`; the actual type name is in `udt_name`.

**Resolution.** Match `udt_name` too, or use `format_type(atttypid, atttypmod)` from pg_attribute.

**Prevention.** Never conclude 'nothing remains' from a type-filtered sweep.

**How it is checked.**

- `regex`: `data_type\s+IN\s*\(\s*'text'`

**Evidence.** footgun #13

## PG-012 — A cast to an enum type that does not exist, inside a broad except

*Severity:* **high** · *Stacks:* postgresql, enums

**Symptom.** `CAST(:s AS core.user_status)` pointed at a type that does not exist (the real one was `core.record_status`). The UndefinedObject error was caught as 'non-fatal', so every status change for months was written to one store only.

**Root cause.** Catalog-level programming errors were swallowed as if they were transient.

**Resolution.** Cast to the correct type, and let `UndefinedObject`, `UndefinedColumn` and `UndefinedTable` propagate.

**Prevention.** Validate `::schema.type` and `CAST(... AS schema.type)` in SQL strings against `pg_type` in CI.

**How it is checked.**

- `regex`: `(::|AS\s+)(\w+)\.(\w+)  -- extract and verify against pg_type`

**Evidence.** footgun #23

## PG-013 — A NULL in a composite UNIQUE key collides with nothing

*Severity:* **critical** · *Stacks:* postgresql, constraints

**Symptom.** Every per-record credit was doubled, exactly 2.000×. Rows with a NULL `fund_id` coexisted with rows carrying a value for the same key.

**Root cause.** NULLs are distinct in UNIQUE constraints by default, so adding a nullable dimension to a key makes existing rows invisible to the new rows' uniqueness check. A partial index `WHERE fund_id IS NULL` deduplicated only within its own cohort.

**Resolution.** Fixed the rows and the key. Use `UNIQUE NULLS NOT DISTINCT` (PG15+) or a NOT NULL column with a sentinel value.

**Prevention.** Every nullable column inside a unique key needs a written justification. `ON CONFLICT` targets must match the index exactly.

**How it is checked.**

- `sql`: `SELECT conrelid::regclass, conname FROM pg_constraint c WHERE contype='u' AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attnum = ANY(c.conkey) AND NOT a.attnotnull);`

**Evidence.** memory: pattern_a_null_in_a_uniqueness_key_collides_with_nothing (2026-09-04)

**Sources.** <https://www.postgresql.org/docs/16/ddl-constraints.html#DDL-CONSTRAINTS-UNIQUE-CONSTRAINTS>

## PG-014 — A text discriminator column with no CHECK constraint splits silently

*Severity:* **medium** · *Stacks:* postgresql, constraints

**Symptom.** `party_type` held 'individual' ×351, 'person' ×32 and 'organisation' ×31, so `WHERE party_type = 'individual'` returned a plausible, wrong count.

**Root cause.** A backfill migration wrote a new spelling into a free-text column. Nothing fails when a discriminator drifts.

**Resolution.** Normalised the values and added `CHECK (party_type IN (...))`, after auditing every writer, because the constraint would otherwise block a writer nobody had checked.

**Prevention.** Use an enum, lookup table or CHECK from day one for any discriminator.

**How it is checked.**

- `sql`: `SELECT col, count(*) FROM t GROUP BY 1  -- for low-cardinality text columns with no CHECK/FK; flag near-synonyms`

**Evidence.** c231bedab; 0120 migration

## PG-015 — The ORM naming convention rewrites explicit constraint names

*Severity:* **low** · *Stacks:* sqlalchemy, alembic

**Symptom.** `parties_party_type_check` became `ck_parties_parties_party_type_check`, so a later `DROP CONSTRAINT` by the literal name failed.

**Root cause.** The `MetaData(naming_convention=...)` template wraps the name you pass.

**Resolution.** Pass the SHORT name and let the convention build it. Verify against `pg_constraint.conname` after upgrade.

**Prevention.** Read the emitted DDL, not the Python.

**How it is checked.**

- `sql`: `SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_%\_%\_%check%';`

**Evidence.** e787a9c94

## PG-016 — A backfill with no change to the writers is not an invariant

*Severity:* **high** · *Stacks:* postgresql, migrations

**Symptom.** A migration linked 32 users to their parties, and the very next INSERT reopened the gap. Four other writers (seeds, a startup hook, genesis) still created unlinked rows, and `ON CONFLICT DO UPDATE` never touched the column, so the gap reappeared only on a FRESH deploy.

**Root cause.** The data was fixed while the code that creates it was not.

**Resolution.** Fixed every writer in the same PR (deriving ids with uuid5 so re-runs are idempotent), and added a test that measures the writers, not only the rows.

**Prevention.** Every data backfill is paired with a writer change and a row-invariant test.

**How it is checked.**

- `review`: a migration with UPDATE … SET col and no code diff touching INSERTs into that table
- `test`: count(*) WHERE col IS NULL = 0 after running every writer path

**Evidence.** b9a629afb; 9d85dd826 (2026-09-02)

## PG-017 — `ON CONFLICT … DO UPDATE … RETURNING id` returns the existing row's OLD id

*Severity:* **medium** · *Stacks:* postgresql

**Symptom.** A fix to how ids are derived 'did nothing': existing rows kept their old ids.

**Root cause.** An upsert keeps the surviving row's primary key.

**Resolution.** After changing a derivation, compare the new value against the live rows. Delete row by row, each inside its own savepoint.

**Prevention.** Verify a fix against live data, not only against its derivation.

**How it is checked.**

- `review`: id-derivation changes without a live-row comparison

**Evidence.** 91611f343 (2026-09-07)

## PG-018 — Schema gates run against a freshly migrated database prove only that the migrations agree with themselves

*Severity:* **medium** · *Stacks:* postgresql, testing

**Symptom.** RLS-coverage tests stayed green while drift existed in production: a policy dropped by hand, a table created outside migrations.

**Root cause.** CI migrates a throwaway container from the committed files, so it can never see the deployed catalog.

**Resolution.** The read-only gates also run against the DEPLOYED schema through a read-only DSN. They found a defect in the author's own migration within a minute.

**Prevention.** Every catalog assertion runs twice: against a fresh migrate AND against the deployed database.

**How it is checked.**

- `ci`: run catalog tests with DATABASE_URL pointing at a read-only replica of production

**Evidence.** aa5e321c3 (2026-09-06)

## PG-019 — A permission-losing default on a dark code path

*Severity:* **critical** · *Stacks:* postgresql, authorization

**Symptom.** `allowed_roles=None` meant 'no filter'. The moment the domain was promoted, the new store's read would have returned every document to anonymous callers.

**Root cause.** An optional permission parameter defaulted to 'no restriction', on a path no test exercised because it was not yet live.

**Resolution.** Visibility became a REQUIRED keyword argument, a restrictive-by-default `is_public BOOLEAN NOT NULL DEFAULT FALSE` column was added, and the function raises `VisibilityNotExpressible` instead of approximating.

**Prevention.** Authorisation parameters never default to None. A restricted caller with no expressible branch sees nothing, not everything.

**How it is checked.**

- `ast`: repo/query functions whose role/visibility/permission parameter defaults to None

**Evidence.** 2dd288921 (0105, 2026-08-29)

## PG-020 — Money and time types: `timestamp` versus `timestamptz`, `numeric` versus float/`money`

*Severity:* **high** · *Stacks:* postgresql, types

**Symptom.** Dates shifted around DST changes, and amounts drifted.

**Root cause.** `timestamp without time zone` stores wall-clock time with no zone. Floats are inexact, and the `money` type depends on the locale.

**Resolution.** Use `timestamptz` for instants, `date` for business dates, and integer cents (`bigint`) or `numeric(p,s)` for money.

**Prevention.** Audit the catalog for these types.

**How it is checked.**

- `sql`: `SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns WHERE data_type IN ('timestamp without time zone','real','double precision','money');`

**Sources.** <https://wiki.postgresql.org/wiki/Don%27t_Do_This>

## PG-021 — Bitemporal 'current' means both open intervals

*Severity:* **medium** · *Stacks:* postgresql, temporal

**Symptom.** Superseded (retracted) records were reported as live.

**Root cause.** A retracted row still has `valid_to IS NULL`. Only `recorded_to IS NULL` distinguishes it.

**Resolution.** Use `valid_to IS NULL AND recorded_to IS NULL`, in one shared predicate.

**Prevention.** Centralise temporal predicates in the repository layer.

**How it is checked.**

- `regex`: `valid_to IS NULL(?!.*recorded_to)`

**Evidence.** CLAUDE.md §Ownership

## PG-022 — Sequences are not transactional; identity versus serial

*Severity:* **low** · *Stacks:* postgresql

**Symptom.** Gaps in ids, and duplicate-key errors after a bulk load with explicit ids.

**Root cause.** Rolled-back inserts consume sequence values, and loading explicit ids does not advance the sequence.

**Resolution.** Use `GENERATED ALWAYS AS IDENTITY`, and `setval` after bulk loads. A gapless legal number needs a counter table updated under a lock.

**Prevention.** Never use a surrogate id as an invoice or receipt number.

**How it is checked.**

- `sql`: `compare max(id) with the sequence's last_value per table`

**Sources.** <https://wiki.postgresql.org/wiki/Don%27t_Do_This#Don.27t_use_serial>

## PG-023 — `SECURITY DEFINER` functions without a pinned `search_path`

*Severity:* **high** · *Stacks:* postgresql, security

**Symptom.** A privilege-escalation risk: objects in a schema the caller controls can shadow the names the function uses.

**Root cause.** The function resolves names through the caller's `search_path`. Clusters upgraded from before PG15 keep `CREATE` on `public` for everyone.

**Resolution.** `ALTER FUNCTION … SET search_path = pg_catalog, <schema>`; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

**Prevention.** Catalog check in CI.

**How it is checked.**

- `sql`: `SELECT proname FROM pg_proc WHERE prosecdef AND NOT coalesce(array_to_string(proconfig, ',') LIKE '%search_path%', false);`

**Sources.** <https://www.postgresql.org/docs/16/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY>

## PG-024 — No `statement_timeout`, `lock_timeout` or `idle_in_transaction_session_timeout`

*Severity:* **medium** · *Stacks:* postgresql, operations

**Symptom.** A runaway query or an abandoned transaction held locks and blocked vacuum indefinitely.

**Root cause.** The defaults are unlimited.

**Resolution.** Set them per role, and override per migration.

**Prevention.** Check the role configuration.

**How it is checked.**

- `sql`: `SELECT rolname, rolconfig FROM pg_roles WHERE rolname = '<app_role>';`

**Sources.** <https://www.postgresql.org/docs/16/runtime-config-client.html#GUC-STATEMENT-TIMEOUT>

## PG-025 — A GRANT is not ownership, and the gap fails one step at a time

*Severity:* **high** · *Stacks:* postgresql, privileges, deploy

**Symptom.** The deploy died on `CREATE SCHEMA IF NOT EXISTS core` for a schema that had existed for months, right after preflight printed 'PostgreSQL: OK'. Granting ALL cleared that error, and the next migration failed with 'must be owner of table'.

**Root cause.** The database had been created by a personal superuser. PostgreSQL checks the CREATE privilege BEFORE evaluating `IF NOT EXISTS`, and `ALTER TABLE` requires ownership.

**Resolution.** An idempotent `align-postgres-ownership` repair that walks one database's catalog. It refuses if any RLS table lacks FORCE, because handing ownership to the app role would otherwise switch isolation off.

**Prevention.** Traps: `REASSIGN OWNED BY` also moves SHARED objects (other databases, template0/1). A sequence owned by an identity or serial column cannot be re-owned on its own; exclude pg_depend deptypes 'a', 'i' and 'e'.

**How it is checked.**

- `sql`: `SELECT c.oid::regclass FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname = ANY(:app_schemas) AND c.relowner <> :app_role::regrole;`
- `sql`: `SELECT has_database_privilege(current_user, current_database(), 'CREATE');`

**Evidence.** 0fb5bc0b8 (2026-09-05); footgun #29

**Sources.** <https://www.postgresql.org/docs/16/sql-reassign-owned.html>
