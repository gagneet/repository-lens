"""Shared, read-only stack analysis for the CLI and local HTTP API."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path

from . import __version__
from .config import load_config
from .core.files import is_test_code
from .core.findings import Finding, ToolRun, sort_key, to_sarif
from .core.html_report import render_html
from .provenance import describe_build, tool_build
from .core import javascript
from .impact.config import Config
from .impact.model import Graph
from .impact.plugins import available, load_extractors
from .impact.query import ImpactResult, impact
from .impact.render import markdown_cell, render_mermaid
from .impact.scanner import scan_repository
from .report.sarif import import_sarif

LIMITS = [
    "Static syntax and declared relationships do not prove runtime calls, authorization or business behavior.",
    "JavaScript/TypeScript call targets have no compiler type resolution; dynamic imports, package `exports` maps and runtime-assigned members may remain unresolved.",
    "Next.js App Router and Pages Router routes and pages are modelled; middleware, rewrites, redirects and server-action authorization are not. Other JS/TS backends are modelled only as `app.<verb>(\"/literal\", handler)` on an Express/Fastify/Hono/Polka/Elysia server the same file creates and starts listening (probable). Routers and plugins under a mount prefix, NestJS controllers, SvelteKit `+server`, Astro and Expo API routes, Remix/React Router `loader`/`action`, Nuxt server handlers and Python frameworks other than FastAPI (Flask, Django, ...) are not modelled: when code outside test paths registers such routes (a Python module must also build the app), `API_CALL_WITHOUT_HANDLER` and `API_METHOD_MISMATCH` are info and name them. That judgement is repository-wide, not per package. `.route(path).<verb>()` chains, hapi/Fastify `.route({ method, path, handler })` objects and a router-named parameter given a handler by reference count as such registrations.",
    "FastAPI includes, sub-app mounts, `api_route`/`add_api_route`, dependencies and prefixes from module constants or settings defaults are resolved; prefixes computed at runtime and routes registered in loops over non-literal values remain unresolved.",
    "PostgreSQL references are parsed from literal SQL, one statement at a time for .sql files; DDL the parser keeps as text (RLS, policies and their USING/WITH CHECK tables, triggers, grants, routines) is read lexically, and PL/pgSQL bodies only where a statement parses alone; `SQL_UNSUPPORTED_STATEMENT` lists only the known kinds whose effect is not modelled (rules, event triggers, maintenance commands, DO blocks that EXECUTE a string). No database connection, live schema, RLS enforcement or query-plan validation is performed.",
    "`SQL_UNKNOWN_COLUMN` checks only references attributable to one physical table, outside migrations and test paths, against the project's Prisma schema or scanned `CREATE`/`ALTER TABLE` DDL; dynamic SQL, CTEs, views, tables other ORMs or unparsed-language migrations manage, and unqualified names under a changed `search_path` are not checked.",
    "A `.sql` file detected as another dialect (T-SQL, MySQL, Oracle, SQLite) or a dbt/Jinja template is reported once as `SQL_DIALECT_NOT_POSTGRES` and not read, so its tables are not recorded.",
    "A JS/TS request URL or client base that starts with a configured origin (`process.env.X`, `import.meta.env.X`, an origin-named constant) is this repository's API only when the origin's name, after a framework prefix such as `NEXT_PUBLIC_` or `VITE_`, is made of words like API, BACKEND, SERVER, BASE or URL, or is listed in `[impact] api_origins`; another name (`STRIPE_API_URL`) is an `EXTERNAL_API_REFERENCE`, and a `localhost`/`127.0.0.1` origin is this repository. A call matched only after skipping the path an origin may carry is `ambiguous` and needs a literal segment. A URL of only runtime segments, or an open-ended one more than `max_ambiguous_targets` handlers could serve, is `DYNAMIC_HTTP_REQUEST`: neither linked nor a gap. A client whose declaration cannot be traced gets `[impact] client_api_base`, else the one base URL its package declares, else the repository's one, never added twice; jQuery, Angular `HttpClient`, bare axios/ky, browser-test globals and receivers in test code get no assumed base.",
    "Store references matched only by a pattern in test code or fixtures link only to stores other evidence found, and a router/datastore artifact's `postgres_unverified_refs` link only to tables the scan found by more than a pattern; the other names are counted in `ARTIFACT_STORE_UNCONFIRMED` (info).",
    "A JS/TS MongoDB collection is recorded only when the receiver is provably a driver handle in that file or through one local import; handles passed as untyped parameters, returned from functions, split across lines before `.collection()` or set in another file are missed.",
    "Built-in security/performance findings cover Python and Alembic. For JS/TS the only built-in security check is SQL text that splices in a CLI argument or request input, traced through variables within one file; other JS/TS security coverage requires imported SARIF from a separate analyzer. Request input is recognised only in these shapes: Express/Fastify/Next.js `req`/`request` `.query|body|params|headers|cookies|url|nextUrl`, Koa `ctx.query|params|body` and `ctx.request.query|body|params|headers|files`, hapi `request.payload|params|query`, Lambda `event.queryStringParameters|pathParameters`, `request.json()|formData()|text()`, `<any>.searchParams.get()`, h3/Nuxt `getQuery()|readBody()|getRouterParam()`, `location.search|hash`, `params`/`searchParams` of Next.js route handlers and default exports, `params`/`url` of functions named `load`, `loader`, `action` or an HTTP verb, and NestJS `@Body()`/`@Param()`/`@Query()`/`@Headers()`/`@Req()` parameters. Numeric conversions and arithmetic, node-postgres and pg-format quoting helpers, mysql/sqlstring `escape`/`escapeId` on a receiver named or imported like a SQL connection, placeholder lists built without reading the elements, lookups in a constant object or array, and `includes`/`has`/`in`/`hasOwnProperty`/`typeof`/`Number.isInteger` guards over a constant or inline literal table (also `Object.keys`/`Object.values` of one) are not tainted: in a ternary, inside the guarding `if`, or after an `if (!guard)` in an enclosing block that throws, returns, breaks or continues, with no later assignment. Guards in helper functions or `else` branches are not followed. Escaped string text (`\\'`, `\\u0065`) is decoded before SQL and URLs are rebuilt.",
    "Dead-code judgements for JS/TS callers follow imports (including `const { a } = require()`, literal dynamic `import()` and side-effect imports, which load the whole module) and same-file uses by value from routed pages, handlers and entry files: entry-named stems such as `main`, `index`, `page`, `route` and `layout` (a re-export-only `index` barrel is not one), Next.js special and metadata files, Remix `root`, `entry.client`/`entry.server` and `app/routes/**`, SvelteKit `+page`/`+layout`/`+server`, files a package.json names in `main`, `module`, `browser`, `bin` or `exports`, and every file under `app/` in a package that uses Expo Router. A repository containing any .vue, .svelte, .astro or .mdx file gets no dead-code judgements, because those files' imports are not read. Code loaded by configuration, HTML or runtime-built import paths can still be reported as unreachable.",
    "Untracked files that git ignores (`[impact] respect_gitignore`) and non-source files over `max_file_bytes` (`DATA_FILE_SKIPPED`) are not scanned and do not make the analysis incomplete; in a git submodule or nested checkout, ignore rules are read only from the checkout that holds the scanned root. The built-in Python checks apply the same setting, read like the graph's from `[impact]` and `.impact-tracer.json`; deployment manifests are read even when git ignores them, and under `repolens scan` or `repolens report` a git that cannot list ignored files leaves every file read without a diagnostic.",
    "Unresolved local imports, router mounts and mount cycles in test code (a tests/, test/, `__tests__`/, e2e/ or cypress/ directory, or a test file name) are reported but do not make the analysis incomplete; a test file that cannot be parsed or is skipped still does.",
    "Deployment detection reads systemd, Dockerfile, block-style compose, Procfile, supervisord, shell and Python run-script command lines in the repository. package.json scripts and test, CI or dev files never make a deployment known; an ambiguous or run-time application name, a manifest or format it does not interpret (Kubernetes, Helm, `app.yaml`, `fly.toml`, compose flow style, ...), an image whose command is not in the repository, or a reachable file importing modules chosen at run time keeps it unknown, and a known deployment is still not proof of what runs in production.",
    "`repolens docs generate` and `repolens featuretrace propose` document only what the scan found. OpenAPI paths, methods and path parameters come from route declarations; a request or response schema is named only where the source declares a model, and everything else is listed in `x-repolens-gaps`. The ER diagram shows columns from scanned PostgreSQL DDL and Prisma models and declared foreign keys, not MongoDB fields or ORM-only columns; SQL files apply in path order with drops, renames and nullability changes, but not dropped constraints, and a schema-qualified name is a separate table from the bare one. A Pages Router API route that answers every method is in the `x-repolens-any-method` extension, which OpenAPI viewers do not show. Feature groups are route areas named by their first path segment, not business features. Proposed tags, roles and owners are placeholders for a person's decision, and a proposal that passes `featuretrace audit` has well-formed references, not a verified data flow.",
    "Generated debugging plans are static caller, route, handler and store candidates, not observed traces. Repository Lens installs no runtime instrumentation. Optional `@functionlens:` comments carry only a stable id and have no runtime behavior; inferred callers and stores remain in the generated index.",
    "This local process is not a sandbox for hostile repositories or plugins. Target builds, installs and configured commands are not run.",
]


# A table is read by a service, called by a handler, served as an endpoint, called by
# a client and rendered by a page: 5-6 hops. At the old fixed depth of 2 a table query
# stopped at the service. The query budget and per-layer caps still bound the result.
VIEW_DEPTH = 6
MAX_VIEW_DEPTH = 8

# Diagnostics that mean a Python file was never examined by the checks that read it.
_UNEXAMINED = {"FILE_SKIPPED", "FILE_SCAN_FAILED", "FILE_TOO_DEEPLY_NESTED", "PYTHON_PARSE_ERROR"}


#: "Could not link" diagnostics that do not make an analysis incomplete when the code that
#: raised them is test code; see `Analysis.incomplete_reasons`.
_RESOLUTION_CODES = frozenset({"UNRESOLVED_LOCAL_IMPORT", "UNRESOLVED_ROUTER_MOUNT", "ROUTER_MOUNT_CYCLE"})


def _in_test_code(evidence: str) -> bool:
    path, _, line = (evidence or "").rpartition(":")
    return bool(path and line.isdigit() and is_test_code(path))


@dataclass
class Analysis:
    """One scan's graph and tool runs, with the config that built them; what the CLI and API export."""
    graph: Graph
    runs: list[ToolRun]
    config: Config

    @property
    def complete(self) -> bool:
        """True when no tool was skipped or crashed and no input went unread; see `incomplete_reasons`."""
        return not self.incomplete_reasons()

    def incomplete_reasons(self) -> list[str]:
        """Why the analysis is incomplete, one line per cause; empty when it is complete."""
        # Completeness means the requested supported analysis finished, not that
        # every behavior/language has been understood or that the code is safe.
        # SQL_PARSE_ERROR stays here: it comes only from statements in .sql files (SQL
        # picked out of code is gated and reports SQL_NOT_PARSED or DYNAMIC_SQL instead),
        # so it is a statement we could not read. Any parser exception, not only a
        # ParseError, ends there: it never fails the rest of the file.
        incomplete = {"FILE_SKIPPED", "FILE_SCAN_FAILED", "FILE_TOO_DEEPLY_NESTED", "SCAN_FILE_LIMIT",
                      "PYTHON_PARSE_ERROR", "JAVASCRIPT_PARSE_ERROR", "JAVASCRIPT_PARSER_UNAVAILABLE",
                      "SQL_PARSER_UNAVAILABLE", "SQL_PARSE_ERROR", "EXTRACTOR_FAILED", "IMPORT_CONFIG_ERROR",
                      "ARTIFACT_SCAN_FAILED", "ARTIFACT_PATH_OUTSIDE_ROOT",
                      "BAD_CANONICAL_OWNER_ARTIFACT", "BAD_ROUTER_DATASTORE_ARTIFACT",
                      "UNRESOLVED_LOCAL_IMPORT", "UNRESOLVED_ROUTER_MOUNT", "ROUTER_MOUNT_CYCLE",
                      "ANALYSIS_PASS_FAILED"}
        # Deliberately NOT incomplete, and never to be added above:
        # - SQL_NOT_PARSED: a string picked out of source by heuristic that looked like
        #   SQL and did not parse. It may be prose or another dialect; nothing we were
        #   asked to analyze was skipped.
        # - SQL_UNSUPPORTED_STATEMENT: a complete statement of a known PostgreSQL kind
        #   that the parser keeps as a command or rejects, and whose effect the lexical
        #   reader does not model (rules, event triggers, maintenance commands, a DO block
        #   that EXECUTEs a string). RLS, policies, triggers, grants and routines are read
        #   lexically in full and not reported (`postgres.lexically_modelled`); counting
        #   them made every schema with RLS "incomplete". Text that is not a complete
        #   statement of a known kind is SQL_PARSE_ERROR instead.
        # - SQL_DIALECT_NOT_POSTGRES: a .sql file in another dialect (T-SQL, MySQL, Oracle,
        #   SQLite) or a dbt/Jinja template, reported once per file instead of a parse
        #   error per statement. PostgreSQL analysis does not apply to it.
        # - UNRESOLVED_LOCAL_IMPORT, UNRESOLVED_ROUTER_MOUNT and ROUTER_MOUNT_CYCLE located in
        #   test code (`core.files.is_test_code`), in any language: a test harness wiring
        #   routers or importing helpers is not a served application, and its file was read.
        #   The diagnostic stays in the output. Anywhere else these still make it incomplete,
        #   and a test file that fails to parse or is skipped still does (its facts are lost).
        # - SQL_PLPGSQL_OUTSIDE_BLOCK and SQL_UNKNOWN_COLUMN: warnings about the target
        #   (a script PostgreSQL rejects, a column the declared schema lacks). The input
        #   was read and understood; nothing was skipped. A reference the column check
        #   leaves out (search_path, test code, a table another tool may reshape) is a
        #   precision choice about an advisory warning, not unread input.
        reasons = [f"tool {r.tool} {'crashed' if r.error else 'did not run'}: {r.error or r.skipped}"
                   for r in self.runs if r.error or r.skipped]
        by_code: dict[str, list] = {}
        for issue in self.graph.issues:
            if issue.code in incomplete and not (issue.code in _RESOLUTION_CODES and _in_test_code(issue.evidence)):
                by_code.setdefault(issue.code, []).append(issue)
        for code, issues in sorted(by_code.items()):
            examples = ", ".join(issue.evidence or issue.subject or "-" for issue in issues[:3])
            reasons.append(f"{code} x{len(issues)} (e.g. {examples})")
        # Per-file isolation in the scan/ checks reports a file it could not
        # read as `<check>/could-not-scan`: that check's result has a hole.
        holes = [f for r in self.runs for f in r.findings if f.rule.endswith("/could-not-scan")]
        if holes:
            reasons.append(f"could-not-scan x{len(holes)} (e.g. {', '.join(f.file for f in holes[:3])})")
        return reasons

    def to_dict(self) -> dict:
        """The JSON analysis document (`analysis.json`, API responses); `graph.root` is the directory name only."""
        graph = self.graph.to_dict()
        graph["root"] = Path(self.graph.root).name  # export no host filesystem prefix
        return {"schema_version": "1.0", "tool_version": __version__, "tool_build": tool_build(),
                "config_sha256": _config_sha256(self.config), "complete": self.complete,
                "incomplete_reasons": self.incomplete_reasons(),
                "scope": "static working-tree analysis", "limits": LIMITS,
                "coverage": dict(Counter(node.language or "unknown" for node in self.graph.nodes.values() if node.kind == "file")),
                "graph": graph,
                "tools": [{"tool": run.tool, "error": run.error, "skipped": run.skipped,
                           "finding_count": len(run.findings), "notes": run.notes} for run in self.runs],
                "findings": [finding.to_dict() for finding in sorted(
                    (f for run in self.runs for f in run.findings), key=sort_key)]}

    def view(self, query: str = "", max_nodes: int = 30, depth: int = VIEW_DEPTH) -> ImpactResult:
        """The bounded impact result for `query`, or a repository overview when it is empty.

        A query traverses the stored graph only, never the working tree. The overview
        ranks endpoints, tables and concepts first, then by degree, and leaves regex-only
        stores out, counted in `omitted_breakdown`. Raises ValueError for an out-of-range
        `max_nodes` or `depth`."""
        if not 1 <= max_nodes <= 100:
            raise ValueError("max_nodes must be between 1 and 100")
        if not 1 <= depth <= MAX_VIEW_DEPTH:
            raise ValueError(f"depth must be between 1 and {MAX_VIEW_DEPTH}")
        if query:
            # Use only the stored graph. API queries must never re-read a newer
            # working tree and mix its snippets with an older scan's relationships.
            return impact(self.graph, Path(self.graph.root), query, self.config,
                          max_nodes=max_nodes, depth=depth, search_source=False)
        from .impact.render import UNVERIFIED_REASON, unverified_stores
        # A store only a regex saw (a schema-qualified name in a string) is a lead, not a
        # table: the overview leaves it out and says how many, rather than drawing it.
        hidden = unverified_stores(self.graph)
        degree = Counter(endpoint for edge in self.graph.edges for endpoint in (edge.source, edge.target))
        ordered = sorted((n for n in self.graph.nodes.values() if n.id not in hidden), key=lambda n: (
            0 if n.kind in {"endpoint", "postgres_table", "concept"} else 1,
            -degree[n.id], n.path or "", n.label, n.id))
        nodes = {n.id: n for n in ordered[:max_nodes]}
        edges = [e for e in self.graph.edges if e.source in nodes and e.target in nodes]
        left_out = Counter(self.graph.nodes[node_id].kind for node_id in hidden)
        breakdown = [{"file": "(repository)", "type": kind, "relationship": "TOUCHES_STORE",
                      "reason": UNVERIFIED_REASON, "count": count} for kind, count in sorted(left_out.items())]
        return ImpactResult("Repository overview", [], nodes, edges, self.graph.issues, {},
                            max(0, len(ordered) - len(nodes)) + len(hidden), breakdown)

    def html(self, query: str = "", max_nodes: int = 30, depth: int = VIEW_DEPTH) -> str:
        """The report as one self-contained HTML page (no scripts or external resources)."""
        return _html(self, query, max_nodes, depth)

    def markdown(self, query: str = "", max_nodes: int = 30, depth: int = VIEW_DEPTH) -> str:
        """The report as Markdown: completeness, the Mermaid map of `view`, findings, diagnostics and limits."""
        payload = self.to_dict()
        view = self.view(query, max_nodes, depth)
        cell = markdown_cell
        lines = ["# Repository Lens analysis", "",
                 f"Repository: {cell(Path(self.graph.root).name)}. Supported analysis complete: {self.complete}.", "",
                 *[f"- Incomplete: {cell(reason)}" for reason in payload["incomplete_reasons"]],
                 *([""] if payload["incomplete_reasons"] else []),
                 f"Produced by {cell(describe_build(payload['tool_build']))}; configuration {payload['config_sha256'][:12]}.", "",
                 f"{len(self.graph.nodes)} nodes, {len(self.graph.edges)} relationships, "
                 f"{len(self.graph.issues)} analysis diagnostics, {len(payload['findings'])} findings.", "",
                 "## Linkage map", "", "```mermaid", render_mermaid(view).rstrip(), "```", "",
                 f"Diagram omits {view.omitted_nodes} nodes. Use --query to inspect one feature or module.", "",
                 "## Findings", "", "| Priority | Rule | Location | Finding |", "|---|---|---|---|"]
        for f in payload["findings"]:
            lines.append("| " + " | ".join(cell(f[k]) for k in ("priority", "rule", "location", "message")) + " |")
        if not payload["findings"]:
            lines.append("| — | — | — | No findings from the checks that ran. |")
        lines += ["", "## Analysis diagnostics", "", "| Level | Code | Evidence | Action |", "|---|---|---|---|"]
        for issue in self.graph.issues:
            lines.append("| " + " | ".join(cell(value) for value in (issue.severity, issue.code, issue.evidence, issue.recommendation)) + " |")
        for run in self.runs:
            if run.error:
                lines += ["", f"Tool error ({cell(run.tool)}): {cell(run.error)}"]
        lines += ["", "## Coverage and limits", "", *[f"- {limit}" for limit in LIMITS], ""]
        return "\n".join(lines)


