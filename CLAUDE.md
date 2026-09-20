# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python -m pip install -e '.[stack,api,test]'          # full dev environment (Python >= 3.11)
python -m unittest discover -s tests -q                # whole suite (unittest, not pytest)
python -m unittest tests.test_stack                    # one module
python -m unittest tests.test_api.ApiTests.test_workflow_scan_query_and_all_exports  # one test
python -m compileall -q repolens tests                 # syntax check used in the audit
bin/repolens <command>                                 # run from a checkout without installing
repolens analyze --out .repolens/analysis              # scan this checkout
repolens api export --out docs/api                     # regenerate OpenAPI + Postman contracts
python -m build                                        # wheel + sdist (needs the `build` package)
```

Without the `stack`/`api`/`test` extras about a third of the suite skips (`skipUnless` on
`tree_sitter`, `sqlglot`, `fastapi`, `httpx`). A green run in a bare environment does not
cover the JS/TS, SQL or API paths, so install the extras before trusting a change there.
A new test that needs an extra must carry the same guard.

`docs/api/openapi.json` and the Postman files are generated from `repolens/api/app.py`.
After changing API models, routes or defaults (including `analysis.VIEW_DEPTH`), re-run
`repolens api export --out docs/api`. Otherwise
`tests.test_api.ApiTests.test_generated_contracts_match_the_committed_documents` fails.

There is no lint config in `pyproject.toml`. `requirements-ci.txt` pins ruff/bandit for
`repolens report` baselines in *target* repositories, and matches `bootstrap.SCANNER_PINS`.
Its comments cover both this checkout and a vendored copy (a consumer typically keeps one at
`tools/repolens`). The package version lives only in `repolens/__init__.py`;
`pyproject.toml` reads it through `[tool.setuptools.dynamic]`, so a release bumps that one
line (`docs/installation.md`, "Building a release"). Pydantic model docstrings in
`api/app.py` become OpenAPI descriptions, so editing them also needs `repolens api
export`. Record user-visible changes and upgrade notes in `CHANGELOG.md`. Doc sections a release removes go
under `docs/history/`, verbatim except that identifiers of other repositories are generalised.

## Architecture

`repolens` is a read-only static analyzer for one checked-out repository. It must never
import or execute target code, install target dependencies, touch the network, or connect
to a database. Keep that invariant when adding features.

### CLI dispatch
`repolens/cli.py` maps `"<group> <command>"` strings to modules in `COMMANDS`. Each
module exposes `main(argv, *, config, prog)`. The dispatcher tries two-word names first,
then one-word names. To add a command, add a `COMMANDS` entry and a module with that
`main` signature. `impact-tracer` is a second console script kept for backward
compatibility (`repolens/impact/cli.py`).

### Two config layers (easy to confuse)
- `repolens/config.py`: `load_config()` reads `repolens.toml`. Each subpackage reads its
  own table through `Config.section(...)` and deep-merges it over its own `DEFAULTS`
  (e.g. `scan/settings.py`, `report/runner.py`).
- `repolens/impact/config.py`: a separate `Config` dataclass for the graph scanner. It
  starts from defaults, applies `[impact]` from `repolens.toml`, then applies
  `.impact-tracer.json` on top, and finishes with `validate()`. Artifact paths must be
  relative; absolute paths and `..` are rejected.

`analysis.analyze()` uses both: the impact `Config` for the graph, and
`scan.settings.from_config(load_config(root))` for the Python security/performance/
migration checks. API requests may override only `max_file_bytes`/`max_files`, and only
on top of the repository policy.

### Evidence graph (`repolens/impact/`)
- `model.py`: `Graph` holds `Node`, `Edge` and `Issue`. `add_edge` raises if an endpoint
  node is missing, so add nodes first. IDs come from `stable_id(kind, value)`.
- An edge's `origin` (tree-sitter, python_ast, sqlglot, declared, heuristic, ...) is kept
  separate from its `resolution` (`exact`/`high`/`probable`/`declared`/`ambiguous`).
  Name-only matches are `probable`, never `exact`. SQL edges keep the kind
  `TOUCHES_STORE` for compatibility; read/write/DDL goes in `detail`.
- `scanner.py` `scan_repository()` is the pipeline. It collects files (capped by
  `max_files`), reads each one bounded (`source.py`, BOM-tolerant), and dispatches per
  file:
  - Python goes to `python_scan.py` (the AST visitor: definitions, calls, SQL literals,
    ORM/ODM tables, Mongo collections).
  - JS/TS goes to `_scan_javascript_syntax`, which consumes `core/javascript.py`
    `JSFacts`, falling back to regex with an issue when tree-sitter is unavailable.
  - `.sql` goes to `postgres.add_sql_file`; `.prisma` files are collected.
  - Every file then gets generic lexical checks and any enabled plugins.
- Shared per-scan state and graph helpers live in `state.py` (`ScanState`,
  `PendingCall`, `_add_store_edge`, Mongo method vocabularies).
- After all files, passes run in this order:
  1. deferred JS requests (HTTP client base URLs across modules)
  2. JS stores (Prisma schemas, Drizzle/Mongoose/TypeORM model bindings), then
     `columns.check_columns`
  3. Python ORM references
  4. FastAPI routes/mounts (`fastapi.py`)
  5. call resolution: import bindings, JS barrels and export aliases, Python package
     re-exports, `self`/`this` methods, then name-only fallback; `external:` bindings
     mean package imports and never name-match
  6. declared artifacts, then the `API_PREFIX_ALREADY_RESOLVED` note
  7. `_match_endpoints` (route-shape matching, always `probable`)
  8. endpoint gaps (`API_CALL_WITHOUT_HANDLER`, `API_METHOD_MISMATCH`), structural
     duplicates
  9. `render.mark_unverified_stores`

  Each pass runs through `run_pass`: an exception becomes `ANALYSIS_PASS_FAILED` (which
  makes the analysis incomplete) and the later passes still run.
- Anything that needs another file's facts must be recorded on `ScanState` during the
  per-file walk and resolved in a pass after it, never resolved eagerly.
- SQL text from code must go through `postgres.add_sql(..., gated=True)`, which ignores
  prose that isn't SQL-shaped (`looks_like_sql`). Template substitutions bound as query
  parameters use `parameterized=True`. Parse failures in gated text are
  `SQL_NOT_PARSED` (info) and do not make the analysis incomplete.
- JS/TS SQL passed to `.query/.execute/.raw/$queryRawUnsafe` is rebuilt by
  `core/javascript.py` `sql_text` through same-file bindings (template fragments,
  ternaries, `+=`); spliced values become `$n` and are recorded in
  `JSFacts.sql_interpolations` with an `untrusted` origin (`process.argv`, request input,
  Next.js handler `params`/`searchParams`). Values that cannot carry SQL text are not
  tainted: numeric conversions (`Number`/`parseInt`/`parseFloat`/`BigInt`/`toFixed`/`Math.*`,
  unary and arithmetic operators), node-postgres `escapeLiteral`/`escapeIdentifier`,
  pg-format `format.ident`/`format.literal` and a `format` of only `%I`/`%L`, mysql/sqlstring
  `escape`/`escapeId` on a SQL-handle receiver (`_SQL_HANDLE`; not `_`/`validator`/`he`/`CSS`),
  placeholder lists built without reading the elements (`ids.map((_, i) => …)`, a
  one-argument `fill`), constant allow-list lookups, and branches a
  `typeof`/`Number.isInteger`/`includes` guard proves. The scanner
  emits `SQL_INJECTION_RISK` (warning) or a named `DYNAMIC_SQL` (info), and
  `analysis.analyze` copies `SQL_INJECTION_RISK` into the security run as
  `security/sql-string-interpolation`. Tagged templates stay bound parameters.
- `API_CALL_WITHOUT_HANDLER` and `API_METHOD_MISMATCH` drop to info when every caller is
  dead by `_JavaScriptLiveness` (import reachability from routed files and `_ENTRY_STEMS`,
  then exported-symbol use by live modules; with no entry file nothing is judged dead), or
  when code outside test paths registers routes the scanner does not model: an
  Express/Koa/Hono router or router-named receiver given a handler (not one bound to another
  value unless the file imports a server framework), a NestJS controller, file-based
  SvelteKit/Expo/Astro/Nuxt handlers, a `routes/` `loader`/`action` in a Remix/React Router
  module, or a Python module that imports Flask/Django/… and builds an app or `urlpatterns`.
  A catch-all on a listening server does not count. The message names them; the judgement
  is repository-wide.
- `backend_api_prefix` predates mount resolution. `state.with_api_prefix` adds it only
  to routes that do not already start with it (unmounted routers, artifact routes), and
  the scan reports `API_PREFIX_ALREADY_RESOLVED` (info) when it skipped any, so a 0.3.0
  config does not silently produce `/api/api/...`.
- Store edges are always `TOUCHES_STORE`. Put `reads`/`writes`/`declares` in `detail`.
  A Mongo collection is claimed only when a driver method is called on it
  (`MONGO_NOT_COLLECTIONS` guards `db.session`, `db.query`, ...). In JS/TS the receiver
  must also be a MongoDB handle (`scanner._mongo_handle`). When the receiver is bound in
  the file, that binding decides: assigned from `<client>.db(…)`/`mongoose.connection.db`,
  typed as the driver's `Db` (`Connection` for `.collection()`), or imported from a local
  module exporting such a handle. Untyped parameters, other initialisers and package
  bindings are not handles; comments and `db = null` do not bind. An unbound receiver
  counts only in a file importing `mongodb`/`mongoose` or in a mongo shell script. Answers
  are cached per file and receiver (`ScanState.mongo_handles`). `db.User.findOne` (Sequelize) and `db.users.find(fn)` (an array) are not.
- Regex store matches (`scanner._add_regex_store_edge`) are always `probable` and say
  "unverified" in `detail`. A configured `<schema>.<table>` counts only in a SQL table
  position (`_in_sql_table_position`: after FROM/JOIN/INTO/UPDATE…SET/TABLE/...), never
  in patch/import targets, dict keys or bare dotted paths (a schema is often also a
  module name). `render.mark_unverified_stores` runs last in `scan_repository` and sets
  `metadata.unverified` only on stores whose every edge is regex. The default overview
  (`Analysis.view` with no query) leaves those out and the Mermaid map notes how many.
- Failures stay local to one file. An exception becomes a `FILE_SCAN_FAILED`/
  `FILE_TOO_DEEPLY_NESTED`/`EXTRACTOR_FAILED` issue, and the scan continues.
- `graph.metadata.content_sha256` must equal `repository_content_sha()` because the
  impact CLI cache (`.impact-tracer/index.json`) is invalidated by comparing them. If you
  change what is read or fingerprinted, update both. Bump `SCANNER_REVISION` when
  extraction output changes.
- `query.py` `impact()` runs a bounded weighted traversal from search seeds, with
  hub-file damping. New edge kinds need an `EDGE_COST` entry. `Analysis.view` (CLI
  `--depth`, API `depth`) defaults to `VIEW_DEPTH = 6`, because table → service →
  handler → endpoint → client → page is about 6 hops. `impact()`'s own default is still
  2. `render.py` produces Mermaid/Markdown/JSON, and Mermaid labels are
  escaped against injection.
- `plugins.py` loads trusted extractors from the `repolens.extractors` entry-point group.
  They load only when named explicitly (`--plugin`), are validated before merging, and
  are capped per file. The API never loads plugins. Contract: `docs/plugins.md`.

### Completeness is never implied
The core design rule is that a skipped, truncated or crashed input must not look like a
clean result. `analysis.Analysis.complete` is `not incomplete_reasons()`: false if any `ToolRun`
errored or skipped, or if any issue code is in its `incomplete` set. The reasons (one line
per cause) and the `provenance.tool_build()` stamp go into `analysis.json`, the reports,
SARIF (`driver.properties.repolensBuild`) and the API response. `analyze` exits 2 when incomplete and 1
under `--check` for P0/P1 findings that are not low confidence. When you add a diagnostic
that means "we could not look", add its code to that set. Advisory codes
(`SQL_NOT_PARSED`, `SQL_UNSUPPORTED_STATEMENT`, `IMPORT_CONFIG_PARTIAL`, `SQL_DIALECT_NOT_POSTGRES`) deliberately
stay out of it. So do findings about the target that the reader fully understood:
`SQL_PLPGSQL_OUTSIDE_BLOCK` (warning: a top-level `RAISE`/`PERFORM`/`IF … THEN`… in a
`.sql` script, which PostgreSQL rejects), `SQL_UNKNOWN_COLUMN` (warning: a column
the declared schema does not have), `SQL_INJECTION_RISK` and `API_METHOD_MISMATCH`.
Coverage choices the user controls stay out too: `DATA_FILE_SKIPPED` (a non-source file
over `max_file_bytes`), `GITIGNORE_UNAVAILABLE` and `API_PREFIX_ALREADY_RESOLVED`. The link
diagnostics `UNRESOLVED_LOCAL_IMPORT`, `UNRESOLVED_ROUTER_MOUNT` and `ROUTER_MOUNT_CYCLE`
(`analysis._RESOLUTION_CODES`) count only outside test code: evidence in a path
`core.files.is_test_code` accepts (a tests/, test/, `__tests__`/, e2e/ or cypress/
directory, or a test file name, in any language) is not a served application. Parse errors
and skipped files in test code still count. `core.files.is_test_path` is broader (spec/,
fixtures/, testdata/ too) and serves precision choices such as the column check; any
completeness exemption must use `is_test_code`.

`SQL_UNKNOWN_COLUMN` (`impact/columns.py`) favours precision. `add_sql` records column
references and DDL on `Graph.sql_facts` (never serialised), and `check_columns` runs
right after `_resolve_js_stores`. It checks only references attributable to exactly one
physical table, against the Prisma schema the project uses (`package.json` `prisma.schema`,
`prisma.config.*` `schema:`, else `schema.prisma`/`prisma/schema/`; stray `.prisma` files
count only when no rule matches and they agree) or, for tables without a model,
`CREATE TABLE`/`ALTER TABLE` in scanned SQL. Columns `ALTER TABLE` adds are unioned into
a Prisma model's. `CREATE TABLE` catalogs are per package (nearest ancestor with `package.json`,
`pyproject.toml` or `setup.cfg`), but what only suppresses a report applies repository-wide,
because packages usually share a database: columns `ALTER TABLE` adds, tables run-time DDL
reshapes, and an `EXECUTE`d `ALTER TABLE` whose target is computed (`format('%I')`), which
disables every column set. DDL and schemas in test code or fixtures declare nothing; an
unqualified name never resolves through `pg_schemas`. Prisma `view` blocks have unknown
columns (Prisma does not own the view's SQL). The Prisma provider is decided per package
(`columns.POSTGRES_PRISMA_PROVIDERS`, CockroachDB included, is shared with
`scanner._scan_prisma_schemas`, which uses the same schema selection for client accessors).
Not checked: references in migration directories or test paths (`core.files.is_test_path`: tests,
spec, e2e, cypress, fixtures, `*.test.ts`...), dynamic SQL, CTEs, derived tables,
ORM/Alembic-declared tables, tables Knex/Sequelize/TypeORM/Django code creates, alters or
maps (`_orm_ddl_tables`), tables that run-time DDL (`EXECUTE 'ALTER TABLE t …'`) names,
and unqualified names in a file whose SQL sets `search_path` (`SET search_path`, `SET SCHEMA`,
`set_config('search_path', …)`; not inside a string literal or comment, not a function or
procedure `SET` clause), or anywhere when a role/database default sets it, or a Python/JS
SQL string or `-c search_path=` connection option does (comments and UI copy do not). An
unqualified `CREATE TABLE` after the file's `SET` declares nothing; one before it still declares. A DDL-only (non-Prisma) catalog is also not trusted for tables that
source this scanner does not parse creates or alters (`_foreign_ddl_tables`: Rails, Laravel,
EF Core, Ecto, Go/Java/Rust SQL strings, Liquibase changelogs); no DDL catalog is trusted when such
a file runs an `ALTER TABLE` whose target is computed (`%s`, `" + name`, `${…}`), when a
migration or changelog names no table literally, or when a migration cannot be read. A
signal word with no readable table outside a migration (UI copy, comments, query builders)
is ignored, and run-time DDL in test code does not taint the catalog. These lexical reads run only
when there is something to report. Neither is a table the same file itself creates
or alters (a one-off script is written against the shape it makes). It emits one issue per SQL location listing all
of that statement's unknown columns (`subject` is `table.col, table.col`). Any failure in
that recording is swallowed: the statement is just not checked.

