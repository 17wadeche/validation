# launcher.py
from __future__ import annotations
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
APP_DIR_NAME = "MedtronicValidationTool"
LATEST_URL = "https://your-server/validation-ui/latest.json"  # <-- change
APP_EXE_BASENAME = "validation-ui.exe"  # what we call it locally
def _get_app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME
def _read_local_version(app_dir: Path) -> str:
    ver_file = app_dir / "version.txt"
    if not ver_file.exists():
        return ""
    try:
        return ver_file.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
def _write_local_version(app_dir: Path, version: str) -> None:
    ver_file = app_dir / "version.txt"
    app_dir.mkdir(parents=True, exist_ok=True)
    ver_file.write_text(version.strip(), encoding="utf-8")
def _download(url: str) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="validation_update_"))
    target = tmp_dir / "downloaded.exe"
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    target.write_bytes(data)
    return target
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
def _fetch_latest_info() -> dict | None:
    try:
        with urllib.request.urlopen(LATEST_URL) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None
def _version_newer(v1: str, v2: str) -> bool:
    def parse(v: str):
        return [int(p) for p in v.split(".") if p.isdigit()]
    return parse(v1) > parse(v2)
def ensure_latest(app_dir: Path) -> Path:
    app_dir.mkdir(parents=True, exist_ok=True)
    exe_path = app_dir / APP_EXE_BASENAME
    local_version = _read_local_version(app_dir)
    info = _fetch_latest_info()
    if not info or "version" not in info or "url" not in info:
        return exe_path
    remote_version = str(info["version"]).strip()
    url = str(info["url"]).strip()
    expected_sha = str(info.get("sha256", "")).strip().lower()
    needs_update = not exe_path.exists() or not local_version or _version_newer(remote_version, local_version)
    if not needs_update:
        return exe_path
    try:
        downloaded = _download(url)
    except Exception:
        return exe_path if exe_path.exists() else downloaded
    if expected_sha:
        actual_sha = _sha256(downloaded).lower()
        if actual_sha != expected_sha:
            return exe_path if exe_path.exists() else downloaded
    try:
        if exe_path.exists():
            exe_path.unlink()
        downloaded.rename(exe_path)
        _write_local_version(app_dir, remote_version)
    except Exception:
        return exe_path if exe_path.exists() else downloaded
    return exe_path
def main() -> None:
    app_dir = _get_app_dir()
    exe_path = ensure_latest(app_dir)
    if not exe_path.exists():
        import tkinter.messagebox as mbox
        mbox.showerror("Medtronic Validation Tool",
                       "Could not download or locate the validation app.\n"
                       "Please contact support.")
        sys.exit(1)
    args = [str(exe_path)] + sys.argv[1:]
    subprocess.Popen(args, cwd=str(app_dir))
    sys.exit(0)
if __name__ == "__main__":
    main()