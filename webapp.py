from __future__ import annotations
import json
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple
from flask import Flask, render_template_string, request
from src.validation_agent.prompt_builder import (
    Example,
    build_planning_prompt,
    build_prompt,
    build_update_prompt,
    extract_placeholders,
)
from src.validation_agent.document_loader import load_text_document
from src.validation_agent.medtronic_client import MedtronicGPTClient, MedtronicGPTError
from src.validation_agent.credentials import StoredCredentials, load_credentials, save_credentials
from src.validation_agent.storage import (
    SavedInputs,
    StoredFile,
    load_saved_inputs,
    save_inputs,
)
from src.validation_agent.workbook_loader import extract_excel_context, extract_pbix_context
import tempfile
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
app = Flask(__name__)
def _read_upload(file_storage) -> Tuple[Optional[str], Optional[bytes], Optional[str], Optional[str]]:
    if not file_storage:
        return None, None, None, None
    filename = file_storage.filename
    if not filename:
        return None, None, None, None
    raw_bytes = b""
    try:
        file_storage.stream.seek(0)
        raw_bytes = file_storage.stream.read()
    except Exception:
        try:
            raw_bytes = file_storage.read()
        except Exception:
            raw_bytes = b""
    if raw_bytes is None:
        raw_bytes = b""
    suffix = Path(filename).suffix
    text: Optional[str]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        try:
            text = load_text_document(Path(tmp.name))
        except Exception:
            text = ""
    return text, raw_bytes, suffix, filename
