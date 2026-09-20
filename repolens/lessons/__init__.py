"""The technical lessons-learnt catalogue, shipped as package data.

`catalogue.json` is the canonical file and the one to edit; `docs/lessons/` holds the
browsable page and the generated rule packs, and `docs/lessons/export_lessons_learnt.py`
regenerates them from here.
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

CATALOGUE = "catalogue.json"


def load() -> dict[str, Any]:
    """The catalogue as shipped. Works from a checkout and from a wheel."""
    return json.loads(resources.files(__package__).joinpath(CATALOGUE).read_text(encoding="utf-8"))
