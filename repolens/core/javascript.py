"""Syntax-backed JS/TS facts; never load a target package or run its build.

Tree-sitter establishes syntax and lexical ownership, not runtime dispatch or type
resolution. Callers must keep those distinctions in the resulting graph.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
import re

EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
#: `fetch`-shaped calls: URL first, an options object with `method` second.
FETCH_CALLS = frozenset({"fetch", "globalThis.fetch", "window.fetch", "self.fetch", "$fetch", "ofetch"})
#: SWR hooks whose first argument is, by convention, the request URL.
SWR_CALLS = frozenset({"useSWR", "useSWRImmutable", "useSWRSubscription"})
#: Factories returning an HTTP client whose relative URLs are resolved against a base.
CLIENT_FACTORIES = {"axios.create": "baseURL", "ky.create": "prefixUrl", "ky.extend": "prefixUrl",
                    "ofetch.create": "baseURL", "$fetch.create": "baseURL"}

# Store method vocabularies. A binding is only treated as a model when it was declared
# as one (pgTable, mongoose.model, @Entity, ...) — these sets only classify the operation.
MONGO_READS = frozenset({"find", "findOne", "aggregate", "countDocuments", "estimatedDocumentCount",
                         "distinct", "watch", "listIndexes"})
MONGO_WRITES = frozenset({"insertOne", "insertMany", "updateOne", "updateMany", "replaceOne", "deleteOne",
                          "deleteMany", "bulkWrite", "findOneAndUpdate", "findOneAndDelete",
                          "findOneAndReplace", "createIndex", "createIndexes", "drop", "dropIndex", "rename"})
PRISMA_READS = frozenset({"findMany", "findUnique", "findFirst", "findUniqueOrThrow", "findFirstOrThrow",
                          "count", "aggregate", "groupBy"})
PRISMA_WRITES = frozenset({"create", "createMany", "createManyAndReturn", "update", "updateMany",
                           "updateManyAndReturn", "upsert", "delete", "deleteMany"})
MODEL_READS = MONGO_READS | PRISMA_READS | frozenset({
    "findById", "findAll", "findByPk", "findAndCountAll", "exists", "findOneBy", "findBy", "findOneOrFail",
    "findByIds", "createQueryBuilder"})
MODEL_WRITES = MONGO_WRITES | PRISMA_WRITES | frozenset({
    "findByIdAndUpdate", "findByIdAndDelete", "save", "destroy", "bulkCreate", "insert", "remove",
    "increment", "decrement", "truncate", "softDelete", "restore", "upsert"})
#: Drizzle-style builders take the table binding as an argument.
BUILDER_ARGUMENT_OPS = {"from": "reads", "leftJoin": "reads", "rightJoin": "reads", "innerJoin": "reads",
                        "fullJoin": "reads", "join": "reads", "insert": "writes", "update": "writes",
                        "delete": "writes", "getRepository": "", "InjectRepository": "", "getCustomRepository": ""}
SQL_KEYWORDS = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP|MERGE|TRUNCATE)\b", re.I)
#: An expression that names where a service lives rather than a path inside this repository.
_ORIGIN_NAME = re.compile(r"(?i)(?:api|base|backend|server|service|host)_?(?:url|uri|origin|host|base|endpoint)$|^(?:base_?url|origin)$")


@dataclass
class Symbol:
    """A function or class definition: dotted `qualified` name, line range and byte offsets."""
    name: str
    qualified: str
    line: int
    end_line: int
    start: int
    end: int
    kind: str
    exported: bool
    is_async: bool
    default_export: bool = False
    #: Public names the module exports this symbol under (`export { run as start }`).
    export_names: list[str] = field(default_factory=list)
    #: Where this file uses the symbol by value rather than by a call (`[{ loader: load }]`,
    #: `el.addEventListener("click", load)`): the top-level variable whose initializer holds
    #: the reference, or "" for any other position (a function body, a module statement).
    value_holders: list[str] = field(default_factory=list)
    #: What receives an anonymous function, when the syntax says: the JSX attribute
    #: (`onClick`) or the called function or method (`then`, `useEffect`). "" otherwise.
    role: str = ""


@dataclass
class Request:
    """An outgoing HTTP call (`fetch`, `swr` or `client` style) made by `owner`, or at module level when ""."""
    owner: str
    method: str            # upper-case verb, or UNKNOWN when the options hide it
    url: str               # path with {dynamic} for runtime substitutions
    dynamic: bool
    line: int
    receiver: str = ""     # client binding for `<receiver>.get(...)`; "" for fetch-style calls
    #: The name of the env/base-URL value the URL starts with (`NEXT_PUBLIC_API_URL`), or ""
    #: when it starts with none. The scanner decides whether that origin is this repository's API.
    configured_origin: str = ""
    style: str = "fetch"
    #: The receiver is, at this call site, the result of a hook or injection
    #: (`const { api } = useSession()`, `inject(HttpClient)`): a client whose declaration
    #: cannot be followed statically.
    hook_bound: bool = False


@dataclass
class StoreFact:
    """A table or collection that `owner` declares or touches, with its resolution and the evidence in `detail`."""
    owner: str
    kind: str              # postgres_table | mongo_collection
    name: str
    operation: str         # reads | writes | declares | "" (unknown)
    line: int
    resolution: str
    detail: str


@dataclass
class JSFacts:
    """Everything `parse_source` extracted from one JS/TS file, for the scanner to link across files."""
    symbols: list[Symbol] = field(default_factory=list)
    # (module, local binding, exported name, line); * denotes a namespace import.
    imports: list[tuple[str, str, str, int]] = field(default_factory=list)
    # (module, public name or *, imported name or *, line) for `export ... from`.
    reexports: list[tuple[str, str, str, int]] = field(default_factory=list)
    # public export name -> local binding it exports.
    exports: dict[str, str] = field(default_factory=dict)
    # (owning symbol, callee, line, relationship CALLS|RENDERS|PASSES); PASSES is a function
    # handed to a JSX attribute by reference (`<form action={save}>`).
    calls: list[tuple[str, str, int, str]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    # local HTTP client binding -> literal base URL ("" when it has none or it is not literal)
    clients: dict[str, str] = field(default_factory=dict)
    # Request receivers this file never binds (a global or injected `api`): no local router.
    free_receivers: set[str] = field(default_factory=set)
    # (owning symbol, SQL text, line, dynamic, parameterized)
    queries: list[tuple[str, str, int, bool, bool]] = field(default_factory=list)
    stores: list[StoreFact] = field(default_factory=list)
    # local binding -> the table/collection it declares (Drizzle table, Mongoose model, entity class)
    models: dict[str, StoreFact] = field(default_factory=dict)
    # (owning symbol, binding, operation, line, via): `Item.find()`, `.from(items)`
    model_refs: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    # (owning symbol, receiver, accessor, method, line): `prisma.user.findMany()`, `db.items.find()`
    member_stores: list[tuple[str, str, str, str, int]] = field(default_factory=list)
    errors: list[int] = field(default_factory=list)
    uncertain_requests: list[int] = field(default_factory=list)
    # (owning symbol, line, [(spliced expression, untrusted origin or "", inside a quoted literal)])
    # for SQL text built by string interpolation instead of bound parameters.
    sql_interpolations: list[tuple[str, int, list[tuple[str, str, bool]]]] = field(default_factory=list)
    # (method or ANY, literal path or "", line, shape, receiver) for server-side route registrations:
    # shape "server" is `app.get("/x", h)` on a server this file creates and starts listening,
    # a route the scanner can model; "router" (a router, plugin or sub-app, whose mount prefix
    # is unknown) and "controller" (NestJS `@Get()` in a `@Controller`) are not modelled.
    route_registrations: list[tuple[str, str, int, str, str]] = field(default_factory=list)


@lru_cache(maxsize=3)
def _language(kind: str):
    from tree_sitter import Language
    if kind == "javascript":
        import tree_sitter_javascript
        return Language(tree_sitter_javascript.language())
    import tree_sitter_typescript
    return Language(tree_sitter_typescript.language_tsx() if kind == "tsx"
                    else tree_sitter_typescript.language_typescript())


def available() -> bool:
    """True when tree-sitter and its JavaScript, TypeScript and TSX grammars all load."""
    try:
        for kind in ("javascript", "typescript", "tsx"):
            _language(kind)
        return True
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        return False


def pluralize(name: str) -> str:
    """The default collection/table name ODMs derive from a model name. An
    approximation of their inflection libraries, so callers mark it probable."""
    lower = name[:1].lower() + name[1:] if name[:1].isupper() and not name.isupper() else name
    lower = lower.lower()
    if re.search(r"[^aeiou]y$", lower):
        return lower[:-1] + "ies"
    if re.search(r"(?:s|x|z|ch|sh)$", lower):
        return lower + "es"
    return lower + "s"


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


_WRAPPERS = frozenset({"as_expression", "satisfies_expression", "parenthesized_expression", "non_null_expression"})


_URL_SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)
#: A client base behind a configured origin is written `<_CONFIGURED>name<NUL>path`. NUL cannot
#: appear in a URL the scanner reads (escapes are decoded to U+FFFD there), so no literal base
#: can be taken for the marker.
_CONFIGURED = "\0configured-origin:"
_JS_ESCAPE = re.compile(r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])")
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\ufffd",
                   "\n": "", "\r\n": "", "\r": "", "\u2028": "", "\u2029": ""}


def decode_escapes(raw: str) -> str:
    """The value of JS string or template source text: `\\'` is a quote, `\\n` a newline,
    `\\u{1F600}` its character, a line continuation nothing, and any other escaped character
    itself. NUL and lone surrogates become U+FFFD, so the text stays printable and encodable."""
    if "\\" not in raw:
        return raw

    def character(match) -> str:
        escape = match.group(1)
        if escape[0] == "u" and len(escape) > 1:
            code = int(escape[2:-1] if escape[1] == "{" else escape[1:], 16)
            return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF and code else "\ufffd"
        if escape[0] == "x" and len(escape) == 3:
            return chr(int(escape[1:], 16)) if escape[1:] != "00" else "\ufffd"
        return _SIMPLE_ESCAPES.get(escape, escape)

    return _JS_ESCAPE.sub(character, raw)


def configured_base(name: str, path: str) -> str:
    """The client base marker for `path` behind the configured origin `name` (see `split_configured`)."""
    return f"{_CONFIGURED}{name}\0{path}"


def split_configured(base: str) -> tuple[str, str] | None:
    """(origin name, path) of a base written by `configured_base`, else None."""
    if not base.startswith(_CONFIGURED):
        return None
    name, _, path = base[len(_CONFIGURED):].partition("\0")
    return name, path


_CHARACTER_REFERENCE = re.compile(rb"&(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);")
_FUNCTION_SCOPES = frozenset({"program", "function_declaration", "function_expression", "arrow_function",
                              "method_definition", "generator_function_declaration", "generator_function"})
#: Nodes that own a binding scope: functions, and TypeScript signatures whose parameters are declarations.
#: Blocks that scope `let`, `const` and `class` (a `var` belongs to its function).
_BLOCK_SCOPES = frozenset({"statement_block", "for_statement"})
_SCOPE_OWNERS = _FUNCTION_SCOPES | {"function_signature", "method_signature", "call_signature", "construct_signature",
                                    "function_type", "constructor_type", "abstract_method_signature"}
#: The start of an expression whose value arrives from outside the process. Shapes:
#: Express/Fastify/Next.js `req|request.query|body|params|headers|cookies|url`, Koa
#: `ctx.query|params|body` and `ctx.request.query|body|params|headers|files`, hapi
#: `request.payload`, Lambda `event.queryStringParameters|pathParameters`, fetch-API
#: `request.json()|formData()|text()`, `<any URL>.searchParams.get()`, Nuxt/h3 `getQuery()`,
#: `readBody()`, `getRouterParam()`, and browser `location.search|hash`.
_UNTRUSTED = re.compile(
    r"process\.argv\b"
    r"|(?:req|request|ctx|context|event)\.(?:query|body|params|payload|headers|cookies|url|nextUrl|queryStringParameters|pathParameters)\b"
    r"|ctx\.request\.(?:query|body|params|headers|files|rawBody)\b"
    r"|(?:req|request|ctx\.request)\.(?:json|formData|text)\s*\("
    r"|(?:[\w$]+\.)*searchParams\.(?:get|getAll)\s*\("
    r"|(?:getQuery|readBody|getRouterParams?|useSearchParams|useParams)\s*\("
    r"|router\.query\b|(?:window\.)?location\.(?:search|hash)\b")
#: Calls whose result cannot carry SQL syntax: numeric conversions, and the quoting helpers of
#: node-postgres (`escapeLiteral`/`escapeIdentifier`) and pg-format (`format.ident`/`format.literal`,
#: matched with `_QUOTING_CALL`). mysql's `escape`/`escapeId` need a SQL receiver (`_SQL_HANDLE`).
_SANITIZERS = frozenset({"Number", "parseInt", "parseFloat", "Boolean", "BigInt", "isNaN", "isFinite",
                         "escapeLiteral", "escapeIdentifier", "toFixed"})
#: `escape`/`escapeId` count on a receiver named like a SQL connection or driver (see `sql_escape`).
_SQL_HANDLE = re.compile(r"(?i)mysql|sqlstring|pool|conn|^db$|database|^client$|^sql$|^knex$")
_SQL_ESCAPE_MODULES = frozenset({"mysql", "mysql2", "sqlstring"})
_QUOTING_CALL = re.compile(r"(?:^|\.)format\.(?:ident|literal)$")
#: Operators whose result is a number or boolean, never text.
_NON_TEXT_OPERATORS = frozenset({"===", "!==", "==", "!=", "<", ">", "<=", ">=", "instanceof", "in", "-", "*", "/", "%",
                                 "**", "|", "&", "^", "<<", ">>", ">>>"})
#: Type checks that prove a value is not text: `Number.isInteger(n)`, `typeof n === "number"`.
_NUMBER_CHECKS = frozenset({"Number.isInteger", "Number.isSafeInteger", "Number.isFinite"})
_NON_TEXT_TYPES = frozenset({"number", "bigint", "boolean"})
_ROUTE_HANDLER_NAMES = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
#: Functions a framework calls with request data as `params`/`url`: Next.js route handlers
#: and default exports, Remix `loader`/`action`, SvelteKit `load` and `+server` handlers.
_INPUT_FUNCTION_NAMES = _ROUTE_HANDLER_NAMES | {"load", "loader", "action", "clientLoader", "clientAction"}
#: NestJS parameter decorators whose parameter holds request input (`@Body() dto`).
_INPUT_DECORATORS = frozenset({"Body", "Param", "Query", "Headers", "Req", "Request", "UploadedFile", "UploadedFiles"})
#: Allow-list checks: in `OK.includes(x) ? x : "id"` the guarded branch is a known value.
_GUARD_METHODS = frozenset({"includes", "has", "hasOwnProperty", "hasOwn"})
_MAX_SQL_SPELLINGS = 8
_MAX_URL_NESTING = 64


def _without_bare_ampersands(parser, data: bytes, root):
    """Re-parse with bare `&` in JSX text blanked out, or None if that does not help.

    `<p>Tools & Parts</p>` is valid JSX (React renders the `&`), but the tree-sitter
    grammar only accepts `&` as the start of a character reference, so it reports a parse
    error and sometimes loses the element. Only `&`s inside ERROR and JSX text spans are
    replaced, byte for byte, so every offset is unchanged; the new tree is kept only when
    it has fewer errors, and a real syntax error elsewhere is still reported. One recovery
    can swallow the rest of an element, so each re-parse may expose the next `&`."""
    patched, original = bytearray(data), _error_count(root)
    for _ in range(8):
        spans, stack = [], [root]
        while stack:
            node = stack.pop()
            if node.type in {"ERROR", "jsx_text"}:
                spans.append((node.start_byte, node.end_byte))
            else:
                stack.extend(node.children)
        changed = False
        for start, end in spans:
            at = data.find(b"&", start, end)
            while at != -1:
                if patched[at] == ord("&") and not _CHARACTER_REFERENCE.match(data, at):
                    patched[at] = ord("_")
                    changed = True
                at = data.find(b"&", at + 1, end)
        if not changed:
            break
        root = parser.parse(bytes(patched)).root_node
        if not root.has_error:
            return root
    return root if patched != data and _error_count(root) < original else None


#: `as import("./m").T` and `readonly import("./m").T[]`: TypeScript import types in
#: positions the tree-sitter grammar rejects (it accepts them in a `type` alias).
_TYPE_IMPORT = re.compile(rb"(?:\bas|\breadonly|[:|&<,])\s*(import\(\s*(['\"])[^'\"\n]*\2\s*\))(?=\s*\.\s*[A-Za-z_$])")


def _without_type_imports(data: bytes) -> bytes | None:
    """The source with TypeScript import types blanked to identifiers, or None if it has none.

    A valid file would otherwise report a parse error and mark the analysis incomplete.
    Only non-newline bytes are replaced, so offsets and line numbers are unchanged; the
    caller keeps the new tree only when it has fewer errors."""
    patched = bytearray(data)
    for match in _TYPE_IMPORT.finditer(data):
        for at in range(*match.span(1)):
            if patched[at] not in b"\r\n":
                patched[at] = ord("_")
    return bytes(patched) if patched != data else None


def _error_count(root) -> int:
    count, stack = 0, [root]
    while stack:
        node = stack.pop()
        count += node.type == "ERROR" or node.is_missing
        stack.extend(node.children)
    return count


def parse_source(text: str, suffix: str) -> JSFacts:
    """Extract `JSFacts` from one file's text; `suffix` selects the JavaScript, TypeScript or TSX grammar.

    Syntax errors do not raise: their lines are recorded in `errors`. Requires tree-sitter;
    check `available()` first."""
    from tree_sitter import Parser
    data = text.encode("utf-8")
    kind = "tsx" if suffix == ".tsx" else "typescript" if suffix in {".ts", ".mts", ".cts"} else "javascript"
    parser = Parser(_language(kind))
    root = parser.parse(data).root_node
    reparse = data
    if kind != "javascript" and root.has_error and (patched := _without_type_imports(data)) is not None:
        retry = parser.parse(patched).root_node
        if _error_count(retry) < _error_count(root):
            root, reparse = retry, patched
    if kind != "typescript" and root.has_error and b"&" in data:
        root = _without_bare_ampersands(parser, reparse, root) or root
    facts = JSFacts()

    def value(node) -> str:
        return data[node.start_byte:node.end_byte].decode("utf-8") if node else ""

    def unwrap(node):
        while node is not None and node.type in _WRAPPERS:
            node = node.named_children[0] if node.named_children else None
        return node

    def same(a, b) -> bool:
        return a is not None and b is not None and a.id == b.id

    # Module-level `const API_URL = "/api"`: `${API_URL}/items` is the path /api/items, not
    # a configured origin whose value is unknown.
    constants: dict[str, str] = {}
    # `const API_URL = `${process.env.BACKEND_URL}/api``: resolved lazily through url_value.
    url_constants: dict[str, object] = {}
    resolving: set[str] = set()
    # url_value per node id. A `+` chain or a constant built from constants is evaluated
    # once per node; re-evaluating a failed operand made long chains exponential.
    url_values: dict[int, tuple[str, bool, bool] | None] = {}
    # Intra-file bindings, built on first use (see declarations()).
    declared: dict[str, list] | None = None
    for statement in root.named_children:
        declaration = statement.child_by_field_name("declaration") if statement.type == "export_statement" else statement
        if declaration is None or declaration.type != "lexical_declaration" or value(declaration.children[0]) != "const":
            continue
        for declarator in declaration.named_children:
            name, initial = declarator.child_by_field_name("name"), unwrap(declarator.child_by_field_name("value"))
            if name is None or name.type != "identifier" or initial is None:
                continue
            if initial.type == "string" or (initial.type == "template_string" and not any(
                    child.type == "template_substitution" for child in initial.named_children)):
                # A template without substitutions is the same constant as a quoted string.
                constants[value(name)] = decode_escapes(value(initial)[1:-1])
            elif initial.type in {"template_string", "binary_expression"}:
                url_constants[value(name)] = initial

    def shadowed(identifier) -> bool:
        """A parameter or declaration in a function around the use rebinds a module constant's name."""
        scoped = declarations_by_scope().get(value(identifier))
        current = outer.get(identifier.id) if scoped else None
        while current is not None:
            if current.id in scoped and current.type != "program":
                return True
            current = outer.get(current.id)
        return False

    def module_constant(identifier) -> str | None:
        """The `const` string an identifier denotes where it is used, else None: the module
        constant it names, or, when a function around the use rebinds the name, the one
        function-local `const` it resolves to (a parameter or `let` is not a constant)."""
        if identifier is None or identifier.type != "identifier":
            return None
        if not shadowed(identifier):
            return constants.get(value(identifier))
        found = bindings(identifier)
        if len(found) != 1 or found[0][0] != "=" or found[0][1].type != "variable_declarator":
            return None
        holder, initial = found[0][1].parent, unwrap(found[0][2])
        if (holder is None or holder.type != "lexical_declaration" or value(holder.children[0]) != "const"
                or initial is None or initial.type not in {"string", "template_string"}
                or any(child.type == "template_substitution" for child in initial.named_children)):
            return None
        return decode_escapes(value(initial)[1:-1])

    def module_url_constant(identifier) -> bool:
        return (identifier is not None and identifier.type == "identifier" and value(identifier) in url_constants
                and not shadowed(identifier))

    def parameter_default(identifier) -> str | None:
        """The literal string default of the one parameter `identifier` names where it is
        used (`function Widget({ apiBaseUrl = "/api" })`), when nothing before the use in that
        function assigns it again. A caller may pass another value, so the use is dynamic."""
        if identifier is None or identifier.type != "identifier":
            return None
        found = bindings(identifier)
        if len(found) != 1 or found[0][0] != "param":
            return None
        parameters = found[0][1].child_by_field_name("parameters")
        name, stack = value(identifier), [parameters] if parameters is not None else []
        while stack:
            current = stack.pop()
            if current.type in {"assignment_pattern", "object_assignment_pattern"}:
                left, right = current.child_by_field_name("left"), unwrap(current.child_by_field_name("right"))
                if left is not None and left.type in {"identifier", "shorthand_property_identifier_pattern"} \
                        and value(left) == name:
                    if right is None or not (right.type == "string" or (right.type == "template_string" and not any(
                            child.type == "template_substitution" for child in right.named_children))):
                        return None
                    return decode_escapes(value(right)[1:-1])
            stack.extend(child for child in current.named_children if child is not None)
        return None

    def origin_like(node) -> bool:
        spelled = value(node)
        return ("process.env." in spelled or "import.meta.env" in spelled
                or bool(_ORIGIN_NAME.search(spelled.rsplit(".", 1)[-1])))

    def origin_name(node) -> str:
        """The name an origin-like expression reads: `NEXT_PUBLIC_API_URL` for
        `process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"`, else "origin"."""
        node = unwrap(node)
        while (node is not None and node.type == "binary_expression"
               and value(node.child_by_field_name("operator")) in {"||", "??"}):
            node = unwrap(node.child_by_field_name("left"))
        names = re.findall(r"[A-Za-z_$][\w$]*", value(node)) if node is not None else []
        return names[-1] if names else "origin"

    def empty_string(node) -> bool:
        return node is not None and node.type in {"string", "template_string"} and value(node) in {'""', "''", "``"}

    def query_like(node, depth: int = 0) -> bool:
        """A value that only appends a query string: text starting with `?` or `&`,
        `new URLSearchParams(...)`, `"?" + x`, a ternary between such a value and "", or a
        binding every assignment of which is one. It adds no path segments to a URL."""
        node = unwrap(node)
        if node is None or depth > 4:
            return False
        kind = node.type
        if kind in {"string", "template_string"}:
            return value(node)[1:2] in {"?", "&"}
        if kind == "new_expression":
            return value(node.child_by_field_name("constructor")) == "URLSearchParams"
        if kind == "binary_expression" and value(node.child_by_field_name("operator")) == "+":
            return query_like(node.child_by_field_name("left"), depth + 1)
        if kind == "call_expression":
            function = node.child_by_field_name("function")
            return (function is not None and function.type == "member_expression"
                    and value(function.child_by_field_name("property")) == "toString"
                    and query_like(function.child_by_field_name("object"), depth + 1))
        if kind == "ternary_expression":
            branches = [unwrap(node.child_by_field_name(name)) for name in ("consequence", "alternative")]
        elif kind == "identifier":
            found = bindings(node)
            if not found or any(kind_ != "=" for kind_, _, _ in found):
                return False
            branches = [unwrap(initial) for _, _, initial in found]
        else:
            return False
        queries = [query_like(branch, depth + 1) for branch in branches]
        return any(queries) and all(query or empty_string(branch) for query, branch in zip(queries, branches, strict=True))

    def template(node, placeholder=None) -> tuple[str, bool, str] | None:
        """(decoded text, has substitutions, the configured origin it starts with or "").
        Substitutions are replaced by byte span, never by a regex that breaks on nested expressions."""
        pieces, at, dynamic, origin = [], node.start_byte + 1, False, ""
        for index, child in enumerate(c for c in node.named_children if c.type == "template_substitution"):
            raw_before = data[at:child.start_byte].decode("utf-8")
            before = decode_escapes(raw_before)
            expression = child.named_children[0] if child.named_children else None
            head = index == 0 and not raw_before and expression is not None
            # A module constant is its literal value, unless it is an absolute URL whose
            # name marks it as the configured origin (`${API_BASE_URL}`), which keeps that meaning.
            constant = module_constant(expression) if placeholder is None else None
            resolved = (url_value(expression) if placeholder is None and head and module_url_constant(expression)
                        else None)
            if constant is not None and not (head and _URL_SCHEME.match(constant) and origin_like(expression)):
                pieces.extend((before, constant))
            elif resolved is not None and not resolved[1]:
                # `${API_URL}/items` where API_URL is `${process.env.X}/api`: keep the `/api`.
                origin = resolved[2]
                pieces.append(resolved[0])
            elif (head and placeholder is None and (default := parameter_default(expression)) is not None
                  and (default.startswith("/") or _URL_SCHEME.match(default))):
                # A component rendered without that prop requests the default base. Only a base
                # describes the target: `{ id = "me" }` at the head is a path segment, not a base.
                dynamic = True
                pieces.extend((before, default))
            elif head and origin_like(expression):
                origin = origin_name(expression)
            elif (placeholder is None and not head and "?" not in "".join(pieces) + before
                  and query_like(expression)):
                # `/items${query}` where query is `?status=open` or "": the path is exactly /items.
                dynamic = True
                pieces.extend((before, "?{dynamic}"))
            else:
                dynamic = True
                pieces.append(before)
                pieces.append(placeholder(index) if placeholder else "{dynamic}")
            at = child.end_byte
        pieces.append(decode_escapes(data[at:node.end_byte - 1].decode("utf-8")))
        return "".join(pieces), dynamic, origin

    def literal(node) -> tuple[str, bool] | None:
        node = unwrap(node)
        if node is None or node.type not in {"string", "template_string"}:
            return None
        if node.type == "string":
            raw, dynamic = decode_escapes(value(node)[1:-1]), False
        else:
            raw, dynamic, origin = template(node)
            if origin:
                raw, dynamic = "{dynamic}" + raw, True
        return raw, dynamic

    url_nesting: list = []

    def url_value(node) -> tuple[str, bool, str] | None:
        """(path, dynamic, configured origin name or "") for a string, template or `BASE + "/x"`."""
        node = unwrap(node)
        if node is None:
            return None
        if node.id in url_values:
            return url_values[node.id]
        # `a + b + c` nests to the left. Its operands are folded in a loop from the innermost
        # one, so a long generated chain cannot exhaust the interpreter's recursion limit.
        spine, current = [], node
        while (current is not None and current.id not in url_values and current.type == "binary_expression"
               and value(current.child_by_field_name("operator")) == "+"):
            url_values[current.id] = None  # a node reached again while it is evaluated is unresolved
            spine.append(current)
            current = unwrap(current.child_by_field_name("left"))
        if not spine:
            url_values[node.id] = None
            url_values[node.id] = evaluate_url(node)
            return url_values[node.id]
        head = url_value(current)
        for binary in reversed(spine):
            head = url_values[binary.id] = joined_url(binary, head)
        return head

    def joined_url(node, head) -> tuple[str, bool, str] | None:
        """`left + right` given the left operand's url_value. A right operand is evaluated
        recursively, so `"/a" + ("/b" + (...))` nested past `_MAX_URL_NESTING` is unresolved."""
        left = node.child_by_field_name("left")
        if len(url_nesting) >= _MAX_URL_NESTING:
            return None
        url_nesting.append(node)
        try:
            tail = url_value(node.child_by_field_name("right"))
        finally:
            url_nesting.pop()
        if head and tail and not tail[2]:
            return head[0] + tail[0], head[1] or tail[1], head[2]
        if tail and not tail[2] and origin_like(left):
            return tail[0], tail[1], origin_name(left)
        if head and "?" not in head[0] and query_like(node.child_by_field_name("right")):
            return head[0] + "?{dynamic}", True, head[2]  # `"/api/items" + query`: only a query string
        # `"/api/items/" + id` is a dynamic segment; `"/api" + path` could be any
        # number of segments, so it is not a route at all.
        if head and not head[2] and head[0].endswith("/"):
            return head[0] + "{dynamic}", True, ""
        return None

    def evaluate_url(node) -> tuple[str, bool, str] | None:
        if node.type == "string":
            return decode_escapes(value(node)[1:-1]), False, ""
        if node.type == "template_string":
            return template(node)
        if node.type == "identifier":
            name = value(node)
            if module_url_constant(node) and name not in resolving:
                resolving.add(name)
                try:
                    return url_value(url_constants[name])
                finally:
                    resolving.discard(name)
            constant = module_constant(node)
            if constant is None or (_URL_SCHEME.match(constant) and origin_like(node)):
                return None
            return constant, False, ""
        return None

    def client_origin(node) -> str:
        """The configured origin a non-literal client base names (`baseURL: process.env.API_URL`), or ""."""
        node = unwrap(node)
        return origin_name(node) if node is not None and node.type != "string" and origin_like(node) else ""

    def string_key(node) -> str:
        return value(node).strip("\"'`") if node is not None else ""

    def object_pairs(node) -> dict[str, object] | None:
        node = unwrap(node)
        if node is None or node.type != "object":
            return None
        pairs = {}
        for child in node.named_children:
            if child.type == "pair":
                pairs[string_key(child.child_by_field_name("key"))] = child.child_by_field_name("value")
            elif child.type == "shorthand_property_identifier":
                pairs[value(child)] = None  # present, value is a runtime binding
            elif child.type == "spread_element":
                pairs["..."] = None
        return pairs

    def options_method(node) -> str:
        pairs = object_pairs(node)
        if pairs is None:
            return "UNKNOWN"
        if "method" not in pairs:
            # `{ ...getHeaders() }` may carry the method; a spelled-out `method` is kept.
            return "UNKNOWN" if "..." in pairs else "GET"
        method = literal(pairs["method"]) if pairs["method"] is not None else None
        return method[0].upper() if method and not method[1] else "UNKNOWN"

    def declarator_name(node) -> str:
        name = node.child_by_field_name("name") if node is not None else None
        return value(name) if name is not None and name.type == "identifier" else ""

    def symbol_name(node) -> tuple[str, str] | None:
        kind = node.type
        if kind in {"function_declaration", "generator_function_declaration"}:
            return value(node.child_by_field_name("name")), "function"
        if kind in {"class_declaration", "abstract_class_declaration"}:
            return value(node.child_by_field_name("name")), "class"
        if kind == "method_definition":
            return string_key(node.child_by_field_name("name")), "function"
        if kind not in {"arrow_function", "function_expression", "generator_function"}:
            return None
        parent = node.parent
        name = ""
        if parent is not None:
            if parent.type == "variable_declarator":
                name = declarator_name(parent)
            elif parent.type == "pair":
                name = string_key(parent.child_by_field_name("key"))
            elif parent.type in {"public_field_definition", "field_definition"}:
                name = string_key(parent.child_by_field_name("name") or parent.child_by_field_name("property"))
            elif parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                name = value(left.child_by_field_name("property")) if left is not None and left.type == "member_expression" \
                    else value(left) if left is not None and left.type == "identifier" else ""
            elif parent.type == "export_statement":
                name = "default"
            elif (parent.type == "arguments" and parent.named_children and same(parent.named_children[0], node)
                  and parent.parent is not None and parent.parent.type == "call_expression"):
                # `export const GET = withAuth(async (req) => ...)` and `memo(() => ...)`:
                # the wrapped function is what the declared name runs.
                holder = parent.parent.parent
                if holder is not None and holder.type == "variable_declarator":
                    name = declarator_name(holder)
                elif holder is not None and holder.type == "export_statement":
                    name = "default"
        if not name and kind == "function_expression":
            name = value(node.child_by_field_name("name"))
        # Anonymous callbacks get a stable location-derived scope, preventing their
        # calls being attributed to a neighbouring top-level function.
        return name or f"anonymous@{node.start_point.row + 1}:{node.start_point.column}", "function"

    def callback_role(node) -> str:
        """`onClick` for `<button onClick={() => …}>`, `then` for `p.then(() => …)`, else ""."""
        parent = node.parent
        while parent is not None and parent.type == "parenthesized_expression":
            parent = parent.parent
        if parent is None or parent.parent is None:
            return ""
        if parent.type == "jsx_expression" and parent.parent.type == "jsx_attribute":
            name = next((c for c in parent.parent.named_children if c.type == "property_identifier"), None)
            return value(name) if name is not None else ""
        if parent.type == "arguments" and parent.parent.type == "call_expression":
            function = parent.parent.child_by_field_name("function")
            if function is not None and function.type == "member_expression":
                return value(function.child_by_field_name("property"))
            if function is not None and function.type == "identifier":
                return value(function)
        return ""

    def export_status(node) -> tuple[bool, bool]:
        current = node.parent
        if current is not None and current.type == "arguments" and current.parent is not None \
                and current.parent.type == "call_expression":
            current = current.parent.parent
        member = False  # `module.exports = { a() {} }`: `a` is a named member, not the default export
        while current is not None and (current.type in _WRAPPERS or current.type in {"pair", "object"}):
            member = member or current.type in {"pair", "object"}
            current = current.parent
        while current is not None and current.type in {"variable_declarator", "lexical_declaration", "variable_declaration"}:
            current = current.parent
        if current is not None and current.type == "assignment_expression":
            left = value(current.child_by_field_name("left"))
            return left.startswith(("module.exports", "exports.")), left == "module.exports" and not member
        if current is not None and current.type == "export_statement":
            return True, not member and any(child.type == "default" for child in current.children)
        return False, False

    # One iterative walk records lexical scope and the innermost owning symbol for every
    # node. Looking the owner up per call afterwards was quadratic on minified bundles.
    stack = [(root, (), "", None)]
    order = []
    # The nearest enclosing function or signature of every node, by node id. A tree-sitter
    # `.parent` costs O(depth), so walking parents from a deeply nested use was cubic.
    outer: dict[int, object] = {}
    while stack:
        node, scope, owner, lexical = stack.pop()
        order.append((node, owner))
        if lexical is not None:
            outer[node.id] = lexical
        if node.type == "ERROR" or node.is_missing:
            facts.errors.append(node.start_point.row + 1)
        child_scope, child_owner = scope, owner
        named = symbol_name(node)
        if named and named[0]:
            name, symbol_kind = named
            qualified = ".".join((*scope, name))
            exported, default_export = export_status(node)
            facts.symbols.append(Symbol(name, qualified, node.start_point.row + 1, node.end_point.row + 1,
                                        node.start_byte, node.end_byte, symbol_kind, exported,
                                        any(c.type == "async" for c in node.children), default_export,
                                        role=callback_role(node) if name.startswith("anonymous@") else ""))
            child_scope, child_owner = (*scope, name), qualified
        elif node.type == "variable_declarator":
            # `export const api = { list: () => fetch(...) }`: members are `api.list`,
            # so two objects' `onClick` handlers stay two symbols.
            target = unwrap(node.child_by_field_name("value"))
            if target is not None and target.type == "object" and declarator_name(node):
                child_scope = (*scope, declarator_name(node))
        child_lexical = node if node.type in _SCOPE_OWNERS or node.type in _BLOCK_SCOPES else lexical
        stack.extend((child, child_scope, child_owner, child_lexical) for child in reversed(node.named_children))

    _collect_exports(facts, order, value, unwrap, literal)

    # Intra-file bindings (`declared`, built on first use): SQL is often assembled in variables
    # (`const where = id ? \`AND id = '${id}'\` : ""`) before it reaches `.query()`.

    def enclosing(node):
        current = outer.get(node.id)
        while current is not None and current.type not in _FUNCTION_SCOPES:
            current = outer.get(current.id)
        return current

    def pattern_names(pattern):
        stack = [pattern]
        while stack:
            current = stack.pop()
            if current.type in {"identifier", "shorthand_property_identifier_pattern"}:
                yield current
            elif current.type in {"assignment_pattern", "object_assignment_pattern"}:
                stack.append(current.child_by_field_name("left"))
            elif current.type == "pair_pattern":
                stack.append(current.child_by_field_name("value"))
            elif current.type != "type_annotation":
                stack.extend(child for child in current.named_children if child is not None)

    def declarations() -> dict[str, list]:
        nonlocal declared
        if declared is None:
            declared = {}
            for node, _owner in order:
                kind = node.type
                if kind == "variable_declarator" and node.child_by_field_name("name") is not None:
                    name, initial = node.child_by_field_name("name"), node.child_by_field_name("value")
                    # `let`/`const` belong to the innermost block, `var` to its function.
                    scope = outer.get(node.id) if node.parent is not None and node.parent.type == "lexical_declaration" \
                        else enclosing(node)
                    for leaf in ([name] if name.type == "identifier" else pattern_names(name)):
                        declared.setdefault(value(leaf), []).append(
                            ("=" if leaf is name else "part", scope, node, initial))
                elif kind in {"assignment_expression", "augmented_assignment_expression"}:
                    left = node.child_by_field_name("left")
                    if left is not None and left.type == "identifier":
                        operator = "=" if kind == "assignment_expression" else value(node.child_by_field_name("operator"))
                        declared.setdefault(value(left), []).append((operator, None, node, node.child_by_field_name("right")))
                elif kind == "formal_parameters" and node.parent is not None:
                    for leaf in pattern_names(node):
                        declared.setdefault(value(leaf), []).append(("param", node.parent, node.parent, None))
        return declared

    # declarations() indexed for lookups: name -> {scope node id: entries}, and name -> the
    # assignments (no scope) in source order with their start offsets. Scanning every
    # declaration of a name per use was quadratic (5,000 functions each taking `id`).
    by_scope: dict[str, dict[int, list]] | None = None
    assignments: dict[str, tuple[list[int], list]] = {}

    def declarations_by_scope() -> dict[str, dict[int, list]]:
        nonlocal by_scope
        if by_scope is None:
            by_scope = {}
            for name, entries in declarations().items():
                for entry in entries:
                    if entry[1] is not None:
                        by_scope.setdefault(name, {}).setdefault(entry[1].id, []).append(entry)
                    else:
                        starts, assigned = assignments.setdefault(name, ([], []))
                        starts.append(entry[2].start_byte)
                        assigned.append(entry)
        return by_scope

    def bindings(identifier) -> list[tuple[str, object, object]]:
        """(kind, anchor, value) for what `identifier` can hold where it is used: the
        innermost enclosing declaration plus the assignments to it that precede the use."""
        use, name = identifier.start_byte, value(identifier)
        scoped = declarations_by_scope().get(name)

        def before(anchor) -> bool:
            return anchor.start_byte < use and not anchor.start_byte <= use < anchor.end_byte

        found, best = [], outer.get(identifier.id) if scoped else None
        while best is not None:
            found = [(kind, anchor, initial) for kind, _scope, anchor, initial in scoped.get(best.id, ())
                     if kind == "param" or before(anchor)]
            if found:
                break
            best = outer.get(best.id)
        if best is None:
            return []
        starts, assigned = assignments.get(name, ((), ()))
        found += [(kind, anchor, initial) for kind, _scope, anchor, initial
                  in assigned[bisect_left(starts, best.start_byte):bisect_left(starts, use)]
                  if before(anchor) and assigns_to(anchor, scoped, best)]
        return found

    def assigns_to(assignment, scoped, scope) -> bool:
        """`assignment` writes the binding declared in `scope`: no scope between them declares
        the name again (a parameter or local of a nested function is another variable)."""
        left = assignment.child_by_field_name("left")
        current = outer.get(left.id) if left is not None else None
        while current is not None and current.id != scope.id:
            if current.id in scoped:
                return False
            current = outer.get(current.id)
        return True

    # sql_spellings per (node, depth). `sql = sql + ...` repeated reaches the same bindings
    # through every later use, and expanding them again each time was exponential.
    spelled_memo: dict[tuple[int, int], list[list]] = {}

    def sql_spellings(node, depth: int = 0) -> list[list]:
        """Ways a string-valued expression can be spelled: lists of text and ("hole", node).

        `depth` counts binding hops and nested substitutions, not operands of one `a + b + c`
        chain: that chain is folded in a loop, so a long concatenation keeps its SQL keyword."""
        node = unwrap(node)
        if node is None:
            return [[""]]
        if depth > 6:
            return [[("hole", node)]]
        key = (node.id, depth)
        if key not in spelled_memo:
            spelled_memo[key] = [[("hole", node)]]  # a node reached again while it is spelled is a hole
            spelled_memo[key] = spell(node, depth)
        return spelled_memo[key]

    def joined(spellings: list[list], parts: list[list]) -> list[list]:
        return [[*s, *p] for s in spellings for p in parts][:_MAX_SQL_SPELLINGS]

    def spell(node, depth: int) -> list[list]:
        kind = node.type
        if kind == "string":
            return [[value(node)[1:-1]]]
        if kind in {"number", "true", "false", "null"}:
            return [[value(node)]]
        if kind == "template_string":
            spellings, at = [[]], node.start_byte + 1
            for child in node.named_children:
                if child.type != "template_substitution":
                    continue
                inner = child.named_children[0] if child.named_children else None
                text_before = data[at:child.start_byte].decode("utf-8")
                spellings = joined(spellings, [[text_before, *p] for p in sql_spellings(inner, depth + 1)])
                at = child.end_byte
            tail = data[at:node.end_byte - 1].decode("utf-8")
            return [[*s, tail] for s in spellings]
        operator = value(node.child_by_field_name("operator")) if kind == "binary_expression" else ""
        if operator == "+":
            spine, current = [], node
            while (current is not None and current.type == "binary_expression"
                   and value(current.child_by_field_name("operator")) == "+"):
                spine.append(current)
                current = unwrap(current.child_by_field_name("left"))
            spellings = sql_spellings(current, depth)
            for binary in reversed(spine):
                right = unwrap(binary.child_by_field_name("right"))
                nested = right is not None and right.type == "binary_expression"
                spellings = joined(spellings, sql_spellings(right, depth + 1 if nested else depth))
            return spellings
        if operator in {"||", "??"}:
            return (sql_spellings(node.child_by_field_name("left"), depth + 1)
                    + sql_spellings(node.child_by_field_name("right"), depth + 1))[:_MAX_SQL_SPELLINGS]
        if kind == "ternary_expression":
            return (sql_spellings(node.child_by_field_name("consequence"), depth + 1)
                    + sql_spellings(node.child_by_field_name("alternative"), depth + 1))[:_MAX_SQL_SPELLINGS]
        if kind == "identifier":
            found = bindings(node)
            if found and all(kind_ in {"=", "+="} for kind_, _, _ in found):
                spellings: list[list] = []
                for kind_, _, initial in found:
                    if kind_ == "=" and len(spellings) < _MAX_SQL_SPELLINGS:
                        spellings += sql_spellings(initial, depth + 1)
                spellings = [list(s) for s in spellings[:_MAX_SQL_SPELLINGS]] or [[""]]
                for kind_, _, initial in found:
                    if kind_ == "+=":
                        parts = sql_spellings(initial, depth + 1)
                        if len(parts) == 1:
                            for s in spellings:  # own copies: extend in place, not a new list per `+=`
                                s.extend(parts[0])
                        else:
                            spellings = joined(spellings, parts)
                if any(isinstance(piece, str) and piece for s in spellings for piece in s):
                    return spellings
        return [[("hole", node)]]

    def sql_text(node) -> tuple[str, list[tuple[object, bool]]] | None:
        """(the fullest spelling with `$n` for each spliced value, [(value, inside a quoted literal)])."""
        spelled = []
        for spelling in sql_spellings(node):
            pieces, holes, quotes = [], [], 0
            for piece in spelling:
                if isinstance(piece, str):
                    # Source text: `'… = \'' + x + '\''` quotes the value. Decoded escapes never
                    # hold NUL (U+FFFD instead), which marks a hole here.
                    piece = decode_escapes(piece)
                    pieces.append(piece)
                    quotes += piece.count("'")
                else:
                    holes.append((piece[1], quotes % 2 == 1))
                    pieces.append("\0")
            spelled.append(("".join(pieces), holes))
        text = max((text for text, _ in spelled), key=len)
        start = max((int(number) for number in re.findall(r"\$(\d+)", text)), default=0)
        first, *rest = text.split("\0")
        text = first + "".join(f"${start + index + 1}{piece}" for index, piece in enumerate(rest))
        holes = {}
        for _, spliced in spelled:
            for hole, quoted in spliced:
                holes.setdefault((hole.start_byte, hole.end_byte), (hole, quoted))
        return text, [holes[key] for key in sorted(holes)]

    def function_name(function) -> str:
        name, parent = function.child_by_field_name("name"), function.parent
        if name is None and parent is not None and parent.type == "variable_declarator":
            name = parent.child_by_field_name("name")
        return value(name) if name is not None else ""

    def route_handler(function) -> bool:
        parent = function.parent
        default = parent is not None and parent.type == "export_statement" and any(c.type == "default" for c in parent.children)
        return default or function_name(function) in _INPUT_FUNCTION_NAMES

    def input_parameter(function, name: str) -> bool:
        """`name` is request data the framework passes to `function`: `{ params }` of a route
        handler or loader, `{ url }` of a named handler or loader, or a NestJS `@Body() name`."""
        if name in {"params", "searchParams"} and route_handler(function):
            return True
        if name == "url" and function_name(function) in _INPUT_FUNCTION_NAMES:
            return True
        parameters = function.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else ():
            decorated = any(child.type == "decorator" and child.named_children and value(
                child.named_children[0].child_by_field_name("function") if child.named_children[0].type == "call_expression"
                else child.named_children[0]) in _INPUT_DECORATORS for child in parameter.children)
            if decorated and any(leaf.type == "identifier" and value(leaf) == name for leaf in parameter.named_children):
                return True
        return False

    def literal_entries(table) -> bool:
        """An object, array, `new Set([...])`, `new Map([...])`, `Object.freeze(...)` or
        `Object.keys/values(<literal table>)` whose entries are all literals."""
        table = unwrap(table)
        if table is not None and table.type in {"call_expression", "new_expression"}:
            callee = value(table.child_by_field_name("function") or table.child_by_field_name("constructor"))
            arguments = table.child_by_field_name("arguments")
            if arguments is None or len(arguments.named_children) != 1:
                return False
            if callee in {"Object.keys", "Object.values"}:
                return literal_table(arguments.named_children[0])
            if callee not in {"Object.freeze", "Set", "Map"}:
                return False
            table = unwrap(arguments.named_children[0])
        if table is None or table.type not in {"object", "array"}:
            return False
        for entry in table.named_children:
            leaf = unwrap(entry.child_by_field_name("value")) if entry.type == "pair" else unwrap(entry)
            if leaf is not None and leaf.type == "array" and table.type == "array":  # `new Map([["a", "x"]])`
                if all(unwrap(item) is not None and unwrap(item).type in {"string", "number"} for item in leaf.named_children):
                    continue
            if leaf is None or leaf.type not in {"string", "number", "true", "false", "null"} or entry.type == "spread_element":
                return False
        return True

    def literal_table(node) -> bool:
        """An allow-list lookup: an inline literal table (`["name", "date"]`), or a `const`
        bound only to one (see `literal_entries`)."""
        node = unwrap(node)
        if node is None:
            return False
        if node.type != "identifier":
            return literal_entries(node)
        found = bindings(node)
        if not found:
            return False
        for kind_, anchor, initial in found:
            holder = anchor.parent if anchor is not None else None
            if (kind_ != "=" or holder is None or holder.type != "lexical_declaration"
                    or value(holder.children[0]) != "const" or not literal_entries(initial)):
                return False
        return True

    def allow_listed(condition):
        """(the expression a condition proves is in a fixed set, whether the proof is negated)."""
        condition, negated = unwrap(condition), False
        while condition is not None and condition.type == "unary_expression" and value(condition.child_by_field_name("operator")) == "!":
            condition, negated = unwrap(condition.child_by_field_name("argument")), not negated
        if condition is None:
            return None, negated
        operator = value(condition.child_by_field_name("operator")) if condition.type == "binary_expression" else ""
        if operator in {"&&", "||"}:
            # `A && B ? x : y` proves what A or B proves for the consequence; `!A || B ? y : x`
            # proves what a negated A (or B) proves for the alternative.
            for side in ("left", "right"):
                guarded, side_negated = allow_listed(condition.child_by_field_name(side))
                if guarded is not None and side_negated == (operator == "||"):
                    return guarded, side_negated != negated
            return None, negated
        if operator in {"===", "==", "!==", "!="}:
            # `typeof n === "number"`, either way round.
            left, right = unwrap(condition.child_by_field_name("left")), unwrap(condition.child_by_field_name("right"))
            for check, kind_name in ((left, right), (right, left)):
                if (check is not None and kind_name is not None and check.type == "unary_expression"
                        and value(check.child_by_field_name("operator")) == "typeof" and kind_name.type == "string"
                        and value(kind_name)[1:-1] in _NON_TEXT_TYPES):
                    return value(unwrap(check.child_by_field_name("argument"))), negated != operator.startswith("!")
            return None, negated
        if operator == "in":
            if literal_table(condition.child_by_field_name("right")):
                return value(unwrap(condition.child_by_field_name("left"))), negated
            return None, negated
        if condition.type != "call_expression":
            return None, negated
        callee = condition.child_by_field_name("function")
        arguments = condition.child_by_field_name("arguments")
        args = [unwrap(arg) for arg in arguments.named_children] if arguments is not None else []
        if callee is not None and value(callee) in _NUMBER_CHECKS and len(args) == 1:
            return value(args[0]), negated
        if callee is None or callee.type != "member_expression" or not args:
            return None, negated
        method, table = value(callee.child_by_field_name("property")), callee.child_by_field_name("object")
        spelled = value(callee)
        if spelled in {"Object.hasOwn", "Object.prototype.hasOwnProperty.call"} and len(args) == 2 and literal_table(args[0]):
            return value(args[1]), negated
        if method in _GUARD_METHODS and literal_table(table):
            return value(args[0]), negated
        return None, negated

    def guarded_branch(node) -> bool:
        """`node` is the branch of `OK.includes(x) ? x : "id"` its condition proves allow-listed.

        sql_spellings splits a ternary into one spelling per branch, so the spliced hole is the
        branch itself and the condition has to be found from it."""
        child, parent = node, node.parent
        while parent is not None and parent.type in _WRAPPERS:
            child, parent = parent, parent.parent
        if parent is None or parent.type != "ternary_expression":
            return False
        guarded, negated = allow_listed(parent.child_by_field_name("condition"))
        if guarded is None:
            return False
        for field_name in ("consequence", "alternative"):
            if same(parent.child_by_field_name(field_name), child):
                return value(unwrap(child)) == guarded and (field_name == "alternative") == negated
        return False

    # Per block or program: [("after", if statement, value) for `if (!OK.includes(x)) throw …`,
    # ("inside", consequence, value) for `if (OK.includes(x)) { … }`].
    block_guards: dict[int, list[tuple[str, object, str]]] = {}

    def exits(statement) -> bool:
        """`statement` always leaves the block: throw, return, break or continue, or a block ending in one."""
        statement = unwrap(statement)
        if statement is None:
            return False
        if statement.type in {"throw_statement", "return_statement", "break_statement", "continue_statement"}:
            return True
        if statement.type == "statement_block":
            children = [child for child in statement.named_children if child.type != "comment"]
            return bool(children) and exits(children[-1])
        return False

    def guards(block) -> list[tuple[str, object, str]]:
        if block.id not in block_guards:
            found = []
            for statement in block.named_children:
                if statement.type != "if_statement":
                    continue
                guarded, negated = allow_listed(statement.child_by_field_name("condition"))
                consequence = statement.child_by_field_name("consequence")
                if guarded is None or consequence is None:
                    continue
                if not negated:
                    found.append(("inside", consequence, guarded))
                elif statement.child_by_field_name("alternative") is None and exits(consequence):
                    found.append(("after", statement, guarded))
            block_guards[block.id] = found
        return block_guards[block.id]

    def statement_guarded(node) -> bool:
        """`node` is a value an `if` proves allow-listed where it is used: inside the consequence
        of `if (OK.includes(x))`, or after `if (!OK.includes(x)) throw …` in an enclosing block,
        with no assignment to it after the proof."""
        if node.type not in {"identifier", "member_expression", "subscript_expression"}:
            return False
        spelled, current = value(node), node
        while current.parent is not None:
            block = current.parent
            if block.type in {"statement_block", "program"}:
                for shape, anchor, guarded in guards(block):
                    if guarded != spelled:
                        continue
                    proven = (anchor.end_byte <= node.start_byte if shape == "after"
                              else anchor.start_byte <= node.start_byte < anchor.end_byte)
                    since = anchor.end_byte if shape == "after" else anchor.start_byte
                    if proven and not (node.type == "identifier" and any(
                            kind_ in {"=", "+="} and anchor_ is not None and since <= anchor_.start_byte < node.start_byte
                            for kind_, anchor_, _ in bindings(node))):
                        return True
            current = block
        return False

    def imported_from(name: str) -> str | None:
        """The module a top-level import or require binds `name` from, else None."""
        return next((module for module, local, _exported, _line in facts.imports if local == name), None)

    def quoting_format(call) -> bool:
        """pg-format `format("SELECT * FROM %I WHERE a = %L", t, a)`: every value is quoted.

        The callee is a bare name that is pg-format's, or a `format` imported from nowhere
        else; `util.format` and a date library's `format` splice their arguments verbatim."""
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        first = unwrap(arguments.named_children[0]) if arguments is not None and arguments.named_children else None
        if function is None or function.type != "identifier" or first is None or first.type != "string":
            return False
        module = imported_from(value(function))
        if module != "pg-format" and (value(function) != "format" or module is not None):
            return False
        specifiers = re.findall(r"%(.)", value(first)[1:-1].replace("%%", ""))
        return bool(specifiers) and set(specifiers) <= {"I", "L"}

    def element_free(function) -> bool:
        """A callback whose result does not use the element it is given: `(_, i) => `$${i + 1}``."""
        function = unwrap(function)
        if function is None or function.type not in {"arrow_function", "function_expression"}:
            return False
        parameters = function.child_by_field_name("parameters") or function.child_by_field_name("parameter")
        listed = ([parameters] if parameters is not None and parameters.type == "identifier"
                  else list(parameters.named_children) if parameters is not None else [])
        # The element (first) and the array itself (third) both carry the values; only the index does not.
        names = {"arguments"} | {value(leaf) for index, parameter in enumerate(listed) if index != 1
                                 for leaf in pattern_names(parameter)}
        body = function.child_by_field_name("body")
        return body is not None and not any(node.type == "this" or (node.type == "identifier" and value(node) in names)
                                            for node in walk(body))

    def element_free_values(call):
        """What a list built without reading its elements splices in, or None for another call:
        `ids.map((_, i) => `$${i + 1}`).join(",")` is only placeholders and the separator, however
        many ids a request sends. Also `new Array(n).fill("?")` and `Array.from(ids, () => "?")`."""
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        args = [unwrap(arg) for arg in arguments.named_children] if arguments is not None else []
        callee = value(function)
        if callee == "Array.from" and len(args) >= 2 and element_free(args[1]):
            return [args[1].child_by_field_name("body")]
        if function is None or function.type != "member_expression":
            return None
        method, target = value(function.child_by_field_name("property")), unwrap(function.child_by_field_name("object"))
        if method in {"map", "flatMap"} and args and element_free(args[0]):
            return [args[0].child_by_field_name("body"), *args[1:]]
        if method == "fill" and len(args) == 1:
            return args  # with a start index the elements before it are kept
        if method == "join" and target is not None and target.type == "call_expression":
            inner = element_free_values(target)
            return None if inner is None else [*inner, *args]
        return None

    # untrusted() per binding initializer. `const x1 = x0 + x0` reuses a binding; following each
    # use again made the walk exponential in the chain length. A binding reached again while it
    # is evaluated (`let x = y; y = x`) counts as "" so the walk ends.
    # A result cut short by the depth limit is kept with the depth it was computed at and
    # reused only at that depth or deeper, so the answer does not depend on query order.
    taint_origins: dict[int, tuple[str, int, bool]] = {}
    truncations = [0]

    def initializer_taint(initial, depth: int) -> str:
        known = taint_origins.get(initial.id)
        if known is not None and (known[0] or not known[2] or depth >= known[1]):
            truncations[0] += known[2] and not known[0]
            return known[0]
        taint_origins[initial.id] = ("", depth, False)
        before = truncations[0]
        origin = untrusted(initial, depth + 1)
        taint_origins[initial.id] = (origin, depth, truncations[0] > before)
        return origin

    def sql_escape(callee: str) -> bool:
        """mysql/mysql2/sqlstring `escape`/`escapeId`, on a receiver named or imported like a
        SQL connection, or the global `escape` (it encodes quotes and spaces). An HTML or
        CSS escaper (`_.escape`, `validator.escape`, `CSS.escape`) leaves SQL syntax intact."""
        receiver, _, method = callee.rpartition(".")
        if method not in {"escape", "escapeId"}:
            return False
        if not receiver:
            return method == "escape" and imported_from(method) is None and not declarations().get(method)
        head = receiver.rsplit(".", 1)[-1]
        return bool(_SQL_HANDLE.search(head) or (imported_from(head) or "").split("/")[0] in _SQL_ESCAPE_MODULES)

    def untrusted(node, depth: int = 0) -> str:
        """Where a value comes from when that is outside the process, else ""."""
        stack = [unwrap(node)]
        while stack:
            current = stack.pop()
            if current is None or guarded_branch(current) or statement_guarded(current):
                continue
            if depth > 48:
                truncations[0] += 1
                continue
            kind = current.type
            if kind in _FUNCTION_SCOPES or kind in {"string", "number", "true", "false", "null"}:
                continue
            if kind == "subscript_expression" and literal_table(current.child_by_field_name("object")):
                continue  # `COLUMNS[req.query.sort]` is one of the table's literal values, or undefined
            if kind == "ternary_expression":
                # The condition is not the result. A branch the condition proves is in a fixed
                # set (`ALLOWED.includes(x) ? x : "id"`) is that set's value.
                guarded, negated = allow_listed(current.child_by_field_name("condition"))
                for field_name in ("consequence", "alternative"):
                    branch = unwrap(current.child_by_field_name(field_name))
                    if not (guarded is not None and branch is not None and value(branch) == guarded
                            and (field_name == "alternative") == negated):
                        stack.append(branch)
                continue
            if kind in {"call_expression", "member_expression"}:
                if kind == "call_expression":
                    callee = value(current.child_by_field_name("function"))
                    if (callee.rsplit(".", 1)[-1] in _SANITIZERS or callee.startswith("Math.") or sql_escape(callee)
                            or _QUOTING_CALL.search(callee) or quoting_format(current)):
                        continue
                    if (instead := element_free_values(current)) is not None:
                        stack.extend(instead)  # only the callback's own result and separators are spliced
                        continue
                elif value(current.child_by_field_name("property")) == "length":
                    continue
                if match := _UNTRUSTED.match(value(current)):
                    return "a command-line argument" if match.group(0).startswith("process") else "HTTP request input"
            if kind in {"unary_expression", "update_expression"}:
                continue  # `+n`, `-n`, `~~n`, `!x`, `typeof x`, `n++`: a number, boolean or type name
            if kind == "binary_expression" and value(current.child_by_field_name("operator")) in _NON_TEXT_OPERATORS:
                continue
            if kind == "identifier":
                for kind_, anchor, initial in bindings(current):
                    if kind_ == "param":
                        if input_parameter(anchor, value(current)):
                            return "HTTP request input"
                    elif initial is not None and (origin := initializer_taint(initial, depth)):
                        return origin
                continue
            stack.extend(current.named_children)
        return ""

    by_qualified = {}
    for symbol in facts.symbols:
        by_qualified.setdefault(symbol.qualified, []).append(symbol)
    for public, local in facts.exports.items():
        for symbol in by_qualified.get(local, ()):
            symbol.exported = True
            symbol.default_export = symbol.default_export or public == "default"
            if public not in symbol.export_names:
                symbol.export_names.append(public)

    def value_holder(identifier) -> str | None:
        """Where a same-file use by value sits: the top-level variable whose initializer holds
        it, "" elsewhere, or None when the identifier declares or exports the name instead."""
        parent = identifier.parent
        if parent is None or parent.type in {
                "import_specifier", "import_clause", "namespace_import", "export_specifier", "export_statement",
                "formal_parameters", "required_parameter", "optional_parameter", "pair_pattern", "object_pattern",
                "array_pattern", "assignment_pattern", "object_assignment_pattern", "labeled_statement",
                "function_declaration", "generator_function_declaration", "class_declaration", "function_expression"}:
            return None
        if parent.type == "variable_declarator" and same(parent.child_by_field_name("name"), identifier):
            return None
        holder = parent.parent if parent.type in {"pair", "object"} else parent
        if parent.type == "pair":
            holder = holder.parent if holder is not None else None  # pair -> object -> its holder
        if holder is not None and holder.type == "assignment_expression":
            target = value(holder.child_by_field_name("left"))
            if same(holder.child_by_field_name("left"), identifier):
                return None
            if target == "module.exports" or target.startswith(("module.exports.", "exports.")):
                return None  # `module.exports = { load }` exports it; it is not a use
        current = parent
        while current is not None:
            if current.type in _FUNCTION_SCOPES and current.type != "program" or current.type == "class_body":
                return ""
            if current.type == "variable_declarator":
                declaration = current.parent
                top = declaration.parent if declaration is not None else None
                if top is not None and top.type == "export_statement":
                    top = top.parent
                if top is not None and top.type == "program":
                    return declarator_name(current)
            if current.type == "program":
                return ""
            current = current.parent
        return ""

    top_level = {}
    for symbol in facts.symbols:
        if symbol.exported and "." not in symbol.qualified and not symbol.name.startswith("anonymous@"):
            top_level.setdefault(symbol.name, []).append(symbol)
    if top_level:
        for node, _owner in order:
            if node.type in {"identifier", "shorthand_property_identifier"} and value(node) in top_level:
                holder = value_holder(node)
                for symbol in top_level[value(node)] if holder is not None else ():
                    if holder not in symbol.value_holders:
                        symbol.value_holders.append(holder)

    def hook_client(call) -> bool:
        """`call` hands over a value that may be an HTTP client (see `_HOOK_CALL`)."""
        call = unwrap(call)
        if call is not None and call.type == "await_expression" and call.named_children:
            call = unwrap(call.named_children[0])
        if call is None or call.type != "call_expression":
            return False
        callee = value(call.child_by_field_name("function")).rsplit(".", 1)[-1]
        if callee in _STATE_HOOKS:
            arguments = value(call.child_by_field_name("arguments"))
            return any(factory + "(" in arguments for factory in CLIENT_FACTORIES)
        return bool(_HOOK_CALL.fullmatch(callee)) and callee not in _NOT_CLIENT_HOOKS

    def hook_bound(receiver) -> bool:
        """At this call site the receiver is the result, or a destructured part of the result,
        of a hook or injection call. A same-named parameter elsewhere is not."""
        receiver = unwrap(receiver)
        if receiver is None:
            return False
        if receiver.type == "identifier":
            return any(kind_ in {"=", "part"} and anchor is not None and anchor.type == "variable_declarator"
                       and hook_client(anchor.child_by_field_name("value")) for kind_, anchor, _ in bindings(receiver))
        if receiver.type == "member_expression" and value(receiver.child_by_field_name("object")) == "this":
            return injected_http_client(receiver, value(receiver.child_by_field_name("property")))
        return False

    def injected_http_client(member, field_name: str) -> bool:
        """Angular `http = inject(HttpClient)` or `constructor(private http: HttpClient)` in the enclosing class."""
        body = member.parent
        while body is not None and body.type != "class_body":
            body = body.parent
        for item in body.named_children if body is not None else ():
            if item.type in {"public_field_definition", "field_definition"}:
                name = item.child_by_field_name("name") or item.child_by_field_name("property")
                initial = unwrap(item.child_by_field_name("value"))
                if (name is not None and value(name) == field_name and initial is not None and initial.type == "call_expression"
                        and value(initial.child_by_field_name("function")) == "inject"
                        and value(initial.child_by_field_name("arguments")).strip("() ") == "HttpClient"):
                    return True
            elif item.type == "method_definition" and value(item.child_by_field_name("name")) == "constructor":
                parameters = item.child_by_field_name("parameters")
                for parameter in parameters.named_children if parameters is not None else ():
                    names = [value(c) for c in parameter.named_children if c.type == "identifier"]
                    annotation = next((c for c in parameter.named_children if c.type == "type_annotation"), None)
                    if (field_name in names and annotation is not None and value(annotation).lstrip(": ") == "HttpClient"
                            and any(c.type == "accessibility_modifier" or value(c) == "readonly" for c in parameter.children)):
                        return True
        return False

    server_import: list[bool] = []

    def route_shape(receiver, args) -> str:
        """How `<receiver>.<verb>(path, ...)` registers a server route, or "" for a request.

        "server": the receiver is bound from a server factory (`express()`, `fastify()`,
        `new Hono()`); "router": bound from a router factory (`express.Router()`), or unbound
        or a parameter named like one (`router`, `app`) that is given a handler function, or
        any handler when the file imports a server framework. A receiver bound to another
        value is not a router unless the file imports a server framework. `$.get(url, callback)`
        and a client binding take a callback too, and are requests."""
        receiver = unwrap(receiver)
        if receiver is None or receiver.type != "identifier" or len(args) < 2:
            return ""
        name = value(receiver)
        if name in _CALLBACK_CLIENTS or name in facts.clients:
            return ""
        if not server_import:
            server_import.append(bool(_SERVER_IMPORT.search(text)))
        other_value = parameter = False
        for kind_, _anchor, initial in bindings(receiver):
            target = unwrap(initial)
            if target is not None and target.type == "await_expression" and target.named_children:
                target = unwrap(target.named_children[0])
            parameter = parameter or kind_ == "param"
            if kind_ != "=" or target is None or target.type not in {"call_expression", "new_expression"}:
                other_value = other_value or kind_ in {"=", "part"}
                continue
            callee = value(target.child_by_field_name("function") or target.child_by_field_name("constructor"))
            last = callee.rsplit(".", 1)[-1]
            if last in _ROUTER_FACTORIES or _REQUIRED_SERVER.fullmatch(callee):
                return "server" if last in _SERVER_FACTORIES or _REQUIRED_SERVER.fullmatch(callee) else "router"
            if callee in CLIENT_FACTORIES or hook_client(target):
                return ""
            other_value = True
        # `const app = new Map()` or `const router = createNavigator()` is not a server router,
        # unless the file imports a server framework (a local factory may build one).
        if name not in _ROUTER_NAMES or (other_value and not server_import[0]):
            return ""
        handler = any(unwrap(arg) is not None and unwrap(arg).type in {"arrow_function", "function_expression", "function"}
                      for arg in args[1:])
        # `module.exports = function (app) { app.get("/orders", orders.list); }`: a router handed in
        # by the caller, given a handler by reference.
        by_reference = parameter and any(unwrap(arg) is not None and unwrap(arg).type in {"identifier", "member_expression"}
                                         for arg in args[1:])
        return "router" if handler or by_reference or server_import[0] else ""

    def chained_route(function, args) -> tuple[str, str, str] | None:
        """(method or ANY, literal path or "", receiver) for `app.route("/x").get(h).post(h)` and
        hapi/Fastify `server.route({ method, path | url, handler })` on a router-named receiver
        or in a file importing a server framework; None for any other call."""
        if function is None or function.type != "member_expression":
            return None
        method = value(function.child_by_field_name("property"))
        target = unwrap(function.child_by_field_name("object"))
        if not server_import:
            server_import.append(bool(_SERVER_IMPORT.search(text)))

        def router_named(receiver) -> bool:
            return receiver is not None and (value(receiver) in _ROUTER_NAMES or server_import[0])

        def literal_path(node) -> str:
            path = url_value(node) if node is not None else None
            return path[0] if path and not path[1] and not path[2] else ""

        if method == "route" and len(args) == 1:
            pairs = object_pairs(args[0])
            if (not pairs or not router_named(target) or pairs.get("method", False) is False
                    or not {"path", "url"} & set(pairs) or not {"handler", "options", "config"} & set(pairs)):
                return None
            verb = literal(pairs["method"]) if pairs["method"] is not None else None
            verb_name = verb[0].upper() if verb and not verb[1] and verb[0].lower() in HTTP_METHODS else "ANY"
            return verb_name, literal_path(pairs.get("path") or pairs.get("url")), value(target)
        if method not in HTTP_METHODS and method != "all":
            return None
        current = target
        while current is not None and current.type == "call_expression":
            callee = current.child_by_field_name("function")
            arguments = current.child_by_field_name("arguments")
            if callee is None or callee.type != "member_expression" or arguments is None:
                return None
            name = value(callee.child_by_field_name("property"))
            receiver = unwrap(callee.child_by_field_name("object"))
            if name == "route":
                if len(arguments.named_children) != 1 or not router_named(receiver):
                    return None
                return "ANY" if method == "all" else method.upper(), literal_path(arguments.named_children[0]), value(receiver)
            if name not in HTTP_METHODS and name != "all":
                return None
            current = receiver
        return None

    schemas: dict[str, str] = {}
    for node, owner in order:
        line = node.start_point.row + 1
        kind = node.type
        if kind == "import_statement":
            _collect_import(facts, node, line, value, literal)
        elif kind == "variable_declarator":
            _declarator_facts(facts, node, line, value, unwrap, literal, object_pairs, declarator_name, schemas, url_value,
                              client_origin)
        elif kind in {"class_declaration", "abstract_class_declaration"}:
            _entity_facts(facts, node, line, value, literal, object_pairs)
        elif kind == "new_expression":
            constructor = node.child_by_field_name("constructor")
            if constructor is not None:
                facts.calls.append((owner, value(constructor), line, "CALLS"))
                arguments = node.child_by_field_name("arguments")
                args = arguments.named_children if arguments is not None else []
                # `new Worker(new URL("./worker.ts", import.meta.url))`: a module the bundler
                # loads by URL (workers, worklets), so it is reachable from this file.
                if value(constructor) == "URL" and len(args) >= 2 and re.sub(r"\s", "", value(args[1])) == "import.meta.url":
                    module = literal(args[0])
                    if (module and not module[1] and module[0].startswith(".")
                            and "." + module[0].rsplit(".", 1)[-1].lower() in EXTENSIONS):
                        facts.imports.append((module[0], "", "*", line))
        elif kind == "decorator" and node.parent is not None and node.parent.type == "class_body" and node.named_children:
            # NestJS `@Get(":id")` on a controller method registers a route under the
            # `@Controller()` prefix and any global prefix; it is not modelled.
            call = node.named_children[0]
            decorator = value(call.child_by_field_name("function") if call.type == "call_expression" else call)
            if decorator in _NEST_ROUTE_DECORATORS and re.search(r"@Controller\b", text):
                facts.route_registrations.append(("ANY" if decorator == "All" else decorator.upper(), "", line,
                                                  "controller", ""))
        elif kind in {"jsx_opening_element", "jsx_self_closing_element"}:
            element = value(node.child_by_field_name("name"))
            # Native links and embedded resources use GET. The graph linker keeps
            # only references to known handlers; page links are not API calls.
            if element in {"a", "img", "iframe"}:
                attribute_name = "href" if element == "a" else "src"
                style = "anchor" if element == "a" else "resource"
                for attribute in node.named_children:
                    if attribute.type != "jsx_attribute" or not attribute.named_children:
                        continue
                    if value(attribute.named_children[0]) != attribute_name or len(attribute.named_children) < 2:
                        continue
                    target = attribute.named_children[1]
                    if target.type == "jsx_expression":
                        target = target.named_children[0] if target.named_children else None
                    url = url_value(target)
                    if url is not None:
                        facts.requests.append(Request(owner, "GET", url[0], url[1], line, "", url[2], style))
            # Lower-case JSX names are host elements (<div>), not components.
            if element[:1].isupper() and re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", element):
                facts.calls.append((owner, element, line, "RENDERS"))
            # `<form action={save}>`, `onClick={remove}`: a function handed over by reference
            # runs when the element fires it. The scanner links it only through an import.
            for attribute in node.named_children:
                if attribute.type == "jsx_attribute" and len(attribute.named_children) == 2 \
                        and attribute.named_children[1].type == "jsx_expression" \
                        and len(attribute.named_children[1].named_children) == 1 \
                        and attribute.named_children[1].named_children[0].type in {"identifier", "member_expression"}:
                    passed = value(attribute.named_children[1].named_children[0])
                    if re.fullmatch(r"[\w$]+(?:\.[\w$]+)*", passed):
                        facts.calls.append((owner, passed, line, "PASSES"))
        if kind != "call_expression":
            continue
        function = node.child_by_field_name("function")
        # `api?.get("/x")` calls the same member as `api.get("/x")`.
        called = value(function).replace("?.", ".")
        arguments = node.child_by_field_name("arguments")
        args = arguments.named_children if arguments is not None else []
        # Tagged templates are call_expression nodes with template_string arguments.
        if arguments is not None and arguments.type == "template_string":
            sql = template(arguments, placeholder=lambda index: f"${index + 1}")
            if sql and SQL_KEYWORDS.search(sql[0]):
                # postgres.js/Prisma bind substitutions as parameters; they are not SQL text.
                facts.queries.append((owner, sql[0], line, False, sql[1] or sql[2]))
            continue
        facts.calls.append((owner, called, line, "CALLS"))
        if called in {"require", "import"} and args:
            module = literal(args[0])
            if module and not module[1]:
                parent = node.parent
                pattern = parent.child_by_field_name("name") if parent is not None and parent.type == "variable_declarator" else None
                named = _required_names(pattern, value) if called == "require" and pattern is not None else []
                # `const { load, save: store } = require("./api")` takes named exports.
                facts.imports.extend((module[0], local, imported, line) for local, imported in named)
                if not named:
                    facts.imports.append((module[0], declarator_name(parent) if pattern is not None else "", "*", line))
            continue
        if called in CLIENT_FACTORIES:
            holder = node.parent
            while holder is not None and holder.type in _WRAPPERS:
                holder = holder.parent
            # `export default axios.create(...)` / `module.exports = axios.create(...)`: the
            # module's default export is the client (a declarator is handled in _declarator_facts).
            if holder is not None and ((holder.type == "export_statement" and any(c.type == "default" for c in holder.children))
                                       or (holder.type == "assignment_expression"
                                           and value(holder.child_by_field_name("left")) == "module.exports")):
                facts.clients["default"] = _factory_base(called, [unwrap(arg) for arg in args], object_pairs, url_value,
                                                         client_origin)
        _request_facts(facts, node, owner, called, args, line, url_value, options_method, object_pairs, literal,
                       hook_bound, route_shape)
        if (chained := chained_route(function, args)) is not None:
            facts.route_registrations.append((chained[0], chained[1], line, "router", chained[2]))
        parts = called.split(".")
        method = parts[-1]
        if args and method in {"query", "execute", "raw", "unsafe", "$queryRawUnsafe", "$executeRawUnsafe"}:
            first = unwrap(args[0])
            if first is not None and first.type == "object":
                first = (object_pairs(first) or {}).get("text")
            built = sql_text(first) if first is not None else None
            if built and SQL_KEYWORDS.search(built[0]):
                # Spliced values become `$n`, so the tables still parse. What was spliced in,
                # and whether it came from outside the process, is reported separately.
                facts.queries.append((owner, built[0], line, False, bool(built[1])))
                if built[1]:
                    facts.sql_interpolations.append((owner, line, [
                        (" ".join(value(hole).split())[:60], untrusted(hole), quoted) for hole, quoted in built[1]]))
        if method in BUILDER_ARGUMENT_OPS and args and unwrap(args[0]) is not None and unwrap(args[0]).type == "identifier":
            facts.model_refs.append((owner, value(unwrap(args[0])), BUILDER_ARGUMENT_OPS[method], line, method))
        if len(parts) == 2 and (method in MODEL_READS or method in MODEL_WRITES) and re.fullmatch(r"[\w$]+", parts[0]):
            operation = "reads" if method in MODEL_READS and method not in MODEL_WRITES else "writes"
            facts.model_refs.append((owner, parts[0], operation, line, method))
        if (len(parts) >= 3 and function is not None and function.type == "member_expression"
                and re.fullmatch(r"[\w$]+", parts[-2]) and (method in MODEL_READS or method in MODEL_WRITES)):
            facts.member_stores.append((owner, ".".join(parts[:-2]), parts[-2], method, line))
        if method == "collection" and len(parts) >= 2 and args and (name := literal(args[0])) and not name[1]:
            chained = _chained_methods(node)
            operation = next(("reads" if m in MONGO_READS else "writes" for m in chained
                              if m in MONGO_READS or m in MONGO_WRITES), "")
            fact = StoreFact(owner, "mongo_collection", name[0], operation, line, "high",
                             "MongoDB driver collection() literal")
            facts.stores.append(fact)
            holder = node.parent
            if holder is not None and holder.type == "variable_declarator" and declarator_name(holder):
                facts.models[declarator_name(holder)] = fact
        if called == "knex" and args and (name := literal(args[0])) and not name[1]:
            chained = _chained_methods(node)
            writes = {"insert", "update", "del", "delete", "upsert", "truncate", "increment", "decrement"}
            operation = "writes" if writes & set(chained) else "reads"
            facts.stores.append(StoreFact(owner, "postgres_table", name[0], operation, line, "probable",
                                          "Knex table literal; SQL dialect not verified"))
    if facts.route_registrations:
        # A server's own routes have no mount prefix only when this file starts it: a server
        # that is exported or mounted elsewhere may be a sub-app under an unknown prefix.
        listening = {called.rsplit(".", 1)[0] for _owner, called, _line, _kind in facts.calls if called.endswith(".listen")}
        facts.route_registrations = [
            registration for registration in facts.route_registrations
            if not (registration[3] == "server" and registration[4] in listening and _CATCH_ALL.fullmatch(registration[1]))]
        facts.route_registrations = [
            (method, path, line, shape if shape != "server" or (receiver in listening and path.startswith("/")
                                                                 and not re.search(r"[*?()+\[\]\\]", path)) else "router",
             receiver)
            for method, path, line, shape, receiver in facts.route_registrations]
    if any(request.receiver for request in facts.requests):
        bound = set(declarations()) | {symbol.name for symbol in facts.symbols} \
            | {local for _module, local, _exported, _line in facts.imports if local}
        facts.free_receivers = {r.receiver for r in facts.requests
                                if r.receiver and "." not in r.receiver and r.receiver not in bound}
    facts.errors = sorted(set(facts.errors))
    return facts


