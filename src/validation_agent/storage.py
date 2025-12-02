from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

DEFAULT_INPUT_STORE = Path.home() / ".validation_agent" / "inputs.json"
# If the primary location is unavailable (e.g., locked-down home directory in a
# packaged build), fall back to a project-local store so uploads persist.
FALLBACK_INPUT_STORE = Path(".validation_agent") / "inputs.json"


@dataclass
class StoredFile:
    name: str
    b64: str
    suffix: str = ""

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> "StoredFile":
        suffix = Path(name).suffix
        return cls(name=name, b64=base64.b64encode(data).decode("utf-8"), suffix=suffix)

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.b64)


@dataclass
class SavedInputs:
    templates: List[StoredFile] = field(default_factory=list)
    examples: List[StoredFile] = field(default_factory=list)


def _load_from_path(store: Path) -> SavedInputs:
    if not store.exists():
        return SavedInputs()
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
        templates: List[StoredFile] = []
        if raw.get("templates"):
            templates = [StoredFile(**item) for item in raw.get("templates", [])]
        elif raw.get("template"):
            templates = [StoredFile(**raw["template"])]

        unique_templates: List[StoredFile] = []
        seen = set()
        for tmpl in templates:
            if tmpl.name in seen:
                continue
            seen.add(tmpl.name)
            unique_templates.append(tmpl)
        templates = unique_templates
        examples_raw = [StoredFile(**item) for item in raw.get("examples", [])]
        examples: List[StoredFile] = []
        seen_examples = set()
        for ex in examples_raw:
            if ex.name in seen_examples:
                continue
            seen_examples.add(ex.name)
            examples.append(ex)
        return SavedInputs(templates=templates, examples=examples)
    except Exception:
        return SavedInputs()


def load_saved_inputs(store: Path = DEFAULT_INPUT_STORE) -> SavedInputs:
    """Load persisted inputs, falling back to a local store if needed."""

    primary = _load_from_path(store)
    if primary.templates or primary.examples or store.exists():
        return primary

    # If the primary store is empty or unavailable, try the fallback.
    return _load_from_path(FALLBACK_INPUT_STORE)


def save_inputs(saved: SavedInputs, store: Path = DEFAULT_INPUT_STORE) -> None:
    payload = asdict(saved)
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return
    except Exception:
        # Fall back to a local project directory so inputs persist even when
        # the home directory is locked down (e.g., packaged EXE on Windows).
        fallback = FALLBACK_INPUT_STORE
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(json.dumps(payload, indent=2), encoding="utf-8")
