# GitHub & Dependency Automation: lessons learnt

**Scope.** gh CLI, token scopes, Dependabot, review bots.

| id | severity | lesson |
|---|---|---|
| GH-001 | low | `gh pr edit` fails on old gh versions (Projects classic sunset), and the failure is partial |
| GH-002 | low | A token without `workflow` scope cannot merge PRs that touch `.github/workflows` |
| GH-003 | medium | Dependabot groups by directory, so the root copy of a package goes stale |
| GH-004 | medium | Dependency bumps that pass CI but change behaviour |
| GH-005 | medium | AI review bots can fabricate findings |

## GH-001 — `gh pr edit` fails on old gh versions (Projects classic sunset), and the failure is partial

*Severity:* **low** · *Stacks:* github

**Symptom.** Reads worked, edits failed, and `| tail` swallowed the exit code, so the PR kept its old body.

**Root cause.** gh 2.45 queries the removed `projectCards` field.

**Resolution.** Use the REST API (`gh api -X PATCH repos/:o/:r/pulls/N`) and VERIFY that the body landed.

**Prevention.** Never pipe a mutating command through `tail`/`head` without `set -o pipefail`.

**How it is checked.**

- `shell`: `gh --version < 2.46 and scripts using gh pr edit`

**Evidence.** cfe9266a7

## GH-002 — A token without `workflow` scope cannot merge PRs that touch `.github/workflows`

*Severity:* **low** · *Stacks:* github

**Symptom.** An API merge was refused, while an SSH push worked.

**Root cause.** GitHub requires the `workflow` scope for workflow-file changes.

**Resolution.** Grant the scope, or merge through a push.

**Prevention.** Check `gh auth status` scopes.

**How it is checked.**

- `shell`: `gh auth status 2>&1 | grep -q workflow`

**Evidence.** memory: pattern_gh_token_cannot_merge_workflow_prs

## GH-003 — Dependabot groups by directory, so the root copy of a package goes stale

*Severity:* **medium** · *Stacks:* dependencies, github

**Symptom.** Frontend mermaid was bumped and the root copy was not. The CI parse gate used the root copy, so it passed syntax the browser couldn't draw.

**Root cause.** Each `package.json` directory is a separate Dependabot ecosystem entry.

**Resolution.** List every manifest directory in `dependabot.yml`, and pin shared tools once.

**Prevention.** A test asserts versions match across manifests for shared packages.

**How it is checked.**

- `test`: same package version across all package.json files

**Evidence.** 8e7449186; memory: pattern_dependabot_groups_by_directory

## GH-004 — Dependency bumps that pass CI but change behaviour

*Severity:* **medium** · *Stacks:* dependencies

**Symptom.** A minor bump changed runtime semantics (for example FastAPI's lazy `app.routes`, or Jest 30's flag rename).

**Root cause.** CI didn't cover the changed path.

**Resolution.** Read the changelog for every bump. Batch low-risk bumps, and isolate framework bumps with a targeted smoke test.

**Prevention.** Keep lockfiles committed, and use `npm ci`/`yarn --frozen-lockfile`.

**How it is checked.**

- `ci`: frozen-lockfile installs

**Evidence.** 94fe68df3

## GH-005 — AI review bots can fabricate findings

*Severity:* **medium** · *Stacks:* github, process

**Symptom.** 3 of 4 bot findings on a PR cited code that existed nowhere.

**Root cause.** The model hallucinated the code under review.

**Resolution.** Grep the cited symbol before acting on a bot finding.

**Prevention.** Treat bot output as leads, never as facts.

**How it is checked.**

- `review`: verify each cited file:line exists and says what is claimed

**Evidence.** memory: pattern_review_bot_cites_code_that_does_not_exist
