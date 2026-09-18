# Docs, Knowledge & Agent Practice: lessons learnt

**Scope.** Readings vs facts, doc gates, working with coding agents.

| id | severity | lesson |
|---|---|---|
| DK-001 | medium | Docs are the one artefact no gate reads |
| DK-002 | medium | A number in a document goes stale; give its as-of date and the command that re-derives it |
| DK-003 | medium | A resolved blocker left standing, or a reopened one recorded as closed |
| DK-004 | medium | Working with coding agents: delegate search, verify load-bearing results |

## DK-001 — Docs are the one artefact no gate reads

*Severity:* **medium** · *Stacks:* documentation

**Symptom.** A phantom schema, a live tenant described as deleted, five diagrams that never rendered, and a canonical file that contradicted itself.

**Root cause.** Nothing executes documentation.

**Resolution.** Validators for doc code blocks and diagrams (parse mermaid in CI with the same version the browser uses), and link and path checking.

**Prevention.** A citation that EXISTS is not a citation that is CORRECT: check that it says what you claim.

**How it is checked.**

- `ci`: mermaid parse, link check, cited file:line existence
- `review`: cited lines actually support the claim

**Evidence.** fe2a64474; bff8d4f8b; 324046032; memory: pattern_citation_exists_is_not_citation_is_correct

## DK-002 — A number in a document goes stale; give its as-of date and the command that re-derives it

*Severity:* **medium** · *Stacks:* documentation

**Symptom.** Counts were quoted for weeks after they changed, and partitions didn't sum to their population.

**Root cause.** Readings were written as facts.

**Resolution.** Every figure carries a date and its re-run command, and partitions are checked to sum to the stated total.

**Prevention.** Line numbers in long-lived docs are readings too; cite a grep instead.

**How it is checked.**

- `review`: numbers without dates; partitions whose parts don't sum

**Evidence.** bf6393dc9

## DK-003 — A resolved blocker left standing, or a reopened one recorded as closed

*Severity:* **medium** · *Stacks:* documentation, process

**Symptom.** Work continued against a gap that had already been closed, while a regression went unchecked because the doc said 'done'.

**Root cause.** No re-verification step.

**Resolution.** Stale TO-DO lists cost the same as stale facts. Re-verify status claims before acting on them.

**Prevention.** Periodic 'stale checks' passes.

**How it is checked.**

- `review`: status sentences older than N days in canonical docs

## DK-004 — Working with coding agents: delegate search, verify load-bearing results

*Severity:* **medium** · *Stacks:* process, ai-agents

**Symptom.** Context bloated with file dumps, and confident wrong answers from subagents were taken at face value.

**Root cause.** Reading is expensive and thinking is cheap; an agent's summary is only as good as its reading.

**Resolution.** Delegate wide searches to subagents that return conclusions, and verify every number before it lands in a commit or a doc.

**Prevention.** Deferred decisions noted by an agent are decisions unmade; re-evaluate them.

**How it is checked.**

- `review`: agent-provided numbers cited without re-verification

**Evidence.** memory: reference_claude_code_usage_profile; memory: pattern_a_deferral_repeated_is_a_decision_unmade
