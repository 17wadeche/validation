"""Simple PyInstaller wrapper to bundle the Medtronic validation UI into a Windows-friendly EXE.

Usage (from repo root on Windows):
    py -m venv .venv
    .venv\\Scripts\\activate
    pip install -r requirements.txt pyinstaller
    py packaging\\build_exe.py

Result: dist\\validation-ui.exe (double-click to run; opens http://127.0.0.1:8000)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from PyInstaller.__main__ import run as pyinstaller_run
except ImportError:  # pragma: no cover
    raise SystemExit("PyInstaller is required. Install it with `pip install pyinstaller`.")

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "webapp.py"


def main() -> None:
    os.chdir(ROOT)
    opts = [
        "--onefile",
        "--noconsole",
        "--name",
        "validation-ui",
        "--add-data",
        f"{ROOT / 'src'}{os.pathsep}src",
        str(ENTRYPOINT),
    ]
    pyinstaller_run(opts)


if __name__ == "__main__":
    main()
