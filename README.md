# validation

A lightweight toolkit for generating Medtronic-style validation drafts from templates, guidance, examples, and source code.

## Quick start

1. Prepare inputs:
   - A validation template (Markdown or Word `.docx`; see `examples/validation_template.md`).
   - Requirements and guidance (Markdown, Word `.docx`, or PDF).
   - Examples: either `examples/examples.json`, individual example documents (`.md`, `.docx`, `.pdf`, `.txt`), or a directory containing any mix of those.
   - Paths to the program code to be reflected in the documentation.

2. Generate a draft prompt or validation:

```bash
python cli.py examples/validation_template.md \
  examples/requirements.md \
  examples/examples.json \
  path/to/source/code \
  --output draft.md
```

You can also substitute the template and example arguments with Word (`.docx`) files or PDFs to use your existing compliance artifacts. PDF extraction requires installing the optional dependency `pypdf`.

The default behavior prints the assembled prompt. Provide an LLM callable to `ValidationAgent` to automatically produce drafts.

## Web UI with MedtronicGPT

1. Install UI dependencies:

```bash
pip install flask
```

2. Start the UI server:

```bash
python webapp.py
```

3. Open `http://localhost:8000` and upload your template, requirements, examples, and code context. Optional: check **Generate draft with MedtronicGPT** and provide your `subscription-key`, `api-token`, `refresh-token`, and desired `model` (defaults to `gpt-41`; API version `3.0`, base URL `https://api.gpt-dev.medtronic.com`).

The UI will assemble the same prompt used by the CLI and, when credentials are provided, will request a draft from MedtronicGPT.

## Extending

- `src/validation_agent/prompt_builder.py` builds the prompt sections from the provided inputs.
- `src/validation_agent/agent.py` offers a composable interface and a `load_code_context` helper to include source files.
- Replace the `llm_callable` with your model integration (e.g., OpenAI client) to return the draft directly instead of the prompt.
