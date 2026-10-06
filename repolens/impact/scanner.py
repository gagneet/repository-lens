"""Build one repository's evidence graph from static reads; target code is never imported or run."""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
import os
import posixpath
from importlib import metadata as package_metadata
from itertools import islice
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Callable, Iterable

from .config import Config
from .model import Edge, Graph, Issue, Node, stable_id
from .source import read_source
from .plugins import Extractor, SourceFile, merge_extraction
from .resolution import ImportIndex
from .state import (MONGO_NOT_COLLECTIONS, MONGO_READ_METHODS, MONGO_WRITE_METHODS,  # noqa: F401
                    PendingCall, ScanState, _add_store_edge, _endpoint_id, _file_id, _rel,
                    _symbol_id, mongo_operation, normalise_route, route_metadata, with_api_prefix)
from .python_scan import PythonVisitor, _resolve_orm_references, _scan_python  # noqa: F401
from .render import mark_unverified_stores, unverified_stores
from ..core import javascript
from ..core.files import is_test_path

SCANNER_REVISION = 11


FEATURE_RE = re.compile(r"@featuretrace:([A-Za-z0-9_.-]+)")
IMPORT_RE = re.compile(
    r"(?:import\s+(?:type\s+)?(?:[^'\"]+?\s+from\s+)?|require\()['\"]([^'\"]+)['\"]"
)
JS_FUNCTION_RE = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
JS_CALL_RE = re.compile(r"(?<![.$\w])([A-Za-z_$][\w$]*)\s*\(")
API_CALL_RE = re.compile(
    r"(?:\bapi\b|\baxios\b)\.(get|post|put|patch|delete)\s*\(\s*([`'\"])(.+?)\2|"
    r"\bfetch\s*\(\s*([`'\"])(.+?)\4",
    re.IGNORECASE,
)
RELATED_FIELD_RE = re.compile(r"^\s*(?:#|//|\*)?\s*Related:\s*(.+)$")
RELATED_CONT_RE = re.compile(r"^\s*(?:#|//|\*)\s{2,}(.+)$")

JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "new", "super", "import", "require", "describe", "it", "test", "expect",
}


def _language_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".ts", ".tsx", ".mts", ".cts"}:
        return "typescript"
    if suffix in javascript.EXTENSIONS:
        return "javascript"
    if suffix == ".sql":
        return "sql"
    return suffix.removeprefix(".") or "unknown"


@lru_cache(maxsize=1)
def _sql_parser_available() -> bool:
    try:
        import sqlglot  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - optional parser probe
        return False


