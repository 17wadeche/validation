from __future__ import annotations

from io import BytesIO
from typing import Iterable, Optional


class DocxExportError(RuntimeError):
    """Raised when a draft cannot be exported to DOCX."""


def _iter_paragraphs(document) -> Iterable:
    for paragraph in document.paragraphs:
        yield paragraph
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell)


def _is_blue_run(run) -> bool:
    color = getattr(run.font, "color", None)
    if not color:
        return False

    rgb = getattr(color, "rgb", None)
    if rgb:
        rgb_text = str(rgb).lower()
        if rgb_text in {"0000ff", "1f4e79", "2f5496"}:
            return True

    theme_color = getattr(color, "theme_color", None)
    if theme_color:
        return "accent" in str(theme_color).lower()
    return False


def _replace_paragraph_with_lines(paragraph, lines: list[str]):
    style = paragraph.style
    parent = paragraph._parent
    paragraph.text = lines[0]
    paragraph.style = style
    for line in lines[1:]:
        parent.add_paragraph(line, style=style)


def draft_to_docx_bytes(draft: str, template_bytes: Optional[bytes] = None) -> bytes:
    try:
        from docx import Document  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise DocxExportError(
            "python-docx is required to export Word files. Install with `pip install python-docx`."
        ) from exc

    if template_bytes:
        doc = Document(BytesIO(template_bytes))
    else:
        doc = Document()

    inserted = False
    placeholders = ["[[GENERATED_DRAFT]]", "<GENERATED_DRAFT>", "{GENERATED_DRAFT}"]
    lines = draft.splitlines() or [draft]

    for paragraph in _iter_paragraphs(doc):
        if any(_is_blue_run(run) for run in getattr(paragraph, "runs", [])) and paragraph.text.strip():
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            break

        for placeholder in placeholders:
            if placeholder in paragraph.text:
                _replace_paragraph_with_lines(paragraph, lines)
                inserted = True
                break
        if inserted:
            break

    if not inserted:
        for line in lines:
            doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
