"""Build, check and query the function index.

WHAT IT CANNOT ANSWER, AND WHY THAT IS NOT A GAP
------------------------------------------------
It cannot tell you a concept already has an owner. A re-implementation creates no
edge to the original — new name, new file, calling nothing the original calls — so
every reachability structure renders it as healthy new code. Uniqueness is not
derivable from reachability. A capability index (`owners_yaml`) answers it, and the
lens JOINS that answer in (`canonical.owns` / `canonical.violates`) rather than
pretending to derive it.

HONEST LIMITS OF THE CALL EDGES
-------------------------------
Python edges are NAME-based, not type-resolved. `self.foo()`, `obj.foo()` and a
module-level `foo()` all record an edge to every definition named `foo`. That makes
callers/callees a NAVIGATION aid and blast_radius an UPPER BOUND, never proof. A
type-resolving analysis would be exact and far slower; this is meant to be cheap
enough to run in CI.

Frontend extraction is chosen by `[lens] javascript_parser`, never by what is installed:
"regex" (the default; declarations only, no callees) or "tree-sitter" (functions and their
calls; needs `repolens[stack]`, and refuses to run without it). The committed digest
records the parser, and `--check` refuses to compare a digest built by the other one: a
different parser is a different index, not a stale one. Neither parser performs compiler
type or runtime resolution.
"""
from __future__ import annotations

import warnings
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from ..core.git import run_git
from ..config import Config, load_config
from ..core.console import utf8_console
from ..core.files import iter_files, read_text, read_text_or_none
from ..docs.coverage import _jsdoc_before
from ..featuretrace.model import MARKER_PREFIXES, MARKER_RE
from .settings import LensSettings, from_config

_LAYER_RE = re.compile(r"^\s*(?:#|//|\*)?\s*Layer:\s*(.+?)\s*$", re.M)
_FUNCTION_LENS_RE = re.compile(r"^\s*(?://|#)\s*@functionlens:([A-Za-z][A-Za-z0-9_.-]*)\s*$")
_HEAD_CHARS = 4000
_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


#: Comment lines an id may sit among, above a declaration, outside a JSDoc block.
_ID_WINDOW = 12


def attached_source_id(lines: list[str], index: int) -> str | None:
    """The Function Lens id attached to the declaration on `lines[index]`, without parsing code.

    Comments and decorators immediately above the declaration belong to it, up to `_ID_WINDOW`
    lines. A `/** */` block is skipped whole, however long, so an id written above a JSDoc block
    (where a line comment cannot hide the JSDoc from linters) attaches. A blank line or code stops
    the search, which prevents an id on the preceding function from leaking forward.
    """
    i, budget = index - 1, _ID_WINDOW
    while i >= 0 and budget > 0:
        stripped = lines[i].strip()
        if match := _FUNCTION_LENS_RE.match(stripped):
            return match.group(1)
        if not stripped:
            break
        if stripped.endswith("*/") and (start := _jsdoc_start(lines, i)) is not None:
            i = start - 1
            continue
        if not stripped.startswith(("//", "#", "/*", "*", "*/", "@")):
            break
        i -= 1
        budget -= 1
    return None


def _jsdoc_start(lines: list[str], end: int) -> int | None:
    """The line opening the `/** */` block that closes on `lines[end]`, or None if it is not JSDoc."""
    i = end
    while i >= 0 and "/*" not in lines[i]:
        i -= 1
    if i < 0:
        return None
    opener = lines[i].strip()
    return i if opener.startswith("/**") and not opener.startswith("/**/") else None


def _jsdoc_purpose(s: LensSettings, lines: list[str], index: int) -> str:
    """The summary of the JSDoc block documenting the declaration on `lines[index]`: its first
    paragraph, joined onto one line, ending at a blank line or the first `@` tag."""
    summary: list[str] = []
    for line in (_jsdoc_before(lines, index) or "").splitlines():
        text = line.strip().lstrip("*").strip()
        if text.startswith("@") or (not text and summary):
            break
        if text:
            summary.append(text)
    purpose = " ".join(summary)
    return "" if purpose.startswith(s.placeholder_prefixes) else purpose


