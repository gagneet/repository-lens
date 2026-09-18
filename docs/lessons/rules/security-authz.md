# Security & Authorisation: lessons learnt

**Scope.** BOLA, mass assignment, effective roles, claims, credentials.

| id | severity | lesson |
|---|---|---|
| SC-001 | critical | BOLA/IDOR: an object fetched by id with no ownership or tenant check |
| SC-002 | high | Mass assignment: request models that accept privileged fields |
| SC-003 | high | Reading the raw role instead of the effective role |
| SC-004 | high | A permission check that reads a claim nobody populated |
| SC-005 | medium | Tri-state authorisation answers treated as booleans |
| SC-006 | critical | Approval granted on the strength of a public fact |
| SC-007 | low | 403 versus 404, and error-code classification |
| SC-008 | medium | Rate-limit and trusted-proxy configuration |

## SC-001 — BOLA/IDOR: an object fetched by id with no ownership or tenant check

*Severity:* **critical** · *Stacks:* fastapi, authorization, owasp

**Symptom.** A detail endpoint returned any record by id. A todo endpoint accepted an arbitrary user id and echoed back that person's name.

**Root cause.** The list route filtered by permission and the detail route did not, or the guard existed only in an unregistered duplicate.

**Resolution.** Every object read and write resolves the object THROUGH the caller's scope (`WHERE id = :id AND tenant_id = :t AND <visibility>`) and returns 404 on a miss. Database RLS is the second line.

**Prevention.** For every `/{id}` route, test that user A cannot read, update or delete user B's object.

**How it is checked.**

- `ast`: route handlers with a path id param whose DB lookup filter lacks the tenant/owner field
- `test`: cross-tenant and cross-user access matrix per id route

**Evidence.** CLAUDE.md §The duplicate that RUNS; memory: pattern_hiding_test_data_is_not_removing_it

**Sources.** <https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/>

## SC-002 — Mass assignment: request models that accept privileged fields

*Severity:* **high** · *Stacks:* fastapi, pydantic, owasp

**Symptom.** Clients could set `is_test_data`, `role` or `is_approved` in a body or header.

**Root cause.** The request model reused the storage model, or the code did `**body` into an update.

**Resolution.** Separate Create/Update models containing only client-settable fields, with `extra='forbid'`. Derive server-owned fields on the server.

**Prevention.** Lint for `**payload.dict()` in `update_one`/`UPDATE` calls.

**How it is checked.**

- `regex`: `\$set['\"]?\s*:\s*(body|payload|data)(\.model_dump\(\)|\.dict\(\))`
- `ast`: request models declaring role/is_admin/is_approved/tenant_id/is_test_data

**Evidence.** CLAUDE.md §Never let the caller set the flag

**Sources.** <https://owasp.org/API-Security/editions/2023/en/0xa3-broken-object-property-level-authorization/>

## SC-003 — Reading the raw role instead of the effective role

*Severity:* **high** · *Stacks:* authorization

**Symptom.** Elevated users got silent 403s, or a guard that included an elevated role never matched.

**Root cause.** Elevation changes the effective role, not the stored role.

**Resolution.** One `effective_role()` helper, used by every guard, and a test that bans raw `user['role']` in guards.

**Prevention.** Use role constants, never string literals.

**How it is checked.**

- `regex`: `current_user\[['\"]role['\"]\]\s*(in|==|not in)`

**Evidence.** CLAUDE.md §Role Guard Rules

## SC-004 — A permission check that reads a claim nobody populated

*Severity:* **high** · *Stacks:* authorization

**Symptom.** The guard never fired in production; the unit test passed because it supplied the claim itself.

**Root cause.** The claim was hydrated only on a different request path.

**Resolution.** One resolver function computes entitlements, and the guard calls it.

**Prevention.** Before gating on a claim, find the code that populates it on THIS request path.

**How it is checked.**

- `review`: guards reading current_user[...] keys not set by the auth dependency

**Evidence.** commit 969c600

## SC-005 — Tri-state authorisation answers treated as booleans

*Severity:* **medium** · *Stacks:* authorization

**Symptom.** 'Unmeasured' (None) was treated as False, flagging every record in a tenant with no imported data.

**Root cause.** Truthiness collapses None and False.

**Resolution.** Compare `is False` or `=== false` explicitly, and render 'unmeasured' distinctly.

**Prevention.** Type tri-state values as `bool | None` and lint for truthiness checks on them.

**How it is checked.**

- `review`: `if not x` where x is Optional[bool]

**Evidence.** CLAUDE.md §A Link Is Not a Roll

## SC-006 — Approval granted on the strength of a public fact

*Severity:* **critical** · *Stacks:* authentication

**Symptom.** Matching a name against a public register minted an approved, active account on an unverified address. An unauthenticated call approved a DIFFERENT pending account.

**Root cause.** Knowledge of a public fact was treated as a credential.

**Resolution.** Only a credential approves an account: a hashed, expiring, single-use invite token bound to its scope. Nothing else may be added to that expression.

**Prevention.** Never let an unauthenticated request mutate another account, or write a credential onto an account it hasn't proven it owns.

**How it is checked.**

- `review`: registration/approval logic whose inputs are not secrets issued by the system

**Evidence.** CLAUDE.md §Only a Credential Approves an Account

## SC-007 — 403 versus 404, and error-code classification

*Severity:* **low** · *Stacks:* authorization, api-contract

**Symptom.** 403s leaked that an object exists, and the UI rendered the same message for three different refusals.

**Root cause.** Status codes alone carry too little information.

**Resolution.** Return 404 when the caller has no right to know the object exists, and a typed error code for 403s (feature disabled / scope / role), classified once on the client.

**Prevention.** Never compare error-code literals in pages; use the classifier.

**How it is checked.**

- `regex`: `['\"]MANAGER_FUNCTION_SCOPE['\"]  -- outside the classifier`

**Evidence.** CLAUDE.md §Never render a 403 from the status code alone

## SC-008 — Rate-limit and trusted-proxy configuration

*Severity:* **medium** · *Stacks:* security, fastapi

**Symptom.** Every client appeared to come from the proxy's IP, so a single rate-limit bucket covered everyone. A test run raised production's login limit from 10 to 200 per minute through a shared client.

**Root cause.** X-Forwarded-For was not trusted (or was trusted from anyone), and configuration was shared between test and production.

**Resolution.** `--proxy-headers --forwarded-allow-ips=<proxy CIDRs>`, and isolated rate-limit configuration per environment.

**Prevention.** Log the resolved client IP at login.

**How it is checked.**

- `config`: uvicorn forwarded-allow-ips not '*'

**Evidence.** memory: pattern_a_repair_arms_everything_that_shared_the_break

**Sources.** <https://www.uvicorn.org/deployment/#proxies-and-forwarded-headers>
