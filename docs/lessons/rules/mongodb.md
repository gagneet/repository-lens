# MongoDB & PyMongo: lessons learnt

**Scope.** Indexes, types, filters, upserts, pagination, injection.

| id | severity | lesson |
|---|---|---|
| MG-001 | high | A global unique index on a tenant-scoped collection |
| MG-002 | high | A tenant-scoping wrapper covers filters but not inserts or unique keys, and 'global' collections bypass it |
| MG-003 | low | Numeric strings sort lexicographically |
| MG-004 | medium | Mixed BSON types in one field make range queries miss documents |
| MG-005 | medium | `{field: false}` versus `{$ne: true}` for flags that may be absent |
| MG-006 | high | `to_list(N)` silently truncates |
| MG-007 | high | An upsert or `find_one` on a key that should be unique but isn't enforced |
| MG-008 | medium | A check-then-insert seeder races concurrent startups |
| MG-009 | medium | `$setOnInsert` idempotency is permanent |
| MG-010 | high | `insert_one(doc)` mutates `doc` by adding an ObjectId `_id` |
| MG-011 | low | Index creation conflicts and partial-filter limits |
| MG-012 | medium | Unsorted pagination, and `estimated_document_count` versus `count_documents` |
| MG-013 | medium | BSON datetimes come back naive, and ObjectId and string ids don't match |
| MG-014 | high | NoSQL operator injection from JSON bodies |
| MG-015 | medium | Motor is deprecated: migrate to PyMongo's native async client |
| MG-016 | medium | Transactions need a replica set |

## MG-001 — A global unique index on a tenant-scoped collection

*Severity:* **high** · *Stacks:* mongodb, multi-tenant

**Symptom.** `DuplicateKeyError index: year_1`: the SECOND tenant failed mid-import and was left with partial data.

**Root cause.** The index predated multi-tenancy. An unreferenced seed script would have recreated it.

**Resolution.** Create the compound `(tenant_id, key)` unique index BEFORE dropping the old one. A deploy-preflight gate requires every unique index to lead with the tenant key or carry a written exemption, and stale exemptions fail too.

**Prevention.** Exemptions must be justified from data, not from code: six 'the id is a uuid' exemptions were false because the seeds wrote slugs.

**How it is checked.**

- `shell`: `db.coll.getIndexes() — flag unique:true whose key lacks the tenant field`
- `regex`: `create_index\([^)]*unique=True(?![^)]*tenant_id)`

**Evidence.** 4c32d4072; 501aced42; 5405b80dc

## MG-002 — A tenant-scoping wrapper covers filters but not inserts or unique keys, and 'global' collections bypass it

*Severity:* **high** · *Stacks:* mongodb, multi-tenant

**Symptom.** An invitations list returned every tenant's invitations because the collection was on the GLOBAL list. A lookup by a natural key matched across tenants. Self-registered users were stored with `tenant_id: None`.

**Root cause.** The wrapper injects the tenant into query filters only.

**Resolution.** Removed the collection from the global list, scoped the lookups, and required the tenant field on inserts.

**Prevention.** Review the global-collection list like an allow-list of privileged operations.

**How it is checked.**

- `ast`: queries on GLOBAL collections without the tenant field in the filter
- `test`: inserts into scoped collections require the tenant field

**Evidence.** c231bedab; 12be5aecb (2026-09-04)

## MG-003 — Numeric strings sort lexicographically

*Severity:* **low** · *Stacks:* mongodb

**Symptom.** '1', '10', '11', '2'.

**Root cause.** String fields sort by byte order.

**Resolution.** Post-sort in application code with a natural key, or store a numeric sort key.

**Prevention.** Mirror of TS-006.

**How it is checked.**

- `regex`: `\.sort\(['\"]\w*(number|no|num)['\"]`

**Evidence.** footgun #6

## MG-004 — Mixed BSON types in one field make range queries miss documents

*Severity:* **medium** · *Stacks:* mongodb

**Symptom.** `year` was an int for one tenant and a string for another, so `{$gte: 2026}` silently skipped the strings.

**Root cause.** Different writers used different types, and BSON comparison is bracketed by type.

**Resolution.** Normalise the type, and add `$jsonSchema` validators.

**Prevention.** Profile field types per collection.

**How it is checked.**

- `shell`: `db.c.aggregate([{$group:{_id:{$type:'$field'}, n:{$sum:1}}}])  -- more than one type ⇒ finding`

