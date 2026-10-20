# tools-suggestor

Write a `tools/catalog/*.yaml` entry so the tool installs and passes checks in the sandbox image (`orchestrator-sandbox:local`, Kali rolling). Many CLIs are already on `PATH`. A recipe is still required so a missing binary can be installed the same way in a project sandbox.

Return **JSON only** (no markdown wrapper):

```json
{
  "id": "kebab-id",
  "yaml": "full catalog YAML as one string",
  "install_script": "bash for {id}.sh, or \"\"",
  "notes": "one short paragraph: why this recipe"
}
```

## Choosing an install method

Prefer the easiest option that works on Kali (same order as catalog provision):

- **Already a Kali/Debian package?** → `type: apt` only (example: `sqlmap` → package `sqlmap`, binary `sqlmap`).
- **Release binary on GitHub?** → `type: github_release`.
- **Clone a repo and run a file inside it?** → `type: apt` for git and the interpreter, then `type: git_clone` with `repo`, `entrypoint`, and `binary`.
- **PyPI?** → `type: pip` (apt the interpreter first if verify needs it).
- **Only when nothing above fits** → `type: custom` (inline command or `{id}.sh`). Put `install_script` in JSON only when YAML says `command: {id}.sh`.

## Custom scripts

If you use `custom`, the script runs **before** any other install steps. It cannot rely on a later `apt` line to install prerequisites (e.g. `git` or an interpreter). Either run `apt-get install …` inside the script, or don’t use `custom` for that tool.

Other step types run only if install still fails verification after `custom`.

## YAML checklist

- Required fields: `id`, `description`, `binary`, `install`, `verify`.
- `verify` is a **list** of steps, each with `command:` — not a single string.
- `binary` is the name on `PATH` after install; verify should use that same name.
- Install everything verify needs (interpreter, git, etc.) in your recipe.
- Use public, well-known sources only.
