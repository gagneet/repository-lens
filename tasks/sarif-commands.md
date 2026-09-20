# Open tasks: `[report] sarif_commands`

Follow-ups left behind by the SARIF-command seam (`_sarif_command_run` in
`repolens/report/runner.py`). Status and severity use the vocabulary of
[`docs/tasks.md`](../docs/tasks.md).

| ID | Task | Status | Severity |
|---|---|---|---|
| [SAR-01](#sar-01--one-global-timeout-for-tools-with-very-different-runtimes) | One global timeout for tools with very different runtimes | open | medium |
| [SAR-02](#sar-02--no-placeholder-for-the-repository-root) | No placeholder for the repository root | open | low |
| [SAR-03](#sar-03--the-shipped-example-invocations-are-unverified) | The shipped example invocations are unverified | open | medium |
| [SAR-04](#sar-04--a-sarif-run-that-reports-executionsuccessful-false-is-not-surfaced-differently) | A SARIF run reporting `executionSuccessful: false` is not surfaced differently | open | low |

---

## SAR-01 — One global timeout for tools with very different runtimes

**Status:** open · **Severity:** medium

**Analysis.** `_sarif_command_run` uses `ctx.section["command_timeout_seconds"]`, which
defaults to 300 and is shared with `[report] commands`. The tools this seam exists for do
not share a runtime: `gitleaks dir .` on a mid-size repository is seconds, `trivy fs .` on
first run downloads and builds its vulnerability database and routinely exceeds five
minutes. A timeout is recorded as `error`, which is correct behaviour — but a user whose
only problem is that trivy is slow has to raise the timeout for every configured command to
fix it.

**Required fix.** Accept an optional `timeout_seconds` on the spec, defaulting to the
section value:

```python
timeout = spec.get("timeout_seconds", ctx.section["command_timeout_seconds"])
```

Validate it is a positive int, and raise the same "the command line was rejected" style of
error when it is not. Apply the same key to `[report] commands` for consistency; the two
readers are three lines apart.

**Why it matters.** Without it the seam's headline use case fails on first run on any
repository with a lockfile, and the failure looks like a broken tool rather than a slow one.

**Verification.** A fake tool that sleeps past a 1-second `timeout_seconds` and is recorded
as an error, while the section default stays 300.

---

## SAR-02 — No placeholder for the repository root

**Status:** open · **Severity:** low

**Analysis.** `{output}` and `{python}` are substituted; there is no `{root}`. Commands run
with `cwd=ctx.root`, so `.` works for tools that accept a relative target. Tools that insist
on an absolute path, and any command that itself changes directory, cannot be expressed.

**Required fix.** Substitute `{root}` with `str(ctx.root)` in the same comprehension.
One line, plus a test. Document it in `repolens/templates/repolens.toml` beside `{output}`.

**Why it matters.** Small, but it is the kind of gap a user hits once and works around with
a wrapper script, which then needs its own maintenance.

**Verification.** A fake tool that asserts `sys.argv[2] == os.getcwd()`.

---

## SAR-03 — The shipped example invocations are unverified

**Status:** open · **Severity:** medium

**Analysis.** `repolens/templates/repolens.toml` ships two commented examples:

```toml
run = ["gitleaks", "dir", ".", "--report-format", "sarif", "--report-path", "{output}"]
run = ["trivy", "fs", "--quiet", "--format", "sarif", "--output", "{output}", "."]
```

Neither tool is installed on the development machine, so **neither command line has been
run**. They were written from the tools' documented interfaces. Both projects have moved
their CLI within the last two major versions — gitleaks replaced `detect --no-git --source`
with `dir`, and trivy renamed `--security-checks` to `--scanners` — so a wrong flag or a
renamed subcommand is a realistic failure. The seam itself is tested against a fake
analyser and is not in question; the two example lines are.

**Required fix.** Install both and run each example against a fixture repository with a
planted secret and a vulnerable manifest. Record the versions they were verified against in
a comment beside each example, the way `requirements-ci.txt` pins the scanners it was
checked with. If a command line is wrong, correct it; if a tool needs a subcommand this seam
cannot express, note that instead of shipping a line that will not work.

**Why it matters.** A shipped example that does not run is worse than no example: the user
assumes the seam is broken rather than the flag.

**Verification.** Manual, once, with the versions recorded. Not suitable for the suite —
it would make the test run depend on two external binaries.

---

## SAR-04 — A SARIF run reporting `executionSuccessful: false` is not surfaced differently

**Status:** open · **Severity:** low

**Analysis.** `import_sarif` already handles `invocations[].executionSuccessful: false`
(there is a test for it, `test_failed_producer_does_not_become_a_clean_result`). When that
document arrives through `sarif_commands` rather than `--sarif`, the resulting `ToolRun` is
renamed to the configured name and given the elapsed time, and whatever `import_sarif` set
is preserved — so the behaviour is believed correct. It has not been tested through this
path, and the rename code touches `run.tool` while the error field is set elsewhere.

**Required fix.** A test: a fake analyser that writes a SARIF document with
`executionSuccessful: false`, asserted to come back as an errored run under the configured
name. If it does not, fix the rename to preserve the failure.

**Why it matters.** It is the seam's core promise — a tool that did not look must not read
as clean — and it is the one path of that promise with no test.

**Verification.** The test itself.
