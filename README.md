# validation

A lightweight toolkit for generating Medtronic-style validation drafts from templates, examples, and source code.

## Quick start

1. Prepare inputs:
   - A validation template (Markdown or Word `.docx`; see `examples/validation_template.md`).
   - Examples: either `examples/examples.json`, individual example documents (`.md`, `.docx`, `.pdf`, `.txt`), or a directory containing any mix of those.
   - Paths to the program code to be reflected in the documentation.

2. Generate a draft prompt or validation:

```bash
python cli.py examples/validation_template.md \
  examples/examples.json \
  path/to/source/code \
  --output draft.md \
  --docx-output draft.docx
```

You can also substitute the template and example arguments with Word (`.docx`) files or PDFs to use your existing compliance artifacts. PDF extraction requires installing the optional dependency `pypdf`.

The template should carry all guidance and placeholders (e.g., blue text) that need to be completed—no separate requirements upload is necessary.

Prompts are assembled internally; the UI surfaces only the generated output. Provide an LLM callable to `ValidationAgent` to automatically produce drafts. Add `--docx-output` to emit a Word document (requires `python-docx`).

## Web UI with MedtronicGPT

1. Install UI dependencies (add `mammoth` for an HTML fallback preview):

```bash
pip install flask python-docx mammoth
```

2. Start the UI server:

```bash
python webapp.py
```

3. Open `http://localhost:8000` and upload your template, examples, and code context. The modernized layout highlights each step in cards with clear labels, so you can see what is saved and what will be reused at a glance. The UI uses files only for templates and examples—guidance should be embedded as blue placeholder text inside the template itself. **Generate draft with MedtronicGPT**, **Remember credentials on this machine**, and **Remember template and examples on this machine** are checked by default; provide your `subscription-key`, `api-token`, `refresh-token`, and desired `model` (defaults to `gpt-41`; API version `3.0`, base URL `https://api.gpt.medtronic.com`). The UI sends `subscription-key`, `api-token`, `refresh-token`, and `api-version` as headers and defaults to the path template `/models/{model}`—aligning with the working Postman example. Adjust the path if your gateway expects an alternate route.

   - Saved credentials live at `~/.validation_agent/credentials.json` for reuse in both drafting and chat.
   - Saved templates/examples live at `~/.validation_agent/inputs.json`. If you leave the upload fields empty on a later visit, the stored template/examples are preselected automatically; uncheck the saved file rows to drop them or upload replacements to overwrite.
- After a draft is generated, you can download it as a Word document directly from the UI; when you upload a Word template, the download reuses that template as the base document to preserve tables, charts, and formatting. The model is asked to return a JSON payload with a `placeholders` map plus a full `draft` string; the exporter uses the map to swap inline tokens (e.g., `<Tool Name>`, `<#.#.#>`, underscores) in-place and only inserts the full draft when no placeholders can be replaced—preventing duplicate content. Blue placeholder text inside the template is replaced in place—even when the color comes from theme accents, custom hex values, paragraph/run styles, highlight colors, or style-level placeholder names—so tables and inline formatting remain intact. Explicit placeholders `[[GENERATED_DRAFT]]`, `<GENERATED_DRAFT>`, or `{GENERATED_DRAFT}` are also honored. Footers are left unchanged. Template instructions that match the Medtronic purpose statement samples (e.g., the “Assurance (standard deliverables) sample purpose statement…” and “Validation (enhanced deliverables) sample purpose statement…” language) are also treated as placeholders and replaced directly inside the template.
   - The Generated Draft section now streams the DOCX through a client-side renderer so the preview mirrors the Word template (including tables and charts). If rendering fails or the browser blocks it, the page falls back to an HTML preview when `mammoth` is installed; otherwise raw draft text is shown.
   - Use the **Clarify or refine via chat** panel to ask MedtronicGPT follow-up questions when a template field or code behavior is unclear. The chat reuses the saved credentials and keeps conversation history in the page.

Draft responses are expected to mirror the template exactly—only placeholder text should change while headings, bullets, tables, and formatting remain intact. When the model lacks sufficient detail to fill a placeholder, it will ask concise clarifying questions instead of guessing.

The UI will assemble the same prompt used by the CLI and, when credentials are provided, will request a draft from MedtronicGPT. Validation instructions should come from the template (including any blue placeholder text).

## Notes on compilation check

Running `python -m compileall src cli.py webapp.py` will emit errors if bytecode generation fails; otherwise it completes quietly after listing the paths. You should see `__pycache__` directories appear beside the compiled files.

## Extending

- `src/validation_agent/prompt_builder.py` builds the prompt sections from the provided inputs.
- `src/validation_agent/agent.py` offers a composable interface and a `load_code_context` helper to include source files.
- Replace the `llm_callable` with your model integration (e.g., OpenAI client) to return the draft directly instead of the prompt.
