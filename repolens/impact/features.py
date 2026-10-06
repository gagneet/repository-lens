"""Feature groups: the pages, endpoints, handlers, services and stores one route area connects.

A group is named by the first static segment of its routes (`/api/orders/{orderId}` and the
`/orders` page are both `orders`). It is a navigation aid built from evidence edges, not a
business concept: a person or an agent renames it. The documentation generator and the
FeatureTrace proposer both read groups from here, so the two outputs never disagree about
which files belong together.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..core.files import is_test_code, is_test_path
from .model import Graph, Node

#: Layers in precedence order: a file that both handles a route and queries a table is a router.
LAYERS = ("router", "service", "model", "frontend")
#: Edges followed from a handler or page component towards the code it runs: calls, renders and the
#: functions and inline callbacks a component defines.
_RUNS = frozenset({"CALLS", "RENDERS", "DEFINES"})
_STORE_KINDS = frozenset({"postgres_table", "mongo_collection"})
#: Edges from code, or from a router a declared artifact names, to a store it uses.
STORE_EDGES = frozenset({"TOUCHES_STORE", "READS_STORE", "WRITES_STORE"})
#: Resolutions a walk may follow. A name-only (`probable`) or `ambiguous` call can be any function
#: with that name, and following it made every handler reach most of a large backend's stores.
CONFIDENT = frozenset({"exact", "high", "declared"})
#: Edges from an endpoint to what serves it: a scanned handler, or a route a declared artifact names.
HANDLER_EDGES = frozenset({"HANDLES_API", "IMPLEMENTED_BY"})
_VERSION = re.compile(r"^v\d+$", re.I)


@dataclass
class FeatureGroup:
    """One route area: node ids of its pages, endpoints and stores, and its files by layer."""
    name: str
    pages: list[str] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    stores: list[str] = field(default_factory=list)
    #: path -> layer (one of `LAYERS`)
    files: dict[str, str] = field(default_factory=dict)
    #: endpoint id -> ids of the nodes that call it, from any group
    callers: dict[str, list[str]] = field(default_factory=dict)
    #: endpoint ids in other groups that this group's pages call
    calls_out: list[str] = field(default_factory=list)

    def add_file(self, path: str | None, layer: str) -> None:
        """Record `path` at `layer`, keeping the higher-precedence layer when it is already known."""
        if not path or is_test_path(path):
            return
        known = self.files.get(path)
        if known is None or LAYERS.index(layer) < LAYERS.index(known):
            self.files[path] = layer


def route_path(node: Node) -> str:
    """The route as the source spells it (`/api/orders/{orderId}`) when known, else the matching shape."""
    return str(node.metadata.get("path") or node.metadata.get("route") or node.label.split(" ", 1)[-1])


def group_name(route: str, *, page: bool) -> str:
    """The first static route segment, skipping `api` and version segments; `home` or `api` when there is none."""
    for segment in (s for s in route.split("/") if s):
        if segment.lower() == "api" or _VERSION.match(segment) or segment[0] in "{[:$":
            continue
        name = re.sub(r"[^a-z0-9]+", "-", segment.lower()).strip("-")
        if name:
            return name
    return "home" if page else "api"


def reach(outgoing: dict[str, list], start: str, depth: int) -> set[str]:
    """Nodes reachable from `start` over confident calls, renders and definitions within `depth` hops."""
    seen, queue = {start}, deque([(start, 0)])
    while queue:
        node, hops = queue.popleft()
        if hops == depth:
            continue
        for edge in outgoing.get(node, ()):
            if edge.kind in _RUNS and edge.resolution in CONFIDENT and edge.target not in seen:
                seen.add(edge.target)
                queue.append((edge.target, hops + 1))
    return seen


def declares(detail: str | None) -> bool:
    """Whether a store edge declares the store (DDL, a model class, a Prisma model) rather than using it.

    Readers put `declares` in the detail (`SQL syntax reference (declares)`, `SQLAlchemy model (declares)`)."""
    return "declares" in (detail or "")


def _from_test_code(graph: Graph, edge) -> bool:
    node = graph.nodes.get(edge.source)
    return bool(node is not None and node.path and is_test_code(node.path))


def edge_index(graph: Graph) -> tuple[dict[str, list], dict[str, list]]:
    """(outgoing, incoming) edges by node id."""
    outgoing: dict[str, list] = defaultdict(list)
    incoming: dict[str, list] = defaultdict(list)
    for edge in graph.edges:
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)
    return outgoing, incoming


def endpoint_status(graph: Graph, outgoing: dict[str, list], incoming: dict[str, list]) -> dict[str, str]:
    """Every endpoint id -> "served" (a handler or declared route serves it), "matched" (a placeholder for a
    call the scanner also linked to a served endpoint), "open" (a URL so open that more handlers than
    `max_ambiguous_targets` fit, reported as `DYNAMIC_HTTP_REQUEST`), "unserved" (a call from application
    code nothing is known to serve) or "uncalled" (no handler, and no call outside test code).

    Calls from test code (`core.files.is_test_code`) are not evidence of what the application requests."""
    served = {node.id for node in graph.nodes.values() if node.kind == "endpoint"
              and any(e.kind in HANDLER_EDGES for e in outgoing.get(node.id, ()))}
    linked = {(e.source, e.evidence) for e in graph.edges if e.kind == "CALLS_API" and e.target in served}
    status = {}
    for node in graph.nodes.values():
        if node.kind != "endpoint":
            continue
        if node.id in served:
            status[node.id] = "served"
            continue
        if node.metadata.get("matched_handler_count"):
            status[node.id] = "open"
            continue
        calls = [(e.source, e.evidence) for e in incoming.get(node.id, ())
                 if e.kind == "CALLS_API" and not _from_test_code(graph, e)]
        if not calls:
            status[node.id] = "uncalled"
        else:
            status[node.id] = "matched" if all(call in linked for call in calls) else "unserved"
    return status


def feature_groups(graph: Graph, *, depth: int = 4) -> dict[str, FeatureGroup]:
    """Every feature group in `graph`, by name, in name order.

    A placeholder endpoint whose calls were matched to a served endpoint belongs to no group (the
    served endpoint carries those callers), and neither does a call too open to link or one only
    test code makes. Test files are never group files."""
    outgoing, incoming = edge_index(graph)
    status = endpoint_status(graph, outgoing, incoming)
    groups: dict[str, FeatureGroup] = {}

    def group(name: str) -> FeatureGroup:
        return groups.setdefault(name, FeatureGroup(name))

    def path_of(node_id: str) -> str | None:
        node = graph.nodes.get(node_id)
        return node.path if node else None

    def add_stores(target: FeatureGroup, reached: set[str], home: set[str]) -> None:
        for node_id in sorted(reached):
            for edge in outgoing.get(node_id, ()):
                store = graph.nodes.get(edge.target)
                if edge.kind not in STORE_EDGES or store is None or store.kind not in _STORE_KINDS:
                    continue
                if edge.target not in target.stores:
                    target.stores.append(edge.target)
                where = path_of(node_id)
                if where not in home:
                    target.add_file(where, "model" if declares(edge.detail) else "service")

    page_files: set[str] = set()
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.kind != "page":
            continue
        target = group(group_name(route_path(node), page=True))
        target.pages.append(node.id)
        components = [e.target for e in outgoing.get(node.id, ()) if e.kind == "IMPLEMENTED_BY"]
        for component in components:
            target.add_file(path_of(component), "frontend")
            page_files.add(path_of(component) or "")
            # A server component or a Next.js server action reaches data with no HTTP route:
            # the stores its confident (import-bound) calls reach belong to the page's group.
            add_stores(target, reach(outgoing, component, depth), {path_of(component)})
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.kind != "endpoint" or status.get(node.id) in {"matched", "open", "uncalled"}:
            continue
        target = group(group_name(route_path(node), page=False))
        target.endpoints.append(node.id)
        handlers = [e.target for e in outgoing.get(node.id, ()) if e.kind in HANDLER_EDGES]
        home = {path_of(h) for h in handlers}
        for handler in handlers:
            target.add_file(path_of(handler), "router")
            add_stores(target, reach(outgoing, handler, depth), home)
        callers = sorted({e.source for e in incoming.get(node.id, ())
                          if e.kind == "CALLS_API" and not _from_test_code(graph, e)})
        target.callers[node.id] = callers
        for caller in callers:
            where = path_of(caller)
            if where and where not in page_files:
                target.add_file(where, "frontend" if (graph.nodes[caller].language or "").startswith(("javascript", "typescript")) else "service")
    # Files that declare a group's stores (DDL, Prisma schemas, ORM models) are its model layer.
    for g in groups.values():
        for store in g.stores:
            for edge in incoming.get(store, ()):
                source = graph.nodes.get(edge.source)
                if edge.kind in STORE_EDGES and source is not None and declares(edge.detail):
                    g.add_file(source.path, "model")
    # A page calls endpoints through its component and the functions it defines or renders.
    endpoint_group = {endpoint: g.name for g in groups.values() for endpoint in g.endpoints}
    for g in list(groups.values()):
        for page in g.pages:
            for component in (e.target for e in outgoing.get(page, ()) if e.kind == "IMPLEMENTED_BY"):
                for reached in reach(outgoing, component, depth):
                    for edge in outgoing.get(reached, ()):
                        if edge.kind == "CALLS_API" and endpoint_group.get(edge.target, g.name) != g.name:
                            if edge.target not in g.calls_out:
                                g.calls_out.append(edge.target)
    return dict(sorted(groups.items()))