#: Frontend declarations. Deliberately shallow — see the module docstring.
_JS_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:async\s+)?(?:function\s+(?P<fn>[A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+(?P<const>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>)",
    re.M,
)


def limits(s: LensSettings) -> list[str]:
    """The caveats that travel inside the artefact, so nobody cites a number without them."""
    out = [
        "Call edges are NAME-based, not type-resolved: every definition sharing a "
        "callee's name is linked. Navigation aid; blast_radius is an upper bound.",
        (f"Frontend records were extracted by the {s.javascript_parser} parser ([lens] javascript_parser)"
         + ("; regex records carry no callees." if s.javascript_parser == "regex"
            else "; call targets remain name-based.")),
        f"tests[] is a NAME match against {s.tests_dir}/. When tests_ambiguous is true the "
        "list belongs to every function sharing the name, not this one. "
        "untested_upper_bound counts functions no test NAMES - not uncovered code.",
        ("postgres_tables adds the table of every ORM class (__tablename__) a function "
         "NAMES, matched by class name: two classes sharing a name share their tables."),
        ("source_id is present only when an attached @functionlens comment declares it. It is a navigation "
         "identity, not evidence about behavior; source_id_duplicate reports a copied id."),
        *s.extra_limits,
    ]
    if s.owners_yaml:
        out.append("This lens CANNOT tell you a concept already has an owner - a "
                   f"re-implementation creates no edge. See {s.owners_yaml}.")
    return out


# ── Joined artefacts ─────────────────────────────────────────────────────────

def _load_canonical_owners(s: LensSettings) -> tuple[dict[str, str], dict[str, str]]:
    """(owner "path::symbol" -> concept it owns, "path:symbol" -> concept it violates).

    Joined rather than re-derived: the registry is the ONLY artefact that can say a
    concept already has an owner.
    """
    owns: dict[str, str] = {}
    violates: dict[str, str] = {}
    path = s.root / s.owners_yaml if s.owners_yaml else None
    if path is None or not path.exists():
        return owns, violates
    try:
        import yaml
    except ImportError:
        # NEVER degrade to "no owners". A digest with empty ownership lists looks
        # valid, passes `--check` against itself on the same machine, and disagrees
        # with CI — which does have PyYAML — about a file the author just verified.
        # A committed, gated artefact must be reproducible or refuse to be produced.
        raise SystemExit(
            f"{s.command} needs PyYAML to read {s.owners_yaml}, and it is not importable.\n"
            "Without it this would emit a digest with no ownership data —\n"
            "valid-looking, self-consistent, and wrong. Install it:\n"
            "    python3 -m pip install PyYAML"
        )
    data = yaml.safe_load(path.read_text()) or {}
    for concept in data.get("concepts") or []:
        name = concept.get("concept")
        owner = concept.get("owner", "")
        for symbol in concept.get("symbols") or []:
            owns[f"{owner}::{symbol}"] = name
        for violation in concept.get("known_violations") or []:
            violates[str(violation)] = name
    return owns, violates


def _load_router_stores(s: LensSettings) -> dict[str, dict[str, list[str]]]:
    """router file -> {mongo: [...], postgres: [...]}, from a router -> datastore map."""
    path = s.root / s.datastore_json if s.datastore_json else None
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    rows = data.get("routers") if isinstance(data, dict) else data
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        file = row.get("file") or row.get("path") or row.get("router")
        if not file:
            continue
        out[str(file)] = {
            "mongo": sorted({str(c) for c in (row.get("mongo_collections") or row.get("mongo") or [])}),
            "postgres": sorted({str(t) for t in (row.get("postgres_tables") or row.get("postgres") or [])}),
        }
    return out


def _file_churn(s: LensSettings) -> dict[str, dict[str, Any]]:
    """path -> {commits, last_changed}. One `git log` pass, not one per file."""
    churn: dict[str, dict[str, Any]] = defaultdict(lambda: {"commits": 0, "last_changed": None})
    try:
        raw = run_git(
            # quotePath off: a non-ASCII path must come back as itself to match `rel`.
            # -z: paths end in NUL, so one holding a newline or edge spaces survives; each
            # commit reads `\x01<date>\0\n<path>\0<path>\0`.
            s.root, "-c", "core.quotePath=false", "log", "--no-merges", "-z",
            "--format=%x01%aI", "--name-only", timeout=180, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}
    stamp: str | None = None
    for token in raw.split("\0"):
        name = token.removeprefix("\n")
        if name.startswith("\x01"):
            stamp = name[1:]
            continue
        if not name:
            continue
        entry = churn[name]
        entry["commits"] += 1
        if entry["last_changed"] is None:
            entry["last_changed"] = stamp  # git log is newest-first
    return dict(churn)


def _test_references(s: LensSettings) -> dict[str, list[str]]:
    """symbol -> test files naming it. Word-boundary matched to avoid substrings."""
    refs: dict[str, set[str]] = defaultdict(set)
    for path in iter_files(s.root, [s.tests_dir], s.test_extensions, s.skip_parts):
        text = read_text_or_none(path)
        if text is None:
            continue
        rel = s.rel(path)
        for token in set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", text)):
            refs[token].add(rel)
    return {k: sorted(v) for k, v in refs.items()}


# ── Python extraction ────────────────────────────────────────────────────────

def _decorator_route(node: ast.AST) -> list[dict[str, str]]:
    """HTTP contracts declared by @router.get("/x") / @app.post("/y") decorators."""
    routes: list[dict[str, str]] = []
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        verb = dec.func.attr.lower()
        if verb not in _HTTP_VERBS:
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
            routes.append({"method": verb.upper(), "path": dec.args[0].value})
    return routes


def _called_names(node: ast.AST) -> set[str]:
    """Every callee NAME in a function body. Name-based — see the module docstring."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if isinstance(fn, ast.Name):
            names.add(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.add(fn.attr)
    return names


#: Calls whose first argument is SQL text: SQLAlchemy `text()`/`execute()`, asyncpg.
_SQL_CALLS = frozenset({"text", "execute", "executemany", "fetch", "fetchrow", "fetchval",
                        "exec_driver_sql"})
# A table after FROM/JOIN/INTO/UPDATE, schema-qualified or bare. A parenthesis after it
# means a function (`FROM unnest(...)`) except after INTO, where it opens the column list;
# one that closes a parenthesis is a column (`EXTRACT(YEAR FROM created_at)`).
_SQL_TABLE_RE = re.compile(
    r'\b(FROM|JOIN|INTO|UPDATE)\s+"?([a-z_][a-z0-9_]*)"?(?:\."?([a-z_][a-z0-9_]*)"?)?(?![\w."]|\s*\))(\s*\()?',
    re.IGNORECASE)
# Words that follow FROM/UPDATE without naming a table: `DO UPDATE SET`, `FOR UPDATE SKIP
# LOCKED`, `FROM LATERAL`.
_SQL_NOT_TABLES = frozenset({"set", "lateral", "only", "select", "values", "skip", "nowait", "of"})
_CTE_RE = re.compile(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)([a-z_][a-z0-9_]*)\s+AS\s*(?:NOT\s+)?"
                     r"(?:MATERIALIZED\s+)?\(", re.IGNORECASE)


def _sql_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def _sql_tables(s: LensSettings, node: ast.AST) -> list[str]:
    """Tables named in SQL text executed inside this function: `schema.table`, or a bare
    `table` that is not a CTE the same statement defines."""
    calls = _SQL_CALLS | {s.sql_call}
    tables: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not sub.args:
            continue
        name = (sub.func.id if isinstance(sub.func, ast.Name) else
                sub.func.attr if isinstance(sub.func, ast.Attribute) else "")
        sql = _sql_text(sub.args[0]) if name in calls else None
        if not sql:
            continue
        ctes = {c.lower() for c in _CTE_RE.findall(sql)}
        for match in _SQL_TABLE_RE.finditer(sql):
            keyword, first, second, paren = match.groups()
            if paren and keyword.upper() != "INTO":
                continue
            if second:
                tables.add(f"{first}.{second}")
            elif (first.lower() not in _SQL_NOT_TABLES and first.lower() not in ctes
                  and not sql[:match.start()].rstrip().upper().endswith("DISTINCT")):
                tables.add(first)
    return sorted(tables)


def _orm_table(cls: ast.ClassDef) -> str | None:
    """The table a mapped class stores: `__tablename__`, qualified by a `schema` in
    `__table_args__` (a dict, or a tuple ending in one)."""
    table = schema = None
    for stmt in cls.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            continue
        name, value = stmt.targets[0].id, stmt.value
        if name == "__tablename__" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            table = value.value
        elif name == "__table_args__":
            args = value.elts[-1] if isinstance(value, ast.Tuple) and value.elts else value
            if isinstance(args, ast.Dict):
                for key, val in zip(args.keys, args.values):
                    if (isinstance(key, ast.Constant) and key.value == "schema"
                            and isinstance(val, ast.Constant) and isinstance(val.value, str)):
                        schema = val.value
    return f"{schema}.{table}" if table and schema else table


def _mongo_collections(s: LensSettings, node: ast.AST) -> list[str]:
    """`<receiver>.<collection>` accesses inside this function."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == s.mongo_receiver
            and not sub.attr.startswith("_")
        ):
            found.add(sub.attr)
    return sorted(found)


def _extract_python(s: LensSettings, path: Path, module_tags: list[str],
                    layer: str | None, orm: dict[str, set[str]] | None = None) -> list[dict[str, Any]]:
    try:
        text = read_text(path)
        with warnings.catch_warnings():  # target code's own SyntaxWarnings are not ours to print
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, OSError, ValueError, RecursionError):
        return []
    if orm is not None:
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and (table := _orm_table(cls)):
                orm[cls.name].add(table)

    records: list[dict[str, Any]] = []
    rel = s.rel(path)
    lines = text.splitlines()

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                doc = ast.get_docstring(child) or ""
                purpose = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
                # A generated header is not a purpose. Say so rather than presenting
                # boilerplate as documentation.
                if purpose.startswith(s.placeholder_prefixes):
                    purpose = ""
                callees = _called_names(child)
                records.append({
                    "key": f"{rel}::{qualname}",
                    "path": rel,
                    "name": child.name,
                    "qualname": qualname,
                    "language": "python",
                    "lineno": child.lineno,
                    "is_async": isinstance(child, ast.AsyncFunctionDef),
                    "is_private": child.name.startswith("_"),
                    "purpose": purpose,
                    "source_id": attached_source_id(lines, child.lineno - 1),
                    "feature_tags": module_tags,
                    "layer": layer,
                    "routes": _decorator_route(child),
                    "callees": sorted(callees),
                    "guards": sorted(callees & s.guard_calls),
                    "postgres_tables": _sql_tables(s, child),
                    "mongo_collections": _mongo_collections(s, child),
                    # Names read, for the ORM join in build(); dropped before indexing.
                    "_names": sorted({n.id for n in ast.walk(child) if isinstance(n, ast.Name)}),
                })
                visit(child, f"{qualname}.")

    visit(tree, "")
    return records