def _artifact_rel(state: ScanState, value: object) -> str | None:
    """Accept only repository-relative paths from generated relationship artifacts."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.replace("\\", "/")
    pure = PurePosixPath(text)
    if (pure.is_absolute() or ".." in pure.parts or ":" in text
            or any(ord(char) < 32 for char in text)):
        return None
    return pure.as_posix().removeprefix("./")


def _gitignored(root: Path) -> tuple[frozenset[str], str]:
    """Untracked paths git ignores under `root`, and why that list is unavailable; see
    `core.git.untracked_ignored`. A gitignored backup is not source, and reading it made
    scans incomplete. Repository config that could run a command is overridden."""
    from ..core.git import untracked_ignored
    return untracked_ignored(root)


def iter_source_files(root: Path, config: Config, ignored: frozenset[str] | None = None) -> Iterable[Path]:
    """Yield regular files with a configured extension under `root`, in sorted walk order.

    Symlinks, excluded directories and paths, and (with `respect_gitignore`) untracked
    gitignored paths are pruned before they are descended into."""
    if ignored is None:
        ignored = _gitignored(root)[0] if config.respect_gitignore else frozenset()

    def excluded(path: Path) -> bool:
        rel = path.relative_to(root).as_posix()
        # An ignored directory is pruned before its files are reached, so membership suffices.
        return rel in ignored or any(rel == item or rel.startswith(item + "/") for item in config.exclude_paths)

    # Prune before descending: rglob followed by filtering still walks node_modules.
    for base, directories, files in os.walk(root, followlinks=False):
        parent = Path(base)
        directories[:] = sorted(name for name in directories
                                if name not in config.exclude_dirs
                                and not (parent / name).is_symlink()
                                and not excluded(parent / name))
        for name in sorted(files):
            path = parent / name
            if (path.suffix.lower() in config.extensions and not path.is_symlink()
                    and path.is_file() and not excluded(path)):
                yield path


def config_fingerprint(config: Config) -> str:
    """The settings and scanner version an index was built with. A cached index is
    reusable only when these match too: adding a role to `[impact] roles` changes the
    graph without changing a single source file."""
    from .. import __version__

    parser_versions = {}
    for name in ("tree-sitter", "tree-sitter-javascript", "tree-sitter-typescript", "sqlglot"):
        try:
            parser_versions[name] = package_metadata.version(name)
        except package_metadata.PackageNotFoundError:
            parser_versions[name] = "unavailable"
    payload = json.dumps({"version": __version__, "scanner_revision": SCANNER_REVISION,
                          "parsers": parser_versions, "config": asdict(config)}, sort_keys=True,
                         default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def repository_content_sha(root: Path, config: Config) -> str:
    """Return the same deterministic source fingerprint stored in an index."""
    root = root.resolve()
    config.validate()
    if not root.is_dir():
        raise ValueError("repository root must be an existing directory")
    digest = hashlib.sha256()
    files = list(islice(iter_source_files(root, config), config.max_files + 1))
    digest.update(b"truncated" if len(files) > config.max_files else b"complete")
    for path in files[:config.max_files]:
        source = read_source(root, path, config.max_file_bytes)
        digest.update(source.fingerprint(_rel(root, path)))
    return digest.hexdigest()


#: Bundler inputs a JS module may import that are not code: never a missing local module.
_ASSET_SUFFIXES = frozenset({
    ".css", ".scss", ".sass", ".less", ".styl", ".pcss", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".avif", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3", ".wav",
    ".pdf", ".txt", ".md", ".mdx", ".graphql", ".gql", ".wasm", ".html", ".yaml", ".yml", ".json", ".glsl",
})
#: A too-large file of another type is data (a JSON backup), not a hole in the code analysis,
#: unless the scanner interprets it (`_interpreted_data_file`).
_DATA_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".md", ".mdx", ".txt", ".csv", ".tsv", ".html", ".xml", ".lock", ".log",
                            ".map", ".svg"})
#: Receivers whose members are the platform, never a function in this repository.
_JS_GLOBALS = frozenset({
    "JSON", "console", "Math", "Object", "Array", "Promise", "window", "document", "process", "Number",
    "String", "Date", "Reflect", "globalThis", "Intl", "Symbol", "Map", "Set", "WeakMap", "WeakSet", "URL",
    "URLSearchParams", "Buffer", "localStorage", "sessionStorage", "navigator", "location", "history",
    "crypto", "performance", "Response", "Request", "Headers", "Error", "RegExp", "BigInt", "Proxy",
    "module", "exports", "require", "self", "customElements", "Atomics", "ArrayBuffer", "DataView",
})
_HTTP_HANDLER_NAMES = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _interpreted_data_file(name: str) -> bool:
    """`package.json` (Prisma schema selection, project roots, liveness) and `tsconfig`/
    `jsconfig` path aliases change results, so skipping one is a hole, not skipped data."""
    return name == "package.json" or bool(re.fullmatch(r"(?:ts|js)config(?:\.[\w.-]+)?\.json", name))
#: Files that mark a directory as a JS project root, so its `app/`/`pages/` is a router.
_NEXT_PROJECT_MARKERS = ("package.json", "next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")
_URL_SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)


def _store_detail(detail: str, operation: str) -> str:
    return f"{detail} ({operation})" if operation else detail


def _report_sql_interpolation(graph, source: str, evidence: str, holes: list[tuple[str, str, bool]]) -> None:
    """SQL text built by splicing values in. A value traced to a CLI argument or request
    input is an injection; any other is named, so a reviewer knows what to check."""
    untrusted = [(text, origin) for text, origin, _ in holes if origin]
    if untrusted:
        origins = " and ".join(sorted({origin for _, origin in untrusted}))
        graph.issues.append(Issue(
            "SQL_INJECTION_RISK", "warning",
            f"SQL text splices in {', '.join(f'`{text}`' for text, _ in untrusted)}, which comes from {origins}.",
            [source], evidence,
            "Pass the value as a bound parameter ($1 with a values array, or a tagged template) instead of building the SQL string.",
        ))
        return
    shown = ", ".join(f"`{text}`" + (" inside a quoted literal" if quoted else "") for text, _, quoted in holes[:4])
    more = f" and {len(holes) - 4} more" if len(holes) > 4 else ""
    graph.issues.append(Issue("DYNAMIC_SQL", "info", f"SQL text splices in {shown}{more}.", [source], evidence,
                              "Bind values as parameters; choose any varying identifier from a fixed allow-list."))


def _into_excluded_directory(state: ScanState, rel: str, module: str) -> bool:
    """`./.next/types/routes.d.ts` from next-env.d.ts: output the scan skips by design."""
    if not module.startswith("."):
        return False
    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), module))
    parts = PurePosixPath(target).parts
    if not parts or parts[0] == "..":
        return False
    return (any(part in state.config.exclude_dirs for part in parts[:-1])
            or any(target == prefix or target.startswith(prefix + "/") for prefix in state.config.exclude_paths))


def _scan_javascript_syntax(state: ScanState, path: Path, text: str, file_node: str) -> None:
    rel = _rel(state.root, path)
    facts = javascript.parse_source(text, path.suffix.lower())
    language = "typescript" if path.suffix.lower() in {".ts", ".tsx", ".mts", ".cts"} else "javascript"
    graph = state.graph

    def owner_id(owner: str) -> str:
        node_id = _symbol_id(rel, owner) if owner else file_node
        return node_id if node_id in graph.nodes else file_node

    for line in facts.errors[:20]:
        graph.issues.append(Issue("JAVASCRIPT_PARSE_ERROR", "warning", "Parser recovered from invalid or unsupported JavaScript/TypeScript syntax.",
                                  [file_node], f"{rel}:{line}", "Correct the syntax or report a grammar limitation."))
    for symbol in facts.symbols:
        node_id = _symbol_id(rel, symbol.qualified)
        metadata = {"qualified_name": symbol.qualified, "symbol_kind": symbol.kind,
                    "exported": symbol.exported, "default_export": symbol.default_export,
                    "end_line": symbol.end_line}
        if symbol.export_names:
            metadata["export_names"] = list(symbol.export_names)
        if symbol.value_holders:
            # Same-file uses by value (`[{ loader: load }]`), read by _JavaScriptLiveness.
            metadata["value_holders"] = list(symbol.value_holders)
        if symbol.role:
            metadata["lexical_role"] = symbol.role
        graph.add_node(Node(node_id, "symbol", symbol.qualified, path=rel, line=symbol.line, language=language,
                            metadata=metadata))
        graph.add_edge(Edge(file_node, node_id, "CONTAINS", "exact", f"{rel}:{symbol.line}", origin="tree-sitter"))
        state.definitions[symbol.name].append(node_id)
    # A function defined inside another runs as part of it: `OrdersPage` owns its `load` helper
    # and its inline `onClick` callback, whose calls would otherwise reach a page only through
    # the file. Object-literal members (`api.list`) have no symbol parent and get no edge.
    for symbol in facts.symbols:
        parent_name = symbol.qualified.rpartition(".")[0]
        parent = _symbol_id(rel, parent_name) if parent_name else ""
        if parent not in graph.nodes or graph.nodes[parent].kind != "symbol":
            continue
        owner = parent_name.rpartition(".")[2]
        if not symbol.name.startswith("anonymous@"):
            detail = f"`{symbol.name}` is defined inside `{owner}`"
        elif symbol.role:
            detail = f"inline `{symbol.role}` callback defined inside `{owner}`"
        else:
            detail = f"anonymous function defined inside `{owner}`"
        graph.add_edge(Edge(parent, _symbol_id(rel, symbol.qualified), "DEFINES", "exact", f"{rel}:{symbol.line}",
                            origin="tree-sitter", detail=detail))
    state.js_exports[rel] = dict(facts.exports)
    edges_before = len(graph.edges)
    _add_next_routes(state, rel, path, facts, file_node, language)
    _add_route_registrations(state, rel, facts, file_node, language, routed=len(graph.edges) > edges_before)
    for module, local, exported, line in facts.imports:
        target, expected_local = state.import_index.javascript(rel, module)
        if target:
            target_id = _file_id(target)
            graph.add_node(Node(target_id, "file", target, path=target))
            graph.add_edge(Edge(file_node, target_id, "IMPORTS", "exact", f"{rel}:{line}", origin="tree-sitter"))
            # `import("./Settings")` in `React.lazy` or `next/dynamic`, and `import "./setup"`, bind
            # no name but load the whole module; the NUL key can never be a receiver or callee.
            state.imports.setdefault(rel, {})[local or f"\0{module}"] = (target, exported)
        elif expected_local:
            if PurePosixPath(module.split("?", 1)[0]).suffix.lower() in _ASSET_SUFFIXES:
                continue  # a stylesheet, image or font: bundler input, not a missing module
            bare = module.split("?", 1)[0]
            if (module.startswith(".") and PurePosixPath(bare).suffix.lower() in _UNSCANNED_COMPONENT_SUFFIXES
                    and (path.parent / bare).is_file()):
                continue  # `import App from "./App.vue"`: a component file this scanner does not read
            if _into_excluded_directory(state, rel, module):
                continue
            graph.issues.append(Issue("UNRESOLVED_LOCAL_IMPORT", "warning", f"Local import could not be resolved: {module}",
                                      [file_node], f"{rel}:{line}", "Check paths, tsconfig aliases and excluded files."))
        elif local:
            # A package binding. Recording it keeps `format()` from `date-fns` from being
            # name-matched to an unrelated `format` defined in this repository.
            state.imports.setdefault(rel, {})[local] = (f"external:{module}", exported)
    for module, public, imported, _line in facts.reexports:
        target, _ = state.import_index.javascript(rel, module)
        if target:
            state.js_reexports.setdefault(rel, []).append((target, public, imported))
    for owner, called, line, relationship in facts.calls:
        if re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", called) and called not in {"require", "import"}:
            state.pending_calls.append(PendingCall(owner_id(owner), called, f"{rel}:{line}", language, relationship))
    for name, base in facts.clients.items():
        state.http_clients[(rel, name)] = base
    # Hook and injection results are decided per call site (Request.hook_bound), not by a
    # file-wide name: a same-named parameter in another function is not that client.
    state.js_untraced_clients.update((rel, name) for name in facts.free_receivers)
    state.js_requests.extend((rel, file_node, request) for request in facts.requests)
    for line in facts.uncertain_requests:
        graph.issues.append(Issue("DYNAMIC_HTTP_REQUEST", "info", "HTTP URL or method requires runtime values.",
                                  [file_node], f"{rel}:{line}", "Declare the API mapping or review the request wrapper."))
    from .postgres import add_sql, looks_like_sql
    sql_shaped = set()
    for owner, sql, line, dynamic, parameterized in facts.queries:
        # A splice in a table position (`FROM ${t}` spelled `FROM $1`) is not grammar the gate
        # accepts, so the spelling with an identifier in each hole is checked too.
        if looks_like_sql(sql) or looks_like_sql(re.sub(r"\$\d+", "x", sql)):
            sql_shaped.add((owner, line))
        add_sql(graph, owner_id(owner), sql, f"{rel}:{line}", dynamic=dynamic, gated=True, parameterized=parameterized)
    for owner, line, holes in facts.sql_interpolations:
        # Prose handed to a `.query()` method (`dialog.query(`Delete ${name}?`)`) is not SQL,
        # so what it splices in is neither an injection nor dynamic SQL.
        if (owner, line) in sql_shaped:
            _report_sql_interpolation(graph, owner_id(owner), f"{rel}:{line}", holes)
    declared = set()
    # `db.collection('x')` is Firestore and others too: the receiver must be a MongoDB handle.
    lines = text.splitlines() if any(store.detail == "MongoDB driver collection() literal" for store in facts.stores) else []
    not_mongo = {id(store) for store in facts.stores if store.detail == "MongoDB driver collection() literal"
                 and not _mongo_collection_call(state, rel, text, store.line, store.name, lines)}
    for store in facts.stores:
        declared.add(id(store))
        if id(store) in not_mongo:
            continue
        _add_store_edge(graph, owner_id(store.owner), store.kind, store.name, store.resolution, f"{rel}:{store.line}",
                        detail=_store_detail(store.detail, store.operation), origin="tree-sitter")
    for name, model in facts.models.items():
        if id(model) in not_mongo:
            continue
        state.js_models[(rel, name)] = model
        if id(model) not in declared:
            _add_store_edge(graph, file_node, model.kind, model.name, model.resolution, f"{rel}:{model.line}",
                            detail=_store_detail(model.detail, model.operation), origin="tree-sitter")
    for owner, binding, operation, line, via in facts.model_refs:
        state.js_model_refs.append((rel, owner_id(owner), binding, operation, line, via))
    for owner, receiver, accessor, method, line in facts.member_stores:
        state.js_member_stores.append((rel, owner_id(owner), receiver, accessor, method, line))


def _next_router_base(state: ScanState, parts: list[str], directory: str, *, lenient: bool) -> int | None:
    """Index of the Next.js router directory in `parts`.

    The first `app`/`pages` directory sitting at a project root (the repository root, a
    directory holding package.json or next.config.*, either optionally followed by
    `src/`) is the router. A route SEGMENT named `app` (`app/(dash)/app/settings`) is
    not, and neither is `components/pages/`. `lenient` keeps the first `app` directory
    for checkouts without a package manifest, which App Router file names already
    disambiguate; `pages/` has no such file naming, so it requires a project root."""
    indexes = [index for index, part in enumerate(parts[:-1]) if part == directory]
    for index in indexes:
        prefix = parts[:index]
        if prefix and prefix[-1] == "src":
            prefix = prefix[:-1]
        project = "/".join(prefix)
        if not prefix or any(f"{project}/{marker}" in state.admitted_paths for marker in _NEXT_PROJECT_MARKERS):
            return index
    return indexes[0] if indexes and lenient else None


def _next_route_variants(segments: list[str]) -> tuple[list[str], bool, list[str]]:
    """URL paths a Next.js route directory serves, whether it ends in a catch-all, and each
    path as declared (`/orders/[orderId]`, for `state.route_metadata`).

    Route groups `(x)` and parallel-route slots `@x` add no URL segment; intercepting
    markers `(.)x` are dropped from the segment. `[[...slug]]` also serves its parent."""
    parts: list[str] = []
    declared: list[str] = []
    optional = False
    for segment in segments:
        if segment.startswith("@") or (segment.startswith("(") and segment.endswith(")")):
            continue
        segment = re.sub(r"^(?:\((?:\.{1,3}|\.\.\)\(\.\.)\))+", "", segment)
        if not segment:
            continue
        declared.append(segment)
        if segment.startswith("[[..."):
            optional = True
            parts.append("{dynamic}")
        elif segment.startswith("["):
            parts.append("{dynamic}")
        else:
            parts.append(segment)
    route = "/" + "/".join(parts)
    variants, spelled = [route], ["/" + "/".join(declared)]
    if optional:
        variants.append("/" + "/".join(parts[:-1]))
        spelled.append("/" + "/".join(declared[:-1]))
    catch_all = bool(segments) and segments[-1].startswith(("[...", "[[..."))
    return variants, catch_all, spelled


def _add_next_routes(state: ScanState, rel: str, path: Path, facts, file_node: str, language: str) -> None:
    graph = state.graph
    parts = rel.split("/")

    def handler_target(local: str) -> str:
        symbol = _symbol_id(rel, local)
        return symbol if symbol in graph.nodes else file_node

    def add_endpoint(method: str, route: str, target: str, origin: str, catch_all: bool, declared: str) -> None:
        endpoint = _endpoint_id(method, route)
        graph.add_node(Node(endpoint, "endpoint", f"{method} {normalise_route(route)}", path=rel,
                            line=graph.nodes[target].line, language=language,
                            metadata={"method": method, "route": normalise_route(route), "framework": "nextjs",
                                      "catch_all": catch_all, **route_metadata(graph, endpoint, declared)}))
        graph.add_edge(Edge(endpoint, target, "HANDLES_API", "exact", f"{rel}:{graph.nodes[target].line or 1}",
                            origin=origin))

    def add_page(route: str, origin: str, declared: str) -> None:
        page_id = stable_id("page", route)
        graph.add_node(Node(page_id, "page", route, path=rel, metadata={"route": route, "framework": "nextjs",
                                                                         **route_metadata(graph, page_id, declared)}))
        graph.add_edge(Edge(page_id, file_node, "IMPLEMENTED_BY", "exact", origin, origin="framework_path"))
        # The default export IS the page component. Reaching it only through the file's
        # CONTAINS edge made every page one expensive hop further from its data.
        component = _symbol_id(rel, facts.exports.get("default", ""))
        if component in graph.nodes:
            graph.add_edge(Edge(page_id, component, "IMPLEMENTED_BY", "exact", origin, origin="framework_path"))

    if path.stem in {"route", "page"}:
        base = _next_router_base(state, parts, "app", lenient=True)
        if base is None:
            return
        variants, catch_all, spelled = _next_route_variants(parts[base + 1:-1])
        if path.stem == "page":
            for route, declared in zip(variants, spelled):
                add_page(route, "next_app_router_path", declared)
            return
        for public, local in sorted(facts.exports.items()):
            if public in _HTTP_HANDLER_NAMES:
                for route, declared in zip(variants, spelled):
                    add_endpoint(public, route, handler_target(local), "nextjs_app_router", catch_all, declared)
        return
    base = _next_router_base(state, parts, "pages", lenient=False)
    if base is None or "default" not in facts.exports:
        return
    segments = [*parts[base + 1:-1], path.stem]
    if any(segment.startswith("_") for segment in segments):
        return  # _app, _document, _error, _middleware: framework shells, not routes
    if segments[-1] == "index":
        segments = segments[:-1]
    variants, catch_all, spelled = _next_route_variants(segments)
    if segments and segments[0] == "api":
        # A Pages Router API route is one default-export handler for every method.
        for route, declared in zip(variants, spelled):
            add_endpoint("ANY", route, handler_target(facts.exports["default"]), "nextjs_pages_router", catch_all,
                         declared)
    else:
        for route, declared in zip(variants, spelled):
            add_page(route, "next_pages_router_path", declared)


#: The `style` of a `ScanState.js_requests` entry that records a server route this scanner does not model.
_UNMODELLED_ROUTE = "unmodelled_route"
#: Imports that make a `routes/` module's `loader`/`action` a server route.
_SERVER_ROUTE_MODULE = re.compile(r"@remix-run/|@react-router/|react-router$|\./\+types/")


def _add_route_registrations(state: ScanState, rel: str, facts, file_node: str, language: str, *, routed: bool) -> None:
    """Endpoints for the server routes a JS/TS file registers in a shape that is modelled, and
    a record of every other route it registers, which `_detect_endpoint_gaps` reads.

    Only `app.<verb>("/literal", handler)` on a server this file creates and starts
    listening is modelled (probable). A router, plugin or sub-app is mounted under a prefix
    decided elsewhere, and NestJS controllers, SvelteKit `+server`, Remix resource routes and
    Nuxt server handlers are not modelled, so a call with no handler may be served by them.
    Test code (`core.files.is_test_path`) registers mock servers, not the application's
    routes, so it records neither."""
    graph = state.graph
    if is_test_path(rel):
        return

    def unmodelled(description: str, line: int, method: str = "ANY") -> None:
        state.js_requests.append((rel, file_node, javascript.Request("", method, "", False, line, description,
                                                                      style=_UNMODELLED_ROUTE)))

    for method, route, line, shape, receiver in facts.route_registrations:
        if shape != "server":
            verb = "all" if method == "ANY" else method.lower()
            what = "a NestJS controller route" if shape == "controller" else f"`{receiver}.{verb}()` on a router"
            unmodelled(what, line, method)
            continue
        declared, route = route, normalise_route(re.sub(r":([A-Za-z_]\w*)", r"{\1}", route))
        endpoint = _endpoint_id(method, route)
        graph.add_node(Node(endpoint, "endpoint", f"{method} {route}", path=rel, line=line, language=language,
                            metadata={"method": method, "route": route, "framework": "javascript-server",
                                      **route_metadata(graph, endpoint, declared)}))
        graph.add_edge(Edge(endpoint, file_node, "HANDLES_API", "probable", f"{rel}:{line}", origin="tree-sitter",
                            detail=f"`{receiver}.{method.lower()}()` on a server this file starts; middleware unverified"))
    if routed:
        return
    # File-based handlers of other frameworks: SvelteKit `+server`, Expo `*+api`, Astro
    # `pages/**` and Remix/React Router `routes/**` exporting verbs, `loader` or `action`, and
    # Nuxt/Nitro `server/api/**` and `server/routes/**` default exports.
    directories = rel.split("/")[:-1]
    stem = PurePosixPath(rel).name.split(".")[0]
    verbs = sorted(set(facts.exports) & _HTTP_HANDLER_NAMES)
    if verbs and (stem.startswith("+") or stem.endswith("+api") or stem == "route" or {"pages", "routes"} & set(directories)):
        unmodelled(f"file-based `{'`/`'.join(verbs)}` handlers", 1)
    elif ({"loader", "action"} & set(facts.exports) and "routes" in directories
          and any(_SERVER_ROUTE_MODULE.match(module) for module, _local, _exported, _line in facts.imports)):
        # A client-side data router (a Vite SPA) exports `loader` too; only Remix and React
        # Router framework modules (or their generated `./+types/` route types) run it on a server.
        unmodelled("a route module's `loader`/`action`", 1)
    elif "default" in facts.exports and "server" in directories and {"api", "routes"} & set(
            directories[directories.index("server") + 1:]):
        unmodelled("a server route file", 1)


def _js_export_origin(state: ScanState, path: str, public: str, depth: int = 0) -> tuple[str, str] | None:
    """(file, local binding) that `public` exported from `path` denotes, through barrels."""
    if depth > 6:
        return None
    exports = state.js_exports.get(path, {})
    if public in exports:
        return path, exports[public]
    for target, name, imported in state.js_reexports.get(path, []):
        if name == public and imported != "*":
            return _js_export_origin(state, target, imported, depth + 1) or (target, imported)
        if name == "*" and public != "default" and (found := _js_export_origin(state, target, public, depth + 1)):
            return found
    return None


def _client_base(state: ScanState, rel: str, receiver: str) -> str | None:
    """Literal base URL of an HTTP client binding; None when the receiver is not a client."""
    if (rel, receiver) in state.http_clients:
        return state.http_clients[(rel, receiver)]
    binding = state.imports.get(rel, {}).get(receiver)
    if not binding or binding[0].startswith("external:") or binding[1] == "":
        return None
    if binding[1] == "*":
        # `const api = require("./client")` of a module that assigns `module.exports = axios.create(...)`.
        return state.http_clients.get((binding[0], "default"))
    origin = _js_export_origin(state, *binding)
    return state.http_clients.get(origin) if origin else None


#: Framework prefixes of public environment variables: `NEXT_PUBLIC_API_URL` names `API_URL`.
_ORIGIN_PREFIXES = (("next", "public"), ("nuxt", "public"), ("expo", "public"), ("react", "app"), ("vue", "app"),
                    ("vite",), ("gatsby",), ("public",))
#: Words an origin's name is made of when it denotes this repository's own API. Any other word
#: (`STRIPE_API_URL`, `AUTH_SERVICE_URL`, `supabaseUrl`) names another service.
_OWN_ORIGIN_WORDS = frozenset({
    "api", "apis", "backend", "server", "service", "base", "app", "site", "web", "rest", "http", "https",
    "internal", "local", "gateway", "proxy", "graphql", "origin", "url", "uri", "host", "endpoint", "domain",
    "address", "addr", "root", "path", "prefix", "v1", "v2", "v3", "v4"})
#: A development origin on this machine: the repository's own backend behind a dev proxy.
_LOCAL_ORIGIN = re.compile(r"^(?:https?:)?//(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?=[/?#]|$)", re.I)
#: Globals whose `.get(url)` is not a request to this repository's API: browser-test runners
#: (Protractor/WebdriverIO `browser`, Cypress `cy`, Playwright `page`, Selenium `driver`) and DOM objects.
_NOT_CLIENT_GLOBALS = frozenset({"browser", "cy", "page", "driver", "window", "document", "location", "history",
                                 "navigator", "localStorage", "sessionStorage", "globalThis", "self"})
#: Receivers that send a URL as written: axios/ky defaults, jQuery, and Angular's injected HttpClient
#: (`this.http`). A base URL assumed for an untraced client is never theirs.
_UNBASED_RECEIVERS = frozenset({"axios", "ky", "$", "jQuery"})


def _own_origin(state: ScanState, name: str) -> bool:
    """Whether a configured origin named `name` is this repository's API: listed in
    `[impact] api_origins`, or spelled only with words such as API, BACKEND, SERVER or BASE
    after a framework prefix."""
    if name.lower() in {origin.lower() for origin in state.config.api_origins}:
        return True
    words = [word.lower() for word in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z0-9]+(?![a-z])", name)]
    for prefix in _ORIGIN_PREFIXES:
        if tuple(words[:len(prefix)]) == prefix and len(words) > len(prefix):
            words = words[len(prefix):]
            break
    return all(word in _OWN_ORIGIN_WORDS for word in words)


def _base_target(state: ScanState, base: str, *, trusted: bool = False) -> tuple[str, str] | None:
    """(path, configured origin name or "") that a client base puts requests under, or None
    when the base is another service's: a literal absolute URL that is not this machine, or
    a configured origin whose name is not this repository's API (unless `trusted`)."""
    configured = javascript.split_configured(base)
    if configured is not None:
        name, path = configured
        return (path, name) if trusted or _own_origin(state, name) else None
    if local := _LOCAL_ORIGIN.match(base):
        return base[local.end():], "localhost"
    if _URL_SCHEME.match(base):
        return None
    return base, ""


