# CI Gates, Audits & Ratchets: lessons learnt

**Scope.** How checks die, oracles, baselines, what green actually means.

| id | severity | lesson |
|---|---|---|
| GT-001 | high | A check dies in exactly three ways: ABSENT, DEFANGED, or BLIND |
| GT-002 | medium | A text-search oracle counts prose as code |
| GT-003 | medium | A transitive import oracle credits everything |
| GT-004 | medium | A gate whose oracle is the working tree is non-deterministic |
| GT-005 | medium | An environment-dependent gate passes in one place and fails in another |
| GT-006 | medium | A detector encodes ONE spelling of the defect |
| GT-007 | medium | A gate that blesses a helper it cannot see being used |
| GT-008 | medium | Report-only audits exit 0 with findings |
| GT-009 | high | Ratchet, don't wall: a baseline that can only fall, and fails on an un-baselined improvement |
| GT-010 | medium | A clean summary printed on a schema error |
| GT-011 | medium | `--check` that validates format but not freshness |
| GT-012 | medium | A single CI job running sequential gates names only the FIRST failure |
| GT-013 | high | Green CI with no backend tests run |
| GT-014 | high | An unprotected main branch makes every gate advisory |
| GT-015 | medium | A CI job wrongly declared as 'needs no database' |
| GT-016 | medium | A filtered test run cannot support a 'zero failures' claim |
| GT-017 | high | Duplicate implementations are invisible to call graphs: keep a capability index |

## GT-001 — A check dies in exactly three ways: ABSENT, DEFANGED, or BLIND

*Severity:* **high** · *Stacks:* ci, tooling

**Symptom.** 7 of 18 validation scripts could not fail anything: nothing ran them, or they ran without the flag that fails, or their subject list was a hardcoded literal that new files never joined.

**Root cause.** A check is only as good as its invocation, its exit code and how it discovers what to check.

**Resolution.** Every check is wired into CI or preflight, with `--check`/`--strict` that exits non-zero, and DERIVES its subjects from the tree (glob or AST) rather than from a hand-maintained list.

**Prevention.** When you add a check, prove it can fail: seed one violation and watch it go red.

**How it is checked.**

