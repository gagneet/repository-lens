# Generated Artefacts & Git: lessons learnt

**Scope.** Merge drivers, determinism, scripted edits, worktrees.

| id | severity | lesson |
|---|---|---|
| GA-001 | high | A three-way text merge of a generated file is always wrong |
| GA-002 | high | A rebase resolves generated artefacts with no conflict, and the result is wrong |
| GA-003 | low | Git config and hooks are not versioned |
| GA-004 | medium | A wall clock (or hostname, username, absolute path) stamped into a committed artefact |
| GA-005 | low | A generic filename routed to the wrong generator; skip versus fail |
| GA-006 | medium | Regenerating in a shared working tree while other work is in progress |
| GA-007 | high | Scripted edits (`sed -i`, `cat >`) corrupting or overwriting files |
| GA-008 | medium | A path-rewrite checked with the same malformed list reports '0 leftover' |
| GA-009 | low | An annotated tag's SHA versus its commit's SHA; two-dot versus three-dot diff |

## GA-001 — A three-way text merge of a generated file is always wrong

*Severity:* **high** · *Stacks:* git

**Symptom.** A 'successful' merge produced an artefact describing neither branch, and it still validated.

**Root cause.** The correct result is whatever the generator produces from the MERGED tree.

**Resolution.** A `.gitattributes` merge driver keeps one side without a conflict, and a post-merge/post-rewrite hook regenerates from the completed tree. CI `--check` is the backstop.

**Prevention.** The driver must not regenerate: it runs mid-merge on a half-applied tree, and a version that did regenerate was measured losing one branch's change.

**How it is checked.**

- `config`: .gitattributes entries for every generated path; a test derives the set from the generators

**Evidence.** c374665c8; fd795f5db

## GA-002 — A rebase resolves generated artefacts with no conflict, and the result is wrong

*Severity:* **high** · *Stacks:* git

**Symptom.** After a rebase the artefact matched neither tree, and hygiene failed later.

**Root cause.** The driver picks a side for each replayed commit, and nothing regenerates at the end.

**Resolution.** Regenerate after every rebase (a post-rewrite hook) and before pushing.

**Prevention.** Server-side merges (the GitHub button) run no client hooks, so regenerate after merging main.

**How it is checked.**

- `ci`: regenerate-and-diff on every PR

**Evidence.** memory: pattern_a_rebase_resolves_generated_files_silently

## GA-003 — Git config and hooks are not versioned

*Severity:* **low** · *Stacks:* git

**Symptom.** `.gitattributes` named a driver that a fresh clone did not have.

**Root cause.** `git config` and `.git/hooks` are local to each clone.

**Resolution.** An install script, run once per clone. Without it the fallback is a normal conflict, which degrades safely.

**Prevention.** Document the installer in the README.

**How it is checked.**

- `review`: .gitattributes merge= drivers without an installer

## GA-004 — A wall clock (or hostname, username, absolute path) stamped into a committed artefact

*Severity:* **medium** · *Stacks:* generated-artefacts

**Symptom.** Every regeneration dirtied the tree and every branch conflicted. Reviewers learned to skim the diff, and three API descriptions sat months stale behind a date line that changed daily.

**Root cause.** Non-deterministic metadata in the output.

**Resolution.** Stamp a content hash, not a time. Omit fields such as `_postman_exported_at` rather than faking them.

**Prevention.** Keep optional metadata out of the hash input.

**How it is checked.**

- `regex`: `datetime\.now\(|utcnow\(|time\.time\(|socket\.gethostname\(|getpass\.getuser\(  -- in a generator writing a committed file`

**Evidence.** be4bd74dd; 5babfe2ce

## GA-005 — A generic filename routed to the wrong generator; skip versus fail

*Severity:* **low** · *Stacks:* generated-artefacts

**Symptom.** '1 generator failed' on a healthy tree, because an artefact was mis-routed. A phantom failure looks identical to a real one.

**Root cause.** Routing was done by filename pattern.

**Resolution.** Route by exact path. A generator that CANNOT run (no venv) is skipped and reported, never failed.

**Prevention.** Run generators in dependency order.

**How it is checked.**

- `test`: every generated path routes to exactly one generator

**Evidence.** 34953e135

## GA-006 — Regenerating in a shared working tree while other work is in progress

*Severity:* **medium** · *Stacks:* git, process

**Symptom.** A regeneration picked up another session's uncommitted edits, or clobbered them.

**Root cause.** Concurrent sessions or agents shared one checkout.

**Resolution.** Regenerate in a dedicated worktree (`git worktree add`), and use one worktree per concurrent session.

**Prevention.** Check `git status` for files you didn't touch before committing.

**How it is checked.**

- `review`: commits containing unrelated generated diffs

## GA-007 — Scripted edits (`sed -i`, `cat >`) corrupting or overwriting files

*Severity:* **high** · *Stacks:* shell, process

**Symptom.** A sed insert prefixed ALL 205 lines and still committed clean. `cat >` overwrote a 534-line existing module; git showed ` M`, not `??`.

**Root cause.** Bulk text edits without verification, and a write to a path assumed to be new.

**Resolution.** Run `py_compile`/`tsc` after every scripted edit, check that a path doesn't exist before `>`, and read `git diff --stat` before committing.

**Prevention.** Prefer structured edits over sed.

**How it is checked.**

- `shell`: `python3 -m py_compile $(git diff --name-only -- '*.py')`
- `review`: git status ' M' on a file you believed you created

**Evidence.** c402f0e9a; 7a3665a0d; 58bf6d07c

## GA-008 — A path-rewrite checked with the same malformed list reports '0 leftover'

*Severity:* **medium** · *Stacks:* shell

**Symptom.** 97 references were stale and 35 were corrupted, and verification reported success.

**Root cause.** The check reused the defect in the input list.

**Resolution.** Verify with an independent method, for example resolving every link target.

**Prevention.** Never let a verification reuse the artefact it verifies.

**How it is checked.**

- `review`: verification scripts sharing inputs with the change

**Evidence.** memory: pattern_a_path_rewrite_check_that_shares_its_own_defect

## GA-009 — An annotated tag's SHA versus its commit's SHA; two-dot versus three-dot diff

*Severity:* **low** · *Stacks:* git

**Symptom.** A comparison against a tag 'did not match' any commit. A diff showed main's changes as though they were the branch's.

**Root cause.** `git rev-parse tag` returns the tag object; `A..B` and `A...B` mean different things in `diff`.

**Resolution.** Use `git rev-parse tag^{commit}`, and `git diff main...HEAD` for 'what this branch changed'.

**Prevention.** Be deliberate about the diff range.

**How it is checked.**

- `regex`: `git rev-parse \S+(?<!\^\{commit\})\s`