def _html(analysis: "Analysis", query: str, max_nodes: int, depth: int) -> str:
    payload = analysis.to_dict()
    return render_html(
        analysis.runs, title="Repository Lens analysis", repository=Path(analysis.graph.root).name,
        complete=payload["complete"], incomplete_reasons=payload["incomplete_reasons"],
        build=describe_build(payload["tool_build"]), configuration=payload["config_sha256"][:12],
        diagnostics=analysis.graph.issues, coverage=payload["coverage"], limits=LIMITS,
        linkage=render_mermaid(analysis.view(query, max_nodes, depth)),
        facts=[f"{len(analysis.graph.nodes)} nodes, {len(analysis.graph.edges)} relationships, "
               f"{len(analysis.graph.issues)} analysis diagnostics, {len(payload['findings'])} findings."])


def _config_sha256(config) -> str:
    from .impact.scanner import config_fingerprint
    return config_fingerprint(config)


def _graph_correctness_findings(graph) -> list[Finding]:
    """A live client call to a route that exists without its method: a 405 at run time."""
    findings = []
    for issue in graph.issues:
        if issue.code != "API_METHOD_MISMATCH" or issue.severity != "warning":
            continue
        path, _, line = issue.subject.rpartition(":")
        findings.append(Finding(
            tool="stack", rule="stack/api-method-mismatch", severity="medium", confidence="medium",
            category="correctness", file=path, line=int(line) if line.isdigit() else 0,
            message=issue.message, remedy=issue.recommendation,
        ))
    return findings


