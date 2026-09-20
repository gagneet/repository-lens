# Installation and CI

Repository Lens is a Python package that analyzes one local checkout. It does not
clone, install or execute the checkout being analyzed.

The 0.2 version of this page, including "Making it a separate repository" (the split,
packaging, trusted publishing, the Action, the CI matrix and the hardening checklist) and
the baseline, merge-hook and removal procedures, is kept verbatim in
[history/installation-0.2.md](history/installation-0.2.md). Upgrade notes for each
release are in [CHANGELOG.md](../CHANGELOG.md).

## Requirements and optional extras

| Extra | Provides | Use it for |
|---|---|---|
| *(none)* | Python 3.11+ standard-library core | existing FeatureTrace, lens, owners and lower-level commands |
| `stack` | Tree-sitter JavaScript/TypeScript grammars and SQLGlot | focused JS/TS/Next.js/PostgreSQL analysis |
| `api` | FastAPI and Uvicorn | local authenticated API, Swagger UI and ReDoc |
| `test` | HTTPX and PyYAML | API tests and the full test suite |
| `yaml` | PyYAML | owners/capability files |
| `docs` | pdoc | HTML Python documentation |

Install from this checkout:

```bash
python -m pip install -e '.[stack,api]'
```

For development and tests:

```bash
python -m pip install -e '.[stack,api,test,yaml,docs]'
```

The package has no required runtime dependency. If `stack` is absent, `impact` reports a
degraded JavaScript parser; `analyze` exits with an actionable dependency message rather
than claiming full coverage.

## First run

From the repository to inspect:

```bash
repolens analyze --out .repolens/analysis
```

Use `repolens --root /path/to/checkout analyze` when the command is launched elsewhere.
The report writes JSON, Markdown, HTML, Mermaid and SARIF under the chosen output directory.

Start the local API only when you need an interactive client:

```bash
export REPOLENS_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
repolens --root /path/to/checkout serve
```

The service listens on `127.0.0.1` and retains only the latest scan in memory. See
[`api/README.md`](api/README.md) for Swagger/OpenAPI and Postman usage.

## Building a release

The build backend is setuptools with no plugin, so a wheel needs no script of its own:

```bash
python -m pip install build          # once, in the build environment
python -m build                      # dist/repolens-<version>-py3-none-any.whl and the sdist
```

The wheel is `py3-none-any`: the package itself has no compiled extension and no required
runtime dependency, so one wheel serves Linux and macOS, and the `stack` extras
(Tree-sitter grammars, SQLGlot) publish platform wheels for both, Apple Silicon included.
The package data that has to travel with it — `repolens/templates/`, `repolens/rules/*.md`
and `repolens/lessons/catalogue.json`, which `init`, `rules` and `report --only lessons`
read inside a *target* repository — is declared in `[tool.setuptools.package-data]`. When
a new data file is added under `repolens/`, add it there too, or it will work from a
checkout and be missing from the wheel.

The version has one home, `repolens/__init__.py`. `pyproject.toml` declares
`dynamic = ["version"]` and reads that attribute, so the wheel filename,
`repolens --version`, the provenance stamp in every report and the API's OpenAPI
version cannot drift apart.

Releasing:

1. Bump `__version__` in `repolens/__init__.py`.
2. Move the `[Unreleased]` section of [CHANGELOG.md](../CHANGELOG.md) under the new
   number, with upgrade notes, and move any documentation section the release removed
   into `docs/history/` verbatim.
3. `python -m unittest discover -s tests -q` with the extras installed — without them
   about a third of the suite skips.
4. `repolens api export --out docs/api && git diff --exit-code -- docs/api`.
5. Tag, then `python -m build`.

## Installing it as a standalone tool

A checkout is not needed to run the tool. An isolated install keeps repolens out of the
Python environment of the repository being analyzed, which matters because the scanner
must never share an environment it might be asked to read:

```bash
pipx install 'dist/repolens-0.3.0-py3-none-any.whl[stack,api]'
# or straight from the source:
pipx install 'repolens[stack,api] @ git+https://github.com/<owner>/repository-lens@v0.3.0'
repolens --root /path/to/checkout analyze --out .repolens/analysis
```

`uv tool install` takes the same arguments. `bin/repolens` runs the tool from a checkout
with nothing installed at all, which is what a vendored copy uses.

There is no frozen single-file build (PyInstaller, zipapp). `provenance._source_sha256`
hashes the package's files on disk to stamp each output, so a frozen build would need
work for no gain over an isolated install; it is only worth doing for a machine with no
Python 3.11.

## Bootstrap a target repository

`init` detects common source/test/migration directories and writes a starter
`repolens.toml`:

```bash
repolens --root /path/to/checkout init --dry-run
repolens --root /path/to/checkout init --ci github
```

Review the generated paths and rules before committing them. Repository configuration is
data, not target application code, and artifact paths must remain inside the checkout.

## CI

Run the deterministic test suite and focused analysis in CI:

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with:
    python-version: '3.11'
