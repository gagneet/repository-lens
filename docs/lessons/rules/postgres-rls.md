# PostgreSQL: RLS & Tenancy: lessons learnt

**Scope.** Row-level security, tenant context, pooling, views.

| id | severity | lesson |
|---|---|---|
| PG-001 | critical | With no tenant context set, a read of a FORCE-RLS table returns 0 rows and no error |
| PG-002 | high | A bypass sentinel works only on tables whose policy has a bypass clause |
| PG-003 | medium | A join under a real tenant context silently drops rows that belong to another tenant |
| PG-004 | critical | Owners are exempt from RLS unless FORCE is set; superusers and BYPASSRLS roles are always exempt |
| PG-005 | high | A new tenant-data table created without ENABLE/FORCE RLS |
| PG-006 | high | A view over RLS tables runs with the VIEW OWNER's rights |
| PG-007 | critical | Setting the tenant GUC by string interpolation, and `SET` versus `SET LOCAL` |
| PG-008 | high | A migration's backfill UPDATE under FORCE RLS matches 0 rows |
| PG-009 | medium | Neither `count(*)` nor `n_live_tup` alone tells you whether a table is empty |
| PG-010 | high | A tenant id derived by computation instead of looked up |

## PG-001 — With no tenant context set, a read of a FORCE-RLS table returns 0 rows and no error

*Severity:* **critical** · *Stacks:* postgresql, rls

**Symptom.** A feature 'never fired' while every test was green. A lookup returned None for a tenant that exists. An outbox relay polled 0 of 15,000+ rows from its first day while logging 'worker started' on every boot.

**Root cause.** A policy such as `tenant_id = current_setting('app.tenant_id')::uuid` evaluates to NULL or false when the GUC is unset. RLS filters rows; it never raises.

**Resolution.** Set the context (a bypass sentinel or the real tenant) before querying. The relay's table got an explicit policy clause for its worker role.

**Prevention.** Keep at least one test that runs the real SQL against a populated database, not mocks. When a `count(*)` returns 0, suspect missing context before concluding the data is gone.

**How it is checked.**

- `sql`: `SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relforcerowsecurity;`
- `ast`: functions executing text(...) SQL against a FORCE-RLS table with no set_config('app.tenant_id', …) in the same function (match executed SQL, not docstrings)

**Evidence.** 36535efb4 (2026-08-29); 856ff86b5; footgun #8

## PG-002 — A bypass sentinel works only on tables whose policy has a bypass clause

*Severity:* **high** · *Stacks:* postgresql, rls

**Symptom.** A cleanup `DELETE FROM lots` run under the sentinel deleted 0 rows, and the next `DELETE FROM schemes` failed with an FK violation. A snapshot reported '0 overrides' while 13 existed.

**Root cause.** Policies are asymmetric: some tables add `OR current_setting(...) = '<sentinel>'`, others are strictly `tenant_id = current_tenant_id()`.

**Resolution.** Switch tenant context per TABLE, not per script block.

**Prevention.** List which policies carry the bypass. Lint repair scripts for DML on a no-bypass table issued after SETting the sentinel.

**How it is checked.**

- `sql`: `SELECT polrelid::regclass, pg_get_expr(polqual, polrelid) FROM pg_policy;  -- grep each for the sentinel literal`

**Evidence.** 7336d6fdb (2026-08-11); footguns #7, #8

## PG-003 — A join under a real tenant context silently drops rows that belong to another tenant

*Severity:* **medium** · *Stacks:* postgresql, rls

**Symptom.** A report showed '8 of 13 rows have set_by = NULL' for a column declared NOT NULL.

**Root cause.** RLS on the joined users table hid platform-tenant actors (super admins), so the LEFT JOIN produced NULLs.

**Resolution.** Resolve actors in a separate lookup under the bypass context.

**Prevention.** When a NOT NULL column shows NULLs through a join, suspect RLS.

**How it is checked.**

- `review`: joins from a tenant-scoped table to a table holding rows of other tenancies

