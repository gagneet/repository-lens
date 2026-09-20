# Open tasks: the lessons-learnt catalogue as a report tool

Follow-ups left behind by `repolens report --only lessons` (`repolens/report/lessons.py`,
catalogue at `repolens/lessons/catalogue.json`). Status and severity use the vocabulary of
[`docs/tasks.md`](../docs/tasks.md).

| ID | Task | Status | Severity |
|---|---|---|---|
| [LES-01](#les-01--only-91-of-324-detection-recipes-are-executed) | Only 91 of 324 detection recipes are executed | open | medium |
| [LES-02](#les-02--25-regex-recipes-are-refused-only-because-their-scope-is-prose) | 25 regex recipes are refused only because their scope is prose | open | medium |
| [LES-03](#les-03--lessons-are-matched-against-test-code-with-no-per-category-decision) | Lessons are matched against test code, with no per-category decision | decision | low–medium |
| [LES-04](#les-04--lessonsmd-carries-no-build-stamp) | `lessons.md` carries no build stamp | open | low |
| [LES-05](#les-05--no-documented-adoption-path-for-an-existing-repository) | No documented adoption path for an existing repository | open | low |
| [LES-06](#les-06--stack-detection-has-no-negative-evidence) | Stack detection has no negative evidence | open | low |
| [LES-07](#les-07--the-catalogue-has-no-schema-check-in-the-suite) | The catalogue has no schema check in the suite | open | low |

---

## LES-01 — Only 91 of 324 detection recipes are executed

**Status:** open · **Severity:** medium

**Analysis.** The catalogue's 260 lessons carry 324 detection recipes of nine kinds:

| kind | count | run today |
|---|---:|---|
| `regex` | 116 | 91 |
| `ast` | 59 | no |
| `review` | 48 | no — needs a person, correctly |
| `shell` | 29 | no |
| `test` | 18 | no |
| `ci` | 18 | no |
| `sql` | 17 | no — needs a live catalog, which repolens never connects to |
| `lint` | 10 | no |
| `config` | 9 | no |

Only `regex` is executed, and only 91 of those. `review` and `sql` should stay
unexecuted: the first needs
judgement, and the second would need a database connection, which the architecture forbids.
The interesting gap is **`ast` (59 recipes)**, which are semgrep patterns in all but
syntax — the report already has a `semgrep` adapter and `[report] semgrep_config` points at
a local rules directory.

**Required fix.** Add an exporter (extend `docs/lessons/export_lessons_learnt.py`) that
writes a semgrep rule pack from the `ast` recipes, one rule per lesson, with `id` =
the lesson id, `severity` mapped from the lesson, and `metadata.confidence: LOW` so the
existing `_semgrep` adapter carries the confidence through. Ship it as package data next to
the catalogue so `semgrep_config` can point at it without a checkout. Recipes that are not
valid semgrep patterns must be refused by the exporter with a named error, not skipped
silently — the same rule `runnable_pattern` follows.

**Why it matters.** `lessons.md` currently says "not checked here" against roughly two
thirds of applicable lessons. Each `ast` recipe converted is a lesson that moves from a
checklist item to a check.

**Verification.** A test that the exporter refuses a malformed pattern; a probe repository
with one `ast`-recipe trap that `repolens report --only semgrep` reports once.

**Research needed.** Whether the `ast` recipes were written against a specific tool's
syntax. Read a sample of 10 before designing the exporter.

---

## LES-02 — 25 regex recipes are refused only because their scope is prose

**Status:** open · **Severity:** medium

**Analysis.** `runnable_pattern` refuses a recipe whose scope is written for a person. Run
this to list them:

```python
from repolens.lessons import load
from repolens.report.lessons import runnable_pattern
for l in load()["lessons"]:
    for d in l["detection"]:
        if d["kind"] == "regex" and runnable_pattern(d["recipe"]) is None:
            print(l["id"], d["recipe"])
```

25 of 116 are refused. Roughly 15 of those carry a scope that is mechanical, not a
judgement — `-- in *.example`, `(in tests/ or modules tests import)`, `-- outside the
classifier`. The regex itself is fine; only the scope cannot be read.

**Required fix.** Add two optional keys to a detection entry in the catalogue schema:

```json
{"kind": "regex", "recipe": "...", "include_glob": ["*.example"], "exclude_glob": ["tests/**"]}
```

Then in `lessons.py`: accept a recipe whose *qualifier text* is present when a glob is also
present (the glob supersedes the note), and filter candidate paths through
`PurePosixPath.match` before matching. Bump `schema_version` in the catalogue and add the
keys to the `CatalogueShipsWithThePackage` structural test.

**Why it matters.** `SE-001` (a live secret in a committed `.env.example`) is `critical` and
is currently not checked at all. Scoped to `*.example` it is precise.

**Verification.** A probe with a matching string in `config.example` and the same string in
`src/app.py`; only the first is reported.

**Do not** relax `runnable_pattern` to run these recipes unscoped. That was tried during
development and `SE-001` matched every long assignment in the repository, including inside
`site-packages`.

---

## LES-03 — Lessons are matched against test code, with no per-category decision

**Status:** decision · **Severity:** low–medium

**Analysis.** `lessons.scan` walks every admitted file, including test code.
`core.files.is_test_path` exists and serves exactly this kind of precision choice
elsewhere. Two categories — `frontend-testing` (12 lessons) and `python-testing` (16) —
are *about* test code and must keep running there. Most others are about application code
and arguably should not. Observed today: running the tool on this repository reports
`PY-018` three times, all against the probe strings inside `tests/test_lessons.py`.

**Decision needed from the maintainer.** One of:

1. Leave as is. Test code is code; a naive `datetime.utcnow()` in a test is still a trap.
2. Restrict to non-test paths except for the two testing categories. Cheapest: add
   `"frontend-testing"`, `"python-testing"`, `"gates-ratchets"` and
   `"secrets-test-isolation"` to a `_TEST_AWARE_CATEGORIES` set and filter the rest through
   `not is_test_path(relative)`.
3. Report them with `exposure="internal"`, which lowers the priority without hiding them,
   matching how `scan/` treats code that never serves a request.

**Recommendation:** (3). It keeps the finding visible, which is the house rule, and it is
four lines. But it is a precision policy, so it is the maintainer's call.

**Verification.** A probe with the same trap in `src/` and `tests/`; assert the priority
difference (or the absence) the chosen option implies.

---

## LES-04 — `lessons.md` carries no build stamp

**Status:** open · **Severity:** low

**Analysis.** `report.md`, `report.json` and SARIF all carry `provenance.tool_build()`
(`produced_by` / `driver.properties.repolensBuild`). `lessons.md` carries the detected
stacks and the counts but nothing that says which repolens, which catalogue, or when. A
checklist that outlives its run and cannot be dated is a hazard: the catalogue is edited,
and a stale `lessons.md` looks identical to a current one.

**Required fix.** Pass `describe_build(build)` and the catalogue's own `as_of` and
`content_hash` (both already in `catalogue.json`) into `lessons.to_markdown`, and render a
line under the title. `to_markdown` takes a `Survey`, so carry them on `Survey` from
`survey()` rather than threading a second argument.

**Why it matters.** Same reason every other output is stamped.

**Verification.** Assert the catalogue's `content_hash` appears in the rendered text.

---

## LES-05 — No documented adoption path for an existing repository

**Status:** open · **Severity:** low

**Analysis.** A mature repository turning the tool on for the first time gets a wave of P2
and P3 findings. The machinery to handle that already exists — `--update-baseline`, and
`[report] suppress` for reviewed-and-accepted fingerprints — but nothing tells the reader
to use it, and the per-lesson cap of 20 interacts with a baseline in a way that is not
written down: if a lesson is capped, the baseline records 20 occurrences, and a repository
that grows from 25 to 300 occurrences shows no change.

**Required fix.** A short section in `docs/lessons/README.md`: run it, read `lessons.md`,
fix or accept, then `--update-baseline`. State the cap interaction explicitly.

**Verification.** Documentation only.

---

## LES-06 — Stack detection has no negative evidence

**Status:** open · **Severity:** low

**Analysis.** `detect_stacks` only ever adds. A repository that has a single `.sql` file in
a `docs/` folder is credited with `postgresql` and gets all 25 PostgreSQL schema lessons in
`lessons.md`. There is no threshold and no way to say "this stack is incidental".

**Required fix.** Either a count threshold per stack (a stack established by exactly one
file is reported as "incidental" in `lessons.md` rather than driving the applicable set),
or a `[report] lessons_stacks` override so a repository can state its own list. Prefer the
override: it is explicit, and a threshold is another heuristic.

**Why it matters.** It only affects `lessons.md` breadth, not findings — a lesson with no
runnable recipe produces nothing either way. Low severity for that reason.

**Verification.** A probe with one stray `.sql` file; assert the PostgreSQL categories are
not in the applicable set (or are marked incidental).

---

## LES-07 — The catalogue has no schema check in the suite

**Status:** open · **Severity:** low

**Analysis.** `CatalogueShipsWithThePackage` asserts the required keys are present on every
lesson and that every `severity` is one `Finding` accepts. It does not check that
`category` values exist in `categories`, that `categories[].count` matches the real count,
that ids are unique, or that `detection[].kind` is in the documented set of nine. All four
hold in the committed catalogue today — they were checked by hand while writing this — but
the catalogue is hand-edited, so nothing keeps them holding.

**Required fix.** Extend that test class. All four assertions are one comprehension each.

**Verification.** The test itself.