def _chained_methods(call) -> list[str]:
    """Methods invoked on the result of `call`: `db.collection("x").find().toArray()`."""
    names, current = [], call
    while current.parent is not None and current.parent.type == "member_expression":
        member = current.parent
        obj = member.child_by_field_name("object")
        if obj is None or obj.id != current.id:
            break
        prop = member.child_by_field_name("property")
        if prop is not None:
            names.append(prop.text.decode("utf-8") if prop.text else "")
        if member.parent is None or member.parent.type != "call_expression":
            break
        current = member.parent
    return names


def _collect_import(facts: JSFacts, node, line: int, value, literal) -> None:
    source = node.child_by_field_name("source")
    module = literal(source) if source is not None else None
    require = next((child for child in node.named_children if child.type == "import_require_clause"), None)
    if require is not None:
        # `import fs = require("./attrs")` (TypeScript CommonJS interop)
        target = require.child_by_field_name("source") or next(
            (c for c in require.named_children if c.type == "string"), None)
        module = literal(target)
        binding = next((c for c in require.named_children if c.type == "identifier"), None)
        if module and not module[1]:
            facts.imports.append((module[0], value(binding), "*", line))
        return
    if not module or module[1]:
        return
    found = False
    for child in walk(node):
        if child.type == "import_specifier":
            imported = value(child.child_by_field_name("name"))
            facts.imports.append((module[0], value(child.child_by_field_name("alias")) or imported, imported, line))
            found = True
        elif child.type == "namespace_import":
            facts.imports.append((module[0], value(child.named_children[-1]), "*", line))
            found = True
        elif child.type == "import_clause":
            for binding in child.named_children:
                if binding.type == "identifier":
                    facts.imports.append((module[0], value(binding), "default", line))
                    found = True
    if not found:
        facts.imports.append((module[0], "", "", line))