def _read_saved_file(saved_file: StoredFile) -> Tuple[Optional[str], Optional[bytes], Optional[str], Optional[str]]:
    try:
        raw_bytes = saved_file.to_bytes()
    except Exception:
        return None, None, None, None
    suffix = saved_file.suffix or Path(saved_file.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        text = load_text_document(Path(tmp.name))
    return text, raw_bytes, suffix, saved_file.name
def _dedupe_by_name(files: List[StoredFile]) -> List[StoredFile]:
    seen = set()
    unique: List[StoredFile] = []
    for item in files:
        if item.name in seen:
            continue
        seen.add(item.name)
        unique.append(item)
    return unique
def _extract_questions_from_json(payload: str) -> List[str]:
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except Exception:
        return []
    questions: List[str] = []
    if isinstance(data, dict):
        for key in ("questions", "clarifying_questions"):
            value = data.get(key)
            if isinstance(value, list):
                questions.extend(str(item).strip() for item in value if str(item).strip())
    return questions
def _gather_examples(
    uploaded_files,
    saved_examples: List[StoredFile],
    *,
    tag: Optional[str] = None,
) -> Tuple[List[Example], List[StoredFile]]:
    examples: List[Example] = []
    stored_examples: List[StoredFile] = []
    for file_storage in uploaded_files or []:
        content, raw_bytes, _, filename = _read_upload(file_storage)
        if raw_bytes is not None and filename:
            label = f"[{tag}] {filename}" if tag else filename
            examples.append(Example(title=label, context="", output=content or ""))
            stored_examples.append(StoredFile.from_bytes(label, raw_bytes))
    for saved in saved_examples:
        content, raw_bytes, _, name = _read_saved_file(saved)
        if raw_bytes is not None and name:
            examples.append(Example(title=name, context="", output=content or ""))
            stored_examples.append(StoredFile.from_bytes(name, raw_bytes))
    return examples, _dedupe_by_name(stored_examples)
def _format_code_section(label: str, snippets: List[str]) -> str:
    if not snippets:
        return ""
    return f"## {label}\n" + "\n".join(snippets)
def _gather_code_context(
    current_code_files, inline_code: str, old_code_files=None, new_code_files=None
) -> str:
    snippets_current: List[str] = []
    snippets_old: List[str] = []
    snippets_new: List[str] = []
    def _extract_from_upload(fs) -> str:
        from pathlib import Path
        if not fs or not fs.filename:
            return ""
        suffix = Path(fs.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            fs.stream.seek(0)
            tmp.write(fs.stream.read())
            tmp.flush()
            tmp_path = Path(tmp.name)
        try:
            if suffix in {".xlsm", ".xlsx", ".xls", ".xlsb"}:
                return extract_excel_context(tmp_path)
            if suffix == ".pbix":
                return extract_pbix_context(tmp_path)
            tmp_path_str = tmp_path.read_text(encoding="utf-8", errors="ignore")
            return tmp_path_str
        except Exception:
            return ""
    def _collect(files, bucket: List[str], tag: str):
        from pathlib import Path
        for fs in files or []:
            if not fs or not fs.filename:
                continue
            text = _extract_from_upload(fs)
            if text.strip():
                bucket.append(f"\n# {tag} File: {fs.filename}\n{text.strip()}\n")
    _collect(current_code_files, snippets_current, "Current")
    _collect(old_code_files or [], snippets_old, "Previous")
    _collect(new_code_files or [], snippets_new, "Updated")
    sections = [
        _format_code_section("Current code/context", snippets_current),
        _format_code_section("Previous version (for updates)", snippets_old),
        _format_code_section("Updated version (for updates)", snippets_new),
    ]
    if inline_code.strip():
        sections.append(_format_code_section("Additional notes", [inline_code.strip()]))
    return "\n\n".join(part for part in sections if part).strip()
def _compute_missing_placeholders(template_text: str, draft_json: str) -> List[str]:
    if not template_text or not draft_json:
        return []
    tokens = [tok.strip() for tok in extract_placeholders(template_text) if tok.strip()]
    if not tokens:
        return []
    try:
        data = json.loads(draft_json)
    except Exception:
        return tokens
    filled = set()
    placeholders_map = data.get("placeholders") if isinstance(data, dict) else {}
    if isinstance(placeholders_map, dict):
        for token, value in placeholders_map.items():
            token = str(token).strip()
            if not token:
                continue
            if isinstance(value, str):
                if value.strip():
                    filled.add(token)
            elif value is not None:
                filled.add(token)
    answers = data.get("answers") if isinstance(data, dict) else []
    if isinstance(answers, list):
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            token = str(entry.get("placeholder", "")).strip()
            replacement = entry.get("replacement", entry.get("answer"))
            if not token:
                continue
            if replacement is None:
                continue
            if isinstance(replacement, str) and not replacement.strip():
                continue
            filled.add(token)
    return [tok for tok in tokens if tok not in filled]
def _build_prompt_from_request(
    form,
    files,
    selected_template: Optional[StoredFile],
    kept_saved_examples: List[StoredFile],
    plan_context: str | None,
    release_type: str,
) -> Tuple[str, Optional[bytes], Optional[StoredFile], List[StoredFile], str, List[Example], str]:
    template_text, template_bytes, _, template_name = _read_upload(files.get("template_file"))
    stored_template: Optional[StoredFile] = None
    if not template_text and selected_template:
        template_text, template_bytes, _, template_name = _read_saved_file(selected_template)
    if template_bytes is not None and template_name:
        stored_template = StoredFile.from_bytes(template_name, template_bytes)
        selected_template = stored_template
        selected_template_name = stored_template.name
    examples, stored_examples = _gather_examples(files.getlist("examples"), kept_saved_examples)
    code_context = _gather_code_context(
        files.getlist("code_files"),
        form.get("code_context", ""),
        old_code_files=files.getlist("code_files_old") if release_type == "update" else None,
        new_code_files=files.getlist("code_files_new") if release_type == "update" else None,
    )
    prompt = build_prompt(
        template_text or "",
        examples,
        code_context,
        plan_context=plan_context,
        release_type=release_type,
    )
    return (
        prompt,
        template_bytes,
        stored_template,
        stored_examples,
        template_text or "",
        examples,
        code_context,
    )
@app.route("/", methods=["GET", "POST"])
def index():
    prompt: Optional[str] = None
    draft: Optional[str] = None
    error: Optional[str] = None
    history: list[dict] = []
    plan_text: str = ""
    draft_questions: List[str] = []
    template_bytes: Optional[bytes] = None
    code_context_text: str = ""
    template_text: str = ""
    missing_placeholders: List[str] = []
    coverage_note: Optional[str] = None
    stored_inputs: SavedInputs = load_saved_inputs()
    persisted_inputs: SavedInputs = stored_inputs
    draft_json_from_form: str = ""
    release_type: str = "initial"
    selected_template_name: str = stored_inputs.templates[0].name if stored_inputs.templates else ""
    defaults = {
        "base_url": MedtronicGPTClient.DEFAULT_BASE_URL,
        "api_version": MedtronicGPTClient.DEFAULT_API_VERSION,
        "path_template": MedtronicGPTClient.DEFAULT_PATH_TEMPLATE,
        "model": "gpt-41",
    }
    stored = load_credentials()
    defaults.update(
        {
            "base_url": stored.base_url or defaults["base_url"],
            "api_version": stored.api_version or defaults["api_version"],
            "path_template": stored.path_template or defaults["path_template"],
        }
    )
    if request.method == "POST":
        draft_json_from_form = request.form.get("draft_json", "")
        remove_template_name = request.form.get("remove_template", "").strip()
        remove_example_name = request.form.get("remove_example", "").strip()
        clear_saved_templates = request.form.get("clear_templates") == "on"
        keep_saved_templates = not clear_saved_templates
        kept_saved_examples: List[StoredFile] = []
        for idx, saved_example in enumerate(stored_inputs.examples):
            keep_flag = request.form.get(f"keep_example_{idx}")
            if keep_flag == "on":
                kept_saved_examples.append(saved_example)
        template_choice = request.form.get("selected_template", "")
        selected_template_name = template_choice.strip()
        available_templates = stored_inputs.templates if keep_saved_templates else []
        selected_template_file: Optional[StoredFile] = None
        if available_templates and selected_template_name:
            selected_template_file = next(
                (item for item in available_templates if item.name == selected_template_name),
                available_templates[0],
            )
            selected_template_name = selected_template_file.name
        final_templates: List[StoredFile] = available_templates
        final_examples: List[StoredFile] = kept_saved_examples
        plan_text = request.form.get("plan_text", "")
        remember_credentials = request.form.get("remember_credentials") == "on"
        release_type = request.form.get("release_type", "initial") or "initial"
        history_json = request.form.get("history_json", "[]")
        code_context_text = request.form.get("code_context", "")
        try:
            history = json.loads(history_json) if history_json else []
        except json.JSONDecodeError:
            history = []
        action = request.form.get("action", "build")
        if remove_template_name:
            action = "remove_template"
        if remove_example_name:
            action = "remove_example"
        if action == "remove_template":
            updated_templates = [
                tmpl for tmpl in stored_inputs.templates if tmpl.name != remove_template_name
            ]
            persisted_inputs = SavedInputs(templates=updated_templates, examples=stored_inputs.examples)
            save_inputs(persisted_inputs)
            stored_inputs = persisted_inputs
            selected_template_name = updated_templates[0].name if updated_templates else ""
            draft = draft_json_from_form or draft
            draft_questions = _extract_questions_from_json(draft) if draft else []
            return render_template_string(
                TEMPLATE,
                prompt=prompt,
                draft=draft,
                error=error,
                defaults=defaults,
                history=history,
                stored=stored,
                saved_inputs=persisted_inputs,
                plan_text=plan_text,
                draft_questions=draft_questions,
                draft_json=draft_json_from_form,
                code_context=code_context_text,
                release_type=release_type,
                selected_template_name=selected_template_name,
                missing_placeholders=missing_placeholders,
            )
        if action == "remove_example":
            updated_examples = [
                ex for ex in stored_inputs.examples if ex.name != remove_example_name
            ]
            persisted_inputs = SavedInputs(templates=stored_inputs.templates, examples=updated_examples)
            save_inputs(persisted_inputs)
            stored_inputs = persisted_inputs
            kept_saved_examples = updated_examples
            draft = draft_json_from_form or draft
            draft_questions = _extract_questions_from_json(draft) if draft else []
            return render_template_string(
                TEMPLATE,
                prompt=prompt,
                draft=draft,
                error=error,
                defaults=defaults,
                history=history,
                stored=stored,
                saved_inputs=persisted_inputs,
                plan_text=plan_text,
                draft_questions=draft_questions,
                draft_json=draft_json_from_form,
                code_context=code_context_text,
                release_type=release_type,
                selected_template_name=selected_template_name,
                missing_placeholders=missing_placeholders,
            )
        (
            prompt,
            template_bytes,
            stored_template,
            stored_examples,
            template_text,
            examples,
            code_context,
        ) = _build_prompt_from_request(
            request.form,
            request.files,
            selected_template_file,
            kept_saved_examples,
            plan_text,
            release_type,
        )
        if stored_template:
            selected_template_name = stored_template.name
        code_context_text = code_context
        if not template_bytes and selected_template_file:
            try:
                template_bytes = selected_template_file.to_bytes()
            except Exception:
                template_bytes = None
        client = None
        model = request.form.get("model", "").strip() or defaults["model"]
        if request.form.get("use_model") == "on":
            client = MedtronicGPTClient(
                base_url=request.form.get("base_url", "").strip() or defaults["base_url"],
                api_version=request.form.get("api_version", "").strip() or defaults["api_version"],
                path_template=request.form.get("path_template", "").strip() or defaults["path_template"],
                subscription_key=request.form.get("subscription_key", "").strip(),
                api_token=request.form.get("api_token", "").strip(),
                refresh_token=request.form.get("refresh_token", "").strip(),
            )
            if remember_credentials:
                stored = StoredCredentials(
                    subscription_key=request.form.get("subscription_key", "").strip(),
                    api_token=request.form.get("api_token", "").strip(),
                    refresh_token=request.form.get("refresh_token", "").strip(),
                    api_version=request.form.get("api_version", "").strip() or defaults["api_version"],
                    base_url=request.form.get("base_url", "").strip() or defaults["base_url"],
                    path_template=request.form.get("path_template", "").strip() or defaults["path_template"],
                )
                save_credentials(stored)
        if action in {"answers", "refine"}:
            answered = []
            for key, value in request.form.items():
                if key.startswith("question_"):
                    idx = key.split("_", 1)[1]
                    question = value.strip()
                    answer = request.form.get(f"answer_{idx}", "").strip()
                    if question and answer:
                        answered.append((question, answer))
            if not draft_json_from_form.strip():
                error = "No draft JSON was provided to update with answers."
            else:
                try:
                    parsed = json.loads(draft_json_from_form)
                except json.JSONDecodeError:
                    parsed = None
                    error = "Draft JSON could not be parsed."
                if parsed is not None:
                    answers_list = parsed.get("answers")
                    if not isinstance(answers_list, list):
                        answers_list = []
                    questions_list = parsed.get("questions")
                    if not isinstance(questions_list, list):
                        questions_list = []

                    if answered:
                        for question, answer in answered:
                            answers_list.append({"question": question, "answer": answer})
                            questions_list = [q for q in questions_list if q != question]

                    parsed["answers"] = answers_list
                    parsed["questions"] = questions_list

                    if action == "answers":
                        draft = json.dumps(parsed, indent=2)
                        draft_questions = [q for q in questions_list if q]
                    elif action == "refine":
                        if not client:
                            error = "Provide MedtronicGPT credentials to update with GPT."
                        else:
                            missing_for_update: List[str] = []
                            if template_text:
                                missing_for_update = _compute_missing_placeholders(
                                    template_text, json.dumps(parsed)
                                )
                            update_prompt = build_update_prompt(
                                template_text or "",
                                examples,
                                code_context,
                                json.dumps(parsed),
                                answered,
                                plan_context=plan_text,
                                release_type=release_type,
                                missing_tokens=missing_for_update,
                            )
                            try:
                                draft = client.generate_completion(update_prompt, model=model)
                                draft_questions = _extract_questions_from_json(draft)
                            except MedtronicGPTError as exc:
                                error = str(exc)
        if action == "chat" and client:
            user_message = request.form.get("chat_input", "").strip()
            if user_message:
                history.append({"role": "user", "content": user_message})
                seed = {
                    "role": "system",
                    "content": (
                        "You are assisting with Medtronic validation drafting. Answer user questions directly and succinctly using the provided context. "
                        "Do not invent person names or signatures. If context is missing, ask one concise follow-up question. Avoid returning JSON unless explicitly requested."
                    ),
                }
                full_history = [seed]
                if prompt:
                    full_history.append({"role": "system", "content": f"Reference prompt context to stay on-topic:\n{prompt}"})
                if plan_text.strip():
                    full_history.append(
                        {
                            "role": "system",
                            "content": "Use this planning JSON to map placeholders before proposing edits:\n"
                            + plan_text.strip(),
                        }
                    )
                context_json = draft or draft_json_from_form
                if context_json and context_json.strip():
                    full_history.append(
                        {
                            "role": "system",
                            "content": "Ground answers in the latest generated JSON (placeholders/answers/questions):\n"
                            + context_json.strip(),
                        }
                    )
                full_history += history
                try:
                    reply = client.generate_completion(model=model, messages=full_history)
                    history.append({"role": "assistant", "content": reply})
                except MedtronicGPTError as exc:
                    error = str(exc)
        elif action == "chat" and not client:
            error = "Provide MedtronicGPT credentials to chat."
        elif action == "build" and client:
            if not plan_text.strip():
                planning_prompt = build_planning_prompt(
                    template_text or "", examples, code_context
                )
                try:
                    plan_text = client.generate_completion(planning_prompt, model=model)
                except MedtronicGPTError as exc:
                    error = str(exc)
            if not error:
                prompt = build_prompt(template_text or "", examples, code_context, plan_context=plan_text)
                try:
                    draft = client.generate_completion(prompt, model=model)
                    draft_questions = _extract_questions_from_json(draft)
                except MedtronicGPTError as exc:
                    error = str(exc)
        if action in {"chat", "answers"} and not draft and draft_json_from_form.strip():
            draft = draft_json_from_form
            draft_questions = _extract_questions_from_json(draft)
        if draft:
            draft_json_from_form = draft
        if stored_template:
            final_templates = _dedupe_by_name([stored_template] + final_templates)
            selected_template_name = stored_template.name
        existing_examples: List[StoredFile] = list(stored_inputs.examples)
        existing_names = {ex.name for ex in existing_examples}
        for ex in stored_examples:
            if ex.name not in existing_names:
                existing_examples.append(ex)
                existing_names.add(ex.name)
        persisted_inputs = SavedInputs(
            templates=final_templates,
            examples=_dedupe_by_name(existing_examples),
        )
        if not selected_template_name and persisted_inputs.templates:
            selected_template_name = persisted_inputs.templates[0].name
        save_inputs(persisted_inputs)
        if client and client.last_refresh and remember_credentials:
            stored = StoredCredentials(
                subscription_key=client.subscription_key,
                api_token=client.api_token,
                refresh_token=client.refresh_token,
                api_version=client.api_version,
                base_url=client.base_url,
                path_template=client.path_template,
            )
            save_credentials(stored)
        coverage_note = None
        missing_placeholders: List[str] = []
        coverage_source = draft or draft_json_from_form
        if template_text and coverage_source:
            missing_placeholders = _compute_missing_placeholders(template_text, coverage_source)
            if (
                missing_placeholders
                and client
                and action in {"build", "refine", "answers"}
                and not error
            ):
                update_prompt = build_update_prompt(
                    template_text or "",
                    examples,
                    code_context,
                    coverage_source,
                    answered=answered if "answered" in locals() else [],
                    plan_context=plan_text,
                    release_type=release_type,
                    missing_tokens=missing_placeholders,
                )
                try:
                    draft = client.generate_completion(update_prompt, model=model)
                    draft_json_from_form = draft
                    draft_questions = _extract_questions_from_json(draft)
                    coverage_source = draft
                    missing_placeholders = _compute_missing_placeholders(
                        template_text, coverage_source
                    )
                except MedtronicGPTError as exc:
                    error = error or str(exc)
            elif missing_placeholders and not client:
                coverage_note = "Provide MedtronicGPT credentials to auto-fill the remaining placeholders."
    return render_template_string(
        TEMPLATE,
        prompt=prompt,
        draft=draft,
        error=error,
        defaults=defaults,
        history=history,
        stored=stored,
        saved_inputs=persisted_inputs,
        plan_text=plan_text,
        draft_questions=draft_questions,
        code_context=code_context_text,
        draft_json=draft_json_from_form,
        coverage_note=coverage_note,
        template_text=template_text,
        missing_placeholders=missing_placeholders,
        release_type=release_type,
        selected_template_name=selected_template_name,
    )
TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Medtronic Validation Draft Builder</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --card: #ffffff;
      --muted: #475569;
      --text: #0f172a;
      --accent: #2563eb;
      --accent-2: #22d3ee;
      --border: rgba(15, 23, 42, 0.08);
      --shadow: 0 24px 70px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at 12% 10%, rgba(34, 211, 238, 0.18), transparent 26%),
                  radial-gradient(circle at 82% 0%, rgba(37, 99, 235, 0.14), transparent 23%),
                  var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    a { color: var(--accent); }
    h1, h2, h3 { margin: 0; }
    .page { max-width: 1200px; margin: 0 auto; padding: 32px 24px 48px; }
    .header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
    .badge { padding: 8px 12px; border-radius: 999px; background: linear-gradient(120deg, rgba(34, 211, 238, 0.15), rgba(37, 99, 235, 0.14)); color: var(--accent); font-weight: 600; font-size: 14px; }
    .subtitle { color: var(--muted); margin-top: 10px; line-height: 1.5; }
    .stack { display: flex; flex-direction: column; gap: 12px; }
    .muted { color: var(--muted); }
    .card {
      background: var(--card);
      border-radius: 14px;
      border: 1px solid var(--border);
      padding: 18px;
      box-shadow: 0 14px 40px rgba(15, 23, 42, 0.06);
    }
    .section-body { display: grid; gap: 14px; }
    .panel {
      background: var(--card);
      border-radius: 14px;
      border: 1px solid var(--border);
      padding: 16px 18px;
      box-shadow: 0 14px 40px rgba(15, 23, 42, 0.06);
      position: relative;
    }
    .panel::before {
      content: attr(data-step);
      position: absolute;
      left: 14px;
      top: -12px;
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      color: #fff;
      padding: 6px 10px;
      border-radius: 12px;
      font-weight: 700;
      font-size: 13px;
      box-shadow: 0 10px 25px rgba(37, 99, 235, 0.35);
    }
    .panel h3 { margin: 6px 0 8px; font-size: 17px; }
    .panel p { margin: 0 0 10px; color: var(--muted); }
    .tag-row { display: flex; align-items: center; gap: 8px; }
    .pill-old { background: rgba(59,130,246,0.1); color: #2563eb; border-color: rgba(59,130,246,0.35); }
    .pill-new { background: rgba(16,185,129,0.1); color: #059669; border-color: rgba(16,185,129,0.3); }
    .card h3 { margin-bottom: 10px; }
    .card p { color: var(--muted); margin: 6px 0 12px; }
    .section { margin-top: 22px; }
    .section-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; margin-bottom: 8px; }
    .section-head h2 { font-size: 22px; margin: 0; }
    .section-head p { margin: 0; color: var(--muted); }
    .input, textarea, select { width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); background: #f8fafc; color: var(--text); font-size: 14px; }
    .input:focus, textarea:focus, select:focus { outline: 2px solid rgba(37,99,235,0.3); border-color: rgba(37,99,235,0.35); box-shadow: 0 0 0 3px rgba(37,99,235,0.08); }
    textarea { min-height: 110px; resize: vertical; }
    .checkbox { display: flex; align-items: center; gap: 8px; color: var(--text); }
    .checkbox input { width: 16px; height: 16px; accent-color: var(--accent); }
    .actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; align-items: center; }
    .btn {
      border: none; cursor: pointer; border-radius: 12px; padding: 12px 16px; font-weight: 700; font-size: 15px;
      transition: all 0.15s ease; display: inline-flex; align-items: center; gap: 8px;
    }
    .btn-primary { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; box-shadow: 0 12px 30px rgba(37, 99, 235, 0.25); }
    .btn-ghost { background: #eef2ff; color: #1e293b; border: 1px solid rgba(37, 99, 235, 0.2); }
    .btn:hover { transform: translateY(-1px); }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; background: #eef2ff; border: 1px solid rgba(37,99,235,0.18); color: var(--muted); font-size: 12px; }
    .output { white-space: pre-wrap; background: #f8fafc; border: 1px solid var(--border); padding: 16px; border-radius: 14px; min-height: 140px; color: var(--text); }
    .chat { margin-top: 6px; }
    .error { border: 1px solid #ef4444; color: #991b1b; background: #fee2e2; padding: 12px 14px; border-radius: 12px; }
    .tagline { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; color: var(--muted); }
    .loading {
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.35);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 50;
      backdrop-filter: blur(2px);
    }
    .loading.visible { display: flex; }
    .loading-card {
      background: #fff;
      padding: 18px 20px;
      border-radius: 16px;
      box-shadow: var(--shadow);
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 260px;
      border: 1px solid var(--border);
    }
    .spinner {
      width: 24px;
      height: 24px;
      border: 3px solid rgba(37, 99, 235, 0.25);
      border-top-color: #2563eb;
      border-radius: 999px;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"loading\" id=\"loading\" aria-live=\"polite\" aria-busy=\"true\">
      <div class=\"loading-card\">
        <div class=\"spinner\" role=\"status\" aria-label=\"Loading\"></div>
        <div>
          <div style=\"font-weight: 700; color: var(--text);\">Working on your answers…</div>
          <div style=\"color: var(--muted); font-size: 14px;\">This may take a few seconds.</div>
        </div>
      </div>
    </div>
    <div class=\"header\">
      <div>
        <div class=\"badge\">Medtronic Validation Draft Builder</div>
      </div>
    </div>
    {% if error %}
      <div class=\"error\"><strong>Error:</strong> {{ error }}</div>
    {% endif %}
    <div class=\"section\">
      <div class=\"section-head\">
        <h2>Inputs</h2>
        <p>Step through the essentials: template, release type, examples, context, and connection.</p>
      </div>
      <form id=\"mainForm\" method=\"post\" enctype=\"multipart/form-data\">
      <input type=\"hidden\" name=\"plan_text\" value=\"{{ plan_text }}\">
      <textarea name=\"draft_json\" style=\"display:none;\">{{ draft or draft_json }}</textarea>
      <input type=\"hidden\" name=\"remove_example\" id=\"removeExampleInput\" value=\"\">
      <div class=\"section-body\">
        <div class="panel" data-step="Step 1">
          <h3>Template & release</h3>
          <p>Pick a template (saved or new), then choose the release type.</p>
          <div class="stack">
            <div>
              <label class="muted" style="font-weight:600;">Upload template</label>
              <input class="input" type="file" name="template_file">
            </div>
            {% if saved_inputs.templates %}
              <div class="stack" style="gap: 8px;">
                <label class="muted" style="font-weight:600;">Select template</label>
                <div style="display:flex; gap:8px; align-items:center;">
                  <select class="input" name="selected_template" id="templateSelect">
                    {% for tmpl in saved_inputs.templates %}
                      <option value="{{ tmpl.name }}" {% if tmpl.name == selected_template_name %}selected{% endif %}>{{ tmpl.name }}</option>
                    {% endfor %}
                  </select>
                  <button type="submit" name="remove_template" id="removeTemplateButton" value="{{ selected_template_name or saved_inputs.templates[0].name }}" class="btn btn-ghost" style="padding:10px 12px;" title="Remove selected template">&#8722;</button>
                </div>
              </div>
            {% endif %}
            <div style="display:grid; gap:10px;">
              <label class="checkbox">
                <input type="radio" name="release_type" value="initial" {% if release_type != 'update' %}checked{% endif %}>
                <span>Initial release (default)</span>
              </label>
              <label class="checkbox" style="align-items:flex-start;">
                <input type="radio" name="release_type" value="update" {% if release_type == 'update' %}checked{% endif %}>
                <span>Update/change: upload prior + current code/files so deltas are clear</span>
              </label>
              <p class="muted" style="margin:0;">When set to update, extra slots appear for previous vs updated code/context so GPT knows what changed.</p>
            </div>
          </div>
        </div>
        <div class="panel" data-step="Step 2">
          <h3>Examples</h3>
          <p>Provide example docs to guide tone and structure.</p>
          <div class="stack">
            <input class="input" type="file" name="examples" multiple>
            {% if saved_inputs.examples %}
              <div class="stack" style="gap:8px;">
                <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
                  <div class="pill" style="background: rgba(34,211,238,0.1); color: #067bc7; border-color: rgba(34,211,238,0.25);">
                    Saved examples
                  </div>
                  <button
                    type="button"
                    id="uncheckAllExamples"
                    class="btn btn-ghost"
                    style="padding:6px 10px; font-size:12px;"
                    title="Uncheck all saved examples for this run"
                  >
                    Uncheck all
                  </button>
                </div>
                <div class="stack" style="gap:6px;">
                  {% for example in saved_inputs.examples %}
                    <div style="display:flex; align-items:center; gap:10px;">
                      <label class="checkbox" style="margin:0; flex:1;">
                        <input type="checkbox" name="keep_example_{{ loop.index0 }}" id="keep_example_{{ loop.index0 }}" checked>
                        <span>{{ example.name }}</span>
                      </label>
                      <button type="button" class="btn btn-ghost" data-remove-example="{{ example.name }}" title="Remove example" style="padding:8px 10px;">&#8722;</button>
                    </div>
                  {% endfor %}
                </div>
                <p class="muted" style="margin-top:6px;">
                  Select which saved examples to use for this run. “Uncheck all” keeps them saved but excludes them from the next build.
                </p>
              </div>
            {% endif %}
          </div>
        </div>
        <div class=\"panel\" data-step=\"Step 3\">
          <h3>Code & context</h3>
          <p>Attach relevant code or notes so answers stay anchored to your build.</p>
          <div class=\"stack\">
            <div class=\"file-picker-row\">
              <label class=\"btn btn-ghost\" style=\"padding:8px 12px;\">Add files
                <input type=\"file\" id=\"codeFilesPicker\" multiple style=\"display:none;\">
              </label>
              <label class=\"btn btn-ghost\" style=\"padding:8px 12px;\">Add folder
                <input type=\"file\" id=\"codeFolderPicker\" webkitdirectory directory multiple style=\"display:none;\">
              </label>
            </div>
            <input class=\"input\" type=\"file\" name=\"code_files\" id=\"codeFilesManaged\" multiple style=\"display:none;\">
            <div id=\"codeFileList\" class=\"stack\" style=\"gap:6px;\"></div>
            <div class=\"update-only\" style=\"margin-top: 4px; display:none;\">
              <div class=\"tag-row\">
                <span class=\"pill pill-old\">OLD</span>
                <span class=\"muted\" style=\"margin:0;\">Previous code or config</span>
              </div>
              <input class=\"input\" type=\"file\" name=\"code_files_old\" multiple>
              <div class=\"tag-row\" style=\"margin-top: 10px;\">
                <span class=\"pill pill-new\">NEW</span>
                <span class=\"muted\" style=\"margin:0;\">Updated code or config</span>
              </div>
              <input class=\"input\" type=\"file\" name=\"code_files_new\" multiple>
            </div>
            <div>
              <label class=\"muted\" style=\"font-weight:600;\">Provide supporting snippets</label>
              <textarea name=\"code_context\" placeholder=\"Paste notes, links, or code snippets to ground the output.\">{{ code_context }}</textarea>
            </div>
          </div>
        </div>
        <div class=\"panel\" data-step=\"Step 4\" id=\"connection-card\">
            <div style=\"display:flex; align-items:center; justify-content:space-between; gap:10px;\">
              <div>
                <h3 style=\"margin:6px 0 2px;\">MedtronicGPT connection</h3>
              </div>
            <button type=\"button\" id=\"toggle-connection\" class=\"btn btn-ghost\" style=\"padding:8px 10px;\">Hide</button>
          </div>
          <div id=\"connection-body\" style=\"margin-top: 12px;\">
            <label class=\"checkbox\">
              <input type=\"checkbox\" name=\"use_model\" checked>
              <span>Generate answers with MedtronicGPT</span>
            </label>
            <div style=\"margin-top: 10px; display:grid; gap:10px;\">
              <div class=\"field\">
                <label for=\"base_url\" class=\"muted\" style=\"font-weight:600;\">Base URL</label>
                <input class=\"input\" id=\"base_url\" type=\"text\" name=\"base_url\" placeholder=\"Base URL\" value=\"{{ defaults.base_url }}\">
              </div>
              <div class=\"field\">
                <label for=\"path_template\" class=\"muted\" style=\"font-weight:600;\">Path template</label>
                <input class=\"input\" id=\"path_template\" type=\"text\" name=\"path_template\" placeholder=\"Path template\" value=\"{{ defaults.path_template }}\">
              </div>
              <div class=\"field\">
                <label for=\"api_version\" class=\"muted\" style=\"font-weight:600;\">API version</label>
                <input class=\"input\" id=\"api_version\" type=\"text\" name=\"api_version\" placeholder=\"API version\" value=\"{{ defaults.api_version }}\">
              </div>
              <div class=\"field\">
                <label for=\"model\" class=\"muted\" style=\"font-weight:600;\">Model</label>
                <input class=\"input\" id=\"model\" type=\"text\" name=\"model\" placeholder=\"Model (gpt-41)\" value=\"{{ defaults.model }}\">
              </div>
              <div class=\"field\">
                <label for=\"subscription_key\" class=\"muted\" style=\"font-weight:600;\">Subscription key</label>
                <input class=\"input\" id=\"subscription_key\" type=\"text\" name=\"subscription_key\" placeholder=\"Subscription key\" value=\"{{ stored.subscription_key or '' }}\">
              </div>
              <div class=\"field\">
                <label for=\"api_token\" class=\"muted\" style=\"font-weight:600;\">API token</label>
                <input class=\"input\" id=\"api_token\" type=\"text\" name=\"api_token\" placeholder=\"API token\" value=\"{{ stored.api_token or '' }}\">
              </div>
              <div class=\"field\">
                <label for=\"refresh_token\" class=\"muted\" style=\"font-weight:600;\">Refresh token</label>
                <input class=\"input\" id=\"refresh_token\" type=\"text\" name=\"refresh_token\" placeholder=\"Refresh token\" value=\"{{ stored.refresh_token or '' }}\">
              </div>
            </div>
            <label class=\"checkbox\" style=\"margin-top: 10px;\">
              <input type=\"checkbox\" name=\"remember_credentials\" checked>
              <span>Remember credentials on this device</span>
            </label>
          </div>
        </div>
        </div>
        <div class=\"actions\" style=\"margin-top: 12px; justify-content:flex-start;\">
          <button class=\"btn btn-primary\" type=\"submit\" name=\"action\" value=\"build\">Generate answers</button>
          <label class=\"checkbox\" style=\"gap:6px;\">
            <input type=\"checkbox\" name=\"remember_inputs\" checked>
            <span>Remember uploaded template and examples on this device</span>
          </label>
        </div>
      {% if draft_questions %}
        <div class="card" style="margin-top: 10px;">
          <div class="tagline"><span class="pill">Questions to answer</span><span>Fill these in to update the JSON</span></div>
          <form method="post" id="answersForm">
            <textarea name="draft_json" style="display:none;">{{ draft }}</textarea>
            <input type="hidden" name="plan_text" value="{{ plan_text }}">
            <textarea name="code_context" style="display:none;">{{ code_context }}</textarea>
            <input type="hidden" name="use_model" value="on">
            <input type="hidden" name="model" value="{{ defaults.model }}">
            <input type="hidden" name="base_url" value="{{ defaults.base_url }}">
            <input type="hidden" name="api_version" value="{{ defaults.api_version }}">
            <input type="hidden" name="path_template" value="{{ defaults.path_template }}">
            <input type="hidden" name="subscription_key" value="{{ stored.subscription_key }}">
            <input type="hidden" name="api_token" value="{{ stored.api_token }}">
            <input type="hidden" name="refresh_token" value="{{ stored.refresh_token }}">
            {% for q in draft_questions %}
              <div style="margin-top: 12px;">
                <div class="pill" style="margin-bottom: 6px; display: inline-flex;">Question {{ loop.index }}</div>
                <div style="margin-bottom: 6px; color: #0f172a;">{{ q }}</div>
                <textarea name="answer_{{ loop.index0 }}" placeholder="Type your answer..." style="min-height: 70px;"></textarea>
                <input type="hidden" name="question_{{ loop.index0 }}" value="{{ q }}">
              </div>
            {% endfor %}
            <div class="actions" style="margin-top: 12px; gap: 10px;">
              <button class="btn btn-ghost" type="submit" name="action" value="answers">Save answers into JSON</button>
              <button class="btn btn-primary" type="submit" name="action" value="refine">Send answers to GPT</button>
            </div>
          </form>
        </div>
      {% endif %}
    {% if draft %}
      <div class="section">
        <div class="section-head">
          <h2>Answers</h2>
          <p>Review and copy all current mappings.</p>
        </div>
        <div class="card" style="margin-top: 10px;">
          <div class="tagline"><span class="pill">Generated Answers</span><span>Copy</span></div>
          <div class="actions" style="margin-top: 8px; gap: 8px;">
            <div class="pill" id="viewToggleJson" style="cursor: pointer;">JSON view</div>
            <div class="pill" id="viewToggleFriendly" style="cursor: pointer; background: rgba(34,197,94,0.1); color: #22c55e; border-color: rgba(34,197,94,0.3);">Easy view</div>
            <button type="button" class="btn btn-primary" id="copyAll">Copy all</button>
          </div>
          <div id="jsonView" class="output" style="margin-top: 10px; white-space: pre-wrap;">{{ draft }}</div>
          <div id="friendlyView" class="output" style="margin-top: 10px; display: none;"></div>
          <p style="margin: 10px 0 0; color: #475569;">Toggle between the raw JSON and a simplified list of answers. Use Copy all to grab the current JSON.</p>
        </div>
      </div>
    {% endif %}
      {% if template_text and (draft or draft_json) %}
        <div class="section">
          <div class="section-head">
            <h2>Coverage</h2>
          </div>
          <div class="card" style="margin-top: 10px;">
          <div class="tagline"><span class="pill">Coverage check</span><span>Template placeholders</span></div>
          {% if missing_placeholders %}
            <p style="margin: 6px 0 10px; color: #475569;">These placeholders still need answers:</p>
            <ul style="margin: 0; padding-left: 18px; color: #0f172a;">
              {% for token in missing_placeholders %}
                <li>{{ token }}</li>
              {% endfor %}
            </ul>
              {% if coverage_note %}
                <p style="margin: 10px 0 0; color: #ef4444;">{{ coverage_note }}</p>
              {% endif %}
          {% else %}
            <p style="margin: 6px 0 0; color: #0f172a;">All detected placeholders have values based on the current answers.</p>
          {% endif %}
        </div>
        </div>
      {% endif %}
    <div class="section" style="margin-bottom: 12px;">
      <div class="section-head">
        <h2>Ask Clarifying Questions and Refine Answers</h2>
        <p>Chat stays grounded in your uploaded context and current answers.</p>
      </div>
      <div class="card" style="margin-top: 10px;">
        <div class="tagline"><span class="pill">Clarify or refine</span></div>
        <form method="post" class="chat" id="chatForm">
          <input type="hidden" name="action" value="chat">
          <input type="hidden" name="use_model" value="on">
          <input type="hidden" name="model" value="{{ defaults.model }}">
          <input type="hidden" name="base_url" value="{{ defaults.base_url }}">
          <input type="hidden" name="api_version" value="{{ defaults.api_version }}">
          <input type="hidden" name="path_template" value="{{ defaults.path_template }}">
          <input type="hidden" name="subscription_key" value="{{ stored.subscription_key }}">
          <input type="hidden" name="api_token" value="{{ stored.api_token }}">
          <input type="hidden" name="refresh_token" value="{{ stored.refresh_token }}">
          <input type="hidden" name="plan_text" value="{{ plan_text }}">
          <textarea name="draft_json" style="display:none;">{{ draft or draft_json }}</textarea>
          <textarea name="code_context" style="display:none;">{{ code_context }}</textarea>
          <input type="hidden" name="history_json" value='{{ history | tojson }}'>
          <textarea name="chat_input" placeholder="Ask a question or request edits..." style="min-height: 80px;"></textarea>
          <div class="actions" style="margin-top: 10px;">
            <button class="btn btn-ghost" type="submit">Send</button>
            <div class="pill">Chat stays aligned to your uploaded context.</div>
          </div>
        </form>
        {% if history %}
          <div class="output" style="margin-top: 12px;">
            {% for message in history %}
              <div style="margin-bottom: 8px;"><strong>{{ message.role|capitalize }}:</strong> {{ message.content }}</div>
            {% endfor %}
          </div>
        {% endif %}
      </div>
    </div>
    </div>
    <script>
    const loading = document.getElementById('loading');
    const mainForm = document.getElementById('mainForm');
    const answersForm = document.getElementById('answersForm');
    const chatForm = document.getElementById('chatForm');
    const connectionToggle = document.getElementById('toggle-connection');
    const connectionBody = document.getElementById('connection-body');
    const removeExampleInput = document.getElementById('removeExampleInput');
    const removeExampleButtons = Array.from(document.querySelectorAll('[data-remove-example]'));
    const templateSelect = document.getElementById('templateSelect');
    const removeTemplateButton = document.getElementById('removeTemplateButton');
    const releaseValue = '{{ release_type }}';
    const updateSections = Array.from(document.querySelectorAll('.update-only'));
    const releaseRadios = Array.from(document.querySelectorAll('input[name="release_type"]'));
    const rememberInputs = document.querySelector('input[name="remember_inputs"]');
    const templateInput = document.querySelector('input[name="template_file"]');
    const examplesInput = document.querySelector('input[name="examples"]');
    const codeFilesManaged = document.getElementById('codeFilesManaged');
    const codeFilesPicker = document.getElementById('codeFilesPicker');
    const codeFolderPicker = document.getElementById('codeFolderPicker');
    const codeFileList = document.getElementById('codeFileList');
    const uncheckAllExamplesButton = document.getElementById('uncheckAllExamples');
    function submitWithAction(actionValue) {
      if (!mainForm) return;
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'action';
      hidden.value = actionValue;
      mainForm.appendChild(hidden);
      mainForm.submit();
    }
    let answersReleaseInput = null;
    let chatReleaseInput = null;
    function syncUpdateSections(value) {
      const show = value === 'update';
      updateSections.forEach((node) => {
        node.style.display = show ? '' : 'none';
      });
    }
    function syncReleaseInputs(value) {
      if (answersReleaseInput) answersReleaseInput.value = value;
      if (chatReleaseInput) chatReleaseInput.value = value;
    }
    syncUpdateSections(releaseValue);
    releaseRadios.forEach((radio) => {
      radio.addEventListener('change', (e) => {
        const value = e.target.value;
        syncUpdateSections(value);
        syncReleaseInputs(value);
      });
    });
    if (removeExampleButtons.length && mainForm && removeExampleInput) {
      removeExampleButtons.forEach((btn) => {
        btn.addEventListener('click', (event) => {
          event.preventDefault();
          removeExampleInput.value = btn.getAttribute('data-remove-example') || '';
          if (rememberInputs) rememberInputs.checked = true;
          submitWithAction('remove_example');
        });
      });
    }
    if (templateInput && mainForm) {
      templateInput.addEventListener('change', () => {
        if (rememberInputs) rememberInputs.checked = true;
        submitWithAction('save_template');
      });
    }
    if (examplesInput && mainForm) {
      examplesInput.addEventListener('change', () => {
        if (rememberInputs) rememberInputs.checked = true;
        submitWithAction('save_examples');
      });
    }
    if (uncheckAllExamplesButton) {
      uncheckAllExamplesButton.addEventListener('click', () => {
        const boxes = document.querySelectorAll('input[type="checkbox"][name^="keep_example_"]');
        boxes.forEach((box) => {
          box.checked = false;
        });
      });
    }
    const codeStore = new Map();
    function rebuildCodeFiles() {
      if (!codeFilesManaged || typeof DataTransfer === 'undefined') return;
      const dt = new DataTransfer();
      if (codeFileList) codeFileList.innerHTML = '';
      codeStore.forEach((file, key) => {
        dt.items.add(file);
        if (codeFileList) {
          const row = document.createElement('div');
          row.style.display = 'flex';
          row.style.alignItems = 'center';
          row.style.gap = '8px';
          row.style.justifyContent = 'space-between';
          row.style.padding = '6px 10px';
          row.style.border = '1px solid #e2e8f0';
          row.style.borderRadius = '10px';
          row.style.background = 'white';
          const name = document.createElement('span');
          name.textContent = key;
          name.style.flex = '1';
          const removeBtn = document.createElement('button');
          removeBtn.type = 'button';
          removeBtn.className = 'btn btn-ghost';
          removeBtn.textContent = '−';
          removeBtn.style.padding = '6px 10px';
          removeBtn.title = 'Remove file';
          removeBtn.addEventListener('click', () => {
            codeStore.delete(key);
            rebuildCodeFiles();
          });
          row.appendChild(name);
          row.appendChild(removeBtn);
          codeFileList.appendChild(row);
        }
      });
      codeFilesManaged.files = dt.files;
    }
    function addCodeFiles(fileList) {
      if (!fileList) return;
      Array.from(fileList).forEach((file) => {
        const key = file.webkitRelativePath && file.webkitRelativePath.length ? file.webkitRelativePath : file.name;
        if (!codeStore.has(key)) {
          codeStore.set(key, file);
        }
      });
      if (rememberInputs) rememberInputs.checked = true;
      rebuildCodeFiles();
    }
    if (codeFilesPicker) {
      codeFilesPicker.addEventListener('change', (event) => {
        addCodeFiles(event.target.files);
        codeFilesPicker.value = '';
      });
    }
    if (codeFolderPicker) {
      codeFolderPicker.addEventListener('change', (event) => {
        addCodeFiles(event.target.files);
        codeFolderPicker.value = '';
      });
    }
    if (answersForm) {
      const rel = document.createElement('input');
      rel.type = 'hidden';
      rel.name = 'release_type';
      rel.value = releaseValue;
      answersForm.prepend(rel);
      answersReleaseInput = rel;
    }
    if (chatForm) {
      const relChat = document.createElement('input');
      relChat.type = 'hidden';
      relChat.name = 'release_type';
      relChat.value = releaseValue;
      chatForm.prepend(relChat);
      chatReleaseInput = relChat;
    }
    if (templateSelect && removeTemplateButton) {
      const syncRemoveTarget = () => {
        removeTemplateButton.value = templateSelect.value || removeTemplateButton.value;
      };
      syncRemoveTarget();
      templateSelect.addEventListener('change', syncRemoveTarget);
    }
    if (mainForm && loading) {
      mainForm.addEventListener('submit', (event) => {
        const submitter = event.submitter;
        const actionValue = submitter ? submitter.value : mainForm.querySelector('input[name="action"]')?.value;
        if (actionValue === 'build' || actionValue === 'refine') {
          loading.classList.add('visible');
        }
      });
    }
    if (answersForm && loading) {
      answersForm.addEventListener('submit', (event) => {
        const submitter = event.submitter;
        const actionValue = submitter ? submitter.value : answersForm.querySelector('input[name="action"]')?.value;
        if (actionValue === 'refine') {
          loading.classList.add('visible');
        }
      });
    }
    if (chatForm && loading) {
      chatForm.addEventListener('submit', () => {
        loading.classList.add('visible');
      });
    }
    if (connectionToggle && connectionBody) {
      const persisted = localStorage.getItem('medtronic-connection-hidden');
      if (persisted === 'true') {
        connectionBody.style.display = 'none';
        connectionToggle.textContent = 'Show';
      }
      connectionToggle.addEventListener('click', () => {
        const hidden = connectionBody.style.display === 'none';
        const nextHidden = !hidden;
        connectionBody.style.display = nextHidden ? 'none' : '';
        connectionToggle.textContent = nextHidden ? 'Show' : 'Hide';
        localStorage.setItem('medtronic-connection-hidden', String(nextHidden));
      });
    }
    const rawDraft = {{ draft|tojson if draft else 'null' }};
    const jsonView = document.getElementById('jsonView');
    const friendlyView = document.getElementById('friendlyView');
    const toggleJson = document.getElementById('viewToggleJson');
    const toggleFriendly = document.getElementById('viewToggleFriendly');
    const copyAll = document.getElementById('copyAll');
    function rawDraftText() {
      if (rawDraft === null || rawDraft === undefined) return '';
      if (typeof rawDraft === 'string') return rawDraft;
      try {
        return JSON.stringify(rawDraft, null, 2);
      } catch (e) {
        return '' + rawDraft;
      }
    }
    function parseDraft() {
      if (rawDraft === null || rawDraft === undefined || rawDraft === '') return null;
      if (typeof rawDraft === 'string') {
        try {
          return JSON.parse(rawDraft);
        } catch (e) {
          return null;
        }
      }
      return rawDraft;
    }
    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    function formatValue(value, depth = 0) {
      if (value === null || value === undefined || value === '') return '<span style="color:#94a3b8;">(empty)</span>';
      if (Array.isArray(value)) {
        const items = value
          .map((entry) => `<li>${formatValue(entry, depth + 1)}</li>`)
          .join('');
        return `<ul>${items}</ul>`;
      }
      if (typeof value === 'object') {
        const entries = Object.entries(value)
          .map(([k, v]) => `<li><strong>${escapeHtml(k)}</strong>: ${formatValue(v, depth + 1)}</li>`)
          .join('');
        return `<ul>${entries}</ul>`;
      }
      return escapeHtml(value);
    }
    function renderFriendly() {
      if (!friendlyView) return;
      const parsed = parseDraft();
      if (!parsed) {
        friendlyView.textContent = 'Could not parse JSON. Use the JSON view to copy manually.';
        return;
      }
      const answers = Array.isArray(parsed.answers) ? parsed.answers : [];
      const sections = [];
      if (answers.length) {
        const list = answers
          .map((item) => {
            const placeholder = item.placeholder || item.question;
            const value = item.replacement !== undefined ? item.replacement : item.answer;
            if (!placeholder) return '';
            return `<li><strong>${escapeHtml(placeholder)}</strong> → ${formatValue(value)}</li>`;
          })
          .filter(Boolean)
          .join('');
        if (list) {
          sections.push(`<div style="margin-bottom: 10px;"><div class="pill" style="margin-bottom:6px;">Answers</div><ul>${list}</ul></div>`);
        }
      }
      friendlyView.innerHTML = sections.join('') || 'No parsed answers available.';
    }
    if (toggleJson && toggleFriendly && jsonView && friendlyView) {
      toggleJson.addEventListener('click', () => {
        jsonView.style.display = 'block';
        friendlyView.style.display = 'none';
        toggleJson.style.background = 'rgba(34,211,238,0.1)';
        toggleJson.style.borderColor = 'rgba(34,211,238,0.3)';
        toggleFriendly.style.background = 'rgba(34,197,94,0.05)';
        toggleFriendly.style.borderColor = 'rgba(34,197,94,0.2)';
      });
      toggleFriendly.addEventListener('click', () => {
        renderFriendly();
        jsonView.style.display = 'none';
        friendlyView.style.display = 'block';
        toggleFriendly.style.background = 'rgba(34,197,94,0.1)';
        toggleFriendly.style.borderColor = 'rgba(34,197,94,0.3)';
        toggleJson.style.background = 'rgba(34,211,238,0.05)';
        toggleJson.style.borderColor = 'rgba(34,211,238,0.2)';
      });
      renderFriendly();
      jsonView.textContent = rawDraftText();
      jsonView.style.display = 'none';
      friendlyView.style.display = 'block';
    }
    if (copyAll && rawDraft !== null) {
      copyAll.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(rawDraftText());
          copyAll.textContent = 'Copied!';
          setTimeout(() => (copyAll.textContent = 'Copy all'), 1200);
        } catch (e) {
          copyAll.textContent = 'Copy failed';
          setTimeout(() => (copyAll.textContent = 'Copy all'), 1200);
        }
      });
    }
  </script>
</body>
</html>
"""
if __name__ == "__main__":
    import os
    import socket
    import threading
    import time
    import webbrowser
    def _find_open_port(host: str, preferred: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, preferred))
                return preferred
            except OSError:
                pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, 0))
            return s.getsockname()[1]
    host = os.getenv("VALIDATION_UI_HOST", "127.0.0.1")
    requested_port = int(os.getenv("VALIDATION_UI_PORT", "8000"))
    port = _find_open_port(host, requested_port)
    def _open_browser() -> None:
        time.sleep(1)
        try:
            webbrowser.open(f"http://{host}:{port}")
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=host, port=port, debug=False)