def _extract_frontend(s: LensSettings, path: Path, module_tags: list[str],
                      layer: str | None) -> list[dict[str, Any]]:
    text = read_text_or_none(path)
    if text is None:
        return []
    rel = s.rel(path)
    lines = text.splitlines()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if s.javascript_parser == "tree-sitter":
        from ..core import javascript
        facts = javascript.parse_source(text, path.suffix.lower())
        language = "typescript" if path.suffix.lower() in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
        for symbol in facts.symbols:
            if symbol.kind != "function":
                continue
            out.append({
                "key": f"{rel}::{symbol.qualified}", "path": rel, "name": symbol.name,
                "qualname": symbol.qualified, "language": language, "lineno": symbol.line,
                "is_async": symbol.is_async, "is_private": symbol.name.startswith("_"),
                "purpose": _jsdoc_purpose(s, lines, symbol.line - 1),
                "feature_tags": module_tags, "layer": layer, "routes": [],
                "source_id": attached_source_id(lines, symbol.line - 1),
                # JSX renders are recorded beside calls; a rendered component is not a callee.
                "callees": sorted({called.rsplit(".", 1)[-1] for owner, called, _line, kind in facts.calls
                                   if owner == symbol.qualified and kind == "CALLS"}),
                "guards": [], "postgres_tables": [], "mongo_collections": [],
                "extraction": "tree-sitter",
            })
        return out
    for m in _JS_DECL_RE.finditer(text):
        name = m.group("fn") or m.group("const")
        if not name or name in seen:
            continue
        seen.add(name)
        lineno = text.count("\n", 0, m.start()) + 1
        out.append({
            "key": f"{rel}::{name}",
            "path": rel,
            "name": name,
            "qualname": name,
            "language": "javascript",
            "lineno": lineno,
            "is_async": False,
            "is_private": name.startswith("_"),
            "purpose": _jsdoc_purpose(s, lines, lineno - 1),
            "source_id": attached_source_id(lines, lineno - 1),
            "feature_tags": module_tags,
            "layer": layer,
            "routes": [],
            "callees": [],
            "guards": [],
            "postgres_tables": [],
            "mongo_collections": [],
        })
    return out