**Evidence.** memory: pattern_tenant_scoped_collection_with_global_unique_index

## MG-005 — `{field: false}` versus `{$ne: true}` for flags that may be absent

*Severity:* **medium** · *Stacks:* mongodb

**Symptom.** `{is_archived: False}` silently excluded older documents that lacked the field.

**Root cause.** An equality match does not match a missing field, while `$ne` does. `{f: null}` matches both null and missing.

**Resolution.** Use `{'$ne': True}` for 'not flagged'. Add `$exists` when you mean existence, and backfill hot-path fields so plain equality can be used.

**Prevention.** Regex lint.

**How it is checked.**

- `regex`: `['\"](is_\w+)['\"]:\s*False`

**Evidence.** deea81ccc

**Sources.** <https://www.mongodb.com/docs/manual/reference/operator/query/ne/>

## MG-006 — `to_list(N)` silently truncates

*Severity:* **high** · *Stacks:* mongodb, motor, pymongo

**Symptom.** A 130-row plan showed 100 rows and lost the LAST years. A collection grew past `to_list(200)`.

**Root cause.** `to_list(length)` returns at most N documents with no warning.

**Resolution.** Aggregate totals server-side (`$group`), use named limit constants, and paginate.

**Prevention.** Log when a result length equals its cap.

**How it is checked.**

- `regex`: `to_list\(\d+\)`

**Evidence.** deea81ccc (2026-08-30); footgun #2

## MG-007 — An upsert or `find_one` on a key that should be unique but isn't enforced

*Severity:* **high** · *Stacks:* mongodb

**Symptom.** Two cache documents existed for one tenant. `update_one` refreshed one of them, and `find_one` with no sort could serve the frozen $0 document.

**Root cause.** Concurrent upserts without a unique index create duplicates.

**Resolution.** A deterministic sort, a dedupe script, then a unique index. Index creation logs rather than crashing startup while duplicates remain.

**Prevention.** Every `upsert=True` filter must be covered by a unique index.

**How it is checked.**

- `review`: each update_one(filter, upsert=True): a unique index covers the filter keys

**Evidence.** 6152266d4 (2026-09-05)

**Sources.** <https://www.mongodb.com/docs/manual/reference/method/db.collection.updateOne/#upsert-with-duplicate-values>

## MG-008 — A check-then-insert seeder races concurrent startups

*Severity:* **medium** · *Stacks:* mongodb, concurrency

**Symptom.** 17 system chat groups where 9 belonged; the duplicates were written 0.8 ms apart.

**Root cause.** A `find_one` followed by `insert_one` is not atomic.

**Resolution.** A single upsert keyed on a partial unique index, treating `DuplicateKeyError` as success.

**Prevention.** Lint for the check-then-insert shape.

**How it is checked.**

- `ast`: count_documents/find_one followed by insert_one on the same collection in one function

**Evidence.** a67ceb9d4 (2026-09-02)

## MG-009 — `$setOnInsert` idempotency is permanent

*Severity:* **medium** · *Stacks:* mongodb

**Symptom.** 176 of 209 backfill rows matched archived documents and never revived them. An invalid vote permanently occupied its key. An idempotency key containing today's date duplicated on re-run.

**Root cause.** The upsert matched an existing document, even an invalid or archived one, and did nothing.

**Resolution.** Validate BEFORE the upsert. Idempotency keys never contain wall-clock values.

**Prevention.** Review every `$setOnInsert`.

**How it is checked.**

- `ast`: update_one(..., {'$setOnInsert': …}, upsert=True) preceding a validation of the same value

**Evidence.** footgun #5

## MG-010 — `insert_one(doc)` mutates `doc` by adding an ObjectId `_id`

*Severity:* **high** · *Stacks:* mongodb, pymongo, fastapi

**Symptom.** Every real HTTP call returned 500 (ObjectId is not JSON-serialisable) AFTER the write had committed, so clients retried and duplicated it. Unit tests that called the function directly passed.

**Root cause.** The driver adds `_id` to the dict you pass in. A `find_one` without a projection returns `_id` too. `uuid.UUID` values are not BSON-encodable without configuration.

**Resolution.** Use `insert_one(dict(doc))` and a shared projection constant `{'_id': 0}`.

**Prevention.** Test through the HTTP layer.

**How it is checked.**

- `ast`: a variable passed to insert_one/insert_many that is later returned
- `ast`: find/find_one without an _id-excluding projection whose result is returned

