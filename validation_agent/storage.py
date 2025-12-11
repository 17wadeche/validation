from __future__ import annotations
import base64
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_ROOT = BASE_DIR / "user_data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
def _inputs_path_for_user(user_id: str) -> Path:
    user_dir = DATA_ROOT / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / "inputs.json"
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
def load_saved_inputs(user_id: str) -> SavedInputs:
    store = _inputs_path_for_user(user_id)
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
def save_inputs(saved: SavedInputs, user_id: str) -> None:
    store = _inputs_path_for_user(user_id)
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(saved)
    store.write_text(json.dumps(payload, indent=2), encoding="utf-8")