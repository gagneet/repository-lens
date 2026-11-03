"""Python AST extraction: definitions, calls, SQL literals, ORM/ODM models and stores."""
from __future__ import annotations

import warnings
import ast
from collections import defaultdict
import hashlib
from pathlib import Path
import re
import sys

from ..scan.python_ast import bound_names, dotted, keyword_value, last, module_level
from .model import Edge, Issue, Node, stable_id
from .state import (MONGO_NOT_COLLECTIONS, PendingCall, ScanState, _add_store_edge, _file_id, _rel,
                    _symbol_id, mongo_operation)

_SQL_WORD = re.compile(r"\b(?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.I)
_SQL_START = re.compile(r"\s*(?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.I)
_SQL_CALLS = frozenset({"text", "execute", "executemany", "fetch", "fetchrow", "fetchval",
                        "exec_driver_sql", "query"})

#: Decorators that register the function with a router. `@router.get("/items")` is not a
#: call the handler makes: resolving `get` by name linked every handler to `CRUDBase.get`.
ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace",
                              "api_route", "websocket", "route", "websocket_route"})

#: What a call naming a model class does to its table or collection. `select(User)` reads
#: and `insert(Order)` writes; a model named anywhere else says nothing about direction.
MODEL_READS = frozenset({"select", "query", "get", "scalars", "scalar", "exec", "find", "find_one",
                         "find_all", "find_many", "get_or_none", "objects", "count", "get_one",
                         "search"})
MODEL_WRITES = frozenset({"insert", "update", "delete", "add", "add_all", "merge", "bulk_save_objects",
                          "bulk_insert_mappings", "bulk_update_mappings", "save", "insert_many",
                          "delete_all", "replace", "upsert", "create", "modify"})

_MONGO_CLIENTS = frozenset({"MongoClient", "AsyncIOMotorClient", "AsyncMongoClient", "MotorClient"})
#: Aggregation stages that name another collection: (key inside the stage, operation).
_PIPELINE_STAGES = {"$lookup": ("from", "reads"), "$graphLookup": ("from", "reads"),
                    "$unionWith": ("coll", "reads"), "$out": ("coll", "writes"),
                    "$merge": ("into", "writes")}

#: Alembic `op.<name>` -> the (position, keyword) of each table argument it takes.
_ALEMBIC_TABLES: dict[str, tuple[tuple[int, str, str], ...]] = {
    "create_table": ((0, "table_name", "schema"),),
    "drop_table": ((0, "table_name", "schema"),),
    "add_column": ((0, "table_name", "schema"),),
    "drop_column": ((0, "table_name", "schema"),),
    "alter_column": ((0, "table_name", "schema"),),
    "create_index": ((1, "table_name", "schema"),),
    "drop_index": ((1, "table_name", "schema"),),
    "rename_table": ((0, "old_table_name", "schema"), (1, "new_table_name", "schema")),
    "create_foreign_key": ((1, "source_table", "source_schema"), (2, "referent_table", "referent_schema")),
    "create_unique_constraint": ((1, "table_name", "schema"),),
    "create_check_constraint": ((1, "table_name", "schema"),),
    "create_primary_key": ((1, "table_name", "schema"),),
    "drop_constraint": ((1, "table_name", "schema"),),
    "create_table_comment": ((0, "table_name", "schema"),),
    "batch_alter_table": ((0, "table_name", "schema"),),
}
_MIGRATION_PATH = re.compile(r"(?:^|/)(?:alembic|migrations)/versions/")
_ODM_BASES = {"beanie": frozenset({"Document"}),
              "mongoengine": frozenset({"Document", "DynamicDocument"}),
              "odmantic": frozenset({"Model"})}
_ENUM_BASES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})


