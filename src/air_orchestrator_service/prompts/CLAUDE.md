### Prompts

`prompts/registry.py` holds prompts in Python as `route → version → Prompt`, selected by
`PromptSettings.pins` and otherwise by the newest version. `"latest"` is a real version, not
an alias: a prompt change stays reviewable and revertible like code.
