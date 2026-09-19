"""`repolens docs generate`: documentation drafted from the evidence graph of the scanned application.

    repolens docs generate                   write .repolens/docs-generated/
    repolens docs generate --out DIR         write somewhere else
    repolens docs generate --max-groups 60   draw more feature groups in the architecture map

It runs the same read-only analysis as `repolens analyze` and writes:

  openapi.json              OpenAPI 3.1 for the application's own routes, with gaps kept
  schema.mmd, schema.md     a Mermaid ER diagram of the declared tables and collections
  architecture.mmd/.md      pages -> API -> code -> stores, one node per feature group
  debugging.md              evidence-led trace points and production-safe instrumentation guidance
  features/<group>.md       a mindmap and evidence tables for one route area
  features/<group>.context.json  the same facts as data, for a person or an agent to write prose from
  index.md                  what was generated, whether the analysis was complete, and the limits

Nothing is invented. A request or response schema the source does not declare stays a gap
(`x-repolens-gaps`), a table whose DDL was not read has no columns, and a feature group is a
route area, not a business feature. The command never imports or runs the application,
writes only inside the output directory (relative to the root unless absolute), refuses to write
through a symlink, and never replaces or deletes a file there that it did not write. It exits 2 when the analysis is incomplete (the
files are still written, and say so) or cannot be written, else 0.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import tomllib
from collections import defaultdict
from pathlib import Path

from ..analysis import Analysis, analyze
from ..core.files import is_test_code, is_test_path
from ..impact import columns, postgres
from ..impact.config import Config
from ..impact.features import (HANDLER_EDGES, STORE_EDGES, FeatureGroup, edge_index, endpoint_status, feature_groups,
                               reach, route_path)
from ..impact.model import Graph, Node
from ..impact.render import _safe_label, markdown_cell, markdown_inline
from ..impact.source import read_source
from ..provenance import describe_build, tool_build

DEFAULT_OUT = ".repolens/docs-generated"
DEFAULT_MAX_GROUPS = 40
MAX_ENTITIES = 150
MAX_STORES_DRAWN = 60
#: Leaves per mindmap section; Mermaid refuses a diagram over 50,000 characters by default.
MAX_LEAVES = 25
#: Edges in the architecture flowchart. Mermaid refuses more than 500 by default, and a diagram
#: cannot raise the limit (`maxEdges` is a secure setting).
MAX_EDGES = 450

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_BODY_METHODS = frozenset({"post", "put", "patch"})
_STORE_KINDS = {"postgres_table": "PostgreSQL table", "mongo_collection": "MongoDB collection"}
#: Hops walked from a handler over confident calls when collecting its stores (as `feature_groups` walks).
STORE_DEPTH = 4
#: The gap an operation served only by a route a generated artifact declares carries.
DECLARED_HANDLER_GAP = "handler declared by an artifact, not scanned"
#: Diagnostics a feature page lists as gaps.
GAP_CODES = ("API_CALL_WITHOUT_HANDLER", "API_METHOD_MISMATCH", "API_HANDLER_WITHOUT_STATIC_CALLER",
             "SQL_INJECTION_RISK", "SQL_UNKNOWN_COLUMN", "DYNAMIC_HTTP_REQUEST")
#: What static analysis cannot say about any feature; every context pack repeats it.
UNKNOWNS = [
    "Business purpose and rules: the source shows what the code touches, not why.",
    "Authorization: middleware, guards and server-action checks are not modelled.",
    "Request and response shapes the source does not declare as models.",
    "Runtime behaviour: which branches run, feature flags, environment configuration.",
]
LIMITS = {
    "openapi.json": "Paths, methods and path parameters come from route declarations; request and response "
                    "schemas are only named when the source declares a model, and every unresolved part is "
                    "listed in `x-repolens-gaps` rather than guessed (conservatively: `authentication` and "
                    "`query parameters` are listed on every operation). A route that answers every method (a "
                    "Pages Router API route) is under the path-item extension `x-repolens-any-method`, which "
                    "Swagger UI and client generators do not show.",
    "schema.mmd": "The ER diagram shows only columns declared in scanned `CREATE TABLE`/`ALTER TABLE` DDL or a "
                  "PostgreSQL Prisma schema, and relationships declared as foreign keys; MongoDB fields are not inferred. "
                  "SQL files apply in path order, with `DROP TABLE`, `DROP COLUMN`, `RENAME` and `SET`/`DROP NOT NULL` "
                  "applied; a dropped constraint is not, and `public.t` and `t` are separate tables.",
    "architecture.mmd": "Feature groups are route areas named by their first path segment, not business features, "
                        "and the edges are static evidence, not observed traffic.",
    "debugging.md": "Trace points are candidates from static evidence. Repository Lens installs no runtime "
                    "instrumentation and cannot prove which path executes in production.",
    "features/": "Each group page lists the evidence the scan found and its diagnostics; an empty section means "
                 "nothing was found, not that nothing exists.",
}


# ── Mermaid text ─────────────────────────────────────────────────────────────────
def _flat(text: object, limit: int = 100) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()[:limit]


def flow_label(text: object) -> str:
    """Repository text inside a flowchart `["..."]` label (entity-escaped, as `impact.render` does)."""
    return _safe_label(text)


def leaf_label(text: object, limit: int = 80) -> str:
    """Repository text inside a mindmap or ER `["..."]` label: no quote, bracket, backtick or newline.

    Entity codes rather than `mermaid_label`'s `[` -> `(`: Mermaid decodes `#91;` for every
    diagram type, and a Next.js path drawn as `app/(orderId)/page.tsx` would name a route
    group instead of the dynamic segment `[orderId]`."""
    return _safe_label(_flat(text, limit))


def _token(text: str) -> str:
    """An ER attribute type or name: word characters only."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_") or "unknown"


