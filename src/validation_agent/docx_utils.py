from __future__ import annotations

from io import BytesIO


class DocxExportError(RuntimeError):
    """Raised when a draft cannot be exported to DOCX."""


def draft_to_docx_bytes(draft: str) -> bytes:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise DocxExportError(
            "python-docx is required to export Word files. Install with `pip install python-docx`."
        ) from exc

    doc = Document()
    for line in draft.splitlines() or [draft]:
        # Preserve blank lines for readability
        doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
