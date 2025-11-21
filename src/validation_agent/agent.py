from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from .prompt_builder import Example, build_prompt, load_examples, read_text

LLMCallable = Callable[[str], str]


def load_code_context(paths: Iterable[Path], max_chars: int = 12000) -> str:
    parts = []
    total = 0
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    snippet = _read_snippet(child, max_chars - total)
                    total += len(snippet)
                    parts.append(f"\n# File: {child}\n{snippet}\n")
        elif path.is_file():
            snippet = _read_snippet(path, max_chars - total)
            total += len(snippet)
            parts.append(f"\n# File: {path}\n{snippet}\n")
        if total >= max_chars:
            break
    return "".join(parts).strip()


def _read_snippet(path: Path, remaining: int) -> str:
    if remaining <= 0:
        return ""
    return path.read_text(encoding="utf-8")[:remaining]


class ValidationAgent:
    def __init__(self, llm_callable: Optional[LLMCallable] = None) -> None:
        self.llm_callable = llm_callable

    def create_prompt(
        self,
        template_path: Path,
        requirements_path: Path,
        examples_path: Path,
        code_paths: Iterable[Path],
    ) -> str:
        template = read_text(template_path)
        requirements = read_text(requirements_path)
        examples = load_examples(examples_path)
        code_context = load_code_context(code_paths)
        return build_prompt(template, requirements, examples, code_context)

    def generate_draft(
        self,
        template_path: Path,
        requirements_path: Path,
        examples_path: Path,
        code_paths: Iterable[Path],
    ) -> str:
        prompt = self.create_prompt(template_path, requirements_path, examples_path, code_paths)
        if self.llm_callable:
            return self.llm_callable(prompt)
        return prompt