def _module_tags(head: str) -> list[str]:
    """The FeatureTrace tags declared on marker lines in a file's header.

    Read with FeatureTrace's own grammar and its own idea of a marker line, so the lens
    and the maps can never disagree about which tag a file carries. A private pattern
    used to stop at the first underscore, filing `demo_bank` markers under `demo`.

    Two further differences from that pattern, both deliberate: a tag named only on a
    `Related:` line is no longer counted (it declares a neighbour, not the file's own
    feature; five files lost a tag this way), and tags keep their case, as FeatureTrace's
    `MARKER_RE` does, instead of being lowercased.
    """
    tags: set[str] = set()
    for line in head.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(MARKER_PREFIXES):
            match = MARKER_RE.search(stripped)
            if match:
                tags.add(match.group(1))
    return sorted(tags)


def _module_context(path: Path) -> tuple[list[str], str | None]:
    """FeatureTrace tags and Layer from the file header."""
    head = (read_text_or_none(path) or "")[:_HEAD_CHARS]
    layer_match = _LAYER_RE.search(head)
    return _module_tags(head), (layer_match.group(1).strip() if layer_match else None)


# ── Build ────────────────────────────────────────────────────────────────────

def require_parser(s: LensSettings) -> None:
    """Refuse to build with a configured parser that cannot run. Falling back to regex
    would emit an index with fewer functions and no callees that passes `--check`
    against itself on this machine and disagrees with every machine that has the extra."""
    if s.javascript_parser != "tree-sitter" or not s.frontend_roots:
        return
    from ..core import javascript
    if not javascript.available():
        raise SystemExit(
            f"{s.command}: [lens] javascript_parser = \"tree-sitter\", and the Tree-sitter "
            "grammars are not importable.\nInstall them (python -m pip install 'repolens[stack]'), "
            "or set [lens] javascript_parser = \"regex\" and regenerate the committed index.")


