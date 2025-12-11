from dataclasses import dataclass, asdict
from pathlib import Path
import json
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_ROOT = BASE_DIR / "user_data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
def _creds_path_for_user(user_id: str) -> Path:
    user_dir = DATA_ROOT / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / "credentials.json"
@dataclass
class StoredCredentials:
    subscription_key: str = ""
    api_token: str = ""
    refresh_token: str = ""
    api_version: str = ""
    base_url: str = ""
    path_template: str = ""
    model: str = ""
def load_credentials(user_id: str) -> StoredCredentials:
    path = _creds_path_for_user(user_id)
    if not path.exists():
        return StoredCredentials()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return StoredCredentials()
    return StoredCredentials(**data)
def save_credentials(creds: StoredCredentials, user_id: str) -> None:
    path = _creds_path_for_user(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(creds), indent=2), encoding="utf-8")