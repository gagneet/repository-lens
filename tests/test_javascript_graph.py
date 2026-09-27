"""JavaScript/TypeScript, Next.js and JS data-store linkage in the impact graph.

Each test builds the smallest repository that shows one relationship a real Next.js or
Node codebase depends on, and asserts the edge (or the absence of a false one)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import time
import unittest

from repolens.core import javascript
from repolens.impact.scanner import scan_repository

HAS_STACK = javascript.available() and importlib.util.find_spec("sqlglot") is not None


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def scan(self):
        self.graph = scan_repository(self.root)
        return self.graph

    def node(self, label, kind=None):
        found = [n for n in self.graph.nodes.values() if n.label == label and (kind is None or n.kind == kind)]
        self.assertTrue(found, f"no node {label!r}; have {sorted(n.label for n in self.graph.nodes.values())}")
        return found[0]

    def labels(self, kind):
        return {n.label for n in self.graph.nodes.values() if n.kind == kind}

    def edges(self, kind):
        return {(self.graph.nodes[e.source].label, self.graph.nodes[e.target].label)
                for e in self.graph.edges if e.kind == kind}

    def codes(self):
        return {i.code for i in self.graph.issues}


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class AnchorNavigationTests(Repo):
    def test_native_links_to_get_handlers_are_callers_but_page_links_are_not_api_gaps(self):
        self.write("app/reports/[id]/pdf/route.ts", "export async function GET() { return Response.json({}); }")
        self.write("app/assets/[id]/route.ts", "export async function GET() { return Response.json({}); }")
        self.write("app/preview/[id]/route.ts", "export async function GET() { return Response.json({}); }")
        self.write("app/page.tsx", '''export default function Page({id}: {id: string}) {
          return <><a href={`/reports/${id}/pdf`} download>PDF</a>
            <img src={`/assets/${id}`} alt="cover" />
            <iframe src={`/preview/${id}`} title="preview" />
            <img src="/static.png" alt="static" />
            <a href="/plain-page">Page</a><a href="https://example.org/report">External</a></>;
        }''')
        self.scan()
        self.assertIn(("Page", "GET /reports/{dynamic}/pdf"), self.edges("CALLS_API"))
        self.assertIn(("Page", "GET /assets/{dynamic}"), self.edges("CALLS_API"))
        self.assertIn(("Page", "GET /preview/{dynamic}"), self.edges("CALLS_API"))
        self.assertNotIn("API_HANDLER_WITHOUT_STATIC_CALLER", self.codes())
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())
        self.assertNotIn("EXTERNAL_API_REFERENCE", self.codes())

    def test_unlinked_handler_remains_diagnostic(self):
        self.write("app/reports/route.ts", "export async function GET() { return Response.json({}); }")
        self.write("app/page.tsx", 'export default function Page(){ return <a href="/plain-page">Page</a>; }')
        self.scan()
        self.assertIn("API_HANDLER_WITHOUT_STATIC_CALLER", self.codes())
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class ImportResolutionTests(Repo):
    def test_stylesheets_images_and_declaration_files_are_not_missing_modules(self):
        self.write("types/env.d.ts", "declare const X: string;")
        self.write("app/globals.css", "body{}")
        self.write("app/layout.tsx", 'import "./globals.css";\nimport styles from "./page.module.css";\n'
                                     'import logo from "../public/logo.svg";\nimport "../types/env";\n'
                                     "export default function Layout(){ return null; }")
        from repolens.analysis import analyze
        result = analyze(self.root)
        self.assertNotIn("UNRESOLVED_LOCAL_IMPORT", {i.code for i in result.graph.issues})
        self.assertTrue(result.complete)

    def test_a_package_extends_keeps_the_local_aliases_and_is_not_fatal(self):
        self.write("tsconfig.json", '{"extends":"@repo/typescript-config/nextjs.json",'
                                    '"compilerOptions":{"baseUrl":".","paths":{"@/*":["./src/*"]}}}')
        self.write("src/lib/x.ts", "export function helper(){ return 1; }")
        self.write("src/app/page.tsx", 'import { helper } from "@/lib/x";\nexport default function Page(){ return helper(); }')
        self.scan()
        self.assertNotIn("IMPORT_CONFIG_ERROR", self.codes())
        self.assertIn("IMPORT_CONFIG_PARTIAL", self.codes())
        edge = next(e for e in self.graph.edges if e.kind == "CALLS" and self.graph.nodes[e.target].label == "helper")
        self.assertEqual(edge.origin, "import_binding")

    def test_child_paths_without_base_url_resolve_from_the_declaring_config(self):
        self.write("tsconfig.base.json", '{"compilerOptions":{"strict":true}}')
        self.write("apps/web/tsconfig.json", '{"extends":"../../tsconfig.base.json","compilerOptions":{"paths":{"@/*":["./src/*"]}}}')
        self.write("apps/web/src/lib/x.ts", "export const x = () => 1;")
        self.write("apps/web/src/app/page.tsx", 'import { x } from "@/lib/x";\nexport default function Page(){ return x(); }')
        self.scan()
        self.assertNotIn("UNRESOLVED_LOCAL_IMPORT", self.codes())
        self.assertIn(("apps/web/src/app/page.tsx", "apps/web/src/lib/x.ts"), self.edges("IMPORTS"))

    def test_package_bindings_and_platform_globals_are_never_name_matched(self):
        self.write("utils/parse.ts", "export function parse(){}\nexport function format(){}")
        self.write("use.tsx", 'import { format } from "date-fns";\nexport function main(){ JSON.parse("{}"); format("x"); }')
        self.scan()
        self.assertNotIn(("main", "parse"), self.edges("CALLS"))
        self.assertNotIn(("main", "format"), self.edges("CALLS"))

    def test_a_barrel_export_star_and_a_renamed_reexport_resolve(self):
        self.write("lib/a.ts", "export const arrow = () => 1;\nexport function inner(){}")
        self.write("lib/index.ts", 'export * from "./a";\nexport { inner as renamed } from "./a";')
        self.write("use.tsx", 'import { arrow, renamed } from "./lib";\nexport function main(){ arrow(); renamed(); }')
        self.scan()
        calls = [e for e in self.graph.edges if e.kind == "CALLS"]
        targets = {(self.graph.nodes[e.target].label, e.resolution) for e in calls}
        self.assertIn(("arrow", "high"), targets)
        self.assertIn(("inner", "high"), targets)

    def test_export_forms_that_do_not_wrap_the_declaration(self):
        self.write("make.ts", "const make = () => 5;\nexport default make;")
        self.write("inner.ts", "function inner(){}\nexport { inner as renamed };")
        self.write("list.tsx", "function ItemList(){ return null; }\nexport default memo(ItemList);")
        self.write("use.tsx", 'import make from "./make";\nimport { renamed } from "./inner";\nimport List from "./list";\n'
                              "export function main(){ make(); renamed(); return <List/>; }")
        self.scan()
        self.assertTrue({("main", "make"), ("main", "inner")} <= self.edges("CALLS"))
        self.assertIn(("main", "ItemList"), self.edges("RENDERS"))

    def test_typescript_import_require_is_an_import(self):
        self.write("attrs.ts", "export const a = 1;")
        self.write("main.ts", 'import attrs = require("./attrs");\nexport const b = attrs.a;')
        self.scan()
        self.assertIn(("main.ts", "attrs.ts"), self.edges("IMPORTS"))


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class SymbolAndCallTests(Repo):
    def test_members_of_an_exported_object_api_are_import_bound(self):
        self.write("lib/objapi.ts", "export const api = { getItems: async () => fetch('/api/items'), save() { return 1; } };")
        self.write("use.tsx", 'import { api } from "./lib/objapi";\nexport function main(){ api.getItems(); api.save(); }')
        self.scan()
        edges = {(self.graph.nodes[e.source].label, self.graph.nodes[e.target].label, e.resolution)
                 for e in self.graph.edges if e.kind == "CALLS"}
        self.assertIn(("main", "api.getItems", "high"), edges)
        self.assertIn(("main", "api.save", "high"), edges)

    def test_same_named_handlers_in_two_objects_stay_two_symbols(self):
        self.write("h.ts", "export const a = { onClick: () => one() };\nexport const b = { onClick: () => two() };\n"
                           "function one(){}\nfunction two(){}")
        self.scan()
        self.assertTrue({"a.onClick", "b.onClick"} <= self.labels("symbol"))
        self.assertIn(("a.onClick", "one"), self.edges("CALLS"))
        self.assertNotIn(("a.onClick", "two"), self.edges("CALLS"))

    def test_class_field_arrows_are_named_and_this_calls_bind_to_the_class(self):
        self.write("store.ts", "export class Store {\n  load = async () => this.parse();\n  parse() { return 1; }\n}\n"
                               "export class Other { parse() { return 2; } }")
        self.scan()
        self.assertIn("Store.load", self.labels("symbol"))
        self.assertIn(("Store.load", "Store.parse"), self.edges("CALLS"))
        self.assertNotIn(("Store.load", "Other.parse"), self.edges("CALLS"))

    def test_new_expressions_and_jsx_components_are_dependencies(self):
        self.write("svc.ts", "export class Svc {}")
        self.write("list.tsx", "export function ItemList(){ return <div/>; }")
        self.write("page.tsx", 'import { Svc } from "./svc";\nimport { ItemList } from "./list";\n'
                               "export function View(){ new Svc(); return <section><ItemList /></section>; }")
        self.scan()
        self.assertIn(("View", "Svc"), self.edges("CALLS"))
        self.assertIn(("View", "ItemList"), self.edges("RENDERS"))
        self.assertNotIn("section", {target for _, target in self.edges("RENDERS")})

    def test_a_minified_bundle_parses_in_linear_time(self):
        body = "".join(f"function f{i}(){{return f{max(i - 1, 0)}()}}" for i in range(6000))
        self.write("bundle.js", body)
        started = time.perf_counter()
        facts = javascript.parse_source(body, ".js")
        self.assertEqual(len(facts.symbols), 6000)
        self.assertLess(time.perf_counter() - started, 3.0)


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class NextRoutingTests(Repo):
    def endpoint_handlers(self):
        return {self.graph.nodes[e.source].label for e in self.graph.edges if e.kind == "HANDLES_API"}

    def test_route_handlers_exported_as_aliases_wrappers_and_clauses(self):
        self.write("src/app/api/const/route.ts", "async function handler(){ return new Response(); }\nexport const GET = handler;")
        self.write("src/app/api/wrapped/route.ts", "export const GET = withAuth(async (req) => new Response());")
        self.write("src/app/api/multi/route.ts", "async function handler(){ return new Response(); }\nexport { handler as GET, handler as POST };")
        self.scan()
        self.assertTrue({"GET /api/const", "GET /api/wrapped", "GET /api/multi", "POST /api/multi"} <= self.endpoint_handlers())

    def test_an_endpoint_is_located_at_its_handler_not_its_first_caller(self):
        self.write("aaa/client.ts", "export const load = () => fetch('/api/items');")
        self.write("backend/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/api/items")\ndef items(): return []\n')
        self.scan()
        endpoint = self.node("GET /api/items", "endpoint")
        self.assertEqual((endpoint.path, endpoint.language), ("backend/main.py", "python"))

    def test_a_route_segment_named_app_is_part_of_the_url(self):
        self.write("package.json", "{}")
        self.write("src/app/(dash)/app/settings/route.ts", "export async function GET(){ return new Response(); }")
        self.write("src/app/(dash)/app/settings/page.tsx", "export default function Page(){ return null; }")
        self.scan()
        self.assertIn("GET /app/settings", self.labels("endpoint"))
        self.assertIn("/app/settings", self.labels("page"))

    def test_pages_router_api_routes_and_app_router_page_conventions(self):
        self.write("package.json", "{}")
        self.write("pages/api/legacy.ts", "export default function handler(req, res){ res.json({}); }")
        self.write("pages/blog/[slug].tsx", "export default function Post(){ return null; }")
        self.write("pages/_app.tsx", "export default function App(){ return null; }")
        self.write("src/components/pages/card.tsx", "export default function Card(){ return null; }")
        self.write("src/app/js/page.js", "export default function Page(){ return null; }")
        self.write("src/app/@modal/login/page.tsx", "export default function Login(){ return null; }")
        self.write("client.ts", "export const legacy = () => fetch('/api/legacy', { method: 'POST' });")
        self.scan()
        self.assertIn("ANY /api/legacy", self.labels("endpoint"))
        self.assertTrue({"/blog/{dynamic}", "/js", "/login"} <= self.labels("page"))
        self.assertFalse({"/_app", "/card"} & self.labels("page"))
        self.assertIn(("legacy", "ANY /api/legacy"), self.edges("CALLS_API"))
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())

    def test_optional_catch_all_serves_its_parent_and_nested_paths(self):
        self.write("app/docs/[[...slug]]/route.ts", "export async function GET(){ return new Response(); }")
        self.write("client.ts", "export const a = () => fetch('/docs');\nexport const b = () => fetch('/docs/intro/setup');")
        self.scan()
        self.assertTrue({("a", "GET /docs"), ("b", "GET /docs/{dynamic}")} <= self.edges("CALLS_API"))
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())

    def test_a_literal_client_path_matches_a_dynamic_handler(self):
        self.write("backend/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/api/items/cart/{cid}")\ndef cart(cid): return []\n')
        self.write("web/cart.ts", "export const load = () => fetch('/api/items/cart/1');")
        self.scan()
        edge = next(e for e in self.graph.edges if e.kind == "CALLS_API"
                    and self.graph.nodes[e.target].label == "GET /api/items/cart/{dynamic}")
        self.assertEqual(edge.resolution, "probable")
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class HttpClientTests(Repo):
    def test_method_hidden_in_options_is_unknown_and_matches_same_route_handlers(self):
        self.write("app/api/items/route.ts", "export async function GET(){}\nexport async function POST(){}")
        self.write("a.tsx", "export function one(method: string){ return fetch('/api/items', { method }); }\n"
                            "export function two(o: RequestInit){ return fetch('/api/items', o); }")
        self.scan()
        self.assertTrue({("one", "GET /api/items"), ("one", "POST /api/items"), ("two", "GET /api/items")}
                        <= self.edges("CALLS_API"))
        self.assertNotIn(("one", "GET /api/items", "exact"),
                         {(self.graph.nodes[e.source].label, self.graph.nodes[e.target].label, e.resolution)
                          for e in self.graph.edges if e.kind == "CALLS_API"})
        self.assertNotIn("API_CALL_WITHOUT_HANDLER", self.codes())

    def test_axios_instance_base_url_is_applied_across_modules(self):
        self.write("lib/api.ts", "export const api = axios.create({ baseURL: '/api/v1' });\nexport default api;")
        self.write("feature.ts", 'import client from "./lib/api";\nexport const load = () => client.get("/items");')
        self.write("backend/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/api/v1/items")\ndef items(): return []\n')
        self.scan()
        self.assertIn(("load", "GET /api/v1/items"), self.edges("CALLS_API"))
        self.assertNotIn("GET /items", self.labels("endpoint"))

    def test_express_style_receivers_are_not_http_requests(self):
        self.write("server.ts", "const router = express.Router();\nrouter.get('/health', (req, res) => res.send('ok'));")
        self.scan()
        self.assertFalse(self.labels("endpoint"))

    def test_swr_keys_and_configured_origins_are_probable_requests(self):
        self.write("items.tsx", "export function Items(){ useSWR('/api/swr', fetcher);\n"
                                "  return fetch(`${process.env.NEXT_PUBLIC_API}/api/items`); }")
        self.scan()
        requests = {(self.graph.nodes[e.target].label, e.resolution) for e in self.graph.edges if e.kind == "CALLS_API"}
        self.assertIn(("GET /api/swr", "exact"), requests)
        self.assertIn(("GET /api/items", "probable"), requests)
        self.assertNotIn("EXTERNAL_API_REFERENCE", self.codes())


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class JavaScriptStoreTests(Repo):
    def store_edges(self):
        return {(self.graph.nodes[e.source].label, self.graph.nodes[e.target].kind, self.graph.nodes[e.target].label)
                for e in self.graph.edges if e.kind == "TOUCHES_STORE"}

    def test_drizzle_table_bindings_resolve_through_imports(self):
        self.write("db/schema.ts", "export const widgets = pgTable('widgets', { id: serial('id') });")
        self.write("svc.ts", 'import { widgets } from "./db/schema";\n'
                             "export async function list(){ return db.select().from(widgets); }\n"
                             "export async function add(){ return db.insert(widgets).values({}); }")
        self.scan()
        self.assertTrue({("list", "postgres_table", "widgets"), ("add", "postgres_table", "widgets")} <= self.store_edges())
        add = next(e for e in self.graph.edges if e.kind == "TOUCHES_STORE" and self.graph.nodes[e.source].label == "add")
        self.assertIn("writes", add.detail)

    def test_mongoose_models_and_the_node_driver(self):
        self.write("models/item.ts", "const Item = models.Item || model('Item', schema);\nexport default Item;\n"
                                     "export const Cat = mongoose.model('Cat', s, 'felines');")
        self.write("svc.ts", 'import Item, { Cat } from "./models/item";\nimport type { Db } from "mongodb";\n'
                             "export async function load(){ await Item.find({}); return Cat.findOne({}); }\n"
                             "export async function save(db: Db){ return db.collection('orders').insertOne({}); }")
        self.scan()
        edges = self.store_edges()
        self.assertTrue({("load", "mongo_collection", "items"), ("load", "mongo_collection", "felines"),
                         ("save", "mongo_collection", "orders")} <= edges)

    def test_prisma_accessors_map_to_schema_tables(self):
        self.write("prisma/schema.prisma", 'datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n'
                                           'model User {\n  id Int @id\n  @@map("users")\n}\nmodel Post {\n  id Int @id\n}\n')
        self.write("svc.ts", "export async function list(){ await prisma.user.findMany(); return prisma.post.create({ data: {} }); }")
        self.scan()
        self.assertTrue({("list", "postgres_table", "users"), ("list", "postgres_table", "Post")} <= self.store_edges())

    def test_knex_typeorm_and_parameterized_postgres_js(self):
        self.write("entity.ts", "@Entity('employees')\nexport class Employee {}")
        self.write("svc.ts", 'import { Employee } from "./entity";\n'
                             "export const repo = () => dataSource.getRepository(Employee);\n"
                             "export const gadgets = () => knex('gadgets').insert({});\n"
                             "export const account = (id: string) => sql`SELECT * FROM accounts WHERE id = ${id}`;\n"
                             "export const unsafe = (t: string) => sql`SELECT * FROM ${t}`;")
        self.scan()
        edges = self.store_edges()
        self.assertTrue({("repo", "postgres_table", "employees"), ("gadgets", "postgres_table", "gadgets"),
                         ("account", "postgres_table", "accounts")} <= edges)
        dynamic = [i for i in self.graph.issues if i.code == "DYNAMIC_SQL"]
        self.assertEqual([self.graph.nodes[i.node_ids[0]].label for i in dynamic], ["unsafe"])

    def test_database_handles_hostnames_and_prose_are_not_collections(self):
        self.write("README.md", "Call db.connect() first.")
        self.write("config.json", '{"host": "db.internal", "fallback": "db.example.com"}')
        self.write("drizzle.ts", "export async function q(){ await db.select().from(t); await db.transaction(async () => {}); }")
        self.write("ui.ts", 'export function confirm(db){ return db.query("Delete this item?"); }')
        self.scan()
        self.assertFalse(self.labels("mongo_collection"))
        self.assertNotIn("SQL_PARSE_ERROR", self.codes())

    def test_configured_schema_names_count_only_inside_sql_text(self):
        self.write(".impact-tracer.json", '{"pg_schemas": ["app"]}')
        self.write("web/x.ts", "export const r = app.router;\nexport const q = () => pool.query('SELECT id FROM app.users');")
        self.write("docs/guide.md", "Set app.config before starting.")
        self.scan()
        self.assertEqual(self.labels("postgres_table"), {"app.users"})



@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class StackChainTests(Repo):
    def test_a_table_query_reaches_the_page_that_renders_it_at_the_default_depth(self):
        self.write("web/package.json", "{}")
        self.write("web/tsconfig.json", '{"compilerOptions":{"paths":{"@/*":["./src/*"]}}}')
        self.write("web/src/lib/api.ts", 'export const api = axios.create({ baseURL: "/api/v1" });\n'
                                         'export const orders = { list: () => api.get("/orders") };')
        self.write("web/src/app/orders/page.tsx", 'import { orders } from "@/lib/api";\n'
                                                  "export default function OrdersPage(){ orders.list(); return null; }")
        self.write("backend/app/__init__.py", "")
        self.write("backend/app/main.py", "from fastapi import FastAPI\nfrom app.routers import orders\n"
                                          'app = FastAPI()\napp.include_router(orders.router, prefix="/api/v1")\n')
        self.write("backend/app/routers/__init__.py", "")
        self.write("backend/app/routers/orders.py", "from fastapi import APIRouter\nfrom app.services.orders import list_orders\n"
                                                    'router = APIRouter(prefix="/orders")\n@router.get("")\n'
                                                    "async def get_orders():\n    return await list_orders()\n")
        self.write("backend/app/services/__init__.py", "")
        self.write("backend/app/services/orders.py", "async def list_orders():\n"
                                                     '    return await conn.fetch("SELECT id FROM sales.orders LIMIT 50")\n')
        from repolens.analysis import analyze
        result = analyze(self.root)
        self.assertTrue(result.complete)
        reached = {(n.kind, n.label) for n in result.view("sales.orders", max_nodes=40).nodes.values()}
        self.assertTrue({("endpoint", "GET /api/v1/orders"), ("symbol", "OrdersPage"), ("page", "/orders")} <= reached)

@unittest.skipUnless(HAS_STACK, "requires repolens[stack]")
class DebuggingSignalTests(Repo):
    """Shapes that each reproduce one reviewed false warning or missed fact in a Next.js + pg + Prisma layout."""

    def issues(self, code):
        return [issue for issue in self.graph.issues if issue.code == code]

    def test_a_bare_ampersand_in_jsx_text_is_not_a_parse_error(self):
        self.write("app/catalog/page.tsx", "export default function Catalog({ x }) {\n"
                                           "  return <div><h2>Tools & Parts</h2><p>A&B &amp; {x}<b>Q&A</b></p></div>;\n}\n")
        self.write("app/broken.tsx", "export function Broken() { return <p>a & b</p>; }\nconst x = ;\n")
        self.scan()
        self.assertEqual([issue.evidence for issue in self.issues("JAVASCRIPT_PARSE_ERROR")], ["app/broken.tsx:2"])
        self.node("Catalog", "symbol")

    def test_a_module_constant_base_path_prefixes_fetch_urls(self):
        self.write("package.json", "{}")
        self.write("src/services/api.ts", "const API_URL = '/api';\n"
                   "export async function upload(form) {\n"
                   "  return fetch(`${API_URL}/invoices/upload`, { method: 'POST', body: form, ...headers() });\n}\n"
                   "export const summary = () => fetch(API_URL + '/invoices/summary');\n")
        self.write("src/app/api/invoices/upload/route.ts", "export async function POST(request) { return Response.json({}); }\n")
        self.write("src/app/api/invoices/summary/route.ts", "export async function GET() { return Response.json([]); }\n")
        self.scan()
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])
        self.assertIn(("upload", "POST /api/invoices/upload"), self.edges("CALLS_API"))
        self.assertIn(("summary", "GET /api/invoices/summary"), self.edges("CALLS_API"))

    def test_imports_into_excluded_build_output_are_not_unresolved(self):
        self.write("next-env.d.ts", '/// <reference types="next" />\nimport "./.next/types/routes.d.ts";\nimport "./missing";\n')
        self.scan()
        self.assertEqual([issue.message for issue in self.issues("UNRESOLVED_LOCAL_IMPORT")],
                         ["Local import could not be resolved: ./missing"])

    def test_a_cli_argument_spliced_into_sql_through_variables_is_a_security_finding(self):
        self.write("scripts/process.js", "const { Pool } = require('pg');\n"
                   "const args = process.argv.slice(2);\n"
                   "const CUSTOMER_ID = args.find(arg => arg.startsWith('--customer='))?.split('=')[1];\n"
                   "const LIMIT = parseInt(args[1]);\n"
                   "async function main(client) {\n"
                   "  const customerFilter = CUSTOMER_ID ? `AND \"customerId\" = '${CUSTOMER_ID}'` : '';\n"
                   "  let sql = `SELECT po.id FROM pending_orders po WHERE 1=1 ${customerFilter}`;\n"
                   "  if (LIMIT) sql += ` LIMIT ${LIMIT}`;\n"
                   "  await client.query(sql);\n"
                   "  await client.query(`SELECT id FROM products WHERE category = '${CATEGORY}'`);\n}\n")
        from repolens.analysis import analyze
        result = analyze(self.root)
        self.graph = result.graph
        self.assertIn(("main", "pending_orders"), self.edges("TOUCHES_STORE"))
        [risk] = self.issues("SQL_INJECTION_RISK")
        self.assertEqual(risk.evidence, "scripts/process.js:9")
        self.assertIn("`CUSTOMER_ID`, which comes from a command-line argument", risk.message)
        self.assertNotIn("LIMIT", risk.message)
        self.assertIn("`CATEGORY` inside a quoted literal", " ".join(i.message for i in self.issues("DYNAMIC_SQL")))
        [finding] = [f for run in result.runs for f in run.findings if f.rule == "security/sql-string-interpolation"]
        self.assertEqual((finding.file, finding.line, finding.severity), ("scripts/process.js", 9, "medium"))

    def test_request_input_spliced_into_sql_is_high_severity(self):
        self.write("app/api/items/[id]/route.ts",
                   "export async function GET(request, { params }) {\n"
                   "  const { searchParams } = new URL(request.url);\n"
                   "  const n = Number(searchParams.get('n'));\n"
                   "  await pool.query(`SELECT * FROM items LIMIT ${n}`);\n"
                   "  return pool.query(`SELECT * FROM items WHERE owner = '${params.id}' ORDER BY ${searchParams.get('sort')}`);\n}\n")
        from repolens.analysis import analyze
        result = analyze(self.root)
        self.graph = result.graph
        [risk] = self.issues("SQL_INJECTION_RISK")
        self.assertEqual(risk.evidence, "app/api/items/[id]/route.ts:5")
        self.assertEqual([f.severity for run in result.runs for f in run.findings
                          if f.rule == "security/sql-string-interpolation"], ["high"])

    def test_a_call_without_handler_names_served_methods_and_dead_callers(self):
        self.write("package.json", "{}")
        self.write("src/app/api/orders/[id]/route.ts", "export async function GET() { return Response.json({}); }\n")
        self.write("src/components/OrderEditor.tsx", "export function OrderEditor({ id }) {\n"
                   "  const save = () => fetch(`/api/orders/${id}`, { method: 'PATCH' });\n  return <button onClick={save} />;\n}\n")
        self.write("src/components/OrderArchive.tsx", "export function OrderArchive({ id }) {\n"
                   "  const go = () => fetch(`/api/orders/${id}/archive`, { method: 'POST' });\n  return <button onClick={go} />;\n}\n")
        self.write("src/app/orders/page.tsx", 'import { OrderArchive } from "../../components/OrderArchive";\n'
                   "export default function Page() { return <OrderArchive id={1} />; }\n")
        self.scan()
        [patch] = self.issues("API_METHOD_MISMATCH")
        self.assertTrue(patch.message.startswith("PATCH /api/orders/{dynamic} has no handler"))
        self.assertIn("handled for GET, so the call gets 405", patch.message)
        self.assertEqual(patch.severity, "info")
        self.assertEqual(patch.subject, "src/components/OrderEditor.tsx:2")
        self.assertIn("src/components/OrderEditor.tsx is not imported from any page", patch.message)
        issues = {issue.message.split(" was found for ")[1].split(".")[0]: issue for issue in self.issues("API_CALL_WITHOUT_HANDLER")}
        self.assertEqual(issues["POST /api/orders/{dynamic}/archive"].severity, "warning")

    def test_an_exported_request_function_no_live_module_imports_is_dead(self):
        self.write("package.json", "{}")
        self.write("src/services/api.ts", "export const used = () => fetch('/api/missing-a', { method: 'POST' });\n"
                   "export const unused = () => fetch('/api/missing-b');\n"
                   "const api = { used, unused };\nexport default api;\n")
        self.write("src/services/all.ts", "import * as everything from './api';\nexport const call = (p) => fetch(`/api/missing-c/${p}`);\n")
        self.write("src/components/Old.tsx", "import api from '../services/api';\nexport const Old = () => { api.unused(); return null; };\n")
        self.write("src/app/page.tsx", "import { used } from '../services/api';\nexport default function Home() { used(); return null; }\n")
        self.scan()
        issues = {issue.message.split(" was found for ")[1].split(".")[0]: issue for issue in self.issues("API_CALL_WITHOUT_HANDLER")}
        self.assertEqual(issues["POST /api/missing-a"].severity, "warning")
        self.assertEqual(issues["GET /api/missing-b"].severity, "info")
        self.assertIn("`unused` in src/services/api.ts is exported but never imported or called", issues["GET /api/missing-b"].message)
        # all.ts is imported by nothing live, so its call is dead too; the namespace import does not revive it.
        self.assertIn("src/services/all.ts is not imported", issues["GET /api/missing-c/{dynamic}"].message)


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class UntracedClientTests(Repo):
    """A client created in a provider and handed to components by a hook: the React
    context pattern, where the call site cannot see the client's declaration."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def provider(self):
        self.write("package.json", "{}")
        self.write("frontend/src/index.jsx", 'import App from "./App";\n')
        self.write("frontend/src/App.jsx", 'import Board from "./pages/Board";\nexport default function App() { return <Board />; }\n')
        self.write("frontend/src/contexts/Session.jsx",
                   "import axios from 'axios';\n"
                   "const API_URL = `${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000'}/api`;\n"
                   "export function SessionProvider({ children }) {\n"
                   "  const client = useMemo(() => {\n"
                   "    const instance = axios.create({ baseURL: API_URL, timeout: 30000 });\n"
                   "    return instance;\n  }, []);\n"
                   "  return children;\n}\n")
        self.write("backend/routers/orders.py",
                   "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/orders')\n"
                   "@router.get('')\ndef list_orders(status: str = ''): return []\n"
                   "@router.get('/{order_id}')\ndef get_order(order_id: int): return {}\n"
                   "@router.delete('/{order_id}')\ndef delete_order(order_id: int): return {}\n")

    def test_hook_bound_client_calls_resolve_and_a_wrong_method_is_a_live_mismatch(self):
        self.provider()
        self.write("frontend/src/pages/Board.jsx",
                   "export default function Board({ id, query }) {\n"
                   "  const { user, client } = useSession();\n"
                   "  const { searchParams } = useSearch();\n"
                   "  searchParams.get('status');\n"
                   "  client.get(`/orders${query}`);\n"
                   "  client.patch(`/orders/${id}`, { status: 'done' });\n"
                   "  return null;\n}\n")
        from repolens.analysis import analyze
        result = analyze(self.root)
        self.graph = result.graph
        self.assertIn(("Board", "GET /api/orders"), self.edges("CALLS_API"))
        [mismatch] = self.issues("API_METHOD_MISMATCH")
        self.assertEqual((mismatch.severity, mismatch.subject), ("warning", "frontend/src/pages/Board.jsx:6"))
        self.assertIn("handled for DELETE, GET", mismatch.message)
        self.assertEqual([i.message for i in self.issues("API_CALL_WITHOUT_HANDLER")], [])
        self.assertNotIn("EXTERNAL_API_REFERENCE", self.codes())
        [finding] = [f for run in result.runs for f in run.findings if f.rule == "stack/api-method-mismatch"]
        self.assertEqual((finding.file, finding.line, finding.category), ("frontend/src/pages/Board.jsx", 6, "correctness"))
        detail = next(e.detail for e in self.graph.edges if e.kind == "CALLS_API" and e.evidence.endswith(":5"))
        self.assertIn("client `client` was not traced", detail)
        self.assertIn("matched by route prefix", detail)

    def test_configured_base_and_receivers_cover_what_a_hook_does_not(self):
        self.provider()
        self.write("frontend/src/other.js", "import axios from 'axios';\nexport const admin = axios.create({ baseURL: '/admin' });\n")
        self.write("frontend/src/pages/Board.jsx",
                   "export default function Board({ http, id }) {\n  http.delete(`/orders/${id}`);\n  return null;\n}\n")
        self.scan()
        self.assertEqual(self.edges("CALLS_API"), set())  # `http` is a parameter: not known to be a client
        self.write("repolens.toml", '[impact]\nclient_receivers = ["http"]\nclient_api_base = "/api"\n')
        from repolens.impact.config import Config
        self.graph = scan_repository(self.root, Config.load(self.root))
        self.assertIn(("Board", "DELETE /api/orders/{dynamic}"), self.edges("CALLS_API"))
        self.assertEqual(self.issues("API_METHOD_MISMATCH"), [])

    def test_express_style_router_members_are_still_not_requests(self):
        self.write("server/routes.js", "const router = express.Router();\nrouter.get('/orders', listOrders);\n"
                                       "app.post('/orders', function (req, res) { res.json({}); });\n"
                                       "server.delete('/orders/:id', async (req, res) => res.json({}));\n")
        self.scan()
        self.assertEqual(self.edges("CALLS_API"), set())

    def test_a_receiver_the_file_never_binds_is_a_probable_client(self):
        self.write("src/lib/pay.js", "export const pay = (id) => api.post(`/api/invoices/${id}/pay`, { id });\n")
        self.scan()
        [edge] = [e for e in self.graph.edges if e.kind == "CALLS_API"]
        self.assertEqual((self.graph.nodes[edge.target].label, edge.resolution), ("POST /api/invoices/{dynamic}/pay", "probable"))
        self.assertIn("client `api` was not traced", edge.detail)


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class RouteShapeTests(Repo):
    """Calls whose URL the source spells only in part still reach the handlers that serve them."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def test_open_ended_urls_and_runtime_segments_match_declared_handlers(self):
        self.write("backend/routers/items.py",
                   "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/items')\n"
                   "@router.get('')\ndef list_items(): return []\n"
                   "@router.get('/{item_id}/export.{fmt}')\ndef export_item(item_id: int, fmt: str): return {}\n"
                   "@router.post('/{item_id}/approve')\ndef approve(item_id: int): return {}\n"
                   "@router.post('/{item_id}/reject')\ndef reject(item_id: int): return {}\n"
                   "@router.get('/v2/accounts')\ndef accounts(): return []\n")
        self.write("frontend/src/index.jsx",
                   "export default function App({ id, fmt, action, query, path }) {\n"
                   "  fetch(`/api/items${query}`);\n"
                   "  fetch(`/api/items/${id}/export.${fmt}`);\n"
                   "  fetch(`/api/items/${id}/${action}`, { method: 'POST' });\n"
                   "  fetch(`/api/items/v2${path}`);\n"
                   "  fetch(`/api/missing${query}`);\n"
                   "  return null;\n}\n")
        self.scan()
        calls = self.edges("CALLS_API")
        for handler in ("GET /api/items", "GET /api/items/{dynamic}/export.{dynamic}", "POST /api/items/{dynamic}/approve",
                        "POST /api/items/{dynamic}/reject", "GET /api/items/v2/accounts"):
            self.assertIn(("App", handler), calls)
        self.assertNotIn(("App", "POST /api/items/{dynamic}/approve"),
                         {(s, t) for s, t in calls if t.startswith("GET")})
        self.assertEqual([i.message for i in self.issues("API_CALL_WITHOUT_HANDLER")],
                         ["No matching backend handler was found for GET /api/missing*."])
        [approve] = [e for e in self.graph.edges if e.kind == "CALLS_API" and self.graph.nodes[e.target].label.endswith("/approve")]
        self.assertEqual(approve.resolution, "ambiguous")
        self.assertIn("through a runtime segment", approve.detail)

    def test_a_base_url_parameter_default_is_the_requested_base(self):
        self.write("backend/routers/reports.py",
                   "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/reports')\n"
                   "@router.get('/index')\ndef index(): return {}\n")
        self.write("frontend/src/index.jsx",
                   "export default function Widget({ apiBaseUrl = '/api' }) {\n"
                   "  fetch(`${apiBaseUrl}/reports/index`);\n  return null;\n}\n")
        self.scan()
        self.assertIn(("Widget", "GET /api/reports/index"), self.edges("CALLS_API"))
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_a_configured_origin_may_carry_the_route_prefix(self):
        self.write("backend/routers/orders.py",
                   "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/orders')\n"
                   "@router.get('')\ndef orders(): return []\n")
        self.write("frontend/src/index.jsx",
                   "export default function App() {\n"
                   "  fetch(`${process.env.NEXT_PUBLIC_API_URL}/orders`);\n"
                   "  fetch(`${process.env.NEXT_PUBLIC_API_URL}/nowhere`);\n  return null;\n}\n")
        self.scan()
        [edge] = [e for e in self.graph.edges if e.kind == "CALLS_API"
                  and self.graph.nodes[e.target].label == "GET /api/orders"]
        self.assertIn("configured origin may carry", edge.detail)
        self.assertEqual([i.message for i in self.issues("API_CALL_WITHOUT_HANDLER")],
                         ["No matching backend handler was found for GET /nowhere."])

    def test_a_literal_call_is_not_served_by_a_different_literal_route(self):
        self.write("backend/routers/items.py",
                   "from fastapi import APIRouter\nrouter = APIRouter(prefix='/api/items')\n"
                   "@router.get('/export')\ndef export(): return []\n")
        self.write("frontend/src/index.jsx", "export default function App() { fetch('/api/items/archive'); return null; }\n")
        self.scan()
        self.assertEqual(len(self.issues("API_CALL_WITHOUT_HANDLER")), 1)

    def test_destructured_route_handler_exports_are_endpoints(self):
        self.write("package.json", "{}")
        self.write("src/auth.ts", "export const { handlers, auth } = createAuth({});\n")
        self.write("src/app/api/auth/[...slug]/route.ts", 'import { handlers } from "@/auth";\nexport const { GET, POST } = handlers;\n')
        self.write("src/app/page.tsx", "export default function Home() { fetch('/api/auth/session'); return null; }\n")
        self.scan()
        self.assertTrue({"GET /api/auth/{dynamic}", "POST /api/auth/{dynamic}"} <= self.labels("endpoint"))
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_typescript_import_types_are_not_parse_errors(self):
        for source in ("const v = ([] as unknown as import('../c').Item[]);\n",
                       "interface P { items?: readonly import('../c').Item[]; }\n",
                       "function f(x: import(\n  './c'\n).Item) { return x; }\nexport const g = () => broken(;\n"):
            facts = javascript.parse_source(source, ".tsx")
            if "broken" in source:
                self.assertEqual(facts.errors, [4])  # a real error is still reported, on its own line
            else:
                self.assertEqual(facts.errors, [], source)
        lazy = javascript.parse_source("export const load = () => import('./page').then((m) => m.default);\n", ".ts")
        self.assertIn("./page", {module for module, *_ in lazy.imports})


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class ReviewedExtractionTests(Repo):
    """Each test pins one reviewed false warning or silently dropped fact in JS/TS extraction."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def gap_severities(self, prefix):
        return {i.message.split(f" was found for {prefix}")[1].split(".")[0]: i.severity
                for i in self.issues("API_CALL_WITHOUT_HANDLER")}

    def test_long_plus_chains_evaluate_in_linear_time(self):
        names = [f"a{i}" for i in range(40)]
        for url in ("'/api/items/' + " + " + ".join(names), " + ".join(names)):
            started = time.perf_counter()
            javascript.parse_source(f"export const f = ({', '.join(names)}) => fetch({url});\n", ".ts")
            self.assertLess(time.perf_counter() - started, 2.0)

    def test_a_literal_absolute_client_base_is_an_external_service(self):
        self.write("src/lib/gh.ts", 'export const gh = axios.create({ baseURL: "https://api.github.com" });\n'
                                    'export const repos = () => gh.get("/user/repos");\n')
        self.write("src/pages/Board.jsx", "export default function Board() { const { api } = useSession(); api.get('/orders'); return null; }\n")
        self.write("backend/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/user/repos")\ndef r(): return []\n')
        self.scan()
        self.assertNotIn(("repos", "GET /user/repos"), self.edges("CALLS_API"))
        self.assertIn("src/lib/gh.ts:2", [i.evidence for i in self.issues("EXTERNAL_API_REFERENCE")])
        # The GitHub client is not "the repository's only client base URL" for an untraced client.
        edge = next(e for e in self.graph.edges if e.kind == "CALLS_API" and e.evidence == "src/pages/Board.jsx:1")
        self.assertEqual(self.graph.nodes[edge.target].label, "GET /orders")
        self.assertIn("its base URL is unknown", edge.detail)

    def test_head_is_served_by_get_and_options_is_never_a_gap(self):
        self.write("app/api/items/route.ts", "export async function GET() { return new Response(); }\n")
        self.write("app/page.tsx", "export default function P() {\n  fetch('/api/items', { method: 'HEAD' });\n"
                                   "  fetch('/api/items', { method: 'OPTIONS' });\n  fetch('/api/none', { method: 'OPTIONS' });\n"
                                   "  fetch('/api/items', { method: 'PUT' });\n  return null;\n}\n")
        self.scan()
        self.assertIn(("P", "GET /api/items"), self.edges("CALLS_API"))
        self.assertEqual([i.message.split(" has no handler")[0] for i in self.issues("API_METHOD_MISMATCH")], ["PUT /api/items"])
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_allow_listed_request_input_is_not_an_injection(self):
        self.write("app/api/items/route.ts",
                   "const COLUMNS = { name: 'name', created: 'created_at' };\nconst ORDER = ['asc', 'desc'];\n"
                   "export async function GET(req) {\n"
                   "  const sort = req.nextUrl.searchParams.get('sort');\n"
                   "  await pool.query(`SELECT * FROM items ORDER BY ${COLUMNS[sort] ?? 'id'}`);\n"
                   "  const dir = ORDER.includes(sort) ? sort : 'asc';\n"
                   "  await pool.query(`SELECT * FROM items ORDER BY id ${dir}`);\n"
                   "  await pool.query(`SELECT * FROM items ORDER BY id ${!ORDER.includes(sort) ? 'asc' : sort}`);\n"
                   "  return pool.query(`SELECT * FROM items ORDER BY ${sort}`);\n}\n")
        self.scan()
        self.assertEqual([i.evidence for i in self.issues("SQL_INJECTION_RISK")], ["app/api/items/route.ts:9"])

    def test_no_dead_code_judgement_when_component_files_are_not_read(self):
        self.write("package.json", "{}")
        self.write("src/main.ts", "import App from './App.vue';\n")
        self.write("src/App.vue", "<script setup>\nimport { save } from './lib/save';\n</script>\n")
        self.write("src/lib/save.ts", "export const save = () => fetch('/api/save', { method: 'POST' });\n")
        self.scan()
        self.assertEqual([i.severity for i in self.issues("API_CALL_WITHOUT_HANDLER")], ["warning"])
        self.assertNotIn("UNRESOLVED_LOCAL_IMPORT", self.codes())

    def test_framework_entry_files_are_live(self):
        self.write("package.json", "{}")
        files = {"app/routes/items.$id.tsx": "remixRoute", "app/root.tsx": "remixRoot", "app/entry.server.tsx": "remixEntry",
                 "src/routes/blog/+page.ts": "sveltePage", "src/routes/+layout.server.ts": "svelteLayout",
                 "app/sitemap.ts": "sitemap", "app/opengraph-image.tsx": "ogImage", "app/@modal/default.tsx": "slotDefault",
                 "app/global-error.tsx": "globalError", "src/lib/old.ts": "old"}
        for path, name in files.items():
            self.write(path, f"export const {name} = () => fetch('/api/{name}', {{ method: 'POST' }});\n")
        self.scan()
        severities = self.gap_severities("POST /api/")
        self.assertEqual(severities.pop("old"), "info")  # judgements are still made
        self.assertEqual((len(severities), set(severities.values())), (len(files) - 1, {"warning"}))

    def test_functions_used_by_value_or_required_by_name_are_not_dead(self):
        self.write("package.json", "{}")
        self.write("src/api.ts", "export function load() { return fetch('/api/load', { method: 'POST' }); }\n"
                                 "export function stale() { return fetch('/api/stale', { method: 'POST' }); }\n"
                                 "export const routes = [{ loader: load }];\nexport const staleRoutes = [{ loader: stale }];\n")
        self.write("src/cjs.js", "function save() { return fetch('/api/save', { method: 'POST' }); }\n"
                                 "function drop() { return fetch('/api/drop', { method: 'POST' }); }\nmodule.exports = { save, drop };\n")
        self.write("src/main.ts", "import { routes } from './api';\nconst { save } = require('./cjs');\nsave();\nconsole.log(routes);\n")
        self.scan()
        self.assertEqual(self.gap_severities("POST /api/"), {"load": "warning", "save": "warning", "stale": "info", "drop": "info"})
        facts = javascript.parse_source("const { load, save: store } = require('./api');\n", ".js")
        self.assertEqual(facts.imports, [("./api", "load", "load", 1), ("./api", "store", "save", 1)])

    def test_a_hook_bound_client_is_decided_at_the_call_site(self):
        self.write("src/pages/Board.jsx",
                   "export function A() { const { api } = useSession(); api.get('/api/a'); return null; }\n"
                   "export function B({ api }) { api.get('/api/b'); return null; }\n"
                   "export function C() { const router = useRouter(); router.get('/api/c'); return null; }\n")
        self.scan()
        self.assertEqual(self.edges("CALLS_API"), {("A", "GET /api/a")})

    def test_only_single_argument_wrappers_alias_an_export(self):
        facts = javascript.parse_source("export const selectUser = createSelector(selectState, (s) => s.user);\n"
                                        "export const List = memo(ItemList);\nexport const Styled = withTheme(Button, extra);\n"
                                        "export default React.forwardRef(Input);\n", ".tsx")
        self.assertEqual(facts.exports, {"selectUser": "selectUser", "List": "ItemList", "Styled": "Styled", "default": "Input"})

    def test_a_default_exported_client_keeps_its_base_url(self):
        self.write("src/lib/api.ts", "export default axios.create({ baseURL: '/api/v1' });\n")
        self.write("src/lib/legacy.js", "module.exports = axios.create({ baseURL: '/api/v2' });\n")
        self.write("src/lib/ky.ts", "export const k = ky.create({ prefixUrl: '/api/v3' });\n")
        self.write("src/a.ts", "import api from './lib/api';\nimport { k } from './lib/ky';\n"
                               "export const a = () => api.get('/items');\nexport const c = () => k.get('orders');\n")
        self.write("src/b.js", "const legacy = require('./lib/legacy');\nexports.b = () => legacy.get('/things');\n")
        self.scan()
        self.assertTrue({("a", "GET /api/v1/items"), ("b", "GET /api/v2/things"), ("c", "GET /api/v3/orders")}
                        <= self.edges("CALLS_API"))

    def test_prose_passed_to_a_query_method_is_not_dynamic_sql(self):
        self.write("ui.ts", "export function confirmDelete(name) { return dialog.query(`Delete ${name}?`); }\n"
                            "export function byTable(t) { return pool.query(`SELECT id FROM ${t}`); }\n")
        self.scan()
        self.assertEqual([i.evidence for i in self.issues("DYNAMIC_SQL") if "splices in" in i.message], ["ui.ts:2"])
        self.assertNotIn("SQL_INJECTION_RISK", self.codes())

    def test_request_input_shapes_beyond_express_and_next(self):
        self.write("server/handlers.ts",
                   "export async function koa(ctx) { await db.query(`SELECT * FROM t WHERE a = '${ctx.request.body.a}'`); }\n"
                   "export async function koaParams(ctx) { await db.query(`SELECT * FROM t WHERE a = '${ctx.params.id}'`); }\n"
                   "export async function hapi(request) { await db.query(`SELECT * FROM t WHERE a = '${request.payload.a}'`); }\n"
                   "export async function search(url) { await db.query(`SELECT * FROM t WHERE a = '${url.searchParams.get('q')}'`); }\n"
                   "export async function loader({ params }) { await db.query(`SELECT * FROM t WHERE a = '${params.id}'`); }\n"
                   "export async function helper({ params }) { await db.query(`SELECT * FROM t WHERE a = '${params.id}'`); }\n"
                   "export class ItemsController {\n  async update(@Body() dto, @Param('id') id) {\n"
                   "    await db.query(`UPDATE t SET a = '${dto.a}' WHERE id = ${id}`);\n  }\n}\n"
                   "export async function sane(req) { await db.query(`SELECT * FROM t LIMIT ${Number(req.query.n)} OFFSET ${parseInt(req.query.o, 10)}`); }\n")
        self.scan()
        self.assertEqual(sorted(int(i.evidence.rsplit(":", 1)[1]) for i in self.issues("SQL_INJECTION_RISK")), [1, 2, 3, 4, 5, 9])
        self.assertTrue({"server/handlers.ts:6", "server/handlers.ts:12"}
                        <= {i.evidence for i in self.issues("DYNAMIC_SQL") if "splices in" in i.message})

    def test_members_of_a_module_exports_object_are_named_exports(self):
        facts = javascript.parse_source("module.exports = { list() { return 1; }, show: () => 2 };\n", ".js")
        self.assertEqual({(s.name, s.exported, s.default_export) for s in facts.symbols},
                         {("list", True, False), ("show", True, False)})
        facts = javascript.parse_source("module.exports = function run() {};\n", ".js")
        self.assertTrue(facts.symbols and all(s.default_export for s in facts.symbols))

    def test_url_constants_respect_scope_and_spelling(self):
        facts = javascript.parse_source(
            "const API = '/api';\nconst SVC_T = `https://svc.example.com`;\nconst SVC_S = 'https://svc.example.com';\n"
            "export function a() { const API = '/other'; return fetch(`${API}/items`); }\n"
            "export function b() { return fetch(`${API}/items`); }\n"
            "export function c(API) { return fetch(API + '/items'); }\n"
            "export function d() { return fetch(`${SVC_T}/items`); }\n"
            "export function e() { return fetch(`${SVC_S}/items`); }\n"
            "const API_URL = `https://svc.example.com`;\nconst BASE_URL = 'https://svc.example.com';\n"
            "export function f() { return fetch(`${API_URL}/items`); }\n"
            "export function g() { return fetch(`${BASE_URL}/items`); }\n", ".js")
        # A template without substitutions is the same constant as a quoted string, and an
        # origin-named absolute constant is a configured origin in either spelling.
        self.assertEqual([(r.owner, r.url, r.configured_origin) for r in facts.requests],
                         [("a", "/other/items", ""), ("b", "/api/items", ""),
                          ("d", "https://svc.example.com/items", ""), ("e", "https://svc.example.com/items", ""),
                          ("f", "/items", "API_URL"), ("g", "/items", "BASE_URL")])
        self.assertIn(6, facts.uncertain_requests)

    def test_callback_clients_and_optional_chaining_are_requests(self):
        facts = javascript.parse_source(
            "$.get('/api/items', function (data) { render(data); });\n"
            "const app = express();\napp.get('/health', (req, res) => res.send('ok'));\n"
            "router.post('/x', (req, res) => res.json({}));\n"
            "export const load = () => api?.get('/api/orders');\n", ".js")
        self.assertEqual([(r.receiver, r.url) for r in facts.requests], [("$", "/api/items"), ("api", "/api/orders")])

    def test_angular_injected_http_clients(self):
        facts = javascript.parse_source(
            "export class A { private http = inject(HttpClient); list() { return this.http.get('/api/a'); } }\n"
            "export class B { constructor(private readonly http: HttpClient) {} save() { return this.http.post('/api/b', {}); } }\n"
            "export class C { http = makeClient(); list() { return this.http.get('/api/c'); } }\n", ".ts")
        self.assertEqual([(r.method, r.url, r.receiver, r.hook_bound) for r in facts.requests],
                         [("GET", "/api/a", "this.http", True), ("POST", "/api/b", "this.http", True)])
        self.write("src/app/items.service.ts",
                   "export class ItemsService { private http = inject(HttpClient); list() { return this.http.get('/api/a'); } }\n")
        self.scan()
        self.assertIn(("ItemsService.list", "GET /api/a"), self.edges("CALLS_API"))

    def test_raw_unsafe_and_text_queries_and_sanitizers(self):
        facts = javascript.parse_source(
            "export async function q(req) {\n"
            "  await prisma.$queryRawUnsafe(`SELECT * FROM t WHERE a = '${req.query.a}'`);\n"
            "  await pool.query({ text: `SELECT * FROM u WHERE b = '${req.body.b}'` });\n"
            "  await pool.query(`SELECT * FROM v LIMIT ${Number(req.query.n)} OFFSET ${parseInt(req.query.o)}`);\n}\n", ".ts")
        self.assertEqual([(line, [origin for _, origin, _ in holes]) for _, line, holes in facts.sql_interpolations],
                         [(2, ["HTTP request input"]), (3, ["HTTP request input"]), (4, ["", ""])])

    def test_a_bare_ampersand_in_a_jsx_file_is_not_a_parse_error(self):
        facts = javascript.parse_source("export default function P() { return <p>R&D & Co &amp; more</p>; }\n", ".jsx")
        self.assertEqual(facts.errors, [])
        self.assertIn("P", {s.name for s in facts.symbols})


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class AuditedLinkageTests(Repo):
    """Each test pins one audited false warning, false link or pathological input in JS/TS linkage."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def test_routes_in_unmodelled_backends_make_handler_gaps_info(self):
        self.write("web/src/main.tsx", "export function A() { fetch('/api/orders'); return fetch('/api/orders', { method: 'PUT' }); }\n")
        self.write("server/routes.js", "const express = require('express');\nconst router = express.Router();\n"
                                       "router.get('/orders', listOrders);\nmodule.exports = router;\n")
        self.scan()
        self.assertEqual(self.edges("CALLS_API"), {("A", "GET /api/orders"), ("A", "PUT /api/orders")})
        gaps = self.issues("API_CALL_WITHOUT_HANDLER")
        self.assertEqual({i.severity for i in gaps}, {"info"})
        self.assertIn("`router.get()` on a router (server/routes.js:3)", gaps[0].message)
        for path, text in (("server/routes.js", "export const x = 1;\n"),
                           ("src/orders.controller.ts", "@Controller('orders')\nexport class Orders { @Get(':id') find() { return 1; } }\n"),
                           ("app.py", "from flask import Flask\napp = Flask(__name__)\n")):
            self.write(path, text)
            self.scan()
            self.assertEqual({i.severity for i in self.issues("API_CALL_WITHOUT_HANDLER")}, {"info"} if path != "server/routes.js" else {"warning"}, path)
            (self.root / path).unlink()

    def test_a_listening_server_s_literal_routes_are_endpoints(self):
        self.write("server/index.js", "const express = require('express');\nconst app = express();\n"
                                      "app.get('/api/orders/:id', (req, res) => res.json({}));\napp.listen(3000);\n")
        self.write("web/src/main.ts", "export const load = (id) => fetch(`/api/orders/${id}`);\n"
                                      "export const drop = () => fetch('/api/orders/1', { method: 'DELETE' });\n")
        self.scan()
        self.assertIn(("load", "GET /api/orders/{dynamic}"), self.edges("CALLS_API"))
        [mismatch] = self.issues("API_METHOD_MISMATCH")
        self.assertEqual(mismatch.severity, "warning")  # every route is modelled, so a 405 is still a warning

    def test_taint_through_reused_bindings_is_linear(self):
        lines = ["export async function h(req) {", "  const x0 = req.query.a;"]
        lines += [f"  const x{i} = " + " + ".join([f"x{i - 1}"] * 6) + ";" for i in range(1, 25)]
        lines.append("  return pool.query(`SELECT * FROM t WHERE a = '${x24}'`);\n}\n")
        started = time.perf_counter()
        facts = javascript.parse_source("\n".join(lines), ".ts")
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual([origin for _, _, holes in facts.sql_interpolations for _, origin, _ in holes], ["HTTP request input"])

    def test_many_functions_sharing_parameter_names_parse_in_linear_time(self):
        source = "".join(f"export async function f{i}(id) {{ return fetch(`/api/items/${{id}}`); }}\n" for i in range(5000))
        started = time.perf_counter()
        facts = javascript.parse_source("const id = 'x';\n" + source, ".ts")
        self.assertLess(time.perf_counter() - started, 6.0)
        self.assertEqual({r.url for r in facts.requests}, {"/api/items/{dynamic}"})  # each `id` is its parameter, not the constant

    def test_a_long_plus_chain_does_not_lose_the_file(self):
        chain = " + ".join(["'/a'"] * 2000)
        self.write("src/a.ts", f"const API = {chain};\nexport const load = () => fetch(API + '/x');\n"
                               f"export const other = (x) => fetch('/api/b/' + {' + '.join(['x'] * 2000)});\n")
        started = time.perf_counter()
        self.scan()
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertFalse({"FILE_TOO_DEEPLY_NESTED", "FILE_SCAN_FAILED"} & self.codes())
        self.assertIn(("load", "GET " + "/a" * 2000 + "/x"), self.edges("CALLS_API"))


H = ("export async function h(req){ const ids = req.body.ids; const n = req.query.n; const x = req.query.x;\n")
R = "HTTP request input"
#: (body appended to H, the origin of each spliced value in source order: "" is safe).
SANITISER_CASES = [
    # safe
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map((_, i) => `$${i + 1}`).join(',')})`, ids); }", [""]),
    ("const ph = ids.map(() => '?').join(','); return pool.query(`SELECT * FROM t WHERE id IN (${ph})`, ids); }", [""]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${Array.from({ length: ids.length }, (_, i) => `$${i + 1}`).join(', ')})`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${+n}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${-n} OFFSET ${~~x}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM t OFFSET ${(n - 1) * x}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${n | 0} OFFSET ${x >> 1}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${pool.escape(x)} AND b = ${mysql.escapeId(n)}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM ${client.escapeIdentifier(x)} WHERE a = ${client.escapeLiteral(n)}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM ${format.ident(x)} WHERE a = ${format.literal(n)}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM ${format('%I', x)} WHERE a = ${format('%L', n)}`); }", ["", ""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${Number.isInteger(n) ? n : 10}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${!Number.isFinite(n) ? 10 : n}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n === 'number' ? n : 10}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${'number' !== typeof n ? 10 : n}`); }", [""]),
    # risky
    ("return pool.query(`SELECT * FROM t WHERE a = '${x}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map((id) => `'${id}'`).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map(() => '?').join(x)})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${x + 'y'}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${String(x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${format('%s', x)}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${!Number.isNaN(n) ? n : 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${typeof x === 'string' ? x : ''}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${Number.isInteger(n) ? x : 10}`); }", [R]),
    # map / join / fill: a callback reading the element or the array itself carries the values
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map((_, i, all) => all[i]).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.flatMap((_, i, a) => [a[i]]).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map(function () { return arguments[0]; }).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map((_, i) => ids[i]).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map(function (_, i) { return this[i]; }, ids).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${Array.from(ids, (_, i) => ids[i]).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${Array.from(ids, (v) => v).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.fill('?').join(',')})`); }", [""]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.fill('?', 1).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${new Array(3).fill(x).join(',')})`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE id IN (${ids.map(() => '?').concat(x).join(',')})`); }", [R]),
    # arithmetic versus concatenation, conversions
    ("return pool.query(`SELECT * FROM t WHERE a = ${'a' + x}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${n + 1}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${n - 0}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${+n + '0'}`); }", [""]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${-n + x}`); }", ["", R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${n || 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${n ?? 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${x.length}`); }", [""]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${x.slice(0, 5)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${JSON.stringify(x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${encodeURIComponent(x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${parseInt(x).toString()}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${x.toFixed(2)}`); }", [""]),
    ("return pool.query(`SELECT * FROM t LIMIT ${Math.min(n, 100)}`); }", [""]),
    # escape look-alikes: only SQL drivers' escapers quote SQL
    ("return pool.query(`SELECT * FROM t WHERE a = '${escape(x)}'`); }", [""]),  # the global escape encodes ' and spaces
    ("return pool.query(`SELECT * FROM t LIMIT ${_.escape(x)}`); }", [R]),
    ("return pool.query(`SELECT * FROM t ORDER BY ${validator.escape(x)}`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = '${he.escape(x)}'`); }", [R]),
    ("const escape = (s) => s; return pool.query(`SELECT * FROM t WHERE a = '${escape(x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t ORDER BY ${CSS.escape(x)}`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a ~ '${escapeRegExp(x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${SqlString.escape(x)}`); }", [""]),
    # format look-alikes
    ("return pool.query(`SELECT * FROM t WHERE a = '${util.format('%s', x)}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${util.format('%L', x)}`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE d = '${format(x, 'yyyy')}'`); }", [R]),
    ("return pool.query(`SELECT * FROM t WHERE a = ${format('%%I %L', x)}`); }", [""]),  # %% is a literal percent sign
    ("return pool.query(`SELECT * FROM ${format('%1$I', x)}`); }", [R]),  # positional: an accepted false positive
    # guards that do and do not decide the spliced branch
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n === 'number' ? 10 : n}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n !== 'number' ? n : 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n === 'number' || x ? n : 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n === 'number' && x ? n : 10}`); }", [""]),
    ("const ok = typeof n === 'number'; return pool.query(`SELECT * FROM t LIMIT ${ok ? n : 10}`); }", [R]),  # accepted false positive
    ("return pool.query(`SELECT * FROM t LIMIT ${typeof n === 'number' ? (n = x, n) : 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${isFinite(n) ? n : 10}`); }", [R]),
    ("return pool.query(`SELECT * FROM t LIMIT ${Number.isInteger(+n) ? n : 10}`); }", [R]),
    ("const lim = Number.isInteger(n) ? n : 10; return pool.query(`SELECT * FROM t LIMIT ${lim}`); }", [""]),
    ("if (!Number.isInteger(n)) throw new Error(); return pool.query(`SELECT * FROM t LIMIT ${n}`); }", [""]),  # an early exit
    ("return pool.query(`SELECT * FROM t WHERE a = ${typeof x === 'object' ? x : 1}`); }", [R]),
    # block scope: a `const` in one block is not the same-named `const` in the next (5 is spelled in, no hole)
    ("{ const v = req.query.x; log(v); } { const v = 5; return pool.query(`SELECT * FROM t LIMIT ${v}`); } }", []),
]


def origins(source: str) -> list[str]:
    return [origin for _, _, holes in javascript.parse_source(source, ".ts").sql_interpolations for _, origin, _ in holes]


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class SqlInterpolationTests(Repo):
    """What a value spliced into SQL text is judged to be: request input, or provably not text."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def test_sanitisers_and_their_look_alikes(self):
        for body, expected in SANITISER_CASES:
            with self.subTest(body=body):
                self.assertEqual(origins(H + body), expected)

    def test_a_long_plus_chain_keeps_its_query_and_every_hole(self):
        source = ("export async function h(req) { return pool.query(\"SELECT * FROM orders WHERE a = \" + req.query.a"
                  " + \" AND b = \" + req.query.b + \" AND c = \" + req.query.c + \" LIMIT \" + req.query.d); }\n")
        facts = javascript.parse_source(source, ".ts")
        self.assertEqual(len(facts.queries), 1)
        self.assertEqual(origins(source), [R, R, R, R])
        self.write("api/orders.ts", source)
        self.scan()
        self.assertEqual(len(self.issues("SQL_INJECTION_RISK")), 1)
        self.assertIn(("h", "orders"), self.edges("TOUCHES_STORE"))

    def test_self_assigned_sql_is_spelled_in_linear_time(self):
        lines = ["export async function h(req) {", "  let sql = 'SELECT * FROM t WHERE 1=1';"]
        lines += [f"  sql = sql + ` AND c{i} = '${{req.query.c{i}}}'`;" for i in range(160)]
        lines += ["  return pool.query(sql);", "}"]
        interleaved = ["export async function g(req) {", "  let sql = 'SELECT * FROM t WHERE 1=1';"]
        interleaved += [f"  sql += ` AND c{i} = 1`; await pool.query(sql);" for i in range(1000)] + ["}"]
        started = time.perf_counter()
        facts = javascript.parse_source("\n".join(lines), ".ts")
        javascript.parse_source("\n".join(interleaved), ".ts")
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertEqual(len(facts.queries), 1)
        self.assertTrue(facts.sql_interpolations)

    def test_sibling_blocks_declaring_the_same_name_parse_in_linear_time(self):
        body = "".join(f"  if (a{i}) {{ const id = {i}; await pool.query(`SELECT * FROM t WHERE id = ${{id}}`); }}\n"
                       for i in range(3000))
        started = time.perf_counter()
        facts = javascript.parse_source("export async function h(req) {\n" + body + "}\n", ".ts")
        self.assertLess(time.perf_counter() - started, 10.0)
        self.assertEqual(len(facts.queries), 3000)
        self.assertEqual(facts.sql_interpolations, [])  # each `id` is its own block's constant

    def test_an_assignment_to_another_variable_of_the_same_name_does_not_taint(self):
        other_function = ("let where = '';\nfunction setFilter(req) { let where; where = req.query.w; return where; }\n"
                          "export async function run() { return pool.query(`SELECT * FROM t WHERE ${where}`); }\n")
        parameter = ("export async function h(req) {\n  function inner(q) { q = req.query.x; return q; }\n"
                     "  const q = 'fixed';\n  return pool.query(`SELECT * FROM t WHERE a = '${q}'`);\n}\n")
        closure = ("export async function h(req) {\n  let q = 'x';\n  const inner = () => { q = req.query.x; };\n"
                   "  return pool.query(`SELECT * FROM t WHERE a = '${q}'`);\n}\n")
        self.assertNotIn(R, origins(other_function))
        self.assertNotIn(R, origins(parameter))
        self.assertEqual(origins(closure), [R])  # the closure writes the outer `let`

    def test_taint_does_not_depend_on_which_query_is_read_first(self):
        chain = ["export async function h(req) {", "  const x0 = req.query.a;"] + [f"  const x{i} = x{i - 1};" for i in range(1, 61)]
        far_first = origins("\n".join(chain + ["  await pool.query(`SELECT '${x60}'`);", "  await pool.query(`SELECT '${x20}'`);", "}"]))
        near_only = origins("\n".join(chain + ["  await pool.query(`SELECT '${x20}'`);", "}"]))
        self.assertEqual(near_only, [R])
        self.assertEqual(far_first[1], R)

    def test_a_right_nested_url_concatenation_does_not_lose_the_file(self):
        nested = "'/a'"
        for _ in range(600):
            nested = f"'/a' + ({nested})"
        self.write("src/a.ts", f"export const load = () => fetch({nested});\nexport const ok = () => fetch('/api/ok');\n")
        self.scan()
        self.assertFalse({"FILE_TOO_DEEPLY_NESTED", "FILE_SCAN_FAILED"} & self.codes())
        self.assertIn(("ok", "GET /api/ok"), self.edges("CALLS_API"))


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class UnmodelledRouteEvidenceTests(Repo):
    """Only code that could serve the application's routes lowers a handler gap to info."""

    WEB = "export function A() { fetch('/api/missing'); return fetch('/api/items', { method: 'PATCH' }); }\n"

    def severities(self, extra):
        for path, text in {"package.json": "{}", "app/api/items/route.ts": "export async function GET() { return new Response(); }\n",
                           "app/page.tsx": "import { A } from '../web/src/main';\nexport default function P() { A(); return null; }\n",
                           "web/src/main.tsx": self.WEB, **extra}.items():
            self.write(path, text)
        self.scan()
        return sorted({(i.code, i.severity) for i in self.graph.issues
                       if i.code in {"API_CALL_WITHOUT_HANDLER", "API_METHOD_MISMATCH"}})

    def fresh(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_look_alike_registrations_keep_gaps_as_warnings(self):
        warnings = [("API_CALL_WITHOUT_HANDLER", "warning"), ("API_METHOD_MISMATCH", "warning")]
        for name, extra in {
            "client-side navigator": {"web/src/nav.ts": "const router = createNavigator();\nrouter.get('/orders', () => render('orders'));\n"},
            "map lookup": {"web/src/cache.ts": "const app = new Map();\nexport const v = app.get('/orders', () => 1);\n"},
            "test mock server": {"__tests__/api.test.ts": "import express from 'express';\nconst app = express();\n"
                                                          "app.get('/api/orders', (req, res) => res.json([]));\n"},
            "custom server catch-all": {"server.js": "const express = require('express');\nconst server = express();\n"
                                                     "server.all('*', (req, res) => handle(req, res));\nserver.listen(3000);\n"},
            "client data-router loader": {"web/src/routes/orders.tsx": "export async function loader() { return null; }\n"
                                                                       "export default function Orders() { return null; }\n"},
            "flask import in a test": {"tests/test_compat.py": "from flask import Flask\n"},
            "django import in a script": {"scripts/seed.py": "from django.conf import settings\n"},
        }.items():
            with self.subTest(name):
                self.fresh()
                self.assertEqual(self.severities(extra), warnings)

    def test_real_unmodelled_registrations_still_lower_gaps(self):
        info = [("API_CALL_WITHOUT_HANDLER", "info"), ("API_METHOD_MISMATCH", "info")]
        for name, extra in {
            "express router": {"server/routes.js": "const express = require('express');\nconst router = express.Router();\n"
                                                   "router.all('/orders', listOrders);\nmodule.exports = router;\n"},
            "remix loader": {"app/routes/orders.tsx": "import { json } from '@remix-run/node';\n"
                                                      "export async function loader() { return json([]); }\n"},
            "flask application": {"backend/app.py": "from flask import Flask\napp = Flask(__name__)\n"},
        }.items():
            with self.subTest(name):
                self.fresh()
                self.assertEqual(self.severities(extra), info)
                if name == "express router":
                    message = next(i.message for i in self.graph.issues if i.code == "API_CALL_WITHOUT_HANDLER")
                    self.assertIn("`router.all()` on a router", message)

    def test_route_chains_route_objects_and_parameter_routers_are_unmodelled_routes(self):
        info = [("API_CALL_WITHOUT_HANDLER", "info"), ("API_METHOD_MISMATCH", "info")]
        for name, extra in {
            "exported function, handlers by reference": {
                "server/orders.js": "module.exports = function (app) { app.get('/api/orders', orders.list); };\n"},
            "route chain": {
                "server/orders.js": "const router = require('express').Router();\n"
                                    "router.route('/api/orders').get(list).post(create);\nmodule.exports = router;\n"},
            "hapi route object": {
                "server/orders.js": "exports.plugin = { register(server) {\n"
                                    "  server.route({ method: 'GET', path: '/api/orders', handler: list });\n} };\n"},
            "fastify route object": {
                "server/orders.js": "export default async function (fastify) {\n"
                                    "  fastify.route({ method: 'POST', url: '/api/orders', handler: create });\n}\n"},
        }.items():
            with self.subTest(name):
                self.fresh()
                self.assertEqual(self.severities(extra), info)
        for name, extra in {
            "a client parameter given an options object": {
                "web/src/load.ts": "export function load(api) { return api.get('/api/orders', { params: {} }); }\n"},
            "a route lookup on a non-router": {
                "web/src/nav.ts": "export const t = navigation.route('/orders').get('title');\n"},
            "an object without a handler": {
                "web/src/menu.ts": "export const m = menu.route({ method: 'GET', path: '/orders' });\n"},
        }.items():
            with self.subTest(name):
                self.fresh()
                self.assertEqual(self.severities(extra), [("API_CALL_WITHOUT_HANDLER", "warning"),
                                                          ("API_METHOD_MISMATCH", "warning")])


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class LivenessEntryTests(Repo):
    """Code a bundler, package manager or router loads is live, so its gaps stay warnings."""

    def gap(self, files):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for path, text in files.items():
            self.write(path, text)
        self.scan()
        return [(i.severity, i.message) for i in self.graph.issues if i.code == "API_CALL_WITHOUT_HANDLER"]

    def test_loaded_code_is_not_judged_dead(self):
        page = {"package.json": "{}", "app/page.tsx": "export default function P(){ return null; }\n"}
        settings = {"src/Settings.tsx": "export default function Settings(){ fetch('/api/missing'); return null; }\n"}
        for name, files in {
            "React.lazy": {**page, **settings, "app/layout.tsx":
                           "const S = React.lazy(() => import('../src/Settings'));\nexport default function L(){ return null; }\n"},
            "next/dynamic": {**page, **settings, "app/layout.tsx":
                             "import dynamic from 'next/dynamic';\nconst S = dynamic(() => import('../src/Settings'));\n"
                             "export default function L(){ return null; }\n"},
            "a side-effect import": {**page, **settings, "app/layout.tsx":
                                     "import '../src/Settings';\nexport default function L(){ return null; }\n"},
            "package.json bin": {"package.json": '{"bin": {"tool": "bin/cli.js"}}', "bin/cli.js": "require('../lib/run').run();\n",
                                 "lib/run.js": "exports.run = () => fetch('/api/missing');\n"},
            "package.json main without extension": {"package.json": '{"main": "lib/start"}', "lib/start.js": "fetch('/api/missing');\n",
                                                    "app/page.tsx": "export default function P(){ return null; }\n"},
            "a barrel index is not an entry": {"src/index.ts": "export * from './a';\n", "src/a.ts": "export const a = 1;\n",
                                               "lib/run.js": "fetch('/api/missing');\n"},
            "Expo Router": {"package.json": '{"main": "expo-router/entry"}', "app/index.tsx": "export default function H(){ return null; }\n",
                            "app/_layout.tsx": "export default function Layout(){ fetch('/api/missing'); return null; }\n",
                            "app/settings.tsx": "export default function S(){ fetch('/api/missing'); return null; }\n"},
        }.items():
            with self.subTest(name):
                self.assertEqual({severity for severity, _ in self.gap(files)}, {"warning"})
        # A module nothing loads is still judged dead.
        dead = self.gap({**page, "src/Unused.tsx": "export default function U(){ fetch('/api/missing'); return null; }\n"})
        self.assertEqual([severity for severity, _ in dead], ["info"])


@unittest.skipUnless(HAS_STACK, "install repolens[stack]")
class OriginAndMatchingTests(Repo):
    """Which origin a request goes to, which base URL is assumed for it, and how widely an
    incomplete URL may be matched."""

    def issues(self, code):
        return [i for i in self.graph.issues if i.code == code]

    def calls(self):
        return {(self.graph.nodes[e.source].label, self.graph.nodes[e.target].label, e.resolution)
                for e in self.graph.edges if e.kind == "CALLS_API"}

    def router(self, path, *routes):
        body = "from fastapi import APIRouter\nrouter = APIRouter()\n"
        for index, (method, route) in enumerate(routes):
            body += f"@router.{method}('{route}')\ndef handler_{index}(): return []\n"
        self.write(path, body)

    def test_a_vendor_origin_is_external_and_suffix_matches_are_ambiguous(self):
        self.router("backend/routes.py", ("get", "/api/v1/charges"), ("get", "/api/orders"), ("get", "/admin/orders"),
                    ("get", "/api/users/{user_id}"))
        self.write("frontend/src/index.jsx",
                   "export function Pay() { return fetch(`${process.env.STRIPE_API_URL}/v1/charges`); }\n"
                   "export function Any({ path }) { return fetch(`${process.env.NEXT_PUBLIC_API_URL}${path}`); }\n"
                   "export function Orders() { return fetch(`${process.env.NEXT_PUBLIC_API_URL}/orders`); }\n")
        self.scan()
        self.assertEqual({(s, t, r) for s, t, r in self.calls() if s in {"Pay", "Any"} and not t.endswith("/charges*")}, set())
        self.assertFalse({c for c in self.calls() if c[0] == "Pay"})
        [external] = self.issues("EXTERNAL_API_REFERENCE")
        self.assertIn("`STRIPE_API_URL`", external.message)
        self.assertEqual([i.evidence for i in self.issues("DYNAMIC_HTTP_REQUEST")], ["frontend/src/index.jsx:2"])
        self.assertTrue({("Orders", "GET /api/orders", "ambiguous"), ("Orders", "GET /admin/orders", "ambiguous")}
                        <= self.calls())
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_api_origins_names_an_origin_whose_words_do_not(self):
        self.router("backend/routes.py", ("get", "/api/orders"))
        self.write("frontend/src/index.jsx", "export function A() { return fetch(`${process.env.PAYMENTS_API_URL}/api/orders`); }\n")
        self.scan()
        self.assertEqual(self.calls(), set())
        self.write("repolens.toml", '[impact]\napi_origins = ["PAYMENTS_API_URL"]\n')
        from repolens.impact.config import Config
        self.graph = scan_repository(self.root, Config.load(self.root))
        self.assertEqual(self.calls(), {("A", "GET /api/orders", "probable")})

    def test_an_environment_or_localhost_client_base_is_a_configured_origin(self):
        self.router("backend/routes.py", ("get", "/orders"), ("get", "/items"))
        self.write("frontend/src/api.js",
                   "import axios from 'axios';\n"
                   "export const api = axios.create({ baseURL: process.env.API_URL });\n"
                   "export const local = axios.create({ baseURL: 'http://localhost:8000' });\n"
                   "export const marker = axios.create({ baseURL: '//configured.example.com/api' });\n"
                   "export const a = () => api.get('/orders');\nexport const b = () => local.get('/items');\n"
                   "export const c = () => marker.get('/items');\n"
                   "export const d = () => fetch('http://127.0.0.1:8000/items');\n")
        self.scan()
        self.assertEqual({c for c in self.calls() if c[0] in {"a", "b", "c", "d"}},
                         {("a", "GET /orders", "probable"), ("b", "GET /items", "probable"), ("d", "GET /items", "probable")})
        self.assertEqual([i.evidence for i in self.issues("EXTERNAL_API_REFERENCE")], ["frontend/src/api.js:7"])

    def test_only_a_base_url_parameter_default_is_a_base(self):
        facts = javascript.parse_source(
            "export function P({ id = 'me' }) { return fetch(`${id}/profile`); }\n"
            "export function Q({ base = '/api' }) { return fetch(`${base}/profile`); }\n", ".jsx")
        self.assertEqual([(r.url, r.dynamic) for r in facts.requests], [("{dynamic}/profile", True), ("/api/profile", True)])

    def test_query_string_tails_match_only_their_path(self):
        self.router("backend/routes.py", ("get", "/api/items"), ("get", "/api/items/{item_id}"), ("get", "/api/items/export/csv"))
        self.write("frontend/src/index.jsx",
                   "export function A({ params }) {\n"
                   "  const query = params.toString() ? `?${params}` : '';\n  return fetch(`/api/items${query}`);\n}\n"
                   "export function B({ status }) { return fetch('/api/items' + (status ? `?status=${status}` : '')); }\n"
                   "export function C({ suffix }) { return fetch(`/api/items${suffix}`); }\n")
        self.scan()
        calls = self.calls()
        self.assertEqual({t for s, t, _ in calls if s in {"A", "B"}}, {"GET /api/items"})
        self.assertEqual({t for s, t, _ in calls if s == "C" and not t.endswith("*")},
                         {"GET /api/items", "GET /api/items/{dynamic}", "GET /api/items/export/csv"})

    def test_a_request_wrapper_open_to_every_handler_is_not_linked(self):
        self.router("backend/routes.py", *[("get", f"/api/resource{index}") for index in range(13)])
        self.write("frontend/src/fetcher.js",
                   "import axios from 'axios';\n"
                   "export const fetcher = { get: (url) => axios.get(`${process.env.NEXT_PUBLIC_BACKEND_URL"
                   " || 'http://localhost:8000'}/api${url}`) };\n")
        self.scan()
        self.assertEqual({c for c in self.calls() if not c[1].endswith("*")}, set())
        [issue] = [i for i in self.issues("DYNAMIC_HTTP_REQUEST") if "handlers" in i.message]
        self.assertIn("GET /api* could be served by 13 handlers", issue.message)
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_the_assumed_base_is_per_package_and_never_added_twice(self):
        self.router("backend/routes.py", ("get", "/api/orders"), ("get", "/v1/items"), ("get", "/admin-api/users"),
                    ("get", "/api/profile"))
        self.write("web/package.json", "{}")
        self.write("web/src/lib/api.js", "import axios from 'axios';\nexport const client = axios.create({ baseURL: '/v1' });\n")
        self.write("web/src/Board.jsx",
                   "export function Board() {\n  const { api } = useSession();\n  api.get('/v1/items');\n"
                   "  $.get('/api/orders');\n  return null;\n}\n"
                   "export class ProfileService {\n  constructor(private http: HttpClient) {}\n"
                   "  load() { return this.http.get('/api/profile'); }\n}\n")
        self.write("admin/package.json", "{}")
        self.write("admin/src/lib/api.js", "import axios from 'axios';\nexport const client = axios.create({ baseURL: '/admin-api' });\n")
        self.write("admin/src/Users.jsx", "export function Users() { const { api } = useSession(); api.get('/users'); return null; }\n")
        self.scan()
        targets = {t for _, t, _ in self.calls()}
        self.assertTrue({"GET /v1/items", "GET /api/orders", "GET /admin-api/users"} <= targets, targets)
        self.assertFalse({"GET /v1/v1/items", "GET /v1/api/orders", "GET /v1/api/profile"} & targets)
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_browser_test_runner_navigation_is_not_an_api_call(self):
        self.write("e2e/login.spec.js", "it('logs in', () => { browser.get('/login'); api.get('/api/session'); });\n")
        self.write("src/tour.js", "export function start() { browser.get('/welcome'); driver.get('/start'); }\n")
        self.scan()
        self.assertEqual(self.calls(), set())
        self.assertEqual(self.issues("API_CALL_WITHOUT_HANDLER"), [])

    def test_client_receivers_do_not_turn_route_registrations_into_calls(self):
        self.write("repolens.toml", '[impact]\nclient_receivers = ["api"]\n')
        self.write("server/routes.js", "const express = require('express');\nconst api = express.Router();\n"
                                       "api.get('/orders', listOrders);\napi.post('/orders', create);\nmodule.exports = api;\n")
        from repolens.impact.config import Config
        self.graph = scan_repository(self.root, Config.load(self.root))
        self.assertEqual(self.calls(), set())

    def test_a_patch_to_a_get_only_catch_all_route_is_a_method_mismatch(self):
        self.write("package.json", "{}")
        self.write("app/api/docs/[...slug]/route.ts", "export async function GET() { return new Response(); }\n")
        self.write("app/page.tsx", "export default function P() { fetch('/api/docs/a/b', { method: 'PATCH' }); return null; }\n")
        self.scan()
        self.assertEqual([i.code for i in self.graph.issues if i.code in {"API_METHOD_MISMATCH", "API_CALL_WITHOUT_HANDLER"}],
                         ["API_METHOD_MISMATCH"])

    def test_escaped_string_text_is_decoded_not_dropped(self):
        source = ("export async function h(req) { await fetch('/api/it\\u0065ms');\n"
                  "  return pool.query('SELECT * FROM orders WHERE a = \\'' + req.query.a + '\\''); }\n")
        facts = javascript.parse_source(source, ".ts")
        self.assertEqual([r.url for r in facts.requests], ["/api/items"])
        self.assertEqual([text for _, text, *_ in facts.queries], ["SELECT * FROM orders WHERE a = '$1'"])
        [(_, _, holes)] = facts.sql_interpolations
        self.assertEqual(holes, [("req.query.a", R, True)])

    def test_allow_list_guards_beyond_a_named_ternary(self):
        safe = [
            "if (!['name', 'date'].includes(x)) throw new Error('bad sort'); return pool.query(`SELECT * FROM t ORDER BY ${x}`); }",
            "const SORT = { name: 'name', date: 'created_at' }; if (!Object.keys(SORT).includes(x)) { return null; }"
            " return pool.query(`SELECT * FROM t ORDER BY ${x}`); }",
            "return pool.query(`SELECT * FROM t ORDER BY ${['name', 'date'].includes(x) ? x : 'name'}`); }",
            "const OK = new Set(['a', 'b']); if (OK.has(x)) { return pool.query(`SELECT * FROM t ORDER BY ${x}`); } }",
        ]
        unsafe = [
            "if (!['name'].includes(x)) console.warn('bad'); return pool.query(`SELECT * FROM t ORDER BY ${x}`); }",
            "let y = req.query.x; if (!['a'].includes(y)) throw 1; y = req.query.z; return pool.query(`SELECT ${y}`); }",
            "if (['a'].includes(x)) { log(x); } return pool.query(`SELECT * FROM t ORDER BY ${x}`); }",
            "if (![n].includes(x)) throw 1; return pool.query(`SELECT * FROM t ORDER BY ${x}`); }",
        ]
        for body in safe:
            with self.subTest(body=body):
                self.assertEqual(origins(H + body), [""])
        for body in unsafe:
            with self.subTest(body=body):
                self.assertEqual(origins(H + body), [R])


if __name__ == "__main__":
    unittest.main()