- run: python -m pip install -e '.[stack,api,test]'
- run: python -m unittest discover -s tests -q
- run: repolens analyze --check --out .repolens/analysis
```

Treat `complete=false`, parser failures, file limits and unreadable files as review
signals. A clean report is not proof of runtime safety. Pin the package version or commit
in production CI and regenerate `docs/api/*` whenever the API changes:

```bash
repolens api export --out docs/api
git diff --exit-code -- docs/api
```

The existing `action.yml` is an optional composite action for the lower-level impact
report. It does not provide remote-repository access or a hosted service.

## Upgrading and troubleshooting

- Check `repolens --version` and installed parser versions with `python -m pip show
  tree-sitter tree-sitter-javascript tree-sitter-typescript sqlglot`. Parser versions
  contribute to the graph's configuration fingerprint.
- Delete a stale `.impact-tracer/index.json` only when you want to force a rebuild;
  content/config fingerprints normally invalidate it automatically.
- Increase `max_file_bytes` or `max_files` deliberately, understanding that larger
  values consume more memory/time. The API imposes tighter request bounds.
- If Tree-sitter or SQLGlot is missing, install `.[stack]` in the same environment that
  runs `repolens`.
- SARIF producer files are imported as evidence only. External property files and
  unsuccessful producer runs remain incomplete and cannot be baselined.
- `repolens docs build` imports each module pdoc documents before running pdoc. A module
  whose third-party dependency is not installed (for example `repolens.api.app` without
  the `api` extra) is left out and named as `NOT DOCUMENTED, dependency not installed`,
  so a `"!repolens.api"` entry in `[docs.python] modules` is no longer needed. Set
  `[docs.python] missing_dependency = "fail"` to make that a failure. An import error
  inside the documented packages themselves always fails the build.
- Upgrading a vendored copy: read the release's upgrade notes in
  [CHANGELOG.md](../CHANGELOG.md) first. Then replace the whole directory and refresh
  `VENDORED.json` (below), rather than merging file by file.

### Checking repolens itself

A workflow for this repository should run the gates it asks targets to run, against its
own package. Run them in two environments: once with only `requirements-ci.txt`, which
proves the docs build survives a missing optional extra, and once with
`.[stack,api,test,docs]`:

```yaml
- run: python -m pip install -e . -r requirements-ci.txt
- run: python -m unittest discover -s tests -q
- run: repolens docs coverage --check          # needs a committed .repolens/docstring_baseline.json
- run: repolens docs build --python --strict   # needs [docs.python] modules = ["repolens"]
```

The repository has no workflow or `repolens.toml` of its own yet. Until both are
committed, these commands run only by hand.

## Vendoring a copy

A repository that vendors repolens as a plain copy, instead of installing a pinned
package, should record what it copied. Put `VENDORED.json` at the top of the vendored
directory:

```json
{
  "schema": 1,
  "name": "repolens",
  "upstream": "https://github.com/<owner>/repository-lens",
  "version": "0.3.0",
  "commit": "<40-hex upstream commit SHA>",
  "tree": "<40-hex git tree hash of that commit's root: git rev-parse <commit>^{tree}>",
  "path": "tools/repolens",
  "vendored_on": "2026-09-13",
  "local_changes": []
}
```

`commit` says which upstream revision was copied. `tree` lets anyone check the copy
without trusting the record: git hashes a directory from its file names, modes and
contents, so an unmodified copy has exactly the upstream root tree hash. The record
itself is left out of that hash. `local_changes` lists any file deliberately patched
after copying, with the reason; a non-empty list means `tree` will not match.

Manual procedure, from the consumer's repository root (here `tools/repolens`):

```bash
UP=/path/to/repository-lens            # a clean checkout at the release to vendor
git -C "$UP" status --short             # must print nothing
COMMIT=$(git -C "$UP" rev-parse HEAD)
TREE=$(git -C "$UP" rev-parse 'HEAD^{tree}')

git rm -r -q --cached tools/repolens 2>/dev/null; rm -rf tools/repolens
mkdir -p tools/repolens
git -C "$UP" archive HEAD | tar -x -C tools/repolens   # tracked files only
git add tools/repolens

# The copy's tree hash, without VENDORED.json, must equal $TREE before the record is written.
test "$(git write-tree --prefix=tools/repolens/)" = "$TREE" && echo "copy matches $COMMIT"
# ... write tools/repolens/VENDORED.json with $COMMIT and $TREE, then git add it and commit.
```

To verify a committed copy later:

```bash
git ls-tree HEAD:tools/repolens | grep -v $'\tVENDORED.json$' | git mktree
# prints the tree hash; compare it with "tree" in tools/repolens/VENDORED.json
```

The check fails if a `.gitattributes` text or eol conversion in the consumer rewrites
files as they are added, or if an ignore rule there drops a file. In both cases the copy
really does differ from upstream. A `repolens vendor verify` command that automates this
is planned in the [roadmap](roadmap.md). It is not implemented.

## Security posture

Run local analysis with reviewer-level permissions, not production credentials. The
scanner uses bounded UTF-8 reads, rejects source symlinks and artifact paths that escape
the root, never invokes target code, and keeps plugin loading explicit. Plugins are
trusted in-process Python and are not a sandbox; hosted provider checkouts require a
separate isolated worker design and are not part of this release.