def _collect_exports(facts: JSFacts, order, value, unwrap, literal) -> None:
    """Public name -> local binding for every export form, and `export ... from` records.

    `export { handler as GET, handler as POST }`, `export default Page`, `export default
    memo(List)` and `export const GET = handler` all export a binding without placing
    its declaration beneath the export statement."""
    for node, _ in order:
        if node.type == "expression_statement" and node.named_children:
            assignment = node.named_children[0]
            if assignment.type == "assignment_expression" and value(assignment.child_by_field_name("left")) == "module.exports":
                right = unwrap(assignment.child_by_field_name("right"))
                if right is not None and right.type == "identifier":
                    facts.exports.setdefault("default", value(right))
                elif right is not None and right.type == "call_expression":
                    facts.exports.setdefault("default", _wrapped_binding(right, value, unwrap) or "default")
                elif right is not None and right.type == "object":
                    for child in right.named_children:
                        member = unwrap(child.child_by_field_name("value")) if child.type == "pair" else None
                        if child.type == "shorthand_property_identifier":
                            facts.exports.setdefault(value(child), value(child))
                        elif member is not None and member.type == "identifier":
                            facts.exports.setdefault(value(child.child_by_field_name("key")), value(member))
                        elif member is not None and member.type in {"arrow_function", "function_expression"}:
                            # `module.exports = { list: () => ... }`: the member symbol is named by its key.
                            key = value(child.child_by_field_name("key")).strip("\"'")
                            facts.exports.setdefault(key, key)
                        elif child.type == "method_definition":
                            key = value(child.child_by_field_name("name")).strip("\"'")
                            facts.exports.setdefault(key, key)
            continue
        if node.type != "export_statement":
            continue
        line = node.start_point.row + 1
        source = node.child_by_field_name("source")
        if source is not None:
            module = literal(source)
            if not module or module[1]:
                continue
            specifiers = [child for child in walk(node) if child.type == "export_specifier"]
            namespace = next((child for child in node.named_children if child.type == "namespace_export"), None)
            for specifier in specifiers:
                name = value(specifier.child_by_field_name("name"))
                facts.reexports.append((module[0], value(specifier.child_by_field_name("alias")) or name, name, line))
            if namespace is not None:
                facts.reexports.append((module[0], value(namespace.named_children[-1]) if namespace.named_children else "*", "*", line))
            elif not specifiers:
                facts.reexports.append((module[0], "*", "*", line))
            facts.imports.append((module[0], "", "", line))
            continue
        is_default = any(child.type == "default" for child in node.children)
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            if declaration.type in {"lexical_declaration", "variable_declaration"}:
                for declarator in declaration.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    pattern = declarator.child_by_field_name("name")
                    if pattern is not None and pattern.type in {"object_pattern", "array_pattern"}:
                        # `export const { GET, POST } = handlers` exports each bound name.
                        for bound in _bound_names(pattern, value):
                            facts.exports[bound] = bound
                        continue
                    name = value(pattern)
                    target = unwrap(declarator.child_by_field_name("value"))
                    local = name
                    if target is not None and target.type == "identifier":
                        local = value(target)
                    elif target is not None and target.type == "call_expression":
                        # `export const List = memo(ItemList)` runs ItemList; `createSelector(a, b)` does not.
                        local = _wrapped_binding(target, value, unwrap) or name
                    facts.exports[name] = local
            else:
                name = value(declaration.child_by_field_name("name"))
                if name:
                    facts.exports["default" if is_default else name] = name
            continue
        exported = unwrap(node.child_by_field_name("value"))
        if is_default and exported is not None:
            if exported.type == "identifier":
                facts.exports["default"] = value(exported)
            elif exported.type == "call_expression":
                facts.exports["default"] = _wrapped_binding(exported, value, unwrap) or "default"
            else:
                facts.exports["default"] = "default"
            continue
        for specifier in walk(node):
            if specifier.type == "export_specifier":
                local = value(specifier.child_by_field_name("name"))
                facts.exports[value(specifier.child_by_field_name("alias")) or local] = local