Two other sources also make an analysis incomplete. A `scan/` check that cannot read one
file (e.g. `RecursionError`) emits a `<tool>/could-not-scan` finding instead of failing
the whole tool, and any such finding makes the analysis incomplete. Python files the
checks never opened are attached as `ToolRun.notes`, which `to_sarif` reports as
`executionSuccessful: false`. The same rule applies in
`report/runner.py`: an unavailable tool is SKIPPED, a crash is ERROR, and neither counts
as zero findings.

### Findings and reports
`core/findings.py` defines `Finding`/`ToolRun`, the severity × confidence × exposure
priority score (P0–P3), fingerprints, baselines/ratchets and SARIF/Markdown/JSON output.
`scan/` holds the built-in Python/Alembic checks (security, performance, migrations,
mounts, wiring, deploy). Reachability lowers exposure but never hides a finding.
`wiring.route_exposure` marks route files nothing imports as "unreachable". With
`[scan] deployment_detection` (on by default), `deploy.py` reads manifests as text:
systemd, Dockerfile, block-style compose, Procfile, package.json, supervisord and `*.sh`
command lines (not echo/comments/heredocs), following in-repository scripts and Python run
scripts. If a non-auxiliary Python server target resolves to exactly one file and
`discover` reports no blocking target, a route file that only non-deployed apps reach
becomes "undeployed". It fails closed: an unnamed or ambiguous app (auxiliary files
included), an unreadable or unparsable manifest, an image whose command is not in the
repository (`_INERT_IMAGES` excepted), a run script changing `sys.path`/cwd, a symlink
leaving the root, CI deploy commands, and any format the module does not interpret
(Kubernetes, Helm, `app.yaml`, `fly.toml`, server lines in Makefiles, ...) all block, and
any exception in `discover` means no demotion. Auxiliary evidence (package.json scripts,
tests/CI/dev files, `--reload` servers) is live but never makes the deployment known.
`wiring` follows constant `import_module` calls; a reachable file that imports modules
chosen at run time makes reachability unknown. The appended note is
left out of the fingerprint. `security.scan`/`performance.scan` apply both through
`wiring.mark`. `report/runner.py` (`repolens report`) aggregates every tool, including
external ruff/bandit/semgrep, into one prioritised report under `.repolens/report/`.
`report/sarif.py` imports external SARIF.

