# Documenting an undocumented application

Use this workflow when an application has little or no documentation, for example one
generated with an AI assistant: a Next.js/React frontend, API routes or a FastAPI backend,
and PostgreSQL or MongoDB. Every step reads the checkout without importing or running it.
Only `featuretrace propose --apply` changes the repository's files. The other commands write
under `.repolens/` in the checkout by default (a directory the scan excludes), or where `--out`
says.

What static evidence can and cannot give you decides how each output is used:

| It can show | It cannot show |
|---|---|
| Which pages call which API routes, which handlers serve them, and which tables and collections the code reads or writes | Why the feature exists, or its business rules |
| Route paths, methods and path parameter names as declared | Request and response shapes the source does not declare as a model |
| Columns and foreign keys declared in SQL DDL or a PostgreSQL Prisma schema | MongoDB document fields, or columns an ORM creates at run time |
| Calls with no handler, wrong methods, handlers nothing calls, request input spliced into SQL | Authorization, middleware and runtime configuration |

## 1. Find what is broken

```bash
repolens analyze --out .repolens/analysis
```

On an undocumented application the most useful diagnostics are usually
`API_CALL_WITHOUT_HANDLER`, `API_METHOD_MISMATCH` (a likely 405),
`API_HANDLER_WITHOUT_STATIC_CALLER`, `SQL_INJECTION_RISK` and `SQL_UNKNOWN_COLUMN`. Fix those
before documenting the code, or the documentation will describe the bugs.

## 2. Generate reference documentation

```bash
repolens docs generate                 # writes .repolens/docs-generated/
```

| Output | What it holds |
|---|---|
| `openapi.json` | OpenAPI 3.1 for the application's own routes, with source parameter names (`/api/orders/{orderId}`). Each operation names its handler, callers, stores and feature group. Every part the source does not declare is listed in `x-repolens-gaps` instead of being guessed; declared FastAPI models are named, not expanded. A route a generated router artifact declares is an operation with resolution `declared` and the gap "handler declared by an artifact, not scanned". Calls nobody serves are in `x-repolens-unserved-calls`; a call the scanner matched to a declared route is listed as that operation's caller, and calls from test code are not callers. Stores are those the handler reaches through import-bound or exact calls, never a name-only guess. A Pages Router API route answers every method, so it is under the path-item extension `x-repolens-any-method`, which Swagger UI and client generators do not show. |
| `schema.md`, `schema.mmd` | A Mermaid ER diagram of declared tables, columns and foreign keys, with MongoDB collections marked as having no inferred fields. SQL files apply in path order (how numbered migrations sort), so a later `DROP TABLE`, `DROP COLUMN`, `RENAME` or `SET NOT NULL` changes the drawing; a dropped constraint does not, and `public.orders` and `orders` are drawn as two tables. A foreign key shows "exactly one" parent only when its columns are `NOT NULL`. Tables with columns or foreign keys are drawn first; past 150 entities the rest are counted by kind, and `schema.md` lists every store with whether it was drawn, and the tables a later statement dropped or renamed. |
| `architecture.md`, `architecture.mmd` | Pages and clients, API, code and stores, one node per feature group. At most `--max-groups` groups (default 40), 60 stores (the ones most groups share) and 450 edges are drawn, because Mermaid refuses a flowchart with more than 500 edges; a comment counts what was left out. |
| `debugging.md` | A repository-wide starting point for static debugging, linked to the exact caller, handler and store candidates on each feature page, plus guidance for bounded runtime telemetry when static evidence is insufficient. It installs no instrumentation. |
| `features/<group>.md` | A mindmap (at most 25 leaves per section), evidence tables and an endpoint-by-endpoint debugging chain for one route area, including its diagnostics and what the source cannot tell you. |
| `features/<group>.context.json` | The same facts as data, including structured debugging trace points and safe runtime fields, for a person or an agent to write prose or instrumentation from. |
| `index.md` | Whether the analysis was complete, and the limits of each output. |

A feature group is a route area named by its first path segment (`/orders` and
`/api/orders/{orderId}` are both `orders`), not a business feature. An endpoint only test code
calls belongs to no group. The command exits 2 when the analysis is incomplete; the files are
still written and say so. It refuses to write through a symlink, and to replace or delete a file
in the output directory that does not carry its build stamp where the command writes it (the
first line of a Markdown page, the last line of a Mermaid file, the build field of JSON).

`repolens api export` is a different command: it documents Repository Lens's own local API,
not the scanned application.

## 3. Put FeatureTrace markers, Function Lens ids and JSDoc into the code

```bash
repolens featuretrace propose --jsdoc --function-lens --owners
# review .repolens/featuretrace-proposal/proposal.patch and proposal.json
repolens featuretrace propose --jsdoc --function-lens --tag orders=order-management
repolens featuretrace propose --apply
repolens featuretrace audit
repolens featuretrace map order-management --out docs/featuretrace
```

