# Auth & Session (frontend): lessons learnt

**Scope.** NextAuth/session loading, token storage, impersonation.

| id | severity | lesson |
|---|---|---|
| AU-001 | high | `Bearer ${token}` sends the literal string `Bearer null` |
| AU-002 | critical | A hardcoded fallback for the auth signing secret makes sessions forgeable |
| AU-003 | medium | A catch-all in credentials `authorize()` hides rate limits and outages as 'wrong password' |
| AU-004 | medium | Logout via `router.push` keeps the session cookie; navigation after `signIn` loses it |

## AU-001 — `Bearer ${token}` sends the literal string `Bearer null`

*Severity:* **high** · *Stacks:* javascript, axios, auth

**Symptom.** Every sub-request on one page returned 401. Backend tests could not see it.

**Root cause.** The token came from `localStorage.getItem('token')`, which returned null because the app keeps its token in React state. A template literal turns null into a truthy header string, which bypassed the shared interceptor's `if (token)` guard.

**Resolution.** Use the authenticated axios instance from context. A source-scan test forbids hand-built Authorization headers.

**Prevention.** One authenticated client per app, and no raw `axios.get` with manual auth headers in components.

**How it is checked.**

- `regex`: ``Bearer \$\{[^}]+\}` where the identifier comes from (local|session)Storage\.getItem`
- `regex`: `axios\.(get|post|put|patch|delete)\([^)]*Authorization`

**Evidence.** 122871ac9 (2026-09-03); tests/frontend/unit/authenticated-client-usage.test.ts; 6c950ddfd

## AU-002 — A hardcoded fallback for the auth signing secret makes sessions forgeable

*Severity:* **critical** · *Stacks:* nextauth, security

**Symptom.** When `AUTH_SECRET` was unset, sessions were signed with the literal `'secret'`.

**Root cause.** `process.env.AUTH_SECRET || 'secret'` fails open.

**Resolution.** Fail fast at startup when the secret is missing.

**Prevention.** Secrets never have literal fallbacks, in any language.

**How it is checked.**

- `regex`: `(AUTH|NEXTAUTH|JWT)_SECRET\s*(\|\||\?\?|or)\s*['\"]`

**Evidence.** 37ef3c596 (2026-04-18)

## AU-003 — A catch-all in credentials `authorize()` hides rate limits and outages as 'wrong password'

*Severity:* **medium** · *Stacks:* nextauth

**Symptom.** A 429 from the backend looked identical to invalid credentials (`CredentialsSignin`).

**Root cause.** `authorize()` had `catch { return null }`.

**Resolution.** Re-throw on 429 and 5xx, and map NextAuth error codes to distinct user messages.

**Prevention.** Never collapse different failure classes into one return value.

**How it is checked.**

- `ast`: authorize() whose catch block only returns null without inspecting status

**Evidence.** 22079ce54 (2026-04-18)

## AU-004 — Logout via `router.push` keeps the session cookie; navigation after `signIn` loses it

*Severity:* **medium** · *Stacks:* nextauth

**Symptom.** Users 'logged out' but were still authenticated. After login, the next page saw no session.

**Root cause.** Client-side navigation neither clears nor waits for the cookie. NextAuth v5 reads `AUTH_URL`, not `NEXTAUTH_URL`, and needs `AUTH_TRUST_HOST=true` behind a proxy.

**Resolution.** `await signOut({callbackUrl})`; use a full `window.location.href` navigation after `signIn`; set the v5 env names.

**Prevention.** Keep JWT session payloads minimal: they live in a cookie readable by the client (~4 KB, chunked) and cannot be revoked before they expire.

**How it is checked.**

- `ast`: logout handler calling router.push('/'|'/login') without an awaited signOut
- `regex`: `getServerSession|NEXTAUTH_URL`

**Evidence.** fd4df225b; aaf731348 (2026-07-02)

**Sources.** <https://authjs.dev/getting-started/migrating-to-v5>
