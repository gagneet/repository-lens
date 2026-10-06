"""Docs settings: generic defaults, overridden by `[docs]` in repolens.toml."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config, load_config, merge

#: Opens every JSDoc block `repolens featuretrace propose --jsdoc` drafts. Always a placeholder, even when
#: a repository lists its own patterns: `merge` replaces lists, and `repolens init` writes an empty one.
REPOLENS_PLACEHOLDER = r"TODO\(repolens\)"

DEFAULTS: dict[str, Any] = {
    # Where the coverage count looks. Build output and dependencies are left out by
    # `skip_parts` and `skip_file_patterns`; a repository narrows either list to its sources.
    "python_roots": ["."],
    "javascript_roots": ["."],
    "python_extensions": [".py"],
    "javascript_extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"],
    # Whole path components; `a/b` is a run of consecutive components.
    "skip_parts": [".git", "venv", ".venv", "node_modules", "__pycache__", "site-packages",
                   "dist", "build", "coverage", ".next", "alembic/versions"],
    # Repo-relative path regexes. Tests describe themselves by name; a declaration file
    # restates a type whose documentation lives with its implementation.
    "skip_file_patterns": [r"(^|/)test_[^/]*\.py$", r"(^|/)[^/]*_test\.py$", r"(^|/)conftest\.py$",
                           r"\.(test|spec|stories)\.[cm]?[jt]sx?$", r"\.d\.ts$", r"\.min\.[cm]?js$"],
    # `_private` names are implementation detail. Dunders are never counted: a class
    # docstring describes construction, and `__eq__` needs no prose. In TypeScript and
    # JavaScript, true also counts file-local (non-exported) functions, classes and
    # function-valued consts, and `private`/`#name` methods.
    "include_private": False,
    # A module docstring is a public symbol too: it is the first thing a reader sees.
    "count_modules": True,
    # Exported TypeScript/JavaScript declaration kinds that count.
    "javascript_kinds": ["function", "class", "method", "const", "interface", "type", "enum"],
    # Regexes. A docstring matching one is boilerplate and counts as MISSING: a generated
    # "Function header" placeholder is not documentation, and counting it as such would
    # let the number rise while nothing a reader can use was written.
    # `TODO(repolens)` (`REPOLENS_PLACEHOLDER`) is added to whatever this lists: it opens every JSDoc
    # block `repolens featuretrace propose --jsdoc` drafts, whose purpose is still unwritten.
    "placeholder_patterns": [],
    "baseline": ".repolens/docstring_baseline.json",
    "command": "repolens docs coverage",
    # Build output. Not committed: HTML regenerated from docstrings is a build product.
    "out_dir": ".repolens/docs",
    "timeout_seconds": 900,
    "python": {
        # Importable module names pdoc documents, e.g. ["mypackage"]. pdoc IMPORTS them,
        # so their dependencies must be installed for `interpreter`.
        "modules": [],
        # Repo-relative directories put on PYTHONPATH so `modules` import.
        "path": [],
        # Submodules to leave out, each with a reason in the profile's comments.
        "exclude": [],
        # Interpreter that runs pdoc (repo-relative); empty or missing = this one.
        "interpreter": "",
        "docformat": "",
        "extra_args": [],
        # A module that cannot import because a third-party package is not installed for
        # `interpreter` (an optional extra, e.g. fastapi): "exclude" leaves it out and names
        # it in the build output; "fail" fails the build. A first-party import error
        # always fails.
        "missing_dependency": "exclude",
    },
    "typescript": {
        # Repo-relative files or directories; directories are expanded.
        "entry_points": [],
        "tsconfig": "",
        # Pinned: an unpinned TypeDoc changes its output under an unchanged tree.
        "command": ["npx", "--yes", "typedoc@0.28.20"],
        # Rendering the API does not need the type-checker's verdict, and CI may not have
        # the frontend's node_modules installed.
        "extra_args": ["--skipErrorChecking"],
    },
}


@dataclass
class DocsSettings:
    """`[docs]` merged over `DEFAULTS`, with paths resolved against the repository root."""
    root: Path
    python_roots: list[str]
    javascript_roots: list[str]
    python_extensions: list[str]
    javascript_extensions: list[str]
    skip_parts: list[str]
    skip_files: tuple[re.Pattern[str], ...]
    include_private: bool
    count_modules: bool
    javascript_kinds: frozenset[str]
    placeholders: tuple[re.Pattern[str], ...]
    baseline: Path
    command: str
    out_dir: Path
    timeout_seconds: int
    python: dict[str, Any]
    typescript: dict[str, Any]

    def skipped_file(self, rel_path: str) -> bool:
        """Whether a repo-relative path matches `skip_file_patterns` and is not measured."""
        return any(p.search(rel_path) for p in self.skip_files)

    def is_placeholder(self, doc: str) -> bool:
        """Whether a docstring is generated boilerplate that counts as missing."""
        return any(p.search(doc) for p in self.placeholders)

    def python_interpreter(self) -> str:
        """The configured interpreter when it exists, else the one running repolens.

        The fallback lets a profile name a local venv that CI does not have. `docs build`
        prints the substitution, so a mistyped path is not silently papered over."""
        configured = self.python.get("interpreter") or ""
        if configured and (self.root / configured).exists():
            return str(self.root / configured)
        return sys.executable


def from_config(cfg: Config | None = None) -> DocsSettings:
    """Docs settings for `cfg`, or for the repolens.toml found from the working directory."""
    cfg = cfg if cfg is not None else load_config()
    d = merge(DEFAULTS, cfg.section("docs"))
    root = cfg.root
    return DocsSettings(
        root=root,
        python_roots=list(d["python_roots"]),
        javascript_roots=list(d["javascript_roots"]),
        python_extensions=list(d["python_extensions"]),
        javascript_extensions=list(d["javascript_extensions"]),
        skip_parts=list(d["skip_parts"]),
        skip_files=tuple(re.compile(p) for p in d["skip_file_patterns"]),
        include_private=bool(d["include_private"]),
        count_modules=bool(d["count_modules"]),
        javascript_kinds=frozenset(d["javascript_kinds"]),
        placeholders=tuple(re.compile(p) for p in dict.fromkeys([*d["placeholder_patterns"], REPOLENS_PLACEHOLDER])),
        baseline=root / d["baseline"],
        command=str(d["command"]),
        out_dir=root / d["out_dir"],
        timeout_seconds=int(d["timeout_seconds"]),
        python=dict(d["python"]),
        typescript=dict(d["typescript"]),
    )
