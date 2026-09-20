"""Match the technical lessons-learnt catalogue against the repository under report.

The catalogue (`repolens/lessons/catalogue.json`) records, for each trap, what it looks
like, why it happens and how to avoid it, plus one or more *detection recipes*. Those
recipes are written for a human running a search, not for a gate: the catalogue's own
README says every regex matches some correct code. Two rules follow from that, and they
are the whole design of this module.

**Only mechanically safe recipes run.** A recipe qualifies when, after a trailing
human qualifier is stripped (`  (flag when not preceded by await)`, `  -- verify against
pg_type`), what is left is a regular expression and not a sentence. A recipe whose
condition is prose in the middle ("X followed by a line matching Y") is never run,
because the part that is machine-checkable is only half the rule and would fire on
every X. `runnable_pattern` is that decision, and it is the thing to test.

**A match is evidence, not a verdict.** Every finding is `confidence="low"`, which caps
it at P2 under the shared severity x confidence x exposure score, so no lesson can fail
`--check` at the default `fail_on = "P1"`. A lesson is a prompt to go and look, and the
finding carries the lesson's own prevention text so the reader knows what they are
looking for.

Lessons are also scoped to the stacks the repository actually uses: a Next.js trap is
not reported against a repository with no JavaScript in it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..core.files import iter_files, read_text_or_none
from ..core.findings import Finding
from ..core.git import under_ignored, untracked_ignored
from ..lessons import load
from ..scan.settings import DEFAULTS as _SCAN_DEFAULTS

#: What a caller that passes no `skip_parts` gets. Shared with the built-in checks, so a
#: bare `scan(root)` does not report the catalogue against a repository's installed
#: dependencies — which buried every real result behind site-packages.
DEFAULT_SKIP_PARTS: tuple[str, ...] = tuple(_SCAN_DEFAULTS["skip_parts"])

#: At most this many matches per lesson. One broad recipe (`:\s*any\b`, `\.sort\(\)`)
#: legitimately matches hundreds of lines; without a cap that one lesson is the report.
MAX_PER_LESSON = 20
#: Lines longer than this are skipped rather than matched. Minified bundles and embedded
#: data URIs are one enormous line, and a recipe with `[^)]*` in it is quadratic on those.
MAX_LINE_LENGTH = 2_000
#: How much of a dependency manifest is read for stack detection. A generated lockfile can
#: be tens of megabytes; the declarations that establish a stack are at the top, and the
#: bound is applied to the READ, not to a slice of an already-loaded string.
MAX_MANIFEST_BYTES = 400_000

#: Anything after a run of two or more spaces is a note to the person running the search
#: ("  -- verify against pg_type", "  (in tests/ or modules tests import)"). Its presence
#: means the regex is only part of the rule.
_WIDE_QUALIFIER = re.compile(r"\s{2,}.*\Z", re.DOTALL)
#: The same thing written with one space: a trailing parenthetical of several plain words,
#: "(outside the classifier)". The content must contain a space, so a real alternation
#: `(get|post)` or an optional group `(\([^)]*\))?` is left alone; and the paren must be
#: preceded by a literal space, so an escaped `\(` — whose preceding character is the
#: backslash, not a space — cannot match either.
_PAREN_QUALIFIER = re.compile(r" \([a-z][^()]*\s[^()]*\)\s*\Z")
#: A plain lowercase word. Two in a row is a sentence fragment, not a pattern.
_PLAIN_WORD = re.compile(r"[a-z]{2,}\Z")
_MAX_PLAIN_WORD_RUN = 1


def runnable_pattern(recipe: str) -> re.Pattern[str] | None:
    r"""The compiled pattern for `recipe`, or None when it is a recipe for a person.

    A recipe is refused when the regex is only half the rule, because running that half
    reports every occurrence of the harmless general case. Two shapes say so:

    * a **qualifier** — a trailing note naming a condition or a place the recipe applies
      ("  -- in *.example", "(outside the classifier)"). Honouring "in a generator writing
      a committed file" needs judgement this module does not have, and ignoring it turned
      a secrets recipe scoped to `.env.example` into a match on every long assignment.
    * **prose in the body** — two consecutive plain lowercase words, as in
      "X assigned to a year-like identifier". This also refuses a handful of genuine
      patterns whose literals read like words (`:\s*any\b|as any|as unknown as`); losing
      a broad recipe costs less than reporting a sentence fragment as a match.
    """
    if _WIDE_QUALIFIER.search(recipe) or _PAREN_QUALIFIER.search(recipe):
        return None
    body = recipe.strip()
    if not body:
        return None
    run = 0
    for token in body.split():
        run = run + 1 if _PLAIN_WORD.match(token) else 0
        if run > _MAX_PLAIN_WORD_RUN:
            return None
    try:
        return re.compile(body)
    except re.error:
        return None


# ── which stacks this repository uses ────────────────────────────────────────────
#: Suffix -> the catalogue stacks its presence establishes.
_SUFFIX_STACKS: dict[str, tuple[str, ...]] = {
    ".py": ("python",), ".pyi": ("python",),
    ".ts": ("typescript", "javascript"), ".tsx": ("typescript", "javascript", "react", "jsx"),
    ".js": ("javascript",), ".jsx": ("javascript", "react", "jsx"),
    ".mjs": ("javascript",), ".cjs": ("javascript",),
    ".sql": ("postgresql",), ".prisma": ("postgresql",),
    ".sh": ("shell",),
}
#: A name in a dependency manifest -> the stacks it establishes. Matched as a word, so
#: `motor` does not fire on `motorcycle` and `react` does not fire on `react-pdf` alone.
_DEPENDENCY_STACKS: dict[str, tuple[str, ...]] = {
    "next": ("nextjs", "app-router", "ssr", "javascript"),
    "react": ("react", "jsx", "javascript"),
    "next-auth": ("nextauth", "auth"), "axios": ("axios", "api-contract"),
    "jest": ("jest", "testing"), "@playwright/test": ("playwright", "testing"),
    "@testing-library/react": ("rtl", "testing", "jsdom"),
    "tailwindcss": ("tailwind",), "recharts": ("recharts",), "@tremor/react": ("tremor",),
    "typescript": ("typescript", "javascript"), "eslint": ("eslint",), "prisma": ("postgresql", "schema"),
    "fastapi": ("fastapi", "starlette", "api-design"), "pydantic": ("pydantic", "validation"),
    "sqlalchemy": ("sqlalchemy", "migrations"), "asyncpg": ("asyncpg", "pooling"),
    "alembic": ("alembic", "migrations"), "pymongo": ("pymongo", "mongodb"),
    "motor": ("motor", "mongodb", "asyncio"), "pytest": ("pytest", "testing"),
    "uvicorn": ("uvicorn", "deploy"), "psycopg2": ("postgresql",), "psycopg": ("postgresql",),
}
#: A path that exists -> the stacks it establishes.
_PATH_STACKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("alembic", ("alembic", "migrations")),
    (".github/workflows", ("ci", "github", "git")),
    (".github/dependabot.yml", ("dependabot", "dependencies")),
    ("docker-compose.yml", ("deploy", "operations")),
    ("Dockerfile", ("deploy", "operations")),
)
_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
              "setup.cfg", "Pipfile", "poetry.lock")

#: Suffixes a lesson of each category group can meaningfully match.
_GROUP_SUFFIXES: dict[str, tuple[str, ...]] = {
    "frontend": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "python": (".py",),
    "database": (".py", ".sql", ".ts", ".js"),
    "cross-cutting": (".py", ".ts", ".tsx", ".js", ".jsx"),
    "tooling": (".py", ".ts", ".js", ".sh", ".yml", ".yaml", ".toml", ".json"),
}
#: Categories whose traps live in a narrower set of files than their group's.
_CATEGORY_SUFFIXES: dict[str, tuple[str, ...]] = {
    "postgres-rls": (".sql", ".py"), "postgres-schema": (".sql", ".py", ".prisma"),
    "alembic": (".py",), "sqlalchemy-asyncpg": (".py",),
    "generated-artefacts-git": (".py", ".js", ".ts", ".sh", ".yml", ".yaml"),
    "github-deps": (".yml", ".yaml", ".json", ".sh"),
    "deploy-ops": (".sh", ".yml", ".yaml", ".toml", ".py"),
    # Accessibility traps live in markup and stylesheets, which no group walks: the
    # frontend group is script suffixes only, so an accessibility lesson scoped to its
    # group could never match the `role=` attribute or the colour token it is about.
    "accessibility": (".html", ".htm", ".css", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"),
}


def _read_head(path: Path, limit: int) -> str | None:
    """The first `limit` bytes of `path` as text, or None when it cannot be read.

    A generated lockfile runs to tens of megabytes and only its declarations matter here,
    so the bound is on the read. Undecodable bytes are dropped rather than raising: a
    manifest with one bad byte should still establish its stacks.
    """
    try:
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", errors="ignore")
    except OSError:
        return None


def _ignored(root: Path) -> list[str]:
    """The untracked paths git ignores, or an empty list when git cannot say.

    Failing open is deliberate: reading a stray backup file is noise, whereas silently
    skipping real source would be a hole in the survey.
    """
    try:
        return untracked_ignored(root)[0]
    except (OSError, ValueError):
        return []


def _tracked(root: Path, paths: list[Path], ignored: list[str] | None = None) -> list[Path]:
    """`paths` without the untracked files git ignores, matching `python_ast.python_files`.

    A build directory or a vendored copy is not the repository's code. `ignored` is passed
    in by `survey`, which lists it once: the listing is a git subprocess, and it was being
    run again for every group of suffixes the catalogue walks.
    """
    listed = _ignored(root) if ignored is None else ignored
    if not listed:
        return paths
    return [p for p in paths if not under_ignored(p.relative_to(root).as_posix(), listed)]


def detect_stacks(root: Path, skip_parts: Iterable[str],
                  ignored: list[str] | None = None) -> set[str]:
    """The catalogue stacks this repository shows evidence of.

    Text only: manifests are read, never resolved or installed, so a dependency that is
    declared but not installed still counts and nothing is executed.
    """
    stacks: set[str] = set()
    suffixes = set(_SUFFIX_STACKS) | {".yml", ".yaml", ".toml", ".json"}
    for path in _tracked(root, iter_files(root, ["."], suffixes, skip_parts), ignored):
        stacks.update(_SUFFIX_STACKS.get(path.suffix, ()))
        if path.name in _MANIFESTS:
            text = _read_head(path, MAX_MANIFEST_BYTES)
            if text is None:
                continue
            for name, established in _DEPENDENCY_STACKS.items():
                if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text):
                    stacks.update(established)
    for relative, established in _PATH_STACKS:
        if (root / relative).exists():
            stacks.update(established)
    return stacks


def applies(lesson: dict[str, Any], stacks: set[str], known: set[str]) -> bool:
    """Whether `lesson` is about a stack this repository uses.

    A lesson tagged only with stacks nothing can detect (`owasp`, `architecture`) is
    general advice and always applies; one tagged with a detectable stack applies only
    when that stack was detected, so Next.js traps stay out of a Python-only report.
    """
    detectable = [s for s in lesson["stacks"] if s in known]
    return not detectable or any(s in stacks for s in detectable)


def _suffixes_for(lesson: dict[str, Any], groups: dict[str, str]) -> tuple[str, ...]:
    category = lesson["category"]
    if category in _CATEGORY_SUFFIXES:
        return _CATEGORY_SUFFIXES[category]
    return _GROUP_SUFFIXES.get(groups.get(category, ""), _GROUP_SUFFIXES["cross-cutting"])


@dataclass
class Survey:
    """One pass of the catalogue over one repository.

    `findings` is what the report ranks. `applicable` is the larger set the findings are
    drawn from — every lesson for the stacks this repository uses, including the ~170 whose
    detection is an `ast`, `sql`, `review` or human-scoped recipe that this module will not
    run. Those never become findings, and leaving them invisible would hide most of the
    catalogue's value, so `to_markdown` writes them out as a checklist beside the report.
    """
    stacks: set[str]
    applicable: list[dict[str, Any]]
    #: Lesson ids that had at least one recipe this module was willing to run. A lesson
    #: outside this set was CHECKED BY NOBODY, which is not the same as passing.
    checked: set[str]
    findings: list[Finding]
    categories: dict[str, dict[str, Any]]


def scan(root: Path, skip_parts: Iterable[str] | None = None,
         catalogue: dict[str, Any] | None = None) -> list[Finding]:
    """Findings for every runnable recipe of every lesson that applies to this repository."""
    return survey(root, skip_parts, catalogue).findings


def survey(root: Path, skip_parts: Iterable[str] | None = None,
           catalogue: dict[str, Any] | None = None) -> Survey:
    """Match the catalogue against `root` and report both what matched and what applies."""
    data = catalogue if catalogue is not None else load()
    skip_parts = DEFAULT_SKIP_PARTS if skip_parts is None else tuple(skip_parts)
    groups = {c["id"]: c.get("group", "") for c in data["categories"]}
    known_stacks = {s for values in _SUFFIX_STACKS.values() for s in values}
    known_stacks |= {s for values in _DEPENDENCY_STACKS.values() for s in values}
    known_stacks |= {s for _, values in _PATH_STACKS for s in values}

    ignored = _ignored(root)   # one git listing for the whole survey, not one per walk
    stacks = detect_stacks(root, skip_parts, ignored)
    # Reading a file once per lesson would re-read the tree dozens of times; the lessons
    # are grouped by the suffixes they can match and each file set is walked once.
    by_suffixes: dict[tuple[str, ...], list[tuple[dict[str, Any], re.Pattern[str], str]]] = {}
    applicable: list[dict[str, Any]] = []
    checked: set[str] = set()
    for lesson in data["lessons"]:
        if not applies(lesson, stacks, known_stacks):
            continue
        applicable.append(lesson)
        for recipe in lesson["detection"]:
            if recipe["kind"] != "regex":
                continue
            pattern = runnable_pattern(recipe["recipe"])
            if pattern is not None:
                checked.add(lesson["id"])
                by_suffixes.setdefault(_suffixes_for(lesson, groups), []).append(
                    (lesson, pattern, recipe["recipe"]))

    out: list[Finding] = []
    counts: dict[str, int] = {}
    for suffixes, rules in by_suffixes.items():
        for path in _tracked(root, iter_files(root, ["."], suffixes, skip_parts), ignored):
            text = read_text_or_none(path)
            if text is None:
                continue
            relative = path.relative_to(root).as_posix()
            lines = text.splitlines()
            for lesson, pattern, recipe in rules:
                if counts.get(lesson["id"], 0) >= MAX_PER_LESSON:
                    continue
                for number, line in enumerate(lines, 1):
                    if len(line) > MAX_LINE_LENGTH:
                        continue
                    if not pattern.search(line):
                        continue
                    counts[lesson["id"]] = counts.get(lesson["id"], 0) + 1
                    out.append(Finding(
                        tool="lessons", rule=f"lessons/{lesson['id']}",
                        severity=lesson["severity"], confidence="low",
                        category="lessons", file=relative, line=number,
                        message=f"{lesson['title']} [{lesson['id']}, {lesson['category']}]",
                        remedy=lesson["prevention"],
                        evidence=f"matched the catalogue recipe `{recipe}`"))
                    if counts[lesson["id"]] >= MAX_PER_LESSON:
                        break
    for lesson_id, count in sorted(counts.items()):
        if count >= MAX_PER_LESSON:
            # Deliberately NOT counts_matter: `_MAGNITUDE` would read the digits of the
            # lesson id ("TS-006" -> 6) as the number that matters and ratchet on it. The
            # digits are normalised out of the fingerprint, so this keeps its identity
            # whether the cap is reached by 20 matches or 200.
            out.append(Finding(
                tool="lessons", rule=f"lessons/{lesson_id}-capped", severity="info",
                confidence="low", category="lessons",
                message=f"{lesson_id} matched more than the per-lesson cap of {MAX_PER_LESSON}; "
                        "the remaining matches are not listed",
                remedy="Search the repository for this lesson's recipe to see every occurrence."))
    return Survey(stacks=stacks, applicable=applicable, checked=checked, findings=out,
                  categories={c["id"]: c for c in data["categories"]})



#: Order used when listing lessons, worst first. `SEVERITIES` in core.findings is the same
#: order; it is repeated here rather than imported so the ranking stays a local decision.
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def to_markdown(result: Survey, title: str = "Lessons that apply to this repository") -> str:
    """The applicable catalogue as a checklist, for `lessons.md` beside the report.

    This is deliberately NOT a list of findings. It answers a different question — "what is
    known to go wrong with the stack I am running, and which of it did anything actually
    check?" — and the distinction is carried on every row: a lesson with no runnable recipe
    was not checked by this tool, and its absence from the report means nothing at all.
    """
    stacks = ", ".join(sorted(result.stacks)) or "none detected"
    checked, total = len(result.checked), len(result.applicable)
    lines = [
        f"# {title}",
        "",
        f"**Stacks detected:** {stacks}",
        "",
        f"{total} of the catalogue's lessons apply to this repository. "
        f"{checked} of them have a detection recipe that `repolens report` runs; the other "
        f"{total - checked} are recorded here only, because their detection needs a person "
        "(a code review, a live database, a CI log) or a judgement this tool does not make.",
        "",
        "> A lesson listed here is **not** a finding. An unchecked lesson being absent from "
        "the report means nobody looked, not that the repository is clear of it.",
        "",
    ]

    by_category: dict[str, list[dict[str, Any]]] = {}
    for lesson in result.applicable:
        by_category.setdefault(lesson["category"], []).append(lesson)

    lines += ["| category | applies | checked here |", "|---|---:|---:|"]
    for category_id, lessons in by_category.items():
        name = result.categories.get(category_id, {}).get("title", category_id)
        here = sum(1 for lesson in lessons if lesson["id"] in result.checked)
        lines.append(f"| {name} | {len(lessons)} | {here} |")
    lines.append("")

    hits: dict[str, int] = {}
    for finding in result.findings:
        lesson_id = finding.rule.split("/", 1)[1].removesuffix("-capped")
        hits[lesson_id] = hits.get(lesson_id, 0) + 1

    for category_id, lessons in by_category.items():
        name = result.categories.get(category_id, {}).get("title", category_id)
        lines += [f"## {name}", ""]
        for lesson in sorted(lessons, key=lambda l: (_SEVERITY_ORDER[l["severity"]], l["id"])):
            if lesson["id"] not in result.checked:
                status = "not checked here"
            elif lesson["id"] in hits:
                status = f"**{hits[lesson['id']]} match(es) in the report**"
            else:
                status = "checked, no match"
            lines += [
                f"### {lesson['id']} — {lesson['title']}",
                "",
                f"*{lesson['severity']}* · {status}",
                "",
                f"**Symptom.** {lesson['symptom']}",
                "",
                f"**Prevention.** {lesson['prevention']}",
                "",
            ]
            if lesson["id"] not in result.checked and lesson["detection"]:
                recipes = "; ".join(f"`{d['kind']}`: {d['recipe']}" for d in lesson["detection"])
                lines += [f"**How to check it yourself.** {recipes}", ""]
    return "\n".join(lines) + "\n"
