# Multi-store Consistency & Data Repair: lessons learnt

**Scope.** Two stores with no shared transaction; shadow/parity; repair scripts.

| id | severity | lesson |
|---|---|---|
| DS-001 | high | Two stores, no shared transaction: order the writes |
| DS-002 | high | The same entity has different ids in two stores, so an update matches 0 and 'succeeds' |
| DS-003 | high | A write or sweep that swallows its failure and reports success |
| DS-004 | medium | Shadow and parity comparisons must compare the same population |
| DS-005 | medium | A routing control plane whose missing row defaults to the legacy store |
| DS-006 | high | A DR or parity figure written into a document is a reading, not a fact |
| DS-007 | high | The repair-script pattern: dry-run, back up, re-verify each row, refuse on partial match, verify survivors |
| DS-008 | low | Soft-retire with first-class columns, not a flag buried in JSON |

## DS-001 — Two stores, no shared transaction: order the writes

*Severity:* **high** · *Stacks:* postgresql, mongodb, outbox

**Symptom.** A mirror write issued inside an open PostgreSQL transaction survived that transaction's rollback. A careful restore updated one copy of a denormalised field and missed a second, leaving 68 of 87 records with the wrong name.

**Root cause.** There is no atomicity across stores.

**Resolution.** Commit the primary, then replay the secondary writes. The durable form is a transactional outbox: a row in the same transaction, and a relay using `FOR UPDATE SKIP LOCKED`, backoff and a dead-letter queue. Backfills emit through the same path.

**Prevention.** List every denormalised copy of a field before repairing it.

**How it is checked.**

- `ast`: secondary-store write calls lexically inside `async with session.begin()` or before commit()

**Evidence.** 5b3651839; 7fc20f822; ca5df6dbe; footgun #21

## DS-002 — The same entity has different ids in two stores, so an update matches 0 and 'succeeds'

*Severity:* **high** · *Stacks:* postgresql, mongodb

**Symptom.** Accounts archived in one store stayed active in the other. The mirror reported success.

**Root cause.** The mirror keyed on the primary store's id, and `update_one` returns success with `matched_count == 0`.

**Resolution.** Match on id OR a shared natural key, resolve the secondary row's own id before touching child documents, and assert `matched_count`.

**Prevention.** A zero match can be legitimate (a record that exists in one store only), so establish which case applies.

**How it is checked.**

- `ast`: update_one/update_many/delete_many results whose matched/deleted counts are never read

**Evidence.** 5b3651839; 91611f343; footgun #24

## DS-003 — A write or sweep that swallows its failure and reports success

*Severity:* **high** · *Stacks:* postgresql, mongodb

**Symptom.** An emitter logged 'emitted 87' while the database rejected all 87, because a unit number was passed into a UUID column. A teardown `delete_many` aimed at the wrong database deleted 0 rows for six months while 947 rows accumulated.

**Root cause.** Errors were swallowed, and the counts reported were attempts, not results.

**Resolution.** Return and count successes, re-count afterwards, and report what SURVIVED.

**Prevention.** Every sweep compares its deleted count against a pre-count.

**How it is checked.**

- `regex`: `except Exception:\s*(pass|logger\.)`
- `review`: deleted_count never compared with a pre-count

**Evidence.** 7fc20f822; 1a065726e; memory: pattern_a_sweep_that_succeeds_at_nothing

## DS-004 — Shadow and parity comparisons must compare the same population

*Severity:* **medium** · *Stacks:* dual-store, migration

**Symptom.** 260 'critical' diffs came from a one-unit payload compared against an 87-unit aggregate, plus test fixtures. A domain was promoted with no parity check at all. A route marked 'shadow passing' served a balance that was 100× wrong.

**Root cause.** Comparisons did not carry their population. A missing check read as 'passing'.

**Resolution.** Population markers on every comparison (a mis-scoped one records nothing), a registry test that derives the expected set of checks from the source, and a live call against a known reference value.

**Prevention.** Parity is a gate, not a reading: promotion refuses on measured divergence. Every field in a compared payload is classified (redacted, excluded or compared), or PII leaks into the diff table.

**How it is checked.**

- `test`: derive the set of parity checks from the source tree; assert each is registered

**Evidence.** 9b48824ea; CLAUDE.md §Shadow Comparisons

## DS-005 — A routing control plane whose missing row defaults to the legacy store

*Severity:* **medium** · *Stacks:* dual-store

**Symptom.** Emptying the routing table silently de-promoted every domain.

**Root cause.** Failing closed is correct, but the failure is invisible.

**Resolution.** Restore a domain's routing row only together with the data it routes to, and alert when the number of promoted domains drops.

**Prevention.** Emit a metric of routing decisions by reason.

**How it is checked.**

- `review`: count of promoted domains monitored

**Evidence.** footgun #17

## DS-006 — A DR or parity figure written into a document is a reading, not a fact

*Severity:* **high** · *Stacks:* dual-store, documentation

**Symptom.** '87/87 matching, $0.00 gap' stood in six places for five days while the live figure was '39 diverged, $37,797.50'.

**Root cause.** The sentence read as settled, so nobody re-ran the check.

**Resolution.** Documents give the re-run command next to any figure.

**Prevention.** Never quote a DR figure: run the read-only check.

**How it is checked.**

- `review`: numbers in docs without an as-of date and a re-derivation command

**Evidence.** memory: pattern_a_dr_figure_is_a_reading_not_a_fact; footgun #27

## DS-007 — The repair-script pattern: dry-run, back up, re-verify each row, refuse on partial match, verify survivors

*Severity:* **high** · *Stacks:* operations, data-repair

**Symptom.** Scripts that trusted a pattern acted on rows that didn't fit it: 17 of 87 did not match the assumed shape. A post-check that re-ran the script's own blind query printed VERIFIED on a half-applied production write.

**Root cause.** The script and its verification shared the same defect.

**Resolution.** Dry-run by default; back up the rows that will change; re-verify EVERY per-row condition; REFUSE on unresolvable or partial matches; `DELETE … RETURNING` with the count asserted; switch tenant context per table; build new indexes before dropping old ones; verify what SURVIVES with an independent query.

**Prevention.** Never let a verification reuse the artefact it verifies.

**How it is checked.**

- `review`: repair scripts lacking --apply gating, backups, refusal paths or an independent post-check

**Evidence.** 3bf1d8231; 6152266d4; 9d85dd826

## DS-008 — Soft-retire with first-class columns, not a flag buried in JSON

*Severity:* **low** · *Stacks:* postgresql, schema

**Symptom.** Retired rows were indistinguishable in queries.

**Root cause.** The retirement flag lived inside a JSONB blob.

**Resolution.** Added `retired_at` and `retired_reason` columns, so `WHERE retired_at IS NULL` is visible to reviewers.

**Prevention.** Lifecycle state belongs in typed columns.

**How it is checked.**

- `review`: status/lifecycle flags stored in JSONB

**Evidence.** 3bf1d8231 (0098)
