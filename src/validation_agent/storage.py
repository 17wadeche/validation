from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

DEFAULT_INPUT_STORE = Path.home() / ".validation_agent" / "inputs.json"


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
    template: Optional[StoredFile] = None
    examples: List[StoredFile] = field(default_factory=list)


def load_saved_inputs(store: Path = DEFAULT_INPUT_STORE) -> SavedInputs:
    if not store.exists():
        return SavedInputs()
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
        template = None
        if raw.get("template"):
            template = StoredFile(**raw["template"])
        examples = [StoredFile(**item) for item in raw.get("examples", [])]
        return SavedInputs(template=template, examples=examples)
    except Exception:
        return SavedInputs()


def save_inputs(saved: SavedInputs, store: Path = DEFAULT_INPUT_STORE) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(saved)
    store.write_text(json.dumps(payload, indent=2), encoding="utf-8")
