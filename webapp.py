from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

from flask import Flask, render_template_string, request

from src.validation_agent.prompt_builder import Example, build_prompt
from src.validation_agent.document_loader import load_text_document
from src.validation_agent.medtronic_client import MedtronicGPTClient, MedtronicGPTError

app = Flask(__name__)


def _read_upload(file_storage) -> Optional[str]:
    if not file_storage:
        return None
    filename = file_storage.filename
    if not filename:
        return None

    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file_storage.save(tmp.name)
        return load_text_document(Path(tmp.name))


def _gather_examples(uploaded_files, inline_example: str) -> List[Example]:
    examples: List[Example] = []
    for file_storage in uploaded_files or []:
        content = _read_upload(file_storage)
        if content:
            examples.append(Example(title=file_storage.filename, context="", output=content))
    if inline_example.strip():
        examples.append(Example(title="Inline example", context="", output=inline_example.strip()))
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


def _build_prompt_from_request(form, files) -> str:
    template_text = _read_upload(files.get("template_file")) or form.get("template_text", "")
    requirements_text = _read_upload(files.get("requirements_file")) or form.get("requirements_text", "")
    examples = _gather_examples(files.getlist("examples"), form.get("examples_text", ""))
    code_context = _gather_code_context(files.getlist("code_files"), form.get("code_context", ""))

    return build_prompt(template_text, requirements_text, examples, code_context)


@app.route("/", methods=["GET", "POST"])
def index():
    prompt: Optional[str] = None
    draft: Optional[str] = None
    error: Optional[str] = None

    if request.method == "POST":
        prompt = _build_prompt_from_request(request.form, request.files)

        use_model = request.form.get("use_model") == "on"
        if use_model:
            model = request.form.get("model", "").strip() or "gpt-41"
            client = MedtronicGPTClient(
                base_url=request.form.get("base_url", "").strip() or MedtronicGPTClient.DEFAULT_BASE_URL,
                api_version=request.form.get("api_version", "").strip() or MedtronicGPTClient.DEFAULT_API_VERSION,
                subscription_key=request.form.get("subscription_key", "").strip(),
                api_token=request.form.get("api_token", "").strip(),
                refresh_token=request.form.get("refresh_token", "").strip(),
            )
            try:
                draft = client.generate_completion(prompt, model=model)
            except MedtronicGPTError as exc:
                error = str(exc)

    return render_template_string(
        TEMPLATE,
        prompt=prompt,
        draft=draft,
        error=error,
        defaults={
            "base_url": MedtronicGPTClient.DEFAULT_BASE_URL,
            "api_version": MedtronicGPTClient.DEFAULT_API_VERSION,
            "model": "gpt-41",
        },
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
  <p>Upload your template, requirements, examples, and code context to build a prompt or generate a draft via MedtronicGPT.</p>

  {% if error %}
    <div style=\"color: red;\"><strong>Error:</strong> {{ error }}</div>
  {% endif %}

  <form method=\"post\" enctype=\"multipart/form-data\">
    <div class=\"section\">
      <label>Template</label><br>
      <input type=\"file\" name=\"template_file\"> or paste text:
      <textarea name=\"template_text\"></textarea>
    </div>

    <div class=\"section\">
      <label>Requirements and guidance</label><br>
      <input type=\"file\" name=\"requirements_file\"> or paste text:
      <textarea name=\"requirements_text\"></textarea>
    </div>

    <div class=\"section\">
      <label>Examples (upload multiple files)</label><br>
      <input type=\"file\" name=\"examples\" multiple> or paste an inline example:
      <textarea name=\"examples_text\"></textarea>
    </div>

    <div class=\"section\">
      <label>Code context (upload files and/or paste)</label><br>
      <input type=\"file\" name=\"code_files\" multiple>
      <textarea name=\"code_context\" placeholder=\"Paste relevant code snippets, configs, and notes...\"></textarea>
    </div>

    <div class=\"section\">
      <label>MedtronicGPT connection</label><br>
      <input type=\"checkbox\" name=\"use_model\" id=\"use_model\"> <label for=\"use_model\">Generate draft with MedtronicGPT</label><br>
      <div style=\"margin-left: 1rem;\">
        <div><label>Model</label><br><input type=\"text\" name=\"model\" value=\"{{ defaults.model }}\" style=\"width:100%\"></div>
        <div><label>Base URL</label><br><input type=\"text\" name=\"base_url\" value=\"{{ defaults.base_url }}\" style=\"width:100%\"></div>
        <div><label>API version</label><br><input type=\"text\" name=\"api_version\" value=\"{{ defaults.api_version }}\" style=\"width:100%\"></div>
        <div><label>Subscription key</label><br><input type=\"text\" name=\"subscription_key\" style=\"width:100%\"></div>
        <div><label>API token</label><br><input type=\"text\" name=\"api_token\" style=\"width:100%\"></div>
        <div><label>Refresh token</label><br><input type=\"text\" name=\"refresh_token\" style=\"width:100%\"></div>
      </div>
    </div>

    <button type=\"submit\">Build</button>
  </form>

  {% if prompt %}
    <div class=\"section\">
      <h2>Prompt</h2>
      <div class=\"output\">{{ prompt }}</div>
    </div>
  {% endif %}

  {% if draft %}
    <div class=\"section\">
      <h2>Generated Draft</h2>
      <div class=\"output\">{{ draft }}</div>
    </div>
  {% endif %}
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
