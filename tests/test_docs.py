from __future__ import annotations

import contextlib
import io
import tempfile
import textwrap
import unittest
from pathlib import Path

from repolens.config import load_config
from repolens.docs import build, coverage
from repolens.docs.settings import from_config


class Tree:
    def __init__(self, files: dict[str, str], docs: str = ""):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write({"repolens.toml": "[docs]\n" + textwrap.dedent(docs), **files})

    def write(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(text), encoding="utf-8")

    @property
    def config(self):
        return load_config(str(self.root))

    @property
    def settings(self):
        return from_config(self.config)

    def close(self) -> None:
        self.tmp.cleanup()


PYTHON = '''
    """Module doc."""
    from typing import overload

    def documented():
        """Yes."""

    def bare():
        pass

    def _private():
        pass

    class Thing:
        """A thing."""
        def __init__(self):
            pass
        def method(self):
            pass
        @property
        def value(self):
            """The value."""
        @value.setter
        def value(self, v):
            pass
        def outer(self):
            """Outer."""
            def inner():
                pass

    @overload
    def f(x: int) -> int: ...
    def f(x):
        """F."""
'''

JAVASCRIPT = """
    /** Documented. */
    export function documented() {}

    export function bare() {}

    /** Decorated class. */
    @decorator
    export class Decorated {}

    function Page() {}
    export default withAuth(Page);

    const a = 1;
    const b = 2;
    export { a };
    export { b } from "./b";

    /**/
    export const empty = 1;

    /* not jsdoc */
    export const plain = 2;

    function internal() {}
"""


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class PythonCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = Tree({})
        self.s = self.tree.settings

    def tearDown(self) -> None:
        self.tree.close()

    def test_public_symbols_are_counted_and_stubs_dunders_and_nesting_are_not(self):
        result = coverage.python_file("m.py", textwrap.dedent(PYTHON), self.s)
        self.assertEqual([s.name for s in result.symbols],
                         ["<module>", "documented", "bare", "Thing", "Thing.method",
                          "Thing.value", "Thing.outer", "f"])
        self.assertEqual([s.name for s in result.missing], ["bare", "Thing.method"])

    def test_an_empty_module_has_nothing_to_describe(self):
        self.assertEqual(coverage.python_file("__init__.py", "", self.s).symbols, [])

    def test_a_file_that_does_not_parse_is_unmeasured_and_keeps_its_baseline_count(self):
        result = coverage.python_file("bad.py", "def (:\n", self.s)
        self.assertTrue(result.error)
        self.assertEqual(coverage.missing_counts([result], {"bad.py": 4}), {"bad.py": 4})
        self.assertEqual(coverage.missing_counts([result], None), {})


class PlaceholderTests(unittest.TestCase):
    def test_generated_boilerplate_is_not_documentation(self):
        tree = Tree({}, docs='placeholder_patterns = ["Generated inventory header"]\n')
        try:
            result = coverage.python_file(
                "m.py", '"""Generated inventory header: 12 functions."""\nx = 1\n', tree.settings)
        finally:
            tree.close()
        self.assertEqual([s.name for s in result.missing], ["<module>"])


class JavaScriptCoverageTests(unittest.TestCase):
    def test_exported_declarations_and_names_exported_later_are_counted(self):
        tree = Tree({})
        try:
            result = coverage.javascript_file("a.ts", textwrap.dedent(JAVASCRIPT), tree.settings)
        finally:
            tree.close()
        documented = [s.name for s in result.symbols if s.documented]
        self.assertEqual(documented, ["documented", "Decorated"])
        # `b` is re-exported from another module and `internal` is never exported.
        self.assertEqual([s.name for s in result.missing], ["bare", "Page", "a", "empty", "plain"])


CLASSES = """
    /** A store. */
    export class Store {
      private cache = new Map<string, number>();
      constructor(private db: Db) {}

      /** Reads one. */
      async get(id: string): Promise<number> {
        if (id) {
          return lookup(id);
        }
      }
      put(id: string): void;
      put(id: string, value?: number): void {}
      get size() { return 1; }
      set size(value) {}
      private secret() {}
      #hidden() {}
      handle = async (event: Event): Promise<void> => {};
      label = (x + y);
    }

    class Local {
      run() {}
    }

    function helper() {}
    const arrow = async (a: string) => a;
    const value = 3;
    /** TODO(repolens): purpose. */
    export const drafted = () => 1;
"""


