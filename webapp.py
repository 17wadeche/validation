from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, render_template_string, request, send_file

from src.validation_agent.prompt_builder import Example, build_planning_prompt, build_prompt
from src.validation_agent.document_loader import load_text_document
from src.validation_agent.medtronic_client import MedtronicGPTClient, MedtronicGPTError
from src.validation_agent.credentials import StoredCredentials, load_credentials, save_credentials
from src.validation_agent.docx_utils import DocxExportError, draft_to_docx_bytes
from src.validation_agent.storage import (
    SavedInputs,
    StoredFile,
    load_saved_inputs,
    save_inputs,
)

app = Flask(__name__)


def _read_upload(file_storage) -> Tuple[Optional[str], Optional[bytes], Optional[str], Optional[str]]:
    if not file_storage:
        return None, None, None, None
    filename = file_storage.filename
    if not filename:
        return None, None, None, None

    raw_bytes = file_storage.stream.read()
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        text = load_text_document(Path(tmp.name))
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


def _gather_examples(uploaded_files, saved_examples: List[StoredFile]) -> Tuple[List[Example], List[StoredFile]]:
    examples: List[Example] = []
    stored_examples: List[StoredFile] = []

    for file_storage in uploaded_files or []:
        content, raw_bytes, _, filename = _read_upload(file_storage)
        if content and raw_bytes is not None and filename:
            examples.append(Example(title=filename, context="", output=content))
            stored_examples.append(StoredFile.from_bytes(filename, raw_bytes))

    for saved in saved_examples:
        content, raw_bytes, _, name = _read_saved_file(saved)
        if content and raw_bytes is not None and name:
            examples.append(Example(title=name, context="", output=content))
            stored_examples.append(StoredFile.from_bytes(name, raw_bytes))

    return examples, stored_examples


def _gather_code_context(code_files, inline_code: str) -> str:
    snippets: List[str] = []
    for file_storage in code_files or []:
        if not file_storage or not file_storage.filename:
            continue
        try:
            text = file_storage.stream.read().decode("utf-8")
        except Exception:
            continue
        if text.strip():
            snippets.append(f"\n# File: {file_storage.filename}\n{text.strip()}\n")
    if inline_code.strip():
        snippets.append(inline_code.strip())
    return "\n".join(snippets).strip()