def build(s: LensSettings, with_churn: bool = False) -> dict[str, Any]:
    require_parser(s)
    owns, violates = _load_canonical_owners(s)
    router_stores = _load_router_stores(s)
    test_refs = _test_references(s)
    churn = _file_churn(s) if with_churn else {}

    records: list[dict[str, Any]] = []
    orm: dict[str, set[str]] = defaultdict(set)
    for roots, suffixes, extract in (
        (s.python_roots, [".py"], partial(_extract_python, orm=orm)),
        (s.frontend_roots, s.frontend_extensions, _extract_frontend),
    ):
        for path in iter_files(s.root, roots, suffixes, s.skip_parts):
            tags, layer = _module_context(path)
            records.extend(extract(s, path, tags, layer))

    # A function that names a mapped class touches that class's table. Matched by class
    # NAME, like the call edges, so it inherits the same ambiguity (see limits()).
    for rec in records:
        via_orm = {t for n in rec.pop("_names", ()) for t in orm.get(n, ())}
        if via_orm:
            rec["postgres_tables"] = sorted(set(rec["postgres_tables"]) | via_orm)

    # Name -> definitions, for the caller edges. Name-based by design.
    by_name: dict[str, list[str]] = defaultdict(list)
    by_source_id: dict[str, list[str]] = defaultdict(list)
    for rec in records:
        by_name[rec["name"]].append(rec["key"])
        if rec.get("source_id"):
            by_source_id[rec["source_id"]].append(rec["key"])

    callers: dict[str, set[str]] = defaultdict(set)
    for rec in records:
        for callee in rec["callees"]:
            for target in by_name.get(callee, ()):
                if target != rec["key"]:
                    callers[target].add(rec["key"])

    index: dict[str, dict[str, Any]] = {}
    for rec in records:
        key = rec["key"]
        # How many definitions share this bare name. When >1, every name-based edge
        # into or out of this function is ambiguous, and the caller list is a UNION
        # across all of them. Surfacing the count is the difference between a useful
        # upper bound and a misleading precise-looking number.
        homonyms = len(by_name.get(rec["name"], ()))
        stores = router_stores.get(rec["path"], {})
        rec_callees = sorted({t for c in rec["callees"] for t in by_name.get(c, ()) if t != key})
        index[key] = {
            **{k: v for k, v in rec.items() if k != "callees"},
            "callers": sorted(callers.get(key, ())),
            "callees": rec_callees,
            "blast_radius": len(callers.get(key, ())),
            "homonyms": homonyms,
            "edges_ambiguous": homonyms > 1,
            "source_id_duplicate": bool(rec.get("source_id") and len(by_source_id[rec["source_id"]]) > 1),
            "canonical": {
                "owns": owns.get(f"{rec['path']}::{rec['name']}"),
                "violates": violates.get(f"{rec['path']}:{rec['name']}"),
            },
            "router_stores": stores or None,
            "tests": test_refs.get(rec["name"], [])[:12],
            # Test references are NAME matches and inherit the same ambiguity as the
            # call edges. Flagged rather than dropped.
            "tests_ambiguous": homonyms > 1,
            # None unless --with-churn, and EXCLUDED from the digest hash: a gate that
            # fails on which flag somebody passed gets switched off.
            "churn": churn.get(rec["path"]),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "javascript_parser": s.javascript_parser,
        "counts": {
            "total": len(index),
            "python": sum(1 for r in index.values() if r["language"] == "python"),
            "javascript": sum(1 for r in index.values() if r["language"] != "python"),
            "routed": sum(1 for r in index.values() if r["routes"]),
            "guarded": sum(1 for r in index.values() if r["guards"]),
            # An UPPER BOUND on genuinely untested functions.
            "untested_upper_bound": sum(1 for r in index.values() if not r["tests"]),
            "tests_name_ambiguous": sum(1 for r in index.values() if r["tests_ambiguous"]),
            "ambiguous_edges": sum(1 for r in index.values() if r["edges_ambiguous"]),
            "stable_ids": sum(1 for r in index.values() if r.get("source_id")),
            "duplicate_stable_ids": len([stable_id for stable_id, keys in by_source_id.items() if len(keys) > 1]),
        },
        "limits": limits(s),
        "functions": index,
    }


def digest(data: dict[str, Any]) -> dict[str, Any]:
    """The committed summary: counts, limits, high-signal lists, and a content hash.

    The hash covers every record except `churn`, and never `generated_at`: a hash that
    moves without the code moving makes every run report dirty.
    """
    hashable = {
        key: {k: v for k, v in rec.items() if k != "churn"}
        for key, rec in data["functions"].items()
    }
    payload = json.dumps(hashable, sort_keys=True, separators=(",", ":"))
    fns = data["functions"]
    return {
        # Outside the hash on purpose, so digests committed before it existed stay valid.
        "javascript_parser": data.get("javascript_parser", "regex"),
        "counts": data["counts"],
        "limits": data["limits"],
        "content_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "canonical_owners": sorted(k for k, r in fns.items() if r["canonical"]["owns"]),
        "canonical_duplicates": sorted(k for k, r in fns.items() if r["canonical"]["violates"]),
        "ambiguous_names": sorted({r["name"] for r in fns.values() if r["edges_ambiguous"]}),
    }