def _graph_security_findings(graph) -> list[Finding]:
    findings = []
    for issue in graph.issues:
        if issue.code != "SQL_INJECTION_RISK":
            continue
        path, _, line = issue.evidence.rpartition(":")
        request = "HTTP request" in issue.message
        findings.append(Finding(
            tool="stack", rule="security/sql-string-interpolation", severity="high" if request else "medium",
            confidence="medium", category="security", file=path, line=int(line) if line.isdigit() else 0,
            message=issue.message, remedy=issue.recommendation,
        ))
    return findings


def analyze(root: Path, *, config: Config | None = None, plugins: list[str] | None = None,
            sarif_files: list[Path] | None = None) -> Analysis:
    """Analyze an existing local checkout; no network or target-code execution."""
    if not javascript.available():
        raise ValueError("The supported stack requires parser dependencies: pip install 'repolens[stack]'")
    try:
        import sqlglot  # noqa: F401
    except ImportError as exc:
        raise ValueError("PostgreSQL analysis requires: pip install 'repolens[stack]'") from exc
    root = root.resolve()
    settings = config or Config.load(root)
    graph = scan_repository(root, settings, extractors=load_extractors(plugins or []))
    from .scan.python_ast import python_files
    from .scan.settings import from_config
    from .scan import security, performance, migrations
    # Reuse the repository's scan policy (auth patterns, migration roots, and
    # admitted Python roots). The impact config above is a separate namespace.
    # Configured commands are not executed. Configured regexes remain trusted policy.
    checks = from_config(load_config(root), max_file_bytes=settings.max_file_bytes)
    configured_python = {checks.rel(path) for path in python_files(checks)}
    checks.admitted_python_files = tuple(sorted(
        n.path for n in graph.nodes.values()
        if n.kind == "file" and n.path and n.path.endswith(".py")
        and n.path in configured_python and n.metadata.get("scan_status") == "read"
    ))
    # Alembic directories are excluded from ordinary Python checks by default,
    # but still belong to migration analysis. Keep their admitted inventory separate.
    migration_checks = replace(checks, admitted_python_files=tuple(sorted(
        n.path for n in graph.nodes.values()
        if n.kind == "file" and n.path and n.path.endswith(".py")
        and n.metadata.get("scan_status") == "read"
    )))
    runs = []
    for name, scanner in (("security", security.scan), ("performance", performance.scan), ("migrations", migrations.scan)):
        try:
            runs.append(ToolRun(name, findings=scanner(migration_checks if name == "migrations" else checks)))
        except Exception as exc:
            runs.append(ToolRun(name, error=f"{type(exc).__name__}: analysis failed; review the scanner diagnostics."))
    # The JS/TS scan finds SQL injection while building the graph; it is a defect in the
    # analysed code, so it joins the security findings instead of the diagnostics.
    # Kept in their own run: a crash in the Python security check must not take them with it.
    runs.append(ToolRun("stack", findings=_graph_security_findings(graph) + _graph_correctness_findings(graph)))
    _note_unexamined(graph, runs, checks, configured_python, migration_checks)
    for path in sarif_files or []:
        runs.extend(import_sarif(path, root))
    return Analysis(graph, runs, settings)


