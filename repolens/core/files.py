"""Walking and reading a source tree."""
from __future__ import annotations

import re
import sys
import os
from pathlib import Path, PurePosixPath
from typing import Iterable


def iter_files(
    root: Path,
    scan_dirs: Iterable[str],
    extensions: Iterable[str],
    skip_parts: Iterable[str],
    *,
    respect_gitignore: bool = False,
) -> list[Path]:
    """Every file under `scan_dirs` with a wanted suffix, sorted and de-duplicated.

    Skipped directories are matched as whole path COMPONENTS below the root. A
    substring test would skip `rebuild_ledger.py` for containing "build", and testing
    the absolute path would skip the entire repository whenever it is cloned under a
    directory that happens to be called `build`.

    With `respect_gitignore`, untracked files git ignores are dropped (one git listing,
    `core.git.untracked_ignored`); outside a git checkout, or when git cannot list them,
    every file is kept.
    """
    wanted = frozenset(extensions)
    skip = SkipRule(skip_parts)
    found: set[Path] = set()
    for scan_dir in scan_dirs:
        base = root / scan_dir
        if (not base.exists() or base.is_symlink()
                or not base.resolve().is_relative_to(root.resolve())):
            continue
        paths = []
        if base.is_file():
            paths = [base]
        else:
            for parent, directories, files in os.walk(base, followlinks=False):
                directory = Path(parent)
                directories[:] = [name for name in directories if
                                  not (directory / name).is_symlink()
                                  and not skip.matches((directory / name).relative_to(root).parts)]
                paths.extend(directory / name for name in files)
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.suffix not in wanted:
                continue
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                parts = path.parts
            if skip.matches(parts):
                continue
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            found.add(path)
    if respect_gitignore and found:
        from .git import under_ignored, untracked_ignored
        listed = untracked_ignored(root)[0]
        found = {path for path in found if not under_ignored(path.relative_to(root).as_posix(), listed)}
    return sorted(found)


class SkipRule:
    """Directory names to skip, matched against whole path components.

    An entry with a slash (`alembic/versions`) names a run of CONSECUTIVE components,
    so it skips `backend/alembic/versions/0001.py` and not a file that merely has
    `versions` somewhere in its path.
    """

    def __init__(self, entries: Iterable[str]):
        self.single: frozenset[str] = frozenset(e.strip("/") for e in entries if "/" not in e.strip("/"))
        self.runs: tuple[tuple[str, ...], ...] = tuple(
            tuple(e.strip("/").split("/")) for e in entries if "/" in e.strip("/")
        )

    def matches(self, parts: tuple[str, ...]) -> bool:
        """Whether path components `parts` contain a skipped name or a skipped run."""
        if any(part in self.single for part in parts):
            return True
        for run in self.runs:
            width = len(run)
            if any(tuple(parts[i:i + width]) == run for i in range(len(parts) - width + 1)):
                return True
        return False


def read_text(path: Path) -> str:
    """The file as UTF-8, dropping undecodable bytes. Raises OSError when it cannot be
    read at all; walkers that should carry on call `read_text_or_none` instead."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def read_text_or_none(path: Path) -> str | None:
    """`read_text`, or None (with a line on stderr) when the file cannot be read.

    A file can vanish between listing and reading: a checkout, or a generator running
    beside the scan. One such file must not crash a whole run. A caller that has to
    REPORT an unreadable file (docs coverage records it as an error) calls `read_text`
    and handles OSError itself.
    """
    try:
        return read_text(path)
    except OSError as exc:
        print(f"repolens: skipped {path}: {exc.strerror or exc}", file=sys.stderr)
        return None


#: Directory names that hold test code or fixtures, compared case-insensitively.
TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "e2e", "cypress", "fixtures", "__fixtures__", "testdata"})


def is_test_path(path: str) -> bool:
    """True for test code and fixtures: a tests/, __tests__, spec/, e2e/, cypress/ or
    fixtures directory, `test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts` or `*.spec.js`."""
    posix = PurePosixPath(path)
    if any(part.lower() in TEST_DIRS for part in posix.parts[:-1]):
        return True
    name = posix.name.lower()
    return bool(re.fullmatch(r"test_.*\.py|.*_test\.py|conftest\.py", name) or re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", name))


#: Directories whose files are test code, whatever the language.
_TEST_CODE_DIRS = frozenset({"test", "tests", "__tests__", "e2e", "cypress"})


def is_test_code(path: str) -> bool:
    """Stricter than `is_test_path`, for rules that must never exempt application code: a
    file under a tests/, test/, `__tests__`/, e2e/ or cypress/ directory, or named as a test
    (`test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts`, `*.spec.js`, `*.cy.ts`). A spec/,
    fixtures/ or testdata/ directory alone does not count: applications keep API specs and
    seed data under those names."""
    posix = PurePosixPath(path)
    if any(part.lower() in _TEST_CODE_DIRS for part in posix.parts[:-1]):
        return True
    name = posix.name.lower()
    return bool(re.fullmatch(r"test_.*\.py|.*_test\.py|conftest\.py", name)
                or re.search(r"\.(?:test|spec|cy)\.[cm]?[jt]sx?$", name))


def rel(path: Path, root: Path) -> str:
    """Repo-relative path, or the absolute one when it lies outside the repo.

    `Path.relative_to` RAISES on a path outside the root, and a display helper should
    never be the thing that fails — a baseline under a test tmpdir is outside the root.
    """
    try:
        # POSIX separators: the result keys baselines and fingerprints, which must be
        # the same on Windows as on the Linux CI runner that checks them.
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
