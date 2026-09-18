# API Contract (client ↔ server): lessons learnt

**Scope.** Axios base URLs, error envelopes, payload shapes that fail silently.

| id | severity | lesson |
|---|---|---|
| AC-001 | high | A double `/api/api` prefix on the axios base URL |
| AC-002 | high | A global error-envelope rewrap breaks every client that reads `detail.code` |
| AC-003 | medium | A FastAPI 422 `detail` is an array, and `typeof [] === 'object'` |
| AC-004 | medium | `responseType: 'blob'` also applies to the error body |
| AC-005 | high | FormData sent through an instance whose default Content-Type is JSON is JSON-stringified |
| AC-006 | medium | Classifying 403s by status code alone collapses different refusals |
| AC-007 | high | A page reads a key the API never emits, and renders nothing |
| AC-008 | high | A request body carries fields the server silently drops, or omits a required one |
| AC-009 | medium | A root-relative asset URL from the API resolves against the frontend origin |
| AC-010 | high | Missing data rendered as a confident zero or verdict |
| AC-011 | medium | `.catch(() => {})` plus a loading fallback means a failure looks like loading forever |
| AC-012 | medium | Hardcoded 'now': the year, relative labels, currency formatting |

## AC-001 — A double `/api/api` prefix on the axios base URL

*Severity:* **high** · *Stacks:* axios

**Symptom.** 33 call sites in 9 files returned 404 in production.

**Root cause.** The instance's `baseURL` already ended in `/api`, and callers passed `'/api/…'` as well.

**Resolution.** Removed the prefix at every call site and documented the rule next to the client.

**Prevention.** Flag any path whose first segment equals the last segment of the instance's `baseURL`.

**How it is checked.**

- `regex`: `\bapi\.(get|post|put|patch|delete)\(\s*[`'\"]/api/`

**Evidence.** 2addb9a70, 70a5eb335, 5b444aa30 (2026-04-25)

## AC-002 — A global error-envelope rewrap breaks every client that reads `detail.code`

*Severity:* **high** · *Stacks:* axios, fastapi

**Symptom.** Specific error messages became unreachable and users saw generic toasts, including for the login 'pending approval' case.

**Root cause.** A server-side global exception handler rewrapped structured `detail` into `{error: {code, message, metadata}}`, so `err.response.data.detail.code` was `undefined` and the UI silently degraded.

**Resolution.** Added one tolerant reader, `getApiErrorDetail()`, that accepts both shapes, and migrated callers to it.

**Prevention.** An error-shape change is an API contract change: grep every consumer.

**How it is checked.**

- `lint`: ESLint no-restricted-syntax on MemberExpression response.data.detail.(code|message) outside the owner module
- `regex`: `response\??\.data\??\.detail\??\.(code|message)`

**Evidence.** 17c7881ba, cb62106a6 (2026-08-27)

## AC-003 — A FastAPI 422 `detail` is an array, and `typeof [] === 'object'`

*Severity:* **medium** · *Stacks:* javascript, fastapi

**Symptom.** Validation errors rendered as empty messages.

**Root cause.** The reader branched on `typeof detail === 'object'` and destructured named keys from an array.

**Resolution.** Check `Array.isArray` first and join the `msg` fields.

**Prevention.** Handle every documented error shape in the one error reader.

**How it is checked.**

- `ast`: typeof x === 'object' branch that destructures named keys with no Array.isArray(x) check before it

**Evidence.** aa2e58923 (2026-08-28)

## AC-004 — `responseType: 'blob'` also applies to the error body

*Severity:* **medium** · *Stacks:* axios

**Symptom.** A failed download showed `[object Blob]` or a generic message.

**Root cause.** axios decodes an error response with the same `responseType`, so the JSON error arrives as a Blob.

**Resolution.** Added an async reader: `await blob.text()`, then `JSON.parse`.

**Prevention.** Every blob request's catch block must decode the Blob.

**How it is checked.**

- `ast`: call with responseType 'blob' whose catch does not call .text()/the async reader

**Evidence.** 4eff813d5, 5eb0efa57 (2026-09-04)

## AC-005 — FormData sent through an instance whose default Content-Type is JSON is JSON-stringified

*Severity:* **high** · *Stacks:* axios

**Symptom.** Every upload returned 422 because the request had no file part.

**Root cause.** axios `transformRequest` sees a JSON content type and runs `JSON.stringify(formDataToJSON(data))`.

**Resolution.** Don't hardcode `Content-Type: application/json` on the shared instance, or override it per multipart call.

**Prevention.** Test uploads through the real client instance.

**How it is checked.**

- `ast`: api.(post|put)(url, <FormData>) on an instance created with headers Content-Type application/json and no per-call override

**Evidence.** 5eb0efa57

## AC-006 — Classifying 403s by status code alone collapses different refusals

*Severity:* **medium** · *Stacks:* axios, api-design

**Symptom.** 'Feature disabled', 'outside your appointment' and 'role not allowed' all rendered as one generic 403. Removing the interceptor branch kept the suite green.

**Root cause.** The client keyed off `status === 403` instead of the backend's typed error code.

