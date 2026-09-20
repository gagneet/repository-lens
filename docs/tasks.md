# Open tasks: stack-depth audit follow-ups

This list tracks every known open issue and piece of pending work after the stack-depth
audit of 2026-09-13 (merged as PR #16) and the field evaluation of 2026-09-14. Each task records the
analysis behind it (what was observed, with the probe input and output), what resolving it
requires, why it matters, and how to verify it.

The numbered sections list work that is not finished. When a task is done, move it to
[Resolved](#resolved) with the verifying test, and record user-visible changes in
`CHANGELOG.md`. Capabilities that
are wanted but not planned for this branch belong in [`roadmap.md`](roadmap.md), not here.

**Status:**

| Status | Meaning |
|---|---|
| `open` | Not started. |
| `in progress` | A fix is being written. The task is not done until its tests pass and the final verification (REL-01) confirms it. |
| `deferred` | Understood and recorded, deliberately not in this branch. |
| `decision` | Needs a decision from the maintainer before work can start. |

**Severity:**

| Severity | Meaning |
|---|---|
| high | A false result that hides risk, a crash that loses facts, or unbounded work. |
| medium | A false warning or false link in a realistic layout. |
| low | A miss, an inaccuracy, or a clean-up. |

**Source of the evidence.** The analysis comes from adversarial audits. Each audit built
small temporary repositories (probes) that reproduce a behaviour and ran the scanner on them.
Performance figures come from profiling a full `repolens analyze` of a large Python and
Next.js monorepo, with about 2,400 Python files and 930 JS/TS files, checked out read-only.
The evaluation targets are deliberately not named.

## Index

49 open tasks on 2026-09-14: 3 decision, 41 open, 2 in progress, 3 deferred. 84 items are in the Resolved table.

| ID | Task | Status | Severity |
|---|---|---|---|
| [REL-01](#rel-01--final-verification-pass) | Final verification pass | in progress | high |
| [REL-04](#rel-04--docsimpactmd-does-not-describe-the-new-diagnostics) | `docs/impact.md` does not describe the new diagnostics | open | low |
| [SQL-13](#sql-13--strict-package-scoping-loses-recall-for-a-root-level-schema) | Strict package scoping loses recall for a root-level schema | deferred | low |
| [SQL-15](#sql-15--all-lowercase-prose-still-passes-the-sql-gate) | All-lowercase prose still passes the SQL gate | open | low |
| [SQL-16](#sql-16--mongodb-handles-passed-around-without-evidence-are-missed) | MongoDB handles passed around without evidence are missed | open | low–medium |
| [SQL-17](#sql-17--collection-receivers-are-re-read-from-source-lines) | `collection()` receivers are re-read from source lines | open | low |
| [SQL-18](#sql-18--two-diverging-lists-of-test-directories) | Two diverging lists of test directories | in progress | low |
| [SQL-19](#sql-19--the-foreign-ddl-scan-re-walks-the-tree-and-can-disable-catalogs-silently) | The foreign-DDL scan re-walks the tree and can disable catalogs silently | open | low |
| [SQL-20](#sql-20--a-search_path-change-hides-every-unqualified-reference-in-its-file) | A `search_path` change hides every unqualified reference in its file | open | low |
| [JS-10](#js-10--hook-results-assumed-to-be-http-clients) | Hook results assumed to be HTTP clients | decision | medium |
| [JS-14](#js-14--params-counted-as-request-input-in-any-default-export) | `params` counted as request input in any default export | open | low |
| [JS-15](#js-15--axiosurl-call-form-not-recognised) | `axios(url)` call form not recognised | open | low |
| [JS-25](#js-25--two-sql-rebuilding-shapes-are-still-quadratic) | Two SQL-rebuilding shapes are still quadratic | open | low–medium |
| [JS-27](#js-27--unmodelled-route-downgrades-are-repository-wide) | Unmodelled-route downgrades are repository-wide | open | low–medium |
| [JS-28](#js-28--poolqueryformat-s-x-is-not-seen-as-sql) | `pool.query(format('… %s', x))` is not seen as SQL | open | low |
| [JS-31](#js-31--defines-misses-object-literal-members-and-wrapped-callbacks) | `DEFINES` misses object-literal members and wrapped callbacks | open | low |
| [JS-21](#js-21--known-misses-nuxt-angular-private-fields-hono-base-paths) | Known misses: Nuxt, Angular private fields, Hono base paths | deferred | low |
| [JS-22](#js-22--grammar-gaps-make-an-analysis-incomplete) | Grammar gaps make an analysis incomplete | deferred | medium |
| [JS-23](#js-23--claimed-behaviour-without-tests) | Claimed behaviour without tests | open | low |
| [DEP-16](#dep-16--unresolved-apps-in-auxiliary-files-block-the-demotion) | Unresolved apps in auxiliary files block the demotion | open | low–medium |
| [DEP-17](#dep-17--a-development-compose-override-hides-the-dockerfiles-command) | A development compose override hides the Dockerfile's command | open | low |
| [DEP-18](#dep-18--one-run-time-import-anywhere-switches-reachability-off) | One run-time import anywhere switches reachability off | open | low |
| [DEP-19](#dep-19--ci-and-container-publishing) | CI and container publishing | open | low |
| [OUT-02](#out-02--grammar-versions-and-a-reproduction-procedure) | Grammar versions and a reproduction procedure | open | low |
| [OUT-05](#out-05--git-timeouts-in-the-artefact-commands) | Git timeouts in the artefact commands | open | low |
| [OUT-06](#out-06--pre-existing-lint-findings) | Pre-existing lint findings | open | low |
| [OUT-07](#out-07--deployment-manifests-are-read-even-when-git-ignores-them) | Deployment manifests are read even when git ignores them | open | low |
| [OUT-08](#out-08--should-unreadable-test-files-also-leave-the-analysis-complete) | Should unreadable test files also leave the analysis complete? | decision | low |
| [OUT-09](#out-09--report-summary-overview-map-and-output-size-on-large-repositories) | Report summary, overview map and output size on large repositories | open | medium |
| [OUT-10](#out-10--repolens-report-outputs-carry-no-configuration-hash) | `repolens report` outputs carry no configuration hash | open | low |
| [PERF-02](#perf-02--wiringroute_exposure-is-computed-twice) | `wiring.route_exposure` is computed twice | open | low |
| [PERF-03](#perf-03--schema-reference-string-scan) | Schema-reference string scan | open | low |
| [PERF-04](#perf-04--function-body-fingerprints) | Function-body fingerprints | open | low |
| [PERF-05](#perf-05--serialising-the-analysis-several-times) | Serialising the analysis several times | open | low |
| [PERF-06](#perf-06--python-files-parsed-twice) | Python files parsed twice | open | low |
| [DOC-02](#doc-02--a-dated-audit-document-for-this-pass) | A dated audit document for this pass | open | low |
| [DOC-03](#doc-03--verify-the-skipped-test-statement-in-claudemd) | Verify the skipped-test statement in CLAUDE.md | open | low |
| [DOC-06](#doc-06--generated-openapi-has-no-request-or-response-shapes-for-jsts-routes) | Generated OpenAPI has no request or response shapes for JS/TS routes | open | medium |
| [DOC-07](#doc-07--the-generated-er-diagram-leaves-out-orm-declared-columns) | The generated ER diagram leaves out ORM-declared columns | open | low |
| [DOC-08](#doc-08--generated-openapi-hides-any-method-routes-and-the-er-diagram-keeps-dropped-constraints) | Generated OpenAPI hides any-method routes, and the ER diagram keeps dropped constraints | open | low–medium |
| [TEST-01](#test-01--wall-clock-limits-in-tests-can-flake-under-load) | Wall-clock limits in tests can flake under load | open | low |
| [PY-01](#py-01--any-file-or-folder-named-like-a-package-makes-its-imports-local) | Any file or folder named like a package makes its imports local | open | low–medium |
| [FT-01](#ft-01--featuretrace-maps-match-basenames-by-substring) | FeatureTrace maps match basenames by substring | open | medium |
| [FT-02](#ft-02--an-untracked-related-target-is-called-nonexistent) | An untracked `Related:` target is called nonexistent | open | low |
| [FT-03](#ft-03--cross-tag-related-references-are-listed-but-never-drawn) | Cross-tag `Related:` references are listed but never drawn | decision | low |
| [FT-05](#ft-05--proposed-markers-skip-scope-qualifiers-cjs-and-schema-files) | Proposed markers skip scope qualifiers, `.cjs` and schema files | open | low |
| [FT-06](#ft-06--code-a-page-calls-directly-belongs-to-no-feature-group) | Code a page calls directly belongs to no feature group | open | low–medium |
| [REL-08](#rel-08--continuous-integration-for-this-repository) | Continuous integration for this repository | open | medium |
| [REL-09](#rel-09--repolens-vendor-verify) | `repolens vendor verify` | open | low |
| [REL-10](#rel-10--released-tags-do-not-carry-their-own-version) | Released tags do not carry their own version | open | medium |

---

## Field evaluation, 2026-09-14

A full `repolens analyze` of the large read-only evaluation checkout (6,934 tracked files, 2,417
Python and 929 JS/TS, a Next.js frontend calling a FastAPI backend, PostgreSQL and MongoDB), with every
extra installed and writing only to a scratch directory, before and after the fixes below. The
target's `git status` was the same before and after each run. Every run was incomplete for the
same real syntax error in an archived script (`PYTHON_PARSE_ERROR`).

| Measure | Before (master `a7638f7`) | After |
|---|---|---|
| Run time, peak memory | 2 min, 1.6 GB | 2 min, 1.6 GB |
| `analysis.json` / `report.md` | 130 MB / 3.5 MB | 130 MB / 3.5 MB (OUT-09) |
| PostgreSQL tables (regex- or artifact-only) | 270 (56) | 221 (8) |
| Most handlers one call was matched to | 782 | 10 |
| `CALLS_API` edges | 1,084 | 998 |
| `API_CALL_WITHOUT_HANDLER` | 0 | 0 |
| Findings (P2 + P3) | 1,767 (153 + 1,614) | 1,710 (153 + 1,557) |
| `API_HANDLER_WITHOUT_STATIC_CALLER` | 1,969 | 1,989 |

What the run showed, and the task each observation maps to:
- A request wrapper `` `${origin}/api${url}` `` matched every one of 782 handlers under `/api`,
  and edges went to an arbitrary 12 of them (JS-24).
- 864 of 1,084 calls (865 of 998 after) came from a client `api` the scanner did not trace, and
  all of them took their base URL from the single-base fallback; 815 of the 865 are in files that
  take `api` from a hook (JS-08, JS-10).
- A vendored copy of an analysis tool, tests included, is scanned as target code. Its SQL test
  fixtures created 11 tables such as `schema.mv` and `schema.v` (SQL-21).
- 38 tables came only from a generated artifact's list of unvalidated names (`schema.view`,
  `schema.fact_`, `schema.generate`) (SQL-22).
- 403 `performance/unbounded-sql-fetch` findings; 57 were grouped aggregates, which SCAN-01 no
  longer reports (403 → 346, the whole P3 drop; the JS/TS fixes changed no finding count).
- 17,926 of 20,554 diagnostics are info-level `AMBIGUOUS_CALL`, and the whole graph is written
  to `analysis.json` (OUT-09).
- Not exercised: no JS/TS code splices values into SQL (`SQL_INJECTION_RISK` 0 in both runs), so
  JS-25/JS-28 found nothing; there is no Prisma schema and only two tracked `.sql` files, so
  `SQL_UNKNOWN_COLUMN` had almost no catalog to check against.

`repolens docs generate` on the same checkout, writing only to a scratch directory; the target's
`git status` was unchanged after every run, and every run was incomplete for the same parse error.
The final run is of the code in this change (DOC-05).

| Measure | First draft | Final |
|---|---|---|
| OpenAPI operations | 1,586 (scanned handlers only) | 2,693 (1,501 scanned, 1,107 artifact-declared, 85 both); 0 errors from `openapi-spec-validator` |
| Unserved calls listed | 1,123 (artifact routes and matched placeholders counted as unserved) | 0 (the one request wrapper too open to link is now "open") |
| Stores per operation, median / p90 / max | 9 / 15 / 36 (probable and ambiguous calls followed) | 3 / 7 / 17 (exact, high and declared only) |
| ER diagram | 150 entities, 0 relationships (the cap spent on MongoDB collections) | 150 entities, 44 relationships; 382 stores listed but not drawn (311 MongoDB, 71 PostgreSQL) |
| Architecture map | parsed by Mermaid | 500 edges hit Mermaid's limit and failed to parse; capped at 450, 118 edges counted in a comment |
| Feature groups / largest mindmap | 496 / 31,459 characters | 496 / 9,012 characters (25 leaves per section) |
| Diagrams parsed by Mermaid 11 | not checked | 500 of 500 |
| Run time, peak memory | 285.6 s, 1.3 GB | 116 s, 1.3 GB |

`repolens featuretrace propose --jsdoc --owners` on the same checkout, drafting only (no `--apply`),
writing to a scratch directory; the target's `git status` was unchanged. The final run is of the
code in this change (FT-04).

| Measure | First draft | Final |
|---|---|---|
| Markers proposed | 272 | 371 (frontend 277, router 61, service 28, model 5) |
| Stores per marker, median / p90 / max | 14 / 48 / 83 (the whole group's stores) | 0 / 7 / 16 (the file's own) |
| `Related:` entries | always 8 | at most 8; 169 markers say `Related: none found (draft)` |
| Files left alone | 477, 114 of them wrongly as generated (`* @generated FunctionHeader`) | 289: 288 already carry a marker, 1 outside `[featuretrace] scan_dirs` |
| Routes with no static caller | described as called by "HTTP client" | 32, saying "no static caller found" |
| Predicted audit issues | not recorded | 367 drafts lack the scope qualifier this repository's audit requires (FT-05); 1 names a value that is not a store identifier |
| JSDoc blocks | 0 | 0 (no exported JS/TS route handler or data-access function without one) |
| Owners draft | 1.8 MB | 0.5 MB (at most 10 consumers per concept) |
| Exit code, run time, peak memory | 0, 139 s | 2 (incomplete analysis; proposal still written), 120 s, 1.3 GB |

---

## 1. Release blockers for this branch

### REL-01 — Final verification pass
- **Status:** in progress · **Severity:** high
- **Analysis:** the fixes from the first audits, and from the second audits of those fixes,
  have landed. The combined tree must be checked as a whole, not module by module.
- **Required, with the results of 2026-09-14:**
  1. Full suite with extras (`python -m unittest discover -s tests -q`): 505 tests, OK,
     1 skipped.
  2. The same in a bare virtualenv, to confirm every test needing `tree_sitter`, `sqlglot`,
     `fastapi` or `httpx` carries a `skipUnless` guard: **not yet run** (also DOC-03).
  3. `python -m compileall -q repolens tests`: clean.
  4. `ruff check --select F,E9,B` against `master`: master 20 findings, branch 12, none new
     (the remaining ones are listed in OUT-06).
  5. `repolens analyze` on the large read-only evaluation checkout, writing only to a scratch
     directory: 119 s; the target's `git status` was identical before and after (the only
     newer file was the target application's own log). `complete=false` because of one real
     syntax error in an archived script (`PYTHON_PARSE_ERROR`). Against the previous run:
     the one application `UNRESOLVED_ROUTER_MOUNT` is gone (the aliased-router fix; the other
     5 are in test code and do not count), and the one `API_METHOD_MISMATCH` is gone because
     the target's owner changed that call to a method the route serves between the runs.
     No route file was demoted to `undeployed`.
  6. The sanitized copy of the private evaluation repository: 8 s, `complete=true`.
  7. Self-scan of this repository: 7 s, `complete=true`.
  8. Name sweep of every tracked and untracked file for evaluation-target names and domain
     identifiers: clean.
- **Why:** the branch changes what the analyzer reports and how confident it claims to be.
  The rule "a skipped or failed input never looks complete" only holds if the combined tree
  is checked.
- **Done when:** step 2 has run, and the results are in the PR description.

### REL-04 — `docs/impact.md` does not describe the new diagnostics
- **Status:** open · **Severity:** low
- **Analysis:** CHANGELOG, README, CLAUDE.md, `docs/roadmap.md` and `analysis.LIMITS` were
  re-read against the final code and corrected (deployment fail-closed rules, the SQL column
  check scope, the dialect skip, the MongoDB handle rule, the unmodelled-route downgrade and
  the SQL sanitisers). `docs/impact.md` still does not mention `SQL_UNKNOWN_COLUMN`,
  `SQL_DIALECT_NOT_POSTGRES`, `API_METHOD_MISMATCH` or the MongoDB handle rule.
- **Required:** add a short section for each to `docs/impact.md`, linking to `LIMITS` rather
  than repeating it.
- **Why:** `docs/impact.md` is the reference for the graph's diagnostics.

---

## 2. PostgreSQL and data layer

Every SQL task comes from the SQL/data-layer audit. SQL-01 to SQL-12 and SQL-14 are in the
Resolved table. A second audit of those fixes (2026-09-14) found a quadratic MongoDB handle
check, a completeness hole for psql statements, T-SQL scripts reported as parse errors, and
several over-broad `search_path`/foreign-DDL/prose rules (A-SQL-1 to A-SQL-13). All of them
are fixed with tests except the parts kept below. `tests.test_sql`, `tests.test_columns`,
`tests.test_postgres` and `tests.test_schema_references` pass.

### SQL-13 — Strict package scoping loses recall for a root-level schema
- **Status:** deferred · **Severity:** low
- **Analysis:** a Prisma schema or `CREATE TABLE` catalog checks only queries in its own
  package. A root schema does not check a nested package that has its own `package.json`.
  This trades recall for precision on purpose.
- **Required:** document it in `analysis.LIMITS`. Optionally add an `[impact]` setting that
  maps packages to a shared schema.
- **Why:** users should know when a query is not checked, not assume it is.

### SQL-15 — All-lowercase prose still passes the SQL gate
- **Status:** open · **Severity:** low
- **Analysis:** a statement keyword counts only when it is all upper or all lower case,
  unless other SQL punctuation is present, which rejects "Delete from history". But
  all-lowercase UI copy such as "delete from history" or "select name from list" is still
  accepted as SQL, and can create a store edge for a table named `history` or `list`.
- **Required:** reject gated text that has no SQL punctuation (`;`, `(`, `=`, `*`, `,`, a
  quote or a placeholder), unless the named table is known from DDL, a Prisma schema or an ORM
  model. Test with lowercase UI strings, and with genuine short lowercase queries such as
  `select * from users` and `delete from sessions where id = $1`, which must still pass.
- **Why:** i18n catalogs and UI copy are full of short lowercase phrases. A false store edge
  links unrelated code to a table in impact queries.

### SQL-16 — MongoDB handles passed around without evidence are missed
- **Status:** open · **Severity:** low–medium
- **Analysis:** to stop false collections (SQL-02), a `db` bound as an untyped parameter, or
  with a non-driver type, is not a handle even in a file that imports the driver. So
  `constructor(private db: any)`, plain-JS `function save(db) { db.users.insertOne(x) }`,
  `(await getDb()).collection('orders')` and a `req.app.locals.db` set in another file give
  no collection. This is a deliberate recall loss, now listed in `LIMITS`.
- **Required:** follow same-file call sites where cheap (a parameter whose every caller
  passes a known handle counts) and functions whose return value is `<client>.db(...)`.
  Otherwise add an `[impact] mongo_handles` setting (receiver names to trust) like
  `client_receivers`.
- **Why:** plain-JS services pass the database handle around untyped. Without a way to
  recover these, impact queries for a collection miss its writers.

### SQL-17 — `collection()` receivers are re-read from source lines
- **Status:** open · **Severity:** low
- **Analysis:** `JSFacts` does not keep the receiver of `db.collection('x')`, so the scanner
  reads it back from the source line. A call split across lines (`db\n.collection('orders')`,
  `this.db\n.collection(...)`) records no collection. Comments, `db = null` and several
  receivers on one line are handled since the second audit.
- **Required:** record the receiver expression, and its binding where known, on the
  collection fact in `core/javascript.py`. Then drop the line re-read, and test multi-line
  and member-chain receivers.
- **Why:** the syntax path exists precisely so that decisions do not depend on line text.

### SQL-18 — Two diverging lists of test directories
- **Status:** in progress · **Severity:** low
- **Analysis:** `core.files.is_test_path` (precision choices) and `is_test_code` (the
  completeness exemption) are shared. `bootstrap._TEST_DIRS` (`tests`, `test`, `__tests__`,
  `spec`, `specs`) is still a separate list, and JS liveness does not use either.
- **Required:** use the shared helpers in bootstrap (which picks the tests directory to
  configure, a narrower question) and in the JS rules, with a table test.
- **Why:** one concept with several definitions drifts, and each drift is a class of false
  warning in one checker but not another.

### SQL-19 — The foreign-DDL scan re-walks the tree and can disable catalogs silently
- **Status:** open · **Severity:** low
- **Analysis:** `columns._foreign_ddl_tables` walks the repository again (with a second git
  ignore listing) instead of reusing the admitted inventory. Files past `max_files` are
  skipped without a note. When it disables the DDL catalogs (an unreadable or over-size
  migration, a computed `ALTER TABLE` target), no diagnostic says so, so the column check
  goes quiet with no visible cause.
- **Required:** reuse `ScanState.admitted_paths`, and report an info diagnostic naming the
  file whenever DDL catalogs are disabled or the walk is truncated.
- **Why:** a check that silently stops checking looks like a clean result.

### SQL-20 — A `search_path` change hides every unqualified reference in its file
- **Status:** open · **Severity:** low
- **Analysis:** DDL is now positional (a `CREATE TABLE` before the file's `SET search_path`
  still declares), but references are not: every unqualified reference in a file whose SQL
  sets `search_path` is unchecked, including those before the `SET`.
- **Required:** check references that precede the first `SET search_path` in the same file,
  against the catalog as it stood at that point.
- **Why:** recall. Migration-style scripts often set `search_path` halfway through.

---

## 3. JavaScript / TypeScript

Every JS task comes from the JS/TS audit. JS-01 to JS-05 and JS-20 are in the Resolved table.
A second audit of those fixes (2026-09-14) found SQL dropped from long `+` chains, remaining
exponential SQL rebuilding, HTML escapers accepted as SQL sanitisers, and ordinary code
(test mocks, `new Map().get`) hiding warnings repository-wide (A-JS-1 to A-JS-17). Those
are fixed with tests except where JS-25 to JS-28 below say otherwise. The field evaluation of
2026-09-14 led to JS-06 to JS-09, JS-11 to JS-13, JS-16 to JS-19, JS-24 and JS-26 being fixed
(see Resolved); JS-10 now needs a decision.

### JS-10 — Hook results assumed to be HTTP clients
- **Status:** decision · **Severity:** medium
- **Field evidence (2026-09-14):** on the evaluation repository 865 of 998 linked calls go
  through a client the scanner did not trace, mostly `const { api } = useAuth()` from a React
  context. The fix as written below would drop every one of those links unless the repository
  sets `client_receivers = ["api"]`. The choice is between (a) that fix, with the setting
  documented as required for the context pattern; (b) tracing a `useX` hook to a context
  provider whose value is built by a client factory, and keeping the current behaviour
  otherwise; or (c) (b), and dropping untraced hook results only when no client factory exists
  in the caller's package.
- **Analysis:**
  - `usePageTitles().get('/settings')` produces a `CALLS_API` edge and a warning.
  - `useSearch().get(k)` and `useCartStore().delete(k)` produce `DYNAMIC_HTTP_REQUEST` noise.
  - The exclusion list is hand-picked names.
- **Required:** treat a hook result as a client only when the hook traces to a client factory,
  or when it is listed in `client_receivers`. Unresolved hook receivers emit nothing.
- **Why:** framework semantics, not name lists, keep the rule valid for any codebase.

### JS-14 — `params` counted as request input in any default export
- **Status:** open · **Severity:** low
- **Analysis:** `export default async function buildReport(db, { params })` produces
  `SQL_INJECTION_RISK`.
- **Required:** only routed files (Next route/page, `+server`, Remix `app/routes`, SvelteKit)
  supply request `params`. Document the exact recognised shapes in `LIMITS`.
- **Why:** precision of a security finding.

### JS-15 — `axios(url)` call form not recognised
- **Status:** open · **Severity:** low
- **Analysis:** `axios('/api/orders')` and `axios('/api/orders', {method:'post'})` produce no
  request, plus a false "requires runtime values" diagnostic.
- **Required:** support a string first argument with an optional config object.
- **Why:** it is a documented axios form.

### JS-25 — Two SQL-rebuilding shapes are still quadratic
- **Status:** open · **Severity:** low–medium
- **Analysis:** the second JS audit's fixes made reused bindings, `sql = sql + …` chains and
  sibling blocks linear. Two shapes remain quadratic:
  - one shared `let q` assigned in N callbacks (4,000 took 20 s);
  - interleaved `sql += …; await pool.query(sql)`, where each query's text grows
    (n = 2,000 took 19.6 s).
- **Required:** look up assignments per declaration instead of slicing every assignment after
  the declaring scope's start, and share the rebuilt prefix between successive queries of the
  same binding. Add timing tests with generous limits (TEST-01).
- **Why:** generated test suites and migration scripts have exactly these shapes.

### JS-27 — Unmodelled-route downgrades are repository-wide
- **Status:** open · **Severity:** low–medium
- **Analysis:** one unmodelled registration anywhere outside test paths lowers every
  `API_CALL_WITHOUT_HANDLER` and `API_METHOD_MISMATCH` in the repository to info, even in a
  package that has nothing to do with it. That includes a mismatch on a path a modelled
  handler serves, which can remove the only live `stack/api-method-mismatch` finding.
- **Required:** scope the downgrade to the caller's nearest package (or its configured backend
  package). Keep a mismatch a warning unless an unmodelled registration's path could match
  the called path.
- **Why:** a monorepo with one legacy router should not silence every gap in every app.

### JS-28 — `pool.query(format('… %s', x))` is not seen as SQL
- **Status:** open · **Severity:** low
- **Analysis:** a query built by a formatting call passed straight to the driver is not rebuilt,
  so neither a table edge nor an injection risk is recorded for it. pg-format `%I`/`%L` are
  safe, but `%s` and `util.format` splice text.
- **Required:** rebuild `format(literal, …)` arguments the way template literals are rebuilt,
  with `%s` as a spliced value and `%I`/`%L` as quoted parameters; test both.
- **Why:** a missed injection shape in a security check.

### JS-31 — `DEFINES` misses object-literal members and wrapped callbacks
- **Status:** open · **Severity:** low
- **Analysis:** found by the audit of JS-30. A handler object defined inside a component
  (`const handlers = { go: () => fetch("/api/go") }`, qualified `Page.handlers.go`) gets no
  `DEFINES` edge, because `Page.handlers` is not a symbol, so feature groups do not see that
  page's call. `onClick={handle(() => save())}` records the role `handle` (the call that
  receives the arrow) instead of the JSX attribute `onClick`.
- **Required fix:** link a nested function to the nearest enclosing symbol by walking up its
  qualified name, and prefer an enclosing `jsx_attribute` name over a wrapping call's.
- **Why:** the page-to-API calls feature groups report miss handlers written this way.

### JS-21 — Known misses: Nuxt, Angular private fields, Hono base paths
- **Status:** deferred · **Severity:** low
- **Analysis:** these are misses only; no false results were observed.
  - Nuxt `this.$axios.get`.
  - Angular `#http = inject(HttpClient)`, and `constructor(http) { this.http = http }`.
  - `new Hono().basePath('/api')` chains.
- **Required:** trace each shape to its client or router, with tests.
- **Why:** coverage for common frameworks. Out of scope for this branch.

### JS-22 — Grammar gaps make an analysis incomplete
- **Status:** deferred · **Severity:** medium
- **Analysis:** `export type * from '…'` and `interface I<in out T>` produce
  `JAVASCRIPT_PARSE_ERROR`, which is in the incomplete set. The installed tree-sitter
  TypeScript grammar does not support these newer TypeScript syntax forms.
- **Required:** upgrade the grammar, or add a retry that strips the unsupported syntax, as is
  already done for `import type`. Record grammar versions in the build stamp (OUT-02).
- **Why:** valid TypeScript should not make an analysis incomplete.

### JS-23 — Claimed behaviour without tests
- **Status:** open · **Severity:** low
- **Analysis:** these have no test:
  - `.svelte`, `.astro` and `.mdx` disabling dead-code judgements (only `.vue` is tested);
  - Vue `inject`;
  - Remix `clientLoader`;
  - taint performance;
  - the route-name heuristics.
- **Required:** add one regression test per claim.
- **Why:** project rule: a documented capability needs an implementation and a regression
  test.

---

## 4. Deployment-aware exposure

Every DEP task comes from the deployment audit, which rated the original feature **3/10**.
Deployment detection *lowers* finding priority ("undeployed"), so a wrong demotion hides risk.
The first audit found at least 25 realistic layouts where a running application was
demoted, because the design *failed open*.

**State (2026-09-14):** the fail-closed rewrite is finished. DEP-01 to DEP-15 are in the
Resolved table. A second audit of the rewrite ran 60 earlier probes plus new ones and found 14
more issues (A-DEP-1 to A-DEP-14). A-DEP-1 to A-DEP-12 are fixed. Every probe layout is a
table row in `tests/test_deploy_probes.py` `DeploymentProbeTests.test_every_probe_layout`
(107 rows), and there are separate tests for run-time router discovery, reads outside the
root, linear time on long lines, and seeded fuzzing. What is still open is listed below; all
of it errs toward *blocking* a demotion, which loses precision but never hides risk.

The probe setup: two FastAPI apps with an unauthenticated `POST` (`app/main.py`,
`legacy/server.py`), usually a Procfile running `app.main:app`, plus the manifest under test.
A correct result demotes only what nothing deploys.

### DEP-16 — Unresolved apps in auxiliary files block the demotion
- **Status:** open · **Severity:** low–medium
- **Analysis:** auxiliary files (dev scripts, tests, CI, `--reload` servers) never make a
  deployment known, but an application name in one of them that does not resolve still
  blocks. Examples: `scripts/dev.sh` running `uvicorn main:app --reload` in a repository with
  two `main.py`, or `tests/run_e2e.sh` running a module that does not exist. Either one
  switches the demotion off for the whole repository.
- **Required:** in auxiliary files, mark every candidate of an ambiguous name live instead of
  blocking, and ignore a module that does not exist. Keep blocking for names chosen at run
  time (`uvicorn $APP_MODULE`), because the unknown app could be any file.
- **Why:** usefulness. The feature stays safe but turns itself off in common layouts.

### DEP-17 — A development compose override hides the Dockerfile's command
- **Status:** open · **Severity:** low
- **Analysis:** a Dockerfile that a compose service builds is read only through that service.
  When the service overrides the command with a `--reload` (auxiliary) command, the image's
  own `CMD` is never recorded as a deployment, so nothing is known and nothing is demoted.
- **Required:** record the Dockerfile's own command as well when the building service
  overrides it.
- **Why:** precision in the common "production Dockerfile plus dev compose" layout.

### DEP-18 — One run-time import anywhere switches reachability off
- **Status:** open · **Severity:** low
- **Analysis:** to fix DEP-09 safely, any reachable file that imports modules chosen at run
  time (`pkgutil.iter_modules`, `walk_packages`, `runpy`, `spec_from_file_location`, a
  non-constant `import_module`/`__import__`) now makes reachability unknown for the whole
  repository. Nothing is then marked `unreachable` or `undeployed`.
- **Required:** a narrower rule. Mark every file under the importing module's package, or under
  the package a constant `iter_modules(x.__path__)` names, as live, and keep the rest of the
  analysis. Add a probe that shows an unrelated `legacy/` is still demoted.
- **Why:** plugin-style routers are common, and the broad rule removes the feature for those
  repositories.

### DEP-19 — CI and container publishing
- **Status:** open · **Severity:** low
- **Analysis:**
  - A CI workflow that pushes an image (`docker push`) blocks by design, because the image
    may run elsewhere with another command. This switches the feature off for every
    repository that publishes images, and it is not documented.
  - A GitHub Actions step running `docker run … uvicorn …` (a self-hosted runner deploying
    in place) is not a CI deploy command, so it does not block.
- **Required:** document the push rule in `LIMITS` and the README. Treat `docker run` with a
  server command in CI as a blocking deploy line, and add probes for both.
- **Why:** accurate documentation, and one remaining fail-open shape.

---

## 5. Outputs, provenance and completeness

### OUT-02 — Grammar versions and a reproduction procedure
- **Status:** open · **Severity:** low
- **Analysis:** `tool_build.extras` records tree-sitter, sqlglot, fastapi and pdoc versions,
  but not the tree-sitter JavaScript and TypeScript grammar versions. No document explains how
  to reproduce a report from its stamp.
- **Required:** add the grammar distributions to `_EXTRAS`, and document reproduction:
  - check out `commit`;
  - compare `source_sha256`;
  - install matching extras;
  - compare `config_sha256`.
- **Why:** extraction output depends on the grammar version (see JS-22). This is roadmap P0
  item 3.

### OUT-05 — Git timeouts in the artefact commands
- **Status:** open · **Severity:** low
- **Analysis:** git calls in `artefacts/` now have timeouts; `affected_paths` has 600 s. A
  timeout raises `SubprocessError`. Inside the installed hooks, `|| true` swallows it, so a hook
  never breaks the git operation. The interactive `repolens artefacts` commands would show a
  traceback instead of a message.
- **Required:** catch timeout errors in the CLI entry points and print a clear message with a
  non-zero exit.
- **Why:** actionable errors instead of stack traces.

### OUT-06 — Pre-existing lint findings
- **Status:** open · **Severity:** low
- **Analysis:** `ruff check --select F,E9,B` still reports findings that exist on `master`,
  including:
  - `B904` and `B905` in `lens/build.py`;
  - `B023` in `report/runner.py` (a closure over a loop variable);
  - `B008` for FastAPI `Depends` defaults, which is idiomatic.

  `pyproject.toml` has no lint configuration.
- **Required:**
  - Fix `B023` (a latent bug class) and `B904`.
  - Add a `[tool.ruff]` section that ignores `B008` for FastAPI.
  - Optionally run ruff in CI.
- **Why:** it keeps new warnings visible.

### OUT-07 — Deployment manifests are read even when git ignores them
- **Status:** open · **Severity:** low
- **Analysis:** after OUT-03, the Python checks skip untracked gitignored files, but
  `scan/deploy.py` finds manifests with its own `os.walk` (two sites). A gitignored
  `docker-compose.override.yml` or a local `.env`-generated unit file is still read as
  deployment evidence. After the fail-closed rewrite that can only block a demotion, never
  cause a wrong one, so the effect is lost precision, not hidden risk.
- **Required:** once DEP-14/DEP-15 are done, filter the manifest walk through
  `python_ast.not_gitignored` (or `core.git.under_ignored` with the run's cached listing).
  Test with an ignored override file.
- **Why:** one `respect_gitignore` switch should mean the same thing for every reader.

### OUT-08 — Should unreadable test files also leave the analysis complete?
- **Status:** decision · **Severity:** low
- **Analysis:** OUT-04 exempted link diagnostics in test code from completeness. A test file
  that fails to parse (`PYTHON_PARSE_ERROR`, `JAVASCRIPT_PARSE_ERROR`) or is skipped
  (`FILE_SKIPPED`) still makes the analysis incomplete, because its facts are lost: the test →
  code edges used by impact queries, and any SQL or requests in it. On the large evaluation
  repository no parse error was in test code, so this has not come up in practice yet.
- **Required:** the maintainer decides. If test files should be exempt too, add those codes to
  the same `_in_test_code` rule and list the exemption in `LIMITS` ("impact queries may miss
  tests that could not be read").
- **Why:** it is a trade-off between an honest completeness flag and a flag that turns red for
  code that is never deployed.

### OUT-09 — Report summary, overview map and output size on large repositories
- **Status:** open · **Severity:** medium
- **Analysis:** on a graph of about 60,000 nodes, `analyze` wrote a 132 MB `analysis.json`
  and a 24,000-line `report.md` (GitHub issue #8). `report.md` now opens with completeness,
  the incomplete reasons, the build stamp and total counts. It still has no per-code count
  table and no top findings, and it lists every finding and diagnostic in full. The default
  map is a node list ranked by kind and degree, not route → handler → store chains;
  regex-only stores were removed from it. `analysis.json` holds the whole graph with no bound.
- **Required:**
  - Open `report.md` with a per-code count table and the top P0/P1 findings, and cap rows per
    code as `report.html` does, pointing to `analysis.json` for the rest.
  - Draw the default overview from the most-connected endpoint → handler → store chains.
  - Offer a way to write the graph separately or leave it out.
- **Why:** output nobody can read on a large repository is not a result.

### OUT-10 — `repolens report` outputs carry no configuration hash
- **Status:** open · **Severity:** low
- **Analysis:** `analyze` and `impact doctor` stamp `config_sha256` (GitHub issue #10), but
  `report/runner.py` writes the build stamp without a configuration hash, so two reports from
  one build with different `repolens.toml` settings look alike.
- **Required:** compute the same configuration fingerprint in the report runner and write it
  in the Markdown, HTML and SARIF (`driver.properties`) outputs, with a test.
- **Why:** configuration changes results without changing the build.

---

## 6. Performance

Figures come from profiling a full `analyze` of the large evaluation repository: 609 s
profiled, 5 min 23 s unprofiled, before the import-resolution fix.

### PERF-02 — `wiring.route_exposure` is computed twice
- **Status:** open · **Severity:** low
- **Analysis:** two calls to `route_exposure` cost 38 s in total, of which `_route_exposure`
  is 28 s. `ParseCache.derived` should make the second call free.
- **Required:** find out why the derived key differs between the security and performance
  runs (a `run_inputs` field that differs?), and share the result.
- **Why:** about 15–20 s saved on large repositories.

### PERF-03 — Schema-reference string scan
- **Status:** open · **Severity:** low
- **Analysis:** `scanner._python_strings` and `_schema_references` cost 33 s across 2,275
  Python files.
- **Required:** pre-filter each string with a cheap substring test for the configured schema
  names before running the regex.
- **Why:** the scan runs on every Python string literal.

### PERF-04 — Function-body fingerprints
- **Status:** open · **Severity:** low
- **Analysis:** `python_scan._add_definition` spends about 15–18 s in `ast.dump` producing
  structural-duplicate fingerprints for 27,681 functions.
- **Required:** skip trivial bodies, and hash a cheaper normalised token stream instead of a
  full dump.
- **Why:** measurable time for an info-level diagnostic.

### PERF-05 — Serialising the analysis several times
- **Status:** open · **Severity:** low
- **Analysis:** `json.dumps` ran 6 times for 18 s in total, and `dataclasses.asdict` also
  shows up. `Analysis.to_dict()` is probably built again for each output (JSON, Markdown, HTML,
  API).
- **Required:** build the payload once per `main` and reuse it for every output.
- **Why:** a straightforward 10–15 s saving.

### PERF-06 — Python files parsed twice
- **Status:** open · **Severity:** low
- **Analysis:** 3,252 `ast.parse` calls for about 2,275 Python files, 17 s in total. The graph
  scanner and the `scan/` checks parse independently.
- **Required:** let the graph scan populate or share the `ParseCache`, keyed the same way
  (path plus SHA-256 of the bounded read).
- **Why:** about 1,000 redundant parses on a large repository.

---

## 7. Documentation and genericity

### DOC-02 — A dated audit document for this pass
- **Status:** open · **Severity:** low
- **Analysis:** `docs/audit/2026-09-12-professional-foundation.md` is a snapshot at the 0.3.0
  merge. This branch's audit produced ratings per area:

  | Area | Rating |
  |---|---|
  | HTML escaping | 9 |
  | Git hardening, before the fixes | 9 |
  | SQL statement precision | 9 |
  | Deployment detection, before the fixes | 3 |
  | JS sanitisers, before the fixes | 4 |

  None of this is recorded in the repository.
- **Required:** after REL-01, write `docs/audit/2026-09-13-stack-depth.md` with the method,
  final per-area confidence ratings, and the residual limitations.
- **Why:** the project keeps its audited scope in `docs/audit/` beside `analysis.LIMITS`.

### DOC-03 — Verify the skipped-test statement in CLAUDE.md
- **Status:** open · **Severity:** low
- **Analysis:** `CLAUDE.md` says that without the extras "about a third of the suite skips".
  The last bare-environment measurement predates several hundred new tests.
- **Required:** measure in a bare virtualenv (part of REL-01), and adjust the wording.
- **Why:** contributors decide whether a green run is meaningful based on that sentence.

### TEST-01 — Wall-clock limits in tests can flake under load
- **Status:** open · **Severity:** low
- **Analysis:** `tests/test_javascript_graph.py` `test_a_long_plus_chain_does_not_lose_the_file`
  asserts a 10 s limit. During concurrent edits and a parallel suite run, it once took 118 s.
  That run coincided with `core/javascript.py` being rewritten mid-run, and the same scan
  profiled at 0.3 s afterwards. The JS-02 and JS-03 performance tests will need similar limits.
- **Required:** assert work that does not depend on the machine where possible, for example
  the number of `untrusted` or `url_value` calls through a counter or mock. Keep wall-clock
  assertions generous (10× the measured time), and name the measured baseline in the test.
- **Why:** a flaky test on a shared CI runner gets retried or disabled, and then the
  regression it guards returns unnoticed.

---

### DOC-06 — Generated OpenAPI has no request or response shapes for JS/TS routes
- **Status:** open · **Severity:** medium
- **Analysis:** `repolens docs generate` names declared FastAPI models and lists every other
  request body and response schema in `x-repolens-gaps`. A Next.js or Express handler that
  validates with a zod schema, types its body with a TypeScript interface, or returns
  `Response.json({ ... })` with a literal object has a shape the source states, and the
  generated contract still says it is unknown.
- **Required fix:** read literal zod schemas bound in the handler's file (`z.object({...})`
  passed to `.parse`/`.safeParse` on `request.json()`), and object literals returned through
  `Response.json`/`NextResponse.json`, into OpenAPI schemas marked with their evidence. Leave a
  gap wherever the shape comes from a type the scanner cannot resolve without a compiler.
- **Why:** request and response shapes are the part of an API contract a reader of an
  undocumented application most needs, and a gap for a shape written in the same file
  understates what static evidence can show.

### DOC-07 — The generated ER diagram leaves out ORM-declared columns
- **Status:** open · **Severity:** low
- **Analysis:** `schema.mmd` draws columns from scanned SQL DDL and PostgreSQL Prisma models.
  Tables declared through Drizzle, TypeORM, Sequelize or SQLAlchemy models appear with
  `%% columns not resolved`, although those declarations name their columns. Prisma schema
  selection in the generator also counts every non-test `.prisma` file when all providers are
  PostgreSQL, instead of the per-package rule `columns.check_columns` uses.
- **Required fix:** record declared columns (and relations) on the store facts the JS/TS and
  Python extractors already build, and draw them; reuse the column check's per-package Prisma
  selection.
- **Why:** an application built on an ORM without raw DDL gets an entity list with no columns.

### DOC-08 — Generated OpenAPI hides any-method routes, and the ER diagram keeps dropped constraints
- **Status:** open · **Severity:** low–medium
- **Analysis:** found by the audit of `docs generate`.
  - A Pages Router API route (`pages/api/items.ts`) answers every method, so `openapi.json`
    puts it under the path-item extension `x-repolens-any-method`. The document validates
    against the OpenAPI 3.1 schema, but Swagger UI, Redoc and client generators ignore `x-`
    keys, so real served routes are invisible there while `index.md` counts them.
  - `schema.mmd` applies `DROP TABLE`, `DROP COLUMN`, `RENAME` and `SET`/`DROP NOT NULL` in path
    order, but not `DROP CONSTRAINT`: a constraint's name does not say which foreign key it was.
  - `REFERENCES public.orders` creates a `public.orders` store separate from `orders`, so the
    diagram can show an orphan table.
- **Required fix:** read the handler's `req.method` checks (`if (req.method !== "POST")`,
  `switch (req.method)`) into real operations, and keep `x-repolens-any-method` only when none
  is found. Record constraint names with the foreign keys they declare so a later drop removes
  the right one. Resolve a `public.` qualifier to the bare name when no `search_path` change is
  in the scanned SQL.
- **Why:** documentation a standard viewer cannot show, or a relationship the schema no longer
  has, misleads the reader it was generated for.

## 8. Checks, FeatureTrace, Python imports and release tooling

Tasks from the verification of the open GitHub issues (#6, #8, #10, #13, #14, #15) and
the mount investigation on the large evaluation repository.

### PY-01 — Any file or folder named like a package makes its imports local
- **Status:** open · **Severity:** low–medium
- **Analysis:** `python_scan._local_python_names` collects every path segment of every
  admitted `.py` file. A top-level import whose name matches any of them anywhere
  (`from fastapi import …` next to a vendored tool's `impact/fastapi.py`, `import redis`
  next to `app/cache/redis.py`) is treated as a local resolution gap instead of an
  `external:` binding. Call resolution then name-matches its members against local
  definitions, and anything that relied on the binding loses it (the aliased-router case in
  the Resolved table was one).
- **Required:** a name is local only when it is importable as a top-level module from a
  Python root the import index knows (`python_roots`, a directory holding the importing
  file's package, or the repository root), not when it appears as any path segment. Test
  with a nested same-named module and a real local top-level package.
- **Why:** false `probable` call edges into unrelated local code, and missed facts that
  depend on the binding.

### FT-01 — FeatureTrace maps match basenames by substring
- **Status:** open · **Severity:** medium
- **Analysis:** `featuretrace/render.py` binds a data-flow step to a node when
  `Path(rel_path).name in step`, a substring test, so a step naming `documents_store.py`
  also binds `store.py`, and `data.py` binds `a.py`. `Related:` edges match on basename
  across directories (GitHub issue #15). There are no FeatureTrace tests.
- **Required:** match a whole relative path first, then a basename on path-segment and word
  boundaries; refuse an ambiguous basename with an audit note. Add render tests.
- **Why:** wrong edges in committed maps mislead the reader the map exists for.
- **Progress (2026-09-14):** the `Related:` half is fixed: an entry binds its exact path first,
  then a unique path ending in it, and an ambiguous basename binds nothing; reference paths keep
  Next.js segments such as `[id]` and `(group)`
  (`RelatedReferenceTests.test_a_related_entry_binds_its_whole_path_before_a_basename`).
  `Data flow:` steps in `render.py` still match by substring.

### FT-02 — An untracked `Related:` target is called nonexistent
- **Status:** open · **Severity:** low
- **Analysis:** `featuretrace/audit.py` `dangling_refs` resolves references against tracked
  files, so a new file that exists but is not yet added to git is reported as "references a
  path that does not exist". A gitignored path that is tracked counts as present.
- **Required:** when the path exists on disk but is untracked, say "exists but is not tracked
  by git"; test both messages.
- **Why:** the current message sends people looking for a file that is there.

### FT-03 — Cross-tag `Related:` references are listed but never drawn
- **Status:** decision · **Severity:** low
- **Analysis:** a reference to a file under another tag is counted in the audit by design,
  but the per-tag map builds `node_by_path` from that tag's nodes only, so the edge is not
  rendered and the map looks smaller than the markers say.
- **Required:** the maintainer decides between drawing external stub nodes in per-tag maps
  and emitting a distinct advisory. Then add tests.
- **Why:** a map that silently drops declared links under-reports a feature's reach.

### FT-05 — Proposed markers skip scope qualifiers, `.cjs` and schema files
- **Status:** open · **Severity:** low
- **Analysis:** `repolens featuretrace propose` drafts no scope qualifier, so in a repository
  that sets `[featuretrace.audit] scope_values` every applied marker is weak until someone adds
  one. It drafts nothing for `.cjs` files or for `.sql`/`.prisma` model files, which can carry a
  marker in a comment, and its bounds (`Related:` length, the minified-line length) are
  constants rather than `[featuretrace.propose]` settings.
- **Required fix:** draft the qualifier as a placeholder the audit still reports, add `.cjs`,
  `.sql` (`--`) and `.prisma` (`//`) comment syntax, and read the bounds from settings.
- **Why:** the proposal should pass the audit a repository has actually configured.

### FT-06 — Code a page calls directly belongs to no feature group
- **Status:** open · **Severity:** low–medium
- **Analysis:** found by the audit of `featuretrace propose`. `features.feature_groups` adds
  code reached from an endpoint's handler, and a page's component file, but not modules the
  page component itself calls: a Next.js server component or server action that imports
  `lib/orders.ts` and queries the database without an API route. Such a module is in no group,
  so it gets no marker, no JSDoc and no place in the generated feature page.
- **Required fix:** walk confident calls from each page component as the handler walk does, and
  add the reached files and stores to the page's group at the `service`/`model` layer.
- **Why:** App Router applications often read data in server components, so the most important
  data-access code of a page can be invisible to both commands.

### REL-08 — Continuous integration for this repository
- **Status:** open · **Severity:** medium
- **Analysis:** there is no `.github/workflows` here (only the template shipped for
  consumers), so the suite with and without extras, `repolens docs coverage --check` and
  `repolens docs build` never run on the tool itself (GitHub issue #14). A read of the
  public symbols found 20 still undocumented, which breaks a consumer's docstring ratchet
  when it vendors the tool.
- **Required:** a workflow that runs the suite in a bare environment and with all extras,
  `compileall`, the docs coverage ratchet with a committed baseline, and `docs build` without
  the `api` extra. Document the remaining public symbols.
- **Why:** the tool's own quality gates should not first fail in a consumer's CI.

### REL-09 — `repolens vendor verify`
- **Status:** open · **Severity:** low
- **Analysis:** `docs/installation.md` documents a `VENDORED.json` record (upstream commit and
  tree hash) and a manual check; `docs/roadmap.md` lists the command as planned (GitHub issue
  #14).
- **Required:** a command that reads `VENDORED.json`, recomputes the tree hash of the vendored
  directory without the record, and exits non-zero on a difference; test it on a temporary
  git repository.
- **Why:** proving a vendored copy is unmodified should not take a hand diff.

### REL-10 — Released tags do not carry their own version
- **Status:** open · **Severity:** medium
- **Analysis:** `v0.2.0`, `v0.3.0` and `v0.4.0` all point at commits whose
  `repolens/__init__.py` reads `__version__ = "0.3.0"`, and `pyproject.toml` repeated the
  same literal. A wheel built from any of the three is named `repolens-0.3.0`, and a report
  produced by the newest is stamped `repolens 0.3.0` by `provenance.tool_build()`. Three
  releases cannot be told apart from the tool's own output, and two of those wheels cannot
  coexist in an index. The duplication is now gone — `pyproject.toml` declares
  `dynamic = ["version"]` and reads the attribute — but that only keeps the two in step; it
  cannot make a stale attribute right.
- **Required:** bump `__version__` past `0.4.0` in the next release, and check the artifact
  against the tag before pushing it (`python -m build`, then the wheel filename and
  `repolens --version` from an install of it). Retagging the three published tags is not
  proposed: the commits they name are public, and a moved tag is worse than a recorded one.
- **Why:** the provenance stamp exists so a result can be matched to the code that produced
  it. A version shared by three releases makes that stamp's first field decorative, and the
  cache/config fingerprints it sits beside do not cover the tool's own behaviour changes.

---

## Resolved

Implemented and covered by tests during this audit pass (verified together under REL-01).

| Item | What changed | Test |
|---|---|---|
| Git config could run commands | Every git call goes through `core.git.run_git`, which overrides `core.fsmonitor`, drops `GIT_*` variables and has a timeout. The dirty flag uses `diff-index`/`diff-files`, which run no clean filters, instead of `git status`; unknown is reported as `+unknown` or `dirty: null`. | `tests/test_git_hardening.py` |
| HTML page size unbounded with many distinct rules | Once the row budget is spent, rule and diagnostic groups collapse into one line, and tool notes are capped. 100k distinct rules plus 100k notes gave 1.5 MB instead of 27.9 MB. | `tests/test_html_report.py` |
| `repolens report` had no HTML output or build stamp | It writes `report.html`, and the Markdown and SARIF carry the build. | `tests/test_scan.py` (`test_the_report_writes_a_stamped_html_page_beside_markdown_and_sarif`) |
| Oversized `package.json` or `tsconfig` looked complete | Interpreted manifests stay `FILE_SKIPPED`, which is incomplete. | scan input tests |
| Stamp hash changed with editor swap files | `source_sha256` hashes only shipped file types and skips dotfiles. | `tests/test_provenance.py` |
| Target `SyntaxWarning`s printed during scans | Suppressed at every `ast.parse` of target code. | probe |
| `${base}/path` with a parameter default lost its base | The literal default base is used (`probable`). See JS-18 for the remaining restriction. | `test_a_base_url_parameter_default_is_the_requested_base` |
| Aliased `APIRouter as _X` left mounts unresolved | Import aliases of constructor classes are recognised. | `test_a_router_built_from_an_aliased_import_is_mounted` |
| Import resolution spent minutes in `realpath` | Lexical, memoised resolution against the admitted inventory. | `test_candidates_resolve_against_the_admitted_inventory_without_the_filesystem` |
| Regex fallback claimed any `db.x.find` as Mongo | Requires the same MongoDB handle evidence as the syntax path. | `test_the_regex_fallback_needs_the_same_mongodb_handle_evidence` |
| A bare `&` in JSX text was a `JAVASCRIPT_PARSE_ERROR` (incomplete) | Tolerated in JSX text and `.jsx` files (found while fixing JS-01 to JS-05). | `test_a_bare_ampersand_in_a_jsx_file_is_not_a_parse_error` |
| Docs over-claims and consumer-specific leftovers | Corrected the package.json deployment wording and the "every output" claim; added gitignore and deployment `LIMITS` entries; removed a consumer-only pin; neutralised domain identifiers in comments and tests. | docs review, name sweep |
| DEP-01 Unrecognised start commands were skipped | In a non-auxiliary manifest, an unknown or dynamic program blocks. In-repository scripts with a shebang are followed; `uwsgi` and `waitress-serve` app specs are resolved. | `tests/test_deploy_probes.py` rows `Procfile bin/start` … `cheroot` |
| DEP-02 Module resolution took the first same-named file | Demotes only when the app name resolves to exactly one file. `WORKDIR` is mapped through `COPY`, and a Dockerfile is read with both its folder and the repository root as build context. | probe rows for `COPY api/ .`, unmapped `WorkingDirectory`, compose `build.dockerfile`, `COPY srv/ /app/`; `tests/test_scan.py` `test_an_image_that_copies_no_code_cannot_name_its_module` |
| DEP-03 Many deployment formats were not detected | Vercel, SAM, Azure Functions, Cloud Foundry, Terraform, Cloud Build, Kubernetes JSON and YAML (also flow style and `values*.yaml`), App Engine services, `fly.<env>.toml`, Bicep, systemd drop-ins, `.in` templates, `Dockerfile-*`/`Containerfile*`, pm2 and circus all block, and so does a server line in a Makefile, justfile, Taskfile, Ansible task, `web.config`, `startup.txt` or `.cmd`/`.bat`/`.ps1` script. | probe rows (A-DEP-4 group) |
| DEP-04 Unreadable deployment files did not block | Too large, binary or invalid UTF-8 files block. Compose YAML the line reader cannot follow (flow style, a service header with a value, quoted or mis-indented keys, service keys outside `services:`, a service with no image/build/command) blocks. CRLF files are read. A 64 KiB command line blocks, and any exception while reading blocks. | probe rows (A-DEP-1, A-DEP-5 groups), `test_random_manifests_never_raise` |
| DEP-05 Servers started from Python scripts were missed | `uvicorn.Config`, aliased imports and `subprocess` are handled. A run script that changes `sys.path` or the working directory, or passes `app_dir=`, blocks. | probe rows (A-DEP-7 group) |
| DEP-06 Shell options stopped command parsing | `-o`/`+o` values are skipped and in-repository scripts are followed. | probe rows `bash -o pipefail -c` … |
| DEP-07 Compose anchors, `extends:` and image-only services were ignored | They block, except a command-less service or final Dockerfile stage on an image that cannot serve the app (`_INERT_IMAGES`: plain OS/interpreter images, postgres, redis, nginx, …). | probe rows (A-DEP-3, A-DEP-12 groups) |
| DEP-08 Ambiguous app specs | Option values with colons are not taken as the app; more than one app-like positional blocks. | probe row `gunicorn --dogstatsd-tags` |
| DEP-09 Dynamic router imports broke reachability | `wiring` follows constant `import_module`/`__import__` (relative names too). A reachable file that imports modules chosen at run time makes reachability unknown, so nothing is demoted (narrowing it is DEP-18). | `test_routers_found_at_run_time_are_not_called_unreachable`, probe rows |
| DEP-10 Supervisor continuation lines | A key is set only on lines with `=`. | probe row `supervisor continuation` |
| DEP-11 Docstrings over-claimed | The `deploy.py` and `wiring.py` docstrings, README, CLAUDE.md, CHANGELOG, `LIMITS`, the settings comment and the config template describe the fail-closed rules and the real read bounds (`max_file_bytes` for parsed manifests, 64 MiB in 1 MiB chunks for lexical checks). | docs review |
| DEP-12 Deploy fixtures looked lifted from a real layout | Renamed to `api.service`, `scripts/notes.sh`, `svc/server_copy.py`, port 8000, `/srv/example/worker`. | name sweep |
| DEP-13 Deployment detection default | Stays on (maintainer decision, 2026-09-14), now that the probes are tests. | — |
| DEP-14 Three deployment tests failed after the rewrite | The fixtures now match the fail-closed rules: an entrypoint that is in the repository still demotes, and one that is not blocks. A Dockerfile that copies code gives the module its root, and one with no `COPY` blocks. | `tests/test_scan.py` DeploymentTests |
| DEP-15 Audit probes were only temporary scripts | Every probe is a table row; the two rows that used to fail are ordinary rows now. | `tests/test_deploy_probes.py` |
| A-DEP-6/8/10 Symlinks, OS errors and reads outside the root | A manifest symlink or linked directory leaving the root blocks. `OSError` in path checks means "not a file", `discover` failures mean no demotion, and a shebang is read only inside the root with `O_NOFOLLOW`. | `test_nothing_is_opened_through_a_directory_that_leaves_the_root`, probe rows |
| A-DEP-9 Quadratic work on crafted manifests | Bounded patterns, one package.json line index, list-built continuations, incremental bracket depth. A 20k-character rsync line went from 1.47 s to 0 s; 20k package.json scripts from 34 s to 0.6 s. | `test_long_lines_stay_linear` |
| REL-07 Interrupted JS and deployment fix agents | The deployment rewrite was finished and tested (see the DEP rows), and the JS/TS work was re-audited and finished; the task list was the resume point. | full suite |
| OUT-01 `linkage.mmd` carried no build stamp | It ends with a `%% produced by …` Mermaid comment. | `tests/test_html_report.py` (analyze writes the stamp) |
| DOC-01 Field-report labels in test docstrings | The `RL-nn` prefixes are gone; each docstring describes the behaviour it tests. | `grep -rn "RL-[0-9]" tests/` is empty |
| DOC-04 Domain vocabulary in source docstrings | Scanner examples use `billing.*`, and the tenant-scoping explanation in `scan/security.py` uses organisations. A hardcoded domain word in the identity-parameter filter became the `[scan.security] scope_dependency_patterns` setting (default empty). | `tests/test_scan.py` `ScopeDependencyTests`, name sweep |
| PERF-01 Re-measure the large repository | A full `analyze` of the large evaluation repository took 124 s on 2026-09-14, down from 5 min 23 s, with no files written to the target. PERF-02 to PERF-06 remain. | measured run (REL-01 records the final one) |
| REL-06 Author attribution in `LICENSE` and `pyproject.toml` | Both name "Repository Lens contributors" (maintainer decision, 2026-09-14). | — |
| OUT-03 The Python checks ignored `respect_gitignore` | Maintainer decision (2026-09-14): skip gitignored files that are not part of the project. `scan.python_ast.python_files` and the `migrations.migration_files` fallback drop untracked gitignored files through `not_gitignored`. One switch serves both the graph and the checks: `respect_gitignore`, read from `[impact]` and `.impact-tracer.json` into `ScanSettings.respect_gitignore` and included in `run_inputs`. The git listing is `core.git.untracked_ignored`, shared with the graph scan and cached per run. If git cannot list ignored files, every file is read. Under `analyze` the checks already used the graph's inventory, so the gap was only in `repolens scan`/`report`. Deployment manifests are still read when ignored (OUT-07). | `tests/test_scope_rules.py` `GitignoredPythonFilesTests` (ignored directory and pattern, tracked-but-ignored file kept, migrations, `respect_gitignore = false`, the `.impact-tracer.json` layer, non-boolean value rejected, unusable repository reads everything) |
| OUT-04 Router mounts in test code made the analysis incomplete | Maintainer decision (2026-09-14): test code must not make an analysis incomplete, in any language. `UNRESOLVED_LOCAL_IMPORT`, `UNRESOLVED_ROUTER_MOUNT` and `ROUTER_MOUNT_CYCLE` whose evidence is in test code (`core.files.is_test_code`: a `tests/`, `test/`, `__tests__/`, `e2e/` or `cypress/` directory, or a test file name) stay in the output but are left out of `incomplete_reasons`. `spec/`, `fixtures/` and `testdata/` on their own are application code for this rule; the broader `is_test_path` serves only precision choices. Parse errors and skipped files in test code still count (OUT-08). | `tests/test_scope_rules.py` `TestCodeCompletenessTests` |
| SQL-01 `GRANT;` crashed the file and lost later statements | Any exception parsing one statement is a located `SQL_PARSE_ERROR`; GRANT/REVOKE patterns tightened. The reader's own splitting helper runs outside that handler, so a repolens bug is not reported as the target's parse error. | `tests/test_sql.py` `test_a_statement_the_parser_raises_a_non_parse_error_on_stays_local`, `test_a_failure_in_the_readers_own_helper_is_not_reported_as_the_targets_parse_error` |
| SQL-02 Mongo collection claims were too trusting | A receiver bound in the file decides: `<client>.db(...)`, `mongoose.connection.db`, a driver-typed parameter, or a local import exporting a handle. `collection()` uses the same check. Comments and `db = null` do not bind. Answers are cached per file and receiver (1000 call sites: 14 s → 0.13 s). | `tests/test_columns.py` `test_a_driver_import_does_not_make_every_db_binding_a_mongodb_handle`, `test_clearing_comments_nested_arguments_and_other_receivers_do_not_hide_a_handle`, `test_mongodb_handle_checks_do_not_repeat_per_call_site`; `test_the_regex_fallback_needs_the_same_mongodb_handle_evidence` |
| SQL-03 Package-scoped DDL suppression caused false column warnings | ALTER additions, reshaped tables and computed-target dynamic DDL apply repository-wide; `CREATE TABLE` catalogs stay per package. Run-time DDL in test code does not taint the catalog. | `test_alter_table_and_dynamic_ddl_in_another_package_apply_to_the_shared_database`, `test_run_time_ddl_in_test_code_does_not_disable_the_catalog` |
| SQL-04 Migrations in languages the scanner does not read | `columns._foreign_ddl_tables` reads Rails, Laravel, EF Core, Ecto, Go/Java/Rust SQL strings and Liquibase lexically. Catalogs are disabled only for a computed `ALTER TABLE` target or a migration naming no table; UI copy, comments and query builders mentioning "create table" are ignored. | `test_migrations_in_languages_the_scanner_does_not_read_disable_the_tables_they_name`, `test_a_computed_table_name_in_an_unread_language_disables_every_ddl_catalog`, `test_words_that_only_mention_schema_changes_disable_nothing` |
| SQL-05 `SET search_path` was ignored | Unqualified names are unchecked where SQL sets `search_path` (not in string literals, comments or function `SET` clauses), repository-wide for role/database defaults and SQL strings or `-c search_path=` options in code. A `CREATE TABLE` before the `SET` still declares. See SQL-20. | `test_search_path_changes_leave_unqualified_names_unchecked`, `test_an_unqualified_create_under_a_changed_search_path_declares_nothing`, `test_only_a_search_path_the_sql_itself_sets_redirects_names` |
| SQL-06 Prisma `view` blocks treated as complete | A view's columns are unknown. | `test_a_prisma_view_has_unknown_columns` |
| SQL-07 Test queries checked against production DDL | References in `core.files.is_test_path` paths are not checked. | `test_queries_in_tests_specs_and_e2e_suites_are_not_checked` |
| SQL-08 Valid PostgreSQL misreported as another dialect | Ambiguous signals (`GO` once, `TOP (n)`, `NVARCHAR(`, `IDENTITY(n,n)`) are weak and need a second signal. Strong T-SQL signals (`GO` twice, `USE x;`, `SET NOCOUNT`, `EXEC sp_`, bracketed names, `SELECT TOP n`) name common scripts on their own. | `test_valid_postgresql_that_resembles_another_dialect_is_postgresql`, `test_common_t_sql_scripts_are_named_without_a_second_weak_signal` |
| SQL-09 The prose gate accepted UI copy | Capitalised verbs need SQL punctuation in the first statement after the verb; TRUNCATE is cased; COPY needs FROM/TO plus a path, STDIN, STDOUT or PROGRAM. Lowercase prose remains (SQL-15). | `test_capitalised_statement_verbs_without_sql_punctuation_are_ui_copy`, `test_prose_commas_later_sentences_and_copy_without_a_source_are_not_sql` |
| SQL-10 Prisma provider decided for the whole repository | Per package, from `columns.POSTGRES_PRISMA_PROVIDERS` (CockroachDB included); a package with an unsupported provider does not borrow another package's tables. | `test_each_package_maps_prisma_accessors_with_its_own_provider`, `test_cockroachdb_is_checked_like_postgresql`, `test_a_package_with_an_unsupported_provider_does_not_borrow_another_packages_table` |
| SQL-11 Statement splitter edge cases | `t.end` no longer closes an atomic body; unterminated `COPY … FROM stdin` data ends at end of file; psql variables are not substituted in `E'…'` strings or after `[`; rows after `\copy … FROM stdin` are data. A psql statement that does not parse is `SQL_PARSE_ERROR`, not `DYNAMIC_SQL`. | `test_a_column_named_end_is_not_the_end_of_an_atomic_body`, `test_copy_data_without_a_terminator_runs_to_the_end_of_the_text`, `test_escape_strings_and_array_slices_are_not_variables`, `test_rows_after_a_psql_copy_from_stdin_are_data`, `test_a_psql_statement_that_does_not_parse_is_a_parse_error_not_dynamic_sql` |
| SQL-12 Inaccurate comments | Prisma `/* */` claim removed; the `incomplete_reasons` comment corrected. | the Prisma accessor test uses `//` comments |
| SQL-14 `LIMITS` lacked the column-check scope | `LIMITS` now states the column-check scope, the dialect skip and the MongoDB handle limit; CLAUDE.md, the README and the CHANGELOG match. | docs review |
| SQL fixture vocabulary | Test fixtures renamed to neutral names (`billing.payments`, `inventory`, `reservations`, `channelType`, `labelText`, `noteKind`). | name sweep |
| Two target-code parses still printed `SyntaxWarning`s | The string-annotation re-parse in `impact/fastapi.py` and the `python -c` parse in `scan/deploy.py` suppress target warnings like every other `ast.parse` of target code (GitHub issue #13). | `tests/test_python_graph.py` `test_a_string_annotation_with_an_invalid_escape_prints_no_warning`, `tests/test_deploy_probes.py` `test_python_dash_c_code_with_an_invalid_escape_prints_no_warning` |
| JS-01 Every call to an Express/Koa/Hono/Fastify/Nest backend was a warning | Literal routes of an Express/Fastify/Hono/Polka/Elysia server the same file starts listening on are `probable` endpoints. When code outside test paths registers routes that are not modelled (routers, NestJS controllers, file-based handlers, Remix/React Router `loader`/`action` in a module importing those packages, a Python module that builds a Flask/Django/… app), both gap codes are info and name the evidence. Look-alike receivers, catch-alls on a listening server and bare framework imports do not count. Remaining: JS-26, JS-27. | `tests/test_javascript_graph.py` `test_routes_in_unmodelled_backends_make_handler_gaps_info`, `test_a_listening_server_s_literal_routes_are_endpoints`, `UnmodelledRouteEvidenceTests` |
| JS-02 Taint analysis was exponential on reused bindings | `untrusted()` and `sql_spellings` are memoised per binding (and depth); a result cut short by the depth limit is reused only at that depth or deeper, so answers do not depend on query order. `sql = sql + …` at n=160: 40 s → 0.01 s. Remaining: JS-25. | `test_taint_through_reused_bindings_is_linear`, `test_self_assigned_sql_is_spelled_in_linear_time`, `test_taint_does_not_depend_on_which_query_is_read_first` |
| JS-03 Binding lookups were quadratic | Declarations are looked up per function and per block (`let`/`const` in `statement_block`/`for`); an assignment counts only when no scope in between redeclares the name. Remaining: JS-25. | `test_many_functions_sharing_parameter_names_parse_in_linear_time`, `test_sibling_blocks_declaring_the_same_name_parse_in_linear_time`, `test_an_assignment_to_another_variable_of_the_same_name_does_not_taint` |
| JS-04 Long `+` chains exceeded the recursion limit | Left-nested chains are folded in a loop for URLs and SQL, so a long SQL concatenation keeps its query and every spliced value; right-nested URL concatenation stops after 64 levels. | `test_a_long_plus_chain_does_not_lose_the_file`, `test_a_long_plus_chain_keeps_its_query_and_every_hole`, `test_a_right_nested_url_concatenation_does_not_lose_the_file` |
| JS-05 Common safe SQL idioms were reported as injection risks | Numeric conversions and operators, node-postgres/pg-format quoting, mysql/sqlstring `escape`/`escapeId` on a SQL-connection receiver, index-only placeholder callbacks, one-argument `fill`, and `typeof`/`Number.isInteger` guards are safe. HTML/CSS escapers, locally defined `escape`, `util.format` and callbacks reading the element, the array, `arguments` or `this` are not. | `SqlInterpolationTests.test_sanitisers_and_their_look_alikes` (75 table rows) |
| JS-20 Test docstring and fixtures derived from a real application | Docstring made generic; fixtures renamed to catalog/invoices/orders/reports/customers; one source comment example neutralised. | name sweep |
| JS-audit clean-ups | `.all()` registrations are labelled `router.all()`; the unused `JSFacts.hook_bindings` field is removed. | `UnmodelledRouteEvidenceTests` |
| REL-02 `SCANNER_REVISION` bump | Bumped from 7 to 8 after the last extraction change; the CHANGELOG upgrade note says "went from 2 to 8". | `tests.test_impact` |
| REL-03 Generated API contracts | Unchanged by the later fixes; the committed OpenAPI and Postman documents match a fresh export. | `tests.test_api.ApiTests.test_generated_contracts_match_the_committed_documents` |
| An aliased router import was lost when a local file shared the framework's name | `_FastAPI._constructors` also reads aliases from the import statements, because a vendored `fastapi.py` anywhere makes `from fastapi import APIRouter as _R` an unbound local import. Found as the one remaining `UNRESOLVED_ROUTER_MOUNT` on the large evaluation repository. | `tests/test_python_graph.py` `test_an_aliased_router_import_survives_a_local_file_named_like_the_framework` |
| REL-05 Logical commits and the pull request | The stack-depth audit branch was merged into `master` as PR #16. | — |
| SCAN-01 Grouped aggregates reported as unbounded fetches | A `GROUP BY` whose select list is only group keys (by expression, alias, position or unqualified name), aggregates and constants is bounded, in SQL text and in SQLAlchemy `select`/`query` + `group_by` with `func.*`. Other columns, set operations, `ROLLUP`/`CUBE`/`GROUPING SETS` and nested ORM selects are still reported. `[scan.performance] scope_key_patterns` (default empty) lowers the severity one step (confidence is already low) when every statement's `WHERE` compares a matching column with a value; joins do not count. The message names the column; the note is left out of the fingerprint. On the field evaluation: 403 → 346 findings. | `tests/test_unbounded_fetch.py` `GroupedAggregateTests`, `ScopeKeyTests`; `tests/test_postgres.py` `UnboundedFetchTests` |
| JS-06 Allow-list guards were too narrow | `literal_table` accepts inline literal arrays, objects and Sets and `Object.keys`/`Object.values` of a literal table. `statement_guarded` accepts a value inside the consequence of `if (guard)`, or after an `if (!guard)` in an enclosing block whose consequence throws, returns, breaks or continues, when no assignment to it follows the guard. | `tests/test_javascript_graph.py` `OriginAndMatchingTests.test_allow_list_guards_beyond_a_named_ternary`, `SANITISER_CASES` (`if (!Number.isInteger(n)) throw` row) |
| JS-07 A backslash silently dropped a query | `javascript.decode_escapes` decodes JS string and template escapes (`\'`, `\n`, `\x..`, `\u....`, `\u{...}`, line continuations; NUL and lone surrogates become U+FFFD) for URLs, module constants, parameter defaults, tagged templates and SQL text, and quotes are counted on the decoded text. No backslash bail-out remains. | `OriginAndMatchingTests.test_escaped_string_text_is_decoded_not_dropped` |
| JS-08 The single base URL fallback was applied too broadly | `scanner._untraced_bases`: `client_api_base`, else the one base the caller's package declares (`columns.PackageRoots`), else the repository's one. An assumed base is not prepended to a URL that already starts with it, and `$`, `jQuery`, `axios`, `ky` and `this.<field>` (Angular `HttpClient`) never get one. | `OriginAndMatchingTests.test_the_assumed_base_is_per_package_and_never_added_twice` |
| JS-09 Configured-origin suffix matching created false links | `Request.configured_origin` carries the origin's name. `scanner._own_origin` accepts a name whose words, after a framework prefix, are all API/BACKEND/SERVER/BASE/URL-like, or one listed in the new `[impact] api_origins`; any other origin is `EXTERNAL_API_REFERENCE` naming it. Suffix matches need a literal segment and are always `ambiguous`; a URL of only runtime segments is `DYNAMIC_HTTP_REQUEST`. | `OriginAndMatchingTests.test_a_vendor_origin_is_external_and_suffix_matches_are_ambiguous`, `test_api_origins_names_an_origin_whose_words_do_not` |
| JS-11 Test-runner globals treated as clients | `browser`, `cy`, `page`, `driver`, `window`, `document`, `location`, `history`, `navigator`, the storages, `globalThis` and `self` are never untraced clients, nor is any unbound receiver in an `is_test_path` file. | `OriginAndMatchingTests.test_browser_test_runner_navigation_is_not_an_api_call` |
| JS-12 `client_receivers` overrode local bindings | Did not reproduce on the merged tree: extraction classifies a receiver bound from a router factory (`express.Router()`) as a route registration, and `_resolve_js_requests` skips registrations before it consults `client_receivers`. Regression test added. | `OriginAndMatchingTests.test_client_receivers_do_not_turn_route_registrations_into_calls` |
| JS-13 Liveness misjudged dynamic imports, barrels and Expo Router | Imports that bind no name (literal `import()`, side-effect imports) are recorded as namespace uses. A re-export-only `index` file is not an entry. `_package_entries` adds files a package.json names in `main`/`module`/`browser`/`bin`/`exports` (with extension and `index` resolution) and every file under `app/` of a package using Expo Router. `LIMITS` corrected. | `LivenessEntryTests.test_loaded_code_is_not_judged_dead` (7 layouts plus one module that stays dead) |
| JS-16 Environment and localhost base URLs handled inconsistently | `baseURL: process.env.X` is a configured origin with an empty path. A `localhost`, `127.0.0.1`, `0.0.0.0` or `[::1]` origin in a base or URL is this repository, `probable`, without suffix matching. | `OriginAndMatchingTests.test_an_environment_or_localhost_client_base_is_a_configured_origin` |
| JS-17 The configured-origin marker collided with a real URL | The marker is `\0configured-origin:<name>\0<path>` (`javascript.configured_base`/`split_configured`); a literal `//configured.example.com/api` base is external. | same test |
| JS-18 Parameter defaults used for non-URL values | A default at the head of a URL is a base only when it starts with `/` or a URL scheme. | `OriginAndMatchingTests.test_only_a_base_url_parameter_default_is_a_base` |
| JS-19 Method-mismatch check ignored catch-all routes | `_methods_serving` uses `_serves`. | `OriginAndMatchingTests.test_a_patch_to_a_get_only_catch_all_route_is_a_method_mismatch` |
| JS-24 Open-ended request URLs linked to every handler under the prefix | A query-like tail (text starting with `?`/`&`, `new URLSearchParams`, `"?" + x`, a ternary with `""`, or a binding of those) is spelled `?{dynamic}`, so only the exact path matches. An open-ended or runtime-segment match with more than `max_ambiguous_targets` handlers is not linked: it is `DYNAMIC_HTTP_REQUEST` with the count, and the endpoint stores `matched_handler_count` instead of every handler id. Field evaluation: 782 → 10. | `OriginAndMatchingTests.test_query_string_tails_match_only_their_path`, `test_a_request_wrapper_open_to_every_handler_is_not_linked` |
| JS-26 Common route registration shapes were not recognised | `chained_route` records `.route(path).<verb>()` chains and hapi/Fastify `.route({ method, path, handler })` objects (`url` for `path`, `options` or `config` for `handler`) on a router-named receiver or in a file importing a server framework; `route_shape` counts a router-named parameter given a handler by reference. All are unmodelled routes. | `UnmodelledRouteEvidenceTests.test_route_chains_route_objects_and_parameter_routers_are_unmodelled_routes` (4 shapes, 3 look-alikes) |
| SQL-21 Pattern-only store references in test code created stores | Found in the field evaluation: a vendored tool's SQL test fixtures created 11 tables. Regex store matches in `is_test_path` files are recorded on `ScanState.test_store_references` and linked by `_link_test_store_references` (after every other pass, before `mark_unverified_stores`) only to stores other evidence created. | `tests/test_schema_references.py` `TestCodeAndArtifactStoreTests.test_a_pattern_match_in_test_code_links_only_to_a_store_found_elsewhere` |
| SQL-22 Unvalidated artifact table names became tables | Found in the field evaluation: 38 tables (`schema.view`, `schema.fact_`) came only from the router/datastore artifact's `postgres_unverified_refs`. Such a name now links only to a table the scan found by more than a pattern match; the rest are listed in one `ARTIFACT_STORE_UNCONFIRMED` (info). | `TestCodeAndArtifactStoreTests.test_unvalidated_artifact_names_link_only_to_tables_the_scan_found` |
| JS-29 Route parameter names were discarded | Endpoint and page nodes carry `path` (OpenAPI spelling with source names: `[orderId]`, `[...slug]`, `:id`, `{file_path:path}`, `:id{[0-9]+}`, `:from-:to` become `{orderId}`, `{slug}`, `{id}`, `{file_path}`, `{id}`, `{from}-{to}`) and `parameters`, for Next.js App and Pages Router, listening JS servers, FastAPI mount prefixes and artifact routes. `route`, ids and matching are unchanged; the first spelling read (the walk reads a directory's files before its subdirectories) keeps its names (`state.route_path_and_parameters`, `state.route_metadata`). | `tests/test_route_parameters.py` `RoutePathTests`, `EndpointPathTests`, `FastApiPathTests`, `ExampleApplicationTests.test_the_order_route_is_documented_with_its_parameter_name` |
| JS-30 Page views missed API calls inside inline callbacks | A `DEFINES` edge (cost 2) runs from a JS/TS function or class to each function defined inside it, and an anonymous callback records `lexical_role` (`onClick`, `onSubmit`, `then`). Feature groups follow it from a page component to the calls its callbacks make; `impact()` walks it only from the inner function outwards, so a matched callback does not pull in its siblings (a page with 65 helpers keeps the page in the endpoint's view). Liveness and gap checks do not read the edge. Remaining misses: JS-31. | `ExampleApplicationTests.test_functions_and_inline_callbacks_are_defined_by_their_component`; `DefinesTraversalTests.test_an_endpoint_view_reaches_the_page_not_the_siblings_of_its_callback`; `tests/test_features.py` `test_a_page_calls_another_area_through_an_inline_callback` |
| SQL-23 SQL foreign keys drew no relationship | `postgres._foreign_keys` adds a `REFERENCES` edge (exact, sqlglot) for `REFERENCES`/`FOREIGN KEY` in `CREATE TABLE`/`ALTER TABLE`, as `python_scan` does for SQLAlchemy; DDL in test paths adds none. | `tests/test_route_parameters.py` `ForeignKeyTests`; `tests/test_docs_generate.py` `test_schema_draws_declared_columns_relationships_and_collections` |
| DOC-05 No documentation for the scanned application | `repolens docs generate` (`docs/generate.py`) writes OpenAPI 3.1 for the application's routes with gaps listed in `x-repolens-gaps`, a Mermaid ER diagram, an architecture flowchart, a repository-wide static debugging guide, a mindmap page and context pack per route area (`impact/features.py`), and an index with completeness. Each feature context carries caller → route → handler → store trace candidates and safe runtime fields. Unserved calls stay out of `paths`. Calls from test code are not callers and form no group. The ER diagram applies SQL files in path order (drops, renames, nullability). It exits 2 when incomplete, never writes through a symlink, and never replaces or deletes an output-directory file without its build stamp in the place it writes one. `repolens api export` still documents only Repository Lens's own API. The outputs of the example application validate with `openapi-spec-validator` (OpenAPI 3.1) and every diagram parses with Mermaid 11 (checked by hand in the audit, not in the suite). Open: DOC-06, DOC-07, DOC-08. | `tests/test_docs_generate.py` `ExampleAppDocsTests`, `MermaidLabelTests`, `StoreReachTests`, `CommandBoundaryTests` (including `test_debugging_guide_is_static_and_gives_bounded_runtime_advice`, `test_a_file_that_only_quotes_the_stamp_is_not_its_output`, `test_it_never_writes_through_a_symlink`, `test_the_schema_is_what_the_migrations_leave`, `test_a_mindmap_section_is_capped`); `tests/test_features.py` |
| FT-04 FeatureTrace could not bootstrap an undocumented repository | `repolens featuretrace propose` drafts markers as `proposal.patch` and `proposal.json`, with the issues the audit would report recorded per draft. `--apply` writes the saved, reviewed proposal (never a new draft) under hash, edited-patch, git-clean (literal pathspecs) and root checks, and preserves BOM, line endings, encoding declarations and directives. `--jsdoc` drafts fact-only JSDoc opening with `TODO(repolens)`, which is always a `[docs] placeholder_patterns` entry. `--function-lens` drafts comment-only stable ids for public feature-boundary functions; the lens indexes and queries them after renames and reports duplicates. `--owners` writes a bounded capability-index draft to the proposal directory only. The output directory refuses symlinks and unstamped files. The audit's description now says it checks structure and references, not that a declared flow matches the code. Remaining: FT-05, FT-01 (data-flow half), JS-31. | `tests/test_featuretrace_propose.py` including `test_function_lens_ids_are_reviewed_comments_and_survive_a_symbol_rename`, `test_apply_writes_the_reviewed_proposal_not_a_new_draft`, `test_applied_markers_pass_the_audit_and_render_maps`; `tests/test_lens.py` `test_stable_comment_ids_are_indexed_and_duplicates_are_visible` |
