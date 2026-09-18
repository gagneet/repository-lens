# Next.js & React: lessons learnt

**Scope.** Rendering, hydration, build output, App Router, Next 16 changes.

| id | severity | lesson |
|---|---|---|
| NX-001 | critical | A statement line with no semicolon merges with a following line that starts with `(`, `[` or a backtick after minification |
| NX-002 | high | A role guard that runs before the session resolves redirects authorised users |
| NX-003 | medium | Reading browser storage in a `useState` initialiser causes a hydration mismatch |
| NX-004 | high | An effect whose condition is always true and which sets its own dependency loops forever |
| NX-005 | high | Unstable references in a dependency array cause render loops |
| NX-006 | medium | StrictMode runs effects twice in development; non-idempotent effects fire twice |
| NX-007 | medium | A one-shot URL parameter that is never consumed re-fires its action |
| NX-008 | low | Invalid DOM nesting breaks layout and hydration |
| NX-009 | low | A `<Link>` wrapper breaks the child's `h-full` |
| NX-010 | high | `NEXT_PUBLIC_*` is inlined at build time, and a Create React App prefix is `undefined` in Next.js |
| NX-011 | high | Next.js 16: request APIs are Promises only |
| NX-012 | critical | Next.js 16: `middleware` becomes `proxy`, and proxy is not an authorization layer |
| NX-013 | critical | Server Actions are public POST endpoints |
| NX-014 | high | The server/client boundary follows the import graph |
| NX-015 | high | `tsc --noEmit` passes while `next build` fails |
| NX-016 | high | A warm `.next`, `.turbopack` or `tsbuildinfo` cache masks build regressions |
| NX-017 | low | Stale `.next/types` route validators reference deleted routes |
| NX-018 | high | Rebuilding `.next` under a live `next start`, or two servers sharing one `.next`, causes 500s |
| NX-019 | medium | A stray `.d.ts` next to a `.jsx` page breaks Turbopack resolution |
| NX-020 | high | Security headers set on the wrong tier |
| NX-021 | medium | Root-layout `robots: {index: true}` is inherited by authenticated routes |
| NX-022 | critical | Everything under `public/` is served to the whole internet with no auth |
| NX-023 | medium | Seeded navigation points at routes that don't exist |
| NX-024 | low | HTML `<title>` is RCDATA, so injected markup renders as literal text |
| NX-025 | medium | Next.js 16 removals: `next lint`, runtime config, and a custom webpack config fail silently or loudly |
| NX-026 | medium | next/image defaults tightened; a local-IP allowance is an SSRF risk |
| NX-027 | medium | Tailwind v4: CSS-first config, and classes built at runtime are never generated |
| NX-028 | low | A nav or UI state keyed off `onClick` only misses link-driven affordances |

## NX-001 — A statement line with no semicolon merges with a following line that starts with `(`, `[` or a backtick after minification

*Severity:* **critical** · *Stacks:* nextjs, typescript, nextauth, minification

**Symptom.** Login worked in `next dev` and failed in the production build with `JWTSessionError: TypeError: t.role is not a function`.

**Root cause.** Automatic semicolon insertion does not insert a semicolon before a line that begins with `(`. `(token as any).role` followed by a line starting `(session.user as any).data` was parsed as a call: `t.role(e.user)`. The dev server did not minify, so it never showed up there.

**Resolution.** Added explicit semicolons to every statement in the NextAuth `jwt()`/`session()` callbacks. Introduced intermediate variables (`const tok = token as any;`) so no line starts with `(`. Left a warning comment in the file.

**Prevention.** Enforce `semi: always` and `no-unexpected-multiline` in ESLint for all TS/JS. Verify auth flows against a production build, never only against `next dev`.

**How it is checked.**

