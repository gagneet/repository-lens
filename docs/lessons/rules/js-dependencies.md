# JS Dependencies & Tooling: lessons learnt

**Scope.** Yarn/npm, lockfiles, CVEs, upgrades.

| id | severity | lesson |
|---|---|---|
| JD-001 | medium | Yarn 1 `yarn upgrade <pkg>` is a silent no-op for transitive dependencies |
| JD-002 | low | Stale or contradictory `resolutions` / `overrides` pins |
| JD-003 | medium | Two lockfiles for one manifest, or a hand-edited package.json, break frozen installs |
| JD-004 | medium | Dependabot groups by directory, so a dependency declared in two manifests drifts |
| JD-005 | high | Minor bumps break types that mocked tests cannot see |
| JD-006 | medium | Major bumps silently change defaults, attributes and module paths |
| JD-007 | low | Removing a UI library: runtime-generated classes, same-named components, duplicate majors |
| JD-008 | high | npm resolves a peer conflict by overriding it, and only warns |

## JD-001 — Yarn 1 `yarn upgrade <pkg>` is a silent no-op for transitive dependencies

*Severity:* **medium** · *Stacks:* yarn

**Symptom.** A security alert 'fix' exited successfully and changed nothing.

**Root cause.** Yarn 1 only upgrades direct dependencies with that command.

**Resolution.** Delete the stale `yarn.lock` entries and run `yarn install` to re-resolve within the existing ranges.

**Prevention.** After an alert fix, verify with `yarn why <pkg>` and check that `yarn.lock` actually changed.

**How it is checked.**

- `ci`: after a dependency-fix PR, assert the named package's lock entry changed

**Evidence.** 55b6da2e5 (2026-09-11); 5e64313af (2026-09-07)

## JD-002 — Stale or contradictory `resolutions` / `overrides` pins

*Severity:* **low** · *Stacks:* yarn, npm

**Symptom.** Nine packages were pinned twice (yarn and npm). One pin contradicted its parent's declared range and was silently ignored.

**Root cause.** Pins accumulate and are never re-derived.

**Resolution.** Removed the redundant and contradicted pins.

**Prevention.** For each pin, compare against every parent's declared range: flag 'already satisfied' and 'unsatisfiable'.

**How it is checked.**

- `shell`: `for each resolutions/overrides entry: npm ls / yarn why and compare against parent ranges`

**Evidence.** f7768c14c (2026-08-25)

## JD-003 — Two lockfiles for one manifest, or a hand-edited package.json, break frozen installs

*Severity:* **medium** · *Stacks:* yarn, npm

**Symptom.** `--frozen-lockfile` failed in CI.

**Root cause.** `package.json` was edited without re-locking, and both `package-lock.json` and `yarn.lock` existed.

**Resolution.** Keep one package manager and one lockfile per directory.

**Prevention.** CI runs `yarn install --frozen-lockfile` / `npm ci`.

**How it is checked.**

- `shell`: `a directory containing both package-lock.json and yarn.lock`

**Evidence.** 0bfa6dc1a; b90606cd6 (2026-09-01)

## JD-004 — Dependabot groups by directory, so a dependency declared in two manifests drifts

*Severity:* **medium** · *Stacks:* dependabot, mermaid

**Symptom.** The diagram-lint gate parsed with mermaid v11 while the browser rendered with v12, so the gate went green on syntax the browser could not draw.

**Root cause.** The root manifest (lint) and `frontend/` (runtime) each declared mermaid, and Dependabot bumped only one.

**Resolution.** Bump both copies in one commit, with a warning comment in the workflow.

**Prevention.** Fail CI when a package appears in several manifests with different major versions, and especially when a gate's version differs from the runtime's.

**How it is checked.**

- `shell`: `jq -r '.dependencies.mermaid // .devDependencies.mermaid' package.json frontend/package.json | cut -d. -f1 | sort -u | wc -l  # must be 1`

**Evidence.** 8257196fe; 8e7449186 (2026-09-15)

## JD-005 — Minor bumps break types that mocked tests cannot see

*Severity:* **high** · *Stacks:* recharts, nextjs, typescript