def _request_facts(facts: JSFacts, node, owner: str, called: str, args, line: int,
                   url_value, options_method, object_pairs, literal, hook_bound, route_shape) -> None:
    parts = called.split(".")
    function = node.child_by_field_name("function")
    receiver = function.child_by_field_name("object") if function is not None and function.type == "member_expression" else None
    if called in FETCH_CALLS:
        url = url_value(args[0]) if args else None
        if url is None:
            facts.uncertain_requests.append(line)
            return
        method = options_method(args[1]) if len(args) > 1 else "GET"
        if method == "UNKNOWN":
            facts.uncertain_requests.append(line)
        facts.requests.append(Request(owner, method, url[0], url[1], line, "", url[2], "fetch"))
    elif called in SWR_CALLS:
        url = url_value(args[0]) if args else None
        if url is not None and (url[0].startswith("/") or url[2]):
            facts.requests.append(Request(owner, "GET", url[0], url[1], line, "", url[2], "swr"))
    elif called in {"axios", "axios.request"} or (len(parts) == 2 and parts[1] == "request"):
        pairs = object_pairs(args[0]) if args else None
        if pairs is None or pairs.get("url") is None:
            facts.uncertain_requests.append(line) if called.startswith("axios") else None
            return
        url = url_value(pairs["url"])
        method = literal(pairs["method"]) if pairs.get("method") is not None else ("GET", False)
        if url is None or method is None or method[1] or "..." in pairs:
            facts.uncertain_requests.append(line)
            return
        facts.requests.append(Request(owner, method[0].upper(), url[0], url[1], line,
                                      parts[0] if len(parts) == 2 else "axios", url[2], "client",
                                      len(parts) == 2 and hook_bound(receiver)))
    elif len(parts) == 2 and (parts[1] in HTTP_METHODS or parts[1] == "all") and re.fullmatch(r"[\w$]+", parts[0]):
        # Emitted for any `<binding>.get("/...")`; the scanner keeps it only when the
        # receiver is a known HTTP client, so Express `router.get("/x", h)` is not a request.
        if shape := route_shape(receiver, args):
            # `app.get("/x", (req, res) => ...)` registers a route; `$.get(url, callback)` is a request.
            path = url_value(args[0])
            literal_path = path[0] if path and not path[1] and not path[2] else ""
            facts.route_registrations.append(("ANY" if parts[1] == "all" else parts[1].upper(), literal_path, line,
                                              shape, parts[0]))
            return
        if parts[1] == "all":
            return
        bound = hook_bound(receiver)
        url = url_value(args[0]) if args else None
        if url is None:
            if parts[0] in {"axios", "ky"} or parts[0] in facts.clients or bound:
                facts.uncertain_requests.append(line)
            return
        facts.requests.append(Request(owner, parts[1].upper(), url[0], url[1], line, parts[0], url[2], "client", bound))
    elif (len(parts) == 3 and parts[0] == "this" and parts[2] in HTTP_METHODS and re.fullmatch(r"[\w$]+", parts[1])
          and hook_bound(receiver)):
        # Angular `this.http.get(url)` on an injected HttpClient.
        url = url_value(args[0]) if args else None
        if url is None:
            facts.uncertain_requests.append(line)
            return
        facts.requests.append(Request(owner, parts[2].upper(), url[0], url[1], line, f"this.{parts[1]}", url[2], "client", True))


