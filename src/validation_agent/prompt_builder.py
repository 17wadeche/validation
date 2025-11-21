from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


@dataclass
class Example:
    """Represents an example validation document."""

    title: str
    context: str
    output: str


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_examples(path: Path) -> List[Example]:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    examples = []
    for item in data:
        examples.append(
            Example(
                title=item["title"],
                context=item["context"].strip(),
                output=item["output"].strip(),
            )
        )
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


def build_prompt(template: str, requirements: str, examples: Iterable[Example], code_context: str) -> str:
    prompt_sections = [
        "You are an AI assistant that drafts Medtronic validation documentation.",
        "Follow the provided template exactly, replacing placeholders with project-specific content.",
        "Incorporate the requirements and reflect the provided program code context.",
    ]

    prompt_sections.append("\n## Template\n" + template.strip())
    prompt_sections.append("\n## Requirements and guidance\n" + requirements.strip())

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
        "\n## Expected output\n"
        "Produce a complete validation draft ready for compliance review."
        " Maintain clear traceability to requirements, articulate test rationale,"
        " and avoid inventing functionality that is not evidenced in the code context."
    )

    return "\n\n".join(prompt_sections).strip() + "\n"
