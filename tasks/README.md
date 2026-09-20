# tasks/

Pending work raised by a specific change, recorded at the point the change landed.

This sits alongside [`docs/tasks.md`](../docs/tasks.md), which is the long-lived register
from the stack-depth audit and the field evaluation. The split is by origin, not by
importance:

| where | what goes there |
|---|---|
| [`docs/tasks.md`](../docs/tasks.md) | the audit register: 49 open items, each with its probe, analysis and verification |
| `tasks/` | follow-ups a feature deliberately left behind, one file per feature |
| [`docs/roadmap.md`](../docs/roadmap.md) | capabilities wanted but not planned for this branch |

Every entry states what was observed, what a fix requires, why it matters, and how to
verify it — the same contract `docs/tasks.md` uses, so an item can move between them
without being rewritten. An item that needs a decision or research before any code can be
written says so in its status and names the question.

| file | feature |
|---|---|
| [`lessons-tool.md`](lessons-tool.md) | the lessons-learnt catalogue as a report tool |
| [`sarif-commands.md`](sarif-commands.md) | `[report] sarif_commands` |