def _untraced_bases(state: ScanState):
    """For a file, (base, description) assumed for a client whose declaration cannot be followed.

    `[impact] client_api_base` when set. Otherwise the one base URL the caller's package
    declares, or, when that package declares none, the one the repository declares; with
    several candidates the base is unknown. Only bases of this repository's API count."""
    from .columns import PackageRoots

    explicit = state.config.client_api_base
    if explicit:
        if _URL_SCHEME.match(explicit) and not _LOCAL_ORIGIN.match(explicit):
            # Configured explicitly, so an absolute value still names this repository's API.
            explicit = javascript.configured_base("client_api_base", "/" + explicit.split("//", 1)[1].partition("/")[2])
        return lambda _rel: (explicit, "[impact] client_api_base")
    roots = PackageRoots(state)
    by_package: dict[str, set[str]] = defaultdict(set)
    for (rel, _name), base in state.http_clients.items():
        if base and _base_target(state, base) is not None:
            by_package[roots.of(rel)].add(base)
    everywhere = set().union(*by_package.values()) if by_package else set()

    def untraced(rel: str) -> tuple[str, str]:
        package = roots.of(rel)
        candidates = by_package.get(package) or everywhere
        if len(candidates) != 1:
            return "", ""
        [base] = candidates
        configured = javascript.split_configured(base)
        shown = f"<{configured[0]}>{configured[1]}" if configured else base
        owner = "this package's" if package and by_package.get(package) else "the repository's"
        return base, f"{owner} only client base URL ({shown})"

    return untraced


def _external_reference(state: ScanState, source: str, evidence: str, origin: str = "") -> None:
    # A fully qualified URL belongs to a different service unless configured explicitly.
    # It must not alias a same-path handler in this repository.
    named = f" (behind `{origin}`, which does not name this repository's API)" if origin else ""
    state.graph.issues.append(Issue(
        "EXTERNAL_API_REFERENCE", "info", f"HTTP target is external or relative to runtime configuration{named}.",
        [source], evidence,
        "Review service origin and base-path configuration; list an origin in [impact] api_origins if it is this API."))


def _resolve_js_requests(state: ScanState) -> None:
    graph = state.graph
    untraced_base = _untraced_bases(state)
    handled_get = [graph.nodes[edge.source] for edge in graph.edges
                   if edge.kind in {"HANDLES_API", "IMPLEMENTED_BY"}
                   and graph.nodes[edge.source].metadata.get("method") == "GET"]
    for rel, file_node, request in state.js_requests:
        if request.style in {"anchor", "resource"} and (not request.url.startswith("/") or request.url.startswith("//")):
            continue  # external browser navigation is not an API reference
        if request.style == _UNMODELLED_ROUTE:
            continue  # a server route registration, read by _detect_endpoint_gaps
        source = _symbol_id(rel, request.owner) if request.owner else file_node
        source = source if source in graph.nodes else file_node
        evidence = f"{rel}:{request.line}"
        url, resolution = request.url, "exact"
        details = [f"HTTP request syntax ({request.style}); runtime dispatch unverified"]
        origin = request.configured_origin
        if request.receiver:
            base, assumed, trusted = _client_base(state, rel, request.receiver), False, False
            if base is None:
                if request.receiver in _UNBASED_RECEIVERS or request.receiver.startswith("this."):
                    base = ""
                elif (request.hook_bound or request.receiver in state.config.client_receivers
                      or ((rel, request.receiver) in state.js_untraced_clients
                          and request.receiver not in _NOT_CLIENT_GLOBALS and not is_test_path(rel))):
                    if not (request.url.startswith("/") or request.configured_origin):
                        continue  # `params.get("q")` on a hook result is not a request
                    (base, described), assumed, resolution = untraced_base(rel), True, "probable"
                    trusted = described == "[impact] client_api_base"
                    details.append(f"client `{request.receiver}` was not traced to a declaration; "
                                   + (f"base URL from {described}" if base else "its base URL is unknown"))
                else:
                    continue  # `router.get("/x", handler)` and other non-client receivers
            if base and not _URL_SCHEME.match(url):
                target = _base_target(state, base, trusted=trusted)
                if target is None:
                    configured = javascript.split_configured(base)
                    _external_reference(state, source, evidence, configured[0] if configured else "")
                    continue
                path, base_origin = target
                prefix = path.rstrip("/")
                # An assumed base is not added again to a URL that already starts with it.
                if not (assumed and prefix and (url == prefix or url.startswith(prefix + "/"))):
                    url = prefix + "/" + url.lstrip("/")
                origin = origin or base_origin
                details.append(f"base URL from client `{request.receiver}`")
        if not origin and (local := _LOCAL_ORIGIN.match(url)):
            url, origin = url[local.end():] or "/", "localhost"
        if origin:
            if origin != "localhost" and not _own_origin(state, origin) and origin != "client_api_base":
                _external_reference(state, source, evidence, origin)
                continue
            resolution = "probable"
            details.append(f"URL starts with a configured origin ({origin}); assumed to be this repository's API")
            url = url if url.startswith("/") else "/" + url
        if not url.startswith("/") or url.startswith("//"):
            _external_reference(state, source, evidence)
            continue
        if request.dynamic or request.method == "UNKNOWN":
            resolution = "probable"
        path = url.split("?", 1)[0].split("#", 1)[0]
        # `/export.${fmt}`, `/v2${path}`, `/items${query}`: a runtime value joined to the last
        # segment may be a file extension, further segments or a query string. The call is
        # kept as an open-ended prefix and matched against the handlers that extend it.
        open_tail = bool(re.search(r"[^/]\{dynamic\}$", path))
        if open_tail:
            path, resolution = path[:-len("{dynamic}")], "probable"
            details.append("the URL ends in a runtime value joined to its last segment; matched by route prefix")
        route = normalise_route(path)
        segments = _route_segments(route)
        # A page link is not evidence of a missing API. Keep browser navigation
        # only when a scanned GET handler can serve its route shape.
        if request.style in {"anchor", "resource"} and not any(
                _serves(segments, handler, open_tail=open_tail, loose=False)
                for handler in handled_get):
            continue
        if segments and all(segment == "{dynamic}" for segment in segments):
            # `${API_URL}${path}` or `/${path}`: nothing of the target is spelled, so no handler
            # can be matched and none can be called missing.
            graph.issues.append(Issue("DYNAMIC_HTTP_REQUEST", "info", "HTTP URL or method requires runtime values.",
                                      [source], evidence, "Declare the API mapping or review the request wrapper."))
            continue
        endpoint = _endpoint_id(request.method, route + "*" if open_tail else route)
        # No path or line: an endpoint node belongs to its handler, and the call site is
        # already the edge's evidence. The first caller must not become its location.
        graph.add_node(Node(endpoint, "endpoint", f"{request.method} {route}{'*' if open_tail else ''}",
                            metadata={"method": request.method, "route": route, "dynamic": request.dynamic,
                                      **({"open_tail": True} if open_tail else {})}))
        if origin and origin != "localhost":
            graph.nodes[endpoint].metadata["open_head"] = True
        graph.add_edge(Edge(source, endpoint, "CALLS_API", resolution, evidence,
                            origin="tree-sitter", detail="; ".join(details)))


_PRISMA_PROVIDER = re.compile(r'datasource\s+\w+\s*\{[^}]*?\bprovider\s*=\s*"([\w-]+)"', re.S)
_PRISMA_MODEL = re.compile(r"^[ \t]*(model|view)\s+(\w+)\s*\{(.*?)^[ \t]*\}", re.S | re.M)


def _scan_prisma_schemas(state: ScanState) -> dict[tuple[str, str], tuple[str, str]]:
    """(package root, client accessor `user`) -> (store kind, table). Declarations get edges.

    The schema files are the ones the column check uses (`columns.prisma_schema_sets`:
    the configured or conventional schema of each package, never a test fixture), so a
    stray copy cannot remap an accessor. Comments are ignored, and `public` is the
    default schema, not part of the table name. An accessor that the used files map to
    different tables is dropped rather than guessed."""
    from .columns import POSTGRES_PRISMA_PROVIDERS, PackageRoots, prisma_schema_sets, strip_prisma_comments
    models: dict[tuple[str, str], tuple[str, str]] = {}
    if not state.prisma_schemas:
        return models
    sets = prisma_schema_sets(state, PackageRoots(state))
    texts = {rel: strip_prisma_comments(text) for rel, text in state.prisma_schemas}
    ambiguous: set[tuple[str, str]] = set()
    unsupported: set[str] = set()
    for root, (files, _) in sets.items():
        if not files:
            continue
        # Each package declares its own datasource: a monorepo may hold a PostgreSQL
        # service beside a MongoDB one, and neither decides the other's models.
        providers = {m.group(1) for rel in files for m in _PRISMA_PROVIDER.finditer(texts[rel])}
        kind = ("postgres_table" if providers and providers <= POSTGRES_PRISMA_PROVIDERS
                else "mongo_collection" if providers == {"mongodb"} else None)
        if kind is None:
            state.graph.issues.append(Issue(
                "PRISMA_PROVIDER_UNSUPPORTED", "info",
                f"Prisma datasource provider {', '.join(sorted(providers)) or 'not declared'}; only PostgreSQL and MongoDB models are mapped.",
                [_file_id(files[0])], files[0],
                "Declare the datasource provider in a schema file inside the scanned tree.",
            ))
            unsupported.add(root)
            continue
        for rel in files:
            text = texts[rel]
            for match in _PRISMA_MODEL.finditer(text):
                block, name, body = match.groups()
                mapped = re.search(r'@@map\(\s*(?:name\s*:\s*)?"([^"]+)"', body)
                schema = re.search(r'@@schema\(\s*"([^"]+)"', body)
                table = mapped.group(1) if mapped else name
                if schema and schema.group(1) != "public" and kind == "postgres_table":
                    table = f"{schema.group(1)}.{table}"
                key = (root, name[0].lower() + name[1:])
                if models.setdefault(key, (kind, table)) != (kind, table):
                    ambiguous.add(key)
                _add_store_edge(state.graph, _file_id(rel), kind, table, "exact",
                                f"{rel}:{text.count(chr(10), 0, match.start()) + 1}",
                                detail=f"Prisma {block} {name} (declares)", origin="prisma_schema")
    for key in ambiguous:
        models.pop(key, None)
    for root in unsupported:
        # A package with its own (MySQL, SQLite…) schema maps nothing, and its accessors must
        # not fall back to a table other packages declare: `_prisma_accessor` sees the package.
        models[(root, "")] = ("", "")
    return models


def _prisma_accessor(prisma: dict[tuple[str, str], tuple[str, str]], root: str, accessor: str) -> tuple[str, str] | None:
    """The model a client accessor names from a file in package `root`: that package's own
    schema when it has one, else the one table every package's schema agrees on."""
    if (root, accessor) in prisma:
        return prisma[(root, accessor)] if accessor else None
    if any(package == root for package, _ in prisma):
        return None
    found = {model for (_, name), model in prisma.items() if name == accessor}
    return found.pop() if len(found) == 1 else None


_MONGO_PACKAGES = frozenset({"mongodb", "mongoose"})
_MONGO_IMPORT = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)["'](?:mongodb|mongoose)(?:/[^"']*)?["']""")
# mongosh/legacy shell scripts use a global `db`; these calls exist only there.
_MONGO_SHELL = re.compile(r"\b(?:db\s*\.\s*(?:getSiblingDB|createCollection|createUser|getCollection|getName|dropDatabase)"
                          r"|printjson|ISODate|NumberLong|NumberDecimal)\s*\(")


# What a MongoDB database (or, for `.collection()`, a Mongoose connection) is assigned from.
_MONGO_DB_INIT = re.compile(r"\.\s*(?:db|getSiblingDB)\s*\(|\bconnection\s*\.\s*db\b")
_MONGO_CONNECTION_INIT = re.compile(r"\bmongoose\s*\.\s*connection\b(?!\s*\.)|\.\s*(?:createConnection|useDb)\s*\(")
_MONGO_MODIFIERS = r"(?:(?:private|public|protected|readonly|static|declare|override)\s+)*"
_MONGO_NOT_PARAMETER_LISTS = frozenset({"if", "while", "for", "switch", "catch", "return", "typeof", "await", "with"})
# A JS string or template literal (kept) or a comment (`c`, blanked before reading bindings).
_JS_COMMENT_OR_STRING = re.compile(r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`|(?P<c>//[^\n]*|/\*.*?\*/)""", re.S)
# A type annotation, bounded: an unbounded lazy `[^=;\n]+?` rescanned the rest of the line
# at every occurrence of the name (`{ db: 1, db: 2, … }` on one line).
_MONGO_TYPE = r"[^=;\n]{1,120}?"
# What follows a parameter list: an optional return type, then `=>` or a body.
_MONGO_AFTER_PARAMETERS = re.compile(r"\s*(?::\s*[^{;=\n]{1,120}?)?\s*(?:=>|\{)")


def _mongo_type(text: str, annotation: str, *, collection: bool) -> bool:
    """Is a type annotation `Db` (or `Connection` for `.collection()`) imported from the driver?"""
    names = ("Db", "Connection") if collection else ("Db",)
    match = re.fullmatch(r"\s*(?:(mongodb|mongoose)\s*\.\s*)?([\w$]+)\s*", annotation or "")
    if not match or match.group(2) not in names:
        return False
    return bool(match.group(1)) or bool(re.search(
        rf"""\bimport\s+(?:type\s+)?\{{[^}}]*\b{match.group(2)}\b[^}}]*\}}\s*from\s*["'](?:mongodb|mongoose)["']"""
        rf"""|\{{[^}}]*\b{match.group(2)}\b[^}}]*\}}\s*=\s*require\s*\(\s*["'](?:mongodb|mongoose)["']""", text))