class JavaScriptMethodAndPrivateTests(unittest.TestCase):
    def measure(self, docs: str = "") -> list[tuple[str, bool]]:
        tree = Tree({}, docs=docs)
        try:
            result = coverage.javascript_file("a.ts", textwrap.dedent(CLASSES), tree.settings)
        finally:
            tree.close()
        return [(s.name, s.documented) for s in result.symbols]

    def test_methods_of_an_exported_class_count_and_private_members_do_not(self):
        # Not counted: the constructor, the `put` overload signature, the `set` accessor,
        # `private`/`#` members, fields, and everything file-local. Drafted JSDoc is missing.
        self.assertEqual(self.measure(), [("Store", True), ("Store.get", True), ("Store.put", False),
                                          ("Store.size", False), ("Store.handle", False), ("drafted", False)])

    def test_include_private_counts_file_local_functions_classes_and_private_methods(self):
        names = [name for name, _ in self.measure("include_private = true\n")]
        self.assertEqual(names, ["Store", "Store.get", "Store.put", "Store.size", "Store.secret", "Store.#hidden",
                                 "Store.handle", "Local", "Local.run", "helper", "arrow", "drafted"])

    def test_a_repository_without_docs_configuration_measures_its_typescript(self):
        tree = Tree({"src/a.ts": "/** A. */\nexport function a() {}\n", "public/vendor.min.js": "export function b() {}\n",
                     "node_modules/x/index.js": "export function c() {}\n"})
        (tree.root / "repolens.toml").write_text("", encoding="utf-8")
        try:
            results = coverage.measure(tree.settings)
        finally:
            tree.close()
        self.assertEqual([(r.path, len(r.symbols)) for r in results], [("src/a.ts", 1)])


class RatchetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = Tree({"pkg/a.py": '"""A."""\ndef one():\n    pass\n'})

    def tearDown(self) -> None:
        self.tree.close()

    def _run(self, *argv: str) -> int:
        return _quiet(coverage.main, list(argv), config=self.tree.config)

    def test_a_check_with_no_baseline_fails_rather_than_passing_by_construction(self):
        self.assertEqual(self._run("--check"), 1)

    def test_a_file_may_not_gain_an_undocumented_symbol(self):
        self.assertEqual(self._run("--update-baseline"), 0)
        self.assertEqual(self._run("--check"), 0)
        self.tree.write({"pkg/a.py": '"""A."""\ndef one():\n    pass\ndef two():\n    pass\n'})
        self.assertEqual(self._run("--check"), 1)

    def test_a_new_file_must_be_fully_documented(self):
        self._run("--update-baseline")
        self.tree.write({"pkg/b.py": '"""B."""\ndef three():\n    pass\n'})
        self.assertEqual(self._run("--check"), 1)

    def test_an_improvement_fails_until_the_baseline_records_it(self):
        self._run("--update-baseline")
        self.tree.write({"pkg/a.py": '"""A."""\ndef one():\n    """Now documented."""\n'})
        self.assertEqual(self._run("--check"), 1)
        self.assertEqual(self._run("--update-baseline"), 0)
        self.assertEqual(self._run("--check"), 0)


class BuildTests(unittest.TestCase):
    def test_only_a_directory_repolens_generated_is_ever_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            mine, theirs = Path(tmp) / "mine", Path(tmp) / "theirs"
            for target in (mine, theirs):
                target.mkdir()
                (target / "keep.html").write_text("x", encoding="utf-8")
            (mine / build.MARKER).write_text("", encoding="utf-8")
            build._clear(mine)
            build._clear(theirs)  # not ours: left exactly as it was
            self.assertTrue((theirs / "keep.html").is_file())
            self.assertFalse((mine / "keep.html").exists())

    def test_the_index_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            parts = [build.Part("python", "built", "3 pages", out / "python"),
                     build.Part("typescript", "skipped", "no npx")]
            first = build.write_index(out, parts, "demo").read_text(encoding="utf-8")
            second = build.write_index(out, parts, "demo").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertIn("no npx", first)

    def test_a_missing_generator_is_a_skip_and_strict_turns_a_skip_into_a_failure(self):
        # An interpreter that exists and cannot import pdoc: the same answer whether or not
        # the machine running the test has pdoc installed.
        tree = Tree({"pkg/__init__.py": '"""P."""\n', "nopdoc": "#!/bin/sh\nexit 1\n"},
                    docs='[docs.python]\nmodules = ["pkg"]\ninterpreter = "nopdoc"\n')
        (tree.root / "nopdoc").chmod(0o755)
        try:
            self.assertEqual(_quiet(build.main, ["--python", "--out", str(tree.root / "out")],
                                    config=tree.config), 0)
            self.assertEqual(_quiet(build.main, ["--python", "--strict", "--out", str(tree.root / "out")],
                                    config=tree.config), 1)
        finally:
            tree.close()

    def test_a_configured_interpreter_that_does_not_exist_is_named_not_papered_over(self):
        tree = Tree({}, docs='[docs.python]\nmodules = ["no_such_module"]\ninterpreter = "venv/bin/python3"\n')
        try:
            part = build.build_python(tree.settings, tree.root / "out")
        finally:
            tree.close()
        self.assertIn("configured interpreter venv/bin/python3 not found", part.detail)


