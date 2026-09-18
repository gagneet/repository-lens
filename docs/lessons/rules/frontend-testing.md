# Frontend Testing: lessons learnt

**Scope.** Jest, React Testing Library, Radix, Playwright.

| id | severity | lesson |
|---|---|---|
| FT-001 | high | A mock returning a new object per call plus a dependent effect makes Jest hang forever |
| FT-002 | low | Order-keyed `mockResolvedValueOnce` chains shift when a page adds a fetch |
| FT-003 | low | A hook mock missing a helper looks like a component bug |
| FT-004 | low | Tests with hardcoded dates expire |
| FT-005 | medium | The suite passes on a dirty `node_modules` |
| FT-006 | medium | Jest configuration breakage fails silently |
| FT-007 | low | A patch bump exposes a latent async race in RTL tests |
| FT-008 | low | Radix UI and Recharts need specific handling in jsdom |
| FT-009 | low | `not.toThrow()` passes on broken error-reporting code |
| FT-010 | low | Text-oracle tests break on harmless refactors |
| FT-011 | medium | Playwright pitfalls: missing APIs, redirects, drifting locators, rate limits, committed auth state |
| FT-012 | medium | Playwright is installed in a different package from the app |

## FT-001 — A mock returning a new object per call plus a dependent effect makes Jest hang forever

*Severity:* **high** · *Stacks:* jest, react

**Symptom.** 'Maximum update depth exceeded', then a worker that never exits.

**Root cause.** `jest.mock(..., () => ({ useX: () => ({ ... }) }))` returns a fresh object on every render. An effect that depends on it re-runs indefinitely.

**Resolution.** Hoist mock return values to constants so they are referentially stable.

**Prevention.** To tell a hang from an out-of-memory failure, watch CPU time (`ps -o time=`).

**How it is checked.**

- `regex`: `jest\.mock\([^)]*,\s*\(\)\s*=>\s*\(\{\s*use\w+:\s*\(\)\s*=>\s*\(\{`

**Evidence.** tasks/lessons.md (2026-08-25)

## FT-002 — Order-keyed `mockResolvedValueOnce` chains shift when a page adds a fetch

*Severity:* **low** · *Stacks:* jest

**Symptom.** Unrelated tests broke when a component gained one extra request.

**Root cause.** The mock answered by call order, not by URL.

**Resolution.** Route mocks by exact URL.

**Prevention.** Prefer URL-keyed mock routers, and match exactly, not with `includes`.

**How it is checked.**

- `regex`: `(\.mockResolvedValueOnce\([^)]*\)\s*){3,}`

**Evidence.** 3d6b2935d

## FT-003 — A hook mock missing a helper looks like a component bug

*Severity:* **low** · *Stacks:* jest

**Symptom.** A TypeError in the component under test, caused by the mock.

**Root cause.** The hand-written auth mock lacked a function that the component calls.

**Resolution.** A single shared, complete mock factory.

**Prevention.** Build mocks from the real module's export list.

**How it is checked.**

- `review`: per-test partial mocks of shared hooks

**Evidence.** 3d6b2935d

## FT-004 — Tests with hardcoded dates expire

*Severity:* **low** · *Stacks:* jest, time

**Symptom.** A test started failing on 2026-09-02 with no code change.

**Root cause.** Fixture dates compared against logic that uses 'today'.

**Resolution.** Use fake timers or relative dates.

**Prevention.** Flag literal ISO dates in fixtures used with date-relative logic.

**How it is checked.**

- `regex`: `['\"]20\d{2}-\d{2}-\d{2}['\"] in test files that do not call useFakeTimers/setSystemTime`

**Evidence.** 3d6b2935d

## FT-005 — The suite passes on a dirty `node_modules`

*Severity:* **medium** · *Stacks:* jest, ci

**Symptom.** `jest.mock('@removed/pkg')` passed locally after the package had been removed from package.json.

**Root cause.** The package was still installed on the developer machine.

**Resolution.** Run CI from a clean install.

**Prevention.** Every `jest.mock('<pkg>')` must name a declared dependency.

**How it is checked.**

- `shell`: `for each jest.mock('<pkg>') literal, assert <pkg> is in package.json deps/devDeps`

**Evidence.** dee235489 (2026-08-25)

## FT-006 — Jest configuration breakage fails silently

*Severity:* **medium** · *Stacks:* jest

**Symptom.** The `jest` block was dropped from package.json with no error. Tests outside the frontend folder could not resolve `react/jsx-runtime`. `--testPathPattern` began exiting 1.

**Root cause.** `moduleDirectories` takes directory names; `modulePaths` is the correct option for absolute paths. Jest 30 renamed `--testPathPattern` to `--testPathPatterns`, removed alias matchers (`toBeCalled`, `toThrowError`), and jsdom 26 forbids reassigning `window.location`.

**Resolution.** Fixed the configuration and updated the docs and scripts.

**Prevention.** In CI, assert that `jest --listTests` returns a non-zero count that does not drop.

**How it is checked.**

