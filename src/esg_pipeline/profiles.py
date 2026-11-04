"""Versioned, user-replaceable profiles; not the undelivered 179-variable dictionary."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[2]


def load_profile(path=None):
    path = Path(path) if path else ROOT / "config/water_profile.json"
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    if not profile.get("version") or not profile.get("fields") or profile.get("id") != "water_v1":
        raise ValueError("PROFILE_INVALID")
    for field, spec in profile["fields"].items():
        if not isinstance(field, str) or not spec.get("definition") or not spec.get("unit"):
            raise ValueError("PROFILE_FIELD_INVALID")
        for pattern in spec.get("patterns", []):
            re.compile(pattern, re.I)
    return profile


def matches(label, profile):
    return [field for field, spec in profile["fields"].items()
            if any(re.search(p, label, re.I) for p in spec.get("patterns", []))]