**Symptom.** A recharts minor release widened the Tooltip formatter's types (`ValueType`), and Next 16.3 needed a newer `@swc/helpers`. Jest mocked recharts, so only `tsc` / `next build` caught it.

**Root cause.** A caret range accepted a breaking minor release.

**Resolution.** Used exact pins to hold versions back, and added a Dependabot `ignore` entry that states its reason. Introduced a shared `ChartValue` type.

**Prevention.** Every dependency PR must pass a clean `next build`.

**How it is checked.**

- `ast`: formatter callbacks annotated (v: number) passed to recharts formatter

**Evidence.** 3ca08b858; f8209b7e6 (2026-08-18); 7d6a0e283 (2026-09-01)

## JD-006 — Major bumps silently change defaults, attributes and module paths

*Severity:* **medium** · *Stacks:* react-pdf, mermaid, react-resizable-panels

**Symptom.** react-pdf 11 enabled Suspense by default. mermaid 12 switched layout engine and look. react-resizable-panels 4 renamed exports and dropped `data-panel-group-direction`, so Tailwind `data-[…]` selectors became dead. A copy script with a hardcoded nested `node_modules/x/node_modules/y` path broke when the dependency was hoisted.

**Root cause.** Defaults and DOM attributes are part of a library's contract but are not type-checked.

**Resolution.** Read the changelog and resolve paths with `require.resolve(dep, {paths:[require.resolve(consumer + '/package.json')]})`.

**Prevention.** Grep for hardcoded nested `node_modules` paths, and for `data-[attr]` selectors whose attribute is no longer emitted.

**How it is checked.**

- `regex`: `node_modules/[^/]+/node_modules/`
- `regex`: `data-\[[a-z-]+`

**Evidence.** 8e7449186; 7d6a0e283

## JD-007 — Removing a UI library: runtime-generated classes, same-named components, duplicate majors

*Severity:* **low** · *Stacks:* tailwind, tremor, shadcn

**Symptom.** A 475-line CSS block hand-declared the removed library's classes. Tremor `TableHead` is `<thead>` while shadcn `TableHead` is `<th>`. A prefix replace produced `<TableHeadererCell`. The old library had pulled in two majors of a charting library.

**Root cause.** Library ports assume same names mean the same semantics. Naive codemods replace the shorter name first.

**Resolution.** Mapped each component explicitly, replacing the longest names first.

**Prevention.** Check the lockfile for duplicate majors of one package.

**How it is checked.**

- `shell`: `yarn why <pkg> | grep -c 'Found' (more than one major)`

**Evidence.** 8d7419b26; 0bfa6dc1a; bb66813f6 (2026-06-28)

## JD-008 — npm resolves a peer conflict by overriding it, and only warns

*Severity:* **high** · *Stacks:* npm, node, dependencies

**Symptom.** `npm install pkg@latest` printed 15 `npm warn ERESOLVE overriding peer dependency` lines and exited 0. The tree then held a second, older copy of the conflicting package nested under the offender, and `npm ls --all` reported it `invalid:`.

**Root cause.** npm 7+ installs peer dependencies automatically and, when they conflict, prefers completing the install to failing. The result is a tree that does not match what the manifests ask for, announced only in a warning that scrolls past in CI output.

**Resolution.** Treat an ERESOLVE warning as a failure. Either hold the dependency at the last version that resolves cleanly, or wait for the upstream package to widen its peer range — the block is usually one transitive package pinning an old major.

**Prevention.** Gate on a clean resolve, not on the exit code. `--legacy-peer-deps` and an `overrides` block silence the same problem without fixing it.

**How it is checked.**

- `shell`: `rm -rf node_modules && npm ci 2>&1 | grep -c 'ERESOLVE\|Conflicting peer'`
- `shell`: `npm ls --all 2>&1 | grep -c 'invalid:'`
- `regex`: `legacy-peer-deps|\"overrides\"\s*:`

**Evidence.** retirement_calculator_au (2026-09-20 audit): @babel/core 8 held at 7 because babel-preset-current-node-syntax still depends on @babel/plugin-syntax-*@^7

**Sources.** <https://docs.npmjs.com/cli/v10/commands/npm-install>