**Resolution.** One `classifyApiError()` owner; the interceptor attaches the classification to every failure, with static tests asserting the interceptor still calls it.

**Prevention.** Pages never compare error-code literals themselves.

**How it is checked.**

- `regex`: `status\s*===\s*403 (outside the classifier)`

**Evidence.** ac6f11f39 (2026-08-29)

## AC-007 — A page reads a key the API never emits, and renders nothing

*Severity:* **high** · *Stacks:* typescript, react, api-contract

**Symptom.** One tab was empty for every tenant. A countdown never rendered. No error anywhere.

**Root cause.** The component read `data.capital_outlook`, which only an older engine emitted, and `voting_closes_at`, where the writer uses `voting_deadline`. An undefined key and a legitimately empty state look identical. TypeScript cannot catch it when the response is typed `any` or lives in `.jsx`.

**Resolution.** Extracted the keys each JSX dereferences and asserted that the backend emits each one. Aligned key sets across implementations.

**Prevention.** Generate client types from the OpenAPI schema, or contract-test recorded fixtures.

**How it is checked.**

- `ast`: collect data?.X / props.x.y reads per component, diff against OpenAPI response schema keys

**Evidence.** 360fdb1d7 (2026-09-05); 71c59b20f (2026-09-06); 3f600c11b (2026-09-12); 4170d67c7

## AC-008 — A request body carries fields the server silently drops, or omits a required one

*Severity:* **high** · *Stacks:* api-contract, pydantic

**Symptom.** Voting always returned 422 because `lot_id` was missing. `estimated_cost` was discarded by `extra='ignore'`, so the user's value was never stored.

**Root cause.** Client payloads and server request models drifted. The server ignored unknown keys instead of rejecting them.

**Resolution.** Fixed the payloads, and use `extra='forbid'` on request models so a typo fails loudly.

**Prevention.** Compare literal request bodies against the OpenAPI request schema.

**How it is checked.**

- `ast`: api.post(url, {literal keys}) compared against the OpenAPI requestBody: unknown keys and missing required keys

**Evidence.** 71c59b20f

## AC-009 — A root-relative asset URL from the API resolves against the frontend origin

*Severity:* **medium** · *Stacks:* nextjs, html

**Symptom.** `<img src='/api/settings/logo/…'>` 404'd silently. `src=""` made some browsers re-request the current page.

**Root cause.** A backend-relative path is resolved by the browser against the Next.js origin.

**Resolution.** One URL-resolver helper that prefixes the backend URL and returns null for absent values.

**Prevention.** Never render `src=""`.

**How it is checked.**

- `regex`: `src=\"\"|src=\{[^}]*\|\|\s*''\}`

**Evidence.** 4eff813d5

## AC-010 — Missing data rendered as a confident zero or verdict

*Severity:* **high** · *Stacks:* react, typescript, ux

**Symptom.** An unmeasured score rendered red on one screen and `Healthy` on another (`?? 100`). A fallback rate of `?? 0.035` was shown as fact. `amount || 0` appeared at 456 sites.

**Root cause.** `??`/`||` defaults turn 'unknown' into a number, and `undefined >= 80` is false.

**Resolution.** The scoring helpers return an explicit `'unmeasured'` member, and missing values render as an em dash.

**Prevention.** Zero and missing are different states in every layer: API, formatter and UI.

**How it is checked.**

- `regex`: `\?\?\s*\d|\|\|\s*0\b  (on numeric display fields)`
- `ast`: >=|<= comparisons on possibly-undefined props

**Evidence.** feefe5259 (2026-09-09); 21771129c; de843f769

## AC-011 — `.catch(() => {})` plus a loading fallback means a failure looks like loading forever

*Severity:* **medium** · *Stacks:* react

**Symptom.** A page sat on 'Loading…' permanently when the request failed.

**Root cause.** The error was swallowed, and render only distinguished data from no data.

**Resolution.** Model three states (loading, error, data) and render the error.

**Prevention.** Ban empty catch handlers in UI code.

**How it is checked.**

- `regex`: `\.catch\(\s*\(\)\s*=>\s*\{\s*\}\s*\)|catch\s*(\([^)]*\))?\s*\{\s*\}`

**Evidence.** 4764b8394 (2026-09-02)

## AC-012 — Hardcoded 'now': the year, relative labels, currency formatting

*Severity:* **medium** · *Stacks:* javascript

**Symptom.** `CURRENT_YEAR = 2026`, a literal '3.5 yrs ago', `` `$${(v/1000).toFixed(0)}k` `` on 12 chart axes, and an off-by-one financial year.

**Root cause.** Values that change over time or by locale were written as literals.

**Resolution.** Derive dates from the clock, and route every money display through one formatter owner.

**Prevention.** Money detectors must match the AST (see GT-008), not single lines.

**How it is checked.**

- `regex`: `\b(19|20)\d{2}\b assigned to a year-like identifier`
- `regex`: `[`'\"]\$\$\{|>\$\{`
- `regex`: `Intl\.NumberFormat\('en-[A-Z]{2}' (outside the owner)`

**Evidence.** 21771129c; 6cb1e8cec; 9ccab84da; 6f6a4ab7f