def _mongo_bindings(text: str, name: str, *, member: bool, collection: bool) -> set[str]:
    """How `name` is bound in `text`: "handle" (from `<client>.db(…)`, typed `Db`), "value"
    (anything else it is initialised or imported as), "parameter" (an untyped or other-typed
    parameter). `member`: the receiver is `this.name`, so only fields and `this.name =` count."""
    kinds: set[str] = set()
    n = re.escape(name)
    inits = (_MONGO_DB_INIT, _MONGO_CONNECTION_INIT) if collection else (_MONGO_DB_INIT,)
    # `// db = null when disconnected` is not a binding.
    text = _JS_COMMENT_OR_STRING.sub(lambda m: m.group(0) if m.group("c") is None else re.sub(r"[^\n]", " ", m.group(0)), text)

    def initialised(init: str) -> str | None:
        if re.fullmatch(r"\s*(?:null|undefined|void\s+0)\s*[;,)}]*\s*", init):
            return None  # `db = null` in `close()` clears a handle; it does not rebind it
        return "handle" if any(p.search(init) for p in inits) else "value"

    if member:
        assignments = [rf"\bthis\s*\.\s*{n}\s*=(?![=>])(?P<init>[^;\n]*)",
                       rf"^[ \t]*{_MONGO_MODIFIERS}{n}\s*[?!]?\s*(?::\s*(?P<type>{_MONGO_TYPE}))?\s*(?:=(?![=>])(?P<init>[^;\n]*)|;|$)"]
        parameters = [rf"[(,]\s*(?:@\w+\([^()]*\)\s*)*(?:private|public|protected|readonly)\s+(?:readonly\s+)?{n}\s*\??"
                      rf"\s*(?::\s*(?P<type>[\w$.]+))?"]
    else:
        assignments = [rf"(?:^|[^\w$.])(?:(?:const|let|var)\s+)?{n}\s*(?::\s*(?P<type>{_MONGO_TYPE}))?\s*(?:=(?![=>])(?P<init>[^;\n]*))",
                       rf"\b(?:const|let|var)\s+{n}\s*(?::\s*(?P<type>{_MONGO_TYPE}))?\s*(?:;|$)"]
        parameters = [rf"(?<![\w$.]){n}\s*=>"]
        if re.search(rf"\b(?:function|class)\s+{n}\b|\bimport\b[^;\n]*?(?<![\w$.]){n}\b[^;\n]*?\bfrom\b"
                     rf"|\bimport\s+{n}\s*=|\{{[^{{}}]{{0,400}}?(?<![\w$.]){n}\b[^{{}}]{{0,400}}\}}\s*=", text, re.M):
            kinds.add("value")
        # `(a, db: Db) => …`, `function f(db) {`: each innermost parenthesised list is read
        # once. A pattern spanning list and name backtracked over every occurrence of the
        # name, which made an argument object with many `db:` keys quadratic.
        name_in_list = re.compile(rf"(?<![\w$.]){n}\s*\??\s*(?::\s*(?P<type>[\w$.]+))?")
        for group in re.finditer(r"\(([^()]*)\)", text):
            if name not in group.group(1) or not _MONGO_AFTER_PARAMETERS.match(text, group.end()):
                continue
            head = re.search(r"([\w$]*)\s*$", text[max(0, group.start() - 64):group.start()])
            if head and head.group(1) in _MONGO_NOT_PARAMETER_LISTS:
                continue
            for match in name_in_list.finditer(group.group(1)):
                kinds.add("handle" if match.group("type") and _mongo_type(text, match.group("type"), collection=collection)
                          else "parameter")
    for pattern in assignments:
        for match in re.finditer(pattern, text, re.M):
            if match.groupdict().get("type") and _mongo_type(text, match.group("type"), collection=collection):
                kinds.add("handle")
            elif match.groupdict().get("init") is not None and (kind := initialised(match.group("init"))):
                kinds.add(kind)
    for pattern in parameters:
        for match in re.finditer(pattern, text, re.M):
            if match.groupdict().get("head") in _MONGO_NOT_PARAMETER_LISTS:
                continue
            kinds.add("handle" if match.groupdict().get("type") and _mongo_type(text, match.group("type"), collection=collection)
                      else "parameter")
    return kinds


def _mongo_handle(state: ScanState, rel: str, receiver: str, texts: dict[str, str], *,
                  collection: bool = False, depth: int = 0) -> bool:
    """`_mongo_handle_uncached`, once per (file, receiver): every call site on one receiver
    has the same answer, and each answer reads the whole file."""
    key = (rel, receiver, collection, depth)
    if key not in state.mongo_handles:
        state.mongo_handles[key] = _mongo_handle_uncached(state, rel, receiver, texts, collection=collection, depth=depth)
    return state.mongo_handles[key]


def _mongo_handle_uncached(state: ScanState, rel: str, receiver: str, texts: dict[str, str], *,
                           collection: bool = False, depth: int = 0) -> bool:
    """Is `receiver` (`db`, `this.db`) a MongoDB database handle in `rel`?

    `db.users.find(u => …)` is as often an in-memory array, a Sequelize model registry
    (`db.User.findOne`), a Prisma client (`this.db.user.aggregate`) or Firestore
    (`admin.firestore().collection('x')`). A file importing the driver does not decide it:
    when the receiver is bound in the file, that binding does. It is a handle only when
    assigned from `<client>.db(…)` or `mongoose.connection.db`, typed as the driver's `Db`,
    or imported from a module that exports such a handle. An unbound (global) receiver
    counts in a file that imports `mongodb`/`mongoose`, or in a mongo shell script.
    `collection`: the call is `<receiver>.collection('x')`, which a Mongoose connection
    also has (`mongoose.connection`, `createConnection(…)`, `useDb(…)`, typed `Connection`)."""
    if rel not in texts:
        try:
            texts[rel] = read_source(state.root, state.root / rel, state.config.max_file_bytes).text or ""
        except OSError:
            texts[rel] = ""
    text = texts[rel]
    parts = [part.strip() for part in receiver.split(".")]
    imports = state.imports.get(rel, {})

    def driver_package(module: str) -> bool:
        return module.startswith("external:") and module[len("external:"):].split("/", 1)[0] in _MONGO_PACKAGES

    driver = any(driver_package(module) for module, _ in imports.values()) or bool(_MONGO_IMPORT.search(text))
    if parts[0] != "this" and parts[0] in imports and driver_package(imports[parts[0]][0]) and len(parts) > 1:
        return True  # `mongoose.connection.db`, `mongoose.connection.collection('x')`
    if len(parts) == 1 or (len(parts) == 2 and parts[0] == "this"):
        name, member = parts[-1], parts[0] == "this"
        if not member and name in imports:
            module, exported = imports[name]
            if driver_package(module) or module.startswith("external:"):
                return False  # a package binding: not a database handle
            return (depth < 2 and exported not in ("", "*", "default")
                    and _mongo_handle(state, module, exported, texts, collection=collection, depth=depth + 1))
        kinds = _mongo_bindings(text, name, member=member, collection=collection)
    else:
        # `ctx.db`, `req.app.locals.db`: only an assignment to the whole path is a binding.
        written = r"\s*\.\s*".join(re.escape(part) for part in parts)
        inits = (_MONGO_DB_INIT, _MONGO_CONNECTION_INIT) if collection else (_MONGO_DB_INIT,)
        kinds = {"handle" if any(p.search(m.group("init")) for p in inits) else "value"
                 for m in re.finditer(rf"(?:^|[^\w$.]){written}\s*=(?![=>])(?P<init>[^;\n]*)", text, re.M)}
    if "value" in kinds:
        return False
    if "handle" in kinds:
        return True
    if kinds:
        return False  # a parameter: whatever the caller passes
    return driver or (receiver == "db" and bool(_MONGO_SHELL.search(text))
                      and not re.search(r"\bimport\b|\brequire\s*\(", text))


def _mongo_collection_call(state: ScanState, rel: str, text: str, line: int, name: str,
                           lines: list[str] | None = None) -> bool:
    """Is the `.collection('name')` call on `line` made on a MongoDB handle? The syntax fact
    does not keep its receiver, so it is read back from the source lines ending there.
    `lines`: `text.splitlines()`, split once by a caller checking many calls."""
    lines = text.splitlines() if lines is None else lines
    window = "\n".join(lines[max(0, line - 4):line])
    offset = len("\n".join(lines[max(0, line - 4):line - 1]))
    calls = [m for m in re.finditer(rf"\.\s*collection\s*\(\s*(['\"`]){re.escape(name)}\1", window) if m.start() >= offset]
    if not calls:
        return False
    for call in calls:
        before = window[:call.start()]
        if re.search(r"\.\s*db\s*\((?:[^()]|\([^()]*\))*\)\s*$", before):
            return True  # `client.db('shop').collection('orders')`, `client.db(cfg.name()).collection(…)`
        receiver = re.search(r"((?:this\s*\.\s*)?[\w$]+(?:\s*\.\s*[\w$]+)*)\s*$", before)
        if receiver and _mongo_handle(state, rel, re.sub(r"\s+", "", receiver.group(1)), {rel: text}, collection=True):
            return True  # one call on a handle is enough: `db.collection('x'); other.collection('x')`
    return False


def _resolve_js_stores(state: ScanState) -> None:
    graph = state.graph
    prisma = _scan_prisma_schemas(state)
    for rel, source, binding, operation, line, via in state.js_model_refs:
        model = state.js_models.get((rel, binding))
        if model is None:
            bound = state.imports.get(rel, {}).get(binding)
            if bound and not bound[0].startswith("external:") and bound[1] not in ("", "*"):
                origin = _js_export_origin(state, *bound)
                model = state.js_models.get(origin) if origin else None
        if model is None:
            continue
        _add_store_edge(graph, source, model.kind, model.name, "high" if model.resolution == "exact" else "probable",
                        f"{rel}:{line}", detail=_store_detail(f"{model.detail}; {via}()", operation), origin="tree-sitter")
    drizzle: dict[str, list] = defaultdict(list)
    for (_, name), model in state.js_models.items():
        if model.detail.startswith("Drizzle"):
            drizzle[name].append(model)
    from .columns import PackageRoots
    roots = PackageRoots(state)
    texts: dict[str, str] = {}
    for rel, source, receiver, accessor, method, line in state.js_member_stores:
        tail = receiver.rsplit(".", 1)[-1]
        evidence = f"{rel}:{line}"
        model = _prisma_accessor(prisma, roots.of(rel), accessor) if prisma else None
        if model is not None and (method in javascript.PRISMA_READS or method in javascript.PRISMA_WRITES):
            kind, table = model
            operation = "reads" if method in javascript.PRISMA_READS else "writes"
            _add_store_edge(graph, source, kind, table, "high", evidence, origin="tree-sitter",
                            detail=f"Prisma client {receiver}.{accessor}.{method}() ({operation})")
        elif tail == "query" and len(drizzle.get(accessor, ())) == 1 and method in javascript.PRISMA_READS:
            model = drizzle[accessor][0]
            _add_store_edge(graph, source, model.kind, model.name, "probable", evidence, origin="tree-sitter",
                            detail=f"Drizzle relational query {receiver}.{accessor}.{method}() (reads)")
        elif (tail == state.config.mongo_receiver and (operation := mongo_operation(method))
              and accessor not in MONGO_NOT_COLLECTIONS and _mongo_handle(state, rel, receiver, texts)):
            _add_store_edge(graph, source, "mongo_collection", accessor, "probable", evidence, origin="tree-sitter",
                            detail=f"MongoDB driver call {receiver}.{accessor}.{method}() ({operation})")


def _route_segments(route: str) -> list[str]:
    return [segment for segment in route.split("/") if segment]


#: File stems a bundler, framework or runtime loads without an import from another module:
#: Next.js route, special and metadata files (`sitemap`, `opengraph-image`, `global-error`,
#: parallel-route `default`), Remix `root` and `entry.client`/`entry.server`, SvelteKit
#: `+page`/`+layout`/`+server`/`+error` (also `+page.server`) and `hooks.server`/`hooks.client`.
_ENTRY_STEMS = frozenset({"main", "index", "app", "_app", "_document", "server", "client", "entry", "middleware",
                          "instrumentation", "layout", "page", "route", "template", "loading", "error", "not-found",
                          "global-error", "default", "sitemap", "robots", "manifest", "opengraph-image",
                          "twitter-image", "icon", "apple-icon", "root", "+page", "+layout", "+server", "+error",
                          "hooks"})
#: Directories whose every module a framework loads by file name: Remix `app/routes/**`.
_ENTRY_DIRECTORIES = ("app/routes/",)
#: Component files the JS/TS extractor never reads. Their imports are invisible, so a
#: repository holding any of them gets no dead-code judgements at all.
_UNSCANNED_COMPONENT_SUFFIXES = frozenset({".vue", ".svelte", ".astro", ".mdx"})


_JS_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")


class _JavaScriptLiveness:
    """Which JS/TS code the app can load: modules reached by import from entry files
    (routed pages and handlers, layouts, `main`/`index`...), and exported functions a
    live module imports or something calls. With no entry file, nothing is judged dead."""

    def __init__(self, state: ScanState):
        graph = state.graph
        imports: dict[str, set[str]] = defaultdict(set)
        entries = set()
        callers: list[tuple[str, str]] = []
        with_code: set[str] = set()
        for edge in graph.edges:
            source, target = graph.nodes.get(edge.source), graph.nodes.get(edge.target)
            if source is None or target is None:
                continue
            if edge.kind in {"CONTAINS", "CALLS", "CALLS_API", "RENDERS", "TOUCHES_STORE"}:
                with_code.add(edge.source)
            if edge.kind == "IMPORTS" and source.path and target.path:
                imports[source.path].add(target.path)
            elif edge.kind in {"IMPLEMENTED_BY", "HANDLES_API"} and target.path:
                entries.add(target.path)
            if edge.kind in {"CALLS", "RENDERS", "IMPLEMENTED_BY", "HANDLES_API"} and edge.source != edge.target:
                callers.append((source.path or "", edge.target))
        for rel, reexports in state.js_reexports.items():
            imports[rel].update(target for target, _, _ in reexports)
        declared, app_directories = _package_entries(state)

        def entry(node: Node) -> bool:
            stem = PurePosixPath(node.path).name.split(".")[0].lower()
            if stem in _ENTRY_STEMS:
                # A re-export-only `index` barrel is loaded by whoever imports it, not by a runtime.
                return not (stem == "index" and node.path in state.js_reexports and node.id not in with_code)
            return (any(directory in "/" + node.path for directory in ("/" + d for d in _ENTRY_DIRECTORIES))
                    or node.path.startswith(app_directories))

        entries |= declared
        entries |= {n.path for n in graph.nodes.values() if n.kind == "file" and n.path
                    and n.path.endswith(_JS_SOURCE_SUFFIXES) and entry(n)}
        # A .vue/.svelte/.astro/.mdx file can import any module, and those imports are not read.
        self.known = bool(entries) and not self._has_unscanned_components(state)
        self.exports = state.js_exports
        self.entries = frozenset(entries)
        self.live, stack = set(entries), list(entries)
        while stack:
            for target in imports[stack.pop()] - self.live:
                self.live.add(target)
                stack.append(target)
        # Only a caller the app can load keeps a function alive; a dead module's call does not.
        self.incoming = {target for path, target in callers if not path or path in self.live}
        # Names live modules take from each module; "*" (namespace, default, `export *`) is anything.
        self.used: dict[str, set[str]] = defaultdict(set)
        for importer in self.live:
            for target, exported in state.imports.get(importer, {}).values():
                self.used[target].add("*" if exported in {"*", "", "default"} else exported)
            for target, _public, imported in state.js_reexports.get(importer, []):
                self.used[target].add(imported)
        self.graph = graph

    def dead_reason(self, node: Node) -> str:
        """Why `node` cannot run, or "" when it may."""
        path = node.path or ""
        if not self.known or not path.endswith(_JS_SOURCE_SUFFIXES):
            return ""
        if path not in self.live:
            return f"{path} is not imported from any page, route or entry file"
        qualified = str(node.metadata.get("qualified_name") or "") if node.kind == "symbol" else ""
        if not qualified or path in self.entries:
            return ""  # an entry file's exports are loaded by the framework or runtime, not imported
        top = self.graph.nodes.get(_symbol_id(path, qualified.split(".")[0]), node)
        used = self.used.get(path, set())
        # An unexported function may be a callback or event handler; that is not judged.
        if top.id in self.incoming or not top.metadata.get("exported") or "*" in used:
            return ""
        names = {top.label.split(".")[0], *top.metadata.get("export_names", [])}
        if names & used:
            return ""
        # Used by value in its own file (`export const routes = [{ loader: load }]`): alive when
        # the use sits in a function body or module statement (""), or in a variable that is
        # not exported or whose export a live module imports.
        exports = self.exports.get(path, {})
        for holder in top.metadata.get("value_holders", []):
            public = {name for name, local in exports.items() if local == holder}
            if not holder or not public or public & used:
                return ""
        return f"`{top.label}` in {path} is exported but never imported or called"

    @staticmethod
    def _has_unscanned_components(state: ScanState) -> bool:
        import dataclasses
        config = dataclasses.replace(state.config, extensions=set(_UNSCANNED_COMPONENT_SUFFIXES))
        # Gitignored component files count too: being conservative only withholds a judgement.
        return next(iter_source_files(state.root, config, frozenset()), None) is not None


