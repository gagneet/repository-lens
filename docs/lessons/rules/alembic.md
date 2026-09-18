# Alembic Migrations: lessons learnt

**Scope.** Revision discipline, heads, env.py, locks, autogenerate.

| id | severity | lesson |
|---|---|---|
| AL-001 | medium | A revision id longer than `alembic_version.version_num` (VARCHAR(32)) |
| AL-002 | high | A migration applied to production from an UNTRACKED file |
| AL-003 | high | A migration applied from an unmerged branch breaks every deploy from main |
| AL-004 | medium | Two branches each add a migration on the same parent: multiple heads, and git sees no conflict |
| AL-005 | high | `connection.execute()` in env.py auto-begins an outer transaction that swallows the migration run |
| AL-006 | high | Autogenerate blind spots: renames, enums, server defaults, policies |
| AL-007 | high | Lock-heavy DDL: `CREATE INDEX CONCURRENTLY`, volatile defaults, `SET NOT NULL` |
| AL-008 | medium | Importing ORM models into a migration binds it to current code |
| AL-009 | low | Migration linters that read source text naively |

## AL-001 — A revision id longer than `alembic_version.version_num` (VARCHAR(32))

*Severity:* **medium** · *Stacks:* alembic

**Symptom.** The migration's DDL ran, then the final `UPDATE alembic_version` raised StringDataRightTruncation and rolled the whole transaction back. Offline `--sql` mode does not catch it.

**Root cause.** Alembic's version table column is 32 characters, and descriptive, sentence-style revision ids exceed that.

**Resolution.** Use `NNNN_few_words` ids, with the filename matching the revision.

**Prevention.** A CI test asserts `len(revision) <= 32` for every file in `versions/`.

**How it is checked.**

- `regex`: `^revision\s*=\s*['\"][^'\"]{33,}['\"]`

**Evidence.** e787a9c94; 284d4a095; bf0975313; footgun #9

## AL-002 — A migration applied to production from an UNTRACKED file

*Severity:* **high** · *Stacks:* alembic, process

**Symptom.** Production reported head `0124_…` while that file showed `??` in `git status`. This happened twice.

**Root cause.** The CI head check migrates a throwaway container from the committed files, so it validates the repo against itself and can never see the live head.

**Resolution.** Commit the migration in the same change as the code that needs it, then run `upgrade`.

**Prevention.** Deploy preflight reads the live `alembic_version` and asserts that `git ls-files` contains that revision and that it is an ancestor of HEAD.

**How it is checked.**

- `shell`: `rev=$(psql -Atc 'select version_num from core.alembic_version'); git ls-files backend/alembic/versions | grep -q "$rev"`

**Evidence.** footgun #28; memory: pattern_untracked_migration_applied_to_production

## AL-003 — A migration applied from an unmerged branch breaks every deploy from main

*Severity:* **high** · *Stacks:* alembic

**Symptom.** Every deploy failed with `Can't locate revision identified by '0127_…'` for a day before anyone noticed.

**Root cause.** Alembic walks backwards from the database's revision, and main does not have that file.

**Resolution.** Fix forward by merging; do not downgrade. `deploy.sh` now prints `alembic current`, `alembic heads` and the versions listing on failure.

**Prevention.** The same preflight as AL-002: the database head must exist in the deployed checkout.

**How it is checked.**

- `shell`: `alembic current vs alembic heads in the deployed checkout`

**Evidence.** d3b21141c (2026-09-05)

## AL-004 — Two branches each add a migration on the same parent: multiple heads, and git sees no conflict

*Severity:* **medium** · *Stacks:* alembic

**Symptom.** `alembic upgrade head` failed with 'Multiple head revisions'.

**Root cause.** Both files declare the same `down_revision`, and they are different files, so git merges them cleanly.

**Resolution.** Renumber and repoint `down_revision` (or `alembic merge heads`), and update every prose reference.

**Prevention.** CI asserts exactly one head.

**How it is checked.**

- `ci`: test $(alembic heads | wc -l) -eq 1

**Evidence.** d90743fbc (2026-09-03)

