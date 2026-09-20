"""The lessons-learnt catalogue as a report tool.

The catalogue's recipes are written for a person running a search, so the tests that
matter are the ones that pin down which recipes are allowed to run automatically and
that nothing this tool reports can fail a gate.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from repolens.core.findings import Finding
from repolens.lessons import load
from repolens.report import lessons


class CatalogueShipsWithThePackage(unittest.TestCase):
    def test_the_catalogue_loads_from_package_data(self):
        """`repolens report` reads it inside a TARGET repository, so it cannot live in docs/."""
        data = load()
        self.assertGreater(len(data["lessons"]), 200)
        self.assertTrue({"categories", "lessons", "schema_version"} <= set(data))
        for lesson in data["lessons"]:
            self.assertTrue({"id", "category", "severity", "stacks", "detection",
                             "title", "prevention"} <= set(lesson), lesson.get("id"))

    def test_every_severity_is_one_the_finding_model_accepts(self):
        for lesson in load()["lessons"]:
            Finding(tool="lessons", rule=f"lessons/{lesson['id']}", severity=lesson["severity"],
                    confidence="low", message=lesson["title"])

    def test_the_docs_copy_was_regenerated_from_the_catalogue(self):
        """`docs/lessons/technical-lessons-learnt.js` is generated; a stale copy makes the
        browsable page disagree with what the tool reports."""
        page = Path(__file__).resolve().parents[1] / "docs/lessons/technical-lessons-learnt.js"
        prefix, suffix = "window.LESSONS = ", ";\n"
        text = page.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(prefix) and text.endswith(suffix))
        self.assertEqual(json.loads(text[len(prefix):-len(suffix)].replace("<\\/", "</")), load())


class RunnableRecipes(unittest.TestCase):
    """`runnable_pattern` is the whole precision story: it decides which of the catalogue's
    human-written recipes are safe to run without judgement."""

    def test_a_bare_pattern_runs(self):
        self.assertIsNotNone(lessons.runnable_pattern(r"@ts-nocheck|@ts-ignore"))
        self.assertIsNotNone(lessons.runnable_pattern(r"forEach\(async"))
        # A literal two-word alternative is still a pattern, not prose.
        self.assertIsNotNone(lessons.runnable_pattern(r"getConfig\(|RuntimeConfig|next lint"))

    def test_a_recipe_scoped_by_a_trailing_note_is_refused(self):
        """The note carries the scope. Dropping "in *.example" turned a secrets recipe into
        a match on every long assignment in the repository, including its dependencies."""
        self.assertIsNone(lessons.runnable_pattern(
            r"(?i)(secret|password|api_key|token)\s*=\s*(?!<)[A-Za-z0-9/+_\-]{12,}  -- in *.example"))
        self.assertIsNone(lessons.runnable_pattern(
            r"load_dotenv\([^)]*override=True  (in tests/ or modules tests import)"))
        self.assertIsNone(lessons.runnable_pattern(r"status\s*===\s*403 (outside the classifier)"))

    def test_a_recipe_whose_condition_is_prose_is_refused(self):
        self.assertIsNone(lessons.runnable_pattern(
            r"a line not ending in [;{},(\[] followed by a line matching ^\s*[\(\[`]"))
        self.assertIsNone(lessons.runnable_pattern(r"\b(19|20)\d{2}\b assigned to a year-like identifier"))

    def test_a_pattern_that_does_not_compile_is_refused_rather_than_raising(self):
        self.assertIsNone(lessons.runnable_pattern(r"unbalanced ( group"))
        self.assertIsNone(lessons.runnable_pattern("   "))

    def test_the_catalogue_still_offers_a_useful_number_of_runnable_recipes(self):
        """A stricter filter is welcome; silently filtering everything out is not."""
        runnable = [recipe for lesson in load()["lessons"] for recipe in lesson["detection"]
                    if recipe["kind"] == "regex" and lessons.runnable_pattern(recipe["recipe"])]
        self.assertGreater(len(runnable), 60)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def rules(self, findings: list[Finding]) -> set[str]:
        return {f.rule for f in findings}


class StackScoping(Fixture):
    def test_dependencies_and_suffixes_establish_the_stacks(self):
        self.write("package.json", json.dumps({"dependencies": {"next": "15", "axios": "1"}}))
        self.write("api.py", "x = 1\n")
        stacks = lessons.detect_stacks(self.root, ())
        self.assertTrue({"nextjs", "axios", "javascript", "python"} <= stacks)

    def test_a_dependency_name_is_matched_as_a_whole_word(self):
        self.write("package.json", json.dumps({"dependencies": {"next-intl": "1", "reactivity": "1"}}))
        stacks = lessons.detect_stacks(self.root, ())
        self.assertNotIn("nextjs", stacks)
        self.assertNotIn("react", stacks)

    def test_a_nextjs_lesson_does_not_apply_to_a_python_only_repository(self):
        catalogue = load()
        nextjs = next(l for l in catalogue["lessons"] if "nextjs" in l["stacks"])
        self.assertFalse(lessons.applies(nextjs, {"python"}, {"nextjs", "python"}))
        self.assertTrue(lessons.applies(nextjs, {"python", "nextjs"}, {"nextjs", "python"}))

    def test_a_lesson_tagged_only_with_undetectable_stacks_always_applies(self):
        general = {"id": "X", "stacks": ["architecture", "owasp"]}
        self.assertTrue(lessons.applies(general, set(), {"nextjs", "python"}))


class Scanning(Fixture):
    PROBE = {
        "package.json": json.dumps({"dependencies": {"next": "15", "react": "19"}}),
        "app/page.tsx": ("const rows = items.sort();\n"
                         "list.forEach(async (x) => { await save(x); });\n"
                         "// @ts-ignore\n"),
        "api.py": "from datetime import datetime\nts = datetime.utcnow()\n",
    }

    def probe(self, extra: dict[str, str] | None = None) -> list[Finding]:
        for path, text in {**self.PROBE, **(extra or {})}.items():
            self.write(path, text)
        return lessons.scan(self.root)

    def test_it_reports_the_traps_that_are_present(self):
        found = self.rules(self.probe())
        self.assertTrue({"lessons/TS-006", "lessons/TS-007", "lessons/TS-002", "lessons/PY-018"}
                        <= found, found)

    def test_nothing_it_reports_can_fail_a_gate(self):
        """Low confidence caps a lesson at P2, below the default `fail_on = "P1"`. A
        catalogue recipe is a prompt to go and look, never a verdict."""
        for finding in self.probe():
            self.assertEqual(finding.confidence, "low")
            self.assertIn(finding.priority, ("P2", "P3"))

    def test_a_finding_carries_the_lesson_and_its_prevention(self):
        finding = next(f for f in self.probe() if f.rule == "lessons/TS-007")
        self.assertEqual(finding.file, "app/page.tsx")
        self.assertEqual(finding.line, 2)
        self.assertIn("TS-007", finding.message)
        self.assertTrue(finding.remedy)
        self.assertIn("recipe", finding.evidence)

    def test_a_python_lesson_is_not_matched_against_typescript(self):
        findings = self.probe({"app/dates.tsx": "const ts = datetime.utcnow();\n"})
        self.assertEqual({f.file for f in findings if f.rule == "lessons/PY-018"}, {"api.py"})

    def test_skipped_directories_are_not_read(self):
        """A repository's installed dependencies are not its code; reporting the catalogue
        against site-packages buried every real result."""
        findings = self.probe({".venv/lib/python3.12/site-packages/x/y.py":
                               "from datetime import datetime\nts = datetime.utcnow()\n"})
        self.assertEqual({f.file for f in findings if f.rule == "lessons/PY-018"}, {"api.py"})
        with_venv = lessons.scan(self.root, [".git", "__pycache__"])
        self.assertIn(".venv/lib/python3.12/site-packages/x/y.py",
                      {f.file for f in with_venv if f.rule == "lessons/PY-018"})

    def test_one_broad_recipe_cannot_take_over_the_report(self):
        body = "".join(f"const a{i} = xs.sort();\n" for i in range(lessons.MAX_PER_LESSON + 25))
        findings = self.probe({"app/many.tsx": body})
        matches = [f for f in findings if f.rule == "lessons/TS-006"]
        self.assertEqual(len(matches), lessons.MAX_PER_LESSON)
        capped = [f for f in findings if f.rule == "lessons/TS-006-capped"]
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0].severity, "info")

    def test_a_very_long_line_is_skipped_rather_than_matched(self):
        """A minified bundle is one enormous line, and a recipe with `[^)]*` in it is
        quadratic on those."""
        bundle = "x=1;" * lessons.MAX_LINE_LENGTH + "items.sort();\n"
        findings = self.probe({"app/bundle.js": bundle})
        self.assertNotIn("app/bundle.js", {f.file for f in findings})

    def test_a_repository_with_none_of_the_traps_reports_nothing(self):
        self.write("api.py", "from datetime import datetime, UTC\nts = datetime.now(UTC)\n")
        self.assertEqual(lessons.scan(self.root), [])


class Companion(Fixture):
    """`lessons.md` answers a different question from the report: not "what matched" but
    "what is known to go wrong with this stack, and which of it did anything check?"."""

    def survey(self):
        self.write("package.json", json.dumps({"dependencies": {"next": "15"}}))
        self.write("app/page.tsx", "const rows = items.sort();\n")
        return lessons.survey(self.root)

    def test_the_survey_separates_what_applies_from_what_was_checked(self):
        result = self.survey()
        self.assertGreater(len(result.applicable), len(result.checked))
        self.assertTrue(result.checked <= {l["id"] for l in result.applicable})
        self.assertIn("TS-006", result.checked)

    def test_scan_still_returns_exactly_the_surveys_findings(self):
        self.write("package.json", json.dumps({"dependencies": {"next": "15"}}))
        self.write("app/page.tsx", "const rows = items.sort();\n")
        self.assertEqual([f.rule for f in lessons.scan(self.root)],
                         [f.rule for f in lessons.survey(self.root).findings])

    def test_the_checklist_marks_an_unchecked_lesson_as_unchecked(self):
        """The whole point: silence about a lesson nobody ran must not read as a pass."""
        result = self.survey()
        text = lessons.to_markdown(result)
        unchecked = next(l for l in result.applicable if l["id"] not in result.checked)
        self.assertIn(f"### {unchecked['id']} ", text)
        self.assertIn("not checked here", text)
        self.assertIn("means nobody looked", text)

    def test_a_lesson_that_matched_says_so_with_its_count(self):
        result = self.survey()
        text = lessons.to_markdown(result)
        self.assertIn("**1 match(es) in the report**", text)
        self.assertIn("checked, no match", text)

    def test_an_unchecked_lesson_carries_its_own_recipe_so_a_person_can_run_it(self):
        result = self.survey()
        text = lessons.to_markdown(result)
        self.assertIn("**How to check it yourself.**", text)

    def test_the_report_writes_it_beside_the_other_outputs(self):
        import contextlib, io
        from repolens.config import Config
        from repolens.report.runner import main
        self.write("package.json", json.dumps({"dependencies": {"next": "15"}}))
        self.write("app/page.tsx", "const rows = items.sort();\n")
        out = self.root / "out"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            main(["--only", "lessons", "--out", str(out)], config=Config(self.root, {}))
        self.assertTrue((out / "lessons.md").is_file())
        self.assertIn("lessons.md", buffer.getvalue())

    def test_no_lessons_md_when_the_tool_did_not_run(self):
        """An absent file is honest; a stale one from a previous run would not be."""
        import contextlib, io
        from repolens.config import Config
        from repolens.report.runner import main
        out = self.root / "out"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(["--only", "migrations", "--out", str(out)], config=Config(self.root, {}))
        self.assertFalse((out / "lessons.md").exists())


class EveryLessonCanReachTheFilesItIsAbout(unittest.TestCase):
    """A lesson whose recipe searches markup is dead if its category only walks scripts.

    The accessibility lessons were written against `role="tablist"` in HTML and a colour
    token in CSS, and reported nothing at all: their category inherited the `frontend`
    group, whose suffixes are script extensions only. Nothing failed -- the survey simply
    never opened a .html file. These tests pin the coupling between what a recipe looks
    for and what the walk is given.
    """

    def setUp(self):
        self.data = load()
        self.groups = {c["id"]: c["group"] for c in self.data["categories"]}

    def test_accessibility_lessons_walk_markup_and_stylesheets(self):
        for lesson in self.data["lessons"]:
            if lesson["category"] != "accessibility":
                continue
            suffixes = lessons._suffixes_for(lesson, self.groups)
            self.assertIn(".html", suffixes, lesson["id"])
            self.assertIn(".css", suffixes, lesson["id"])

    def test_a_recipe_naming_a_suffix_is_given_that_suffix(self):
        """`<button` or `role=` can only match markup; `--token:` only a stylesheet."""
        markers = ((("role=", "<button", "<div", "aria-", "<h["), ".html"),
                   (("--ink", "@media", "color:"), ".css"))
        for lesson in self.data["lessons"]:
            suffixes = lessons._suffixes_for(lesson, self.groups)
            for recipe in lesson["detection"]:
                if recipe["kind"] != "regex" or lessons.runnable_pattern(recipe["recipe"]) is None:
                    continue
                for needles, suffix in markers:
                    if any(n in recipe["recipe"] for n in needles):
                        self.assertIn(suffix, suffixes,
                                      f"{lesson['id']} searches for {needles} but never opens a {suffix} file")

    def test_a_lesson_is_not_scoped_out_by_a_framework_it_does_not_need(self):
        """AX-001 was tagged `react`, so a vanilla-HTML repo -- exactly the kind that
        still hand-writes a broken tablist -- never had the lesson applied to it."""
        known = {s for values in lessons._SUFFIX_STACKS.values() for s in values}
        known |= {s for values in lessons._DEPENDENCY_STACKS.values() for s in values}
        for lesson in self.data["lessons"]:
            if lesson["category"] != "accessibility":
                continue
            detectable = [s for s in lesson["stacks"] if s in known]
            self.assertNotIn("react", detectable, lesson["id"])
            self.assertNotIn("tailwind", detectable, lesson["id"])


class DefaultToolList(unittest.TestCase):
    def test_no_networked_tool_is_on_by_default(self):
        """CLAUDE.md: repolens must not touch the network. osv-scanner queries api.osv.dev
        and pip-audit queries PyPI, so neither may be in the default list."""
        from repolens.report.runner import DEFAULTS
        self.assertNotIn("osv-scanner", DEFAULTS["tools"])
        self.assertNotIn("pip-audit", DEFAULTS["tools"])
        self.assertNotIn("impact", DEFAULTS["tools"])

    def test_lessons_and_semgrep_are_on_by_default(self):
        """semgrep's adapter refuses a registry config, so it is offline by construction;
        being absent from the list is indistinguishable from having found nothing."""
        from repolens.report.runner import DEFAULTS
        self.assertIn("lessons", DEFAULTS["tools"])
        self.assertIn("semgrep", DEFAULTS["tools"])

    def test_semgrep_skips_with_an_actionable_reason_when_unconfigured(self):
        from repolens.config import Config, merge
        from repolens.report.runner import DEFAULTS, Context, Skip, _semgrep
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(Path(tmp), {})
            ctx = Context(cfg, merge(DEFAULTS, cfg.section("report")))
            with self.assertRaises(Skip) as caught:
                _semgrep(ctx)
        self.assertIn("semgrep_config", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