**Evidence.** 9d85dd826; footgun #11

## PG-004 — Owners are exempt from RLS unless FORCE is set; superusers and BYPASSRLS roles are always exempt

*Severity:* **critical** · *Stacks:* postgresql, rls, security

**Symptom.** No symptom at all: tenant isolation switches off silently once the app role owns a table without FORCE, or connects as a superuser.

**Root cause.** PostgreSQL applies RLS to the table owner only under `FORCE ROW LEVEL SECURITY`, and never applies it to superusers or BYPASSRLS roles. RLS with no policies is default-deny. Unique constraints and FK checks bypass RLS, so they can leak whether a row exists.

**Resolution.** The ownership-repair tool checks every RLS table for FORCE and refuses if any lacks it; all 220 carry it. DATABASE_URL never points at a superuser.

**Prevention.** The app connects as a NOSUPERUSER, non-owner role. Test isolation with that role: a superuser test DB turns isolation tests green while they test nothing.

**How it is checked.**

- `sql`: `SELECT relname FROM pg_class WHERE relrowsecurity AND NOT relforcerowsecurity;`
- `sql`: `SELECT rolname FROM pg_roles WHERE rolsuper OR rolbypassrls;  -- compare to the app login role`

**Evidence.** 0fb5bc0b8 (2026-09-05); footgun #29

**Sources.** <https://www.postgresql.org/docs/16/ddl-rowsecurity.html>

## PG-005 — A new tenant-data table created without ENABLE/FORCE RLS

*Severity:* **high** · *Stacks:* postgresql, rls

**Symptom.** One table had no isolation for months. It was harmless only because nothing wrote to it, and it was about to hold bank details.

**Root cause.** RLS was added by a one-off sweep migration, and tables created after it were not covered.

**Resolution.** A migration added the policy in the same change that gave the table sensitive data.

**Prevention.** A test asserts that every table with a `tenant_id` column has `relrowsecurity`, `relforcerowsecurity` and at least one policy. Run it against the DEPLOYED schema too (see PG-018).

**How it is checked.**

- `sql`: `SELECT c.oid::regclass FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname='tenant_id' WHERE c.relkind='r' AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity OR NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid));`

**Evidence.** ae45be32e (2026-09-12); tests/backend/test_rls_coverage.py

## PG-006 — A view over RLS tables runs with the VIEW OWNER's rights

*Severity:* **high** · *Stacks:* postgresql, views, rls

**Symptom.** A latent leak: correct today, broken the moment the view owner differs from the caller, FORCE is dropped, or the view reads a table without RLS.

**Root cause.** Before `security_invoker` (PG15+), views evaluate policies and privileges as their owner. Materialized views store the owner's view of the data and cannot enforce the caller's policies at all.

**Resolution.** Every view is created `WITH (security_invoker = true)`, and a test asserts it.

**Prevention.** Never expose a materialized view directly; reach it through a tenant-filtered, permission-checked interface.

**How it is checked.**

- `sql`: `SELECT c.oid::regclass FROM pg_class c WHERE c.relkind IN ('v','m') AND c.relnamespace::regnamespace::text NOT IN ('pg_catalog','information_schema') AND NOT coalesce(c.reloptions @> '{security_invoker=true}', false);`
- `regex`: `CREATE (OR REPLACE )?VIEW(?![^;]*security_invoker)`

**Evidence.** 1e778d719 (2026-09-02); docs/architecture/database_views_viability_2026-09-02.md

**Sources.** <https://www.postgresql.org/docs/16/sql-createview.html>

## PG-007 — Setting the tenant GUC by string interpolation, and `SET` versus `SET LOCAL`

*Severity:* **critical** · *Stacks:* postgresql, asyncpg, security, pooling

**Symptom.** 103 sites built `f"SET app.tenant_id = '{tid}'"`. `text('SET LOCAL app.tenant_id = :tid')` can never work.