def _text(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


class _Scope:
    """What one module, class or function body binds, for the names read inside it."""
    __slots__ = ("kind", "bound", "imports", "mongo")

    def __init__(self, kind: str, bound: frozenset[str] | set[str] = frozenset()):
        self.kind = kind
        # A function's own names: its `User = 3` is not the mapped class `User`.
        self.bound = bound
        # Function-local `from app.models import User`: not in the module's bindings.
        self.imports: dict[str, tuple[str, str]] = {}
        # Local name -> (client|database|collection, name, traced to a client constructor).
        self.mongo: dict[str, tuple[str, str, bool]] = {}


class PythonVisitor(ast.NodeVisitor):
    """Walks one Python module: adds its symbols and store edges, and records calls and model
    references on `ScanState` for the passes that resolve them after every file is read."""
    def __init__(self, state: ScanState, path: str, file_node: str, text: str):
        self.state = state
        self.path = path
        self.file_node = file_node
        self.text = text
        self.scope: list[str] = []
        self.symbol_stack: list[str] = []
        self.sql_values: list[dict[str, ast.AST]] = [{}]
        self.scopes: list[_Scope] = [_Scope("module")]
        # Callee names of the calls being visited, innermost last.
        self.call_stack: list[str] = []
        # The table whose columns are being declared (class body or Table(...) call).
        self.table_stack: list[str | None] = []
        self.module_assigned: set[str] = set()
        self.migration = bool(_MIGRATION_PATH.search(path))
        # Local classes deriving from an ODM document base -> the ODM.
        self.odm_bases: dict[str, str] = {}

    def _current_source(self) -> str:
        return self.symbol_stack[-1] if self.symbol_stack else self.file_node

    def _evidence(self, node: ast.AST) -> str:
        return f"{self.path}:{getattr(node, 'lineno', '?')}"

    def _add_definition(self, node: ast.AST, name: str, kind: str) -> str:
        qualified = ".".join([*self.scope, name])
        node_id = _symbol_id(self.path, qualified)
        doc = ast.get_docstring(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
        metadata = {"qualified_name": qualified, "symbol_kind": kind}
        if doc:
            metadata["doc"] = doc[:600]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_dump = ast.dump(
                ast.Module(body=node.body, type_ignores=[]),
                annotate_fields=True,
                include_attributes=False,
                # 3.13 stopped printing empty fields by default; keep the 3.11/3.12 dump so the
                # 110-character floor and the fingerprints are the same on every version.
                **({"show_empty": True} if sys.version_info >= (3, 13) else {}),
            )
            if len(body_dump) >= 110:
                metadata["body_fingerprint"] = hashlib.sha256(body_dump.encode("utf-8")).hexdigest()
        self.state.graph.add_node(Node(
            id=node_id,
            kind="symbol",
            label=qualified,
            path=self.path,
            line=getattr(node, "lineno", None),
            language="python",
            metadata=metadata,
        ))
        self.state.graph.add_edge(Edge(
            self.file_node, node_id, "CONTAINS", "exact", "python_ast", origin="ast"
        ))
        self.state.definitions[name].append(node_id)
        return node_id

    # -- module ---------------------------------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:
        """Pre-reads module-level statements (Alembic `op`, assigned names, Mongo handles), then visits."""
        for statement in module_level(node):
            if isinstance(statement, ast.ImportFrom) and statement.module == "alembic" \
                    and any(alias.name == "op" for alias in statement.names):
                self.migration = True
            targets = (statement.targets if isinstance(statement, ast.Assign)
                       else [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign, ast.For))
                       else [])
            for target in targets:
                self.module_assigned.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            # A module-level client or database handle is used by functions defined
            # ABOVE its assignment as often as below it.
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                self._track_mongo(statement.targets[0], statement.value)
        self.generic_visit(node)

    # -- definitions ----------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Adds a class symbol, and a TOUCHES_STORE (declares) edge when it maps a table or collection."""
        # Bases, keywords and decorators run where the class statement runs.
        for child in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(child)
        symbol = self._add_definition(node, node.name, "class")
        qualified = ".".join([*self.scope, node.name])
        model = self._model(node)
        table = None
        if model:
            kind, name, resolution, detail = model
            self.state.py_models[(self.path, qualified)] = model
            _add_store_edge(self.state.graph, symbol, kind, name, resolution, self._evidence(node),
                            detail=detail, origin="python_ast", metadata={"orm_class": node.name})
            table = name if kind == "postgres_table" else None
        self.scope.append(node.name)
        self.symbol_stack.append(symbol)
        self.sql_values.append({})
        self.scopes.append(_Scope("class"))
        self.table_stack.append(table)
        for statement in node.body:
            self.visit(statement)
        self.table_stack.pop()
        self.scopes.pop()
        self.sql_values.pop()
        self.symbol_stack.pop()
        self.scope.pop()

    def _model(self, node: ast.ClassDef) -> tuple[str, str, str, str] | None:
        """(store kind, name, resolution, detail) for a mapped SQL or document class."""
        table = schema = None
        tablename_declared = False
        for statement in node.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if "__tablename__" in names:
                tablename_declared = True
                table = _text(statement.value)
            if "__table__" in names and (declared := self._table_call(statement.value)):
                table, schema = declared
            if "__table_args__" in names:
                for value in ast.walk(statement.value):
                    if isinstance(value, ast.Dict):
                        for key, item in zip(value.keys, value.values, strict=True):
                            if _text(key) == "schema" and _text(item):
                                schema = _text(item)
        if any(isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "__tablename__"
               for s in node.body):
            tablename_declared = True  # a @declared_attr: computed, never guessed
        if table:
            return ("postgres_table", f"{schema}.{table}" if schema else table, "exact",
                    "SQLAlchemy model (declares)")
        sqlmodel_table = any(k.arg == "table" and isinstance(k.value, ast.Constant) and k.value.value is True
                             for k in node.keywords)
        if sqlmodel_table and not tablename_declared:
            # SQLModel's rule: the lower-cased class name unless __tablename__ is set.
            name = node.name.lower()
            return ("postgres_table", f"{schema}.{name}" if schema else name, "high",
                    "SQLModel table model (declares)")
        return self._document(node)

    def _document(self, node: ast.ClassDef) -> tuple[str, str, str, str] | None:
        odm = None
        imports = self.state.imports.get(self.path, {})
        for base in node.bases:
            name = dotted(base)
            if not name:
                continue
            if name in self.odm_bases:
                odm = self.odm_bases[name]
                break
            parts = name.split(".")
            target, exported = imports.get(parts[0], ("", ""))
            if not target.startswith("external:"):
                continue
            exported = exported if len(parts) == 1 else parts[-1]
            package = target.removeprefix("external:").split(".")[0]
            if exported in _ODM_BASES.get(package, ()):
                odm = package
                break
        if odm is None:
            return None
        self.odm_bases[node.name] = odm
        explicit = None
        abstract = False
        for statement in node.body:
            if isinstance(statement, ast.ClassDef) and statement.name in {"Settings", "Collection", "Config"}:
                for inner in statement.body:
                    if isinstance(inner, (ast.Assign, ast.AnnAssign)) and inner.value is not None:
                        targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
                        if any(isinstance(t, ast.Name) and t.id in {"name", "collection"} for t in targets):
                            explicit = _text(inner.value) or explicit
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if not any(isinstance(t, ast.Name) and t.id in {"meta", "model_config"} for t in targets):
                continue
            value = statement.value
            if isinstance(value, ast.Dict):
                for key, item in zip(value.keys, value.values, strict=True):
                    if _text(key) == "collection":
                        explicit = _text(item) or explicit
                    if _text(key) == "abstract" and isinstance(item, ast.Constant) and item.value is True:
                        abstract = True
            elif isinstance(value, ast.Call):
                explicit = _text(keyword_value(value, "collection")) or explicit
        label = {"beanie": "Beanie document", "mongoengine": "MongoEngine document",
                 "odmantic": "ODMantic model"}[odm]
        if explicit:
            return ("mongo_collection", explicit, "exact", f"{label} (declares)")
        if abstract:
            return None
        # The ODM's default name. A base class, a settings subclass or runtime
        # configuration can change it, so it is a candidate only.
        default = node.name if odm == "beanie" else _snake(node.name)
        return ("mongo_collection", default, "probable", f"{label} (declares; default collection name)")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Adds a function symbol and visits its body as that symbol's scope."""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Adds an async function symbol and visits its body as that symbol's scope."""
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators, defaults and annotations are evaluated where the `def` runs, in the
        # ENCLOSING scope. Visited after the push they became calls the body makes.
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in ROUTE_DECORATORS):
                # A route registration, not a call: only its arguments are code.
                for value in [*decorator.args, *(k.value for k in decorator.keywords)]:
                    self.visit(value)
            else:
                self.visit(decorator)
        args = node.args
        for default in [*args.defaults, *args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None and arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        symbol = self._add_definition(node, node.name, "function")
        self.scope.append(node.name)
        self.symbol_stack.append(symbol)
        self.sql_values.append({})
        self.scopes.append(_Scope("function", bound_names(node)))
        saved_calls, self.call_stack = self.call_stack, []
        for statement in node.body:
            self.visit(statement)
        self.call_stack = saved_calls
        self.scopes.pop()
        self.sql_values.pop()
        self.symbol_stack.pop()
        self.scope.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Visits a lambda as part of the enclosing symbol; it gets no symbol of its own."""
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Records a function-local import binding; module-level imports are bound before the visit."""
        scope = self.scopes[-1]
        index = self.state.import_index
        if scope.kind != "function" or index is None:
            return
        for alias in node.names:
            module = node.module or ""
            submodule = index.python(self.path, ".".join(filter(None, (module, alias.name))), node.level)
            target = submodule or index.python(self.path, module, node.level)
            if target:
                scope.imports[alias.asname or alias.name] = (target, "*" if submodule else alias.name)

    # -- calls ----------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        """Records a pending call and extracts SQL, Mongo, Alembic, foreign-key and `Table(...)` facts."""
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = ast.unparse(node.func)
        if name:
            self.state.pending_calls.append(PendingCall(
                self._current_source(), name, self._evidence(node), "python",
            ))
        callee = last(node.func)
        if callee in _SQL_CALLS:
            self._sql(node)
        if isinstance(node.func, ast.Attribute):
            if operation := mongo_operation(node.func.attr):
                self._mongo_call(node, operation)
            if self.migration and dotted(node.func.value) == "op" and node.func.attr in _ALEMBIC_TABLES:
                self._alembic(node)
        self._foreign_keys(node, callee)
        declared = self._table_call(node)
        if declared:
            self.table_stack.append(f"{declared[1]}.{declared[0]}" if declared[1] else declared[0])
        self.call_stack.append(callee)
        self.generic_visit(node)
        self.call_stack.pop()
        if declared:
            self.table_stack.pop()

    def _fold(self, node: ast.AST) -> str | None:
        """The text of a string built only from literals: `"SELECT * " + "FROM t"`,
        an f-string without substitutions, or a name bound to one. Iterative, so a
        generated 3,000-term concatenation cannot exhaust the recursion limit here."""
        parts: list[str] = []
        stack = [node]
        names = 0
        while stack:
            current = stack.pop()
            if isinstance(current, ast.Constant) and isinstance(current.value, str):
                parts.append(current.value)
            elif isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add):
                stack.extend((current.right, current.left))
            elif isinstance(current, ast.JoinedStr) and all(isinstance(v, ast.Constant) for v in current.values):
                parts.append("".join(str(v.value) for v in current.values))
            elif isinstance(current, ast.Name) and names < 20:
                names += 1
                value = next((scope[current.id] for scope in reversed(self.sql_values) if current.id in scope), None)
                if value is None:
                    return None
                stack.append(value)
            else:
                return None
        return "".join(parts)

    def _sql(self, node: ast.Call) -> None:
        value = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in {"query", "sql", "statement"}), None)
        if value is None:
            return
        from .postgres import add_sql
        text = self._fold(value)
        if isinstance(value, ast.Name):
            value = next((scope[value.id] for scope in reversed(self.sql_values) if value.id in scope), None)
        if text is not None:
            if _SQL_WORD.search(text):
                add_sql(self.state.graph, self._current_source(), text, self._evidence(node), gated=True)
        elif isinstance(value, (ast.JoinedStr, ast.BinOp)):
            add_sql(self.state.graph, self._current_source(), "", self._evidence(node), dynamic=True)

    def _alembic(self, node: ast.Call) -> None:
        func = node.func
        assert isinstance(func, ast.Attribute)
        for position, keyword, schema_keyword in _ALEMBIC_TABLES[func.attr]:
            value = node.args[position] if len(node.args) > position else keyword_value(node, keyword)
            table = self._fold(value) if value is not None else None
            if not table:
                continue
            schema = keyword_value(node, schema_keyword)
            schema_text = self._fold(schema) if schema is not None else None
            _add_store_edge(self.state.graph, self._current_source(), "postgres_table",
                            f"{schema_text}.{table}" if schema_text else table, "exact", self._evidence(node),
                            detail="Alembic migration (declares)", origin="python_ast")

    def _table_call(self, node: ast.AST | None) -> tuple[str, str | None] | None:
        """(table, schema) for SQLAlchemy `Table("name", metadata, ..., schema="s")`. The
        metadata argument is required: `rich.table.Table("Title")` is not a table."""
        if not (isinstance(node, ast.Call) and last(node.func) == "Table" and len(node.args) >= 2):
            return None
        table = _text(node.args[0])
        return (table, _text(keyword_value(node, "schema"))) if table else None

    def _foreign_keys(self, node: ast.Call, callee: str) -> None:
        if not self.table_stack or not self.table_stack[-1]:
            return
        references: list[str] = []
        if callee == "ForeignKey" and node.args and _text(node.args[0]):
            references.append(_text(node.args[0]))
        elif callee in {"Field", "mapped_column", "Column"} and _text(keyword_value(node, "foreign_key")):
            references.append(_text(keyword_value(node, "foreign_key")))
        elif callee == "ForeignKeyConstraint" and len(node.args) > 1 and isinstance(node.args[1], (ast.List, ast.Tuple)):
            references.extend(t for e in node.args[1].elts if (t := _text(e)))
        declaring = stable_id("postgres_table", self.table_stack[-1])
        if declaring not in self.state.graph.nodes:
            return
        for reference in references:
            if "." not in reference:
                continue
            table = reference.rsplit(".", 1)[0]
            target = stable_id("postgres_table", table)
            self.state.graph.add_node(Node(target, "postgres_table", table, metadata={"store": table}))
            self.state.graph.add_edge(Edge(declaring, target, "REFERENCES", "exact", self._evidence(node),
                                           origin="python_ast", detail="foreign key"))

    # -- MongoDB --------------------------------------------------------------------

    def _mongo_lookup(self, key: str) -> tuple[str, str, bool] | None:
        for scope in reversed(self.scopes):
            if key in scope.mongo:
                return scope.mongo[key]
        return None

    def _mongo_value(self, expr: ast.AST, depth: int = 0) -> tuple[str, str, bool] | None:
        """(client|database|collection, name, traced) for an expression, or None.

        A database is the configured receiver name (untraced) or a value derived from a
        MongoClient/Motor construction (traced). A collection is only ever reached from
        a database: `session.query` has no database under it."""
        if depth > 8:
            return None
        key = dotted(expr)
        if key:
            found = self._mongo_lookup(key)
            if found:
                return found
            if key == self.state.config.mongo_receiver:
                return ("database", key, False)
        if isinstance(expr, ast.Call):
            callee = last(expr.func)
            if callee in _MONGO_CLIENTS:
                return ("client", "", True)
            name = _text(expr.args[0]) if expr.args else _text(keyword_value(expr, "name"))
            if isinstance(expr.func, ast.Attribute):
                owner = self._mongo_value(expr.func.value, depth + 1)
                if owner and owner[0] == "client" and callee in {"get_database", "get_default_database"}:
                    return ("database", name or "", owner[2])
                if owner and owner[0] == "database" and callee in {"get_collection", "collection"} and name:
                    return ("collection", name, owner[2])
            elif callee == "get_database":
                return ("database", name or "", False)
            return None
        if isinstance(expr, ast.Attribute):
            name = expr.attr
        elif isinstance(expr, ast.Subscript):
            name = _text(expr.slice)
        else:
            return None
        if not name:
            return None
        owner = self._mongo_value(expr.value, depth + 1)
        if owner is None:
            return None
        if owner[0] == "client":
            return ("database", name, owner[2])
        if owner[0] == "database" and name not in MONGO_NOT_COLLECTIONS and not name.startswith("_"):
            return ("collection", name, owner[2])
        return None

    def _track_mongo(self, target: ast.AST, value: ast.AST | None) -> None:
        key = dotted(target)
        if not key or value is None:
            return
        scope = self.scopes[-1]
        if key.startswith(("self.", "cls.")):
            # `self.db = client.appdb` in __init__ is read by every other method.
            scope = next((s for s in reversed(self.scopes) if s.kind == "class"), scope)
        found = self._mongo_value(value)
        if found:
            scope.mongo[key] = found
        else:
            scope.mongo.pop(key, None)

    def _mongo_call(self, node: ast.Call, operation: str) -> None:
        assert isinstance(node.func, ast.Attribute)
        collection = self._mongo_value(node.func.value)
        if not collection or collection[0] != "collection":
            return
        resolution = "high" if collection[2] else "probable"
        evidence = self._evidence(node)
        _add_store_edge(self.state.graph, self._current_source(), "mongo_collection", collection[1],
                        resolution, evidence, detail=f"MongoDB driver call ({operation})", origin="python_ast")
        if node.func.attr != "aggregate":
            return
        for stage in (n for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Dict)):
            for key, value in zip(stage.keys, stage.values, strict=True):
                spec = _PIPELINE_STAGES.get(_text(key) or "")
                if spec is None:
                    continue
                field, stage_operation = spec
                name = _text(value)
                if isinstance(value, ast.Dict):
                    name = next((_text(v) for k, v in zip(value.keys, value.values, strict=True) if _text(k) == field), None)
                if name:
                    _add_store_edge(self.state.graph, self._current_source(), "mongo_collection", name,
                                    resolution, evidence, detail=f"MongoDB aggregation stage {_text(key)} ({stage_operation})",
                                    origin="python_ast")

    # -- names and assignments ------------------------------------------------------

    def _model_reference(self, node: ast.AST, name: str) -> None:
        """A name read inside a call may be a mapped class or Table; resolved later."""
        if not self.call_stack:
            return
        head = name.split(".")[0]
        binding = None
        for scope in reversed(self.scopes):
            if scope.kind != "function":
                continue
            if head in scope.imports:
                binding = scope.imports[head]
                break
            if head in scope.bound:
                return  # a local variable or parameter of that name
        operation = ""
        for callee in reversed(self.call_stack):
            operation = mongo_operation(callee) or ("reads" if callee in MODEL_READS else
                                                    "writes" if callee in MODEL_WRITES else "")
            if operation:
                break
        self.state.py_model_refs.append((
            self._current_source(), self.path, name, self._evidence(node), operation, binding,
            head in self.module_assigned,
        ))

    def visit_Name(self, node: ast.Name) -> None:
        """Records a loaded name as a possible model reference, resolved after all modules are visited."""
        if isinstance(node.ctx, ast.Load):
            self._model_reference(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Records a loaded `name.attr` as a possible model reference (`models.User`)."""
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            self._model_reference(node, f"{node.value.id}.{node.attr}")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Tracks values for SQL folding and Mongo handles, `Table(...)` bindings and SQL string literals."""
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.sql_values[-1][target.id] = node.value
            self._track_mongo(target, node.value)
        self._declare_table(node.targets, node.value, node)
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if _SQL_START.match(node.value.value):
                from .postgres import add_sql
                add_sql(self.state.graph, self._current_source(), node.value.value, self._evidence(node), gated=True)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Same as `visit_Assign`, for an annotated assignment."""
        if isinstance(node.target, ast.Name):
            self.sql_values[-1][node.target.id] = node.value
        self._track_mongo(node.target, node.value)
        self._declare_table([node.target], node.value, node)
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if _SQL_START.match(node.value.value):
                from .postgres import add_sql
                add_sql(self.state.graph, self._current_source(), node.value.value, self._evidence(node), gated=True)
        self.generic_visit(node)

    def _declare_table(self, targets: list[ast.expr], value: ast.AST | None, node: ast.AST) -> None:
        """`tags = Table("tags", metadata, ...)` at module level binds `tags` to the table:
        `tags.select()` and `select(tags)` elsewhere in the file read it."""
        declared = self._table_call(value)
        if not declared or len(self.scopes) != 1:
            return
        table, schema = declared
        name = f"{schema}.{table}" if schema else table
        model = ("postgres_table", name, "exact", "SQLAlchemy Table (declares)")
        for target in targets:
            if isinstance(target, ast.Name):
                self.state.py_models[(self.path, target.id)] = model
        _add_store_edge(self.state.graph, self._current_source(), "postgres_table", name, "exact",
                        self._evidence(node), detail=model[3], origin="python_ast")


def _local_python_names(state: ScanState) -> frozenset[str]:
    if state.python_local_names is None:
        names: set[str] = set()
        for path in state.admitted_paths:
            if path.endswith(".py"):
                names.update(path[:-3].split("/"))
        state.python_local_names = frozenset(names)
    return state.python_local_names


def _bind_external_imports(state: ScanState, rel: str, tree: ast.Module, bound: set[str]) -> None:
    """Record module imports that leave the repository as `external:<module>` bindings.

    Without one, call resolution name-matched `json.loads()` to any local `loads`. A
    top-level name that is also a local directory or module is left unbound instead:
    that import is a local resolution gap, and name matching stays its fallback."""
    local = _local_python_names(state)
    imports = state.imports.setdefault(rel, {})

    def external(module: str) -> bool:
        top = module.split(".")[0]
        return bool(top) and (top in sys.stdlib_module_names or top not in local)

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.level or not external(node.module or ""):
                continue
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name != "*" and name not in bound:
                    imports[name] = (f"external:{node.module}", alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not external(alias.name):
                    continue
                # `import x.y as z` binds z to x.y; `import os.path` binds `os` (and the
                # resolver also looks up the dotted `os.path` prefix of a call).
                pairs = ([(alias.asname, alias.name)] if alias.asname
                         else [(alias.name, alias.name), (alias.name.split(".")[0], alias.name.split(".")[0])])
                for name, module in dict.fromkeys(pairs):
                    if name not in bound:
                        imports.setdefault(name, (f"external:{module}", "*"))


def _scan_python(state: ScanState, path: Path, text: str, file_node: str) -> None:
    rel = _rel(state.root, path)
    try:
        with warnings.catch_warnings():  # target code's own SyntaxWarnings are not ours to print
            warnings.simplefilter("ignore")
            tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        state.graph.issues.append(Issue(
            "PYTHON_PARSE_ERROR", "warning", f"Could not parse {rel}: {exc.msg}",
            [file_node], f"{rel}:{exc.lineno or 1}", "Fix syntax or exclude generated/vendor code.",
        ))
        return
    bound: set[str] = set()
    for target, local, symbol, line in state.import_index.python_bindings(rel, tree):
        state.imports.setdefault(rel, {})[local] = (target, symbol)
        bound.add(local)
        target_id = _file_id(target)
        state.graph.add_node(Node(target_id, "file", target, path=target))
        state.graph.add_edge(Edge(file_node, target_id, "IMPORTS", "exact", f"{rel}:{line}", origin="python_ast"))
    _bind_external_imports(state, rel, tree, bound)
    PythonVisitor(state, rel, file_node, text).visit(tree)
    # Stored only once the walk succeeded: a file that failed (e.g. too deeply nested)
    # must not feed its tree to the cross-file passes that run after every file.
    state.python_trees[rel] = tree


def _follow_model(state: ScanState, key: tuple[str, str]) -> tuple[str, str]:
    """Follow re-exports (`models/__init__.py: from .user import User`) to the class."""
    for _ in range(8):
        if key in state.py_models:
            return key
        binding = state.imports.get(key[0], {}).get(key[1])
        if not binding or binding[0].startswith("external:") or binding[1] == "*":
            return key
        key = binding
    return key


def _model_targets(state: ScanState, path: str, name: str, binding: tuple[str, str] | None,
                   module_assigned: bool, by_class: dict[str, list[tuple[str, str]]]) -> list[tuple[tuple[str, str], str]]:
    parts = name.split(".")
    binding = binding or state.imports.get(path, {}).get(parts[0])
    if binding:
        # The file says which class the name is. A pydantic `schemas.User` imported
        # here is not the mapped `models.User`, however the names collide.
        target, exported = binding
        if target.startswith("external:"):
            return []
        if len(parts) == 1 and exported != "*":
            key = _follow_model(state, (target, exported))
        elif len(parts) == 2 and exported == "*":
            key = _follow_model(state, (target, parts[1]))
        else:
            return []
        return [(key, "high")] if key in state.py_models else []
    if len(parts) != 1:
        return []
    if (path, name) in state.py_models:
        return [((path, name), "high")]
    if module_assigned or _symbol_id(path, name) in state.graph.nodes:
        return []  # `User = 3`, or a local class/function that is not a model
    candidates = by_class.get(name, [])
    if len(candidates) == 1:
        return [(candidates[0], "probable")]
    if len(candidates) > state.config.max_ambiguous_targets:
        return []
    return [(candidate, "ambiguous") for candidate in candidates]


def _resolve_orm_references(state: ScanState) -> None:
    """Link model class and Table references after every admitted module has been visited."""
    by_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, name in sorted(state.py_models):
        by_class[name.rsplit(".", 1)[-1]].append((path, name))
    merged: dict[tuple[str, tuple[str, str], str], tuple[str, set[str]]] = {}
    for source, path, name, evidence, operation, binding, module_assigned in state.py_model_refs:
        if source not in state.graph.nodes:
            continue
        for key, resolution in _model_targets(state, path, name, binding, module_assigned, by_class):
            _, operations = merged.setdefault((source, key, evidence), (resolution, set()))
            if operation:
                operations.add(operation)
    for (source, key, evidence), (resolution, operations) in merged.items():
        kind, store, _, _ = state.py_models[key]
        operation = "writes" if "writes" in operations else "reads" if operations else ""
        family = "ORM" if kind == "postgres_table" else "ODM"
        detail = f"{family} class reference {key[1]}" + (f" ({operation})" if operation else "") \
            + "; runtime query shape unverified"
        metadata = {"orm_class": key[1], **({"dialect": "postgres"} if kind == "postgres_table" else {})}
        _add_store_edge(state.graph, source, kind, store, resolution, evidence, detail=detail,
                        origin="python_ast", metadata=metadata)