def write_cache(s: LensSettings, data: dict[str, Any]) -> None:
    """The full index: compact, gitignored, a cache rather than a reviewable artefact."""
    s.out_full.parent.mkdir(parents=True, exist_ok=True)
    s.out_full.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))


def _canonical(value: Any) -> Any:
    """A settings value as JSON that is the same in every process: sets sorted, paths
    and patterns as text."""
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, re.Pattern):
        return value.pattern
    return value


def source_stamp(s: LensSettings) -> str:
    """A fingerprint of every input the index is built from: path, size and mtime.

    A stat per file and no reads, so it is cheap enough to take on every lookup. It errs
    one way only: touching a file without changing it forces a rebuild, which costs time
    and never a wrong answer. Git history (`--with-churn`) is not an input it covers.

    The settings are hashed as sorted JSON, not `repr(s)`: `repr` of a frozenset follows
    the per-process string hash, so every new process took a different stamp and every
    lookup rebuilt the index.

    `javascript_parser` is a settings field, so it is part of the stamp: a cache built by
    the other parser is rebuilt, not answered from."""
    h = hashlib.sha256(json.dumps({f.name: _canonical(getattr(s, f.name)) for f in fields(s)},
                                  sort_keys=True).encode())
    inputs = {
        *iter_files(s.root, s.python_roots, [".py"], s.skip_parts),
        *iter_files(s.root, s.frontend_roots, s.frontend_extensions, s.skip_parts),
        *iter_files(s.root, [s.tests_dir], s.test_extensions, s.skip_parts),
        *(s.root / p for p in (s.owners_yaml, s.datastore_json) if p),
    }
    for path in sorted(inputs):
        try:
            st = path.stat()
        except OSError:
            continue
        h.update(f"{s.rel(path)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def committed_parser(old_digest: dict[str, Any]) -> str:
    """The parser a committed digest was built with. A digest from before the setting
    existed was built by regex whenever it could have been compared by `--check` on a
    machine without the stack extras, so a missing field reads as regex."""
    return str(old_digest.get("javascript_parser") or "regex")


def parser_mismatch(s: LensSettings, old_digest: dict[str, Any]) -> str | None:
    committed = committed_parser(old_digest)
    if committed == s.javascript_parser:
        return None
    return (f"function lens digest was committed with {committed}, this run would use "
            f"{s.javascript_parser}; set [lens] javascript_parser.\n"
            f"  To check the committed digest: [lens] javascript_parser = \"{committed}\"\n"
            f"  To switch parsers: set it to \"{s.javascript_parser}\", run {s.command}, "
            "and commit the regenerated digest.\n"
            "Not compared: a different parser yields a different index, which is not staleness.")


def load_index(s: LensSettings) -> dict[str, Any]:
    """The cached full index, rebuilt first when any input changed since it was built.

    The cache used to be read whenever it existed, so a lookup after an edit, a pull or
    a branch switch answered from the tree as it had been: callers added since were
    missing and callers removed since were listed, with nothing to say so."""
    stamp = source_stamp(s)
    data: dict[str, Any] | None = None
    if s.out_full.exists():
        try:
            data = json.loads(s.out_full.read_text())
        except (OSError, ValueError):
            data = None
        if data is not None and data.get("source_stamp") != stamp:
            print("the cached index is older than the tree; rebuilding it.", file=sys.stderr)
            data = None
    else:
        print("building the lens index (gitignored cache, first run only)...", file=sys.stderr)
    if data is None:
        data = build(s)
        data["source_stamp"] = stamp
        write_cache(s, data)
    return data


# ── Lookup ───────────────────────────────────────────────────────────────────

def lookup(data: dict[str, Any], query: str, limit: int = 12) -> list[dict[str, Any]]:
    """Find functions by exact key, stable source id, exact name, then substring."""
    fns = data["functions"]
    if query in fns:
        return [fns[query]]
    stable = [r for r in fns.values() if r.get("source_id") == query]
    if stable:
        return stable[:limit]
    exact = [r for r in fns.values() if r["name"] == query or r["qualname"] == query]
    if exact:
        return exact[:limit]
    q = query.lower()
    return sorted(
        (r for r in fns.values() if q in r["name"].lower() or q in r["key"].lower()),
        key=lambda r: (len(r["name"]), r["key"]),
    )[:limit]


def render_lens(rec: dict[str, Any]) -> str:
    """The one-page lens for one function."""
    L: list[str] = []
    L.append(f"\n{rec['key']}")
    L.append("=" * min(len(rec["key"]), 100))
    L.append(f"  Purpose      {rec['purpose'] or '(no docstring — generated header or none)'}")
    L.append(f"  Location     {rec['path']}:{rec['lineno']}"
             f"  [{rec['language']}{', async' if rec['is_async'] else ''}]")
    if rec.get("source_id"):
        suffix = "  !! DUPLICATE" if rec.get("source_id_duplicate") else ""
        L.append(f"  Stable id    {rec['source_id']}{suffix}")
    if rec.get("layer") or rec.get("feature_tags"):
        L.append(f"  Feature      {', '.join(rec['feature_tags']) or '(untagged)'}"
                 f"   Layer: {rec.get('layer') or '(none)'}")
    if rec["routes"]:
        L.append("  Entry points " + ", ".join(f"{r['method']} {r['path']}" for r in rec["routes"]))
    if rec["guards"]:
        L.append(f"  Security     {', '.join(rec['guards'])}")
    stores = []
    if rec["postgres_tables"]:
        stores.append("pg: " + ", ".join(rec["postgres_tables"]))
    if rec["mongo_collections"]:
        stores.append("mongo: " + ", ".join(rec["mongo_collections"]))
    if rec.get("router_stores"):
        rs = rec["router_stores"]
        if rs.get("postgres"):
            stores.append("router pg: " + ", ".join(rs["postgres"][:6]))
        if rs.get("mongo"):
            stores.append("router mongo: " + ", ".join(rs["mongo"][:6]))
    if stores:
        L.append("  Stores       " + " | ".join(stores))
    if rec["canonical"]["owns"]:
        L.append(f"  Canonical    OWNS concept '{rec['canonical']['owns']}'")
    if rec["canonical"]["violates"]:
        L.append(f"  Canonical    *** DUPLICATE of '{rec['canonical']['violates']}' — call the owner")
    if rec.get("edges_ambiguous"):
        L.append(f"  !! AMBIGUOUS  {rec['homonyms']} functions share the name "
                 f"'{rec['name']}'. Edges are name-based, so the callers below are the "
                 f"UNION across all {rec['homonyms']} — not this definition's own. "
                 f"Open the call sites before acting on them.")
    L.append(f"  Callers      {len(rec['callers'])} (blast radius, upper bound)")
    for c in rec["callers"][:8]:
        L.append(f"                 <- {c}")
    if len(rec["callers"]) > 8:
        L.append(f"                 ... {len(rec['callers']) - 8} more")
    proof = ', '.join(rec['tests'][:5]) or '*** no test names this function'
    if rec.get('tests_ambiguous'):
        proof += '  (name-matched across all definitions - may not be this one)'
    L.append(f'  Proof        {proof}')
    if rec.get("churn"):
        L.append(f"  Churn        {rec['churn']['commits']} commits, last {rec['churn']['last_changed']}")
    return "\n".join(L)


# ── HTML ─────────────────────────────────────────────────────────────────────

def render_html(data: dict[str, Any], content_sha: str = "", s: LensSettings | None = None) -> str:
    """Render the committed HTML view.

    DETERMINISM IS A HARD REQUIREMENT, not a nicety. This file is committed and is a
    few very long lines. It used to stamp the build's wall clock, so every
    regeneration dirtied the tree and every branch conflicted with every other on a
    line Git cannot merge. The content hash says the same useful thing — WHICH TREE
    this reflects — and changes only when the content does.
    """
    title = s.title if s else "Function Lens"
    command = s.command if s else "repolens lens"
    full = s.rel(s.out_full) if s else "function_lens.json"
    c = data["counts"]
    rows = []
    interesting = sorted(
        (r for r in data["functions"].values() if r["routes"] or r["guards"] or r["canonical"]["owns"]),
        key=lambda r: (-len(r["callers"]), r["key"]),
    )[:400]
    for r in interesting:
        rows.append(
            "<tr><td><code>{k}</code></td><td>{p}</td><td>{g}</td><td>{rt}</td>"
            "<td style='text-align:right'>{b}</td><td>{t}</td></tr>".format(
                k=r["key"], p=(r["purpose"] or "—")[:120],
                g=", ".join(r["guards"]) or "—",
                rt=", ".join(f"{x['method']} {x['path']}" for x in r["routes"]) or "—",
                b=len(r["callers"]),
                t=("%d" % len(r["tests"])) if r["tests"] else "<b>0</b>",
            )
        )
    limits_html = "".join(f"<li>{x}</li>" for x in data["limits"])
    return f"""<!doctype html><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font:14px/1.5 Inter,system-ui,sans-serif;margin:2rem;color:#1c1917;background:#fff}}
 h1{{font-family:Manrope,sans-serif}} code{{font:12px JetBrains Mono,monospace}}
 table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #e7e5e4;padding:6px;text-align:left;vertical-align:top}}
 th{{background:#f5f5f4}} .warn{{background:#fef3c7;padding:1rem;border-radius:8px}}
</style>
<h1>{title}</h1>
<p>Content <code>{content_sha[:12] or 'unknown'}</code> —
 <b>{c['total']}</b> functions ({c['python']} python, {c['javascript']} frontend);
 {c['routed']} routed, {c['guarded']} guarded, {c.get('stable_ids', 0)} stable ids,
 <b>{c['untested_upper_bound']}</b> with no test naming them.</p>
<div class="warn"><b>Read the limits before citing a number:</b><ul>{limits_html}</ul></div>
<p>Showing the {len(interesting)} routed / guarded / canonical-owner functions,
 highest blast radius first. Full data: <code>{full}</code>,
 or <code>{command} --lookup &lt;name&gt;</code>.</p>
<table><tr><th>Function</th><th>Purpose</th><th>Guards</th><th>Route</th><th>Callers</th><th>Tests</th></tr>
{''.join(rows)}
</table>"""


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None, *, config: Config | None = None,
         settings: LensSettings | None = None, prog: str | None = None) -> int:
    utf8_console()
    s = settings or from_config(config or load_config())
    ap = argparse.ArgumentParser(prog=prog, description=f"Generate or query the {s.title}.")
    ap.add_argument("--lookup", metavar="NAME", help="show the one-page lens for a function")
    ap.add_argument("--json", action="store_true", help="with --lookup, emit raw JSON")
    ap.add_argument("--check", action="store_true",
                    help="CI: exit 1 if the artefact is stale, 2 if it was built by another javascript_parser")
    ap.add_argument("--with-churn", action="store_true",
                    help="include git churn (one extra `git log` pass)")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args(argv)

    if args.lookup:
        # The local cache, when it still describes this tree; otherwise rebuilt and left
        # behind for the next lookup.
        data = load_index(s)
        hits = lookup(data, args.lookup, args.limit)
        if not hits:
            print(f"no function matching {args.lookup!r}")
            return 1
        if args.json:
            print(json.dumps(hits, indent=2))
        else:
            for rec in hits:
                print(render_lens(rec))
            print(f"\n{len(hits)} match(es).")
        return 0

    old: dict[str, Any] | None = None
    if args.check:
        if not s.out_digest.exists():
            print(f"function lens digest missing — run:\n  {s.command}")
            return 1
        old = json.loads(s.out_digest.read_text())
        # Before the build: the refusal needs no index, and must not wait for one.
        if (mismatch := parser_mismatch(s, old)) is not None:
            print(mismatch)
            return 2

    stamp = source_stamp(s)  # taken BEFORE the build: an edit during it reads as stale
    data = build(s, with_churn=args.with_churn)
    data["source_stamp"] = stamp  # cache only; the digest hashes `functions` alone

    if old is not None:
        fresh = digest(data)
        if old.get("content_sha256") != fresh["content_sha256"]:
            print("function lens is STALE. Regenerate and commit:")
            print(f"  {s.command}")
            print(f"  functions: {old.get('counts', {}).get('total')} committed "
                  f"-> {fresh['counts']['total']} on disk")
            for label in ("canonical_duplicates", "canonical_owners"):
                added = set(fresh[label]) - set(old.get(label, []))
                gone = set(old.get(label, [])) - set(fresh[label])
                for k in sorted(added)[:5]:
                    print(f"    + {label}: {k}")
                for k in sorted(gone)[:5]:
                    print(f"    - {label}: {k}")
            return 1
        print(f"function lens: OK ({fresh['counts']['total']} functions)")
        return 0

    s.out_digest.parent.mkdir(parents=True, exist_ok=True)
    # Computed ONCE and shared, so the committed HTML and the committed digest can never
    # disagree about which tree they describe.
    dg = digest(data)
    s.out_digest.write_text(json.dumps(dg, indent=1, sort_keys=True) + "\n")
    # Keeps `generated_at`, useful locally and harmless because the cache is never committed.
    write_cache(s, data)
    print(f"  wrote {s.rel(s.out_digest)}  (committed)")
    print(f"  wrote {s.rel(s.out_full)}  (local cache, gitignored)")
    if s.out_html:
        s.out_html.parent.mkdir(parents=True, exist_ok=True)
        s.out_html.write_text(render_html(data, dg["content_sha256"], s))
        print(f"  wrote {s.rel(s.out_html)}")
    c = data["counts"]
    print(f"  {c['total']} functions — {c['python']} python, {c['javascript']} frontend, "
          f"{c['routed']} routed, {c['guarded']} guarded, {c.get('stable_ids', 0)} stable ids "
          f"({c.get('duplicate_stable_ids', 0)} duplicate), {c['untested_upper_bound']} with no test naming them")
    return 0
