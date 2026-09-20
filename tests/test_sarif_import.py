from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from repolens.config import Config
from repolens.report.runner import main
from repolens.report.sarif import import_sarif


class SarifTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "source.sarif"
        self.doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Example Analyzer", "rules": [
            {"id": "RULE1", "defaultConfiguration": {"level": "error"}, "properties": {"security-severity": "8.1"}}]}},
            "results": [{"ruleIndex": 0, "message": {"text": "Review this input"},
                         "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/a%20b.ts"}, "region": {"startLine": 5}}}]}]}]}

    def save(self):
        self.path.write_text(json.dumps(self.doc))
        return self.path

    def test_rule_index_default_severity_and_encoded_path(self):
        [run] = import_sarif(self.save(), self.root)
        [finding] = run.findings
        self.assertEqual((finding.file, finding.line, finding.severity), ("src/a b.ts", 5, "high"))
        self.assertEqual(finding.category, "security")
        self.assertEqual(finding.confidence, "medium")

    def test_failed_producer_does_not_become_a_clean_result(self):
        self.doc["runs"][0]["invocations"] = [{"executionSuccessful": False}]
        [run] = import_sarif(self.save(), self.root)
        self.assertTrue(run.error)
        self.assertEqual(len(run.findings), 1)

    def test_missing_results_does_not_become_a_clean_result(self):
        del self.doc["runs"][0]["results"]
        [run] = import_sarif(self.save(), self.root)
        self.assertTrue(run.error)

    def test_remote_or_traversal_locations_keep_findings_without_opening_them(self):
        for uri in ("https://example.com/secret.ts", "../../etc/passwd", "file:///etc/passwd", "C:/build/main.ts"):
            self.doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = uri
            [run] = import_sarif(self.save(), self.root)
            self.assertEqual(len(run.findings), 1)
            self.assertEqual(run.findings[0].file, "")

    def test_external_property_files_and_bad_versions_are_rejected(self):
        self.doc["runs"][0]["externalPropertyFileReferences"] = {"results": [{"location": {"uri": "https://example.com/results"}}]}
        with self.assertRaises(ValueError):
            import_sarif(self.save(), self.root)
        self.doc["version"] = "2.0.0"
        with self.assertRaises(ValueError):
            import_sarif(self.save(), self.root)

    def test_incomplete_import_cannot_be_baselined(self):
        self.doc["runs"][0]["invocations"] = [{"executionSuccessful": False}]
        self.save()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--only", "commands", "--sarif", str(self.path), "--update-baseline"], config=Config(self.root, {}))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / ".repolens/report_baseline.json").exists())

    def test_required_tool_not_selected_fails_check(self):
        profile = Config(self.root, {})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--only", "commands", "--update-baseline"], config=profile), 0)
            self.assertEqual(main(["--only", "commands", "--require", "bandit", "--check"], config=profile), 1)

    def test_malformed_rule_and_result_scalars_are_rejected(self):
        self.doc["runs"][0]["tool"]["driver"]["rules"][0]["id"] = ["RULE1"]
        with self.assertRaises(ValueError):
            import_sarif(self.save(), self.root)
        self.doc["runs"][0]["tool"]["driver"]["rules"][0]["id"] = "RULE1"
        self.doc["runs"][0]["results"][0]["ruleId"] = ["RULE1"]
        with self.assertRaises(ValueError):
            import_sarif(self.save(), self.root)


