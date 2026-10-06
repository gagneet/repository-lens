"""`repolens docs coverage`: which public symbols carry a docstring, as a per-file ratchet.

    repolens docs coverage                       summary, and the files with most gaps
    repolens docs coverage --missing backend/x   every undocumented symbol under a path
    repolens docs coverage --update-baseline     accept today's counts
    repolens docs coverage --check               CI: fail if a file gained an undocumented symbol

What counts as PUBLIC:

  Python      the module itself, and every class, function and method whose name does
              not start with `_`, at module level or inside a public class. Dunders never
              count; nor do `@overload` stubs or `@x.setter`/`@x.deleter` (the getter
              carries the property's docstring). Functions nested in functions are
              implementation detail. Parsed with `ast`.
  TypeScript  every top-level EXPORTED declaration — `export function|class|const|
  JavaScript  interface|type|enum`, and a local declaration exported by name
              (`export default Page`, `export { a, b }`, `export default withAuth(Page)`),
              and the methods of each counted class (not `private`/`#name` members, the
              constructor or `set` accessors). A file-local function, class or
              function-valued `const` is the TypeScript `_private`: it counts only with
              `include_private = true`, as do private methods.
              Documented means a `/** ... */` block before it, with only blank lines,
              `//` comments or decorators between. This half is a REGEX over lines, not a
              parser: a declaration that does not start at column 0 is not seen, and a
              re-export from another module (`export { a } from "./a"`) is counted where
              it is declared, not where it is re-exported.

A docstring matching `[docs] placeholder_patterns` counts as MISSING. Generated
boilerplate is not documentation, and counting it would let coverage rise while nothing a
reader can use was written.

The gate is a ratchet on the count of undocumented symbols per file, for the reason every
ratchet here exists: a strict gate would fail every change on debt its author did not
create. A NEW file must be fully documented; an existing file may not gain an undocumented
symbol; an improvement fails until the baseline is updated, so a win cannot be silently
given back. A file that no longer parses is UNMEASURED and keeps its baseline count — it
is neither an improvement nor a regression, and it is printed.
"""
from __future__ import annotations

import warnings
import argparse
import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Config
from ..core.files import iter_files, read_text
from ..core.ratchet import Ratchet
from .settings import DocsSettings, from_config

_OVERLOADS = frozenset({"overload", "typing.overload", "typing_extensions.overload"})


@dataclass(frozen=True)
class Symbol:
    """One public symbol, and whether it carries real documentation."""
    path: str
    line: int
    kind: str
    name: str
    documented: bool


@dataclass
class FileResult:
    """The public symbols of one file, or why the file could not be measured."""
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    error: str = ""

    @property
    def missing(self) -> list[Symbol]:
        """The public symbols in this file that carry no real documentation."""
        return [s for s in self.symbols if not s.documented]


# ── Python ───────────────────────────────────────────────────────────────────────
def _decorators(node: ast.AST) -> list[str]:
    names = []
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        try:
            names.append(ast.unparse(target))
        except Exception:  # noqa: BLE001 - an exotic decorator is simply not one we skip on
            continue
    return names


def _stub(node: ast.AST) -> bool:
    """An overload signature or a property's setter/deleter: documented elsewhere."""
    return any(name in _OVERLOADS or name.endswith((".setter", ".deleter"))
               for name in _decorators(node))


def _public(name: str, include_private: bool) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return False
    return include_private or not name.startswith("_")


def _real_doc(doc: str | None, s: DocsSettings) -> bool:
    return bool(doc and doc.strip()) and not s.is_placeholder(doc)