def _note_unexamined(graph: Graph, runs: list[ToolRun], checks, configured_python: set[str], migration_checks) -> None:
    """Attach "never examined" notes to each check whose inputs include a skipped file.

    A file over max_file_bytes (or one that did not parse) is not admitted to the Python
    checks, so they report nothing for it, and SARIF said `executionSuccessful: true`.
    `Analysis.complete` already turns false through the graph diagnostic; the SARIF run
    has to say so on its own, because it is often the only file a consumer reads."""
    unexamined: dict[str, str] = {}
    for issue in graph.issues:
        if issue.code not in _UNEXAMINED:
            continue
        node = graph.nodes.get(issue.node_ids[0]) if issue.node_ids else None
        path = node.path if node is not None and node.kind == "file" and node.path else issue.evidence.rsplit(":", 1)[0]
        if path.endswith(".py"):
            unexamined.setdefault(path, issue.code)
    if not unexamined:
        return
    from .scan import migrations
    try:
        roots = [r.resolve() for r in migrations.migration_roots(migration_checks)]
    except Exception:  # noqa: BLE001 - unknown roots: every skipped file may be a migration
        roots = None
    for run in runs:
        if run.error or run.skipped:
            continue
        if run.tool == "migrations":
            affected = [p for p in sorted(unexamined) if roots is None
                        or any((checks.root / p).resolve().is_relative_to(r) for r in roots)]
        elif run.tool in {"security", "performance"}:
            affected = [p for p in sorted(unexamined) if p in configured_python]
        else:
            continue  # the graph-derived `stack` run does not read Python files one by one
        # Bounded: a repository with thousands of oversized files needs a count, not a list.
        run.notes.extend(f"{p} was not examined ({unexamined[p]})." for p in affected[:20])
        if len(affected) > 20:
            run.notes.append(f"{len(affected) - 20} more Python files were not examined.")