class SarifCommandTests(unittest.TestCase):
    """`[report] sarif_commands` runs an analyser that writes SARIF and imports what it
    wrote, so trivy, gitleaks, grype and anything else that speaks SARIF joins the report
    finding by finding, with its own ToolRun: missing binary SKIPPED, crash ERROR."""

    DOC = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "fakescanner"}},
           "results": [{"ruleId": "LEAK", "level": "error", "message": {"text": "a token"},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/a.ts"},
                                                            "region": {"startLine": 3}}}]}]}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tool(self, body: str) -> Path:
        """A stand-in analyser, so these tests do not need trivy or gitleaks installed."""
        path = self.root / "fake_tool.py"
        path.write_text(body, encoding="utf-8")
        return path

    def runs(self, spec: dict) -> list:
        from repolens.report.runner import Context, _sarif_command_runs
        from repolens.config import merge
        from repolens.report.runner import DEFAULTS
        cfg = Config(self.root, {"report": {"sarif_commands": [spec]}})
        return _sarif_command_runs(Context(cfg, merge(DEFAULTS, cfg.section("report"))))

    def test_findings_are_imported_under_the_configured_name(self):
        tool = self.tool("import json, sys\n"
                         f"json.dump({self.DOC!r}, open(sys.argv[1], 'w'))\n")
        [run] = self.runs({"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]})
        self.assertEqual(run.tool, "fakescanner")
        self.assertEqual((run.findings[0].file, run.findings[0].line), ("src/a.ts", 3))
        self.assertEqual(run.findings[0].severity, "high")

    def test_a_missing_binary_is_skipped_not_a_clean_result(self):
        [run] = self.runs({"name": "gitleaks", "run": ["definitely-not-installed", "{output}"]})
        self.assertIn("not found", run.skipped)
        self.assertEqual(run.findings, [])
        self.assertFalse(run.error)

    def test_an_unexpected_exit_is_an_error(self):
        tool = self.tool("import sys\nsys.exit(7)\n")
        [run] = self.runs({"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]})
        self.assertIn("exited 7", run.error)

    def test_exit_one_is_found_something_not_a_failure(self):
        """Scanners conventionally exit 1 when they found a result; treating that as a
        crash would drop every run that had something to say."""
        tool = self.tool("import json, sys\n"
                         f"json.dump({self.DOC!r}, open(sys.argv[1], 'w'))\n"
                         "sys.exit(1)\n")
        [run] = self.runs({"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]})
        self.assertFalse(run.error)
        self.assertEqual(len(run.findings), 1)

    def test_a_clean_exit_that_wrote_nothing_is_an_error(self):
        """Exit 0 and no document is a tool that did not look, not a clean repository."""
        tool = self.tool("pass\n")
        [run] = self.runs({"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]})
        self.assertIn("wrote no SARIF", run.error)

    def test_an_argv_without_the_output_placeholder_is_rejected(self):
        [run] = self.runs({"name": "fakescanner", "run": ["true"]})
        self.assertIn("{output}", run.error)

    def test_the_seam_is_selected_like_any_other_tool(self):
        """It is configuration, not an implicit side effect: `--only lessons` must not
        shell out to a configured scanner, and `--list-tools` must say the seam exists."""
        from repolens.report.runner import _PSEUDO_TOOLS, main
        self.assertIn("sarif-commands", _PSEUDO_TOOLS)
        tool = self.tool("import sys\nopen('" + (self.root / "ran").as_posix() + "', 'w').close()\n"
                         "sys.exit(9)\n")
        cfg = Config(self.root, {"report": {"sarif_commands": [
            {"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]}]}})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            main(["--only", "migrations", "--out", str(self.root / "out")], config=cfg)
        self.assertFalse((self.root / "ran").exists())
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            main(["--only", "sarif-commands", "--out", str(self.root / "out")], config=cfg)
        self.assertTrue((self.root / "ran").exists())

    def test_unparsable_sarif_is_an_error_on_that_tool_alone(self):
        tool = self.tool("import sys\nopen(sys.argv[1], 'w').write('not json')\n")
        [run] = self.runs({"name": "fakescanner", "run": ["{python}", str(tool), "{output}"]})
        self.assertTrue(run.error)
        self.assertEqual(run.findings, [])


class AnalysisSarifTests(unittest.TestCase):
    @unittest.skipUnless(all(importlib.util.find_spec(m) for m in ("tree_sitter", "sqlglot")), "install repolens[stack]")
    def test_a_python_file_the_checks_never_opened_is_not_a_successful_run(self):
        from repolens.analysis import analyze
        from repolens.core.findings import to_sarif
        from repolens.impact.config import Config as ImpactConfig
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "svc.py").write_text('def f(cur, x):\n    cur.execute(f"SELECT * FROM t WHERE id = {x}")\n' + "# pad\n" * 40)
            (root / "app" / "small.py").write_text("x = 1\n")
            settings = ImpactConfig.load(root)
            settings.max_file_bytes = 100
            result = analyze(root, config=settings)
            self.assertFalse(result.complete)
            runs = {run["tool"]["driver"]["name"]: run for run in json.loads(to_sarif(result.runs, "0"))["runs"]}
            for name in ("repolens/security", "repolens/performance"):
                invocation = runs[name]["invocations"][0]
                self.assertFalse(invocation["executionSuccessful"])
                self.assertIn("app/svc.py", invocation["toolExecutionNotifications"][0]["message"]["text"])
                self.assertIn("results", runs[name])  # what WAS examined is still reported
            # No migration root contains the file, so that run's claim is unchanged.
            self.assertTrue(runs["repolens/migrations"]["invocations"][0]["executionSuccessful"])
            self.assertEqual(next(t for t in result.to_dict()["tools"] if t["tool"] == "security")["notes"],
                             ["app/svc.py was not examined (FILE_SKIPPED)."])