#: React hooks (`useApi`, `useContext(ApiContext)`) and Vue `const api = inject("api")`: calls
#: that hand over a value created elsewhere, which may be a configured HTTP client. Most hooks
#: return something else, so a hook result only counts as a client where it is called with an
#: HTTP verb and a URL starting with "/" or a configured origin (the scanner checks the URL),
#: and never for the hooks below. Angular's class-field `inject(HttpClient)` is handled apart,
#: by `this.<field>.<verb>(url)` receivers.
_HOOK_CALL = re.compile(r"use[A-Z0-9]\w*|inject")
#: Framework hooks whose result is never an HTTP client: routing, URL state, forms, query
#: caches, stores, i18n and browser storage (`useSearchParams().get("/x")` is not a request).
_NOT_CLIENT_HOOKS = frozenset({
    "useRouter", "useSearchParams", "useParams", "usePathname", "useSelectedLayoutSegment", "useNavigate",
    "useNavigation", "useLocation", "useMatch", "useMatches", "useRoute", "useLoaderData", "useActionData",
    "useFetcher", "useForm", "useFormContext", "useFieldArray", "useController", "useWatch", "useFormState",
    "useQueryClient", "useSWRConfig", "useMutation", "useQuery", "useSelector", "useDispatch", "useStore",
    "useTranslation", "useTranslations", "useI18n", "useIntl", "useLocale", "useTheme", "useColorScheme",
    "useMap", "useSet", "useList", "useLocalStorage", "useSessionStorage", "useStorage", "useCookies",
    "useHeaders", "useMediaQuery", "useWindowSize", "useTransition", "useDeferredValue", "useId",
    "useHead", "useRuntimeConfig", "useAppConfig", "useState", "useReducer", "useRef", "useMemo", "useCallback",
})
#: React state hooks count only when their argument creates a client (`useMemo(() => axios.create(...))`).
_STATE_HOOKS = frozenset({"useState", "useRef", "useMemo", "useCallback"})
#: Receivers whose methods take a success callback: `$.get("/api/items", function (data) {...})`.
_CALLBACK_CLIENTS = frozenset({"$", "jQuery"})
#: Factories whose result registers routes with `.get(path, handler)`.
_ROUTER_FACTORIES = frozenset({"express", "Router", "Hono", "fastify", "Fastify", "polka", "Elysia", "createRouter",
                               "createApp", "createServer", "OpenAPIHono", "Koa", "basePath"})