- `regex`: `--testPathPattern\b|toBeCalled\b|toThrowError|window\.location\s*=`
- `ci`: jest --listTests | wc -l >= baseline

**Evidence.** b1c90c60e, 07359040c (2026-07-06); e9bf6a8d3

**Sources.** <https://jestjs.io/docs/upgrading-to-jest30>

## FT-007 — A patch bump exposes a latent async race in RTL tests

*Severity:* **low** · *Stacks:* rtl, react19

**Symptom.** Tests asserted content set in an effect synchronously after a `waitFor`, and began failing after a React patch release.

**Root cause.** Timing changed, and a duplicate text node made `getByText` throw.

**Resolution.** Use `findBy*`, or assert inside `waitFor`.

**Prevention.** Flag `getBy*` assertions on effect-populated content outside `waitFor`.

**How it is checked.**

- `review`: getBy* for effect-set content outside waitFor/findBy

**Evidence.** bffcd63ac (2026-08-10)

## FT-008 — Radix UI and Recharts need specific handling in jsdom

*Severity:* **low** · *Stacks:* rtl, radix, shadcn, recharts

**Symptom.** Tab clicks did nothing, dialogs never appeared, tooltips threw, and charts rendered empty.

**Root cause.** Radix `TabsTrigger` activates on `mousedown`. Dialogs portal and animate. `Tooltip` requires a provider. `ResponsiveContainer` measures -1 in jsdom.

**Resolution.** Use `fireEvent.mouseDown` for tabs, `waitFor` dialog content, wrap renders in `TooltipProvider`, and pass `minWidth`/`minHeight` to charts.

**Prevention.** Keep these in a shared test-render helper.

**How it is checked.**

- `ast`: fireEvent.click on an element located by role 'tab'
- `review`: Tooltip import in a component under test with no provider in the wrapper

**Evidence.** frontend/CLAUDE.md; 4b60f76c1

## FT-009 — `not.toThrow()` passes on broken error-reporting code

*Severity:* **low** · *Stacks:* jest, jsdom

**Symptom.** A test of the global error reporter passed while the reporter was broken.

**Root cause.** jsdom swallows exceptions thrown inside event listeners. Also, `String(x)` is not total: `Object.create(null)` throws.

**Resolution.** Assert that the report arrived, not that nothing threw.

**Prevention.** Assert outcomes, not the absence of exceptions.

**How it is checked.**

- `regex`: `expect\([^)]*\)\.not\.toThrow\(\)`

**Evidence.** a19c7ae97 (2026-09-07)

## FT-010 — Text-oracle tests break on harmless refactors

*Severity:* **low** · *Stacks:* jest

**Symptom.** `includes(key)` matched `status` inside `status-finance-routes`. A regex required a prop to come first. A `.tsx` path was truncated to `.ts`.

**Root cause.** Source-scan tests used substring and regex matching on JSX source.

**Resolution.** Parse with a real parser, or match tokens exactly.

**Prevention.** Prefer AST checks for source-scan tests.

**How it is checked.**

- `review`: tests that read source files and use .includes()/regex on JSX

**Evidence.** e71c7f1bf; 3d6b2935d (2026-09-03)

## FT-011 — Playwright pitfalls: missing APIs, redirects, drifting locators, rate limits, committed auth state

*Severity:* **medium** · *Stacks:* playwright

**Symptom.** A spec called `request.options()`, which does not exist. Trailing-slash paths returned 307 instead of the expected status. `xpath=ancestor::div[1]` locators drifted silently. Parallel workers tripped login rate limits.

**Root cause.** Assumptions about the API and the app that nobody verified; shared credentials across workers. A spec that has never been executed is not a test.

**Resolution.** Use `request.fetch({method:'OPTIONS'})`, canonical paths, role/test-id locators, and log in once in global setup (per-worker users where needed). Gitignore `playwright/.auth`, because storageState files hold live cookies.

**Prevention.** Prefer web-first assertions over `waitForTimeout`.

**How it is checked.**

- `regex`: `xpath=ancestor::|request\.options\(|waitForTimeout|page\.\$\(`

**Evidence.** e7f266b39; 7c8e9c45a (2026-06-28)

**Sources.** <https://playwright.dev/docs/best-practices> <https://playwright.dev/docs/auth>

## FT-012 — Playwright is installed in a different package from the app

*Severity:* **medium** · *Stacks:* playwright, npm, yarn

**Symptom.** `npx playwright test --list` failed with a missing module on a freshly provisioned machine.

**Root cause.** Playwright lives in the root `package.json` (npm), while the app lives in `frontend/` (yarn). The installer only ran `yarn install` in `frontend/`. Bumping Playwright without `playwright install` leaves a browser-build mismatch and a silent hang.

**Resolution.** Documented `npm ci && npx playwright install chromium` at the root.

**Prevention.** Provisioning scripts install every package root, and run a post-install `playwright install`.

**How it is checked.**

- `shell`: `every directory with a package.json that CI uses is installed by the provisioning script`

**Evidence.** CLAUDE.md §E2E