The default tool list must stay offline: `osv-scanner` queries api.osv.dev and `pip-audit`
queries PyPI, so both are `--with` only, and `impact` is out for cost. `semgrep` IS a default
although it is optional to configure, because its adapter refuses a registry config and can
only run local rules; until `semgrep_config` is set it reports itself SKIPPED with that
reason, which a tool merely absent from the list cannot do.

`[report] sarif_commands` is the seam for any analyser that writes SARIF (trivy, gitleaks,
grype, checkov, hadolint): `{output}` in its argv becomes a temporary path, the document is
read back through `report/sarif.py` (which opens nothing the document references), and each
gets its own `ToolRun`, so a missing binary is SKIPPED and a crash is ERROR. An exit outside
`ok_exit` (default `[0, 1]`, because scanners exit 1 for "found something"), an argv without
`{output}`, and a clean exit that wrote no document are all errors: a tool that did not look
must never read as a clean repository. It is selected by the tools list under the name
`sarif-commands` — like `commands`, it is a `_PSEUDO_TOOLS` entry rather than an adapter, so
`--only` scopes it and `--list-tools` shows it.

The `lessons` tool (`report/lessons.py`) matches `repolens/lessons/catalogue.json` — 260
stack-level traps, package data so it travels into a target repository — against the
repository under report. It is precision-first in two ways that must not be relaxed
casually. `runnable_pattern` refuses any recipe whose scope or condition is written for a
person (a trailing `  -- in *.example` or `(outside the classifier)`, or prose in the body:
two consecutive plain lowercase words): the regex is then only half the rule, and running
that half reports the harmless general case everywhere. And every finding is
`confidence="low"`, which caps it at P2 and so below the default `fail_on = "P1"`, with a
20-match cap per lesson; a catalogue recipe is a prompt to go and look, never a verdict.
Lessons are scoped to stacks the repository shows evidence of (suffixes, dependency
manifests read bounded, `alembic/`, `.github/workflows`) and to the suffixes their category
can match, and the walk drops untracked gitignored files like `python_ast.python_files`.
`lessons.survey()` returns both the findings and the larger APPLICABLE set, and the runner
writes that set to `lessons.md` beside the report: roughly two thirds of applicable lessons
have no runnable recipe, and the file marks each one "not checked here" so silence about it
is never read as a pass. `docs/lessons/` holds the browsable page and the generated rule
packs; `export_lessons_learnt.py` regenerates both from the package copy.