#: Of those, the ones that create a whole server (not a router mounted under an unknown prefix).
_SERVER_FACTORIES = frozenset({"express", "fastify", "Fastify", "polka", "Hono", "Elysia", "OpenAPIHono"})
#: `require("express")()`: the package call is the factory.
_REQUIRED_SERVER = re.compile(r"""require\(\s*['"](express|fastify|polka)['"]\s*\)""")
#: A file importing one of these registers server routes with `<router>.get(path, handler)`.
_SERVER_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*)['"](?:express|koa|@koa/router|koa-router|hono|fastify|polka|restify|"""
    r"""@hapi/hapi|elysia|itty-router|@tinyhttp/app|hyper-express|ultimate-express|find-my-way)(?:/[^'"]*)?['"]""")
#: `app.all("*", handle)`: a catch-all that hands every request to something else.
_CATCH_ALL = re.compile(r"/?(?:\*|\(\.\*\)|\*\w*|:\w+\(\.\*\)|\{\*\w*\})")
_NEST_ROUTE_DECORATORS = frozenset({"Get", "Post", "Put", "Patch", "Delete", "All", "Head", "Options"})
#: Unbound or parameter receivers that, by convention, register routes when given a handler.
_ROUTER_NAMES = frozenset({"app", "router", "server", "fastify", "hono", "srv", "application", "routes"})
#: Single-argument wrappers whose result runs the wrapped binding: `export default memo(List)`.
_BINDING_WRAPPERS = frozenset({"memo", "forwardRef", "observer"})