- `ci`: for each scripts/validation/*.py: is it invoked in a workflow or preflight WITH its failing flag?
- `regex`: `(FILES|TARGETS|PATHS)\s*=\s*\(\s*['\"]  -- a literal subject list inside a gate`

**Evidence.** d58013530; 6de6b9824; ba0d9f562; aa696899a

## GT-002 — A text-search oracle counts prose as code

*Severity:* **medium** · *Stacks:* tooling

**Symptom.** A router was 'on the seam' because a DOCSTRING mentioned the dispatch function. `"_store" in src` matched the word 'stored'. One comment made a unit read as both missing AND orphaned.

**Root cause.** A substring search can't tell identifiers from comments and strings.

**Resolution.** Use AST-based oracles: imports, calls and names, with comments and strings excluded. `--list` prints each credit's chain so a wrong credit can be argued with.

**Prevention.** If a number doesn't move when you move the code, the gate is broken.

**How it is checked.**

- `review`: gates implemented as `in src` / grep over raw file text

**Evidence.** memory: pattern_a_text_search_oracle_counts_prose_as_code; GAP-TOOL-004

## GT-003 — A transitive import oracle credits everything

*Severity:* **medium** · *Stacks:* tooling

**Symptom.** An 'imports a PostgreSQL module (transitively)' rule credited all 66 routers, because the shared auth helper reaches PostgreSQL.

**Root cause.** Everything transitively reaches everything through shared utilities.

**Resolution.** Bound the walk: two hops at most, never through another router, a registered shared owner, or the control plane.

**Prevention.** Sanity-check any oracle by counting how many subjects it credits. 100% means it measures nothing.

**How it is checked.**

- `review`: transitive-closure oracles without a boundary

**Evidence.** 38268548b

## GT-004 — A gate whose oracle is the working tree is non-deterministic

*Severity:* **medium** · *Stacks:* ci, tooling

**Symptom.** It passed locally and failed CI on the same commit. A marker path 'dangled' until `git add`.

**Root cause.** `Path.exists()` sees untracked files locally, and generated files that CI doesn't have.

**Resolution.** The oracle is `git ls-files` (tracked content), never the filesystem.

**Prevention.** Run gates in a clean clone or worktree before trusting a local green.

**How it is checked.**

- `regex`: `Path\([^)]*\)\.exists\(\)  -- inside a validation/gate script`

**Evidence.** memory: pattern_a_gate_whose_oracle_is_the_filesystem; memory: pattern_featuretrace_path_oracle_is_git

## GT-005 — An environment-dependent gate passes in one place and fails in another

*Severity:* **medium** · *Stacks:* ci

**Symptom.** Gates behaved differently depending on whether a venv, a database, or a particular tool version was present.

**Root cause.** Their output depended on the environment, not on the tree.

**Resolution.** Make generators deterministic: a DB-free mode for the committed artefact, and an explicit skip-and-report when a dependency is missing.

**Prevention.** State what a gate needs in its header.

**How it is checked.**

- `review`: gate output that changes with installed deps or DB reachability

**Evidence.** 859424898; 5aa684bc6; e604f78fe

## GT-006 — A detector encodes ONE spelling of the defect

*Severity:* **medium** · *Stacks:* tooling, frontend

**Symptom.** A currency gate caught `$${x}` template literals and missed the JSX `<td>${fmt(x)}</td>` form that shipped.

**Root cause.** The defect has more than one syntactic form.

**Resolution.** Enumerate the syntaxes, and add a fixture for each one.

**Prevention.** Write the detector's test with the SHIPPED bad code as a fixture.

**How it is checked.**

- `test`: one fixture per syntactic form of the defect

**Evidence.** 8ecf8458b

## GT-007 — A gate that blesses a helper it cannot see being used

*Severity:* **medium** · *Stacks:* tooling

**Symptom.** A gate accepted 'sanctioned indirection' by reading its own docstring. The blessed helper had 0 callers, and the guard was hand-rolled elsewhere.

**Root cause.** The allow-path was never verified.

**Resolution.** Verify that sanctioned helpers are actually called (AST), and re-derive allowlists from data.

**Prevention.** Stale exemptions fail the gate too.

**How it is checked.**

- `test`: every allowlist entry still matches something

**Evidence.** 5405b80dc; memory: pattern_a_sanctioned_indirection_the_gate_cannot_see

## GT-008 — Report-only audits exit 0 with findings

*Severity:* **medium** · *Stacks:* ci, tooling

**Symptom.** `audit_*.py` scripts printed hundreds of findings and exited 0, so nobody read them.

**Root cause.** The scripts were built to report, and not everything was wired to fail.

**Resolution.** Run them ALL before committing, grep the output for YOUR files, and convert any audit that people rely on into a ratchet.

**Prevention.** 'Exit 0' does not mean 'clean'.

**How it is checked.**

- `shell`: `for f in scripts/validation/audit_*.py; do python3 $f | grep -F "$(git diff --name-only)"; done`

**Evidence.** 09607eddc; memory: feedback_run_every_validation_audit_script

## GT-009 — Ratchet, don't wall: a baseline that can only fall, and fails on an un-baselined improvement

*Severity:* **high** · *Stacks:* ci

**Symptom.** A day-one `--strict` gate failed every PR on debt nobody in that PR had created, and was about to be deleted.

**Root cause.** A hard gate on legacy debt blocks work that is unrelated to the debt.

**Resolution.** A baseline JSON of existing debt: new violations fail, and so does an improvement that isn't locked into the baseline. The correct merge of two baselines is the LOWER value.

**Prevention.** Parse the counts robustly: a ratchet that misparses reads as 0 and passes.

**How it is checked.**

- `ci`: gate compares current counts to a committed baseline; both directions fail

**Evidence.** f1585fd66; 8dbff8273; d9a65f0e2

## GT-010 — A clean summary printed on a schema error

*Severity:* **medium** · *Stacks:* tooling

**Symptom.** A validator hit a schema/parse error, skipped the item, and printed '0 findings'.

**Root cause.** The error path fell through to the success summary.

**Resolution.** Parse and schema errors are findings (non-zero exit), never skips.

**Prevention.** Test the gate with a malformed input.

**How it is checked.**

- `test`: malformed input ⇒ non-zero exit

**Evidence.** 4ff782878

## GT-011 — `--check` that validates format but not freshness

*Severity:* **medium** · *Stacks:* tooling

**Symptom.** A committed generated file was well-formed and stale.

**Root cause.** The check verified its shape, not that it equals a regeneration.

**Resolution.** `--check` regenerates in memory and diffs against the committed file (or compares content hashes).

**Prevention.** Every generated artefact gets a regenerate-and-compare gate.

**How it is checked.**

- `ci`: regenerate → git diff --exit-code

**Evidence.** b18b53d73

## GT-012 — A single CI job running sequential gates names only the FIRST failure

*Severity:* **medium** · *Stacks:* ci

**Symptom.** Fixing the reported gate revealed the next one, then another: three rounds of CI.

**Root cause.** Ten gates in one job, `set -e`.

**Resolution.** Run every gate and aggregate the results (`|| fail=1`), or use a matrix. Locally, run all ten before pushing.

**Prevention.** Print a summary table of pass/fail per gate.

**How it is checked.**

- `review`: multi-gate CI step using set -e without aggregation

**Evidence.** memory: pattern_repo_hygiene_stops_at_first_gate

## GT-013 — Green CI with no backend tests run

*Severity:* **high** · *Stacks:* ci, testing

**Symptom.** No PR job ran the main backend suite; only the security tests ran. The frontend Jest job had been dropped. Green meant nothing.

**Root cause.** Workflows drifted and nobody asserted WHICH suites ran.

**Resolution.** A job that runs the full suite with the right services, and a test that asserts the workflow files invoke each suite.

**Prevention.** Read the job logs for the test count, not the checkmark.

**How it is checked.**

- `ci`: assert test count > threshold in each suite job
- `review`: workflows vs the list of suites on disk

**Evidence.** 83b80c83a; 81e72a0a0; memory: pattern_green_ci_does_not_mean_backend_tests_ran

## GT-014 — An unprotected main branch makes every gate advisory

*Severity:* **high** · *Stacks:* github, ci

**Symptom.** Merges landed with red checks.

**Root cause.** Branch protection or required checks were not configured.

**Resolution.** Configure required status checks and block force-pushes.

**Prevention.** Audit the repository settings periodically (`gh api repos/:o/:r/branches/main/protection`).

**How it is checked.**

- `shell`: `gh api repos/{owner}/{repo}/branches/main/protection`

**Evidence.** GAP-TOOL-012

## GT-015 — A CI job wrongly declared as 'needs no database'

*Severity:* **medium** · *Stacks:* ci

**Symptom.** Tests skipped silently or failed confusingly in CI.

**Root cause.** The job had no service container, and the tests guarded with skips.

**Resolution.** Declare the services a job needs, and make a skipped test count visible (fail if skips exceed N).

**Prevention.** `-rs` in pytest output, and a skip budget.

**How it is checked.**

- `ci`: pytest -rs; fail on unexpected skip count

**Evidence.** f3459e2db; edc7f2af3

## GT-016 — A filtered test run cannot support a 'zero failures' claim

*Severity:* **medium** · *Stacks:* testing

**Symptom.** `pytest -k capital` skipped `TestCapitalShock` (`-k` is case-sensitive) and hid 6 failures. Passing `tests/backend` explicitly skipped 133 tests collected by `testpaths`.

**Root cause.** The filter semantics were not what the author assumed.

**Resolution.** Claim 'all green' only from the unfiltered command, and report the collected count.

**Prevention.** Put the collected-test count in commit messages or PRs.

**How it is checked.**

- `review`: claims of 'all tests pass' from a -k or path-filtered run

**Evidence.** memory: pattern_filtered_test_runs_hide_failures

## GT-017 — Duplicate implementations are invisible to call graphs: keep a capability index

*Severity:* **high** · *Stacks:* architecture, tooling

**Symptom.** Lot-to-unit resolution was rebuilt five times. Two `dollars_to_cents` functions returned DIFFERENT money for '10.005'.

**Root cause.** A re-implementation creates no edge to the original, so every map renders it as healthy new code.

**Resolution.** A YAML capability index (concept → owner module → consumers → detector), enforced in CI. Search by behaviour ('which functions read these stores') before writing a helper.

**Prevention.** A detector must separate correct use from incorrect use, or it gets allowlisted into uselessness (`detect: null` is acceptable).

**How it is checked.**

- `ci`: canonical-owner registry --check
- `shell`: `find_similar_functions.py --like '<purpose>' --touches <stores>`

**Evidence.** memory: pattern_capability_index_one_concept_one_owner
