"""Mutable per-scan state and graph helpers shared by the language extractors."""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re

from .config import Config
from .model import Edge, Graph, Node, stable_id
from .resolution import ImportIndex


@dataclass(slots=True)
class PendingCall:
    """A call or JSX render seen during the per-file walk, resolved to definitions in a later pass."""
    source: str
    name: str
    evidence: str
    language: str
    #: CALLS for a call or `new`; RENDERS for a JSX element naming a component; PASSES for a
    #: function handed to a JSX attribute by reference, linked (as CALLS) only through an import.
    relationship: str = "CALLS"


@dataclass(slots=True)
class ScanState:
    """Mutable state for one `scan_repository()` run.

    Facts that depend on another file (imports, pending calls, model bindings, re-exports,
    HTTP clients) are recorded here during the per-file walk and resolved in the passes
    that run after it."""
    graph: Graph
    root: Path
    config: Config
    definitions: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    pending_calls: list[PendingCall] = field(default_factory=list)
    module_files: dict[str, str] = field(default_factory=dict)
    files: list[Path] = field(default_factory=list)
    imports: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    python_trees: dict[str, ast.Module] = field(default_factory=dict)
    import_index: ImportIndex | None = None
    admitted_paths: frozenset[str] = frozenset()
    orm_tables: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    orm_references: list[tuple[str, str, str]] = field(default_factory=list)
    # JS/TS facts that can only be resolved once every admitted file has been read:
    # `export * from`, HTTP clients created in one module and used in another, and
    # ORM/ODM model bindings (Drizzle tables, Mongoose models, TypeORM entities).
    js_reexports: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    js_requests: list[tuple[str, str, object]] = field(default_factory=list)
    http_clients: dict[tuple[str, str], str] = field(default_factory=dict)
    # (file, receiver) that is a client of unknown declaration: bound from a hook or injection
    # result, or never bound in the file at all (a global `api`).
    js_untraced_clients: set[tuple[str, str]] = field(default_factory=set)
    # (source node, store kind, name, evidence, detail) matched by a pattern in test code:
    # linked after the walk, and only to a store other evidence created.
    test_store_references: list[tuple[str, str, str, str, str | None]] = field(default_factory=list)
    js_models: dict[tuple[str, str], object] = field(default_factory=dict)
    js_model_refs: list[tuple[str, str, str, str, int, str]] = field(default_factory=list)
    js_member_stores: list[tuple[str, str, str, str, str, int]] = field(default_factory=list)
    prisma_schemas: list[tuple[str, str]] = field(default_factory=list)
    # module -> {public export name: local binding}, for barrels and aliased exports.
    js_exports: dict[str, dict[str, str]] = field(default_factory=dict)
    # Python model facts resolved after every module is visited (python_scan.py).
    # (path, class or Table variable) -> (store kind, store name, resolution, detail).
    # Keyed by path: `models.User` and `schemas.User` are different classes.
    py_models: dict[tuple[str, str], tuple[str, str, str, str]] = field(default_factory=dict)
    # (source, path, name, evidence, operation, function-local import binding or None,
    # name is assigned at module level).
    py_model_refs: list[tuple[str, str, str, str, str, tuple[str, str] | None, bool]] = field(default_factory=list)
    # Every directory and module stem of an admitted .py file: an unresolved absolute
    # import whose top-level name is among them is a local gap, not a package.
    python_local_names: frozenset[str] | None = None
    # (file, receiver, `.collection()` call, import depth) -> is a MongoDB handle. Each
    # answer reads the whole file, so call sites sharing a receiver must not repeat it.
    mongo_handles: dict[tuple[str, str, bool, int], bool] = field(default_factory=dict)
    # Routes that already began with `backend_api_prefix` once their mounts resolved. The
    # setting predates mount resolution; adding it again produced `/api/api/...`.
    api_prefix_already_resolved: int = 0


def with_api_prefix(state: ScanState, route: str) -> str:
    """`backend_api_prefix` + `route`, unless the resolved route already starts with it.

    Routers whose mounts did not resolve still get the prefix; a route resolved through
    `APIRouter(prefix="/api")` is not given a second one."""
    configured = state.config.backend_api_prefix
    prefix = "/" + configured.strip("/") if configured.strip("/") else ""
    if not prefix:
        return route
    bare = "/" + route.lstrip("/")
    if bare == prefix or bare.startswith(prefix + "/"):
        state.api_prefix_already_resolved += 1
        return route
    return prefix + bare if route else prefix


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_id(path: str) -> str:
    return stable_id("file", path)


def _symbol_id(path: str, qualified: str) -> str:
    return stable_id("symbol", f"{path}::{qualified}")


def _endpoint_id(method: str, route: str) -> str:
    return stable_id("endpoint", f"{method.upper()} {normalise_route(route)}")


def normalise_route(route: str) -> str:
    """Canonical route shape for endpoint ids and matching: `${...}` and `{param}` become
    `{dynamic}`, repeated slashes collapse and a trailing slash is dropped."""
    route = route.strip()
    route = re.sub(r"\$\{[^}]+\}", "{dynamic}", route)
    # Parameter spelling differs between client templates and backend routes.
    route = re.sub(r"\{[^{}]+\}", "{dynamic}", route)
    route = re.sub(r"//+", "/", route)
    if route != "/":
        route = route.rstrip("/")
    return route or "/"


