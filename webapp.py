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
from src.validation_agent.docx_utils import DocxExportError, draft_to_docx_bytes

app = Flask(__name__)


def _read_upload(file_storage) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
    if not file_storage:
        return None, None, None
    filename = file_storage.filename
    if not filename:
        return None, None, None

    raw_bytes = file_storage.stream.read()
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        text = load_text_document(Path(tmp.name))
    return text, raw_bytes, suffix


def _gather_examples(uploaded_files) -> List[Example]:
    examples: List[Example] = []
    for file_storage in uploaded_files or []:
        content, _, _ = _read_upload(file_storage)
        if content:
            examples.append(Example(title=file_storage.filename, context="", output=content))
    return examples


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


def _build_prompt_from_request(form, files) -> Tuple[str, Optional[bytes]]:
    template_text, template_bytes, _ = _read_upload(files.get("template_file"))
    template_text = template_text or form.get("template_text", "")
    examples = _gather_examples(files.getlist("examples"))
    code_context = _gather_code_context(files.getlist("code_files"), form.get("code_context", ""))
    return build_prompt(template_text, examples, code_context), template_bytes


@app.route("/", methods=["GET", "POST"])
def index():
    prompt: Optional[str] = None
    draft: Optional[str] = None
    error: Optional[str] = None
    history: list[dict] = []
    template_bytes: Optional[bytes] = None

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
        prompt, template_bytes = _build_prompt_from_request(request.form, request.files)
        if not template_bytes:
            template_b64 = request.form.get("template_b64", "")
            if template_b64:
                try:
                    template_bytes = base64.b64decode(template_b64)
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
            encoded_template = request.form.get("template_b64", "")
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
            except MedtronicGPTError as exc:
                error = str(exc)

    return render_template_string(
        TEMPLATE,
        prompt=prompt,
        draft=draft,
        error=error,
        defaults=defaults,
        history=history,
        stored=stored,
        template_b64=base64.b64encode(template_bytes).decode("utf-8") if template_bytes else "",
    )


TEMPLATE = """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Validation Draft UI</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; }
    textarea { width: 100%; height: 120px; }
    .section { margin-bottom: 1.5rem; }
    .output { white-space: pre-wrap; background: #f6f8fa; padding: 1rem; border-radius: 6px; }
    label { font-weight: bold; }
  </style>
</head>
<body>
  <h1>Validation Draft Builder</h1>
  <p>Upload your template, examples, and code context to generate a MedtronicGPT draft. Any required guidance should live inside the template (for example, blue placeholder text). The UI focuses on the completed output and keeps prompts hidden.</p>

  {% if error %}
    <div style=\"color: red;\"><strong>Error:</strong> {{ error }}</div>
  {% endif %}

  <form method=\"post\" enctype=\"multipart/form-data\">
    <input type=\"hidden\" name=\"action\" value=\"build\">
    <div class=\"section\">
      <label>Template (upload the source file)</label><br>
      <input type=\"file\" name=\"template_file\" required>
    </div>

    <div class=\"section\">
      <label>Examples (upload multiple files)</label><br>
      <input type=\"file\" name=\"examples\" multiple>
    </div>

    <div class=\"section\">
      <label>Code context (upload files and/or paste)</label><br>
      <input type=\"file\" name=\"code_files\" multiple>
      <textarea name=\"code_context\" placeholder=\"Paste relevant code snippets, configs, and notes...\"></textarea>
    </div>

    <div class=\"section\">
      <label>MedtronicGPT connection</label><br>
      <input type=\"checkbox\" name=\"use_model\" id=\"use_model\" checked> <label for=\"use_model\">Generate draft with MedtronicGPT</label><br>
      <div style=\"margin-left: 1rem;\">
        <div><label>Model</label><br><input type=\"text\" name=\"model\" value=\"{{ defaults.model }}\" style=\"width:100%\"></div>
        <div><label>Base URL</label><br><input type=\"text\" name=\"base_url\" value=\"{{ defaults.base_url }}\" style=\"width:100%\"></div>
        <div><label>API version</label><br><input type=\"text\" name=\"api_version\" value=\"{{ defaults.api_version }}\" style=\"width:100%\"></div>
        <div><label>Completions path template</label><br><input type=\"text\" name=\"path_template\" value=\"{{ defaults.path_template }}\" style=\"width:100%\"></div>
        <div><label>Subscription key</label><br><input type=\"text\" name=\"subscription_key\" value=\"{{ stored.subscription_key }}\" style=\"width:100%\"></div>
        <div><label>API token</label><br><input type=\"text\" name=\"api_token\" value=\"{{ stored.api_token }}\" style=\"width:100%\"></div>
        <div><label>Refresh token</label><br><input type=\"text\" name=\"refresh_token\" value=\"{{ stored.refresh_token }}\" style=\"width:100%\"></div>
        <div><input type=\"checkbox\" name=\"remember_credentials\" id=\"remember_credentials\" checked> <label for=\"remember_credentials\">Remember credentials on this machine</label></div>
      </div>
    </div>

    <button type=\"submit\">Build</button>
  </form>

  {% if draft %}
    <div class=\"section\">
      <h2>Generated Draft</h2>
      <div class=\"output\">{{ draft }}</div>
      <form method=\"post\" style=\"margin-top: 0.5rem;\">
        <input type=\"hidden\" name=\"action\" value=\"download\">
        <input type=\"hidden\" name=\"draft_text\" value=\"{{ draft }}\">
        <input type=\"hidden\" name=\"template_b64\" value=\"{{ template_b64 }}\">
        <label>Word file name</label><br>
        <input type=\"text\" name=\"filename\" value=\"validation_draft.docx\" style=\"width: 50%;\"> <button type=\"submit\">Download as Word</button>
      </form>
    </div>
  {% endif %}

  <div class=\"section\">
    <h2>Clarify or refine via chat</h2>
    <p>Use this chat to ask MedtronicGPT for clarifications or follow-up questions when something in the template or code context is unclear.</p>
    <form method=\"post\">
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

      <textarea name=\"chat_input\" placeholder=\"Ask a question or request edits...\" style=\"height: 80px;\"></textarea><br>
      <button type=\"submit\">Send</button>
    </form>

    {% if history %}
      <div class=\"output\" style=\"margin-top: 0.75rem;\">
        {% for message in history %}
          <strong>{{ message.role|capitalize }}:</strong> {{ message.content }}<br>
        {% endfor %}
      </div>
    {% endif %}
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