def _manifest_paths(value: object) -> list[str]:
    """Every string in a package.json `main`/`module`/`browser`/`bin`/`exports` value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [path for item in value.values() for path in _manifest_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in _manifest_paths(item)]
    return []


def _package_entries(state: ScanState) -> tuple[set[str], tuple[str, ...]]:
    """(scanned files a package.json names as `main`, `module`, `browser`, `bin` or `exports`,
    the `app/` directories of packages that use Expo Router, where every file is a route).
    An unreadable or invalid manifest adds nothing; a named file that was not scanned
    (`dist/index.js`) is not an entry."""
    scanned = {_rel(state.root, path) for path in state.files}
    entries, directories = set(), []
    for path in state.files:
        if path.name != "package.json":
            continue
        source = read_source(state.root, path, state.config.max_file_bytes)
        try:
            manifest = json.loads(source.text) if source.text is not None else None
        except ValueError:
            continue
        if not isinstance(manifest, dict):
            continue
        package = PurePosixPath(_rel(state.root, path)).parent.as_posix()
        package = "" if package == "." else package
        for key in ("main", "module", "browser", "bin", "exports"):
            for declared in _manifest_paths(manifest.get(key)):
                candidate = posixpath.normpath(posixpath.join(package, declared))
                if candidate.startswith("..") or _URL_SCHEME.match(declared):
                    continue
                options = [candidate, *(candidate + suffix for suffix in _JS_SOURCE_SUFFIXES),
                           *(f"{candidate}/index{suffix}" for suffix in _JS_SOURCE_SUFFIXES)]
                if found := next((option for option in options if option in scanned), None):
                    entries.add(found)
        dependencies = {name for field in ("dependencies", "devDependencies")
                        if isinstance(manifest.get(field), dict) for name in manifest[field]}
        if "expo-router" in dependencies or str(manifest.get("main", "")).startswith("expo-router"):
            directories.append(f"{package}/app/" if package else "app/")
    return entries, tuple(directories)


def _serves(called: list[str], handler: Node, *, open_tail: bool, loose: bool, skip: int = 0) -> bool:
    """Whether `handler`'s route can serve a call with these path segments.

    A dynamic handler segment serves any literal; the reverse is a different route
    (`/items/{id}` is not `/items/export`) unless `loose`, where a whole runtime segment in
    the call may be any literal the handler declares. An `open_tail` call ends in a runtime
    value joined to its last segment, so the handler may extend that segment with an
    extension (`export.` serves `export.xlsx`) or add segments after it. `skip` drops that
    many leading handler segments: the path a configured origin may carry."""
    served = _route_segments(str(handler.metadata.get("route", "")))[skip:]

    def same(call: str, declared: str) -> bool:
        return call == declared or declared == "{dynamic}" or (loose and call == "{dynamic}")

    if handler.metadata.get("catch_all") and served and served[-1] == "{dynamic}":
        prefix = served[:-1]
        if len(called) > len(prefix) or (open_tail and len(called) == len(prefix)):
            return all(same(c, h) for c, h in zip(called, prefix, strict=False))
        return False
    if not open_tail:
        return len(called) == len(served) and all(same(c, h) for c, h in zip(called, served, strict=True))
    if not called or len(served) < len(called):
        return False
    *head, last = called
    tail = served[len(called) - 1]
    extends = tail.startswith(last) and (last.endswith(".") or tail[len(last):].startswith("."))
    return all(same(c, h) for c, h in zip(head, served, strict=False)) and (same(last, tail) or extends)


def _methods_serving(node: Node, handlers: list[Node]) -> list[str]:
    """Other methods served on the route shape `node` calls: a PATCH sent to a GET-only route."""
    route = node.metadata.get("route")
    if not isinstance(route, str) or node.metadata.get("open_tail"):
        return []
    called = _route_segments(route)
    methods = set()
    for handler in handlers:
        # As in _match_endpoints: a dynamic handler segment serves any value, a literal one only
        # itself, and a catch-all serves everything under its prefix.
        if _serves(called, handler, open_tail=False, loose=False):
            methods.add(str(handler.metadata.get("method") or ""))
    return sorted(methods - {"", "UNKNOWN", str(node.metadata.get("method"))})


def _match_endpoints(state: ScanState) -> None:
    """Link client calls to handlers whose route shape serves them without being the
    same normalized endpoint: a literal `/items/42` served by `/items/{id}`, a call whose
    method is hidden in its options, a Pages Router `ANY` handler, a catch-all route.

    Every such edge is `probable`: the shapes agree, the runtime dispatch is unverified."""
    graph = state.graph
    handled: dict[str, Node] = {}
    for edge in graph.edges:
        if edge.kind in {"HANDLES_API", "IMPLEMENTED_BY"} and graph.nodes[edge.source].kind == "endpoint":
            handled[edge.source] = graph.nodes[edge.source]
    callers: dict[str, list[Edge]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "CALLS_API":
            callers[edge.target].append(edge)
    for endpoint_id, edges in sorted(callers.items()):
        if endpoint_id in handled:
            continue
        node = graph.nodes[endpoint_id]
        method, route = node.metadata.get("method", ""), node.metadata.get("route")
        if not isinstance(route, str):
            continue
        called = _route_segments(route)
        open_tail = bool(node.metadata.get("open_tail"))
        candidates = [handler for handler in handled.values()
                      if handler.metadata.get("method") == "ANY" or handler.metadata.get("method") == method
                      or (method == "UNKNOWN" and handler.metadata.get("method"))
                      # Frameworks (Next.js, Express, FastAPI/Starlette) answer HEAD with the GET handler.
                      or (method == "HEAD" and handler.metadata.get("method") == "GET")]
        matches = [handler for handler in candidates if _serves(called, handler, open_tail=open_tail, loose=False)]
        reason, loose = "matched by route shape to", False
        if not matches and "{dynamic}" in called:
            # `/items/${id}/${action}` sent to `/items/{id}/approve`: a runtime segment can hold
            # any literal a handler declares. Tried only when no handler has the call's shape.
            matches = [handler for handler in candidates if _serves(called, handler, open_tail=open_tail, loose=True)]
            reason, loose = "matched, through a runtime segment, to", True
        suffix = False
        if not matches and node.metadata.get("open_head") and any(segment != "{dynamic}" for segment in called):
            # `${process.env.API_URL}/items`: the origin may carry a path (`https://host/api`),
            # so a handler whose route ENDS with the call's segments may serve it. Only a call
            # with a literal segment is matched this way, and never better than `ambiguous`.
            matches = [handler for handler in candidates
                       if any(_serves(called, handler, open_tail=open_tail, loose=False, skip=skip)
                              for skip in range(1, len(_route_segments(str(handler.metadata.get("route", ""))))))]
            reason, loose, suffix = "matched, after the path a configured origin may carry, to", True, True
        if not matches:
            continue
        cap = state.config.max_ambiguous_targets
        widened = loose or open_tail
        if widened and len(matches) > cap:
            # `/api${path}` in a request wrapper: every handler under /api fits, so a link to
            # any of them says nothing. The call is reported, not linked and not a gap.
            node.metadata["matched_handler_count"] = len(matches)
            for site in sorted({edge.evidence for edge in edges}):
                graph.issues.append(Issue(
                    "DYNAMIC_HTTP_REQUEST", "info",
                    f"{node.label} could be served by {len(matches)} handlers, more than max_ambiguous_targets "
                    f"({cap}); the URL is too open to link.",
                    sorted({edge.source for edge in edges if edge.evidence == site}), site,
                    "Spell more of the path at the call site, or declare the API mapping."))
            continue
        node.metadata["matched_handlers"] = sorted(handler.id for handler in matches)
        resolution = "ambiguous" if suffix or (widened and len(matches) > 1) else "probable"
        for handler in matches:
            for edge in edges:
                graph.add_edge(Edge(edge.source, handler.id, "CALLS_API", resolution, edge.evidence, origin=edge.origin,
                                    detail=f"{node.label} {reason} {handler.label}; runtime dispatch unverified"))


def _scan_javascript(state: ScanState, path: Path, text: str, file_node: str) -> None:
    rel = _rel(state.root, path)
    ranges: list[tuple[int, str]] = []
    for match in JS_FUNCTION_RE.finditer(text):
        name = match.group(1) or match.group(2)
        line = text.count("\n", 0, match.start()) + 1
        node_id = _symbol_id(rel, name)
        state.graph.add_node(Node(
            node_id, "symbol", name, path=rel, line=line,
            language="typescript" if path.suffix in {".ts", ".tsx"} else "javascript",
            metadata={"qualified_name": name, "symbol_kind": "function"},
        ))
        state.graph.add_edge(Edge(file_node, node_id, "CONTAINS", "probable", "javascript_regex", origin="regex"))
        state.definitions[name].append(node_id)
        ranges.append((match.start(), node_id))

    def source_for(offset: int) -> str:
        candidates = [item for item in ranges if item[0] <= offset]
        return candidates[-1][1] if candidates else file_node

    for match in JS_CALL_RE.finditer(text):
        name = match.group(1)
        if name in JS_KEYWORDS:
            continue
        state.pending_calls.append(PendingCall(
            source_for(match.start()), name,
            f"{rel}:{text.count(chr(10), 0, match.start()) + 1}",
            "typescript" if path.suffix in {".ts", ".tsx"} else "javascript",
        ))

    for match in API_CALL_RE.finditer(text):
        if match.group(1):
            method, route = match.group(1).upper(), match.group(3)
        else:
            method, route = "GET", match.group(5)
        route = normalise_route(route)
        endpoint = _endpoint_id(method, route)
        state.graph.add_node(Node(
            endpoint, "endpoint", f"{method} {route}", path=rel,
            line=text.count("\n", 0, match.start()) + 1,
            metadata={"method": method, "route": route, "dynamic": "{dynamic}" in route},
        ))
        state.graph.add_edge(Edge(
            source_for(match.start()), endpoint, "CALLS_API",
            "probable" if "{dynamic}" in route else "exact", "javascript_regex",
            origin="regex", detail="dynamic route template" if "{dynamic}" in route else None,
        ))


def _resolve_import(root: Path, source: Path, import_path: str) -> Path | None:
    if not import_path.startswith("."):
        return None
    base = (source.parent / import_path).resolve()
    candidates = [base]
    for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".py"):
        candidates.append(Path(str(base) + suffix))
    for suffix in (".ts", ".tsx", ".js", ".jsx", ".py"):
        candidates.append(base / f"index{suffix}")
    for candidate in candidates:
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


_GENERIC_SQL_TABLE_RE = re.compile(
    r'\b(FROM|JOIN|INTO|UPDATE)\s+"?([a-z_][a-z0-9_]*)"?(?:\."?([a-z_][a-z0-9_]*)"?)?(?![\w."]|\s*\))(\s*\()?')
_SQL_NOT_TABLES = frozenset({"set", "lateral", "only", "select", "values", "skip", "nowait", "of"})


def _vocabulary(config: Config) -> tuple[re.Pattern | None, ...]:
    """(PostgreSQL table, document collection, role, toggle) patterns from config."""
    return _compile_vocabulary(tuple(config.pg_schemas), config.mongo_receiver,
                               tuple(config.roles), tuple(config.toggle_calls))


@lru_cache(maxsize=8)
def _compile_vocabulary(pg_schemas: tuple[str, ...], mongo_receiver: str, roles: tuple[str, ...],
                        toggle_calls: tuple[str, ...]) -> tuple[re.Pattern | None, ...]:
    """Compiled once per vocabulary, not once per file. A pattern with nothing to
    match is None, never an empty alternation, which would match everywhere."""
    def words(items: tuple[str, ...]) -> str:
        return "|".join(re.escape(item) for item in items)

    return (
        re.compile(rf"\b({words(pg_schemas)})\.([a-z_][a-z0-9_]*)\b") if pg_schemas else None,
        re.compile(rf"\b{re.escape(mongo_receiver)}\.([a-zA-Z_][a-zA-Z0-9_]*)\.({'|'.join(sorted(MONGO_READ_METHODS | MONGO_WRITE_METHODS))})\s*\(")
        if mongo_receiver else None,
        re.compile(rf"\b({words(roles)})\b") if roles else None,
        re.compile(rf"(?:{'|'.join(toggle_calls)})[(\s'\"]+([a-z][a-z0-9_.-]+)") if toggle_calls else None,
    )


#: Callables whose string arguments name code, not data: `patch("routers.billing.db")`,
#: `patch.object(...)`, `monkeypatch.setattr("app.billing.x", ...)`, `import_module(...)`.
_CODE_TARGET_CALLS = frozenset({"patch", "object", "multiple", "setattr", "delattr", "import_module",
                                "__import__", "reload", "find_spec", "resolve_name"})
_DOTTED_PATH_RE = re.compile(r"\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\s*")
#: Where SQL puts a table name. ON counts only after INDEX/TRIGGER/POLICY/GRANT/REVOKE.
_TABLE_KEYWORD_BEFORE_RE = re.compile(
    r"(?<![\w.$])(FROM|JOIN|INTO|UPDATE|TABLE|REFERENCES|TRUNCATE|COPY|VIEW|ON)"
    r"(?:\s+ONLY|\s+IF\s+(?:NOT\s+)?EXISTS)?\s+$", re.I)
_ON_OBJECT_RE = re.compile(r"\b(?:INDEX|TRIGGER|POLICY|GRANT|REVOKE|RULE)\b", re.I)
_UPDATE_SET_RE = re.compile(r"(?:\s+(?:AS\s+)?[A-Za-z_]\w*)?\s+SET\b", re.I)
_TABLE_LIST_GAP_RE = re.compile(r"(?:\s+(?:AS\s+)?[A-Za-z_]\w*)?\s*,\s*", re.I)
#: A clause keyword written the way SQL is written, in capitals. Case-sensitive on purpose.
_SQL_UPPER_KEYWORD_RE = re.compile(
    r"(?<![\w.$])(?:SELECT|INSERT|UPDATE|DELETE|MERGE|FROM|JOIN|WHERE|INTO|VALUES|SET|RETURNING|"
    r"GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|HAVING|UNION|CREATE|ALTER|DROP|TRUNCATE|TABLE|"
    r"ON\s+CONFLICT|COPY|VIEW)(?![\w.$])")
#: String literals in C-like source (JS/TS and similar). Comments are alternatives too, and
#: the leftmost match wins, so the apostrophe in `// don't read from x` never opens a
#: string. Quoted strings end at the line; a template literal may span lines.
_C_LIKE_LITERAL_RE = re.compile(
    r"//[^\n]*|/\*.*?\*/|`((?:\\.|[^`\\])*)`|'((?:\\.|[^'\\\n])*)'|\"((?:\\.|[^\"\\\n])*)\"", re.S)


def _sql_shaped(value: str) -> bool:
    """Does a string read as SQL beyond the one keyword in front of a table? It does when
    it opens like a statement (`looks_like_sql`: `select id from x`, `update x set`) or
    carries a capitalised clause keyword (`" update x set total = 0 WHERE "`). Prose
    ("Importing rows from billing.staging", "Copy billing.summary") does neither."""
    from .postgres import looks_like_sql
    return bool(_SQL_UPPER_KEYWORD_RE.search(value)) or looks_like_sql(value)


def _python_strings(state: ScanState, rel: str, *, sql_only: bool = False) -> list[tuple[str, int]]:
    """String constants in a parsed Python module, docstrings excluded: prose that says
    "reads rows FROM staging INTO the cache" is documentation, not a query.

    With `sql_only`, strings that name code or keys are left out too: patch, setattr and
    import targets, dict keys, subscripts, and any string that is only a dotted path
    (`"billing.summary"` as a route key is not a query)."""
    tree = state.python_trees.get(rel)
    if tree is None:
        return []
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if not sql_only:
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _CODE_TARGET_CALLS:
                skip.update(id(arg) for arg in (*node.args, *(kw.value for kw in node.keywords)))
        elif isinstance(node, ast.Dict):
            skip.update(id(key) for key in node.keys if key is not None)
        elif isinstance(node, ast.Subscript):
            skip.add(id(node.slice))
    return [(node.value, node.lineno) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
            and not (sql_only and _DOTTED_PATH_RE.fullmatch(node.value))]


def _in_sql_table_position(text: str, start: int, end: int, previous_end: int | None,
                           sql_shaped: Callable[[], bool] | None = None) -> bool:
    """Is `text[start:end]` where SQL names a table? Straight after FROM, JOIN, INTO,
    UPDATE … SET, TABLE, REFERENCES, TRUNCATE, COPY or VIEW (ONLY / IF [NOT] EXISTS
    allowed between), after ON in an INDEX/TRIGGER/POLICY/GRANT statement, or next in a
    comma list after a name that was (`previous_end`).

    `routers.billing.db` (part of a longer dotted path), `billing.fn(` (a call) and
    `from billing.jobs import x` (Python) never are. A schema name alone is not SQL
    context: the configured schema is often also a module name. `looks_like_sql` alone
    does not decide it, because it only reads a statement's opening, and a query is
    often built from fragments (`" FROM billing.invoices WHERE "`).

    `sql_shaped` is None when `text` is known SQL (a .sql file). Otherwise a keyword that
    is not in capitals counts only when `sql_shaped()` says the whole string reads as
    SQL: "Importing rows from billing.staging" and "Open the view billing.dashboard"
    are sentences."""
    if start and text[start - 1] == ".":
        return False
    after = text[end:end + 80]
    if re.match(r"\s+import\b", after):
        return False
    # After FROM/JOIN/UPDATE/TRUNCATE a parenthesis makes it a function; after INTO, ON,
    # TABLE, REFERENCES, COPY or VIEW it opens a column list.
    call = bool(re.match(r"\s*\(", after))
    before = text[max(0, start - 120):start]
    if keyword := _TABLE_KEYWORD_BEFORE_RE.search(before):
        if sql_shaped is not None and not keyword.group(1).isupper() and not sql_shaped():
            return False
        word = keyword.group(1).upper()
        if call and word in {"FROM", "JOIN", "UPDATE", "TRUNCATE"}:
            return False
        if word == "ON":
            return bool(_ON_OBJECT_RE.search(before))
        if word == "UPDATE":
            return bool(_UPDATE_SET_RE.match(after))
        return True
    return not call and previous_end is not None and bool(_TABLE_LIST_GAP_RE.fullmatch(text, previous_end, start))


def _schema_references(state: ScanState, rel: str, suffix: str, text: str, pattern: re.Pattern) -> list[tuple[str, str]]:
    """`<schema>.<table>` for a configured schema, only where SQL can be (a .sql file or a
    string literal) and only in a table position (`_in_sql_table_position`). `app.state`
    in code, `app.config` in a guide and `patch("routers.billing.db")` are not tables."""
    if suffix in {".md", ".mdx", ".html", ".json", ".yaml", ".yml", ".txt", ".sh"}:
        return []
    found: list[tuple[str, str]] = []

    def collect(value: str, where: Callable[[int], str], known_sql: bool) -> None:
        shaped: list[bool] = []

        def sql_shaped() -> bool:  # computed once per string, and only when asked
            if not shaped:
                shaped.append(_sql_shaped(value))
            return shaped[0]

        previous = None
        for m in pattern.finditer(value):
            in_table = _in_sql_table_position(value, m.start(), m.end(), previous,
                                              None if known_sql else sql_shaped)
            previous = m.end() if in_table else None
            if in_table:
                found.append((f"{m.group(1)}.{m.group(2)}", where(m.start())))

    if suffix == ".py":
        for value, line in _python_strings(state, rel, sql_only=True):
            collect(value, lambda pos, value=value, line=line: f"{rel}:{line + value.count(chr(10), 0, pos)}", False)
    elif suffix == ".sql":
        collect(text, lambda pos: f"{rel}:{text.count(chr(10), 0, pos) + 1}", True)
    elif pattern.search(text):
        # Only inside string literals; comments are skipped by the literal pattern itself.
        for literal in _C_LIKE_LITERAL_RE.finditer(text):
            group = next((g for g in (1, 2, 3) if literal.group(g) is not None), None)
            if group is None:
                continue
            offset = literal.start(group)
            collect(literal.group(group),
                    lambda pos, offset=offset: f"{rel}:{text.count(chr(10), 0, offset + pos) + 1}", False)
    return found


def _add_regex_store_edge(graph: Graph, source: str, kind: str, name: str, evidence: str,
                          detail: str | None = None) -> None:
    """A store seen only by a pattern: always `probable`, and said to be unverified. A store
    first created here is flagged `unverified`; `render.mark_unverified_stores` settles the
    flag after the scan, since a later file can confirm it with a parsed statement."""
    new = stable_id(kind, name) not in graph.nodes
    note = "unverified (regex match only)"
    _add_store_edge(graph, source, kind, name, "probable", evidence, origin="regex",
                    detail=f"{detail}; {note}" if detail else note,
                    metadata={"unverified": True} if new else None)


def _link_test_store_references(state: ScanState) -> None:
    """Pattern matches in test code link to stores that other evidence already put in the
    graph. On their own they create none: a test or fixture names sample tables, and a
    vendored tool's tests name whatever tables its own fixtures use."""
    for source, kind, name, evidence, detail in state.test_store_references:
        if stable_id(kind, name) in state.graph.nodes:
            _add_regex_store_edge(state.graph, source, kind, name, evidence, detail)


