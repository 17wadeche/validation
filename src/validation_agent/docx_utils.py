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


def _looks_like_placeholder(text: str) -> bool:
    """Heuristically detect placeholder text that should be replaced.

    This is a fallback when color-based detection is unavailable (e.g., when
    the template strips run-level color metadata but keeps placeholder markers).
    """

    stripped = text.strip()
    if not stripped:
        return False

    if any(stripped.startswith(prefix) for prefix in ("<", "[[", "{")):
        return True

    return any(marker in stripped for marker in ("<", ">", "[[", "]]", "{", "}"))


def _color_matches_blue(color) -> bool:
    """Return True when a ``ColorFormat`` resembles the template's blue."""

    if not color:
        return False

    known_blues = {
        "0000ff",
        "1f4e79",
        "2f5496",
        "2b579a",
        "4472c4",  # Word Accent 1
        "5b9bd5",  # Word Accent 1 variant
        "0070c0",  # Word Accent 1 dark
        "0563c1",  # Alternate theme blue
    }

    rgb = getattr(color, "rgb", None)
    if rgb:
        rgb_text = str(rgb).lower()
        if rgb_text in known_blues:
            return True

    # ``val`` is used when the color comes from the theme or a direct hex code
    val = getattr(color, "val", None)
    if val and str(val).lower() in known_blues:
        return True

    try:  # pragma: no cover - depends on docx internals
        from docx.oxml.ns import qn  # type: ignore

        element = getattr(color, "_element", None)
        if element is not None:
            raw_val = element.get(qn("w:val"))
            if raw_val and raw_val.lower() in known_blues:
                return True

            theme_val = element.get(qn("w:themeColor"))
            if theme_val and "accent" in theme_val.lower():
                return True
    except Exception:
        pass

    theme_color = getattr(color, "theme_color", None)
    if theme_color:
        theme_text = str(theme_color).lower()
        if "accent" in theme_text or "blue" in theme_text:
            return True

    highlight = getattr(color, "highlight_color", None)
    if highlight:
        highlight_text = str(highlight).lower()
        if "blue" in highlight_text or "accent" in highlight_text:
            return True

    return False


def _is_blue_run(run) -> bool:
    """Detects common blue placeholder styling from templates.

    Some templates encode the blue text as RGB values, others use theme colors
    (e.g., ``ACCENT_1``), highlight colors, or styling applied at the run or
    paragraph level. We normalize all the available representations so
    placeholders in tables or styled paragraphs are still recognized.
    """

    if _color_matches_blue(getattr(run.font, "color", None)):
        return True

    try:  # pragma: no cover - optional style metadata
        run_style = getattr(run, "style", None)
        if run_style and _color_matches_blue(getattr(run_style.font, "color", None)):
            return True
    except Exception:
        pass

    try:  # pragma: no cover - paragraph styles may carry the placeholder color
        paragraph = getattr(run, "paragraph", None)
        if paragraph and _color_matches_blue(getattr(paragraph.style.font, "color", None)):
            return True
    except Exception:
        pass

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
        runs = getattr(paragraph, "runs", [])
        if any(_is_blue_run(run) for run in runs) and paragraph.text.strip():
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            continue

        if any(_looks_like_placeholder(getattr(run, "text", "")) for run in runs):
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            continue

        for placeholder in placeholders:
            if placeholder in paragraph.text:
                _replace_paragraph_with_lines(paragraph, lines)
                inserted = True
                break

    if not inserted:
        for line in lines:
            doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