def python_file(rel_path: str, source: str, s: DocsSettings) -> FileResult:
    """Public symbols of one Python file."""
    result = FileResult(rel_path, "python")
    try:
        with warnings.catch_warnings():  # target code's own SyntaxWarnings are not ours to print
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        result.error = f"does not parse: {exc.__class__.__name__}: {exc}"[:200]
        return result
    # An empty module (a bare `__init__.py`) has nothing to describe.
    if s.count_modules and tree.body:
        result.symbols.append(Symbol(rel_path, 1, "module", "<module>",
                                     _real_doc(ast.get_docstring(tree), s)))

    def visit(body: list[ast.stmt], in_class: bool, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _public(node.name, s.include_private) and not _stub(node):
                    result.symbols.append(Symbol(
                        rel_path, node.lineno, "method" if in_class else "function",
                        prefix + node.name, _real_doc(ast.get_docstring(node), s)))
            elif isinstance(node, ast.ClassDef) and _public(node.name, s.include_private):
                result.symbols.append(Symbol(rel_path, node.lineno, "class", prefix + node.name,
                                             _real_doc(ast.get_docstring(node), s)))
                visit(node.body, True, f"{prefix}{node.name}.")

    visit(tree.body, False, "")
    return result


# ── TypeScript / JavaScript ─────────────────────────────────────────────────────
_IDENT = r"[A-Za-z_$][\w$]*"
_JS_DECL = re.compile(
    rf"^(?P<export>export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?"
    rf"(?P<kind>function\s*\*?|abstract\s+class|class|const\s+enum|const|let|var|interface|type|enum)"
    rf"(?:\s+|\s*(?=\())(?P<name>{_IDENT})?")
_EXPORT_DEFAULT_NAME = re.compile(
    rf"^export\s+default\s+(?:[\w$.]+\s*\(\s*)*(?P<name>{_IDENT})\s*\)*\s*;?\s*(?://.*)?$")
_EXPORT_LIST = re.compile(r"^export\s+(?:type\s+)?\{(?P<names>[^}]*)\}(?P<tail>[^\n]*)",
                          re.MULTILINE)
_KEYWORDS = frozenset({"function", "class", "async", "abstract", "const", "let", "var",
                       "interface", "type", "enum", "new", "await", "declare"})


def _kind(raw: str) -> str:
    raw = " ".join(raw.split()).replace(" *", "").rstrip("*")
    if raw.endswith("class"):
        return "class"
    if raw in ("const", "let", "var"):
        return "const"
    if raw == "const enum":
        return "enum"
    return raw


def _exported_by_name(text: str) -> set[str]:
    """Local names exported by `export default X`, `export default hoc(X)` or `export {a}`."""
    names: set[str] = set()
    for line in text.splitlines():
        match = _EXPORT_DEFAULT_NAME.match(line)
        if match and match.group("name") not in _KEYWORDS:
            names.add(match.group("name"))
    for match in _EXPORT_LIST.finditer(text):
        if re.match(r"\s*from\b", match.group("tail")):
            continue  # a re-export: documented where it is declared
        for part in match.group("names").split(","):
            local = part.strip().removeprefix("type ").split(" as ")[0].strip()
            if re.fullmatch(_IDENT, local):
                names.add(local)
    return names


def _jsdoc_before(lines: list[str], index: int) -> str | None:
    """The `/** */` block that documents the declaration on `lines[index]`, if any."""
    i = index - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("@"):
            i -= 1
            continue
        break
    if i < 0 or not lines[i].rstrip().endswith("*/"):
        return None
    end = i
    while i >= 0 and "/*" not in lines[i]:
        i -= 1
    if i < 0:
        return None
    block = "\n".join(lines[i:end + 1])
    start = block.index("/*")
    if not block.startswith("/**", start) or block.startswith("/**/", start):
        return None
    return block[start + 3:].rsplit("*/", 1)[0]


# A class member that is a method: `name(`, `async *name<T>(`, `get name(`, or an arrow
# property `name = async (…) =>`. Read only at the class body's own indentation.
_JS_MEMBER = re.compile(
    rf"^(?P<indent>[ \t]+)(?P<mods>(?:(?:public|private|protected|static|async|override|readonly|abstract|declare|"
    rf"accessor|get|set)\s+)*)(?:\*\s*)?(?P<name>#?{_IDENT})\s*(?:\?\s*)?(?:<[^>]*>\s*)?"
    rf"(?:\(|=\s*(?:async\s+)?(?:\(|{_IDENT}\s*=>|function\b)(?=.*=>|.*function\b|[^)]*$))")
_NOT_MEMBERS = frozenset({"constructor", "if", "for", "while", "switch", "return", "catch", "super", "with", "do"})
# A non-exported `const f = …` counts only when it holds a function (under `include_private`).
_FUNCTION_VALUE = re.compile(rf"=\s*(?:async\s+)?(?:function\b|{_IDENT}\s*=>|\(.*=>|\([^)]*$|<)")


def _class_methods(rel_path: str, lines: list[str], start: int, owner: str, s: DocsSettings) -> list[Symbol]:
    """Methods of the class declared on `lines[start]`, up to its column-0 `}`.

    Public members only (not `private`, not `#name`) unless `include_private`; never the
    constructor (the class's own JSDoc describes construction, as a Python class docstring
    does) or a `set` accessor (the getter documents the property). An overload or abstract
    signature without a body (`f(a: string): void;`) is documented on the implementation."""
    if lines[start].rstrip().endswith("}"):
        return []  # `class A {}` on one line
    found: list[Symbol] = []
    indent: str | None = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("}"):
            break
        if not line.strip() or line.lstrip().startswith(("//", "/*", "*", "@")):
            continue
        current = line[:len(line) - len(line.lstrip())]
        indent = current if indent is None else indent
        if current != indent or not (match := _JS_MEMBER.match(line)):
            continue
        name, mods = match.group("name"), match.group("mods").split()
        if name in _NOT_MEMBERS or "set" in mods or (re.search(r"\)\s*(?::[^{=]*)?;\s*$", line) and "{" not in line):
            continue
        if (name.startswith("#") or "private" in mods) and not s.include_private:
            continue
        found.append(Symbol(rel_path, index + 1, "method", f"{owner}.{name}",
                            _real_doc(_jsdoc_before(lines, index), s)))
    return found


def javascript_file(rel_path: str, text: str, s: DocsSettings) -> FileResult:
    """Top-level declarations and class methods of one TypeScript/JavaScript file.

    Exported declarations count; a non-exported function, class or function-valued
    `const` counts only under `include_private` (a file-local helper is the TypeScript
    `_private`). Methods count inside every counted class. A line regex, not a parser."""
    result = FileResult(rel_path, "javascript")
    lines = text.splitlines()
    by_name = _exported_by_name(text)
    for index, line in enumerate(lines):
        match = _JS_DECL.match(line)
        if not match:
            continue
        kind = _kind(match.group("kind"))
        name = match.group("name") or ""
        if kind not in s.javascript_kinds:
            continue
        exported = bool(match.group("export"))
        if not exported and name not in by_name:
            local = s.include_private and name and (
                kind in ("function", "class") or (kind == "const" and _FUNCTION_VALUE.search(line)))
            if not local:
                continue
        if not name:
            if "default" not in (match.group("export") or ""):
                continue
            name = "default"
        doc = _jsdoc_before(lines, index)
        result.symbols.append(Symbol(rel_path, index + 1, kind, name, _real_doc(doc, s)))
        if kind == "class" and "method" in s.javascript_kinds:
            result.symbols.extend(_class_methods(rel_path, lines, index, name, s))
    return result


# ── walking ──────────────────────────────────────────────────────────────────────
def measure(s: DocsSettings) -> list[FileResult]:
    """Every configured source file, measured. Sorted by path, so output is stable."""
    results: list[FileResult] = []
    for roots, exts, reader in ((s.python_roots, s.python_extensions, python_file),
                                (s.javascript_roots, s.javascript_extensions, javascript_file)):
        if not roots:
            continue
        for path in iter_files(s.root, roots, exts, s.skip_parts):
            rel_path = path.relative_to(s.root).as_posix()
            if s.skipped_file(rel_path):
                continue
            try:
                text = read_text(path)
            except OSError as exc:  # deleted or unreadable between listing and reading
                results.append(FileResult(rel_path, reader.__name__.split("_")[0],
                                          error=f"unreadable: {exc}"[:200]))
                continue
            results.append(reader(rel_path, text, s))
    return sorted(results, key=lambda r: r.path)


def missing_counts(results: list[FileResult], baseline: dict[str, int] | None) -> dict[str, int]:
    """Per-file undocumented counts for the ratchet. An unmeasured file keeps its
    baseline count, so failing to parse is never mistaken for an improvement."""
    counts: dict[str, int] = {}
    for r in results:
        if r.error:
            if baseline and baseline.get(r.path):
                counts[r.path] = baseline[r.path]
            continue
        if r.missing:
            counts[r.path] = len(r.missing)
    return counts


def ratchet_for(s: DocsSettings) -> Ratchet:
    """The per-file undocumented-symbol ratchet, backed by `[docs] baseline`."""
    return Ratchet(
        label="Docstring coverage",
        baseline_path=s.baseline,
        root=s.root,
        comment=("Undocumented public symbols per file. A ratchet: a file may not gain one, "
                 "a new file must have none, and an improvement fails until this is updated. "
                 f"Regenerate with `{s.command} --update-baseline`; never edit by hand."),
        update_command=f"{s.command} --update-baseline",
        unit="undocumented symbol",
        subject="docstring coverage",
        regress_reason="a file gained an undocumented public symbol",
        new_file_note="a new file documents every public symbol",
        failure_help=(f"List them with `{s.command} --missing <path>`, and write the docstring "
                      "(or JSDoc block) a reader needs: what it is for, not what the code says.",),
    )


# ── output ───────────────────────────────────────────────────────────────────────
def summarise(results: list[FileResult]) -> dict:
    """Per language: files, unmeasured files, symbols, documented symbols and percent."""
    by_lang: dict[str, dict[str, int]] = {}
    for r in results:
        lang = by_lang.setdefault(r.language, {"files": 0, "symbols": 0, "documented": 0,
                                               "unmeasured": 0})
        lang["files"] += 1
        lang["unmeasured"] += bool(r.error)
        lang["symbols"] += len(r.symbols)
        lang["documented"] += sum(1 for sym in r.symbols if sym.documented)
    for lang in by_lang.values():
        lang["percent"] = round(100.0 * lang["documented"] / lang["symbols"], 1) if lang["symbols"] else 100.0
    return by_lang


def _print_summary(results: list[FileResult], top: int) -> None:
    for language, row in sorted(summarise(results).items()):
        print(f"  {language:<11} {row['documented']:>6} / {row['symbols']:<6} documented "
              f"({row['percent']:.1f}%) across {row['files']} files"
              + (f", {row['unmeasured']} UNMEASURED" if row["unmeasured"] else ""))
    worst = sorted((r for r in results if r.missing), key=lambda r: (-len(r.missing), r.path))[:top]
    if worst:
        print(f"\n  most undocumented ({top} shown):")
        for r in worst:
            print(f"    {len(r.missing):>4}  {r.path}")
    for r in results:
        if r.error:
            print(f"  UNMEASURED  {r.path}: {r.error}")


def main(argv: list[str] | None = None, *, config: Config | None = None,
         settings: DocsSettings | None = None, prog: str | None = None) -> int:
    """`repolens docs coverage`. Exit 1 only under `--check`/`--update-baseline` rules."""
    ap = argparse.ArgumentParser(prog=prog, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="ratchet: exit 1 if a file gained an undocumented public symbol")
    ap.add_argument("--update-baseline", action="store_true", help="accept today's counts")
    ap.add_argument("--missing", nargs="*", metavar="PATH",
                    help="list undocumented symbols (optionally only under these paths)")
    ap.add_argument("--json", metavar="FILE", help="write every measured symbol as JSON")
    ap.add_argument("--top", type=int, default=15, help="files to show in the summary")
    args = ap.parse_args(argv)

    s = settings or from_config(config)
    results = measure(s)
    print(f"docs coverage: {len(results)} files")
    _print_summary(results, args.top)

    if args.missing is not None:
        prefixes = [p.rstrip("/") for p in args.missing]
        print()
        for r in results:
            if prefixes and not any(r.path == p or r.path.startswith(p + "/") for p in prefixes):
                continue
            for sym in r.missing:
                print(f"  {sym.path}:{sym.line}  {sym.kind:<9} {sym.name}")

    if args.json:
        payload = {"summary": summarise(results),
                   "files": [{**asdict(r), "symbols": [asdict(x) for x in r.symbols]} for r in results]}
        Path(args.json).write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")

    if not (args.check or args.update_baseline):
        return 0
    ratchet = ratchet_for(s)
    return ratchet.run(missing_counts(results, ratchet.load()), update=args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