`report --require a,b` is an assertion by the invocation: a named tool that is skipped,
errors or is not selected exits 1 in every mode and refuses `--update-baseline` (exit 2).
`[report] require` only fails `--check`. `Context.executable` searches
`REPOLENS_TOOLS_BIN`, the `venv_python` dir, `Path(sys.executable).parent`, then PATH.

The Python checks share one `ParseCache` per run, on `ScanSettings.parse_cache`
(`dataclasses.replace` copies share it, and `from_config` makes a fresh one). Read files
through `python_ast.parsed_files(s)` or `s.parse_cache.parse(...)`, never through the
module-level `parse()`: that one is a 256-entry fallback, and security and performance
each parse the whole tree three times (`route_exposure`, `MountIndex`, their own loop).
`python_files(s)` drops untracked gitignored files (`respect_gitignore` from `[impact]` and
`.impact-tracer.json`, read into `ScanSettings.respect_gitignore`; the git listing is `core.git.untracked_ignored`, shared
with the graph scan). Under `analyze` the admitted inventory comes from the graph, which
already applied it. Entries are keyed on path and SHA-256 of the bounded read. Trees are shared, so checks must
not mutate them. Whole-tree indexes go through `ParseCache.derived(name,
run_inputs(s, trees), compute)`, as `wiring.route_exposure` and `mounts.mount_index(s)` do.
Use `mount_index(s)`, not `MountIndex(s)`, so security and performance build it once per
run. If an index starts reading another settings field, add that field to `run_inputs`.
`python_ast.may_mention(tree, word)` checks the source text before a whole-tree `ast.walk`
for a name. It only answers False when the source is ASCII.

