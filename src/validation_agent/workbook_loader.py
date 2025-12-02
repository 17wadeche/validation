# src/validation_agent/workbook_loader.py
from __future__ import annotations
from pathlib import Path
from typing import List
import textwrap
def extract_excel_context(path: Path, max_chars: int = 12000) -> str:
    suffix = path.suffix.lower()
    if suffix not in {".xlsm", ".xls", ".xlsx", ".xlsb"}:
        raise ValueError(f"Not an Excel workbook: {path}")
    parts: List[str] = [f"# Excel workbook: {path.name}"]
    try:
        from openpyxl import load_workbook  # type: ignore
        wb = load_workbook(filename=str(path), data_only=False, keep_links=True)
        sheet_names = list(wb.sheetnames)
        parts.append("## Sheets")
        for s in sheet_names:
            parts.append(f"- {s}")
        if wb.defined_names.definedName:
            parts.append("\n## Named ranges")
            for dn in wb.defined_names.definedName:
                parts.append(f"- {dn.name}: {dn.attr_text}")
    except Exception:
        parts.append("\n[Could not read workbook structure via openpyxl.]")
    try:
        from oletools.olevba import VBA_Parser  # type: ignore
        vba = VBA_Parser(str(path))
        if vba.detect_vba_macros():
            parts.append("\n## VBA macros")
            for (_, _, vba_filename, vba_code) in vba.extract_all_macros():
                if not vba_code:
                    continue
                snippet = vba_code[: max_chars // 3]
                parts.append(
                    f"\n### Module: {vba_filename}\n"
                    + "```vba\n"
                    + snippet
                    + "\n```"
                )
        vba.close()
    except Exception:
        parts.append("\n[No VBA macros extracted or oletools not installed.]")
    text = "\n".join(parts).strip()
    return text[:max_chars]
def extract_pbix_context(path: Path, max_chars: int = 12000) -> str:
    suffix = path.suffix.lower()
    if suffix != ".pbix":
        raise ValueError(f"Not a PBIX file: {path}")
    base = f"# Power BI PBIX: {path.name}\n"
    details = "[PBIX extraction not yet implemented – plug in pbi-tools or your internal extractor here.]"
    text = base + details
    return text[:max_chars]
