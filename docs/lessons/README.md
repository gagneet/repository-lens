# Technical lessons learnt

A catalogue of **260 stack-level lessons** for applications built with Next.js / TypeScript /
JavaScript on the front end and FastAPI / Python with PostgreSQL and MongoDB behind it. It also
covers the CI, git and deploy tooling around such a stack. Each lesson records:

- **symptom**: what you see when you hit the trap
- **root cause**: why it happens
- **resolution**: how it was fixed
- **prevention**: the day-one decision that avoids it
- **detection**: one or more recipes for finding it, each tagged with a kind: `regex`, `ast`, `sql`, `shell`, `ci`, `test`, `lint`, `config` or `review`

Severities use the repolens vocabulary (`critical`, `high`, `medium`, `low`, `info`) and rank
the blast radius when the trap is hit, not how often it happens.

Most lessons come from incidents in a production multi-tenant codebase; the `evidence` field
cites that repository's commits and documents. The rest are well-known traps from vendor
documentation and OWASP, with links in the `sources` field. The lessons describe the stack
and are not specific to any one application.

| file | what it is |
|---|---|
| [`technical-lessons-learnt.json`](technical-lessons-learnt.json) | **The canonical catalogue.** Edit this file. |
| [`technical-lessons-learnt.html`](technical-lessons-learnt.html) | A browsable page with category navigation, severity and stack filters, and search. It reads the JSON, so serve the folder: `python3 -m http.server -d docs/lessons`, then open `/technical-lessons-learnt.html`. |
| [`rules/`](rules/README.md) | One Markdown rule pack per category, generated from the JSON |
| [`export_lessons_learnt.py`](export_lessons_learnt.py) | The generator for `rules/` (standard library only) |

```bash
python3 docs/lessons/export_lessons_learnt.py --out docs/lessons/rules                 # regenerate the committed packs
python3 docs/lessons/export_lessons_learnt.py --out /tmp/pg --category postgres-rls --min-severity high
```

## Using a lesson as a check

The detection recipes are **starting points**, not finished rules. Every regex matches some
correct code, so run a recipe as a search first and read what it finds before you gate on it.
`sql` recipes read a live catalog, which repolens itself never connects to; run them in your
own preflight. A recipe that cannot separate correct use from incorrect use does not become a
gate (see [`one-concept-one-owner`](../../repolens/rules/one-concept-one-owner.md) and
[`gates-and-ratchets`](../../repolens/rules/gates-and-ratchets.md)).
