from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, render_template_string, request, send_file

from src.validation_agent.prompt_builder import Example, build_prompt
from src.validation_agent.document_loader import load_text_document
from src.validation_agent.medtronic_client import MedtronicGPTClient, MedtronicGPTError
from src.validation_agent.credentials import StoredCredentials, load_credentials, save_credentials
from src.validation_agent.docx_utils import DocxExportError, docx_bytes_to_html, draft_to_docx_bytes
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
) -> Tuple[str, Optional[bytes], Optional[StoredFile], List[StoredFile]]:
    template_text, template_bytes, _, template_name = _read_upload(files.get("template_file"))
    stored_template: Optional[StoredFile] = None

    if not template_text and keep_saved_template and saved_inputs.template:
        template_text, template_bytes, _, template_name = _read_saved_file(saved_inputs.template)

    if template_text and template_bytes is not None and template_name:
        stored_template = StoredFile.from_bytes(template_name, template_bytes)

    examples, stored_examples = _gather_examples(files.getlist("examples"), kept_saved_examples)
    code_context = _gather_code_context(files.getlist("code_files"), form.get("code_context", ""))
    return build_prompt(template_text or "", examples, code_context), template_bytes, stored_template, stored_examples


@app.route("/", methods=["GET", "POST"])
def index():
    prompt: Optional[str] = None
    draft: Optional[str] = None
    error: Optional[str] = None
    history: list[dict] = []
    template_bytes: Optional[bytes] = None
    preview_html: Optional[str] = None
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

        prompt, template_bytes, stored_template, stored_examples = _build_prompt_from_request(
            request.form, request.files, stored_inputs, keep_saved_template, kept_saved_examples
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
            try:
                draft = client.generate_completion(prompt, model=model)
                docx_bytes = draft_to_docx_bytes(draft, template_bytes=template_bytes)
                docx_b64 = base64.b64encode(docx_bytes).decode("utf-8")
                preview_html = docx_bytes_to_html(docx_bytes)
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
        preview_html=preview_html,
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
      --bg: #0f172a;
      --panel: #0b1220;
      --card: #111a2f;
      --muted: #8da2c0;
      --accent: #3b82f6;
      --accent-2: #22d3ee;
      --border: rgba(255,255,255,0.08);
      --shadow: 0 10px 30px rgba(0,0,0,0.3);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at 20% 20%, rgba(34, 211, 238, 0.12), transparent 35%),
                  radial-gradient(circle at 80% 0%, rgba(59, 130, 246, 0.12), transparent 25%),
                  var(--bg);
      color: #eef2ff;
      min-height: 100vh;
    }
    a { color: var(--accent); }
    h1, h2, h3 { margin: 0; }
    .page { max-width: 1200px; margin: 0 auto; padding: 32px 24px 48px; }
    .header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 16px; margin-bottom: 24px;
    }
    .badge { padding: 8px 12px; border-radius: 999px; background: rgba(59, 130, 246, 0.12); color: var(--accent); font-weight: 600; font-size: 14px; }
    .subtitle { color: var(--muted); margin-top: 8px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-top: 16px; }
    .card {
      background: linear-gradient(145deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(6px);
    }
    .card h3 { margin-bottom: 10px; }
    .card p { color: var(--muted); margin: 6px 0 12px; }
    .input, textarea, select { width: 100%; padding: 12px 14px; border-radius: 10px; border: 1px solid var(--border); background: rgba(255,255,255,0.03); color: #fff; font-size: 14px; }
    .input:focus, textarea:focus, select:focus { outline: 2px solid rgba(59,130,246,0.5); border-color: rgba(59,130,246,0.3); }
    textarea { min-height: 110px; resize: vertical; }
    .checkbox { display: flex; align-items: center; gap: 8px; color: #dbeafe; }
    .checkbox input { width: 16px; height: 16px; }
    .actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; }
    .btn {
      border: none; cursor: pointer; border-radius: 12px; padding: 12px 16px; font-weight: 700; font-size: 15px;
      transition: all 0.15s ease; display: inline-flex; align-items: center; gap: 8px;
    }
    .btn-primary { background: linear-gradient(135deg, var(--accent), #2563eb); color: #fff; box-shadow: 0 12px 30px rgba(37, 99, 235, 0.35); }
    .btn-ghost { background: rgba(255,255,255,0.06); color: #e2e8f0; border: 1px solid var(--border); }
    .btn:hover { transform: translateY(-1px); }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: var(--muted); font-size: 12px; }
    .output { white-space: pre-wrap; background: rgba(15,23,42,0.8); border: 1px solid var(--border); padding: 16px; border-radius: 14px; min-height: 140px; }
    .chat { margin-top: 6px; }
    .error { border: 1px solid #ef4444; color: #fecdd3; background: rgba(239,68,68,0.08); padding: 12px 14px; border-radius: 12px; }
    .tagline { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; color: var(--muted); }
    .preview { margin-top: 14px; background: #f8fafc; color: #0f172a; border-radius: 14px; padding: 16px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.6); border: 1px solid rgba(15,23,42,0.1); }
    .preview h4 { margin: 0 0 8px 0; color: #0f172a; }
    .preview .doc-surface { background: #fff; border-radius: 10px; padding: 14px; border: 1px solid rgba(15,23,42,0.08); box-shadow: 0 6px 18px rgba(15,23,42,0.08); max-height: 520px; overflow: auto; }
    .preview .doc-surface table { width: 100%; border-collapse: collapse; }
    .preview .doc-surface table, .preview .doc-surface td, .preview .doc-surface th { border: 1px solid #cbd5e1; }
    .preview .doc-surface td, .preview .doc-surface th { padding: 6px; }
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"header\">
      <div>
        <div class=\"badge\">Medtronic Validation</div>
        <h1>Validation Draft Builder</h1>
        <div class=\"subtitle\">Upload your template and examples, then generate a completed draft with MedtronicGPT. Blue placeholder text is replaced in-place so your tables and formatting stay intact.</div>
      </div>
    </div>

    {% if error %}
      <div class=\"error\"><strong>Error:</strong> {{ error }}</div>
    {% endif %}

    <form method=\"post\" enctype=\"multipart/form-data\">
      <input type=\"hidden\" name=\"action\" value=\"build\">
      <div class=\"grid\">
        <div class=\"card\">
          <div class=\"tagline\"><span class=\"pill\">Template</span><span>Upload the source file to preserve layout</span></div>
          <div style=\"margin-top: 12px;\">
            <input class=\"input\" type=\"file\" name=\"template_file\">
          </div>
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

      <div class=\"actions\" style=\"margin-top: 18px;\">
        <button class=\"btn btn-primary\" type=\"submit\">Generate draft</button>
        <div class=\"pill\">Prompts stay hidden; only the completed output is shown.</div>
      </div>
    </form>

    {% if draft %}
      <div class=\"card\" style=\"margin-top: 20px;\">
        <div class=\"tagline\"><span class=\"pill\">Generated Draft</span><span>Grounded in your template, examples, and code</span></div>
        {% if preview_html %}
          <div class=\"preview\">
            <h4>Live preview</h4>
            <div class=\"doc-surface\" aria-label=\"Draft preview\" tabindex=\"0\">{{ preview_html | safe }}</div>
            <p style=\"margin: 10px 0 0; color: #475569;\">Download preserves tables, charts, and formatting from your template.</p>
          </div>
        {% else %}
          <div class=\"output\" style=\"margin-top: 12px;\">{{ draft }}</div>
        {% endif %}
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
    app.run(host="0.0.0.0", port=8000, debug=False)