def _scan_generic(state: ScanState, path: Path, text: str, file_node: str) -> None:
    rel = _rel(state.root, path)
    suffix = path.suffix.lower()
    regex_javascript = suffix in javascript.EXTENSIONS and not javascript.available()
    for match in IMPORT_RE.finditer(text) if regex_javascript else ():
        target = _resolve_import(state.root, path, match.group(1))
        if target:
            target_rel = _rel(state.root, target)
            target_id = _file_id(target_rel)
            state.graph.add_node(Node(target_id, "file", target_rel, path=target_rel))
            state.graph.add_edge(Edge(file_node, target_id, "IMPORTS", "exact", f"{rel}:{text.count(chr(10), 0, match.start()) + 1}", origin="syntax"))

    tags = FEATURE_RE.findall(text)
    for tag in sorted(set(tags)):
        tag_id = stable_id("concept", f"featuretrace:{tag}")
        state.graph.add_node(Node(
            tag_id, "concept", tag, metadata={"source": "featuretrace", "concept_id": tag}
        ))
        state.graph.add_edge(Edge(file_node, tag_id, "DECLARES_CONCEPT", "declared", "featuretrace_marker", origin="declared"))

    pg_table_re, mongo_re, role_re, toggle_re = _vocabulary(state.config)
    if is_test_path(rel):
        # A pattern match in test code or a fixture (a vendored tool's own tests, sample SQL)
        # links only to a store other evidence declares; `_link_test_store_references`.
        def add_store(graph, source, kind, name, evidence, detail=None):
            state.test_store_references.append((source, kind, name, evidence, detail))
    else:
        add_store = _add_regex_store_edge
    for table, evidence in _schema_references(state, rel, suffix, text, pg_table_re) if pg_table_re else ():
        add_store(state.graph, file_node, "postgres_table", table, evidence,
                  "Configured PostgreSQL schema name after a SQL table keyword")
    if suffix == ".py" and not _sql_parser_available() and not pg_table_re:
        # No SQL parser and no schema list: fall back to SQL-shaped string literals.
        # Keywords must be upper case, so a Python `from x import y` never reads as a table.
        for value, line in _python_strings(state, rel):
            if not re.match(r"\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b", value):
                continue
            for match in _GENERIC_SQL_TABLE_RE.finditer(value):
                keyword, first, second, paren = match.groups()
                # A parenthesis opens INSERT's column list, and is a function call anywhere else.
                if first in _SQL_NOT_TABLES or (paren and keyword != "INTO"):
                    continue
                table = f"{first}.{second}" if second else first
                add_store(state.graph, file_node, "postgres_table", table,
                          f"{rel}:{line + value.count(chr(10), 0, match.start())}")
    if regex_javascript and mongo_re:
        # Tree-sitter extraction handles collections when it is installed; this fallback
        # still requires a driver method, so `db.connect()` is never a collection, and the
        # same MongoDB handle evidence (`_mongo_handle`) as the syntax path, read lexically.
        mongo_file = _mongo_handle(state, rel, state.config.mongo_receiver, {rel: text})
        for match in mongo_re.finditer(text) if mongo_file else ():
            if match.group(1) in MONGO_NOT_COLLECTIONS:
                continue
            add_store(state.graph, file_node, "mongo_collection", match.group(1),
                      f"{rel}:{text.count(chr(10), 0, match.start()) + 1}",
                      f"MongoDB driver call ({mongo_operation(match.group(2))})")

    for role in sorted(set(role_re.findall(text))) if role_re else ():
        role_id = stable_id("policy", f"role:{role}")
        state.graph.add_node(Node(role_id, "policy", role, metadata={"policy_kind": "role"}))
        state.graph.add_edge(Edge(file_node, role_id, "GUARDED_BY", "ambiguous", "literal_role_reference", origin="heuristic"))
    for toggle in sorted(set(toggle_re.findall(text))) if toggle_re else ():
        toggle_id = stable_id("policy", f"toggle:{toggle}")
        state.graph.add_node(Node(toggle_id, "policy", toggle, metadata={"policy_kind": "feature_toggle"}))
        state.graph.add_edge(Edge(file_node, toggle_id, "GUARDED_BY", "probable", "toggle_reference", origin="regex"))

    if regex_javascript and (rel.endswith("/page.tsx") or rel.endswith("/page.jsx")):
        # Syntax extraction registers App and Pages Router routes; this is the fallback.
        parts = rel.split("/")
        try:
            app_index = max(index for index, part in enumerate(parts[:-1]) if part == "app")
            route_parts = [p for p in parts[app_index + 1:-1] if not (p.startswith("(") and p.endswith(")"))]
            route = "/" + "/".join(route_parts)
            page_id = stable_id("page", route)
            state.graph.add_node(Node(page_id, "page", route, path=rel,
                                      metadata={"route": route, **route_metadata(state.graph, page_id, route)}))
            state.graph.add_edge(Edge(page_id, file_node, "IMPLEMENTED_BY", "exact", "next_app_router_path", origin="framework_path"))
        except ValueError:
            pass

    _scan_related_fields(state, rel, text, file_node)


def _scan_related_fields(state: ScanState, rel: str, text: str, file_node: str) -> None:
    lines = text.splitlines()
    collecting = False
    for index, line in enumerate(lines, start=1):
        match = RELATED_FIELD_RE.match(line)
        if match:
            collecting = True
            value = match.group(1).strip()
        elif collecting:
            cont = RELATED_CONT_RE.match(line)
            if not cont:
                collecting = False
                continue
            value = cont.group(1).strip()
        else:
            continue
        token = value.split()[0].rstrip(",;)") if value else ""
        if not token or "/" not in token:
            continue
        target_path = token.replace("`", "")
        if target_path in state.admitted_paths:
            target = _file_id(target_path)
            state.graph.add_node(Node(target, "file", target_path, path=target_path))
            state.graph.add_edge(Edge(file_node, target, "RELATED_TO", "declared", f"{rel}:{index}", origin="declared"))
        else:
            state.graph.issues.append(Issue(
                "DANGLING_RELATED", "warning", f"Related target does not exist: {target_path}",
                [file_node], f"{rel}:{index}", "Correct the path or remove the stale relationship.",
            ))


def _listish(raw: dict, key: str) -> list:
    """`raw.get(key, [])` does NOT default a key that exists holding `None`.

    This is footgun #14 in the repository these adapters were written to read, and
    `docs/architecture/canonical_owners.json` is full of it: 58 of its 65 entries
    serialise `consumers: null` (the field is optional in the source YAML, and the
    generator emits the key regardless). The result was a hard TypeError partway
    through `_load_canonical_owners` — after the AST scan had completed — so the whole
    run died with no output at the one step that reads declared policy.

    An absent key and a null key mean the same thing here: nothing declared.
    """
    value = raw.get(key)
    return value if isinstance(value, (list, tuple)) else []