def _wrapped_binding(call, value, unwrap) -> str | None:
    """`ItemList` for `memo(ItemList)`, `React.forwardRef(Input)` or `withAuth(handler)`; None
    for any other call, so `createSelector(selectState, fn)` is not an alias of `selectState`."""
    callee = value(call.child_by_field_name("function")).rsplit(".", 1)[-1]
    arguments = call.child_by_field_name("arguments")
    args = arguments.named_children if arguments is not None else []
    if len(args) != 1 or not (callee in _BINDING_WRAPPERS or re.fullmatch(r"with[A-Z]\w*", callee)):
        return None
    first = unwrap(args[0])
    return value(first) if first is not None and first.type == "identifier" else None


def _required_names(pattern, value) -> list[tuple[str, str]]:
    """(local, imported) for `const { load, save: store } = require(...)`; [] for other patterns."""
    if pattern.type != "object_pattern":
        return []
    names = []
    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            names.append((value(child), value(child)))
        elif child.type == "object_assignment_pattern" and (left := child.child_by_field_name("left")) is not None:
            names.append((value(left), value(left)))
        elif child.type == "pair_pattern" and (bound := child.child_by_field_name("value")) is not None \
                and bound.type == "identifier":
            names.append((value(bound), value(child.child_by_field_name("key")).strip("\"'")))
        else:
            return []  # a rest element or nested pattern: the whole module is taken
    return names


