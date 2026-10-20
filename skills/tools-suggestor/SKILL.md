---
name: tools-suggestor
description: Suggest a tools/catalog YAML (and optional {id}.sh) for installing a CLI in the sandbox. Use when an operator describes a tool to add or asks how to install it.
allowed-tools: Bash Read
---

## Goal
Turn a natural-language tool request into a **valid catalog YAML** for the
Kali sandbox image. Prefer apt, then a GitHub release, then git clone, then
pip. Use a custom script only when those do not fit.

## Scripts
- `scripts/run.sh` — entry (argv; writes suggestion under `workspace/learn/`)

## Output
- `workspace/learn/tool.yaml` — suggested catalog entry
- `workspace/learn/tool.sh` — optional custom install script (only when needed)
- `workspace/learn/notes.md` — short rationale

See [prompt](references/PROMPT.md) for the JSON contract the authoring path uses.

## Run
```
run_skill_script("tools-suggestor", "scripts/run.sh", args='["Add a catalog entry for …"]')
```