`propose` drafts one marker per page, route, service and model file in each feature group:
tag, `Layer:`, `Data flow:`, `Related:` and `Table:`/`Collection:`, with every role ending in
`(draft)`. Only files the `[featuretrace]` settings scan are drafted. Test code, minified or
generated files, files that already carry a marker, and files declaring an encoding that cannot
hold the marker are left alone, each with a reason in `proposal.json`.

What a draft may say:
- **Stores:** only the file's own tables and collections (a route file also those its handlers
  reach). A store edge counts unless it is ambiguous, a pattern match, or to an unverified store.
- **Code:** followed only through import-bound or exact calls; a dependency reached through a
  name-only call is left out rather than guessed.
- **Pages and routes:** linked by the scanner's route matches, except ambiguous ones and calls from
  test code; `proposal.json` records each link's resolution.
- **Pages with no HTTP route (Next.js):** a page that import-binds a function in another file and
  calls it, or hands it to a JSX attribute (`<form action={save}>`), flows through it to the
  stores it reaches: `action save` for an export of a `"use server"` module, `server load` for any
  other server function, and `queries in this file` for a server component's own SQL. Those files
  are `Related:`, and an action or server module's flow starts at the pages that call it. Only
  import-bound links count; a bare name inside one module binds to that module's top-level
  declaration (a shadowing local is not checked), and no name is matched across files.
- **`Related:`:** at most 8 files with a direct link, the group's own files first and `scripts/`,
  `migrations/`, `tools/`, `vendor/` last; `Related: none found (draft)` when there is none.
- **A file in several groups** takes the group with the most evidence. A route nobody calls says
  "no static caller found".

Each draft also records the issues `featuretrace audit` would report once every draft is applied
(`audit_issues`). A draft is not guaranteed to pass: the audit reads about 20 lines below a marker
and can take an import path there for a reference.

With `--jsdoc` it also drafts fact-only JSDoc for exported JS/TS route handlers and data-access
functions that have none. Python files get no docstrings, so a JS/TS frontend with no route
handlers or data access of its own gets none. Each block starts with
`TODO(repolens): describe what this does and who may call it.` `TODO\(repolens\)` is always a
`[docs] placeholder_patterns` entry, whatever the repository lists, so `repolens docs coverage`
keeps counting the symbol as undocumented until a person writes the purpose.

With `--function-lens` it drafts a one-line `@functionlens:` id on public route handlers,
functions that call an API or touch a store, and exported frontend entry functions. The id is
created from the original path and symbol, then read from the comment thereafter; keep the comment
with the declaration and Function Lens can find it after a rename or move. Duplicate ids are
reported in the index. The comment stores identity only, not inferred callers or data flow, so
changing code does not leave derived claims behind. It is a comment and has no runtime behavior.

`--apply` writes the `proposal.json` you reviewed. It never drafts again, and it takes no `--tag`,
`--only`, `--jsdoc`, `--function-lens` or `--owners`: tags and symbol ids are fixed when you draft.

It refuses the whole run (exit 2) when:
- there is no proposal;
- the proposal was drafted for another root or from an incomplete analysis;
- the proposal is malformed.

It skips and lists (exit 1) a file:
- whose bytes changed since the draft;
- whose section of `proposal.patch` you edited (apply an edited patch with `git apply`);
- that resolves outside the root;
- that git does not hold clean, unless you pass `--allow-dirty` (outside a git checkout, every file).

It keeps BOM, the file's line ending and the final newline. It inserts after a shebang, a Python
encoding declaration or a directive such as `"use client"`. Drafting exits 2 when the analysis is
incomplete (the proposal is still written) or a `--tag`/`--only` group does not exist. The proposal
directory is guarded like `docs generate`'s.

Then rename the draft tags and roles. `featuretrace audit` checks that each marker has its
required fields, sits near the top of its file and has references that resolve. It does not
check that the declared data flow is what the code does; compare with `repolens analyze`
evidence for that. `featuretrace map <tag>` renders one tag as a flowchart, mindmap, tour and
JSON graph (`featuretrace map --index` lists every tag).

## 4. Function Lens and capability owners

Function Lens still needs no markers: `repolens lens` builds a function index the repository
commits, and `repolens lens similar --clusters` lists groups of functions that may re-implement
each other, for a person to review. Optional `@functionlens:` ids give important boundary
functions a durable lookup key; use `repolens lens --lookup <id>` after a rename. `--owners` above writes `canonical_owners.draft.yaml` into the
proposal directory only, never into the repository: one concept per table or collection, with at
most 10 consumers listed. The suggested owner is the application file outside route handlers that
references it most, preferring files outside `scripts/`, `migrations/`, `tools/` and `vendor/`. Who
owns a concept is a decision; copy the entries you agree with into your capability index and
check it with `repolens owners --check`.

## 5. Write the prose

Hand `features/<group>.context.json` and the drafted JSDoc to a person or an agent to write
what the evidence cannot: purpose, business rules, authorization and the request and
response shapes. `repolens docs coverage --check` ratchets the result, and
`repolens docs build` renders TypeDoc and pdoc pages from it. Regenerate step 2 after the
code changes; the generated files carry the build that produced them.