## AL-005 — `connection.execute()` in env.py auto-begins an outer transaction that swallows the migration run

*Severity:* **high** · *Stacks:* alembic, sqlalchemy

**Symptom.** All 62 migrations 'ran', and afterwards `alembic_version` did not exist.

**Root cause.** SQLAlchemy 2.x autobegins. Alembic's transaction nested inside that outer one, which was rolled back when the connection closed.

**Resolution.** Commit the pre-statement (`CREATE SCHEMA`) before `context.begin_transaction()`.

**Prevention.** After any env.py change, assert `alembic current` against a fresh database.

**How it is checked.**

- `review`: connection.execute(...) in env.py before context.begin_transaction() with no commit

**Evidence.** 8a97f4f48; 1c755796a (2026-07-05)

## AL-006 — Autogenerate blind spots: renames, enums, server defaults, policies

*Severity:* **high** · *Stacks:* alembic

**Symptom.** A renamed column was generated as drop + add, which would have lost data. Enum value additions, views, RLS policies and triggers never appeared in the diff.

**Root cause.** Autogenerate compares ORM metadata only, and cannot infer intent.

**Resolution.** Always read and edit the generated file. Use `op.alter_column(new_column_name=…)` for renames and `ALTER TYPE … ADD VALUE` in an autocommit block.

**Prevention.** In review, flag drop_column + add_column on the same table.

**How it is checked.**

- `review`: op.drop_column and op.add_column on the same table in one migration

**Sources.** <https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect>

## AL-007 — Lock-heavy DDL: `CREATE INDEX CONCURRENTLY`, volatile defaults, `SET NOT NULL`

*Severity:* **high** · *Stacks:* alembic, postgresql, performance

**Symptom.** A 'fast' ALTER queued behind a long transaction and stalled every query on the table. `CREATE INDEX CONCURRENTLY` failed inside the migration transaction.

**Root cause.** Most ALTERs take ACCESS EXCLUSIVE, and every later query queues behind them. A volatile default forces a table rewrite. `SET NOT NULL` scans the whole table. CONCURRENTLY cannot run inside a transaction.

**Resolution.** `SET lock_timeout` with retry; add FK and CHECK constraints `NOT VALID`, then `VALIDATE`; use `autocommit_block()` for concurrent index builds.

**Prevention.** Also flag `ADD COLUMN … NOT NULL` with no DEFAULT.

**How it is checked.**

- `regex`: `server_default=.*(gen_random_uuid|clock_timestamp|random)`
- `regex`: `postgresql_concurrently=True(?![\s\S]*autocommit_block)`
- `sql`: `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;`

**Evidence.** 7f2a35fa8 (2026-09-11)

**Sources.** <https://www.postgresql.org/docs/16/sql-altertable.html#SQL-ALTERTABLE-NOTES> <https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.autocommit_block>

## AL-008 — Importing ORM models into a migration binds it to current code

*Severity:* **medium** · *Stacks:* alembic

**Symptom.** An old migration broke after the models changed.

**Root cause.** A migration must reflect the schema at ITS point in history, not the current model classes.

**Resolution.** Use `sa.table()`/`sa.column()` lightweight constructs or raw SQL inside migrations, and batch large backfills in a separate script.

**Prevention.** Test downgrades.

**How it is checked.**

- `regex`: `^from (models|db_postgres\.models)  (in alembic/versions)`

**Sources.** <https://alembic.sqlalchemy.org/en/latest/cookbook.html>

## AL-009 — Migration linters that read source text naively

*Severity:* **low** · *Stacks:* alembic, tooling

**Symptom.** An RLS lint missed tables created in a loop and misread combined ALTER statements.

**Root cause.** Loops over literal lists, file-order ENABLE/FORCE/NO FORCE, comma-joined actions and schema qualification all defeat line-by-line reading.

**Resolution.** The lint now unrolls literal loops, follows statement order, splits actions, matches exact qualified names, and ignores print, log and comment text.

**Prevention.** Test lints with adversarial fixtures.

**How it is checked.**

- `test`: fixtures covering loops, combined ALTERs and comments

**Evidence.** 7f2a35fa8 (2026-09-11)