def _load_canonical_owners(state: ScanState) -> None:
    configured = state.config.canonical_owners_json
    if not configured:
        return
    configured = _artifact_rel(state, configured)
    if not configured:
        state.graph.issues.append(Issue(
            "ARTIFACT_PATH_OUTSIDE_ROOT", "warning",
            "Canonical-owner artifact path is not repository-relative.", [], "",
            "Use a relative path inside the repository or disable the artifact.",
        ))
        return
    path = state.root / configured
    if not path.is_file():
        return
    source = read_source(state.root, path, state.config.max_file_bytes)
    if source.text is None:
        state.graph.issues.append(Issue(
            "BAD_CANONICAL_OWNER_ARTIFACT", "warning",
            f"Cannot read {configured}: {source.status}", [], configured,
            "Regenerate or correct the canonical-owner JSON artifact.",
        ))
        return
    try:
        payload = json.loads(source.text)
    except json.JSONDecodeError as exc:
        state.graph.issues.append(Issue(
            "BAD_CANONICAL_OWNER_ARTIFACT", "warning", f"Cannot parse {configured}: {exc}",
            [], configured, "Regenerate or correct the canonical-owner JSON artifact.",
        ))
        return
    for raw in payload.get("concepts", []):
        concept = str(raw.get("concept", "")).strip()
        owner = _artifact_rel(state, raw.get("owner"))
        if not concept or not owner:
            continue
        concept_id = stable_id("concept", f"canonical:{concept}")
        state.graph.add_node(Node(
            concept_id, "concept", concept,
            metadata={
                "source": "canonical_owners", "concept_id": concept,
                "rule": raw.get("rule"), "why": raw.get("why"),
                "symbols": _listish(raw, "symbols"),
            },
        ))
        owner_id = _file_id(owner)
        state.graph.add_node(Node(owner_id, "file", owner, path=owner))
        state.graph.add_edge(Edge(concept_id, owner_id, "OWNS", "declared", configured, origin="policy"))
        for symbol in _listish(raw, "symbols"):
            candidates = state.definitions.get(str(symbol), [])
            for candidate in candidates:
                if state.graph.nodes[candidate].path == owner:
                    state.graph.add_edge(Edge(concept_id, candidate, "OWNED_SYMBOL", "exact", configured, origin="policy+ast"))
        for test_path in _listish(raw, "tests"):
            test_path = _artifact_rel(state, test_path)
            if not test_path:
                continue
            test_id = _file_id(test_path)
            state.graph.add_node(Node(test_id, "file", test_path, path=test_path))
            state.graph.add_edge(Edge(concept_id, test_id, "VERIFIED_BY", "declared", configured, origin="policy"))
        for consumer in _listish(raw, "consumers"):
            consumer_path = consumer if isinstance(consumer, str) else consumer.get("path") if isinstance(consumer, dict) else None
            consumer_path = _artifact_rel(state, consumer_path)
            if not consumer_path:
                continue
            consumer_id = _file_id(consumer_path)
            state.graph.add_node(Node(consumer_id, "file", consumer_path, path=consumer_path))
            state.graph.add_edge(Edge(
                consumer_id, concept_id, "CONSUMES", "declared", configured,
                origin="policy", detail=consumer.get("relationship") if isinstance(consumer, dict) else None,
            ))
        for violation in _listish(raw, "found_violations"):
            state.graph.issues.append(Issue(
                "CANONICAL_OWNER_VIOLATION", "error",
                f"{concept} has a recorded non-owner implementation: {violation}",
                [concept_id], configured,
                "Repoint the implementation to the canonical owner or document a genuine distinct concept.",
            ))


def _load_router_datastore_map(state: ScanState) -> None:
    configured = state.config.router_datastore_json
    if not configured:
        return
    configured = _artifact_rel(state, configured)
    if not configured:
        state.graph.issues.append(Issue(
            "ARTIFACT_PATH_OUTSIDE_ROOT", "warning",
            "Router/datastore artifact path is not repository-relative.", [], "",
            "Use a relative path inside the repository or disable the artifact.",
        ))
        return
    path = state.root / configured
    if not path.is_file():
        return
    source = read_source(state.root, path, state.config.max_file_bytes)
    if source.text is None:
        state.graph.issues.append(Issue(
            "BAD_ROUTER_DATASTORE_ARTIFACT", "warning",
            f"Cannot read {configured}: {source.status}", [], configured,
            "Regenerate or correct the router/datastore JSON artifact.",
        ))
        return
    try:
        payload = json.loads(source.text)
    except json.JSONDecodeError as exc:
        state.graph.issues.append(Issue(
            "BAD_ROUTER_DATASTORE_ARTIFACT", "warning", f"Cannot parse {configured}: {exc}",
            [], configured, "Regenerate or correct the router/datastore JSON artifact.",
        ))
        return
    pattern_only, unconfirmed = unverified_stores(state.graph), set()
    try:
        _load_routers(state, payload, configured, pattern_only, unconfirmed)
    finally:
        if unconfirmed:
            shown = ", ".join(sorted(unconfirmed)[:10]) + (", …" if len(unconfirmed) > 10 else "")
            state.graph.issues.append(Issue(
                "ARTIFACT_STORE_UNCONFIRMED", "info",
                f"{len(unconfirmed)} unvalidated PostgreSQL name(s) in {configured} match no table the scan found "
                f"and were not added: {shown}.",
                [], configured, "Regenerate the artifact against the database catalog, or ignore names its "
                "generator could not validate."))


def _load_routers(state: ScanState, payload: dict, configured: str, pattern_only: set[str], unconfirmed: set[str]) -> None:
    for router in payload.get("routers", []):
        router_path = _artifact_rel(state, router.get("file"))
        if not router_path:
            continue
        router_id = _file_id(router_path)
        state.graph.add_node(Node(
            router_id, "file", router_path, path=router_path,
            metadata={"wired": router.get("wired"), "classification": router.get("classification")},
        ))
        for route in router.get("routes", []):
            method = str(route.get("method", "GET")).upper()
            raw_path = str(route.get("path", "/"))
            endpoint_path = normalise_route(with_api_prefix(state, raw_path))
            endpoint_id = _endpoint_id(method, endpoint_path)
            state.graph.add_node(Node(
                endpoint_id, "endpoint", f"{method} {endpoint_path}", path=router_path,
                metadata={"method": method, "route": endpoint_path, "wired": router.get("wired"),
                          **route_metadata(state.graph, endpoint_id, with_api_prefix(state, raw_path))},
            ))
            state.graph.add_edge(Edge(
                endpoint_id, router_id, "IMPLEMENTED_BY", "declared", configured,
                origin="generated_static_artifact",
            ))
        for field, relationship in (("mongo_reads", "READS_STORE"), ("mongo_writes", "WRITES_STORE")):
            value = router.get(field, {})
            names = value.keys() if isinstance(value, dict) else value or []
            for name in names:
                store_id = stable_id("mongo_collection", str(name))
                state.graph.add_node(Node(store_id, "mongo_collection", str(name), metadata={"store": str(name)}))
                state.graph.add_edge(Edge(
                    router_id, store_id, relationship, "declared", configured,
                    origin="generated_static_artifact",
                ))
        for name in router.get("postgres_tables", []):
            store_id = stable_id("postgres_table", str(name))
            state.graph.add_node(Node(store_id, "postgres_table", str(name), metadata={"store": str(name)}))
            state.graph.add_edge(Edge(
                router_id, store_id, "TOUCHES_STORE", "declared", configured,
                origin="generated_static_artifact",
            ))
        for name in router.get("postgres_unverified_refs", []):
            # The generator could not validate these names (`schema.view`, `schema.fact_`), so a
            # name links only to a table this scan found by other than a pattern match.
            store_id = stable_id("postgres_table", str(name))
            known = state.graph.nodes.get(store_id)
            if known is None or known.kind != "postgres_table" or store_id in pattern_only:
                unconfirmed.add(str(name))
                continue
            state.graph.add_edge(Edge(
                router_id, store_id, "TOUCHES_STORE", "ambiguous", configured,
                origin="generated_static_artifact", detail="PostgreSQL name not validated against a live catalog",
            ))
        if router.get("wired") is False:
            state.graph.issues.append(Issue(
                "UNWIRED_ROUTER", "warning", f"Router is declared but not wired: {router_path}",
                [router_id], configured,
                "Confirm whether the router is intentionally dormant or missing from application registration.",
            ))


_MODULE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".mjs")


def _es_module(state: ScanState, path: str) -> bool:
    """Is `path` a JS/TS module (own scope), not a classic script sharing the global one?"""
    return path.endswith(_MODULE_SUFFIXES) or bool(state.imports.get(path) or state.js_exports.get(path))


def _resolve_calls(state: ScanState) -> None:
    graph = state.graph
    labels: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    defaults: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes.values():
        if node.kind == "symbol" and node.path:
            labels[node.path][node.label].append(node.id)
            if node.metadata.get("default_export"):
                defaults[node.path].append(node.id)

    def lookup(path: str, name: str, depth: int = 0) -> list[str]:
        """Symbols a dotted `name` denotes in module `path`: a qualified symbol, a public
        export alias, a barrel `export ... from`, or a Python package re-export."""
        if depth > 6 or path.startswith("external:") or not name:
            return []
        if name == "default" and defaults.get(path):
            return defaults[path]
        if found := labels[path].get(name):
            return found
        head, _, rest = name.partition(".")
        suffix = f".{rest}" if rest else ""
        local = state.js_exports.get(path, {}).get(head)
        if local and local != head and (found := lookup(path, local + suffix, depth + 1)):
            return found
        for target, public, imported in state.js_reexports.get(path, []):
            if public == head:
                inner = imported + suffix if imported != "*" else rest
                found = lookup(target, inner, depth + 1)
            elif public == "*" and head != "default":
                found = lookup(target, name, depth + 1)
            else:
                continue
            if found:
                return found
        binding = state.imports.get(path, {}).get(head)
        if binding and path.endswith(".py"):
            target, exported = binding
            inner = rest if exported == "*" else exported + suffix
            if found := lookup(target, inner, depth + 1):
                return found
        # `ItemService.build` with no such method in the file (inherited, or assigned at
        # runtime): the class itself is the closest static target.
        return labels[path].get(head, []) if rest and depth == 0 else []

    grouped: dict[tuple[str, str, str], list[PendingCall]] = defaultdict(list)
    for call in state.pending_calls:
        grouped[(call.source, call.name, call.relationship)].append(call)
    for (source, name, relationship), calls in grouped.items():
        source_node = graph.nodes.get(source)
        if source_node is None:
            continue
        language = calls[0].language
        parts = name.split(".")
        bindings = state.imports.get(source_node.path or "", {})
        binding, remainder = None, []
        for size in range(len(parts), 0, -1):
            if (key := ".".join(parts[:size])) in bindings:
                binding, remainder = bindings[key], parts[size:]
                break
        targets: list[str] = []
        resolution, origin = "probable", "name_resolution"
        detail = "Name-only candidate; imports and object types unverified"
        if binding:
            path, exported = binding
            if path.startswith("external:"):
                continue  # a package or the standard library: nothing in this repository
            inner = (".".join(remainder) or "default") if exported == "*" else ".".join([exported, *remainder])
            targets = sorted(set(lookup(path, inner)))
            if targets:
                # Import-bound syntax, not type or runtime resolution.
                resolution, origin, detail = "high", "import_binding", "Import-bound syntax; runtime dispatch unverified"
        if not targets and len(parts) == 2 and parts[0] in {"self", "cls", "this"} and source_node.kind == "symbol":
            scope = source_node.label.split(".")[:-1]
            while scope and not targets:
                targets = labels[source_node.path or ""].get(".".join([*scope, parts[1]]), [])
                scope = scope[:-1]
            if targets:
                detail = "Method of the enclosing class; subclass overrides and runtime binding unverified"
        if not targets:
            if language != "python" and len(parts) > 1 and parts[0] in _JS_GLOBALS:
                continue
            targets = sorted(set(state.definitions.get(parts[-1], [])))
            # Avoid cross-language name joins (e.g. a Python save and a TS save).
            targets = [target for target in targets
                       if (graph.nodes[target].language == "python") == (language == "python")]
            if parts[0] in {"self", "cls", "this"}:
                targets = [target for target in targets if "." in graph.nodes[target].label]
            # A local lexical definition outranks a repository-wide name candidate.
            local = [target for target in targets if graph.nodes[target].path == source_node.path]
            if local and len(parts) == 1:
                targets = local
            elif len(parts) == 1 and language != "python" and _es_module(state, source_node.path or ""):
                # An ES module has no scope shared with other files: a bare name neither
                # imported nor defined here is a global (`fetch`, `setTimeout`) or a
                # parameter (`resolve`), never another module's function.
                continue
        targets = [target for target in targets if target != source]
        if not targets or len(targets) > state.config.max_ambiguous_targets:
            continue
        # A unique name in the index says nothing about an object's runtime type.
        # It is a candidate even when only one definition happens to be present.
        resolution = resolution if len(targets) == 1 else "ambiguous"
        evidence = calls[0].evidence
        for target in targets:
            graph.add_edge(Edge(source, target, relationship, resolution, evidence, origin=origin, detail=detail))
        if len(targets) > 1:
            graph.issues.append(Issue(
                "AMBIGUOUS_CALL", "info",
                f"Call to {name} has {len(targets)} possible definitions.",
                [source, *targets[:6]], evidence,
                "Treat this edge as a candidate until imports or type information resolve it.",
                subject=name,
            ))


#: Python web frameworks whose routes are not modelled (FastAPI's are): a module that imports one
#: and constructs an application or route table (`_PYTHON_APP_CONSTRUCTORS`).
_UNMODELLED_PYTHON_FRAMEWORKS = frozenset({"flask", "quart", "sanic", "bottle", "falcon", "litestar", "django",
                                           "pyramid", "aiohttp.web", "tornado.web", "cherrypy", "responder"})


#: Calls (by last name) and assignments that build a Flask/Quart/Sanic/Bottle/Falcon/Litestar/
#: aiohttp/Tornado/Pyramid/CherryPy/Responder application or router, or a Django `urlpatterns`.
_PYTHON_APP_CONSTRUCTORS = frozenset({"Flask", "Quart", "Sanic", "Bottle", "Blueprint", "App", "API", "Litestar",
                                      "Application", "Configurator", "Router", "RouteTableDef", "route", "quickstart"})


def _constructs_python_app(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
            if name in _PYTHON_APP_CONSTRUCTORS:
                return True
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "urlpatterns" for target in targets):
                return True
    return False


