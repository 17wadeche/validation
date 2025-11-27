# validation

A lightweight toolkit for generating Medtronic-style validation drafts from templates, examples, and source code.

## Quick start

1. Prepare inputs:
   - A validation template (Markdown, Word `.docx`, or PDF; see `examples/validation_template.md`).
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

Add `--plan-only` to produce a planning JSON that lists every placeholder, suggested section mapping, and concise clarifying questions before drafting. Use `--plan-output` to save that plan or `--plan-input` to feed an existing plan back into the drafter so replacements land in the right spots.

You can also substitute the template and example arguments with Word (`.docx`) files or PDFs to use your existing compliance artifacts. PDF extraction requires installing the optional dependency `pypdf`.

The template should carry all guidance and placeholders (e.g., blue text) that need to be completed—no separate requirements upload is necessary.

Prompts are assembled internally; the UI surfaces only the generated output. Provide an LLM callable to `ValidationAgent` to automatically produce drafts. Add `--docx-output` to emit a Word document (requires `python-docx`).

## Web UI with MedtronicGPT

1. Install UI dependencies:

```bash
pip install flask  # add python-docx if you plan to use the CLI Word export
```

2. Start the UI server (it now binds to `127.0.0.1` by default to avoid internet scanners hitting the dev server; set `VALIDATION_UI_HOST=0.0.0.0` only if you intentionally need remote access):

```bash
python webapp.py
```

3. Open `http://localhost:8000` and upload your template, examples, and code context, then click **Generate draft**. The agent now runs the planning step automatically behind the scenes (inventorying placeholders, asking clarifying questions, and mapping where to write) before drafting, so you only press one button. The modernized layout highlights each step in cards with clear labels, so you can see what is saved and what will be reused at a glance. The UI uses files only for templates and examples—guidance should be embedded as blue placeholder text inside the template itself. **Generate draft with MedtronicGPT**, **Remember credentials on this machine**, and **Remember template and examples on this machine** are checked by default; provide your `subscription-key`, `api-token`, `refresh-token`, and desired `model` (defaults to `gpt-41`; API version `3.0`, base URL `https://api.gpt.medtronic.com`). The UI sends `subscription-key`, `api-token`, `refresh-token`, and `api-version` as headers and defaults to the path template `/models/{model}`—aligning with the working Postman example. Adjust the path if your gateway expects an alternate route.

    - Saved credentials live at `~/.validation_agent/credentials.json` for reuse in both drafting and chat.
    - The client automatically refreshes your `api-token` using the `refresh-token` when MedtronicGPT returns unauthorized responses, and updated tokens are saved when credential persistence is enabled.
   - Calls to MedtronicGPT default to a deterministic sampling temperature of `0.0` and a `max_tokens` cap of `32768` to reduce drift and keep answers complete; adjust the defaults in `MedtronicGPTClient` if your gateway expects different values.
   - Pick the release type (Initial vs Update). Initial release is the default; when you select Update, extra upload slots appear for **Previous** and **Current** examples/code. Files dropped there are tagged as OLD/NEW in the prompt so MedtronicGPT knows which content changed.
   - Saved templates/examples live at `~/.validation_agent/inputs.json`. If you leave the upload fields empty on a later visit, the stored template/examples are preselected automatically; uncheck the saved file rows to drop them, or check **Clear all saved examples** to remove them in one click before uploading replacements.
   - After a draft is generated, the UI shows the JSON `placeholders`, `answers`, and any remaining `questions`. Use **Copy all** to grab the JSON or switch to the **Easy view** toggle for a friendlier list of replacements and questions; nested objects and arrays are expanded in this view so you can see deeper fields without reading raw JSON. Word downloads are disabled for now. PDF templates and examples are still supported for prompt generation (install optional `pypdf` to ingest PDF content).
  - The page also runs a coverage check against the uploaded template: any detected placeholders that remain empty are listed, and if credentials are provided the app automatically resends the gaps plus context for another pass before routing the document.
   - Template guidance paragraphs that match the long “Blue text is included for reference…” block and the Medtronic sample purpose statement bullets are treated as placeholders in the returned answers so you know what to delete or replace in the template.
   - Any `questions` returned in the JSON are pulled out and displayed in their own card. Each question includes a text box so you can answer directly; the answers are merged back into the JSON with one click. When you need MedtronicGPT to reconcile repeated placeholders or reuse your new answers everywhere, click **Send answers to GPT** to resend the template, examples, code context, prior JSON, and your responses for an updated mapping. The planning step still runs in the background, but its detailed output stays hidden to reduce clutter.
   - Use the **Clarify or refine via chat** panel to ask MedtronicGPT follow-up questions when a template field or code behavior is unclear. The chat reuses the saved credentials and keeps conversation history in the page.

Draft responses carry a `placeholders` map **and a detailed `answers` list that spells out, for every blue instruction or `<token>`, exactly what to type (or delete) and where to place it**—only placeholder text should change while headings, bullets, tables, and formatting remain intact. The agent fills every placeholder it can using the provided template, examples, and code before requesting anything else; if details are still missing, it includes concise follow-up questions alongside the answers instead of withholding them.

The UI will assemble the same prompt used by the CLI and, when credentials are provided, will request a draft from MedtronicGPT. Validation instructions should come from the template (including any blue placeholder text).

## One-click Windows EXE (for non-technical users)

If colleagues don’t have Python installed, you can hand them a single executable that launches the same UI locally:

1. On Windows, install dependencies and PyInstaller:

   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt pyinstaller
   ```

2. Build the EXE (outputs `dist\validation-ui.exe`):

   ```powershell
   py packaging\build_exe.py
   ```

3. Share `dist\validation-ui.exe` (and any template/example files). Recipients double-click the EXE; it starts the local server, automatically opens the browser, and picks the next open port if `8000` is in use on their machine. No separate Python setup is required.

Tips:
- Keep the EXE and any saved templates/examples in the same folder when sharing to simplify hand-off.
- If the EXE is blocked by Windows SmartScreen, users can choose “More info” → “Run anyway” (the binary is unsigned). The app still runs only on localhost by default; if port 8000 is taken, it selects a free port automatically and opens the browser to that address.

## Notes on compilation check

Running `python -m compileall src cli.py webapp.py` will emit errors if bytecode generation fails; otherwise it completes quietly after listing the paths. You should see `__pycache__` directories appear beside the compiled files.

## Extending

- `src/validation_agent/prompt_builder.py` builds the prompt sections from the provided inputs.
- `src/validation_agent/agent.py` offers a composable interface and a `load_code_context` helper to include source files.
- Replace the `llm_callable` with your model integration (e.g., OpenAI client) to return the draft directly instead of the prompt.
