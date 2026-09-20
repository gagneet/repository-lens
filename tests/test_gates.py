from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from repolens.gates.reachability import can_fail


class CanFailTests(unittest.TestCase):
    def check(self, name: str, text: str) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(text)
            return can_fail(SimpleNamespace(gate_flags=("--check",)), path)

    def test_each_language_spells_failure_its_own_way(self):
        for name, text in [("a.sh", "#!/bin/bash\n[ -f x ] || exit 1\n"),
                           ("b.sh", "#!/usr/bin/env bash\nset -euo pipefail\npsql -f x.sql\n"),
                           ("c.sh", "run || exit \"$rc\"\n"),
                           ("d.sql", "DO $$ BEGIN\n  RAISE EXCEPTION 'breach';\nEND $$;\n"),
                           ("e.py", "import sys\nif bad:\n    sys.exit(2)\n"),
                           ("f.py", "raise SystemExit(main())\n"),
                           ("g.py", "def main():\n    return 1\n")]:
            with self.subTest(name):
                self.assertTrue(self.check(name, text))

    def test_success_exits_and_comments_are_not_failure(self):
        for name, text in [("a.sh", "#!/bin/bash\n# exit 1 on error\n# set -e\necho done\nexit 0\n"),
                           ("b.sql", "-- RAISE EXCEPTION here if it breaks\nSELECT 1;\n"),
                           ("c.py", "import sys\nsys.exit(0)\n"),
                           ("d.sql", "RAISE NOTICE 'ok';\n")]:
            with self.subTest(name):
                self.assertFalse(self.check(name, text))


if __name__ == "__main__":
    unittest.main()
