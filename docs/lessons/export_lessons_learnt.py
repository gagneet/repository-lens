#!/usr/bin/env python3
"""Export the Technical Lessons Learnt catalogue as repolens-style rule packs.

The canonical catalogue is ``repolens/lessons/catalogue.json``, which ships as package
data so `repolens report` can read it inside a target repository; the script only READS it. It writes one Markdown file per category (plus an index) into
``--out``, and refreshes ``technical-lessons-learnt.js`` next to the catalogue: the same data
as a script, so ``index.html`` works when opened straight from disk (``file://`` blocks fetch). The committed copy under ``docs/lessons/rules/`` is its output: edit the JSON,
then re-run the first command below. Standard library only.

    python3 docs/lessons/export_lessons_learnt.py --out docs/lessons/rules
    python3 docs/lessons/export_lessons_learnt.py --out /tmp/rules --category postgres-rls --min-severity high
"""
import argparse
import json
from pathlib import Path

CATALOGUE = Path(__file__).resolve().parents[2] / "repolens" / "lessons" / "catalogue.json"
PAGE_DATA = Path(__file__).resolve().with_name("technical-lessons-learnt.js")
SEVERITIES = ["critical", "high", "medium", "low", "info"]


def render_category(cat: dict, lessons: list[dict]) -> str:
    out = [f"# {cat['title']}: lessons learnt", "", f"**Scope.** {cat['summary']}", "",
           "| id | severity | lesson |", "|---|---|---|"]
    out += [f"| {l['id']} | {l['severity']} | {l['title']} |" for l in lessons]
    for l in lessons:
        out += ["", f"## {l['id']} — {l['title']}", "",
                f"*Severity:* **{l['severity']}** · *Stacks:* {', '.join(l['stacks'])}", "",
                f"**Symptom.** {l['symptom']}", "",
                f"**Root cause.** {l['root_cause']}", "",
                f"**Resolution.** {l['resolution']}", "",
                f"**Prevention.** {l['prevention']}"]
        if l["detection"]:
            out += ["", "**How it is checked.**", ""]
            out += [f"- `{d['kind']}`: `{d['recipe']}`" if d["kind"] in ("regex", "sql", "shell")
                    else f"- `{d['kind']}`: {d['recipe']}" for d in l["detection"]]
        if l["evidence"]:
            out += ["", "**Evidence.** " + "; ".join(l["evidence"])]
        if l["sources"]:
            out += ["", "**Sources.** " + " ".join(f"<{s}>" if s.startswith("http") else s for s in l["sources"])]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path, help="directory to write the rule packs into")
    ap.add_argument("--category", action="append", help="export only this category id (repeatable)")
    ap.add_argument("--min-severity", choices=SEVERITIES, default="info")
    args = ap.parse_args()

    data = json.loads(CATALOGUE.read_text())
    # `</` escaped so no string in the catalogue can close the <script> element.
    PAGE_DATA.write_text("window.LESSONS = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";\n")
    floor = SEVERITIES.index(args.min_severity)
    known = {c["id"] for c in data["categories"]}
    unknown = set(args.category or []) - known
    if unknown:
        ap.error(f"unknown category: {', '.join(sorted(unknown))} (known: {', '.join(sorted(known))})")

    args.out.mkdir(parents=True, exist_ok=True)
    index = ["# Lessons learnt — rule packs", "",
             f"Exported from the catalogue (as of {data['as_of']}, content {data['content_hash']}).", "",
             "| file | lessons |", "|---|---|"]
    written = 0
    for cat in data["categories"]:
        if args.category and cat["id"] not in args.category:
            continue
        lessons = [l for l in data["lessons"]
                   if l["category"] == cat["id"] and SEVERITIES.index(l["severity"]) <= floor]
        if not lessons:
            continue
        (args.out / f"{cat['id']}.md").write_text(render_category(cat, lessons))
        index.append(f"| [{cat['id']}.md]({cat['id']}.md) | {len(lessons)} |")
        written += len(lessons)
    (args.out / "README.md").write_text("\n".join(index) + "\n")
    print(f"wrote {written} lessons to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