**Root cause.** PostgreSQL cannot bind parameters in `SET`. A plain `SET` (or `set_config(..., false)`) is session-scoped, so a pooled connection carries the previous request's tenant into the next one. Under PgBouncer transaction pooling, a SET outside a transaction lands on an arbitrary server connection.

**Resolution.** `SELECT set_config('app.tenant_id', $1, true)` inside the request transaction.

**Prevention.** Reset the connection on checkout (`DISCARD ALL` / `reset_on_return`). `current_setting('app.tenant_id', true)` must yield NULL, never a default tenant.

**How it is checked.**

- `regex`: `SET( LOCAL)?\s+app\.\w+\s*=\s*['\"]?\{`
- `regex`: `set_config\('app\.[^']+',\s*[^,]+,\s*false\)`

**Evidence.** 8a420bb14; fddd857b8; 74a6ac3f0 (2026-08-29/30)

**Sources.** <https://www.postgresql.org/docs/16/sql-set.html> <https://www.pgbouncer.org/features.html>

## PG-008 — A migration's backfill UPDATE under FORCE RLS matches 0 rows

*Severity:* **high** · *Stacks:* postgresql, alembic, rls

**Symptom.** The backfill left the column NULL and the following `SET NOT NULL` failed. Another migration reported 'linked 0 accounts' when 27 were unlinked.

**Root cause.** Alembic's env never set a tenant context, so every RLS read inside the migration saw nothing.

**Resolution.** Disable/re-enable RLS around the backfill (or iterate real tenants), and assert the post-condition inside the migration.

**Prevention.** Every data migration ends with an assertion that counts the rows it was meant to fix.

**How it is checked.**

- `ast`: UPDATE/DELETE on a FORCE-RLS table in upgrade() with no set_config or DISABLE/ENABLE and no trailing assertion

**Evidence.** fe332612e (0077, 2026-08-03); 1e778d719 (0117)

## PG-009 — Neither `count(*)` nor `n_live_tup` alone tells you whether a table is empty

*Severity:* **medium** · *Stacks:* postgresql, statistics

**Symptom.** `n_live_tup` read 0 for tables holding 3,480, 2,272 and 322 rows (never analysed), and 101 against a real 87 for another. A table census came back 44 populated / 202 empty when the truth was 67/179.

**Root cause.** `count(*)` is filtered by RLS. `n_live_tup` and `reltuples` are statistics that are zero until ANALYZE runs, and `reltuples = -1` means never analysed. The census script looked up the real tenants BEFORE setting the sentinel, so that lookup also returned 0 rows.

**Resolution.** Take the MAX of the sentinel `count(*)`, each real tenant's `count(*)`, and `n_live_tup`. Set the sentinel BEFORE listing tenants, and assert more than one context was used.

**Prevention.** Run `ANALYZE` after bulk loads. Treat `n_live_tup = 0` with `last_analyze IS NULL` as 'unmeasured'.

**How it is checked.**

- `sql`: `SELECT relname, n_live_tup, last_analyze, last_autoanalyze FROM pg_stat_user_tables WHERE last_analyze IS NULL AND last_autoanalyze IS NULL;`

**Evidence.** 9a421f8ff (2026-09-03); 1e778d719

**Sources.** <https://www.postgresql.org/docs/16/routine-vacuuming.html#VACUUM-FOR-STATISTICS>

## PG-010 — A tenant id derived by computation instead of looked up

*Severity:* **high** · *Stacks:* postgresql, foreign-keys

**Symptom.** No registration ever created a user row. The FK rejected every insert, and a try/except swallowed the error.

**Root cause.** The tenant id was computed as `uuid5(NAMESPACE_DNS, f'tenant-{id}')`, which is not the real tenant.

**Resolution.** Resolve `tenant_id` from the tenants/schemes table. Never derive it.

**Prevention.** If a write must happen, assert that it landed.

**How it is checked.**

- `regex`: `uuid5\([^)]*\)\s*#?.*tenant|tenant_id\s*=\s*uuid\.?uuid5\(`

**Evidence.** footgun #16