**Evidence.** 128eed577; 381074c26; db73f2c02

## MG-011 — Index creation conflicts and partial-filter limits

*Severity:* **low** · *Stacks:* mongodb

**Symptom.** `IndexOptionsConflict` (code 85) aborted the rest of `ensure_indexes`. `$ne` was rejected inside `partialFilterExpression`.

**Root cause.** The same key spec under a different name conflicts, and partial filters support only a subset of operators.

**Resolution.** Compare key specs before creating, and use `$eq`/`$exists`/`$gt`/`$in`. Manage indexes in migrations, not on every boot.

**Prevention.** Dedupe BEFORE creating a unique index.

**How it is checked.**

- `regex`: `partialFilterExpression[^}]*\$(ne|nin|not)`

**Evidence.** tasks/lessons.md LESSONS 15-17

## MG-012 — Unsorted pagination, and `estimated_document_count` versus `count_documents`

*Severity:* **medium** · *Stacks:* mongodb

**Symptom.** Pages duplicated and skipped documents. A census count drifted.

**Root cause.** Natural order is not insertion order. `estimated_document_count()` reads metadata and ignores filters.

**Resolution.** Always sort on a unique tiebreaker and prefer range pagination. Use `count_documents(filter)` for truth.

**Prevention.** Lint `.skip(` without `.sort(`.

**How it is checked.**

- `regex`: `\.skip\((?![\s\S]{0,80}\.sort\()`
- `regex`: `estimated_document_count`

**Sources.** <https://www.mongodb.com/docs/manual/reference/method/cursor.sort/#sort-consistency>

## MG-013 — BSON datetimes come back naive, and ObjectId and string ids don't match

*Severity:* **medium** · *Stacks:* mongodb, pymongo, time

**Symptom.** Comparing with aware datetimes raised TypeError. `{_id: '65f…'}` matched nothing.

**Root cause.** Without `tz_aware=True` the driver returns naive datetimes. Strings and ObjectIds never compare equal.

**Resolution.** `MongoClient(..., tz_aware=True, tzinfo=timezone.utc)`. Pick one id representation per collection and convert at the boundary.

**Prevention.** Lint client construction.

**How it is checked.**

- `regex`: `(Async)?MongoClient\((?![^)]*tz_aware)`

**Sources.** <https://pymongo.readthedocs.io/en/stable/examples/datetimes.html>

## MG-014 — NoSQL operator injection from JSON bodies

*Severity:* **high** · *Stacks:* mongodb, security

**Symptom.** A login filter built from raw JSON would accept `{'$ne': null}`.

**Root cause.** Client-supplied dicts were passed straight into query filters.

**Resolution.** Validate with strict Pydantic `str` types. Never pass raw request dicts into filters.

**Prevention.** Grep for request JSON flowing into find calls.

**How it is checked.**

- `regex`: `find(_one)?\(\s*(await\s+)?request\.json|find(_one)?\(\s*body\b`

**Sources.** <https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/07-Input_Validation_Testing/05.6-Testing_for_NoSQL_Injection>

## MG-015 — Motor is deprecated: migrate to PyMongo's native async client

*Severity:* **medium** · *Stacks:* mongodb, motor, pymongo

**Symptom.** An end-of-life dependency sat on every data path.

**Root cause.** MongoDB deprecated Motor in favour of `pymongo.AsyncMongoClient`. The APIs differ in which calls are coroutines (`aggregate`, `watch`, `start_session`, `close`).

**Resolution.** DONE here (2026-04-28): migrated to `pymongo.AsyncMongoClient`.

**Prevention.** Never import both `motor` and `pymongo.asynchronous` in one module, and check awaits on the calls that changed.

**How it is checked.**

- `regex`: `^from motor|^import motor`

**Evidence.** backend/database.py (GAP-INF-001)

**Sources.** <https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/>

## MG-016 — Transactions need a replica set

*Severity:* **medium** · *Stacks:* mongodb

**Symptom.** `start_transaction()` failed on a standalone development server.

**Root cause.** Multi-document transactions require a replica set.

**Resolution.** Run a single-node replica set in development as well, and use `with_transaction(callback)` for its retry loop.

**Prevention.** Keep development topology identical to production.

**How it is checked.**

- `config`: mongod started with --replSet in dev compose

**Sources.** <https://www.mongodb.com/docs/manual/core/transactions/>