- `lint`: ESLint `semi: ['error','always']` + `no-unexpected-multiline: 'error'`
- `regex`: `a line not ending in [;{},(\[] followed by a line matching ^\s*[\(\[`]`

**Evidence.** fd4df225b (2026-02-21); frontend/src/auth.ts

## NX-002 — A role guard that runs before the session resolves redirects authorised users

*Severity:* **high** · *Stacks:* react, nextauth, client-components

**Symptom.** Admins were bounced to `/dashboard`. The Playwright specs covering admin pages failed about one run in two.

**Root cause.** On the first render `user` is null, so `isAdmin()` is false and a `useEffect` or render guard redirects before the session has loaded.

**Resolution.** Every guard checks `if (loading) return;` (or renders null) BEFORE any role predicate, both in the effect and in the render guard.

**Prevention.** Guard helpers should return a tri-state (`loading | allowed | denied`) so a boolean check cannot be written before loading completes.

**How it is checked.**

- `ast`: a useEffect or early return that calls router.replace/push inside a branch on is[A-Z]\w*\( or user?.role, where the scope destructures useAuth() without reading `loading` first

**Evidence.** 2c1a0934e (2026-07-02); CLAUDE.md §Auth Loading Guard

## NX-003 — Reading browser storage in a `useState` initialiser causes a hydration mismatch

*Severity:* **medium** · *Stacks:* react, ssr, nextjs

**Symptom.** React error #418 (hydration mismatch) on first load.

**Root cause.** `useState(() => localStorage.getItem(...))` returns null during the server render and a value in the browser, so the first client render differs from the server HTML.

**Resolution.** Initialise the state to a server-safe value and read storage in a mount `useEffect` (or render behind a `mounted` flag).

**Prevention.** Treat every access to `window`, `document`, `navigator` or storage during render as server-unsafe. Non-deterministic values (`Date.now()`, `Math.random()`, `toLocaleString()` without a fixed `timeZone`) cause the same class of mismatch.

**How it is checked.**

- `ast`: useState( initialiser referencing localStorage|sessionStorage|window.|document.|navigator.
- `regex`: `toLocale(Date|Time)?String\(\)|Math\.random\(\)|Date\.now\(\) inside a component render body`

**Evidence.** 8fd208c77, 1ac3c8182 (2026-04-21)

**Sources.** <https://react.dev/reference/react-dom/client/hydrateRoot#handling-different-client-and-server-content>

## NX-004 — An effect whose condition is always true and which sets its own dependency loops forever

*Severity:* **high** · *Stacks:* react, hooks

**Symptom.** A POST fired about once per second and hammered every downstream endpoint.

**Root cause.** The effect compared two identifiers of different kinds (a human-readable number against a UUID), so the condition was always true. It then called `setToken`, and `token` was in the effect's dependency list, so the effect re-ran.

**Resolution.** Compare like-for-like identifiers, make the condition idempotent, and never set state that the same effect depends on unless it converges.

**Prevention.** Add a test asserting the request count per mount (e.g. exactly one POST). Review every effect that calls a setter for one of its own dependencies.

**How it is checked.**

- `ast`: useEffect whose deps include X and whose body (directly or via a called callback) calls setX under a condition
- `test`: count network calls per mount with a mocked client; assert an upper bound

**Evidence.** b9bc1a0cd, 9d6e0b1be (2026-06-28)

## NX-005 — Unstable references in a dependency array cause render loops

*Severity:* **high** · *Stacks:* react, hooks, eslint

**Symptom.** Adding `router` from `useRouter()` to an effect's dependencies, to satisfy `exhaustive-deps`, looped under the test harness and turned 12 tests red.

**Root cause.** An object that is re-created on every render (a mocked router, an inline object or array literal, an inline `accessors` config) changes identity on every render, so the effect re-fires, sets state, re-renders, and repeats.

**Resolution.** Hoist constant configuration to module scope, memoise derived objects, and keep unstable hook results out of the dependency list or read them through a ref.

**Prevention.** When satisfying `exhaustive-deps`, check that each added dependency is referentially stable.

**How it is checked.**

- `ast`: deps array containing a useRouter()/useNavigate() result or an object/array literal created in render, where the effect calls a state setter

**Evidence.** de93f0f38 (2026-08-20); tasks/lessons.md

## NX-006 — StrictMode runs effects twice in development; non-idempotent effects fire twice

*Severity:* **medium** · *Stacks:* react, strictmode

**Symptom.** Duplicate POSTs and duplicate analytics events in development. Combined with RSC prefetch, a burst of guaranteed 403s looked like a redirect loop.

**Root cause.** In development, React 18/19 StrictMode mounts, unmounts and re-mounts every component, so an effect with no cleanup runs twice. Effects that fire fetches the user is not entitled to make produce 403s on every mount.

**Resolution.** Mutations belong in event handlers or server actions, not effects. Fetch effects return an AbortController cleanup. Gate scoped fetches on claims already present in the session.

**Prevention.** Lint: flag `useEffect` bodies that call `.post/.put/.delete`.

**How it is checked.**

- `regex`: `useEffect\([^)]*=>\s*\{[^}]*\.(post|put|patch|delete)\(`

**Evidence.** de27aeff1 (2026-05-03); 7a6e4c2e7

**Sources.** <https://react.dev/reference/react/StrictMode#fixing-bugs-found-by-double-rendering-in-development>

## NX-007 — A one-shot URL parameter that is never consumed re-fires its action

*Severity:* **medium** · *Stacks:* nextjs, router

**Symptom.** `?new=true` survived every `router.replace()` and re-opened the dialog after each navigation.

**Root cause.** The effect read the parameter and opened the dialog, but nothing removed the parameter from the URL, so every re-render re-triggered it.

**Resolution.** Consume the parameter: after acting on it, `router.replace` to the same path without it.

**Prevention.** Pair every `searchParams.get(X)` that triggers an action with a code path that deletes X.

**How it is checked.**

- `ast`: effect reading searchParams.get(X) that triggers setOpen(true)/an action, with no code path that removes X from the URL

**Evidence.** de93f0f38

## NX-008 — Invalid DOM nesting breaks layout and hydration

*Severity:* **low** · *Stacks:* react, html

**Symptom.** Tables rendered misaligned, and hydration warnings appeared for `<div>` inside `<p>` and `<tr>` inside `<tr>`.

**Root cause.** Browsers repair invalid nesting while parsing the HTML, so the DOM no longer matches React's virtual tree.

**Resolution.** Use fragments with sibling `<tr>` elements, and `<span>` inside `<p>`.

**Prevention.** Run a JSX nesting validator (React's validateDOMNesting rules) in lint or tests.

**How it is checked.**

- `ast`: JSX <div>|<p>|<table> inside <p>; <tr> directly inside <tr>

**Evidence.** ae822ed69 (2026-04-26); 785a102bc (2026-08-25)

## NX-009 — A `<Link>` wrapper breaks the child's `h-full`

*Severity:* **low** · *Stacks:* nextjs, tailwind

**Symptom.** Card grids rendered cards of unequal height, and the link-cards were missing their footer.

**Root cause.** `next/link` renders an `<a>` element with no height of its own, so `h-full` on its child resolves against an auto-height parent.

**Resolution.** Put `h-full` (or `block h-full`) on the `Link` itself.

**Prevention.** Lint for a `<Link>` whose only child carries `h-full` while the Link lacks it.

**How it is checked.**

- `ast`: <Link className=...> without h-full whose single child has className containing h-full

**Evidence.** 3d8ae5253, 314ec5960

## NX-010 — `NEXT_PUBLIC_*` is inlined at build time, and a Create React App prefix is `undefined` in Next.js

*Severity:* **high** · *Stacks:* nextjs, env

**Symptom.** Eight files silently read `undefined` from `process.env.REACT_APP_*`. After changing a public env value, restarting the server did nothing.

**Root cause.** Next.js text-replaces `process.env.NEXT_PUBLIC_X` into the client bundle during `next build`. Other prefixes are not exposed to the client. Dynamic access (`process.env[name]`) is never inlined.

**Resolution.** Renamed the variables to `NEXT_PUBLIC_*` and documented that a change needs a rebuild, not a restart.

**Prevention.** Never put secrets under `NEXT_PUBLIC_`. Server-only values are read on the server at request time.

**How it is checked.**

- `regex`: `process\.env\.REACT_APP_`
- `regex`: `NEXT_PUBLIC_\w*(SECRET|KEY|TOKEN|PASSWORD)`
- `ast`: client component ('use client' or pages/) reading process.env.X where X lacks the NEXT_PUBLIC_ prefix

**Evidence.** 4e7c1e86b (2026-04-18)

**Sources.** <https://nextjs.org/docs/app/guides/environment-variables#bundling-environment-variables-for-the-browser>

## NX-011 — Next.js 16: request APIs are Promises only

*Severity:* **high** · *Stacks:* nextjs

**Symptom.** Code written for Next 14 reads `params.id` synchronously, which fails at type-check or returns a Promise at runtime.

**Root cause.** Next 16 removed the Next 15 sync compatibility shim. `params`, `searchParams`, `cookies()`, `headers()` and `draftMode()` must be awaited, and the `upgrade` codemod does not run the async-API migration.

**Resolution.** Run `npx @next/codemod@canary next-async-request-api .`, then type pages with `PageProps<'/route'>` from `next typegen`.

**Prevention.** Run `tsc --noEmit` in CI.

**How it is checked.**

- `regex`: `(params|searchParams)\.\w+|cookies\(\)\.(get|set)|headers\(\)\.get  (flag when not preceded by await)`

**Sources.** <https://nextjs.org/docs/app/guides/upgrading/version-16#async-request-apis-breaking-change>

## NX-012 — Next.js 16: `middleware` becomes `proxy`, and proxy is not an authorization layer

*Severity:* **critical** · *Stacks:* nextjs, security

**Symptom.** Auth decisions made only in middleware are bypassable. CVE-2025-29927: a forged `x-middleware-subrequest` header skipped middleware entirely on unpatched 11.1–15.2.2.

**Root cause.** Middleware/proxy matches paths and does not cover every entry point: server actions, route handlers and data fetches. Next 16 renames `middleware.ts` to `proxy.ts`, which runs on Node only.

**Resolution.** Authorise inside the data-access layer, every route handler and every server action. Strip `x-middleware-subrequest` at the reverse proxy.

**Prevention.** Rename `middleware.ts` with the codemod, and audit any auth logic that exists only in proxy.

**How it is checked.**

- `shell`: `ls src/middleware.* middleware.*; grep -n skipMiddleware next.config.*`
- `config`: nginx: proxy_set_header x-middleware-subrequest "";

**Sources.** <https://nextjs.org/blog/cve-2025-29927> <https://nextjs.org/docs/app/guides/upgrading/version-16#middleware-to-proxy>

## NX-013 — Server Actions are public POST endpoints

*Severity:* **critical** · *Stacks:* nextjs, react19, security

**Symptom.** A page-level auth check does not protect the actions defined on that page. Their return values are serialised to the client.

**Root cause.** Every exported `'use server'` function that is referenced from anywhere is reachable by a direct POST, whatever the page's own guard does.

**Resolution.** In every action: authenticate, check ownership (IDOR), validate arguments (zod), and return a minimal DTO. Mark the DAL with `import 'server-only'`.

**Prevention.** When there are multiple instances, set `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY`, and set `serverActions.allowedOrigins` behind a proxy.

**How it is checked.**

- `shell`: `rg -l "^['\"]use server" | xargs rg -L "auth\(|getCurrentUser"`

**Sources.** <https://nextjs.org/docs/app/guides/data-security#mutating-data>

## NX-014 — The server/client boundary follows the import graph

*Severity:* **high** · *Stacks:* nextjs, security

**Symptom.** A module that reads a non-public env var, imported by a client component, produced `undefined` in the browser. Passing a DB row as a prop shipped every column to the client.

**Root cause.** Anything imported by a `'use client'` module is bundled for the browser, and props crossing the boundary are serialised in full.

**Resolution.** Mark DAL and secret modules with `import 'server-only'`, and pass DTOs, never rows.

**Prevention.** Optionally enable `experimental.taint` for sensitive objects.

**How it is checked.**

- `shell`: `rg -L "server-only" $(rg -l "process\.env\.(?!NEXT_PUBLIC)" src/lib)`

**Sources.** <https://nextjs.org/docs/app/guides/data-security#preventing-client-side-execution-of-server-only-code>

## NX-015 — `tsc --noEmit` passes while `next build` fails

*Severity:* **high** · *Stacks:* nextjs, turbopack, typescript, allowJs

**Symptom.** The build failed on `the name Button is defined multiple times` in a `.jsx` file, and on an imported package that was not installed. Type-checking was green.

**Root cause.** `tsc` with `allowJs` does not report duplicate import bindings or unresolved modules in JS the way the bundler does. Only `next build` exercises the real module graph.

**Resolution.** Rule: no change is 'verified' until a clean `next build` has passed.

**Prevention.** Enable `import/no-duplicates` and `import/no-unresolved` for JS/JSX, and run `next build` as a CI job.

**How it is checked.**

- `lint`: eslint-plugin-import: no-duplicates, no-unresolved on **/*.{js,jsx}
- `ci`: a job that runs `next build` from a clean checkout

**Evidence.** b371e1b15 (2026-08-25); tasks/lessons.md

## NX-016 — A warm `.next`, `.turbopack` or `tsbuildinfo` cache masks build regressions

*Severity:* **high** · *Stacks:* nextjs, ci

**Symptom.** A dependency bump built green locally and broke the clean production build.

**Root cause.** Incremental caches reuse results computed against the old dependency tree.

**Resolution.** Run `rm -rf .next .turbopack tsconfig.tsbuildinfo` before every verification build.

**Prevention.** CI verification jobs must not restore `.next/cache`.

**How it is checked.**

- `ci`: a build step not preceded by a cache clean, or a cache restore of .next in a verification job

**Evidence.** 3ca08b858 (2026-08-10)

## NX-017 — Stale `.next/types` route validators reference deleted routes

*Severity:* **low** · *Stacks:* nextjs

**Symptom.** Type errors pointed at pages that had been deleted.

**Root cause.** Next generates route-type validators under `.next/types` and does not prune them when routes disappear.

**Resolution.** A `prebuild` script removes `.next/types` and `.next/dev/types` (not `.next/cache`).

**Prevention.** Clean generated type directories whenever routes are removed.

**How it is checked.**

- `config`: package.json has a prebuild step that clears .next/types

**Evidence.** 0eaf9408f (2026-08-07); frontend/scripts/clean-next-types.js

## NX-018 — Rebuilding `.next` under a live `next start`, or two servers sharing one `.next`, causes 500s

*Severity:* **high** · *Stacks:* nextjs, deploy

**Symptom.** Nine routes returned `ERRORED_DOCUMENT_REQUEST (500)` and clients saw `ChunkLoadError`. A deploy was killed by a concurrent build.

**Root cause.** The running server lazy-loads chunks from `.next`. Replacing or deleting them underneath it, running two `next build`s at once, or pruning `node_modules` while live chunks still import a removed package all break it.

**Resolution.** Build and restart atomically, never share a `.next` between processes, provide a `--clean` deploy flag, and never `rm -rf .next` in the serving directory while the service runs.

**Prevention.** A `.next` owned by root after a sudo build leaves the service unable to read it, so check ownership after every build. If a different file is missing on each retry, suspect a concurrent writer (`ps` for `next build`).

**How it is checked.**

- `shell`: `deploy scripts that rm -rf .next or run next build in the serving dir before stopping the service; multiple units with the same WorkingDirectory`

**Evidence.** 0bfa6dc1a; docs/deployment/INSTALL_AND_DEPLOY.md

## NX-019 — A stray `.d.ts` next to a `.jsx` page breaks Turbopack resolution

*Severity:* **medium** · *Stacks:* nextjs, turbopack, typescript

**Symptom.** `Can't resolve 'next/dist/compiled/process'`.

**Root cause.** A declaration file with the same basename as a page module confused the resolver.

**Resolution.** Deleted the stray sidecars.

**Prevention.** Flag any `.d.ts` that shares a basename with a `.js`/`.jsx` file under `pages/` or `app/`.

**How it is checked.**

- `shell`: `for f in $(find src -name '*.d.ts'); do b=${f%.d.ts}; ls $b.js $b.jsx 2>/dev/null; done`

**Evidence.** 0006e9928 (2026-08-10)

## NX-020 — Security headers set on the wrong tier

*Severity:* **high** · *Stacks:* nextjs, nginx, security

**Symptom.** The CSP was attached only to API JSON responses, never to the HTML pages. `frame-ancestors 'none'` conflicted with `X-Frame-Options: SAMEORIGIN`. A rate-limit location regex matched nothing.

**Root cause.** The headers were configured on the backend tier, which does not serve the documents the browser renders.

**Resolution.** Moved the CSP into `next.config` `headers()` as Report-Only first, aligned the frame directives, and added `frame-src blob:` for PDF iframes.

**Prevention.** Assert on the headers of an HTML page response, not an API one.

**How it is checked.**

- `config`: next.config.* headers() lacks Content-Security-Policy*; frame-ancestors 'none' alongside X-Frame-Options SAMEORIGIN; Report-Only without report-to

**Evidence.** bf5b901a5 (2026-09-07)

## NX-021 — Root-layout `robots: {index: true}` is inherited by authenticated routes

*Severity:* **medium** · *Stacks:* nextjs, seo

**Symptom.** Private pages were indexable. A `'use client'` layout cannot export `metadata`, so it could not override the setting.

**Root cause.** App Router metadata cascades from the root layout.

**Resolution.** Send an `X-Robots-Tag: noindex` header from `next.config`, built from one private-route list, with a test that walks the `app/` directories.

**Prevention.** Keep `robots.txt` and the private-route list derived from the same source.

**How it is checked.**

- `ast`: root layout metadata robots.index true + authenticated segments under 'use client' layouts

**Evidence.** b6d170731 (2026-09-02)

## NX-022 — Everything under `public/` is served to the whole internet with no auth

*Severity:* **critical** · *Stacks:* nextjs, security

**Symptom.** 24 MB of sensitive documents sat under `public/`, including three copies of one plan: a PDF, JPEGs, and a base64 copy embedded in an innocently named `.html` file.

**Root cause.** Static files bypass every auth layer, and nothing checked what was committed there.

**Resolution.** Moved the records behind an authenticated asset endpoint. A test refuses identifying tokens in public paths, guide pages over 1 MB, and large images that have no stated reason.

**Prevention.** Use a SIZE rule as well as a name rule: the third copy was found by asking which files were large, not by grepping a name.

**How it is checked.**

- `shell`: `find public -size +1M; grep -l 'data:image/[a-z]*;base64' public/**/*.html`
- `test`: assert no file in public/ exceeds N MB unless listed with a written reason

**Evidence.** 317e7e58d (2026-09-08)

## NX-023 — Seeded navigation points at routes that don't exist

*Severity:* **medium** · *Stacks:* nextjs, app-router

**Symptom.** 12 sidebar links returned 404. The route audit passed because it only read hrefs hardcoded in components.

**Root cause.** The nav config lives in database seeds, not code, so a code-only audit never sees it.

**Resolution.** Collected every href from code AND seeds and resolved each one against `app/**/page.tsx` and `public/`.

**Prevention.** Also flag nav items that belong to no group.

**How it is checked.**

- `test`: enumerate hrefs from nav sources incl. seed data; each must resolve to an app route or public file

**Evidence.** 407de29d6 (2026-08-10); docs/fixes/nav_stale_route_404s_2026-08-10.md

## NX-024 — HTML `<title>` is RCDATA, so injected markup renders as literal text

*Severity:* **low** · *Stacks:* html

**Symptom.** A tab title displayed `<span data-x>` literally.

**Root cause.** The contents of `<title>` are raw text, not parsed as markup.

**Resolution.** Put plain text in titles.

**Prevention.** Regex check on title contents.

**How it is checked.**

- `regex`: `<title>[^<]*<[a-z]`

**Evidence.** 49ca3e814 (2026-09-07)

## NX-025 — Next.js 16 removals: `next lint`, runtime config, and a custom webpack config fail silently or loudly

*Severity:* **medium** · *Stacks:* nextjs

**Symptom.** CI that relied on `next build` running lint now lints nothing. `getConfig()` throws. A custom or plugin-injected `webpack` config fails the build under Turbopack, which is now the default.

**Root cause.** Next 16 removed `next lint`, the `eslint` key, `serverRuntimeConfig` and `publicRuntimeConfig`, and moved `experimental.turbopack` to top-level `turbopack`.

**Resolution.** Run `eslint` explicitly in CI, use env vars for configuration, and migrate aliases to `turbopack.resolveAlias` (or build with `--webpack`).

**Prevention.** Re-read the upgrade guide on every major bump. Also: `revalidateTag` needs a second argument (use `updateTag` for read-your-writes), and parallel-route slots need a `default.js`.

**How it is checked.**

- `regex`: `getConfig\(|RuntimeConfig|next lint|experimental\.turbopack|revalidateTag\(\s*[^,)]+\)`

**Sources.** <https://nextjs.org/docs/app/guides/upgrading/version-16>

## NX-026 — next/image defaults tightened; a local-IP allowance is an SSRF risk

*Severity:* **medium** · *Stacks:* nextjs, security

**Symptom.** Images returned 400 inside a VPC, and `domains` produced deprecation warnings.

**Root cause.** Next 16 blocks private/local IPs unless `dangerouslyAllowLocalIP` is set, defaults `qualities` to `[75]`, and deprecates `images.domains`.

**Resolution.** Use explicit `remotePatterns` (protocol plus hostname) and avoid `**` wildcards.

**Prevention.** Review next.config image settings on upgrade.

**How it is checked.**

- `regex`: `domains:|dangerouslyAllowLocalIP|hostname:\s*['\"]\*\*['\"]`

**Sources.** <https://nextjs.org/docs/app/guides/upgrading/version-16#nextimage-changes>

## NX-027 — Tailwind v4: CSS-first config, and classes built at runtime are never generated

*Severity:* **medium** · *Stacks:* tailwind

**Symptom.** Styles vanished for `` `bg-${color}-500` ``. Borders, rings and shadows changed visually after the upgrade. Removing a UI library whose classes were generated at runtime dropped its styles.

**Root cause.** Tailwind scans source text for literal class names. v4 reads `@theme` in CSS, not `tailwind.config.js` (unless `@config` is used), respects `.gitignore` when detecting sources, and changed several defaults (border colour, ring width, shadow scale names).

**Resolution.** Use full literal class maps, use `@source` for packages that are gitignored or live in `node_modules`, and review the visual defaults after upgrading.

**Prevention.** Lint for template-literal class names.

**How it is checked.**

- `regex`: `className=\{`[^`]*\$\{`

**Evidence.** 8d7419b26

**Sources.** <https://tailwindcss.com/docs/upgrade-guide> <https://tailwindcss.com/docs/detecting-classes-in-source-files#dynamic-class-names>

## NX-028 — A nav or UI state keyed off `onClick` only misses link-driven affordances

*Severity:* **low** · *Stacks:* react, accessibility

**Symptom.** Cards that navigated via `href` were missing the footer/affordance that `onClick` cards had.

**Root cause.** Rendering logic checked `onClick` to decide whether something was actionable.

**Resolution.** Derive 'actionable' from either `href` or `onClick`.

**Prevention.** Review component props that gate affordances.

**How it is checked.**

- `review`: components deciding interactivity from a single prop

**Evidence.** 314ec5960
