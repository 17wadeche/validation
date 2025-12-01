from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from .document_loader import load_text_document
from .prompt_builder import Example, build_planning_prompt, build_prompt, load_examples

LLMCallable = Callable[[str], str]


def load_code_context(
    paths: Iterable[Path],
    max_chars: int = 20000,
    per_file_limit: int = 1200,
    manifest_limit: int = 120,
) -> str:
    """Return a text snapshot with prioritized files and a manifest.

    The snapshot favors READMEs, requirements/metadata, and source files, then
    falls back to other text artifacts. Only common text/code extensions are
    considered to avoid binary noise. A short manifest of discovered files is
    included to help the LLM reason about unseen sections.
    """

    candidates: list[tuple[int, Path]] = []
    manifest_paths: list[str] = []
    seen: set[Path] = set()

    for raw in paths:
        root = raw.resolve()
        if root.is_file():
            _maybe_collect(root, candidates, seen)
        elif root.is_dir():
            for child in sorted(root.rglob("*")):
                if child.is_file():
                    _maybe_collect(child.resolve(), candidates, seen)

    # Build manifest of first N text files for quick coverage hints.
    for _, path in candidates[:manifest_limit]:
        manifest_paths.append(str(path))

    parts = []
    if manifest_paths:
        parts.append("# File manifest (truncated)\n" + "\n".join(f"- {p}" for p in manifest_paths) + "\n")

    total = len(parts[0]) if parts else 0
    for priority, path in candidates:
        if total >= max_chars:
            break
        remaining = max_chars - total
        snippet = _read_snippet(path, per_file_limit, remaining)
        if not snippet:
            continue
        block = f"\n# File: {path}\n{snippet}\n"
        block_len = len(block)
        if total + block_len > max_chars:
            block = block[: max_chars - total]
        parts.append(block)
        total += len(block)

    return "".join(parts).strip()


def _maybe_collect(path: Path, bucket: list[tuple[int, Path]], seen: set[Path]) -> None:
    if path in seen:
        return
    if not path.is_file():
        return
    suffix = path.suffix.lower()
    if suffix not in _ALLOWED_TEXT_SUFFIXES:
        return
    seen.add(path)
    bucket.append((_priority_for_path(path), path))
    bucket.sort(key=lambda item: (item[0], str(item[1]).lower()))


def _priority_for_path(path: Path) -> int:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name.startswith("readme"):
        return 0
    if name in {"requirements.txt", "package.json", "pyproject.toml", "setup.py"}:
        return 1
    if suffix in {".md", ".markdown", ".txt", ".rst"}:
        return 2
    if suffix in {".yaml", ".yml", ".json", ".ini", ".toml", ".cfg", ".conf", ".xml"}:
        return 3
    # Source files next.
    if suffix in {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".go",
        ".rb",
        ".rs",
        ".cs",
        ".php",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".swift",
        ".kt",
        ".m",
        ".scala",
    }:
        return 4
    # Shell, SQL, configs that may describe flows.
    if suffix in {".sh", ".ps1", ".bat", ".sql"}:
        return 5
    return 6


_ALLOWED_TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".rb",
    ".rs",
    ".cs",
    ".php",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".swift",
    ".kt",
    ".m",
    ".scala",
    ".sh",
    ".ps1",
    ".bat",
    ".sql",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".toml",
    ".cfg",
    ".conf",
    ".xml",
    ".md",
    ".markdown",
    ".txt",
    ".rst",
}


def _read_snippet(path: Path, per_file_limit: int, remaining: int) -> str:
    if remaining <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    limit = min(per_file_limit, remaining)
    return text[:limit]


class ValidationAgent:
    def __init__(self, llm_callable: Optional[LLMCallable] = None) -> None:
        self.llm_callable = llm_callable

    def create_prompt(
        self,
        template_path: Path,
        examples_path: Path,
        code_paths: Iterable[Path],
        plan_context: str | None = None,
        release_type: str = "initial",
    ) -> str:
        template = load_text_document(template_path)
        examples = load_examples(examples_path)
        code_context = load_code_context(code_paths)
        return build_prompt(
            template,
            examples,
            code_context,
            plan_context=plan_context,
            release_type=release_type,
        )

    def plan_document(
        self, template_path: Path, examples_path: Path, code_paths: Iterable[Path]
    ) -> str:
        template = load_text_document(template_path)
        examples = load_examples(examples_path)
        code_context = load_code_context(code_paths)
        planning_prompt = build_planning_prompt(template, examples, code_context)
        if self.llm_callable:
            return self.llm_callable(planning_prompt)
        return planning_prompt

    def generate_draft(
        self,
        template_path: Path,
        examples_path: Path,
        code_paths: Iterable[Path],
        plan_context: str | None = None,
        release_type: str = "initial",
    ) -> str:
        prompt = self.create_prompt(
            template_path,
            examples_path,
            code_paths,
            plan_context=plan_context,
            release_type=release_type,
        )
        if self.llm_callable:
            return self.llm_callable(prompt)
        return prompt