def _factory_base(called: str, args, object_pairs, url_value, client_origin) -> str:
    """The base URL a client factory call configures: the literal path, `configured_base(name,
    path)` behind a configured origin, or "" when it has none or it is not literal."""
    pairs = object_pairs(args[0]) if args else None
    option = pairs.get(CLIENT_FACTORIES[called]) if pairs else None
    base = url_value(option) if option is not None else None
    if base is None and option is not None and (name := client_origin(option)):
        # `baseURL: process.env.API_URL`: an origin with no literal path, like the template form.
        return configured_base(name, "")
    # A base behind a configured origin (`${process.env.API}/api`) keeps its path and the
    # origin's name, which the scanner reads to decide whether it is this repository's API.
    return "" if base is None or base[1] else configured_base(base[2], base[0]) if base[2] else base[0]


def _bound_names(pattern, value) -> list[str]:
    """Local names a declarator binds: `x`, or each `a`, `b: c` in `{ a, b: c }`."""
    if pattern.type == "identifier":
        return [value(pattern)]
    names = []
    if pattern.type == "array_pattern":
        return [value(child) for child in pattern.named_children if child.type == "identifier"]
    for child in pattern.named_children if pattern.type == "object_pattern" else ():
        if child.type == "shorthand_property_identifier_pattern":
            names.append(value(child))
        elif child.type == "pair_pattern" and (bound := child.child_by_field_name("value")) is not None \
                and bound.type == "identifier":
            names.append(value(bound))
        elif child.type == "object_assignment_pattern" and (left := child.child_by_field_name("left")) is not None:
            names.append(value(left))
    return names


def _declarator_facts(facts: JSFacts, node, line: int, value, unwrap, literal, object_pairs,
                      declarator_name, schemas: dict[str, str], url_value, client_origin) -> None:
    name = declarator_name(node)
    target = unwrap(node.child_by_field_name("value"))
    if not name or target is None:
        return
    if target.type == "binary_expression":
        # `models.Item || model("Item", schema)` is the Next.js hot-reload idiom.
        right = unwrap(target.child_by_field_name("right"))
        target = right if right is not None and right.type == "call_expression" else target
    if target.type == "await_expression" and target.named_children:
        target = unwrap(target.named_children[0])
    if target is None or target.type != "call_expression":
        return
    called = value(target.child_by_field_name("function"))
    arguments = target.child_by_field_name("arguments")
    args = [unwrap(arg) for arg in arguments.named_children] if arguments is not None else []
    first = literal(args[0]) if args else None
    first = first[0] if first and not first[1] else None
    last = called.rsplit(".", 1)[-1]
    if called in CLIENT_FACTORIES:
        facts.clients[name] = _factory_base(called, args, object_pairs, url_value, client_origin)
    elif last == "pgSchema" and first:
        schemas[name] = first
    elif first and (last in {"pgTable", "pgView", "pgMaterializedView"}
                    or (last in {"table", "view"} and called.split(".")[0] in schemas)):
        schema = schemas.get(called.split(".")[0], "") if last in {"table", "view"} else ""
        table = f"{schema}.{first}" if schema else first
        fact = StoreFact("", "postgres_table", table, "declares", line, "exact", f"Drizzle {last}() declaration")
        facts.models[name] = fact
        facts.stores.append(fact)
    elif last == "model" and first and (called in {"model", "mongoose.model"} or called.endswith(".model")):
        explicit = literal(args[2]) if len(args) > 2 else None
        collection = explicit[0] if explicit and not explicit[1] else pluralize(first)
        resolution = "exact" if explicit and not explicit[1] else "probable"
        facts.models[name] = StoreFact("", "mongo_collection", collection, "declares", line, resolution,
                                       f"Mongoose model {first}" + ("" if resolution == "exact" else "; collection name inferred by pluralization"))
    elif last == "define" and first and len(args) >= 1:
        options = object_pairs(args[2]) if len(args) > 2 else None
        table = literal(options.get("tableName")) if options and options.get("tableName") is not None else None
        explicit = table[0] if table and not table[1] else ""
        facts.models[name] = StoreFact("", "postgres_table", explicit or pluralize(first), "declares", line,
                                       "exact" if explicit else "probable",
                                       f"Sequelize model {first}" + ("" if explicit else "; table name inferred by pluralization"))


def _entity_facts(facts: JSFacts, node, line: int, value, literal, object_pairs) -> None:
    """TypeORM `@Entity("name")` / `@Entity({ name })` classes are table declarations."""
    name = value(node.child_by_field_name("name"))
    decorators = [child for child in node.children if child.type == "decorator"]
    holder = node.parent
    if holder is not None and holder.type == "export_statement":
        decorators += [child for child in holder.children if child.type == "decorator"]
    for decorator in decorators:
        call = decorator.named_children[0] if decorator.named_children else None
        if call is None:
            continue
        callee = value(call.child_by_field_name("function")) if call.type == "call_expression" else value(call)
        if callee != "Entity":
            continue
        args = call.child_by_field_name("arguments") if call.type == "call_expression" else None
        first = args.named_children[0] if args is not None and args.named_children else None
        table = literal(first) if first is not None else None
        if table is None and first is not None:
            pairs = object_pairs(first) or {}
            table = literal(pairs.get("name")) if pairs.get("name") is not None else None
            schema = literal(pairs.get("schema")) if pairs.get("schema") is not None else None
            if table and schema and not schema[1]:
                table = (f"{schema[0]}.{table[0]}", table[1])
        explicit = table[0] if table and not table[1] else ""
        fact = StoreFact("", "postgres_table", explicit or _snake(name), "declares", line,
                         "exact" if explicit else "probable",
                         "TypeORM @Entity" + ("" if explicit else "; table name inferred from the class name"))
        facts.models[name] = fact
        facts.stores.append(StoreFact(name, fact.kind, fact.name, "declares", line, fact.resolution, fact.detail))
        return


def walk(node):
    """Yield `node` and all its named descendants, depth-first in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))