### Function Lens (`repolens/lens/`)
`[lens] javascript_parser` is `"regex"` (default) or `"tree-sitter"` and is never
auto-detected, because the committed digest must not depend on installed extras. A
configured tree-sitter that cannot import raises `SystemExit`. Regex records must stay
byte-identical, since `content_sha256` hashes `functions` and existing committed digests
were regex-built. The digest carries `javascript_parser` outside the hash (a missing one
counts as regex). `lens --check` exits 2 with a "committed with X, this run would use Y"
message before building when they differ, and exits 1 only for real staleness.

### Local API (`repolens/api/app.py`)
`create_app(root, token)` serves one fixed checkout. It uses TrustedHost with loopback
only, a bearer token of at least 32 chars compared in constant time, and `/health` is the
only unauthenticated route. The latest `Analysis` lives in process memory behind a
non-blocking lock: a concurrent scan gets 409, and queries before a scan get 409.
`/v1/impact` queries the stored graph with `search_source=False` so it never re-reads a
newer working tree. Request models use `extra="forbid"`, so callers cannot pass paths,
commands or plugins. Error responses must not leak paths or exception text.

### Other command families
These come from the original toolkit and are configured through `repolens.toml`:
`lens` (function index / similarity search), `owners` (capability index in
`canonical_owners.yaml`), `featuretrace` (`@featuretrace:` markers → maps), `artefacts`
(generated-file merge driver/regeneration), `gates` (every check is actually run), `docs`
(docstring coverage ratchet, pdoc/TypeDoc), and `init` (`bootstrap.py`, which writes
`repolens.toml`, rules docs and the CI workflow from `repolens/templates/`). The portable
rules each command enforces are in `repolens/rules/*.md`. `templates/` and `rules/*.md`
ship as package data.

`docs generate` (`docs/generate.py`) and `featuretrace propose` (`featuretrace/propose.py`)
turn one `analyze()` graph into documentation and draft markers. Both group files through
`impact/features.py` `feature_groups`, so they agree on route areas. Neither may invent a
schema, a purpose or an owner: unresolved parts stay as listed gaps, drafts say `(draft)` or
`TODO(repolens)`. Only `propose --apply` changes the scanned repository's files, under its hash,
git-clean and in-root checks; both commands otherwise write under `.repolens/` in the checkout
(excluded from the scan) or `--out`, never through a symlink, and never over a file without their
build stamp.

## Documentation conventions
Docs are deliberately conservative. Capabilities not backed by an implementation and a
regression test belong in `docs/roadmap.md`, not the README. Known open issues and pending
work, each with its analysis, required fix and reason, are tracked in `docs/tasks.md`; move
a task to its Resolved table (with the verifying test) when it lands. `tasks/` holds the
same kind of record for follow-ups a single feature left behind, one file per feature, so a
change can land with its own debts written down instead of enlarging the audit register. Scope limits are listed in
`analysis.LIMITS` and `docs/audit/`. Keep those in sync when behaviour changes.
