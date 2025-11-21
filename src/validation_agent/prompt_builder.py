from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from .document_loader import load_text_document


@dataclass
class Example:
    """Represents an example validation document."""

    title: str
    context: str
    output: str
def load_examples(path: Path) -> List[Example]:
    import json

    if path.is_dir():
        return _load_examples_from_directory(path)

    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        examples = []
        for item in data:
            examples.append(
                Example(
                    title=item["title"],
                    context=item.get("context", "").strip(),
                    output=item["output"].strip(),
                )
            )
        return examples

    if suffix in {".md", ".txt", ".markdown", ".docx", ".pdf"}:
        content = load_text_document(path)
        return [Example(title=path.stem, context="", output=content)]

    raise ValueError(f"Unsupported examples source: {path}")


def _load_examples_from_directory(path: Path) -> List[Example]:
    examples: List[Example] = []
    for doc in sorted(path.iterdir()):
        if not doc.is_file():
            continue
        suffix = doc.suffix.lower()
        if suffix in {".json"}:
            examples.extend(load_examples(doc))
        elif suffix in {".md", ".txt", ".markdown", ".docx", ".pdf"}:
            content = load_text_document(doc)
            examples.append(Example(title=doc.stem, context="", output=content))
    return examples


def format_examples(examples: Iterable[Example]) -> str:
    lines = []
    for example in examples:
        lines.append(f"### Example: {example.title}\n")
        lines.append("Context:\n")
        lines.append(example.context)
        lines.append("\nExpected Output:\n")
        lines.append(example.output)
        lines.append("\n---\n")
    return "\n".join(lines).strip()


def build_prompt(template: str, examples: Iterable[Example], code_context: str) -> str:
    prompt_sections = [
        "You are an AI assistant that drafts Medtronic validation documentation.",
        "Follow the provided template exactly, replacing placeholders (highlighted in blue in the template) with project-specific content while preserving every heading, bullet, table, and piece of surrounding text.",
        "Use the program code context to ground statements and avoid inventing functionality.",
        "If any placeholder cannot be confidently completed from the provided materials, ask concise clarifying questions before delivering the draft.",
    ]

    prompt_sections.append("\n## Template\n" + template.strip())

    formatted_examples = format_examples(examples)
    if formatted_examples:
        prompt_sections.append("\n## Reference examples\n" + formatted_examples)

    prompt_sections.append(
        "\n## Program code context\n"
        "Use the following code snapshot to ground the validation description."
        " Focus on behaviors, data flows, risk mitigations, and control mechanisms visible in the code.\n"
        + code_context.strip()
    )

    prompt_sections.append(
        "\n## Response rules\n"
        "- Preserve the template's structure and formatting exactly; replace only the placeholder text while keeping all other content unchanged.\n"
        "- If clarification is required, respond with concise follow-up questions instead of guessing.\n"
        "- When information is sufficient, return only the completed validation draft (no prompt restatement, commentary, or code fences).\n"
        "- Maintain clear traceability to the template placeholders and avoid inventing functionality not evidenced in the code context."
    )

    return "\n\n".join(prompt_sections).strip() + "\n"
