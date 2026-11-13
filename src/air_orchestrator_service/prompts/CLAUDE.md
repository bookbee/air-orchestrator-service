### Prompts

`prompts/registry.py` holds prompts in Python as `name → version → Prompt`
(`customer_care`, `business_analyst`), selected by `PromptSettings.pins` and otherwise by
`_LATEST`. The engine picks the name from the channel (spec 0006). `{brand_name}` and `{who}`
are filled from `PersonaSettings`.

Once a version has shipped, change it by adding a new version, never by editing it, and run
`make prompt-review` on both. When a review run shows a failure the flags missed, add a flag
for it in `tools/prompt_review.py` with a test, as `offers_to_act` was added.
