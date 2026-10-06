"""`repolens featuretrace propose`: drafted markers, JSDoc and owners, and the rules `--apply` keeps."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from repolens.config import load_config
from repolens.core import javascript
from repolens.featuretrace import propose
from repolens.featuretrace.audit import AuditContext, markers_for
from repolens.featuretrace.settings import from_config as ft_settings
from tests.stack_app import write_example_app

STACK = importlib.util.find_spec("sqlglot") and javascript.available()
GIT = shutil.which("git")
OUT = ".repolens/featuretrace-proposal"


@unittest.skipUnless(STACK, "install repolens[stack]")
class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write_example_app(self.root)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_propose(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = propose.main(list(args), config=load_config(self.root))
        return code, stdout.getvalue()

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               *args], check=True, capture_output=True, text=True)

    def commit(self) -> None:
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "app")

    def draft(self, *args: str) -> tuple[int, str]:
        return self.run_propose(*args)

    def apply(self, *args: str) -> tuple[int, str]:
        return self.run_propose("--apply", *args)

    def proposal(self) -> dict:
        return json.loads((self.root / OUT / "proposal.json").read_text(encoding="utf-8"))

    def patch(self) -> str:
        return (self.root / OUT / "proposal.patch").read_text(encoding="utf-8")

    def text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    @unittest.skipUnless(GIT, "git is not installed")
    def test_the_patch_applies_and_drafts_code_files_but_never_test_code(self):
        self.write("app/orders/page.test.tsx", 'test("loads", () => fetch("/api/orders"));\n')
        before = self.text("app/orders/page.tsx")
        code, _ = self.run_propose()
        self.assertEqual(code, 0)
        patch = self.patch()
        for path in ("app/orders/page.tsx", "app/api/orders/route.ts", "lib/orders.ts"):
            self.assertIn(f"+++ b/{path}\n", patch)
        self.assertNotIn("page.test.tsx", patch)
        self.assertEqual(self.text("app/orders/page.tsx"), before, "nothing is written without --apply")
        result = subprocess.run(["git", "apply", "--check", str(self.root / OUT / "proposal.patch")],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(GIT, "git is not installed")
    def test_applied_markers_pass_the_audit_and_render_maps(self):
        self.commit()
        self.assertEqual(self.draft()[0], 0)
        self.assertEqual({f["path"]: f["audit_issues"] for f in self.proposal()["files"] if f["audit_issues"]}, {})
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.text("app/orders/page.tsx").splitlines()[0], '"use client";')
        self.assertIn("// @featuretrace:orders — API GET, POST /api/orders (draft)", self.text("app/api/orders/route.ts"))
        ctx = AuditContext(ft_settings(load_config(self.root)))
        markers = [m for path in ctx.iter_files() for m in markers_for(ctx, path, path.read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(markers), 6)
        self.assertEqual({str(m.path): m.issues for m in markers if m.issues}, {})
        from repolens.featuretrace import maps
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(maps.main(["orders", "--out", str(self.root / "maps")], config=load_config(self.root)), 0)
        self.assertTrue((self.root / "maps/orders_mindmap.md").is_file())
        flow = (self.root / "maps/orders_flow.md").read_text(encoding="utf-8")
        page, route = "n_app_orders_page_tsx", "n_app_api_orders_route_ts"
        self.assertRegex(flow, rf"{page} -\.- {route}|{route} -\.- {page}")
        # `Related: app/api/orders/route.ts` names that file, not another directory's route.ts.
        dynamic = "n_app_api_orders__orderId__route_ts"
        self.assertNotRegex(flow, rf"{page} -\.- {dynamic}|{dynamic} -\.- {page}")

    @unittest.skipUnless(GIT, "git is not installed")
    def test_a_file_with_uncommitted_changes_is_skipped_without_allow_dirty(self):
        self.commit()
        self.write("lib/orders.ts", self.text("lib/orders.ts") + "\nexport const pageSize = 50;\n")
        self.draft()
        code, output = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("skipped  lib/orders.ts: has uncommitted changes", output)
        self.assertNotIn("@featuretrace", self.text("lib/orders.ts"))
        self.assertIn("@featuretrace:orders", self.text("app/orders/page.tsx"))

    def test_outside_git_apply_needs_allow_dirty(self):
        self.assertEqual(self.draft()[0], 0)
        code, output = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("not a git checkout", output)
        self.assertNotIn("@featuretrace", self.text("app/orders/page.tsx"))

    def test_a_file_that_already_carries_a_marker_is_not_drafted(self):
        self.write("lib/orders.ts", "// @featuretrace:billing — order store\n" + self.text("lib/orders.ts"))
        self.run_propose()
        patch = self.patch()
        self.assertNotIn("b/lib/orders.ts", patch)
        self.assertIn("app/api/orders/route.ts", patch)

    def test_crlf_files_keep_crlf(self):
        path = self.root / "app/api/reviews/route.ts"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.draft()
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 0, output)
        data = path.read_bytes()
        self.assertIn(b"// @featuretrace:reviews", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_a_python_marker_goes_after_the_shebang_and_encoding_cookie(self):
        self.write("server/invoices.py", "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
                                         "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
                                         '@app.get("/api/invoices")\ndef list_invoices():\n    return []\n')
        self.draft()
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 0, output)
        lines = self.text("server/invoices.py").splitlines()
        self.assertEqual(lines[:2], ["#!/usr/bin/env python3", "# -*- coding: utf-8 -*-"])
        self.assertTrue(lines[2].startswith("# @featuretrace:invoices — API GET /api/invoices (draft)"), lines[2])

    def test_tag_names_a_group(self):
        self.run_propose("--tag", "orders=order-management")
        patch = self.patch()
        self.assertIn("@featuretrace:order-management — page /orders (draft)", patch)
        self.assertNotIn("@featuretrace:orders ", patch)

    def test_a_path_resolving_outside_the_root_is_never_written(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "shared.ts"
        target.write_text("export const x = 1;\n", encoding="utf-8")
        link = self.root / "lib/shared.ts"
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest("symlinks are not available")
        draft = propose.Draft("lib/shared.ts", "orders", "orders", "service", [],
                              propose._sha(target.read_bytes()), target.read_bytes(), b"// changed\n")
        applied, skipped = propose.apply_drafts(self.root, [draft], allow_dirty=True)
        self.assertEqual(applied, [])
        self.assertEqual(skipped, [("lib/shared.ts", "resolves outside the repository root")])
        self.assertEqual(target.read_text(encoding="utf-8"), "export const x = 1;\n")

    def test_a_file_changed_after_drafting_is_not_overwritten(self):
        path = self.root / "lib/orders.ts"
        draft = propose.Draft("lib/orders.ts", "orders", "orders", "service", [], propose._sha(b"old"), b"old", b"new")
        applied, skipped = propose.apply_drafts(self.root, [draft], allow_dirty=True)
        self.assertEqual((applied, skipped), ([], [("lib/orders.ts", "changed since the proposal was drafted")]))
        self.assertNotEqual(path.read_bytes(), b"new")

    def test_an_incomplete_analysis_refuses_apply(self):
        self.write("scripts/broken.py", "def (:\n")
        code, output = self.draft()
        self.assertEqual(code, 2, "an incomplete analysis exits 2 even without --apply")
        self.assertTrue((self.root / OUT / "proposal.patch").is_file(), "the proposal is still written")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 2)
        self.assertIn("Refusing --apply", output)
        self.assertNotIn("@featuretrace", self.text("app/orders/page.tsx"))

    def test_jsdoc_drafts_facts_that_docs_coverage_still_counts_as_undocumented(self):
        from repolens.docs.coverage import javascript_file
        from repolens.docs.settings import from_config as docs_settings
        self.draft("--jsdoc")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 0, output)
        text = self.text("app/api/orders/route.ts")
        block = ("/**\n * TODO(repolens): describe what this does and who may call it.\n *\n"
                 " * Handles `GET /api/orders`.\n * Reads table `orders`.\n * @see app/orders/page.tsx\n"
                 " * @remarks Generated by repolens from static evidence; review before relying on it.\n */\n"
                 "export async function GET(request: Request) {")
        self.assertIn(block, text)
        result = javascript_file("app/api/orders/route.ts", text, docs_settings(load_config(self.root)))
        self.assertEqual({s.name: s.documented for s in result.symbols}, {"GET": False, "POST": False})

    def test_function_lens_ids_are_reviewed_comments_and_survive_a_symbol_rename(self):
        self.write("repolens.toml", '[lens]\nfrontend_roots = ["app", "lib"]\n'
                                    'javascript_parser = "tree-sitter"\n')
        code, output = self.draft("--jsdoc", "--function-lens")
        self.assertEqual(code, 0, output)
        payload = self.proposal()
        orders = next(item for item in payload["files"] if item["path"] == "lib/orders.ts")
        annotated = {item["symbol"]: item["id"] for item in orders["function_lens"]}
        self.assertEqual(set(annotated), {"listOrders", "getOrder", "createOrder"})
        self.assertIn(f"// @functionlens:{annotated['listOrders']}", self.patch())
        self.assertIn(f"+// @functionlens:{annotated['listOrders']}\n+/**", self.patch())
        self.assertIn("+ */\n export async function listOrders", self.patch())
        self.assertNotIn("@functionlens", self.text("lib/orders.ts"), "drafting remains read-only")

        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 0, output)
        path = self.root / "lib/orders.ts"
        path.write_text(path.read_text(encoding="utf-8").replace("function listOrders", "function loadOrders"),
                        encoding="utf-8")
        from repolens.lens.build import build, lookup
        from repolens.lens.settings import from_config as lens_settings
        data = build(lens_settings(load_config(self.root)))
        [renamed] = lookup(data, annotated["listOrders"])
        self.assertEqual((renamed["name"], renamed["source_id"]), ("loadOrders", annotated["listOrders"]))

    def test_a_tampered_function_lens_entry_cannot_insert_code(self):
        self.draft("--function-lens")
        payload = self.proposal()
        item = next(item for item in payload["files"] if item["function_lens"])
        item["function_lens"][0]["lines"] = ["process.exit(1);"]
        (self.root / OUT / "proposal.json").write_text(json.dumps(payload), encoding="utf-8")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 2)
        self.assertIn("Function Lens id line is not a comment", output)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_the_owners_draft_validates_and_stays_out_of_the_repository(self):
        import yaml
        from repolens.owners.registry import OwnerRegistry
        from repolens.owners.settings import from_config as owner_settings
        code, _ = self.run_propose("--owners")
        self.assertEqual(code, 0)
        text = (self.root / OUT / "canonical_owners.draft.yaml").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# DRAFT"))
        data = yaml.safe_load(text)
        self.assertEqual(OwnerRegistry(owner_settings(load_config(self.root))).validate_schema(data), [])
        [orders] = [c for c in data["concepts"] if c["concept"] == "table orders"]
        self.assertEqual(orders["owner"], "lib/orders.ts")
        self.assertEqual(orders["symbols"], ["createOrder", "getOrder", "listOrders"])
        self.assertFalse(list(self.root.glob("canonical_owners*")))

    def test_a_maintainer_note_is_not_a_generated_header_but_generated_is(self):
        self.write("lib/orders.ts", "// do not edit this list without asking\n" + self.text("lib/orders.ts"))
        self.write("app/api/reviews/route.ts", "// @generated\n" + self.text("app/api/reviews/route.ts"))
        self.run_propose()
        payload = json.loads((self.root / OUT / "proposal.json").read_text(encoding="utf-8"))
        self.assertIn("lib/orders.ts", [f["path"] for f in payload["files"]])
        skipped = {item["path"]: item["reason"] for item in payload["skipped"]}
        self.assertTrue(skipped.get("app/api/reviews/route.ts", "").startswith("generated"), skipped)


    @unittest.skipUnless(GIT, "git is not installed")
    def test_apply_writes_the_reviewed_proposal_not_a_new_draft(self):
        self.commit()
        self.assertEqual(self.draft("--tag", "orders=order-management")[0], 0)
        # Code committed after the review would change a new draft; --apply must not pick it up.
        self.write("app/api/invoices/route.ts", "export async function GET() { return Response.json([]); }\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "invoices")
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertIn("@featuretrace:order-management", self.text("app/api/orders/route.ts"))
        self.assertNotIn("@featuretrace", self.text("app/api/invoices/route.ts"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.apply("--tag", "orders=other")
        self.assertEqual(raised.exception.code, 2)

    def test_apply_refuses_without_a_saved_proposal_or_one_for_another_root(self):
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 2)
        self.assertIn("no proposal drafted", output)
        self.draft()
        payload = self.proposal()
        payload["root"] = "/somewhere/else"
        (self.root / OUT / "proposal.json").write_text(json.dumps(payload), encoding="utf-8")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 2)
        self.assertIn("drafted for '/somewhere/else'", output)
        self.assertNotIn("@featuretrace", self.text("app/orders/page.tsx"))

    def test_a_file_whose_patch_was_edited_is_not_written_from_the_json(self):
        self.draft()
        patch = self.patch()
        start = patch.index("diff --git a/lib/orders.ts")
        end = patch.find("diff --git ", start + 1)
        end = len(patch) if end < 0 else end
        edited = patch[:start] + patch[start:end].replace("(draft)", "(reviewed)") + patch[end:]
        (self.root / OUT / "proposal.patch").write_text(edited, encoding="utf-8")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 1, output)
        self.assertIn("skipped  lib/orders.ts: proposal.patch was edited", output)
        self.assertNotIn("@featuretrace", self.text("lib/orders.ts"))
        self.assertIn("@featuretrace:orders", self.text("app/api/orders/route.ts"))

    def test_a_tampered_proposal_cannot_insert_code(self):
        self.draft()
        payload = self.proposal()
        payload["files"][0]["marker"]["lines"][1] = "process.exit(1);"
        (self.root / OUT / "proposal.json").write_text(json.dumps(payload), encoding="utf-8")
        code, output = self.apply("--allow-dirty")
        self.assertEqual(code, 2)
        self.assertIn("not a comment", output)

    def test_files_in_the_output_directory_it_did_not_write_are_never_replaced(self):
        self.write(f"{OUT}/README.md", "# Notes a person wrote\n")
        code, output = self.draft()
        self.assertEqual(code, 2)
        self.assertIn("not written by repolens featuretrace propose", output)
        self.assertEqual(self.text(f"{OUT}/README.md"), "# Notes a person wrote\n")
        (self.root / OUT / "README.md").unlink()
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text("{}", encoding="utf-8")
        try:
            os.symlink(elsewhere, self.root / OUT / "proposal.json")
        except OSError:
            self.skipTest("symlinks are not available")
        code, output = self.draft()
        self.assertEqual(code, 2)
        self.assertIn("symlink", output)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "{}")
        (self.root / OUT / "proposal.json").unlink()
        self.assertEqual(self.draft()[0], 0)
        self.assertEqual(self.draft()[0], 0, "a second run replaces its own files")

    def test_only_files_the_featuretrace_settings_scan_are_drafted(self):
        self.write("repolens.toml", '[featuretrace]\nscan_dirs = ["lib"]\n')
        self.assertEqual(self.draft()[0], 0)
        payload = self.proposal()
        self.assertEqual([f["path"] for f in payload["files"]], ["lib/orders.ts"])
        skipped = {item["path"]: item["reason"] for item in payload["skipped"]}
        self.assertIn("scan_dirs", skipped["app/api/orders/route.ts"])
        related = [line for line in payload["files"][0]["marker"]["lines"] if "Related:" in line]
        self.assertEqual(related, ["// Related: db/schema.sql"], "files outside scan_dirs carry no marker, so none is related")

    def test_an_unknown_group_name_exits_2_and_lists_the_groups(self):
        code, output = self.draft("--only", "invoices")
        self.assertEqual(code, 2)
        self.assertIn("no feature group named invoices; the groups are: orders, products, reviews", output)

    def test_the_audit_issues_a_draft_would_have_are_recorded(self):
        self.write("app/api/reviews/route.ts", 'import { pool } from "../../../lib/db.js";\n' + self.text("app/api/reviews/route.ts"))
        self.draft()
        issues = {f["path"]: f["audit_issues"] for f in self.proposal()["files"]}
        self.assertIn("references a path that does not exist: ../../../lib/db.js", issues["app/api/reviews/route.ts"])
        self.assertEqual(issues["lib/orders.ts"], [])

    @unittest.skipUnless(GIT, "git is not installed")
    def test_form_feeds_and_line_separators_keep_the_patch_applicable(self):
        self.write("lib/orders.ts", "\x0c\n// section\u2028break\n" + self.text("lib/orders.ts"))
        self.draft()
        self.assertIn("+++ b/lib/orders.ts", self.patch())
        result = subprocess.run(["git", "apply", "--check", str(self.root / OUT / "proposal.patch")],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PlacementTests(unittest.TestCase):
    def test_an_encoding_declaration_on_line_two_below_a_comment_stays_first(self):
        self.assertEqual(propose._marker_index(["# a comment", "# -*- coding: utf-8 -*-", "import os"], True), (2, 2))
        self.assertEqual(propose._marker_index(["import os", "# -*- coding: utf-8 -*-"], True), (0, 0))

    def test_a_file_declaring_an_encoding_that_cannot_hold_the_marker_is_left_alone(self):
        reason = propose._unusable(Path("."), "a.py", b"# comment\n# -*- coding: ascii -*-\nx = 1\n", 10_000)
        self.assertIn("ascii encoding", reason)
        self.assertIsNone(propose._unusable(Path("."), "a.py", b"# -*- coding: utf8 -*-\nx = 1\n", 10_000))

    def test_comments_before_a_directive_leave_the_top_of_the_file_valid(self):
        lines = ["// licence"] * 12 + ['"use client";', "import x from 'y';"]
        self.assertEqual(propose._marker_index(lines, False), (13, 0))
        self.assertEqual(propose._marker_index(["#!/usr/bin/env node", "'use strict';", "run();"], False), (2, 1))

    def test_inserted_lines_take_the_majority_line_ending(self):
        self.assertTrue(propose._insert("a\r\nb\r\nc\n", [(0, 0, ["// m"])]).startswith("// m\r\na\r\n"))
        self.assertTrue(propose._insert("a\nb\nc\r\n", [(0, 0, ["// m"])]).startswith("// m\na\n"))

    def test_lines_split_on_newlines_only(self):
        self.assertEqual(propose.split_lines("a\x0cb\u2028c\r\nd"), ["a\x0cb\u2028c\r", "d"])
        self.assertEqual(propose.split_lines("a\nb", keepends=True), ["a\n", "b"])

    @unittest.skipUnless(GIT, "git is not installed")
    def test_a_bracketed_path_is_not_a_git_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app/i").mkdir(parents=True)
            (root / "app/i/page.tsx").write_text("a\n", encoding="utf-8")
            run = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run([*run, "init", "-q"], check=True)
            subprocess.run([*run, "add", "-A"], check=True)
            subprocess.run([*run, "commit", "-qm", "x"], check=True)
            (root / "app/[id]").mkdir()
            (root / "app/[id]/page.tsx").write_text("b\n", encoding="utf-8")
            self.assertTrue(propose._clean_in_git(root, "app/[id]/page.tsx").startswith("not committed"))
            self.assertIsNone(propose._clean_in_git(root, "app/i/page.tsx"))


class PlaceholderSettingTests(unittest.TestCase):
    def test_a_repository_list_never_drops_the_repolens_placeholder(self):
        from repolens.docs.settings import from_config as docs_settings
        for listed in ("[]", '["Generated inventory header"]'):
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "repolens.toml").write_text(f"[docs]\nplaceholder_patterns = {listed}\n", encoding="utf-8")
                settings = docs_settings(load_config(Path(tmp)))
                self.assertTrue(settings.is_placeholder(propose.PLACEHOLDER), listed)
        template = Path(propose.__file__).parents[1] / "templates/repolens.toml"
        self.assertIn("is always included", template.read_text(encoding="utf-8"))


class RelatedReferenceTests(unittest.TestCase):
    def test_reference_paths_keep_next_route_segments(self):
        from repolens.featuretrace.audit import REF_PATH_RE
        text = "Related: app/api/orders/[orderId]/route.ts, app/(shop)/@modal/(.)photo/page.tsx; see call(lib/x.ts)"
        self.assertEqual(REF_PATH_RE.findall(text),
                         ["app/api/orders/[orderId]/route.ts", "app/(shop)/@modal/(.)photo/page.tsx", "lib/x.ts"])

    def test_a_related_entry_binds_its_whole_path_before_a_basename(self):
        from repolens.featuretrace.render import _related_target
        nodes = {"app/api/orders/route.ts": "a", "app/api/orders/[orderId]/route.ts": "b", "lib/x.ts": "c", "lib/y/x.ts": "d"}
        self.assertEqual(_related_target("app/api/orders/route.ts", nodes), "a")
        self.assertEqual(_related_target("app/api/orders/[orderId]/route.ts (the item)", nodes), "b")
        self.assertEqual(_related_target("y/x.ts", nodes), "d")
        self.assertIsNone(_related_target("route.ts", nodes), "an ambiguous basename binds nothing")
        self.assertIsNone(_related_target("x.ts", nodes))

    def test_none_found_is_not_a_related_file(self):
        from repolens.featuretrace.model import parse_marker_block
        with tempfile.TemporaryDirectory() as tmp:
            settings = ft_settings(load_config(Path(tmp)))
            lines = ["// @featuretrace:orders — API GET /orders (draft)", "// Layer: router",
                     "// Data flow: no static caller found → GET /orders → orders (draft).", "// Related: none found (draft)"]
            node = parse_marker_block(settings, Path(tmp) / "a.ts", lines, 0, "orders")
        self.assertEqual(node.related_raw, [])


class EvidenceRuleTests(unittest.TestCase):
    def graph(self):
        from repolens.impact.model import Graph
        return Graph("/repo")

    def test_a_marker_names_no_ambiguous_regex_or_unverified_store(self):
        from repolens.impact.model import Edge, Node
        graph = self.graph()
        graph.add_node(Node("s", "symbol", "load", path="lib/a.ts", line=1))
        for name, meta in (("parsed", {}), ("regex", {}), ("guess", {}), ("unverified", {"unverified": True})):
            graph.add_node(Node(name, "postgres_table", name, metadata=meta))
        graph.add_edge(Edge("s", "parsed", "TOUCHES_STORE", "probable", "lib/a.ts:2", origin="sqlglot", detail="SQL (reads)"))
        graph.add_edge(Edge("s", "regex", "TOUCHES_STORE", "probable", "lib/a.ts:3", origin="regex", detail="unverified"))
        graph.add_edge(Edge("s", "guess", "TOUCHES_STORE", "ambiguous", "lib/a.ts:4", origin="declared"))
        graph.add_edge(Edge("s", "unverified", "TOUCHES_STORE", "exact", "lib/a.ts:5", origin="sqlglot"))
        stores = propose._Evidence(graph).stores("lib/a.ts")
        self.assertEqual([(store.label, operation) for store, operation, _ in stores], [("parsed", "reads")])

    def test_a_route_file_lists_a_few_routes_and_not_an_artifacts_second_spelling(self):
        from repolens.impact.model import Edge, Node
        graph = self.graph()
        graph.add_node(Node("h", "symbol", "handler", path="api.py", line=1))
        graph.add_node(Node("f", "file", "api.py", path="api.py"))
        routes = {"scanned": "/orders", "declared": "/api/orders", "a": "/a", "b": "/b", "c": "/c", "d": "/d"}
        for node_id, route in routes.items():
            graph.add_node(Node(node_id, "endpoint", f"GET {route}", metadata={"method": "GET", "route": route}))
            if node_id == "declared":
                graph.add_edge(Edge(node_id, "f", "IMPLEMENTED_BY", "declared", "map.json", origin="generated_static_artifact"))
            else:
                graph.add_edge(Edge(node_id, "h", "HANDLES_API", "exact", "api.py:1"))
        ev = propose._Evidence(graph)
        served = ev.served_here("api.py")
        self.assertNotIn("declared", served)
        self.assertEqual(propose._api_role(ev, served), "GET /a; GET /b; GET /c and 2 more")

    def test_the_owners_draft_prefers_application_code_bounds_consumers_and_keeps_unicode(self):
        from repolens.impact.model import Edge, Node
        graph = self.graph()
        graph.add_node(Node("t", "postgres_table", "orders"))
        files = {"scripts/fix.py": 3, "app/store.py": 2, "lib/store.mjs": 5, **{f"app/c{i:02}.py": 1 for i in range(12)}}
        for path, count in files.items():
            for n in range(count):
                graph.add_node(Node(f"{path}:{n}", "symbol", f"f{n}", path=path, line=n + 1))
                graph.add_edge(Edge(f"{path}:{n}", "t", "TOUCHES_STORE", "exact", f"{path}:{n + 1}", origin="python_ast"))
        text = propose.owners_draft(graph, {})
        self.assertIn('owner: "app/store.py"', text, "scripts/ is ranked last; .mjs is not a file `repolens owners` reads")
        self.assertEqual(text.count("      - path: "), propose.MAX_CONSUMERS)
        self.assertIn("# and 3 more consumers not listed", text)
        self.assertEqual(propose._yaml_string("app/\U0001f600.py"), '"app/\U0001f600.py"')
        if importlib.util.find_spec("yaml"):
            import yaml
            self.assertEqual(yaml.safe_load(text)["concepts"][0]["owner"], "app/store.py")
            for value in ("app/\U0001f600.py", "a\u2028b", 'quote " and: colon'):
                self.assertEqual(yaml.safe_load(f"k: {propose._yaml_string(value)}")["k"], value)


class GeneratedHeaderTests(unittest.TestCase):
    def test_only_standard_generated_headers_count(self):
        def generated(line):
            return propose._unusable(Path("."), "a.ts", f"{line}\nexport const a = 1;\n".encode(), 10_000) is not None

        for line in ("// @generated", "/* @generated */", "// @generated SignedSource<<0a1b>>", "// @generated by openapi-typescript", "// Code generated by protoc-gen-go. DO NOT EDIT.",
                     "# This file is auto-generated by the build", "/* This file was automatically generated */",
                     "// <auto-generated />", "// DO NOT EDIT", "# DO NOT EDIT!"):
            self.assertTrue(generated(line), line)
        for line in ("// do not edit this list without asking", " * @generated FunctionHeader",
                     "const banner = '@generated by hand for docs';", "# Edit with care: generated values below are checked",
                     "// TODO: do not edit by hand once the generator exists"):
            self.assertFalse(generated(line), line)


#: A FastAPI service: one route area reaches `orders` (PostgreSQL), `order_items` and `reviews`
#: (MongoDB) through import-bound calls, and calls `purge` by name only; a shared model file
#: declares two tables of that area and one of `accounts`.
PY_SERVICE = {
    "app/__init__.py": "",
    "app/main.py": ("from fastapi import FastAPI\nfrom app.routers import accounts, admin, orders\n\n"
                    "app = FastAPI()\napp.include_router(orders.router)\napp.include_router(admin.router)\n"
                    "app.include_router(accounts.router)\n"),
    "app/models.py": ("from sqlalchemy import Column, Integer\nfrom sqlalchemy.orm import declarative_base\n\n"
                      "Base = declarative_base()\n\n\nclass Order(Base):\n    __tablename__ = \"orders\"\n"
                      "    id = Column(Integer, primary_key=True)\n\n\nclass OrderItem(Base):\n"
                      "    __tablename__ = \"order_items\"\n    id = Column(Integer, primary_key=True)\n\n\n"
                      "class Account(Base):\n    __tablename__ = \"accounts\"\n    id = Column(Integer, primary_key=True)\n"),
    "app/services/__init__.py": "",
    "app/services/orders.py": ("from sqlalchemy import text\n\n\ndef list_orders(session):\n"
                               "    return session.execute(text(\"SELECT id FROM orders LIMIT 10\")).all()\n"),
    "app/services/items.py": ("from sqlalchemy import text\n\n\ndef list_items(session, order_id):\n"
                              "    return session.execute(text(\"SELECT id FROM order_items WHERE id = :id\"), {\"id\": order_id}).all()\n"),
    "app/services/reviews.py": "def recent_reviews(db):\n    return list(db.reviews.find({}).limit(5))\n",
    "app/services/cleanup.py": "def purge(db):\n    return db.audit_log.delete_many({})\n",
    "app/services/accounts.py": ("from sqlalchemy import text\n\n\ndef get_account(session, account_id):\n"
                                 "    return session.execute(text(\"SELECT id FROM accounts WHERE id = :id\"), {\"id\": account_id}).first()\n"),
    "app/routers/__init__.py": "",
    "app/routers/orders.py": ("from fastapi import APIRouter\nfrom app.services.items import list_items\n"
                              "from app.services.orders import list_orders\nfrom app.services.reviews import recent_reviews\n\n"
                              "router = APIRouter()\n\n\n@router.get(\"/api/orders\")\ndef get_orders(session=None, db=None):\n"
                              "    recent_reviews(db)\n    purge(db)\n    return list_orders(session)\n\n\n"
                              "@router.get(\"/api/orders/{order_id}/items\")\ndef get_items(order_id: int, session=None):\n"
                              "    return list_items(session, order_id)\n"),
    "app/routers/admin.py": ("from fastapi import APIRouter\nfrom app.services.cleanup import purge\n\n"
                             "router = APIRouter()\n\n\n@router.post(\"/api/admin/purge\")\ndef run_purge(db=None):\n"
                             "    return purge(db)\n"),
    "app/routers/accounts.py": ("from fastapi import APIRouter\nfrom app.services.accounts import get_account\n\n"
                                "router = APIRouter()\n\n\n@router.get(\"/api/accounts/{account_id}\")\n"
                                "def read_account(account_id: int, session=None):\n    return get_account(session, account_id)\n"),
}


@unittest.skipUnless(STACK, "install repolens[stack]")
class FileEvidenceTests(unittest.TestCase):
    """Each marker names the file's own stores and directly linked files, not its whole route area's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        for path, text in PY_SERVICE.items():
            target = cls.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = propose.main([], config=load_config(cls.root))
        assert code == 0, code
        payload = json.loads((cls.root / OUT / "proposal.json").read_text(encoding="utf-8"))
        cls.files = {item["path"]: item for item in payload["files"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def marker(self, path: str) -> list[str]:
        return [line.lstrip("# ") for line in self.files[path]["marker"]["lines"]]

    def field(self, path: str, name: str) -> list[str]:
        """A field's value lines, continuation lines included."""
        lines = self.marker(path)
        found: list[str] = []
        for line in lines:
            if found and not any(line.startswith(f"{label}:") for label in ("Table", "Collection", "Layer", "Data flow")):
                found.append(line.strip())
            elif line.startswith(f"{name}:"):
                found = [line.split(":", 1)[1].strip()]
            elif found:
                break
        return found

    def test_a_service_names_only_the_stores_its_own_code_references(self):
        self.assertEqual(self.field("app/services/orders.py", "Table"), ["orders"])
        self.assertEqual(self.field("app/services/orders.py", "Collection"), [])
        # The route file its handlers reach does name the whole reach.
        self.assertEqual(self.field("app/routers/orders.py", "Table"), ["order_items, orders"])
        self.assertEqual(self.field("app/routers/orders.py", "Collection"), ["reviews"])

    def test_a_model_names_exactly_the_tables_it_declares(self):
        self.assertEqual(self.field("app/models.py", "Table"), ["accounts, order_items, orders"])
        self.assertEqual(self.field("app/models.py", "Collection"), [])

    def test_a_file_reached_only_by_a_name_only_call_is_not_related(self):
        self.assertIn("app/services/cleanup.py", self.files, "the file is drafted, so only the evidence rule leaves it out")
        related = self.field("app/routers/orders.py", "Related")
        self.assertNotIn("app/services/cleanup.py", related)
        self.assertIn("app/services/orders.py", related)
        self.assertEqual(self.field("app/routers/orders.py", "Collection"), ["reviews"], "audit_log is not reached")

    def test_a_shared_model_takes_the_group_with_the_most_evidence(self):
        model = self.files["app/models.py"]
        self.assertEqual((model["tag"], model["other_groups"]), ("orders", ["accounts"]))

    def test_a_route_nobody_calls_says_so(self):
        self.assertTrue(self.field("app/routers/orders.py", "Data flow")[0].startswith("no static caller found → "))

    def test_related_lists_direct_links_only(self):
        self.assertEqual(sorted(self.field("app/services/orders.py", "Related")), ["app/models.py", "app/routers/orders.py"])
        self.assertEqual(sorted(self.field("app/models.py", "Related")),
                         ["app/services/accounts.py", "app/services/items.py", "app/services/orders.py"])


SERVER_ACTIONS = {
    "package.json": '{"name": "sa", "private": true, "dependencies": {"next": "15.0.0", "react": "19.0.0", "pg": "8.13.0"}}\n',
    "lib/db.ts": 'import { Pool } from "pg";\nexport const pool = new Pool();\n',
    "app/actions/orders.ts": ('// Server actions for orders.\n"use server";\nimport { pool } from "../../lib/db";\n\n'
                              'export async function saveOrder(form: FormData) {\n'
                              '  await pool.query("insert into orders (note) values ($1)", [form.get("note")]);\n}\n'),
    # The query runs in a same-module helper, which the exported function calls by bare name.
    "server/orders.ts": ('import { pool } from "../lib/db";\n\nexport async function listOrders() {\n'
                         '  return load();\n}\n\nasync function load() {\n'
                         '  const { rows } = await pool.query("select id, note from order_lines");\n  return rows;\n}\n'),
    # Never imported by the page: `onDone={purge}` there is a name, not evidence.
    "lib/cleanup.ts": 'import { pool } from "./db";\nexport async function purge() {\n  await pool.query("delete from audit_log");\n}\n',
    "app/orders/history/page.tsx": ('import { pool } from "../../../lib/db";\n\nexport default async function History() {\n'
                                    '  const { rows } = await pool.query("select id from order_history");\n'
                                    '  return <ul>{rows.length}</ul>;\n}\n'),
    "app/orders/page.tsx": ('import { saveOrder } from "../actions/orders";\nimport { listOrders } from "../../server/orders";\n\n'
                            'export default async function OrdersPage() {\n  const rows = await listOrders();\n'
                            '  return (\n    <form action={saveOrder}>\n      <List items={rows} onDone={purge} />\n    </form>\n  );\n}\n'),
}


@unittest.skipUnless(STACK, "install repolens[stack]")
class ServerActionTests(FileEvidenceTests):
    """Next.js pages that reach their data with no HTTP route: a `"use server"` action handed to
    `<form action>` and a server module a server component imports and calls."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        for path, text in SERVER_ACTIONS.items():
            target = cls.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            propose.main([], config=load_config(cls.root))
        payload = json.loads((cls.root / OUT / "proposal.json").read_text(encoding="utf-8"))
        cls.files = {item["path"]: item for item in payload["files"]}

    def marker(self, path: str) -> list[str]:
        return [line.removeprefix("//").lstrip() if line.startswith("// ") else line.removeprefix("//")
                for line in self.files[path]["marker"]["lines"]]

    def test_a_page_flows_through_its_actions_and_server_functions_to_their_tables(self):
        self.assertEqual(self.field("app/orders/page.tsx", "Data flow")[0],
                         "page /orders → action saveOrder, server listOrders → order_lines, orders (draft).")

    def test_a_server_component_names_its_own_queries(self):
        self.assertEqual(self.field("app/orders/history/page.tsx", "Data flow")[0],
                         "page /orders/history → queries in this file → order_history (draft).")

    def test_related_lists_the_action_and_server_module_but_not_a_name_only_match(self):
        self.assertEqual(sorted(self.field("app/orders/page.tsx", "Related")), ["app/actions/orders.ts", "server/orders.ts"])

    def test_an_action_module_names_the_page_that_uses_it(self):
        self.assertTrue(self.field("app/actions/orders.ts", "Data flow")[0].startswith("page /orders → saveOrder → orders"))
        self.assertIn("app/orders/page.tsx", self.field("app/actions/orders.ts", "Related"))

    # The inherited Python-fixture tests do not apply to this fixture.
    test_a_service_names_only_the_stores_its_own_code_references = None
    test_a_model_names_exactly_the_tables_it_declares = None
    test_a_file_reached_only_by_a_name_only_call_is_not_related = None
    test_a_shared_model_takes_the_group_with_the_most_evidence = None
    test_a_route_nobody_calls_says_so = None
    test_related_lists_direct_links_only = None


if __name__ == "__main__":
    unittest.main()
