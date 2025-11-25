from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
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


def extract_placeholders(template: str) -> List[str]:
    """Capture inline placeholder tokens from the template text."""

    pattern = re.compile(r"(<[^>]+>|\[\[[^\]]+\]\]|\{[^}]+\}|_{3,})")
    found = set(match.strip() for match in pattern.findall(template))
    return sorted(found)


def build_planning_prompt(template: str, examples: Iterable[Example], code_context: str) -> str:
    """Build a planning prompt that inventories placeholders and open questions."""

    prompt_sections = [
        "You are a Medtronic validation planning assistant.",
        "Inspect the template and summarize what must be filled before drafting.",
        "Return JSON only with three keys: {\n  \"needs\": [ {\"token\": str, \"description\": str, \"where\": str} ],\n  \"clarifying_questions\": [str],\n  \"drafting_steps\": [ {\"section\": str, \"tokens\": [str], \"notes\": str} ]\n}.",
        "Do not generate the draft itself—focus on the fill plan and concise questions to unblock drafting.",
    ]

    prompt_sections.append("\n## Template\n" + template.strip())

    placeholders = extract_placeholders(template)
    if placeholders:
        prompt_sections.append(
            "\n## Detected placeholders\n"
            + "\n".join(f"- {token}" for token in placeholders)
        )

    formatted_examples = format_examples(examples)
    if formatted_examples:
        prompt_sections.append("\n## Reference examples\n" + formatted_examples)

    prompt_sections.append(
        "\n## Program code context\n"
        "Use the following code snapshot for grounding; flag any missing pieces that prevent filling the template.\n"
        + code_context.strip()
    )

    prompt_sections.append(
        "\n## Response rules\n"
        "- Output valid JSON only (no code fences).\n"
        "- Keep questions brief and tied to specific tokens or sections.\n"
        "- Suggest drafting steps that map tokens to template sections so a follow-on agent can place them precisely.\n"
    )

    return "\n\n".join(prompt_sections).strip() + "\n"


def build_prompt(
    template: str, examples: Iterable[Example], code_context: str, plan_context: str | None = None
) -> str:
    prompt_sections = [
        "You are an AI assistant that drafts Medtronic validation documentation.",
        "Follow the provided template exactly, replacing placeholders (highlighted in blue in the template) with project-specific content while preserving every heading, bullet, table, and piece of surrounding text.",
        "Use the program code context to ground statements and avoid inventing functionality.",
        "Fill every placeholder you can from the provided information before asking for anything else—only ask clarifying questions for the gaps that remain.",
    ]

    prompt_sections.append("\n## Template\n" + template.strip())

    placeholders = extract_placeholders(template)
    if placeholders:
        prompt_sections.append(
            "\n## Template placeholders to map\n"
            "Return structured JSON only, as: {\n"
            "  \"placeholders\": {<token>: <replacement>, ...},\n"
            "  \"answers\": [ {\n"
            "    \"placeholder\": <token or exact blue/instructional text>,\n"
            "    \"replacement\": <what to put there (or 'delete' to remove)>,\n"
            "    \"where\": \"describe the section/table cell this belongs in\"\n"
            "  } ],\n"
            "  \"questions\": [optional, only for items still unknown]\n"
            "}.\n"
            "- Map every placeholder you can infer, including long blue instructional sentences (e.g., the sample purpose statements).\n"
            "- Do NOT embed the full draft; just provide the mappings and any necessary questions.\n"
            "- Keep labels, bullets, and table headers unchanged—only tell the user what to type in place of the placeholder.\n"
            + "\n".join(f"- {token}" for token in placeholders)
        )

    formatted_examples = format_examples(examples)
    if formatted_examples:
        prompt_sections.append("\n## Reference examples\n" + formatted_examples)

    if plan_context and plan_context.strip():
        prompt_sections.append(
            "\n## Drafting plan\n"
            "Use this plan when deciding where to place generated content. If details are missing, ask clarifying questions before replacing placeholders.\n"
            + plan_context.strip()
        )

    prompt_sections.append(
        "\n## Program code context\n"
        "Use the following code snapshot to ground the validation description."
        " Focus on behaviors, data flows, risk mitigations, and control mechanisms visible in the code.\n"
        + code_context.strip()
    )

    prompt_sections.append(
        "\n## Response rules\n"
        "- Preserve the template's structure conceptually; do not rewrite sections—just tell the user what to type.\n"
        "- Always include your best-effort `placeholders` map and `answers` list even if some items are blank; add `questions` only for the missing pieces.\n"
        "- Do not provide the full draft text; focus on explicit mappings from template text (blue instructions or <tokens>) to replacements.\n"
        "- Maintain clear traceability to the template placeholders and avoid inventing functionality not evidenced in the code context.\n"
        "- Never fabricate person names or signatures; leave them blank or ask a question when not provided."
    )

    return "\n\n".join(prompt_sections).strip() + "\n"


def build_update_prompt(
    template: str,
    examples: Iterable[Example],
    code_context: str,
    prior_json: str,
    answered: list[tuple[str, str]] | None = None,
    plan_context: str | None = None,
) -> str:
    """Ask the model to merge new answers into an existing mapping JSON."""

    answered = answered or []

    prompt_sections = [
        "You are an AI assistant that updates Medtronic validation mappings.",
        "You will be given the template, prior JSON mappings (placeholders/answers/questions), and new user-provided answers.",
        "Merge them into an updated JSON while preserving existing values unless they conflict with the new answers.",
        "Return JSON only with the keys: placeholders, answers (list of {question, answer, placeholder?}), and questions (still-open items).",
        "Fill every placeholder you can infer from the new answers, examples, template cues, and code context; keep unknowns blank and list them in questions.",
        "Never fabricate person names or signatures—leave them blank or move them to questions when missing.",
    ]

    prompt_sections.append("\n## Template\n" + template.strip())

    placeholders = extract_placeholders(template)
    if placeholders:
        prompt_sections.append(
            "\n## Template placeholders to map\n" + "\n".join(f"- {token}" for token in placeholders)
        )

    formatted_examples = format_examples(examples)
    if formatted_examples:
        prompt_sections.append("\n## Reference examples\n" + formatted_examples)

    if plan_context and plan_context.strip():
        prompt_sections.append(
            "\n## Drafting plan\n"
            "Use this plan when deciding where to place generated content.\n"
            + plan_context.strip()
        )

    prompt_sections.append(
        "\n## Program code context\n"
        "Ground replacements in the provided code details.\n"
        + code_context.strip()
    )

    prompt_sections.append(
        "\n## Prior JSON mapping\n"
        "Update this JSON instead of starting over. Keep existing answers unless replaced by new input.\n"
        + prior_json.strip()
    )

    if answered:
        answered_lines = [f"- Q: {q}\n  A: {a}" for q, a in answered if q or a]
        prompt_sections.append(
            "\n## New answers to merge\n" + "\n".join(answered_lines)
        )

    prompt_sections.append(
        "\n## Response rules\n"
        "- Output valid JSON only (no code fences).\n"
        "- Return placeholders map plus answers list and remaining questions.\n"
        "- Reuse prior values when still valid; add new inferred placeholder fills based on the answers and template cues.\n"
        "- Do not invent person names or signatures; leave them blank or add a clarifying question.\n"
        "- If a placeholder appears multiple times, ensure the same value is reused consistently.\n"
    )

    return "\n\n".join(prompt_sections).strip() + "\n"