def _unmodelled_python_routes(state: ScanState) -> list[str]:
    """Modules that import an unmodelled Python web framework and build an app or route table.
    An import alone (a compatibility test, `django.conf.settings` in a script) serves nothing,
    and test code is left out."""
    found = set()
    for rel, tree in state.python_trees.items():
        if is_test_path(rel):
            continue
        for statement in tree.body:
            names = ([alias.name for alias in statement.names] if isinstance(statement, ast.Import)
                     else [statement.module or ""] + [f"{statement.module}.{alias.name}" for alias in statement.names]
                     if isinstance(statement, ast.ImportFrom) and not statement.level else [])
            # `aiohttp` and `tornado` are also HTTP clients; only their `.web` modules serve routes.
            framework = next((name.split(".")[0] for name in names
                              if name in _UNMODELLED_PYTHON_FRAMEWORKS or name.split(".")[0] in _UNMODELLED_PYTHON_FRAMEWORKS),
                             None)
            if framework:
                if _constructs_python_app(tree):
                    found.add(f"a {framework} application module ({rel})")
                break
    return sorted(found)


def _detect_endpoint_gaps(state: ScanState) -> None:
    incoming_kinds: dict[str, set[str]] = defaultdict(set)
    outgoing_kinds: dict[str, set[str]] = defaultdict(set)
    api_callers: dict[str, set[str]] = defaultdict(set)
    call_sites: dict[str, set[str]] = defaultdict(set)
    for edge in state.graph.edges:
        outgoing_kinds[edge.source].add(edge.kind)
        incoming_kinds[edge.target].add(edge.kind)
        if edge.kind == "CALLS_API":
            api_callers[edge.target].add(edge.source)
            call_sites[edge.target].add(edge.evidence)
    handlers = [n for n in state.graph.nodes.values()
                if n.kind == "endpoint" and {"HANDLES_API", "IMPLEMENTED_BY"} & outgoing_kinds[n.id]]
    liveness = _JavaScriptLiveness(state)
    # Routes registered in a shape the scanner does not model (an Express router under an
    # unknown mount, a NestJS controller, a Flask app) may serve any call that matched nothing.
    # A gap is then only a lead: reported as info, naming where those routes are.
    unmodelled = sorted({f"{request.receiver} ({rel}:{request.line})" if request.line > 1 else f"{request.receiver} ({rel})"
                         for rel, _file, request in state.js_requests if request.style == _UNMODELLED_ROUTE})
    unmodelled += _unmodelled_python_routes(state)
    for node in state.graph.nodes.values():
        if node.kind != "endpoint":
            continue
        called = "CALLS_API" in incoming_kinds[node.id]
        handled = bool({"HANDLES_API", "IMPLEMENTED_BY"} & outgoing_kinds[node.id])
        if (called and not handled and not node.metadata.get("matched_handlers")
                and not node.metadata.get("matched_handler_count")):
            if node.metadata.get("method") == "OPTIONS":
                continue  # a CORS preflight or capability query: frameworks answer it without a handler
            code, severity = "API_CALL_WITHOUT_HANDLER", "warning"
            message = f"No matching backend handler was found for {node.label}."
            recommendation = "Check API prefixes, dynamic path normalization, proxy routes, or a missing backend handler."
            sites = sorted(call_sites[node.id])
            if others := _methods_serving(node, handlers):
                # The path exists and the method does not: a 405 at run time, and the one kind
                # of handler gap that is almost always a real bug.
                code, recommendation = "API_METHOD_MISMATCH", "Use a method the route serves, or add a handler for this method."
                message = (f"{node.label} has no handler, but the route is handled for {', '.join(others)}, so the call "
                           f"gets 405 Method Not Allowed. Called from {', '.join(sites[:3])}.")
            reasons = [liveness.dead_reason(state.graph.nodes[caller]) for caller in sorted(api_callers[node.id])]
            if reasons and all(reasons):
                # Code nothing loads cannot send the request; say so rather than rank it with live calls.
                severity = "info"
                message += f" The call is likely dead code: {'; '.join(sorted(set(reasons))[:3])}."
            if unmodelled:
                severity = "info"
                more = f" and {len(unmodelled) - 3} more" if len(unmodelled) > 3 else ""
                message += (f" The repository also registers routes this scanner does not model, which may serve it: "
                            f"{'; '.join(unmodelled[:3])}{more}.")
            state.graph.issues.append(Issue(code, severity, message, [node.id], node.path or node.label, recommendation,
                                            subject=sites[0] if sites else ""))
        elif handled and not called:
            state.graph.issues.append(Issue(
                "API_HANDLER_WITHOUT_STATIC_CALLER", "info",
                f"No static frontend caller was found for {node.label}.", [node.id],
                node.path or node.label,
                "Confirm whether this is external, scheduled, dynamically invoked, or unreachable.",
            ))


def _detect_structural_duplicates(state: ScanState) -> None:
    groups: dict[str, list[Node]] = defaultdict(list)
    for node in state.graph.nodes.values():
        fingerprint = node.metadata.get("body_fingerprint")
        if not fingerprint or not node.path:
            continue
        if node.path.startswith(("tests/", "test/")) or "/tests/" in node.path:
            continue
        groups[str(fingerprint)].append(node)
    for fingerprint, nodes in groups.items():
        paths = {node.path for node in nodes}
        if len(nodes) < 2 or len(paths) < 2:
            continue
        ordered = sorted(nodes, key=lambda item: (item.path or "", item.line or 0, item.label))
        for left, right in zip(ordered, ordered[1:], strict=False):
            state.graph.add_edge(Edge(
                left.id, right.id, "STRUCTURALLY_SIMILAR", "ambiguous",
                f"python_ast_body:{fingerprint[:16]}", origin="heuristic",
            ))
        state.graph.issues.append(Issue(
            "SIMILAR_FUNCTION_BODY", "info",
            f"{len(ordered)} Python functions in different files have the same normalized AST body.",
            [node.id for node in ordered[:8]], f"python_ast_body:{fingerprint[:16]}",
            "Review for a shared owner, but keep distinct implementations when their business meaning differs.",
        ))


def scan_repository(root: Path, config: Config | None = None, *,
                    extractors: Iterable[Extractor] = ()) -> Graph:
    """Scan `root` into a `Graph`: extract each file, then run the cross-file resolution passes.

    `config` is copied, never mutated (extractors add their extensions to the copy). A
    file that fails becomes an issue and the scan continues. `metadata["content_sha256"]`
    equals `repository_content_sha` for the same tree and config."""
    root = root.resolve()
    config = config or Config.load(root)
    config.validate()
    if not root.is_dir():
        raise ValueError("repository root must be an existing directory")
    extractors = tuple(extractors)
    # Work on a copy so repeated calls with different plugins cannot mutate the
    # caller's settings. The content fingerprint includes the newly scanned files.
    from dataclasses import replace
    config = replace(config, extensions=config.extensions | {
        ext.lower() for plugin in extractors for ext in plugin.extensions
    })
    graph = Graph(root=str(root))
    state = ScanState(graph=graph, root=root, config=config)
    ignored, gitignore_problem = _gitignored(root) if config.respect_gitignore else (frozenset(), "")
    if gitignore_problem:
        graph.issues.append(Issue("GITIGNORE_UNAVAILABLE", "info",
                                  f"Git-ignored files could not be listed ({gitignore_problem}); untracked ignored files were read.",
                                  [], "", "Check that git is installed and trusts this directory, or exclude generated paths."))
    state.files = list(islice(iter_source_files(root, config, ignored), config.max_files + 1))
    if len(state.files) > config.max_files:
        state.files.pop()
        graph.issues.append(Issue("SCAN_FILE_LIMIT", "warning", "Scan stopped at the configured file-count limit.",
                                  [], "", "Narrow the scope or increase max_files; this scan is incomplete."))
    state.admitted_paths = frozenset(_rel(root, path) for path in state.files)
    state.import_index = ImportIndex(root, state.files, config.max_file_bytes)
    for path, problem, fatal in state.import_index.problems:
        if fatal:
            graph.issues.append(Issue("IMPORT_CONFIG_ERROR", "warning", problem, [], path, "Correct or flatten the local tsconfig."))
        else:
            graph.issues.append(Issue("IMPORT_CONFIG_PARTIAL", "info", problem, [], path,
                                      "Aliases declared in this config still resolve; inherited options are unknown."))

    digest = hashlib.sha256()
    digest.update(b"truncated" if any(i.code == "SCAN_FILE_LIMIT" for i in graph.issues) else b"complete")
    read_count = 0
    skipped_count = 0
    for path in state.files:
        rel = _rel(root, path)
        source = read_source(root, path, config.max_file_bytes)
        digest.update(source.fingerprint(rel))
        text = source.text
        file_node = _file_id(rel)
        graph.add_node(Node(
            file_node, "file", rel, path=rel,
            language=_language_for_path(path),
            metadata={"size": source.size, "lines": text.count("\n") + 1 if text is not None else 0,
                      "scan_status": source.status},
        ))
        if text is None:
            skipped_count += 1
            # Only a data format no extractor was asked to read; an oversized source file,
            # including one a plugin or `extensions` names, is a hole in the analysis.
            suffix = path.suffix.lower()
            data_file = (source.status == "too_large" and suffix in _DATA_SUFFIXES
                         and not _interpreted_data_file(path.name)
                         and not any(suffix in plugin.extensions for plugin in extractors))
            graph.issues.append(Issue(
                "DATA_FILE_SKIPPED" if data_file else "FILE_SKIPPED", "info" if data_file else "warning",
                f"{'Data file' if data_file else 'File'} was not analyzed: {source.status}.",
                [file_node], rel,
                "Review the file encoding, access and configured max_file_bytes limit.",
                subject=source.status,
            ))
            continue
        read_count += 1
        graph.nodes[file_node].metadata["extractors"] = []
        # A scanner walks whatever a checkout happens to contain, including vendored
        # or generated code. One malformed/deeply nested file must not discard the
        # graph for every other admitted file; its failure is recorded below.
        try:
            if path.suffix == ".py":
                _scan_python(state, path, text, file_node)
                graph.nodes[file_node].metadata["extractors"].append("python-ast")
            elif path.suffix.lower() in javascript.EXTENSIONS:
                if javascript.available():
                    _scan_javascript_syntax(state, path, text, file_node)
                    graph.nodes[file_node].metadata["extractors"].append("tree-sitter")
                else:
                    _scan_javascript(state, path, text, file_node)
                    graph.nodes[file_node].metadata["extractors"].append("javascript-regex")
                    graph.issues.append(Issue("JAVASCRIPT_PARSER_UNAVAILABLE", "warning", "Using legacy regex extraction; JS/TS syntax coverage is incomplete.",
                                              [file_node], rel, "Install repolens[stack]."))
            elif path.suffix.lower() == ".sql":
                from .postgres import add_sql_file
                add_sql_file(graph, file_node, text, rel)
            elif path.suffix.lower() == ".prisma":
                state.prisma_schemas.append((rel, text))
            _scan_generic(state, path, text, file_node)
        except RecursionError:
            state.graph.issues.append(Issue(
                "FILE_TOO_DEEPLY_NESTED", "warning",
                f"Could not walk {rel}: expression nesting exceeded the interpreter's "
                "recursion limit. The file is skipped; the rest of the scan continues.",
                [file_node], rel,
                "Usually vendored or generated code. Exclude it, or raise the "
                "recursion limit if the file is one you own and need indexed.",
            ))
        except Exception as exc:  # noqa: BLE001 - one file may never end the run
            state.graph.issues.append(Issue(
                "FILE_SCAN_FAILED", "warning",
                f"Could not scan {rel}: {type(exc).__name__}: {exc}",
                [file_node], rel,
                "Report the file; the scan continued without it, so this result is "
                "incomplete for that path only.",
            ))

        for plugin in extractors:
            if path.suffix.lower() not in {ext.lower() for ext in plugin.extensions}:
                continue
            try:
                merge_extraction(graph, plugin.analyze(SourceFile(rel, text, file_node)), plugin)
                graph.nodes[file_node].metadata["extractors"].append(plugin.name)
            except Exception as exc:
                graph.issues.append(Issue(
                    "EXTRACTOR_FAILED", "warning",
                    f"Extractor {plugin.name} failed ({type(exc).__name__}); its output was discarded.",
                    [file_node], rel, "Check the installed extractor version and report this file.",
                    subject=plugin.name,
                ))

    def run_pass(name: str, step: Callable[..., object], *args: object) -> None:
        # The per-file guard above does not cover the cross-file passes. One pass that
        # crashes (a pathological tree, an unforeseen shape) must not discard the graph;
        # what it would have added is missing, so the code is an incomplete one. The
        # exception text is left out: it can carry paths from the scanned checkout.
        try:
            step(*args)
        except Exception as exc:  # noqa: BLE001 - one pass may never end the run
            graph.issues.append(Issue(
                "ANALYSIS_PASS_FAILED", "warning",
                f"Analysis pass {name} failed ({type(exc).__name__}); the relationships it adds are missing.",
                [], "", "Report this; the scan continued without that pass, so this result is incomplete.",
                subject=name,
            ))

    from .columns import check_columns
    from .fastapi import add_routes
    run_pass("resolve_js_requests", _resolve_js_requests, state)
    run_pass("resolve_js_stores", _resolve_js_stores, state)
    run_pass("check_columns", check_columns, state)
    run_pass("resolve_orm_references", _resolve_orm_references, state)
    run_pass("add_routes", add_routes, state)
    run_pass("resolve_calls", _resolve_calls, state)

    def load_artifact(loader: Callable[[ScanState], None]) -> None:
        try:
            loader(state)
        except (ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
            graph.issues.append(Issue("ARTIFACT_SCAN_FAILED", "warning",
                                      f"A declared relationship artifact is malformed ({type(exc).__name__}).",
                                      [], "", "Validate artifact structure; declared relationships may be incomplete."))

    run_pass("load_canonical_owners", load_artifact, _load_canonical_owners)
    run_pass("load_router_datastore_map", load_artifact, _load_router_datastore_map)
    if state.api_prefix_already_resolved:
        state.graph.issues.append(Issue(
            "API_PREFIX_ALREADY_RESOLVED", "info",
            f"backend_api_prefix {state.config.backend_api_prefix!r} was already part of "
            f"{state.api_prefix_already_resolved} resolved route(s) and was not added to them again.",
            [], "[impact] backend_api_prefix",
            "Remove backend_api_prefix when the application declares the prefix itself; keep it only for a "
            "prefix added outside the code (a proxy or root_path).",
        ))
    run_pass("match_endpoints", _match_endpoints, state)
    run_pass("detect_endpoint_gaps", _detect_endpoint_gaps, state)
    run_pass("detect_structural_duplicates", _detect_structural_duplicates, state)
    run_pass("link_test_store_references", _link_test_store_references, state)
    run_pass("mark_unverified_stores", mark_unverified_stores, graph)
    graph.metadata.update({
        "content_sha256": digest.hexdigest(),
        "config_sha256": config_fingerprint(config),
        "file_count": len(state.files),
        "read_file_count": read_count,
        "skipped_file_count": skipped_count,
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "issue_count": len(graph.issues),
        "evidence_model": "declared intent, static syntax, lexical inference kept separate",
        "scanner_revision": SCANNER_REVISION,
        "extractors": [{"name": plugin.name, "version": plugin.version,
                        "api_version": plugin.api_version, "extensions": list(plugin.extensions)}
                       for plugin in extractors],
    })
    return graph