def _build_prompt_from_request(
    form,
    files,
    saved_inputs: SavedInputs,
    keep_saved_template: bool,
    kept_saved_examples: List[StoredFile],
    plan_context: str | None,
) -> Tuple[str, Optional[bytes], Optional[StoredFile], List[StoredFile], str, List[Example], str]:
    template_text, template_bytes, _, template_name = _read_upload(files.get("template_file"))
    stored_template: Optional[StoredFile] = None

    if not template_text and keep_saved_template and saved_inputs.template:
        template_text, template_bytes, _, template_name = _read_saved_file(saved_inputs.template)

    if template_text and template_bytes is not None and template_name:
        stored_template = StoredFile.from_bytes(template_name, template_bytes)

    examples, stored_examples = _gather_examples(files.getlist("examples"), kept_saved_examples)
    code_context = _gather_code_context(files.getlist("code_files"), form.get("code_context", ""))
    prompt = build_prompt(template_text or "", examples, code_context, plan_context=plan_context)
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
    docx_b64: Optional[str] = None
    stored_inputs: SavedInputs = load_saved_inputs()
    persisted_inputs: SavedInputs = stored_inputs

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
        keep_saved_template = request.form.get("remove_template") != "on"
        kept_saved_examples: List[StoredFile] = []
        for idx, saved_example in enumerate(stored_inputs.examples):
            if request.form.get(f"keep_example_{idx}") == "on":
                kept_saved_examples.append(saved_example)

        plan_text = request.form.get("plan_text", "")

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
            stored_inputs,
            keep_saved_template,
            kept_saved_examples,
            plan_text,
        )
        if not template_bytes and keep_saved_template and stored_inputs.template:
            try:
                template_bytes = stored_inputs.template.to_bytes()
            except Exception:
                template_bytes = None
        action = request.form.get("action", "build")

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

            if request.form.get("remember_credentials") == "on":
                save_credentials(
                    StoredCredentials(
                        subscription_key=request.form.get("subscription_key", "").strip(),
                        api_token=request.form.get("api_token", "").strip(),
                        refresh_token=request.form.get("refresh_token", "").strip(),
                        api_version=request.form.get("api_version", "").strip() or defaults["api_version"],
                        base_url=request.form.get("base_url", "").strip() or defaults["base_url"],
                        path_template=request.form.get("path_template", "").strip() or defaults["path_template"],
                    )
                )

        history_json = request.form.get("history_json", "[]")
        try:
            history = json.loads(history_json) if history_json else []
        except json.JSONDecodeError:
            history = []

        if action == "chat" and client:
            user_message = request.form.get("chat_input", "").strip()
            if user_message:
                history.append({"role": "user", "content": user_message})
                seed = {
                    "role": "system",
                    "content": (
                        "You are assisting with Medtronic validation drafting. "
                        "Ask concise follow-up questions when required inputs are unclear and keep the chat grounded in the uploaded template, examples, and code context."
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
                full_history += history
                try:
                    reply = client.generate_completion(model=model, messages=full_history)
                    history.append({"role": "assistant", "content": reply})
                except MedtronicGPTError as exc:
                    error = str(exc)
        elif action == "chat" and not client:
            error = "Provide MedtronicGPT credentials to chat."
        elif action == "download":
            draft_text = request.form.get("draft_text", "")
            filename = request.form.get("filename", "validation_draft.docx") or "validation_draft.docx"
            encoded_docx = request.form.get("docx_b64", "")
            encoded_template = request.form.get("template_b64", "")
            if encoded_docx:
                try:
                    docx_bytes = base64.b64decode(encoded_docx)
                except Exception:
                    docx_bytes = None
                else:
                    buffer = tempfile.SpooledTemporaryFile()
                    buffer.write(docx_bytes)
                    buffer.seek(0)
                    return send_file(
                        buffer,
                        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        download_name=filename,
                        as_attachment=True,
                    )

            if encoded_template and not template_bytes:
                try:
                    template_bytes = base64.b64decode(encoded_template)
                except Exception:
                    template_bytes = None
            try:
                docx_bytes = draft_to_docx_bytes(draft_text, template_bytes=template_bytes)
            except DocxExportError as exc:
                error = str(exc)
            else:
                buffer = tempfile.SpooledTemporaryFile()
                buffer.write(docx_bytes)
                buffer.seek(0)
                return send_file(
                    buffer,
                    mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    download_name=filename,
                    as_attachment=True,
                )
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
                    docx_bytes = draft_to_docx_bytes(draft, template_bytes=template_bytes)
                    docx_b64 = base64.b64encode(docx_bytes).decode("utf-8")
                except MedtronicGPTError as exc:
                    error = str(exc)
                except DocxExportError as exc:
                    error = str(exc)

        if request.form.get("remember_inputs") == "on":
            final_template = stored_template if stored_template else (stored_inputs.template if keep_saved_template else None)
            final_examples = stored_examples
        persisted_inputs = SavedInputs(template=final_template, examples=final_examples)
        save_inputs(persisted_inputs)

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
        docx_b64=docx_b64,
        template_b64=base64.b64encode(template_bytes).decode("utf-8")
        if template_bytes
        else (
            persisted_inputs.template.b64 if persisted_inputs.template else ""
        ),
    )


TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Validation Draft Builder</title>
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
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; margin-top: 16px; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 20px;
      box-shadow: var(--shadow);
    }
    .card h3 { margin-bottom: 10px; }
    .card p { color: var(--muted); margin: 6px 0 12px; }
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
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"header\">
      <div>
        <div class=\"badge\">Medtronic Validation</div>
        <h1>Validation Draft Builder</h1>
        <div class=\"subtitle\">Upload your template and examples, then generate a filled-out answer list with MedtronicGPT. Blue placeholder text is replaced in-place so your tables and formatting stay intact.</div>
      </div>
    </div>

    {% if error %}
      <div class=\"error\"><strong>Error:</strong> {{ error }}</div>
    {% endif %}

    <form method=\"post\" enctype=\"multipart/form-data\">
      <input type=\"hidden\" name=\"plan_text\" value=\"{{ plan_text }}\">
      <div class=\"grid\">
        <div class=\"card\">
          <div class=\"tagline\"><span class=\"pill\">Template</span><span>Upload the source file to preserve layout</span></div>
          <div style=\"margin-top: 12px;\">
            <input class=\"input\" type=\"file\" name=\"template_file\">
          </div>
          <p style=\"margin: 10px 0 0;\">Blue text and inline tokens like &lt;Tool Name&gt; or ___ are detected and replaced in place using the model's structured response.</p>
          {% if saved_inputs.template %}
            <div style=\"margin-top: 12px;\">
              <label class=\"checkbox\">
                <input type=\"checkbox\" name=\"remove_template\" id=\"remove_template\">
                <span>Remove saved template ({{ saved_inputs.template.name }})</span>
              </label>
              <p style=\"margin: 6px 0 0;\">If you skip an upload, the saved template will be reused.</p>
            </div>
          {% endif %}
        </div>

        <div class=\"card\">
          <div class=\"tagline\"><span class=\"pill\">Examples</span><span>Upload multiple files to guide tone and structure</span></div>
          <div style=\"margin-top: 12px;\">
            <input class=\"input\" type=\"file\" name=\"examples\" multiple>
          </div>
          {% if saved_inputs.examples %}
            <div style=\"margin-top: 12px;\">
              <div class=\"pill\" style=\"background: rgba(34,211,238,0.1); color: #67e8f9; border-color: rgba(34,211,238,0.4);\">Saved examples</div>
              {% for example in saved_inputs.examples %}
                <label class=\"checkbox\" style=\"margin-top: 8px;\">
                  <input type=\"checkbox\" name=\"keep_example_{{ loop.index0 }}\" id=\"keep_example_{{ loop.index0 }}\" checked>
                  <span>Reuse {{ example.name }}</span>
                </label>
              {% endfor %}
              <p style=\"margin: 8px 0 0;\">Uncheck to drop saved examples; upload to add or replace.</p>
            </div>
          {% endif %}
        </div>

        <div class=\"card\">
          <div class=\"tagline\"><span class=\"pill\">Code context</span><span>Provide supporting snippets</span></div>
          <div style=\"margin-top: 12px;\">
            <input class=\"input\" type=\"file\" name=\"code_files\" multiple>
          </div>
          <div style=\"margin-top: 10px;\">
            <textarea name=\"code_context\" placeholder=\"Paste relevant code snippets, configs, and notes...\"></textarea>
          </div>
        </div>

        <div class=\"card\">
          <div class=\"tagline\"><span class=\"pill\">MedtronicGPT</span><span>Connection is pre-enabled</span></div>
          <div style=\"margin-top: 12px;\">
            <label class=\"checkbox\">
              <input type=\"checkbox\" name=\"use_model\" id=\"use_model\" checked>
              <span>Generate draft with MedtronicGPT</span>
            </label>
          </div>
          <div class=\"grid\" style=\"grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; margin-top: 10px;\">
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Model</label>
              <input class=\"input\" type=\"text\" name=\"model\" value=\"{{ defaults.model }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Base URL</label>
              <input class=\"input\" type=\"text\" name=\"base_url\" value=\"{{ defaults.base_url }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">API version</label>
              <input class=\"input\" type=\"text\" name=\"api_version\" value=\"{{ defaults.api_version }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Completions path</label>
              <input class=\"input\" type=\"text\" name=\"path_template\" value=\"{{ defaults.path_template }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Subscription key</label>
              <input class=\"input\" type=\"text\" name=\"subscription_key\" value=\"{{ stored.subscription_key }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">API token</label>
              <input class=\"input\" type=\"text\" name=\"api_token\" value=\"{{ stored.api_token }}\">
            </div>
            <div>
              <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Refresh token</label>
              <input class=\"input\" type=\"text\" name=\"refresh_token\" value=\"{{ stored.refresh_token }}\">
            </div>
          </div>
          <div class=\"actions\" style=\"margin-top: 12px;\">
            <label class=\"checkbox\">
              <input type=\"checkbox\" name=\"remember_credentials\" id=\"remember_credentials\" checked>
              <span>Remember credentials on this machine</span>
            </label>
            <label class=\"checkbox\">
              <input type=\"checkbox\" name=\"remember_inputs\" id=\"remember_inputs\" checked>
              <span>Remember template and examples on this machine</span>
            </label>
          </div>
        </div>
      </div>

      <div class=\"actions\" style=\"margin-top: 18px; gap: 10px;\">
        <button class=\"btn btn-primary\" type=\"submit\" name=\"action\" value=\"build\">Generate answers</button>
        <div class=\"pill\">The agent plans, asks questions, and drafts in one step.</div>
      </div>
    </form>

    {% if draft_questions %}
      <div class=\"card\" style=\"margin-top: 18px;\">\n        <div class=\"tagline\"><span class=\"pill\">Questions to answer</span><span>Share these details or reply in chat so the agent can finish</span></div>\n        <p style=\"margin: 8px 0; color: #475569;\">From generated answers:</p>\n        <ul style=\"color: #0f172a; padding-left: 20px; margin-top: 4px;\">\n          {% for q in draft_questions %}\n            <li style=\"margin-bottom: 6px;\">{{ q }}</li>\n          {% endfor %}\n        </ul>\n        <p style=\"margin: 6px 0 0; color: #475569;\">Use chat below to respond; the agent will keep context from your uploads.</p>\n      </div>
    {% endif %}

    {% if draft %}
      <div class=\"card\" style=\"margin-top: 20px;\">
        <div class=\"tagline\"><span class=\"pill\">Generated Answers</span><span>Copy into your template or download with replacements</span></div>
        <div class=\"output\" style=\"margin-top: 10px; white-space: pre-wrap;\">{{ draft }}</div>
        <p style=\"margin: 10px 0 0; color: #475569;\">Download always reuses your uploaded template; the JSON includes `placeholders`, `answers`, and any remaining `questions` so you can paste values directly.</p>
        <form method=\"post\" class=\"actions\" style=\"margin-top: 12px; align-items: flex-end;\">
          <input type=\"hidden\" name=\"action\" value=\"download\">
          <input type=\"hidden\" name=\"draft_text\" value=\"{{ draft }}\">
          <input type=\"hidden\" name=\"docx_b64\" value=\"{{ docx_b64 or '' }}\">
          <input type=\"hidden\" name=\"template_b64\" value=\"{{ template_b64 }}\">
          <div style=\"flex: 1; min-width: 220px;\">
            <label class=\"pill\" style=\"margin-bottom: 6px; display: inline-flex;\">Word file name</label>
            <input class=\"input\" type=\"text\" name=\"filename\" value=\"validation_draft.docx\">
          </div>
          <button class=\"btn btn-primary\" type=\"submit\">Download as Word</button>
        </form>
      </div>
    {% endif %}

    <div class=\"card\" style=\"margin-top: 18px;\">
      <div class=\"tagline\"><span class=\"pill\">Clarify or refine</span><span>Let MedtronicGPT ask for missing details</span></div>
      <p style=\"margin-top: 8px;\">Use chat to resolve unclear inputs before downloading. The agent will ask concise follow-up questions when something in the template or code is ambiguous.</p>
      <form method=\"post\" class=\"chat\">
        <input type=\"hidden\" name=\"action\" value=\"chat\">
        <input type=\"hidden\" name=\"use_model\" value=\"on\">
        <input type=\"hidden\" name=\"model\" value=\"{{ defaults.model }}\">
        <input type=\"hidden\" name=\"base_url\" value=\"{{ defaults.base_url }}\">
        <input type=\"hidden\" name=\"api_version\" value=\"{{ defaults.api_version }}\">
        <input type=\"hidden\" name=\"path_template\" value=\"{{ defaults.path_template }}\">
        <input type=\"hidden\" name=\"subscription_key\" value=\"{{ stored.subscription_key }}\">
        <input type=\"hidden\" name=\"api_token\" value=\"{{ stored.api_token }}\">
        <input type=\"hidden\" name=\"refresh_token\" value=\"{{ stored.refresh_token }}\">
        <input type=\"hidden\" name=\"plan_text\" value=\"{{ plan_text }}\">
        <input type=\"hidden\" name=\"history_json\" value='{{ history | tojson }}'>

        <textarea name=\"chat_input\" placeholder=\"Ask a question or request edits...\" style=\"min-height: 80px;\"></textarea>
        <div class=\"actions\" style=\"margin-top: 10px;\">
          <button class=\"btn btn-ghost\" type=\"submit\">Send</button>
          <div class=\"pill\">Chat stays aligned to your uploaded context.</div>
        </div>
      </form>

      {% if history %}
        <div class=\"output\" style=\"margin-top: 12px;\">
          {% for message in history %}
            <div style=\"margin-bottom: 8px;\"><strong>{{ message.role|capitalize }}:</strong> {{ message.content }}</div>
          {% endfor %}
        </div>
      {% endif %}
    </div>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    # Bind to loopback by default so the dev server is not exposed to the network unless explicitly
    # configured. Override via VALIDATION_UI_HOST/VALIDATION_UI_PORT when remote access is required.
    import os

    host = os.getenv("VALIDATION_UI_HOST", "127.0.0.1")
    port = int(os.getenv("VALIDATION_UI_PORT", "8000"))
    app.run(host=host, port=port, debug=False)