#: A whole-segment route parameter: Next.js `[id]`, `[...slug]`, `[[...slug]]`; Express/Hono `:id`, `:id?`,
#: `:id(\\d+)`, `:id{[0-9]+}`.
_BRACKET_PARAMETER = re.compile(r"^\[\[?(?:\.\.\.)?([^\[\]]+)\]\]?$")
_COLON_PARAMETER = re.compile(r"^:([A-Za-z_$][\w$]*)(?:\([^/]*\)|\{[^/]*\})?\??$")
#: A parameter inside a segment: FastAPI/OpenAPI `{file_path:path}` (converter dropped), or Express
#: `:from-:to` and `:name.:ext`. A colon after a letter (`/v1:batch`, a custom method) stays text.
_SEGMENT_PARAMETER = re.compile(r"\{([A-Za-z_][\w-]*)(?::[^{}]*)?\}|(?<![^-.]):([A-Za-z_]\w*)")


def route_path_and_parameters(route: str) -> tuple[str, list[str]]:
    """The declared route in OpenAPI spelling with the source's parameter names, and those names in order.

    `/api/orders/[orderId]` -> (`/api/orders/{orderId}`, ["orderId"]); `/files/{file_path:path}` ->
    (`/files/{file_path}`, ["file_path"]). Unlike `normalise_route`, names are kept: this is for
    documentation, never for matching."""
    names: list[str] = []
    parts: list[str] = []

    def inline(match: re.Match[str]) -> str:
        names.append(match.group(1) or match.group(2))
        return "{" + names[-1] + "}"

    for segment in route.strip().split("/"):
        if not segment:
            continue
        whole = _BRACKET_PARAMETER.match(segment) or _COLON_PARAMETER.match(segment)
        if whole:
            names.append(whole.group(1).strip())
            parts.append("{" + names[-1] + "}")
        else:
            parts.append(_SEGMENT_PARAMETER.sub(inline, segment))
    return "/" + "/".join(parts), names


def route_metadata(graph: Graph, node_id: str, declared: str) -> dict:
    """`path` and `parameters` for an endpoint or page node; empty when the node already has a path.

    Two declarations that differ only in parameter spelling share one node id, and the first
    declaration read keeps its names."""
    existing = graph.nodes.get(node_id)
    if existing is not None and "path" in existing.metadata:
        return {}
    path, parameters = route_path_and_parameters(declared)
    return {"path": path, "parameters": parameters}


def _add_store_edge(
    graph: Graph, source: str, kind: str, name: str, resolution: str, evidence: str,
    *, detail: str | None = None, origin: str = "syntax", metadata: dict | None = None,
) -> None:
    """One TOUCHES_STORE edge. The relationship name stays stable for graph consumers;
    what the code does to the store (reads/writes/declares) belongs in `detail`."""
    store_id = stable_id(kind, name)
    graph.add_node(Node(store_id, kind, name, metadata={"store": name, **(metadata or {})}))
    graph.add_edge(Edge(source, store_id, "TOUCHES_STORE", resolution, evidence, origin=origin, detail=detail))


#: Document-collection driver methods (PyMongo/Motor spelling and the Node driver's),
#: by what they do. A collection is only claimed when one of these is called on it:
#: `db.session`, `db.commit()` and `db.query(...)` are a SQLAlchemy session, not data.
MONGO_READ_METHODS = frozenset({
    "find", "find_one", "findOne", "aggregate", "count_documents", "countDocuments",
    "estimated_document_count", "estimatedDocumentCount", "distinct", "watch",
    "find_raw_batches", "list_indexes", "listIndexes", "index_information",
})
MONGO_WRITE_METHODS = frozenset({
    "insert_one", "insertOne", "insert_many", "insertMany", "update_one", "updateOne",
    "update_many", "updateMany", "replace_one", "replaceOne", "delete_one", "deleteOne",
    "delete_many", "deleteMany", "bulk_write", "bulkWrite", "find_one_and_update",
    "findOneAndUpdate", "find_one_and_delete", "findOneAndDelete", "find_one_and_replace",
    "findOneAndReplace", "create_index", "createIndex", "create_indexes", "createIndexes",
    "drop", "drop_index", "dropIndex", "rename",
})
#: Attribute names on a database/session handle that are never a collection.
MONGO_NOT_COLLECTIONS = frozenset({
    "session", "query", "add", "add_all", "commit", "rollback", "refresh", "flush", "execute",
    "close", "get_collection", "client", "command", "begin", "connect", "transaction",
    "select", "insert", "update", "delete", "merge", "expunge", "scalar", "scalars", "get",
    "collection", "list_collection_names", "listCollections", "admin", "db", "run_command",
    "runCommand", "exec", "bind", "engine", "metadata", "Model", "func",
})


def mongo_operation(method: str) -> str | None:
    """"reads"/"writes" for a collection driver method, None for anything else."""
    if method in MONGO_READ_METHODS:
        return "reads"
    if method in MONGO_WRITE_METHODS:
        return "writes"
    return None
