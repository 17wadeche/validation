from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable, Optional

try:  # pragma: no cover - optional dependency
    from docx.enum.text import WD_BREAK  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    WD_BREAK = None


class DocxExportError(RuntimeError):
    """Raised when a draft cannot be exported to DOCX."""


def _iter_paragraphs(document, include_headers_footers: bool = False) -> Iterable:
    """Yield paragraphs across the document, optionally headers/footers."""

    for paragraph in document.paragraphs:
        yield paragraph

    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell, include_headers_footers=include_headers_footers)

    if not include_headers_footers:
        return

    for section in getattr(document, "sections", []) or []:
        for hdr in (section.header, section.first_page_header, section.even_page_header):
            if hdr:
                yield from _iter_paragraphs(hdr, include_headers_footers=include_headers_footers)
        for ftr in (section.footer, section.first_page_footer, section.even_page_footer):
            if ftr:
                yield from _iter_paragraphs(ftr, include_headers_footers=include_headers_footers)


KNOWN_PLACEHOLDER_SNIPPETS = [
    "assurance (standard deliverables) sample purpose statement",
    "validation (enhanced deliverables) sample purpose statement",
    "the purpose of this document is to record the quality assurance activities",
]


def _matches_known_placeholder(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False

    return any(snippet in lowered for snippet in KNOWN_PLACEHOLDER_SNIPPETS)


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

    if any(marker in stripped for marker in ("<", ">", "[[", "]]", "{", "}")):
        return True

    return _matches_known_placeholder(stripped)


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


def _is_placeholder_run(run) -> bool:
    """Detect placeholder runs using color, style, or instructional text."""

    text = getattr(run, "text", "") or ""
    stripped = text.strip()

    if not stripped:
        return False

    if _is_blue_run(run):
        return True

    style = getattr(run, "style", None)
    try:  # pragma: no cover - style lookups depend on template metadata
        style_name = getattr(style, "name", "") or ""
        if "placeholder" in style_name.lower():
            return True
    except Exception:
        pass

    lowered = stripped.lower()
    if any(marker in lowered for marker in ("fill", "replace", "insert", "enter", "provided by")):
        return True

    if any(token in stripped for token in ("<", ">", "[[", "]]", "{", "}", "___")):
        return True

    if _matches_known_placeholder(stripped):
        return True

    if stripped.isupper() and len(stripped) > 6:
        return True

    return False


def _paragraph_placeholder_score(paragraph) -> int:
    """Score how placeholder-like a paragraph is.

    This helps catch template placeholders that are split across runs or rely
    on paragraph-level styling instead of run-level color metadata.
    """

    score = 0

    try:
        if _color_matches_blue(getattr(paragraph.style.font, "color", None)):
            score += 2
    except Exception:
        pass

    runs = getattr(paragraph, "runs", []) or []
    if not runs:
        return score

    for run in runs:
        if _is_placeholder_run(run):
            score += 3
        elif _is_blue_run(run):
            score += 2

    text = (paragraph.text or "").strip()
    if text and _looks_instructional(text):
        score += 1

    if _looks_like_placeholder(text):
        score += 2

    if _matches_known_placeholder(text):
        score += 3

    return score


def _looks_instructional(text: str) -> bool:
    """Detect paragraphs that read like instructions even without markers/colors."""

    lowered = text.strip().lower()
    if not lowered:
        return False

    phrases = (
        "enter ",
        "describe ",
        "provide ",
        "summarize ",
        "explain ",
        "insert ",
        "complete ",
    )

    return any(lowered.startswith(p) or f" {p}" in lowered for p in phrases)


def _replace_paragraph_with_lines(paragraph, lines: list[str]):
    style = paragraph.style
    parent = paragraph._parent
    paragraph.text = lines[0]
    paragraph.style = style
    for line in lines[1:]:
        parent.add_paragraph(line, style=style)


def _replace_run_with_draft(run, draft: str):
    run.text = ""
    parts = draft.splitlines() or [draft]
    for idx, part in enumerate(parts):
        if idx and WD_BREAK:
            try:  # pragma: no cover - relies on optional enum
                run.add_break(WD_BREAK.LINE)
            except Exception:
                run.add_text("\n")
        elif idx:
            run.add_text("\n")
        run.add_text(part)


def _replace_tokens_in_run(run, draft: str) -> bool:
    """Replace inline placeholder tokens inside a run while preserving styling."""

    text = getattr(run, "text", "") or ""
    if not text:
        return False

    pattern = re.compile(r"(<[^>]+>|\[\[[^\]]+\]\]|\{[^}]+\}|_{3,})")
    if not pattern.search(text):
        return False

    replaced = pattern.sub(draft, text)
    if replaced != text:
        run.text = replaced
        return True

    return False


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

    candidate_paragraph = None
    best_scored_paragraph = None
    best_score = 0

    for paragraph in _iter_paragraphs(doc):
        runs = list(getattr(paragraph, "runs", []))
        score = _paragraph_placeholder_score(paragraph)
        if score > best_score:
            best_score = score
            best_scored_paragraph = paragraph

        if _matches_known_placeholder(paragraph.text or ""):
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            continue

        for idx, run in enumerate(runs):
            if _replace_tokens_in_run(run, draft):
                inserted = True
                break

            if _is_placeholder_run(run):
                _replace_run_with_draft(run, draft)
                for follower in runs[idx + 1 :]:
                    if _is_placeholder_run(follower):
                        follower.text = ""
                    else:
                        break
                inserted = True
                break

        if inserted:
            continue

        if any(_looks_like_placeholder(getattr(run, "text", "")) for run in runs):
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            continue

        if _looks_like_placeholder(paragraph.text):
            _replace_paragraph_with_lines(paragraph, lines)
            inserted = True
            continue

        if not candidate_paragraph and _looks_instructional(paragraph.text):
            candidate_paragraph = paragraph

        for placeholder in placeholders:
            if placeholder in paragraph.text:
                _replace_paragraph_with_lines(paragraph, lines)
                inserted = True
                break

    if not inserted and candidate_paragraph:
        _replace_paragraph_with_lines(candidate_paragraph, lines)
        inserted = True

    if not inserted and best_scored_paragraph and best_score >= 3:
        _replace_paragraph_with_lines(best_scored_paragraph, lines)
        inserted = True

    if not inserted:
        for line in lines:
            doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def docx_bytes_to_html(docx_bytes: bytes) -> Optional[str]:
    """Render DOCX bytes to HTML for on-screen preview, if mammoth is installed."""

    try:  # pragma: no cover - optional dependency
        import mammoth  # type: ignore
    except Exception:
        return None

    try:
        result = mammoth.convert_to_html(BytesIO(docx_bytes), style_map="p[style-name='Normal'] => p")
    except Exception:
        return None

    return result.value if result else None