# A stand-in for pdoc with the two entry points `docs build` uses: `extract.walk_specs` (the
# probe) and `python -m pdoc` (the render). It records the argv it was run with, so a test
# can see what was excluded, and runs whether or not the real pdoc is installed.
FAKE_PDOC = {
    "src/pdoc/__init__.py": "",
    "src/pdoc/extract.py": '''
        import importlib, pkgutil, re

        def load_module(name):
            try:
                return importlib.import_module(name)
            except Exception as exc:
                raise RuntimeError(f"Error importing {name}") from exc

        def walk_specs(specs):
            names = []
            for spec in specs:
                if spec.startswith("!"):
                    pattern = re.compile(spec[1:])
                    names = [n for n in names if not pattern.match(n)]
                    continue
                names.append(spec)
                module = importlib.import_module(spec)
                names += [m.name for m in pkgutil.iter_modules(getattr(module, "__path__", []), spec + ".")]
            return names
    ''',
    "src/pdoc/__main__.py": '''
        import json, pathlib, sys
        args = sys.argv[1:]
        out = pathlib.Path(args[args.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "index.html").write_text("pages", encoding="utf-8")
        pathlib.Path(__file__).with_name("argv.json").write_text(json.dumps(args), encoding="utf-8")
    ''',
    "src/pkg/__init__.py": '"""P."""\n',
    "src/pkg/ok.py": '"""Imports."""\nimport json\n',
}
OPTIONAL = {"src/pkg/api.py": '"""Needs an extra."""\nimport repolens_test_absent_extra\n'}


class OptionalDependencyBuildTests(unittest.TestCase):
    """One module needing an uninstalled extra must not stop the whole pdoc build."""

    def _build(self, files: dict[str, str], docs: str = "", *argv: str):
        tree = Tree({**FAKE_PDOC, **files},
                    docs='[docs.python]\nmodules = ["pkg"]\npath = ["src"]\n' + docs)
        self.addCleanup(tree.close)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build.main(["--python", "--out", str(tree.root / "out"), *argv], config=tree.config)
        recorded = tree.root / "src/pdoc/argv.json"
        pdoc_argv = __import__("json").loads(recorded.read_text(encoding="utf-8")) if recorded.exists() else None
        return code, out.getvalue(), pdoc_argv

    def test_a_module_whose_third_party_dependency_is_missing_is_excluded_and_named(self):
        code, output, pdoc_argv = self._build(OPTIONAL, "", "--strict")
        self.assertEqual(code, 0)
        self.assertIn("BUILT", output)
        self.assertIn("NOT DOCUMENTED, dependency not installed: pkg.api (needs repolens_test_absent_extra)",
                      output)
        self.assertEqual(pdoc_argv[-2:], ["pkg", r"!pkg\.api$"])

    def test_a_clean_tree_is_rendered_with_nothing_excluded(self):
        code, output, pdoc_argv = self._build({})
        self.assertEqual(code, 0)
        self.assertNotIn("NOT DOCUMENTED", output)
        self.assertEqual(pdoc_argv[-1], "pkg")

    def test_the_repository_can_ask_for_a_missing_dependency_to_fail_the_build(self):
        code, output, pdoc_argv = self._build(OPTIONAL, 'missing_dependency = "fail"\n')
        self.assertEqual(code, 1)
        self.assertIn("dependency not installed", output)
        self.assertIsNone(pdoc_argv)

    def test_a_first_party_import_error_still_fails_and_pdoc_is_not_run(self):
        code, output, pdoc_argv = self._build(
            {"src/pkg/broken.py": '"""Broken."""\nfrom pkg.ok import no_such_name\n'})
        self.assertEqual(code, 1)
        self.assertIn("FAILED", output)
        self.assertIn("cannot import pkg.broken", output)
        self.assertIsNone(pdoc_argv)

    def test_a_missing_first_party_module_is_an_error_not_an_optional_dependency(self):
        code, output, _ = self._build({"src/pkg/typo.py": '"""Typo."""\nimport pkg.gone\n'})
        self.assertEqual(code, 1)
        self.assertIn("cannot import pkg.typo", output)

    def test_an_excluded_module_is_not_probed(self):
        code, output, pdoc_argv = self._build(OPTIONAL, 'exclude = ["pkg.api"]\n')
        self.assertEqual(code, 0)
        self.assertNotIn("NOT DOCUMENTED", output)
        self.assertEqual(pdoc_argv[-1], "!pkg.api")


@unittest.skipUnless(__import__("importlib.util").util.find_spec("pdoc"), "requires pdoc (repolens[docs])")
class RealPdocBuildTests(unittest.TestCase):
    def test_pdoc_documents_the_rest_of_a_package_when_one_module_needs_a_missing_extra(self):
        tree = Tree({"src/pkg/__init__.py": '"""P."""\n', "src/pkg/ok.py": '"""OK."""\n', **OPTIONAL},
                    docs='[docs.python]\nmodules = ["pkg"]\npath = ["src"]\n')
        self.addCleanup(tree.close)
        code = _quiet(build.main, ["--python", "--strict", "--out", str(tree.root / "out")], config=tree.config)
        self.assertEqual(code, 0)
        self.assertTrue((tree.root / "out/python/pkg/ok.html").is_file())
        self.assertFalse((tree.root / "out/python/pkg/api.html").exists())


if __name__ == "__main__":
    unittest.main()