def main(argv=None, *, config=None, prog=None) -> int:
    """`repolens analyze`: write analysis.json, report.md, report.html, linkage.mmd and findings.sarif.

    Exits 2 when the analysis is incomplete or cannot be written, 1 under `--check` for a
    P0/P1 finding that is not low confidence, and 0 otherwise."""
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--out", type=Path, help="output directory (default .repolens/analysis)")
    parser.add_argument("--query", default="")
    parser.add_argument("--max-nodes", type=int, default=30)
    parser.add_argument("--depth", type=int, default=VIEW_DEPTH,
                        help=f"relationship hops from --query matches (1-{MAX_VIEW_DEPTH}, default {VIEW_DEPTH})")
    parser.add_argument("--plugin", action="append", default=[], help="explicitly enable an installed extractor")
    parser.add_argument("--sarif", type=Path, action="append", default=[])
    parser.add_argument("--list-plugins", action="store_true")
    parser.add_argument("--check", action="store_true", help="exit 1 for high-priority medium/high-confidence findings; 2 for incomplete scans")
    args = parser.parse_args(argv)
    tool_build()  # stamp the code this process loaded
    if args.list_plugins:
        print(json.dumps(available(), indent=2))
        return 0
    if not 1 <= args.max_nodes <= 100:
        parser.error("--max-nodes must be between 1 and 100")
    if not 1 <= args.depth <= MAX_VIEW_DEPTH:
        parser.error(f"--depth must be between 1 and {MAX_VIEW_DEPTH}")
    root = config.root if config else Path.cwd()
    out = (args.out or root / ".repolens/analysis").resolve()
    if out == root.resolve():
        parser.error("--out must be a dedicated output directory")
    settings = Config.load(root)
    if out.is_relative_to(root.resolve()):
        settings.exclude_paths.add(out.relative_to(root.resolve()).as_posix())
    try:
        result = analyze(root, config=settings, plugins=args.plugin, sarif_files=args.sarif)
        out.mkdir(parents=True, exist_ok=True)
        (out / "analysis.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        (out / "report.md").write_text(result.markdown(args.query, args.max_nodes, args.depth), encoding="utf-8")
        (out / "report.html").write_text(result.html(args.query, args.max_nodes, args.depth), encoding="utf-8")
        linkage = render_mermaid(result.view(args.query, args.max_nodes, args.depth)).rstrip("\n")
        (out / "linkage.mmd").write_text(f"{linkage}\n%% produced by {describe_build()}\n", encoding="utf-8")
        (out / "findings.sarif").write_text(to_sarif(result.runs, __version__, build=tool_build()), encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        print(f"repolens analyze: {exc}")
        return 2
    print(f"Analysis complete={result.complete}; wrote {out}")
    for reason in result.incomplete_reasons():
        print(f"  incomplete: {reason}")
    print(f"  produced by {describe_build()}")
    if not result.complete:
        return 2
    if args.check and any(f.priority in {"P0", "P1"} and f.confidence != "low" for run in result.runs for f in run.findings):
        return 1
    return 0