def _slug(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "group"
    slug = base
    for n in itertools.count(2):
        if slug not in used:
            break
        slug = f"{base}-{n}"
    used.add(slug)
    return slug


# ── graph lookups ────────────────────────────────────────────────────────────────
class _Index:
    """Edges, endpoint status and gap diagnostics by node and path, built once per graph."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self.outgoing, self.incoming = edge_index(graph)
        self.status = endpoint_status(graph, self.outgoing, self.incoming)
        self.by_path: dict[str, list[str]] = defaultdict(list)
        for node in graph.nodes.values():
            if node.path:
                self.by_path[node.path].append(node.id)
        # Gap diagnostics by position in `graph.issues`, so a group's list keeps the issue order.
        self.gaps_by_node: dict[str, list[int]] = defaultdict(list)
        self.gaps_by_path: dict[str, list[int]] = defaultdict(list)
        for position, issue in enumerate(graph.issues):
            if issue.code in GAP_CODES:
                for node_id in issue.node_ids:
                    self.gaps_by_node[node_id].append(position)
                self.gaps_by_path[_evidence_path(issue.evidence)].append(position)

    def out(self, node_id: str, kind: str) -> list:
        return [e for e in self.outgoing.get(node_id, ()) if e.kind == kind]

    def into(self, node_id: str, kind: str) -> list:
        return [e for e in self.incoming.get(node_id, ()) if e.kind == kind]

    def callers(self, endpoint: str) -> list[str]:
        """Evidence of each call to `endpoint` from application code (test code is not a caller)."""
        return sorted({e.evidence for e in self.into(endpoint, "CALLS_API")
                       if not is_test_code(_evidence_path(e.evidence))})

    def handler_edges(self, endpoint: str) -> list:
        """What serves `endpoint`: scanned handlers first, then routes a generated artifact declares."""
        edges = [e for e in self.outgoing.get(endpoint, ()) if e.kind in HANDLER_EDGES]
        return sorted(edges, key=lambda e: (e.kind != "HANDLES_API", e.target))

    def stores_from(self, starts: list[str]) -> list[Node]:
        """Stores used by `starts` or by code they reach over confident calls (`features.reach`).

        A name-only or ambiguous call can be any function of that name; following them made
        every handler of a large backend reach most of its stores."""
        found: dict[str, Node] = {}
        for start in starts:
            for node_id in reach(self.outgoing, start, STORE_DEPTH):
                for edge in self.outgoing.get(node_id, ()):
                    target = self.graph.nodes.get(edge.target)
                    if edge.kind in STORE_EDGES and target is not None and target.kind in _STORE_KINDS:
                        found[target.id] = target
        return sorted(found.values(), key=lambda n: n.label)

    def symbol(self, node_id: str) -> dict:
        node = self.graph.nodes[node_id]
        return {"symbol": node.metadata.get("qualified_name") or node.label, "file": node.path, "line": node.line}


def openapi_path(node: Node) -> tuple[str, list[str]]:
    """The OpenAPI path template for an endpoint and its parameter names, in order.

    The source spelling (`metadata["path"]`) is used when the scan kept it; otherwise each
    `{dynamic}` segment of the matching shape becomes `{param1}`, `{param2}`, ..."""
    path = str(node.metadata.get("path") or "")
    if not path:
        counter = itertools.count(1)
        path = re.sub(r"\{dynamic\}", lambda _m: f"{{param{next(counter)}}}", route_path(node))
    path = re.sub(r"(?<=/):([A-Za-z_]\w*)", r"{\1}", path) or "/"
    names: list[str] = []

    def unique(match: re.Match) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", match.group(1).lstrip(".")) or "param"
        candidate = name
        for n in itertools.count(2):
            if candidate not in names:
                break
            candidate = f"{name}{n}"
        names.append(candidate)
        return "{" + candidate + "}"

    path = re.sub(r"\{([^{}]*)\}", unique, path)
    return path, names


def _evidence_path(evidence: str) -> str:
    return re.sub(r":\d+(?::\d+)?$", "", evidence or "")


# ── OpenAPI ──────────────────────────────────────────────────────────────────────
def _project(root: Path) -> tuple[str, str]:
    """(title, version) from `package.json`, then `pyproject.toml`, field by field; non-empty strings only."""
    found: dict[str, str] = {}
    readers = (("package.json", json.loads, lambda data: data),
               ("pyproject.toml", tomllib.loads, lambda data: data.get("project")))
    for name, parse, section in readers:
        try:
            data = section(parse((root / name).read_text(encoding="utf-8-sig")[:1_000_000]))
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(data, dict):
            for key in ("name", "version"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    found.setdefault(key, value.strip())
    return found.get("name") or root.name or "application", found.get("version") or "unversioned"


def build_openapi(analysis: Analysis, root: Path, groups: dict[str, FeatureGroup], index: _Index | None = None) -> dict:
    """The OpenAPI 3.1 document for the scanned application's routes.

    A served endpoint is an operation. A placeholder for a call the scan also matched to a
    served endpoint is left out (the served operation lists that caller), and a call nothing is
    known to serve goes in `x-repolens-unserved-calls`."""
    graph = analysis.graph
    index = index or _Index(graph)
    group_of = {endpoint: g.name for g in groups.values() for endpoint in g.endpoints}
    title, version = _project(root)
    paths: dict[str, dict] = {}
    unserved = []
    operation_ids: set[str] = set()
    for node in sorted((n for n in graph.nodes.values() if n.kind == "endpoint"), key=lambda n: n.label):
        method = str(node.metadata.get("method") or node.label.split(" ", 1)[0]).lower()
        path, names = openapi_path(node)
        status = index.status.get(node.id)
        if status in {"matched", "open", "uncalled"}:
            continue  # callers on the served operation, a URL too open to link, or only test code calls it
        handler_edges = index.handler_edges(node.id)
        handlers = list(dict.fromkeys(e.target for e in handler_edges))
        callers = index.callers(node.id)
        if status != "served":
            messages = sorted({i.message for i in graph.issues if node.id in i.node_ids})
            unserved.append({"method": method.upper(), "path": path, "callers": callers, "issues": messages})
            continue
        requests = [index.symbol(e.target) for e in index.out(node.id, "ACCEPTS_MODEL") if e.target in graph.nodes]
        responses = [index.symbol(e.target) for e in index.out(node.id, "RETURNS_MODEL") if e.target in graph.nodes]
        gaps = ["authentication", "query parameters"]
        if names:
            gaps.append("path parameter types")
        if method in _BODY_METHODS and not requests:
            gaps.append("request body")
        if not responses:
            gaps.append("response schema")
        if node.metadata.get("catch_all"):
            gaps.append("catch-all route: the last parameter spans several segments")
        if not any(e.kind == "HANDLES_API" for e in handler_edges):
            gaps.append(DECLARED_HANDLER_GAP)
        base = re.sub(r"[^A-Za-z0-9]+", "_", f"{method}_{path}").strip("_") or method
        operation_id = base
        for n in itertools.count(2):
            if operation_id not in operation_ids:
                break
            operation_id = f"{base}_{n}"
        operation_ids.add(operation_id)
        handler = index.symbol(handlers[0])
        described = ", ".join(r["symbol"] for r in responses)
        operation = {
            "operationId": operation_id,
            "summary": f"Handled by {handler['symbol']} ({handler['file']})",
            "tags": [group_of.get(node.id, "api")],
            "parameters": [{"name": name, "in": "path", "required": True, "schema": {},
                            "description": "Type not inferred from source"} for name in names],
            "responses": {"default": {"description": (f"Response model {described} named in source; its fields were not resolved"
                                                      if responses else "Response not inferred from source")}},
            "x-repolens-handler": handler,
            "x-repolens-resolution": sorted({e.resolution for e in handler_edges}),
            "x-repolens-callers": callers,
            "x-repolens-stores": [{"name": s.label, "kind": s.kind} for s in index.stores_from(handlers)],
            "x-repolens-feature": group_of.get(node.id, "api"),
            "x-repolens-gaps": gaps,
        }
        if len(handlers) > 1:
            operation["x-repolens-handlers"] = [index.symbol(h) for h in handlers]
        if requests:
            operation["x-repolens-request-models"] = requests
        if responses:
            operation["x-repolens-response-models"] = responses
        if node.metadata.get("dependencies"):
            operation["x-repolens-dependencies"] = list(node.metadata["dependencies"])
        item = paths.setdefault(path, {})
        if method in _HTTP_METHODS:
            item[method] = operation
        elif method == "any":
            # A Pages Router API route answers every method; OpenAPI has no such verb.
            item["x-repolens-any-method"] = operation
        else:
            item.setdefault("x-repolens-other-methods", {})[method.upper()] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version,
                 "description": "Drafted by repolens from static evidence in the source. Gaps are listed per "
                                "operation in x-repolens-gaps; nothing that the source does not declare is filled in."},
        "paths": dict(sorted(paths.items())),
        "tags": [{"name": name} for name in sorted({g for item in paths.values() for op in _operations(item)
                                                   for g in op.get("tags", [])})],
        "x-repolens-build": describe_build(),
        "x-repolens-complete": analysis.complete,
        "x-repolens-incomplete-reasons": analysis.incomplete_reasons(),
        "x-repolens-unserved-calls": unserved,
    }


def _operations(item: dict) -> list[dict]:
    ops = [op for key, op in item.items() if key in _HTTP_METHODS]
    if "x-repolens-any-method" in item:
        ops.append(item["x-repolens-any-method"])
    ops.extend(item.get("x-repolens-other-methods", {}).values())
    return ops


# ── schema ───────────────────────────────────────────────────────────────────────
def _column(table: dict, name: str) -> dict:
    return table["columns"].setdefault(name, {"type": "", "pk": False, "fk": False, "not_null": False})


def _new_table() -> dict:
    #: columns: name -> facts; references: referenced table -> the local columns that point at it
    return {"columns": {}, "files": set(), "references": defaultdict(set)}


def _identifier(identifier) -> str:
    return identifier.name if identifier.args.get("quoted") else identifier.name.lower()


def _column_name(node) -> str | None:
    from sqlglot import exp
    identifier = node.this if isinstance(node, exp.Column) else node
    return _identifier(identifier) if isinstance(identifier, exp.Identifier) else None


def _referenced_table(reference) -> str | None:
    from sqlglot import exp
    table = reference.this.this if isinstance(reference.this, exp.Schema) else reference.this
    return postgres._table_name(table) if isinstance(table, exp.Table) else None


def _column_def(table: dict, item) -> None:
    from sqlglot import exp
    name = _column_name(item.this)
    if not name:
        return
    column = _column(table, name)
    column["type"] = item.args["kind"].sql(dialect="postgres").lower() if item.args.get("kind") else ""
    for constraint in item.args.get("constraints") or ():
        kind = constraint.args.get("kind")
        column["pk"] |= isinstance(kind, exp.PrimaryKeyColumnConstraint)
        column["not_null"] |= isinstance(kind, (exp.NotNullColumnConstraint, exp.PrimaryKeyColumnConstraint))
        if isinstance(kind, exp.Reference) and (referenced := _referenced_table(kind)):
            column["fk"] = True
            table["references"][referenced].add(name)


def _table_keys(table: dict, expression) -> None:
    """Table-level `PRIMARY KEY (…)` and `FOREIGN KEY (…) REFERENCES t` of one statement."""
    from sqlglot import exp
    for key in expression.find_all(exp.PrimaryKey):
        for part in key.expressions:
            if name := _column_name(part.this if isinstance(part, exp.Ordered) else part):
                _column(table, name).update(pk=True, not_null=True)
    for key in expression.find_all(exp.ForeignKey):
        reference = key.args.get("reference")
        referenced = _referenced_table(reference) if reference is not None else None
        for part in key.expressions:
            if name := _column_name(part):
                _column(table, name)["fk"] = True
                if referenced:
                    table["references"][referenced].add(name)


def _sql_tables(root: Path, graph: Graph, max_bytes: int) -> tuple[dict[str, dict], dict[str, str]]:
    """(tables, removed): the tables scanned `.sql` files outside test code leave behind, and why others are gone.

    Files apply in path order (how numbered or timestamped migrations sort) and statements in
    file order, so a later `DROP TABLE`, `DROP COLUMN` or `RENAME` changes what an earlier
    `CREATE TABLE` declared. A dropped constraint is not applied: its name does not say which
    columns it covered."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return {}, {}
    tables: dict[str, dict] = {}
    removed: dict[str, str] = {}
    files = sorted(n.path for n in graph.nodes.values()
                   if n.kind == "file" and n.path and n.path.endswith(".sql") and not is_test_path(n.path))
    for rel in files:
        text = read_source(root, root / rel, max_bytes).text
        if not text or postgres.non_postgres_dialect(text):
            continue
        for _line, statement in postgres.split_statements(text):
            if listed := postgres._DROP_LIST.match(statement.strip()):
                # `DROP TABLE a, b` does not parse; each name is dropped.
                for name in re.split(r"\s*,\s*", listed.group("list")):
                    if tables.pop(postgres._name(name), None) is not None:
                        removed[postgres._name(name)] = f"dropped in {rel}"
                continue
            sql = postgres.psql_variables(statement)[0]
            try:
                try:
                    expression = sqlglot.parse_one(sql, read="postgres")
                except Exception:  # noqa: BLE001 - retried once with a known parser gap worked around
                    if (rewritten := postgres._parser_workaround(sql)) is None:
                        raise
                    expression = sqlglot.parse_one(rewritten, read="postgres")
            except Exception:  # noqa: BLE001 - the scan already reported what does not parse
                continue
            kind = str(getattr(expression, "args", {}).get("kind") or "").upper()
            if not isinstance(expression, (exp.Create, exp.Alter, exp.Drop)) or kind != "TABLE":
                continue
            target = postgres._target(expression)
            name = postgres._table_name(target) if target is not None else None
            if not name:
                continue
            if isinstance(expression, exp.Drop):
                if tables.pop(name, None) is not None:
                    removed[name] = f"dropped in {rel}"
                continue
            if isinstance(expression, exp.Alter) and name not in tables:
                continue  # altering a table no scanned DDL created: its other columns are unknown
            removed.pop(name, None)
            table = tables.setdefault(name, _new_table())
            table["files"].add(rel)
            if isinstance(expression, exp.Create):
                for item in expression.this.expressions if isinstance(expression.this, exp.Schema) else ():
                    if isinstance(item, exp.ColumnDef):
                        _column_def(table, item)
                _table_keys(table, expression)
                continue
            for action in expression.args.get("actions") or ():
                if isinstance(action, exp.ColumnDef):
                    _column_def(table, action)
                elif isinstance(action, exp.Drop) and str(action.args.get("kind") or "").upper() == "COLUMN":
                    if column := _column_name(action.this):
                        table["columns"].pop(column, None)
                        for local in table["references"].values():
                            local.discard(column)
                elif isinstance(action, exp.RenameColumn):
                    old, new = _column_name(action.this), _column_name(action.args.get("to"))
                    if old in table["columns"] and new:
                        table["columns"][new] = table["columns"].pop(old)
                        for local in table["references"].values():
                            if old in local:
                                local.discard(old)
                                local.add(new)
                elif isinstance(action, exp.AlterColumn) and action.args.get("allow_null") is not None:
                    if (column := _column_name(action.this)) in table["columns"]:
                        table["columns"][column]["not_null"] = not action.args["allow_null"]
                elif isinstance(action, exp.AlterRename) and isinstance(action.this, exp.Table):
                    renamed = postgres._table_name(action.this)
                    if renamed and "." not in renamed and "." in name:
                        renamed = f"{name.rsplit('.', 1)[0]}.{renamed}"  # RENAME TO keeps the schema
                    if renamed:
                        tables[renamed] = tables.pop(name)
                        removed[name] = f"renamed to {renamed} in {rel}"
                        removed.pop(renamed, None)
                        table, name = tables[renamed], renamed
                        table["files"].add(rel)
            _table_keys(table, expression)
    for table in tables.values():
        for referenced in list(table["references"]):
            if not table["references"][referenced]:
                del table["references"][referenced]
    return tables, removed


_PRISMA_ID = re.compile(r"@@id\(\s*(?:fields\s*:\s*)?\[([^\]]*)\]")


def _prisma_tables(root: Path, graph: Graph, max_bytes: int) -> tuple[dict[str, dict], list[tuple[str, str, bool]]]:
    """(tables, relations) declared by PostgreSQL Prisma models outside test code.

    A relation is (table, referenced table, required): required when the relation field is not optional."""
    texts = {}
    for node in graph.nodes.values():
        if node.kind == "file" and node.path and node.path.endswith(".prisma") and not is_test_path(node.path):
            text = read_source(root, root / node.path, max_bytes).text
            if text:
                texts[node.path] = columns.strip_prisma_comments(text)
    providers = {m.group(1) for text in texts.values() for m in columns._PRISMA_PROVIDER.finditer(text)}
    if not providers or not providers <= columns.POSTGRES_PRISMA_PROVIDERS:
        return {}, []
    blocks = [(rel, m.group(1), m.group(2), m.group(3)) for rel, text in sorted(texts.items())
              for m in columns._PRISMA_BLOCK.finditer(text)]
    table_names = {}
    for _rel, kind, name, body in blocks:
        if kind == "model":
            mapped = re.search(r'@@map\(\s*(?:name\s*:\s*)?"([^"]+)"', body)
            table_names[name] = mapped.group(1) if mapped else name
    tables: dict[str, dict] = {}
    relations: list[tuple[str, str, bool]] = []
    for rel, kind, name, body in blocks:
        if kind != "model":
            continue
        table = tables.setdefault(table_names[name], _new_table())
        table["files"].add(rel)
        field_columns: dict[str, str] = {}
        for line in body.splitlines():
            match = columns._PRISMA_FIELD.match(line)
            if not match or line.strip().startswith("@@"):
                continue
            field_name, type_name, is_list, optional, attributes = match.groups()
            if type_name in table_names:
                if "@relation" in attributes and "fields:" in attributes:
                    relations.append((table_names[name], table_names[type_name], not optional))
                    for local in re.findall(r"fields:\s*\[([^\]]*)\]", attributes)[:1]:
                        for field_ref in re.split(r"\s*,\s*", local.strip()):
                            if field_ref:
                                _column(table, field_columns.get(field_ref, field_ref))["fk"] = True
                continue
            mapped = columns._PRISMA_MAP.search(attributes)
            field_columns[field_name] = mapped.group(1) if mapped else field_name
            column = _column(table, field_columns[field_name])
            column["type"] = type_name + ("[]" if is_list else "") + ("?" if optional else "")
            column["pk"] |= bool(re.search(r"@id\b", attributes))
            column["not_null"] = not optional
        for composite in _PRISMA_ID.findall(body):
            for field_ref in re.split(r"\s*,\s*", composite.strip()):
                if field_ref:
                    _column(table, field_columns.get(field_ref, field_ref))["pk"] = True
    return tables, relations


def build_schema(analysis: Analysis, root: Path) -> tuple[str, str]:
    """(`schema.mmd`, `schema.md`)."""
    graph = analysis.graph
    max_bytes = analysis.config.max_file_bytes
    declared, removed = _sql_tables(root, graph, max_bytes)
    prisma, prisma_relations = _prisma_tables(root, graph, max_bytes)
    gone = {name: why for name, why in removed.items() if name not in prisma}
    all_stores = [n for n in graph.nodes.values() if n.kind in _STORE_KINDS]
    stores_by_label = {n.label: n for n in all_stores if n.kind == "postgres_table"}
    table_ids = {label: node.id for label, node in stores_by_label.items()}
    # (source id, target id) -> whether every referencing column is required (NOT NULL or a key).
    relations: dict[tuple[str, str], bool] = {}

    def relate(source: str, target: str, required: bool) -> None:
        if source in table_ids and target in table_ids and source not in gone and target not in gone:
            pair = (table_ids[source], table_ids[target])
            relations[pair] = relations.get(pair, True) and required

    for name, table in declared.items():
        for referenced, local in table["references"].items():
            relate(name, referenced, all(table["columns"].get(c, {}).get("not_null") for c in local))
    for source, target, required in prisma_relations:
        relate(source, target, required)
    # Foreign keys another reader recorded (SQLAlchemy, Alembic); SQL DDL was re-read above with drops applied.
    for edge in graph.edges:
        if (edge.kind == "REFERENCES" and edge.origin != "sqlglot" and edge.source in graph.nodes
                and edge.target in graph.nodes and not is_test_path(_evidence_path(edge.evidence))):
            relate(graph.nodes[edge.source].label, graph.nodes[edge.target].label, False)
    related = {node_id for pair in relations for node_id in pair}

    def known_columns(node: Node) -> dict | None:
        return declared.get(node.label) or prisma.get(node.label)

    def rank(node: Node) -> tuple:
        # Tables with declared columns or a foreign key say the most; field-less collections the least.
        if node.kind != "postgres_table":
            return 2, node.kind, node.label
        known = known_columns(node)
        return (0 if (known and known["columns"]) or node.id in related else 1), node.kind, node.label

    stores = sorted((n for n in all_stores if n.kind != "postgres_table" or n.label not in gone), key=rank)
    drawn = stores[:MAX_ENTITIES]
    ids = {node.id: f"e{position}" for position, node in enumerate(drawn)}
    lines = ["erDiagram"]
    for node in drawn:
        known = known_columns(node)
        lines.append(f'  {ids[node.id]}["{leaf_label(node.label)}"] {{')
        if node.kind == "mongo_collection":
            lines.append("    %% fields not inferred: MongoDB documents have no declared schema here")
        elif not known or not known["columns"]:
            lines.append("    %% columns not resolved")
        else:
            for name, column in known["columns"].items():
                keys = ",".join(k for k, flag in (("PK", column["pk"]), ("FK", column["fk"])) if flag)
                spelled = f' "{leaf_label(name, 60)}"' if _token(name) != name else ""
                lines.append(f"    {_token(column['type'] or 'unknown')} {_token(name)}{' ' + keys if keys else ''}{spelled}")
        lines.append("  }")
    for (source, target), required in sorted(relations.items()):
        if source in ids and target in ids:
            # `||` exactly one parent when every referencing column is required, else `o|` zero or one.
            lines.append(f'  {ids[source]} }}o--{"||" if required else "o|"} {ids[target]} : "foreign key"')
    if len(stores) > MAX_ENTITIES:
        undrawn = defaultdict(int)
        for node in stores[MAX_ENTITIES:]:
            undrawn[node.kind] += 1
        counts = ", ".join(f"{count} {_STORE_KINDS[kind]}{'' if count == 1 else 's'}"
                           for kind, count in sorted(undrawn.items(), key=lambda item: -item[1]))
        lines.append(f"  %% {len(stores) - MAX_ENTITIES} more stores not drawn: {counts}")
    mermaid = "\n".join(lines) + f"\n%% produced by {describe_build()}\n"
    md = [f"<!-- produced by {describe_build()} -->", "# Data schema", "",
          LIMITS["schema.mmd"], "", "```mermaid", mermaid.rstrip("\n"), "```", "",
          "| Store | Kind | Columns | Declared in | Drawn |", "|---|---|---|---|---|"]
    for node in stores:
        known = known_columns(node)
        declared_in = ", ".join(sorted(known["files"])) if known else ""
        count = str(len(known["columns"])) if known and known["columns"] else "not resolved"
        md.append(f"| {markdown_cell(node.label, code=True)} | {_STORE_KINDS[node.kind]} | {count} | "
                  f"{markdown_cell(declared_in)} | {'yes' if node.id in ids else 'no'} |")
    shown_gone = sorted(name for name in gone if name in stores_by_label)
    if shown_gone:
        md += ["", "## Tables the scanned DDL removes", "",
               "Named elsewhere in the source, but a later statement drops or renames them, so they are not drawn.", "",
               "| Table | What happened |", "|---|---|"]
        md += [f"| {markdown_cell(name, code=True)} | {markdown_cell(gone[name])} |" for name in shown_gone]
    return mermaid, "\n".join(md) + "\n"


# ── architecture ─────────────────────────────────────────────────────────────────
def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def build_architecture(analysis: Analysis, groups: dict[str, FeatureGroup], max_groups: int,
                       index: _Index | None = None) -> tuple[str, str]:
    """(`architecture.mmd`, `architecture.md`)."""
    graph = analysis.graph
    index = index or _Index(graph)
    ranked = sorted(groups.values(), key=lambda g: (-(len(g.pages) + len(g.endpoints) + len(g.files)), g.name))
    shown = sorted(ranked[:max_groups], key=lambda g: g.name)
    position = {g.name: n for n, g in enumerate(shown)}
    lines = ["flowchart LR"]
    sections = {"frontend": [], "api": [], "code": []}
    for g in shown:
        n = position[g.name]
        frontend = [p for p, layer in g.files.items() if layer == "frontend"]
        served = [e for e in g.endpoints if index.status.get(e) == "served"]
        code = [p for p, layer in g.files.items() if layer != "frontend"]
        if g.pages or frontend:
            sections["frontend"].append(f'    f{n}["{flow_label(f"{g.name}: {_plural(len(g.pages), "page")}, {_plural(len(frontend), "file")}")}"]')
        if g.endpoints:
            unserved = len(g.endpoints) - len(served)
            text = f"{g.name}: {_plural(len(served), 'endpoint')}" + (f", {unserved} unserved" if unserved else "")
            sections["api"].append(f'    a{n}["{flow_label(text)}"]')
        if code:
            sections["code"].append(f'    c{n}["{flow_label(f"{g.name}: {_plural(len(code), "file")}")}"]')
    titles = {"frontend": "Pages and clients", "api": "API", "code": "Code"}
    for key, nodes in sections.items():
        if nodes:
            lines += [f'  subgraph {key}["{titles[key]}"]', *nodes, "  end"]
    # The stores most groups share first, so a cut keeps the ones that connect the most.
    users: dict[str, int] = defaultdict(int)
    for g in shown:
        for store in g.stores:
            users[store] += 1
    stores = sorted(users, key=lambda s: (-users[s], graph.nodes[s].label))
    store_ids = {s: f"s{n}" for n, s in enumerate(stores[:MAX_STORES_DRAWN])}
    if store_ids:
        lines.append('  subgraph stores["Stores"]')
        for store, sid in store_ids.items():
            node = graph.nodes[store]
            lines.append(f'    {sid}["{flow_label(f"{node.label} ({_STORE_KINDS.get(node.kind, node.kind)})")}"]')
        lines.append("  end")
    has = {line.strip().split("[", 1)[0] for line in lines}
    edges = set()
    frontend_groups: dict[str, set[str]] = defaultdict(set)
    for g in shown:
        for path, layer in g.files.items():
            if layer == "frontend":
                frontend_groups[path].add(g.name)
    for g in shown:
        n = position[g.name]
        for endpoint in g.endpoints:
            for caller in g.callers.get(endpoint, []):
                path = graph.nodes[caller].path if caller in graph.nodes else None
                for source in frontend_groups.get(path or "", ()):
                    edges.add((f"f{position[source]}", f"a{n}"))
        for endpoint in g.calls_out:
            target = next((o for o in shown if endpoint in o.endpoints), None)
            if target is not None:
                edges.add((f"f{n}", f"a{position[target.name]}"))
        edges.add((f"a{n}", f"c{n}"))
        for store in g.stores:
            if store in store_ids:
                edges.add((f"c{n}", store_ids[store]))
    # Page -> API and API -> code edges first; store edges are the first to go past the cap.
    drawable = sorted((e for e in edges if e[0] in has and e[1] in has), key=lambda e: (e[1].startswith("s"), e))
    lines += [f"  {a} --> {b}" for a, b in drawable[:MAX_EDGES]]
    if len(drawable) > MAX_EDGES:
        lines.append(f"  %% {len(drawable) - MAX_EDGES} more edges not drawn (Mermaid draws at most 500)")
    if len(groups) > len(shown):
        lines.append(f"  %% {len(groups) - len(shown)} more groups not drawn (--max-groups {max_groups})")
    if len(stores) > MAX_STORES_DRAWN:
        lines.append(f"  %% {len(stores) - MAX_STORES_DRAWN} more stores not drawn")
    mermaid = "\n".join(lines) + f"\n%% produced by {describe_build()}\n"
    md = [f"<!-- produced by {describe_build()} -->", "# Architecture", "", LIMITS["architecture.mmd"], ""]
    if len(groups) > len(shown):
        md += [f"{len(groups) - len(shown)} smaller groups are not drawn; raise `--max-groups` to include them.", ""]
    md += ["```mermaid", mermaid.rstrip("\n"), "```", ""]
    return mermaid, "\n".join(md) + "\n"


# ── feature pages ────────────────────────────────────────────────────────────────
def _group_gaps(graph: Graph, index: _Index, group: FeatureGroup) -> list[dict]:
    ids = set(group.endpoints) | set(group.pages) | {i for path in group.files for i in index.by_path.get(path, ())}
    positions = {p for node_id in ids for p in index.gaps_by_node.get(node_id, ())}
    positions |= {p for path in group.files for p in index.gaps_by_path.get(path, ())}
    gaps = []
    for position in sorted(positions):
        issue = graph.issues[position]
        gaps.append({"code": issue.code, "severity": issue.severity, "message": issue.message,
                     "evidence": issue.evidence})
    return sorted(gaps, key=lambda g: (g["severity"] != "warning", g["code"], g["evidence"]))


def _debugging_context(group: FeatureGroup, endpoints: list[dict], gaps: list[dict]) -> dict:
    """Reviewable trace points for a group; this is guidance, never injected instrumentation."""
    traces = []
    for endpoint in endpoints:
        route = f"{endpoint['method']} {endpoint['path']}"
        handlers = [f"{item['file']}:{item['line']}" for item in endpoint["handlers"]]
        chain = [*(endpoint["callers"] or ["no static caller found"]), route,
                 *(handlers or ["no handler found"]), *endpoint["stores"]]
        first_check = ("route registration and HTTP method" if not endpoint["served"] else
                       "the client and handler locations, then each store boundary")
        traces.append({"request": route, "direction": endpoint.get("direction", "feature endpoint"),
                       "served": endpoint["served"], "static_chain": chain,
                       "caller_locations": endpoint["callers"], "handler_locations": handlers,
                       "stores": endpoint["stores"], "resolution": endpoint["resolution"],
                       "first_check": first_check})
    return {
        "mode": "static guidance; no code or production instrumentation is installed",
        "impact_query": f"repolens analyze --query {group.name}",
        "traces": traces,
        "diagnostics": sorted({gap["code"] for gap in gaps}),
        "safe_runtime_fields_if_instrumented": ["trace or request id", "route template", "HTTP method",
                                                 "status code", "duration", "error class", "store operation"],
        "do_not_record": ["request or response bodies", "authorization headers", "cookies", "secrets"],
    }


def build_feature(analysis: Analysis, group: FeatureGroup, index: _Index | None = None) -> tuple[str, dict]:
    """(`features/<group>.md`, the context pack) for one group."""
    graph = analysis.graph
    index = index or _Index(graph)
    endpoints = []
    for endpoint in group.endpoints:
        node = graph.nodes[endpoint]
        handler_edges = index.handler_edges(endpoint)
        handlers = list(dict.fromkeys(e.target for e in handler_edges))
        endpoints.append({
            "method": str(node.metadata.get("method") or node.label.split(" ", 1)[0]),
            "path": openapi_path(node)[0],
            "handlers": [index.symbol(h) for h in handlers],
            "callers": index.callers(endpoint),
            "stores": [s.label for s in index.stores_from(handlers)] if handlers else [],
            "resolution": sorted({e.resolution for e in handler_edges}),
            "served": index.status.get(endpoint) == "served",
        })
    pages = [{"route": str(graph.nodes[p].metadata.get("path") or route_path(graph.nodes[p])),
              "file": graph.nodes[p].path} for p in group.pages]
    stores = [{"name": graph.nodes[s].label, "kind": graph.nodes[s].kind} for s in group.stores]
    gaps = _group_gaps(graph, index, group)
    calls_out = [graph.nodes[e].label for e in group.calls_out if e in graph.nodes]
    debug_endpoints = [{**endpoint, "direction": "feature endpoint"} for endpoint in endpoints]
    for endpoint_id in group.calls_out:
        if endpoint_id in group.endpoints or endpoint_id not in graph.nodes:
            continue
        node = graph.nodes[endpoint_id]
        handler_edges = index.handler_edges(endpoint_id)
        handlers = list(dict.fromkeys(e.target for e in handler_edges))
        callers = [where for where in index.callers(endpoint_id) if _evidence_path(where) in group.files]
        debug_endpoints.append({
            "method": str(node.metadata.get("method") or node.label.split(" ", 1)[0]),
            "path": openapi_path(node)[0], "handlers": [index.symbol(h) for h in handlers],
            "callers": callers, "stores": [s.label for s in index.stores_from(handlers)] if handlers else [],
            "resolution": sorted({e.resolution for e in handler_edges}),
            "served": index.status.get(endpoint_id) == "served", "direction": "outbound request",
        })
    debugging = _debugging_context(group, debug_endpoints, gaps)
    context = {
        "group": group.name, "build": describe_build(), "complete": analysis.complete,
        "note": "A feature group is a route area named by its first path segment, not a business feature.",
        "pages": pages, "endpoints": endpoints,
        "files": [{"path": path, "layer": layer} for path, layer in sorted(group.files.items())],
        "stores": stores, "calls_to_other_groups": calls_out, "gaps": gaps,
        "debugging": debugging, "unknowns": UNKNOWNS,
    }

    counter = itertools.count(1)

    def leaf(depth: int, text: object) -> str:
        return f'{"  " * depth}m{next(counter)}["{leaf_label(text)}"]'

    def section(title: str, items: list[str]) -> list[str]:
        if not items:
            return []
        more = [leaf(3, f"… {len(items) - MAX_LEAVES} more in the tables below")] if len(items) > MAX_LEAVES else []
        return [leaf(2, title), *(leaf(3, item) for item in items[:MAX_LEAVES]), *more]

    mind = ["mindmap", f'  root(("{leaf_label(group.name, 60)}"))']
    mind += section("Pages", [page["route"] for page in pages])
    mind += section("API", [f"{e['method']} {e['path']}" + ("" if e["served"] else " (no handler)") for e in endpoints])
    for layer in ("router", "service", "model", "frontend"):
        mind += section(f"Code: {layer}", sorted(p for p, known in group.files.items() if known == layer))
    mind += section("Stores", [f"{s['name']} ({_STORE_KINDS.get(s['kind'], s['kind'])})" for s in stores])
    mind += section("Gaps", [f"{g['code']} {g['evidence']}" for g in gaps])

    md = [f"<!-- produced by {describe_build()} -->", f"# Feature group: {markdown_inline(group.name)}", "",
          context["note"] + " Rename it and write its purpose; the facts below are what the source shows.", ""]
    if not analysis.complete:
        md += ["> The analysis was incomplete, so this page may miss evidence. See index.md.", ""]
    md += ["```mermaid", *mind, "```", "", "## Endpoints", ""]
    if endpoints:
        md += ["| Method | Path | Handler | Callers | Stores | Resolution |", "|---|---|---|---|---|---|"]
        for e in endpoints:
            handler = ", ".join(f"{h['file']}:{h['line']}" for h in e["handlers"]) or "no handler"
            md.append(f"| {markdown_cell(e['method'])} | {markdown_cell(e['path'], code=True)} | {markdown_cell(handler)} | "
                      f"{markdown_cell(', '.join(e['callers']) or 'none found')} | {markdown_cell(', '.join(e['stores']))} | "
                      f"{markdown_cell(', '.join(e['resolution']))} |")
    else:
        md.append("None found.")
    md += ["", "## Pages", ""]
    if pages:
        md += ["| Route | File |", "|---|---|"]
        md += [f"| {markdown_cell(p['route'], code=True)} | {markdown_cell(p['file'])} |" for p in pages]
    else:
        md.append("None found.")
    md += ["", "## Files by layer", ""]
    if group.files:
        md += ["| File | Layer |", "|---|---|"]
        md += [f"| {markdown_cell(path)} | {layer} |" for path, layer in sorted(group.files.items())]
    else:
        md.append("None found.")
    if calls_out:
        md += ["", "## Calls to other groups", "", *[f"- `{markdown_inline(label)}`" for label in calls_out]]
    md += ["", "## Gaps", ""]
    if gaps:
        md += ["| Code | Severity | Evidence | Message |", "|---|---|---|---|"]
        md += [f"| {g['code']} | {g['severity']} | {markdown_cell(g['evidence'])} | {markdown_cell(g['message'])} |" for g in gaps]
    else:
        md.append("No diagnostics in this group's files or endpoints.")
    md += ["", "## Debugging plan", "",
           "These are static trace candidates. Repository Lens adds no logger, span, dependency or build hook to the application.", "",
           f"Start with `{debugging['impact_query']}`. If you add runtime telemetry, carry one trace or request id across "
           "the client, handler and store boundaries; record route templates, status, duration and error class, and "
           "exclude bodies, authorization headers, cookies and secrets.", ""]
    if debugging["traces"]:
        md += ["| Request | Direction | Static chain | First check | Resolution |", "|---|---|---|---|---|"]
        for trace in debugging["traces"]:
            md.append(f"| {markdown_cell(trace['request'], code=True)} | {markdown_cell(trace['direction'])} | "
                      f"{markdown_cell(' → '.join(trace['static_chain']))} | "
                      f"{markdown_cell(trace['first_check'])} | "
                      f"{markdown_cell(', '.join(trace['resolution']) or 'unresolved')} |")
    else:
        md.append("No request path was found for this group.")
    md += ["", "## Not known from the source", "", *[f"- {item}" for item in UNKNOWNS], ""]
    return "\n".join(md), context


def build_debugging(analysis: Analysis, contexts: list[tuple[str, str, dict]]) -> str:
    """A repository-wide debugging entry point linked to each feature's evidence."""
    md = [f"<!-- produced by {describe_build()} -->", "# Debugging guide", "",
          "Repository Lens has no production runtime path: it reads source and writes this documentation. "
          "It does not add a logger, tracing SDK, dependency, middleware or build hook to the application.", ""]
    if not analysis.complete:
        md += ["> The analysis was incomplete. Treat missing links as unknown and see index.md for the reasons.", ""]
    md += ["## Start with static evidence", "",
           "1. Run `repolens analyze --query <feature>` and open the bounded relationship graph.",
           "2. Open that feature's page below and follow a request from its caller location to the handler and stores.",
           "3. Look up an important function by name or `@functionlens:` id with `repolens lens --lookup <value>`.",
           "4. Reproduce the failure and run the application's type checker, linter and tests alongside the static findings.", "",
           "| Feature | Requests | Diagnostics | Evidence page | Query |", "|---|---:|---|---|---|"]
    for name, slug, context in contexts:
        debugging = context["debugging"]
        diagnostics = ", ".join(debugging["diagnostics"]) or "none found"
        md.append(f"| {markdown_cell(name)} | {len(debugging['traces'])} | {markdown_cell(diagnostics)} | "
                  f"[open](features/{slug}.md#debugging-plan) | `{markdown_inline(debugging['impact_query'])}` |")
    md += ["", "## If runtime telemetry is still needed", "",
           "Add it deliberately at the client, route-handler and store boundaries listed on each feature page. "
           "Carry one trace or request id through the chain and record the route template, HTTP method, status, "
           "duration, error class and store operation.", "",
           "Keep production cost bounded: enable detailed diagnostics through configuration, sample successful "
           "requests, retain errors, batch exports and measure overhead under representative load. Redact at the "
           "instrumentation boundary; do not record bodies, authorization headers, cookies or secrets.", "",
           "Repository Lens only proposes the locations. Runtime instrumentation needs framework-specific review "
           "because middleware, authorization, deployment topology and live traffic are outside static evidence.", ""]
    return "\n".join(md)


# ── index and writing ────────────────────────────────────────────────────────────
def _ours(path: Path) -> bool:
    """Whether `path` is an output of this command: a regular file carrying the build stamp where the
    command puts it (the first line of Markdown, the last line of Mermaid, the top-level build field
    of JSON). A file that only quotes the stamp is not."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if path.name.endswith(".json"):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        build = data.get("x-repolens-build", data.get("build")) if isinstance(data, dict) else None
        return isinstance(build, str) and build.startswith("repolens ")
    lines = text.splitlines()
    if path.suffix == ".md":
        return bool(lines) and lines[0].startswith("<!-- produced by repolens ")
    if path.suffix == ".mmd":
        return bool(lines) and lines[-1].startswith("%% produced by repolens ")
    return False


def write_docs(analysis: Analysis, root: Path, out: Path, *, max_groups: int = DEFAULT_MAX_GROUPS) -> list[str]:
    """Write every output under `out`; returns one summary line per output group."""
    groups = feature_groups(analysis.graph)
    used: set[str] = set()
    links = [(group, _slug(group.name, used)) for group in groups.values()]
    features = out / "features"
    targets = [out / name for name in ("openapi.json", "schema.mmd", "schema.md", "architecture.mmd",
                                       "architecture.md", "debugging.md", "index.md")]
    targets += [features / f"{slug}{suffix}" for _group, slug in links for suffix in (".md", ".context.json")]
    # A symlink (a checkout can commit one under the output directory) would carry a write outside it.
    linked = [path for path in (out, features, *targets) if path.is_symlink()]
    if linked:
        raise ValueError(f"refusing to write through {len(linked)} symlink(s): {', '.join(map(str, linked[:3]))}")
    # `--out docs` must not replace or delete pages a person wrote: refuse before writing anything.
    foreign = [path for path in targets if path.exists() and not _ours(path)]
    if foreign:
        shown = ", ".join(str(path) for path in foreign[:3])
        raise ValueError(f"refusing to overwrite {len(foreign)} file(s) not written by repolens docs generate: {shown}")
    out.mkdir(parents=True, exist_ok=True)
    index = _Index(analysis.graph)
    openapi = build_openapi(analysis, root, groups, index)
    (out / "openapi.json").write_text(json.dumps(openapi, indent=2) + "\n", encoding="utf-8")
    schema_mmd, schema_md = build_schema(analysis, root)
    (out / "schema.mmd").write_text(schema_mmd, encoding="utf-8")
    (out / "schema.md").write_text(schema_md, encoding="utf-8")
    arch_mmd, arch_md = build_architecture(analysis, groups, max_groups, index)
    (out / "architecture.mmd").write_text(arch_mmd, encoding="utf-8")
    (out / "architecture.md").write_text(arch_md, encoding="utf-8")
    features.mkdir(exist_ok=True)
    # Pages of groups that no longer exist go, but only the ones this command wrote.
    for stale in [*features.glob("*.md"), *features.glob("*.context.json")]:
        if stale not in targets and _ours(stale):
            stale.unlink()
    contexts = []
    for group, slug in links:
        md, context = build_feature(analysis, group, index)
        (features / f"{slug}.md").write_text(md, encoding="utf-8")
        (features / f"{slug}.context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
        contexts.append((group.name, slug, context))
    (out / "debugging.md").write_text(build_debugging(analysis, contexts), encoding="utf-8")
    operations = sum(len(_operations(item)) for item in openapi["paths"].values())
    stores = sum(1 for n in analysis.graph.nodes.values() if n.kind in _STORE_KINDS)
    page = [f"<!-- produced by {describe_build()} -->", "# Generated documentation", "",
             "Drafted from static evidence by `repolens docs generate`. Review before publishing; "
             "regenerate after the code changes.", "",
             f"Analysis complete: **{'yes' if analysis.complete else 'no'}**"]
    page += [f"- {markdown_inline(reason)}" for reason in analysis.incomplete_reasons()]
    page += ["", "| Output | What it holds | Limit |", "|---|---|---|",
              f"| [openapi.json](openapi.json) | {_plural(operations, 'operation')}, {_plural(len(openapi['x-repolens-unserved-calls']), 'unserved call')} | {LIMITS['openapi.json']} |",
              f"| [schema.md](schema.md) ([.mmd](schema.mmd)) | {stores} stores | {LIMITS['schema.mmd']} |",
              f"| [architecture.md](architecture.md) ([.mmd](architecture.mmd)) | {len(groups)} feature groups | {LIMITS['architecture.mmd']} |",
              f"| [debugging.md](debugging.md) | static trace plans for {len(groups)} feature groups | {LIMITS['debugging.md']} |",
              f"| features/ | one page and one context pack per group | {LIMITS['features/']} |", "", "## Feature groups", ""]
    page += [f"- [{markdown_inline(group.name)}](features/{slug}.md) ([context](features/{slug}.context.json)): "
              f"{_plural(len(group.pages), 'page')}, {_plural(len(group.endpoints), 'endpoint')}, "
              f"{_plural(len(group.stores), 'store')}" for group, slug in links]
    (out / "index.md").write_text("\n".join(page) + "\n", encoding="utf-8")
    return [f"openapi.json: {_plural(operations, 'operation')}, {_plural(len(openapi['x-repolens-unserved-calls']), 'unserved call')}",
            f"schema.mmd, schema.md: {stores} stores",
            f"architecture.mmd, architecture.md: {len(groups)} feature groups",
            f"debugging.md: {len(groups)} static trace plans",
            f"features/: {len(links)} pages and context packs",
            "index.md"]


def main(argv: list[str] | None = None, *, config=None, prog: str | None = None) -> int:
    """`repolens docs generate`; see the module docstring."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help=f"output directory (default {DEFAULT_OUT})")
    parser.add_argument("--max-groups", type=int, default=DEFAULT_MAX_GROUPS,
                        help=f"feature groups drawn in the architecture map (default {DEFAULT_MAX_GROUPS})")
    args = parser.parse_args(argv)
    if args.max_groups < 1:
        parser.error("--max-groups must be at least 1")
    tool_build()  # stamp the code this process loaded
    root = (config.root if config else Path.cwd()).resolve()
    # A relative --out is under the root, as `featuretrace propose` reads it.
    out = (args.out if args.out and args.out.is_absolute() else root / (args.out or DEFAULT_OUT)).resolve()
    if out == root:
        parser.error("--out must be a dedicated output directory")
    if not args.out and not out.is_relative_to(root):
        parser.error(f"{DEFAULT_OUT} resolves outside the repository (a symlink); pass --out")
    settings = Config.load(root)
    if out.is_relative_to(root):
        settings.exclude_paths.add(out.relative_to(root).as_posix())
    name = prog or "repolens docs generate"
    try:
        result = analyze(root, config=settings)
        written = write_docs(result, root, out, max_groups=args.max_groups)
    except (OSError, ValueError) as exc:
        print(f"{name}: {exc}")
        return 2
    print(f"Wrote {out}")
    for line in written:
        print(f"  {line}")
    for reason in result.incomplete_reasons():
        print(f"  incomplete: {reason}")
    print(f"  produced by {describe_build()}")
    return 0 if result.complete else 2
