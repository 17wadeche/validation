# validation

A lightweight toolkit for generating Medtronic-style validation drafts from templates, guidance, examples, and source code.

## Quick start

1. Prepare inputs:
   - A validation template (see `examples/validation_template.md`).
   - Requirements and guidance (see `examples/requirements.md`).
   - A JSON array of example validations (see `examples/examples.json`).
   - Paths to the program code to be reflected in the documentation.

2. Generate a draft prompt or validation:

```bash
python cli.py examples/validation_template.md \
  examples/requirements.md \
  examples/examples.json \
  path/to/source/code \
  --output draft.md
```

The default behavior prints the assembled prompt. Provide an LLM callable to `ValidationAgent` to automatically produce drafts.

## Extending

- `src/validation_agent/prompt_builder.py` builds the prompt sections from the provided inputs.
- `src/validation_agent/agent.py` offers a composable interface and a `load_code_context` helper to include source files.
- Replace the `llm_callable` with your model integration (e.g., OpenAI client) to return the draft directly instead of the prompt.
