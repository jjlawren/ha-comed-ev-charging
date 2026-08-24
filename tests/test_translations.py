"""Guard against drift between strings.json and the shipped translations/en.json.

``strings.json`` is the source authors edit; ``translations/en.json`` is what
Home Assistant actually loads at runtime for entity names. When a key is added
to one but not the other, entities lose their translated name and Home Assistant
cannot build the expected ``sensor.<device>_<key>`` entity id, which silently
breaks anything (like the bundled cards) that references it.
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "comed_ev"
_STRINGS = _ROOT / "strings.json"
_EN = _ROOT / "translations" / "en.json"


def _entity_names(path: Path) -> dict[str, str]:
    """Flatten the ``entity`` block to ``{platform.key: name}``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for platform, keys in data.get("entity", {}).items():
        for key, spec in keys.items():
            names[f"{platform}.{key}"] = spec.get("name", "")
    return names


def test_entity_translation_keys_match() -> None:
    """Every entity name in strings.json must ship identically in en.json."""
    strings = _entity_names(_STRINGS)
    en = _entity_names(_EN)

    missing = sorted(set(strings) - set(en))
    extra = sorted(set(en) - set(strings))
    mismatched = sorted(
        k for k in strings.keys() & en.keys() if strings[k] != en[k]
    )

    assert not missing, f"in strings.json but missing from en.json: {missing}"
    assert not extra, f"in en.json but missing from strings.json: {extra}"
    assert not mismatched, f"name differs between strings.json and en.json: {mismatched}"
